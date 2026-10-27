"""Jobs against a real PostgreSQL and real procrastinate workers (S01.5).

Pinned claims:

- a free-text owner turn and its `agent.owner_request` job commit together, and
  a rollback leaves neither (M00 spike E1, used for real);
- one account's owner turns run one at a time (lock `acct:{id}:control`);
- a slow `llm` job does not delay `system` or `outbound` jobs (one worker per
  queue);
- a job left `doing` by a dead worker is retried by `system.retry_stalled_jobs`;
- the reconciler re-defers a turn whose job was lost, and only that turn.

Waiting is done by polling the database for a condition with a generous
deadline (M00 F9: draining once mis-reads lock behaviour); no assertion
depends on elapsed time.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from procrastinate import App
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import Account, ControlTurn
from assistant.agent.confirmations import ApplyOutcome
from assistant.agent.runtime import RunOutcome, RunStatus
from assistant.core.jobs import OWNER_REQUEST_TASK, owner_control_lock
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.uow import transaction
from assistant.inbox.notify import OwnerAlerts
from assistant.jobs.app import QUEUES, build_app, run_queue_worker
from assistant.jobs.scheduler import ProcrastinateScheduler
from assistant.jobs.tasks import JobHandlers, register_tasks
from assistant.telegram.control.dispatcher import ControlDispatcher
from assistant.telegram.control.handlers import IncomingMessage
from assistant.telegram.gateway import OutboundGateway
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock, FakeSleep
from tests.fakes.deletions import RecordingDeleter
from tests.fakes.sender import FakeTelegramSender
from tests.fakes.summaries import RecordingRefresher

pytestmark = pytest.mark.integration

OWNER = 7000000001

TRUNCATE = (
    "TRUNCATE app.accounts, app.inbound_updates, app.system_state CASCADE; "
    "TRUNCATE procrastinate.procrastinate_jobs, procrastinate.procrastinate_events, "
    "procrastinate.procrastinate_periodic_defers, procrastinate.procrastinate_workers CASCADE"
)


@dataclass
class RecordingApplier:
    """Stands in for the confirmation applier."""

    applied: list[int] = field(default_factory=list)
    cutoffs: list[int | None] = field(default_factory=list)

    async def apply(self, confirmation_id: int, turn_cutoff: int | None = None) -> ApplyOutcome:
        self.applied.append(confirmation_id)
        self.cutoffs.append(turn_cutoff)
        return ApplyOutcome("applied")


@dataclass
class RecordingRuntime:
    """Stands in for the agent: records when each turn ran, optionally waits."""

    intervals: list[tuple[int, float, float]] = field(default_factory=list)
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, turn_id: int) -> RunOutcome:
        loop = asyncio.get_running_loop()
        begin = loop.time()
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        else:
            await asyncio.sleep(0.05)  # an opening for overlap, not a timing assertion
        self.intervals.append((turn_id, begin, loop.time()))
        return RunOutcome(RunStatus.SUCCEEDED)


@dataclass
class Env:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    app: App
    scheduler: ProcrastinateScheduler
    runtime: RecordingRuntime
    clock: FakeClock
    handlers: JobHandlers
    deleter: RecordingDeleter


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    engine = build_engine(migrated_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(TRUNCATE)
    factory = build_session_factory(engine)
    app = build_app(migrated_url)
    scheduler = ProcrastinateScheduler(app)
    runtime = RecordingRuntime()
    clock = FakeClock(datetime.now(UTC))
    deleter = RecordingDeleter()
    handlers = JobHandlers(
        session_factory=factory,
        clock=clock,
        scheduler=scheduler,
        owner_runtime=runtime,
        alerts=OwnerAlerts(factory, FakeChannel(), clock),
        monthly_budget_usd=None,
        heartbeat_url=None,
        confirmation_applier=RecordingApplier(),
        summary_refresher=RecordingRefresher(),
        data_deleter=deleter,
    )
    register_tasks(app, handlers)
    async with app.open_async():
        yield Env(engine, factory, app, scheduler, runtime, clock, handlers, deleter)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(TRUNCATE)
    await engine.dispose()


@contextlib.asynccontextmanager
async def workers(app: App, *names: str) -> AsyncIterator[None]:
    tasks = [
        asyncio.create_task(run_queue_worker(app, spec)) for spec in QUEUES if spec.name in names
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def wait_until(check: Callable[[], Awaitable[bool]], *, deadline: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while not await check():
        if loop.time() > end:
            raise AssertionError("condition not met before the deadline")
        await asyncio.sleep(0.05)


async def job_statuses(env: Env, task_name: str) -> list[str]:
    async with env.factory() as session:
        result = await session.execute(
            text(
                "SELECT status::text FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = :t ORDER BY id"
            ),
            {"t": task_name},
        )
        return [row[0] for row in result.all()]


async def owner_jobs(env: Env) -> list[dict[str, Any]]:
    async with env.factory() as session:
        result = await session.execute(
            text(
                "SELECT args, lock FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = :t ORDER BY id"
            ),
            {"t": OWNER_REQUEST_TASK},
        )
        return [{"args": row[0], "lock": row[1]} for row in result.all()]


async def onboarded_account(env: Env) -> int:
    async with transaction(env.factory) as uow:
        account, _ = await service.get_or_create_account(
            uow.session, telegram_user_id=OWNER, clock=env.clock
        )
        await service.set_control_chat(uow.session, account, OWNER)
        return account.id


async def add_turn(env: Env, account_id: int, *, created_at: datetime | None = None) -> int:
    async with transaction(env.factory) as uow:
        turn = ControlTurn(account_id=account_id, role="owner", text="Synthetic question")
        if created_at is not None:
            turn.created_at = created_at
        uow.session.add(turn)
        await uow.session.flush()
        return turn.id


# ------------------------------------------------- transactional defer (E1)
async def test_a_free_text_turn_and_its_job_commit_together(env: Env) -> None:
    account_id = await onboarded_account(env)
    dispatcher = ControlDispatcher(
        OutboundGateway(FakeTelegramSender(), env.clock, sleep=FakeSleep()),
        env.clock,
        env.scheduler,
    )
    message = IncomingMessage(
        account_id=0, chat_id=OWNER, telegram_message_id=5, text="Synthetic question", from_id=OWNER
    )

    async with transaction(env.factory) as uow:
        outcome = await dispatcher.dispatch(uow.session, message)

    assert outcome.result.note == "queued_for_agent"
    async with env.factory() as session:
        turn_id = (await session.execute(select(ControlTurn.id))).scalar_one()
    assert await owner_jobs(env) == [
        {"args": {"turn_id": turn_id}, "lock": owner_control_lock(account_id)}
    ]


async def test_a_rolled_back_turn_leaves_no_job(env: Env) -> None:
    await onboarded_account(env)
    dispatcher = ControlDispatcher(
        OutboundGateway(FakeTelegramSender(), env.clock, sleep=FakeSleep()),
        env.clock,
        env.scheduler,
    )
    message = IncomingMessage(
        account_id=0, chat_id=OWNER, telegram_message_id=5, text="Synthetic question", from_id=OWNER
    )

    async def dispatch_then_crash() -> None:
        async with transaction(env.factory) as uow:
            await dispatcher.dispatch(uow.session, message)
            raise RuntimeError("synthetic crash before commit")

    with pytest.raises(RuntimeError, match="synthetic"):
        await dispatch_then_crash()

    async with env.factory() as session:
        assert (await session.execute(select(ControlTurn))).first() is None
    assert await owner_jobs(env) == []


# ------------------------------------------------------------ ordering (lock)
async def test_one_accounts_turns_run_one_at_a_time(env: Env) -> None:
    account_id = await onboarded_account(env)
    turn_ids = [await add_turn(env, account_id) for _ in range(3)]
    for turn_id in turn_ids:
        await env.scheduler.defer(
            OWNER_REQUEST_TASK, args={"turn_id": turn_id}, lock=owner_control_lock(account_id)
        )

    async with workers(env.app, "llm"):  # concurrency 3: only the lock serializes
        await wait_until(lambda: _all_done(env, OWNER_REQUEST_TASK, 3))

    spans = sorted(env.runtime.intervals, key=lambda span: span[1])
    assert [span[0] for span in spans] == turn_ids  # in order, too
    for previous, current in itertools.pairwise(spans):
        assert current[1] >= previous[2], "two turns of one account overlapped"


async def _all_done(env: Env, task: str, count: int) -> bool:
    statuses = await job_statuses(env, task)
    return len(statuses) == count and all(status == "succeeded" for status in statuses)


# ---------------------------------------------------------- queue isolation
async def test_a_slow_llm_job_does_not_delay_system_or_outbound_jobs(env: Env) -> None:
    account_id = await onboarded_account(env)
    env.runtime.gate = asyncio.Event()
    outbound_ran = asyncio.Event()

    @env.app.task(name="test.outbound_probe", queue="outbound")
    async def outbound_probe() -> None:
        outbound_ran.set()

    turn_id = await add_turn(env, account_id)
    async with workers(env.app, "llm", "system", "outbound"):
        await env.scheduler.defer(
            OWNER_REQUEST_TASK, args={"turn_id": turn_id}, lock=owner_control_lock(account_id)
        )
        await asyncio.wait_for(env.runtime.started.wait(), timeout=20)
        await env.scheduler.defer("system.retention_sweep", args={"timestamp": 0})
        await env.scheduler.defer("test.outbound_probe", args={})

        await asyncio.wait_for(outbound_ran.wait(), timeout=20)
        await wait_until(lambda: _all_done(env, "system.retention_sweep", 1))
        assert await job_statuses(env, OWNER_REQUEST_TASK) == ["doing"]
        env.runtime.gate.set()
        await wait_until(lambda: _all_done(env, OWNER_REQUEST_TASK, 1))


# ----------------------------------------------------- stalled-job recovery
async def test_a_job_left_doing_by_a_dead_worker_is_retried(env: Env) -> None:
    job_id = await env.scheduler.defer("system.retention_sweep", args={"timestamp": 0})
    async with env.engine.begin() as conn:
        dead_worker = (
            await conn.execute(
                text(
                    "INSERT INTO procrastinate.procrastinate_workers (last_heartbeat) "
                    "VALUES (now() - interval '10 minutes') RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "UPDATE procrastinate.procrastinate_jobs "
                "SET status = 'doing', worker_id = :w WHERE id = :j"
            ),
            {"w": dead_worker, "j": job_id},
        )

    await env.app.tasks["system.retry_stalled_jobs"].func(0)

    assert await job_statuses(env, "system.retention_sweep") == ["todo"]


# ------------------------------------------------------------- reconciler
async def test_a_stalled_job_with_a_queueing_lock_is_failed_not_retried_into_a_collision(
    env: Env,
) -> None:
    """procrastinate's retry puts the job back to `todo` with its queueing
    lock; when a newer `todo` job already holds that lock (a summary refresh
    deferred meanwhile), the retry would violate the unique index. Such a
    job is failed instead; the reconciler (summaries) or the next period
    (periodic tasks) re-creates the work."""
    stalled = await env.scheduler.defer(
        "memory.refresh_summary", args={"conversation_id": 1}, queueing_lock="sum:1"
    )
    async with env.engine.begin() as conn:
        dead_worker = (
            await conn.execute(
                text(
                    "INSERT INTO procrastinate.procrastinate_workers (last_heartbeat) "
                    "VALUES (now() - interval '10 minutes') RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "UPDATE procrastinate.procrastinate_jobs "
                "SET status = 'doing', worker_id = :w WHERE id = :j"
            ),
            {"w": dead_worker, "j": stalled},
        )
    newer = await env.scheduler.defer(
        "memory.refresh_summary", args={"conversation_id": 1}, queueing_lock="sum:1"
    )
    assert newer is not None

    await env.app.tasks["system.retry_stalled_jobs"].func(0)

    assert await job_statuses(env, "memory.refresh_summary") == ["failed", "todo"]


async def test_the_summary_task_is_never_retried_by_procrastinate(env: Env) -> None:
    """A failed refresh leaves the flag set and the reconciler re-creates the
    job; a procrastinate retry could collide with the queueing lock."""
    assert env.app.tasks["memory.refresh_summary"].retry_strategy is None


DELETE_ARGS = {
    "account_id": 1,
    "scope": "everything",
    "subject_id": 1,
    "card_id": 2,
    "turn_cutoff": 3,
}


@contextlib.asynccontextmanager
async def llm_worker_with_short_grace(app: App) -> AsyncIterator[None]:
    """The `llm` worker as `run_queue_worker` starts it, with a shutdown grace
    short enough for a test: a job still running then is cancelled."""
    task = asyncio.create_task(
        app.run_worker_async(
            queues=["llm"],
            name="worker-llm-test",
            concurrency=1,
            wait=True,
            install_signal_handlers=False,
            shutdown_graceful_timeout=0.1,
            listen_notify=True,
        )
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_failed_deletion_is_never_retried_by_procrastinate(env: Env) -> None:
    """S03.5 fix round 2, item 1: a failed deletion tells the owner that
    nothing was deleted; an automatic retry would then delete after that
    message. The owner asks again instead (M03 D87)."""
    env.deleter.error = RuntimeError("synthetic failure")

    async def settled() -> bool:
        statuses = await job_statuses(env, "privacy.delete_data")
        return bool(statuses) and statuses[0] not in ("todo", "doing")

    await env.scheduler.defer("privacy.delete_data", args=DELETE_ARGS, lock=owner_control_lock(1))
    async with llm_worker_with_short_grace(env.app):
        await wait_until(settled)
    assert await job_statuses(env, "privacy.delete_data") == ["failed"]
    assert len(env.deleter.runs) == 1


async def test_a_deletion_cut_off_by_a_worker_shutdown_goes_back_to_the_queue(env: Env) -> None:
    """Fix round 3, D: procrastinate cancels a job still running when the
    shutdown grace ends. A deletion cut off so has decided nothing (its
    transaction rolled back) and told nobody, so it is requeued — at once,
    keeping its lock and its place before later owner runs — and runs after
    the restart; that run deletes once and sends the receipt."""
    env.deleter.gate = asyncio.Event()  # never set: still running at shutdown

    async def doing() -> bool:
        return await job_statuses(env, "privacy.delete_data") == ["doing"]

    await env.scheduler.defer("privacy.delete_data", args=DELETE_ARGS, lock=owner_control_lock(1))
    async with llm_worker_with_short_grace(env.app):
        await wait_until(doing)
    assert await job_statuses(env, "privacy.delete_data") == ["todo"]
    async with env.factory() as session:
        lock, args, later = (
            await session.execute(
                text(
                    "SELECT lock, args, scheduled_at > now() + interval '1 second' "
                    "FROM procrastinate.procrastinate_jobs WHERE task_name = 'privacy.delete_data'"
                )
            )
        ).one()
    assert lock == owner_control_lock(1)
    assert args == DELETE_ARGS
    assert not later  # never a future-scheduled job holding the lock (AGENTS §13)


async def test_the_deletion_task_passes_the_turn_cutoff_through(env: Env) -> None:
    """Ids only (AGENTS §13); a job queued before the cutoff existed still runs."""
    task = env.app.tasks["privacy.delete_data"].func
    await task(account_id=1, scope="everything", subject_id=1, card_id=2, turn_cutoff=5)
    await task(account_id=1, scope="everything", subject_id=1, card_id=3)
    assert env.deleter.runs == [(1, "everything", 1, 2, 5), (1, "everything", 1, 3, None)]


def test_a_worker_cannot_be_built_without_a_data_deleter() -> None:
    """Like the confirmation applier: a worker that could not run an owner's
    deletion must not start (the job would fail where the owner was told
    "deleting now")."""
    [deleter] = [f for f in dataclasses.fields(JobHandlers) if f.name == "data_deleter"]
    assert deleter.default is dataclasses.MISSING


async def test_the_reconciler_redefers_only_a_lost_recent_turn(env: Env) -> None:
    account_id = await onboarded_account(env)
    now = env.clock.now()
    lost = await add_turn(env, account_id, created_at=now - timedelta(minutes=2))
    queued = await add_turn(env, account_id, created_at=now - timedelta(minutes=2))
    await env.scheduler.defer(
        OWNER_REQUEST_TASK, args={"turn_id": queued}, lock=owner_control_lock(account_id)
    )
    await add_turn(env, account_id, created_at=now - timedelta(hours=2))  # stale
    await add_turn(env, account_id, created_at=now - timedelta(seconds=5))  # too fresh

    redeferred = await env.handlers.reconcile_pending()

    assert redeferred == 1
    jobs = await owner_jobs(env)
    assert {"args": {"turn_id": lost}, "lock": owner_control_lock(account_id)} in jobs
    assert len(jobs) == 2
    assert await env.handlers.reconcile_pending() == 0  # idempotent


async def test_a_lock_is_never_put_on_a_future_job(env: Env) -> None:
    with pytest.raises(ValueError, match="future-scheduled"):
        await env.scheduler.defer(
            OWNER_REQUEST_TASK,
            args={"turn_id": 1},
            lock="acct:1:control",
            schedule_at=env.clock.now() + timedelta(minutes=5),
        )


async def test_registered_reconcilers_run_in_the_reconcile_transaction_and_defer_after_commit(
    env: Env,
) -> None:
    from dataclasses import replace

    from assistant.jobs.reconcile import DeferRequest

    account_id = await onboarded_account(env)
    turn_id = await add_turn(env, account_id, created_at=env.clock.now())
    calls: list[str] = []

    async def reconcile(session: AsyncSession, now: datetime) -> list[DeferRequest]:
        assert session.in_transaction()
        assert now == env.clock.now()
        await session.execute(
            text("UPDATE app.accounts SET autonomy_paused = true WHERE id = :id"),
            {"id": account_id},
        )
        calls.append("reconcile")
        return [DeferRequest(OWNER_REQUEST_TASK, {"turn_id": turn_id})]

    class Scheduler:
        async def defer(self, task: str, **kwargs: Any) -> None:
            assert kwargs.get("connection") is None
            async with env.factory() as session:
                assert await session.scalar(
                    select(Account.autonomy_paused).where(Account.id == account_id)
                )
            calls.append("defer")

    handlers = replace(env.handlers, scheduler=Scheduler(), reconcilers=(reconcile,))
    assert await handlers.reconcile_pending() == 1
    assert calls == ["reconcile", "defer"]


async def test_handler_groups_left_out_register_no_tasks(env: Env) -> None:
    assert env.handlers.actions is env.handlers.inbox is env.handlers.automation is None
    assert "actions.execute" not in env.app.tasks
    assert "inbox.triage" not in env.app.tasks
    assert "automation.fire" not in env.app.tasks
