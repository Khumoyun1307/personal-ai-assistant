"""Connection payload parsing and capability rules (ARCHITECTURE §6.4, FR-ONB-04).

The fail-closed rules are the point: a missing `rights` object grants nothing,
a disabled connection has no permissions whatever `rights` says, and the
legacy top-level `can_reply` flag is never read (§6 invariant 7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from assistant.accounts.connections import (
    Change,
    Tier,
    capability_tier,
    unused_rights,
)
from assistant.inbox.connection_alerts import compose
from assistant.telegram.connections import (
    ConnectionPayloadError,
    business_connection_id,
    parse_connection,
)


def payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "synthetic-conn",
        "user": {"id": 7000000001, "is_bot": False, "first_name": "Synthetic"},
        "user_chat_id": 7000000001,
        "date": 1792350000,
        "is_enabled": True,
        "rights": {"can_reply": True, "can_read_messages": True},
    }
    base.update(overrides)
    return base


def test_a_connection_payload_becomes_a_snapshot() -> None:
    snapshot = parse_connection(payload())

    assert snapshot.connection_id == "synthetic-conn"
    assert snapshot.user_id == snapshot.user_chat_id == 7000000001
    assert snapshot.date == datetime.fromtimestamp(1792350000, tz=UTC)
    assert snapshot.rights == {"can_reply", "can_read_messages"}
    assert len(snapshot.payload_digest) == 64


def test_absent_rights_grant_nothing_and_the_legacy_flag_is_ignored() -> None:
    body = payload(can_reply=True)
    del body["rights"]

    assert parse_connection(body).rights == frozenset()


def test_a_right_set_to_false_is_not_granted() -> None:
    assert parse_connection(payload(rights={"can_reply": False})).rights == frozenset()


def test_disabled_means_no_effective_rights() -> None:
    assert parse_connection(payload(is_enabled=False)).effective_rights == frozenset()


@pytest.mark.parametrize(
    "broken",
    [
        {"id": ""},
        {"user": None},
        {"user_chat_id": "7000000001"},
        {"is_enabled": "yes"},
        {"date": None},
    ],
)
def test_a_payload_missing_what_the_registry_needs_is_rejected(broken: dict[str, Any]) -> None:
    with pytest.raises(ConnectionPayloadError):
        parse_connection(payload(**broken))


def test_the_digest_does_not_depend_on_key_order() -> None:
    forward = payload()
    backward = dict(reversed(list(forward.items())))

    assert parse_connection(forward).payload_digest == parse_connection(backward).payload_digest


@pytest.mark.parametrize(
    ("enabled", "rights", "tier"),
    [
        (False, {"can_reply", "can_read_messages"}, Tier.NONE),
        (True, set(), Tier.OBSERVE_ONLY),
        (True, {"can_read_messages"}, Tier.OBSERVE_ONLY),
        (True, {"can_reply"}, Tier.CAN_REPLY),
        (True, {"can_reply", "can_edit_bio"}, Tier.CAN_REPLY),
        (True, {"can_reply", "can_read_messages"}, Tier.EXTENDED),
        (True, {"can_reply", "can_delete_sent_messages"}, Tier.EXTENDED),
        # Undocumented in Bot API 10.3 (aiogram-only field): grants nothing.
        (True, {"can_reply", "can_delete_outgoing_messages"}, Tier.CAN_REPLY),
        (True, {"can_reply", "can_delete_all_messages"}, Tier.EXTENDED),
    ],
)
def test_capability_tier(enabled: bool, rights: set[str], tier: Tier) -> None:
    assert capability_tier(enabled, frozenset(rights)) is tier


def test_unused_rights_are_profile_gift_star_story_and_delete_all() -> None:
    granted = frozenset(
        {
            "can_reply",
            "can_read_messages",
            "can_delete_sent_messages",
            "can_delete_all_messages",
            "can_edit_name",
            "can_change_gift_settings",
            "can_view_gifts_and_stars",
            "can_transfer_stars",
            "can_manage_stories",
        }
    )
    assert unused_rights(granted) == [
        "can_change_gift_settings",
        "can_delete_all_messages",
        "can_edit_name",
        "can_manage_stories",
        "can_transfer_stars",
        "can_view_gifts_and_stars",
    ]


def test_business_connection_id_is_read_from_the_update_body() -> None:
    update = {"update_id": 1, "business_message": {"business_connection_id": "c-1"}}

    assert business_connection_id(update, "business_message") == "c-1"
    assert business_connection_id(update, "edited_business_message") is None
    assert business_connection_id({"business_message": {}}, "business_message") is None


@pytest.mark.parametrize(
    "change", [Change.CONNECTED, Change.ACTIVATED, Change.RECONNECTED, Change.RIGHTS_CHANGED]
)
def test_every_live_notice_names_the_tier(change: Change) -> None:
    assert "Capability:" in compose(change, Tier.OBSERVE_ONLY, [])


def test_there_is_no_notice_for_a_non_event() -> None:
    with pytest.raises(ValueError, match="no notice"):
        compose(Change.UNCHANGED, Tier.CAN_REPLY, [])
