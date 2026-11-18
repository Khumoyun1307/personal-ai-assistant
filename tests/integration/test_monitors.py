"""S05.4 monitors over the real inbox, jobs table and Postgres: the
deterministic fast path at ingestion, the `evaluate_monitor` run, the job,
the reconciler, expiry, the alert and deletion (S05.4 §3.3-§3.8).

Required tests, findable by name (S05.4 §5):
`test_notify_me_when_daniel_replies_fires_exactly_once` (PRD R2 #4),
`test_a_bypass_monitor_alerts_during_quiet_hours`,
`test_a_low_priority_contacts_monitor_alert_is_not_demoted` (R9),
`test_two_messages_of_one_burst_complete_a_one_shot_once`,
`test_a_keyword_monitor_matches_across_scripts_and_apostrophes`,
`test_a_keyword_monitor_ignores_non_matching_messages`,
`test_a_media_monitor_matches_a_photo`,
`test_a_semantic_monitor_fires_on_a_fake_llm_verdict`,
`test_three_semantic_monitors_in_scope_make_exactly_one_evaluate_call_per_burst`,
`test_a_verdict_citing_no_contact_message_does_not_fire`,
`test_verdicts_for_unknown_monitor_ids_are_dropped`,
`test_monitors_ignore_ignore_mode_chats`, `test_owner_messages_never_match`,
`test_a_backlog_message_older_than_the_monitor_never_fires_it`,
`test_an_expired_monitor_never_fires`,
`test_standing_monitors_keep_firing_and_stop_on_stop`,
`test_the_reconciler_recovers_a_lost_evaluation`,
`test_evaluate_monitor_runs_have_no_tools` (RG-17),
`test_evaluate_monitor_sees_only_its_own_conversation` (RG-09 style),
`test_without_managed_consent_no_evaluation_call_is_made` (RG-08),
`test_a_deleted_chat_removes_its_monitors_and_suppresses_pending_alerts`.

Synthetic text only; FakeLLM and FakeChannel stand in for the provider and
the control chat (AGENTS §11).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import Account, AuditLogEntry, PendingInput
from assistant.agent import prompt_registry
from assistant.agent.models import AgentRun
from assistant.agent.monitor_eval import (
    BURST_LIMIT,
    MonitorEvalConfig,
    MonitorEvalStatus,
    MonitorEvaluator,
)
from assistant.agent.tools.registry import CapabilityTier, default_registry
from assistant.automation import monitor_review, monitors
from assistant.automation.models import Automation
from assistant.automation.monitor_trigger import MonitorIngestionListener
from assistant.automation.rules import MonitorCondition, MonitorRule, MonitorScope
from assistant.core.jobs import MONITOR_TASK
from assistant.db.uow import transaction
from assistant.inbox.alert_texts import RenderedAlert
from assistant.inbox.models import ConversationTriage, Notification, ReviewProgress
from assistant.inbox.notification_types import (
    AlertClass,
    NotificationKind,
    NotificationRefs,
    Priority,
)
from assistant.inbox.notifications import NotificationDelivery, enqueue
from assistant.jobs.reconcile_monitors import monitor_reconciler
from assistant.jobs.tasks_automation import MonitorJobs
from assistant.llm.port import LLMError, LLMErrorKind, LLMRequest, StopReason, TextItem
from assistant.memory.search import normalize
from assistant.privacy import deletion
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import gated, one_id
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    Env,
    business_message,
    connect,
    deleted_messages,
    deliver,
    environment,
    grant_managed,
)

pytestmark = pytest.mark.integration

DEBOUNCE = timedelta(seconds=20)
#: Before the FakeClock's noon: the chat's first message, from before any monitor.
EARLY = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


def ts(moment: datetime) -> int:
    return int(moment.timestamp())


class _Late:
    """The listener needs the environment's scheduler and clock, which exist
    only once the environment is built; it is bound right after."""

    def __init__(self) -> None:
        self.inner: MonitorIngestionListener | None = None

    async def on_message(self, session: Any, change: Any) -> None:
        assert self.inner is not None
        await self.inner.on_message(session, change)

    async def on_deletion(self, session: Any, change: Any) -> None:
        assert self.inner is not None
        await self.inner.on_deletion(session, change)


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    """The inbox with the monitor listener wired, as `_wire_m05` does with
    flag `monitors.run` on."""
    late = _Late()
    async with environment(migrated_url, listeners=[late]) as built:
        assert built.scheduler is not None
        late.inner = MonitorIngestionListener(
            scheduler=built.scheduler, clock=built.clock, debounce=DEBOUNCE
        )
        yield built


# ------------------------------------------------------------ seeding
async def setup(env: Env, *, managed: bool = True) -> tuple[int, int]:
    """A connected owner, chat A (Kamola) and chat B (Bobur), each with one
    message from before any monitor. Returns the two conversation ids."""
    await connect(env, date=ts(EARLY) - 3600)
    if managed:
        await grant_managed(env)
    await deliver(
        env,
        business_message(
            message_id=1, message_text="SYNTH-A salom", first_name="Kamola", date=ts(EARLY)
        ),
        business_message(
            message_id=1,
            from_id=OTHER_CONTACT,
            chat_id=OTHER_CONTACT,
            message_text="SYNTH-B salom",
            first_name="Bobur",
            date=ts(EARLY),
        ),
    )
    return await chat_id(env, CONTACT), await chat_id(env, OTHER_CONTACT)


async def chat_id(env: Env, business_chat: int) -> int:
    return await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=business_chat
    )


async def contact_id(env: Env, telegram_user: int = CONTACT) -> int:
    return await one_id(
        env, "SELECT id FROM app.contacts WHERE telegram_user_id = :u", u=telegram_user
    )


async def account_id(env: Env) -> int:
    return await one_id(env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER)


async def monitor(
    env: Env,
    kind: str,
    *,
    contact_ids: Sequence[int] = (),
    conversation_ids: Sequence[int] = (),
    all_observed: bool = False,
    contact: int | None = None,
    terms: Sequence[str] = (),
    description: str | None = None,
    media_kinds: Sequence[str] = (),
    lifecycle: str = "one_shot",
    action: str = "alert",
    bypass: bool = False,
    expires_at: datetime | None = None,
) -> int:
    """A confirmed monitor, created as the `create_monitor` tool does (now)."""
    rule = MonitorRule(
        scope=MonitorScope(
            contact_ids=tuple(contact_ids),
            conversation_ids=tuple(conversation_ids),
            all_observed=all_observed,
        ),
        condition=MonitorCondition.model_validate(
            {
                "kind": kind,
                "contact_id": contact,
                "terms": tuple(terms),
                "normalized_terms": tuple(normalize(t) for t in terms),
                "description": description,
                "media_kinds": tuple(media_kinds),
            }
        ),
        action=action,  # type: ignore[arg-type]
        lifecycle=lifecycle,  # type: ignore[arg-type]
        bypass_quiet_hours=bypass,
    )
    owner = await account_id(env)
    async with transaction(env.factory) as uow:
        row, created = await monitors.create(
            uow.session,
            account_id=owner,
            rule=rule,
            expires_at=expires_at,
            timezone="UTC",
            clock=env.clock,
            run_id=None,
            tool_call_id=None,
        )
    assert created
    return row.id


async def say(
    env: Env,
    message_id: int,
    body: str | None,
    *,
    chat: int = CONTACT,
    sender: int | None = None,
    at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """A business message in a chat, sent now (or `at`), by its contact
    unless `sender` says otherwise."""
    await deliver(
        env,
        business_message(
            message_id=message_id,
            from_id=sender if sender is not None else chat,
            chat_id=chat,
            message_text=body,
            date=ts(at or env.clock.now()),
            first_name="Kamola" if chat == CONTACT else "Bobur",
            extra=extra,
        ),
    )


async def alerts(env: Env) -> list[Notification]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(Notification)
                    .where(Notification.kind == NotificationKind.MONITOR_MATCH.value)
                    .order_by(Notification.id)
                )
            )
            .scalars()
            .all()
        )


async def automation(env: Env, automation_id: int) -> Automation | None:
    async with env.factory() as session:
        return await session.get(Automation, automation_id)


async def events(env: Env, event: str) -> int:
    async with env.factory() as session:
        rows = await session.execute(select(AuditLogEntry.id).where(AuditLogEntry.event == event))
        return len(rows.all())


async def message_id_of(env: Env, telegram_id: int, chat: int = CONTACT) -> int:
    return await one_id(
        env,
        "SELECT m.id FROM app.messages m JOIN app.conversations c ON c.id = m.conversation_id "
        "WHERE c.business_chat_id = :c AND m.telegram_message_id = :t",
        c=chat,
        t=telegram_id,
    )


async def flag(env: Env, conversation: int) -> datetime | None:
    async with env.factory() as session:
        value: datetime | None = (
            await session.execute(
                text("SELECT pending_monitor_at FROM app.conversations WHERE id = :c"),
                {"c": conversation},
            )
        ).scalar_one()
        return value


async def monitor_jobs(env: Env) -> list[tuple[int, str]]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT (args->>'conversation_id')::bigint, status::text "
                "FROM procrastinate.procrastinate_jobs WHERE task_name = :t ORDER BY id"
            ),
            {"t": MONITOR_TASK},
        )
        return [(int(r[0]), str(r[1])) for r in rows.all()]


def evaluator(env: Env, fake: FakeLLM, **config: Any) -> MonitorEvaluator:
    values: dict[str, Any] = {"model": "gpt-6-luna"}
    values.update(config)
    return MonitorEvaluator(
        session_factory=env.factory,
        llm=gated(env, fake),
        clock=env.clock,
        config=MonitorEvalConfig(**values),
        scheduler=None,
    )


def everything(request: LLMRequest) -> str:
    parts = [request.instructions]
    parts.extend(item.text for item in request.items if isinstance(item, TextItem))
    return "\n".join(parts)


def shown_new(request: LLMRequest) -> list[int]:
    burst = next(
        item.text
        for item in request.items
        if isinstance(item, TextItem) and item.text.startswith("New messages to check:")
    )
    return [int(i) for i in re.findall(r"^\[msg:(\d+)\]", burst, flags=re.MULTILINE)]


def listed_monitors(request: LLMRequest) -> list[int]:
    return [int(i) for i in re.findall(r"\[monitor:(\d+)\]", everything(request))]


def verdicts(*items: tuple[int, bool, list[int]]) -> dict[str, Any]:
    return {
        "language": "en",
        "verdicts": [
            {"monitor_id": monitor_id, "matched": matched, "message_ids": ids}
            for monitor_id, matched, ids in items
        ],
    }


def match_all(request: LLMRequest) -> dict[str, Any]:
    """Every listed monitor matches the newest new message."""
    newest = max(shown_new(request))
    return verdicts(*((m, True, [newest]) for m in listed_monitors(request)))


async def evaluate_runs(env: Env) -> list[AgentRun]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(AgentRun)
                    .where(AgentRun.run_type == "evaluate_monitor")
                    .order_by(AgentRun.id)
                )
            )
            .scalars()
            .all()
        )


def delivery(env: Env, channel: FakeChannel) -> NotificationDelivery:
    """S05.2's delivery with the monitor renderer registered, as bootstrap does."""
    assert env.scheduler is not None
    service = NotificationDelivery(
        session_factory=env.factory,
        channel=channel,
        clock=env.clock,
        scheduler=env.scheduler,
        enabled=True,
    )
    renderer = monitors.MonitorMatchRenderer(env.clock)
    service.register_renderer(NotificationKind.MONITOR_MATCH, renderer)
    return service


