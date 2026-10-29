# M7-AUTO — Automation management: create, update, delete, `AutomationControls`

| | |
|---|---|
| **Unit** | M7-AUTO — `create_automation` (scheduled summaries), `update_automation` (pause, resume, reschedule, edit), `delete_automation`, `schedules.reschedule`, `AutomationControls` |
| **Team / slot** | Edvard / E1 |
| **Size** | S+ (≈ 0.75 engineer-day, one fix round included) |
| **Depends on** | F0-* (F0-S: `automations.paused_reason`, `provenance`, flags; F0-R: `ScheduledSummaryRule`, `ReminderRule`, the fire registry, `agent/tools/automations.py` with `TOOLS = ()`, `core/p3_ports.py`, `_wire_automation`). Also M7-TASK (`cancel`), M7-REM (`RecurrenceArgs`) and, through it, RRULE (R42; README §3 lists them): E1 builds M7-TASK → M7-REM → M7-AUTO in that order; if one has not merged, stack on its branch |
| **Branch → base** | `p3/m7-auto` → `int/p3` |
| **Peer reviewer** | a Josh engineer (Claude; the TL picks J1, J2 or J3). SET-P3's E1 read covers the `AutomationControls` consumer side |
| **QA** | QE1, after merge (reschedule double fire is QE1's focus) |
| **Tier** | 2 (product flow) |
| **Normative inputs** | [P3-schema §3.5, §7.2](../contracts/P3-schema.md); [P3-interfaces §4.5, §8.1, §8.2, §9.4, §9.5, §13, §15](../contracts/P3-interfaces.md); decisions P3-T22 (ADR-18), P3-T58, P3-T62; TL rulings R42, R51 (`delegation_end` hidden), R68, R69 and R97 (tool status `scheduled`, a NOT_RUN status), R71; IMPLEMENTATION_PLAN §9 S07.3; PRD FR-FUP-04, FR-FUP-05, FR-CTL-10, FR-ACT-01; ARCHITECTURE §7.5 (`AutomationSpec`), §8.1, §8.2, §10.3 `automations`, §12.4 |

## 1. Goal

The owner manages automations in plain words: "send me a summary of my work chats every weekday at 18:00", "pause my morning reminder", "move it to 8", "delete that summary". Every create and change goes through a confirmation card (FR-FUP-05). Pausing stops firing at once; resuming starts from now without a backlog; rescheduling recomputes `next_run_at` and never fires an occurrence twice; the owner's delete removes the automation and its content completely (OD-19). SET-P3's `/settings` rows use the same functions through `AutomationControls`.

## 2. Scope

**In:**
1. `automation/management.py`: create a scheduled summary; pause, resume, reschedule, edit, delete; `allowed_ops`; `describe_automation`; `AutomationManager` (implements `AutomationControls`).
2. `automation/schedules.py::reschedule` (named touch point, P3-interfaces §8.1).
3. `agent/automation_schemas.py`: `SummaryScopeArgs`, `CreateAutomationArgs`, `UpdateAutomationArgs`, `DeleteAutomationArgs`.
4. `agent/tools/automations.py`: `create_automation`, `update_automation`, `delete_automation` (all `always_confirm`), describers and notices.
5. Wiring lines in `_wire_automation`.

**Out:** `list_automations` (RD-M7; it may call `describe_automation`); firing scheduled summaries (M7-SSUM); creating reminders (M7-REM) and follow-ups (M7-FUP); the monitor tools `create_monitor` / `list_monitors` / `stop_monitor` (kept as M05 built them, OD-19); `/settings` screens (SET-P3); a scheduled-summary preview; creating any kind other than `scheduled_summary` through `create_automation`.

## 3. Design

### 3.1 What can be managed (`automation/management.py::allowed_ops`)

`class ManageOp(StrEnum): PAUSE, RESUME, RESCHEDULE, EDIT, DELETE`. `def allowed_ops(automation: Automation) -> frozenset[ManageOp]` is pure and is the single rule the tools, the describers and SET-P3's buttons use:

| Kind | Pause | Resume | Reschedule | Edit | Delete | Why |
|---|---|---|---|---|---|---|
| `scheduled_summary` | yes | yes | `at` or `recurrence` | scope, period | yes | |
| `reminder`, recurring (`rrule` set) | yes | yes | `recurrence` only | text | yes | |
| `reminder`, one-off | yes | yes | `at` only, before it goes off (also moves `tasks.due_at` and clears `tasks.snoozed_until`) | no (its text is `tasks.title`, P3-T49) | yes | After it went off, the owner snoozes its task instead (M7-TASK) |
| `monitor` | yes | yes | no (event-only) | no (stop it and create a new one) | yes | OD-19: `list_automations` shows monitors |
| `follow_up` | no | no | no | no | yes | Its deadline, matching and nudge semantics are M7-FUP's; the owner creates a new one instead |
| `digest` | no | no | no | no | no | Preference-managed (`update_preferences digest_time`, M05 `_sync_digest`) |
| `delegation_end` | no | no | no | no | no | Ending a delegation is M6-DELEG's (`end_delegation`); never gated, never deleted here. **Hidden** (R51): not listed, not described, not controllable |

**Hidden kind (R51).** `delegation_end` rows are internal timers, so `automation/management.py` also exports `LISTED_KINDS` (every kind except `delegation_end`) and `def is_listed(automation: Automation) -> bool`. `list_automations` (RD-M7), SET-P3's automation rows and the status block use `is_listed` and never show a `delegation_end` row. Every domain function, tool handler, describer and `AutomationManager` method that is given a `delegation_end` id answers exactly as for an id that does not exist (`NOT_FOUND`, or `NotInAccountError` before any card), so the owner cannot learn that the row exists and cannot pause, resume, reschedule or delete it (an ended delegation's timer is cleaned up by M6-LIFE and the retention sweep).

