# M03 → M04 handoff

*For the tech lead who plans M04 (Assisted replies) and directs its engineers. It is the **one document to start from**: it says what you can build on, what is still moving in M03 and why that does not affect you, and which open M03 items are yours, someone else's, or closed. You do not need to read M03.md's history; it is linked where a detail matters. Written 2026-10-26 by the M03 lead; kept current until the owner says "M03 OK" (see "How this page stays current").*

## 1. Where things stand

- **Milestones:** M00–M02 `done`. M03 (Context, memory & summaries) has all its planned slices S03.1–S03.6 merged and deployed; **S03.7 "Robust attribution and search"** (added by the owner on 2026-10-26 after the first live evals) is being finished on branch `m03/s03.7-attribution-search` and will merge as one PR. **M04 is `approved` in advance** (IMPLEMENTATION_PLAN §1.4): planning may start now; **M04 code starts only after the owner's "M03 OK"** (AGENTS §3).
- **Production:** Oracle server, production bot Alviro, `main` deployed with every M03 feature flag on, privacy policy v5 published, the owner's `ai_control` and `ai_managed` consents granted. Summaries in production run on `summarize.v4` until S03.7 deploys.
- **Process (AGENTS §11, §15):** CI is the test gate (docs-only changes skip CI); locally only the relevant tests (if they would take ~15 min or more, push and let CI run them); a branch and a draft PR per slice; the owner merges; bug fixes start with a failing test.

## 2. What M04 can build on (stable)

These are the interfaces M04 will use. S03.7's remaining work is tuning inside them; it does not change them (§3). Paths are under `src/assistant/`; items marked *(S03.7)* are on the S03.7 branch until it merges.

| Area | What exists | Where |
|---|---|---|
| Agent loop | `OwnerRequestRuntime` (owner turns, bounded loop, metering, taint, confirmation cards for state changes); contact-triggered runs (`summarize`) have no tools and return validated structured output (invariant 3a) | `agent/runtime.py`, `agent/summarize.py`, ARCHITECTURE §7 |
| Tools | Registry with strict args, capability tier, run types, release flags; `for_run(..., stub_managed_tools=True)` offers chat-reading tools as **blocked stubs** returning `chat_access_off` when `ai_managed` is missing *(S03.7)* | `agent/tools/registry.py`, `agent/tools/executor.py`, ARCHITECTURE §8 |
| Confirmations | Owner confirmation cards (tool confirmations, deletion cards), Undo window | `agent/confirmations.py`, `accounts/service.py` |
| Prompts | Versioned files, active versions in one registry (never edit a used version) — `owner_request` v6 *(S03.7)*, `summarize` v6 *(S03.7)* | `agent/prompts/`, `agent/prompt_registry.py` |
| Citations | The model writes `[msg:<id>]`; the application replaces it with "(name, local time)" from the database and removes ids no tool returned this run, with a fixed note *(S03.7, D102)* | `agent/citations.py`, `agent/runtime.py` |
| Attribution | Summary items carry who asked / promised / agreed only in structured fields (`by`, `status`: promised / agreed / requested); owner-facing "who" text comes from fixed per-locale templates; a deterministic check demotes unsupported promises *(S03.7, D95–D99)* | `agent/attribution.py`, `agent/schemas.py`, `agent/tools/common.py` `summary_view` |
| Fixed owner-facing texts | Per-locale (uz-Latn, uz-Cyrl, ru, en) fixed wording; `supported_locale(account.locale_pref)` *(S03.7)* | `agent/locales.py`, `agent/owner_texts.py`, `agent/attribution.py` `WHO_TEMPLATES` |
| Search | Full-text search over stored messages; widening an empty contact-scoped search; Russian case siblings *(S03.7)* | `memory/retrieval.py`, `memory/search.py` |
| Memory | Owner notes (memory items), modes per chat (Observe / Ignore), reply defaults in preferences (unused until M04) | `memory/`, `accounts/preferences.py` |
| Privacy | Content-columns registry, deletion cascade (per chat, per contact, everything, managed data) as queued jobs under the owner's control lock, retention that only shortens, receipts | `privacy/`, ARCHITECTURE §10, §12.2 |
| Telegram | `OutboundGateway` is the only path for visible effects; `ControlChat` vs `BusinessChat` targets; durable poller; business-update classification | `telegram/gateway.py`, AGENTS §5, §7 |
| Evals | Synthetic E1 (summaries) and retrieval sets; summary target; retrieval deterministic / replay / variants / **agent mode** (the real runtime end to end) *(S03.7)*; a hard in-flight budget cap; owner-confirmed review ledgers gate every metric | `evals/`, `evals/README.md`, IMPLEMENTATION_PLAN §10.1 |