async def set_mode(env: Env, conversation: int, mode: str) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = :m WHERE id = :c"),
            {"m": mode, "c": conversation},
        )


async def revoke(env: Env, kind: str) -> None:
    """Revoke one consent kind, committed (`/privacy revoke` revokes both)."""
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.consents SET revoked_at = :t WHERE kind = :k AND revoked_at IS NULL"),
            {"t": env.clock.now(), "k": kind},
        )


async def stop_now(env: Env, automation_id: int) -> monitors.StopResult:
    """The owner's Stop, committed."""
    async with transaction(env.factory) as uow:
        return await monitors.stop(
            uow.session,
            account_id=await account_id(env),
            automation_id=automation_id,
            clock=env.clock,
            actor="owner",
        )


async def wait_for_lock_waiter(env: Env, *, deadline: float = 15.0) -> None:
    """Until a backend of this database waits on a lock (a condition, not a
    timer; as in `test_notifications` and `test_summaries_verification`)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while True:
        async with env.factory() as session:
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return
        if loop.time() > end:
            raise AssertionError("no backend started waiting on a lock")
        await asyncio.sleep(0.05)


# ============================================================ deterministic
async def test_notify_me_when_daniel_replies_fires_exactly_once(env: Env) -> None:
    """PRD R2 #4: "notify me when Kamola replies" — two replies, one alert,
    and the monitor is completed."""
    a, _ = await setup(env)
    kamola = await contact_id(env)
    watched = await monitor(env, "contact_replied", contact_ids=[kamola], contact=kamola)
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH-A javob")
    env.clock.advance(minutes=1)
    await say(env, 3, "SYNTH-A yana")

    [alert] = await alerts(env)
    assert alert.conversation_id == a
    assert alert.contact_id == kamola
    assert alert.priority == "high"
    assert alert.dedupe_key == f"monitor:{watched}:{await message_id_of(env, 2)}"
    assert alert.refs["automation_ids"] == [watched]
    row = await automation(env, watched)
    assert row is not None
    assert (row.status, row.ended_at) == ("completed", env.clock.now() - timedelta(minutes=1))
    assert await events(env, "monitor_matched") == 1
    assert await events(env, "monitor_completed") == 1
    # Bobur writing never fires Kamola's monitor.
    await say(env, 2, "SYNTH-B javob", chat=OTHER_CONTACT)
    assert len(await alerts(env)) == 1


async def test_one_shot_monitor_delivers_after_quiet_hours_with_digest_off(env: Env) -> None:
    """R3-02 / D-S8: completing the monitor must not strand its held alert."""
    await setup(env)
    owner = await account_id(env)
    async with env.factory() as session, session.begin():
        account = await session.get(Account, owner)
        assert account is not None
        account.settings = {"digest_time": "off"}
    env.clock.set(datetime(2026, 10, 18, 23, 30, tzinfo=UTC))
    kamola = await contact_id(env)
    watched = await monitor(env, "contact_replied", contact_ids=[kamola], contact=kamola)
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH-A reply during quiet hours")
    [alert] = await alerts(env)
    completed = await automation(env, watched)
    assert completed is not None
    assert completed.status == "completed"
    assert alert.refs["automation_ids"] == [watched]
    original_refs = dict(alert.refs)
    channel = FakeChannel()
    service = delivery(env, channel)
    assert (await service.deliver(alert.id)).status == "deferred"
    [held] = await alerts(env)
    quiet_end = datetime(2026, 10, 19, 8, tzinfo=UTC)
    assert (held.status, held.deliver_after) == ("queued", quiet_end)
    assert channel.sends == []
    assert (await service.deliver(alert.id)).status == "not_due"
    env.clock.set(quiet_end)
    assert (await service.deliver(alert.id)).status == "sent"
    assert (await service.deliver(alert.id)).status == "not_queued"
    [sent] = await alerts(env)
    assert sent.refs == original_refs
    assert len(channel.sends) == 1
    assert "<b>Monitor:</b>" in channel.sends[0]
    assert "Kamola" in channel.sends[0]
    assert "SYNTH-A reply during quiet hours" not in channel.sends[0]


async def test_a_bypass_monitor_alerts_during_quiet_hours(env: Env) -> None:
    """A monitor that asked to reach the owner in quiet hours does (R9); one
    that did not waits for quiet hours to end. 23:30 UTC is inside the default
    quiet hours (23:00-08:00, the owner's timezone is UTC)."""
    await setup(env)
    env.clock.set(datetime(2026, 10, 18, 23, 30, tzinfo=UTC))
    loud = await monitor(env, "keywords", all_observed=True, terms=["P2LOUD"], bypass=True)
    quiet = await monitor(env, "keywords", all_observed=True, terms=["P2QUIET"])
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH P2LOUD")
    await say(env, 3, "SYNTH P2QUIET")
    by_monitor = {n.refs["automation_ids"][0]: n for n in await alerts(env)}
    assert by_monitor[loud].bypass_quiet_hours is True
    assert by_monitor[quiet].bypass_quiet_hours is False

    channel = FakeChannel()
    service = delivery(env, channel)
    sent = await service.deliver(by_monitor[loud].id)
    held = await service.deliver(by_monitor[quiet].id)
    assert (sent.status, held.status, held.reason) == ("sent", "deferred", "quiet_hours")
    [text_sent] = channel.sends
    assert "Monitor:" in text_sent
    assert "Kamola" in text_sent
    assert "SYNTH" not in text_sent  # never message text
    assert [b.label for b in channel.buttons[0]] == ["Open chat", "Useful", "Not useful"]


async def test_a_low_priority_contacts_monitor_alert_is_not_demoted(env: Env) -> None:
    """R9: the owner asked for this monitor explicitly, so a `low` contact's
    priority never lowers its alert (a `high` alert reaches the owner now)."""
    a, _ = await setup(env)
    kamola = await contact_id(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.contacts SET priority = 'low' WHERE id = :c"), {"c": kamola}
        )
    await monitor(env, "contact_replied", contact_ids=[kamola], contact=kamola)
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH-A javob")
    [alert] = await alerts(env)
    channel = FakeChannel()
    result = await delivery(env, channel).deliver(alert.id)
    assert (result.status, result.reason) == ("sent", None)
    assert len(channel.sends) == 1
    assert alert.conversation_id == a


async def test_monitor_never_borrows_an_explanation_created_after_its_match(env: Env) -> None:
    """A later covering verdict is not the verdict that triggered the event."""
    a, _ = await setup(env)
    kamola = await contact_id(env)
    monitor_id = await monitor(
        env,
        "contact_replied",
        contact_ids=[kamola],
        contact=kamola,
        action="alert_with_summary",
        lifecycle="standing",
    )
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH-A javob")
    matched = await message_id_of(env, 2)
    async with env.factory() as session, session.begin():
        session.add(
            ConversationTriage(
                account_id=await account_id(env),
                conversation_id=a,
                covers_through_message_id=matched,
                needs_reply=True,
                contains_question=False,
                requests_decision=True,
                urgency="high",
                escalation_category="needs_owner",
                reasons=["SYNTH-REASON <b>a decision</b>"],
                prompt_version="triage.v1",
                model="gpt-6-luna",
            )
        )
    async with transaction(env.factory) as uow:
        captured = await enqueue(
            uow.session,
            account_id=await account_id(env),
            kind=NotificationKind.MONITOR_MATCH,
            priority=Priority.HIGH,
            dedupe_key="synthetic:captured-monitor-verdict",
            refs=NotificationRefs(
                conversation_ids=(a,),
                message_ids=(matched,),
                automation_ids=(monitor_id,),
            ),
            clock=env.clock,
            conversation_id=a,
        )
    assert captured is not None
    await say(env, 3, "SYNTH-A yana")
    first, _, second = await alerts(env)
    async with env.factory() as session, session.begin():
        verdict = await session.scalar(
            select(ConversationTriage).where(ConversationTriage.conversation_id == a)
        )
        assert verdict is not None
        verdict.covers_through_message_id = await message_id_of(env, 3)
        verdict.reasons = ["SYNTH-LATER settled"]
    channel = FakeChannel()
    service = delivery(env, channel)
    assert (await service.deliver(first.id)).status == "sent"
    assert (await service.deliver(second.id)).status == "sent"
    assert (await service.deliver(captured)).status == "sent"
    covered, stale, original = channel.sends
    assert "SYNTH-REASON" not in covered
    assert "original explanation is no longer available" in covered
    assert "SYNTH-REASON" not in stale  # the verdict predates message 3
    assert "• SYNTH-REASON &lt;b&gt;a decision&lt;/b&gt;" in original
    assert "SYNTH-LATER" not in original
    assert "superseded or resolved" in original
    for sent in (covered, stale, original):
        assert "javob" not in sent
        assert "yana" not in sent


async def test_a_standing_monitors_alert_carries_stop_and_other_alerts_are_unchanged(
    env: Env,
) -> None:
    """ICR-4 (S05.4 §3.7): a standing monitor's alert has S05.2's three
    buttons and then "Stop monitor" (its own single-use `mon` card); a
    one-shot monitor's alert and a triage alert keep exactly S05.2's three;
    a system alert (no `ntf` card: the other branch ICR-4 edited, Jack on
    #116) still has no buttons at all."""
    a, _ = await setup(env)
    standing = await monitor(
        env, "keywords", all_observed=True, terms=["P2KEEP"], lifecycle="standing"
    )
    await monitor(env, "keywords", all_observed=True, terms=["P2ONCE"])
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH P2KEEP")
    await say(env, 3, "SYNTH P2ONCE")
    async with env.factory() as session, session.begin():
        session.add(
            ConversationTriage(
                account_id=await account_id(env),
                conversation_id=a,
                covers_through_message_id=await message_id_of(env, 3),
                needs_reply=True,
                contains_question=False,
                requests_decision=False,
                urgency="normal",
                escalation_category="needs_owner",
                reasons=["SYNTH-REASON"],
                prompt_version="triage.v1",
                model="gpt-6-luna",
            )
        )
    async with transaction(env.factory) as uow:
        triage_alert = await enqueue(
            uow.session,
            account_id=await account_id(env),
            kind=NotificationKind.TRIAGE_NEEDS_REPLY,
            priority=Priority.HIGH,
            dedupe_key="triage:synthetic",
            refs=NotificationRefs(conversation_ids=(a,)),
            clock=env.clock,
            conversation_id=a,
        )
    assert triage_alert is not None
    async with transaction(env.factory) as uow:
        system_alert = await enqueue(
            uow.session,
            account_id=await account_id(env),
            kind=NotificationKind.SYSTEM,
            priority=Priority.HIGH,
            dedupe_key="system:synthetic",
            refs=NotificationRefs(conversation_ids=(a,)),
            clock=env.clock,
            alert_class=AlertClass.SYSTEM,
            conversation_id=a,
        )
    assert system_alert is not None

    async def render_system(session: Any, notification: Notification, account: Any) -> Any:
        # S05.2 has no built-in system renderer yet (CP4); this one names a
        # chat, so only the alert class keeps the `ntf` card off.
        return RenderedAlert("SYNTH system notice", a)

    kept, once = await alerts(env)
    channel = FakeChannel()
    service = delivery(env, channel)
    service.register_renderer(NotificationKind.SYSTEM, render_system)
    for notification_id in (kept.id, once.id, triage_alert, system_alert):
        assert (await service.deliver(notification_id)).status == "sent"
    s052 = ["Open chat", "Useful", "Not useful"]
    assert [[b.label for b in row] for row in channel.buttons] == [
        [*s052, "Stop monitor"],
        s052,
        s052,
        [],
    ]
    async with env.factory() as session:
        [card] = (
            await session.execute(select(PendingInput).where(PendingInput.kind == "mon"))
        ).scalars()
    assert card.ref_id == standing
    assert card.prompt_message_id is None  # an alert's card: the alert is never edited
    assert channel.buttons[0][-1].callback_data == f"mon:{card.token}:stop"


async def test_two_messages_of_one_burst_complete_a_one_shot_once(env: Env) -> None:
    """Two matching messages in one batch, and a second match recorded for an
    already completed monitor: one alert, one completion (exactly once)."""
    await setup(env)
    watched = await monitor(env, "keywords", all_observed=True, terms=["P2KEY"])
    env.clock.advance(minutes=1)
    when = env.clock.now()
    await deliver(
        env,
        business_message(message_id=2, message_text="SYNTH P2KEY one", date=ts(when)),
        business_message(message_id=3, message_text="SYNTH P2KEY two", date=ts(when)),
    )
    assert len(await alerts(env)) == 1
    async with transaction(env.factory) as uow:
        assert not await monitors.complete_one_shot(
            uow.session,
            account_id=await account_id(env),
            automation_id=watched,
            now=env.clock.now(),
        )
    assert await events(env, "monitor_completed") == 1
    row = await automation(env, watched)
    assert row is not None
    assert row.status == "completed"


async def test_a_keyword_monitor_matches_across_scripts_and_apostrophes(env: Env) -> None:
    """Both sides go through `memory.search.normalize`: a Latin term with a
    modifier apostrophe matches Cyrillic text, a Cyrillic term matches
    apostrophe-less Latin, and a phrase matches consecutive words."""
    await setup(env)
    teacher = await monitor(
        env, "keywords", all_observed=True, terms=["oʻqituvchi"], lifecycle="standing"
    )
    city = await monitor(env, "keywords", all_observed=True, terms=["Тошкент"])
    place = await monitor(env, "keywords", all_observed=True, terms=["uchrashuv joyi"])
    env.clock.advance(minutes=1)
    await say(env, 2, "Ўқитувчи келди")
    await say(env, 3, "ertaga toshkentga emas, Toshkent markaziga boramiz")
    await say(env, 4, "Учрашув жойи ўзгарди!")
    fired = sorted(n.refs["automation_ids"][0] for n in await alerts(env))
    assert fired == sorted([teacher, city, place])


async def test_a_keyword_monitor_ignores_non_matching_messages(env: Env) -> None:
    """Whole words only, in order: a longer word, split words and the words of
    a phrase out of order are no match."""
    await setup(env)
    watched = await monitor(env, "keywords", all_observed=True, terms=["P2KEY", "kun tartibi"])
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH P2KEYS and p2 key")
    await say(env, 3, "tartibi kun emas")
    await say(env, 4, None, extra={"photo": [{"file_unique_id": "u1", "width": 1, "height": 1}]})
    assert await alerts(env) == []
    row = await automation(env, watched)
    assert row is not None
    assert row.status == "active"


async def test_a_media_monitor_matches_a_photo(env: Env) -> None:
    await setup(env)
    photos = await monitor(env, "media_received", all_observed=True, media_kinds=["photo"])
    anything = await monitor(
        env, "media_received", all_observed=True, media_kinds=["any"], lifecycle="standing"
    )
    voice = await monitor(env, "media_received", all_observed=True, media_kinds=["voice"])
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH plain text")
    await say(env, 3, None, extra={"photo": [{"file_unique_id": "u1", "width": 1, "height": 1}]})
    fired = sorted(n.refs["automation_ids"][0] for n in await alerts(env))
    assert fired == sorted([photos, anything])
    row = await automation(env, voice)
    assert row is not None
    assert row.status == "active"


# ============================================================ semantic
async def test_a_semantic_monitor_fires_on_a_fake_llm_verdict(env: Env) -> None:
    a, _ = await setup(env)
    watched = await monitor(
        env, "semantic", conversation_ids=[a], description="the delivery date changes"
    )
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH yetkazib berish sanasi o'zgardi")
    # No model at ingestion: the flag is set and one evaluation is queued.
    assert await flag(env, a) is not None
    assert await monitor_jobs(env) == [(a, "todo")]
    assert await alerts(env) == []

    fake = FakeLLM()
    fake.parsed = [match_all]
    outcome = await evaluator(env, fake).evaluate(a)
    assert (outcome.status, outcome.calls, outcome.alerts) == (MonitorEvalStatus.EVALUATED, 1, 1)
    [alert] = await alerts(env)
    newest = await message_id_of(env, 2)
    assert alert.dedupe_key == f"monitor:{watched}:{newest}"
    assert alert.refs["message_ids"] == [newest]
    assert await flag(env, a) is None
    [run] = await evaluate_runs(env)
    assert (run.status, run.prompt_version, run.trigger_ref) == (
        "succeeded",
        prompt_registry.active("evaluate_monitor").label,
        f"monitor:conv:{a}:through:{newest}",
    )
    row = await automation(env, watched)
    assert row is not None
    assert row.status == "completed"


async def test_three_semantic_monitors_in_scope_make_exactly_one_evaluate_call_per_burst(
    env: Env,
) -> None:
    """IMPLEMENTATION_PLAN S05.4: one call per burst for every semantic
    monitor of the chat — through the job, after the trailing debounce."""
    a, _ = await setup(env)
    ids = [
        await monitor(env, "semantic", all_observed=True, description="a new price"),
        await monitor(env, "decision_requested", conversation_ids=[a]),
        await monitor(env, "urgent", contact_ids=[await contact_id(env)], lifecycle="standing"),
    ]
    keyword = await monitor(env, "keywords", all_observed=True, terms=["P2NEVER"])
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH narx o'zgardi")
    env.clock.advance(seconds=5)
    await say(env, 3, "SYNTH bugun hal qilish kerak")
    fake = FakeLLM()
    fake.parsed = [match_all]
    assert env.scheduler is not None
    job = MonitorJobs(
        session_factory=env.factory,
        clock=env.clock,
        scheduler=env.scheduler,
        debounce=DEBOUNCE,
        evaluator=evaluator(env, fake),
    )
    # Still inside the debounce: the job re-defers instead of calling.
    assert await job.evaluate(a) is None
    assert fake.requests == []
    env.clock.advance(seconds=DEBOUNCE.total_seconds())
    outcome = await job.evaluate(a)
    assert outcome is not None
    assert outcome.status is MonitorEvalStatus.EVALUATED

    [request] = fake.requests
    assert sorted(listed_monitors(request)) == sorted(ids)
    assert keyword not in listed_monitors(request)
    assert shown_new(request) == [await message_id_of(env, 2), await message_id_of(env, 3)]
    assert sorted(n.refs["automation_ids"][0] for n in await alerts(env)) == sorted(ids)


async def test_a_verdict_citing_no_contact_message_does_not_fire(env: Env) -> None:
    """A match must cite a shown NEW contact message: no citation, the
    owner's message, an earlier message or an id not shown is no match."""
    a, _ = await setup(env)
    watched = await monitor(env, "semantic", all_observed=True, description="a payment")
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH to'lov")
    await say(env, 3, "SYNTH owner text", sender=OWNER)
    first = await message_id_of(env, 1)
    owners = await message_id_of(env, 3)
    fake = FakeLLM()
    fake.parsed = [verdicts((watched, True, [])), verdicts((watched, True, [owners, first, 10**9]))]
    for number in (2, 4):
        if number == 4:  # the first answer reviewed message 2: a new message is next
            env.clock.advance(seconds=5)
            await say(env, 4, "SYNTH to'lov yana")
        outcome = await evaluator(env, fake).evaluate(a)
        assert outcome.status is MonitorEvalStatus.EVALUATED
        assert outcome.alerts == 0
    assert await alerts(env) == []
    row = await automation(env, watched)
    assert row is not None
    assert row.status == "active"


async def test_verdicts_for_unknown_monitor_ids_are_dropped(env: Env) -> None:
    """Only listed monitors: another id — unknown, or a deterministic
    monitor's — is dropped; a missing verdict is no match."""
    a, _ = await setup(env)
    listed = await monitor(env, "semantic", all_observed=True, description="a contract")
    unlisted = await monitor(env, "keywords", all_observed=True, terms=["P2NEVER"])
    other = await monitor(env, "urgent", all_observed=True)
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH shartnoma")
    newest = await message_id_of(env, 2)
    fake = FakeLLM()
    fake.parsed = [verdicts((10**9, True, [newest]), (unlisted, True, [newest]))]
    outcome = await evaluator(env, fake).evaluate(a)
    assert outcome.status is MonitorEvalStatus.EVALUATED
    assert sorted(listed_monitors(fake.requests[0])) == sorted([listed, other])
    assert await alerts(env) == []


async def test_monitors_ignore_ignore_mode_chats(env: Env) -> None:
    a, b = await setup(env)
    await monitor(env, "keywords", all_observed=True, terms=["P2KEY"], lifecycle="standing")
    await monitor(env, "semantic", conversation_ids=[a], description="anything")
    await set_mode(env, a, "ignore")
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH P2KEY")
    assert await alerts(env) == []
    assert await flag(env, a) is None
    async with env.factory() as session:
        assert (
            await monitors.applicable(
                session, account_id=await account_id(env), conversation_id=a, now=env.clock.now()
            )
            == []
        )
    # The evaluation job skips an Ignore-mode chat even with a flag left set.
    fake = FakeLLM()
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET pending_monitor_at = :t WHERE id = :c"),
            {"t": env.clock.now(), "c": a},
        )
    assert (await evaluator(env, fake).evaluate(a)).status is MonitorEvalStatus.SKIPPED
    assert fake.requests == []
    # The Observe chat still matches.
    await say(env, 2, "SYNTH P2KEY", chat=OTHER_CONTACT)
    [alert] = await alerts(env)
    assert alert.conversation_id == b