Status filters on top of the table: pause needs `status = 'active'`; resume needs `status = 'paused' AND paused_reason = 'owner'` (a paused row with `paused_reason` NULL is an invalid stored rule, or the digest switched off; resume is never offered, P3-schema §3.5); reschedule and edit need `active` or `paused`; delete is allowed in every status (an ended row still holds content until the one-year sweep).

### 3.2 Domain functions (`automation/management.py`)

```python
@dataclass(frozen=True, slots=True)
class ManageResult:
    result: PortResult  # core/p3_ports.py
    kind: str | None = None
    reason: str | None = None  # INVALID only: a fixed code (below)
    audit_id: int | None = None
    next_run_at: datetime | None = None


async def create_scheduled_summary(
    session,
    *,
    account_id: int,
    rule: ScheduledSummaryRule,
    spec: ScheduleSpec,
    expires_at: datetime | None,
    refs: Mapping[str, list[int]],
    provenance: Mapping[str, Any],
    run_id: int | None,
    tool_call_id: str | None,
    actor: str,
    clock: Clock,
) -> tuple[Automation, bool]: ...
async def pause(
    session, *, account_id: int, automation_id: int, actor: str, now: datetime
) -> ManageResult: ...
async def resume(
    session, *, account_id: int, automation_id: int, actor: str, now: datetime
) -> ManageResult: ...
async def reschedule(
    session,
    *,
    account_id: int,
    automation_id: int,
    run_at: datetime | None,
    recurring: ScheduleSpec | None,
    actor: str,
    now: datetime,
) -> ManageResult: ...
async def edit(
    session,
    *,
    account_id: int,
    automation_id: int,
    scope: MonitorScope | None,
    period: str | None,
    text: str | None,
    provenance: Mapping[str, Any],
    actor: str,
    now: datetime,
) -> ManageResult: ...
async def delete(
    session, *, account_id: int, automation_id: int, actor: str, now: datetime
) -> ManageResult: ...
async def describe_automation(
    session, *, account_id: int, automation: Automation, timezone: str
) -> str: ...
```

