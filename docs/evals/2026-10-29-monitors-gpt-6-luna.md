# Evaluation: monitors (monitors) - gpt-6-luna - 2026-10-29

| | |
|---|---|
| Run date | 2026-10-29 |
| Target / mode | monitors / e3-monitor-cases |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | evaluate_monitor.v1 |
| Items evaluated | 30 of 30 |
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
| Monitor accuracy (fired exactly when labelled matching) | >= 90 % | 30/30 (100.0 %) (pass) | n/a (no items) | **INFO** (0 of 30 confirmed) |
| False-match rate (labelled not matching among the monitors that fired) | <= 10 % | 0/15 (0.0 %) (pass) | n/a (no items) | **INFO** (0 of 15 confirmed) |
| Match recall (labelled matching that fired) | informational | 15/15 (100.0 %) (info) | n/a (no items) | **INFO** (0 of 15 confirmed) |
| Negatives matched (labelled not matching that fired) | informational | 0/15 (0.0 %) (info) | n/a (no items) | **INFO** (0 of 15 confirmed) |

Note, Monitor accuracy (fired exactly when labelled matching): The bar for the model choice (spec E3 §B, informational): compared on the pre-screen, not an owner-confirmed gate.

Note, False-match rate (labelled not matching among the monitors that fired): The bar for the model choice (spec E3 §B, informational): compared on the pre-screen, not an owner-confirmed gate.

### monitor accuracy by variant

| Slice | Result |
|---|---|
| en | 6/6 (100.0 %) |
| ru | 6/6 (100.0 %) |
| uz-Cyrl | 6/6 (100.0 %) |
| uz-Latn | 6/6 (100.0 %) |
| uz-ru-mix | 6/6 (100.0 %) |

### false matches (labelled not matching among fired) by variant

| Slice | Result |
|---|---|
| en | 0/3 (0.0 %) |
| ru | 0/3 (0.0 %) |
| uz-Cyrl | 0/3 (0.0 %) |
| uz-Latn | 0/3 (0.0 %) |
| uz-ru-mix | 0/3 (0.0 %) |

### match recall by variant

| Slice | Result |
|---|---|
| en | 3/3 (100.0 %) |
| ru | 3/3 (100.0 %) |
| uz-Cyrl | 3/3 (100.0 %) |
| uz-Latn | 3/3 (100.0 %) |
| uz-ru-mix | 3/3 (100.0 %) |

## Cost

Cap USD 1; spent $0.0030 over 30 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 30 | 24885 | 1101 | $0.0030 | $0.0101 |

Cost per 100 items is over the 30 items evaluated.

## Notes

- Accuracy >= 90 % and false-match rate <= 10 % are the bar for the model choice (TL decision, spec E3 §B: informational), compared on the pre-screen; no gate. Owner-confirmed cases (`confirmed_items` = case ids) are counted for information.
- A monitor fired when the product's effects enqueued its `monitor_match` notification (one evaluate_monitor call per case for all its monitors); nothing was delivered.
- Model choice (IMPLEMENTATION_PLAN §7): Luna first; a tier that misses the bar on the pre-screen is followed by the next (Sol, then Astra). `python -m evals compare <the route model's full monitors run, then the next tiers' in the order tried>` states the decision for the evaluate_monitor route.

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
