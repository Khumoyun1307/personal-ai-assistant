"""Business connection registry adapter (ARCHITECTURE §6.4, §5.2).

The only module that may call `getBusinessConnection` (AGENTS §5), and the
inbox glue between raw Telegram payloads and `accounts.connections`:

- `parse_connection` turns a stored `BusinessConnection` payload into a
  domain snapshot. Missing `rights` means no rights — never a default
  (§6 invariant 7).
- `ConnectionRegistry.prepare` is the inbox *preparer* for business traffic:
  an unknown connection id is looked up **before** the processing transaction
  opens (§6.2), because the initial connection update can be missing (TF §2).
- `ConnectionRegistry.connection` is the inbox handler for connection
  updates; `.gate_business` is the first step of the business pipeline
  (`telegram/business.py`): foreign → rejected, not yet live → ignored,
  payload cleared either way; live traffic is handed on to be stored.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import connections
from assistant.accounts.connections import (
    ApplyResult,
    ConnectionSnapshot,
    Resolution,
    ResolutionKind,
    Source,
)
from assistant.accounts.models import Account
from assistant.accounts.service import MANAGED_MODE_STOPPED, managed_mode
from assistant.core.clock import Clock
from assistant.core.errors import TransientError
from assistant.core.jobs import CONNECTION_NOTIFY_TASK, JobScheduler
from assistant.core.routing import Disposition, RouteDecision
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate
from assistant.observability.logging import get_logger
from assistant.telegram.errors import ErrorClass, classify_exception

logger = get_logger(__name__)


class ConnectionPayloadError(ValueError):
    """A connection payload without the fields the registry depends on."""


def payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _timestamp(value: Any) -> datetime:
    if isinstance(value, bool):
        raise ConnectionPayloadError("date")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise ConnectionPayloadError("date")


def parse_connection(payload: dict[str, Any]) -> ConnectionSnapshot:
    """A stored `BusinessConnection` (aiogram JSON dump) → domain snapshot.

    The top-level legacy `can_reply` flag is deliberately ignored: only the
    `rights` object grants anything, and its absence grants nothing.
    """
    connection_id = payload.get("id")
    user = payload.get("user")
    user_id = user.get("id") if isinstance(user, dict) else None
    user_chat_id = payload.get("user_chat_id")
    is_enabled = payload.get("is_enabled")
    if not isinstance(connection_id, str) or not connection_id:
        raise ConnectionPayloadError("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise ConnectionPayloadError("user.id")
    if not isinstance(user_chat_id, int) or isinstance(user_chat_id, bool):
        raise ConnectionPayloadError("user_chat_id")
    if not isinstance(is_enabled, bool):
        raise ConnectionPayloadError("is_enabled")
    rights_obj = payload.get("rights")
    rights = (
        frozenset(name for name, granted in rights_obj.items() if granted is True)
        if isinstance(rights_obj, dict)
        else frozenset()
    )
    return ConnectionSnapshot(
        connection_id=connection_id,
        user_id=user_id,
        user_chat_id=user_chat_id,
        date=_timestamp(payload.get("date")),
        is_enabled=is_enabled,
        rights=rights,
        payload_digest=payload_digest(payload),
    )


def business_connection_id(update: dict[str, Any], kind: str) -> str | None:
    body = update.get(kind)
    if not isinstance(body, dict):
        return None
    value = body.get("business_connection_id")
    return value if isinstance(value, str) and value else None


class ConnectionLookup(Protocol):
    async def get(self, connection_id: str) -> dict[str, Any] | None:
        """The connection's payload, or None if Telegram does not know the id."""
        ...


