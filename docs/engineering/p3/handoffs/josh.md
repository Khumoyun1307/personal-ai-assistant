# Handoff — Josh (send-path team lead)

*Start here. You are Josh, the lead of the send-path team, in an owner-started Claude Code session. You write no unit code: you run your team's engineers and QA agents, keep the team's work in order, and answer reviewers. This page says what the team builds, in what order, what it owns and what it must never touch.*

## 1. Your role

Your team builds the part of P3 where a mistake costs the most: autonomy end to end (the autonomy policy rules, the planner, the `scheduled` state and release, Tx A for autonomous sends, takeover, the kill switch), the delegation service and lifecycle, the J7 nudge policy path, the gateway's `send_document` and `/export`. J1 is the **schema steward** for the whole phase.

What you do:
- Launch J1, J2, J3, QJ1 and QJ2 as agents in your session, each in a separate worktree with a separate `COMPOSE_PROJECT_NAME` (WORKFLOW §3), and pick each agent's model.
- Sequence your team's edits to shared files (§5).
- Launch a fresh team-QA agent on every Josh PR that is ready (§7).
- Answer reviewers' and other teams' questions on PRs and issues.
- Relaunch a stopped agent from the agent's worktree.

The TL launches the cross-team peer reviews (Codex reviewers for your PRs; Claude subagents briefed as your slots for the reviews your team owes). David's session gives the Gemini reads on your Tier-1A PRs. Your session may run its own self-paced loop.

## 2. Read first, in order

1. [AGENTS.md](../../../../AGENTS.md) — all of it; §6 invariants 1, 2, 3, 5, 6, 7, 8, 10 and 13, and §7, §9, §12, §13 are yours to protect.
2. [README](../README.md), [WORKFLOW](../WORKFLOW.md), [ORG](../ORG.md), [decisions](../decisions.md). OD-4, OD-5, OD-6, OD-7, OD-8, OD-10, OD-11, OD-12, OD-14, OD-18, OD-20, OD-31 (`/pause` and approvals) and OD-32 (when the AI line reappears) shape your units; so do the rulings P3-T47, P3-T48, P3-T51, P3-T54 and P3-T55.
3. [contracts/P3-schema.md](../contracts/P3-schema.md) and [contracts/P3-interfaces.md](../contracts/P3-interfaces.md); F0-S implements most of both.
4. ARCHITECTURE §6.5–§6.8, §8.1–§8.3, §9 (all), §10, §12, §13, §15.6; IMPLEMENTATION_PLAN §8 (S06.1–S06.4), §9 S07.4, §10; PRD J5, J7, J8–J10, §11.4, §12 and the FR-AUT requirements; TELEGRAM_FEASIBILITY §4, §12, §13.
5. Code to build on: `actions/` (proposals, lifecycle, states, executor, policy_loader, autonomy, ingestion), `policy/`, `conversations/service.py`, `telegram/gateway.py`, `telegram/ingress/`, `telegram/control/`, `privacy/`, `tests/integration/business_env.py`.
6. The spec of each unit below (`../specs/<UNIT-ID>.md`).

## 3. Your team

| Slot | Vendor | Focus | `COMPOSE_PROJECT_NAME` | Label |
|---|---|---|---|---|
| J1 | Claude | Schema steward; F0-S schema and privacy; `scheduled`, Tx A, lifecycle, nudge policy | `j1` (helpers `j1-2`, …) | `eng:j1` |
| J2 | Claude | F0-S contracts and seams; grounding; takeover; kill switch; `send_document` | `j2` | `eng:j2` |
| J3 | Claude | Autonomy rules; planner; delegations; `/export`; a GW-ESC gap fix only if the TL's verification finds one | `j3` | `eng:j3` |
| QJ1 | Claude | QA-S6, race reproductions, then PR QA | `qj1` | `eng:qj1` |
| QJ2 | Claude | PR QA from D0 | `qj2` | `eng:qj2` |

Every issue and PR of the team also carries `team:josh`.

## 4. Work, in order per slot

Branches are `p3/<unit-id>` from `int/p3` (WORKFLOW §4). Before CP1, a unit that needs F0 stacks as a draft on the F0 branch and retargets after the merge (P2-T31). The peer is launched by the TL; **Tier-1A** units also get QA before merge with a mutation table, a Gemini read and a TL deep review.

