"""S03.3 conversation modes: Ignore/Observe from the conversation panel, the
default mode of new chats and the chats listed by mode in /settings.

The required S03.3 tests (IMPLEMENTATION_PLAN §5), each findable by name:

- Ignore-mode non-storage — `test_ignore_mode_stores_nothing_from_the_next_message_on`;
- existing data kept after switching to Ignore —
  `test_existing_data_is_kept_after_switching_to_ignore`;
- conversation panel buttons, the Ignore confirmation (no deletion option
  until S03.5) — `test_the_panel_switches_to_ignore_only_after_a_confirmation`,
  `test_the_ignore_confirmation_offers_no_deletion_before_s03_5`;
- /settings: default mode for new chats —
  `test_new_chats_start_in_the_default_mode_set_in_settings`; chats listed by
  mode, paginated, opening the panel —
  `test_settings_lists_chats_by_mode_page_by_page_and_opens_a_panel`.

Synthetic text only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from assistant.accounts.models import AuditLogEntry, PendingInput
from assistant.conversations.models import Contact, Conversation, Message
from assistant.memory import items
from assistant.memory.models import ConversationSummary
from assistant.telegram.control import panels
from tests.integration.agent_env import one_id
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    inbound,
    payload_is_sql_null,
    private_message,
)

pytestmark = pytest.mark.integration

FIRST = "SYNTH-FIRST Ertaga uchrashamiz"
LATER = "SYNTH-LATER Manzil: Chilonzor 5"


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def conversation_row(env: Env, chat_id: int = CONTACT) -> Conversation:
    async with env.factory() as session:
        return (
            await session.execute(
                select(Conversation).where(Conversation.business_chat_id == chat_id)
            )
        ).scalar_one()


async def messages(env: Env, conversation_id: int) -> list[Message]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )


async def card(env: Env, kind: str) -> PendingInput:
    """The newest open card of `kind`."""
    async with env.factory() as session:
        return (
            (
                await session.execute(
                    select(PendingInput)
                    .where(PendingInput.kind == kind)
                    .where(PendingInput.token.is_not(None))
                    .order_by(PendingInput.id.desc())
                )
            )
            .scalars()
            .first()
        )  # type: ignore[return-value]


async def tap(env: Env, kind: str, op: str) -> None:
    row = await card(env, kind)
    assert row is not None, f"no open {kind} card"
    await deliver(env, callback(f"{kind}:{row.token}:{op}", message_id=row.prompt_message_id))


def keyboard_ops(markup: Any) -> list[str]:
    return [b.callback_data.rsplit(":", 1)[1] for row in markup.inline_keyboard for b in row]


def keyboard_labels(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def open_panel(env: Env, chat_id: int = CONTACT) -> None:
    await deliver(env, private_message(OWNER, f"/start bizChat{chat_id}"))


async def ignore_from_panel(env: Env) -> None:
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "ignore")
    await tap(env, panels.IGNORE_CARD, "confirm")


# ------------------------------------------------------------ Ignore
async def test_the_panel_switches_to_ignore_only_after_a_confirmation(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    await open_panel(env)
    panel_send = env.sender.sends[-1]
    assert "Mode: observe" in panel_send["text"]
    assert keyboard_ops(panel_send["reply_markup"]) == ["observe", "ignore"]
    assert keyboard_labels(panel_send["reply_markup"]) == ["Observe ✓", "Ignore"]

    await tap(env, panels.PANEL_CARD, "ignore")
    assert (await conversation_row(env)).mode == "observe"  # asked, not done
    question = env.sender.edits[-1]
    assert "Ignore the chat with Kamola?" in question["text"]
    assert "What is already stored stays" in question["text"]

    await tap(env, panels.IGNORE_CARD, "cancel")
    assert (await conversation_row(env)).mode == "observe"
    await tap(env, panels.PANEL_CARD, "ignore")
    await tap(env, panels.IGNORE_CARD, "confirm")
    assert (await conversation_row(env)).mode == "ignore"
    panel_again = env.sender.edits[-1]
    assert keyboard_labels(panel_again["reply_markup"]) == ["Observe", "Ignore ✓"]

    # Observe applies at once from the panel.
    await tap(env, panels.PANEL_CARD, "observe")
    assert (await conversation_row(env)).mode == "observe"
    async with env.factory() as session:
        changes = (
            (
                await session.execute(
                    select(AuditLogEntry.details).where(
                        AuditLogEntry.event == "conversation_mode_changed"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [(c["from"], c["to"]) for c in changes] == [
        ("observe", "ignore"),
        ("ignore", "observe"),
    ]


async def test_the_ignore_confirmation_offers_no_deletion_before_s03_5(env: Env) -> None:
    """The "Also delete existing data" option needs `delete_conversation_data`
    (S03.5): until then the confirmation offers Ignore and Cancel only — a
    marked hook, not a button that pretends to delete."""
    assert panels.IGNORE_DELETE_OPTION is None
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST))
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "ignore")
    assert keyboard_ops(env.sender.edits[-1]["reply_markup"]) == ["confirm", "cancel"]
    await tap(env, panels.IGNORE_CARD, "purge")  # a forged op does nothing
    assert (await conversation_row(env)).mode == "observe"


async def test_ignore_mode_stores_nothing_from_the_next_message_on(env: Env) -> None:
    """FR-OBS-08: after Ignore, a new message, an edit of it and a new name
    leave no content: metadata only, payload cleared, no summary flag."""
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    await ignore_from_panel(env)
    later = business_message(message_id=2, message_text=LATER, first_name="Renamed")
    await deliver(env, later)
    await deliver(
        env,
        business_message(message_id=2, message_text=LATER + " edited", edit=True),
    )
    conversation = await conversation_row(env)
    rows = await messages(env, conversation.id)
    second = next(m for m in rows if m.telegram_message_id == 2)
    assert (second.text, second.media, second.search_key, second.content_hash) == (
        None,
        None,
        None,
        None,
    )
    assert await payload_is_sql_null(env, later)
    assert (await inbound(env, later)).status in {"processed", "ignored"}
    assert conversation.pending_ai_since is None
    async with env.factory() as session:
        contact = (
            await session.execute(select(Contact).where(Contact.telegram_user_id == CONTACT))
        ).scalar_one()
    assert contact.display_name == "Kamola"  # the new name was not stored


async def test_existing_data_is_kept_after_switching_to_ignore(env: Env) -> None:
    """Ignore stops storage going forward; it deletes nothing (S03.5 adds
    the optional deletion)."""
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    conversation = await conversation_row(env)
    async with env.factory() as session, session.begin():
        session.add(
            ConversationSummary(
                account_id=conversation.account_id,
                conversation_id=conversation.id,
                kind="rolling",
                covers_through_message_id=None,
                summary={"overview": "SYNTH summary"},
            )
        )
        await items.save(
            session,
            account_id=conversation.account_id,
            content="SYNTH note about Kamola",
            kind="fact",
            contact_id=conversation.contact_id,
            actor="owner",
            now=env.clock.now(),
        )
    await ignore_from_panel(env)
    [first] = await messages(env, conversation.id)
    assert first.text == FIRST
    assert first.search_key is not None
    async with env.factory() as session:
        summaries = (await session.execute(select(ConversationSummary))).scalars().all()
        notes, _ = await items.list_items(
            session, conversation.account_id, contact_id=conversation.contact_id
        )
    assert [s.summary["overview"] for s in summaries] == ["SYNTH summary"]
    assert [n.content for n in notes] == ["SYNTH note about Kamola"]
    # The panel still shows them (J12).
    await open_panel(env)
    shown = env.sender.sends[-1]["text"]
    assert "SYNTH summary" in shown
    assert "SYNTH note about Kamola" in shown


# ------------------------------------------------------------ /settings
async def test_new_chats_start_in_the_default_mode_set_in_settings(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    settings = env.sender.sends[-1]
    assert "New chats start in: Observe" in settings["text"]
    assert "dignore" in keyboard_ops(settings["reply_markup"])
    await tap(env, panels.SETTINGS_CARD, "dignore")
    assert "New chats start in: Ignore" in env.sender.edits[-1]["text"]

    new_chat = business_message(message_id=1, message_text=FIRST, first_name="Newcomer")
    await deliver(env, new_chat)
    conversation = await conversation_row(env)
    assert conversation.mode == "ignore"
    [message] = await messages(env, conversation.id)
    assert message.text is None
    assert await payload_is_sql_null(env, new_chat)
    async with env.factory() as session:
        contact = (
            await session.execute(select(Contact).where(Contact.telegram_user_id == CONTACT))
        ).scalar_one()
    assert contact.display_name is None  # not even the name

    # Back to Observe: the next new chat is stored again.
    await tap(env, panels.SETTINGS_CARD, "dobserve")
    await deliver(
        env,
        business_message(message_id=1, from_id=7000000006, chat_id=7000000006, message_text=LATER),
    )
    other = await conversation_row(env, 7000000006)
    assert other.mode == "observe"
    assert [m.text for m in await messages(env, other.id)] == [LATER]


async def test_settings_lists_chats_by_mode_page_by_page_and_opens_a_panel(env: Env) -> None:
    await connect(env)
    for n in range(10):
        chat = 7100000000 + n
        await deliver(
            env,
            business_message(
                message_id=1,
                from_id=chat,
                chat_id=chat,
                first_name=f"Synth{n}",
                date=T_BASE + n,
                message_text=f"SYNTH {n}",
            ),
        )
    await deliver(env, private_message(OWNER, "/settings"))
    assert "Observed chats (10)" in keyboard_labels(env.sender.sends[-1]["reply_markup"])
    await tap(env, panels.SETTINGS_CARD, "lobserve")
    first_page = env.sender.sends[-1]
    assert "page 1 of 2" in first_page["text"]
    labels = keyboard_labels(first_page["reply_markup"])
    assert labels[0] == "Synth9"  # most recent first
    assert len([label for label in labels if label.startswith("Synth")]) == panels.PAGE_SIZE
    assert labels[-1] == "Next ▶"

    await tap(env, panels.LIST_CARD, "p_observe_2")
    second_page = env.sender.edits[-1]
    assert "page 2 of 2" in second_page["text"]
    assert keyboard_labels(second_page["reply_markup"]) == ["Synth1", "Synth0", "◀ Previous"]

    target = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=7100000000
    )
    await tap(env, panels.LIST_CARD, f"o_{target}")
    panel = env.sender.sends[-1]
    assert panel["text"].startswith("<b>Synth0</b>")
    assert keyboard_ops(panel["reply_markup"]) == ["observe", "ignore"]


async def test_a_list_cannot_open_another_accounts_chat(env: Env) -> None:
    """The op carries a conversation id from the client: it is checked
    against the pressing owner's account (§6 invariant 12)."""
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST))
    await deliver(env, private_message(OWNER, "/settings"))
    await tap(env, panels.SETTINGS_CARD, "lobserve")
    sends_before = len(env.sender.sends)
    await tap(env, panels.LIST_CARD, "o_999999999")
    assert len(env.sender.sends) == sends_before
    assert env.sender.answered_callbacks[-1]["text"] == "This button is no longer active."
