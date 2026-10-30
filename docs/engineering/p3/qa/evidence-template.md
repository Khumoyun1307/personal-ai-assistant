# Evidence template — P3 run or checkpoint

Copy into `qa/evidence/<YYYY-MM-DD>-<run>.md` during LIVE-P3. This is a blank template, not evidence. Sources: [LIVE-P3 §3.2, §3.13](../specs/LIVE-P3.md), [GATE-RUN §3.4, §5](../specs/GATE-RUN.md), [OPS-TV3 §3.6](../specs/OPS-TV3.md#36-content-rules).

Use only counts, states, codes, timings, synthetic marker numbers, commit SHAs and CI/review links. Runtime IDs in TL output become stable **local aliases** (`action-A`, `delegation-A`, `chat-A`) before publication. Keep no production-ID lookup table here. No message/owner-note text, names, usernames, phones, IPs, hostnames, emails, filenames from exports, content hashes, exception messages, raw JSON, DSNs or secrets. Owner judgements are yes/no or approved enum ratings; never quote the content (launch public-evidence boundary; LIVE-P3 §4 L3).

## <Run> — <YYYY-MM-DD>

| Field | Value |
|---|---|
| Run / checklist section / checkpoint | <reference> |
| Status | NOT RUN / PASS / FAIL / NOT EVALUABLE |
| Date, owner timezone, local start/end | <date, timezone, times> |
| UTC start/end and verification interval | <ISO with timezone; interval [since, until)> |
| Roles and chats | <owner, second account, F1, F2, TL, SQ; chat aliases only> |
| Bot/configuration | <Alviro production window / Alviro LV-16 D+6 / synthetic eval; no IDs> |
| Owner go-ahead | <per-run time and short reference, not a quotation; LV-16 also records merged tool change and approved AGENTS §9 production-token allowance> |
| Started by | <TL / SQ under TL brief for eval; TL for LV-16; TL server for live> |
| Deployed / evaluated commit | <SHA and merge PR> |
| Automated twins | <exact scenario IDs/names; CI URL and SHA; passes/fails/skips/xfails> |
| Alembic head | <revision> |
| Release expectations | <complete cumulative flag names, languages, allow-listed settings> |
| Release actual / guards / exit | <safe lines; flags/languages/settings PASS/FAIL; exit number> |
| Notice guard, when relevant | <version, delivered UTC, elapsed hours; uncertain is not delivered> |

### Steps

| # | Checklist/spec reference | Expected | Observed safe projection | Owner yes/no | Result |
|---|---|---|---|---|---|
| <n> | <section and ID> | <state/count/timing> | <aliases, codes, counts> | <yes/no/not asked> | PASS / FAIL / NOT RUN / NOT EVALUABLE |

### Summary checks

| Check | Evidence |
|---|---|
| `trial-check` | <interval, shadow yes/no, RESULT, exit; each check result/code/count> |
| Autonomous totals | <actions, claims, succeeded, uncertain, failed, cancelled, disclosed, scheduled_now, scheduled_overdue, last_claim_utc> |
| Held replies | <fallback, shadow, converted, reproposed; autonomy_not_sent counts by safe kind/status if available> |
| Marker verification | <prefix, direction, exact expected numbers, found/missing/unexpected; optional/retry allocation; earlier evidence for deleted markers> |
| Positive control | <at least one succeeded auto action for J5/soak; reminder delivered while paused; other run-specific control> |
| LV-16, if run | <shadow evidence cut-off; probe interval; counts/pace/first error/retry_after; zero post-error sends; delegation ended; echo class counts or no echo; zero new takeover/application business actions; end before J7; TF §12 existing-row agent-run follow-up link, no Verified label> |
| Pause | <source/attempt/marker, cancellation delta, lane, trigger_at, apply_ms, computed latency; button stopwatch upper bound; no-claim-after-brake result> |
| Catch-up / notices | <delivered state and counts; owner accuracy and Pause-button yes/no> |
| Reminders / follow-ups | <task/automation/nudge aliases, states, due/delivered UTC/local, delay, late/skipped; approve/skip> |
| Export | <parts, bytes, classes, delivery-state counts, complete yes/no; owner-only inspection yes/no; no filenames or files attached> |
| Memory review | <created, dropped_sensitive, approved, edited, discarded, daily cap> |
| Restore | <scratch P3 lines, active-at-backup count≥1, ended/cancelled/paused counts; key removed yes/no> |
| NFRs, if run | <sample counts, p95s, per-source pause latency, availability/window/down/budget minutes; not evaluable where no data> |
| Weekly rating, if run | <ISO week, score 1–5, counted/pre-trial/no card sent; malformed count> |

Do not copy a raw console transcript. Store only the allowed projections above. A view that ran successfully with zero rows proves no feature outcome by itself. An uncertain action goes to owner client inspection through TL and is never retried (LIVE-P3 §3.2, §3.10–3.13).

### Eval addendum (only for GATE-RUN)

| Field | Value |
|---|---|
| Run / estimate / limit-one / full | <artifact links; separate go-ahead references for each paid run> |
| Candidate / model / rubric / judge labels | <frozen versions from reports; configuration names, no secrets> |
| Data / manifest / confirmations | <version/digests from synthetic artifacts, confirmed counts by set/language, owner-review reference> |
| Budget | <approved phase cap USD; prior spend; worst-case estimate; run hard cap; actual spend incl. aborted/limit-one; new total and remaining> |
| Completeness | <expected/evaluated/not-evaluated counts; harness denials, signal mismatches, leaks, forbidden-hit counts> |
| Pre-review / QE2 | <fresh review link; check #1/#2/#3 link and status> |
| Ratings / verdicts | <rated/unrated/confirmed/pending counts; no copied replies or quotes; ledger/sheet links for synthetic artifacts> |
| Gate | <per-language PASS/FAIL/NOT EVALUABLE, k/N and reasons; passed-only recommendation> |
| OD-29 | <confirmed/pending slip-through IDs and reason codes only> |

### Deviations, pending checks and failures

| Item | Evidence / gap | Severity | Owning unit | Follow-up / issue / next window |
|---|---|---|---|---|
| <n> | <codes/aliases/counts; not content> | stop-the-line / blocking / open item | <unit> | <reference> |

A missing result, unused optional step or failed timing round is never PASS. Note settings restored, test delegations ended, pending cards left for owner action and any effect on J7 silence. For a brake, record reduced-set release result, claims/last-claim comparison, fix/deploy approval and replacement T0 (LIVE-P3 §3.12).

### Sign-off

| Role | State / date / reference |
|---|---|
| SQ recorded same day | <state> |
| TL reviewed | <state> |
| Owner accepted checkpoint, where required | <accepted / pending / declined> |

For CP-L, CP-T and CP-X, list each criterion separately with its evidence link or NOT EVALUABLE; use LIVE-P3 §3.13. For CP-A/R1, include merge-result CI, mutation/race evidence and marker-audit counts (QA-X §3.8; README §5).
