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
| 2026-10-26 | `summary-gpt-6-luna-s036-v4-trial`, `…-s036-v4-aborted.json` (run lost its database, no results), `summary-gpt-6-luna-s036-v4` (S03.6) | summarize.v4, fabrication.v1 (the report says so; its judge read **local** times, which later reports label `fabrication.v1+render.v2`), open_loops.v3 | 0.0011 + 0.0210 + 0.0392 |

| 2026-10-26 | `summary-gpt-6-luna-s037-v5-trial`, `summary-gpt-6-luna-s037-v5` (S03.7) | summarize.v5, fabrication.v1+render.v2+labels.v2, open_loops.v3 | 0.0014 + 0.0463 |
| 2026-10-26 | `retrieval-agent-gpt-6-luna-s037-v6-trial`, `retrieval-agent-gpt-6-luna-s037-v6` (S03.7, agent mode) | owner_request.v6 (the shipped runtime, E-aa) | 0.0011 + 0.0481 |
| 2026-10-26 | `summary-gpt-6-luna-s037-v6-trial`, `…-s037-v6-aborted.json` (the checkout changed branch under the run, no results), `summary-gpt-6-luna-s037-v6` (S03.7) | summarize.v6, fabrication.v2+render.v3+labels.v2, open_loops.v4 | 0.0016 + 0.0110 + 0.0474 |
| 2026-10-26 | `summary-gpt-6-luna-s037-v7-only-low`, `summary-gpt-6-luna-s037-v7-only-medium` (S03.7, `--only c36,c28,c15` effort trials, never a gate) | summarize.v7, fabrication.v2+render.v3+labels.v2, open_loops.v4; effort low / medium | 0.0037 + 0.0081 |
| 2026-10-26 | `summary-gpt-6-luna-s037-v7` (S03.7) | summarize.v7, fabrication.v2+render.v3+labels.v2, open_loops.v4 | 0.0506 |
| 2026-10-26 | `retrieval-agent-gpt-6-luna-s037-v7-trial`, `retrieval-agent-gpt-6-luna-s037-v7` (S03.7, agent mode; **incomplete**: r24 not evaluated after 3 provider outages, E-ag) | owner_request.v7 | 0.0011 + 0.0616 |
| 2026-10-28 | `retrieval-agent-gpt-6-luna-trial`, `retrieval-agent-gpt-6-luna` (P2, M04 re-run after the `owner_request` bump, agent mode) | owner_request.v8 | 0.0014 + 0.0568 |
| 2026-10-29 | `e2-drafts-gpt-6-luna-trial`, `e2-drafts-gpt-6-luna` (P2, M04 E2 baseline, informational) | draft_reply.v1, draft_intent.v1 | 0.0001 + 0.0602 |
| 2026-10-29 | `triage-gpt-6-luna-trial`, `triage-gpt-6-luna` (P2, M05 E3) | triage.v1 | 0.0004 + 0.0539 |
| 2026-10-29 | `monitors-gpt-6-luna-trial`, `monitors-gpt-6-luna-aborted.json` (the shared dev database was recreated under the run by another worktree's compose command, no results), `monitors-gpt-6-luna` (P2, M05 E3) | evaluate_monitor.v1 | 0.0001 + 0.0018 + 0.0030 |
| 2026-10-29 | `retrieval-agent-gpt-6-luna-trial` (failed: 400 `invalid_function_parameters` from `create_monitor`'s strict tool schema, fixed by #141), `retrieval-agent-gpt-6-luna-trial-2`, `retrieval-agent-gpt-6-luna` (run 1), `retrieval-agent-gpt-6-luna-2` (replicate) (P2, M05 re-run after the `owner_request` bump, agent mode) | owner_request.v9 | 0.0000 + 0.0016 + 0.0703 + 0.0693 |

Total so far: USD 0.4343 of the owner's USD 1 (baseline 0.0435, S03.6 0.1088, S03.7 0.2820 — the sum of `cost.spent_usd` in the S03.7 reports), tracked here and in M03.md S03.6 and S03.7.

P2 so far: USD 0.3190 of the phase cap of USD 2 (P2-O8): 2026-10-28 0.0582, 2026-10-29 0.2608 (the sum of `cost.spent_usd` in each day's reports), tracked here and in M04.md and M05.md. Not in the table on 2026-10-29: a debug re-run of the failing retrieval trial (0 model calls, USD 0; its report was lost because Git Bash path conversion mis-mapped its `--out-dir`, and the stray copy was deleted) and four schema-diagnosis API calls with the synthetic input "hi" (under USD 0.001 together).

The fabrication judge reads the conversation as `evals/render.py` draws it. Reports name it
`fabrication.v1` up to the summarize.v4 runs and `fabrication.v1+render.v2` from the S03.6
review on; the baseline and summarize.v3 runs' judge read **UTC** times (render v1), the
summarize.v4 runs' judge **local** times (render v2), though their reports say `fabrication.v1`.
From S03.7 the label also names the attribution wording the judges read (`labels.v2`, D98), and
from summarize.v6 the rubric (`fabrication.v2`: "you" is the owner) and the rendering (`render.v3`:
edit times) changed (E-ae); counts under different labels are not one series.

## Model-free runs (no cost)

| Date | Run | What |
|---|---|---|
| 2026-10-25 | `retrieval-deterministic` (S03.4) | deterministic baseline, 31/38 |
| 2026-10-26 | `retrieval-deterministic` (S03.6) | after the S03.6 search tuning, 32/38 |
| 2026-10-26 | `retrieval-deterministic-2` (S03.6 review) | after the review's stopword and stem changes, 32/38 (same misses) |
| 2026-10-26 | `retrieval-replay-deterministic` (S03.6 review) | replay of `retrieval-gpt-6-luna-s036` (run 2) with the current search code, 36/38 |
| 2026-10-26 | `retrieval-deterministic-3` (S03.7) | with the product's widening (D100) and Russian case siblings (D101), 32/38 |
| 2026-10-26 | `retrieval-replay-deterministic-2` (S03.7) | replay of run 2 through the product's widening (E-z), 36/38 |
