# M07 live checklist — J7, reminders, export and review

*Owner: `SQ` prepares/records; `TL` runs server commands; owner performs all client actions and content checks. [LIVE-P3 §3.6–3.10](../specs/LIVE-P3.md) is the source. Not executed by QA-P3.*

## 0. Preconditions

Use Alviro only, in named approved windows, owner present. Green twins must be on deployed `main`. Follow [M06 §0](live-checklist-M06.md#0-preconditions-and-evidence), including cumulative `release --expect-flags`, language expectations from enable 4, notice guards, safe evidence and time restrictions. Commands below belong to TL; placeholders never contain public production identifiers. Record PASS/FAIL/NOT RUN/NOT EVALUABLE with [evidence-template](evidence-template.md) the same day (LIVE-P3 §3.1–3.4).

Enable 1 supplies reminders/tasks; enable 2 follow-ups/nudges. J7 nudges are owner-approved `agent_proposal` actions, never `delegation_auto`, even if the chat later has a delegation. Use the second-account chat after shadow ends and keep it undelegated throughout J7. Do not send any extra test text during the silence leg (LIVE-P3 §3.7; QA-X X1a).

## 1. J7 — c → b → a

Times are owner-local. Owner requests deterministic media expectations, not semantic matching. Use synthetic documents/photos only. All follow-ups need the owner's confirmation card ([QA-S7 §3.4](../specs/QA-S7.md#34-s072-conditional-follow-ups-and-j7-test_m07_s072_j7_followupspy); IP §9 S07.2, OD-33).

| # / source | Account / client step | Expected evidence | Twin |
|---|---|---|---|
| J7-1, D+6 10:00 | Second account → main business chat: `P3-7001 I will send the synthetic signed document tomorrow by eleven.` Owner in control chat creates F-c for that commitment, document expected, deadline D+7 11:00, nudge requested; confirms | Open contact commitment task; active follow_up automation with the correct deadline | H1; creation path |
| Silence | No second-account message, document or manual owner reply in this chat until leg c is recorded. No shadow/testing, typing probe or reopen check here | Record last contact UTC time and planned deadline; ≥24 h at leg c | LIVE-P3 §3.2/§3.7 |
| J7-c, D+7 11:00 | No client message. TL observes deadline after ≥24 h since last contact message | outcome=missed; missed notice sent; nudge blocked, copy_ready=yes, sent=no; zero approved/autonomous sends in this chat. Owner checks copy-ready display (yes/no), does not attempt a send | H4; X1a approval boundary |
| J7-2, D+7 afternoon | After c evidence is saved, second account: `P3-7002 I will send the synthetic photo in ten minutes.` Owner creates F-b with photo expected, deadline +15 min, nudge requested; confirms. Send nothing until deadline | Missed notice; nudge awaiting_approval; no AI line/disclosure flag; no business send yet | H2 |
| J7-b approval | Owner Approve (preferred to cover the send leg) or skip, recorded | Approve: succeeded once, sent=yes, origin=agent_proposal, trigger=F-b, disclosure=no; P2 approved_before_claim PASS. Skip: still pending, never reported sent; approval leg remains NOT RUN | H2; X3b |
| J7-3 | Second account: `P3-7003 I will send the synthetic document tomorrow before ten.` Owner creates F-a, document expected, deadline D+8 10:00; confirms | F-a active | H1 creation |
| J7-a, D+8 before 10:00 | Second account sends synthetic document with caption `P3-7004 Here is the synthetic document I promised earlier.` | outcome=met, after_deadline=no; task done/follow_up_met; one met notice with Reopen; no nudge | H1/H3 |
| Deadline a | TL checks again after 10:00 without a new message | No missed notice or nudge for F-a; no duplicate closure | H1/H8 |

Sources: [LIVE-P3 §3.7 J7-1/c/2/3/a](../specs/LIVE-P3.md#37-j7-in-three-legs-second-account-follow-ups-and-nudges-as-approval-cards-only-od-18), [QA-S7 §4](../specs/QA-S7.md#4-named-tests), [QA-X §4](../specs/QA-X.md#4-named-tests). A late-arrival correction, Reopen and duplicate closure are deterministic scenario checks H5/H7/H8; do not silently count them as additional live legs.

Commands for TL at each leg:

```text
just prod <ip> p3-verify followups --since <J7-start-UTC>
just prod <ip> p3-verify automations --since <J7-start-UTC> --kind follow_up
just prod <ip> p3-verify script P3-70 --expect <sent-contact-numbers> --direction contact --answers
just prod <ip> p3-verify trial-check --since <J7-start-UTC> --until <check-UTC>
```

At c expect only marker 7001; after b add 7002; after commitment a add 7003; after document a add 7004. Empty/missing output is not a pass. Record aliases for task, automation and nudge, states, reasons, notice counts and dates. A failed/incomplete c leg is never repaired by sending in that chat: retain the failure and schedule a fresh ≥24 h silence interval (LIVE-P3 §3.2, §3.7).

## 2. Reminders across a restart — D+6

One approved stop/start window, outside 20:00–22:00 owner-local. The script intentionally crosses a due time; do not substitute a quick restart that never crosses it ([LIVE-P3 §3.6](../specs/LIVE-P3.md#36-reminders-across-a-restart-d6-about-14501530)).

| # | Account / action | Expected | Twin |
|---|---|---|---|
| R1a, about 14:50 | Main control chat: create synthetic reminders for 15:00 and 16:00 | Both persisted with correct owner-local times | G4/G1 |
| R1b, 14:55 | TL `just prod <ip> app-stop` | Stop time recorded, separate from due time | G4 |
| R3, optional | F1, undelegated chat: `P3-7101 This synthetic message was sent during the planned restart.` Second account stays silent | Ingested after start, no message lost; marker 7101 present | Existing durable-ingestion tests |
| R1c, 15:25 | TL `just prod <ip> app-start` | 15:00 occurrence delivered once, late=yes; delay about 1500–1700 s; owner says late note and phone receipt seen | G4 |
| R2, 16:00 | Owner observes phone; TL checks reminders | Second occurrence late=no; delay <60 s; owner says phone receipt at 16:00 | G1/G8 |

TL runs `just prod <ip> p3-verify reminders --since <today-UTC>` and cumulative trial-check. If R3 ran, also `p3-verify script P3-71 --expect 7101 --direction contact --answers`. Record scheduled/delivered UTC and local times, late flag, delay, counts, phone yes/no, approved stop/start times. Do not claim a crash-after-claim test from this planned stop; deterministic execution-interruption tests carry RG-05 (LIVE-P3 §3.6; QA-S7 G4).

## 3. Enable 5, export and memory — D+8

After leg a and clean enable 4/J5, owner enables `delegations.standing`, `memory.extract`, `export.enabled`; TL pushes/restarts and runs release with cumulative flags/languages plus trial-check. The memory notice guard must prove delivery ≥24 h earlier. Do not begin the soak before any needed deploy #2 (README §6; LIVE-P3 §3.9 E5-1).

| # | Owner's client steps | Expected content-free evidence | Twin |
|---|---|---|---|
| X-1 | Main control chat `/export`; read warning that files stay in Telegram history; Confirm. Owner alone receives, opens and inspects parts/manifest | final=data_exported, complete=yes; part count, byte totals, class count, per-part delivered/failed/uncertain/not-attempted counts; owner “opens and looks complete” yes/no | N1/N2/N4 |
| M-1a | In a consented test chat, conduct a synthetic conversation with safe, factual work/schedule/logistics information; use fresh P3-72 markers allocated in the brief | ≥3 suggestions created; ≤5 review cards per owner-local day; dropped_sensitive count recorded. A model producing fewer than three leaves taps pending | K5/K7 |
| M-1b | Owner approves one suggestion, edits one to a new synthetic factual sentence and saves, discards one | approved=1, edited=1, discarded=1; owner yes/no counts match; no pending suggestion used before review | K1/K5 |

Sources: [LIVE-P3 §3.9 X-1/M-1](../specs/LIVE-P3.md#39-enable-5-export-memory-review-ops-rt3-d8), [QA-S7 §3.6–3.7](../specs/QA-S7.md), [decisions §B10 P3-T162](../decisions.md). An edited suggestion keeps `agent_inferred`, scope/category/provenance; owner replacement text skips SENS. Live evidence records this only as owner taps/counts; detailed storage behaviour stays in the unit tests.

TL runs:

```text
just prod <ip> p3-verify exports --since <today-UTC>
just prod <ip> p3-verify report --since <today-UTC> --until <check-UTC>
just prod <ip> p3-verify script P3-72 --expect <sent-contact-numbers> --direction contact --answers
```

No agent downloads, opens, parses, hashes or asks to see an export. Owner's yes/no is separate from transport completeness. No file names or exported values enter evidence. Failed/uncertain parts are reported honestly and never replayed; a partial export is not complete. If safe suggestions/cards are insufficient, record NOT RUN/NOT EVALUABLE and finish in a later window within the daily cap (LIVE-P3 §3.2, §3.9; QA-S7 N4/K7).

## 4. Restore, soak and handoff

1. After enable 5, owner confirms one standing delegation for one informed test contact (F1/F2 or second account, now free after J7). Record contact-informed yes/no, not identity (LIVE-P3 §3.10).
2. With a separate go-ahead away from the digest, TL takes a backup **after** the delegation is active. Owner supplies the offline key where the restore tool expects it. TL runs `just prod-backup <ip>` then `just prod-restore-test <ip>`. Owner removes the key afterwards; no agent sees it (LIVE-P3 §3.9 RT-1).
3. Expect scratch-copy P3 restore lines: active-at-backup count≥1, all those delegations ended/restored_from_backup, pending autonomous sends cancelled, autonomy paused, nothing resumed. Missing lines or zero active count make the delegation half NOT EVALUABLE; repeat with a suitable backup. Production state is not the scratch state (QA-X X5a–X5c; LIVE-P3 RT-1).
4. Start the ≥3-day standing soak only after needed fixes/deploys. Daily 09:30 owner-local release plus cumulative trial-check, at least one succeeded standing reply, recorder≥5 days, CP-L complete and freeze recorded before T0. Use [trial-log-template](trial-log-template.md) (LIVE-P3 §3.10–3.13).

CP-L remains open until every required live leg has evidence. A skipped nudge approval does not prove H2's live send path. Any safety FAIL follows [LIVE-P3 §3.12](../specs/LIVE-P3.md#312-stop-the-line-rule); no agent sends to a contact to fill an evidence gap.
