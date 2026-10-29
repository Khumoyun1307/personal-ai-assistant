# OPS-AV — NFR-08 availability recorder and the NFR-07/NFR-05 queries (junior unit)

| | |
|---|---|
| **Unit** | OPS-AV — the NFR-08 recorder (`observability/availability.py::AvailabilityRecorder`, implementing F0-S's `PollObserver`: one minute upsert per committed poll, in its own transaction), `deploy/sql/nfr08.sql` (minute coverage ∪ outage and restore gaps), `deploy/sql/nfr07.sql` (pause latency per source; zero automatic replies claimed while paused) and the escalation-latency extension of `deploy/sql/nfr05.sql`, all tested. Must be in deploy #1 |
| **Owner slot** | D2 (David's team, junior) |
| **Mentor** | **J2 (Claude; mandatory)** — the owner of the poller hook (F0-S) and of `nfr05.sql`'s extension review (ORG §6). Peer review dispatched by the TL |
| **QA** | QD1, **before merge** (depth: a recorder failure never blocks or delays a poll and never rolls back `last_poll_ok`; the SQL is tested) |
| **Size** | S+ (≈ 0.75 engineer-day, one fix round included) |
| **Depends on** | F0-S (`availability_minutes` table and `db/models.py::AvailabilityMinute`; the `PollObserver` hook and `Poller(poll_observer=…)` in `telegram/ingress/poller.py`; its named test; `p3_factories.seed_availability_minute`); F0-R (`P3Wiring.poll_observer`, `_wire_surfaces`, the poller built with the merged observer); F0-E (`deploy/server.sh report <name> [days]`, `tests/log_hygiene.py`). For PR 2 also M6-PAUSE's audit fields (`source`, `lane`, `trigger_at`, `update_id`, `cancelled` on `autonomy_paused`) and M6-PLAN's `delegation_escalation` rows — the SQL reads them as data and is tested with synthetic rows, so it does **not** wait for those units |
| **Consumed by** | Deploy #1 (the recorder starts the NFR-08 history ≥ 5 days before T0); SQ (NFR-05/07/08 at trial day 7 and day 14, `report nfr08 14`, `report nfr07 14`, `report nfr05 14`); OPS-VIEWS (trial report reads `availability_minutes`) |
| **Branch → base** | Two PRs (§12). PR 1 `p3/ops-av` (recorder + `nfr08.sql`) and PR 2 `p3/ops-av-latency` (`nfr07.sql` + `nfr05.sql`), both from `origin/int/p3` → `int/p3`. Before CP1, stack PR 1 on `origin/p3/f0-r` (it carries F0-S, and F0-R supplies `P3Wiring.poll_observer`) and retarget after F0-S and F0-R merge (WORKFLOW §4.2). PR 2 needs neither: it adds SQL only |
| **Normative inputs** | [P3-schema](../contracts/P3-schema.md) §2.3 (`availability_minutes`), §6 (90-day retention); [P3-interfaces](../contracts/P3-interfaces.md) §4.4 (`PollObserver`, `POLL_OBSERVER_TIMEOUT_SECONDS`, the exact upsert), §10.2 ("not flagged: the NFR-08 recorder"), §13 (`autonomy_paused` details), §14.3 (`report` and the SQL names), §15.1, §15.3 (`poll_observer` in `P3Wiring`), §15.4 (OPS-AV: ARCHITECTURE §17); [specs/M6-PAUSE.md](M6-PAUSE.md) §3.1 (the `autonomy_paused` audit fields `source`, `lane`, `trigger_at`, `update_id`, `apply_ms`, `cancelled`), §3.3 (fast lane), §3.8 (how latency is computed; the legacy `fast_lane` value); [specs/M6-PLAN.md](M6-PLAN.md) §3.6 step 3 (`delegation_escalation` priority and refs); decisions P3-T28, P3-T64 (OD-31: approvals survive a pause); decisions.md §C conflict 22 (S07.5 corrected: NFR-08 comes from the recorder and `coverage_gaps`), P3-T122 (the nfr08 semantics are this unit's; R86); PRD §17 (NFR-05, NFR-07, NFR-08) |

## 1. Goal

The Personal v1 exit needs three numbers that nothing can compute today:

- **NFR-08 ingestion availability ≥ 99 %.** The poller's heartbeat keeps only its latest value, so there is no history. F0-S created a content-free table, `availability_minutes` (one row per UTC minute in which a poll completed and committed), and a hook the poller calls after each committed poll. You write the recorder behind that hook and the query that turns minutes and recorded outage/restore gaps into an availability figure. The recorder must be live at deploy #1, so the trial has a baseline.
- **NFR-07 pause latency ≤ 5 s**, per source (`/pause`, the stop word, a blocked bot; the button is timed by stopwatch), computed from the audit fields M6-PAUSE records (`trigger_at`, `lane`, `apply_ms`, `update_id`; the lock wait counts), and **zero automatic replies claimed while paused**.
- **Escalation-alert latency** (a decision request in a delegated chat must alert the owner at once): one more measure in the existing `nfr05.sql`.

All three queries are read-only, content-free, run on the server with `just prod <ip> report <name> [days]`, and are tested on synthetic rows exactly as written. The recorder never blocks or slows ingestion: the poller calls it after its own commit, with a 2 s timeout, and logs its failure (F0-S's hook); the recorder itself catches nothing. A cancelled or failed upsert is one rolled-back transaction: it leaves no partial row, only a missing minute, which the query tolerates for one minute and counts as down beyond that.

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2, §3, §4 (PR template), §5, §6.2.
3. [P3-interfaces](../contracts/P3-interfaces.md) §4.4 and [P3-schema](../contracts/P3-schema.md) §2.3.
4. `src/assistant/telegram/ingress/poller.py` (after F0-S): `poll_once`, `_mark_poll_ok` (it stores `last_poll_ok` in its own transaction, then calls `poll_observer.on_poll_ok(now)` outside it, inside `asyncio.timeout(POLL_OBSERVER_TIMEOUT_SECONDS)` and its `try/except Exception`, logging `poll_observer_failed` with `error_class` only). You read it; you never edit it.
5. `tests/integration/test_poll_observer.py` (F0-S): the named hook test you complement with the real recorder.
6. `src/assistant/db/models.py::AvailabilityMinute` (F0-S) and `src/assistant/db/engine.py` (`build_engine`, `build_session_factory`).
7. `src/assistant/conversations/coverage.py::record_outage` and `src/assistant/privacy/post_restore.py` step 5: how `outage` and `restore` gaps are written (one row per active account; for a `possible` outage `at_risk_to` is **not** the end of the outage — `created_at` is).
8. `src/assistant/actions/autonomy.py::pause` (the `autonomy_paused` audit row: `at`, `details.source`, plus M6-PAUSE's `lane`, `trigger_at`, `update_id`, `cancelled`), `actions/lifecycle.py::_event` (`approved → executing` writes `action_claimed`, `subject_type = 'action'`).
9. `deploy/sql/nfr04.sql`, `deploy/sql/nfr05.sql` and `tests/integration/test_nfr05_query.py` (the house pattern: header comment, one `WITH` statement, psql variable `:days`, the test runs the file through SQLAlchemy `text()` on `db_session` with `{"days": n}`).
10. `tests/integration/test_ingestion.py` (building a `Poller` over `FakeUpdateSource` and a real session factory).
11. This spec, to the end.

## 3. Allowed files

**PR 1 (`p3/ops-av`): recorder and NFR-08**

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/observability/availability.py` | new | The whole module (§6.1) |
| `src/assistant/bootstrap.py` | existing | Inside `_wire_surfaces` only: one function-local import and `poll_observer=AvailabilityRecorder(ctx.session_factory)` in the returned `P3Wiring` (§6.2) |
| `deploy/sql/nfr08.sql` | new | The query of §6.3 |
| `tests/integration/test_availability_recorder.py` | new | §7.1 |
| `tests/integration/test_nfr08_query.py` | new | §7.2 |
| `tests/integration/test_log_hygiene_ops_av.py` | new | §7.5, copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` (under `tests/integration/`: it needs the database) |
| `docs/ARCHITECTURE.md` | existing | §17 only: the new bullet of §6.6 (a) |

**PR 2 (`p3/ops-av-latency`): NFR-07 and the escalation measure**

| Path | New or existing | Exactly what may change |
|---|---|---|
| `deploy/sql/nfr07.sql` | new | The query of §6.4 |
| `deploy/sql/nfr05.sql` | existing | The three edits of §6.5 only (header lines, one CTE, one `UNION ALL` member). The three existing measures stay byte-for-byte |
| `tests/integration/test_nfr07_query.py` | new | §7.3 |
| `tests/integration/test_nfr05_query.py` | existing | The edits of §5 and the two new tests of §7.4, appended at the end. No existing assertion value changes |
| `docs/ARCHITECTURE.md` | existing | §17 only: the sentence of §6.6 (b), appended to your PR 1 bullet |

Nothing else, in either PR.

## 4. Forbidden

- `src/assistant/telegram/ingress/` (including `poller.py`: the hook is F0-S's and frozen), `src/assistant/telegram/gateway.py`, the rest of `src/assistant/telegram/`, `src/assistant/policy/`, `src/assistant/actions/`, `migrations/`, `src/assistant/db/` (`AvailabilityMinute` is F0-S's), `src/assistant/privacy/` (the 90-day sweep is F0-S's), `src/assistant/agent/`, consent code, `src/assistant/config.py`, `src/assistant/core/`, `src/assistant/jobs/`, `AGENTS.md`.
- `src/assistant/observability/status.py`, `heartbeat.py` and every other existing `observability/` file.
- `deploy/server.sh` (F0-E's `report` action already whitelists every `deploy/sql/*.sql`; you add files only), `deploy/sql/nfr04.sql`, `deploy/sql/usefulness.sql` (OPS-RATE).
- `tests/integration/test_poll_observer.py` (F0-S), `tests/integration/test_ingestion.py`, `tests/integration/p3_factories.py`.
- Imports in `availability.py` other than the standard library, `sqlalchemy` and `sqlalchemy.ext.asyncio`. No logger: the recorder logs nothing (the poller logs a failure).
- Any `try`/`except` in the recorder (the poller's hook is the one sanctioned catch, P3-interfaces §4.4).
- Every other wire function in `bootstrap.py` and every line outside `_wire_surfaces`.
- `pyproject.toml`, `uv.lock`, `.env*`, `var/`, every `docs/` file except the §17 bullet.
- **Every file not in §3.**

## 5. Shared lists you must update

PR 1: none. The recorder is not flagged (P3-interfaces §10.2), and `availability_minutes` is already in F0-S's migration, retention and tenant-exemption lists.

PR 2: one existing test file holds the list of `nfr05.sql` measures, and adding a measure changes it. In `tests/integration/test_nfr05_query.py` make exactly these edits:
- `MEASURES = [...]` gains `"escalation alert delivery"` as its fourth (last) element;
- in `test_rows_outside_the_window_are_ignored`, `[0, 0, 0]` becomes `[0, 0, 0, 0]` (one more measure, still all zero);
- `_alert(...)` gains a keyword `priority: str = "urgent"`, and its SQL uses `:priority` with `"priority": priority` in the parameters instead of the literal `'urgent'` (the default keeps every existing call the same).

Nothing else in that file changes except the two tests you append (§7.4). Say so in the PR's "Shared lists updated".

## 6. Step-by-step

### 6.1 `src/assistant/observability/availability.py` (exact API)

```python
"""NFR-08 availability recorder (P3 OPS-AV; P3-schema §2.3, P3-interfaces §4.4).

The poller's `PollObserver`: after each poll whose `last_poll_ok` commit
succeeded, the poller calls `on_poll_ok(at)` outside that transaction, with a
2 s timeout and its own catch. This records the UTC minute of `at` in
`availability_minutes` in a transaction of its own: one row per minute with a
completed, committed poll; `polls` counts them (diagnostics only). It never
catches an error: a failure must reach the poller's one sanctioned handler,
which logs the class name and never blocks, delays or rolls back a poll.
`deploy/sql/nfr08.sql` turns the minutes into the availability figure."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: The exact upsert of P3-interfaces §4.4. `availability_minutes.polls` is
#: table-qualified: a bare `polls` is ambiguous with `excluded.polls`.
UPSERT_MINUTE: Final = text(
    "INSERT INTO app.availability_minutes (minute, polls) VALUES (:minute, 1) "
    "ON CONFLICT (minute) DO UPDATE SET polls = availability_minutes.polls + 1"
)


def minute_of(at: datetime) -> datetime: ...


class AvailabilityRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...
    async def on_poll_ok(self, at: datetime) -> None: ...
```

These names are binding (the wiring and the tests use them). The class satisfies F0-S's `PollObserver` Protocol structurally; do not import the Protocol (that would import `assistant.telegram` into `observability`).

**Rules:**
1. **`minute_of(at)`**: `at.tzinfo is None` ⇒ `ValueError("at must be timezone-aware")`; else `at.astimezone(UTC).replace(second=0, microsecond=0)`.
2. **`on_poll_ok(at)`**: `minute = minute_of(at)` first (a naive time raises before any session opens); then `async with self._session_factory() as session, session.begin(): await session.execute(UPSERT_MINUTE, {"minute": minute})`. Nothing else: no read, no log, no retry, no `except`.
3. The poller passes the same `now` it stored as `last_poll_ok` (a UTC `Clock` value), so the minute written is the poll's minute.

### 6.2 Wiring (PR 1; `_wire_surfaces` in `bootstrap.py`; function-local import)

```python
    # OPS-AV (not flagged: ops instrumentation, live at deploy #1)
    from assistant.observability.availability import AvailabilityRecorder
```

and `poll_observer=AvailabilityRecorder(ctx.session_factory)` as a keyword of the `P3Wiring(...)` that `_wire_surfaces` returns (keep every keyword already there; if the function still returns a bare `P3Wiring()`, change it to `P3Wiring(poll_observer=AvailabilityRecorder(ctx.session_factory))`). F0-R passes it to the poller (at most one observer; a second raises at startup). The recorder is wired in every process; only the poller role calls it.

### 6.3 `deploy/sql/nfr08.sql` (PR 1)

Exactly this query, with this header comment (the comment may wrap differently; the code may not change):

```sql
-- NFR-08 (PRD §17; P3 OPS-AV; IMPLEMENTATION_PLAN S07.5 as corrected by the
-- TL, P3 decisions conflict 22): ingestion availability over the last `days`
-- days, at most 90 (the availability_minutes retention). Run on the server
-- with `just prod <ip> report nfr08 [days]` (deploy/server.sh `report`: psql,
-- read-only transaction, psql variable `days`, default 7); tested in
-- tests/integration/test_nfr08_query.py.
--
-- Content-free by construction: minute timestamps and poll counts
-- (availability_minutes) and the kind and times of coverage gaps; never a
-- content column.
--
-- The window is whole UTC minutes: the current minute (excluded) minus
-- 1440 x `days` minutes. A minute is DOWN when
--   (a) no poll was recorded in it or in the minute before it (a healthy
--       50 s long poll can complete on either side of one minute boundary,
--       so one missing minute between two polls is tolerated), or
--   (b) any part of it lies inside a recorded `outage` gap (at_risk_from to
--       created_at: a `possible` outage's at_risk_to is not its end) or
--       `restore` gap (at_risk_from to at_risk_to: data after the backup may
--       be lost). Both are conservative: a touched minute counts as down.
-- availability = 1 - down / window minutes; 'met' when down <= 1 % of the
-- window. 'not evaluable' when the first recorded minute is after the window
-- start (before deploy #1, or beyond the retention); 'no data' when nothing
-- was ever recorded. Outages shorter than a minute and before the first
-- recorded minute are not seen.
WITH bounds AS (
    SELECT
        date_trunc('minute', now())
            - make_interval(mins => 1440 * least(CAST(:days AS integer), 90)) AS w_start,
        date_trunc('minute', now()) AS w_end
),
minutes AS (
    SELECT m.minute
    FROM bounds AS b
    CROSS JOIN LATERAL generate_series(
        b.w_start, b.w_end - interval '1 minute', interval '1 minute'
    ) AS m (minute)
),
gaps AS (
    SELECT DISTINCT
        g.kind,
        g.at_risk_from AS g_from,
        CASE
            WHEN g.kind = 'outage' THEN greatest(g.created_at, g.at_risk_to)
            ELSE g.at_risk_to
        END AS g_to
    FROM app.coverage_gaps AS g
    WHERE g.kind IN ('outage', 'restore')
),
window_gaps AS (
    SELECT gp.kind, gp.g_from, gp.g_to
    FROM gaps AS gp
    CROSS JOIN bounds AS b
    WHERE gp.g_from < b.w_end
        AND gp.g_to > b.w_start
),
marked AS (
    SELECT
        mi.minute,
        EXISTS (
            SELECT 1 FROM app.availability_minutes AS a
            WHERE a.minute = mi.minute
        ) AS polled,
        EXISTS (
            SELECT 1 FROM app.availability_minutes AS a
            WHERE a.minute IN (mi.minute, mi.minute - interval '1 minute')
        ) AS covered,
        EXISTS (
            SELECT 1 FROM window_gaps AS g
            WHERE g.g_from < mi.minute + interval '1 minute'
                AND g.g_to > mi.minute
        ) AS in_gap
    FROM minutes AS mi
),
summary AS (
    SELECT
        count(*) AS window_min,
        count(*) FILTER (WHERE polled) AS polled_min,
        count(*) FILTER (WHERE NOT covered OR in_gap) AS down_min
    FROM marked
),
recorder AS (
    SELECT min(a.minute) AS recorded_since
    FROM app.availability_minutes AS a
)
SELECT
    b.w_start AS window_start,
    b.w_end AS window_end,
    s.window_min,
    s.polled_min,
    s.down_min,
    round(100.0 * (s.window_min - s.down_min) / s.window_min, 3) AS availability_pct,
    99.0 AS target_pct,
    CAST(floor(s.window_min / 100.0) AS integer) AS budget_min,
    (SELECT count(*) FROM window_gaps WHERE kind = 'outage') AS outage_gaps,
    (SELECT count(*) FROM window_gaps WHERE kind = 'restore') AS restore_gaps,
    r.recorded_since,
    CASE
        WHEN r.recorded_since IS NULL THEN 'no data'
        WHEN r.recorded_since > b.w_start THEN 'not evaluable'
        WHEN 100 * s.down_min <= s.window_min THEN 'met'
        ELSE 'NOT met'
    END AS verdict
FROM bounds AS b
CROSS JOIN summary AS s
CROSS JOIN recorder AS r;
```

Why the window uses `1440 × days` minutes and not `days => n`: a calendar-day interval depends on the session's time zone across a DST change; minutes do not.

### 6.4 `deploy/sql/nfr07.sql` (PR 2)

Exactly this query and header:

```sql
-- NFR-07 (PRD §17; PRD §21 R2 #2; P3 OPS-AV): how fast a pause stops
-- automatic replies, per pause source, and whether any automatic reply was
-- claimed while autonomy was paused. Run on the server with
-- `just prod <ip> report nfr07 [days]` (deploy/server.sh `report`; default
-- 7); tested in tests/integration/test_nfr07_query.py.
--
-- Content-free by construction: audit events with their `source`, `lane`,
-- `trigger_at`, `update_id`, `apply_ms` and `cancelled` codes and numbers,
-- timestamps, and action origins; never a content column.
--
-- Latency = the `autonomy_paused` audit row's `at` minus its `trigger_at`
-- (the pausing update's Telegram date in epoch seconds, written by M6-PAUSE
-- for /pause, the stop word and a blocked bot) plus `apply_ms` (fast lane
-- only: the wait for the account lock inside the poller's transaction, which
-- starts after `at` was sampled). A fast-lane row (`lane` = 'fast') without
-- a numeric `apply_ms` would understate the latency, so it is counted, not
-- timed. A Pause-button press has no trigger time (it pauses in the
-- transaction that stores the press; LIVE-P3 times it by stopwatch);
-- `tool`, `card`, a bot block found by a failed control send (lane 'send')
-- and the legacy M04 value `fast_lane` have none either. Those rows are
-- counted, not timed. The verdict uses the slowest timed pause of a source,
-- because NFR-07 says "within 5 s", every time; read it together with `n`
-- and `timed`. `fast_lane_n` counts the rows written by the fast lane and
-- `update_id_n` those that carry Telegram's update id (a number, never
-- content) so a reviewer can pair a row with its update.
-- "automatic replies claimed while paused" counts `action_claimed` audit
-- rows of `delegation_auto` actions after a pause and before the same
-- account's next `autonomy_resumed`; it must be 0 (invariant 10). OPS-TV3's
-- trial-check is the authoritative send audit; this is the NFR-07 summary.
WITH pauses AS (
    SELECT
        p.account_id,
        p.at,
        CASE
            WHEN p.details ->> 'source' IN (
                'command', 'stop_word', 'button', 'bot_blocked', 'tool', 'card', 'fast_lane'
            ) THEN p.details ->> 'source'
            ELSE 'other'
        END AS source,
        p.details ->> 'lane' AS lane,
        CASE
            WHEN jsonb_typeof(p.details -> 'update_id') = 'number'
                AND (p.details ->> 'update_id') ~ '^[0-9]+$'
                THEN 1
        END AS traced,
        CASE
            WHEN jsonb_typeof(p.details -> 'trigger_at') = 'number'
                AND (
                    p.details ->> 'lane' IS DISTINCT FROM 'fast'
                    OR jsonb_typeof(p.details -> 'apply_ms') = 'number'
                )
                THEN CAST(extract(EPOCH FROM p.at) AS float8)
                    - CAST(p.details ->> 'trigger_at' AS float8)
                    + CASE
                        WHEN jsonb_typeof(p.details -> 'apply_ms') = 'number'
                            THEN CAST(p.details ->> 'apply_ms' AS float8) / 1000.0
                        ELSE 0
                    END
        END AS latency_s,
        CASE
            WHEN jsonb_typeof(p.details -> 'cancelled') = 'number'
                THEN CAST(p.details ->> 'cancelled' AS integer)
            ELSE 0
        END AS cancelled
    FROM app.audit_log AS p
    WHERE p.event = 'autonomy_paused'
        AND p.at >= now() - make_interval(days => CAST(:days AS integer))
),
sources (ord, source, label) AS (
    VALUES
        (1, 'command', 'pause latency: command'),
        (2, 'stop_word', 'pause latency: stop_word'),
        (3, 'button', 'pause latency: button'),
        (4, 'bot_blocked', 'pause latency: bot_blocked'),
        (5, 'tool', 'pause latency: tool'),
        (6, 'card', 'pause latency: card'),
        (7, 'fast_lane', 'pause latency: fast_lane (legacy)'),
        (8, 'other', 'pause latency: other')
),
per_source AS (
    SELECT
        s.ord,
        s.label,
        count(p.at) AS n,
        count(p.latency_s) AS timed,
        count(*) FILTER (WHERE p.lane = 'fast') AS fast_n,
        count(p.traced) AS traced_n,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY p.latency_s) AS p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY p.latency_s) AS p95,
        max(p.latency_s) AS max_s
    FROM sources AS s
    LEFT JOIN pauses AS p ON p.source = s.source
    GROUP BY s.ord, s.label
),
claims AS (
    SELECT count(DISTINCT c.id) AS n
    FROM pauses AS p
    JOIN app.audit_log AS c
        ON c.account_id = p.account_id
        AND c.event = 'action_claimed'
        AND c.subject_type = 'action'
        AND c.at > p.at
        AND c.at < coalesce(
            (
                SELECT min(r.at)
                FROM app.audit_log AS r
                WHERE r.account_id = p.account_id
                    AND r.event = 'autonomy_resumed'
                    AND r.at > p.at
            ),
            CAST('infinity' AS timestamptz)
        )
    JOIN app.actions AS a
        ON a.account_id = c.account_id
        AND a.id = c.subject_id
        AND a.origin = 'delegation_auto'
),
measured (ord, measure, n, timed, fast_n, traced_n, p50, p95, max_s, limit_s) AS (
    SELECT ord, label, n, timed, fast_n, traced_n, p50, p95, max_s, 5
    FROM per_source
    UNION ALL
    SELECT
        9, 'automatic replies claimed while paused', (SELECT n FROM claims),
        CAST(NULL AS bigint), CAST(NULL AS bigint), CAST(NULL AS bigint),
        CAST(NULL AS float8), CAST(NULL AS float8), CAST(NULL AS float8), 0
    UNION ALL
    SELECT
        10, 'automatic replies cancelled by a pause',
        (SELECT coalesce(sum(cancelled), 0) FROM pauses),
        CAST(NULL AS bigint), CAST(NULL AS bigint), CAST(NULL AS bigint),
        CAST(NULL AS float8), CAST(NULL AS float8), CAST(NULL AS float8),
        CAST(NULL AS integer)
)
SELECT
    measure,
    n,
    timed,
    fast_n AS fast_lane_n,
    traced_n AS update_id_n,
    round(p50::numeric, 1) AS p50_s,
    round(p95::numeric, 1) AS p95_s,
    round(max_s::numeric, 1) AS max_s,
    limit_s,
    CASE
        WHEN ord <= 8 AND n = 0 THEN 'no data'
        WHEN ord <= 8 AND timed = 0 THEN 'not timed'
        WHEN ord <= 8 AND max_s <= limit_s THEN 'met'
        WHEN ord <= 8 THEN 'NOT met'
        WHEN ord = 9 AND (SELECT count(*) FROM pauses) = 0 THEN 'no data'
        WHEN ord = 9 AND n = 0 THEN 'met'
        WHEN ord = 9 THEN 'NOT met'
        ELSE 'informational'
    END AS verdict
FROM measured
ORDER BY ord;
```

If PostgreSQL rejects a type in the `UNION ALL` (for example `sum` returning `numeric` where `count` returns `bigint`), add a `CAST(... AS bigint)` around the value, and only that; say so in the PR.

### 6.5 `deploy/sql/nfr05.sql` (PR 2; three edits, nothing else)

1. **Header:** after the `urgent alert delivery` lines of the "Measures:" comment, add:

   ```sql
   --   escalation alert delivery
   --                           sent `urgent` `delegation_escalation`
   --                           notifications (a decision request in a
   --                           delegated chat, M06): sent_at minus the newest
   --                           sent_at among refs.message_ids. Target p95 <
   --                           2 min (PRD §21 R2 #1: a decision request
   --                           triggers an immediate alert). P3 OPS-AV.
   ```
2. **A new CTE**, placed right after the `urgent AS (...)` CTE (with its comma):

   ```sql
   escalation AS (
       SELECT extract(EPOCH FROM n.sent_at - newest.sent_at) AS s
       FROM app.notifications AS n
       CROSS JOIN LATERAL (
           SELECT max(m.sent_at) AS sent_at
           FROM jsonb_array_elements_text(coalesce(n.refs -> 'message_ids', '[]'::jsonb)) AS ref (id)
           JOIN app.messages AS m
               ON m.account_id = n.account_id
               AND m.id = CAST(ref.id AS bigint)
       ) AS newest
       WHERE n.kind = 'delegation_escalation'
           AND n.priority = 'urgent'
           AND n.status = 'sent'
           AND newest.sent_at IS NOT NULL
           AND n.sent_at >= now() - make_interval(days => CAST(:days AS integer))
   ),
   ```
3. **A fourth measured row**, appended as the last member of `measured (...)` (after the `FROM immediate` member):

   ```sql
       UNION ALL
       SELECT
           4, 'escalation alert delivery', count(*),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY s),
           percentile_cont(0.95) WITHIN GROUP (ORDER BY s),
           120, CAST(NULL AS float8)
       FROM escalation
   ```

The final `SELECT` and its verdict rules are unchanged; the new row uses them as they are (p95 < 120 ⇒ `met`).

### 6.6 ARCHITECTURE §17

**(a) PR 1** — add this bullet directly after the "External heartbeat" bullet:

```markdown
- **Availability (NFR-08; as built, P3 OPS-AV).** `observability/availability.py::AvailabilityRecorder`, the poller's `PollObserver`, upserts one `availability_minutes` row per UTC minute with a completed, committed poll, in its own transaction after the `last_poll_ok` commit; it catches nothing, and the poller logs a failure or a 2 s timeout as `poll_observer_failed` (class name only) without blocking, delaying or rolling back the poll. Not flagged; live from deploy #1. `deploy/sql/nfr08.sql` (`server.sh report nfr08 [days]`, window capped at the 90-day retention) counts a minute as down when no poll was recorded in it or in the minute before (one missing minute between polls is tolerated), or when any part of it lies in a recorded `outage` gap (to its `created_at`) or `restore` gap (to its `at_risk_to`); availability = 1 − down / window minutes, target ≥ 99 %; "not evaluable" when the window starts before the first recorded minute.
```

**(b) PR 2** — append to that bullet:

```markdown
 `deploy/sql/nfr07.sql` reports pause latency per source (`autonomy_paused` `at` − `trigger_at` + `apply_ms`, the fast-lane lock wait; every timed pause ≤ 5 s; the button, `tool`, `card` and control-send pauses are counted, not timed) and automatic replies claimed while paused (must be 0); `nfr05.sql` adds escalation-alert delivery (sent `urgent` `delegation_escalation`, p95 < 2 min).
```

### 6.7 Steps (each ends with a check)

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd2'` set, from PowerShell on the host (they run inside the container). If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR (WORKFLOW §5).

**PR 1**
1. **Branch.** `git fetch origin`; `git switch -c p3/ops-av origin/int/p3` (before CP1: `origin/p3/f0-r`). **Check:** `just test tests/integration/test_poll_observer.py -q` → passes (F0-S's hook is on your base).
2. **Red tests.** Create `tests/integration/test_availability_recorder.py`, `tests/integration/test_nfr08_query.py` with every test of §7.1–§7.2, `availability.py` with the docstring, `UPSERT_MINUTE` and bodies `raise NotImplementedError`, and `deploy/sql/nfr08.sql` containing only `SELECT 1;`. **Check:** `just test tests/integration/test_availability_recorder.py tests/integration/test_nfr08_query.py -q` → the tests fail for the right reason (`NotImplementedError`, a wrong column or value), none with `ImportError`.
3. **Recorder.** **Check:** `just test tests/integration/test_availability_recorder.py -q -k "not wire"` → passes.
4. **Wiring** (§6.2). **Check:** `just test tests/integration/test_availability_recorder.py tests/integration/test_poll_observer.py -q` → all pass.
5. **`nfr08.sql`** (§6.3). **Check:** `just test tests/integration/test_nfr08_query.py -q` → passes.
6. **Hygiene test** (§7.5). **Check:** `just test tests/integration/test_log_hygiene_ops_av.py -q` → passes.
7. **ARCHITECTURE** (§6.6 a). **Check:** `git diff origin/int/p3 -- docs/ARCHITECTURE.md` shows one added bullet in §17.
8. **Static checks.** `docker compose -f compose.dev.yml run --rm app uv run ruff check src/assistant/observability/availability.py src/assistant/bootstrap.py tests/integration/test_availability_recorder.py tests/integration/test_nfr08_query.py tests/integration/test_log_hygiene_ops_av.py`; the same with `uv run ruff format --check`; `… uv run mypy src/assistant/observability/availability.py src/assistant/bootstrap.py`; `… uv run lint-imports`. **Check:** all clean.
9. **Neighbours unchanged:** `just test tests/integration/test_ingestion.py tests/integration/test_ingestion_barrier.py -q`. **Check:** all pass.
10. **Diff.** `git diff --stat origin/int/p3...HEAD` → exactly the seven PR 1 files of §3. Commit at step 2 and step 8 (`git commit -F <file>`); open PR 1 (§12).

**PR 2** (start when PR 1 is ready for review; it does not depend on PR 1's code)
11. **Branch.** `git switch -c p3/ops-av-latency origin/int/p3`. **Check:** `just test tests/integration/test_nfr05_query.py -q` → passes (M05's query is on your base).
12. **Red tests.** Make the §5 edits and append §7.4's tests to `test_nfr05_query.py`; create `tests/integration/test_nfr07_query.py` (§7.3) and `deploy/sql/nfr07.sql` containing only `SELECT 1;`. **Check:** `just test tests/integration/test_nfr05_query.py tests/integration/test_nfr07_query.py -q` → the new tests and the four-measure assertions fail; the old nfr05 assertions fail only because the fourth measure is missing.
13. **`nfr05.sql`** (§6.5). **Check:** `just test tests/integration/test_nfr05_query.py -q` → all pass.
14. **`nfr07.sql`** (§6.4). **Check:** `just test tests/integration/test_nfr07_query.py -q` → passes.
15. **ARCHITECTURE** (§6.6 b; after PR 1 merged, rebase first). **Check:** the diff adds the sentence only.
16. **Static checks** on the two test files (`ruff check`, `ruff format --check`). **Check:** clean.
17. **Diff.** `git diff --stat origin/int/p3...HEAD` → exactly the five PR 2 files of §3. Commit and open PR 2 (§12).

## 7. Named tests (red first)

### 7.1 `tests/integration/test_availability_recorder.py`

A fixture `factory(migrated_url)` builds an engine and session factory and first runs `TRUNCATE app.availability_minutes, app.inbound_updates, app.system_state CASCADE` (the `test_ingestion.py` fixture pattern). `rows(factory)` returns `[(minute, polls), …]` ordered by minute. A **broken recorder** is `AvailabilityRecorder(build_session_factory(build_engine(make_url(migrated_url).set(database="ops_av_canary_synth_db").render_as_string(hide_password=False))))` (a database that does not exist; dispose its engine at the end of the test). `T = datetime(2026, 11, 2, 10, 15, 42, 500000, tzinfo=UTC)`.

- `test_minute_of_truncates_to_the_utc_minute` — `minute_of(T) == datetime(2026, 11, 2, 10, 15, tzinfo=UTC)`; `minute_of(datetime(2026, 11, 2, 15, 15, 42, tzinfo=ZoneInfo("Asia/Tashkent"))) == datetime(2026, 11, 2, 10, 15, tzinfo=UTC)`.
- `test_minute_of_refuses_a_naive_time` — `minute_of(datetime(2026, 11, 2, 10, 15))` → `ValueError`.
- `test_a_poll_records_its_utc_minute` — `await AvailabilityRecorder(factory).on_poll_ok(T)`; `rows == [(datetime(2026, 11, 2, 10, 15, tzinfo=UTC), 1)]`.
- `test_polls_in_one_minute_share_one_row` — calls at `10:15:01`, `10:15:30`, `10:15:59.999999` → `rows == [(10:15, 3)]`.
- `test_the_next_minute_gets_a_new_row` — calls at `10:15:59` and `10:16:00` → `rows == [(10:15, 1), (10:16, 1)]`.
- `test_a_naive_time_writes_nothing` — `on_poll_ok(datetime(2026, 11, 2, 10, 15))` → `ValueError`; `rows == []`.
- `test_a_recorder_error_reaches_the_caller` — `await broken.on_poll_ok(T)` raises `sqlalchemy.exc.OperationalError` (the recorder catches nothing; the poller's hook is the one handler).
- `test_the_poller_records_a_minute_after_each_committed_poll` — `clock = FakeClock(T)`; `Poller(FakeUpdateSource([[], [make_update(1)]]), factory, clock, poll_observer=AvailabilityRecorder(factory))`; `await poller.startup()`; two `poll_once()` calls → `rows == [(10:15, 2)]`; `clock.advance(minutes=1)`; one more `poll_once()` → `rows == [(10:15, 2), (10:16, 1)]`.
- `test_a_failing_recorder_never_blocks_a_poll_or_rolls_back_last_poll_ok` — the same poller with `poll_observer=broken`, inside `capture_logs()`: `poll_once()` returns (no exception); `system_state['last_poll_ok']` read in a fresh session equals `{"at": T.isoformat()}`; a second `poll_once()` also returns and `FakeUpdateSource.calls` has two entries; `rows == []`; the captured logs contain the event `poll_observer_failed` with `error_class == "OperationalError"`.
- `test_wire_surfaces_wires_the_availability_recorder` — call `bootstrap._wire_surfaces(ctx)` with the context helper F0-R's `tests/integration/test_p3_wiring.py` uses (import it; never copy it): `isinstance(wiring.poll_observer, AvailabilityRecorder)`, and its session factory is `ctx.session_factory`.

### 7.2 `tests/integration/test_nfr08_query.py`

Copy the shape of `test_nfr05_query.py`: `QUERY = <repo>/deploy/sql/nfr08.sql`; `_code()` strips comments; `_measure(session, days)` runs `text(QUERY.read_text("utf-8"))` with `{"days": days}` and returns the single row. Each test first runs `DELETE FROM app.availability_minutes` and `DELETE FROM app.coverage_gaps` on `db_session` (rolled back with the test). Helpers (minute index `i` counts from the window start, `w_start = date_trunc('minute', now()) - make_interval(mins => 1440 * :d)`):

- `_poll(session, days, indexes)`: `INSERT INTO app.availability_minutes (minute, polls) SELECT date_trunc('minute', now()) - make_interval(mins => 1440 * CAST(:d AS integer)) + make_interval(mins => i), 1 FROM unnest(CAST(:ix AS integer[])) AS t (i)`.
- `_account(session, telegram_user_id) -> int`: `INSERT INTO app.accounts (telegram_user_id) VALUES (:t) RETURNING id`. Use `50808` for the first account of a test and `50809` for the second.
- `_gap(session, account, *, days, kind, certainty, start_s, end_s, created_s)`: inserts a `coverage_gaps` row (`conversation_id` NULL) with `at_risk_from`, `at_risk_to` and `created_at` = `w_start + make_interval(secs => <the value>)`.

Tests (`days = 1` unless named; `ALL = range(0, 1440)`):
- `test_the_query_reads_no_content_column` — for every `(table, column)` in `CONTENT_COLUMNS` whose `app.<table>` appears in the code, the bare column name does not appear.
- `test_the_query_reads_only_availability_and_coverage_gaps` — the set of `app.<name>` references is `{"availability_minutes", "coverage_gaps"}`.
- `test_the_query_is_one_read_only_statement` — starts with `WITH `, exactly one `;` at the end, no `insert|update|delete|truncate|drop|alter|create` word.
- `test_every_minute_polled_is_fully_available` — `_poll(ALL)` plus index `1440` (the current minute). Row: `window_min == 1440`, `polled_min == 1440` (the current minute is outside the window), `down_min == 0`, `availability_pct == Decimal("100.000")`, `target_pct == Decimal("99.0")`, `budget_min == 14`, `outage_gaps == 0`, `restore_gaps == 0`, `recorded_since == window_start`, `window_end - window_start == timedelta(days=1)`, `verdict == "met"`.
- `test_one_missing_minute_between_polls_is_tolerated` — `ALL` without `600`: `polled_min == 1439`, `down_min == 0`, `verdict == "met"`.
- `test_two_missing_minutes_count_one_down` — `ALL` without `600` and `601`: `polled_min == 1438`, `down_min == 1`, `availability_pct == Decimal("99.931")`.
- `test_an_outage_gap_counts_every_minute_it_touches` — `ALL` without `100…109`; two accounts, each with an `outage`/`delayed` gap `start_s = 5970` (minute 99 + 30 s), `end_s = created_s = 6620` (minute 110 + 20 s). `polled_min == 1430`, `down_min == 12` (minutes 99…110), `outage_gaps == 1` (identical per-account rows count once), `availability_pct == Decimal("99.167")`, `verdict == "met"`.
- `test_a_possible_outage_ends_at_its_creation_not_at_risk_to` — `days = 2`, `_poll(range(0, 2880))`; one `outage`/`possible` gap `start_s = 600`, `end_s = 1200`, `created_s = 87000`. `window_min == 2880`, `down_min == 1440` (minutes 10…1449), `budget_min == 28`, `availability_pct == Decimal("50.000")`, `verdict == "NOT met"`.
- `test_a_restore_gap_counts_from_the_backup_time` — `_poll(ALL)`; one `restore`/`possible` gap `start_s = 3600`, `end_s = created_s = 10800`. `down_min == 120`, `restore_gaps == 1`, `availability_pct == Decimal("91.667")`, `verdict == "NOT met"`.
- `test_dead_letter_gaps_are_ignored` — `_poll(ALL)`; one `dead_letter`/`definite` gap `start_s = 0`, `end_s = created_s = 6000`. `down_min == 0`, `verdict == "met"`.
- `test_the_target_is_one_percent_of_the_window` — parametrized `(end_s, down, verdict)`: `(840, 14, "met")`, `(900, 15, "NOT met")`; `_poll(ALL)`; one `restore`/`possible` gap `start_s = 0`, `end_s = created_s = <end_s>`. `down_min == down`, `verdict == verdict`.
- `test_a_window_older_than_the_recorder_is_not_evaluable` — `_poll(range(840, 1440))`: `polled_min == 600`, `down_min == 840`, `recorded_since == window_start + timedelta(minutes=840)`, `verdict == "not evaluable"`.
- `test_nothing_recorded_is_no_data` — no rows: `polled_min == 0`, `down_min == 1440`, `availability_pct == Decimal("0.000")`, `recorded_since is None`, `verdict == "no data"`.
- `test_the_window_is_capped_at_the_retention` — `days = 120`, no rows: `window_min == 129600`, `verdict == "no data"`.

### 7.3 `tests/integration/test_nfr07_query.py` (PR 2)

Same shape (`QUERY = …/nfr07.sql`, `_code()`, `_measure(session, days)` returning `{measure: row}` after asserting `[row.measure for row in rows] == MEASURES`). Each test first runs `DELETE FROM app.audit_log` on `db_session` (rolled back with the test; old actions without audit rows cannot match, so no other table is cleared). Accounts, chats and actions come from `p2_factories` (`seed_account`, `seed_conversation`, `seed_action(session, account_id=…, conversation_id=…, origin=…, state="succeeded")`), which work on `db_session`.

```python
MEASURES = [
    "pause latency: command",
    "pause latency: stop_word",
    "pause latency: button",
    "pause latency: bot_blocked",
    "pause latency: tool",
    "pause latency: card",
    "pause latency: fast_lane (legacy)",
    "pause latency: other",
    "automatic replies claimed while paused",
    "automatic replies cancelled by a pause",
]
```

Helpers (times in whole seconds so latencies are exact):
- `_pause(session, a, *, ago, source, latency=None, cancelled=0, lane="fast", apply_ms=0, update_id=None)`: `INSERT INTO app.audit_log (account_id, at, actor, event, subject_type, subject_id, details) VALUES (:a, date_trunc('second', now()) - make_interval(secs => CAST(:ago AS integer)), 'owner', 'autonomy_paused', 'account', :a, jsonb_build_object('source', CAST(:src AS text), 'lane', CAST(:lane AS text), 'trigger_at', CAST(extract(EPOCH FROM date_trunc('second', now())) AS bigint) - CAST(:ago AS integer) - CAST(:lat AS integer), 'update_id', CAST(:uid AS bigint), 'apply_ms', CAST(:ms AS integer), 'cancelled', CAST(:c AS integer)))` (`latency=None` gives `trigger_at: null`; `lane=None`, `apply_ms=None` and `update_id=None` give JSON nulls, which the query reads as absent). The defaults model a normal fast-lane pause with no lock wait.
- `_resume(session, a, *, ago)`: the same insert with event `autonomy_resumed`, details `'{"source": "command"}'`.
- `_claim(session, a, action_id, *, ago)`: event `action_claimed`, actor `system`, `subject_type = 'action'`, `subject_id = action_id`, details `'{"from": "approved", "to": "executing"}'`.

Tests (`days = 1` unless named):
- `test_the_query_reads_no_content_column` (the §7.2 rule), `test_the_query_reads_only_audit_log_and_actions` (`{"audit_log", "actions"}`), `test_the_query_is_one_read_only_statement`.
- `test_pause_latency_is_measured_per_source_from_the_trigger_time` — `command` pauses at `ago` 600, 500, 400 with latencies 1, 2, 3; one `stop_word` pause at `ago` 300 with latency 6. `command`: `n == 3`, `timed == 3`, `p50_s == Decimal("2.0")`, `p95_s == Decimal("2.9")`, `max_s == Decimal("3.0")`, `limit_s == 5`, `verdict == "met"`. `stop_word`: `n == 1`, `max_s == Decimal("6.0")`, `verdict == "NOT met"`. `button`: `n == 0`, `verdict == "no data"`.
- `test_untimed_sources_are_counted_but_not_timed` — one pause each with `latency=None`: `button`, `tool`, `fast_lane`, and source `"mystery"`. Rows `pause latency: button`, `… tool`, `… fast_lane (legacy)` and `… other` each have `n == 1`, `timed == 0`, `max_s is None`, `verdict == "not timed"`.
- `test_the_lock_wait_is_added_to_the_latency` — two `command` pauses: `ago=600, latency=3, apply_ms=1500` and `ago=500, latency=3, apply_ms=2500`. `command`: `n == 2`, `timed == 2`, `p50_s == Decimal("5.0")`, `max_s == Decimal("5.5")`, `verdict == "NOT met"` (3 s + 2.5 s is over the limit although `at` − `trigger_at` alone is 3 s).
- `test_a_fast_lane_pause_without_its_lock_wait_is_counted_but_not_timed` — one `command` pause with `lane="fast"`, `latency=2`, `apply_ms=None`. `command`: `n == 1`, `timed == 0`, `fast_lane_n == 1`, `max_s is None`, `verdict == "not timed"`.
- `test_an_inbox_lane_pause_is_timed_from_its_trigger_time` — one `command` pause with `lane="inbox"`, `latency=4`, `apply_ms=None`. `command`: `n == 1`, `timed == 1`, `fast_lane_n == 0`, `max_s == Decimal("4.0")`, `verdict == "met"`.
- `test_a_bot_block_found_by_a_failed_control_send_is_not_timed` — two `bot_blocked` pauses: one with `lane="send"`, `latency=None`, `apply_ms=None`; one with `lane="fast"`, `latency=1`, `apply_ms=0`, `update_id=77`. `bot_blocked`: `n == 2`, `timed == 1`, `fast_lane_n == 1`, `update_id_n == 1`, `max_s == Decimal("1.0")`, `verdict == "met"` (the row shows `n` and `timed` side by side: one pause is untimed).
- `test_an_automatic_reply_claimed_while_paused_fails` — `command` pause at `ago` 300 (latency 1); a `delegation_auto` action claimed at `ago` 200; no resume. `automatic replies claimed while paused`: `n == 1`, `limit_s == 0`, `verdict == "NOT met"`.
- `test_claims_after_a_resume_or_of_other_origins_do_not_count` — pause at 300, resume at 250, a `delegation_auto` claim at 200; an `agent_proposal` action claimed at 280 (an owner-approved draft may still go out during a pause, P2-T23, OD-31). `n == 0`, `verdict == "met"`.
- `test_a_claim_before_the_pause_does_not_count` — `delegation_auto` claim at 400, pause at 300: `n == 0`, `verdict == "met"`.
- `test_rg21_another_accounts_resume_does_not_end_a_pause` — account A paused at 300; account B resumed at 250; A's `delegation_auto` claim at 200: `n == 1`, `verdict == "NOT met"`.
- `test_without_a_pause_the_claim_check_has_no_data` — a `delegation_auto` claim and no pause: `n == 0`, `verdict == "no data"`.
- `test_cancelled_counts_are_summed` — pauses with `cancelled` 2 and 3: `automatic replies cancelled by a pause` has `n == 5`, `verdict == "informational"`.
- `test_rows_outside_the_window_are_ignored` — a `command` pause at `ago = 2 * 86400` with latency 1: `days = 1` → `command` `n == 0`; `days = 3` → `n == 1`.

### 7.4 `tests/integration/test_nfr05_query.py` (PR 2; appended)

With the §5 edits in place. Both tests start with `account, conversation = await _chat(session)` (the file's existing helper) and call the existing helpers as `_message(session, account, conversation, <seconds ago>)` and `_alert(session, account, "<key>", message_ids=[…], sent_ago=…, kind=…, priority=…)`; the measures come from `(await _measure(session, 1))["<measure name>"]`.
- `test_escalation_alert_latency_counts_urgent_delegation_escalations_only` — `m1 = await _message(session, account, conversation, 400)`, `m2 = await _message(session, account, conversation, 500)`; then `_alert(session, account, "e1", message_ids=[m1], sent_ago=370, kind="delegation_escalation")` (30 s); `_alert(session, account, "e2", message_ids=[m2], sent_ago=410, kind="delegation_escalation")` (90 s); `_alert(session, account, "e3", message_ids=[m1], sent_ago=60, kind="delegation_escalation", priority="high")` (not counted: not urgent); `_alert(session, account, "u1", message_ids=[m1], sent_ago=380)` (a `triage_urgent`, 20 s). `escalation alert delivery`: `n == 2`, `p50_s == Decimal("60.0")`, `p95_s == Decimal("87.0")`, `p95_limit_s == 120`, `verdict == "met"`; `urgent alert delivery`: `n == 1`.
- `test_a_slow_escalation_alert_is_not_met` — `m = await _message(session, account, conversation, 400)`; `_alert(session, account, "e1", message_ids=[m], sent_ago=100, kind="delegation_escalation")` (300 s): `n == 1`, `p95_s == Decimal("300.0")`, `verdict == "NOT met"`.

### 7.5 `tests/integration/test_log_hygiene_ops_av.py` (PR 1)

- `test_the_recorder_and_its_failure_log_nothing_but_codes` — inside `capture_logs()` (`tests/log_hygiene.py`): a poller with the working recorder polls one batch `[make_update(1, text=CANARY)]`; a second poller with the broken recorder (§7.1) polls once. `assert_content_free(output, CANARY, "ops_av_canary_synth_db")` (the database name is inside the driver's error message; only the class name may be logged), and the output contains `poll_observer_failed`.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **Missing data is not uptime.** A minute with no poll (and none in the minute before) is down; nothing recorded at all is `no data`, never 100 %.
- **A partly covered window is not evaluable,** rather than an availability computed over minutes the recorder never saw.
- **A `possible` outage counts to its real end** (`created_at`), not to `at_risk_to`, which only bounds messages that may have expired (`test_a_possible_outage_ends_at_its_creation_not_at_risk_to`).
- **A restore window counts as down:** data after the backup may be lost.
- **Conservative rounding:** a minute touched by a gap is down; per-account duplicates of one gap count once.
- **The recorder's failure is visible, never silent and never fatal:** the poller logs `poll_observer_failed`; the poll and its `last_poll_ok` stand.
- **An untimed pause is not reported as fast.** Button, `tool`, `card`, a bot block found by a failed control send, legacy `fast_lane` and unknown sources say `not timed`; "no pauses" is `no data`, not `met`. A fast-lane row with no `apply_ms` is untimed too: `at` is sampled before the account-lock wait, so `at` − `trigger_at` alone would flatter it.
- **`met` covers the timed pauses only.** A source row with `n` larger than `timed` says so in its own columns; SQ reads `n`, `timed` and the verdict together, and the button is always the owner's stopwatch (LIVE-P3).
- **An hour after deploy #1, `report nfr08 1` says `not evaluable`.** The recorder has no data for most of a one-day window; the check at that point is `polled_min` of about 60 and a verdict other than `no data` (LIVE-P3 V4 reads those columns). A `met` appears once the window is covered (`report nfr08 14` on trial day 14).
- **A slow or cancelled recorder is the poller's concern,** not this query's: a missing minute is tolerated once and counted as down beyond that. The 2 s timeout path has its own F0-S test.
- **Zero claims while paused is shown as a count**, and "no pauses in the window" is `no data`, not a pass.
- **An escalation alert that was never sent is not measured as fast:** only `sent` rows count.

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing for the right reason, and passes; F0-S's `test_poll_observer.py` still passes.
- [ ] `git diff --stat origin/int/p3...HEAD` lists only the PR's files of §3 (seven for PR 1, five for PR 2); the `bootstrap.py` hunk is inside `_wire_surfaces` only; the `test_nfr05_query.py` hunk is the §5 edits plus the appended tests.
- [ ] `ruff check`, `ruff format --check` and `mypy` (strict) are clean on the changed Python files; `lint-imports` is clean (`observability/availability.py` imports no `assistant` package).
- [ ] The recorder has no `try`, no `except`, no `assert`, no logger; it opens its own session and transaction.
- [ ] The upsert text is exactly `UPSERT_MINUTE` (table-qualified `availability_minutes.polls`).
- [ ] Each SQL file is one read-only `WITH` statement, reads only the tables its test names, and no content column. `:days` is the only `:name` token in it, comments included (SQLAlchemy reads any other `:name` as a bind parameter, so a comment such as `note:text` would break the tests).
- [ ] J2's review is posted (mandatory) and QD1's review is posted before merge; PR 1 carries no `nfr07` or `nfr05` change, PR 2 carries no recorder change.
- [ ] The three existing `nfr05.sql` measures and their test values are unchanged.
- [ ] PR 1 is merged before deploy #1 (the TL checks this at CP-R1).
- [ ] Each PR description lists how SQ runs the report (`just prod <ip> report nfr08 14`, `… report nfr07 14`, `… nfr05 14`) and the output columns (nfr08: `window_min`, `polled_min`, `down_min`, `availability_pct`, `budget_min`, `outage_gaps`, `restore_gaps`, `recorded_since`, `verdict`; nfr07: `measure`, `n`, `timed`, `fast_lane_n`, `update_id_n`, `p50_s`, `p95_s`, `max_s`, `limit_s`, `verdict`).

## 10. When to stop and ask

Ask on the OPS-AV issue, starting the comment with "Question for J2:" (plain text, never an @-mention). If nobody answers within 2 h, David raises it with the TL.

- `availability_minutes`, `AvailabilityMinute`, `Poller(poll_observer=…)`, `POLL_OBSERVER_TIMEOUT_SECONDS` or `P3Wiring.poll_observer` does not exist on your base, or is shaped differently from P3-interfaces §4.4 and §15.3.
- `test_a_failing_recorder_never_blocks_a_poll_or_rolls_back_last_poll_ok` fails: that is an F0-S hook problem; do **not** add an `except` to the recorder.
- `F0-R`'s `test_p3_wiring.py` has no reusable context helper, or `_wire_surfaces` has no `P3Wiring` return you can extend.
- PostgreSQL rejects the SQL in a way §6.4's one allowed cast does not fix, or an expected value in §7 differs from what the query returns when you work it out by hand: do **not** change the value; ask.
- M6-PAUSE's merged audit rows use field names other than `source`, `lane`, `trigger_at`, `update_id`, `apply_ms` and `cancelled` (M6-PAUSE §3.1), or M6-PLAN's escalation rows do not carry `refs.message_ids` and priority `urgent` for a decision request. (PR 2 can be written and tested on synthetic rows before those units merge; this check is for its review.)
- A test that lists `deploy/sql` files, or any other exact-set test, fails.
- You have spent more than the unit's size and are not close.

## 11. Coding rules (reviewers block on these)

- No broad `except` (no bare `except`, no `except Exception`); the recorder has no `except` at all. The poller's hook is the only sanctioned broad catch, and it is F0-S's.
- No `assert` in production code.
- Full type hints; `mypy --strict` clean; names exactly as §6.1.
- No `datetime.now()`: the poller passes `at`; SQL uses the database's `now()` only inside the report queries.
- No logging in the recorder; SQL output is ids, counts, codes, timestamps and percentages only.
- SQL: one statement, read-only, `:days` only as a bound variable (psql `-v days`), never spliced.
- GitHub: every PR body and comment through `--body-file <file>` (never an inline body in PowerShell); no bare `@names`; no recurring status comments.
- Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`; never commit `.env`, `var/`, dumps or real data.

## 12. Pull requests

Two PRs, both labelled `team:david`, `eng:d2`; when CI is green, mark ready and add `needs-review:j2` and `needs-qa` (QD1 reviews **before** merge; J2's review is mandatory); comment on the issue.

- **PR 1:** title `OPS-AV (1/2): NFR-08 availability recorder and query`. Expected ≈ 45 code + ≈ 100 SQL (half of it the header comment) + ≈ 330 tests + 1 doc bullet; about 480 lines is accepted for this PR because the SQL's comment and the fixed test data make up most of it. Target: merged by D+1, so it rides deploy #1.
- **PR 2:** title `OPS-AV (2/2): NFR-07 pause latency and escalation-alert latency`. Expected ≈ 140 SQL (the header comment and the three nfr05 edits included) + ≈ 290 tests + 1 doc sentence; about 450 lines is accepted for the same reason as PR 1.
- **Template** (WORKFLOW §4.3), filled honestly for each:
  - *Unit:* `WU: OPS-AV · Spec: docs/engineering/p3/specs/OPS-AV.md · Issue: #<n> · Base: int/p3`; Tier: other; QA lane: before merge.
  - *Safety invariants touched:* 7 fail closed and 11 honest status in the reports (`test_nothing_recorded_is_no_data`, `test_a_window_older_than_the_recorder_is_not_evaluable`, `test_untimed_sources_are_counted_but_not_timed`); 10 kill switch, measured not changed (`test_an_automatic_reply_claimed_while_paused_fails`); 12 tenant scoping (`test_rg21_another_accounts_resume_does_not_end_a_pause`); ingestion never blocked (`test_a_failing_recorder_never_blocks_a_poll_or_rolls_back_last_poll_ok`).
  - *Tests:* the §7 names; the commands you ran and their result, or "not run locally: <reason>; CI is the gate". RG tests: RG-10 by `test_log_hygiene_ops_av.py`; RG-21 extended (PR 2).
  - *Shared lists updated:* PR 1 none; PR 2 the `test_nfr05_query.py` edits of §5.
  - *Data:* no migration; `availability_minutes` is F0-S's content-free system table; logs: `test_log_hygiene_ops_av.py`.
  - *Prompts, flags and settings:* none (the recorder is unflagged by contract).
  - *Docs:* ARCHITECTURE §17 (J2 checks it as your mentor).
  - *Interface changes:* none; out-of-list edits: none.
  - *Open items for M07.md:* anything non-blocking the review raised.
- QD1's depth checks (team-qa §8): run the failing-recorder test and see it pass; temporarily remove the tolerance (`covered` = `polled`) or use `at_risk_to` for outages and see the named tests fail; in `nfr07.sql` drop the `apply_ms` term, or let a fast-lane row without `apply_ms` be timed, and see `test_the_lock_wait_is_added_to_the_latency` and `test_a_fast_lane_pause_without_its_lock_wait_is_counted_but_not_timed` fail; in the recorder, swallow the error and see `test_a_recorder_error_reaches_the_caller` fail; then revert. Mutations are never committed.
