# P2 regression and traceability matrix

*Owner: Antony (QA). IMPLEMENTATION_PLAN §10 is the single list of mandatory regression tests; this matrix tracks which P2 unit adds or extends each one and which tests prove it. Names below are planned contracts from the unit specs, not claims that the files already exist. Status: **planned** → **added** (in a merged PR) → **green** (passing on `int/*` and `main`). Antony updates the status column in each QA PR; the TL copies new names into IMPLEMENTATION_PLAN §10 when a milestone merges. A green status needs a commit and CI-run link; xfailed or skipped tests remain planned/added.*

## A. Regression tests RG-01…RG-21

QA-S5 scenario evidence and marker-removal ownership are tracked in
[m05-scenarios.md](m05-scenarios.md). Expected failures remain pending;
scenario implementation alone does not make an RG row green.

| RG | Guards (AGENTS §6) | First slice (§10) | P2 unit | Planned P2 tests (file::name) | Status |
|---|---|---|---|---|---|
| RG-01 | Owner messages never trigger drafts | S04.2 | S04.2; extended S05.1 (triage trigger) | `tests/integration/test_drafting.py::test_rg01_owner_messages_never_trigger_drafts`; `tests/scenarios/test_m04_assist_bursts.py::test_owner_messages_never_trigger_drafts`; `tests/integration/test_assist_trigger_switch.py::test_owner_messages_never_trigger_drafts_in_either_mode` | planned |
| RG-02 | Mirrored bot chats ignored | S02.2 | S04.2 (no drafts in bot chats) | Existing `test_business_ingestion.py` RG-02 tests stay green; `tests/integration/test_assist_drafting.py::test_rg02_bot_chats_and_ignore_mode_never_get_a_draft_job` | existing + planned |
| RG-03 | Business updates never reach control handlers (5) | S01.2 | F0 (control routes registry) | Existing RG-03 tests stay green; F0 adds `tests/integration/test_control_routes.py::test_business_updates_never_reach_registered_routes` (F0 spec §4) | existing; F0 extends |
| RG-04 | Double-tap approval sends once (6) | S04.3 | S04.3 | `tests/integration/test_approvals.py::test_rg04_double_tap_approve_sends_once` (name per spec); `tests/scenarios/test_m04_r1_at_most_once.py::test_a_double_tap_on_approve_sends_once` | planned |
| RG-05 | Restart in `executing` ⇒ `delivery_uncertain`, never resent (6) | S04.4 | S04.4; F0 (post-restore step) | `tests/integration/test_execution.py::test_rg05_restart_in_executing_is_uncertain_never_resent` (name per spec); `test_post_restore.py::test_post_restore_cancels_pending_actions_and_marks_executing_uncertain` (F0); scenario `test_a_restart_between_claim_and_send_is_uncertain_and_never_resent` | planned |
| RG-06 | Stale drafts never send (1) | S04.4 | S04.4 (with S04.2 outdating) | `test_execution.py::test_rg06_a_stale_draft_never_sends` (name per spec); scenarios in `test_m04_outdating_and_expiry.py` | planned |
| RG-07 | Non-owner connections ignored (7, 12) | S02.1 | — | Existing `test_connections.py::test_rg07_*` stay green | existing |
| RG-08 | Managed content never reaches the LLM without consent (9) | S02.2 | S04.2 (draft run), S05.1 (triage), S05.4 (evaluate_monitor) | `test_drafting.py::test_rg08_without_managed_consent_naming_the_processor_no_draft_call_is_made`, `::test_rg08_a_consent_revoked_after_the_defer_stops_the_draft_job`; `test_triage.py::test_without_managed_consent_no_triage_call_is_made`; `test_monitors.py::test_without_managed_consent_no_evaluation_call_is_made` (all under `tests/integration/`); QA-S5 `test_revoked_consent_stops_a_queued_triage_run`, `test_revoked_consent_stops_a_queued_semantic_monitor_evaluation` | planned |
| RG-09 | No cross-conversation content in draft prompts (4) | S04.2 | S04.2; same check for triage (S05.1) and monitor evaluation (S05.4) | `tests/integration/test_drafting.py::test_rg09_draft_prompt_never_contains_another_conversation`, `::test_rg09_owner_scope_facts_and_other_contacts_notes_never_reach_a_draft`; `tests/integration/test_triage.py::test_triage_sees_only_its_own_conversation`; `tests/integration/test_monitors.py::test_evaluate_monitor_sees_only_its_own_conversation` | planned |
| RG-10 | Logs content-free | S00.1 | S04.4 (send failures), S05.2 (alert delivery failures) | `test_log_hygiene.py` gains canary cases for a failed business send (Telegram 400 with the draft as canary) and a failed alert delivery (names per spec) | planned |
| RG-11 | A tainted owner run changes no state without a tap (3b) | S03.1 | S04.2 (`set_conversation_mode` assist), S04.4 (`resume_autonomy` card-only), S05.2 (`set_contact_priority`), S05.4 (`create_monitor`, `stop_monitor`) | One test per new configure/control tool: after an injected tool result the call yields a card and no state change (names per spec); scenario `test_m05_j6_monitors.py::test_a_tainted_run_cannot_create_a_monitor_without_a_tap` | planned |
| RG-12 | Edit/delete of an owner or contact message refused by policy and gateway (14) | S04.4 | S04.1 (rule), S04.4 (gateway) | `tests/unit/test_policy_rules.py::test_rg12_target_is_assistant_sent_denies_owner_and_contact_targets` and `tests/integration/test_gateway_business.py::test_rg12_gateway_refuses_editing_or_deleting_a_non_assistant_message` (names per spec) | planned |
| RG-13 | Never claims to be human (13) | S06.2 | — (M06) | — | not in P2 |
| RG-14 | Disclosure present when required (13) | S06.2 | — (M06) | — | not in P2 |
| RG-15 | Sensitive categories never inferred | S07.4 | — (M07) | — | not in P2 |
| RG-16 | No emotion/personality/trust fields (14) | S07.4 | — (M07) | — | not in P2 |
| RG-17 | Contact-triggered runs get no tools; no tool sends (2, 3a) | S01.4 | S04.2 (`draft_reply`), S05.1 (`triage`), S05.4 (`evaluate_monitor`) | `tests/integration/test_drafting.py::test_rg17_draft_reply_runs_are_given_no_tools`; `tests/integration/test_triage.py::test_triage_runs_are_given_no_tools`; `tests/integration/test_monitors.py::test_evaluate_monitor_runs_have_no_tools`; existing registry-refusal tests stay green | planned |
| RG-18 | A business send reaches Telegram only after the Tx A re-check; a failed precondition means no Telegram call (1, 7, 8) | S04.4 | S04.1 (rules), S04.4 (protocol) | `test_execution.py::test_rg18_no_telegram_call_when_a_precondition_fails` parametrised over window closed, rights missing, `is_enabled=false`, unknown conversation, missing consent, Ignore mode, sending disabled (name per spec) | planned |
| RG-19 | `/pause` and an owner manual message stop autonomous activity; a scheduled autonomous send re-checks (10) | S04.4 | S04.4 (extended in S06.3) | `tests/integration/test_kill_switch.py::test_rg19_pause_blocks_an_autonomous_action_at_claim`; `tests/integration/test_execution.py::test_rg19_an_owner_manual_message_blocks_the_send`; scenario `test_m04_j9_kill_switch.py::test_pause_blocks_an_autonomous_action_at_claim`; owner-approved sends remain allowed while paused | planned |
| RG-20 | Nothing reports pending/blocked/failed/uncertain work as done (11) | S04.2 | S04.2 (tool results), S04.3 (cards), S04.4 (outcomes), S04.5 (activity) | `tests/integration/test_reply_tools.py::test_rg20_propose_results_never_say_sent`; `tests/unit/test_approval_cards.py::test_rg20_card_labels_never_show_pending_blocked_or_uncertain_work_as_sent`; `tests/integration/test_execution.py::test_rg20_uncertain_and_failed_outcomes_are_never_reported_as_sent`; scenario `test_m04_j10_activity.py::test_drafts_and_sends_are_never_shown_alike` | planned |
| RG-21 | Repositories never return another account's rows (12) | S01.1 | F0 (bulk helpers, automations repository), S04.1 (proposals), S04.5 (activity), S05.1 (tasks, verdicts), S05.2 (notifications) | `test_action_lifecycle_bulk.py::test_clear_texts_never_crosses_accounts` (F0); a foreign-account test per new repository/tool (names per spec) | planned |

