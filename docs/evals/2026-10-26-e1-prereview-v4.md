# E1 pre-review of the first summarize.v4 run (agent, 2026-10-26)

*An agent's pre-review, **not** owner-confirmed. It reviewed `2026-10-26-summary-gpt-6-luna-s036-v4.json` and the retrieval runs; the draft ledgers are not committed (coverage verdicts are keyed to that run's text and do not carry over). It led to S03.7 ([../milestones/M03-S03.7-plan.md](../milestones/M03-S03.7-plan.md)). The proposed label changes are applied only after the owner accepts them (S03.7 Task C).*


---

## Summary for the owner

# E1 pre-review: summary for the owner (AGENT PRE-REVIEW, nothing confirmed by you yet)

I read all 40 chats and the 40 summaries of the latest run (summarize.v4, gpt-6-luna, written in uz-Latn) and gave a draft verdict on every item the gates count. Nothing in the repo was changed.

**Real model errors (8 flags I would confirm, in 7 chats):**
- c37: Ergash aka's request "bidonlarni qaytarib bering" is reported as *your* promise (the same error as in every earlier run).
- c17: Shoira's request that the courier call first is reported as *your* promise.
- c36: the summary says 18:00/18:30 was the *old* time and "yangi vaqt ko‘rsatilmagan" - wrong, 18:00/18:30 is the corrected time.
- c07 (x2): the summary uses "aloqa egasi" for both you and the landlord: your "I study until 14:00, I'll leave the key" is given to the landlord, and one sentence reads "Aloqa egasi aloqa egasidan ... kutmoqda".
- c19: "brings the original passport" is listed as your promise; it was only the uncle's reminder (low harm).
- c26: "we'll discuss pets at the viewing" was the landlord's words, reported as the realtor's promise (low harm).
- c28: the charts upload, already done, is listed under "Commitments not yet done".

**Judge over-flagging:** 16 of the 24 flags are supported by the messages (c07, c09, c10, c13, c18, c20, c22 x3, c24, c27, c32, c36, c37, c38 x2). The open-loop judge also marked 9 loops missed while quoting the right line (c06.c2, c23.c1, c28.c1, c28.c2, c38.c1, c40.c1, plus c05.c4, c08.c1, c34.c1, which I also call covered, with less certainty).

**Gates if you accept my draft as-is** (computed with the harness's own code, on this run's summaries only):
- Open-loop recall **100/103 = 97.1 % - PASS**, but narrow: 3 more misses would fail it.
- Owner language **40/40 - PASS**.
- Fabrication **8 - FAIL** (threshold 0; at least 3 of the 8 are unambiguous).
- Retrieval (live run 2): top-5 **34/38 = 89.5 % - PASS**; correct answer **31/38 = 81.6 % - PASS by one item**, scored with the older, looser check, and assuming owner_request changes not yet shipped.
- Caveats: INFO by design.

**Label changes proposed for v1.1:** 10. Four contradict the chat (c11.c2 is a request, not your promise; c07.c2, c02.c2 and c39.c1 are already done). Three are ambiguous (split c01.q3; c38.c1 probably done; reword c05.c4). One is a retrieval target (r19: add c30.m01). Two are optional (c01.c3, c22.d1). See `label-changes-v1.1.md`.

**Where a 1-minute look from you helps most** (my least certain verdicts; each changes a gate number):

