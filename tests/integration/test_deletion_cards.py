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

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import AuditLogEntry
from assistant.conversations.models import Conversation
from assistant.memory import items
from assistant.memory.models import MemoryItem
from assistant.telegram.control import panels
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    Env,
    business_message,
    connect,
    deliver,
    environment,
    grant_managed,
    live_card,
    press,
    private_message,
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

    assert await mode(env, conversation) == "ignore"
    assert await stored(env, "CHATK") == 0  # messages, chat note, name, raw updates
    assert await stored(env, "NOTEK") == 1  # the owner's note about the person stays
    assert await stored(env, "CHATB") > 0  # another chat is untouched
    assert await deletions(env) == 1
    assert any("Deleted" in t for t in env.sender.texts)
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
    assert await stored(env, "CHATK") == 0
    assert await stored(env, "NOTEK") == 1
    assert await mode(env, conversation) == "observe"  # deleting is not ignoring
    assert await deletions(env) == 1


async def test_the_panel_can_delete_the_chat_and_the_notes_about_the_person(env: Env) -> None:
    await seed(env)
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "contact")
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
    assert await stored(env, "CHATB") == 0


async def test_a_delete_card_cannot_be_pressed_twice(env: Env) -> None:
    await seed(env)
    await deliver(env, private_message(OWNER, "/privacy delete"))
    token_card = await card(env, panels.DELETE_ALL_CARD)
    assert token_card is not None
    data = f"{panels.DELETE_ALL_CARD}:{token_card.token}:everything"
    from tests.integration.business_env import callback

    await deliver(env, callback(data))
    await deliver(env, callback(data))
    assert await deletions(env) == 1


# ------------------------------------------------------------ revocation offers the scopes
async def test_revoking_consent_offers_deleting_everything_too(env: Env) -> None:
    await seed(env)
    await grant_managed(env)
    await deliver(env, private_message(OWNER, "/privacy revoke"))
    shown = env.sender.sends[-1]
    assert keyboard_labels(shown["reply_markup"]) == ["Delete", "Delete everything", "Keep"]
    assert "panel" in shown["text"]  # one chat's data: from its panel
    await live_card(env, "managed_delete")
    await press(env, "managed_delete", "everything")
    for marker in ("CHATK", "NOTEK", "CHATB"):
        assert await stored(env, marker) == 0, marker
    assert await deletions(env) == 1
