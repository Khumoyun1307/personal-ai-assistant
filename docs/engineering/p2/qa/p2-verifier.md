# OPS-TV: content-free views and the daily trial check

Issue #65. Part 1 (`ops/trial-verifier`, PR #109) added the read-only views.
Part 2 (`ops/p2-trial-check`) builds the audit on S04.4's merged executor.

## Part 1 evidence

Implemented: release, actions, action, script, notifications, tasks, digests,
and the `deploy/server.sh p2-verify` wrapper. The script imports only stdlib
and psycopg, uses one connection with default_transaction_read_only enabled,
and one transaction. Queries have a 10-second statement timeout and a
1-second lock timeout. Arguments are validated before a connection is opened.
All ordinary runtime errors print their class only, never the message or DSN.

No content column reaches Python for output. Script-number extraction is in
SQL; audit details have both key and value allow-lists. The canary test writes
synthetic data into every CONTENT_COLUMNS entry (the generated search vector
comes from its search key), verifies coverage, then checks all seven modes.
It also drives an action through the real lifecycle transitions, inserts a
delivered digest, and requires populated output from every view. The action
checks pin the claim timeline, attempt id, attempt count and matching hashes;
an unlisted audit-detail canary must remain hidden.

Red-first evidence: release view and server wrapper tests failed before the
implementation (**2 failed, 68.06 s**). Local verification results and CI are
recorded on the PR. Only the synthetic test database is used.

The part 1 views alone do not prove trial acceptance. The audit is described
below; peer/TL review and the server dry-run during the owner-approved CP3
window remain separate requirements.

Final affected-view rerun (release, all-mode content canaries, script numbers)
passed: **3 passed, 271.03 s**. The preceding run had **11 passes** for the
other checks (including read-only enforcement, seven invalid-argument cases,
audit-value filtering and the wrapper). Its two failures were the release
schema lookup, fixed to `app.alembic_version`; both are covered by the final
rerun. Thus all 13 relevant cases have passing evidence across these runs.

Review fix round: the new failure-boundary and session-timeout checks failed
before the implementation change (**3 failed, 2 passed, 78.22 s**). The canary
fixture now exercises populated action and digest views instead of accepting
empty output. Focused verification after the fix: **16 passed, 114.61 s**
(`test_p2_verify.py` unit/integration files and `test_server_sh.py`); Ruff
check and format check passed on the changed Python files. An independent
QA engineer reviewed the fix without further findings. CI and the external
Neryn/TL recheck remain required after the push.

## Part 2: daily trial check

Red-first evidence: before adding the mode and server action, the parser
rejected `trial-check`; the new wrapper and usage tests failed because the
arm and usage entry were missing. The existing `p2-verify` wrapper test passed.
These were synthetic/local checks; no server action was run.

In an owner-approved server window, the TL runs:

```powershell
just prod <ip> trial-check --since <trial-start-ISO-with-timezone>
just prod <ip> trial-check --since <start-ISO-with-timezone> --until <end-ISO-with-timezone>
```

`p2-verify trial-check` accepts the same arguments. Both server actions require
the existing data volume and start a one-off container with `--no-deps` and
the reviewed `tools/acceptance/` mounted read-only. They do not start a poller.
The verifier uses one read-only, repeatable-read database transaction and the
same query timeouts as the view modes, so concurrent writes cannot split the
checks across different snapshots.

The interval includes `since` and excludes `until`. Both must include a
timezone; defaults are the Unix epoch and the current UTC time. An empty or
reversed interval is rejected before opening a database connection. Actions
created in the interval, with any audit event in it, or with a linked message
created in it are checked against their retained history; orphan
assistant-sent messages are checked in the interval.

| Check | Evidence checked |
|---|---|
| `approved_before_claim` | Every claim follows approval of that version; no autonomous-origin action is allowed in P2. |
| `hash_match` | Successful or uncertain delivery has non-null, matching approved and sent text hashes. Approved and scheduled actions have non-null text (computed as an SQL boolean). |
| `one_send_per_action` | A success has one linked message, except the deletion receipt case below; other states have none; at most one attempt delivered or may have delivered. |
| `no_send_after_terminal` | A claim or success never follows a terminal denial, and denied actions have no delivered message identity. |
| `recipient_matches` | Linked messages belong to the action's conversation. |
| `no_orphan_sends` | Every assistant-sent message in the interval belongs to a successful action. |

