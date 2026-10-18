"""Ports the agent needs from adapters (ARCHITECTURE §5.2).

The agent may not import `telegram/` (import-linter contract). It reaches the
owner's control chat through `ControlChannel`, which the Telegram adapter
implements over `OutboundGateway` and `bootstrap.py` wires in. There is no
business-chat method here, on purpose: the agent never sends as the owner
(§6 invariant 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Delivery:
    """What happened to a control-chat message. Never assumed to be success."""

    ok: bool
    message_id: int | None = None
    error: str | None = None


class ControlChannel(Protocol):
    async def send(self, *, account_id: int, chat_id: int, text: str) -> Delivery: ...

    async def typing(self, *, account_id: int, chat_id: int) -> None: ...
