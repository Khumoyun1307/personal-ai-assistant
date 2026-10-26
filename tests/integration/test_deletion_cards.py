"""S03.5 deletion from the control chat's own cards (FR-PRV-04, FR-CTL-10).

The required S03.5 test (IMPLEMENTATION_PLAN §5), findable by name:

- choosing "Also delete existing data" on the Ignore confirmation deletes
  that conversation's data —
  `test_choosing_also_delete_existing_data_on_the_ignore_confirmation_deletes_that_conversations_data`.

Plus: the conversation panel's delete screen (chat, or chat and notes, or
cancel), /settings → Delete everything (asks first), `/privacy delete`, and
the consent-revocation card offering these scopes (S02.4).
Synthetic text only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from assistant.accounts.models import AuditLogEntry, ControlTurn
from assistant.conversations.models import Conversation
from assistant.inbox.processor import requeue_failed
from assistant.memory import catch_up, items
from assistant.memory.models import MemoryItem
from assistant.privacy import deletion
from assistant.privacy.requests import RequestedDeletions
from assistant.telegram.control import panels
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    grant_managed,
    inbound,
    live_card,
    payload_is_sql_null,
    press,
    private_message,
    run_deletion_jobs,
)
from tests.integration.test_conversation_modes import (
    card,
    keyboard_labels,
    keyboard_ops,
    open_panel,
    tap,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def seed(env: Env) -> tuple[int, int]:
    """Kamola's chat (text, name, a chat note and a note about her) and
    Bobur's chat. Returns Kamola's (conversation id, contact id)."""
    await connect(env)
    await deliver(
        env, business_message(message_id=1, message_text="CHATK one", first_name="CHATK Kamola")
    )
    await deliver(
        env,
        business_message(
            message_id=1, from_id=OTHER_CONTACT, chat_id=OTHER_CONTACT, message_text="CHATB one"
        ),
    )
    async with env.factory() as session:
        conversation, contact = (
            await session.execute(
                select(Conversation.id, Conversation.contact_id).where(
                    Conversation.business_chat_id == CONTACT
                )
            )
        ).one()
    async with env.factory() as session, session.begin():
        account_id = (
            await session.execute(
                select(Conversation.account_id).where(Conversation.id == conversation)
            )
        ).scalar_one()
        await items.save(
            session,
            account_id=account_id,
            content="CHATK chat note",
            kind="fact",
            conversation_id=conversation,
            actor="owner",
            now=env.clock.now(),
        )
        await items.save(
            session,
            account_id=account_id,
            content="NOTEK about Kamola",
            kind="fact",
            contact_id=contact,
            actor="owner",
            now=env.clock.now(),
        )
    return int(conversation), int(contact)


async def stored(env: Env, needle: str) -> int:
    """Rows of the main content tables holding `needle`."""
    async with env.factory() as session:
        total = 0
        for sql in (
            "SELECT count(*) FROM app.messages WHERE text ILIKE :n",
            "SELECT count(*) FROM app.memory_items WHERE content ILIKE :n",
            "SELECT count(*) FROM app.contacts WHERE display_name ILIKE :n",
            "SELECT count(*) FROM app.inbound_updates WHERE payload::text ILIKE :n",
        ):
            total += int((await session.execute(text(sql), {"n": f"%{needle}%"})).scalar_one())
        return total


async def last_press(env: Env) -> int:
    """The newest button press's inbound row."""
    async with env.factory() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT id FROM app.inbound_updates WHERE kind = 'callback_query' "
                        "ORDER BY id DESC LIMIT 1"
                    )
                )
            ).scalar_one()
        )


async def payload_is_null(env: Env, row_id: int) -> bool:
    async with env.factory() as session:
        return bool(
            (
                await session.execute(
                    text("SELECT payload IS NULL FROM app.inbound_updates WHERE id = :i"),
                    {"i": row_id},
                )
            ).scalar_one()
        )


async def deletions(env: Env) -> int:
    async with env.factory() as session:
        return len(
            (
                await session.execute(
                    select(AuditLogEntry.id).where(AuditLogEntry.event == "data_deleted")
                )
            ).all()
        )


async def mode(env: Env, conversation: int) -> str:
    async with env.factory() as session:
        return str(
            (
                await session.execute(
                    select(Conversation.mode).where(Conversation.id == conversation)
                )
            ).scalar_one()
        )