async def test_owner_messages_never_match(env: Env) -> None:
    a, _ = await setup(env)
    kamola = await contact_id(env)
    await monitor(env, "contact_replied", contact_ids=[kamola], contact=kamola)
    await monitor(env, "keywords", all_observed=True, terms=["P2KEY"])
    await monitor(env, "semantic", all_observed=True, description="anything")
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH P2KEY from the owner", sender=OWNER)
    assert await alerts(env) == []
    assert await flag(env, a) is None
    assert await monitor_jobs(env) == []


async def test_a_backlog_message_older_than_the_monitor_never_fires_it(env: Env) -> None:
    """TL decision (S05.4 §3.3): a message sent before the monitor existed —
    delivered late — never fires it, deterministic or semantic."""
    a, _ = await setup(env)
    kamola = await contact_id(env)
    await monitor(env, "contact_replied", contact_ids=[kamola], contact=kamola)
    semantic = await monitor(env, "semantic", all_observed=True, description="anything")
    env.clock.advance(minutes=5)
    await say(env, 2, "SYNTH-A eski xabar", at=env.clock.now() - timedelta(minutes=10))
    assert await alerts(env) == []
    assert await flag(env, a) is None
    # Even with a flag set by hand, an evaluation never calls the model for it.
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET pending_monitor_at = :t WHERE id = :c"),
            {"t": env.clock.now() - timedelta(minutes=1), "c": a},
        )
    fake = FakeLLM()
    outcome = await evaluator(env, fake).evaluate(a)
    assert outcome.status is MonitorEvalStatus.UP_TO_DATE
    assert fake.requests == []
    assert await flag(env, a) is None
    row = await automation(env, semantic)
    assert row is not None
    assert row.status == "active"


