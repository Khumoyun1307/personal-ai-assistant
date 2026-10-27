# P2 engineering organization

*Who owns what, who reviews whom, how people talk to each other and when something goes to the owner. Engineers are independent engineers in owner-controlled sessions, not the TL's subagents; each engineer organises their own internal subagents within their unit's boundaries (WORKFLOW §2).*

## 1. Roles

| Role | Person | Accountable for | Not responsible for |
|---|---|---|---|
| **Owner** | the owner | Product decisions, approvals, merges into `main`, production go-aheads, live-window client steps, eval budget, milestone acceptance | Implementation details |
| **Technical Lead** | TL (Claude session) | Specs and contracts; architecture and safety sign-off on every PR; integration merges into `int/*`; interface change requests; decision log; coordinating owner gates and live windows; daily integration summary; deploys on the owner's go-ahead; P3 plan | Writing feature code (only fixes to its own docs/specs) |
| **Send-path engineer, schema steward** | Neryn (Claude) | F0, S04.1, S04.4; Alembic revision numbers; content-column/retention/cascade correctness; ARCHITECTURE §6.5, §6.6, §6.8, §9.1–§9.4, §10, §13, §15.6 | M05 features |
| **Product-flow engineer** | Viro (Codex) | S04.2, S04.3, E2T, S05.4; both `owner_request` prompt bumps (P2-T17); ARCHITECTURE §6.7 approval-card paragraph, §7 drafting rows, §9.5; mentoring Junior on M04 units | Send-path internals (reviews them) |
| **M05 stream engineer** | Jack (Claude) | S05.1, S05.2, S05.3b, E3T; M05 model comparison; mentoring Junior on M05 units | M04 features |
| **Junior engineer** | Junior (Gemini) | E2D, E3D, E4D, S05.3a, S04.5, S05.2P — each exactly as specified | Anything outside the unit's file list (see handoffs/junior.md) |
| **QA engineer** | Antony (Codex) | Test plan, regression matrix, independent acceptance scenarios (QA-S4/QA-S5), QA review of every PR, live checklists and verification queries, OPS-LV08, OPS-TV, OPS-RT, eval run operations and pre-reviews, release verification | Feature code (QA writes tests and ops tooling only) |

The TL may be resumed in a new session from [handoffs/tech-lead.md](handoffs/tech-lead.md).

## 2. Ownership (RACI per unit)

R = does the work · A = accountable (merges or signs off) · C = consulted (reviews) · I = informed.

| Unit | Neryn | Viro | Jack | Junior | Antony | TL | Owner |
|---|---|---|---|---|---|---|---|
| F0 | R | C | C | I | C | A | merges `main` |
| S04.1, S04.4 | R | C | I | I | C | A | I |
| S04.2, S04.3, E2T | C | R | I | I | C | A | I |
| S04.5 | I | C (mentor) | I | R | C | A | I |
| S05.1, S05.2, S05.3b, E3T | C (S05.2, S05.3b) | C (S05.1) | R | I | C | A | I |
| S05.4 | I | R | C | I | C | A | I |
| S05.3a, S05.2P | I | I | C (mentor) | R | C | A | I |
| E2D, E3D, E4D | I | C (E2D/E4D) | C (E3D) | R | C | A | reviews language later |
| QA-P, QA-S4, QA-S5 | C (QA-S4) | C (QA-S4) | C (QA-S5) | I | R | A | I |
| OPS-LV08, OPS-RT | C | I | I | I | R | A | go-ahead, client steps |
| OPS-TV | C | I | I | I | R | A | I |
| CP3/CP5 deploy | C | C | C | I | R (verification) | R (deploy) | A (merge, go-ahead) |

## 3. Review matrix

Every PR gets **four reviews**: the peer below, Antony (QA), the TL, and — for `main` — the owner's merge. Peers come from a different model family where possible, so the review is not anchored on the author's own reasoning.

| Author | Peer reviewer | Second reader for safety-critical PRs |
|---|---|---|
| Neryn (F0, S04.1, S04.4) | Viro | Jack reads F0's privacy cascade; Antony runs the race tests |
| Viro (S04.2, S04.3, E2T, S05.4) | Neryn (S04.x), Jack (S05.4) | — |
| Jack (S05.1, S05.2, S05.3b, E3T) | Viro (S05.1), Neryn (S05.2, S05.3b) | — |
| Junior (all) | Mentor: Viro (S04.5, E2D, E4D), Jack (S05.3a, S05.2P, E3D) | — |
| Antony (QA-S4/S5, OPS) | The feature owner whose behaviour the tests encode | — |

Review turnaround target: start within the reviewer's next session; a review that cannot start within a day is flagged on the integration issue.

