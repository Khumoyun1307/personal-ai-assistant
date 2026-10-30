# M6-TXA — Tx A for autonomous sends

| | |
|---|---|
| **Unit** | M6-TXA ★ — delegation lock, every autonomy rule re-run at claim, disclosure re-check, late conversion, `disclosure_included` |
| **Team / slot** | Josh / J1 |
| **Size** | M |
| **Depends on** | M6-SCHED (`convert_cancelled`, released actions), M6-POL (rules, `autonomous_execution_target`), M6-PLAN (`autonomous_context`, the builders, the `delegation_auto` propose path), F0-R (the executor seam in `bootstrap.py`, R43) |
| **Branch → base** | `p3/m6-txa` → `int/p3` |
| **Peer reviewer** | E3 (Codex, dispatched by the TL) |
| **QA** | QJ1 or QJ2, before merge, with the mutation table (§5.7); QJ1's race reproductions run against this PR |
| **Tier** | 1A: Gemini read (David lead), TL deep review, part of CP-A |
| **Normative inputs** | ARCHITECTURE §9.3, §9.4 (execution protocol, lock order, ingestion barrier (c)), §13; P3-interfaces §2.1–§2.5, §3.1, §3.4–§3.6, §5.1, §6.1, §13, §15; P3-schema §3.1 (`ck_actions_disclosure_origin`, `ux_actions_delegation_base`, R-1); AGENTS §6 invariants 1, 6, 8, 10, 13; IMPLEMENTATION_PLAN §8 S06.2 #1 (claim half), #12, S06.3 #4, §10 (RG-05, RG-06, RG-14, RG-18; RG-19's existing claim test kept, §5.5); decisions P3-T19, P3-T21, P3-T47 (R10), P3-T51 (R14); README §3 row M6-TXA |

## 1. Goal

The last check before an autonomous send happens in Tx A, under the account → conversation → **delegation** → action locks: the late check, then every send precondition and all ten autonomy rules again, with the caps counted under the delegation row lock and the disclosure re-checked against prior sends (an uncertain send counts as not disclosed). A pass claims the action and fixes `disclosure_included` from the exact text claimed. A failure cancels it with a truthful reason and, for autonomy-only failures, turns it into a held reply, announced to the owner by an `autonomy_not_sent` alert whose Draft reply button shows the same text as an approval card (or, once, a re-proposal with the disclosure), with no model call (invariants 1, 6, 8, 10, 13; OD-11).

## 2. Scope

**In:**
1. `actions/executor.py`: the `delegation_auto` branch of Tx A (locks, late check, decision through `autonomous_context`, autonomous execution targets, after-commit conversions, `disclosure_included`, audit details); the `ActionExecutor(autonomy=...)` keyword; the `_wire_autonomy` line that builds the executor with `AutonomyConfig` through F0-R's seam (R43, §3.5).
2. `actions/policy_loader.py`, the execution half of `autonomous_context` (P3-interfaces §3.5): verify and, if needed, adjust its execution-phase branch; own its execution-phase tests.
3. RG-14 (new) and the RG-05/06/18 extensions for autonomous actions; the interleaving tests.

**Out:** `propose`, the planner and the fact builders (M6-PLAN); scheduling, release, bulk paths and `convert_cancelled` itself (M6-SCHED: this unit only calls it); the rules (M6-POL); Tx B and `mark_uncertain` (unchanged); the disclosure composer and `disclosure_required` (F0-S).

## 3. Design

### 3.1 Contracts consumed and provided

| Consumes | From |
|---|---|
| Event table: `approved → executing` = `action_claimed` (+ `delegation_id`, `disclosure_included` in details) | F0-S, P3-interfaces §2.2, §13 |
| `actions.disclosure_included`, `late_after`, `delegation_id`; `ck_actions_disclosure_origin`; `ux_actions_delegation_base` (excludes `disclosure_missing`) | F0-S, P3-schema §3.1 |
| `active_covering(..., for_update=True)` | F0-S, §6.1 |
| `policy_loader.autonomous_context`, `delegation_snapshot`, `rate_counters`, `autonomy_facts` | M6-PLAN, §3.5 |
| `decide`; `policy.engine.autonomous_execution_target`; `Conversion` | M6-POL, §3.6 |
| `disclosure.includes_disclosure` | F0-S, §5.1 |
| `release.convert_cancelled` | M6-SCHED |
| `AutonomyConfig` | F0-S, §10.1 |

