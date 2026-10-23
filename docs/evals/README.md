# Evaluation results

This folder records the results of real-model evaluation runs (IMPLEMENTATION_PLAN
§10, §10.1). The datasets, schema and runner live in [`evals/`](../../evals/README.md).
Unit tests never call a real model; the runs recorded here are manual.

## Naming

Each run gets one file: `<YYYY-MM-DD>-<set>-<model>.md`. For example:
`2026-10-30-e1-gpt-6-luna.md`, `2026-10-30-retrieval-gpt-6-luna.md`. A rerun on
the same day adds a suffix (`…-luna-2.md`).

Each report states:

- the dataset and its version (for example E1 v1.1, with the manifest hash);
- the model and route, the prompt version, and the run date;
- each metric against its threshold (S03.4: question/commitment recall ≥ 95 %,
  zero fabricated facts, a coverage caveat whenever gaps exist, owner-facing
  language correct 100 %; retrieval top-5 ≥ 85 %, correct answer with reference
  ≥ 80 %);
- the cost (USD, from `llm_calls`) and the cost per 100 conversations;
- which items the owner has confirmed. Gate results count only owner-confirmed
  items (§10.1).
- a model-choice decision, when one is made (§10.1 model-choice rule: a route
  moves up from `gpt-6-luna` only when it misses a threshold).

Reports contain synthetic data only. Quote dataset item ids (`c05.c3`,
`r17`), not chat content beyond what the synthetic set holds.

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

## Dataset log

| Date | Set | Version | Change | Manifest |
|---|---|---|---|---|
| 2026-10-23 | E1 + retrieval | v1 | Initial synthetic set: 40 conversations (8 per variant; c01–c08 = S00.3 smoke set), 262 labelled items, 38 retrieval queries. Hand-written by an AI coding agent; pending the owner's Uzbek and label review. | `evals/datasets/MANIFEST.sha256` at this commit |
