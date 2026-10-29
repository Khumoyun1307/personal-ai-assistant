# M6-TAKE — takeover scoped to the delegation session

| | |
|---|---|
| **Unit** | M6-TAKE ★ — session-scoped `takeover_at`, "Resume here", the one-time `takeover_paused` notice |
| **Team / slot** | Josh / J2 |
| **Size** | M |
| **Depends on** | F0-S, M6-SCHED (`cancel_autonomous_in_conversation`; the takeover bulk cancel). It also uses F0-R's `ControlRoutes` fragment, renderer registration and `_wire_autonomy` (merged at CP1). In `actions/policy_loader.py` it follows M6-PLAN and M6-TXA (Josh lead's order) and adds one argument to M6-PLAN's `autonomous_context`; if this unit merges before M6-PLAN, M6-PLAN passes that argument instead (§3.3) |
| **Branch → base** | `p3/m6-take` → `int/p3` |
| **Peer reviewer** | E1 (Codex, dispatched by the TL) |
| **QA** | QJ1 or QJ2, before merge, with the mutation table (§5.5) |
| **Tier** | 1A: Gemini read (David lead), TL deep review, part of CP-A |
| **Normative inputs** | ARCHITECTURE §6.5 step 4, §9.3 (`autonomy_not_paused`), §9.4 (lock order); P3-interfaces §3.1 (last bullet), §3.4, §4.5, §11.1–§11.3, §12.3, §12.5, §13, §15; P3-schema §3.3 (columns, data step, "no code writes `'takeover'` after 0015"); PRD R-09, FR-AUT-05, J8, J5; AGENTS §6 invariants 3b, 5, 7, 10, 11; IMPLEMENTATION_PLAN §8 S06.3, §10 (RG-19); decisions OD-5, P3-T20 (ADR-19); README §3 row M6-TAKE |

## 1. Goal

When the owner writes in a delegated chat, autonomy in that chat stops until the delegation ends or the owner taps **Resume here**, and the owner is told once. A takeover counts only inside the current delegation session: `takeover_at > max(delegation.confirmed_at, autonomy_resumed_at)` (OD-5, ADR-19). The takeover marker becomes its own column, separate from `ingest_error` (closes M03 D91), so a takeover from before the delegation never blocks it and one pause reason never hides the other. Drafts are still offered while paused.

## 2. Scope

**In:**
1. `conversations/service.py::record_message` step 4: writes `takeover_at` (never `autonomy_paused_reason`) and calls `note_takeover`; `record_ingest_failure`: one line cancelling autonomous actions first (contract gap CG-12).
2. `actions/autonomy.py`, new functions only: `takeover_counts` (pure), `note_takeover` (audit + notice), `resume_here` (card-only), `paused_by_takeover` (read helper for STAT-ACT and SET-P3).
3. `actions/policy_loader.py`: the takeover input to `ConversationSnapshot.autonomy_paused_reason` (one parameter through `load_context`, set by `autonomous_context`).
4. `telegram/control/takeover.py` (new): the `takeover_paused` renderer with the Resume here button, and the `tko` press handler.
5. Lines in `_wire_autonomy`: the renderer and the `tko` callback route.
6. Updates to existing tests that assert the old takeover column (§5.4).

