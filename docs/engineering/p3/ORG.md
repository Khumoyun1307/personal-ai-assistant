# P3 engineering organization

*Who owns what, who reviews whom, how people talk to each other and when something goes to the owner. Josh, Edvard and David each run a team in a different vendor's tool; the TL integrates, dispatches cross-team reviews and runs Senior QA. Every agent may run subagents inside the unit's boundaries (WORKFLOW §2). Slots (J1, E2, D1, QJ1, SQ …) may get personal names later (P3-T08); documents refer to leads and engineers by slot or lead name only, never by pronoun.*

## 1. Roles

| Role | Who (vendor, session) | Accountable for | Not responsible for |
|---|---|---|---|
| **Owner** | the owner | Product decisions (OD-1…OD-33), approvals, the R1 merge of `int/p3` into `main` (or delegating it), every deploy go-ahead, editing the production flags in `var/prod.env` (the TL pushes the file and restarts), live-window client steps on Alviro, eval spend per run, native-language review and ratings, the 14-day trial use, milestone acceptance; starting the three lead sessions and saying "continue" when the daily summary asks | Implementation details |
| **Technical Lead (TL)**, also called Principal Lead | TL (Claude Code, always-on self-paced loop) | Contracts and every spec sign-off; decisions log; ICR rulings within 2 h; **all merges into `int/p3`** (batches about twice a day) and P3's PRs to `main` (docs, plan, pre-start data; after R1, flagged PRs); **review dispatch**: every cross-team peer review, launched in the reviewer's vendor; running **Senior QA** as a Codex agent; relaunching any team-QA review past its 3 h SLA; TL review on every PR (deep on Tier-1A and OPS-TV3); CP-A sign-off; owner-gate coordination; deploys on the owner's go-ahead, and `just prod-env-push` plus the restart after the owner edits the flags (never opening `var/prod.env`); the daily summary; PV6 text; the GW-ESC verification; DOCS-EXIT | Writing feature code (only fixes to the TL's own docs and specs) |
| **Lead** (×3) | **Josh** (Claude Code), **Edvard** (Codex CLI), **David** (Gemini in Antigravity), each an owner-started session | Slotting the team's engineers; launching the team's engineers (builders) and team-QA agents, each fresh per step in a separate worktree with a separate `COMPOSE_PROJECT_NAME`; sequencing in-team edits to shared files (for example Josh's `actions/policy_loader.py`, which M6-PLAN, M6-TXA, M6-TAKE and NUDGE-POL all touch); relaunching a stopped agent from the agent's worktree; drafting the team's senior specs before D0 (docs only; the TL signs off); answering the team's questions on issues; handoff notes. **David** also gives the time-boxed **Gemini reads** on Tier-1A PRs and OPS-TV3 | Writing unit code; merging; cross-team peer reviews (the TL dispatches them, including those named for a lead) |
| **Engineer** | J1–J3 (Claude), E1–E3 (Codex): seniors. D1, D2 (Gemini): **juniors** | The slot's units exactly as specified: red-first tests, implementation, relevant local tests, a draft push at every coherent step, the PR with the template, the issue note, one fix round. **J1 is the schema steward** (Alembic revision numbers; the only issuer of a later `0016`) | Files owned by another unit without an ICR; juniors: anything outside the unit's closed allow-list |
| **Team QA** | QJ1, QJ2 (Claude, Josh's team); QE1, QE2 (Codex, Edvard's team); QD1 (Gemini, David's team), each a fresh agent per PR | QJ1: QA-S6, then race and interleaving reproductions on real Postgres. QE1: QA-S7, then M07 PR QA. QE2: three gate checks (after pre-screen #1, before the rating sheet, before the CP-G post; P3-T113), plus privacy and isolation QA. QJ2, and from D+1.5 also QJ1: PR QA for Josh units, with the **mutation table** on Tier-1A. QD1: PR QA for every David PR | Feature code (QA writes tests and ops tooling only); re-reviewing a peer's line-by-line work |
| **Senior QA (SQ)** | SQ (Codex, run by the TL session through `codex exec`, workspace-write only in SQ's own worktree) | QA-P3, QA-X, OPS-TV3, OPS-VIEWS; the GW-ESC check (P2 #133 against P2-T53); GATE-RUN (sheets only after QE2's check, ledgers, reports to `int/p3` before R1 and to `main` after it); CP-A evidence; QA sign-off at CP-A and R1; deploy verification; LIVE-P3 records; the daily trial check; NFR-05/07/08 at day 7 and day 14; the weekly rating counts; peer review of OPS-RT3s and the gate metrics (EV-GATE) | Feature code; starting a live eval run without the owner's go-ahead |

The TL may be resumed in a new session from [handoffs/tech-lead.md](handoffs/tech-lead.md). A lead may be resumed from the lead's handoff; the team's open work is on GitHub and in the engineers' worktrees.

## 2. Ownership (RACI by unit group)

R = does the work · A = accountable (merges or signs off) · C = consulted (reviews) · I = informed.

| Unit group | Josh's team | Edvard's team | David's team | SQ | TL | Owner |
|---|---|---|---|---|---|---|
| F0-C (including the GW-ESC verification), PV6 text, DOCS-EXIT | C (Josh lead: team specs) | C (Edvard lead: team specs) | C (David lead: team specs) | C | R, A | approves decisions and v6; reviews reports |
| F0-S | R (J1 + J2) | C (Edvard lead) | C (Gemini read) | C | A (deep) | I |
| F0-R, F0-E | C (Josh lead; a Josh engineer) | R (E1, E3) | I | C (F0-E) | A | I |
| Tier-1A send path: GRND, M6-POL, M6-PLAN, M6-SCHED, M6-TXA, M6-TAKE, M6-PAUSE | R (J1–J3; QJ1/QJ2 QA) | C (E1–E3 peers) | C (Gemini read) | C (CP-A evidence) | A (deep, CP-A) | I |
| M6-DELEG, M6-LIFE, NUDGE-POL, GW-DOC, M7-EXP (and a GW-ESC gap fix, only if the verification finds one) | R | C (E1–E3 peers; QE2 privacy read on M7-EXP) | I | I | A | I |
| DELEG-DRAFT and the gate iterations | C (J3 peer) | R (E2; QE2 QA) | C (Gemini read) | I | A (deep) | I |
| EV-E4L, EV-AUT, EV-GATE | C (J1, J3, a Josh engineer) | R (E2, E3; QE2 QA) | I | C (EV-GATE metrics) | A | I |
| M6-ESC | C (J2) | R (E1; QE2 QA) | I | I | A | I |
| M07 flows: M7-REM, M7-TASK, M7-FUP, M7-NUDGE, M7-AUTO, M7-SSUM, M7-MACT, M7-MEX; PR-OR10; PV6 code | C (J1, J3, a Josh engineer) | R (E1–E3; QE1/QE2 QA) | I | I | A | go-ahead for the retrieval re-run (PR-OR10) |
| David's data: DATA-E2, DATA-E4a, DATA-E4b, DATA-FIX, DATA-E3F | C (J3 mentor for E4a/E4b) | C (E3 mentor for E2, FIX, E3F) | R (D1, D2; QD1 QA) | I | A | native review; corrections |
| David's code: RRULE, SENS, M6-CATCH, STAT-ACT, RD-DEL, SET-P3, RD-M7, M7-MRV, COMMIT-DUE, OPS-AV, OPS-RATE, OPS-RT3s | C (J1, J2, J3 mentors) | C (E1, E2, E3 mentors) | R (D1, D2; QD1 QA) | C (OPS-RT3s mentor) | A (light) | I |
| QA-P3, QA-X, OPS-TV3, OPS-VIEWS | C (Josh lead; J1 for OPS-TV3, OPS-VIEWS) | C (Edvard lead for QA-X) | C (Gemini read on OPS-TV3) | R | A (deep on OPS-TV3) | I |
| QA-S6, QA-S7 | R (QJ1, QA-S6); C (Josh lead; a Josh engineer on QA-S7) | R (QE1, QA-S7); C (Edvard lead) | I | C (QA-S6) | A (light) | I |
| GATE-RUN | I | C (E2 iterations; QE2's three gate checks) | I | R | A | go-ahead per run; ratings; verdicts (card-only finding kinds need none, P3-T111) |
| R1, deploys, flags, LIVE-P3 | C | C | I | R (verification, records) | R (deploys, server side) | A (merge, go-aheads, client steps) |
| OWN-LV16 | I | I | I | C (probe script) | R (TF §12 record) | R (runs it) |

## 3. Review matrix

**Principles:**
- **No senior PR is reviewed only by its author's vendor.** Claude-authored PRs get a Codex peer; Codex-authored PRs get a Claude peer; Gemini-authored PRs get a Claude or Codex mentor.
- **Every review is a fresh agent in the reviewer's role**, never the authoring agent. It is briefed with the reviewer slot's handoff, the unit's spec, the contracts and WORKFLOW §2.
- **Each reviewer posts one GitHub review** (`gh pr review <n> --comment --body-file <file>`), with every finding marked **blocking** or **non-blocking**, and no bare `@names`. Only content authored by `Khumoyun1307` counts as coordination input.
- **One fix round**, for blocking findings only (WORKFLOW §6.4). Everything else goes to "Open items for M06.md/M07.md" in the PR description. A broken safety invariant is always fixed.

**Who launches which review (P3-T05):**

| Review | Launched by | How |
|---|---|---|
| **Cross-team peer review** (every row below, including reviews named for a lead) | **TL session**, the moment the PR is CI-green and marked ready | Codex reviewer: `codex exec --sandbox read-only --ephemeral` in a clean checkout of the PR head, briefed as the named slot; the TL saves its final message to a private temp file and posts it with `gh pr review --comment --body-file`. The sandbox flags are unverified on codex-cli 0.164 on Windows until the TL's first launch tests them ([tech-lead.md](handoffs/tech-lead.md) §4.4, P3-T66). Claude reviewer: a Claude subagent briefed as the named Claude engineer, which posts its own review |
| **Team QA** | The **author's lead**, in the lead's own tool, at the same moment | A fresh QA agent per PR, in its own worktree and compose project (QJ in Claude Code, QE in Codex, QD1 in Antigravity) |
| **Gemini read** (Tier-1A, OPS-TV3) | **David's session**, when it runs | Time-boxed to 1 h, in parallel, **non-blocking** unless it alleges an AGENTS §6 break; such a claim needs a failing test, and the TL rules on it |
| **Senior QA** work and SQ's own reviews | **TL session** | `codex exec` with workspace-write limited to SQ's own worktree |
| **TL review** | TL | After peer and QA are clean, within 1 h |
| **Fix-round re-check** | Whoever launched the original review | A fresh agent of the same slot and vendor, given the original review, checks only the fix commits, within 1 h (never the original agent; P3-T65) |
| **SLA substitute** | TL | When a peer or QA review misses its SLA, the TL launches a fresh reviewer itself (Claude subagent or `codex exec`), never from the author's vendor |

**Matrix** (★ = Tier-1A):

| Author (vendor) | Units | Peer: fresh agent, other team, other vendor | QA | Extra | TL |
|---|---|---|---|---|---|
| Josh (Claude), **Tier-1A** | F0-S ★, GRND ★, M6-POL ★, M6-PLAN ★, M6-SCHED ★, M6-TXA ★, M6-TAKE ★, M6-PAUSE ★ | Edvard (Codex), chosen for interface knowledge: **E2** for GRND, M6-POL and M6-PLAN (E2 builds the draft they consume); **E1** for M6-SCHED, M6-TAKE and M6-PAUSE (the inbox and triage side); **E3** for M6-TXA. F0-S: **Edvard lead + SQ** | QJ1/QJ2 before merge + **mutation table** | **Gemini read** (David lead), ≤ 1 h, non-blocking | **Deep review; CP-A** |
| Josh (Claude), Tier-1 | M6-DELEG, M6-LIFE, NUDGE-POL, GW-DOC, M7-EXP; a GW-ESC gap fix (only if the verification finds one) | **E1** for M6-DELEG and M6-LIFE; **E3** for NUDGE-POL (the M7-NUDGE consumer); **E2** for M7-EXP, GW-DOC and a GW-ESC gap fix | QJ before merge; M7-EXP also gets a QE2 privacy read | — | Scope check (deep on M7-EXP) |
| Edvard (Codex), **Tier-1A** | DELEG-DRAFT ★ | **J3** (Claude; the M6-PLAN consumer) | QE2 before merge + **mutation table** (validator and refusal paths) | Gemini read (prompt-injection angle) | **Deep review** (prompt rules against AGENTS §8) |
| Edvard (Codex), high and safety-adjacent | F0-R, EV-AUT, EV-GATE, M6-ESC, M7-FUP, M7-NUDGE, M7-SSUM, M7-MEX, PR-OR10 | **Josh lead** for F0-R; **J3** for EV-AUT (the planner author); **J1 + SQ** for EV-GATE (SQ for the gate metrics); **J2** for M6-ESC (fast lane and pause notices); **J1** for M7-NUDGE (the NUDGE-POL author); **J3** for M7-FUP, M7-SSUM, M7-MEX and PR-OR10 | QE1/QE2 before merge | — | Scope check; deep on EV-GATE and M7-MEX |
| Edvard (Codex), product flows and tooling | F0-E, EV-E4L, M7-REM, M7-TASK, M7-AUTO, M7-MACT, PV6 code | A Josh engineer (Claude); F0-E also **SQ** (shared test lists) | QE1 after merge (EV-E4L: QE2 after merge) | — | Scope check |
| David (Gemini), juniors | M6-CATCH, STAT-ACT, RD-DEL, SET-P3, OPS-AV, DATA-E4a, DATA-E4b | **Josh mentor** (Claude): **J1** for M6-CATCH and STAT-ACT; **J2 for OPS-AV (mandatory)**; **J3** for RD-DEL, SET-P3, DATA-E4a and DATA-E4b (the first 10 E4a records are reviewed before the rest are written) | QD1 | SET-P3 also gets an E1 read (`AutomationControls`) | Light: allow-list, shared lists, named tests present |
| David (Gemini), juniors | RRULE, SENS, RD-M7, M7-MRV, COMMIT-DUE, OPS-RATE, DATA-E2, DATA-FIX, DATA-E3F | **Edvard mentor** (Codex): **E1** for RRULE, RD-M7 and COMMIT-DUE; **E2** for SENS (the M7-MEX owner) and M7-MRV; **E3** for OPS-RATE and the data units | QD1 | — | Light |
| David (Gemini), juniors | OPS-RT3s | **SQ** (Codex) | QD1 | — | Light |
| SQ (Codex) | OPS-TV3, OPS-VIEWS, QA-X, QA-P3 | OPS-TV3 and OPS-VIEWS: **J1** (Claude; the semantics must match M6-TXA's checks and the send-path states). QA-X: Josh lead + Edvard lead (scenario fidelity). QA-P3: TL + Josh and Edvard leads | — | OPS-TV3: Gemini read on the fail-closed logic (the P2 #123 lesson) | **Deep review** on OPS-TV3; light on the rest |
| Team QA | QA-S6 (QJ1, Claude), QA-S7 (QE1, Codex) | QA-S6 → **SQ** + Josh lead; QA-S7 → a Josh engineer + Edvard lead | — | — | Light |
| TL | F0-C, PV6 text | The three leads (each lead checks the team's specs) + SQ | — | The owner approves the decisions and v6 | — |

**Turnaround rules** (the P2 stall from 15:35 to 22:45 must not recur):
1. On CI green the author marks the PR ready and labels it `needs-review:<slot>` (or `needs-review:<team>`) and `needs-qa`. **Peer review, QA and the Gemini read start together.**
2. **SLAs:** peer review within **2 h**; QA within **3 h** (before-merge lane); Gemini read time-boxed to **1 h** (non-blocking); TL within **1 h** after peer and QA are clean; fix-round re-check within **1 h**.
3. A missed SLA is never waited out: the TL launches the substitute (table above).
4. QA runs in parallel with the peer review. It reproduces behaviour and checks named tests and invariants; it does not re-review the peer's line-by-line work.
5. A lead may launch an extra fresh QA agent on the same brief whenever more than one PR of the lead's team has waited over 1 h for QA.

## 4. Communication

- **GitHub is the bus** (WORKFLOW §8): one issue per unit, PRs, review comments, labels and **one `P3 integration` issue**. Anything another team needs to know is written there, not only in a session. No temporary handoff files: a file on a branch is invisible until merged and goes stale.
- **Handoff notes:** when a unit is merged, blocked or stopped, the unit's author (or the author's lead) comments on the unit's issue: what is done, what is next, what others can now use (new functions with signatures), anything left open.
- **Merge posts:** one short post per merge batch on the integration issue (PRs merged, what they unblock).
- **Daily TL summary** on the integration issue, short: merges, checkpoint status, blockers, SLA breaches, owner gates due, the metrics in §7, next priorities per team. It **ends with "Nudges for the owner"**: the exact one-liners to send ("Edvard: continue", "David: continue; Gemini read on #NNN"). Codex and Antigravity sessions cannot wake themselves; Josh's Claude Code session can run a self-paced loop and rarely needs one. Leads read the latest summary at session start.
- **Questions to another team:** a comment on that unit's issue or PR, with the slot's label (`needs-review:<slot>` for reviews). The owner relays only when a session is not running.
- **Owner relay is the exception:** starting the three lead sessions, and "continue" when a nudge is listed. Everything else (what was done, what needs review, findings, rulings) is on GitHub.
- If a session cannot reach GitHub for a while, it ends with the same *done / next / needs* note as text; the owner pastes it into the TL session and the TL posts it.

## 5. Interface changes, disagreements, escalation

**Interface change request (ICR).** After CP1, any change to the contracts (schema, types, ports, codes, job names and arguments, registries, settings, flags, kinds, audit events, prompt allocation, file ownership) and any edit to a file on the **never-edit-without-an-ICR list** ([contracts/P3-interfaces.md](contracts/P3-interfaces.md) §15) needs an ICR:
1. Post a comment on the `P3 integration` issue starting with `ICR:`. Give the contract section, the proposed change, why, the affected units, and the plan for code already written.
2. The TL rules **within 2 h**; affected owners may object on the thread.
3. The ruling is posted at once and logged in [decisions.md](decisions.md) §D through the **one batched decisions PR per day**. The change lands in the requesting unit's PR, labelled `icr` and linking the ruling. A schema change becomes migration `0016`, issued by the schema steward (J1).
4. Standing rulings in WORKFLOW §6.5 (shared lists, keyboard appends, append-only registries) need no ICR.

**Disagreement between engineers or teams:** each side states the case on the issue; the TL decides and logs it. **Spec conflicts with a higher document** (AGENTS §2 precedence): the higher document wins; tell the TL, who corrects the spec. Never resolve a conflict by editing the PRD, an AGENTS §6 invariant or a TF fact.

**To the owner (through the TL), always:** anything consequential (architecture beyond the contracts, product behaviour the PRD does not fix, new external services or processors, data use, cost), any production action, any change to a safety invariant or regression test, eval spending, and any case where someone believes a rule blocks correct work.

**Stop-the-line.** Anyone who finds a possible breach of an AGENTS §6 invariant on `main`, on `int/p3` or in production posts it on the integration issue with the `owner-gate` label and tells the team's lead; the TL tells the owner at once.
- **Before R1:** no merge into `int/p3` that touches the affected path until the TL rules; a confirmed break is fixed before anything else on that path.
- **In production:** the TL proposes `autonomy.send` off (one flag; `/pause` is the instant brake) or another flag rollback at once. No further deploys until resolved.
- **During the 14-day trial:** a failed daily `trial-check`, or a wrong-recipient, duplicate or out-of-policy send, stops the line: `autonomy.send` off → fix PR to `main` → deploy go-ahead → the clock restarts (README §6).
- A Gemini read or any reviewer alleging a §6 break must back it with a failing test; the TL adjudicates.

## 6. Mentoring David's juniors

- **Units are small and closed** (PRs of about 400 lines or fewer). Each junior-grade spec has exact files at line granularity, a Forbidden list, named red-first tests with exact values, a "Shared lists you must update" section, truthfulness edge cases, an acceptance checklist that includes `git diff --stat`, stop-and-ask triggers and the coding rules repeated ([handoffs/junior-engineers.md](handoffs/junior-engineers.md)). No David unit touches the policy engine, `actions.execute`, Tx A, the gateway, consent, the privacy cascade, migrations or prompt files. Every job a David unit needs is a shell F0-R pre-registers, and every port it calls has a no-op default, so a junior writes only a handler function and never waits for a senior merge.
- **Mentors per unit** (§3): **J1** M6-CATCH, STAT-ACT; **J2** OPS-AV (mandatory); **J3** RD-DEL, SET-P3, DATA-E4a, DATA-E4b; **E1** RRULE, RD-M7, COMMIT-DUE; **E2** SENS, M7-MRV; **E3** OPS-RATE, DATA-E2, DATA-FIX, DATA-E3F; **SQ** OPS-RT3s. Named-touch-point exceptions are reviewed by the owning senior: `actions/activity.py` (STAT-ACT, J1) and `deploy/sql/nfr05.sql` (OPS-AV, J2). The TL dispatches each mentor review like any cross-team peer review.
- **Strict allow-list review.** Any file outside the allow-list, any new dependency, and any change to a contract, prompt, policy, gateway, migration, consent or privacy code is a **blocking** finding. A needed out-of-list edit goes in a **separate last commit** with an issue comment, so the TL can keep or drop it without a rebase.
- **Datasets:** the mentor reviews a sample first (the first 10 records of DATA-E4a) before the rest are written. QD1 checks label consistency per variant, runs the banned real-name scan (Latin and Cyrillic), and confirms the manifest was regenerated in **every** data commit.
- **Stop and ask:** a junior asks on the unit's issue when a spec is unclear, rather than guessing. The mentor answers within the review SLA; the TL resolves what the mentor cannot.
- **One fix round, then the mentor finishes.** A junior fixes the blocking findings in one round. If blocking non-safety findings are still open after that round (a second non-safety round would be needed), **the mentor finishes the unit**: the TL launches a fresh agent of the mentor's slot with write access to the junior's branch (a Claude subagent, or `codex exec --sandbox workspace-write` in the junior's worktree or clone; allowed, P3-T65). The mentor records what was hard, for the next spec. Non-blocking findings go to open items as for every unit. A broken safety invariant is always fixed, whoever fixes it.

## 7. Metrics

The TL tracks these in the daily summary:
- units by state (not started, PR open, merged), against the slot schedule (README §4);
- PRs waiting for peer review, QA, a Gemini read or TL review, for how long, and **SLA breaches** with the substitute launched;
- CI failures on `int/p3` pushes and on PRs;
- open blocking findings; surviving mutations on Tier-1A PRs;
- open ICRs and their age (target ≤ 2 h);
- junior units: fix rounds used, mentor finishes, out-of-list commits;
- usage-limit stops and relaunches;
- owner gates due in the next 48 h;
- eval spend so far against the P3 budget (USD 3, OD-22);
- after R1: deploys, flag steps, daily `trial-check` result, NFR-05/07/08 numbers, weekly rating counts.
