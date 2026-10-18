"""Update routing — the hard boundary (ARCHITECTURE §6.3, §6 invariant 5).

Pure classification: raw update in, routing decision out. No I/O, so the whole
table is table-testable.

The invariant this enforces: **business updates never reach control-chat
handlers**, even when `from.id` is the owner. They are a different namespace
with a different meaning — a `business_message` from the owner is something
they typed to a *contact*, not an instruction to the assistant. Treating one as
the other would let a message meant for a friend be executed as a command.
"""

from __future__ import annotations

from typing import Any

from assistant.core.routing import CLEAR_PAYLOAD, Route, RouteDecision

__all__ = [
    "BUSINESS_KINDS",
    "CLEAR_PAYLOAD",
    "Route",
    "RouteDecision",
    "route_update",
    "update_kind",
]

#: Update kinds that carry business traffic.
BUSINESS_KINDS: frozenset[str] = frozenset(
    {"business_message", "edited_business_message", "deleted_business_messages"}
)


def update_kind(update: dict[str, Any]) -> str:
    """The update's single payload key (`message`, `business_message`, …)."""
    for key in update:
        if key != "update_id":
            return key
    return "unknown"


def _chat(payload: dict[str, Any]) -> dict[str, Any]:
    chat = payload.get("chat")
    return chat if isinstance(chat, dict) else {}


def _sender(payload: dict[str, Any]) -> dict[str, Any]:
    sender = payload.get("from") or payload.get("from_user")
    return sender if isinstance(sender, dict) else {}


def route_update(
    update: dict[str, Any], *, owner_ids: frozenset[int], bot_id: int
) -> RouteDecision:
    """Decide where an update goes. Pure; no I/O.

    `owner_ids` is the allowlist. Anyone not in it gets a refusal and leaves no
    trace beyond metadata (§6 invariant 12).
    """
    kind = update_kind(update)
    payload = update.get(kind)
    payload = payload if isinstance(payload, dict) else {}

    # --- business namespace, checked FIRST ---------------------------------
    # Deliberately before any owner check: a business update from the owner is
    # still business traffic (§6 invariant 5).
    if kind in BUSINESS_KINDS:
        chat = _chat(payload)
        chat_id = chat.get("id")
        sender = _sender(payload)
        if chat_id == bot_id or sender.get("is_bot") is True:
            # Mirror of the owner's own chat with a bot, or a bot counterpart:
            # ignoring these is the loop guard (TF §3.2).
            return RouteDecision(Route.IGNORED, kind, "bot chat mirror", chat_id=chat_id)
        return RouteDecision(
            Route.BUSINESS,
            kind,
            "business traffic",
            chat_id=chat_id,
            from_id=sender.get("id"),
        )

    if kind == "business_connection":
        return RouteDecision(Route.CONNECTION, kind, "connection lifecycle")

    # --- bot's own chats ----------------------------------------------------
    if kind in {"message", "edited_message"}:
        chat = _chat(payload)
        chat_type = chat.get("type")
        chat_id = chat.get("id")
        sender = _sender(payload)
        sender_id = sender.get("id")

        if chat_type != "private":
            # The bot has group joins disabled; anything here is out of scope.
            return RouteDecision(Route.IGNORED, kind, "non-private chat", chat_id=chat_id)
        if sender.get("is_bot") is True:
            return RouteDecision(Route.IGNORED, kind, "bot sender", chat_id=chat_id)
        if sender_id is None or sender_id not in owner_ids:
            return RouteDecision(
                Route.REFUSED,
                kind,
                "sender not allowlisted",
                chat_id=chat_id,
                from_id=sender_id,
            )
        return RouteDecision(
            Route.CONTROL, kind, "owner control chat", chat_id=chat_id, from_id=sender_id
        )

    if kind == "callback_query":
        sender = _sender(payload)
        sender_id = sender.get("id")
        if sender_id is None or sender_id not in owner_ids:
            return RouteDecision(Route.REFUSED, kind, "callback from non-owner", from_id=sender_id)
        return RouteDecision(Route.CONTROL, kind, "owner callback", from_id=sender_id)

    if kind == "my_chat_member":
        sender = _sender(payload)
        sender_id = sender.get("id")
        if sender_id is not None and sender_id in owner_ids:
            return RouteDecision(
                Route.CONTROL, kind, "owner blocked/unblocked the bot", from_id=sender_id
            )
        return RouteDecision(Route.IGNORED, kind, "membership change, not the owner")

    return RouteDecision(Route.IGNORED, kind, "unhandled update kind")
