# Evaluation: retrieval-agent (retrieval) - gpt-6-luna - 2026-10-26

> **TRIAL RUN.** Only 1 of the set's 38 items were selected (`--limit`); a trial is never a gate.

| | |
|---|---|
| Run date | 2026-10-26 |
| Target / mode | retrieval / agent |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | owner_request.v6 |
| Items evaluated | 1 of 1 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Target message in the top 5 of any search in the agent run | >= 85 % | 1/1 (100.0 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |
| Correct reply with a verified citation of a target | >= 80 % | 1/1 (100.0 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |

### top-5 hit by intent

| Slice | Result |
|---|---|
| exact | 1/1 (100.0 %) |

## Cost

Cap USD 1; spent $0.0011 over 2 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 2 | 9751 | 173 | $0.0011 | $0.1062 |

Cost per 100 items is over the 1 items evaluated.

## Notes

- Agent mode: the real owner_request runtime with the shipped prompt, the production release flags and the product's citation step (E-aa).

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
