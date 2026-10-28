"""The gateway's business methods (S04.4 §3.1; ARCHITECTURE §6.8).

The resolver reads the database, so these run against the real Postgres
with `FakeTelegramSender`: what WOULD have reached Telegram is recorded, with
every argument. A refused call must record nothing at all (§6 invariant 7).
Texts are synthetic.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy import update

from assistant.accounts.models import BusinessConnection
from assistant.config import Settings
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.delivery import ErrorClass
from assistant.observability.logging import configure_logging
from assistant.telegram.business_targets import BusinessTargetResolver
from assistant.telegram.gateway import LOCAL_TARGET_REFUSED, OutboundGateway
from tests.fakes.clock import FakeClock, FakeSleep
from tests.fakes.sender import FakeTelegramSender, network_error, telegram_error
from tests.integration.actions_env import Chat, add_message, seed_chat
from tests.integration.business_env import NOW, Env

pytestmark = pytest.mark.integration

CANARY = "CANARY-SYNTH-b41e"
TOKEN = "8123456789:AAH1bQ7xKqY-ZZ_pLm3nOpQrStUvWxYz012"


def gateway_for(env: Env, sender: FakeTelegramSender, sleep: FakeSleep | None = None) -> Any:
    return OutboundGateway(
        sender,
        FakeClock(NOW),
        sleep=sleep or FakeSleep(),
        targets=BusinessTargetResolver(env.factory),
    )


async def seed(env: Env, **fields: Any) -> Chat:
    async with env.factory() as session, session.begin():
        return await seed_chat(session, NOW, **fields)


async def chat_id_of(env: Env, chat: Chat) -> int:
    async with env.factory() as session:
        row = await session.get(Conversation, chat.conversation_id)
        assert row is not None
        return row.business_chat_id


async def set_connection(env: Env, chat: Chat, **values: Any) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(
            update(BusinessConnection)
            .where(BusinessConnection.account_id == chat.account_id)
            .values(**values)
        )


async def test_business_send_attaches_the_current_connection_id(env: Env) -> None:
    """Resolved at call time, never captured earlier (TF §12 LV-05: a
    reconnect rotates the id); the chat is the conversation's own, in a
    two-account fixture."""
    first = await seed(env)
    second = await seed(env)
    sender = FakeTelegramSender()
    gateway = gateway_for(env, sender)
    await set_connection(env, first, connection_id="synthetic-rotated")

    result = await gateway.send_business_text(
        account_id=first.account_id, conversation_id=first.conversation_id, text="Synthetic"
    )
    other = await gateway.send_business_text(
        account_id=second.account_id, conversation_id=second.conversation_id, text="Synthetic"
    )

    assert result.ok
    assert other.ok
    assert sender.plain_sends[0]["business_connection_id"] == "synthetic-rotated"
    assert sender.plain_sends[0]["chat_id"] == await chat_id_of(env, first)
    assert sender.plain_sends[1]["chat_id"] == await chat_id_of(env, second)
    assert sender.plain_sends[1]["business_connection_id"] != "synthetic-rotated"
    assert sender.sends == []  # never the HTML control path


async def test_business_send_is_plain_text_and_never_split(env: Env) -> None:
    """One `sendMessage` with exactly the text, markup and all, however long:
    the gateway never splits and never interprets it (ARCHITECTURE §6.8)."""
    chat = await seed(env)
    sender = FakeTelegramSender()
    text = "<b>not bold</b> & " + "x" * 5000

    await gateway_for(env, sender).send_business_text(
        account_id=chat.account_id, conversation_id=chat.conversation_id, text=text
    )

    assert [call["text"] for call in sender.plain_sends] == [text]
    assert sender.sends == []


@pytest.mark.parametrize(
    "state",
    ["stale", "disabled", "pending_owner", "not_enabled", "no_reply_right", "missing", "bot_chat"],
)
async def test_business_send_refuses_a_disabled_or_stale_connection_without_calling_telegram(
    env: Env, state: str
) -> None:
    chat = await seed(env, connection=state != "missing")
    if state in {"stale", "disabled", "pending_owner"}:
        await set_connection(env, chat, status=state)
    elif state == "not_enabled":
        await set_connection(env, chat, is_enabled=False)
    elif state == "no_reply_right":
        # Viro on #107: the current connection may be a replacement whose
        # rights Tx A never saw; without `can_reply` nothing is sent.
        await set_connection(env, chat, rights=["can_read_messages"])
    elif state == "bot_chat":
        async with env.factory() as session, session.begin():
            conversation = await session.get(Conversation, chat.conversation_id)
            assert conversation is not None
            await session.execute(
                update(Contact).where(Contact.id == conversation.contact_id).values(is_bot=True)
            )
    sender = FakeTelegramSender()

    result = await gateway_for(env, sender).send_business_text(
        account_id=chat.account_id, conversation_id=chat.conversation_id, text="Synthetic"
    )

    assert result.error_class is ErrorClass.BAD_REQUEST
    assert result.error_code == LOCAL_TARGET_REFUSED
    assert sender.business_calls == 0
    assert sender.sends == []


async def test_business_send_refuses_another_accounts_conversation_without_calling_telegram(
    env: Env,
) -> None:
    mine = await seed(env)
    theirs = await seed(env)
    sender = FakeTelegramSender()

    result = await gateway_for(env, sender).send_business_text(
        account_id=mine.account_id, conversation_id=theirs.conversation_id, text="Synthetic"
    )

    assert result.error_code == LOCAL_TARGET_REFUSED
    assert sender.business_calls == 0


async def test_business_send_returns_message_id_zero_as_is(env: Env) -> None:
    """`message_id == 0` is Telegram's "pending identity" (ARCHITECTURE §6.8):
    a success, reported as given — the executor decides what it means."""
    chat = await seed(env)
    sender = FakeTelegramSender()
    sender.plain_script = [0]

    result = await gateway_for(env, sender).send_business_text(
        account_id=chat.account_id, conversation_id=chat.conversation_id, text="Synthetic"
    )

    assert result.ok
    assert result.message_id == 0


async def test_business_send_reports_the_connection_id_it_used(env: Env) -> None:
    """Antony #1 on #107: the executor may stale only the connection a
    failed call was sent on, so the send names it (resolved at call time);
    a call refused locally used none."""
    chat = await seed(env)
    other = await seed(env)
    sender = FakeTelegramSender()
    gateway = gateway_for(env, sender)
    sender.plain_script = [
        telegram_error("Bad Request: BUSINESS_CONNECTION_INVALID", status_code=400)
    ]
    await set_connection(env, chat, connection_id="synthetic-current")

    failed, used = await gateway.send_business_text_with_connection(
        account_id=chat.account_id, conversation_id=chat.conversation_id, text="Synthetic"
    )
    refused, none = await gateway.send_business_text_with_connection(
        account_id=chat.account_id, conversation_id=other.conversation_id, text="Synthetic"
    )

    assert failed.error_class is ErrorClass.CONNECTION_INVALID
    assert used == "synthetic-current" == sender.plain_sends[0]["business_connection_id"]
    assert refused.error_code == LOCAL_TARGET_REFUSED
    assert none is None
    assert len(sender.plain_sends) == 1


@pytest.fixture
def json_logs(clean_env: None, capsys: pytest.CaptureFixture[str]) -> pytest.CaptureFixture[str]:
    configure_logging(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url="postgresql+psycopg://user:pw@db:5432/assistant",
            log_format="json",
        )
    )
    return capsys


async def test_business_send_never_logs_text_or_urls(
    env: Env, json_logs: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """RG-10 on the business path: success, a Telegram refusal and a network
    error whose message holds the Bot API URL all log ids and codes only."""
    caplog.set_level(logging.DEBUG)
    chat = await seed(env)
    sender = FakeTelegramSender()
    gateway = gateway_for(env, sender)
    sender.plain_script = [
        telegram_error(f"Bad Request: {CANARY}", status_code=400),
        network_error(f"https://api.telegram.org/bot{TOKEN}/sendMessage {CANARY}"),
    ]
    for _ in range(3):
        await gateway.send_business_text(
            account_id=chat.account_id, conversation_id=chat.conversation_id, text=CANARY
        )

    output = json_logs.readouterr().out
    assert "business_message_sent" in output
    assert "business_call_failed" in output
    assert CANARY not in output
    assert TOKEN not in output
    assert "/bot" not in output
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.__dict__}"
        assert CANARY not in rendered
        assert TOKEN not in rendered


async def test_rg12_gateway_refuses_editing_or_deleting_a_non_assistant_message(
    env: Env,
) -> None:
    """Gateway half of RG-12 (invariant 14): only a live `assistant_sent` row
    of this conversation, with a known Telegram id, may be edited or deleted.
    Everything else is refused with no Telegram call."""
    chat = await seed(env)
    other = await seed(env)
    async with env.factory() as session, session.begin():
        ids = {
            "owner": await add_message(
                session,
                chat.account_id,
                chat.conversation_id,
                direction="owner_manual",
                sent_at=NOW,
            ),
            "contact": chat.contact_message_id,
            "deleted_own": await add_message(
                session,
                chat.account_id,
                chat.conversation_id,
                direction="assistant_sent",
                sent_at=NOW,
                deleted=True,
            ),
            "pending_identity": await add_message(
                session,
                chat.account_id,
                chat.conversation_id,
                direction="assistant_sent",
                sent_at=NOW,
            ),
            "other_chat": await add_message(
                session,
                other.account_id,
                other.conversation_id,
                direction="assistant_sent",
                sent_at=NOW,
            ),
        }
        own = await add_message(
            session, chat.account_id, chat.conversation_id, direction="assistant_sent", sent_at=NOW
        )
        # A send whose Telegram id is still pending (`message_id == 0`).
        await session.execute(
            update(Message)
            .where(Message.id == ids["pending_identity"])
            .values(telegram_message_id=None)
        )
    sender = FakeTelegramSender()
    gateway = gateway_for(env, sender)

    for name, ref in ids.items():
        edit = await gateway.edit_assistant_message(
            account_id=chat.account_id,
            conversation_id=chat.conversation_id,
            assistant_message_ref=ref,
            text="Synthetic",
        )
        delete = await gateway.delete_own_business_messages(
            account_id=chat.account_id,
            conversation_id=chat.conversation_id,
            assistant_message_refs=[own, ref],
        )
        assert edit.error_code == LOCAL_TARGET_REFUSED, name
        assert delete.error_code == LOCAL_TARGET_REFUSED, name
    empty = await gateway.delete_own_business_messages(
        account_id=chat.account_id, conversation_id=chat.conversation_id, assistant_message_refs=[]
    )
    assert empty.error_code == LOCAL_TARGET_REFUSED
    assert sender.business_calls == 0

    edited = await gateway.edit_assistant_message(
        account_id=chat.account_id,
        conversation_id=chat.conversation_id,
        assistant_message_ref=own,
        text="Synthetic",
    )
    deleted = await gateway.delete_own_business_messages(
        account_id=chat.account_id,
        conversation_id=chat.conversation_id,
        assistant_message_refs=[own],
    )
    assert edited.ok
    assert deleted.ok
    assert len(sender.plain_edits) == 1
    assert len(sender.deletes) == 1
    assert sender.plain_edits[0]["message_id"] == sender.deletes[0]["message_ids"][0]


async def test_global_throttle_spaces_sends(env: Env) -> None:
    """Two business sends to different chats at the same instant: the second
    waits the global spacing (< 25 msg/s, ARCHITECTURE §6.8); the per-chat
    second applies to the same chat."""
    first = await seed(env)
    second = await seed(env)
    sender = FakeTelegramSender()
    sleep = FakeSleep()
    gateway = gateway_for(env, sender, sleep)

    for chat in (first, second, first):
        await gateway.send_business_text(
            account_id=chat.account_id, conversation_id=chat.conversation_id, text="Synthetic"
        )

    assert sleep.waits == [pytest.approx(0.04), pytest.approx(1.0)]
    assert len(sender.plain_sends) == 3


@pytest.mark.parametrize(
    ("error", "expected", "retry_after"),
    [
        (telegram_error("Bad Request: PEER_ID_INVALID", status_code=400), "window_closed", None),
        (
            telegram_error("Bad Request: BUSINESS_PEER_INVALID", status_code=400),
            "peer_unavailable",
            None,
        ),
        (
            telegram_error("Bad Request: BUSINESS_CONNECTION_INVALID", status_code=400),
            "connection_invalid",
            None,
        ),
        (telegram_error("Bad Request: chat not found", status_code=400), "chat_unreachable", None),
        (telegram_error("Forbidden: bot was blocked", status_code=403), "forbidden", None),
        (
            telegram_error("Too Many Requests: retry later", status_code=429, retry_after=7),
            "rate_limited",
            7,
        ),
        (telegram_error("Internal Server Error", status_code=500), "transient", None),
        (network_error(), "transient", None),
    ],
)
async def test_each_error_description_maps_to_its_class(
    env: Env, error: Exception, expected: str, retry_after: int | None
) -> None:
    chat = await seed(env)
    sender = FakeTelegramSender()
    sender.plain_script = [error]

    result = await gateway_for(env, sender).send_business_text(
        account_id=chat.account_id, conversation_id=chat.conversation_id, text="Synthetic"
    )

    assert result.error_class is ErrorClass(expected)
    assert result.retry_after == retry_after
    assert len(sender.plain_sends) == 1
    if expected == "transient":
        # No answer or a 5xx: the send may have happened (§6 invariant 6).
        assert result.delivery_uncertain
    else:
        assert not result.delivery_uncertain
