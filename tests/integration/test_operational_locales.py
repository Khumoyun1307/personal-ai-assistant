"""Operational controls follow the saved language, without a model call."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from assistant.accounts import service
from assistant.accounts.models import Account, Consent
from assistant.core.control_texts import text
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.db.uow import transaction
from assistant.inbox.connection_alerts import send_managed_consent_card
from assistant.inbox.models import Notification
from assistant.inbox.notification_feedback import list_mutes
from assistant.inbox.notifications import NTF_CARD
from assistant.inbox.notify import AlertKind, OwnerAlerts
from assistant.telegram.control.alerts import handle_ntf_press
from assistant.telegram.control.handlers import local_time
from assistant.telegram.control.routes import ControlRoutes
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import (
    OWNER,
    callback,
    connection_update,
    deliver,
    environment,
    onboard,
    press_own,
    private_message,
)
from tests.integration.p2_factories import seed_notification

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("locale", ["uz-Latn", "uz-Cyrl", "ru", "en"])
async def test_controls_follow_language_selection(migrated_url: str, locale: str) -> None:
    async with environment(migrated_url) as env:
        await onboard(env)
        await deliver(env, private_message(OWNER, f"/settings language {locale}"))
        for command, key in [("/help", "help"), ("/pause", "paused"), ("/resume", "resumed")]:
            await deliver(env, private_message(OWNER, command))
            assert env.sender.texts[-1] == text(key, locale)
        await deliver(env, private_message(OWNER, "/settings timezone Invalid/Synthetic"))
        assert text("invalid_timezone", locale, value="Invalid/Synthetic") in (env.sender.texts[-1])
        await deliver(env, private_message(OWNER, "/settings"))
        assert text("settings_heading", locale) in env.sender.texts[-1]
        await press_own(env, "settings", "alerts")
        edited = env.sender.edits[-1]
        assert text("alerts_heading", locale).replace("&", "&amp;") in edited["text"]
        labels = [b.text for row in edited["reply_markup"].inline_keyboard for b in row]
        assert text("back", locale) in labels
        assert text("off_button", locale) in labels
        assert text("off_button", locale) + " ✓" in labels
        await deliver(env, private_message(OWNER, "/privacy"))
        assert text("privacy_heading", locale) in env.sender.texts[-1]
        await deliver(env, private_message(OWNER, "/settings language en"))
        await deliver(env, private_message(OWNER, "/help"))
        assert env.sender.texts[-1] == text("help", "en")


@pytest.mark.parametrize("locale", ["uz-Latn", "uz-Cyrl", "ru", "en"])
async def test_settings_subscreens_and_stale_buttons(migrated_url: str, locale: str) -> None:
    async with environment(migrated_url) as env:
        await onboard(env)
        await deliver(env, private_message(OWNER, f"/settings language {locale}"))
        for op, key, card in (
            ("replies", "reply_body", "set_reply"),
            ("retention", "retention_body", "set_keep"),
            ("delall", "delete_all_body", "del_all"),
        ):
            await deliver(env, private_message(OWNER, "/settings"))
            await press_own(env, "settings", op)
            rendered = env.sender.edits[-1]
            assert (
                text(key, locale, undo="", arriving="", backup="").split("\n")[0]
                in (rendered["text"])
            )
            assert rendered["reply_markup"].inline_keyboard
            if card == "set_keep":
                await press_own(env, card, "m_30")
                assert text("cannot_undo", locale) in env.sender.edits[-1]["text"]
                await press_own(env, "keep_ok", "cancel")
            elif card == "del_all":
                await press_own(env, card, "cancel")
        await press_own(env, "settings", "unknown_synthetic_op")
        assert env.sender.answered_callbacks[-1]["text"] == text("expired", locale)


@pytest.mark.parametrize("locale", ["uz-Latn", "uz-Cyrl", "ru", "en"])
async def test_managed_consent_entry_points_share_scope_and_processors(
    migrated_url: str, locale: str
) -> None:
    async with environment(migrated_url) as env:
        await onboard(env)
        await deliver(env, private_message(OWNER, f"/settings language {locale}"))
        await deliver(env, connection_update())
        async with env.factory() as session:
            account = (await session.scalars(select(Account))).one()
            account_id = account.id
        channel = FakeChannel()
        await send_managed_consent_card(
            env.factory, env.clock, channel, account_id=account_id, chat_id=OWNER
        )
        expected = text("managed_prompt", locale, processors=", ".join(service.AI_PROCESSORS))
        assert channel.sends[-1] == expected
        assert [button.label for button in channel.buttons[-1]] == [
            text("turn_on", locale),
            text("not_now", locale),
        ]
        async with transaction(env.factory) as uow:
            await service.close_cards(
                uow.session, account_id=account_id, kind=service.MANAGED_CONSENT_CARD
            )
        await deliver(env, private_message(OWNER, "/privacy managed"))
        assert env.sender.texts[-1] == expected
        await press_own(env, service.MANAGED_CONSENT_CARD, "agree")
        async with env.factory() as session:
            consents = list((await session.scalars(select(Consent))).all())
        assert {row.kind for row in consents} == {
            service.CONSENT_AI_CONTROL,
            service.CONSENT_AI_MANAGED,
        }
        assert all(row.processors == list(service.AI_PROCESSORS) for row in consents)
        assert all(row.policy_version == service.POLICY_VERSION for row in consents)
        await deliver(env, private_message(OWNER, "/privacy revoke"))
        async with env.factory() as session:
            for kind in (service.CONSENT_AI_CONTROL, service.CONSENT_AI_MANAGED):
                state = await service.active_consent(session, account_id, kind=kind)
                assert not state.granted


@pytest.mark.parametrize("locale", ["uz-Latn", "uz-Cyrl", "ru", "en"])
async def test_system_alert_uses_language_at_delivery(migrated_url: str, locale: str) -> None:
    async with environment(migrated_url) as env:
        await onboard(env)
        await deliver(env, private_message(OWNER, f"/settings language {locale}"))
        async with env.factory() as session:
            account_id = (await session.scalars(select(Account.id))).one()
        channel = FakeChannel()
        alerts = OwnerAlerts(env.factory, channel, env.clock)
        result = await alerts.notify_owner(
            account_id, AlertKind.BUDGET_THRESHOLD, dedupe_key="synthetic:budget:1", percent=80
        )
        assert result.sent
        assert channel.sends[-1] == text("alert_budget_threshold", locale, percent=80)
        await deliver(env, private_message(OWNER, "/settings language en"))
        await alerts.notify_owner(
            account_id, AlertKind.BUDGET_THRESHOLD, dedupe_key="synthetic:budget:2", percent=90
        )
        assert channel.sends[-1] == text("alert_budget_threshold", "en", percent=90)


@pytest.mark.parametrize("locale", ["uz-Latn", "uz-Cyrl", "ru", "en"])
async def test_feedback_and_mute_reset_controls_preserve_scopes(
    migrated_url: str, locale: str
) -> None:
    async with environment(
        migrated_url, routes=ControlRoutes(callbacks={NTF_CARD: handle_ntf_press})
    ) as env:
        await onboard(env)
        await deliver(env, private_message(OWNER, f"/settings language {locale}"))
        await deliver(env, private_message(OWNER, "/settings timezone Asia/Tashkent"))
        callbacks: list[str] = []
        ids: list[int] = []
        async with transaction(env.factory) as uow:
            account_id = (await uow.session.scalars(select(Account.id))).one()
            for monitor in (71, 71, 72, 72):
                notification = await seed_notification(
                    uow.session,
                    account_id=account_id,
                    kind="monitor_match",
                    refs={"automation_ids": [monitor]},
                    feedback="not_useful" if monitor == 72 else None,
                    created_at=env.clock.now(),
                    updated_at=env.clock.now(),
                )
                ids.append(notification.id)
                if monitor == 71:
                    card = await service.create_card(
                        uow.session,
                        account_id=account_id,
                        kind=NTF_CARD,
                        ref_id=notification.id,
                        clock=env.clock,
                    )
                    callbacks.append(f"{NTF_CARD}:{card.token}")
        for index, data in enumerate(callbacks):
            await deliver(env, callback(f"{data}:nu"))
            key = "not_useful" if index == 0 else "feedback_muted"
            assert env.sender.answered_callbacks[-1]["text"] == text(key, locale)
        async with env.factory() as session:
            mutes = await list_mutes(session, account_id=account_id, now=env.clock.now())
        assert {mute.monitor_id for mute in mutes} == {71, 72}
        await deliver(env, private_message(OWNER, "/settings"))
        await press_own(env, "settings", "alerts")
        labels = [
            b.text for row in env.sender.edits[-1]["reply_markup"].inline_keyboard for b in row
        ]
        assert text("mutes_button", locale) in labels
        await press_own(env, "set_alerts", "mutes")
        screen = env.sender.edits[-1]
        assert text("mutes_body", locale) in screen["text"]
        assert text("mute_kind_monitor_match", locale) in screen["text"]
        assert text("mute_monitor", locale, id=71) in screen["text"]
        assert text("mute_no_contact", locale) in screen["text"]
        assert local_time(env.clock.now(), "Asia/Tashkent") in screen["text"]
        labels = [b.text for row in screen["reply_markup"].inline_keyboard for b in row]
        scope = text("mute_no_contact", locale) + ", " + text("mute_monitor", locale, id=71)
        assert (
            text("mute_reset", locale, label=text("mute_kind_monitor_match", locale), scope=scope)
            in labels
        )
        assert text("mutes_reset_all", locale) in labels
        await press_own(env, "set_mutes", f"r{ids[0]}")
        assert env.sender.answered_callbacks[-1]["text"] == text("mute_reset_done", locale)
        async with env.factory() as session:
            mutes = await list_mutes(session, account_id=account_id, now=env.clock.now())
        assert {mute.monitor_id for mute in mutes} == {72}
        await press_own(env, "set_mutes", "all")
        assert env.sender.answered_callbacks[-1]["text"] == text("mutes_all_reset", locale)
        assert text("mutes_empty", locale) in env.sender.edits[-1]["text"]
        await press_own(env, "set_mutes", f"r{max(ids) + 1}")
        assert env.sender.answered_callbacks[-1]["text"] == text("expired", locale)
        await deliver(env, callback(f"{callbacks[0]}:us"))
        assert env.sender.answered_callbacks[-1]["text"] == text("noted", locale)
        async with env.factory() as session:
            rows = list(
                (await session.scalars(select(Notification).where(Notification.id.in_(ids)))).all()
            )
            assert {row.status for row in rows} == {"queued"}
            assert all(row.sent_at is None for row in rows)


@pytest.mark.parametrize("locale", ["uz-Latn", "uz-Cyrl", "ru", "en"])
async def test_unresolved_restore_refuses_consent_in_the_saved_language(
    migrated_url: str, locale: str
) -> None:
    async with environment(migrated_url) as env:
        await onboard(env)
        await deliver(env, private_message(OWNER, f"/settings language {locale}"))
        await deliver(env, connection_update())
        async with transaction(env.factory) as uow:
            account = (await uow.session.scalars(select(Account))).one()
            account_id = account.id
            await service.revoke_consent(
                uow.session, account, kind=service.CONSENT_AI_CONTROL, clock=env.clock
            )
            account.settings = {
                **account.settings,
                service.RESTORE_CONSENT_PENDING: [
                    service.CONSENT_AI_CONTROL,
                    service.CONSENT_AI_MANAGED,
                ],
            }
            uow.session.add(SystemState(key=KEY_RESTORE_PENDING, value={}))
        await deliver(env, private_message(OWNER, "/privacy managed"))
        await press_own(env, service.MANAGED_CONSENT_CARD, "agree")
        assert env.sender.edits[-1]["text"] == text("consent_blocked", locale)
        assert env.sender.answered_callbacks[-1]["text"] == text("consent_blocked", locale)
        await deliver(env, private_message(OWNER, "/start"))
        async with env.factory() as session:
            pending = await service.get_pending_input(
                session, account_id=account_id, clock=env.clock
            )
            assert pending is not None
            assert pending.prompt_message_id is not None
            prompt_id = pending.prompt_message_id
        reply = private_message(OWNER, "yes")
        reply["message"]["message_id"] = prompt_id + 1
        await deliver(env, reply)
        assert env.sender.texts[-1] == text("consent_blocked", locale)
        await deliver(env, private_message(OWNER, "/status"))
        assert text("restore_pending", locale) in env.sender.texts[-1]
        assert (
            text("restore_consent_pending", locale, scopes="ai_control, ai_managed")
            in env.sender.texts[-1]
        )
        async with env.factory() as session:
            assert await session.get(SystemState, KEY_RESTORE_PENDING) is not None
            for kind in (service.CONSENT_AI_CONTROL, service.CONSENT_AI_MANAGED):
                assert not (await service.active_consent(session, account_id, kind=kind)).granted