# ------------------------------------------------------------ Ignore + delete
async def test_choosing_also_delete_existing_data_on_the_ignore_confirmation_deletes_that_conversations_data(  # noqa: E501 - the plan's wording, findable
    env: Env,
) -> None:
    conversation, _ = await seed(env)
    assert panels.IGNORE_DELETE_OPTION is not None
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "ignore")
    question = env.sender.edits[-1]
    assert keyboard_ops(question["reply_markup"]) == [
        "confirm",
        panels.IGNORE_DELETE_OPTION[1],
        "cancel",
    ]
    assert "What is already stored stays" in question["text"]
    assert "cannot be undone" in question["text"]
    # Review items 4, 6, 10: what stays as D79 has it, arrivals, backups.
    assert "your notes about the person stay" not in question["text"]
    assert "Other notes about" in question["text"]
    assert "backups" in question["text"]
    assert "arriving" in question["text"]

    await tap(env, panels.IGNORE_CARD, panels.IGNORE_DELETE_OPTION[1])
    assert await mode(env, conversation) == "ignore"  # at once: nothing new is stored
    assert any("Deleting" in t for t in env.sender.texts)
    # The deletion itself is queued under the owner's control lock (D83).
    receipts = FakeChannel()
    assert await run_deletion_jobs(env, receipts) == 1

    assert await stored(env, "CHATK") == 0  # messages, chat note, name, raw updates
    assert await stored(env, "NOTEK") == 1  # the owner's note about the person stays
    assert await stored(env, "CHATB") > 0  # another chat is untouched
    assert await deletions(env) == 1
    [receipt] = receipts.sends
    assert "Deleted" in receipt
    assert "held change(s)" in receipt  # review item 5
    assert "answer" not in receipt
    assert "backups" in receipt  # review item 6
    # The panel comes back, in Ignore mode.
    assert "Ignore ✓" in keyboard_labels(env.sender.edits[-1]["reply_markup"])


async def test_plain_ignore_still_keeps_the_data(env: Env) -> None:
    conversation, _ = await seed(env)
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "ignore")
    await tap(env, panels.IGNORE_CARD, "confirm")
    assert await mode(env, conversation) == "ignore"
    assert await stored(env, "CHATK") > 0
    assert await deletions(env) == 0


# ------------------------------------------------------------ panel deletion
async def test_the_panel_deletes_a_chats_data_only_after_a_second_tap(env: Env) -> None:
    conversation, _ = await seed(env)
    await open_panel(env)
    assert "delete" in keyboard_ops(env.sender.sends[-1]["reply_markup"])
    await tap(env, panels.PANEL_CARD, "delete")
    asked = env.sender.edits[-1]
    assert "cannot be undone" in asked["text"]
    assert keyboard_ops(asked["reply_markup"]) == ["chat", "contact", "cancel"]
    assert "Other notes about" in asked["text"]  # D79 (review item 10)
    assert "notes saved about" in asked["text"]  # review item 8
    assert "only mention" in asked["text"]
    assert "backups" in asked["text"]
    assert "arriving" in asked["text"]
    assert await stored(env, "CHATK") > 0  # asked, not done

    await tap(env, panels.DELETE_CARD, "cancel")
    assert await stored(env, "CHATK") > 0
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "chat")
    press_row = await last_press(env)
    await run_deletion_jobs(env)
    assert await stored(env, "CHATK") == 0
    assert await stored(env, "NOTEK") == 1
    assert await mode(env, conversation) == "observe"  # deleting is not ignoring
    assert await deletions(env) == 1
    # The press's own raw update (its message is the rendered panel) is cleared
    # too (safety review item 2).
    assert await payload_is_null(env, press_row)


async def test_the_panel_can_delete_the_chat_and_the_notes_about_the_person(env: Env) -> None:
    await seed(env)
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "contact")
    await run_deletion_jobs(env)
    assert await stored(env, "CHATK") == 0
    assert await stored(env, "NOTEK") == 0
    assert await stored(env, "CHATB") > 0
    async with env.factory() as session:
        assert len((await session.execute(select(MemoryItem))).scalars().all()) == 0


