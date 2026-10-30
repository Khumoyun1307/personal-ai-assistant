# Phase P3 — Delegated autonomy + Follow-ups (M06 ∥ M07) → Personal v1

*Engineering plan for the phase after P2. Written by the Technical Lead (TL). This folder is the **single entry point** for every lead, engineer and QA agent in P3: start here, then read your handoff in [handoffs/](handoffs/).*

**Status (2026-10-29): package merged (#139); decision group A answered; M06 and M07 approved by the owner (OD-1, [decisions.md](decisions.md) §A0); the three follow-up questions (OD-10a, OD-11a, OD-11b, §A1) were answered the same day (§A2). On 2026-10-30 the owner approved the autonomy texts (marker 🤖) and the v6 privacy text, and the LV-16 re-test moved to the TL (§A3; on Alviro in the D+6 window with the owner's go-ahead, P3-T168); the native review is v1 by D+1 and v1.1 by D+2 (P3-T166). Before D0 the owner's only item is starting the three lead sessions.** Until D0, only docs and synthetic eval data are written (the before-D0 window is open now). CP5 landed early: `int/m05` was merged into `main` on Thu 2026-10-29 (#137). The owner kept **D0 = Mon 2026-11-02**; `int/p3` is cut from `main` at D0, so the planning dates stand. Exception to any later date shift: R1 and deploy #1 cannot come before ≈ Sat 11-07, the M04 trial's last daily check and digest day 5. Two P2 inputs are already on `main`: the OPS-RT restore verification (P2 #130) and the gateway double-escape fix with the multi-chunk partial-delivery outcome (P2-T53, P2 #133), so GW-ESC is verification only (P3-T45).

---

## 1. What P3 delivers

| Milestone | Outcome | Ends when |
|---|---|---|
| **M06 Delegated autonomous communication** (IMPLEMENTATION_PLAN §8) | The owner delegates named chats for a time (or, behind its own flag, standing). Inside that scope the assistant replies on its own, with an AI disclosure line the application inserts, only after every autonomy rule passes at proposal and again at claim. Unknowns are asked about or escalated; decision requests alert the owner at once. A takeover or `/pause` stops it. An end-of-delegation catch-up reports what happened. Each reply language is enabled only after it passes the S06.5 gate | PRD §21 R2 items 1, 2 and 6; S06.5 gate passed for at least one language (CP-G); J5 passed live (CP-L); owner reviews `docs/milestones/M06.md` → `done` |
| **M07 Follow-ups, reminders & conditional automation** (IMPLEMENTATION_PLAN §9) | Owner reminders and tasks (snooze, reopen); conditional follow-ups that close themselves or remind with a nudge **approval card** (J7); automation management and scheduled summaries; monitor actions `create_task` and `digest`; inferred memory behind review cards; `/export`; the NFR-08 availability recorder; the weekly usefulness rating | PRD §21 R2 items 3 and 7; J7 passed live (CP-L); the 14-day trial passed; owner reviews `docs/milestones/M07.md` → `done` |
| **Personal v1 trial** (S07.5; PRD §21 R2 #7) | 14 days of daily use with M05, M06 and M07 in production and the flag set frozen | Zero sends to a wrong recipient, zero duplicates, zero sends outside policy; weekly usefulness rating ≥ 4/5 (two ratings); ≥ 70 % of immediate alerts not marked "not useful"; NFR-08 availability ≥ 99 %; PRD R1 #7 re-verified across every data class (CP-X) |

**Out of scope:** M08 and M09; groups; any send authorised by an automation (nudges always need an approval card, OD-18); the drafted-reply monitor action (OD-19); semantic follow-up matching through `evaluate_monitor` (OD-33 default); `interpret_instruction` (specs come from strict tool arguments plus a confirmation card, OD-13, ADR-18); a separate `delegated_reply` run type (OD-9 default); an independent audit model call, unless the owner adopts it after the E4 results (OD-29); a downloadable or HTTP export (ADR-14); `cancel_action` (reserved, not built); OPS-LV08 (still deferred); cheaper models or providers; the professional privacy and onboarding tone rewrite.

**Language (OD-14).** If the owner approves OD-14, language/script ≥ 98 % becomes a gate for each autonomy language (uz-Latn, uz-Cyrl, ru, en), reversing P2-O11 for M06 only. Mixed or unknown-language chats always get a card. Reply language, tone and style stay owner preferences passed to the model.

## 2. Team

Each lead is an **owner-started session** in the lead's own tool. Each lead runs the team's engineers (builders) and team-QA agents in that tool, every agent in a separate worktree with a separate `COMPOSE_PROJECT_NAME`. Leads write no unit code. The TL session dispatches every cross-team peer review in the reviewer's vendor and runs Senior QA (ORG §3). Slots may get personal names later (P3-T08); until then documents use slot and lead names.

| Who | Vendor and session | Role | Main work | Handoff |
|---|---|---|---|---|
| **Technical Lead (TL)** | Claude Code, always-on self-paced loop | Contracts, specs, decisions, integration merges into `int/p3`, review dispatch, runs Senior QA, CP-A sign-off, owner gates, deploys on the owner's go-ahead | F0-C (including the GW-ESC verification), PV6 text, DOCS-EXIT | [handoffs/tech-lead.md](handoffs/tech-lead.md) |
| **Josh** (lead) | Claude Code; the session can run a self-paced loop | Autonomy send chain, delegation lifecycle, gateway, ingress, `/export`; schema steward (J1). ≈ 15.25 eng-days | Runs J1–J3, QJ1, QJ2 | [handoffs/josh.md](handoffs/josh.md) |
| J1 | Claude (Josh's team) | Send-path engineer, **schema steward** | F0-S (schema, privacy), M6-SCHED, M6-TXA, M6-LIFE, NUDGE-POL | [handoffs/josh.md](handoffs/josh.md) |
| J2 | Claude (Josh's team) | Send-path engineer | F0-S (contracts, seams), GRND, M6-TAKE, M6-PAUSE, GW-DOC | [handoffs/josh.md](handoffs/josh.md) |
| J3 | Claude (Josh's team) | Autonomy pipeline engineer | M6-POL, M6-PLAN, M6-DELEG, M7-EXP (a GW-ESC gap fix only if the verification finds one) | [handoffs/josh.md](handoffs/josh.md) |
| QJ1, QJ2 | Claude (Josh's team) | Team QA | QJ1: QA-S6, then race reproductions; from D+1.5 both do PR QA for every Josh unit, with mutation tables on Tier-1A | [handoffs/team-qa.md](handoffs/team-qa.md) |
| **Edvard** (lead) | Codex CLI | Registries, delegated drafting, S06.5 tooling, inbox escalation, M07 flows. ≈ 16.75 eng-days | Runs E1–E3, QE1, QE2 | [handoffs/edvard.md](handoffs/edvard.md) |
| E1 | Codex (Edvard's team) | Registries and M07 flows | F0-R, M6-ESC, M7-TASK, M7-REM, M7-AUTO, PR-OR10 | [handoffs/edvard.md](handoffs/edvard.md) |
| E2 | Codex (Edvard's team) | Delegated drafting and gate | DELEG-DRAFT, EV-AUT, gate iterations, PV6 code, then M7-MEX, M7-SSUM | [handoffs/edvard.md](handoffs/edvard.md) |
| E3 | Codex (Edvard's team) | Eval tooling and follow-ups | F0-E, EV-E4L, EV-GATE, M7-FUP, M7-MACT, M7-NUDGE | [handoffs/edvard.md](handoffs/edvard.md) |
| QE1, QE2 | Codex (Edvard's team) | Team QA | QE1: QA-S7, then M07 PR QA. QE2: three gate-integrity checks (after pre-screen #1, before the rating sheet, before the CP-G post; P3-T113); privacy and isolation QA | [handoffs/team-qa.md](handoffs/team-qa.md) |
| **David** (lead) | Gemini in Antigravity (no Gemini CLI on the owner's machine) | Data, pure helpers, read tools, panels, ops SQL and tooling; Gemini reads on Tier-1A PRs and OPS-TV3. ≈ 9.0 eng-days in-phase + 3.5 pre-start | Runs D1, D2, QD1 | [handoffs/david.md](handoffs/david.md) |
| D1 | Gemini (David's team), junior | Junior engineer | DATA-E4a, STAT-ACT, M6-CATCH, DATA-FIX, RD-DEL, SET-P3, OPS-RATE | [handoffs/junior-engineers.md](handoffs/junior-engineers.md) |
| D2 | Gemini (David's team), junior | Junior engineer | DATA-E2, DATA-E4b, RRULE, OPS-AV, SENS, COMMIT-DUE, RD-M7, M7-MRV, OPS-RT3s, DATA-E3F | [handoffs/junior-engineers.md](handoffs/junior-engineers.md) |
| QD1 | Gemini (David's team) | Team QA | PR QA for every David PR | [handoffs/team-qa.md](handoffs/team-qa.md) |
| **Senior QA (SQ)** | Codex, run by the TL session (`codex exec`) | Cross-team QA, trial verifier, eval and live operations | QA-P3, QA-X, OPS-TV3, OPS-VIEWS, GATE-RUN, LIVE-P3; the GW-ESC check; CP-A and R1 QA sign-off | [handoffs/senior-qa.md](handoffs/senior-qa.md) |

Roles, ownership, review matrix, communication and mentoring: [ORG.md](ORG.md). Safety rules, workspace, branches, PRs, tests, integration, deploy, flags, live windows and evals: [WORKFLOW.md](WORKFLOW.md). Every agent may run subagents inside the unit's boundaries, under WORKFLOW §2.

## 3. Work-unit index

Every piece of work is a **work unit (WU)** with one owner, one spec, one branch and one PR: **59 units**. Sizes are engineer-days at P2 AI pace, one fix round included: S ≈ 0.5, S+ ≈ 0.75, M ≈ 1, M+ ≈ 1.25–1.5, L ≈ 2+.

How to read the table:
- **★ = Tier-1A.** These nine PRs get a Gemini read, a mutation table and a TL deep review (ORG §3).
- **Branch → base:** branches are `p3/<unit-id>` in lowercase, with base `int/p3` unless the row says otherwise. A unit that depends on an unmerged unit may stack on it (WORKFLOW §4).
- **QA lane:** "before" = QA before merge; "after" = QA after merge, on `int/p3` before R1 (WORKFLOW §6.3).
- **Peer:** a fresh agent in that slot's role, from another team and another vendor, dispatched by the TL (ORG §3). "A Josh engineer" = whichever of J1–J3 the TL picks.
- **Files:** each spec lists owned files and named touch points; [contracts/P3-interfaces.md](contracts/P3-interfaces.md) §15 is the per-file ownership table.

**Foundation, docs, owner (8)**

| WU | Title | Team / slot | Size | Depends on | Branch → base | Spec | Peer | QA lane |
|---|---|---|---|---|---|---|---|---|
| F0-C | P3 package: contracts, specs, decisions batch, ADR-17..21 drafts, the GW-ESC verification; part 2 = lower-document corrections, the PR template, the AI marker, disclosure lines and identity answers (owner-approved 2026-10-30), and TF §13 docs-checks (P3-T09, P3-T61, P3-T65) | TL | M | owner OK to plan (OD-1) | `docs/p3-engineering` → `main` (part 2: its own docs PR after group A) | this folder | Josh, Edvard and David leads (each lead checks the team's specs) + SQ; the owner approves the decisions | — |
| F0-S ★ | Schema `0015`, ORM and CHECK mirrors, privacy (registry, retention, cascade, post-restore), pure contracts, codes, settings (incl. `LLM_ROUTE_DRAFT_DELEGATED_MODEL`), flags, kinds, ports, `AutonomyConfig`, disclosure module, grounding and language-key stubs, fail-closed fact-builder stubs, end primitives, `list_current`, read seams, poller hook, fakes (P3-T53, P3-T54, P3-T115, P3-T125) | Josh / J1 + J2 | L | CP5, F0-C part 2 (AI marker, disclosure lines, identity answers; P3-T61) | `p3/f0-s` | [specs/F0-S.md](specs/F0-S.md) | Edvard lead + SQ | before (QJ1/QJ2) |
| F0-R | Product registries and hot-file refactors, behaviour-neutral; pre-created job modules and the eight P3 `agent/tools` modules, the `FirePostponed` outcome, the `feedback_buttons` lookup (P3-T118, P3-T131) | Edvard / E1 | M+ | CP5, F0-C; rebases on `p3/f0-s` and merges second | `p3/f0-r` | [specs/F0-R.md](specs/F0-R.md) | Josh lead | before (QE1) |
| F0-E | Eval-target registry, derived exact-set lists, prompt-registry reshape, scenario packages, CI `push: int/p3`, `server.sh` `report`/`p3-verify`, `p3_verify.py` skeleton and its four subcommand modules | Edvard / E3 | M+ | CP5, F0-S (flags); rebases on `p3/f0-s` and `p3/f0-r`, merges third | `p3/f0-e` | [specs/F0-E.md](specs/F0-E.md) | a Josh engineer + SQ | after (QE1) |
| GW-ESC | Gateway double-escape fix + multi-chunk partial-delivery outcome (P2-T53): **landed in P2 #133 — verification only.** Confirm #133 covers P2-T53 and record the escape-once rule renderers follow (P3-T45). If a gap is found, its fix is the first PR into `int/p3`. No P3 renderer merges before GW-ESC is closed | TL (F0-C) with an SQ check; a gap fix: Josh / J3 | verification ≈ 1 h (a gap fix: S) | P2 #133 on `main` | none (result posted on the `P3 integration` issue before D0); a gap fix: `p3/gw-esc` → `int/p3` | [specs/GW-ESC.md](specs/GW-ESC.md) | — (a gap fix: E2) | — (a gap fix: before, QJ) |
| PV6 | Privacy policy v6 + SEC-12 notice: TL text, `POLICY_VERSION = "v6"` code | TL (text) / Edvard E2 (code) | S | owner approval (OD-24; text and notice approved 2026-10-30) | text: TL docs PR → `main`; code: `p3/pv6` | [specs/PV6.md](specs/PV6.md) | text: leads + SQ, owner approves; code: a Josh engineer | code: after (QE1) |
| OWN-LV16 | LV-16 rate re-test at volume (`burst` through Alviro's connection in the D+6 window; the dev bot has no business connection), run by the TL with SQ's `qa/lv16-runbook.md` and the owner's go-ahead (OD-27 as answered 2026-10-30, P3-T168) | TL (runs it, records TF §12) | S | QA-P3 (the runbook); the owner's go-ahead | TL docs PR → `main` (the record) | [handoffs/tech-lead.md](handoffs/tech-lead.md) | — | — |
| DOCS-EXIT | `M06.md`, `M07.md`, trial report, eval reports, ARCHITECTURE as-built sweep | TL | S | trial end | `docs/p3-exit` → `main` | [handoffs/tech-lead.md](handoffs/tech-lead.md) | owner reviews | — |

**M06 autonomy chain, delegation lifecycle, gateway and export: Josh (12)**

| WU | Title | Team / slot | Size | Depends on | Branch → base | Spec | Peer | QA lane |
|---|---|---|---|---|---|---|---|---|
| GRND ★ | Pure `content_grounded` extractor + language-gate key mapper; over-blocking is the safe failure | Josh / J2 | M | F0-C API (P3-interfaces §3.7: frozen signatures and fail-closed stubs; F0-S creates the stub files, so before CP1 it stacks on `p3/f0-s`) | `p3/grnd` | [specs/GRND.md](specs/GRND.md) | E2 | before (QJ) |
| M6-POL ★ | Autonomy rules (pure) and outcome mapping: DENY / REQUIRE_APPROVAL / ALLOW_AUTO | Josh / J3 | M | F0-S (starts D0 on the contract) | `p3/m6-pol` | [specs/M6-POL.md](specs/M6-POL.md) | E2 | before (QJ) |
| M6-PLAN ★ | Side-effect-free `AutonomyPlanner`, `replies.delegated` job, `DelegationRequester`, `delegation_auto` propose path, identity answer; held replies and their alerts, the held-reply card sync rule (OD-11) | Josh / J3 | M+ | F0-*, M6-POL, M6-SCHED (`schedule_autonomous`; stacks on it until merged, P3-T55), DELEG-DRAFT (fake until merged), GRND (stub until merged) | `p3/m6-plan` | [specs/M6-PLAN.md](specs/M6-PLAN.md) | E2 | before (QJ) |
| M6-SCHED ★ | `scheduled` state end to end, `actions.release_scheduled`, due reconciler, bulk cancel paths | Josh / J1 | M | F0-S, F0-R, M6-POL (P3-T80; stacks on `p3/m6-pol` if not merged) | `p3/m6-sched` | [specs/M6-SCHED.md](specs/M6-SCHED.md) | E1 | before (QJ) |
| M6-TXA ★ | Tx A for autonomy: delegation lock, rules re-run at claim, disclosure re-check, late conversion, `disclosure_included` | Josh / J1 | M | M6-SCHED, M6-POL, M6-PLAN, F0-R (executor seam, ICR-P3-1) | `p3/m6-txa` | [specs/M6-TXA.md](specs/M6-TXA.md) | E3 | before (QJ) |
| M6-TAKE ★ | Takeover redesign (session-scoped `takeover_at`), "Resume here", one-time notice | Josh / J2 | M | F0-S, M6-SCHED (and M6-PLAN, M6-TXA for the one shared `actions/policy_loader.py` argument; see the spec) | `p3/m6-take` | [specs/M6-TAKE.md](specs/M6-TAKE.md) | E1 | before (QJ) |
| M6-PAUSE ★ | Kill-switch completion: Pause button, bot-block auto-pause, pause sources, latency harness | Josh / J2 | M | F0-S, F0-R, M6-SCHED (Josh lead sequences its `actions/autonomy.py` edits after M6-TAKE) | `p3/m6-pause` | [specs/M6-PAUSE.md](specs/M6-PAUSE.md) | E1 | before (QJ) |
| M6-DELEG | Delegations service and tools, standing behind its flag, disclosure wording, shareable designation | Josh / J3 | M+ | F0-S, F0-R | `p3/m6-deleg` | [specs/M6-DELEG.md](specs/M6-DELEG.md) | E1 | before (QJ) |
| M6-LIFE | Delegation lifecycle: `delegation_end` never skipped, overdue reconciler, end chain, revoke and disable | Josh / J1 | M | M6-DELEG (`end` service; needed only for its timezone test), F0-S, F0-R | `p3/m6-life` | [specs/M6-LIFE.md](specs/M6-LIFE.md) | E1 | before (QJ) |
| NUDGE-POL | J7 nudge policy path: `propose(trigger=)`, `no_takeover_since` reference, never `delegation_auto` | Josh / J1 | S | F0-S | `p3/nudge-pol` | [specs/NUDGE-POL.md](specs/NUDGE-POL.md) | E3 | before (QJ) |
| GW-DOC | `send_document`, ControlChat only | Josh / J2 | S | F0-S | `p3/gw-doc` | [specs/GW-DOC.md](specs/GW-DOC.md) | E2 | before (QJ) |
| M7-EXP | `/export`: confirm card, `privacy.export` job, per-class JSON parts | Josh / J3 | M | GW-DOC, F0-S, F0-R; soft: M6-PAUSE (`pause_after_control_forbidden`, wired once on the base) | `p3/m7-exp` | [specs/M7-EXP.md](specs/M7-EXP.md) | E2 | before (QJ) + QE2 privacy read |

**Delegated drafting, evals, escalation, M07 flows: Edvard (14)**

| WU | Title | Team / slot | Size | Depends on | Branch → base | Spec | Peer | QA lane |
|---|---|---|---|---|---|---|---|---|
| DELEG-DRAFT ★ | `draft_delegated.v1` under run type `draft_reply`, `DelegatedDraftResult`, delegated context builder; owns the gate iterations v2–v5 | Edvard / E2 | M (+0.5 iterations) | F0-S (port, `shareable_for`) | `p3/deleg-draft` | [specs/DELEG-DRAFT.md](specs/DELEG-DRAFT.md) | J3 | before (QE2) |
| M6-ESC | Escalation and covered-chat effects (single owner of `triage_store.py` effects), `system.delegation_health`, notices; the `autonomy_not_sent` alert and the Draft reply press (`drf`, OD-11) | Edvard / E1 | M+ | F0-R, M6-PLAN escalation contract (not its code), GW-ESC closed (merge condition) | `p3/m6-esc` | [specs/M6-ESC.md](specs/M6-ESC.md) | J2 | before (QE2) |
| EV-E4L | E4 schema, loader and integrity checks | Edvard / E3 | M (≈ 1 d; P3-T110) | F0-E (eval-target registry); reads the E4 data that DATA-E4a/E4b merge (on `int/p3` at D0) | `p3/ev-e4l` | [specs/EV-E4L.md](specs/EV-E4L.md) | a Josh engineer | after (QE2) |
| EV-AUT | Autonomy eval target (`python -m evals run autonomy`), side-effect-free, decoys and canaries | Edvard / E2 | M+ | EV-E4L; `AutonomyPlanner` (fake → real at M6-PLAN merge); a live run also needs DELEG-DRAFT, M6-POL, M6-SCHED, M6-PLAN and GRND merged with no fail-closed stub in use | `p3/ev-aut` | [specs/EV-AUT.md](specs/EV-AUT.md) | J3 | before (QE2) |
| EV-GATE | Gate semantics per language, NOT EVALUABLE rule, rating sheet, gate report → `AUTONOMY_LANGUAGES` | Edvard / E3 | M+ (≈ 1.25 d; P3-T110) | EV-E4L (`AutonomyDetail`); not EV-AUT | `p3/ev-gate` | [specs/EV-GATE.md](specs/EV-GATE.md) | J1 + SQ (metrics) | before (QE2) |
| M7-REM | Reminders, plus the `reminder_due` renderer and its Done / Snooze 1 h / Tomorrow buttons (P3-T02); a recurring reminder's card shows Pause reminder instead (P3-T58) | Edvard / E1 | M | F0-*, RRULE, M7-TASK (P3-T80), GW-ESC (landed, #133; verification closed first) | `p3/m7-rem` | [specs/M7-REM.md](specs/M7-REM.md) | a Josh engineer | after (QE1) |
| M7-TASK | Owner tasks: `update_task`, snooze, owner-only reopen, `TaskControls` | Edvard / E1 | S+ | F0-* | `p3/m7-task` | [specs/M7-TASK.md](specs/M7-TASK.md) | a Josh engineer | after (QE1) |
| M7-FUP | Conditional follow-ups, plus the `follow_up_met` (Reopen), `follow_up_missed` and `occurrence_skipped` renderers (P3-T02) | Edvard / E3 | M+ | F0-*, M7-TASK (`reopen`), GW-ESC (landed, #133) | `p3/m7-fup` | [specs/M7-FUP.md](specs/M7-FUP.md) | J3 | before (QE1) |
| M7-NUDGE | `followups.nudge` job: approval card if the window is open, copy-ready if closed | Edvard / E3 | M | M7-FUP, NUDGE-POL, F0-S, F0-R (merges last in the J7 chain) | `p3/m7-nudge` | [specs/M7-NUDGE.md](specs/M7-NUDGE.md) | J1 | before (QE1) |
| M7-AUTO | Automation management: create (scheduled summary), update, delete, `AutomationControls` | Edvard / E1 | S+ | F0-*, M7-TASK, M7-REM, RRULE (P3-T80; E1 builds M7-TASK → M7-REM → M7-AUTO) | `p3/m7-auto` | [specs/M7-AUTO.md](specs/M7-AUTO.md) | a Josh engineer | after (QE1) |
| M7-SSUM | Scheduled summaries: fire handler → `automation.summarize` | Edvard / E2 | S+ | F0-R | `p3/m7-ssum` (may land after R1 → `main`) | [specs/M7-SSUM.md](specs/M7-SSUM.md) | J3 | before (QE1) |
| M7-MACT | Monitor actions `create_task` and `digest`, including the dispatch in `automation/monitors.py` | Edvard / E3 | S | F0-R, F0-S, M7-TASK | `p3/m7-mact` | [specs/M7-MACT.md](specs/M7-MACT.md) | a Josh engineer | after (QE1) |
| M7-MEX | Inferred memory: `extract_memory.v1`, `memory.extract` job, `pending_review` queue, review cards capped at 5 per owner-local day (P3-T56) | Edvard / E2 | M | F0-S, F0-R, F0-E, SENS | `p3/m7-mex` | [specs/M7-MEX.md](specs/M7-MEX.md) | J3 | before (QE2) |
| PR-OR10 | `owner_request.v10`, the single P3 bump, and the retrieval agent-mode re-run | Edvard / E1 | S | all tool units merged (M6-DELEG, M7-REM, M7-TASK, M7-FUP, M7-AUTO, M7-MACT, RD-DEL, RD-M7) | `p3/pr-or10` | [specs/PR-OR10.md](specs/PR-OR10.md) | J3 | before (QE1) |

**Junior units: David (17).** Closed allow-lists, junior-grade specs, PRs of about 400 lines or fewer. The peer is the unit's mentor (ORG §6).

| WU | Title | Team / slot | Size | Depends on | Branch → base | Spec | Peer (mentor) | QA lane |
|---|---|---|---|---|---|---|---|---|
| DATA-E2 | E2 v1.1: `must_not` label fixes, +3 buffer per gate language, coverage additions, dataset-log rows | David / D2 | S+ | owner OK (OD-1) | `p3/data-e2` → `main`, merged before the CP4 sync PR is cut; otherwise retargeted to `int/p3` at D0 | [specs/DATA-E2.md](specs/DATA-E2.md) | E3 | before (QD1) |
| DATA-E4a | E4 v1.1 part a (a081–a140): in-scope delegation cases + buffer; mentor reviews the first 10 records first | David / D1 | M+ | owner OK (OD-1); the E4 v1.1 format ([specs/EV-E4L.md](specs/EV-E4L.md) §3.1–§3.4) | `p3/data-e4a` → `main` (same rule) | [specs/DATA-E4a.md](specs/DATA-E4a.md) | J3 | before (QD1) |
| DATA-E4b | E4 v1.1 part b (a141–a200): re-disclosure, knowledge scope, injection, decoys with canaries | David / D2 | M | DATA-E4a merged; owner OK (OD-1); the E4 v1.1 format | `p3/data-e4b` → `main` (same rule) | [specs/DATA-E4b.md](specs/DATA-E4b.md) | J3 | before (QD1) |
| DATA-FIX | Owner native-review corrections → E2/E4 v1.2 by D+3 | David / D1 | S | owner review (as it lands); DATA-E2, DATA-E4a and DATA-E4b on `int/p3`; EV-E4L for `python -m evals.e4` | `p3/data-fix` | [specs/DATA-FIX.md](specs/DATA-FIX.md) | E3 | after (QD1); QE2 integrity check before the sheets |
| DATA-E3F | E3 v1.1: 15 follow-up-style monitor cases (informational) | David / D2 | S | CP5 (E3 on `main`) | `p3/data-e3f` | [specs/DATA-E3F.md](specs/DATA-E3F.md) | E3 | after (QD1) |
| RRULE | Pure recurrence → RRULE mapper + owner-local echo formatter | David / D2 | S | the contract only (F0-C); its hygiene test needs F0-E's `tests/log_hygiene.py` | `p3/rrule` | [specs/RRULE.md](specs/RRULE.md) | E1 | after (QD1) |
| SENS | Pure sensitive-category filter (closed list, OD-15) + lexical second screen | David / D2 | S | F0-S (`MEMORY_CATEGORIES`, `SENSITIVE_MEMORY_CATEGORIES`; values from OD-15), F0-E (`tests/log_hygiene.py`) | `p3/sens` | [specs/SENS.md](specs/SENS.md) | E2 | before (QD1) |
| M6-CATCH | Deterministic end-of-delegation catch-up builder + renderer | David / D1 | S+ (two PRs) | F0-S, F0-R | `p3/m6-catch` | [specs/M6-CATCH.md](specs/M6-CATCH.md) | J1 | before (QD1) |
| STAT-ACT | `/status` + `get_status` P3 blocks; `/activity` delegation authorization | David / D1 | M (two PRs) | F0-S, F0-R; M6-TAKE for the `takeover_pauses` block (Part B) | `p3/stat-act` | [specs/STAT-ACT.md](specs/STAT-ACT.md) | J1 | after (QD1) |
| RD-DEL | `list_delegations` read tool (managed, taints the run) | David / D1 | S | F0-S, F0-R | `p3/rd-del` | [specs/RD-DEL.md](specs/RD-DEL.md) | J3 | after (QD1) |
| SET-P3 | `/settings` P3 screens: delegations with End, disclosure wording, automations; conversation-panel Delegate row | David / D1 | M | F0-R (hooks, ports), F0-S; M7-AUTO (listing rule, R51; part B may stack on `p3/m7-auto`); GW-ESC closed (merge condition) | `p3/set-p3` | [specs/SET-P3.md](specs/SET-P3.md) | J3 (+ E1 read for `AutomationControls`) | after (QD1) |
| RD-M7 | `list_tasks` + `list_automations` read tools (managed, taint the run) | David / D2 | M | F0-S, F0-R, F0-E (derived managed set); M7-AUTO (listing rule, R51; may stack on `p3/m7-auto`) | `p3/rd-m7` | [specs/RD-M7.md](specs/RD-M7.md) | E1 | after (QD1) |
| M7-MRV | Memory review cards: approve, edit, discard (card-only) | David / D2 | S | F0-S, F0-R; GW-ESC closed (merge condition) | `p3/m7-mrv` | [specs/M7-MRV.md](specs/M7-MRV.md) | E2 | after (QD1) |
| COMMIT-DUE | Handler for the pre-registered `system.surface_due_commitments` | David / D2 | S | F0-R (shell, wiring, delivery rule), F0-S (flag, delivery-class row) | `p3/commit-due` | [specs/COMMIT-DUE.md](specs/COMMIT-DUE.md) | E1 | after (QD1) |
| OPS-AV | NFR-08 recorder (`PollObserver`), `nfr08.sql`, `nfr07.sql`, `nfr05.sql` extension; must be in deploy #1 | David / D2 | S+ | F0-S (table, hook), F0-R (`P3Wiring.poll_observer`), F0-E (`report`) | `p3/ops-av` | [specs/OPS-AV.md](specs/OPS-AV.md) | J2 (mandatory) | before (QD1) |
| OPS-RATE | Handler for the pre-registered `system.weekly_rating` (Sunday 19:00 owner-local, ≤ 1 per ISO week, P3-T60) + usefulness card + `usefulness.sql` | David / D1 | S | F0-R shell, F0-S, F0-E; GW-ESC closed (merge condition) | `p3/ops-rate` | [specs/OPS-RATE.md](specs/OPS-RATE.md) | E3 | after (QD1) |
| OPS-RT3s | Restore-test P3 check script + the P3 section of the restore runbook | David / D2 | S | F0-S (post-restore step 2b), F0-E (`p3_verify.py` skeleton) | `p3/ops-rt3s` | [specs/OPS-RT3s.md](specs/OPS-RT3s.md) | SQ | after (QD1) |

**QA, trial verification, eval and live operations (8)**

| WU | Title | Team / slot | Size | Depends on | Branch → base | Spec | Peer | QA lane |
|---|---|---|---|---|---|---|---|---|
| QA-P3 | P3 test plan, regression matrix, live checklists (M06, M07), 14-day trial log, eval-ops runbook, `P3-nn` markers | SQ | S | — (pre-start, docs) | `p3/qa-p3` → `main` | [qa/README.md](qa/README.md) | TL + Josh and Edvard leads | — |
| QA-S6 | M06 acceptance scenarios, strict xfail first, merged by D+1.5 | Josh / QJ1 | M+ (P3-T156) | F0-E, F0-S, QA-X's first commit (the driver additions); starts D0 stacked on `p3/f0-e` | `p3/qa-s6` | [specs/QA-S6.md](specs/QA-S6.md) | SQ + Josh lead | — |
| QA-S7 | M07 acceptance scenarios, strict xfail first | Edvard / QE1 | M+ (P3-T156) | F0-E, F0-S; starts D0 stacked on `p3/f0-e` (the export scenarios also use the driver additions from QA-X) | `p3/qa-s7` | [specs/QA-S7.md](specs/QA-S7.md) | a Josh engineer + Edvard lead | — |
| QA-X | Cross-team scenarios (`tests/scenarios/p3_cross/`), skeletons by D+1.5 | SQ | M (P3-T156) | F0-E, F0-S; most scenarios turn green only as M06 and M07 units merge | `p3/qa-x` | [specs/QA-X.md](specs/QA-X.md) | Josh lead + Edvard lead | — |
| OPS-TV3 | `p3_verify trial-check`: every `delegation_auto` check fails closed | SQ | M | F0-S, F0-E skeleton; decision-record shape (contract) | `p3/ops-tv3` | [specs/OPS-TV3.md](specs/OPS-TV3.md) | J1 (+ David lead Gemini read, TL deep) | — |
| OPS-VIEWS | `p3_verify` view subcommands + content-free trial-report aggregator (moved to SQ, P3-T02) | SQ | S+ | F0-E skeleton, F0-S; `report` stacks on `p3/ops-tv3` until it merges | `p3/ops-views` | [specs/OPS-VIEWS.md](specs/OPS-VIEWS.md) | J1 | — |
| GATE-RUN | S06.5 eval operations: estimate → limit 1 → full, pre-screen D+3, final rated run D+4, sheets, ledgers, reports | SQ (+ owner, TL) | M (calendar) | EV-AUT, EV-GATE, DATA-FIX (v1.2 before the final run); for CP-G0 also DELEG-DRAFT, M6-PLAN (with M6-POL, M6-SCHED), GRND and EV-E4L merged into `int/p3`, and DATA-E2/E4a/E4b (v1.1) on `int/p3` | report PRs → `int/p3` (→ `main` after R1) | [specs/GATE-RUN.md](specs/GATE-RUN.md) | QE2 integrity check; TL | — |
| LIVE-P3 | Live operations: deploy verification, shadow window, J5, J7, export inspection, OPS-RT3 run, soak, daily trial checks, NFR-05/07/08, weekly ratings, trial log | SQ (+ owner, TL) | calendar | R1 and deploy #1; OPS-TV3 and OPS-VIEWS (the tools); OPS-AV, OPS-RATE, OPS-RT3s and QA-P3 (their SQL, cards, scripts and templates); for enable 4: GATE-RUN's CP-G, OWN-LV16, the v6 notice ≥ 24 h old, CP-A | evidence PRs → `main` | [specs/LIVE-P3.md](specs/LIVE-P3.md) | TL | — |

**Totals:** foundation, docs and owner 8; Josh 12; Edvard 14; David 17; QA, eval and live operations 8. P3-T02 dissolved M7-TXT into M7-REM and M7-FUP and moved OPS-VIEWS to SQ.

## 4. Waves and critical path

Owner gates and calendar trials set the exit date, not engineering days (P2 built both milestones' code in about 33 hours). Everything that feeds a gate starts first. Two chains meet at enable 4 and J5.

**Gate chain (engineering, then owner):** specs and DATA-E4a/E4b/E2 before D0 → M6-POL and DELEG-DRAFT on the contract (D0) → M6-PLAN and EV-AUT (D+1 to D+2.5), GRND merged by D+2 → **CP-G0 pre-screen #1 (D+3)** → prompt iterations and DATA-FIX v1.2 → **candidate frozen (D+4)** and the final rated run → QE2 integrity check → owner ratings (≈ 3–4 h) and judge-flag verdicts (≈ 1 h) → **CP-G (D+6)**.

**Production chain (calendar):** M04 trial last check + digest day 5 (≈ 11-07 = D+5) → **R1 + deploy #1** (D+5/D+6) → shadow window and J7 day 1 (D+6).

**Critical path:** CP5 → F0-S → M6-POL/GRND and M6-SCHED → M6-PLAN → EV-AUT → pre-screen → frozen candidate → owner ratings → CP-G → enable 4 → J5 → enable 5 → 3-day soak → **T0** → 14 days → exit.
- **Send-path sign-off:** M6-SCHED → M6-TXA, plus M6-TAKE and M6-PAUSE → **CP-A (≈ D+4.5)** → R1.
- **Slack:** M07 and David's units have about one day of slack against R1; stragglers ride deploy #2.
- **QA packages (P3-T156):** QA-S6 and QA-S7 are M+ and QA-X is M; all three start at D0 and still merge by D+1.5 with every scenario fully written (QA-S6's strict-xfail scenarios included); no scenario is dropped to make the date.
- **Near-critical owner chain:** native review of E2/E4 (v1 by D+1, v1.1 by D+2; P3-T166) → DATA-FIX → ratings → the friends' availability on D+7.

```
before D0 (10-29..11-01; docs and synthetic data only; window open now)
  F0-C contracts + specs + decisions (TL) · QA-P3 (SQ) · DATA-E4a/E2/E4b (David) · PV6 text (TL)
  GW-ESC verification of P2 #133 (TL + SQ check; a gap → J3's first int/p3 PR)
  P2 tail: CP5 landed Thu 10-29 (#137: int/m05 → main); data PRs go straight to main
  owner: approve P3 (OD-1) ✓ and group A ✓ (10-29), autonomy texts ✓ and v6 text ✓ (10-30),
         start the three lead sessions (LV-16 → TL at D+6; native review → D+1/D+2)
──── D0 Mon 11-02 (kept by the owner) · int/p3 cut from main ──────────────────────────────
Wave 0 D0      F0-S (J1+J2) · F0-R (E1) · F0-E (E3)                              ← critical
               on the contract: M6-POL (J3) · DELEG-DRAFT (E2) · RRULE, STAT-ACT, OPS-AV (David)
──── CP1 = D+1 morning: F0 merged; contracts frozen; QA-S6/S7/X skeletons by D+1.5 ────────
Wave 1 D+1..D+3  M6-PLAN (J3, stacks on M6-SCHED) ← critical · GRND → M6-TAKE (J2) · M6-SCHED → M6-TXA (J1)
                 EV-AUT (E2) ← critical · EV-E4L → EV-GATE → M7-FUP (E3) · M6-ESC → M7-TASK → M7-REM (E1)
                 David: M6-CATCH, DATA-FIX, RD-DEL, SET-P3 · SENS, COMMIT-DUE
                 SQ: OPS-TV3, OPS-VIEWS, QA-X
──── CP-G0 = D+3: pre-screen #1 ────
Wave 2 D+3..D+5  gate iterations (E2) · M6-PAUSE → GW-DOC (J2) · M6-LIFE → NUDGE-POL (J1) · M6-DELEG → M7-EXP (J3)
                 M7-AUTO → PR-OR10 (E1) · M7-MACT → M7-NUDGE (E3) · M7-MEX → M7-SSUM (E2)
                 David: RD-M7, M7-MRV, OPS-RATE, OPS-RT3s, DATA-E3F · QA-after-merge lane on int/p3
──── D+4: candidate frozen → owner rates ── CP-A ≈ D+4.5: send-path safety sign-off ────
──── CP-R1 = D+5 (Sat 11-07): int/p3 → main (owner) · deploy #1 (flags off, v6 notice, recorder) ────
D+6   enable 1–3 · shadow window (09:00, second account) · J7 day 1 (10:00) · reminders across a restart · CP-G
D+7   deploy #2 (if needed) · enable 4 · J5 live with F1/F2 (+ M05 §7 burst) · /pause timed ×3 sources · J7 leg c, then b
D+8   J7 leg a · enable 5 (standing, memory, export) · export inspection · review cards · soak starts · OPS-RT3 run
──── CP-L = D+8: live windows passed ── soak D+8..D+11 ────
──── CP-T = D+11 (Fri 11-13): T0 ── trial D+11..D+25 · weekly ratings Sundays 19:00 (D+13, D+20, D+27) · NFR-08 day 7/14 ────
──── CP-X ≈ D+26..D+28 (11-28..11-30): M06.md + M07.md + trial report → owner review → done ────
```

**Suggested slot schedule.** Leads may re-slot; this order keeps the gate chain and J5/J7 first. E2 does nothing but DELEG-DRAFT, EV-AUT and the gate iterations until the candidate is frozen (D+4). Team QA agents are fresh per PR and have no fixed slot row.

| Slot | Before D0 (10-29..11-01) | D0 (11-02) | D+1 | D+2 | D+3 | D+4 | D+5 (Sat 11-07) |
|---|---|---|---|---|---|---|---|
| J1 | — | F0-S (schema, privacy) | M6-SCHED | M6-TXA | M6-LIFE | NUDGE-POL → CP-A fixes | R1 fixes |
| J2 | — | F0-S (contracts, seams) | GRND | M6-TAKE | M6-PAUSE | GW-DOC → CP-A fixes | R1 fixes |
| J3 | — | M6-POL (a GW-ESC gap fix first, only if the verification found one) | M6-PLAN | M6-PLAN → M6-DELEG | M6-DELEG | M7-EXP | M7-EXP |
| E1 | — | F0-R | M6-ESC | M7-TASK → M7-REM | M7-REM → M7-AUTO | M7-AUTO → PR-OR10 | PR-OR10 (retrieval re-run) |
| E2 | — | DELEG-DRAFT | EV-AUT | EV-AUT; PV6 code while an eval run is in flight | gate prompt iterations | candidate frozen → M7-MEX | M7-SSUM (may land after R1 → `main`) |
| E3 | — | F0-E | EV-E4L → EV-GATE | EV-GATE → M7-FUP | M7-FUP | M7-MACT → M7-NUDGE | M7-NUDGE |
| D1 | DATA-E4a | STAT-ACT | M6-CATCH, DATA-FIX | RD-DEL → SET-P3 | SET-P3, DATA-FIX | OPS-RATE | buffer, mentor fix rounds |
| D2 | DATA-E2 → DATA-E4b | RRULE → OPS-AV | OPS-AV → SENS | COMMIT-DUE → RD-M7 | RD-M7 | M7-MRV → OPS-RT3s | DATA-E3F |
| SQ | QA-P3, GW-ESC check | F0-S review | QA-X skeleton, OPS-TV3 | OPS-TV3, OPS-VIEWS | GATE-RUN pre-screen | CP-A evidence, final rated run, QA-X | R1 QA sign-off, deploy verification |

## 5. Checkpoints

| CP | When | Who | Exit criteria |
|---|---|---|---|
| **CP0** Phase kickoff | D−4..D−1 (10-29..11-01) | TL, owner, leads | This package merged to `main`; the owner approved P3 (OD-1) and answered decision group A (or "approve all defaults"); F0-C part 2 merged (lower-document corrections, IMPLEMENTATION_PLAN §1.4 status with the approval date, the AGENTS §15 P3 line, TF §13 rows, the PR template, the proofread AI marker, disclosure lines and identity answers); labels, one issue per unit, the `P3 integration` issue and the owner-gate issues exist (WORKFLOW §8); the three lead sessions started, and each posted the capability check (WORKFLOW §3.2); QA-P3 merged; DATA-E2/E4a/E4b merged to `main` before the CP4 sync PR was cut, or held for `int/p3`; the GW-ESC verification recorded (P2 #133 covers P2-T53, or the gap filed as J3's first `int/p3` PR) |
| D0 | Mon 11-02 (kept by the owner) | TL | `int/m05` already merged into `main` (P2's CP5, Thu 10-29, #137); `int/p3` cut from `main` and pushed; F0-S, F0-R, F0-E and the contract-first units start |
| **CP1** Foundation merged | D+1 morning | Josh, Edvard, TL | F0-S → F0-R → F0-E merged into `int/p3` in that order, each CI-green with its reviews clean (ORG §3); the `push: int/p3` CI run green; **contracts frozen**, later changes only through an ICR (ORG §5); QA-S6, QA-S7 and QA-X skeletons merged by D+1.5 |
| **CP-G0** Pre-screen #1 | D+3 (11-05) | SQ, TL, E2, owner | DELEG-DRAFT, M6-PLAN, GRND, EV-E4L, EV-AUT and EV-GATE merged into `int/p3`; each live run had the owner's go-ahead and went `--estimate-only` → `--limit 1` → full (WORKFLOW §11); pre-screen report in `docs/evals/`; iteration list for E2 |
| **CP-A** Send-path safety sign-off | ≈ D+4.5 | TL, SQ, QJ1, the Tier-1A peers (E1–E3) | All nine Tier-1A units merged into `int/p3`; RG-13, RG-14, RG-18, RG-19 and RG-20 (and every extended RG test) green on the merge result; every mutation table complete with no survivor; QJ1's race and interleaving reproductions pass; SQ's evidence collated; TL sign-off recorded in [decisions.md](decisions.md) §E |
| **CP-R1** Release 1 | D+5 (Sat 11-07) | owner, TL, SQ | CP-A signed; all RG and scenario tests green on the merge result; QA-after-merge findings fixed or logged; OPS-TV3 and OPS-AV merged; the M04 trial's last check passed and digest day 5 counted; `int/p3` → `main` merged by the **owner** (or the TL, if the owner delegates this merge); `git merge-base --is-ancestor` confirms the tip is on `main`; deploy #1 with every new flag off, the v6 notice and the NFR-08 recorder; `p3-verify release` passes |
| **CP-G** Gate result | D+6 (Sun 11-08) | owner, SQ, QE2, TL | Owner ratings and judge-flag verdicts complete for the frozen candidate; QE2's three gate checks passed (the last one before this post); EV-GATE report per language in `docs/evals/`; at least one language passed; `AUTONOMY_LANGUAGES` = the passed languages only (a missing rating or verdict = NOT EVALUABLE, never a pass) |
| **CP-L** Live windows passed | D+8 (Tue 11-10) | owner, TL, SQ | Shadow window with 0 autonomous sends in `trial-check`; J5 passed (in-scope with disclosure, unknown, decision alert, injection, takeover, `/pause` within 5 s from each source, catch-up delivered); J7 legs c → b → a passed; reminders across a restart delivered; `/export` inspected by the owner; review cards checked; OPS-RT3 restore check passed; evidence content-free in `qa/evidence/`; enable 5 done |
| **CP-T** Trial start (T0) | D+11 (Fri 11-13) under OD-28 | owner, TL, SQ | Soak of ≥ 3 days with one standing delegation and a clean daily `trial-check`; the recorder has ≥ 5 days of data; v6 published ≥ 24 h before `autonomy.send` and `memory.extract`; flag set frozen and recorded; trial log started |
| **CP-X** Close | ≈ D+26..D+28 (11-28..11-30) | TL, owner | Trial ended D+25 with zero wrong-recipient, duplicate or out-of-policy sends; two weekly ratings ≥ 4/5; ≥ 70 % of immediate alerts not marked "not useful"; NFR-08 ≥ 99 %; R1 #7 re-verified across every class; DOCS-EXIT done (`M06.md`, `M07.md`, trial report, eval reports); the owner reviews → `done` |

**Trial-overlap option (OD-28):** T0 = D+8 (11-10), exit ≈ 11-25. The soak is dropped; J7 leg a and the export and review-card checks fall on trial day 1; any failure restarts the clock.

## 6. Release, deploy and flags

Every new flag defaults off and every deploy ships flags off. For each step the **owner** edits `FEATURE_FLAGS` in `var/prod.env`; the TL then runs `just prod-env-push <ip>` → restart → `just prod <ip> p3-verify release` → `trial-check` (P3-T65). Values are never printed, and no agent opens the file. Deploys come only from `main`, with the owner's go-ahead for that deploy (WORKFLOW §10). Flag names: [contracts/P3-interfaces.md](contracts/P3-interfaces.md) §10.

| Step | When | What | Guard |
|---|---|---|---|
| **Deploy #1** | D+5 evening or D+6 morning, never within ±1 h of the 21:00 digest | All R1 code, every new flag **off**. `POLICY_VERSION = "v6"`, so the one-time notice with opt-out goes out now. The NFR-08 recorder is live (unflagged ops instrumentation). `owner_request.v10` active (retrieval re-run already passed) | Owner go-ahead; `p3-verify release`; the restore-test check runs in LIVE-P3 |
| Enable 1: owner-only M07 | D+6 | `reminders.run`, `tool.create_reminder`, `tool.list_tasks`, `tool.update_task`, `tool.create/list/update/delete_automation`, `tool.list_delegations`, `commitments.due_alerts`, `rating.weekly`, `scheduled_summaries.run` (if in deploy #1), `monitors.p3_actions` | Deploy #1 healthy |
| Enable 2: follow-ups | D+6, before 10:00 | `followups.run`, `tool.create_follow_up`, `followups.nudge` (approval cards only; FR-FUP-06) | Enable 1 healthy |
| Enable 3: shadow | D+6 | `triage.assist_trigger` (if not already on after the M04 trial); `tool.create_delegation`, `tool.end_delegation`, `autonomy.run` with **`autonomy.send` off**. Every ALLOW_AUTO becomes a held reply with an `autonomy_not_sent` alert (`alert_kind = shadow`; Draft reply shows the card), with the decision recorded (the P2-T21 "drafts only" pattern); `trial-check` must show 0 autonomous sends | OPS-TV3 deployed |
| **Deploy #2** | D+7 early, away from the digest | Stragglers merged to `main` after R1 (expected M7-SSUM; possibly M7-EXP, M7-MEX, M7-MACT) and shadow-window fixes. **Skipped if there are none.** Nothing deploys after the soak starts except a stop-the-line fix | Owner go-ahead; `p3-verify release` |
| Enable 4: autonomy | D+7 | `autonomy.send` on; `AUTONOMY_LANGUAGES` = the languages that passed S06.5 | CP-G (≥ 1 language passed); LV-16 done; v6 notice ≥ 24 h old; CP-A signed; shadow window clean |
| Enable 5 | D+8 | `delegations.standing` (after J5's time-bounded delegation ran clean); `memory.extract` (≥ 24 h after the notice); `export.enabled` (the owner inspects; no agent ever opens the file) | Enable 4 clean |
| **Soak** | D+8 → D+11 | ≥ 3 days with one standing delegation (OD-28); daily `trial-check` | — |
| **T0** | D+11 | 14-day Personal v1 trial; flag set frozen | Soak clean; recorder ≥ 5 days of data; v6 published |

**Trial protections:**
1. **M04 trial and M05 digest days.** No P3 deploy, not even flags-off, before the M04 trial's last daily check and digest day 5 (≈ 11-07). `main` stays P2-only until R1, so a P2 hotfix never ships P3 code. (The P2 verifier fails any `delegation_auto` or new action origin during the M04 trial.)
2. **Digest count.** No deploy within ±1 h of the 21:00 local digest.
3. **Personal v1 freeze.** For the 14 days: no flag changes and no feature deploys. **Stop-the-line** = `autonomy.send` off (one flag; `/pause` is the instant brake) → a fix PR to `main` → a deploy go-ahead → the 14-day clock restarts (PRD §21 R2 #7). Non-safety findings wait until after the trial.
4. **Daily `p3-verify trial-check`** at a fixed time; any FAIL stops the line.
5. **Business-chat live checks run only on Alviro**, in planned windows with a go-ahead per run (P2-O7). Evidence is content-free.

## 7. What the owner does ("Needs you")

Only what needs the owner's hands, accounts, money or judgement. Everything else the TL and the leads carry on with. Decision questions and their defaults: [decisions.md](decisions.md) §A; the six document conflicts the owner must answer: [decisions.md](decisions.md) §C.

| When | Item | Why it cannot be automated |
|---|---|---|
| By D−1 (Sun 11-01) | **Done 2026-10-29:** P3 approved (OD-1); decision group A answered (OD-11 changed, the rest defaults; [decisions.md](decisions.md) §A0); conflicts 1–5 answered (conflict 6 stays with OD-24). The owner merged the planning PR on 10-29 (#139) | Milestone approval (AGENTS §3); product decisions; higher documents change only with the owner (AGENTS §2) |
| Done 10-30 | Proofread [autonomy-texts.md](autonomy-texts.md): approved as drafted, marker 🤖 (OD-4, OD-10, P3-T61; [decisions.md](decisions.md) §A3) | Contacts read these words in the owner's name |
| Done 10-30 | Approve the v6 privacy text and its in-bot notice (OD-24, due D+3; §A3). Publication before deploy #1 still needs the owner's go-ahead (PV6 §3.4) | Privacy policy (AGENTS §16) |
| Moved to the TL 10-30 | **LV-16 rate re-test:** the TL runs it on Alviro in the D+6 window (the dev bot has no business connection); the owner's part is the go-ahead in the D+6 row (OD-27 as answered, P3-T168) | — |
| By D−1 | Start the three lead sessions: Josh (Claude Code), Edvard (Codex CLI), David (Gemini in Antigravity); fix anything a capability check reports missing | Session setup is the owner's |
| D+1..D+2 (11-03..11-04) | Native-language review: E2 and E4 v1 plus the SENS term list by D+1; the v1.1 additions by D+2 as DATA-E2/E4a/E4b merge (≈ 8 h in total; P3-T166, P3-T167) | Native-speaker judgement |
| Done 10-29 | Merge `int/m05` into `main` (P2's CP5; done 10-29, #137; D0 stays Mon 11-02) | An integration branch into `main` is the owner's (AGENTS §15) |
| D+2 (Wed 11-04) | Decision group B: gate details (OD-21), the P3 eval budget, USD 3 for Luna runs (OD-22), envelope or per-run go-aheads (OD-23) | Cost (AGENTS §8, §9) |
| D+3 (Thu 11-05) | Go-ahead for each pre-screen run; decision group C (OD-25; OD-24 answered 10-30) | Eval spend; production rules |
| D+4..D+6 (11-06..11-08) | Rate the frozen candidate: ≈ 100 E2 delegated-mode drafts + ≈ 50 E4 in-scope replies (≈ 3–4 h), then judge-flag verdicts (≈ 1 h) | Gate results count only owner-confirmed items (IMPLEMENTATION_PLAN §10.1) |
| D+5 (Sat 11-07) | **Merge R1** (`int/p3` → `main`) or delegate that merge; deploy #1 go-ahead; retrieval re-run go-ahead (≈ USD 0.1) | AGENTS §15; deploys and eval spend are the owner's |
| D+6 (Sun 11-08) | Decision group D (OD-26; OD-27 answered 10-30); the go-ahead for the TL's LV-16 burst on Alviro right after the shadow window, and for its tooling path (P3-T168); edit `FEATURE_FLAGS` in `var/prod.env` for enable 1–3, one step at a time (the TL then pushes the file and restarts; P3-T65); **shadow window** (≈ 45 min, 09:00, second account); **J7 day 1** (10:00: commitment #1 and follow-up F-c, then that chat stays silent); confirm reminders on the phone across a restart; finish the ratings → CP-G | Production bot (Alviro); real accounts; phone |
| D+6..D+7 | Brief F1 and F2 that an AI will **reply** to them, with disclosure (OD-26) | Real people |
| D+7 (Mon 11-09) | Deploy #2 go-ahead (if needed); edit the flags for enable 4; **J5 live** with F1 and F2 in their own chats + the M05 §7 burst test; `/pause` timed from the command, the button and the stop word; **J7 leg c**, then commitment #2 and **leg b** (approve or skip the nudge card), then commitment #3 | Client taps; real people; content judgement |
| D+8 (Tue 11-10) | **J7 leg a**; edit the flags for enable 5; open and inspect `/export` (only the owner opens it); memory review cards; OPS-RT3 restore check (go-ahead, age key, then delete the key); decision group E (OD-28) | Real data; offline key; client taps |
| ≈ D+6 | Group F (OD-29): adopt an independent audit model call? Only if a confirmed E4 violation reached ALLOW_AUTO past the deterministic rules | Product and cost decision |
| D+11 (Fri 11-13) | OK T0 after a clean soak | Trial start |
| D+11..D+25 | Daily use; a daily glance at `/activity`; check any uncertain chat; mark alerts "not useful" when they are; the **weekly usefulness rating** card, each Sunday at 19:00 (D+13, D+20 and D+27; P3-T60) | The owner's own use and judgement |
| D+26..D+28 (11-28..11-30) | Review `M06.md`, `M07.md` and the trial report → `done` | Milestone acceptance |

## 8. Document map

| Document | Purpose |
|---|---|
| [decisions.md](decisions.md) | Owner decisions requested (OD-1…OD-33, §A), TL decisions (§B), document conflicts (§C), the interface-change log (§D), safety sign-offs (§E) |
| [ORG.md](ORG.md) | Roles, ownership, review matrix, communication, escalation, mentoring, metrics |
| [WORKFLOW.md](WORKFLOW.md) | Binding procedure: safety rules, workspace, branches and PRs, tests, review and fix, integration, GitHub coordination, datasets, deploy, flags, live windows, evals, usage limits |
| [contracts/P3-schema.md](contracts/P3-schema.md) | Exact schema of migration `0015_p3`; content columns; retention; deletion cascade; post-restore |
| [contracts/P3-interfaces.md](contracts/P3-interfaces.md) | Module map, reason codes, state events, policy, ports, disclosure, read seams, jobs, registries, prompts, settings, flags, notifications, control chat, audit events, eval and ops seams, file ownership and the never-edit-without-an-ICR list |
| [autonomy-texts.md](autonomy-texts.md) | Owner-approved texts contacts read (AI marker, disclosure lines, identity answers; approved 2026-10-30); F0-S copies them into `core/autonomy_texts.py` (P3-T61) |
| [specs/](specs/) | One spec per unit, named by unit id (`specs/M6-POL.md`); junior-grade for David's units |
| [handoffs/](handoffs/) | One start-here brief per role: `tech-lead`, `josh`, `edvard`, `david`, `junior-engineers`, `senior-qa`, `team-qa` |
| [qa/](qa/) | QA-P3 delivers the test plan, regression matrix, live checklists, trial log, evidence template and `p3-verifier.md` here |
| `docs/milestones/M06.md`, `M07.md` | Milestone records (plan pointer now; report at the end) |
| `docs/evals/` | Pre-screen, gate and retrieval reports |

Source documents keep their precedence (AGENTS §2): TELEGRAM_FEASIBILITY > PRD > AGENTS §6 > ARCHITECTURE > IMPLEMENTATION_PLAN > these specs. If a spec here conflicts with a higher document, the higher one wins and the engineer raises it with the TL (ORG §5).
