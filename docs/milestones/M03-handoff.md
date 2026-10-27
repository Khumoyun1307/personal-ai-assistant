# M03 → M04 handoff

*For the tech lead who plans M04 (Assisted replies) and directs its engineers. It is the **one document to start from**: it says what you can build on, what is still moving in M03 and why that does not affect you, and which open M03 items are yours, someone else's, or closed. You do not need to read M03.md's history; it is linked where a detail matters. Written 2026-10-26 by the M03 lead; kept current until the owner says "M03 OK" (see "How this page stays current").*

## 1. Where things stand

> **Update 2026-10-27: M03 is `done`** (owner "M03 OK"; decisions confirmed; checklist done, NFR-04 met). The owner plans an **M03 fix round before any M04 code**: C1–C5 and O1 (M03.md "Owner checklist"), G1 and the S03.7 open items. M04 planning can proceed; M04 code starts after that fix round.

- **Milestones:** M00–M02 `done`. M03 (Context, memory & summaries) has S03.1–S03.6 and the close-out merged and deployed; **S03.7 "Robust attribution and search"** (added by the owner on 2026-10-26 after the first live evals) is **complete** on branch `m03/s03.7-attribution-search` and **in its PR into `main`**, waiting for the owner's merge and a deploy (record: [M03.md](M03.md) *S03.7*). **M04 is `approved` in advance** (IMPLEMENTATION_PLAN §1.4): planning may start now; **M04 code starts only after the owner's "M03 OK"** (AGENTS §3).
- **Production:** Oracle server, production bot Alviro, `main` deployed with every M03 feature flag on, privacy policy v5 published (2026-10-26), the owner's `ai_control` and `ai_managed` consents granted. Production runs `summarize.v4` and `owner_request.v5` until S03.7 deploys; then `summarize.v7` and `owner_request.v7`.
- **Process (AGENTS §11, §15):** CI is the test gate (docs-only changes skip CI); locally only the relevant tests (if they would take ~15 min or more, push and let CI run them); a branch and a draft PR per slice; the owner merges; bug fixes start with a failing test.

## 2. What M04 can build on (stable)

These are the interfaces M04 will use. What still moves in M03 is tuning and sign-offs inside them; it does not change them (§3). Paths are under `src/assistant/`; items marked *(S03.7)* are on `main` once the S03.7 PR merges (until then on `m03/s03.7-attribution-search`).

| Area | What exists | Where |
|---|---|---|
| Agent loop | `OwnerRequestRuntime` (owner turns, bounded loop, metering, taint, confirmation cards for state changes); contact-triggered runs (`summarize`) have no tools and return validated structured output (invariant 3a) | `agent/runtime.py`, `agent/summarize.py`, ARCHITECTURE §7 |
| Tools | Registry with strict args, capability tier, run types, release flags; `for_run(..., stub_managed_tools=True)` offers chat-reading tools as **blocked stubs** returning `chat_access_off` when `ai_managed` is missing *(S03.7)* | `agent/tools/registry.py`, `agent/tools/executor.py`, ARCHITECTURE §8 |
| Confirmations | Owner confirmation cards (tool confirmations, deletion cards), Undo window | `agent/confirmations.py`, `accounts/service.py` |
| Prompts | Versioned files, active versions in one registry (never edit a used version) — `owner_request` v7 *(S03.7)*, `summarize` v7 *(S03.7)* | `agent/prompts/`, `agent/prompt_registry.py` |
| Citations | The model writes `[msg:<id>]`; the application replaces it with "(name, local time)" from the database and removes ids no tool returned this run, with a fixed note *(S03.7, D102)* | `agent/citations.py`, `agent/runtime.py` |
| Attribution | Summary items carry who asked / promised / agreed only in structured fields (`by`, `status`: promised / agreed / requested); owner-facing "who" text comes from fixed per-locale templates; a deterministic check demotes unsupported promises *(S03.7, D95–D99)* | `agent/attribution.py`, `agent/schemas.py`, `agent/tools/common.py` `summary_view` |
| Fixed owner-facing texts | Per-locale (uz-Latn, uz-Cyrl, ru, en) fixed wording; `supported_locale(account.locale_pref)` *(S03.7)* | `agent/locales.py`, `agent/owner_texts.py`, `agent/attribution.py` `WHO_TEMPLATES` |
| Search | Full-text search over stored messages; widening an empty contact-scoped search; Russian case siblings; a contact not found points to a search across all chats *(S03.7)* | `memory/retrieval.py`, `memory/search.py` |
| Memory | Owner notes (memory items), modes per chat (Observe / Ignore), reply defaults in preferences (unused until M04) | `memory/`, `accounts/preferences.py` |
| Privacy | Content-columns registry, deletion cascade (per chat, per contact, everything, managed data) as queued jobs under the owner's control lock, retention that only shortens, receipts | `privacy/`, ARCHITECTURE §10, §12.2 |
| Telegram | `OutboundGateway` is the only path for visible effects; `ControlChat` vs `BusinessChat` targets; durable poller; business-update classification | `telegram/gateway.py`, AGENTS §5, §7 |
| Evals | Synthetic E1 (summaries) and retrieval sets; summary target; retrieval deterministic / replay / variants / **agent mode** (the real runtime end to end) *(S03.7)*; a hard in-flight budget cap; owner-confirmed review ledgers gate every metric | `evals/`, `evals/README.md`, IMPLEMENTATION_PLAN §10.1 |

