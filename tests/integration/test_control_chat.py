"""Control-chat flows against a real database (ARCHITECTURE §6.7, §14.3).

Named regression suite. The claims being pinned:

- **no AI call without consent** (§6 invariant 9), asked in the owner's
  language after the language card (M03 fix round C4;
  `test_onboarding_language.py` has the rest of that flow);
- onboarding state survives in Postgres, not in process memory;
- a `/command` always beats a pending question;
- the kill switch works without the model;
- `/settings` and `/status` work when the provider is unreachable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import Account, ControlTurn, PendingInput
from assistant.db.uow import run_after_commit
from assistant.telegram.control.dispatcher import ControlDispatcher
from assistant.telegram.control.handlers import IncomingMessage
from assistant.telegram.gateway import OutboundGateway
from tests.fakes.clock import FakeClock
from tests.fakes.sender import FakeTelegramSender

pytestmark = pytest.mark.integration

OWNER = 584113992
CHAT = 584113992


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sender() -> FakeTelegramSender:
    return FakeTelegramSender()


@pytest.fixture
def dispatcher(sender: FakeTelegramSender, clock: FakeClock) -> ControlDispatcher:
    return ControlDispatcher(OutboundGateway(sender, clock), clock)


def message(text: str, *, message_id: int = 1) -> IncomingMessage:
    return IncomingMessage(
        account_id=0,
        chat_id=CHAT,
        telegram_message_id=message_id,
        text=text,
        from_id=OWNER,
    )


async def choose_language(
    session: AsyncSession, dispatcher: ControlDispatcher, op: str = "en"
) -> None:
    """Tap `op` on the language card, then run what the tap queued for after
    its commit, as the inbox processor does. This suite's transaction never
    commits, so those follow-up transactions join it as savepoints."""
    card = (
        await session.execute(
            select(PendingInput)
            .where(PendingInput.kind == "onb_lang")
            .where(PendingInput.token.is_not(None))
        )
    ).scalar_one()
    await dispatcher.dispatch_callback(
        session,
        callback_id="cb-synthetic",
        from_id=OWNER,
        data=f"onb_lang:{card.token}:{op}",
        chat_id=CHAT,
        message_id=card.prompt_message_id,
    )
    await session.flush()
    joined = async_sessionmaker(
        bind=await session.connection(),
        class_=AsyncSession,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    await run_after_commit(session, joined)


async def onboard(
    session: AsyncSession, dispatcher: ControlDispatcher, *, agree: bool = True
) -> Account:
    await dispatcher.dispatch(session, message("/start"))
    await choose_language(session, dispatcher)
    await dispatcher.dispatch(session, message("yes" if agree else "no", message_id=2))
    result = await session.execute(select(Account).where(Account.telegram_user_id == OWNER))
    return result.scalar_one()


# ------------------------------------------------------------------ onboarding
async def test_start_creates_the_account_and_records_where_to_reply(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    await dispatcher.dispatch(db_session, message("/start"))

    account = (
        await db_session.execute(select(Account).where(Account.telegram_user_id == OWNER))
    ).scalar_one()
    assert account.status == "onboarding"
    # Without this the bot cannot reach the owner at all (TF §12 LV-09).
    assert account.control_chat_id == CHAT


async def test_onboarding_state_lives_in_the_database_not_in_memory(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    """A restart mid-onboarding must not lose the question that was asked:
    the language card after /start, the consent question after the tap."""
    await dispatcher.dispatch(db_session, message("/start"))

    card = (await db_session.execute(select(PendingInput))).scalar_one()
    assert card.kind == "onb_lang"
    assert card.token is not None

    await choose_language(db_session, dispatcher)

    pending = (await db_session.execute(select(PendingInput))).scalar_one()
    assert pending.kind == "onboarding_consent"
    assert pending.token is None


@pytest.mark.parametrize("op", ["uz_latn", "uz_cyrl", "ru", "en"])
async def test_the_consent_prompt_names_the_processor(
    db_session: AsyncSession, dispatcher: ControlDispatcher, sender: FakeTelegramSender, op: str
) -> None:
    """Consent that does not say who sees the data is not consent (SEC-02) —
    in every language; and it is asked only once the language is known."""
    await dispatcher.dispatch(db_session, message("/start"))
    assert "OpenAI" not in sender.texts[0]

    await choose_language(db_session, dispatcher, op)

    assert "<b>OpenAI</b>" in sender.texts[-1]


async def test_agreeing_grants_consent_and_activates_the_account(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    account = await onboard(db_session, dispatcher)
    assert account.status == "active"

    state = await service.active_consent(db_session, account.id, kind=service.CONSENT_AI_CONTROL)
    assert state.granted
    assert "OpenAI" in state.processors


async def test_declining_leaves_no_consent_and_the_bot_still_works(
    db_session: AsyncSession, dispatcher: ControlDispatcher, sender: FakeTelegramSender
) -> None:
    account = await onboard(db_session, dispatcher, agree=False)
    state = await service.active_consent(db_session, account.id, kind=service.CONSENT_AI_CONTROL)
    assert not state.granted

    await dispatcher.dispatch(db_session, message("/settings", message_id=3))
    assert "Settings" in sender.texts[-1]


# --------------------------------------------------------------- consent gate
async def test_requiring_consent_raises_when_it_was_never_granted(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    """§6 invariant 9 is a wall, not a branch."""
    account = await onboard(db_session, dispatcher, agree=False)
    with pytest.raises(service.ConsentMissingError):
        await service.require_consent(db_session, account.id, kind=service.CONSENT_AI_CONTROL)


async def test_requiring_consent_raises_for_an_unnamed_processor(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    """Switching provider without re-consent is exactly what this stops."""
    account = await onboard(db_session, dispatcher)
    await service.require_consent(
        db_session, account.id, kind=service.CONSENT_AI_CONTROL, processor="OpenAI"
    )
    with pytest.raises(service.ConsentMissingError):
        await service.require_consent(
            db_session, account.id, kind=service.CONSENT_AI_CONTROL, processor="SomeoneElse"
        )


async def test_revoking_consent_takes_effect_immediately(
    db_session: AsyncSession, dispatcher: ControlDispatcher, clock: FakeClock
) -> None:
    account = await onboard(db_session, dispatcher)
    assert (
        await service.revoke_consent(
            db_session, account, kind=service.CONSENT_AI_CONTROL, clock=clock
        )
        == 1
    )

    with pytest.raises(service.ConsentMissingError):
        await service.require_consent(db_session, account.id, kind=service.CONSENT_AI_CONTROL)


async def test_consent_history_is_append_only(
    db_session: AsyncSession, dispatcher: ControlDispatcher, clock: FakeClock
) -> None:
    """What was agreed, and when, has to survive a revoke-and-regrant."""
    from assistant.accounts.models import Consent

    account = await onboard(db_session, dispatcher)
    await service.revoke_consent(db_session, account, kind=service.CONSENT_AI_CONTROL, clock=clock)
    await service.grant_consent(
        db_session,
        account,
        kind=service.CONSENT_AI_CONTROL,
        policy_version="v0",
        processors=["OpenAI"],
        clock=clock,
    )
    rows = (
        (await db_session.execute(select(Consent).where(Consent.account_id == account.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2


# --------------------------------------------------------- routing precedence
async def test_a_command_cancels_a_pending_question(
    db_session: AsyncSession, dispatcher: ControlDispatcher, sender: FakeTelegramSender
) -> None:
    """Otherwise /status would be swallowed as the answer to the question."""
    await dispatcher.dispatch(db_session, message("/start"))
    await choose_language(db_session, dispatcher)
    assert (await db_session.execute(select(PendingInput))).scalar_one_or_none() is not None

    await dispatcher.dispatch(db_session, message("/help", message_id=2))

    assert (await db_session.execute(select(PendingInput))).scalar_one_or_none() is None
    assert "What I can do" in sender.texts[-1]


async def test_a_pending_question_consumes_the_next_plain_message(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    await dispatcher.dispatch(db_session, message("/start"))
    await choose_language(db_session, dispatcher)
    outcome = await dispatcher.dispatch(db_session, message("yes", message_id=2))
    assert outcome.result.command == "consent"


async def test_a_pending_question_expires(
    db_session: AsyncSession, dispatcher: ControlDispatcher, clock: FakeClock
) -> None:
    await dispatcher.dispatch(db_session, message("/start"))
    await choose_language(db_session, dispatcher)
    assert (
        await service.get_pending_input(
            db_session,
            account_id=(
                await db_session.execute(
                    select(Account.id).where(Account.telegram_user_id == OWNER)
                )
            ).scalar_one(),
            clock=clock,
        )
    ) is not None
    clock.advance(hours=2)

    account = (
        await db_session.execute(select(Account).where(Account.telegram_user_id == OWNER))
    ).scalar_one()
    assert (await service.get_pending_input(db_session, account_id=account.id, clock=clock)) is None


async def test_a_stop_word_pauses_without_involving_the_model(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    """The kill switch must work when the provider is down or out of budget."""
    account = await onboard(db_session, dispatcher)
    assert account.autonomy_paused is False

    outcome = await dispatcher.dispatch(db_session, message("stop", message_id=5))
    assert outcome.result.command == "pause"
    await db_session.refresh(account)
    assert account.autonomy_paused is True


@pytest.mark.parametrize("word", ["stop", "стоп", "to'xta", "тўхта", "STOP"])
async def test_stop_words_in_every_language_the_owner_uses(
    db_session: AsyncSession, dispatcher: ControlDispatcher, word: str
) -> None:
    account = await onboard(db_session, dispatcher)
    await dispatcher.dispatch(db_session, message(word, message_id=6))
    await db_session.refresh(account)
    assert account.autonomy_paused is True


async def test_a_sentence_containing_stop_is_not_a_stop_word(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    """Whole-message match only, or ordinary sentences would trip the switch."""
    account = await onboard(db_session, dispatcher)
    await dispatcher.dispatch(
        db_session, message("tell Dilnoza I will stop by tomorrow", message_id=7)
    )
    await db_session.refresh(account)
    assert account.autonomy_paused is False


async def test_free_text_is_recorded_for_the_agent(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    account = await onboard(db_session, dispatcher)
    outcome = await dispatcher.dispatch(db_session, message("what did I miss today?", message_id=8))
    assert outcome.result.command == "free_text"

    turns = (
        (await db_session.execute(select(ControlTurn).where(ControlTurn.account_id == account.id)))
        .scalars()
        .all()
    )
    assert any(turn.text == "what did I miss today?" for turn in turns)


async def test_resume_clears_the_pause(
    db_session: AsyncSession, dispatcher: ControlDispatcher
) -> None:
    account = await onboard(db_session, dispatcher)
    await dispatcher.dispatch(db_session, message("stop", message_id=9))
    await dispatcher.dispatch(db_session, message("/resume", message_id=10))
    await db_session.refresh(account)
    assert account.autonomy_paused is False


# -------------------------------------------------------------------- commands
async def test_status_works_with_no_model_available(
    db_session: AsyncSession, dispatcher: ControlDispatcher, sender: FakeTelegramSender
) -> None:
    """/status must never depend on the provider being up."""
    await onboard(db_session, dispatcher)
    await dispatcher.dispatch(db_session, message("/status", message_id=11))
    assert "Status" in sender.texts[-1]


async def test_settings_changes_timezone_and_rejects_nonsense(
    db_session: AsyncSession, dispatcher: ControlDispatcher, sender: FakeTelegramSender
) -> None:
    account = await onboard(db_session, dispatcher)

    await dispatcher.dispatch(
        db_session, message("/settings timezone Asia/Tashkent", message_id=12)
    )
    await db_session.refresh(account)
    assert account.timezone == "Asia/Tashkent"

    await dispatcher.dispatch(db_session, message("/settings timezone Mars/Olympus", message_id=13))
    await db_session.refresh(account)
    assert account.timezone == "Asia/Tashkent"  # unchanged
    assert "do not know" in sender.texts[-1]


async def test_an_unknown_command_is_answered_not_ignored(
    db_session: AsyncSession, dispatcher: ControlDispatcher, sender: FakeTelegramSender
) -> None:
    await onboard(db_session, dispatcher)
    outcome = await dispatcher.dispatch(db_session, message("/teleport", message_id=14))
    assert outcome.result.handled is False
    assert "/help" in sender.texts[-1]


async def test_messaging_before_start_asks_for_start(
    db_session: AsyncSession, dispatcher: ControlDispatcher, sender: FakeTelegramSender
) -> None:
    outcome = await dispatcher.dispatch(db_session, message("hello", message_id=15))
    assert outcome.result.command == "start_required"
    assert "/start" in sender.texts[-1]


async def test_no_send_ever_carries_a_business_connection_id(
    db_session: AsyncSession, dispatcher: ControlDispatcher, sender: FakeTelegramSender
) -> None:
    """Every control reply, across every flow in this suite."""
    await onboard(db_session, dispatcher)
    for text, mid in [("/help", 20), ("/status", 21), ("/privacy", 22), ("stop", 23)]:
        await dispatcher.dispatch(db_session, message(text, message_id=mid))
    assert all(call["business_connection_id"] is None for call in sender.sends)
    assert sender.edits  # the language card, edited in place
    assert all(call["business_connection_id"] is None for call in sender.edits)
