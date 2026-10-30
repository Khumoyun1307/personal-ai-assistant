# M6-ESC — escalation and covered-chat effects

| | |
|---|---|
| **Unit** | M6-ESC — FR-TRI-03 delivery rule for chats covered by an active delegation; covered-chat triage effects (one draft path per burst, no duplicate `triage_needs_reply`, suspicious handling); `system.delegation_health` (AI unavailable or budget out > 10 min, chat possibly paused); renderers for `delegation_escalation`, `delegation_paused_llm`, `chat_possibly_paused`, `autonomy_volume_cap`, `autonomy_not_sent`; the Draft reply button and its `drf` press handler (OD-11) |
| **Team / slot** | Edvard / E1 |
| **Size** | M+ (M before OD-11 added the `autonomy_not_sent` renderer and the `drf` press) |
| **Depends on** | F0-R (the `delegation_requester` hook, the `active_delegation` slot, the `escalation` job group and its `system.delegation_health` shell, `P3Ports`) and M6-PLAN's escalation **contract** (P3-interfaces §4.1, §11.2), not its code: tests seed the producer's rows with `p2_factories`/`p3_factories` and a fake requester (README §3). Merge condition: GW-ESC closed (P3-interfaces §4.6: no P3 renderer merges before it; landed in P2 #133, verification only). The Pause button on two renderers uses F0-S's pure `core/autonomy_texts.py::pause_button()` (R45, §3.6); its press handler is M6-PAUSE's |
| **Branch → base** | `p3/m6-esc` → `int/p3` |
| **Peer reviewer** | J2 (Claude; fast lane and pause notices), dispatched by the TL |
| **QA** | QE2, before merge (privacy and isolation QA, ORG §3) |
| **Tier** | 1 (high and safety-adjacent): TL scope check |
| **Normative inputs** | P3-interfaces §2.3, §4.1, §4.5, §6.1, §7.1–§7.2, §11.1–§11.3, §12.2–§12.5, §13, §15.1, §15.3, §15.4; PRD J5, FR-TRI-03, FR-AUT-07, §11.5, §15 (rows "LLM unavailable/budget exhausted > 10 min" and "chat appears paused/excluded"); ARCHITECTURE §17, §18; AGENTS §6 invariants 3a, 7, 9, 11, 12; IMPLEMENTATION_PLAN §8 S06.3; decisions P3-T31; TL rulings R45, R60 |

## 1. Goal

In a chat covered by an active delegation, the owner hears about what needs the owner and nothing twice: a decision request alerts at once (quiet-hours bypass only when the delegation allows it), everything else waits for the end-of-delegation catch-up, a burst gets exactly one draft path (delegated or Assist, never both), and a suspicious contact is never answered automatically and alerts when it repeats. When the AI is unavailable or the budget is used up for more than 10 minutes, each active delegation is reported once as paused and its chats fall back to alerts with a Draft reply button; a chat that Telegram reports as paused is reported once. **OD-11 (answered 2026-10-29):** whenever autonomy does not send, the owner gets an alert that offers a draft, not a card: the `autonomy_not_sent` alert (rendered here) and the escalation alert both carry a Draft reply button. Pressing it shows the reply autonomy already wrote as a normal approval card with no model call, or, when no text exists, asks for one owner-approved draft (one model call, consent re-checked when it runs). All alert text is rendered at delivery from ids, escaped once, and never calls a scheduled or uncertain reply "sent"; no press answer ever reports a draft that does not exist.

## 2. Scope

**In:**
1. `inbox/triage_store.py` (effects in covered chats): the requester call moved before the alerts, no delegated path on a suspicious verdict, no `triage_needs_reply` and no Assist draft when the delegated path is registered, `delegated` in the audit details.
2. `inbox/notification_policy.py`: the covered-chat rule, the `already_alerted` guard, `NotificationFacts.escalation_category` and `already_alerted`, the `effective_priority` changes for `delegation_escalation`, the pure helper `escalation_category_of`.
3. `inbox/notifications.py`, fact loading only: `active_delegation`, `already_alerted`, `escalation_category`, the suspicious repeat count; the setter `use_delegation_facts`.
4. `inbox/delegation_alerts.py` (new): the five renderers (the four above plus `autonomy_not_sent`, OD-11), the Draft reply button helper, `DelegationHealth` (the `system.delegation_health` body), the outage and chat-paused queries, constants and audit event names.
5. `telegram/control/draft_reply.py` (new, OD-11): the `drf` press handler (§3.6a).
6. `jobs/tasks_escalation.py`: only if the F0-R shell needs an adjustment (the handler is wired through the group).
7. Lines in `_wire_automation` (`bootstrap.py`).
8. ARCHITECTURE §17 and §18.

**Out:** producing `delegation_escalation` rows from plans and every `autonomy_volume_cap` row (M6-PLAN); producing the held replies and their `autonomy_not_sent` rows (M6-PLAN's job, M6-SCHED's `convert_cancelled`; this unit produces only the outage `autonomy_not_sent` of §3.5); the held-reply sync rule in `telegram/control/approvals.py` (M6-PLAN); the `ALERT_DRAFT` job reason (F0-S); the `DelegationRequester` implementation (M6-PLAN); `takeover_paused` (M6-TAKE); the `delegation_catch_up` builder and renderer (M6-CATCH); the Pause button helper and the `pz` press (M6-PAUSE); setting `conversations.availability` (M04's executor, unchanged); the digest (unchanged: it keeps listing batched alerts).

## 3. Design

### 3.1 Contracts consumed and provided

| | Name | Contract |
|---|---|---|
| Consumes | `DelegationRequester.request(...) -> bool` (`True` = an active delegation covers the chat and the delegated path is registered; `False` with `autonomy.run` off) | P3-interfaces §4.5 |
| Consumes | the F0-R hook in `TriageRepository.apply`; `NotificationFacts.active_delegation` slot; `EscalationJobHandlers.delegation_health: PeriodicHandler` | §4.5, §11.3, §7.2; F0-R spec §3.6–§3.8 |
| Consumes | `active_covering`, `list_active`, `get` | §6.1 |
| Consumes | kinds, `NotificationRefs.delegation_ids`, delivery classes (`delegation_paused_llm` = system; the other three standard) | §11.1–§11.2 |
| Consumes | `delegation_escalation` rows from M6-PLAN (M6-PLAN spec §3.6 step 3): priority `urgent_decision` → urgent, `needs_owner` → high, `suspicious` → normal, `fyi` → low (one to one); dedupe `deleg:{delegation_id}:{conversation_id}:{base_message_id}`; refs `conversation_ids`, `contact_ids`, `message_ids=(base_message_id,)`, `delegation_ids`, `action_ids` (the action the plan created, if any); `bypass_quiet_hours = rules.escalation.urgent_bypass_quiet_hours` for `urgent_decision`. Also enqueued, as `needs_owner`, for a plan that ends `NOT_DRAFTED` with draft status `invalid`, `refused` or `failed` | §4.1, §11.2 |
| Consumes | `autonomy_volume_cap` rows from M6-PLAN (priority high, dedupe `cap:{delegation_id}:{conversation_id}:{hour_epoch}`); the failed code travels in `refs.alert_kind`, which M6-PLAN sets (R60; §9 note 1); a row without it renders a generic line | §11.2 |
| Consumes | the Pause button: F0-S's pure `core/autonomy_texts.py::pause_button() -> Button` (R45; no `telegram` import, so `inbox/` may use it); the press handler is M6-PAUSE's | §12.5 |
| Consumes | `AUTONOMY_CODES[code].explanation` | §3.2 |
| Consumes (OD-11) | `autonomy_not_sent` rows from M6-PLAN and M6-SCHED: class `system` (OD-11a; enqueued with `AlertClass.SYSTEM`), dedupe `notsent:{action_id}`, refs `conversation_ids`, `contact_ids`, `delegation_ids`, `message_ids`, `action_ids = (held reply,)`, `alert_kind` (a code, `late`, `shadow` or `not_sent`); the held reply is `agent_proposal` with `delegation_id` and `draft_flags["presentation"] = "on_request"` | P3-interfaces §2.3, §11.2 |
| Consumes (OD-11) | `P3WireContext.approval_cards` (`ApprovalCardPresenter`); M6-PLAN's held-reply sync rule (the `approval_card_requested` audit row releases a held reply to the sync) | P3-interfaces §2.3, §15.3 |
| Consumes (OD-11) | `actions/drafts.py::request_draft(..., reason=DraftReason.ALERT_DRAFT)` (F0-S adds the reason; REDRAFT semantics); `agent.drafting.draft_consent_ok(session, account_id, model)` for the press-time consent check, with `model = ActionsConfig.draft_route_model` (the `replies.draft` route; `telegram` may import `agent`, as `approvals.py` already does) | P3-interfaces §7.1 |
| Provides | renderers for `delegation_escalation`, `delegation_paused_llm`, `chat_possibly_paused`, `autonomy_volume_cap`, `autonomy_not_sent` (OD-11) | §11.2 |
| Provides | producer of `delegation_paused_llm` and `chat_possibly_paused`; the outage `autonomy_not_sent` per covered chat (dedupe `outage:{delegation_id}:{conversation_id}:{m}`; OD-11 replaced the deterministic `delegation_escalation` of R60, §9 note 8) | §11.2, §13 |
| Provides (OD-11) | the `drf` callback kind and its press handler `telegram/control/draft_reply.py::DraftReplyPress`; audit `approval_card_requested` | P3-interfaces §12.3, §12.5, §13 |
| Provides | `inbox/notification_policy.py::escalation_category_of(priority: str, alert_kind: str \| None) -> str` (pure; used by fact loading, `_load` and the renderer) | this spec |

### 3.2 Covered-chat triage effects (`inbox/triage_store.py`)

`TriageRepository.apply` (m05 `inbox/triage_store.py:328`) keeps every step up to the task closures. Then, in this order:

```python
delegated = False
if (
    self._delegation_requester is not None
    and verdict.escalation_category != "suspicious"
    and await _contact_wrote(session, account_id, verdict.burst_message_ids)
):
    delegated = await self._delegation_requester.request(
        session, account_id=account_id, conversation_id=conversation_id,
        covers_through_message_id=verdict.covers_through_message_id, now=now,
    )
alerts = verdict_alerts(verdict)
if delegated:
    # The delegated path answers or escalates this burst (no duplicate needs-reply alert).
    alerts = tuple(a for a in alerts if a.kind != NotificationKind.TRIAGE_NEEDS_REPLY)
# ... enqueue each alert exactly as today ...
if not delegated and <the existing Assist conditions>:
    await self._draft_requester(session, account_id, conversation_id)
```

- **One draft path per burst:** `True` from the requester skips the Assist draft; `False` (no delegation, `autonomy.run` off, or no contact message) leaves the Assist path exactly as today.
- **Suspicious burst:** a verdict with `escalation_category == "suspicious"` never starts the delegated path (PRD §11.5 "do not comply": a suspicious contact is never answered automatically). Its `triage_suspicious` alert follows §3.3. The Assist path already excludes suspicious bursts (unchanged).
- `triage_urgent` and `triage_decision` are still enqueued in a covered chat and stay immediate: they are the immediate decision alert (J5; R60).
- The `triage_completed` audit row's `details` gains `"delegated": <bool>` (content-free).
- The requester runs in the effects transaction, after the §9.4 locks `apply` already holds; it only sets a flag and registers an after-commit defer (M6-PLAN), so no lock is held across a model or Telegram call.
- **Locks (ARCHITECTURE §9.4), the whole unit:** triage effects keep `apply`'s order (account `FOR SHARE` → conversation → `conversation_triage` row) and add none; delivery keeps M05's account-then-notification order and the fact queries read without locking; `system.delegation_health` takes no row lock (reads, then `enqueue` with dedupe keys, one short transaction per account), and its periodic queueing lock (the task name) prevents two runs overlapping.

### 3.3 The delivery rule (`inbox/notification_policy.py`, FR-TRI-03, OD-7)

1. `NotificationFacts` gains `escalation_category: str | None = None` (set only for `delegation_escalation`, §3.4) and `already_alerted: bool = False` (`active_delegation` exists since F0-R).
2. `effective_priority` changes, for every chat (covered or not):
   - a `delegation_escalation` is never demoted for a low-priority contact (as `monitor_match`, R9: the owner set up the delegation and its rule decides urgency);
   - the "alert if repeated" rule (`load.suspicious_from_contact_24h >= 1` ⇒ at least `HIGH`) applies to `delegation_escalation` with `escalation_category == "suspicious"` as it does to `triage_suspicious`.
3. `COVERED_KINDS = {triage_urgent, triage_decision, triage_needs_reply, triage_suspicious, delegation_escalation}`. In `decide_delivery`, after F0-R's class handling and after computing the effective priority, before the M05 standard rules:
   ```
   if facts.active_delegation and facts.kind in COVERED_KINDS:
       if facts.already_alerted:            -> BATCH, reason "already_alerted"
       if not passes(facts, priority, load): -> BATCH, reason "delegation_catch_up"
   # otherwise continue with the M05 rules (mute, quiet by default, quiet hours, cap)
   ```
   `passes`:

   | Kind | Passes to the M05 rules when |
   |---|---|
   | `triage_urgent`, `triage_decision` | always (a decision request alerts at once, J5, PRD R2 #1) |
   | `delegation_escalation` | effective priority is `URGENT` (an `urgent_decision`, or a VIP contact's `needs_owner`), or `escalation_category == "suspicious"` and it is repeated |
   | `triage_suspicious` | repeated (`load.suspicious_from_contact_24h >= 1`) |
   | `triage_needs_reply` | never (the delegated path or the catch-up covers it, even for a VIP) |

   An urgent `delegation_escalation` then meets the M05 quiet-hours rule: it goes out during quiet hours only when `facts.bypass_quiet_hours` (the delegation's `urgent_bypass_quiet_hours`, OD-7) or the owner's global urgent bypass is on; otherwise it is deferred to the end of quiet hours (held, never dropped). The M05 cap is unchanged.
4. Every other kind, and every kind while `active_delegation` is false, behaves exactly as after F0-R. `autonomy_volume_cap` and `chat_possibly_paused` are standard `HIGH` alerts (immediate outside quiet hours, batched inside, as M05 does for `HIGH`); `delegation_paused_llm` is `SYSTEM` (never held, capped or gated by `notifications.deliver`). `autonomy_not_sent` is `SYSTEM` too (OD-11a, P3-interfaces §11.2): the owner chose that these alerts reach them at once, so it is never held for quiet hours, capped, batched or gated by `notifications.deliver`. It is not in `COVERED_KINDS`, so a covered chat never batches it.
5. A batched alert keeps status `batched` with the reason code above; the digest keeps listing batched alerts as today (so a standing delegation's batched alerts are never lost), and M6-CATCH reads the `delegation_catch_up` ones for its "decisions needed" section.

### 3.4 Fact loading (`inbox/notifications.py`, fact loading only)

1. `NotificationDelivery` gains `use_delegation_facts(self, *, enabled: bool) -> None` (like `register_renderer`; called once at startup by M6-ESC's wire line with `autonomy.run`). While it is off (the default) `active_delegation` stays `False`: with `autonomy.run` off nothing autonomous handles a covered chat, so no alert is ever held back for it.
2. `_facts` additions, inside the delivery transaction:
   - `escalation_category` (for `delegation_escalation` only): `refs["alert_kind"]` when it is an `EscalationCategory` value (M6-PLAN sets it on every escalation, R60; since OD-11 M6-ESC produces no escalation of its own), else (a row from before the key existed) derived from the row's stored producer `priority` through M6-PLAN's one-to-one mapping (`urgent` → `urgent_decision`, `high` → `needs_owner`, `normal` → `suspicious`, `low` → `fyi`); anything else → `needs_owner`. The helper is the pure `escalation_category_of(priority, alert_kind)` in `inbox/notification_policy.py`, so `inbox/notifications.py` never imports `inbox/delegation_alerts.py` (which imports `enqueue` from `notifications.py`; no import cycle). Imports go one way: `notifications.py` → `automation/delegations_repo.py` (the read seam);
   - `active_delegation` = enabled, kind in `COVERED_KINDS`, `notification.conversation_id` set, and `active_covering(session, account_id=notification.account_id, conversation_id=…, now=now) is not None`;
   - `already_alerted` (only when `active_delegation` and the kind is `triage_urgent`, `triage_decision` or `delegation_escalation`): another notification of those three kinds exists for the same account and conversation, `id <> :this`, `status IN ('sent', 'uncertain')`, `created_at >= now − 24 h`, whose `refs -> 'message_ids'` shares at least one id with this one's (`EXISTS (SELECT 1 FROM jsonb_array_elements_text(n.refs -> 'message_ids') AS m(id) WHERE m.id::bigint = ANY(:ids))`). Whichever alert of a burst is delivered first goes out; the second is batched.
3. `_load`: `suspicious_from_contact_24h` also counts `delegation_escalation` rows of that contact whose category is `suspicious` (`refs ->> 'alert_kind' = 'suspicious'`, or no `alert_kind` and `priority = 'normal'`) in the last 24 h (excluding this row).
4. Every query carries `account_id` (invariant 12). Nothing else in the file changes (claim, send, record, cap load).

### 3.5 `system.delegation_health` (`inbox/delegation_alerts.py::DelegationHealth`; OD-11, PRD §15)

`DelegationHealth(*, session_factory, clock, scheduler, monthly_budget_usd)`, method `async run(now: datetime) -> int` (a `PeriodicHandler`; returns the number of notification rows inserted). Wired only while `autonomy.run` is on (§3.7). Each run:

1. Accounts to check: `SELECT DISTINCT account_id FROM app.delegations WHERE status = 'active'` (a system scan, like the M6-LIFE overdue reconciler); everything after this is per account, in its own short transaction, with an explicit `account_id` predicate.
2. **Outage** `llm_outage(session, *, account_id, now, monthly_budget_usd) -> Outage | None`, `Outage(cause: Literal["llm_unavailable", "budget"], since: datetime)`:
   - `budget`: a budget is set and this month's spend is at or above it; `since` = the `created_at` of the first `llm_calls` row of the month at which the running sum of `cost_estimate` (ordered by `created_at, id`) reached the budget. An outage when `now − since >= LLM_OUTAGE_AFTER` (10 min).
   - `llm_unavailable`: `last_ok` = the newest `started_at` of this account's `agent_runs` with status `succeeded` or `refused` (the provider answered); `failures` = runs with status `failed`, `error_class IN UNAVAILABLE_ERROR_CLASSES` (`llm_unavailable`, `llm_rate_limited`, `llm_auth`), `started_at > last_ok` (any, when there is no `last_ok`) and `started_at >= now − 24 h`. An outage when failures exist, `since` = the oldest failure, `now − since >= 10 min` and the newest failure is within the last 10 min (still failing). Verify that every LLM run type writes `error_class = f"llm_{kind}"` (drafting, triage, summarize, monitor evaluation, owner requests) and list any other spelling explicitly.
   - Both: `budget` wins. Neither: `None`.
3. **Per active delegation** (`list_active`), when there is an outage:
   - enqueue `delegation_paused_llm`: `alert_class=SYSTEM`, priority `HIGH`, dedupe `llmpause:{delegation_id}:{epoch(since)}`, refs `delegation_ids=(id,)`, `conversation_ids` = the delegation's live chats, `alert_kind` = the cause, `scheduler=`; when a row was inserted, write the audit row `delegation_paused_llm` (`subject_type` `delegation`, actor `system`, details `{"cause": cause}`). Once per delegation per outage; a new outage (a new `since`) reports again.
   - **Outage alert** per covered chat (OD-11; replaces the deterministic `delegation_escalation` of the default answer): the newest contact message with `sent_at >= max(since, delegation.starts_at)`; if there is one, enqueue `autonomy_not_sent`: `alert_class=SYSTEM` (delivery class `system`, OD-11a: delivered at once, even in quiet hours), priority `HIGH`, refs `conversation_ids`, `contact_ids`, `message_ids=(m,)`, `delegation_ids`, no `action_ids`, `alert_kind` = the cause (`llm_unavailable` / `budget`), dedupe `outage:{delegation_id}:{conversation_id}:{m}`, `conversation_id` and `contact_id` columns set. Its own key, not M6-PLAN's `deleg:` key: during an outage the planner's failed draft has usually already enqueued a `needs_owner` escalation under `deleg:…:{m}`, which a covered chat batches for the catch-up, and the owner must still get a deliverable alert with the Draft reply button. It creates no action: during the outage nothing is sent; the chat falls back to alerts. The Draft reply press checks the outage again and answers factually while it lasts (§3.6a).
4. **Chat possibly paused** (every run, outage or not): per conversation of this account whose `availability` is still `'possibly_paused'` (M04's executor sets it on `peer_unavailable`; a new contact message resets it), the **episode start** is the earliest action with `origin = 'delegation_auto'`, `state = 'failed'`, `error_class = 'peer_unavailable'` and `attempt_started_at >= now − 24 h` whose `attempt_started_at` is after the conversation's newest contact message `sent_at`. When one exists, enqueue `chat_possibly_paused`, standard, priority `HIGH`, dedupe `chatpaused:{conversation_id}:{since_epoch}` with `since_epoch` = the episode start's `attempt_started_at` in epoch seconds (P3-interfaces §11.2), refs `conversation_ids`, `contact_ids`, `action_ids` (that action), `delegation_ids` (when set). Keying on the earliest failure after the newest contact message gives exactly one notice per episode even when two autonomous sends fail in it, and a new key once the contact writes again and a later send fails. The M04 precondition `conversation_available` already stops further autonomous sends there until the contact writes again.
5. Errors propagate (procrastinate logs the job failure; the next minute retries). No broad `except`.

### 3.6 Renderers (`inbox/delegation_alerts.py`)

Each is a `Renderer` (`(session, notification, account) -> RenderedAlert | None`, m05 `inbox/alert_texts.py:46`). English (P2-O11); owner-local times (a module helper that falls back to UTC for a bad zone); every untrusted field (contact names, triage reasons) escaped once with `html.escape(…, quote=False)`, application markup (`<b>`) written as markup; the gateway sanitizer keeps entities and never escapes them again (P2 #133; WORKFLOW §6.5 "escape once"). A renderer returns `None` when what it describes is gone (suppressed at delivery, "deleted source → suppressed"). Delivery adds Open chat / Useful / Not useful for standard alerts about a chat (unchanged).

**Draft reply button (OD-11).** The `autonomy_not_sent` renderer always, and the `delegation_escalation` renderer when no automatic reply was sent or is pending for the burst (no `action_ids`; a held reply; or a `delegation_auto` action that ended `cancelled`, `expired` or `failed`) and the escalation is not `suspicious`, append "📝 Draft reply" through `extra_buttons` before the Pause button. One helper, `draft_reply_button(session, *, notification, window_end, clock) -> Button`, creates the `drf` card inside the delivery's claim transaction (`accounts.service.create_card(kind="drf", ref_id=notification.id, ttl=…)`, the M05 monitor Stop pattern, `automation/monitors.py::stop_button`), with `ttl = clamp(window_end − now, 1 min, 24 h)` where `window_end` is the chat's reply-window close less `core.reply_window.SAFETY_MARGIN` (24 h when unknown), and returns `Button("📝 Draft reply", encode("drf", token, "dr"))`. A delivery whose send definitely fails leaves the card to expire (harmless; the next attempt makes its own). **A suspicious escalation never shows Draft reply** (P3-T153): when `escalation_category_of(priority, refs.alert_kind)` is `suspicious` (a suspected instruction or prompt-injection attempt) the renderer adds no `drf` card and only Pause; the owner reads the chat and answers, and no model drafts in response to an attack attempt.

**Pause button (R45).** The escalation, volume-cap and `autonomy_not_sent` (OD-11) renderers append the Pause button through `extra_buttons` (P3-interfaces §12.5), using F0-S's pure `core/autonomy_texts.py::pause_button()` (it imports no `telegram` module, so `inbox/delegation_alerts.py` may call it; the module still never imports `assistant.telegram`). No constructor argument, wiring line or port is involved, and the test `test_the_escalation_and_volume_cap_alerts_carry_the_pause_button` is an ordinary test, not an xfail. The press handler (`pz:pause:p`) is M6-PAUSE's; until it merges a press is an unknown callback, harmless with every P3 flag off. The release check (R48, before enable 3) is that every §12.5 card carries the button and that M6-PAUSE's handler answers it.

| Kind | `None` when | Text (read at delivery; names and triage reasons escaped once; never message text; ids, codes, times and counts otherwise) |
|---|---|---|
| `delegation_escalation` | conversation or contact missing | Headline by `escalation_category_of(notification.priority, refs.alert_kind)`: `urgent_decision` "⚠️ <b>{name} needs your decision</b> (delegated chat)"; `needs_owner` "<b>{name} needs you</b> (delegated chat)"; `suspicious` "<b>{name} sent something suspicious</b> (delegated chat)"; `fyi` "For your information: {name} (delegated chat)"; unknown → the `needs_owner` line. Then up to 3 triage reasons. Then the outcome line from the first `action_ids` entry (account-scoped). For a `delegation_auto` action: `proposed`/`approved`/`scheduled` → "An automatic reply is scheduled; it is checked again before it goes out."; `executing` → "An automatic reply is being sent."; `succeeded` → "An automatic reply was sent."; `delivery_uncertain` → "An automatic reply may or may not have arrived."; any other state → "No automatic reply was sent." For a held reply (`agent_proposal`, OD-11): `awaiting_approval`/`revising` → "A reply was written but not sent; tap Draft reply to see it."; `blocked` → "A reply was written; it cannot be sent from here, and Draft reply shows it to copy."; any other state → "Nothing was sent." No action → "Nothing was sent." (plus, when the Draft reply button is shown, "Tap Draft reply to have a draft written for you to approve."; never on a `suspicious` escalation, which shows no Draft reply, P3-T153). Then: "Open the chat to answer yourself; your own message there pauses automatic replies in that chat." Buttons: Draft reply (when shown, above), Pause |
| `delegation_paused_llm` | delegation missing or no longer active | "⏸ <b>Automatic replies are paused</b> for your delegation {until <local end> \| (standing)}: {the AI service is not answering \| this month's AI budget is used up}. New messages in its {n} chat(s) get no automatic reply; each chat with a new message gets an alert with a Draft reply button instead (OD-11). Automatic replies start again by themselves when {the AI answers again \| budget is available again}." |
| `autonomy_not_sent` (OD-11) | conversation or contact missing; for a held reply: the action (account-scoped, `refs.action_ids[0]`) is missing, has no text, or is no longer `awaiting_approval`, `revising` or `blocked` (the owner already acted, or a takeover, a new message or expiry ended it) | Held reply: "🤖 <b>Automatic reply not sent</b> — {name}'s chat (delegated). {reason} Tap Draft reply to see the reply it wrote and approve, edit or reject it." then "Sendable until {owner-local HH:MM}" (the card's `sendable_until` rule: `min(expires_at, window close − margin)`); for `blocked`: "…It cannot be sent from here; Draft reply shows the text to copy." `{reason}` by `refs.alert_kind`: an `AUTONOMY_CODES` key → its explanation; `late` → "It could not be sent in time."; `shadow` → "Automatic sending is switched off, so it was kept for you."; `not_sent` or unknown → "A check did not pass." Outage (no `action_ids`): "🤖 <b>No automatic reply</b> — {name} wrote in a delegated chat, and {the AI service is not answering \| this month's AI budget is used up}. Tap Draft reply to have a draft written when the AI is available." Never message text and never the reply's text. Buttons: Draft reply, Pause (no feedback buttons: `feedback_buttons` is false; delivery adds Open chat) |
| `chat_possibly_paused` | conversation or contact missing, or the chat's `availability` is back to `active` | "<b>{name}'s chat may be paused or excluded</b> in Telegram's Chat Automation: an automatic reply could not be delivered. Automatic replies there stop until {name} writes again." |
| `autonomy_volume_cap` | conversation or contact missing | "<b>Automatic replies stopped in {name}'s chat.</b> {AUTONOMY_CODES[alert_kind].explanation, or "An automatic-reply limit was reached." for an unknown code} New replies there wait for your approval as drafts." |

Truthful wording (invariant 11, RG-20): "scheduled", "being sent", "may or may not have arrived" and "waits for your approval" are never "sent".

### 3.6a The Draft reply press (`telegram/control/draft_reply.py`, OD-11)

`DraftReplyPress(*, presenter: ApprovalCardPresenter | None, actions_config: ActionsConfig, monthly_budget_usd: float | None)`, registered as the `drf` callback (`ControlRoutes.callbacks["drf"]`). It follows the control-chat rules (P3-interfaces §12.2): all database work in the press transaction, the press answered and every Telegram call made after commit through the gateway (`gateway.answer_callback`), never a shortcut (AGENTS §5, §7). It is a control-chat handler only: business updates never reach it (invariant 5). Order:

1. **Card:** look up (never claim) the `pending_inputs` row of kind `drf` with this token, of the presser's account, not expired. Missing, expired, another account's or an unknown op ⇒ `CALLBACK_EXPIRED_TEXT` ("no longer active"), nothing else.
2. **Notification:** the row `ref_id` names, of this account (invariant 12). Missing (its chat's data was deleted) ⇒ `CALLBACK_EXPIRED_TEXT`. Read `conversation_id`, `refs.action_ids`, `refs.delegation_ids`, `refs.message_ids`.
3. **Locks (§9.4):** account `FOR SHARE`, then the conversation `FOR SHARE` (as `approvals.py::_lock`). Conversation gone ⇒ `CALLBACK_EXPIRED_TEXT`.
4. **Stored text** — `refs.action_ids[0]` is an action of this account and chat with origin `agent_proposal`, `delegation_id` set and `draft_flags["presentation"] == "on_request"` (a held reply):
   - Card already shown (`card_message_id` set) ⇒ answer "The draft is in its card." and `presenter.sync_after_commit` (no new row).
   - Otherwise insert the audit row `approval_card_requested` (subject the action, actor `owner`, `details = {"notification_id", "version"}`) and `presenter.present_after_commit(session, action_id)`. Answer by state: `awaiting_approval` or `revising` ⇒ "Here is the draft."; any other state ⇒ "This reply can no longer be sent ({REASON_SHORT of its `state_reason`}); its card has the text to copy." (the card renders the truthful state and offers copy and, where M04 does, Re-draft).
   - **No model call** on this path, ever; nothing changes the action's state, text, version or expiry. `presenter is None` (a process without the presenter) ⇒ the row is still written and the answer is "The draft will appear here shortly." (M04's `system.sync_action_cards` presents it within about a minute, because the row releases it to the sync).
5. **No stored text** (no `action_ids`, or the named action is not a held reply). Checks, in order, each with a factual answer and no draft:
   - the notification is a `delegation_escalation` whose category (`escalation_category_of`) is `suspicious` (a token from an older renderer, or a forged one) ⇒ "No draft is written for a suspicious message; open the chat to answer yourself." (P3-T153);
   - a `delegation_auto` action of this chat and delegation with `base_message_id` in `refs.message_ids` that is `proposed`, `approved`, `scheduled`, `executing`, `succeeded` or `delivery_uncertain` ⇒ "An automatic reply is scheduled or was sent, so no draft was written.";
   - the conversation is in Ignore mode or the contact is a bot ⇒ "No drafts are made for this chat.";
   - the reply window is closed (`core.reply_window.is_open(last_contact_msg_at, now)` false) ⇒ "The 24-hour reply window has closed, so no reply can be sent.";
   - `not await draft_consent_ok(session, account_id, actions_config.draft_route_model)` ⇒ "AI processing is off for this chat, so no draft can be written.";
   - `llm_outage(session, account_id=…, now=now, monthly_budget_usd=…)` (§3.5) returns an outage ⇒ "The AI service is not answering, so no draft can be written now. Try again later." or "This month's AI budget is used up, so no draft can be written now.";
   - a live (`awaiting_approval` or `revising`) `agent_proposal` card of this chat whose `shown_through_message_id` is at least the chat's newest contact message id ⇒ "A draft for this chat already waits for your approval." and that card is synced (if it is an unrequested held reply, step 4's request row is written and it is presented instead);
   - otherwise `request_draft(session, account_id=…, conversation_id=…, reason=DraftReason.ALERT_DRAFT, scheduler=…, clock=…, debounce=…)` (P3-interfaces §7.1: deferred in a SAVEPOINT with the `redraft:{conversation_id}` queueing lock). A queued job already holds the lock (the savepoint rolled back) ⇒ "A draft is already being written."; else ⇒ "Writing a draft; if it can't be written, I'll tell you here."
   The `replies.draft` job then re-checks consent and the budget when it runs (invariant 9), drafts with the tool-less `draft_reply` run (invariant 3a) and presents an ordinary owner-approved card (no AI line, PRD §11.4; it survives `/pause`, P2-T23, OD-31). If it ends without a card (a check fails, the model fails or refuses, over budget, consent missing, no longer needed), F0-S's settle step sends the owner one line, "Couldn't write a draft for {contact}: {reason}.", once per job (P3-interfaces §7.1, P3-T154); this unit adds nothing for it and only words the press answer so it stays true. One press makes at most one model call, and only on this path.
6. The handler writes nothing else: no action, no state transition, no notification. A press never sends anything into a business chat (invariants 1, 2): approving the card it shows is the M04 Approve path, which goes through policy and Tx A as for any card.

### 3.7 Wiring (`_wire_automation` lines; P3-interfaces §15.3)

Function-local imports only. Always: register the five renderers on `ctx.notification_delivery` (the `autonomy_not_sent` renderer takes a `Clock` for its `drf` card); add `callbacks={"drf": DraftReplyPress(presenter=ctx.approval_cards, actions_config=ctx.actions_config, monthly_budget_usd=ctx.settings.llm_monthly_budget_usd)}` to the `ControlRoutes` fragment (OD-11; never flag-gated, because an alert already delivered must keep working, P3-interfaces §10.2); call `ctx.notification_delivery.use_delegation_facts(enabled=ctx.autonomy_config.run_enabled)`. Only when `autonomy.run` is on: return `P3Wiring(escalation=EscalationJobHandlers(delegation_health=DelegationHealth(session_factory=ctx.session_factory, clock=ctx.clock, scheduler=ctx.scheduler, monthly_budget_usd=ctx.settings.llm_monthly_budget_usd).run))`. The triage store already receives the requester from F0-R's wiring. `delegation_paused_llm` rows are enqueued with `alert_class=SYSTEM` (F0-R spec §3.8 step 3: the class table alone does not lift the `notifications.deliver` gate); `autonomy_not_sent` rows are enqueued with `alert_class=SYSTEM` too (OD-11a), by every producer.

### 3.8 Edge cases (each has a test in §5)

- The delegation ends between enqueue and delivery: `active_delegation` is false at delivery, so the alert follows the M05 rules (a `needs_owner` escalation is `HIGH`: immediate outside quiet hours, batched `quiet_hours` inside them).
- A decision request in a covered chat during quiet hours with the delegation's bypass off: the triage alert is batched for quiet hours (M05 rule for `HIGH`) and the urgent escalation is deferred to the end of quiet hours; when the escalation is delivered, the guard sees no sent sibling and it goes out; the later triage delivery is batched as `already_alerted`.
- Both siblings queued at once: the first delivered wins; the second sees it `sent`/`uncertain` and is batched.
- A burst whose newest contact message arrived after triage covered it: the message-id sets may not overlap, so both alerts may go out (a new message is new information).
- An outage that starts and ends within 10 minutes: no notice, no outage alert.
- A run with no traffic during an outage: the budget outage is still reported (deterministic); an unavailable-provider outage needs recent failures as evidence.
- `delegation_auto` failed with `peer_unavailable` and the contact wrote again before the health run: availability is `active` again, so no notice (and a queued notice renders `None`).
- A foreign account's delegation, chat or notification never affects another account's facts or health rows (RG-21).
- OD-11: a Draft reply press on a held reply whose card is already shown; pressed twice; pressed after the owner approved, rejected or edited the card from the escalation alert's button; pressed after a takeover outdated the held reply or the reply window closed (the card shows the truthful state, copy-ready); a `blocked` held reply; a press on an outage alert while the outage lasts and after it ends; a press while a `replies.draft` job is already queued; a press when a live card already covers the burst; a press after consent was revoked; a press on an escalation whose automatic reply was sent; a press from another account's token; a press after the chat's data was deleted; a press in a process without the presenter. A suspicious escalation (no Draft reply; an old token answers factually, P3-T153). A burst that both escalates and holds a reply: both alerts are delivered and both Draft reply buttons open the same card idempotently (P3-T155).

### 3.9 Failure modes (fail closed)

- With `autonomy.run` off or no requester wired, every triage effect and every delivery decision is exactly as after F0-R: no alert is held back for a chat nothing autonomous handles.
- A renderer whose source is gone returns `None` (suppressed, never an alert about deleted data).
- An escalation whose category cannot be read (no `alert_kind`, an unexpected priority) renders the generic `needs_owner` text and counts as not suspicious (no repeat upgrade); a volume-cap notice without a code renders the generic limit line.
- A failing fact query fails the delivery job (the M05 retry and reconciler recover); it never defaults to "covered".
- OD-11: a Draft reply press that cannot do what it says answers why (expired, gone, window closed, consent off, AI down, budget used up, already written, already queued) and changes nothing; it never answers "Here is the draft." without a held reply to show, never makes a model call for stored text, and never defers a draft the job would refuse for consent (the job re-checks anyway). A lost presentation after the request row is recovered by M04's sync.

## 4. Files

**Owned (P3-interfaces §15.1):** `inbox/notification_policy.py`; `inbox/notifications.py` (fact loading and the `use_delegation_facts` setter only); `inbox/triage_store.py` (effects); `inbox/delegation_alerts.py` (new); `telegram/control/draft_reply.py` (new, OD-11); `jobs/tasks_escalation.py` (body adjustments only); lines inside `_wire_automation`; tests in §5. M6-ESC is the **single owner of triage effects** after F0-R: M6-PLAN supplies only the requester (through `P3Ports`), and no other unit edits `inbox/triage_store.py` (a needed change there is a request to E1, or an ICR).

**Never edit:** `inbox/notification_types.py`, `core/*` (F0-S/F0-R; a missing kind or field is an ICR); `inbox/alert_texts.py` (renderers register on the delivery service); `actions/*` and `policy/*` (the escalation producer and the volume-cap code are M6-PLAN's and M6-POL's; the press only calls `actions/drafts.py::request_draft` and reads `actions` rows); `telegram/control/approvals.py` (the press uses the `ApprovalCardPresenter` port; the held-reply sync rule is M6-PLAN's); `agent/drafting.py` (call `draft_consent_ok` only); `automation/delegations_repo.py`; `conversations/service.py` and `actions/executor.py` (they set `availability`; this unit only reads it); every other file on P3-interfaces §15.2; another team's wire function.

## 5. Tests (red first)

### 5.1 Named tests (IMPLEMENTATION_PLAN §8 S06.3; RG list)

| Test | File | Covers |
|---|---|---|
| `test_decision_request_triggers_an_immediate_alert` | `tests/integration/test_m6_escalation.py` | "decision request → immediate alert" (J5, PRD R2 #1): `TriageRepository.apply` on a decision verdict in a covered chat, then delivery through `FakeChannel` sends at once |
| `test_llm_down_ten_minutes_gives_one_notice_per_delegation_and_escalation_only` | `tests/integration/test_delegation_health.py` | "LLM down 10 min → one notice, drafts/escalation only" (PRD §15, OD-11): two active delegations, failed runs for 11 min; one `delegation_paused_llm` each; a second run adds none; a contact message after `since` gives one `autonomy_not_sent` outage alert (`alert_class = system`, OD-11a; `outage:` key, `alert_kind` = the cause, a Draft reply button) per chat; no action row. The name is the S06.3 name and is kept |
| `test_one_draft_path_per_burst` | `tests/integration/test_m6_escalation.py` | requester `True` ⇒ no Assist draft request in an Assist-mode covered chat; requester `False` ⇒ the Assist draft request as today |
| `test_no_double_alert_in_a_covered_chat` | same | no `triage_needs_reply` when delegated; a `triage_decision` sent and a `delegation_escalation` for the same burst ⇒ the second is batched `already_alerted` |
| `test_rg20_the_escalation_alert_never_reports_scheduled_or_uncertain_as_sent` | `tests/integration/test_delegation_alert_texts.py` | RG-20 (invariant 11); extended to the `autonomy_not_sent` texts (never "sent") |
| `test_draft_reply_on_a_held_reply_shows_its_card_with_no_model_call` | `tests/integration/test_draft_reply_press.py` | OD-11, the reuse path: a seeded held reply and its alert; press `drf:<token>:dr` ⇒ one `approval_card_requested` row, the presenter's `present_after_commit` called once for that action, the card shows the same text with Approve / Edit / Reject and "Sendable until", no `AI_MARKER`; `FakeLLM` saw zero calls; the action's state, text, version and expiry unchanged |
| `test_draft_reply_without_stored_text_asks_for_one_alert_draft` | same | OD-11, the write path: an escalation alert with no action ⇒ exactly one `replies.draft` job with `reason = "alert_draft"` and the `redraft:{conversation_id}` queueing lock; a second press answers "A draft is already being written." and defers nothing; running the job with `FakeLLM` makes one call and one ordinary `agent_proposal` card (no `delegation_id`, no `presentation` key) |
| `test_rg08_a_draft_reply_press_never_drafts_without_consent` | same | RG-08 extension (invariant 9): consent revoked ⇒ factual answer, no job; consent revoked after the job was queued ⇒ the job ends `no_consent` with zero model calls |
| `test_rg21_a_draft_reply_token_never_works_across_accounts` | same | RG-21 (invariant 12): account B pressing account A's token ⇒ "no longer active", no row, no job, no presentation |
| `test_rg10_escalation_paths_log_no_content` | `tests/integration/test_log_hygiene_m6_esc.py` | RG-10: canaries in contact names, triage reasons and message text through the effects, the health job and every renderer |
| `test_rg21_delegation_facts_and_health_never_cross_accounts` | `tests/integration/test_delegation_health.py` | RG-21 (invariant 12) |

### 5.2 `tests/unit/test_covered_chat_delivery.py` (pure `decide_delivery`)
- `test_a_decision_request_in_a_covered_chat_is_immediate`
- `test_an_urgent_triage_alert_in_a_covered_chat_is_immediate`
- `test_an_urgent_delegation_escalation_is_immediate`
- `test_an_urgent_escalation_bypasses_quiet_hours_only_with_the_delegation_flag_or_the_global_bypass`
- `test_needs_owner_fyi_and_a_first_suspicious_escalation_are_batched_for_the_catch_up`
- `test_needs_reply_in_a_covered_chat_is_batched_even_for_a_vip_contact`
- `test_a_repeated_suspicious_alert_in_a_covered_chat_alerts`
- `test_an_already_alerted_burst_is_batched`
- `test_a_low_priority_contacts_urgent_escalation_is_not_demoted`
- `test_a_repeated_suspicious_escalation_is_raised_to_high`
- `test_without_an_active_delegation_every_kind_follows_the_f0r_rules` (table)
- `test_volume_cap_and_chat_paused_notices_follow_the_standard_rules`
- `test_delegation_paused_llm_is_system_and_never_held`
- `test_autonomy_not_sent_is_system_and_delivered_at_once_in_quiet_hours` (OD-11a: never held, capped, batched or gated by `notifications.deliver`)

### 5.3 `tests/integration/test_m6_escalation.py` (real Postgres, `FakeClock`, `FakeChannel`, fake requester)
- the three named tests of §5.1 for this file
- `test_a_suspicious_burst_never_starts_the_delegated_path`
- `test_the_requester_result_is_audited_as_delegated`
- `test_with_autonomy_run_off_triage_effects_and_delivery_are_unchanged` (delivery facts disabled, requester `False`)
- `test_active_delegation_is_loaded_from_active_covering` (ended, not started, superseded and foreign delegations ⇒ false)
- `test_an_alert_delivered_after_the_delegation_ended_follows_the_m05_rules`
- `test_a_deferred_urgent_escalation_and_a_batched_triage_alert_alert_once`

### 5.4 `tests/integration/test_delegation_health.py`
- the two named tests of §5.1 for this file
- `test_budget_used_up_for_ten_minutes_gives_one_notice_per_delegation`
- `test_an_outage_shorter_than_ten_minutes_gives_no_notice`
- `test_a_success_after_the_failures_ends_the_outage`
- `test_a_new_outage_notifies_again` (new `since`, new dedupe key)
- `test_failures_older_than_ten_minutes_with_none_since_are_not_an_outage`
- `test_the_outage_alert_reaches_the_owner_even_when_the_planner_escalation_was_batched` (OD-11: a pre-existing M6-PLAN `needs_owner` escalation under `deleg:…:{m}`, batched `delegation_catch_up` ⇒ the outage `autonomy_not_sent` under `outage:…:{m}` is still inserted and delivered at once, also inside quiet hours and with `notifications.deliver` off, never batched (system, OD-11a); a second run adds none)
- `test_chat_possibly_paused_is_told_once_per_episode` (one notice; a second run none; two failed sends in one episode, one notice; after a new contact message and a new failure, a second notice)
- `test_paused_llm_notices_are_system_alerts_delivered_with_notifications_deliver_off`
- `test_a_resolved_pause_gives_no_notice`
- `test_the_paused_notice_writes_one_audit_row_with_its_cause`

### 5.5 `tests/integration/test_delegation_alert_texts.py`
- the RG-20 named test
- `test_each_renderer_returns_none_when_its_source_is_gone`
- `test_names_and_reasons_are_escaped_once` (a name with `&` and `<`: one escape after the gateway sanitizer)
- `test_the_escalation_and_volume_cap_alerts_carry_the_pause_button` (the button equals `core/autonomy_texts.py::pause_button()`; R45)
- `test_delegation_alerts_never_import_telegram` (AST scan of `inbox/delegation_alerts.py`; import-linter also covers it)
- `test_the_category_is_read_from_alert_kind_or_derived_from_the_producer_priority`
- `test_the_volume_cap_text_uses_the_policy_explanation_or_a_generic_line`
- `test_the_paused_notice_names_the_owner_local_end_and_the_cause`
- `test_renderers_show_no_message_text` (a canary in the contact's message, and in a held reply's text, never appears in any rendered text)
- `test_the_not_sent_alert_names_the_reason_and_the_sendable_until_time` (OD-11: each `alert_kind` family: an autonomy code, `late`, `shadow`, `not_sent`, an unknown value, the two outage causes; `blocked` wording)
- `test_the_not_sent_alert_is_suppressed_once_the_held_reply_is_settled` (approved, rejected, outdated, expired or deleted before delivery ⇒ `None`)
- `test_the_not_sent_alert_carries_draft_reply_and_pause_and_no_feedback_buttons` (`feedback_buttons` false: Draft reply and Pause plus delivery's Open chat, no Useful or Not useful; the `drf` card's `ref_id` is the notification and its TTL ends at the reply window's close less the margin)
- `test_the_escalation_alert_offers_draft_reply_only_when_no_automatic_reply_is_pending_or_sent` (no action, a held reply, a cancelled `delegation_auto` ⇒ shown; a scheduled, executing, succeeded or uncertain `delegation_auto` ⇒ hidden)
- `test_a_suspicious_escalation_alert_has_no_draft_reply_button` (P3-T153: `alert_kind = suspicious` with no action ⇒ Pause only, no `drf` card created; the same burst state with `needs_owner` ⇒ Draft reply shown)

### 5.6 `tests/integration/test_draft_reply_press.py` (OD-11; real Postgres, `FakeClock`, `FakeChannel`, `FakeLLM`, a spy presenter)
- the four named tests of §5.1 for this file
- `test_a_second_press_on_a_shown_held_reply_only_syncs_its_card`
- `test_a_press_after_the_held_reply_was_outdated_shows_the_truthful_copy_ready_card` (takeover and window-closed variants: the answer names the reason; no model call)
- `test_a_press_on_a_blocked_held_reply_shows_its_copy_ready_card`
- `test_a_press_while_the_outage_lasts_answers_factually_and_defers_nothing` (both causes); `test_a_press_after_the_outage_ended_asks_for_one_draft`
- `test_a_press_when_a_live_card_already_covers_the_burst_writes_no_new_draft`
- `test_an_escalation_press_while_an_unrequested_held_reply_covers_the_burst_shows_that_card` (the escalation's `refs.action_ids` does not name the held reply ⇒ one `approval_card_requested` row for it, `present_after_commit` called once, answer "Here is the draft.", no model call; never "already waits" with nothing shown, invariant 11)
- `test_a_press_on_an_escalation_whose_automatic_reply_was_sent_writes_no_draft`
- `test_a_press_on_a_suspicious_escalation_writes_no_draft` (P3-T153: a `drf` token seeded on a suspicious escalation ⇒ the factual answer, no job, no model call)
- `test_a_burst_that_escalates_and_holds_a_reply_opens_one_card_from_either_button` (P3-T155: both alerts delivered; pressing the escalation's and then the not-sent alert's Draft reply ⇒ one `approval_card_requested` row, one presentation, then a sync; no model call)
- `test_the_write_path_answer_promises_a_line_if_no_draft_comes` (the answer is "Writing a draft; if it can't be written, I'll tell you here."; the line itself is F0-S's test, P3-T154)
- `test_a_press_after_the_window_closed_or_in_an_ignore_or_bot_chat_writes_no_draft`
- `test_a_press_after_the_chats_data_was_deleted_is_no_longer_active`
- `test_a_press_without_a_presenter_releases_the_held_reply_to_the_sync` (the next `sync_cards` presents it)
- `test_an_approved_held_reply_survives_pause_and_is_sent_once_without_the_ai_line` (OD-31, invariant 13: shown via the press, `/pause`, Approve, `actions.execute` ⇒ one send, no `AI_MARKER`, `disclosure_included` false)
- `test_the_press_answers_through_the_gateway_after_commit` (no answer when the transaction rolls back; AGENTS §7)
- `test_draft_reply_logs_ids_only` (folded into `test_rg10_escalation_paths_log_no_content`, which also drives the press)

## 6. Acceptance criteria

1. In a covered chat with `autonomy.run` on: a decision request alerts at once; `needs_owner`, `fyi`, a first suspicious alert and every needs-reply alert are batched `delegation_catch_up`; a repeated suspicious alert alerts; the same burst never alerts twice.
2. An urgent escalation passes quiet hours only with the delegation's bypass or the owner's global urgent bypass; otherwise it is held to the end of quiet hours.
3. A burst gets the delegated path or the Assist path, never both; a suspicious burst gets neither.
4. An outage of more than 10 minutes (AI unavailable or budget used up) produces one `delegation_paused_llm` per active delegation and one `autonomy_not_sent` alert with a Draft reply button per covered chat's newest new contact message (delivery class `system`, OD-11a: at once, even in quiet hours); no action is created (OD-11).
4a. OD-11: every `autonomy_not_sent` alert and every escalation alert with no pending or sent automatic reply carries Draft reply, except a suspicious escalation (P3-T153); pressing it on a held reply shows its existing card with no model call; with no stored text it defers at most one `replies.draft` (`alert_draft`) after the press-time checks; every refusal is a factual answer.
5. A `delegation_auto` send refused with `peer_unavailable` produces one `chat_possibly_paused` per episode.
6. With `autonomy.run` off, behaviour equals F0-R's (the whole M05 notification suite unchanged).
7. Rendered texts never call scheduled, uncertain or pending work "sent"; logs are content-free.
8. ARCHITECTURE §17 and §18 updated.
9. The escalation and volume-cap alerts carry the Pause button from F0-S's `pause_button()` (R45); M6-PAUSE's handler answers it before enable 3 (R48).

## 7. Docs updated in the same PR

- ARCHITECTURE §17: the covered-chat delivery rule (the table of §3.3), the `already_alerted` guard, the reason codes `delegation_catch_up` and `already_alerted`, the five renderers (OD-11 adds `autonomy_not_sent`), the Draft reply press, and `system.delegation_health`.
- ARCHITECTURE §18 (degraded modes): "AI unavailable or budget used up > 10 min → one `delegation_paused_llm` per active delegation; each covered chat with a new contact message gets an `autonomy_not_sent` alert with a Draft reply button (OD-11)"; "chat possibly paused → one notice per episode; autonomous sends stop there until the contact writes again (M04 precondition)".
- PR description: the decisions for M06.md (suspicious bursts never start the delegated path; the global urgent bypass also applies to delegation escalations; batched alerts still reach the digest) and the open items.

## 8. Owner decisions

- **OD-7** (named chats; urgent decisions at once; the quiet-hours bypass per delegation, off by default): §3.3. If the owner wanted the bypass global only, the `bypass_quiet_hours` flag on escalations would be dropped and only the global urgent bypass would apply.
- **OD-11** (answered 2026-10-29, changed from the default): when autonomy does not send, the owner gets an alert offering a draft (`autonomy_not_sent`, §3.6) and the escalation alert offers it too; Draft reply reuses stored text with no model call or writes one draft (§3.6a). AI down or out of budget > 10 min ⇒ each delegation reported once, and its chats fall back to alerts with the Draft reply button (§3.5). A different threshold changes only `LLM_OUTAGE_AFTER`.
- **OD-11a** (answered 2026-10-29): `autonomy_not_sent` is class `system`, always delivered at once (§3.3 item 4, §3.5); PRD §14 clarified. **OD-11b** (answered 2026-10-29): PRD §15's degraded-mode row now says the chats "fall back to alerts that offer a draft for approval (Draft reply) or escalation only", which §3.5 implements.
- **Owner, 2026-10-29 (P3-T153):** a suspicious escalation offers no Draft reply (§3.6, §3.6a).
- **OD-17** (delivery classes): consumed as F0-S wrote them; this unit adds no class.
- The rule "decision requests alert at once in a covered chat" follows PRD J5 and R2 #1; it reads P3-interfaces §11.3's "other than `urgent_decision`" as covering `triage_decision` too (§9 note 2).

## 9. Risks and notes

1. **Category and code on producer rows (ruled, R60).** M6-PLAN sets `refs.alert_kind` to the escalation category on every `delegation_escalation` and to the failed volume code on `autonomy_volume_cap`; contract §11.2 names it. The one-to-one priority mapping stays only as a fallback for a row without the key, and a volume-cap row without it renders the generic line.
2. **Decision requests (ruled, R60).** `triage_urgent` and `triage_decision` stay immediate in covered chats (J5: a decision request triggers an immediate alert); the `already_alerted` guard prevents a second alert for the same burst. Contract §11.3 follows this.
3. **Silent invalid drafts.** Because a registered delegated path suppresses `triage_needs_reply`, a delegated plan that ends `NOT_DRAFTED` with draft status `refused`, `invalid` or `failed` must itself enqueue a `needs_owner` escalation (PRD §15: "model output invalid → draft or escalation"). The M6-PLAN spec does (its §3.6 step 3); P3-interfaces §4.1 should say so too (reported). Over-budget and provider outages are covered here by §3.5.
4. **`active_delegation` and `autonomy.run`.** Holding back alerts while `autonomy.run` is off would hide them with nothing handling the chat; hence `use_delegation_facts` (reported as a refinement of §11.3).
5. **Digest and catch-up both list batched alerts.** Acceptable: the digest covers standing delegations (no end, no catch-up) and anything after the delegation ends; the catch-up gives the per-delegation view.
6. **Outage detection is evidence-based.** It needs failed runs (or the budget arithmetic); a quiet period during an outage produces no notice until traffic fails. The 24 h window bounds the queries.
7. **Pause button (ruled, R45).** G1 is closed: F0-S's pure `pause_button()` is the one source; this unit adds no seam. Enable 3 requires M6-PAUSE's handler to be merged (R48).
8. **Outage fallback (R60, revised by OD-11).** During an outage M6-PLAN cannot draft. The default answer had §3.5 enqueue a deterministic `delegation_escalation` with M6-PLAN's key; but the planner's failed draft usually takes that key first with a `needs_owner` escalation that a covered chat batches, so the owner would get no deliverable alert. Under OD-11 §3.5 enqueues an `autonomy_not_sent` under its own `outage:` key instead, with the Draft reply button; M6-ESC no longer produces `delegation_escalation`.
9. **ARCHITECTURE §17 (OD-11):** add the `autonomy_not_sent` alert (class `system`, OD-11a: the owner chose that these alerts reach them at once), the Draft reply button and the `drf` press (reuse without a model call; `ALERT_DRAFT` otherwise) to this unit's §17 text.
10. **Merge order with M6-PLAN.** If M6-PLAN merges first, F0-R's hook calls its requester but ignores the result, so with `autonomy.run` on a covered chat gets both draft paths on `int/p3` until M6-ESC merges. Production is unaffected (`autonomy.run` stays off until enable 3, after both merge); any QA scenario that asserts one draft path per burst stays strict-xfail until M6-ESC lands.
11. **A burst that escalates and holds a reply (decided, P3-T155).** Both messages are kept: the `autonomy_not_sent` alert (at once) and the escalation (FR-TRI-03 rules). Both Draft reply buttons open the same held reply's card idempotently; no merge or suppression rule is added.
12. **A failed alert draft (decided, P3-T154).** The line that tells the owner is F0-S's, in `agent/drafting.py`; this unit only keeps the press answer truthful ("if it can't be written, I'll tell you here").