# ------------------------------------------------------------ delete everything
async def test_settings_delete_everything_asks_first_then_deletes_all_content(env: Env) -> None:
    await seed(env)
    await deliver(env, private_message(OWNER, "/settings"))
    await tap(env, panels.SETTINGS_CARD, "delall")
    asked = env.sender.edits[-1]
    assert "cannot be undone" in asked["text"]
    assert "keep observing" in asked["text"]  # what stays is said plainly
    assert "backups" in asked["text"]
    assert "arriving" in asked["text"]
    assert keyboard_ops(asked["reply_markup"]) == ["everything", "cancel"]
    await tap(env, panels.DELETE_ALL_CARD, "cancel")
    assert await stored(env, "CHATB") > 0

    await tap(env, panels.SETTINGS_CARD, "delall")
    await tap(env, panels.DELETE_ALL_CARD, "everything")
    await run_deletion_jobs(env)
    for marker in ("CHATK", "NOTEK", "CHATB"):
        assert await stored(env, marker) == 0, marker
    assert await deletions(env) == 1


async def test_privacy_delete_shows_the_delete_everything_card(env: Env) -> None:
    await seed(env)
    await deliver(env, private_message(OWNER, "/privacy delete"))
    shown = env.sender.sends[-1]
    assert keyboard_ops(shown["reply_markup"]) == ["everything", "cancel"]
    assert "panel" in shown["text"]  # how to delete one chat instead
    assert await stored(env, "CHATB") > 0
    await tap(env, panels.DELETE_ALL_CARD, "everything")
    await run_deletion_jobs(env)
    assert await stored(env, "CHATB") == 0


async def test_a_delete_card_cannot_be_pressed_twice(env: Env) -> None:
    await seed(env)
    await deliver(env, private_message(OWNER, "/privacy delete"))
    token_card = await card(env, panels.DELETE_ALL_CARD)
    assert token_card is not None
    data = f"{panels.DELETE_ALL_CARD}:{token_card.token}:everything"
    await deliver(env, callback(data))
    await deliver(env, callback(data))
    assert await run_deletion_jobs(env) == 1  # one job, one deletion
    assert await deletions(env) == 1


async def test_a_button_deletion_is_a_job_under_the_owners_control_lock(env: Env) -> None:
    """Nothing is deleted in the press itself: the job, with ids and an enum
    only, queues behind any owner run or applied confirmation (M03 D83)."""
    await seed(env)
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "chat")
    assert await stored(env, "CHATK") > 0  # not yet
    assert await deletions(env) == 0
    async with env.factory() as session:
        lock, queue, job_args, account_id = (
            await session.execute(
                text(
                    "SELECT j.lock, j.queue_name, j.args, a.id "
                    "FROM procrastinate.procrastinate_jobs j, app.accounts a "
                    "WHERE j.task_name = 'privacy.delete_data' AND a.telegram_user_id = :o"
                ),
                {"o": OWNER},
            )
        ).one()
    assert lock == f"acct:{account_id}:control"
    assert queue == "llm"
    assert set(job_args) == {"account_id", "scope", "subject_id", "card_id", "turn_cutoff"}
    assert job_args["scope"] == "conversation"
    assert all(isinstance(v, int) for k, v in job_args.items() if k != "scope")


async def test_a_deletion_job_run_twice_deletes_once(env: Env) -> None:
    """A stalled-worker retry runs the job again: it finds its own audit row
    (keyed by the card) and neither deletes a message that arrived since nor
    sends a second receipt."""
    await seed(env)
    await deliver(env, private_message(OWNER, "/privacy delete"))
    await tap(env, panels.DELETE_ALL_CARD, "everything")
    async with env.factory() as session:
        job_args = (
            await session.execute(
                text(
                    "SELECT args FROM procrastinate.procrastinate_jobs "
                    "WHERE task_name = 'privacy.delete_data'"
                )
            )
        ).scalar_one()
    receipts = FakeChannel()
    await run_deletion_jobs(env, receipts)
    await deliver(
        env,
        business_message(
            message_id=9, from_id=OTHER_CONTACT, chat_id=OTHER_CONTACT, message_text="CHATB new"
        ),
    )
    await RequestedDeletions(env.factory, env.clock, receipts).run(**job_args)
    assert await stored(env, "CHATB") > 0
    assert await deletions(env) == 1
    assert len(receipts.sends) == 1


