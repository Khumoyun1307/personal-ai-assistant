"""S03.3 conversation modes: Ignore/Observe from the conversation panel, the
default mode of new chats and the chats listed by mode in /settings.

The required S03.3 tests (IMPLEMENTATION_PLAN §5), each findable by name:

- Ignore-mode non-storage — `test_ignore_mode_stores_nothing_from_the_next_message_on`;
- existing data kept after switching to Ignore —
  `test_existing_data_is_kept_after_switching_to_ignore`;
- conversation panel buttons, the Ignore confirmation (its deletion option
  since S03.5) — `test_the_panel_switches_to_ignore_only_after_a_confirmation`,
  `test_the_ignore_confirmation_offers_the_deletion_option` (S03.5 wired it);
- /settings: default mode for new chats —
  `test_new_chats_start_in_the_default_mode_set_in_settings`; chats listed by
  mode, paginated, opening the panel —
  `test_settings_lists_chats_by_mode_page_by_page_and_opens_a_panel`.

M03 fix round C3 (owner check 11): what the panel says a mode change did,
the panel's saved / not-saved counts, "nothing to change" (D68), an Ignore
message that no longer keeps an expired name (B2), and guard tests for each
"nothing stored" property.

Synthetic text only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from assistant.accounts import preferences
from assistant.accounts.models import Account, AuditLogEntry, PendingInput
from assistant.actions.models import Action
from assistant.agent import owner_texts
from assistant.agent.locales import SupportedLocale
from assistant.agent.models import ToolConfirmation
from assistant.agent.tools.modes import MODE_EFFECT, UpdatePreferencesArgs
from assistant.conversations import modes
from assistant.conversations.models import Contact, Conversation, CoverageGap, Message
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest, ToolResultItem
from assistant.memory import items
from assistant.memory.models import ConversationSummary
from assistant.memory.retrieval import search_messages
from assistant.privacy.retention import sweep_all
from assistant.telegram.control import panels
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import (
    MEMORY_FLAGS,
    READ_FLAGS,
    S035_PREFS_UNCHANGED,
    args,
    ask,
    one_id,
    runtime,
)
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
    grant_managed,
    inbound,
    job_names,
    payload_is_sql_null,
    private_message,
    queued_refreshes,
    take_job,
)
from tests.integration.p2_factories import seed_action

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
    assert "Mode: <b>Observe</b> (new messages here are saved)" in panel_send["text"]
    assert keyboard_ops(panel_send["reply_markup"]) == [
        "observe",
        "ignore",
        "assist",  # S04.2
        "delete",
        "p_vip",
        "p_normal",
        "p_low",
    ]  # S03.5 adds Delete, S05.2P the contact's priority
    assert keyboard_labels(panel_send["reply_markup"]) == [
        "Observe ✓",
        "Ignore",
        "Assist",
        "Delete stored data…",
        "VIP",
        "Normal ✓",
        "Low",
    ]

    await tap(env, panels.PANEL_CARD, "ignore")
    assert (await conversation_row(env)).mode == "observe"  # asked, not done
    question = env.sender.edits[-1]
    assert "Ignore the chat with Kamola?" in question["text"]
    assert "What was saved before stays unless deleted" in question["text"]
    assert "New messages here are not saved" in question["text"]

    await tap(env, panels.IGNORE_CARD, "cancel")
    assert (await conversation_row(env)).mode == "observe"
    await tap(env, panels.PANEL_CARD, "ignore")
    await tap(env, panels.IGNORE_CARD, "confirm")
    assert (await conversation_row(env)).mode == "ignore"
    panel_again = env.sender.edits[-1]
    assert keyboard_labels(panel_again["reply_markup"]) == [
        "Observe",
        "Ignore ✓",
        "Assist",
        "Delete stored data…",
        "VIP",
        "Normal ✓",
        "Low",
    ]

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


async def test_the_ignore_confirmation_offers_the_deletion_option(env: Env) -> None:
    """S03.5 wired "Also delete existing data" (`delete_conversation_data`);
    the deletion itself is tested in `test_deletion_cards.py`. A forged op
    still does nothing."""
    assert panels.IGNORE_DELETE_OPTION is not None
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST))
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "ignore")
    assert keyboard_ops(env.sender.edits[-1]["reply_markup"]) == [
        "confirm",
        panels.IGNORE_DELETE_OPTION[1],
        "cancel",
    ]
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


# ------------------------------------------------------------ C3: what the panel says
async def test_the_panel_says_what_ignore_did(env: Env) -> None:
    """Owner check 11: after the confirmation the panel opens with what
    Ignore does and does not do — not a toast that disappears (§6
    invariant 11)."""
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    await ignore_from_panel(env)

    shown = env.sender.edits[-1]["text"]
    assert shown.startswith("✅ <b>Ignore is on</b> for the chat with Kamola.")
    assert "• New messages here are not saved" in shown
    assert "left out of summaries, catch-ups" in shown
    assert "messages until 90 days old" in shown
    assert "Delete stored data…" in shown
    assert "Mode: <b>Ignore</b> (new messages here are not saved)" in shown
    assert env.sender.answered_callbacks[-1]["text"] == "Ignore is on"


async def test_the_panel_says_what_observe_did_and_that_ignored_messages_are_lost(
    env: Env,
) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    await ignore_from_panel(env)
    await deliver(env, business_message(message_id=2, message_text=LATER))
    await tap(env, panels.PANEL_CARD, "observe")

    shown = env.sender.edits[-1]["text"]
    assert shown.startswith("✅ <b>Observe is on</b> for the chat with Kamola.")
    assert "• New messages here are saved" in shown
    assert "• Messages that arrived while the chat was ignored were not saved:" in shown
    assert "cannot be recovered" not in shown  # a later edit can store one
    assert "/privacy managed" in shown  # AI for managed chats is off: said so
    assert "Mode: <b>Observe</b> (new messages here are saved)" in shown

    # With AI for the chats on, the receipt does not say it is off.
    await grant_managed(env)
    await tap(env, panels.PANEL_CARD, "ignore")
    await tap(env, panels.IGNORE_CARD, "confirm")
    await tap(env, panels.PANEL_CARD, "observe")
    shown = env.sender.edits[-1]["text"]
    assert shown.startswith("✅ <b>Observe is on</b>")
    assert "/privacy managed" not in shown


@pytest.mark.parametrize(
    ("tag", "locale"), [("en", "en"), ("uz", "uz-Latn"), ("uz-Cyrl", "uz-Cyrl"), ("ru", "ru")]
)
async def test_the_mode_receipt_follows_the_owner_language(
    env: Env, tag: str, locale: SupportedLocale
) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    async with env.factory() as session, session.begin():
        await session.execute(text("UPDATE app.accounts SET locale_pref = :t"), {"t": tag})
    await ignore_from_panel(env)

    shown = env.sender.edits[-1]["text"]
    header = owner_texts.MODE_ON[locale]["ignore"].format(name="Kamola")
    assert shown.startswith("✅ " + header)
    assert owner_texts.IGNORE_NOT_SAVED[locale] in shown
    assert owner_texts.MODE_LINE[locale]["ignore"] in shown


async def test_the_panel_counts_an_ignored_message_as_not_saved(env: Env) -> None:
    """The panel's count was every row with a direction, so a message that
    arrived in Ignore made it go up (owner check 11 could not be read)."""
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    await ignore_from_panel(env)
    await deliver(env, business_message(message_id=2, message_text=LATER))
    await open_panel(env)

    shown = env.sender.sends[-1]["text"]
    assert "Messages saved: 1 (1 contact)" in shown
    assert "Not saved (arrived while ignored): 1" in shown


async def test_confirming_ignore_on_an_already_ignored_chat_says_nothing_changed(
    env: Env,
) -> None:
    """A stale confirmation (the chat was set to Ignore another way in the
    meantime) says "nothing to change", not that it ignored it (D68)."""
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "ignore")  # the confirmation is shown
    conversation = await conversation_row(env)
    async with transaction(env.factory) as uow:
        await modes.set_mode(
            uow.session,
            account_id=conversation.account_id,
            conversation_id=conversation.id,
            mode="ignore",
            actor="owner",
            clock=env.clock,
        )
    await tap(env, panels.IGNORE_CARD, "confirm")

    assert env.sender.answered_callbacks[-1]["text"] == "Nothing changed"
    shown = env.sender.edits[-1]["text"]
    assert shown.startswith("Nothing to change: the chat with Kamola is already in Ignore.")
    assert "Ignore is on" not in shown


async def test_tapping_the_current_mode_says_nothing_to_change(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "observe")

    assert env.sender.answered_callbacks[-1]["text"] == "Nothing changed"
    shown = env.sender.edits[-1]["text"]
    assert shown.startswith("Nothing to change: the chat with Kamola is already in Observe.")


# ------------------------------------------------------------ C3: retention (B2)
async def test_messages_in_ignore_mode_do_not_keep_an_expired_contacts_name(env: Env) -> None:
    """B2: an Ignore message moved `contacts.updated_at`, which the name sweep
    reads, so a contact who kept writing kept a name saved before the switch
    long after its messages expired (§10.4: names while content is retained)."""
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    await ignore_from_panel(env)
    env.clock.advance(days=60)
    await deliver(
        env,
        business_message(
            message_id=2,
            message_text=LATER,
            first_name="Kamola",
            date=T_BASE + int(timedelta(days=60).total_seconds()),
        ),
    )
    env.clock.advance(days=31)  # message 1 is past the 90-day retention now

    await sweep_all(env.factory, env.clock)

    conversation = await conversation_row(env)
    first = next(m for m in await messages(env, conversation.id) if m.telegram_message_id == 1)
    assert first.purged_at is not None
    async with env.factory() as session:
        contact = (
            await session.execute(select(Contact).where(Contact.telegram_user_id == CONTACT))
        ).scalar_one()
    assert (contact.display_name, contact.username) == (None, None)


# ------------------------------------------------------------ C3: guard tests
#: Synthetic markers delivered in an Ignore chat; none may be stored anywhere.
IGNORED_MARKERS = (
    "IGNTXT",
    "IGNNAME",
    "ignuser",
    "IGNFID",
    "IGNFUQ",
    "IGNCAP",
    "IGNEDIT2",
    "IGNEDIT1",
    "IGNPRE",
)


async def columns_holding(env: Env, needle: str) -> set[str]:
    """Every column of every table in our schemas (content-bearing or not)
    whose value, as text, contains `needle`."""
    found: set[str] = set()
    async with env.factory() as session:
        columns = (
            await session.execute(
                text(
                    "SELECT c.table_schema, c.table_name, c.column_name "
                    "FROM information_schema.columns c JOIN information_schema.tables t "
                    "USING (table_schema, table_name) "
                    "WHERE c.table_schema IN ('app', 'procrastinate') "
                    "AND t.table_type = 'BASE TABLE'"
                )
            )
        ).all()
        for schema, table, column in columns:
            row = await session.execute(
                text(
                    f'SELECT 1 FROM "{schema}"."{table}" '  # noqa: S608 - catalog identifiers
                    f'WHERE "{column}"::text ILIKE :needle LIMIT 1'
                ),
                {"needle": f"%{needle}%"},
            )
            if row.first() is not None:
                found.add(f"{schema}.{table}.{column}")
    return found


async def test_an_ignored_chats_messages_leave_no_text_name_or_file_id_in_any_column(
    migrated_url: str,
) -> None:
    """Owner check 11, verified: after Ignore, a text with a new name and
    username, a photo with a caption and file ids, an edit of it, an edit of
    a message saved before the switch and an edit of a message never seen
    leave their content in no column of the database (content-bearing or
    not); every raw update is cleared; no summary job is queued; no gap is
    recorded. With the summary trigger wired, as production has it."""
    async with environment(migrated_url, summaries=True) as env:
        await connect(env)
        await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
        await ignore_from_panel(env)
        conversation = await conversation_row(env)
        # Message 1 queued a refresh (the trigger is wired). A worker takes
        # it, so its queueing lock no longer swallows a later defer.
        assert (await job_names(env)).count("memory.refresh_summary") == 1
        await take_job(env, conversation.id)
        assert await queued_refreshes(env, conversation.id) == 0
        photo = [
            {"file_id": "IGNFID-1", "file_unique_id": "IGNFUQ-1", "file_size": 9},
            {"file_id": "IGNFID-2", "file_unique_id": "IGNFUQ-2", "file_size": 99, "width": 2},
        ]
        updates = [
            business_message(
                message_id=2,
                message_text="IGNTXT Ertaga keling",
                first_name="IGNNAME",
                username="ignuser",
            ),
            business_message(
                message_id=3,
                message_text=None,
                first_name="IGNNAME",
                extra={"photo": photo, "caption": "IGNCAP rasm"},
            ),
            business_message(message_id=2, message_text="IGNEDIT2 tahrir", edit=True),
            business_message(message_id=1, message_text="IGNEDIT1 tahrir", edit=True),
            business_message(message_id=9, message_text="IGNPRE eski", edit=True),
        ]
        for update in updates:
            await deliver(env, update)

        for marker in IGNORED_MARKERS:
            assert await columns_holding(env, marker) == set(), marker
        for update in updates:
            assert await payload_is_sql_null(env, update)
            assert (await inbound(env, update)).status == "processed"
        conversation = await conversation_row(env)
        async with env.factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT telegram_message_id, text, media, search_key, search_tsv, "
                        "script, lang_guess, content_hash, content_type "
                        "FROM app.messages WHERE conversation_id = :c "
                        "AND telegram_message_id <> 1 ORDER BY telegram_message_id"
                    ),
                    {"c": conversation.id},
                )
            ).all()
            contact = (
                await session.execute(select(Contact).where(Contact.telegram_user_id == CONTACT))
            ).scalar_one()
            gaps = (await session.execute(select(CoverageGap))).scalars().all()
        # Metadata only: the row says a message came, not what it said.
        assert [(r[0], r[1:8], r[8]) for r in rows] == [
            (2, (None,) * 7, "text"),
            (3, (None,) * 7, "photo"),
            (9, (None,) * 7, "text"),
        ]
        assert await queued_refreshes(env, conversation.id) == 0
        assert (await job_names(env)).count("memory.refresh_summary") == 1
        assert conversation.pending_ai_since is None
        assert list(gaps) == []
        assert (contact.display_name, contact.username) == ("Kamola", None)


async def test_what_was_saved_before_ignore_is_still_found_by_search(env: Env) -> None:
    """The receipt says Ignore does not hide earlier data from answers: an
    owner run's search still finds a message saved before the switch."""
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    await ignore_from_panel(env)
    conversation = await conversation_row(env)

    async with env.factory() as session:
        result = await search_messages(
            session, conversation.account_id, ["uchrashamiz"], now=env.clock.now()
        )

    assert [hit.text for hit in result.hits] == [FIRST]


