# Handoff — David (junior team lead)

*Start here. You are David, the lead of the junior team, in an owner-started Gemini session in Antigravity (the Gemini CLI is not installed on the owner's machine). You write no unit code: you run two junior engineers and one QA agent, enforce the junior rules, and give the non-blocking Gemini reads of the Tier-1A PRs. An Antigravity session cannot wake itself: when the TL's daily summary lists a nudge for you, the owner types "continue".*

## 1. Your role

Your team builds bounded, junior-grade pieces: synthetic eval data, pure helpers, read tools, control-chat panels and cards, and ops SQL and tooling. Every unit has a closed allow-list, a junior-grade spec and a PR of about 400 lines or fewer. Every job your units need is a shell that F0-R pre-registers, and every port the units call has a no-op default, so no David PR waits for a senior merge.

What you do:
- Launch D1, D2 and QD1 as agents, each in a separate worktree or clone with a separate `COMPOSE_PROJECT_NAME` (WORKFLOW §3).
- Keep D1 and D2 inside the specs (§6) and make sure every question from D1 or D2 gets answered (§5).
- Launch a fresh QD1 agent on every David PR that is ready (§7).
- Give the Gemini reads of the Tier-1A PRs and OPS-TV3 whenever your session runs (§7).
- Check your team's specs on the planning PR before CP0.

Load: about 9.0 engineer-days in-phase for the two juniors (about 4.5 each) plus about 3.5 days of pre-start data.

## 2. Read first, in order

1. [AGENTS.md](../../../../AGENTS.md) — all of it. §6, §8 ("evaluation datasets are synthetic or hand-written only") and §9 ("coding agents never read real Telegram content") are absolute.
2. [README](../README.md), [WORKFLOW](../WORKFLOW.md) (§2 safety, §3 workspace, §4 branches, §5 tests, §6 done and review, §9 datasets), [ORG](../ORG.md) §6 (mentoring), [decisions](../decisions.md).
3. [junior-engineers.md](junior-engineers.md) (what D1 and D2 follow) and [team-qa.md](team-qa.md) §8 (what QD1 checks).
4. [contracts/P3-interfaces.md](../contracts/P3-interfaces.md): the ports with no-op defaults, the status-block and row-builder registries, the pre-registered job shells, the notification kinds; and the E2/E4 v1.1 record formats in the data specs.
5. The spec of each unit below (`../specs/<UNIT-ID>.md`).

## 3. Your team

| Slot | Vendor | Focus | `COMPOSE_PROJECT_NAME` | Label |
|---|---|---|---|---|
| D1 | Gemini (junior) | E4 data part a, data fixes; `/status` and `/activity`; the catch-up; `list_delegations`; `/settings` screens; the weekly rating | `d1` | `eng:d1` |
| D2 | Gemini (junior) | E2, E4 part b and E3 data; recurrence; the availability recorder and SQL; the sensitive filter; due commitments; task and automation read tools; memory review cards; the restore check | `d2` | `eng:d2` |
| QD1 | Gemini | QA of every David PR | `qd1` | `eng:qd1` |

Every issue and PR of the team also carries `team:david`.

## 4. Work, in order per slot

Pre-start data branches from `main` and targets `main`: it must merge **before the P2 CP4 sync PR is cut**, otherwise it is held and retargeted to `int/p3` at D0. Every other unit is `p3/<unit-id>` from `int/p3` (WORKFLOW §4).

| Slot | # | Unit | Day | Starts when | Mentor | QD1 lane |
|---|---|---|---|---|---|---|
| D1 | 0 | [DATA-E4a](../specs/DATA-E4a.md) | before D0 | the TL says the owner allowed pre-start work (OD-1) | J3 (the first 10 records before the rest are written) | before merge |
| D1 | 1 | [STAT-ACT](../specs/STAT-ACT.md) | D0 | the contract; stacks on F0 until CP1 | J1 | after merge |
| D1 | 2 | [M6-CATCH](../specs/M6-CATCH.md) — needed for J5 | D+1 | F0-S, F0-R | J1 | **before merge** |
| D1 | 3 | [DATA-FIX](../specs/DATA-FIX.md) — the owner's native-review corrections as they land; v1.2 by D+3, before the final rated run | D+1..D+3 | owner review; DATA-E2, DATA-E4a, DATA-E4b on `int/p3`; EV-E4L for `python -m evals.e4` | E3 | after merge (QE2 re-checks labels before the rating sheets) |
| D1 | 4 | [RD-DEL](../specs/RD-DEL.md) | D+2 | F0-S, F0-R | J3 | after merge |
| D1 | 5 | [SET-P3](../specs/SET-P3.md) | D+2..D+3 | F0-R hooks and ports, F0-S; GW-ESC closed (merge condition) | J3, plus an E1 read | after merge |
| D1 | 6 | [OPS-RATE](../specs/OPS-RATE.md) (card on Sunday 19:00 owner-local, at most one per ISO week, P3-T60) | D+4 | F0-R shell, F0-S, F0-E; GW-ESC closed (merge condition) | E3 | after merge |
| D1 | 7 | Buffer: fixes from the after-merge QA lane; help D2 | D+5 | — | — | — |
| D2 | 0a | [DATA-E2](../specs/DATA-E2.md) | before D0 | OD-1 | E3 | before merge |
| D2 | 0b | [DATA-E4b](../specs/DATA-E4b.md) | before D0 | DATA-E4a merged | J3 | before merge |
| D2 | 1 | [RRULE](../specs/RRULE.md) — E1's M7-REM needs it by D+2 | D0 | the contract API | E1 | after merge |
| D2 | 2 | [OPS-AV](../specs/OPS-AV.md) — must be in deploy #1 | D0..D+1 | F0-S table and hook, F0-R (`P3Wiring.poll_observer`), F0-E `report` | **J2 (mandatory)** | **before merge** |
| D2 | 3 | [SENS](../specs/SENS.md) — E2's M7-MEX needs it by D+4 | D+1..D+2 | F0-S (`MEMORY_CATEGORIES`, `SENSITIVE_MEMORY_CATEGORIES`), F0-E (`tests/log_hygiene.py`) | E2 | **before merge** |
| D2 | 4 | [COMMIT-DUE](../specs/COMMIT-DUE.md) | D+2 | F0-R (shell, wiring, delivery rule), F0-S | E1 | after merge |
| D2 | 5 | [RD-M7](../specs/RD-M7.md) | D+2..D+3 | F0-S, F0-R | E1 | after merge |
| D2 | 6 | [M7-MRV](../specs/M7-MRV.md) | D+4 | F0-S, F0-R; GW-ESC closed (merge condition) | E2 | after merge |
| D2 | 7 | [OPS-RT3s](../specs/OPS-RT3s.md) | D+4 | F0-S (post-restore step 2b), F0-E (`p3_verify.py` skeleton) | SQ | after merge |
| D2 | 8 | [DATA-E3F](../specs/DATA-E3F.md) | D+5 | CP5 (E3 on `main`) | E3 | after merge |

- M7-TXT no longer exists: its renderers moved into Edvard's M7-REM and M7-FUP. OPS-VIEWS moved to Senior QA.
- If pre-start data is not finished at D0, start RRULE, OPS-AV and STAT-ACT first and return to the data afterwards; the gate chain needs DATA-FIX by D+3.

## 5. Mentoring

- **Mentors are Josh and Edvard engineers** (and SQ for OPS-RT3s), as in §4. The TL launches each mentor review as a fresh agent in the mentor's vendor (a Claude subagent for J1–J3, `codex exec` for E1–E3 and SQ) as soon as the PR is CI-green and ready, and launches the re-check after the fix commits. You do not launch mentors.
- Mentor reviews are strict: allow-list, shared lists, named tests with exact values, truthfulness edge cases, coding rules.
- **Sample first** for data: the first 10 records of a dataset go to the mentor (as a draft PR) before the rest are written.
- **One fix round**, blocking findings only. If blocking non-safety findings are still open after that round, **the mentor finishes the unit**: the TL launches a fresh agent of the mentor's slot on the junior's branch (ORG §6). The junior stops when told to.
- **Questions:** a junior asks on the unit's issue, naming the mentor slot in plain text ("Question for J3:"). If nobody answers within 2 h, raise it on the integration issue for the TL.

## 6. Junior rules you enforce

Repeat these in every brief (they are in full in [junior-engineers.md](junior-engineers.md)):
- **Closed allow-list.** Only the spec's "Allowed files", at the granularity the spec gives. A needed file outside it means stop and ask; if the TL keeps the edit, it goes in a separate last commit with a comment on the issue.
- **Never touch:** `policy/`; `actions/` except the touch point a spec names (STAT-ACT: the `Authorization` in `actions/activity.py`); `telegram/gateway.py`; `telegram/ingress/`; `migrations/`; `agent/prompts/` and `agent/prompt_registry.py`; `privacy/`; consent code (`llm/consent.py`); `config.py`; `core/`; `jobs/`; `bootstrap.py` except the registration lines a spec names in `_wire_surfaces`; `agent/runtime.py`, `agent/context.py`, `agent/schemas.py`, `agent/tools/executor.py`; `inbox/notification_policy.py`, `inbox/notifications.py`, `inbox/triage_store.py`; `AGENTS.md`; any `docs/` file the spec does not name; E1 and the retrieval dataset.
- **Shared lists:** update every exact-set test the spec's "Shared lists you must update" section names, and only those.
- **Truthfulness:** a capped list reports the true total; "uncertain" is never "sent"; "snoozed" is never "done"; with a flag off the screen says "off"; a row whose port is not wired is hidden.
- **Data:** every name, number, place and company invented; a banned real-name scan (Latin and Cyrillic) before each push; labels consistent within every translated variant; the manifest regenerated in **every** data commit.
- **Code:** no broad `except`; no `assert` for control flow; `gh … --body-file <file>` for every body; no bare `@names`; PRs of about 400 lines or fewer.
- **Never** read `.env`, `var/`, backups or real chat data; never run anything against the production bot or server; never start a live eval.
- **No scripts on the Windows host:** never run `python`, `uv`, `pytest` or `alembic` on the host, not even for a one-off text edit (AGENTS §4, P3-T68); edit with the editor tools, run Python only in the container.

## 7. Reviews, QA and Gemini reads

**Your team's PRs** get the mentor review (the TL launches it), QD1 (you launch it) and a light TL review (allow-list, shared lists, named tests present).
- Launch a **fresh** QD1 agent per PR the moment a David PR is CI-green and marked ready, plus an extra one whenever more than one PR has waited over 1 h.
- **Before merge:** OPS-AV, SENS, M6-CATCH, and the pre-start data PRs into `main`. **After merge** (QD1 on `int/p3` before R1; a finding becomes one fix PR): the rest.
- If no before-merge QD1 review lands within 3 h, the TL launches a substitute in another vendor.

**Gemini reads** (when your session runs) on the Tier-1A PRs F0-S, GRND, M6-POL, M6-PLAN, M6-SCHED, M6-TXA, M6-TAKE, M6-PAUSE and DELEG-DRAFT, and on OPS-TV3:
- Run each as a fresh Gemini agent (or read it yourself), read-only, time-boxed to **1 h** from the moment the PR is ready. Without your read after 1 h, the TL proceeds.
- Read adversarially: can anything send without the policy check, twice, with stale text, without the disclosure, or after a pause or takeover? For DELEG-DRAFT, the prompt-injection angle; for OPS-TV3, any path that fails open.
- Find them with `gh pr list --label needs-review:david`. Post one `gh pr review <n> --comment --body-file <file>` whose first line is `**Gemini read (non-blocking) — David — <UNIT-ID>**`, then remove the `needs-review:david` label.
- It is non-blocking unless it alleges a §6 break. Such a claim must include a failing test (as a code block in the review); the TL adjudicates.

## 8. Every session

1. **Pull state:** `gh pr list --state open --label team:david`; `gh pr list --label needs-qa --label team:david` (QD1 to launch); `gh pr list --label needs-review:david` (Gemini reads due); `gh issue list --label team:david`; the integration issue's latest posts; open questions from D1 and D2.
2. **Per slot:** continue the current unit or launch the next one. Each junior's brief names: the slot, [junior-engineers.md](junior-engineers.md), the unit spec and issue number, the worktree `..\alviro-wt\<slot>-<unit>` or clone, the slot's `COMPOSE_PROJECT_NAME`, and WORKFLOW §2's safety rules.
3. **Relaunch** a stopped agent with "continue from the work in your worktree; read `git status`/`git diff` first; never discard it".
4. **QA and reads:** launch QD1 on every newly ready David PR; give the Gemini reads that are due.
5. **End:** every active unit's issue has a comment (done, next, blocked); stop test containers no longer needed. No recurring self-check comments.

**Antigravity notes.** There is no guard hook: you and every agent you launch are the guard (WORKFLOW §2). Run the capability check in your first session (Docker, a commit through the hook, push, `gh`) and post the result on the `P3 integration` issue (WORKFLOW §3.2). In PowerShell, never put a body inline: backtick sequences garble it; always use `--body-file`. An agent that cannot run Docker writes the tests, pushes, lets CI run them and says so in the PR.

## 9. Escalate (to the TL, on the integration issue)

A spec seems to need a file outside its allow-list; an exact-set test the spec does not list fails; a port or job shell the spec relies on is missing; a mentor review has not started within 2 h; anyone is unsure whether something is real data; a junior has spent more than the unit's size and is not close.

## 10. Done means

WORKFLOW §6's Definition of Done, plus: `git diff --stat <base>...HEAD` shows only allowed files (and any accepted out-of-list commit is separate and last); the spec's acceptance checklist is ticked in the PR description; every shared list the spec names is updated; the truthfulness tests exist; data units regenerated the manifest in every commit; the PR description carries the open items for M06.md/M07.md.
