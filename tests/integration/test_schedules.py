"""S05.3b: the occurrence ledger and the schedule reconciler, over Postgres
(S05.3b §3.2; ARCHITECTURE §12.4; FR-FUP-08; IMPLEMENTATION_PLAN S05.3).

Required tests, findable by name:
`test_a_due_occurrence_is_claimed_once_and_next_run_advances`,
`test_a_reconciler_run_while_an_occurrence_executes_does_not_redefer_it`,
`test_a_late_occurrence_fires_with_a_note`,
`test_an_obsolete_occurrence_is_skipped_with_a_note`,
`test_several_missed_occurrences_are_skipped_and_the_next_is_in_the_future`,
`test_an_exhausted_schedule_completes`, `test_an_expired_automation_stops`,
`test_an_invalid_rule_pauses_the_automation_and_never_fires`,
`test_follow_owner_automations_move_with_the_owners_timezone`,
`test_fixed_automations_keep_their_instant`,
`test_europe_berlin_digest_fires_at_local_time_on_the_last_sundays_of_march_and_october`,
`test_tashkent_to_moscow_change_under_both_policies`.

Every test runs in one rolled-back transaction (`db_session`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry
from assistant.automation.models import Automation, AutomationOccurrence
from assistant.automation.rules import ScheduleRule
from assistant.automation.schedule import ScheduleSpec
from assistant.automation.schedules import create_scheduled, reconcile_due
from tests.fakes.clock import FakeClock
from tests.integration.p2_factories import seed_account, seed_automation

pytestmark = pytest.mark.integration

#: 09:00 in Tashkent (UTC+5) on 2026-10-28.
NINE_TASHKENT = datetime(2026, 10, 28, 4, 0, tzinfo=UTC)


def spec(
    *,
    timezone: str = "Asia/Tashkent",
    policy: str = "follow_owner",
    start: str = "2026-10-27T09:00",
) -> ScheduleSpec:
    return ScheduleSpec(
        timezone=timezone,
        tz_policy=policy,  # type: ignore[arg-type]
        rrule="FREQ=DAILY",
        dtstart_local=datetime.fromisoformat(start),
    )


async def daily(
    session: AsyncSession,
    account_id: int,
    *,
    at: datetime,
    max_lateness: int = 60,
    **schedule: Any,
) -> Automation:
    """A daily 09:00 reminder made the way M07 will make one, created at `at`."""
    return await create_scheduled(
        session,
        account_id=account_id,
        kind="reminder",
        rule=ScheduleRule(max_lateness_minutes=max_lateness),
        spec=spec(**schedule),
        clock=FakeClock(at),
    )


async def occurrences(session: AsyncSession, automation_id: int) -> list[AutomationOccurrence]:
    return list(
        (
            await session.execute(
                select(AutomationOccurrence)
                .where(AutomationOccurrence.automation_id == automation_id)
                .order_by(AutomationOccurrence.occurrence_at)
            )
        )
        .scalars()
        .all()
    )


async def fresh(session: AsyncSession, automation: Automation) -> Automation:
    await session.refresh(automation)
    return automation


async def owner(session: AsyncSession, timezone: str = "Asia/Tashkent") -> Account:
    return await seed_account(session, timezone=timezone)


# ============================================================ claiming
async def test_a_due_occurrence_is_claimed_once_and_next_run_advances(
    db_session: AsyncSession,
) -> None:
    account = await owner(db_session)
    automation = await daily(db_session, account.id, at=NINE_TASHKENT - timedelta(hours=3))
    assert automation.next_run_at == NINE_TASHKENT
    assert automation.spec["dtstart_local"] == "2026-10-27T09:00"

    now = NINE_TASHKENT + timedelta(seconds=40)
    report = await reconcile_due(db_session, now)
    again = await reconcile_due(db_session, now)

    assert [(d.automation_id, d.occurrence_at) for d in report.claimed] == [
        (automation.id, NINE_TASHKENT)
    ]
    assert report.claimed[0].epoch == int(NINE_TASHKENT.timestamp())
    assert again.claimed == ()
    [occurrence] = await occurrences(db_session, automation.id)
    assert (occurrence.status, occurrence.note_code) == ("claimed", None)
    assert occurrence.account_id == account.id
    automation = await fresh(db_session, automation)
    assert automation.next_run_at == NINE_TASHKENT + timedelta(days=1)
    assert automation.last_run_at == now
    assert automation.status == "active"


async def test_a_reconciler_run_while_an_occurrence_executes_does_not_redefer_it(
    db_session: AsyncSession,
) -> None:
    """The claim and the advance commit together: a later run finds
    `next_run_at` in the future while the claimed occurrence still runs."""
    account = await owner(db_session)
    automation = await daily(db_session, account.id, at=NINE_TASHKENT - timedelta(hours=1))
    first = await reconcile_due(db_session, NINE_TASHKENT + timedelta(seconds=5))
    assert len(first.claimed) == 1
    # The occurrence is still `claimed` (its fire job is running) one minute on.
    later = await reconcile_due(db_session, NINE_TASHKENT + timedelta(minutes=1, seconds=5))
    assert later.claimed == ()
    assert [o.status for o in await occurrences(db_session, automation.id)] == ["claimed"]


async def test_a_late_occurrence_fires_with_a_note(db_session: AsyncSession) -> None:
    account = await owner(db_session)
    automation = await daily(
        db_session, account.id, at=NINE_TASHKENT - timedelta(hours=1), max_lateness=60
    )
    report = await reconcile_due(db_session, NINE_TASHKENT + timedelta(minutes=30))
    assert [d.occurrence_at for d in report.claimed] == [NINE_TASHKENT]
    [occurrence] = await occurrences(db_session, automation.id)
    assert (occurrence.status, occurrence.note_code) == ("claimed", "late")


async def test_an_on_time_occurrence_within_the_grace_has_no_note(
    db_session: AsyncSession,
) -> None:
    account = await owner(db_session)
    automation = await daily(db_session, account.id, at=NINE_TASHKENT - timedelta(hours=1))
    await reconcile_due(db_session, NINE_TASHKENT + timedelta(minutes=2))
    [occurrence] = await occurrences(db_session, automation.id)
    assert occurrence.note_code is None


async def test_an_obsolete_occurrence_is_skipped_with_a_note(db_session: AsyncSession) -> None:
    account = await owner(db_session)
    automation = await daily(
        db_session, account.id, at=NINE_TASHKENT - timedelta(hours=1), max_lateness=60
    )
    now = NINE_TASHKENT + timedelta(hours=3)
    report = await reconcile_due(db_session, now)
    assert report.claimed == ()
    assert report.skipped == 1
    [occurrence] = await occurrences(db_session, automation.id)
    assert (occurrence.status, occurrence.note_code) == ("skipped", "skipped_obsolete")
    automation = await fresh(db_session, automation)
    assert automation.next_run_at == NINE_TASHKENT + timedelta(days=1)


async def test_several_missed_occurrences_are_skipped_and_the_next_is_in_the_future(
    db_session: AsyncSession,
) -> None:
    """Fifteen days down: ten `skipped_obsolete` rows at most, then a jump
    to the next future occurrence; nothing fires for the missed days."""
    account = await owner(db_session)
    automation = await daily(db_session, account.id, at=NINE_TASHKENT - timedelta(hours=1))
    now = NINE_TASHKENT + timedelta(days=15, hours=3)
    report = await reconcile_due(db_session, now)
    assert report.claimed == ()
    rows = await occurrences(db_session, automation.id)
    assert len(rows) == 10
    assert {(o.status, o.note_code) for o in rows} == {("skipped", "skipped_obsolete")}
    assert rows[0].occurrence_at == NINE_TASHKENT
    automation = await fresh(db_session, automation)
    assert automation.next_run_at == NINE_TASHKENT + timedelta(days=16)
    assert automation.next_run_at > now


async def test_after_the_skips_a_missed_occurrence_within_its_lateness_still_fires(
    db_session: AsyncSession,
) -> None:
    account = await owner(db_session)
    automation = await daily(
        db_session, account.id, at=NINE_TASHKENT - timedelta(hours=1), max_lateness=120
    )
    # Down for two days and an hour: the first two are obsolete, today's is late.
    now = NINE_TASHKENT + timedelta(days=2, hours=1)
    report = await reconcile_due(db_session, now)
    assert [d.occurrence_at for d in report.claimed] == [NINE_TASHKENT + timedelta(days=2)]
    assert [(o.status, o.note_code) for o in await occurrences(db_session, automation.id)] == [
        ("skipped", "skipped_obsolete"),
        ("skipped", "skipped_obsolete"),
        ("claimed", "late"),
    ]
    assert (await fresh(db_session, automation)).next_run_at == NINE_TASHKENT + timedelta(days=3)


async def test_an_exhausted_schedule_completes(db_session: AsyncSession) -> None:
    account = await owner(db_session)
    once = await create_scheduled(
        db_session,
        account_id=account.id,
        kind="reminder",
        rule=ScheduleRule(max_lateness_minutes=30),
        spec=ScheduleSpec(
            timezone="Asia/Tashkent",
            tz_policy="fixed",
            run_at=NINE_TASHKENT + timedelta(microseconds=250),
        ),
        clock=FakeClock(NINE_TASHKENT - timedelta(hours=2)),
    )
    # Stored to the second, so the job's epoch argument names it exactly.
    assert once.next_run_at == NINE_TASHKENT
    now = NINE_TASHKENT + timedelta(seconds=30)
    report = await reconcile_due(db_session, now)
    assert len(report.claimed) == 1
    assert report.completed == 1
    once = await fresh(db_session, once)
    assert (once.status, once.next_run_at, once.ended_at) == ("completed", None, now)


async def test_an_expired_automation_stops(db_session: AsyncSession) -> None:
    account = await owner(db_session)
    automation = await daily(db_session, account.id, at=NINE_TASHKENT - timedelta(hours=1))
    automation.expires_at = NINE_TASHKENT - timedelta(minutes=1)
    await db_session.flush()
    now = NINE_TASHKENT + timedelta(seconds=30)
    report = await reconcile_due(db_session, now)
    assert report.claimed == ()
    assert report.expired == 1
    automation = await fresh(db_session, automation)
    assert (automation.status, automation.ended_at) == ("expired", now)
    assert await occurrences(db_session, automation.id) == []


@pytest.mark.parametrize(
    "broken",
    [
        {"spec": {"schema_version": 1}},  # no max lateness
        {"spec": {"schema_version": 2, "max_lateness_minutes": 5}},
        {"rrule": "FREQ=SECONDLY"},
        {"timezone": "Synthetic/Nowhere"},
        {
            "spec": {
                "schema_version": 1,
                "max_lateness_minutes": 5,
                "dtstart_local": "2026-02-30T09:00",
            }
        },
    ],
)
async def test_an_invalid_rule_pauses_the_automation_and_never_fires(
    db_session: AsyncSession, broken: dict[str, Any]
) -> None:
    account = await owner(db_session)
    fields: dict[str, Any] = {
        "kind": "reminder",
        "spec": {
            "schema_version": 1,
            "max_lateness_minutes": 60,
            "dtstart_local": "2026-10-27T09:00",
        },
        "rrule": "FREQ=DAILY",
        "timezone": "Asia/Tashkent",
        "next_run_at": NINE_TASHKENT,
        **broken,
    }
    automation = await seed_automation(db_session, account_id=account.id, **fields)
    report = await reconcile_due(db_session, NINE_TASHKENT + timedelta(seconds=10))
    again = await reconcile_due(db_session, NINE_TASHKENT + timedelta(days=1, seconds=10))
    assert report.claimed == again.claimed == ()
    assert report.invalid == 1
    assert (await fresh(db_session, automation)).status == "paused"
    assert await occurrences(db_session, automation.id) == []
    [audit] = (
        (
            await db_session.execute(
                select(AuditLogEntry).where(AuditLogEntry.event == "automation_invalid")
            )
        )
        .scalars()
        .all()
    )
    assert (audit.subject_type, audit.subject_id, audit.details) == (
        "automation",
        automation.id,
        {"kind": "reminder"},
    )


async def test_an_event_only_monitor_is_never_touched(db_session: AsyncSession) -> None:
    """Monitors (S05.4) have no schedule and a rule this module cannot read:
    the reconciler neither claims nor pauses them, even with a stray time."""
    account = await owner(db_session, "Europe/Moscow")
    monitor = await seed_automation(
        db_session,
        account_id=account.id,
        kind="monitor",
        spec={"schema_version": 1, "synthetic": True},
        timezone="Asia/Tashkent",
        next_run_at=NINE_TASHKENT,
    )
    report = await reconcile_due(db_session, NINE_TASHKENT + timedelta(minutes=1))
    assert (report.claimed, report.invalid, report.rezoned) == ((), 0, 0)
    monitor = await fresh(db_session, monitor)
    assert (monitor.status, monitor.timezone) == ("active", "Asia/Tashkent")


# ============================================================ timezones
async def test_follow_owner_automations_move_with_the_owners_timezone(
    db_session: AsyncSession,
) -> None:
    account = await owner(db_session)
    automation = await daily(db_session, account.id, at=NINE_TASHKENT - timedelta(hours=3))
    account.timezone = "Europe/Moscow"
    await db_session.flush()
    now = NINE_TASHKENT - timedelta(hours=2)
    report = await reconcile_due(db_session, now)
    assert report.rezoned == 1
    automation = await fresh(db_session, automation)
    assert automation.timezone == "Europe/Moscow"
    # 09:00 Moscow (UTC+3) is 06:00 UTC.
    assert automation.next_run_at == datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
    assert automation.spec["dtstart_local"] == "2026-10-27T09:00"


async def test_fixed_automations_keep_their_instant(db_session: AsyncSession) -> None:
    account = await owner(db_session)
    automation = await daily(
        db_session, account.id, at=NINE_TASHKENT - timedelta(hours=3), policy="fixed"
    )
    account.timezone = "Europe/Moscow"
    await db_session.flush()
    report = await reconcile_due(db_session, NINE_TASHKENT - timedelta(hours=2))
    assert report.rezoned == 0
    automation = await fresh(db_session, automation)
    assert (automation.timezone, automation.next_run_at) == ("Asia/Tashkent", NINE_TASHKENT)


async def test_tashkent_to_moscow_change_under_both_policies(db_session: AsyncSession) -> None:
    """IMPLEMENTATION_PLAN S05.3: a daily 09:00 under each policy, then the
    owner moves from Tashkent (UTC+5) to Moscow (UTC+3): the follow-owner
    one fires at 09:00 Moscow time, the fixed one keeps 09:00 Tashkent time."""
    account = await owner(db_session)
    follows = await daily(db_session, account.id, at=NINE_TASHKENT - timedelta(hours=3))
    fixed = await daily(
        db_session, account.id, at=NINE_TASHKENT - timedelta(hours=3), policy="fixed"
    )
    account.timezone = "Europe/Moscow"
    await db_session.flush()
    fired: list[tuple[int, datetime]] = []
    moment = NINE_TASHKENT - timedelta(hours=2)
    while moment <= datetime(2026, 10, 29, 7, 0, tzinfo=UTC):
        report = await reconcile_due(db_session, moment)
        fired.extend((d.automation_id, d.occurrence_at) for d in report.claimed)
        moment += timedelta(minutes=30)
    assert fired == [
        (fixed.id, datetime(2026, 10, 28, 4, 0, tzinfo=UTC)),
        (follows.id, datetime(2026, 10, 28, 6, 0, tzinfo=UTC)),
        (fixed.id, datetime(2026, 10, 29, 4, 0, tzinfo=UTC)),
        (follows.id, datetime(2026, 10, 29, 6, 0, tzinfo=UTC)),
    ]
    late = [o.note_code for a in (follows, fixed) for o in await occurrences(db_session, a.id)]
    assert late == [None, None, None, None]


async def test_europe_berlin_digest_fires_at_local_time_on_the_last_sundays_of_march_and_october(
    db_session: AsyncSession,
) -> None:
    """IMPLEMENTATION_PLAN S05.3 DST test, on the real reconciler: the
    digest automation the preference creates fires at 21:00 Berlin time on
    the Saturday and the Sunday of both DST changes (2026-03-29 and
    2026-10-25), i.e. at 20:00 → 19:00 UTC in March, 19:00 → 20:00 UTC in
    October."""
    account = await owner(db_session, "Europe/Berlin")
    account.settings = {**(account.settings or {}), "digest_time": "21:00"}
    await db_session.flush()

    async def run(hour: int, minute: int, second: int, *, day: tuple[int, int]) -> list[datetime]:
        moment = datetime(2026, *day, hour, minute, second, tzinfo=UTC)
        report = await reconcile_due(db_session, moment, digest_enabled=True)
        return [d.occurrence_at for d in report.claimed]

    # Saturday 2026-03-28, CET (UTC+1): 21:00 local = 20:00 UTC.
    assert await run(12, 0, 0, day=(3, 28)) == []  # creates the digest automation
    assert await run(19, 59, 30, day=(3, 28)) == []
    assert await run(20, 0, 30, day=(3, 28)) == [datetime(2026, 3, 28, 20, 0, tzinfo=UTC)]
    # Sunday 2026-03-29, CEST (UTC+2) from 01:00 UTC: 21:00 local = 19:00 UTC.
    assert await run(18, 59, 30, day=(3, 29)) == []
    assert await run(19, 0, 30, day=(3, 29)) == [datetime(2026, 3, 29, 19, 0, tzinfo=UTC)]
    assert await run(20, 0, 30, day=(3, 29)) == []
    # Seven months without a reconciler run: the missed days are skipped,
    # then Saturday 2026-10-24, CEST: 21:00 local = 19:00 UTC.
    assert await run(18, 59, 30, day=(10, 24)) == []
    assert await run(19, 0, 30, day=(10, 24)) == [datetime(2026, 10, 24, 19, 0, tzinfo=UTC)]
    # Sunday 2026-10-25, CET again from 01:00 UTC: 21:00 local = 20:00 UTC.
    assert await run(19, 0, 30, day=(10, 25)) == []
    assert await run(20, 0, 30, day=(10, 25)) == [datetime(2026, 10, 25, 20, 0, tzinfo=UTC)]

    [digest] = (
        (
            await db_session.execute(
                select(Automation)
                .where(Automation.account_id == account.id)
                .where(Automation.kind == "digest")
            )
        )
        .scalars()
        .all()
    )
    claimed = [o for o in await occurrences(db_session, digest.id) if o.status == "claimed"]
    assert [o.note_code for o in claimed] == [None, None, None, None]
