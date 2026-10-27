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
| C1, P9 | "Who is waiting" misses a contact who asked last; an open request keeps "needs a reply" on | `m03fix/c1-waiting` | in progress |
| C3 | Ignore mode stores nothing new; the panel says what a mode change did | `m03fix/c3-ignore` | in progress |
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

_Pending._

## C3 — Ignore mode

_Pending._

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
