# Evaluation: summary (summary) - gpt-6-luna - 2026-10-26

| | |
|---|---|
| Run date | 2026-10-26 |
| Target / mode | summary / period-uz-Latn |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | summarize.v2, fabrication.v1, open_loops.v1 |
| Items evaluated | 40 of 40 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Open question/commitment recall | >= 95 % | 70/103 (68.0 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 103 confirmed) |
| Coverage caveat present whenever gaps exist | >= 100 % | 6/6 (100.0 %) (pass) | n/a (no items) | **INFO** (0 of 6 confirmed) |
| Caveats for gaps that lost nothing | <= 0 | 0 (of 40 items) (pass) | n/a (no items) | **INFO** (0 of 40 confirmed) |
| Owner-facing language and script correct | >= 100 % | 31/40 (77.5 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 40 confirmed) |
| Fabricated facts (zero allowed) | <= 0 | 15 (of 40 items) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 40 confirmed) |

Failed items, Open question/commitment recall: c01.q3, c01.c1, c02.q1, c02.c1, c05.c1, c05.c4, c06.c1, c07.c1, c07.c2, c08.c1, c11.c2, c13.q1, c15.c1, c19.c1, c20.c3, c21.c1, c22.c2, c22.c3, c23.c1, c26.c1, c27.c1, c28.q2, c28.c1, c28.c2, c29.c1, c31.c3, c33.c1, c34.c1, c36.q2, c38.q1, c38.q3, c38.c1, c38.c3

Note, Open question/commitment recall: 42 open loop(s) have no `values` to match: judge pre-screen only, the gate needs the owner's verdict (0 unresolved among the owner-confirmed items)

Note, Coverage caveat present whenever gaps exist: Ungated wiring check: the plan's caveat threshold (the final reply keeps every caveat, D64) is not measured by this target; see E-p.

Note, Caveats for gaps that lost nothing: Ungated wiring check: the plan's caveat threshold (the final reply keeps every caveat, D64) is not measured by this target; see E-p.

Failed items, Owner-facing language and script correct: c03, c04, c16, c17, c18, c19, c20, c25, c40

Failed items, Fabricated facts (zero allowed): c02#8cf4209a, c04#56d82aed, c08#08fdda25, c11#07588f42, c17#668143f9, c17#65e2d3f8, c22#3d770c0d, c22#ec00584b, c24#8b59df05, c27#5740af28, c33#4447fda4, c35#4419fde7, c37#49a22171, c37#40cef39b, c39#2fb24ffc

Note, Fabricated facts (zero allowed): 15 flagged statement(s) pending owner confirmation

### open-loop recall (pre-screen) by variant

| Slice | Result |
|---|---|
| en | 11/19 (57.9 %) |
| ru | 10/18 (55.6 %) |
| uz-Cyrl | 17/20 (85.0 %) |
| uz-Latn | 17/23 (73.9 %) |
| uz-ru-mix | 15/23 (65.2 %) |

### owner-language check by variant

| Slice | Result |
|---|---|
| en | 8/8 (100.0 %) |
| ru | 7/8 (87.5 %) |
| uz-Cyrl | 1/8 (12.5 %) |
| uz-Latn | 8/8 (100.0 %) |
| uz-ru-mix | 7/8 (87.5 %) |

## Cost

Cap USD 1; spent $0.0330 over 110 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 110 | 142781 | 37367 | $0.0330 | $0.0824 |

Cost per 100 items is over the 40 items evaluated.

## Notes

- Caveats come from the product's deterministic renderer over the harness's own synthetic gap rows, not from the model (E-d, E-p): the two caveat metrics are an ungated wiring regression check. Recall and language are measured on the per-chat summary (the summarize output), not on the owner-facing reply.

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
