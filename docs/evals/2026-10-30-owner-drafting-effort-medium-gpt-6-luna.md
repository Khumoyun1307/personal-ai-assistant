# Evaluation: owner-drafting-effort-medium (owner_drafting) - gpt-6-luna - 2026-10-30

| | |
|---|---|
| Run date | 2026-10-30 |
| Target / mode | owner_drafting / effort-medium |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | medium |
| Prompts / rubrics | owner_request.v10, draft_owner.v2, owner_drafting.v1 |
| Items evaluated | 31 of 31 |
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
| Every deterministic check of the case passed | informational | 27/31 (87.1 %) (info) | n/a (no items) | **INFO** (0 of 31 confirmed) |
| Routing: the right tools, and a card staged or revised only when asked | informational | 30/31 (96.8 %) (info) | n/a (no items) | **INFO** (0 of 31 confirmed) |
| Card: chat, origin, mode, answered message, text and length checks | informational | 20/23 (87.0 %) (info) | n/a (no items) | **INFO** (0 of 23 confirmed) |
| Reply: identity and the exact word count | informational | 3/4 (75.0 %) (info) | n/a (no items) | **INFO** (0 of 4 confirmed) |
| Judge pre-screen: tone or substance as asked | informational | 3/4 (75.0 %) (info) | n/a (no items) | **INFO** (0 of 4 confirmed) |

Failed items, Every deterministic check of the case passed: od03, od16, od17, od18

Failed items, Routing: the right tools, and a card staged or revised only when asked: od17

Failed items, Card: chat, origin, mode, answered message, text and length checks: od16, od17, od18

Failed items, Reply: identity and the exact word count: od03

Failed items, Judge pre-screen: tone or substance as asked: od29

Note, Judge pre-screen: tone or substance as asked: A pre-screen only; the owner reads the JSON report's drafts.

### case passed by category

| Slice | Result |
|---|---|
| advice | 2/2 (100.0 %) |
| append | 3/3 (100.0 %) |
| command_words | 2/2 (100.0 %) |
| compose | 3/3 (100.0 %) |
| dictate | 3/3 (100.0 %) |
| identity | 2/3 (66.7 %) |
| pick_suggestion | 1/1 (100.0 %) |
| reply_earlier | 0/3 (0.0 %) |
| shorter | 2/2 (100.0 %) |
| substance | 2/2 (100.0 %) |
| suggest | 2/2 (100.0 %) |
| tone | 2/2 (100.0 %) |
| word_bounds | 2/2 (100.0 %) |
| word_count | 1/1 (100.0 %) |

### case passed by variant

| Slice | Result |
|---|---|
| en | 7/8 (87.5 %) |
| ru | 4/6 (66.7 %) |
| uz-Latn | 16/17 (94.1 %) |

## Cost

Cap USD 1; spent $0.0863 over 114 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 114 | 819626 | 8659 | $0.0863 | $0.2784 |

Cost per 100 items is over the 31 items evaluated.

## Notes

- Owner-requested drafting and routing through the real owner_request runtime, the product's drafter and card machinery; owner_request effort medium, draft_owner effort medium. Informational: no threshold, no gate.
- 4 case(s) failed a check: od03, od16, od17, od18

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
