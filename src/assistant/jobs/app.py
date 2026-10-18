"""The procrastinate app and its queues (ARCHITECTURE §12.1, ADR-06).

Built once, in `bootstrap.py`, with its final connector: `App.with_connector()`
returns a new app and would silently drop registered tasks (M00 finding F8).

Queues and concurrency are explicit because procrastinate defaults to 1:

- `system`   — periodic tasks, reconcilers, heartbeat; concurrency 1.
- `outbound` — short, time-sensitive sends (M04 onwards); concurrency 2.
- `llm`      — agent runs and other model work; concurrency 3.

A slow `llm` job therefore never delays a `system` or `outbound` job: each
queue has its own worker.
"""

from __future__ import annotations

from dataclasses import dataclass

from procrastinate import App, PsycopgConnector

from assistant.db.base import JOBS_SCHEMA
from assistant.db.url import libpq_dsn

QUEUE_SYSTEM = "system"
QUEUE_OUTBOUND = "outbound"
QUEUE_LLM = "llm"

#: Seconds without a worker heartbeat before its jobs count as stalled. At
#: least 2x the longest event-loop stall we tolerate (§4.3), so recovery never
#: re-runs a job whose worker is merely busy.
STALLED_WORKER_TIMEOUT = 60.0
HEARTBEAT_INTERVAL = 10.0


@dataclass(frozen=True, slots=True)
class QueueSpec:
    name: str
    concurrency: int
    #: How long a shutdown waits for in-flight jobs before aborting them.
    shutdown_grace_seconds: float


QUEUES: tuple[QueueSpec, ...] = (
    QueueSpec(QUEUE_SYSTEM, concurrency=1, shutdown_grace_seconds=20.0),
    QueueSpec(QUEUE_OUTBOUND, concurrency=2, shutdown_grace_seconds=30.0),
    # Within Compose's 90 s stop grace period. An agent run cut off here is
    # re-run: the interrupted run is marked and the turn is re-deferred.
    QueueSpec(QUEUE_LLM, concurrency=3, shutdown_grace_seconds=45.0),
)


def build_app(database_url: str) -> App:
    """A procrastinate app on the configured database, in its own schema."""
    connector = PsycopgConnector(
        conninfo=libpq_dsn(database_url),
        kwargs={"options": f"-csearch_path={JOBS_SCHEMA},public"},
    )
    return App(connector=connector)


async def run_queue_worker(app: App, spec: QueueSpec) -> None:
    """One worker for one queue. Signals are the supervisor's job, not procrastinate's."""
    await app.run_worker_async(
        queues=[spec.name],
        name=f"worker-{spec.name}",
        concurrency=spec.concurrency,
        wait=True,
        install_signal_handlers=False,
        shutdown_graceful_timeout=spec.shutdown_grace_seconds,
        stalled_worker_timeout=STALLED_WORKER_TIMEOUT,
        update_heartbeat_interval=HEARTBEAT_INTERVAL,
        listen_notify=True,
    )
