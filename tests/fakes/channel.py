"""A `ControlChannel` that records what would have been sent to the owner."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from assistant.core.channel import Button, Delivery


@dataclass
class FakeChannel:
    sends: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    #: The account each send was addressed to, parallel to `sends`.
    recipients: list[int] = field(default_factory=list)
    #: The buttons each send carried, parallel to `sends`.
    buttons: list[tuple[Button, ...]] = field(default_factory=list)
    deliver: bool = True
    _next_id: int = 500

    async def send(
        self, *, account_id: int, chat_id: int, text: str, buttons: Sequence[Button] = ()
    ) -> Delivery:
        self.sends.append(text)
        self.recipients.append(account_id)
        self.buttons.append(tuple(buttons))
        self.events.append("send")
        self._next_id += 1
        if not self.deliver:
            return Delivery(ok=False, error="forbidden")
        return Delivery(ok=True, message_id=self._next_id)

    async def typing(self, *, account_id: int, chat_id: int) -> None:
        self.events.append("typing")