async def test_an_expired_monitor_never_fires(env: Env) -> None:
    await setup(env)
    expiring = await monitor(
        env,
        "keywords",
        all_observed=True,
        terms=["P2KEY"],
        expires_at=env.clock.now() + timedelta(minutes=1),
    )
    env.clock.advance(minutes=2)
    # Past its expiry, before the hourly sweep: matching ignores it already.
    await say(env, 2, "SYNTH P2KEY")
    assert await alerts(env) == []
    assert env.scheduler is not None
    job = MonitorJobs(
        session_factory=env.factory, clock=env.clock, scheduler=env.scheduler, debounce=DEBOUNCE
    )
    assert await job.expire() == 1
    row = await automation(env, expiring)
    assert row is not None
    assert (row.status, row.ended_at) == ("expired", env.clock.now())
    assert await events(env, "monitor_expired") == 1
    assert await job.expire() == 0


async def test_standing_monitors_keep_firing_and_stop_on_stop(env: Env) -> None:
    await setup(env)
    standing = await monitor(
        env, "keywords", all_observed=True, terms=["P2KEY"], lifecycle="standing"
    )
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH P2KEY one")
    await say(env, 3, "SYNTH P2KEY two")
    assert len(await alerts(env)) == 2
    async with transaction(env.factory) as uow:
        stopped = await monitors.stop(
            uow.session,
            account_id=await account_id(env),
            automation_id=standing,
            clock=env.clock,
            actor="owner",
        )
    assert stopped is monitors.StopResult.STOPPED
    await say(env, 4, "SYNTH P2KEY three")
    assert len(await alerts(env)) == 2
    row = await automation(env, standing)
    assert row is not None
    assert row.status == "cancelled"