## B. Traceability: requirements → units → tests → manual checks

QA-S4's scenario implementations and gate limitations are tracked in
[m04-scenarios.md](m04-scenarios.md). A scenario marked strict xfail is still
pending acceptance; its existence does not make the corresponding RG row pass.

| Requirement | Unit(s) | Automated tests | Manual / live check |
|---|---|---|---|
| FR-DRF-01 Draft on owner request | S04.2, S04.3 | `test_m04_j4_draft_approve.py::test_j4_draft_edit_approve_sends_exactly_the_approved_text` | M04 checklist 1.1 |
| FR-DRF-02 Assist proactive drafts, one card per burst | S04.2 (M05: S05.1 trigger) | `test_m04_assist_bursts.py::*`; `test_m05_triage.py::test_with_the_triage_trigger_a_burst_still_produces_one_card` | M04 checklist 2.9 |
| FR-DRF-03 Approval card contents and in-place state | S04.3 | S04.3 card tests (name per spec) | M04 checklist 1.1 |
| FR-DRF-04 Edit by text or instruction | S04.3 | `test_j4_a_revision_instruction_creates_a_new_version_shown_for_approval`; replacement path in the J4 test | M04 checklist 1.2, 1.3 |
| FR-DRF-05 Outdated / Expired | S04.2, S04.3, S04.4 | `test_m04_outdating_and_expiry.py::*` | M04 checklist 1.5, 1.6, 1.6b–c (source deletion/edit), 2.3, 2.4, 2.6 |
| FR-DRF-06 Impossible send → reason + copy-ready | S04.1, S04.3, S04.4 | `test_m04_drafts_only.py::test_with_sending_off_approve_ends_blocked_with_copy_ready_text`; `test_the_window_closing_expires_the_draft_with_copy_ready_text` | M04 checklist 1.4, 2.5, 2.6 |
| FR-DRF-07 Re-verify at approval; real outcome | S04.4 | RG-18 tests; `test_m04_r1_at_most_once.py::*` | M04 checklist 2.1, 2.7 |
| FR-DRF-08 Owner-dictated always confirmed | S04.2, S04.3 | `test_j4_owner_dictated_text_is_shown_exactly_with_the_resolved_recipient` | M04 checklist 1.7 |
| FR-DRF-09 At most once under double taps, retries, restarts | S04.3, S04.4 | RG-04, RG-05; `test_m04_r1_at_most_once.py::*` | M04 checklist 2.2, 2.7 |
| FR-ACT-01 Everything recorded, viewable | S04.5 (+ audit events from S04.1–S04.4) | `test_m04_j10_activity.py::test_activity_lists_every_send_with_its_authorization` | M04 checklist 2.11 |
| FR-ACT-02 Drafts vs sent distinct | S04.3, S04.5 | `test_drafts_and_sends_are_never_shown_alike` | M04 checklist 1.1, 2.11 |
| FR-ACT-04 Nothing hidden | S04.5 | Activity completeness test | Trial: daily `/activity` glance |
| FR-AUT-06 Kill switch (R1 part) | S04.4 | RG-19; `test_m04_j9_kill_switch.py::*` | M04 checklist 1.8, 2.10 |
| FR-TRI-01 Triage bursts | S05.1 | S05.1 tests; `test_m05_triage.py::*` | M05 checklist 1 |
| FR-TRI-02 Attention list with reasons | S05.1 | Attention v2 tests (name per spec) | M05 checklist 4.1 |
| FR-TRI-03 Notification policy | S05.2 | `test_m05_notification_policy.py::*` | M05 checklist 2 |
| FR-TRI-04 Contact priority | S05.2, S05.2P | Priority tool/panel tests (name per spec) | M05 checklist 2.5 |
| FR-TRI-05 Commitments/open loops into tasks, dedupe, auto-close | S05.1 | `test_a_repeated_promise_creates_one_task`, `test_the_owners_manual_answer_closes_the_open_question` | M05 checklist 1.6–1.8 |
| FR-TRI-06 Monitors from natural language, confirmed | S05.4 | `test_m05_j6_monitors.py::*` | M05 checklist 3 |
| FR-TRI-07 Noise control and feedback | S05.2 | Dedupe, cap and feedback tests | M05 checklist 2.4, 2.6 |
| NFR-05 Triage < 90 s p95; urgent alert < 2 min p95 | S05.1, S05.2 | — (measured live) | M05 checklist 1.10 (`nfr05`) |
| NFR-06 Send safety (zero duplicates, zero policy bypass, 100 % recorded) | S04.1, S04.4, OPS-TV | RG-04/05/06/18; `test_m04_r1_assistant_sent.py::*`; `tests/integration/test_p2_verify.py::test_trial_check_*` checks the independent structural audit (OPS-TV part2 CI/review tracked on #65) | Trial: daily `trial-check`; synthetic audit tests do not replace trial evidence |
| NFR-09 Restore never sends | F0, S04.4 | `test_m04_restore.py::test_a_restored_database_with_an_approved_action_sends_nothing` | OPS-restore-test |
| PRD R1 #3 Draft → edit → approve → sent; sent = approved | S04.2–S04.4 | J4 scenario (hash equality) | M04 checklist 2.1 (edit → approve → sent on one action); 1.2–1.3 cover edits alone |
| PRD R1 #4 Every outdating/expiry condition prevents a stale send | S04.2–S04.4 | `test_m04_outdating_and_expiry.py::*`, `test_m04_j8_takeover.py::*` | M04 checklist 2.3–2.6 |
| PRD R1 #5 Double tap, restart, timeouts never duplicate | S04.3, S04.4 | `test_m04_r1_at_most_once.py::*` | M04 checklist 2.2, 2.7 |
| PRD R1 #6 `/activity` lists all sends with authorization | S04.5 | `test_m04_j10_activity.py::*` | M04 checklist 2.11 |
| PRD R1 #7 Deleting a conversation removes derived data (incl. tasks) | F0, S05.1 | F0 deletion canary tests; `test_m05_r1_item7.py::*` | — |
| PRD R1 #9 Each send recorded once as `assistant_sent` | S04.4 | `test_m04_r1_assistant_sent.py::*` | M04 checklist 2.1 (`p2-verify actions`) |
| PRD R2 #4 "Notify me when Daniel replies" fires once | S05.4 | `test_notify_me_when_contact_replies_fires_exactly_once` | M05 checklist 3.1–3.2 |
| PRD R2 #5 Triage eval recall ≥ 90 %, false alerts ≤ 20 % | S05.1, E3D, E3T | E3 eval run (gate after owner label confirmation) | — |

## C. Additional safety evidence at checkpoints

These extend the RG evidence; they do not replace or renumber mandatory regressions. All are **planned** at QA-P kickoff (2026-10-27). Integration filenames below are under `tests/integration/`.

| Checkpoint | Risk | Required planned evidence |
|---|---|---|
| CP1 | Old startup barrier permits a new process to send | F0: `test_startup_resets_the_ingestion_barrier_before_any_role_runs` |
| CP2 | Stale or foreign process barrier | `test_ingestion_barrier.py::test_a_worker_running_before_the_poller_reset_never_sends`, `::test_a_process_without_the_poller_role_never_sends`, `::test_a_poller_never_marks_another_runs_barrier_caught_up` |
| CP2 | Backlogged pause/takeover; update arrives after approval | `test_ingestion_barrier.py::test_restart_with_a_backlogged_takeover_and_pause_sends_nothing`; `test_action_execution_races.py` explicit-barrier interleavings from S04.4 §5 |
| CP2 | Wrong attempt settles a send, or a late failure causes a retry | `test_execution.py::test_tx_b_never_touches_a_row_of_another_attempt`, `::test_a_late_429_or_window_closed_after_mark_uncertain_fails_and_is_never_retried` |
| CP3 | A late revision overwrites an outdated card; consent revoked after approval | QA-S4 scenarios `test_a_revision_finishing_after_outdating_cannot_reactivate_the_card`, `test_revoking_managed_consent_cancels_a_queued_ai_draft` (test-plan §4) |
| CP3 | Verifier misses wrong recipient, changed text or an extra delivery | OPS-TV: `test_p2_verify.py` seeded violations and content canaries; clean 429 → success history passes, duplicate delivered/possibly delivered attempts fail; uncertain alone is not an incorrect send |
| CP4 | System notice resent after its notification row is swept | `test_system_alerts_on_notifications.py::test_a_once_ever_alert_is_not_resent_after_its_notification_row_is_swept`; same proof for an uncertain outcome (P2-T24/R10) |
| CP5 | Claimed notification or digest is resent after ambiguity | `test_notifications.py::test_an_uncertain_delivery_is_never_resent`; `test_digest.py::test_an_uncertain_digest_is_never_resent`; QA-S5 `test_an_uncertain_alert_is_never_resent_after_reconciliation` |
| CP5 | Owner-requested monitor demoted by contact priority or quiet hours | `test_monitors.py::test_a_bypass_monitor_alerts_during_quiet_hours`, `::test_a_low_priority_contacts_monitor_alert_is_not_demoted` |
| CP5 | E3 pre-screen incorrectly treated as an accepted gate | `test_eval_triage.py::test_gate_needs_owner_confirmed_bursts`; owner-confirmed counts and all four thresholds from E3 §B |

## D. QA-S4 marker ownership (R13)

Every marked scenario is listed below. For a shared scenario, the last listed
feature dependency to land removes its marker after the entire journey passes.
S04.4 also settles outcomes used by S04.3; a card assertion may require both.
CP3 requires zero expected failures. Double-tap and resume rows include both
parameter cases. The takeover journeys include ingestion listeners; isolated
Tx A guarantees remain covered by the S04.4 integration regressions.

| Scenario | Required units |
|---|---|
| `test_m04_assist_bursts.py::test_a_burst_in_an_assist_chat_produces_one_card` | S04.2b/S04.3 |
| `test_m04_assist_bursts.py::test_no_card_when_the_draft_says_no_reply_is_needed` | S04.2b |
| `test_m04_assist_bursts.py::test_owner_messages_never_trigger_drafts` | S04.2b |
| `test_m04_drafts_only.py::test_with_sending_off_approve_ends_blocked_with_copy_ready_text` | S04.2b/S04.3/S04.4 |
| `test_m04_j4_draft_approve.py::test_j4_draft_edit_approve_sends_exactly_the_approved_text` | S04.2b/S04.3/S04.4 |
| `test_m04_j4_draft_approve.py::test_j4_a_revision_instruction_creates_a_new_version_shown_for_approval` | S04.2b/S04.3 |
| `test_m04_j4_draft_approve.py::test_approve_during_revision_sends_nothing` | S04.2b/S04.3 |
| `test_m04_j4_draft_approve.py::test_a_revision_finishing_after_outdating_cannot_reactivate_the_card` | S04.2b/S04.3 |
| `test_m04_j4_draft_approve.py::test_j4_reject_sends_nothing_and_says_rejected` | S04.2b/S04.3 |
| `test_m04_j4_draft_approve.py::test_j4_owner_dictated_text_is_shown_exactly_with_the_resolved_recipient` | S04.2b/S04.3 |
| `test_m04_j8_takeover.py::test_an_owner_message_dated_after_the_base_but_ingested_late_blocks_the_send` | S04.4 |
| `test_m04_j8_takeover.py::test_takeover_outdates_the_card_and_audits_without_messaging_the_contact` | S04.2b |
| `test_m04_j9_kill_switch.py::test_pause_blocks_an_autonomous_action_at_claim` | S04.4 |
| `test_m04_j9_kill_switch.py::test_a_model_resume_call_never_resumes` | S04.4 |
| `test_m04_j9_kill_switch.py::test_pause_does_not_block_an_owner_approved_send` | S04.4 |
| `test_m04_outdating_and_expiry.py::test_a_new_contact_message_outdates_a_pending_draft` | S04.2b/S04.3 |
| `test_m04_outdating_and_expiry.py::test_an_owner_manual_reply_outdates_a_pending_draft` | S04.2b |
| `test_m04_outdating_and_expiry.py::test_deleting_the_source_message_outdates_the_draft` | S04.2b |
| `test_m04_outdating_and_expiry.py::test_editing_the_source_message_outdates_the_draft` | S04.2b/S04.3 |
| `test_m04_outdating_and_expiry.py::test_switching_to_ignore_cancels_a_pending_draft` | S04.2b |
| `test_m04_outdating_and_expiry.py::test_revoking_managed_consent_cancels_a_queued_ai_draft` | S04.4 |
| `test_m04_outdating_and_expiry.py::test_removing_the_reply_right_blocks_an_approved_draft` | S04.3/S04.4 |
| `test_m04_outdating_and_expiry.py::test_the_window_closing_expires_the_draft_with_copy_ready_text` | S04.3 |
| `test_m04_outdating_and_expiry.py::test_a_contact_message_ingested_after_approval_blocks_the_send` | S04.4 |
| `test_m04_r1_assistant_sent.py::test_each_send_is_recorded_once_as_assistant_sent` | S04.4 |
| `test_m04_r1_assistant_sent.py::test_an_echo_processed_first_merges_into_the_sent_record` | S04.4 |
| `test_m04_r1_assistant_sent.py::test_two_zero_id_sends_in_one_chat_both_succeed` | S04.4 |
| `test_m04_r1_at_most_once.py::test_a_double_tap_on_approve_sends_once` | S04.3/S04.4 |
| `test_m04_r1_at_most_once.py::test_a_restart_between_claim_and_send_is_uncertain_and_never_resent` | S04.4 |
| `test_m04_r1_at_most_once.py::test_a_network_timeout_on_send_is_uncertain_not_retried` | S04.4 |
| `test_m04_r1_at_most_once.py::test_a_rate_limited_send_is_retried_after_retry_after_and_sent_once` | S04.4 |
| `test_m04_r1_at_most_once.py::test_a_late_result_after_mark_uncertain_settles_to_succeeded` | S04.4 |
| `test_m04_restore.py::test_a_restored_database_with_an_approved_action_sends_nothing` | S04.4 |
