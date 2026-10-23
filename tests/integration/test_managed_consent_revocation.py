"""Declining or revoking `ai_managed`: the cards and their effects (S02.4).

FR-ONB-06 / J1 step 5: the owner is offered deletion of what was stored from
their chats, then a choice between observing without AI and stopping. The
cards are the S01.3 confirmation-card primitive: single-use tokens, owner
scoped, edited in place, always answered.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError

from assistant.accounts.models import AuditLogEntry, PendingInput
from assistant.accounts.service import ConsentMissingError, managed_mode
from assistant.agent.persistence import DbConsentReader
from assistant.conversations.models import Contact, Message
from assistant.conversations.service import conversations_awaiting_ai
from assistant.core.routing import Route
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.port import LLMRequest, Provenance, TextItem
from tests.fakes.llm import FakeLLM
from tests.integration.business_env import (
    OWNER,
    STRANGER,
    Env,
    business_message,
    callback,
    connect,
    count,
    decline_managed,
    deliver,
    grant_managed,
    inbound,
    live_card,
    press,
    private_message,
)

pytestmark = pytest.mark.integration

CANARY = "CANARY-SYNTH-2b7e"


async def seed(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_text=CANARY, first_name=CANARY))


# ------------------------------------------------------------ offering the cards
async def test_declining_managed_consent_offers_the_deletion_card(env: Env) -> None:
    await seed(env)
    await decline_managed(env)

    card = await live_card(env, "managed_delete")
    assert card.account_id is not None
    assert "Delete what I stored" in env.sender.texts[-1]
    keyboard = env.sender.sends[-1]["reply_markup"]
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert labels == ["Delete", "Keep"]
    assert all(
        f":{card.token}:" in b.callback_data for row in keyboard.inline_keyboard for b in row
    )


async def test_revoke_offers_the_deletion_card_only_when_managed_consent_existed(env: Env) -> None:
    await seed(env)
    await deliver(env, private_message(OWNER, "/privacy revoke"))
    assert "Delete what I stored" not in env.sender.texts[-1]

    await deliver(env, private_message(OWNER, "/start"))
    await deliver(env, private_message(OWNER, "yes"))
    await grant_managed(env)
    await deliver(env, private_message(OWNER, "/privacy revoke"))
    assert "Delete what I stored" in env.sender.texts[-1]


async def test_revoking_managed_consent_stops_managed_processing(env: Env) -> None:
    await seed(env)
    await grant_managed(env)
    account_id = await live_or_account_id(env)
    conversation_id = await _conversation_id(env)
    async with env.factory() as session:
        assert await conversations_awaiting_ai(session, account_id) == [conversation_id]
    await deliver(env, private_message(OWNER, "/privacy revoke"))

    fake = FakeLLM()
    gated = ConsentGatedLLMClient(fake, DbConsentReader(env.factory))
    async with env.factory() as session:
        assert await conversations_awaiting_ai(session, account_id) == []
    request = LLMRequest(
        account_id=account_id,
        purpose="summarize",
        model="gpt-6-luna",
        instructions="synthetic",
        items=(TextItem(role="user", text=CANARY, provenance=Provenance.MANAGED),),
        max_output_tokens=10,
        timeout_seconds=1.0,
    )
    with pytest.raises(ConsentMissingError):
        await gated.complete(request)
    assert fake.requests == []


async def live_or_account_id(env: Env) -> int:
    async with env.factory() as session:
        return int(
            (
                await session.execute(
                    text("SELECT id FROM app.accounts WHERE telegram_user_id = :o"), {"o": OWNER}
                )
            ).scalar_one()
        )


async def _conversation_id(env: Env) -> int:
    async with env.factory() as session:
        return int((await session.execute(text("SELECT id FROM app.conversations"))).scalar_one())


# ------------------------------------------------------------ pressing the cards
async def test_delete_removes_managed_data_and_writes_one_audit_row(env: Env) -> None:
    await seed(env)
    original = business_message(message_text=f"{CANARY} second")
    await deliver(env, original)
    await decline_managed(env)

    await press(env, "managed_delete", "delete")

    assert await count(env, Message) == 0
    async with env.factory() as session:
        names = (await session.execute(select(Contact.display_name, Contact.username))).all()
        audits = (
            (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.event == "managed_data_deleted")
                )
            )
            .scalars()
            .all()
        )
    assert names == [(None, None)]
    assert len(audits) == 1
    assert audits[0].details["messages"] == 2
    assert CANARY not in repr(audits[0].details)
    assert (await inbound(env, original)).payload is None
    # The card was edited in place and answered; card B follows.
    assert env.sender.edits[-1]["text"].startswith("🗑 Deleted: 2 message(s)")
    assert env.sender.answered_callbacks[-1]["text"] == "Deleted"
    assert "How should I continue?" in env.sender.texts[-1]
    await live_card(env, "managed_mode")


async def test_a_card_press_holds_no_row_lock_while_calling_telegram(env: Env) -> None:
    """AGENTS §13: the deletion locks the account and updates conversation
    rows; the card edit and card B go out only after that commits."""
    await seed(env)
    await decline_managed(env)
    seen_locked: list[bool] = []

    async def probe() -> None:
        async with env.factory() as session:
            try:
                for table in ("accounts", "conversations", "inbound_updates", "pending_inputs"):
                    await session.execute(
                        text(f"SELECT 1 FROM app.{table} FOR UPDATE NOWAIT")  # noqa: S608
                    )
                seen_locked.append(False)
            except DBAPIError:
                seen_locked.append(True)
            finally:
                await session.rollback()

    env.sender.on_call = probe
    try:
        await press(env, "managed_delete", "delete")
    finally:
        env.sender.on_call = None

    assert seen_locked, "the press must have called Telegram"
    assert not any(seen_locked)
    assert env.sender.edits[-1]["text"].startswith("🗑 Deleted:")
    assert "How should I continue?" in env.sender.texts[-1]
    await live_card(env, "managed_mode")


async def test_a_card_press_whose_commit_fails_announces_once(env: Env) -> None:
    """A transaction that fails after the handler ran is retried; the retry
    must not edit the card or send card B a second time (at most once)."""
    await seed(env)
    await decline_managed(env)
    original = env.processor._handlers
    control = original[Route.CONTROL]
    failures = [OperationalError("synthetic", {}, Exception("synthetic connection lost"))]

    async def flaky(session, row, decision):  # type: ignore[no-untyped-def]
        result = await control(session, row, decision)
        if failures:
            raise failures.pop()
        return result

    env.processor._handlers = {**original, Route.CONTROL: flaky}
    edits_before, sends_before = len(env.sender.edits), len(env.sender.sends)
    try:
        with pytest.raises(OperationalError):
            await press(env, "managed_delete", "delete")
        await env.processor.process_batch()  # the retry
    finally:
        env.processor._handlers = original

    new_edits = env.sender.edits[edits_before:]
    new_sends = env.sender.sends[sends_before:]
    assert [e["text"].startswith("🗑 Deleted:") for e in new_edits] == [True]
    assert ["How should I continue?" in s["text"] for s in new_sends] == [True]
    async with env.factory() as session:
        cards = (
            (await session.execute(select(PendingInput).where(PendingInput.kind == "managed_mode")))
            .scalars()
            .all()
        )
        audits = (
            (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.event == "managed_data_deleted")
                )
            )
            .scalars()
            .all()
        )
    assert len(cards) == 1
    assert len(audits) == 1


async def test_keep_keeps_the_data(env: Env) -> None:
    await seed(env)
    await decline_managed(env)

    await press(env, "managed_delete", "keep")

    assert await count(env, Message) == 1
    assert env.sender.edits[-1]["text"] == "Kept. Nothing was deleted."
    assert "How should I continue?" in env.sender.texts[-1]


async def test_a_card_is_single_use(env: Env) -> None:
    """Double tap ⇒ one transition (S01.3)."""
    await seed(env)
    await decline_managed(env)
    card = await live_card(env, "managed_delete")
    data = f"managed_delete:{card.token}:delete"

    await deliver(env, callback(data, message_id=card.prompt_message_id))
    await deliver(env, callback(data, message_id=card.prompt_message_id))

    async with env.factory() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.event == "managed_data_deleted")
                )
            )
            .scalars()
            .all()
        )
    assert len(audits) == 1
    assert env.sender.answered_callbacks[-1]["text"] == "This button is no longer active."


async def test_a_non_owner_cannot_press_a_card(env: Env) -> None:
    await seed(env)
    await decline_managed(env)
    card = await live_card(env, "managed_delete")

    update = callback(
        f"managed_delete:{card.token}:delete", from_id=STRANGER, chat_id=STRANGER, message_id=5
    )
    await deliver(env, update)

    assert (await inbound(env, update)).status == "rejected"
    assert await count(env, Message) == 1
    await live_card(env, "managed_delete")  # still live


@pytest.mark.parametrize(
    "data", ["managed_delete:zzzzzzzzzz:delete", "garbage", None, "managed_delete:a:b:c"]
)
async def test_a_stale_or_malformed_press_is_no_longer_active(env: Env, data: str | None) -> None:
    await seed(env)
    await deliver(env, callback(data))
    assert env.sender.answered_callbacks[-1]["text"] == "This button is no longer active."
    assert await count(env, Message) == 1


async def test_an_expired_card_is_no_longer_active(env: Env) -> None:
    await seed(env)
    await decline_managed(env)
    env.clock.advance(hours=25)

    await press(env, "managed_delete", "delete")

    assert env.sender.answered_callbacks[-1]["text"] == "This button is no longer active."
    assert await count(env, Message) == 1
    async with env.factory() as session:
        assert (await session.execute(select(PendingInput))).scalars().all() == []


# ------------------------------------------------------------ the mode choice
async def test_continuing_without_ai_keeps_observing(env: Env) -> None:
    await seed(env)
    await decline_managed(env)
    await press(env, "managed_delete", "keep")
    await press(env, "managed_mode", "observe")

    later = business_message(message_text="synthetic later")
    await deliver(env, later)
    assert (await inbound(env, later)).status == "processed"
    assert await count(env, Message) == 2


async def test_stop_observing_stores_nothing_further(env: Env) -> None:
    await seed(env)
    await decline_managed(env)
    await press(env, "managed_delete", "keep")
    await press(env, "managed_mode", "stop")

    later = business_message(message_text="synthetic later")
    await deliver(env, later)
    row = await inbound(env, later)
    assert row.status == "ignored"
    assert row.payload is None
    assert await count(env, Message) == 1
    async with env.factory() as session:
        from assistant.accounts.models import Account

        account = (
            await session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()
        assert managed_mode(account) == "stopped"

    # Agreeing again resumes observation.
    await grant_managed(env)
    resumed = business_message(message_text="synthetic resumed")
    await deliver(env, resumed)
    assert (await inbound(env, resumed)).status == "processed"


async def test_a_command_does_not_cancel_a_live_card(env: Env) -> None:
    """S01.3 precedence rule 1 cancels *questions*; a card stays tappable."""
    await seed(env)
    await decline_managed(env)
    await deliver(env, private_message(OWNER, "/help"))
    await live_card(env, "managed_delete")


async def test_expired_cards_are_swept(env: Env) -> None:
    from assistant.privacy.retention import sweep_all

    await seed(env)
    await decline_managed(env)
    env.clock.advance(hours=25)
    report = await sweep_all(env.factory, env.clock)
    assert report.expired_inputs == 1