1. **c01.q3** - Summary: "Kontakt Nodira opalarga kim xabar berishini so‘radi". Does that count as surfacing m09 "Nodira opalar ham keladimi? Ularga siz aytasizmi yoki men qo‘ng‘iroq qilaymi?" (I said yes.)
2. **c01.c3** - Summary: "Kontakt katta ro‘mol va choynak to‘plamini sovg‘a qilishni **taklif qildi**". Does that tell you Dilnoza will buy them (m07 "olmoqchiman")? (I said no: missed.)
3. **c10.c1** - Summary only says you'll come to football, not "Pulni o‘sha yerda beraman". Is the missing payment enough to call it missed? (I said yes.)
4. **c07.c2** - Your key plan is written with "Aloqa egasi" (the word used for the landlord). Missed, or covered? (I said missed; the label should be "done" anyway.)
5. **c37.c1** - "promised by the owner: **Kontaktdan** oldindan to‘lovni bugun kartasiga o‘tkazadi" - does "Kontaktdan" make it wrong? (I said covered.)
6. **c05.c4** - Timur's "Do pyatnitsy kartamga tashla" reported as his request with "juma kunigacha". Covered? (I said yes.)
7. **c08.c1** - "Yetkazish 14-oktabr 18:00–20:00 oralig‘iga taklif qilingan; kontakt ma’qullagan" - no "who delivers". Covered? (I said yes.)
8. **c28#16625ad7** - a *done* upload listed under "not yet done" (the line itself says uploaded): fabrication, or only a wrong heading? (I said fabrication.)
9. **c19#c06b28cd / c26#a26bdff6** - small attribution slips: confirm as fabrications or dismiss? (I said confirm; the gate fails either way.)
10. **c10#86c68205** - "Egasi kontaktning o‘tgan oy olgan 500 ming qarzini ... qaytarishini aytdi": does "kontaktning ... olgan" read to you as "the contact borrowed"? (I dismissed it.)

Files: `ledger-draft.json` (E1), `ledger-draft-retrieval.json`, `verdict-evidence.md` (the quoted line behind every verdict), `label-changes-v1.1.md`, `gate-preview.md`.

---

## Gate preview

# Gate preview: what the gates would read if the owner accepted the draft ledgers as-is (AGENT PRE-REVIEW)

## How this was computed

- **With the harness's own metric code**, not by hand: `evals.metrics.recall_metric`, `language_metric`, `ratio_metric`, `evals.judge.fabrication_metric`, `MetricResult.gate_status()`, and `evals.review.ReviewLedger.load` (so the draft ledgers also pass the harness's own format check, `extra="forbid"`).
- Code and inputs came from a `git archive` snapshot of `main` (9ed9280) copied into the scratchpad; the script ran in the dev container with only the scratchpad mounted on top (`scratchpad/work/build.py`). No database, no model call, nothing written to the repo or to `evals/reviews/`.
- Inputs: the committed run JSONs (summaries, judge verdicts, flags, retrieval outcomes) + `ledger-draft.json` (E1) and `ledger-draft-retrieval.json` (retrieval).
- Sanity check: with an empty ledger the script reproduces the recorded numbers (recall 89/103, language 40/40, 24 flags pending, every gate NOT EVALUABLE).
- **Limit:** the CLI has no "re-score a recorded run with a ledger" command; gates are computed only during a run. Coverage verdicts are keyed to *this* run's summary text (`loop#digest`) and flag verdicts to the flagged statement, so this ledger gates **only the v4 run's summaries**. A new run (another prompt, model, or E1 v1.1) writes new summaries and needs new verdicts.
- The draft confirms every conversation (c01-c40) and every query (r01-r38). That is the owner's act (it means "I reviewed the text and labels"); it is in the draft only so the preview can show a gate. Under E1 **v1** labels (the label changes are not applied).

## Summary target: `2026-10-26-summary-gpt-6-luna-s036-v4` (summarize.v4, gpt-6-luna, owner language uz-Latn)

| Metric | Threshold | Pre-screen (as recorded) | Gate with the draft ledger | Gate status |
|---|---|---|---|---|
| Open-loop recall | >= 95 % | 89/103 (86.4 %) FAIL | **100/103 (97.1 %)**, 103 of 103 confirmed, 0 pending | **PASS** (narrow) |
| Owner language and script | 100 % | 40/40 PASS | **40/40**, 40 of 40 confirmed | **PASS** |
| Fabricated facts | 0 | 24 flags pending | **8 confirmed** in 7 conversations (c07 x2, c17, c19, c26, c28, c36, c37); 16 dismissed; 40 of 40 confirmed | **FAIL** |
| Caveat presence / spurious caveats | 100 % / 0 | as recorded | ungated by design (E-p) | INFO |

Recall misses in the gate: **c01.c3, c07.c2, c10.c1** (all three are judgment calls, see `owner-summary.md`).

