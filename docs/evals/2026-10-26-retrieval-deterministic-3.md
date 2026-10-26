# Evaluation: retrieval (retrieval) - deterministic - 2026-10-26

| | |
|---|---|
| Run date | 2026-10-26 |
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

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Target message in the top 5 results | >= 85 % | 32/38 (84.2 %) (fail) | n/a (no items) | **INFO** (0 of 38 confirmed) |

Failed items, Target message in the top 5 results: r03, r05, r11, r19, r24, r33

Note, Target message in the top 5 results: Baseline without a model; the S03.4 gate measures retrieval with model-generated query variants (mode `variants`), so 85 % is a pre-screen comparison here, not a gate.

### top-5 hit by intent

| Slice | Result |
|---|---|
| cross_language | 3/5 (60.0 %) |
| cross_script | 13/14 (92.9 %) |
| exact | 10/10 (100.0 %) |
| new_alphabet | 2/3 (66.7 %) |
| no_apostrophe | 4/5 (80.0 %) |
| paraphrase | 19/25 (76.0 %) |
| translit | 3/3 (100.0 %) |

## Cost

No model was called: cost USD 0.

## Model-choice decision

Not applicable: a model-free baseline.
