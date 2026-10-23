"""The owner's control chat as a port (ARCHITECTURE §5.2, §6.7).

Lives in the shared kernel because two layers need it and may not import the
Telegram adapter: the agent (replies) and the domain alert service
(`inbox/notify.py`). The Telegram adapter implements it over `OutboundGateway`.

There is no business-chat method here on purpose: nothing that holds this port
can speak as the owner (§6 invariant 2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Delivery:
    """What happened to a control-chat message. Never assumed to be success."""

    ok: bool
    message_id: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Button:
    """An inline button on a control-chat card (§6.7). `callback_data` is the
    encoded `<kind>:<token>:<op>` (`accounts.callbacks.encode`)."""

    label: str
    callback_data: str


class ControlChannel(Protocol):
    async def send(
        self, *, account_id: int, chat_id: int, text: str, buttons: Sequence[Button] = ()
    ) -> Delivery: ...

    async def typing(self, *, account_id: int, chat_id: int) -> None: ...
