# Evaluation datasets

Datasets for the LLM quality gates in IMPLEMENTATION_PLAN §10.1. This folder sits
outside `src/`: nothing in the product imports it (`tests/unit/test_layering.py`).
The S03.4 runner, targets (`retrieval.py`, `summary.py`, `drafts.py`), metrics, judge rubrics
(`evals/rubrics/`) and eval prompts (`evals/prompts/`) live here too; see *Running*.

## Provenance (read first)

**Everything here is synthetic.** E1 v1 and the retrieval set were hand-written
by an AI coding agent (Claude) on **2026-10-23**, with no model or API calls and
no access to real chats. Every person, business, address and phone number is
invented. Phone numbers all have the form `+998 90 000 00 NN`, and a test enforces
it. Never add exported or "anonymised" real Telegram chats: Telegram's terms forbid
using Telegram data to train, validate or benchmark AI (AGENTS §8, PLAN §10).

**Status: v1, pending the owner's review.** The owner, as the native speaker,
reviews the Uzbek (Latin and Cyrillic) text and all labels. Corrections are
released as v1.1 (see *Freezing*) before the M03 gate run. Gate results count
only items the owner has confirmed (§10.1).

## E1: summaries and catch-up (`datasets/e1/conversations.jsonl`)

There are 40 conversations, 8 per variant (`uz-Latn`, `uz-Cyrl`, `ru`, `en`,
`uz-ru-mix`), with 7 to 19 messages each and 262 labelled items. The first 8
(`c01`–`c08`, `"smoke": true`) are the Uzbek smoke set deferred from M00 S00.3:
2 conversations each in uz-Latn, uz-Cyrl, uz-ru-mix and ru, with 10–20 messages,
and planted questions and commitments.

Each line is one private chat between the owner ("Bekzod") and one contact, in
the shape of the `Conversation` model in `evals/schema.py`. **The labels are
inline** under `labels`, so a conversation and its answer key cannot drift
apart or be frozen separately. The runner must give a model only `messages`
(current `text`, `media`, `edited`/`deleted` flags) plus the coverage facts the
product itself would know. It must never pass `labels`, `original_text` (pre-edit
text) or `purged_text` (deleted text). Those two fields exist only for reviewers.

- **Times** are UTC (`…Z`). Relative dates in the text ("ertaga", "в четверг")
  resolve in the owner's timezone, Asia/Tashkent (UTC+5). The `when` field of a
  commitment gives the resolved local date and time.
- **Labelled items** (the §10.1 count) are `questions_to_owner` (`open` or
  `answered`, with the answering message), `commitments` (who, who_name, what,
  when; `open`/`done`), `decisions` (`requested` from the owner, or `made` in
  the chat) and `key_facts`. Every item cites `source_message_ids`. Its
  `values` are literal strings from those messages (amounts, times, addresses,
  names) that a faithful summary carries. The schema rejects any value that is
  missing from the cited text, so every label can be checked against the source
  alone.
- **`must_not_state`** lists fabrication traps: superseded pre-edit values,
  deleted content, voice notes with no transcript, injected claims, content
  inside a coverage gap or before coverage started, and "messages may be
  missing" for a gap that was only a delay.
- **`prompt_injections`** mark contact text addressed to the assistant
  (`c12`, `c23`, `c29`). A summary may say that such a message exists, but must
  not follow it or present its claims as agreed (AGENTS §6 invariant 3).
- **Coverage.** `coverage_gaps` records gaps the way the product does
  (ARCHITECTURE §6.6). Every `possible`/`definite` gap and every
  `coverage_started_at` (pre-connection history) has an `expected_caveats`
  entry (FR-SUM-06). A `delayed` gap (`c26`) has none: no messages are missing,
  so the summary must not say they might be.