async def test_an_alert_queued_before_stop_is_not_delivered_after_it(env: Env) -> None:
    """Invariant 11 (Antony and Jack on #116): the Stop receipt says the
    monitor "will not alert you again", so an alert queued before the Stop
    (held by quiet hours, or not yet delivered) is suppressed at delivery.
    A one-shot monitor is `completed` by its match, not cancelled: its
    queued alert is still delivered."""
    await setup(env)
    standing = await monitor(
        env, "keywords", all_observed=True, terms=["P2KEEP"], lifecycle="standing"
    )
    once = await monitor(env, "keywords", all_observed=True, terms=["P2ONCE"])
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH P2KEEP")
    await say(env, 3, "SYNTH P2ONCE")
    queued = {n.refs["automation_ids"][0]: n.id for n in await alerts(env)}
    assert await stop_now(env, standing) is monitors.StopResult.STOPPED
    assert await stop_now(env, once) is monitors.StopResult.ALREADY_ENDED

    channel = FakeChannel()
    service = delivery(env, channel)
    stopped = await service.deliver(queued[standing])
    assert (stopped.status, stopped.reason) == ("suppressed", "source_deleted")
    assert (await service.deliver(queued[once])).status == "sent"
    [sent] = channel.sends
    assert "P2ONCE" in sent


async def test_a_monitor_stopped_during_the_evaluation_does_not_fire(env: Env) -> None:
    """Jack on #116: the owner stops a semantic monitor while its evaluation
    is in flight; the answer still matches it, but the effects re-read the
    monitor under the locks: no alert, the monitor stays cancelled, and the
    burst counts as evaluated."""
    a, _ = await setup(env)
    watched = await monitor(env, "semantic", conversation_ids=[a], description="a new price")
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH narx o'zgardi")

    async def stop_then_match(request: LLMRequest) -> dict[str, Any]:
        assert await stop_now(env, watched) is monitors.StopResult.STOPPED
        return match_all(request)

    fake = FakeLLM()
    fake.parsed = [stop_then_match]
    outcome = await evaluator(env, fake).evaluate(a)
    assert (outcome.status, outcome.calls, outcome.alerts) == (MonitorEvalStatus.EVALUATED, 1, 0)
    assert await alerts(env) == []
    row = await automation(env, watched)
    assert row is not None
    assert row.status == "cancelled"
    assert await flag(env, a) is None


async def test_two_transactions_complete_a_one_shot_once(env: Env) -> None:
    """Jack and Antony on #116: two separate transactions race to complete
    one one-shot monitor. The second blocks on the row lock and, once the
    first commits, finds it no longer active: exactly one completion."""
    await setup(env)
    watched = await monitor(env, "keywords", all_observed=True, terms=["P2KEY"])
    owner = await account_id(env)

    async def complete() -> bool:
        async with transaction(env.factory) as uow:
            return await monitors.complete_one_shot(
                uow.session, account_id=owner, automation_id=watched, now=env.clock.now()
            )

    async with env.factory() as first, first.begin():
        assert await monitors.complete_one_shot(
            first, account_id=owner, automation_id=watched, now=env.clock.now()
        )
        second = asyncio.create_task(complete())
        await wait_for_lock_waiter(env)
        assert not second.done()
    assert await second is False
    row = await automation(env, watched)
    assert row is not None
    assert row.status == "completed"


async def test_the_reconciler_recovers_a_lost_evaluation(env: Env) -> None:
    a, _ = await setup(env)
    await monitor(env, "semantic", all_observed=True, description="anything")
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH xabar")
    # The defer was lost (a crash right after the ingestion commit).
    async with env.factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM procrastinate.procrastinate_jobs WHERE task_name = :t"),
            {"t": MONITOR_TASK},
        )
    reconcile = monitor_reconciler()
    async with env.factory() as session:
        assert await reconcile(session, env.clock.now()) == []  # its own defer may still come
    env.clock.advance(seconds=31)
    async with env.factory() as session:
        [request] = await reconcile(session, env.clock.now())
    assert (request.task, dict(request.args), request.queueing_lock) == (
        MONITOR_TASK,
        {"conversation_id": a},
        f"mon:{a}",
    )
    # A queued job is not duplicated; a chat without the flag is not deferred.
    assert env.scheduler is not None
    await env.scheduler.defer(request.task, args=request.args, queueing_lock=request.queueing_lock)
    async with env.factory() as session:
        assert await reconcile(session, env.clock.now()) == []


# ============================================================ the run's guarantees
async def test_evaluate_monitor_runs_have_no_tools(env: Env) -> None:
    """RG-17 (§6 invariants 2, 3a): one structured call with no tools, and
    the registry offers the run type nothing."""
    a, _ = await setup(env)
    await monitor(env, "semantic", all_observed=True, description="anything")
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH xabar")
    fake = FakeLLM()
    fake.parsed = [match_all]
    await evaluator(env, fake).evaluate(a)
    [request] = fake.requests
    assert request.tools == ()
    assert request.purpose == "evaluate_monitor"
    registry = default_registry()
    every_flag = frozenset(tool.release_flag for tool in registry.all())
    assert (
        registry.for_run(
            "evaluate_monitor",
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=every_flag,
            managed_consent=True,
        )
        == {}
    )


async def test_evaluate_monitor_sees_only_its_own_conversation(env: Env) -> None:
    """§6 invariant 4: one conversation per call — its messages, its
    contact's name (once, wrapped). FakeLLM inspects every prompt."""
    a, b = await setup(env)
    await monitor(env, "semantic", all_observed=True, description="anything", lifecycle="standing")
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH-A yangi")
    await say(env, 2, "SYNTH-B yangi", chat=OTHER_CONTACT)
    fake = FakeLLM()
    fake.parsed = [match_all, match_all]
    await evaluator(env, fake).evaluate(a)
    await evaluator(env, fake).evaluate(b)
    first, second = fake.requests
    for request, own, other, own_text, other_text in (
        (first, "Kamola", "Bobur", "SYNTH-A", "SYNTH-B"),
        (second, "Bobur", "Kamola", "SYNTH-B", "SYNTH-A"),
    ):
        body = everything(request)
        assert own_text in body
        assert other_text not in body
        assert f'<untrusted source="contact_name" trust="untrusted">{own}</untrusted>' in body
        assert body.count(own) == 1
        assert other not in body
    assert {n.conversation_id for n in await alerts(env)} == {a, b}


async def test_without_managed_consent_no_evaluation_call_is_made(env: Env) -> None:
    """RG-08 (invariant 9): without `ai_managed` nothing reaches the provider
    and no run row is written; the flag stays for when consent returns."""
    a, _ = await setup(env, managed=False)
    await monitor(env, "semantic", all_observed=True, description="anything")
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH xabar")
    fake = FakeLLM()
    outcome = await evaluator(env, fake).evaluate(a)
    assert outcome.status is MonitorEvalStatus.NO_CONSENT
    assert fake.requests == []
    assert await evaluate_runs(env) == []
    assert await alerts(env) == []
    # ...and the reconciler does not churn on it every minute.
    env.clock.advance(minutes=5)
    async with env.factory() as session:
        assert await monitor_reconciler()(session, env.clock.now()) == []


async def revoked_mid_call(env: Env, kind: str) -> tuple[int, int, FakeLLM]:
    """A semantic monitor and a matching message; the model's answer is a
    match, returned after `kind` was revoked and committed while the call
    was in flight. Returns the chat, the monitor and the fake."""
    a, _ = await setup(env)
    watched = await monitor(env, "semantic", conversation_ids=[a], description="a new price")
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH narx o'zgardi")

    async def revoke_then_match(request: LLMRequest) -> dict[str, Any]:
        await revoke(env, kind)
        return match_all(request)

    fake = FakeLLM()
    fake.parsed = [revoke_then_match]
    return a, watched, fake


