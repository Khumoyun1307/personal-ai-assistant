"""Contract tests on sanitised real payloads (TF §12 LV-01/05/12, S02.1).

`tests/fixtures/telegram/*.sanitized.json` is produced by the owner- or
agent-run sanitiser from probe dumps (AGENTS §9): real structure, synthetic
content.
These tests pin that our parsing and routing hold for what Telegram actually
sent, not only for payloads we wrote by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from assistant.accounts.connections import Tier, capability_tier
from assistant.core.routing import Route
from assistant.telegram.connections import parse_connection
from assistant.telegram.routing import BUSINESS_KINDS, route_update

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "telegram"
    / "updates-20261018.sanitized.json"
)


def records() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def connection_updates() -> list[dict[str, Any]]:
    return [
        record["payload"]["business_connection"]
        for record in records()
        if record.get("update_type") == "business_connection"
    ]


def lookups() -> list[dict[str, Any]]:
    return [
        record["result"]
        for record in records()
        if record.get("label") == "getBusinessConnection" and isinstance(record.get("result"), dict)
    ]


def bot_id() -> int:
    me = next(record["result"] for record in records() if record.get("label") == "getMe")
    return int(me["id"])


def test_the_fixture_covers_the_connection_lifecycle() -> None:
    updates = connection_updates()
    assert len(updates) >= 10
    assert any(not update["is_enabled"] for update in updates)
    assert len({update["id"] for update in updates}) >= 2  # a reconnect rotated the id
    assert lookups()


@pytest.mark.parametrize("source", ["updates", "lookups"])
def test_every_observed_connection_payload_parses(source: str) -> None:
    payloads = connection_updates() if source == "updates" else lookups()
    for payload in payloads:
        snapshot = parse_connection(payload)
        assert snapshot.connection_id == payload["id"]
        assert snapshot.user_id == payload["user"]["id"]


def test_a_disconnect_carries_no_rights_and_the_legacy_flag_grants_nothing() -> None:
    disabled = [update for update in connection_updates() if not update["is_enabled"]]
    assert disabled
    for payload in disabled:
        assert "rights" not in payload  # LV-01: rights absent on disconnect
        snapshot = parse_connection(payload)
        assert snapshot.effective_rights == frozenset()
        assert capability_tier(snapshot.is_enabled, snapshot.rights) is Tier.NONE


def test_null_rights_in_a_lookup_grant_nothing() -> None:
    """`getBusinessConnection` returns every right, unset ones as null."""
    for payload in lookups():
        rights = payload.get("rights") or {}
        granted = parse_connection(payload).rights
        assert granted == {name for name, value in rights.items() if value is True}


def test_observed_tiers_match_the_rights_granted() -> None:
    tiers = {
        capability_tier(snapshot.is_enabled, snapshot.rights)
        for snapshot in map(parse_connection, connection_updates())
    }
    # LV-12: all rights off still connected (observe only); full rights; disconnect.
    assert {Tier.OBSERVE_ONLY, Tier.EXTENDED, Tier.NONE} <= tiers


def test_no_business_update_ever_routes_to_the_control_chat() -> None:
    """§6 invariant 5 on real payloads, including the owner's own messages."""
    owners = frozenset(update["user"]["id"] for update in connection_updates())
    business = [
        record["payload"] for record in records() if record.get("update_type") in BUSINESS_KINDS
    ]
    assert business
    for update in business:
        decision = route_update(update, owner_ids=owners, bot_id=bot_id())
        assert decision.route is Route.BUSINESS


def test_connection_updates_route_to_the_registry() -> None:
    for record in records():
        if record.get("update_type") == "business_connection":
            decision = route_update(record["payload"], owner_ids=frozenset(), bot_id=bot_id())
            assert decision.route is Route.CONNECTION


# ------------------------------------------------------- S02.2: normalization
def _business_records() -> list[tuple[str, dict[str, Any]]]:
    return [
        (record["update_type"], record["payload"])
        for record in records()
        if record.get("update_type") in BUSINESS_KINDS
    ]


def test_every_observed_business_update_normalizes() -> None:
    """Nothing Telegram actually sent in the session is malformed to us."""
    from assistant.conversations.events import ObservedDeletion, ObservedMessage
    from assistant.telegram.normalize import normalize

    owner = connection_updates()[0]["user"]["id"]
    seen: list[type[object]] = []
    for kind, update in _business_records():
        event = normalize(update, kind, owner_user_id=owner, bot_id=bot_id())
        assert isinstance(event, ObservedMessage | ObservedDeletion)
        seen.append(type(event))
    assert ObservedMessage in seen
    assert ObservedDeletion in seen


def test_observed_messages_classify_as_contact_or_owner_manual() -> None:
    """The session had a contact (the second account) and the owner typing
    from two clients (LV-02) — nothing automated, nothing bot-sent (LV-03)."""
    from assistant.conversations.events import Direction, ObservedMessage
    from assistant.telegram.normalize import normalize

    owner = connection_updates()[0]["user"]["id"]
    directions = []
    for kind, update in _business_records():
        if kind == "deleted_business_messages":
            continue
        event = normalize(update, kind, owner_user_id=owner, bot_id=bot_id())
        assert isinstance(event, ObservedMessage)
        directions.append(event.direction)
    assert set(directions) == {Direction.CONTACT, Direction.OWNER_MANUAL}
    assert directions.count(Direction.OWNER_MANUAL) >= 2
    assert directions.count(Direction.CONTACT) > directions.count(Direction.OWNER_MANUAL)


def test_observed_media_keeps_structure_and_drops_file_ids() -> None:
    """LV-11: photo, voice, document and video note — kinds and sizes stay,
    `file_id` (a download capability) and file names never do."""
    from assistant.conversations.events import ObservedMessage
    from assistant.telegram.normalize import normalize

    owner = connection_updates()[0]["user"]["id"]
    kinds: set[str] = set()
    for kind, update in _business_records():
        if kind == "deleted_business_messages":
            continue
        event = normalize(update, kind, owner_user_id=owner, bot_id=bot_id())
        assert isinstance(event, ObservedMessage)
        if event.content.media is not None:
            kinds.add(event.content.media.kind)
            dumped = event.content.media.as_json()
            assert "file_id" not in dumped
            assert "file_name" not in dumped
            assert dumped.get("file_unique_id")
    assert {"photo", "voice", "document", "video_note"} <= kinds
