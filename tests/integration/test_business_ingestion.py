"""Business ingestion end to end through the inbox (S02.2, ARCHITECTURE §6.5).

Real processor, real router, real pipeline, real Postgres; the connection
lookup and the sender are fakes. Covers the S02.2 test list
(IMPLEMENTATION_PLAN §4) and the named regression test RG-02.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import AuditLogEntry
from assistant.conversations.models import Contact, Conversation, CoverageGap, Message
from assistant.inbox.processor import DeterministicError, requeue_failed
from tests.integration.business_env import (
    BOT_ID,
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    connect,
    contact,
    conversation,
    count,
    deleted_messages,
    deliver,
    gaps,
    inbound,
    message,
    messages,
    payload_is_sql_null,
    private_message,
)

pytestmark = pytest.mark.integration


async def _set_mode(env: Env, mode: str, chat_id: int = CONTACT) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = :mode WHERE business_chat_id = :chat"),
            {"mode": mode, "chat": chat_id},
        )


# ------------------------------------------------------------ RG-02
@pytest.mark.parametrize(
    ("chat_id", "sender_is_bot"),
    [(BOT_ID, False), (OWNER, False), (777000, False), (CONTACT, True)],
    ids=["bot chat mirror", "saved messages", "service chat", "bot counterpart"],
)
async def test_rg02_mirrored_bot_chats_are_ignored(
    env: Env, chat_id: int, sender_is_bot: bool
) -> None:
    """RG-02 (never weaken): a chat with a bot, Saved Messages, a service chat
    or a bot counterpart creates no contact, no conversation, no message, and
    keeps no payload."""
    await connect(env)
    update = business_message(chat_id=chat_id, from_id=chat_id, sender_is_bot=sender_is_bot)
    await deliver(env, update)

    row = await inbound(env, update)
    assert row.status == "ignored"
    assert await payload_is_sql_null(env, update)
    assert await count(env, Contact) == 0
    assert await count(env, Conversation) == 0
    assert await count(env, Message) == 0


# ------------------------------------------------------------ third-party bot chats
BOT_CHAT = 7000000007


def _bot_reply(**kwargs: Any) -> dict[str, Any]:
    return business_message(
        chat_id=BOT_CHAT,
        from_id=BOT_CHAT,
        sender_is_bot=True,
        message_text="synthetic bot reply",
        **kwargs,
    )


async def test_a_bot_answer_marks_the_chat_and_purges_what_was_stored(env: Env) -> None:
    """FR-OBS-04: a private `Chat` has no `is_bot`, so the owner's first
    messages to a third-party bot are stored like any chat. The bot's first
    answer reveals it: the contact becomes a bot, the conversation goes to
    Ignore, and everything stored from it is purged (§6.5)."""
    await connect(env)
    first = business_message(
        chat_id=BOT_CHAT, from_id=OWNER, message_text="synthetic /start", username="synthbot"
    )
    await deliver(env, first)
    assert len(await messages(env, BOT_CHAT)) == 1

    reply = _bot_reply()
    await deliver(env, reply)

    row = await inbound(env, reply)
    assert row.status == "ignored"
    assert await payload_is_sql_null(env, reply)
    assert await payload_is_sql_null(env, first)
    assert await messages(env, BOT_CHAT) == []
    bot = await contact(env, BOT_CHAT)
    assert bot is not None
    assert bot.is_bot is True
    assert (bot.display_name, bot.username) == (None, None)
    conv = await conversation(env, BOT_CHAT)
    assert conv is not None
    assert conv.mode == "ignore"
    assert conv.pending_ai_since is None
    async with env.factory() as session:
        audits = (
            (await session.execute(select(AuditLogEntry).where(AuditLogEntry.event == "bot_chat")))
            .scalars()
            .all()
        )
    assert len(audits) == 1


async def test_later_messages_in_a_known_bot_chat_are_ignored(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(chat_id=BOT_CHAT, from_id=OWNER, message_text="a"))
    await deliver(env, _bot_reply())
    await _set_mode(env, "observe", BOT_CHAT)  # even if the mode is changed later

    later = business_message(chat_id=BOT_CHAT, from_id=OWNER, message_text="synthetic b")
    edit = business_message(chat_id=BOT_CHAT, from_id=OWNER, message_text="c", edit=True)
    deletion = deleted_messages(chat_id=BOT_CHAT, message_ids=[1, 2])
    for update in (later, edit, deletion):
        await deliver(env, update)
        assert (await inbound(env, update)).status == "ignored"
        assert await payload_is_sql_null(env, update)
    assert await messages(env, BOT_CHAT) == []
    conv = await conversation(env, BOT_CHAT)
    assert conv is not None
    assert conv.last_owner_msg_at is not None  # only the first, pre-recognition message


async def test_a_bot_answer_does_not_touch_other_chats(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_text="synthetic contact"))
    await deliver(env, business_message(chat_id=BOT_CHAT, from_id=OWNER, message_text="a"))
    await deliver(env, _bot_reply())

    assert len(await messages(env, CONTACT)) == 1
    person = await contact(env, CONTACT)
    assert person is not None
    assert person.is_bot is False
    assert person.display_name == "Synthetic"


async def test_a_bot_sender_that_is_not_the_counterpart_does_not_mark_the_chat(
    env: Env,
) -> None:
    """Only the chat's own counterpart reveals a bot chat: a private chat is
    the conversation with `chat.id`, so a bot-flagged sender with another id
    (`from.id != chat.id`) proves nothing about the contact and must not purge
    what was stored from them."""
    await connect(env)
    await deliver(env, business_message(message_text="synthetic contact"))
    stray = business_message(
        chat_id=CONTACT, from_id=BOT_CHAT, sender_is_bot=True, message_text="synthetic"
    )
    await deliver(env, stray)

    row = await inbound(env, stray)
    assert row.status == "ignored"
    assert await payload_is_sql_null(env, stray)
    assert len(await messages(env, CONTACT)) == 1
    person = await contact(env, CONTACT)
    assert person is not None
    assert person.is_bot is False
    conv = await conversation(env, CONTACT)
    assert conv is not None
    assert conv.mode != "ignore"


async def test_a_bot_answer_on_a_stopped_or_foreign_connection_changes_nothing(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(chat_id=BOT_CHAT, from_id=OWNER, message_text="a"))
    foreign = _bot_reply(connection_id="synthetic-unknown")
    await deliver(env, foreign)

    assert (await inbound(env, foreign)).status == "ignored"
    assert await payload_is_sql_null(env, foreign)
    assert len(await messages(env, BOT_CHAT)) == 1
    bot = await contact(env, BOT_CHAT)
    assert bot is not None
    assert bot.is_bot is False


# ------------------------------------------------------------ storage
async def test_a_contact_message_creates_contact_conversation_and_message(env: Env) -> None:
    await connect(env)
    update = business_message(message_text="synthetic hello", username="syn")
    await deliver(env, update)

    row = await inbound(env, update)
    assert row.status == "processed"
    assert row.payload is not None  # live traffic keeps its payload until the sweep
    person = await contact(env)
    assert person is not None
    assert (person.display_name, person.username) == ("Synthetic", "syn")
    conv = await conversation(env)
    assert conv is not None
    assert conv.mode == "observe"
    assert conv.last_contact_msg_at == datetime.fromtimestamp(T_BASE, tz=UTC)
    assert conv.coverage_started_at == conv.last_contact_msg_at
    assert conv.pending_ai_since is not None
    stored = (await messages(env))[0]
    assert stored.direction == "contact"
    assert stored.text == "synthetic hello"
    assert stored.search_key == "synthetic hello"
    assert stored.content_hash is not None
    assert row.message_id == stored.id


async def test_an_owner_message_is_owner_manual_and_sets_takeover(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(from_id=OWNER, message_text="synthetic reply"))

    stored = (await messages(env))[0]
    assert stored.direction == "owner_manual"
    conv = await conversation(env)
    assert conv is not None
    assert conv.last_owner_msg_at is not None
    assert conv.last_contact_msg_at is None
    assert conv.autonomy_paused_reason == "takeover"
    # The counterpart is still the contact, never the owner.
    assert (await contact(env, OWNER)) is None
    assert (await contact(env, CONTACT)) is not None


async def test_a_new_owner_message_sets_takeover_once(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(from_id=OWNER, message_text="one"))
    await deliver(env, business_message(from_id=OWNER, message_text="two"))

    async with env.factory() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.event == "takeover_detected")
                )
            )
            .scalars()
            .all()
        )
    assert len(audits) == 1


async def test_a_redelivered_message_is_stored_once(env: Env) -> None:
    await connect(env)
    first = business_message(message_id=500, message_text="synthetic once")
    again = business_message(message_id=500, message_text="synthetic once")
    await deliver(env, first)
    await deliver(env, again)

    assert len(await messages(env)) == 1
    assert (await inbound(env, again)).status == "processed"


async def test_a_message_before_its_connection_update_is_stored(env: Env) -> None:
    """The initial connection update can be missing (TF §2): the preparer
    bootstraps it and the message is stored, not dropped."""
    from tests.integration.business_env import CONNECTION, connection_body, onboard

    await onboard(env)
    env.lookup.known[CONNECTION] = connection_body()
    update = business_message(message_text="synthetic early")
    await deliver(env, update)

    assert env.lookup.calls == [CONNECTION]
    assert (await inbound(env, update)).status == "processed"
    assert len(await messages(env)) == 1


async def test_traffic_on_a_rotated_id_is_stored_for_the_owner(env: Env) -> None:
    from tests.integration.business_env import connection_update

    await connect(env, connection_id="synthetic-old")
    await deliver(env, connection_update(connection_id="synthetic-old", enabled=False, rights=None))
    await deliver(env, connection_update(connection_id="synthetic-new", date=1792360000))
    late = business_message("synthetic-old", message_text="synthetic late")
    await deliver(env, late)

    row = await inbound(env, late)
    assert row.status == "processed"
    assert len(await messages(env)) == 1


# ------------------------------------------------------------ edits
async def test_an_edit_with_changed_content_overwrites_and_marks(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=600, message_text="synthetic v1"))
    await deliver(env, business_message(message_id=600, message_text="synthetic v2", edit=True))

    stored = await message(env, 600)
    assert stored is not None
    assert stored.text == "synthetic v2"
    assert stored.edited_at is not None
    assert len(await messages(env)) == 1


async def test_an_edit_with_unchanged_content_changes_nothing(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=601, message_text="synthetic same"))
    before = await message(env, 601)
    assert before is not None
    await deliver(env, business_message(message_id=601, message_text="synthetic same", edit=True))

    after = await message(env, 601)
    assert after is not None
    assert after.edited_at is None
    assert after.updated_at == before.updated_at


async def test_a_contact_edit_does_not_move_the_window(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=602, date=T_BASE, message_text="synthetic a"))
    await deliver(
        env,
        business_message(
            message_id=602,
            date=T_BASE,
            edit=True,
            edit_date=T_BASE + 7200,
            message_text="synthetic b",
        ),
    )
    conv = await conversation(env)
    assert conv is not None
    assert conv.last_contact_msg_at == datetime.fromtimestamp(T_BASE, tz=UTC)


async def test_an_edit_of_an_unseen_old_message_is_stored_without_state_change(env: Env) -> None:
    """TF §3.2: edits of pre-connection messages arrive. They are kept as
    edits and never set the window, the owner timestamps or takeover."""
    await connect(env)
    await deliver(
        env, business_message(from_id=OWNER, message_id=50, date=T_BASE - 86400, edit=True)
    )
    conv = await conversation(env)
    assert conv is not None
    assert conv.last_owner_msg_at is None
    assert conv.autonomy_paused_reason is None
    stored = await message(env, 50)
    assert stored is not None
    assert stored.observed_via == "edit"


async def test_an_owner_no_op_edit_does_not_set_takeover(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=603, message_text="synthetic c"))
    await deliver(
        env, business_message(from_id=OWNER, message_id=603, message_text="synthetic c", edit=True)
    )
    conv = await conversation(env)
    assert conv is not None
    assert conv.autonomy_paused_reason is None


# ------------------------------------------------------------ deletions
async def test_a_deletion_purges_content_and_keeps_a_tombstone(env: Env) -> None:
    await connect(env)
    original = business_message(message_id=700, message_text="synthetic gone")
    await deliver(env, original)
    await deliver(env, deleted_messages(message_ids=[700]))

    stored = await message(env, 700)
    assert stored is not None
    assert stored.text is None
    assert stored.search_key is None
    assert stored.content_hash is None
    assert stored.deleted_at is not None
    assert stored.direction == "contact"  # the tombstone keeps who and when
    assert await payload_is_sql_null(env, original)


async def test_an_edit_after_deletion_does_not_restore_content(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=701, message_text="synthetic x"))
    await deliver(env, deleted_messages(message_ids=[701]))
    await deliver(env, business_message(message_id=701, message_text="synthetic y", edit=True))

    stored = await message(env, 701)
    assert stored is not None
    assert stored.text is None


async def test_a_deletion_of_an_unseen_id_leaves_a_tombstone(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_text="synthetic to create the chat"))
    await deliver(env, deleted_messages(message_ids=[999]))

    stored = await message(env, 999)
    assert stored is not None
    assert stored.observed_via == "deletion"
    assert stored.direction is None
    # ...and a late (requeued) original can no longer store content.
    await deliver(env, business_message(message_id=999, message_text="synthetic late original"))
    stored = await message(env, 999)
    assert stored is not None
    assert stored.text is None


async def test_deleting_a_message_clears_every_linked_inbound_payload(env: Env) -> None:
    await connect(env)
    original = business_message(message_id=702, message_text="synthetic v1")
    edit = business_message(message_id=702, message_text="synthetic v2", edit=True)
    await deliver(env, original)
    await deliver(env, edit)
    # An update for the same message that never became a message row (it is
    # still pending here, so the content sits only in its payload).
    unlinked = business_message(message_id=702, message_text="synthetic v3", edit=True)
    async with env.factory() as session, session.begin():
        from assistant.telegram.ingress.writer import write_batch

        await write_batch(session, bot_id=BOT_ID, updates=[unlinked])
    await deliver(env, deleted_messages(message_ids=[702]))

    assert await payload_is_sql_null(env, original)
    assert await payload_is_sql_null(env, edit)
    assert await payload_is_sql_null(env, unlinked)


async def test_a_deletion_listing_the_same_id_twice_is_processed_once(env: Env) -> None:
    """A repeated id used to insert two tombstones for one message and hit the
    unique key, dead-lettering the deletion (S02.6)."""
    await connect(env)
    await deliver(env, business_message(message_id=703, message_text="synthetic dup"))
    update = deleted_messages(message_ids=[703, 703, 998, 998])
    await deliver(env, update)

    assert (await inbound(env, update)).status == "processed"
    ids = [m.telegram_message_id for m in await messages(env)]
    assert sorted(ids) == [703, 998]
    assert all(m.deleted_at is not None for m in await messages(env))


async def test_a_dead_lettered_deletion_also_clears_unlinked_payloads(env: Env) -> None:
    """The fallback purge for a deletion that cannot be parsed must reach the
    same places as the normal path, including updates that never became
    message rows (§6.2)."""
    await connect(env)
    await deliver(env, business_message(message_id=704, message_text="synthetic v1"))
    unlinked = business_message(message_id=704, message_text="synthetic v2", edit=True)
    async with env.factory() as session, session.begin():
        from assistant.telegram.ingress.writer import write_batch

        await write_batch(session, bot_id=BOT_ID, updates=[unlinked])
        # Set aside earlier (a dead letter of its own): it never became a row.
        await session.execute(
            text(
                "UPDATE app.inbound_updates SET status = 'failed', account_id = "
                "(SELECT id FROM app.accounts) WHERE update_id = :id"
            ),
            {"id": unlinked["update_id"]},
        )
    poison = deleted_messages(message_ids=[704, "not-a-number"])  # type: ignore[list-item]
    for _ in range(3):
        await deliver(env, poison)

    assert (await inbound(env, poison)).status == "failed"
    stored = await message(env, 704)
    assert stored is not None
    assert stored.text is None
    assert await payload_is_sql_null(env, unlinked)


async def test_an_ignore_mode_deletion_keeps_no_payload(env: Env) -> None:
    """A deletion's payload names the contact (`chat.first_name`): in an
    Ignore-mode chat it is not ours to keep (§6.5 last paragraph)."""
    await connect(env)
    await deliver(env, business_message(message_id=705, message_text="synthetic"))
    await _set_mode(env, "ignore")
    update = deleted_messages(message_ids=[705])
    await deliver(env, update)

    assert (await inbound(env, update)).status == "processed"
    assert await payload_is_sql_null(env, update)


async def test_a_deletion_for_an_unknown_chat_stores_nothing(env: Env) -> None:
    await connect(env)
    update = deleted_messages(chat_id=OTHER_CONTACT, message_ids=[1])
    await deliver(env, update)
    assert (await inbound(env, update)).status == "ignored"
    assert await count(env, Conversation) == 0


# ------------------------------------------------------------ Ignore mode
async def test_ignore_mode_stores_metadata_only(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_text="synthetic first"))
    await _set_mode(env, "ignore")
    update = business_message(message_text="synthetic private", username="syn")
    await deliver(env, update)

    stored = await message(env, update["business_message"]["message_id"])
    assert stored is not None
    assert stored.direction == "contact"
    assert stored.sent_at is not None
    assert stored.text is None
    assert stored.content_hash is None
    assert await payload_is_sql_null(env, update)
    person = await contact(env)
    assert person is not None
    assert person.username is None  # names are content too


async def test_ignore_mode_still_tracks_the_window(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_text="synthetic first"))
    await _set_mode(env, "ignore")
    await deliver(env, business_message(date=T_BASE + 3600, message_text="synthetic later"))
    conv = await conversation(env)
    assert conv is not None
    assert conv.last_contact_msg_at == datetime.fromtimestamp(T_BASE + 3600, tz=UTC)


# ------------------------------------------------------------ backlog timing
async def test_a_three_hour_backlog_sets_the_window_from_message_dates(env: Env) -> None:
    """Delivered late is not sent late: `last_contact_msg_at` is Telegram's
    `date`, never receipt time."""
    await connect(env)
    env.clock.advance(hours=12)  # receipt long after the message date
    sent = T_BASE + 60
    await deliver(env, business_message(date=sent, message_text="synthetic delayed"))
    conv = await conversation(env)
    assert conv is not None
    assert conv.last_contact_msg_at == datetime.fromtimestamp(sent, tz=UTC)
    assert conv.last_contact_msg_at < env.clock.now()


async def test_out_of_order_delivery_keeps_the_newest_date(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=801, date=T_BASE + 600, message_text="b"))
    await deliver(env, business_message(message_id=800, date=T_BASE, message_text="a"))
    conv = await conversation(env)
    assert conv is not None
    assert conv.last_contact_msg_at == datetime.fromtimestamp(T_BASE + 600, tz=UTC)
    assert conv.coverage_started_at == datetime.fromtimestamp(T_BASE, tz=UTC)


# ------------------------------------------------------------ dead letters
async def test_a_dead_lettered_business_update_pauses_the_chat_and_records_a_gap(env: Env) -> None:
    """§6.2: a business update that will never process pauses automation for
    its chat, records a definite gap, and `/status` says so (§6 invariant 11)."""
    await connect(env)
    await deliver(env, business_message(message_text="synthetic fine"))
    poison = business_message(message_text="synthetic poison", extra={"message_id": None})
    for _ in range(3):
        await deliver(env, poison)

    row = await inbound(env, poison)
    assert row.status == "failed"
    assert row.error_code == "NormalizeError"
    conv = await conversation(env)
    assert conv is not None
    assert conv.autonomy_paused_reason == "ingest_error"
    recorded = await gaps(env)
    assert len(recorded) == 1
    assert (recorded[0].kind, recorded[0].certainty) == ("dead_letter", "definite")
    assert recorded[0].inbound_update_id == row.id
    assert recorded[0].resolved_at is None

    await deliver(env, private_message(OWNER, "/status"))
    status_text = env.sender.texts[-1]
    assert "could not be processed" in status_text
    assert "definite" in status_text


async def test_requeue_after_a_fix_clears_the_caveat(env: Env) -> None:
    await connect(env)
    poison = business_message(message_text="synthetic poison", extra={"message_id": None})
    for _ in range(3):
        await deliver(env, poison)
    assert (await inbound(env, poison)).status == "failed"

    # "The fix": repair the payload the way a code fix would make it parse.
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE app.inbound_updates SET payload = jsonb_set(payload, "
                "'{business_message,message_id}', '4242') WHERE update_id = :id"
            ),
            {"id": poison["update_id"]},
        )
    assert (await requeue_failed(env.factory)).reset == 1
    await env.processor.process_batch()

    assert (await inbound(env, poison)).status == "processed"
    conv = await conversation(env)
    assert conv is not None
    assert conv.autonomy_paused_reason is None
    assert all(gap.resolved_at is not None for gap in await gaps(env))
    await deliver(env, private_message(OWNER, "/status"))
    assert "could not be processed" not in env.sender.texts[-1]


async def test_a_dead_letter_hook_never_creates_rows_for_a_foreign_connection(env: Env) -> None:
    await connect(env)
    foreign = business_message("synthetic-foreign", extra={"message_id": None})
    env.lookup.known["synthetic-foreign"] = {
        "id": "synthetic-foreign",
        "user": {"id": 7000000099, "is_bot": False},
        "user_chat_id": 7000000099,
        "date": 1792350000,
        "is_enabled": True,
    }
    await deliver(env, foreign)
    assert (await inbound(env, foreign)).status == "rejected"
    assert await count(env, CoverageGap) == 0
    assert await count(env, Conversation) == 0


@pytest.mark.parametrize("state", ["stopped", "disabled"])
async def test_a_dead_letter_creates_no_rows_when_storage_is_off(env: Env, state: str) -> None:
    """Fail closed (§6 invariant 7): with the owner's "stop observing" choice,
    or a disabled connection, a dead-lettered update records nothing."""
    from tests.integration.business_env import connection_update

    await connect(env)
    if state == "stopped":
        async with env.factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE app.accounts SET settings = settings || "
                    '\'{"managed_mode": "stopped"}\'::jsonb'
                )
            )
    else:
        await deliver(env, connection_update(enabled=False, rights=None))
    original = env.processor._handlers

    async def explode(session, row, decision):  # type: ignore[no-untyped-def]
        raise DeterministicError("synthetic bug")

    env.processor._handlers = {**original, list(original)[3]: explode}
    try:
        poison = business_message(message_text="synthetic poison")
        for _ in range(3):
            await deliver(env, poison)
    finally:
        env.processor._handlers = original

    assert (await inbound(env, poison)).status == "failed"
    assert await count(env, Contact) == 0
    assert await count(env, Conversation) == 0
    assert await count(env, CoverageGap) == 0


async def test_a_handler_bug_on_a_live_message_dead_letters_with_a_gap(env: Env) -> None:
    """Not only parse errors: any deterministic failure in the pipeline ends
    in the same honest state."""
    await connect(env)
    original = env.processor._handlers

    async def explode(session, row, decision):  # type: ignore[no-untyped-def]
        raise DeterministicError("synthetic bug")

    env.processor._handlers = {**original, list(original)[3]: explode}
    try:
        update = business_message(message_text="synthetic")
        for _ in range(3):
            await deliver(env, update)
    finally:
        env.processor._handlers = original
    assert (await inbound(env, update)).status == "failed"
    conv = await conversation(env)
    assert conv is not None
    assert conv.autonomy_paused_reason == "ingest_error"
    assert (await gaps(env))[0].at_risk_from == datetime.fromtimestamp(T_BASE, tz=UTC)
