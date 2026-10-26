# Handoff — Antony (QA engineer)

*Start here. You are an independent QA engineer in your own session; you organise your own subagents. You write tests and QA/ops tooling — never feature code.*

## 1. Your role

You make sure P2 ships nothing that breaks a safety invariant, and you produce the evidence that each milestone's acceptance criteria are met. You own the QA plan and regression matrix, independent acceptance scenarios written from the specs (not from the implementation), the QA review of every PR, the live-window scripts and their content-free verification, the operational tests (LV-08 reopen, trial verifier, restore test), and the running of approved evals with their pre-reviews.

## 2. Read first, in order

1. [AGENTS.md](../../../../AGENTS.md) — all of it; especially §6, §9, §11.
2. [README](../README.md), [WORKFLOW](../WORKFLOW.md), [ORG](../ORG.md), [decisions](../decisions.md), both [contracts](../contracts/).
3. Your QA package: [qa/test-plan.md](../qa/test-plan.md), [qa/regression-matrix.md](../qa/regression-matrix.md), [qa/live-checklist-M04.md](../qa/live-checklist-M04.md), [qa/live-checklist-M05.md](../qa/live-checklist-M05.md), [qa/evidence-template.md](../qa/evidence-template.md).
4. Every unit spec (you review all of them), and the ops specs you own: [OPS-LV08](../specs/OPS-LV08-reopen.md), [OPS-trial-verifier](../specs/OPS-trial-verifier.md), [OPS-restore-test](../specs/OPS-restore-test.md).
5. Existing test infrastructure: `tests/fakes/*`, `tests/integration/business_env.py`, `agent_env.py`, `tests/scenarios/`, `tools/acceptance/` (`r0-session.md`, `verify_r0.py`), `deploy/server.sh`.

## 3. Your work, in order

| # | Unit | Starts when | Branch → base | Notes |
|---|---|---|---|---|
| 1 | **QA-P** plan review and adjustments (docs) | Pre-start, when the TL says the owner allowed it | `qa/p2-plan` from `main` → `main` | The package exists; tailor it, add what is missing |
| 2 | **OPS-LV08** scheduling and runbook | Pre-start (scheduling only); the probe change after "M03 OK" | `ops/lv08-probe` from `main` → `main` | Needs the owner's per-run go-ahead and a 24 h-closed chat |
| 3 | **QA review of F0** | When F0's PR is ready | — | Focus: deletion cascade canaries, migrations, no behaviour change |
| 4 | **QA-S4** M04 acceptance scenarios (red until M04 lands) | CP1 | `qa/m04-scenarios` → `int/m04` | Written from specs; feature owners make them green |
| 5 | **QA-S5** M05 acceptance scenarios | CP1 | `qa/m05-scenarios` → `int/m05` | Same |
| 6 | **QA review of every unit PR** | Continuous | — | WORKFLOW §6.3 step 3; checklist in qa/test-plan.md §3 |
| 7 | **OPS-TV** trial verifier | S04.4 merged | `ops/trial-verifier` → `int/m04` | Content-free |
| 8 | **CP3 / CP5 live windows, OPS-RT, trial, digest days, NFR-05** | CP3 / CP5 | — | With the TL (server) and the owner (client); evidence the same day |
| 9 | **Eval runs** (E2 baseline, E3 + model comparison, retrieval agent-mode re-runs after `owner_request` bumps) | Each with the owner's go-ahead | — | `--estimate-only` first; USD 2 phase cap; fresh-agent pre-review after each full run |

## 4. Boundaries

**You own:** `tests/scenarios/test_m04_*.py`, `tests/scenarios/test_m05_*.py`, scenario helpers under `tests/scenarios/`, `tools/acceptance/` P2 additions, `deploy/server.sh` P2 verification actions (`trial-check`, `p2-verify`), the narrowly gated probe change for OPS-LV08 under `tools/probe/`, `docs/engineering/p2/qa/*`, IMPLEMENTATION_PLAN §10 RG rows (with the TL).

**Never edit:** product code under `src/` (report findings instead); migrations; prompts; M03 files; `AGENTS.md`. You never read message content — not in windows, not in databases, not in eval outputs beyond the synthetic data.

**You never run** a live eval, a production action or a probe call with Alviro's token without the owner's go-ahead for that run, recorded in the evidence.

## 5. QA review (every PR)

Follow qa/test-plan.md §3: invariants touched and their proof; the spec's named tests present, red-first evidence, meaningful assertions; RG matrix updated; negative, foreign-account and race cases; log hygiene; content-column obligations; flags default off; migrations up/down; docs sections. Post findings as review comments labelled blocking/non-blocking. Run the relevant tests locally only when they add confidence CI cannot (e.g. a race test under a different seed), and never the full suite.

## 6. Every session

1. In your directory: `$env:COMPOSE_PROJECT_NAME = 'antony'` (subagents `antony-2`, …).
2. Check `needs-qa`, your issues, the TL's daily summary, upcoming owner gates (windows, eval runs).
3. Continue; end with a comment on your issue: done, next, blocked.

## 7. Codex session notes

AGENTS.md is read automatically, but there is no guard hook: never open `.env` or `var/`, never print environment variables, never read message-content rows (WORKFLOW §2). Run the capability check (WORKFLOW §3.2) in your first session; tell the owner if Docker, commit (use a separate clone if a worktree's shared `.git` is blocked), push or `gh` is missing. Never put punctuation right after a command in a shell line.
