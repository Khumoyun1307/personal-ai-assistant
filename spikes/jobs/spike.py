"""M00 S00.3 jobs spike (risk A6, TQ-01).

Throwaway. Answers, against the pinned PostgreSQL 18 image:

  E1  transactional defer on the SQLAlchemy connection's raw psycopg connection
  E2  `lock` serializes jobs that share it
  E3  `queueing_lock` dedupes and raises AlreadyEnqueued
  E4  `schedule_in` is respected
  E5  does a FUTURE-scheduled locked job block a later immediate job with the
      same lock? (the hazard AGENTS §13 forbids)
  E6  retry strategy re-schedules a failing job
  E7  Alembic up / down / up round trip, procrastinate schema included

Run:  docker compose -f compose.dev.yml run --rm app python spikes/jobs/spike.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
import sqlalchemy as sa
from procrastinate import App, PsycopgConnector, RetryStrategy, exceptions
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

HERE = Path(__file__).parent
SPIKE_DB = "spike_jobs"

results: list[tuple[str, bool, str]] = []
events: list[tuple[str, str, float]] = []  # (label, phase, monotonic)
attempts: dict[str, int] = {}


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)


def urls() -> tuple[str, str]:
    """(SQLAlchemy URL, libpq DSN) for the spike database."""
    base = sa.engine.make_url(os.environ["TEST_DATABASE_URL"])
    url = base.set(database=SPIKE_DB)
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    return url.render_as_string(hide_password=False), dsn


def create_database() -> None:
    admin = (
        sa.engine.make_url(os.environ["TEST_DATABASE_URL"])
        .set(drivername="postgresql", database="postgres")
        .render_as_string(hide_password=False)
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{SPIKE_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SPIKE_DB}"')


def alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=HERE,
        env={**os.environ, "SPIKE_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )


# The connector is built at import time: the pool is opened lazily in
# `open_async`, and `App.with_connector` returns a NEW app rather than mutating
# this one, so the tasks below would be registered on the wrong instance.
SPIKE_URL, SPIKE_DSN = urls()
app = App(connector=PsycopgConnector(conninfo=SPIKE_DSN))


@app.task(name="spike.record", queue="default")
async def record_task(label: str = "", sleep: float = 0.0) -> None:
    events.append((label, "start", time.monotonic()))
    if sleep:
        await asyncio.sleep(sleep)
    events.append((label, "end", time.monotonic()))


@app.task(
    name="spike.flaky",
    queue="default",
    retry=RetryStrategy(max_attempts=3, wait=0),
)
async def flaky_task(label: str = "") -> None:
    attempts[label] = attempts.get(label, 0) + 1
    events.append((label, f"attempt{attempts[label]}", time.monotonic()))
    if attempts[label] < 2:
        raise RuntimeError("deliberate failure")


async def drain(timeout: float = 20.0) -> None:
    """Run a worker until the queue is empty (or the timeout expires)."""
    try:
        await asyncio.wait_for(
            app.run_worker_async(
                wait=False,
                concurrency=4,
                install_signal_handlers=False,
                listen_notify=False,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        pass


async def defer_in_transaction(engine: AsyncEngine, *, commit: bool, note: str) -> None:
    """Write a domain row and defer a job on the SAME connection (TQ-01)."""
    conn = await engine.connect()
    try:
        trans = await conn.begin()
        await conn.execute(
            sa.text("INSERT INTO spike_conversations (account_id, note) VALUES (:a, :n)"),
            {"a": 1, "n": note},
        )
        raw = await conn.get_raw_connection()
        driver_conn: Any = raw.driver_connection
        deferrer = app.configure_task(name="spike.record", allow_unknown=False)
        job = deferrer.make_new_job(label=note)
        await app.job_manager.defer_job_async(job, connection=driver_conn)
        if commit:
            await trans.commit()
        else:
            await trans.rollback()
    finally:
        await conn.close()


async def count(engine: AsyncEngine, sql: str, **params: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(sa.text(sql), params)).scalar_one())


async def experiment_transactional_defer(engine: AsyncEngine) -> None:
    await defer_in_transaction(engine, commit=False, note="rolled-back")
    rows = await count(
        engine, "SELECT count(*) FROM spike_conversations WHERE note = :n", n="rolled-back"
    )
    jobs = await count(
        engine,
        "SELECT count(*) FROM procrastinate_jobs WHERE args ->> 'label' = :n",
        n="rolled-back",
    )
    record(
        "E1a transactional defer, ROLLBACK discards BOTH",
        rows == 0 and jobs == 0,
        f"domain rows={rows}, jobs={jobs} (both must be 0)",
    )

    await defer_in_transaction(engine, commit=True, note="committed")
    rows = await count(
        engine, "SELECT count(*) FROM spike_conversations WHERE note = :n", n="committed"
    )
    jobs = await count(
        engine,
        "SELECT count(*) FROM procrastinate_jobs WHERE args ->> 'label' = :n",
        n="committed",
    )
    record(
        "E1b transactional defer, COMMIT persists BOTH",
        rows == 1 and jobs == 1,
        f"domain rows={rows}, jobs={jobs} (both must be 1)",
    )
    await drain()


async def experiment_lock() -> None:
    events.clear()
    for label in ("lockA-1", "lockA-2"):
        await app.configure_task(
            name="spike.record", allow_unknown=False, lock="acct:1:control"
        ).defer_async(label=label, sleep=0.4)
    # `wait=False` makes the worker exit as soon as it cannot fetch a job, and a
    # job blocked by a held lock looks exactly like an empty queue. Drain again
    # until both have run, otherwise this measures the worker, not the lock.
    deadline = time.monotonic() + 20
    while len([1 for _, phase, _ in events if phase == "end"]) < 2:
        if time.monotonic() > deadline:
            break
        await drain()
        await asyncio.sleep(0.1)
    starts = {lbl: t for lbl, phase, t in events if phase == "start"}
    ends = {lbl: t for lbl, phase, t in events if phase == "end"}
    serialized = len(starts) == 2 and (
        starts["lockA-2"] >= ends["lockA-1"] or starts["lockA-1"] >= ends["lockA-2"]
    )
    record(
        "E2 shared `lock` serializes execution (concurrency=4)",
        serialized,
        f"ran {len(starts)}/2, overlap={'no' if serialized else 'YES'}",
    )


async def experiment_queueing_lock() -> None:
    await app.configure_task(
        name="spike.record", allow_unknown=False, queueing_lock="debounce:7"
    ).defer_async(label="q1")
    try:
        await app.configure_task(
            name="spike.record", allow_unknown=False, queueing_lock="debounce:7"
        ).defer_async(label="q2")
    except exceptions.AlreadyEnqueued:
        record("E3 queueing_lock raises AlreadyEnqueued", True, "AlreadyEnqueued raised")
    else:
        record("E3 queueing_lock raises AlreadyEnqueued", False, "no exception raised")
    await drain()


async def experiment_schedule_in() -> None:
    events.clear()
    await app.configure_task(
        name="spike.record", allow_unknown=False, schedule_in={"seconds": 30}
    ).defer_async(label="future")
    await drain(timeout=5)
    record(
        "E4 `schedule_in` is honoured (future job not run)",
        not any(lbl == "future" for lbl, _, _ in events),
        "future job stayed in todo",
    )


async def experiment_future_lock_blocks() -> None:
    events.clear()
    await app.configure_task(
        name="spike.record",
        allow_unknown=False,
        lock="acct:2:control",
        schedule_in={"seconds": 300},
    ).defer_async(label="future-locked")
    await app.configure_task(
        name="spike.record", allow_unknown=False, lock="acct:2:control"
    ).defer_async(label="immediate-locked")
    await drain(timeout=5)
    immediate_ran = any(lbl == "immediate-locked" for lbl, _, _ in events)
    record(
        "E5 future-scheduled locked job BLOCKS a later immediate job",
        not immediate_ran,
        "immediate job was blocked, confirming the AGENTS §13 hazard"
        if not immediate_ran
        else "immediate job ran anyway - AGENTS §13 may be over-cautious",
    )


async def experiment_retry() -> None:
    events.clear()
    await app.configure_task(name="spike.flaky", allow_unknown=False).defer_async(
        label="flaky1"
    )
    deadline = time.monotonic() + 15
    while attempts.get("flaky1", 0) < 2 and time.monotonic() < deadline:
        await drain(timeout=3)
        await asyncio.sleep(0.2)
    record(
        "E6 RetryStrategy retries a failing job to success",
        attempts.get("flaky1", 0) == 2,
        f"attempts={attempts.get('flaky1', 0)} (expected 2)",
    )


async def main() -> int:
    url = SPIKE_URL
    print(f"spike database: {SPIKE_DB}\n")

    create_database()
    up = alembic(url, "upgrade", "head")
    if up.returncode != 0:
        record("E7 alembic upgrade", False, up.stderr.strip()[-800:])
        return 1
    down = alembic(url, "downgrade", "base")
    reup = alembic(url, "upgrade", "head")
    record(
        "E7 alembic up/down/up with the procrastinate schema vendored",
        down.returncode == 0 and reup.returncode == 0,
        f"downgrade rc={down.returncode}, re-upgrade rc={reup.returncode}"
        + (f" | {down.stderr.strip()[-400:]}" if down.returncode else ""),
    )

    engine = create_async_engine(url, hide_parameters=True)

    async with app.open_async():
        await experiment_transactional_defer(engine)
        await experiment_lock()
        await experiment_queueing_lock()
        await experiment_schedule_in()
        await experiment_future_lock_blocks()
        await experiment_retry()

    await engine.dispose()

    print("\n" + "=" * 74)
    failed = [name for name, ok, _ in results if not ok]
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
    print("=" * 74)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
