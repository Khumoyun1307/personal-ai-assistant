# F0-R — product registries and hot-file refactors (behaviour-neutral)

| | |
|---|---|
| **Unit** | F0-R — fire and rule registries, per-module tool tuples, one `ToolServices`, domain wire functions, `core/p3_ports.py`, P3 job groups and pre-created job modules, triage and delivery hooks, control-chat hooks, undo registry |
| **Team / slot** | Edvard / E1 |
| **Size** | M+ (≈ 1.5 engineer-days) |
| **Depends on** | CP5, F0-C. Branches from `main` right after CP5 like every F0 PR, rebases on `p3/f0-s` (it reads F0-S's constants: `core/jobs.py` task names, `core/flags.py`, `inbox/notification_types.py` kinds, `DeliveryClass` and the `feedback_buttons` column of the delivery table, `actions/config.py::AutonomyConfig`), and **merges second**, after F0-S (README §3; P3-interfaces §15.5 step 1) |
| **Branch → base** | `p3/f0-r` → `int/p3` (draft stacked on `p3/f0-s` until F0-S merges, then retargeted; WORKFLOW §4.2) |
| **Peer reviewer** | Josh lead (a Claude reviewer briefed as Josh lead, dispatched by the TL) |
| **QA** | QE1, before merge |
| **Tier** | 1 (high and safety-adjacent): TL scope check (ORG §3) |
| **Normative inputs** | P3-interfaces §4.5, §7.1–§7.3, §8.1–§8.2, §9.5, §11.2–§11.3, §12.2, §12.6, §12.7, §15.1–§15.3; decisions P3-T14, P3-T26; TL rulings R2, R17, R43, R45, R82 (feedback buttons), R95 (`FirePostponed`); AGENTS §5, §6 (invariants 2, 3b), §13; ARCHITECTURE §5, §6.7, §12.1–§12.4 |

## 1. Goal

Turn every file that several P3 units would otherwise edit into a registration point, so that after CP1 each unit adds code only in its own modules and its own wire function. **No product behaviour changes:** the whole suite stays green, every M04/M05 path behaves exactly as before, and with all P3 flags off the running system is indistinguishable from CP5. Two P2 defects are fixed on the way: the confirmation applier gets the same `ToolServices` (and the same `enabled_flags`) as the owner runtime.

## 2. Scope

**In:**
1. `automation/schedules.py`: a `FireRegistry` (`register_fire_handler(kind, handler, *, flag, skip_policy, on_skipped)`), the fire path dispatching through it, the `on_skipped` hook, `FIRE_LATE`; the digest re-registered on it; the quiet `FirePostponed` outcome (R95). `jobs/tasks_automation.py::ScheduleJobs.fire` uses the registry (named touch point).
2. `automation/rules.py`: `ReminderRule`, `FollowUpRule`, `ScheduledSummaryRule`, `DelegationEndRule` and per-kind dispatch in `load_schedule_rule`.
3. `agent/tools/registry.py`: `default_registry` concatenates per-module `TOOLS` tuples; eight empty P3 tool modules; `ToolServices` P3 fields. `agent/runtime.py` and `bootstrap.build_*`: one `ToolServices` shared by the runtime and the confirmation applier.
4. `bootstrap.py`: `P3WireContext`, `P3Wiring`, `_wire_autonomy`, `_wire_automation`, `_wire_surfaces` (empty bodies), the fragment merge, duplicate detection, the triage store built after the P3 ports exist, and the executor seam through which `_wire_autonomy` hands `AutonomyConfig` to `ActionExecutor` (R43, ICR-P3-1; §3.4 step 8).
5. `core/p3_ports.py`: `PortResult`, the five port protocols, `P3Ports`; the late-bound final ports for wiring and tools (§3.4, contract gaps noted in §9).
6. `jobs/tasks.py`: the eight `JobHandlers` P3 groups and their `register_*` calls; new `jobs/tasks_{delegation,release,export,escalation,followups,memory,summaries,surfaces}.py` with every P3 task of P3-interfaces §7.1 registered against a handler field that starts unwired; new empty `jobs/reconcile_{delegation,memory}.py`; `jobs/retry.py` (moved `RequeueOnShutdown`).
7. `inbox/triage_store.py`: the `delegation_requester` hook (called, result ignored).
8. `inbox/notification_policy.py`, `inbox/notifications.py`: `decide_delivery` consults F0-S's delivery-class table; `NotificationFacts.active_delegation` slot (always `False` here); the alert card consults the table's `feedback_buttons` column (R82).
9. Control chat: `telegram/control/routes.py` P3 fields; `panels.py` row-builder hooks; `handlers.py` help text built from registered lines and `render_status` printing status blocks; `dispatcher.py` passing the routes through (named touch point); `observability/status.py` and `agent/tools/status.py` status-block loaders; `memory/undo.py` event → handler registry.
10. `tests/integration/test_conversation_modes.py` (and any other test asserting a full panel or `/settings` keyboard): assertions per builder.
11. ARCHITECTURE §5.1 and §12.4 as built.

**Out:** any handler body, renderer, tool, job body or reconciler (the owning units); the M7-MACT `MonitorRule.action` widening; F0-S's constants, kinds, delivery-class table, settings and flags (F0-R consumes them); F0-E's eval, prompt-registry, CI and ops seams; the covered-chat delivery rule and triage effects (M6-ESC); `reschedule` (M7-AUTO) and the `_still_runs` race (M7-FUP).

## 3. Design

Build only against the names in P3-interfaces; invent none. Where this spec fixes a detail the contract leaves open, the detail is marked "(F0-R)". Paths are under `src/assistant/`. Line references are to `main` (M04) or `int/m05` (M05) as named; the CP5 tree has both.

### 3.1 Fire registry (`automation/schedules.py`; P3-interfaces §8.1)

Today `plan_fire` (m05 `automation/schedules.py:605`) settles every non-digest kind `no_handler` and returns `FirePlan(FireAction.DIGEST)`; `ScheduleJobs.fire` (m05 `jobs/tasks_automation.py:147`) then defers `digest.build` and marks the occurrence fired in a second transaction.

1. **Types**, exactly as P3-interfaces §8.1: `SkipPolicy`, `FireContext`, `SkippedOccurrence`, `FireHandler`, `SkipHook`, `FireRegistry`. (F0-R, R95) `FirePostponed(Exception)`: raised by a fire handler that cannot settle the occurrence yet (M7-FUP's ingestion-lag guard); it carries no data and the handler logs its own reason (ids only). (F0-R) `FireRegistry.__init__(self, *, enabled_flags: frozenset[str])` (the process's `FEATURE_FLAGS`); a read method `registration(kind) -> FireRegistration | None` (frozen dataclass `kind, handler, flag, skip_policy, on_skipped`). It is built once in `bootstrap.py` and passed to `ScheduleJobs` (fire and reconcile); no module-level registry (AGENTS §5).
2. **Startup checks** (`register_fire_handler` raises `ValueError`): a kind registered twice; a kind outside `SCHEDULED_KINDS`; `FIRE_LATE` with a non-`None` flag; `on_skipped` together with `FIRE_LATE`.
3. **Fire path.** `plan_fire(session, *, automation_id, occurrence_at, registry, now) -> FirePlan` replaces the `digest_enabled` parameter. Order, keeping the as-built order so every existing digest test is unchanged (F0-R; see §9 note 1):
   1. Lock the occurrence row; anything but `claimed` → `FireAction.NONE` (unchanged).
   2. Account not active or the automation no longer runs → settle `inactive` (unchanged).
   3. For kinds that are **not** `FIRE_LATE`: past `expires_at` or `occurrence_at + max_lateness` → settle `expired` / `skipped_obsolete` (unchanged); for `skipped_obsolete` of a registered kind with `on_skipped`, call the hook in the same transaction.
   4. No registration → settle `no_handler` (unchanged).
   5. Registration with a flag not in `registry.enabled_flags` → settle `disabled` (the digest's `digest.daily` check moves here; same note as today).
   6. Otherwise: build `FireContext(account_id, automation_id, kind, occurrence_at, timing=timing(...), now)`, call `await handler(session, ctx)` inside `async with session.begin_nested()`, then `mark_fired` in the **same** transaction. Return `FirePlan(FireAction.DIGEST if kind == "digest" else FireAction.FIRED, account_id)` (F0-R: new member `FIRED`; `DIGEST` kept so existing assertions hold).
   7. **`FirePostponed` (R95).** When the handler raises `FirePostponed`, the savepoint rolls back, the occurrence is **not** marked fired and stays `claimed`, and `plan_fire` returns `FirePlan(FireAction.POSTPONED, account_id)` (F0-R: new member `POSTPONED`). It logs `fire_postponed` at info with the automation id, kind and occurrence epoch only. `ScheduleJobs.fire` then commits and returns quietly: no `automation_fired` line, no failed job, no `FIRE_RETRY`. The existing recovery re-defers the claimed occurrence: `lost_fires` after the 2-minute `FIRE_GRACE` on the next `reconcile_schedules` run, for as long as the occurrence is `claimed`; past `max_lateness` the registry's own step 3 settles it `skipped_obsolete` and calls `on_skipped` (a `FIRE_LATE` kind is never settled that way, so such a handler must not raise `FirePostponed` forever; none does). The outcome is for "not yet", not for errors: any other exception still propagates and rolls the whole transaction back as before.
   A handler never commits, never calls Telegram or the model; an exception other than `FirePostponed` propagates and rolls the whole transaction back, leaving the occurrence `claimed` for the existing `FIRE_RETRY` and `lost_fires` recovery.
4. **`ScheduleJobs.fire`** (named touch point in `jobs/tasks_automation.py`): one transaction around `plan_fire`; the two-step digest defer + `mark_fired` is removed (it moves into the digest handler). The log line `automation_fired` keeps its fields.
5. **Digest re-registration.** `DigestFireHandler(scheduler)` (in `jobs/tasks_automation.py`, beside `ScheduleJobs`) is registered with `kind="digest"`, `flag="digest.daily"`, `SKIP_WITH_NOTE`, no `on_skipped`, wherever `_wire_m05` builds `ScheduleJobs` today. It must keep today's guarantee that a crash never loses a digest (today: defer, then mark in a second transaction; a crash in between re-fires through `lost_fires`). So it **defers `digest.build` inside the fire transaction**: inside `session.begin_nested()`, `scheduler.defer(DIGEST_TASK, args=…, queueing_lock=digest_queueing_lock(account_id, epoch), connection=<raw driver connection of the session>)`; when `defer` returns `None` (the queueing lock already holds a job), raise a private exception inside the savepoint block and catch it outside, so only the savepoint rolls back (the P2 F0-interfaces §6.1 savepoint rule). The occurrence is then marked fired; the build job and the mark commit together or not at all.
6. **`_advance`, `reconcile_due`, `recover_claims`** gain `registry: FireRegistry` (`reconcile_due(session, now, *, digest_enabled, registry)`):
   - `_advance` (m05 `:503`): for a `FIRE_LATE` kind, claim the earliest due occurrence however late (`verdict` ignored; note `late` when past the grace); for the others, unchanged, plus `on_skipped` for each `skipped_obsolete` row actually recorded (`_record` returned `True`; at most `MAX_SKIPPED_ROWS` per automation per run, as today).
   - `recover_claims` (m05 `:665`): a `FIRE_LATE` kind is always returned for re-defer; for the others unchanged, plus `on_skipped` for a `skipped_obsolete` settle.
   - `skip_for_restore(session, now, registry: FireRegistry | None = None)`: when a registry is given, `on_skipped` for each `skipped_restore` row recorded. `privacy/post_restore.py` keeps calling it without a registry (J1's file; restore skips are reported by the restore notice's skipped-schedules line).
   - Notes `no_handler`, `disabled`, `inactive` and `expired` never call `on_skipped`.
7. `timing()` stays as it is; a `FIRE_LATE` kind's handler receives `Timing.OBSOLETE` or `EXPIRED` when late (its handler decides; M6-LIFE's `delegation_end` handler checks `ends_at` itself).

### 3.2 Rule models (`automation/rules.py`; P3-interfaces §8.2)

Add `ReminderRule`, `FollowUpRule`, `ScheduledSummaryRule`, `DelegationEndRule` exactly as §8.2 (defaults: reminder 720, follow-up 1440, scheduled summary 120, `delegation_end` 0; `ReminderRule.text` 1–400 chars or `None`). `FollowUpRule` has a `model_validator` refusing `expected.kind` outside `{contact_replied, keywords, media_received, semantic}`. `load_schedule_rule` dispatches with a module-level mapping `_RULES: Mapping[str, type[ScheduleRule]]` (`digest`, `reminder`, `follow_up`, `scheduled_summary`, `delegation_end`); anything else → `ScheduleRule` (unchanged). `MonitorRule` is untouched (its `action` widening is M7-MACT's).

### 3.3 Tool tuples and one `ToolServices` (`agent/tools/registry.py`, `agent/runtime.py`, `bootstrap.py`; P3-interfaces §9.5)

1. Create `agent/tools/{delegations,delegation_reads,reminders,tasks,task_reads,followups,automations,automation_reads}.py`, each with a module docstring naming its owner unit (§9.4) and `TOOLS: tuple[ToolDefinition, ...] = ()`.
2. `default_registry()` keeps the CP5 tools inline in their current order and appends `*delegations.TOOLS, *delegation_reads.TOOLS, *reminders.TOOLS, *tasks.TOOLS, *task_reads.TOOLS, *followups.TOOLS, *automations.TOOLS, *automation_reads.TOOLS` (function-local imports, as today). The registry's existing guards (run types `owner_request` only, the forbidden-name pattern, tiers, flags, `TAINT_EXEMPT_TOOLS`, `CARD_ONLY_TOOLS`) are unchanged: RG-17 and RG-11 keep holding.
3. `ToolServices` gains `autonomy_config: AutonomyConfig | None = None`, `scheduler: JobScheduler | None = None`, `status_block_loaders: tuple[StatusBlockLoader, ...] = ()` and `p3_ports: P3Ports | None = None` (the final merged ports, so a tool such as `end_delegation` can reach the catch-up requester; the last two are contract gaps, §9 notes 2 and 3). `enabled_flags` already exists (M05 ICR-2).
4. `bootstrap.py` gains `build_tool_services(settings, session_factory, channel, clock, *, summarizer, drafter, approval_cards, actions_config, notifications_enabled, autonomy_config, scheduler, status_block_loaders, p3_ports) -> ToolServices` with `enabled_flags=settings.feature_flags`. `running_process` builds it **once**, after the P3 wire functions, and passes the same instance to `build_owner_runtime(..., services=…)` and `build_confirmation_applier(..., services=…)`.
5. `OwnerRequestRuntime.__init__` gains `services: ToolServices | None = None`. When given, `run` uses it as the `ToolContext.services` unchanged (no copy, no override); when `None` it builds one as today from the existing keyword arguments (kept for `evals/agent_mode.py` and the tests). `ConfirmationApplier` already takes `services`; bootstrap now passes the shared one. The runtime's own `config.enabled_flags` and the services' `enabled_flags` are the same `settings.feature_flags`. `running_process` always passes `services`, so the process never builds a second instance (P3-interfaces §9.5); `test_bootstrap_builds_one_tool_services_for_the_runtime_and_the_applier` pins it.
6. Invariants 2 and 3b are unchanged: the applier still runs only calls the owner confirmed on a card, and the shared instance gives it no tool the runtime would not offer (the registry, not `ToolServices`, decides what is offered).

### 3.4 Wire functions and the fragment merge (`bootstrap.py`; P3-interfaces §15.3)

1. `P3WireContext` (frozen dataclass): `settings`, `session_factory`, `clock`, `scheduler`, `llm` (`LLMClient | None`), `roles`, `gateway`, `notification_delivery` (`NotificationDelivery`), `fire_registry`, `actions_config`, `autonomy_config`, `ports` (`P3Ports` assembled so far), and `executor_factory: Callable[..., ActionExecutor] | None` (the executor seam, §3.4 step 8; `None` in a process without the `worker` or `system` role), `approval_cards: ApprovalCardPresenter | None` (OD-11: `_wire_m04`'s `wiring.approval_cards`, passed through unchanged; `None` where it is not built; closes contract gap CG-8; M6-ESC's `drf` press presents a held reply's card through it, and M6-PLAN and M6-SCHED sync superseded cards), and `final_ports: Callable[[], P3Ports]` (contract gap, §9 note 3): a late-bound accessor that returns the fully merged ports once all three wire functions have run and raises `RuntimeError` when called earlier. Units capture the accessor during wiring and call it only at run time (for example M6-DELEG and M6-LIFE reach `catch_up_requester`, which `_wire_surfaces` provides after `_wire_autonomy`). (F0-R) `_PhaseWiring` gains `notification_delivery: NotificationDelivery | None = None`, set by `_wire_m05` where it builds the delivery service today (main `bootstrap.py:435`); `P3WireContext.notification_delivery` is that instance.
2. `P3Wiring` (frozen dataclass, every field defaulted empty): the eight job groups (`delegation`, `release`, `export`, `escalation`, `followups`, `memory`, `summaries`, `surfaces`), `reconcilers: tuple[Reconciler, ...]`, `listeners: tuple[IngestionListener, ...]`, `routes: ControlRoutes`, `status_blocks: tuple[StatusBlockLoader, ...]`, `ports: P3Ports`, `poll_observer: PollObserver | None`, `executor: ActionExecutor | None = None` (§3.4 step 8).
3. `_wire_autonomy(ctx) -> P3Wiring`, `_wire_automation(ctx) -> P3Wiring`, `_wire_surfaces(ctx) -> P3Wiring`: each returns `P3Wiring()` in F0-R, with a docstring naming the owning team and the rule "function-local imports only". Place them far apart in the file (autonomy after `_wire_m05`, automation after it with a separator comment, surfaces last), so concurrent team edits never touch the same hunk.
4. `running_process`, after `_wire_m04` and `_wire_m05`: build `AutonomyConfig` (F0-S, `actions/config.py`) and the `FireRegistry`; call the three functions in order autonomy → automation → surfaces, each with `ctx.ports` = the ports merged so far; merge each fragment into the process wiring:
   - job groups: a group set by two fragments raises `ValueError` (one owner per group);
   - routes: callback kinds, pending-input kinds, commands, help lines and undo-handler events are merged with a duplicate check that also covers the P2 routes (`DIGEST_CARD`, `NTF_CARD`, `MON_CARD`, …), the dispatcher's built-in kinds and commands, and, for undo events, the built-in `UNDOABLE_EVENTS` (raise `ValueError` naming the key); `panel_rows`, `settings_rows`, `status_blocks` are concatenated in call order;
   - listeners: appended after the P2 listeners (actions → triage → monitors), so follow-ups come last;
   - reconcilers: appended after the P2 reconcilers;
   - ports: a `P3Ports` field set by two fragments raises; otherwise `dataclasses.replace`;
   - `poll_observer`: at most one; passed to the poller (F0-S's `Poller(poll_observer=…)`).
   Duplicate task names and fire kinds raise inside procrastinate's registration and `register_fire_handler` respectively. After the third merge, bind the `final_ports` accessor to the merged `P3Ports` and build the single `ToolServices` (§3.3) with `p3_ports=` those ports.
5. **The triage store after the ports.** `TriageRepository` is built in `_wire_m05` (main `bootstrap.py:473`, inside `if llm is not None:`) before any P3 port exists, with `draft_requester=request_triage_draft` (the CP4 closure at main `:460`, which calls `actions.drafts.request_draft(reason=DraftReason.TRIAGE_NEEDS_REPLY, debounce=settings.draft_debounce_seconds)`). Move the store, `Triager` and `TriageJob` construction, together with that closure, into a helper called after the merge, and pass both `draft_requester=request_triage_draft` (unchanged) and `delegation_requester=ports.delegation_requester`. Keep the `triage.run` and `llm is not None` guards. Dropping the closure would silently stop the Assist draft trigger from triage, a behaviour change F0-R must not make. The triage listener and reconciler stay where they are, so the listener order is unchanged.
6. The module import block is frozen after F0-R (P3-T26). Every P3 import lives inside a wire function.
7. `JobHandlers(...)` receives the merged groups; `surfaces` is always a `SurfaceJobHandlers` (fields `None` until David's units wire them), so the periodic shells are registered whenever the worker runs (§3.6).
8. **Executor seam (R43, ICR-P3-1, pre-approved).** M6-TXA gives `ActionExecutor` an `autonomy: AutonomyConfig | None = None` keyword, and `_wire_m04` (P2 code, outside the P3 wire functions) is where the executor is built today (main `bootstrap.py:305`). F0-R makes the construction late-bindable without changing it: `_wire_m04` stores `wiring.executor_factory = functools.partial(ActionExecutor, session_factory=…, clock=…, config=…, sender=…, alerts=…, scheduler=…, presenter=…)` (exactly today's arguments; set only when the process has the `worker` or `system` role) and builds the default executor by calling `wiring.executor_factory()`, so with no P3 fragment the executor is identical to CP5's. `P3WireContext.executor_factory` is that partial. A fragment that needs a different executor (M6-TXA: `ctx.executor_factory(autonomy=ctx.autonomy_config)`) returns it as `P3Wiring.executor`; the merge allows at most one fragment to set it (a second raises `ValueError`) and, when set, replaces the M04 executor held by the process's `ActionsJobHandlers` (and any other holder of it that CP5 wiring has) with that instance. An `executor` set while `ctx.executor_factory` is `None` raises. Nothing else about executor construction changes, and F0-R adds no `autonomy` keyword to `ActionExecutor` (M6-TXA's file).

### 3.5 Cross-team ports (`core/p3_ports.py`; P3-interfaces §4.5)

Exactly §4.5: `PortResult`, `DelegationControls`, `AutomationControls`, `TaskControls`, `CatchUpRequester`, `DelegationRequester`, `P3Ports` (all fields `None`). `AsyncSession` only under `TYPE_CHECKING`; `core` imports no other assistant package (import-linter contract `core-depends-on-nothing`). The Pause button is not a `P3Ports` field and needs no wiring: G1 is ruled (R45). F0-S adds the pure `core/autonomy_texts.py::pause_button() -> Button` and `PAUSE_CALLBACK_DATA`, every renderer imports that builder, and M6-PAUSE owns the press handler. F0-R adds nothing for it.

### 3.6 Job groups and pre-created modules (`jobs/`; P3-interfaces §7.1–§7.3)

1. `JobHandlers` gains the eight optional fields of §7.2. `register_tasks` calls, after the three P2 calls, `register_delegation_tasks`, `register_release_tasks`, `register_export_tasks`, `register_escalation_tasks`, `register_followup_tasks`, `register_memory_tasks`, `register_summary_tasks`, `register_surface_tasks` with `handlers.<group>`; a `None` group registers nothing.
2. Each new module holds its group dataclass (frozen, every field `None` by default) and its `register_*` function. Handler field types (F0-R; §7.2 names only the groups):

   | Module | Group | Field → task (args) | Queue, locks, schedule, retry (§7.1) |
   |---|---|---|---|
   | `tasks_delegation.py` | `DelegationJobHandlers` | `delegated: Callable[[int], Awaitable[object]]` → `replies.delegated(conversation_id)` | `llm`; qlock set at defer (`delegated_queueing_lock`); **no lock**; no retry |
   | `tasks_release.py` | `ReleaseJobHandlers` | `release: Callable[[int], Awaitable[object]]` → `actions.release_scheduled(action_id)` | `outbound`; qlock at defer; **no lock**; `RELEASE_RETRY = RetryStrategy(max_attempts=2, exponential_wait=2, retry_exceptions={OperationalError})` (the `EXECUTE_RETRY` shape: database errors only, which can only happen before the release transaction commits) |
   | `tasks_export.py` | `ExportJobHandlers` | `export: Callable[[int, int], Awaitable[object]]` → `privacy.export(account_id, card_id)` | `llm`; lock `acct:{id}:control` and qlock set at defer; `RequeueOnShutdown()` |
   | `tasks_escalation.py` | `EscalationJobHandlers` | `delegation_health: PeriodicHandler` → periodic `system.delegation_health` | `system`; `* * * * *`; qlock = the name |
   | `tasks_followups.py` | `FollowUpJobHandlers` | `nudge: Callable[[int, int], Awaitable[object]]` → `followups.nudge(automation_id, occurrence_at)` | `llm`; qlock at defer; no retry |
   | `tasks_memory.py` | `MemoryJobHandlers` | `extract: Callable[[int], Awaitable[object]]` → `memory.extract(conversation_id)` | `llm`; qlock at defer; **no lock**; no retry |
   | `tasks_summaries.py` | `SummaryJobHandlers` | `summarize: Callable[[int, int], Awaitable[object]]` → `automation.summarize(automation_id, occurrence_at)` | `llm`; qlock at defer; no retry |
   | `tasks_surfaces.py` | `SurfaceJobHandlers` | `commitments_due`, `weekly_rating: PeriodicHandler` → periodic `system.surface_due_commitments`, `system.weekly_rating` | `system`; `*/5 * * * *` and `41 * * * *`; qlock = the name |

   `PeriodicHandler = Callable[[datetime], Awaitable[int]]` and `SurfaceJobHandlers` (§7.2) are defined in `tasks_surfaces.py` and re-exported from `jobs/tasks.py` (`from assistant.jobs.tasks_surfaces import PeriodicHandler, SurfaceJobHandlers`), so the contract's `jobs.tasks` names resolve; `tasks_escalation.py` imports `PeriodicHandler` from `tasks_surfaces.py`, and no `tasks_*.py` module imports `jobs/tasks.py` (no cycle). A periodic shell converts the procrastinate `timestamp` with `datetime.fromtimestamp(timestamp, UTC)`.
3. A deferred task whose handler field is `None` (its group is wired, the field is not) logs `task_not_wired` with the task name only and returns. A periodic shell whose handler is `None` returns `0` without a log line (it runs every 5 minutes or hourly whenever the worker runs; a line per run would be noise).
4. `RequeueOnShutdown` moves from `jobs/tasks.py` to a new `jobs/retry.py`; `jobs/tasks.py` re-imports it, so `DELETE_DATA_RETRY` and every existing import keep working (no cycle between `tasks.py` and `tasks_export.py`).
5. `jobs/reconcile_delegation.py` and `jobs/reconcile_memory.py`: module docstrings naming the owners (M6-PLAN + M6-LIFE; M7-MEX) and the §7.3 rules; no functions. `jobs/reconcile.py` needs no change (P3 reconcilers reach `JobHandlers.reconcilers` through the fragments).
6. The P3 task names come from F0-S's `core/jobs.py` constants; nothing in `jobs/` hard-codes them.

### 3.7 Triage hook (`inbox/triage_store.py`; P3-interfaces §4.5)

`TriageRepository.__init__` gains `delegation_requester: DelegationRequester | None = None` (`core/p3_ports.py`). In `apply`, right next to the existing Assist block (m05 `:434-442`), after the alerts:

```python
if self._delegation_requester is not None and await _contact_wrote(
    session, account_id, verdict.burst_message_ids
):
    # F0-R: called, result ignored; M6-ESC uses it (one draft path per burst).
    await self._delegation_requester.request(
        session,
        account_id=account_id,
        conversation_id=conversation_id,
        covers_through_message_id=verdict.covers_through_message_id,
        now=now,
    )
```

The Assist block itself is unchanged. With the requester unwired (the default until M6-PLAN), nothing changes.

### 3.8 Delivery classes and the `active_delegation` slot (`inbox/notification_policy.py`, `inbox/notifications.py`; P3-interfaces §11.2–§11.3)

1. `NotificationFacts` gains `active_delegation: bool = False`. `NotificationDelivery._facts` (main `inbox/notifications.py:408`) passes `False` explicitly (fact loading arrives with M6-ESC).
2. `decide_delivery` starts with the class from F0-S's `delivery_class(facts.kind)` (unknown kind ⇒ `STANDARD`):
   - `SYSTEM` (or `facts.alert_class == AlertClass.SYSTEM`) ⇒ `IMMEDIATE`, reason `system` (unchanged for every M05 system alert);
   - `OWNER_SCHEDULED` ⇒ `IMMEDIATE`, reason `owner_scheduled` (no mute, quiet hours or cap);
   - `HELD_IN_QUIET`: effective priority `LOW`/`NORMAL` ⇒ the `STANDARD` rules; `HIGH`/`URGENT` ⇒ inside quiet hours `DEFER` to their end, reason `quiet_hours`; outside them `IMMEDIATE`, reason `immediate`; never muted or capped;
   - `STANDARD` ⇒ the M05 rules, unchanged.
   Every M05 kind maps to `STANDARD` except `commitment_due` (`HELD_IN_QUIET`), which has no producer before COMMIT-DUE; F0-R therefore changes no observable behaviour. `effective_priority` is unchanged.
3. `NotificationDelivery` reads the row's `alert_class` for three things F0-R does not change: the `notifications.deliver` gate (`alert_class == STANDARD and not enabled` ⇒ wait), the account lock (`FOR NO KEY UPDATE` for standard, `FOR KEY SHARE` for system, P2-T42, compatible with a standard cap claim) and the cap count. So a producer of a `SYSTEM`-class kind (`delegation_paused_llm`, M6-ESC) must enqueue it with `alert_class=SYSTEM`; the class table alone does not lift the gate. F0-R writes this rule in the `decide_delivery` docstring and in its PR description (M6-ESC's spec already follows it).
4. (F0-R) `NotificationDelivery.register_renderer` raises `ValueError` naming the kind when that kind already has a renderer (the defaults in `inbox/alert_texts.py::RENDERERS` included), so two units can never silently replace each other's renderer. The only CP5 caller registers `MONITOR_MATCH` once, and `RENDERERS` does not hold it. `inbox/notifications.py` gets no other change besides step 5.
5. **Feedback buttons (R82).** F0-S's delivery table carries a `feedback_buttons: bool` column and a lookup `feedback_buttons(kind) -> bool` beside `delivery_class` (unknown kind ⇒ `True`). It is `False` for the owner-created kinds `reminder_due`, `follow_up_met`, `follow_up_missed`, `occurrence_skipped`, `memory_review`, `usefulness_rating`, `delegation_catch_up`, and `True` for every other kind, including every M05 kind and `commitment_due` (an assistant-inferred alert). `NotificationDelivery._card` (main `inbox/notifications.py:375`) builds the card buttons as `NTF_BUTTONS` today; when `feedback_buttons(kind)` is false it builds only the Open chat button and omits Useful and Not useful (the renderer's `extra_buttons` follow as before; a kind whose renderer returns no `conversation_id` still gets no card). The reason: a "not useful" tap on an alert the owner asked for (a reminder, a follow-up result) would feed M05's `(kind, contact_id)` mute pairs and silence it. Every M05 kind keeps all three buttons, so F0-R changes no observable behaviour.

### 3.9 Control-chat hooks (P3-interfaces §12.2, §12.6, §12.7)

1. **`telegram/control/routes.py`**: `PanelRowBuilder`, `SettingsRowBuilder` and the four §12.2 fields (`help_lines`, `panel_rows`, `settings_rows`, `undo_handlers`), plus `status_blocks: tuple[StatusBlockLoader, ...] = ()` (contract gap, §9 note 2). `UndoHandler` is imported from `memory/undo.py`.
2. **Help** (`handlers.py`): `help_text(lines: Mapping[str, str]) -> str` returns today's text with one line per registered command, in registration order, inserted after the `/privacy` line and before "Send me anything else"; `HELP_TEXT = help_text({})` stays exported and byte-identical. `handle_help(gateway, account, message, *, help_lines: Mapping[str, str] = MappingProxyType({}))`. Each line is application text written by the registering unit; `help_text` adds no escaping (the gateway sanitizer escapes once, P2 #133).
3. **Status blocks**: in `observability/status.py`, `StatusBlock` and `StatusBlockLoader` (§12.6); `StatusReport.blocks: tuple[StatusBlock, ...] = ()`; `build_status(..., block_loaders: Sequence[StatusBlockLoader] = ())` awaits each loader in order in the caller's session and keeps the non-`None` results; `as_dict()` adds `"blocks": {block.key: dict(block.data) for block in blocks}` only when at least one block exists (F0-R), so the CP5 `get_status` result is byte-identical while nothing is registered. `render_status` prints, after the M05 lines and before the coverage caveat, one blank line and then each block's lines (nothing at all when there are no blocks), each line plain text escaped once with `escape`. `get_status` passes `services.status_block_loaders`; the dispatcher's `/status` passes `routes.status_blocks`. A loader returns `None` when it has nothing to show; it never swallows errors (no broad `except`).
4. **Panel and settings rows** (`panels.py`): `Screen` gains `extra_rows: tuple[tuple[Button, ...], ...] = ()`; `markup()` appends them, one `InlineKeyboardButton(text=button.label, callback_data=button.callback_data)` per `Button`, after the screen's own rows (a screen with only extra rows still gets a keyboard). `panel_screen(..., row_builders: Sequence[PanelRowBuilder] = ())` and `settings_screen(..., row_builders: Sequence[SettingsRowBuilder] = ())` call each builder in order inside the transaction and append its rows; an empty result adds nothing. A bot chat keeps showing no buttons at all (builders are not called for it). The public entry points that build these screens (`handle_press`, `handle_panel_command`, `handle_settings_command`, and any other caller of `panel_screen`/`settings_screen` reached from the dispatcher) gain a keyword `routes: ControlRoutes = ControlRoutes()` and pass the builders down; `dispatcher.py` passes `self._routes`.
5. **Undo registry** (`memory/undo.py`): `UndoHandler` and `BUILTIN_UNDO` (§12.7). `BUILTIN_UNDO` holds `memory_saved`, `memory_forgotten`, `mode_changed` and `priority_changed` as handlers moved out of the `if/elif` chain with identical bodies. `preferences_changed` stays a dedicated branch before the registry lookup, because it returns `PARTIAL` with `restored`/`kept`, which an `UndoHandler` returning `bool` cannot carry (§9 note 4). `undo(..., extra=MappingProxyType({}))`: an event is undoable when it is in `UNDOABLE_EVENTS` or in `extra`; lookup order: preferences branch, `BUILTIN_UNDO`, `extra`; a handler's `False` ⇒ `SUPERSEDED`; the account-scoped audit-row lookup, the retention-change `NOT_FOUND` check (M03 D85), the `GONE` check for memory events and the `change_undone` audit row keep their places and bodies. `handlers.handle_undo_press(..., undo_handlers=…)` passes `routes.undo_handlers`.

### 3.10 Keyboard assertions per builder (tests)

Every exact assertion on the conversation panel's or `/settings`' complete keyboard (grep `keyboard_ops(`, `keyboard_labels(`, `keyboard_rows(` and `.inline_keyboard` in `tests/integration/`: at least `test_conversation_modes.py`, `test_settings_m05_panels.py` and `test_visibility.py`; CP4 rewrote the panel assertions as full row-by-row comparisons) becomes an assertion on the core builder's rows only, through a helper `core_rows(markup, n)` that takes the first `n` rows the core screen produces. Assertion values do not change. The CP5 core panel of a contact chat has three rows: mode (Observe / Ignore / Assist), Delete, priority; so `core_rows(markup, 3)`. A P3 builder's rows are asserted in the owning unit's own test file (P3-interfaces §15.1).

### 3.11 Behaviour-neutral by construction

- Every new parameter defaults to today's behaviour; every new registry starts empty; every port is `None`.
- No flag is read except the digest's (moved into the registry), and no new setting exists.
- Edge cases: a digest fire whose `digest.build` is already queued still marks the occurrence fired (savepoint); a handler exception leaves the occurrence `claimed`; a registry without the digest (tests that build `ScheduleJobs` directly) settles `no_handler` for the digest too, so test helpers that build `ScheduleJobs` must register the digest handler (update the fixtures of `tests/integration/test_automation_fire.py` and `test_schedules.py`, never their assertions).
- Fail closed: a duplicate registration of any kind, command, task, port, group, undo event or renderer stops the process at startup.
- Locks (ARCHITECTURE §9.4): F0-R adds none. The fire path keeps the occurrence row lock it takes today, and the digest defer runs in a savepoint inside that transaction; the triage hook runs under the account → conversation → triage-row locks `apply` already holds; delivery keeps its account-then-notification order. No lock is held across a model or Telegram call.

## 4. Files

**Owned (P3-interfaces §15.1; after CP1 most become "nobody" or pass to the named unit):**

| File | What F0-R does | After CP1 |
|---|---|---|
| `automation/schedules.py` | §3.1 | M7-AUTO (`reschedule`), M7-FUP (`_still_runs`), named touch points only |
| `automation/rules.py` | §3.2 | M7-MACT (`MonitorRule.action` Literal only) |
| `agent/tools/registry.py` | §3.3 | nobody |
| `agent/tools/{delegations,delegation_reads,reminders,tasks,task_reads,followups,automations,automation_reads}.py` (new) | `TOOLS = ()` | the tool's unit (§9.4) |
| `agent/runtime.py` | `services` parameter | nobody (ICR) |
| `bootstrap.py` | §3.3 step 4, §3.4 (including the `_wire_m04` executor construction moved behind `executor_factory`, step 8) | each team inside its own wire function; import block frozen |
| `core/p3_ports.py` (new) | §3.5 | nobody (ICR) |
| `jobs/tasks.py`, `jobs/reconcile.py`, `jobs/retry.py` (new) | §3.6 | nobody |
| `jobs/tasks_{delegation,release,export,escalation,followups,memory,summaries}.py` (new) | §3.6 | M6-PLAN; M6-SCHED; M7-EXP; M6-ESC; M7-NUDGE; M7-MEX; M7-SSUM |
| `jobs/tasks_surfaces.py` (new) | §3.6 | nobody (juniors write handler functions only) |
| `jobs/reconcile_{delegation,memory}.py` (new) | docstrings | M6-PLAN + M6-LIFE; M7-MEX |
| `inbox/triage_store.py` | §3.7 | M6-ESC (effects) |
| `inbox/notification_policy.py`, `inbox/notifications.py` | §3.8 | M6-ESC (rule; fact loading only) |
| `telegram/control/{routes,handlers,panels,dispatcher}.py` | §3.9 | nobody |
| `observability/status.py`, `agent/tools/status.py` | §3.9 step 3 | nobody (blocks live in `observability/status_p3.py`, STAT-ACT) |
| `memory/undo.py` | §3.9 step 5 | nobody |
| `tests/integration/test_conversation_modes.py`, `tests/integration/test_settings_m05_panels.py` (keyboard assertions only) | §3.10 | nobody; P3 builders test in their own files |

**Named touch points outside the table:** `jobs/tasks_automation.py` (`ScheduleJobs.fire`, `reconcile_schedules` and `lost_fires` take the registry; `DigestFireHandler` added); the fixtures (not the assertions) of `tests/integration/test_automation_fire.py`, `test_schedules.py`, `test_control_routes.py` and any other test that constructs `ScheduleJobs`, `TriageRepository`, `ControlDispatcher`, `OwnerRequestRuntime` or `build_status` positionally.

**Never edit (P3-interfaces §15.2):** `policy/*`; `actions/{executor,lifecycle,states,proposals,ingestion,policy_loader,autonomy}.py`; `telegram/gateway.py`, `telegram/ingress/*`; `conversations/service.py`; migrations, `privacy/*` (including `privacy/post_restore.py`: `skip_for_restore` keeps a default that needs no change there); `llm/consent.py`; `agent/{context,schemas}.py`, `agent/tools/executor.py`; `agent/prompts/*`; `config.py`; `core/*` other than the new `core/p3_ports.py`. F0-S's files (`core/jobs.py`, `core/flags.py`, `inbox/notification_types.py`, `actions/config.py`) are read, never edited: a missing name is an F0-S fix requested on the F0-S PR before CP1.

## 5. Tests (red first)

F0-R adds no product behaviour, so IMPLEMENTATION_PLAN names no test for it. The proof is the **whole existing suite green with no assertion changed** (keyboard assertions only re-scoped, §3.10), plus the tests below. RG-11 and RG-17 are kept, not extended.

### 5.0 Existing tests that prove behaviour neutrality (assertions unchanged; fixtures only where §3.11 says)

QE1 runs these first and confirms in the review that no assertion line changed (CP5 paths; the files exist on `main` or `int/m05` today):

| Area F0-R touches | Existing tests |
|---|---|
| Fire path, digest on the registry | `tests/integration/test_digest.py`, `tests/integration/test_automation_fire.py`, `tests/integration/test_schedules.py`, `tests/unit/test_schedule.py`, `tests/unit/test_schedule_wiring.py`, `tests/scenarios/test_m05_digest.py` |
| Delivery classes | `tests/unit/test_notification_policy.py`, `tests/integration/test_notifications.py`, `tests/scenarios/test_m05_notification_policy.py` |
| Triage hook | `tests/integration/test_triage.py` (incl. `test_rg21_triage_never_loads_another_accounts_tasks`), `tests/integration/test_triage_jobs.py`, `tests/scenarios/test_m05_triage.py` |
| Tool tuples, one `ToolServices` | `tests/unit/test_tool_registry.py` (RG-17: `test_rg17_contact_triggered_runs_get_no_tools`, `test_rg17_no_registered_tool_sends_edits_deletes_or_marks_read`, `test_rg17_a_send_like_tool_cannot_be_registered`), `tests/integration/test_agent_runtime.py`, `tests/integration/test_tool_confirmations.py`, RG-11: `tests/integration/test_conversation_tools.py::test_rg11_*` and `tests/integration/test_memory_tools.py::test_rg11_*` |
| Help, status, panels, settings | `tests/integration/test_control_routes.py`, `tests/integration/test_control_chat.py`, `tests/integration/test_visibility.py`, `tests/integration/test_conversation_modes.py` and `tests/integration/test_settings_m05_panels.py` (assertion values unchanged; scope re-cut per §3.10) |
| Undo registry | `tests/integration/test_memory_tools.py`, `tests/integration/test_preferences_m05.py` (incl. `PARTIAL`), `tests/integration/test_contact_priority.py`, `tests/integration/test_retention_settings.py` |
| Job groups, `RequeueOnShutdown` move | `tests/integration/test_jobs.py`, `tests/unit/test_bootstrap.py` |
| Logs | `tests/integration/test_log_hygiene.py::test_rg10_full_scenario_logs_are_content_free` (RG-10) |

### 5.1 `tests/unit/test_fire_registry.py`
- `test_registering_a_kind_twice_raises`
- `test_a_kind_outside_scheduled_kinds_raises`
- `test_fire_late_with_a_flag_raises`
- `test_fire_late_with_an_on_skipped_hook_raises`
- `test_load_schedule_rule_dispatches_each_p3_kind` (each kind → its model; `monitor` and unknown kinds → `ScheduleRule` as today)
- `test_rule_defaults_per_kind` (720, 1440, 120, 0)
- `test_follow_up_rule_refuses_decision_requested_and_urgent_conditions`
- `test_reminder_text_bounds` (empty and 401 characters refused; `None` allowed)

### 5.2 `tests/integration/test_fire_registry.py` (real Postgres, `FakeClock`, `FakeScheduler`)
- `test_an_unregistered_kind_still_settles_no_handler`
- `test_a_registered_handler_runs_and_the_occurrence_is_marked_fired_in_the_same_transaction`
- `test_a_handler_exception_rolls_back_and_leaves_the_occurrence_claimed`
- `test_a_flagged_kind_with_its_flag_off_settles_disabled`
- `test_past_max_lateness_settles_skipped_obsolete_and_calls_on_skipped_once` (two reconcile runs: one hook call)
- `test_on_skipped_is_never_called_for_no_handler_disabled_inactive_or_expired`
- `test_reconcile_due_calls_on_skipped_for_each_recorded_obsolete_occurrence`
- `test_a_fire_late_kind_is_claimed_and_fired_however_late`
- `test_recover_claims_never_settles_a_fire_late_occurrence`
- `test_skip_for_restore_without_a_registry_behaves_as_before`
- `test_a_handler_raising_fire_postponed_leaves_the_occurrence_claimed_without_a_failed_job` (R95: plan `POSTPONED`, no `mark_fired`, the handler's other writes rolled back to the savepoint, `ScheduleJobs.fire` returns normally and logs no `automation_fired`)
- `test_a_postponed_occurrence_is_redeferred_by_lost_fires_and_skipped_past_max_lateness` (R95: after `FIRE_GRACE` it is returned for re-defer; past `max_lateness` it settles `skipped_obsolete` and `on_skipped` is called once)
- `test_any_other_handler_exception_still_rolls_back_the_whole_transaction` (the existing behaviour is unchanged)
- `test_the_digest_fires_through_the_registry` (`FireAction.DIGEST`, `digest.build` deferred, occurrence `fired`)
- `test_a_digest_fire_defers_the_build_and_marks_fired_in_one_transaction` (inject a failure after the defer: neither the job row nor the `fired` mark remains)
- `test_a_digest_fire_whose_build_is_already_queued_still_marks_fired` (pre-existing queueing lock; transaction not aborted)

### 5.3 `tests/unit/test_tool_registry_p3.py`
- `test_the_eight_p3_tool_modules_exist_and_start_empty`
- `test_default_registry_lists_the_cp5_tools_unchanged_and_in_order`
- `test_rg17_the_registry_still_offers_no_tool_to_a_non_owner_run` (kept; the existing RG-17 tests stay as they are)
- `test_tool_services_p3_fields_default_to_none_or_empty`

### 5.4 `tests/integration/test_p3_wiring.py`
- `test_bootstrap_builds_one_tool_services_for_the_runtime_and_the_applier` (identity, `is`)
- `test_the_confirmation_applier_sees_the_process_feature_flags` (regression for P2-T32/T47: a held call whose tool reads `services.enabled_flags` sees the same set as the runtime)
- `test_empty_wire_functions_change_nothing` (a process built with all P3 flags off registers exactly the module-level `ALWAYS_WIRED` expected set: per registry (P3 callbacks, commands, pending-input kinds, renderers, fire kinds, listeners, status blocks, `P3Ports` fields and non-`None` `JobHandlers` P3 groups), a `frozenset` of the names a unit registers whatever the flags (P3-interfaces §10.2). F0-R leaves every set empty except `surfaces` in the job groups, so on F0-R's own base nothing else is registered. The set is **append-only** (WORKFLOW §6.5; P3-T157): a unit that registers an always-wired renderer, route, status block, port, fire kind or job group adds exactly its own names, in a separate last commit named in its PR, and removes or rewrites nothing; anything flag-gated that appears with every flag off still fails the test)
- `test_a_duplicate_callback_kind_across_fragments_raises_at_startup`
- `test_a_duplicate_command_pending_input_kind_or_undo_event_raises_at_startup`
- `test_a_p3_route_colliding_with_a_p2_route_raises_at_startup`
- `test_a_job_group_or_port_set_twice_raises_at_startup`
- `test_a_duplicate_renderer_kind_raises_at_startup` (including a P3 kind registered over a `RENDERERS` default)
- `test_an_undo_event_colliding_with_a_builtin_event_raises_at_startup`
- `test_listener_order_is_actions_triage_monitors_then_p3_fragments`
- `test_the_triage_store_receives_the_delegation_requester_port`
- `test_the_default_executor_is_built_by_the_factory_with_the_cp5_arguments` (R43: with no fragment, the executor equals the one `_wire_m04` built before; a process without the `worker` or `system` role has `executor_factory = None`)
- `test_a_fragment_executor_replaces_the_default_and_a_second_one_raises` (the `ActionsJobHandlers` executor is the fragment's instance; two fragments, or one with `executor_factory = None`, raise at startup)
- `test_final_ports_is_valid_only_after_all_three_wire_functions_ran` (raises before; returns the merged ports after)
- `test_tool_services_carry_the_final_ports`
- `test_the_bootstrap_module_import_block_is_frozen` (AST: the set of top-level imports equals the list F0-R leaves; a later unit adding a top-level import fails it)

### 5.5 `tests/integration/test_p3_job_groups.py`
- `test_every_p3_task_is_registered_when_its_group_is_wired` (names from `core/jobs.py`)
- `test_a_none_group_registers_nothing`
- `test_an_unwired_handler_logs_task_not_wired_and_returns` (a deferred task of a wired group whose field is `None`)
- `test_the_surface_shells_are_registered_and_return_zero_while_unwired` (no log line)
- `test_periodic_handler_and_surface_handlers_resolve_from_jobs_tasks`
- `test_no_p3_task_registered_for_schedule_at_defers_carries_a_lock` (`replies.delegated`, `actions.release_scheduled`, `memory.extract`)
- `test_release_retries_only_database_errors` (`RELEASE_RETRY` retries `OperationalError`, not `ValueError`)
- `test_requeue_on_shutdown_still_imports_from_jobs_tasks`

### 5.6 `tests/integration/test_triage_hooks.py`
- `test_an_unwired_delegation_requester_changes_nothing` (verdict, alerts, Assist trigger, audit identical to CP5)
- `test_a_wired_requester_is_called_with_the_burst_watermark_and_its_result_is_ignored` (requester returns `True`; the Assist trigger still runs, alerts unchanged)
- `test_the_requester_is_not_called_when_no_contact_wrote`

### 5.7 `tests/unit/test_notification_policy_classes.py`
- `test_every_m05_kind_is_decided_exactly_as_before` (table: every M05 kind × priority × in/out of quiet hours × cap reached; expected values computed by the CP5 rules, written out)
- `test_system_class_is_immediate`
- `test_owner_scheduled_is_immediate_through_quiet_hours_mute_and_the_cap`
- `test_held_in_quiet_high_defers_to_the_end_of_quiet_hours_and_is_never_capped`
- `test_held_in_quiet_low_and_normal_follow_the_standard_rules`
- `test_an_unknown_kind_is_standard`
- `test_active_delegation_defaults_to_false`

### 5.7a `tests/integration/test_notification_cards_p3.py` (R82)
- `test_a_kind_with_feedback_buttons_false_gets_open_chat_only` (a registered test renderer for `follow_up_missed` returning a `conversation_id`: the card has one button plus the renderer's extra buttons, no Useful or Not useful)
- `test_every_m05_kind_keeps_open_chat_useful_and_not_useful` (the table `feedback_buttons` is true for each M05 kind)
- `test_an_unknown_kind_keeps_the_feedback_buttons`
- `test_a_kind_without_feedback_buttons_creates_no_mute_pair_input` (no feedback press is possible, so `FeedbackMark` rows stay absent for it)

### 5.8 `tests/integration/test_control_hooks.py`
- `test_help_without_registered_lines_is_byte_identical`
- `test_help_lists_registered_lines_in_registration_order`
- `test_status_prints_blocks_after_the_m05_lines`
- `test_get_status_returns_blocks_under_their_keys`
- `test_status_without_blocks_is_byte_identical` (`render_status` text and `get_status` data equal the CP5 output when no loader is registered)
- `test_a_loader_returning_none_shows_nothing`
- `test_panel_row_builders_append_rows_after_the_panel_rows`
- `test_an_empty_builder_result_shows_no_row`
- `test_a_bot_chat_panel_still_shows_no_buttons`
- `test_settings_row_builders_append_rows`
- `test_a_registered_undo_handler_reverts_its_event`
- `test_a_handler_returning_false_is_superseded`
- `test_builtin_undo_events_behave_as_before` (memory saved/forgotten, mode, priority, preferences incl. `PARTIAL`)
- `test_an_event_neither_builtin_nor_registered_is_not_found`

### 5.9 `tests/unit/test_log_hygiene_f0r.py`
F0-E's template and `tests/log_hygiene.py` helper merge after F0-R, so this file uses the capture `tests/integration/test_log_hygiene.py` uses today, with a synthetic canary (never edit that RG-10 file).
- `test_task_not_wired_logs_only_the_task_name`
- `test_the_fire_path_logs_ids_and_codes_only` (including the `fire_postponed` line)

## 6. Acceptance criteria

1. CI green on the full suite; no existing test assertion changed except the keyboard re-scoping of §3.10. `_wire_autonomy` can hand `AutonomyConfig` to `ActionExecutor` through the executor seam without touching `_wire_m04` again (R43).
2. With all P3 flags off, a process built by `running_process` registers exactly the CP5 routes, renderers, listeners and fire kinds (`digest` only) plus the P3 tasks of non-`None` groups (`surfaces` shells only).
3. Every name in P3-interfaces §4.5, §7.2, §8.1, §8.2, §9.5, §12.2, §12.6, §12.7 and §15.3 exists with the contract's signature (`mypy --strict` clean); `lint-imports` clean (including `core-depends-on-nothing`). `FirePostponed` (R95) and the `feedback_buttons` lookup (R82) exist as §3.1 and §3.8 describe.
4. One `ToolServices` instance reaches both the owner runtime and the confirmation applier, with `enabled_flags == settings.feature_flags`.
5. A digest occurrence fires once through the registry, and a crash between the build defer and the `fired` mark leaves neither.
6. A duplicate registration of any key stops startup with an error naming the key.
7. ARCHITECTURE §5.1 and §12.4 describe the registries as built.

## 7. Docs updated in the same PR

- ARCHITECTURE §5.1: the new modules (`core/p3_ports.py`, the eight tool modules, the job modules, `jobs/retry.py`), the three wire functions and the rule "each team edits only its own wire function; the import block is frozen".
- ARCHITECTURE §12.4: the fire registry (`register_fire_handler`, `SkipPolicy`, `on_skipped`, `FIRE_LATE` for `delegation_end`), the per-kind lateness defaults (OD-17), the digest on the registry, and that `skip_for_restore` reports through the restore notice.
- PR description: every place this spec chose a detail the contract left open (marked "(F0-R)"), for the TL to log in decisions §D.

## 8. Owner decisions

- **OD-17** (lateness): the per-kind `max_lateness_minutes` defaults (reminder 12 h, follow-up 24 h, summary 2 h). Another answer changes only the defaults in §3.2 and their test.
- **OD-2** (one `int/p3`): the branch base. Two integration branches would make F0-R target both and duplicate the merge.
- No other owner decision changes this unit: it is behaviour-neutral.

## 9. Risks and notes

1. **Fire-path order.** P3-interfaces §8.1 lists "no registration → flag → deadline"; the as-built order is "deadline → no handler → disabled". This spec keeps the as-built order so the digest tests stay unchanged (reported as a contract wording gap). The only difference is the note on an occurrence that is both late and unregistered or disabled.
2. **Status-block carrier.** The contract names the loaders (§12.6) and registers them in `_wire_surfaces`, but gives no field that carries them to `/status` and `get_status`. This spec adds `ControlRoutes.status_blocks` and `ToolServices.status_block_loaders` (recorded in P3-interfaces §9.5 and §12.2).
3. **Late-bound ports.** The catch-up requester comes from `_wire_surfaces`, after `_wire_autonomy` has wired the units that need it (M6-DELEG, M6-LIFE), and tools need the final ports too. This spec adds `P3WireContext.final_ports` and `ToolServices.p3_ports` (recorded in P3-interfaces §4.5, §9.5 and §15.3). The Pause button needs no injection (R45: F0-S's pure `pause_button()`); it is not a `P3Ports` field.
4. **Undo `PARTIAL`.** `UndoHandler` returns `bool`, so the preferences undo (which reports restored and kept fields) stays a dedicated branch; `BUILTIN_UNDO` holds four events, not five (reported).
5. **Behaviour drift in the digest path** is the main risk: the digest defer moves into the fire transaction. The two digest tests in §5.2 and the unchanged M05 digest suite guard it; QE1 runs `test_digest.py`, `test_automation_fire.py` and `test_schedules.py` first.
6. **Merge conflicts with F0-S**: F0-S also touches `core/jobs.py`, `inbox/notification_types.py` and `bootstrap.py`-adjacent code. Rebase on `p3/f0-s` before marking ready; never edit F0-S's files.
7. **Time.** F0-R is on CP1's path. If it grows past ≈ 1.5 days, split it: F0-R1 (fire and rule registries, job groups, ports, wire functions, `ToolServices`) first, F0-R2 (control-chat hooks, undo registry, delivery classes, triage hook) second; tell the TL.
8. **Renderer duplicates.** P3-interfaces §15.3 lists the keys whose duplicates raise (callback kind, pending-input kind, command, task name, fire kind) but not renderer kinds, and `register_renderer` silently replaces today. §3.8 step 4 makes a duplicate raise (behaviour-neutral: no CP5 duplicate exists). P3-interfaces §15.3 lists "renderer kind" among the keys whose duplicates raise.
9. **`FirePostponed` and `feedback_buttons`** are ruled (R95, R82) and recorded in P3-interfaces §8.1 (the `POSTPONED` outcome) and §11.2 (the `feedback_buttons` column and lookup, owned by F0-S). F0-R implements the consuming side: the handler call inside a savepoint, the `POSTPONED` plan and quiet `ScheduleJobs.fire` (§3.1 step 7), and the card rule (§3.8 step 5). M7-FUP raises `FirePostponed` (its ingestion-lag guard); the renderers of the seven owner-created kinds rely on the column.