Repeated claims after definite rate-limited non-delivery are allowed. Every
outcome names the latest claim; each attempt has exactly one closing outcome,
with at most one late result after uncertainty. A claim without an outcome
fails (`missing_outcome`), except an executing action's current in-flight claim.
Every claim counts as possibly delivered until definite non-delivery closes
it; two distinct possibly delivered attempts fail. Approved or scheduled
actions with claims require their latest claim to have closed as rate-limited.
A `delivery_uncertain` state with valid evidence is listed for the owner
to check and does not fail the trial by itself.

A succeeded action with no retained linked message prints the non-violation
code `message_deleted_after_success` only when a later `data_deleted` or
`managed_data_deleted` audit receipt covers that account and conversation.
Conversation, contact, everything and managed-data deletion scopes are
recognized; timestamp ties use audit ids. Missing/mismatching receipts still
fail. This exception never waives approval, outcome, hash, recipient or duplicate
checks, and never permits NULL text in an approved or scheduled action.

Output includes each check's `PASS`/`FAIL`, fixed violation codes with account
and action ids, each scoped action's structural state, counts by state,
uncertain action ids with UTC and owner-local times,
total assistant-sent messages created in the interval, and `RESULT: PASS` or
`RESULT: FAIL (N violations)`. Text, names,
hash values and raw audit details are never printed. Exit codes are 0 for a
clean check, 1 for violations and 2 for invalid arguments. Operational errors
also exit 1 and print only the exception class; a missing RESULT is never a PASS.

Each trial day, record the RESULT and counts in the M04 trial log. The owner
checks `/activity` and any uncertain chat. Any incorrect send stops the trial
under [test-plan.md §7](test-plan.md#7-trial-support-m04-7-days-after-cp3).
The CP3 server dry-run and seven daily records remain live evidence; synthetic
test results alone do not complete those steps.

Part 2 local verification (2026-10-28, synthetic PostgreSQL, project `antony`):

- Initial focused gate: **60 passed, 260.66 s**.
- Independent review found an executing action could pass without its current
  claim audit. The added regression was run red: **3 failed, 1 passed, 55.78 s**,
  covering missing current claim, missing history and an older valid 429 claim
  that cannot authorize the current attempt. The intact executing control passed.
- After requiring the current attempt/version's claim: **64 passed, 349.97 s**
  across `test_p2_verify.py` (unit/integration) and `test_server_sh.py`, with no
  skipped or expected failures. Changed-file Ruff check/format, strict mypy on
  the verifier and `bash -n deploy/server.sh` passed.
- The all-mode canary's trial interval was then made explicit over the fixed
  synthetic dates, removing its wall-clock dependency: **1 passed, 102.44 s**.

The backend and final claim fix also received independent delegated source
reviews; the root reviewed the complete diff. CI and external Neryn/TL review
are tracked on the PR linked from #65. This is automated tooling evidence;
the owner/TL CP3 server dry-run and daily trial checks remain pending.

One review fix round (2026-10-28, Neryn review and TL disposition on PR #123):

- Before the fix, the 44-case focused selection produced **28 failures and
  16 passes**, reproducing stale outcomes, missing outcomes, contradictory
  closures, rearming after uncertainty, NULL pending text, linked-message
  scope gaps and privacy deletion false alarms.
- Added direct terminal-send and outcome-order assertions, a real
  `ActionExecutor` fixture with a fake Telegram sender, four real deletion
  scopes and negative receipt-account/conversation/time/outcome cases.
- Final focused gate: **108 passed** across `tests/unit/test_p2_verify.py`,
  `tests/unit/test_server_sh.py` and `tests/integration/test_p2_verify.py`, in
  Compose project `antony-tvfix`. Changed-file Ruff check/format and strict
  mypy on the verifier (`--cache-dir /tmp/mypy-cache`) passed. Full-suite
  verification remains GitHub CI's gate; independent peer/QA/TL reviews
  follow the push.
- The change remains in QA tooling/tests/docs; no product source, migrations,
  provider calls, Telegram calls or production actions are involved.