async def state(env: Env, conversation: int, automation_id: int) -> tuple[str, int, bool]:
    """(the monitor's status, its alerts, whether the chat's flag is kept)."""
    row = await automation(env, automation_id)
    assert row is not None
    return row.status, len(await alerts(env)), await flag(env, conversation) is not None


@pytest.mark.parametrize("kind", ["ai_managed", "ai_control"])
async def test_consent_revoked_during_the_evaluation_applies_nothing(env: Env, kind: str) -> None:
    """Invariant 9 / FR-ONB-06 (Antony and Jack on #116, blocking): consent
    revoked while the call is in flight. The call itself was allowed, but
    its answer must not take effect: the effects re-check both consents
    under the §9.4 locks — no completion, no alert, the flag kept for a
    re-grant — and the run closes as not succeeded (as triage's effects,
    `inbox/triage_store.py`)."""
    a, watched, fake = await revoked_mid_call(env, kind)
    outcome = await evaluator(env, fake).evaluate(a)
    assert (outcome.status, outcome.calls, outcome.alerts) == (MonitorEvalStatus.NO_CONSENT, 1, 0)
    assert await state(env, a, watched) == ("active", 0, True)
    [run] = await evaluate_runs(env)
    assert (run.status, run.error_class) == ("failed", "discarded:no_consent")


async def test_a_match_discarded_for_revoked_consent_fires_once_consent_returns(
    env: Env,
) -> None:
    """The recovery side: while consent stays revoked, the kept flag makes no
    call; once the owner grants `ai_managed` again, the next evaluation
    reads the same burst and the match fires, once."""
    a, watched, fake = await revoked_mid_call(env, "ai_managed")
    assert (await evaluator(env, fake).evaluate(a)).status is MonitorEvalStatus.NO_CONSENT
    assert (await evaluator(env, fake).evaluate(a)).status is MonitorEvalStatus.NO_CONSENT
    assert len(fake.requests) == 1  # no call while revoked

    await grant_managed(env)
    fake.parsed = [match_all]
    outcome = await evaluator(env, fake).evaluate(a)
    assert (outcome.status, outcome.alerts) == (MonitorEvalStatus.EVALUATED, 1)
    assert shown_new(fake.requests[-1]) == [await message_id_of(env, 2)]
    assert await state(env, a, watched) == ("completed", 1, False)
    assert [run.status for run in await evaluate_runs(env)] == ["failed", "succeeded"]


async def test_a_deleted_chat_removes_its_monitors_and_suppresses_pending_alerts(
    env: Env,
) -> None:
    """The F0 cascade deletes a monitor that names the chat (its `refs`) and
    the chat's queued alerts; an alert whose monitor is gone renders to
    nothing (`source_deleted`), and a monitor of every chat stays."""
    a, _ = await setup(env)
    kamola = await contact_id(env)
    scoped = await monitor(env, "keywords", conversation_ids=[a], terms=["P2KEY"])
    by_contact = await monitor(env, "contact_replied", contact_ids=[kamola], contact=kamola)
    everywhere = await monitor(
        env, "keywords", all_observed=True, terms=["P2KEY"], lifecycle="standing"
    )
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH P2KEY")
    assert len(await alerts(env)) == 3
    owner = await account_id(env)
    async with transaction(env.factory) as uow:
        await deletion.delete_conversation_data(
            uow.session, account_id=owner, conversation_id=a, actor="owner", clock=env.clock
        )
    assert await automation(env, scoped) is None
    assert await automation(env, by_contact) is None
    assert await automation(env, everywhere) is not None
    assert await alerts(env) == []

    # An alert queued for a monitor deleted since is suppressed at delivery.
    await say(env, 3, "SYNTH P2KEY", chat=OTHER_CONTACT)
    [alert] = await alerts(env)
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM app.automations WHERE id = :i"), {"i": everywhere})
    channel = FakeChannel()
    result = await delivery(env, channel).deliver(alert.id)
    assert (result.status, result.reason) == ("suppressed", "source_deleted")
    assert channel.sends == []


# ============================================================ I4: reliable review
# A monitor's review (C7) has its own cursor: oldest unreviewed messages first,
# failures counted on its row (bounded, then degraded and recoverable), a source
# changed during the call never settled as current.


async def progress_row(env: Env, monitor_id: int) -> ReviewProgress:
    async with env.factory() as session:
        return (
            await session.execute(
                select(ReviewProgress).where(
                    ReviewProgress.monitor_id == monitor_id, ReviewProgress.reader == "monitor"
                )
            )
        ).scalar_one()


async def review_state(env: Env, monitor_id: int) -> monitor_review.MonitorReview:
    async with env.factory() as session:
        found = await monitor_review.states(
            session,
            account_id=await account_id(env),
            monitor_ids=[monitor_id],
            now=env.clock.now(),
        )
    return found[monitor_id]


async def revision(env: Env, conversation: int) -> int:
    async with env.factory() as session:
        return int(
            (
                await session.execute(
                    text("SELECT content_revision FROM app.conversations WHERE id = :c"),
                    {"c": conversation},
                )
            ).scalar_one()
        )


async def edit(env: Env, telegram_id: int, body: str) -> None:
    await deliver(
        env,
        business_message(
            message_id=telegram_id,
            message_text=body,
            date=ts(env.clock.now()),
            first_name="Kamola",
            edit=True,
        ),
    )


async def job_times(env: Env) -> list[tuple[str, datetime]]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT status::text, scheduled_at FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = :t ORDER BY id"
            ),
            {"t": MONITOR_TASK},
        )
        return [(str(r[0]), r[1]) for r in rows.all()]


async def one_message_watch(env: Env, *, lifecycle: str = "one_shot") -> tuple[int, int]:
    """A chat, a semantic monitor confirmed now and one message after it."""
    a, _ = await setup(env)
    watched = await monitor(
        env, "semantic", conversation_ids=[a], description="a refund", lifecycle=lifecycle
    )
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH qaytarish kerak")
    return a, watched


async def test_a_backlog_is_drained_oldest_first_and_no_message_is_skipped(env: Env) -> None:
    """R3-09: more messages than one call reads. The oldest `BURST_LIMIT` are
    read first, the cursor stops at the last one read (never at the newest
    message of the chat), and the next call reads the rest, a match in the
    last one included."""
    a, _ = await setup(env)
    watched = await monitor(env, "semantic", conversation_ids=[a], description="a refund")
    env.clock.advance(minutes=1)
    total = BURST_LIMIT + 5
    for number in range(2, 2 + total):
        await say(env, number, f"SYNTH xabar {number}")
    ids = [await message_id_of(env, number) for number in range(2, 2 + total)]

    fake = FakeLLM()
    fake.parsed = [verdicts((watched, False, [])), match_all]
    first = await evaluator(env, fake).evaluate(a)
    assert (first.status, first.backlog) == (MonitorEvalStatus.EVALUATED, True)
    assert shown_new(fake.requests[0]) == ids[:BURST_LIMIT]
    row = await progress_row(env, watched)
    assert (row.position or {}).get("message_id") == ids[BURST_LIMIT - 1]
    assert (row.status, row.attempts) == ("pending", 0)
    assert await flag(env, a) is not None  # work is still waiting

    second = await evaluator(env, fake).evaluate(a)
    assert (second.status, second.alerts) == (MonitorEvalStatus.EVALUATED, 1)
    assert shown_new(fake.requests[1]) == ids[BURST_LIMIT:]
    [alert] = await alerts(env)
    assert alert.refs["message_ids"] == [ids[-1]]
    assert await flag(env, a) is None
    assert (await progress_row(env, watched)).status == "current"


FAILURES: list[tuple[str, Any, str]] = [
    ("refusal", StopReason.REFUSAL, "refusal"),
    ("content_filter", StopReason.CONTENT_FILTER, "refusal"),
    ("invalid_output", LLMError(LLMErrorKind.INVALID_OUTPUT), "invalid_output"),
    ("provider_outage", LLMError(LLMErrorKind.UNAVAILABLE), "provider_failed"),
]


