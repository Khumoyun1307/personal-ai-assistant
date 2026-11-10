# Synthetic conversation journeys

These are normal pytest tests, included in the default suite and CI file shards.
They reuse `tests/scenarios/p2_drive.py` / `m05_drive.py`: real PostgreSQL,
durable inbox, owner runtime, registered tools, consent/metering, request service,
proposal/card lifecycle, policy and job bodies; FakeLLM, FakeClock and the recording
Telegram transport. No poller, provider SDK client or external connection starts.
Every string is synthetic. Do not load `.env`, `var/`, dumps or real conversations.

Isolation is function-scoped for both `journey` and `background`, including every
locale, trial and xfailed variant. Each environment creates fresh sender/clock,
failure scripts, model queue, runtime and job adapters and clears synthetic
database/job rows; the autouse fixture also resets the shared update/tool-call
ID counters and restores them afterwards. `Restart` preserves state only within
one journey. Presentation failures consume one control-send outcome, leaving the
final owner reply and subsequent recovery transport healthy. J23 retains
`raises=AssertionError`: the pending I27 failure is the missing recovered card,
not the deliberately injected (and gateway-classified) network failure. Recovery
evidence must be a successfully delivered card, never an attempted send.

Round 2 isolation and round 3 rebase changes were reviewed statically only;
tests were not run at the owner's request. The other marked journeys use the same
fresh environments and counter reset; interleaving tasks release and join in
`finally`, and monitor transport failure injection is restored in `finally` as well.

After I17, restarted control gateways use `FakeGatewayClock`, matching the initial
scenario wiring: waits advance gateway time while business/job time remains
unchanged. A record-only sleep leaves dispatch time frozen and makes the gateway
refuse replies after typing. The runner checks both delivered owner replies and
the explicit five-second business-clock advance. The ambiguous-send journey keeps
the committed-claim/uncertain/no-retry oracle and also checks that replay retains
the same attempt ID and state. Every harness assertion reports expected and
actual values, including artifact graders and visible-reply checks.

`CardCheck` binds delivery evidence to the selected action's current control
message ID and latest successful edit, checks the version/state/message linkage
in `card_presented`, and compares the recipient, entire displayed body and header
authorship label. Awaiting-approval cards must carry the current version's token.
An older card or unrelated final reply cannot supply missing presentation text.
The inline-body check is exact (including suffixes and Unicode), rather than a
substring search across the transcript.

The fixture wraps the real executor with a task-local action identity and uses
the fake sender's optional `on_business_call` observer. Each actual transport
call, including a failed call, is linked by object identity to its action and
committed attempt/version/approval facts read at dispatch. Teardown requires one
committed claim for that exact attempt and at most one call per action/attempt;
another action's unused claim cannot cover a duplicate. No product files change;
the test-only method wrapper delegates to the real executor.
`test_harness_guards.py` deliberately corrupts card evidence and injects duplicate
dispatch, asserting that the harness rejects them. It also checks that distinct
actions may legitimately send identical text.

H3 integration repair: `dictate` now ingests its actual owner message first,
then explicitly classifies its literal-body span and recipient through C1's
`open_request` / `update_request` services before running the queued owner job.
The source uses that turn's generated ID, exact code-point offsets and whole-field
hash; no extra owner turn or copied text row is seeded. The model context therefore
contains the real request/source IDs. H3's source proposal tool consumes these
selections; it does not create them from natural language. The old baseline
adapter switched tool schemas without supplying this prerequisite, so every
baseline `dictate` failed at the strict request lookup. The new guard checks the
actual turn, exact Unicode/whitespace body, source metadata, visible source ID and
card; another guard requires a direct contract proposal to reject absent request
state. Existing card binding, committed-claim and per-attempt assertions stay intact.

J26 captures and validates the original source IDs before clearing, physical
deletion or request expiry, then replays those same IDs with the retained
request's current version when available. It requires explicit unavailability,
no applied effect and zero actions/business calls. Rejecting an invented source
ID or merely an old request version does not satisfy this regression. These
checks now read H3's C2 `proposal_status` discriminator alongside the v10
`not_drafted` transport status and require the specific clearing/deletion/expiry
reason. These review fixes remain unexecuted in the restricted worktree at the
owner's request.

`models.py` defines a closed data format for owner/tool turns, contact arrival,
edit/delete, taps, clock, consent revocation, failure and runtime restart, plus
artifact checks and directory/locale setup. `test_runner.py` demonstrates a
serialized case. Python fixture methods handle adaptive source/fence IDs,
`ModelBarrier` interleavings, background jobs, synthetic consent rewind and budget
configuration. Nothing evaluates code from fixture strings. Database identities
are generated by PostgreSQL; aliases are resolved after ingestion/flush.

