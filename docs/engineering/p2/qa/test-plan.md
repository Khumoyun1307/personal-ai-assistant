# P2 test plan (QA)

*Owner: Antony (QA). Reviewed by the TL. How P2 is tested, what QA checks on every PR, the independent acceptance scenarios for M04 and M05, release verification, trial support, eval operations and where evidence goes. It adds P2 specifics to IMPLEMENTATION_PLAN §10 and ARCHITECTURE §16; those stay authoritative.*

## 1. Scope and layers

| Layer | What P2 adds | Where | Gate |
|---|---|---|---|
| Unit | Policy rules and `decide` (table-driven), action state machine (incl. hypothesis), schedule maths (DST), notification policy, draft/triage sanitisers, renderers | `tests/unit` | CI on every PR |
| Integration | Migrations 0013/0014 up/down, deletion cascade and retention for new content columns, proposal service, execution protocol (Tx A/Tx B, barrier, `mark_uncertain`), interleavings, card presses, jobs and reconcilers, triage/task store, notifications delivery, automations ledger | `tests/integration` (real Postgres 18) | CI |
| Contract | Gateway request shapes for business sends (`business_connection_id` present for `BusinessChat`, absent for `ControlChat`), error classification of `PEER_ID_INVALID` / `BUSINESS_PEER_INVALID` / `BUSINESS_CONNECTION_INVALID` | `tests/contract`, `tests/unit/test_gateway_and_render.py` | CI |
| Scenario | Independent acceptance journeys QA-S4 (M04) and QA-S5 (M05), §4–§5 | `tests/scenarios/test_m04_*.py`, `test_m05_*.py` | CI |
| Evaluation | E2 baseline (informational), E3 gate + model comparison, retrieval agent-mode re-runs after each `owner_request` bump | `evals/` | Manual, owner go-ahead, USD 2 phase cap |
| Manual live | M04 and M05 live windows on Alviro, 7-day trial, 5 digest days, friends' session, restore test, LV-08 reopen | [live-checklist-M04.md](live-checklist-M04.md), [live-checklist-M05.md](live-checklist-M05.md), OPS specs | CP3, CP5 |

Rules from AGENTS §11 apply unchanged: real Postgres, no real Telegram or model calls in tests, deterministic (FakeClock, barriers, no sleeps), red first, never the full suite locally (CI is the gate), one pytest process per compose project.

**QA-P kickoff (2026-10-27):** the owner authorised the P2 team to start; IMPLEMENTATION_PLAN §1.4 records "M03 OK" and the end of its fix round. This is a planning review only: every P2 scenario and regression extension remains **planned** until its test PR lands. QA-S4/QA-S5 start at CP1, on F0's merged contracts. The phase go-ahead does not substitute for a specific production-window or live-eval go-ahead.

## 2. Test infrastructure to use

