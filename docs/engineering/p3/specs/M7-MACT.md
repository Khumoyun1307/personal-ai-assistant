# M7-MACT — Monitor actions `create_task` and `digest`

| | |
|---|---|
| **Unit** | M7-MACT (the monitor actions P2-T27 deferred to M07; this unit owns the dispatch in `automation/monitors.py`) |
| **Team / slot** | Edvard / E3 |
| **Size** | S (≈ 0.5 eng-day, one fix round included) |
| **Depends on** | F0-R (`automation/rules.py` after its rule-model change, so this unit's one-line edit comes second), F0-S (`MONITORS_P3_ACTIONS`, `tasks.refs`/`provenance`), M7-TASK (owner to-do semantics: an open owner task of kind `reminder` with no due time, M7-TASK's `update_task` and expiry rules; and its `create_owner_task(source="monitor", automation_id=…)`, which this unit calls to write the to-do and its one `task_created` row, §3.3, R83) |
| **Branch → base** | `p3/m7-mact` → `int/p3` |
| **Peer reviewer** | A Josh engineer (Claude) picked by the TL |
| **QA slot and lane** | QE1, after merge (on `int/p3`, before R1) |
| **Tier** | 2 (TL scope check) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §8.2, §9.4, §10.2, §13, §15; [P3-schema](../contracts/P3-schema.md) §3.4, §7.2 steps 4–5; [GW-ESC](GW-ESC.md) §3.3; ARCHITECTURE §7.5 (`MonitorSpec` as built), §8.2, §10.3, §17; PRD FR-TRI-06, FR-FUP-06; IMPLEMENTATION_PLAN §9 S07.3 (gap: the actions are not listed there; this unit is the P3 plan's addition for P2-T27's deferral, README §3); decisions OD-17 (time-less to-dos, R20), OD-19, P2-T27; rulings R83 (dispatch-time flag check authoritative; the `source` keyword; MACT-4 and the monitor-provenance read accepted) |

## 1. Goal

A monitor can do two more things when it matches: **`create_task`** adds a to-do the owner can find in their tasks, one per chat (and alerts as `alert` does), and **`digest`** lists the match in the next daily digest instead of interrupting. Both are deterministic: no model call beyond the existing semantic evaluation, no send. Rules stored by M05 keep loading and behave exactly as before. The drafted-reply action is not built (OD-19).

## 2. Scope

**In:**
1. `automation/rules.py`: widen `MonitorRule.action` to `Literal["alert", "alert_with_summary", "create_task", "digest"]` (named touch point: that line only).
2. `automation/monitors.py`: the dispatch in `record_match` (with the dispatch-time flag check, §3.2), the monitor's to-do (`_task_for_match`, which calls M7-TASK's `create_owner_task`), the words in `describe`, the added-task line in `MonitorMatchRenderer`.
3. `agent/monitor_schemas.py`: `CreateMonitorArgs.action` widened, the field description, one validator.
4. `agent/tools/monitors.py`: the `monitors.p3_actions` gate in `create_monitor_tool`; docstrings; the `create_monitor` description.
5. The flag's path to `record_match` (R83): a `p3_actions: bool = False` keyword on `MonitorIngestionListener` (`automation/monitor_trigger.py`) and a `p3_actions: bool = False` field on `MonitorEvalConfig` (`agent/monitor_eval.py`), each passed through to `record_match`, and the two lines in the M05 block of `bootstrap.py` that build them (named touch points, §4).
6. Tests; ARCHITECTURE §7.5 and the §8.2 `create_monitor` row.

**Out:**
- A drafted-reply monitor action (OD-19: dropped for Personal v1).
- A richer digest line per monitor: the digest lists batched alerts as a count per kind (`telegram/control/digest.py::_ALERT_LABELS`, M05, no P3 owner); changing it is out of scope (§9).
- `list_automations` (RD-M7; it already shows monitors and their `action`), pausing or deleting monitors (M7-AUTO), the M05 tools' other behaviour.
- The monitor matcher and the evaluation run (unchanged; M7-FUP imports the matcher).

