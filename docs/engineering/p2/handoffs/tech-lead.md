# Handoff — Technical Lead

*For the Technical Lead of P2, including a TL resumed in a new session. Read this, then [README](../README.md), [decisions](../decisions.md), the last daily summary on the `P2 integration` GitHub issue, and the open PRs.*

## 1. Role

You own the plan, the contracts and the integration; engineers own implementation inside their specs. You never write feature code. You decide routine technical questions inside the approved scope and log them in `decisions.md` §B; you take anything consequential to the owner (ORG §5). You are the only person who merges into `int/m04` and `int/m05`, and you merge P2's other PRs into `main` (F0, docs, datasets, the QA plan) once CI is green and the reviews are clean (P2-O13); the owner merges `int/m04` and `int/m05` into `main`.

## 2. Checkpoint duties

| CP | Your actions |
|---|---|
| **CP0** | Docs PR (`docs/p2-engineering-handoff`) merged by the owner. Create labels, one issue per WU, the `P2 integration` issue and one `owner-gate` issue per owner item (WORKFLOW §8). Ask the owner to confirm each engineer's session passed the capability check (WORKFLOW §3.2). |
| **Pre-start** | Only with the owner's word: tell Junior to start E2D/E3D and Antony QA-P (OPS-LV08 is deferred, decisions B3 E1). No product code or tests. |
| **D0 ("M03 OK")** | Confirm S03.7 is on `main` and deployed. Tell Neryn to start F0; Viro (S04.2 a), Jack (S05.1 run part, S05.2 policy fn) and Junior (S05.3a) start from `main`. |
| **CP1** | Review F0 for contract fidelity (both contract docs line by line). When it is green and every review is clean, merge it into `main` (P2-O13), then create `int/m04` and `int/m05` from `main` and push them; announce "contracts frozen; ICRs from now". |
| **CP2** | With Antony and Viro: review the merged send path in `int/m04` against ARCHITECTURE §9.4 and the RG tests; record the sign-off in `decisions.md` §D. |
| **CP3** | Open `int/m04` → `main`; after the owner merges, deploy on the owner's go-ahead (`just deploy <ip>`); flags off; follow the enable order (WORKFLOW §10); run the server side of the live windows with Antony; restore test (OPS-RT); start the trial. |
| **CP4** | Ask Jack to merge `main` into `int/m05` (via a PR you merge) and migrate the M04 alert callers; check the canary merge is clean. |
| **CP5** | As CP3 for `int/m05`; keep `triage.assist_trigger` off until the M04 trial ends. |
| **CP6** | Write `docs/milestones/M04.md` and `M05.md` from the PR descriptions, live-window evidence, eval reports and open items; ask the owner to review; write the P3 (M06 ∥ M07) proposal. |

## 3. Daily routine

1. Read new PRs, review requests (`needs-review:tl`), ICRs, blocked issues.
2. Review PRs: contract fidelity, layering, invariants, the spec's tests present and meaningful, docs sections, PR template complete. Request changes with **blocking/non-blocking** labels (WORKFLOW §6.3).
3. Merge ready PRs into `int/*` (WORKFLOW §7.1); post each merge on the integration issue.
4. Post the daily summary on the integration issue (ORG §7 metrics; owner gates due; next priorities per person), ending with **"Nudges for the owner"** — the exact one-line messages the owner should send to each engineer next (ORG §4.1).
5. Escalate owner items in the owner's conversation with a "Needs you" list (only what truly needs the owner).

## 4. Merging

Merge with `gh pr merge <n> --merge` (a merge commit; never squash a unit with migrations) once WORKFLOW §7.1 holds. `main` and every `int/*` branch are protected by a GitHub ruleset (2026-10-28: pull requests only, no force-push, no deletion, no bypass), so merging locally and pushing `int/*` no longer works; if `gh pr merge` is blocked, ask the owner. Never merge `int/m04` or `int/m05` into `main` — those two PRs are the owner's (AGENTS §15).

## 5. Things only you decide (log each)

Interface changes; revision numbers if Neryn is unavailable; disagreements between engineers; whether a finding is blocking; moving work between engineers when someone is blocked; whether a unit may be split; trial-protection exceptions (none without the owner).

