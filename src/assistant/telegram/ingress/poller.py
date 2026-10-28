"""The durable poller (ARCHITECTURE §6.1).

The one rule everything else rests on: **the offset advances only after the
batch is committed.** Telegram treats updates as confirmed when `getUpdates` is
next called with a higher offset, so advancing early means a crash loses
messages permanently — there is no history API to recover them from.

This is also why aiogram's `start_polling` is banned (AGENTS §7): it confirms
updates before handlers finish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.actions.policy_loader import KEY_INGESTION_BARRIER
from assistant.conversations.coverage import record_outage
from assistant.core.clock import Clock
from assistant.db.models import KEY_BOT_ID, KEY_LAST_POLL_OK, KEY_POLLER_HEARTBEAT, SystemState
from assistant.observability.logging import get_logger
from assistant.telegram.ingress import fast_lane
from assistant.telegram.ingress.ports import (
    BotIdentity,
    TelegramConflictError,
    UpdateSource,
)
from assistant.telegram.ingress.writer import read_state, write_new_updates, write_state

logger = get_logger(__name__)

#: Explicit on every call: the server remembers the last list it was given, so
#: an omission silently stops delivery of that kind until it is set again.
ALLOWED_UPDATES: list[str] = [
    "message",
    "edited_message",
    "callback_query",
    "my_chat_member",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]

#: After a week of silence Telegram may reuse update_ids, and a stale-high
#: offset would confirm new updates we never saw (TF §3.1). Six days is the
#: safety margin: drop the offset and let Telegram replay from its earliest
#: unconfirmed update, which the unique key de-duplicates.
OFFSET_MAX_AGE = timedelta(days=6)


class BotIdentityMismatchError(RuntimeError):
    """The token does not match the database. Refuse to start.

    A dev token against the production database (or the reverse) would ingest
    one account's traffic into another's tenant.
    """


@dataclass(slots=True)
class PollerState:
    """In-memory polling state. The persisted copy is for observability only."""

    offset: int | None = None
    last_update_at: datetime | None = None
    batches: int = 0
    updates_written: int = 0
    conflicts: int = 0
    _started: bool = field(default=False, repr=False)


class Poller:
    """Long-polls Telegram and writes updates durably."""

    def __init__(
        self,
        source: UpdateSource,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        timeout: int = 50,
        limit: int = 100,
        process_run_id: str | None = None,
        owner_ids: frozenset[int] = frozenset(),
    ) -> None:
        self._source = source
        self._session_factory = session_factory
        self._clock = clock
        self._timeout = timeout
        self._limit = limit
        self.process_run_id = process_run_id
        #: The allowlist, for the kill-switch fast lane (S04.4 §3.7).
        self._owner_ids = owner_ids
        self.state = PollerState()
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._last_poll_ok_before: datetime | None = None
        #: This run's barrier is marked caught up (decisions R1): once per run.
        self._caught_up = False

    @property
    def bot_id(self) -> int | None:
        return self._bot_id

    async def startup(self) -> BotIdentity:
        """Startup checks (§6.1). Raises rather than polling into a mess."""
        identity = await self._source.get_me()
        self._bot_id = identity.id
        self._bot_username = identity.username

        async with self._session_factory() as session, session.begin():
            stored = await read_state(session, KEY_BOT_ID)
            if stored is not None and stored.get("id") != identity.id:
                raise BotIdentityMismatchError(
                    "token/database mismatch: this database belongs to another bot"
                )
            if stored is None:
                await write_state(session, KEY_BOT_ID, {"id": identity.id})

        if not identity.can_connect_to_business:
            # Not fatal: the control chat still works. But the product's whole
            # purpose is off until Secretary Mode is enabled, so say so loudly.
            logger.warning(
                "secretary_mode_disabled",
                bot_id=identity.id,
                detail="can_connect_to_business is false; business features unavailable",
            )

        # Never drop_pending_updates=True: it discards up to 24 h of business
        # messages, permanently (TF §3.1).
        await self._source.delete_webhook()
        # Remember when the previous run last polled successfully, so the first
        # successful poll of this run can record the outage in between (§6.6).
        async with self._session_factory() as session:
            previous = await read_state(session, KEY_LAST_POLL_OK)
        self._last_poll_ok_before = (
            datetime.fromisoformat(previous["at"]) if previous and "at" in previous else None
        )
        # A poller that passed its startup checks is alive. Without this, the
        # watchdog reads the previous run's heartbeat while the first long poll
        # (up to `timeout` seconds) is open, and reports a restart as a stall.
        await self._heartbeat()
        logger.info("poller_started", bot_id=identity.id)
        return identity

    def _effective_offset(self) -> int | None:
        """The offset to send, or None to let Telegram replay from its oldest.

        The first call after start deliberately omits the offset: Telegram then
        returns the earliest unconfirmed update, and the unique key absorbs
        anything we already stored.
        """
        if not self.state._started:
            return None
        if self.state.offset is None:
            return None
        last = self.state.last_update_at
        if last is not None and self._clock.now() - last > OFFSET_MAX_AGE:
            logger.warning("poller_offset_expired", detail="offset dropped after 6 days idle")
            self.state.offset = None
            return None
        return self.state.offset

    async def poll_once(self) -> int:
        """One `getUpdates` → commit → advance offset cycle.

        Returns the number of rows written (duplicates are not counted).
        """
        if self._bot_id is None:
            raise RuntimeError("poll_once called before startup()")

        updates = await self._source.get_updates(
            offset=self._effective_offset(),
            # The first poll after start does not wait (§9.4 ingestion
            # barrier): it learns at once whether Telegram holds a backlog.
            timeout=self._timeout if self.state._started else 0,
            limit=self._limit,
            allowed_updates=ALLOWED_UPDATES,
        )
        self.state._started = True
        self.state.batches += 1
        # Fewer than `limit`: Telegram's queue was drained by this poll.
        drained = len(updates) < self._limit

        if not updates:
            await self._heartbeat()
            if drained and not self._caught_up:
                async with self._session_factory() as session, session.begin():
                    marked = await self._mark_caught_up(session)
                self._caught_up = marked
            await self._mark_poll_ok()
            return 0

        highest = max(update["update_id"] for update in updates if "update_id" in update)

        marked = False
        async with self._session_factory() as session, session.begin():
            stored = await write_new_updates(session, bot_id=self._bot_id, updates=updates)
            written = len(stored)
            # The kill switch's fast lane, in the batch's own transaction: a
            # /pause waiting in Telegram's queue is in force once it is stored.
            # Only updates stored by THIS write: a replayed one was stored
            # (and maybe processed, with a later /resume) before, so it has
            # no new effect; an unprocessed stored /pause still holds every
            # claim through `inbox_drained` until the inbox applies it.
            pausing = fast_lane.pausing_owners(
                [update for update in updates if update.get("update_id") in stored],
                owner_ids=self._owner_ids,
                bot_id=self._bot_id,
                bot_username=self._bot_username,
            )
            if pausing:
                await fast_lane.apply(session, pausing, self._clock.now())
            # Still inside the transaction: the observability copy of the
            # offset must not claim progress the batch did not make.
            await write_state(
                session,
                KEY_POLLER_HEARTBEAT,
                {"at": self._clock.now().isoformat(), "offset": highest + 1},
            )
            if drained and not self._caught_up:
                marked = await self._mark_caught_up(session)

        # ONLY NOW. Everything above this line can fail and be replayed; past
        # it, Telegram will never send these updates again.
        self._caught_up = self._caught_up or marked
        self.state.offset = highest + 1
        self.state.last_update_at = self._clock.now()
        self.state.updates_written += written
        await self._mark_poll_ok()

        logger.info(
            "poller_batch", received=len(updates), written=written, offset=self.state.offset
        )
        return written

    async def _mark_caught_up(self, session: AsyncSession) -> bool:
        """Decisions R1: set `caught_up_at` of THIS run's barrier only — a key
        another run reset is never marked (P2-T36). The caller records it as
        done only after its commit, so a failed commit marks again next poll."""
        if self.process_run_id is None:
            return False
        result = await session.execute(
            update(SystemState)
            .where(
                SystemState.key == KEY_INGESTION_BARRIER,
                SystemState.value["process_run_id"].astext == self.process_run_id,
            )
            .values(
                value={
                    "process_run_id": self.process_run_id,
                    "caught_up_at": self._clock.now().isoformat(),
                }
            )
            .returning(SystemState.key)
        )
        marked = result.first() is not None
        if marked:
            logger.info("ingestion_caught_up", process_run_id=self.process_run_id)
        return marked

    async def _mark_poll_ok(self) -> None:
        """Record a completed poll; on the first one after startup, record the
        outage since the previous run's last completed poll (§6.6)."""
        now = self._clock.now()
        async with self._session_factory() as session, session.begin():
            if self._last_poll_ok_before is not None:
                written = await record_outage(
                    session, last_ok=self._last_poll_ok_before, resumed=now, clock=self._clock
                )
                if written:
                    logger.warning(
                        "poller_outage_recorded",
                        since=self._last_poll_ok_before.isoformat(),
                        gaps=written,
                    )
                self._last_poll_ok_before = None
            await write_state(session, KEY_LAST_POLL_OK, {"at": now.isoformat()})

    async def _heartbeat(self) -> None:
        async with self._session_factory() as session, session.begin():
            await write_state(
                session,
                KEY_POLLER_HEARTBEAT,
                {"at": self._clock.now().isoformat(), "offset": self.state.offset},
            )

    async def run_forever(self, *, stop_after_batches: int | None = None) -> None:
        """Poll until stopped.

        `stop_after_batches` exists so tests can run the real loop rather than
        a reimplementation of it.
        """
        while stop_after_batches is None or self.state.batches < stop_after_batches:
            try:
                await self.poll_once()
            except TelegramConflictError:
                # Two consumers on one token make no progress at all; stop and
                # let the supervisor surface it (§6.1).
                self.state.conflicts += 1
                logger.error(
                    "poller_conflict",
                    detail="another consumer is polling this token; stopping",
                )
                raise