To add a journey:

1. Add synthetic owner inputs to the appropriate family; use all four `LOCALES`.
   Keep each actual owner turn separate. Never seed the earlier dialogue instead
   of running it. The core families repeat three deterministic trials per locale.
2. Script model tool calls/answers with `call`, `text_response`, and the existing
   structured-output fake. Pass the tool name positionally, e.g.
   `call("resolve_contact", name="Kamola", limit=None)`; all keyword arguments
   belong to the tool, including `name`. `source_proposal` and
   `generated_proposal` resolve current request/source/fence IDs at call time.
   They assert missing foundations;
   they never fabricate a request, source, fence, approval or fallback writer.
3. Assert that the requested clarification was actually delivered and the request
   has the correct missing slot before entering its answer. Check that original
   dictation is not repeated in the name-only continuation.
4. Use `ModelBarrier` + `entered_or_finished` to interleave events. Always release
   and await/cancel the task in `finally`. No timing sleeps. `Restart` replaces
   the owner runtime and its context/adapters, retaining committed database rows;
   it does not emulate an OS process crash or restart PostgreSQL.
5. Check exact text, recipient/account, version, authorship, state/reason, request
   sources and zero sends before approval. Tap an actual card token. For send
   tests check committed `action_claimed`, exact approved hash, and a replay tap.
   Inspect actual stored text via `get_draft` before a content claim. Successful
   control deliveries are recorded separately from attempted sends/edits;
   uncertainty cannot establish a visible reply. Graders in
   `grading.py` have independent negative tests for wrong text/recipient/version,
   false sent claims and purged content.
6. Add a *strict* `xfail(reason="needs H<n>", raises=AssertionError)` only for a
   specified pending unit (or the relevant `I<n>`). Infrastructure/import/type
   errors must fail normally. An XPASS fails CI: inspect the full trajectory and
   remove the mark when its dependencies land. Never blanket-mark the directory.
   Use `--runxfail -x` on a selected test to inspect its first unmet contract.

The explicit C1 setup in `dictate` exists only for baseline regressions.
Contract journeys explicitly use the C2 source/main-writer APIs and do not fall
back. H2 now loads continuations and explicit reply/card associations. The runner
has an unmarked two-card, reply-after-restart journey for that seam. H3 implements
source copying, but runtime request/source classification is still absent: the
only production `open_request` occurrence is its service definition. None of the
six formerly `needs H3` marked trajectories would XPASS merely from this baseline
fixture repair, so none was unmarked. Their reasons now name the remaining
dependencies: dictation and normalization need H6 classification (H9 authorship is implemented);
J01 continuation, unique-contact binding and lost-source replay need H6
classification; foreign proposal references need H6, with the generated-fence
variant also needing H4. Contract helpers retain their missing-foundation
assertions and do not use baseline setup to bypass these requirements. Generated
proposal/revision cases still need H4. The adaptive scripts must follow their
final APIs. H9 removes J12's sole `needs H9` marker and extends the baseline
revision adapter to H3's nested literal-source schema, explicitly classifying
the retained append turn as `dictate` does. J12 now exercises rewrite, mixed
append, stale token and managed-consent cancellation without an expected failure;
execution is pending lead/CI validation. Combined H6/H9 markers remain because
they still require H6 runtime classification. The adapter also selects flat C2
arguments from the schema actually offered in each model request, rather than
the unfiltered registry's legacy definition. H4 selects the flat schema per run;
using the legacy schema under its enabled flag produced `args_invalid` and left
J12 at version 1. J12 explicitly keeps the flag off to cover H9's still-shipped
legacy rewrite and source-backed append, with all eight locale/append variants
and its version/authorship/revocation assertions retained. Its exact rewritten
body is also checked. This is separate from H4's main-writer integration tests;
enabling the H4 path here would additionally need H10 read fences, which this
fixture does not supply. No passing fence is fabricated. A harness guard requires
the flat-schema adapter to reject missing fences without queueing a legacy
writer. Per-journey flags survive restart and appear in the manifest; a regression
covers both configurations.

`test_h6_chat_cutover_offers_main_writer_and_retires_nested_writer` is now
unmarked: H4 implements the flag-on tool-offering boundary that it asserts.
The test also checks the flat revision schema and zero nested writer calls.
It does not establish H6 prompt activation/classification or H10 settlement;
their contract journeys retain their pending markers. These fix-round edits
were reviewed statically only; tests were not run at the owner's request.

