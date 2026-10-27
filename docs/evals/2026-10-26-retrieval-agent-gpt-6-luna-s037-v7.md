# Evaluation: retrieval-agent (retrieval) - gpt-6-luna - 2026-10-26

> **INCOMPLETE RUN.** 1 item(s) could not be evaluated (the provider stayed unavailable through the retries): r24. They are neither hits nor misses; no gate can pass.

| | |
|---|---|
| Run date | 2026-10-26 |
| Target / mode | retrieval / agent |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | owner_request.v7 |
| Items evaluated | 37 of 38 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Target message in the top 5 of any search in the agent run | >= 85 % | 33/37 (89.2 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |
| Correct reply with a verified citation of a target | >= 80 % | 30/37 (81.1 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |
| Target message reached the model (any search result or chat read) | >= 85 % | 35/37 (94.6 %) (pass) | n/a (no items) | **INFO** (0 of 38 confirmed) |

Failed items, Target message in the top 5 of any search in the agent run: r04, r05, r11, r26

Failed items, Correct reply with a verified citation of a target: r04, r08, r11, r15, r29, r35, r36

Failed items, Target message reached the model (any search result or chat read): r04, r11

Note, Target message reached the model (any search result or chat read): Pipeline signal next to the gated top 5: it also counts search results past the fifth and messages get_conversation showed, which can hide a ranking regression; the answer metric already scores the pipeline end to end.

### top-5 hit by intent

| Slice | Result |
|---|---|
| cross_language | 3/5 (60.0 %) |
| cross_script | 13/13 (100.0 %) |
| exact | 10/10 (100.0 %) |
| new_alphabet | 2/3 (66.7 %) |
| no_apostrophe | 4/5 (80.0 %) |
| paraphrase | 20/24 (83.3 %) |
| translit | 3/3 (100.0 %) |

## Cost

Cap USD 1; spent $0.0616 over 95 calls (4 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 95 | 451711 | 5363 | $0.0616 | $0.1665 |

Cost per 100 items is over the 37 items evaluated.

## Notes

- Agent mode: the real owner_request runtime with the shipped prompt, the production release flags and the product's citation step (E-aa).
- 2 item(s) were run again after a provider outage: r22, r24
- INCOMPLETE: 1 item(s) not evaluated (the provider stayed unavailable through 3 attempts; neither hits nor misses): r24

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