## 3. Still moving in M03 (fenced off from M04)

| Item | Status | Why it does not affect M04 |
|---|---|---|
| Owner reviews and sign-offs: merge and deploy S03.7; proofread the uz/ru fixed texts (`WHO_TEMPLATES`, `owner_texts.py`, `NO_CONTACT_NOTE`, v7 wording); confirm eval verdicts (E1 review → v1.1 + ledger) and the decisions (D60–D114, E-a…E-al); the M03 Telegram checklist; NFR-04 measurement; "M03 OK" | pending (owner) | product wording and sign-offs; none changes an interface |
| **G1: summarize model/effort for this route** (c36 wrong in all four prompt rounds; the fabrication gate fails on `gpt-6-luna` at effort low) | owner decision: a cent-sized `--only` trial at effort medium, then `gpt-6-sol` for the summarize route (AGENTS §8 model-choice rule) | configuration of one route (`LLM_SUMMARIZE_EFFORT`, `LLM_ROUTE_SUMMARIZE_MODEL`); the schema (v5) and `summary_view` contract stay |
| Eval label fixes (E1 v1.1: stale open labels, the c36 trap, c40.c1 values; retrieval r19, r35) | owner review pending | eval data only |

None of these changes an interface. The S03.7 eval results and open items are in [M03.md](M03.md) *S03.7* (summary fabrication not met: 3 in the v7 draft; retrieval agent mode 33/37 top-5 and 30/37 answers, run incomplete).

If any of these ever needs an interface change, the M03 lead updates §2 here and says so in the owner's channel; you do not need to watch M03's branches.

## 4. Open M03 items, already sorted

Every open item from M03.md's "Known follow-ups" (numbered as there) and the S03.7 findings has one tag. **Yours** = consider it in M04's plan. **Later** = a named later milestone owns it. **Accepted** = recorded, rare or minor, not planned. **Closed** = done by S03.7 or the M03 fix round.

| Item | Tag | Note |
|---|---|---|
| 1–3 Deletion receipts: two true receipts on an overlapping stalled retry; a lost receipt on a crash between commit and send; "could not confirm" where "nothing was deleted" would be exact | Accepted | rare; D90 |
| 4 Lifting the `ingest_error` pause on deletion may drop a lost-takeover signal | Later — M06 | matters once autonomy relies on that pause (D91) |
| 5 `except LookupError` in `privacy/requests.py` `_attempt` also catches `KeyError`/`IndexError` | Closed | M03 fix round C2: `core/errors.py::NotInAccountError` is the one "not found" error (ARCHITECTURE §8.3); `_attempt` catches only it, a `KeyError` takes the failure path (test `test_a_lookup_bug_inside_the_deletion_is_a_failure_not_not_found`). Raise `NotInAccountError`, never a bare `LookupError`, for an id that is not the account's (a unit test scans `src/`) |
| 6–8 Unattributed dead letters cleared by the 7-day sweep; `delete_managed_data` also clears pending payloads; the M01 no-consent notice can repeat on a stalled re-run | Accepted | |
| 9 Period-summary cache key lacks the prompt version | Closed | S03.7 D99 (key = range + digest + prompt version + model) |
| 10 `owner_request` search changes (name out of phrases; widen an empty filtered search) | Closed | S03.7 D100, D103, D109 |
| 11 `summarize.v4` role mix-ups, requests as promises, done items as open | Closed | S03.7 D95–D99, D105–D108, D112–D114 (no attribution error in the v5–v7 pre-reviews) |
| 12 S03.7 open items: G2–G4, P1–P9, H1–H4, D1–D3 (summary prompt, judges, labels) and S1–S9 (search gaps in agent mode) | Later — prompt/eval tuning, M03 follow-up or M04 E2 work | listed one per line with evidence and fix in M03.md *Open items from S03.7*; a prompt change is a new version and an eval re-run with the owner's cost go-ahead |
| 12 G1 (c36): a stronger summarize setting or model for this route | **Owner decision** | before relying on summaries; AGENTS §8 model-choice rule |
| 12 Owner ideas O1–O3: show deleted content in summaries; show pre-edit text; the same for automatic messages | **Owner decision** (one by one, later) | O1 reverses a privacy decision (D63/D66, PRD §20); O2 needs a new content column, retention row and deletion cascade (AGENTS §9); O3 changes D96's neutral treatment |
| S03.1 numbers like `15.00` are single lexemes; hidden forward senders matched by display name | Accepted | |
| S03.2 catch-up caveat details (ignored-chat counting, off intervals, a per-chat pause invisible to the bot) | Accepted | TF LV-04 |
| S03.2 "assistant activity in a catch-up" | **Yours** | arrives with M04's sent drafts (assistant-sent messages already count as the owner's side in attribution, D96) |
| S03.3 delegation scope, shareable kind, inferred memory | Later — M06 (R2) | |
| S03.3 `set_contact_priority` | Later — R2 | |
| S03.3 quiet hours and digest time | Later — M05 | |
| S03.3 catch-up caches not invalidated when a note changes; forgetting a note does not rebuild a rolling summary | Accepted | D75 |
| S03.5 the owner's question turns cleared per chat/contact only when their refs name the scope | Accepted | |
| S03.5 **deletion of `actions.text`, `tasks`, `delegations`, `automations`, `notifications`** | **Yours** for `actions.text`; others Later (M05–M07) | AGENTS §9: a new content column joins `privacy/content_columns.py`, ARCHITECTURE §10.1/§10.4, the sweeper and the deletion-cascade tests **in the same change** |
| S03.5 **reply defaults unused until M04** | **Yours** | `update_preferences` already stores them (tone/formality); drafting should read them |
| S03.5 `inbox requeue` "Skipped: N" counts every cleared failed row | Accepted | cosmetic |
| S03.4/S03.6 E-p: an agent-level check that the final reply relays every caveat | Later — optional eval | |
| S03.7 owner replies not always in the owner's language (10 of 33 v6 agent-mode replies; v7 adds the rule and the spelled-out script, D109/D111) | **Yours** | drafts must follow the owner's reply-language preferences; plan an eval check |
| S03.7 coverage caveat when only some chats have a coverage start | Closed for the eval (E-aj) | in the product a null `coverage_started_at` means "nothing stored yet" (`conversations/service.py`); product unchanged |
| Owner checklist 2026-10-27 (M03.md "Owner checklist"): C1 attention list misses a waiting contact; C2 notes do not save/load, silently; C3 Ignore-mode check and panel wording; C4 owner language unset at onboarding; C5 tone; O1 remember deleted messages like a coworker (owner direction) | Later — M03 fix round (before M04 code) | C2 also matters to M04 (notes feed drafts); C5 and O1 shape M04 drafting |
| TF LV-08 reopen half (does an owner message or a contact edit reopen the 24-hour window?) | **Yours** | moved to M04 by the owner (2026-10-26): run on Alviro's own connection in a planned server window with the server's poller briefly stopped and a per-run owner go-ahead — no second Premium. Until verified, assume only a new contact message reopens the window |