**Out:** cancelling `scheduled` and autonomous `approved` actions on a takeover (M6-SCHED's ingestion path); the Pause button's press (M6-PAUSE; the builder is F0-S's `pause_button()`, which this unit only appends, §3.5); `/status` blocks (STAT-ACT, using `paused_by_takeover`); the panel's Delegate row (SET-P3); the account kill switch (`pause`/`resume`, unchanged).

## 3. Design

### 3.1 Contracts consumed and provided

| Consumes | From |
|---|---|
| `conversations.takeover_at`, `autonomy_resumed_at`; the 0015 data step; `LEGACY_TAKEOVER_REASON` kept in the CHECK for downgrade only | F0-S, P3-schema §3.3 |
| `active_covering`, `DelegationView` (`confirmed_at`, `starts_at`, `ends_at`, `kind`) | F0-S, P3-interfaces §6.1 |
| `NotificationKind.TAKEOVER_PAUSED` (standard class, priority high, dedupe `takeover:{delegation_id}:{conversation_id}:{resumed_epoch}`, R81), `NotificationRefs.delegation_ids`; `inbox.notifications.enqueue`; `NotificationDelivery.register_renderer`, `RenderedAlert.extra_buttons` | F0-S / M05, §11 |
| Callback kind `tko`, op `rh`; card `ref_id` = conversation id | F0-R routes, §12.3 |
| `accounts.service.create_card`, `claim_card`; `accounts.callbacks.encode`; `telegram.render.escape` (escape once, P2 #133) | M03–M05, P2 |
| `lifecycle.cancel_autonomous_in_conversation` | M6-SCHED |
| `policy_loader.autonomous_context` | M6-PLAN |
| `core/autonomy_texts.py::pause_button() -> Button`, `PAUSE_CALLBACK_DATA` | F0-S (R45, G1; on the base from CP1), P3-interfaces §12.5 |

| Provides | To |
|---|---|
| `takeover_counts(*, takeover_at, confirmed_at, resumed_at) -> bool` | the loader, `note_takeover`, `resume_here`, STAT-ACT, SET-P3, OPS-TV3 (same rule) |
| `ConversationSnapshot.autonomy_paused_reason = "takeover"` only when the takeover counts | M6-POL's `autonomy_not_paused`, through M6-PLAN (proposal), M6-SCHED (release), M6-TXA (claim) |
| `resume_here(...)`; the `tko` press | the owner, card only |
| `paused_by_takeover(session, *, account_id, now) -> list[int]` | STAT-ACT (`takeover_pauses` block), SET-P3 |
| Audit `takeover_detected` (+ `delegation_id`, `counts`), `conversation_autonomy_resumed` | OPS-TV3, OPS-VIEWS, M6-CATCH |

### 3.2 The takeover marker (`conversations/service.py::record_message`, step 4)

For a new message (`Effect.INSERTED` or `RESURFACED`) with `direction is OWNER_MANUAL`:
1. `previous = conversation.takeover_at`; `conversation.takeover_at = _later(previous, event.sent_at)` (Telegram's `date`, the newest owner message wins; an out-of-order late delivery never moves it back).
2. Never write `autonomy_paused_reason` (P3-schema §3.3; RG-19 asserts it). Remove the `PAUSE_TAKEOVER` constant (no other user; `conversations/models.py::LEGACY_TAKEOVER_REASON` documents the legacy value).
3. `await autonomy.note_takeover(session, account_id=…, conversation_id=conversation.id, contact_id=conversation.contact_id, previous_takeover_at=previous, takeover_at=conversation.takeover_at, resumed_at=conversation.autonomy_resumed_at, clock=clock)`.

Edits, no-op edits, duplicates and other directions do not touch `takeover_at` (an owner edit outdates drafts without pausing, ARCHITECTURE §6.5 step 4, as today). Ignore-mode chats record the marker too (state, not content).

`conversations/service.py` already imports `assistant.actions.lifecycle` at module level. Import `assistant.actions.autonomy` the same way; if `lint-imports` or a test import shows a cycle (through `automation.delegations_repo` or `inbox.notifications`), use a function-local import with a comment saying why.

### 3.3 When a takeover counts, and the loader input

```python
def takeover_counts(
    *, takeover_at: datetime | None, confirmed_at: datetime, resumed_at: datetime | None
) -> bool:
    """OD-5 / ADR-19: True when the owner's newest manual message in the chat is newer than
    both the delegation's confirmation and the owner's last Resume here."""
    return takeover_at is not None and takeover_at > max(confirmed_at, resumed_at or confirmed_at)
```

Strictly greater: a message dated exactly at the confirmation or the resume does not count.

`actions/policy_loader.py`:
- `_conversation(session, account_id, conversation_id, *, takeover_reference: datetime | None = None)` computes `ConversationSnapshot.autonomy_paused_reason`: the stored value when it is not NULL (`ingest_error` passes through unchanged; a legacy `'takeover'`, impossible after 0015, also passes through, fail closed); else `"takeover"` when `takeover_reference is not None and takeover_counts(takeover_at=row.takeover_at, confirmed_at=takeover_reference, resumed_at=row.autonomy_resumed_at)`; else `None`.
- `load_context` gains the keyword `takeover_reference: datetime | None = None` and passes it on. Every existing caller keeps the default.
- `autonomous_context` (M6-PLAN's function) passes `takeover_reference=delegation.confirmed_at` when a delegation is given (a one-argument named touch point; Josh lead sequences after M6-TXA). If this unit merges before M6-PLAN, it adds only the `load_context` keyword, and M6-PLAN's `autonomous_context` passes it from the start (Josh lead tells J3; M6-PLAN spec §3.2).

So `autonomy_not_paused` fails for `delegation_auto` exactly while the takeover counts (P3-interfaces §3.1). At proposal that gives a fallback card ("drafts are still offered", R-09); at release and claim it gives `cancelled/autonomy_paused` with no card. Separately, `no_takeover_since` (a send precondition, unchanged) still blocks any reply whose base message is older than the owner's message.

### 3.4 `note_takeover` (audit and the one-time notice)

`note_takeover(session, *, account_id, conversation_id, contact_id, previous_takeover_at, takeover_at, resumed_at, clock) -> None`, inside the ingestion transaction (the conversation row is locked; no commit, no Telegram call, no model call); `now = clock.now()`:
1. `covering = await active_covering(session, account_id=…, conversation_id=…, now=now)` (no lock).
2. `counted_before = covering is not None and takeover_counts(takeover_at=previous_takeover_at, confirmed_at=covering.confirmed_at, resumed_at=resumed_at)`; `counts_now` likewise with `takeover_at`.
3. **Audit** `takeover_detected` (subject `conversation`, actor `system`, `details = {"delegation_id": covering id or None, "counts": counts_now}`) when `previous_takeover_at is None` (the chat's first takeover, as today's "once" behaviour) or when `counts_now and not counted_before` (a takeover starts blocking a delegation session).
4. **Notice** when `counts_now and not counted_before`: `enqueue(session, account_id=…, kind=NotificationKind.TAKEOVER_PAUSED, priority=Priority.HIGH, dedupe_key=f"takeover:{covering.delegation_id}:{conversation_id}:{resumed_epoch}", refs=NotificationRefs(conversation_ids=(conversation_id,), contact_ids=(contact_id,), delegation_ids=(covering.delegation_id,)), clock=clock, conversation_id=conversation_id, contact_id=contact_id)` with no scheduler: `record_message` has none, so M05's notification reconciler delivers it within about a minute (the pause itself is immediate). `resumed_epoch = int(resumed_at.timestamp())` when the owner has pressed Resume here in this chat, else `0` (R81). The key therefore carries the resume epoch: the notice goes out once per pause, so the first takeover notifies once, and each new takeover after a Resume here notifies once again and offers the Resume here button again (FR-AUT-05; CG-10 ruled). A second owner message in the same pause finds `counted_before` true and enqueues nothing.

### 3.5 The notice and Resume here (`telegram/control/takeover.py`)

`TKO_CARD = "tko"`, `OP_RESUME_HERE = "rh"`.

**`TakeoverNoticeRenderer(clock, autonomy_config)`**, a `Renderer` (`(session, notification, account) -> RenderedAlert | None`), runs inside the delivery's claim transaction:
1. Return `None` (suppressed, never a stale claim, invariant 11) when: `autonomy_config.run_enabled` is false; the notification has no `conversation_id` or no `delegation_ids`; the conversation or its contact is gone (account-scoped); `active_covering(...)` is `None` or is not `delegation_ids[0]`; or the takeover no longer counts (`takeover_counts(...)` false: the owner already resumed).
2. Text (HTML; the name escaped once with `render.escape`; no message text, no numbers from contact content):
   - time-bounded: "✋ <b>You replied to {name}</b>, so I paused automatic replies in this chat until the delegation ends ({end}). I can still prepare drafts for you to approve."
   - standing: "✋ <b>You replied to {name}</b>, so I paused automatic replies in this chat until you resume them. I can still prepare drafts for you to approve."
   - `{end}` = `ends_at` in the owner's timezone, in the same format as M05's monitor alerts (`local_moment`).
3. Buttons: "▶️ Resume here" = `create_card(session, account_id=…, kind=TKO_CARD, clock=…, ref_id=conversation_id, ttl=…)` with `ttl` = 30 days for a standing delegation, else `min(ends_at − now, 30 days)`, and never below 1 minute (the M05 `stop_button` pattern); and `Button("▶️ Resume here", encode(TKO_CARD, card.token, OP_RESUME_HERE))` (callback data ≤ 64 bytes); then the Pause button (P3-interfaces §12.5).
4. Return `RenderedAlert(text, conversation_id, extra_buttons=(resume_button, pause_button()))`.

**The Pause button** is built by F0-S's pure helper `core/autonomy_texts.py::pause_button() -> Button` (fixed label, data `PAUSE_CALLBACK_DATA` = `pz:pause:p`; TL ruling R45, P3-interfaces §12.5). This unit appends it with `from assistant.core.autonomy_texts import pause_button` and writes no pause logic: a press goes to M6-PAUSE's `pz` handler. The helper is on the base from CP1, so the button is in from the start and `test_the_notice_shows_a_pause_button` is a normal test (no xfail). Named touch point kept from R45 as a fallback only: if this PR merged without the button, M6-PAUSE's PR may append it in `telegram/control/takeover.py` and fix the test (a few lines; Josh lead approves). No other M6-PAUSE edit to this file is allowed.

**`handle_tko_press(session, gateway, clock, scheduler, account, press) -> HandlerResult`** (a `CallbackHandler`; the `mon` press of M05 is the pattern):
1. `card = await claim_card(session, account_id=account.id, token=press.token, clock=clock)` (single use, account-scoped). `None`, `card.kind != TKO_CARD`, `card.ref_id is None` or `press.op != OP_RESUME_HERE` ⇒ after commit answer `CALLBACK_EXPIRED_TEXT`; `HandlerResult(False, "tko", None, note="no_longer_active")`.
2. `result = await autonomy.resume_here(session, account_id=account.id, conversation_id=card.ref_id, now=clock.now())`.
3. After commit (`db.uow.after_commit`), through the gateway only (AGENTS §5, §7): answer the press, then send the outcome as a new message (the alert keeps its text):

| Result | Answer | Message |
|---|---|---|
| `RESUMED` | "Resumed here" | "▶️ Automatic replies in this chat are on again. If you reply there yourself, they pause again." + "Automatic replies are still paused for your whole account; send /resume to turn them on." when `account.autonomy_paused` + "A message in this chat could not be processed, so they stay paused here until that is fixed." when the chat's `autonomy_paused_reason` is `ingest_error` |
| `NOT_PAUSED` | "Already on" | "Automatic replies were already on in this chat; nothing changed." |
| `NO_DELEGATION` | "Delegation ended" | "This delegation has ended; nothing changed." |
| `NOT_FOUND` | `CALLBACK_EXPIRED_TEXT` | — |

**`resume_here(session, *, account_id, conversation_id, now) -> ResumeHere`** (`ResumeHere` = `RESUMED`, `NOT_PAUSED`, `NO_DELEGATION`, `NOT_FOUND`), in `actions/autonomy.py`:
1. Lock the conversation of this account `FOR UPDATE` (`populate_existing`). Missing ⇒ `NOT_FOUND`.
2. `covering = await active_covering(...)` (no lock; conversation before delegation is the §9.4 order). `None` ⇒ `NO_DELEGATION`.
3. `takeover_counts(takeover_at, covering.confirmed_at, autonomy_resumed_at)` false ⇒ `NOT_PAUSED`.
4. `autonomy_resumed_at = now`, `updated_at = now`; audit `conversation_autonomy_resumed` (subject `conversation`, actor `owner`, `details = {"delegation_id", "source": "card"}`). Return `RESUMED`.

It clears only this chat's takeover pause for this delegation session: the account kill switch and an `ingest_error` pause stay. It is **card-only**: no agent tool calls it, and no tool may be added for it (like `resume_autonomy`, invariant 3b); a test scans `src/assistant/agent/` for it.

**`paused_by_takeover(session, *, account_id, now) -> list[int]`**: the conversation ids, ordered by id, of the live chats (`delegation_conversations.ended_at IS NULL`) of every delegation that `active_covering` would return now (status `active`, `starts_at <= now`, `ends_at IS NULL OR now < ends_at`), where `takeover_counts(...)` holds; one account-scoped query over `delegation_conversations` joined to `delegations` and `conversations`, then the pure check in Python.

### 3.6 Dead-lettered updates (`conversations/service.py::record_ingest_failure`)

Before the existing `lifecycle.outdate_pending(..., reason=INGEST_ERROR, include_approved=True)`, call `lifecycle.cancel_autonomous_in_conversation(session, account_id=…, conversation_id=conversation.id, reason=StateReason.INGEST_ERROR, now=clock.now())` (M6-SCHED's helper). Autonomous `approved`/`scheduled` actions then end `cancelled/ingest_error`, never `outdated` (P3-interfaces §2.2), and owner cards keep the M04 outcome. The `ingest_error` pause itself is unchanged and stays independent of `takeover_at`. This is one line outside "the takeover marker only" (contract gap CG-12).

### 3.7 Wiring (`_wire_autonomy`)

- `ctx.notification_delivery.register_renderer(NotificationKind.TAKEOVER_PAUSED, TakeoverNoticeRenderer(ctx.clock, ctx.autonomy_config))`.
- The `ControlRoutes` fragment: `callbacks={"tko": handle_tko_press}`. A duplicate kind raises at startup (F0-R).
- Function-local imports only.

### 3.8 Edge cases (each has a test in §5)

An owner message before the delegation's confirmation, including one ingested late; an owner message dated exactly at the confirmation or the resume; two owner messages (one audit, one notice); owner messages delivered out of order; an owner edit or a no-op edit; Resume here, then another owner message; Resume here pressed twice, from another account, after the card expired, after the delegation ended; an `ingest_error` pause together with a takeover; a delegation that ends and a new one confirmed later in the same chat; a notice delivered after the owner already resumed or after the delegation ended; `autonomy.run` off when the notice is delivered; a standing delegation.

### 3.9 Failure modes (fail closed)

| Situation | Outcome |
|---|---|
| No covering delegation (so no reference) | the takeover does not count, and `delegation_active` fails anyway: no autonomous send |
| A legacy `'takeover'` value in `autonomy_paused_reason` | passes through: autonomy stays paused |
| Notice row lost or its renderer suppressed | no claim is made; the pause still holds (the loader decides, not the notice) |
| Resume here on a stale or foreign card | "no longer active"; nothing changes |
| Import cycle | function-local import; never a broad `except` |

## 4. Files

**Owned (P3-interfaces §15.1):** `conversations/service.py` (the takeover marker in `record_message`, plus the one `record_ingest_failure` line, CG-12); `actions/autonomy.py` (new functions `takeover_counts`, `note_takeover`, `resume_here`, `ResumeHere`, `paused_by_takeover`; M6-PAUSE edits only its pause-source code, Josh lead sequences); `actions/policy_loader.py` (`_conversation`'s takeover input, the `load_context` keyword, the one argument in `autonomous_context`); `telegram/control/takeover.py` (new).

**Named touch points:** `bootstrap.py::_wire_autonomy` (append only).

**Later editor of your files:** M6-PAUSE, only as the fallback of §3.5 (append the Pause button in `telegram/control/takeover.py` if it is missing; R45 named touch point).

**Do not edit:** `actions/ingestion.py` (M6-SCHED does the takeover bulk cancel), `actions/release.py`, `actions/executor.py`, `actions/proposals.py`, `actions/lifecycle.py` (call `cancel_autonomous_in_conversation` only), `policy/*`, `conversations/models.py` (frozen ORM; `LEGACY_TAKEOVER_REASON` is F0-S's), `observability/status*.py` (STAT-ACT), `telegram/control/{dispatcher,routes,handlers,panels}.py`, the never-edit list in P3-interfaces §15.2.

## 5. Tests (red first)

Real PostgreSQL 18, `FakeClock`, `FakeTelegramSender`, the business ingestion helpers (`tests/integration/business_env.py::deliver`, `business_message`), the F0-S factories (`seed_delegation`, `seed_autonomous_action`). All text synthetic.

### 5.1 Named tests (P3 plan, IMPLEMENTATION_PLAN, RG list)

In `tests/integration/test_takeover.py`. A row that needs M6-PLAN's planner, M6-SCHED's release or M6-TXA's Tx A and runs before that unit is merged is `xfail(strict=True, reason="awaiting <UNIT-ID>")`; whichever PR merges second removes the marker.

| Source | Test |
|---|---|
| RG-19 ext | `test_rg19_after_0015_no_code_writes_the_takeover_pause_reason` (an owner message sets `takeover_at`; `autonomy_paused_reason` stays NULL; a source scan finds no assignment of `"takeover"` to that column under `src/assistant`) |
| RG-19 ext | `test_rg19_a_counting_takeover_stops_autonomy_at_proposal_release_and_claim` (the owner writes in the chat after the delegation's confirmation, then the contact writes again; every action below answers that newer contact message, so the M04 `no_takeover_since` passes and only the session-scoped pause can block. Then: the planner's decision fails `autonomy_not_paused` ⇒ fallback card; a `scheduled` action seeded after the takeover is released ⇒ `cancelled/autonomy_paused`; an `approved` one seeded after the takeover, at Tx A ⇒ `cancelled/autonomy_paused`; zero sends. The ingestion bulk cancel of actions that existed at the takeover is M6-SCHED's RG-19 test) |
| P3 plan (OD-5) | `test_a_pre_delegation_takeover_does_not_block` (an owner message before `confirmed_at`, and one migrated by the 0015 data step: the next reply may be `ALLOW_AUTO`) |
| P3 plan (OD-5) | `test_a_post_start_takeover_pauses_until_the_end_or_resume_here` |
| P3 plan (FR-AUT-05, R81) | `test_the_takeover_notice_is_sent_once` (two owner messages ⇒ one `takeover_paused` row, one audit) |

### 5.2 Behaviour (`tests/integration/test_takeover.py`)

- `test_an_owner_message_sets_takeover_at_to_its_telegram_date`
- `test_takeover_at_keeps_the_newest_date_when_messages_arrive_out_of_order`
- `test_an_owner_edit_or_a_no_op_edit_does_not_move_takeover_at`
- `test_a_takeover_dated_exactly_at_the_confirmation_does_not_count`
- `test_no_notice_without_an_active_covering_delegation`
- `test_a_takeover_after_resume_here_pauses_again`
- `test_a_takeover_after_resume_here_notifies_again_with_a_fresh_resume_button` (R81: first takeover ⇒ key `takeover:{d}:{c}:0`; Resume here at epoch T ⇒ the next owner message enqueues `takeover:{d}:{c}:T`, a second `takeover_paused` row, and its rendered alert carries a new Resume here card; a second owner message in the same pause adds no row)
- `test_resume_here_resumes_only_this_chat_and_only_this_delegation_session`
- `test_resume_here_keeps_an_ingest_error_pause_and_the_account_pause`
- `test_an_ingest_error_and_a_takeover_are_tracked_separately` (closes M03 D91: an `ingest_error` pause no longer hides a later takeover, and clearing it leaves the takeover counting)
- `test_a_takeover_does_not_carry_into_a_later_delegation`
- `test_a_dead_letter_cancels_scheduled_and_approved_autonomous_actions` (`cancelled/ingest_error`; owner cards `outdated/ingest_error` as in M04)
- `test_takeover_detected_audit_carries_the_delegation_and_whether_it_counts`
- `test_paused_by_takeover_lists_only_chats_where_the_takeover_counts`

### 5.3 Notice and card (`tests/integration/test_takeover_notice.py`)

- `test_the_notice_names_the_contact_once_escaped_and_the_owner_local_end` (a contact name with `<` and `&`; exactly one escaping; no message text)
- `test_a_standing_delegation_notice_says_until_you_resume`
- `test_the_notice_is_suppressed_when_the_delegation_ended_or_the_owner_already_resumed`
- `test_the_notice_is_suppressed_while_autonomy_run_is_off`
- `test_resume_here_press_resumes_and_answers_after_commit`
- `test_a_second_resume_here_press_is_no_longer_active`
- `test_a_resume_here_press_from_another_account_or_an_expired_card_changes_nothing` (RG-21)
- `test_resume_here_after_the_delegation_ended_changes_nothing`
- `test_resume_here_is_card_only_and_no_tool_can_call_it` (no tool in the default registry is bound to it; a scan of `src/assistant/agent/` finds no reference)
- `test_the_notice_shows_a_pause_button` (the second extra button equals `core.autonomy_texts.pause_button()`: data `PAUSE_CALLBACK_DATA`, within 64 bytes; R45)

### 5.4 Existing tests to update (same PR)

- `tests/integration/test_business_ingestion.py::test_an_owner_message_is_owner_manual_and_sets_takeover`: assert `takeover_at` equals the message date and `autonomy_paused_reason is None`.
- `…::test_a_new_owner_message_sets_takeover_once`: unchanged assertion (one `takeover_detected`); keep it passing.
- `tests/integration/test_kill_switch.py::test_resume_clears_the_account_flag_and_keeps_conversation_takeover_pauses`: seed a covering delegation and `takeover_at` after its confirmation instead of `autonomy_paused_reason="takeover"`; after `/resume` the account flag is clear and `paused_by_takeover` still lists the chat. Tell M6-PAUSE and M6-TXA (both edit the same file; Josh lead sequences).
- `tests/integration/test_draft_outdating.py::test_an_owner_edit_outdates_without_pausing_autonomy` and `tests/scenarios/test_m04_j8_takeover.py`: must pass unchanged (no assertion on the old column remains true by accident; check them).
- Policy unit tests that feed `autonomy_paused_reason="takeover"` directly into `PolicyContext` stay valid: the rule input did not change.

### 5.5 Mutation table (Tier-1A)

| # | Rule, lock or guard added | Mutation | Named test expected to fail |
|---|---|---|---|
| 1 | The writer no longer writes `autonomy_paused_reason` | restore the old assignment | `test_rg19_after_0015_no_code_writes_the_takeover_pause_reason` |
| 2 | `takeover_at` keeps the newest date | assign `sent_at` unconditionally | `test_takeover_at_keeps_the_newest_date_when_messages_arrive_out_of_order` |
| 3 | Counts only after `max(confirmed_at, resumed_at)` | compare with `confirmed_at` only | `test_a_takeover_after_resume_here_pauses_again` / `test_a_post_start_takeover_pauses_until_the_end_or_resume_here` |
| 4 | Strictly greater | `>=` | `test_a_takeover_dated_exactly_at_the_confirmation_does_not_count` |
| 5 | The loader sets `"takeover"` only when it counts | set it whenever `takeover_at` is set | `test_a_pre_delegation_takeover_does_not_block` |
| 6 | `ingest_error` passes through | let `"takeover"` override it, or drop it | `test_an_ingest_error_and_a_takeover_are_tracked_separately` |
| 7 | `autonomous_context` passes the reference | drop the argument | `test_rg19_a_counting_takeover_stops_autonomy_at_proposal_release_and_claim` |
| 8 | Notice dedupe and the counted-before guard | enqueue on every owner message without the key | `test_the_takeover_notice_is_sent_once` |
| 8a | The dedupe key carries the resume epoch | drop `resumed_epoch` from the key | `test_a_takeover_after_resume_here_notifies_again_with_a_fresh_resume_button` |
| 9 | Notice only with a covering delegation | drop the check | `test_no_notice_without_an_active_covering_delegation` |
| 10 | `resume_here` needs a covering delegation | drop the check | `test_resume_here_after_the_delegation_ended_changes_nothing` |
| 11 | Card claim is single use and account-scoped | look the card up without `claim_card` | `test_a_resume_here_press_from_another_account_or_an_expired_card_changes_nothing` |
| 12 | Renderer suppresses stale notices | always render | `test_the_notice_is_suppressed_when_the_delegation_ended_or_the_owner_already_resumed` |
| 13 | Dead letter cancels autonomous rows first | remove the line | `test_a_dead_letter_cancels_scheduled_and_approved_autonomous_actions` |
| 14 | Resume keeps the other pauses | also clear `ingest_error` or the account flag | `test_resume_here_keeps_an_ingest_error_pause_and_the_account_pause` |

## 6. Acceptance criteria

1. Every test in §5 exists under its name and passes in CI; the updated existing tests pass.
2. After this merges, no code writes `autonomy_paused_reason = 'takeover'`; `takeover_at` and `ingest_error` are independent.
3. A takeover blocks autonomy only inside the current delegation session and only until Resume here; drafts are still offered as cards.
4. The owner gets one `takeover_paused` notice per pause (per delegation, chat and resume epoch: a new takeover after Resume here notifies once more, R81), and Resume here works only through the card.
5. Every mutation in §5.5 is killed.
6. `ruff`, format, `mypy` on the changed packages and `lint-imports` clean; logs content-free (the renderer's text is never logged).

## 7. Docs (same PR)

- ARCHITECTURE §6.5 step 4: the takeover as built (`takeover_at` from Telegram's date; no pause column write; counted only inside the delegation session; the one-time notice; Resume here card-only; the dead-letter cancel).
- ADR-19 (takeover scoped to the delegation session) as written by the TL in F0-C part 2; the PR description notes any as-built difference.
- PR description: the CG-12 note (and that CG-10 is ruled, R81), the mutation table, open items for M06.md (for example the notice latency through the reconciler).

## 8. Owner decisions

- **OD-5** (paused until the delegation ends or Resume here; only messages after the start or the last resume count; legacy flags migrated): the whole unit. A cooldown instead (for example 2 h) would change only `takeover_counts` (add `now − takeover_at < cooldown`) and the notice text.
- **OD-11** (drafts still offered while paused): the fallback-card path is M6-PLAN's; this unit only supplies the rule input.
- **OD-5 follow-up (ruled, R81; was CG-10):** after Resume here, a second takeover pauses the chat again and the owner is told again. The notice dedupe key includes the resume epoch, `takeover:{delegation_id}:{conversation_id}:{resumed_epoch}` (`0` before any resume), so each pause notifies once and offers a fresh Resume here card. P3-interfaces §11.1 states the key.

## 9. Risks and notes

- **Contract gaps:** CG-10 (silent re-pause after Resume here: ruled R81, the dedupe key carries the resume epoch), CG-12 (the `record_ingest_failure` line and the new `actions/autonomy.py` functions). The Pause button uses F0-S's `core/autonomy_texts.py::pause_button()` (R45, G1 ruled). The M6-PAUSE touch point in `telegram/control/takeover.py` (§3.5) is confirmed by R45 and named in M6-PAUSE's spec.
- **Notice latency:** without a scheduler in `record_message`, the notice waits for M05's notification reconciler (about a minute). The pause is immediate because the loader decides from `takeover_at`; the notice is information only.
- **Clock comparison:** `takeover_at` is Telegram's date (whole seconds) and `confirmed_at`/`autonomy_resumed_at` are server time. Within a few seconds of a confirmation or a resume the comparison can go either way: a message sent just before a resume may still count (safe), and a message sent just after a confirmation may not count (unsafe, but narrow). In the unsafe case the M04 `no_takeover_since` still blocks every reply whose base message is older than the owner's message, so only a reply to a contact message that arrives after the owner's is exposed. Record this in the PR description and in M06.md's open items; no code change.
- **Ordering with M6-SCHED:** the takeover's bulk cancel (M6-SCHED, in the actions listener) and this marker (in `record_message`) run in the same ingestion transaction; neither depends on the other's order.
