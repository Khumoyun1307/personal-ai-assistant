"""Message search over retained managed conversations (ARCHITECTURE §11.2).

Domain layer, SQL only. One query per query variant (the model may send the
same question in Latin, Cyrillic, Russian and English), merged by best score:

- every query is **account-scoped** (§6 invariant 12) and sees only retained
  content: not deleted, not purged, with a search key;
- a row matches on words (`search_tsv @@ tsquery`, terms as prefixes with
  light Uzbek stem variants) **or** on trigram word similarity of the whole
  normalized query (`<%`, which typos and other scripts still reach);
- score = 0.7 · `ts_rank_cd` (normalized to [0, 1)) + 0.3 · `word_similarity`
  + a small recency bonus, so a fuzzy hit never outranks a real word match
  by much, and among equals the newer message wins;
- all words must match first (AND); when no variant matched on all its words
  (only weak trigram hits, or nothing), any word will do (OR), and the
  result says so (`relaxed`).
- query terms carry typed-spelling alternatives (zh/j, kh/x, y/i, …) and
  Russian case siblings on the query side only
  (`memory/search.py::term_alternatives`);
- on request, a search narrowed to a contact or conversation that finds
  nothing is run again over all the account's chats (`widened`, S03.7 B1).

Every value reaches Postgres as a bound parameter. The tsquery string is
built from `[a-z0-9]+` terms only (`memory/search.py`), so it cannot carry
operators either.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Float,
    Select,
    and_,
    cast,
    extract,
    func,
    literal,
    literal_column,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnClause

from assistant.conversations.models import Conversation, Message
from assistant.memory.search import (
    TsqueryMode,
    build_tsquery,
    content_terms,
    normalize,
    query_terms,
)

#: How many variants of one question are searched, and how many hits returned.
MAX_QUERIES = 6
MAX_LIMIT = 25
#: Weights of the score (§11.2). The recency bonus decays over ~a month.
WEIGHT_TS = 0.7
WEIGHT_TRGM = 0.3
RECENCY_BONUS = 0.05
RECENCY_DAYS = 30.0

_SIMPLE: ColumnClause[Any] = literal_column("'simple'::regconfig")


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Optional narrowing. Ids are trusted only after the caller checked they
    belong to the account; the query is account-scoped regardless."""

    contact_ids: tuple[int, ...] = ()
    conversation_ids: tuple[int, ...] = ()
    since: datetime | None = None
    until: datetime | None = None
    directions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    message_id: int
    conversation_id: int
    contact_id: int
    direction: str
    sent_at: datetime
    text: str | None
    score: float
    #: Matched on words (`search_tsv @@ tsquery`), not only by trigrams.
    word_match: bool = False


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    #: Normalized queries that were searched (empty ones are skipped).
    searched: tuple[str, ...] = ()
    #: True when no variant matched on all words and any-word matching was used.
    relaxed: bool = False
    #: True when a search narrowed to a contact or conversation found nothing
    #: and was run again over all chats (`widen_empty_scope`, S03.7 B1).
    widened: bool = False


