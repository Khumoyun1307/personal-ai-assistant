# NUDGE-POL — J7 nudge policy path: `propose(trigger=)`, the `no_takeover_since` reference, never `delegation_auto`

| | |
|---|---|
| **Unit** | NUDGE-POL (S07.2 send-path part; J7) |
| **Team / slot** | Josh / J1 |
| **Size** | S (≈ 0.5 eng-day) |
| **Depends on** | F0-S (`actions.trigger_automation_id`, `ck_actions_trigger_not_autonomous`, `AutomationTrigger`, the `propose(..., trigger=)` signature that refuses with `trigger_not_available`). Sequenced after M6-PLAN in `actions/proposals.py` and after M6-PLAN, M6-TXA and M6-TAKE in `actions/policy_loader.py` (Josh lead posts the order); if M6-PLAN has not merged when this unit starts, this unit goes first in `proposals.py` and M6-PLAN rebases |
| **Branch → base** | `p3/nudge-pol` → `int/p3` |
| **Peer reviewer** | E3 (Codex; author of M7-NUDGE, the consumer), dispatched by the TL |
| **QA slot and lane** | QJ1 or QJ2, before merge |
| **Tier** | 1 (TL scope check). Merges before M7-FUP and M7-NUDGE (J7 chain, P3-interfaces §15.5) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §2.3, §4.3, §15; [P3-schema](../contracts/P3-schema.md) §3.1; ARCHITECTURE §9.3 (`no_takeover_since`), §9.4; PRD J7, FR-FUP-06, §12.2; IMPLEMENTATION_PLAN §9 S07.2, §10 RG-06, RG-18; decisions OD-18, P3-T24, conflict 1 |

## 1. Goal

A follow-up nudge (J7 step 4) becomes a normal **approval card** through `propose()`, linked to its follow-up automation. Its takeover reference is the moment the nudge was made, so an owner "ok" sent after the contact's promise does not block it (D9, P3-T24). It is never an autonomous send, under any delegation (OD-18), and every M04 send rule still applies at proposal and at claim.

## 2. Scope

