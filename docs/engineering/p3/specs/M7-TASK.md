# M7-TASK — Owner tasks: complete, snooze, cancel, owner-only reopen

| | |
|---|---|
| **Unit** | M7-TASK — owner task functions in `inbox/tasks.py`, the `update_task` tool, the `TaskControls` port, snooze filters, expiry changes |
| **Team / slot** | Edvard / E1 |
| **Size** | S+ (≈ 0.75 engineer-day, one fix round included) |
| **Depends on** | F0-* (F0-S: `tasks.refs`, `tool_call_id`, `provenance` and `ux_tasks_run_id_tool_call_id`; F0-R: `core/p3_ports.py`, `agent/tools/tasks.py` with `TOOLS = ()`, the undo registry, `_wire_automation`). Merge it early: M7-REM, M7-FUP, M7-MACT and M7-AUTO call its functions |
| **Branch → base** | `p3/m7-task` → `int/p3` |
| **Peer reviewer** | a Josh engineer (Claude; the TL picks J1, J2 or J3) |
| **QA** | QE1, after merge (on `int/p3` before R1) |
| **Tier** | 2 (product flow) |
| **Normative inputs** | [P3-schema §3.4, §7.2](../contracts/P3-schema.md); [P3-interfaces §4.5, §9.3, §9.4, §12.7, §13, §15](../contracts/P3-interfaces.md); decisions P3-T49, P3-T57, P3-T62; TL rulings R68, R71, R83 (the `source` keyword); IMPLEMENTATION_PLAN §9 S07.1; PRD FR-FUP-03; ARCHITECTURE §8.1, §8.2, §10.3 `tasks` ("snoozed tasks stay open and are hidden from attention lists and reminders until `snoozed_until`") |

## 1. Goal

The owner can complete, snooze, cancel and reopen tasks: from the owner run (`update_task`, echoed with Undo where the contract allows it) and from cards (`TaskControls`, used by M7-REM's reminder card and M7-FUP's Reopen button). A snoozed task disappears from the attention list and the digest until its time and, for an owner reminder, comes back as a reminder at that time. Triage never reopens a task; only the owner does. Expiry never silently closes a task that a follow-up watches or that the owner snoozed or reopened.

## 2. Scope

**In:**
1. Owner functions in `inbox/tasks.py`: `create_owner_task`, `complete`, `cancel`, `snooze`, `reopen`; content-free audit rows; idempotency on `(run_id, tool_call_id)`.
2. `OwnerTaskControls`, the `TaskControls` implementation (P3-interfaces §4.5), wired into `P3Ports.task_controls`.
3. Undo handlers `undo_task_closed` (reason `owner_done` only) and `undo_task_snoozed` (P3-interfaces §12.7).
4. Snooze filters: `TaskView.snoozed_until`, `awake`, `hide_snoozed` in `inbox/tasks.py`; one separate filter function or call each in `memory/catch_up.py`, `agent/tools/summaries.py` and `inbox/digest.py` (P3-interfaces §1.2).
5. Expiry (`system.expire_tasks`): skips snoozed tasks, tasks watched by an active follow-up, and tasks the owner reopened.
6. The `update_task` tool (`agent/task_schemas.py::UpdateTaskArgs`, `agent/tools/tasks.py`).
7. The "triage never reopens" docstrings (the document conflict on who reopens a task: the IMPLEMENTATION_PLAN wins, only the owner reopens).