def _statement(
    *,
    account_id: int,
    key: str,
    tsquery: str | None,
    filters: SearchFilters,
    now: datetime,
    limit: int,
) -> Select[Any]:
    query = func.to_tsquery(_SIMPLE, literal(tsquery)) if tsquery is not None else None
    trigram = literal(key).op("<%")(Message.search_key)
    words = Message.search_tsv.op("@@")(query) if query is not None else literal(False)
    matched = trigram if query is None else or_(words, trigram)
    ts_rank = (
        func.coalesce(func.ts_rank_cd(Message.search_tsv, query, 32), 0.0)
        if query is not None
        else literal(0.0)
    )
    age_days = extract("epoch", literal(now) - Message.sent_at) / 86400.0
    recency = RECENCY_BONUS / (1.0 + func.greatest(age_days, 0.0) / RECENCY_DAYS)
    score = cast(
        WEIGHT_TS * ts_rank
        + WEIGHT_TRGM * func.word_similarity(literal(key), Message.search_key)
        + recency,
        Float,
    ).label("score")

    conditions = [
        Message.account_id == account_id,
        Conversation.account_id == account_id,
        Message.deleted_at.is_(None),
        Message.purged_at.is_(None),
        Message.search_key.is_not(None),
        Message.direction.is_not(None),
        Message.sent_at.is_not(None),
        matched,
    ]
    if filters.contact_ids:
        conditions.append(Conversation.contact_id.in_(filters.contact_ids))
    if filters.conversation_ids:
        conditions.append(Message.conversation_id.in_(filters.conversation_ids))
    if filters.since is not None:
        conditions.append(Message.sent_at >= filters.since)
    if filters.until is not None:
        conditions.append(Message.sent_at < filters.until)
    if filters.directions:
        conditions.append(Message.direction.in_(filters.directions))

    statement: Select[Any] = (
        select(
            Message.id,
            Message.conversation_id,
            Conversation.contact_id,
            Message.direction,
            Message.sent_at,
            Message.text,
            score,
            func.coalesce(words, False).label("word_match"),
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(and_(*conditions))
        .order_by(score.desc(), Message.sent_at.desc(), Message.id.desc())
        .limit(limit)
    )
    return statement


async def _run(
    session: AsyncSession,
    *,
    account_id: int,
    key: str,
    terms: list[str],
    mode: TsqueryMode,
    filters: SearchFilters,
    now: datetime,
    limit: int,
) -> list[SearchHit]:
    statement = _statement(
        account_id=account_id,
        key=key,
        tsquery=build_tsquery(terms, mode),
        filters=filters,
        now=now,
        limit=limit,
    )
    rows = (await session.execute(statement)).all()
    return [
        SearchHit(
            message_id=int(row[0]),
            conversation_id=int(row[1]),
            contact_id=int(row[2]),
            direction=str(row[3]),
            sent_at=row[4],
            text=row[5],
            score=float(row[6]),
            word_match=bool(row[7]),
        )
        for row in rows
    ]


async def search_messages(
    session: AsyncSession,
    account_id: int,
    queries: Sequence[str],
    *,
    now: datetime,
    filters: SearchFilters | None = None,
    limit: int = 10,
    widen_empty_scope: bool = False,
) -> SearchResult:
    """The best `limit` messages matching any of `queries`, best first.

    With `widen_empty_scope`, a search narrowed to a contact or conversation
    that finds nothing runs again without those two filters — dates,
    directions and the account scope stay — and says so (`widened`): the
    owner may have named the wrong person, or the message is in another chat
    (S03.7 B1). The caller must tell the owner whose messages they are.
    """
    if account_id <= 0:
        raise ValueError("a positive account_id is required")
    filters = filters or SearchFilters()
    result = await _search(session, account_id, queries, now=now, filters=filters, limit=limit)
    if result.hits or not widen_empty_scope:
        return result
    if not (filters.contact_ids or filters.conversation_ids):
        return result
    wider = replace(filters, contact_ids=(), conversation_ids=())
    result = await _search(session, account_id, queries, now=now, filters=wider, limit=limit)
    return replace(result, widened=True)


async def _search(
    session: AsyncSession,
    account_id: int,
    queries: Sequence[str],
    *,
    now: datetime,
    filters: SearchFilters,
    limit: int,
) -> SearchResult:
    limit = max(1, min(limit, MAX_LIMIT))
    variants: dict[str, list[str]] = {}
    for raw in list(queries)[:MAX_QUERIES]:
        key = normalize(raw)
        if key:
            variants.setdefault(key, content_terms(query_terms(raw)))

    best: dict[int, SearchHit] = {}

    async def collect(mode: TsqueryMode) -> None:
        for key, terms in variants.items():
            if mode == "or" and len(terms) < 2:
                continue  # one word: OR is the same query again
            for hit in await _run(
                session,
                account_id=account_id,
                key=key,
                terms=terms,
                mode=mode,
                filters=filters,
                now=now,
                limit=limit,
            ):
                kept = best.get(hit.message_id)
                if kept is None or hit.score > kept.score:
                    best[hit.message_id] = hit

    await collect("and")
    relaxed = False
    if not any(hit.word_match for hit in best.values()):
        # No variant matched on all its words (at most weak trigram hits):
        # any word will do. Trigram-only hits of the AND pass are kept.
        before = set(best)
        await collect("or")
        relaxed = bool(set(best) - before) or any(hit.word_match for hit in best.values())
    ranked = sorted(best.values(), key=lambda h: (-h.score, -h.sent_at.timestamp(), -h.message_id))
    return SearchResult(hits=tuple(ranked[:limit]), searched=tuple(variants), relaxed=relaxed)