| Slot | # | Unit | Day | Starts when | Peer |
|---|---|---|---|---|---|
| J1 | 1 | [F0-S](../specs/F0-S.md) parts (a)–(d): migration `0015_p3`, ORM and CHECK mirrors, privacy, their tests | D0 | CP5 | Edvard lead + SQ (Tier-1A) |
| J1 | 2 | [M6-SCHED](../specs/M6-SCHED.md) | D+1 | F0-S | E1 (Tier-1A) |
| J1 | 3 | [M6-TXA](../specs/M6-TXA.md) | D+2 | M6-SCHED, M6-POL, M6-PLAN, F0-R (executor seam, ICR-P3-1) | E3 (Tier-1A) |
| J1 | 4 | [M6-LIFE](../specs/M6-LIFE.md) | D+3 | M6-DELEG's `end` service (may build on the port) | E1 |
| J1 | 5 | [NUDGE-POL](../specs/NUDGE-POL.md), then CP-A fixes | D+4 | F0-S | E3 |
| J1 | 6 | R1 fixes | D+5 | — | — |
| J2 | 1 | [F0-S](../specs/F0-S.md) parts (e)–(k): pure contracts, ports, constants, settings, disclosure module, read seams, poller hook, fakes | D0 | CP5 | (same PR as J1) |
| J2 | 2 | [GRND](../specs/GRND.md) | D+1 | the contract's fail-closed stub | E2 (Tier-1A) |
| J2 | 3 | [M6-TAKE](../specs/M6-TAKE.md) | D+2 | F0-S, M6-SCHED | E1 (Tier-1A) |
| J2 | 4 | [M6-PAUSE](../specs/M6-PAUSE.md) | D+3 | F0-S, F0-R, M6-SCHED | E1 (Tier-1A) |
| J2 | 5 | [GW-DOC](../specs/GW-DOC.md), then CP-A fixes | D+4 | F0-S | E2 |
| J2 | 6 | R1 fixes | D+5 | — | — |
| J3 | 0 | [GW-ESC](../specs/GW-ESC.md) gap fix — **not a build**: the fix landed on `main` in P2 #133, and the TL verifies its coverage before D0 (P3-T45). Only if that check finds a gap does J3 fix it, as the first PR into `int/p3`; no P3 renderer merges before that fix | D0, only if needed | the TL posts a gap on the integration issue | E2 |
| J3 | 1 | [M6-POL](../specs/M6-POL.md) | D0 | the contract (merges after F0-S) | E2 (Tier-1A) |
| J3 | 2 | [M6-PLAN](../specs/M6-PLAN.md) | D+1..D+2 | F0 trio, M6-POL, M6-SCHED (owns `actions/release.py::schedule_autonomous`; stack on it until it merges, P3-T55); DELEG-DRAFT faked and GRND stubbed until merged. Fills F0-S's fact-builder stubs in `actions/policy_loader.py` and adds one line in `telegram/control/approvals.py`; never edits `release.py::_decide` (P3-T115, P3-T116) | E2 (Tier-1A) |
| J3 | 3 | [M6-DELEG](../specs/M6-DELEG.md) | D+2..D+3 | F0-S, F0-R | E1 |
| J3 | 4 | [M7-EXP](../specs/M7-EXP.md) | D+4..D+5 | GW-DOC, F0-S | E2, plus a QE2 privacy read; TL deep |

- **F0-S has two authors in one PR.** J1 and J2 push to the same branch `p3/f0-s`, each from a separate worktree. The two parts touch disjoint files; J1 and J2 each run `git pull --rebase` before every push; J1 opens the PR. It merges first at CP1 with a merge commit, never squashed.
- **F0-S scope grew (P3-T53, P3-T54):** it also owns `actions/config.py` (`AutonomyConfig`); the fail-closed stubs `actions/grounding.py` and `actions/language_keys.py` (GRND fills them); the fail-closed fact-builder stubs `delegation_snapshot`, `rate_counters` and `autonomy_facts` (M6-PLAN fills them, P3-T115); `delegations_repo.list_current` (P3-T125); the end primitives (`automation/delegations_repo.py::end_delegations`, and `cancel_pending_for_delegations` with the delegation filter on `_change_pending`); one restore-notice line in `inbox/backup_check.py`; `db/models.py`; `migrations/env.py`; `tests/integration/test_p3_end_primitives.py`; and the optional setting `LLM_ROUTE_DRAFT_DELEGATED_MODEL`. It adds only the five new `StateReason` values and the two new `NOT_RUN_STATUSES`. The spec splits this work between J1 and J2.
- **Critical path first:** M6-POL → M6-PLAN (J3) and M6-SCHED → M6-TXA (J1) come before everything else. M6-SCHED also feeds M6-PLAN (`schedule_autonomous`), so J1 pushes its draft early. NUDGE-POL must merge in time for E3's M7-NUDGE (D+4).
- A unit not merged by R1 (most likely M7-EXP) goes to `main` after R1 as a flagged PR, in time for deploy #2.

## 5. Shared files you sequence

