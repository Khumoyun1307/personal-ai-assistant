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

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import AuditLogEntry
from assistant.conversations.models import Conversation
from assistant.memory import catch_up, items
from assistant.memory.models import MemoryItem
from assistant.telegram.control import panels
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    grant_managed,
    live_card,
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
    assert any("Deleted" in t for t in receipts.sends)
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
    assert set(job_args) == {"account_id", "scope", "subject_id", "card_id"}
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
    from assistant.privacy.requests import RequestedDeletions

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
    from assistant.privacy import deletion

    assert deletion.EVENT_DATA_DELETED in catch_up.DELETION_EVENTS


async def test_revoking_consent_offers_deleting_everything_too(env: Env) -> None:
    await seed(env)
    await grant_managed(env)
    await deliver(env, private_message(OWNER, "/privacy revoke"))
    shown = env.sender.sends[-1]
    assert keyboard_labels(shown["reply_markup"]) == ["Delete", "Delete everything", "Keep"]
    assert "panel" in shown["text"]  # one chat's data: from its panel
    await live_card(env, "managed_delete")
    await press(env, "managed_delete", "everything")
    await run_deletion_jobs(env)
    for marker in ("CHATK", "NOTEK", "CHATB"):
        assert await stored(env, marker) == 0, marker
    assert await deletions(env) == 1
