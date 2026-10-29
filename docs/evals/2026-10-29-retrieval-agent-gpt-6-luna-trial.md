# Evaluation: retrieval-agent (retrieval) - gpt-6-luna - 2026-10-29

> **TRIAL RUN.** Only 1 of the set's 38 items were selected (`--limit` or `--only`); a trial is never a gate.

| | |
|---|---|
| Run date | 2026-10-29 |
| Target / mode | retrieval / agent |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | owner_request.v9 |
| Items evaluated | 1 of 1 |
| Owner review ledger | none (no item is owner-confirmed) |

Dataset files (SHA-256, verified against `evals/datasets/MANIFEST.sha256` before the run; the version is in the dataset log of `docs/evals/README.md`):

- `e1/conversations.jsonl`: `a3e632af9fdcd323cedbc8a24191f2a57f734d80152424d4e658a34c53180b5a`
- `e2/situations.jsonl`: `720a1ab1da281fecdfaa462ab71e107635fec7cc6bbbc54f91d0dd692e2bac2f`
- `e3/bursts.jsonl`: `b8a243680a3e2dbdb7175ae1645f0f8c2cec632eb1d779a6e2abc5aff52711d4`
- `e3/monitor_cases.jsonl`: `5f54aa692f82caf99a5a3c24a5459448455a92878dcb3661d58f3e57fb53bbc0`
- `e4/cases.jsonl`: `68088b0014f098dfe3dff0b30492d973cd23c5dbfe5789d86489110c304659ad`
- `retrieval/queries.jsonl`: `1d22cee552bff47262136360fddb0530b9cce41cd4a79ce18ac43a5e6dfcbb88`

## Results

Pre-screen = every item (deterministic checks and judge flags only pre-screen). Gate = items the owner has confirmed (IMPLEMENTATION_PLAN §10.1), and it needs every item of the set confirmed: with fewer, the gate is *not evaluable*, never a pass (the count is shown as "k of N confirmed").

| Metric | Threshold | Pre-screen (all items) | Gate (owner-confirmed) | Gate status |
|---|---|---|---|---|
| Target message in the top 5 of any search in the agent run | >= 85 % | 0/1 (0.0 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |
| Correct reply with a verified citation of a target | >= 80 % | 0/1 (0.0 %) (fail) | n/a (no items) | **NOT EVALUABLE** (0 of 38 confirmed) |
| Target message reached the model (any search result or chat read) | >= 85 % | 0/1 (0.0 %) (fail) | n/a (no items) | **INFO** (0 of 38 confirmed) |

Failed items, Target message in the top 5 of any search in the agent run: r01

Failed items, Correct reply with a verified citation of a target: r01

Failed items, Target message reached the model (any search result or chat read): r01

Note, Target message reached the model (any search result or chat read): Pipeline signal next to the gated top 5: it also counts search results past the fifth and messages get_conversation showed, which can hide a ranking regression; the answer metric already scores the pipeline end to end.

### top-5 hit by intent

| Slice | Result |
|---|---|
| exact | 0/1 (0.0 %) |

## Cost

Cap USD 0.30; spent $0.0000 over 0 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|

Cost per 100 items is over the 1 items evaluated.

## Notes

- Agent mode: the real owner_request runtime with the shipped prompt, the production release flags and the product's citation step (E-aa).
- 1 item(s) made no search_messages call (a top-5 miss, `no_search`): r01
- 1 run(s) did not succeed and count as misses: r01

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
