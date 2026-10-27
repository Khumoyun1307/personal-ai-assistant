"""Unit tests for S04.5 activity rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from assistant.actions.activity import ActivityEntry, Authorization
from assistant.telegram.control.activity import render_activity

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _entry(
    action_id: int,
    state: str,
    group: str,
    *,
    display_name: str = "Dilnoza R.",
    text: str = "Synthetic reply text",
    created_at: datetime = NOW,
    decided_at: datetime | None = None,
    executed_at: datetime | None = None,
    state_reason: str | None = None,
    auth: Authorization | None = None,
    deleted_in_chat_at: datetime | None = None,
) -> ActivityEntry:
    return ActivityEntry(
        action_id=action_id,
        conversation_id=10,
        contact_id=20,
        contact_display_name=display_name,
        contact_username=None,
        origin="agent_proposal",
        state=state,
        state_reason=state_reason,
        group=group,
        created_at=created_at,
        decided_at=decided_at,
        executed_at=executed_at,
        version=1,
        excerpt=text,
        authorization=auth,
        deleted_in_chat_at=deleted_in_chat_at,
    )


def test_sent_and_draft_lines_look_different() -> None:
    sent = _entry(
        1,
        "succeeded",
        "sent",
        executed_at=datetime(2026, 10, 27, 13, 5, tzinfo=UTC),
        auth=Authorization("owner_approval", datetime(2026, 10, 27, 13, 4, tzinfo=UTC), 2),
    )
    draft = _entry(
        2,
        "awaiting_approval",
        "awaiting",
        created_at=datetime(2026, 10, 27, 13, 10, tzinfo=UTC),
    )

    rendered = render_activity([sent, draft], timezone="UTC", period_label="today")

    assert "✓ Sent 13:05" in rendered
    assert "approved by you at 13:04 (version 2)" in rendered
    assert "📝 Draft — awaiting your approval" in rendered
    # Sent and draft must be clearly distinguished
    lines = rendered.strip().split("\n")
    assert any("✓ Sent" in line for line in lines)
    assert any("Draft" in line for line in lines)


def test_long_lists_are_cut_with_a_count() -> None:
    entries = [
        _entry(
            i,
            "succeeded",
            "sent",
            executed_at=NOW + timedelta(minutes=i),
            text=f"Reply {i}",
        )
        for i in range(1, 36)
    ]

    rendered = render_activity(entries, timezone="UTC", period_label="today")

    assert "…and 5 more — ask me for a narrower period." in rendered


def test_empty_period_says_nothing_happened() -> None:
    rendered = render_activity([], timezone="UTC", period_label="today")
    assert rendered == "Nothing was sent or drafted today."

    rendered_yesterday = render_activity([], timezone="UTC", period_label="yesterday")
    assert rendered_yesterday == "Nothing was sent or drafted yesterday."


def test_a_deleted_sent_message_is_noted_without_saying_who_deleted_it() -> None:
    del_time = datetime(2026, 10, 27, 13, 20, tzinfo=UTC)
    entry = _entry(
        1,
        "succeeded",
        "sent",
        executed_at=datetime(2026, 10, 27, 13, 5, tzinfo=UTC),
        deleted_in_chat_at=del_time,
    )

    rendered = render_activity([entry], timezone="UTC", period_label="today")

    assert "deleted in the chat at 13:20 (Telegram does not say by whom)" in rendered
    # Invariant: Telegram doesn't reveal who deleted, never attribute deletion
    assert "deleted by contact" not in rendered
    assert "deleted by owner" not in rendered
    assert "deleted by you" not in rendered