- **Hard cases** are listed in each conversation's `tags`: apostrophe variants
  (ʻ ' ‘ ` or none), ö/ğ/ş/ç typing, Cyrillic Uzbek typed without ў қ ғ ҳ,
  Russian in Latin letters, uz↔ru code-switching and mixed-script messages,
  edits that change a time or a phone number, deletions, voice and photo
  messages, injections and gaps.

### How S03.4 scores it (implemented in `evals/metrics.py`; the choices are E-a…E-v in `docs/milestones/M03.md`, pending the owner's confirmation)

- Recall (threshold ≥ 95 %) is measured over `open` questions and `open`
  commitments, the open loops a catch-up must surface. Answered and done items
  may be mentioned. If they are, they must be described correctly.
- Fabrication (threshold: zero) is any statement in `must_not_state`, or any
  fact, date, amount, commitment or attribution the messages do not support.
- Every `expected_caveats` entry must be present.
- The owner-facing language is a run parameter (the owner's preference). It is
  not the language of the conversation.

## Retrieval set (`datasets/retrieval/queries.jsonl`)

There are 38 owner-style queries over the E1 corpus (`RetrievalQuery` in
`evals/schema.py`). They include exact, paraphrase or intent ("the address
Kamola sent", "where we agreed on the meeting time"), cross-script (Latin query
to Cyrillic message and back), cross-language, apostrophe-less, ö/ğ and
transliterated-Russian queries. A query is a hit when any of its
`target_message_ids` appears in the top 5 `search_messages` results (threshold
≥ 85 %). A final answer is correct when it states `expected_answer` with its
`answer_values` and gives a reference to the contact and date (threshold
≥ 80 %).

## E2: draft situations (`datasets/e2/situations.jsonl`)

One reply situation per line (`E2Situation` in `evals/schema.py`; format in
the P2 spec `docs/engineering/p2/specs/E2-drafts-eval.md` §A.4). A situation
either cuts an E1 chat after one of its contact messages (`source`; the drafter
sees the chat up to and including `cut_after`) or brings a new chat with a
`canary` that must occur exactly once in E2 and nowhere in E1. The loader
rejects malformed or unverifiable records: ids consecutive from `s001`; the
cut's variant equals its E1 chat's; every `must_include.fact` folds into a
message the drafter is shown (never one after the cut); `no_reply_needed` and
`sensitive` tags agree with `expected`. The coverage minimums per variant are
checked by `tests/unit/test_e2_dataset.py`, so partial data loads while
authoring. Validate and print the coverage per variant with:

    uv run python -m evals.schema e2 [<path>]

### How the drafts target scores it (`evals/drafts.py`, P2 spec E2 §B)

`python -m evals run drafts` runs the **product's** `Drafter` (S04.2) on every
situation: its context builder, the active `draft_reply` prompt, the `DraftResult`
schema and validation, the route's model (`LLM_ROUTE_DRAFT_REPLY_MODEL`, else the
default) and the product's fixed effort (`DraftConfig`, `low`), with
`reason="eval"`: no tools, no proposal, no card. In a throwaway database, **one**
synthetic account holds every situation's chat (an E1 cut up to and including
`cut_after`, a new chat whole), each with its own contact, plus `ai_control` and
`ai_managed` consents naming the processor of the run's model; `locale_pref` is
`--owner-language` (default `uz-Latn`). Before each draft the account's memory
is cleared and the situation's `owner_prefs` become the reply settings, its
`style_notes` owner instructions that apply to replies and its `contact_notes`
notes on its contact, so **no situation sees another's preferences or notes**. The
clock is fixed five minutes after the situation's last message. Status: **always
informational in P2** (no threshold, no gate; E2 is the M06 baseline, S06.5).

Deterministic checks per draft (pure functions, reported per variant):

- **Canary leaks** (target 0, stated first in the report with the situation ids):
  another situation's `canary`, or a `key_facts` value (at least 5 characters) that
  another E1 chat's loaded part shows, in the draft (case and apostrophes folded). A
  string the situation's own context shows is not a leak; situations cut from the
  same E1 chat do not count as each other's leak.
- `needs_reply` agreement; a reply of 1–4096 characters where one was needed.
- **Unsupported values**: a number, clock time or amount in the draft that the shown
  context (messages with their local times, the notes, the contact's name, the
  current time) does not hold, compared as values (`agent_mode.value_key`: `18:00` =
  `6:00 p.m.`, `250 000` = `250000`). A flag for review, not M06's grounding rule.