`INVALID` reason codes: `kind_not_managed`, `op_not_allowed`, `not_active`, `not_paused_by_owner`, `invalid_rule`, `time_passed`, `no_future_time`, `expired`, `chat_ignored`, `busy` (delete only: an occurrence is being fired right now; "try again in a minute"). Every statement carries `account_id = :account_id` (RG-21). Each function starts with `SELECT … FROM automations WHERE account_id = :a AND id = :id FOR UPDATE`; none found, or a `delegation_end` row (R51), ⇒ `NOT_FOUND`. Locks follow the frozen cross-unit rule (R71; P3-interfaces §8.1, M7-REM §3.4): automation → task, and never a wait on an `automation_occurrences` row lock while holding the automation (`NOWAIT` in a savepoint, or `SKIP LOCKED`). Nothing commits, calls Telegram or calls a model. Audit rows (P3-interfaces §13; `subject_type = 'automation'`; ids, codes and field names only): `automation_created` `{"kind"}`, `automation_updated` `{"kind", "fields": [...]}`, `automation_paused` `{"kind"}`, `automation_resumed` `{"kind"}`, `automation_deleted` `{"kind"}`. The actor is `owner` (every tool here is confirmed on a card; SET-P3 presses are the owner's).

**`create_scheduled_summary`.** Idempotent: an existing row of this account with the same `(run_id, tool_call_id)` ⇒ `(row, False)`, nothing written. Otherwise `schedules.create_scheduled(session, account_id=…, kind="scheduled_summary", rule=rule, spec=spec, clock=clock, refs=refs, expires_at=expires_at, run_id=run_id, tool_call_id=tool_call_id, confirmed_at=clock.now())`; then set `row.provenance = provenance` and flush (`repository.create` has no provenance parameter and is a never-edit file); audit `automation_created`. `ScheduleError` (no future occurrence) propagates to the tool as a factual error.

**`pause`.** Allowed per §3.1 ⇒ `status = 'paused', paused_reason = 'owner', updated_at = now`; audit `automation_paused` ⇒ `DONE`. Already paused by the owner ⇒ `ALREADY`. A claimed occurrence still queued is settled `inactive` by `plan_fire` (`_still_runs` is false for a paused row), so a pause takes effect for work already in a queue. A paused monitor stops matching (`monitors.applicable` reads `status = 'active'`).

**`resume`.** Allowed per §3.1 ⇒ for a `follow_owner` row first set `timezone` to the account's current timezone (the reconciler moves only active rows); then compute `next_run_at`: recurring ⇒ `next_occurrence(schedule_of(row, rule), after=now)`; one-off ⇒ `run_at` if `run_at > now`, else `INVALID` (`time_passed`: "its time has passed; reschedule it"); a monitor ⇒ `INVALID` (`expired`) when `expires_at <= now`, else no `next_run_at`. Set `status = 'active', paused_reason = NULL, next_run_at = …, updated_at = now`; audit `automation_resumed` ⇒ `DONE`. Missed occurrences during the pause are **not** recorded or reported: a pause means silence until resumed. A stored rule that no longer loads ⇒ `INVALID` (`invalid_rule`).

**`reschedule`.** Allowed per §3.1 and shape-preserving for reminders (a one-off takes `run_at`, a recurring one takes `recurring`; a scheduled summary takes either). Sets `timezone` to the account's timezone and flushes, calls `schedules.reschedule(...)` (§3.3) and, for a one-off reminder, `UPDATE tasks SET due_at = :run_at, snoozed_until = NULL, updated_at = :now WHERE account_id = :a AND id = :task_id AND status = 'open'` (after the automation lock; lock order automation → task, shared with M7-TASK and M7-REM). Clearing `snoozed_until` matters: the new time supersedes an earlier snooze, and M7-REM's fire handler skips a task whose `snoozed_until` is still in the future, so a reschedule to before the snooze time would otherwise be silently lost. `False` from `reschedule` ⇒ `INVALID` (`no_future_time`). A one-off that already fired is `completed`, so it fails the status filter (`not_active`, fixed sentence "it already went off; snooze its task or set a new reminder"). Audit `automation_updated` `{"kind", "fields": ["schedule"]}` ⇒ `DONE` with `next_run_at`.

**`edit`.** Allowed per §3.1:
- `scheduled_summary`: a new `ScheduledSummaryRule` from the stored one with the new `scope` and/or `period` (`model_copy`), re-validated; the scope is checked exactly as at creation (§3.4 step 2: ids in the account, no Ignore-mode or bot chat ⇒ `INVALID` `chat_ignored`); `spec` rewritten; `refs` rewritten to the new scope's ids (deletion coverage, P3-schema §7.2).
- recurring `reminder`: `ReminderRule.text` replaced (1–400 characters); when the new provenance is tainted, its origin chat and contact ids are added to `refs` too, as M7-REM §3.2 step 3 does for new reminder text (per-chat deletion must find text that may come from those chats).
- `provenance` merged: `tainted = old.tainted or new.tainted`, `managed = old.managed or new.managed`, `refs` = union (owner text written by a tainted run stays marked; P3-schema §3.5).
- Audit `automation_updated` with the changed field names (`scope`, `period`, `text`) ⇒ `DONE`. Nothing that changes `next_run_at` happens here.

**`delete`** (owner delete = hard delete, OD-19). Allowed per §3.1:
1. Remember `kind`, `task_id`, `rrule` and the describe text (for the notice) before deleting.
2. Inside `session.begin_nested()`: `SELECT id FROM automation_occurrences WHERE account_id = :a AND automation_id = :id FOR UPDATE NOWAIT`. A lock conflict (`LockNotAvailable`, SQLSTATE 55P03) rolls back the savepoint and returns `INVALID` (`busy`): a fire is running on this automation, and the cascade below would otherwise wait for its occurrence lock while holding the automation lock, while a reminder fire waits for the automation lock (a deadlock, M7-REM §3.4). Then `DELETE FROM automations WHERE account_id = :a AND id = :id`. The ledger rows go with it (`automation_occurrences.automation_id` FK `ON DELETE CASCADE`), so a queued `automation.fire` job finds no occurrence and `plan_fire` returns `NONE` — "delete cancels a queued occurrence (the handler re-checks status)". A queued `automation.summarize` job finds no automation and sends nothing (M7-SSUM re-checks). Nudge actions keep existing (`actions.trigger_automation_id` is `ON DELETE SET NULL`). Queued notifications about it render to `None` and are suppressed (their renderers reload the automation).
3. The linked task: when `task_id` is set and the task is `open` and either (a) the row was a one-off reminder, or (b) the row was a follow-up and the task is `kind = 'follow_up'` with `created_by = 'owner'` (a task made for the follow-up), call M7-TASK's `inbox.tasks.cancel(session, account_id=…, task_id=…, actor=…, now=…)`. A triage task that a follow-up merely watched is left alone.
4. Audit `automation_deleted` `{"kind"}` (the row id stays in `subject_id`; the audit row is content-free) ⇒ `DONE`.

There is no Undo for automations (P3-interfaces §12.7); the card is the safeguard.

**`AutomationManager`** (stateless) implements `AutomationControls` exactly (P3-interfaces §4.5): `pause`, `resume`, `delete` call the functions above with `actor="owner"` and return `ManageResult.result`. SET-P3 hides a button whose op is not in `allowed_ops(row)` and shows the fixed reason for an `INVALID` result.

**`describe_automation`** returns plain owner-facing words (callers escape): `scheduled_summary` "a summary of {scope words} covering {period words}, {schedule words}"; `reminder` "the reminder “{text}” {schedule words}" (one-off text from `tasks.title`); `follow_up` "the follow-up on the chat with {name}, due {local time}"; `monitor` `automation.monitors.describe(...)` (exists). Scope words: "all chats you observe" or "the chats with {names}" (names via `memory.summaries.preferred_name`; a deleted contact reads "a contact"). Schedule words: a one-off "on {echo_instant(run_at, tz)}"; a recurring one `describe_recurrence(rec)` when `rec = recurrence_from_rule(row.rrule, rule.start())` is not `None` (RRULE spec §6.1; there is no `describe_rrule`), else "on a repeating schedule"; then "next on {echo_instant(next_run_at, tz)}" when `next_run_at` is set. A paused row adds " (paused)".

### 3.3 `schedules.reschedule` (named touch point in `automation/schedules.py`)

```python
async def reschedule(
    session: AsyncSession,
    *,
    account_id: int,
    automation_id: int,
    rrule: str | None,
    run_at: datetime | None,
    now: datetime,
    dtstart_local: datetime | None = None,
) -> bool: ...
```

`dtstart_local` is an addition to the contract's signature (a recurring schedule needs its local start; contract gap reported). Behaviour:
1. `SELECT … FOR UPDATE` the row (account-scoped; the caller already holds the lock in the same transaction). Kinds `digest` and `delegation_end`, a missing row, or a status other than `active`/`paused` ⇒ return `False`.
2. Build `ScheduleSpec(timezone=row.timezone, tz_policy="follow_owner" if rrule else "fixed", rrule=rrule, dtstart_local=dtstart_local, run_at=run_at)` and `validate` it (`ScheduleError` ⇒ `False`).
3. **No double fire.** Settle every `claimed` occurrence of this automation that no job is holding: `UPDATE automation_occurrences SET status = 'skipped', note_code = 'inactive' WHERE id IN (SELECT id FROM automation_occurrences WHERE automation_id = :id AND account_id = :a AND status = 'claimed' FOR UPDATE SKIP LOCKED)`. A row a running `plan_fire` holds is skipped by `SKIP LOCKED` and finishes its fire (it was already firing). Note `inactive` never calls `on_skipped`: the owner changed the schedule, nothing was missed.
4. `last = SELECT max(occurrence_at) FROM automation_occurrences WHERE account_id = :a AND automation_id = :id AND status IN ('claimed', 'fired')` (what is fired or still firing). `next_at = next_occurrence(spec, after=max(now, last))` (`last` may be `None`). `None`, or `expires_at` set and `next_at >= expires_at` ⇒ return `False`; the caller's transaction is rolled back to before step 3 (run steps 3–5 inside `session.begin_nested()` and roll it back on `False`), so nothing changes. So no instant ever fires twice, and a stale claim never fires after the change.
5. Write `rrule`, `run_at`, `tz_policy`, `next_run_at = next_at` (whole seconds), the rule's `dtstart_local` (`rule.model_copy(update={"dtstart_local": None if dtstart_local is None else dtstart_local.strftime(DTSTART_FORMAT)})` from `automation/rules.py`, the format `create_scheduled` writes; stored as `spec`, owner text unchanged) and `updated_at = now`. The status is not changed: only `active` and `paused` rows get here (step 1), and a `paused` row stays `paused` (resume recomputes `next_run_at` from now anyway). Return `True`.

Lock order against the fire path: `plan_fire` locks the occurrence, then (in some handlers) the automation; `reschedule` locks the automation and never waits on an occurrence (`SKIP LOCKED`), so the two cannot deadlock. `reconcile_due` locks automations with `SKIP LOCKED` and skips a row being rescheduled for that minute.

DST: a recurring schedule keeps its owner-local wall time (`follow_owner`, `next_occurrence` resolves every candidate with `to_utc`: a gap time fires at the first valid instant, a fold once at its first occurrence); a one-off `at` is resolved once with `to_utc` and echoed as the resolved instant.

### 3.4 Tools (`agent/automation_schemas.py`, `agent/tools/automations.py`)

Arguments (strict via `strict_schema`; validators raise `ArgumentRuleError` in fixed words; owner-local times as strings parsed with `parse_local_moment` and `automation/schedule.py::to_utc`, never `datetime` fields; P3-interfaces §9.4):

```python
class SummaryScopeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_ids: list[int]  # 0–20, from resolve_contact
    conversation_ids: list[int]  # 0–20
    all_observed: bool  # every chat in Observe or Assist mode
    # validator: all_observed ⇒ both lists empty; otherwise at least one id; ids via check_id; no duplicates


class CreateAutomationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["scheduled_summary"]
    scope: SummaryScopeArgs
    period: Literal["since_last", "last_24h", "last_7d"]
    at: str | None  # one-off: owner-local "YYYY-MM-DD HH:MM"
    recurrence: RecurrenceArgs | None  # agent/reminder_schemas.py (M7-REM)
    expires_at: str | None  # owner-local "YYYY-MM-DD HH:MM"; recurring only; null = no end
    # validator: exactly one of at / recurrence; expires_at only with recurrence


class UpdateAutomationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    automation_id: Annotated[int, AfterValidator(_required_id)]
    op: Literal["pause", "resume", "reschedule", "edit"]
    at: str | None
    recurrence: RecurrenceArgs | None
    scope: SummaryScopeArgs | None
    period: Literal["since_last", "last_24h", "last_7d"] | None
    text: str | None  # recurring reminder text, 1–400
    # validator: pause/resume ⇒ every other field null; reschedule ⇒ exactly one of at/recurrence,
    #            the rest null; edit ⇒ at least one of scope/period/text, never at/recurrence,
    #            and text never together with scope/period


class DeleteAutomationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    automation_id: Annotated[int, AfterValidator(_required_id)]
```

Definitions (P3-interfaces §9.4; class `configure`; `returns_managed=False`; `always_confirm=True`; flags `tool.create_automation`, `tool.update_automation`, `tool.delete_automation`): `CREATE_AUTOMATION`, `UPDATE_AUTOMATION`, `DELETE_AUTOMATION`, each with its describer; `agent/tools/automations.py::TOOLS = (CREATE_AUTOMATION, UPDATE_AUTOMATION, DELETE_AUTOMATION)`. Before the owner's tap the executor returns `awaiting_confirmation`; a handler reached without `ctx.held` returns `ToolOutcome("not_executed", {"reason": "an automation changes only after the owner taps Confirm on its card"}, log_code="not_confirmed")` and changes nothing (the `create_monitor` pattern). Idempotency on `(run_id, tool_call_id)` (P3-interfaces §9.4): a held call is applied at most once by `ConfirmationApplier.apply` (it claims the `tool_confirmations` row `confirmed → applying → applied`), and a repeated call in the run gets the confirmation's status (`confirmed` / `applied`) from the executor; `create_automation` also checks `(run_id, tool_call_id)` itself (step 6), and a repeated pause or resume is `ALREADY`.

**`create_automation` handler** (only with `ctx.held`; one transaction):
1. `SCHEDULED_SUMMARIES_RUN` (`scheduled_summaries.run`) must be in `ctx.services.enabled_flags`; otherwise `ToolOutcome("error", {"reason": "scheduled summaries are not switched on yet, so nothing was created"})`. Without this check a confirmed summary would never fire (`disabled` or `no_handler` notes are not reported), for example while M7-SSUM is not yet deployed.
2. Scope check: `require_contacts` / `require_conversations` (foreign ids ⇒ `NotInAccountError`, RG-21); a named chat in Ignore mode or with a bot ⇒ error "that chat is ignored: summaries cover only chats in Observe or Assist mode" (the rule of `agent/tools/monitors.py::scope_problem`, using `automation.monitors.WATCHED_MODES`).
3. Schedule (`tz = account.timezone`): one-off `run_at = to_utc(parse_local_moment(at), tz)`, must be `> now` and ≤ 366 days ahead, `ScheduleSpec(timezone=tz, tz_policy="fixed", run_at=run_at)`; recurring `recurrence.to_schedule(args.recurrence.to_recurrence(), timezone=tz, starts_on=<starts_on, or the owner-local date of now>)` (RRULE, as M7-REM §3.3). `expires_at`, when given, is `to_utc(parse_local_moment(expires_at), tz)` and must be after the first occurrence (`next_occurrence(spec, after=now)`). A `ScheduleError` or no future occurrence ⇒ error "that schedule has no future time".
4. Rule `ScheduledSummaryRule(scope=MonitorScope(...), period=…)` (default `max_lateness_minutes` 120, OD-17); `refs = {"conversation_ids": …, "contact_ids": …}` from the scope (an `all_observed` scope has empty refs).
5. Provenance, the M7-REM §3.2 step 3 baseline: `origin = await run_origin(session, ctx)` with `ctx` unchanged; `provenance = memory.items.provenance(tainted=origin.tainted, managed=origin.tainted, refs=origin.refs.as_json() if origin.tainted else None)`. `run_origin` counts every held call as tainted (M03 N1, fail closed), and every tool here is `always_confirm`, so every scheduled summary is stored tainted and managed, and managed-data deletion removes it (P3-schema §7.2 step 5): over-deletion, the safe side. Never clear `ctx.held` to get around N1. Every P3 unit uses this baseline (R68, §9); a narrower rule for `always_confirm` tools would need a contract change.
6. `management.create_scheduled_summary(...)`; `created = False` ⇒ `ToolOutcome("scheduled", {"automation_id", "changed": False})`, no notice.
7. Notice after commit through `queue_echo(ctx, session, audit_id, text, undo=False)`: "🗓️ Scheduled summary on: {describe_automation(...)}." (escaped once with `html.escape(…, quote=False)`).
8. Result (R69) `ToolOutcome("scheduled", {"automation_id", "kind": "scheduled_summary", "automation_status": "active", "next": "YYYY-MM-DD HH:MM", "timezone": tz})` — never the scope's names. Status `scheduled` is a NOT_RUN status (in `NOT_RUN_STATUSES`, P3-interfaces §9.3; R97: it has not run yet, so it is never a success status): armed and not yet fired, never `ok` or done (invariant 11). `update_automation` and `delete_automation` keep `ok` for the change they report.

**`update_automation` handler** (only with `ctx.held`): load the row (account-scoped; missing ⇒ `NotInAccountError`); op not in `allowed_ops(row)` ⇒ `ToolOutcome("error", {"reason": <fixed sentence for the kind>})`; parse the schedule or scope as in create; call the domain function; `INVALID` ⇒ error with its fixed sentence; `ALREADY` ⇒ `ok`, `changed: False`; `DONE` ⇒ notice "⏸️ Paused: …", "▶️ Resumed: …; next on …", "🗓️ Moved: …; next on …" or "✏️ Changed: …" and `ToolOutcome("ok", {"automation_id", "kind", "automation_status", "next": local|null, "changed": True})`.

**`delete_automation` handler** (only with `ctx.held`): load (account-scoped); not allowed ⇒ error; `management.delete(...)`; notice "🗑️ Deleted: {describe text captured before the delete}." (+ " Its task is cancelled too." when step 3 cancelled one); result `ToolOutcome("ok", {"automation_id", "kind", "deleted": True})`. A repeat after deletion ⇒ `NotInAccountError` ("not found"), which is the truthful state.

**Describers** (the cards; owner-facing plain text; the card escapes it and makes hidden characters visible): create "Send you {summary words}"; update "Pause / Resume / Move to {new schedule words} / Change to {new scope or period or text}: {describe_automation(row)}"; delete "Delete {describe_automation(row)} completely (it cannot be undone)". A foreign or unknown id raises `NotInAccountError` before any card (RG-21). An op the row does not allow, or an ignored chat in a new scope, gives "{text}. This cannot be done: {reason}. Confirming changes nothing." (the `create_monitor` pattern), and the handler refuses again after the tap.

**`owner_request` rule text** for PR-OR10 (in the PR description): "Use `create_automation` for a scheduled summary of chats ('every weekday at 18:00, summarize my work chats'); use `update_automation` to pause, resume, reschedule or edit, and `delete_automation` to remove one; take ids from `list_automations`. Every one of them waits for the owner's confirmation: never say an automation is on, changed or deleted before the tool returned `ok`. The daily digest is changed with `update_preferences`, a delegation with `end_delegation`, and a follow-up only by deleting it and creating a new one."

### 3.5 Wiring (`_wire_automation` in `bootstrap.py`; function-local imports)

Next to M7-TASK's line at the top of the function:

```python
automation_controls: AutomationControls | None = AutomationManager()  # M7-AUTO
```

The fragment's ports gain `automation_controls=automation_controls`. M7-REM's renderer and press handler (built later in the same function) receive it, so the recurring reminder card's "Pause reminder" appears once this merges (replace M7-REM's temporary `None` declaration if present). No job, reconciler or route is added by this unit.