# ------------------------------------------------------------ /settings
async def test_new_chats_start_in_the_default_mode_set_in_settings(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    settings = env.sender.sends[-1]
    assert "New chats start in: Observe" in settings["text"]
    assert "dignore" in keyboard_ops(settings["reply_markup"])
    await tap(env, panels.SETTINGS_CARD, "dignore")
    assert "New chats start in: Ignore" in env.sender.edits[-1]["text"]

    new_chat = business_message(
        message_id=1, message_text=FIRST, first_name="Newcomer", username="newuser"
    )
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
    assert contact.username is None  # nor the username (C3 guard)

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
    assert keyboard_ops(panel["reply_markup"]) == [
        "observe",
        "ignore",
        "assist",
        "delete",
        "p_vip",
        "p_normal",
        "p_low",
    ]


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


# ------------------------------------------------------------ Assist (S04.2 §3.7, P2-T11)
async def _assist_chat(env: Env) -> int:
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_id=1, message_text=FIRST, first_name="Kamola"))
    return (await conversation_row(env)).id


def _results(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(item.output) for item in request.items if isinstance(item, ToolResultItem)]


async def test_assist_is_settable_by_tool_and_panel_and_undoable(env: Env) -> None:
    conversation = await _assist_chat(env)
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "assist")
    assert (await conversation_row(env)).mode == "assist"
    shown = env.sender.edits[-1]
    assert "<b>Assist is on</b> for the chat with Kamola." in shown["text"]
    assert "Nothing is sent without your approval." in shown["text"]
    assert panels.ASSIST_PANEL_LINE in shown["text"]
    assert keyboard_labels(shown["reply_markup"]) == [
        "Observe",
        "Ignore",
        "Assist ✓",
        "Delete stored data…",
    ]
    assert env.sender.answered_callbacks[-1]["text"] == "Assist is on"
    await tap(env, panels.PANEL_CARD, "assist")
    assert "already in Assist" in env.sender.edits[-1]["text"]
    await tap(env, panels.PANEL_CARD, "observe")
    assert (await conversation_row(env)).mode == "observe"

    # The tool, with its one-tap Undo.
    turn = await ask(env, "Turn on drafts for Kamola's chat")
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "set_conversation_mode",
                args(conversation_id=conversation, contact_id=None, mode="assist"),
            )
        ),
        text_response("Done."),
    )
    channel = FakeChannel()
    await runtime(env, fake, channel, flags=MEMORY_FLAGS).run(turn)
    [result] = _results(fake.requests[1])
    assert (result["status"], result["previous_mode"], result["mode"]) == (
        "ok",
        "observe",
        "assist",
    )
    assert result["effect"] == MODE_EFFECT["assist"]
    assert (await conversation_row(env)).mode == "assist"
    [button] = channel.buttons[0]
    assert button.label == "Undo"
    await deliver(env, callback(button.callback_data))
    assert (await conversation_row(env)).mode == "observe"