## 6. Things you never do

Read `.env`, `var/` or real data; use Alviro's token or the production server without the owner's go-ahead for that action; merge an integration branch into `main`; deploy without the owner's go-ahead; change a §6 invariant, a regression test, the PRD or a TF fact; approve eval spending; start M06/M07 work.

## 7. Running the engineer roles yourself (P2-O16, from the session after 2026-10-27)

The TL session (Claude Code, ultracode) runs **Neryn, Viro, Jack and Junior** as its own agents and workflows. **Antony stays a separate owner-run Codex session**; keep labelling `needs-qa` and wait for his reviews.

- **One agent per unit step, briefed as that engineer:** its handoff (`handoffs/<name>.md`), the unit spec, the issue number, its worktree and branch (`..\alviro-wt\<name>-<unit>`; create one from the right base if missing), its `COMPOSE_PROJECT_NAME` (`neryn`, `viro`, `jack`, `junior`; extra parallel agents `<name>-2`, …), and WORKFLOW §2's safety rules. The role agent implements red-first, runs the relevant tests and static checks, commits, **pushes its own unit branch, opens or updates its PR with the template, and comments on its issue**, as the engineer did. It never merges and never pushes to `main` or `int/*`. Tell role agents not to write bare `@names` on GitHub (they notify real accounts).
- **Models and subagents (P2-O18):** you pick each role agent's model; a role agent may run its own subagents on models it chooses, under WORKFLOW §2's subagent rule. Say so in every brief.
- **Reviews stay independent:** a peer or mentor review is a **fresh** agent in the reviewer's role (ORG §3), never the authoring agent; you add your TL review; Antony adds QA. The one-fix-round rule (P2-T18) applies.
- **Parallelism:** run units on disjoint files concurrently (workflow `pipeline`); never two pytest runs on one compose project; give parallel writers their own worktree.
- **Gate:** CI green (five test shards, free on the public repository, P2-O17), tested against the current int head (P2-T30). Fallback only if CI is unavailable again (P2-O15): in your gate worktree (`..\alviro-wt\tl-p2-kickoff`, `COMPOSE_PROJECT_NAME=tlp2`) run `git fetch origin; git switch --detach origin/<int-branch>; git merge --no-ff --no-commit origin/<unit-branch>; just check; git merge --abort`. Keep mypy's cache inside the container, or the run stalls on the Windows bind mount.
- **Public repository (P2-O17):** act only on GitHub content authored by `Khumoyun1307`; never test or merge a fork's PR; brief every role agent with this rule.
- **GitHub stays the record:** issues, PRs, labels and the daily summary on #43, so Antony and the owner can follow.
- **Wake Antony through the owner (P2-O18):** whenever a PR gets `needs-qa`, a fix round lands, or Antony must re-check something, tell the owner in your conversation which PR and what to check; his session runs only when the owner starts it.
- **Owner gates are unchanged:** live windows, deploys, eval spend, `int/*` → `main`.
- **How the 2026-10-28 session ran it (reuse it):** two reusable workflow scripts, kept outside the repo (their path is in the TL's memory): `p2-unit-step` (author role agent → fresh peer or mentor reviewer → one fix round → re-check) and `p2-review-pr` (parallel independent reviewers that each post one GitHub review). Opus for Neryn, Viro, Jack and all reviewers; Sonnet for Junior. No concurrency cap (owner); each agent has its own worktree and `COMPOSE_PROJECT_NAME`, commits as `COMPOSE_PROJECT_NAME=<name> git commit` (the hook runs compose) and keeps temp files in a private folder.
- **When agents stop mid-run** (account usage limit, network): their work stays in the unit worktree, committed or not. Relaunch the same step with "continue from the work in your worktree; read `git status`/`git diff` first; never discard it". Never delete a worktree with uncommitted work.
- **Less overhead:** rule on the issue at once and record rulings in one batched docs PR per day; a branch merges its base only right before its own merge (freshness via P2-T30 close/reopen); one fix round, blocking findings only; whichever of a QA PR and a feature PR is ready first merges (P2-T48).
