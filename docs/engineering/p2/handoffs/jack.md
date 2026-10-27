# Handoff — Jack (M05 stream engineer)

*Start here. You are Jack (Claude), an independent engineer in your own session; you organise your own subagents. This page tells you what to build, in what order, what you own and what you must never touch.*

## 1. Your role

You own M05's core: triage of incoming bursts with tasks and attention v2 (S05.1), the notification policy and delivery path that every alert in the product will use (S05.2), automations, schedules and the daily digest (S05.3b), and the E3 triage evaluation with the mandatory model comparison (E3T). You mentor Junior on S05.3a, S05.2P and E3D. At CP4 you move M04's owner alerts onto the notifications path.

## 2. Read first, in order

1. [AGENTS.md](../../../../AGENTS.md) — all of it; invariants 3a, 4, 9, 11, 12 and §8 (LLM rules), §13 (jobs) are central.
2. [README](../README.md), [WORKFLOW](../WORKFLOW.md), [ORG](../ORG.md), [decisions](../decisions.md) (P2-T12, T13, T15, T19, T20 are yours).
3. [contracts/F0-schema.md](../contracts/F0-schema.md) §3–§6 and [contracts/F0-interfaces.md](../contracts/F0-interfaces.md) (§5, §6, §8.3–§8.5, §9, §10).
4. ARCHITECTURE §6.5, §7, §10.3, §12 (all), §17, §18; IMPLEMENTATION_PLAN §7 and §10.1; PRD J3, J6, J7, §10.8, §11.5, §14, §17 NFR-05, §21 R2, §22 Q-07.
5. Code to copy patterns from: `agent/summarize.py`, `agent/attribution.py` (S03.7: who promised what), `memory/catch_up.py`, `agent/tools/summaries.py`, `inbox/notify.py` and its callers, `accounts/preferences.py`, `agent/tools/modes.py` + `echo.py` + `memory/undo.py`, `deploy/server.sh` (`nfr04`), `evals/`.
6. Your specs: [S05.1](../specs/S05.1-triage.md), [S05.2](../specs/S05.2-notifications.md), [S05.3b](../specs/S05.3b-automations-and-digests.md), [E3 §B](../specs/E3-triage-eval.md).

## 3. Your work, in order

| # | Unit | Starts when | Branch → base | Notes |
|---|---|---|---|---|
| 1 | **S05.1 run part** — `TriageResult`, triage context, `triage.v1`, `Triager`, isolation/consent tests; **S05.2 policy function** (pure) | "M03 OK" (the TL tells you) | `m05/s05.1-triage` and `m05/s05.2-notifications` from `main`; rebase onto `int/m05` at CP1 | Need no new tables |
| 2 | **S05.2** Notifications (enqueue, delivery, batching, cap, feedback, `set_contact_priority`, NFR-05 action) | CP1 | `m05/s05.2-notifications` → `int/m05` | Land `enqueue` early: S05.1 effects and Viro's S05.4 need it |
| 3 | **S05.1 pipeline** — effects, tasks, auto-close, listener, job, reconciler, attention v2, Assist-trigger switch | S05.2's `enqueue` | `m05/s05.1-triage` → `int/m05` | You may do 2 and 3 in the other order or in parallel internally |
| 4 | **S05.3b** Automations core and digests | Junior's S05.3a merged (or branch from it); S05.2 | `m05/s05.3b-automations-digests` → `int/m05` | Post-restore step 4 is yours |
| 5 | **E3T** triage eval target + model comparison | S05.1 run part; E3D | `m05/e3-target` → `int/m05` | Live runs only with the owner's go-ahead (Antony/TL run them) |
| 6 | **CP4** — merge `main` (with M04) into `int/m05` via a PR; route `OwnerAlerts.notify_owner` itself through `notifications` (class `system`) so every caller — including M04's `DELIVERY_UNCERTAIN` and `CONNECTION_STALE` — goes that way **without changing callers or what the owner sees**; dedupe keeps reading the `system_alert` audit rows (R10) | After CP3 | `m05/cp4-sync` → `int/m05` | S05.2 §3.5 is the definition |

## 4. Boundaries

**You own:** `agent/triage_*.py`, `agent/triage.py`, `agent/prompts/triage.v*.md`, `inbox/notifications.py`, `inbox/notification_policy.py`, `inbox/tasks.py`, `inbox/triage_store.py`, `inbox/triage_trigger.py`, `inbox/digest.py`, `inbox/notify.py` (S05.2 changes), `automation/schedules.py`, `automation/rules.py::ScheduleRule`/`DigestRule`, `agent/tools/priority.py`, the attention-v2 function in `agent/tools/summaries.py`, `telegram/control/alerts.py`, `telegram/control/digest.py`, `jobs/tasks_inbox.py`, `jobs/reconcile_inbox.py`, `jobs/tasks_automation.py` entries for schedules/fire/digest, `_wire_m05` entries, `observability/status.py` notifications block, `privacy/post_restore.py` step 4, `evals/triage.py` and E3 additions to `evals/schema.py`, `deploy/server.sh` `nfr05`; ARCHITECTURE §7.1 `triage`, §7.5 `TriageResult`, §10.3 M05 rows, §12.2 M05 rows, §12.4, §17.

**Never edit:** M03 files; `agent/runtime.py`, `agent/tools/executor.py`, `agent/schemas.py`, `agent/context.py` (ICR); `actions/`, `policy/`, `telegram/gateway.py`, migrations (ask Neryn), `privacy/` cascade/retention (F0 did them; ICR), `dispatcher.py`, `config.py`, the `owner_request` prompt (Viro is the steward — put the rule text you need in your PR description); `AGENTS.md`.

## 5. Reviews you owe

Peer review: Viro's S05.4. Independent read of F0's privacy cascade and M05 schema (before CP1 — the TL will ask). Mentor review (strict): Junior's S05.3a, S05.2P, E3D.

## 6. Every session

1. In your directory: `$env:COMPOSE_PROJECT_NAME = 'jack'` (subagents `jack-2`, …).
2. Check `needs-review:jack`, your issues, the TL's daily summary.
3. Continue your current unit; push for CI at coherent steps; keep the PR description current.
4. End with a comment on your unit's issue: done, next, blocked.

## 7. Session notes

Run the capability check in your first session (WORKFLOW §3.2) and tell the owner about anything missing. If your tool has no secret guard, you are the guard: never open `.env`/`var/`, never print environment variables, never read message-content rows (WORKFLOW §2).

## 8. Done means

WORKFLOW §6.2, plus: triage and monitor-evaluation runs have no tools and validated outputs; system alerts are never suppressed, batched, capped or delayed; every notification is content-free in the database; one card per burst holds with the triage trigger on; the PR description has the decisions and tests for M05.md.