| Provides | To |
|---|---|
| The only claim of a `delegation_auto` action, with a stored execution decision that holds a passing check for each of the ten `AUTONOMY_RULES`, the two limits used at the claim (`limits.per_conversation_hour`, `limits.per_delegation`, integers; R84) and `disclosure_included` | OPS-TV3 (reads only decision records and non-content columns: `disclosure_included`, `delegation_id`, `late_after`, the decision; never `delegations.spec`), QA-S6, STAT-ACT |
| Audit `action_claimed` with `delegation_id`, `disclosure_included`; `action_converted` (through `convert_cancelled`) | OPS-TV3, OPS-VIEWS |

### 3.2 Tx A for `delegation_auto` (`ActionExecutor._tx_a`)

Owner-approved and dictated actions keep the M04 path unchanged, line for line. The autonomous branch, in order:

1. **Keys, no lock:** `SELECT account_id, conversation_id, origin FROM actions WHERE id = :id` (origin never changes). `autonomous = origin == 'delegation_auto'`.
2. **Locks, §9.4 order:** account `FOR SHARE`; the conversation (of that account) `FOR SHARE`; **if `autonomous`:** `covering = await active_covering(session, account_id=…, conversation_id=…, now=now, for_update=True)` (locks the covering delegation row; ADR-20); then the action `FOR UPDATE`. Replace the comment "(delegation: M06)" with the real step.
3. Not found ⇒ `NOT_FOUND`; state not `approved` ⇒ `SETTLED` (unchanged).
4. **Autonomy configured:** `autonomous and self._autonomy is None` ⇒ `cancelled/sending_disabled` with the stored decision `_refused("autonomy_configured", "sending_disabled")` (fail closed; a process wired without `AutonomyConfig` never sends autonomously; §3.5).
5. `text IS NULL` ⇒ autonomous: `cancelled/data_deleted`; otherwise `blocked/data_deleted` (M04).
6. Conversation not owned ⇒ autonomous: `cancelled/policy_denied`; otherwise `blocked/policy_denied` (M04).
7. `retry_at` in the future ⇒ `NOT_YET` (unchanged; a rate-limited autonomous send comes back here and meets step 8).
8. **Late first (autonomous):** before the policy re-check (P3-interfaces §3.6; ARCHITECTURE §9.4 barrier (c)):
   - `late_after IS NULL` ⇒ `cancelled/policy_denied`, stored decision `_refused("on_time", "not_scheduled")`, **no** conversion. The schema allows both schedule columns NULL on an `approved` row (`ck_actions_schedule_pair`), but every legitimate autonomous action passed `schedule_autonomous`; a row that did not is never sent and never turned into a held reply.
   - `now >= late_after` ⇒ `cancelled/late`, stored decision = one failed check `CheckResult("on_time", False, "late")` (the `_refused` pattern), after commit `CARD` conversion (§3.3).
9. **Decision (autonomous):** `delegation = covering if covering is not None and covering.delegation_id == action.delegation_id else None`; `decision = decide(await autonomous_context(session, account_id=…, conversation_id=…, delegation=delegation, facts=<ActionFacts from the locked row>, text=action.text, draft_flags=action.draft_flags or {}, base_message_id=action.base_message_id, phase=Phase.EXECUTION, now=now, actions_config=self._config, autonomy_config=self._autonomy, process_run_id=self._config.process_run_id))`. A superseded, ended or not-yet-started delegation, or a NULL `delegation_id` (its row was deleted), leaves `delegation = None` and fails `delegation_active` (`cancelled/delegation_ended`, no card). The counters are read here, after the delegation lock, so a second claim of the same delegation sees the first one's committed claim (READ COMMITTED; ADR-20).
10. **`WAIT`** (only `inbox_drained` failed) ⇒ unchanged: stays `approved`, decision stored, re-deferred in 3 s. Step 8 catches a wait that runs past `late_after`.
11. **`DENY` (autonomous):** `target = autonomous_execution_target(decision)`. `None` (`already_claimed`) ⇒ `SETTLED`. Otherwise `_deny(session, action, now, decision, ActionState.CANCELLED, target[0], extra_details={"delegation_id": …})`; after commit, only when `_deny` applied the transition (not `SETTLED`) and `target[1]` is not `Conversion.NONE`, the conversion (§3.3). Never `blocked`, `outdated` or `expired` for this origin at Tx A (P3-interfaces §2.2). A send-precondition failure (takeover, pause, closed window, rights, consent, `sending_disabled`, …) never converts (OD-11).
12. **`ALLOW_AUTO`:** the M04 claim (`approved → executing`, new `attempt_id`, `attempt_started_at`, `sent_text_hash`, `policy_decision`, `retry_at = None`) plus, in `values`, `disclosure_included = autonomous and disclosure.includes_disclosure(action.text)`, and in `details`, for autonomous actions, `delegation_id` and `disclosure_included`. For an autonomous action the stored `policy_decision` is `decision_record(decision, Phase.EXECUTION)` plus one top-level key, `"limits": {"per_conversation_hour": <int>, "per_delegation": <int>}`: the two limits the claim used, read from the `DelegationSnapshot` that `decide` saw (`limit_per_conversation_hour`, `limit_per_delegation`; R84). The record then lets OPS-TV3 check the caps without reading `delegations.spec` (content-bearing). Commit before the gateway call (unchanged, invariant 6).

