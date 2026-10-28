# QA-S4 implementation evidence

Antony owns issue #62 and branch `qa/m04-scenarios`, based on
`int/m04` at `f4030c3e4985c2b91809b3bf442d0a54a3f70d0d`.
This records automated scenario evidence; it is not CP3 acceptance or a live-test result.

## What the scenarios exercise

All 38 named scenarios in test-plan §4 have implementations. Double-tap
batching and tainted/untainted resume requests each add a parameter case.
Five additional checks verify the driver independently of missing features.

| File under `tests/scenarios/` | Coverage |
|---|---|
| `test_m04_j4_draft_approve.py` | Exact approved version/hash/recipient, replacement and instruction edits, revision interleavings, reject, dictated text |
| `test_m04_outdating_and_expiry.py` | Contact messages, manual replies, source deletion/edit, Ignore, consent, rights, window expiry, approval-before-ingestion |
| `test_m04_j8_takeover.py` | Late manual reply; takeover audit without a contact-facing message |
| `test_m04_j9_kill_switch.py` | Autonomous pause, stop word without a model call, card-only resume, owner-approved send while paused |
| `test_m04_r1_at_most_once.py` | Double taps in one/two batches, crash after claim, timeout, 429 backoff, late settlement |
| `test_m04_r1_assistant_sent.py` | One durable record, echo-before-result merge, two zero-id sends |
| `test_m04_j10_activity.py` | All outcomes/authorization in command and tool, distinct status groups, catch-up activity |
| `test_m04_assist_bursts.py` | Debounce, no-reply verdict, owner/automated-owner negative triggers, Observe negative trigger |
| `test_m04_restore.py` | Restored approved/executing actions, content-free notice, reconciliation without sending |
| `test_m04_drafts_only.py` | Sending off permits a proposal, then approval is honestly blocked with copy-ready text |
| `test_m04_drive.py` | Real inbox setup, committed F0 seeds, registered job execution/order/limit/due-time/no repeat |

`p2_drive.py` calls bootstrap's M04 extension point with fake transport/model
adapters and rebuilds the existing integration inbox with its listeners/routes.
Owner tools and action jobs use the production registrations. The driver marks
a due job doing before calling its registered function, releasing its todo-only
queueing lock, and records its completion or failure. It does not implement
drafting, approval, sending, retries or reconciliation itself.

F0 seed helpers establish otherwise unreachable states. The default synthetic
account has both AI grants, one connected chat, a retained source message and
a caught-up barrier bound to this test process. Send assertions inspect the
fake transport's exact kwargs; revision/send interleavings use explicit async
events/hooks and committed database reads. Watchdog timeouts report hangs;
they do not order the interleavings.

## Expected failures and gate interpretation

Local evidence on the base above (all commands run in the dev container):

| Check | Result |
|---|---|
| `uv run pytest tests/scenarios -k m04 -ra --tb=short -o addopts=` before markers | 43 selected: 36 failed, 7 passed, 1 deselected; 387.44 s |
| Failure classification | 35 missing-feature assertions; one QA setup error (catch-up needs a string `since`), corrected without an xfail |
| Rerun `test_m04_j10_activity.py`, `test_m04_assist_bursts.py`, `test_m04_j9_kill_switch.py` with markers | 5 passed, 7 xfailed; 180.94 s |
| Driver checks in the initial run | All 3 passed |
| `uv run ruff check tests/scenarios` | Passed |
| `uv run lint-imports` | 5 kept, 0 broken |

Eight distinct checks have passed across these runs: three driver checks,
three activity journeys, the stop word and the Observe negative trigger.
The 35 feature-dependent cases have individual strict markers. The complete
marked rerun and repository-wide verification belong to CI, linked from the PR;
the local evidence is not a claim that all 43 cases passed.

Feature-dependent scenarios are run without markers before assigning
`xfail(strict=True, raises=AssertionError, reason="awaiting <WU>")`.
Only demonstrated missing-feature assertions qualify. Collection, fixtures,
imports and unrelated exceptions must remain errors. Feature owners remove
the marker when their implementation turns its scenario green (R13).

The local run results, per-scenario failures and CI run are recorded in the PR
and issue #62. Passing driver/negative/activity checks establish only their
own behavior. CP3 requires every M04 scenario to pass with no xfails or skips,
plus the independent live-window, restore and trial evidence.

Only `assistant_test` in compose project `antony` and synthetic fixtures are
used. No real provider, Telegram, production/server action or eval spend is
part of this unit.

## Peer-review fix round

The first Neryn/Viro reviews found harness and assertion defects that the
original strict markers hid. Those original red runs do not establish the
approval or final-card parts of the journeys. The fix round uses a parser-valid
seed token, observes every fake transport recording (including plain sends),
scripts zero ids through the plain transport, reads `LLMRequest.purpose`,
revokes through the owner command, and advances beyond the configured uncertainty
threshold. Copy and Re-draft assertions inspect the final card's buttons;
activity assertions inspect only the new command reply. Driver guard failures
raise RuntimeError, so xfail cannot absorb them.

Relevant rerun: driver, activity and outdating files — **8 passed, 9 xfailed**,
171.09 s. This includes independent checks that plain sends/edits are observed
and the initial transcript cannot satisfy final-card assertions. Full marked
execution belongs to CI. Per-scenario removal ownership is in regression-matrix
§D; no feature acceptance is claimed while a marker remains.
