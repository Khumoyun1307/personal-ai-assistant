"""Business traffic pipeline: gate → normalize → store (ARCHITECTURE §6.5).

The inbox handler for `business_message`, `edited_business_message` and
`deleted_business_messages`, and the dead-letter hook for the same route.
It keeps the S02.1 gate unchanged — foreign connection → rejected, unknown or
not live → ignored, payload cleared — and adds what S02.2 does with live
traffic. No Bot API call is made here (the bootstrap lookup stays in
`telegram/connections.py`, as a preparer).
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import connections
from assistant.accounts.connections import ResolutionKind
from assistant.accounts.models import Account
from assistant.conversations import service
from assistant.conversations.events import (
    IgnoredUpdate,
    ObservedDeletion,
)
from assistant.core.clock import Clock
from assistant.core.routing import Disposition, RouteDecision
from assistant.inbox.models import InboundUpdate
from assistant.observability.logging import get_logger
from assistant.telegram.connections import ConnectionRegistry, business_connection_id
from assistant.telegram.normalize import normalize
from assistant.telegram.payload import chat_of, optional_timestamp

logger = get_logger(__name__)

MESSAGE_KINDS = ("business_message", "edited_business_message")


class BusinessPipeline:
    """Handles live business updates for an onboarded owner."""

    def __init__(self, registry: ConnectionRegistry, clock: Clock, *, bot_id: int) -> None:
        self._registry = registry
        self._clock = clock
        self._bot_id = bot_id

    async def handle(
        self, session: AsyncSession, row: InboundUpdate, decision: RouteDecision
    ) -> Disposition | None:
        verdict = await self._registry.gate_business(session, row, decision)
        if isinstance(verdict, Disposition):
            return verdict
        account_id = verdict.account_id
        if account_id is None:  # pragma: no cover - is_live implies an account
            return Disposition.IGNORED
        owner_user_id = await _owner_user_id(session, account_id)
        event = normalize(
            row.payload or {}, decision.kind, owner_user_id=owner_user_id, bot_id=self._bot_id
        )
        if isinstance(event, IgnoredUpdate):
            logger.info("business_update_ignored", update_row_id=row.id, reason=event.reason.value)
            return Disposition.IGNORED
        if isinstance(event, ObservedDeletion):
            outcome = await service.record_deletion(
                session, account_id=account_id, event=event, clock=self._clock
            )
            await _clear_unlinked_payloads(
                session, account_id, event.business_chat_id, event.telegram_message_ids
            )
            if outcome.conversation_id is None:
                return Disposition.IGNORED
            await service.resolve_ingest_failure(
                session, account_id=account_id, inbound_update_id=row.id, clock=self._clock
            )
            return None
        result = await service.record_message(
            session,
            account_id=account_id,
            event=event,
            source_update_id=row.id,
            clock=self._clock,
        )
        row.message_id = result.message_id
        if result.metadata_only:
            # Ignore mode: the row is processed, but the content is not ours
            # to keep (§6.5 last paragraph).
            row.payload = None
        await service.resolve_ingest_failure(
            session, account_id=account_id, inbound_update_id=row.id, clock=self._clock
        )
        return None

    async def on_dead_letter(
        self, session: AsyncSession, row: InboundUpdate, decision: RouteDecision
    ) -> None:
        """A business update that will never process: pause autonomy for the
        chat and record a definite gap (§6.2), reading the payload leniently —
        this is the path for payloads the normal path could not parse."""
        payload = row.payload or {}
        connection_id = business_connection_id(payload, decision.kind)
        if connection_id is None:
            return
        resolution = await connections.resolve(session, connection_id, self._clock)
        if resolution.kind is not ResolutionKind.OWNER or resolution.account_id is None:
            return  # not the owner's traffic: no domain rows (§6 invariant 12)
        account_id = resolution.account_id
        row.account_id = account_id
        body = payload.get(decision.kind)
        body = body if isinstance(body, dict) else {}
        chat_id = chat_of(body).get("id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            return
        if decision.kind == "deleted_business_messages":
            ids = body.get("message_ids")
            if isinstance(ids, list):
                event = ObservedDeletion(
                    business_chat_id=chat_id,
                    telegram_message_ids=tuple(i for i in ids if isinstance(i, int)),
                )
                await service.record_deletion(
                    session, account_id=account_id, event=event, clock=self._clock
                )
            return
        try:
            at = optional_timestamp(body.get("date"), "date")
        except ValueError:
            at = None
        await service.record_ingest_failure(
            session,
            account_id=account_id,
            business_chat_id=chat_id,
            telegram_user_id=chat_id,
            inbound_update_id=row.id,
            at=at or row.received_at,
            clock=self._clock,
        )


async def _owner_user_id(session: AsyncSession, account_id: int) -> int:
    account = await session.get(Account, account_id)
    if account is None:  # pragma: no cover - the gate guarantees an account
        raise RuntimeError("account vanished during processing")
    return account.telegram_user_id


async def _clear_unlinked_payloads(
    session: AsyncSession, account_id: int, chat_id: int, telegram_message_ids: tuple[int, ...]
) -> None:
    """Deleted messages whose updates never became `messages` rows (a failed
    row, an Ignore-mode row) still hold the content in `inbound_updates`.
    Matched by jsonb containment — never by casting a value that may not be
    a number, which would put it in a Postgres error line."""
    for telegram_message_id in telegram_message_ids:
        for kind in MESSAGE_KINDS:
            marker = {kind: {"message_id": telegram_message_id, "chat": {"id": chat_id}}}
            await session.execute(
                update(InboundUpdate)
                .where(InboundUpdate.account_id == account_id)
                .where(InboundUpdate.kind == kind)
                .where(InboundUpdate.payload.is_not(None))
                .where(InboundUpdate.payload.contains(marker))
                .values(payload=None)
            )
