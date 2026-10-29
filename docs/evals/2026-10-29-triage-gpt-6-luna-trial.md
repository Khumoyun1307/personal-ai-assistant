# Evaluation: triage (triage) - gpt-6-luna - 2026-10-29

> **TRIAL RUN.** Only 1 of the set's 150 items were selected (`--limit` or `--only`); a trial is never a gate.

| | |
|---|---|
| Run date | 2026-10-29 |
| Target / mode | triage / e3-bursts |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | triage.v1 |
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
| Urgent/decision recall (alert: true that got an urgent or decision alert) | >= 90 % | 1/1 (100.0 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 55 confirmed) |
| False-alert rate (alert: false among bursts that got an urgent/decision alert) | <= 20 % | 0/1 (0.0 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 1 confirmed) |
| False-closure rate (unresolved tasks the run closed) | <= 10 % | n/a (no items) (not evaluable) | n/a (no items) | **NOT EVALUABLE** (0 of 20 confirmed) |
| Resolution recall (resolved tasks the run closed) | >= 80 % | 1/1 (100.0 %) (pass) | n/a (no items) | **NOT EVALUABLE** (0 of 20 confirmed) |
| Negatives alerted (alert: false that got an urgent/decision alert) | informational | n/a (no items) (info) | n/a (no items) | **INFO** (0 of 0 confirmed) |
| needs_reply agrees with the label | informational | 1/1 (100.0 %) (info) | n/a (no items) | **INFO** (0 of 1 confirmed) |
| Commitment recall (a task of the right side citing the labelled message) | informational | n/a (no items) (info) | n/a (no items) | **INFO** (0 of 0 confirmed) |
| Recalled commitments carrying their values (title or due) | informational | n/a (no items) (info) | n/a (no items) | **INFO** (0 of 0 confirmed) |
| Suspicious recall (suspicious: true flagged suspicious) | informational | n/a (no items) (info) | n/a (no items) | **INFO** (0 of 0 confirmed) |
| Reasons and task titles in the owner's language and script (P2-O11) | informational | n/a (no items) (info) | n/a (no items) | **INFO** (0 of 0 confirmed) |

Note, False-alert rate (alert: false among bursts that got an urgent/decision alert): The gate also waits for every burst of the set to be run and owner-confirmed (an unconfirmed burst could be an alert); 'pending' counts those not yet.

### urgent/decision recall by variant

| Slice | Result |
|---|---|
| uz-Latn | 1/1 (100.0 %) |

### false alerts (alert: false among alerted) by variant

| Slice | Result |
|---|---|
| uz-Latn | 0/1 (0.0 %) |

### resolution recall by variant

| Slice | Result |
|---|---|
| uz-Latn | 1/1 (100.0 %) |

### false closures by variant

| Slice | Result |
|---|---|

### needs_reply agreement by variant

| Slice | Result |
|---|---|
| uz-Latn | 1/1 (100.0 %) |

### commitment recall by side

| Slice | Result |
|---|---|

### owner language by variant

| Slice | Result |
|---|---|

## Cost

Cap USD 1; spent $0.0004 over 1 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 1 | 2695 | 313 | $0.0004 | $0.0426 |

Cost per 100 items is over the 1 items evaluated.

## Notes

- Pre-screen only until the owner confirms the bursts' labels (PRD R2 #5): the gate counts owner-confirmed bursts (`confirmed_items` = burst ids) and needs the whole set.
- Alerts are what the product's effects store enqueued (`notifications` rows, kinds `triage_urgent`/`triage_decision`); nothing was delivered.
- owner language not determined (too short to name; script right): b001
- Model choice (IMPLEMENTATION_PLAN §7): Luna first; a tier that misses a threshold on the pre-screen is followed by the next (Sol, then Astra). `python -m evals compare <the route model's full run, then the next tiers' in the order tried>` states the decision.

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
