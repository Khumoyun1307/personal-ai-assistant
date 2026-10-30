# Evaluation: retrieval-agent (retrieval) - gpt-6-luna - 2026-10-30

| | |
|---|---|
| Run date | 2026-10-30 |
| Target / mode | retrieval / agent |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | owner_request.v10 |
| Items evaluated | 38 of 38 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `e2/situations.jsonl`: `720a1ab1da281fecdfaa462ab71e107635fec7cc6bbbc54f91d0dd692e2bac2f`
- `e3/bursts.jsonl`: `b8a243680a3e2dbdb7175ae1645f0f8c2cec632eb1d779a6e2abc5aff52711d4`
- `e3/monitor_cases.jsonl`: `5f54aa692f82caf99a5a3c24a5459448455a92878dcb3661d58f3e57fb53bbc0`
- `e4/cases.jsonl`: `68088b0014f098dfe3dff0b30492d973cd23c5dbfe5789d86489110c304659ad`
- `owner_drafting/cases.jsonl`: `709dad6042c3e79f8b9bd0708bb1a91f5df324eda0000f3ceaa6a2e43346de09`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Target message in the top 5 of any search in the agent run | >= 85 % | 31/38 (81.6 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |
| Correct reply with a verified citation of a target | >= 80 % | 31/38 (81.6 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |
| Target message reached the model (any search result or chat read) | >= 85 % | 36/38 (94.7 %) (pass) | n/a (no items) | **INFO** (0 of 38 confirmed) |

Failed items, Target message in the top 5 of any search in the agent run: r03, r04, r05, r11, r24, r26, r30

Failed items, Correct reply with a verified citation of a target: r08, r11, r15, r29, r30, r35, r36

Failed items, Target message reached the model (any search result or chat read): r11, r30

Note, Target message reached the model (any search result or chat read): Pipeline signal next to the gated top 5: it also counts search results past the fifth and messages get_conversation showed, which can hide a ranking regression; the answer metric already scores the pipeline end to end.

### top-5 hit by intent

| Slice | Result |
|---|---|
| cross_language | 2/5 (40.0 %) |
| cross_script | 13/14 (92.9 %) |
| exact | 10/10 (100.0 %) |
| new_alphabet | 2/3 (66.7 %) |
| no_apostrophe | 4/5 (80.0 %) |
| paraphrase | 18/25 (72.0 %) |
| translit | 3/3 (100.0 %) |

## Cost

Cap USD 1; spent $0.0819 over 86 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 86 | 792014 | 5449 | $0.0819 | $0.2156 |

Cost per 100 items is over the 38 items evaluated.

## Notes

- Agent mode: the real owner_request runtime with the shipped prompt, the production release flags and the product's citation step (E-aa).
- 1 item(s) made no search_messages call (a top-5 miss, `no_search`): r30

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
