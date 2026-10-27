# Handoff — Viro (product-flow engineer)

*Start here. You are an independent engineer in your own session; you organise your own subagents. This page tells you what to build, in what order, what you own and what you must never touch.*

## 1. Your role

You build what the owner sees and touches in M04: the reply drafter and the propose tools (S04.2), the approval cards with edit, reject, expiry and copy-ready text (S04.3), and the E2 draft evaluation (E2T). Then, in M05, monitors (S05.4). You are the steward of the `owner_request` prompt bumps for both milestones (P2-T17), and you mentor Junior on S04.5, E2D and E4D.

## 2. Read first, in order

1. [AGENTS.md](../../../../AGENTS.md) — all of it; §6 invariants 1–4, 9, 11 and §7–§8 are central to your work.
2. [README](../README.md), [WORKFLOW](../WORKFLOW.md), [ORG](../ORG.md), [decisions](../decisions.md).
3. [contracts/F0-interfaces.md](../contracts/F0-interfaces.md) (§2, §3, §5–§9 especially) and [contracts/F0-schema.md](../contracts/F0-schema.md) §2.
4. ARCHITECTURE §6.5, §6.7, §7 (all), §8 (all), §9.4–§9.5; IMPLEMENTATION_PLAN §6, §7 (S05.4), §10.1; PRD J3, J4, J6, J8, J10, §10.6, §11.4, §12.1.
5. The code you will copy patterns from: `agent/summarize.py` (structured runs), `agent/context.py`, `memory/items.py::contact_facing`, `agent/tools/*` (registry, executor, modes, deletion's `describe`), `agent/confirmations.py`, `telegram/control/cards.py`, `handlers.py`, `panels.py`, `evals/` (agent mode on S03.7).
6. Your specs: [S04.2](../specs/S04.2-drafting.md), [S04.3](../specs/S04.3-approval-ux.md), [E2 §B](../specs/E2-drafts-eval.md), [S05.4](../specs/S05.4-monitors.md).

## 3. Your work, in order

| # | Unit | Starts when | Branch → base | Notes |
|---|---|---|---|---|
| 1 | **S04.2 part a** — `draft_reply` run (schema, context, prompt v1, Drafter, isolation and consent tests) | "M03 OK" (the TL tells you) | `m04/s04.2-drafting` from `main`; rebase onto `int/m04` at CP1 | Needs no new tables |
| 2 | **S04.2 part b** — propose tools, Assist mode, `ActionsIngestionListener`, `replies.draft`, drafts reconciler | CP1 and Neryn's `propose()` (branch from S04.1's branch if it is not merged yet) | same branch → `int/m04` | |
| 3 | **S04.3** Approval UX + the M04 `owner_request` bump | S04.2 part b | `m04/s04.3-approval-ux` → `int/m04` | Includes `replies.revise`, `system.expire_actions`, `system.sync_action_cards` |
| 4 | **E2T** draft eval target | S04.2 part a merged; E2D available | `m04/e2-target` → `int/m04` | No live run without the owner's go-ahead (Antony or the TL runs it) |
| 5 | **S05.4** Monitors + the M05 `owner_request` bump | S05.2's `enqueue` merged in `int/m05` | `m05/s05.4-monitors` → `int/m05` | |

## 4. Boundaries

**You own:** `agent/draft_schemas.py`, `agent/draft_context.py`, `agent/drafting.py`, `agent/prompts/draft_reply.v*.md`, `agent/tools/replies.py`, `actions/ingestion.py`, `request_draft` in `actions/drafts.py`, `conversations/modes.py` (M04 changes), the `Source.DRAFT` member in `agent/untrusted.py`, `telegram/control/approvals.py`, `jobs/tasks_actions.py` entries for `replies.*`, `system.expire_actions`, `system.sync_action_cards`, `jobs/reconcile_actions.py` drafts reconciler, the `_wire_m04` entries for those, the Assist row in `telegram/control/panels.py`'s conversation panel, `agent/tools/modes.py` (accepting `assist`), `evals/drafts.py`, E2 additions to `evals/schema.py`; for S05.4: `agent/monitor_*.py`, `agent/prompts/evaluate_monitor.v*.md`, `agent/tools/monitors.py`, `automation/monitors.py`, `automation/monitor_trigger.py`, `automation/rules.py::MonitorRule`, `telegram/control/monitors.py`, `jobs/tasks_automation.py` entry for `monitors.evaluate`; the `owner_request` prompt versions in M04 (in S04.3) and M05 (in S05.4); ARCHITECTURE §6.7 (approval-card paragraph), §7.1 `draft_reply`, §7.3, §7.5 `DraftResult`, §8.2 M04 tools and monitor rows, §9.5.

**Never edit:** M03 files; `agent/runtime.py`, `agent/tools/executor.py`, `agent/schemas.py`, `agent/context.py` (ICR needed); `policy/`, `actions/` outside `ingestion.py` and `request_draft` in `drafts.py`, `telegram/gateway.py`, migrations (ask Neryn), `privacy/`, `dispatcher.py`, `config.py`; `AGENTS.md`.

**Prompt steward:** other units put the `owner_request` rule text they need into their PR description; you fold them into one version bump per milestone (never edit a used version; new file `owner_request.vN.md`; registry switch), and ask Antony/the TL to re-run the retrieval agent-mode eval (owner go-ahead, ≈ USD 0.10–0.15).

## 5. Reviews you owe

Peer review: Neryn's F0, S04.1, S04.4 (read the send path adversarially: could anything send twice, send stale text, or send without the Tx A check?); Jack's S05.1. Mentor review (strict): Junior's S04.5, E2D, E4D.

## 6. Every session

1. In your directory: `$env:COMPOSE_PROJECT_NAME = 'viro'` (subagents `viro-2`, …).
2. Check `needs-review:viro`, your issues, the TL's daily summary.
3. Continue your current unit; push for CI at coherent steps; keep the PR description current.
4. End with a comment on your unit's issue: done, next, blocked.

## 7. Codex session notes

- AGENTS.md is read automatically, but there is **no guard hook**: never open `.env` or `var/`, never print environment variables, never read database rows with message content (WORKFLOW §2).
- Run the capability check (WORKFLOW §3.2) in your first session. If your sandbox cannot reach Docker, commit (a shared `.git` from a worktree is often blocked — use a separate clone), push or use `gh`, tell the owner; the owner fixes it.
- Never pass a trailing punctuation mark right after a command in a shell line.

## 8. Done means

WORKFLOW §6.2, plus: drafts are produced only by runs with no tools; every propose result says "awaiting approval" or "blocked", never "sent" (RG-20); one live card per conversation; isolation tests inspect FakeLLM prompts (RG-09); the PR description has the decisions and tests for M04.md / M05.md.
