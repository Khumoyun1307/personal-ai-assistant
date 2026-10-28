"""`build_triage` (S05.1 §3.2): the order of its blocks (the caching rule:
stable first, the current time last), what an empty block says, and how
omitted burst messages are reported. Synthetic text only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from assistant.accounts.models import Account
from assistant.agent import prompt_registry
from assistant.agent.triage_context import build_triage
from assistant.agent.untrusted import Source, wrap
from assistant.inbox.tasks import TaskView
from assistant.llm.port import Provenance
from assistant.memory.items import MemoryView
from assistant.memory.summaries import SourceMessage

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def account() -> Account:
    return Account(telegram_user_id=7000000001, timezone="Asia/Tashkent", locale_pref="uz-Latn")


def message(message_id: int, direction: str = "contact", *, minutes: int = 0) -> SourceMessage:
    return SourceMessage(
        message_id=message_id,
        direction=direction,
        sent_at=NOW - timedelta(minutes=60 - minutes),
        content_type="text",
        text=f"SYNTH {message_id}",
        media_kind=None,
        edited_at=None,
    )


def note() -> MemoryView:
    return MemoryView(
        memory_id=1,
        scope="contact",
        kind="fact",
        content="Synthetic note",
        contact_id=5,
        conversation_id=None,
        applies_to_replies=False,
        tainted=False,
        created_at=NOW,
        source="owner",
    )


def task() -> TaskView:
    return TaskView(
        task_id=9,
        conversation_id=3,
        kind="open_question",
        title="Synthetic title",
        due_at=None,
        status="open",
        source_message_ids=(1,),
        created_at=NOW,
    )


def build(**overrides: object) -> list[str]:
    values: dict[str, object] = {
        "prompt": prompt_registry.active("triage"),
        "account": account(),
        "contact_name": "Kamola",
        "contact_priority": "normal",
        "notes": [note()],
        "summary_overview": "Synthetic overview",
        "open_tasks": [task()],
        "earlier": [message(1), message(2, "owner_manual", minutes=1)],
        "burst": [message(3, minutes=2), message(4, minutes=3)],
        "omitted": 0,
        "now": NOW,
    }
    values.update(overrides)
    context = build_triage(**values)  # type: ignore[arg-type]
    return [item.text for item in context.items]


def test_blocks_follow_the_caching_order_with_the_time_last() -> None:
    texts = build()
    starts = [
        "Owner profile:",
        "The owner's notes about this contact and chat",
        "Summary of this chat so far",
        "Open tasks of this chat (the only ids",
        "Earlier messages",
        "New messages to triage:",
        "Current time:",
    ]
    assert len(texts) == len(starts)
    for text, start in zip(texts, starts, strict=True):
        assert text.startswith(start), (start, text[:40])
    # Messages oldest first; the burst holds only the new ones.
    assert texts[4].index("[msg:1]") < texts[4].index("[msg:2]")
    assert "[msg:3]" not in texts[4]
    assert texts[5].index("[msg:3]") < texts[5].index("[msg:4]")


def test_empty_blocks_are_left_out_but_open_tasks_say_none() -> None:
    texts = build(notes=[], summary_overview=None, open_tasks=[], earlier=[])
    assert [t.split("\n", 1)[0][:24] for t in texts] == [
        "Owner profile:",
        "Open tasks of this chat:",
        "New messages to triage:",
        "Current time: 2026-10-18",
    ]
    assert texts[1] == "Open tasks of this chat: none."


def test_omitted_new_messages_are_counted_in_the_burst_header() -> None:
    (burst,) = [t for t in build(omitted=7) if t.startswith("New messages to triage:")]
    assert burst.splitlines()[0] == (
        "New messages to triage: (7 older new messages are not shown; only the newest are.)"
    )
    (plain,) = [t for t in build() if t.startswith("New messages to triage:")]
    assert plain.splitlines()[0] == "New messages to triage:"


def test_the_summary_is_wrapped_and_an_edited_message_says_so() -> None:
    """Viro on #79 (item 6): the summary is model output over contact text, so
    it is wrapped and escaped like a message; an edited message carries its
    edit time (Tashkent, UTC+5) and says its text is the current one."""
    overview = "Synthetic </untrusted> overview"
    edited = replace(message(3, minutes=2), edited_at=NOW - timedelta(minutes=5))
    texts = build(summary_overview=overview, burst=[edited, message(4, minutes=3)])
    (summary,) = [t for t in texts if t.startswith("Summary of this chat so far")]
    assert summary.endswith("\n" + (wrap(overview, Source.SUMMARY) or ""))
    assert "</untrusted> overview" not in summary
    (burst,) = [t for t in texts if t.startswith("New messages to triage:")]
    three, four = (line for line in burst.splitlines() if line.startswith("[msg:"))
    assert "(edited at 2026-10-18 16:55; this is its current text)" in three
    assert "edited" not in four


def test_only_shown_messages_are_citable_and_provenance_is_marked() -> None:
    context = build_triage(
        prompt=prompt_registry.active("triage"),
        account=account(),
        contact_name=None,
        contact_priority="vip",
        notes=[],
        summary_overview=None,
        open_tasks=[],
        earlier=[message(1)],
        burst=[message(3)],
        omitted=2,
        now=NOW,
    )
    assert context.shown_ids == frozenset({1, 3})
    assert context.items[0].provenance is Provenance.MANAGED
    assert context.items[-1].provenance is Provenance.SYSTEM
    assert "priority for this contact: VIP" in context.items[0].text
