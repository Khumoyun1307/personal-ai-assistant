# DATA-E3F — E3 v1.1: follow-up-style monitor cases (junior unit, informational)

| | |
|---|---|
| **Unit** | DATA-E3F — 15 follow-up-style monitor cases `m031`–`m045` (3 per E3 variant: a delivery shown by media and its description, a partial delivery, a repeated promise) appended to `evals/datasets/e3/monitor_cases.jsonl`; two dataset tests; the E3 README; one dataset-log row. **Informational:** no gate reads them |
| **Owner slot** | D2 (David's team, junior) |
| **Mentor (peer review)** | E3 (Codex; the TL dispatches the review). E3 reviews your sample (`m031`–`m040`) before you write the rest |
| **QA** | QD1, after merge (on `int/p3` before R1; on `main` if the PR lands after R1) |
| **Size** | S (≈ 0.5 engineer-day, one fix round included) |
| **Depends on** | CP5 (E3 is on `main`, so on `int/p3` from D0). No F0 code, no other unit |
| **Branch → base** | `p3/data-e3f` from `origin/int/p3` → PR to `int/p3` (planned D+5). If R1 (`int/p3` → `main`) has merged before you start, `int/p3` is retired: branch from `origin/main` and target `main` (WORKFLOW §4.1, stragglers row). David tells you which applies |
| **Normative inputs** | WORKFLOW §9; P2 spec `docs/engineering/p2/specs/E3-triage-eval.md` §A ("Layout — `monitor_cases.jsonl`") and §B (the `evaluate_monitor` target; its bar is informational); `evals/schema.py` (`MonitorCase`, `MonitorUnderTest`, `MonitorLabels`, `BurstMessage`, `load_monitor_cases`); `evals/monitors.py` (how one case runs; `OWNER_LANGUAGE`); `src/assistant/agent/prompts/evaluate_monitor.v1.md` rule 3; `tests/unit/test_e3_dataset.py`; `tests/integration/test_eval_monitors.py`; [M7-FUP](M7-FUP.md) §2 and §9 (contract gap FUP-1) |

## 1. Goal

A follow-up closes when the awaited thing arrives. The worst follow-up error is a **false auto-close**: the owner is told the documents arrived when only half came, or when the contact only promised them again. In P3 a follow-up matches only deterministically (`contact_replied`, `keywords`, `media_received`; `create_follow_up` refuses `semantic`, M7-FUP contract gap FUP-1), and for "a message about the contract" the owner uses a semantic monitor. E3's 30 monitor cases do not measure how `evaluate_monitor` handles these follow-up-shaped messages. You append 15 synthetic cases, 3 per variant, so the next `python -m evals run monitors` reports them and the TL has evidence for FUP-1. They are informational: no threshold, gate or code changes.

## 2. Read first

1. [AGENTS.md](../../../../AGENTS.md) §8 ("evaluation datasets are synthetic or hand-written only") and §9.
2. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md) (all) and [WORKFLOW](../WORKFLOW.md) §2, §3, §4, §9.
3. P2 spec `docs/engineering/p2/specs/E3-triage-eval.md` §A, the monitor-case layout, and `evals/datasets/e3/README.md` (labelling guidelines; rule 2: "a promise repeated or reaffirmed is not fulfillment", "reporting partial progress is not fulfillment").
4. `evals/schema.py`, from `class BurstMessage` (a message needs text or media; media is `photo`, `voice` or `document`; `sent_at` in UTC) to `load_monitor_cases`. `MonitorCase` forbids extra keys.
5. `src/assistant/agent/prompts/evaluate_monitor.v1.md` rule 3: a semantic monitor matches only when a new contact message "is about what the owner described, in substance — not merely a shared word"; "If you are unsure, it does not match".
6. `evals/monitors.py`, the module docstring and `makes_a_call`; `src/assistant/agent/monitor_eval.py::message_line` (a media message reaches the model as `(photo)` or `(document)` followed by its caption).
7. `tests/unit/test_e3_dataset.py` (all) and `tests/integration/test_eval_monitors.py::test_a_model_answering_the_labels_meets_the_bar_on_every_e3_case` (it runs **every** committed case through the product's context and cites the newest new message of each matching case).
8. The 30 existing lines of `evals/datasets/e3/monitor_cases.jsonl`. They are synthetic.
9. This spec, to the end, before you start.

## 3. Allowed files

| Path | New or existing | Exactly what may change |
|---|---|---|
| `evals/datasets/e3/monitor_cases.jsonl` | existing | Append 15 lines `m031`–`m045` after `m030`, one JSON object per line. Never change `m001`–`m030` |
| `evals/datasets/e3/README.md` | existing | The edits of §6.4 step 9 only |
| `evals/datasets/MANIFEST.sha256` | existing | Only by `python -m evals.manifest --write` in the container, never by hand |
| `docs/evals/README.md` | existing | Append **one** row to the "Dataset log" table (§6.4 step 9). Nothing else |
| `tests/unit/test_e3_dataset.py` | existing | Append the block of §7 at the end of the file. No existing line changes |

## 4. Forbidden

- Every file not in §3.
- Explicitly: `evals/datasets/e3/bursts.jsonl`; `evals/datasets/e1/**`, `e2/**`, `e4/**`, `retrieval/**`; `evals/reviews/**`; `evals/schema.py`, `evals/monitors.py` and every other `.py` file except the test file of §3 (`tests/integration/test_eval_monitors.py` is read, never edited); `src/` (including `agent/prompts/` and `automation/`); `migrations/`; `policy/`; `actions/`; `telegram/`; `privacy/`; consent code; `config.py`; `core/`; `AGENTS.md`; any `docs/` file other than the one row in `docs/evals/README.md`.
- Changing a threshold, a metric or the matching rule: this unit is data and two dataset tests.
- Committing the checker (it lives outside the repository).
- Copying anything from a real chat, person, company, website or other dataset.

## 5. Shared lists you must update

| List | Edit |
|---|---|
| `evals/datasets/MANIFEST.sha256` | Regenerate in **every** data commit, after rebasing on the current base: `docker compose -f compose.dev.yml run --rm app uv run python -m evals.manifest --write` |
| `tests/unit/test_e1_dataset.py::test_manifest_matches_the_dataset_files` | **No edit:** you add no new data file (and since F0-E the set is derived) |
| `tests/unit/test_e3_dataset.py` | The appended block of §7 (its only change) |
| `docs/evals/README.md` "Dataset log" | One row (§6.4 step 9), after the existing rows (DATA-E2 added the E3 v1 row) |

## 6. Step-by-step

### 6.1 The record format (unchanged from E3 v1)

One JSON object per line, UTF-8, **LF**, NFC, no BOM, a newline after the last line; keys in this order; separators `", "` and `": "`; letters as they are (no `\u` escapes). `MonitorCase` forbids any other key: there is no `notes`, `tags` or `contact` field.

| Field | Rule |
|---|---|
| `id` | `m031` … `m045`, consecutive, in file order |
| `variant` | Blocks, in the v1 order: `m031`–`m033` `uz-Latn`, `m034`–`m036` `en`, `m037`–`m039` `ru`, `m040`–`m042` `uz-Cyrl`, `m043`–`m045` `uz-ru-mix` |
| `monitors` | Exactly one: `{"id": 1, "kind": "semantic", "description": "<text>"}` |
| `description` | One line, 1–120 characters (the owner's tool allows 300), in the owner's language for the variant: the variant's own language, and **Russian for `uz-ru-mix`** (`evals/monitors.py::OWNER_LANGUAGE`). Written as the owner would ask for a follow-up: it names the awaited thing precisely, and says "all" or the number when completeness matters ("all three signed pages of the agreement"). Different from every other description in the file |
| `burst` | 1–3 messages, **every one `"direction": "contact"`** (the product cites only contact messages, and the integration test cites the newest message of a matching case). Ids `<case id>.m01`, `.m02`, …; `sent_at` UTC ending in `Z`, strictly increasing |
| message keys | `id`, `direction`, `sent_at`, `text`, and `media` only on a media message. A text message has no `media` key (as v1). A media message with a caption has `"text": "<caption>", "media": "<kind>"`; a bare media message has `"text": null, "media": "<kind>"` |
| `media` | `"photo"` or `"document"` only, with the kind per block and slot of the table in §6.2 (the pinned test checks it). Never `"voice"` (the model only sees `(voice)`, so a voice note can never show what it contains) |
| `labels` | `{"matched_monitor_ids": [1]}` for slot 1; `{"matched_monitor_ids": []}` for slots 2 and 3 |

### 6.2 What to write: the 3 slots of each variant block

Slot `k` of block `b` (b = 0 for `uz-Latn` … 4 for `uz-ru-mix`) is `m(030 + 3 × b + k)`.

| Slot | Shape | Burst | Label |
|---|---|---|---|
| 1 | **Delivered: media and its description.** The awaited thing arrives as a photo or document, and the contact says what it is | **Caption form** (`uz-Latn`, `ru`, `uz-ru-mix`): one media message whose caption names what it is. **Bare form** (`en`, `uz-Cyrl`): `m01` bare media (`text: null`), then `m02` a text 1–3 min later naming what was sent | `[1]` |
| 2 | **Partial delivery.** Only part of the awaited thing arrives, and the contact says the rest comes later | 1–2 messages: a media message with a caption (or followed by a text) that says it is only part | `[]` |
| 3 | **A repeated promise is not delivery.** The contact promises again, apologises or gives a new time; nothing is sent | 2–3 text messages, no media | `[]` |

Media kinds per block:

| Slot | `uz-Latn` | `en` | `ru` | `uz-Cyrl` | `uz-ru-mix` |
|---|---|---|---|---|---|
| 1 | `document` (caption) | `photo` (bare) | `document` (caption) | `photo` (bare) | `document` (caption) |
| 2 | `photo` | `document` | `photo` | `document` | `photo` |

**Seeds** (situations, not wording; write the text yourself, naturally, in the variant's language; `uz-ru-mix` mixes Uzbek and Russian in the messages, as v1 does):

| Variant | Slot 1 (delivered) | Slot 2 (partial) | Slot 3 (repeated promise) |
|---|---|---|---|
| `uz-Latn` | the signed rental contract | all six photos of the flat for a listing; two arrive | the October invoice |
| `en` | a photo of the repaired bicycle | all three signed pages of the agreement; page one arrives | the scanned ID copy for the lease |
| `ru` | the scanned diploma for an application | photos of all four rooms after the renovation; two rooms arrive | the signed certificate of completed work |
| `uz-Cyrl` | a photo of the finished kitchen cabinets | the full report with its appendices; only the main part arrives | the price list for the event menu |
| `uz-ru-mix` | the payment receipt | photos of all three delivered boxes; one arrives | the photos from the event |

**Labelling rules** (as a careful person would read the owner's monitor):
- Slot 1 matches: the media is there and the words say it is the awaited thing.
- Slot 2 does not match: the monitor asks for all of it, and the contact says only part came. The caption must say so plainly ("the other two tomorrow"); never leave it for the reader to guess.
- Slot 3 does not match: talking about the thing is not sending it (E3 README labelling rule 2).
- Never write a case where a careful person would hesitate. If a seed starts to feel borderline in your language, make the wording plainer, not the label different.
- No case contains an instruction to the assistant (v1 already covers injections).

### 6.3 Timing and examples

- Date 2026-11-04 (v1 uses 2026-11-03). Block `b` starts at 10:00 UTC + `b` hours; slot `k`'s `m01` at that start + (k − 1) × 20 min; later messages of a case 1–5 min apart.

`m034`, `m035`, `m036` (the `en` block), which you may use exactly as written:

```json
{"id": "m034", "variant": "en", "monitors": [{"id": 1, "kind": "semantic", "description": "the workshop sends a photo of my repaired bicycle"}], "burst": [{"id": "m034.m01", "direction": "contact", "sent_at": "2026-11-04T11:00:00Z", "text": null, "media": "photo"}, {"id": "m034.m02", "direction": "contact", "sent_at": "2026-11-04T11:02:00Z", "text": "That is your bicycle after the repair: new chain, and both brakes adjusted."}], "labels": {"matched_monitor_ids": [1]}}
{"id": "m035", "variant": "en", "monitors": [{"id": 1, "kind": "semantic", "description": "the contact sends all three signed pages of the agreement"}], "burst": [{"id": "m035.m01", "direction": "contact", "sent_at": "2026-11-04T11:20:00Z", "text": "Here is page one, signed. I will sign the other two pages at the office tomorrow.", "media": "document"}], "labels": {"matched_monitor_ids": []}}
{"id": "m036", "variant": "en", "monitors": [{"id": 1, "kind": "semantic", "description": "the contact sends the scanned copy of their ID for the lease"}], "burst": [{"id": "m036.m01", "direction": "contact", "sent_at": "2026-11-04T11:40:00Z", "text": "Sorry, I have not forgotten the ID copy for the lease."}, {"id": "m036.m02", "direction": "contact", "sent_at": "2026-11-04T11:43:00Z", "text": "I will scan it tonight and send it first thing tomorrow, promise."}], "labels": {"matched_monitor_ids": []}}
```

### 6.4 Steps (each ends with a check)

All commands run in PowerShell in your directory, with `$env:COMPOSE_PROJECT_NAME = 'd2'` set. `$check` is set once per session: `$check = (Resolve-Path ..\p3check).Path`.

1. **Start** when David says (planned D+5). Comment "started" on the DATA-E3F issue. Create your directory: `git fetch origin; git worktree add ..\alviro-wt\d2-data-e3f -b p3/data-e3f origin/int/p3` (after R1: `origin/main`), or a clone (WORKFLOW §3.1); `just setup`; `just hooks`.
   **Check:** `just test tests/unit/test_e3_dataset.py -q` → all passed and **none skipped** (a skip means E3 is not on your base: stop, §10).
2. **The checker.** Use `..\p3check\p3data_check.py` ([DATA-E4a](DATA-E4a.md) Appendix A), unchanged; save it from there if it is missing. You use only its `scan` command.
   **Check:** `docker compose -f compose.dev.yml run --rm -v "${check}:/check:ro" app uv run python /check/p3data_check.py scan evals/datasets/e3/monitor_cases.jsonl m031-m045; "exit $LASTEXITCODE"` → 15 `not found` lines, `exit 1`.
3. **Red tests.** Append the §7 block to `tests/unit/test_e3_dataset.py`.
   **Check:** `just test tests/unit/test_e3_dataset.py -q` → exactly the two new tests fail, each on an `assert` (not an import or syntax error); every other test passes. `docker compose -f compose.dev.yml run --rm app uv run ruff check tests/unit/test_e3_dataset.py` and `… uv run ruff format --check tests/unit/test_e3_dataset.py` → clean. Commit (`test(evals): pin the E3 v1.1 follow-up monitor cases`).
4. **Plan** the 15 cases in your private notes (outside the repository): id, slot, seed, description, media kind, message times. **Check:** the §6.2 tables hold for every block; every description is distinct.
5. **Write the sample `m031`–`m040`** (blocks `uz-Latn`, `en`, `ru`, and `uz-Cyrl` slot 1).
   **Check:** `docker compose -f compose.dev.yml run --rm app uv run python -c "from evals.schema import load_monitor_cases; print(len(load_monitor_cases()))"` → `40` (every line loads); the step 2 scan with `m031-m040` → `ok`, `exit 0`.
6. **Commit the sample.** `git fetch origin; git rebase origin/int/p3` (or `origin/main`); `docker compose -f compose.dev.yml run --rm app uv run python -m evals.manifest --write`; `just test tests/unit/test_e1_dataset.py tests/unit/test_e3_dataset.py -q` → only the two new tests fail (they need all 15 cases). Commit (`feat(evals): E3 v1.1 sample: follow-up monitor cases m031-m040`), push, open the draft PR (§12) with the note "the two new tests turn green with m041–m045", label it `needs-review:e3`, and comment on the issue: "Sample ready for E3: m031–m040". The TL dispatches E3's review from that label and comment (a draft is fine for a sample). **Wait for E3's comment** before step 7.
   **Rebase conflicts (this and every later rebase):** in `MANIFEST.sha256`, run the regenerate command (it overwrites the file), `git add` it and continue; in `docs/evals/README.md`, keep every row in date order; in any `.jsonl` file, stop and ask (§10).
   **Check:** CI on the draft shows only those two expected failures.
7. **Apply E3's comments, then write `m041`–`m045`.**
   **Check:** `just test tests/unit/test_e3_dataset.py -q` → all passed (including `test_e3_monitor_cases_meet_the_minimum_counts`: 20 matching of 45 is inside 35–65 %); the scan with `m031-m045` → `ok`, `exit 0`; `just test tests/integration/test_eval_monitors.py -q` → all passed (if it runs longer than 10 minutes, stop it, say so in the PR and let CI run it).
8. **Label consistency pass** (QD1 repeats it): for each case read the description and the burst and confirm that a careful person would give exactly the label of its slot; that slot 2's caption says plainly that only part came; and that slot 3 sends nothing. Then read each case once for real brands, banks, platforms, cities, streets or people, and replace any with invented ones. **Check:** you can say, for each case, why its label is right; the scan is `ok`.
9. **Docs.**
   - `evals/datasets/e3/README.md`: (a) in "Provenance and Privacy" add "v1.1 (<date>): `m031`–`m045`, follow-up-style monitor cases, by D2 (P3 DATA-E3F), mentored by E3"; (b) in "Dataset Structure" change the `monitor_cases.jsonl` count from 30 to 45; (c) change the heading "### `monitor_cases.jsonl` (30 cases)" to "(45 cases)" and update the table under it (per variant: total, matching, non-matching; the total row; the matching share in the "Spec Target" row) from the output of the command below, never from your plan; (d) add a section `## v1.1 follow-up cases (P3 DATA-E3F)`: why (false auto-close of follow-ups; informational; M7-FUP contract gap FUP-1), the three shapes with their labels, the ids per variant, the media kinds used, and "pending the owner's native-language review".
     Count command: `docker compose -f compose.dev.yml run --rm app uv run python -c "from collections import Counter; from evals.schema import load_monitor_cases; cases = load_monitor_cases(); print(len(cases)); print(sorted(Counter((c.variant, bool(c.labels.matched_monitor_ids)) for c in cases).items()))"` → `45`, then ten `((variant, matched), count)` pairs (each variant: `True` 4, `False` 5).
   - `docs/evals/README.md`, "Dataset log", append: `| <today> | E3 | v1.1 | 15 follow-up-style monitor cases m031–m045, 3 per variant: a delivery shown by media and its description (matching), a partial delivery and a repeated promise (not matching). Informational, no gate. Synthetic; pending the owner's native-language review. | evals/datasets/MANIFEST.sha256 at this commit |` (ids and file names in backticks, as the existing rows do).
   **Check:** the README numbers equal the count command's output.
10. **Final commit.** Rebase on your base; regenerate the manifest; `just test tests/unit/test_e1_dataset.py tests/unit/test_e3_dataset.py -q` → all passed; ruff check and format check on the test file → clean. Commit (`feat(evals): E3 v1.1: follow-up-style monitor cases m031-m045`), push.
    **Check:** `git diff --stat origin/int/p3...HEAD` (or `origin/main...HEAD`) lists only the five files of §3; CI green.
11. **Ready.** Mark the PR ready, keep `needs-review:e3`, add `needs-qa`, comment on the issue. Fix blocking findings in one round (§10).

## 7. Named tests (red first)

Append this block at the end of `tests/unit/test_e3_dataset.py`, exactly (names, constants and values as written; it uses only names the file already imports):

```python
# ------------------------------------------------- E3 v1.1 (P3 DATA-E3F)

#: Follow-up-style monitor cases (informational; P3 DATA-E3F): per variant,
#: in this order, a delivery shown by media and its description (matches),
#: a partial delivery and a repeated promise (neither matches).
FOLLOW_UP_IDS = tuple(f"m{n:03d}" for n in range(31, 46))
FOLLOW_UP_BLOCKS = ("uz-Latn", "en", "ru", "uz-Cyrl", "uz-ru-mix")
#: Media kind of the delivered case and of the partial case, per block (the
#: spec table of section 6.2; never "voice").
FOLLOW_UP_MEDIA = {
    "uz-Latn": ("document", "photo"),
    "en": ("photo", "document"),
    "ru": ("document", "photo"),
    "uz-Cyrl": ("photo", "document"),
    "uz-ru-mix": ("document", "photo"),
}
#: Blocks whose delivered case sends bare media first and the text after it.
FOLLOW_UP_BARE = frozenset({"en", "uz-Cyrl"})
MAX_DESCRIPTION_CHARS = 120


@needs_monitor_cases
def test_e3_v1_1_has_three_follow_up_cases_per_variant_in_block_order() -> None:
    by_id = {c.id: c for c in load_monitor_cases()}

    assert set(FOLLOW_UP_IDS) <= set(by_id)
    variants = [by_id[case_id].variant for case_id in FOLLOW_UP_IDS]
    assert variants == [v for v in FOLLOW_UP_BLOCKS for _ in range(3)]


@needs_monitor_cases
def test_e3_v1_1_follow_up_cases_have_their_shape_and_label() -> None:
    by_id = {c.id: c for c in load_monitor_cases()}
    descriptions: list[str] = []

    for position, case_id in enumerate(FOLLOW_UP_IDS):
        case = by_id.get(case_id)
        assert case is not None, case_id
        variant = FOLLOW_UP_BLOCKS[position // 3]
        slot = position % 3
        media = [m.media for m in case.burst if m.media is not None]
        assert len(case.monitors) == 1, case_id
        assert case.monitors[0].kind == "semantic", case_id
        descriptions.append(case.monitors[0].description)
        assert 1 <= len(descriptions[-1]) <= MAX_DESCRIPTION_CHARS, case_id
        assert all(m.direction == "contact" for m in case.burst), case_id
        if slot == 0:  # delivered: media and its description
            assert case.labels.matched_monitor_ids == [1], case_id
            assert media == [FOLLOW_UP_MEDIA[variant][0]], case_id
            if variant in FOLLOW_UP_BARE:
                assert len(case.burst) == 2, case_id
                assert case.burst[0].text is None, case_id
                assert case.burst[1].text, case_id
            else:
                assert len(case.burst) == 1, case_id
                assert case.burst[0].text, case_id
        elif slot == 1:  # partial delivery
            assert case.labels.matched_monitor_ids == [], case_id
            assert media == [FOLLOW_UP_MEDIA[variant][1]], case_id
            assert 1 <= len(case.burst) <= 2, case_id
        else:  # a repeated promise is not delivery
            assert case.labels.matched_monitor_ids == [], case_id
            assert not media, case_id
            assert 2 <= len(case.burst) <= 3, case_id
    assert len(set(descriptions)) == len(descriptions)
```

| Test or check | Before your data (red) | After (green) |
|---|---|---|
| `test_e3_v1_1_has_three_follow_up_cases_per_variant_in_block_order` | fails: `m031` is not in the file | passes: 3 cases per variant in the order `uz-Latn`, `en`, `ru`, `uz-Cyrl`, `uz-ru-mix` |
| `test_e3_v1_1_follow_up_cases_have_their_shape_and_label` | fails on `m031` (`case is not None`) | passes: 5 matching delivered cases, 5 partial and 5 promise cases not matching; media kind per block, bare or caption form, 1 distinct description of 1–120 characters per case |
| `test_e3_monitor_cases_meet_the_minimum_counts` (existing) | passes: 30 cases, 15 matching (50 %) | passes: 45 cases, 9 per variant, 20 matching (44.4 %) |
| every other test in `tests/unit/test_e3_dataset.py`, `tests/unit/test_e1_dataset.py` | passes | passes (manifest regenerated) |
| `tests/integration/test_eval_monitors.py::test_a_model_answering_the_labels_meets_the_bar_on_every_e3_case` (existing) | passes on 30 cases | passes on 45 cases: one model call per case, every case answered, accuracy 45/45 |
| `p3data_check.py scan evals/datasets/e3/monitor_cases.jsonl m031-m045` | 15 × `not found`, `exit 1` | `ok`, `exit 0` |

The counts in the green column are what §6.2 produces; if one differs, your data differs from §6.2.

## 8. Truthfulness edge cases

- **Half a delivery is not a delivery.** Slot 2's label is "not matching" because the owner asked for all of it; its caption says plainly that the rest comes later.
- **Talking about it is not sending it.** Slot 3 carries no media and no attachment; a promise, an apology or a new time is never labelled a match.
- **A label must be what a careful person concludes,** never what a keyword suggests: the delivered case uses the words of the awaited thing, and so may the other two; only the substance differs.
- **The model can only see what the product shows it:** a bare photo or document reaches it as `(photo)` or `(document)`, so slot 1's bare form adds a text saying what it is, and no case relies on a voice note.
- **Informational means informational.** These cases change no threshold and no gate; the README and the log row say so, and they say the owner has not reviewed v1.1 yet.
- **The autonomy gate is untouched.** EV-GATE compares only the `e1/`, `e2/` and `e4/` digests (EV-GATE §3.1), so landing on D+5, while the owner rates the frozen candidate, changes nothing there.
- **Runs on v1 and v1.1 are not comparable.** `python -m evals compare` refuses monitor runs whose `monitor_cases.jsonl` digests differ (`MONITOR_COMPARISON`); that is intended, and the README section says so.
- **Counts come from the loader** (the count command), never from your plan.

## 9. Acceptance checklist

- [ ] `git diff --stat origin/int/p3...HEAD` (or `origin/main...HEAD`) lists only the five files of §3.
- [ ] `m001`–`m030` are byte-for-byte unchanged (`git diff` shows only added lines in `monitor_cases.jsonl`).
- [ ] `m031`–`m045` follow the §6.2 tables (variant block, shape, media kind, label); every burst message is `contact`; every description is one line, in the owner's language, and distinct.
- [ ] `just test tests/unit/test_e1_dataset.py tests/unit/test_e3_dataset.py -q` → all passed; the two new tests were seen failing first (the step 3 commit); one condition per `assert` (ruff PT018).
- [ ] `tests/integration/test_eval_monitors.py` passed locally, or the PR says it was left to CI and why.
- [ ] `ruff check` and `ruff format --check` clean on `tests/unit/test_e3_dataset.py`.
- [ ] The scan of `m031-m045` → `ok`; the manual read found no real name.
- [ ] E3 reviewed the sample before `m041`–`m045` were written.
- [ ] The manifest was regenerated in **every** data commit.
- [ ] The E3 README section and counts, and the dataset-log row, exist, with loader numbers.

## 10. When to stop and ask

Comment on the DATA-E3F issue starting with "Question for E3:" (plain text, no `@`). If nobody answers within 2 h, David raises it with the TL. Stop and ask when:
- the E3 tests skip, or `evals/datasets/e3/monitor_cases.jsonl` is missing on your base (CP5 not on it);
- you are unsure whether to branch from `int/p3` or `main` (R1 may be merging the same day);
- a seed cannot be written so that a careful person would not hesitate over its label;
- `tests/integration/test_eval_monitors.py` fails on your data for a reason this spec does not explain;
- you are unsure whether a word, name or place is real;
- a rebase conflict touches a `.jsonl` file;
- you need a file outside §3;
- you have spent more than half a day and are not close.

## 11. Coding rules

- No Python, `uv`, `pytest` or `alembic` on the Windows host, not even for a one-off text edit (AGENTS §4); edit with your editor tool; run every check, including the count command, in the container.
- The test block: typed signatures, no broad `except`, no `assert` outside tests (in tests `assert` is the check), no new import, ids only in assertion messages (never dataset text).
- Never read `.env`, `var/`, backups or real chat data; never run anything against the production bot or server; never start a live eval (a `monitors` run is the TL's or SQ's, with the owner's go-ahead).
- Every `gh` body from a file (`--body-file`); no bare `@names`; commit messages from a file, Conventional Commits, your tool's attribution line if it uses one; never `--no-verify`; `$env:COMPOSE_PROJECT_NAME = 'd2'` set before committing.
- No real names, phone numbers, emails, links or `@handles` in the data; no content copied from anywhere.
- Out-of-list edits only in a separate last commit, with a comment on the issue.

## 12. Pull request

- **Title:** `DATA-E3F: E3 v1.1 follow-up-style monitor cases (m031-m045)`.
- **Base:** `int/p3` (or `main` after R1). Draft at the sample (§6.4 step 6); ready at step 11.
- **Labels:** `team:david`, `eng:d2`; at the sample `needs-review:e3`; when ready `needs-review:e3` and `needs-qa`.
- **Size:** about 75 changed lines (15 data lines, the test block, the README, one log row); well under 400.
- **Body** (from a file, WORKFLOW §4.3 template): Unit (Tier: other; QA lane: after merge; Base: int/p3 or main); What changed (the §6.2 tables, the seeds used); Safety invariants touched: none (data and dataset tests only); Tests: the §7 table with outputs (counts and `ok` lines only, no dataset text); Shared lists updated: `MANIFEST.sha256` in every data commit, the dataset-log row, the test block; Data: no migration, no content column, no logs; Prompts, flags and settings: none; Docs: the E3 README section and counts; Interface changes: none; Open items for M07.md: any seed that read unnaturally in a language, and a note for the TL that these cases inform FUP-1 once a `monitors` run includes them.
