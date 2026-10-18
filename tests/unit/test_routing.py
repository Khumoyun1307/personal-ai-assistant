"""The routing table (ARCHITECTURE §6.3).

Named regression suite for §6 invariant 5 — the hard boundary between control
chat and business traffic. Never weaken these.
"""

from __future__ import annotations

import pytest

from assistant.telegram.routing import Route, route_update, update_kind
from tests.fakes.telegram import BOT_ID, make_update

OWNER = 584113992
STRANGER = 999000111
OWNERS = frozenset({OWNER})


def route(update: dict[str, object]) -> Route:
    return route_update(update, owner_ids=OWNERS, bot_id=BOT_ID).route


def test_owner_private_message_reaches_the_control_chat() -> None:
    assert route(make_update(1, chat_id=OWNER)) is Route.CONTROL


def test_non_owner_private_message_is_refused() -> None:
    assert route(make_update(2, chat_id=STRANGER)) is Route.REFUSED


def test_group_messages_are_ignored() -> None:
    update = make_update(3, chat_id=-1001234, chat_type="supergroup", from_id=OWNER)
    assert route(update) is Route.IGNORED


def test_messages_from_bots_are_ignored() -> None:
    assert route(make_update(4, chat_id=OWNER, is_bot=True)) is Route.IGNORED


@pytest.mark.parametrize(
    "kind", ["business_message", "edited_business_message", "deleted_business_messages"]
)
def test_business_updates_never_reach_the_control_router(kind: str) -> None:
    """§6 invariant 5, the one that matters most here.

    Note `from_id=OWNER`: a business message *from the owner* is something they
    typed to a contact, not an instruction to the assistant. Routing it to the
    control chat would execute a message meant for a friend as a command.
    """
    update = make_update(5, kind=kind, chat_id=STRANGER, from_id=OWNER)
    assert route(update) is Route.BUSINESS


def test_business_message_in_the_bots_own_chat_is_ignored() -> None:
    """The mirror of the owner's chat with this bot — the loop guard."""
    update = make_update(6, kind="business_message", chat_id=BOT_ID, from_id=OWNER)
    assert route(update) is Route.IGNORED


def test_business_message_from_another_bot_is_ignored() -> None:
    update = make_update(7, kind="business_message", chat_id=STRANGER, is_bot=True)
    assert route(update) is Route.IGNORED


def test_business_connection_goes_to_the_registry() -> None:
    assert route(make_update(8, kind="business_connection")) is Route.CONNECTION


def test_owner_callback_is_control_and_stranger_callback_is_refused() -> None:
    assert route(make_update(9, kind="callback_query", from_id=OWNER)) is Route.CONTROL
    assert route(make_update(10, kind="callback_query", from_id=STRANGER)) is Route.REFUSED


def test_unknown_update_kinds_are_ignored_not_crashed_on() -> None:
    """Telegram adds update kinds; an unknown one must not break ingestion."""
    assert route({"update_id": 11, "some_future_update": {"a": 1}}) is Route.IGNORED


def test_update_with_no_payload_key_is_ignored() -> None:
    assert route({"update_id": 12}) is Route.IGNORED
    assert update_kind({"update_id": 12}) == "unknown"


@pytest.mark.parametrize("route_value", [Route.REFUSED, Route.IGNORED])
def test_refused_and_ignored_clear_their_payload(route_value: Route) -> None:
    """We have no basis to keep content from a non-owner (§6 invariant 12)."""
    update = (
        make_update(13, chat_id=STRANGER)
        if route_value is Route.REFUSED
        else make_update(14, chat_id=-100123, chat_type="group")
    )
    decision = route_update(update, owner_ids=OWNERS, bot_id=BOT_ID)
    assert decision.route is route_value
    assert decision.clears_payload is True


@pytest.mark.parametrize("route_value", [Route.CONTROL, Route.BUSINESS, Route.CONNECTION])
def test_kept_routes_do_not_clear_their_payload(route_value: Route) -> None:
    updates = {
        Route.CONTROL: make_update(15, chat_id=OWNER),
        Route.BUSINESS: make_update(16, kind="business_message", chat_id=STRANGER),
        Route.CONNECTION: make_update(17, kind="business_connection"),
    }
    decision = route_update(updates[route_value], owner_ids=OWNERS, bot_id=BOT_ID)
    assert decision.clears_payload is False


def test_an_empty_allowlist_refuses_everyone() -> None:
    """Fail closed: a misconfigured allowlist must not open the control chat."""
    decision = route_update(make_update(18, chat_id=OWNER), owner_ids=frozenset(), bot_id=BOT_ID)
    assert decision.route is Route.REFUSED
