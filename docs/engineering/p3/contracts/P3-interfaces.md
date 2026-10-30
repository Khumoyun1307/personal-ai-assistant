# P3 contract — interfaces between the P3 work units

*Normative. The TL froze this contract in F0-C, before D0. F0-S (Josh: J1 + J2), F0-R (Edvard E1) and F0-E (Edvard E3) create every type, constant, protocol and registration point below: real implementations where marked **F0**, signatures or fail-closed stubs where a later unit implements them. Units build against these names and invent none of their own for shared seams. After CP1 a change is an interface change request (ICR: a comment on the P3 integration issue, ruled by the TL within 2 h, WORKFLOW). Tables, columns and CHECKs are owned by `P3-schema.md`; this file owns Python names, signatures and semantics. Paths are under `src/assistant/` unless rooted. Names assume the CP5 tree (`main` with M04 + `int/m05` with M05). Layering (`.importlinter`, AGENTS §5) is binding: domain packages (`accounts`, `conversations`, `policy`, `actions`, `memory`, `inbox`, `automation`, `privacy`) never import `aiogram`, `openai`, `procrastinate`, `telegram`, `llm`, `jobs` or `agent`; `policy` and `actions/states.py` never import `sqlalchemy` or `db`; `core` imports no other assistant package; only `bootstrap.py` instantiates adapters. "(OD-n)" marks text that changes if the owner answers OD-n differently (`decisions.md`); every default below is the default proposed in `decisions.md` §A. "Conflict n" cites the document-conflict list in `decisions.md` §C. The TL rulings of 2026-10-28 and 2026-10-29 (R10–R101) are applied; text they settled is marked "(Rn)" (R10–R31 are P3-T47…P3-T68 in `decisions.md` §B6, Rn = P3-T(n+37); R32 onward are the rows of §B7 and §B8 that cite them). Where a reviewed spec was stricter or more precise than this contract and consistent with those rulings, the contract adopts the spec's text (R32, R78).*

## 1. Module map

### 1.1 New modules

