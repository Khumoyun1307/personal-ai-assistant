"""`reindex-search`: re-key retained messages after a normalizer change
(ARCHITECTURE §11.2).

The applied normalizer version is global (`system_state.normalizer_version`);
there are no per-row versions. When the code's `NORMALIZER_VERSION` is newer,
this rebuilds `messages.search_key` (and `lang_guess`) for **every** retained
row, then records the new version. `search_tsv` is generated from the key by
Postgres, so it follows.

Safe to run at any time, as often as wanted:

- **batched, short transactions** — each batch is read in one transaction and
  written in another; no transaction is held while keys are computed;
- **an edit always wins** — a row is updated only if its `content_hash` is
  still the one that was read, so an edit committed meanwhile (which wrote
  its own, current key) is never overwritten with a key of the old text;
- **idempotent** — rows whose key is already right are not touched, and a
  second run changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.conversations.models import Message
from assistant.core.clock import Clock
from assistant.db.models import KEY_NORMALIZER_VERSION, SystemState
from assistant.db.uow import transaction
from assistant.memory.search import NORMALIZER_VERSION, guess_lang, normalize

BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class KeyedRow:
    """One row as read: id, the hash of its content, and the keys to write."""

    message_id: int
    content_hash: str | None
    search_key: str
    lang_guess: str | None


@dataclass(frozen=True, slots=True)
class ReindexReport:
    scanned: int
    updated: int
    #: Rows edited between read and write: left as the edit wrote them.
    skipped_concurrent: int
    previous_version: int | None
    version: int
    #: Nothing to do: the stored version is already current.
    up_to_date: bool = False


async def stored_normalizer_version(session: AsyncSession) -> int | None:
    """The normalizer version the stored keys were built with, or None."""
    row = await session.get(SystemState, KEY_NORMALIZER_VERSION)
    if row is None:
        return None
    value: Any = (row.value or {}).get("version")
    return int(value) if isinstance(value, int) else None


async def read_batch(session: AsyncSession, *, after_id: int, size: int) -> list[KeyedRow]:
    """The next retained rows with text, keyed with the current normalizer."""
    rows = (
        await session.execute(
            select(Message.id, Message.text, Message.content_hash)
            .where(Message.id > after_id)
            .where(Message.text.is_not(None))
            .where(Message.deleted_at.is_(None))
            .where(Message.purged_at.is_(None))
            .order_by(Message.id)
            .limit(size)
        )
    ).all()
    return [
        KeyedRow(
            message_id=int(row[0]),
            content_hash=row[2],
            search_key=normalize(str(row[1])),
            lang_guess=guess_lang(str(row[1])),
        )
        for row in rows
    ]


async def apply_batch(session: AsyncSession, batch: list[KeyedRow]) -> tuple[int, int]:
    """Write the keys. Returns `(updated, skipped_because_edited)`."""
    updated = skipped = 0
    for row in batch:
        result = await session.execute(
            update(Message)
            .where(Message.id == row.message_id)
            .where(Message.content_hash.is_not_distinct_from(row.content_hash))
            .where(Message.text.is_not(None))
            .where(
                Message.search_key.is_distinct_from(row.search_key)
                | Message.lang_guess.is_distinct_from(row.lang_guess)
            )
            .values(search_key=row.search_key, lang_guess=row.lang_guess)
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0) or 0):
            updated += 1
            continue
        current = (
            await session.execute(select(Message.content_hash).where(Message.id == row.message_id))
        ).first()
        if current is not None and current[0] != row.content_hash:
            skipped += 1
    return updated, skipped


async def record_version(session: AsyncSession, clock: Clock, version: int) -> None:
    statement = insert(SystemState).values(
        key=KEY_NORMALIZER_VERSION, value={"version": version}, updated_at=clock.now()
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[SystemState.key],
            set_={"value": statement.excluded.value, "updated_at": statement.excluded.updated_at},
        )
    )


async def reindex(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    force: bool = False,
    batch_size: int = BATCH_SIZE,
) -> ReindexReport:
    """Re-key every retained message if the stored version is older (or `force`)."""
    async with transaction(session_factory) as uow:
        previous = await stored_normalizer_version(uow.session)
    if not force and previous is not None and previous >= NORMALIZER_VERSION:
        return ReindexReport(0, 0, 0, previous, previous, up_to_date=True)

    scanned = updated = skipped = 0
    after_id = 0
    while True:
        async with transaction(session_factory) as uow:
            batch = await read_batch(uow.session, after_id=after_id, size=batch_size)
        if not batch:
            break
        async with transaction(session_factory) as uow:
            done, edited = await apply_batch(uow.session, batch)
        scanned += len(batch)
        updated += done
        skipped += edited
        after_id = batch[-1].message_id

    async with transaction(session_factory) as uow:
        await record_version(uow.session, clock, NORMALIZER_VERSION)
    return ReindexReport(scanned, updated, skipped, previous, NORMALIZER_VERSION)