## 3. Design

### 3.1 The rule (`automation/rules.py`; P3-interfaces §8.2)

```python
action: Literal["alert", "alert_with_summary", "create_task", "digest"]
```

A widened `Literal` is a backward-compatible change to a persisted rule (AGENTS §12; ARCHITECTURE §7.5): every stored M05 rule (`alert` or `alert_with_summary`, `schema_version = 1`) still validates, so `schema_version` stays 1. No other line of the file changes (F0-R owns the rule models; P3-interfaces §15.1).

Rollback note (for the PR's data section): if code without this change ever ran against a row with a new action, `parse_rule` would return `None` and `_pause_invalid` would pause the monitor with `automation_invalid` (fail closed, M05 behaviour).

### 3.2 The dispatch (`automation/monitors.py::record_match`)

Constants: `ACTION_ALERT = "alert"`, `ACTION_ALERT_WITH_SUMMARY = "alert_with_summary"`, `ACTION_CREATE_TASK = "create_task"`, `ACTION_DIGEST = "digest"`, `P3_ACTIONS = frozenset({ACTION_CREATE_TASK, ACTION_DIGEST})`.

`record_match` keeps its signature apart from one new keyword, `p3_actions: bool = False` (R83: fail closed by default), and its first steps (the one-shot completion or the standing touch, both conditional, so a monitor still fires once per match). **The flag check at dispatch is authoritative (R83):** the effective action is `view.rule.action`, except that an action in `P3_ACTIONS` with `p3_actions` false is dispatched as `ACTION_ALERT` (the M05 behaviour: nothing new happens while the flag is off, and a match is never lost); `record_match` then logs `monitor_p3_action_off` (monitor id and the code only). The callers pass `p3_actions` from `MONITORS_P3_ACTIONS in settings.feature_flags` (§3.2 "Flag" below). Then, by the effective action:

| Action | Notification | Extra |
|---|---|---|
| `alert`, `alert_with_summary` | `MONITOR_MATCH`, priority `HIGH`, `bypass_quiet_hours` from the rule — **unchanged** | — |
| `digest` | `MONITOR_MATCH`, priority **`LOW`**, `bypass_quiet_hours=False` | none. `decide_delivery` batches a low alert (`quiet_by_default`); a VIP contact lifts it only to `normal`, still batched; `monitor_match` is a `standard` kind (P3-interfaces §11.2). So the match never interrupts and the next digest counts it under "Other alerts" |
| `create_task` | `MONITOR_MATCH`, priority `HIGH`, `bypass_quiet_hours` from the rule, `refs.task_ids = (task_id,)` | The monitor's to-do (§3.3), created in the same transaction |

For `create_task` the order is (inside `async with session.begin_nested():`):
1. If a notification with this account and `dedupe_key = f"monitor:{automation_id}:{newest}"` already exists ⇒ return `None` (this message already matched; nothing new).
2. `task_id = await _task_for_match(...)` (§3.3).
3. `enqueue(...)` as above. A `None` result (a race on the dedupe key, not expected: both callers hold the conversation row lock — the ingestion transaction and the evaluation's store transaction) raises a private exception inside the savepoint block, caught outside it, so the task insert rolls back with it and `record_match` returns `None`.

The `monitor_matched` audit row gains `"action"` (the effective action) and, for `create_task`, `"task_id"` (ids and enums only; backward-compatible). Everything else (`monitor_completed`, the log line) is unchanged.

**Callers.** `automation/monitor_trigger.py` (deterministic matches at ingestion) and `agent/monitor_eval.py` (semantic verdicts) both call `record_match`, so both paths dispatch the new actions through it. Each gets one small named edit so the flag reaches it (R83, §4): `MonitorIngestionListener.__init__` takes `p3_actions: bool = False`, keeps it, and passes it in its `record_match` call; `MonitorEvalConfig` gains `p3_actions: bool = False` and `MonitorEvaluator` passes `self._config.p3_actions` in its call. The M05 block of `bootstrap.py` that builds them passes `p3_actions=MONITORS_P3_ACTIONS in settings.feature_flags` (function-local import of the constant from `core/flags.py`; the module import block stays frozen). A caller that is not updated keeps the default `False` and therefore fails closed.

**Flag `monitors.p3_actions`.** It gates **both** creation (§3.5: with the flag off no rule with a new action can be made) and dispatch (above: with the flag off a stored `create_task` or `digest` rule behaves as `alert`). The dispatch check is the authoritative one, because a flag turned off after a rule was confirmed must switch the behaviour off too (R83; MACT-1). A `digest` monitor that degrades to an alert is louder than the owner chose, which is the safe direction while the feature is off; the owner stops any monitor with `stop_monitor`, the Stop button or `delete_automation`.

### 3.3 The monitor's to-do (`_task_for_match`)

An owner to-do in the P3 shape (P3-schema §3.4; R20): `kind = 'reminder'`, `due_at` NULL, no automation, so nothing ever fires for it.

1. **One open task per monitor and chat.** Look for an open task this monitor created for this chat: `tasks` (account-scoped, `status = 'open'`, `refs @> {"conversation_ids": [conversation_id]}`) joined to `audit_log` (`account_id` equal, `subject_type = 'task'`, `subject_id = tasks.id`, `event = 'task_created'`, `details @> {"source": "monitor", "automation_id": automation_id}`), newest first. Found ⇒ return its id (a standing monitor that matches every day in one chat does not pile up to-dos; the alert names the task). The key is the chat as well as the monitor because an `all_observed` or multi-chat monitor has one to-do per chat: a single shared to-do would carry the first chat's `refs`, so deleting that chat would delete the to-do the other chats' alerts point at, and `list_tasks` could not show who the later chats were about. **Race:** `record_match` has already run its one-shot completion or standing touch, which is an `UPDATE` of the monitor's `automations` row; that row lock is held until the transaction ends, so two matches of one monitor in two chats (two ingestion transactions, two different conversation locks) serialise on it, and the second sees the first's committed to-do (READ COMMITTED). A missed lookup could only ever create a duplicate to-do, never lose one.
2. Otherwise call M7-TASK's `inbox.tasks.create_owner_task(session, account_id=…, kind="reminder", title=_task_title(rule), due_at=None, refs=<below>, provenance=<below>, run_id=None, tool_call_id=None, actor="system", now=now, source="monitor", automation_id=automation_id, source_message_ids=<the matched ids>)` (R83, MACT-2: the `source` keyword means this unit writes no second `task_created` row). The row it inserts:

| Column | Value |
|---|---|
| `account_id` | the monitor's |
| `conversation_id` | NULL (owner tasks keep it NULL so triage can never retitle or close them, M7-TASK §3.1); the chat is in `refs` |
| `kind` | `reminder` |
| `title` | `_task_title(rule)` (below), stripped, at most 400 characters (M7-TASK's `MAX_OWNER_TITLE`) |
| `details`, `due_at`, `snoozed_until` | NULL |
| `status` | `open` |
| `created_by` | `owner` (the owner confirmed the monitor; owner tasks never expire, M7-TASK §3.5) |
| `source_message_ids` | the matched message ids, sorted (ids only); `first_source_message_id` NULL (outside the agent dedupe index) |
| `run_id`, `tool_call_id` | NULL |
| `refs` | `{"conversation_ids": [conversation_id], "contact_ids": [contact_id]}` (the per-chat and per-contact deletion finds it, P3-schema §7.2 step 4) |
| `provenance` | `memory.items.provenance(tainted=False, managed=True, refs=<the same refs>)`: no contact text is in the title (`tainted` false), but the to-do exists because of activity in a managed chat, so "delete managed data" deletes it (P3-schema §7.2 step 4, managed column). R68 (the `run_origin` baseline, a held call counts as tainted) does not apply here: no tool call is running at match time. The title is built only from the rule's terms and description, which the owner saw and confirmed on the monitor's card, never from message text or a contact name; `_task_for_match` reads the monitor row's `provenance` (one account-scoped read) and, if it says `tainted: true`, sets `tainted` true here too (fail closed; M05 rows hold `{}`, read as untainted) |
| `created_at`, `updated_at` | `now` |

3. The audit row is written by `create_owner_task`, not here: `task_created` (existing event; `subject_type = 'task'`, actor `system`) with details `{"kind": "reminder", "conversation_id": null, "run_id": null, "source": "monitor", "automation_id": automation_id}` (P3-interfaces §13; M7-TASK §3.1). The lookup in step 1 reads exactly these keys.

`_task_title(rule)` is application text plus the owner's own words from the confirmed rule, never message text and never a contact name (names are contact-chosen; the alert and `list_tasks` show who): `"Monitor: " + what`, where `what` is `contact_replied` "a reply arrived"; `keywords` "a message mentioned “t1”, “t2”" (the terms as stored); `media_received` "{a document | a photo | a voice message | a video | media, joined with "or"} arrived"; `semantic` "a message about: {description}"; `decision_requested` "a decision was requested" (+ " about: {description}" when set); `urgent` "an urgent message arrived" (+ " about: {description}").

### 3.4 Owner-facing words (`describe`, `MonitorMatchRenderer`)

**`describe`** (the card, the "Monitor on" notice, alerts): the verb follows the action.
- `alert`, `alert_with_summary`, `create_task`: "Alert you …" (unchanged); for the AI kinds "Ask the AI to check new messages and alert you …".
- `digest`: "List in your daily digest …"; for the AI kinds "Ask the AI to check new messages and list in your daily digest …".
- Extras (the parenthesis): `alert_with_summary` "with the chat's latest triage reasons" (unchanged); `create_task` "and add a task to your list"; `digest` adds nothing about quiet hours (a digest monitor never bypasses them, §3.5).
`describe` stays plain text; each caller escapes it once (M05 behaviour).

**`MonitorMatchRenderer`**: for a `create_task` alert, after the existing lines, when `refs.task_ids` names a task of this account that is still `open`: "📝 In your tasks: {title}" with the title escaped exactly once (`html.escape(title, quote=False)`, GW-ESC §3.3). A task that is gone or closed adds no line. A `digest` alert is batched and never rendered on its own (the digest renders the count).

### 3.5 `create_monitor` (`agent/monitor_schemas.py`, `agent/tools/monitors.py`)

`CreateMonitorArgs.action: Literal["alert", "alert_with_summary", "create_task", "digest"]` with the description: "alert: tell the owner who wrote and when; alert_with_summary: also the chat's latest triage reasons when they cover the message; create_task: alert and add a to-do to the owner's tasks; digest: do not alert, list the match in the next daily digest. create_task and digest work only when the owner has them switched on." New validator (raises `ArgumentRuleError` in fixed words): `action == "digest" and bypass_quiet_hours` ⇒ "a digest monitor never alerts at once, so it cannot reach the owner in quiet hours".

`create_monitor_tool` (runs only `held`, unchanged), first step after `rule = to_rule(args)`: `args.action in P3_ACTIONS and MONITORS_P3_ACTIONS not in ctx.services.enabled_flags` ⇒ `ToolOutcome("error", {"reason": "the monitor actions 'create task' and 'digest' are not switched on yet; use alert or alert_with_summary"}, log_code="p3_actions_off")`. Nothing is created. The card describer (`describe_create`) has no access to the flags (`ToolDescriber` takes only the session, account id and arguments), so with the flag off the owner sees the card and gets this factual refusal after the tap (contract gap MACT-1; the PR-OR10 rule keeps the model from offering the actions while they are off only if the owner says so — see the rule text).

`list_monitors` needs no change: its items already carry `action`. `stop_monitor` is unchanged.

**`owner_request` rule text** for PR-OR10 (in the PR description): "`create_monitor` actions: `alert` (default), `alert_with_summary`, `create_task` (alert and add a to-do) and `digest` (no alert; listed in the next daily digest). Use `create_task` or `digest` only when the owner asks for a task or for the digest; if the tool says they are not switched on, offer `alert` instead. A monitor never replies to anyone."

### 3.6 Edge cases (each has a test in §5)

A standing `create_task` monitor matching three times in one chat (one to-do, three alerts naming it); an `all_observed` monitor matching in two chats (two to-dos); the to-do completed by the owner and a later match (a new to-do); a one-shot `create_task` monitor (one to-do); a `create_task` match through the semantic evaluation path; a `digest` match from a VIP contact (still batched); a `digest` match during quiet hours (batched, not deferred); `digest` with `bypass_quiet_hours` (refused); a P3 action with the flag off (refused after the tap, nothing created); a stored P3-action rule matching while the flag is off (dispatched as `alert`, R83); an M05 rule (unchanged behaviour); deleting the matched chat, the contact, or managed data (the to-do goes); another account's monitor and task.

## 4. Files

**Owned (P3-interfaces §15.1):**

| File | What |
|---|---|
| `src/assistant/automation/rules.py` | `MonitorRule.action` only (named touch point; F0-R owns the rest) |
| `src/assistant/automation/monitors.py` | §3.2, §3.3, §3.4 (M7-FUP imports the matcher here and never edits it) |
| `src/assistant/agent/monitor_schemas.py` | §3.5 (args only; `MonitorVerdicts` unchanged, so no `evaluate_monitor` prompt bump) |
| `src/assistant/agent/tools/monitors.py` | §3.5 |
| Tests (new) | §5 |

**Named touch points (R83, the flag's path to `record_match`):** `automation/monitor_trigger.py` (`MonitorIngestionListener.__init__` and its `record_match` call: `p3_actions` only); `agent/monitor_eval.py` (`MonitorEvalConfig.p3_actions` and the `record_match` call: that only); `bootstrap.py`, the M05 block that builds the two (the `p3_actions=` argument and a function-local import of `MONITORS_P3_ACTIONS`; the import block stays frozen); `docs/ARCHITECTURE.md` §7.5, §8.2 (§7). P3-interfaces §15.1 lists these touch points for M7-MACT.

**Consumed, never edited:** `inbox/tasks.py::create_owner_task` (M7-TASK), `inbox/notifications.py::enqueue`, `inbox/notification_policy.py`, `inbox/models.py::Task`, `memory/items.py::provenance`, `core/flags.py`, `telegram/control/digest.py`.

**Never edit without an ICR** (§15.2): `policy/*`; the send-path `actions/` files; `telegram/*`; `inbox/tasks.py` (M7-TASK); every other line of `automation/rules.py`; `automation/schedules.py`; migrations, `privacy/*`; `config.py`, `core/*`; `agent/prompts/*` (`evaluate_monitor` is unchanged).

## 5. Tests (red first)

Real PostgreSQL 18, `FakeClock`, `FakeLLM` for the semantic path, the real ingestion path with `FakeUpdateSource`, the real `NotificationDelivery` and `decide_delivery`. Files: `tests/unit/test_monitor_rule_actions.py`, `tests/integration/test_monitor_actions.py`, `tests/integration/test_log_hygiene_m7_mact.py`.

**Named tests (the actions are not in IMPLEMENTATION_PLAN S07.3; these are the unit's required tests, README §3):**
- `test_old_monitor_rules_still_load` (unit): stored M05 specs (`alert` and `alert_with_summary`, one-shot and standing, each condition kind, as JSON fixtures written by hand) validate as `MonitorRule`; an unknown action still fails; integration twin `test_an_m05_monitor_still_alerts_exactly_as_before` (priority `high`, same dedupe key, same audit rows apart from the added `action`).
- A test per action:
  - `test_a_create_task_monitor_adds_one_to_do_and_alerts`: one `tasks` row (`kind = 'reminder'`, `due_at` NULL, `created_by = 'owner'`, `conversation_id` NULL, `refs` naming the chat and contact, `provenance` `{"tainted": false, "managed": true, …}`, the title from §3.3 with no contact name and no message text); one `MONITOR_MATCH` (high, `refs.task_ids`); audit `task_created` with `source: "monitor"` and the automation id.
  - `test_a_digest_monitor_batches_the_match_for_the_digest`: the notification is `low`, delivery settles it `batched` (`quiet_by_default`), nothing is sent; the next `build_digest` counts it under other alerts.

**Further tests:**
- `test_a_standing_create_task_monitor_keeps_one_open_task_per_chat` (three matches in one chat: one to-do, three alerts with the same `task_ids`); `test_an_all_observed_create_task_monitor_makes_one_to_do_per_chat` (two chats: two to-dos, each with its own chat in `refs`; deleting one chat deletes only its to-do); `test_two_matches_of_one_monitor_in_two_chats_serialise_on_the_monitor_row` (an async hook starts the second match between the first's lookup and insert: still one to-do per chat, no duplicate); `test_a_later_match_after_the_to_do_is_done_creates_a_new_one`.
- Dispatch-time flag (R83): `test_a_stored_create_task_or_digest_monitor_dispatches_as_alert_when_the_flag_is_off` (both paths, ingestion and evaluation: priority `high`, no to-do, `monitor_p3_action_off` logged with ids only, the audit `action` is `alert`); `test_a_caller_that_passes_no_flag_fails_closed` (`record_match` without `p3_actions` behaves as `alert`); `test_turning_the_flag_back_on_dispatches_the_stored_action` (the same rule makes the to-do and the low-priority match); `test_the_bootstrap_passes_the_flag_to_the_listener_and_the_evaluator`.
- `test_the_monitor_matched_audit_row_gains_action_and_task_id_only`; `test_the_to_do_is_written_through_create_owner_task_with_one_task_created_row` (R83: exactly one `task_created` row, details `source: "monitor"` and the automation id, none hand-written by this unit).
- `test_the_task_provenance_follows_a_tainted_monitor_row` (a monitor row with `provenance = {"tainted": true}` ⇒ the to-do is stored tainted; an M05 row with `{}` ⇒ untainted).
- `test_a_create_task_match_through_the_semantic_evaluation_adds_the_to_do` (a scripted `MonitorVerdicts`).
- `test_a_vip_contact_does_not_lift_a_digest_match_out_of_the_digest`; `test_a_digest_match_in_quiet_hours_is_batched_not_deferred`.
- `test_digest_with_bypass_quiet_hours_is_refused` (`ArgumentRuleError`, before any card).
- `test_create_monitor_refuses_p3_actions_while_the_flag_is_off` (after the tap: factual error, no automation, no audit) and `test_create_monitor_accepts_create_task_and_digest_with_the_flag_on` (the card and notice say "and add a task to your list" / "List in your daily digest").
- `test_describe_names_each_action` (four actions × a deterministic and a semantic kind).
- `test_the_monitor_to_do_goes_with_its_chat_its_contact_and_managed_data` (`delete_conversation_data`, `delete_contact_data`, `delete_managed_data`; an `all_observed` monitor's to-do goes with the matched chat).
- `test_rg21_a_monitor_never_creates_or_reuses_another_accounts_task` (account B's open monitor task with the same automation id in its audit details is never reused by account A).
- `test_the_create_task_alert_names_the_task_once_escaped` (GW-ESC §3.3 rule 6: a term with `&`, `<b>x</b>` and a literal `&amp;` in the task title, through `telegram.render.sanitize`) and `test_a_closed_monitor_task_adds_no_line`.
- `test_a_failed_alert_insert_rolls_back_the_to_do` (the savepoint: a forced dedupe conflict leaves no task row).

**Log hygiene** (`tests/integration/test_log_hygiene_m7_mact.py`): `test_monitor_actions_log_ids_and_codes_only` — canary terms and a canary contact name; the dispatch, the to-do insert and the renderer log no canary.

**Scenarios:** when a QA-S7 scenario marked `awaiting M7-MACT` turns green, tell QE1, who removes the strict-xfail marker in a commit on this PR's branch (R47; P3-interfaces §15.1).

Run locally: these files plus `tests/integration/test_monitors.py`, `tests/integration/test_monitor_tools.py` and `tests/integration/test_digest.py`; ruff, format, mypy on `src/assistant/{automation,agent}`; `lint-imports`.

## 6. Acceptance criteria

1. `MonitorRule.action` accepts the two new actions; every stored M05 rule loads and behaves as before.
2. A `create_task` match adds at most one open to-do per monitor and chat (deletable with its chat, contact and managed data) and alerts naming it; a `digest` match never interrupts and appears in the digest count.
3. Both paths (deterministic at ingestion, semantic evaluation) dispatch the same way; nothing sends, nothing calls a model.
4. `create_monitor` refuses the new actions while `monitors.p3_actions` is off and refuses `digest` with `bypass_quiet_hours`; the card says what each action does. A stored rule with a new action behaves as `alert` while the flag is off, on both match paths.
5. Every named test in §5 passes (CI after merge; QE1 on `int/p3`); no RG test weakened.
6. ARCHITECTURE §7.5 and §8.2 updated.

## 7. Docs updated in the same PR

- **ARCHITECTURE §7.5**, "`MonitorSpec` as built": the four actions and what each does; the to-do's shape (one open to-do per monitor and chat); that `digest` cannot bypass quiet hours; that `monitors.p3_actions` gates creation; the drafted-reply action dropped (OD-19).
- **ARCHITECTURE §8.2**, the `create_monitor` row: the new actions and their flag.
- The PR description carries the `owner_request` rule text (§3.5), the rollback note (§3.1), the contract gaps and the open items for M07.md.

## 8. Owner decisions

- **OD-19:** monitor actions add only "create task" and "digest". If the owner wanted the drafted-reply action back, it would go through `propose()` like a nudge (an approval card, NUDGE-POL's trigger path widened to monitor automations: an ICR, a new unit and new tests).

## 9. Risks and notes

- **Contract gaps** (reported to the TL):
  - **MACT-1 (ruled, R83):** P3-interfaces §10.2 says the actions are "dispatched by monitors" behind `monitors.p3_actions`; the dispatch-time check is authoritative and fails closed. It needs the flag at `record_match`'s callers, so this unit has the three named touch points of §4 (`monitor_trigger.py`, `monitor_eval.py`, the M05 block of `bootstrap.py`). Creation is gated too, but `ToolDescriber` cannot read flags, so a flag-off action is refused after the card's tap, not before.
  - **MACT-2 (ruled, R83):** M7-TASK's `create_owner_task` gains the `source` keyword (with `automation_id` and `source_message_ids`), so this unit calls it and writes no duplicate `task_created` row.
  - **MACT-3:** P3-interfaces §15.4 gives M7-MACT only ARCHITECTURE §7.5; this unit also updates the §8.2 `create_monitor` row (§7), so the contract table should add it.
  - **MACT-4 (accepted, R83):** the one open to-do is per monitor **and chat** (§3.3), a precision of "one open task per monitor"; the lookup uses the to-do's `refs` plus the existing `task_created` audit keys, so no contract change is needed beyond the wording. The fail-closed read of the monitor row's provenance (`tainted: true` makes the to-do tainted) is accepted too.
- **Digest line:** the digest shows "N monitor matches" (a count per kind), not which monitor matched. Good enough for "don't interrupt me"; a per-monitor line would need `inbox/digest.py` and `telegram/control/digest.py` changes that no P3 unit owns. Open item for M07.md.
- **The one-open-task lookup** reads `audit_log` by the details containment; the audit row is written in the same transaction as the task and is swept only after a year, longer than an open to-do usually lives. If it were gone, a second to-do would be created: a duplicate, never a loss.