## 4. Communication

- **GitHub is the bus** (WORKFLOW §8): unit issues, PRs, review comments, labels, the `P2 integration` issue. Anything another engineer needs to know is written there, not only in a session.
- **Handoff notes:** when a unit is merged or blocked, its owner comments on the issue with: what is done, what is next, what others can now use (new functions with signatures), and anything left open.
- **Daily integration summary** (TL, on the integration issue): merges, checkpoint status, blockers, owner gates due, next priorities per person. Engineers read it at session start.
- **Questions to another engineer:** a comment on their unit's issue or PR, mentioning their label (`needs-review:<name>` for reviews). The owner relays only when an engineer's session is not running.
- **Owner relay is the exception.** Each handoff tells the engineer what to do at the start of every session, so "continue" is enough for the owner to say most of the time.

### 4.1 How it works day to day (the owner's view)

Sessions do not run all the time, so someone has to start each one: that is the owner's only relay job, and it is a one-line nudge. **Everything else — what was done, what needs review, findings, decisions — is written on GitHub**, where every engineer and the TL read it. No temporary handoff files: a file on a branch is invisible to others until merged, goes stale, and conflicts; a PR or issue comment is visible at once, linked to the code and kept.

Example loop:
1. **Owner → Neryn:** "Start F0" (later: "continue").
2. **Neryn** builds, pushes, opens the PR (template filled), labels it `needs-review:viro`, `needs-qa`, `needs-review:tl`, and comments on the F0 issue: *done / next / needs reviews*.
3. **Owner → Viro:** "do your reviews". Viro's session-start routine finds PRs labelled `needs-review:viro` and posts a GitHub review (findings marked blocking or non-blocking).
4. **Owner → Antony:** "do your QA reviews" — same, for `needs-qa`.
5. **Owner → TL:** "check the integration" (or "Neryn finished F0" — no content needed). The TL reads the PR and its reviews, reviews it, merges it into `int/*` (or asks the owner to merge F0 into `main`), and posts the **daily summary** on the `P2 integration` issue.
6. The daily summary ends with **"Nudges for the owner"**: the exact one-liners to send next ("Tell Viro: review #34", "Tell Junior: start S04.5"). The owner forwards those; each engineer reads the summary at session start and continues.

If an engineer's session cannot reach GitHub for a while, the engineer ends the session with the same *done / next / needs* note as text; the owner pastes it into the TL session, and the TL posts it on the issue.

## 5. Interface changes, disagreements, escalation

**Interface change request (ICR)** — needed after CP1 for any change to the contracts (schema, types, ports, job names/args, registries, settings, flags, file ownership):
1. Open an issue labelled `icr`: the contract section, the proposed change, why, which units are affected, and a migration plan for code already written.
2. The TL decides within a day; affected owners are mentioned and may object on the issue.
3. Approved changes are logged in [decisions.md](decisions.md) §C and land in the requesting unit's PR (or a tiny `p2/icr-<n>` PR into both int branches if both streams need it at once).

**Disagreement between engineers:** each states their case on the issue; the TL decides; the decision is logged. **Spec conflicts with a higher document** (AGENTS §2 precedence): the higher document wins; tell the TL, who corrects the spec.

**To the owner (via the TL), always:** anything consequential (architecture beyond the contracts, product behaviour the PRD does not fix, new external services or processors, data use, cost), any production action, any change to a safety invariant or regression test, eval spending, and any case where an engineer believes a rule blocks correct work.

**Stop-the-line:** anyone who finds a possible breach of an AGENTS §6 invariant on `main` or in production posts it on the integration issue with `owner-gate` and tells the owner in their session. The TL proposes `/pause` or a flag rollback at once; no further deploys until resolved.

## 6. Mentoring Junior

- Junior's units are small (≤ about 1.5 days), each with an explicit allowed-file list, a forbidden list, named failing-first tests and an acceptance checklist ([handoffs/junior.md](handoffs/junior.md)).
- The mentor (Viro for M04 units, Jack for M05 units) reviews spec adherence strictly: any file outside the allowed list, any new dependency, any change to a contract, prompt, policy, gateway, migration, consent or privacy code is a blocking finding.
- Junior asks on the issue when a spec is unclear rather than guessing; the mentor answers within their next session; the TL resolves anything the mentor cannot.
- If a Junior unit needs more than one fix round for non-safety findings, the mentor finishes it and records what was hard for the next spec.

## 7. Engineering metrics the TL tracks (in the daily summary)

Units by status; PRs waiting for review and for how long; CI failures on `int/*`; open blocking findings; open ICRs; owner gates due; eval spend so far against USD 2.