- A commitment (`makes_commitment`) where `makes_commitment_ok` is false;
  `sensitive_topic` agreement.
- Language and script against `owner_prefs.reply_language` (`match` reads the
  contact's last message, else the situation's variant; a mixed one is listed as not
  determined; so is a draft too short to name its language whose script is right; a
  wrong script is always a miss): informational only (P2-O11).

The judge pre-screen (rubric `draft_intent.v1`, one call per draft with text) names
the draft's intent, which `must_include` facts it conveys (a fact found literally
needs no judge) and violations of the kinds `commitment`, `other_conversation`,
`personal_data`, `claim_human`. A draft that declines to reply has intent
`no_reply_needed` without a call. A judge failure leaves the draft unscreened
(listed).

**Owner ratings.** The ledger (`evals/reviews/e2.json`, or `--reviews`) has
`ratings`, keyed by the situation and a digest of the exact draft rated
(`s001#1a2b3c4d`, `review.rating_key`; each JSON record carries its `rating_key`):
`as_is`, `minor`, `major` or `unusable`. Acceptability = (as_is + minor) / rated, per
variant; unrated drafts are listed and never counted. Older ledger files without
`ratings` still load.

    python -m evals review drafts --from docs/evals/<drafts run>.json                  # ratings status
    python -m evals review drafts --from docs/evals/<drafts run>.json --export sheet.md  # the sheet to rate

The sheet shows, per draft, the situation, its preferences and notes, the last six
messages it answers and the draft, with the key to record the rating under. It
refuses a report made on another version of E2.