class AiogramConnectionLookup:
    """`getBusinessConnection` — the one Bot API call this module may make."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def get(self, connection_id: str) -> dict[str, Any] | None:
        try:
            connection = await self._bot.get_business_connection(
                business_connection_id=connection_id
            )
        except TelegramBadRequest as error:
            classified = classify_exception(error)
            if classified.error_class in (ErrorClass.CONNECTION_INVALID, ErrorClass.BAD_REQUEST):
                return None
            raise
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as error:
            raise TransientError(type(error).__name__) from error
        return connection.model_dump(mode="json", exclude_none=True)


class ConnectionRegistry:
    """Inbox preparer and handlers for connection and business updates."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        lookup: ConnectionLookup,
        scheduler: JobScheduler,
        *,
        owner_ids: frozenset[int],
        connection_id_of: Callable[[dict[str, Any], str], str | None] = business_connection_id,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._lookup = lookup
        self._scheduler = scheduler
        self._owner_ids = owner_ids
        self._connection_id_of = connection_id_of

    # ---------------------------------------------------------------- preparer
    async def prepare(self, update: dict[str, Any], decision: RouteDecision) -> None:
        """Bootstrap an unknown connection id before the update is processed."""
        connection_id = self._connection_id_of(update, decision.kind)
        if connection_id is None:
            return
        async with transaction(self._session_factory) as uow:
            resolution = await connections.resolve(uow.session, connection_id, self._clock)
        if resolution.kind is not ResolutionKind.UNKNOWN:
            return

        payload = await self._lookup.get(connection_id)
        if payload is None:
            logger.warning("connection_bootstrap_unknown_id", detail="Telegram does not know it")
            return
        snapshot = parse_connection(payload)
        async with transaction(self._session_factory) as uow:
            result = await connections.apply_snapshot(
                uow.session,
                snapshot,
                owner_ids=self._owner_ids,
                source=Source.BOOTSTRAP,
                clock=self._clock,
            )
            await self._after_apply(uow.session, result)
        logger.info("connection_bootstrapped", change=result.change.value)

    # ---------------------------------------------------------------- handlers
    async def connection(
        self, session: AsyncSession, row: InboundUpdate, decision: RouteDecision
    ) -> Disposition | None:
        payload = (row.payload or {}).get(decision.kind)
        if not isinstance(payload, dict):
            raise ConnectionPayloadError("business_connection")
        snapshot = parse_connection(payload)
        result = await connections.apply_snapshot(
            session, snapshot, owner_ids=self._owner_ids, source=Source.UPDATE, clock=self._clock
        )
        logger.info("connection_update", update_row_id=row.id, change=result.change.value)
        if result.account_id is None:
            # Not an allowlisted owner: no account, and no content kept.
            return Disposition.REJECTED
        row.account_id = result.account_id
        await self._after_apply(session, result)
        return None

    async def gate_business(
        self, session: AsyncSession, row: InboundUpdate, decision: RouteDecision
    ) -> Disposition | Resolution:
        """Whose traffic is this, and may it be stored? (§6.4, §6 invariant 7)

        Returns a `Disposition` when the update must end here with its
        payload cleared — foreign → rejected; unknown, not yet onboarded or
        disabled → ignored — or the owner's live `Resolution`, for the
        business pipeline (`telegram/business.py`) to store.
        """
        connection_id = self._connection_id_of(row.payload or {}, decision.kind)
        if connection_id is None:
            return Disposition.IGNORED
        resolution = await connections.resolve(session, connection_id, self._clock)
        if resolution.kind is ResolutionKind.FOREIGN:
            return Disposition.REJECTED
        if resolution.kind is ResolutionKind.UNKNOWN:
            # Telegram could not tell us whose it is: fail closed.
            return Disposition.IGNORED
        account = await session.get(Account, resolution.account_id)
        if account is None or account.telegram_user_id not in self._owner_ids:
            # An owner removed from the allowlist keeps their account and
            # connection rows, but their traffic is no longer ours to store
            # (§6 invariant 12): treated exactly like a foreign connection.
            logger.warning("business_update_not_allowlisted", update_row_id=row.id)
            return Disposition.REJECTED
        row.account_id = resolution.account_id
        if not resolution.is_live:
            logger.info(
                "business_update_inert",
                update_row_id=row.id,
                connection_status=None if resolution.status is None else resolution.status.value,
            )
            return Disposition.IGNORED
        if managed_mode(account) == MANAGED_MODE_STOPPED:
            # The owner chose "stop observing" (FR-ONB-06): nothing further is
            # stored, and the payload goes with the row.
            logger.info("business_update_stopped", update_row_id=row.id)
            return Disposition.IGNORED
        return resolution

    async def _after_apply(self, session: AsyncSession, result: ApplyResult) -> None:
        if not result.notify or result.event_id is None:
            return
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        await self._scheduler.defer(
            CONNECTION_NOTIFY_TASK,
            args={"event_id": result.event_id},
            connection=raw.driver_connection,
        )


__all__ = [
    "AiogramConnectionLookup",
    "ConnectionLookup",
    "ConnectionPayloadError",
    "ConnectionRegistry",
    "business_connection_id",
    "parse_connection",
    "payload_digest",
]
