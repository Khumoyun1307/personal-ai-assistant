# Evaluation: owner-drafting-effort-low (owner_drafting) - gpt-6-luna - 2026-10-30

> **TRIAL RUN.** Only 1 of the set's 31 items were selected (`--limit` or `--only`); a trial is never a gate.

| | |
|---|---|
| Run date | 2026-10-30 |
| Target / mode | owner_drafting / effort-low |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | owner_request.v10, draft_owner.v2, owner_drafting.v1 |
| Items evaluated | 1 of 1 |
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
| Every deterministic check of the case passed | informational | 1/1 (100.0 %) (info) | n/a (no items) | **INFO** (0 of 31 confirmed) |
| Routing: the right tools, and a card staged or revised only when asked | informational | 1/1 (100.0 %) (info) | n/a (no items) | **INFO** (0 of 31 confirmed) |
| Card: chat, origin, mode, answered message, text and length checks | informational | n/a (no items) (info) | n/a (no items) | **INFO** (0 of 0 confirmed) |
| Reply: identity and the exact word count | informational | 1/1 (100.0 %) (info) | n/a (no items) | **INFO** (0 of 1 confirmed) |
| Judge pre-screen: tone or substance as asked | informational | n/a (no items) (info) | n/a (no items) | **INFO** (0 of 0 confirmed) |

Note, Judge pre-screen: tone or substance as asked: A pre-screen only; the owner reads the JSON report's drafts.

### case passed by category

| Slice | Result |
|---|---|
| identity | 1/1 (100.0 %) |

### case passed by variant

| Slice | Result |
|---|---|
| uz-Latn | 1/1 (100.0 %) |

## Cost

Cap USD 1; spent $0.0008 over 1 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 1 | 8169 | 12 | $0.0008 | $0.0823 |

Cost per 100 items is over the 1 items evaluated.

## Notes

- Owner-requested drafting and routing through the real owner_request runtime, the product's drafter and card machinery; owner_request effort low, draft_owner effort low. Informational: no threshold, no gate.

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