**Cost and run procedure (WORKFLOW §11).** Per situation: the draft call, its one
truncation retry at the larger budget and the judge, each priced at its worst case.
For 150 situations on `gpt-6-luna` the worst case is about USD 0.59 (a proxy over E1
cuts with the longest contexts; the real figure prints with `--estimate-only`), under
the USD 1 cap; a typical run costs a small fraction of it. Steps: (1) `python -m evals
verify`; (2) `python -m evals run drafts --estimate-only` and state the cost to the
owner; (3) with the owner's go-ahead, a trial `--live --only s001,s031` or `--limit
N`; (4) the full `python -m evals run drafts --live`; the report is
`docs/evals/<date>-e2-drafts-<model>.md` and `.json` (the JSON keeps every draft,
check and judge verdict; synthetic data); (5) export the sheet for the owner's
ratings.

## Freezing

`datasets/MANIFEST.sha256` holds the SHA-256 of every `*.jsonl` under
`datasets/`. Verify it with either command:

```sh
just test tests/unit/test_e1_dataset.py           # includes the manifest check
docker compose -f compose.dev.yml run --rm app uv run python -m evals.manifest   # exit 1 on mismatch
cd evals/datasets && sha256sum -c MANIFEST.sha256  # any POSIX shell
```

E1 is frozen before S03.2 starts. **To change a frozen file, bump the version**
(v1 → v1.1, …) and record the change and the reason in `docs/evals/README.md`
(dataset log). Only then regenerate the manifest with
`uv run python -m evals.manifest --write`, in the same commit. Never edit a
dataset to make a model pass.

## Running

All commands run in the dev container (host: `docker compose -f compose.dev.yml
run --rm app uv run python -m evals …`, PowerShell). `just test
tests/unit/test_e1_dataset.py` validates both files through the schema and checks
the §10.1 minimums.

```sh
python -m evals verify                                  # datasets match MANIFEST.sha256
python -m evals run retrieval                           # deterministic baseline: no model, no cost
python -m evals run retrieval --mode variants --estimate-only   # worst-case cost, calls nothing
python -m evals run retrieval --mode variants --live    # REAL model; owner's go-ahead + cost approval
python -m evals run retrieval --mode replay --replay-from docs/evals/<variants run>.json  # no model, no cost
python -m evals run retrieval --mode agent --live --limit 1   # REAL model: a one-query trial of the agent loop
python -m evals run retrieval --mode agent --live --calibrate-from docs/evals/<agent trial>.json  # full agent run (E-ab)
python -m evals run summary --estimate-only             # worst-case cost of the summary run, calls nothing
python -m evals run summary --live                      # REAL model; owner's go-ahead + cost approval
python -m evals run summary --live --owner-language ru  # summaries in another owner language (default uz-Latn)
python -m evals run drafts --estimate-only              # worst-case cost of the E2 drafts run, calls nothing
python -m evals run drafts --live                       # REAL model; owner's go-ahead + cost approval
python -m evals review drafts --from docs/evals/<run>.json --export sheet.md   # owner's rating sheet
python -m evals compare docs/evals/<run>.json …         # quality and cost per 100 items per run
python -m evals cleanup [--yes]                         # list/drop throwaway databases a killed run left
```

What a run does: it refuses unless the datasets match the frozen manifest, builds a
**throwaway migrated database** (`assistant_eval_<random>` on the server of
`EVAL_DATABASE_URL`, default `TEST_DATABASE_URL`; never `DATABASE_URL`) loaded with
the E1 corpus as the product stores messages, runs the target, drops the database
and writes `docs/evals/<date>-<set>-<model>.md` plus a `.json` next to it. Useful
flags: `--limit N` (N >= 1: a **trial** run, named `…-trial`, never a gate),
`--reviews PATH`, `--out-dir`, `--no-write` (refused together with `--live`), `--date`,
`--model ID` (model-choice comparison; default is the route's configured model),
`--price MODEL=IN:OUT` (positive USD per 1M tokens, for a model the product's price
table lacks; refused for a model it already prices), `--budget-usd` (default 1),
`--owner-language uz-Latn|uz-Cyrl|ru|en` (summary, drafts and retrieval `--mode agent`,
default `uz-Latn`), `--only ID,ID` (summary and drafts: a trial of the named items), `--calibrate-from TRIAL.json` (retrieval `--mode agent` only, E-ab).
Exit codes: 0 done (a deliberate trial included), 2 refused or misconfigured, 4 the
run **stopped before its planned items** (budget cap; its report is still written). A run that fails midway writes a content-free
`…-aborted.json` with what it spent. A run drops its throwaway database when it ends, fails
or is interrupted, but **a killed process cannot**: it leaves an `assistant_eval_<8 hex>`
database on the server. `python -m evals cleanup` lists such leftovers and
`python -m evals cleanup --yes` drops them (only names of exactly that form, and never a database a running evaluation is
connected to: it is not listed, and the drop is not forced, S03.6).

**Targets.** `retrieval` has four modes over the 38 queries: `deterministic` (the
query as typed goes to `memory.retrieval.search_messages`; target in the top 5,
threshold 85 %) and `variants` (a model writes query variants with the eval-side prompt
`retrieval_variants.v2`, whose rule 1 mirrors the search bullet of the shipped
`owner_request` rule 5 (v6, unchanged in v7) — the
partner's name out of the search phrases, the words the message would contain — E-f, and
names the chat partner if the owner did; the name is resolved with the product's own
contact matcher, as the agent's `resolve_contact` would, and an unambiguous match
narrows the search to that contact; a narrowed search that finds nothing is widened by
the **product's** search service (`widen_empty_scope`, as the `search_messages` tool
does), not by the harness, E-t, E-z; the original plus variants are
searched; the JSON report keeps what was searched, the partner named, the contact filter
that produced the hits and `widened` (the filter was dropped because the filtered search
found nothing; the filter is then not shown); a second call writes the final answer from
the top 5, which
is checked mechanically: it must reference a target message, name the contact and
the day (whole-word name match), quote the message verbatim (a quote of fewer than 8
word characters must be the whole message) and carry every `answer_values` string as
whole words, except that a quote found verbatim in the message may carry it as the
message inflected it (E-u); threshold 80 %). `replay` (no model, no cost) searches again
what a recorded `variants` run searched (its variants and named partner, from the JSON
report given with `--replay-from`) with the current search code: top 5 only, a
pre-screen, written as `<date>-retrieval-replay-deterministic`. It measures a search
change without paying for a model, but the variants are those the model wrote for the
old code.

`agent` (`evals/agent_mode.py`, S03.7) measures **the pipeline exactly as the product
runs it**: per query, the question is stored as an owner control turn and the real
`OwnerRequestRuntime` answers it, with the shipped `owner_request` prompt (the prompt
registry's active version), `default_registry()` with the production release flags (the
`FEATURE_FLAGS` line of the M03 owner guide), a real `Summarizer`, the product's
`AgentConfig` budgets, a fixed clock and an eval-side control channel that keeps the
reply. The corpus account gets a control chat, `ai_control` and `ai_managed` consents
naming the processor of the run's model, an active connection and the owner language
(`--owner-language`, default `uz-Latn`). After each query the account's control turns,
agent runs, memory items, summaries, cards, confirmations and audit rows are deleted and
its settings and chat modes restored: **no query sees another's history**. Every stored
chat has a coverage start, as ingestion sets it (the dataset's pre-connection start, else
the chat's first message; `corpus.coverage_start`), so the account-wide coverage start a
search reports is the earliest chat's, not the one labelled chat's. Wrappers around the
`search_messages` and `get_conversation` handlers record what each call returned, and the
run's tool sequence is read from `agent_run_steps` before the reset (instrumentation
only). Metrics (E-aa): **top 5** = a target among the first 5 results of *any*
`search_messages` call in the run (no search at all is a miss, `no_search`); **correct
reply with reference** = the run succeeded, at least one citation the runtime verified
(`[msg:id]` turned into a source, ARCHITECTURE §7.2) points to a target, no citation was
removed as unverified, and every `answer_values` string is in the model's own words
(markers removed; the sources the application added do not count) as whole words, or
with E-s endings only where a cited target message has that very form. Amounts and clock
times compare as values (`agent_mode.value_key`): `03:15` = `3:15 a.m.`, `1 500 000` =
`1,5 mln` = `1 million 500 ming`, `3 mln` = `3 миллион`, `300 ming` = `300 000`; a number
with no unit or thousands group is left as written, and an Uzbek ending on an amount
still needs a cited target with that form. Thresholds 85 % and 80 %, gated on
owner-confirmed items like every gate. Next to them, **target reached the model** (a
target among *any* search result, past the fifth too, or among the messages
`get_conversation` showed) is informational only: it would also credit a run that found
the answer by reading a whole chat, hiding a search-ranking regression, and the answer
metric already scores the pipeline end to end. **Provider outages:** a run that fails
with `llm_unavailable` or `llm_rate_limited` is run again from a fresh state after 15 s
and 60 s (at most 3 attempts, each billed; the evaluation's SDK client retries nothing);
an item that never gets through is **not evaluated** — neither a hit nor a miss, listed
in `not_evaluated` — and the run is incomplete (exit 4, no gate, no calibration). The
JSON keeps per item the tool sequence (names and statuses), the searches (phrases,
contact filter, `widened`, top 5 and every result), the chats read and the message ids
they showed, the verified citations, the unverified count, the run status, the provider
error's SDK class (`exc_type`) and each failed attempt, and the reply (synthetic). **Cost (E-ab):** the agent
loop's worst case (its token budget, the last call at full output plus one truncation
retry, and two summary calls per query) is USD 1.34 for 38 queries on gpt-6-luna (USD
0.035 for one), over the default cap, so a full run is refused unless it is calibrated: run a `--limit 1`
trial first, then pass its JSON report with `--calibrate-from`. The run then starts when
the trial's spend per query × the queries × 1.5 fits the cap (the trial must be a
finished agent-mode run of the same model and the shipped prompt, with spend recorded),
and the hard in-flight cap still refuses every call that could pass it: a run that hits
the cap is **stopped**, incomplete (exit 4), never a gate. Without `--calibrate-from` the
usual worst-case rule applies.

`summary` (`evals/summary.py`) runs the **real S03.2 `summarize` path** over the 40 E1
conversations, always with a model (so `--live` is always needed). Each conversation
goes into the throwaway database under its **own synthetic account**, with what the
product needs to summarize it: an `ai_control` and an `ai_managed` consent naming the
processor of the run's model, an active business connection (dated at the dataset's
`coverage_started_at` where it has one) and a `coverage_gaps` row for every labelled
gap (the `delayed` one too: it must not be reported). Then `Summarizer.summarize_period`
runs over the chat's whole range, using the product's active summarize prompt
(`summarize.vN` from the prompt registry). The structured-output
schema, the output guard (items citing no shown message are dropped) and the stop-reason
handling, including the one truncation retry at a larger budget, are the product's own;
the effort is the product's `LLM_SUMMARIZE_EFFORT`. The range is fixed (E-n): from one hour
before the earliest of the first message, the coverage start and the first gap, to the
last message plus a day. The metrics: open-loop recall (the `values` match, which can only
**pass** a loop: every value found, whole words except an inflectional suffix on a value's last
word such as `900 mingga` for `900 ming`; a loop with no `values`, or whose values are not found
literally because the summary translated or reformatted them, goes to the open-loop judge,
which is shown the loop's key values, and to the owner's verdict, E-s), **caveats** (the product's own coverage renderer is
asked about the same chat and range; which gap ids and whether a coverage-start note it
carried are the structured caveats, E-d, E-p), owner language and script (read over the
model's own words only, never over application text), and the fabrication judge's flags.
Per conversation the summarize call, its retry, the fabrication judge and (only where a
loop is not matched on `values`, at worst every open loop) the open-loop judge are planned, metered and capped like every call;
the pre-run estimate prices the judges for the largest summary `sanitize` can store, so
it is a true upper bound and far above a typical run. A conversation whose summary fails
(refusal, error, truncated twice, invalid output) is a **miss on open-loop recall** (every
loop, whatever the ledger says) **and on the language check**, and is **unscreened** for
fabrication (that gate stays open); it is listed, and its caveats are still reported (they
are deterministic). The two **caveat metrics are ungated** (INFO, with a note): they come
from the harness's own gap rows and the product's renderer, not from the model, so they are
true by construction and only catch a wiring regression; the plan's caveat threshold (the
final reply keeps every caveat, D64) is **not measured** by this target (E-p). Recall and
language are measured on the per-chat summary, not on the owner-facing reply. An owner
verdict on an open loop not matched on `values` is keyed by the loop **and a digest of the summary
it judged** (`c05.q2#1a2b3c4d`; the JSON report's `open_loop_keys` lists them), so it never
carries over to another run's summary. The JSON report keeps each
conversation's summary text and flagged statements (synthetic data) for the owner's review
(E-o); the Markdown report holds numbers only. The owner language is a run parameter,
not the conversation's language. What a model may see of an E1 conversation is built in
one place for the judges (`evals/render.py`); the summarizer sees only what the product's
context builder gives it (messages, never labels, `original_text` or `purged_text`); a test
asserts that for every call.

