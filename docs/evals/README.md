# Evaluation results

This folder records the results of real-model evaluation runs (IMPLEMENTATION_PLAN
§10, §10.1). The datasets, schema and runner live in [`evals/`](../../evals/README.md).
Unit tests never call a real model; the runs recorded here are manual.

## How results are recorded

`python -m evals run …` (see [`evals/README.md`](../../evals/README.md) *Running*)
writes both files itself; nobody hand-edits a result. Each run produces a Markdown
report and a `.json` with the same name next to it (machine-readable: metrics with
pre-screen and gate status, failed item ids, per-item records, cost per model and
per 100 items). `python -m evals compare <run>.json …` turns several JSON files
into one quality-and-cost table, which is what the model-choice rule needs.

Two kinds of result live here:

- **Deterministic runs** (`…-<set>-deterministic.md`): no model, no cost. Baselines
  and regression checks of the search service. They are *pre-screen* results.
- **Real-model runs** (`…-<set>-<model>.md`): manual, with the owner's go-ahead and
  cost approval, counted against the USD 1 total below. A model run that stopped
  at its budget cap is marked incomplete and cannot pass any gate.

Every metric is recorded over all items (pre-screen) and over owner-confirmed items
(gate). The gate reads `not evaluable` until the owner has confirmed **every item of
the set** (and ruled on pending judge flags); a `--limit` trial (file name ends
`-trial`) or a run stopped at its cap can never pass; each report shows "k of N
confirmed" beside the gate. A report is evidence for the owner's decision, not the
decision. The model-choice decision itself is written down by the owner after
comparing runs.

## Naming

Each run gets one file: `<YYYY-MM-DD>-<set>-<model>.md`. For example:
`2026-10-30-summary-gpt-6-luna.md`, `2026-10-30-retrieval-gpt-6-luna.md`,
`2026-10-25-retrieval-deterministic.md`. A rerun on the same day adds a suffix
(`…-luna-2.md`; the S03.6 tuning runs use the slice and prompt instead,
`…-luna-s036.md`, `…-luna-s036-v3.md`). The summary run is always a model run; its report names the owner
language the summaries were written in (`mode`, e.g. `period-uz-Latn`) and the
prompt and rubric versions used (`summarize.v1`, `fabrication.v1`, `open_loops.v1`).

Each report states:

- the dataset and its version (for example E1 v1.1, with the manifest hash);
- the model and route, the prompt version, and the run date;
- each metric against its threshold (S03.4: question/commitment recall ≥ 95 %,
  zero fabricated facts, a coverage caveat whenever gaps exist, owner-facing
  language correct 100 %; retrieval top-5 ≥ 85 %, correct answer with reference
  ≥ 80 %);
- the cost (USD, from the run's metered call ledger: calls, tokens and USD per
  model) and the cost per 100 items;
- which items the owner has confirmed. Gate results count only owner-confirmed
  items (§10.1).
- a model-choice decision, when one is made (§10.1 model-choice rule: a route
  moves up from `gpt-6-luna` only when it misses a threshold).

Reports contain synthetic data only. Quote dataset item ids (`c05.c3`,
`r17`), not chat content beyond what the synthetic set holds. The Markdown report
holds ids, numbers and statuses. The **summary** run's JSON also keeps, per
conversation, the model-written summary text and the statements the fabrication judge
flagged (model output over synthetic chats), because the owner cannot rule on a flag,
on a loop without `values` or on a judge negative without reading them (M03 E-o).

## Budget

S03.4 real-model evaluation runs are **capped at USD 1 in total** (owner approval,
2026-10-21; IMPLEMENTATION_PLAN §1.4). Any run beyond the cap, including runs
of `gpt-6-sol` or `gpt-6-astra` after a Luna miss, needs a new owner approval
first.

## Owner review step

The owner, a native Uzbek speaker, reviews the synthetic Uzbek text (Latin and
Cyrillic) and all labels before the gate run. Expect about 4 hours per set
(§13). Corrections become **v1.1** of the dataset, with a new
`evals/datasets/MANIFEST.sha256`, before the M03 gate run. The gate run uses
the reviewed version only.

The summary target (`python -m evals run summary`) is wired and tested on `FakeLLM`.
The first real-model runs (2026-10-26, `gpt-6-luna`, owner-approved) were made
**before** this review, as a pre-screen to find weaknesses (M03 S03.6): every gate in
them reads `NOT EVALUABLE`. The gate run uses the reviewed v1.1 and a review ledger.
The summary JSON also keeps the labelled summary (who promised what) and the open-loop
judge's quoted evidence per loop; the retrieval JSON keeps what was searched, the
contact filter and the final answer (synthetic data, for review; E-s, E-t, E-u).

## Dataset log

| Date | Set | Version | Change | Manifest |
|---|---|---|---|---|
| 2026-10-23 | E1 + retrieval | v1 | Initial synthetic set: 40 conversations (8 per variant; c01–c08 = S00.3 smoke set), 262 labelled items, 38 retrieval queries. Hand-written by an AI coding agent; pending the owner's Uzbek and label review. | `evals/datasets/MANIFEST.sha256` at this commit |

## Real-model runs so far (pre-screen only)

| Date | Run | Prompts / rubrics | Cost (USD) |
|---|---|---|---|
| 2026-10-26 | `summary-gpt-6-luna-trial`, `summary-gpt-6-luna` (baseline) | summarize.v2, fabrication.v1, open_loops.v1 | 0.0011 + 0.0330 |
| 2026-10-26 | `retrieval-gpt-6-luna` (baseline) | retrieval_variants.v1, retrieval_answer.v1 | 0.0094 |
| 2026-10-26 | `retrieval-gpt-6-luna-s036-trial`, `retrieval-gpt-6-luna-s036` (S03.6) | retrieval_variants.v2, retrieval_answer.v1 | 0.0003 + 0.0102 |
| 2026-10-26 | `summary-gpt-6-luna-s036-v3-trial`, `summary-gpt-6-luna-s036-v3` (S03.6) | summarize.v3, fabrication.v1, open_loops.v2 | 0.0011 + 0.0359 |

The S03.4/S03.6 total is tracked against the owner's USD 1 here and in M03.md S03.6.
