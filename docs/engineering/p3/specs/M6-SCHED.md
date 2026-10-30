# M6-SCHED — the `scheduled` state end to end

| | |
|---|---|
| **Unit** | M6-SCHED ★ — `scheduled` state, `actions.release_scheduled`, due reconciler, bulk cancel paths |
| **Team / slot** | Josh / J1 (schema steward) |
| **Size** | M |
| **Depends on** | F0-S (README §3). It also uses F0-R's pre-registered `jobs/tasks_release.py` and `_wire_autonomy`, and M6-POL's `autonomous_execution_target`; both merge before it (P3-interfaces §15.5 steps 1 and 3). If M6-POL is not merged when you start, stack on `p3/m6-pol` (WORKFLOW §4.2) |
| **Branch → base** | `p3/m6-sched` → `int/p3` |
| **Peer reviewer** | E1 (Codex, dispatched by the TL) |
| **QA** | QJ1 or QJ2, before merge, with the mutation table (§5.6) |
| **Tier** | 1A: Gemini read (David lead), TL deep review, part of CP-A |
| **Normative inputs** | ARCHITECTURE §9.3, §9.4, §12.1–§12.2, §13; P3-interfaces §2.1–§2.4, §3.2, §3.6, §4.1, §6.1, §7.1, §7.3, §10, §13, §15; P3-schema §3.1, §7.1; AGENTS §6 invariants 1, 6, 7, 10, 11, §13; IMPLEMENTATION_PLAN §8 S06.2/S06.3, §10 (RG-19, RG-20); README §3 row M6-SCHED |

## 1. Goal

An autonomous reply that passed the proposal policy waits `AUTONOMY_SEND_DELAY_SECONDS` in `scheduled`. When the delay ends, `actions.release_scheduled` re-checks it under the §9.4 locks and either releases it to `actions.execute` or cancels it with a truthful reason. Everything that must stop autonomy (owner takeover, a new contact message, `/pause`, a rights change, a disabled connection, a consent revoke, an edit or deletion of the answered message) cancels a `scheduled` or autonomous `approved` action at once. An autonomous action never ends `outdated`, and an `executing` one is never touched (invariants 1, 6, 7, 10).

## 2. Scope

**In:**
1. `actions/release.py` (new): `schedule_autonomous` (contract seam for M6-PLAN), `defer_release_after_commit`, `ReleaseJob` (the `actions.release_scheduled` body) and `convert_cancelled` (the card conversion and the one disclosure re-proposal, shared with M6-TXA).
2. `actions/lifecycle.py`, bulk helpers only: `cancel_autonomous_in_conversation`; autonomous actions first in `outdate_pending_for_account`; `cancel_pending_ai_generated` also covers `delegation_auto`; `_change_pending` audit rows carry `delegation_id`.
3. `actions/ingestion.py`: the takeover, new-contact-message, owner-edit, base-edit and base-deletion paths cancel autonomous `approved`/`scheduled` actions first; a new contact message (and a contact edit of the answered message) re-requests a delegated reply.
4. `jobs/tasks_release.py` (registration body), `jobs/reconcile_actions.py` (`unqueued_releases`, `unconverted_autonomous`).
5. Lines in `_wire_autonomy` (`bootstrap.py`).
6. Tests proving the existing paths that already cover `scheduled` (pause, disabled connection, Ignore mode, data deletion, restore).

**Out:** `propose()`'s `delegation_auto` path and the `replies.delegated` job that calls `schedule_autonomous` (M6-PLAN); Tx A (M6-TXA); the takeover marker, notice and Resume here (M6-TAKE); pause sources, the Pause button and bot-block pause (M6-PAUSE); ending delegations (F0-S `end_delegations`, M6-DELEG, M6-LIFE); the autonomy fact builders (F0-S stubs, bodies by M6-PLAN; this unit only calls them from `_decide`); the autonomy rules (M6-POL).

## 3. Design

### 3.1 Contracts consumed and provided

| Consumes | From |
|---|---|
| `TRANSITIONS` (unchanged), event table `action_auto_approved` / `action_scheduled` / `action_released` and its origin guards | F0-S, P3-interfaces §2.2 |
| `StateReason.LATE`, `DELEGATION_ENDED`, `TRIGGER_STALE`, `VOLUME_CAP`, `DISCLOSURE_MISSING` | F0-S, §2.1 |
| `actions.scheduled_for`, `late_after`, `delegation_id`; `ix_actions_scheduled`; `ux_actions_autonomous_inflight`; `ux_actions_delegation_base` | F0-S, P3-schema §3.1 |
| `AutonomyConfig` (`send_delay`, `max_lateness`, `send_enabled`, …) | F0-S, §10.1 |
| `active_covering(..., for_update=True)`, `DelegationView` | F0-S, §6.1 |
| `policy.engine.autonomous_execution_target`, `policy.codes.Conversion` | M6-POL / F0-S, §3.2, §3.6 |
| `disclosure.split_disclosure`, `compose`; `language_keys.declared_key` | F0-S, §3.7, §5.1 |
| `RELEASE_TASK`, `release_queueing_lock`, `EXECUTE_TASK` | F0-S, §7.1 |
| `JobHandlers.release`, pre-registered task in `jobs/tasks_release.py` | F0-R, §7.2 |

| Provides | To |
|---|---|
| `release.schedule_autonomous(session, *, account_id: int, action_id: int, now: datetime, config: AutonomyConfig) -> bool` (contract signature, §4.1) | M6-PLAN (`replies.delegated`), `convert_cancelled` |
| `release.defer_release_after_commit(session, scheduler, *, action_id: int, at: datetime) -> None` | M6-PLAN, `convert_cancelled` |
| `release.convert_cancelled(...)` (§3.4) | M6-TXA (Tx A conversions), the release job, `unconverted_autonomous` |
| `lifecycle.cancel_autonomous_in_conversation(...)` (§3.5) | ingestion (here), M6-TAKE (`record_ingest_failure`) |
| Task `actions.release_scheduled`; reconcilers `unqueued_releases`, `unconverted_autonomous` | the worker |

### 3.2 `schedule_autonomous`

Called by M6-PLAN's job in the same transaction as `propose()`, right after it returned a new `delegation_auto` action in `proposed`. The caller already holds account `FOR SHARE`, conversation `FOR UPDATE` and the delegation row (propose takes them).