| Piece | Where | Use it for |
|---|---|---|
| `FakeTelegramSender` | `tests/fakes/sender.py` | Records every call's exact kwargs (`sends`, `edits`, `keyboard_edits`, `answered_callbacks`; F0 adds business deletions). `fail_with` scripts real aiogram errors via `telegram_error(...)` / `network_error()`; `on_call` lets a test act "while Telegram is being called" (interleavings, lock checks) |
| `FakeLLM` | `tests/fakes/llm.py` | `complete` scripts (`text_response`, `tool_response`, `stop_response`, an `LLMError`, or an async hook); `parse` scripts in `fake.parsed` (a dict = the structured output, a `StopReason`, an `LLMError`, or a callable). `fake.requests` holds every request for isolation/consent assertions |
| `FakeClock` | `tests/fakes/clock.py` | All time; expiry, debounce, `mark_uncertain`, quiet hours, DST |
| `FakeChannel` | `tests/fakes/channel.py` | Control-channel output of owner runs |
| `RecordingApprovalPresenter` | `tests/fakes/approvals.py` (F0) | Asserting a card was presented/synced without the S04.3 UI |
| Business environment | `tests/integration/business_env.py` | `environment(...)`, `Env` (`factory`, `processor`, `sender`, `lookup`, `clock`, `dispatcher`, `scheduler`), payload builders (`business_message`, `deleted_messages`, `connection_update`, `callback`, `private_message`), `deliver`, `press`, `live_card`, `onboard`, `connect`, `grant_managed`, row readers |
| Agent environment | `tests/integration/agent_env.py` | `runtime(...)`, `ask(env, question)`, `gated(env, fake)`, `summarizer(...)`, `request_texts(request)` |
| P2 seed helpers | `tests/integration/p2_factories.py` (F0) | Account-scoped seeding of actions (any state, any origin incl. `delegation_auto`), tasks, triage verdicts, automations, notifications — for states the UI cannot reach directly |
| Scenario drive helpers | `tests/scenarios/p2_drive.py` (QA-S4, owned by Antony) | `run_queued(env, task_name, *, limit=None)` — runs queued procrastinate jobs of one task through the registered task functions, in order, as a worker would; `advance(env, seconds)` — moves the FakeClock and runs due periodic tasks named by the caller; `approve(env, action_id)`, `edit(env, action_id, text)`, `reject(...)` thin wrappers over `press`/`deliver` using the `act` callback grammar (F0-interfaces §7) |

**Red tests against F0 types.** QA-S4/QA-S5 are written before the features exist. Each scenario that cannot pass yet is marked `@pytest.mark.xfail(strict=True, reason="awaiting <WU>")`. CI stays green while it fails; when the unit makes it pass, strict xfail turns it into a CI failure, and the unit's author removes the marker in their PR. **Any unit — Junior units included — may remove the strict-xfail markers of the scenarios it turns green** (R13); removing the marker is the only edit a unit makes to Antony's scenario files. A scenario may be adjusted to the implemented API only through a QA-reviewed change that keeps its assertions.

Before adding an xfail marker, run the scenario and retain its failure reason. Collection, fixtures and F0 setup must work; an import error or broken helper is not evidence of the missing feature. List xfails by scenario and WU in the QA PR. An expected failure is not a passing acceptance check; CP3/CP5 require their milestone's scenarios to pass with no remaining xfails or skips. Interleavings use explicit barriers around the fake model/sender and database commits, not wall-clock timing.

## 3. QA review checklist (every PR, WORKFLOW §6.3 step 3)

Post each finding as a review comment labelled **blocking** or **non-blocking** (WORKFLOW §6.3). Blocking = an invariant, a spec requirement, a missing or failing required test, a correctness bug.

### 3.1 Safety invariants (AGENTS §6) — what evidence to look for

| # | Invariant | Evidence in the PR |
|---|---|---|
| 1 | Model proposes; application decides | No path to `send_business_text` except `actions/executor.py`; executor evaluates `decide(phase=EXECUTION)` in Tx A from locked rows; RG-18 tests |
| 2 | No tool sends | New tools are `propose`/`read`/`configure`/`control`; registry refuses `send_*`/`edit_*`/`delete_message*`/`mark_read*`/`reply*`; tool results say `awaiting_approval`/`blocked`, never "sent" (RG-17, RG-20) |
| 3a | Contact-triggered runs have no tools | `draft_reply`, `triage`, `evaluate_monitor` call `parse` with `tools=()`; a test asserts the request has no tools (RG-17 extension) |
| 3b | Tainted owner runs change no state without a tap | New configure/control tools go through the executor gate; tests with an injected tool result (RG-11 extension); `create_monitor` is `always_confirm` |
| 4 | Isolation | Context builders take one `conversation_id` and load memory only via `contact_facing`; a FakeLLM test asserts another conversation's canary never appears (RG-09) |
| 5 | Control/business boundary | No business update reaches `ControlRoutes` handlers; RG-03 stays green |
| 6 | At-most-once | Claim committed before the gateway call; ambiguous/transient → `delivery_uncertain`, never retried; Tx B guarded by `attempt_id`; RG-04/RG-05 |
| 7 | Fail closed | Unknown connection, `is_enabled=false`, missing `can_reply`, unknown conversation, missing consent, policy error ⇒ no send; tests for each |
| 8 | Telegram rules | Window checked before every send/edit; no initiation; copy-ready instead of late sends |
| 9 | Consent | New model calls go through `ConsentGatedLLMClient`; consent and budget re-checked when jobs run; a revoked-consent test per new run type (RG-08 extension) |
| 10 | Kill switch, takeover | `/pause` blocks autonomous-origin actions at claim; owner manual message outdates pending actions (RG-19) |
| 11 | Honest status | Cards, replies, activity and alerts distinguish done/awaiting/blocked/failed/uncertain (RG-20) |
| 12 | Tenant scoping | Every new query filters by `account_id`; a foreign-account test per new repository/tool (RG-21) |
| 13 | AI disclosure | Not live in P2 (no autonomous sends); approved drafts carry no disclosure (PRD §11.4) |
| 14 | Forbidden capabilities | Edit/delete only of `assistant_sent` (RG-12); nothing touching profile, gifts, Stars, stories |

