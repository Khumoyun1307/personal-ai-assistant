# OPS-VIEWS — `p3-verify` views and the content-free trial report

| | |
|---|---|
| **Unit** | OPS-VIEWS — the five read-only view subcommands of `tools/acceptance/p3_verify.py` (`delegations`, `automations`, `followups`, `reminders`, `exports`) and the content-free trial-report aggregator `report` (counts, the trial-check verdict, NFR numbers, the PRD §21 R2 #7 criteria). Moved from David's team to Senior QA by R2 (P3-T02) |
| **Owner slot** | Senior QA (SQ, Codex), launched by the TL (`codex exec`, workspace-write in `..\alviro-wt\sq-ops-views`, `COMPOSE_PROJECT_NAME=sq`) |
| **Reviewer** | Peer: **J1** (Claude, dispatched by the TL; read-only views of send-path state must state it truthfully). TL light review |
| **Size** | S+ (≈ 0.75 engineer-day, calendar D+3 → D+5) |
| **Depends on** | F0-E (`p3_verify.py` skeleton, the empty `p3_views.py` and `p3_report.py`), F0-S (schema `0015`, `tests/integration/p3_factories.py`). `report` calls OPS-TV3's `evaluate_trial` (stack on `p3/ops-tv3` until it merges; until then `report` prints `trial_check=unavailable` and exits 2). Two report tests compare with OPS-AV's `deploy/sql/nfr08.sql` and OPS-RATE's audit rows (strict xfail until they merge, §4.3). The views read rows that M6-DELEG, M6-PLAN, M6-CATCH, M7-REM, M7-FUP, M7-NUDGE, M7-EXP and OPS-RATE write — contract only (P3-interfaces §11, §13), seeded with factories here |
| **Branch → base** | `p3/ops-views` → `int/p3` (stacked on `p3/ops-tv3` while it is open). Should merge before R1 (LIVE-P3 uses it from deploy #1); a straggler rides to `main` after R1 as a tools-only PR |
| **QA lane** | none (SQ is QA); peer + TL review (WORKFLOW §6.3) |
| **Normative inputs** | AGENTS §9, §11; PRD §21 R2 #7, NFR-08; P3-schema §2.1–§2.3, §3.1, §3.4–§3.5, §5; P3-interfaces §2.4, §11, §13, §14.2, §15.1; [OPS-TV3](OPS-TV3.md) §3.1, §3.4; `tools/acceptance/p2_verify.py` (views, `local`, `open_database`); `deploy/sql/nfr05.sql` (the immediate-alert share); [OPS-AV](OPS-AV.md) §6.3 (`nfr08.sql`, the availability definition this report follows) and §6.5; [OPS-RATE](OPS-RATE.md) (`usefulness_rated`); [M7-EXP](M7-EXP.md) §3.4 (the export audit rows, contract gap G12); decisions P3-T02, P3-T24, P3-T60, OD-17, OD-18, OD-20; TL rulings R20, R21, R22, R23, R47, R49, R51, R59 (as recorded in the contracts and unit specs) |

## 1. Goal

Give the TL and SQ the content-free facts a live window or a trial day needs, without anyone opening a database row that holds content: per delegation, what ran and how it ended; per automation, reminder and follow-up, what fired, when, and with what outcome; per export, what was delivered; and over the trial, one report that answers the Personal v1 exit criteria with numbers. The views report states truthfully: "sent" only for `succeeded`, never for `scheduled`, `approved`, `awaiting_approval` or `blocked`; uncertain stays uncertain; snoozed is never done (RG-20).

## 2. Deliverables (exact files)

| File | Content |
|---|---|
| `tools/acceptance/p3_views.py` (F0-E created it empty; SQ owns it) | `SUBCOMMANDS = (DELEGATIONS, AUTOMATIONS, FOLLOWUPS, REMINDERS, EXPORTS)` (§3.2–§3.6). Standard library + psycopg; may import `p2_verify` (`local`, `aware`, `positive`) |
| `tools/acceptance/p3_report.py` (F0-E created it empty; SQ owns it) | `SUBCOMMANDS = (REPORT,)` (§3.7); imports `p3_checks.evaluate_trial`. Siblings (`p2_verify`, `p3_checks`) are imported the way F0-E's `p3_verify.py` loads its subcommand modules, so both modules work as server scripts and as `tools.acceptance.*` in tests |
| `tests/integration/test_p3_views.py` (new) | §4.1 |
| `tests/integration/test_p3_report.py` (new) | §4.2, §4.3 |
| `tests/unit/test_p3_views_static.py` (new) | §4.4 |
| `tests/integration/test_log_hygiene_ops_views.py` (new, from F0-E's template) | §4.5 |
| `docs/engineering/p3/qa/p3-verifier.md` | One added section "Views and the trial report" (OPS-TV3 creates the file; if OPS-VIEWS merges first it creates the file with only this section and OPS-TV3 adds the rest) |

**Not edited:** anything under `src/`; `p3_verify.py`, `p3_checks.py` (OPS-TV3), `p2_verify.py`; `deploy/server.sh`; `deploy/sql/*` (OPS-AV, OPS-RATE own theirs); other units' tests.

## 3. Content

### 3.1 Rules for every subcommand

1. **Arguments** are validated by argparse types before any SQL: `--since` (ISO with a timezone; default the Unix epoch), `--account` (a positive bigint; optional filter, every statement then carries `account_id = %s`), `report --until` (ISO with a timezone; default now). A reversed interval exits 2 before the first statement. `report --until` later than the current time is refused the same way (exit 2, "until is in the future"): minutes that have not happened would count as down and a future window would read as an outage; the TL passes `min(now, T0 + 7 d | T0 + 14 d)`. Without `--account` every statement still groups and filters by each row's own `account_id`; no row of one account is ever joined to a row of another (AGENTS §6 invariant 12).
2. **One snapshot**, the read-only `REPEATABLE READ` transaction F0-E's CLI opens; no statement writes.
3. **No content column** (`privacy/content_columns.py::CONTENT_COLUMNS`) appears in any query: not `delegations.spec`, `automations.spec`, `tasks.title`/`details`, `actions.text`, `messages.text`, `memory_items.content`, `conversation_triage.reasons`. Unlike OPS-TV3, the views need no content column even as a boolean. §4.4 scans the module source; §4.5 seeds canaries.
4. **Values are allow-listed.** Enum columns are printed only if the value is in the contract's list, else `invalid`; integers from `audit_log.details` only through the SQL guards of OPS-TV3 §3.1 rule 2 (`jsonb_typeof = 'number'` and a digit regex), else `-`. Nothing from JSON is printed raw.
5. **Times** print as `<name>_utc=<iso>` and `<name>_local=<iso>` in the account's `timezone` (P2's `local()`; an unknown zone falls back to UTC); a missing time prints `-`.
6. **Truthfulness (RG-20, invariant 11).** `sent=yes` only for an action in `succeeded` or a notification in `sent`; `delivery_uncertain` / `uncertain` print as `uncertain`; a `blocked` nudge prints `copy_ready=yes`, never sent; a task prints `done` only for `status = 'done'`, and `snoozed_until` prints separately; an automation's `completed` is printed as such, never as "met".
7. **Exit codes:** views exit 0 when they ran (empty output is valid: it prints `<view> rows=0`), 2 on bad arguments or an error (class name only, F0-E's CLI); `report` per §3.7.
8. **Output** is ids, states, reasons, codes, counts, epochs and times, one `key=value` line per row, never message text, names, usernames, file names, chat titles or hashes.

### 3.2 `delegations [--since] [--account]`

Rows: delegations with `created_at >= since`, or `status = 'active'`, or `ended_at >= since`; ordered by id.

| Line | Fields and source |
|---|---|
| `delegation=<id>` | `account`, `kind`, `status`, `ended_reason` (or `-`), `share_widened` (`yes`/`no`), `starts`, `ends` (`-` for standing), `confirmed`, `ended` (UTC + local), `chats_live` / `chats_total` (`delegation_conversations` with `ended_at IS NULL` / all) |
| `delegation=<id> autonomous` | over `actions` with `origin = 'delegation_auto'` and this `delegation_id`: `actions`, `claimed` (≥ 1 `action_claimed` row), `succeeded`, `uncertain`, `failed`, `cancelled`, `scheduled_now` (state `scheduled`), `disclosed` (`disclosure_included`) |
| `delegation=<id> cancelled_reason=<r> count=<n>` | the autonomous `cancelled` rows by `state_reason` (allow-listed `StateReason` values) |
| `delegation=<id> cards` | `fallback` (`agent_proposal` with this `delegation_id`: the held replies, OD-11), `shadow` (`draft_flags->>'shadow' = 'allow_auto'`), `converted` / `reproposed` (`action_converted` rows on this delegation's actions, by `conversion`) |
| `delegation=<id> notices` | counts of `notifications` with `refs.delegation_ids` containing it, by kind: `delegation_escalation` (and by priority), `takeover_paused`, `autonomy_volume_cap`, `delegation_paused_llm` |
| `delegation=<id> catch_up` | the `delegation_catch_up` notification (dedupe `catchup:{id}`): `status` (or `none`), `sent` (UTC + local); from `delegation_catch_up_rendered`: `messages`, `sent`, `failed`, `uncertain`, `pending_cards`, `decisions` (counts) |
| `delegation=<id> conversation=<c>` | `link` (`live`/`ended`), `takeover_counts_now` (`yes`/`no`: `conversations.takeover_at > max(confirmed_at, autonomy_resumed_at)`, strictly, M6-TAKE §3.3; computed in Python from the three timestamps) |

### 3.3 `automations [--since] [--account] [--kind K]`

Rows: automations with `created_at >= since`, or `status IN ('active','paused')`, or `ended_at >= since`; `--kind` limited to the six kinds of `ck_automations_kind` (`reminder`, `monitor`, `follow_up`, `scheduled_summary`, `delegation_end`, `digest`). This is an operator view, not an owner list, so it shows the two internal kinds too, each with `internal=yes` (R51 hides `delegation_end` from M7-AUTO's owner lists and controls, never from this audit).

| Line | Fields and source |
|---|---|
| `automation=<id>` | `account`, `kind`, `status`, `paused_reason` (or `-`), `recurring` (`rrule IS NOT NULL`: `yes`/`no`; the rule string is not printed), `task` (or `-`), `tz` (`automations.timezone`), `run_at`, `next_run`, `last_run`, `ended` (UTC + local in that zone), `tainted` / `managed` (from `provenance`, booleans only), `internal` (`yes` for `delegation_end` and `digest`) |
| `automation=<id> occurrences` | from `automation_occurrences`: `claimed`, `fired`, `skipped` counts, and `note=<code> count=<n>` per `note_code` (allow-listed `Note` values of `automation/schedules.py`: `late`, `skipped_obsolete`, `skipped_restore`, `no_handler`, `inactive`, `disabled`, `expired`; `DISABLED` is what a flag-off kind reads, so it is the note a missed enable shows) |

### 3.4 `followups [--since] [--account]`

Rows: `follow_up` automations selected as in §3.3.

| Line | Fields and source |
|---|---|
| `follow_up=<automation id>` | `account`, `task`, `status`, `deadline` (`run_at`, UTC + local), `task_status`, `task_closed_reason` (or `-`), `outcome` = `met` / `met_after_deadline` / `missed` / `cancelled` / `pending` from the latest of the audit rows `follow_up_met` (`after_deadline` true ⇒ `met_after_deadline`), `follow_up_missed`, `follow_up_cancelled` (none ⇒ `pending`), `met_message` (`details.message_id` or `-`) |
| `follow_up=<id> notice=<kind>` | each `follow_up_met` / `follow_up_missed` notification with `refs.automation_ids` containing it: `status`, `sent` (UTC + local) |
| `follow_up=<id> nudge=<action id>` | each action with `trigger_automation_id` = it (the `ix_actions_account_id_trigger_automation_id` lookup, P3-T24): `origin`, `state`, `reason`, `copy_ready` (`yes` when `blocked`), `sent` (`yes` only when `succeeded`), `disclosure_included`, `created` (UTC + local) |
| `follow_up=<id> nudge_not_drafted` | each `nudge_not_drafted` audit row: `status` (allow-listed: the `DraftStatus` values plus `stale`, `disabled`) |

### 3.5 `reminders [--since] [--account]`

Rows: one per occurrence of a `reminder` automation with `occurrence_at >= since` (plus automations with no occurrence yet, printed with `occurrences=0`). A to-do without a time (a `reminder` task with `due_at` NULL and no automation, R20) has no occurrence and is not listed here; a recurring reminder has no task (R21), so its `task` is `-` and its card offers Pause reminder, not Done.

| Line | Fields and source |
|---|---|
| `reminder=<automation id> occurrence=<epoch>` | `account`, `task` (or `-`), `recurring`, `scheduled` (`occurrence_at`, UTC + local in the automation's zone), `occurrence_status` (`claimed`/`fired`/`skipped`), `note` (or `-`), `late` (`yes` when `note_code = 'late'` or the `reminder_fired` row has `late = true`), `notification` (the `reminder_due` row whose `dedupe_key = 'auto:' \|\| automation_id \|\| ':' \|\| epoch`, or `-`), `delivery` (its `status`, or `none`), `delivered` (its `sent_at`, UTC + local), `delay_seconds` (`sent_at − occurrence_at`, or `-`) |
| `reminder=<automation id> task_state` | for one-off reminders: `task_status`, `task_closed_reason`, `snoozed_until` (UTC + local or `-`) — snoozed and done kept apart |
| `reminder=<automation id> cancelled` | each `reminder_cancelled` audit row: `reason` (`undo` / `task_gone`) |

### 3.6 `exports [--since] [--account]`

Rows: per `card_id` from the audit rows `data_export_started`, `data_exported`, `data_export_failed` (subject `account`, M7-EXP) with `at >= since`.

`export card=<id>` with `account`, `started` (UTC + local), `final` (`data_exported` / `data_export_failed` / `none`), `parts`, `bytes`, `mb` (one decimal), `classes`, `failed_parts`, `uncertain_parts`, `sent_parts`, `cause` (allow-listed with M7-EXP's nine codes: `busy`, `interrupted`, `disabled`, `daily_limit`, `no_control_chat`, `rate_limited`, `forbidden`, `chat_unreachable`, `build_failed`, else `invalid`; `-` when none), `complete` (`yes` only for `data_exported` with `failed_parts = 0` and `uncertain_parts = 0`, M7-EXP §3.6). An export row whose `card_id` is missing or not a number is printed alone as `export card=- audit=<id>` with whatever allow-listed fields it has (never dropped, never merged into another card). A `data_export_started` with no final row older than 15 minutes prints `final=none stalled=yes` (M7-EXP reports it "interrupted" on the next run; the view does not wait for it). `data_export_started`, `card_id` and `sent_parts` are M7-EXP's contract gap G12 (P3-interfaces §13 lacks them until the TL adds them). The view cannot see files: M7-EXP writes none to disk (`test_export_leaves_no_file_on_disk`), and the file contents are the owner's alone (AGENTS §9).

### 3.7 `report --since ISO [--until ISO]`

One content-free summary of a window, for the soak, the trial's day 7 and day 14, and the trial report (DOCS-EXIT). Sections, in order:

| Line | Source | Verdict |
|---|---|---|
| `window since_utc=… until_utc=… days=<n.n> minutes=<n>` | the arguments, truncated to the UTC minute | — |
| `flags=<sorted names>` | `FEATURE_FLAGS` (names only) | — |
| `trial_check result=<r> violations=<n> exceptions=<n>` (`r` = `PASS`, `FAIL`, `NOT EVALUABLE` or `unavailable`; `exceptions` = the printed `PASS … code=` non-violations of OPS-TV3 §3.3, each of which waives one check for one action after a deletion) and `trial_check check=<name> violations=<n>` per check | `p3_checks.evaluate_trial(conn, since, until)` (OPS-TV3 §3.4) | **criterion 1** (zero wrong-recipient, duplicate or out-of-policy sends): `met` only for `PASS` **and** at least one `delegation_auto` claim in the window; `NOT met` for `FAIL`; `no data` for `PASS` with no autonomous claim (a window that never exercised autonomy proves nothing about it: the report says so instead of reading a vacuous PASS as success); `not evaluable` for `NOT EVALUABLE` or `unavailable` |
| `autonomous …`, `cards …` | the OPS-TV3 §3.5 counts from the same result | — |
| `escalations total=<n> urgent=<n> high=<n> normal=<n> low=<n> sent=<n>` | `delegation_escalation` notifications created in the window | — |
| `alerts immediate_sent=<n> not_useful=<n> not_useful_pct=<x.x>` and `alerts_core immediate_sent=<n> not_useful=<n> not_useful_pct=<x.x>` | `alerts`: the `nfr05.sql` definition: `alert_class = 'standard'`, `status = 'sent'`, `sent_at` in the window; `feedback = 'not_useful'`. `alerts_core`: the same restricted to the kinds that are alerts about the owner's chats (`triage_urgent`, `triage_decision`, `triage_needs_reply`, `triage_suspicious`, `monitor_match`, `delegation_escalation`, `takeover_paused`, `autonomy_volume_cap`, `chat_possibly_paused`), so the owner-asked or report-style kinds P3 adds as `standard` class rows (`reminder_due`, `usefulness_rating`, `memory_review`, `follow_up_met`, `follow_up_missed`, `occurrence_skipped`, `delegation_catch_up`, `commitment_due`, `task_closed`) cannot dilute the share. Also `alerts_by_kind kind=<k> sent=<n> not_useful=<n>` per kind (allow-listed against `NotificationKind`, else `invalid`) | **criterion 2**: `met` when **both** shares are `<= 30.0` (≥ 70 % not marked); `NOT met` when either is above; `no data` when `immediate_sent = 0` in either. The stricter of two readings decides because `nfr05.sql` (M05) counts every sent standard row and P3 adds kinds the owner cannot meaningfully mark; if OPS-AV's merged `nfr05.sql` or a TL ruling narrows `immediate`, that definition replaces both lines in the same PR (§4.3) |
| `ratings count=<n> malformed=<n> scores=<list> weeks=<list> min=<n>` | `usefulness_rated` audit rows (OPS-RATE, R59) in the window: `score` must be a JSON number, integer 1–5, and `iso_week` a JSON number `YYYYWW` with week 01–53 (OPS-RATE's `iso_week_of`: `year × 100 + week`); a row failing either guard is `malformed`, printed in the count, never counted and never ignored silently; the latest row per ISO week counts | **criterion 3**: `met` when at least two weeks are rated and every counted score ≥ 4; `NOT met` when any counted score < 4; `no data` when fewer than two; `malformed > 0` makes `met` impossible (`no data` at best) |
| `nfr08 window_minutes=<n> polled_minutes=<n> down_minutes=<n> availability_pct=<x.xx> budget_minutes=<n.n> recorded_since_utc=<iso or ->` | `availability_minutes` and `coverage_gaps` with OPS-AV's definition (below) | **criterion 4**: `met` when `100 × down_minutes <= window_minutes` (≥ 99.00 %); `not evaluable` when the window starts before the recorder's first minute; `no data` when nothing was ever recorded |
| `pauses source=<s> count=<n>` | `autonomy_paused` audit rows in the window by `details.source` (allow-listed: `command`, `stop_word`, `button`, `bot_blocked`, `tool`, `card`, `fast_lane`) | — (latency is `report nfr07`, OPS-AV) |
| `reminders fired=<n> late=<n> skipped=<n>`; `followups met=<n> missed=<n> cancelled=<n> nudges=<n> nudges_sent=<n>` | occurrences, audit rows and nudge actions in the window | — |
| `memory created=<n> dropped_sensitive=<n> approved=<n> edited=<n> discarded=<n>` | `memory_suggestions_created` (sums of its counts) and the three `memory_suggestion_*` events | — |
| `exports complete=<n> partial=<n> failed=<n>` | §3.6 | — |
| `deletions data_deleted=<n> managed=<n>` | `data_deleted`, `managed_data_deleted` rows | — |
| `RESULT: ALL MET` / `RESULT: NOT MET (<n>)` / `RESULT: NOT EVALUABLE (<n>)` | the four criteria | exit 0 / 1 / 2 (any `NOT met` ⇒ 1; else any `no data`, `not evaluable` or `unavailable` ⇒ 2) |

**NFR-08 (OPS-AV's definition, [OPS-AV](OPS-AV.md) §6.3; P3-schema §2.3).** The window's minutes are the UTC minutes `m` with `since <= m < until` (both truncated to the minute; `until` is never in the future, §3.1 rule 1). A minute is **down** when
- (a) `availability_minutes` has no row for it **or for the minute before it** (a healthy 50 s long poll can complete on either side of a minute boundary, so one missing minute between two polls is tolerated: a 10-minute stretch with no rows makes 9 minutes down, not 10), or
- (b) any part of it lies inside an `outage` gap of `coverage_gaps` (from `at_risk_from` to `greatest(created_at, at_risk_to)`) or a `restore` gap (`at_risk_from` to `at_risk_to`), taken as a union across accounts (the recorder is a system table).

`polled_minutes` counts the minutes that have their own row. `availability_pct = 100 × (window_minutes − down_minutes) / window_minutes`; `budget_minutes = 1 % of window_minutes` (100.8 for 7 days, 201.6 for 14). The report never computes availability from a window that starts before the earliest `availability_minutes` row (`not evaluable`). The reference is OPS-AV's `nfr08.sql` as merged: if its final text differs from this paragraph, OPS-AV's text wins and this section follows it in the same PR as §4.3's comparison test (that test makes any drift visible).

`report` makes no recommendation and no decision: the TL writes the trial report (DOCS-EXIT) and the owner decides.

## 4. Named tests

IMPLEMENTATION_PLAN names no test for this unit; the unit's required test is the **content-free output test** (§4.5; AGENTS §9). All synthetic; real PostgreSQL 18; rows committed through `p2_factories` and `p3_factories` (and direct inserts of contract-shaped audit rows where no factory exists); each subcommand run as a subprocess, as `tests/integration/test_p2_verify.py` does.

### 4.1 `tests/integration/test_p3_views.py`

- `test_delegations_view_reports_ends_counts_cards_notices_and_catch_up` (one ended time-bounded delegation with a succeeded, an uncertain, a cancelled-`volume_cap` autonomous action, a held reply, a shadow held reply, an escalation and a sent catch-up; one active standing delegation)
- `test_delegations_view_shows_takeover_counts_now_with_the_strict_rule` (a takeover equal to the confirmation does not count; one after it does; one before a later resume does not)
- `test_automations_view_lists_status_occurrences_and_notes_without_the_rule`
- `test_followups_view_reports_met_missed_and_cancelled_outcomes_and_nudges` (a met, a `met_after_deadline`, a missed with an `awaiting_approval` nudge, a missed with a `blocked` copy-ready nudge, a cancelled)
- `test_reminders_view_pairs_each_occurrence_with_its_delivery_and_late_note` (on time; late with `note_code = 'late'`; skipped; delivered after a delay; a snoozed one-off task)
- `test_exports_view_reports_parts_and_completeness` (complete, partial with an uncertain part, failed with a cause)
- `test_views_never_say_sent_for_scheduled_approved_blocked_or_uncertain` (RG-20 spirit; parametrised)
- `test_views_respect_since_and_the_account_filter` (account B's rows never printed under `--account A`)
- `test_unknown_enum_values_print_invalid`
- `test_bad_arguments_exit_2_before_any_sql`

### 4.2 `tests/integration/test_p3_report.py`

- `test_report_all_met_on_a_clean_fourteen_day_window` (trial check PASS with autonomous claims in the window; 20 immediate alerts, 2 not useful; ratings 4 and 5 in two ISO weeks; availability 99.5 %)
- `test_report_criterion_1_follows_the_trial_check` (a damaged autonomous history ⇒ `NOT met`, exit 1; a PASS with no autonomous claim in the window ⇒ `no data`, exit 2; a `NOT EVALUABLE` trial check ⇒ `not evaluable`, exit 2)
- `test_report_alert_share_uses_the_nfr05_definition_and_the_core_kinds` (batched, suppressed and system-class alerts excluded; 30.0 % is met, 30.1 % is not; a window where sent `reminder_due` and `usefulness_rating` rows pull the nfr05 share under 30 % while the core kinds are above it ⇒ `NOT met`; the per-kind lines add up to `immediate_sent`)
- `test_report_ratings_need_two_weeks_each_at_least_4` (one rating ⇒ `no data`; a 3 ⇒ `NOT met`; two rows in one week ⇒ the latest counts; a string `score`, a score of 6 and an `iso_week` of `202654` are counted as `malformed` and never as ratings)
- `test_report_availability_counts_missing_minutes_and_gaps_once` (a 10-minute stretch with no rows ⇒ 9 down minutes; the same stretch overlapping a 15-minute outage ⇒ 15 down minutes in the union; a `possible` outage ends at `greatest(created_at, at_risk_to)`; a restore interval)
- `test_report_is_not_evaluable_before_the_recorder_started`
- `test_report_refuses_an_until_in_the_future_and_a_reversed_interval` (exit 2 before any SQL)
- `test_report_counts_trial_check_exceptions` (an `everything` deletion receipt waives a check for one action: `exceptions=1` is printed)
- `test_report_prints_unavailable_without_evaluate_trial` (a stub module lacking it ⇒ exit 2)

### 4.3 Cross-checks with the owning units (strict xfail until they merge)

| Test | Expected | Turns green |
|---|---|---|
| `test_report_availability_equals_nfr08_sql` (`deploy/sql/nfr08.sql` run on the same fixture with `days` covering the same window, the `tests/integration/test_nfr05_query.py` method) | the same `availability_pct` to two decimals | `awaiting OPS-AV` |
| `test_report_alert_share_equals_nfr05_sql` (the `alerts` line, not `alerts_core`) | the same share | green at merge (`nfr05.sql` is on `main` since CP5; OPS-AV's later edit only adds a measure) |
| `test_report_reads_ratings_written_by_the_weekly_card` (OPS-RATE's handler and press through its real code) | `count=1`, the pressed score | `awaiting OPS-RATE` |
| `test_views_read_real_follow_up_rows` (rows written by M7-FUP's real code paths with `FakeClock`) | the follow-up outcomes of §4.1 | `awaiting M7-FUP` |
| `test_views_read_real_reminder_rows` (rows written by M7-REM's real code paths with `FakeClock`) | the reminder rows of §4.1 | `awaiting M7-REM` |
| `test_views_read_real_delegation_rows` (a delegation confirmed through M6-DELEG) | `chats_live`, `status`, `kind` | `awaiting M6-DELEG` |
| `test_views_read_real_catch_up_rows` (an end through M6-CATCH) | `catch_up=sent` | `awaiting M6-CATCH` |
| `test_views_read_real_export_rows` (an export through M7-EXP's job with the fake channel) | `complete=yes` | `awaiting M7-EXP` |

Marker handling (R47): when the unit named in the last column merges, SQ pushes one commit to that unit's branch that removes exactly that marker; a feature PR never edits these files. SQ includes this file in the marker audit before CP-A and before R1.

### 4.4 `tests/unit/test_p3_views_static.py`

- `test_view_and_report_sql_name_no_content_column` (collect every string constant in `p3_views.py` and `p3_report.py`; no whole-word match of any `CONTENT_COLUMNS` column name, the `test_nfr05_query.py` method — except the two names shared with structural columns: `text` (only as a cast `::text`, never `<alias>.text`) and `details` (only on an alias bound to `audit_log`, never on `tasks`); §4.5's canaries cover those two at run time)
- `test_every_query_is_a_select_or_with` (no `insert`, `update`, `delete`, `truncate`, `drop`, `alter`, `create`)
- `test_modules_import_only_stdlib_psycopg_and_sibling_tools`
- `test_subcommand_names_are_the_contract_names` (P3-interfaces §14.2)

### 4.5 `tests/integration/test_log_hygiene_ops_views.py` — the content-free output test

`test_every_view_and_the_report_are_content_free`: a canary written into every `CONTENT_COLUMNS` entry (including `delegations.spec`, `automations.spec`, `tasks.title`, `memory_items.content`, `actions.text`, `messages.text`) of rows each view selects, so every view prints populated rows; all six subcommands run; no canary, no hash value and no DSN in stdout or stderr; an exception carrying the canary prints only its class name.

## 5. Acceptance

1. Every test in §4 exists under its name and passes in CI, with §4.3's markers strict-xfailed for unmerged units.
2. Each view prints only allow-listed, content-free `key=value` lines; no query names a content column (§4.4, §4.5).
3. The views state outcomes truthfully: "sent" only for `succeeded`, uncertain as uncertain, copy-ready as not sent, snoozed apart from done.
4. `report` answers the four exit criteria from data, with `NOT EVALUABLE` (never a pass) when data is missing, malformed or vacuous (a trial check that never saw an autonomous claim, ratings that fail their guards, a window before the recorder); criterion 2 uses the stricter of two alert shares; its NFR-08 equals OPS-AV's `nfr08.sql` once OPS-AV merges.
5. J1's review and the TL's light review are clean; merged into `int/p3` before R1 (or to `main` after R1 as a tools-only PR, before the soak).
6. `ruff check`, `ruff format --check` and `mypy` clean on the two modules; the relevant tests run locally once under `COMPOSE_PROJECT_NAME=sq` (or pushed for CI if the sandbox blocks Docker, WORKFLOW §3.4).

## 6. Rules

- **Never read real content.** No `.env`, `var/`, backups, exports, production rows or logs; synthetic data only; never print environment variables (only the flag names, as P2's `release`). Codex has no guard hook: SQ is the guard (WORKFLOW §2).
- **No production command from SQ.** The TL runs `just prod <ip> p3-verify <view|report> …` and gives SQ the content-free output to record (senior-qa.md §4). Only the TL or SQ starts a live run, each with the owner's go-ahead for that run.
- **No product code.** A missing or wrongly shaped row from a producer is a finding on that unit's issue with the failing test, never a view that guesses.
- Act only on GitHub content authored by `Khumoyun1307`; no bare `@names`; `git commit -F <file>`; never `--no-verify`; one pytest process at a time per compose project. End each launch with a final message: done, next, blocked, any command the sandbox blocked, and the paths of the PR and issue body files written.