@pytest.mark.parametrize(("label", "failure", "last_error"), FAILURES, ids=[f[0] for f in FAILURES])
async def test_a_failed_review_gives_up_no_message_and_reads_it_again(
    env: Env, label: str, failure: Any, last_error: str
) -> None:
    """R3-09: a refusal, a filtered or invalid answer or an outage no longer
    settles the flag. The attempt is counted on the monitor's row, the
    cursor stays, the flag stays, and after the backoff the same message is
    read again and fires."""
    a, watched = await one_message_watch(env)
    newest = await message_id_of(env, 2)
    fake = FakeLLM()
    fake.parsed = [failure, match_all]
    failed = await evaluator(env, fake).evaluate(a)
    assert failed.status in (MonitorEvalStatus.FAILED, MonitorEvalStatus.REFUSED)
    assert failed.retry_at == env.clock.now() + monitor_review.RETRY_DELAYS[0]
    row = await progress_row(env, watched)
    assert (row.position, row.attempts, row.last_error, row.status) == (
        None,
        1,
        last_error,
        "pending",
    )
    assert await flag(env, a) is not None
    assert (await review_state(env, watched)).state == "delayed"
    assert await alerts(env) == []

    # Within the backoff nothing is called; after it the message is read again.
    waiting = await evaluator(env, fake).evaluate(a)
    assert (waiting.status, waiting.retry_at) == (MonitorEvalStatus.DEFERRED, failed.retry_at)
    assert len(fake.requests) == 1
    env.clock.advance(seconds=61)
    again = await evaluator(env, fake).evaluate(a)
    assert (again.status, again.alerts) == (MonitorEvalStatus.EVALUATED, 1)
    assert shown_new(fake.requests[1]) == [newest]
    assert await flag(env, a) is None
    row = await progress_row(env, watched)
    assert (row.status, row.attempts, row.last_error, row.retry_at) == ("current", 0, None, None)


async def test_exhausted_attempts_leave_the_range_inspectable_and_recoverable(env: Env) -> None:
    """Retries are bounded (3 attempts per batch, never a hot loop): the monitor
    is degraded, its unreviewed range stays on record and in the owner's
    monitor list, no further call is made, and an explicit retry reads the
    original message and fires."""
    a, watched = await one_message_watch(env)
    newest = await message_id_of(env, 2)
    fake = FakeLLM()
    fake.parsed = [LLMError(LLMErrorKind.INVALID_OUTPUT)] * monitor_review.MAX_ATTEMPTS
    for delay in (*monitor_review.RETRY_DELAYS, None):
        outcome = await evaluator(env, fake).evaluate(a)
        assert outcome.status is MonitorEvalStatus.FAILED
        if delay is not None:
            assert outcome.retry_at == env.clock.now() + delay
            env.clock.advance(seconds=delay.total_seconds() + 1)
    assert outcome.retry_at is None
    row = await progress_row(env, watched)
    assert (row.status, row.attempts, row.retry_at, row.position) == ("degraded", 3, None, None)
    assert row.pending_ranges == [
        {"first_message_id": newest, "last_message_id": newest, "reason": "invalid_output"}
    ]
    assert await flag(env, a) is None  # no wake-ups for a monitor that is out of attempts
    degraded = await review_state(env, watched)
    assert (degraded.state, degraded.reasons) == ("degraded", ("invalid_output",))
    assert [(r.status, r.error_class) for r in await evaluate_runs(env)] == [
        ("failed", "invalid_output")
    ] * 3
    automation_row = await automation(env, watched)
    assert automation_row is not None
    assert automation_row.status == "active"  # still on, and now visibly degraded

    # No paid loop: nothing is called again, however often it is evaluated.
    env.clock.advance(hours=3)
    quiet = await evaluator(env, fake).evaluate(a)
    assert (quiet.status, len(fake.requests)) == (MonitorEvalStatus.UP_TO_DATE, 3)

    # Explicit recovery: fresh attempts, the original message, one alert.
    async with transaction(env.factory) as uow:
        chats = await monitor_review.request_retry(
            uow.session, account_id=await account_id(env), now=env.clock.now(), monitor_id=watched
        )
    assert chats == [a]
    assert await flag(env, a) is not None
    fake.parsed = [match_all]
    recovered = await evaluator(env, fake).evaluate(a)
    assert (recovered.status, recovered.alerts) == (MonitorEvalStatus.EVALUATED, 1)
    assert shown_new(fake.requests[-1]) == [newest]
    assert (await review_state(env, watched)).state == "current"


async def test_a_message_edited_during_the_call_is_not_settled_as_current(env: Env) -> None:
    """C6/C7: the contact edits the message while the model reads it. The
    answer (a match for the old text) applies nothing and certifies nothing;
    the message is read again, in its edited form."""
    a, watched = await one_message_watch(env)
    first_revision = await revision(env, a)

    async def edit_then_match(request: LLMRequest) -> dict[str, Any]:
        await edit(env, 2, "SYNTH tahrirlangan matn")
        return match_all(request)

    fake = FakeLLM()
    fake.parsed = [edit_then_match]
    stale = await evaluator(env, fake).evaluate(a)
    assert (stale.status, stale.calls, stale.alerts) == (MonitorEvalStatus.STALE, 1, 0)
    assert await revision(env, a) > first_revision
    assert await alerts(env) == []
    row = await progress_row(env, watched)
    assert (row.position, row.attempts, row.last_error) == (None, 1, "source_changed")
    assert await flag(env, a) is not None
    [run] = await evaluate_runs(env)
    assert (run.status, run.error_class) == ("failed", "discarded:source_changed")

    fake.parsed = [match_all]
    fresh = await evaluator(env, fake).evaluate(a)
    assert (fresh.status, fresh.alerts) == (MonitorEvalStatus.EVALUATED, 1)
    assert "SYNTH tahrirlangan matn" in everything(fake.requests[-1])
    assert (await progress_row(env, watched)).status == "current"


async def test_a_message_deleted_during_the_call_is_not_settled_and_leaves_a_gap(
    env: Env,
) -> None:
    """The deletion makes the answer stale: no alert, no cursor movement. The
    message is gone, so nothing is read again, but the loss stays on record
    instead of reading as reviewed."""
    a, watched = await one_message_watch(env)

    async def delete_then_match(request: LLMRequest) -> dict[str, Any]:
        await deliver(env, deleted_messages(message_ids=[2]))
        return match_all(request)

    fake = FakeLLM()
    fake.parsed = [delete_then_match]
    stale = await evaluator(env, fake).evaluate(a)
    assert (stale.status, stale.alerts) == (MonitorEvalStatus.STALE, 0)
    again = await evaluator(env, fake).evaluate(a)
    assert (again.status, len(fake.requests)) == (MonitorEvalStatus.UP_TO_DATE, 1)
    assert await alerts(env) == []
    assert await flag(env, a) is None
    row = await progress_row(env, watched)
    gone = await message_id_of(env, 2)
    assert row.status == "partial"
    assert row.pending_ranges == [
        {"first_message_id": gone, "last_message_id": gone, "reason": "deleted"}
    ]
    assert (await review_state(env, watched)).state == "gap"


async def test_a_message_that_arrives_during_the_call_does_not_stale_the_batch(env: Env) -> None:
    """New arrivals change the chat's revision but only add messages beyond the
    batch: the answer applies, and the newcomer is the next batch."""
    a, watched = await one_message_watch(env, lifecycle="standing")
    first = await message_id_of(env, 2)

    async def another_arrives(request: LLMRequest) -> dict[str, Any]:
        await say(env, 3, "SYNTH yana bir xabar")
        return match_all(request)

    fake = FakeLLM()
    fake.parsed = [another_arrives, verdicts((watched, False, []))]
    outcome = await evaluator(env, fake).evaluate(a)
    assert (outcome.status, outcome.alerts) == (MonitorEvalStatus.EVALUATED, 1)
    row = await progress_row(env, watched)
    assert (row.position or {}).get("message_id") == first
    assert await flag(env, a) is not None
    second = await evaluator(env, fake).evaluate(a)
    assert second.status is MonitorEvalStatus.EVALUATED
    assert shown_new(fake.requests[1]) == [await message_id_of(env, 3)]
    assert await flag(env, a) is None


async def test_monitors_with_different_cursors_are_read_separately_and_fail_separately(
    env: Env,
) -> None:
    """C7: a faster monitor's cursor never clears another's failed range. B is
    created after A has reviewed message 2: both still owe message 3, but from
    different cursors, so they are read in separate calls; A's failure leaves
    B free to be read at once, and A is read again after its backoff."""
    a, _ = await setup(env)
    first = await monitor(
        env, "semantic", conversation_ids=[a], description="a refund", lifecycle="standing"
    )
    env.clock.advance(minutes=1)
    await say(env, 2, "SYNTH birinchi")
    fake = FakeLLM()
    fake.parsed = [verdicts((first, False, []))]
    assert (await evaluator(env, fake).evaluate(a)).status is MonitorEvalStatus.EVALUATED

    env.clock.advance(minutes=1)
    second = await monitor(
        env, "semantic", conversation_ids=[a], description="a price", lifecycle="standing"
    )
    env.clock.advance(minutes=1)
    await say(env, 3, "SYNTH ikkinchi")
    third = await message_id_of(env, 3)

    fake.parsed = [LLMError(LLMErrorKind.UNAVAILABLE), match_all, match_all]
    failed = await evaluator(env, fake).evaluate(a)  # the oldest cursor goes first
    assert failed.status is MonitorEvalStatus.FAILED
    assert listed_monitors(fake.requests[-1]) == [first]
    other = await evaluator(env, fake).evaluate(a)
    assert (other.status, other.alerts) == (MonitorEvalStatus.EVALUATED, 1)
    assert listed_monitors(fake.requests[-1]) == [second]
    assert (await progress_row(env, second)).status == "current"
    assert (await progress_row(env, first)).attempts == 1
    assert await flag(env, a) is not None

    env.clock.advance(seconds=61)
    last = await evaluator(env, fake).evaluate(a)
    assert (last.status, last.alerts) == (MonitorEvalStatus.EVALUATED, 1)
    assert listed_monitors(fake.requests[-1]) == [first]
    assert shown_new(fake.requests[-1]) == [third]
    assert await flag(env, a) is None
    assert sorted(n.refs["automation_ids"][0] for n in await alerts(env)) == sorted([first, second])


