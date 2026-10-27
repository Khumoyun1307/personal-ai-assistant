# AGENTS.md — Repository Instructions

Persistent instructions for anyone (human or AI coding agent) working in this repository. They apply to every change. When a rule here conflicts with a convenience, the rule wins; if a rule seems wrong, raise it with the owner and update this file once the owner agrees, rather than ignoring it.

## 1. Project in one paragraph

A **Telegram-native personal AI agent**: one Telegram bot that is both the owner's private **control chat** and a **connected business bot** (Telegram "Chat Automation" / BotFather "Secretary Mode") on the owner's personal account. It observes the private chats the owner includes, summarizes, remembers, tracks commitments, monitors, alerts, drafts replies for approval, and — only under explicit delegations — replies autonomously within Telegram's rules. It is **not** an autoresponder or a generic chatbot. Python modular monolith, PostgreSQL, OpenAI behind a provider-neutral port (ADR-07a). Single owner now; multi-user later without a rewrite.

## 2. Documents and source of truth

| Question | Document |
|---|---|
| Vision, history, owner's collaboration preferences | [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) |
| What Telegram/libraries/providers actually allow (verified, with live-test results) | [docs/TELEGRAM_FEASIBILITY.md](docs/TELEGRAM_FEASIBILITY.md) |
| What to build and how it must behave | [docs/PRODUCT_REQUIREMENTS.md](docs/PRODUCT_REQUIREMENTS.md) |
| How it is built | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| In what order, with what tests and acceptance criteria | [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) |
| Milestone reports, eval results | `docs/milestones/`, `docs/evals/` |
| Phase plans for multi-engineer phases (work units, contracts, specs, handoffs, QA) | `docs/engineering/<phase>/` (P2: [docs/engineering/p2/](docs/engineering/p2/README.md)) |

Precedence when documents disagree: **TELEGRAM_FEASIBILITY (verified facts) > PRD > AGENTS §6 safety invariants > ARCHITECTURE > IMPLEMENTATION_PLAN > code comments**. Follow the higher-ranked document and correct the lower one in the same change. Never resolve a conflict by editing the PRD, a §6 invariant or a TF fact; report it to the owner and wait. Only an owner-executed live test (date, client versions, synthetic payload summary) can mark an LV item or behaviour *Verified* in TF §12.