async def test_rg11_an_injected_result_holds_a_set_mode_assist_call_for_a_tap(env: Env) -> None:
    conversation = await _assist_chat(env)
    turn = await ask(env, "What did Kamola say?")
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "get_conversation",
                args(conversation_id=conversation, contact_id=None, cursor=None, limit=None),
            )
        ),
        tool_response(
            (
                "c2",
                "set_conversation_mode",
                args(conversation_id=conversation, contact_id=None, mode="assist"),
            )
        ),
        text_response("Asked you."),
    )
    await runtime(env, fake, FakeChannel(), flags=READ_FLAGS | MEMORY_FLAGS).run(turn)
    read, held = _results(fake.requests[2])
    assert read["status"] == "ok"
    assert held["status"] == "awaiting_confirmation"
    assert (await conversation_row(env)).mode == "observe"
    async with env.factory() as session:
        [confirmation] = (await session.scalars(select(ToolConfirmation))).all()
    assert confirmation.tool_name == "set_conversation_mode"


async def test_leaving_assist_clears_the_draft_flag(env: Env) -> None:
    conversation = await _assist_chat(env)
    account_id = (await conversation_row(env)).account_id
    for leave_to in ("observe", "ignore"):
        async with transaction(env.factory) as uow:
            await modes.set_mode(
                uow.session,
                account_id=account_id,
                conversation_id=conversation,
                mode="assist",
                actor="owner",
                clock=env.clock,
            )
            row = await uow.session.get(Conversation, conversation)
            assert row is not None
            row.pending_draft_at = env.clock.now()
        async with transaction(env.factory) as uow:
            await modes.set_mode(
                uow.session,
                account_id=account_id,
                conversation_id=conversation,
                mode=leave_to,
                actor="owner",
                clock=env.clock,
            )
        assert (await conversation_row(env)).pending_draft_at is None