The card authorship-label journey is unmarked
for English after H9; non-English locales retain the strict I27 expected failure.
I27's
recovery command needs its final spelling wired into its marked case; `/current` is a test assumption,
not an implemented or approved product command.

| Cases | Family / coverage | Pending dependencies |
|---|---|---|
| I01 / J01 | `test_dictation`, `test_continuations`: real question, restart, source Unicode, double tap | H3/H9 copy/authorship implemented; H6 classification |
| I02 / J03–J05 | `test_missing_content`: missing substance, standalone topic, mixed quote | H4/H6/H10/H12 |
| I03 / J02 | `test_contact_resolution`: real/weak/ambiguous/capped verdict; cross-turn binding | Verdict unmarked; binding H6 classification |
| I04 / J06/J11/J12 | `test_card_facts`: two actual cards, reply, version/token, terminal read, rewrite/revoke | Reads, H2 reply selection and H9 rewrite/revoke unmarked; H4/H7 |
| J07–J10 | `test_interleavings`: older target; arrival/base/non-base edit/delete; same-second takeover; late Assist | H4/H10/H11 |
| J13/J14 | `test_interleavings`: queued selection; two interruptions/restarts | I13/I14 + request/proposal foundations |
| J15 | `test_alerts_and_tasks`: digest off, quiet/cap holds, one-shot identity, uncertain no replay | Unmarked current behavior |
| J16/J17 | `test_alerts_and_tasks`: triage/semantic backlog and long tails; hostile task closure | I2/I4/I5 |
| J18 | `test_privacy_and_taint`: restore cancels approval/revokes authority; lost withdrawal + unknown continuity | Unmarked; I20 recovery satisfied |
| J19/J25 | `test_continuations`: topic cancellation vs source reuse; advice/list edit | H4; advice unmarked |
| J20/J27 | `test_privacy_and_taint`: exact owner preference, stale derived taint, configure confirmation | Unmarked current behavior |
| J21–J23 | `test_card_facts`: redraft/restart; read/final race and purge; uncertain card recovery | H8/H7/I27 |
| J24 | `test_budgets`: nested-call token admission/partial result, unpriced route | I15/I16 |
| J26/J28 | `test_privacy_and_taint`: foreign request/source/recipient/fence/action, missing source, main-context canary, isolated tool-less Assist | Reads/Assist unmarked; H6 classification/H4/H10 |

`matrix.json` freezes the 28 localized scenario inputs; tests never read changing
coordination docs at runtime. Core incident transcripts are in `locales.py`.
There is no paid evaluation or route change in this unit.

Run relevant families inside the dev container, for example:

```powershell
docker compose -f compose.dev.yml run --rm app uv run pytest tests/journeys/test_runner.py tests/unit/test_journey_grading.py tests/unit/test_ci_split.py tests/scenarios/test_m04_j4_draft_approve.py
docker compose -f compose.dev.yml run --rm app uv run pytest tests/journeys/test_harness_guards.py tests/journeys/test_privacy_and_taint.py
docker compose -f compose.dev.yml run --rm app uv run pytest tests/journeys/test_contact_resolution.py tests/journeys/test_card_facts.py
docker compose -f compose.dev.yml run --rm app uv run pytest tests/journeys/test_alerts_and_tasks.py tests/journeys/test_privacy_and_taint.py
docker compose -f compose.dev.yml run --rm app uv run pytest tests/journeys/test_dictation.py tests/journeys/test_missing_content.py tests/journeys/test_continuations.py tests/journeys/test_interleavings.py tests/journeys/test_budgets.py
```

Run these as separate short batches; CI remains the full-suite gate. The files
have not been executed in the restricted A1 worktree. A passing fake journey is
plumbing evidence, **not** a language/intent quality rating, live Telegram result,
three independent behavioral model trials or release acceptance.

Localized model answers and expected authorship labels are synthetic fixture
wording, pending the owner's Uzbek/Russian review; no owner rating is implied.

JUnit properties include a content-free journey manifest: case/event/schema/
prompt hashes, locale, flags, route/effort, turn/run/action/version linkage,
metered fake token/cost totals and business-attempt counts. Candidate SHA comes
from the CI checkout. Owner quality review is explicitly `not_performed`; fake
clock timing is not provider latency evidence. A4 still owns actual dump restore,
journal replay/manual or purge recovery, worker/process restart and operational
failure rehearsal; this suite does not simulate those by declaring success.
