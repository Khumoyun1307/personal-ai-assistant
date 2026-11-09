"""Restored consent cannot authorize AI until the owner agrees again (I19)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update

from assistant.accounts import service
from assistant.accounts.models import Account, Consent, PendingInput
from assistant.accounts.service import ConsentMissingError
from assistant.agent.persistence import DbConsentReader
from assistant.conversations.models import Message
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.db.uow import transaction
from assistant.inbox.backup_check import deliver_restore_notice
from assistant.inbox.connection_alerts import notify_connection_change, send_managed_consent_card
from assistant.inbox.notify import OwnerAlerts
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.port import LLMRequest, Provenance, TextItem
from assistant.privacy import post_restore as recovery
from assistant.privacy.deletion import delete_managed_data
from assistant.telegram.ingress.writer import write_batch
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response
from tests.integration.business_env import (
    OWNER,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    events,
    grant_managed,
    own_card,
    press_own,
    private_message,
)
from tests.integration.p2_factories import seed_account

pytestmark = pytest.mark.integration


class SyntheticOutput(BaseModel):
    text: str


def request(account_id: int, *, managed: bool = False) -> LLMRequest:
    return LLMRequest(
        account_id=account_id,
        purpose="owner_request",
        model="gpt-6-luna",
        instructions="Synthetic instruction",
        items=(
            TextItem(
                "user",
                "Synthetic restored text",
                Provenance.MANAGED if managed else Provenance.OWNER_CONTROL,
            ),
        ),
        max_output_tokens=100,
        timeout_seconds=10,
    )


async def agree_to_current_prompt(env: Env) -> None:
    async with env.factory() as session:
        prompt_id = await session.scalar(
            select(PendingInput.prompt_message_id)
            .join(Account, Account.id == PendingInput.account_id)
            .where(Account.telegram_user_id == OWNER, PendingInput.kind == "onboarding_consent")
        )
        assert prompt_id is not None
    reply = private_message(OWNER, "yes")
    reply["message"]["message_id"] = prompt_id + 1
    await deliver(env, reply)


async def test_rg08_restored_revocation_and_deleted_content_need_fresh_control_chat_consent(
    env: Env,
) -> None:
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message())
    async with transaction(env.factory) as uow:
        account = (
            await uow.session.scalars(select(Account).where(Account.telegram_user_id == OWNER))
        ).one()
        account_id = account.id
        message = (await uow.session.scalars(select(Message))).one()
        # A synthetic snapshot of exactly the rows whose privacy decisions
        # would be rewound by pg_restore; no real dumps or APIs are involved.
        snapshot = {
            column.key: getattr(message, column.key)
            for column in Message.__table__.columns
            if not column.primary_key and column.computed is None and column.identity is None
        }
        taken_at = env.clock.now()
        for kind in (service.CONSENT_AI_CONTROL, service.CONSENT_AI_MANAGED):
            await service.revoke_consent(uow.session, account, kind=kind, clock=env.clock)
        await delete_managed_data(uow.session, account_id=account_id, clock=env.clock)
    async with env.factory() as session:
        assert await session.scalar(select(Message.id)) is None
        assert not (
            await service.active_consent(session, account_id, kind=service.CONSENT_AI_CONTROL)
        ).granted
    # Restore the snapshot: both revoked grants and deleted text return.
    async with transaction(env.factory) as uow:
        await uow.session.execute(
            update(Consent).where(Consent.account_id == account_id).values(revoked_at=None)
        )
        uow.session.add(Message(**snapshot))
        uow.session.add(SystemState(key=KEY_RESTORE_PENDING, value={}))
        stale_card = await service.create_card(
            uow.session,
            account_id=account_id,
            kind=service.MANAGED_CONSENT_CARD,
            clock=env.clock,
        )
        stale_token = stale_card.token
        await service.set_pending_input(
            uow.session, account_id=account_id, kind="onboarding_consent", clock=env.clock
        )
    fake = FakeLLM(text_response("Synthetic answer"), text_response("Synthetic managed answer"))
    gated = ConsentGatedLLMClient(fake, DbConsentReader(env.factory))
    with pytest.raises(ConsentMissingError):
        await gated.complete(request(account_id))
    with pytest.raises(ConsentMissingError):
        await gated.parse(request(account_id, managed=True), SyntheticOutput)
    assert fake.requests == []

    await recovery.post_restore(env.factory, env.clock, backup_taken_at=taken_at)
    with pytest.raises(ConsentMissingError):
        await gated.complete(request(account_id, managed=True))
    assert fake.requests == []
    async with env.factory() as session:
        assert await session.scalar(select(Message.id)) is not None
    # A plain yes and an obsolete pre-backup card cannot grant consent.
    assert await own_card(env, service.MANAGED_CONSENT_CARD) is None
    await deliver(env, callback(f"managed_consent:{stale_token}:agree"))
    await deliver(env, private_message(OWNER, "yes"))
    with pytest.raises(ConsentMissingError):
        await gated.complete(request(account_id))
    await deliver(env, private_message(OWNER, "/start"))
    prompt = env.sender.texts[-1]
    assert "deletions" in prompt
    assert "revocations" in prompt
    assert "/settings" in prompt
    assert "/privacy revoke" in prompt
    await agree_to_current_prompt(env)
    await gated.complete(request(account_id))
    # Control consent never implicitly re-enables processing managed text.
    with pytest.raises(ConsentMissingError):
        await gated.complete(request(account_id, managed=True))
    await deliver(env, private_message(OWNER, "/privacy managed"))
    await press_own(env, service.MANAGED_CONSENT_CARD, "agree")
    await gated.complete(request(account_id, managed=True))
    assert len(fake.requests) == 2
    fake.parsed.append({"text": "Synthetic parsed answer"})
    parsed = await gated.parse(request(account_id, managed=True), SyntheticOutput)
    assert parsed.parsed == SyntheticOutput(text="Synthetic parsed answer")
    await deliver(env, private_message(OWNER, "/status"))
    assert "restore_consent_pending" not in env.sender.texts[-1]


async def test_restore_sweep_failure_keeps_gate_closed_and_retry_finishes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_action_lifecycle_bulk import seed_actions

    await connect(env)
    action_account_id, _, action_ids = await seed_actions(env)
    real_sweep = recovery.sweep_all

    async def fail_sweep(*args: object, **kwargs: object) -> None:
        # Even while the sweep is running, the committed restore gate remains.
        async with env.factory() as session:
            with pytest.raises(recovery.RestorePendingError):
                await recovery.assert_not_restore_pending(session)
        raise RuntimeError("synthetic sweep failure")

    monkeypatch.setattr(recovery, "sweep_all", fail_sweep)
    taken_at = env.clock.now() - timedelta(hours=1)
    with pytest.raises(RuntimeError, match="synthetic sweep failure"):
        await recovery.post_restore(env.factory, env.clock, backup_taken_at=taken_at)
    await deliver(env, private_message(OWNER, "/status"))
    assert "restore_pending: recovery incomplete" in env.sender.texts[-1]
    assert "restore_consent_pending: ai_control, ai_managed" in env.sender.texts[-1]
    async with env.factory() as session:
        with pytest.raises(recovery.RestorePendingError):
            await recovery.assert_not_restore_pending(session)
        assert await session.get(SystemState, recovery.KEY_RESTORE_NOTICE) is None
        assert await session.scalar(select(PendingInput.id)) is None
    monkeypatch.setattr(recovery, "sweep_all", real_sweep)
    await recovery.post_restore(env.factory, env.clock, backup_taken_at=taken_at)
    async with env.factory() as session:
        await recovery.assert_not_restore_pending(session)
        notice = await session.get(SystemState, recovery.KEY_RESTORE_NOTICE)
        assert notice is not None
        cancelled = notice.value["actions"][str(action_account_id)]["cancelled"]
        assert action_ids["approved"] in {row["id"] for row in cancelled}


async def test_restore_reconsent_is_account_scoped(env: Env) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        other = await seed_account(uow.session)
        other_id = other.id
        await service.grant_consent(
            uow.session,
            other,
            kind=service.CONSENT_AI_CONTROL,
            policy_version=service.POLICY_VERSION,
            processors=list(service.AI_PROCESSORS),
            clock=env.clock,
        )
    await recovery.post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    await deliver(env, private_message(OWNER, "/start"))
    await agree_to_current_prompt(env)
    gated = ConsentGatedLLMClient(FakeLLM(), DbConsentReader(env.factory))
    with pytest.raises(ConsentMissingError):
        await gated.complete(request(other_id))
    async with env.factory() as session:
        owner_id = await session.scalar(select(Account.id).where(Account.telegram_user_id == OWNER))
        assert owner_id is not None
        assert (
            await service.active_consent(session, owner_id, kind=service.CONSENT_AI_CONTROL)
        ).granted
        assert not (
            await service.active_consent(session, other_id, kind=service.CONSENT_AI_CONTROL)
        ).granted


@pytest.mark.parametrize("reply_offset", [-1, 0])
async def test_restored_inbox_reply_cannot_answer_a_new_consent_prompt(
    env: Env, reply_offset: int
) -> None:
    await connect(env)
    async with env.factory() as session:
        account_id = await session.scalar(
            select(Account.id).where(Account.telegram_user_id == OWNER)
        )
        assert account_id is not None
    taken_at = env.clock.now()
    # The snapshot includes unprocessed /start + yes. The fake sender's
    # next id is 10001, just as a real newly delivered prompt follows the
    # restored messages in the same control chat.
    env.sender._next_message_id = 10000
    start = private_message(OWNER, "/start")
    start["message"]["message_id"] = 9000
    old_yes = private_message(OWNER, "yes")
    old_yes["message"]["message_id"] = 10001 + reply_offset
    async with transaction(env.factory) as uow:
        await write_batch(uow.session, bot_id=env.bot_id, updates=[start, old_yes])
    env.clock.advance(hours=1)
    await recovery.post_restore(env.factory, env.clock, backup_taken_at=taken_at)
    # Replaying /start sends a NEW prompt, but the next inbox row is still
    # the old yes. Processing time must not turn it into a fresh decision.
    await env.processor.process_batch()
    fake = FakeLLM(text_response("Synthetic answer"))
    gated = ConsentGatedLLMClient(fake, DbConsentReader(env.factory))
    with pytest.raises(ConsentMissingError):
        await gated.complete(request(account_id))
    assert fake.requests == []
    async with env.factory() as session:
        pending = await service.get_pending_input(session, account_id=account_id, clock=env.clock)
        assert pending is not None
        assert pending.prompt_message_id == 10001
        assert pending.kind == "onboarding_consent"
    # The stale reply neither grants nor consumes the new question. A new
    # owner reply after that prompt can renew consent without another /start.
    fresh_yes = private_message(OWNER, "yes")
    fresh_yes["message"]["message_id"] = 10002
    await deliver(env, fresh_yes)
    await gated.complete(request(account_id))


async def test_restore_consent_without_a_prompt_id_stays_quarantined(env: Env) -> None:
    await connect(env)
    await recovery.post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    await deliver(env, private_message(OWNER, "/start"))
    async with transaction(env.factory) as uow:
        account_id = await uow.session.scalar(
            select(Account.id).where(Account.telegram_user_id == OWNER)
        )
        assert account_id is not None
        pending = await service.get_pending_input(
            uow.session, account_id=account_id, clock=env.clock
        )
        assert pending is not None
        pending.prompt_message_id = None
    await deliver(env, private_message(OWNER, "yes"))
    fake = FakeLLM()
    gated = ConsentGatedLLMClient(fake, DbConsentReader(env.factory))
    with pytest.raises(ConsentMissingError):
        await gated.complete(request(account_id))
    assert fake.requests == []
    async with env.factory() as session:
        assert await service.get_pending_input(session, account_id=account_id, clock=env.clock)


def assert_privacy_warning(prompt: str) -> None:
    assert "deletions" in prompt.lower()
    assert "revocations" in prompt
    assert "after the backup time may have been undone" in prompt
    assert "/settings → Delete data" in prompt
    assert "/privacy revoke" in prompt


@pytest.mark.parametrize("language", ["en", "uz_latn", "uz_cyrl", "ru"])
async def test_restore_language_picker_consent_repeats_warning_when_notice_fails(
    env: Env, language: str
) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        account = (
            await uow.session.scalars(select(Account).where(Account.telegram_user_id == OWNER))
        ).one()
        account.locale_pref = None
    await recovery.post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    channel = FakeChannel(deliver=False)
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    assert not await deliver_restore_notice(env.factory, alerts)
    await deliver(env, private_message(OWNER, "/start"))
    await press_own(env, "onb_lang", language)
    assert_privacy_warning(env.sender.texts[-1])
    async with env.factory() as session:
        pending = await service.get_pending_input(session, account_id=account.id, clock=env.clock)
        assert pending is not None
        assert pending.prompt_message_id is not None


@pytest.mark.parametrize("entry_point", ["connection_notice", "agent_card", "command"])
async def test_restore_managed_consent_entry_points_repeat_warning_when_notice_fails(
    env: Env, entry_point: str
) -> None:
    await connect(env)
    await recovery.post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    channel = FakeChannel(deliver=False)
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    assert not await deliver_restore_notice(env.factory, alerts)
    channel.deliver = True
    async with env.factory() as session:
        account_id = await session.scalar(
            select(Account.id).where(Account.telegram_user_id == OWNER)
        )
        assert account_id is not None
    if entry_point == "connection_notice":
        event = (await events(env))[-1]
        assert (await notify_connection_change(env.factory, env.clock, alerts, event.id)).sent
        prompt = channel.sends[-1]
    elif entry_point == "agent_card":
        result = await send_managed_consent_card(
            env.factory, env.clock, channel, account_id=account_id, chat_id=OWNER
        )
        assert result is not None
        assert result.ok
        prompt = channel.sends[-1]
    else:
        await deliver(env, private_message(OWNER, "/privacy managed"))
        prompt = env.sender.texts[-1]
    assert_privacy_warning(prompt)
    assert await own_card(env, service.MANAGED_CONSENT_CARD) is not None