# ------------------------------------------------------------ revocation offers the scopes
async def test_a_catch_up_after_a_chat_deletion_says_earlier_messages_were_deleted(
    env: Env,
) -> None:
    """General review item 13: deleted rows leave no tombstone, so the
    deletion itself is the marker: a later catch-up over a range that began
    before it says that messages from before it are not included."""
    conversation, _ = await seed(env)
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "chat")
    await run_deletion_jobs(env)
    now = env.clock.now()
    async with env.factory() as session:
        account_id, bobur = (
            await session.execute(
                select(Conversation.account_id, Conversation.id).where(
                    Conversation.business_chat_id == OTHER_CONTACT
                )
            )
        ).one()

        async def lines(start: datetime, ids: list[int] | None) -> list[str]:
            coverage = await catch_up.period_coverage(
                session, account_id, start=start, end=now + timedelta(hours=1), conversation_ids=ids
            )
            return catch_up.caveats(coverage, "UTC")

        def says_deleted(found: list[str]) -> bool:
            return any("deleted at the owner's request" in line for line in found)

        assert says_deleted(await lines(now - timedelta(days=7), [conversation]))
        assert says_deleted(await lines(now - timedelta(days=7), None))
        assert not says_deleted(await lines(now - timedelta(days=7), [bobur]))
        # A range that starts after the deletion lost nothing to it.
        assert not says_deleted(await lines(now + timedelta(minutes=1), [conversation]))
    assert deletion.EVENT_DATA_DELETED in catch_up.DELETION_EVENTS


async def test_revoking_consent_offers_deleting_everything_too(env: Env) -> None:
    await seed(env)
    await grant_managed(env)
    await deliver(env, private_message(OWNER, "/privacy revoke"))
    shown = env.sender.sends[-1]
    assert keyboard_labels(shown["reply_markup"]) == ["Delete", "Delete everything", "Keep"]
    assert "panel" in shown["text"]  # one chat's data: from its panel
    assert "backups" in shown["text"]  # review item 6
    await live_card(env, "managed_delete")
    await press(env, "managed_delete", "everything")
    await run_deletion_jobs(env)
    for marker in ("CHATK", "NOTEK", "CHATB"):
        assert await stored(env, marker) == 0, marker
    assert await deletions(env) == 1


# ------------------------------------------------------------ fix round 2
async def queued_job_args(env: Env) -> dict[str, Any]:
    async with env.factory() as session:
        found = (
            await session.execute(
                text(
                    "SELECT args FROM procrastinate.procrastinate_jobs "
                    "WHERE task_name = 'privacy.delete_data' AND status = 'todo'"
                )
            )
        ).scalar_one()
    return dict(found)


async def turn_texts(env: Env) -> list[str]:
    async with env.factory() as session:
        rows = await session.execute(
            select(ControlTurn.text).where(ControlTurn.text.is_not(None)).order_by(ControlTurn.id)
        )
        return [str(value) for (value,) in rows.all()]


async def test_a_failed_deletion_job_tells_the_owner_that_nothing_was_deleted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 2, item 1 (invariant 11): the press promised a receipt. A job
    that fails part-way says so; the deletion is one transaction, so nothing
    of it was deleted, and the job ends failed (no silent success)."""
    await seed(env)
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "chat")

    async def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("synthetic failure after the messages went")

    monkeypatch.setattr(deletion, "_clear_names", broken)
    receipts = FakeChannel()
    with pytest.raises(RuntimeError):
        await RequestedDeletions(env.factory, env.clock, receipts).run(**await queued_job_args(env))
    [report] = receipts.sends
    assert "nothing was deleted" in report
    assert "Deleted:" not in report
    assert await stored(env, "CHATK") > 0  # rolled back as a whole
    assert await deletions(env) == 0


async def test_a_deletion_whose_commit_reports_an_error_is_checked_before_reporting(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit went through but the connection dropped while saying so:
    the job looks for its audit row and sends the real receipt, not
    "failed"."""
    await seed(env)
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "chat")
    real = deletion.delete_conversation_data

    async def then_commit_reports_failure(session: Any, **kwargs: Any) -> Any:
        report = await real(session, **kwargs)
        commit = session.commit

        async def committed_then_lost() -> None:
            await commit()
            raise OperationalError("synthetic", {}, Exception("connection lost after commit"))

        session.commit = committed_then_lost
        return report

    monkeypatch.setattr(deletion, "delete_conversation_data", then_commit_reports_failure)
    receipts = FakeChannel()
    outcome = await RequestedDeletions(env.factory, env.clock, receipts).run(
        **await queued_job_args(env)
    )
    assert outcome == "deleted"
    [report] = receipts.sends
    assert report.startswith("🗑 Deleted:")
    assert await stored(env, "CHATK") == 0
    assert await deletions(env) == 1