## 3. Still moving in M03 (fenced off from M04)

| Item | Status | Why it does not affect M04 |
|---|---|---|
| S03.7 summary quality (`summarize.v6`) | live eval re-running; agent pre-review follows | prompt wording and eval harness only; the summary schema (v5) and `summary_view` contract stay |
| S03.7 search quality | diagnosis done: part harness (no retry on provider outages, metric counts only search top-5), part prompt (bare keywords not treated as a search; a name that is not a contact stops the search). Planned: harness fixes and **`owner_request.v7`** (needs the owner's OK) | prompt rules and eval harness; the tool interfaces stay |
| S03.7 docs (M03.md section, decisions page) and the PR | after the fixes | docs only |
| Eval labels (E1 v1.1, retrieval r19) | owner review pending | eval data only |
| Owner steps: proofread uz/ru fixed texts; sign off the RG-08 test change (D104); permission-card behaviour after "Not now"; confirm eval verdicts and decisions; the M03 Telegram checklist; NFR-04 measurement; "M03 OK" | pending | product wording and sign-offs; none changes an interface |

If any of these ever needs an interface change, the M03 lead updates §2 here and says so in the owner's channel; you do not need to watch M03's branches.

## 4. Open M03 items, already sorted

Every open item from M03.md's "Known follow-ups" (numbered as there) and the S03.7 findings has one tag. **Yours** = consider it in M04's plan. **Later** = a named later milestone owns it. **Accepted** = recorded, rare or minor, not planned. **Closed** = done by S03.7.

| Item | Tag | Note |
|---|---|---|
| 1–3 Deletion receipts: two true receipts on an overlapping stalled retry; a lost receipt on a crash between commit and send; "could not confirm" where "nothing was deleted" would be exact | Accepted | rare; D90 |
| 4 Lifting the `ingest_error` pause on deletion may drop a lost-takeover signal | Later — M06 | matters once autonomy relies on that pause (D91) |
| 5 `except LookupError` in `privacy/requests.py` `_attempt` also catches `KeyError`/`IndexError` | Yours (optional hygiene, XS) | a dedicated `SubjectNotFound`; fold into any slice that touches `privacy/` |
| 6–8 Unattributed dead letters cleared by the 7-day sweep; `delete_managed_data` also clears pending payloads; the M01 no-consent notice can repeat on a stalled re-run | Accepted | |
| 9 Period-summary cache key lacks the prompt version | Closed | S03.7 D99 |
| 10 `owner_request` search changes (name out of phrases; widen an empty filtered search) | Closed | S03.7 D100, D103 |
| 11 `summarize.v4` role mix-ups, requests as promises, done items as open | Closed (pending S03.7's last eval) | S03.7 D95–D99, D105–D108 |
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
| S03.7 owner replies not always in the owner's language (10 of 33 agent-mode replies) | **Yours** (and S03.7 v7 tries) | drafts must follow the owner's reply-language preferences; plan an eval check |
| S03.7 coverage caveat when only some chats have a coverage start | Later — S03.7 or M04 | a null start is ambiguous (`agent/tools/common.py` `account_coverage`) |
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
