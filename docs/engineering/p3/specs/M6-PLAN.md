# M6-PLAN — autonomy planner and the delegated reply pipeline

| | |
|---|---|
| **Unit** | M6-PLAN ★ — side-effect-free `AutonomyPlanner`, `replies.delegated` job, `DelegationRequester`, `delegation_auto` propose path, identity answer |
| **Team / slot** | Josh / J3 |
| **Size** | M+ |
| **Depends on** | F0-S, F0-R, F0-E; M6-POL; **M6-SCHED** (R18, P3-T55: `schedule_autonomous`; stack on `p3/m6-sched` until it merges); DELEG-DRAFT (`FakeDelegatedDrafter` until it merges; it merges before this unit, P3-interfaces §15.5 step 5); GRND (F0-S's fail-closed stub until it merges) |
| **Branch → base** | `p3/m6-plan` → `int/p3` |
| **Peer reviewer** | E2 (Codex, dispatched by the TL; E2 builds the draft this unit consumes) |
| **QA** | QJ1 or QJ2, before merge, with the mutation table (§5.7) |
| **Tier** | 1A: Gemini read (David lead), TL deep review, part of CP-A; on the gate chain (EV-AUT calls `plan`) |
| **Normative inputs** | ARCHITECTURE §7.3, §9.2–§9.4, §12.1–§12.2; P3-interfaces §2.3–§2.5, §3.1–§3.6, §4.1–§4.3, §4.5, §5, §6, §7.1, §7.3, §10, §11, §13, §15; P3-schema §3.1, §3.3; PRD J5, §11.3–§11.6, §12.3, §15, FR-AUT-03/04/07/08/11/12/13; IMPLEMENTATION_PLAN §8 S06.2, §10 (RG-13, RG-17); decisions P3-T19, P3-T25, P3-T47, P3-T51, P3-T55; README §3 row M6-PLAN |

## 1. Goal

For a burst in a chat covered by an active delegation, draft a reply in delegated mode, compute every fact the autonomy rules need, insert the AI disclosure or the fixed identity answer, and let the policy engine decide (invariant 1). `ALLOW_AUTO` becomes a `delegation_auto` action that M6-SCHED schedules; anything else becomes a held reply (the stored text, no disclosure, shown to the owner as an `autonomy_not_sent` alert with a Draft reply button that opens the normal approval card, OD-11), an escalation, or nothing. The planner itself has no side effects, so the S06.5 eval (EV-AUT) runs the exact production logic.

## 2. Scope

**In:**
1. `actions/policy_loader.py` (autonomy facts): the bodies of the three §3.5 builders that F0-S ships as fail-closed stubs (`delegation_snapshot`, `rate_counters`, `autonomy_facts`; R79), two read helpers (`burst_language`, `prior_autonomous_sends`) and one assembly function, `autonomous_context`, used by the planner, `propose` and Tx A (M6-TXA). M6-SCHED's release job builds the same facts by calling the three builders from its own `_decide`, so it needs no edit from this unit.
2. `actions/autonomy_planner.py` (new): `DelegatedAutonomyPlanner` (implements `AutonomyPlanner`), `DelegatedPathRequester` (implements `DelegationRequester`), `DelegatedReplyJob` (the `replies.delegated` body).
3. `actions/proposals.py`: the `delegation_auto` path (re-decision under the locks, idempotency through the two unique indexes, `delegation_id` stored, `ProposalRefused.decision`).
4. `jobs/tasks_delegation.py` (registration body); `jobs/reconcile_delegation.py` (`unqueued_delegated` only).
5. Lines in `_wire_autonomy`, including the startup refusal and instantiating DELEG-DRAFT's `DelegatedDraftRunner`.
6. Two named touch points in `telegram/control/approvals.py`: the autonomy line of a held reply's card (§3.8, R80) and the held-reply rule in `sync` and `sync_cards` (§3.8, OD-11). **No** edit to `actions/release.py::_decide` (M6-SCHED writes it once against the builders, R79).
7. The held reply's `autonomy_not_sent` alert row (enqueue only; M6-ESC renders it and handles the Draft reply press) (OD-11).

**Out:** the rules and outcome mapping (M6-POL); the delegated prompt, schema and context builder (DELEG-DRAFT); scheduling, release and conversions (M6-SCHED); Tx A (M6-TXA); takeover counting (M6-TAKE adds one argument to `autonomous_context`); covered-chat triage effects, `decide_delivery` and the escalation renderer (M6-ESC; this unit only produces the rows); delegation creation and lifecycle (M6-DELEG, M6-LIFE); the eval target (EV-AUT).

## 3. Design

### 3.1 Contracts consumed and provided

| Consumes | From |
|---|---|
| `AutonomyPlanner`, `AutonomyPlan`, `PlanOutcome`, `DelegatedDrafter`, `DelegatedDraft`, `DelegatedDraftStatus` | F0-S, P3-interfaces §4.1–§4.2 |
| `PolicyContext.delegation/counters/autonomy`, `DelegationSnapshot`, `RateCounters`, `AutonomyFacts` | F0-S, §3.1 |
| Stub bodies of `policy_loader.delegation_snapshot`, `rate_counters`, `autonomy_facts` (each returns `None`, so every autonomy rule fails closed) | F0-S, §3.5; this unit replaces them (R79) |
| `decide`, the ten `AUTONOMY_RULES`, `AUTONOMY_CODES`, `EscalationCategory`, `Behaviour` | M6-POL / F0-S, §3.2–§3.4 |
| `active_covering`, `DelegationView`; `shareable_for` | F0-S, §6 |
| `disclosure.compose/split_disclosure/includes_disclosure/disclosure_required/identity_answer`, `PriorAutonomousSend`; `GateLanguage`, `AI_MARKER` | F0-S, §5 |
| `grounding.ground`; `language_keys.text_key/contact_key/declared_key` | F0-S stubs, GRND bodies, §3.7 |
| `propose(..., trigger=None, autonomy=None)`, `ProposalRequest.delegation_id` | F0-S, §4.3, §2.3 |
| `release.schedule_autonomous`, `defer_release_after_commit` | M6-SCHED |
| `DELEGATED_TASK`, `delegated_queueing_lock`; `AutonomyConfig`; flags `autonomy.run`, `autonomy.send` | F0-S, §7.1, §10 |
| `DelegationRequester`, `P3Ports`; `JobHandlers.delegation`; the triage hook | F0-R, §4.5, §7.2 |
| `NotificationKind.DELEGATION_ESCALATION`, `AUTONOMY_VOLUME_CAP`, `AUTONOMY_NOT_SENT` (OD-11), `NotificationRefs.delegation_ids`; `inbox.notifications.enqueue` | F0-S / M05, §11 |
| `agent/delegated_draft.py::DelegatedDraftRunner(*, session_factory, llm, clock, config: DraftConfig)` | DELEG-DRAFT, §4.2 |

| Provides | To |
|---|---|
| `DelegatedAutonomyPlanner.plan(*, account_id, conversation_id, now) -> AutonomyPlan` | the job; EV-AUT |
| `policy_loader.autonomous_context(...)` and the §3.5 builders (real bodies) | `propose`, M6-TXA's Tx A, M6-TAKE (one argument); M6-SCHED's release job calls the builders directly |
| `AutonomyPlan.escalates` (R87) | the `replies.delegated` job; EV-AUT |
| The autonomy line on a held reply's card (`telegram/control/approvals.py`, R80) and the held-reply sync rule (OD-11) | the owner; M6-SCHED and M6-TXA conversions reuse both; M6-ESC's Draft reply press relies on the sync rule |
| `propose()` for origin `delegation_auto` | the job, M6-SCHED's re-proposal |
| `DelegatedPathRequester` | F0-R's triage hook, then M6-ESC |
| Task `replies.delegated`; reconciler `unqueued_delegated` | the worker |
| Rows: `delegation_auto` actions, held replies, `delegation_escalation`, `autonomy_volume_cap` and `autonomy_not_sent` notifications, audit `autonomy_decision` | M6-SCHED, M6-ESC, STAT-ACT, M6-CATCH, OPS-TV3 |

### 3.2 Facts (`actions/policy_loader.py`)

The contract signatures (P3-interfaces §3.5) are kept exactly. All reads are account-scoped and run under the caller's locks; none writes.

**Stubs to bodies (R79).** F0-S created `actions/policy_loader.py`'s three builders as stubs with the contract parameter lists whose bodies return `None` (declared `-> DelegationSnapshot | None`, `-> RateCounters | None`, `-> AutonomyFacts | None`, so `PolicyContext` keeps `None` and every autonomy rule fails closed). This unit replaces the three bodies and narrows the return annotations to the contract's non-optional types; callers written against the stub (M6-SCHED's `_decide`) handle `None` and keep working. This unit does not touch `actions/release.py`.

**`delegation_snapshot(delegation, *, conversation_id, contact_id, config) -> DelegationSnapshot`** (pure): copy the view's fields; `covers_conversation = conversation_id in delegation.conversation_ids`; `topic_scoped = rules.topic is not None`; `contact_listed = contact_id in rules.knowledge_contact_ids`; `allowed_behaviours = frozenset(rules.allowed)`; the two limits from `rules.limits` (M6-DELEG enforces "never above the setting unless raised on the card").

**`rate_counters(session, *, account_id, conversation_id, delegation_id, now, config) -> RateCounters`** (ADR-20: derived from `actions`, no counter column; at Tx A the delegation row is locked):
- `conversation_last_hour`: `delegation_auto` actions of this chat (any delegation) with `attempt_started_at > now − 1 h`.
- `delegation_total`: `delegation_auto` actions with this `delegation_id` and `attempt_started_at >= delegations.starts_at`.
- Every claimed attempt counts, whatever its outcome (`delivery_uncertain` and `failed` count). An unclaimed action has `attempt_started_at` NULL and does not count; a 429 return clears it (nothing was sent).
- `rapid_replies`: `assistant_sent` messages of this chat that belong to `delegation_auto` actions (`messages.action_id`) with `sent_at >= now − loop_window`, each followed by a contact message with `sent_at` in `(that sent_at, that sent_at + loop_answer]`.
- `loop_reply_threshold = config.loop_replies`.
- `repeated_contact_message`: the base message's `content_hash` equals the `content_hash` of another contact message of this chat with `sent_at >= now − loop_window`; NULL hashes never match. Hashes are compared in SQL; no text reaches policy.

**`burst_language(session, *, account_id, conversation_id, base_message_id) -> GateLanguage | None`** (new helper): the answered burst is the contact messages of this chat with `id <= base_message_id` and `id >` the newest `owner_manual` or `assistant_sent` message id below `base_message_id` (0 if none), newest 10 by id; returns `language_keys.contact_key([(lang_guess, script), …])`. `base_message_id` NULL ⇒ `None`.

**`prior_autonomous_sends(session, *, account_id, conversation_id) -> list[PriorAutonomousSend]`** (new helper): every `delegation_auto` action of this chat that reached a send attempt (`succeeded`, `delivery_uncertain`, `failed`), with `sent_at = executed_at` for `succeeded`, else `None`, and its `disclosure_included`. `disclosure_required` itself ignores all but `succeeded` (P3-interfaces §5.1).

**`autonomy_facts(session, *, account_id, conversation_id, delegation, text, draft_flags, base_message_id, now, config) -> AutonomyFacts`**:

| Field | Value |
|---|---|
| model flags | from `draft_flags`; a missing or wrongly typed key reads as its failing value: `confidence` "low", `uses_only_permitted_information` False, `sensitive_topic` True, `makes_commitment` True, `should_escalate` True, `in_scope` False, `behaviour` "none", `identity_question` False, `escalation_category` "needs_owner" |
| `identity_answer` | `text == disclosure.identity_answer(lang)` for some `lang` in `GateLanguage` (exact string compare). Never read from a flag: a flag claiming it on model text is ignored |
| `trigger_message_sent_at` | `messages.sent_at` of `base_message_id` in this chat; `None` when missing, deleted or purged |
| `max_trigger_age` | `config.max_message_age` |
| `ungrounded_values` | 0 when `identity_answer`; else `grounding.ground(body, sources=[item.text for item in shareable_for(...)] + ([rules.topic] if rules.topic else [])).ungrounded`, with `body = disclosure.split_disclosure(text)[1]`. Contact messages are never sources |
| `knowledge_ids_used` | `frozenset(draft_flags["knowledge_ids"])` (empty when `identity_answer`) |
| `knowledge_ids_allowed` | the ids `shareable_for(session, account_id=…, delegation=delegation, conversation_id=…)` returns |
| `contact_language` | `burst_language(...)` |
| `draft_language` | `language_keys.text_key(body)` |
| `declared_language` | `language_keys.declared_key(draft_flags.get("language", ""))` |
| language keys when `identity_answer` | all three = the identity sentence's locale (§3.4 of the contract) |
| `enabled_languages` | `config.languages` |
| `disclosure_required` | `disclosure.disclosure_required(prior_autonomous_sends(...), session_start=delegation.starts_at, now=now)` |
| `disclosure_included` | `disclosure.includes_disclosure(text)` |

**`autonomous_context(...) -> PolicyContext`** (new; the one assembly function):

```python
async def autonomous_context(
    session: AsyncSession, *, account_id: int, conversation_id: int,
    delegation: DelegationView | None, facts: ActionFacts, text: str,
    draft_flags: Mapping[str, Any], base_message_id: int | None, phase: Phase, now: datetime,
    actions_config: ActionsConfig, autonomy_config: AutonomyConfig, process_run_id: str | None,
) -> PolicyContext:
```

1. `ctx = await load_context(session, …, facts=facts, base_message_id=base_message_id, target_message_id=None, owner_requested=False, phase=phase, now=now, config=actions_config, ai_generated=True, process_run_id=process_run_id)`. `ai_generated=True` makes `consent_valid` apply to every autonomous text (invariant 9).
2. `sending_enabled = actions_config.sending_enabled and autonomy_config.send_enabled` (P3-interfaces §3.1: turning `autonomy.send` off stops every pending autonomous send).
3. `delegation is None` ⇒ return `replace(ctx, sending_enabled=…)` with the three autonomy fields `None` (fail closed: `delegation_active` fails first).
4. Else read `conversations.contact_id` (account-scoped) and return `replace(ctx, sending_enabled=…, delegation=delegation_snapshot(...), counters=await rate_counters(...), autonomy=await autonomy_facts(...))`. `base_message_id is None` (the frozen `autonomy_facts` signature takes an `int`) ⇒ `autonomy=None`, so every rule that reads the facts fails closed.

`autonomous_context` is the only place that calls `load_context` for `delegation_auto`; M6-TAKE adds its takeover reference there (one argument, Josh lead sequences). If M6-TAKE merged first, its `load_context(takeover_reference=…)` keyword already exists: pass `takeover_reference=delegation.confirmed_at` yourself (M6-TAKE spec §3.3). It serves both phases; the execution-phase tests are M6-TXA's (the contract's "execution half").

### 3.3 The planner (`DelegatedAutonomyPlanner`)

`DelegatedAutonomyPlanner(*, session_factory, drafter: DelegatedDrafter, actions_config, autonomy_config)`. `plan(*, account_id, conversation_id, now)`:

1. **Read** (a short session, no locks, rolled back at the end): the conversation of this account with its contact's `is_bot` and `mode`. Not found, a bot chat or Ignore mode ⇒ `NOT_DRAFTED` (`draft_status = NOT_DRAFTABLE`, `delegation_id = None`). `delegation = active_covering(...)`; `None` ⇒ the same.
2. **Draft** with no session open: `draft = await drafter.draft(account_id=…, conversation_id=…, delegation_id=delegation.delegation_id, now=now)`. `NotInAccountError` ⇒ `NOT_DRAFTED`.
3. **Status:** `NOT_NEEDED` ⇒ `NO_REPLY` (keep the draft's `escalation_category`). Any other status but `DRAFTED` ⇒ `NOT_DRAFTED`; `final_text`, `body` and `decision` stay `None`. For `INVALID`, `REFUSED` and `FAILED` the plan's `escalation_category` is `needs_owner` (invalid or refused output is an escalation, PRD §15); for `NOT_DRAFTABLE`, `NO_CONSENT` and `OVER_BUDGET` it is `none` (M6-ESC's `system.delegation_health` reports outages). A non-drafted result never reaches `propose` (named test).
4. **Final text** (a second short session):
   - `locale = burst_language(...)`.
   - If `draft.identity_question` and `locale in autonomy_config.languages`: `final_text = disclosure.identity_answer(locale)`, `identity_answer = True`, `disclosure_added = False` (OD-10; the sentence carries `AI_MARKER`, so it counts as disclosed).
   - Else `required = disclosure_required(prior_autonomous_sends(...), session_start=delegation.starts_at, now=now)`. If `required` and `locale` is not `None`: `final_text = compose(draft.body, locale=locale, wording=delegation.rules.disclosure.get(locale.value))`, `disclosure_added = True`. Otherwise `final_text = draft.body` (when `required` and `locale` is `None`, `disclosure_present` and `language_gate` fail and the reply is kept as a held reply, OD-11).
   - `draft_flags = _draft_flags(draft, identity_answer=…, disclosure_required=…, disclosure_added=…)`: the P3-interfaces §2.4 keys `language`, `confidence`, `uses_only_permitted_information`, `sensitive_topic`, `makes_commitment`, `should_escalate`, `in_scope`, `behaviour`, `identity_question`, `escalation_category`, `knowledge_ids` (the model's `knowledge_ids_used`, as given), `identity_answer`, `disclosure_required`, `disclosure_added`. Ids, enums and bools only.
   - `facts = ActionFacts(action_id=None, kind=SEND_MESSAGE, origin=DELEGATION_AUTO, state=PROPOSED, version=1, text_length=sendable_length(final_text), created_at=now, expires_at=None, shown_through_message_id=draft.shown_through_message_id, owner_retry=False)`.
   - `decision = decide(await autonomous_context(..., phase=Phase.PROPOSAL, process_run_id=None))`.
5. **Outcome:** `escalation_category == "suspicious"` ⇒ `ESCALATE` (do not comply: no reply and no card for an attempt to instruct the assistant, PRD §11.5; contract gap CG-13). Otherwise `ALLOW_AUTO`, `REQUIRE_APPROVAL` or `DENY` from `decision.outcome`. `reason_codes` = the failed checks' codes in `RULES` order (`()` for `ALLOW_AUTO`).
6. **Escalation category:** the draft's. When `identity_question` is set, its sentence is not sent (no identity answer, because the language is not enabled, or an identity answer whose outcome is not `ALLOW_AUTO`) and the category is `none`, use `fyi`: the identity question escalates instead of the sentence (P3-interfaces §4.1, §5.1, R14, OD-10). An identity answer on an `ALLOW_AUTO` plan keeps the draft's category and adds no escalation of its own (OD-10a, decisions §A2); if the job then holds it (shadow mode or a downgrade under the locks), the held reply's `autonomy_not_sent` alert tells the owner. This sets only `AutonomyPlan.escalation_category` (the notification); `draft_flags["escalation_category"]` keeps the model's value, so the application never changes what `draft_safe` reads.
7. **`escalates` (R87):** `AutonomyPlan.escalates: bool` is computed once, here, by one private function `_escalates(plan_category, outcome, draft_status)`: true exactly when `escalation_category` (after steps 3 and 6) is not `none`. That covers every drafted outcome with a non-`none` category, an identity question whose sentence is not sent (category `fyi` at least; OD-10a) and a `NOT_DRAFTED` plan whose draft status is `invalid`, `refused` or `failed` (category `needs_owner`). The `replies.delegated` job (§3.6 Apply step 3) and EV-AUT both read `plan.escalates` and never recompute the rule, so the production job and the eval cannot drift. The contract's `AutonomyPlan` gains this field (P3-interfaces §4.1; F0-S ships it as a required field with no default, so a hand-built plan must say it).

The returned `AutonomyPlan` fills every field (§4.1 of the contract, plus `escalates`): `body` = the model's body only (what a card shows), `draft_facts` = the `AutonomyFacts` `decide` saw.

**No side effects.** `plan` writes nothing except what the drafter meters (`agent_runs`, `llm_calls`): no action, notification, job, audit row or Telegram call. Expected outcomes are values; infrastructure errors propagate.

### 3.4 `propose()` for `delegation_auto` (`actions/proposals.py`)

Changes, in order inside `propose`:
1. Origin `delegation_auto`: `request.delegation_id is None` ⇒ `ProposalRefused("delegation_required")`; `autonomy is None` ⇒ `ProposalRefused("autonomous_not_available")` (F0-S). The M04 blanket refusal goes. Any `trigger` keeps F0-S's `trigger_not_available`.
2. Locks: account `FOR SHARE`, conversation `FOR UPDATE` (as today), then `covering = await active_covering(session, …, now=now, for_update=True)` (the §9.4 order account → conversation → delegation). `covering` counts only when its id equals `request.delegation_id`; otherwise it is `None`.
3. `decision = decide(await autonomous_context(..., delegation=covering, facts=<as today>, text=request.text, draft_flags=request.draft_flags or {}, base_message_id=request.base_message_id, phase=PROPOSAL, process_run_id=None))`.
4. `decision.outcome is not ALLOW_AUTO` ⇒ `raise ProposalRefused("autonomy_downgraded", decision=decision)`; nothing is inserted. `ProposalRefused.__init__(self, code: str, decision: Decision | None = None)` gains the optional attribute (backward compatible; contract gap CG-14), so the caller's held reply records the codes of this re-decision.
5. Insert as `proposed` with `origin = delegation_auto`, `delegation_id`, `draft_flags`, the proposal-phase `policy_decision`, `expires_at` (the one expiry rule), `run_id = tool_call_id = trigger_turn_id = None`. Use `ON CONFLICT DO NOTHING` with no conflict target for this origin, so a violation of `ux_actions_autonomous_inflight` or `ux_actions_delegation_base` returns no row.
6. No row ⇒ look up the conflicting action (the chat's in-flight `delegation_auto`, else the same `(delegation_id, base_message_id)` row outside `disclosure_missing`) and return `_reused(it)`: the idempotent "already proposed" outcome, never an error. Neither found ⇒ `RuntimeError` (as today).
7. A new row ⇒ return `ProposalResult(action_id, PROPOSED, False, None, decision)`. No card, no `_supersede_live`: the caller schedules it (M6-SCHED's `schedule_autonomous`) in the same transaction.

For every origin, `_insert` now writes `delegation_id=request.delegation_id` (held replies carry it; contract gap CG-11 if F0-S did not). For origin `agent_proposal` nothing else changes.

### 3.5 The requester (`DelegatedPathRequester`, implements `DelegationRequester`)

`request(session, *, account_id, conversation_id, covers_through_message_id, now) -> bool`, inside the triage transaction (no commit, no model or Telegram call):
1. `autonomy_config.run_enabled` false ⇒ `False`.
2. The conversation of this account; missing, a bot chat or Ignore mode ⇒ `False`.
3. `active_covering(...)` `None` ⇒ `False`.
4. **Already owned:** an action of this chat with this `delegation_id`, `shown_through_message_id >= covers_through_message_id` and `state_reason` not in (`new_contact_message`, `source_edited`, `source_deleted`) ⇒ return `True` without a new flag (the delegated path already answered or is answering this burst).
5. `UPDATE conversations SET pending_delegation_at = coalesce(pending_delegation_at, :now) WHERE account_id = :a AND id = :c RETURNING id` (no row ⇒ `False`); register after-commit `scheduler.defer(DELEGATED_TASK, args={"conversation_id": c}, queueing_lock=delegated_queueing_lock(c), schedule_at=now + autonomy_config.debounce)`. Return `True`.

`True` means "an active delegation covers the chat and the delegated path is registered", exactly P3-interfaces §4.5. M6-ESC then skips the Assist trigger for the burst.

### 3.6 The job (`DelegatedReplyJob.run(conversation_id) -> str`)

`DelegatedReplyJob(*, session_factory, planner: AutonomyPlanner, scheduler, clock, actions_config, autonomy_config, model: str, presenter: ApprovalCardPresenter | None)`. It holds no lock across the model call and is safe to run twice. It returns a content-free code, also logged with the conversation id.

**Check** (one transaction; the conversation `FOR UPDATE`, joined to its contact and account):

| Condition | Code | Flag |
|---|---|---|
| conversation missing | `not_found` | — |
| `pending_delegation_at` NULL | `no_flag` | — |
| `autonomy.run` off | `autonomy_off` | cleared |
| account inactive, bot chat, Ignore mode | `account_inactive` / `bot_chat` / `ignore_mode` | cleared |
| no covering delegation | `no_delegation` | cleared |
| consent: `ai_control` and `ai_managed` active and both naming `processor_for_model(model)` (as `agent.drafting.draft_consent_ok`, re-implemented with `accounts.service.active_consent`; `actions` may not import `agent`) | `no_consent` | **kept** (the reconciler waits) |
| newest contact message `created_at + debounce > now` (trailing debounce) | `debounced` | kept; re-defer at that time |
| a `delegation_auto` action of this chat is `executing` | `inflight_wait` | kept; re-defer at `now + debounce` |
| "already owned" (§3.5 step 4, against the newest contact message id) | `nothing_new` | cleared |

A re-defer runs after commit (the job's own row is `doing`, so its queueing lock is free; a conflict means another job already waits).

**Plan:** `plan = await planner.plan(account_id=…, conversation_id=…, now=now)` with no transaction open.

**Apply** (one transaction; `now = clock.now()` is read again at its start, after the model call, so the send delay and `late_after` count from the proposal, never from the job's start):
1. Account `FOR SHARE`, conversation `FOR UPDATE`. A contact message newer than `plan.shown_through_message_id` ⇒ `stale`: no proposal, flag kept, re-defer at `now + debounce` after commit.
2. By outcome:

| Outcome | Effect |
|---|---|
| `ALLOW_AUTO`, `autonomy.send` on | `propose(origin=DELEGATION_AUTO, text=plan.final_text, base_message_id, shown_through_message_id, delegation_id, draft_run_id, draft_flags, autonomy=autonomy_config)`. New action ⇒ `schedule_autonomous(..., now=now)` must return `True` (else raise: a bug; the transaction rolls back and the flag stays), then `defer_release_after_commit(at=scheduled_for)`; code `scheduled`. Reused: when the reused action's `shown_through_message_id` is at least the plan's, it already answers this burst ⇒ `already_proposed`; when it is lower (an older reply still in flight, for example claimed between Check and Apply), the burst is not answered yet ⇒ code `inflight_wait`, flag kept, steps 3–5 skipped (the next run decides again), re-defer at `now + debounce` after commit. `ProposalRefused("autonomy_downgraded")` ⇒ held reply with `refused.decision`'s codes |
| `ALLOW_AUTO`, `autonomy.send` off (shadow) | held reply with `draft_flags["shadow"] = "allow_auto"` |
| `REQUIRE_APPROVAL`, `DENY` | held reply (the M04 proposal policy makes it `awaiting_approval`, or `blocked` with copy-ready text when a send precondition failed) |
| `ESCALATE` | nothing proposed |
| `NO_REPLY` | nothing proposed |
| `NOT_DRAFTED` | nothing proposed; `no_consent` keeps the flag |

   **Held reply (OD-11):** `propose(ProposalRequest(origin=AGENT_PROPOSAL, kind=SEND_MESSAGE, text=plan.body, base_message_id, shown_through_message_id, delegation_id, run_id=None, tool_call_id=None, trigger_turn_id=None, draft_run_id, draft_flags={<plan flags without identity_answer, disclosure_required, disclosure_added>, "autonomy_codes": [...], "shadow"?: "allow_auto", "presentation": "on_request"}), clock=…, config=actions_config)`. No disclosure line, ever (PRD §11.4; the CHECK). It survives `/pause` (origin `agent_proposal`, P2-T23, OD-31). The row, its state, expiry and retention are exactly those of the approval card the default answer showed. **The card is not presented.** Instead, for a new action (not a reused one), in the same transaction: `enqueue(kind=AUTONOMY_NOT_SENT, priority=HIGH, alert_class=AlertClass.SYSTEM (OD-11a: always at once), dedupe_key=f"notsent:{action_id}", refs=NotificationRefs(conversation_ids=(c,), contact_ids=(contact,), delegation_ids=(d,), message_ids=(base,), action_ids=(action_id,), alert_kind=<the first autonomy code; "shadow" for a shadow reply; "not_sent" when there is none>), conversation_id=c, contact_id=contact, scheduler=scheduler)` (P3-interfaces §11.2: class `system`, the timing of the card it replaces). A superseded older card is still synced after commit (`presenter.sync_after_commit`, when a presenter is wired: `P3WireContext.approval_cards`, which closes CG-8). The owner's Draft reply press (M6-ESC) shows this card with no model call; until then M04's sync leaves it unshown (§3.8).
3. **Escalation** when `plan.escalates` is true (R87; the planner's single predicate, §3.3 step 7: a category other than `none`, an identity question whose sentence is not sent (OD-10a), or a `NOT_DRAFTED` plan with draft status `invalid`, `refused` or `failed`, whose category is `needs_owner`; invalid or refused output becomes an escalation, PRD §15). The job never recomputes the rule: `enqueue(kind=DELEGATION_ESCALATION, priority=<urgent_decision → URGENT, needs_owner → HIGH, suspicious → NORMAL, fyi → LOW>, dedupe_key=f"deleg:{delegation_id}:{conversation_id}:{base_message_id or 0}", refs=NotificationRefs(conversation_ids, contact_ids, delegation_ids, message_ids=(base,), action_ids=(new id,) if any, alert_kind=<the escalation category value>), conversation_id, contact_id, bypass_quiet_hours=(category == urgent_decision and rules.escalation.urgent_bypass_quiet_hours), scheduler=scheduler)`. M6-ESC decides delivery and renders it. `refs.alert_kind` (existing `NotificationRefs.alert_kind`) is set here to the escalation category value (`urgent_decision`, `needs_owner`, `suspicious`, `fyi`), so M6-ESC and the renderer never parse the dedupe key (R60; P3-interfaces §11.2). M6-ESC is a second producer of `delegation_escalation` (the outage fallback) and uses the same dedupe-key format. `triage_urgent` and `triage_decision` notifications stay immediate in covered chats (M6-ESC). `over_budget` and `no_consent` enqueue nothing here (M6-ESC's `system.delegation_health` reports LLM or budget outages).
4. **Cap notice:** when `volume_conversation`, `volume_delegation` or `loop_suspected` is among the failed codes (the plan's or the re-decision's): `enqueue(kind=AUTONOMY_VOLUME_CAP, priority=HIGH, dedupe_key=f"cap:{delegation_id}:{conversation_id}:{hour_epoch}", …)` with `hour_epoch = int(now.timestamp()) // 3600 * 3600`.
5. **Audit** `autonomy_decision` (subject `conversation`, actor `system`): `delegation_id`, `outcome`, `codes`, `action_id` (or null), `shadow`, `identity_answer`, `escalation_category`. Content-free.
6. **Flag:** cleared under the conversation lock unless a contact message newer than `plan.shown_through_message_id` arrived (that burst's own job drafts it), and except for `stale`, `inflight_wait` and `no_consent`.

### 3.7 Reconciler (`jobs/reconcile_delegation.py::UnqueuedDelegated`, the pending part)

`UnqueuedDelegated(model: str)`, a `Reconciler`, registered by `_wire_autonomy` only while `autonomy.run` is on (P3-interfaces §7.3):
1. Clear stray flags: `pending_delegation_at < now − 30 s` on bot chats or Ignore chats ⇒ NULL.
2. Find `pending_delegation_at < now − 30 s`, not a bot chat, not Ignore, with no `procrastinate_jobs` row whose `queueing_lock = 'deleg:' || id` and `task_name = 'replies.delegated'` is `todo`/`doing`; `ORDER BY pending_delegation_at, id LIMIT 500`.
3. Skip accounts whose consent fails (`agent.drafting.draft_consent_ok(session, account_id, model)`; the jobs adapter may import `agent`); the flag waits.
4. Return `DeferRequest(DELEGATED_TASK, {"conversation_id": id}, queueing_lock=delegated_queueing_lock(id))`.

M6-LIFE adds `overdue_delegations` to the same module later, as a separate function.

### 3.8 Wiring, the held reply's card line and the held-reply sync rule

`_wire_autonomy` (function-local imports only):
- **Startup refusal, first line:** `autonomy.run` on without both `triage.run` and `triage.assist_trigger` ⇒ `raise ConfigError("autonomy.run needs triage.run and triage.assist_trigger")` (flag names only; `assistant.config.ConfigError`).
- Every role: `P3Ports.delegation_requester = DelegatedPathRequester(scheduler=ctx.scheduler, autonomy_config=ctx.autonomy_config)`.
- When `ctx.llm` is not `None`: `drafter = DelegatedDraftRunner(session_factory=ctx.session_factory, llm=ctx.llm, clock=ctx.clock, config=DraftConfig(model=settings.draft_delegated_model, effort=settings.llm_draft_reply_effort, monthly_budget_usd=settings.llm_monthly_budget_usd))` (R16, OD-9). The model always comes from the property `Settings.draft_delegated_model` that F0-S provides (R37), never from the raw field: it is the field when set, else the `draft_reply` route's model. `planner = DelegatedAutonomyPlanner(...)`; `JobHandlers.delegation = DelegationJobHandlers(delegated=DelegatedReplyJob(..., model=settings.draft_delegated_model, presenter=ctx.approval_cards))` (the presenter only syncs superseded cards; a held reply is never presented by the job, OD-11) (keep F0-R's field name if it differs; contract gap CG-6); reconciler `UnqueuedDelegated(model=…)` when `autonomy_config.run_enabled`.
- `jobs/tasks_delegation.py` (owned after CP1): the task `replies.delegated(conversation_id: int)`, queue `llm`, no lock, **no procrastinate retry** (a model call is not repeated blindly; the reconciler recovers from the flag), as `replies.draft`.

**`actions/release.py::_decide` is not edited here (R79).** M6-SCHED wrote it against the three builders; filling their bodies (§3.2) is what gives it real facts. Turn M6-SCHED's strict-xfail rows ("awaiting M6-PLAN") green in the same PR by removing their markers in a commit on this branch (WORKFLOW §5; the QA owner does it for the scenario rows).

**Held-reply sync rule (`telegram/control/approvals.py`, named touch point, OD-11).** A held reply must reach the owner only through its `autonomy_not_sent` alert until the owner presses Draft reply; M04's sync would otherwise present it as a lost card after `PRESENT_GRACE`. Add one module-level SQL predicate next to `_pending_presentation`, `_held_unrequested()`: `Action.draft_flags["presentation"].as_string() == "on_request"` and no `audit_log` row with `event = 'approval_card_requested'`, `subject_type = 'action'`, `subject_id = Action.id` and `account_id = Action.account_id` exists (tenant scoped on both rows, as `_pending_presentation`). Use it in exactly two places:
- `sync_cards`: `lost_cards` gains `~_held_unrequested()`. `stale_cards` is unchanged (an unshown card has no `card_message_id`, so it is never "stale").
- `sync(action_id)`: the "never shown" branch presents a live card only when the action is not held-unrequested (one account-scoped `EXISTS` read in `_prepare`'s transaction, carried on the view as `held: bool = False`, a new last field of `ApprovalCardView` with a default, so every existing constructor call is unchanged).
`present(action_id)` (the explicit call) is unchanged: M6-ESC's Draft reply press writes the `approval_card_requested` row in its own transaction and then calls `present_after_commit`, so a lost or failed presentation is recovered by the next `sync_cards`. An action without the key (every M04/M05 card, every Assist or owner-requested draft) renders and syncs exactly as today. Tests (`tests/integration/test_held_reply_presentation.py`): `test_a_held_reply_is_never_presented_by_the_sync_before_draft_reply`, `test_after_the_request_row_the_sync_presents_a_lost_held_reply`, `test_an_explicit_present_shows_a_held_reply`, `test_cards_without_the_presentation_key_are_presented_as_before` (the M04 lost-card test unchanged), `test_the_held_rule_is_tenant_scoped` (a request row of another account never releases this account's held reply).

**Held reply's card line (`telegram/control/approvals.py`, named touch point, R80).** Today a card shows only the M04 warnings, so the owner would not learn why the automatic reply was held. Extend `_warnings` (the existing list `_compose` already prints under the text) with up to two lines, built by one small helper next to it, for an `agent_proposal` card whose `draft_flags` carry autonomy keys (nothing is added for any other card; the owner's dictated words still carry none):
- **Explanation line.** When `draft_flags["autonomy_codes"]` is a non-empty list, one line "ℹ️ " + `AUTONOMY_CODES[code].explanation` for the first code that is a key of `AUTONOMY_CODES` (the list is in `RULES` order; an unknown code is skipped; none known ⇒ no line). Import `policy.codes.AUTONOMY_CODES` only (`telegram` may import `policy`); the text is a fixed sentence, never contact text, and is HTML-escaped like the other fixed lines.
- **Late note.** When `draft_flags["was_due_at"]` is an integer (written by M6-SCHED's `convert_cancelled` for a late conversion, so M6-TXA's late conversion reuses it), one line "⏱ This reply was due at HH:MM (late)." with the time in the owner's timezone (`_clock_time(datetime.fromtimestamp(value, UTC), view.timezone)`).
- Neither line changes the keyboard, the buttons or any state; both are skipped when the keys are missing or wrongly typed (a card from before P3 renders as it does today). The lines are part of the card's base text, so the existing `_fits`/`_fitted` logic (shorten the contact's words, then move the text to its own message) applies unchanged; the approve, edit and reject paths do not change.
- Tests: `tests/unit/test_approvals_autonomy_line.py`: `test_a_card_with_autonomy_codes_shows_the_first_known_explanation`, `test_a_late_converted_card_says_when_it_was_due`, `test_a_card_without_autonomy_keys_renders_as_before` (the M04 golden text is unchanged), `test_malformed_autonomy_keys_add_no_line`, `test_a_dictated_card_never_shows_the_autonomy_lines`, `test_a_card_with_the_added_lines_still_fits_one_gateway_chunk` (a long contact excerpt plus both lines).

### 3.9 Edge cases (each has a test in §5)

A burst that grows while the job waits (trailing debounce); a contact message during the model call (stale); a job run twice or a defer lost; an autonomous send still `executing`, or claimed between Check and Apply (the reused older in-flight action); a delegation ended, superseded or out of its window between plan and apply (re-decision ⇒ held reply); `/pause` between plan and apply (re-decision ⇒ held reply that survives the pause); a held reply is never presented before Draft reply, and exactly one `autonomy_not_sent` alert exists per held reply (OD-11); consent revoked between plan and apply; the cap reached between plan and apply; a second proposal for the same base message; the identity question in an enabled and in a disabled language; an identity flag on model text stored in `draft_flags`; required disclosure with an unknown chat language; a suspicious burst; `NOT_NEEDED` with an urgent decision; invalid, refused or failed drafts; a bot chat or an Ignore chat; a foreign account's conversation; shadow mode.

### 3.10 Failure modes (fail closed)

| Situation | Outcome |
|---|---|
| A fact cannot be computed (missing base, unknown language, stub grounding) | its rule fails ⇒ held reply, never `delegation_auto` |
| Re-decision under the locks is not `ALLOW_AUTO` | nothing inserted; held reply |
| The `autonomy_not_sent` enqueue fails | the whole Apply transaction rolls back (flag kept; the reconciler re-defers): a held reply never exists without its alert (OD-11) |
| Unique index violation | "already proposed" when the reused action covers the burst; otherwise flag kept and re-deferred (`inflight_wait`), never a lost reply |
| `schedule_autonomous` returns `False` | exception, rollback, flag kept |
| Draft not `DRAFTED` | never reaches `propose` |
| Job crashes after the model call | flag kept; the reconciler re-defers; the next run drafts again (metered) |
| Requester called with `autonomy.run` off | `False`; P2 behaviour unchanged |

## 4. Files

**Owned (P3-interfaces §15.1):** `actions/autonomy_planner.py` (new); `actions/policy_loader.py` (the autonomy-fact functions; Josh lead sequences M6-TXA, M6-TAKE and NUDGE-POL after you); `actions/proposals.py` (the `delegation_auto` path; NUDGE-POL follows); `jobs/tasks_delegation.py`; `jobs/reconcile_delegation.py` (`UnqueuedDelegated` only).

**Named touch points:** `bootstrap.py::_wire_autonomy` (append only); `telegram/control/approvals.py` (the autonomy line of a held reply's card, `_warnings` and one helper, R80; the held-reply rule: `_held_unrequested`, its use in `sync_cards` and `sync`, and the defaulted `ApprovalCardView.held` field, OD-11; §3.8 only). The three builder bodies in `policy_loader.py` replace F0-S's stubs (§3.2, R79); `release.py` is not touched.

**Do not edit:** `inbox/triage_store.py`, `inbox/notification_policy.py`, `inbox/notifications.py` (M6-ESC; you supply only the requester); `agent/*` and every prompt (DELEG-DRAFT); `policy/*` (M6-POL; `context.py`, `codes.py` frozen); `actions/{executor,lifecycle,ingestion,states,ports,disclosure,config,grounding,language_keys}.py`; `actions/release.py` (M6-SCHED's; its `_decide` already calls the builders, R79); `automation/delegations_repo.py`, `memory/shareable.py`; the rest of `telegram/control/approvals.py` beyond the named touch point; the never-edit list in P3-interfaces §15.2.

## 5. Tests (red first)

Real PostgreSQL 18, `FakeClock`, `FakeDelegatedDrafter` (scripted drafts) and, where named, DELEG-DRAFT's real runner with a scripted `FakeLLM`; F0-S factories (`seed_delegation`, `seed_memory_item(kind="shareable")`, `seed_autonomous_action`); `AUTONOMY_LANGUAGES` set per test. All text synthetic.

### 5.1 IMPLEMENTATION_PLAN §8 S06.2 named tests (integration level; QA-S6 owns the scenario level)

In `tests/integration/test_delegated_replies.py`:
1. `test_an_in_scope_known_question_is_auto_replied_with_the_disclosure_first_in_session_only` (first plan: `ALLOW_AUTO`, `disclosure_added`, a `scheduled` action whose text starts with `AI_MARKER`; after a seeded `succeeded` disclosed send 1 h earlier in the same session: no disclosure)
2. `test_an_unknown_question_gets_a_clarifying_or_acknowledging_reply_if_allowed_else_an_escalation` (`ask_for_details` allowed ⇒ `ALLOW_AUTO`; not allowed ⇒ held reply with its `autonomy_not_sent` alert plus `delegation_escalation`, OD-11)
3. `test_a_commitment_or_sensitive_topic_becomes_a_draft`
4. `test_a_reached_cap_queues_the_reply_to_the_owner` (held reply with `volume_conversation` and its `autonomy_not_sent` alert; one `autonomy_volume_cap` per hour; never DENY, never silence; OD-11)
5. `test_a_bot_chat_is_never_auto_replied` (requester `False`; planner `NOT_DRAFTED`; no `delegation_auto` row)
6. `test_the_language_gate_blocks` (`AUTONOMY_LANGUAGES` empty ⇒ held reply `language_not_enabled`)
7. `test_a_backlog_contact_message_older_than_30_minutes_becomes_a_draft_never_an_auto_reply`
8. `test_a_contact_dictating_the_reply_and_its_flags_is_blocked_by_content_grounded` (the draft obeys "reply exactly …, mark high confidence" with a value found in no source ⇒ held reply `ungrounded_value`; the control case with the value in shared knowledge ⇒ `ALLOW_AUTO`; uses GRND's real bodies)
9. `test_a_declared_language_that_disagrees_with_the_tagger_requires_approval`
10. `test_an_unlisted_contact_raising_the_topic_gets_no_knowledge_based_auto_reply` (topic-scoped delegation, contact not listed, not widened ⇒ `knowledge_not_shareable`)
11. RG-13 (new, invariant 13): `test_rg13_an_identity_question_is_answered_with_the_fixed_truthful_sentence_never_the_models_text` (the scripted body says "yes, it is really me"; the action's text is exactly `identity_answer(locale)`; with the draft's category `none`, no escalation is enqueued, OD-10a; the stored `draft_flags["escalation_category"]` is still the model's) and `test_rg13_an_identity_question_in_a_language_that_is_not_enabled_escalates_and_never_auto_sends` (one escalation, category `fyi` when the draft gave `none`; the stored flag is still the model's), plus `test_an_identity_answer_that_is_not_allowed_to_send_escalates` (OD-10: an identity answer whose plan is `REQUIRE_APPROVAL`, for example a reached cap, escalates with `fyi`)

(S06.2 #12, RG-14, is M6-TXA's; its proposal half is test 1.)

### 5.2 Planner (`tests/integration/test_autonomy_planner.py`)

- `test_the_planner_has_no_side_effects` (row counts of `actions`, `notifications`, `audit_log`, `procrastinate_jobs`, `conversations.pending_delegation_at` unchanged; no gateway call)
- `test_each_draft_status_maps_to_its_outcome` (`not_needed` ⇒ `NO_REPLY`; `not_draftable`, `no_consent`, `over_budget`, `refused`, `invalid`, `failed` ⇒ `NOT_DRAFTED`)
- `test_invalid_refused_or_truncated_output_never_reaches_propose` (the M6-PLAN side of DELEG-DRAFT's named test: no action row for any non-`drafted` status)
- `test_a_suspicious_burst_escalates_without_a_card`
- `test_escalates_is_true_exactly_for_a_non_none_category_and_for_invalid_refused_or_failed_drafts` (R87: `fyi` for an identity question whose sentence is not sent and `none` for an `ALLOW_AUTO` identity answer with a `none` draft (OD-10a), `needs_owner` for `invalid`/`refused`/`failed`, `none` for `no_consent`/`over_budget`/`not_draftable`/`not_needed` with a `none` draft; the job and a direct reading of `plan.escalates` agree)
- `test_an_identity_flag_in_draft_flags_never_passes_rules_for_model_text`
- `test_required_disclosure_with_an_unknown_chat_language_becomes_a_held_reply`
- `test_the_plan_reports_failed_codes_in_rules_order_and_the_facts_decide_saw`
- `test_a_foreign_accounts_conversation_is_not_drafted` (RG-21)
- `test_rg17_the_delegated_path_starts_no_owner_run_and_offers_no_tools` (real runner + `FakeLLM`: every request has no tools; no `owner_request` run; RG-17 extension)

### 5.3 Facts (`tests/integration/test_autonomy_facts.py`)

- `test_counters_count_every_claimed_attempt_whatever_its_outcome`
- `test_counters_never_count_another_chats_actions_except_the_delegation_total`
- `test_a_loop_is_suspected_after_three_quickly_answered_replies_or_a_repeated_message`
- `test_the_burst_language_is_none_when_the_burst_is_mixed`
- `test_missing_flags_read_as_their_failing_values`
- `test_contact_messages_are_never_grounding_sources`
- `test_autonomous_context_sets_sending_enabled_from_both_flags_and_marks_the_text_ai_generated`
- `test_a_delegation_that_does_not_cover_the_chat_leaves_the_facts_failing`
- `test_the_release_decision_and_autonomous_context_see_the_same_facts` (R79: for one scheduled action, the execution-phase context that M6-SCHED's `_decide` builds and `autonomous_context(phase=EXECUTION)` have equal `delegation`, `counters`, `autonomy` and `sending_enabled`)
- `test_the_f0_stub_bodies_are_replaced` (none of the three builders returns `None` for an active delegation; with no delegation `autonomous_context` still leaves all three `None`)

### 5.4 Proposals (`tests/integration/test_autonomous_proposals.py`)

- `test_propose_refuses_delegation_auto_without_a_delegation`
- `test_propose_re_decides_under_the_locks_and_inserts_nothing_when_downgraded` (pause committed between plan and propose ⇒ `autonomy_downgraded`, `refused.decision` has `autonomy_paused`)
- `test_a_second_proposal_for_the_same_base_message_is_already_proposed`
- `test_a_second_in_flight_autonomous_action_in_a_chat_is_never_created`
- `test_a_disclosure_missing_cancel_frees_the_base_message_for_one_reproposal`
- `test_propose_locks_the_delegation_after_the_conversation` (lock order: a concurrent `end_delegations` holding the delegation waits as expected; explicit barrier)
- `test_the_held_reply_carries_the_delegation_codes_and_presentation_and_no_disclosure` (OD-11: `draft_flags["presentation"] == "on_request"`)
- `test_a_held_reply_survives_pause`
- `test_agent_proposals_without_a_delegation_are_unchanged` (M04 regression)

### 5.5 Job, requester, reconciler, wiring (`tests/integration/test_delegated_reply_job.py`)

- `test_allow_auto_schedules_the_action_and_defers_the_release_after_commit` (the scripted drafter advances the fake clock 30 s: `scheduled_for` = the Apply time + 45 s, not the job start + 45 s)
- `test_a_reused_older_in_flight_action_keeps_the_flag_for_the_newer_burst` (an older autonomous action is claimed between Check and Apply: no new action, flag kept, one re-defer; after it settles the next run proposes for the newer burst)
- `test_shadow_mode_turns_allow_auto_into_a_held_reply_marked_shadow`
- `test_a_held_reply_enqueues_one_not_sent_alert_and_presents_no_card` (OD-11: one `autonomy_not_sent` row, `alert_class = system` (OD-11a), dedupe `notsent:{action_id}`, refs as §3.6, `alert_kind` = the first code; the fake presenter saw no `present_after_commit` for it; FakeChannel shows no approval card until M6-ESC's press; a second job run adds no row)
- `test_a_held_reply_and_its_alert_commit_together` (a forced failure in `enqueue` rolls back the held reply and keeps the flag)
- `test_the_job_debounces_a_burst_and_drafts_once`
- `test_a_contact_message_arriving_during_the_draft_discards_it_and_redrafts`
- `test_the_job_waits_while_an_autonomous_send_is_executing`
- `test_the_job_is_safe_to_run_twice_and_clears_its_flag`
- `test_no_consent_keeps_the_flag_and_makes_no_model_call` (RG-08 extension)
- `test_escalations_carry_the_category_priority_and_dedupe_key` (and `bypass_quiet_hours` only when ticked on the card, OD-7; `refs.alert_kind` equals the category value on an escalation and the first failed volume code on `autonomy_volume_cap`, R60)
- `test_invalid_or_refused_output_becomes_an_escalation_never_a_send`
- `test_the_requester_returns_true_only_when_a_delegation_covers_and_the_path_is_registered`
- `test_the_requester_does_nothing_with_autonomy_run_off`
- `test_the_requester_reports_an_already_answered_burst_without_a_new_flag`
- `test_a_lost_delegated_job_is_recreated_by_the_reconciler`
- `test_the_reconciler_clears_flags_on_bot_and_ignore_chats_and_waits_for_consent`
- `test_the_autonomy_decision_audit_is_content_free`
- `test_startup_refuses_autonomy_run_without_triage_and_the_assist_trigger`

### 5.6 Other

- `tests/unit/test_autonomy_planner.py`: `test_escalation_priority_per_category`, `test_draft_flags_hold_ids_enums_and_bools_only`, `test_the_identity_answer_is_recognised_only_by_exact_text`.
- The approvals autonomy line (R80) has its own file, `tests/unit/test_approvals_autonomy_line.py` (names in §3.8).
- `tests/integration/test_log_hygiene_m6_plan.py::test_the_delegated_path_logs_ids_and_codes_only` (canary text in the contact message, the draft body and knowledge; RG-10).
- Strict-xfail scenarios in `tests/scenarios/m06/` this merge turns green: tell QJ1.

### 5.7 Mutation table (Tier-1A)

| # | Rule, lock or guard added | Mutation | Named test expected to fail |
|---|---|---|---|
| 1 | `propose` re-decides under the locks | skip the re-decision | `test_propose_re_decides_under_the_locks_and_inserts_nothing_when_downgraded` |
| 2 | Delegation `FOR UPDATE` after the conversation in `propose` | remove `for_update=True` | `test_propose_locks_the_delegation_after_the_conversation` |
| 3 | Covering delegation must be the request's | drop the id comparison | `test_a_delegation_that_does_not_cover_the_chat_leaves_the_facts_failing` |
| 4 | `identity_answer` from exact text only | read the flag | `test_an_identity_flag_in_draft_flags_never_passes_rules_for_model_text` |
| 5 | Identity answer only in an enabled language | drop the check | `test_rg13_an_identity_question_in_a_language_that_is_not_enabled_escalates_and_never_auto_sends` |
| 6 | The final text is the identity sentence, never the body | keep the body | `test_rg13_an_identity_question_is_answered_with_the_fixed_truthful_sentence_never_the_models_text` |
| 7 | Unique violation ⇒ reused | let it raise | `test_a_second_proposal_for_the_same_base_message_is_already_proposed` |
| 8 | Non-`drafted` never proposed | propose any status with a body | `test_invalid_refused_or_truncated_output_never_reaches_propose` |
| 9 | Shadow when `autonomy.send` is off | ignore the flag | `test_shadow_mode_turns_allow_auto_into_a_held_reply_marked_shadow` |
| 10 | Held reply is `agent_proposal` with no disclosure | create it as `delegation_auto`; compose the text | `test_a_held_reply_survives_pause`; `test_the_held_reply_carries_the_delegation_codes_and_presentation_and_no_disclosure` |
| 11 | `ai_generated=True` for autonomous text | `False` | `test_autonomous_context_sets_sending_enabled_from_both_flags_and_marks_the_text_ai_generated` |
| 12 | `sending_enabled` needs both flags | `actions.send` only | same test |
| 13 | Missing flags read as failing values | default to passing values | `test_missing_flags_read_as_their_failing_values` |
| 14 | Contact messages never ground | add them as sources | `test_contact_messages_are_never_grounding_sources` |
| 15 | Counters count uncertain and failed attempts | count `succeeded` only | `test_counters_count_every_claimed_attempt_whatever_its_outcome` |
| 16 | Stale draft discarded | skip the freshness check | `test_a_contact_message_arriving_during_the_draft_discards_it_and_redrafts` |
| 17 | Wait while a send is `executing` | skip it | `test_the_job_waits_while_an_autonomous_send_is_executing` |
| 18 | Requester off with `autonomy.run` off | drop the check | `test_the_requester_does_nothing_with_autonomy_run_off` |
| 19 | Startup refusal | remove it | `test_startup_refuses_autonomy_run_without_triage_and_the_assist_trigger` |
| 20 | Planner writes nothing | write the audit row in `plan` | `test_the_planner_has_no_side_effects` |
| 21 | A reused older in-flight action keeps the flag | treat every reuse as `already_proposed` | `test_a_reused_older_in_flight_action_keeps_the_flag_for_the_newer_burst` |
| 22 | Scheduling uses the Apply-time `now` | pass the job's start time | `test_allow_auto_schedules_the_action_and_defers_the_release_after_commit` |
| 23 | Identity escalation defaults to `fyi` when the sentence is not sent, adds none when it is (OD-10a), and never rewrites the stored flag | write `needs_owner` into `draft_flags` / escalate an `ALLOW_AUTO` identity answer | `test_rg13_an_identity_question_in_a_language_that_is_not_enabled_escalates_and_never_auto_sends`, `test_rg13_an_identity_question_is_answered_with_the_fixed_truthful_sentence_never_the_models_text` |
| 24 | `escalates` is the one predicate | the job recomputes the rule / drop the failed-draft branch | `test_escalates_is_true_exactly_for_a_non_none_category_and_for_invalid_refused_or_failed_drafts` |
| 25 | The release decision and `autonomous_context` share the builders | make `autonomous_context` skip `rate_counters` | `test_the_release_decision_and_autonomous_context_see_the_same_facts` |
| 26 | A held reply's card shows the autonomy explanation and the late note | drop either line | `test_a_card_with_autonomy_codes_shows_the_first_known_explanation`; `test_a_late_converted_card_says_when_it_was_due` |
| 27 | A held reply is announced by its alert, not presented (OD-11) | call `present_after_commit` for it; skip the enqueue | `test_a_held_reply_enqueues_one_not_sent_alert_and_presents_no_card` |
| 28 | The sync never presents an unrequested held reply (OD-11) | drop `~_held_unrequested()` from `lost_cards` | `test_a_held_reply_is_never_presented_by_the_sync_before_draft_reply` |
| 29 | The held rule is tenant scoped (OD-11) | drop the `account_id` match on the request row | `test_the_held_rule_is_tenant_scoped` |

## 6. Acceptance criteria

1. Every test in §5 exists under its name and passes in CI; M6-SCHED's "awaiting M6-PLAN" rows are green with no edit to `actions/release.py`; the three F0-S builder stubs are replaced.
2. `delegation_auto` is created only by `propose()` called from `replies.delegated` (or M6-SCHED's re-proposal), with a stored `ALLOW_AUTO` proposal decision, `delegation_id` set and no tool call (invariant 2). No tool can create one.
3. An autonomy-only failure gives a held reply (`agent_proposal` with `delegation_id`, `autonomy_codes`, `presentation = "on_request"` and no disclosure) and one `autonomy_not_sent` alert, and its card is shown only after Draft reply (OD-11); a reached cap gives a held reply, never DENY or silence.
4. The planner has no side effects beyond the drafter's metering; EV-AUT can call it against a synthetic account and reads `plan.escalates` (the job reads the same field).5. Every mutation in §5.7 is killed.
6. `ruff`, format, `mypy` on `src/assistant/actions` and `src/assistant/jobs`, and `lint-imports` clean (`actions` imports no `agent`, `jobs` or `telegram`).
7. A held reply's card shows the first known autonomy explanation, and a late-converted card says when the reply was due; no other card changes, and every card without the `presentation` key is presented and synced as before.
8. An identity answer that is sent enqueues no escalation of its own; an identity question whose sentence is not sent escalates (OD-10a). Every `autonomy_not_sent` row is enqueued with `AlertClass.SYSTEM` (OD-11a).

## 7. Docs (same PR)

- ARCHITECTURE §9.4: the autonomous proposal path as built (requester → `replies.delegated` → planner → `propose` re-decision under account → conversation → delegation → `schedule_autonomous`; the held reply, its `autonomy_not_sent` alert and the held-reply sync rule (OD-11); idempotency through the two unique indexes; the identity answer).
- ARCHITECTURE §12.2: the `replies.delegated` row (queue `llm`, qlock `deleg:{conversation_id}`, `schedule_at = now + AUTONOMY_DEBOUNCE_SECONDS`, trailing debounce by re-defer, no lock, no retry, `pending_delegation_at` reconciler).
- ADR-17 (app-inserted disclosure) and ADR-21 (separate prompt key) are consumed as written by the TL in F0-C part 2; the PR description notes anything that differs as built.

## 8. Owner decisions

- **OD-4** (disclosure wording, fixed AI marker): `compose` and the per-delegation override; a different answer changes only the texts in `core/autonomy_texts.py`.
- **OD-6** (caps, loop guard, debounce): settings defaults only.
- **OD-7** (urgent-decision quiet-hours bypass per delegation, off by default): the `bypass_quiet_hours` argument in §3.6.
- **OD-9** (route and prompt key): the `DraftConfig` model (`Settings.draft_delegated_model`) in §3.8; a separate run type would change DELEG-DRAFT, not this unit's logic.
- **OD-10** (identity answer): §3.3 step 4 and step 6. "Always escalate instead" would drop the identity branch; RG-13 then asserts escalation only.
- **OD-10a** (answered 2026-10-29, changed from the default): no alert when the identity sentence is sent; the identity question escalates only when its sentence is not sent (§3.3 step 6, RG-13 tests).
- **OD-11a** (answered 2026-10-29): `autonomy_not_sent` is enqueued with `AlertClass.SYSTEM`, always at once (§3.6).
- **OD-11** (answered 2026-10-29, changed from the default): when autonomy does not send, the owner gets an alert with a Draft reply button, not the card; the stored text is reused with no model call when the owner presses it; no AI line; survives `/pause`: §3.6 (held reply and its alert) and §3.8 (sync rule). The alert's renderer and the press are M6-ESC's.
- **OD-14** (language gate; mixed ⇒ held reply): `burst_language` and the gate keys.
- **OD-32** (re-disclosure reading): `disclosure_required` is F0-S's; only its table changes.

## 9. Risks and notes

- **Contract gaps** (reported to the TL): CG-3 (the builders arrive after M6-SCHED: ruled R79, F0-S ships fail-closed stubs, this unit fills them, M6-SCHED's `_decide` is written once and not edited here), CG-6 (handler field names), CG-7 (the autonomy explanation on cards: ruled R80, this unit's one-line touch point in `telegram/control/approvals.py`, §3.8), CG-8 (no presenter in `P3WireContext`: closed by OD-11, `P3WireContext.approval_cards`), CG-11 (`delegation_id` stored by `propose`), CG-13 (`ESCALATE` defined here), CG-14 (`ProposalRefused.decision`), CG-17 (budget-threshold alerts for delegated drafts belong in DELEG-DRAFT's runner: `actions` cannot import `agent.budget`).
- **Consent processor:** `policy_loader._account` checks `ai_managed` against `actions_config.draft_route_model`. If `LLM_ROUTE_DRAFT_DELEGATED_MODEL` names a model of another processor, the check uses the wrong processor; today both are OpenAI. Raise it with the TL if the override is ever set (OD-22).
- **Clock comparison:** `trigger_fresh` compares server time with Telegram's `date`; skew is seconds against a 30-minute limit.
- **Cost:** a job that crashes after the model call drafts again on the next run (metered, within `LLM_MONTHLY_BUDGET_USD`).
- **Gate chain:** EV-AUT builds on `FakeAutonomyPlanner` until this merges; keep `plan`'s behaviour exactly as specified so the eval measures production logic.
