# M6-POL — Autonomy rules and outcome mapping

| | |
|---|---|
| **Unit** | M6-POL: the eight new autonomy rules and the real `delegation_active` (pure), the proposal outcomes DENY / REQUIRE_APPROVAL / ALLOW_AUTO for `delegation_auto`, `autonomous_execution_target`, and the explanations of every autonomy code |
| **Team / slot** | Josh / J3 |
| **Size** | M |
| **Depends on** | F0-S: `policy/context.py` P3 fields, `policy/codes.py`, the five new `StateReason` members, `core/autonomy_texts.GateLanguage`. Starts on D0 from the contract; stacks on `p3/f0-s` until CP1 |
| **Branch → base** | `p3/m6-pol` → `int/p3` (stacked on `p3/f0-s` until CP1, then retargeted) |
| **Peer reviewer** | E2 (Codex; E2 builds the delegated draft whose flags these rules read) |
| **QA** | QJ1 or QJ2, before merge, applies the mutation table |
| **Tier** | **1A ★** — Gemini read (David lead), TL deep review; part of CP-A |

## 1. Goal

Make the policy engine decide autonomy: for origin `delegation_auto`, `decide` returns **ALLOW_AUTO only when every send precondition and all ten autonomy rules pass**, **REQUIRE_APPROVAL** when only autonomy rules fail (the reply becomes a held reply: stored for approval and announced by an `autonomy_not_sent` alert with a Draft reply button, OD-11), and **DENY** when a send precondition fails. Model flags can only downgrade. Give Tx A (M6-TXA) and the release job (M6-SCHED) one pure function that maps a failed execution decision to `cancelled/<reason>` plus the card or re-proposal conversion. `decide` stays pure and total; nothing changes for owner-dictated and agent-proposal actions.

## 2. Scope

**In:**
- `policy/rules.py`: real `delegation_active`; the eight rules `in_scope`, `trigger_fresh`, `draft_safe`, `content_grounded`, `knowledge_scope`, `disclosure_present`, `volume_ok`, `language_gate`, appended to `RULES` in that order; `REASON_CODES` built from `policy/codes.py::AUTONOMY_CODES`.
- `policy/engine.py`: the proposal outcome for `delegation_auto`; the completeness guard; `autonomous_execution_target`.
- `policy/explanations.py`: the autonomy codes' sentences merged into `EXPLANATIONS`.
- The tests in §5, including the updates to existing policy tests the new behaviour requires.

**Out:**
- Building the facts: M6-PLAN (`actions/policy_loader.py`: `delegation_snapshot`, `rate_counters`, `autonomy_facts`), M6-TXA (execution facts under the delegation lock), M6-TAKE (whether a takeover counts), NUDGE-POL (the J7 reference).
- Grounding and language keys (GRND), the disclosure decision (`actions/disclosure.py`, F0-S).
- Applying the targets: moving actions, converting to cards, re-proposing (M6-TXA, M6-SCHED).
- `policy/context.py` and `policy/codes.py`: F0-S's, frozen (an ICR changes them).
- M04's `execution_target`, the send-precondition rules and their explanations: unchanged.

## 3. Design

### 3.1 Contract consumed (P3-interfaces §3.1–§3.4, §3.6; F0-S)

`PolicyContext.delegation: DelegationSnapshot | None`, `.counters: RateCounters | None`, `.autonomy: AutonomyFacts | None` (all default `None`); `policy/codes.py`: `Behaviour`, `EscalationCategory`, `RuleClass`, `Conversion`, `ReasonCode`, `AUTONOMY_RULES`, `AUTONOMY_CODES`, `rule_class`; `StateReason.{LATE, DELEGATION_ENDED, TRIGGER_STALE, VOLUME_CAP, DISCLOSURE_MISSING}`; `GateLanguage`. M6-POL provides (P3-interfaces §3.4, §3.6): the rules, the outcome derivation and `autonomous_execution_target(decision) -> tuple[StateReason, Conversion] | None`.

### 3.2 Applicability

Each of the ten autonomy rules returns `None` (not applicable) unless `ctx.action.origin is ActionOrigin.DELEGATION_AUTO`. For `delegation_auto` each returns a `CheckResult` **in both phases, whatever facts are missing**: a missing snapshot, counters or facts fail the rule with its first code (P3-interfaces §3.1 "missing facts fail closed"). So a `delegation_auto` decision always lists all ten autonomy checks, and owner and agent-proposal decisions list none of the eight new ones.

### 3.3 Rules, in `RULES` order after `delegation_active`

