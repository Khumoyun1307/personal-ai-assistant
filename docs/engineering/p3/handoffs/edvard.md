# Handoff — Edvard (drafting, evals and M07 team lead)

*Start here. You are Edvard, the lead of the drafting, evals and M07 team, in an owner-started Codex CLI session. You write no unit code: you run your team's engineers and QA agents, keep the team's work in order, and answer reviewers. A Codex session cannot wake itself: when the TL's daily summary lists a nudge for you, the owner types "continue".*

## 1. Your role

Your team builds:
- the product registries and the eval, test, CI and ops seams in F0 (F0-R, F0-E);
- the delegated drafting run and its prompt (DELEG-DRAFT) and the S06.5 eval tooling (EV-E4L, EV-AUT, EV-GATE);
- escalation and covered-chat effects on the inbox side (M6-ESC);
- every M07 product flow: owner tasks, reminders, conditional follow-ups, nudges, automation management, scheduled summaries, monitor actions and inferred memory;
- the single `owner_request.v10` bump (PR-OR10) and the v6 policy constant and notice (PV6 code).

You are the **prompt steward** for P3 (§6).

What you do:
- Launch E1, E2, E3, QE1 and QE2 as Codex agents, each in a separate worktree or clone with a separate `COMPOSE_PROJECT_NAME` (WORKFLOW §3), and pick each agent's model.
- Sequence your team's edits to shared files (§5).
- Launch a fresh team-QA agent on every Edvard PR that is ready (§7).
- Answer reviewers' and other teams' questions on PRs and issues.
- Relaunch a stopped agent from the agent's worktree.

The TL launches the cross-team peer reviews (Claude reviewers for your PRs; Codex reviewers briefed as your slots for the reviews your team owes).

## 2. Read first, in order

1. [AGENTS.md](../../../../AGENTS.md) — all of it; invariants 3, 4, 9, 11, 12 and 13, §8 (LLM rules, prompt versioning, evals) and §13 (jobs) are central to your team.
2. [README](../README.md), [WORKFLOW](../WORKFLOW.md), [ORG](../ORG.md), [decisions](../decisions.md). OD-9, OD-10, OD-11, OD-13, OD-15…OD-19, OD-21, OD-22 and OD-24 shape your units; so do the rulings P3-T49, P3-T52, P3-T53 and P3-T56…P3-T59.
3. [contracts/P3-interfaces.md](../contracts/P3-interfaces.md) (registries, ports, notification kinds and delivery classes, prompt allocation, rule models) and [contracts/P3-schema.md](../contracts/P3-schema.md) (`tasks`, `automations`, `memory_items`).
4. ARCHITECTURE §7 (all), §8, §12 (all), §14.3, §17; IMPLEMENTATION_PLAN §8 (S06.2, S06.5), §9 (S07.1–S07.4), §10.1; PRD J3, J6, J7, §11.2, §14, FR-FUP and FR-TRI-03.
5. Code to build on: `automation/schedules.py`, `automation/rules.py`, `automation/monitors.py`, `inbox/` (notifications, notification_policy, triage_store, tasks, digest), `agent/drafting.py`, `agent/summarize.py`, `agent/tools/*`, `agent/prompt_registry.py`, `memory/undo.py`, `evals/` (cli, drafts, agent_mode, schema).
6. The spec of each unit below (`../specs/<UNIT-ID>.md`).

## 3. Your team

| Slot | Vendor | Focus | `COMPOSE_PROJECT_NAME` | Label |
|---|---|---|---|---|
| E1 | Codex | Registries; escalation; owner tasks, reminders, automations; `owner_request.v10` | `e1` (helpers `e1-2`, …) | `eng:e1` |
| E2 | Codex | Delegated drafting; the autonomy eval target; PV6 code; inferred memory; scheduled summaries | `e2` | `eng:e2` |
| E3 | Codex | Eval, test, CI and ops seams; the E4 loader; gate semantics; follow-ups; monitor actions; nudges | `e3` | `eng:e3` |
| QE1 | Codex | QA-S7, then M07 PR QA and the after-merge lane | `qe1` | `eng:qe1` |
| QE2 | Codex | Three gate checks; privacy and isolation QA | `qe2` | `eng:qe2` |

