# Handoff — Neryn (send-path engineer, schema steward)

*Start here. You are an independent engineer in your own session; you organise your own subagents. This page tells you what to build, in what order, what you own and what you must never touch.*

## 1. Your role

You build the part of M04 where a mistake costs the most: the foundation every unit builds on (F0), the actions core and policy engine (S04.1), and verified, at-most-once execution with the kill switch (S04.4). You are also the **schema steward**: you hand out Alembic revision numbers and keep the content-column, retention and deletion obligations correct for the whole phase.

## 2. Read first, in order

1. [AGENTS.md](../../../../AGENTS.md) — all of it; §6 (invariants 1, 2, 6, 7, 8, 10, 11, 12), §7, §9, §12, §13 are yours to protect.
2. [README](../README.md), [WORKFLOW](../WORKFLOW.md), [ORG](../ORG.md), [decisions](../decisions.md).
3. [contracts/F0-schema.md](../contracts/F0-schema.md) and [contracts/F0-interfaces.md](../contracts/F0-interfaces.md) — you implement both.
4. ARCHITECTURE §6.1–§6.8, §9 (all), §10, §12, §13, §15.6, §18; IMPLEMENTATION_PLAN §6 and §10; TELEGRAM_FEASIBILITY §4 and §12.
5. Your specs: [F0](../specs/F0-foundation.md), [S04.1](../specs/S04.1-actions-and-policy.md), [S04.4](../specs/S04.4-verified-execution.md).

## 3. Your work, in order

| # | Unit | Starts when | Branch → base | Notes |
|---|---|---|---|---|
| 1 | **F0** Foundation | The owner says "M03 OK" (the TL tells you) | `p2/f0-foundation` from `main` → `main` | On everyone's critical path. If it runs past ~2 days, split into F0a (schema, models, privacy, core moves) and F0b (registries, settings, fakes) — F0a first |
| 2 | **S04.1** Actions core and policy | CP1 (F0 merged; `int/m04` exists) | `m04/s04.1-actions-policy` → `int/m04` | `propose()` unblocks Viro's S04.2 part b: land it early (a first PR with `propose` + transitions is welcome, then the full rule set) |
| 3 | **S04.4** Verified execution, kill switch, takeover | S04.1's transitions and `decide` exist (branch from S04.1's branch if not yet merged) | `m04/s04.4-verified-execution` → `int/m04` | The gateway business methods depend only on F0 — start them as soon as CP1 |
| 4 | CP2–CP3 support | — | — | Answer review findings; support the live windows (Antony scripts, TL runs server steps); restore test (OPS-RT) review |
| 5 | M04 trial fixes | During the trial | `m04/fix-<topic>` → `main` via the TL's process | Bug fixes start with a failing test |

## 4. Boundaries

**You own:** `migrations/versions/*` (steward), `actions/*` except `actions/ingestion.py` and `request_draft` in `actions/drafts.py` (Viro) and `actions/activity.py` (Junior), `policy/*`, `core/reply_window.py`, `core/delivery.py`, `core/jobs.py` P2 additions, `conversations/listeners.py`, `conversations/service.py`, `telegram/business.py`, `telegram/gateway.py`, `telegram/business_targets.py`, `telegram/control/routes.py`, the F0 refactors of `dispatcher.py`, `jobs/tasks.py`, `jobs/reconcile.py`, `bootstrap.py` (F0) and `_wire_m04` entries for your units, `privacy/*` P2 changes, `agent/tools/autonomy.py`, `jobs/tasks_actions.py` and `jobs/reconcile_actions.py` entries for `actions.execute` and `system.mark_uncertain`, the texts of your two alert kinds in `inbox/notify.py`, ARCHITECTURE §6.5, §6.6, §6.8, §9.1–§9.4, §10, §12.2 (M04 rows), §13, §15.6.

**Never edit:** M03 files; `agent/runtime.py` (except F0's `ToolContext.turn_id` line), `agent/tools/executor.py`, `agent/schemas.py`, `agent/context.py` (ICR needed); `conversations/modes.py` (Viro's in M04); ARCHITECTURE §6.7 and §9.5 (Viro's); any prompt; M05 modules beyond F0's skeletons; `AGENTS.md`; IMPLEMENTATION_PLAN except via the TL.

**Steward duty:** anyone needing a migration after F0 asks you on their issue; you reply with the next revision number and the `down_revision`, and record it in their issue. Keep exactly one head.

## 5. Reviews you owe

Peer review: Viro's S04.2, S04.3; Jack's S05.2, S05.3b. Technical read: Antony's QA-S4 scenario tests (do they encode the spec correctly?), OPS-TV, OPS-RT. Turnaround: start within your next session.

## 6. Every session

1. `$env:COMPOSE_PROJECT_NAME = 'neryn'` in your directory (subagents: `neryn-2`, `neryn-3`, … each in its own worktree).
2. Check `needs-review:neryn`, your issues, ICRs touching your contracts, and the TL's daily summary.
3. Continue your current unit; push for CI when a coherent step is done; keep the PR description current.
4. End with a comment on your unit's issue: done, next, blocked.

## 7. Done means

WORKFLOW §6.2, plus: every test your spec names exists and is green in CI; the race tests use explicit barriers, not timing; `lint-imports` proves `policy` touches no database code; no business send path exists outside `actions/executor.py`; the PR description has the decisions and test list for M04.md.

## 8. Escalate (to the TL, on the issue) when

A spec or contract seems wrong or unsafe; you need a change to a frozen contract (ICR); a higher document conflicts with a spec; a test guarding an invariant fails and you cannot see why; anything would need the production bot or server.
