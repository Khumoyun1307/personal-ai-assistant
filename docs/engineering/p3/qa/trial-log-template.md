# P3 soak and 14-day trial log template

*Copy to `qa/trial-log.md` in LIVE-P3. No trial has run in this template. TL executes server checks; SQ records safe results the same day. Sources: [LIVE-P3 §3.10–3.13](../specs/LIVE-P3.md), [OPS-VIEWS §3.7](../specs/OPS-VIEWS.md#37-report---since-iso---until-iso), decisions §B10 P3-T160.*

No message content or free-text owner diary. Use role/chat aliases, approved enums, counts and yes/no answers. Replace production IDs with stable local aliases before publication; no lookup table. Follow [evidence-template](evidence-template.md) for all data boundaries.

## 1. Soak record

| Field | Value |
|---|---|
| Start / target end, UTC and owner-local | <times; ≥3 full days, OD-28 default> |
| Standing delegation | <local alias; exactly one chat; active throughout yes/no> |
| Owner confirmed contact was informed | <yes/no and reference; no name> |
| CP-L, Pause-button gate, notice guards | <evidence links; PASS required> |
| Deployments complete | <SHA; no feature deploys after soak begins> |
| Daily fixed check | 09:30 owner-local, <timezone> |
| Three consecutive daily checks | <links and results; elapsed duration also ≥3 days> |
| Succeeded standing replies | <count; ≥1 before T0; zero means wait, not pass> |
| Recorder history at T0 | <recorded_since_utc; ≥5 days> |
| Soak restart, if any | <brake/fix/evidence/new start> |

## 2. T0 record — freeze

| Field | Value |
|---|---|
| Trial iteration / start UTC / owner-local | <iteration and times> |
| Planned end | <T0 + 14 days> |
| Owner T0 acceptance | <reference/time> |
| Deployed SHA / Alembic head | <values> |
| Full cumulative `flags=` set | <sorted names, including existing flags> |
| Languages | <exact CP-G-passed list> |
| Every allow-listed `setting=` line | <validated values from release, including default/set/unset; never secret settings> |
| Release guard results / exit | <flags/languages/settings/notice PASS, exit 0> |
| Standing delegation / informed contact | <alias; owner yes/no> |
| Clean soak / ≥1 succeeded / recorder≥5 days | <evidence links> |
| CP-L / CP-A / CP-G / LV-16 | <accepted evidence links> |

Keep this record immutable for the trial iteration. A restart gets a new record; retain prior failed iterations and spend/evidence. The owner-overlap variant requires an explicit decision and the revised LIVE-P3 §3.14 conditions, never an assumed waiver.

## 3. Daily ledger

Use one row per soak day and each trial day. Add soak rows before day 1. Fill dates from actual T0, not the planning calendar. All interval checks are cumulative from soak start or T0; never use `--shadow` during the trial (LIVE-P3 §3.10–3.11).

| Day | Owner-local date / check UTC | Interval [T0, until) | Release result / exit; freeze unchanged or changed names | Trial result / exit | Evidence | Activity glance yes/no | Deviations |
|---|---|---|---|---|---|---|---|
| 1 | | | | | | | |
| 2 | | | | | | | |
| 3 | | | | | | | |
| 4 | | | | | | | |
| 5 | | | | | | | |
| 6 | | | | | | | |
| 7 | | | | | | | |
| 8 | | | | | | | |
| 9 | | | | | | | |
| 10 | | | | | | | |
| 11 | | | | | | | |
| 12 | | | | | | | |
| 13 | | | | | | | |
| 14 | | | | | | | |

TL runs at 09:30 owner-local:

```text
just prod <ip> p3-verify release --expect-flags <T0-full-set> --expect-languages <T0-list> --expect-setting <NAME=VALUE>
just prod <ip> p3-verify trial-check --since <T0-UTC> --until <now-UTC>
```

Repeat `--expect-setting` for **every** setting line captured at T0. Record both exit codes. Daily detail (copy under each row's evidence link):

| Detail | Value |
|---|---|
| Each trial-check check | <PASS/FAIL plus fixed codes and counts> |
| Autonomy | <actions, claims, succeeded, uncertain, failed, cancelled, disclosed, scheduled_now, scheduled_overdue, last_claim_utc> |
| Change since prior day | <claim/succeeded delta; no new claims is recorded, not failed> |
| Held replies / alerts | <fallback, shadow, converted, reproposed; autonomy_not_sent counts when exposed> |
| Uncertainty | <action aliases; TL asked owner; owner inspected yes/no; never resend> |
| Freeze | <GUARD flags/languages/setting outcomes; changed names if any> |
| Errors / availability signal | <fixed error classes/codes; no exception text> |
| Owner daily `/activity` glance | <yes/no; no narrative content> |
| Follow-up | <issue, unit, severity, next check> |

Exit 1 from trial-check stops the line. Exit 2 on either command: rerun once within 15 min; second exit 2 means unverified and stops the line. Any release FAIL while autonomy.send is on stops the line; languages FAIL or added autonomy/delegation flags are explicit triggers. Other setting/flag differences break the freeze and default to clock restart, owner decides (LIVE-P3 §3.11).

A missed check is `missed`, never PASS. TL runs it as soon as available that day; by 18:00 if still missing, tells owner to decide whether to `/pause`. Two missed days in a row go to owner as a trial-quality question. Next cumulative check covers the interval but does not rewrite the missed row (LIVE-P3 §3.11).

## 4. Day-7 and day-14 measurements

After the daily check, TL runs reports `nfr05`, `nfr07`, `nfr08` for 7 or 14 days and `p3-verify report --since <T0> --until <min(now,T0+7d-or-14d)>`. Never pass a future `until` (LIVE-P3 §3.11; OPS-VIEWS §3.1).

| Measure | Day 7 | Day 14 |
|---|---|---|
| Exact report intervals / exit codes | | |
| Triage p95 seconds / sample count (target <90 s) | | |
| Urgent-alert p95 seconds / sample count (target <120 s) | | |
| Immediate alerts: sent / not useful / percent | | |
| Core alerts: sent / not useful / percent | | |
| Per-kind sent / not useful counts | | |
| Both shares not-useful≤30% (≥70% not marked) | | |
| Pause command/stop_word latency + samples | | |
| Button latency upper bounds + sample count | | |
| New autonomous claims after pause (required 0) | | |
| Availability window / polled / down minutes | | |
| Availability percent (≥99%) / recorder start | | |
| Down-minute budget (100.8 at 7 d; 201.6 at 14 d) | | |
| OPS-AV / report availability agreement | | |
| Report criterion 1: send safety | | |
| Report criterion 2: alert usefulness | | |
| Report criterion 3: ratings | | |
| Report criterion 4: availability | | |

Criterion states: `met`, `NOT met`, `no data`, `not evaluable`/`unavailable` as printed. No samples or missing recorder history cannot pass. Use both alert denominators so owner-requested cards cannot dilute poor alert usefulness (OPS-VIEWS §3.7; LIVE-P3 §3.11).

## 5. Weekly ratings

Sunday 19:00 owner-local, at most once per ISO week. TL `just prod <ip> report usefulness 7`, or the report's ratings line. No Monday catch-up: if the worker missed Sunday evening, write **no card sent**, never a rating (OPS-RATE / P3-T160; LIVE-P3 §3.11).

| ISO week | Card date/local time | Score 1–5 / no card sent / not answered | Pre-trial or counted | Malformed count | Evidence |
|---|---|---|---|---|---|
| <week> | | | | | |
| <week> | | | | | |
| <week, if needed> | | | | | |

Need at least two counted weeks, each score≥4; pre-T0 ratings do not count. A rating <4 is an exit miss reported that day, not a safety stop (LIVE-P3 §3.11–3.12).

## 6. Stop-the-line and restart ledger

| Trigger UTC / code / alias | Owner `/pause` | autonomy.send removed / TL restart | Reduced-set release / exit | Claims and last_claim_utc before→after | Classification / fix PR | Deploy go-ahead / re-enable | New T0 or owner exception |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

If a new claim appears after the brake, line stays stopped and TL stops the app until classified; nothing resumes automatically. A safety or repeated-unverified failure restarts the 14-day clock after fix/deploy/re-enable and PASS. A verifier defect still restarts by default unless owner explicitly decides otherwise. Soak failures restart the soak. NFR/usefulness misses and non-safety findings are recorded for exit; uncertain once-delivered work is never retried (LIVE-P3 §3.12).

## 7. Close evidence

Link CP-X's cumulative 14-day PASS, succeeded autonomy count, all daily freeze results, missed/unverified days, two counted ratings≥4, both usefulness denominators, availability≥99%, restart recovery<60 s and CI for QA-X X4a/X4b deletion across every class. Zero succeeded autonomy leaves its half NOT EVALUABLE. TL writes milestone/trial reports; owner reviews milestones before `done` (LIVE-P3 §3.13; README §5).
