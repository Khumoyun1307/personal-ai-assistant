# Evaluation: triage (triage) - gpt-6-luna - 2026-10-29

| | |
|---|---|
| Run date | 2026-10-29 |
| Target / mode | triage / e3-bursts |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | triage.v1 |
| Items evaluated | 150 of 150 |
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
| Urgent/decision recall (alert: true that got an urgent or decision alert) | >= 90 % | 54/55 (98.2 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 55 confirmed) |
| False-alert rate (alert: false among bursts that got an urgent/decision alert) | <= 20 % | 11/65 (16.9 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 65 confirmed) |
| False-closure rate (unresolved tasks the run closed) | <= 10 % | 0/20 (0.0 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 20 confirmed) |
| Resolution recall (resolved tasks the run closed) | >= 80 % | 20/20 (100.0 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 20 confirmed) |
| Negatives alerted (alert: false that got an urgent/decision alert) | informational | 11/95 (11.6 %) (info) | n/a (no items) | **INFO** (0 of 95 confirmed) |
| needs_reply agrees with the label | informational | 149/150 (99.3 %) (info) | n/a (no items) | **INFO** (0 of 150 confirmed) |
| Commitment recall (a task of the right side citing the labelled message) | informational | 20/20 (100.0 %) (info) | n/a (no items) | **INFO** (0 of 20 confirmed) |
| Recalled commitments carrying their values (title or due) | informational | 0/20 (0.0 %) (info) | n/a (no items) | **INFO** (0 of 20 confirmed) |
| Suspicious recall (suspicious: true flagged suspicious) | informational | 15/15 (100.0 %) (info) | n/a (no items) | **INFO** (0 of 15 confirmed) |
| Reasons and task titles in the owner's language and script (P2-O11) | informational | 61/61 (100.0 %) (info) | n/a (no items) | **INFO** (0 of 61 confirmed) |

Failed items, Urgent/decision recall (alert: true that got an urgent or decision alert): b102

Failed items, False-alert rate (alert: false among bursts that got an urgent/decision alert): b002, b020, b021, b032, b050, b080, b081, b110, b111, b140, b141

Note, False-alert rate (alert: false among bursts that got an urgent/decision alert): The gate also waits for every burst of the set to be run and owner-confirmed (an unconfirmed burst could be an alert); 'pending' counts those not yet.

Failed items, Negatives alerted (alert: false that got an urgent/decision alert): b002, b020, b021, b032, b050, b080, b081, b110, b111, b140, b141

Failed items, needs_reply agrees with the label: b086

Failed items, Recalled commitments carrying their values (title or due): b004.c1, b005.c1, b007.c1, b028.c1, b034.c1, b035.c1, b037.c1, b058.c1, b064.c1, b065.c1, b067.c1, b088.c1, b094.c1, b095.c1, b097.c1, b118.c1, b124.c1, b125.c1, b127.c1, b148.c1

### urgent/decision recall by variant

| Slice | Result |
|---|---|
| en | 11/11 (100.0 %) |
| ru | 10/11 (90.9 %) |
| uz-Cyrl | 11/11 (100.0 %) |
| uz-Latn | 11/11 (100.0 %) |
| uz-ru-mix | 11/11 (100.0 %) |

### false alerts (alert: false among alerted) by variant

| Slice | Result |
|---|---|
| en | 2/13 (15.4 %) |
| ru | 2/12 (16.7 %) |
| uz-Cyrl | 2/13 (15.4 %) |
| uz-Latn | 3/14 (21.4 %) |
| uz-ru-mix | 2/13 (15.4 %) |

### resolution recall by variant

| Slice | Result |
|---|---|
| en | 4/4 (100.0 %) |
| ru | 4/4 (100.0 %) |
| uz-Cyrl | 4/4 (100.0 %) |
| uz-Latn | 4/4 (100.0 %) |
| uz-ru-mix | 4/4 (100.0 %) |

### false closures by variant

| Slice | Result |
|---|---|
| en | 0/4 (0.0 %) |
| ru | 0/4 (0.0 %) |
| uz-Cyrl | 0/4 (0.0 %) |
| uz-Latn | 0/4 (0.0 %) |
| uz-ru-mix | 0/4 (0.0 %) |

### needs_reply agreement by variant

| Slice | Result |
|---|---|
| en | 30/30 (100.0 %) |
| ru | 30/30 (100.0 %) |
| uz-Cyrl | 29/30 (96.7 %) |
| uz-Latn | 30/30 (100.0 %) |
| uz-ru-mix | 30/30 (100.0 %) |

### commitment recall by side

| Slice | Result |
|---|---|
| owner | 5/5 (100.0 %) |
| contact | 15/15 (100.0 %) |

### owner language by variant

| Slice | Result |
|---|---|
| en | 23/23 (100.0 %) |
| ru | 17/17 (100.0 %) |
| uz-Cyrl | 4/4 (100.0 %) |
| uz-Latn | 8/8 (100.0 %) |
| uz-ru-mix | 9/9 (100.0 %) |

## Cost

Cap USD 1; spent $0.0539 over 150 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 150 | 369994 | 33732 | $0.0539 | $0.0359 |

Cost per 100 items is over the 150 items evaluated.

## Notes

- Pre-screen only until the owner confirms the bursts' labels (PRD R2 #5): the gate counts owner-confirmed bursts (`confirmed_items` = burst ids) and needs the whole set.
- Alerts are what the product's effects store enqueued (`notifications` rows, kinds `triage_urgent`/`triage_decision`); nothing was delivered.
- owner language not determined (too short to name; script right): b001, b004, b005, b006, b010, b011, b012, b013, b014, b015, b017, b020, b021, b025, b028, b030, b060, b061, b063, b064, b065, b066, b067, b070, b071, b072, b073, b074, b075, b078, b080, b081, b085, b086, b088, b089, b090, b093, b099, b107, b115, b118, b119, b120, b121, b122, b124, b125, b126, b127, b130, b134, b135, b140, b141, b145, b148, b149, b150
- Model choice (IMPLEMENTATION_PLAN §7): Luna first; a tier that misses a threshold on the pre-screen is followed by the next (Sol, then Astra). `python -m evals compare <the route model's full run, then the next tiers' in the order tried>` states the decision.

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