### 3.6 Edge cases and failure modes

| Case | Behaviour |
|---|---|
| Reschedule while the old occurrence is claimed and its fire job queued | The claim is settled `inactive`; the old time never fires; the next fire is at the new time |
| Reschedule while `plan_fire` is firing the old occurrence | `SKIP LOCKED` leaves it; it fires once; the new `next_run_at` is after it |
| Reschedule to a time already fired today (daily summary) | `after = max(now, last fired)` ⇒ the next valid instant after the fired one |
| Pause, then the queued fire job runs | `plan_fire` settles `inactive`; nothing sent |
| Resume a one-off whose time passed | `INVALID` `time_passed`; the row stays paused; the owner reschedules or deletes |
| Resume a paused monitor past its expiry | `INVALID` `expired`; delete offered |
| Resume a row paused as invalid (`paused_reason` NULL) | Not offered; `INVALID` `not_paused_by_owner` |
| Delete with a queued `automation.summarize` job | The job finds no automation and sends nothing (M7-SSUM) |
| Delete a follow-up that watches a triage task | The automation goes; the triage task stays open |
| Owner deletes, then a stale card press arrives | The port returns `NOT_FOUND` |
| Delete while an occurrence of it is being fired | `INVALID` `busy` ("try again in a minute"); nothing deleted; no deadlock |
| Reschedule a snoozed one-off reminder to before its snooze time | `tasks.snoozed_until` is cleared with the move, so the reminder fires at the new time |
| Reschedule with an `expires_at` at or before the next occurrence | `INVALID` `no_future_time`; nothing changes (savepoint) |
| Reschedule or resume a daily 18:00 summary across a DST change | Fires at 18:00 local on both sides of the change |
| Edit a scope to a chat now in Ignore mode | `INVALID` `chat_ignored`; nothing changes |