**Attribution (S03.7).** The summarizer's context also names the conversation's contact
once (the dataset's `contact.display_name`, wrapped as untrusted, as the product does), and
the stored summary has already passed the product's attribution check (a "promise" citing
only the other side's messages is stored as that side's request; an item no cited message
supports from the side it names is dropped). The judges read the summary with who asked,
promised or agreed written **in the product's own English wording** (`agent/attribution.py`
`render_who`, with the contact's name — e.g. `- Aziza asks you to (not agreed yet): …`),
not a harness-only phrasing; that text is `labelled_text`, version `labels.v2`, and the
fabrication judge's label names it (then `fabrication.v1+render.v2+labels.v2`), so flags counted
over the older "promised by the owner" text are not compared as one series. The language
and value checks still read the model's own words only (`plain_text`, E-q). Each
conversation's JSON record carries `attribution_demoted` and `attribution_dropped` (counts).

**Judges after the summarize.v5 run (S03.7 A2).** Both rubrics now say that "you" in the
summary (or its translation: siz, сиз, вы) is the owner, the person marked `(owner)` in the
transcript (`fabrication.v2`, `open_loops.v4`): 3 of the 11 over-flags of that run were
correct "You promised …" lines. The judge transcript shows when an edited message was
edited and that its text is the current one, as the summarizer is shown it (`render.v3`),
so the fabrication judge's label is `fabrication.v2+render.v3+labels.v2`; earlier labels are
not one series with it. The open-loop judge's quote is checked in code
(`judge.evidence_in_summary`: every quoted line, split at line breaks and ellipses, must
occur in the summary, ignoring case, spacing, bullets and apostrophe forms): a "surfaced"
whose quote is not in the summary — or is empty — counts as **missed** in the pre-screen,
and the JSON record lists it under `open_loops_unquoted` with the judge's quote kept in
`open_loops_evidence` for the owner. This changes only the pre-screen; owner verdicts decide
the gate.

