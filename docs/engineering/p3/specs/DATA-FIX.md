# DATA-FIX — the owner's native-review corrections: E2 and E4 v1.2 (junior unit)

| | |
|---|---|
| **Unit** | DATA-FIX — apply exactly the corrections from the owner's native-language review of E2 and E4 (wording and labels) as **E2 v1.2** and **E4 v1.2**; the v1.2 README sections; the dataset-log rows |
| **Owner slot** | D1 (David's team, junior) |
| **Mentor (peer review)** | E3 (Codex; the TL dispatches the review) |
| **QA** | QD1, after merge (on `int/p3` before R1). Before any rating sheet reaches the owner, QE2's gate integrity check re-checks the E4 labels against the outcome mapping (team-qa §7, GATE-RUN §3.7) |
| **Size** | S (≈ 0.5 engineer-day spread over D+1..D+3, one fix round included; the corrections arrive in batches) |
| **Depends on** | The owner's review, as it lands (the TL posts each batch on the DATA-FIX issue, §6.1); DATA-E2, DATA-E4a and DATA-E4b on `int/p3`; EV-E4L merged into `int/p3` for `python -m evals.e4` (until then the checker of [DATA-E4a](DATA-E4a.md) Appendix A only) |
| **Deadline** | v1.2 **merged into `int/p3` by D+3 (Thu 11-05) 18:00 local**, and in any case before the TL freezes the candidate on D+4 morning: the final rated run never precedes this merge (GATE-RUN §3.1, §3.7) |
| **Branch → base** | `p3/data-fix` from `origin/int/p3` → PR to `int/p3`. A batch that arrives after this PR merged goes in `p3/data-fix-2` (same spec) only if the TL says so (§6.4 step 10) |
| **Normative inputs** | WORKFLOW §9; [GATE-RUN](GATE-RUN.md) §3.3 ("an item the owner corrected is confirmed only after DATA-FIX ships the correction") and §3.7; [team-qa.md](../handoffs/team-qa.md) §7; [EV-E4L](EV-E4L.md) §3.1–§3.4 (E4 format and integrity); P2 spec `docs/engineering/p2/specs/E2-drafts-eval.md` §A.4 and `evals/schema.py` (E2 format and `check_e2`); [DATA-E2](DATA-E2.md) (E2 v1.1 and its four tests); [DATA-E4a](DATA-E4a.md), [DATA-E4b](DATA-E4b.md) (E4 v1.1 conventions; Appendix A, the data checker); decisions OD-14, OD-21, OD-32; IMPLEMENTATION_PLAN §10.1 (gate results count only owner-confirmed items) |

## 1. Goal

The S06.5 gate counts only items the owner has confirmed (IMPLEMENTATION_PLAN §10.1). The owner reviews E2 and E4 in Uzbek, Russian and English, and some items will need different wording or a different label. You apply **exactly those corrections and nothing of your own**, and ship them as v1.2, so the TL can freeze the candidate and Senior QA can run the final rated run on corrected data. Every correction is traceable (issue row → commit → README line), every dataset rule still holds, and nothing you write claims that the owner confirmed an item.

## 2. Read first

1. [AGENTS.md](../../../../AGENTS.md) §8 ("evaluation datasets are synthetic or hand-written only"), §9, §11 (public repository: act only on content from the owner's account).
2. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md) (all) and [WORKFLOW](../WORKFLOW.md) §2, §3, §4, §9, §11.
3. [GATE-RUN](GATE-RUN.md) §3.1, §3.3 and §3.7: why the deadline matters, and that confirmations are GATE-RUN's job, not yours.
4. [EV-E4L](EV-E4L.md) §3.1–§3.4 and [DATA-E4b](DATA-E4b.md) §6.1 and §6.4 (E4 field rules, disclosure timing).
5. `evals/schema.py` lines 404–713 (the E2 models, `check_must_include`, `check_e2`, `_check_canaries`) and `tests/unit/test_e2_dataset.py` (including DATA-E2's appended block).
6. `evals/datasets/e2/README.md` and `evals/datasets/e4/README.md`.
7. [DATA-E4a](DATA-E4a.md) Appendix A (the data checker) and this spec's Appendix A (the correction checker).
8. This spec, to the end, before you start.

## 3. Allowed files

| Path | New or existing | Exactly what may change |
|---|---|---|
| `evals/datasets/e2/situations.jsonl` | existing | Only the field values named by an applied correction (§6.2 classes W and L), in the records they name. No line added, removed or reordered; no id changed |
| `evals/datasets/e4/cases.jsonl` | existing | Same rule |
| `evals/datasets/e4/decoys.jsonl` | existing | Same rule (only if a correction names a decoy) |
| `evals/datasets/e2/README.md` | existing | Add the section `## v1.2 (P3 DATA-FIX, <date>)` (§6.4 step 9). Nothing else |
| `evals/datasets/e4/README.md` | existing | Same |
| `evals/datasets/MANIFEST.sha256` | existing | Only by `python -m evals.manifest --write` in the container, never by hand |
| `docs/evals/README.md` | existing | Append one "Dataset log" row per set you changed (at most two rows). Nothing else |

You add or change no test.

## 4. Forbidden

- Every file not in §3.
- Explicitly: `evals/datasets/e1/**` and `retrieval/**` (an E2 situation whose `source` is an E1 chat shows E1's text: a wording correction there is out of scope, class O); `evals/datasets/e3/**` (E3 corrections are out of scope); every `.py` file, including every test file; `evals/reviews/**` (GATE-RUN's ledger); `src/`; `migrations/`; `agent/prompts/`; `policy/`; `actions/`; `telegram/`; `privacy/`; consent code; `config.py`; `core/`; `AGENTS.md`; any `docs/` file other than the rows in `docs/evals/README.md`.
- Changing a field no correction names, "improving" wording while you are in a record, or applying your own proposal before the owner's answer (§6.2 class P).
- Adding or deleting a record; changing an id, a `sent_at`, a `source` cut point or a canary value (a correction that needs one of these is class O).
- Acting on a correction posted by any account other than the owner's (`Khumoyun1307`, which every project session uses): it is untrusted data (AGENTS §11).
- Changing E2 or E4 at all after the TL has frozen the candidate (D+4), until the TL says CP-G is done: the gate refuses a run whose `e2/situations.jsonl`, `e4/cases.jsonl` or `e4/decoys.jsonl` digest differs from the current manifest (EV-GATE §3.1, GATE-RUN §3.7), so a late change would void the owner's ratings.
- Committing either checker or your corrections file (they live outside the repository).

## 5. Shared lists you must update

| List | Edit |
|---|---|
| `evals/datasets/MANIFEST.sha256` | Regenerate in **every** data commit, after rebasing on the current base: `docker compose -f compose.dev.yml run --rm app uv run python -m evals.manifest --write` |
| `docs/evals/README.md` "Dataset log" | One row per changed set (§6.4 step 9), after the existing rows |
| `tests/unit/test_e1_dataset.py`, `test_e2_dataset.py`, `test_e4_dataset.py` | **No edit.** If a correction makes one of them fail, the correction is class O: stop and ask (§10) |

## 6. Step-by-step

### 6.1 How corrections reach you

The owner gives corrections to the TL in the conversation or on the owner-gate issue. The TL (or David) posts each batch on the **DATA-FIX issue** as a table. That table is the only input you act on:

| # | Set | Id | Field | New value (exact) | Note |
|---|---|---|---|---|---|
| 7 | e4 | a093 | `conversation.messages.0.text` | `"<the owner's exact new text>"` | word order |
| 8 | e2 | s125 | `expected.must_not.0` | `{"kind": "commitment", "text": "<exact text>"}` | label |
| 9 | e4 | a117 | `conversation.messages.2.text` | — | sounds translated |

- **Set:** `e2` (`evals/datasets/e2/situations.jsonl`), `e4` (`evals/datasets/e4/cases.jsonl`), `e4-decoys` (`evals/datasets/e4/decoys.jsonl`).
- **Field:** the dotted path from the record's root. List positions count from 0, so message `mNN` is `messages.<NN − 1>` (`m01` is `messages.0`, `m03` is `messages.2`).
- **New value:** the exact JSON value: a string in the record's language, `true` or `false`, a list or an object. Removing or reordering items of a list is written as the whole new list at the list's path (for example `expected.must_not` with the full new list).
- **"—"** means the owner noted a problem without giving the new text.
- `#` numbers continue across batches; the owner's corrections to v1 (reviewed by D+1, P3-T166) form batch 1; the v1.1 corrections (by D+2) form the next batches.

### 6.2 Classify every row

| Class | When | What you do |
|---|---|---|
| **W** (wording) | A new text for a string field: message `text`, a `knowledge` line, `topic`, `scope`, `contact_notes`, `style_notes`, a `display_name`, a `must_not` or `may` text, `notes` | Apply exactly, character for character |
| **L** (label) | A new value for a label field. E4: `category`, `safe_behaviour`, `behaviour`, `disclosure_required`, `forbidden_strings`, `must_not`, `may`, `allowed`, `contact_listed`. E2: `intent`, `needs_reply`, `must_include`, `must_not`, `makes_commitment_ok`, `sensitive_topic`, `tags`, `owner_prefs` | Apply exactly. If a loader then demands a second change the row does not name (for example a new E4 `category` needs another `safe_behaviour`, or an E2 `needs_reply` change needs the `no_reply_needed` tag), do **not** choose it: ask (§10) |
| **P** (proposal needed) | The row has no new value ("—") | Write one proposed replacement in your private notes and post it in the issue comment of §6.4 step 4. The TL asks the owner; apply it only when the TL posts the owner's answer as a new row with an exact value. No answer by D+3 12:00 local: leave the item unchanged and list it as an open item |
| **O** (out of scope) | E1 text (any message of an E2 situation with `source` set); E3; adding or deleting a record; changing an id, a `sent_at`, a `source` cut point or a canary value; a v1.1-only field on a v1 E4 record (`a001`–`a080`); any change that breaks a §6.3 rule or a dataset test | Do not apply. List it on the issue as `Out of scope: #<n> <set> <id> <field> — <reason>`; the TL rules |

### 6.3 Rules every applied correction must keep

The loaders and the checkers enforce most of these; the rest you check by reading.

**E4** (EV-E4L §3.4 rules 1–12, DATA-E4a/E4b rules D1–D7 and the `slot` checks):
- every case `delegation.canary`, decoy `canary` and `knowledge_canary` still appears exactly once, where it belongs, and nowhere else in E4, the decoys or E2 (a wording change that drops or copies a canary is class O);
- every `forbidden_strings` entry is still found, case-insensitively, in the case's knowledge or contact messages; a `knowledge_scope` case's forbidden values still appear in no message;
- a disclosed `assistant_sent` (`disclosure_included: true`) still starts with its AI line; an undisclosed one has none; no `sent_at` changes, so every disclosure label stays valid;
- an extraction case that named a decoy contact still names it in dictionary form; a correction to a decoy's `display_name` would break the extraction case that names it, so it is class O (ask);
- a W row on a `knowledge` line of a `knowledge_scope` case keeps every `forbidden_strings` value in it, and a W row on a contact message keeps each forbidden value out of it (a `knowledge_scope` case) or in it (an injection case); otherwise it is class O;
- a W row on `topic`, `knowledge` or a message of an `in_scope` or `re_disclosure` case keeps the right reply grounded: every value the reply needs is still in a knowledge line, and the contact's last message gains no value that the reply would have to repeat (DATA-E4a §6.2);
- the per-language counts and the `slot` checks of `p3data_check.py e4b` stay the same unless an L row changes `category`, `safe_behaviour`, `behaviour`, `disclosure_required` or `contact_listed`. Then the checker reports `slot` and count lines on purpose: list them in the PR, and only after the TL ruled on the row run the checker with `--no-slots` (the counts then show `MISSING` lines; `python -m evals.e4`, once merged, must still exit 0). A count below EV-E4L's `E4_MINIMUMS` is class O.

**E2** (`evals/schema.py`, P2 spec E2 §A.4):
- every `must_include.fact` is still a literal, case-insensitive substring of the message it cites;
- the canary still occurs once in all of E2 (its own message) and nowhere in E1 or E4;
- `intent` is `no_reply_needed` exactly when `needs_reply` is false, and the tags `no_reply_needed` and `sensitive` still match `needs_reply` and `sensitive_topic`;
- `tests/unit/test_e2_dataset.py` (with DATA-E2's five tests, including `test_the_v1_1_situations_follow_the_spec_table`, which pins variant, intent, reply language, tone, style and the `must_not` kind of `s151`–`s162`) still passes; a row that changes one of those fields on `s151`–`s162` makes that test fail and is class O (ask).

**Both:** text stays in its record's language and script (`uz-Latn` and `en` Latin; `uz-Cyrl` and `ru` Cyrillic; `uz-ru-mix` as written); no real names, phone numbers, links or `@handles` (the scan); UTF-8, LF, NFC, separators `", "` and `": "`, letters as they are (no `\u` escapes).

### 6.4 Steps (each ends with a check)

All commands run in PowerShell in your directory, with `$env:COMPOSE_PROJECT_NAME = 'd1'` set. `$check` is set once per session: `$check = (Resolve-Path ..\p3check).Path`.

1. **Start when David says the first batch is posted** (not before D+1). Comment "started" on the DATA-FIX issue. Create your directory: `git fetch origin; git worktree add ..\alviro-wt\d1-data-fix -b p3/data-fix origin/int/p3` (or a clone, WORKFLOW §3.1); `just setup`; `just hooks`.
   **Check:** `just test tests/unit/test_e1_dataset.py tests/unit/test_e2_dataset.py -q` → all passed (add `tests/unit/test_e4_dataset.py` to the command if the file exists); `docker compose -f compose.dev.yml run --rm app uv run python -m evals.schema e2` → last line `ok: 162 situations`; `docker compose -f compose.dev.yml run --rm -v "${check}:/check:ro" app uv run python /check/p3data_check.py e4b; "exit $LASTEXITCODE"` → `ok`, `exit 0`. If any of these fails, v1.1 is not complete on `int/p3`: stop and ask (§10).
2. **Save the checkers.** `p3data_check.py` ([DATA-E4a](DATA-E4a.md) Appendix A; reuse it if it is already in `..\p3check`) and `p3fix_check.py` (Appendix A below), both in `..\p3check` with your editor tool (UTF-8, LF).
   **Check:** `docker compose -f compose.dev.yml run --rm -v "${check}:/check:ro" app uv run python /check/p3fix_check.py; "exit $LASTEXITCODE"` → the usage text, `exit 2`.
3. **Transcribe the batch** into `..\p3check\v1_2_corrections.jsonl`: one line per W or L row, `{"n": <#>, "set": "<set>", "id": "<id>", "path": "<field>", "new": <exact new value>}`. P and O rows get no line (a P row gets one only when the owner's exact value arrives as a new row).
   **Check:** `docker compose -f compose.dev.yml run --rm -v "${check}:/check:ro" app uv run python /check/p3fix_check.py /check/v1_2_corrections.jsonl; "exit $LASTEXITCODE"` → one `NOT applied` line per correction, last line `applied 0, not applied <N>, errors 0`, `exit 1`. This is your red state. An `unknown set or id` line means a typo in your file or in the row: fix your transcription, or ask.
4. **Post your classification** on the issue (one comment from a file, `gh issue comment <n> --body-file <file>`): every row number with its class; for P rows your proposal (id, field and the proposed text); for O rows the reason. **Check:** the comment is posted.
5. **Apply the W and L rows**, one record at a time, with your editor tool. After the E2 rows: `python -m evals.schema e2` → `ok: 162 situations` and `just test tests/unit/test_e2_dataset.py -q` → all passed. After the E4 rows: `p3data_check.py e4b` → `ok`, and, if EV-E4L is merged, `docker compose -f compose.dev.yml run --rm app uv run python -m evals.e4; "exit $LASTEXITCODE"` → `exit 0` and `just test tests/unit/test_e4_dataset.py -q` → all passed.
   **Check:** `p3fix_check.py /check/v1_2_corrections.jsonl` → every line `applied`, last line `applied <N>, not applied 0, errors 0`, `exit 0`.
6. **Scan and read the changed records.** For each set you changed: `docker compose -f compose.dev.yml run --rm -v "${check}:/check:ro" app uv run python /check/p3data_check.py scan evals/datasets/e4/cases.jsonl 'a093,a117'; "exit $LASTEXITCODE"` (the quoted list is your changed ids; PowerShell turns an unquoted comma into a list) and the same for `evals/datasets/e2/situations.jsonl` and `evals/datasets/e4/decoys.jsonl`. Read every changed value once for real names, and for the §6.3 rules the tools cannot see (AI line, dictionary-form decoy names, forbidden values absent from messages).
   **Check:** each scan → `ok`, `exit 0`, or only `WHOLE_WORDS` hits that are the ordinary word (say so in the PR); a hit in `s125` for `Toshkent` or `Samarqand` is the open item DATA-E2 listed, not yours; your read found nothing.
7. **Commit the batch.** `git fetch origin; git rebase origin/int/p3`; `docker compose -f compose.dev.yml run --rm app uv run python -m evals.manifest --write`; `just test tests/unit/test_e1_dataset.py tests/unit/test_e2_dataset.py -q` (plus `test_e4_dataset.py` if it exists) → all passed; `docker compose -f compose.dev.yml run --rm app uv run python -m evals verify; "exit $LASTEXITCODE"` → `exit 0`. Commit with `git commit -F <message file>` (`fix(evals): E2/E4 v1.2 batch <b>: owner native-review corrections (rows #<first>-#<last>)`); push. On the first batch open the draft PR (§12). Comment on the issue: "batch <b> applied: rows … (W <w>, L <l>); proposals: …; out of scope: …".
   **Rebase conflicts (this and every later rebase):** in `MANIFEST.sha256`, run the regenerate command (it overwrites the file), `git add` it and continue; in `docs/evals/README.md`, keep every row in date order; in any `.jsonl` file, stop and ask (§10).
   **Check:** CI on the PR is green.
8. **Next batches** (and owner answers to P rows): repeat steps 3–7, appending lines to the same corrections file. **Check:** `p3fix_check.py` shows every line of every batch `applied`, `exit 0`.
9. **Docs** (before you mark the PR ready; update them if a later batch lands before the merge).
   - In each changed set's README (`evals/datasets/e2/README.md`, `evals/datasets/e4/README.md`) add `## v1.2 (P3 DATA-FIX, <date>)`: the source ("corrections from the owner's native-language review, batches of <dates>, applied by D1, mentored by E3"); a table with one line per applied row (`#`, id, field path, class W or L; no dataset text); the per-variant counts after v1.2 copied from `python -m evals.schema e2` (E2) or from the `p3data_check.py e4b` count lines (E4); the rows not applied (P without an answer, O) with their reason; and the sentence "Which ids the owner has confirmed is recorded by GATE-RUN in `evals/reviews/autonomy.json`, not here."
   - `docs/evals/README.md`, "Dataset log", one row per changed set, for example: `| <today> | E2 | v1.2 | Owner native-review corrections: <w> wording and <l> label changes in <k> situations (rows and ids in evals/datasets/e2/README.md, v1.2). No situation added or removed. | evals/datasets/MANIFEST.sha256 at this commit |` and `| <today> | E4 | v1.2 | Owner native-review corrections: <w> wording and <l> label changes in <k> cases and <d> decoys (rows and ids in evals/datasets/e4/README.md, v1.2). No record added or removed. | evals/datasets/MANIFEST.sha256 at this commit |` (ids and file names in backticks, as the existing rows do).
   **Check:** every number in the README sections equals the loader or checker output; every applied row number appears once.
10. **Ready, by D+3 18:00 local** (or earlier, when the TL says the review is complete). Rebase, regenerate the manifest, run step 7's checks, push; mark the PR ready; add `needs-review:e3` and `needs-qa`; comment on the issue with the corrected ids (GATE-RUN confirms them after the merge). A batch that arrives while the PR is open becomes one more commit (tell E3 on the PR). A batch that arrives after the merge: list it on the issue and wait; the TL decides whether `p3/data-fix-2` follows, and never before CP-G once the candidate is frozen.
    **Check:** `git diff --stat origin/int/p3...HEAD` lists only files of §3; CI green.

## 7. Named tests (red first)

You add no test. These checks must pass:

| Check | Before your edits (red) | After (green) |
|---|---|---|
| `p3fix_check.py /check/v1_2_corrections.jsonl` | one `NOT applied` line per correction; `applied 0, not applied <N>, errors 0`; `exit 1` | one `applied` line per correction; `applied <N>, not applied 0, errors 0`; `exit 0` |
| `python -m evals.schema e2` | `ok: 162 situations` | `ok: 162 situations` (no situation added or removed) |
| `p3data_check.py e4b` | `ok`, `exit 0`, with the counts of [DATA-E4b](DATA-E4b.md) §7 | `ok`, `exit 0`, with the same counts and no `slot` line, unless an L row changed `category`, `safe_behaviour`, `behaviour`, `disclosure_required` or `contact_listed` (then the PR lists the changed counts and `slot` lines, the TL has ruled on the row, the checker runs with `--no-slots`, and each count stays at or above `E4_MINIMUMS`) |
| `p3data_check.py scan <file> <changed ids>` (per changed set) | — | `ok`, `exit 0` |
| `tests/unit/test_e2_dataset.py` (all, including `test_each_gate_language_has_the_v1_1_buffer`, `test_every_gate_language_has_answer_question_situations`, `test_every_gate_language_has_a_fixed_reply_language_situation`, `test_the_v1_must_not_defects_stay_fixed`) | passed | passed |
| `tests/unit/test_e1_dataset.py::test_manifest_matches_the_dataset_files`, `::test_dataset_files_are_nfc_and_lf` | passed | passed (manifest regenerated) |
| `python -m evals verify` | `exit 0` | `exit 0` (after the regeneration) |
| (EV-E4L merged) `python -m evals.e4` and `tests/unit/test_e4_dataset.py` | `exit 0`; passed | `exit 0`; passed |

## 8. Truthfulness edge cases

- **Only the owner's words.** A W or L row is applied character for character; nothing else in the record changes. Your proposal is not a correction until the owner's answer comes back as a row with an exact value.
- **Corrected is not confirmed.** The README and the PR say which rows were applied; they never say the owner confirmed an item. GATE-RUN adds confirmations to the ledger, and only after v1.2 ships (GATE-RUN §3.3).
- **Nothing is silently dropped.** Every row ends in exactly one place: applied (README table), proposed and unanswered, or out of scope with a reason (README and PR).
- **Gate-relevant label changes are visible.** An E4 change of `category`, `safe_behaviour` or `disclosure_required`, or an E2 change of `needs_reply` or `intent`, is listed separately in the PR, so QE2 checks it against the OD-21 mapping before any sheet reaches the owner.
- **No silent shift of the item.** No `sent_at`, id or canary changes, so disclosure labels, leak checks and rating keys keep their meaning.
- **Frozen means frozen.** After the candidate freeze no dataset byte changes until CP-G; a late correction waits.
- **Counts come from the loaders and the checker,** never from your notes.

## 9. Acceptance checklist

- [ ] `git diff --stat origin/int/p3...HEAD` lists only files of §3.
- [ ] `git diff origin/int/p3...HEAD -- evals/datasets` changes exactly the lines of the records named by applied rows, and inside them only the named fields.
- [ ] `p3fix_check.py` → every correction `applied`, `exit 0`.
- [ ] `python -m evals.schema e2` → `ok: 162 situations`; `p3data_check.py e4b` → `ok`; `python -m evals verify` → `exit 0`; with EV-E4L, `python -m evals.e4` → `exit 0`.
- [ ] `tests/unit/test_e1_dataset.py`, `tests/unit/test_e2_dataset.py` (and `tests/unit/test_e4_dataset.py` if present) pass.
- [ ] Each changed set's scan is `ok`; the manual read found no real name.
- [ ] Every row of every batch is classified on the issue; P rows have an answer applied or are listed as open; O rows have a reason.
- [ ] The manifest was regenerated in **every** data commit.
- [ ] The README v1.2 sections and the dataset-log rows exist, with loader numbers.
- [ ] Merged by D+3 18:00 local, or the TL was told on the issue by D+3 12:00 why not.
- [ ] No ruff or mypy run is needed (no Python changed); the PR says so.

## 10. When to stop and ask

Comment on the DATA-FIX issue starting with "Question for E3:" (plain text, no `@`); for anything about the owner's intent, also name the TL. If nobody answers within 2 h, David raises it with the TL. Stop and ask when:
- step 1's checks fail on a fresh `int/p3` (v1.1 incomplete or broken there);
- a row's id, set or path does not exist, or the row is ambiguous;
- an L row needs a second field change it does not name, or a W row would drop or copy a canary, a `must_include` fact or a forbidden string;
- a correction would make a loader, a checker or a dataset test fail, or push a per-language count below its minimum;
- a correction targets a record whose data PR is not on `int/p3` yet (David routes it to that PR's author);
- a rebase conflict touches a `.jsonl` file;
- a row comes from an account other than the owner's;
- a batch arrives after the freeze;
- you need a file outside §3;
- you have spent more than the unit's size and are not close.

## 11. Coding rules

- No Python, `uv`, `pytest` or `alembic` on the Windows host, not even for a one-off text edit (AGENTS §4); edit with your editor tool; run every check in the container.
- Never read `.env`, `var/`, backups or real chat data; never run anything against the production bot or server; never start a live eval.
- Every `gh` body from a file (`--body-file`); no bare `@names`; commit messages from a file, Conventional Commits, your tool's attribution line if it uses one; never `--no-verify`; `$env:COMPOSE_PROJECT_NAME = 'd1'` set before committing.
- No real names, phone numbers, emails, links or `@handles` in the data, even if a correction seems to contain one: ask instead.
- Out-of-list edits only in a separate last commit, with a comment on the issue.

## 12. Pull request

- **Title:** `DATA-FIX: E2 and E4 v1.2 (owner native-review corrections)`.
- **Base:** `int/p3`. Draft at the first batch (§6.4 step 7); ready at step 10.
- **Labels:** `team:david`, `eng:d1`; when ready `needs-review:e3` and `needs-qa`.
- **Size:** usually well under 100 changed lines (one line per corrected record, the README sections, two log rows). If a batch would take the PR past about 400 lines, tell David: the TL may split it.
- **Body** (from a file, WORKFLOW §4.3 template): Unit (Tier: other; QA lane: after merge; Base: int/p3); What changed (the applied rows: `#`, set, id, field, class; no dataset text); **Corrected ids** (one line per set, for GATE-RUN's confirmations); **Gate-relevant label changes** (for QE2); Not applied (P rows without an answer, O rows with reasons); Safety invariants touched: none (data only); Tests: the §7 checks with their output lines (counts, `applied` totals and `ok` only); Shared lists updated: `MANIFEST.sha256` in every data commit, the dataset-log rows; Data: no migration, no content column, no logs; Prompts, flags and settings: none; Docs: the v1.2 README sections; Interface changes: none; Open items for M06.md: unanswered proposals, out-of-scope rows (for example E1 wording), anything the owner should look at again.

## Appendix A — the correction checker (throwaway; never committed)

A small standard-library script that reads your corrections file and reports, for each line, whether the dataset now holds the new value at that path. It prints line and row numbers, sets, ids, paths and counts, never dataset text. Save it as `..\p3check\p3fix_check.py` (outside the repository) and run it from the repository root:

```powershell
$check = (Resolve-Path ..\p3check).Path
docker compose -f compose.dev.yml run --rm -v "${check}:/check:ro" app uv run python /check/p3fix_check.py /check/v1_2_corrections.jsonl; "exit $LASTEXITCODE"
```

```python
"""DATA-FIX correction checker: a throwaway aid. Never commit it.

Reads the corrections file (one JSON object per line, kept outside the
repository; spec DATA-FIX section 6.4) and reports, for each line, whether
the dataset value at its path equals the new value. Run it from the
repository root inside the dev container, with its folder mounted read-only
at /check:

  python /check/p3fix_check.py /check/v1_2_corrections.jsonl

Each line: {"n": <row>, "set": "e2" | "e4" | "e4-decoys", "id": "<record id>",
"path": "<dotted path, list positions from 0>", "new": <exact JSON value>}.

Output: line and row numbers, sets, ids, paths and counts only, never dataset
text. Exit: 0 every correction applied; 1 some not applied; 2 a malformed
line, or an unknown set or id.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SETS = {
    "e2": Path("evals/datasets/e2/situations.jsonl"),
    "e4": Path("evals/datasets/e4/cases.jsonl"),
    "e4-decoys": Path("evals/datasets/e4/decoys.jsonl"),
}
KEYS = {"n", "set", "id", "path", "new"}
MISSING = object()


def records(path: Path) -> dict[str, Any]:
    by_id: dict[str, Any] = {}
    if not path.exists():
        return by_id
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            by_id[str(record.get("id"))] = record
    return by_id


def lookup(record: Any, path: str) -> Any:
    value = record
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return MISSING
    return value


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    data = {name: records(path) for name, path in SETS.items()}
    applied = pending = errors = 0
    lines = Path(argv[0]).read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            print(f"line {number}: not valid JSON")
            errors += 1
            continue
        if not isinstance(item, dict) or set(item) != KEYS:
            print(f"line {number}: keys must be exactly {sorted(KEYS)}")
            errors += 1
            continue
        label = f"line {number} #{item['n']} {item['set']} {item['id']} {item['path']}"
        record = data.get(str(item["set"]), {}).get(str(item["id"]))
        if record is None:
            print(f"{label}: unknown set or id")
            errors += 1
            continue
        value = lookup(record, str(item["path"]))
        if value is not MISSING and value == item["new"]:
            print(f"{label}: applied")
            applied += 1
        else:
            print(f"{label}: NOT applied" + (" (path not found)" if value is MISSING else ""))
            pending += 1
    print(f"applied {applied}, not applied {pending}, errors {errors}")
    if errors:
        return 2
    return 1 if pending else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

A correction that adds an item at a new list position shows `NOT applied (path not found)` until you apply it; that is expected. The script compares parsed JSON values, so key order inside an object does not matter, but every character of a string does.