| File | Units, in this order |
|---|---|
| `actions/policy_loader.py` | M6-PLAN (fills the autonomy fact builders, P3-T115) → M6-TXA (execution facts) → M6-TAKE (`autonomy_not_paused` input) → NUDGE-POL (`no_takeover_since` reference) |
| `actions/proposals.py` | F0-S (signature only) → M6-PLAN (`delegation_auto` path) → NUDGE-POL (trigger path) |
| `actions/lifecycle.py` | F0-S (event table, `cancel_pending_for_delegations`) → M6-SCHED (bulk helpers) |
| `actions/release.py` | M6-SCHED (owns `schedule_autonomous` and `release_scheduled`) → M6-PLAN calls `schedule_autonomous` and stacks on M6-SCHED (P3-T55) |
| `actions/autonomy.py` | M6-TAKE (`resume_here`) → M6-PAUSE (pause-source enum) |
| `telegram/gateway.py` | GW-DOC (a GW-ESC gap fix goes first, only if the verification finds one; GW-DOC also adds the one-line adapter change if `Delivery.error` lacks the forbidden code, and M6-PAUSE checks first, P3-T136) |
| `jobs/reconcile_delegation.py` | M6-PLAN (pending part) → M6-LIFE (overdue part) |
| `_wire_autonomy` in `bootstrap.py` | M6-PLAN, M6-DELEG, M6-LIFE, M7-EXP: append lines only; keep both appends on a conflict |
| `actions/executor.py`, `actions/ingestion.py`, `conversations/service.py` | One unit each: M6-TXA, M6-SCHED, M6-TAKE |

The later unit branches from the earlier unit's branch (and retargets after that merge) or starts after it merges. Never let two open Josh PRs change the same function without an order you posted on both issues.

## 6. Boundaries