Notation: `d = ctx.delegation`, `c = ctx.counters`, `a = ctx.autonomy`. "First" = the code of the first failing condition, top to bottom. Codes and their order are exactly `AUTONOMY_CODES` (P3-interfaces §3.2).

| Rule | Fails with (first failing, top to bottom) | Identity answer (`a.identity_answer`) |
|---|---|---|
| `autonomy_not_paused` (M04, unchanged) | `autonomy_paused` when the account kill switch is on or `ctx.conversation.autonomy_paused_reason` is set | applies |
| `delegation_active` (rewritten) | `no_delegation` if `d is None` · `delegation_ended` if `d.status != "active"` or (`d.ends_at is not None` and `now >= d.ends_at`) · `delegation_not_started` if `now < d.starts_at` · `not_covered` if not `d.covers_conversation`, or `d.account_id != ctx.account.account_id` (TL decision, spec: a snapshot of another account is a loader bug; fail closed). It reads the times itself and never relies on the `delegation_end` automation having fired | applies |
| `in_scope` | `out_of_scope` if `d is None` or `a is None`, or `d.topic_scoped and not a.in_scope` · `behaviour_not_allowed` if `a.behaviour` is not the value of a member of `d.allowed_behaviours` (so `"none"` and any unknown string fail) | **passes** |
| `trigger_fresh` | `trigger_stale` if `a is None`, `a.trigger_message_sent_at is None`, or `now - a.trigger_message_sent_at > a.max_trigger_age` (exactly the maximum age is still fresh) | applies |
| `draft_safe` | `low_confidence` if `a is None` or `a.confidence != "high"` · `unpermitted_information` if not `a.uses_only_permitted_information` · `sensitive_topic` if `a.sensitive_topic` · `makes_commitment` if `a.makes_commitment` · `should_escalate` if `a.should_escalate` or `a.escalation_category not in {"fyi", "none"}` (so `urgent_decision`, `needs_owner`, `suspicious` and any unknown value fail) | **passes** |
| `content_grounded` | `ungrounded_value` if `a is None` or `a.ungrounded_values != 0` (a negative count is a loader bug and fails closed) | **passes** |
| `knowledge_scope` | `knowledge_not_shareable` if `a is None` or `a.knowledge_ids_used` is not a subset of `a.knowledge_ids_allowed` (an id that was never shown is not allowed) | applies |
| `disclosure_present` | `disclosure_missing` if `a is None`, or `a.disclosure_required and not a.disclosure_included` | applies |
| `volume_ok` | `volume_conversation` if `c is None` or `d is None`, or `c.conversation_last_hour >= d.limit_per_conversation_hour` · `volume_delegation` if `c.delegation_total >= d.limit_per_delegation` · `loop_suspected` if `c.rapid_replies >= c.loop_reply_threshold` or `c.repeated_contact_message` | applies |
| `language_gate` | `language_unknown` if `a is None` or any of `a.contact_language`, `a.draft_language`, `a.declared_language` is `None` · `language_mismatch` if the three keys are not all equal · `language_not_enabled` if the key is not in `a.enabled_languages` (an empty set enables nothing) | applies |