async def test_a_question_sent_after_delete_everything_keeps_its_text(env: Env) -> None:
    """Fix round 2, item 2: the question's run queues behind the deletion
    (same lock); the deletion clears the owner's turns only up to the tap."""
    await seed(env)
    await deliver(env, private_message(OWNER, "OLDQ asked before the tap"))
    await deliver(env, private_message(OWNER, "/privacy delete"))
    await tap(env, panels.DELETE_ALL_CARD, "everything")
    await deliver(env, private_message(OWNER, "NEWQ asked after the tap"))
    job = await queued_job_args(env)
    assert isinstance(job["turn_cutoff"], int)  # an id, not content
    await run_deletion_jobs(env)
    assert await turn_texts(env) == ["NEWQ asked after the tap"]


async def test_a_forward_from_the_chat_sent_after_its_deletion_keeps_its_text(env: Env) -> None:
    """The per-chat scope clears turns whose refs name the chat; a forward
    the owner sends after the tap is a new message and keeps its text."""
    await seed(env)
    before = private_message(OWNER, "CHATK forwarded before")
    before["message"]["forward_from"] = {"id": CONTACT, "is_bot": False, "first_name": "K"}
    before["message"]["forward_date"] = T_BASE
    await deliver(env, before)
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "chat")
    after = private_message(OWNER, "FWDNEW forwarded after")
    after["message"]["forward_from"] = {"id": CONTACT, "is_bot": False, "first_name": "K"}
    after["message"]["forward_date"] = T_BASE
    await deliver(env, after)
    await run_deletion_jobs(env)
    texts = await turn_texts(env)
    assert "FWDNEW forwarded after" in texts
    assert not any("CHATK" in t for t in texts)


async def test_the_pressed_panel_stops_showing_what_is_being_deleted(env: Env) -> None:
    """Fix round 2, item 4: the press re-renders the panel before the job
    runs; it must not show the name, summary or notes being deleted."""
    conversation, _ = await seed(env)
    async with env.factory() as session, session.begin():
        account_id = (
            await session.execute(
                select(Conversation.account_id).where(Conversation.id == conversation)
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO app.conversation_summaries "
                "(account_id, conversation_id, kind, summary) "
                "VALUES (:a, :c, 'rolling', CAST(:j AS jsonb))"
            ),
            {"a": account_id, "c": conversation, "j": json.dumps({"overview": "CHATK summary"})},
        )
    await open_panel(env)
    shown = env.sender.sends[-1]["text"]
    assert "CHATK summary" in shown
    assert "CHATK chat note" in shown
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "contact")
    deleting = env.sender.edits[-1]["text"]
    assert "CHATK" not in deleting
    assert "NOTEK" not in deleting
    assert "Deleting" in deleting

    await deliver(env, private_message(OWNER, f"/start bizChat{OTHER_CONTACT}"))
    await tap(env, panels.PANEL_CARD, "ignore")
    assert panels.IGNORE_DELETE_OPTION is not None
    await tap(env, panels.IGNORE_CARD, panels.IGNORE_DELETE_OPTION[1])
    deleting = env.sender.edits[-1]["text"]
    assert "CHATB" not in deleting
    assert "Deleting" in deleting
    assert "Ignore ✓" in keyboard_labels(env.sender.edits[-1]["reply_markup"])


async def test_a_dead_letter_cleared_by_a_deletion_is_not_requeued_empty(env: Env) -> None:
    """Fix round 2, item 7: the deletion clears a dead-lettered control
    update too (its text may quote the chat); `inbox requeue` then leaves it
    failed and counts it, instead of processing an empty update."""
    await seed(env)
    failed = private_message(OWNER, "CHATK dead letter")
    await deliver(env, failed)
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.inbound_updates SET status = 'failed' WHERE update_id = :u"),
            {"u": failed["update_id"]},
        )
    await deliver(env, private_message(OWNER, "/privacy delete"))
    await tap(env, panels.DELETE_ALL_CARD, "everything")
    await run_deletion_jobs(env)
    assert await payload_is_sql_null(env, failed)
    requeued = await requeue_failed(env.factory)
    assert (requeued.reset, requeued.cleared) == (0, 1)
    assert (await inbound(env, failed)).status == "failed"
