"""`summarize_period` and `get_attention_list` (ARCHITECTURE §8.2; S03.2).

Read tools over the owner's managed chats, built on the `summarize` run
type (`agent/summarize.py`):

- **`summarize_period`** — a catch-up over a time range (FR-SUM-01, J3):
  one period summary per active chat (map; cached while the range's
  messages are unchanged), combined here deterministically (grouped by
  importance), with the range's coverage caveats rendered by
  `memory/catch_up.py` (FR-SUM-06: possible/definite gaps, the period before
  the connection, Ignore-mode and possibly paused chats, expired and deleted
  messages; delayed-only gaps are never reported). The owner-facing wording
  is the owner model's job; the caveats must all be relayed (prompt rule).
  A chat whose contact asked after the owner's last answer is
  `may_be_waiting`, with that question, and needs attention (C1).
- **`get_attention_list`** — v1 (FR-SUM-03): chats where the contact wrote
  last and nobody answered, each with the application's own question rule
  (`may_be_waiting`: the contact asked after the owner's last answer, with
  that question; M03 fix round, C1) and the rolling summary's "needs reply"
  verdict (kept on while something is open to the owner, P9). Waiting chats
  are ranked first and the limit applies after ranking; what it cuts is
  counted (`not_listed`; may be waiting, not checked). A listed chat whose
  summary is stale is refreshed first — at most `MAX_INLINE_CHECKS` chats,
  ONE summary call each (plus one retry of a truncated output), never
  chained (M03 D62) — otherwise marked unchecked.

A chat whose summary fails is listed as not summarized (or unchecked); it
never aborts the other chats. A summary written from messages that changed
while the model ran is discarded and never returned (D63, D66).

Both: class `read`, `owner_request` only, offered only with `ai_managed`
(`returns_managed`), flags default off, every contact-authored or
model-written string wrapped as untrusted (the result taints the run), refs
returned for deletion, foreign ids answered as "not found". Each summary
call is its own `summarize` run with no tools and only ONE chat's messages
(§6 invariants 3a, 4).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.agent import owner_texts
from assistant.agent.attribution import ATTRIBUTION_RULE, check_attribution
from assistant.agent.citations import CitableMessage
from assistant.agent.locales import SupportedLocale, supported_locale
from assistant.agent.summarize import PeriodOutcome, RefreshOutcome, SummaryStatus
from assistant.agent.tools.common import (
    COVERAGE_RULE,
    ArgumentRuleError,
    OptionalId,
    account_coverage,
    local_time,
    local_to_utc,
    message_text,
    name,
    one,
    parse_local_moment,
    require_contacts,
    require_conversations,
    summaries_authors,
    summary_authors,
    summary_view,
)
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
)
from assistant.conversations.models import Contact, Conversation
from assistant.core.errors import NotInAccountError
from assistant.db.uow import transaction
from assistant.memory import catch_up, summaries
from assistant.memory.summaries import StoreResult
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

SUMMARIZE_PERIOD_FLAG = "tool.summarize_period"
ATTENTION_FLAG = "tool.get_attention_list"

#: The longest catch-up range.
MAX_PERIOD = timedelta(days=31)
#: Chats summarized per catch-up; the rest are listed as not summarized.
MAX_CATCH_UP_CONVERSATIONS = 15
#: Stale chats the attention list refreshes per call (M03 D62).
MAX_INLINE_CHECKS = 8
#: Chats the attention list ranks before its limit applies (C1), and the
#: largest limit the model may ask for.
MAX_ATTENTION_CANDIDATES = 100
MAX_ATTENTION_LIMIT = 20
#: Characters of a waiting question shown in the attention list.
QUESTION_LIMIT = 400
#: Summary calls in flight at once within one tool call.
SUMMARY_CONCURRENCY = 4
#: An open-ended catch-up also covers messages dated this far after our clock.
CLOCK_SKEW = timedelta(minutes=5)
#: Immediate retries of a chat whose summary was discarded because a message
#: was deleted, edited or expired during the call (owner, 2026-10-25;
#: D63/D66). A chat set to Ignore or found to be a bot chat is not retried.
CHANGED_RETRIES = 1
_RETRYABLE = frozenset({StoreResult.CHANGED, StoreResult.EXPIRED})
#: Reason (and attention `check`) per discard cause, when the chat is left out.
DISCARD_REASONS = {
    StoreResult.CHANGED: "changed_during_summary",
    StoreResult.EXPIRED: "expired_during_summary",
    StoreResult.IGNORED: "set_to_ignore_during_summary",
    StoreResult.BOT_CHAT: "bot_chat_during_summary",
}
CHANGED_DURING_SUMMARY = DISCARD_REASONS[StoreResult.CHANGED]
_NOTICE_CAUSE = {
    StoreResult.CHANGED: (
        "changed while it was being summarized (a message was deleted or edited), so it was "
        "left out. Ask again shortly to include it."
    ),
    StoreResult.EXPIRED: (
        "had messages expire under the retention setting while it was being summarized, so "
        "it was left out. Ask again shortly to include what is still stored."
    ),
    StoreResult.IGNORED: (
        "was set to Ignore mode while it was being summarized, so it was left out: chats in "
        "Ignore mode are not summarized and their new messages are not stored."
    ),
    StoreResult.BOT_CHAT: (
        "is a chat with a bot (this showed while it was being summarized), so it was left out "
        "and its stored messages were removed: bot chats are not stored or summarized."
    ),
}


def discard_notice(conversation_id: int, cause: StoreResult) -> str:
    """Our own owner-facing words for a chat left out because it changed
    while it was being summarized, per cause. Content-free: what changed is
    never quoted or described."""
    return f"The chat with conversation_id {conversation_id} {_NOTICE_CAUSE[cause]}"


def changed_notice(conversation_id: int) -> str:
    """The notice for a deletion or edit (the common cause)."""
    return discard_notice(conversation_id, StoreResult.CHANGED)


def _moment(value: str | None) -> str | None:
    parse_local_moment(value)
    return value


def _bounded(low: int, high: int) -> AfterValidator:
    def check(value: int | None) -> int | None:
        if value is not None and not low <= value <= high:
            raise ArgumentRuleError(f"must be between {low} and {high}")
        return value

    return AfterValidator(check)


async def _bounded_gather[T](
    work: Sequence[Callable[[], Awaitable[T]]], *, limit: int = SUMMARY_CONCURRENCY
) -> list[T]:
    semaphore = asyncio.Semaphore(limit)

    async def run(item: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await item()

    return list(await asyncio.gather(*(run(item) for item in work)))


# ------------------------------------------------------------ summarize_period
class SummarizePeriodArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since: Annotated[str, AfterValidator(_moment)] = Field(
        description="Start of the period: 'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD' for midnight) "
        "in the owner's timezone."
    )
    until: Annotated[str | None, AfterValidator(_moment)] = Field(
        description="End of the period, same format, or null for now."
    )
    contact_id: OptionalId = Field(description="Only the chat with this contact, or null.")
    conversation_id: OptionalId = Field(description="Only this conversation, or null.")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        since, until = parse_local_moment(self.since), parse_local_moment(self.until)
        if since is not None and until is not None and since >= until:
            raise ArgumentRuleError("since must be before until")
        if self.contact_id is not None and self.conversation_id is not None:
            raise ArgumentRuleError(
                "give at most one of contact_id or conversation_id; set the other to null"
            )
        return self


_FAILED_REASONS = {
    SummaryStatus.NO_CONSENT: "no_consent_for_the_summary_model",
    SummaryStatus.OVER_BUDGET: "monthly_ai_budget_reached",
    SummaryStatus.REFUSED: "the_model_refused",
    SummaryStatus.FAILED: "summary_failed",
    SummaryStatus.NOT_FOUND: "not_found",
    SummaryStatus.SKIPPED: "chat_not_summarized_ignore_or_bot",
}


def _waiting(
    conversation_id: int,
    contact_name: str | None,
    question: catch_up.WaitingQuestion | None,
    *,
    locale: SupportedLocale,
    tz: str,
) -> tuple[dict[str, Any], dict[int, CitableMessage]]:
    """A chat's `may_be_waiting` (C1) and, when set, our own words for why and
    the question itself — the contact's, wrapped as untrusted — which the
    reply may cite (S03.7 B3): the fields, and the question as citable."""
    if question is None:
        return {"may_be_waiting": False}, {}
    fields = {
        "may_be_waiting": True,
        "waiting_reason": owner_texts.MAY_BE_WAITING[locale],
        "question": {
            "message_id": question.message_id,
            "sent_at": local_time(question.sent_at, tz),
            "text": message_text(question.text, "contact", limit=QUESTION_LIMIT),
        },
    }
    source = CitableMessage(
        conversation_id=conversation_id,
        contact_name=contact_name,
        sent_at=question.sent_at,
        direction="contact",
    )
    return fields, {question.message_id: source}


async def summarize_period_tool(ctx: ToolContext, args: SummarizePeriodArgs) -> ToolOutcome:
    services = ctx.services
    now = services.clock.now()
    async with transaction(services.session_factory) as uow:
        session = uow.session
        account = await session.get(Account, ctx.account_id)
        if account is None:
            raise NotInAccountError("account")
        tz = account.timezone
        since = parse_local_moment(args.since)
        until = parse_local_moment(args.until)
        assert since is not None
        start = local_to_utc(since, tz)
        end = now if until is None else min(local_to_utc(until, tz), now)
        open_ended = until is None or end == now
        if start >= end:
            return ToolOutcome("error", {"reason": "the period has not started yet"})
        if end - start > MAX_PERIOD:
            return ToolOutcome("error", {"reason": "a period is at most 31 days"})
        # "Until now" also takes messages Telegram dated a little after our
        # clock (clock skew); a fixed end is taken as given.
        query_end = end + CLOCK_SKEW if open_ended else end
        await require_contacts(session, ctx.account_id, one(args.contact_id))
        await require_conversations(session, ctx.account_id, one(args.conversation_id))
        scope: list[int] | None = None
        if args.conversation_id is not None:
            scope = [args.conversation_id]
        elif args.contact_id is not None:
            scope = [
                int(i)
                for i in (
                    await session.execute(
                        select(Conversation.id)
                        .where(Conversation.account_id == ctx.account_id)
                        .where(Conversation.contact_id == args.contact_id)
                    )
                ).scalars()
            ]
        active = await catch_up.active_in_period(
            session,
            ctx.account_id,
            start=start,
            end=query_end,
            conversation_ids=one(args.conversation_id),
            contact_ids=one(args.contact_id),
        )
        coverage = await catch_up.period_coverage(
            session, ctx.account_id, start=start, end=query_end, conversation_ids=scope
        )
        activity_counts = await catch_up.assistant_activity(
            session,
            account_id=ctx.account_id,
            start=start,
            end=query_end,
            conversation_ids=scope,
        )
        activity_entries = await _assistant_activity_entries(
            session, ctx.account_id, activity_counts
        )

    chosen = active[:MAX_CATCH_UP_CONVERSATIONS]
    summarizer = services.summarizer

    def job(conversation_id: int) -> Callable[[], Awaitable[PeriodOutcome]]:
        async def run() -> PeriodOutcome:
            if summarizer is None:
                return PeriodOutcome(SummaryStatus.FAILED)
            try:
                outcome = PeriodOutcome(SummaryStatus.DISCARDED)
                # The owner asked: a chat that changed during the call (a
                # deletion, an edit or an expiry) is retried once at once, with
                # the fresh messages (D63/D66); a second change — or a chat
                # set to Ignore or found to be a bot chat — leaves it out.
                for _ in range(1 + CHANGED_RETRIES):
                    outcome = await summarizer.summarize_period(
                        account_id=ctx.account_id,
                        conversation_id=conversation_id,
                        start=start,
                        end=query_end,
                        open_ended=open_ended,
                        parent_run_id=ctx.run_id,
                    )
                    if outcome.discard_reason not in _RETRYABLE:
                        break
                return outcome
            except Exception as error:
                # One chat's failure (its run already closed as failed) must
                # not abort the whole catch-up; it is listed as not summarized.
                logger.warning(
                    "catch_up_chat_failed",
                    conversation_id=conversation_id,
                    exc_type=type(error).__name__,
                )
                return PeriodOutcome(SummaryStatus.FAILED)

        return run

    outcomes = await _bounded_gather([job(item.conversation_id) for item in chosen])
    locale = supported_locale(account.locale_pref)
    # Who wrote what each summary cites, for the read-time attribution check
    # (S03.7): each chat's own messages only.
    authors: dict[int, dict[int, str]] = {}
    async with transaction(services.session_factory) as uow:
        for item, outcome in zip(chosen, outcomes, strict=True):
            if outcome.summary is not None:
                authors[item.conversation_id] = await summary_authors(
                    uow.session, ctx.account_id, item.conversation_id, outcome.summary
                )
        # The application's own question rule (C1), read after the summary
        # calls, so a question deleted meanwhile is not evidence (D63).
        asked = await catch_up.waiting_questions(
            uow.session,
            ctx.account_id,
            [item.conversation_id for item in chosen if item.awaiting_owner_reply],
        )

    entries: list[dict[str, Any]] = []
    not_summarized: list[dict[str, Any]] = []
    notices: list[str] = []
    citable: dict[int, CitableMessage] = {}
    for item, outcome in zip(chosen, outcomes, strict=True):
        waits = item.conversation_id in asked
        waiting, cites = _waiting(
            item.conversation_id,
            item.display_name,
            asked.get(item.conversation_id),
            locale=locale,
            tz=tz,
        )
        citable.update(cites)
        base = {
            "conversation_id": item.conversation_id,
            "contact_id": item.contact_id,
            "contact_name": name(item.display_name),
            "contact_username": name(item.username),
            "messages_in_period": item.messages_in_period,
            "awaiting_owner_reply": item.awaiting_owner_reply,
            **waiting,
        }
        if outcome.summary is None:
            if summarizer is None:
                reason = "summaries_unavailable"
            elif outcome.discard_reason is not None:
                reason = DISCARD_REASONS[outcome.discard_reason]
                notices.append(discard_notice(item.conversation_id, outcome.discard_reason))
            else:
                reason = _FAILED_REASONS.get(outcome.status, outcome.status.value)
            not_summarized.append({**base, "reason": reason})
            continue
        checked, _ = check_attribution(outcome.summary, authors[item.conversation_id])
        entry = {
            **base,
            "importance": catch_up.importance(
                checked, awaiting_owner_reply=item.awaiting_owner_reply, asked_question=waits
            ),
            "summary": summary_view(
                checked,
                contact_name=summaries.preferred_name(item.display_name, item.username),
                locale=locale,
                authors=authors[item.conversation_id],
            ),
        }
        if outcome.summarized_count < outcome.message_count:
            entry["summary_covers"] = (
                f"only the latest {outcome.summarized_count} of {outcome.message_count} messages"
            )
        entries.append(entry)
    for item in active[MAX_CATCH_UP_CONVERSATIONS:]:
        not_summarized.append(
            {
                "conversation_id": item.conversation_id,
                "contact_id": item.contact_id,
                "contact_name": name(item.display_name),
                "messages_in_period": item.messages_in_period,
                "reason": "too_many_chats_in_one_catch_up",
            }
        )
    order = {group: index for index, group in enumerate(catch_up.IMPORTANCE_ORDER)}
    entries.sort(key=lambda e: (order[e["importance"]], -e["messages_in_period"]))

    lines = catch_up.caveats(coverage, tz)
    data: dict[str, Any] = {
        "period": {"from": local_time(start, tz), "to": local_time(end, tz), "timezone": tz},
        "conversations": entries,
        "conversation_count": len(entries),
        "not_summarized": not_summarized,
        "coverage_caveats": lines,
        "coverage_rule": COVERAGE_RULE,
        "assistant_activity": activity_entries,
    }
    if lines:
        data["note"] = "Relay every coverage caveat to the owner; none may be dropped."
    if notices:
        data["notices"] = notices
    if not active:
        data["note_empty"] = (
            "No stored messages in this period. That does not mean nothing was said: "
            "see the coverage caveats and rule."
        )
    involved = [*chosen, *active[MAX_CATCH_UP_CONVERSATIONS:]]
    extra_conv_ids = frozenset(c.conversation_id for c in activity_counts)
    extra_contact_ids = frozenset(c.contact_id for c in activity_counts)
    return ToolOutcome(
        "ok",
        data,
        contains_managed=True,
        refs=ToolRefs(
            conversation_ids=frozenset(i.conversation_id for i in involved) | extra_conv_ids,
            contact_ids=frozenset(i.contact_id for i in involved) | extra_contact_ids,
        ),
        citable=citable,
    )


async def _assistant_activity_entries(
    session: AsyncSession,
    account_id: int,
    counts: Sequence[catch_up.ActivityCount],
) -> list[dict[str, Any]]:
    if not counts:
        return []
    contact_ids = list({c.contact_id for c in counts})
    contacts = (
        await session.execute(
            select(Contact.id, Contact.display_name, Contact.username)
            .where(Contact.account_id == account_id)
            .where(Contact.id.in_(contact_ids))
        )
    ).all()
    names_by_id = {c_id: disp or (f"@{user}" if user else None) for c_id, disp, user in contacts}
    entries: list[dict[str, Any]] = []
    for c in counts:
        contact_name = names_by_id.get(c.contact_id)
        entries.append(
            {
                "conversation_id": c.conversation_id,
                "contact_id": c.contact_id,
                "contact_name": name(contact_name),
                "sent": c.sent,
                "awaiting": c.awaiting,
                "not_sent": c.not_sent,
                "problems": c.problems,
                "deleted_after_send": c.deleted_after_send,
            }
        )
    return entries


SUMMARIZE_PERIOD = ToolDefinition(
    name="summarize_period",
    description=(
        "Catch-up over a time range in the owner's chats with other people (all chats, or "
        "one contact or conversation): a summary per active chat — open questions to the "
        "owner, decisions requested, commitments, key facts — grouped with the chats "
        "needing the owner first, plus coverage caveats that must all be relayed."
    ),
    args_model=SummarizePeriodArgs,
    capability=CapabilityClass.READ,
    handler=summarize_period_tool,
    release_flag=SUMMARIZE_PERIOD_FLAG,
    returns_managed=True,
)


# ------------------------------------------------------------ get_attention_list
class GetAttentionListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_within_days: Annotated[int | None, _bounded(1, 30)] = Field(
        description="Only chats whose contact wrote in the last N days, 1-30 (default 7), or null."
    )
    limit: Annotated[int | None, _bounded(1, MAX_ATTENTION_LIMIT)] = Field(
        description="How many chats, 1-20 (default 10), or null."
    )


ATTENTION_RULE = (
    "Listed: chats where the contact wrote last and neither the owner nor the assistant "
    "answered since (an automatic away or greeting message is not an answer). Chats that "
    "may be waiting come first; waiting_count counts them. may_be_waiting is the "
    "application's own check, not a model's: the contact asked a question after the "
    "owner's last answer (question is that message). List every such chat as possibly "
    "waiting, with its waiting_reason, even when needs_reply is false. needs_reply is the "
    "chat summary's verdict, kept true while a question, decision or request to the owner "
    "is open; null means it could not be checked now, never that nothing is needed. A "
    "check ending in _during_summary means the chat changed while it was being checked — "
    "relay its notice. requests_to_owner: what the contact asked the owner to do that the "
    "owner has not agreed to. not_listed: more such chats beyond the limit — say how many, "
    "how many of them may be waiting and how many were not checked (unknown, never "
    "'nothing needed')."
)


def _rank(needs_reply: bool | None, may_be_waiting: bool) -> int:
    """Waiting first (the question rule or the summary's verdict), then the
    chats that could not be checked, then the chats needing nothing."""
    if may_be_waiting or needs_reply:
        return 0
    return 1 if needs_reply is None else 2


async def get_attention_list_tool(ctx: ToolContext, args: GetAttentionListArgs) -> ToolOutcome:
    services = ctx.services
    now = services.clock.now()
    days = args.active_within_days or 7
    limit = args.limit or 10
    async with transaction(services.session_factory) as uow:
        session = uow.session
        account = await session.get(Account, ctx.account_id)
        if account is None:
            raise NotInAccountError("account")
        found = await catch_up.unanswered(
            session,
            ctx.account_id,
            since=now - timedelta(days=days),
            limit=MAX_ATTENTION_CANDIDATES,
        )
        found_ids = [c.conversation_id for c in found]
        rolling = await summaries.rolling_summaries(session, ctx.account_id, found_ids)
        fresh = {
            c.conversation_id: rolling[c.conversation_id].summary
            for c in found
            if not c.summary_stale and c.conversation_id in rolling
        }
        fresh_authors = await summaries_authors(session, ctx.account_id, fresh)
        asked_before = await catch_up.waiting_questions(session, ctx.account_id, found_ids)

    # The limit applies after ranking (C1): waiting chats first. A fresh
    # stored summary ranks on the verdict the owner will see (`summary_view`:
    # P9 on the attribution-checked summary; unchecked, a v1-v4 row could rank
    # a chat late as well as early); a stale or missing one is unchecked.
    # sorted() is stable: within a rank the chats waiting longest stay first.
    known = {
        conversation_id: catch_up.needs_reply(
            check_attribution(summary, fresh_authors[conversation_id])[0]
        )
        for conversation_id, summary in fresh.items()
    }
    first_rank = {
        c.conversation_id: _rank(known.get(c.conversation_id), c.conversation_id in asked_before)
        for c in found
    }
    ranked = sorted(found, key=lambda c: first_rank[c.conversation_id])
    candidates, cut = ranked[:limit], ranked[limit:]
    cut_waiting = sum(1 for c in cut if first_rank[c.conversation_id] == 0)
    cut_unchecked = sum(1 for c in cut if first_rank[c.conversation_id] == 1)

    stale = [
        c.conversation_id for c in candidates if c.summary_stale or c.conversation_id not in rolling
    ][:MAX_INLINE_CHECKS]
    summarizer = services.summarizer
    changed: dict[int, StoreResult] = {}
    if stale and summarizer is not None:

        def job(conversation_id: int) -> Callable[[], Awaitable[RefreshOutcome | None]]:
            async def run() -> RefreshOutcome | None:
                try:
                    # One call per chat (plus a truncation retry), never
                    # chained (D62) — except one immediate retry when the
                    # chat changed during the call (owner, D63/D66).
                    outcome = await summarizer.refresh(conversation_id, max_calls=1)
                    for _ in range(CHANGED_RETRIES):
                        if outcome.discard_reason not in _RETRYABLE:
                            break
                        outcome = await summarizer.refresh(conversation_id, max_calls=1)
                    return outcome
                except Exception as error:
                    logger.warning(
                        "attention_check_failed",
                        conversation_id=conversation_id,
                        exc_type=type(error).__name__,
                    )
                    return None

            return run

        outcomes = await _bounded_gather([job(conversation_id) for conversation_id in stale])
        changed = {
            conversation_id: outcome.discard_reason
            for conversation_id, outcome in zip(stale, outcomes, strict=True)
            if outcome is not None and outcome.discard_reason is not None
        }

    async with transaction(services.session_factory) as uow:
        session = uow.session
        ids = [c.conversation_id for c in candidates]
        rolling = await summaries.rolling_summaries(session, ctx.account_id, ids)
        pending = {
            int(conversation_id)
            for conversation_id in (
                await session.execute(
                    select(Conversation.id)
                    .where(Conversation.account_id == ctx.account_id)
                    .where(Conversation.id.in_(ids))
                    .where(Conversation.pending_ai_since.is_not(None))
                )
            ).scalars()
        }
        coverage = await account_coverage(session, ctx.account_id, account.timezone)
        authors = await summaries_authors(
            session,
            ctx.account_id,
            {conversation_id: stored.summary for conversation_id, stored in rolling.items()},
        )
        # Read again after the summary calls: a question deleted or expired
        # meanwhile must never be shown (D63).
        asked = await catch_up.waiting_questions(session, ctx.account_id, ids)

    tz = account.timezone
    locale = supported_locale(account.locale_pref)
    entries: list[dict[str, Any]] = []
    citable: dict[int, CitableMessage] = {}
    for candidate in candidates:
        stored = rolling.get(candidate.conversation_id)
        checked = (
            stored is not None
            and candidate.conversation_id not in pending
            and candidate.conversation_id not in changed
        )
        waiting, cites = _waiting(
            candidate.conversation_id,
            candidate.display_name,
            asked.get(candidate.conversation_id),
            locale=locale,
            tz=tz,
        )
        citable.update(cites)
        entry: dict[str, Any] = {
            "conversation_id": candidate.conversation_id,
            "contact_id": candidate.contact_id,
            "contact_name": name(candidate.display_name),
            "contact_username": name(candidate.username),
            "waiting_since": local_time(candidate.last_contact_msg_at, tz),
            "last_answer": local_time(candidate.last_answer_at, tz),
            "needs_reply": None,
            "check": (
                DISCARD_REASONS[changed[candidate.conversation_id]]
                if candidate.conversation_id in changed
                else "not_checked"
            ),
            **waiting,
        }
        if checked and stored is not None:
            view = summary_view(
                stored.summary,
                contact_name=summaries.preferred_name(candidate.display_name, candidate.username),
                locale=locale,
                authors=authors[candidate.conversation_id],
            )
            entry.update(
                {
                    "needs_reply": view["needs_reply"],
                    "check": "model",
                    "reason": view["needs_reply_reason"],
                    "open_questions_to_owner": view["open_questions_to_owner"],
                    "decisions_requested": view["decisions_requested"],
                    # What the contact asked the owner to do, not yet agreed (S03.7).
                    "requests_to_owner": [
                        item
                        for item in view["commitments"]
                        if item["by"] == "owner" and item["status"] == "requested"
                    ],
                }
            )
        entries.append(entry)
    entries.sort(key=lambda e: _rank(e["needs_reply"], e["may_be_waiting"]))
    waiting_count = sum(1 for e in entries if _rank(e["needs_reply"], e["may_be_waiting"]) == 0)

    data: dict[str, Any] = {
        "conversations": entries,
        "count": len(entries),
        "waiting_count": waiting_count,
        "rule": ATTENTION_RULE,
        "window_days": days,
        "timezone": tz,
        "coverage": coverage,
        "attribution_rule": ATTRIBUTION_RULE,
    }
    if not entries:
        data["note"] = "No stored chat is waiting for the owner's reply in this window."
    if cut:
        more = f"at least {len(cut)}" if len(found) == MAX_ATTENTION_CANDIDATES else str(len(cut))
        larger = (
            f" A larger limit (at most {MAX_ATTENTION_LIMIT}) lists more of them."
            if limit < MAX_ATTENTION_LIMIT
            else ""
        )
        data["not_listed"] = len(cut)
        data["not_listed_may_be_waiting"] = cut_waiting
        # A cut chat nobody checked is unknown, never "not waiting".
        data["not_listed_not_checked"] = cut_unchecked
        data["note_not_listed"] = (
            f"{more} more chat(s) where the contact wrote last are not listed (limit {limit}): "
            f"{cut_waiting} may be waiting, {cut_unchecked} not checked (unknown, never "
            f"'nothing needed'). Say so.{larger}"
        )
    if changed:
        data["notices"] = [
            discard_notice(c.conversation_id, changed[c.conversation_id])
            for c in candidates
            if c.conversation_id in changed
        ]
    # Counts only: C1 could not be diagnosed from the logs.
    logger.info(
        "attention_list_built",
        candidates=len(found),
        listed=len(entries),
        waiting=waiting_count,
        may_be_waiting=sum(1 for e in entries if e["may_be_waiting"]),
        not_checked=sum(1 for e in entries if e["check"] == "not_checked"),
        inline_checks=len(stale) if summarizer is not None else 0,
    )
    return ToolOutcome(
        "ok",
        data,
        contains_managed=True,
        refs=ToolRefs(
            conversation_ids=frozenset(c.conversation_id for c in candidates),
            contact_ids=frozenset(c.contact_id for c in candidates),
        ),
        citable=citable,
    )


GET_ATTENTION_LIST = ToolDefinition(
    name="get_attention_list",
    description=(
        "Chats waiting for the owner: the contact wrote last and nobody answered (an "
        "automatic away message is not an answer). Each with when the contact wrote; "
        "whether it may be waiting (the application's own check: a question after the "
        "owner's last answer, with that question); whether the chat's summary says a reply "
        "is needed and why; and open questions, decisions or requests to the owner. Chats "
        "that may be waiting come first."
    ),
    args_model=GetAttentionListArgs,
    capability=CapabilityClass.READ,
    handler=get_attention_list_tool,
    release_flag=ATTENTION_FLAG,
    returns_managed=True,
)
