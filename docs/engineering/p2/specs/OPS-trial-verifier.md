# OPS-TV — Trial verifier and P2 server verification actions

| | |
|---|---|
| **Owner** | Antony · **Peer review** Neryn · **Sign-off** TL |
| **Size** | S |
| **Branch** | `ops/trial-verifier` from `int/m04` → PR to `int/m04` |
| **Depends on** | F0 (schema for every mode); S04.1/S04.4 audit events for `trial-check` (build against F0 factories first; finish the audit-based checks once S04.4 is in `int/m04`) |
| **Normative inputs** | [qa/test-plan.md §6.1, §7](../qa/test-plan.md), IMPLEMENTATION_PLAN §6 acceptance ("zero incorrect sends … checked daily against /activity"), NFR-06, AGENTS §9 (content-free), `tools/acceptance/verify_r0.py` and `deploy/sql/nfr04.sql` (patterns to follow) |

## 1. Goal

Give the live windows, the release checks and the 7-day M04 trial an **objective, content-free** answer to "did every send happen exactly as approved?", plus the read-only views of actions, notifications, tasks and digests the M04/M05 live windows need — without anyone reading message content (AGENTS §9).

## 2. Scope

**In:** `tools/acceptance/p2_verify.py` with modes `trial-check`, `release`, `actions`, `action`, `script`, `notifications`, `tasks`, `digests`; `deploy/server.sh` actions `trial-check` and `p2-verify`; their integration tests; the daily trial procedure line.

**Out:** `nfr05` (S05.2 owns it, same pattern as `nfr04`); any write to the database; anything printing a content column.

## 3. Design

### 3.1 Shape (mirror `verify-r0`)

- `tools/acceptance/p2_verify.py` — stdlib + `psycopg` only, outside `src/` and never imported by it (AGENTS §5). Reads `DATABASE_URL` from the environment like `verify_r0.py`; opens one connection with `default_transaction_read_only = on` and runs everything in a single read-only transaction.
- `deploy/server.sh`:
  ```
  trial-check)  require_data_volume; compose run --rm --no-deps -T -v "$(pwd)/tools/acceptance:/app/tools/acceptance:ro" app python tools/acceptance/p2_verify.py trial-check "$@" ;;
  p2-verify)    require_data_volume; compose run --rm --no-deps -T -v "$(pwd)/tools/acceptance:/app/tools/acceptance:ro" app python tools/acceptance/p2_verify.py "$@" ;;
  ```
  Usage line updated. Called as `just prod <ip> trial-check --since <ISO>` / `just prod <ip> p2-verify <mode> …`. Arguments are validated in Python (`argparse` with strict types: ISO dates, positive ints, prefixes matching `^P2-[0-9]{1,2}$`, ranges `^[0-9]+(-[0-9]+)?(,…)*$`) and reach SQL only as bound parameters.
- **Content-free by construction:** no query selects a column listed in `privacy/content_columns.CONTENT_COLUMNS` for output. The only place content is touched is `script` mode's number extraction, done **inside SQL** (`substring(text from '…')`), returning numbers only — the `verify_r0.py` precedent. `audit_log.details` is printed only through an allow-list of keys (`from`, `to`, `version`, `reason`, `attempt_id`, `identity_pending`, `retry_after`).
- Times print in UTC and, where the plan asks, in the account's timezone (`accounts.timezone`). Exit 0 when every check passes (or for view modes, always 0 unless the query fails), 1 when `trial-check` finds a violation, 2 on bad arguments.

### 3.2 `trial-check [--since ISO] [--until ISO]`

Scope: actions with `created_at` in range plus any action with an audit event in range. Checks (each prints `PASS`/`FAIL <check> action=<id> …` with ids, states and codes only):

| Check | Rule (fails when …) |
|---|---|
| `approved_before_claim` | an action has an `action_claimed` audit row without an earlier `action_approved` row with the same `details.version`; or its origin is `delegation_auto` (no autonomy in P2) |
| `hash_match` | a `succeeded` or `delivery_uncertain` action has `approved_text_hash` NULL, `sent_text_hash` NULL, or the two differ |
| `one_send_per_action` | a `succeeded` action has ≠ 1 `messages` row with its `action_id`; any other state has ≥ 1; or an action has more than one `attempt_id` whose outcome was not a definite non-delivery — outcome events per attempt (matched on `details.attempt_id`): `action_succeeded`, `action_delivery_uncertain`, or `action_late_result` with `details.to = 'succeeded'`. Repeated claims after `action_rate_limited` (`executing → approved`) are allowed — they were never delivered; the test plan's "one attempt per action" means one attempt that delivered or may have delivered |
| `no_send_after_terminal` | an `action_claimed`/`action_succeeded` audit row is later than the action's `outdated`/`expired`/`rejected`/`cancelled`/`blocked` transition; or an action in one of those states has a `telegram_message_id` or a linked `messages` row |
| `recipient_matches` | a linked `messages` row's `conversation_id` ≠ the action's `conversation_id` |
| `no_orphan_sends` | an `assistant_sent` `messages` row in range has `action_id` NULL or points to an action that is not `succeeded` (a late `OK` after `mark_uncertain` moves the action to `succeeded`, decisions R4) |

Summary block: counts by state; `delivery_uncertain` action ids with their local times (for the owner to check the chats — not a failure by itself, test-plan §7); total sends; `RESULT: PASS` or `RESULT: FAIL (<n> violations)`.