Rules:
- **Model flags only downgrade** (P3-interfaces §3.4). Model flags: `confidence`, `uses_only_permitted_information`, `sensitive_topic`, `makes_commitment`, `should_escalate`, `in_scope`, `behaviour`, `escalation_category`, `identity_question`. Application facts: everything else in `AutonomyFacts`, the snapshot and the counters. A rule computed from application facts (`autonomy_not_paused`, `delegation_active`, `trigger_fresh`, `content_grounded`, `knowledge_scope`, `disclosure_present`, `volume_ok`, `language_gate`) never reads a model flag, and a missing or invalid flag reads as its failing value. `draft_safe` is never the sole basis for ALLOW_AUTO (ARCHITECTURE §9.3).
- **Identity answers** (OD-10, P3-T51; R14): when `a.identity_answer` is true (the final text is the app's fixed identity sentence), exactly `in_scope`, `draft_safe` and `content_grounded` pass, because no model-written text is sent. No other flag or fact lets any rule pass. `identity_question` alone (without `identity_answer`) changes nothing. Evaluation order in those three rules: `a is None` ⇒ fail with the rule's first code; then `a.identity_answer is True` ⇒ pass; then the rule's own conditions (so `in_scope` passes an identity answer even when `d is None`; `delegation_active` still fails that case).
- **Exact values only** (fail closed on a wrong type from jsonb): a boolean flag or fact passes only on its exact passing value (`uses_only_permitted_information is True`, `in_scope is True`, `identity_answer is True`; `sensitive_topic is False`, `makes_commitment is False`, `should_escalate is False`, `repeated_contact_message is False`), and a string only on the exact passing string. So the string `"false"` read back from `draft_flags` never passes `uses_only_permitted_information`. A negative counter (`conversation_last_hour`, `delegation_total`, `rapid_replies`) or a negative `ungrounded_values` is a loader bug and fails with that condition's code.
- **Bot chats** fail M04's send precondition `conversation_owned` (`bot_chat`), so they are DENY at proposal and never auto-replied. The `volume_ok` loop guard covers contact-side automation that arrives as ordinary contact messages (TF §13 row, conflict 30; OD-6).
- Rules read only their argument: no clock, no settings, no globals.

`REASON_CODES` becomes: the fourteen M04 send-precondition entries unchanged, then for each of the ten `AUTONOMY_RULES` the tuple of `AUTONOMY_CODES` codes whose `rule` is that name, in table order. `list(REASON_CODES)` still equals the `RULES` names in order; every code still belongs to exactly one rule.

### 3.4 `decide` (engine)

```python
checks = tuple(check for rule in RULES if (check := rule(ctx)) is not None)
failed = [check for check in checks if not check.passed]
autonomous = ctx.action.origin is ActionOrigin.DELEGATION_AUTO
if ctx.phase is Phase.PROPOSAL:
    if not autonomous:
        outcome = DENY if failed else REQUIRE_APPROVAL  # M04, unchanged: never ALLOW_AUTO
    elif any(rule_class(check.name) is RuleClass.SEND_PRECONDITION for check in failed):
        outcome = DENY
    elif failed or not _autonomy_complete(checks):
        outcome = REQUIRE_APPROVAL
    else:
        outcome = ALLOW_AUTO
else:  # execution phase, M04 semantics
    if not failed:
        outcome = ALLOW_AUTO if not autonomous or _autonomy_complete(checks) else DENY
    elif all(check.name == "inbox_drained" for check in failed):
        outcome = WAIT
    else:
        outcome = DENY
```

`_autonomy_complete(checks)` is true only when `checks` holds a **passing** `CheckResult` for each of the ten `AUTONOMY_RULES`. It is a defensive guard: with §3.2 it always holds, but if a future edit drops a rule from `RULES`, an autonomous action becomes a held reply at proposal and is refused at execution instead of being sent (P3-interfaces §3.4 "Completeness"; OPS-TV3 fails closed on a stored decision without the ten checks).

### 3.5 `autonomous_execution_target(decision)`

```python
def autonomous_execution_target(decision: Decision) -> tuple[StateReason, Conversion] | None:
```

1. `None` when `decision.outcome` is ALLOW_AUTO or WAIT, or any failed check has reason `already_claimed` (leave the action as it is; M04 semantics).
2. Otherwise take the **first failed check in `decision.checks` order (= `RULES` order), skipping `inbox_drained` failures** (an undrained inbox only ever causes WAIT, so beside another failure it says nothing about why the action must stop; TL ruling R35, and P3-interfaces §3.6 states it).
3. Its reason is an autonomy code ⇒ `(AUTONOMY_CODES[reason].execution_reason, AUTONOMY_CODES[reason].conversion)`.
4. Its reason is a send-precondition code ⇒ `(reason_of_m04_target, Conversion.NONE)`, where the M04 reason is `takeover` → `TAKEOVER`, `new_contact_message` → `NEW_CONTACT_MESSAGE`, `source_deleted` → `SOURCE_DELETED`, `window_closed` → `WINDOW_CLOSED`, `max_age` → `MAX_AGE`, `consent_missing` → `CONSENT_REVOKED`, `sending_disabled` → `SENDING_DISABLED`, `too_long` and `empty_text` → `TOO_LONG`, anything else → `POLICY_DENIED`. Implement it by reading the reason of the `_EXECUTION_TARGETS` row that contains the code (the state is dropped: this origin always ends `cancelled`), so the two tables cannot drift. Send-precondition failures never convert: after a takeover, a pause or a closed window the owner gets no card (OD-11).
   The choice is by `RULES` order (P3-interfaces §3.6), not by M04's priority table: `window_closed` together with `takeover` gives `(WINDOW_CLOSED, NONE)` here, while M04's `execution_target` gives `outdated/takeover` for other origins. Only the reason label differs; neither converts (R35: the send-precondition reason follows `RULES` order, labels only; nothing converts differently).
5. A DENY with no usable failed check (impossible by construction) ⇒ `(POLICY_DENIED, Conversion.NONE)` (fail closed).

Because send preconditions come before the autonomy rules in `RULES`, a takeover together with a volume cap gives `(TAKEOVER, NONE)`, never a card. The callers (M6-TXA, M6-SCHED) move the action to `cancelled` with the returned reason; `(APPROVED, CANCELLED)` and `(SCHEDULED, CANCELLED)` are both in `TRANSITIONS`. For `delegation_auto` at execution, the loader sets `sending_enabled = actions.send and autonomy.send` (P3-interfaces §3.1), so `autonomy.send` off gives `(SENDING_DISABLED, NONE)`. The "past `late_after`" check is not policy: Tx A and the release job compare `now` with `late_after` before calling `decide` (P3-interfaces §3.6). M04's `execution_target` stays exactly as it is and remains what Tx A uses for every other origin.

### 3.6 Explanations

`EXPLANATIONS` = the M04 sentences, plus `{code: AUTONOMY_CODES[code].explanation}` for every autonomy code. F0-S copies M04's sentences for `autonomy_paused` and `no_delegation` into `AUTONOMY_CODES` (each is two sentences, as M04 allows), so no M04 sentence changes. Both M04 texts stay (TL ruling R34): the contract rows for `autonomy_paused` and `no_delegation` read "M04 text". `explain` and its fallback are unchanged. Every new sentence (the 20 new codes) is one plain sentence with no names, message text or placeholders (FR-ACT-03).

### 3.7 Edge cases (each has a test in §5)

At `ends_at` exactly ⇒ `delegation_ended`; at `starts_at` exactly ⇒ active; a standing delegation (`ends_at None`) never ends by time; status `cancelled` ⇒ `delegation_ended`; a trigger exactly `max_trigger_age` old ⇒ fresh; counters equal to a limit ⇒ fail; a limit of 0 ⇒ always fails; `enabled_languages` empty ⇒ `language_not_enabled`; `behaviour = "none"` or `"chat"` ⇒ `behaviour_not_allowed`; `confidence = "HIGH"` ⇒ `low_confidence`; `escalation_category = "fyi"` passes, an unknown value fails; `knowledge_ids_used` empty ⇒ passes; used non-empty with allowed empty ⇒ fails; a foreign-account snapshot ⇒ `not_covered`; several failures ⇒ every failed check is reported (M04 behaviour) and the outcome follows the class rule.

### 3.8 What does not change

The fourteen send preconditions and their codes and sentences; outcomes for `owner_dictated` and `agent_proposal` in both phases; WAIT semantics; `execution_target`; `explain`. With every P3 flag off nothing in production builds a `delegation_auto` context with facts, so production behaviour is unchanged until M6-PLAN and M6-TXA use these functions:
- `propose()` still refuses origin `delegation_auto` (`autonomous_not_available`) **before** it calls `decide` (kept by F0-S until M6-PLAN), so the new REQUIRE_APPROVAL outcome for a `delegation_auto` proposal can never insert an autonomous action that waits for approval. M6-PLAN maps it to a held reply (an `agent_proposal` row shown through its alert, OD-11).
- Tx A keeps applying M04's `execution_target` to every origin until M6-TXA switches `delegation_auto` to `autonomous_execution_target`. A `delegation_auto` action without P3 facts still gets DENY at execution, so `tests/integration/test_kill_switch.py`, `test_action_execution_races.py`, `test_ingestion_barrier.py` and `tests/scenarios/test_m04_j9_kill_switch.py` (`blocked/policy_denied`, or `cancelled/autonomy_paused` when paused, including RG-19) pass unchanged.

## 4. Files

| Owned (M6-POL) | What |
|---|---|
| `src/assistant/policy/rules.py` | `delegation_active` rewritten; eight new rule functions; `RULES` and `REASON_CODES` extended |
| `src/assistant/policy/engine.py` | `decide` outcome derivation; `_autonomy_complete`; `autonomous_execution_target` |
| `src/assistant/policy/explanations.py` | autonomy sentences merged |
| `tests/unit/test_policy_autonomy.py` (new) | the M6-POL named tests (§5.1) |
| `tests/unit/test_policy_rules.py`, `tests/unit/test_policy_engine.py` | the updates in §5.2 |

No other files. `policy/context.py` and `policy/codes.py` are frozen (F0-S; ICR). **Never edit without an ICR** (P3-interfaces §15.2): every `actions/` file, `telegram/*`, `conversations/service.py`. `policy` stays free of `sqlalchemy` and `assistant.db` (`policy-is-pure` contract). No log lines are added, so no hygiene file is needed (say so in the PR).

## 5. Tests (red first)

Build contexts with `tests/unit/test_policy_rules.py::ctx()` plus three fixtures in the new file: `passing_delegation()` (an active, covering snapshot), `passing_counters()`, `passing_facts()` (every model flag at its best value, every application fact passing, all three language keys `GateLanguage.EN`, `enabled_languages={EN}`), so each case overrides one fact and names exactly one rule.

### 5.1 `tests/unit/test_policy_autonomy.py`

Named in the plan or the RG matrix:
- `test_draft_safe_is_never_the_sole_basis_for_allow_auto` (named; ARCHITECTURE §9.3): with every model flag at its best value, breaking each application rule's fact on its own never gives ALLOW_AUTO (REQUIRE_APPROVAL for autonomy rules, DENY for a send precondition).
- `test_cap_reached_gives_a_card_never_deny` (named): `volume_conversation`, `volume_delegation`, `loop_suspected` ⇒ REQUIRE_APPROVAL at proposal (unit half of S06.2 #4).
- `test_bot_chat_is_never_auto_replied` (named; S06.2 #5): `is_bot` ⇒ DENY at proposal with everything else passing; at execution `autonomous_execution_target` ⇒ `(POLICY_DENIED, NONE)`.
- `test_language_mismatch_requires_approval` (named; S06.2 #9 unit half: the declared language disagrees with the tagger).
- `test_explanation_coverage_for_every_autonomy_code` (named "explanation coverage"): every autonomy code has a sentence in `EXPLANATIONS`, equal to `AUTONOMY_CODES[code].explanation`, ending with ".", with no placeholders; the 20 new codes are one sentence each; `autonomy_paused` and `no_delegation` equal their M04 text.
- `test_rg18_a_failed_send_precondition_denies_an_autonomous_action_in_both_phases` (RG-18 extension, the policy half; M6-TXA extends the Tx A half): with every autonomy fact passing, (a) proposal phase, parametrized over each proposal-phase send-precondition failure (`account_inactive`, the four connection codes, `not_owned`, `bot_chat`, `chat_ignored`, `chat_possibly_paused`, `window_closed`, `takeover`, `consent_missing`, `empty_text`, `too_long`) ⇒ DENY; (b) execution phase, the same set plus `source_deleted`, `new_contact_message`, `max_age`, `sending_disabled` ⇒ DENY and `autonomous_execution_target` returns a reason with `Conversion.NONE`; (c) `inbox_pending` or `barrier_pending` alone ⇒ WAIT, and `already_claimed` ⇒ DENY with target `None` (never ALLOW_AUTO in any case).

Rule and outcome tests:
- `test_allow_auto_only_when_every_send_precondition_and_autonomy_rule_passes` (both phases; the decision holds ten passing autonomy checks).
- `test_a_model_flag_can_only_downgrade_an_allow_auto` — each model flag at its failing value turns ALLOW_AUTO into REQUIRE_APPROVAL; no flag value makes a failing application rule pass.
- `test_autonomy_only_failures_require_approval_at_proposal` — parametrized over every autonomy code.
- `test_allow_auto_needs_a_passing_check_for_each_autonomy_rule` — with `assistant.policy.engine.RULES` monkeypatched to drop `language_gate`: REQUIRE_APPROVAL at proposal, DENY at execution.
- `test_other_origins_never_get_allow_auto_at_proposal` — `owner_dictated` and `agent_proposal` with every P3 fact present.
- `test_missing_facts_fail_each_autonomy_rule_with_its_first_code` — `delegation`, `counters`, `autonomy` each `None`.
- `test_delegation_active_checks_the_window_itself` — at and after `ends_at`, before and at `starts_at`, standing, `ended`/`cancelled` status, not covered, foreign account.
- `test_in_scope_needs_the_topic_and_an_allowed_behaviour` (also the unit half of S06.2 #2: `ask_for_details` or `acknowledge_and_defer` passes only when the delegation allows it, otherwise `behaviour_not_allowed` and a card)
- `test_flags_and_counters_of_the_wrong_type_or_sign_fail_closed` — `"false"`/`"true"` strings and `None` in each boolean flag, a negative value in each counter and in `ungrounded_values` ⇒ the rule fails with its code.
- `test_a_backlog_message_older_than_30_minutes_requires_approval` (S06.2 #7 unit half; boundary: exactly 30 min is fresh).
- `test_a_commitment_or_sensitive_draft_requires_approval` (S06.2 #3 unit half) and `test_draft_safe_reports_the_first_failing_flag` (order and unknown values).
- `test_content_grounded_fails_on_any_ungrounded_value` (S06.2 #8 unit half; negative count fails).
- `test_an_unlisted_contact_gets_no_knowledge_based_auto_reply` (S06.2 #10 unit half: `knowledge_ids_allowed` empty, used non-empty).
- `test_disclosure_present_fails_only_when_required_and_missing`
- `test_volume_ok_reports_conversation_then_delegation_then_loop`
- `test_language_gate_blocks` (S06.2 #6 unit half): unknown, mismatch, not enabled, empty enabled set.
- `test_identity_answer_passes_only_scope_safety_and_grounding` (P3-T51): with `identity_answer=True`, the three rules pass with failing flags; every other rule still fails on its own failing fact.
- `test_autonomous_execution_target_follows_the_contract_table` — parametrized over all 22 autonomy codes ⇒ `(execution_reason, conversion)` from `AUTONOMY_CODES`.
- `test_autonomous_execution_target_never_converts_a_send_precondition_failure` — takeover, window closed, consent missing, sending disabled, a generic one ⇒ the M04 reason with `NONE`; takeover plus volume cap ⇒ `(TAKEOVER, NONE)`; window closed plus takeover ⇒ `(WINDOW_CLOSED, NONE)` (RULES order).
- `test_send_precondition_reasons_match_the_m04_targets` — for every send-precondition code, the reason equals the one M04's `_EXECUTION_TARGETS` gives (or `POLICY_DENIED`).
- `test_autonomous_execution_target_leaves_allow_auto_wait_and_claimed_actions_alone`
- `test_autonomous_execution_target_ignores_a_pending_inbox_beside_another_failure`
- `test_every_autonomous_execution_target_is_a_legal_cancellation` — every returned reason is a `StateReason`; `(APPROVED, CANCELLED)` and `(SCHEDULED, CANCELLED)` are in `TRANSITIONS`.

### 5.2 Updates to existing tests (the new contract replaces the P2 expectation; nothing is weakened)

| Test | Change |
|---|---|
| `test_policy_engine.py::test_proposal_never_allows_auto_in_p2` | Replaced by `test_proposal_allows_auto_only_for_an_autonomous_action_that_passes_everything`: a `delegation_auto` proposal with no facts is REQUIRE_APPROVAL with failed codes exactly `[no_delegation, out_of_scope, trigger_stale, low_confidence, ungrounded_value, knowledge_not_shareable, disclosure_missing, volume_conversation, language_unknown]`; with every fact passing it is ALLOW_AUTO; sending off and an undrained inbox are still not proposal facts |
| `test_policy_engine.py::test_decide_is_pure_and_total` | The strategies gain optional `DelegationSnapshot`, `RateCounters`, `AutonomyFacts` (including unknown strings for the string flags); the outcome oracle becomes §3.4's rule; the target check uses `autonomous_execution_target` for `delegation_auto` and `execution_target` otherwise |
| `test_policy_rules.py::test_rule_names_and_order_match_the_contract` | Gains the eight names after `delegation_active` |
| `test_policy_rules.py::test_rules_not_applicable_to_a_phase_or_kind_are_left_out` | Non-autonomous origins drop all ten `AUTONOMY_RULES` |
| `test_policy_rules.py::test_an_autonomous_action_never_passes_without_a_delegation_in_p2` | Renamed `test_an_autonomous_action_never_passes_without_a_delegation`; assertions kept |

These pass unchanged (a reviewer checks none was edited): `test_policy_engine.py` — `test_execution_failure_maps_to_its_state`, `test_execution_targets_are_legal_transitions_from_approved`, `test_checks_list_every_evaluated_rule_in_order`, `test_wait_only_when_inbox_drained_is_the_sole_failure`, `test_proposal_denies_on_any_failed_precondition`, `test_every_reason_code_has_an_explanation`, `test_spec_example_sentences_are_used_verbatim`, `test_explanations_never_contain_placeholders_for_content`; `test_policy_rules.py` — `test_the_default_context_passes_every_rule`, `test_rg19_an_autonomous_action_is_denied_while_paused` (RG-19, rule level), `test_every_rule_reports_only_its_declared_reason_codes`, `test_rules_read_only_the_context`; `test_f0_shared.py::test_policy_imports_nothing_that_reaches_the_database`; and the integration tests named in §3.8.

### 5.3 Mutation table (Tier-1A; QA applies each, never committed)

| # | Rule, lock or guard | Mutation | Named test expected to fail |
|---|---|---|---|
| 1 | `delegation_active` end time | drop `now >= ends_at` | `test_delegation_active_checks_the_window_itself` |
| 2 | `delegation_active` status | drop `status != "active"` | `test_delegation_active_checks_the_window_itself` |
| 3 | `delegation_active` start time | drop `now < starts_at` | `test_delegation_active_checks_the_window_itself` |
| 4 | `delegation_active` coverage and account | drop `covers_conversation` / the account check | `test_delegation_active_checks_the_window_itself` |
| 5 | `in_scope` topic | drop the topic condition | `test_in_scope_needs_the_topic_and_an_allowed_behaviour` |
| 6 | `in_scope` behaviour | accept `"none"` | `test_in_scope_needs_the_topic_and_an_allowed_behaviour` |
| 7 | `trigger_fresh` | `>` → `>=`, or drop the `None` check | `test_a_backlog_message_older_than_30_minutes_requires_approval` |
| 8 | `draft_safe` confidence | accept `"medium"` | `test_draft_safe_reports_the_first_failing_flag` |
| 9 | `draft_safe` commitment / sensitive | drop either flag | `test_a_commitment_or_sensitive_draft_requires_approval` |
| 10 | `draft_safe` escalation category | pass unknown categories | `test_draft_safe_reports_the_first_failing_flag` |
| 11 | `content_grounded` | `!= 0` → `> 1` | `test_content_grounded_fails_on_any_ungrounded_value` |
| 12 | `knowledge_scope` | subset → intersection | `test_an_unlisted_contact_gets_no_knowledge_based_auto_reply` |
| 13 | `disclosure_present` | drop the rule's failure | `test_disclosure_present_fails_only_when_required_and_missing` |
| 14 | `volume_ok` caps | `>=` → `>` | `test_volume_ok_reports_conversation_then_delegation_then_loop` |
| 15 | `volume_ok` missing counters | pass when `counters is None` | `test_missing_facts_fail_each_autonomy_rule_with_its_first_code` |
| 16 | `volume_ok` loop guard | drop `repeated_contact_message` | `test_volume_ok_reports_conversation_then_delegation_then_loop` |
| 17 | `language_gate` unknown / mismatch / enabled | drop each condition in turn | `test_language_gate_blocks`, `test_language_mismatch_requires_approval` |
| 18 | Identity exception scope | also pass `knowledge_scope` on `identity_answer` | `test_identity_answer_passes_only_scope_safety_and_grounding` |
| 19 | Model flags only downgrade | let `in_scope = True` pass `content_grounded` | `test_draft_safe_is_never_the_sole_basis_for_allow_auto` |
| 20 | Send precondition ⇒ DENY | REQUIRE_APPROVAL for a failed send precondition | `test_bot_chat_is_never_auto_replied`, `test_rg18_a_failed_send_precondition_denies_an_autonomous_action_in_both_phases` |
| 21 | Cap ⇒ card | DENY for an autonomy failure | `test_cap_reached_gives_a_card_never_deny` |
| 22 | Other origins never ALLOW_AUTO at proposal | drop the `autonomous` branch | `test_other_origins_never_get_allow_auto_at_proposal` |
| 23 | Completeness guard | remove `_autonomy_complete` | `test_allow_auto_needs_a_passing_check_for_each_autonomy_rule` |
| 24 | Execution target: no conversion for send preconditions | return `CARD` for `takeover` | `test_autonomous_execution_target_never_converts_a_send_precondition_failure` |
| 25 | Execution target: claimed and WAIT left alone | drop the `already_claimed` check | `test_autonomous_execution_target_leaves_allow_auto_wait_and_claimed_actions_alone` |
| 26 | Execution target skips `inbox_drained` | stop skipping | `test_autonomous_execution_target_ignores_a_pending_inbox_beside_another_failure` |
| 27 | Exact boolean values | `if a.uses_only_permitted_information` (truthiness) instead of `is True` | `test_flags_and_counters_of_the_wrong_type_or_sign_fail_closed` |
| 28 | Negative counters fail | drop the negative check on `conversation_last_hour` | `test_flags_and_counters_of_the_wrong_type_or_sign_fail_closed` |
| 29 | Empty `enabled_languages` enables nothing | treat an empty set as "all languages" | `test_language_gate_blocks` |
| 30 | Send-precondition reasons from `_EXECUTION_TARGETS` | map `window_closed` to `POLICY_DENIED` | `test_send_precondition_reasons_match_the_m04_targets` |

## 6. Acceptance criteria

1. For `delegation_auto`: ALLOW_AUTO only when all send preconditions and all ten autonomy rules pass; an autonomy-only failure is REQUIRE_APPROVAL; a send-precondition failure is DENY; every ALLOW_AUTO decision holds ten passing autonomy checks.
2. `draft_safe` is never the sole basis for ALLOW_AUTO; a cap gives a card, never DENY; bot chats are never auto-replied; a language mismatch or an unknown/mixed language gives a card; missing facts, wrong-typed flags and negative counters fail closed.
3. `autonomous_execution_target` follows P3-interfaces §3.2/§3.6 for all 22 autonomy codes and never converts a send-precondition failure.
4. Owner-dictated and agent-proposal outcomes, `execution_target` and every M04 sentence are unchanged (their existing tests pass).
5. `decide` is pure and total (hypothesis test with the new facts).
6. Every §5 test exists and passes in CI; the mutation table is complete and QA killed every row; ruff, format, mypy and `lint-imports` are clean.
7. ARCHITECTURE §9.2 and §9.3 updated.

## 7. Docs

- **ARCHITECTURE §9.2 (as built):** proposal outcomes for `delegation_auto` (DENY / REQUIRE_APPROVAL / ALLOW_AUTO) and the completeness guard.
- **ARCHITECTURE §9.3 (as built):** replace "the other autonomy conditions above arrive with M06" with the rule table (codes, explanations' source in `policy/codes.py`, the identity exception, model flags only downgrade), consistent with the TL's F0-C part 2 corrections (conflicts 7 and 16); cite ADR-17 (application-inserted disclosure and the autonomy decision) and ADR-20 (counters derived from `actions` under the delegation lock).
- **PR description:** the TL clarifications in this spec (foreign snapshot ⇒ `not_covered`; negative ungrounded count fails; `inbox_drained` skipped by the execution target) and open items for M06.md.

## 8. Owner decisions

- **OD-6 (caps and loop guard):** the rules compare counters with the snapshot's limits; different numbers change only F0-S's settings and M6-DELEG's card, not this unit.
- **OD-7 (scope):** `topic_scoped`, `covers_conversation` and one live delegation per chat come from the snapshot; a different answer changes the loader and M6-DELEG.
- **OD-10 (identity answer):** the three-rule exception (§3.3). If the owner chooses "always escalate", the planner never sets `identity_answer` and the exception is never used; the code may stay.
- **OD-11 (fallbacks; answered 2026-10-29, changed from the default):** REQUIRE_APPROVAL is the held reply (presented as an alert with a Draft reply button, M6-PLAN and M6-ESC); the conversions come from `AUTONOMY_CODES`. This unit's code is unchanged by the answer.
- **OD-14 (language gate):** if the owner drops the language gate, `language_gate` leaves `AUTONOMY_RULES` through a codes ICR and its rule and tests are removed here.
- **OD-5 and OD-32** (takeover scope, disclosure re-trigger) are facts computed by the loader (M6-TAKE, `actions/disclosure.py`); the rules do not change with them. **OD-31** (approvals under `/pause`) does not touch policy: `autonomy_not_paused` applies to `delegation_auto` only.

## 9. Risks and notes

- **Replaced P2 expectations can look like weakened tests.** Reviewers check that each change in §5.2 is exactly the new contract and that no assertion about owner and agent-proposal actions is dropped.
- **Fact quality decides safety.** A wrong fact from the loader (M6-PLAN, M6-TXA) passes a rule; that is why the application facts are built by one loader for proposal, planner and claim (P3-interfaces §3.5) and Tx A re-runs every rule. This unit's tests cover the rules, not the loader.
- **`fyi` passes `draft_safe`** by design (PRD §11.5: FYI goes to the digest); the escalation enqueue for any category other than `none` is M6-PLAN's.
- **`identity_answer` bypasses three rules,** so it must be an application fact derived from the final text (the text equals `disclosure.identity_answer(locale)` for an enabled `GateLanguage`), never copied from a model flag. M6-PLAN and M6-TXA own and test that derivation; a loader bug there would send model text past `draft_safe` and `content_grounded`. The PR names this for the M6-PLAN and M6-TXA reviewers.
- **Code order is part of the contract:** `autonomous_execution_target` and stored decisions (OPS-TV3) depend on `RULES` order and the ten check names; changing either needs an ICR.
