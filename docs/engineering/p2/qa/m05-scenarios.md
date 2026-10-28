# QA-S5 implementation evidence

Issue #63, branch `qa/m05-scenarios`, based on `int/m05@3aa74e0`.
All 24 names in test-plan §5 are implemented, with two additional driver checks.
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
| System alert despite quiet hours and cap | S05.2 system-alert caller/renderer migration at CP4 |
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

The unit now expects **8 passing checks / 18 strict expected failures**.
The quiet-hour-to-digest case still awaits S05.3b; the system-alert migration
still awaits CP4. Full verification after marker removal belongs to CI.
