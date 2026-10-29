# M7-REM — Reminders and the `reminder_due` card

| | |
|---|---|
| **Unit** | M7-REM — `create_reminder` (one-off, recurring, to-do), the `reminder` fire handler and its skip reporter, Undo, the `reminder_due` renderer and its card buttons (moved in from M7-TXT, R2) |
| **Team / slot** | Edvard / E1 |
| **Size** | M (≈ 1 engineer-day, one fix round included; R28: the renderer and buttons are included) |
| **Depends on** | F0-*, RRULE, GW-ESC (landed in P2 #133; verification must be closed before merge), per README §3. M7-TASK (R42: `create_owner_task` and `TaskControls`; README §3 lists it) |
| **Branch → base** | `p3/m7-rem` → `int/p3` |
| **Peer reviewer** | a Josh engineer (Claude; the TL picks J1, J2 or J3) |
| **QA** | QE1, after merge (on `int/p3` before R1; reminder lateness and DST are QE1's focus) |
| **Tier** | 2 (product flow) |
| **Normative inputs** | [P3-schema §3.4, §3.5, §7.2 step 7](../contracts/P3-schema.md); [P3-interfaces §8.1, §8.2, §9.3, §9.4, §11, §12.3, §12.5, §12.7, §13, §15](../contracts/P3-interfaces.md); decisions P3-T45, P3-T49, P3-T52, P3-T57, P3-T58, P3-T62; TL rulings R42, R68, R69 and R97 (tool status `scheduled`, a NOT_RUN status), R71, R72, R73, R82 (no feedback buttons); IMPLEMENTATION_PLAN §9 S07.1; PRD FR-FUP-01, FR-FUP-08, §11.2 ("simple reminders confirmed inline"), §15 (reminders fire with the LLM down); ARCHITECTURE §8.1, §8.2, §10.3, §12.4; [specs/GW-ESC.md §3.3](GW-ESC.md) (escape-once rule) |

## 1. Goal

"Remind me tomorrow at 9 to call the bank" sets a reminder at once and echoes the exact local time with Undo; "every weekday at 9" asks for a confirmation card first; "add 'renew passport' to my list" makes a to-do with no time. At the right local time, across DST and restarts, the owner gets a reminder card in the control chat — even in quiet hours, over the alert cap, and with the LLM down — with Done / Snooze 1 h / Tomorrow (one-off) or Pause reminder (recurring). A late reminder says it is late; a reminder too late to matter is skipped and reported, never silently lost.

## 2. Scope

**In:**
1. `agent/reminder_schemas.py`: `RecurrenceArgs` (also used by M7-AUTO), `CreateReminderArgs`.
2. `agent/tools/reminders.py`: `create_reminder` tool, its card describer and echoes.
3. `automation/reminders.py`: reminder creation (task + automation), `ReminderFire` (fire handler + `on_skipped` reporter), `undo_reminder_created`.
4. `inbox/reminder_texts.py`: the `reminder_due` renderer.
5. `telegram/control/reminder_cards.py`: the `rem` presses (Done, Snooze 1 h, Tomorrow; Pause reminder for a recurring reminder, R21).
6. Wiring lines in `_wire_automation`.
7. Closing the P2-T53 item "a failed fire job is re-deferred until its deadline (M07 revisits)" for reminders: safe, because the notification dedupe key makes a re-fire idempotent (named test).

**Out:** snooze/complete/cancel/reopen logic (M7-TASK; this unit calls `TaskControls`); pause/resume/reschedule/delete of reminder automations (M7-AUTO); the `occurrence_skipped` renderer (M7-FUP; this unit only enqueues the notification); `list_tasks` (RD-M7); the recurrence mapper itself (RRULE); a reminder about a contact sending anything to anyone (reminders only notify the owner).

## 3. Design

### 3.1 Shapes and storage (P3-schema §3.4; R12, R20)

| Shape | Arguments | Rows written (one transaction) |
|---|---|---|
| **One-off** | `at` set | A task (`kind = 'reminder'`, `title` = the text, `due_at` = the instant, `conversation_id` NULL, `created_by = 'owner'`, `refs`, `provenance`, `run_id`, `tool_call_id`) through M7-TASK's `create_owner_task`; **then** a `reminder` automation with `task_id` set, `run_at` = the instant, `rrule` NULL, `tz_policy = 'fixed'`, `ReminderRule()` (no `text`), the same `refs` and `provenance`, `confirmed_at = now`. The text lives only in `tasks.title` (R12, P3-T49) |
| **Recurring** | `recurrence` set | A `reminder` automation only: `task_id` NULL, `rrule` + `dtstart_local` from RRULE, `tz_policy = 'follow_owner'`, `ReminderRule(text=…)`, `refs`, `provenance`, `confirmed_at = now` |
| **To-do** (OD-17) | neither | A task only (`kind = 'reminder'`, `due_at` NULL); no automation, so nothing ever fires (R20, P3-T57) |

- One-offs are `fixed`: they keep their instant if the owner later changes timezone, and the echo states the timezone name. Recurring reminders are `follow_owner`: the reconciler moves them with `accounts.timezone` (`_follow_owner_timezones`) and the local time stays.
- Owner reminders and to-dos keep `conversation_id` NULL; the chat and contact they name go in `refs` only (`{"conversation_ids": […], "contact_ids": […]}`; a named chat also adds its contact). When the run is tainted, the chats and contacts it drew on (`run_origin(...).refs`) are added to `refs` too, because the text may derive from them (§3.2 step 3). This lets per-chat and per-contact deletion find them (F0-S's `purge_p3` step 4 for tasks; `purge_p2` deletes automations whose `refs` name the scope) and keeps contact-triggered triage away from them (M7-TASK §3.1).
- `automations.refs` = `tasks.refs` for the same reminder, so a deletion removes both.
- Provenance: §3.2 step 3.

### 3.2 The `create_reminder` tool

**Arguments** (`agent/reminder_schemas.py`; strict schema via `strict_schema`; validators raise `ArgumentRuleError` in fixed words, never echoing input; no `format`/`pattern` keywords):

```python
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class RecurrenceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frequency: Literal["daily", "weekdays", "weekly", "every_n_days"]
    time: str  # owner-local "HH:MM" (validated: 00:00–23:59)
    weekdays: list[Weekday]  # weekly: 1–7 distinct days; [] for every other frequency
    interval_days: int | None  # every_n_days: 2–30; null otherwise
    starts_on: str | None  # owner-local "YYYY-MM-DD" (parse_local_date); null = today

    def to_recurrence(self) -> Recurrence: ...  # RRULE's value type (§3.3)


class CreateReminderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str  # 1–400 characters after strip; the owner's own words
    at: str | None  # one-off: owner-local "YYYY-MM-DD HH:MM" (the time part is required)
    recurrence: RecurrenceArgs | None
    conversation_id: OptionalId  # optional: the chat the reminder is about (refs only)
    contact_id: OptionalId  # optional: the contact it is about (refs only)
    # model_validator: `at` and `recurrence` are never both set
```

Field descriptions tell the model: resolve "tomorrow", "in 2 hours" against the owner's local date and time; `at` and `recurrence` both null means a to-do without a time; the text is what the owner wants to be reminded of, in the owner's words.

**Definition:** `CREATE_REMINDER = ToolDefinition(name="create_reminder", capability=CapabilityClass.CONFIGURE, release_flag="tool.create_reminder", handler=create_reminder_tool, describe=describe_create_reminder, confirm_if=_is_recurring)` with `_is_recurring = lambda args: args.recurrence is not None`; `returns_managed=False`. So a recurring reminder is always a card (OD-17), a one-off or a to-do executes and is echoed, and any call in a tainted run is held for a card by the gate (RG-11). `agent/tools/reminders.py::TOOLS = (CREATE_REMINDER,)`.

**Handler** (one transaction; `now = services.clock.now()`; actor `owner` when `ctx.held`, else `agent`, P3-interfaces §13):
1. **Flag check.** A one-off or recurring reminder needs `REMINDERS_RUN` (`reminders.run`) in `ctx.services.enabled_flags`; otherwise `ToolOutcome("error", {"reason": "reminders are switched off right now, so no reminder was set"}, log_code="reminders_off")`. Without this check the occurrence would be settled `disabled` at fire time and never reported. A to-do needs no run flag.
2. **Ids.** `require_conversations` / `require_contacts` (`agent/tools/common.py`) for the given ids — another account's id raises `NotInAccountError` (RG-21). Build `refs` (§3.1).
3. **Provenance.** `origin = await run_origin(session, ctx)` with `ctx` unchanged, then `provenance = memory.items.provenance(tainted=origin.tainted, managed=origin.tainted, refs=origin.refs.as_json() if origin.tainted else None)` on both the task and the automation. When `origin.tainted`, also add `origin.refs`' chat and contact ids to the `refs` column (§3.1), so per-chat and per-contact deletion finds a reminder whose text may come from those chats. `run_origin` counts every held call as tainted (M03 N1, fail closed), so a card-confirmed reminder (every recurring one, and any call from a tainted run) is stored tainted and managed, and managed-data deletion removes it (P3-schema §7.2 step 5). Never clear `ctx.held` to get around N1; every P3 unit uses this baseline (R68). A narrower rule for `confirm_if` / `always_confirm` tools would need a contract change.
4. **Idempotent retry.** Look up this account's task and automation by `(run_id, tool_call_id)` (when both are set). Found ⇒ the same status as a fresh create would give (`scheduled` for a one-off or recurring reminder, `ok` for a to-do) with `{…ids…, "changed": False}`, no new rows and no echo.
5. **Shape-specific checks and writes** through `automation/reminders.py::create_reminder(...)` (§3.4 holds the domain function):
   - One-off: `local = parse_local_moment(at)`; `run_at = automation/schedule.py::to_utc(local, account.timezone)` (a DST-gap time resolves to the first valid instant; an ambiguous one to the first occurrence). `run_at <= now` ⇒ error "that time has already passed"; `run_at > now + 366 days` ⇒ error "a reminder can be at most a year ahead".
   - Recurring: `spec = recurrence.to_schedule(args.recurrence.to_recurrence(), timezone=account.timezone, starts_on=<starts_on or the owner-local today>)`; a `ScheduleError` or no future occurrence ⇒ error "that schedule has no future time".
   - To-do: no time checks.
6. **Audit** `reminder_created` (P3-interfaces §13): `subject_type = 'automation'`, `subject_id` = the automation id, details `{"task_id": id|null, "recurring": bool, "card": ctx.held}`; for a to-do (no automation) `subject_type = 'task'`, `subject_id` = the task id (contract gap reported).
7. **Echo** (after commit, through `agent/tools/echo.py::queue_echo`; text escaped once with `html.escape(text, quote=False)`):
   - One-off: "⏰ Reminder set for {echo_instant(run_at, tz)}: {text}" **with Undo** (P3-interfaces §12.7).
   - Recurring: "🔁 Reminder set: {text} — {describe_recurrence(rec)} ({tz}); next on {echo_instant(next_run_at, tz)}." with `undo=False` (a card-confirmed automation has no Undo; pause or delete it instead).
   - To-do: "📝 Added to your to-dos: {text}. It has no time, so I will not remind you." **with Undo**.
   The echo shows the **resolved** instant (for a DST-gap request it is the first valid time, never the time the model typed).
8. **Result (R69):** a one-off or recurring reminder returns `ToolOutcome("scheduled", {"reminder_id": automation_id, "task_id": id|null, "shape": "one_off"|"recurring", "state": "scheduled", "next": "YYYY-MM-DD HH:MM", "timezone": tz, "echo": echo.note})`; a to-do returns `ToolOutcome("ok", {"reminder_id": null, "task_id": id, "shape": "todo", "state": "open", "next": null, "timezone": tz, "echo": echo.note})`. Status `scheduled` is a NOT_RUN status (in `NOT_RUN_STATUSES`, P3-interfaces §9.3; R97): the reminder is armed and has not fired, so it is never a success status, never `ok` or done, and nothing says "reminded" (RG-20, invariant 11). The text is never in the result (it is the owner's own words, but `list_tasks` is the read path for it).

**Card describer** `describe_create_reminder(session, account_id, args) -> str` (owner-facing; the card escapes it): one-off "Remind you “{text}” on {echo_instant(to_utc(parse_local_moment(at), tz), tz)}" (the resolved instant, so a DST-gap request shows the time that will really be used; `to_utc` needs no clock); recurring "Remind you “{text}” {describe_recurrence(rec)} ({tz})"; to-do "Add “{text}” to your to-dos (no time)". A foreign chat or contact id raises `NotInAccountError` before any card. (The describer has no clock, so the card does not state the first occurrence; the notice after Confirm does.)

**`owner_request` rule text** for PR-OR10 (in the PR description): "For 'remind me …' use `create_reminder`. Resolve relative times against the owner's local date and time and pass `at` as 'YYYY-MM-DD HH:MM', or a `recurrence` for repeating reminders; pass neither for a to-do without a time. A one-off reminder is set at once — relay the echoed time exactly; a recurring one waits for the owner's confirmation; never say the owner was reminded before it fires."

### 3.3 RRULE consumed (`automation/recurrence.py`, David D2)

M7-REM consumes RRULE's pure API (`specs/RRULE.md` §6.1). P3-interfaces names the module but does not freeze these signatures; the TL should copy the agreed API into the contract before CP1:

```python
@dataclass(frozen=True, slots=True)
class Recurrence:
    frequency: Literal["daily", "weekdays", "weekly", "every_n_days"]
    time_local: time                          # whole minutes
    weekdays: tuple[Weekday, ...] = ()        # weekly only
    interval_days: int | None = None          # every_n_days only

def to_schedule(rec: Recurrence, *, timezone: str, starts_on: date) -> ScheduleSpec
    # follow_owner; rrule (FREQ=DAILY | FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR | FREQ=WEEKLY;BYDAY=… |
    # FREQ=DAILY;INTERVAL=n); dtstart_local = starts_on at time_local; validated (ScheduleError)
def describe_recurrence(rec: Recurrence) -> str        # "every weekday at 09:00"
def echo_instant(at: datetime, timezone: str) -> str   # "Thu 30 Oct 2026, 09:00 (Asia/Tashkent)"
def recurrence_from_rule(rrule: str, dtstart_local: datetime) -> Recurrence | None
    # M7-AUTO passes a non-None result to describe_recurrence; no describe_rrule API exists
```

The schedule maths itself (`next_occurrence`, `to_utc`, DST gaps and folds, `lateness`) is M05's `automation/schedule.py`, unchanged. Stored instants and dedupe epochs use UTC whole seconds; recurrence anchors remain naive local datetimes. A fold fires once at fold=0; the resolved echo includes the offset when ambiguous.

### 3.4 Domain module (`automation/reminders.py`)

Constants: `REMINDER_KIND = "reminder"`, `EVENT_REMINDER_CREATED = "reminder_created"`, `EVENT_REMINDER_FIRED = "reminder_fired"`, `EVENT_REMINDER_CANCELLED = "reminder_cancelled"`, `MAX_AHEAD = timedelta(days=366)`, `REMINDER_CARD = "rem"` (the callback kind, P3-interfaces §12.3), `OP_DONE = "dn"`, `OP_SNOOZE_HOUR = "s1"`, `OP_TOMORROW = "st"`, `OP_PAUSE = "pa"`.

**Creation** `async def create_reminder(session, *, account_id, text, run_at: datetime | None, schedule: ScheduleSpec | None, refs, provenance, run_id, tool_call_id, actor, card: bool, clock) -> CreatedReminder` (`CreatedReminder(automation_id, task_id, next_run_at, audit_id)`): writes §3.1's rows in the order task → automation (the task must exist for `task_id`; nothing else holds a lock on either new row), using `inbox.tasks.create_owner_task` and `automation.schedules.create_scheduled(kind="reminder", rule=ReminderRule(...), spec=…, clock=clock, refs=refs, task_id=…, run_id=run_id, tool_call_id=tool_call_id, confirmed_at=clock.now())`. `create_scheduled` → `repository.create` has no `provenance` parameter (`repository.py` is a never-edit file), so set `row.provenance = provenance` on the returned ORM row and flush, in the same transaction. Writes the `reminder_created` audit row (§3.2 step 6).

**Fire handler** `ReminderFire.fire_reminder(self, session, ctx: FireContext) -> None` — the contract's `automation/reminders.py::fire_reminder` (P3-interfaces §8.1). It is a method of `class ReminderFire` because it needs the injected `scheduler` and `clock`; bootstrap builds one instance in `_wire_automation` and registers the bound method (§3.8), so there is no module-level instance. The registry has already locked the occurrence, checked the account, deadlines (`max_lateness` 12 h, OD-17) and flag; it marks the occurrence `fired` in the same transaction after the handler returns. The handler **never calls the model or Telegram and never commits** (PRD §15).
1. Reload the automation account-scoped `FOR UPDATE`, then its task (if `task_id` is set) `FOR UPDATE`. Re-check account activity and automation status under that lock (the registry's `_still_runs` read was unlocked). Accept `active`, or `completed` only for the matching final occurrence; never accept `paused`, `cancelled` or `expired`. Load `ReminderRule` with `load_schedule_rule`; a `ValidationError` is handled as in step 3 (pause as invalid, audit, enqueue nothing, return; never raise, which would only re-run the fire until its deadline). For a one-off require `ctx.occurrence_at == automation.run_at`; an obsolete queued fire returns without enqueueing even if the snooze time has already passed. This serializes fire with M7-TASK and M7-AUTO; see the lock rule below.
2. **One-off** (`rrule IS NULL`):
   - `task_id` NULL, or no task row of this account (the task was deleted; FK `ON DELETE SET NULL`): set the automation `status = 'cancelled', ended_at = now, updated_at = now`, audit `reminder_cancelled` `{"reason": "task_gone"}`, enqueue nothing, return. This is the fire-time twin of F0-S's orphan step (P3-schema §7.2 step 7; R12).
   - Task not `open` (the owner completed or cancelled it): enqueue nothing; log `reminder_not_due` with ids and the code `task_closed`; return.
   - Task `snoozed_until > ctx.now`: enqueue nothing (its re-armed occurrence comes later, M7-TASK §3.1); log `reminder_not_due` code `task_snoozed`; return.
3. **Corrupt row** (the rule did not load, or a recurring rule's `ReminderRule.text` is `None`): set `status = 'paused'` (`paused_reason` stays NULL = invalid rule, P3-schema §3.5) with audit `automation_invalid` (`schedules.EVENT_AUTOMATION_INVALID`, details `{"kind": "reminder"}`); enqueue nothing; return.
4. **Enqueue** `reminder_due` with `inbox.notifications.enqueue(session, account_id=…, kind=NotificationKind.REMINDER_DUE, priority=Priority.NORMAL, dedupe_key=f"auto:{automation_id}:{epoch_of(ctx.occurrence_at)}", refs=NotificationRefs(automation_ids=(automation_id,), task_ids=(task_id,) for a one-off and () for a recurring reminder, conversation_ids=…, contact_ids=… from the automation's `refs`), clock=self.clock, scheduler=self.scheduler)` (P3-interfaces §11.2; epoch seconds). `conversation_id` and `contact_id` columns stay NULL, so delivery adds no "Open chat" or feedback buttons and alert feedback can never mute an owner reminder; the delivery table's `feedback_buttons` is also false for `reminder_due` (R82), so the rule holds even if a later change gives the renderer a `conversation_id`. The delivery class `owner_scheduled` (F0-S table) makes it immediate through quiet hours and the cap (OD-17); `notifications.deliver` still gates it (§9).
5. If `enqueue` returned an id: audit `reminder_fired` `{"occurrence_at": epoch, "late": ctx.timing is Timing.LATE}`. A `None` (the dedupe key exists) writes nothing more.

**Idempotence and the P2-T53 item.** A handler exception rolls back the whole fire transaction, so the occurrence stays `claimed` and `FIRE_RETRY` / `lost_fires` re-defer it until its 12 h deadline; each re-run starts from nothing committed. After a commit, `plan_fire` finds `fired` and returns `NONE`. The dedupe key `auto:{automation_id}:{epoch}` makes a second enqueue impossible in any order. So the M05 re-defer rule is safe for reminders; the named test proves it.

**Skip reporter** `ReminderFire.on_skipped(self, session, skipped: SkippedOccurrence) -> None` uses P3-interfaces §8.1's hook for `skipped_obsolete` and `skipped_restore`. Enqueue `occurrence_skipped` with priority `HIGH`, dedupe `skipped:{automation_id}:{epoch_of(skipped.occurrence_at)}`, automation/task/chat/contact refs and `scheduler=self.scheduler`. Do not suppress a recorded skip merely because the task later closed: OD-17 says every recorded skip is reported. Deleted sources are suppressed by the renderer. Notes `inactive`, `disabled`, `expired` and `no_handler` do not invoke this hook. M7-FUP owns the renderer; release enablement requires that renderer to be deployed so a missing renderer cannot silently consume these notices.

**Locks and transactions.** ARCHITECTURE §9.4 orders account → conversation → delegation → action and says nothing about automations, so the cross-unit automation lock rule frozen in P3-interfaces §8.1 applies as written here (R71; M7-TASK, M7-AUTO and M7-FUP follow it):
- The fire path holds occurrence → automation → task (`plan_fire` locks the occurrence; this handler locks the automation, then the task).
- An owner write (snooze, cancel, Undo, pause, reschedule, delete) locks automation → task and **never waits for an occurrence row lock while holding the automation**. If it must lock or delete ledger rows, it takes them `FOR UPDATE NOWAIT` inside a savepoint (a conflict rolls back the savepoint and returns `INVALID` with a fixed "it is running right now; try again in a minute" message) or `FOR UPDATE SKIP LOCKED` (leaving a row that is being fired to finish). Undo's "no occurrence row" check is a plain read, not a lock. Deleting an automation cascades to its ledger rows, so M7-AUTO's delete takes them `NOWAIT` first (M7-AUTO §3.2).
- `reconcile_due` locks automations `SKIP LOCKED`, so it skips a row an owner write holds for that minute.
- No transaction spans a Telegram or LLM call.

### 3.5 Undo (`automation/reminders.py::undo_reminder_created`; P3-interfaces §12.7)

`UndoHandler` shape `(session, account_id, entry, clock) -> bool`; `False` ⇒ `SUPERSEDED`; the framework writes `change_undone`.
- `details.recurring` true ⇒ `False` (no Undo is offered for a recurring reminder).
- **One-off** (`subject_type = 'automation'`): lock the automation `FOR UPDATE` (account-scoped), then its task (lock order automation → task, shared with M7-TASK and M7-AUTO). Revert only while the automation is `active`, `rrule IS NULL`, **no** `automation_occurrences` row exists for it (not yet claimed or fired), and the task is `open`. Then: automation `status = 'cancelled', ended_at = now`; task closed with `inbox.tasks.close(..., reason=OWNER_CANCELLED, actor="owner")`; audit `reminder_cancelled` `{"reason": "undo"}`; return `True`. Otherwise `False` (it already fired: "nothing was changed").
- **To-do** (`subject_type = 'task'`): only while the task is `open` with `due_at` NULL: close it with `owner_cancelled`; return `True`.

Undo **cancels** (R73, accepted): the rows stay, closed, under the normal retention (closed tasks 1 year, ended automations 1 year). A hard delete would be kinder to privacy, so this is an open item for the owner (§9).

### 3.6 The `reminder_due` renderer (`inbox/reminder_texts.py`)

`class ReminderDueRenderer` built with `clock`, `task_buttons: bool` (`TaskControls` wired) and `pause_button: bool` (`AutomationControls` wired); `async def __call__(self, session, notification, account) -> RenderedAlert | None`, registered with `notification_delivery.register_renderer(NotificationKind.REMINDER_DUE, renderer)` (P3-interfaces §11.3). It runs in the delivery's claim transaction, reads content at delivery, never calls Telegram.
1. `automation_id` = `refs.automation_ids[0]`; load it account-scoped. Missing ⇒ `None` (suppressed: "deleted source → suppressed").
2. Read the occurrence instant from `notification.dedupe_key` (`auto:{automation_id}:{epoch}`). Empty refs, malformed keys, mismatched automation ids or no matching account-scoped ledger row ⇒ `None`; never index unchecked refs.
3. Suppress paused/cancelled/expired automations. A completed one-off is valid only for its final fired occurrence. One-off: require `occurrence_at == automation.run_at` and `refs.task_ids == (automation.task_id,)`; load that owned task. Gone, not `open`, or `snoozed_until > now` ⇒ `None`. Text = `task.title`. Recurring: require a valid rule and non-NULL `ReminderRule.text`. A notification queued before a snooze, completion, cancellation, pause or deletion is re-checked here; creation-time checks are not enough. A recurring reminder's already-fired occurrence still delivers after a reschedule (it was due; the same rule as M7-SSUM §9); only a pause, cancellation or deletion suppresses it. A one-off cannot be rescheduled after it fired (M7-AUTO §3.1 status filter), so for one-offs the snooze is the case to suppress.
4. Text: `"⏰ <b>Reminder:</b> " + html.escape(text, quote=False)`; when `clock.now() - occurrence_at > schedules.GRACE`, add "Due at {HH:MM} ({tz}) — this reminder is late." Include the local date when the due date differs from today. Use delivery time, since `notifications.deliver` can delay a row created on time. Application markup is raw; each untrusted field is escaped once (P2 #133; never import `telegram.render` from the domain).
5. Buttons (P3-interfaces §12.5, R21): one-off with `task_buttons` ⇒ Done (`rem:<token>:dn`), Snooze 1 h (`s1`), Tomorrow (`st`); recurring with `pause_button` ⇒ Pause reminder (`pa`). The card is one `pending_inputs` row created here with `accounts.service.create_card(session, account_id=…, kind="rem", clock=…, ref_id=notification.id, ttl=REMINDER_CARD_TTL)` (`REMINDER_CARD_TTL = timedelta(days=7)`); callback data via `accounts.callbacks.encode` (≤ 64 bytes). No wired port ⇒ no card and no buttons.
6. Return `RenderedAlert(text, conversation_id=None, extra_buttons=buttons)`. No feedback buttons: `feedback_buttons` is false for `reminder_due` (R82; F0-R's delivery consults the column), and with no `conversation_id` delivery creates no alert card at all.

### 3.7 Card presses (`telegram/control/reminder_cards.py`)

`class ReminderCardPresses` built with `task_controls: TaskControls | None` and `automation_controls: AutomationControls | None`; `async def __call__(self, session, gateway, clock, scheduler, account, press) -> HandlerResult` (the `CallbackHandler` shape), registered under callback kind `rem`. Follows `telegram/control/monitors.py::handle_mon_press`: database work in the press transaction; `gateway.answer_callback` and the message edit after commit; no aiogram shortcut (AGENTS §5, §7).
1. `claim_card(session, account_id=account.id, token=press.token, clock=clock)`; a missing, expired, foreign or already-pressed card, a kind other than `rem`, or an unknown op ⇒ answer `CALLBACK_EXPIRED_TEXT`, `HandlerResult(False, "rem", None, note="no_longer_active")`.
2. Load the notification `card.ref_id` and automation account-scoped; missing or malformed refs ⇒ `NOT_FOUND` row of the table below. Re-check kind, shape, task linkage and occurrence as in §3.6. **Stale one-off card:** if the occurrence in the notification's dedupe key is no longer `automation.run_at` (the reminder was snoozed or moved after this card was sent), change nothing and answer with the "Out of date" row: an old card must never complete or move the re-armed reminder. A recurring reminder's `pa` is valid from any of its cards while the automation is active. Check `press.message_id` against the card's stored message id where available. A token is single-use (`claim_card` consumes it); invalid ops or unavailable ports change no domain state.
3. Ops (`now = clock.now()`; `tz = account.timezone`):
   - `dn` — `task_controls.done(session, account_id=…, task_id=…, now=now)`.
   - `s1` — `task_controls.snooze(..., until=now + 1 h, now=now)` (whole seconds).
   - `st` (R72) — `until = to_utc(datetime.combine(press_local_date + 1 day, occurrence_local_time), tz)` with `automation/schedule.py::to_utc`, where `press_local_date` is the owner-local date of `now` (the button says "Tomorrow" from the owner's point of view when pressing) and `occurrence_local_time` is the occurrence's owner-local `HH:MM`. A gap time resolves to the first valid instant, a fold to its first occurrence; the answer text shows the resolved instant. Never add a 24-hour duration: the next local day can be 23 or 25 hours away.
   - `pa` — `automation_controls.pause(session, account_id=…, automation_id=…, now=now)` (recurring only).
   M7-TASK's `snooze` moves the task's one-off reminder automation to `until` in the same transaction (M7-TASK §3.1 snooze step 4), so the reminder fires again then; this unit does not touch the automation itself.
   A one-off op without `task_id` or with `task_controls` `None`, or `pa` without `automation_controls` or on a one-off ⇒ answer "Not available right now", change nothing (P3-interfaces §4.5).
4. Outcome → answer and the reminder message's new text (the edit removes the buttons; the title or text is read in the transaction and escaped once):

   | Result | Answer | New message text |
   |---|---|---|
   | `DONE` (`dn`) | "Done" | "✅ Done: {text}" |
   | `DONE` (`s1`, `st`) | "Snoozed" | "⏰ Snoozed until {echo_instant(until, tz)}: {text}" |
   | `DONE` (`pa`) | "Paused" | "⏸️ Reminder paused: {text}. Resume it in /settings or ask me." |
   | `ALREADY` | "Nothing to change" | "It was already so; nothing changed." |
   | `INVALID` | "Not possible" | "This reminder's task is already closed; nothing changed." (`pa` on a row that is not active: "This reminder is not running; nothing changed.") |
   | `NOT_FOUND` | "No longer exists" | "This reminder no longer exists." |
   | Stale one-off card (step 2) | "Out of date" | "This reminder changed after this card was sent; nothing changed." |

   A snooze is never worded as done (RG-20). With `press.message_id` known, `gateway.edit_control_text(target, message_id, text)`; otherwise `gateway.send_text(target, text)`.

### 3.8 Wiring (`_wire_automation` in `bootstrap.py`; function-local imports only)

After M7-TASK's `task_controls` line and M7-AUTO's `automation_controls` line (until M7-AUTO merges, declare `automation_controls: AutomationControls | None = None`; M7-AUTO's PR replaces it):

```python
reminder_fire = ReminderFire(scheduler=ctx.scheduler, clock=ctx.clock)
ctx.fire_registry.register_fire_handler(
    "reminder", reminder_fire.fire_reminder, flag=REMINDERS_RUN, on_skipped=reminder_fire.on_skipped
)
ctx.notification_delivery.register_renderer(
    NotificationKind.REMINDER_DUE,
    ReminderDueRenderer(
        clock=ctx.clock,
        task_buttons=task_controls is not None,
        pause_button=automation_controls is not None,
    ),
)
# fragment: routes callbacks {"rem": ReminderCardPresses(task_controls, automation_controls)},
#           undo_handlers {EVENT_REMINDER_CREATED: undo_reminder_created}
```

The fire handler is registered whatever the flags (the registry settles `disabled` when `reminders.run` is off). No job group, reconciler or listener is needed: the scheduler's reconciler claims reminders and the notification reconciler recovers a lost delivery defer.

### 3.9 Edge cases and failure modes

| Case | Behaviour | Test |
|---|---|---|
| DST change between creation and firing (recurring, Europe/Berlin) | Fires at the same local wall time; a gap time fires at the first valid instant | `test_reminder_fires_at_local_time_across_a_dst_change` |
| One-off asked for a DST-gap time | Stored and echoed as the resolved instant | `test_a_one_off_in_a_dst_gap_echoes_the_resolved_local_time` |
| Worker down at the due time, back 30 min later | Claimed late (note `late`); the card says it is late | `test_restart_before_due_fires_late_with_note` |
| Down more than 12 h | Settled `skipped_obsolete`; `occurrence_skipped` enqueued once | `test_a_skipped_reminder_occurrence_is_reported` |
| LLM unavailable or budget exhausted | Fire and delivery need no model | `test_reminders_fire_without_the_llm` |
| Fire job re-run after a rollback, or twice | One `reminder_due` row | `test_a_refired_occurrence_enqueues_one_reminder` |
| Task deleted (data deletion) before the time | Automation cancelled (`task_gone`), no reminder | `test_a_one_off_reminder_whose_task_is_gone_is_cancelled_not_fired` |
| Task completed or snoozed before the time | No reminder at that occurrence | `test_a_closed_or_snoozed_reminder_task_gets_no_reminder_at_fire` |
| `notifications.deliver` off | The row waits `queued` and is delivered when the flag is on (M05 behaviour) | (M05 tests) |
| Quiet hours or the alert cap | Delivered at once (`owner_scheduled`) | `test_a_reminder_is_delivered_in_quiet_hours_and_over_the_cap` |
| A press after the reminder was deleted | "This reminder no longer exists." | `test_a_press_for_a_deleted_reminder_changes_nothing` |
| A press on an old one-off card after a snooze | "Out of date"; the re-armed reminder is untouched | `test_stale_card_cannot_change_a_rescheduled_reminder` |
| "Tomorrow" pressed after midnight, or before a DST change | The press's local date + 1 at the reminder's local time, resolved by `to_utc` | `test_tomorrow_is_a_calendar_day_near_midnight_and_both_dst_transitions` |
| Owner changes timezone | One-off keeps its instant; recurring follows the owner | `test_a_timezone_change_keeps_one_offs_and_moves_recurring_reminders` |
| Chat or contact data deleted | The task and the automation are deleted with it (`refs`) | `test_a_reminder_naming_a_deleted_chat_is_deleted_with_it`, `test_a_reminder_naming_a_deleted_contact_is_deleted_with_it` |
| Managed-data deletion | An untainted one-off or to-do stays; a card-confirmed or tainted one is deleted (provenance `managed`) | `test_managed_data_deletion_keeps_untainted_reminders_and_removes_managed_ones` |
| Fire and an owner change race (snooze, cancel, Undo) | No deadlock; the fire waits for the automation lock and re-checks | `test_fire_and_owner_change_do_not_deadlock` |
| Restore from backup | M05's `skip_for_restore` skips missed occurrences; the restore notice reports them | (F0-R, F0-S) |

Fail closed: a rule that does not load pauses the row (registry); a missing task cancels the reminder; any foreign id is "not found"; nothing is reported as reminded or done before it happens.

### 3.10 Privacy and logs

The text is content in registered columns (`tasks.title`, `automations.spec`, P3-schema §5); no new column. `refs` make both deletable per chat and contact; a reminder created from a tainted run carries managed provenance, so managed-data deletion removes it (P3-schema §7.2 steps 4–5). Notification rows stay content-free. Logs carry ids and codes only (`reminder_not_due`, `reminders_off`).

## 4. Files

**Owned:**

| File | Change |
|---|---|
| `src/assistant/agent/reminder_schemas.py` (new) | `RecurrenceArgs`, `CreateReminderArgs` |
| `src/assistant/agent/tools/reminders.py` (pre-created by F0-R) | the tool, describer, echoes; `TOOLS = (CREATE_REMINDER,)` |
| `src/assistant/automation/reminders.py` (new) | §3.4, §3.5 |
| `src/assistant/inbox/reminder_texts.py` (new) | §3.6 |
| `src/assistant/telegram/control/reminder_cards.py` (new) | §3.7 |
| `src/assistant/bootstrap.py` | lines inside `_wire_automation` only (§3.8) |
| the test files in §5 | new |

**Named touch points in files you do not own:** none. You call (never edit) `inbox/tasks.py` (M7-TASK), `automation/schedules.py` (`create_scheduled`, `epoch_of`, `GRACE`, `EVENT_AUTOMATION_INVALID`), `automation/schedule.py`, `automation/recurrence.py` (RRULE), `inbox/notifications.py::enqueue`, `accounts/service.py` (`create_card`, `claim_card`), `agent/tools/{common,echo,memory}.py`, `memory/items.py::provenance`.

**Never edit without an ICR** (P3-interfaces §15.2): `policy/*`; the send-path `actions/*` files; `telegram/gateway.py`, `telegram/render.py`, `telegram/ingress/*`; `conversations/service.py`; migrations, ORM models and `privacy/*`; `config.py`, `core/*`; `agent/{runtime,context,schemas}.py`, `agent/tools/executor.py`, `agent/tools/registry.py`; `automation/repository.py`, `automation/rules.py`, `automation/schedules.py` (M7-AUTO and M7-FUP hold its named touch points); `inbox/notification_types.py`, `inbox/alert_texts.py`; `memory/undo.py`; `telegram/control/{dispatcher,routes,handlers,panels}.py`.

## 5. Tests (red first)

Synthetic data only; `FakeClock` (no sleeps); `FakeLLM` that raises or records; real PostgreSQL 18; `p2_factories.seed_automation`, `p3_factories.seed_owner_task`; `FakeChannel` / `FakeTelegramSender` for delivery. Run only these files locally.

**IMPLEMENTATION_PLAN §9 S07.1 named tests assigned here** (`tests/integration/test_reminders.py`):
- `test_reminder_fires_at_local_time_across_a_dst_change` — a daily 09:00 Europe/Berlin reminder fires at 09:00 local before and after 2026-03-29 and 2026-10-25 (instants 08:00Z → 07:00Z → 08:00Z).
- `test_recurring_reminder_schedules_next_occurrence` — after a fire, `next_run_at` is the next matching local time; "every weekday" skips Saturday and Sunday.
- `test_restart_before_due_fires_late_with_note` — the clock jumps 30 min past the due time; the occurrence is claimed `late`; the card text says "this reminder is late".
- (S07.1 "snooze/complete/cancel" and "snoozed task re-surfaces" are M7-TASK's; this unit adds `test_a_snoozed_reminder_fires_again_at_snoozed_until` below.)

**RG tests added or extended:**
- `tests/integration/test_reminder_tools.py::test_rg11_a_tainted_run_cannot_create_a_reminder_without_a_tap` — in a tainted run a one-off call and a to-do call each ⇒ `awaiting_confirmation`, no task or automation; Confirm ⇒ created once, stored tainted and managed.
- `tests/integration/test_reminder_tools.py::test_rg21_create_reminder_refuses_another_accounts_chat_or_contact` — describer and handler: "not found"; nothing created.
- `tests/integration/test_reminder_cards.py::test_rg21_a_reminder_card_from_another_account_is_no_longer_active`.
- `tests/integration/test_reminder_cards.py::test_rg20_snooze_buttons_never_say_done` — `s1`/`st` answers and texts say "Snoozed".
- `tests/integration/test_reminder_tools.py::test_rg20_the_result_says_scheduled_never_done` — a one-off and a recurring reminder return status `scheduled` (never `ok`, R69); a to-do returns `ok` with `state: "open"`.
- `tests/integration/test_log_hygiene_m7rem.py::test_rg10_the_reminder_flow_logs_no_reminder_text` — canary text through create, fire, skip, render, presses and Undo; no log line or audit `details` contains it.

**The unit's own tests:**
- `tests/unit/test_reminder_args.py`: `test_create_reminder_schema_is_strict`, `test_at_and_recurrence_are_exclusive`, `test_a_one_off_needs_a_time_of_day`, `test_recurrence_fields_follow_the_frequency`, `test_text_is_one_to_400_characters`, `test_recurring_reminders_need_a_card` (`confirm_if`), `test_argument_errors_never_echo_the_input`.
- `tests/integration/test_reminders.py`:
  - `test_reminders_fire_without_the_llm` (PRD §15; `FakeLLM` raising on any call; the card is delivered)
  - `test_a_one_off_reminder_keeps_its_text_only_in_the_task` (R12: `ReminderRule.text` is `None`; `tasks.title` holds it)
  - `test_a_recurring_reminder_has_no_task_and_keeps_its_text_in_the_rule`
  - `test_a_todo_without_a_time_has_no_automation` (R20)
  - `test_owner_reminders_have_no_conversation_and_name_chats_in_refs`
  - `test_a_one_off_in_a_dst_gap_echoes_the_resolved_local_time`
  - `test_a_timezone_change_keeps_one_offs_and_moves_recurring_reminders`
  - `test_a_skipped_reminder_occurrence_is_reported` (OD-17, FR-FUP-08: dedupe `skipped:{a}:{epoch}`; two reconcile runs, one row)
  - `test_a_recorded_skip_is_reported_even_if_the_task_later_closed`
  - `test_a_refired_occurrence_enqueues_one_reminder` (P2-T53 item: a handler failure then a re-run, and a re-run after commit)
  - `test_a_one_off_reminder_whose_task_is_gone_is_cancelled_not_fired` (R12)
  - `test_a_closed_or_snoozed_reminder_task_gets_no_reminder_at_fire`
  - `test_a_snoozed_reminder_fires_again_at_snoozed_until` (with M7-TASK's `snooze`)
  - `test_a_corrupt_recurring_reminder_is_paused_as_invalid`
  - `test_undo_cancels_a_one_off_reminder_before_it_fires`
  - `test_undo_after_the_reminder_fired_changes_nothing`
  - `test_undo_cancels_a_todo`
  - `test_reminder_provenance_follows_the_run` (an untainted one-off is not managed; a held create is tainted and managed (N1); a tainted run's origin chats and contacts are in `refs`)
  - `test_a_reminder_naming_a_deleted_chat_is_deleted_with_it` (`privacy.deletion.delete_conversation_data` removes the task and the automation)
  - `test_a_reminder_naming_a_deleted_contact_is_deleted_with_it` (`delete_contact_data`; also a reminder whose only link to the contact is a tainted run's origin refs)
  - `test_managed_data_deletion_keeps_untainted_reminders_and_removes_managed_ones` (`delete_managed_data`; `delete_everything` removes all of them)
- `tests/integration/test_reminder_tools.py`:
  - `test_a_one_off_reminder_is_set_and_echoed_with_the_exact_local_time_and_undo`
  - `test_a_recurring_reminder_waits_for_confirmation_and_creates_nothing_before`
  - `test_create_reminder_is_idempotent_on_run_and_call` (one task, one automation, one echo)
  - `test_create_reminder_is_offered_only_with_its_flag` (registry filtering; `owner_request` only)
  - `test_create_reminder_refuses_when_reminders_are_off` (a to-do still works)
  - `test_semantically_invalid_reminders_are_factual_errors` (past time, over a year, a recurrence with no future time)
  - `test_create_reminder_echo_escapes_untrusted_text_once` (GW-ESC §3.3 rule 6 string as the reminder text, in the one-off, recurring and to-do echoes)
- `tests/integration/test_reminder_cards.py`:
  - `test_a_reminder_is_delivered_in_quiet_hours_and_over_the_cap` (OD-17, `owner_scheduled`)
  - `test_the_reminder_card_carries_no_feedback_buttons` (R82: the delivered reminder has only its own buttons, never Useful or Not useful, and the table says `feedback_buttons(reminder_due)` is false)
  - `test_the_one_off_card_offers_done_snooze_and_tomorrow`
  - `test_the_recurring_card_offers_only_pause` (R21, P3-T58)
  - `test_buttons_are_hidden_when_their_port_is_unwired`
  - `test_done_closes_the_task`
  - `test_snooze_and_tomorrow_rearm_the_reminder`
  - `test_tomorrow_keeps_the_local_time_across_a_dst_change`
  - `test_pause_reminder_pauses_a_recurring_reminder` (with M7-AUTO's port, or a recording fake before it merges)
  - `test_a_press_for_an_unwired_port_changes_nothing`
  - `test_a_press_for_a_deleted_reminder_changes_nothing`
  - `test_a_deleted_reminder_is_suppressed_at_delivery`
  - `test_reminder_due_renderer_escapes_untrusted_text_once` (GW-ESC §3.3 rule 6: `&`, `<b>x</b>`, a literal `&amp;`, through `telegram.render.sanitize`)
  - `test_reminder_card_press_text_escapes_untrusted_text_once` (the same string in the Done, Snoozed and Paused message edits)

Additional red-first cases in the same owned test files:
- `test_delayed_old_fire_after_snoozed_until_never_notifies` (a one-off's old claimed occurrence fires after the snooze: `occurrence_at != run_at` ⇒ nothing enqueued)
- `test_queued_notification_after_snooze_is_suppressed` (one-off) and `test_a_fired_recurring_reminder_still_delivers_after_a_reschedule_but_not_after_a_pause`
- `test_stale_card_cannot_change_a_rescheduled_reminder` ("Out of date"; the re-armed automation and the task are unchanged)
- `test_delivery_delay_adds_a_late_note` (enqueued on time, delivered after `GRACE`)
- `test_tomorrow_is_a_calendar_day_near_midnight_and_both_dst_transitions` (Europe/Berlin: a 23:30 reminder of day D pressed at 00:10 on D+1 ⇒ 23:30 on D+2; a 09:00 reminder of 2026-03-28 or 2026-10-24 pressed the same morning ⇒ 09:00 local the next day, 23 h or 25 h after the occurrence)
- `test_autumn_fold_fires_once_with_an_unambiguous_echo` (a daily 02:30 Europe/Berlin reminder fires once on 2026-10-25; the echo shows the offset)
- `test_malformed_reminder_refs_are_suppressed`
- `test_fire_and_owner_change_do_not_deadlock` (two sessions with barriers: fire vs snooze, cancel and Undo)
- `test_rg17_create_reminder_is_never_offered_to_contact_runs`

Use two sessions with explicit barriers for races, never sleeps. Every tool test checks strict schema, `CONTROL_ONLY` tier, `owner_request` only, default-off flag and a content-free run step. Concurrent duplicate creates must leave one task, one automation and one echo; resolve a unique conflict within a savepoint and reload, never continue an aborted transaction.

No mutation table: this unit is Tier 2, not Tier-1A (WORKFLOW §4.3).

**Shared lists you must update:** none by hand (`tool.create_reminder` and `reminders.run` are in F0-S's `P3_TOOL_FLAGS` / `P3_FEATURE_FLAGS`; F0-E derives the exact-set lists). If a strict-xfail QA-S7 scenario (reminders across a restart and DST) passes with this merge, tell QE1 on the unit's issue.

## 6. Acceptance criteria

1. A one-off reminder is created at once and echoed with the exact resolved local time and the timezone name, with a working Undo; a recurring one is a card and creates nothing before Confirm; a to-do creates a task and nothing that fires.
2. One-off text lives only in `tasks.title`; a recurring reminder has no task; every owner reminder has `conversation_id` NULL and names its chat and contact in `refs`.
3. Reminders fire at the right local time across DST and timezone changes, with the LLM down, in quiet hours and over the cap; late ones say so; obsolete ones are skipped and reported once.
4. A re-run fire job never produces a second reminder; a reminder whose task is gone never fires.
5. The card offers Done / Snooze 1 h / Tomorrow for a one-off and only Pause reminder for a recurring one; buttons of unwired ports are hidden; outcomes are truthful.
6. In a tainted run nothing is created without the owner's tap; foreign ids are "not found".
7. All §5 tests green in CI; `lint-imports` (no `telegram` import from `inbox/` or `automation/`) and `mypy --strict` clean.

## 7. Docs updated in the same PR

- ARCHITECTURE §8.2: the `create_reminder` row as built (shapes, inline echo vs card, the flag check, idempotency, provenance) and, in the same row, the `reminder_due` card (buttons, owner-scheduled delivery, lateness note). The `list_tasks` / `update_task` row is M7-TASK's and RD-M7's.
- PR description: the `owner_request` rule text (§3.2), the P2-T53 closure note for M07.md, the RRULE call names used, and the contract gaps.

## 8. Owner decisions

- **OD-17** (default assumed): one-off executed and echoed with Undo, recurring on a card; reminders fire through quiet hours and the cap; reminder lateness 12 h; every skip reported; to-dos allowed. If the owner wants recurring reminders inline too, drop `confirm_if` (§3.2) and give them an Undo; if reminders should wait for quiet hours, only F0-S's class table changes (`reminder_due` → `held_in_quiet`); if no to-dos, refuse `at = recurrence = null`.
- **OD-13** (strict arguments plus a card instead of `interpret_instruction`, ADR-18): the reminder spec is the tool's arguments. A different answer would add a model call before the card; this unit's storage and firing do not change.
- **Document conflict** (ARCHITECTURE §8.1 vs §8.2 vs PRD §11.2: are simple reminders confirmed inline?) is resolved by the PRD: simple reminders inline. The TL corrects ARCHITECTURE §8.1 in F0-C part 2.

## 9. Risks and notes

- **RRULE signatures are not in the contract** (P3-interfaces lists only the module). If RRULE's spec differs from §3.3, adapt the call sites; never re-implement recurrence here.
- **`occurrence_skipped` needs M7-FUP's renderer.** Until M7-FUP merges, skip notices are suppressed (fail safe). QE1's after-merge check covers both once both are on `int/p3`.
- **Depends on M7-TASK (R42).** `create_owner_task` and `TaskControls` come from M7-TASK; E1 builds it first (README §4 slot order). M7-AUTO is optional: until it merges, `automation_controls` is `None` and the recurring card shows no button.
- **Contract points** (adopted under R32/R78 where consistent with the rulings): §13 covers the task-subject `reminder_created` event and §12.7 its time-less to-do Undo; §8.1 freezes the lock rule of §3.4 (R71) and says that the handler names are bound methods of injected instances; §9 copies RRULE's API (RRULE spec §6.1); P3-schema §3.4 says `tasks.refs` names "every chat and contact the task names", and this spec also adds a tainted run's origin chats (§3.2 step 3). The provenance baseline is the `run_origin` rule for every unit (R68: a held call counts as tainted), so managed-data deletion removes every card-confirmed reminder; M7-FUP aligns. Existing contract text wins until the contract is changed; do not silently widen a frozen interface.
- **Undo cancels, not deletes** (R73). The text stays in a closed task for a year; an open item for the owner if deletion is preferred.
- **`notifications.deliver` off** delays reminders until it is on. It has been on since the M05 enable order; the live checklist (QA-P3) confirms it before enable 1.
- **"Tomorrow"** means the day after the press's owner-local date, at the reminder's local time (§3.7); the answer shows the resolved instant, so the owner sees exactly when. Owner wording feedback goes to M07.md, not a fix round.
- **GW-ESC** landed in P2 #133 and is verification-only; this unit's renderer, echoes and press texts follow its escape-once rule (§3.3 of GW-ESC) and must not merge before GW-ESC is closed (P3-interfaces §15.5 step 0). This unit sends nothing into business chats and adds no disclosure behaviour.
