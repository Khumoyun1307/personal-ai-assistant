# Evaluation: retrieval (retrieval) - gpt-6-luna - 2026-10-26

| | |
|---|---|
| Run date | 2026-10-26 |
| Target / mode | retrieval / variants |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | retrieval_variants.v2, retrieval_answer.v1 |
| Items evaluated | 38 of 38 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Target message in the top 5 results | >= 85 % | 34/38 (89.5 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |
| Correct final answer with reference | >= 80 % | 31/38 (81.6 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |

Failed items, Target message in the top 5 results: r04, r11, r19, r33

Failed items, Correct final answer with reference: r04, r08, r11, r19, r23, r25, r33

### top-5 hit by intent

| Slice | Result |
|---|---|
| cross_language | 5/5 (100.0 %) |
| cross_script | 14/14 (100.0 %) |
| exact | 10/10 (100.0 %) |
| new_alphabet | 2/3 (66.7 %) |
| no_apostrophe | 4/5 (80.0 %) |
| paraphrase | 21/25 (84.0 %) |
| translit | 3/3 (100.0 %) |

## Cost

Cap USD 1; spent $0.0102 over 76 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 76 | 42147 | 11917 | $0.0102 | $0.0268 |

Cost per 100 items is over the 38 items evaluated.

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