1. Read `Action.state`, `origin`, `delegation_id`, `text` for `action_id` and `account_id`. Not found, not `proposed`, or origin not `delegation_auto` ⇒ return `False` (nothing written).
2. `transition(proposed → approved, reason=None, values={"decided_at": now, "approved_text_hash": text_hash(text)}, details={"delegation_id": …})`. The event is `action_auto_approved` (F0-S's table; its guard refuses any other origin).
3. `scheduled_for = now + config.send_delay`; `late_after = scheduled_for + config.max_lateness`. `transition(approved → scheduled, values={"scheduled_for": …, "late_after": …}, details={"delegation_id": …, "scheduled_for": <epoch s>, "late_after": <epoch s>})`. Event `action_scheduled`.
4. Return `True` only when both transitions applied. It never checks flags (the caller decided shadow mode) and never defers a job: the caller calls `defer_release_after_commit(session, scheduler, action_id=…, at=scheduled_for)`.

`defer_release_after_commit` registers `db.uow.after_commit` work that runs `scheduler.defer(RELEASE_TASK, args={"action_id": id}, queueing_lock=release_queueing_lock(id), schedule_at=at)`. No procrastinate `lock` (AGENTS §13: never with `schedule_at`). A queueing-lock conflict returns `None` and is fine (one is already queued). A lost defer is recovered by `unqueued_releases` (§3.6).

### 3.3 The release job (`ReleaseJob.run(action_id) -> str`)

Returns a content-free code (also logged with `action_id` only). One transaction (`db.uow.transaction`):

1. Read `account_id, conversation_id, origin, delegation_id` of the action with no lock. Not found ⇒ `not_found`. Origin not `delegation_auto` ⇒ log `release_not_autonomous` and return `not_autonomous` (nothing written; a scheduled non-autonomous row cannot exist, F0-S's guard).
2. **Locks, in §9.4 order:** account `FOR SHARE`; the conversation (of that account) `FOR SHARE`; `active_covering(session, account_id=…, conversation_id=…, now=now, for_update=True)` (locks the covering delegation row); the action `FOR UPDATE`. The covering delegation counts only when `covering.delegation_id == action.delegation_id`; otherwise it is treated as `None`.
3. State not `scheduled` ⇒ `settled` (a second job, or a bulk path cancelled it). Idempotent.
4. `now < scheduled_for` (an early reconciler run, or clock skew) ⇒ after commit re-defer at `scheduled_for` (`defer_release_after_commit`); return `not_yet`. The re-defer is accepted although it uses the same queueing lock: this job's own row is `doing`, and procrastinate's queueing lock only blocks a second `todo` job.
5. **Late first:** `now >= late_after` ⇒ `transition(scheduled → cancelled, reason=LATE, values={"policy_decision": decision_record(Decision(Outcome.DENY, (CheckResult("on_time", False, "late"),)), Phase.EXECUTION)}, details={"delegation_id", "failed_rules": ["on_time"]})` (the executor's `_refused` pattern, written locally); after commit `convert_cancelled(conversion=CARD)`; return `late`. `late_after IS NULL` cannot happen on a `scheduled` row (`ck_actions_scheduled_has_time` and `ck_actions_schedule_pair`); if it ever does, fail closed the same way as Tx A (M6-TXA §3.2 step 8): `cancelled/policy_denied` with the check `CheckResult("on_time", False, "not_scheduled")` and **no** conversion; return `not_scheduled`.
6. **Decision** (execution phase): `_decide(session, action, delegation, now)` builds the `PolicyContext` (§3.3.1) and calls `decide`. The facts present the action as `approved` (the state it would move to), so `not_already_executed` asks the right question.
7. `ALLOW_AUTO` or `WAIT` ⇒ `transition(scheduled → approved, details={"delegation_id"})` (event `action_released`); store the decision record in `policy_decision`; only when the transition applied, after commit `scheduler.defer(EXECUTE_TASK, args={"action_id": id})` (no lock, no queueing lock, as the M04 approval path); return `released`. A transition that applied to nothing defers nothing and returns `settled`. `WAIT` (only `inbox_drained` failed) releases on purpose: Tx A owns the ingestion barrier and waits there, and a takeover still in the inbox is applied by the ingestion path before Tx A can pass.
8. `DENY` ⇒ `target = autonomous_execution_target(decision)`. `None` (only `already_claimed`, which cannot occur for a row that is `scheduled` under its lock) still fails closed: `cancelled/policy_denied`, no conversion, log `release_no_target`; a `scheduled` row is never left behind for the reconciler to wake again. Otherwise `transition(scheduled → cancelled, reason=target[0], values={"policy_decision": record}, details={"delegation_id", "failed_rules": [...]})`; after commit, only when the transition applied and `target[1]` is not `Conversion.NONE`, `convert_cancelled(conversion=target[1])`; return `cancelled_<reason>`.

The job holds no lock across anything slow: no model call, no Telegram call. After-commit work (the execute defer, a conversion) runs in its own `try/except Exception` with a comment saying why (a failure there must not make procrastinate retry a committed release); it logs `release_after_commit_failed` with the exception class name only. `unqueued_executions` (M04) recovers a lost execute defer; `unconverted_autonomous` (§3.6) recovers a lost conversion.

Never `blocked`, `outdated` or `expired` from this job: every exit from `scheduled` is `approved` or `cancelled` with a non-NULL reason (P3-interfaces §2.2). The only runs that leave the row `scheduled` are `not_yet` (re-deferred) and a rolled-back transaction (retried, or found by `unqueued_releases`).

#### 3.3.1 The release decision (written once; R79)

R18 makes M6-PLAN depend on M6-SCHED, but the autonomy facts come from the three builders in `actions/policy_loader.py` (P3-interfaces §3.5). F0-S ships `delegation_snapshot`, `rate_counters` and `autonomy_facts` there as fail-closed stubs (every one returns `None`), and M6-PLAN fills their bodies. So `release.py::_decide` is written **once, in this unit**, against those three functions, and M6-PLAN never edits it:

```python
async def _decide(self, session: AsyncSession, action: Action, delegation: DelegationView | None, now: datetime) -> Decision
```

1. `ctx = await load_context(session, account_id=…, conversation_id=…, facts=<ActionFacts from the row, state=APPROVED>, base_message_id=action.base_message_id, target_message_id=None, owner_requested=False, phase=Phase.EXECUTION, now=now, config=actions_config, ai_generated=True, process_run_id=actions_config.process_run_id)`, then `dataclasses.replace(ctx, sending_enabled=actions_config.sending_enabled and autonomy_config.send_enabled)`.
2. `delegation is None` ⇒ return that context (the three autonomy fields stay `None`; `delegation_active` fails first).
3. Otherwise read the chat's `contact_id` (account-scoped) and set `delegation = policy_loader.delegation_snapshot(...)`, `counters = await policy_loader.rate_counters(...)` and, when `action.base_message_id is not None`, `autonomy = await policy_loader.autonomy_facts(..., text=action.text, draft_flags=action.draft_flags or {}, base_message_id=action.base_message_id, ...)` (a NULL base message leaves `autonomy = None`, as M6-PLAN's `autonomous_context` does). This is the same assembly as `autonomous_context` steps 3 and 4 (M6-PLAN §3.2); M6-PLAN's `test_the_release_decision_and_autonomous_context_see_the_same_facts` keeps the two from drifting.
4. `return decide(ctx)`.

Until M6-PLAN merges, the stubs return `None`, so every autonomy rule fails closed (`delegation_active` first ⇒ `cancelled/delegation_ended`, no card). That is safe: before M6-PLAN no `delegation_auto` row can exist. When M6-PLAN fills the bodies, this function gets real facts with no edit here, and the strict-xfail rows "awaiting M6-PLAN" turn green.

`ai_generated=True` matters: it makes `consent_valid` apply to autonomous text (invariant 9).

### 3.4 Conversions (`convert_cancelled`)

```python
async def convert_cancelled(
    session: AsyncSession, *, account_id: int, action_id: int, conversion: Conversion, now: datetime,
    clock: Clock, actions_config: ActionsConfig, autonomy_config: AutonomyConfig,
    scheduler: JobScheduler | None, presenter: ApprovalCardPresenter | None,
) -> int | None:
```

Runs in its own transaction, after the cancelling transaction committed (callers: the release job, M6-TXA's Tx A, `unconverted_autonomous`). Returns the new action id, or `None`.

1. Read the cancelled action (account-scoped). It must be origin `delegation_auto` and `cancelled`; otherwise return `None`.
2. **Idempotency:** take account `FOR SHARE` and the conversation `FOR UPDATE` first (the same order `propose` uses), then check. If an action of the account exists with `draft_flags @> {"converted_from": action_id}` or `draft_flags @> {"reproposed_from": action_id}`, return its id; if an `action_converted` audit row with `subject_type = 'action'` and `subject_id = action_id` exists (the reused case in step 5), return its `to_action_id`. Two concurrent conversions of one action therefore serialize and the second finds the first.
3. **Nothing to convert** (return `None`; the reconciler's filter excludes both cases once settled, §3.6):
   - `text IS NULL` (a deletion cleared it): log `conversion_no_text`.
   - **Identity answer** (`draft_flags["identity_answer"]` is true, or the text equals `disclosure.identity_answer(lang)` for some `GateLanguage`): log `conversion_identity_skipped`. The text is the AI identity sentence, which starts with `AI_MARKER` and has no composed disclosure line for `split_disclosure` to strip, so a card would put an AI disclosure into an owner-approved message (PRD §11.4, OD-11 "carries no AI line"). No escalation was enqueued at plan time, because the sentence was to be sent (OD-10a), so the identity question now escalates instead (OD-10, R14): in this transaction, `enqueue(kind=DELEGATION_ESCALATION, priority=<the stored `draft_flags["escalation_category"]` mapped as M6-PLAN maps it, `fyi` (LOW) when it is `none` or missing>, dedupe_key=f"deleg:{delegation_id}:{conversation_id}:{base_message_id}", refs=NotificationRefs(conversation_ids, contact_ids, delegation_ids, message_ids=(base_message_id,), action_ids=(action_id,), alert_kind=<that category>), conversation_id, contact_id, scheduler=scheduler)`; the shared key means an escalation the plan already enqueued for another category is not repeated. This is the only write on this path. A re-proposal is not possible either: no model body is stored. (TL ruling R40: a cancelled identity answer never becomes a card; it escalates instead, and P3-interfaces §3.6 states it.)
4. `body = disclosure.split_disclosure(text)[1]`.
5. **`REPROPOSE`** (only reason `disclosure_missing`): if `draft_flags` already has `reproposed_from`, switch to `CARD` (at most one re-proposal per base message, R10). Else `locale = language_keys.declared_key(draft_flags.get("language", ""))`; `None` ⇒ switch to `CARD`. Else `view = await delegations_repo.get(session, account_id=…, delegation_id=action.delegation_id)`; `None` (deleted, or its spec scrubbed) ⇒ switch to `CARD`; else `wording = view.rules.disclosure.get(locale.value)` and `propose(ProposalRequest(origin=DELEGATION_AUTO, text=compose(body, locale=locale, wording=wording), base_message_id, shown_through_message_id, delegation_id, run_id=None, tool_call_id=None, trigger_turn_id=None, draft_run_id, draft_flags={**flags, "disclosure_required": True, "disclosure_added": True, "reproposed_from": action_id}), clock=…, config=actions_config, autonomy=autonomy_config)`. A new action ⇒ `schedule_autonomous(..., now=now)` must return `True` (else raise: the transaction rolls back and the reconciler retries) + `defer_release_after_commit` (the full proposal policy and a new send delay, P3-interfaces §3.6). `ProposalRefused` (`autonomy_downgraded`, or `autonomous_not_available` before M6-PLAN merges) ⇒ switch to `CARD`. **A reused result** (a unique index already holds the slot): when it carries `reproposed_from == action_id` it is this conversion's own earlier result; otherwise the chat's in-flight slot belongs to a newer autonomous reply, which answers a burst that includes this message, so no card is made either. In both cases write the step 7 audit row with `to_action_id` = the reused id and `conversion = "repropose"` (so step 2 and the reconciler stop here) and return that id.
6. **`CARD`:** `propose(ProposalRequest(origin=AGENT_PROPOSAL, text=body, base_message_id, shown_through_message_id, delegation_id, run_id=None, tool_call_id=None, trigger_turn_id=None, draft_run_id, draft_flags={<the stored flags without identity_answer, disclosure_required, disclosure_added, reproposed_from>, "autonomy_codes": <failed codes of the stored execution decision, () for late>, "converted_from": action_id, plus "was_due_at": <the cancelled action's scheduled_for as epoch seconds> only when its `state_reason` is `late`, "presentation": "on_request"}), clock=…, config=actions_config)`: a **held reply** (OD-11). `was_due_at` is an integer (content-free) that lets the card say "was due at … (late)" (R80: M6-PLAN's one-line renderer touch point in `telegram/control/approvals.py`; this unit writes the key and does not edit the renderer). No model call; no disclosure line (PRD §11.4, the CHECK). The M04 proposal policy decides `awaiting_approval` or blocked copy-ready. **The card is not presented (OD-11).** For a new action, in this same transaction, `enqueue(kind=AUTONOMY_NOT_SENT, priority=HIGH, alert_class=AlertClass.SYSTEM (OD-11a: always at once), dedupe_key=f"notsent:{new_id}", refs=NotificationRefs(conversation_ids, contact_ids, delegation_ids, message_ids=(base_message_id,), action_ids=(new_id,), alert_kind=<the first failed autonomy code; "late" for a late conversion>), conversation_id, contact_id, scheduler=scheduler)` (P3-interfaces §11.2). A held reply and its alert therefore commit together, and the step 2 idempotency check (which finds the held reply by `converted_from`) also prevents a second alert; the dedupe key is a second guard. The owner's Draft reply press (M6-ESC) shows the card with no model call; until then M04's sync leaves it unshown (M6-PLAN's held-reply rule). `presenter` is used only to sync a card the new action superseded.
7. Audit `action_converted` (subject the cancelled action; `details = {"to_action_id", "conversion": "card" | "repropose", "reason": <its state_reason>}`; actor `system`).

`base_message_id` is set on every autonomous action when it is proposed, so the card normally satisfies `base_required`; if a purge cleared it since, the M04 proposal policy blocks the card, and nothing is sent. `delegation_id` reaches the card through `ProposalRequest.delegation_id` (F0-S stores it; contract gap CG-11 if it does not).

### 3.5 Bulk paths

Rule: **cancel the autonomous ones first, then run the M04 helper unchanged.** Autonomous rows are never in `awaiting_approval`/`revising`, and the M04 helpers move only rows still in their states, so after the cancel they find no autonomous row. Owner-approved and Assist actions keep their M04 outcome (`outdated`).

`lifecycle.cancel_autonomous_in_conversation(session, *, account_id: int, conversation_id: int, reason: StateReason, now: datetime) -> list[int]` = `_change_pending(conversation_ids=[conversation_id], states=PENDING, target=CANCELLED, reason=reason, now=now, origins=frozenset({ActionOrigin.DELEGATION_AUTO}))`. `PENDING` includes `proposed`, `approved` and `scheduled`; `executing` is never in it.

| Trigger | Where | Autonomous `approved`/`scheduled` | Then | Re-request |
|---|---|---|---|---|
| New `owner_manual` message (takeover) | `ingestion.py::on_message` | `cancelled/takeover` | `outdate_pending(…, TAKEOVER, include_approved=True)` as today | no |
| New contact message | same | `cancelled/new_contact_message` | `outdate_pending(…, NEW_CONTACT_MESSAGE, include_approved=False)` as today; the Assist ingestion trigger as today | **yes** |
| Owner edit with changed content | same | `cancelled/owner_edit` | `outdate_pending(…, OWNER_EDIT, include_approved=True)` | no |
| Contact edit of an action's base message | `ingestion.py::_outdate_based_on` | `cancelled/source_edited` (rows based on that message) | other origins as today (`outdated/source_edited`) | **yes** |
| Deletion of an action's base message | `ingestion.py::_outdate_based_on` | `cancelled/source_deleted` | other origins as today | no |
| Connection rights changed | `lifecycle.outdate_pending_for_account` | `cancelled/rights_changed` (whole account) | the existing outdate | no |
| `ai_managed` revoked | `lifecycle.cancel_pending_ai_generated` | origins become `{AGENT_PROPOSAL, DELEGATION_AUTO}` ⇒ `cancelled/consent_revoked` | — | no |
| Connection disabled; Ignore mode; data deletion; restore | `cancel_pending` (unchanged) | already `cancelled` with their reason | — | no |
| `/pause`, stop word, fast lane, Pause button, bot blocked | `cancel_autonomous_pending` (unchanged) | already `cancelled/autonomy_paused` | — | no |
| Delegation ended or superseded | `cancel_pending_for_delegations` (F0-S, frozen) | `cancelled/delegation_ended` | — | no |

`_outdate_based_on` selects `LIVE_CARD | {APPROVED, SCHEDULED}`; for origin `delegation_auto` it moves the row to `cancelled` with the same reason through `transition`; for every other origin it keeps today's move to `outdated` (a non-autonomous row is never `scheduled`).

`connections.py`, `telegram/control/handlers.py` (`handle_revoke`) and `actions/autonomy.py` need no edit: the helpers they call change.

**Re-request.** When the new-contact-message or base-edit path cancelled at least one autonomous action, set `conversations.pending_delegation_at = coalesce(pending_delegation_at, now)` for that chat (one account-scoped UPDATE; the listener already holds the conversation lock). Without it a reply cancelled during the delay could be lost when the new message does not itself trigger the delegated path (for example a "thanks" after a question). M6-PLAN's `unqueued_delegated` reconciler (or the triage hook) then defers `replies.delegated`; the job drafts for the whole burst. This is a second writer of `pending_delegation_at` (contract gap CG-4). Before M6-PLAN merges the flag only waits.

**`_change_pending` audit.** Add `Action.delegation_id` to its `RETURNING`; when it is not `NULL`, the audit `details` gain `"delegation_id"` (P3-interfaces §2.2: every autonomous cancel carries it). Backward compatible; `cancel_pending_for_delegations` (frozen) benefits without an edit.

**Not changed, on purpose:** `lifecycle.expire_due` may still end an autonomous `approved` action `expired` (window closed or max age) if Tx A has not claimed it by then; that is truthful and sends nothing (contract gap CG-15). `conversations/service.py::record_ingest_failure` is M6-TAKE's file; it calls `cancel_autonomous_in_conversation(reason=INGEST_ERROR)` there (M6-TAKE spec §3.6).

### 3.6 Reconcilers (`jobs/reconcile_actions.py`)

Both run inside `reconcile_pending`'s transaction (P3-interfaces §7.3) and are registered by `_wire_autonomy` always (a scheduled action is always settled; ending is never gated).

- **`unqueued_releases`** (contract): `state = 'scheduled' AND scheduled_for <= :now AND updated_at < :now - interval '10 seconds'` with no `procrastinate_jobs` row of task `actions.release_scheduled` for that `action_id` in `todo`/`doing`; `ORDER BY scheduled_for, id LIMIT 500`; returns `DeferRequest(RELEASE_TASK, {"action_id": id}, queueing_lock=release_queueing_lock(id))`. A cross-account system scan on `ix_actions_scheduled` (like `unqueued_executions`).
- **`unconverted_autonomous`** (spec addition, contract gap CG-5): `origin = 'delegation_auto' AND state = 'cancelled' AND updated_at` between `now − 24 h` and `now − 2 min`, `state_reason IN ('late', 'disclosure_missing', 'policy_denied', 'trigger_stale', 'volume_cap')`, `text IS NOT NULL`, `draft_flags->>'identity_answer' IS DISTINCT FROM 'true'` or (OD-10a) no `delegation_escalation` notification of the account under the action's `deleg:{delegation_id}:{conversation_id}:{base_message_id}` key, no action of the account with `draft_flags->>'converted_from'` or `->>'reproposed_from'` equal to its id, and no `action_converted` audit row for it (§3.4 step 2); at most 20 per run, by id. Without the last three filters a row that `convert_cancelled` deliberately leaves alone would be woken on every run for 24 h. For each, compute the conversion: `late` ⇒ `CARD`; otherwise `target = autonomous_execution_target(decision_from_record(policy_decision))`; `target is None` (a `release_no_target` cancel) or `target[1] is Conversion.NONE` ⇒ skip (a send precondition failed, or the row was never scheduled: no card, OD-11); else `target[1]`. The reconciler takes no row lock inside the reconcile transaction (other reconcilers there may hold delegation locks, and `convert_cancelled` locks a conversation, which would invert the §9.4 order): it only selects the ids and registers `db.uow.after_commit` work that runs `convert_cancelled(...)` for each, each in its own transaction, with `presenter=None` (a held reply is announced by its `autonomy_not_sent` alert, never presented here; OD-11). It returns no defers. The `updated_at` window keeps it away from conversions still running after commit; the 24 h bound keeps it from waking very old rows; the idempotency check (§3.4 step 2) makes a race with a late after-commit conversion harmless.

### 3.7 Job registration and wiring

- `jobs/tasks_release.py` (pre-created by F0-R; this unit owns it after CP1): `ReleaseJobHandlers` has one field, `release: ReleaseRunner | None` (keep F0-R's field name if it differs; contract gap CG-6), where `ReleaseRunner` is a Protocol with `async def run(self, action_id: int) -> str`. The task: `@app.task(name=RELEASE_TASK, queue=QUEUE_OUTBOUND, retry=RELEASE_RETRY)` with `RELEASE_RETRY = RetryStrategy(max_attempts=2, exponential_wait=2, retry_exceptions={OperationalError})`: only database errors before the release commits (after-commit work never raises, §3.3). Never `retry=True`.
- `_wire_autonomy` (when the `worker` or `system` role runs): `ReleaseJob(session_factory=ctx.session_factory, clock=ctx.clock, actions_config=ctx.actions_config, autonomy_config=ctx.autonomy_config, scheduler=ctx.scheduler, presenter=ctx.approval_cards)` (`P3WireContext.approval_cards`, closing CG-8; used only to sync a superseded card, OD-11) into `JobHandlers.release`; reconcilers `unqueued_releases` and `UnconvertedAutonomous(session_factory=…, clock=…, actions_config=…, autonomy_config=…, scheduler=…)` (the session factory opens each conversion's own transaction after the reconcile transaction commits). Function-local imports only.

### 3.8 Edge cases (each has a test in §5)

Two release jobs for one action; a release that runs before `scheduled_for`; a release after a 40-minute outage; a release exactly at `late_after`; `autonomy.send` switched off during the delay; the delegation ended, superseded or past `ends_at` during the delay; the account paused during the delay (bulk path and, if the bulk path lost the race, the re-check); a takeover, a new contact message, an owner edit, a contact edit or a deletion of the answered message during the delay; a rights change, a disabled connection or a consent revoke during the delay; an action already `executing` when any of these arrive (untouched); a conversion run twice; a crash between the cancel and its conversion; a re-proposal that is downgraded to a card; a second `disclosure_missing` on a re-proposal; a re-proposal whose chat slot is held by a newer in-flight autonomous action; a cancelled identity answer; a release whose decision has no execution target.

### 3.9 Failure modes (fail closed)

| Situation | Outcome |
|---|---|
| Facts missing (before M6-PLAN, or a builder raises `NotInAccountError`) | `cancelled/delegation_ended` or the reason of the first failed rule; no send |
| No covering delegation, or it is not the action's | `delegation_active` fails ⇒ `cancelled/delegation_ended`, no card |
| `late_after` missing (impossible by CHECK) | `cancelled/policy_denied`, no card |
| DENY with no execution target | `cancelled/policy_denied`, no card; never left `scheduled` |
| Identity answer cancelled | no card, no re-proposal; the identity question escalates instead (one `delegation_escalation` under the `deleg:` key, OD-10a) |
| Release defer lost | `unqueued_releases` |
| Execute defer lost after release | M04 `unqueued_executions` |
| Conversion lost after commit | `unconverted_autonomous` |
| Any exception before the release commits | rolled back; DB errors retried twice; nothing was released |

## 4. Files

**Owned (P3-interfaces §15.1):** `actions/release.py` (new); `actions/lifecycle.py` (bulk helpers; the event table and `cancel_pending_for_delegations` stay frozen); `actions/ingestion.py`; `jobs/tasks_release.py`; `jobs/reconcile_actions.py`.

**Named touch points:** `bootstrap.py::_wire_autonomy` (append lines only; Josh lead sequences with M6-PLAN, M6-DELEG, M6-LIFE, M7-EXP).

**Later editors of your file:** none. M6-PLAN fills the fact builders in `policy_loader.py` and does not edit `release.py::_decide` (§3.3.1, R79). M6-TXA imports `convert_cancelled` and never edits `release.py`.

**Never edit without an ICR:** `policy/*`, `actions/{executor,states,proposals,policy_loader,autonomy,ports,disclosure,config}.py`, `conversations/service.py`, `telegram/gateway.py`, `telegram/ingress/*`, migrations, `privacy/*`, `core/*`, `config.py`, `agent/{runtime,context,schemas}.py`, any prompt (P3-interfaces §15.2). `accounts/connections.py` and `telegram/control/handlers.py` need no edit.

## 5. Tests (red first)

Real PostgreSQL 18, `FakeClock`, `FakeTelegramSender`, a recording scheduler (`tests/integration/execution_env.py::RecordingScheduler`), F0-S's `tests/integration/p3_factories.py` (`seed_delegation`, `seed_autonomous_action(state="scheduled")`). All text synthetic. New files are this unit's own.

### 5.1 Named tests from IMPLEMENTATION_PLAN §8 and the RG list

| Source | Test |
|---|---|
| S06.3 #1 / RG-19 ext | `tests/integration/test_scheduled_bulk_paths.py::test_rg19_an_owner_manual_message_cancels_a_scheduled_send_within_the_delay_window` (through the real ingestion path; the action ends `cancelled/takeover`, zero sends after the release job runs) |
| S06.3 #2 / RG-19 ext | `…::test_rg19_pause_during_scheduled_cancels` (`/pause` through the fast lane and through the command; `cancelled/autonomy_paused`; release then finds it settled) |
| S06.3 #4 | `tests/integration/test_scheduled_release.py::test_an_overdue_scheduled_autonomous_send_after_a_40_minute_outage_is_cancelled_late_and_converted` (clock + 40 min; `cancelled/late`; a held reply with the same body, no disclosure, `presentation = "on_request"`, one `autonomy_not_sent` alert with `alert_kind = "late"` and no card presented (OD-11), `was_due_at` = the cancelled action's `scheduled_for` (epoch seconds), `converted_from` set; FakeLLM saw zero calls) |
| P3 plan (consent) | `test_scheduled_bulk_paths.py::test_consent_revoke_cancels_delegation_auto` (`/privacy revoke` ⇒ scheduled and approved autonomous ⇒ `cancelled/consent_revoked`; a dictated card stays) |
| RG-20 ext | `test_scheduled_release.py::test_rg20_a_scheduled_autonomous_action_is_never_reported_as_sent` (`STATE_LABELS[SCHEDULED] == "Scheduled"`; `/activity` groups it as awaiting, never sent; after release and before Tx A it is not "Sent" either) |

### 5.2 `tests/integration/test_scheduled_release.py`

- `test_schedule_autonomous_moves_proposed_to_scheduled_with_both_times` (`scheduled_for = now + 45 s`, `late_after = scheduled_for + 120 s`, `decided_at`, `approved_text_hash`)
- `test_schedule_autonomous_writes_auto_approved_and_scheduled_audits_with_ids_and_epochs`
- `test_schedule_autonomous_refuses_a_non_autonomous_or_non_proposed_action` (returns `False`, writes nothing)
- `test_release_moves_a_due_scheduled_action_to_approved_and_defers_execute` (`action_released` with `delegation_id`; one `actions.execute` defer, after commit)
- `test_release_before_scheduled_for_redefers_and_changes_nothing`
- `test_two_release_jobs_for_one_action_release_it_once` (two jobs run back to back and concurrently: one `action_released` audit row and exactly one `actions.execute` defer)
- `test_a_release_exactly_at_late_after_is_late`
- `test_release_locks_the_delegation_after_the_conversation` (a competing transaction holds the covering delegation row `FOR UPDATE`; `lock_waits` shows the release waiting on it while already holding the conversation; released, it proceeds; explicit barrier, no sleeps)
- `test_release_cancels_with_the_mapped_reason_for_each_failed_check` (parametrized. Baseline: `actions.send` and `autonomy.send` on and every fact passing, so exactly one fact fails per row and the first failed check in `RULES` order decides the reason, M6-POL §3.5. Facts made to fail through the database or settings: new contact message inserted without the listener ⇒ `new_contact_message`; base message older than 30 min ⇒ `trigger_stale`; `autonomy.send` off ⇒ `sending_disabled`; delegation ended ⇒ `delegation_ended`; delegation superseded by a newer one for the chat ⇒ `delegation_ended`; delegation past `ends_at` with no end fired ⇒ `delegation_ended`; account paused without the bulk path ⇒ `autonomy_paused`; window closed ⇒ `window_closed`; consent missing ⇒ `consent_revoked`; cap reached ⇒ `volume_cap` + card; a 24 h gap since the last disclosed send crossed during the delay ⇒ `disclosure_missing` + one re-proposal). Rows that need M6-PLAN's facts are `xfail(strict=True, reason="awaiting M6-PLAN")` until it merges
- `test_release_never_moves_a_scheduled_action_to_outdated_expired_or_blocked` (Hypothesis over failing facts, the `test_action_state_machine.py` pattern: every run ends the row `approved` or `cancelled` with a non-NULL reason, except `not_yet`; a decision with no execution target, injected through a stubbed `_decide`, ends `cancelled/policy_denied`)
- `test_release_with_only_the_inbox_pending_releases_and_tx_a_waits`
- `test_a_send_precondition_failure_at_release_makes_no_card` (takeover after the base message ⇒ `cancelled/takeover`, no new action)
- `test_a_card_conversion_proposes_the_same_body_without_disclosure_and_no_model_call`
- `test_a_card_conversion_enqueues_one_not_sent_alert_and_presents_no_card` (OD-11: one `autonomy_not_sent` row with `alert_class = system` (OD-11a), dedupe `notsent:{new_id}`, `action_ids = (new_id,)`, `alert_kind` = the first failed code; the fake presenter saw no `present_after_commit`; a second `convert_cancelled` and the reconciler add no row)
- `test_a_late_conversion_records_when_the_reply_was_due_and_a_non_late_one_does_not` (R80: `was_due_at` equals the cancelled action's `scheduled_for` in epoch seconds for `cancelled/late`; absent for `volume_cap`)
- `test_a_conversion_is_made_once_even_when_run_twice` (two concurrent `convert_cancelled` with an explicit barrier; one new action)
- `test_a_repropose_conversion_falls_back_to_a_card_when_the_proposal_is_refused`
- `test_a_second_disclosure_missing_becomes_a_card_never_a_second_reproposal` (seed a cancelled action whose flags carry `reproposed_from`)
- `test_a_cancelled_identity_answer_never_becomes_a_card` (late and `volume_cap` cancels of an action whose text is `identity_answer(locale)`: no new action, no `action_converted` row; one `delegation_escalation` (`fyi` when the stored category is `none`, `action_ids` = the cancelled action; OD-10a), none added by a second conversion, and once it exists `unconverted_autonomous` picks the action up on no later run; with the escalation missing, the reconciler enqueues it)
- `test_a_reproposal_that_meets_a_newer_in_flight_action_makes_nothing_more` (a newer `scheduled` autonomous action holds the chat's slot: no card, no second proposal; one `action_converted` row pointing at the newer action; a second `convert_cancelled` and the reconciler make nothing)
- `test_a_lost_conversion_is_recovered_by_the_reconciler` (cancel committed, conversion skipped; `unconverted_autonomous` makes the held reply and its one alert; a second run makes nothing)
- `test_the_conversion_reconciler_skips_send_precondition_cancellations`
- `test_a_lost_release_job_is_recreated_by_the_reconciler_and_releases_once`
- `test_the_release_reconciler_ignores_actions_not_yet_due`
- `test_release_after_commit_failures_never_retry_the_release` (scheduler scripted to raise; the action stays `approved`; `unqueued_executions` later defers it)

### 5.3 `tests/integration/test_scheduled_bulk_paths.py`

- `test_a_takeover_cancels_scheduled_and_autonomous_approved_and_outdates_the_rest` (an owner-approved card and an Assist card in the same chat end `outdated/takeover`, as in M04)
- `test_a_new_contact_message_cancels_a_scheduled_send_and_requests_a_new_delegated_reply` (`pending_delegation_at` set; the cancelled row frees `ux_actions_autonomous_inflight`)
- `test_a_contact_edit_of_the_answered_message_cancels_the_scheduled_send_and_requests_a_new_reply`
- `test_a_deleted_answered_message_cancels_the_scheduled_send`
- `test_an_owner_edit_cancels_a_scheduled_send`
- `test_a_rights_change_cancels_scheduled_and_autonomous_approved_actions` (owner-approved ones still `outdated/rights_changed`)
- `test_a_disabled_connection_cancels_scheduled_actions`
- `test_ignore_mode_data_deletion_and_restore_cancel_scheduled_actions` (parametrized over the three existing paths)
- `test_bulk_paths_never_touch_an_executing_autonomous_action` (parametrized over every row of §3.5)
- `test_autonomous_bulk_cancels_audit_the_delegation_id`
- `test_owner_approved_actions_keep_their_m04_outcomes` (regression for takeover, rights change, revoke)

### 5.4 Interleavings (`tests/integration/test_scheduled_races.py`)

Explicit `asyncio.Event` barriers and `pg_stat_activity` lock waits, the `test_action_execution_races.py` pattern; no sleeps:
- `test_a_takeover_committed_during_the_delay_cancels_before_the_release`
- `test_a_takeover_waiting_on_a_release_cancels_the_released_action_before_tx_a`
- `test_a_pause_waiting_on_a_release_never_lets_tx_a_claim_after_it`
- `test_a_delegation_end_waiting_on_a_release_cancels_the_released_action` (uses F0-S `end_delegations`)

The invariant asserted in each: either the competing change commits first and nothing is claimed, or the release commits first and the change then cancels the `approved` row before Tx A can claim it.

### 5.5 Other

- `tests/unit/test_release_codes.py::test_every_autonomous_execution_reason_is_a_cancel_reason` (each `execution_reason` in `AUTONOMY_CODES` and each M04 target reason maps to `cancelled`, never `outdated`, `expired` or `blocked`, for `delegation_auto`)
- `tests/integration/test_log_hygiene_m6_sched.py::test_release_and_bulk_paths_log_ids_only` (canary text in the action; no canary in logs; RG-10)
- Strict-xfail scenarios in `tests/scenarios/m06/` that this merge turns green: tell QJ1 (P3-interfaces §15.1 scenario rule).

### 5.6 Mutation table (Tier-1A; QA applies each, WORKFLOW §5)

| # | Rule, lock or guard added | Mutation | Named test expected to fail |
|---|---|---|---|
| 1 | Ingestion cancels autonomous rows on a takeover | remove the call | `test_rg19_an_owner_manual_message_cancels_a_scheduled_send_within_the_delay_window` |
| 2 | Ingestion cancels on a new contact message and re-requests | remove the cancel; then remove only the flag write | `test_a_new_contact_message_cancels_a_scheduled_send_and_requests_a_new_delegated_reply` |
| 3 | Autonomous first in `outdate_pending_for_account` | remove it | `test_a_rights_change_cancels_scheduled_and_autonomous_approved_actions` |
| 4 | `cancel_pending_ai_generated` covers `delegation_auto` | drop the origin | `test_consent_revoke_cancels_delegation_auto` |
| 5 | Base edit/deletion cancels autonomous rows | keep `outdated` for them | `test_a_contact_edit_of_the_answered_message_cancels_the_scheduled_send_and_requests_a_new_reply` |
| 6 | Late check before the decision | remove it | `test_an_overdue_scheduled_autonomous_send_after_a_40_minute_outage_is_cancelled_late_and_converted` |
| 7 | `now >= late_after` boundary | `>` | `test_a_release_exactly_at_late_after_is_late` |
| 8 | Release acts only on a `scheduled` row, and defers `actions.execute` only when the transition applied | drop the state check and defer whatever the transition returned | `test_two_release_jobs_for_one_action_release_it_once` |
| 9 | No release before `scheduled_for` | drop the check | `test_release_before_scheduled_for_redefers_and_changes_nothing` |
| 10 | Delegation row `FOR UPDATE` at release, after the conversation | remove `for_update=True` | `test_release_locks_the_delegation_after_the_conversation` (the end race test alone would not kill it: `end_delegations` also cancels a released `approved` row) |
| 11 | Autonomous targets are `cancelled` | use M04 `execution_target` | `test_release_never_moves_a_scheduled_action_to_outdated_expired_or_blocked` |
| 12 | Send preconditions never convert | convert every cancel | `test_a_send_precondition_failure_at_release_makes_no_card` |
| 13 | Conversion idempotency check under the locks | remove the existence check | `test_a_conversion_is_made_once_even_when_run_twice` |
| 14 | At most one re-proposal | ignore `reproposed_from` | `test_a_second_disclosure_missing_becomes_a_card_never_a_second_reproposal` |
| 15 | `unqueued_releases` due filter | drop `scheduled_for <= now` | `test_the_release_reconciler_ignores_actions_not_yet_due` |
| 16 | Bulk paths exclude `executing` | add `executing` to the states | `test_bulk_paths_never_touch_an_executing_autonomous_action` |
| 17 | `ai_generated=True` in the release decision | `False` | the consent row of `test_release_cancels_with_the_mapped_reason_for_each_failed_check` |
| 18 | `delegation_id` in bulk audit rows | remove it | `test_autonomous_bulk_cancels_audit_the_delegation_id` |
| 19 | DENY with no execution target still cancels | leave the row `scheduled` and return `settled` | `test_release_never_moves_a_scheduled_action_to_outdated_expired_or_blocked` |
| 20 | Identity answers never convert | drop the identity check in `convert_cancelled` | `test_a_cancelled_identity_answer_never_becomes_a_card` |
| 21 | A reused re-proposal slot is recorded and ends the conversion | return the reused id without the audit row | `test_a_reproposal_that_meets_a_newer_in_flight_action_makes_nothing_more` |
| 22 | A late card records `was_due_at` | omit it | `test_a_late_conversion_records_when_the_reply_was_due_and_a_non_late_one_does_not` |

## 6. Acceptance criteria

1. Every test in §5 exists under its name and passes in CI; the M6-PLAN-dependent rows are strict xfail with the stated reason and turn green when M6-PLAN merges.
2. A `scheduled` action leaves only to `approved` (release) or `cancelled` with a reason; no code path moves a `delegation_auto` row to `outdated`, and no path touches an `executing` row.
3. Takeover, `/pause`, a rights change, a disabled connection and a consent revoke cancel `scheduled` and autonomous `approved` actions in the triggering transaction.
4. A release never runs a model or Telegram call and never holds a lock across one; every after-commit failure is logged by class name and recovered by a reconciler.
5. A conversion makes at most one held reply (with exactly one `autonomy_not_sent` alert and no presented card, OD-11) or one re-proposal per cancelled action, with no model call and no disclosure on the held reply; a cancelled identity answer makes neither and escalates instead (OD-10a); a converted, reused or skipped action is never woken again by the reconciler.
6. Every mutation in §5.6 is killed.
7. `ruff`, `ruff format --check`, `mypy` on `src/assistant/actions` and `src/assistant/jobs`, and `lint-imports` are clean; logs content-free.

## 7. Docs (same PR)

- ARCHITECTURE §9.4: the scheduled exits as built (the diagram's `scheduled → cancelled` reasons: takeover, new contact message, owner edit, source edited or deleted, pause, rights changed, connection disabled, consent revoked, delegation ended, late, every release re-check failure; `approved → cancelled` for autonomous origin); the release protocol and lock order; conversions.
- ARCHITECTURE §12.2: the `actions.release_scheduled` row (queue `outbound`, qlock `release:{action_id}`, `schedule_at = scheduled_for`, DB-only retry before commit) and the `reconcile_pending` row (now covers `approved` and `scheduled`, plus `unconverted_autonomous`; P3 conflict §12 #12).
- ARCHITECTURE §13: the release in the concurrency summary.
- ADR-20 (counters under the delegation `FOR UPDATE`, lock order account → conversation → delegation → action) is consumed as the TL writes it in F0-C part 2; the release follows the same order. Note any as-built difference in the PR description.
- PR description: the decisions marked "spec addition" here (ingestion early cancels and re-request, `unconverted_autonomous`), the mutation table, open items for M06.md.

## 8. Owner decisions

- **OD-6** (send delay 45 s, lateness 2 min): only the settings' defaults change; no code change.
- **OD-11** (answered 2026-10-29, changed from the default): a late send or an autonomy failure at release ⇒ a held reply with the same text and an `autonomy_not_sent` alert whose Draft reply button shows that text as a normal card with no new model call; a send-precondition failure ⇒ nothing: §3.4 and §3.6. The alert's renderer and the press are M6-ESC's; the sync rule is M6-PLAN's.
- **OD-10 × OD-11 (ruled, R40):** a cancelled identity answer makes no card (§3.4 step 3), because "the same text" is the AI identity sentence and a card carries no AI line (PRD §11.4; an owner-approved message never carries disclosure). Since OD-10a no escalation is enqueued at plan time for an identity answer, so the conversion enqueues it (§3.4 step 3); it is the owner's only signal, and its Draft reply button asks for a fresh owner-approved draft (M6-ESC, OD-11).
- **OD-31** (keep P2-T23: `/pause` cancels autonomous actions only): §3.5. If approvals should lapse on `/pause`, M6-PAUSE changes `cancel_autonomous_pending`; nothing here changes.
- **OD-32** (disclosure re-trigger reading) affects only the re-proposal reason, not this unit's code.

## 9. Risks and notes

- **Contract gaps this spec works around** (reported to the TL): CG-3 (fact builders needed before M6-PLAN: ruled R79, F0-S stubs, M6-PLAN fills them, `_decide` is written once here), CG-4 (re-request writes `pending_delegation_at`), CG-5 (`unconverted_autonomous`), CG-6 (handler field name), CG-8 (no presenter in `P3WireContext`: closed by OD-11, `P3WireContext.approval_cards`), CG-11 (`delegation_id` stored by `propose`), CG-15 (`expire_due` on autonomous `approved`). §2.2's exit table gains the ingestion exits of §3.5 (CG-4). Send-path review additions: CG-SP1 (P3-interfaces §3.6 did not cover a cancelled identity answer; this spec makes no card, ruled R40, the contract adopts it), CG-SP2 (P3-interfaces §13 `action_converted.to_action_id` does not say it may name a reused newer action rather than a new one; this spec uses it so for the reused re-proposal slot).
- **Lost reply during the delay** is the main product risk this unit closes with the re-request; QJ1's "takeover inside the 45 s delay" reproduction and QA-S6's J5 scenario both exercise it.
- **Lock order:** the release takes account `FOR SHARE` → conversation `FOR SHARE` → delegation `FOR UPDATE` → action `FOR UPDATE`, the same order as Tx A. Never lock the conversation after the delegation.
- **Hot file:** `actions/lifecycle.py` is shared with F0-S's frozen parts; edit only the bulk helpers and `_change_pending`.