**Recall is close to the threshold.** 98/103 passes, 97/103 fails: two more misses still pass, three fail. Sensitivity run with the same code: if the five loops I marked covered with doubt (c01.q3, c05.c4, c08.c1, c34.c1, c37.c1) were all ruled missed, recall reads **95/103 (92.2 %) - FAIL**. Conversely, if the owner reads c01.c3, c07.c2 and c10.c1 as covered, 103/103.

**Fabrication fails whatever happens to the doubtful flags**: c17#38f53707, c36#9aefd3ff and c37#7a96d6b1 are clear errors on their own (a request reported as the owner's promise twice, and a correct time reported as unknown). Even with the five doubtful confirmations dismissed, 3 > 0.

## Retrieval

| Run | Mode | Metric | Threshold | Gate with the draft ledger (38 of 38 confirmed) | Status |
|---|---|---|---|---|---|
| `2026-10-26-retrieval-gpt-6-luna-s036` (run 2) | variants (live) | top-5 hit | >= 85 % | **34/38 (89.5 %)**; misses r04, r11, r19, r33 | **PASS** |
| same | variants (live) | correct answer with reference | >= 80 % | **31/38 (81.6 %)**; misses r04, r08, r11, r19, r23, r25, r33 | **PASS** (by one item: 30/38 fails) |
| `2026-10-26-retrieval-replay-deterministic` | replay (no model) | top-5 hit | (85 %, comparison only) | 36/38 (94.7 %); misses r11, r19 | INFO (ungated) |
| `2026-10-26-retrieval-deterministic-2` | deterministic | top-5 hit | (85 %, comparison only) | 32/38 (84.2 %); misses r03, r05, r11, r19, r24, r33 | INFO (ungated) |

Caveats on the retrieval gate (from M03 S03.6, re-stated because they change what PASS means):

1. Run 2 scored the answer check with the **older, looser** E-u (any letters after a value, free text included). The review round narrowed it; run 2 recorded no final answers, so it cannot be re-scored. The narrower check can only lower 31/38, and one more failure fails the threshold.
2. Run 2 predates the final search code (the replay says 36/38 on the recorded variants).
3. Both variants numbers assume `owner_request` changes that are **not shipped** (partner name kept out of the search phrases; widen an empty filtered search, E-t). They measure the eval pipeline, not yet the product agent.
4. Label change #8 (r19 target) would need a new run to show any effect.

## Bottom line

If the owner accepts the draft as-is: **recall PASS (narrowly), language PASS, fabrication FAIL (8, at least 3 unambiguous), retrieval top-5 PASS and answer PASS (by one item, on an older scoring rule)**. The M03 summary gate is therefore not met on summarize.v4 + gpt-6-luna; the blocker is fabrication (attribution: requests turned into promises, role words), not recall or language.

---

## Proposed E1 v1.1 label changes

# Proposed label corrections for E1 v1.1 (AGENT PRE-REVIEW - not applied)

Nothing here was edited in `evals/datasets/`. Each change is for the owner to accept or reject in the v1.1 review (bump version, log in `docs/evals/README.md`, regenerate the manifest - `evals/README.md` *Freezing*). None is proposed to make a model pass; four of them (c02.c2, c11.c2, c38.c1, c39.c1) remove loops the v4 summary already **covered**.

Priority: **A** = the label contradicts the chat; **B** = the label is defensible but inconsistent or ambiguous; **C** = optional.