| Module | Created by | Implemented by | Purpose |
|---|---|---|---|
| `migrations/versions/0015_p3.py` | F0-S (J1) | F0-S | The only P3 revision (`P3-schema.md`) |
| `automation/delegation_models.py` | F0-S (J1) | F0-S | `Delegation`, `DelegationConversation` ORM + CHECK mirror tuples |
| `automation/delegation_rules.py` | F0-S (J2) | F0-S | `DelegationRules` persisted model (§3.3) |
| `automation/delegations_repo.py` | F0-S (J2 reads, J1 `end_delegations`) | F0-S | Read seam `active_covering`, `list_active`, `list_current`, `get` (§6.1); the one end primitive `end_delegations` (P3-schema §7.1) |
| `memory/shareable.py` | F0-S (J2) | F0-S | Read seam: `shareable_for` (§6.2) |
| `policy/codes.py` | F0-S (J2) | F0-S | Autonomy reason codes, explanations, outcome classes, `Behaviour`, `EscalationCategory` (§3.2) |
| `actions/disclosure.py` | F0-S (J2) | F0-S | Pure disclosure composer, validator, `disclosure_required`, identity answer (§5) |
| `actions/grounding.py` | F0-S (J2): fail-closed stub | GRND (J2) | `ground(...)` extractor for `content_grounded` (§3.7) |
| `actions/language_keys.py` | F0-S (J2): fail-closed stub | GRND (J2) | Tagger → gate-language key mapper (§3.7) |
| `actions/autonomy_planner.py` | — | M6-PLAN (J3) | `AutonomyPlanner` implementation (§4.1) |
| `actions/release.py` | — | M6-SCHED (J1) | `schedule_autonomous` (`proposed → approved → scheduled`, §4.1; called by M6-PLAN's job, so M6-PLAN depends on M6-SCHED, R18) and the `actions.release_scheduled` body: re-check and release or cancel |
| `core/flags.py` | F0-S (J2) | F0-S | Every P3 flag name (§10.2) |
| `core/autonomy_texts.py` | F0-S (J2) | F0-S (texts proofread by the owner, OD-4, OD-10) | `GateLanguage`, `AI_MARKER`, default disclosure lines, identity answers, and the Pause-button seam `PAUSE_CALLBACK_DATA` and `pause_button()` (§5.2, R45) |
| `core/p3_ports.py` | F0-R (E1) | — (protocols) | `DelegationControls`, `AutomationControls`, `TaskControls`, `CatchUpRequester`, `DelegationRequester`, `P3Ports` (§4.5) |
| `privacy/p3_deletion.py` | F0-S (J1) | F0-S | `purge_p3`, called from every deletion scope |
| `privacy/export.py` | — | M7-EXP (J3) | `/export` snapshot and per-class JSON parts |
| `automation/delegations.py` | — | M6-DELEG (J3) | Delegation service: create from held args, confirm, supersede, owner end, wording; implements `DelegationControls` |
| `automation/delegation_lifecycle.py` | — | M6-LIFE (J1) | `delegation_end` fire handler; ends on consent revoke and connection disabled; overdue reconciler body |
| `automation/reminders.py` | — | M7-REM (E1) | Reminder creation, fire handler, undo handler |
| `automation/followups.py`, `automation/followup_listener.py` | — | M7-FUP (E3) | Follow-up service, deadline handler, 4th ingestion listener |
| `automation/nudges.py` | — | M7-NUDGE (E3) | `followups.nudge` body |
| `automation/management.py` | — | M7-AUTO (E1) | Create/update/pause/resume/delete automations; implements `AutomationControls` |
| `automation/scheduled_summaries.py` | — | M7-SSUM (E2) | Fire handler and `automation.summarize` body |
| `automation/recurrence.py` | — | RRULE (D2) | Pure structured recurrence → RRULE + owner-local echo (API frozen in §8.3) |
| `memory/sensitive_filter.py` | — | SENS (D2) | Pure sensitive-category filter (OD-15; API frozen in §9.6) |
| `memory/inferred.py` | — | M7-MEX (E2) | Candidates → filter → dedupe → `pending_review` rows |
| `memory/review.py` | — | M7-MRV (D2) | Approve / edit / discard a `pending_review` item |
| `memory/delegation_catch_up.py` | — | M6-CATCH (D1) | Deterministic end-of-delegation catch-up builder; implements `CatchUpRequester` |
| `inbox/delegation_alerts.py` | — | M6-ESC (E1) | Escalation delivery rules, LLM-down and chat-paused notices; the `autonomy_not_sent` renderer and the Draft reply button on it and on the escalation alert (§11.2, §12.5) (OD-11) |
| `inbox/catch_up_texts.py` | — | M6-CATCH (D1) | `delegation_catch_up` renderer |
| `inbox/reminder_texts.py` | — | M7-REM (E1) | `reminder_due` renderer (R2: was M7-TXT) |
| `inbox/followup_texts.py` | — | M7-FUP (E3) | `follow_up_met`, `follow_up_missed`, `occurrence_skipped` renderers (R2: was M7-TXT) |
| `inbox/commitments_due.py` | — | COMMIT-DUE (D2) | Handler for `system.surface_due_commitments` |
| `inbox/rating.py` | — | OPS-RATE (D1) | Handler for `system.weekly_rating` (Sunday 19:00 owner-local, R23); `usefulness_rating` renderer |
| `observability/status_p3.py` | — | STAT-ACT (D1) | P3 status-block loaders (§12.6) |
| `observability/availability.py` | — | OPS-AV (D2) | NFR-08 recorder; implements `PollObserver` (§4.4) |
| `agent/delegated_draft.py`, `agent/delegated_draft_schemas.py`, `agent/delegated_context.py` | — | DELEG-DRAFT (E2) | Delegated draft run; implements `DelegatedDrafter` (§9) |
| `agent/memory_extract.py`, `agent/memory_schemas.py` | — | M7-MEX (E2) | `extract_memory` run (§9) |
| `agent/delegation_schemas.py` | — | M6-DELEG (J3) | `CreateDelegationArgs`, `EndDelegationArgs` |
| `agent/reminder_schemas.py` | — | M7-REM (E1) | `CreateReminderArgs` |
| `agent/task_schemas.py` | — | M7-TASK (E1) | `UpdateTaskArgs` |
| `agent/followup_schemas.py` | — | M7-FUP (E3) | `CreateFollowUpArgs` |
| `agent/automation_schemas.py` | — | M7-AUTO (E1) | `CreateAutomationArgs`, `UpdateAutomationArgs`, `DeleteAutomationArgs` |
| `agent/tools/{delegations,delegation_reads,reminders,tasks,task_reads,followups,automations,automation_reads}.py` | F0-R (E1): each `TOOLS = ()` | the tool's unit (§9.4) | Per-module tool tuples (§9.5) |
| `telegram/control/takeover.py` | — | M6-TAKE (J2) | Takeover notice card, "Resume here" press |
| `telegram/control/pause_button.py` | — | M6-PAUSE (J2) | The `pz` press handler only. There is no wrapper in this file: every caller, telegram-side or not, uses `core/autonomy_texts.py::pause_button()` (R45, R99, §12.5) |
| `telegram/control/draft_reply.py` | — | M6-ESC (E1) | The `drf` press handler (Draft reply): shows a held reply's approval card with no model call, or asks for one owner-approved draft (§12.3, §12.5) (OD-11) |
| `telegram/control/export.py` | — | M7-EXP (J3) | `/export` command and confirm card |
| `telegram/control/settings_p3.py` | — | SET-P3 (D1) | `/settings` delegation, wording and automation screens; panel Delegate row |
| `telegram/control/reminder_cards.py` | — | M7-REM (E1) | Done / Snooze 1 h / Tomorrow presses (R2); Pause reminder on a recurring reminder (R21) |
| `telegram/control/followup_cards.py` | — | M7-FUP (E3) | Reopen press (R2) |
| `telegram/control/memory_review.py` | — | M7-MRV (D2) | Review card presses, `mem_edit` input |
| `telegram/control/rating.py` | — | OPS-RATE (D1) | Rating card presses |
| `jobs/tasks_{delegation,release,export,escalation,followups,memory,summaries,surfaces}.py` | F0-R (E1): every §7.1 task pre-registered, calling a handler field that starts unwired | §7.2 | Per-domain job registration |
| `jobs/reconcile_{delegation,memory}.py` | F0-R (E1): empty modules | M6-PLAN + M6-LIFE; M7-MEX | Per-domain reconciler functions, registered by the team's wire function (§7.3) |
| `evals/registry.py` | F0-E (E3) | F0-E; entries by target owners | Eval-target registry (§14.1) |
| `evals/e4.py` | — | EV-E4L (E3) | E4 loader and integrity; hosts `AutonomyDetail`, the per-item record shared by EV-AUT and EV-GATE (§14.4) |
| `evals/autonomy.py`, `evals/rubrics/autonomy_safety.v1.md` | — | EV-AUT (E2) | Autonomy target and judge rubric |
| `evals/gate.py` | — | EV-GATE (E3) | Per-language gate semantics |
| `tools/acceptance/p3_verify.py` | F0-E (E3) | F0-E (CLI, registry) | `p3-verify` CLI skeleton (§14.2) |
| `tools/acceptance/{p3_checks,p3_views,p3_report,p3_restore_check}.py` | F0-E (E3): each `SUBCOMMANDS = ()` | OPS-TV3 (SQ); OPS-VIEWS (SQ, R2); OPS-VIEWS; OPS-RT3s (D2) | Subcommand modules (§14.2) |
| `deploy/sql/{nfr07,nfr08,usefulness}.sql` | — | OPS-AV (D2); OPS-RATE (D1) | `server.sh report <name>` queries |
| `tests/integration/test_p3_end_primitives.py` | F0-S (J1) | F0-S | Tests of `end_delegations` and `cancel_pending_for_delegations` (P3-schema §9; R17) |
| `tests/integration/test_p3_wiring.py` | F0-R (E1) | F0-R | Wiring tests and the one reusable builder `p3_wire_context(*, flags: frozenset[str] = frozenset(), **overrides) -> P3WireContext` that every unit's wiring test imports (§15.3) |
| `tests/integration/p3_factories.py` | F0-S (J1) | F0-S | Factories (P3-schema §9): `seed_delegation`, `seed_memory_item` (any scope, kind, source and status, incl. `pending_review`), `seed_owner_task`, `seed_autonomous_action` (any state, incl. `scheduled`), `seed_availability_minute`. Reminder, follow-up and summary automations use `p2_factories.seed_automation` |
| `tests/fakes/autonomy.py` | F0-S (J2) | F0-S | `FakeAutonomyPlanner`, `FakeDelegatedDrafter` |
| `tests/scenarios/{m06,m07,p3_cross}/conftest.py` | F0-E (E3) | QA-S6 (QJ1), QA-S7 (QE1), QA-X (SQ) | Scenario packages; the driver xfails (never errors) on an unregistered task |
| `tests/scenarios/p3_drive.py`, `tests/scenarios/test_p3_drive.py` | F0-E (E3); SQ after CP1 (R57) | F0-E | `p3_environment(migrated_url, *, flags: frozenset[str], settings: Mapping[str, str] \| None = None)`: `settings` overrides settings such as `AUTONOMY_LANGUAGES` (empty by default, which blocks every ALLOW_AUTO) and `EXPORT_MAX_PART_MB`. The `P3Scenario` members the three packages rely on: `env`, `fake`, `app`, `handlers`, `settings`, `account_id`, `conversation_id`, `contact_id`, `date`, `rows`, `message`, `owner`, `preferences`, `revoke`, `output`, `run_task`, `run_periodic`, `require`. The driver starts no poller (the P2 pattern), so scenarios cannot exercise the fast lane; the Pause button and `/pause` scenarios go through the inbox dispatcher |

Every unit also owns its own new test files, including `test_log_hygiene_<unit>.py` (F0-E template) under `tests/unit/`, or `tests/integration/` when it needs the database (e.g. F0-S's `tests/integration/test_log_hygiene_f0s.py`, P3-schema §9).

### 1.2 Existing modules P3 edits

The full per-file rule after CP1 is §15. This table says who touches what, in merge order.

| Module | Editors (in order) | What |
|---|---|---|
| `policy/context.py` | F0-S | `DelegationSnapshot`, `RateCounters`, `AutonomyFacts`, three `PolicyContext` fields (§3.1) |
| `policy/rules.py`, `policy/engine.py`, `policy/explanations.py` | M6-POL | Real autonomy rules, outcome derivation, explanations, execution targets (§3) |
| `actions/states.py` | F0-S | Five `StateReason` members + explanations (§2.1) |
| `actions/lifecycle.py` | F0-S (event table + origin guard; the `delegation_ids` filter on `_change_pending` + `cancel_pending_for_delegations`, P3-schema §7.1); M6-SCHED (bulk helpers) | §2.2 |
| `actions/proposals.py` | F0-S (signature; `_insert` stores `ProposalRequest.delegation_id`; `ProposalRefused.decision`, §4.3); M6-PLAN (`delegation_auto` path); NUDGE-POL (trigger path) | §4.3 |
| `actions/policy_loader.py` | F0-S (the three autonomy fact builders as fail-closed stubs returning `None`, R79); M6-PLAN (fills them); M6-TXA (execution facts); M6-TAKE (takeover input); NUDGE-POL (reference) | One fact builder for proposal and execution (§3.5) |
| `actions/executor.py`; `bootstrap._wire_m04` | M6-TXA | Tx A for autonomy; `ActionExecutor` gains `autonomy: AutonomyConfig \| None = None`, and the executor is built late-bindably (ICR-P3-1, pre-approved, R43; §15.3) |
| `actions/ingestion.py` | M6-SCHED | Takeover bulk path covers `scheduled` and autonomous `approved`; the ingestion exits of §2.2 |
| `actions/autonomy.py` | M6-TAKE (`takeover_counts`, `note_takeover`, `resume_here`, `paused_by_takeover`); M6-PAUSE (pause sources, the `lock_account` wrapper, `pause_after_control_forbidden`, R46) | §12, §13 |
| `inbox/notifications.py` (control-chat delivery) | M6-PAUSE (named touch point: calls `pause_after_control_forbidden` after commit when a control send comes back forbidden, R46; M7-EXP calls it for its document sends); M6-ESC for fact loading | §11.3 |
| `actions/activity.py`, `agent/tools/activity.py` | STAT-ACT | `Authorization.kind = "delegation"`: two constants and the lookup block in `actions/activity.py`; `get_activity` carries `authorized.kind`, `authorized.delegation_id` and `origin` (three keys and one sentence in `STATUS_RULE`) (named touch points) |
| `actions/ports.py` | F0-S | `AutonomyPlanner`, `DelegatedDrafter`, `AutomationTrigger` (§4) |
| `actions/config.py` | F0-S | `AutonomyConfig` (§10.1) |
| `conversations/service.py` | M6-TAKE | `takeover_at` writer, separate from `ingest_error`; one line in `record_ingest_failure` (cancel autonomous actions first) |
| `telegram/ingress/poller.py` | F0-S (hook only) | `PollObserver` call after the `_mark_poll_ok` commit (§4.4) |
| `telegram/ingress/fast_lane.py` | M6-PAUSE | Pause-button callback; the `my_chat_member` (bot blocked) auto-pause (the poll's own clock and commit) |
| `telegram/gateway.py` | GW-DOC (`send_document` on `OutboundGateway`, `TelegramSender` and `AiogramSender`, which live in this file). GW-ESC: the fix landed in P2 #133; verification only (an edit only if it finds a gap, §4.6) | §4.6 |
| `core/channel.py`; `telegram/control/agent_channel.py` | GW-DOC | `ControlChannel.send_document`; `GatewayControlChannel.send_document` implements it (named touch points) |
| `core/jobs.py` | F0-S | Task names, queueing-lock helpers, `DraftReason` members (§7) |
| `actions/drafts.py`; `agent/drafting.py`; `inbox/notify.py` | F0-S (named touch points, OD-11, P3-T154) | `DraftReason.ALERT_DRAFT` joins `JOB_REASONS` (the unflagged path, as `REDRAFT`); `_skip_code` treats it as `REDRAFT` (any mode but Ignore, any trigger); `AssistDraftJob` tells the owner once when an `ALERT_DRAFT` job ends without a card, through a new `AlertKind.ALERT_DRAFT_FAILED` and its `ALERT_TEXT` in `inbox/notify.py`. Nothing else changes (§7.1) |
| `config.py`, `.env.example` | F0-S | §10.1 |
| `inbox/notification_types.py` | F0-S | 13 kinds (`autonomy_not_sent` added, OD-11), `NotificationRefs.delegation_ids` and `.memory_ids`, `DeliveryClass` table with its `feedback_buttons` column and lookup (§11, R82) |
| `inbox/notification_policy.py` | F0-R (class consult + `active_delegation` slot); M6-ESC | §11.3 |
| `inbox/notifications.py` | F0-R (class consult; the card's `feedback_buttons` consult, R82); M6-ESC (fact loading only) | §11.3 |
| `inbox/triage_store.py` | F0-R (`delegation_requester` hook); M6-ESC (covered-chat effects) | §4.5 |
| `inbox/tasks.py` | M7-TASK | Owner functions; "triage never reopens" comment (conflict 28); `create_owner_task` gains a `source` keyword so M7-MACT writes no duplicate `task_created` row (R83) |
| `inbox/digest.py`, `agent/tools/summaries.py`, `memory/catch_up.py` | M7-TASK | Separate `snoozed_until` filter functions |
| `memory/items.py` | F0-S (two named touch points: `contact_facing` excludes `kind = 'shareable'` for every run, R54; `forget` treats a `pending_review` row as not found, P3-schema §3.2); M6-DELEG | `kind = shareable` write path |
| `memory/undo.py` | F0-R | Event → handler registry (§12.7) |
| `automation/schedules.py` | F0-R (fire registry); M7-AUTO (`reschedule`, named touch point); M7-FUP (`_still_runs` race, named touch point) | §8.1 |
| `automation/rules.py` | F0-R (rule models, dispatch); M7-MACT (`MonitorRule.action` Literal only, named touch point) | §8.2 |
| `automation/monitors.py`, `agent/monitor_schemas.py`, `agent/tools/monitors.py` | M7-MACT | Monitor actions `create_task`, `digest` |
| `agent/tools/registry.py` | F0-R | Per-module `TOOLS` concatenation; P3 `ToolServices` fields (§9.5) |
| `agent/runtime.py`, `bootstrap.build_*` | F0-R | One `ToolServices` for runtime and confirmation applier |
| `agent/tools/executor.py` | F0-S | Two `NOT_RUN_STATUSES` additions (§9.3) |
| `agent/untrusted.py` | F0-S | Four `Source` members (§9.3) |
| `agent/tools/memory.py` | M6-DELEG | One branch: `remember(kind=shareable)` |
| `agent/prompt_registry.py` | F0-E (reshape); then one line per prompt owner (§9.2) | §9.2 |
| `agent/tools/status.py`, `observability/status.py`, `telegram/control/handlers.py` | F0-R | Status-block registry, `render_status` iteration, `HELP_TEXT` from routes (§12) |
| `telegram/control/panels.py`, `telegram/control/routes.py` | F0-R | Row-builder hooks; `ControlRoutes` P3 fields (§12) |
| `telegram/control/approvals.py` | M6-PLAN (named touch points: R80, one line on the approval card showing the autonomy explanation from `draft_flags.autonomy_codes` and, for a late conversion, a "was due at … (late)" note; OD-11, the held-reply rule in `sync` and `sync_cards`, §2.3); M6-TXA's late conversion reuses it | The held reply's card presentation (§2.3, §12.5) (OD-11) |
| `telegram/control/activity.py` | STAT-ACT | A private `_delegation_suffix` helper, an `owner_approval` kind check in the sent branch, and the suffix in the sent, awaiting, uncertain and failed branches (an automatic reply never reads "approved by you") |
| `accounts/service.py`, `inbox/policy_notice.py` | PV6 (E2) | `POLICY_VERSION = "v6"`, `POLICY_CHANGES`, notice text |
| `privacy/{content_columns,retention,deletion,post_restore}.py` | F0-S (J1) | Registry rows, sweeps, `purge_p3` calls, post-restore step 2b (P3-schema §5–§8) |
| `inbox/backup_check.py`, `db/models.py`, `migrations/env.py` | F0-S (J1) | Restore-notice count line; `AvailabilityMinute`; model import (P3-schema §11 R-6) |
| `actions/models.py`, `memory/models.py`, `conversations/models.py`, `inbox/models.py`, `automation/models.py` | F0-S (J1) | ORM columns, CHECK mirrors and constants (P3-schema §4) |
| `telegram/control/inbox_handlers.py` | M6-PAUSE | `my_chat_member` (bot blocked): logs the status code only; the auto-pause is in `fast_lane.py` and never resumes by itself |
| `tests/fakes/*` | F0-S (`tests/fakes/autonomy.py`); GW-DOC (`send_document` on the channel and sender fakes) | §4.2, §4.6 |
| `jobs/tasks.py`, `jobs/reconcile.py`, `jobs/reconcile_actions.py` | F0-R (groups, registration); M6-SCHED (`reconcile_actions.py`) | §7 |
| `bootstrap.py` | F0-R (three wire functions); then units inside their team's function only (§15.3) | §15.3 |
| `evals/{cli,targets,live,agent_mode}.py` | F0-E | Derived from `evals/registry.py` (§14.1) |
| `evals/drafts.py`, `tests/unit/test_eval_drafts.py` | EV-GATE | Thresholds hook; replaces `test_the_status_is_always_info_in_p2` |
| `deploy/server.sh` | F0-E | Generic `report` and `p3-verify` actions (§14.3) |
| `deploy/sql/nfr05.sql`; `tests/integration/test_nfr05_query.py` | OPS-AV | Escalation-latency extension; the test edit is the measure list, one expected list and the `_alert` helper only (named touch point) |
| `tools/acceptance/restore_verify.py` | OPS-RT3s | One import and `main()`'s `with` block, so the existing `restore-test` runs the P3 check against the scratch restore database without touching `server.sh` (named touch point) |
| `.github/workflows/ci.yml` | F0-E | `push: branches: [main, int/p3]` |

**F0 scope growth (TL-ruled R17).** Beyond the core F0 file lists of README §3, all named in the tables above and in §15.1:
- **F0-S** also owns `actions/config.py` (`AutonomyConfig`, §10.1); the fail-closed stubs `actions/grounding.py` and `actions/language_keys.py` (§3.7); the `delegation_ids` filter on `_change_pending` and `cancel_pending_for_delegations` in `actions/lifecycle.py`; `automation/delegations_repo.py::end_delegations`; one restore-notice line in `inbox/backup_check.py`; `db/models.py`; `migrations/env.py`; `tests/integration/test_p3_end_primitives.py` (P3-schema §7.1, §8, §11 R-6). It also adds `LLM_ROUTE_DRAFT_DELEGATED_MODEL` (§10.1, R16).
- F0-S also edits, as named touch points: `jobs/tasks.py::JobHandlers.retention_sweep` (logs the four new `SweepReport` counts; F0-R also edits `jobs/tasks.py` before CP1, so the later PR rebases), `tools/acceptance/p2_verify.py::REASONS` (the five new reasons), `memory/items.py` (§1.2), and the existing tests the new CHECKs, events and unique index break (`tests/integration/test_action_transitions.py`, `test_action_lifecycle_bulk.py`, `test_execution.py` and the other users of `execution_env.inserted`, `tests/integration/test_p2_verify.py`, `tests/unit/test_p2_verify.py`; R41 for `execution_env.py`).
- **F0-R** pre-creates the eight P3 `agent/tools` modules, each with `TOOLS = ()` (§9.5).
- **F0-E** pre-creates the four `tools/acceptance` subcommand modules, each with `SUBCOMMANDS = ()` (§14.2).
- F0-S adds only the **five new** `StateReason` members (§2.1) and the **two new** `NOT_RUN_STATUSES` (§9.3). `rights_changed`, `consent_revoked` and `awaiting_confirmation` already exist.

## 2. Actions: reasons, scheduled-state events, origin, disclosure flag

### 2.1 `StateReason` additions (`actions/states.py`, F0-S)

```python
class StateReason(StrEnum):
    # ... the 26 M04 members, unchanged ...
    LATE = "late"
    DELEGATION_ENDED = "delegation_ended"
    TRIGGER_STALE = "trigger_stale"
    VOLUME_CAP = "volume_cap"
    DISCLOSURE_MISSING = "disclosure_missing"
```

`RIGHTS_CHANGED` and `CONSENT_REVOKED` already exist (M04). P3 reuses them for `scheduled` and autonomous actions; their explanations do not change. New entries in `STATE_REASON_EXPLANATIONS` (English, fixed table; the owning unit may polish wording, never the meaning):

| Reason | Explanation |
|---|---|
| `late` | "This automatic reply could not be sent in time, so it became a draft for you to approve." |
| `delegation_ended` | "The delegation for this chat ended before the reply was sent." |
| `trigger_stale` | "The message this reply answers is too old for an automatic reply." |
| `volume_cap` | "The automatic-reply limit for this chat or delegation was reached." |
| `disclosure_missing` | "This automatic reply needed the AI notice and did not have it, so it was not sent." |

`STATE_LABELS` is unchanged (`scheduled` → "Scheduled"). Read models render a scheduled action as "Scheduled — automatic reply under delegation #N"; never "Sent" (RG-20, STAT-ACT). `ActivityEntry` has no `scheduled_for`, so no time is shown; adding one is an ICR.

### 2.2 Transitions and events (`actions/lifecycle.py` event table, F0-S)

`TRANSITIONS` is unchanged: it already holds `proposed → approved`, `approved → scheduled`, `scheduled → approved` and `scheduled → cancelled`. P3 adds **no** pair. There is no `scheduled → outdated`, `scheduled → expired` or `scheduled → blocked`.

F0-S replaces the `IllegalTransitionError("… has no P2 audit event")` branch of `_event` with:

| Transition | Event | Guard added to the conditional UPDATE (F0-S) |
|---|---|---|
| `proposed → approved` | `action_auto_approved` | `origin = 'delegation_auto'`; a non-autonomous action is never auto-approved (the UPDATE applies to nothing and `transition` returns `False`) |
| `approved → scheduled` | `action_scheduled` | `origin = 'delegation_auto'`; `values` must set `scheduled_for` and `late_after` |
| `scheduled → approved` | `action_released` | — |
| `scheduled → cancelled` | `action_cancelled` (existing target mapping) | `reason` is required (never `None`) |

Audit `details` keep the M04 shape (`from`, `to`, `version`, `reason`) and add, as ids and epochs only: `delegation_id` on all four and, whenever the action row has one, on every other transition, every `_change_pending` bulk cancel and every held reply (§2.3); `scheduled_for` and `late_after` (epoch seconds) on `action_scheduled`. Actor is `system`.

**Scheduled exits.** A `scheduled` action leaves only to `approved` (the release re-check passed; `actions.execute` is deferred after commit) or to `cancelled` with a reason:

| Cause | Reason | Path (unit) |
|---|---|---|
| Owner manual message in the chat (takeover) | `takeover` | ingestion bulk path (M6-SCHED) |
| Contact message newer than `shown_through_message_id` | `new_contact_message` | ingestion (M6-SCHED: cancels the autonomous `approved` and `scheduled` actions and sets `pending_delegation_at` again, so the reply is requested again) and the release re-check |
| Contact edit or deletion of the answered message; owner edit; dead letter | `source_edited`; `source_deleted`; `owner_edit`; `ingest_error` | ingestion (M6-SCHED; a source edit also sets `pending_delegation_at` again; other origins keep the M04 `outdated` paths) |
| `/pause`, Pause button, stop word, bot blocked | `autonomy_paused` | `actions/autonomy.pause` → `cancel_autonomous_pending` (already covers `PENDING`) |
| Connection rights changed | `rights_changed` | bulk path (M6-SCHED) |
| Connection disabled | `connection_disabled` | bulk path (M6-SCHED) |
| `ai_managed` revoked | `consent_revoked` | revoke bulk path; `cancel_pending_ai_generated` covers `delegation_auto` (M6-SCHED) |
| Delegation ended, superseded or no longer covering | `delegation_ended` | `delegations_repo.end_delegations` → `lifecycle.cancel_pending_for_delegations` (F0-S; called by M6-DELEG, M6-LIFE, deletion, restore); a supersede that leaves the older delegation active cancels its actions in the shared chats with `cancel_pending_for_delegations(..., conversation_ids=...)` (M6-DELEG, P3-schema §2.2); otherwise the release re-check |
| Past `late_after` at release | `late` | release (M6-SCHED); card conversion (§3.6) |
| Trigger message too old | `trigger_stale` | release |
| Volume cap or loop guard | `volume_cap` | release |
| `actions.send` or `autonomy.send` off | `sending_disabled` | release |
| Chat set to Ignore; data deleted; restore | `ignore_mode`; `data_deleted`; `restored_from_backup` | existing paths (`cancel_pending` already covers `scheduled`) |
| Any other failed check | `policy_denied` | release |

**Autonomous end states.** For origin `delegation_auto`, a policy failure in the release re-check or in Tx A ends the action `cancelled` with the reason from §3.6: never `blocked`, `outdated` or `expired`. Tx B outcomes (`succeeded`, `failed`, `delivery_uncertain`, `executing → expired` on a `WINDOW_CLOSED` send result, late results) are unchanged. The M6-SCHED takeover, rights and connection bulk paths move autonomous `approved` actions to `cancelled` too (the M04 `outdate_pending(include_approved=True)` keeps doing what it does for the other origins). Owner-approved and dictated actions keep the M04 table. M04's `lifecycle.expire_due` can still end an autonomous `approved` action `expired` (window closed or maximum age) before Tx A claims it; that is truthful, sends nothing and is left unchanged.

### 2.3 `ActionOrigin` use

`ActionOrigin` gains no member. `ProposalRequest` gains `delegation_id: int | None = None` (F0-S).

- **`delegation_auto`** is created only by `propose()` called from the `replies.delegated` job (M6-PLAN), with a stored ALLOW_AUTO proposal decision, `delegation_id` set, `tool_call_id` `None` and no `trigger`. Never by a tool (invariant 2). Refusals (`ProposalRefused` codes): `delegation_required` (no `delegation_id`), `autonomy_downgraded` (the re-decision under the locks was not ALLOW_AUTO; nothing inserted, §4.1), `trigger_not_available` (a trigger on a non-`agent_proposal` origin, §4.3). Until M6-PLAN merges, the M04 refusal `autonomous_not_available` stays.
- **The held reply** (called "fallback card" before OD-11 was answered; an autonomy-only failure, shadow mode with `autonomy.send` off, a send-precondition DENY at proposal, or a release or Tx A conversion, §3.6) is origin `agent_proposal` with `delegation_id` set, `draft_flags["autonomy_codes"]` = the failed codes and `draft_flags["presentation"] = "on_request"`. Its row, state (`awaiting_approval`, or `blocked` with copy-ready text when a send precondition failed), expiry and retention are exactly those of the card it replaces. It carries no disclosure and survives `/pause` (P2-T23) (OD-31). **What the owner sees first is not the card but one `autonomy_not_sent` alert** (§11.2) with a Draft reply button (§12.3, §12.5), enqueued in the transaction that inserts the held reply. The producer never calls `present_after_commit` for it, and the card sync never presents it on its own (M6-PLAN's touch point in `telegram/control/approvals.py`): `sync` and `sync_cards` skip the "never shown" presentation of an action whose `draft_flags["presentation"]` is `"on_request"` unless an `approval_card_requested` audit row exists for it (§13). The Draft reply press writes that row and presents the existing card after commit: same text, no model call, no AI line, Approve / Edit / Reject and "Sendable until" as on any card; a lost presentation is then recovered by the sync. An action without the key (every card from before P3, every Assist card, every owner-requested draft) is presented exactly as today (OD-11).
- **Nudges** (M7-NUDGE) are `agent_proposal` with `trigger_automation_id`; never `delegation_auto` (OD-18).
- Idempotency of autonomous proposals is `ux_actions_delegation_base` (UNIQUE `(delegation_id, base_message_id)` for `delegation_auto`, excluding rows cancelled with `disclosure_missing`), and at most one in-flight autonomous action per chat is `ux_actions_autonomous_inflight` (`P3-schema.md` §3.1, R-1; TL-ruled R10). A unique violation on either is the idempotent "already proposed" outcome, never an error. So the only recovery that is a second `delegation_auto` for the same base message is the `disclosure_missing` re-proposal (§3.6); every other recovery is a held reply (OD-11).

### 2.4 Content-free keys in `actions.draft_flags` and `actions.policy_decision`

Backward-compatible jsonb additions (AGENTS §12); ids, enums, bools and codes only.

| Key | On | Set by | Meaning |
|---|---|---|---|
| `language`, `confidence`, `uses_only_permitted_information`, `sensitive_topic`, `makes_commitment`, `should_escalate`, `escalation_reason` | `delegation_auto` and held replies | M6-PLAN | The delegated draft's flags (the M04 keys, same names) |
| `in_scope`, `behaviour`, `identity_question`, `escalation_category` | same | M6-PLAN | The delegated draft's P3 flags |
| `knowledge_ids` | same | M6-PLAN | `knowledge_ids_used` as the model gave them |
| `identity_answer`, `disclosure_required`, `disclosure_added` | `delegation_auto` | M6-PLAN, M6-TXA (re-proposal) | §2.5, §5 |
| `autonomy_codes` | held replies | M6-PLAN, M6-TXA, M6-SCHED | The failed autonomy codes that held the reply back |
| `shadow` | held replies | M6-PLAN | `"allow_auto"` when the plan was ALLOW_AUTO and `autonomy.send` was off |
| `converted_from`, `reproposed_from` | held replies; `delegation_auto` | M6-TXA, M6-SCHED | The cancelled action's id |
| `was_due_at` | held replies from a late conversion | M6-SCHED (`convert_cancelled`), reused by M6-TXA | Epoch seconds of the cancelled action's `scheduled_for`; read by the approvals line (R80) |
| `presentation` | held replies | M6-PLAN, M6-SCHED (`convert_cancelled`, reused by M6-TXA) | `"on_request"`: the card is shown only after a Draft reply press (§2.3). The only value; absent on every other action, which is presented as today (OD-11) |

`actions.policy_decision` keeps the M04 `decision_record` shape (`phase`, `outcome`, `checks[{name, passed, reason}]`). `propose()` stores the proposal decision; the claim overwrites it with the execution decision (M04 behaviour). For a claimed `delegation_auto` the stored execution decision holds a passing check for each of the ten `AUTONOMY_RULES` (§3.4); OPS-TV3 fails closed without them. **The two limits used (R84):** the claim also stores one extra top-level key, `"limits": {"per_conversation_hour": <int>, "per_delegation": <int>}` (integers, never strings), taken from the `DelegationSnapshot` (`limit_per_conversation_hour`, `limit_per_delegation`) that `decide` saw at the claim (M6-TXA). A non-autonomous claim stores no `limits` key. OPS-TV3 checks the caps from this key and the other decision records and non-content columns only; it never reads `delegations.spec`, a content column.

### 2.5 `disclosure_included`

- Column `actions.disclosure_included bool NOT NULL DEFAULT false`, CHECK `origin = 'delegation_auto' OR NOT disclosure_included` (`P3-schema.md`; invariant 13, PRD §11.4).
- **Set only by M6-TXA**, in the claim update (`approved → executing`), to `origin == delegation_auto and disclosure.includes_disclosure(action.text)` (§5.1). Every other origin keeps `false` even when its text happens to start with `AI_MARKER` (an owner may type one); setting it there would violate the CHECK and fail the claim. The value is fixed at claim and never changed afterwards.
- `propose()` stores, for `delegation_auto`, `draft_flags["disclosure_required"]` and `draft_flags["disclosure_added"]` (bools). Held replies and nudges never contain the disclosure line (the CHECK forbids the flag; the composer is never called for them).
- An identity answer (§5.1) contains `AI_MARKER`, so it counts as included.
- OPS-TV3 reads only this column, `delegation_id`, `late_after` and the stored decision (including its `limits` key, §2.4, R84); it never reads text and never reads `delegations.spec`.

## 3. Policy

`policy` stays pure: it imports only the standard library, `core` and `assistant.actions.states`. Grounding, tagging, counters and the disclosure decision run in the loader (`actions/policy_loader.py`) and reach `decide` as facts.

### 3.1 `PolicyContext` additions (`policy/context.py`, F0-S)

```python
from assistant.core.autonomy_texts import GateLanguage
from assistant.policy.codes import Behaviour


@dataclass(frozen=True, slots=True)
class DelegationSnapshot:
    """The delegation covering this chat, read under the §9.4 lock order."""

    delegation_id: int
    account_id: int
    kind: str  # "time_bounded" | "standing"
    status: str  # "active" | "ended" | "cancelled"
    starts_at: datetime
    ends_at: datetime | None  # None = standing
    confirmed_at: datetime
    covers_conversation: (
        bool  # a live delegation_conversations row (ended_at IS NULL) for this chat
    )
    topic_scoped: bool  # rules.topic is set
    contact_listed: bool  # the chat's contact is in rules.knowledge_contact_ids
    share_widened: bool  # delegations.share_widened
    allowed_behaviours: frozenset[Behaviour]
    limit_per_conversation_hour: (
        int  # rules.limits, never above the setting unless raised on the card
    )
    limit_per_delegation: int


@dataclass(frozen=True, slots=True)
class RateCounters:
    """Derived from `actions` rows of origin delegation_auto, read under the
    delegation FOR UPDATE (ADR-20); no counter column. Every claimed attempt
    counts, whatever its outcome (uncertain and failed attempts count)."""

    conversation_last_hour: int  # this chat, any delegation, attempt_started_at > now - 1 h
    delegation_total: int  # this delegation, attempt_started_at >= starts_at
    rapid_replies: int  # this chat, last AUTONOMY_LOOP_WINDOW_MINUTES: autonomous replies
    # the contact answered within AUTONOMY_LOOP_ANSWER_SECONDS
    loop_reply_threshold: int  # AUTONOMY_LOOP_REPLIES
    repeated_contact_message: (
        bool  # the trigger equals an earlier contact message of this chat inside the
    )
    # loop window (compared by hash in the loader; no text crosses into policy)


@dataclass(frozen=True, slots=True)
class AutonomyFacts:
    """Model flags come from the delegated draft (stored in actions.draft_flags);
    every other field is computed by the application."""

    # model flags (DelegatedDraftResult): they can only downgrade (§3.4)
    confidence: str  # "low" | "medium" | "high"
    uses_only_permitted_information: bool
    sensitive_topic: bool
    makes_commitment: bool
    should_escalate: bool
    in_scope: bool
    behaviour: str  # a Behaviour value or "none"
    identity_question: bool
    escalation_category: str  # an EscalationCategory value
    # application facts
    identity_answer: bool  # the final text is the fixed identity sentence (§5.1)
    trigger_message_sent_at: datetime | None
    max_trigger_age: timedelta  # AUTONOMY_MAX_MESSAGE_AGE_MINUTES
    ungrounded_values: int  # grounding.ground(body, sources=...).ungrounded
    knowledge_ids_used: frozenset[int]
    knowledge_ids_allowed: frozenset[
        int
    ]  # the ids shareable_for returned for this delegation and chat
    contact_language: GateLanguage | None  # language_keys.contact_key(...) over the answered burst
    draft_language: GateLanguage | None  # language_keys.text_key(body)
    declared_language: GateLanguage | None  # language_keys.declared_key(result.language)
    enabled_languages: frozenset[GateLanguage]  # AUTONOMY_LANGUAGES
    disclosure_required: bool  # disclosure.disclosure_required(...)
    disclosure_included: bool  # disclosure.includes_disclosure(final text)


@dataclass(frozen=True, slots=True)
class PolicyContext:
    # ... the M04 fields, unchanged ...
    delegation: DelegationSnapshot | None = None  # replaces `delegation: None = None`
    counters: RateCounters | None = None
    autonomy: AutonomyFacts | None = None
```

- For every origin except `delegation_auto` the loader leaves the three fields `None`, and every autonomy rule returns `None` (not applicable): owner-approved and dictated actions behave exactly as in M04.
- **Missing facts fail closed.** For `delegation_auto`, `counters is None` fails `volume_ok` with `volume_conversation`, and `autonomy is None` fails every rule that reads it with that rule's first code.
- For `delegation_auto` the loader sets `PolicyContext.sending_enabled = actions.send and autonomy.send` (execution phase), so turning `autonomy.send` off stops every pending autonomous send (stop-the-line, README §6).
- The loader sets `ConversationSnapshot.autonomy_paused_reason = "takeover"` only when the takeover counts for this delegation session: `takeover_at > max(delegation.confirmed_at, conversation.autonomy_resumed_at)` (OD-5; M6-TAKE). `ingest_error` passes through unchanged.

### 3.2 Reason codes (`policy/codes.py`, F0-S)

Every autonomy code is reserved here, with its explanation and outcome, before M6-POL merges, so M6-ESC, STAT-ACT and OPS-TV3 build against the codes from D0. M6-POL adds the rules that produce them, merges `AUTONOMY_CODES` into `rules.REASON_CODES` (a code still belongs to exactly one rule) and into `EXPLANATIONS`, and never changes a code or its outcome without an ICR.

```python
class Behaviour(StrEnum):  # E4 vocabulary: DelegationRules.allowed, DelegatedDraftResult.behaviour
    ANSWER_FROM_KNOWLEDGE = "answer_from_knowledge"
    ASK_FOR_DETAILS = "ask_for_details"
    ACKNOWLEDGE_AND_DEFER = "acknowledge_and_defer"
    DECLINE = "decline"


class EscalationCategory(StrEnum):  # PRD §11.5
    URGENT_DECISION = "urgent_decision"
    NEEDS_OWNER = "needs_owner"
    FYI = "fyi"
    SUSPICIOUS = "suspicious"
    NONE = "none"


class RuleClass(StrEnum):
    SEND_PRECONDITION = "send_precondition"  # a failure ⇒ DENY
    AUTONOMY = "autonomy"  # a failure, with no send-precondition failure ⇒ REQUIRE_APPROVAL


class Conversion(StrEnum):  # after an execution-phase cancel of an autonomous action
    NONE = "none"
    CARD = "card"  # held reply (agent_proposal), same body, no disclosure, no model call;
    # the owner sees an autonomy_not_sent alert with a Draft reply button (OD-11)
    REPROPOSE = "repropose"  # a new delegation_auto proposal: same body + disclosure, no model call


@dataclass(frozen=True, slots=True)
class ReasonCode:
    code: str
    rule: str
    explanation: str  # owner-facing; one sentence for the 20 new codes (the two M04 codes keep their two-sentence M04 text, R34); no names, no message text
    execution_reason: StateReason  # delegation_auto at Tx A or release ⇒ cancelled/<this>
    conversion: Conversion


AUTONOMY_RULES: tuple[str, ...] = (
    "autonomy_not_paused",
    "delegation_active",
    "in_scope",
    "trigger_fresh",
    "draft_safe",
    "content_grounded",
    "knowledge_scope",
    "disclosure_present",
    "volume_ok",
    "language_gate",
)
AUTONOMY_CODES: Mapping[str, ReasonCode]  # exactly the rows of the table below


def rule_class(rule: str) -> RuleClass:  # AUTONOMY for AUTONOMY_RULES, SEND_PRECONDITION otherwise
    ...
```

| Rule | Code | Explanation (card text, FR-ACT-03) | Proposal (`delegation_auto`) | Execution reason | Conversion |
|---|---|---|---|---|---|
| `autonomy_not_paused` | `autonomy_paused` (M04) | M04 text | REQUIRE_APPROVAL | `autonomy_paused` | none |
| `delegation_active` | `no_delegation` (M04) | M04 text (R34; the deployed M04 explanations of `autonomy_paused` and `no_delegation` are two sentences each and stay, so no M04 card text changes) | REQUIRE_APPROVAL | `delegation_ended` | none |
| | `delegation_not_started` | "The delegation for this chat has not started yet." | REQUIRE_APPROVAL | `delegation_ended` | none |
| | `delegation_ended` | "The delegation for this chat has ended." | REQUIRE_APPROVAL | `delegation_ended` | none |
| | `not_covered` | "This chat is not part of the delegation." | REQUIRE_APPROVAL | `delegation_ended` | none |
| `in_scope` | `out_of_scope` | "The message is outside the delegation's topic." | REQUIRE_APPROVAL | `policy_denied` | card |
| | `behaviour_not_allowed` | "The reply does something the delegation does not allow." | REQUIRE_APPROVAL | `policy_denied` | card |
| `trigger_fresh` | `trigger_stale` | "The message is too old for an automatic reply." | REQUIRE_APPROVAL | `trigger_stale` | card |
| `draft_safe` | `low_confidence` | "The assistant was not sure enough to reply on its own." | REQUIRE_APPROVAL | `policy_denied` | card |
| | `unpermitted_information` | "The reply may use information the delegation does not share." | REQUIRE_APPROVAL | `policy_denied` | card |
| | `sensitive_topic` | "The message touches a sensitive topic." | REQUIRE_APPROVAL | `policy_denied` | card |
| | `makes_commitment` | "The reply would commit you to something." | REQUIRE_APPROVAL | `policy_denied` | card |
| | `should_escalate` | "This looks like something you should handle yourself." | REQUIRE_APPROVAL | `policy_denied` | card |
| `content_grounded` | `ungrounded_value` | "The reply contains a number, date, link or contact detail that is not in what you shared." | REQUIRE_APPROVAL | `policy_denied` | card |
| `knowledge_scope` | `knowledge_not_shareable` | "The reply uses knowledge this contact may not receive." | REQUIRE_APPROVAL | `policy_denied` | card |
| `disclosure_present` | `disclosure_missing` | "The reply needed the AI notice and did not have it." | REQUIRE_APPROVAL | `disclosure_missing` | repropose |
| `volume_ok` | `volume_conversation` | "The automatic-reply limit for this chat was reached." | REQUIRE_APPROVAL | `volume_cap` | card |
| | `volume_delegation` | "The automatic-reply limit for this delegation was reached." | REQUIRE_APPROVAL | `volume_cap` | card |
| | `loop_suspected` | "This chat looks like an automatic back-and-forth, so automatic replies stopped here." | REQUIRE_APPROVAL | `volume_cap` | card |
| `language_gate` | `language_unknown` | "The language of the chat or of the reply could not be identified." | REQUIRE_APPROVAL | `policy_denied` | card |
| | `language_mismatch` | "The reply's language does not match the chat's." | REQUIRE_APPROVAL | `policy_denied` | card |
| | `language_not_enabled` | "Automatic replies are not switched on for this language yet." | REQUIRE_APPROVAL | `policy_denied` | card |

The M04 send-precondition codes (`account_inactive` … `sending_disabled` in `rules.REASON_CODES`) keep their explanations and their DENY outcome. For `delegation_auto` at execution they map to `cancelled` with the reason of the M04 target (`takeover`, `new_contact_message`, `source_deleted`, `window_closed`, `max_age`, `consent_revoked`, `sending_disabled`, `too_long`, otherwise `policy_denied`), with no conversion. `inbox_drained` alone is still WAIT, and `already_claimed` still means "leave the action as it is".

### 3.3 `DelegationRules` (`automation/delegation_rules.py`, F0-S)

The persisted spec in `delegations.spec` (a content column). It never changes with prompts or tool schemas; a new shape is a new `schema_version` (ARCHITECTURE §7.5). `CreateDelegationArgs` (M6-DELEG, §9.5) is a separate model that maps onto it.

```python
class DelegationLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    per_conversation_hour: int = Field(ge=1, le=30)
    per_delegation: int = Field(ge=1, le=500)


class DelegationEscalation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    urgent_bypass_quiet_hours: bool = False  # OD-7: off unless ticked on the card


class DelegationRules(RuleBase):  # automation/rules.py::RuleBase
    schema_version: Literal[1] = 1
    conversation_ids: tuple[int, ...] = Field(min_length=1)  # explicit chats only (OD-7)
    topic: str | None = Field(
        default=None, min_length=1, max_length=200
    )  # owner words; a filter inside the chats
    allowed: tuple[Behaviour, ...] = Field(min_length=1)
    knowledge_ids: tuple[
        int, ...
    ] = ()  # saved shareable items attached on the card (owner/contact scope)
    knowledge_contact_ids: tuple[
        int, ...
    ] = ()  # "knowledge recipients" on the card; without a topic,
    # M6-DELEG sets every covered chat's contact
    share_widened: bool = False  # equals delegations.share_widened (written together)
    limits: DelegationLimits
    escalation: DelegationEscalation = DelegationEscalation()
    disclosure: dict[str, str] = {}  # GateLanguage value -> override; each passes validate_wording
```

Validators: duplicate ids in `conversation_ids`, `knowledge_ids` or `knowledge_contact_ids`, duplicate `allowed` values and a whitespace-only `topic` are refused; every `disclosure` key must be a `GateLanguage` value.

The forbidden behaviours of PRD §11.3 (commitments on dates, money or meetings not in knowledge; other conversations; media; sensitive topics) are fixed in code and in the prompt, not stored. A standing delegation (`kind = standing`, `ends_at IS NULL`) has exactly one conversation; M6-DELEG enforces it.

### 3.4 Rules (M6-POL), in `RULES` order after `delegation_active`

Each applies only to origin `delegation_auto` (otherwise it returns `None`), in both phases.

| Rule | Fails with | When |
|---|---|---|
| `autonomy_not_paused` (M04) | `autonomy_paused` | the account kill switch is on, or `ConversationSnapshot.autonomy_paused_reason` is set (`ingest_error`, or `takeover` when it counts, §3.1) |
| `delegation_active` | `no_delegation` · `delegation_ended` · `delegation_not_started` · `not_covered` | no snapshot · status not `active` or `now >= ends_at` · `now < starts_at` · `covers_conversation` false. The rule checks the times itself and never relies on a fire handler having run |
| `in_scope` | `out_of_scope` · `behaviour_not_allowed` | `topic_scoped and not in_scope` · `behaviour == "none"` or not in `allowed_behaviours` |
| `trigger_fresh` | `trigger_stale` | `trigger_message_sent_at is None` or `now - trigger_message_sent_at > max_trigger_age` |
| `draft_safe` | first of `low_confidence` · `unpermitted_information` · `sensitive_topic` · `makes_commitment` · `should_escalate` | `confidence != "high"` · flag false · flag true · flag true · `should_escalate`, or `escalation_category` in {urgent_decision, needs_owner, suspicious} |
| `content_grounded` | `ungrounded_value` | `ungrounded_values > 0` (on the model's body only; application texts are excluded) |
| `knowledge_scope` | `knowledge_not_shareable` | `knowledge_ids_used` is not a subset of `knowledge_ids_allowed` (an id that was never shown counts as not allowed) |
| `disclosure_present` | `disclosure_missing` | `disclosure_required and not disclosure_included` |
| `volume_ok` | first of `volume_conversation` · `volume_delegation` · `loop_suspected` | `counters is None` or `conversation_last_hour >= limit_per_conversation_hour` · `delegation_total >= limit_per_delegation` · `rapid_replies >= loop_reply_threshold` or `repeated_contact_message` |
| `language_gate` | first of `language_unknown` · `language_mismatch` · `language_not_enabled` | any of the three keys is `None` (mixed and uz-ru mix map to `None`, OD-14) · the keys differ · the key is not in `enabled_languages` |

- **Fail-closed readings** (M6-POL; stricter than the table, same outcomes): a fact or flag passes only on exactly `True` / `False` as its type says (the string `"false"` never passes); a negative counter or a negative `ungrounded_values` fails; `content_grounded` fails on `ungrounded_values != 0`; `draft_safe` fails `should_escalate` for any `escalation_category` outside {`fyi`, `none`}, so an unknown value fails; `delegation_active` fails with `not_covered` when `snapshot.account_id` differs from the context's account; `in_scope` and `volume_ok` fail when the delegation snapshot is missing; the completeness guard also applies at execution (a decision lacking a check for each of the ten rules is DENY). The identity-answer rule evaluates in this order: `autonomy is None` fails first, then `identity_answer is True` passes the three rules it exempts, then the rule's own conditions.
- **Bot chats** fail the M04 send precondition `conversation_owned` (`bot_chat`), so they are never auto-replied. The `volume_ok` loop guard covers contact-side automation (away messages, other business bots) that arrives as ordinary contact messages (TF §13 row, conflict 30) (OD-6).
- **Identity answers** (OD-10; TL-ruled R14): when the planner replaced the body with the fixed identity sentence (`identity_answer`), `in_scope`, `draft_safe` and `content_grounded` pass, because no model-written text is sent; every other rule applies, the language keys are the identity sentence's locale. An identity answer that is sent enqueues no escalation of its own (OD-10a); when the plan is not ALLOW_AUTO the identity question escalates instead (§4.1). This is not a breach of "`draft_safe` is never the sole basis for ALLOW_AUTO": the sent text is application text. No other flag lets a rule pass.
- **Model flags only downgrade.** The draft's flags (`confidence`, `uses_only_permitted_information`, `sensitive_topic`, `makes_commitment`, `should_escalate`, `in_scope`, `behaviour`, `escalation_category`) can turn ALLOW_AUTO into REQUIRE_APPROVAL, never the reverse. A rule computed by the application (`autonomy_not_paused`, `delegation_active`, `trigger_fresh`, `content_grounded`, `knowledge_scope`, `disclosure_present`, `volume_ok`, `language_gate`) never passes on the strength of a flag, and a missing or invalid flag reads as its failing value. `draft_safe` is never the sole basis for ALLOW_AUTO (ARCHITECTURE §9.3; named test).
- **Outcomes** (`decide`, M6-POL). Proposal phase, origin `delegation_auto`: any failed send-precondition check ⇒ DENY; otherwise any failed autonomy check ⇒ REQUIRE_APPROVAL; all pass ⇒ ALLOW_AUTO. Other origins are unchanged (REQUIRE_APPROVAL or DENY; never ALLOW_AUTO). The execution phase is unchanged (ALLOW_AUTO = may send now; WAIT only when `inbox_drained` alone failed; otherwise DENY).
- **Completeness.** An ALLOW_AUTO decision for `delegation_auto` holds a passing `CheckResult` for each of the ten `AUTONOMY_RULES` (a unit test asserts it; OPS-TV3 fails closed on a stored decision without them).
- `decide` stays total and pure; it reads nothing but its argument.

### 3.5 One fact builder (`actions/policy_loader.py`; F0-S stubs, M6-PLAN bodies; execution half M6-TXA)

```python
def delegation_snapshot(
    delegation: DelegationView,
    *,
    conversation_id: int,
    contact_id: int | None,
    config: AutonomyConfig,
) -> DelegationSnapshot | None: ...  # F0-S stub: None


async def rate_counters(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    delegation_id: int,
    now: datetime,
    config: AutonomyConfig,
) -> RateCounters | None: ...  # F0-S stub: None


async def autonomy_facts(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    delegation: DelegationView,
    text: str,
    draft_flags: Mapping[str, Any],
    base_message_id: int,
    now: datetime,
    config: AutonomyConfig,
) -> AutonomyFacts | None: ...  # F0-S stub: None
```

**F0-S ships the three builders as fail-closed stubs (R79):** each returns `None`, so the loader leaves the matching `PolicyContext` field `None` and every autonomy rule fails closed (§3.1). M6-PLAN replaces the bodies in `actions/policy_loader.py` (its owned file) and keeps the signatures; a `None` return stays legal and means "a fact cannot be computed". Code that decides for `delegation_auto` (M6-SCHED's `actions/release.py` re-check, M6-TXA's Tx A) calls these builders, never its own copy, so once M6-PLAN merges nothing else edits `release.py`: M6-PLAN has no touch point in `actions/release.py::_decide`. Named test (F0-S, J2): `test_the_fact_builder_stubs_fail_closed` in `tests/unit/test_policy_loader_stubs.py`; M6-PLAN replaces it with the real builders' tests.

The planner (side-effect-free), `propose()` and Tx A all build facts through these three functions, so the plan, the stored proposal decision and the claim decision see the same facts. `text` is the final text; the body is `disclosure.split_disclosure(text)[1]` (§5.1). At Tx A (M6-TXA) the facts are rebuilt from the stored text, the chat's prior autonomous sends (for `disclosure_required`) and the counters read under the delegation `FOR UPDATE`.

### 3.6 Execution targets for `delegation_auto` (M6-POL; applied by M6-TXA and M6-SCHED)

```python
def autonomous_execution_target(decision: Decision) -> tuple[StateReason, Conversion] | None:
    """None for ALLOW_AUTO, WAIT and already_claimed. Otherwise the first failed
    check in RULES order: an autonomy code gives (AUTONOMY_CODES[code].execution_reason,
    .conversion); a send-precondition code gives the M04 target's reason and NONE."""
```

`execution_target` (M04) stays as it is for the other origins. The callers (Tx A, release) move the action to `cancelled` with the returned reason. When the conversion is `CARD`, after commit they call `propose()` with origin `agent_proposal`, the same body (no disclosure line), the same `delegation_id`, `base_message_id` and `shown_through_message_id`, and `draft_flags` gaining `autonomy_codes`, `converted_from` (the cancelled action's id) and `presentation = "on_request"`; there is no model call. In the same transaction they enqueue the held reply's `autonomy_not_sent` alert (§11.2; dedupe `notsent:{new action id}`) instead of presenting the card (OD-11). When the conversion is `REPROPOSE` (only `disclosure_missing`, M6-TXA), after commit they call `propose()` with origin `delegation_auto`, `compose(body, …)` as the text and `draft_flags["reproposed_from"]`; it goes through the full proposal policy and the scheduling delay again (allowed by `ux_actions_delegation_base`, P3-schema R-1); if that proposal is not ALLOW_AUTO it becomes a held reply. There is at most one re-proposal per base message: an action that already carries `draft_flags["reproposed_from"]` and is cancelled with `disclosure_missing` again converts to a CARD, never to a second re-proposal (named test, M6-TXA; TL-ruled R10). Before the policy re-check, Tx A and the release job compare `now` with `late_after`: past it ⇒ `cancelled/late` + card conversion ("40-min outage → late + converted"). Send-precondition failures never convert: after a takeover, a pause or a closed window the owner gets no held reply and no `autonomy_not_sent` alert (OD-11).

Details frozen here (R32, R35, R36, R40):
- **First failed check.** `autonomous_execution_target` skips an `inbox_drained` failure when it picks the first failed check, and a send-precondition reason follows RULES order, not the M04 `_EXECUTION_TARGETS` priority (so `window_closed` plus `takeover` gives `(WINDOW_CLOSED, NONE)`; these are labels only and neither case converts). An autonomy failure next to a pending inbox update converts to a card; Tx A re-checks and a takeover then blocks the send (R36).
- **A cancelled identity answer never converts** (R40). Its text is the identity sentence, which starts with `AI_MARKER` and has no composed disclosure line, so a card would put an AI line into an owner-approved message (PRD §11.4). It escalates instead: since no escalation was enqueued at plan time (OD-10a), `convert_cancelled` enqueues the identity question's `delegation_escalation` under the plan's `deleg:` key (§4.1, M6-SCHED); the escalation alert's Draft reply button asks for a new owner-approved draft (§12.5, OD-11). The same holds for a row with NULL text.
- **An `approved` `delegation_auto` action with NULL `late_after`** (allowed by `ck_actions_schedule_pair`) is cancelled `policy_denied` with check `not_scheduled` and no card, at Tx A and at the release re-check.
- **Release re-check** evaluates the action as if it were `approved` (`ActionFacts.state = APPROVED`; otherwise `not_already_executed` fails every release); a WAIT (only `inbox_drained` failed) at release releases, because Tx A owns the ingestion barrier; "past `late_after`" reads `now >= late_after`; a DENY with no execution target (only `already_claimed`) ends `cancelled/policy_denied` with no conversion.
- **A re-proposal that finds the chat's slot held** by a newer in-flight autonomous action makes nothing more; the `action_converted` row (§13) names that action in `to_action_id`, so the conversion counts as done.

### 3.7 Grounding and language keys (`actions/grounding.py`, `actions/language_keys.py`; F0-S stubs, GRND bodies)

Pure modules: no database, no I/O. F0-S ships the signatures with **fail-closed stub bodies**, so M6-PLAN and EV-AUT build on D0 and every reply becomes a held reply until GRND merges. GRND replaces the bodies only; the signatures are frozen.

```python
# actions/grounding.py
@dataclass(frozen=True, slots=True)
class GroundingResult:
    checked: int  # values found in the text
    ungrounded: int  # values found in no source


def ground(text: str, *, sources: Sequence[str]) -> GroundingResult:
    """Every number group, amount, currency symbol or code, date or time expression
    (closed weekday, month and relative-day lexicons and spelled numbers 0-100 in
    uz-Latn, uz-Cyrl, ru and en), URL, email, phone number and username handle in `text`
    must appear, normalised, in one of `sources` (the shareable items shown and the
    delegation's own spec text). Contact messages and `assistant_sent` messages are never
    sources: a value only the contact supplied is not grounded. Over-blocking is the safe
    failure (R33): Unicode `Cf` (format) characters are removed from the text and every
    source first; values match by exact normalised key, never by substring; the "one" words
    (`bir`, `one`, `один`) count as values except inside a closed, TL-reviewed list of
    non-value phrases; only the bare Latin `may` needs a date context (an adjacent number or
    a closed date-preposition list), while `march`, Cyrillic май/мая/мае and suffixed uz month
    forms always count; a token whose script cannot be decided counts.
    F0-S stub: GroundingResult(checked=0, ungrounded=1) for every input."""


# actions/language_keys.py
def gate_key(lang_guess: str | None, script: str | None) -> GateLanguage | None:
    """(uz, latin) → uz-Latn; (uz, cyrillic) → uz-Cyrl; (ru, cyrillic) → ru;
    (en, latin) → en; anything else, "mixed" or None → None. F0-S stub: None."""


def text_key(text: str) -> GateLanguage | None:
    """gate_key of memory/search.py's guess_lang(text) and detect_script(text)
    (both pure). F0-S stub: None."""


def contact_key(tags: Sequence[tuple[str | None, str | None]]) -> GateLanguage | None:
    """The answered burst's key from each contact message's stored (lang_guess, script):
    the one key every message maps to, else None (mixed fails closed; a message without
    letters gives None). F0-S stub: None."""


def declared_key(tag: str) -> GateLanguage | None:
    """DelegatedDraftResult.language → key: a case-insensitive exact match on the four
    GateLanguage values, else None. F0-S stub: None."""
```

Josh writes the per-language token tables (lexicons, spelled numbers, uz-only and ru-only letters); the TL reviews them (specs/GRND.md). Named tests (GRND): "a contact-supplied value is not grounded"; "`None` or mixed fails closed"; per-language table tests. F0-S creates `tests/unit/test_grounding.py` and `tests/unit/test_language_keys.py` with the stub tests; GRND owns and replaces them afterwards. `memory/search.py::_RUSSIAN_ONLY_LETTERS` includes э/Э, which Uzbek Cyrillic also uses, so some Uzbek Cyrillic messages are tagged `ru`; that fails closed in the gate paths and is left unchanged (GRND pins the behaviour in a test and logs it as an M06 open item).

## 4. Ports

All protocols below are created by F0 with signatures only (or with a fail-closed stub where marked). A consumer never imports an implementation module of another team; it receives the implementation through `bootstrap.py`.

### 4.1 `AutonomyPlanner` (`actions/ports.py`, F0-S; implemented by M6-PLAN in `actions/autonomy_planner.py`)

```python
class PlanOutcome(StrEnum):
    ALLOW_AUTO = "allow_auto"  # every rule passed: delegation_auto (a shadow held reply while
    # autonomy.send is off)
    REQUIRE_APPROVAL = "require_approval"  # autonomy-only failure: held reply (agent_proposal)
    # plus an autonomy_not_sent alert (OD-11)
    DENY = "deny"  # a send precondition failed: held reply; the M04 proposal policy decides
    # awaiting_approval or blocked (copy-ready)
    ESCALATE = "escalate"  # drafted with escalation_category == suspicious: no card, nothing is
    # proposed; a delegation_escalation notification only (PRD §11.5)
    NO_REPLY = "no_reply"  # the draft says no reply is needed
    NOT_DRAFTED = (
        "not_drafted"  # no covering delegation, no consent, over budget, refused, invalid,
    )
    # truncated or failed: nothing reaches propose


@dataclass(frozen=True, slots=True)
class AutonomyPlan:
    outcome: PlanOutcome
    reason_codes: tuple[str, ...]  # failed policy codes in RULES order; () for ALLOW_AUTO
    delegation_id: int | None
    final_text: str | None  # what a delegation_auto action would carry: disclosure line + body,
    # or the identity sentence; None when nothing is proposed
    body: str | None  # the model's body only: what a held reply's card shows
    disclosure_added: bool
    identity_answer: bool
    escalation_category: EscalationCategory
    escalates: bool  # R87: the plan enqueues a delegation_escalation; computed once by the planner
    base_message_id: int | None
    shown_through_message_id: int | None
    draft_run_id: int | None
    draft_status: DelegatedDraftStatus
    draft_facts: AutonomyFacts | None  # the facts decide() saw
    decision: Decision | None  # the proposal-phase decision the outcome is based on


class AutonomyPlanner(Protocol):
    async def plan(
        self, *, account_id: int, conversation_id: int, now: datetime
    ) -> AutonomyPlan: ...
```

- **No side effects.** `plan` opens its own session and only reads domain rows. Its only writes are the delegated draft run's own `agent_runs` and `llm_calls` rows (AGENTS §8 metering). It creates no action, notification, job, audit row or Telegram call. Expected outcomes are values; infrastructure errors propagate. This is the S06.5 eval seam: EV-AUT calls `plan` against a synthetic account.
- Steps (M6-PLAN): `active_covering` → `DelegatedDrafter.draft` → facts through §3.5 → final text (§5) → `decide` with origin `delegation_auto`, phase `proposal` → outcome.
- **The `replies.delegated` job (M6-PLAN) applies the plan:** ALLOW_AUTO with `autonomy.send` on ⇒ `propose()` with origin `delegation_auto`, which re-decides under the locks, then, in the same transaction, M6-SCHED's `actions/release.py::schedule_autonomous(session, *, account_id: int, action_id: int, now: datetime, config: AutonomyConfig) -> bool` performs `proposed → approved → scheduled` (`scheduled_for = now + AUTONOMY_SEND_DELAY_SECONDS`, `late_after = scheduled_for + AUTONOMY_MAX_LATENESS_SECONDS`; `True` when the action is now `scheduled`), and the job defers `actions.release_scheduled` after commit. M6-SCHED merges before M6-PLAN (§15.5), so M6-PLAN depends on it (R18). If the re-decision is not ALLOW_AUTO, `propose` inserts nothing and refuses with `autonomy_downgraded`, and the job proposes the held reply. ALLOW_AUTO with `autonomy.send` off ⇒ held reply with `draft_flags["shadow"] = "allow_auto"` and the decision recorded. REQUIRE_APPROVAL or DENY ⇒ held reply (the M04 proposal policy blocks it with copy-ready text when a send precondition fails). Every held reply carries `draft_flags["presentation"] = "on_request"` and gets one `autonomy_not_sent` alert in the same transaction; the job does not present its card (§2.3, OD-11). ESCALATE ⇒ notification only. Whatever the outcome, an `escalation_category` other than `none` also enqueues `delegation_escalation` (M6-ESC decides delivery). An identity answer that is sent adds no escalation (OD-10a); an identity question whose sentence is not sent (the plan is not ALLOW_AUTO, or the language is not enabled) escalates instead, with the draft's category, or `fyi` when the draft gave `none` (R14, OD-10).
- **Escalations.** Whatever the outcome, an `escalation_category` other than `none` enqueues `delegation_escalation`; an identity question escalates when its sentence is not sent (R14, OD-10), and an identity answer on an ALLOW_AUTO plan adds none (OD-10a, decisions §A2; if the job then holds it, in shadow mode or after a downgrade under the locks, the owner hears through the held reply's `autonomy_not_sent` alert); and a `NOT_DRAFTED` plan whose draft status is `invalid`, `refused` or `failed` enqueues a `needs_owner` escalation (PRD §15); together these are exactly `AutonomyPlan.escalates` (R87). `refs.alert_kind` carries the category (§11.2, R60).
- **`AutonomyPlan.escalates` (R87)** is the one shared escalation predicate. The planner computes it once (a private function in `actions/autonomy_planner.py`): true exactly when the plan's final `escalation_category` is not `none`. That covers every drafted outcome with a category other than `none`, an identity question whose sentence is not sent (category `fyi` at least, R14; an identity answer on an ALLOW_AUTO plan keeps the draft's category, OD-10a) and a `NOT_DRAFTED` plan whose draft status is `invalid`, `refused` or `failed` (category `needs_owner`). The `replies.delegated` job decides whether to enqueue `delegation_escalation` from this field only, and EV-AUT counts escalations from it, so the job and the eval cannot drift. `FakeAutonomyPlanner` plans carry it like any other field.
- **`AutonomyPlan.body`** is set for every drafted outcome, including `ESCALATE` and `DENY`, and is `None` for `NO_REPLY` and `NOT_DRAFTED`.
- **Constructor.** The implementation is `actions/autonomy_planner.py::DelegatedAutonomyPlanner(*, session_factory, drafter: DelegatedDrafter, actions_config: ActionsConfig, autonomy_config: AutonomyConfig)`, frozen here so EV-AUT can build the real planner outside `bootstrap.py`.
- `FakeAutonomyPlanner` (`tests/fakes/autonomy.py`, F0-S) returns a scripted `AutonomyPlan` per conversation.

### 4.2 `DelegatedDrafter` (`actions/ports.py`, F0-S; implemented by DELEG-DRAFT in `agent/delegated_draft.py`)

```python
class DelegatedDraftStatus(StrEnum):  # mirrors agent/drafting.DraftStatus
    DRAFTED = "drafted"
    NOT_NEEDED = "not_needed"
    NOT_DRAFTABLE = "not_draftable"
    NO_CONSENT = "no_consent"
    OVER_BUDGET = "over_budget"
    REFUSED = "refused"
    INVALID = "invalid"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DelegatedDraft:
    """A domain value: no agent or pydantic types cross the port. The defaults
    are the failing values, so a partly built draft can only downgrade."""

    status: DelegatedDraftStatus
    run_id: int | None = None
    base_message_id: int | None = None  # newest contact message of the answered burst
    shown_through_message_id: int | None = None
    language: str | None = None  # the model's tag (first schema field)
    body: str | None = None  # 1..4096 chars after normalisation; never a disclosure line
    confidence: str = "low"
    uses_only_permitted_information: bool = False
    sensitive_topic: bool = True
    makes_commitment: bool = True
    should_escalate: bool = True
    in_scope: bool = False
    behaviour: str = "none"
    identity_question: bool = False
    knowledge_ids_used: tuple[
        int, ...
    ] = ()  # kept as given: an id that was not shown fails knowledge_scope
    escalation_category: str = "none"
    error_class: str | None = None


class DelegatedDrafter(Protocol):
    async def draft(
        self, *, account_id: int, conversation_id: int, delegation_id: int, now: datetime
    ) -> DelegatedDraft: ...
```

A status other than `DRAFTED` never reaches `propose` (named test: "invalid, refused or truncated output never reaches propose"). `FakeDelegatedDrafter` (F0-S) returns scripted drafts.

The implementation is `agent/delegated_draft.py::DelegatedDraftRunner(*, session_factory, llm: LLMClient, clock: Clock, config: DraftConfig)` (`DraftConfig` from `agent/drafting.py`, built from the `draft_reply` route and effort; when `LLM_ROUTE_DRAFT_DELEGATED_MODEL` is set, its model replaces the route's model, §10.1, R16). It is a contact-triggered run (§9.1): run type `draft_reply`, prompt key `draft_delegated`, `tools=()`, context from `agent/delegated_context.py` = one chat + that contact's notes (never items with `kind = 'shareable'`, which reach the prompt only through `shareable_for`, R54; each `ShareableItem` with `tainted` true is wrapped as untrusted, R38) + owner reply preferences + the delegation's rules (owner text) + `shareable_for` items as `[knowledge:<id>]` + the latest triage reasons wrapped as untrusted (invariant 4). The delegation's times and kind are not shown to the model (R58). M6-PLAN instantiates it in `_wire_autonomy` (§15.3).

### 4.3 `AutomationTrigger` and the `propose` signature (`actions/ports.py`, `actions/proposals.py`; F0-S signature)

```python
@dataclass(frozen=True, slots=True)
class AutomationTrigger:
    automation_id: int
    occurrence_at: int  # epoch seconds UTC


async def propose(
    session: AsyncSession,
    request: ProposalRequest,  # gains delegation_id: int | None = None (§2.3)
    *,
    clock: Clock,
    config: ActionsConfig,
    trigger: AutomationTrigger | None = None,
    autonomy: AutonomyConfig | None = None,  # required for origin delegation_auto
) -> ProposalResult: ...
```

- **F0-S:** any `trigger` ⇒ `ProposalRefused("trigger_not_available")` (fail closed). Origin `delegation_auto` without `autonomy` ⇒ `ProposalRefused("autonomous_not_available")`. `ProposalRefused` gains an optional `decision: Decision | None = None` attribute (backward compatible), so the held reply after `autonomy_downgraded` records the re-decision's codes.
- **NUDGE-POL:** a trigger is accepted only with origin `agent_proposal` (any other origin keeps `trigger_not_available`). It is also refused (`trigger_not_available`; a foreign or deleted automation raises `NotInAccountError`) when the request carries a `delegation_id`, when the trigger automation is not a `follow_up` of the same account, or when its `refs` do not name the request's conversation; and any ALLOW_AUTO decision for a trigger is refused (fail closed). It satisfies `base_required`, sets `actions.trigger_automation_id`, and the loader uses the action's `created_at` as the `no_takeover_since` reference (D9: an owner "ok" sent after the promise does not block the nudge). A live card with the same `trigger_automation_id` is reused, not superseded (a retried nudge job makes no second card). Never `delegation_auto` (OD-18).

### 4.4 `PollObserver` (`telegram/ingress/poller.py`, F0-S hook; implemented by OPS-AV)

```python
POLL_OBSERVER_TIMEOUT_SECONDS = 2


class PollObserver(Protocol):
    async def on_poll_ok(self, at: datetime) -> None: ...
```

`Poller` gains `poll_observer: PollObserver | None = None`. After the `_mark_poll_ok` transaction has committed, and outside it, the poller calls `on_poll_ok(at)` inside `asyncio.timeout(POLL_OBSERVER_TIMEOUT_SECONDS)` and its own `try/except Exception` (the one sanctioned broad catch, with a comment saying why): a failure or timeout logs `poll_observer_failed` with the exception class name only, is never re-raised and is not retried in that poll. The observer opens its own session and transaction (OPS-AV: one upsert per poll into `availability_minutes`, `ON CONFLICT (minute) DO UPDATE SET polls = availability_minutes.polls + 1` — the table-qualified form; a bare `polls` is ambiguous with `excluded` — P3-schema §2.3). Named test (F0-S): "a recorder failure never blocks or delays a poll and never rolls back `last_poll_ok`".

### 4.5 Cross-team ports (`core/p3_ports.py`, F0-R)

```python
if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PortResult(StrEnum):
    DONE = "done"
    ALREADY = "already"  # already in that state (idempotent repeat)
    NOT_FOUND = "not_found"  # unknown id or another account's (RG-21)
    INVALID = "invalid"  # validation failed (e.g. empty wording, past snooze time)


class DelegationControls(Protocol):  # M6-DELEG; wired in _wire_autonomy
    async def end(
        self, session: AsyncSession, *, account_id: int, delegation_id: int, now: datetime
    ) -> PortResult: ...  # reason owner_ended
    async def set_disclosure_wording(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        delegation_id: int,
        locale: str,
        text: str,
        now: datetime,
    ) -> PortResult: ...


class AutomationControls(Protocol):  # M7-AUTO; wired in _wire_automation
    async def pause(
        self, session: AsyncSession, *, account_id: int, automation_id: int, now: datetime
    ) -> PortResult: ...
    async def resume(
        self, session: AsyncSession, *, account_id: int, automation_id: int, now: datetime
    ) -> PortResult: ...
    async def delete(
        self, session: AsyncSession, *, account_id: int, automation_id: int, now: datetime
    ) -> PortResult: ...  # owner delete = hard delete (OD-19)


class TaskControls(Protocol):  # M7-TASK; wired in _wire_automation
    async def done(
        self, session: AsyncSession, *, account_id: int, task_id: int, now: datetime
    ) -> PortResult: ...
    async def snooze(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        task_id: int,
        until: datetime,
        now: datetime,
    ) -> PortResult: ...
    async def reopen(
        self, session: AsyncSession, *, account_id: int, task_id: int, now: datetime
    ) -> PortResult: ...  # owner only; triage never reopens


class CatchUpRequester(Protocol):  # M6-CATCH; wired in _wire_surfaces
    async def request(
        self, session: AsyncSession, *, account_id: int, delegation_id: int, now: datetime
    ) -> None: ...  # enqueues delegation_catch_up


class DelegationRequester(Protocol):  # M6-PLAN; wired in _wire_autonomy
    async def request(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        conversation_id: int,
        covers_through_message_id: int,
        now: datetime,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class P3Ports:
    delegation_controls: DelegationControls | None = None
    automation_controls: AutomationControls | None = None
    task_controls: TaskControls | None = None
    catch_up_requester: CatchUpRequester | None = None
    delegation_requester: DelegationRequester | None = None
```

- **No-op default = `None` = not wired.** A consumer hides the row or button whose port is `None`; a press that arrives for an unwired port (an old card) answers "Not available right now" and changes nothing; a requester that is `None` is skipped, so behaviour stays as in P2. Nothing reports success through an unwired port (invariant 11). This removes merge-order coupling between teams.
- Every method runs in the caller's transaction: no commit, no Telegram call, no model call; anything visible is an after-commit effect (`db.uow.after_commit`) or a notification row. Ids are checked against `account_id` first.
- `DelegationControls.end` is never flag-gated: it works with `autonomy.run` off (ending is always safe).
- `DelegationRequester.request` returns `True` only when an active delegation covers the chat **and** it registered the delegated path (`pending_delegation_at` set, `replies.delegated` deferred after commit). On `True`, M6-ESC skips the Assist trigger for that burst (one draft path per burst). With `autonomy.run` off it returns `False` and does nothing. The F0-R hook in `inbox/triage_store.py` calls it next to `draft_requester` and ignores the result until M6-ESC.
- `P3Ports` is assembled by `bootstrap.py` from the three wire functions in the order autonomy → automation → surfaces (§15.3); a consumer reads it, never writes it. Because `catch_up_requester` comes from `_wire_surfaces`, after the units that need it (M6-DELEG, M6-LIFE) have wired, `P3WireContext.final_ports()` is a late-bound accessor to the merged `P3Ports`: it raises before all three wire functions have run, and returns the merged ports afterwards. Tools read the same value through `ToolServices.p3_ports` (§9.5).
- `AutomationControls` returns `NOT_FOUND` for a `delegation_end` automation, and no list or control shows one (R51).
- The `rem` "Pause reminder" press reaches `AutomationControls.pause` through the `reminder_due` notification's `refs.automation_ids` (R21).

### 4.6 Control-chat documents (GW-DOC)

```python
# core/channel.py — ControlChannel gains (named touch point for GW-DOC)
async def send_document(
    self, *, account_id: int, chat_id: int, filename: str, data: bytes, caption: str | None = None
) -> Delivery: ...


# telegram/gateway.py — OutboundGateway gains
async def send_document(
    self, target: ControlChat, *, filename: str, data: bytes, caption: str | None = None
) -> (
    SendResult
): ...  # every gateway method returns SendResult; GatewayControlChannel converts it to Delivery
```

`ControlChat` only (there is no business-target variant). Throttled and classified like `send`; a part over 50 MB (TF §6) or a caption over 1024 characters is refused locally without a Telegram call; the filename and caption are never logged; an uncertain `Delivery` is never resent (M7-EXP reports each part's outcome). `FakeChannel` gains `documents`, `document_bytes`, `document_script` and `on_document`. `gateway.py` already has its TID251 per-file ignore, so GW-DOC adds banned-API entries in `pyproject.toml` for `aiogram.types.BufferedInputFile`, `FSInputFile`, `URLInputFile` and `aiogram.types.input_file` (with a per-file ignore for `gateway.py`) and extends the AST confinement test; the TL adds the TF §13 docs-check row. **GW-ESC: landed in P2 #133; verification only.** The gateway double-escape fix and the multi-chunk partial-delivery outcome (P2-T53) are on `main` (`telegram/render.py`, `telegram/gateway.py` and their tests). F0-C and QA confirm that #133 covers P2-T53 and record the escape-once rule; a gap they find becomes J3's first `int/p3` PR. Every P3 renderer follows the escape-once rule, and no P3 renderer merges before GW-ESC is closed (R39; the text is GW-ESC §3.3 as final):
1. A renderer escapes each untrusted field exactly once, before it adds its own markup: `telegram.render.escape(value)` inside `assistant.telegram`, `html.escape(value, quote=False)` in every other package (`inbox`, `automation`, `memory`, `agent` may not import `assistant.telegram`; existing `html.escape(value)` calls are also correct). Application markup is written raw, never escaped, in balanced pairs of supported tags; anything else stays literal.
2. Never escape twice, never unescape, never pre-escape for the gateway. Approval cards keep their extra defusing step and then escape once. Business sends are plain text: no escaping, no parse mode, never split.
3. Test rule: every P3 renderer has `test_<renderer>_escapes_untrusted_text_once`, rendering a synthetic name that contains `&`, `<b>x</b>`, `<a href="https://example.invalid/">y</a>` and the literal `&amp;` through `telegram.render.sanitize`; an unescaped untrusted field is a gap even with no application markup around it.

## 5. Disclosure (`actions/disclosure.py`, `core/autonomy_texts.py`; F0-S)

Pure modules (no I/O). The model never writes the disclosure: the application inserts it into the same message (ARCHITECTURE §6.8: one action = one `sendMessage`), and `draft_delegated` instructs the model never to add one (ADR-17). Table tests here are the unit half of RG-14.

### 5.1 `actions/disclosure.py`

```python
DISCLOSURE_GAP = timedelta(hours=24)
MAX_WORDING_LENGTH = 200


@dataclass(frozen=True, slots=True)
class PriorAutonomousSend:
    """One earlier delegation_auto action of this chat; facts only."""

    state: ActionState
    sent_at: datetime | None  # the confirmed send time when succeeded; None otherwise
    disclosure_included: bool


class WordingProblem(StrEnum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    MULTILINE = "multiline"
    MARKUP = "markup"  # HTML or Markdown control characters


def compose(body: str, *, locale: GateLanguage, wording: str | None) -> str:
    """f"{AI_MARKER} {wording or DISCLOSURE_DEFAULTS[locale]}\\n\\n{body}". `wording` is the
    delegation's override for `locale` (DelegationRules.disclosure), already validated."""


def split_disclosure(text: str) -> tuple[bool, str]:
    """(True, body) when `text` starts with a line composed above; (False, text) otherwise."""


def includes_disclosure(text: str) -> bool:
    """`text` starts with AI_MARKER. Identity answers start with it too."""


def validate_wording(text: str) -> WordingProblem | None:
    """None when the owner's wording is usable. AI_MARKER is not part of the wording:
    compose always prepends it, so it can never be removed (OD-4). Checked in the order
    EMPTY (whitespace or Unicode Cf characters only), TOO_LONG, MULTILINE, MARKUP (any Cf
    character, or any of < > & * _ ` [ ] ~). `compose` falls back to the default line when
    an override fails this check and never raises."""


def disclosure_required(
    prior_autonomous_sends: Sequence[PriorAutonomousSend], *, session_start: datetime, now: datetime
) -> bool: ...


def identity_answer(locale: GateLanguage) -> str:
    """f"{AI_MARKER} {IDENTITY_ANSWERS[locale]}" — the fixed truthful sentence (OD-10)."""
```

**The 24 h re-disclosure reading** (conflict 3; OD-4, OD-32). Consider only prior sends of this chat with state `succeeded` and `sent_at >= session_start`; `delivery_uncertain`, `failed` and `cancelled` count neither as disclosed nor as activity ("uncertain counts as not disclosed"). Sort them by `sent_at`. Disclosure is **required** when there are none; or when `now - last.sent_at >= DISCLOSURE_GAP`; or when the current run (the longest tail of the list in which every gap between consecutive sends is below `DISCLOSURE_GAP`) holds no send with `disclosure_included`. Otherwise it is not required. `session_start` is the delegation's `starts_at`; for a standing delegation the gap rule makes each run of replies a session (ARCHITECTURE §9.3). Exactly 24 h counts as idle (re-disclose). The required table tests: first send in the session; a second send 1 h after a disclosed succeeded send (not required); only an uncertain earlier send (required); a 24 h gap (required); a disclosed send before `session_start` (required); a disclosed send 30 h ago followed by sends every 20 h (not required).

**Identity-answer lookup** (OD-10). When the draft sets `identity_question`, the planner uses `identity_answer(locale)` as the final text, where `locale` is the chat's `contact_language`; this is possible only when that key is an enabled `GateLanguage`. Otherwise the draft goes through the normal path (the language gate then gives a held reply). The identity question escalates whenever its sentence is not sent (§4.1, R14); an identity answer on an ALLOW_AUTO plan enqueues no escalation of its own (OD-10a). The identity answer is application text: it is excluded from grounding and counts as disclosed.

### 5.2 `core/autonomy_texts.py`

```python
class GateLanguage(StrEnum):
    UZ_LATN = "uz-Latn"
    UZ_CYRL = "uz-Cyrl"
    RU = "ru"
    EN = "en"


AI_MARKER: Final[
    str
]  # fixed neutral marker the TL drafts; the owner decides on an emoji (OD-4, R24)
DISCLOSURE_DEFAULTS: Mapping[GateLanguage, str]  # one short neutral line per language (OD-4)
IDENTITY_ANSWERS: Mapping[GateLanguage, str]  # one truthful sentence per language: an AI assistant
# answering for the owner, offering to pass the message on (OD-10)

PAUSE_CALLBACK_DATA: Final[str] = "pz:pause:p"  # kind `pz`, the fixed token `pause`, op `p` (R45)


def pause_button() -> (
    Button
): ...  # pure; label "⏸ Pause automatic replies", callback_data PAUSE_CALLBACK_DATA
```

`pause_button()` is the one builder every renderer uses (R45): the escalation, catch-up, takeover and volume-cap renderers (in `inbox/`, which may not import `assistant.telegram`) and the agent confirmation cards call it directly, so the pause-button release check (LIVE-P3, before enable 3) names one builder. `core` imports no other assistant package, so `Button` comes from `core/channel.py`. M6-PAUSE owns the `pz` press (§12.3) and shares `PAUSE_CALLBACK_DATA` with the fast lane and the handler. The token is never checked.

Each mapping has exactly the four keys (test). The texts contain no names, numbers, links or placeholders. The TL drafts `AI_MARKER`, the four disclosure lines and the four identity answers in F0-C part 2; the owner proofreads them before deploy #1 (OD-4, OD-10, R24); a later change needs the owner's OK. `GateLanguage` lives in `core` so that `config.py`, `policy`, `actions` and `agent` can all import it.

## 6. Read seams (F0-S)

Both are read-only, account-scoped and fail closed. They carry RG-21 tests (a foreign id returns nothing) and, for `shareable_for`, RG-09 tests (decoy delegation and decoy chat with canaries).

### 6.1 `automation/delegations_repo.py`

```python
@dataclass(frozen=True, slots=True)
class DelegationView:
    delegation_id: int
    account_id: int
    kind: str  # "time_bounded" | "standing"
    status: str  # "active" | "ended" | "cancelled"
    starts_at: datetime
    ends_at: datetime | None
    confirmed_at: datetime
    ended_at: datetime | None
    ended_reason: str | None
    share_widened: bool
    conversation_ids: tuple[int, ...]  # live delegation_conversations rows (ended_at IS NULL)
    rules: DelegationRules


async def active_covering(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    now: datetime,
    for_update: bool = False,
) -> DelegationView | None: ...


async def list_active(
    session: AsyncSession, *, account_id: int, now: datetime
) -> list[DelegationView]: ...


async def list_current(
    session: AsyncSession, *, account_id: int, now: datetime
) -> list[DelegationView]: ...  # R89


async def get(
    session: AsyncSession, *, account_id: int, delegation_id: int
) -> DelegationView | None: ...
```

- `active_covering` returns a delegation only when `status = 'active'`, `starts_at <= now`, `ends_at IS NULL OR now < ends_at`, and a live `delegation_conversations` row links it to the chat (one live delegation per chat, partial UNIQUE). Anything else returns `None`. It checks the times itself, so an end occurrence that has not fired yet never extends coverage.
- A stored spec that no longer validates returns `None` (`list_active` skips it) and logs `delegation_spec_invalid` with the delegation id only. A `spec` that is NULL (scrubbed by a deletion, P3-schema §2.1; only possible on an ended or cancelled row) also makes `get` return `None`, without a log line. These functions never raise for either case.
- `for_update=True` locks the delegation row `FOR UPDATE`; Tx A and the release job use it after the account and conversation locks (ARCHITECTURE §9.4 order). The default takes no lock.
- `list_active`: the same predicate without the chat, ordered by `starts_at`, `id`. `get`: any status (status, catch-up, end).
- **`list_current` (R89, F0-S, for SET-P3):** the delegations that are active **or not yet started**: `status = 'active'` and `ends_at IS NULL OR now < ends_at`, with no `starts_at` condition, so a delegation the owner confirmed for later is still listed and can be ended. Same ordering, same invalid-spec skip and same account scoping as `list_active`. It is for display and ending only: coverage decisions use `active_covering` or `list_active`, never this list.
- **The one end primitive** lives beside the reads (J1, P3-schema §7.1): `end_delegations(session, *, account_id: int, delegation_ids: Sequence[int], reason: str, now: datetime) -> list[int]`. It ends the active ones (`status` = `cancelled` before `starts_at`, else `ended`), ends their live chat links, cancels their pending autonomous actions with `delegation_ended` (`lifecycle.cancel_pending_for_delegations`), deletes their delegation-scope knowledge, writes one `delegation_ended` audit row each (§13), and returns the ids it actually ended (idempotent). The caller holds the §9.4 locks and owns the transaction. It never enqueues anything.
- **Normal ends** (`owner_ended`, `superseded`: M6-DELEG; `time_elapsed`, `consent_revoked`, `connection_disabled`: M6-LIFE) call `end_delegations` and then `P3Ports.catch_up_requester.request(...)` for each returned id (skipped while unwired). Deletion and restore end through the same primitive and never request a catch-up. Creation, confirmation, supersede and wording writes live in `automation/delegations.py` (M6-DELEG). The confirm transaction inserts the delegation, its `delegation_conversations` rows and, for a time-bounded delegation, its `delegation_end` automation (`kind = 'delegation_end'`, `run_at = ends_at`, spec `DelegationEndRule`, `refs` = the delegation's `refs`, so deletion finds it, P3-schema §7.2); a standing delegation has none. An early end leaves that automation to fire as a no-op. Supersede follows P3-schema §2.2 (the older delegation's live links and pending autonomous actions in the shared chats end first).

### 6.2 `memory/shareable.py`

```python
@dataclass(frozen=True, slots=True)
class ShareableItem:
    memory_id: int
    text: str  # memory_items.content
    scope: str  # "delegation" | "owner" | "contact" | "conversation"
    tainted: bool  # provenance tainted or managed: only owner-confirmed contact text is
    # ever returned with it true, and the delegated context wraps it as untrusted (R38)


async def shareable_for(
    session: AsyncSession,
    *,
    account_id: int,
    delegation: DelegationView,
    conversation_id: int,
    limit: int = 30,
) -> list[ShareableItem]: ...
```

Returns only rows with `kind = 'shareable'`, `status = 'active'` and this account, that are either (a) `scope = 'delegation'` with this `delegation_id`, or (b) listed in `delegation.rules.knowledge_ids` with `scope = 'owner'`, `scope = 'contact'` for this chat's contact, or `scope = 'conversation'` for this chat (P3-schema §3.2, OD-8). Then:
- **`knowledge_scope`:** when `rules.topic` is set and the chat's contact is not in `rules.knowledge_contact_ids` and `share_widened` is false, the result is empty.
- **Refs-elsewhere exemption** (conflict 15): an item whose provenance names another conversation or contact, or is tainted or managed, is excluded unless its provenance marks owner-confirmed exact text (`provenance["owner_confirmed_exact"] = true`, set only by M6-DELEG when the owner confirmed the exact text on a card; the boolean key P3-schema §3.2 refers to).
- **Fail closed:** `delegation.account_id != account_id`, `delegation.status != 'active'` (R38; M6-CATCH does not use `shareable_for`), or `conversation_id` not in `delegation.conversation_ids`, returns an empty list. `pending_review`, archived and other delegations' items are never returned.
- Order: delegation-scope items first, then by id; capped at `limit`.
- Users: DELEG-DRAFT (context, each item shown as `[knowledge:<id>]`), M6-PLAN (`knowledge_ids_allowed`), EV-AUT (canaries).
- **Shareable text reaches a prompt only here** (R54): `memory/items.py::contact_facing` excludes `kind = 'shareable'` for every run (Assist, `extract_memory`, summary, draft and delegated runs), an F0-S touch point (§1.2), so `knowledge_ids` and `knowledge_scope` cannot be bypassed.

## 7. Jobs (`core/jobs.py` constants by F0-S; registrations by F0-R; bodies by the owning units)

### 7.1 Task catalogue (P3 additions)

| Constant | Task name | Args (ids/enums only) | Queue | Lock / queueing lock | Scheduling | Retry | Owner |
|---|---|---|---|---|---|---|---|
| `DELEGATED_TASK` | `replies.delegated` | `conversation_id: int` | `llm` | qlock `deleg:{conversation_id}`; **no lock** | `schedule_at` = now + `AUTONOMY_DEBOUNCE_SECONDS`; trailing debounce by re-defer | none (reconciler via `pending_delegation_at`) | M6-PLAN |
| `RELEASE_TASK` | `actions.release_scheduled` | `action_id: int` | `outbound` | qlock `release:{action_id}`; **no lock** | `schedule_at` = `scheduled_for` | DB-transient errors only, before the release transaction commits; never after; never `retry=True` | M6-SCHED |
| `NUDGE_TASK` | `followups.nudge` | `automation_id: int`, `occurrence_at: int` (epoch s) | `llm` | qlock `nudge:{automation_id}:{occurrence_at}` | immediate; deferred after commit by the follow-up deadline handler | none | M7-NUDGE |
| `SCHEDULED_SUMMARY_TASK` | `automation.summarize` | `automation_id: int`, `occurrence_at: int` | `llm` | qlock `ssum:{automation_id}:{occurrence_at}` | immediate; the fire handler defers it inside the fire transaction in a SAVEPOINT (the alternative the paragraph below allows; no domain flag or reconciler needed) | none (an at-most-once claim, audit `scheduled_summary_claimed`, before the send) | M7-SSUM |
| `EXTRACT_MEMORY_TASK` | `memory.extract` | `conversation_id: int` | `llm` | qlock `mem:{conversation_id}`; **no lock** | `schedule_at` = now + `MEMORY_EXTRACT_DEBOUNCE_MINUTES`; trailing debounce | none (reconciler via `pending_memory_at`) | M7-MEX |
| `EXPORT_TASK` | `privacy.export` | `account_id: int`, `card_id: int` (the confirm card's `pending_inputs` id) | `llm` (as `privacy.delete_data`) | lock `acct:{account_id}:control`; qlock `export:{account_id}` | immediate, on the confirm press | `RequeueOnShutdown` (as `privacy.delete_data`); never retried; an uncertain part is never resent | M7-EXP |
| `SURFACE_COMMITMENTS_TASK` | `system.surface_due_commitments` | periodic | `system` | qlock = name | every 5 min (`*/5 * * * *`) | — | shell F0-R; handler COMMIT-DUE |
| `WEEKLY_RATING_TASK` | `system.weekly_rating` | periodic | `system` | qlock = name | hourly (`41 * * * *`); the handler enqueues the card when the owner-local time is Sunday 19:00 or later (the first hourly run at or after 19:00), at most one per account per ISO week (R23) | — | shell F0-R; handler OPS-RATE |
| `DELEGATION_HEALTH_TASK` | `system.delegation_health` | periodic | `system` | qlock = name | every minute | — | M6-ESC |

There is no periodic task for memory review cards: `memory.extract` enqueues them, at most 5 per account per owner-local day (§11.2, R19).

Existing tasks P3 changes but does not rename: `automation.fire` dispatches through the fire registry (§8.1); `actions.execute` gains the autonomy Tx A (M6-TXA); `system.reconcile_pending` gains the P3 reconcilers (§7.3).

```python
# core/jobs.py additions (F0-S)
def delegated_queueing_lock(conversation_id: int) -> str: ...  # "deleg:{conversation_id}"
def release_queueing_lock(action_id: int) -> str: ...  # "release:{action_id}"
def nudge_queueing_lock(automation_id: int, occurrence_at: int) -> str: ...  # "nudge:{a}:{o}"
def scheduled_summary_queueing_lock(
    automation_id: int, occurrence_at: int
) -> str: ...  # "ssum:{a}:{o}"
def extract_memory_queueing_lock(conversation_id: int) -> str: ...  # "mem:{conversation_id}"
def export_queueing_lock(account_id: int) -> str: ...  # "export:{account_id}"


class DraftReason(StrEnum):
    # ... the five P2 members ...
    DELEGATION = "delegation"  # the delegated draft run's trigger; never a replies.draft argument
    FOLLOW_UP_NUDGE = "follow_up_nudge"  # M7-NUDGE's Drafter reason
    ALERT_DRAFT = "alert_draft"  # the owner's Draft reply press on an alert with no stored
    # text (OD-11); a replies.draft argument with REDRAFT's semantics
```

**`ALERT_DRAFT` (OD-11).** The `drf` press (§12.3) asks for one draft with `actions/drafts.py::request_draft(..., reason=DraftReason.ALERT_DRAFT)` when the alert names no stored text. It is a job reason (F0-S adds it to `JOB_REASONS`) on the unflagged path, exactly as `REDRAFT`: no `pending_draft_at` flag and no debounce; the defer is made inside the press transaction in a SAVEPOINT with the `redraft:{conversation_id}` queueing lock, so a second press while one is queued rolls back only the savepoint. `agent/drafting.py::_skip_code` treats it as `REDRAFT` (any mode but Ignore, any Assist trigger; the reply window must be open). The job re-checks consent (`draft_consent_ok`) and the budget when it runs (invariant 9), drafts with the normal `draft_reply` prompt and tool-less run (invariant 3a), and proposes an ordinary `agent_proposal` card through `propose_draft`, presented at once: no `delegation_id`, no `presentation` key, no AI line (PRD §11.4). Args stay `conversation_id` and `reason` (ids and enums only).

**A failed alert draft is told once (P3-T154).** An `ALERT_DRAFT` job that ends without a card (a run-time check fails: window closed, Ignore mode, consent missing; the model failed or refused, or its output was invalid; over budget; not needed; the proposal was refused) sends the owner one factual control-chat line from `AssistDraftJob`'s settle step, restricted to `reason == ALERT_DRAFT`: `OwnerAlerts.notify_owner(account_id, AlertKind.ALERT_DRAFT_FAILED, dedupe_key=f"alertdraft:{job_id}", contact=…, reason=…)` (F0-S adds the kind and its text, "Couldn't write a draft for {contact}: {reason}."), which goes through the gateway. `{contact}` is the contact as the alert renderers label it, escaped once; `{reason}` is a fixed phrase per outcome code ("the AI could not write one", "this month's AI budget is used up", "AI processing is off for this chat", "a reply is no longer needed", "the 24-hour reply window has closed"); never message text. The key is the procrastinate job id, so a retry of the same job sends no second line and each press gives at most one. A stale outcome re-defers and sends nothing. The M04 Re-draft and every other reason are unchanged (named test `test_a_failed_alert_draft_tells_the_owner_once`, F0-S).

AGENTS §13 and the P2 queueing-lock rule (P2 F0-interfaces §6.1) apply unchanged: a queueing-locked defer runs after commit (with a domain flag and a reconciler) or inside a SAVEPOINT; no procrastinate `lock` on a job deferred with `schedule_at`; no row lock across a model or Telegram call; handlers reload state and re-check consent, budget, flags, mode and the row's state, and are safe to run twice.

### 7.2 `JobHandlers` P3 groups (`jobs/tasks.py`, F0-R)

```python
@dataclass(frozen=True, slots=True)
class JobHandlers:
    # ... existing fields, unchanged ...
    delegation: DelegationJobHandlers | None = (
        None  # jobs/tasks_delegation.py  — replies.delegated (M6-PLAN)
    )
    release: ReleaseJobHandlers | None = (
        None  # jobs/tasks_release.py     — actions.release_scheduled (M6-SCHED)
    )
    export: ExportJobHandlers | None = None  # jobs/tasks_export.py      — privacy.export (M7-EXP)
    escalation: EscalationJobHandlers | None = (
        None  # jobs/tasks_escalation.py  — system.delegation_health (M6-ESC)
    )
    followups: FollowUpJobHandlers | None = (
        None  # jobs/tasks_followups.py   — followups.nudge (M7-NUDGE)
    )
    memory: MemoryJobHandlers | None = None  # jobs/tasks_memory.py      — memory.extract (M7-MEX)
    summaries: SummaryJobHandlers | None = (
        None  # jobs/tasks_summaries.py   — automation.summarize (M7-SSUM)
    )
    surfaces: SurfaceJobHandlers | None = None  # jobs/tasks_surfaces.py    — periodic shells


PeriodicHandler = Callable[
    [datetime], Awaitable[int]
]  # opens its own session; returns rows produced


@dataclass(frozen=True, slots=True)
class SurfaceJobHandlers:
    commitments_due: PeriodicHandler | None = None  # COMMIT-DUE: inbox/commitments_due.py
    weekly_rating: PeriodicHandler | None = None  # OPS-RATE: inbox/rating.py
```

- **Handler fields (F0-R):** `delegation.delegated`, `release.release`, `export.export`, `escalation.delegation_health`, `followups.nudge`, `memory.extract`, `summaries.summarize`, `surfaces.commitments_due`, `surfaces.weekly_rating`; `delegated` and `release` are Protocols with `run(conversation_id) -> str` and `run(action_id) -> str`, and F0-R's names win if they differ. `PeriodicHandler` and `SurfaceJobHandlers` are defined in `jobs/tasks_surfaces.py` and re-exported from `jobs/tasks.py`; `RELEASE_RETRY` and `RequeueOnShutdown` live in `jobs/retry.py`.
- `register_tasks` calls `register_<group>_tasks(app, handlers.<group>)` for each group after the three P2 calls; a `None` group registers nothing. F0-R writes every registration in §7.1 (task name, queue, retry, the call into the group's handler field); a handler field left `None` logs `task_not_wired` and returns.
- After CP1 the owning senior unit owns its `jobs/tasks_<group>.py` (it may adjust the body). `jobs/tasks_surfaces.py` stays as F0-R wrote it: its periodic shells are registered whenever the worker runs and call the handler if set, else return 0; each handler checks its own flag (`commitments.due_alerts`, `rating.weekly`) and returns 0 when it is off. Junior units write only the handler function and one wiring line.
- `jobs/tasks.py` and `jobs/reconcile.py` are not edited after F0-R.

### 7.3 Reconcilers

Each team's wire function adds its reconciler functions to `JobHandlers.reconcilers` under the "registered" condition. They run inside `reconcile_pending`'s transaction and their defers go out after commit (P2 F0-interfaces §6.3).

| Reconciler | Module | Finds | Effect | Registered | Owner |
|---|---|---|---|---|---|
| `unqueued_delegated` | `jobs/reconcile_delegation.py` | `pending_delegation_at` older than 30 s with no queued or doing `replies.delegated` | defers `replies.delegated` | `autonomy.run` on | M6-PLAN |
| `overdue_delegations` | `jobs/reconcile_delegation.py` | `status = 'active'` and any of: `ends_at <= now` (matching `delegation_active`'s `now >= ends_at`); no active `ai_managed` consent; no active, enabled, non-stale connection | ends them through `end_delegations` in the reconcile transaction with reason `time_elapsed`, `consent_revoked` or `connection_disabled` respectively (account → delegation → actions order; at most 20 per run) and requests each catch-up; no defers. The only P3 reconciler that writes inside the reconcile transaction. Sends are already blocked at once by policy and by M6-SCHED's bulk paths; this ends the delegation within about a minute | always (ending is never flag-gated) | M6-LIFE |
| `unconverted_autonomous` | `jobs/reconcile_actions.py` | an autonomous action cancelled with a conversion reason whose card or re-proposal was lost between the commit and the after-commit conversion (no `action_converted` row; text present; not an identity answer, or an identity answer whose escalation is missing (OD-10a); within 24 h) | runs each conversion in its own after-commit transaction (no lock inside the reconcile transaction) | always | M6-SCHED |
| `unqueued_releases` | `jobs/reconcile_actions.py` | `state = 'scheduled'`, `scheduled_for <= now`, no queued or doing `actions.release_scheduled` | defers `actions.release_scheduled` | always (a scheduled action is always settled; with `autonomy.send` off it is cancelled) | M6-SCHED |
| `unqueued_extractions` | `jobs/reconcile_memory.py` | `pending_memory_at` older than the debounce + 30 s, no queued or doing `memory.extract` | defers `memory.extract` | only while the job group is wired (`memory.extract` on and an LLM client present) | M7-MEX |

ARCHITECTURE §12.2's "reconcile_pending covers approved and scheduled" becomes true when M6-SCHED merges (conflict 12).

## 8. Automation registries and rule models

### 8.1 Fire registry (`automation/schedules.py`, F0-R)

```python
class SkipPolicy(StrEnum):
    SKIP_WITH_NOTE = (
        "skip_with_note"  # default: past max_lateness or expires_at ⇒ settled skipped with a note,
    )
    # then the on_skipped hook
    FIRE_LATE = "fire_late"  # never skipped for lateness or expiry: the handler runs however late


@dataclass(frozen=True, slots=True)
class FireContext:
    account_id: int
    automation_id: int
    kind: str
    occurrence_at: datetime
    timing: Timing  # ON_TIME | LATE (OBSOLETE | EXPIRED only reach FIRE_LATE kinds)
    now: datetime


@dataclass(frozen=True, slots=True)
class SkippedOccurrence:
    account_id: int
    automation_id: int
    kind: str
    occurrence_at: datetime
    note: Note


FireHandler = Callable[[AsyncSession, FireContext], Awaitable[None]]
SkipHook = Callable[[AsyncSession, SkippedOccurrence], Awaitable[None]]


class FirePostponed(Exception):
    """R95 (F0-R): raised by a fire handler that cannot settle the occurrence yet
    (M7-FUP's ingestion-lag guard). Carries no data; the handler logs its own reason (ids only)."""


class FireRegistry:
    def register_fire_handler(
        self,
        kind: str,
        handler: FireHandler,
        *,
        flag: str | None,
        skip_policy: SkipPolicy = SkipPolicy.SKIP_WITH_NOTE,
        on_skipped: SkipHook | None = None,
    ) -> None: ...
```

- Built once in `bootstrap.py` and passed to the `automation.fire` job and to `reconcile_due`; there is no module-level registry (AGENTS §5). Registering a kind twice, a kind outside `SCHEDULED_KINDS`, or `FIRE_LATE` with a flag raises at startup.
- **Fire path.** `plan_fire` steps 1–3 are unchanged (lock the occurrence; only `claimed` proceeds; the account is active and the automation still runs). Then, in the as-built order (F0-R keeps it so the digest tests stay unchanged): `SKIP_WITH_NOTE` past a deadline ⇒ settled `skipped_obsolete` or `expired`; no registration ⇒ `no_handler` (unchanged); flag set and off ⇒ `disabled`; otherwise `handler(session, ctx)` runs and the occurrence is marked `fired` in the same transaction. A handler never calls Telegram or the model and never commits; it writes rows and registers after-commit work (`notifications.enqueue(..., scheduler=)`, job defers). An exception rolls the transaction back (bounded transient retry, as today).
- **`FirePostponed` (R95; F0-R adds it, M7-FUP raises it while a deadline cannot settle yet).** When the handler raises it, the handler's savepoint rolls back, the occurrence is **not** marked fired and stays `claimed`, and `plan_fire` returns `FirePlan(FireAction.POSTPONED, account_id)` (new `FireAction` member). It logs `fire_postponed` at info with the automation id, kind and occurrence epoch only; `ScheduleJobs.fire` commits and returns quietly: no `automation_fired` line, no failed job, no `FIRE_RETRY`. The existing recovery re-defers the claimed occurrence (`lost_fires`, after the 2-minute `FIRE_GRACE`, on each `reconcile_schedules` run) for as long as it is `claimed`; past `max_lateness` step 3 settles it `skipped_obsolete` and calls `on_skipped`. A `FIRE_LATE` kind is never settled that way, so its handler must not raise `FirePostponed` indefinitely (none does). The outcome means "not yet", never "error": any other exception propagates as before.
- The digest is re-registered on the registry (`kind="digest"`, `flag="digest.daily"`); its behaviour and tests are unchanged.
- **`on_skipped`** is called in the same transaction wherever an occurrence of a registered kind is settled `skipped` with note `skipped_obsolete` or `skipped_restore` (`_advance`, `plan_fire`, `recover_claims`, `skip_for_restore`; at most `MAX_SKIPPED_ROWS` per automation per run, as today). Notes `no_handler`, `disabled`, `inactive` and `expired` do not call it. Its producers enqueue `occurrence_skipped` (dedupe `skipped:{automation_id}:{occurrence_epoch}`): every skip is reported (FR-FUP-08, OD-17).
- **`FIRE_LATE`** (only `delegation_end`): `_advance` claims its occurrences however late, `plan_fire` never settles them for lateness or expiry, and `flag` is `None`. `delegation_active` checks `ends_at` itself, so a late fire never extends autonomy.
- **Named touch points after CP1:** M7-AUTO adds `reschedule(session, *, account_id, automation_id, rrule: str | None, run_at: datetime | None, now: datetime, dtstart_local: datetime | None = None) -> bool` (recomputes `next_run_at` under the automation row lock; no double fire; `dtstart_local` is the local start a recurring schedule needs; it returns `False` and changes nothing, inside a savepoint, when there is no future occurrence or the next one is at or after `expires_at`). M7-FUP closes the `_still_runs` race inside that function only.
- **Cross-unit automation lock rule (R71).** A fire holds `automation_occurrences` → `automations` → `tasks`, in that order. Owner writes (M7-AUTO, M7-TASK, M7-REM, M7-FUP controls) lock `automations` → `tasks` and never wait on an occurrence row: they take it `FOR UPDATE NOWAIT` in a savepoint (a lock failure returns `PortResult.INVALID` "busy") or `SKIP LOCKED`; an owner delete pre-locks the automation's ledger rows `NOWAIT` before the cascading `DELETE`. The handlers' own locks are `NOWAIT` too. ARCHITECTURE §9.4 does not cover automations; M7-FUP and every automation unit follow this rule.
- **Handler binding.** `automation/reminders.py::fire_reminder` and `automation/scheduled_summaries.py::fire_summary` are bound methods of one injected instance each (`ReminderFire`, `ScheduledSummaries`), because they need the scheduler. `FireRegistry` takes `enabled_flags` in its constructor. `on_skipped` is not called for `skip_for_restore` in the post-restore CLI (it has no registry): restore skips are reported only by the restore notice. A queued job that re-checks its own occurrence (`schedules.recheck_occurrence`, e.g. `automation.summarize` waiting past its lateness) reports its own skip (FR-FUP-08).

| Kind | Handler | Flag | Skip policy | `on_skipped` | Unit (wire function) |
|---|---|---|---|---|---|
| `digest` | existing digest handler | `digest.daily` | skip_with_note | — | F0-R (re-registration) |
| `reminder` | `automation/reminders.py::fire_reminder` (no LLM; mark fired + enqueue `reminder_due` in one transaction) | `reminders.run` | skip_with_note | yes | M7-REM (`_wire_automation`) |
| `follow_up` | `automation/followups.py::fire_deadline` | `followups.run` | skip_with_note | yes | M7-FUP (`_wire_automation`) |
| `scheduled_summary` | `automation/scheduled_summaries.py::fire_summary` (defers `automation.summarize`) | `scheduled_summaries.run` | skip_with_note | yes | M7-SSUM (`_wire_automation`) |
| `delegation_end` | `automation/delegation_lifecycle.py::fire_delegation_end` | `None` | fire_late | — | M6-LIFE (`_wire_autonomy`) |

### 8.2 Rule models (`automation/rules.py`, F0-R)

Per-kind `max_lateness_minutes` defaults (OD-17): reminder 12 h, follow-up 24 h, scheduled summary 2 h; the digest keeps 2 h; `delegation_end` ignores it (`FIRE_LATE`).

```python
class ReminderRule(ScheduleRule):
    kind: Literal["reminder"] = "reminder"
    max_lateness_minutes: int = Field(default=720, ge=0, le=MAX_LATENESS_MINUTES)
    text: str | None = Field(default=None, min_length=1, max_length=400)
    # only for a recurring reminder without a task; a one-off reminder's text is tasks.title (R12)


class FollowUpRule(ScheduleRule):
    kind: Literal["follow_up"] = "follow_up"
    max_lateness_minutes: int = Field(default=1440, ge=0, le=MAX_LATENESS_MINUTES)
    conversation_id: int
    contact_id: int
    expected: MonitorCondition  # kind in {contact_replied, keywords, media_received, semantic} (validator);
    # P3 has no follow-up evaluator, so `create_follow_up` refuses `semantic` (OD-33, R94: deterministic matching only)
    on_met: Literal["close_and_notify"] = "close_and_notify"
    on_missed: Literal["remind", "remind_with_draft"] = "remind_with_draft"
    # the watched task is automations.task_id; only messages sent at or after automations.confirmed_at count;
    # a Reopen moves confirmed_at to the press, so after a Reopen only messages sent from then on count.
    # expected.description (the owner's words, 1-200 chars) is stored for every kind (FUP-6)


class ScheduledSummaryRule(ScheduleRule):
    kind: Literal["scheduled_summary"] = "scheduled_summary"
    max_lateness_minutes: int = Field(default=120, ge=0, le=MAX_LATENESS_MINUTES)
    scope: MonitorScope
    period: Literal["since_last", "last_24h", "last_7d"] = "since_last"


class DelegationEndRule(ScheduleRule):
    kind: Literal["delegation_end"] = "delegation_end"
    max_lateness_minutes: int = Field(
        default=0, ge=0, le=MAX_LATENESS_MINUTES
    )  # ignored: fire_late
    delegation_id: int
```

- `load_schedule_rule(kind, spec)` dispatches `digest` → `DigestRule`, `reminder` → `ReminderRule`, `follow_up` → `FollowUpRule`, `scheduled_summary` → `ScheduledSummaryRule`, `delegation_end` → `DelegationEndRule`, anything else → `ScheduleRule` (unchanged). A row that no longer validates is paused and audited `automation_invalid` (M05 behaviour, unchanged; how `paused_reason` reads on such a row is P3-schema §3.5, R-5).
- **Reminder shapes** (P3-schema §3.4): a one-off reminder is a task (`kind = 'reminder'`, text in `tasks.title`) plus a `reminder` automation with `task_id` set (R12); a recurring reminder is a `reminder` automation with `rrule` and `ReminderRule.text` and no task; a to-do without a time (OD-17) is a task with `kind = 'reminder'` and `due_at` NULL and no automation, so nothing fires. There is no new task kind (R20).
- Owner-authored text in a rule (`ReminderRule.text`, `FollowUpRule.expected` terms and description, `DelegationRules.topic`) is content in `automations.spec` / `delegations.spec`; its provenance (`managed`, `tainted`) is in the `provenance` column (`P3-schema.md`), not in the rule.
- `MonitorRule.action` widens to `Literal["alert", "alert_with_summary", "create_task", "digest"]` in M7-MACT (backward compatible; "old rules still load" test). The drafted-reply action is dropped for Personal v1 (OD-19).
- Persisted rules never change with prompts or tool schemas; a new shape is a new `schema_version` (ARCHITECTURE §7.5).

### 8.3 Recurrence API (`automation/recurrence.py`, RRULE; R32)

Pure: no database, no clock reads, no logging, no I/O. The DST rules stay in `automation/schedule.py`; this module builds the rule they evaluate and formats instants for the owner. These names are binding (M7-REM and M7-AUTO call them; there is no `describe_rrule`); the exact rules and tests are RRULE §6.1 and §7.

```python
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Frequency = Literal["daily", "weekdays", "weekly", "every_n_days"]
WEEKDAYS: Final[tuple[Weekday, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_INTERVAL_DAYS: Final = 2
MAX_INTERVAL_DAYS: Final = 30


@dataclass(frozen=True, slots=True)
class Recurrence:
    frequency: Frequency
    time_local: time  # naive, whole minutes
    weekdays: tuple[Weekday, ...] = ()  # weekly only
    interval_days: int | None = None  # every_n_days only


def validate_recurrence(rec: Recurrence) -> None: ...  # raises ScheduleError with a fixed message
def to_rrule(
    rec: Recurrence,
) -> str: ...  # FREQ=DAILY | FREQ=WEEKLY;BYDAY=… (Monday first) | FREQ=DAILY;INTERVAL=n
def to_schedule(
    rec: Recurrence, *, timezone: str, starts_on: date
) -> ScheduleSpec: ...  # tz_policy "follow_owner"
def recurrence_from_rule(
    rrule: str, dtstart_local: datetime
) -> Recurrence | None: ...  # the reverse; None for any other rule, never raises
def describe_recurrence(rec: Recurrence) -> str: ...  # "every Monday and Thursday at 18:30"
def echo_instant(
    at: datetime, timezone: str
) -> str: ...  # "Mon 2 Nov 2026, 14:00 (Asia/Tashkent)"; naive `at` raises ValueError
```

Constructing a `Recurrence` never raises; every function that takes one calls `validate_recurrence` first. `echo_instant` always shows the resolved instant (a reminder asked for 02:30 on a spring-forward day echoes 03:00) and adds the UTC offset only for an ambiguous autumn wall time.

## 9. Agent: run types, prompts, schemas, tools

### 9.1 Run types

| Run type | Prompt key | Module | Output schema | Context builder | Owner |
|---|---|---|---|---|---|
| `draft_reply` (Assist; unchanged) | `draft_reply` (v1) | `agent/drafting.py` | `DraftResult` | `agent/draft_context.py` | — |
| `draft_reply` (delegated) | `draft_delegated` | `agent/delegated_draft.py` | `agent/delegated_draft_schemas.py::DelegatedDraftResult` | `agent/delegated_context.py` | DELEG-DRAFT |
| `draft_reply` (nudge) | `draft_reply` (v1) with owner guidance | `agent/drafting.py` (`Drafter.draft(reason=FOLLOW_UP_NUDGE, guidance=…)`) | `DraftResult` | `agent/draft_context.py` | M7-NUDGE (no prompt change) |
| `extract_memory` | `extract_memory` | `agent/memory_extract.py` | `agent/memory_schemas.py::MemoryCandidates` | in the module; one conversation | M7-MEX |
| `owner_request` | `owner_request` (v10) | existing | — | existing | PR-OR10 |

- `agent_runs.run_type` is unchanged (OD-9): the delegated mode is a second prompt key under `draft_reply`, so Assist's `draft_reply.v1` and its ratings stay byte-identical (ADR-21). The delegated run uses the `draft_reply` route and `LLM_DRAFT_REPLY_EFFORT`. F0-S adds the optional per-key model override `LLM_ROUTE_DRAFT_DELEGATED_MODEL` now (§10.1; unset = the `draft_reply` route's model), so raising the delegated route after a recorded gate miss (AGENTS §8) is a configuration change, not an ICR (OD-22, R16).
- `triage`, `summarize` and `evaluate_monitor` do not change. `interpret_instruction` stays unused (OD-13, ADR-18).
- Every contact-triggered run follows `agent/summarize.py` exactly (P2 F0-interfaces §9): `tools=()` (RG-17); `ai_control` + `ai_managed` naming the route's processor and the budget checked before the `agent_runs` row, and again when a queued job runs; stop reason checked first; one truncation retry with a doubled budget, never prefilling; re-validation by our own model; deterministic sanitising; run row closed on every path; metered; context isolated to one conversation (invariant 4). `pending_review` items never reach any prompt.

### 9.2 Prompt allocation (`agent/prompt_registry.py`)

| Prompt key | Versions | Owner unit | Notes |
|---|---|---|---|
| `owner_request` | **v10**, the single P3 bump; **v11** held for a pre-trial fix (the TL allocates it) | PR-OR10 | v9 comes from CP4 (P2-T50). Each tool PR puts its rule text in its PR description; PR-OR10 merges them |
| `draft_delegated` (run type `draft_reply`) | v1; v2–v5 reserved for gate iterations; the passing version is frozen at the D+4 candidate | DELEG-DRAFT | Separate key (OD-9). Instructs: body only, never a disclosure; never claim to be human; flag a sincere identity question |
| `draft_reply` | stays **v1** | — | Nudges use v1's existing owner-guidance input (M7-NUDGE) |
| `extract_memory` | v1 | M7-MEX | |
| rubric `autonomy_safety.v1` | v1 | EV-AUT | `evals/rubrics/` |

**Registry shape (F0-E).** `ACTIVE` holds one isolated line per prompt key, each under its own comment line; F0-E pre-places commented placeholders for `draft_delegated` and `extract_memory`, and its placeholder test reads "a commented placeholder, or an active line whose prompt file exists", so DELEG-DRAFT and M7-MEX never edit it (R77); the version notes move to `PROMPT_HISTORY: Mapping[str, str]` (one entry per key). A prompt owner edits only its own line and its own history entry; only PR-OR10 edits the `owner_request` line. Pinned prompt labels in tests read `prompt_registry.active(...)`. A version used in a run is never edited (AGENTS §8). A change to a structured-output schema bumps the prompt version.

### 9.3 Schemas and wrappers

```python
# agent/delegated_draft_schemas.py (DELEG-DRAFT)
class DelegatedDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    language: str  # first field: the reply's tag (uz-Latn, uz-Cyrl, ru, en, or other)
    needs_reply: bool
    skip_reason: Literal["no_reply_needed", "unclear", "not_for_owner"] | None
    text: str  # the body only; never a disclosure line
    confidence: Literal["low", "medium", "high"]
    uses_only_permitted_information: bool
    sensitive_topic: bool
    makes_commitment: bool
    should_escalate: bool
    escalation_reason: (
        Literal["decision_needed", "outside_knowledge", "sensitive", "conflict", "other"] | None
    )
    in_scope: bool
    behaviour: Literal[
        "answer_from_knowledge", "ask_for_details", "acknowledge_and_defer", "decline", "none"
    ]
    identity_question: bool
    knowledge_ids_used: list[int]
    escalation_category: Literal["urgent_decision", "needs_owner", "fyi", "suspicious", "none"]
```

Validation (DELEG-DRAFT): `needs_reply = false` requires `skip_reason`; with `needs_reply`, the text is 1–4096 characters after normalisation; a text for which `actions/disclosure.py::includes_disclosure` is true (it starts with `AI_MARKER`), or that starts with any `DISCLOSURE_DEFAULTS` line, is invalid (the model wrote a disclosure); `knowledge_ids_used` is kept as given. A plain strict schema: no `format` or `pattern` (M03 D52).

```python
# agent/memory_schemas.py (M7-MEX)
class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scope: Literal["contact", "conversation"]
    kind: Literal["fact", "preference"]
    category: Literal[
        ...
    ]  # exactly MEMORY_CATEGORIES + SENSITIVE_MEMORY_CATEGORIES (memory/models.py)
    text: str
    source_message_ids: list[int]


class MemoryCandidates(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    language: str
    candidates: list[MemoryCandidate]  # at most 3 kept per run; extras dropped and counted
```

The category values are owned by `P3-schema.md` (`MEMORY_CATEGORIES`, `SENSITIVE_MEMORY_CATEGORIES`; OD-15). The model's enum offers both tuples so it can label a sensitive candidate; SENS drops such a candidate before any insert, and the category CHECK (no sensitive value) rejects it if a filter bug lets it through. M7-MEX writes the `Literal` out in full; a unit test asserts it equals the two tuples. There is no emotion, personality, trust, score or sentiment field (RG-16 schema test). Every candidate passes SENS (`memory/sensitive_filter.py`) before any row is written (RG-15); invalid `source_message_ids` drop the candidate.

**`agent/untrusted.py::Source` additions (F0-S; append only):**

```python
DELEGATION_SPEC = "delegation_spec"  # a delegation's topic or wording shown back in a tool result
AUTOMATION_SPEC = (
    "automation_spec"  # automation spec text (reminder text, follow-up expectation, summary scope)
)
REMINDER_TEXT = "reminder_text"  # reminder text in list_tasks results
MEMORY_CANDIDATE = "memory_candidate"  # inferred note text derived from contact messages
```

**`agent/tools/executor.py::NOT_RUN_STATUSES` additions (F0-S):** `scheduled` (a reminder, follow-up or automation armed, not fired) and `copy_ready` (text offered for the owner to send by hand). `create_reminder`, `create_follow_up` and `create_automation` return status **`scheduled`** when armed, never `ok` or "done" (invariant 11, R69). **`scheduled` is a NOT_RUN status only (R97):** it means "armed, has not run yet". `NOT_RUN_STATUSES` classifies a result as an expected state (logged at info, `agent_tool_call_not_run`) instead of a failure (`agent_tool_call_failed`); `agent/tools/executor.py` never reads it as success. It is never added to `agent/confirmations.py::SUCCESS_STATUSES` (`{"ok"}`, unchanged), and F0-S makes no edit to `confirmations.py`. `awaiting_confirmation` is already in `NOT_RUN_STATUSES`; after CP5 the set also holds M04's `awaiting_approval`, `blocked` and `not_drafted`. **Confirmation applier (TL-ruled R102, P3-T138):** the applier ends any confirmed call whose status is outside `SUCCESS_STATUSES` as `failed`, so F0-S adds `agent/confirmations.py::ARMED_STATUSES = frozenset({"scheduled"})` (named touch point) and one branch: a confirmed call that returns an armed status settles as armed, and the card says it is set and when it will run — never "done" and never "failed" (invariant 11). `SUCCESS_STATUSES` stays `{"ok"}`. Named test (F0-S): `test_a_confirmed_call_that_returns_scheduled_reads_as_set_not_done_or_failed`; M7-REM, M7-FUP and M7-AUTO assert the armed wording for their card-confirmed paths.

### 9.4 Tool catalogue (P3)

All: run type `owner_request` only; tier `CONTROL_ONLY`; release flag default off.

| Tool | Args model (module) | Class | Flag | Confirmation | Taint-exempt | `returns_managed` | `TOOLS` module | Owner |
|---|---|---|---|---|---|---|---|---|
| `create_delegation` | `CreateDelegationArgs` (`agent/delegation_schemas.py`) | configure | `tool.create_delegation` | `always_confirm` | no | no | `delegations` | M6-DELEG |
| `end_delegation` | `EndDelegationArgs` (same) | control | `tool.end_delegation` | none; immediate | **yes** (already in `TAINT_EXEMPT_TOOLS`) | no | `delegations` | M6-DELEG |
| `list_delegations` | `ListDelegationsArgs` (`agent/tools/delegation_reads.py`: `which` = `current` / `ended` / `all`, `conversation_id`, `limit`; items carry a state such as `not_started` or `past_end` and `covered_now`) | read | `tool.list_delegations` | — | — | **yes**: chat names wrapped `CONTACT_NAME`, topic `DELEGATION_SPEC`; taints; blocked stub without `ai_managed` | `delegation_reads` | RD-DEL |
| `create_reminder` | `CreateReminderArgs` (`agent/reminder_schemas.py`) | configure | `tool.create_reminder` | `confirm_if` recurring; a tainted run ⇒ card (gate); one-off ⇒ executed, echoed with the exact local time + Undo (OD-17) | no | no | `reminders` | M7-REM |
| `list_tasks` | `ListTasksArgs` (`agent/tools/task_reads.py`) | read | `tool.list_tasks` | — | — | **yes**: titles wrapped `TASK_TITLE` / `REMINDER_TEXT`; taints; stub; hides snoozed unless asked; true totals | `task_reads` | RD-M7 |
| `update_task` | `UpdateTaskArgs` (`agent/task_schemas.py`) | configure | `tool.update_task` | echo + Undo; a tainted run ⇒ card | no | no | `tasks` | M7-TASK |
| `create_follow_up` | `CreateFollowUpArgs` (`agent/followup_schemas.py`) | configure | `tool.create_follow_up` | `always_confirm` | no | no | `followups` | M7-FUP |
| `create_automation` | `CreateAutomationArgs` (`agent/automation_schemas.py`) | configure | `tool.create_automation` | `always_confirm` | no | no | `automations` | M7-AUTO |
| `list_automations` | `ListAutomationsArgs` (`agent/tools/automation_reads.py`) | read | `tool.list_automations` | — | — | **yes**: spec text wrapped `AUTOMATION_SPEC`, monitor terms `MONITOR_SPEC`; taints; stub; every kind except `delegation_end` (R51), incl. monitors. `ListAutomationsArgs`: `kind` (`any`, `reminder`, `follow_up`, `scheduled_summary`, `monitor`, `digest`), `include_ended`, `limit`; a paused or expired automation shows no next time | `automation_reads` | RD-M7 |
| `update_automation` | `UpdateAutomationArgs` (`agent/automation_schemas.py`) | configure | `tool.update_automation` | `always_confirm` | no | no | `automations` | M7-AUTO |
| `delete_automation` | `DeleteAutomationArgs` (same) | configure | `tool.delete_automation` | `always_confirm` | no | no | `automations` | M7-AUTO |

Changed existing tools:
- `remember` (`tool.remember`): one branch for `kind = shareable` (M6-DELEG); the args model stays the existing strict M03 `remember` schema with `shareable` added to its `kind` values (no new field). Refused unless `tool.create_delegation` is in `ToolServices.enabled_flags`. In a tainted run the text is accepted only verbatim on a card; confirming it sets `owner_confirmed_exact` (OD-8).
- `set_conversation_mode`: keeps rejecting `delegate` (M6-DELEG test).
- `create_monitor`, `list_monitors`: M7-MACT adds the `create_task` and `digest` actions behind `monitors.p3_actions`.
- `cancel_action` stays reserved in `TAINT_EXEMPT_TOOLS` and unbuilt (ARCHITECTURE §8.2 note, F0-C).

Rules for every P3 tool (AGENTS §8, ARCHITECTURE §8.3): strict args (the field lists are in each unit's spec and map onto §3.3 / §8.2); owner-local times as `"YYYY-MM-DD HH:MM"` strings parsed with `agent/tools/common.py` helpers, never `datetime` fields; every referenced id checked against the run's account before any card (a foreign id is a factual error, RG-21); idempotent on `(run_id, tool_call_id)`; provenance flags written on any row come from `agent/tools/memory.py::run_origin` unchanged, so a held call counts as tainted (R68); factual statuses only (`awaiting_confirmation`, `scheduled`, `copy_ready`, …), never "sent" or "done" for pending work; one content-free `agent_run_steps` row; a missing service returns a factual "not available" error. Tests per tool: schema-valid but semantically invalid args, a foreign-account id, registry filtering, an idempotent retry, RG-11 (a held call in a tainted run) for each configure tool, RG-08 (blocked stub) for each `returns_managed` tool. Each tool adds itself to the ARCHITECTURE §8.2 catalogue in its PR.

### 9.5 `TOOLS` tuples and `ToolServices` (F0-R)

```python
# agent/tools/registry.py
def default_registry() -> ToolRegistry:
    from assistant.agent.tools import (
        automation_reads,
        automations,
        delegation_reads,
        delegations,
        followups,
        reminders,
        task_reads,
        tasks,
    )

    return ToolRegistry(
        [
            # ... the CP5 tools, listed inline as today, unchanged ...
            *delegations.TOOLS,
            *delegation_reads.TOOLS,
            *reminders.TOOLS,
            *tasks.TOOLS,
            *task_reads.TOOLS,
            *followups.TOOLS,
            *automations.TOOLS,
            *automation_reads.TOOLS,
        ]
    )


# each agent/tools/<module>.py above
TOOLS: tuple[ToolDefinition, ...] = ()  # the owning unit appends to its own module only


@dataclass(frozen=True, slots=True)
class ToolServices:
    # ... existing fields, unchanged ...
    # enabled_flags: frozenset[str] = frozenset() already exists at CP5 (M05 ICR-2); F0-R fills it
    #   with the process's FEATURE_FLAGS in the single instance below (fixes P2-T47)
    autonomy_config: AutonomyConfig | None = (
        None  # caps, languages, standing flag for create_delegation cards
    )
    scheduler: JobScheduler | None = None  # after-commit defers and immediate notification delivery
    status_block_loaders: tuple[
        StatusBlockLoader, ...
    ] = ()  # §12.6: carries the loaders to get_status
    p3_ports: P3Ports | None = None  # the final merged ports (§4.5), e.g. the catch-up requester
    # that create_delegation and end_delegation need
```

`bootstrap.py` builds **one** `ToolServices` and passes the same instance to `OwnerRequestRuntime` and to the confirmation applier (fixes P2-T32/ICR-2 and P2-T47); `OwnerRequestRuntime` no longer builds its own. P3 tools call their domain module's functions with a session from `session_factory` and `clock`; they need no other field. A unit that needs a new field raises an ICR.

### 9.6 Sensitive filter API (`memory/sensitive_filter.py`, SENS; R32)

Pure and deterministic: no database, no clock, no logging, no model, and never an import of `assistant.agent`. A candidate is dropped when its category is sensitive, when its category is not an allowed value (uncertain means drop, OD-15), or when its normalised text contains a sensitive term; over-blocking is the safe failure. The database CHECK on `memory_items.category` is the second line (RG-15). The names are binding (M7-MEX calls them); the term tables and tests are SENS §6.2 and §7.

```python
class DropReason(StrEnum):
    SENSITIVE_CATEGORY = "sensitive_category"
    UNKNOWN_CATEGORY = "unknown_category"
    SENSITIVE_TERM = "sensitive_term"


class Candidate(
    Protocol
):  # M7-MEX's MemoryCandidate satisfies it without importing this module's internals
    @property
    def category(self) -> str | None: ...
    @property
    def text(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Lexicon:
    stems: tuple[str, ...]  # match at the start of a token; an entry with a space is a phrase
    words: tuple[str, ...]  # match whole tokens only


@dataclass(frozen=True, slots=True)
class FilterResult[C: Candidate]:
    kept: tuple[C, ...]  # input order
    dropped: int
    by_reason: Mapping[DropReason, int]  # every DropReason key, zero included


SENSITIVE_LEXICON: Final[Mapping[str, Lexicon]]  # keys = SENSITIVE_MEMORY_CATEGORIES


def sensitive_categories_in(text: str) -> frozenset[str]: ...
def screen(*, category: str | None, text: str) -> DropReason | None: ...  # None = keep
def filter_candidates[C: Candidate](candidates: Sequence[C]) -> FilterResult[C]: ...
```

`screen` checks, in order: a sensitive category, then an unknown or missing category (including `""` and wrong case), then a sensitive term in the text (whatever the allowed category says, `other_safe` included). Before matching, every Unicode `Cf` (format) character is removed from the text (not replaced by a space), so one hidden character cannot split a term (the same defence as grounding, R33). `filter_candidates` never raises for any `str`, never mutates its input and never returns a dropped candidate; nothing it returns holds the text apart from the kept candidates.

## 10. Settings and flags

### 10.1 Settings (`config.py` and `.env.example`, F0-S)

| Setting (`.env.example` line) | Field type and bounds | Read by |
|---|---|---|
| `AUTONOMY_SEND_DELAY_SECONDS=45` | `int`, 15–600 | M6-PLAN (`scheduled_for`) |
| `AUTONOMY_DEBOUNCE_SECONDS=20` | `int`, 5–300 | M6-PLAN (`replies.delegated` debounce) |
| `AUTONOMY_MAX_MESSAGE_AGE_MINUTES=30` | `int`, 1–240 | `trigger_fresh` |
| `AUTONOMY_MAX_LATENESS_SECONDS=120` | `int`, 30–900 | `late_after` |
| `AUTONOMY_LIMIT_PER_CONVERSATION_HOUR=5` | `int`, 1–30 | `volume_ok` default cap (OD-6) |
| `AUTONOMY_LIMIT_PER_DELEGATION=30` | `int`, 1–500 | `volume_ok` default cap (OD-6) |
| `AUTONOMY_LOOP_REPLIES=3` | `int`, 2–10 | loop guard (OD-6) |
| `AUTONOMY_LOOP_WINDOW_MINUTES=10` | `int`, 1–60 | loop guard |
| `AUTONOMY_LOOP_ANSWER_SECONDS=30` | `int`, 5–300 | loop guard |
| `AUTONOMY_LANGUAGES=` | `Annotated[frozenset[str], NoDecode]`, comma list ⊆ `GateLanguage` values; an unknown value is refused at startup naming the setting; empty = no language enabled (the gate) | `language_gate` (OD-14) |
| `LLM_ROUTE_DRAFT_DELEGATED_MODEL=` | `str \| None`, default unset; in `_blank_means_unset`; checked by `_every_route_has_a_known_processor` (a model no processor serves is refused at startup, naming the setting). Not in `ROUTE_RUN_TYPES`: `draft_delegated` is a prompt key under run type `draft_reply` (OD-9). Unset = the `draft_reply` route's model (OD-22, R16). The field is `Settings.llm_route_draft_delegated_model`; F0-S also provides the resolving property `Settings.draft_delegated_model` (the override, else `model_routes.model_for("draft_reply")`), which M6-PLAN, DELEG-DRAFT and EV-AUT all use (R37) | `DraftConfig.model` of `DelegatedDraftRunner` (built by M6-PLAN in `_wire_autonomy`, and by EV-AUT); consent is checked against the processor of the model actually used |
| `LLM_EXTRACT_MEMORY_EFFORT=low` | the effort `Literal \| None` of the other routes; in `_blank_means_unset` | M7-MEX |
| `MEMORY_EXTRACT_DEBOUNCE_MINUTES=30` | `int`, 1–1440 (the rolling-summary cadence); the job's maximum wait reuses the existing `SUMMARY_REFRESH_MAX_WAIT_HOURS` (no new setting) | M7-MEX |
| `EXPORT_MAX_PART_MB=45` | `int`, 1–45 (margin under Telegram's 50 MB) | M7-EXP |

`LLM_ROUTE_EXTRACT_MEMORY_MODEL` already exists. `healthcheck` reports a bad value by setting name only.

```python
# actions/config.py (F0-S); built once in bootstrap from Settings and FEATURE_FLAGS
@dataclass(frozen=True, slots=True)
class AutonomyConfig:
    send_delay: timedelta
    debounce: timedelta
    max_message_age: timedelta
    max_lateness: timedelta
    limit_per_conversation_hour: int
    limit_per_delegation: int
    loop_replies: int
    loop_window: timedelta
    loop_answer: timedelta
    languages: frozenset[GateLanguage]
    run_enabled: bool  # autonomy.run
    send_enabled: bool  # autonomy.send
    standing_enabled: bool  # delegations.standing


def build_autonomy_config(
    settings: Settings,
) -> AutonomyConfig: ...  # F0-S; F0-R calls it once in bootstrap
```

### 10.2 Flags (`core/flags.py`, F0-S; all default off)

| Constant | Flag | Gates | Unit |
|---|---|---|---|
| `AUTONOMY_RUN` | `autonomy.run` | The delegated path: `DelegationRequester` may report coverage, `replies.delegated` runs, `unqueued_delegated` is registered. Startup refuses it unless `triage.run` and `triage.assist_trigger` are on | M6-PLAN |
| `AUTONOMY_SEND` | `autonomy.send` | Off ⇒ shadow: every ALLOW_AUTO becomes a held reply (with its `autonomy_not_sent` alert, OD-11) with the decision recorded; at execution `sending_enabled` is false for `delegation_auto` (⇒ `cancelled/sending_disabled`). The single stop-the-line flag | M6-PLAN, M6-TXA |
| `DELEGATIONS_STANDING` | `delegations.standing` | `create_delegation` may create a standing delegation (exactly one chat) | M6-DELEG |
| `REMINDERS_RUN` | `reminders.run` | `reminder` fire handler (off ⇒ `disabled`) | M7-REM |
| `FOLLOWUPS_RUN` | `followups.run` | Follow-up listener and deadline handler | M7-FUP |
| `FOLLOWUPS_NUDGE` | `followups.nudge` | A missed follow-up defers `followups.nudge` (approval cards only, OD-18) | M7-NUDGE |
| `SCHEDULED_SUMMARIES_RUN` | `scheduled_summaries.run` | `scheduled_summary` fire handler | M7-SSUM |
| `COMMITMENTS_DUE_ALERTS` | `commitments.due_alerts` | `system.surface_due_commitments` handler | COMMIT-DUE |
| `MEMORY_EXTRACT` | `memory.extract` | `memory.extract` jobs, reconciler, the review cards they enqueue (≤ 5 per account per owner-local day, R19) | M7-MEX |
| `EXPORT_ENABLED` | `export.enabled` | `/export` (off ⇒ "not available") | M7-EXP |
| `RATING_WEEKLY` | `rating.weekly` | Weekly usefulness card (Sunday 19:00 owner-local, R23) | OPS-RATE |
| `MONITORS_P3_ACTIONS` | `monitors.p3_actions` | Monitor actions `create_task` and `digest` (accepted by `create_monitor`, dispatched by monitors) | M7-MACT |

Tool flags (constants `TOOL_CREATE_DELEGATION` …): `tool.create_delegation`, `tool.end_delegation`, `tool.list_delegations`, `tool.create_reminder`, `tool.list_tasks`, `tool.update_task`, `tool.create_follow_up`, `tool.create_automation`, `tool.list_automations`, `tool.update_automation`, `tool.delete_automation`. `core/flags.py` also exports `P3_FEATURE_FLAGS` and `P3_TOOL_FLAGS` (frozensets); F0-E derives the exact-set test lists from them. P2 flag names stay where they are.

Not flagged: the NFR-08 recorder (ops instrumentation, live at deploy #1); the v6 policy notice (`POLICY_VERSION`); the `delegation_end` handler and `overdue_delegations` (ending is never gated); the release job and `unqueued_releases` (a scheduled action is always settled). Renderers, callback routes, status blocks and ports register whatever the flag says, because a card already delivered must still work and ending a delegation is never gated (the catch-up renderer and requester, the memory-review renderer and route, the `rate` route and `usefulness_rating` renderer, the status blocks, the `PollObserver`, the `autonomy_not_sent` renderer and the `drf` route, OD-11); a flag gates what the feature does, not whether its handlers exist. F0-R's `test_empty_wire_functions_change_nothing` therefore checks only F0-R's own empty state (the three wire functions as F0-R wrote them), not a process in which the units have wired.

### 10.3 Enable order (README §6; production, each step: the owner edits `var/prod.env`, then the TL runs `just prod-env-push` → restart → `p3-verify release`, then `trial-check`, R28)

| Step | Flags | Guard |
|---|---|---|
| Deploy #1 | everything off; `POLICY_VERSION = v6`; recorder live; `owner_request.v10` active | owner go-ahead; never within ±1 h of the digest; not before the M04 trial's last check and digest day 5 |
| Enable 1 (owner-only M07) | `reminders.run`, `tool.create_reminder`, `tool.list_tasks`, `tool.update_task`, `tool.create_automation`, `tool.list_automations`, `tool.update_automation`, `tool.delete_automation`, `tool.list_delegations`, `commitments.due_alerts`, `rating.weekly`, `scheduled_summaries.run` (if deployed), `monitors.p3_actions` | deploy #1 healthy |
| Enable 2 (follow-ups) | `followups.run`, `tool.create_follow_up`, `followups.nudge` | enable 1 healthy |
| Enable 3 (shadow) | `triage.assist_trigger` (if not on), `tool.create_delegation`, `tool.end_delegation`, `autonomy.run`; **`autonomy.send` off** | OPS-TV3 deployed; `trial-check` shows 0 autonomous sends |
| Enable 4 (autonomy) | `autonomy.send`; `AUTONOMY_LANGUAGES` = the languages that passed S06.5 | CP-G; LV-16 done; v6 notice ≥ 24 h old; CP-A signed; shadow window clean |
| Enable 5 | `delegations.standing`; `memory.extract` (≥ 24 h after the notice); `export.enabled` | enable 4 clean |
| T0 | flag set frozen for 14 days | soak clean |

## 11. Notifications (`inbox/notification_types.py` by F0-S; delivery hooks by F0-R)

### 11.1 Kinds and refs

```python
class NotificationKind(StrEnum):
    # ... the 8 M05 members, unchanged ...
    DELEGATION_ESCALATION = "delegation_escalation"
    TAKEOVER_PAUSED = "takeover_paused"
    DELEGATION_CATCH_UP = "delegation_catch_up"
    DELEGATION_PAUSED_LLM = "delegation_paused_llm"
    CHAT_POSSIBLY_PAUSED = "chat_possibly_paused"
    AUTONOMY_VOLUME_CAP = "autonomy_volume_cap"
    AUTONOMY_NOT_SENT = "autonomy_not_sent"  # OD-11: a held reply, or no reply during an outage
    REMINDER_DUE = "reminder_due"
    FOLLOW_UP_MET = "follow_up_met"
    FOLLOW_UP_MISSED = "follow_up_missed"
    OCCURRENCE_SKIPPED = "occurrence_skipped"
    MEMORY_REVIEW = "memory_review"
    USEFULNESS_RATING = "usefulness_rating"


@dataclass(frozen=True, slots=True)
class NotificationRefs:
    # ... the M05 fields, unchanged (automation_ids already exists) ...
    delegation_ids: tuple[int, ...] = ()
    memory_ids: tuple[
        int, ...
    ] = ()  # R75: the memory item of a memory_review card (M7-MEX writes, M7-MRV reads);
    # a backward-compatible jsonb key, so no renderer parses a dedupe key for it
```

`NotificationRefs.alert_kind` (an M05 field) carries the escalation category and the failed volume code (§11.2); `automation_ids`, `task_ids`, `conversation_ids` and `contact_ids` already exist. `notifications.kind` has no CHECK, so there is no schema change. Rows stay content-free (P2-T12); renderers read the content at delivery.

### 11.2 Delivery classes

```python
class DeliveryClass(StrEnum):
    STANDARD = "standard"  # the M05 decide_delivery rules, unchanged
    SYSTEM = "system"  # never held, capped or gated (as AlertClass.SYSTEM)
    OWNER_SCHEDULED = "owner_scheduled"  # the owner chose the time: immediate; quiet hours and the cap do not apply
    # low/normal as STANDARD; high/urgent: held to the end of quiet hours (never batched), immediate otherwise, never capped
    HELD_IN_QUIET = "held_in_quiet"


DELIVERY_CLASSES: Mapping[NotificationKind, DeliveryClass]


def delivery_class(kind: NotificationKind | str) -> DeliveryClass: ...  # unknown kind ⇒ STANDARD


FEEDBACK_BUTTONS: Mapping[NotificationKind, bool]  # R82: the table's `feedback_buttons` column


def feedback_buttons(
    kind: NotificationKind | str,
) -> bool: ...  # unknown kind ⇒ True (the M05 card, unchanged)
```

The classes below are TL-ruled (R15): the OD-17 rows follow OD-17; `takeover_paused` and `chat_possibly_paused` are standard/high; `delegation_catch_up`, `follow_up_met`, `memory_review` and `usefulness_rating` are held_in_quiet/high; `delegation_paused_llm` is system; owner_scheduled kinds stay gated by `notifications.deliver` (§11.3).

**`autonomy_not_sent` is `system` (OD-11a).** The owner chose that these alerts reach them at once (OD-11a, 2026-10-29), like approval cards before; PRD §14 is clarified accordingly. The alert is never held for quiet hours, never capped, never batched and not gated by `notifications.deliver` (§11.3). It is enqueued with `alert_class = AlertClass.SYSTEM` (§11.3: the delivery gate, the account lock and the cap count read the row's `alert_class`) and priority `HIGH`. `feedback_buttons` is false, so alert feedback can never mute a contact over it. It is not in M6-ESC's `COVERED_KINDS`, so a covered chat never batches it.

**`feedback_buttons` (R82; F0-S owns the column and the lookup, F0-R's delivery consults it).** It says whether the delivered alert card carries the Useful and Not useful buttons. It is `false` for the owner-created kinds `reminder_due`, `follow_up_met`, `follow_up_missed`, `occurrence_skipped`, `memory_review`, `usefulness_rating` and `delegation_catch_up`, and for `autonomy_not_sent` (OD-11), and `true` for every other kind, including every M05 kind and `commitment_due` (an assistant-inferred alert); the existing kinds are unchanged. When it is `false`, `NotificationDelivery._card` builds only the Open chat button (when the renderer returns a `conversation_id`) plus the renderer's `extra_buttons`, so alert feedback can never mute a contact over an owner-created notice.

| Kind | Class | `feedback_buttons` | Priority (set by the producer) | Dedupe key (ids and epochs only) | Producer | Renderer |
|---|---|---|---|---|---|---|
| `delegation_escalation` | standard (+ `active_delegation`, §11.3) | true | `urgent_decision` → urgent; `needs_owner` → high; `suspicious` → normal (high when repeated, M6-ESC); `fyi` → low. `refs.alert_kind` carries the category (R60); `refs.delegation_ids` the delegation | `deleg:{delegation_id}:{conversation_id}:{base_message_id}` | M6-PLAN only (the LLM-outage fallback is now an `autonomy_not_sent` row with its own `outage:` key, below, so M6-ESC no longer produces this kind; OD-11) | M6-ESC (a Draft reply button, except on `suspicious`, §12.5; OD-11, P3-T153) |
| `takeover_paused` | standard | true | high | `takeover:{delegation_id}:{conversation_id}:{resumed_epoch}`; `resumed_epoch` = the epoch seconds of `conversations.autonomy_resumed_at`, 0 before any Resume (R81): a new takeover after a "Resume here" notifies once and offers the button again, still once per pause | M6-TAKE | M6-TAKE ("Resume here") |
| `delegation_catch_up` | held_in_quiet | false | high | `catchup:{delegation_id}`; refs are `delegation_ids` only (scoped deletion finds it through P3-schema §7.2 step 6) | `CatchUpRequester` (M6-CATCH), called by M6-DELEG and M6-LIFE | M6-CATCH |
| `delegation_paused_llm` | system | true | — | `llmpause:{delegation_id}:{since_epoch}` | M6-ESC | M6-ESC |
| `chat_possibly_paused` | standard | true | high | `chatpaused:{conversation_id}:{since_epoch}`; `since_epoch` = the earliest failed autonomous send after the conversation's newest contact message | M6-ESC | M6-ESC |
| `autonomy_volume_cap` | standard | true | high | `cap:{delegation_id}:{conversation_id}:{hour_epoch}`; `refs.alert_kind` = the failed volume code (R60) | M6-PLAN (when a `volume_ok` code first appears) | M6-ESC |
| `autonomy_not_sent` (OD-11) | system (OD-11a; enqueued with `AlertClass.SYSTEM`) | false | high | **Held reply:** `notsent:{action_id}` (the held reply's id; one alert per held reply); refs `conversation_ids`, `contact_ids`, `delegation_ids`, `message_ids = (base_message_id,)`, `action_ids = (action_id,)`, `alert_kind` = the first of `draft_flags["autonomy_codes"]`, else `late` (a late conversion), else `shadow`, else `not_sent`. **Outage (no text):** `outage:{delegation_id}:{conversation_id}:{m}` (`m` = the chat's newest contact message; its own key, because during an outage the planner's failed draft already took `deleg:…:{m}` with a `needs_owner` escalation that a covered chat batches for the catch-up, and the alert must still reach the owner), no `action_ids`, `alert_kind` = the outage cause (`llm_unavailable` / `budget`). Both set the `conversation_id` and `contact_id` columns, so scoped deletion removes them | M6-PLAN (the job's held replies); M6-SCHED (`convert_cancelled`'s `CARD` branch, also run by M6-TXA and `unconverted_autonomous`); M6-ESC (the outage fallback, §3.5 of its spec) | M6-ESC (Draft reply and Pause buttons, §12.5) |
| `reminder_due` | owner_scheduled (OD-17) | false | normal (ignored) | `auto:{automation_id}:{occurrence_epoch}` | M7-REM | M7-REM |
| `follow_up_met` | held_in_quiet | false | high | `fupmet:{automation_id}:{matched_message_id}` (a closure after Reopen notifies again, R22) | M7-FUP | M7-FUP (Reopen) |
| `follow_up_missed` | held_in_quiet (OD-17) | false | high | `auto:{automation_id}:{occurrence_epoch}`; `refs.alert_kind` ∈ `nudge`, `unchecked`, `chat_ignored`, `nudge_not_drafted` (FUP-4) | M7-FUP | M7-FUP |
| `follow_up_missed` (second notice) | held_in_quiet | false | high | `nudgeout:{automation_id}:{occurrence_epoch}`, `refs.alert_kind = "nudge_not_drafted"`: sent by M7-NUDGE when no draft nudge came and the first notice already went out (NUDGE-2) | M7-NUDGE | M7-FUP |
| `occurrence_skipped` | held_in_quiet (OD-17) | false | high | `skipped:{automation_id}:{occurrence_epoch}`; refs: `automation_ids = (id,)`, `task_ids` when a task exists, the chat and contact refs; the renderer reads the occurrence time from the key and handles reminder, follow-up and scheduled-summary automations | `on_skipped` of M7-REM, M7-FUP, M7-SSUM | M7-FUP |
| `memory_review` | held_in_quiet | false | high | `memreview:{local_date}:{memory_id}` (`local_date` = the owner-local date as `YYYYMMDD`). The notification row has NULL `conversation_id` and `contact_id` columns and no `message_ids` (so delivery adds no Open chat or feedback buttons and feedback cannot mute a contact), and `refs` carry `memory_ids = (memory_id,)` (R75), `conversation_ids` and `contact_ids` | M7-MEX: each `memory.extract` run enqueues one card per new suggestion while the account has fewer than 5 `memory_review` rows for that owner-local date (counted by key prefix, in a transaction that first locks the account row `FOR NO KEY UPDATE`, §9.4 order); a suggestion over the cap gets no card and stays `pending_review` (the 14-day sweep deletes it). No periodic job (R19, OD-16) | M7-MRV |
| `usefulness_rating` | held_in_quiet | false | high | `rating:{account_id}:{iso_year}{iso_week:02d}`; empty refs, the press and the renderer read the ISO week from the key | OPS-RATE | OPS-RATE |
| `commitment_due` (M05 kind) | held_in_quiet (OD-17) | true (an assistant-inferred alert) | normal (digest) unless the contact is VIP or the task is followed up (high) (OD-18) | `commitment_due:{task_id}:{due_epoch}` | COMMIT-DUE | M05 `render_task` |
| every other M05 kind | standard | true | unchanged | unchanged | unchanged | unchanged |

Dedupe keys use epoch seconds, never `{occurrence_at}` as text (conflict 14).

### 11.3 Delivery rules

- `notifications.deliver` (M05) still gates every non-system kind, unchanged.
- **`decide_delivery` (F0-R):** class `SYSTEM` (or `AlertClass.SYSTEM`) ⇒ immediate; `OWNER_SCHEDULED` ⇒ immediate (reason `owner_scheduled`); `HELD_IN_QUIET` with high or urgent priority ⇒ inside quiet hours `DEFER` to their end (reason `quiet_hours`), outside them immediate, never capped; anything else follows the M05 rules. Every M05 kind is `STANDARD` except `commitment_due`, which has no producer before COMMIT-DUE, so F0-R changes no behaviour.
- **`NotificationFacts` gains `active_delegation: bool = False`** (F0-R slot; delivery loads `False` until M6-ESC). M6-ESC (FR-TRI-03, OD-7): in a chat covered by an active delegation, `triage_*` and `delegation_escalation` alerts other than `urgent_decision` are batched into the end-of-session catch-up (reason `delegation_catch_up`); `triage_urgent` and `triage_decision` stay immediate in covered chats (a decision request is alerted at once, PRD R2 #1; M6-ESC adds an `already_alerted` guard); `urgent_decision` is immediate, and bypasses quiet hours only when the delegation's `urgent_bypass_quiet_hours` is on (enqueued with `bypass_quiet_hours=True`). `active_delegation` is loaded only while `autonomy.run` is on (`NotificationDelivery.use_delegation_facts`). A `SYSTEM`-class kind must be enqueued with `alert_class = SYSTEM`, because the delivery gate, the account lock and the cap count read the row's `alert_class`, not the class table.
- **Renderers** are registered with `NotificationDelivery.register_renderer(kind, renderer)` in the owning team's wire function. A kind without a renderer is suppressed at delivery (fail safe). A renderer reads content at delivery, returns `None` when its source is gone (suppressed: "deleted source → suppressed"), adds buttons through `RenderedAlert.extra_buttons`, and follows the escape-once rule of P2 #133, as GW-ESC records it (§4.6). Truthful wording: "scheduled" or "snoozed" is never "sent" or "done" (RG-20).

## 12. Control chat

### 12.1 Commands and `/help`

| Command | Flag | Handler | Unit |
|---|---|---|---|
| `/export` | `export.enabled` (off ⇒ "not available") | `telegram/control/export.py` | M7-EXP |

`/export` is a command only, never a tool (OD-20). It refuses when the account's `control_chat_id` differs from the owner's `telegram_user_id`, and stops with `busy` or `rate_limited` rather than queueing (R50). There is no `setMyCommands` call (unchanged). F0-R builds `HELP_TEXT` from a fixed base plus `ControlRoutes.help_lines` in registration order; each command's unit registers its one line in its wire function.

### 12.2 `ControlRoutes` P3 fields (`telegram/control/routes.py`, F0-R)

```python
PanelRowBuilder = Callable[
    [AsyncSession, Clock, Account, ConversationPanel], Awaitable[Sequence[Sequence[Button]]]
]
SettingsRowBuilder = Callable[[AsyncSession, Clock, Account], Awaitable[Sequence[Sequence[Button]]]]


@dataclass(frozen=True, slots=True)
class ControlRoutes:
    callbacks: Mapping[str, CallbackHandler] = field(default_factory=dict)
    pending_inputs: Mapping[str, PendingInputHandler] = field(default_factory=dict)
    commands: Mapping[str, CommandHandler] = field(default_factory=dict)
    # P3 (F0-R)
    help_lines: Mapping[str, str] = field(default_factory=dict)  # command → one /help line
    panel_rows: tuple[PanelRowBuilder, ...] = ()  # conversation panel extra rows
    settings_rows: tuple[SettingsRowBuilder, ...] = ()  # /settings extra rows
    undo_handlers: Mapping[str, UndoHandler] = field(default_factory=dict)  # §12.7
    status_blocks: tuple[StatusBlockLoader, ...] = ()  # §12.6: carries the loaders to /status
```

A row builder returns complete `core.channel.Button` rows whose callback data uses the builder's own registered kind; it may create its card's `pending_inputs` row in the session. `panels.py` appends the rows after its own, in registration order, and never inspects them; an empty result shows nothing (a hidden row). The P2 precedence rules of the dispatcher are unchanged (a command cancels a pending text question; a stop word pauses; forwarded or third-party text is never an answer, M03 D49). Every handler answers the press, does all database work in the transaction and sends after commit through the gateway (AGENTS §5, §7).

### 12.3 Callback kinds (`<kind>:<token>:<op>`, ≤ 64 bytes)

The M03–M05 kinds (`act`, `ntf`, `mon`, `dg`, `undo`, `tool_confirm`, `conv_panel`, `settings`, …) are unchanged.

| Kind | Card (`pending_inputs.ref_id`) | Ops | Unit |
|---|---|---|---|
| `tko` | takeover notice (conversation id) | `rh` Resume here | M6-TAKE |
| `pz` | Pause button on cards (none); data `pz:pause:p` = `core/autonomy_texts.py::PAUSE_CALLBACK_DATA` (fixed token `pause`, never checked; R45) | `p` pause; the fast lane stores the pause in the poll's own transaction (owner allowlist only, ≤ 5 s, no Bot API call from ingress, AGENTS §5); the dispatcher then answers the press through the gateway | M6-PAUSE |
| `p3d` | delegation row in `/settings` and the panel Delegate row (delegation id; the `ls` op has no subject, so its card has `ref_id` NULL) | `ls` list, `vw` view, `end` ask, `eok` confirm end, `ecx` cancel | SET-P3 |
| `p3w` | disclosure wording (delegation id) | `vw` view; `ed_ul`, `ed_uc`, `ed_ru`, `ed_en` edit the wording for `uz-Latn`, `uz-Cyrl`, `ru`, `en` (open `deleg_wording_<lang>`; the language travels in the op because `pending_inputs` has only a kind and a `ref_id`) | SET-P3 |
| `p3a` | automation row in `/settings` (automation id; `ls` has no subject, `ref_id` NULL) | `ls` list, `pa` pause, `rs` resume, `dl` ask delete, `dok` confirm delete, `dcx` cancel (pause and resume are immediate; only delete has a confirm step) | SET-P3 |
| `drf` | Draft reply on an `autonomy_not_sent` or `delegation_escalation` alert (notification id; created by the renderer in the delivery claim transaction, as M05's monitor Stop card; TTL until the chat's reply window closes, at least 1 min, at most 24 h). Looked up, never claimed, so a second press is answered from the current state (OD-11) | `dr` Draft reply: **stored text** (the alert's `refs.action_ids` names a held reply of this account and chat) ⇒ write the `approval_card_requested` audit row and present that action's existing card after commit (`ApprovalCardPresenter.present_after_commit`; no model call); **no stored text** ⇒ the press checks a suspicious escalation (P3-T153), the reply window, consent, a live card and the outage state, then `request_draft(reason=ALERT_DRAFT)` (§7.1); every refusal is a factual answer (§12.5) | M6-ESC (`telegram/control/draft_reply.py`) |
| `exp` | export confirm card (none) | `ok` export, `cx` cancel | M7-EXP |
| `rem` | `reminder_due` card (notification id) | one-off reminder: `dn` done, `s1` snooze 1 h, `st` tomorrow (the press's owner-local date + 1 at the reminder's time, R72) (through `TaskControls`); recurring reminder: `pa` pause reminder (through `AutomationControls.pause`) (R21) | M7-REM |
| `fup` | `follow_up_met` card (notification id) | `ro` reopen | M7-FUP |
| `mem` | memory review card (memory item id) | `ok` approve, `ed` edit (opens `mem_edit`), `rm` discard | M7-MRV |
| `rate` | usefulness card (notification id) | `1` … `5` | OPS-RATE |

The delegation, follow-up and automation confirmations use the existing `tool_confirm` card (`always_confirm`).

### 12.4 Pending-input kinds

| Kind | `ref_id` | The owner's next message is | Unit |
|---|---|---|---|
| `mem_edit` | memory item id | the replacement note text (owner words; the item stays scoped to its contact or chat, keeps `source = agent_inferred`, its `category` and its tainted and managed provenance, so managed-data and per-chat deletion still remove it; no sensitive-filter pass runs on the owner's words) | M7-MRV |
| `deleg_wording_ul`, `deleg_wording_uc`, `deleg_wording_ru`, `deleg_wording_en` | delegation id | the new disclosure wording for `uz-Latn`, `uz-Cyrl`, `ru`, `en` respectively, saved through `DelegationControls.set_disclosure_wording` (validated by `validate_wording`; cannot be emptied) | SET-P3 |

### 12.5 Cards

| Card | Sent by | Buttons | Notes |
|---|---|---|---|
| Takeover notice | `takeover_paused` renderer | Resume here (`tko:rh`), Pause (`pz:pause:p`) | Once per takeover: per delegation, chat and resume epoch (R81), so a takeover after a Resume notifies again; "Resume here" is card-only, never a tool (`actions/autonomy.py::resume_here`, M6-TAKE) |
| Pause button | `core/autonomy_texts.py::pause_button() -> Button` (pure, R45); every caller uses it directly. `telegram/control/pause_button.py` holds only the `pz` press handler and has no `pause_button` wrapper (R99) | `pz:pause:p` | M6-PAUSE handles the press; appended through `RenderedAlert.extra_buttons` by the escalation, catch-up, takeover, volume-cap and `autonomy_not_sent` (OD-11) renderers, and carried by the delegation-on notice, so every card of this table that is about automatic replies has it (checked before enable 3, R48). M6-PAUSE may append it in `telegram/control/takeover.py` and remove the strict-xfail marker in `tests/integration/test_takeover_notice.py` (named touch point, R45) |
| Delegation-on notice | sent by M6-DELEG after the confirm | Pause (`pz:pause:p`) | States the effective limits (R53); the card itself says "your default limits" |
| Automatic reply not sent (OD-11) | `autonomy_not_sent` renderer (M6-ESC) | 📝 Draft reply (`drf:<token>:dr`), Pause (`pz:pause:p`) | Compact: the contact's name, why no automatic reply went out (`AUTONOMY_CODES[alert_kind].explanation`, the `late` or shadow sentence, or the outage cause), and for a held reply "Sendable until HH:MM" (owner-local). Never message text, never the reply's text. Suppressed at delivery (`None`) when the chat or contact is gone, or when the held reply is no longer `awaiting_approval`, `revising` or `blocked` (the owner already acted, or a takeover outdated it). A held reply in `blocked` says it cannot be sent from here and the button shows its copy-ready card |
| Draft reply press (OD-11) | `drf` handler (M6-ESC) | — | Answers through the gateway after commit; never reports a draft that does not exist (invariant 11). Held reply: any state ⇒ its card is presented (the card shows the truthful state: Approve / Edit / Reject while live; copy-ready with Re-draft where M04 offers it otherwise), answer "Here is the draft." or "This reply can no longer be sent (<short reason>); its card has the text to copy."; card already shown ⇒ "The draft is in its card." and the card is synced. No stored text: `delegation_auto` for that burst still pending or sent ⇒ "An automatic reply is scheduled or was sent, so no draft was written."; Ignore mode or bot chat ⇒ "No drafts are made for this chat."; reply window closed ⇒ "The 24-hour reply window has closed, so no reply can be sent."; no active `ai_managed` and `ai_control` consent naming the drafting model's processor ⇒ "AI processing is off for this chat, so no draft can be written."; `llm_outage(...)` (M6-ESC) ⇒ "The AI service is not answering" / "This month's AI budget is used up", "so no draft can be written now. Try again later."; a live card already covers the newest contact message ⇒ "A draft for this chat already waits for your approval." (that card synced), except that when that live card is an unrequested held reply (`presentation = on_request`, no `approval_card_requested` row) the press writes `approval_card_requested` and presents it as in the held-reply path ("Here is the draft."), never just a sync; a draft job already queued (savepoint conflict) ⇒ "A draft is already being written."; otherwise ⇒ "Writing a draft; if it can't be written, I'll tell you here." The job re-checks consent and budget when it runs; if it ends without a card it sends one factual line, "Couldn't write a draft for {contact}: {reason}." (§7.1, P3-T154). A suspicious escalation offers no button; a press on an old token there answers "No draft is written for a suspicious message; open the chat to answer yourself." and defers nothing (P3-T153) |
| Escalation alert (OD-11) | `delegation_escalation` renderer (M6-ESC) | 📝 Draft reply (`drf:<token>:dr`) when no automatic reply was sent or is pending for the burst (no action, a held reply, or a `delegation_auto` that ended `cancelled`, `expired` or `failed`) and `refs.alert_kind` is not `suspicious` (P3-T153: the owner reads the chat and answers; no model drafts in response to a suspected instruction attempt), Pause (`pz:pause:p`) | Delivery adds Open chat and the feedback buttons as for any standard alert; delivery rules unchanged (§11.3) |
| Export confirm | `/export` | Export, Cancel | Warns that the file stays in Telegram's chat history; ≤ 1 export per day (OD-20) |
| Usefulness rating | `usefulness_rating` renderer | 1–5 | |
| Memory review | `memory_review` renderer | Approve, Edit, Discard | Card-only; no tool can approve a suggestion. No Open chat or feedback buttons: the renderer returns `conversation_id = None`, and the notification's `contact_id` column stays NULL (§11.2) |
| Reminder | `reminder_due` renderer | One-off: Done, Snooze 1 h, Tomorrow. Recurring: Pause reminder | One-off buttons go through `TaskControls`. A recurring reminder has no task, so the card hides Done, Snooze and Tomorrow and shows "Pause reminder" through `AutomationControls` (R21). A button whose port is unwired is hidden |
| Follow-up met | `follow_up_met` renderer | Reopen | Through `TaskControls` |
| Delegation confirmation | existing `tool_confirm` | Confirm, Cancel | Text by M6-DELEG: chats, topic, allowed and forbidden behaviours, knowledge recipients, limits, escalation rule, disclosure wording, owner-local start and end. Describers receive no services and the card text must fit `agent/confirmations.py`'s card limits: only a foreign id is refused before the card (`NotInAccountError`); for an Ignore or bot chat the card says the delegation cannot start and Confirm changes nothing (the M05 monitor pattern); the card says "your default limits" and the post-confirm notice (above) states the numbers |

### 12.6 Status blocks (`observability/status.py`, `agent/tools/status.py`, `telegram/control/handlers.py`; F0-R)

```python
@dataclass(frozen=True, slots=True)
class StatusBlock:
    key: str
    lines: tuple[str, ...]  # /status lines: no names, no message text
    data: Mapping[str, Any]  # JSON-ready; get_status returns it under blocks[key]


StatusBlockLoader = Callable[[AsyncSession, int, datetime], Awaitable[StatusBlock | None]]
```

`build_status(..., block_loaders: Sequence[StatusBlockLoader] = ())`; `StatusReport.blocks: tuple[StatusBlock, ...] = ()`; `render_status` prints the blocks after the M05 lines; `get_status` returns `blocks`, and adds the key (and `render_status` prints block lines) only when at least one block exists, so the CP5 output stays identical. The loaders reach `/status` through `ControlRoutes.status_blocks` and `get_status` through `ToolServices.status_block_loaders` (§9.5, §12.2). A loader returning `None` shows nothing. P3 keys (STAT-ACT, `observability/status_p3.py`, registered in `_wire_surfaces`): `delegations` (active count; per delegation: id, kind, owner-local end, chat count), `takeover_pauses` (chats paused by a counting takeover), `autonomy_languages` (enabled gate languages, "none" when empty), `automations` (counts by kind and status; a P3 kind whose flag is off is marked). STAT-ACT refines `delegations` with the automatic-reply mode (`off`, `paused`, `shadow`, `on`), a `starting_later` count and a `scheduled_replies` count; `takeover_pauses` calls M6-TAKE's `actions/autonomy.py::paused_by_takeover` and never copies the takeover rule, so STAT-ACT's status PR depends on M6-TAKE for that block.

### 12.7 Undo registry (`memory/undo.py`, F0-R)

```python
UndoHandler = Callable[
    [AsyncSession, int, AuditLogEntry, Clock], Awaitable[bool]
]  # True = reverted
BUILTIN_UNDO: Mapping[
    str, UndoHandler
]  # four of the five M03/M05 events, moved out of the if/elif chain;
# `preferences_changed` stays a dedicated branch because UndoHandler returns
# bool and cannot carry its PARTIAL result (restored/kept)


async def undo(
    session: AsyncSession,
    *,
    account_id: int,
    change_id: int,
    clock: Clock,
    extra: Mapping[str, UndoHandler] = MappingProxyType({}),
) -> UndoOutcome: ...
```

The dispatcher passes `ControlRoutes.undo_handlers` as `extra`. `UndoStatus` outcomes are unchanged; a handler returns `False` when the change was superseded. P3 events:

| Event | Handler | Unit |
|---|---|---|
| `reminder_created` | `automation/reminders.py::undo_reminder_created` (cancels a not-yet-fired one-off reminder and its task; for a to-do without a time, whose audit row has `subject_type = 'task'`, it cancels the to-do; rows are kept closed per retention, R73) | M7-REM |
| `task_closed` (reason `owner_done` only) | `inbox/tasks.py::undo_task_closed` | M7-TASK |
| `task_snoozed` | `inbox/tasks.py::undo_task_snoozed` | M7-TASK |

Delegations, automations and exports have no Undo (they go through cards).

## 13. Audit events (content-free; `audit_log.event`)

`details` hold ids, enums, codes, counts and epoch seconds only; never message text, names, topics or wording (the redaction test). `subject_id` is the row id. `actor` is `owner` for the owner's presses, commands and confirmed tool calls, `agent` for model-initiated tool calls, `system` otherwise.

**Action transitions (F0-S event table, §2.2):** `action_auto_approved` (`proposed → approved`), `action_scheduled` (`approved → scheduled`; + `scheduled_for`, `late_after`), `action_released` (`scheduled → approved`), `action_cancelled` (`scheduled → cancelled` and every autonomous cancel; `reason` = the §2.2 / §3.6 reason). All four carry `delegation_id`.

| Unit | Event | `subject_type` | `details` |
|---|---|---|---|
| F0-S (`end_delegations`) | `delegation_ended` | `delegation` | `reason` (an `ended_reason` value), `to` (`ended` / `cancelled`) |
| M6-DELEG | `delegation_created` | `delegation` | `kind`, `conversations` (count), `share_widened`, `starts_at`, `ends_at` (epoch or null) |
| M6-DELEG | `delegation_wording_changed` | `delegation` | `locale` |
| M6-DELEG | `memory_saved` (existing) | `memory_item` | gains `kind: "shareable"`, `owner_confirmed_exact` |
| M6-PLAN | `autonomy_decision` | `conversation` | `delegation_id`, `outcome` (a `PlanOutcome`), `codes` (failed codes), `action_id` (or null), `shadow`, `identity_answer`, `escalation_category` |
| M6-TXA | `action_claimed` (existing) | `action` | gains `delegation_id`, `disclosure_included` |
| M6-TXA, M6-SCHED | `action_converted` | `action` (the cancelled one) | `to_action_id` (the new action, or the newer in-flight autonomous action that already holds the chat's slot, §3.6), `conversion` (`card` / `repropose`), `reason` |
| M6-TAKE | `takeover_detected` (existing) | `conversation` | gains `delegation_id` (or null), `counts` (bool: blocks autonomy now) |
| M6-TAKE | `conversation_autonomy_resumed` | `conversation` | `delegation_id`, `source: "card"` |
| M6-PAUSE | `autonomy_paused` (existing) | `account` | `source` gains `button` and `bot_blocked` (NFR-07 per source; the fast lane records the real trigger `command`, `stop_word`, `button` or `bot_blocked`, and `fast_lane` stays a legacy value in M04 rows); `cancelled` (count); `lane` (`fast`, `inbox`, or `send` for the R46 auto-pause after a forbidden control send); `trigger_at` (epoch of the Telegram date; null for a button press, whose latency is timed by stopwatch); `update_id`; `apply_ms` (R49). OPS-AV's `nfr07.sql` reads them, and the measured latency includes the fast-lane lock wait; an inbox-lane pause carries no `trigger_at` and is counted but not timed |
| M6-ESC | `delegation_paused_llm` | `delegation` | `cause` (`llm_unavailable` / `budget`) |
| M6-ESC (`drf` press) | `approval_card_requested` | `action` (the held reply) | `notification_id`, `version`; actor `owner`. Written in the press transaction before the card is presented; M6-PLAN's sync rule reads it (§2.3) (OD-11). The no-text path writes no new event: `replies.draft` already audits its outcome with `reason = "alert_draft"` |
| M6-CATCH | `delegation_catch_up_rendered` | `delegation` | `messages`, `sent`, `failed`, `uncertain`, `pending_cards`, `decisions` (counts). Written by the renderer in the claim transaction, one row per render (a retry after a definite send failure writes another); the renderer takes a `Clock` (`CatchUpRenderer(clock)`) |
| M7-REM | `reminder_created` | `automation` (`task` for a to-do without a time, whose subject id is the task id, since no automation exists) | `task_id` (or null), `recurring`, `card` (bool) |
| M7-REM | `reminder_fired` | `automation` | `occurrence_at`, `late` (bool) |
| M7-REM | `reminder_cancelled` | `automation` | `reason` (`undo` / `task_gone`) |
| M7-TASK; M7-FUP | `task_closed` (existing) | `task` | `reason` gains `owner_done`, `owner_cancelled` (M7-TASK) and `follow_up_met` (M7-FUP closes the watched task itself, with `closed_reason = 'follow_up_met'`, plus `automation_id`; FUP-2); M7-TASK adds `run_id`, `call_id` and, on a cancel, `automations` (ids of the one-off reminders it cancelled) |
| M7-TASK | `task_snoozed` | `task` | `until` (epoch); for Undo: `previous_until`, `automation_id`, `previous_run_at`, `previous_status`; `run_id`, `call_id` |
| M7-TASK | `task_reopened` | `task` | `source` (`card` / `tool`); `run_id`, `call_id` |
| M7-FUP | `follow_up_created` | `automation` | `task_id`, `expected_kind` |
| M7-FUP | `follow_up_met` | `automation` | `task_id`, `message_id`, `after_deadline` (bool: the "arrived after all" correction) |
| M7-FUP | `follow_up_missed` | `automation` | `task_id`, `occurrence_at` |
| M7-FUP | `follow_up_cancelled` | `automation` | `reason` (`task_gone` / `owner`) |
| M7-NUDGE | `nudge_proposed` | `automation` | `action_id`, `state` (`awaiting_approval` / `blocked`) |
| M7-NUDGE | `nudge_not_drafted` | `automation` | `status` (one of the eight `DraftStatus` values, or `stale`, or `disabled`; NUDGE-1) |
| M7-AUTO | `automation_created`, `automation_updated`, `automation_paused`, `automation_resumed`, `automation_deleted` | `automation` | `kind`; `fields` (names only) on `automation_updated` |
| M7-SSUM | `scheduled_summary_claimed` | `automation` | `occurrence_at`; the at-most-once claim, written and committed before the send (R70) |
| M7-SSUM | `scheduled_summary_sent` | `automation` | `occurrence_at`, `claim_id`, `chats` (count), `content` (`summary` / `could_not_summarize`), `outcome` (`sent` / `uncertain` / `not_delivered`) (R70: delivery and content are separate) |
| M7-MACT | `task_created` (existing) | `task` | gains `source: "monitor"` and `automation_id` for a monitor's task; M7-TASK's owner tasks write `source: "owner"` |
| M7-MEX | `memory_suggestions_created` | `conversation` | `created`, `dropped_sensitive`, `dropped_invalid`, `dropped_duplicate`, `dropped_extra`, `cards` (counts), `run_id` (R76) |
| M7-MRV | `memory_suggestion_approved`, `memory_suggestion_edited`, `memory_suggestion_discarded` | `memory_item` | — |
| M7-EXP | `data_export_started` | `account` | `card_id`; written and committed before the first part is sent: the at-most-once key for a requeued run (a started export with no final row is reported `interrupted` and never resent) |
| M7-EXP | `data_exported` | `account` | `card_id`, `parts`, `bytes`, `classes` (count), `failed_parts` (failed and not attempted), `uncertain_parts` |
| M7-EXP | `data_export_failed` | `account` | `card_id`, `cause` (`interrupted`, `disabled`, `daily_limit`, `no_control_chat`, `busy`, `rate_limited`, `forbidden`, `chat_unreachable`, `build_failed`), and on a stop `sent_parts`, `uncertain_parts` |
| OPS-RATE | `usefulness_rated` | `account` | `score` (1–5), `iso_week` (a JSON number, `YYYYWW`) |

No new audit event: COMMIT-DUE (the `notifications` row is the record), OPS-AV (system table), STAT-ACT, RD-DEL, RD-M7 (reads; `agent_run_steps` rows), SET-P3 (writes go through ports whose owners audit), GW-DOC (gateway logs only). Each unit writes its events through module-level constants (`EVENT_* = "…"`) in its own module.

## 14. Evals and ops seams

### 14.1 Eval-target registry (`evals/registry.py`, F0-E)

```python
@dataclass(frozen=True, slots=True)
class EvalTargetEntry:
    name: str  # python -m evals run <name>
    datasets: tuple[str, ...]  # DatasetName values the target reads
    run_type: str  # the route and effort it uses (the effort map)
    module: str  # e.g. "evals.autonomy"
    factory: str  # the target's async CLI runner in that module (imported lazily), called with the argparse args
    review: str | None = (
        None  # optional "module:attribute" path of the target's review sheet (F0-E; evals/cli.py is frozen after F0-E)
    )
    comparison: str | None = None  # optional "module:attribute" path of a model-choice comparison


TARGETS: tuple[EvalTargetEntry, ...] = (  # one line per target; a target owner adds only its line
    ...,  # retrieval, summary, drafts, triage, monitors (existing)
    # EvalTargetEntry("autonomy", ("e4", "e2"), "draft_reply", "evals.autonomy", "build"),   # EV-AUT
)
```

- The autonomy line sets neither `review` nor `comparison`: a comparison would run `model_choice` on threshold-less informational metrics and report that every tier meets every threshold. EV-GATE therefore exposes `python -m evals.gate {report,sheet,ingest,confirm,compare}` instead. Only EV-AUT adds a registry line (`autonomy`); EV-E4L and EV-GATE add none.
- `evals/targets.py`: `DatasetName = Literal["e1", "e2", "e3", "e4", "retrieval"]` (CP5 adds `e3`; P3 adds `e4`).
- `evals/cli.py` choices and the effort map, `evals/live.py` and `evals/agent_mode.py::PRODUCTION_FLAGS` are derived from `TARGETS` and `core/flags.py` plus one explicit background set (P2-T41); so are the exact-set lists in `tests/integration/agent_env.py`, `tests/unit/test_tool_registry.py` (managed set) and `tests/unit/test_e1_dataset.py` (manifest set).
- EV-AUT: target `autonomy`, rubric `autonomy_safety.v1`, calls `AutonomyPlanner.plan` side-effect-free (§4.1), records reason codes. EV-GATE: `evals/gate.py` (per-language thresholds, NOT EVALUABLE on any missing rating or verdict, `AUTONOMY_COMPARISON` in the `model_choice` pattern, a gate report that recommends `AUTONOMY_LANGUAGES`). EV-E4L: `evals/e4.py` (E4 v1.1 loader and integrity).
- Real-API runs stay manual, with the owner's per-run go-ahead and cost noted (AGENTS §8, §9; OD-22, OD-23).

### 14.2 `p3-verify` (`tools/acceptance/p3_verify.py`, F0-E)

```python
@dataclass(frozen=True, slots=True)
class Subcommand:
    name: str
    help: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[Any, argparse.Namespace], int]  # (read-only connection, args) -> exit code


SUBCOMMAND_MODULES = (
    "p3_checks",
    "p3_views",
    "p3_report",
    "p3_restore_check",
)  # each exports SUBCOMMANDS
```

Standard library + psycopg like `p2_verify.py`; one read-only `REPEATABLE READ` snapshot opened by the CLI; content-free output (ids, counts, codes); exit 0 = pass, 1 = FAIL, 2 = error or NOT EVALUABLE; a duplicate subcommand name raises. Lives in `tools/` and is never imported by `src/`.

| Subcommand | Module | Unit |
|---|---|---|
| `release` (P3 tables and columns present; P2 compatibility) | `p3_checks.py` | OPS-TV3 (SQ) |
| `trial-check` (fail-closed `delegation_auto` audit, specs/OPS-TV3.md; the P2 checks for the other origins) | `p3_checks.py` | OPS-TV3 (SQ) |
| `script` (`P3-nn` markers) | `p3_checks.py` | OPS-TV3 (SQ) |
| `delegations`, `automations`, `followups`, `reminders`, `exports` (views) | `p3_views.py` | OPS-VIEWS (SQ, R2) |
| `report` (content-free trial-report aggregator: counts, NFR numbers) | `p3_report.py` | OPS-VIEWS (SQ) |
| `restore-check` (delegations ended `restored_from_backup`, scheduled autonomous actions cancelled, nothing resumes) | `p3_restore_check.py` | OPS-RT3s (D2) |

Arguments (R32; the specs define the exact semantics): `release --expect-head`, `--policy-version`, `--expect-flags`, `--expect-languages` and a repeatable `--expect-setting`, so the daily-check line can assert the migration head, the policy version, the frozen flag set and the enabled gate languages; `trial-check --since --until --shadow`; `script --answers`; `report --since --until` (an `--until` in the future is refused). P3's `release` prints allow-listed `AUTONOMY_*` settings and its `script` exits 1 when a marker is missing or unexpected, both differing from the P2 verifier by design. Cross-argument checks (`--since < --until`) run inside `run`, after the read-only connection opens and before any statement.

### 14.3 `deploy/server.sh` actions and SQL names (F0-E)

- `report <name> [days]`: `<name>` must match `^[a-z0-9_]+$` and `deploy/sql/<name>.sql` must exist (the file list is the whitelist); runs read-only through psql with `-v days=<n>` (default 7); content-free output.
- `p3-verify <args>`: passes the arguments to `python tools/acceptance/p3_verify.py` in the app container, as `p2-verify` does. F0-E points the existing `trial-check` action at `p3_verify.py trial-check` (R56: the M04 trial is over by R1, and the P2 checker would fail every `delegation_auto` row); it reaches production with R1, and LIVE-P3 uses `p3-verify trial-check` explicitly.
- `deploy/sql` names: `nfr04` (M04), `nfr05` (M05; escalation-latency extension by OPS-AV), `nfr07` (pause latency per source, from the `autonomy_paused` audit fields of §13, and including the fast-lane lock wait; zero autonomous claims after a pause; OPS-AV), `nfr08` (minute coverage ∪ outage and restore gaps; OPS-AV), `usefulness` (OPS-RATE). P3 units add SQL or tool files only, never `server.sh` lines.

### 14.4 E4 v1.1 records and the shared per-item record (R32)

`docs/engineering/p3/specs/EV-E4L.md` §3.1–§3.6 is the normative text; this section freezes the names the DATA units author against and EV-AUT and EV-GATE build against. A change after DATA-E4a starts, or to `AutonomyDetail` after EV-E4L merges, is an ICR between those units, ruled by the TL.

- **Case record** (`evals/datasets/e4/cases.jsonl`; v1 lines a001–a080 load unchanged; the rules below apply to every v1.1 record from `a081`): `id`; `variant` (exactly the four `GateLanguage` values); `category` (v1 `are_you_a_bot`, `injection`, `cross_conversation_extraction`, `commitment_bait`, `sensitive_topic`; v1.1 adds `in_scope`, `re_disclosure`, `knowledge_scope`); `delegation.{topic (nullable), scope, allowed (the four `Behaviour` values), knowledge, contact_listed (default true), share_widened (default false), canary (nullable)}`; `conversation.{owner, contact, messages}` (1–6 messages); `expected.{safe_behaviour (v1.1 adds `auto_reply` and `no_knowledge_reply`), must_not, may, behaviour (nullable), forbidden_strings (default empty), disclosure_required (nullable)}`; `notes`. An E4 message has `direction` `contact` or `assistant_sent` (never `owner`), an aware-UTC `sent_at`, and `disclosure_included` on `assistant_sent` only (required on v1.1 records and equal to the 24 h reading of §5.1 at that message's own time).
- **Decoys** (`evals/datasets/e4/decoys.jsonl`, from DATA-E4b): `id` (`d01`…), `variant`, `contact`, `messages` (contact only), `canary`, `knowledge` (0–4 lines of a decoy delegation covering the chat), `knowledge_canary`. Decoys are never evaluated; their canaries must never appear in any reply.
- **Integrity** (`evals/e4.py::check_e4(cases, decoys, *, e2=None)`, `E4IntegrityError`; the message names the rule, the record id and the field, never text) and the loader constants `E4_PATH`, `DECOYS_PATH`, `ITEM_DELAY` (equals `evals.drafts.DRAFT_DELAY`), `SESSION_LEAD`, `OD32_GAP`, plus `E4_MINIMUMS` (per-language minimums are tests, not load errors).
- **`AutonomyDetail`** (`evals/e4.py`): the per-item record EV-AUT writes into a run report's `details` and EV-GATE parses (a report whose details do not validate is refused): `OutcomeClass` (`auto`, `card`, `escalate_only`, `no_reply`, `not_drafted`), `JudgeFindingKind`, `FindingKind` (adds `unscreened`), `DetailLeak`, `DetailFinding`, `DetailFacts` (the content-free `AutonomyFacts` subset), `DetailCheck`, `DetailDecision`, and the helpers `finding_statement`, `checked_reply` and `model_written`. The identity sentence is application text: it is never judged, rated or pattern-checked.
- **Datasets freeze** from the candidate freeze (D+4) until CP-G: EV-GATE refuses a run whose `e2/situations.jsonl`, `e4/cases.jsonl` or `e4/decoys.jsonl` digest differs from `MANIFEST.sha256`.
- **Gate ledgers.** The autonomy gate uses its own review ledger `evals/reviews/autonomy.json`, separate from Assist's `evals/reviews/e2.json`, and encodes the judge model in the rubric label `autonomy_safety.v1+judge:<model>`.
- Contact text and `assistant_sent` text are never grounding sources (§3.7), so every value a right reply needs must be in a `knowledge` line and the contact's last message must carry no value to repeat; otherwise a correct reply that echoes it counts as ungrounded and shows up as an informational "usefulness miss" in the gate report (EV-GATE §3.3).

## 15. File and section ownership (after CP1)

### 15.1 Files

One accountable owner per file. A file not listed here is a new module, and its only editor is the unit that creates it (§1.1). Where a row names several units, they all belong to one team, edit separate functions, and that team's lead (Josh, Edvard or David) is the single accountable owner who sequences them; no file is shared across teams except `bootstrap.py`, where each team owns only its own wire function (§15.3), and `docs/ARCHITECTURE.md`, where each unit owns only its sections (§15.4).

| File / area | May edit after CP1 | Rule |
|---|---|---|
| `migrations/versions/*`, `migrations/env.py`, `db/models.py`, `privacy/{content_columns,retention,deletion,p3_deletion,post_restore}.py`, `inbox/backup_check.py` | J1 (schema steward) | A schema change is revision `0016`, via ICR. `privacy/export.py` is M7-EXP's new file |
| `actions/models.py`, `memory/models.py`, `conversations/models.py`, `inbox/models.py`, `automation/models.py` | J1 (schema steward) | ORM mirrors change only with a migration, via ICR |
| `tests/integration/{test_migrations,test_data_deletion,test_managed_data_deletion,test_post_restore,test_retention_p3,test_p3_constraints,test_p3_schema_mirrors,test_p3_end_primitives,test_p3_factories,p3_factories}.py` | J1 | Another unit that needs a new factory or canary asks J1 (ICR) |
| `tests/integration/execution_env.py` (`harness`, `inserted`) | F0-S (J1) until CP1; then M6-TXA (J1); Josh sequences | R41. F0-S adapts `inserted` to the new one-in-flight unique index and CHECKs (default origin or chat; the existing users `test_execution.py`, `test_mark_uncertain.py`, `test_status_actions.py` stay green); M6-TXA then gives the harness a send-on `AutonomyConfig` by default and autonomous rows `scheduled_for` and `late_after` |
| `tests/fakes/autonomy.py`; other `tests/fakes/*` | nobody after F0-S; GW-DOC adds `send_document` to the channel and sender fakes | Named touch point |
| `policy/context.py`, `policy/codes.py` | nobody | ICR |
| `policy/rules.py`, `policy/engine.py`, `policy/explanations.py` | M6-POL | |
| `actions/states.py`, `actions/ports.py`, `actions/disclosure.py`, `actions/config.py` | nobody | ICR |
| `actions/lifecycle.py` | M6-SCHED (bulk helpers) | The event table and `cancel_pending_for_delegations` are frozen |
| `actions/proposals.py` | M6-PLAN, then NUDGE-POL | Josh lead sequences the two |
| `actions/policy_loader.py` | F0-S (the three autonomy fact builders as fail-closed stubs, R79); then M6-PLAN (fills them), M6-TXA, M6-TAKE, NUDGE-POL | Separate functions; Josh lead sequences. M6-PLAN has no touch point in `actions/release.py::_decide` (§3.5) |
| `actions/executor.py` | M6-TXA | |
| `telegram/control/approvals.py` | M6-PLAN (the named touch points of §1.2: R80, and the OD-11 held-reply rule); M6-TXA's late conversion reuses it | The autonomy-explanation line and the "was due at … (late)" note on a held reply's card, and the held-reply skip in `sync` and `sync_cards` (§2.3) only (OD-11) |
| `telegram/control/draft_reply.py` | M6-ESC | New (OD-11) |
| `actions/ingestion.py` | M6-SCHED | |
| `actions/autonomy.py` | M6-TAKE (`takeover_counts`, `note_takeover`, `resume_here`, `paused_by_takeover`), M6-PAUSE (pause sources, the `lock_account` wrapper, `pause_after_control_forbidden`) | Separate functions |
| `actions/{autonomy_planner,release}.py` | M6-PLAN; M6-SCHED | New |
| `actions/{grounding,language_keys}.py`; `tests/unit/{test_grounding,test_language_keys}.py` | GRND | Signatures frozen (§3.7); bodies replace F0-S's stubs; GRND owns and replaces the stub tests F0-S creates |
| `actions/activity.py`, `agent/tools/activity.py`, `telegram/control/activity.py` | STAT-ACT | `Authorization` and the delegation lookup only; the three `get_activity` keys; the suffix helper (§1.2; named touch points) |
| `actions/drafts.py`; `agent/drafting.py`; `inbox/notify.py` (the `ALERT_DRAFT_FAILED` kind only) | F0-S only (the `ALERT_DRAFT` named touch points of §1.2, OD-11, P3-T154); nobody after F0-S | |
| `conversations/service.py` | M6-TAKE | The takeover marker, and one line in `record_ingest_failure` (cancel autonomous actions first) |
| `telegram/gateway.py`, `telegram/control/agent_channel.py` | GW-DOC (`send_document`; `GatewayControlChannel.send_document`); GW-ESC only if its verification finds a gap in P2 #133 (J3's first `int/p3` PR) | GW-ESC landed in P2 #133; verification only. GW-DOC also adds the one-line adapter change if `Delivery.error` lacks the forbidden code (M6-PAUSE checks first; Josh sequences; R100) |
| `telegram/ingress/poller.py` | nobody after F0-S | |
| `telegram/ingress/fast_lane.py` | M6-PAUSE | |
| `telegram/control/inbox_handlers.py` | M6-PAUSE | The `my_chat_member` (bot blocked) branch only: it logs the status code |
| `telegram/control/{dispatcher,routes,handlers,panels}.py` | nobody after F0-R | New screens in new modules, registered through `ControlRoutes` |
| `core/*` | nobody after F0-S/F0-R | GW-DOC: `core/channel.py::send_document` (named touch point) |
| `config.py`, `.env.example` | nobody after F0-S | New settings via ICR |
| `automation/{delegation_models,delegation_rules,delegations_repo}.py`, `memory/shareable.py` | nobody | ICR; J1 implements a ruled change to `delegation_models.py` and `end_delegations`, J2 to `delegation_rules.py`, the read seams and `shareable_for` |
| `automation/schedules.py` | M7-AUTO (`reschedule`), M7-FUP (`_still_runs`) | Named touch points only |
| `automation/rules.py` | M7-MACT | `MonitorRule.action` Literal only |
| `automation/monitors.py`, `agent/monitor_schemas.py`, `agent/tools/monitors.py` | M7-MACT | M7-FUP imports the matcher, never edits it (a generalisation needs an ICR) |
| `automation/repository.py` | nobody | ICR |
| `inbox/notification_types.py` | nobody after F0-S | ICR |
| `inbox/notification_policy.py`, `inbox/notifications.py` (fact loading only), `inbox/triage_store.py` (effects) | M6-ESC; M6-PAUSE for the one `pause_after_control_forbidden` call in control-chat delivery (R46) | M6-PLAN supplies only the requester |
| `inbox/tasks.py` | M7-TASK | M7-REM, M7-FUP and M7-MACT insert their tasks from their own modules (or call M7-TASK's owner functions once merged, M7-MACT through `create_owner_task`'s `source` keyword, R83); they never edit this file |
| `accounts/service.py`, `inbox/policy_notice.py` | PV6 (E2) | `POLICY_VERSION`, `POLICY_CHANGES` and the notice text only; plus the tests that pin the current policy version `v5` (`test_policy_update_notice.py`, `test_connections.py`, `test_visibility.py`) as named touch points |
| `inbox/digest.py`, `agent/tools/summaries.py`, `memory/catch_up.py` | M7-TASK | Separate snooze-filter functions |
| `inbox/alert_texts.py` | nobody | Renderers register on the delivery service |
| `memory/items.py` | F0-S (the two named touch points of §1.2: the `contact_facing` shareable exclusion, R54, and the `forget` guard for `pending_review` rows); M6-DELEG | Shareable write path |
| `memory/undo.py` | nobody after F0-R | Handlers via `ControlRoutes.undo_handlers` |
| `agent/untrusted.py` | nobody after F0-S | Append-only, via ICR |
| `agent/tools/registry.py` | nobody after F0-R | Append to your own module's `TOOLS` |
| `agent/tools/{delegations,delegation_reads,reminders,tasks,task_reads,followups,automations,automation_reads}.py` | M6-DELEG; RD-DEL; M7-REM; M7-TASK; RD-M7; M7-FUP; M7-AUTO; RD-M7 | Own module only |
| `agent/runtime.py`, `agent/context.py`, `agent/schemas.py`, `agent/tools/executor.py` | nobody | ICR |
| `agent/prompt_registry.py` | Each prompt owner: its own line and history entry; the `owner_request` line only PR-OR10 | §9.2 |
| `agent/prompts/*` | The allocated owner (§9.2) | A used version is never edited |
| `agent/tools/memory.py` | M6-DELEG | One branch |
| `agent/tools/status.py`, `observability/status.py` | nobody after F0-R | Blocks live in `observability/status_p3.py` (STAT-ACT) |
| `jobs/tasks.py`, `jobs/reconcile.py`, `jobs/tasks_surfaces.py` | nobody after F0-R | Juniors write handler functions only |
| `jobs/tasks_{delegation,release,export,escalation,followups,memory,summaries}.py` | M6-PLAN; M6-SCHED; M7-EXP; M6-ESC; M7-NUDGE; M7-MEX; M7-SSUM | Own module |
| `jobs/reconcile_delegation.py` | M6-PLAN (pending part), M6-LIFE (overdue part) | Separate functions |
| `jobs/reconcile_actions.py`; `jobs/reconcile_memory.py` | M6-SCHED; M7-MEX | |
| `bootstrap.py` | Each team inside its own wire function only (§15.3) | The import block is frozen |
| `evals/registry.py` | One line per target owner (EV-AUT) | |
| `evals/{cli,targets,live,agent_mode}.py`, `tests/integration/{agent_env,test_agent_runtime}.py`, `tests/scenarios/test_find_the_agreed_meeting_time.py`, `tests/unit/{test_tool_registry,test_e1_dataset}.py` | nobody after F0-E | Derived lists and registry-read labels |
| `evals/drafts.py`, `tests/unit/test_eval_drafts.py` | EV-GATE | Thresholds hook |
| `evals/datasets/**`, `evals/datasets/MANIFEST.sha256`, `docs/evals/README.md` (dataset-log rows) | DATA-E2, DATA-E4a, DATA-E4b, DATA-FIX, DATA-E3F (David lead sequences them) | Manifest regenerated in every commit |
| `tests/unit/test_e2_dataset.py`; `tests/unit/test_e3_dataset.py` | DATA-E2; DATA-E3F | Existing P2 files: append-only blocks |
| `tests/unit/test_e4_dataset.py` | EV-E4L (creates it); DATA-E4b (removes one strict-xfail line) | |
| `tests/unit/test_e1_dataset.py` | DATA-E4b (one appended literal-set line, `"e4/decoys.jsonl",`, on `main` before F0-E); F0-E (then derives the set and must keep that file in it) | A one-line exception to "nobody after F0-E": it applies only while the set is literal |
| `evals/README.md` | EV-E4L, EV-AUT, EV-GATE (each one section; Edvard lead sequences) | Named touch point |
| `evals/reviews/**`, the P3 gate-run reports in `docs/evals/`, the "Real-model runs so far" rows of `docs/evals/README.md` | GATE-RUN (SQ) | One report file per run; never an edit to a dataset |
| M06/M07 exit eval summaries in `docs/evals/` | TL (DOCS-EXIT) | Separate files from the gate-run reports |
| `tests/integration/test_conversation_modes.py` | nobody after F0-R | F0-R split its keyboard assertions per builder; a P3 builder's keyboard tests (SET-P3's Delegate row, …) go in the owning unit's own test file |
| `tests/scenarios/m06/**`, `m07/**`, `p3_cross/**` | QA-S6 (QJ1), QA-S7 (QE1), QA-X (SQ) | Only the package's QA owner edits it; a unit whose merge makes a strict xfail pass tells that owner, who removes the marker in a commit on the feature unit's branch (a strict XPASS would turn the feature PR red; R47) |
| `tests/scenarios/p3_drive.py`, `tests/scenarios/test_p3_drive.py`, `tests/log_hygiene.py` | F0-E (creates them); SQ after CP1 (R57) | |
| `tools/acceptance/p3_verify.py` (F0-E creates the CLI and registry), `p3_checks.py`, `p3_views.py`, `p3_report.py` | SQ (OPS-TV3, OPS-VIEWS) after CP1 | |
| `tools/acceptance/p3_restore_check.py`, `tools/acceptance/restore_verify.py` (one import and `main()`'s `with` block), `deploy/runbooks/restore.md` (P3 section) | OPS-RT3s | |
| `deploy/runbooks/deploy.md` (a "P3 verification and daily trial audit" section) | SQ (OPS-TV3) | The procedure lives in `qa/p3-verifier.md` and LIVE-P3 until this section is written |
| `tests/integration/test_p3_wiring.py`; `tests/unit/test_tool_registry_p3.py` | F0-R; each David unit appends its wiring test in a separate commit; each tool unit changes only its own module's expectation in a separate commit | Named touch points: `test_empty_wire_functions_change_nothing` is scoped to F0-R's own empty state (§10.2) |
| `deploy/server.sh`, `.github/workflows/ci.yml` | nobody after F0-E | Add SQL or tool files only |
| `deploy/sql/{nfr07,nfr08}.sql`, `deploy/sql/nfr05.sql` (extension); `deploy/sql/usefulness.sql` | OPS-AV; OPS-RATE | |
| `pyproject.toml` | GW-DOC (banned-API entries for `aiogram.types.BufferedInputFile`, `FSInputFile`, `URLInputFile` and `aiogram.types.input_file`, with a per-file ignore for `gateway.py`, §4.6) | Anything else via ICR |
| `docs/legal/privacy-policy.md` | TL (PV6) | Version bump; owner approval |
| `docs/engineering/p3/**` (except `qa/**`), `docs/milestones/M06.md`, `M07.md` | TL | Engineers put report text in their PR description |
| `docs/engineering/p3/qa/**` (test plan, `p3-verifier.md`, `evidence/**`, trial log) | SQ (QA-P3, OPS-TV3, LIVE-P3) | |
| `docs/IMPLEMENTATION_PLAN.md` | TL | SQ proposes the RG rows in the QA plan and the TL applies them (F0-C part 2, DOCS-EXIT); §1.4 only with the owner's approval |
| `docs/TELEGRAM_FEASIBILITY.md` | TL | §12 only from owner-executed tests (OWN-LV16); §13 docs-checks (GW-DOC's `sendDocument` row, F0-C rows) |
| `docs/ARCHITECTURE.md` | Per §15.4 | Each unit edits only its sections, in its own PR |
| `AGENTS.md` | TL, with the owner's agreement | |

### 15.2 Never edit without an ICR

| Entry | The only editors (all Josh units unless named) |
|---|---|
| `policy/*` | M6-POL (`rules.py`, `engine.py`, `explanations.py`); `context.py` and `codes.py` frozen |
| Send-path `actions/{executor,lifecycle,states,proposals,ingestion,policy_loader,autonomy}.py` | `executor` M6-TXA; `lifecycle` M6-SCHED; `states` frozen; `proposals` M6-PLAN → NUDGE-POL; `ingestion` M6-SCHED; `policy_loader` M6-PLAN, M6-TXA, M6-TAKE, NUDGE-POL; `autonomy` M6-TAKE, M6-PAUSE |
| `telegram/gateway.py`, `telegram/ingress/*` | GW-DOC; GW-ESC only for a gap its verification of P2 #133 finds; `fast_lane.py` M6-PAUSE; `poller.py` frozen after F0-S |
| `conversations/service.py` | M6-TAKE |
| Migrations, `privacy/*` | J1; M7-EXP for its new `privacy/export.py` |
| `llm/consent.py` | nobody (no P3 change planned) |
| `agent/{runtime,context,schemas}.py`, `agent/tools/executor.py` | nobody after F0-R / F0-S |
| `agent/prompts/*` outside the allocation | nobody: each file has one owner (§9.2) — DELEG-DRAFT, M7-MEX and PR-OR10 are Edvard units |
| `config.py`, `core/*` | nobody after F0; GW-DOC's `core/channel.py` touch point |

An edit to a listed file by any other unit, even one line, is a blocking review finding (WORKFLOW). A junior puts any edit outside its spec's allow-list in a separate last commit with a comment on the integration issue. The TL rules an ICR within 2 h; rulings are batched into the day's decisions PR.

### 15.3 `bootstrap.py` wire functions (F0-R)

```python
def _wire_autonomy(ctx: P3WireContext) -> P3Wiring: ...  # Josh units
def _wire_automation(ctx: P3WireContext) -> P3Wiring: ...  # Edvard units
def _wire_surfaces(ctx: P3WireContext) -> P3Wiring: ...  # David units
```

- `P3WireContext` (frozen): `settings`, `session_factory`, `clock`, `scheduler`, `llm`, `roles`, `gateway`, `notification_delivery`, `fire_registry`, `actions_config`, `autonomy_config`, `ports` (the `P3Ports` assembled so far), `executor_factory` (a `functools.partial` of `ActionExecutor` with today's arguments, set only for the `worker` or `system` role), `approval_cards: ApprovalCardPresenter | None` (`_wire_m04`'s `wiring.approval_cards`, the S04.3 presenter; `None` where it is not built; OD-11, closes contract gap CG-8) and `final_ports()` (§4.5).
- `P3Wiring` (frozen fragment): job handler groups, reconcilers, ingestion listeners, a `ControlRoutes` fragment (callbacks, pending inputs, commands, help lines, panel and settings rows, undo handlers, status-block loaders), `P3Ports` fields, `poll_observer` (OPS-AV, passed to the poller) and `executor` (at most one fragment may set it, a second raises `ValueError`; when set it replaces the M04 executor held by `ActionsJobHandlers` and any other CP5 holder).
- **ICR-P3-1 (pre-approved, R43).** `AutonomyConfig` reaches `ActionExecutor` through `_wire_autonomy`: F0-R makes the construction in `_wire_m04` late-bindable without changing it (`wiring.executor_factory`, so with no P3 fragment the executor is identical to CP5's), and M6-TXA returns `ctx.executor_factory(autonomy=ctx.autonomy_config)` as `P3Wiring.executor`. Until then every autonomous claim is cancelled `sending_disabled` (fail closed).
- Bootstrap calls them in the order autonomy → automation → surfaces and merges the fragments; a duplicate callback kind, pending-input kind, command, task name, fire kind, renderer kind, undo event (including a collision with the built-in undoable events), job group or `P3Ports` field raises at startup. Function-local imports only; the module import block is frozen. Ingestion listener order (R74): actions → triage → monitors → follow-ups → memory. The memory listener is always registered, so the withdrawal of an unreviewed suggestion whose cited message was deleted or edited works with the flag off; extraction itself is gated by `memory.extract`. The ARCHITECTURE §6.2 clause is in F0-C part 2; M7-FUP owns only the sentence about what the follow-up listener does.
- **`_wire_surfaces` convention.** Several David units add to the one return value, so each starts the function with `wiring = P3Wiring()`, extends it in its own block with `dataclasses.replace`, and the function ends with `return wiring`. No unit assembles its own accumulators or returns early.
- **Reusable test builder.** F0-R exports `p3_wire_context(*, flags: frozenset[str] = frozenset(), **overrides) -> P3WireContext` from `tests/integration/test_p3_wiring.py`; the COMMIT-DUE, OPS-AV and OPS-RATE wiring tests import it instead of building a context by hand.
- **What each wires.** `_wire_autonomy`: `AutonomyPlanner` and the `DelegatedDrafter` implementation `agent/delegated_draft.py::DelegatedDraftRunner(*, session_factory, llm, clock, config: DraftConfig)` (M6-PLAN instantiates DELEG-DRAFT's class), `DelegationRequester` (M6-PLAN), `DelegationControls` (M6-DELEG), the `delegation_end` fire handler (M6-LIFE), groups `delegation`, `release`, `export`, reconcilers `unqueued_delegated`, `overdue_delegations`, `unqueued_releases`, routes `tko` (M6-TAKE), `pz` (M6-PAUSE), `/export` + `exp` (M7-EXP), the `takeover_paused` renderer. `_wire_automation`: `AutomationControls` (M7-AUTO), `TaskControls` (M7-TASK), fire handlers `reminder`, `follow_up`, `scheduled_summary`, the follow-up listener, the memory listener (M7-MEX, always registered), groups `escalation`, `followups`, `memory`, `summaries`, reconciler `unqueued_extractions`, renderers of M6-ESC (including `autonomy_not_sent`), M7-REM and M7-FUP, routes `drf` (M6-ESC, OD-11), `rem`, `fup`, undo handlers of M7-REM and M7-TASK. `_wire_surfaces`: `CatchUpRequester` + the `delegation_catch_up` renderer (M6-CATCH), status blocks (STAT-ACT), settings and panel rows + `p3d`, `p3w`, `p3a`, `deleg_wording` (SET-P3), `memory_review` renderer + `mem`, `mem_edit` (M7-MRV), `usefulness_rating` renderer + `rate` + the weekly handler (OPS-RATE), the commitments handler (COMMIT-DUE), the `PollObserver` (OPS-AV).

### 15.4 ARCHITECTURE sections each unit updates (as-built text, in its own PR)

The TL corrects §7.1, §7.3, §7.5, §8.1, §8.2, §9.3, §9.4, §12.2, §12.4, §15.2 and adds ADR-17…21 to §20 in F0-C part 2 (§20 ends at ADR-16; R26); F0-S carries the TL's text for §10.1–§10.4 and §15.6 (P3-schema Appendix A).

| Unit | Sections |
|---|---|
| F0-R | §5.1 (new modules), §12.4 (fire registry) |
| F0-E | §16 (scenario packages, hygiene convention), §17 (`p3-verify`, `report`) |
| M6-POL | §9.2 (outcomes), §9.3 (rules and codes as built) |
| EV-GATE | §16 (the per-language gate and the autonomy eval target; R90). F0-E's §16 text (scenario packages, hygiene convention) is a separate paragraph |
| GRND | §11.2 (tagger → gate-language keys) |
| M6-PLAN | §9.4 (autonomous proposal path), §12.2 `replies.delegated` |
| M6-SCHED | §9.4 (scheduled exits as built), §12.2 `actions.release_scheduled`, §13 |
| M6-TXA | §9.4 (Tx A for autonomy), §13 |
| M6-TAKE | §6.5 step 4 (takeover) |
| M6-PAUSE | §6.1 (fast lane), §18 (bot-blocked row) |
| M6-DELEG | §7.5 (`DelegationRules` as built), §8.2 delegation rows |
| M6-LIFE | §12.4 (`delegation_end`) |
| GW-DOC | §6.8 (`send_document`) |
| M7-EXP | §6.7 (`/export`), §10.4 (export row) |
| DELEG-DRAFT | §7.1, §7.3, §7.5 (`DelegatedDraftResult`), §7.6 (prompt key) |
| M6-ESC | §17 (escalation delivery and notices), §18 (degraded-mode rows) |
| M7-REM, M7-TASK | §8.2 rows |
| M7-FUP | §6.2 (what the follow-up listener does; the listener-order sentence is F0-C part 2's, R74), §7.5 (`FollowUpSpec`), §8.2 row |
| NUDGE-POL | none: the §9.3 sentence about the automation-triggered `no_takeover_since` reference is among the TL's F0-C part 2 corrections (R52) |
| M7-AUTO | §7.5 (`AutomationSpec`), §8.2 rows |
| M7-NUDGE, M7-SSUM | §12.2 rows |
| M7-MACT | §7.5 (`MonitorRule` actions), §8.2 (the `create_monitor` row) |
| M7-MEX | §7.1 `extract_memory`, §7.5 `MemoryCandidates`, §12.2 `memory.extract` |
| PR-OR10 | §7.6 (prompt version note) |
| PV6 | §14.5 (policy version) |
| David's units, through their mentor | STAT-ACT and M6-CATCH §17; RD-DEL and RD-M7 §8.2 rows; SET-P3 and M7-MRV §6.7; COMMIT-DUE and OPS-RATE §12.2 periodic rows; OPS-AV §17 (NFR-08 recorder) |

### 15.5 Merge order inside `int/p3`

0. **GW-ESC: landed in P2 #133; verification only.** The fix is on `main` before CP5; F0-C and QA confirm it covers P2-T53. A gap they find is the first PR into `int/p3`, by J3. No P3 renderer merges before GW-ESC is closed.
1. **F0-S → F0-R → F0-E** (CP1, target D+1 morning).
2. The **QA-S6, QA-S7 and QA-X** skeletons (strict xfail).
3. Pure units: **M6-POL, GRND, RRULE, SENS**.
4. Services: **M6-SCHED, M6-DELEG, M7-TASK, M7-REM** (M7-REM carries the `reminder_due` renderer and buttons, R2).
5. Gate chain: **DELEG-DRAFT → M6-PLAN → M6-TXA**; M6-PLAN also depends on M6-SCHED (step 4), whose `schedule_autonomous` its job calls (R18). J7 chain: **NUDGE-POL → M7-FUP → M7-NUDGE** (M7-FUP carries the follow-up and skipped-occurrence renderers, R2).
6. Consumers: **SET-P3, STAT-ACT, RD-DEL, RD-M7, M6-CATCH**. Every other unit merges in the TL's dependency batches once it is green and reviewed.
7. **PR-OR10** last.

The TL merges dependency batches about twice a day and never into a red `int/p3`; a branch merges its base only right before its own merge. R1 (`int/p3` → `main`) is the owner's merge (OD-2).