## 5. What M04 must respect (carried from M03)

- **Sending is new territory.** M04 is the first milestone that acts in business chats (as the owner). Invariants 1, 2, 6, 8, 13 (AGENTS §6) become live: the model proposes, `actions.execute` + policy decides; claim before calling Telegram; ambiguous outcomes are `delivery_uncertain` and never retried; no sends outside the 24-hour window; owner-approved drafts carry no AI disclosure (PRD §11.4).
- **`draft_reply` is contact-triggered:** no tools, structured output validated by code (invariant 3a); context isolated to that conversation, that contact's memory and owner preferences (invariant 4); consent `ai_managed` naming the processor (invariant 9).
- **Approval UX lives only in the control chat** (AGENTS §7), via the gateway; reuse the card patterns and the fixed-text/locale pattern.
- **Draft quality gate:** E2 (draft quality, synthetic, owner-rated; IMPLEMENTATION_PLAN §10.1) before the owner relies on drafts; the 7-day daily-use trial and the E2 rating are the owner's (§1.4). The eval harness's agent mode and the review ledger pattern are reusable.
- **Live checks** run on Alviro (production) because the dev bot cannot connect to a second account without a second Premium; plan server windows and per-run owner go-aheads.
- **Content and logs:** any new text column (drafts, `actions.text`) is a content column (AGENTS §9); logs stay content-free.

## 6. How to use this page

- Read §2, §4 ("Yours"), §5, then the M04 section of IMPLEMENTATION_PLAN (§6: S04.1 Actions and policy core, S04.2 Drafting, S04.3 Approval UX, S04.4 Verified execution, S04.5 Activity log), the PRD sections it cites, ARCHITECTURE §7–§9 and §13, and TELEGRAM_FEASIBILITY for the 24-hour window and send errors.
- Write M04's plan in `docs/milestones/M04.md` and the M04 section of IMPLEMENTATION_PLAN only. **Do not edit M03 files** (`M03*.md`, the M03 row of §1.4) — the M03 lead is still editing them.
- Consequential choices (architecture, product behaviour, new external services, data use) go to the owner first (AGENTS §3).

## 7. How this page stays current

The M03 lead updates **§1 and §3** when S03.7 merges (the "*(S03.7)*" marks in §2 then mean "on `main`") and once more at "M03 OK", and adds any new open item to §4 with its tag. Nothing else here should change. M03 is in its **final clean state** when: S03.7 is merged and deployed; its evals are pre-reviewed and confirmed by the owner; M03.md records every decision and the follow-ups as tagged here; the owner's checklist and NFR-04 are done; the decisions are confirmed; and the owner has said "M03 OK".