### 3.2 Other checks

- **Logs content-free:** no message text, captions, names, usernames, draft text, tokens or URLs in new log calls; exceptions logged by type/code; RG-10 suite green; a new failure path gets a canary case when it can carry text.
- **Tenant scoping (RG-21):** every new query, tool and job handler scoped by `account_id`; foreign ids answer "not found" exactly like missing ones.
- **Red-first evidence:** the spec's named tests exist; the PR description (or a commit) shows they failed before the change; assertions check behaviour, not just "no exception".
- **Flags default off;** new settings in `.env.example`; behaviour with the flag off is tested where the flag gates a side effect.
- **Content-column obligations:** a new `text`/`jsonb` column is classified in `privacy/content_columns.py`; a content column has a §10.4 row, a sweep rule, a deletion-cascade step and canary tests in the same PR.
- **Migrations:** only revisions assigned by the schema steward; one Alembic head; up/down reverse test.
- **Jobs:** payloads are ids/enums; handlers reload and re-check; no `lock` on future-scheduled jobs; no row lock across a model or Telegram call; reconciler covers a lost defer.
- **Docs:** the ARCHITECTURE sections the spec names are updated; RG rows in the matrix updated.
- **One fix round (P2-T18):** after the author's fix round, new non-safety findings go to the milestone's open items with evidence and a suggested fix; QA does not hold the PR for them. A broken invariant is always blocking.

### 3.3 Checkpoint evidence

For each checkpoint, record the reviewed commit, CI run, exact test names, result counts (including skips/xfails), and open findings on the integration issue. A PR being green while its feature scenarios are expected failures does not satisfy a release gate.

| Gate | QA evidence required |
|---|---|
| CP1 / F0 | Both migration reversals and one Alembic head; content canaries for each deletion/retention scope; post-restore pending cancellation and executing → uncertain; unchanged ingestion/control behaviour; startup barrier reset before roles start |
| CP2 / send path | RG-05/06/12/18/19/20, the explicit-barrier race tests, confinement tests, process-run barrier tests and late-result tests; independent QA review and TL sign-off (RG-04 follows at CP3 with S04.3) |
| CP3 / M04 | Every §4 scenario passing without expected failures; RG-04; live checks with observed restart branch recorded; OPS-RT evidence; daily trial evidence for seven days before R1 exit |
| CP4 / M05 catches up | Existing system-alert callers retain audit/cooldown/once-ever behaviour through notifications; uncertain alerts never resent, including after notification retention; `triage.assist_trigger` stays off during the M04 trial |
| CP5 / M05 | Every §5 scenario passing without expected failures; live policy checks with actual settings and cap baseline; NFR-05 sample counts and p95s; five consecutive digest days; E3 result only after owner label confirmation |

CI provides automated evidence; calendar-day and owner-client checks remain separate entries. A skipped live step stays pending with its automated counterpart linked, rather than becoming a live PASS.