**Out:** `list_tasks` (RD-M7); creating reminders and to-dos (M7-REM, through `create_owner_task`); follow-ups and their Reopen card (M7-FUP calls `TaskControls.reopen`); the monitor `create_task` action (M7-MACT); `commitment_due` alerts (COMMIT-DUE, which reuses `awake`); changing a task's title or due date; any change to triage's own task writes in `inbox/triage_store.py` (M6-ESC's file).

## 3. Design

### 3.1 Owner functions (`inbox/tasks.py`)

Constants (module level): `OWNER_DONE = "owner_done"` and `OWNER_CANCELLED = "owner_cancelled"` (both already in `_CLOSED_STATUS`), `EVENT_TASK_SNOOZED = "task_snoozed"`, `EVENT_TASK_REOPENED = "task_reopened"`, `OWNER_TASK_KINDS = ("reminder", "follow_up")`, `MAX_OWNER_TITLE = 400`, `MAX_SNOOZE = timedelta(days=366)`.

`_audit` gains a keyword `actor: str = "agent"` and returns the added `AuditLogEntry` after a flush (its id is the Undo card's `ref_id`). `close(...)` gains `actor: str = "agent"`; its other behaviour and every existing caller are unchanged.

```python
@dataclass(frozen=True, slots=True)
class OwnerChange:
    result: PortResult  # core/p3_ports.py
    task_kind: str | None = None
    audit_id: int | None = None  # the change's audit row; None when nothing changed
    reminders_cancelled: tuple[int, ...] = ()  # automation ids (cancel only)
    rearmed_automation_id: int | None = None  # snooze of an owner reminder
    reason: str | None = None  # INVALID only: "not_open", "time_passed", "too_far", "open_already"


async def create_owner_task(
    session: AsyncSession,
    *,
    account_id: int,
    kind: str,
    title: str,
    due_at: datetime | None,
    refs: Mapping[str, Sequence[int]],
    provenance: Mapping[str, Any],
    run_id: int | None,
    tool_call_id: str | None,
    actor: str,
    now: datetime,
    conversation_id: int | None = None,
    source: Literal["owner", "monitor"] = "owner",
    automation_id: int | None = None,
    source_message_ids: Sequence[int] = (),
) -> tuple[int, bool]: ...  # (task id, created)


async def complete(
    session,
    *,
    account_id: int,
    task_id: int,
    actor: str,
    now: datetime,
    run_id: int | None = None,
    call_id: str | None = None,
) -> OwnerChange: ...
async def cancel(
    session,
    *,
    account_id: int,
    task_id: int,
    actor: str,
    now: datetime,
    run_id: int | None = None,
    call_id: str | None = None,
) -> OwnerChange: ...
async def snooze(
    session,
    *,
    account_id: int,
    task_id: int,
    until: datetime,
    actor: str,
    now: datetime,
    run_id: int | None = None,
    call_id: str | None = None,
) -> OwnerChange: ...
async def reopen(
    session,
    *,
    account_id: int,
    task_id: int,
    actor: str,
    source: Literal["card", "tool"],
    now: datetime,
    run_id: int | None = None,
    call_id: str | None = None,
) -> OwnerChange: ...
```

Every statement carries `account_id = :account_id` (RG-21). Every function runs in the caller's transaction and never commits, calls Telegram or calls a model.

**`create_owner_task`** (for M7-REM, M7-FUP and any later owner-task writer; P3-interfaces §15.1 lets them call it):
- `kind` must be in `OWNER_TASK_KINDS`; `title` is stripped and must be 1–`MAX_OWNER_TITLE` characters (`ValueError` otherwise; callers validate first).
- `refs` holds only `conversation_ids` and `contact_ids`. Every id is checked against the account first (`NotInAccountError("not found")`, the rule of `automation/repository.py::_check_ids`). Stored as `{"conversation_ids": sorted, "contact_ids": sorted}`.
- `conversation_id` defaults to `NULL`. **Owner reminders and to-dos always keep `conversation_id` NULL**; the chat goes in `refs` only. Triage shows the model only the open tasks of the chat it triages, and `resolved_task_ids` is filtered to those ids (`agent/triage.py`), so a task with no `conversation_id` can never be retitled or closed by contact-triggered triage. A follow-up task may pass one (M7-FUP's choice).
- **The `source` keyword (R83).** `source` is `"owner"` (the default: every M7-REM, M7-FUP and tool caller) or `"monitor"` (M7-MACT's to-do for a matched monitor). `source == "monitor"` requires `automation_id` (the monitor's automation id) and `automation_id` with `source == "owner"` is a `ValueError`; callers validate first. The keyword exists so M7-MACT writes no second, hand-made `task_created` row (P3-interfaces §13: a monitor's task event carries `source: "monitor"` and `automation_id`). `source_message_ids` (default none) is stored deduplicated and sorted: a monitor's to-do names the messages that matched, so the existing privacy sweep (`inbox/tasks.py`, tasks whose source messages were deleted) still removes it with them. A monitor's call passes `run_id = None` and `tool_call_id = None` (no tool is running at match time), so the idempotency path below does not apply and a new row is always inserted. The row is otherwise identical for both sources.
- Insert with `created_by = 'owner'`, `status = 'open'`, `source_message_ids = <the argument, sorted>`, `first_source_message_id = NULL`, `run_id`, `tool_call_id`, `refs`, `provenance`, `created_at = updated_at = now`, using `ON CONFLICT (run_id, tool_call_id) WHERE tool_call_id IS NOT NULL DO NOTHING RETURNING id` (the partial unique `ux_tasks_run_id_tool_call_id`, P3-schema §3.4). If no row comes back, select this account's row by `(run_id, tool_call_id)` and return `(id, False)`; none found (another account's) ⇒ `NotInAccountError`.
- A new row writes `task_created` (existing event) with details `{"kind", "conversation_id", "run_id", "source": <source>}` plus `"automation_id"` when `source == "monitor"`, and the given `actor` (ids and enums only).

**`complete`** (`open → done`, reason `owner_done`):
1. Idempotent repeat: if `run_id` and `call_id` are given and this account has an audit row with `subject_type = 'task'`, `subject_id = task_id`, `event IN ('task_closed', 'task_snoozed', 'task_reopened')`, `details.run_id = run_id` and `details.call_id = call_id` (the lookup `agent/tools/autonomy.py` uses), return `OwnerChange(ALREADY, kind)` without writing.
2. Conditional update: `UPDATE tasks SET status = 'done', closed_reason = 'owner_done', closed_at = :now, updated_at = :now WHERE account_id = :a AND id = :t AND status = 'open' RETURNING kind`. One row ⇒ audit `task_closed` `{"kind", "reason": "owner_done", "run_id", "call_id"}` (ids only; `run_id` and `call_id` null for a card) ⇒ `DONE`.
3. No row: not this account's task ⇒ `NOT_FOUND`; already `done` ⇒ `ALREADY`; `cancelled` or `expired` ⇒ `INVALID` (`reason = "not_open"`).
4. `snoozed_until` stays as it is (Undo reopens the task as it was). The task's reminder automation is **not** touched: completing has an Undo, and M7-REM's fire handler skips a task that is not open, so a not-yet-fired reminder of a completed task never notifies, and notifies again only if Undo reopens the task before its time.

**`cancel`** (`open → cancelled`, reason `owner_cancelled`). Same steps as `complete`, plus, in the same transaction and **before** the task update (lock order below): lock the task's one-off reminder automation with `SELECT id FROM automations WHERE account_id = :a AND task_id = :t AND kind = 'reminder' AND rrule IS NULL AND status IN ('active', 'paused') ORDER BY id FOR UPDATE`; after the task update set those rows `status = 'cancelled', ended_at = :now, updated_at = :now`. The audit row's details gain `"automations": [ids]`. Cancel has no Undo (P3-interfaces §12.7), so ending the reminder is safe; a later reopen does not re-arm it (the echo says so, §3.6). Follow-up automations are never touched here: M7-FUP reads the task's state at match and deadline time.

**`snooze`** (open tasks only):
1. Idempotent repeat as in `complete`.
2. `until` must be `> now` (`INVALID`, `reason = "time_passed"`) and `<= now + MAX_SNOOZE` (`INVALID`, `reason = "too_far"`). Microseconds are dropped (whole seconds, as the scheduler's instants).
3. Lock order: the one-off reminder automation first (as in `cancel`, statuses `active`, `paused`, `completed`), then the task row (`SELECT ... FOR UPDATE`, reading the old `snoozed_until`). Not this account's ⇒ `NOT_FOUND`; not open ⇒ `INVALID` (`not_open`); the same `until` already stored ⇒ `ALREADY` (nothing written). Otherwise `UPDATE tasks SET snoozed_until = :until, updated_at = :now`.
4. **Re-arm a reminder.** When the task's `kind` is `reminder` and it has a one-off reminder automation (`rrule IS NULL`), in the same transaction: `UPDATE automations SET run_at = :until, next_run_at = :until, status = CASE WHEN status = 'paused' THEN 'paused' ELSE 'active' END, ended_at = CASE WHEN status = 'paused' THEN ended_at ELSE NULL END, updated_at = :now WHERE id = :auto AND account_id = :a`. The ledger key is `(automation_id, occurrence_at)` and `until > now` is later than every recorded occurrence, so the new instant is a new occurrence and fires exactly once. An old occurrence that is already claimed and still queued finds `occurrence_at != run_at` (and the task snoozed) at fire time and notifies nothing (M7-REM §3.4 step 1); a `reminder_due` already queued for it is suppressed by the renderer. A to-do or a triage task has no reminder: its snooze is passive (it reappears in lists at `until`, §3.4).
5. Audit `task_snoozed` with details `{"until": epoch, "previous_until": epoch|null, "automation_id": id|null, "previous_run_at": epoch|null, "previous_status": str|null, "run_id", "call_id"}` (ids, epochs and a status code only; the extra keys exist for Undo — contract gap reported) ⇒ `DONE`.

**`reopen`** (owner only; `done`, `cancelled` or `expired` → `open`):
1. Idempotent repeat as in `complete`.
2. `UPDATE tasks SET status = 'open', closed_reason = NULL, closed_at = NULL, snoozed_until = NULL, updated_at = :now WHERE account_id = :a AND id = :t AND status IN ('done', 'cancelled', 'expired') RETURNING kind`. Already open ⇒ `INVALID` (`reason = "open_already"`; `TaskControls.reopen` maps it to `ALREADY`, §3.2). Not this account's ⇒ `NOT_FOUND`.
3. Audit `task_reopened` `{"source": "card"|"tool", "run_id", "call_id"}` ⇒ `DONE`.
4. Reopen never re-arms a cancelled reminder and never touches a follow-up automation. M7-FUP's Reopen press decides what happens to its follow-up after `TaskControls.reopen` returns.
5. A triage task the owner reopened is left alone by expiry (§3.5). While it is open, triage may still update its title and due date (the existing upsert rule) and may close it again through `resolved_task_ids`; that is a new closure the owner sees, never a reopen by triage.

**Lock order and transactions.** The cross-unit automation lock rule frozen in P3-interfaces §8.1 applies (R71): the fire path locks occurrence → automation → task; an owner write locks automation → task, with `NOWAIT` or `SKIP LOCKED` on ledger rows, and never waits on an occurrence lock. For an owner reminder the order is therefore **the `automations` row before its `tasks` row**; M7-REM's fire handler and Undo, M7-AUTO's reschedule and delete and M7-FUP use the same order (M7-REM §3.4). `complete` and `reopen` take only the task row (one conditional update), so they cannot form a cycle with the fire path. No function here locks an `automation_occurrences` row, and none waits on one. No other row lock is taken. Owner tasks have no conversation, so the ARCHITECTURE §9.4 conversation lock does not apply. Agent tasks change only through single-row conditional updates, so a concurrent triage close and an owner close cannot both win.

### 3.2 `TaskControls` (`inbox/tasks.py::OwnerTaskControls`)

Implements P3-interfaces §4.5 exactly. Stateless; actor `owner`; reopen source `card`; runs in the caller's transaction.

| Method | Calls | `PortResult` |
|---|---|---|
| `done(session, *, account_id, task_id, now)` | `complete(...)` | `DONE`; `ALREADY` (already done); `NOT_FOUND`; `INVALID` (cancelled or expired) |
| `snooze(session, *, account_id, task_id, until, now)` | `snooze(...)` | `DONE`; `ALREADY` (same time); `NOT_FOUND`; `INVALID` (closed, past or too far) |
| `reopen(session, *, account_id, task_id, now)` | `reopen(..., source="card")` | `DONE`; `ALREADY` (already open); `NOT_FOUND` |

A press handler that receives `NOT_FOUND` says the task no longer exists. Nothing reports success through an unwired port (P3-interfaces §4.5).

### 3.3 Undo handlers (P3-interfaces §12.7)

Both have the `UndoHandler` shape `(session, account_id, entry: AuditLogEntry, clock) -> bool`, are registered in `_wire_automation` through `ControlRoutes.undo_handlers`, and return `False` (⇒ `SUPERSEDED`) whenever the change is no longer the current state. The framework writes `change_undone`.

- **`undo_task_closed`**: only when `entry.details.reason == "owner_done"` (a triage or expiry close, or a cancel, is never undone; no Undo card exists for them anyway). Reverts with `UPDATE tasks SET status = 'open', closed_reason = NULL, closed_at = NULL, updated_at = :now WHERE account_id = :a AND id = :entry.subject_id AND status = 'done' AND closed_reason = 'owner_done' AND closed_at = :entry.at`. The `closed_at = entry.at` guard (both were written from the same `now`) lets a later close–reopen–close cycle supersede the old Undo.
- **`undo_task_snoozed`**: lock the automation named in `details.automation_id` (if any) first, then the task (account-scoped). Return `False` (nothing changes) unless the task is `open` and `snoozed_until` equals `details.until`. Every successful path sets the task's `snoozed_until = details.previous_until` and returns `True`. With no `details.automation_id` (a to-do or a triage task) that is all. With one, also require that automation to still have `run_at = until`, then by `details.previous_status`:
  - `completed` (the reminder had already gone off before the snooze, e.g. "snooze it an hour" after the card arrived): set `status = 'completed'`, `run_at = previous_run_at`, `next_run_at = NULL`, `ended_at = :now`. Undoing the snooze means "do not remind me again".
  - `active` or `paused`: only when `previous_run_at > now` **and** no ledger row exists for `(automation_id, previous_run_at)`: set `run_at = next_run_at = previous_run_at`, `status = previous_status`, `ended_at = NULL`.
  - otherwise (the original time has passed, or its occurrence is already in the ledger, for example claimed just before the snooze and then swallowed by M7-REM's `occurrence_at != run_at` check): return `False`. The snooze stays and the reminder fires at `until`, so it is never silently lost (OD-17). Restoring an instant already in the ledger would never fire (`_advance` cannot claim it twice), and a passed one would fire late or be reported skipped right after the Undo; keeping the snooze is simpler and truthful.
  If an occurrence at `until` is already claimed and in flight when the Undo succeeds, the fire sees `occurrence_at != run_at` and enqueues nothing (M7-REM §3.4 step 1).

### 3.4 Snooze filters (separate functions; P3-interfaces §1.2)

`inbox/tasks.py`:
- `TaskView` gains `snoozed_until: datetime | None = None` as its last field (default kept, so every keyword constructor still works); `_view` fills it.
- `def awake(view: TaskView, now: datetime) -> bool` — `view.snoozed_until is None or view.snoozed_until <= now`.
- `def hide_snoozed(views: Iterable[TaskView], now: datetime) -> list[TaskView]` — keeps order.

Consumers, each with its own small change so the hot files get one-hunk edits:
- `memory/catch_up.py`: `def hide_snoozed_tasks(found: Mapping[int, AttentionTriage], now: datetime) -> dict[int, AttentionTriage]` returns copies (`dataclasses.replace`) whose `open_tasks` pass `tasks.hide_snoozed`. `covering_triage` itself is unchanged.
- `agent/tools/summaries.py`: right after each of the two `catch_up.covering_triage(...)` calls in the attention-list tool, apply `catch_up.hide_snoozed_tasks(..., now)` with the tool's `services.clock.now()`. Nothing else in the file changes.
- `inbox/digest.py`: `_commitments` filters the result of `tasks.due_between(...)` through `tasks.hide_snoozed(due, period_end)` before cutting and counting, so `commitments_more` counts awake tasks only. The digest's `covering_triage` call renders no tasks and is unchanged.
- **The triage context is unchanged on purpose** (`inbox/triage_store.py` → `open_for_conversation`): triage must keep seeing a snoozed open loop, so it can resolve it when the contact delivers and does not raise it again as new. Snooze hides a task from the owner's surfaces, not from triage. COMMIT-DUE and RD-M7 reuse `awake` and `hide_snoozed`.

"Re-surfaces at `snoozed_until`": a triage task or a to-do reappears in the attention list and the digest once `now >= snoozed_until`; an owner reminder fires again at that time through its re-armed automation (§3.1, snooze step 4).

### 3.5 Expiry (`_EXPIRABLE` in `inbox/tasks.py`)

The statement keeps `created_by = 'agent'` (owner tasks never expire) and gains in its `WHERE`:

```sql
AND (t.snoozed_until IS NULL OR t.snoozed_until <= :now)
AND NOT EXISTS (
    SELECT 1 FROM app.automations AS a
    WHERE a.account_id = t.account_id AND a.task_id = t.id
      AND a.kind = 'follow_up' AND a.status IN ('active', 'paused'))
AND NOT EXISTS (
    SELECT 1 FROM app.audit_log AS l
    WHERE l.account_id = t.account_id AND l.subject_type = 'task' AND l.subject_id = t.id
      AND l.event = 'task_reopened')
```

`expire_due` passes `now`. Expiry stays silent (content-free audit, no alert), as M05 built it.

### 3.6 The `update_task` tool (`agent/task_schemas.py`, `agent/tools/tasks.py`)

```python
class UpdateTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: Annotated[int, AfterValidator(_required_id)] = Field(
        description="The task (from list_tasks)."
    )
    op: Literal["complete", "snooze", "cancel", "reopen"] = Field(
        description="complete: mark done; snooze: hide it until snooze_until; cancel: drop it; "
        "reopen: open a closed task again."
    )
    snooze_until: Annotated[str | None, AfterValidator(_moment)] = Field(
        description="For snooze only: the owner-local time 'YYYY-MM-DD HH:MM'; null otherwise."
    )

    @model_validator(mode="after")
    def _shape(self) -> Self: ...  # snooze needs snooze_until; every other op needs null
```

Strict schema via `strict_schema` (no `format`, `pattern` or length keywords; validators raise `ArgumentRuleError` with fixed words and never echo the input). `_moment` reuses `agent/tools/common.py::parse_local_moment`; `_required_id` is the `check_id` pattern of `agent/tools/monitors.py`.

`UPDATE_TASK = ToolDefinition(name="update_task", capability=CapabilityClass.CONFIGURE, release_flag="tool.update_task", handler=update_task_tool, describe=describe_update_task)`, `returns_managed=False`, no `always_confirm`, no `confirm_if`: an untainted call executes and is echoed; a tainted run's call is held for a card by the gate (RG-11). `agent/tools/tasks.py::TOOLS = (UPDATE_TASK,)`.

Handler (one transaction from `services.session_factory`; `now = services.clock.now()`; `run_id = ctx.run_id if ctx.run_id > 0 else None`; `call_id = ctx.call_id or None`; actor `owner` when `ctx.held` (a confirmed card), else `agent`, P3-interfaces §13):
1. Load the account (its timezone). For `snooze`, read `snooze_until` in the owner's timezone with `automation/schedule.py::to_utc` (a DST gap resolves to the first valid instant; a fold to the first occurrence).
2. Call the owner function. `NOT_FOUND` ⇒ raise `NotInAccountError("task not found in this account")` (the executor reports "not found", RG-21). `INVALID` ⇒ `ToolOutcome("error", {"reason": <fixed sentence>})`: `not_open` "only an open task can be completed, snoozed or cancelled"; `time_passed` "that time has already passed"; `too_far` "a snooze is at most a year"; `open_already` "that task is already open".
3. `ALREADY` ⇒ `ToolOutcome("ok", {"task_id", "task_status", "changed": False})`, no echo.
4. `DONE` ⇒ echo through `agent/tools/echo.py::queue_echo(ctx, session, change.audit_id, text, undo=<per op>)` and return `ToolOutcome("ok", {"task_id", "task_status", "snoozed_until": "<local>"|null, "echo": echo.note})`. The result holds **ids, enums and owner-local times only — never the title**: a triage task's title is model-written from contact text, and returning it would taint the run.

| op | Echo (HTML; the title escaped once with `html.escape(title, quote=False)`) | Undo |
|---|---|---|
| complete | "✅ Done: {title}" | yes (`task_closed`) |
| snooze | "⏰ Snoozed until {YYYY-MM-DD HH:MM} ({timezone}): {title}" (+ " I'll remind you then." when a reminder was re-armed) | yes (`task_snoozed`) |
| cancel | "✖️ Cancelled: {title}" (+ " Its reminder is cancelled too." when `reminders_cancelled`) | no |
| reopen | "↩️ Reopened: {title}" (+ " It has no reminder now; ask me to set a new one." for a `kind = 'reminder'` task whose one-off reminder automation is neither `active` nor `paused`: cancelled, or completed because it already fired; the handler reads that automation account-scoped after the change) | no |

`task_status` is the stored status after the change (`done`, `open`, `cancelled`); a snooze reports `open` with `snoozed_until`, never "done" (RG-20). Times use `agent/tools/common.py::local_time` plus the timezone name.

`describe_update_task(session, account_id, args) -> str` (the tainted run's card; owner-facing): "Mark as done: “{title}”", "Snooze until {local} ({tz}): “{title}”", "Cancel: “{title}”", "Reopen: “{title}”". An unknown or foreign `task_id` raises `NotInAccountError` before any card. The describer returns plain text; the card escapes it and makes hidden characters visible (existing confirmation-card rules).

**`owner_request` rule text** for PR-OR10 (put it in the PR description): "Use `update_task` only when the owner asks to complete, snooze, cancel or reopen a task; take the `task_id` from `list_tasks`. Snooze times are owner-local 'YYYY-MM-DD HH:MM'. Relay the result as given: a snoozed task is still open, never done; a cancelled reminder task's reminder is off."

### 3.7 "Triage never reopens"

Rewrite the `inbox/tasks.py` module docstring sentence "Tasks are created only by triage …; the owner's own tasks and reminders are M07" to name both writers, and the `upsert_from_candidate` docstring line "A closed task is never reopened or retitled" to "Triage never reopens or retitles a closed task; only the owner reopens one (`reopen`, M7-TASK)". No triage behaviour changes.

### 3.8 Wiring (`_wire_automation` in `bootstrap.py`, function-local imports)

At the top of the Edvard function, so later blocks of the same function can read it (M7-AUTO adds `automation_controls` next to it):

```python
task_controls: TaskControls | None = OwnerTaskControls()  # M7-TASK
```

The fragment returns `ports=P3Ports(task_controls=task_controls, ...)` and `routes=ControlRoutes(undo_handlers={EVENT_TASK_CLOSED: undo_task_closed, EVENT_TASK_SNOOZED: undo_task_snoozed, ...})`. Lines only inside `_wire_automation`; the module import block is frozen.

### 3.9 Edge cases and failure modes

| Case | Behaviour |
|---|---|
| The owner completes a task that triage closed a second earlier | The conditional update finds no open row ⇒ `ALREADY` (done) or `INVALID`; never two closures |
| Snooze of a one-off reminder the owner paused | `run_at` and `next_run_at` move; the status stays `paused`; M7-AUTO's resume recomputes from now |
| Snooze while the old occurrence is claimed and queued | M7-REM's handler sees `snoozed_until > now` and notifies nothing; the new instant fires later |
| Snooze to a time in a DST gap | `to_utc` resolves to the first valid instant; the echo shows the resolved local time |
| Undo of a snooze after the owner snoozed again | `snoozed_until` no longer equals `until` ⇒ `SUPERSEDED` |
| Undo of a reminder's snooze after its original time passed | `SUPERSEDED`; the snooze stays and the reminder fires at `until` (never silently lost) |
| A fire and an owner change of the same reminder race | Both lock automation → task (M7-REM §3.4 lock rule); no deadlock; the fire re-checks under the lock |
| A task deleted by a data deletion before a press | `NOT_FOUND`; the card says the task no longer exists |
| A retried tool call (same `run_id`, `call_id`) | The audit lookup ⇒ `changed: False`, no second echo |
| A card pressed from another account | `claim_card` (account-scoped) fails first in the press handler; the port re-checks the id too |

Fail closed: any id outside the account is `NOT_FOUND`; a time that cannot be parsed is an argument error; nothing reports "done" for a change that did not happen (invariant 11).

## 4. Files

**Owned:**

| File | Change |
|---|---|
| `src/assistant/inbox/tasks.py` | §3.1–§3.5, §3.7 (M7-TASK owns the file after CP1, P3-interfaces §15.1) |
| `src/assistant/agent/task_schemas.py` (new) | `UpdateTaskArgs` |
| `src/assistant/agent/tools/tasks.py` (pre-created by F0-R) | `update_task_tool`, `describe_update_task`, `UPDATE_TASK`; `TOOLS = (UPDATE_TASK,)` |
| `src/assistant/memory/catch_up.py` | `hide_snoozed_tasks` only |
| `src/assistant/agent/tools/summaries.py` | the two call-site lines applying `hide_snoozed_tasks` |
| `src/assistant/inbox/digest.py` | the `hide_snoozed` filter in `_commitments` only |
| `src/assistant/bootstrap.py` | lines inside `_wire_automation` only (§3.8) |
| the test files in §5 | new |

**Named touch points in files you do not own:** none. `inbox/models.py` (the `Task` mirror) is J1's and already has the F0-S columns.

**Never edit without an ICR** (P3-interfaces §15.2): `policy/*`; the send-path `actions/*` files; `telegram/gateway.py`, `telegram/ingress/*`; `conversations/service.py`; migrations, ORM models and `privacy/*`; `config.py` and `core/*` (including `core/p3_ports.py`); `agent/{runtime,context,schemas}.py`, `agent/tools/executor.py`, `agent/tools/registry.py`; `inbox/triage_store.py` (M6-ESC); `inbox/notification_types.py`; `memory/undo.py`; `telegram/control/{dispatcher,routes,handlers,panels}.py`.

## 5. Tests (red first)

Synthetic data only; `FakeClock`; real PostgreSQL 18; factories `tests/integration/p3_factories.py::seed_owner_task` and `p2_factories.seed_task`, `seed_automation`, `seed_triage`. Run only these files locally (AGENTS §11).

**IMPLEMENTATION_PLAN §9 S07.1 named tests assigned here:**
- `tests/integration/test_owner_tasks.py::test_snooze_complete_cancel` — one task each: complete → `done/owner_done`; snooze → open with `snoozed_until`; cancel → `cancelled/owner_cancelled`; content-free audit rows.
- `tests/integration/test_owner_tasks.py::test_snoozed_task_resurfaces_at_snoozed_until` — a triage commitment snoozed to T is absent from the attention list's open tasks and the digest's commitments before T and present at T (fake clock); a reminder task snoozed to T has its one-off automation at `next_run_at = T`, `status = 'active'`.

**RG tests added or extended:**
- `tests/integration/test_task_tools.py::test_rg11_a_tainted_run_cannot_update_a_task_without_a_tap` — a tainted run gets `awaiting_confirmation` and the task is unchanged; after Confirm it is done.
- `tests/integration/test_task_tools.py::test_rg21_update_task_refuses_another_accounts_task` — the describer and the handler both answer "not found"; the other account's task is unchanged.
- `tests/integration/test_owner_tasks.py::test_rg21_task_controls_refuse_another_accounts_task` — each port method returns `NOT_FOUND`.
- `tests/integration/test_task_tools.py::test_rg20_a_snooze_is_never_reported_as_done` — tool data `task_status = "open"`; the echo says "Snoozed", never "Done".
- `tests/integration/test_log_hygiene_m7task.py::test_rg10_owner_task_changes_log_no_titles` — canary titles through every op; no log line and no audit `details` contains them.

**The unit's own tests:**
- `tests/unit/test_task_args.py`: `test_update_task_schema_is_strict`, `test_snooze_needs_a_time_and_other_ops_refuse_one`, `test_argument_errors_never_echo_the_input`.
- `tests/integration/test_owner_tasks.py`:
  - `test_snoozed_task_hidden_from_attention_list_and_digest_until_snoozed_until` (ARCHITECTURE §10.3)
  - `test_triage_context_still_sees_a_snoozed_task`
  - `test_snoozing_a_reminder_task_rearms_its_reminder` (a completed one-off becomes active at `until`; a paused one stays paused)
  - `test_completing_a_reminder_task_leaves_its_reminder_automation`
  - `test_cancelling_a_reminder_task_cancels_its_one_off_reminder` (a recurring reminder and a follow-up stay untouched)
  - `test_reopen_restores_open` (done, cancelled and expired → open; `closed_*` and `snoozed_until` cleared; audit `task_reopened`)
  - `test_triage_never_reopens_a_closed_task` (a new upsert for a closed task's promise leaves it closed)
  - `test_triage_cannot_close_or_retitle_an_owner_task` (an owner task with `conversation_id` NULL is never in a verdict's open ids; it stays unchanged)
  - `test_expiry_skips_a_task_watched_by_an_active_follow_up`
  - `test_expiry_skips_a_snoozed_task_until_it_wakes`
  - `test_expiry_skips_a_task_the_owner_reopened`
  - `test_owner_tasks_never_expire`
  - `test_create_owner_task_is_idempotent_on_run_and_call`
  - `test_create_owner_task_source_monitor_writes_one_task_created_row_with_the_automation_id` (R83: details `source: "monitor"` and `automation_id`; default source writes `"owner"` and no `automation_id`; `source_message_ids` stored sorted)
  - `test_create_owner_task_refuses_a_monitor_source_without_an_automation_id_and_an_owner_source_with_one` (`ValueError`)
  - `test_create_owner_task_refuses_another_accounts_refs`
  - `test_owner_task_has_no_conversation_and_names_its_chat_in_refs`
  - `test_undo_complete_reopens_only_while_unchanged`
  - `test_undo_snooze_restores_the_previous_time_and_reminder` (a not-yet-fired one-off returns to its old time and status; a fired one returns to `completed`)
  - `test_undo_snooze_after_the_original_time_passed_changes_nothing` (also when the old occurrence is already in the ledger; the reminder still fires at `until`)
  - `test_reopen_echo_says_when_a_reminder_task_has_no_reminder`
  - `test_undo_of_a_triage_close_or_a_cancel_is_refused`
  - `test_task_controls_return_port_results` (each method: `DONE`, `ALREADY`, `INVALID`, `NOT_FOUND`)
- `tests/integration/test_task_tools.py`:
  - `test_update_task_echoes_with_undo_and_returns_ids_only` (no title in `data`)
  - `test_cancel_and_reopen_echo_without_undo`
  - `test_update_task_is_idempotent_on_run_and_call` (one echo)
  - `test_update_task_is_offered_only_with_its_flag` (registry filtering; `owner_request` only)
  - `test_semantically_invalid_updates_are_factual_errors` (past snooze, snooze over a year, completing a closed task, reopening an open one)
  - `test_snooze_time_is_read_in_the_owner_timezone_across_a_dst_change` (Europe/Berlin, 2026-03-29 02:30 → 03:00 local)
  - `test_update_task_echo_escapes_untrusted_text_once` (the GW-ESC rule 6 string — `&`, `<b>x</b>` and a literal `&amp;` — through `telegram.render.sanitize`)

No mutation table: this unit is Tier 2, not Tier-1A (WORKFLOW §4.3).

**Shared lists you must update:** none by hand. `tool.update_task` is in F0-S's `P3_TOOL_FLAGS`, so F0-E's derived lists pick it up. If a strict-xfail QA-S7 scenario passes with this merge, tell QE1 on the unit's issue; QE1 removes the marker (P3-interfaces §15.1).

## 6. Acceptance criteria

1. `update_task` completes, snoozes, cancels and reopens an owned task; complete and snooze are echoed with a working Undo; cancel and reopen are echoed without one.
2. In a tainted run every `update_task` call becomes a confirmation card and changes nothing before the tap.
3. A snoozed task is hidden from the attention list and the digest until `snoozed_until`, stays visible to triage, and a snoozed owner reminder fires again at the new time (once M7-REM is merged).
4. Triage never reopens a task and cannot touch an owner task; expiry never closes a snoozed, follow-up-watched or owner-reopened task; owner tasks never expire.
5. `TaskControls` is wired in `_wire_automation` and returns the §3.2 results; another account's id is always `NOT_FOUND`.
6. No tool result, log line or audit row contains a task title; the title appears only in the owner's control chat.
7. All §5 tests green in CI; `lint-imports` and `mypy --strict` clean.

## 7. Docs updated in the same PR

- ARCHITECTURE §8.2: the `list_tasks` / `update_task` row as built for `update_task` (ops, echo and Undo rules, the taint gate, the ids-only result, `TaskControls`); the `list_tasks` half is RD-M7's.
- PR description: the `owner_request` rule text (§3.6), any contract gap found, and the open items for M07.md.

## 8. Owner decisions

- **OD-17** (reminders and to-dos): to-dos are `kind = reminder` tasks with `due_at` NULL (P3-T57); M7-TASK snoozes and closes them like any task. If the owner answers "no to-dos", only M7-REM changes.
- **OD-18** ("auto-close tells you and offers Reopen"): `TaskControls.reopen` is the mechanism. If the owner chose silent auto-close, `reopen` is still needed for the tool.
- The document conflict on who reopens a task is a TL correction (the IMPLEMENTATION_PLAN wins for owner actions); no owner question.

## 9. Risks and notes

- **Contract refinements this spec needs** (reported to the TL): `task_snoozed` details carry `previous_until`, `automation_id`, `previous_run_at` and `previous_status` for Undo (P3-interfaces §13 lists only `until`); `task_closed`, `task_snoozed` and `task_reopened` details carry `run_id` and `call_id` for idempotency; `task_created` gains `source` (`owner` or `monitor`, R83) and, for a monitor, `automation_id`; a cancel's `task_closed` carries `automations`.
- **Lock order** automation → task for owner reminders is the frozen cross-unit rule (R71) shared with M7-REM, M7-AUTO and M7-FUP; the peer reviewer checks every new write path follows it.
- **Snoozing a reminder writes `automations`** from `inbox/tasks.py`. This is deliberate: a one-off reminder's text lives in `tasks.title` (P3-T49), so the task is the reminder's record and its snooze must re-arm it in the same transaction. The mirror case is M7-AUTO's reschedule of a one-off reminder, which moves `tasks.due_at` and clears `tasks.snoozed_until` from `automation/management.py` (M7-AUTO §3.2), so a moved reminder is never held back by an older snooze.
- **Undo of a reminder's snooze** succeeds only while the old time is still ahead and not in the ledger (§3.3); otherwise the snooze stands and the reminder fires at `until`. The echo's Undo button may therefore answer "nothing was changed"; that is truthful.
- **IMPLEMENTATION_PLAN §9 risk "relative dates across timezone/DST"**: all owner-local parsing goes through `automation/schedule.py::to_utc`; a test covers a gap time.
- The expiry `NOT EXISTS` on `audit_log` is scoped per account and task. The one-year audit sweep can make a reopened task expirable again a year later; accepted.
