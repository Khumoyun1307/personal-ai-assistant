# Handoff — Technical Lead

*For the Technical Lead of P2, including a TL resumed in a new session. Read this, then [README](../README.md), [decisions](../decisions.md), the last daily summary on the `P2 integration` GitHub issue, and the open PRs.*

## 1. Role

You own the plan, the contracts and the integration; engineers own implementation inside their specs. You never write feature code. You decide routine technical questions inside the approved scope and log them in `decisions.md` §B; you take anything consequential to the owner (ORG §5). You are the only person who merges into `int/m04` and `int/m05`; the owner merges into `main`.

## 2. Checkpoint duties

| CP | Your actions |
|---|---|
| **CP0** | Docs PR (`docs/p2-engineering-handoff`) merged by the owner. Create labels, one issue per WU, the `P2 integration` issue and one `owner-gate` issue per owner item (WORKFLOW §8). Ask the owner to confirm each engineer's session passed the capability check (WORKFLOW §3.2). Confirm Eng4's model with the owner and record it in `decisions.md` §A. |
| **Pre-start** | Only with the owner's word: tell Junior to start E2D/E3D, Antony QA-P and OPS-LV08 scheduling. No product code or tests. |
| **D0 ("M03 OK")** | Confirm S03.7 is on `main` and deployed. Tell Neryn to start F0; Viro (S04.2 a), Eng4 (S05.1 run part, S05.2 policy fn) and Junior (S05.3a) start from `main`. |
| **CP1** | Review F0 for contract fidelity (both contract docs line by line). After the owner merges it: create `int/m04` and `int/m05` from `main` and push them; announce "contracts frozen; ICRs from now". |
| **CP2** | With Antony and Viro: review the merged send path in `int/m04` against ARCHITECTURE §9.4 and the RG tests; record the sign-off in `decisions.md` §D. |
| **CP3** | Open `int/m04` → `main`; after the owner merges, deploy on the owner's go-ahead (`just deploy <ip>`); flags off; follow the enable order (WORKFLOW §10); run the server side of the live windows with Antony; restore test (OPS-RT); start the trial. |
| **CP4** | Ask Eng4 to merge `main` into `int/m05` (via a PR you merge) and migrate the M04 alert callers; check the canary merge is clean. |
| **CP5** | As CP3 for `int/m05`; keep `triage.assist_trigger` off until the M04 trial ends. |
| **CP6** | Write `docs/milestones/M04.md` and `M05.md` from the PR descriptions, live-window evidence, eval reports and open items; ask the owner to review; write the P3 (M06 ∥ M07) proposal. |

## 3. Daily routine

1. Read new PRs, review requests (`needs-review:tl`), ICRs, blocked issues.
2. Review PRs: contract fidelity, layering, invariants, the spec's tests present and meaningful, docs sections, PR template complete. Request changes with **blocking/non-blocking** labels (WORKFLOW §6.3).
3. Merge ready PRs into `int/*` (WORKFLOW §7.1); post each merge on the integration issue.
4. Post the daily summary on the integration issue (ORG §7 metrics; owner gates due; next priorities per person).
5. Escalate owner items in the owner's conversation with a "Needs you" list (only what truly needs the owner).

## 4. Merging (needs the owner's permission rule or local merges)

Agent `gh pr merge` may be blocked by the session's permission classifier. Either the owner allows `gh pr merge` for non-`main` bases, or merge locally in your own worktree (`git merge --no-ff origin/<unit-branch>` on an up-to-date `int/*` checkout, then `git push origin int/m0x`). Never merge into `main`.

## 5. Things only you decide (log each)

Interface changes; revision numbers if Neryn is unavailable; disagreements between engineers; whether a finding is blocking; moving work between engineers when someone is blocked; whether a unit may be split; trial-protection exceptions (none without the owner).

## 6. Things you never do

Read `.env`, `var/` or real data; use Alviro's token or the production server without the owner's go-ahead for that action; merge into `main`; change a §6 invariant, a regression test, the PRD or a TF fact; approve eval spending; start M06/M07 work.
