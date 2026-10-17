# M00 S00.3 — jobs spike (A6, TQ-01)

Throwaway code (AGENTS §5): never imported by `src/`, excluded from
import-linter, ruff and mypy. Its deliverables are the decision record in
[`docs/milestones/M00.md`](../../docs/milestones/M00.md) and the pattern that
S01.1/S01.5 re-implement under `src/` with real tests.

```
docker compose -f compose.dev.yml run --rm app python spikes/jobs/spike.py
```

Creates and drops a throwaway `spike_jobs` database each run, applies the two
Alembic revisions under `migrations/`, and prints a PASS/FAIL table. Needs no
secrets and touches no Telegram or Anthropic API.

## What it answers

| | Question | Result (2026-10-17) |
|---|---|---|
| E1 | Can a job be deferred in the *same transaction* as the domain write (TQ-01)? | **Yes** — `defer_job_async(job, connection=...)` on the SQLAlchemy connection's `driver_connection`; rollback discards both, commit persists both. |
| E2 | Does a shared `lock` serialize execution? | Yes, with `concurrency=4`. |
| E3 | Does `queueing_lock` dedupe? | Yes, raising `AlreadyEnqueued`. |
| E4 | Is `schedule_in` honoured? | Yes. |
| E5 | Does a future-scheduled *locked* job block a later immediate job with the same lock? | **Yes** — confirms the hazard AGENTS §13 forbids. |
| E6 | Does `RetryStrategy` retry to success? | Yes. |
| E7 | Alembic up/down/up with procrastinate's schema vendored? | Yes. |

## Two patterns worth carrying into `src/`

**Transactional defer.** procrastinate accepts an external connection, so the
job row is written inside the caller's transaction:

```python
raw = await conn.get_raw_connection()
job = app.configure_task(name=..., allow_unknown=False).make_new_job(**kwargs)
await app.job_manager.defer_job_async(job, connection=raw.driver_connection)
```

**Generic downgrade.** Every object procrastinate creates is prefixed
`procrastinate_`, so the revision drops them by prefix rather than listing ~40
objects by hand — see `migrations/versions/0002_procrastinate_schema.py`.

## Gotchas found

- `App.with_connector()` returns a **new** app; tasks registered on the original
  stay there. Build the `App` with its final connector.
- `run_worker_async(wait=False)` exits as soon as it cannot fetch a job, and a
  job blocked by a held `lock` is indistinguishable from an empty queue. A test
  that drains once will conclude the lock "lost" a job.
