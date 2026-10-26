# Evaluation: summary (summary) - gpt-6-luna - 2026-10-26

| | |
|---|---|
| Run date | 2026-10-26 |
| Target / mode | summary / period-uz-Latn |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | summarize.v7, fabrication.v2+render.v3+labels.v2, open_loops.v4 |
| Items evaluated | 40 of 40 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Open question/commitment recall | >= 95 % | 89/103 (86.4 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 103 confirmed) |
| Coverage caveat present whenever gaps exist | >= 100 % | 6/6 (100.0 %) (pass) | n/a (no items) | **INFO** (0 of 6 confirmed) |
| Caveats for gaps that lost nothing | <= 0 | 0 (of 40 items) (pass) | n/a (no items) | **INFO** (0 of 40 confirmed) |
| Owner-facing language and script correct | >= 100 % | 40/40 (100.0 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 40 confirmed) |
| Fabricated facts (zero allowed) | <= 0 | 14 (of 40 items) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 40 confirmed) |

Failed items, Open question/commitment recall: c01.q3, c01.c3, c02.q1, c05.c4, c12.q2, c19.c1, c21.c1, c28.q2, c31.c3, c37.c1, c38.c1, c39.c1, c39.c2, c40.c1

Note, Open question/commitment recall: 67 open loop(s) not matched on `values` (none, or not found literally, e.g. translated): judge pre-screen only, the gate needs the owner's verdict (0 unresolved among the owner-confirmed items)

Note, Coverage caveat present whenever gaps exist: Ungated wiring check: the plan's caveat threshold (the final reply keeps every caveat, D64) is not measured by this target; see E-p.

Note, Caveats for gaps that lost nothing: Ungated wiring check: the plan's caveat threshold (the final reply keeps every caveat, D64) is not measured by this target; see E-p.

Failed items, Fabricated facts (zero allowed): c01#48806bd7, c04#9e912335, c05#520cbae2, c08#e93242dd, c13#17b7365e, c13#a63fefbf, c16#6bb3bae3, c21#850e8b4d, c22#2109eba0, c27#af952b64, c28#cde38c46, c30#5515e0b4, c36#49c049e9, c37#ec777df5

Note, Fabricated facts (zero allowed): 14 flagged statement(s) pending owner confirmation

### open-loop recall (pre-screen) by variant

| Slice | Result |
|---|---|
| en | 17/19 (89.5 %) |
| ru | 17/18 (94.4 %) |
| uz-Cyrl | 19/20 (95.0 %) |
| uz-Latn | 19/23 (82.6 %) |
| uz-ru-mix | 17/23 (73.9 %) |

### owner-language check by variant

| Slice | Result |
|---|---|
| en | 8/8 (100.0 %) |
| ru | 8/8 (100.0 %) |
| uz-Cyrl | 8/8 (100.0 %) |
| uz-Latn | 8/8 (100.0 %) |
| uz-ru-mix | 8/8 (100.0 %) |

## Cost

Cap USD 1; spent $0.0506 over 116 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 116 | 238770 | 53347 | $0.0506 | $0.1264 |

Cost per 100 items is over the 40 items evaluated.

## Notes

- Caveats come from the product's deterministic renderer over the harness's own synthetic gap rows, not from the model (E-d, E-p): the two caveat metrics are an ungated wiring regression check. Recall and language are measured on the per-chat summary (the summarize output), not on the owner-facing reply.

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
