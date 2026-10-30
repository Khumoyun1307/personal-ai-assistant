# P3 test plan (QA)

*Owner: `SQ`. Review: `TL` and both senior team leads. This is a plan, not passing evidence. The source of mandatory regressions is [IMPLEMENTATION_PLAN §10](../../../IMPLEMENTATION_PLAN.md#10-testing-strategy-applies-to-all-milestones); [AGENTS §2](../../../../AGENTS.md#2-documents-and-source-of-truth) sets precedence.*

## 1. Scope and layers

P3 covers M06 delegated replies and M07 reminders, follow-ups, automations, memory review and export. It ends with the Personal v1 trial. Automation nudges always require owner approval, including in a delegated chat. Semantic follow-up matching, groups and later milestones remain outside P3 ([phase README §1](../README.md#1-what-p3-delivers), OD-18/33).

| Layer | What to prove | Where / owner | Gate |
|---|---|---|---|
| Unit | Pure policy, grounding, language keys, disclosure, state transitions, recurrence/DST, sensitive filtering, honest renderers | `tests/unit/`; owning feature unit | CI |
| Integration | Migration 0015 up/down; constraints; consent; claim/release races; jobs/reconcilers; privacy, retention and restore; card callbacks | `tests/integration/`, PostgreSQL 18; feature authors, team QA | CI |
| Contract | Gateway targets, document sends, error mapping, registry exact sets and port shapes | `tests/contract/`, unit/integration confinement checks; F0 and feature owners | CI |
| Scenario | Production wiring through inbox, tools/cards, ingestion listeners and jobs; fake transport, model and clock | `tests/scenarios/{m06,m07,p3_cross}/`; QJ1, QE1, SQ | CP-A / R1, then `main` before each live twin |
| Evaluation | S06.5 per-language quality and safety; retrieval after `owner_request.v10` | `evals/`; TL or SQ starts, owner approves spend and rates | CP-G; retrieval before deploy #1 |
| Manual live | Shadow, J5, J7 c → b → a, phone reminders, export, review cards, restore, soak/trial | Checklists here; TL server, owner client, SQ evidence | CP-L, CP-T, CP-X |

Sources: [IP §10](../../../IMPLEMENTATION_PLAN.md#10-testing-strategy-applies-to-all-milestones), [WORKFLOW §5](../WORKFLOW.md#5-testing), [QA-S6 §1](../specs/QA-S6.md#1-goal), [QA-S7 §1](../specs/QA-S7.md#1-goal), [QA-X §1](../specs/QA-X.md#1-goal), [GATE-RUN §2](../specs/GATE-RUN.md#2-scope), [LIVE-P3 §3.2](../specs/LIVE-P3.md#32-rules-for-every-live-check).

## 2. Infrastructure and review

Use `FakeClock`, `FakeLLM`, `FakeTelegramSender`, `FakeUpdateSource`, `FakeChannel`, `FakeAutonomyPlanner`, `FakeDelegatedDrafter`, `business_env`, `agent_env`, P3 factories and `p3_drive`. The driver additions land first in QA-X; SQ owns the driver after CP1. Seeds establish only state outside the behaviour under test. Negative checks need a positive control. Scenario helper tests must pass independently of feature availability ([QA-X §2.2, §3.1](../specs/QA-X.md#22-driver-additions-testsscenariosp3_drivepy-testsscenariostest_p3_drivepy-sq-owns-both-after-cp1-f0-e-4); QA-S6/7 §3.1–3.2).

| Role | Checks |
|---|---|
| Feature author | Red-first named tests; strict args, account scoping, consent, idempotency, log hygiene; docs and shared lists |
| Cross-team peer | Contract and implementation review; fresh reviewer from the other vendor |
| QJ1/QJ2 | Josh-unit QA; QJ1 owns QA-S6 and real-Postgres race reproductions |
| QE1 | QA-S7; M07 QA and the after-merge lane |
| QE2 | Gate integrity at three points; privacy/isolation; DELEG-DRAFT QA |
| QD1 and mentor | Junior allow-list, named tests, shared lists, data integrity |
| SQ | QA-X, invariant/RG traceability, marker audits, verifier independence, CP-A/R1 QA sign-off, live/eval evidence |
| TL | Dispatches peers, deep safety reviews, integration results and checkpoint decisions; server operations |
| Owner | Per-run go-aheads, ratings/verdicts, flags, client actions, export inspection, milestone acceptance |

Sources: [ORG §2–3](../ORG.md#2-ownership-raci-by-unit-group), [WORKFLOW §6](../WORKFLOW.md#6-ready-done-review-fix). Tier-1A reviews include complete mutation tables with no survivor; a possible §6 break is blocking and needs a failing reproduction. Relevant short tests may run locally; the full suite runs only in CI. This documentation launch runs no tests or live commands ([AGENTS §11](../../../../AGENTS.md#11-testing-standards); launch boundary).

## 3. Checkpoint entry and exit

Dates are targets from D0 = 2026-11-02, not permission to skip a guard. Record tested SHA, CI URL, results including skips/xfails and reviewer sign-off. A missing result is NOT EVALUABLE ([README §5](../README.md#5-checkpoints); [LIVE-P3 §3.2](../specs/LIVE-P3.md#32-rules-for-every-live-check)).

| Checkpoint | Entry | Exit / evidence | Accountable / source |
|---|---|---|---|
| CP0, pre-D0 | Owner approval OD-1; docs/synthetic-data scope | Package and F0-C corrections merged; owner texts proofread; issues/labels and capability checks present; QA-P3 reviewed/merged; data merged or held for D0; GW-ESC result and C4 gap recorded, C9 mapped | TL/owner/leads; README §5; decisions §B10 P3-T165 |
| D0 | CP5 on `main`; date reached | `int/p3` cut/pushed; foundation and contract-first work starts | TL; README §5 |
| CP1, D+1 | Foundation reviews/CI ready | F0-S → F0-R → F0-E merged in order; integration push CI green; contracts frozen; fully written QA-S6/S7/X packages merged by D+1.5 | TL; README §5; P3-T156 |
| CP-G0, D+3 | Real planner/drafter/rules/grounding/scheduler and eval units merged; v1.1 data ready; group B answers and per-run approvals | Estimate → limit-one → full; run/gate reports and pre-review; QE2 check #1; iteration list | SQ/TL; GATE-RUN §3.4–3.5 |
| CP-A, about D+4.5 | All nine Tier-1A units merged; marker audit | All RG, especially RG-13/14/18/19/20, green on merge result; complete mutation tables, no survivors; races pass; SQ evidence/sign-off; TL records §E sign-off | TL/SQ; README §5; WORKFLOW §7; QA-X §3.8 |
| R1 / CP-R1, D+5 earliest | CP-A signed; all RG/scenarios green on merge result; QA-after findings fixed or logged; OPS-TV3/OPS-AV merged; M04 last check and digest day 5 done | SQ QA sign-off; owner merges integration branch (or expressly delegates); ancestry checked; deploy #1 separately approved, new flags off, v6 notice, recorder; release PASS | Owner/TL/SQ; WORKFLOW §7; LIVE-P3 §3.4 |
| CP-G, D+6 | Frozen v1.2 candidate; owner confirmations, ratings and required verdicts complete | QE2 checks #2/#3 pass; per-language gate report; ≥1 language PASS; exact passed-only language recommendation; TL §E record | Owner/SQ/QE2/TL; GATE-RUN §3.7–3.11 |
| Enable 4, D+7 | CP-A, CP-G, clean shadow including Pause buttons, notice delivered ≥24 h, LV-16 record accepted | Owner enables send and passed languages only; exact cumulative release guards and trial-check PASS | Owner/TL; README §6; LV-16 runbook §5 |
| CP-L, D+8 | Green twins on `main`; approved windows, tools deployed | Shadow zero sends; J5 incl. three valid pause sources; J7 c/b/a; reminders/restart; owner export inspection; memory cards; non-vacuous restore; enable 5; evidence accepted | Owner/TL/SQ; LIVE-P3 §3.13 |
| CP-T / T0, D+11 | CP-L; ≥3-day standing-delegation soak; daily checks; recorder ≥5 days | Three consecutive clean checks, standing delegation active with ≥1 succeeded reply, informed contact yes; release guards PASS; full flag/language/setting set frozen; T0 record | Owner/TL/SQ; LIVE-P3 §3.10 |
| CP-X, after 14 days | Trial from latest T0, frozen set and cumulative checks | Zero wrong-recipient/duplicate/out-of-policy sends; nonzero succeeded autonomy; two weekly ratings ≥4; both alert-usefulness views ≥70%; availability ≥99%; R1 #7 every class; DOCS-EXIT and owner review | Owner/TL; LIVE-P3 §3.11–3.13; README §5 |

R1 never implies enable 4 or milestone `done`. Used prompts stay immutable; a changed candidate needs new eval/rating evidence. A trial FAIL follows the brake, fix and clock-restart procedure in [LIVE-P3 §3.12](../specs/LIVE-P3.md#312-stop-the-line-rule). The overlap variant needs a separate owner decision; this package uses the ≥3-day soak default.

## 4. Scenario packages and marker ownership

The inventories below copy each spec's ID, exact test name, last planned unit and other dependencies. They are planned contracts, not existing-test claims. Paths are under `tests/scenarios/`. IDs are package-qualified: QA-S7 J5 is an automation-panel scenario, not the live J5 journey. [QA-S6 §3–4](../specs/QA-S6.md#4-named-tests), [QA-S7 §3–4](../specs/QA-S7.md#4-named-tests), [QA-X §3–4](../specs/QA-X.md#4-named-tests) remain authoritative for setup/assertions.

Common M06 autonomous-send needs: F0-S, M6-POL, DELEG-DRAFT, GRND, M6-SCHED, M6-PLAN; F0-S grounding/language stubs cannot establish acceptance. QA-S6 B17's “green at merge” label does not remove its listed feature dependencies; audit it when those units land (QA-S6 §3.1, §3.4).

Every skeleton has a full body and an observed failure point. Collection/fixture errors are fixed, never disguised as feature xfails. Use `xfail(strict=True, reason="awaiting <UNIT>")`; parameter cases may name different units. On XPASS the package's QA owner removes only passing markers in a commit on the feature branch. No feature author weakens a scenario. SQ audits before CP-A and R1: compare every marker with merged units, run suspect individual cases with `--runxfail`, classify the cause, record counts and correct the reason if another dependency remains. A failing safety behaviour blocks R1 ([QA-X §3.8](../specs/QA-X.md#38-marker-audit-sq-before-cp-a-and-before-r1); decisions P3-T86/158).

### 4.1 QA-S6 (`m06/`, QJ1)

Source: QA-S6 §2, §3.3–3.8. B19/B20 are included from §3.4/§4 despite the stale B18 inventory ceiling in §5.

| ID | File / exact test name | Turns green | Other needs |
|---|---|---|---|
| A1 | `test_m06_s061_delegations.py::test_s061_spec_validation_refuses_bad_delegations` | M6-DELEG | F0-S |
| A2 | `test_m06_s061_delegations.py::test_s061_a_delegation_starts_only_after_the_owner_confirms_its_card` | M6-DELEG | M6-PLAN, M6-TXA |
| A3 | `test_m06_s061_delegations.py::test_s061_a_delegation_ends_at_its_owner_local_end_time` | M6-LIFE | M6-DELEG, M6-TXA |
| A4 | `test_m06_s061_delegations.py::test_s061_a_standing_delegation_requires_confirmation_and_its_flag` | M6-DELEG | — |
| A5 | `test_m06_s061_delegations.py::test_s061_set_conversation_mode_rejects_delegate` | — (green at merge) | — |
| A6 | `test_m06_s061_delegations.py::test_settings_end_ends_the_delegation_after_a_confirm_step` | SET-P3 | M6-DELEG, M6-CATCH, M6-SCHED |
| A7 | `test_m06_s061_delegations.py::test_settings_disclosure_wording_can_be_changed_but_never_emptied` | SET-P3 | M6-DELEG, M6-TXA |
| A8 | `test_m06_s061_delegations.py::test_j12_the_conversation_panel_shows_the_covering_delegation_with_end` | SET-P3 | M6-DELEG |
| A9 | `test_m06_s061_delegations.py::test_s061_the_confirmation_card_names_who_may_receive_the_knowledge_and_the_notice_states_the_limits` | M6-DELEG | — |
| A10 | `test_m06_s061_delegations.py::test_rg11_a_run_with_forwarded_text_cannot_create_a_delegation_without_a_tap_but_can_end_one` | M6-DELEG | M6-PLAN |
| A11 | `test_m06_s061_delegations.py::test_s061_a_newer_delegation_replaces_the_older_in_a_shared_chat_and_its_scheduled_reply_is_cancelled` | M6-DELEG | M6-PLAN, M6-TXA |
| A12 | `test_m06_s061_delegations.py::test_a_revoked_consent_or_a_disabled_connection_ends_every_delegation` | M6-LIFE | M6-SCHED, M6-PLAN, M6-CATCH |
| A13 | `test_m06_s061_delegations.py::test_a_late_delegation_end_still_ends_it_and_nothing_is_sent_after_ends_at` | M6-LIFE | M6-DELEG, M6-PLAN, M6-SCHED, M6-CATCH, RD-DEL |
| A14 | `test_m06_s061_delegations.py::test_rg11_text_from_a_forwarded_message_becomes_shareable_only_through_a_card_showing_the_exact_text` | M6-DELEG | — |
| B1 | `test_m06_s062_autonomy.py::test_s062_an_in_scope_known_question_is_auto_replied_with_the_disclosure_first_in_session_only` | M6-TXA | DELEG-DRAFT, M6-POL, GRND, M6-SCHED, M6-PLAN |
| B2 | `test_m06_s062_autonomy.py::test_s062_an_unknown_question_gets_a_clarifying_reply_if_allowed_else_an_escalation` | `allowed`: M6-TXA; `not_allowed`: M6-PLAN | M6-PLAN |
| B3 | `test_m06_s062_autonomy.py::test_s062_a_commitment_or_sensitive_topic_becomes_a_draft` | M6-PLAN | M6-POL |
| B4 | `test_m06_s062_autonomy.py::test_s062_a_reached_cap_queues_the_reply_to_the_owner` | M6-TXA | M6-PLAN, M6-ESC |
| B5 | `test_m06_s062_autonomy.py::test_s062_a_bot_chat_is_never_auto_replied` | M6-TXA | M6-PLAN |
| B6 | `test_m06_s062_autonomy.py::test_s062_the_language_gate_blocks` | M6-TXA | GRND, M6-PLAN |
| B7 | `test_m06_s062_autonomy.py::test_s062_a_backlog_contact_message_older_than_30_minutes_becomes_a_draft` | M6-PLAN | M6-POL |
| B8 | `test_m06_s062_autonomy.py::test_s062_a_contact_dictating_the_reply_is_blocked_by_content_grounded` | M6-PLAN | GRND |
| B9 | `test_m06_s062_autonomy.py::test_s062_a_declared_language_that_disagrees_with_the_tagger_requires_approval` | M6-PLAN | GRND |
| B10 | `test_m06_s062_autonomy.py::test_s062_an_unlisted_contact_raising_the_topic_gets_no_knowledge_based_auto_reply` | M6-PLAN | GRND |
| B11 | `test_m06_s062_autonomy.py::test_rg13_an_identity_question_gets_the_fixed_truthful_sentence_never_the_models_text` | `en`: M6-TXA; `ru`: M6-PLAN; `late`: M6-TXA | M6-PLAN, GRND, M6-SCHED |
| B12 | `test_m06_s062_autonomy.py::test_rg14_the_disclosure_is_re_added_after_24_hours_of_inactivity` | M6-TXA | M6-PLAN; `standing`: M6-DELEG |
| B13 | `test_m06_s062_autonomy.py::test_rg14_an_uncertain_earlier_send_counts_as_not_disclosed` | M6-TXA | M6-PLAN |
| B14 | `test_m06_s062_autonomy.py::test_s062_with_autonomy_send_off_every_allowed_reply_is_a_shadow_card` | M6-PLAN | M6-POL, GRND |
| B15 | `test_m06_s062_autonomy.py::test_rg14_a_gap_that_reaches_24_hours_while_the_reply_waits_is_re_proposed_once_with_the_disclosure` | M6-TXA | M6-SCHED, M6-PLAN |
| B16 | `test_m06_s062_autonomy.py::test_rg08_a_revoked_consent_stops_a_queued_delegated_draft_before_any_model_call` | M6-PLAN | DELEG-DRAFT |
| B17 | `test_m06_s062_autonomy.py::test_rg09_shareable_notes_reach_only_the_delegated_draft_prompt` | F0-S (R54; green at merge, else QJ1 files the defect on F0-S and marks it `awaiting F0-S`) | DELEG-DRAFT, M6-PLAN, M6-SCHED |
| B18 | `test_m06_s062_autonomy.py::test_with_autonomy_run_off_an_active_delegation_changes_nothing` | green at merge | — |
| B19 | `test_m06_s062_autonomy.py::test_od11_a_held_reply_is_an_alert_whose_draft_reply_shows_the_same_text_with_no_model_call` | M6-ESC | M6-PLAN, M6-POL |
| B20 | `test_m06_s062_autonomy.py::test_od11_draft_reply_on_an_escalation_without_text_writes_one_owner_approved_draft` | M6-ESC | M6-PLAN, F0-S (`ALERT_DRAFT`) |
| C1 | `test_m06_s063_takeover_pause.py::test_rg19_an_owner_manual_message_cancels_a_scheduled_send_within_the_delay` | M6-PLAN | M6-SCHED |
| C2 | `test_m06_s063_takeover_pause.py::test_rg19_pause_during_scheduled_cancels` | M6-PLAN | M6-SCHED |
| C3 | `test_m06_s063_takeover_pause.py::test_pause_during_executing_is_not_undone_but_no_new_send_starts` | M6-TXA | M6-PLAN |
| C4 | `test_m06_s063_takeover_pause.py::test_an_overdue_scheduled_send_after_a_40_minute_outage_is_cancelled_late_and_converted` | M6-PLAN | M6-SCHED |
| C5 | `test_m06_s063_takeover_pause.py::test_pause_behind_a_1000_update_backlog_still_blocks_the_next_autonomous_send` | M6-PLAN | M6-SCHED, QA-X driver (`poll`) |
| C6 | `test_m06_s063_takeover_pause.py::test_a_stop_word_pauses_while_the_model_is_down` | M6-PLAN | M6-SCHED |
| C7 | `test_m06_s063_takeover_pause.py::test_rg19_a_blocked_bot_pauses_autonomy_and_cancels_scheduled_sends` | M6-PAUSE | M6-PLAN, QA-X driver (`poll`) |
| C8 | `test_m06_s063_takeover_pause.py::test_rg19_the_pause_button_cancels_scheduled_sends_and_only_the_owner_can_press_it` | M6-PAUSE | M6-PLAN, QA-X driver (`poll`) |
| C9 | `test_m06_s063_takeover_pause.py::test_j8_a_takeover_pauses_the_chat_and_tells_the_owner_once` | M6-TAKE | M6-TXA |
| C10 | `test_m06_s063_takeover_pause.py::test_j8_resume_here_restores_autonomy_in_that_chat` | M6-TAKE | M6-TXA |
| C11 | `test_m06_s063_takeover_pause.py::test_j8_a_takeover_before_the_delegation_started_does_not_block` | M6-TAKE | M6-TXA |
| C12 | `test_m06_s063_takeover_pause.py::test_j9_pause_stops_autonomy_keeps_drafts_and_resume_restores_it` | M6-TXA | M6-PLAN |
| C13 | `test_m06_s063_takeover_pause.py::test_j9_a_send_refused_as_possibly_paused_stops_and_tells_the_owner_once` | M6-TXA | M6-ESC |
| C14 | `test_m06_s063_takeover_pause.py::test_rg19_a_forbidden_control_send_pauses_autonomy_and_cancels_scheduled_sends` | M6-PAUSE | M6-PLAN |
| C15 | `test_m06_s063_takeover_pause.py::test_a_rights_loss_a_disabled_connection_or_ignore_mode_cancels_the_scheduled_reply` | M6-PLAN | M6-SCHED |
| C16 | `test_m06_s063_takeover_pause.py::test_inv5_a_pause_word_typed_in_the_delegated_chat_is_a_takeover_not_the_kill_switch` | M6-TAKE | M6-TXA |
| C17 | `test_m06_s063_takeover_pause.py::test_every_autonomy_card_carries_the_pause_button` | M6-TAKE | M6-DELEG, M6-ESC, M6-CATCH, M6-PAUSE, M6-PLAN, M6-TXA |
| D1 | `test_m06_s064_catch_up.py::test_s064_the_catch_up_lists_every_in_scope_message_in_the_window` | M6-DELEG | M6-CATCH |
| D2 | `test_m06_s064_catch_up.py::test_s064_the_catch_up_links_sent_items_including_failed_and_uncertain` | M6-DELEG | M6-CATCH, M6-TXA |
| D3 | `test_m06_s064_catch_up.py::test_s064_the_catch_up_is_produced_even_with_zero_activity` | M6-DELEG | M6-CATCH |
| D4 | `test_m06_s064_catch_up.py::test_s064_the_catch_up_fires_at_delegation_end` | M6-LIFE | M6-DELEG, M6-CATCH |
| E1 | `test_m06_escalation.py::test_j5_a_decision_request_alerts_the_owner_at_once` | M6-PLAN | M6-ESC |
| E2 | `test_m06_escalation.py::test_a_non_urgent_escalation_waits_for_the_catch_up` | M6-PLAN | M6-ESC, M6-CATCH |
| E3 | `test_m06_escalation.py::test_a_repeated_suspicious_burst_alerts_once_and_nothing_is_sent` | M6-PLAN | M6-ESC |
| E4 | `test_m06_escalation.py::test_llm_down_ten_minutes_pauses_each_delegation_once_and_falls_back_to_escalation` | M6-PLAN | M6-ESC |
| E5 | `test_m06_escalation.py::test_one_draft_path_per_burst_in_a_covered_assist_chat` | M6-PLAN | M6-ESC |
| E6 | `test_m06_escalation.py::test_an_urgent_decision_waits_for_the_end_of_quiet_hours_unless_the_delegation_allows_the_bypass` | M6-ESC | M6-PLAN, M6-DELEG |
| F1 | `test_m06_j10_status.py::test_j10_every_autonomous_send_shows_its_delegation` | M6-TXA | STAT-ACT |
| F2 | `test_m06_j10_status.py::test_rg20_a_scheduled_autonomous_reply_is_shown_as_scheduled_never_sent` | M6-PLAN | STAT-ACT |
| F3 | `test_m06_j10_status.py::test_status_shows_delegations_takeover_pauses_and_languages` | M6-TAKE | STAT-ACT |
| F4 | `test_m06_j10_status.py::test_rg08_list_delegations_taints_the_run_and_is_a_blocked_stub_without_consent` | RD-DEL | M6-DELEG |

### 4.2 QA-S7 (`m07/`, QE1)

Source: QA-S7 §2, §3.3–3.8.

| ID | File / exact test name | Turns green | Other needs |
|---|---|---|---|
| G1 | `test_m07_s071_reminders_tasks.py::test_s071_a_daily_reminder_fires_at_local_time_across_a_dst_change` | M7-REM | RRULE |
| G2 | `test_m07_s071_reminders_tasks.py::test_s071_snooze_complete_and_cancel` | M7-TASK | — |
| G3 | `test_m07_s071_reminders_tasks.py::test_s071_a_recurring_reminder_schedules_its_next_occurrence` | M7-REM | RRULE |
| G4 | `test_m07_s071_reminders_tasks.py::test_s071_a_restart_before_due_fires_late_with_a_note` | M7-REM | RRULE |
| G5 | `test_m07_s071_reminders_tasks.py::test_s071_a_snoozed_task_resurfaces_at_snoozed_until` | M7-TASK | — |
| G6 | `test_m07_s071_reminders_tasks.py::test_rg20_the_reminder_card_buttons_never_misreport` | M7-REM | M7-TASK |
| G7 | `test_m07_s071_reminders_tasks.py::test_a_recurring_reminder_card_offers_only_pause_reminder` | M7-AUTO | M7-REM |
| G8 | `test_m07_s071_reminders_tasks.py::test_an_owner_reminder_is_delivered_during_quiet_hours` | M7-REM | — |
| G9 | `test_m07_s071_reminders_tasks.py::test_reminders_and_follow_up_deadlines_fire_while_the_llm_is_down` | M7-FUP | M7-REM |
| G10 | `test_m07_s071_reminders_tasks.py::test_s071_a_skipped_reminder_or_follow_up_deadline_is_reported_never_silent` | M7-FUP (the `occurrence_skipped` renderer is M7-FUP's, R2) | M7-REM, M7-TASK |
| G11 | `test_m07_s071_reminders_tasks.py::test_s071_undo_cancels_a_one_off_reminder_before_it_fires_and_changes_nothing_after` | M7-REM | M7-TASK |
| G12 | `test_m07_s071_reminders_tasks.py::test_rg11_a_run_with_forwarded_text_needs_a_tap_for_reminders_and_task_changes` | `create_reminder`: M7-REM; `update_task`: M7-TASK | M7-TASK |
| G13 | `test_m07_s071_reminders_tasks.py::test_s071_tomorrow_is_the_next_local_day_at_the_reminders_time_across_a_dst_change` | M7-REM | M7-TASK |
| H1 | `test_m07_s072_j7_followups.py::test_j7_documents_arrive_the_task_closes_and_the_owner_is_told` | M7-FUP | M7-TASK |
| H2 | `test_m07_s072_j7_followups.py::test_j7_documents_do_not_arrive_the_owner_approves_the_nudge_and_it_is_sent_once` | M7-NUDGE | NUDGE-POL, M7-FUP |
| H3 | `test_m07_s072_j7_followups.py::test_s072_a_condition_met_just_before_the_deadline_is_met_never_missed` | M7-FUP | M7-TASK |
| H4 | `test_m07_s072_j7_followups.py::test_s072_window_closed_at_the_deadline_gives_copy_ready_only` | M7-NUDGE | NUDGE-POL, M7-FUP |
| H5 | `test_m07_s072_j7_followups.py::test_a_late_ingest_gives_no_false_missed_and_a_false_close_can_be_reopened` | M7-FUP | M7-TASK |
| H6 | `test_m07_s072_j7_followups.py::test_j7_step2_an_open_commitment_surfaces_at_its_due_time` | `plain`: COMMIT-DUE; `followed_up`: M7-FUP | `followed_up`: COMMIT-DUE |
| H7 | `test_m07_s072_j7_followups.py::test_s072_documents_arriving_after_a_missed_reminder_send_an_arrived_after_all_correction` | M7-FUP | M7-TASK |
| H8 | `test_m07_s072_j7_followups.py::test_s072_one_closure_notice_per_task_whether_triage_or_the_follow_up_closes_it_first` | M7-FUP | M7-TASK |
| H9 | `test_m07_s072_j7_followups.py::test_s072_a_follow_up_in_a_chat_set_to_ignore_says_it_cannot_check_and_one_the_owner_completed_ends_silently` | M7-FUP | M7-TASK, M7-NUDGE |
| H10 | `test_m07_s072_j7_followups.py::test_rg08_a_missed_follow_up_after_the_managed_consent_was_revoked_reminds_without_a_nudge` | M7-NUDGE | M7-FUP, NUDGE-POL |
| J1 | `test_m07_s073_automations.py::test_s073_update_recomputes_next_run_at` | M7-AUTO | M7-REM |
| J2 | `test_m07_s073_automations.py::test_s073_delete_cancels_a_queued_occurrence` | M7-AUTO | M7-REM |
| J3 | `test_m07_s073_automations.py::test_s073_create_and_modify_require_confirmation` | M7-AUTO | — |
| J4 | `test_m07_s073_automations.py::test_a_scheduled_summary_rechecks_consent_and_isolates_each_chat` | M7-SSUM | — |
| J5 | `test_m07_s073_automations.py::test_settings_automations_pause_resume_and_delete_after_a_confirm` | M7-AUTO | SET-P3 |
| J6 | `test_m07_s073_automations.py::test_what_automations_do_i_have_lists_every_kind_and_taints_the_run` | RD-M7 | M7-REM, M7-FUP, M7-AUTO |
| J7 | `test_m07_s073_automations.py::test_rg08_list_tasks_hides_snoozed_tasks_taints_the_run_and_is_a_blocked_stub_without_consent` | RD-M7 | M7-TASK, M7-REM |
| J8 | `test_m07_s073_automations.py::test_the_delegation_end_automation_is_never_offered_to_the_automation_controls` | M7-AUTO | SET-P3, M6-DELEG, M6-LIFE |
| J9 | `test_m07_s073_automations.py::test_monitor_actions_create_task_and_digest` | M7-MACT | M7-TASK |
| K1 | `test_m07_s074_memory.py::test_s074_pending_review_suggestions_never_reach_a_prompt` | M7-MEX | SENS |
| K2 | `test_m07_s074_memory.py::test_s074_memory_candidates_carry_a_required_category` | M7-MEX | SENS |
| K3 | `test_m07_s074_memory.py::test_rg15_sensitive_candidates_are_dropped_before_pending_review` | M7-MEX | SENS |
| K4 | `test_m07_s074_memory.py::test_rg16_no_emotion_personality_or_trust_field_reaches_the_model_storage_or_cards` | M7-MEX | SENS |
| K5 | `test_m07_s074_memory.py::test_review_cards_approve_edit_and_discard` | M7-MRV | — |
| K6 | `test_m07_s074_memory.py::test_an_approved_suggestion_reaches_only_its_contacts_drafts` | M7-MRV | — |
| K7 | `test_m07_s074_memory.py::test_review_cards_are_capped_at_five_per_owner_local_day` | M7-MEX | SENS |
| K8 | `test_m07_s074_memory.py::test_rg08_a_revoked_consent_stops_a_queued_extraction_before_any_model_call` | M7-MEX | SENS |
| K9 | `test_m07_s074_memory.py::test_s074_a_pending_suggestion_is_withdrawn_when_its_cited_message_is_deleted_or_edited` | M7-MEX | SENS, M7-MRV |
| N1 | `test_m07_s074_export.py::test_s074_export_covers_every_class_only_the_owners_rows_no_secrets_control_chat_only` | M7-EXP | GW-DOC |
| N2 | `test_m07_s074_export.py::test_s074_export_is_split_under_the_part_limit` | M7-EXP | GW-DOC |
| N3 | `test_m07_s074_export.py::test_s074_export_is_limited_to_once_a_day_refuses_a_foreign_control_chat_and_needs_its_flag` | M7-EXP | GW-DOC |
| N4 | `test_m07_s074_export.py::test_rg20_an_uncertain_export_part_is_never_resent_and_is_reported_not_confirmed` | M7-EXP | GW-DOC |
| L1 | `test_m07_rating.py::test_the_weekly_rating_card_arrives_sunday_at_19_local_once_per_week` | OPS-RATE | — |

### 4.3 QA-X (`p3_cross/`, SQ)

Source: QA-X §2, §3.3–3.7b. Keep every listed row; the prose count of 11 is stale (there are 14 IDs).

| ID | File / exact test name | Turns green | Other needs |
|---|---|---|---|
| X1a | `test_x1_delegation_and_nudge.py::test_x1_a_missed_follow_up_in_a_delegated_chat_gets_an_approval_card_never_an_autonomous_send` | M7-NUDGE | NUDGE-POL, M7-FUP, M6-PLAN, M6-TXA |
| X1b | `test_x1_delegation_and_nudge.py::test_x1_documents_arriving_in_a_delegated_chat_close_the_follow_up_with_at_most_one_reply` | M7-FUP | M6-PLAN, M6-TXA |
| X2a | `test_x2_takeover_and_nudge.py::test_x2_an_owner_ok_after_the_promise_pauses_autonomy_but_the_nudge_card_still_comes` | M7-NUDGE | M6-TAKE, NUDGE-POL, M7-FUP, M6-PLAN |
| X2b | `test_x2_takeover_and_nudge.py::test_x2_an_owner_message_after_the_nudge_card_outdates_it_and_nothing_is_sent` | M7-NUDGE | NUDGE-POL, M7-FUP, M6-TAKE |
| X3a | `test_x3_pause_and_reminders.py::test_x3_pause_cancels_scheduled_autonomous_sends_but_reminders_still_fire` | M7-REM | M6-PLAN, M6-TXA |
| X3b | `test_x3_pause_and_reminders.py::test_x3_a_nudge_card_approved_while_paused_is_sent_once_without_the_ai_line` | M7-NUDGE | NUDGE-POL, M7-FUP, M6-PLAN |
| X4a | `test_x4_deletion_across_p3.py::test_x4_deleting_a_scope_removes_every_p3_class_it_names` | green at merge (F0-S) | — |
| X4b | `test_x4_deletion_across_p3.py::test_x4_deleting_a_contact_after_real_flows_leaves_nothing_that_names_them` | M7-FUP | M6-DELEG, M7-REM, M6-PLAN |
| X4c | `test_x4_deletion_across_p3.py::test_x4_an_export_taken_after_a_deletion_holds_none_of_the_deleted_content` | M7-EXP | GW-DOC |
| X5a | `test_x5_restore_with_a_delegation.py::test_x5_a_restore_ends_active_delegations_and_sends_nothing` | green at merge (F0-S) | — |
| X5b | `test_x5_restore_with_a_delegation.py::test_x5_after_a_restore_nothing_resumes_until_the_owner_delegates_again` | M6-LIFE | M6-DELEG, M6-PLAN, M6-TXA |
| X5c | `test_x5_restore_with_a_delegation.py::test_x5_a_restore_reports_skipped_reminders_and_follow_ups_and_fires_nothing_stale` | M7-FUP | M7-REM, F0-R (registry) |
| X6a | `test_x6_revoke_across_p3.py::test_x6_revoking_managed_consent_ends_the_delegation_and_stops_the_nudge_but_the_reminder_still_fires` | M7-NUDGE | M6-LIFE, M6-SCHED, M6-CATCH, M6-PLAN, M6-TXA, M7-REM, M7-FUP, NUDGE-POL |
| X7a | `test_x7_ignore_mode_across_p3.py::test_x7_setting_a_delegated_chat_to_ignore_cancels_the_scheduled_reply_and_the_follow_up_says_it_cannot_check` | M7-FUP | M6-SCHED, M6-PLAN, M6-TXA, NUDGE-POL, M7-NUDGE |

### 4.4 Journeys, helpers and hygiene

| Package / source | Named test or set | Last unit |
|---|---|---|
| QA-S6 §3.10 | `test_m06_j5_journey.py::test_j5_studying_until_seven_end_to_end` | M6-LIFE; needs M6-DELEG/PLAN/TXA/TAKE/ESC/CATCH, GRND |
| QA-S6 §3.9 | Six helper self-tests in `test_m06_steps.py` | Unmarked on CP1; driver additions first |
| QA-S6 §3.11 | `test_log_hygiene_qa_s6.py::test_the_m06_journey_logs_no_content` | M6-TXA; M6-PLAN |
| QA-S7 §3.9 | Six helper self-tests in `test_m07_steps.py` | Unmarked on CP1 |
| QA-S7 §3.10 | `test_log_hygiene_qa_s7.py::test_the_m07_journeys_log_no_content` | M7-NUDGE; M7-FUP/REM |
| QA-X §2.2 | `test_p3_drive.py::test_settings_overrides_reach_the_wire_functions`; `::test_the_recorder_keeps_document_bytes_beside_the_documents_list` | QA-X; recorder needs GW-DOC |
| QA-X §3.9 | `test_log_hygiene_qa_x.py::test_cross_team_deletion_logs_no_content` | F0-S, unmarked on CP1 |

## 5. Live marker scheme (`P3-nn`)

Use a fresh numeric block per window, with one or two prefix digits plus a two-digit suffix: prefix `P3-60`, message `P3-6001 Synthetic question…`, extracted number `6001`. This fits OPS-TV3 §3.8's `^P[23]-[0-9]{1,2}$` prefix and one/two suffix digits. No leading names, links or production identifiers. Body text is synthetic; published evidence stores only the number ([OPS-TV3 §3.6, §3.8](../specs/OPS-TV3.md#38-script-prefix---expect-numbers---direction----answers)).

| Block | Allocation |
|---|---|
| `P3-60` | Shadow: 6001–6007; unused 6008–6099 for a later, explicitly logged retry |
| `P3-61` | J5: 6101–6124; unused 6125–6199 for later attempts |
| `P3-70` | J7: 7001 promise c; 7002 promise b; 7003 promise a; 7004 synthetic document a |
| `P3-71` | Reminder outage marker 7101 in F1, never in silent J7 chat |
| `P3-72` | Memory-review synthetic inputs 7201–7299, allocated in that run's brief |
| `P3-90` | Dev-only LV-16 diagnostic text; never counted as production journey evidence |

The checklists give each send. Client commands/buttons are steps, not contact markers. Do not reuse a marker on a retry. Run `script` separately for `contact` and `owner_manual` with exact number lists, and `--answers` for contact markers. Include optional/retry markers only if sent. The script has no time-window filter: each expectation includes earlier retained markers in that prefix, or a new unused prefix is allocated and recorded. A deleted marker needs its earlier evidence link; it cannot silently disappear from the expected set. A document marker belongs in its synthetic caption. The J7 silence leg receives no extra marker or client probe ([OPS-TV3 §3.8](../specs/OPS-TV3.md#38-script-prefix---expect-numbers---direction----answers), [LIVE-P3 §3.7](../specs/LIVE-P3.md#37-j7-in-three-legs-second-account-follow-ups-and-nudges-as-approval-cards-only-od-18)).

## 6. Release, trial and evidence

Use [M06](live-checklist-M06.md), [M07](live-checklist-M07.md), [eval operations](eval-ops-runbook.md), [LV-16](lv16-runbook.md), [evidence](evidence-template.md) and [trial log](trial-log-template.md). `p3-verifier.md` and actual evidence are later OPS-TV3/LIVE-P3 deliverables. SQ records only safe projections supplied by TL: states, enums, counts, timings and synthetic markers. No message text, names, usernames, filenames, content hashes, secrets or production identifiers enter this public folder ([LIVE-P3 §3.2, §4 L3](../specs/LIVE-P3.md#32-rules-for-every-live-check); launch boundary).

The LV-16 executor is TL under the dev-bot standing exception (owner update 2026-10-30, OD-27). Its current command cannot stop an internal multi-send loop on 429; the runbook records this gap. Enable 4 requires accepted evidence, not merely a scheduled probe.

## 7. Source gaps and proposed edits outside QA

Only TL applies these after review; QA-P3 edits no file outside this folder. The first two rows follow the current owner's explicit instruction. The R1 row needs a TL ruling, not a QA waiver.

| File / section | Gap | Exact proposed text or replacement |
|---|---|---|
| `decisions.md` §A, append a 2026-10-30 answer | OD-27 still says owner runs it; no P3-T166 or 2026-10-30 answer block exists in this checkout | “OD-27 (owner, 2026-10-30): TL runs the LV-16 rate retest on the dev bot under AGENTS §9. SQ supplies the runbook; TL records the content-free result in TF §12 before enable 4. The owner performs any client actions.” |
| `README.md` §3 OWN-LV16/§7; `ORG.md` §2 OWN-LV16; `handoffs/senior-qa.md` §1; `handoffs/tech-lead.md` §1/§4.4 | Executor references are stale | Replace LV-16 “owner-executed”, “owner runs it” or “the owner's LV-16 run” with “TL-executed on the dev bot under the owner’s 2026-10-30 OD-27 instruction”; keep client actions with owner and SQ as runbook author |
| `AGENTS.md` §2/§16 and TF §12/§13 | TF §12 recording/Verified wording assumes owner-executed tests only | Append: “Exception (owner decision 2026-10-30, OD-27): TL may execute the dev-bot LV-16 rate retest under AGENTS §9 and record its observation in TF §12, with executor and authority explicit. It must not be described as owner-executed; the existing Verified-label rule otherwise remains.” |
| `tools/probe/probe.py::cmd_burst`; OWN-LV16 procedure | Multi-send loop continues after 429; no pacing argument. No documentation-only fix supplies the required stop | Record: “LV-16 multi-send execution is blocked: the existing burst loop does not stop on 429. Supervised count-one calls are diagnostic evidence only where their measured pace does not exercise the intended load. Enable 4 waits for accepted complete evidence.” TL must resolve the execution method; QA-P3 changes no probe code |
| `specs/QA-S6.md` §2, §5.1 | B19/B20 required in §3.4/§4, omitted by B18 ceiling | Replace `B1–B18` with `B1–B20` in both inventories. Append: “There are 65 scenario IDs, plus the J5 journey, helper self-tests and hygiene.” |
| `decisions.md` §B10 P3-T156, append correction | Count says about 63 M06 / 11 cross-team | “Correction to P3-T156: QA-S6 §3 has 65 scenario IDs plus J5; QA-S7 has 46; QA-X has 14. No scenario is dropped.” |
| `README.md` §3/§5; `WORKFLOW.md` §7; QA-S7 J4; P3-T158 | M7-SSUM may land after R1 while R1 requires all scenarios green | Pending ruling, append: “QA-S7 J4 cannot be counted green while M7-SSUM is unmerged. The post-R1 lane and the all-scenarios-green R1 criterion require an explicit TL reconciliation; QA grants no waiver.” |
| `IMPLEMENTATION_PLAN.md` §10, after RG table | P3 test names/extensions and C9 not yet traced there | Append: “P3 regression traceability (planned): see docs/engineering/p3/qa/regression-matrix.md for RG-13/14/15/16, extensions to RG-05/06/08/09/10/11/17/18/19/20/21, exact named tests, owning units and scenario IDs. RG-20 also retains tests/unit/test_gateway_and_render.py::test_partial_control_delivery_is_uncertain_and_never_retried and tests/unit/test_agent_channel.py::test_partial_control_reply_is_reported_uncertain_without_replay (GW-ESC C9, P2 #133). Planned or xfailed coverage is never green evidence.” |
| `IMPLEMENTATION_PLAN.md` §9 Data requirements | Temporary-file wording differs from OD-20 and M7-EXP's in-memory design | Replace `temporary export file deleted after delivery` with `export parts built in memory and never written to disk` |
| `README.md` §7, owner readiness items | Eval checkout configuration step missing (GATE-RUN §9 already flags it) | Add: “Before the first GATE-RUN live run, the owner supplies dev provider configuration in the TL-owned eval checkout. Agents never open, print or copy that file; healthcheck reports setting names only.” |

P3-interfaces §15.5 and decisions P3-T158 intentionally leave some marker ordering to audit. In particular QA-S6 B17 says green at merge while listing later draft/planner dependencies; QA-X X7a names M7-FUP but needs M7-NUDGE. Carry their full dependencies into the QA-X §3.8 audit; do not edit scenario bodies to fit a guessed order. The `poll` helper also appears in QA-S6 §3.2's driver dependency but not QA-X §2.2's explicit driver list: TL should append there, “Add the poll-only driver helper required by QA-S6: store a FakeUpdateSource batch and run its fast lane without processing the inbox; self-test that pending inbox rows remain pending.”

GitHub issue #198 and integration-issue reads were unavailable through the configured proxy in this launch. TL must compare this package with any newer owner-authored rulings, especially P3-T166, before accepting it. No inaccessible ruling is assumed and no required check is reported run.

