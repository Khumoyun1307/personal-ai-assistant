# Evaluation: retrieval-agent (retrieval) - gpt-6-luna - 2026-10-26

| | |
|---|---|
| Run date | 2026-10-26 |
| Target / mode | retrieval / agent |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | owner_request.v6 |
| Items evaluated | 38 of 38 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Target message in the top 5 of any search in the agent run | >= 85 % | 20/38 (52.6 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |
| Correct reply with a verified citation of a target | >= 80 % | 18/38 (47.4 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |

Failed items, Target message in the top 5 of any search in the agent run: r04, r05, r06, r08, r10, r14, r15, r16, r17, r18, r20, r21, r24, r25, r29, r30, r35, r37

Failed items, Correct reply with a verified citation of a target: r05, r06, r08, r10, r14, r15, r16, r17, r18, r19, r21, r24, r25, r26, r29, r30, r31, r32, r35, r37

### top-5 hit by intent

| Slice | Result |
|---|---|
| cross_language | 4/5 (80.0 %) |
| cross_script | 7/14 (50.0 %) |
| exact | 3/10 (30.0 %) |
| new_alphabet | 2/3 (66.7 %) |
| no_apostrophe | 2/5 (40.0 %) |
| paraphrase | 16/25 (64.0 %) |
| translit | 0/3 (0.0 %) |

## Cost

Cap USD 1; spent $0.0481 over 77 calls (5 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 77 | 304651 | 4012 | $0.0481 | $0.1266 |

Cost per 100 items is over the 38 items evaluated.

## Notes

- Agent mode: the real owner_request runtime with the shipped prompt, the production release flags and the product's citation step (E-aa).
- 14 item(s) made no search_messages call (a top-5 miss, `no_search`): r06, r08, r10, r14, r15, r16, r17, r18, r21, r25, r29, r30, r35, r37
- 5 run(s) did not succeed and count as misses: r14, r15, r16, r17, r18

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
