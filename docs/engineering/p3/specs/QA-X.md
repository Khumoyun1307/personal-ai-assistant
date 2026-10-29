# QA-X — cross-team scenarios (strict xfail first)

| | |
|---|---|
| **Unit** | QA-X — the cross-team scenario package `tests/scenarios/p3_cross/`: delegation + nudge in one chat; takeover vs nudge; `/pause` vs reminders; delete a contact (and a chat, managed data, everything) across delegations, tasks and automations (PRD R1 #7 extended) and an export taken afterwards; restore with an active delegation, and with due reminders and follow-ups; `/privacy revoke` across a delegation, a reminder and a follow-up nudge; Ignore mode across a delegation and a follow-up. Also the scenario-driver additions every P3 package needs (SQ owns the driver after CP1, R57) and the marker audit at CP-A and R1 |
| **Owner slot** | Senior QA (SQ, Codex), launched by the TL (`codex exec`, workspace-write in SQ's own worktree, `COMPOSE_PROJECT_NAME=sq`) |
| **Reviewer** | Josh lead + Edvard lead (scenario fidelity, each for its team's half), dispatched by the TL; TL light review |
| **Size** | S+ (≈ 0.75 engineer-day; the skeleton must merge by D+1.5) |
| **Depends on** | F0-E (`tests/scenarios/p3_drive.py`, `tests/scenarios/p3_cross/conftest.py`, `tests/log_hygiene.py`), F0-S (factories, `purge_p3`, post-restore step 2b, every contract name). Most scenarios turn green only as M06 and M07 units merge (§3) |
| **Branch → base** | `p3/qa-x` → `int/p3` (draft stacked on `p3/f0-e` before CP1) |
| **QA lane** | none (SQ is QA); peer + TL review only (WORKFLOW §6.3) |
| **Normative inputs** | IMPLEMENTATION_PLAN §8 S06.1 (deletion regression), §9 acceptance (R1 #7 across every class; all R2 re-verified), §10 (RG-19, RG-21); PRD J5, J7, J9, R-09, §11.4, §12.2, FR-FUP-06, NFR-09, R1 #7; P3-interfaces §2.2, §4.3, §4.5, §6.1, §8.1, §11, §12, §15.1; P3-schema §3.1, §3.3, §3.4, §5, §7, §8; F0-E §3.4, §4, §9 note 3; WORKFLOW §5, §6.5; [senior-qa.md](../handoffs/senior-qa.md); [QA-S6](QA-S6.md), [QA-S7](QA-S7.md); decisions OD-5, OD-11, OD-12, OD-17, OD-18, OD-20, OD-31, P3-T48; TL rulings R47, R57, R74 |

## 1. Goal

Prove, end to end through the production wiring, the places where Josh's autonomy path and Edvard's M07 flows meet in one chat or one data class, so a defect that each team's own scenarios cannot see (the nudge treated as a takeover, a pause that stops reminders, a deletion that leaves a delegation or a reminder naming a deleted contact, a restore that revives a delegation) fails CI. SQ writes the scenarios from the contracts and the PRD, before the units land, each strict xfail with the unit whose merge turns it green; owns the shared scenario driver after CP1; and audits the markers before CP-A and R1.

## 2. Deliverables (exact files)

### 2.1 The package (only SQ edits it after CP1, except marker removals, §3.1 rule 9)

| File | Content |
|---|---|
| `tests/scenarios/p3_cross/conftest.py` (F0-E created it with fixture `p3x`) | Add fixture `p3x_env(flags, settings, timezone)` |
| `tests/scenarios/p3_cross/steps.py` (new) | Package helpers (§3.2). No import from `tests/scenarios/m06` or `m07` (packages merge in any order) |
| `tests/scenarios/p3_cross/test_p3_cross_steps.py` (new) | Helper self-tests that pass on the CP1 base. Unmarked |
| `tests/scenarios/p3_cross/test_x1_delegation_and_nudge.py` (new) | §3.3 |
| `tests/scenarios/p3_cross/test_x2_takeover_and_nudge.py` (new) | §3.4 |
| `tests/scenarios/p3_cross/test_x3_pause_and_reminders.py` (new) | §3.5 |
| `tests/scenarios/p3_cross/test_x4_deletion_across_p3.py` (new) | §3.6 |
| `tests/scenarios/p3_cross/test_x5_restore_with_a_delegation.py` (new) | §3.7 |
| `tests/scenarios/p3_cross/test_x6_revoke_across_p3.py` (new) | §3.7a |
| `tests/scenarios/p3_cross/test_x7_ignore_mode_across_p3.py` (new) | §3.7b |
| `tests/scenarios/p3_cross/test_log_hygiene_qa_x.py` (new) | §3.9 |

### 2.2 Driver additions (`tests/scenarios/p3_drive.py`, `tests/scenarios/test_p3_drive.py`; SQ owns both after CP1, F0-E §4)

The **first commit** of this PR; QA-S6 and QA-S7 rely on them. Add only what F0-E's driver lacks, with these names:

1. `p3_environment(migrated_url, *, flags, settings: Mapping[str, object] | None = None)`: each item is passed to the `Settings(...)` the driver builds (for example `{"autonomy_languages": "en", "export_max_part_mb": 1}`), so the wire functions see it. Until this lands, a package fixture may set the same values with `monkeypatch.setenv`.
2. `P3Scenario` members (the `M05Scenario` names where they exist): `env`, `fake`, `app`, `handlers`, `settings`, `account_id`, `conversation_id`, `contact_id`, `date`; `run_task(name, **args)`, `run_periodic(name)`, `require(*names)` (F0-E); `rows(model)`; `message(number, text, *, owner=False, chat=None, date=None, extra=None) -> int`; `owner(tool, *, forwarded=False, **values)`; `preferences(**values)`; `revoke()`; `output(start=0)`.
3. A recording sender (a `FakeTelegramSender` subclass, as `m05_drive._RecordingSender`) that also keeps every control-chat document's bytes in `document_bytes`, parallel to GW-DOC's `documents` list (synthetic data only; never logged or printed). Until GW-DOC merges, the override is inert (the base class has no `send_document`).
4. Self-tests in `tests/scenarios/test_p3_drive.py`: `test_settings_overrides_reach_the_wire_functions` (an override of `autonomy_send_delay_seconds` is the value `AutonomyConfig.send_delay` holds), `test_the_recorder_keeps_document_bytes_beside_the_documents_list` (unmarked if GW-DOC is on the base, else `awaiting GW-DOC`).

## 3. Content

### 3.1 Rules for every scenario in this package

1. **Header and fixture.** `pytestmark = pytest.mark.integration`; one fixture (`p3x_env`) per test. Default timezone `Asia/Tashkent`; the clock starts at the driver's `NOW`, Sunday 2026-10-18 12:00 UTC (17:00 local).
2. **Flags per test** (none by default). Autonomy tests add `actions.send`, `autonomy.run`, `autonomy.send`, `triage.run`, `triage.assist_trigger`, `notifications.deliver` and `AUTONOMY_LANGUAGES=en`; follow-up tests add `followups.run`, `tool.create_follow_up`, `followups.nudge`; reminder tests add `reminders.run`, `tool.create_reminder`; deletion tests add `tool.delete_conversation_data`, `tool.delete_contact_data`; the export test adds `export.enabled`; X7 adds `tool.set_conversation_mode`.
3. **Marker.** `@pytest.mark.xfail(strict=True, reason="awaiting <ID>")` with the "Turns green" unit: the unit planned to merge last among the scenario's needs (README §3 schedule; ties go to the unit whose behaviour the scenario proves). A scenario green on the CP1 base is unmarked. If a scenario that should be green on the base fails, that is a defect of the named F0 unit: file it on that unit's issue with the scenario as evidence and mark the scenario `awaiting <that unit>` until the fix PR, which removes the marker.
4. **A skeleton is a complete test** (a body-less strict-xfail test would XPASS).
5. **Positive control** in every scenario that asserts an absence.
6. **Imports** at module level only for CP1 names (`assistant.core.*`, `assistant.actions.{disclosure,states,models}`, `assistant.privacy.{content_columns,post_restore,deletion}`, `assistant.inbox.notification_types`, ORM models, `tests.integration.{business_env,agent_env,p2_factories,p3_factories}`, `tests.scenarios.{p3_drive,p2_drive}`, `tests.log_hygiene`).
7. **Seeds** only for state not under test (comment "seeded: not under test"). X4a and X5a seed on purpose: their subject is deletion and restore, so every class is seeded with factories and the subject runs through its real entry point.
8. **Determinism and data.** `FakeClock` only; no sleeps; synthetic English text; canaries `CANARY-SYNTH-P3-<n>` (account A) and `CANARY-SYNTH-P3-B<n>` (account B). Assertions read ids, states, reasons, codes, counts, audit `details` and the content-column scan.
9. **Markers come off on the feature unit's branch (R47).** A feature PR never edits `tests/scenarios/p3_cross/`. When a scenario marked `awaiting <ID>` starts to pass on that unit's branch, CI turns red (strict XPASS); the unit's author comments on the QA-X issue and SQ pushes one commit to the unit's branch that removes exactly the markers of the scenarios that pass (P3-interfaces §15.1). Whichever of this PR and a unit PR is ready first merges; a unit already merged when this PR is rebased has its markers dropped by SQ in this PR (P2-T48). Never delete or weaken a scenario.
10. **Expected-failure classification** in the PR description (scenario → first failure point on the base); a failure of the scenario's own making is fixed, never marked.

### 3.2 Helpers (`steps.py`)

| Name | What it does |
|---|---|
| `delegated_chat(s, *, chats, allowed=ALL_BEHAVIOURS, notes=(…,), ends_in=timedelta(hours=3)) -> Deleg` | Seeds an active delegation (`seed_delegation` + delegation-scope notes), as QA-S6 §3.2 |
| `draft(**changes)`, `triaged(**changes)`, `nudge_draft(**changes)` | The delegated draft, triage and `DraftResult` dicts with passing values (QA-S6 §3.2, QA-S7 §3.2) |
| `reply_cycle(s, number, text, result, *, chat=None, commitment=None) -> Action | None` | Contact burst → triage (with an optional commitment) → `replies.delegated` → release → execute |
| `scheduled_reply(s, number, text, result, *, chat=None) -> Action` | The same, stopped while the action is `scheduled` |
| `promise_and_follow_up(s, *, chat, deadline, number=101) -> FollowUp` | Contact promise "Synthetic: I will send the signed documents tonight." with a triage commitment, then `create_follow_up(expected={"kind": "media_received", "terms": [], "media_kinds": ["document"]}, draft_nudge=True, …)` and Confirm. Returns `FollowUp(task_id, automation_id)` |
| `miss_and_nudge(s, follow_up, draft) -> Action` | Advance to the deadline; `system.reconcile_schedules`; drain `automation.fire`, `notify.deliver`, `followups.nudge` (with `draft`), `notify.deliver`; return the nudge action |
| `owner_manual`, `owner_says`, `owner_tool`, `confirm`, `press`, `drain`, `deliver_alerts`, `fire_due` | As in QA-S6 §3.2 and QA-S7 §3.2 |
| `seed_every_p3_class(s, account_id, *, contact_k, chat_k, contact_l, chat_l, prefix) -> Seeded` | §3.6 seeds for one account; returns the ids and the canary of each item |
| `content_scan(s, account_id, canaries) -> dict[str, list[tuple[str, str]]]` | For each canary, the `(table, column)` pairs of `privacy.content_columns.CONTENT_COLUMNS` where an account-scoped row contains it (one `SELECT … WHERE account_id = :a AND col::text LIKE :p` per column; the R1 #7 canary pattern) |
| `revoke(s)` | `s.revoke()` of `ai_managed` only (the owner keeps `ai_control`); if the driver helper revokes both consents, SQ adds the narrower helper to the driver (§2.2) |
| `export_parts(s) -> list[bytes]` | The control-chat document bytes the driver's recorder kept (§2.2 item 3); parsed in memory, never logged or printed |
| `business_sends(s)`, `actions(s, chat=None)`, `notifications(s, kind)`, `audits(s, event)` | Reads |

In the tables, "run `system.…`" means `s.run_periodic(...)` and "drain `<task>`" means `drain(s, task)`. **Scripted model outputs** are queued immediately before the job that consumes them, and the helper asserts afterwards that `FakeLLM.parsed` is empty, so a job that did not run fails loudly instead of handing its output to the next model call.

### 3.3 X1 — delegation and nudge in one chat (`test_x1_delegation_and_nudge.py`)

| ID | Test | Setup and steps | Expected | Turns green | Needs |
|---|---|---|---|---|---|
| X1a | `test_x1_a_missed_follow_up_in_a_delegated_chat_gets_an_approval_card_never_an_autonomous_send` (FR-FUP-06; OD-18; invariants 1, 2, 13) | Delegation over C1. The promise at 17:00 is answered by the delegated path (`reply_cycle` with an `acknowledge_and_defer` draft). `promise_and_follow_up(deadline="2026-10-18 20:00")` (window open). Nothing arrives. `miss_and_nudge`; press Approve; drain `actions.execute` | The 17:00 reply is auto-sent (positive control). The nudge action: origin `agent_proposal`, `trigger_automation_id` = the follow-up, `delegation_id` NULL, `awaiting_approval`, never `scheduled`, no `action_auto_approved` audit, no `AI_MARKER`, `disclosure_included = false`; the nudge request used prompt `draft_reply.v1` (never `draft_delegated`) with `tools == ()`. Before Approve: one business send; after: two, the second exactly the nudge text | M7-NUDGE | NUDGE-POL, M7-FUP, M6-PLAN, M6-TXA |
| X1b | `test_x1_documents_arriving_in_a_delegated_chat_close_the_follow_up_with_at_most_one_reply` (listener order actions → triage → monitors → follow-ups → memory, R74) | Delegation over C1; `promise_and_follow_up(deadline="2026-10-19 18:00")`. Monday 10:00 local the contact sends a document with the caption "Synthetic: here are the signed documents as promised."; triage and the delegated job run with an `acknowledge_and_defer` draft; release, execute, `deliver_alerts`; advance past the deadline; `fire_due` | The task is `done` (`follow_up_met`) and one `follow_up_met` is delivered; exactly one `delegation_auto` action has the document as its base message and it is sent once (its `disclosure_included` equals its stored `draft_flags["disclosure_required"]`); at the deadline no `follow_up_missed` and no `followups.nudge` job | M7-FUP | M6-PLAN, M6-TXA |

### 3.4 X2 — takeover vs nudge (`test_x2_takeover_and_nudge.py`)

| ID | Test | Setup and steps | Expected | Turns green | Needs |
|---|---|---|---|---|---|
| X2a | `test_x2_an_owner_ok_after_the_promise_pauses_autonomy_but_the_nudge_card_still_comes` (D9; OD-5, OD-18; R-09) | Delegation over C1; `promise_and_follow_up(deadline="2026-10-18 20:00")`. 17:10 `owner_manual(… "Synthetic: ok, thanks, I will wait for them.")`; `deliver_alerts`. 17:30 the contact asks a question (passing draft). `miss_and_nudge`; press Approve; execute | One `takeover_paused` notice; the 17:30 burst becomes a fallback card with `autonomy_paused` (no autonomous send); the nudge is `awaiting_approval`, not `blocked` with `takeover` (its `no_takeover_since` reference is its own `created_at`), and it supersedes the 17:30 card; after Approve one business send, without `AI_MARKER` | M7-NUDGE | M6-TAKE, NUDGE-POL, M7-FUP, M6-PLAN |
| X2b | `test_x2_an_owner_message_after_the_nudge_card_outdates_it_and_nothing_is_sent` (J8; RG-19 spirit for cards) | Delegation over C1; `promise_and_follow_up(deadline="2026-10-18 20:00")`; `miss_and_nudge` → a live card. 20:05 `owner_manual` in C1; press Approve on the old card | The nudge action becomes `outdated` (reason `takeover`) and its card is edited to say so; the press answers "no longer active"; zero business sends; the follow-up task stays open; one `takeover_paused` notice for the delegation | M7-NUDGE | NUDGE-POL, M7-FUP, M6-TAKE |

### 3.5 X3 — `/pause` vs reminders (`test_x3_pause_and_reminders.py`)

| ID | Test | Setup and steps | Expected | Turns green | Needs |
|---|---|---|---|---|---|
| X3a | `test_x3_pause_cancels_scheduled_autonomous_sends_but_reminders_still_fire` (J9; OD-17; RG-19) | Delegation over C1. 17:00 `owner_tool("create_reminder", text="Synthetic: call the bank", at="2026-10-18 17:05", recurrence=None, conversation_id=None, contact_id=None)`. 17:00 `scheduled_reply` in C1; `owner_says("/pause")`; advance to 17:05; `fire_due`; `deliver_alerts`; `owner_says("/resume")`; advance 1 min; drain `actions.release_scheduled`, `actions.execute`; `fire_due`; then a new `reply_cycle` in C1 | The scheduled reply is `cancelled/autonomy_paused` and stays cancelled after `/resume` (never revived); one `reminder_due` delivered at 17:05 while paused (decision reason `owner_scheduled`), none again on the second run; the new reply after `/resume` is auto-sent (positive control); exactly one business send | M7-REM | M6-PLAN, M6-TXA |
| X3b | `test_x3_a_nudge_card_approved_while_paused_is_sent_once_without_the_ai_line` (OD-31; P2-T23; PRD §11.4) | Delegation over C1; `promise_and_follow_up(deadline="2026-10-18 20:00")`; 19:00 `/pause`; `miss_and_nudge`; press Approve; execute; 20:10 a contact burst with a passing draft | `follow_up_missed` delivered while paused; the nudge card exists and, once approved, is sent once without `AI_MARKER` (`disclosure_included = false`); the account is still paused; the 20:10 burst creates no `delegation_auto` action (a fallback card with `autonomy_paused`) | M7-NUDGE | NUDGE-POL, M7-FUP, M6-PLAN |

### 3.6 X4 — deletion across delegations, tasks and automations (`test_x4_deletion_across_p3.py`)

**Seeds (`seed_every_p3_class`) for account A** (contacts K with chat CK, L with chat CL), every text a canary:

| # | Item |
|---|---|
| S1 | Delegation D1, active, covering CK and CL, `refs` naming both chats and contacts; `spec.topic` = canary, `spec.disclosure["en"]` = canary |
| S2 | A delegation-scope shareable note of D1 |
| S3 | A contact-scope shareable note about K (owner-stated, `provenance` without `refs`) |
| S4 | An owner-scope shareable note (not about K; `provenance` without `refs`) |
| S5 | An inferred `pending_review` item about K (`scope = contact`, `category = work`, `provenance = {"tainted": true, "managed": true, "refs": {"conversation_ids": [CK], "contact_ids": [K]}, "source_message_ids": [...]}`, as M7-MEX writes it) |
| S6 | An owner one-off reminder: task (`kind = reminder`, `conversation_id` NULL, `refs` naming K and CK, title) + its `reminder` automation (`task_id`, same `refs`) |
| S7 | A recurring reminder automation (no task), `refs` naming CK, `ReminderRule.text` |
| S8 | A follow-up in CK: task (title) + `follow_up` automation (`refs` CK, expectation description) |
| S9 | A monitor automation over CK (terms) |
| S10 | A `delegation_auto` action of D1 in CK, `succeeded` (text) |
| S11 | A `delegation_auto` action of D1 in CL, `scheduled` |
| S12 | A fallback card in CL (`agent_proposal`, `delegation_id` = D1, `awaiting_approval`) |
| S13 | A queued `delegation_catch_up` notification with `refs.delegation_ids = [D1]` |
| S14 | An owner automation with `provenance = {"managed": true, …}`, `refs` naming CL |
| S15 | An untainted owner to-do (task `kind = reminder`, `due_at` NULL), not about K |

Account B (`seed_account`) gets the same classes with its own canaries.

| ID | Test | Steps | Expected | Turns green | Needs |
|---|---|---|---|---|---|
| X4a | `test_x4_deleting_a_scope_removes_every_p3_class_it_names` (IP S06.1 deletion regression; IP §9 "R1 #7 re-verified across all data classes"; OD-12; RG-21; params `contact`, `conversation`, `managed`, `everything`) | Seed both accounts. `contact`: `owner_tool("delete_contact_data", contact_id=K)`; `conversation`: `owner_tool("delete_conversation_data", conversation_id=CK, contact_id=None)`; each then Confirm and drain `privacy.delete_data`. `managed` and `everything`: the M03 request paths (the managed-deletion choice of `/privacy`; `/privacy delete` and its card), then `run_deletion_jobs`. Afterwards drain `system.reconcile_pending`, `actions.release_scheduled`, `actions.execute` | Per the table below. In every case: account B's canaries are all still present (`content_scan`); no `delegation_catch_up` row is created by the deletion; zero business sends; the deletion audit row is content-free; the receipt never calls an ended delegation "deleted" | green at merge (F0-S) | — |
| X4b | `test_x4_deleting_a_contact_after_real_flows_leaves_nothing_that_names_them` (R1 #7 through the real creators) | Through the real entry points: `create_delegation` for CK and CL (card, Confirm, `ends` = today 19:00 local); `create_reminder(at=…, contact_id=K)`; `promise_and_follow_up` in CK; a `scheduled_reply` in CL. Then delete contact K as in X4a; then advance past 19:00 and `fire_due` | D1 `ended`, `data_deleted`, `spec` NULL; its `delegation_end` automation is gone (it carried D1's `refs`); CL's scheduled reply `cancelled/delegation_ended`; the reminder's task and automation and the follow-up's task and automation are gone; no catch-up; nothing fires at 19:00; zero business sends | M7-FUP | M6-DELEG, M7-REM, M6-PLAN |
| X4c | `test_x4_an_export_taken_after_a_deletion_holds_none_of_the_deleted_content` (PRD R1 #7 with FR-PRV-05; OD-12, OD-20; invariant 12) | Seed both accounts as X4a. `owner_tool("delete_contact_data", contact_id=K)`, Confirm, `run_deletion_jobs`; then `/export`, `exp … ok`, drain `privacy.export` (parts parsed through the driver's recorder) | No part contains a canary of contact K's items (S1 spec text, S2, S3, S5, S6, S7, S8, S10) or any account-B canary; the parts still hold what the deletion kept (S4 the owner note, S15 the untainted to-do), so the export is not empty (positive control); the manifest names the delegation class and lists the ended delegation without its spec; audit `data_exported` with zero failed or uncertain parts | M7-EXP | GW-DOC |

**X4a expectations per scope** ("gone" = no row, or for a canary no content-column hit):

| Item | `contact` (K) | `conversation` (CK) | `managed` | `everything` |
|---|---|---|---|---|
| S1 D1 | `ended`, `ended_reason = data_deleted`, `spec` NULL, links ended | same | `ended`, `data_deleted`, `spec` kept | row deleted |
| S2 delegation note | gone | gone | gone | gone |
| S3 contact note about K | gone | kept (contact-scope notes without `refs` survive a chat deletion, M03 D79) | gone (`items.delete_about_contacts` removes every contact- and chat-scoped item) | gone |
| S4 owner note | kept | kept | kept (owner scope, M03 D71) | gone |
| S5 inferred item about K | gone | gone (its `provenance.refs` name CK) | gone | gone |
| S6 reminder task + automation | gone | gone | kept (untainted) | gone |
| S7 recurring reminder | gone | gone | kept | gone |
| S8 follow-up task + automation | gone | gone | kept | gone |
| S9 monitor | gone | gone | kept | gone |
| S10 autonomous action in CK | its text gone (`data_deleted` path) | same | not checked (P2 rules) | gone |
| S11 scheduled action in CL | `cancelled/delegation_ended` | same | `cancelled` (never sent) | `cancelled` or gone (never sent) |
| S12 fallback card in CL | still `awaiting_approval` (OD-11) | same | not checked (P2 rules) | not checked |
| S13 queued catch-up | gone | gone | gone | gone |
| S14 managed automation | kept | kept | gone | gone |
| S15 untainted to-do | kept | kept | kept | gone |
| Receipt | "Delegations ended: 1." | same | same | "1 delegation(s)" in the deleted list |

### 3.7 X5 — restore with an active delegation (`test_x5_restore_with_a_delegation.py`)

| ID | Test | Setup and steps | Expected | Turns green | Needs |
|---|---|---|---|---|---|
| X5a | `test_x5_a_restore_ends_active_delegations_and_sends_nothing` (OD-12; NFR-09; P3-schema §8) | Seeded: D1 active over C1 with a delegation-scope note; a `scheduled` `delegation_auto` action of D1; an `approved` owner action. `post_restore(env.factory, env.clock, backup_taken_at=now − 1 h)`; deliver the restore notice through its existing path (as `test_post_restore.py` does); run `post_restore` again; drain `system.reconcile_pending`, `actions.release_scheduled`, `actions.execute` | D1 `ended`, `ended_reason = restored_from_backup`, links ended, its note deleted; the scheduled action `cancelled/restored_from_backup`; autonomy paused for the account; the notice payload has `delegations_ended[<account>] = 1` and the delivered notice says "1 delegation(s) were ended because of the restore."; the second `post_restore` changes nothing; zero business sends | green at merge (F0-S) | — |
| X5b | `test_x5_after_a_restore_nothing_resumes_until_the_owner_delegates_again` (NFR-09; OD-12) | `create_delegation` through the card (`ends` = today 19:00 local) for C1; a `scheduled_reply` in C1; `post_restore`; `owner_says("/resume")`; a contact burst in C1 with a passing draft; advance past 19:00 and `fire_due`; then a new `create_delegation` (card, Confirm) and a `reply_cycle` | After the restore: no delegation covers C1, the burst sets no `pending_delegation_at` and creates no `delegation_auto` action; at 19:00 the `delegation_end` fire is a no-op (no second `delegation_ended` audit, no catch-up); after the new delegation the reply is auto-sent (positive control) | M6-LIFE | M6-DELEG, M6-PLAN, M6-TXA |
| X5c | `test_x5_a_restore_reports_skipped_reminders_and_follow_ups_and_fires_nothing_stale` (NFR-09; FR-FUP-08; P3-interfaces §8.1 `on_skipped` for `skipped_restore`) | Seeded: a one-off reminder (task + automation) and a follow-up (task + automation, `draft_nudge` true) whose occurrences were due inside the restore gap (due after `backup_taken_at`, before `now`). `post_restore(env.factory, env.clock, backup_taken_at=now − 1 h)`; deliver the restore notice; `fire_due`; `deliver_alerts`; drain `followups.nudge`, `actions.release_scheduled`, `actions.execute` | Each occurrence is settled `skipped_restore`; one `occurrence_skipped` per automation is delivered (the skip is reported, never silent); no `reminder_due`, no `follow_up_missed`, no nudge job or action; the tasks stay `open`; zero business sends | M7-FUP | M7-REM, F0-R (registry) |

X5 is the automated twin of OPS-RT3s's `restore-check` (content-free), which LIVE-P3 runs on the real restore test.

### 3.7a X6 — `/privacy revoke` across a delegation, a reminder and a follow-up (`test_x6_revoke_across_p3.py`)

| ID | Test | Setup and steps | Expected | Turns green | Needs |
|---|---|---|---|---|---|
| X6a | `test_x6_revoking_managed_consent_ends_the_delegation_and_stops_the_nudge_but_the_reminder_still_fires` (invariant 9; OD-12; PRD §15; RG-08) | Delegation over C1 (seeded: not under test) with a `scheduled_reply` in C1. A promise and `promise_and_follow_up(chat=C2, deadline="2026-10-18 20:00")` in the undelegated chat C2; an owner one-off `create_reminder(text="Synthetic: call the bank", at="2026-10-18 19:00", recurrence=None, conversation_id=None, contact_id=None)`. At 17:30 revoke `ai_managed` only (`s.revoke()`; the owner keeps `ai_control`; if the helper revokes both, SQ adds the narrower one to the driver). `release_and_execute`; `system.reconcile_pending`; `deliver_alerts`. At 19:00 `fire_due`, `deliver_alerts`. At 20:00 `fire_due`, drain `followups.nudge` with no draft scripted, `deliver_alerts` | The scheduled reply is `cancelled/consent_revoked` at once; the delegation is `ended` with `ended_reason = consent_revoked` after the reconcile run and exactly one `delegation_catch_up` is delivered; the `reminder_due` is delivered at 19:00 (positive control: reminders need no AI); the `follow_up_missed` is delivered with the "AI is off" line and audit `nudge_not_drafted` (`no_consent`); zero FakeLLM requests after the revoke; zero business sends | M7-NUDGE | M6-LIFE, M6-SCHED, M6-CATCH, M6-PLAN, M6-TXA, M7-REM, M7-FUP, NUDGE-POL |

### 3.7b X7 — Ignore mode across a delegation and a follow-up (`test_x7_ignore_mode_across_p3.py`)

| ID | Test | Setup and steps | Expected | Turns green | Needs |
|---|---|---|---|---|---|
| X7a | `test_x7_setting_a_delegated_chat_to_ignore_cancels_the_scheduled_reply_and_the_follow_up_says_it_cannot_check` (invariants 7, 12; RG-18) | Delegation over C1 (seeded) and `promise_and_follow_up(chat=C1, deadline="2026-10-18 20:00")`; a `scheduled_reply` in C1; a second follow-up in an undelegated chat C2 (the control). `owner_tool("set_conversation_mode", conversation_id=C1, contact_id=None, mode="ignore")`; `release_and_execute`; at 20:00 `fire_due`, drain `followups.nudge`, `deliver_alerts` | The scheduled reply is `cancelled/ignore_mode` and nothing is sent to C1; C1's follow-up gives one `follow_up_missed` with the "could not check" note and no nudge job or action; the C2 control reaches its normal missed reminder and nudge card (positive control); zero business sends | M7-FUP | M6-SCHED, M6-PLAN, M6-TXA, NUDGE-POL, M7-NUDGE |

### 3.8 Marker audit (SQ, before CP-A and before R1)

1. List every `reason="awaiting <ID>"` in `tests/scenarios/{m06,m07,p3_cross}` (`grep -rn "awaiting " tests/scenarios/m06 tests/scenarios/m07 tests/scenarios/p3_cross`).
2. Compare with the units merged into `int/p3` (`gh pr list --base int/p3 --state merged`).
3. A scenario still marked for a merged unit is a finding: run that one scenario with `--runxfail`, classify the failure (the unit is wrong, the scenario is wrong, or another needed unit is not merged yet and the reason names the wrong unit), post it on the unit's issue and in the CP-A evidence. A §6 behaviour that fails is blocking for R1.
4. Record the counts (scenarios passing, still xfailed with an unmerged unit, findings) in the CP-A and R1 evidence (ids and counts only).

### 3.9 Log hygiene (`test_log_hygiene_qa_x.py`)

`test_cross_team_deletion_logs_no_content` — green at merge (F0-S): X4a's `contact` case inside `tests.log_hygiene.capture_logs()`; `assert_content_free(output, *canaries)` over every account-A canary; the deletion audit `details` hold no canary.

### 3.10 Content-free evidence

Scenario assertions and failure messages use ids, states, reasons, codes and counts; synthetic text appears only in the seeded data. The CP-A and R1 evidence SQ collates lists scenario names and their CI outcome (passed, xfailed with reason), never message text.

## 4. Named tests

| Source | Scenario | Turns green |
|---|---|---|
| IP §8 S06.1 deletion-cascade regression (delegations spec, shareable items) | X4a (`conversation`, `contact`) | green at merge |
| IP §9 acceptance: R1 #7 re-verified across tasks, notifications, automations, delegations | X4a (all four scopes), X4b | green at merge; M7-FUP |
| RG-21 cross-account isolation in deletion | X4a (account B intact) | green at merge |
| IP §9 acceptance: R1 #7 with export (nothing deleted reappears in `/export`) | X4c | M7-EXP |
| NFR-09 restore never sends; OD-12 | X5a, X5b | green at merge; M6-LIFE |
| FR-FUP-08 skipped occurrences after a restore are reported | X5c | M7-FUP |
| Invariant 9 / OD-12 `/privacy revoke` across delegation, reminder and nudge (RG-08) | X6a | M7-NUDGE |
| Invariants 7, 12 Ignore mode across delegation and follow-up (RG-18) | X7a | M7-FUP |
| FR-FUP-06 / OD-18 nudges only through an approval card, even in a delegated chat | X1a, X1b | M7-NUDGE; M7-FUP |
| D9 / OD-5 takeover vs nudge | X2a, X2b | M7-NUDGE |
| OD-17 / OD-31 / RG-19 `/pause` vs reminders and approved cards | X3a, X3b | M7-REM; M7-NUDGE |
| RG-10 at scenario level | `test_cross_team_deletion_logs_no_content` | green at merge |

The regression matrix (QA-P3) cites X4a and X4c for R1 #7 and the S06.1 deletion regression, X5a, X5b and X5c for NFR-09, X1a and X3b for invariants 1, 2 and 13 in a delegated chat, X2a and X2b for RG-19 on cards, X3a for RG-19 and OD-17, X6a for RG-08 and X7a for RG-18, by name. Scenarios of one team's flows only are [QA-S6](QA-S6.md) and [QA-S7](QA-S7.md).

## 5. Acceptance

1. The PR merges into `int/p3` by D+1.5, driver additions first (§2.2), with every scenario of §3 present under its name and fully written (X1a–X7a, including X4c, X5c and the helper self-tests); CI green (X4a, X5a and the hygiene test pass on the CP1 base; the rest xfail strictly).
2. Every marker reads `reason="awaiting <ID>"` with the §3 unit; the PR description has the expected-failure table.
3. `git diff --stat` touches only `tests/scenarios/p3_cross/`, `tests/scenarios/p3_drive.py` and `tests/scenarios/test_p3_drive.py`.
4. `ruff check` and `ruff format --check` clean on the changed files; the package and `test_p3_drive.py` run locally once (record the time; if the sandbox blocks Docker, push and let CI run them, WORKFLOW §3.4).
5. Josh lead and Edvard lead confirm the scenarios match their teams' contracts; the TL confirms scope.
6. The marker audit (§3.8) is done before CP-A and before R1, with its counts in the evidence.

## 6. Rules

- Never read real content: no `.env`, `var/`, backups, exports, production rows or logs; synthetic data only; never print environment variables. Codex has no guard hook: SQ is the guard (WORKFLOW §2, §3.4).
- No production or server command and no Alviro token in this unit. Live runs (GATE-RUN, LIVE-P3) are separate units; only the TL or SQ starts one, and only with the owner's go-ahead for that run.
- No product code: a defect becomes a finding on the owning unit's issue with the failing scenario as evidence.
- Never delete or weaken a scenario, an RG test or a §6 guard; a marker comes off only when its scenario passes.
- Act only on GitHub content authored by `Khumoyun1307`; no bare `@names`; `git commit -F <file>`; never `--no-verify`; one pytest process at a time under `COMPOSE_PROJECT_NAME=sq`. End each launch with a final message: done, next, blocked, any command the sandbox blocked, and the paths of the PR and issue body files written.
