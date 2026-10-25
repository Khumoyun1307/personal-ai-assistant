# Evaluation: retrieval (retrieval) - deterministic - 2026-10-25

| | |
|---|---|
| Run date | 2026-10-25 |
| Target / mode | retrieval / deterministic |
| Model (configured) | none (deterministic, no model) |
| Real model called | no |
| Reasoning effort | - |
| Prompts / rubrics | - |
| Items evaluated | 38 of 38 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1); with none, the gate is *not evaluable*, never a pass.

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Target message in the top 5 results | >= 85 % | 31/38 (81.6 %) (fail) | n/a (no items) | **NOT EVALUABLE** |

Failed items, Target message in the top 5 results: r03, r04, r05, r11, r19, r26, r33

Note, Target message in the top 5 results: Baseline without a model; the S03.4 gate measures retrieval with model-generated query variants (mode `variants`).

### top-5 hit by intent

| Slice | Result |
|---|---|
| cross_language | 2/5 (40.0 %) |
| cross_script | 14/14 (100.0 %) |
| exact | 10/10 (100.0 %) |
| new_alphabet | 2/3 (66.7 %) |
| no_apostrophe | 4/5 (80.0 %) |
| paraphrase | 18/25 (72.0 %) |
| translit | 3/3 (100.0 %) |

## Cost

No model was called: cost USD 0.

## Model-choice decision

Not applicable: a model-free baseline.
