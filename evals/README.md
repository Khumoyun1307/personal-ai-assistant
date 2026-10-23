# Evaluation datasets

Datasets for the LLM quality gates in IMPLEMENTATION_PLAN §10.1. This folder sits
outside `src/`: nothing in the product imports it (`tests/unit/test_layering.py`).
The S03.4 runner, rubrics (`evals/rubrics/`) and metrics will be added here.

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

### How S03.4 is expected to score it (to be confirmed when the runner lands)

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

The runner arrives in S03.4. Until then, `just test tests/unit/test_e1_dataset.py`
validates both files through the schema and checks the §10.1 minimums. Real-model
runs are manual, need the owner's approval for their cost (S03.4 is capped at
USD 1), and record their results in `docs/evals/`.