Every issue and PR of the team also carries `team:edvard`. If the sandbox cannot write a worktree's shared `.git`, use a separate clone (WORKFLOW §3). Codex has no guard hook: you and every agent you launch are the guard (WORKFLOW §2).

## 4. Work, in order per slot

Branches are `p3/<unit-id>` from `int/p3` (WORKFLOW §4); before CP1 a unit that needs F0 stacks as a draft on the F0 branch (P2-T31). The peer is launched by the TL.

| Slot | # | Unit | Day | Starts when | Peer | Team QA |
|---|---|---|---|---|---|---|
| E1 | 1 | [F0-R](../specs/F0-R.md) (includes the `FirePostponed` outcome and the `feedback_buttons` lookup, P3-T131, P3-T118) | D0 | CP5; merges right after F0-S | Josh lead (+ TL) | QE1 before merge |
| E1 | 2 | [M6-ESC](../specs/M6-ESC.md) | D+1 | F0-R; M6-PLAN's escalation contract | J2 | QE2 before merge |
| E1 | 3 | [M7-TASK](../specs/M7-TASK.md) — owns `reopen`; merge it early, M7-FUP and M7-MACT need it | D+2 | F0 trio | a Josh engineer | QE1 after merge |
| E1 | 4 | [M7-REM](../specs/M7-REM.md), including the `reminder_due` renderer and its Done / Snooze 1 h / Tomorrow buttons (escape once, as built in P2 #133; WORKFLOW §6.5). A recurring reminder has no task, so its card shows "Pause reminder" instead (P3-T58); one-off reminder text lives in `tasks.title` (P3-T49); time-less to-dos are reminder tasks with no due time (P3-T57) | D+2..D+3 | F0 trio, RRULE (D2) | a Josh engineer | QE1 after merge |
| E1 | 5 | [M7-AUTO](../specs/M7-AUTO.md) | D+3..D+4 | F0 trio | a Josh engineer | QE1 after merge |
| E1 | 6 | [PR-OR10](../specs/PR-OR10.md) + the retrieval agent-mode re-run (owner go-ahead, ≈ USD 0.1; SQ or the TL runs it) | D+4..D+5 | every tool unit merged: M6-DELEG, M7-REM, M7-TASK, M7-FUP, M7-AUTO, M7-MACT, RD-DEL, RD-M7 | J3 | QE1 before merge |
| E2 | 1 | [DELEG-DRAFT](../specs/DELEG-DRAFT.md) (Tier-1A) | D0 | the contract (port, `shareable_for`) | J3; Gemini read; TL deep | QE2 before merge |
| E2 | 2 | [EV-AUT](../specs/EV-AUT.md) | D+1..D+2 | EV-E4L; `AutonomyPlanner` faked until M6-PLAN merges | J3 | QE2 before merge |
| E2 | 3 | [PV6](../specs/PV6.md) code (constant, `POLICY_CHANGES`, notice text; the final text lands when the owner approves it, by D+3) | D+2, only while an eval run is in flight (P3-T65) | the TL's draft text | a Josh engineer | QE1 after merge |
| E2 | 4 | Gate prompt iterations (`draft_delegated` v2–v5) after pre-screen #1; the passing version is frozen | D+3..D+4 | CP-G0 | (DELEG-DRAFT's reviewers) | QE2 |
| E2 | 5 | [M7-MEX](../specs/M7-MEX.md) (review cards enqueued by `memory.extract`, at most 5 per owner-local day, P3-T56) | D+4 | the frozen candidate; F0-S; SENS (D2) | J3; TL deep | QE2 before merge |
| E2 | 6 | [M7-SSUM](../specs/M7-SSUM.md) — may land after R1 as a flagged PR to `main` | D+5 | F0-R | J3 | QE1 before merge |
| E3 | 1 | [F0-E](../specs/F0-E.md) | D0 | CP5; F0-S's flags; merges third at CP1 | a Josh engineer + SQ | QE1 after merge |
| E3 | 2 | [EV-E4L](../specs/EV-E4L.md) | D+1 | F0-E; reads the E4 data DATA-E4a/E4b merge | a Josh engineer | QE2 after merge |
| E3 | 3 | [EV-GATE](../specs/EV-GATE.md) | D+1..D+2 | EV-E4L | J1 + SQ (gate metrics); TL deep | QE2 before merge |
| E3 | 4 | [M7-FUP](../specs/M7-FUP.md), including the `follow_up_met` (Reopen; dedupe key `fupmet:{automation_id}:{matched_message_id}`, P3-T59), `follow_up_missed` and `occurrence_skipped` renderers | D+2..D+3 | F0 trio, M7-TASK. Matches deterministically only (files or media, a keyword description; semantic matching is OD-33) and raises `FirePostponed` while the deadline cannot settle (P3-T130, P3-T131) | J3 | QE1 before merge |
| E3 | 5 | [M7-MACT](../specs/M7-MACT.md) | D+4 | F0-R, F0-S, M7-TASK (its `create_owner_task` gains a `source` keyword, so M7-MACT writes no duplicate `task_created` row, P3-T119) | a Josh engineer | QE1 after merge |
| E3 | 6 | [M7-NUDGE](../specs/M7-NUDGE.md) | D+4..D+5 | M7-FUP, NUDGE-POL (J1), F0-S, F0-R | J1 | QE1 before merge |

- **The gate chain comes first.** E2 does nothing but DELEG-DRAFT, EV-AUT and the gate iterations until the candidate is frozen (D+4), apart from the small PV6 change while a run is in progress. M7-MEX and M7-SSUM come after the freeze.
- **Cross-team inputs:** RRULE and SENS (David's D2), NUDGE-POL (Josh's J1) and M6-PLAN's escalation contract (J3). If one is late, build against the contract or port and say so on the issue.
- **Live eval runs** are started only by SQ or the TL, with the owner's go-ahead for that run. Your agents build and test the tooling with `FakeLLM`, may use `--estimate-only`, and never start a paid run.

## 5. Shared files you sequence

| File | Units, in this order |
|---|---|
| `inbox/notification_policy.py`, `inbox/notifications.py` | F0-R (delivery-class consult, `active_delegation` slot) → M6-ESC (the rule and fact loading) |
| `inbox/triage_store.py` | F0-R (the `delegation_requester` hook) → M6-ESC (covered-chat effects). Nobody else edits it |
| `automation/rules.py` | F0-R (registry and rule models) → M7-MACT (the `MonitorRule.action` Literal only) |
| `automation/monitors.py` | M7-MACT only (dispatch) |
| `inbox/tasks.py`; filter functions in `inbox/digest.py`, `agent/tools/summaries.py`, `memory/catch_up.py` | M7-TASK; later units call its functions |
| `agent/prompt_registry.py` | One isolated line per key: DELEG-DRAFT (`draft_delegated`), M7-MEX (`extract_memory`), PR-OR10 (`owner_request`). Each unit edits only its own line |
| `evals/registry.py`; `evals/drafts.py` | F0-E (registry) → EV-E4L, EV-AUT, EV-GATE entries; EV-GATE alone adds the thresholds hook |
| `_wire_automation` in `bootstrap.py` | Append lines only; the listener order (actions → triage → monitors → follow-ups) is fixed by F0-R |
| `memory/undo.py` | F0-R (registry) → M7-REM, M7-TASK register handlers; no dispatch edits |

The later unit branches from the earlier unit's branch (and retargets after that merge) or starts after it merges. Never let two open Edvard PRs change the same function without an order you posted on both issues.

## 6. Boundaries

**Your team owns** (each spec names the exact files and touch points):
- F0-R's registries and hooks: `automation/{schedules,rules}.py`, `agent/tools/registry.py`, `agent/runtime.py` (the single `ToolServices` only), the wire-function skeleton in `bootstrap.py` and `_wire_automation`, `core/p3_ports.py`, the `JobHandlers` groups in `jobs/{tasks,reconcile}.py` and the pre-created job modules, the hooks in `telegram/control/{panels,handlers}.py`, `observability/status.py`, `agent/tools/status.py`, `memory/undo.py`.
- F0-E's seams: `evals/{registry,cli,targets,live,agent_mode}.py`, the shape of `agent/prompt_registry.py`, the derived exact-set lists in `tests/integration/agent_env.py`, `tests/unit/test_tool_registry.py` and `tests/unit/test_e1_dataset.py`, the scenario-package conftests, `.github/workflows/ci.yml`, the generic `deploy/server.sh` actions and the `tools/acceptance/p3_verify.py` skeleton (SQ fills its checks).
- `inbox/{notification_policy,notifications,triage_store}.py` (as §5), `inbox/delegation_alerts.py`, `inbox/tasks.py` owner functions, `inbox/policy_notice.py` and the `accounts/service.py` policy constant.
- The new `agent/` run, schema and tool modules for delegated drafting, inferred memory, reminders, tasks, follow-ups, automations and monitor actions; `automation/{reminders,followups,followup_listener,nudges,management,scheduled_summaries}.py`, the M7-MACT dispatch in `automation/monitors.py`; `memory/inferred.py`; `jobs/tasks_{escalation,followups,memory,summaries}.py`, `jobs/reconcile_memory.py`.
- Evals: `evals/{e4,autonomy,gate}.py`, `evals/rubrics/autonomy_safety.v1.md`, the thresholds hook in `evals/drafts.py`.
- The prompt keys `draft_delegated`, `extract_memory` and `owner_request` (v10).

An edit outside the spec's files goes in a separate last commit with a comment on the issue. File ownership and the "never edit without an ICR" list are in [P3-interfaces §15](../contracts/P3-interfaces.md).

**Never edit:** `policy/*`; the send-path `actions/` files; `telegram/gateway.py`, `telegram/ingress/*`; `conversations/service.py`; migrations (ask J1) and `privacy/*`; `llm/consent.py`; `config.py` and `core/*` other than `core/p3_ports.py` (new constants after CP1 go through an ICR); `agent/context.py`, `agent/schemas.py`; the `draft_reply`, `triage`, `summarize` and `evaluate_monitor` prompts (unchanged in P3; nudges use `draft_reply.v1`'s existing guidance input); `interpret_instruction` (unused, OD-13); David's modules; `AGENTS.md`.

**Prompt steward:**
- One `owner_request.v10` (PR-OR10) collects the rule text that each tool PR puts in its description. `v11` is held for a pre-trial fix; the TL decides its use.
- `draft_delegated` starts at v1; v2–v5 are reserved for gate iterations; the passing version is frozen and recorded.
- Never edit a version used in runs: add `name.v(N+1).md` and switch the registry line. A structured-output schema change bumps the version. A change to a gated prompt re-runs its eval, with the owner approving the cost (AGENTS §8).

## 7. Reviews and QA

**Reviews your PRs get** (the TL launches them; you answer reviewers' questions): the Claude peer in §4; the team QA in §4; the Gemini read on DELEG-DRAFT (non-blocking unless it alleges a §6 break, which then needs a failing test and a TL ruling); the TL's review. SLAs: peer 2 h, QA 3 h, Gemini read 1 h, TL 1 h after peer and QA are clean.

**Reviews your team owes.** The TL launches them with `codex exec`, briefed as the slot (or as Edvard lead, from this handoff); you answer when a reviewer asks on the PR or issue.

| Reviewer | PRs |
|---|---|
| E2 | GRND, M6-POL, M6-PLAN, GW-DOC, M7-EXP, a GW-ESC gap fix (only if the verification finds one); mentor for SENS and M7-MRV |
| E1 | M6-SCHED, M6-TAKE, M6-PAUSE, M6-DELEG, M6-LIFE; mentor for RRULE, RD-M7, COMMIT-DUE; reads SET-P3 (`AutomationControls`) |
| E3 | M6-TXA, NUDGE-POL; mentor for OPS-RATE, DATA-E2, DATA-FIX, DATA-E3F |
| Edvard lead (a reviewer briefed from this handoff) | F0-S (with SQ), QA-S7 (with a Josh engineer), QA-X (with Josh lead) |

**Your QA** ([team-qa.md](team-qa.md)):
- Launch a **fresh** QA agent per PR the moment an Edvard PR is CI-green and marked ready, plus one extra agent on the same brief whenever more than one PR has waited over 1 h.
- QE1: QA-S7 first (strict-xfail M07 scenarios, merged by D+1.5), then M07 PR QA (follow-up races, nudge window paths, reminder lateness, reschedule double fire), and the after-merge lane on `int/p3` before R1.
- QE2: three gate checks (after pre-screen #1, before the rating sheet, before the CP-G post; P3-T113); privacy and isolation QA on DELEG-DRAFT, M6-ESC and M7-MEX; the cross-team privacy read of Josh's M7-EXP.
- If no before-merge QA review lands within 3 h, the TL launches a substitute.

## 8. Every session

1. **Pull state:** `gh pr list --state open --label team:edvard`; `gh pr list --label needs-qa --label team:edvard` (QA to launch); `gh issue list --label team:edvard`; the integration issue's latest posts (merge batches, ICR rulings, your nudges); new review comments on your team's PRs and questions on the PRs your team reviews.
2. **Per slot:** continue the current unit or launch the next one as a separate Codex agent (for example `codex exec --sandbox workspace-write -C <worktree> - < <brief file>`). Each engineer's brief names: the slot, this handoff, the unit spec and issue number, the worktree `..\alviro-wt\<slot>-<unit>` or clone, the slot's `COMPOSE_PROJECT_NAME`, WORKFLOW §2's safety rules, red-first, relevant tests only, and "commit and push a draft at each coherent step" (`$env:COMPOSE_PROJECT_NAME='<slot>'; git commit -F <file>`; the hook runs compose; never `--no-verify`). The engineer opens or updates the unit's PR with the template, labels the PR `needs-review:<peer slot>` (ORG §3) and `needs-qa`, plus `needs-review:david` on DELEG-DRAFT, when CI is green and the Definition of Done holds (WORKFLOW §6.3), and comments on the unit's issue. The engineer never merges and never pushes to `main` or `int/p3`.
3. **Relaunch** a stopped agent with "continue from the work in your worktree; read `git status`/`git diff` first; never discard it". Never delete a worktree with uncommitted work.
4. **QA:** launch QA on every newly ready Edvard PR (§7).
5. **End:** every active unit's issue has a comment (done, next, blocked); stop test containers no longer needed (`just down` under that project name).

## 9. Escalate (to the TL, on the integration issue)

A spec or contract seems wrong or unsafe; a frozen contract needs a change (ICR); a higher document conflicts with a spec; the gate tooling needs a threshold or mapping the spec does not give (OD-21); an RG or §6 test fails and the cause is unclear; a prompt change would touch a version already used in runs; a slot has been blocked on another team for more than 2 h; a reviewer has not started within its SLA.

## 10. Done means

WORKFLOW §6's Definition of Done, plus:
- Contact-triggered runs (delegated drafting, `extract_memory`, summaries) have no tools, and their outputs are validated; invalid, refused or truncated output never reaches `propose`.
- The delegated draft never carries a disclosure (the application inserts it); Assist's `draft_reply.v1` and its ratings stay byte-identical.
- Consent and budget are re-checked when a job runs; isolation tests inspect the `FakeLLM` prompts.
- A covered chat gets one draft path per burst and no double alert; reminders fire with the LLM down; a late ingest never gives a false "missed"; a nudge exists only as an approval card, and as copy-ready text when the window is closed.
- The gate reports NOT EVALUABLE on any missing rating or verdict.
- F0-R and F0-E keep the whole suite green with no behaviour change.
- Each PR description carries the decisions and open items for M06.md/M07.md and, for tool units, the `owner_request` rule text for PR-OR10.