**Near quotes (S03.7 round 3).** In the summarize.v6 run the exact check rejected four
correct verdicts and caught no invented quote: the judge had copied the line with a slip (a
Latin letter swapped for its Cyrillic look-alike next to a Cyrillic name, a dropped "(not
agreed yet)"). A quoted piece that is not in the summary now still counts when it is at
least 0.90 similar (`difflib`, on the same folded text) to one summary line **and** carries
exactly that line's numbers (`judge.quote_match`, `NEAR_QUOTE_RATIO`): the four slips score
0.925–0.973, the v5 run's invented line 0.55, and a copy with another time or amount never
counts. Such loops count as surfaced in the pre-screen and are listed under
`open_loops_near_quotes`, so the owner can check them. The rubric text is unchanged
(`open_loops.v4`).

`python -m evals run summary --estimate-only` prints the worst-case cost, and
`--limit N` (a trial) runs the first N conversations; `--only c36,c28,c15` (a trial) runs
just the named conversations, in set order (not with `--limit`, and never the whole set).
Both leave the set sizes of the gate at the whole set, so a trial never passes.

**Gate versus pre-screen.** Every metric is reported twice: over all items (the
deterministic and judge *pre-screen*) and over the items the owner has confirmed
(the *gate*, §10.1). The gate passes or fails only when **every item of the set** is
owner-confirmed (E-k); with fewer it reads `NOT EVALUABLE`, never a pass, and the
report always shows "k of N confirmed" beside it. (Confirmed misses that already make
the threshold unreachable fail it earlier.) So does `NOT EVALUABLE` apply to any run
that stopped early or was limited with `--limit`, any metric with a judge flag still
pending the owner's verdict, and any open loop without checkable `values` that the
owner has not ruled on. The deterministic retrieval run compares against 85 % only as
a pre-screen: that threshold is defined with model-generated variants, so its gate
reads `INFO`. The owner's decisions live in a JSON ledger (default
`evals/reviews/<dataset>.json`, or `--reviews PATH`; format in `evals/review.py`):
`confirmed_items` (conversation and query ids), `findings` (verdicts on judge
flags), `coverage` (covered/missed for open loops not matched on `values`; for the summary
target keyed `loop#<digest of the summary judged>`, see above) and
`fabrications` (conversations where the owner found a fabrication the judge did not
flag; a judge negative otherwise stands, E-c). A malformed ledger is a clean error.

**Real-model runs** are manual and guarded: they refuse without `--live` (pass it
only when the owner asked for that specific run and approved its cost, AGENTS §8,
§9); `python -m evals run … --estimate-only` prices every planned call at its worst
case (one token per input character plus a fixed per-call overhead, the full output
budget); a run whose worst case exceeds the cap does not start; in flight, each call is
refused if the spend so far plus its worst case would pass the cap. The cap is **per
run** (default USD 1); the S03.4 total of USD 1 (owner approval 2026-10-21) is tracked
through the recorded reports, not enforced across runs (E-i). A model with no known
price is refused, and only a dated snapshot (`name-YYYY-MM-DD`) shares a base model's
price. The SDK client never retries (`max_retries=0`), so every attempt is one metered
call; a call that fails with a timeout, a connection error or unusable output is
billed at its worst case, since the provider may have billed it. Every call is metered
in the report (calls, tokens, USD per model and per 100 items). Calls go only through
the `LLMClient` port (official SDK, Responses API, `store=False`); no provider file,
batch, feedback or fine-tuning API is used. Evaluation data is synthetic, so no
consent row is involved. Tests use `FakeLLM` only. A run that stops on the budget cap
is marked stopped and exits with code 4.
