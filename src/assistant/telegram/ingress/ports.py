"""Ports for ingress (ARCHITECTURE §5.2).

The poller depends on these Protocols, not on aiogram, so it can be tested
against a fake that scripts batches, errors and 409s without a network.

Updates cross this boundary as plain dicts: they are stored as raw JSON
anyway, and keeping aiogram's models out of the poller means a Bot API upgrade
cannot change ingestion semantics silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class BotIdentity:
    """The answer to `getMe`, reduced to what ingestion needs."""

    id: int
    username: str | None
    can_connect_to_business: bool


class UpdateSource(Protocol):
    """The read-only side of the Bot API.

    Per AGENTS §5, `telegram/ingress/` may call ONLY these three methods.
    Anything with a visible effect belongs to the gateway.
    """

    async def get_me(self) -> BotIdentity: ...

    async def delete_webhook(self) -> None:
        """Always with `drop_pending_updates=False` — never True."""
        ...

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int,
        limit: int,
        allowed_updates: list[str],
    ) -> list[dict[str, Any]]: ...


class TelegramConflictError(RuntimeError):
    """HTTP 409: another consumer is polling this token.

    Possible causes: the probe is still running, a second deployment, or a
    restore test. The poller must stop and alert, never crash-loop — two
    pollers make no progress at all.
    """


class TelegramTransientError(RuntimeError):
    """A network or 5xx failure. Back off and retry; nothing is lost."""