`_deny` gains an optional `extra_details: Mapping[str, Any] = {}` merged into the audit `details` (backward compatible). Steps 4–6 use `_deny` with `CANCELLED` and the same `extra_details` for autonomous actions.

The disclosure re-check is the `disclosure_present` rule inside step 9: `autonomy_facts` recomputes `disclosure_required` from the chat's prior autonomous sends **now** (only `succeeded` sends count as disclosed; `delivery_uncertain`, `failed` and `cancelled` count neither as disclosed nor as activity) and `disclosure_included` from the stored text. Required but absent ⇒ `disclosure_missing` ⇒ `cancelled/disclosure_missing` with `Conversion.REPROPOSE`.

The identity answer passes `in_scope`, `draft_safe` and `content_grounded` only when the stored text **is** one of the four fixed sentences (`autonomy_facts` compares the exact text; a flag never does it). A stored `identity_answer: true` flag on model text is ignored (R14; named test).

### 3.3 Conversions after commit

For a `CARD` or `REPROPOSE` target (and for step 8's late `CARD`), register `db.uow.after_commit` work that opens a new transaction (`transaction(factory)`) and calls `release.convert_cancelled(session, account_id=…, action_id=…, conversion=…, now=self._clock.now(), clock=self._clock, actions_config=self._config, autonomy_config=self._autonomy, scheduler=self._scheduler, presenter=self._presenter)`. It runs inside its own `try/except Exception` with a comment saying why (the cancel has committed; a failure here must not fail the job or let it retry); it logs `autonomy_conversion_failed` with the action id and the exception class name. M6-SCHED's `unconverted_autonomous` reconciler makes a lost conversion within minutes.

`convert_cancelled` (M6-SCHED) guarantees, and this unit's tests prove through Tx A:
- `CARD`: a held reply (`agent_proposal`), same body, no disclosure line, the same `delegation_id`, `base_message_id`, `shown_through_message_id`, `draft_flags` gaining `autonomy_codes`, `converted_from` and `presentation = "on_request"`, plus one `autonomy_not_sent` alert in the same transaction and no presented card (OD-11); no model call. For a late conversion (step 8) `convert_cancelled` also writes `draft_flags["was_due_at"]` (epoch seconds of the cancelled action's `scheduled_for`), and the card shows "was due at … (late)" through the line M6-PLAN added to `telegram/control/approvals.py` (R80); this unit reuses that line and edits no renderer.
- `REPROPOSE` (only `disclosure_missing`): one new `delegation_auto` proposal with `compose(body, …)` and `draft_flags["reproposed_from"]`, through the full proposal policy and a new send delay; not `ALLOW_AUTO` ⇒ a held reply. An action that already carries `reproposed_from` and fails `disclosure_missing` again converts to a `CARD`, never a second re-proposal (R10, P3-T47). When a newer autonomous action already holds the chat's slot, nothing more is made (M6-SCHED §3.4 step 5).
- **Identity answer:** no card and no re-proposal for any cancel reason (M6-SCHED §3.4 step 3: a card would carry the AI identity sentence as owner-approved text, PRD §11.4); since OD-10a no escalation exists from plan time, so `convert_cancelled` enqueues the identity question's escalation instead (M6-SCHED §3.4 step 3).

### 3.4 `disclosure_included`

- Set only here, in the claim update, to `origin == 'delegation_auto' and includes_disclosure(text)` (P3-interfaces §2.5). Every other origin writes `False` even if the owner's text starts with `AI_MARKER`; writing `True` there would violate `ck_actions_disclosure_origin` and fail the claim.
- Fixed at claim; never changed afterwards (Tx B, late results and `mark_uncertain` leave it).
- The identity answer starts with `AI_MARKER`, so it counts as included.

### 3.5 Wiring

`ActionExecutor.__init__` gains the keyword `autonomy: AutonomyConfig | None = None`. The config reaches the executor through `_wire_autonomy`, not through `_wire_m04` (TL ruling R43, ICR-P3-1, pre-approved): F0-R provides the seam (`P3WireContext.executor_factory`, a `functools.partial` of today's `ActionExecutor(...)` call, and `P3Wiring.executor`, F0-R spec §3.4 step 8). This unit adds one line inside `_wire_autonomy`: `executor = ctx.executor_factory(autonomy=ctx.autonomy_config)` returned as `P3Wiring.executor`, and F0-R's merge replaces the M04 executor held by the job handlers with it. `_wire_m04` is not edited. Until the line is wired, step 4 cancels every autonomous action at claim: fail closed, never a send.

**Test harness.** The M04 RG-19 claim tests seed a bare `delegation_auto` row through `tests/integration/execution_env.py` (`inserted`, `harness`). With this unit, such a row would hit step 4 (no `AutonomyConfig`) or step 8 (NULL `late_after`) before `autonomy_not_paused`, and the RG-19 assertions would see `sending_disabled` or `policy_denied` instead of `autonomy_paused`. Keep the RG tests meaning what they meant (§5.5): `harness` passes a send-on `AutonomyConfig` by default (`send_enabled=True`, OD-6 defaults, all four languages), and `inserted` gives a `delegation_auto` row `scheduled_for = now` and `late_after = now + 2 min` (a released row). Ownership (R41, P3-interfaces §15.1): `tests/integration/execution_env.py` belongs to F0-S (J1) until CP1 and to this unit (J1) from CP1 on; Josh sequences any other unit's edit.

### 3.6 Execution half of the loader

M6-PLAN wrote `autonomous_context` for both phases. This unit checks the execution branch against Tx A and fixes it in place if needed (Josh lead sequences the edit after M6-PLAN): `ActionFacts` from the locked row; the stored text and flags; prior sends and counters read after the delegation lock; `sending_enabled = actions.send and autonomy.send`; `ai_generated = True`; `process_run_id` passed so `inbox_drained` binds to this run. The tests in §5.3 own this half.

### 3.7 Edge cases (each has a test in §5)

A claim exactly at `late_after`; a rate-limited send that comes back after `late_after`; a delegation that ended, was superseded or is past `ends_at` between release and claim; `/pause` or a takeover between release and claim, before and after Tx A takes its locks; two claims of one delegation racing for the last cap slot; an earlier disclosed send that is `delivery_uncertain`, or that settles while Tx A runs; a 24 h gap crossed between proposal and claim; a second `disclosure_missing` on a re-proposal; consent revoked or `autonomy.send` switched off between release and claim; the text cleared by a deletion; a stored identity flag on model text; an executor wired without `AutonomyConfig`; an owner-approved card whose text starts with `AI_MARKER`; an `approved` autonomous row that never passed `schedule_autonomous` (NULL `late_after`); a cancelled identity answer.

### 3.8 Failure modes (fail closed)

| Situation | Outcome |
|---|---|
| No covering delegation, or not the action's | `cancelled/delegation_ended`, no card |
| `late_after` missing (never scheduled) | `cancelled/policy_denied`, no card |
| Identity answer cancelled | no card, no re-proposal |
| `AutonomyConfig` not wired | `cancelled/sending_disabled` |
| Any fact missing | its rule fails ⇒ cancelled with that rule's reason |
| Conversion fails after commit | logged; `unconverted_autonomous` recovers |
| Exception before the claim commits | rolled back; DB errors retried (unchanged `EXECUTE_RETRY`); nothing sent |
| Exception after the claim | unchanged: the row stays `executing` for `mark_uncertain`, never resent |

## 4. Files

**Owned (P3-interfaces §15.1):** `actions/executor.py`; `actions/policy_loader.py` (the execution branch of `autonomous_context`, separate from M6-PLAN's, M6-TAKE's and NUDGE-POL's functions; Josh lead sequences).

**Named touch points:** `bootstrap.py::_wire_autonomy` (one line returning `P3Wiring.executor`; ICR-P3-1, R43; append only); `tests/integration/execution_env.py` (owned by this unit from CP1, R41: `harness` default `autonomy=`, `inserted` schedule times for `delegation_auto`; §3.5); `tests/integration/test_kill_switch.py` (one assertion of the RG-19 claim test, §5.5; M6-PAUSE and M6-TAKE also edit this file, Josh lead sequences).

**Do not edit:** `actions/release.py` (M6-SCHED; import `convert_cancelled` only), `actions/proposals.py`, `actions/lifecycle.py`, `actions/ingestion.py`, `actions/autonomy.py`, `policy/*`, `actions/{states,ports,disclosure,config}.py`, `telegram/gateway.py`, migrations, the never-edit list in P3-interfaces §15.2.

## 5. Tests (red first)

Real PostgreSQL 18, `FakeClock`, `FakeTelegramSender`, `FakeDelegatedDrafter` for the pipeline tests, the F0-S factories (`seed_delegation`, `seed_autonomous_action(state="approved" | "scheduled")`), `tests/integration/execution_env.py` (`harness`, `caught_up`, `lock_waits`). All text synthetic.

### 5.1 Named tests (IMPLEMENTATION_PLAN §8 and the RG list)

| Source | Test (`tests/integration/test_autonomy_claim.py` unless named) |
|---|---|
| **RG-14** (new); S06.2 #1, claim half | `test_rg14_the_first_autonomous_reply_in_a_session_carries_the_disclosure` (through planner → release → Tx A; the sent text starts with `AI_MARKER`; `disclosure_included` true) |
| S06.2 #12 / RG-14 | `test_rg14_the_disclosure_is_re_added_after_24_hours_of_inactivity_within_the_same_delegation` (fake clock: a disclosed `succeeded` send at T; a reply at T + 24 h discloses again. Claim-time variant, so that Tx A and not the release catches it: proposed at T + 23 h 58 m without the line, released at T + 23 h 58 m 45 s (not yet required), the claim held past T + 24 h by a WAIT on the ingestion barrier and run at T + 24 h 0 m 5 s, inside `late_after` ⇒ `cancelled/disclosure_missing` ⇒ one re-proposal with the line ⇒ exactly one send, disclosed) |
| RG-14 | `test_rg14_an_uncertain_earlier_send_counts_as_not_disclosed` |
| RG-14 / R10 | `test_rg14_a_second_disclosure_failure_becomes_a_card_never_a_second_reproposal` |
| RG-14 / PRD §11.4 | `test_rg14_an_owner_approved_fallback_card_is_sent_without_the_disclosure` (OD-11: the held reply's card is shown through the Draft reply path, the `approval_card_requested` row plus `present`, then approved and executed: the sent text has no `AI_MARKER` and `disclosure_included` is false; the name is kept so the RG list does not change) |
| S06.3 #4 | `test_an_overdue_autonomous_send_after_a_40_minute_outage_is_cancelled_late_and_converted_at_claim` (an `approved` action past `late_after`: `cancelled/late`, a held reply with the same body and one `autonomy_not_sent` alert (`alert_kind = "late"`), no card presented and no model call (OD-11), `draft_flags["was_due_at"]` equal to the cancelled action's `scheduled_for` as epoch seconds so the card says when the reply was due, R80) |
| RG-05 ext | `test_rg05_an_autonomous_send_left_executing_is_uncertain_and_never_resent` (the `test_execution.py::test_rg05_…` pattern, kept in this unit's file) |
| RG-06 ext | `test_rg06_a_stale_autonomous_send_is_cancelled_and_never_sent` (a contact message inserted without the listener between release and claim ⇒ `cancelled/new_contact_message`, no card) |
| RG-18 ext | `test_rg18_no_telegram_call_when_an_autonomous_precondition_fails` (parametrized: window closed, rights missing, `is_enabled = false`, unknown conversation, consent missing, Ignore mode, `actions.send` off, `autonomy.send` off, bot chat, account paused) — each ends `cancelled` with the mapped reason, never `blocked`; zero sender calls; no card |

### 5.2 Claim behaviour (`tests/integration/test_autonomy_claim.py`)

- `test_a_claim_exactly_at_late_after_is_late`
- `test_a_rate_limited_autonomous_send_that_turns_late_is_cancelled_late`
- `test_an_unscheduled_autonomous_action_is_cancelled_without_a_card` (an `approved` `delegation_auto` row with NULL `scheduled_for`/`late_after`: `cancelled/policy_denied`, check `not_scheduled`, no new action, zero sender calls)
- `test_a_cancelled_identity_answer_at_claim_makes_no_card` (late, and `volume_cap`: no new action)
- `test_every_autonomy_rule_is_re_evaluated_at_claim` (parametrized over the ten `AUTONOMY_RULES`. Baseline: `actions.send` and `autonomy.send` on and every fact passing, so the first failed check in `RULES` order is the one the row breaks; each made to fail between release and claim through data or settings, e.g. the delegation ended ⇒ `delegation_ended`, the language removed from `AUTONOMY_LANGUAGES` ⇒ `policy_denied` + card, the shared item archived ⇒ `knowledge_not_shareable` + card, the cap reached ⇒ `volume_cap` + card)
- `test_a_claimed_autonomous_action_stores_a_passing_check_for_each_autonomy_rule` (the OPS-TV3 completeness rule)
- `test_the_claim_stores_the_two_limits_it_used_in_the_decision_record` (R84: a delegation with `limits` 3 and 12; the stored `policy_decision["limits"]` equals `{"per_conversation_hour": 3, "per_delegation": 12}`, both integers; a non-autonomous claim stores no `limits` key)
- `test_card_conversions_at_claim_make_no_model_call_and_carry_no_disclosure`
- `test_send_precondition_failures_at_claim_never_convert`
- `test_a_claim_for_a_superseded_or_ended_delegation_is_cancelled_delegation_ended`
- `test_consent_revoked_between_release_and_claim_cancels_with_consent_revoked`
- `test_autonomy_send_off_at_claim_cancels_every_pending_autonomous_send` (stop-the-line)
- `test_an_identity_flag_on_model_text_does_not_pass_at_claim`
- `test_the_identity_answer_passes_at_claim_and_counts_as_disclosed`
- `test_a_delegation_auto_with_cleared_text_is_cancelled_not_blocked`
- `test_an_executor_without_autonomy_config_cancels_autonomous_actions`
- `test_disclosure_included_is_set_only_for_delegation_auto_from_the_claimed_text`
- `test_an_owner_approved_text_starting_with_the_ai_marker_keeps_disclosure_included_false`
- `test_action_claimed_audit_carries_the_delegation_and_disclosure_flag`
- `test_the_claim_counts_uncertain_and_failed_attempts_against_the_caps`
- `test_owner_approved_and_dictated_actions_keep_the_m04_tx_a` (one explicit regression; the existing `test_execution.py` suite must stay green unchanged)

### 5.3 Execution half of the loader (`tests/integration/test_autonomy_facts_execution.py`)

- `test_execution_facts_are_rebuilt_from_the_stored_text_and_flags`
- `test_prior_sends_and_counters_are_read_after_the_delegation_lock`
- `test_the_execution_context_binds_the_ingestion_barrier_to_this_process_run`

### 5.4 Interleavings (`tests/integration/test_autonomy_claim_races.py`)

Explicit `asyncio.Event` barriers and `pg_stat_activity` lock waits (the `test_action_execution_races.py` `Race` pattern); no sleeps:
- `test_a_pause_committed_before_an_autonomous_tx_a_blocks_the_claim`
- `test_a_pause_waiting_on_an_autonomous_tx_a_never_finds_a_claim_made_after_it`
- `test_two_claims_against_one_delegation_cap_send_once_and_the_second_becomes_a_card` (the delegation at `limit − 1`; two released actions in two chats of it. Hold the first Tx A after its counters read and before its commit, then start the second and wait for either a lock wait or its completion: with the lock it waits on the delegation row and then counts the first claim; without it, it would read the same counters and claim too, so the test fails rather than hangs)
- `test_a_delegation_end_waiting_on_tx_a_finds_the_action_claimed_or_cancels_it_first` (F0-S `end_delegations`)
- `test_a_takeover_between_release_and_tx_a_blocks_the_autonomous_send`
- `test_an_uncertain_send_settling_during_tx_a_never_lets_a_required_disclosure_go_missing` (Tx B of the earlier send and Tx A of the new one, both orders)

The invariant in each: the competing change commits first and nothing is claimed, or the claim commits first and is recorded as claimed before it; never a claim after a committed pause, takeover or end, never two claims past a cap.

### 5.5 Existing tests to update (same PR; RG tests keep their meaning)

| Test | Change |
|---|---|
| `tests/integration/test_kill_switch.py::test_rg19_pause_blocks_an_autonomous_action_at_claim` (RG-19) | The paused half stays `(CANCELLED, AUTONOMY_PAUSED)` (needs the §3.5 harness change). The unpaused half's `assert … .state is ActionState.BLOCKED  # no_delegation` becomes `(CANCELLED, DELEGATION_ENDED)`: an autonomous action never ends `blocked` (P3-interfaces §2.2); `business_calls == 0` stays. Update the docstring's "in P2". Stricter, not weaker (TL ruling R44: acknowledged; recorded at CP-A): name the edit in the PR description (AGENTS §6) |
| `tests/integration/test_action_execution_races.py::test_pause_committed_before_tx_a_blocks_an_autonomous_action`, `::test_pause_waiting_on_tx_a_never_finds_a_claim_made_after_it` | Must pass unchanged (the second relies on the harness default) |
| `tests/integration/test_ingestion_barrier.py::test_restart_with_a_backlogged_takeover_and_pause_sends_nothing`; `tests/scenarios/test_m04_j9_kill_switch.py::test_pause_blocks_an_autonomous_action_at_claim` | Must pass unchanged (the pause cancels before Tx A) |
| `tests/integration/test_execution.py` (whole file) | Must pass unchanged for every other origin (`test_owner_approved_and_dictated_actions_keep_the_m04_tx_a` is the explicit regression) |

### 5.6 Other

- `tests/integration/test_log_hygiene_m6_txa.py::test_autonomous_claims_and_conversions_log_ids_only` (canary text in the action; RG-10).
- `tests/unit/test_wire_autonomy_executor.py::test_wire_autonomy_builds_the_executor_with_the_autonomy_config` (R43): with a fake `executor_factory`, `_wire_autonomy` returns `P3Wiring.executor` built by `factory(autonomy=ctx.autonomy_config)` and calls `_wire_m04` nowhere; with `executor_factory` unset in the context it raises (F0-R's rule).
- Strict-xfail scenarios in `tests/scenarios/m06/` this merge turns green (RG-14 and J5 parts): tell QJ1.

### 5.7 Mutation table (Tier-1A)

| # | Rule, lock or guard added | Mutation | Named test expected to fail |
|---|---|---|---|
| 1 | Delegation `FOR UPDATE` in Tx A | remove `for_update=True` | `test_two_claims_against_one_delegation_cap_send_once_and_the_second_becomes_a_card` |
| 2 | Late check before the decision | remove it | `test_an_overdue_autonomous_send_after_a_40_minute_outage_is_cancelled_late_and_converted_at_claim` |
| 3 | `now >= late_after` | `>` | `test_a_claim_exactly_at_late_after_is_late` |
| 4 | Covering delegation must be the action's | drop the id comparison | `test_a_claim_for_a_superseded_or_ended_delegation_is_cancelled_delegation_ended` |
| 5 | Autonomous decision through `autonomous_context` | use M04 `_decide` | `test_every_autonomy_rule_is_re_evaluated_at_claim` |
| 6 | Autonomous targets are `cancelled` | use M04 `execution_target` | `test_rg18_no_telegram_call_when_an_autonomous_precondition_fails` |
| 7 | Send preconditions never convert | convert every cancel | `test_send_precondition_failures_at_claim_never_convert` |
| 8 | `disclosure_included = includes_disclosure(text)` | constant `True`; constant `False` | `test_disclosure_included_is_set_only_for_delegation_auto_from_the_claimed_text`; `test_rg14_the_first_autonomous_reply_in_a_session_carries_the_disclosure` |
| 9 | `disclosure_included` only for `delegation_auto` | drop the origin check | `test_an_owner_approved_text_starting_with_the_ai_marker_keeps_disclosure_included_false` |
| 10 | Uncertain counts as not disclosed (through the facts) | count `delivery_uncertain` as disclosed | `test_rg14_an_uncertain_earlier_send_counts_as_not_disclosed` |
| 11 | At most one re-proposal (through `convert_cancelled`) | bypass the `reproposed_from` check | `test_rg14_a_second_disclosure_failure_becomes_a_card_never_a_second_reproposal` |
| 12 | `ai_generated=True` at claim | `False` | `test_consent_revoked_between_release_and_claim_cancels_with_consent_revoked` |
| 13 | `sending_enabled` needs `autonomy.send` | `actions.send` only | `test_autonomy_send_off_at_claim_cancels_every_pending_autonomous_send` |
| 14 | Identity pass only for the exact sentence | trust the flag | `test_an_identity_flag_on_model_text_does_not_pass_at_claim` |
| 15 | Counters include uncertain and failed attempts | count `succeeded` only | `test_the_claim_counts_uncertain_and_failed_attempts_against_the_caps` |
| 16 | Cleared text ⇒ `cancelled` for autonomous | `blocked` | `test_a_delegation_auto_with_cleared_text_is_cancelled_not_blocked` |
| 17 | No `AutonomyConfig` ⇒ cancel | proceed without it | `test_an_executor_without_autonomy_config_cancels_autonomous_actions` |
| 18 | NULL `late_after` ⇒ cancel with no card | treat it as late (card), or skip the check | `test_an_unscheduled_autonomous_action_is_cancelled_without_a_card` |
| 19 | Identity answers never convert (through `convert_cancelled`) | bypass the identity check | `test_a_cancelled_identity_answer_at_claim_makes_no_card` |
| 20 | The claim stores the limits it used | omit `limits` from the stored decision | `test_the_claim_stores_the_two_limits_it_used_in_the_decision_record` |

## 6. Acceptance criteria

1. Every test in §5 exists under its name and passes in CI; RG-14 added; RG-05, RG-06 and RG-18 extended for autonomous actions; the RG-19 claim test updated only as §5.5 states; every other existing M04 executor test passes unchanged.
2. No autonomous send is claimed without every autonomy rule passing at claim under the delegation lock, inside `late_after`, with the disclosure present when required.
3. `disclosure_included` is true exactly for claimed `delegation_auto` actions whose claimed text carries the application's disclosure (or the identity sentence); every other row keeps `false`. The stored execution decision of a claimed autonomous action carries `limits.per_conversation_hour` and `limits.per_delegation`.
4. An autonomy-only failure at claim gives one card or one re-proposal, never both, never a second re-proposal, never a model call; a send-precondition failure, a never-scheduled row and a cancelled identity answer give no card.
5. Every mutation in §5.7 is killed; QJ1's race reproductions pass on this branch.
6. `ruff`, format, `mypy` on `src/assistant/actions`, and `lint-imports` clean; logs content-free.

## 7. Docs (same PR)

- ARCHITECTURE §9.4: Tx A for autonomy as built (the delegation lock, the late check first, the autonomous execution targets, the after-commit conversions and their reconciler, `disclosure_included`, "uncertain counts as not disclosed"); the diagram's `approved → cancelled` labels for autonomous origin.
- ARCHITECTURE §13: the claim's lock order including the delegation row.
- ADR-17 (application-inserted disclosure) and ADR-20 (counters derived from `actions` under the delegation `FOR UPDATE`) as written by the TL in F0-C part 2; note any as-built difference in the PR description.
- PR description: the RG-19 test edit (`BLOCKED` to `CANCELLED/delegation_ended` in the unpaused half; stricter, not weaker; the TL records it at CP-A, R44) and the ICR-P3-1 wiring line (R43).

## 8. Owner decisions

- **OD-4** (disclosure wording, fixed marker): only the composed text changes; `includes_disclosure` tests the marker.
- **OD-6** (caps, lateness 2 min): settings defaults only.
- **OD-11** (answered 2026-10-29, changed from the default: late or an autonomy failure at claim ⇒ a held reply with the same text and an `autonomy_not_sent` alert; Draft reply shows that text as a normal card with no model call; send-precondition failure ⇒ nothing): §3.2 steps 8 and 11, through `convert_cancelled` (M6-SCHED); this unit's code does not change.
- **OD-32** (re-disclosure reading): `disclosure_required` (F0-S) and the RG-14 cases; Tx A's code does not change.
- **OD-10 × OD-11 (ruled, R40):** a cancelled identity answer never becomes a card (it carries the AI marker, and an owner-approved message never carries disclosure); it escalates instead (M6-SCHED §3.4 step 3 enqueues the escalation, OD-10a; M6-SCHED §8).

## 9. Risks and notes

- **Contract gaps:** CG-9 (`AutonomyConfig` reaching `ActionExecutor`: ruled R43, ICR-P3-1, through `_wire_autonomy` and F0-R's seam; contract §4/§15.3 record it), CG-3 (`autonomous_context` and the merged "execution half"), CG-5 (the conversion reconciler this unit relies on), CG-15 (`expire_due` may expire an autonomous `approved` action before Tx A; truthful, no send), CG-SP1 (no card for a cancelled identity answer; ruled R40, see M6-SCHED §8), CG-SP3 (`tests/integration/execution_env.py` ownership: ruled R41, F0-S to CP1 then this unit; this unit edits `harness` and `inserted`), CG-SP4 (P3-interfaces §3.6 does not say what Tx A does with an `approved` autonomous row whose `late_after` is NULL; this spec cancels it `policy_denied` with no card).
- **RG-19 tests:** the harness change in §3.5 is what keeps `test_rg19_pause_blocks_an_autonomous_action_at_claim` asserting `autonomy_paused`. Changing an RG test's expected reason to `sending_disabled` or `policy_denied` instead would weaken it and is a blocking review finding.
- **Lock order** is the whole point: account `FOR SHARE` → conversation `FOR SHARE` → delegation `FOR UPDATE` → action `FOR UPDATE`. Never read the counters before the delegation lock. Never take the conversation lock after the delegation lock anywhere in this unit.
- **Conversions after commit** keep Tx A short and avoid upgrading the conversation lock under the delegation lock; the price is a short window where a crash loses the card, closed by `unconverted_autonomous`.
- **Hot file:** `actions/policy_loader.py` is edited by four Josh units in a fixed order (M6-PLAN → M6-TXA → M6-TAKE → NUDGE-POL); rebase on the earlier ones before you push.