**In:**
1. `actions/proposals.py`: the trigger path of `propose(..., trigger=AutomationTrigger)` (replaces F0-S's fail-closed refusal).
2. `actions/policy_loader.py`: the `no_takeover_since` reference for automation-triggered actions, at proposal and at execution.
3. Tests: RG-18 and RG-06 extensions, the two named J7 tests, refusals, idempotency.

**Out:**
- The `followups.nudge` job, the draft, the reminder text and the copy-ready message (M7-NUDGE); the follow-up deadline handler (M7-FUP); the card's rendering (S04.3 code, unchanged).
- Any autonomous or delegation-authorized automation send (none in P3, OD-18).
- `actions/executor.py` (M6-TXA's file): this unit changes the reference inside `load_context`, which Tx A already calls, so the executor stays as it is.

## 3. Design

### 3.1 `propose` with a trigger (`actions/proposals.py`)

Signature from F0-S (P3-interfaces §4.3), unchanged:

```python
async def propose(
    session,
    request: ProposalRequest,
    *,
    clock,
    config: ActionsConfig,
    trigger: AutomationTrigger | None = None,
    autonomy: AutonomyConfig | None = None,
) -> ProposalResult: ...
```

New behaviour when `trigger is not None`, in this order, before any lock:
1. `request.origin is not ActionOrigin.AGENT_PROPOSAL` → `ProposalRefused("trigger_not_available")`. A nudge is never `delegation_auto` and never owner-dictated (OD-18). The database CHECK `ck_actions_trigger_not_autonomous` is the backstop.
2. `request.delegation_id is not None` → `ProposalRefused("trigger_not_available")`. A nudge is an owner card, not a delegation held reply (OD-11), so it never enters delegation counters or the delegation's activity (**TL decision (spec)**; contract gap G15).
3. The `base_required` rule (an `agent_proposal` without `trigger_turn_id` and without `base_message_id` is refused) is satisfied by the trigger: a nudge's takeover reference is its own creation (§3.2), not a base message.

Then, after the existing `_by_call` check and the account (`FOR SHARE`) and conversation (`FOR UPDATE`) locks:
4. **Trigger check** (fail closed; RG-21): `SELECT kind, refs FROM automations WHERE id = :automation_id AND account_id = :account_id`. No row → `NotInAccountError` (another account's or deleted). `kind != 'follow_up'` → `ProposalRefused("trigger_not_available")`. `refs` does not name `request.conversation_id` (`refs @> {"conversation_ids": [conversation_id]}`) → `ProposalRefused("trigger_not_available")`: a nudge can only go to the chat its follow-up watches (a wrong recipient is the worst failure). The automation's status is not checked here; M7-NUDGE re-checks it before proposing.
5. **Reuse** (`_live_for_trigger`): a live card (`awaiting_approval` or `revising`) in this conversation with `trigger_automation_id = trigger.automation_id` → return it as reused (`ProposalResult(reused=True)`), with no new row, no supersede and no audit row. A retried nudge job never makes a second card (P3-interfaces §4.3). A non-live earlier nudge (approved, sent, blocked, outdated, rejected) does not count: a reopened follow-up may nudge again (P3-schema §3.1).
6. The proposal-phase context is built with `owner_requested=True` (§3.2).
7. `_insert` writes `trigger_automation_id = trigger.automation_id`; `delegation_id` stays NULL; `draft_flags` are the caller's (content-free keys only). `trigger.occurrence_at` is not stored (M7-NUDGE's own idempotency uses it).
8. The rest is the M04 path, unchanged: REQUIRE_APPROVAL → `awaiting_approval` with an approval token, superseding the chat's live card (a nudge card replaces an older live card exactly as any proposal does); DENY → `blocked` with the reason (for example `window_closed`: the card shows copy-ready text, which is M7-NUDGE's "window closed at the deadline" outcome). A proposal decision is never ALLOW_AUTO for this origin (M6-POL keeps ALLOW_AUTO for `delegation_auto` only). Belt and braces (invariant 1, OD-18): if the decision for a trigger is ALLOW_AUTO anyway (a policy bug), `propose` raises `ProposalRefused("trigger_not_available")` and inserts nothing; the trigger branch never calls `actions/release.py::schedule_autonomous` and never sets `approved` itself.

Without a trigger, `propose` behaves exactly as before this unit.

### 3.2 The takeover reference (`actions/policy_loader.py`)

The rule (`policy/rules.py::no_takeover_since`, unchanged) compares the newest owner manual message with `freshness.base_message_sent_at or action.created_at`. `_freshness` fills `base_message_sent_at` only when `owner_requested` is false (P2 decisions R3, cited in `actions/policy_loader.py::_freshness`). An automation-triggered action must be measured from its own creation (D9):

- **Proposal phase:** `propose` passes `owner_requested=True` when `trigger is not None` (§3.1 step 6). There is no action row yet; `ActionFacts.created_at = now`.
- **Execution phase (Tx A):** the executor computes `owner_requested` from `origin` and `trigger_turn_id` only (M6-TXA's file). So `load_context` itself adds, when `facts.action_id is not None` and `owner_requested` is false: one account-scoped read `SELECT trigger_automation_id FROM actions WHERE id = :action_id AND account_id = :account_id`; a non-NULL value sets `owner_requested = True` for the freshness read. This is a new private helper `_automation_triggered(session, *, account_id, action_id) -> bool` beside `_freshness`; the M04 behaviour for every other action is unchanged.

Effect: an owner message sent between the contact's promise and the nudge's creation does not block the card or its send; an owner message sent **after** the card was created does block the send at Tx A (`takeover`, as for any owner request). If the follow-up automation is deleted after the card was created, `trigger_automation_id` becomes NULL (FK `SET NULL`) and the reference falls back to the base message: the stricter M04 reading, so the send may be blocked as `takeover` (fail closed, never looser). `not_stale` (a newer contact message than `shown_through_message_id`), the reply window, connection, consent, mode, Ignore and every other M04 precondition apply unchanged at proposal and at claim.

### 3.3 Interface consumed by M7-NUDGE

M7-NUDGE calls `propose(session, ProposalRequest(origin=AGENT_PROPOSAL, kind=SEND_MESSAGE, text=<draft>, base_message_id=<None or the chat's newest contact message>, shown_through_message_id=<the draft's>, run_id=None, tool_call_id=None, trigger_turn_id=None, draft_run_id=<the draft run>, draft_flags=…, delegation_id=None), clock=…, config=…, trigger=AutomationTrigger(automation_id, occurrence_at))` and reads `ProposalResult.state` (`awaiting_approval` or `blocked`) and `reused`. Refusal codes it must handle: `trigger_not_available` (a bug or a follow-up that no longer names the chat: the reminder says "no draft") and `NotInAccountError` (the follow-up was deleted).

### 3.4 Edge cases (each has a test in §5)

An owner "ok" between the promise and the nudge; an owner message after the card; a contact message after the card; the window closed at the deadline; a second nudge job for the same occurrence; a reopened follow-up nudging again after the first card was rejected; a trigger with origin `delegation_auto` or `owner_dictated`; a trigger with a `delegation_id`; a trigger whose automation is a reminder, belongs to another account, or names another chat; a nudge in a chat covered by an active delegation with `autonomy.send` on; an ALLOW_AUTO decision for a trigger (a policy bug); a follow-up deleted after its nudge card was created.

## 4. Files

**Owned (named touch points in never-edit files, P3-interfaces §15.1–§15.2):**
- `actions/proposals.py`: the trigger branch, `_live_for_trigger`, the trigger check and the `_insert` column (M6-PLAN owns the `delegation_auto` path in the same file; Josh lead sequences).
- `actions/policy_loader.py`: `_automation_triggered` and its one use in `load_context` (separate from M6-PLAN's, M6-TXA's and M6-TAKE's functions).
- Test files: `tests/integration/test_nudge_policy.py`, `tests/integration/test_log_hygiene_nudge_pol.py`.

**Named touch points elsewhere:** none. **ARCHITECTURE:** P3-interfaces §15.4 gives this unit no section, and §9.3 belongs to M6-POL and the TL's F0-C part 2 correction (contract gap G13). So this PR does **not** edit ARCHITECTURE: its description carries the one sentence for §9.3's `no_takeover_since` row ("for an automation-triggered proposal the reference is the action's creation, P3-T24; a deleted trigger falls back to the base message"), and the TL adds it in F0-C part 2 (TL ruling R52).

**Consumed, never edited:** `actions/{ports,states,models,lifecycle,executor}.py`, `policy/*`, `automation/models.py`.

**Never edit without an ICR** (§15.2): `actions/executor.py` (M6-TXA), `policy/*`, `actions/{lifecycle,states,ingestion,autonomy}.py`, migrations, `config.py`, `core/*`.

## 5. Tests (red first)

`tests/integration/test_nudge_policy.py`: real PostgreSQL; a follow-up automation from `p2_factories.seed_automation(kind="follow_up", refs={"conversation_ids": [chat], "contact_ids": [contact]}, …)`; the real `propose` and the real executor (`execution_env.harness`, `FakeTelegramSender`, `FakeClock`).

**Named in the P3 plan (README §3) and IMPLEMENTATION_PLAN:**
- `test_an_owner_ok_after_the_promise_does_not_block_the_nudge_card`: contact promise at T0; owner manual "ok" at T1; nudge proposed at T2 with `base_message_id` = the promise → `awaiting_approval` (not `blocked/takeover`); approved and claimed at T3 → sent once.
- `test_a_nudge_is_never_auto_sent`: an active delegation covers the chat, `autonomy.send` on, `AUTONOMY_LANGUAGES` set → the nudge is `awaiting_approval` with `delegation_id` NULL and stays there until the owner approves; no `scheduled`, no `approved` without the tap, zero business calls before the tap.
- `test_rg18_a_nudge_outside_the_reply_window_is_blocked_with_copy_ready_text_and_no_telegram_call` (RG-18 extension; S07.2 "window closed at deadline ⇒ copy-ready only"): the window closed → `blocked/window_closed`, zero sender calls; an approved nudge whose window closed before the claim → `expired` or `blocked` per M04, never sent.
- `test_rg06_a_contact_message_after_the_nudge_card_outdates_it` (RG-06 extension): a newer contact message → the card is outdated at proposal re-check or refused at claim; never sent.

**Refusals, reuse and scope:**
- `test_a_trigger_on_another_origin_is_refused` (parametrised `delegation_auto`, `owner_dictated` → `trigger_not_available`, no row).
- `test_a_trigger_with_a_delegation_id_is_refused`.
- `test_a_trigger_satisfies_base_required` (no `base_message_id`, no `trigger_turn_id` → a card).
- `test_a_trigger_sets_trigger_automation_id_and_nothing_else`: `trigger_automation_id` set, `delegation_id` NULL, `disclosure_included` false.
- `test_a_retried_nudge_reuses_its_live_card` (no second row, `reused=True`, no `action_superseded` audit).
- `test_a_reopened_follow_up_may_nudge_again_after_its_card_was_rejected`.
- `test_rg21_a_trigger_of_another_account_is_not_found`.
- `test_a_trigger_that_is_not_a_follow_up_of_this_chat_is_refused` (a reminder automation; a follow-up naming another chat).
- `test_an_owner_message_after_the_nudge_card_blocks_its_send` (Tx A: `blocked/takeover`, the reference is `created_at`).
- `test_the_takeover_reference_of_other_proposals_is_unchanged` (pins: a proactive Assist draft is still measured from its base message; an owner-request card from its creation).
- `test_an_allow_auto_decision_for_a_nudge_is_refused` (a monkeypatched `decide` returning ALLOW_AUTO for the trigger → `trigger_not_available`, no row, no `scheduled`, zero business calls).
- `test_a_nudge_whose_follow_up_was_deleted_falls_back_to_the_stricter_reference`: card created; the automation deleted (`trigger_automation_id` NULL); an owner message between the promise and the card → the claim ends `blocked/takeover`; never sent.

**Scenarios:** QA-S7 (QE1) owns `tests/scenarios/m07/` (the J7 flagship scenario). When a strict-xfail marked `awaiting NUDGE-POL` turns green, tell QE1, who removes the marker in a commit on this PR's branch (P3-interfaces §15.1).

**Log hygiene** (`tests/integration/test_log_hygiene_nudge_pol.py`): `test_nudge_proposals_log_no_text` — a nudge with canary text in its body; no canary in logs or audit rows.

Run locally: these files plus `tests/integration/test_action_proposals.py`, `tests/integration/test_policy_loader.py`, `tests/integration/test_execution.py`; ruff, format, mypy on `src/assistant/actions`; `lint-imports`.

## 6. Acceptance criteria

1. `propose(..., trigger=…)` creates an `agent_proposal` card linked by `trigger_automation_id`, only for a follow-up automation of the same account that watches the same chat; every other combination is refused or not found, with no row.
2. A nudge is never `delegation_auto`, never carries `delegation_id` and never reaches `approved` without the owner's tap.
3. The takeover reference of an automation-triggered action is its creation, at proposal and at claim; every other action's reference is unchanged.
4. A retried nudge reuses its live card.
5. Every named test in §5 passes in CI; RG-06 and RG-18 extended; no RG test weakened.
6. The PR description carries the ARCHITECTURE §9.3 sentence for the TL; this PR edits no ARCHITECTURE section.

## 7. Docs updated in the same PR

None in ARCHITECTURE (P3-interfaces §15.4; contract gap G13). The PR description carries the §9.3 `no_takeover_since` sentence for the TL (§4) and restates the consumer interface of §3.3 for E3.

## 8. Owner decisions

- **OD-18** (and conflict 1): nudges always need the owner's approval, even under a delegation, and the "did you already write?" check counts from the nudge's creation. If the owner allowed delegation-authorized nudges, §3.1 step 1 would admit a second origin path through M6-PLAN's planner, a contract change (ICR) and new tests; if the owner wanted the check to count from the promise, §3.2 would be dropped (the reference would be the base message, M04 behaviour).

## 9. Risks and notes

- **Contract gap G15:** refusing a trigger with `delegation_id`, and checking that the follow-up names the chat, refine P3-interfaces §4.3; both fail closed.
- **Contract gap G13:** the execution-phase reference lives in `load_context` because the executor (M6-TXA) computes `owner_requested` without the trigger; one extra indexed read per claim of an existing action. P3-interfaces §15.4 lists the §9.3 sentence under the TL's F0-C part 2 corrections (R52).
- **Shared files:** four Josh units edit `policy_loader.py` and two edit `proposals.py`. Keep the diff to the named functions; rebase on the latest `int/p3` right before review.
