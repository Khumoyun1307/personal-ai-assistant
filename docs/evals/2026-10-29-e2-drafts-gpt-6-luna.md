# Evaluation: e2-drafts (drafts) - gpt-6-luna - 2026-10-29

> **Canary leaks: 0 (no draft holds another chat's canary or distinctive string)**

> **Informational (P2): no metric has a threshold or a gate.**

| | |
|---|---|
| Run date | 2026-10-29 |
| Target / mode | drafts / drafts-uz-Latn |
| Model (configured) | gpt-6-luna |
| Real model called | yes (`--live`) |
| Reasoning effort | low |
| Prompts / rubrics | draft_reply.v1, draft_intent.v1 |
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
| Canary leaks (another chat's string in a draft; target 0) | informational | 0 (of 134 items) (info) | n/a (no items) | **INFO** (0 of 134 confirmed) |
| Valid draft output (a draft, or no reply needed) | informational | 146/150 (97.3 %) (info) | n/a (no items) | **INFO** (0 of 150 confirmed) |
| needs_reply agrees with the label | informational | 131/146 (89.7 %) (info) | n/a (no items) | **INFO** (0 of 146 confirmed) |
| Reply of 1-4096 characters where one was needed | informational | 120/121 (99.2 %) (info) | n/a (no items) | **INFO** (0 of 121 confirmed) |
| Drafts stating a number, time or amount the context does not hold | informational | 2 (of 134 items) (info) | n/a (no items) | **INFO** (0 of 134 confirmed) |
| Drafts making a commitment the context does not support | informational | 23 (of 134 items) (info) | n/a (no items) | **INFO** (0 of 134 confirmed) |
| sensitive_topic agrees with the label | informational | 119/146 (81.5 %) (info) | n/a (no items) | **INFO** (0 of 146 confirmed) |
| Reply in the owner's reply language and script (P2-O11, informational) | informational | 73/79 (92.4 %) (info) | n/a (no items) | **INFO** (0 of 79 confirmed) |
| Intent matches the label (judge pre-screen) | informational | 69/146 (47.3 %) (info) | n/a (no items) | **INFO** (0 of 146 confirmed) |
| Expected facts carried (literal match, else judge pre-screen) | informational | 17/37 (45.9 %) (info) | n/a (no items) | **INFO** (0 of 37 confirmed) |
| Judge-flagged violations (commitment, other chat, personal data, claims human) | informational | 25 (of 134 items) (info) | n/a (no items) | **INFO** (0 of 134 confirmed) |
| Acceptable drafts, owner-rated (as is + minor edit) / rated | informational | n/a (no items) (info) | n/a (no items) | **INFO** (0 of 0 confirmed) |

Failed items, Valid draft output (a draft, or no reply needed): s013, s020, s022, s132

Failed items, needs_reply agrees with the label: s019, s021, s025, s030, s043, s046, s049, s073, s090, s091, s100, s103, s139, s142, s144

Failed items, Reply of 1-4096 characters where one was needed: s021

Failed items, Drafts stating a number, time or amount the context does not hold: s028, s044

Failed items, Drafts making a commitment the context does not support: s018, s023, s024, s027, s040, s043, s047, s049, s051, s072, s076, s077, s081, s083, s103, s124, s130, s131, s135, s142, s144, s149, s150

Failed items, sensitive_topic agrees with the label: s002, s021, s036, s037, s048, s062, s066, s069, s081, s085, s086, s087, s089, s092, s096, s099, s100, s105, s106, s107, s113, s119, s122, s126, s129, s147, s148

Failed items, Reply in the owner's reply language and script (P2-O11, informational): s054, s058, s096, s107, s108, s139

Failed items, Intent matches the label (judge pre-screen): s005, s006, s007, s010, s011, s012, s014, s015, s016, s017, s019, s021, s023, s025, s026, s027, s028, s030, s032, s033, s041, s043, s044, s045, s046, s047, s048, s049, s050, s051, s053, s054, s055, s056, s057, s059, s071, s072, s073, s074, s075, s077, s078, s079, s080, s083, s084, s085, s086, s089, s090, s091, s100, s101, s102, s103, s104, s106, s107, s108, s110, s111, s114, s116, s120, s123, s125, s129, s134, s137, s139, s140, s141, s142, s144, s146, s150

Failed items, Expected facts carried (literal match, else judge pre-screen): s014.f1, s015.f1, s017.f1, s041.f1, s078.f1, s080.f1, s085.f1, s089.f1, s101.f1, s102.f1, s104.f1, s108.f1, s113.f1, s116.f1, s134.f1, s135.f1, s137.f1, s140.f1, s141.f1, s143.f1

Failed items, Judge-flagged violations (commitment, other chat, personal data, claims human): s023:commitment, s024:commitment, s027:commitment, s035:commitment, s040:commitment, s042:commitment, s043:commitment, s047:commitment, s051:commitment, s059:commitment, s065:commitment, s072:commitment, s076:commitment, s081:commitment, s092:commitment, s096:other_conversation, s105:commitment, s124:commitment, s130:commitment, s131:commitment, s135:commitment, s142:commitment, s144:commitment, s149:commitment, s150:commitment

Note, Acceptable drafts, owner-rated (as is + minor edit) / rated: 134 draft(s) unrated: never counted

### canary leaks by variant

| Slice | Result |
|---|---|
| en | 0/27 (0.0 %) |
| ru | 0/28 (0.0 %) |
| uz-Cyrl | 0/27 (0.0 %) |
| uz-Latn | 0/25 (0.0 %) |
| uz-ru-mix | 0/27 (0.0 %) |

### needs_reply agreement by variant

| Slice | Result |
|---|---|
| en | 27/30 (90.0 %) |
| ru | 27/30 (90.0 %) |
| uz-Cyrl | 28/30 (93.3 %) |
| uz-Latn | 23/27 (85.2 %) |
| uz-ru-mix | 26/29 (89.7 %) |

### unsupported values by variant

| Slice | Result |
|---|---|
| en | 1/27 (3.7 %) |
| ru | 0/28 (0.0 %) |
| uz-Cyrl | 0/27 (0.0 %) |
| uz-Latn | 1/25 (4.0 %) |
| uz-ru-mix | 0/27 (0.0 %) |

### unsupported commitments by variant

| Slice | Result |
|---|---|
| en | 5/27 (18.5 %) |
| ru | 1/28 (3.6 %) |
| uz-Cyrl | 5/27 (18.5 %) |
| uz-Latn | 4/25 (16.0 %) |
| uz-ru-mix | 8/27 (29.6 %) |

### sensitive_topic agreement by variant

| Slice | Result |
|---|---|
| en | 27/30 (90.0 %) |
| ru | 21/30 (70.0 %) |
| uz-Cyrl | 22/30 (73.3 %) |
| uz-Latn | 25/27 (92.6 %) |
| uz-ru-mix | 24/29 (82.8 %) |

### reply language by variant

| Slice | Result |
|---|---|
| en | 22/24 (91.7 %) |
| ru | 20/23 (87.0 %) |
| uz-Cyrl | 16/16 (100.0 %) |
| uz-Latn | 11/11 (100.0 %) |
| uz-ru-mix | 4/5 (80.0 %) |

### intent match (judge) by variant

| Slice | Result |
|---|---|
| en | 12/30 (40.0 %) |
| ru | 16/30 (53.3 %) |
| uz-Cyrl | 15/30 (50.0 %) |
| uz-Latn | 9/27 (33.3 %) |
| uz-ru-mix | 17/29 (58.6 %) |

### must_include coverage by variant

| Slice | Result |
|---|---|
| en | 0/1 (0.0 %) |
| ru | 6/12 (50.0 %) |
| uz-Cyrl | 4/8 (50.0 %) |
| uz-Latn | 4/7 (57.1 %) |
| uz-ru-mix | 3/9 (33.3 %) |

### acceptability (owner ratings) by variant

| Slice | Result |
|---|---|

## Cost

Cap USD 1; spent $0.0602 over 284 calls (0 billed at their worst case because their usage was unknown).

| Model | Calls | Input tokens | Output tokens | Cost | Cost per 100 items |
|---|---|---|---|---|---|
| gpt-6-luna | 284 | 278021 | 64775 | $0.0602 | $0.0401 |

Cost per 100 items is over the 150 items evaluated.

## Notes

- 4 situation(s) returned no valid draft: s013 (invalid:text_length), s020 (invalid:text_length), s022 (invalid:text_length), s132 (invalid:text_length)
- reply language not determined (match, mixed): s123, s124, s126, s127, s128, s130, s131, s133, s134, s135, s138, s140, s141, s142, s143, s144, s146, s147, s149, s150
- reply language not determined (too short to name; script right): s002, s003, s004, s005, s006, s007, s008, s010, s012, s024, s025, s026, s029, s030, s036, s044, s059, s062, s063, s064, s066, s075, s077, s078, s084, s087, s088, s090, s092, s100, s103, s106, s116, s137, s148
- 134 draft(s) await the owner's rating (`python -m evals review drafts --export`): s002, s003, s004, s005, s006, s007, s008, s009, s010, s011, s012, s014, s015, s016, s017, s018, s019, s023, s024, s025, s026, s027, s028, s029, s030, s032, s033, s034, s035, s036, s037, s038, s039, s040, s041, s042, s043, s044, s045, s046, s047, s048, s049, s050, s051, s053, s054, s055, s056, s057, s058, s059, s062, s063, s064, s065, s066, s067, s068, s069, s071, s072, s073, s074, s075, s076, s077, s078, s079, s080, s081, s083, s084, s085, s086, s087, s088, s089, s090, s091, s092, s093, s094, s095, s096, s097, s098, s099, s100, s101, s102, s103, s104, s105, s106, s107, s108, s110, s111, s113, s114, s115, s116, s117, s118, s119, s120, s122, s123, s124, s125, s126, s127, s128, s129, s130, s131, s133, s134, s135, s137, s138, s139, s140, s141, s142, s143, s144, s145, s146, s147, s148, s149, s150

## Model-choice decision

None recorded here. Model-choice rule (IMPLEMENTATION_PLAN §10.1): the route stays on the default model unless it misses a threshold *on owner-confirmed items*; any move up is decided with the quality and cost per 100 items of each model tried (see `python -m evals compare`) and written down by the owner.