## 4. QA-S4 — M04 acceptance scenarios

Branch `qa/m04-scenarios` from `int/m04` → PR to `int/m04`. Each file below holds the listed tests; "Green by" names the unit whose code should make it pass (the xfail reason).

**Shared scripts.** A draft-run response is a `fake.parsed` dict with the `DraftResult` fields of the S04.2 spec, e.g. `{"needs_reply": True, "text": "P2 synthetic draft A", "confidence": "medium", "uses_only_permitted_information": True, "sensitive_topic": False, "makes_commitment": False, "should_escalate": False, … "language": "en"}` (exact field set per the S04.2 spec; P2-T08 revision fields included). An owner request that proposes a reply is `fake.queue(tool_response(("c1", "propose_reply", args(conversation_id=…))), text_response("Draft ready for your approval."))`. Telegram outcomes are scripted on `env.sender.fail_with` (`telegram_error(400, "Bad Request: PEER_ID_INVALID")`, `network_error()`, a 429 with `retry_after`). Contact and owner messages use `business_message(from_id=CONTACT|OWNER, date=T_BASE+n, message_text="P2 synthetic …")`.

| File | Test | Asserts | Green by |
|---|---|---|---|
| `test_m04_j4_draft_approve.py` | `test_j4_draft_edit_approve_sends_exactly_the_approved_text` | Owner asks for a draft → one card (contact, triggering message, draft, sendable-until) → Edit with replacement text → new version card → Approve → exactly one `send_message` with `business_connection_id` = current connection and text == version 2; `approved_text_hash == sent_text_hash`; card ends "Sent ✓" (PRD R1 #3) | S04.2, S04.3, S04.4 |
| | `test_j4_a_revision_instruction_creates_a_new_version_shown_for_approval` | Edit with an instruction → revision run → version 2 card; nothing sent until Approve | S04.3 |
| | `test_approve_during_revision_sends_nothing` | Edit starts a revision held at an explicit FakeLLM barrier; an old Approve press cannot claim or send; the completed revision needs a new approval token | S04.3 |
| | `test_a_revision_finishing_after_outdating_cannot_reactivate_the_card` | A contact message outdates the action while revision is paused; releasing the result cannot restore the card, change its text/version or send | S04.2, S04.3 |
| | `test_j4_reject_sends_nothing_and_says_rejected` | Reject → `rejected`, no `send_message`, card says Rejected | S04.3 |
| | `test_j4_owner_dictated_text_is_shown_exactly_with_the_resolved_recipient` | `propose_message` → card shows recipient and exact text; tool result `awaiting_approval` (R-05, RG-20) | S04.2, S04.3 |
| `test_m04_outdating_and_expiry.py` (PRD R1 #4) | `test_a_new_contact_message_outdates_a_pending_draft` | Card → contact message → `outdated`/`new_contact_message`; Approve answers "no longer active"; no send; card offers Re-draft | S04.2, S04.3 |
| | `test_an_owner_manual_reply_outdates_a_pending_draft` | Owner message in the chat → `outdated`/`takeover`; no send; nothing sent to the contact | S04.2 |
| | `test_deleting_the_source_message_outdates_the_draft` | `deleted_business_messages` of the base message → `outdated`/`source_deleted` | S04.2 |
| | `test_editing_the_source_message_outdates_the_draft` | Content-changing edit of the base message → `outdated`/`source_edited`; old token cannot send | S04.2 |
| | `test_switching_to_ignore_cancels_a_pending_draft` | Switch the conversation to Ignore while a card is live → `cancelled`/`ignore_mode`; old token cannot send | S04.2 |
| | `test_revoking_managed_consent_cancels_a_queued_ai_draft` | Approve an AI draft, revoke `ai_managed` before execute, then drain jobs → no business send; pending AI action cancelled; owner-dictated handling stays per S04.4 | S04.4 |
| | `test_removing_the_reply_right_blocks_an_approved_draft` | `business_connection` update without `can_reply` after Approve but before execute → Tx A `blocked`/`rights_changed` or `outdated`; zero sends; copy-ready shown | S04.4 |
| | `test_the_window_closing_expires_the_draft_with_copy_ready_text` | FakeClock past window close − margin → `system.expire_actions` → `expired`/`window_closed`; card has copy-ready text (copy button ≤ 256 chars) | S04.3 |
| | `test_a_contact_message_ingested_after_approval_blocks_the_send` | Approve committed, contact message ingested before `actions.execute` runs → `outdated` (`not_stale`); zero sends | S04.4 |
| `test_m04_j8_takeover.py` | `test_an_owner_message_dated_after_the_base_but_ingested_late_blocks_the_send` | Owner message with `date` > base message's but delivered after Approve → `no_takeover_since` fails in Tx A → `outdated`; zero sends | S04.4 |
| | `test_takeover_outdates_the_card_and_audits_without_messaging_the_contact` | Audit `takeover_detected`, `action_outdated`; no business send of any kind | S04.2 |
| `test_m04_j9_kill_switch.py` | `test_pause_blocks_an_autonomous_action_at_claim` | Seed (`p2_factories`) an `approved` `delegation_auto` action; `/pause`; run `actions.execute` → not sent; `cancelled` or `blocked`/`autonomy_paused` (RG-19) | S04.4 |
| | `test_a_stop_word_pauses_before_any_model_call` | Stop word → paused, `fake.requests` empty | existing (stays green) |
| | `test_a_model_resume_call_never_resumes` | Scripted `resume_autonomy` tool call (tainted and untainted) → only a Resume card; `accounts.autonomy_paused` stays true until the card or `/resume` | S04.4 |
| | `test_pause_does_not_block_an_owner_approved_send` | `/pause`, then Approve an owner-approved draft → sent once (PRD §12.2 #1) | S04.4 |
| `test_m04_r1_at_most_once.py` (PRD R1 #5) | `test_a_double_tap_on_approve_sends_once` | Two `act:<token>:ok` callbacks delivered in one batch and in two batches → one transition, one `send_message` (RG-04) | S04.3 |
| | `test_a_restart_between_claim_and_send_is_uncertain_and_never_resent` | Claim committed (`executing`), worker "dies" (no Tx B), FakeClock + `system.mark_uncertain` → `delivery_uncertain`; reconciler and re-run `actions.execute` → still zero further sends; owner alert `delivery_uncertain` (RG-05) | S04.4 |
| | `test_a_network_timeout_on_send_is_uncertain_not_retried` | `network_error()` → `delivery_uncertain`; no retry | S04.4 |
| | `test_a_rate_limited_send_is_retried_after_retry_after_and_sent_once` | 429 then OK → back to `approved` with `retry_at` (audit `action_rate_limited`), then one successful send (`action_claimed` twice, one `action_succeeded`) | S04.4 |
| | `test_a_late_result_after_mark_uncertain_settles_to_succeeded` | Tx B after `mark_uncertain` → `succeeded` (audit `action_late_result`), card updated | S04.4 |
| `test_m04_r1_assistant_sent.py` (PRD R1 #9) | `test_each_send_is_recorded_once_as_assistant_sent` | One `messages` row, `direction='assistant_sent'`, `action_id` set, from the send result | S04.4 |
| | `test_an_echo_processed_first_merges_into_the_sent_record` | Echo (`sender_business_bot` = bot) ingested before Tx B → one row | S04.4 |
| | `test_two_zero_id_sends_in_one_chat_both_succeed` | `message_id == 0` twice → both `succeeded`, `identity_pending`, no constraint error | S04.4 |
| `test_m04_j10_activity.py` (PRD R1 #6, FR-ACT-01/02) | `test_activity_lists_every_send_with_its_authorization` | Seeded mix of sent/rejected/expired/blocked/uncertain → `/activity` and `get_activity` list every one; each send shows who approved and when | S04.5 |
| | `test_drafts_and_sends_are_never_shown_alike` | Distinct labels for awaiting/sent/uncertain/blocked (RG-20) | S04.5 |
| | `test_catch_up_mentions_what_the_assistant_sent_and_drafted` | `summarize_period` over a range with actions → assistant activity block (handoff "Yours" item) | S04.5 |
| `test_m04_assist_bursts.py` | `test_a_burst_in_an_assist_chat_produces_one_card` | Three contact messages within the debounce → one `replies.draft` run, one card | S04.2 |
| | `test_no_card_when_the_draft_says_no_reply_is_needed` | `needs_reply=False` → no card, audit `draft_skipped` | S04.2 |
| | `test_owner_messages_never_trigger_drafts` | Owner and owner-automated messages in an Assist chat → no draft job (RG-01) | S04.2 |
| | `test_observe_chats_get_no_proactive_drafts` | Observe mode → no job | S04.2 |
| `test_m04_restore.py` (NFR-09) | `test_a_restored_database_with_an_approved_action_sends_nothing` | Seed `approved` and `executing` actions → `post-restore` → reconcilers and `actions.execute` → zero sends; notice lists the cancelled and uncertain action ids (ids and times only) | F0, S04.4 |
| `test_m04_drafts_only.py` | `test_with_sending_off_approve_ends_blocked_with_copy_ready_text` | Flag `actions.send` off → the proposal is a normal `awaiting_approval` card (P2-T21); Approve answers "Approved — sending is off" → Tx A `blocked`/`sending_disabled`, copy-ready text, no business send | S04.3, S04.4 |

## 5. QA-S5 — M05 acceptance scenarios

Implementation evidence, owning units and expected-failure limitations:
[m05-scenarios.md](m05-scenarios.md). Marker presence is pending acceptance.

Branch `qa/m05-scenarios` from `int/m05` → PR to `int/m05`. Triage responses are `fake.parsed` dicts with the `TriageResult` fields of the S05.1 spec; monitor verdicts per the S05.4 spec; notifications are asserted on `notifications` rows (status, reason code) and on `env.sender.sends` to the control chat.

| File | Test | Asserts | Green by |
|---|---|---|---|
| `test_m05_j6_monitors.py` (PRD R2 #4) | `test_notify_me_when_contact_replies_fires_exactly_once` | Owner run calls `create_monitor` (contact_replied) → confirmation card → Confirm → contact sends two messages → exactly one `monitor_match` alert; monitor `completed` | S05.4, S05.2 |
| | `test_a_keyword_monitor_fires_on_a_match_only` | Standing keyword monitor; matching and non-matching messages | S05.4 |
| | `test_three_semantic_monitors_in_scope_make_one_evaluate_call_per_burst` | One `evaluate_monitor` parse call per burst | S05.4 |
| | `test_monitors_ignore_ignore_mode_chats` | No match, no call | S05.4 |
| | `test_revoked_consent_stops_a_queued_semantic_monitor_evaluation` | Defer semantic evaluation, revoke `ai_managed`, then run the job → no FakeLLM request or monitor-match alert | S05.4 |
| | `test_a_tainted_run_cannot_create_a_monitor_without_a_tap` | `create_monitor` after an injected tool result → card only (RG-11) | S05.4 |
| `test_m05_notification_policy.py` | `test_quiet_hours_move_non_urgent_alerts_to_the_digest` | `batched`, delivered in the next digest | S05.2, S05.3b |
| | `test_urgent_bypasses_quiet_hours_only_when_enabled` | Off → deferred to quiet-hours end; on → immediate | S05.2 |
| | `test_the_hourly_cap_batches_extra_standard_alerts` | Cap 2 → third `batched`/`cap` | S05.2 |
| | `test_a_system_alert_is_delivered_during_quiet_hours_with_the_cap_reached` | `delivery_uncertain` alert immediate (IMPLEMENTATION_PLAN S05.2) | S05.2 |
| | `test_a_duplicate_alert_is_sent_once` | Same dedupe key → one row, one send | S05.2 |
| | `test_an_uncertain_alert_is_never_resent_after_reconciliation` | Claim commits, fake sender returns an ambiguous outcome; re-run delivery and reconciliation → `uncertain`, one sender call | S05.2 |
| | `test_not_useful_feedback_is_recorded` | `ntf:<token>:nu` → `feedback='not_useful'` | S05.2 |
| `test_m05_digest.py` | `test_a_digest_built_twice_for_one_period_is_delivered_once` | Two `digest.build` runs → one `digests` row `sent`, one send | S05.3b |
| | `test_the_digest_fires_at_local_time_across_dst` | Owner in Europe/Berlin, last Sunday of October → fires at the configured local time both days | S05.3a, S05.3b |
| | `test_a_missed_digest_after_downtime_fires_late_with_a_note` | Occurrence `late` note | S05.3b |
| | `test_digest_on_demand` | `/digest` → one digest | S05.3b |
| `test_m05_triage.py` | `test_with_the_triage_trigger_a_burst_still_produces_one_card` | Flags `triage.run` + `triage.assist_trigger` → one triage run, one draft card (S05.1 regression) | S05.1 |
| | `test_an_urgent_decision_request_alerts_the_owner` | `urgency=urgent`/`requests_decision` → immediate alert | S05.1, S05.2 |
| | `test_a_repeated_promise_creates_one_task` | Same commitment in two triage windows → one task | S05.1 |
| | `test_the_owners_manual_answer_closes_the_open_question` | Owner reply → triage `resolved_task_ids` → task `done`/`resolved_by_triage`, audited, notified | S05.1 |
| | `test_triage_runs_have_no_tools_and_see_one_conversation` | RG-17, RG-09-style isolation for triage | S05.1 |
| | `test_revoked_consent_stops_a_queued_triage_run` | Defer triage, revoke `ai_managed`, then run the job → no FakeLLM request, derived task or triage notification | S05.1 |
| `test_m05_r1_item7.py` (PRD R1 #7 re-verified) | `test_deleting_a_conversation_removes_its_tasks_triage_and_notifications` | After `delete_conversation_data`: no task, verdict, notification or monitor naming it; other chats intact | F0, S05.1 |

## 6. Release verification (after every deploy, CP3 and CP5)

Content-free only. Run by the TL (server) with Antony recording the results in the evidence template.

1. `just prod-status <ip>` — containers up, app healthcheck OK, backup markers fresh.
2. `just prod-logs <ip> 300` — scan for `ERROR`/`CRITICAL` lines and tracebacks by type; never paste lines that could carry content (logs are content-free by design; report counts and event names).
3. `just prod <ip> p2-verify release` (built in OPS-TV, §6.1) — Alembic head (`0014` after CP5, `0013`/`0014` after CP3 depending on F0), enabled flag **names** (never values of other settings), P2 tables present, row counts per action state.
4. Smoke: owner sends `/status` and `/activity` in the control chat; the TL confirms the replies arrived (owner says so) — no business-chat action.
5. After each flag change (WORKFLOW §10): step 3 again, then the next step of the enable order.

### 6.1 `deploy/server.sh` verification actions needed (built in OPS-TV by Antony; TL reviews)

| Action | Prints (numbers, ids, states, hashes, times only) | Used by |
|---|---|---|
| `trial-check [--since <ISO>]` | The send audit of OPS-trial-verifier: for each `succeeded`/`delivery_uncertain` action — approval audit present before the claim, `approved_text_hash == sent_text_hash`, one `assistant_sent` row per success, one attempt per action, no send after `outdated`/`expired`/`rejected`/`cancelled`, conversation of the send == conversation of the action; summary line PASS/FAIL | §7 trial, live windows |
| `p2-verify release` | Alembic head, enabled flag names, P2 tables, action state counts | §6 |
| `p2-verify actions --since <ISO>` | One line per action: id, conversation id, origin, state, state_reason, version, card_state, hash match (yes/no/–), telegram id present, attempts | Live windows |
| `p2-verify action <id>` | The action's audit timeline: event names, times (owner's timezone), reason codes | Live windows |
| `p2-verify script <prefix> --expect <range>` | Like `verify_r0.py`: the synthetic numbers (`P2-4xx`, `P2-5xx`) found by direction, extracted inside SQL; never text | Live windows |
| `p2-verify notifications --since <ISO>` | Counts by kind/status/reason_code; per notification: id, kind, class, priority, status, created → sent seconds | M05 window |
| `p2-verify tasks --since <ISO>` | Counts of tasks by kind/status/closed_reason; per task: id, conversation id, kind, status, created/closed times | M05 window |
| `p2-verify digests --since <ISO>` | Per digest: id, period, status, delivered time in the owner's timezone | M05 digest days |
| `nfr05 [DAYS]` (S05.2) | p50/p95 triage completion and urgent-alert latency (seconds) | M05 window |

All actions run like `verify-r0`: a one-off app container with `tools/acceptance/` mounted read-only, a read-only transaction, no content column ever selected for output (number extraction inside SQL is allowed, as in `verify_r0.py`).

## 7. Trial support (M04, 7 days after CP3)

- **Daily (TL or Antony, same time each day):** `just prod <ip> trial-check --since <trial start>`; record PASS/FAIL and counts in the trial log (M04.md section prepared by the TL).
- **Daily (owner):** glance at `/activity` for anything unexpected.
- **An incorrect send** (any one fails the trial): wrong recipient (conversation of the send ≠ the action's); text ≠ approved text (hash mismatch); a duplicate (two sends for one action, or two successful attempts); a send after the action was outdated, expired, rejected or cancelled; a send without approval (no `action_approved` before `action_claimed`). `delivery_uncertain` is not an incorrect send by itself; the owner checks the chat and the result is logged.
- **If one occurs:** stop-the-line (ORG §5): post on the integration issue with `owner-gate`, the TL proposes switching `actions.send` off at once; record the action id, the check that failed and the timeline from `p2-verify action <id>`; the trial restarts after the fix.

## 8. Eval operations (WORKFLOW §11)

- Each live run: the owner's go-ahead quoted in the evidence; `--estimate-only` first; `--limit 1` trial; full run; spend recorded against the USD 2 phase cap.
- Runs: E2 baseline (informational, after S04.2 + E2T), E3 on Luna (after S05.1 + E3T), model comparison only if Luna misses a threshold, retrieval agent-mode re-run after each `owner_request` bump (S04.3, S05.4).
- **Pre-review** after every full run by a fresh read-only agent, in the format of `docs/evals/2026-10-26-e1-prereview-v4.md`: per flagged item — id, judge verdict, what the source supports, real/false positive, proposed label change if any; a summary of real problems; nothing counts as a gate result until the owner confirms (IMPLEMENTATION_PLAN §10.1).
- **E3 metric review:** use only owner-confirmed bursts for the gate; report the evaluated counts and denominators. Urgent/decision recall ≥ 90 %; false-alert rate = alerted negatives / all alerted bursts ≤ 20 %; false-closure rate ≤ 10 %; resolution recall ≥ 80 %. A zero denominator makes that metric not evaluable; a zero numerator with a nonzero denominator is a real 0 %. Never manufacture a 100 % or zero-error PASS for an empty sample. Keep pre-screen and confirmed results distinct (E3 spec §B).
- Language metrics are informational in P2 (P2-O11).

## 9. Evidence and reporting

| Evidence | Where |
|---|---|
| QA review findings | PR review comments (blocking / non-blocking) |
| Open non-blocking findings | The milestone's open-items list (via the TL) |
| Scenario status | [regression-matrix.md](regression-matrix.md) status columns, updated in each QA PR |
| Live windows, restore test, LV-08, trial days, digest days, friends' session | [evidence-template.md](evidence-template.md) copies, pasted into `docs/milestones/M04.md` / `M05.md` sections by the TL the same day |
| Eval runs | `docs/evals/<date>-<set>-<model>.md/json` + pre-review file |
| Release verification | Integration issue comment per deploy |
