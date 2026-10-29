"""Deterministic digest delivery, recurrence and downtime recovery."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant.automation.models import AutomationOccurrence
from assistant.db.uow import transaction
from assistant.inbox.models import Digest
from assistant.inbox.notify import AlertKind
from tests.integration.business_env import OWNER, deliver, private_message
from tests.integration.p2_factories import seed_automation
from tests.scenarios.m05_drive import M05Scenario, advance, run_queued

pytestmark = pytest.mark.integration


async def schedule(s: M05Scenario, due: datetime) -> None:
    async with transaction(s.env.factory) as uow:
        await seed_automation(
            uow.session,
            account_id=s.account_id,
            kind="digest",
            spec={
                "schema_version": 1,
                "kind": "digest",
                "max_lateness_minutes": 180,
                "dtstart_local": due.astimezone(ZoneInfo("Europe/Berlin"))
                .replace(tzinfo=None)
                .isoformat(timespec="minutes"),
            },
            rrule="FREQ=DAILY",
            timezone="Europe/Berlin",
            confirmed_at=s.env.clock.now(),
            next_run_at=due,
        )


async def fire(s: M05Scenario) -> None:
    await advance(s, 0, periodic=("system.reconcile_schedules",))
    assert await run_queued(s, "automation.fire") == 1
    assert await run_queued(s, "digest.build") == 1


def assert_late_note(s: M05Scenario, message_id: int | None) -> None:
    assert re.search(r"\blate\b", s.output_for(message_id), re.IGNORECASE), (
        "the digest message must include a late note"
    )


async def test_late_note_assertion_rejects_other_messages_and_substrings(m05: M05Scenario) -> None:
    await m05.handlers.alerts.notify_owner(
        m05.account_id,
        AlertKind.CONNECTION_CHANGE,
        dedupe_key="late-proof-unrelated",
        body="An unrelated synthetic alert arrived late.",
    )
    for number, body in enumerate(("Synthetic digest.", "Please try again later.")):
        result = await m05.handlers.alerts.notify_owner(
            m05.account_id,
            AlertKind.CONNECTION_CHANGE,
            dedupe_key=f"late-proof-negative:{number}",
            body=body,
        )
        assert result.sent
        with pytest.raises(AssertionError):
            assert_late_note(m05, result.message_id)
    result = await m05.handlers.alerts.notify_owner(
        m05.account_id,
        AlertKind.CONNECTION_CHANGE,
        dedupe_key="late-proof-positive",
        body="Synthetic digest delivered late after downtime.",
    )
    assert result.sent
    assert_late_note(m05, result.message_id)


async def test_a_digest_built_twice_for_one_period_is_delivered_once(m05: M05Scenario) -> None:
    for _ in range(2):
        await m05.job("digest.build", account_id=m05.account_id, period_end=m05.date)
    [row] = await m05.rows(Digest)
    assert row.status == "sent"
    assert len(m05.env.sender.sends) == 1


async def test_the_digest_fires_at_local_time_across_dst(m05: M05Scenario) -> None:
    m05.require("system.reconcile_schedules")
    await m05.preferences(timezone="Europe/Berlin", digest_time="08:00")
    first = datetime(2026, 10, 24, 6, tzinfo=UTC)
    second = datetime(2026, 10, 25, 7, tzinfo=UTC)
    m05.env.clock.set(first - timedelta(minutes=1))
    await schedule(m05, first)
    m05.env.clock.set(first)
    await fire(m05)
    m05.env.clock.set(second)
    await fire(m05)
    rows = await m05.rows(Digest)
    assert len(rows) == 2
    assert all(r.status == "sent" for r in rows)
    assert [r.sent_at.astimezone(ZoneInfo("Europe/Berlin")).hour for r in rows] == [8, 8]
    assert rows[1].sent_at - rows[0].sent_at == timedelta(hours=25)


async def test_a_missed_digest_after_downtime_fires_late_with_a_note(m05: M05Scenario) -> None:
    m05.require("system.reconcile_schedules")
    await m05.preferences(timezone="Europe/Berlin", digest_time="08:00")
    m05.env.clock.set(datetime(2026, 10, 25, 7, 10, tzinfo=UTC))
    await schedule(m05, m05.env.clock.now() - timedelta(minutes=10))
    await fire(m05)
    [row] = await m05.rows(AutomationOccurrence)
    assert (row.status, row.note_code) == ("fired", "late")
    [digest] = await m05.rows(Digest)
    assert digest.status == "sent"
    assert_late_note(m05, digest.message_id)


async def test_digest_on_demand(m05: M05Scenario) -> None:
    m05.require("digest.build")
    await m05.preferences(digest_time="off")
    await deliver(m05.env, private_message(OWNER, "/digest"))
    assert await run_queued(m05, "digest.build") == 1
    assert (await m05.rows(Digest))[0].status == "sent"
    assert len(m05.env.sender.sends) == 1