async def test_switching_to_ignore_cancels_pending_actions(env: Env) -> None:
    conversation = await _assist_chat(env)
    account_id = (await conversation_row(env)).account_id
    states = ("awaiting_approval", "approved", "executing", "succeeded", "delivery_uncertain")
    async with env.factory() as session, session.begin():
        ids = [
            (
                await seed_action(
                    session,
                    account_id=account_id,
                    conversation_id=conversation,
                    state=state,
                    version=1,
                    approval_token="synthtoken" if state == "awaiting_approval" else None,
                )
            ).id
            for state in states
        ]
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "ignore")
    await tap(env, panels.IGNORE_CARD, "confirm")
    async with env.factory() as session:
        rows = {r.id: r for r in await session.scalars(select(Action).where(Action.id.in_(ids)))}
        cancelled = (
            await session.scalars(
                select(AuditLogEntry.subject_id).where(AuditLogEntry.event == "action_cancelled")
            )
        ).all()
    assert [(rows[i].state, rows[i].state_reason) for i in ids] == [
        ("cancelled", "ignore_mode"),
        ("cancelled", "ignore_mode"),
        ("executing", None),
        ("succeeded", None),
        ("delivery_uncertain", None),
    ]
    assert rows[ids[0]].approval_token is None
    assert sorted(cancelled) == sorted(ids[:2])


async def test_the_default_mode_for_new_chats_cannot_be_assist(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/settings"))
    ops = keyboard_ops(env.sender.sends[-1]["reply_markup"])
    assert {"dobserve", "dignore"} <= set(ops)
    assert "dassist" not in ops
    await tap(env, panels.SETTINGS_CARD, "dassist")  # a forged op
    assert env.sender.answered_callbacks[-1]["text"] == "This button is no longer active."
    async with env.factory() as session:
        account = (await session.execute(select(Account))).scalar_one()
    assert preferences.default_mode(account) == "observe"
    with pytest.raises(preferences.PreferenceError):
        preferences.check_default_mode("assist")
    with pytest.raises(ValidationError):
        UpdatePreferencesArgs.model_validate(
            {"preferred_language": None, "timezone": None, "default_mode": "assist"}
            | S035_PREFS_UNCHANGED
        )
    assert preferences.SETTABLE_DEFAULT_MODES == ("observe", "ignore")
