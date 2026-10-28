# OPS-TV part 1: read-only views

Issue #65; `ops/trial-verifier` from `int/m04@f4030c3`.

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

Pending: the complete trial-check audit and its violation fixtures after S04.4
merges, peer/TL review, and the server dry-run during the owner-approved CP3
window. This part does not mark OPS-TV complete or prove trial acceptance.

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
