"""Routing vocabulary (ARCHITECTURE §6.3, §5.2).

These types live in the shared kernel, not in `telegram/`, because the inbox
processor is a **domain** package and may not import an adapter. The adapter
(`telegram/routing.py`) knows what a Telegram update looks like and produces
one of these; the domain only ever sees the decision.

That split is what keeps the layer contract honest: swapping the transport
would replace the parser, not the routing rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class Route(StrEnum):
    """Where an update goes."""

    CONTROL = "control"
    """An allowlisted owner talking to the assistant in its own chat."""

    BUSINESS = "business"
    """Traffic on the owner's account. Never the control router."""

    CONNECTION = "connection"
    """Business connection lifecycle."""

    REFUSED = "refused"
    """A non-allowlisted user. Fixed refusal; no domain rows; payload cleared."""

    IGNORED = "ignored"
    """Groups, bot chats, anything out of scope. Payload cleared."""


#: Routes whose payload must be cleared in the processing transaction, because
#: we have no lawful basis to keep the content (§6.1, §6 invariant 12).
CLEAR_PAYLOAD: frozenset[Route] = frozenset({Route.REFUSED, Route.IGNORED})


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Where an update goes, and why."""

    route: Route
    kind: str
    reason: str
    chat_id: int | None = None
    from_id: int | None = None

    @property
    def clears_payload(self) -> bool:
        return self.route in CLEAR_PAYLOAD


class Router(Protocol):
    """Classifies a raw update. Implemented by the transport adapter."""

    def __call__(self, update: dict[str, Any]) -> RouteDecision: ...