| # | Item | Current label | Proposed label | Reason | Prio |
|---|---|---|---|---|---|
| 1 | c11.c2 | commitment, who=owner, open: "Bring September sales figures, the table printed in 3 copies" | key fact (or `decisions`/request): "Nilufar asked Bekzod to bring the September sales figures, table printed in 3 copies" (values unchanged) | m06 is the manager's instruction; Bekzod never accepted it. The same pattern in c37 (return the cans) is a key fact (c37.f2), and summarize.v4 is told "a request is not a promise". | A |
| 2 | c05.c4 | commitment, who=owner, open: "Transfer his share to Timur's card by Friday", values `Do pyatnitsy` | keep as an owner obligation but reword `what`: "Pay his share to Timur's card by Friday (Timur's terms, m10, after Bekzod asked when to pay, m09)"; or move to key facts like #1 | Bekzod did not promise in words; he asked when to pay (implying he will). The value is Timur's wording. Either way state it as Timur's request so a summary that reports it as a request is not judged missed. | B |
| 3 | c07.c2 | commitment, open: "Leave the key with the neighbour Natalya (Thu 8 Oct)" | status `done` (or drop the loop) | m09 (Fri 9 Oct): "сантехник вчера всё поменял" - the plumber got in on Thursday, so the key was left. | A |
| 4 | c02.c2 | commitment, open: "Take Akmal to the first lesson (Tue 13 Oct 16:30)" | status `done` | m11 (13 Oct 18:15): "Bugungi dars yaxshi o‘tdi" - the lesson happened. Note: with this change the v4 summary's "Commitments not yet done: Bekzod Akmalni birinchi darsga olib borishini aytdi" becomes the same error class as flag c28#16625ad7. | A |
| 5 | c01.q3 | one open question: "Is Nodira opa's family coming, and will Bekzod tell them or should Dilnoza call?" | two open questions: **q3a** "Are Nodira opa's family coming too?" and **q3b** "Will Bekzod tell Nodira opa's family, or should Dilnoza call them?" (both source c01.m09) | One label, two questions: a summary with one half cannot be scored either way (v4 has only q3b). | B |
| 6 | c38.c1 | commitment, open: "Tell the meeting that Kamron will be late (Tue 20 Oct 9:30)" | status `done` (past; it was due at the 9:30 meeting) | m04 (10:30) "Sobranie qanday o‘tdi?" - the meeting is over; Bekzod then reports its outcome (m05). Not explicitly confirmed, hence borderline. | B |
| 7 | c39.c1 | commitment, who=contact, open: "Collect the camera and charger (Sat 17 Oct 20:00)" | status `done` | m05 (18 Oct): "Fotoapparat menda" - he has the camera. | A |
| 8 | r19 (retrieval) | `target_message_ids`: [c30.m02] | [c30.m01, c30.m02] | "when does Daniel land": m01 "I'm **landing** in Tashkent on Tuesday, Oct 27" answers the day (the expected answer quotes that day from m01), m02 the time. Only m02 counting makes a correct day-only hit a miss. This changes a recorded miss into a possible hit; the owner should judge it on the merits, not for the number. | B |
| 9 | c01.c3 | commitment, who=contact, open: "Buy the gift: a large scarf and a tea set" | keep, or reword `what` to "Plans to buy the gift (a large scarf and a tea set)" | m07 "olmoqchiman" is an intention ("I want to buy"), not a firm promise; the reworded label makes the scoring rule explicit. v4 missed it either way (it said "taklif qildi"). | C |
| 10 | c22.d1 | decision `made`: "invoice to the company, 30% prepayment, the rest after delivery" | "invoice to the company and 30% prepayment agreed (m10); 'rest after delivery' proposed by Marina (m07), not explicitly confirmed" | m10 confirms only the invoice and the 30 %. The v4 summary said exactly this; a stricter judge could call it a contradiction of the label. Not a gated item (decisions are not counted). | C |

## Checked and left alone

The other suspects named in M03 S03.6 are all in the table (c11.c2, c05.c4, c07.c2, c02.c2, c01.q3, c38.c1). I also re-read every other open loop against the later messages of its chat (is anything already done, answered or contradicted?) and every retrieval query against its targets (`answer_values` are all literal in a target). No further problems found.

## Effect (illustration only - do not use as a gate)

A v1.1 dataset changes the manifest, so the committed v4 run cannot be re-scored against it; a new summary run is needed, with new summaries and **new** ledger keys. Only to show the direction: applying 1 and 3-7 to the v4 summaries with this pre-review's verdicts moves recall from 100/103 (97.1 %) to 96/99 (97.0 %): c07.c2 (a miss) leaves, four covered loops leave (c11.c2, c02.c2, c38.c1, c39.c1), and the c01.q3 split adds one miss (q3a) and keeps one covered (q3b). If #2 also moves c05.c4 to key facts: 95/98 (96.9 %). The misses left are c01.c3, c10.c1 and c01.q3a.
