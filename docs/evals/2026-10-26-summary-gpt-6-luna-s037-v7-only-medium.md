# Evaluation: summary (summary) - gpt-6-luna - 2026-10-26

> **TRIAL RUN.** Only 3 of the set's 40 items were selected (`--limit` or `--only`); a trial is never a gate.

| | |
|---|---|
| Run date | 2026-10-26 |
| Target / mode | summary / period-uz-Latn |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | medium |
| Prompts / rubrics | summarize.v7, fabrication.v2+render.v3+labels.v2, open_loops.v4 |
| Items evaluated | 3 of 3 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Open question/commitment recall | >= 95 % | 4/8 (50.0 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 103 confirmed) |
| Coverage caveat present whenever gaps exist | >= 100 % | n/a (no items) (not evaluable) | n/a (no items) | **INFO** (0 of 6 confirmed) |
| Caveats for gaps that lost nothing | <= 0 | 0 (of 3 items) (pass) | n/a (no items) | **INFO** (0 of 40 confirmed) |
| Owner-facing language and script correct | >= 100 % | 2/3 (66.7 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 40 confirmed) |
| Fabricated facts (zero allowed) | <= 0 | 1 (of 2 items) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 40 confirmed) |

Failed items, Open question/commitment recall: c15.c2, c28.q2, c28.c1, c28.c2

Note, Open question/commitment recall: 2 open loop(s) not matched on `values` (none, or not found literally, e.g. translated): judge pre-screen only, the gate needs the owner's verdict (0 unresolved among the owner-confirmed items)

Note, Coverage caveat present whenever gaps exist: Ungated wiring check: the plan's caveat threshold (the final reply keeps every caveat, D64) is not measured by this target; see E-p.

Note, Caveats for gaps that lost nothing: Ungated wiring check: the plan's caveat threshold (the final reply keeps every caveat, D64) is not measured by this target; see E-p.

Failed items, Owner-facing language and script correct: c28

Failed items, Fabricated facts (zero allowed): c36#ee41dbe6

Note, Fabricated facts (zero allowed): 1 flagged statement(s) pending owner confirmation; not screened (judge failed): c28

### open-loop recall (pre-screen) by variant

| Slice | Result |
|---|---|
| en | 0/3 (0.0 %) |
| uz-Cyrl | 2/3 (66.7 %) |
| uz-ru-mix | 2/2 (100.0 %) |

### owner-language check by variant

| Slice | Result |
|---|---|
| en | 0/1 (0.0 %) |
| uz-Cyrl | 1/1 (100.0 %) |
| uz-ru-mix | 1/1 (100.0 %) |

## Cost

Cap USD 1; spent $0.0081 over 7 calls (1 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 7 | 14384 | 9003 | $0.0081 | $0.2696 |

Cost per 100 items is over the 3 items evaluated.

## Notes

- Caveats come from the product's deterministic renderer over the harness's own synthetic gap rows, not from the model (E-d, E-p): the two caveat metrics are an ungated wiring regression check. Recall and language are measured on the per-chat summary (the summarize output), not on the owner-facing reply.
- 1 conversation(s) failed at the model and count as misses: c28
- 1 summary call(s) were truncated once and retried larger.

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
