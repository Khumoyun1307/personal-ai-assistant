# QA-S5 implementation evidence

Issue #63, branch `qa/m05-scenarios`, based on `int/m05@ca624a8`.
All 24 names in test-plan §5 are implemented, with two additional driver checks
and one late-note assertion regression.
This is readiness of the test unit; expected failures are pending acceptance.

The driver calls both bootstrap phase extension points, uses the real durable
inbox and production registered job bodies, and injects only FakeClock,
FakeSleep, FakeLLM and the recording Telegram transport. Due jobs are claimed
before invocation and completed afterwards; future jobs wait for FakeClock.
Unknown queued registrations and unbounded drains raise RuntimeError, which
the markers cannot hide. No provider client, poller or worker is started.

Local red-first run: `uv run pytest tests/scenarios -k m05 -ra --tb=short
-o addopts=` — **23 failed, 3 passed, 1 deselected**, 158.63 s. The two driver
checks and the conversation-deletion journey pass. Missing registrations,
the unavailable monitor confirmation tool and Assist are the feature failures.
No collection or fixture error is marked expected.

Each failing feature case has `xfail(strict=True, raises=AssertionError)`.
A focused marked rerun of injected monitor confirmation, notification dedupe
and feedback produced **3 xfailed**, 74.89 s. CI is the full test gate.
Passing after a feature lands causes strict XPASS until the owning unit removes
that case's marker (R13). CP5 requires no markers or skipped scenarios.

| Scenarios | Required units; last dependency removes the marker |
|---|---|
| Six `test_m05_j6_monitors.py` journeys | S05.4; notification delivery also needs S05.2 |
| Quiet hours into the next digest | S05.2 + S05.3b |
| Urgent bypass, cap, dedupe, uncertainty, feedback | S05.2 |
| System alert despite quiet hours and cap | S04.4 alert kind + S05.2 synchronous `notify_owner` routing at CP4 |
| Four `test_m05_digest.py` journeys | S05.3b; DST recurrence consumes S05.3a |
| One Assist card per triaged burst | S05.1 + S04.2b/S04.3 and real requester wiring at CP4 |
| Urgent decision alert | S05.1 + S05.2 |
| Repeated promise, owner closure, isolation/no tools, queued consent | S05.1 |
| Conversation deletion, other chat intact | F0; passes on this base |

Notification tests use decision/high alerts where immediate delivery is
needed: normal/low alerts are batched by default. Monitor creation always
checks for the confirmation card and no automation before the tap. The taint
journey first reads contact-authored injection text via `get_conversation`.
Isolation triages two synthetic chats and inspects each recorded request.
Repeated commitment testing cites a new message and the existing task id,
covering source-id merging. The deletion journey confirms through the real
owner card and preserves the other chat's task, verdict, alert and monitor.

Only synthetic data in compose project `antony` is used. No live API calls,
production/server action or eval spend.

## S05.2 integrated during this unit

The integration base advanced to `ca624a8` when #87 merged. The first PR CI
run [40637332291](https://github.com/Khumoyun1307/personal-ai-assistant/actions/runs/40637332291)
then reported exactly five strict XPASS cases: urgent bypass, hourly cap,
duplicate alert, ambiguous delivery/reconciliation and feedback. Their complete
bodies passed; no assertion was weakened. Their markers have been removed.
The integration-base update preserves the already pushed history.

The unit now expects **9 passing checks / 18 strict expected failures** on this
base, including the unmarked late-note assertion regression added below.
The quiet-hour-to-digest case still awaits S05.3b; the system-alert migration
still awaits CP4. Full verification after marker removal belongs to CI.

## Jack review fix round (2026-10-28)

The system-alert journey now calls the production synchronous
`OwnerAlerts.notify_owner(DELIVERY_UNCERTAIN, contact=...)` entrypoint used by
S04.4, after filling the hourly cap and enabling quiet hours. It requires one
new control send and the corresponding sent system notification row. The
`ALERT_TEXT` guard keeps the unavailable S04.4 + CP4 dependency an explicit
AssertionError; it does not enqueue a synthetic system renderer path.

The missed-digest journey now checks the successful digest's own returned
message id and the whole word `late`. The local recording transport preserves
the normal fake's behavior and records successful control-message ids only.
An unmarked assertion regression uses the real alert/channel path to prove
that an unrelated `late` and the digest substring `later` are both rejected,
while a late note on the selected message passes. It is a check of the
assertion, not proof that S05.3b has landed.

Red first: the new assertion regression against the old transcript-wide
substring check failed with `DID NOT RAISE AssertionError` (**1 failed,
127.40 s**). The two feature journeys retain their original strict markers.
Only synthetic data in compose project `antony-3` was used in this round.

Final focused run: the new assertion regression, missed-digest journey,
system-alert journey and existing real-inbox/no-external-clients driver check
produced **2 passed / 2 strict expected failures, 57.98 s**. The expected
failures still name S05.3b and CP4; this does not claim either feature works.
Changed-file Ruff and format checks pass. The full suite was not run locally;
CI remains its gate.
