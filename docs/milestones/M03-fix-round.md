# M03 fix round (2026-10-27)

M03 is `done` (owner "M03 OK", 2026-10-27). The owner's checklist left open
items C1–C5 and O1, and the decisions page left G1 for this round
([M03.md](M03.md) "Owner checklist (2026-10-27)" and "Open items from
S03.7"; [M03-decisions.md](M03-decisions.md)). **The owner started this round
on 2026-10-27**, before any M04 code, in this order: C2, C1 (with P9), C3, O1
(design confirmed by the owner before code), G1 (a cheap trial; the owner
decides), then C4, C5 and the S03.7 prompt, harness and label items as time
allows.

Rules of the round (owner): each bug fix starts with a failing test; CI is the
test gate and only the relevant tests run locally (AGENTS §11); one branch and
one draft PR per fix group, stacked (AGENTS §15), merged by the owner; one fix
round per finding, then measure once; what is still open goes back to the
open items below instead of another round; a real safety-invariant break is
always fixed. No agent reads real chat content or `var/`; deploys and
production checks only with the owner's go-ahead.

## Status

| Item | What | Branch | State |
|---|---|---|---|
| C2 | Notes do not save or load, silently; every tool failure logs a line | `m03fix/c2-notes` | fixed; draft PR, CI and the owner merge |
| C1, P9 | "Who is waiting" misses a contact who asked last; an open request keeps "needs a reply" on | `m03fix/c1-waiting` | fixed; draft PR, CI and the owner merge |
| C3 | Ignore mode stores nothing new; the panel says what a mode change did | `m03fix/c3-ignore` | fixed; draft PR, CI and the owner merge |
| O1 | Deleted messages are remembered and marked as deleted | — | design, waiting for the owner |
| G1 | Stronger summary model trial | `m03fix/g1-sol-trial` | trial run; the owner decides |
| C4 | Ask the owner's language at onboarding | — | design, waiting for the owner |
| C5 | Tone of answers | — | not started |
| S03.7 items | Prompt, harness and label items | — | not started |

## C2 — notes fail silently

**Finding (owner checklist step 8, production, 2026-10-27).** "Remember that
Name handles the design" was answered "saving failed", and "What do you know
about Name?" with an error. Both runs ended `succeeded`; no card was shown and
the server log had no line about it.

**Cause.** Two parts: the silence is proven in code and in an offline
reproduction; the trigger is the most likely of two, not yet confirmed in
production.

- *Silence (proven).* Every failure path of `ToolExecutor._run` (tool not
  offered, arguments not JSON, invalid arguments, "not found" from a hold or a
  handler) returned an error to the model and wrote no log line; the only
  trace was `agent_run_steps.status`. A cross-field `model_validator` error
  has an empty location, so the model got `{"status":"error","reason":"invalid
  arguments","fields":["(root)"]}` and was never told which rule it broke.
  Validation runs before the taint gate, so no card could appear. A latent
  bug since S01.4/S03.3: step 8 was the first time a real model called these
  tools.
- *Trigger (one of two; both are covered by the fix).* (A) `resolve_contact`
  returns both `contact_id` and `conversation_id` for every candidate, and a
  model that copies both was refused by `RememberArgs._consistent` and
  `ListMemoriesArgs._one_filter` ("give contact_id or conversation_id, not
  both"; `list_memories` also refused `scope` "contact" with `contact_id`).
  The descriptions never said to leave one null, unlike the deletion and mode
  tools that worked in step 12. (B) A guessed id that is not the account's:
  "not found", raised inside `hold` by the card's describer before any card
  existed, or by `list_items`. The reproduction gives the exact C2 signature
  for both; well-formed arguments never failed under any production condition
  tried (locales, time zones, tainted history, the real OpenAI adapter path).
- Ruled out: strict-schema rejection, the S03.7 blocked stubs, a swallowed
  database error, time zone or locale lookups, `hold`'s early returns.

**What changed** (no prompt file, no migration, no new content column).

- *One log line per failed call.* `ToolExecutor.execute` logs every outcome
  whose status is not `ok`: warning `agent_tool_call_failed`, or info
  `agent_tool_call_not_run` for an expected state (held for a card, already
  decided on a card, a card that could not be shown, `chat_access_off`).
  Fields: `account_id`, `run_id`, `tool`, `status`, `tainted` and a fixed
  `code` — `not_offered`, `args_not_json`, `args_invalid` (with `fields`:
  real field names, `(root)` or `(extra)`, and pydantic `error_types`),
  `not_found` / `internal_lookup` (with `exc_type`), `hold:<reason>` or
  `handler:<status>`. Never arguments, reasons, pydantic or exception
  messages. The code travels on `ToolOutcome.log_code` / `log_detail`, never
  sent to the model or persisted. A tool name that is not registered is model
  output: logged and stored in `agent_run_steps.tool_name` as `(unknown)`
  (the runtime passes the registry's names to the executor).
- *`hold` codes.* Every return of `confirmations.hold` carries a code
  (`card_sent`, `args_too_large`, `description_too_large`, `no_channel`,
  `owner_unreachable`, `existing:<status>`, `same_change_pending`,
  `insert_race`, `card_not_delivered`). No behaviour change.
- *Errors the model can act on.* An invalid call keeps `reason` and `fields`
  and gains `problems: [{"field", "rule"}]` with only the fixed text of our
  own rules: validators raise `ArgumentRuleError(ValueError)`
  (`agent/tools/common.py`), recognised through the pydantic error's context.
  Any other message (pydantic's, or `date.fromisoformat`'s, which quotes the
  input) is never echoed. The cross-field rules of every tool and the shared
  field rules (ids, dates, cursors, limits, lengths) now raise it; the "exactly
  one" rule of `get_conversation`, `set_conversation_mode` and
  `delete_conversation_data` says "set the other to null".
- *Typed "not this account's".* `core/errors.py::NotInAccountError`
  (a `LookupError` subclass) replaces every `raise LookupError` in `src/`
  (tool handlers and describers, `memory/items.py`, `conversations/read.py`
  and `modes.py`, `privacy/deletion.py`, `agent/persistence.py`). The
  executor answers it "not found" as before (invariant 12); any other
  `LookupError` (`KeyError`, `IndexError`, a missing time zone) is a bug,
  answered `{"status":"error","reason":"internal error"}` and logged with its
  type. The confirmation applier answers only `NotInAccountError` "not found"
  (anything else is a failed, possibly uncertain apply), and
  `privacy/requests.py` `_attempt` catches only `NotInAccountError` (M03
  follow-up 5, fixed here). The other `except LookupError` sites
  (`memory/undo.py`, `telegram/control/panels.py`) are unchanged and still
  catch it.
- *Memory tool arguments.* `remember` with both ids is a note about the
  contact: the validator sets `conversation_id` to null (pure, before the
  confirmation gate, so the card shows exactly the arguments that are applied
  and the applier's re-validation gives the same note; `items.save` never gets
  both). `list_memories` with both ids uses the contact filter, which already
  includes that contact's chat's notes (both filters at once would AND and
  hide the contact's own notes). A `scope` matching a given id (with both
  ids, either `contact` or `conversation`) is dropped; any
  other scope with an id, and `applies_to_replies` on a kind other than
  instruction, are refused with a fixed rule. The `conversation_id`
  descriptions say "null when contact_id is given"; `scope` explains owner,
  contact and conversation.
- *Docs.* ARCHITECTURE §8.2 (memory argument semantics), §8.3 (the error
  contract, `problems`, `NotInAccountError`, the log line);
  [M03-owner-guide.md](M03-owner-guide.md) steps 8–9 (in production a note
  about a person shows a confirmation card first — `resolve_contact` returns
  the contact's chosen name, which taints the run, invariant 3b — and Confirm
  brings the note with Undo, then "✅ Done"; "Forget that" is a card too);
  the P2 specs that told engineers to raise `LookupError` for a foreign id
  (S04.1, S04.2, S04.5, S05.2, S05.2P, S05.4) now name `NotInAccountError`,
  F0 item 15 (`SubjectNotFound`) is marked done by C2, and
  [M03-handoff.md](M03-handoff.md) §4 marks follow-up 5 closed.

**Tests** (FakeLLM, the real `OwnerRequestRuntime`, the production registry,
`PRODUCTION_FLAGS` — the verbatim production flags line, now in
`tests/integration/agent_env.py` —, both consents, a history window with
other people's text).

| Test | Shows |
|---|---|
| `test_memory_production_flow.py::test_remember_with_both_ids_from_resolve_contact_is_held_for_a_card` | `resolve_contact`, then `remember` with both ids copied from the candidate, tainted: a card whose stored arguments carry the contact only; Confirm saves exactly one contact-scope note, echoed with Undo; one info line `hold:card_sent` |
| `…::test_remember_with_both_ids_in_a_clean_run_saves_a_contact_note` | the same arguments in an untainted run: `ok`, scope contact |
| `…::test_what_do_you_know_about_a_contact_lists_their_notes[contact_id+scope\|both_ids\|both_ids+scope\|both_ids+conversation_scope]` | `list_memories` after `resolve_contact` lists the contact's note and their chat's note (count 2), not another contact's or the owner's |
| `…::test_an_id_that_is_not_the_accounts_is_not_found_and_logged_once` | another account's contact and an unknown id: still "not found", no card, nothing saved; exactly one warning per call, code `not_found`, `exc_type` `NotInAccountError` |
| `test_log_hygiene.py::test_every_tool_call_that_does_not_succeed_logs_one_content_free_line` (RG-10 family) | through the real log pipeline: a canary tool name, bad JSON, too-long content, a date that `date.fromisoformat` would quote, an unknown id (another account's is covered in `test_memory_production_flow`, through `capture_logs`), a `KeyError` in a test-only handler, and a held call too large for a card each give exactly one line with the expected code (six warnings, one info); the canary appears nowhere |
| `test_tool_executor.py` (11 new or extended) | the codes and levels, `(unknown)` for an unregistered name, `problems` only for our rules, `internal error` for a `KeyError`, no line for `ok`, and `test_no_code_raises_a_bare_lookup_error` (an AST scan of `src/`) |
| `test_memory_args.py` | both ids accepted and normalised (and stable on re-validation); the `list_memories` filter cases; real contradictions refused with an `ArgumentRuleError`; the descriptions state the rule. The two "both ids" refusals moved to accepted; three scope-with-the-wrong-id refusals added |
| `test_tool_registry.py::test_the_production_flags_line_is_every_registered_tool_plus_the_refresh` | `PRODUCTION_FLAGS` equals every registered tool's flag plus `summary.refresh`, and the agent-mode eval's copy (a guard; it passed before the fix too) |
| `test_memory_tools.py::test_another_accounts_ids_are_not_found_by_memory_tools` | strengthened: the handlers raise `NotInAccountError` |
| `test_tool_confirmations.py::test_a_lookup_bug_after_confirm_is_a_failure_not_not_found` | a `KeyError` in a confirmed handler: the apply fails, the owner hears it "may or may not have been applied" (not "not found. Nothing was changed"), one `tool_confirmation_handler_failed` line with `exc_type` `KeyError` |
| `…::test_an_id_not_in_the_account_after_confirm_is_reported_not_found` | the other side: `NotInAccountError` after Confirm still reads "not found", with no handler-failure line |
| `test_deletion_outcomes.py::test_a_lookup_bug_inside_the_deletion_is_a_failure_not_not_found` | M03 follow-up 5: a `KeyError` in the deletion's last step rolls the deletion back and takes the failure path ("nothing was deleted", a `failed` marker, the job raises), never `NOT_FOUND_TEXT` over a committed partial deletion |

**Red first.** The tests were written and run before the fix (only the two
empty exception classes and an unused `registered` field existed, so they
could be collected): 27 of 83 failed, each for the reason it names —
`remember` and `list_memories` with both ids answered `invalid arguments` /
`["(root)"]` with no card; no `agent_tool_call_*` line for any failure (0 of
the 7 expected); an unregistered name stored as given; no `problems` key; a
`KeyError` answered "not found"; 28 bare `raise LookupError` sites; the
memory refusals plain `ValueError`s; the descriptions without the rule. After
the fix, locally in the dev container: the C2 set (`test_tool_executor`,
`test_memory_args`, `test_tool_registry`, `test_tool_args`, `test_redaction`,
`test_memory_production_flow`, `test_memory_tools`, `test_memory_safety`,
`test_tool_confirmations`, `test_log_hygiene`) 188 passed; the tests of the
modules whose raise sites changed (`test_conversation_tools`,
`test_deletion_tools`, `test_deletion_outcomes`, `test_deletion_races`,
`test_deletion_cards`, `test_conversation_read`, `test_conversation_modes`,
`test_data_deletion`, `test_agent_runtime`, `test_memory_isolation`,
`test_summaries`) 175 passed in 18 min 43 s, longer than the ~15-minute local
guideline (AGENTS §11). Ruff check and format on the changed files, `mypy src
evals` (148 files) and `lint-imports` (4 contracts kept) are clean. The full
suite was not run locally; CI runs it.

**What the owner should re-run** (Alviro, after the deploy): guide steps 8
and 9. Step 8 now expects a card first. If a step still fails, the server log
has one `agent_tool_call_failed` line per failed call with its code, which
tells the two triggers apart (`args_invalid` with `fields: ["(root)"]` or a
field name, or `not_found`).

**Review.** Two independent reviews of the diff found no must-fix and
confirmed invariants 3b, 9, 11 and 12. What they found and what changed:
(1) the two narrowed `except` clauses (the confirmation applier;
`privacy/requests.py` `_attempt`, follow-up 5) changed behaviour with no
test: three integration tests added (the `test_tool_confirmations` pair and
`test_deletion_outcomes`, rows above), run red against the old
`except LookupError` and green after restoring the narrowed clauses;
(2) `list_memories` with both ids and `scope` "conversation" was refused
with a rule text that said that scope was allowed: with both ids either
scope now matches and gives the contact filter (unit and production-flow
cases added, red first);
(3) the P2 specs still told engineers to raise `LookupError`: corrected in
this change (AGENTS §2), with F0 item 15 and the handoff row for follow-up
5;
(4) nits: the owner guide now claims only that *saving* works with either
id, and this record says "an unknown id" for the log-hygiene test. After
the review, locally in the dev container: the C2 set plus
`test_deletion_outcomes` (11 files) 199 passed; ruff check and format on the
27 changed files, `mypy src evals` (148 files) and `lint-imports` (4 kept)
clean.

**Open items.**

- The production trigger (A or B) is not confirmed; the re-run of step 8 with
  the new log line shows it. The content-free `agent_run_steps` read the
  diagnosis proposed needs the owner's go-ahead and was not done.
- `get_conversation`, `set_conversation_mode`, `delete_conversation_data`
  ("exactly one") and `summarize_period` ("at most one") still refuse both
  ids, by design; the model now gets the rule ("set the other to null") in
  `problems`. `summarize_period`'s id descriptions do not say it yet.
- The memory tool descriptions changed, so every `owner_request` request's
  tool schema and cached prefix changed. No prompt file changed (no
  `owner_request.v8`). The S03.7 agent-mode retrieval numbers were measured
  with the old descriptions; a re-run is optional and its cost needs the
  owner's go-ahead.
- The P2 specs are corrected in this change (above), but a P2 engineer who
  already wrote `raise LookupError` on an open branch gets a red
  `test_no_code_raises_a_bare_lookup_error` after rebasing: the TL should
  say so in the team channel. M03.md's follow-up 5 is still to be marked
  closed by the lead.
- `list_memories` with only a `conversation_id` lists that chat's notes, not
  the notes about its contact (unchanged; the owner guide claims only that
  saving works with either id).
- A pair whose `conversation_id` is not that contact's chat (or not the
  account's) is not refused: the validator drops it and the note goes to the
  contact, which is checked; the card and the echo name the contact. No
  foreign row is read or written.
- `items.save` still raises `ValueError` for both ids; the tools no longer
  reach it.
- Side observations from the reproduction, not checked: `citations.py`
  `_source` calls `ZoneInfo` directly instead of `zone()`; the confirmation
  card's arguments may be HTML-escaped twice (`&quot;`).

## C1 and P9 — waiting contacts

**Cause.** The attention list had one signal for "waiting": the `needs_reply`
of the chat's rolling summary. `get_attention_list` copied it unchanged
(`summary_view`: `bool(summary["needs_reply"])`), sorted a `false` chat last and
told the owner model that `needs_reply` was "a model judgement". In step 7 the
summary was refreshed inline (incrementally: the previous summary of the
step-4 messages, which all counted as the owner's, plus the new question) and
said no reply was needed, so the chat came back last as "no reply needed" and
the owner model left it out. The tool never compared the verdict with the
summary's own open questions or requests either, which `catch_up.importance`
already did for catch-ups (the same inconsistency as E1 c30, P9). A second,
hidden defect: `catch_up.unanswered` sorted the oldest chats first and applied
`limit` (default 10) in SQL, and the 8 inline checks went to the oldest chats
too, so on a real account the newest waiting chat could be cut or left
`not_checked`. Classification was not the cause: an away or greeting message
(`owner_automated`) never moves `last_owner_msg_at`, and `assistant_sent`
counts as an answer (D96).

**What changed** (code, tests and docs; no prompt, output schema, stored jsonb
or migration change):

- `memory/catch_up.py` — the application's own rule, no model call:
  `waiting_questions(session, account_id, conversation_ids)` returns, per chat,
  the newest retained contact message sent after
  `greatest(last_owner_msg_at, last_assistant_msg_at)` whose text or caption
  holds a question mark outside links (`has_question`: `?` plus the full-width,
  small, Arabic, emoji and combined forms; a link's query string is not a
  question, but a mark written straight after a link, as in "…t.me/x?",
  ends the sentence and counts). It reads the newest 20 contact messages after the answer, so a
  later "rahmat" or sticker does not hide an earlier question; deleted and
  expired rows are never evidence (D63); only Observe/Assist chats that are
  not bot chats have questions (`ATTENTION_MODES`, shared with
  `unanswered`), so a chat set to Ignore or found to be a bot chat during a
  summary call shows none (C3); `Message`, `Conversation` and `Contact` are
  all scoped to the account. `open_to_owner` and `needs_reply`
  (P9): the model's verdict, kept on while a question, decision or request to
  the owner (`{by: owner, status: requested}`) is open; a v1-v4 commitment
  without `status` is a promise. `importance(..., asked_question=True)` puts a
  chat whose contact asked after the owner's last answer under
  `needs_attention`. `unanswered` now keeps the newest `limit` chats (still
  returned waiting longest first), so a cap never drops the newest chat.
- `agent/tools/common.py` `summary_view` — P9 at read time: `needs_reply` is
  `catch_up.needs_reply` of the attribution-checked summary; when the model
  said "no" but something is open, the reason becomes the fixed
  `STILL_OPEN` text. This applies to `get_conversation`, catch-ups and the
  attention list alike; the stored summary keeps the model's own verdict.
- `agent/tools/summaries.py` `get_attention_list` — up to 100 candidates
  (`MAX_ATTENTION_CANDIDATES`) are ranked before the limit: may be waiting or
  needing a reply first, then not checked, then needing nothing; within a rank
  the chats waiting longest stay first. A fresh stored summary ranks on the
  verdict the owner will see: P9 on the attribution-checked summary (one
  batched author lookup, `memory/summaries.py::message_directions_by_conversation`
  via `common.summaries_authors`); a stale or missing one ranks as not
  checked. The limit applies after ranking; what it cuts is counted
  (`not_listed`, `not_listed_may_be_waiting`, `not_listed_not_checked`,
  `note_not_listed`): a cut chat nobody checked is unknown, never "not
  waiting". The inline refresh is unchanged and goes to the listed
  stale chats in ranked order (still at most 8 calls, D62). The questions are
  read again after the refresh, so a question deleted meanwhile is never shown.
  Each entry gains `may_be_waiting` and, when set, `waiting_reason` (fixed
  `MAY_BE_WAITING` text) and `question` (`message_id`, local `sent_at`, the
  text wrapped as untrusted contact text, at most 400 characters); the
  question is citable in the reply. The result gains `waiting_count`; the rule
  and the tool description say that `may_be_waiting` is the application's own
  check and that every such chat must be listed as possibly waiting, even when
  `needs_reply` is false. One content-free log line, `attention_list_built`,
  holds counts only (candidates, listed, waiting, may_be_waiting, not_checked,
  inline_checks).
- `summarize_period` — each chat gains `may_be_waiting` and, when set,
  `waiting_reason` and the `question` (wrapped, citable), exactly as the
  attention list shows them (one helper, `_waiting`), read after the summary
  calls; `importance` uses it.
- `agent/owner_texts.py` — `MAY_BE_WAITING` and `STILL_OPEN` in en, uz-Latn,
  uz-Cyrl and ru.
- ARCHITECTURE §7.2 (the attention list and catch-ups return a citable
  question too), §8.2 (`summarize_period` and `get_attention_list` rows) and
  §11.1 (the S03.7 round-3 sentence: read-time `needs_reply`, catch-up
  `may_be_waiting` with the question).

**Tests** (synthetic text, FakeLLM):

- `tests/integration/test_summaries.py::test_c1_a_contact_who_asked_after_the_owners_last_answer_is_listed_first`
  — the owner's case: an older chat (Bobur) whose question the owner answered,
  then "rahmat"; a newer chat (Kamola) with the owner's own messages, then the
  contact's question, "rahmat" and an away message (`is_from_offline`); a
  FakeLLM summary that misses the question (`needs_reply=false`, no items).
  With `limit=1` the one entry is Kamola, checked by the model as `false`,
  `may_be_waiting` true with the fixed reason and the wrapped, citable
  question; `waiting_count` 1, `not_listed` 1, `not_listed_may_be_waiting` 0;
  one summary call; the `attention_list_built` line holds exactly the six
  counts. Without a limit: Kamola, then Bobur (not waiting: answered by the
  owner; the away message answers nothing).
- `test_c1_the_waiting_rule_reads_captions_and_never_a_deleted_question` — a
  photo caption with "?" counts; a deleted question and an expired (purged)
  question are not evidence and never appear in the result; a link's "?" is
  not a question.
- `test_c1_a_catch_up_puts_a_chat_whose_contact_asked_last_under_needs_attention`
  — a summary with no items and `needs_reply=false`: the chat whose contact
  asked is `needs_attention` and `may_be_waiting`, with the question wrapped
  and citable (the only citable entry); the other chat has no question.
- `test_c1_the_limit_counts_cut_chats_that_may_be_waiting_or_were_not_checked`
  — three chats (two ask "?", one says "rahmat"), no summarizer, `limit=1`:
  the older asking chat is listed; `not_listed` 2, `not_listed_may_be_waiting`
  1, `not_listed_not_checked` 1, and the note says both.
- `test_c1_the_limit_ranks_a_stored_summary_on_its_checked_verdict`
  (`request`, `legacy_v4_promise`) — a fresh stored summary with an open
  request to the owner and `needs_reply=false` (stored as `{owner,
  requested}`, or rewritten to a v1-v4 item without `status` citing only the
  contact) plus a newer chat nobody checked, `limit=1`: the request chat is
  listed with `needs_reply` true and `STILL_OPEN`; the other is counted as
  not checked.
- `test_c1_unanswered_keeps_the_newest_chats_under_its_cap` — three
  unanswered chats, `unanswered(limit=2)`: the two newest, oldest first.
- `tests/integration/test_summaries_verification.py::test_an_attention_check_of_a_chat_left_out_shows_no_question`
  (`ignore`, `bot`) — a waiting chat set to Ignore (or marked a bot chat)
  during its inline check keeps its notice, but shows no `may_be_waiting`, no
  question and no citable entry; the existing
  `test_the_notice_names_why_a_chat_was_left_out` covers the catch-up.
- `test_p9_an_open_request_keeps_needs_reply_on_after_a_thank_you` — c30-like:
  the SIM-card request (`{owner, requested}`), the owner's answer, "rahmat",
  the model's `needs_reply=false`: the attention entry and `get_conversation`
  show `needs_reply` true with the `STILL_OPEN` reason; `may_be_waiting` is
  false (the question came before the owner's answer); the stored rolling
  summary keeps `false`.
- `test_another_accounts_conversation_is_not_found` (strengthened, invariant
  12): the other account's contact asks a question; `waiting_questions` under
  our account returns nothing for their chat (and finds it under theirs), and
  our attention list never shows it.
- `tests/unit/test_summary_units.py::test_p9_an_open_request_keeps_needs_reply_on`
  (open question, decision, request and a legacy owner "promise" citing only
  the contact turn it on; the owner's own promise, a legacy promise citing the
  owner, the owner's request to the contact and an owner "request" citing only
  the owner — dropped first, D96 — do not; the model's own "yes" keeps its
  reason), `test_the_attention_rule_finds_a_question_mark_in_any_script_outside_links`
  (19 cases: Uzbek Latin and Cyrillic, Russian, English, full-width, emoji and
  Arabic marks, links with query strings, `t.me`, a mark straight after a
  link or inside brackets after it, a link ending in a full stop, empty and
  None),
  `test_importance_puts_a_chat_whose_contact_asked_after_the_owners_answer_first`;
  `tests/unit/test_citations.py::test_every_fixed_text_exists_in_every_supported_locale`
  now covers the two new tables.
- Existing tests unchanged: the attention-list and catch-up tests in
  `test_summaries.py`, `test_summaries_review.py` (one call per stale chat; a
  chat changed during both calls shows no deleted text, now also no deleted
  question) and `test_summaries_verification.py` (a chat set to Ignore during
  a catch-up shows no text, now also no question), and `test_conversation_tools.py`.
- **Result** (dev container, after the review fixes, the six relevant files
  `tests/unit/test_summary_units.py`, `tests/unit/test_citations.py`,
  `tests/integration/test_summaries.py`, `test_summaries_review.py`,
  `test_summaries_verification.py`, `test_conversation_tools.py`, `-rA`):
  pytest exit 0, 227 passed, 0 failed, 0 errors. `ruff check` and
  `ruff format --check` on the 9 changed Python files clean; `mypy src evals`
  "no issues found in 148 source files"; `lint-imports` 4 kept, 0 broken.
  The full suite and `just check` were not run (CI runs them, AGENTS §11).

**Red first.** The new tests were written and run before the fix (dev
container, only these files): 26 failed. The owner's case failed on the first
assertion (the one entry under `limit=1` was the older chat Bobur, not
Kamola); the edge and catch-up tests failed with `KeyError: 'may_be_waiting'`;
the P9 integration test failed with `('model', False) == ('model', True)`; the
cross-account test with `AttributeError: ... no attribute 'waiting_questions'`;
the P9 unit test with `assert False is True`; the 15 rule cases with
`AttributeError: ... no attribute 'has_question'`; the importance test with
`TypeError: importance() got an unexpected keyword argument 'asked_question'`;
the every-locale test (4 locales) with
`AttributeError: ... no attribute 'MAY_BE_WAITING'`.

**Review.** Two independent reviews of the uncommitted change found no
safety-invariant problem (3b wrapping and taint, 9 consent, 12 scoping, D63,
D62's bound, content-free log all confirmed) and these gaps, all fixed:
(1) the pre-limit ranking read the stored summary without the attribution
check, so a legacy v1-v4 row whose contact request was stored as the owner's
"promise" ranked as needing nothing and could be cut uncounted — the code
comment and this record wrongly said it could only rank a chat early; the
ranking now uses the checked verdict (one batched author lookup); (2) the
note about cut chats counted a cut chat nobody checked as "not waiting" —
`not_listed_not_checked` and the note now say how many were not checked;
(3) no test pinned the cut-waiting count, the ranking on the stored verdict
or `unanswered`'s newest-under-cap order — three tests added; (4) catch-ups
set `may_be_waiting` without the question that D115 names — they now carry
the same wrapped, citable `question` as the attention list; nits: a "?"
written straight after a link was swallowed by the link pattern (fixed, four
unit cases), ARCHITECTURE §7.2 did not name the new citable sources (fixed),
and this record claimed a green run that had not finished (replaced by the
result below). Red first for the bugs (dev container, the new and changed
tests only): 7 failed — the legacy-v4 ranking case listed the newer unchecked
chat (`assert 22 == 21`), the `request` case and the cut-count test with
`KeyError: 'not_listed_not_checked'`, the catch-up test with
`KeyError: 'question'`, and three link cases with `has_question(...)` False;
the `unanswered` order test passed (it pins behaviour this change already
had). The first run of the six files after these fixes had one failure, an
existing test doing its job:
`test_summaries_verification.py::test_the_notice_names_why_a_chat_was_left_out[ignore]`
found the catch-up's new `question` showing the text of a chat set to Ignore
during its summary call. The same gap had existed in the attention list's
re-read since the first build, untested and missed by both reviews: a new test,
`test_an_attention_check_of_a_chat_left_out_shows_no_question[ignore]`,
failed red with `may_be_waiting` `True is False` (the `bot` case passed: a
bot chat's texts are purged). `waiting_questions` now takes only
Observe/Assist chats that are not bot chats.

**Decisions (suggested numbers):**

| # | Decision | Why |
|---|---|---|
| D115 | **A deterministic "may be waiting" rule, and the attention list's limit after ranking.** A retained contact message after the owner's or the assistant's last message whose text or caption has a question mark outside links sets `may_be_waiting` (attention list and catch-ups), with fixed wording and the question itself (wrapped, citable); an away/greeting message answers nothing; deleted or expired messages are never evidence. Up to 100 candidates (the newest) are ranked, waiting first, before the limit, a fresh stored summary on its checked verdict (P9); the cut is counted (may be waiting / not checked). The inline refresh stays at most 8 calls (D62). Pending owner confirmation. | C1: a model's `needs_reply=false` hid a contact who asked last; PRD FR-SUM-03 asks for "R1 heuristic + model". |
| D116 | **P9: "needs a reply" is computed when read** from the attribution-checked summary: the model's verdict, kept on while a question, decision or request to the owner is open, with the fixed `STILL_OPEN` reason. Stored summaries keep the model's own verdict; no migration. Owner confirmed P9 2026-10-27; the read-time form is the lead's. | E1 c30; step 7. |

**Texts to proofread** (`agent/owner_texts.py`; English final, the others
drafts, D98 practice; no name; the ru wording uses no gendered verb):

- `MAY_BE_WAITING` — en "May be waiting for your reply: their latest messages
  include a question that nobody has answered." / uz-Latn "Javobingizni
  kutayotgan boʻlishi mumkin: soʻnggi xabarlarida savol bor, unga hali hech kim
  javob bermagan." / uz-Cyrl "Жавобингизни кутаётган бўлиши мумкин: сўнгги
  хабарларида савол бор, унга ҳали ҳеч ким жавоб бермаган." / ru "Возможно,
  ждёт вашего ответа: в последних сообщениях собеседника есть вопрос, на
  который ещё нет ответа."
- `STILL_OPEN` — en "A reply is still needed: a question, decision or request
  to you in this chat is still open." / uz-Latn "Javob hali kerak: bu chatda
  sizga qaratilgan savol, qaror yoki iltimos hali yopilmagan." / uz-Cyrl "Жавоб
  ҳали керак: бу чатда сизга қаратилган савол, қарор ёки илтимос ҳали
  ёпилмаган." / ru "Ответ всё ещё нужен: в этом чате остаётся открытым вопрос,
  решение или просьба к вам."

**Open items:**

- **Eval labels (harness group):** `evals/summary.py` `labelled_text` still
  renders the stored `needs_reply`, so the judge reads "Needs a reply: no" for
  a c30-like summary while the owner now sees "yes". Rendering it with
  `catch_up.needs_reply` and the `STILL_OPEN` reason changes what the judges
  read, so it needs `LABELS_VERSION` "labels.v3" (with
  `tests/unit/test_eval_summary.py`, `tests/integration/test_eval_summary.py`
  and `evals/README.md`); labels.v3 runs must not be compared with labels.v2
  baselines without re-judging. Not done here (lead decision).
- Questions without a mark (Uzbek "-mi", Russian "ли"), voice questions and
  reactions as answers are not detected by the rule; the summary covers the
  first, M05 triage the rest. A forwarded text containing "?" can give a false
  "may be waiting", which the hedged wording allows.
- `owner_request.v7` rule 6 names only `search_messages` and `get_conversation`
  ids for citations; a `[msg:id]` of the attention list's question still
  verifies (the executor merges every tool's citable). Saying so in the prompt
  is a v8 change, not needed now.
- Model-facing strings changed (the attention tool's description and rule, the
  new fields): the owner_request agent-mode eval may shift slightly.
- Side note, not changed: `conversations/read.py` `list_conversations(
  awaiting_owner_reply=True)` ignores `last_assistant_msg_at` (inconsistent
  with D96 and `unanswered`); it matters once assistant sends exist (M04).
- Noticed, not changed: the existing ru `CITATION_REMOVED` text ends "я её
  убрал" (a gendered verb), against D98's ru practice; for the owner's
  proofreading.
- Manual check: re-run owner guide step 7 on Alviro after the merge and
  deploy (owner-only: needs the second account to send a question).

## C3 — Ignore mode

Owner checklist step 11 left open whether a message sent while a chat was in Ignore was stored.
The behaviour is now verified by guard tests: nothing of such a message's content is stored. The
step could not be read because the panel's "Messages: N" counted the metadata row that Ignore
writes, so the number went up. The panel and the mode echo now say what a mode change did. Two
bugs are fixed: the count, and a retention bug that kept an earlier name. After the owner's answers
(2026-10-27), a failed update in an Ignore chat no longer keeps its raw copy either.

### Verification: what an Ignore chat stores

**Not stored** (each pinned by a guard test that passed before any change):

- Text, caption, media details, `file_id`/`file_unique_id`, search key (`search_key`,
  `search_tsv`), script, language guess, content hash, a new display name or username. This holds
  for a new message, a photo with a caption, an edit of it, an edit of a message saved before the
  switch, and an edit of a message never seen. No synthetic marker from these updates appears in
  **any** column of any table in the `app` and `procrastinate` schemas, content-bearing or not.
  Every one of these raw updates is cleared (`payload IS NULL`, status `processed`). No
  `memory.refresh_summary` job is queued, `pending_ai_since` stays NULL and no coverage gap is
  written, with the summary trigger wired as in production. A worker first takes the refresh
  that message 1 queued before the switch, so that job's queueing lock cannot swallow a later
  one (review) —
  `test_conversation_modes.py::test_an_ignored_chats_messages_leave_no_text_name_or_file_id_in_any_column`.
- A new chat that starts in Ignore (default mode) stores neither the name nor the username —
  `…::test_new_chats_start_in_the_default_mode_set_in_settings` (extended with a username).
- A refresh job that runs after the switch sends nothing to the model (status `SKIPPED`, no
  request, no summary row) — `test_summaries.py::test_a_refresh_queued_before_the_switch_to_ignore_makes_no_call`.
- A deletion in Ignore still purges the content saved before the switch and drops the rolling
  summary that covered it (D63). It sets no refresh flag, queues no rebuild job (705's earlier
  refresh is taken first, as above) and clears its own payload —
  `test_business_ingestion.py::test_an_ignore_mode_deletion_purges_what_was_stored_and_queues_nothing`.
- A failed (dead-lettered) update keeps no raw copy, since owner decision 2 (bug fix 5 below).

**Kept** (metadata, unchanged by this fix): one `messages` row per message (direction,
`sent_at`/`edited_at`, reply-to id, `content_type`, source update id); the conversation's
`last_*_msg_at`, `coverage_started_at`, availability and the takeover pause; for a new chat, a
contact row (Telegram id, `is_bot`). What was saved before the switch stays (D73, D74). Owner runs
can still find and read it: `…::test_what_was_saved_before_ignore_is_still_found_by_search`
(guard) and `…::test_existing_data_is_kept_after_switching_to_ignore`.

A row with a direction, no `content_hash`, and neither `deleted_at` nor `purged_at` is exactly a
message that arrived while its chat was ignored: Observe always writes the hash, and deletion and
expiry set their own marks. `conversations/read.py::not_stored_while_ignored` is that predicate,
and `saved_message` is its counterpart. No migration and no new content column were needed.

### Bugs fixed

1. **Panel count (step 11).** `visibility.py` counted every row with a direction, including Ignore's
   metadata rows (and expired rows). The panel now shows `Messages saved: N (…by direction)`,
   counting rows with content only. A separate `Not saved (arrived while ignored): N` line appears
   when there are any.
2. **B2, retention.** Every Ignore message moved `contacts.updated_at` (`_upsert_contact`'s
   `ON CONFLICT`). The name sweep reads that column (`retention.py`, "names while content is
   retained"), so a contact who kept writing in an Ignore chat kept a name saved before the switch
   long after its messages expired. `updated_at` now moves only with the names (`_refresh_names` in
   Observe). `record_ingest_failure` no longer moves it either, which is harmless: its only reader
   is the sweep. ARCHITECTURE §10.4 contacts row updated.
3. **"Nothing to change" (D68, invariant 11).** A stale Ignore confirmation (the chat was set to
   Ignore another way in the meantime) answered "Ignored: new messages are not stored". Tapping the
   mode already set answered with the mode's name. Both now say "Nothing changed" (toast) and
   "Nothing to change: the chat with {name} is already in {mode}." (panel).
4. **Coverage honesty (part of diagnosis item 7).** Set Ignore clears the refresh flag, so
   `get_conversation` called the rolling summary `up_to_date` while unsaved messages kept arriving.
   It now reports `coverage.not_stored_while_ignored`, and `up_to_date` is false while such rows
   are newer than the summary's `covers_through_message_id`. A catch-up gets a new caveat: "N
   message(s) from this period arrived while their chat was in Ignore mode and were not stored."
   It counts rows in the period whatever the chat's mode is now, so it still appears once the chat
   is back in Observe. The two existing Ignore caveats said "its messages are not stored or
   summarized", which is wrong because earlier messages stay. They now say "its new messages are
   not stored and it is not summarized" (scoped) and "their new messages are not stored and they
   are not summarized" (account-wide).
5. **Failed updates in Ignore (owner decision 2).** A dead-lettered update in an Ignore chat kept
   its raw payload (text, the contact's name) for the raw-update retention, like every failed row.
   `inbox requeue` resets every failed row that still holds its payload, and `record_message`
   reads the mode when the row is processed, so a requeue after the chat was back in Observe
   stored the message in full. Now `record_ingest_failure` returns the mode it reads under the
   conversation lock, and when it is Ignore the dead-letter hook (`business.py::on_dead_letter`)
   clears the row's payload and resolves its gap at once (`resolve_ingest_failure`: the ingest
   pause lifts once no other dead letter of the chat is open). The content-free `ingest_error`
   audit row stays, and one log line (`business_dead_letter_cleared`, ids and an enum) says why the
   payload is gone. Such a row is never reset by `inbox requeue` (counted as skipped), and the
   watchdog does not count it: `_recoverable_dead_letters` counts only failed rows that still
   hold a payload, so no dead-letter alert is sent. `/status` shows no coverage caveat for it;
   its inbox line ("N update(s) could not be processed.") still counts the row, because it
   counts every failed row, as for one an owner deletion cleared (D91). A chat first seen
   through the failed update is created in the owner's default mode (`new_chat_mode`, as in
   `record_message`), so with Ignore as the default it is handled the same way, and its contact
   row gets no name. An Observe chat's dead letter is unchanged: payload kept, gap open, pause
   set, alert sent. No migration, no new column. ARCHITECTURE §6.1, §6.2 (requeue skips) and
   §6.5 say so.
   - Tests in `test_business_ingestion.py`, next to the other dead-letter tests:
     `test_a_dead_letter_in_an_ignore_chat_keeps_no_payload_and_closes_its_gap` (payload NULL,
     gap resolved, no pause, one audit row; `requeue_failed` → `Requeued(reset=0, cleared=1)`;
     watchdog count 0 and nothing sent; no caveat on `/status`; nothing stored after the return
     to Observe), `test_an_ignore_dead_letter_leaves_an_earlier_open_one_and_its_pause` (a dead
     letter from Observe time keeps its payload, gap and pause; requeue resets only it),
     `test_a_dead_letter_for_a_new_chat_that_starts_in_ignore_keeps_nothing` (default mode
     Ignore). Guard, green before the change:
     `test_a_dead_letter_in_an_observe_chat_keeps_its_payload_for_a_requeue`.
   - Red first: 3 failed, 1 passed (the guard). The first two failed at
     `assert await payload_is_sql_null(...)` (`assert False`: the payload was kept). The
     new-chat test failed at `assert ('ignore', 'ingest_error') == ('ignore', None)`: the chat
     was created in Ignore but stayed paused with its gap open. One assertion was corrected during
     the build, not weakened: the first version expected no "could not be processed" on
     `/status` at all; it now checks that the coverage caveat is gone and that the inbox line
     still counts the failed row.

### Owner decisions (2026-10-27)

The owner answered the three questions this record left open. Edits in Ignore (question 1): keep
the old text, as today; this fits O1. Failed updates in Ignore (question 2): clear the raw copy at
once; built, bug fix 5. Ignore and earlier data (to confirm): confirmed as today. Messages saved
before the switch stay usable when the owner asks about that chat; only summaries, catch-ups and
the waiting list leave it out.

### What the panel and the echo say now

These are fixed texts in `agent/owner_texts.py`, in the owner's locale (en, uz-Latn, uz-Cyrl, ru).
No model is involved. Button names stay in English, and a contact's name is never inflected. The
panel's mode line and counts follow the locale; the rest of the panel stays English until C4.

- Every panel view: `Mode: <b>Ignore</b> (new messages here are not saved)` /
  `Mode: <b>Observe</b> (new messages here are saved)`. The old "Ignored: new messages from this
  chat are not stored." line at the bottom is gone.
- After the Ignore confirmation, the panel opens with (en):
  > ✅ **Ignore is on** for the chat with Kamola.
  > • New messages here are not saved: no text, media or new name, just a note that a message arrived.
  > • The chat is left out of summaries, catch-ups and the list of who is waiting for your reply.
  > • What was saved before stays unless deleted: messages until 90 days old, the summary and your notes. Ignore does not hide them from my answers.
  > • To delete them: Delete stored data… on this chat's panel (/settings → Ignored chats).

  "{days}" is the account's message retention. With "Ignore + delete data" the last two bullets are
  left out, and so is the name ("the chat with the contact"), because the panel hides what is
  being deleted.
- After Observe: "✅ **Observe is on** for the chat with Kamola." plus "• New messages here are
  saved (text, media details but not the files, the contact's name) and used for summaries,
  catch-ups, search and the list of who is waiting for your reply." Coming from Ignore, it adds
  "• Messages that arrived while the chat was ignored were not saved: Telegram does not give me
  chat history." It does not say "cannot be recovered", because a contact's later edit, after
  the return to Observe, stores the edited text (review). A requeued failed update could store
  such a message too until owner decision 2; it cannot now (bug fix 5). Without
  `ai_managed` consent, it adds "• AI for your
  connected chats is off: I save these messages but do not read them with AI. Send /privacy
  managed to turn it on."
- The Ignore confirmation shows the same first three bullets before its "Ignore + delete data"
  paragraph, which is unchanged.
- Toasts: "Ignore is on", "Observe is on", "Ignore is on; deleting its data", "Deleting its data",
  "Nothing changed".
- The `set_conversation_mode` echo is "🔧 " plus the same receipt, still with Undo. No echo is sent
  when nothing changed. `mode_change_text` (`agent/tools/modes.py`) builds the receipt for both
  the panel and the echo. The tool result's `effect` says the same in English. A tainted run's
  confirmation card also shows it, so it is worded for the owner as well: "the list of who is
  waiting for a reply", "Ignore does not hide it from answers". `mode_effect` names the
  Ignore-period messages only when the chat was in Ignore: in the tool result when the change
  came from Ignore, and on the card when the chat is in Ignore now (review).

Today's deletion behaviour is described as it is: "unless deleted" covers a deletion in the chat
and the owner's own deletions. O1 (remember deleted messages) is not built. If it lands, this text
and the Ignore-mode deletion path (`service.py::record_deletion`) must change together.

### Tests

Bug fixes and new texts started with a failing test. Locally only the relevant files ran, in the
dev container (CI runs the full suite).

- **New, red first:** `tests/unit/test_owner_texts.py` (every mode text in every locale with the
  same placeholders; receipt lines exact, short, escaped, the deletion variant, the fallback name,
  the mode-line fallback). `test_conversation_modes.py`: `test_the_panel_says_what_ignore_did`,
  `test_the_panel_says_what_observe_did_and_that_ignored_messages_are_lost`,
  `test_the_mode_receipt_follows_the_owner_language` (×4 locales),
  `test_the_panel_counts_an_ignored_message_as_not_saved`,
  `test_confirming_ignore_on_an_already_ignored_chat_says_nothing_changed`,
  `test_tapping_the_current_mode_says_nothing_to_change`,
  `test_messages_in_ignore_mode_do_not_keep_an_expired_contacts_name` (B2).
  `test_memory_tools.py::test_the_mode_echo_says_what_the_mode_does`.
  `test_summaries.py::test_get_conversation_reports_messages_not_stored_while_ignored`; it sits
  next to the other `get_conversation` summary test, where the summarizer fixtures are, not in
  `test_conversation_tools.py`. `test_summaries_review.py::test_a_catch_up_says_messages_were_not_stored_while_a_chat_was_ignored`.
- **Guards, green before the change:** the five tests named under "Verification" plus the extended
  `test_new_chats_start_in_the_default_mode_set_in_settings`.
- **Existing assertions reworded (none weakened):** `test_conversation_modes.py` (mode line; the
  confirmation now asserts "What was saved before stays unless deleted" and "New messages here are
  not saved"); `test_visibility.py::test_bizchat_panel_shows_the_conversation` (mode line,
  `Messages saved: 2 (1 contact, 1 owner manual)`, and no "Not saved" line); `test_deletion_cards.py`
  (confirmation wording, plus new asserts that the Ignore + delete panel names nobody and does not
  say the data stays); the two Ignore catch-up caveat tests now assert the exact sentence instead of
  a prefix or "Ignore mode".
- **Local runs (after the fix):**

| Files | Result |
|---|---|
| `tests/unit/test_owner_texts.py`, `test_citations.py`, `test_summary_units.py`, `tests/integration/test_conversation_modes.py`, touched cases of `test_business_ingestion.py` (4), `test_summaries.py` (6), `test_summaries_review.py` (3) | 130 passed |
| `tests/integration/test_memory_tools.py`, `test_visibility.py`, `test_deletion_cards.py`, `test_conversation_tools.py` | 100 passed |

  After the review fixes (final tree):

| Files | Result |
|---|---|
| `tests/unit/test_owner_texts.py`, `test_citations.py`, `test_summary_units.py`, `tests/integration/test_conversation_modes.py`, the two Ignore-deletion cases of `test_business_ingestion.py`, `test_summaries_verification.py::test_the_notice_names_why_a_chat_was_left_out` (3) | 122 passed |
| `tests/integration/test_memory_tools.py` (with the new card test), `test_visibility.py`, `test_deletion_cards.py`, `test_conversation_tools.py`, plus the 4 `take_job` users and 4 touched cases of `test_summaries.py` and the 2 touched cases of `test_summaries_review.py` | 111 passed |

  ruff check and format (22 changed files), `mypy src evals` (148 files, no issues) and
  `lint-imports` (4 contracts kept) are clean.

  After owner decision 2 (bug fix 5):

| Files | Result |
|---|---|
| `tests/integration/test_business_ingestion.py` (all), `test_conversation_modes.py` | 62 passed |
| `tests/integration/test_ingestion.py`, `test_system_tasks.py`, the two dead-letter cases of `test_deletion_cards.py` | 49 passed |

  ruff check and format (8 changed files), `mypy src evals` (148 files, no issues) and
  `lint-imports` (4 contracts kept) are clean.

**Red-first evidence (before the fix).** The red run: 19 failed, 9 passed. The panel showed
`Mode: ignore`, `Messages: 2 (2 contact)` and no receipt. A stale confirm answered "Ignored: new
messages are not stored", and tapping the current mode answered "Observe". The name sweep left
`('Kamola', None)`. The echo was "🔧 Chat with Kamola set to <b>Ignore</b>: new messages from this
chat are no longer stored or processed; what is already stored stays." `get_conversation` raised
`KeyError: 'not_stored_while_ignored'`. The catch-up had no not-stored caveat, and both Ignore
caveats used the old wording. The unit test failed at collection (`owner_texts` has no
`IGNORE_NOT_SAVED`). The deletion guard was red once for a test reason: switching the mode by SQL
skips `set_mode`, which is what clears `pending_ai_since`. With the production setter it passed
before the fix.

### Review

Two independent reviews read the uncommitted change against the code paths it touches. Both
found the predicate, the B2 fix, D68, the receipts' escaping and scoping, and the log hygiene
sound. They raised one must-fix, which was in the tests and found by both reviewers, one
should-fix and six nits.

- **Must-fix (fixed): two guards could not fail.** In both new guards, the message delivered in
  Observe before the switch queued a `memory.refresh_summary` job. In the test that job never
  runs, so it kept the chat's queueing lock `sum:{id}`. Any later defer was swallowed
  (`AlreadyEnqueued` → None), so "the job count is unchanged" held either way. Shown first: with
  `business.py` deliberately broken (both `elif` branches after the Ignore check made `if`, so
  Ignore messages and the Ignore deletion call the trigger), the old guards passed (2 passed).
  Now each guard checks that the earlier job exists (the trigger is wired). `take_job` (moved
  from `test_summaries.py` to `business_env.py`) then hands that job to a worker, which frees the
  lock. The guard asserts that no `todo` refresh exists for the chat, both before and after the
  Ignore updates (`queued_refreshes`). Against the same broken `business.py` both guards are now
  red (`assert 1 == 0` at the final queued-refresh check). After the revert they are green, and
  `business.py` has no diff.
- **Should-fix (fixed): "cannot be recovered" was not true.** A requeue after the return to
  Observe stores a message that dead-lettered in Ignore in full, and a later edit stores its new
  text. `OBSERVE_GAP` (4 locales), the model-facing effect and a code comment no longer say
  "cannot be recovered". Owner question 2 and the ARCHITECTURE §6.5 pending note now state the
  requeue case exactly. The tests assert the new line and that the old words are gone. (The
  requeue path has been closed since owner decision 2, bug fix 5. The `OBSERVE_GAP` comment and
  §6.5 now name only the later edit.)
- **Nits fixed:**
  - The model-facing effect names the ignored period only for a chat that was ignored
    (`mode_effect`; the card reads the chat's current mode). It is worded for the owner too, and
    its comment says it is also card text. New test:
    `test_memory_tools.py::test_a_mode_card_names_the_ignored_period_only_for_an_ignored_chat`.
    The echo test now pins the exact Observe effect, with and without a change.
  - The `summarize_period` row in ARCHITECTURE §8.2 lists the new caveat.
  - The `set_to_ignore_during_summary` notice says "not summarized and their new messages are
    not stored". `test_summaries_verification.py` asserts the exact sentence.
  - The receipt unit test asserts a length bound (≤ 520 characters, all locales; 459–501 today).
- **Nits not fixed (open items above):** the AI-off line checks consent but not the processor,
  and the panel has no length guard.

### Texts for the owner to proofread

The uz-Latn, uz-Cyrl and ru wording in `agent/owner_texts.py` are drafts, as before: `MODE_LINE`,
`MODE_ON`, `IGNORE_NOT_SAVED`, `IGNORE_LEFT_OUT`, `IGNORE_KEPT`, `IGNORE_DELETE_HINT`,
`OBSERVE_SAVED`, `OBSERVE_GAP`, `OBSERVE_AI_OFF`, `MODE_UNCHANGED`, `SAVED_COUNT`,
`NOT_SAVED_COUNT`. In particular: Russian "чат: {name}" (chosen so the name is not inflected), and
"catch-up" left untranslated in uz ("catch-up sharhlari") and in parentheses in ru.

### Open items

- **Related, by design:** when a contact edits a message that arrived in Ignore, after the chat
  is back in Observe, the edited text is stored, as for any edit of a message never stored.
  That is Observe behaviour, and it is why the receipt does not say "cannot be recovered".
- **A dead letter from Observe time keeps its payload after a switch to Ignore.** Decision 2
  applies to the mode when the row fails. A row that failed while the chat was in Observe keeps
  its payload; a requeue while the chat is in Ignore stores metadata only, and one after a return
  to Observe stores it in full. The message arrived in Observe, so that is Observe behaviour. A
  failed update whose chat id cannot be read at all keeps its payload too: the hook cannot tell
  its chat or mode.
- **"AI is off" uses consent only:** the Observe receipt's AI-off line checks only that
  `ai_managed` is granted. The summarizer and the runtime also require the consent to name the
  configured model's processor (§14.3, D48). If the model moves to a processor the consent does
  not name, the receipt says the messages are used for summaries and leaves out the AI-off line,
  while summaries return NO_CONSENT. Not fixed: the panel handler has no access to the model
  configuration, and plumbing it in is more than this round's scope. A real gap only after a
  provider change without a new consent.
- **Panel length:** the receipt adds up to 520 characters to the panel, a limit the unit test now
  pins. The panel has no length guard: a summary overview near its 1,500-character cap plus five
  long notes already sits close to Telegram's 4,096 characters, and the receipt can push it over.
  The edit then fails, the stale card stays and the receipt is not shown (the mode change has
  committed and the toast is true). This headroom problem predates C3. Options: leave out the
  summary and notes when a receipt is shown, or send the receipt as its own message. That is a
  product choice, so it is not built.
- **Rest of diagnosis item 7, not done:** `coverage_started_at` still moves on Ignore messages, so
  the panel's "Observing since" of a chat that started in Ignore is the first ignored message's
  date. The label is unchanged; only the `models.py` docstring now says what the column means. The
  shared `COVERAGE_RULE` text in read-tool results does not mention Ignore.
- **Observe after Ignore** always adds the "were not saved" line to the owner's receipt, even when
  nothing arrived while the chat was ignored. It is true as stated, but could be shown only when
  the chat has unsaved rows. (The model-facing effect and the card now name the ignored period
  only for a chat that was ignored; review.)
- **Prompt relay:** the new catch-up caveat is application text that `owner_request.v7` must relay
  (E-p caveat-relay check). No prompt changed and no eval was run.
- **Not run locally:** the rest of the suite, including `test_memory_safety.py`, the retention and
  eval-summary tests, the untouched cases of `test_summaries*.py` and of `test_deletion_cards.py`.
  CI is the gate.

## G1 — stronger summary model

_Pending._

## O1 — deleted messages

_Pending._

## C4 — language at onboarding

_Pending._

## C5 — tone

_Pending._

## S03.7 prompt, harness and label items

_Pending._

## Owner decisions (fix round)

_Pending._

## Open items after the fix round

_Pending._