async def test_a_message_deleted_before_any_review_is_recorded_as_a_gap(env: Env) -> None:
    """No call is made for a message that is already gone, and the cursor
    passing over it records the loss."""
    a, watched = await one_message_watch(env)
    await deliver(env, deleted_messages(message_ids=[2]))
    fake = FakeLLM()
    outcome = await evaluator(env, fake).evaluate(a)
    assert (outcome.status, fake.requests) == (MonitorEvalStatus.UP_TO_DATE, [])
    gone = await message_id_of(env, 2)
    row = await progress_row(env, watched)
    assert row.pending_ranges == [
        {"first_message_id": gone, "last_message_id": gone, "reason": "deleted"}
    ]
    assert await flag(env, a) is None


async def test_consent_and_budget_holds_are_visible_and_not_attempts(env: Env) -> None:
    a, watched = await one_message_watch(env)
    await revoke(env, "ai_managed")
    fake = FakeLLM()
    assert (await evaluator(env, fake).evaluate(a)).status is MonitorEvalStatus.NO_CONSENT
    row = await progress_row(env, watched)
    assert (row.status, row.last_error, row.attempts) == ("blocked", "no_consent", 0)
    assert (await review_state(env, watched)).state == "blocked"

    await grant_managed(env)
    over = await evaluator(env, fake, monthly_budget_usd=Decimal(0)).evaluate(a)
    assert over.status is MonitorEvalStatus.OVER_BUDGET
    assert over.retry_at == env.clock.now() + monitor_review.BUDGET_RETRY
    assert fake.requests == []
    row = await progress_row(env, watched)
    assert (row.status, row.last_error, row.attempts) == ("pending", "budget", 0)
    state = await review_state(env, watched)
    assert (state.state, state.reasons) == ("delayed", ("budget",))

    env.clock.advance(hours=1, seconds=1)
    fake.parsed = [match_all]
    assert (await evaluator(env, fake).evaluate(a)).alerts == 1
    assert (await review_state(env, watched)).state == "current"


def monitor_job(env: Env, fake: FakeLLM) -> MonitorJobs:
    assert env.scheduler is not None
    return MonitorJobs(
        session_factory=env.factory,
        clock=env.clock,
        scheduler=env.scheduler,
        debounce=DEBOUNCE,
        evaluator=evaluator(env, fake),
    )


async def clear_jobs(env: Env) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM procrastinate.procrastinate_jobs WHERE task_name = :t"),
            {"t": MONITOR_TASK},
        )


async def test_the_job_follows_a_backlog_at_once_and_a_failure_after_its_backoff(
    env: Env,
) -> None:
    a, _ = await setup(env)
    watched = await monitor(env, "semantic", conversation_ids=[a], description="a refund")
    env.clock.advance(minutes=1)
    for number in range(2, 2 + BURST_LIMIT + 1):
        await say(env, number, f"SYNTH xabar {number}")
    env.clock.advance(seconds=DEBOUNCE.total_seconds())
    await clear_jobs(env)  # the ingestion job: this test is the worker
    fake = FakeLLM()
    fake.parsed = [verdicts((watched, False, [])), LLMError(LLMErrorKind.UNAVAILABLE)]
    job = monitor_job(env, fake)

    outcome = await job.evaluate(a)
    assert outcome is not None
    assert outcome.backlog
    [(_, at)] = await job_times(env)
    assert at <= env.clock.now()  # the rest of the backlog follows at once

    await clear_jobs(env)
    failed = await job.evaluate(a)
    assert failed is not None
    assert failed.status is MonitorEvalStatus.FAILED
    [(status, at)] = await job_times(env)
    assert (status, at) == ("todo", env.clock.now() + monitor_review.RETRY_DELAYS[0])


async def test_progress_deletion_keeps_a_linked_verdict_and_clears_both_link_fields(
    env: Env,
) -> None:
    """C7: every deletion path of a progress row clears `review_progress_id`
    AND `review_version` on the retained verdict, atomically with the removal:
    the hourly sweep, the managed-data deletion, a cascade (the monitor deleted)
    and a raw delete (the database trigger)."""
    a, watched = await one_message_watch(env)
    owner = await account_id(env)
    fake = FakeLLM()
    fake.parsed = [verdicts((watched, False, []))]
    await evaluator(env, fake).evaluate(a)
    through = await message_id_of(env, 2)

    async def verdict(monitor_id: int | None = None) -> int:
        progress = await progress_row(env, monitor_id or watched)
        async with env.factory() as session, session.begin():
            # One verdict per chat: replace the one a previous step left.
            await session.execute(
                text("DELETE FROM app.conversation_triage WHERE conversation_id = :c"), {"c": a}
            )
            triage = ConversationTriage(
                account_id=owner,
                conversation_id=a,
                covers_through_message_id=through,
                needs_reply=False,
                contains_question=False,
                requests_decision=False,
                urgency="low",
                escalation_category="none",
                reasons=[],
                prompt_version="triage.v1",
                model="gpt-6-luna",
                review_progress_id=progress.id,
                review_version=progress.version,
            )
            session.add(triage)
            await session.flush()
            return int(triage.id)

    async def linked(triage_id: int) -> tuple[bool, Any, Any]:
        async with env.factory() as session:
            row = await session.get(ConversationTriage, triage_id)
        return row is not None, *(
            (row.review_progress_id, row.review_version) if row else (None, None)
        )

    async def progress_count() -> int:
        async with env.factory() as session:
            return int(
                (
                    await session.execute(text("SELECT count(*) FROM app.review_progress"))
                ).scalar_one()
            )

    # 1. The sweep: the chat is set to Ignore, so no monitor reads it.
    triage_id = await verdict()
    assert (await linked(triage_id))[1] is not None
    await set_mode(env, a, "ignore")
    async with transaction(env.factory) as uow:
        assert await monitor_review.sweep(uow.session, now=env.clock.now()) == 1
    assert await linked(triage_id) == (True, None, None)
    assert await progress_count() == 0
    await set_mode(env, a, "observe")

    # 2. A raw delete: the database trigger is the backstop.
    fake.parsed = [verdicts((watched, False, []))]
    await say(env, 3, "SYNTH yana")
    await evaluator(env, fake).evaluate(a)
    triage_id = await verdict()
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM app.review_progress"))
    assert await linked(triage_id) == (True, None, None)

    # 3. The monitor is deleted (FK cascade); 4. the managed-data deletion.
    await say(env, 4, "SYNTH uchinchi")
    fake.parsed = [verdicts((watched, False, []))]
    await evaluator(env, fake).evaluate(a)
    triage_id = await verdict()
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM app.automations WHERE id = :i"), {"i": watched})
    assert await linked(triage_id) == (True, None, None)
    assert await progress_count() == 0

    again = await monitor(env, "semantic", conversation_ids=[a], description="a refund")
    env.clock.advance(minutes=1)
    await say(env, 5, "SYNTH to'rtinchi")
    fake.parsed = [verdicts((again, False, []))]
    await evaluator(env, fake).evaluate(a)
    triage_id = await verdict(again)
    assert (await linked(triage_id))[1] is not None
    async with transaction(env.factory) as uow:
        await deletion.delete_managed_data(uow.session, account_id=owner, clock=env.clock)
    assert await progress_count() == 0  # (the managed deletion also removes the verdict)


async def test_the_sweep_drops_the_review_metadata_of_long_ended_monitors(env: Env) -> None:
    a, watched = await one_message_watch(env)
    fake = FakeLLM()
    fake.parsed = [verdicts((watched, False, []))]
    await evaluator(env, fake).evaluate(a)
    async with transaction(env.factory) as uow:
        assert await monitor_review.sweep(uow.session, now=env.clock.now()) == 0
    assert await stop_now(env, watched) is monitors.StopResult.STOPPED
    async with transaction(env.factory) as uow:  # ended recently: kept
        assert await monitor_review.sweep(uow.session, now=env.clock.now()) == 0
    env.clock.advance(days=366)
    async with transaction(env.factory) as uow:
        assert await monitor_review.sweep(uow.session, now=env.clock.now()) == 1
