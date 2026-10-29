# M7-FUP — Conditional follow-ups: `create_follow_up`, the 4th ingestion listener, the deadline handler and the follow-up renderers

| | |
|---|---|
| **Unit** | M7-FUP (S07.2 mechanics; J7 steps 3–4 except the nudge draft; R2: the `follow_up_met`, `follow_up_missed` and `occurrence_skipped` renderers moved in from M7-TXT) |
| **Team / slot** | Edvard / E3 |
| **Size** | M+ (≈ 1.25 eng-days, one fix round included; R28: the renderers are included) |
| **Depends on** | F0-S (`tasks.refs`/`provenance`/`tool_call_id`, `automations.provenance`, `NotificationKind.FOLLOW_UP_MET/FOLLOW_UP_MISSED/OCCURRENCE_SKIPPED`, the delivery-class table with its `feedback_buttons` column (R82), `FOLLOWUPS_RUN`, `FOLLOWUPS_NUDGE`, `TOOL_CREATE_FOLLOW_UP`, `NUDGE_TASK`, `nudge_queueing_lock`), F0-R (fire registry with the `FirePostponed` outcome (R95), the `feedback_buttons` card rule (R82), `FollowUpRule`, `P3Wiring.listeners`, the pre-created `agent/tools/followups.py`, `TaskControls` port), M7-TASK (`create_owner_task`, `TaskControls.reopen`; a senior unit, stack on it until it merges), GW-ESC (landed in P2 #133; no renderer merges before GW-ESC is closed). Merge order in the J7 chain: NUDGE-POL → M7-FUP → M7-NUDGE (P3-interfaces §15.5) |
| **Branch → base** | `p3/m7-fup` → `int/p3` |
| **Peer reviewer** | J3 (Claude), dispatched by the TL |
| **QA slot and lane** | QE1, before merge (follow-up races: deadline vs ingestion lag, false auto-close, the `_still_runs` race) |
| **Tier** | 1 (TL scope check) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §1.1, §4.5, §7.1, §8.1, §8.2, §9.3, §9.4, §10.2, §11, §12.3, §12.5, §13, §15; [P3-schema](../contracts/P3-schema.md) §3.4, §3.5, §7.2 steps 4, 5, 7; [GW-ESC](GW-ESC.md) §3.3; ARCHITECTURE §6.2, §7.5, §8.2, §9.4, §10.3, §12.4; PRD J7, FR-FUP-02, FR-FUP-03, FR-FUP-06, FR-FUP-08, FR-TRI-05, §15; IMPLEMENTATION_PLAN §9 S07.2, §10 (RG-11, RG-20, RG-21); decisions OD-13, OD-17, OD-18, OD-33 (deterministic matching), P3-T49, P3-T52, P3-T59, ADR-18; rulings R22 (dedupe key), R68 (provenance baseline), R69 and R97 (tool status `scheduled`, a NOT_RUN status), R71 (lock rule), R74 (listener order), R82 (no feedback buttons on the three renderers), R94 (OD-33), R95 (`FirePostponed`), R96 (nudge guidance is None) |

## 1. Goal

The owner says "remind me tomorrow at 18:00 if Sardor hasn't sent the documents". A confirmation card shows exactly what will be watched; after the tap the assistant watches that one chat, with no model call. When a matching message arrives the task closes and the owner is told, with a one-tap Reopen. When nothing arrives by the deadline the owner gets a reminder, and M7-NUDGE drafts a nudge for approval. A message that was sent before the deadline but not yet ingested never produces a false "not arrived"; one that turns up after a "not arrived" produces an "arrived after all" correction.

## 2. Scope

**In:**
1. `agent/followup_schemas.py` (new): `CreateFollowUpArgs` and its parts.
2. `agent/tools/followups.py` (pre-created by F0-R): the `create_follow_up` tool, its describer, `TOOLS = (CREATE_FOLLOW_UP,)`.
3. `automation/followups.py` (new): creation, the met path shared by the listener and the deadline handler, the deadline handler (`FollowUps.fire_deadline`), the skip reporter (`FollowUps.on_skipped`), the ingestion-lag guard (raises F0-R's `FirePostponed`), `reactivate_after_reopen`.
4. `automation/followup_listener.py` (new): `FollowUpIngestionListener`, the 4th ingestion listener.
5. `automation/schedules.py::_still_runs`: the follow-up race (named touch point, P3-interfaces §8.1).
6. `inbox/followup_texts.py` (new): the `follow_up_met`, `follow_up_missed` and `occurrence_skipped` renderers.
7. `telegram/control/followup_cards.py` (new): the Reopen press (`fup:<token>:ro`).
8. `_wire_automation` lines.
9. Tests; ARCHITECTURE §6.2, §7.5, §8.2.

**Out:**
- The nudge job, its draft and its card (M7-NUDGE). This unit only requests it (§3.6 step 8) and renders its outcome in the reminder (§3.8).
- The nudge policy path in `propose()` (NUDGE-POL).
- `TaskControls` itself, `create_owner_task`, task expiry (M7-TASK); deleting a follow-up (M7-AUTO's `delete_automation`; follow-ups cannot be paused, rescheduled or edited, M7-AUTO §3.1).
- The `reminder_due` renderer (M7-REM) and `commitment_due` (COMMIT-DUE).
- Semantic expectations ("a message about the contract"), **OD-33 (R94), default: deterministic matching only in P3**. `FollowUpRule` accepts `semantic`, but there is no follow-up evaluator in P3 (`agent/monitor_eval.py` is keyed by monitor ids and has no P3 owner), so a follow-up matches by what arrives (files or media received, a reply, a keyword description through the monitor matcher). `create_follow_up` refuses `semantic` (FUP-1). The owner can use a keyword follow-up, or a semantic monitor. IMPLEMENTATION_PLAN S07.2 says "matching description"; the owner decides in OD-33 whether to add the semantic evaluation later.
- Any send. A follow-up notifies the owner only; the only outbound path is M7-NUDGE's approval card (FR-FUP-06, OD-18).

## 3. Design

### 3.1 Storage (P3-schema §3.4, §3.5; ARCHITECTURE §10.3)

A follow-up is **one `follow_up` automation plus the task it watches**:

| Row | Values |
|---|---|
| `automations` | `kind = 'follow_up'`; `spec` = `FollowUpRule` (P3-interfaces §8.2) with `conversation_id`, `contact_id` (the chat's contact), `expected` (a `MonitorCondition`, §3.2), `on_met = 'close_and_notify'`, `on_missed` = `remind_with_draft` or `remind`, `max_lateness_minutes = 1440` (the rule default, OD-17); `run_at` = the deadline (UTC, whole seconds), `rrule` NULL, `tz_policy = 'fixed'` (an instant: the echo states it with the timezone name), `timezone` = the account's; `next_run_at` = the deadline; `task_id` = the watched task; `refs = {"conversation_ids": [c, …origin chats], "contact_ids": [k, …origin contacts]}` (the watched chat and contact plus the chats and contacts the run drew on, §3.3 step 6: NUDGE-POL checks that the follow-up names the chat; per-chat and per-contact deletion finds the row and removes it when any of them is deleted, because the description may quote them). **What is watched is `spec.conversation_id`, never `refs`**; `confirmed_at` = creation (only messages sent at or after it count; a Reopen moves it to the press, §3.10 step 5); `run_id`, `tool_call_id`; `provenance` (§3.3 step 6) |
| `tasks` (new task only) | Created through M7-TASK's `inbox.tasks.create_owner_task(kind="follow_up", title=<description>, due_at=<deadline>, refs=<same refs>, provenance=<same>, run_id, tool_call_id, actor="owner", now, conversation_id=None)`. `conversation_id` stays NULL on purpose: triage sees only tasks with the chat's `conversation_id`, so it can never retitle the owner's words or close this task (M7-TASK §3.1). The chat is in `refs` |
| `tasks` (reused task) | When the owner names an open task of that chat (`task_id`, typically triage's `contact_commitment` from J7 step 1), no new task: the automation watches it. Its title and due date are not changed |

The description (the owner's words for what is expected, 1–200 characters) is stored in `expected.description` for every kind. For a new task it is also the task title. It is content in `automations.spec` (already registered, P3-schema §5), and it is the nudge guidance M7-NUDGE may use (§3.6, M7-NUDGE §3.3). Contract note FUP-6: the monitor convention "only the fields of its kind are set" does not apply to a follow-up's `expected.description`.

The `MonitorCondition` stores only the kind's own matching fields: `contact_replied` → `contact_id` (the chat's contact); `keywords` → `terms` as given (stripped) and `normalized_terms` (`memory.search.normalize`, exactly as `agent/tools/monitors.py::to_rule` does); `media_received` → `media_kinds` (deduplicated), plus `description` in all three.

### 3.2 `create_follow_up` (ADR-18, OD-13: strict tool arguments plus a card, no `interpret_instruction`)

**Arguments** (`agent/followup_schemas.py`; strict schema via `strict_schema`; validators raise `ArgumentRuleError` in fixed words that never echo the input; no `format`/`pattern` keywords, M03 D52):

```python
FollowUpKind = Literal["contact_replied", "keywords", "media_received"]


class FollowUpExpectationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FollowUpKind  # contact_replied: any message from the contact; keywords: a message
    # containing a word or phrase; media_received: a photo/document/voice/video
    terms: list[str]  # keywords: 1–MAX_TERMS terms, each 1–MAX_TERM_CHARS chars with a letter
    # or digit (query_terms); [] for the other kinds
    media_kinds: list[MediaKind]  # media_received: at least one; [] for the other kinds


class CreateFollowUpArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: RequiredId  # the chat to watch (an id from the owner run's tools)
    task_id: OptionalId  # an open task of that chat to watch; null = create a follow-up task
    expected: FollowUpExpectationArgs
    description: str  # 1–200 chars after strip: what the owner is waiting for, owner's words
    deadline: str  # owner-local "YYYY-MM-DD HH:MM"; the time part is required
    draft_nudge: bool  # true: at the deadline, also draft a nudge for the owner's approval
```

`MAX_TERMS`, `MAX_TERM_CHARS` and `MediaKind` are imported from `agent/monitor_schemas.py`; `RequiredId` is an `int` with `check_id` (as `StopMonitorArgs.monitor_id`), `OptionalId` from `agent/tools/common.py`. Validators: the kind's own field is required and the others empty; `deadline` must match `YYYY-MM-DD HH:MM` (checked in the validator with `parse_local_moment`, refusing a date without a time). There is no `semantic` member (FUP-1).

**Definition:** `CREATE_FOLLOW_UP = ToolDefinition(name="create_follow_up", description=…, args_model=CreateFollowUpArgs, capability=CapabilityClass.CONFIGURE, handler=create_follow_up_tool, release_flag="tool.create_follow_up", always_confirm=True, describe=describe_create_follow_up)`; `returns_managed=False`. Run type `owner_request` only, tier `CONTROL_ONLY` (registry defaults). `always_confirm` makes every call a card, tainted or not (RG-11 holds by construction).

**Card describer** `describe_create_follow_up(session, account_id, args) -> str | None` (owner-facing only; the card escapes it and makes hidden characters visible):
1. Parse; `monitors.chat_facts` for the chat (None ⇒ `NotInAccountError`, "not found" before any card, RG-21); `monitors.contact_names` for the name; when `task_id` is given, the task row (account-scoped; missing ⇒ `NotInAccountError`).
2. Text: "Watch the chat with {name} until {YYYY-MM-DD HH:MM} ({tz}) for {what}. Only messages sent after you confirm count. If it arrives: close {the task “{title}” | a new task “{description}”} and tell you, with Reopen. If not: remind you at the deadline{, with a draft nudge for your approval}." `what`: `contact_replied` "a message from {name}"; `keywords` "a message from {name} mentioning “t1”, “t2”"; `media_received` "{a document | a photo | a voice message | a video | any media, joined with "or"} from {name}" (the `_MEDIA_WORDS` phrasing of `automation/monitors.py`). Description: " (you said: “{description}”)".
3. A problem the owner can see before tapping (§3.3 steps 3–4) is appended: "This follow-up cannot start: {problem}. Confirming changes nothing." (the `describe_create` pattern of `agent/tools/monitors.py`). The describer has no clock, so a past deadline is refused after the tap.

**`owner_request` rule text** for PR-OR10 (in the PR description): "For 'remind me if X hasn't happened by T' about one chat, use `create_follow_up`: the chat, what counts (a reply, words, or media), the deadline as owner-local 'YYYY-MM-DD HH:MM', and the owner's description of what they wait for; pass `task_id` when the owner means an open task of that chat from `list_tasks`. It always waits for the owner's confirmation; never say it is set before the tool returns `ok`. A follow-up only reminds the owner and may draft a nudge for approval; it never writes to anyone. For a topic without fixed words, suggest keywords or a monitor."

### 3.3 The handler (`create_follow_up_tool`)

Runs only `held` (after the owner's tap); `ctx.held` false ⇒ `ToolOutcome("not_executed", {"reason": "a follow-up starts only after the owner taps Confirm on its card"}, log_code="not_confirmed")` (fail closed, as `create_monitor`). One transaction; `now = services.clock.now()`; actor `owner` (a confirmed tool call, P3-interfaces §13).

1. **Flag.** `FOLLOWUPS_RUN` not in `ctx.services.enabled_flags` ⇒ `ToolOutcome("error", {"reason": "follow-ups are switched off right now, so none was set"}, log_code="followups_off")`. Without this check the deadline would be settled `disabled` and never reported.
2. **Account** row (`NotInAccountError` if missing).
3. **Chat.** `monitors.chat_facts` (None ⇒ `NotInAccountError`); `is_bot` or `mode` not in `monitors.WATCHED_MODES` ⇒ error "that chat is ignored: follow-ups watch only chats in Observe or Assist mode, never Ignore-mode or bot chats" (`log_code="scope_ignored"`).
4. **Task** (when `task_id` is given): `SELECT … FROM tasks WHERE account_id = :a AND id = :t` (none ⇒ `NotInAccountError`); `status <> 'open'` ⇒ error "that task is not open"; neither `conversation_id = :c` nor `refs @> {"conversation_ids": [c]}` ⇒ error "that task belongs to another chat"; an automation with `task_id = :t AND kind = 'follow_up' AND status IN ('active','paused')` exists ⇒ error "that task already has a follow-up; delete it first if you want a new one".
5. **Deadline.** `local = parse_local_moment(args.deadline)`; `deadline = automation/schedule.py::to_utc(local, account.timezone).replace(microsecond=0)` (a DST-gap time resolves to the first valid instant, a fold to the first occurrence); `deadline <= now` ⇒ error "the deadline has already passed"; `deadline > now + MAX_AHEAD` (90 days) ⇒ error "a follow-up can be at most 90 days ahead".
6. **Provenance (R68).** `origin = await run_origin(session, ctx)` with `ctx` unchanged, then `provenance = memory.items.provenance(tainted=origin.tainted, managed=origin.tainted, refs=origin.refs.as_json() if origin.tainted else None)`. This handler runs only `held`, and `run_origin` counts every held call as tainted (M03 N1, fail closed; a run whose row is gone is tainted too), so a card-confirmed follow-up is always stored `tainted: true, managed: true` with the chats its run drew on. Never clear `ctx.held` to get around this: the earlier request to do so (FUP-7) is withdrawn, and M7-REM, M7-AUTO and M6-DELEG use the same baseline. Consequences, both intended: managed-data deletion removes every follow-up and the task made for it (P3-schema §7.2 steps 4–5), and M7-NUDGE gets no owner-words guidance from a follow-up made by this tool (M7-NUDGE §3.3 step 8; reported to the TL). The origin chats' and contacts' ids are also merged into `refs` (§3.1), as M7-REM does (M7-REM §3.2 step 3).
7. **Idempotent retry:** an automation of this account with this `(run_id, tool_call_id)` (when both set; `run_id` is `ctx.run_id if ctx.run_id > 0 else None`) ⇒ `ToolOutcome("scheduled", {"follow_up_id": id, "task_id": …, "changed": False, "note": "already created"})` while that automation is `active` (any other status: `"ok"` with `"follow_up_status": status`), no new rows, no echo.
8. **Create** through `automation/followups.py::create_follow_up(session, *, account_id, conversation_id, contact_id, task_id, expectation, description, deadline, on_missed, provenance, refs, run_id, tool_call_id, timezone, clock) -> CreatedFollowUp(automation_id, task_id, audit_id)`:
   - new task (no `task_id`) through `create_owner_task` (§3.1), then the automation through `automation.schedules.create_scheduled(session, account_id=…, kind="follow_up", rule=FollowUpRule(...), spec=ScheduleSpec(timezone=…, tz_policy="fixed", run_at=deadline), clock=…, refs=…, task_id=…, run_id=…, tool_call_id=…, confirmed_at=now)`; `create_scheduled` → `repository.create` has no `provenance` parameter (a never-edit file), so set `row.provenance = provenance` on the returned row and flush (the M7-REM pattern). `create_owner_task` and `repository.create` check every id in `refs` against the account: an origin chat deleted between the run and the tap makes the tap end in the factual error "not found" and nothing is created;
   - audit `follow_up_created` (`subject_type = 'automation'`, `subject_id` = the automation, actor `owner`, details `{"task_id", "expected_kind", "new_task": bool}`; ids and enums only).
9. **Echo** after commit through `agent/tools/echo.py::queue_echo(ctx, session, audit_id, text, undo=False)` (no Undo: a card-confirmed automation is removed with `delete_automation`): "🔔 <b>Follow-up set:</b> {the describer's text without the problem line}". Every untrusted field (names, terms, description, task title) is escaped exactly once with `html.escape(value, quote=False)` before the app markup is added (GW-ESC §3.3).
10. **Result (R69):** `ToolOutcome("scheduled", {"follow_up_id": id, "task_id": id, "deadline": "YYYY-MM-DD HH:MM", "timezone": tz})`. `scheduled` is a NOT_RUN status (it has not run yet; P3-interfaces §9.3, R97), never a success status: the follow-up is armed, not reminded or arrived, and nothing the model hears says otherwise (RG-20, invariant 11). Never `ok` for a freshly armed follow-up. No names, terms or description in the result.

### 3.4 What counts as a match

A stored message counts for a follow-up when all hold:
- it is in `rule.conversation_id`, of this account, `direction = 'contact'`;
- `sent_at >= confirmed_at.replace(microsecond=0)` (Telegram's whole-second dates; the `MonitorView.covers` rule);
- `automation/monitors.py::matches(_as_monitor_rule(rule), facts)` is true, where `facts = monitors.message_facts(...)` and `_as_monitor_rule` builds a transient `MonitorRule(scope=MonitorScope(conversation_ids=(c,)), condition=rule.expected, action="alert", lifecycle="one_shot")`. M7-FUP imports the matcher and never edits it (P3-interfaces §15.1). `media_received` is deterministic (no model, PRD §15).

Owner messages, edits (`change.is_edit`), metadata-only changes and messages in Ignore-mode or bot chats never count.

### 3.5 The met path (`automation/followups.py::record_met`)

```python
async def record_met(
    session: AsyncSession,
    *,
    account_id: int,
    automation_id: int,
    message_id: int,
    sent_at: datetime,
    clock: Clock,
    scheduler: JobScheduler | None,
    nowait: bool,
) -> MetResult: ...  # MET | ALREADY_CLOSED | TASK_GONE | NOT_WATCHING
```

Shared by the listener (§3.7) and the deadline handler's re-scan (§3.6 step 5). In the caller's transaction:
1. Lock the automation: `SELECT … FROM automations WHERE id = :id AND account_id = :a AND kind = 'follow_up' FOR UPDATE` (`NOWAIT` when `nowait`). Status not `active`/`completed` ⇒ `NOT_WATCHING`.
2. `task_id` NULL ⇒ cancel the automation (`status = 'cancelled', ended_at = now, next_run_at = NULL, updated_at = now`), audit `follow_up_cancelled` `{"reason": "task_gone"}` ⇒ `TASK_GONE`.
3. Lock the task: `SELECT … FROM tasks WHERE id = :task_id AND account_id = :a FOR UPDATE` (`NOWAIT` when `nowait`). No row ⇒ as step 2. `status <> 'open'` ⇒ if the automation is `active`, complete it silently (`status = 'completed', ended_at = now, next_run_at = NULL`) ⇒ `ALREADY_CLOSED` (triage, the owner or an earlier match closed it; their own notice, if any, already went out: one closure notice per task).
4. Close the task: `UPDATE tasks SET status = 'done', closed_reason = 'follow_up_met', closed_at = :now, updated_at = :now WHERE account_id = :a AND id = :t AND status = 'open'`; audit `task_closed` (existing event, `subject_type = 'task'`, actor `system`) `{"kind": <task kind>, "reason": "follow_up_met", "automation_id": id}`. M7-FUP writes this UPDATE in its own module (P3-interfaces §15.1: it never edits `inbox/tasks.py`); the reason code `follow_up_met` is contract gap FUP-2. M7-TASK's `reopen` accepts any `done` task, so Reopen works.
5. Complete the automation: `UPDATE automations SET status = 'completed', ended_at = :now, last_run_at = :now, next_run_at = NULL, updated_at = :now WHERE id = :id AND account_id = :a` (from `active` before the deadline, or already `completed` after it). A deadline occurrence not yet claimed is then never claimed (`reconcile_due` reads only `active` rows); one already claimed is settled `inactive` by `_still_runs` (§3.9) or finds the task closed in the handler (§3.6 step 3).
6. Enqueue `follow_up_met`: `inbox.notifications.enqueue(session, account_id=…, kind=NotificationKind.FOLLOW_UP_MET, priority=Priority.HIGH, dedupe_key=f"fupmet:{automation_id}:{message_id}", refs=NotificationRefs(automation_ids=(automation_id,), task_ids=(task_id,), conversation_ids=(c,), contact_ids=(k,), message_ids=(message_id,)), clock=clock, conversation_id=c, scheduler=scheduler)`. Delivery class `held_in_quiet` (P3-interfaces §11.2, P3-T52): high ⇒ held to the end of quiet hours, never capped. The key includes the matched message, so a real closure after a Reopen notifies again (R22, P3-T59). `contact_id` is left NULL on purpose: a low-priority contact must never demote an alert the owner asked for (M05's decision R9, "the owner asked for it, never demote it", kept by construction: `NotificationDelivery._facts` reads the contact's priority only from that column). The `follow_up_met` card carries no Useful or Not useful buttons (R82, `feedback_buttons = false`), so the NULL `contact_id` can never become a mute pair.
7. Audit `follow_up_met` (`subject_type = 'automation'`, actor `system`) `{"task_id", "message_id", "after_deadline": sent_at >= automation.run_at}` ⇒ `MET`.

### 3.6 The deadline handler (`FollowUps.fire_deadline`)

`class FollowUps` is built in `_wire_automation` with `scheduler`, `clock` and `nudge: bool` (§3.12). `async def fire_deadline(self, session, ctx: FireContext) -> None`, registered as `register_fire_handler("follow_up", followups.fire_deadline, flag=FOLLOWUPS_RUN, skip_policy=SkipPolicy.SKIP_WITH_NOTE, on_skipped=followups.on_skipped)` (P3-interfaces §8.1). The registry has locked the occurrence, checked the account, `_still_runs` (§3.9), the deadlines (24 h, OD-17) and the flag, and marks the occurrence `fired` in the same transaction after the handler returns. The handler **never calls the model or Telegram and never commits** (PRD §15: follow-up deadlines are checked with the LLM down or the budget used up). Its locks follow §3.11.

1. Lock and load the automation: `SELECT … FROM automations WHERE id = :id AND account_id = :a AND kind = 'follow_up' FOR UPDATE NOWAIT` (§3.11), then its rule through `load_schedule_rule("follow_up", spec)`. The lock comes **first**, before the re-scan and the guard, and it is what makes the later steps race-free: a matching message's ingestion transaction cannot commit while the handler holds it, because that transaction's listener waits for this same lock (§3.7), and the stored message and its `inbound_updates` row are one transaction. So a message either was committed before the lock (the re-scan sees it) or is still `pending` when the guard looks (the guard waits), never in between. A rule that no longer validates ⇒ log `follow_up_rule_invalid` (ids only) and return (the reconciler pauses invalid rows, M05 behaviour). `deadline = automation.run_at`.
2. **Task gone** (`task_id` NULL, or no task row of this account): cancel the automation and audit `follow_up_cancelled` `{"reason": "task_gone"}` (the fire-time twin of P3-schema §7.2 step 7); no notice; return.
3. **Task not open** (locked `FOR UPDATE NOWAIT`): closed by the owner (`owner_done`, `owner_cancelled`) ⇒ cancel the automation (`status = 'cancelled', ended_at = now, next_run_at = NULL, updated_at = now`; an owner who reopens that task later is not watched again) and audit `follow_up_cancelled` `{"reason": "owner"}`; closed by a match, triage (`resolved_by_triage`) or expiry ⇒ log `follow_up_settled` with the code only (the automation stays `completed`; the late watch of §3.7 still applies if the task is reopened within 7 days). No notice; return (one closure notice per task: whoever closed it told the owner).
4. **Chat** (`monitors.chat_facts`): gone ⇒ as step 2. `mode` not in `WATCHED_MODES` or a bot ⇒ go to step 7 with `alert_kind = "chat_ignored"` (no re-scan, no nudge): "I could not check: the chat is set to Ignore".
5. **Re-scan** stored messages (deterministic backstop for a message the listener could not see, e.g. while `followups.run` was off): the chat's contact messages with `sent_at >= confirmed_at` (whole second), `deleted_at IS NULL`, `purged_at IS NULL`, oldest first (id order), at most `RESCAN_LIMIT = 500`; the first that matches (§3.4) ⇒ `record_met(..., nowait=True)`; return. When all 500 rows were read without a match and more rows remain, go to step 7 with `alert_kind = "unchecked"` (no nudge): a cap must never turn into "not arrived".
6. **Ingestion-lag guard** (a message sent before the deadline but not yet ingested must never produce a false "not arrived"). Caught up means all three, **read in this order**: the poller commits the stored updates first and `last_poll_ok` after, so a poll marker read first guarantees that every update that poll fetched is already visible to the pending count read second (the reverse order can pass while a poll commits between the two reads). All reads are plain reads with no row lock:
   1. `system_state[KEY_LAST_POLL_OK].at >= deadline + POLL_SETTLE` (`POLL_SETTLE = 10 s`: a poll that ended a moment after the deadline can predate a message dated just before it; read directly from `db.models.SystemState`, since `automation` may not import `telegram`);
   2. no `inbound_updates` row with `status = 'pending'` (every fetched update processed; it counts every account's rows, as the send path's `inbox_drained` does);
   3. the ingestion barrier row (`system_state` key `actions.policy_loader.KEY_INGESTION_BARRIER`, read-only import) has a non-null `caught_up_at`: the current run's poller has drained Telegram's backlog at least once since it started, so a restart after an outage cannot be mistaken for caught up after one full batch of a larger backlog. A missing row counts as not caught up (fail closed).
   Not caught up (any of the three):
   - while `ctx.now < ledger.created_at + INGESTION_GUARD` (60 min from the claim; `ledger` = this occurrence's `automation_occurrences` row for `ctx.occurrence_at`, a plain account-scoped read of the row the registry already holds): raise F0-R's `FirePostponed` (R95, FUP-3; imported from `automation/schedules.py`). `plan_fire` rolls back the handler's savepoint, leaves the occurrence `claimed`, logs `fire_postponed` and returns without a failed job; `lost_fires` re-defers it after its 2-minute grace (`FIRE_GRACE`) on the next `reconcile_schedules` run, until caught up, until the guard ends, or until the registry settles it `skipped_obsolete` past the 24 h lateness (then `on_skipped` reports it). Log `follow_up_waiting_for_ingestion` (ids only) before raising. The handler has written nothing before this point (it only holds the automation and task locks), so the rollback loses nothing;
   - past the guard: go to step 7 with `alert_kind = "unchecked"` (no nudge).
   Also `alert_kind = "unchecked"` (no nudge), whether or not the three checks passed, when an update that could hold the message was dead-lettered: an `inbound_updates` row with `status = 'failed'`, `received_at >= confirmed_at` and `received_at <= deadline + POLL_SETTLE`, and `account_id` equal to this account's or NULL (an unrouted update has no account yet). A `failed` row is not `pending`, so the pending count alone would call the inbox caught up while a message is stuck, and "not arrived" would be false; the stuck update is requeued by the owner's `inbox requeue` and a late match then corrects as in §3.8. Also `alert_kind = "unchecked"` (no nudge) when an unresolved coverage gap (`coverage_gaps.resolved_at IS NULL`) of this account, with `conversation_id` NULL or `= c`, `certainty IN ('possible','definite')` and `at_risk_from <= deadline AND at_risk_to >= confirmed_at`, overlaps the watch window: messages from then may be lost for good (TF §3.1). A `delayed` gap is not one of these (its messages arrive late; the three checks above cover it).
7. **Missed.** Enqueue `follow_up_missed`: priority `HIGH`, `dedupe_key = f"auto:{automation_id}:{epoch_of(deadline)}"`, refs `automation_ids`, `task_ids`, `conversation_ids`, `contact_ids`, `alert_kind` (§3.8: `None`, `"nudge"`, `"unchecked"` or `"chat_ignored"`), `conversation_id = c`, `contact_id` NULL (§3.5 step 6), `scheduler=self.scheduler`. Delivery class `held_in_quiet` (OD-17: follow-up deadline alerts wait for quiet hours to end). Audit `follow_up_missed` `{"task_id", "occurrence_at": epoch}` only when `enqueue` returned an id. The task stays open; the automation stays `completed` (set when its only occurrence was claimed).
8. **Nudge request** (FR-FUP-06, OD-18: an approval card only). When `rule.on_missed == "remind_with_draft"`, `self.nudge` is true and step 7 had no `alert_kind`: set `alert_kind = "nudge"` on the notification (decided before the enqueue in step 7) and defer `NUDGE_TASK` with `args={"automation_id": id, "occurrence_at": epoch_of(deadline)}`, `queueing_lock=nudge_queueing_lock(id, epoch)`, `schedule_at = now + NUDGE_START_DELAY` (60 s, so the reminder's delivery decision is made first and M7-NUDGE can follow a quiet-hours hold, M7-NUDGE §3.2) **inside the fire transaction**: `async with session.begin_nested():` defer with `connection=` the session's raw driver connection; a `None` result (the queueing lock already holds a job) raises a private exception inside the savepoint block that is caught outside it, so only the savepoint rolls back (F0-R's `DigestFireHandler` pattern, P2 F0-interfaces §6.1). The reminder and its nudge request commit together or not at all, so there is no lost-defer window and no reconciler is needed. The reminder never waits for the draft (PRD §15).

### 3.7 The 4th ingestion listener (`automation/followup_listener.py`)

`class FollowUpIngestionListener(*, scheduler: JobScheduler, clock: Clock)` implements `conversations.listeners.IngestionListener`. It runs inside the ingesting transaction, after actions, triage and monitors (F0-R fixes the order; P3-interfaces §15.3). No model call, no Telegram call, no commit; idempotent (a re-processed update is a duplicate, not a new message).

`on_message(session, change)`:
1. Return unless `change.new_message`, `change.direction is Direction.CONTACT`, not `change.metadata_only`, `change.mode in monitors.WATCHED_MODES` and `change.message_id` is set.
2. One account-scoped query for watching follow-ups of this chat: `kind = 'follow_up' AND account_id = :a AND spec @> {"conversation_id": c} AND (status = 'active' OR (status = 'completed' AND run_at > :now - LATE_WATCH))`, joined to an open task (`tasks.status = 'open'`), ordered by id. It matches the rule's own `conversation_id`, not `refs` (which also names the chats the creating run drew on). `LATE_WATCH = 7 days`: after the deadline a follow-up keeps watching while its task is open, so a late arrival can be reported. No row ⇒ return (the path of an owner without follow-ups stops at this query).
3. `monitors.message_facts(...)`; `facts.direction != 'contact'` ⇒ return.
4. For each follow-up whose rule loads, whose `conversation_id` is this chat, whose `confirmed_at` covers `facts.sent_at` and whose expectation matches (§3.4): `record_met(..., message_id=facts.message_id, sent_at=facts.sent_at, nowait=False)`.

`on_deletion`: nothing (a deleted message closes nothing and reopens nothing; a notice already sent stays true for that moment).

The conversation row is already locked by ingestion (F0 §5); `record_met` adds the automation and then the task (§3.11).

### 3.8 Renderers (`inbox/followup_texts.py`; P3-interfaces §11.3)

Registered in `_wire_automation` with `ctx.notification_delivery.register_renderer(kind, renderer)`. Each runs in the delivery's claim transaction, reads content at delivery, returns `None` when its source is gone (suppressed), never calls Telegram, and follows the escape-once rule (GW-ESC §3.3: each untrusted field — contact names, task titles, terms, the description, reminder text — through `html.escape(value, quote=False)` exactly once; app markup raw). Times are owner-local (`%a %d %b %H:%M` and the timezone name).

**`FollowUpMetRenderer(clock, task_buttons: bool)`** (`task_buttons` = `TaskControls` wired):
- Load the automation (`refs.automation_ids[0]`), the task (`refs.task_ids[0]`), the chat's contact name, and the message's `sent_at` (`refs.message_ids[0]`), all account-scoped. Automation, task or chat gone ⇒ `None`.
- Text: "✅ <b>Arrived:</b> {what} from <b>{name}</b> at {time}. I closed “{title}”." When the task is no longer closed by this follow-up (`closed_reason <> 'follow_up_met'`, e.g. reopened or closed again by the owner) the second sentence becomes "The task was closed then; it has changed since."
- Correction of an earlier "not arrived", read at delivery. When a `follow_up_missed` row with key `auto:{automation_id}:{epoch_of(run_at)}` exists in status `sent` (the owner saw it) or `uncertain` (claimed; the owner may have seen it), add one line, whatever the message's `sent_at`: if `sent_at >= run_at`, "Earlier I told you it had not arrived — it came after all." else "Earlier I told you it had not arrived — it was sent before the deadline and reached me late." (`uncertain`: "Earlier I may have told you …".) The second form covers a message the ingestion guard could not wait for (`unchecked`) or a gap; without it the owner would hold a "not arrived" that this notice silently contradicts. A `follow_up_missed` that is still `queued`, `batched` or `suppressed` was not shown, so it adds nothing; a queued one is suppressed by its own renderer now that the task is closed. When `sent_at >= run_at` and no such row exists: "It came after the deadline ({time})."
- Button: when `task_buttons` and the task is `done` with `closed_reason = 'follow_up_met'`: Reopen. The card is one `pending_inputs` row created here: `accounts.service.create_card(session, account_id=…, kind=FUP_CARD, clock=…, ref_id=notification.id, ttl=FUP_CARD_TTL)` (`FUP_CARD = "fup"`, `FUP_CARD_TTL = timedelta(days=7)`), callback `accounts.callbacks.encode(FUP_CARD, token, OP_REOPEN)` (`OP_REOPEN = "ro"`, ≤ 64 bytes).
- Return `RenderedAlert(text, conversation_id=c, extra_buttons=(reopen,) or ())` (delivery adds Open chat first and, because `feedback_buttons` is false for this kind (R82), no Useful or Not useful).

**`FollowUpMissedRenderer(clock)`:**
- Load the automation, rule, task and name. Gone ⇒ `None`. **Task no longer open ⇒ `None`** (a match or the owner closed it after the reminder was queued: "not arrived" must never follow "arrived").
- First line: "⏰ <b>Not arrived:</b> {what} from <b>{name}</b> by {deadline time}. Task: “{title}”."
- Second line by `refs.alert_kind`:

| `alert_kind` | Line |
|---|---|
| `None` | "Open the chat to follow up yourself." |
| `"chat_ignored"` | "I could not check: the chat is set to Ignore. Open it to look yourself." |
| `"unchecked"` | "I could not fully check: some messages from that time may not have reached me. If it shows up later, I will tell you." |
| `"nudge"` | The nudge state, read at delivery (below) |
| `"nudge_not_drafted"` (the second notice M7-NUDGE sends, dedupe `nudgeout:{automation_id}:{epoch}`, contract gap FUP-4) | first line replaced by "No draft nudge for “{title}”: {reason}." and "Write to {name} yourself if you still want to follow up." |

  Nudge state (content-free reads, account-scoped): the newest action with `trigger_automation_id = automation_id AND created_at >= run_at` — `awaiting_approval`/`revising` ⇒ "A draft nudge is waiting for your approval (separate card)."; `blocked` with `state_reason = 'window_closed'` ⇒ "The reply window is closed, so it cannot be sent from here; the draft is ready to copy (separate card)."; `blocked` with any other reason ⇒ "It cannot be sent from here right now; the draft is ready to copy (separate card)." (a blocked nudge can be a takeover, a missing connection or a newer contact message, and the line must not claim the window closed); `proposed`/`scheduled` (transient, never expected for a nudge) ⇒ the "drafting" line; `succeeded` ⇒ "You sent the nudge."; `approved`/`executing` ⇒ "You approved the nudge; it is being sent."; `failed`/`delivery_uncertain` ⇒ "The nudge was not confirmed as sent; open the chat to check."; `outdated`/`rejected`/`expired`/`cancelled` ⇒ "The draft nudge is no longer current." Else an audit row `nudge_not_drafted` (`subject_type = 'automation'`, `subject_id = automation_id`, `at >= run_at`) ⇒ "No draft nudge: {reason}." Else ⇒ "I'm drafting a nudge for your approval; it comes as a separate card." Reasons by `details.status`: `not_needed` "the AI found nothing to send"; `no_consent` "AI for your connected chats is off"; `over_budget` "this month's AI budget is used up"; `not_draftable` "this chat cannot be drafted for"; `stale` "the contact wrote again while I was drafting"; `refused`, `invalid`, `failed` "the AI could not write one". Never "sent" before `succeeded` (RG-20). The event name is read through the constant `NUDGE_NOT_DRAFTED_EVENT = "nudge_not_drafted"` (P3-interfaces §13); M7-NUDGE's test pins that its own constant equals it.
- Return `RenderedAlert(text, conversation_id=c)`. No feedback buttons (R82): the owner asked for this follow-up, so a "not useful" tap must not mute it.

**`OccurrenceSkippedRenderer(clock)`** (producers: M7-REM, M7-FUP, M7-SSUM; key `skipped:{automation_id}:{epoch}`):
- Automation from `refs.automation_ids[0]` (gone ⇒ `None`); the instant from the dedupe key (malformed ⇒ `None`); the ledger row `automation_occurrences (automation_id, occurrence_at)` for `note_code` (missing ⇒ `None`).
- What: `reminder` one-off "your reminder “{tasks.title}”" (task gone ⇒ `None`), recurring "your reminder “{ReminderRule.text}”"; `follow_up` "the follow-up check on the chat with {name} (“{description}”)" (task gone or closed ⇒ `None`); `scheduled_summary` "your scheduled summary"; any other kind ⇒ `None`.
- Why: `skipped_restore` "because the data was restored from a backup"; otherwise "because the assistant was not running in time (it was more than {max_lateness as hours} late)".
- Text: "⚠️ <b>Skipped:</b> {what}, due {time}, did not run {why}." For a follow-up add: "The task is still open; check the chat yourself."
- `RenderedAlert(text, conversation_id=None)`.

**`FollowUps.on_skipped(session, skipped)`** (the follow-up producer): only when the automation's task exists and is `open` (else it is noise); enqueue `occurrence_skipped`, priority `HIGH`, dedupe `skipped:{automation_id}:{epoch_of(skipped.occurrence_at)}`, refs `automation_ids`, `task_ids`, `conversation_ids`, `contact_ids`; `scheduler=self.scheduler` (FR-FUP-08: every skip is reported, OD-17).

### 3.9 The `_still_runs` race (`automation/schedules.py`, named touch point)

`_still_runs` returns true for a `completed` automation with no later occurrence (m05 `schedules.py:700`), meant for "completed by this very occurrence" (a one-off is completed when its occurrence is claimed). For a follow-up that is wrong when a match completed it after the claim. Add, at the top of `_still_runs`, one branch:

```python
if automation.kind == FOLLOW_UP_KIND and automation.task_id is not None:
    # M7-FUP: a follow-up met by a message never fires its deadline.
    closed_reason = await session.scalar(
        select(Task.closed_reason)
        .where(Task.account_id == automation.account_id)
        .where(Task.id == automation.task_id)
    )
    if closed_reason == FOLLOW_UP_MET:
        return False
```

The occurrence is then settled `inactive` (no `on_skipped`, F0-R). Every other case falls through unchanged, so the handler records the owner's and the orphan cases (§3.6 steps 2–3). The handler's locked task check is the authority; this branch only keeps a met follow-up from reaching it. `FOLLOW_UP_KIND` and `FOLLOW_UP_MET` are plain string constants defined in `schedules.py` beside the branch (no import of `automation/followups.py`, which imports `schedules.py`). Nothing else in `schedules.py` changes (M7-AUTO owns `reschedule`; never edit each other's function).

### 3.10 Reopen (`telegram/control/followup_cards.py`)

`class FollowUpCardPresses(*, task_controls: TaskControls | None)`; `async def __call__(self, session, gateway, clock, scheduler, account, press) -> HandlerResult` (the `CallbackHandler` shape), registered under callback kind `fup`. Follows `telegram/control/monitors.py::handle_mon_press`: database work in the press transaction; `gateway.answer_callback` and the message edit after commit through `db.uow.after_commit`; no aiogram shortcut (AGENTS §5, §7).
1. `task_controls` is `None` ⇒ answer "Not available right now"; nothing changes and the card is **not** claimed, so it still works once the port is wired (P3-interfaces §4.5). This check comes first on purpose.
2. `claim_card(session, account_id=account.id, token=press.token, clock=clock)`; missing, expired, foreign or pressed card, kind not `fup`, op not `ro` ⇒ answer `CALLBACK_EXPIRED_TEXT`, `HandlerResult(False, "fup", None, note="no_longer_active")`.
3. Load the notification `card.ref_id` (account-scoped; gone ⇒ "This follow-up no longer exists.") and its `automation_id`, `task_id`.
4. Lock the follow-up automation `FOR UPDATE` (account-scoped) **before** the task (§3.11). Then `task_controls.reopen(session, account_id=…, task_id=…, now=…)`.
5. On `DONE`: `followups.reactivate_after_reopen(session, account_id, automation_id, now) -> Reactivation` (`REARMED`, `LATE_WATCH`, `NOT_WATCHING`). **Every non-`NOT_WATCHING` outcome also moves the baseline: `confirmed_at = :now`.** A Reopen says the message that closed the task was the wrong one, so only messages sent from the Reopen on may close it again; without this the deadline re-scan (§3.6 step 5) would find the same wrong message, close the task again, and its `follow_up_met` would be swallowed by the same dedupe key `fupmet:{a}:{m}`, so the owner would hold a reopened task that silently closed itself. First `UPDATE automations SET status = 'active', next_run_at = run_at, ended_at = NULL, confirmed_at = :now, updated_at = :now WHERE id = :id AND account_id = :a AND kind = 'follow_up' AND status = 'completed' AND run_at > :now RETURNING id`: a row ⇒ `REARMED` (the deadline check runs again at `run_at`). Otherwise one account-scoped read of the automation: `active` with `run_at > now` ⇒ set `confirmed_at = :now` and return `REARMED`; `completed` with `run_at > now - LATE_WATCH` ⇒ set `confirmed_at = :now` (status stays `completed`) and return `LATE_WATCH` (the deadline passed; the late watch of §3.7 still reports an arrival of a message sent after the Reopen); anything else (the automation was deleted or cancelled meanwhile, paused, or its deadline is more than `LATE_WATCH` ago) ⇒ `NOT_WATCHING`. The text below must never promise a watch that no row performs (invariant 11).
6. Answers and the new message text (escaped once; the edit removes the Reopen button):

| Result | Answer | New text |
|---|---|---|
| `DONE`, `REARMED` | "Reopened" | "↩️ Reopened: {title}. I'll keep watching until {deadline time}." |
| `DONE`, `LATE_WATCH` | "Reopened" | "↩️ Reopened: {title}. The deadline has passed; I'll tell you if it arrives." |
| `DONE`, `NOT_WATCHING` | "Reopened" | "↩️ Reopened: {title}. I am no longer watching for it; check the chat yourself." |
| `ALREADY` | "Already open" | "It was already open; nothing changed." |
| `NOT_FOUND` | "No longer exists" | "This task no longer exists." |

### 3.11 Locks and transactions (ARCHITECTURE §9.4)

The cross-unit automation lock rule (frozen in P3-interfaces §8.1, R71; M7-TASK §3.1, M7-REM §3.4, M7-AUTO): the fire path holds **occurrence → automation → task**; an owner write locks **automation → task** and never waits on an occurrence row.

| Path | Locks, in order | Notes |
|---|---|---|
| Listener (`record_met`, `nowait=False`) | (ingestion: account, conversation) → follow-up automation `FOR UPDATE` → task `FOR UPDATE` | Never touches an occurrence row. It may wait for the handler's short transaction: a match that commits while the handler runs is applied right after the handler commits (the correction path of §3.8 then tells the owner) |
| Deadline handler | (registry: occurrence) → automation `FOR UPDATE NOWAIT` (the first thing the handler does, §3.6 step 1) → task `FOR UPDATE NOWAIT` | `NOWAIT` raises `LockNotAvailable` (an `OperationalError`), which `FIRE_RETRY` retries after 2 s and 4 s. This avoids a deadlock with M7-AUTO's delete (automation, then a cascade to the occurrence) and with deletion. The guard's reads (§3.6 step 6) and the ledger read take no lock |
| Reopen press | automation `FOR UPDATE` → task (`TaskControls.reopen`) → automation UPDATE | `reconcile_due` locks automations `SKIP LOCKED`, so it skips this row for that minute |
| M7-TASK `cancel` of a follow-up task | task only | Follow-up automations are never touched there |

No row lock is held across a model or Telegram call (there are none here). The handler and listener are idempotent: `record_met`'s conditional UPDATEs and the dedupe keys make a second run a no-op.

### 3.12 Wiring (`_wire_automation` in `bootstrap.py`; function-local imports only)

After M7-TASK's `task_controls` line:

```python
followups = FollowUps(scheduler=ctx.scheduler, clock=ctx.clock, nudge=False)  # M7-NUDGE flips it
ctx.fire_registry.register_fire_handler(
    "follow_up",
    followups.fire_deadline,
    flag=FOLLOWUPS_RUN,
    skip_policy=SkipPolicy.SKIP_WITH_NOTE,
    on_skipped=followups.on_skipped,
)
for kind, renderer in (
    (
        NotificationKind.FOLLOW_UP_MET,
        FollowUpMetRenderer(ctx.clock, task_buttons=task_controls is not None),
    ),
    (NotificationKind.FOLLOW_UP_MISSED, FollowUpMissedRenderer(ctx.clock)),
    (NotificationKind.OCCURRENCE_SKIPPED, OccurrenceSkippedRenderer(ctx.clock)),
):
    ctx.notification_delivery.register_renderer(kind, renderer)
# fragment: listeners += (FollowUpIngestionListener(...),) only when FOLLOWUPS_RUN is on;
#           routes callbacks {"fup": FollowUpCardPresses(task_controls=task_controls)}
```

`nudge` stays `False` in this PR; M7-NUDGE's PR sets `nudge=FOLLOWUPS_NUDGE in ctx.settings.feature_flags and ctx.llm is not None` when it wires its job group. So the reminder never promises a draft that no job will make. The tool is picked up from `agent/tools/followups.py::TOOLS` (F0-R's registry concatenation).

**Listener order (R74): actions → triage → monitors → follow-ups → memory.** F0-R appends P3 listeners after the three P2 ones, so the follow-up listener is the first P3 one; M7-MEX's memory listener (always registered, last) comes after it. The order must not depend on which of the two PRs merges first: whichever merges second places its line so that the follow-up line is above the memory line in `_wire_automation`, and `test_the_follow_up_listener_comes_after_monitors_and_before_memory` pins it.

### 3.13 Edge cases (each has a test in §5)

A match a second before the deadline; a document sent at 17:59:58 but still `pending` in `inbound_updates` at the 18:00 fire; a poll that ended a second after the deadline (within `POLL_SETTLE`); a poll that committed its updates between the guard's two reads; a poller outage across the deadline, and a restart after it whose backlog is larger than one batch (the barrier); a match after "not arrived" (correction, both for a message sent after the deadline and for one sent before it that reached the app late); a match that commits while the handler runs; a false auto-close and Reopen before the deadline (the deadline check runs again, and the wrong message is not matched again because Reopen moved `confirmed_at`) and after it (late watch only); a Reopen of a follow-up that was deleted, cancelled or past the late watch meanwhile (the text says it is no longer watched); a `failed` (dead-lettered) update in the watch window at the deadline (`unchecked`); the deadline handler and the Reopen press, or the handler and M7-AUTO's delete, in parallel (no deadlock, no wait); a second, real closure after Reopen (a second `follow_up_met`); triage closing the watched task before the deadline (no "not arrived", one notice); the owner completing the task (audit `owner`, automation cancelled, no notice); the task deleted (cancelled, `task_gone`); the chat switched to Ignore before the deadline; a coverage gap of certainty `possible` overlapping the window, and a resolved one (ignored); the re-scan cap; the worker down for more than 24 h (skipped, reported once); `followups.run` off at the deadline (`disabled`, no notice); a message from before `confirmed_at`; an owner message with a document; an edited message; another account's chat, task or automation; a semantic expectation (refused); a card press while `TaskControls` is unwired (the card is kept).

## 4. Files

**Owned (P3-interfaces §15.1):**

| File | What |
|---|---|
| `src/assistant/agent/followup_schemas.py` (new) | §3.2 |
| `src/assistant/agent/tools/followups.py` (pre-created) | §3.2, §3.3; `TOOLS = (CREATE_FOLLOW_UP,)` |
| `src/assistant/automation/followups.py` (new) | §3.1, §3.3 step 8, §3.5, §3.6, §3.8 `on_skipped`, §3.10 step 5 |
| `src/assistant/automation/followup_listener.py` (new) | §3.7 |
| `src/assistant/inbox/followup_texts.py` (new) | §3.8 |
| `src/assistant/telegram/control/followup_cards.py` (new) | §3.10 |
| Tests (new) | §5 |

**Named touch points:** `automation/schedules.py::_still_runs` only (§3.9); `bootstrap.py` inside `_wire_automation` only (§3.12); `docs/ARCHITECTURE.md` §6.2, §7.5, §8.2 (§7).

**Consumed, never edited:** `automation/monitors.py` (`matches`, `message_facts`, `chat_facts`, `contact_names`, `WATCHED_MODES`), `automation/{rules,schedule,repository}.py`, `automation/schedules.py` (`create_scheduled`, `epoch_of`, `SkipPolicy`, `FireContext`, `SkippedOccurrence`), `inbox/tasks.py` (`create_owner_task`, M7-TASK), `inbox/notifications.py::enqueue`, `accounts/service.py` (`create_card`, `claim_card`), `agent/monitor_schemas.py`, `agent/tools/{common,echo,memory}.py`, `memory/items.py::provenance`, `memory/search.py::normalize`, `core/p3_ports.py`, `db/models.py` (`SystemState`, `KEY_LAST_POLL_OK`).

**Never edit without an ICR** (§15.2): `policy/*`; the send-path `actions/` files; `telegram/gateway.py`, `telegram/ingress/*`; `conversations/service.py`; migrations and `privacy/*`; `config.py`, `core/*`; `agent/{runtime,context,schemas}.py`, `agent/tools/executor.py`, `agent/tools/registry.py`; `inbox/tasks.py` (M7-TASK); `automation/monitors.py` (M7-MACT); every other function of `automation/schedules.py`.

## 5. Tests (red first)

Real PostgreSQL 18, `FakeClock`, `FakeLLM` (asserted never called by the follow-up paths), the real fire registry and `plan_fire`, the real ingestion path with `FakeUpdateSource`, `p2_factories.seed_automation` / `p3_factories.seed_owner_task`. Files: `tests/integration/test_followups.py` (domain, handler, guard), `tests/integration/test_followup_listener.py`, `tests/integration/test_followup_tool.py`, `tests/integration/test_followup_texts.py`, `tests/integration/test_followup_cards.py`, `tests/integration/test_followup_wiring.py`, `tests/integration/test_followup_deletion.py`, `tests/integration/test_log_hygiene_m7_fup.py`. Race tests use an async hook on the session that commits a change between two statements (the `FakeLLM`-hook technique of M7-MEX §5, applied to the database), never sleeps. Every deadline test starts from a caught-up inbox that the module's own helper `seed_caught_up` writes (`last_poll_ok` at least `POLL_SETTLE` after the deadline, no `pending` update, the ingestion barrier set); the guard tests then break exactly one of the three conditions.

**Named in IMPLEMENTATION_PLAN S07.2 and the unit table (README §3):**
- `test_follow_up_condition_met_closes_and_notifies` ("both outcomes", part 1): a matching document before the deadline → task `done/follow_up_met`, automation `completed`, one `follow_up_met` (high, `fupmet:{a}:{m}`), audits `task_closed` and `follow_up_met`; no `follow_up_missed` at the deadline.
- `test_follow_up_missed_reminds_at_the_deadline` ("both outcomes", part 2): nothing arrives → one `follow_up_missed` (`auto:{a}:{epoch}`), the task stays open; with `nudge=True` and `remind_with_draft`, `alert_kind = "nudge"` and one `followups.nudge` job deferred in the same transaction (a rolled-back fire leaves neither).
- `test_condition_met_just_before_deadline`: the message dated 17:59:59 is ingested at 17:59:59.5 and the fire runs at 18:00:30 → met, never missed.
- `test_late_ingest_gives_no_false_missed`: the message is dated 17:59:58 but its update is still `pending` at the fire → the handler raises `FirePostponed`, the occurrence stays `claimed`, the job does not fail (R95), no notice; after processing and `lost_fires`, the next fire finds the task closed by the listener → no "not arrived" (the closure notice is `follow_up_met` with no correction line, since no missed notice was ever sent).
- `test_reopen_after_a_false_auto_close`: met by the wrong photo at 10:00 → Reopen at 10:05 → task `open`, automation `active` with `next_run_at = run_at` and `confirmed_at` = the press → at the deadline `follow_up_missed` (the re-scan does not find the wrong photo again, so the task is not closed a second time and no `follow_up_met` is swallowed by a repeated key).
- `test_media_received_condition_matches_document_deterministically` (J7 step 3): a `document` message matches `media_kinds=("document",)`; a photo does not; a location is not media; zero LLM calls.
- `test_create_follow_up_requires_confirmation` and `test_rg11_create_follow_up_in_a_tainted_run_is_held_and_saved_tainted`: untainted and tainted runs both hold for a card; before the tap nothing exists and the model hears `awaiting_confirmation`; after the tap the provenance is `tainted: true, managed: true` for both (R68: `run_origin` counts every held call as tainted), with the origin chats kept in `provenance.refs` and merged into the automation's and the new task's `refs`.
- `test_the_follow_up_tool_returns_scheduled_never_ok` (R69, RG-20): the first call, the idempotent retry of an active follow-up and the card-confirmed apply report status `scheduled`; the status is in `NOT_RUN_STATUSES`; no result text says "reminded", "arrived" or "done".
- `test_rg21_create_follow_up_refuses_another_accounts_chat_and_task` (before any card: "not found"); `test_rg21_the_listener_and_handler_never_touch_another_account` (a message in account B's chat never closes account A's follow-up; a fire for A naming B's task id cancels nothing of B's).
- `test_rg20_follow_up_texts_never_report_pending_work_as_done`: a missed reminder with a pending nudge says "drafting", with an `awaiting_approval` card says "waiting for your approval", with a `blocked/window_closed` card says the window is closed and the draft is ready to copy, with any other `blocked` reason only that it cannot be sent from here and the draft is ready to copy; "sent" appears only for `succeeded`.

**Further tests:**
- Race and closure: `test_the_still_runs_race_a_match_after_the_claim_never_reports_missed` (the occurrence is claimed, then a match closes the task, then the fire runs → settled `inactive`); `test_triage_closing_the_tracked_task_completes_the_follow_up` (triage resolves the watched commitment → `TASK_CLOSED` once, no `follow_up_met`, no `follow_up_missed`); `test_one_closure_notice_per_task` (the follow-up closes first; triage's later resolution sends no `TASK_CLOSED`); `test_a_second_closure_after_reopen_notifies_again` (R22: two `follow_up_met` rows with different keys).
- Late arrival: `test_a_match_after_a_missed_notice_sends_an_arrived_after_all_correction` (after_deadline true; the met text says it came after all); `test_a_late_ingested_match_sent_before_the_deadline_also_corrects_an_earlier_not_arrived` (an `unchecked` reminder was sent; the message dated before the deadline is ingested afterwards: the met text says it was sent before the deadline and reached the app late); `test_an_uncertain_missed_notice_gets_a_may_have_told_you_correction`; `test_no_correction_line_when_the_missed_notice_was_never_shown` (queued, batched or suppressed); `test_the_late_watch_ends_after_seven_days`.
- Postponement (R95): `test_a_postponed_fire_is_quiet_and_the_occurrence_stays_claimed` (the guard raises `FirePostponed`; `plan_fire` returns `POSTPONED`, the job succeeds, no `automation_fired` line, no `follow_up_missed`); `test_a_postponed_fire_is_redeferred_by_lost_fires_until_caught_up` (the second fire after catch-up decides); `test_no_followup_path_raises_a_private_exception_for_waiting` (the old `DeadlineNotSettled` does not exist).
- Feedback buttons (R82): `test_follow_up_cards_carry_open_chat_but_no_feedback_buttons` (`follow_up_met`, `follow_up_missed` and `occurrence_skipped` cards: Open chat and the renderer's own buttons only).
- Guard: `test_a_poller_outage_across_the_deadline_waits_then_decides` (`last_poll_ok` before the deadline → waits; after catch-up → decides); `test_the_guard_reads_the_poll_marker_before_the_pending_count` (a hook commits a poll's stored updates and its `last_poll_ok` between the two reads: the guard never passes on the stale count); `test_a_poll_within_the_settle_margin_of_the_deadline_does_not_count` (ended 2 s after the deadline → waits; 11 s → passes); `test_a_restart_after_an_outage_waits_until_the_ingestion_barrier_is_set` (`caught_up_at` null or the key missing → waits; set → decides); `test_past_the_ingestion_guard_the_reminder_says_it_could_not_fully_check` (`alert_kind = "unchecked"`, no nudge); `test_a_possible_coverage_gap_in_the_window_gives_an_unchecked_reminder` and `test_a_resolved_or_delayed_gap_does_not` (also a gap outside `[confirmed_at, deadline]`); `test_a_failed_update_in_the_watch_window_gives_an_unchecked_reminder` (a dead-lettered `inbound_updates` row received inside `[confirmed_at, deadline + POLL_SETTLE]`, with this account's id or NULL, makes the reminder `unchecked` even though nothing is `pending`; a failed row of another account or outside the window does not); `test_a_match_between_the_rescan_and_the_guard_cannot_commit` (the race hook tries to ingest a matching message after the lock is taken: its transaction blocks on the automation lock until the fire ends, so the guard never passes on a stale view); `test_the_rescan_cap_without_a_match_gives_an_unchecked_reminder` (cap lowered by monkeypatching `RESCAN_LIMIT`); `test_a_match_committed_while_the_handler_runs_is_applied_after_it_and_corrects` (the listener waits on the automation lock; after the handler's commit it closes the task and sends `follow_up_met` with the correction).
- Task and chat states: `test_a_follow_up_whose_task_is_gone_is_cancelled_at_its_deadline` (`follow_up_cancelled{task_gone}`); `test_the_owner_completing_the_task_ends_the_follow_up_silently` (`follow_up_cancelled{owner}`, automation `cancelled`, no notice); `test_follow_up_on_a_chat_switched_to_ignore_reminds_with_a_cannot_check_note`.
- Skips: `test_a_skipped_follow_up_deadline_is_reported_once` (worker down > 24 h: `skipped_obsolete`, one `occurrence_skipped`, two reconcile runs); `test_followups_run_off_at_the_deadline_settles_disabled_without_notice`; `test_follow_up_deadlines_fire_with_the_llm_down` (FakeLLM raising; the reminder is enqueued).
- Listener: `test_only_contact_messages_after_confirmation_count` (an earlier message, an owner document and an edit do not count); `test_the_listener_does_nothing_for_an_owner_without_follow_ups` (one query); `test_a_keyword_follow_up_matches_normalised_whole_words`; `test_a_follow_up_is_matched_by_its_own_chat_not_by_its_origin_refs` (a follow-up whose `refs` also name the chat its run drew on is never closed by a message in that other chat).
- Deletion (`test_followup_deletion.py`, calling F0-S's deletion functions, never editing them): `test_a_follow_up_and_its_task_go_with_their_chat_their_contact_and_their_origin_chats`; `test_managed_data_deletion_removes_every_card_confirmed_follow_up_and_its_own_task` (R68: every one is managed) while a watched triage task stays under its own rules; `test_delete_everything_removes_follow_ups`.
- Wiring (`test_followup_wiring.py`): `test_followups_are_wired_only_with_their_flag` (the listener only with `followups.run`; the three renderers, the `fup` route and the `follow_up` fire handler registered; with every P3 flag off nothing of this unit is registered); `test_the_follow_up_listener_comes_after_monitors_and_before_memory` (R74).
- Tool: `test_create_follow_up_creates_a_task_and_an_automation_once` (idempotent retry: same rows, `changed: False`, no second echo); `test_create_follow_up_reuses_an_open_task_of_the_chat`; `test_semantically_invalid_follow_ups_are_factual_errors` (past deadline, > 90 days, task of another chat, closed task, task already watched, ignored chat, bot chat); `test_create_follow_up_refuses_while_followups_run_is_off`; `test_create_follow_up_is_offered_only_with_its_flag` (registry filtering); `test_follow_up_rule_persisted_with_schema_version` (`schema_version = 1`, `expected.description` stored, `refs` and `provenance` set); `test_a_dst_gap_deadline_resolves_to_the_first_valid_instant` (Europe/Berlin spring-forward day).
- Renderers: `test_follow_up_met_renderer_escapes_untrusted_text_once`, `test_follow_up_missed_renderer_escapes_untrusted_text_once`, `test_occurrence_skipped_renderer_escapes_untrusted_text_once` (GW-ESC §3.3 rule 6, exactly: a name and a title containing `&`, `<b>x</b>`, `<a href="https://example.invalid/">y</a>` and a literal `&amp;`, through `telegram.render.sanitize`, asserting the rule's `&lt;b&gt;x&lt;/b&gt;`, `&lt;a href=` and `&amp;amp;` once each and no raw `<b>` or `<a`); `test_a_missed_reminder_for_a_closed_task_is_suppressed`; `test_occurrence_skipped_renders_for_reminders_follow_ups_and_summaries` (each kind; `skipped_restore` and `skipped_obsolete` wording; a gone source suppresses); `test_follow_up_met_shows_reopen_only_when_task_controls_are_wired`.
- Locks and parallel paths (R71): `test_the_handler_takes_the_automation_then_the_task_with_nowait_and_never_waits` (another session holds `FOR UPDATE` on the automation, then on the task: the fire raises `LockNotAvailable` at once and `FIRE_RETRY` retries; after release it settles once); `test_the_listener_waits_for_the_handler_and_never_touches_an_occurrence_row` (the listener's statements are scanned for `automation_occurrences`; it blocks on the automation lock, then closes); `test_a_reopen_press_and_a_fire_in_parallel_never_deadlock`; `test_an_owner_delete_of_the_follow_up_while_the_handler_runs_is_busy_or_after_it` (M7-AUTO's delete returns `busy` or waits for the commit, the handler never deadlocks); `test_no_follow_up_path_sends_or_calls_the_model` (tool, listener, handler, `on_skipped`, renderers and the press: zero `FakeTelegramSender` business calls, zero `FakeLLM` calls; the only enqueued rows are notifications and the nudge job).
- Cards: `test_reopen_press_reopens_and_rearms_before_the_deadline` (also asserts `confirmed_at` moved to the press); `test_reopen_after_the_deadline_keeps_only_the_late_watch` (a message sent before the press never reports; one sent after does); `test_reopen_of_a_follow_up_that_is_no_longer_watched_says_so` (the automation deleted, cancelled, or past `LATE_WATCH`: the text says "no longer watching", never "I'll tell you"); `test_rg21_a_fup_card_from_another_account_is_no_longer_active`; `test_reopen_without_task_controls_changes_nothing_and_keeps_the_card` (the card is still claimable once the port is wired).

**Log hygiene** (`tests/integration/test_log_hygiene_m7_fup.py`): `test_follow_up_paths_log_ids_and_codes_only` — canary text in the description, terms, task title, contact name and message; the tool, listener, handler, renderers and press log no canary.

**Scenarios:** when a QA-S7 scenario marked `awaiting M7-FUP` turns green with this PR, tell QE1 on the unit's issue; QE1 removes the strict-xfail marker in a commit on this PR's branch, because a strict XPASS would turn this PR red (R47; P3-interfaces §15.1). Do not edit `tests/scenarios/m07/**` yourself.

Run locally: these files plus F0-R's fire-registry tests and `tests/integration/test_automation_fire.py`; ruff, format, mypy on `src/assistant/{automation,inbox,agent,telegram/control}`; `lint-imports`.

## 6. Acceptance criteria

1. `create_follow_up` is always a card; after the tap it creates one automation and at most one task, idempotently; foreign ids are "not found" before any card; `semantic` is refused.
2. A matching contact message (sent at or after confirmation) closes the watched task and sends one `follow_up_met` with Reopen, with no model call; a closure after Reopen notifies again.
3. At the deadline, a follow-up whose task is still open and whose ingestion has caught up sends one `follow_up_missed`; a message sent before the deadline but ingested late never produces one; past the 60 min guard or with a lost-message gap the reminder says it could not fully check.
4. A late arrival after "not arrived" sends the "arrived after all" correction; "not arrived" is never delivered after the task closed.
5. Triage or the owner closing the task, the task's deletion, and the `_still_runs` race all end the follow-up without a false "not arrived"; each closure is told once.
6. The nudge is requested only when `remind_with_draft`, the nudge flag is on and M7-NUDGE is wired, in the same transaction as the reminder; no path here sends.
7. The three renderers escape every untrusted field once and suppress when the source is gone; every skip of a follow-up deadline is reported once.
8. Every named test in §5 passes in CI; RG-11, RG-20, RG-21 extended; no RG test weakened.
9. ARCHITECTURE §6.2, §7.5, §8.2 updated.

## 7. Docs updated in the same PR

- **ARCHITECTURE §6.2:** what the follow-up listener does (matching, `record_met`, the late watch). The order sentence "actions → triage → monitors → follow-ups → memory; the memory listener is always registered" is F0-C part 2's (R74); this PR does not write it, and `test_the_follow_up_listener_comes_after_monitors_and_before_memory` pins the order.
- **ARCHITECTURE §7.5:** `FollowUpSpec` as built (ADR-18): the `create_follow_up` strict arguments, the persisted `FollowUpRule` with `expected` (deterministic kinds only in P3), `expected.description`, `on_met = close_and_notify`, `on_missed`; the task-plus-automation mapping; `confirmed_at`; the deadline as `run_at` with `tz_policy = 'fixed'`; replaces the `interpret_instruction` sentence for follow-ups.
- **ARCHITECTURE §8.2:** the `create_follow_up` row (configure, `always_confirm`, flag `tool.create_follow_up`, the result statuses).
- The PR description carries the `owner_request` rule text (§3.2), the contract gaps (§9) and the open items for M07.md.

## 8. Owner decisions

- **OD-18:** auto-close tells the owner and offers Reopen; nudges always need approval. If the owner wanted silent auto-close, `on_met` would gain `close_silent` (the rule already allows only `close_and_notify`, so a rule change) and §3.5 step 6 would be skipped for it.
- **OD-17:** follow-up deadline alerts wait for quiet hours (held_in_quiet) and the follow-up lateness is 24 h. A different answer changes only the delivery-class row (F0-S) or the `FollowUpRule` default (F0-R).
- **OD-33 (R94):** follow-ups match deterministically in P3 (default). If the owner wants "matching description" (S07.2) as a semantic check, a later unit generalises `agent/monitor_eval.py` (`evaluate_monitor`) to follow-ups; `create_follow_up` would then accept `semantic` (§3.2) and the listener a model-backed match. Nothing else here changes.
- **OD-13 / ADR-18:** strict arguments plus a card. If the owner preferred an `interpret_instruction` run, §3.2 would be replaced by a model call that fills `CreateFollowUpArgs`; the card and everything after it stay.

## 9. Risks and notes

- **Contract gaps and their rulings** (the contract adopts this spec's more precise text, R32 and R78, unless noted):
  - **FUP-1 (R94, owner question OD-33):** `FollowUpRule` accepts `semantic`, but P3 has no follow-up evaluator; this unit refuses it at creation, so matching is deterministic by default. IMPLEMENTATION_PLAN S07.2 says "matching description", so this narrows the plan's wording until the owner answers OD-33 (a semantic monitor stays available meanwhile).
  - **FUP-2:** the task `closed_reason` `follow_up_met` and the `task_closed` reason are not in P3-interfaces §13; M7-FUP writes the closing UPDATE in its own module. Contract adopts (R32).
  - **FUP-3 (ruled, R95):** F0-R adds the quiet "not yet" outcome `FirePostponed` (§3.1 step 7 of F0-R: savepoint rollback, info log, occurrence left `claimed`, no failed job, `lost_fires` re-defers). The ingestion guard raises it. M7-FUP depends on F0-R for it.
  - **FUP-4:** the `follow_up_missed` `refs.alert_kind` values (`nudge`, `unchecked`, `chat_ignored`, `nudge_not_drafted`; the field itself exists since M05 and R60 names it for escalations) and M7-NUDGE's second row with dedupe `nudgeout:{automation_id}:{epoch}`. Contract §11.2 adopts (R32).
  - **FUP-6:** `expected.description` is stored for every kind. Contract adopts (R32).
  - **FUP-7:** **withdrawn.** Provenance uses the `run_origin` baseline with `held` unchanged (R68); M7-REM, M7-AUTO and M6-DELEG do the same.
  - **FUP-8:** a Reopen moves `automations.confirmed_at` to the press (§3.10 step 5), so the contract's "only messages sent at or after `automations.confirmed_at` count" also covers Reopen. Contract adopts (R32).
  - **FUP-9 (ruled, R82):** the delivery table's `feedback_buttons` column is false for `follow_up_met`, `follow_up_missed` and `occurrence_skipped`, and F0-R's delivery omits Useful and Not useful for them. The NULL `contact_id` therefore never forms a mute pair for these kinds.
  - Strict-xfail markers: R47 settled it. QE1 removes the marker in a commit on this branch; this spec says so in §5.
- **Nudge guidance (R96):** because card-confirmed follow-ups are stored tainted (R68), M7-NUDGE's guidance step yields `None` for every follow-up this tool creates, so production nudges are generic or "not needed". Open item: revisit after the trial.
- **Guard cost:** with a healthy poller, a fire right after the deadline often finds `last_poll_ok` from before it, so the reminder arrives about 2–3 minutes after the deadline. Correctness over speed; the guard never waits more than 60 minutes from the claim.
- **Busy inbox:** `pending` counts every account's rows (a pending row has no account yet). Single owner today; a multi-user inbox may delay reminders slightly, never falsify them.
- **Tasks without `conversation_id`:** a new follow-up task is invisible to triage on purpose; a reused triage task can still be resolved by triage, which then tells the owner itself.
- **Deleting a follow-up** is M7-AUTO's (it also cancels a task made for the follow-up and leaves a watched triage task open).