Fail closed: unknown ids are "not found"; disallowed ops change nothing; a confirmed card whose state changed meanwhile is re-checked by the handler.

## 4. Files

**Owned:**

| File | Change |
|---|---|
| `src/assistant/automation/management.py` (new) | §3.1, §3.2 |
| `src/assistant/automation/schedules.py` | `reschedule` only (named touch point; §3.3) |
| `src/assistant/agent/automation_schemas.py` (new) | §3.4 argument models |
| `src/assistant/agent/tools/automations.py` (pre-created by F0-R) | the three tools, describers, notices; `TOOLS` |
| `src/assistant/bootstrap.py` | lines inside `_wire_automation` only (§3.5) |
| the test files in §5 | new |

**Named touch points in files you do not own:** `automation/schedules.py::reschedule` (P3-interfaces §8.1, §15.1; M7-FUP holds the separate `_still_runs` touch point — never edit each other's function). You call, never edit: `automation/schedules.py` (`create_scheduled`, `schedule_of`, `next_occurrence`), `automation/monitors.py` (`describe`, `WATCHED_MODES`), `inbox/tasks.py::cancel` (M7-TASK), `agent/reminder_schemas.py::RecurrenceArgs` (M7-REM), `automation/recurrence.py` (RRULE), `agent/tools/{common,echo,memory}.py`.

**Never edit without an ICR** (P3-interfaces §15.2): `policy/*`; the send-path `actions/*` files; `telegram/*`; `conversations/service.py`; migrations, ORM models (`automation/models.py`) and `privacy/*`; `config.py`, `core/*`; `agent/{runtime,context,schemas}.py`, `agent/tools/executor.py`, `agent/tools/registry.py`; `automation/repository.py`, `automation/rules.py`, `automation/monitors.py` (M7-MACT), `agent/tools/monitors.py`; `memory/undo.py`.

## 5. Tests (red first)

Synthetic data only; `FakeClock`; real PostgreSQL 18; `p2_factories.seed_automation(kind=..., spec=..., rrule=..., run_at=..., next_run_at=..., status=..., paused_reason=..., provenance=...)`, `p3_factories.seed_owner_task`; F0-R's `ScheduleJobs.fire` with the fire registry for queued-occurrence tests.

**IMPLEMENTATION_PLAN §9 S07.3 named tests assigned here:**
- `tests/integration/test_automation_management.py::test_update_recomputes_next_run_at` — a daily 18:00 summary moved to 08:00 (and a one-off reminder moved later) has the new `next_run_at`; the reconciler claims at the new time only.
- `tests/integration/test_automation_management.py::test_delete_cancels_a_queued_occurrence` — a claimed occurrence with a queued fire job; delete; running the fire job returns `NONE`, enqueues nothing, defers nothing.
- `tests/integration/test_automation_tools.py::test_create_and_modify_require_confirmation` — `create_automation`, `update_automation` and `delete_automation` each return `awaiting_confirmation` and change nothing; after Confirm each applies once.

**RG tests added or extended:**
- `tests/integration/test_automation_tools.py::test_rg11_a_tainted_run_cannot_change_automations_without_a_tap` (all three tools).
- `tests/integration/test_automation_tools.py::test_rg21_automation_tools_refuse_foreign_ids_before_any_card` (describers and handlers).
- `tests/integration/test_automation_management.py::test_rg21_management_and_controls_refuse_another_accounts_automation`.
- `tests/integration/test_automation_tools.py::test_rg20_results_never_say_active_or_deleted_before_confirmation`.
- `tests/integration/test_log_hygiene_m7auto.py::test_rg10_automation_management_logs_no_names_or_text` (canary reminder text, contact names in a scope).

**The unit's own tests:**
- `tests/unit/test_automation_args.py`: `test_automation_schemas_are_strict`, `test_create_needs_exactly_one_schedule`, `test_update_fields_follow_the_op`, `test_scope_needs_ids_or_all_observed`, `test_allowed_ops_table` (every row of §3.1, including status filters).
- `tests/integration/test_automation_management.py`:
  - `test_reschedule_after_a_claim_does_not_fire_the_old_occurrence`
  - `test_reschedule_never_reuses_a_fired_instant`
  - `test_reschedule_skips_an_occurrence_being_fired` (a second session holds the occurrence lock)
  - `test_rescheduling_a_one_off_reminder_moves_its_task_due_time` (and clears `snoozed_until`, so the moved reminder fires)
  - `test_reschedule_that_fails_changes_nothing` (no future time, or past `expires_at`: claimed occurrences stay claimed)
  - `test_a_rescheduled_daily_summary_keeps_its_local_time_across_a_dst_change` (Europe/Berlin, 18:00 local before and after 2026-10-25)
  - `test_resume_of_a_follow_owner_row_uses_the_current_timezone`
  - `test_delete_while_an_occurrence_is_firing_refuses_without_deadlock` (two sessions with barriers: one holds the occurrence lock as `plan_fire` does; delete ⇒ `INVALID` `busy`; nothing deleted)
  - `test_pause_stops_firing_and_resume_starts_from_now_without_backlog`
  - `test_a_queued_fire_after_pause_is_settled_inactive`
  - `test_resume_of_a_passed_one_off_or_an_invalid_rule_is_refused`
  - `test_pausing_a_monitor_stops_its_matches`
  - `test_owner_delete_is_a_hard_delete` (OD-19: the row and its ledger are gone; the audit row remains, content-free)
  - `test_deleting_a_one_off_reminder_or_follow_up_cancels_its_own_task` (a watched triage task stays open)
  - `test_digest_cannot_be_changed_or_deleted`
  - `test_delegation_end_automations_are_hidden_and_uncontrollable` (R51: `is_listed` is false and the listing readers (RD-M7's `list_automations`, SET-P3's rows, the status block) use it; every domain function, `AutomationManager` method, describer and tool handler answers as for a missing id; nothing changes)
  - `test_follow_ups_can_only_be_deleted`
  - `test_editing_a_scope_rewrites_refs_and_merges_provenance`
  - `test_editing_a_recurring_reminder_text` (provenance merged; a tainted edit adds its origin chats and contacts to `refs`)
  - `test_automation_controls_return_port_results` (pause, resume, delete: `DONE`, `ALREADY`, `INVALID`, `NOT_FOUND`)
- `tests/integration/test_automation_tools.py`:
  - `test_create_automation_creates_one_summary_and_a_notice` (the result status is `scheduled`, R69; it is in `NOT_RUN_STATUSES` and not in any success set, R97)
  - `test_create_automation_is_idempotent_on_run_and_call`
  - `test_update_and_delete_apply_once_per_held_call` (the same confirmation applied twice changes the row once and writes one audit row)
  - `test_create_automation_refuses_ignored_or_bot_chats`
  - `test_create_automation_refuses_when_scheduled_summaries_are_off`
  - `test_automation_tools_are_offered_only_with_their_flags` (registry filtering; `owner_request` only)
  - `test_card_text_describes_the_change_in_plain_words`
  - `test_semantically_invalid_changes_are_factual_errors` (past time, edit a one-off's text, reschedule a monitor, resume an active row, reschedule a one-off that already went off)
  - `test_create_automation_provenance_follows_run_origin` (held ⇒ tainted and managed, origin refs kept; `delete_managed_data` removes the summary)
  - `test_describe_automation_uses_recurrence_from_rule` (the four RRULE shapes in words; an unknown rule reads "on a repeating schedule")
  - `test_automation_notice_escapes_untrusted_text_once` (GW-ESC §3.3 rule 6 string in a contact name and a reminder text, through `telegram.render.sanitize`)

No mutation table: this unit is Tier 2, not Tier-1A (WORKFLOW §4.3).

**Shared lists you must update:** none by hand (the three tool flags are in F0-S's `P3_TOOL_FLAGS`). Tell QE1 if a strict-xfail QA-S7 automation-management scenario passes with this merge.

## 6. Acceptance criteria

1. "Summarize my work chats every weekday at 18:00" → card → Confirm → one active `scheduled_summary` automation with the right `next_run_at`, refs and provenance; nothing exists before the tap.
2. Pause, resume, reschedule, edit and delete work per §3.1; every one is a card; disallowed ops and kinds change nothing and say why.
3. Reschedule recomputes `next_run_at` at the same local wall time across DST, never fires an instant twice, never lets a stale claim fire, and a failed reschedule changes nothing.
4. The owner's delete removes the automation and its ledger completely, cancels its own task (one-off reminder, owner follow-up task), and a queued occurrence fires nothing; while a fire of it is running, delete answers "busy" and never deadlocks.
5. `AutomationControls` is wired; SET-P3 and the recurring reminder card can pause, resume and delete through it.
6. All §5 tests green in CI; `lint-imports` and `mypy --strict` clean.

## 7. Docs updated in the same PR

- ARCHITECTURE §7.5: replace the `AutomationSpec` sketch with the as-built shapes — `CreateAutomationArgs`/`UpdateAutomationArgs` as strict tool arguments (no `interpret_instruction`, ADR-18; this resolves the ARCHITECTURE §7.5 `AutomationSpec` sketch), and the persisted `ScheduledSummaryRule`/`ReminderRule` they map to; the digest is not managed here.
- ARCHITECTURE §8.2: the `create_monitor` / `create_follow_up` / `create_automation` row's `create_automation` part, and the `list_automations` / `update_automation` / `delete_automation` row's update and delete parts (the `allowed_ops` table, hard delete, no Undo, `AutomationControls`); `list_automations` is RD-M7's.
- PR description: the `owner_request` rule text, the contract gaps, open items for M07.md.

## 8. Owner decisions

- **OD-19** (default assumed): the owner's delete is a hard delete; `list_automations` shows monitors and the M05 monitor tools stay. If the owner wants a cancel instead, `delete` sets `status = 'cancelled', ended_at = now` and keeps the row for the one-year sweep; nothing else changes.
- **OD-13** (strict arguments plus a card, ADR-18): the automation spec is the tool's arguments. A different answer adds an interpretation call before the card; the domain functions do not change.
- **OD-17** (lateness): a scheduled summary tolerates 2 h (F0-R's `ScheduledSummaryRule` default); a different value changes only that default.

## 9. Risks and notes

- **`reschedule` signature**: this spec adds `dtstart_local` (needed for a recurring schedule); the contract adopts it (R32). Until it does, existing contract text wins: do not widen a frozen interface without the change.
- **Lock rule**: the delete's `NOWAIT` ledger lock and the `busy` code follow the rule frozen in P3-interfaces §8.1 (R71) and M7-REM §3.4.
- **Dependencies (R42)**: M7-AUTO uses M7-TASK's `cancel`, M7-REM's `RecurrenceArgs` and RRULE; README §3 lists them, and E1's order makes this free.
- **Schedule words** use RRULE's `recurrence_from_rule` + `describe_recurrence` (RRULE spec §6.1). There is no `describe_rrule`; a rule outside RRULE's four shapes reads "on a repeating schedule" with the next time, which is still truthful.
- **Follow-ups are delete-only here.** If the owner asks to pause a follow-up, the tool says it cannot; revisit after M7-FUP if the owner wants it.
- **Provenance of card-confirmed creates** follows the `run_origin` baseline (held ⇒ tainted and managed, M03 N1), as M7-REM and M6-DELEG do; managed-data deletion therefore removes every scheduled summary. Every unit uses this baseline (R68); M7-FUP aligns.
- **Delete can answer "busy"** while a fire of the same automation is running (a few hundred milliseconds); the owner retries. This is the price of never deadlocking with the reminder and follow-up fire handlers.