### 3.3 View modes (content-free)

| Mode | Prints |
|---|---|
| `release` | Alembic head (`alembic_version.version_num`); **names** of the enabled release flags (only the `FEATURE_FLAGS` environment variable is read, split exactly as `config.Settings._parse_flags` does; no other variable is read or printed, never a secret); presence of each P2 table (`actions`, `tasks`, `conversation_triage`, `automations`, `automation_occurrences`, `digests`, `notifications`); action counts by state |
| `actions --since ISO` | One line per action: id, conversation id, origin, state, state_reason, version, card_state, hash match (`yes`/`no`/`–`), telegram id present (`yes`/`no`), attempts (count of `action_claimed`) |
| `action <id>` | The action's audit timeline: event, time (UTC and owner local), allow-listed details keys |
| `script <prefix> --expect <range> [--direction contact\|owner_manual\|assistant_sent]` | For the synthetic live-window scripts (`P2-4xx` M04, `P2-5xx` M05): which expected numbers were found, by direction, extracted inside SQL; missing and unexpected numbers; never text |
| `notifications --since ISO` | Counts by kind/status/reason_code; per row: id, kind, alert_class, priority, status, seconds from `created_at` to `sent_at` |
| `tasks --since ISO` | Counts by kind/status/closed_reason; per row: id, conversation id, kind, status, created/closed times |
| `digests --since ISO` | Per digest: id, period start/end, status, delivered time in the owner's timezone |

### 3.4 Daily trial procedure (owner checklist line)

"Each trial day: the TL or Antony runs `just prod <ip> trial-check --since <trial start>` and records the RESULT line and counts in the M04 trial log; the owner glances at `/activity` and checks any chat listed as delivery-uncertain. Any FAIL stops the trial (test-plan §7)."

## 4. Interfaces

**Consumes:** F0 schema; the audit events of the per-transition table (F0-interfaces §11, S04.1 §3.6, decisions R15) with `subject_type = 'action'`, `subject_id` = action id and `details = {from, to, version, reason}` — plus `attempt_id` on `action_claimed` and on the outcome events `action_succeeded`, `action_failed`, `action_delivery_uncertain`, `action_rate_limited`, `action_late_result`: the events read here are `action_approved`, `action_claimed`, `action_succeeded`, `action_failed`, `action_rate_limited`, `action_delivery_uncertain`, `action_late_result`, `action_outdated`/`action_superseded`, `action_expired`, `action_rejected`, `action_cancelled`, `action_blocked`, and `connection_marked_stale` (`subject_type = 'connection'`, for the `actions` view's notes); `privacy/content_columns.CONTENT_COLUMNS` (the test imports it). Seed audit rows in tests through S04.1's `transition` (or the factories mirroring it), never hand-written event names.

**Provides:** the server actions used by [qa/test-plan.md](../qa/test-plan.md) §6–§7 and both live checklists.

## 5. Tests (red first; `tests/integration/test_p2_verify.py`, seeding with `tests/integration/p2_factories.py`)

`test_trial_check_passes_a_clean_history`, `test_trial_check_fails_a_send_without_approval`, `test_trial_check_fails_an_autonomous_send_in_p2`, `test_trial_check_fails_a_hash_mismatch`, `test_trial_check_fails_two_message_rows_for_one_action`, `test_trial_check_allows_claims_after_rate_limits_but_fails_two_delivered_attempts`, `test_trial_check_fails_a_send_after_an_outdated_transition`, `test_trial_check_fails_a_wrong_recipient`, `test_trial_check_fails_an_orphan_assistant_sent_row`, `test_trial_check_lists_uncertain_actions_without_failing`, `test_every_mode_is_content_free` (a canary in every `CONTENT_COLUMNS` column never appears in any mode's output), `test_release_prints_flag_names_and_no_other_setting` (a canary in another setting's value never appears), `test_script_mode_reports_found_missing_and_unexpected_numbers_only`, `test_modes_run_read_only` (a write inside the transaction fails), `test_bad_arguments_exit_2_and_never_reach_sql`.

`tests/unit/test_server_sh.py` (or extend the existing shellcheck step): the two new actions are present and mount `tools/acceptance` read-only.

## 6. Acceptance criteria

1. `trial-check` detects each incorrect-send class of IMPLEMENTATION_PLAN §6's acceptance (wrong recipient, text ≠ approved, duplicate, sent after outdated/expired/rejected, sent without approval) on seeded data, and passes a clean history.
2. Every mode is read-only and content-free (tests); `release` prints flag names only.
3. The actions run on the server through `deploy/server.sh` like `verify-r0` (dry-run once on the production server in the CP3 window, output checked by the TL before the trial starts).

## 7. Docs

`deploy/runbooks/` (deploy runbook: the new actions), [qa/test-plan.md](../qa/test-plan.md) §6.1 stays the usage reference; ARCHITECTURE §17 one line ("P2 verification actions, content-free"). The PR description notes the `one_send_per_action` interpretation for the TL.

## 8. Risks

- **A check that reads content by accident** — the canary test over every content column is the guard; reviewers check every SELECT list.
- **Audit-detail drift** (S04.1/S04.4 change key names) — the verifier's tests use the factories' audit helpers; any rename breaks them in CI.