Never treat these documents as a substitute for the official Telegram Bot API documentation (https://core.telegram.org/bots/api). Before relying on a Telegram behaviour that is not marked *Verified* or confirmed by a live test, check the docs and add the finding, with its source URL, to TF §13 as "docs-checked, not live-verified".

## 3. Process rules

- Before any code change, read the *Milestone status* table in IMPLEMENTATION_PLAN §1.4. Only a milestone marked `approved` or `in progress` may be implemented, slice by slice in the listed order — or in the order and parallelism of an owner-approved phase plan (`docs/engineering/<phase>/`); if none is, only documentation edits are allowed. Change a status only to record the owner's explicit approval given in the conversation (note the date); never self-approve. A milestone becomes `done` only after the owner reviews `docs/milestones/Mxx.md`. Do not start the next milestone, add speculative features, or build infrastructure "for later" without owner approval.
- Within an approved milestone, execute routine implementation decisions without asking; surface **consequential** choices (architecture, product behaviour, new external services, data use) to the owner first.
- Distinguish analysis from decisions: when the owner asks "is X possible/better?", answer with analysis; do not change the plan unless asked.
- Each slice ends with: tests green, docs updated, acceptance criteria met, manual Telegram checks noted where relevant.
- Never claim something works without running it. Report failures and skipped steps plainly.

## 4. Commands

The owner develops on **Windows 11 + Docker Desktop (WSL2 backend)**. All Python tooling runs **inside the dev container** (service `app` in `compose.dev.yml` at the repo root). The Windows host needs only Docker Desktop, git and `just` (`winget install Casey.Just`). The justfile sets `set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]`, and every recipe except `up`/`down` wraps `docker compose -f compose.dev.yml run --rm app <cmd>`. **Never run `uv`, `python`, `pytest` or `alembic` on the Windows host** (native Windows is unsupported: psycopg async requires a selector event loop). The container keeps its virtualenv outside the bind mount (`UV_PROJECT_ENVIRONMENT=/opt/venv`). Integration tests use the Compose `db` with a separate `assistant_test` database (`TEST_DATABASE_URL`, created by an init script); CI uses a Postgres service container (no testcontainers).

| Task | Host (PowerShell) | In container |
|---|---|---|
| First-time setup | `just setup` | — |
| Start / stop dev stack (app + Postgres) | `just up` / `just down` | — |
| Install git hooks | `just hooks` | — |
| Full check (DoD gate; runs in CI — not on the owner's machine, §11) | `just check` | ruff check + format check + mypy + lint-imports + pytest |
| Run all tests | `just test` | `uv run pytest` |
| Lint + format check | `just lint` | `uv run ruff check . && uv run ruff format --check .` |
| Type check | `just typecheck` | `uv run mypy src evals` |
| Layer contracts | (part of `just check`) | `uv run lint-imports` |
| Configuration/health check | `just healthcheck` | `uv run python -m assistant healthcheck` |
| Apply migrations (from S01.1) | `just migrate` | `uv run python -m assistant migrate` |
| Create a migration | — | `uv run alembic revision --autogenerate -m "<message>"` then review by hand |
| Run the app | `just run` | `uv run python -m assistant run` |
| Dependency audit | `just audit` | `uv run pip-audit` |
| Apply formatting and autofixes | `just fmt` | `uv run ruff check --fix . && uv run ruff format .` |
| Shell in the dev container | `just shell` | — |
| Stop dev stack and delete its volumes (dev database) | `just down-hard` | — |
| M00 probe (dev bot only, §9) | `just probe <cmd>` | `python tools/probe/probe.py <cmd>` |
| Sanitise a probe dump into fixtures (§9) | `just sanitize var/probe/<dump>` | `python tools/probe/sanitize.py` |
| Deploy `main` to the production server | `just deploy <ip>` | — (over SSH) |
| Production log / status / backup now / R0 verifier / restore test | `just prod-logs <ip>` / `just prod-status <ip>` / `just prod-backup <ip>` / `just prod-verify-r0 <ip>` / `just prod-restore-test <ip>` | — (over SSH; server actions in `deploy/server.sh`) |
| Provision the Oracle VM; backup key; bootstrap it; push the production `.env` | `just oci-provision <email>` / `just prod-age-key` / `just prod-bootstrap <ip>` / `just prod-env-push <ip>` (owner ids from `var/prod.env`) | — (OCI CLI container; `deploy/runbooks/provision.md`) |
| Switch the laptop to the dev bot (step D, D40; recreates the dev database) | `just dev-bot-switch` | `python tools/dev/switch_dev_bot.py` |
| One-off non-polling probe call with Alviro's token (`me`, `getfile`; owner go-ahead per run) | `just probe-prod <cmd>` | `python tools/probe/probe.py --prod-token <cmd>` |
| Backup smoke test of the production stack (local, placeholder secrets) | `just smoke-backup` | — |

Run an in-container command without a recipe from the host as `docker compose -f compose.dev.yml run --rm app <cmd>`.

(These commands are created in M00 S00.1; `just migrate` exists from S01.1, `just run` from S01.2, the deploy/`prod-*` recipes from S02.5, and the Oracle/`prod-bootstrap`/`prod-env-push`/`smoke-backup` recipes and the `<ip>` arguments from S02.6; `dev-bot-switch` and `probe-prod` from M02 step D.)

## 5. Repository layout and layering

Layout is defined in ARCHITECTURE §5. Enforced rules:

- Import contracts (import-linter): (1) `accounts`, `conversations`, `policy`, `actions`, `memory`, `inbox`, `automation`, `privacy` **must not import** `aiogram`, `openai`, `procrastinate`, `assistant.telegram`, `assistant.llm`, `assistant.jobs` or `assistant.agent`; they depend on ports (Protocols). (2) `assistant.agent` must not import `aiogram`, `procrastinate`, `assistant.telegram` or `assistant.jobs`; it uses the `LLMClient` and `JobScheduler` ports. (3) Only `assistant.bootstrap` instantiates adapters. (4) `tools/` sits outside `src/`, and nothing in `src/` imports it.
- `policy/` is pure (no I/O): `decide(PolicyContext) -> Decision`.
- **Telegram calls are confined.** `telegram/gateway.py` is the only module that calls Bot API methods with visible effects (send, edit, delete, mark read, chat actions, `answerCallbackQuery`, `setMyCommands`), for control and business targets alike. Control-chat handlers go through the gateway too; never use `Message.answer()`, `CallbackQuery.answer()` or other shortcuts. `telegram/ingress/` may call only `getUpdates`, `getMe` and `deleteWebhook(drop_pending_updates=False)`; `telegram/connections.py` may call only `getBusinessConnection`. Enforced by ruff `flake8-tidy-imports.banned-api` (TID251) on `aiogram.Bot`/`aiogram.methods` with per-file ignores for these modules and `bootstrap.py`, plus a unit test that AST-scans `telegram/control/` for shortcut calls.
- Wiring happens only in `bootstrap.py`. No module-level singletons holding clients or sessions.
- `tools/probe/` and `spikes/` are throwaway code and must never be imported by `src/`.

## 6. Safety invariants (non-negotiable)

Every change must preserve these. Each invariant gets a named regression test in the first slice that touches it (listed in IMPLEMENTATION_PLAN §10); never weaken or delete these tests.

1. **The model proposes; the application decides.** No code path sends, edits, deletes, marks read, or shows chat actions on the owner's behalf without passing the policy engine immediately before execution (ARCHITECTURE §9).
2. **There is no tool that sends directly.** Agent tools may only *propose* actions; sends happen in `actions.execute` after policy re-check.
3. **Untrusted content has no capabilities.** (a) Contact-triggered runs (`triage`, `draft_reply`, `summarize`, `extract_memory`, `evaluate_monitor`) have **no tools** and return structured outputs that deterministic code validates. (b) In `owner_request` runs, tool results that contain contact-authored text, and owner messages that carry forwarded or quoted third-party text, are wrapped as untrusted and mark the run *tainted*. A tainted run cannot change state without an owner confirmation card; this covers configure tools and control tools other than `pause_autonomy`, `end_delegation` and cancel (ARCHITECTURE §7.1). `resume_autonomy` runs only via `/resume` or a card button. Contact text is never an instruction.
4. **Isolation.** Context for a contact-facing run includes only that conversation, that contact's memory, owner preferences, and (for delegations) owner-designated shareable knowledge. Never other conversations.
5. **Hard boundary between control chat and business traffic.** Business updates (`business_message`, `edited_business_message`, `deleted_business_messages`, `business_connection`) never reach control-chat handlers and are never interpreted as owner commands — even when `from` is the owner.
6. **At-most-once sends.** Claim with a committed state transition before calling Telegram; ambiguous outcomes become `delivery_uncertain` and are **never retried automatically**.
7. **Fail closed.** Unknown connection, missing rights (`rights` absent or `is_enabled=false` ⇒ no permissions), unknown conversation, missing consent, or policy error ⇒ do not act.
8. **Never act outside Telegram's rules.** No sending outside the 24-hour reply window, no initiating conversations, no workarounds, no bulk/unsolicited messages.
9. **Consent gates AI processing.** No control-chat content reaches an AI provider without an active `ai_control` consent. No managed-conversation content reaches one without an active `ai_managed` consent naming that provider; this includes tool results and control turns that quote such content. Enforced at the `LLMClient` wrapper (ARCHITECTURE §14.3); queued jobs re-check when they run.
10. **Kill switch and takeover win.** `/pause` and owner manual messages stop autonomous activity; scheduled autonomous sends re-check before executing.
11. **Never report failure as success.** User-facing text distinguishes done / awaiting approval / blocked / failed / uncertain.
12. **Owner allowlist and tenant scoping.** Every tenant table row has `account_id`; every query is scoped; updates from non-allowlisted users or connections create no domain rows and their raw payload is cleared at processing; private messages from them get only the fixed, rate-limited refusal.
13. **No AI-disclosure bypass.** An autonomous send is denied unless policy rule `disclosure_present` passes (FR-AUT-04: the first autonomous reply per conversation per delegation session and after 24 h idle; cannot be disabled while autonomy is on). Owner-approved drafts carry no disclosure (PRD §11.4). Never claim to be human (FR-AUT-12).
14. **Forbidden capabilities stay unimplemented:** deleting contacts' messages, editing the owner's messages, profile/gift/Stars/story methods, emotion inference, contact scoring, MTProto/user-client access, scraping.

## 7. Telegram integration rules

- **Never use aiogram `Message` shortcut methods** (`answer`, `reply`, `edit_*`, `delete`, `pin`, `react`, `answer_*`, `reply_*`, `forward`, `copy_to`) on business messages — they auto-inject `business_connection_id` and send **as the owner**, or act on the wrong chat. Business updates are normalized by `telegram/normalize.py`, not handled by aiogram routers. Use gateway calls everywhere.
- Do not return Telegram methods from handlers (webhook-reply style); results (message ids) are needed for audit.
- Do not use aiogram FSM for business flows; control-chat UI state lives in Postgres (`pending_inputs`).
- The gateway takes an explicit target: `ControlChat` (never attaches `business_connection_id`) or `BusinessChat` (always attaches the *current* one). Never send to a business contact's `chat_id` without a business connection.
- Typing indicators in business chats are owner-voice actions; the gateway's `chat_action` accepts only `ControlChat` until a PRD requirement and a policy rule exist for business targets.
- **Always pass an explicit `allowed_updates` list** to `getUpdates`/`setWebhook` (the setting is sticky). Never use aiogram's `start_polling`; use our durable poller (offset advanced only after commit).
- **Never use `drop_pending_updates=True`** anywhere (it permanently discards up to 24 h of business messages).
- Classify every business message first (`contact` / `owner_manual` / `owner_automated` / `assistant_sent` / ignored bot chat) using `from.id`, `sender_business_bot`, `is_from_offline`, `chat.id`.
- Key business conversations by `(account_id, business_chat_id)`; never by `chat_id` alone (separate namespace from bot chats). Treat `business_connection_id` as **mutable**; key connections by account. Bootstrap unknown connection ids with `getBusinessConnection`.
- There is **no history API**. Never write features that assume access to messages before connection or outside coverage; surface coverage gaps.
- Handle `BUSINESS_PEER_USAGE_MISSING`, `BUSINESS_PEER_INVALID`, `BUSINESS_CONNECTION_INVALID` as expected domain outcomes (no blind retries). Honour 429 `retry_after`.
- Owner-facing controls (approve/edit/reject) appear **only** in the control chat — never as buttons on messages sent into contacts' chats.
- Respect throttling (~1 msg/s per chat). Never hard-code Telegram limits in more than one place.
- Test fixtures derived from real updates must have **all text, names, usernames, file ids and media replaced with synthetic content** before committing; they are produced only by the sanitiser, run by the owner or by the agent under the §9 standing exception.

## 8. LLM and agent rules

- Call the model only through the `LLMClient` port; the OpenAI adapter uses the **official `openai` SDK** (no raw HTTP, no third-party wrappers). Verify SDK usage against official docs; do not guess method names.
- Model IDs come from configuration (`LLM_DEFAULT_MODEL=gpt-6-luna` for every route). Luna is the **cheapest tier**, so the usual cost lever is inverted: there is nothing below it, and a per-route override may raise a route to a more capable model when the M03/M05 evaluation shows Luna misses the quality thresholds on the synthetic eval set. Record every such decision. Never hard-code model strings in business logic.
- Provider specifics are confined to the adapter and must be **verified against the official OpenAI docs before use, not assumed** — the previous provider's rules (always-on thinking, banned sampling parameters, `effort`) do not carry over. Always check the finish/stop reason, including refusal, before using content. Handle truncation by retrying with a larger output budget or failing; never continue by prefilling an assistant turn.
- Agent loop: append-only messages within a run (pass assistant content back unchanged); all tool results for one turn in one message; bounded iterations, tokens, and wall time; every call metered in `llm_calls`.
- Structured outputs via the SDK's native structured-output/JSON-schema support with Pydantic models; re-validate with our own models; invalid/refused output never triggers actions.
- **Adding or changing an agent tool:** register it in `agent/tools/registry.py` with a strict Pydantic args schema, a capability class, allowed run types (`owner_request` only), a minimum capability tier and a release flag that defaults off. Never write a tool that sends, edits, deletes or marks read (outbound effects are `propose_*` tools that create an `actions` row). Follow ARCHITECTURE §8.3: validate; check that every referenced id belongs to the run's account; apply policy; be idempotent on `(run_id, tool_call_id)`; return a factual status (never "sent"); write a content-free `agent_run_steps` row. Mark results containing contact text as untrusted (invariant 3b). Test schema-valid but semantically invalid args, foreign-account ids, registry filtering, and an idempotent retry. Update the ARCHITECTURE §8.2 catalogue.
- Prompts are versioned files in `agent/prompts/` (`name.vN.md`). Never edit a version that has been used in runs: add `name.v(N+1).md`, switch the active version in the registry, and keep old files while retained `agent_runs` reference them. A change to a structured-output schema bumps the prompt version. If the prompt feeds a gated capability (summaries M03, triage M05, drafts/autonomy M06), re-run its eval set and record the result in `docs/evals/` before shipping, with the owner approving the cost. Prompts express rules (isolation, honesty about coverage, disclosure), not wording; reply language/tone/style come from owner preferences; any examples in them are synthetic.
- Prompt caching: stable content first (system, tools), volatile last; no timestamps or random ids in cached prefixes.
- **Never** send message content to provider feedback programs, file/upload APIs, or batch APIs. No fine-tuning. Model training on API data must stay disabled in the provider account (owner confirmed disabled 2026-10-18).
- Evaluation datasets are **synthetic or hand-written only** — never exported real chats (Telegram terms prohibit using Telegram data to train, validate, or benchmark AI). Evals that call the real API are run manually and their cost noted.

## 9. Data, privacy, and security rules

- Every tenant table has `account_id NOT NULL`; repositories require an account context; tests cover cross-account isolation.
- Content-bearing columns are exactly the list in ARCHITECTURE §10.1. Each has an ARCHITECTURE §10.4 retention row and is covered by the deletion-cascade tests; adding one means updating both lists, the sweeper and those tests in the same change. Job arguments are ids or enums only (e.g., `replies.draft` `reason` is an enum).
- Retention is implemented, not aspirational: new data classes need a retention rule and deletion cascade.
- **Logs never contain** message text, captions, contact names/usernames, phone numbers, tokens, API keys, or Bot API URLs (they embed the token). Use internal ids. The redaction test must pass.
- **Coding agents never read real Telegram content.** Do not open, print, grep or summarise `var/` dumps, backups, database rows with message content, or anything else holding real chat data. Fixtures are produced by a deterministic sanitiser (`tools/probe/sanitize.py`) that keeps an allow-list of structural fields and replaces every string, name, username, caption and file id. The agent writes the sanitiser and sees only its output. **This rule is absolute and is not waived by any authorization below:** permission to *run* a command is never permission to *read* content.
- **Two bots (owner decision 2026-10-22, superseding the 2026-10-21 one-bot decision).** **Alviro** is the **production** bot: only the server uses its token to poll — never a laptop. On the laptop the token lives only in the git-ignored `var/prod.env`, read by `just prod-env-push` and the non-polling `just probe-prod` (wording approved by the owner 2026-10-25) (one poller per token: a second `getUpdates` caller gets `409 Conflict` or steals updates and advances the offset, so production messages are lost to the server and real content lands on the laptop). The **dev bot** (neutral name, connected under Chat Automation to the owner's second account) is used for every local run: `just run`, probes, acceptance runs and the agent's checks. The local `.env` holds only the dev bot's token.
- **Live runs need the owner's go-ahead.** Anything using a real `BOT_TOKEN` or LLM provider key (`just run` against a real bot, evals, provider spikes) runs only when the owner asked for that specific run. Never use the production (Alviro) token in development. A one-off, non-polling call with Alviro's token from a laptop (`just probe-prod getfile …` for an LV re-check; the flag refuses every command but `me` and `getfile`) needs a per-run owner go-ahead and never runs `listen` or any `getUpdates` call; production-only behaviours are checked on the server in planned windows (R0 session).
  - **Standing exception, granted by the owner on 2026-10-18 — M00 probe commands** (from 2026-10-22: the dev bot). The agent may run `tools/probe/` commands (`me`, `listen`, `send`, `dm`, `edit`, `typing`, `delete`, `getfile`, `sendphoto`, `burst`, `connection`, `summary`) against the **dev bot** without asking each time, to execute the LV protocol. Conditions, all of which must hold:
    1. The probe's console output is content-free by default; `--verbose` prints real payloads and **an agent must never pass it**.
    2. The agent still never opens `var/probe/`. To learn what was captured it uses `probe summary` (counts, connection ids, error strings) or the sanitiser's output.
    3. Sends go only to chats in `PROBE_ALLOWED_CHAT_IDS`, and message text the agent writes is its own synthetic test text.
    4. This covers the dev bot only. The production bot (Alviro), evals and provider spikes still need a per-run go-ahead; dev-bot runs of the app are covered by the next exception.
    5. Steps needing a human in the Telegram client (connecting, pausing, editing, deleting, tapping "Manage Bot") remain the owner's.
  - **Standing exception, granted by the owner on 2026-10-19 — sanitiser and dev-bot checks** (from 2026-10-22 the dev bot is the separate non-production bot above). The agent may, without asking each time:
    1. Run the fixture sanitiser (`just sanitize var/probe/<dump>`) to produce committable fixtures. It reads only the sanitiser's output, never the dump, and checks every string value in the output is synthetic before committing (the sanitiser is a safety net, not a substitute for looking).
    2. Run the app against the **dev bot and dev database** (`just migrate`, `just run`, `just healthcheck`, `inbox requeue`) to execute a milestone's manual integration checks and acceptance runs. The LLM calls such a run makes (the owner's control-chat messages, the agent's own synthetic control-chat test messages) are covered and stay within `LLM_MONTHLY_BUDGET_USD`; the run is reported in the milestone report.
    3. Change, through the rule change process (§2), the documentation that records these permissions.
    Conditions 1–3 and 5 of the probe exception apply unchanged: content-free console and logs only, never reading `var/` or content-bearing database rows, sends only to allow-listed chats with the agent's own synthetic text, and client-side steps stay with the owner — the agent asks only when it genuinely needs the owner to act in or look at the Telegram client. Still **not** covered: the production bot (Alviro) and the production server's database, eval runs that call a real model (their cost needs the owner's approval, §8), provider spikes, and pushing or merging (§15).
- Local secrets live in `.env` at the repo root (git-ignored), loaded by Compose `env_file:`. `.env.example` (committed) lists every setting with placeholder values and is updated in the same change that adds a setting. The owner enters real values; never ask for secrets in chat. Never display secret values: do not `cat`/`type`/`Get-Content` `.env`, and do not run `docker compose config`, `env` or `printenv`. Verify configuration with `just healthcheck`, which reports missing or invalid settings by name only.
- Secret settings use pydantic `SecretStr`. Never commit `.env`, tokens, keys, `var/`, probe dumps or database dumps, and never paste secrets into chat, issues or docs.
- New external processors (AI, embeddings, transcription, error tracking, analytics) require: owner approval, consent text update, privacy policy update — before any data flows.
- Times are stored in UTC (`timestamptz`); convert to the owner's timezone only for presentation and scheduling.

## 10. Coding standards

- Python 3.14 (`requires-python = ">=3.14,<3.15"`); full type hints; `mypy --strict` clean for `src/` and `evals/`.
- Async I/O throughout; no blocking calls in the event loop (use `asyncio.to_thread` for unavoidable blocking work).
- Pydantic v2 (version capped by aiogram) for settings, tool arguments, structured outputs, and JSON columns; dataclasses (frozen) for internal value objects.
- SQLAlchemy 2.1 typed ORM (`Mapped[]`, `mapped_column`), explicit driver URLs (`postgresql+psycopg://`), explicit transactions via the unit of work; no implicit autocommit side effects; no lazy loading in async code paths.
- Errors: domain errors are typed exceptions or result types; catch specific exceptions (e.g., SDK typed errors), never bare `except`. Telegram and LLM errors are classified at the adapter boundary.
- Inject time (`Clock`) and ids; no direct `datetime.now()` in domain code.
- Keep functions small and named for intent; comments explain *why*, not *what*. Match surrounding style.
- No new dependency without a reason recorded in the PR/commit message; prefer the standard library and existing dependencies.

## 11. Testing standards

- Every code change adds or updates tests at the appropriate layer (unit / integration / contract / scenario — IMPLEMENTATION_PLAN §10).
- Integration tests use a **real PostgreSQL 18** (the Compose `db` service locally; a service container in CI), never SQLite.
- Tests never call real Telegram or real LLM APIs. Use the fakes in `tests/fakes/` (`FakeTelegramSender`, `FakeUpdateSource`, `FakeLLM`, `FakeClock`, `FakeChannel`). Real-API evaluations live in `evals/` and run manually.
- Any change touching sending, policy, classification, ingestion, or consent must keep the **mandatory regression suite** green (IMPLEMENTATION_PLAN §10) and add a test for the new behaviour.
- Bug fixes start with a failing test.
- Tests are deterministic: no sleeps for timing (use the fake clock), no reliance on wall-clock ordering.
- **Where tests run (owner decision 2026-10-26).** GitHub CI runs the full suite; it is the test gate. Never run `just check` or the full pytest suite on the owner's machine. Locally, run only the tests directly relevant to the change, and only when they are short (a few files, minutes — not tens of minutes); lint, format and mypy on the changed files are fine. If the relevant tests would take about as long as CI's full run (roughly 15 minutes; estimate by their share of the suite), push and let CI run them instead. Reviewers likewise read the diff and run only the relevant tests. Documentation-only changes (`docs/`, root `*.md`, runbooks, `tools/**/*.md`, `evals/README.md`) skip CI (`paths-ignore` in `ci.yml`); prompt and rubric files are code and do not.
- **Per-slice loop.** Build (implement; relevant tests locally) → review (reviewers on the diff; relevant tests only) → fix → push so CI runs the full suite. If CI fails or the review finds more, fix and repeat until CI is green and the review is clean.

## 12. Database and migrations

- Every schema change ships an Alembic migration, reviewed by hand (autogenerate is a starting point), tested up and down.
- Never edit an applied migration; add a new one.
- procrastinate schema migrations are vendored as Alembic revisions; review them on every procrastinate upgrade.
- Destructive migrations (drops, type changes on content) require a data-handling note in the commit message.
- Changing a persisted jsonb model requires a backward-compatible addition or an Alembic data migration that rewrites existing rows.

## 13. Jobs

- Job payloads carry **ids (and enums) only** (§9); handlers reload state and re-check conditions (idempotent; safe to run twice).
- Domain rows are the source of truth for scheduled work; jobs are triggers; reconcilers recover lost defers.
- Use `lock="acct:{id}:control"` for owner turns. Never put a procrastinate `lock` on a job deferred with `schedule_at`/`schedule_in` (a future locked job blocks every later same-lock job until it runs). Serialize per-conversation writes with short transactions that lock the conversation row (ARCHITECTURE §9.4 lock order); never hold a row lock across an LLM or Telegram call. Use queueing locks for debounce/dedupe; `queueing_lock` + `schedule_in` coalesces from the first message (fixed window), so a trailing debounce is done by the handler re-deferring itself.
- Bounded retries with small backoff; Telegram terminal errors and ambiguous sends are **not** retried; never `retry=True`.
- Long-running LLM work runs in jobs, never in the inbox processor.

## 14. Dependencies and upgrades

- Versions pinned in `uv.lock`. Upgrades are deliberate: read the changelog (especially aiogram — Bot API changes; openai — API changes; procrastinate — migrations), let CI run the full suite (§11), and note the upgrade in the commit.
- Do not move to Python 3.15 until aiogram and pydantic-core support it.
- Re-check Bot API changes against TELEGRAM_FEASIBILITY when upgrading aiogram; if behaviour changed, add the finding to TF §13 and report it to the owner (§2).

## 15. Git and commits

- Branch per slice (`m01/s01.2-durable-ingestion`); small, focused commits; Conventional Commit style (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`).
- Never commit secrets, `.env`, `var/`, probe dumps, or database files. Before committing, run the tests relevant to the change (§11); CI runs the full suite. The pre-commit hook (installed by `just hooks`; runs gitleaks and ruff inside the container) must pass; never use `--no-verify`.
- **Stacked draft PRs (owner decision 2026-10-26).** Each slice gets a draft PR as soon as it is ready for CI; CI is the test gate. Each slice branches from the previous slice, and its PR targets the previous slice's branch (main for the first).
- **Who merges (owner decision 2026-10-27).** The agent merges a PR whose CI is green and whose review is clean (a docs-only PR, which skips CI, once its review is clean); this includes PRs straight to `main`, the usual flow. Merge a stack bottom-up: merge the lowest PR into `main`, then retarget the next one to `main` (`gh pr edit <n> --base main`) before merging it — a stacked PR merged into its old base branch never reaches `main`. Before a deploy, check that the stack tip is on `main`. The exception is a **separate feature or integration branch**: the agent may merge the PRs into that branch, but the final PR from it into `main` is the owner's. After merges the agent removes the merged slices' worktrees, containers, volumes and branches and updates local `main`. Merging is not deploying: a deploy still needs the owner's go-ahead.
- **Integration branches in multi-engineer phases (owner decision 2026-10-26, phase P2).** Each milestone of the phase has an integration branch (`int/m04`, `int/m05`) cut from `main`; each unit's draft PR targets its integration branch; the Technical Lead merges a unit PR into it once CI is green and the peer, QA and TL reviews are done; the owner merges each integration branch into `main` once per milestone. Shared foundation work (P2's F0) goes straight to `main`. The Technical Lead merges P2's PRs into `main` — F0, docs, datasets, the QA plan — under the rule above (owner, 2026-10-27); only the integration branches' PRs into `main` stay the owner's. Details: `docs/engineering/p2/WORKFLOW.md`.
- Commit, push and merge only when asked by the owner or as this section allows (the per-slice loop above is an approved workflow).

## 16. Documentation rules

- Keep docs consistent with code in the same change: architecture deltas, new decisions (add/adjust ADRs in ARCHITECTURE §20), verified Telegram behaviours (TELEGRAM_FEASIBILITY §12, from owner-executed live tests only; docs-checked findings go to TF §13 — see §2), eval results (`docs/evals/`), milestone reports (`docs/milestones/`).
- Mark unverified claims as unverified. Cite sources for platform facts.
- Privacy policy (`docs/legal/privacy-policy.md`) changes require a version bump and, if processing scope grows, an owner notice with opt-out before taking effect.

## 17. Never do

- Send, edit, delete, mark read, or show chat actions **in a business chat (as the owner)** outside `actions.execute` + policy. Control-chat messages go through `OutboundGateway` with a `ControlChat` target.
- Give tools to contact-triggered runs, or let a tainted owner run change state without a confirmation card.
- Mix data across conversations or accounts in prompts.
- Use aiogram shortcuts on business messages, aiogram `start_polling`, or `drop_pending_updates=True`.
- Assume access to chat history, channels the bot does not administer, or chats outside Chat Automation scope.
- Integrate MTProto/user clients, scrape `t.me`, or add group features without an approved decision.
- Log message content or secrets; commit secrets or real chat data.
- Use real chats for tests or evaluations.
- Retry an ambiguous send.
- Add microservices, message brokers, vector databases, or new external processors without an approved decision.
- Name the bot or product with "Telegram".

## 18. Definition of done

See IMPLEMENTATION_PLAN §1.5 (global Definition of Done) and the milestone's acceptance criteria.