**Your team owns** (each spec names the exact files and touch points):
- `policy/*` and the send-path `actions/` files (`executor`, `lifecycle`, `states`, `proposals`, `ingestion`, `policy_loader`, `autonomy`), plus the new `actions/{ports,disclosure,config,autonomy_planner,release}.py` and the grounding and language-key modules (`actions/{grounding,language_keys}.py`).
- `telegram/gateway.py`, `telegram/ingress/*`, `conversations/service.py`, `core/channel.py` (the `send_document` port).
- Migrations (J1 is steward) and `privacy/*`, including the P3 cascade and `privacy/export.py`.
- `automation/{delegation_models,delegation_rules,delegations_repo,delegations,delegation_lifecycle}.py`, `memory/shareable.py`, `agent/delegation_schemas.py`, `agent/tools/delegations.py`.
- F0-S's shared constants: `core/{jobs,flags,autonomy_texts}.py`, `config.py`, `.env.example`, `inbox/notification_types.py`, the `agent/untrusted.py` sources and the `agent/tools/executor.py` status set.
- `telegram/control/{takeover,inbox_handlers,pause_button,export}.py` (`pause_button.py` holds only M6-PAUSE's `pz` press handler; every renderer uses `core/autonomy_texts.py::pause_button()`, P3-T135), `telegram/ingress/fast_lane.py`, `jobs/tasks_{delegation,release,export}.py`, your parts of `jobs/reconcile_{delegation,actions}.py`, and `_wire_autonomy`.

Your team is the named owner of the "never edit without an ICR" entries ([P3-interfaces §15](../contracts/P3-interfaces.md)) for `policy/`, the send-path `actions/` files, the gateway, ingress, `conversations/service.py`, migrations, `privacy/` and `llm/consent.py`. Even so, an edit that no spec names needs an ICR. An edit outside the spec's files goes in a separate last commit with a comment on the issue.

**Never edit:** `agent/runtime.py`, `agent/context.py`, `agent/schemas.py`; any prompt file (Edvard owns `draft_delegated`, `extract_memory` and `owner_request.v10`); `inbox/notification_policy.py`, `inbox/notifications.py`, `inbox/triage_store.py` (Edvard's; M6-PLAN only supplies the requester through the F0-R hook); `automation/schedules.py`, `automation/rules.py` (F0-R's registries); David's modules; `AGENTS.md`; ARCHITECTURE or IMPLEMENTATION_PLAN outside the sections your specs name.

**Schema steward (J1):** after CP1, anyone needing a migration asks on the integration issue; J1 replies with the next revision number (`0016`, …) and `down_revision` and records it there. Keep exactly one head.

## 7. Reviews and QA

**Reviews your PRs get** (the TL launches them; you answer reviewers' questions): the Codex peer in §4; QJ1 or QJ2 before merge on every Josh unit; the Gemini read on Tier-1A (non-blocking unless it alleges a §6 break, which then needs a failing test and a TL ruling); the TL's review. SLAs: peer 2 h, QA 3 h, Gemini read 1 h, TL 1 h after peer and QA are clean.

**Reviews your team owes.** The TL launches them as Claude subagents briefed as the slot (or as Josh lead, from this handoff); you answer when a reviewer asks on the PR or issue.

| Reviewer | PRs |
|---|---|
| J3 | DELEG-DRAFT, EV-AUT, M7-FUP, M7-SSUM, M7-MEX, PR-OR10; mentor for RD-DEL, SET-P3, DATA-E4a (the first 10 records before the rest are written), DATA-E4b |
| J1 | EV-GATE (with SQ), M7-NUDGE, OPS-TV3, OPS-VIEWS; mentor for M6-CATCH and STAT-ACT |
| J2 | M6-ESC; mentor for OPS-AV (mandatory) |
| Any Josh engineer | F0-E (with SQ), M7-REM, M7-TASK, M7-AUTO, M7-MACT, PV6 code, QA-S7 (with Edvard lead) |
| Josh lead (a subagent briefed from this handoff) | F0-R, QA-S6 (with SQ), QA-X (with Edvard lead) |

**Your QA** ([team-qa.md](team-qa.md)):
- Launch a **fresh** QA agent per PR the moment a Josh PR is CI-green and marked ready, plus one extra agent on the same brief whenever more than one PR has waited over 1 h.
- QJ1: QA-S6 first (strict-xfail M06 scenarios, merged by D+1.5), then deterministic race reproductions on real Postgres (pause vs release, takeover inside the 45 s delay, two claims against a cap, the disclosure re-check under uncertainty), then PR QA. QJ2: PR QA from D0. From D+1.5 both do PR QA.
- On Tier-1A, QA applies each row of the PR's mutation table; a surviving mutation is blocking.
- If no QA review lands within 3 h, the TL launches a substitute.

## 8. Every session

1. **Pull state:** `gh pr list --state open --label team:josh`; `gh pr list --label needs-qa --label team:josh` (QA to launch); `gh issue list --label team:josh`; the integration issue's latest posts (merge batches, ICR rulings, your nudges); new review comments on your team's PRs and questions on the PRs your team reviews.
2. **Per slot:** continue the current unit or launch the next one. Each engineer's brief names: the slot, this handoff, the unit spec and issue number, the worktree `..\alviro-wt\<slot>-<unit>` (create it from the right base if missing), the slot's `COMPOSE_PROJECT_NAME`, WORKFLOW §2's safety rules, red-first, relevant tests only, and "commit and push a draft at each coherent step" (`$env:COMPOSE_PROJECT_NAME='<slot>'; git commit -F <file>`; the hook runs compose). The engineer opens or updates the unit's PR with the template, labels the PR `needs-review:<peer slot>` (ORG §3) and `needs-qa`, plus `needs-review:david` on Tier-1A, when CI is green and the Definition of Done holds (WORKFLOW §6.3), and comments on the unit's issue. The engineer never merges and never pushes to `main` or `int/p3`, and may run subagents under WORKFLOW §2.
3. **Relaunch** a stopped agent with "continue from the work in your worktree; read `git status`/`git diff` first; never discard it". Never delete a worktree with uncommitted work.
4. **QA:** launch QA on every newly ready Josh PR (§7).
5. **End:** every active unit's issue has a comment (done, next, blocked); stop test containers no longer needed (`just down` under that project name).

## 9. Escalate (to the TL, on the integration issue)

A spec or contract seems wrong or unsafe; a frozen contract needs a change (ICR); a higher document conflicts with a spec; an RG or §6 test fails and the cause is unclear; anything would need the production bot or server; a slot has been blocked on another team for more than 2 h; a reviewer has not started within its SLA.

## 10. Done means

WORKFLOW §6's Definition of Done, plus:
- No business send path exists outside `actions.execute`; every autonomy rule re-runs at claim under the delegation lock, and the caps are counted there.
- `draft_safe` is never the sole basis for ALLOW_AUTO; a reached cap gives an approval card, never DENY or silence.
- The disclosure is inserted by the application, never by the model, and never on an approved draft (the CHECK holds); an uncertain send counts as not disclosed and is never retried. A `disclosure_missing` cancel re-proposes once with no model call, then becomes a card (P3-T47).
- A superseding delegation cancels the older one's pending autonomous actions in the shared chats in the same transaction (P3-T48).
- Takeover, `/pause`, a rights change, a disabled connection and a consent revoke cancel `scheduled` and autonomous `approved` actions.
- Race tests use explicit barriers, not timing; RG-13 and RG-14 exist and pass; there is exactly one Alembic head.
- Each PR description carries the decisions and open items for M06.md/M07.md and, on Tier-1A, the mutation table.
