# COMMIT-DUE — Due-time surfacing of open contact commitments (junior unit)

| | |
|---|---|
| **Unit** | COMMIT-DUE — the handler for F0-R's pre-registered periodic `system.surface_due_commitments`: an open `contact_commitment` task whose `due_at` has passed gets one `commitment_due` notification (dedupe `commitment_due:{task_id}:{due_epoch}`); priority `normal` (digest) unless the chat's contact is VIP or the task has a follow-up (`high`); chats set to Ignore are skipped; flag `commitments.due_alerts` |
| **Owner slot** | D2 (David's team, junior) |
| **Mentor** | E1 (Codex; peer review dispatched by the TL) |
| **QA** | QD1, after merge (on `int/p3` before R1) |
| **Size** | S (≈ 0.5 engineer-day, one fix round included) |
| **Depends on** | F0-R (`jobs/tasks_surfaces.py::SurfaceJobHandlers.commitments_due`, the periodic shell, `_wire_surfaces`, the `HELD_IN_QUIET` delivery rule); F0-S (`core/flags.py::COMMITMENTS_DUE_ALERTS`, the `commitment_due` row of `DELIVERY_CLASSES`). M05 code it reuses unchanged: `inbox/notifications.py::enqueue`, `inbox/alert_texts.py::render_task` (the renderer of `commitment_due`), `tasks.snoozed_until`. It does not wait for M7-TASK: the snooze rule below is the same as M7-TASK's `awake` (`snoozed_until IS NULL OR snoozed_until <= now`), written in SQL here. This unit writes no renderer, so GW-ESC's escape-once test rule (GW-ESC §3.3 rule 6) does not apply to it and it may merge before the TL posts "GW-ESC closed" (GW-ESC landed in P2 #133; verification only) |
| **Consumed by** | Nobody calls it: the worker's periodic shell does. Its rows reach the owner through M05 delivery (immediate or the digest) and the digest's "Other alerts" count |
| **Branch → base** | `p3/commit-due` from `origin/int/p3` (after CP1) → PR to `int/p3`. Before CP1, branch from `origin/p3/f0-r` and retarget to `int/p3` after F0-R merges (WORKFLOW §4.2) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §7.1 (`SURFACE_COMMITMENTS_TASK` row), §7.2 (`SurfaceJobHandlers`, `PeriodicHandler`), §10.2 (`commitments.due_alerts`), §11.2 (the `commitment_due` row: class `held_in_quiet`, priority, dedupe key, producer), §11.3, §13 (no new audit event), §15.1, §15.3, §15.4; [decisions](../decisions.md) OD-17, OD-18 (P3-T52); IMPLEMENTATION_PLAN §9 S07.1–S07.2 ("due-time surfacing of open contact commitments"; "snoozed task re-surfaces at `snoozed_until`"); ARCHITECTURE §10.3 `tasks` ("snoozed tasks are hidden … until `snoozed_until`"); AGENTS §6 invariants 7, 11, 12 |

## 1. Goal

J7 step 2: a promise a contact made ("Sardor will send the documents tomorrow") that is still open at its due time must reach the owner. The digest already lists due commitments (M05). This unit adds the missing producer of the M05 `commitment_due` notification kind, which has a renderer but nothing that creates it.

Every 5 minutes the worker calls your handler. For each active account it finds the open `contact_commitment` tasks whose due time passed within the last 24 hours, that are not snoozed, that are in a chat the owner has not set to Ignore, and that have no `commitment_due` notification for that due time yet, and enqueues one `commitment_due` notification per task and due time. The notification is `normal` priority (it waits for the digest) unless the contact is VIP or the owner set up a follow-up for that task, in which case it is `high` (delivered at once, held to the end of quiet hours, never capped; OD-17, OD-18). You write one new module, a few wiring lines and the tests. No model call, no Telegram call, no new table, no new audit event.

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2 (safety), §3 (workspace), §4 (branches, PR template), §5 (tests), §6.2 (Definition of Done).
3. [P3-interfaces](../contracts/P3-interfaces.md) §7.1–§7.2 (the periodic shell and `PeriodicHandler`), §11.2–§11.3 (delivery classes; the `commitment_due` row), §15.3 (`_wire_surfaces`).
4. `src/assistant/inbox/notifications.py`: `enqueue` (its keyword arguments; it returns `None` for a dedupe key the account already used; with `scheduler` it defers `notify.deliver` after commit) and `_refs_json` (empty ref lists are left out of the stored JSON).
5. `src/assistant/inbox/notification_types.py`: `NotificationKind.COMMITMENT_DUE`, `NotificationRefs`, `Priority`.
6. `src/assistant/inbox/tasks.py`: `COMMITMENT_KINDS`, `CONTACT_COMMITMENT`, `due_between` and `expire_due` (the per-account pattern: find the accounts first, then scope every statement by `account_id`).
7. `src/assistant/inbox/alert_texts.py::render_task` (it renders "⏰ Due: <title> (<contact>)" from `refs.task_ids[0]`; you do not change it).
8. `src/assistant/inbox/notifications.py::NotificationDelivery._facts` (delivery reads the contact's VIP/low priority **only** from the notification's `contact_id` column; this is why you leave that column empty, §6.1 rule 5).
9. `src/assistant/jobs/tasks_surfaces.py` (F0-R): `SurfaceJobHandlers`, `PeriodicHandler`, and the shell that calls `handlers.commitments_due(datetime.fromtimestamp(timestamp, UTC))`.
10. `src/assistant/bootstrap.py::_wire_surfaces` (F0-R) and `P3WireContext` (`session_factory`, `clock`, `scheduler`, `settings`).
11. `tests/integration/test_task_expiry.py` (a periodic handler tested over real Postgres with the `env` fixture of `tests/integration/conftest.py`, built on `business_env.environment`) and `tests/integration/execution_env.py::RecordingScheduler`.
12. `src/assistant/conversations/models.py`: the `Conversation.mode` column (`ignore`, `observe`, `assist`) and `inbox/triage_store.py` (a chat in `ignore` gets no triage verdict, so no new alert is produced for it; this unit follows the same rule).
13. This spec, to the end, before you start.

## 3. Allowed files

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/inbox/commitments_due.py` | new | The whole module (§6.1 gives its API) |
| `src/assistant/bootstrap.py` | existing | Inside `_wire_surfaces` only: the function-local imports and the lines of §6.2. Nothing outside that function; the module import block is frozen |
| `tests/integration/test_commitments_due.py` | new | The tests of §7.1 |
| `tests/integration/test_log_hygiene_commit_due.py` | new | The test of §7.2, copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` (under `tests/integration/` because it needs the database) |
| `docs/ARCHITECTURE.md` | existing | §12.2, the "Periodic system tasks" table only: append the one row of §6.3 at the end of the table |

Nothing else.

## 4. Forbidden

- `src/assistant/policy/`, `src/assistant/actions/`, `src/assistant/telegram/` (including `gateway.py` and `ingress/`), `migrations/`, `src/assistant/agent/` (including `prompts/` and `prompt_registry.py`), `src/assistant/privacy/`, consent code (`src/assistant/llm/consent.py`), `src/assistant/config.py`, `src/assistant/core/` (you import `core/flags.py`, `core/clock.py`, `core/jobs.py`; you never edit them), `src/assistant/jobs/` (including `tasks_surfaces.py`: you write the handler, never the shell), `AGENTS.md`.
- `src/assistant/inbox/notifications.py`, `inbox/notification_policy.py`, `inbox/notification_types.py`, `inbox/alert_texts.py`, `inbox/tasks.py`, `inbox/digest.py`, `inbox/triage_store.py` and every other existing `inbox/` file.
- `src/assistant/automation/` (you read `automations` rows in SQL; you never import or edit automation code).
- Every other wire function in `bootstrap.py` (`_wire_autonomy`, `_wire_automation`) and every line outside `_wire_surfaces`.
- `pyproject.toml`, `uv.lock` (no new dependency), `.env*`, `var/`, every `docs/` file except the one §12.2 row.
- **Every file not in §3.**

## 5. Shared lists you must update

None. COMMIT-DUE adds no notification kind (`commitment_due` exists since M05), no delivery class (F0-S's table already maps `commitment_due` to `held_in_quiet`), no flag (F0-S adds `commitments.due_alerts`), no audit event (P3-interfaces §13: "No new audit event: COMMIT-DUE (the `notifications` row is the record)"), no callback kind, no tool, no setting. If an exact-set test fails because of your change, stop and ask (§10).

## 6. Step-by-step

### 6.1 The exact API (`src/assistant/inbox/commitments_due.py`)

```python
"""Due-time surfacing of open contact commitments (P3 COMMIT-DUE; J7 step 2;
OD-17, OD-18; P3-interfaces §11.2).

F0-R's periodic `system.surface_due_commitments` (every 5 minutes) calls
`DueCommitments.run(now)`. An open `contact_commitment` task whose `due_at`
passed within the last `DUE_LOOKBACK`, that is not snoozed and whose chat is
not set to Ignore gets one `commitment_due` notification (a task without a
chat is never surfaced: fail closed). The dedupe key holds the task and its due time,
so a repeated or overlapping run never alerts twice and a moved due date
surfaces again. Priority `normal` (batched for the digest) unless the chat's
contact is VIP or the owner set up a follow-up for the task (`high`; class
`held_in_quiet`: held to the end of quiet hours, never capped). The row holds
ids only; M05's `render_task` reads the title at delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.db.uow import transaction
from assistant.inbox.notification_types import NotificationKind, NotificationRefs, Priority
from assistant.inbox.notifications import enqueue
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: How far back a missed due time is still surfaced (a worker outage, the flag
#: turned on). Older commitments stay in the digest's "Commitments due" list.
DUE_LOOKBACK: Final = timedelta(hours=24)
#: Bounds one run; the rest are surfaced 5 minutes later (dedupe keeps it exact).
MAX_PER_ACCOUNT_RUN: Final = 50
#: A follow-up in one of these states means the owner asked to track the task.
FOLLOW_UP_STATUSES: Final = ("active", "paused", "completed")


def due_dedupe_key(task_id: int, due_at: datetime) -> str: ...


@dataclass(frozen=True, slots=True)
class DueCommitment:
    task_id: int
    conversation_id: int
    contact_id: int
    due_at: datetime
    vip: bool
    followed: bool

    @property
    def priority(self) -> Priority: ...


async def due_commitments(
    session: AsyncSession, *, account_id: int, now: datetime, limit: int = MAX_PER_ACCOUNT_RUN
) -> list[DueCommitment]: ...


@dataclass(frozen=True, slots=True)
class DueCommitments:
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    scheduler: JobScheduler | None
    enabled: bool  # COMMITMENTS_DUE_ALERTS in settings.feature_flags

    async def run(self, now: datetime) -> int: ...  # the PeriodicHandler: rows created
```

These names are binding. `DueCommitments.run` matches F0-R's `PeriodicHandler = Callable[[datetime], Awaitable[int]]` as a bound method.

**Rules (each is tested in §7.1):**

1. **`due_dedupe_key(task_id, due_at)`** returns `f"commitment_due:{task_id}:{int(due_at.timestamp())}"` (epoch seconds, never the datetime as text; P3-interfaces §11.2, §12 #14). A naive `due_at` raises `ValueError("due_at must be timezone-aware")`.
2. **`DueCommitment.priority`** is `Priority.HIGH` when `vip` or `followed`, else `Priority.NORMAL`.
3. **`due_commitments(session, *, account_id, now, limit)`** runs this one statement (as a `text()` constant `_DUE_SQL` in the module) with `since = now - DUE_LOOKBACK`, and maps each row to a `DueCommitment`. It reads no content column (never `title`, `details`, `display_name`, `username`):

   ```sql
   SELECT t.id, t.conversation_id, c.contact_id, t.due_at,
          (ct.priority = 'vip') AS vip,
          EXISTS (
              SELECT 1 FROM app.automations AS a
              WHERE a.account_id = t.account_id
                AND a.task_id = t.id
                AND a.kind = 'follow_up'
                AND a.status IN ('active', 'paused', 'completed')
          ) AS followed
   FROM app.tasks AS t
   JOIN app.conversations AS c ON c.account_id = t.account_id AND c.id = t.conversation_id
   JOIN app.contacts AS ct ON ct.account_id = t.account_id AND ct.id = c.contact_id
   WHERE t.account_id = :account_id
     AND t.kind = 'contact_commitment'
     AND t.status = 'open'
     AND t.due_at IS NOT NULL
     AND t.due_at <= :now
     AND t.due_at > :since
     AND (t.snoozed_until IS NULL OR t.snoozed_until <= :now)
     AND c.mode <> 'ignore'
   ORDER BY t.due_at, t.id
   LIMIT :limit
   ```

   - Every join and the sub-select carry `account_id` (invariant 12, RG-21).
   - `c.mode <> 'ignore'`: the owner told the assistant to leave that chat alone (`conversations.mode` is `ignore`, `observe` or `assist`; triage already gives an Ignore chat no verdict). The digest keeps listing the task; only the alert is withheld (fail closed, invariant 7).
   - The inner joins on `conversations` and `contacts` drop a task whose `conversation_id` is NULL or whose chat is gone: a contact commitment always has a chat, and a task that has none is not surfaced rather than guessed.
   - The `IN (...)` list must equal `FOLLOW_UP_STATUSES`; write it literally in the SQL and keep the constant beside it (a test compares them).
   - `due_at <= now` (due now counts) and `due_at > since` (exactly 24 hours old does not).
4. **`DueCommitments.run(now)`**:
   - `enabled` is `False` ⇒ return `0` at once, before opening any session (the flag is off: nothing is read, nothing is written).
   - `now.tzinfo is None` ⇒ `ValueError("now must be timezone-aware")`.
   - **Accounts:** in one short transaction (`async with self.session_factory() as session, session.begin():`), read the accounts that have a candidate (a cross-account system scan, like `tasks.expire_due`):

     ```sql
     SELECT DISTINCT t.account_id
     FROM app.tasks AS t
     JOIN app.accounts AS a ON a.id = t.account_id
     WHERE a.status = 'active'
       AND t.kind = 'contact_commitment'
       AND t.status = 'open'
       AND t.due_at <= :now
       AND t.due_at > :since
     ORDER BY t.account_id
     ```
   - **Per account**, in its own transaction `async with transaction(self.session_factory) as uow:` (so `enqueue`'s after-commit defer runs): `due_commitments(uow.session, account_id=…, now=now)`, then for each item:

     ```python
     notification_id = await enqueue(
         uow.session,
         account_id=account_id,
         kind=NotificationKind.COMMITMENT_DUE,
         priority=item.priority,
         dedupe_key=due_dedupe_key(item.task_id, item.due_at),
         refs=NotificationRefs(
             conversation_ids=(item.conversation_id,),
             contact_ids=(item.contact_id,),
             task_ids=(item.task_id,),
         ),
         clock=self.clock,
         conversation_id=item.conversation_id,
         scheduler=self.scheduler,
     )
     ```

     Count the calls that returned an id (not `None`).
   - After an account's transaction, when its count is above 0: `logger.info("commitments_due_surfaced", account_id=account_id, created=count)`. Nothing else is logged; never a title, a name or a key.
   - Return the total count over all accounts.
5. **`contact_id` is left out of `enqueue` on purpose** (it defaults to `None`), while `refs.contact_ids` keeps the contact for deletion lookups. Delivery applies the contact's priority ladder only from the `contact_id` column (`NotificationDelivery._facts`): with it set, a low-priority contact would demote a followed-up commitment from `high` to `normal` and send it to the digest, and a VIP's `high` would be raised again. The producer's priority is final (the same choice as M7-FUP's `follow_up_met`, P3-interfaces §11.2 R9). Write this reason as a comment above the call.
6. **What delivery then does (F0-R, not your code):** class `held_in_quiet`; `normal` ⇒ the M05 standard rules (batched for the digest, where it is counted under "Other alerts" and the task is listed under "Commitments due"); `high` ⇒ immediate outside quiet hours, deferred to their end inside them, never capped; `notifications.deliver` still gates it. You do not test delivery; F0-R does.
7. **No row lock and no model or Telegram call.** The dedupe key makes the handler safe to run twice; the periodic's queueing lock keeps it to one instance at a time.
8. **Failure mode: no `except`.** An error in one account's transaction rolls that account back and fails this run; the accounts handled before it are already committed, and the next tick (5 minutes later) repeats the failed account (the dedupe key keeps every row single). Nothing is retried inside the handler and nothing is swallowed.

### 6.2 Wiring (`_wire_surfaces` in `bootstrap.py`; function-local imports only)

Read `_wire_surfaces` on your base first.

```python
    # COMMIT-DUE
    from assistant.core.flags import COMMITMENTS_DUE_ALERTS
    from assistant.inbox.commitments_due import DueCommitments

    commitments_due = DueCommitments(
        session_factory=ctx.session_factory,
        clock=ctx.clock,
        scheduler=ctx.scheduler,
        enabled=COMMITMENTS_DUE_ALERTS in ctx.settings.feature_flags,
    )
```

Then pass `commitments_due=commitments_due.run` to the `SurfaceJobHandlers(...)` that the returned `P3Wiring` carries:
- if `_wire_surfaces` already builds `SurfaceJobHandlers(...)` (another David unit, such as OPS-RATE, merged first), add only your keyword argument to that call;
- otherwise import it function-locally (`from assistant.jobs.tasks_surfaces import SurfaceJobHandlers`) and add `surfaces=SurfaceJobHandlers(commitments_due=commitments_due.run)` to the returned `P3Wiring(...)`, keeping every keyword already there.

The handler is wired whatever the flag; it checks `enabled` itself and returns 0 when off (P3-interfaces §7.2).

### 6.3 ARCHITECTURE §12.2 row (append at the end of the "Periodic system tasks" table)

Append it after the last row that is there. If OPS-RATE's `system.weekly_rating` row or a row of F0-C's part 2 is already the last row, put yours below it; if the two of you conflict in this spot, keep both rows.

```markdown
| `system.surface_due_commitments` | every 5 min | **As built (P3 COMMIT-DUE; flag `commitments.due_alerts`; shell F0-R, `jobs/tasks_surfaces.py`):** `inbox/commitments_due.py::DueCommitments.run` gives each open `contact_commitment` task of an active account whose `due_at` passed within the last 24 h, that is not snoozed and whose chat is not set to Ignore one `commitment_due` notification (dedupe `commitment_due:{task_id}:{due_epoch}`, so a repeated run never alerts twice and a moved due date surfaces again; at most 50 per account per run). Priority `normal` (batched for the digest) unless the chat's contact is VIP or the task has a follow-up in `active`, `paused` or `completed` (`high`); delivery class `held_in_quiet` (OD-17). `contact_id` stays empty so the contact ladder never re-ranks it; `refs` name the chat, contact and task. No model call, no audit row; M05's `render_task` renders it |
```

### 6.4 Steps (each ends with a check)

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd2'` set, from PowerShell on the host (they run inside the container). If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR (WORKFLOW §5).

1. **Branch.** `git fetch origin`; `git switch -c p3/commit-due origin/int/p3`. **Check:** `docker compose -f compose.dev.yml run --rm app uv run python -c "from assistant.jobs.tasks_surfaces import SurfaceJobHandlers; from assistant.core.flags import COMMITMENTS_DUE_ALERTS; print(COMMITMENTS_DUE_ALERTS)"` → prints `commitments.due_alerts`.
2. **Red tests.** Create `tests/integration/test_commitments_due.py` with every test of §7.1 and `src/assistant/inbox/commitments_due.py` with the docstring, imports, constants, `DueCommitment`, `DueCommitments`, and each function or method body `raise NotImplementedError`. **Check:** `just test tests/integration/test_commitments_due.py -q` → every test fails with `NotImplementedError` (none with `ImportError` or `NameError`); `test_wire_surfaces_wires_the_commitments_due_handler` fails because the handler is `None`.
3. **`due_dedupe_key` and `priority`.** **Check:** `just test tests/integration/test_commitments_due.py -q -k "dedupe_key"` → the two key tests pass.
4. **`due_commitments`.** **Check:** `just test tests/integration/test_commitments_due.py -q -k "statuses_match"` → passes.
5. **`DueCommitments.run`.** **Check:** `just test tests/integration/test_commitments_due.py -q` → everything passes except the wiring test.
6. **Wiring (§6.2).** **Check:** `just test tests/integration/test_commitments_due.py -q` → all pass.
7. **Hygiene test** (§7.2): copy `tests/unit/test_log_hygiene_f0e.py` to `tests/integration/test_log_hygiene_commit_due.py`, keep its header comment, replace its tests with §7.2's. **Check:** `just test tests/integration/test_log_hygiene_commit_due.py -q` → passes.
8. **ARCHITECTURE row** (§6.3). **Check:** `git diff origin/int/p3 -- docs/ARCHITECTURE.md` shows exactly one added table row and no other change.
9. **Static checks.** `docker compose -f compose.dev.yml run --rm app uv run ruff check src/assistant/inbox/commitments_due.py src/assistant/bootstrap.py tests/integration/test_commitments_due.py tests/integration/test_log_hygiene_commit_due.py`; the same files with `uv run ruff format --check`; `… uv run mypy src/assistant/inbox/commitments_due.py src/assistant/bootstrap.py`; `… uv run lint-imports`. **Check:** all clean.
10. **Neighbour tests** (unchanged behaviour): `just test tests/integration/test_task_expiry.py tests/integration/test_notifications.py -q`. **Check:** all pass.
11. **Diff.** `git diff --stat origin/int/p3...HEAD` → exactly the five files of §3.
12. **Commit and PR** (§12). Commit at step 2 (red tests) and at step 9 (green), each with `git commit -F <message file>`. **Check:** `git log --oneline origin/int/p3..HEAD` lists exactly those two commits (plus any fix commit after review).

## 7. Named tests (red first)

Fixtures: the `env` fixture of `tests/integration/business_env.py` (a clean database per test; `env.factory`, `env.clock`). Seed inside `async with env.factory() as session, session.begin():` with `tests/integration/p2_factories.py` (`seed_account`, `seed_conversation`, `seed_task`, `seed_automation`). A VIP or low contact: `UPDATE app.contacts SET priority = 'vip' WHERE id = :contact_id` after `seed_conversation` (its `contact_id` attribute). Every task title is the synthetic `"Synthetic commitment"`.

Times: `NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)` (epoch `1793610000`); call `env.clock.set(NOW)` first in every test. `T_DUE = NOW - timedelta(minutes=10)` = `2026-11-02T08:50:00Z` (epoch `1793609400`). The handler under test is `DueCommitments(session_factory=env.factory, clock=env.clock, scheduler=None, enabled=True)` unless a test says otherwise; `handler.run(NOW)`. A "due commitment" is `seed_task(session, account_id=a, conversation_id=c, kind="contact_commitment", status="open", due_at=T_DUE)`.

### 7.1 `tests/integration/test_commitments_due.py`

**Keys and priority**
- `test_due_dedupe_key_uses_epoch_seconds` — `due_dedupe_key(42, T_DUE) == "commitment_due:42:1793609400"`; `due_dedupe_key(42, datetime(2026, 11, 2, 8, 50, 0, 900000, tzinfo=UTC)) == "commitment_due:42:1793609400"`.
- `test_due_dedupe_key_rejects_a_naive_time` — `due_dedupe_key(42, datetime(2026, 11, 2, 8, 50))` → `ValueError`.
- `test_follow_up_statuses_match_the_sql` — the quoted values inside the `a.status IN (...)` of `_DUE_SQL` (read `str(_DUE_SQL)`, take the text between `a.status IN (` and the next `)`, split on `,`, strip spaces and quotes) equal `FOLLOW_UP_STATUSES`.

**Surfacing (J7 step 2)**
- `test_open_contact_commitment_surfaces_at_due_time` — one account, one chat, one due commitment. `run(NOW) == 1`. Exactly one `notifications` row: `kind == "commitment_due"`, `priority == "normal"`, `alert_class == "standard"`, `status == "queued"`, `dedupe_key == f"commitment_due:{task.id}:1793609400"`, `refs == {"conversation_ids": [c.id], "contact_ids": [c.contact_id], "task_ids": [task.id]}`, `conversation_id == c.id`, `contact_id is None`, `account_id == a.id`, `created_at == NOW`.
- `test_a_commitment_due_now_is_surfaced_and_one_due_later_is_not` — two tasks in one chat, `due_at = NOW` and `due_at = NOW + timedelta(minutes=1)`. `run(NOW) == 1`; the one row names the first task.
- `test_a_commitment_overdue_beyond_the_lookback_is_not_surfaced` — tasks with `due_at = NOW - timedelta(hours=24)` and `due_at = NOW - timedelta(hours=24) + timedelta(seconds=1)`. `run(NOW) == 1`; the row names the second task.
- `test_only_open_contact_commitments_are_surfaced` — all due at `T_DUE` in one chat: `kind="owner_commitment"`; `kind="open_question"`; `kind="reminder"`; `kind="contact_commitment", status="done"`; `kind="contact_commitment", status="cancelled"`; `kind="contact_commitment", status="expired"`. `run(NOW) == 0`; no `notifications` row.
- `test_a_commitment_without_a_due_time_is_never_surfaced` — `kind="contact_commitment", status="open", due_at=None`. `run(NOW) == 0`.
- `test_a_snoozed_commitment_is_not_surfaced_until_its_snooze_ends` — a due commitment with `snoozed_until = NOW + timedelta(hours=1)`: `run(NOW) == 0`; then `env.clock.set(NOW + timedelta(hours=1))` and `run(NOW + timedelta(hours=1)) == 1` (its due time, 70 minutes old, is still inside the lookback).
- `test_a_snooze_that_ends_now_is_over` — a due commitment with `snoozed_until = NOW`: `run(NOW) == 1` (`snoozed_until <= now` counts as ended).
- `test_a_chat_set_to_ignore_is_not_surfaced` — parametrized `(mode, expected)`: `("ignore", 0)`, `("observe", 1)`, `("assist", 1)`; the chat is seeded with `seed_conversation(session, account_id=a.id, mode=mode)` and holds one due commitment. `run(NOW) == expected`; with `0` there is no `notifications` row.
- `test_a_commitment_without_a_chat_is_not_surfaced` — `seed_task(session, account_id=a.id, conversation_id=None, kind="contact_commitment", status="open", due_at=T_DUE)`: `run(NOW) == 0`; no row.

**Priority**
- `test_a_vip_contact_raises_the_alert_to_high` — the chat's contact `priority = 'vip'`. One row with `priority == "high"` and `contact_id is None`.
- `test_a_followed_up_commitment_raises_the_alert_to_high` — parametrized `(status, expected)`: `("active", "high")`, `("paused", "high")`, `("completed", "high")`, `("cancelled", "normal")`, `("expired", "normal")`; `seed_automation(session, account_id=a.id, kind="follow_up", task_id=task.id, status=status)`. The row's `priority == expected`.
- `test_a_reminder_automation_on_the_task_does_not_count_as_a_follow_up` — `seed_automation(..., kind="reminder", task_id=task.id, status="active")`. `priority == "normal"`.
- `test_another_accounts_follow_up_never_raises_the_alert` — a follow-up automation of account B whose `task_id` is account A's task (insert it with SQL `INSERT INTO app.automations (account_id, kind, task_id, spec, refs, status, timezone, tz_policy) VALUES (:b, 'follow_up', :t, '{"schema_version": 1}', '{}', 'active', 'UTC', 'fixed')`). Account A's row has `priority == "normal"`.
- `test_a_low_priority_contact_never_demotes_a_followed_up_commitment` — the chat's contact `priority = 'low'`, an `active` follow-up on the task. The row has `priority == "high"` and `contact_id is None`.

**Idempotence and limits**
- `test_a_second_run_never_alerts_twice` — `run(NOW) == 1`, then `run(NOW) == 0`, then `run(NOW + timedelta(minutes=5)) == 0`; one row in all.
- `test_a_moved_due_date_surfaces_again` — after `run(NOW) == 1`, `UPDATE app.tasks SET due_at = :d WHERE id = :t` with `d = NOW - timedelta(minutes=5)` (epoch `1793609700`); `run(NOW) == 1`; two rows, keys `…:1793609400` and `…:1793609700`.
- `test_at_most_fifty_per_account_per_run_and_the_rest_next_run` — 51 due commitments in one chat with `due_at = T_DUE - timedelta(seconds=i)` for `i` in `0..50`. `run(NOW) == 50`; `run(NOW) == 1`; 51 rows; the task left for the second run is the one due last (`i = 0`, `due_at = T_DUE`).
- `test_each_new_alert_is_deferred_for_delivery_after_commit` — `scheduler = RecordingScheduler()` (`tests/integration/execution_env.py`). `run(NOW) == 1`; `scheduler.deferred` has exactly one entry with `task == "notify.deliver"` and `args == {"notification_id": <the row's id>}`. A second `run(NOW)` adds no entry.

**Scope and flag**
- `test_rg21_due_commitments_are_surfaced_only_within_their_own_account` — accounts A and B, each with one chat and one due commitment. `run(NOW) == 2`. A's row has `account_id == A.id` and `refs` naming only A's chat, contact and task; B's row likewise; no row of one account names an id of the other.
- `test_inactive_accounts_are_skipped` — parametrized over account `status` `"onboarding"`, `"blocked"`, `"deleted"` (`seed_account(session, status=…)`), each with a due commitment. `run(NOW) == 0`.
- `test_the_handler_does_nothing_with_its_flag_off` — `enabled=False`, one due commitment. `run(NOW) == 0`; no row. Also pass a `session_factory` that raises `AssertionError` when called (a small stub class in the test) to prove no session is opened.
- `test_a_naive_time_is_refused` — `run(datetime(2026, 11, 2, 9, 0))` → `ValueError`; no row.

**Wiring**
- `test_wire_surfaces_wires_the_commitments_due_handler` — call `bootstrap._wire_surfaces(ctx)` with the context helper F0-R's `tests/integration/test_p3_wiring.py` uses (import it; never copy it), once with `commitments.due_alerts` in the settings' feature flags and once without. Both times `wiring.surfaces.commitments_due` is a bound `DueCommitments.run`; its `__self__.enabled` is `True` and `False` respectively.

### 7.2 `tests/integration/test_log_hygiene_commit_due.py`

- `test_due_commitment_surfacing_logs_ids_and_counts_only` — the task title and the contact's `display_name` are `CANARY` (`tests/log_hygiene.py`). Inside `capture_logs()`, `run(NOW)`. `assert_content_free(output, CANARY)`; the captured output contains the event `commitments_due_surfaced`; the notification's `refs` values are all lists of integers.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **A snoozed commitment is not due for the owner.** It is surfaced only once `snoozed_until <= now` (ARCHITECTURE §10.3); snoozed is never treated as done, and done is never surfaced.
- **Closed means closed.** `done`, `cancelled` and `expired` tasks are never surfaced; only `contact_commitment` (the contact's promise), never the owner's own commitments or questions.
- **No double alert.** The dedupe key makes a repeated run, a restart or two overlapping ticks produce one row (`test_a_second_run_never_alerts_twice`).
- **A changed due date is a new due time.** It surfaces again (`test_a_moved_due_date_surfaces_again`), never silently dropped.
- **An old miss is not resurrected as fresh.** Beyond 24 hours the alert is not created; the digest still lists the commitment as overdue (M05), so nothing is hidden.
- **The owner's explicit interest wins.** A follow-up keeps the alert `high` even for a low-priority contact (`contact_id` empty, §6.1 rule 5).
- **Flag off means nothing happens**, and no row says otherwise.
- **Known delivery-time limit (not yours to fix):** M05's `render_task` does not re-check the task at delivery, so a `high` alert held through quiet hours for a commitment closed in the meantime still reads "Due: …". Put this in the PR's open items for M07.md; do not edit `inbox/alert_texts.py`.
- **The first run after the flag goes on.** It surfaces every open contact commitment that fell due in the last 24 hours (never older), at most 50 per account per run, the rest five minutes later. `normal` ones wait for the digest; VIP and followed-up ones (`high`) are delivered at once, outside quiet hours, and are never capped (class `held_in_quiet`). Say so in the PR so the TL and SQ expect it at enable 1 (P3-interfaces §10.3, enable 1); do not add a cap or a "since the flag was turned on" rule here.
- **An error never produces a partial lie.** The handler catches nothing (§6.1 rule 8): a failed account is retried five minutes later and the dedupe key keeps its rows single.
- **An Ignore chat stays quiet.** A commitment in a chat the owner set to Ignore is not alerted (`test_a_chat_set_to_ignore_is_not_surfaced`); the digest still lists the task, so nothing is hidden, only not pushed.

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing for the right reason, and passes.
- [ ] `git diff --stat origin/int/p3...HEAD` lists only the five files of §3; the `bootstrap.py` hunk is inside `_wire_surfaces` only; the ARCHITECTURE hunk is one table row.
- [ ] `ruff check`, `ruff format --check` and `mypy` (strict) are clean on the changed files; `lint-imports` is clean (the module imports no `telegram`, `jobs`, `agent`, `llm` or `automation` package).
- [ ] No `assert` in production code; no broad `except` (the module catches nothing).
- [ ] The SQL reads no content column; every statement in the per-account path carries `account_id`.
- [ ] Logs carry `account_id` and counts only.
- [ ] No new audit event, notification kind, flag or setting.
- [ ] The PR description names the class, its constructor arguments and the wiring line, and lists the §8 delivery-time limit and the first-run note under open items.

## 10. When to stop and ask

Ask on the COMMIT-DUE issue, starting the comment with "Question for E1:" (plain text, never an @-mention). If nobody answers within 2 h, David raises it with the TL.

- `SurfaceJobHandlers`, its `commitments_due` field, `COMMITMENTS_DUE_ALERTS` or `NotificationKind.COMMITMENT_DUE` does not exist on your base, or has another name or type.
- `_wire_surfaces` has no `P3Wiring` return you can extend as §6.2 says, or F0-R's `test_p3_wiring.py` has no reusable helper to build a `P3WireContext`.
- `enqueue` has a different signature than §6.1 uses, or `notifications.contact_id` is required.
- A column the SQL of §6.1 names (`tasks.snoozed_until`, `conversations.mode`, `contacts.priority`, `automations.task_id`) does not exist on your base or has other values than §6.1 and §7 assume.
- A named test cannot pass without changing a file outside §3 (for example `inbox/alert_texts.py` or `jobs/tasks_surfaces.py`).
- An exact-set test you did not expect fails.
- An expected value in §7 looks wrong when you work it out by hand: do **not** change it; ask.
- You have spent more than half a day and are not close.

## 11. Coding rules (reviewers block on these)

- No broad `except` (no bare `except`, no `except Exception`); this module needs no `except` at all.
- No `assert` in production code, neither for control flow nor for type narrowing: use `if …: raise ValueError(…)`.
- Full type hints; `mypy --strict` clean; names exactly as §6.1.
- No `datetime.now()`: `now` comes in as the argument; `enqueue` uses the injected `clock`.
- Content-free logs: the one `commitments_due_surfaced` line with `account_id` and `created`.
- No Telegram call, no model call, no aiogram import; the module is domain code (`inbox`).
- GitHub: every PR body and comment through `--body-file <file>` (never an inline body in PowerShell); no bare `@names`; no recurring status comments.
- Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`; never commit `.env`, `var/` or real data.

## 12. Pull request

- **Title:** `COMMIT-DUE: surface open contact commitments at their due time`.
- **Size:** about 400 changed lines or fewer (expected ≈ 110 code + ≈ 280 tests + 1 doc row).
- **Labels:** `team:david`, `eng:d2` on the draft; when CI is green, mark it ready and add `needs-review:e1` and `needs-qa`; comment on the issue that it is ready.
- **Template** (WORKFLOW §4.3), filled honestly:
  - *Unit:* `WU: COMMIT-DUE · Spec: docs/engineering/p3/specs/COMMIT-DUE.md · Issue: #<n> · Base: int/p3`; Tier: other; QA lane: after merge.
  - *Safety invariants touched:* 11 honest status (snoozed and closed tasks are not surfaced: `test_a_snoozed_commitment_is_not_surfaced_until_its_snooze_ends`, `test_only_open_contact_commitments_are_surfaced`); 7 fail closed (`test_a_chat_set_to_ignore_is_not_surfaced`, `test_a_commitment_without_a_chat_is_not_surfaced`, `test_inactive_accounts_are_skipped`); 12 tenant scoping (`test_rg21_due_commitments_are_surfaced_only_within_their_own_account`, `test_another_accounts_follow_up_never_raises_the_alert`).
  - *Tests:* the §7 names; the commands you ran and their result, or "not run locally: <reason>; CI is the gate". RG tests: RG-21 extended (the `test_rg21_…` above); RG-10 by `test_log_hygiene_commit_due.py`.
  - *Shared lists updated:* none.
  - *Data:* no migration, no content column; logs: `test_log_hygiene_commit_due.py`.
  - *Prompts, flags and settings:* none new (reads `commitments.due_alerts`, added by F0-S, default off).
  - *Docs:* ARCHITECTURE §12.2, one periodic row (E1 checks it as your mentor).
  - *Interface changes:* none; out-of-list edits: none.
  - *Open items for M07.md:* the §8 delivery-time limit of `render_task`, plus anything non-blocking the review raised.
- After merge: QD1 checks it on `int/p3`; a finding becomes one fix PR from you.
