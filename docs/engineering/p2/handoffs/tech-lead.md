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

## 8. State at the end of the 2026-10-28 TL session (start here)

**Historical baseline:** this section describes the earlier session. For the resumed source work and current next steps, continue with §9 below.

**Done.** M04 is on `main` (#125, de19e05) and **deployed to production with every new M04 flag off** (backup 20261028T184105Z first; healthy). CP2 is signed off (decisions §D). All M05 units are merged into `int/m05` (327e5d0): S05.1, S05.2, S05.2P, S05.3a, S05.3b, S05.4, E3D, E3T parts 1 and 2, QA-S5. The retrieval agent-mode re-run for `owner_request.v8` passed its pre-screen (USD 0.058; #72). No role agent is running; no unit has unpushed work.

**Next, in order:**
1. **#123 OPS-TV part 2:** Antony's fix round (the trial check failed open; TL disposition on the PR) → Neryn re-check → merge into `int/m04` → a small `int/m04` → `main` PR (the TL may merge it under P2-O13).
2. **CP3 remainder (#69):** enable the M04 flags in order (WORKFLOW §10, `just prod-env-push` after the owner adds them) → the M04 live checklist in a production window on Alviro → the restore test (OPS-RT #66) → start the 7-day trial (#70) with the trial check running.
3. **Priority follow-up (P2-T53):** control texts are escaped twice (names with `&`/`<` show as `&amp;amp;`); a small gateway fix unit (Neryn), plus the multi-chunk send outcome ICR.
4. **CP4 (after CP3), Jack:** a `main` → `int/m05` sync PR: `OwnerAlerts.notify_owner` through notifications (S05.2 §3.5, system alerts take `FOR KEY SHARE`, P2-T42); `owner_request.v8` on `int/m05` → **v9** = M04's v8 + rules 18–20 (P2-T50); keep both appends in `panels.py`/`test_conversation_modes.py` (#106 vs #111); the two CP4 strict-xfail scenarios (one card per burst, the system alert) must turn green. Then CP5 (#71) and the v9 retrieval re-run.
5. **Owner gates:** E2 ratings and E3 label confirmation plus the uz/ru dataset review (#73); the E2/E3 live runs (#72); the trials; CP6 reports (#74).

**Open items** for M04.md and M05.md are in each unit PR's description and in decisions P2-T42…T54. **Workflow scripts** and the role setup: §7 above; their stable copies are in the TL's memory folder.

## 9. Resumed source work (2026-10-28)

The owner requested a safe stop after the current step, with work saved and temporary resources cleaned. The production process remains deferred until the owner initiates it. No production/server/bot action or paid evaluation ran in this resumed session. Do not request production approval as part of resuming ordinary source work.

**Completed:**

- OPS-TV #123 was fixed, independently rechecked and merged into `int/m04`; the small promotion #129 reached `main` at `f8803c8`. The verifier now rejects missing/ambiguous attempt evidence and narrowly recognizes successful deletion receipts. Current-base CI passed all nine jobs; #65 remains open for production dry-run/trial records.
- OPS-RT #130 reached `main` at `db0071c` after fresh peer and QA rechecks and nine green CI jobs. Structural verification and isolated restore tooling are ready. Review caught unsafe URL suffix replacement; the fixed launcher structurally selects `restore_test` and rejects unsupported URL shapes before child execution. #66 remains open for the live rehearsal. The merged unit worktree and local/remote branch were removed with the owner's explicit cleanup approval.
- Gateway/ICR-5 #131 (PR #133) reached `main` at `b108027`. Existing control entities are preserved without activating escaped contact markup; rejected tag pairs stay literal. Later control-chunk failures return partial/uncertain, with no complete-message id or retry hint. Durable approval-presentation claims prevent replay after uncertain delivery or a crash; late old-version completion cannot overwrite a newer card. Peer, fresh QA and TL rechecks were clear after one fix round; CI passed all nine jobs. CP4 must retain this correction and the narrow legacy OwnerAlerts delivered-or-uncertain dedupe.
- Junior checked owner-gate readiness: E2 has 150 situations; E3 has 150 bursts and 30 monitor cases. Specific real-model runs (#72), owner ratings/confirmed labels (#73), live checks and final acceptance (#74) remain pending. The earlier v8 retrieval run cost USD 0.0582; the phase cap is not per-run spending authorization.

**CP4 clean-stop checkpoint:** #132, [draft PR #134](https://github.com/Khumoyun1307/personal-ai-assistant/pull/134), branch `int-sync/main-into-m05`, saved and pushed at **`a0e4bf0b8c5652d665416794da8524ac7d06ac13`**. The unit is based on `int/m05` `327e5d0a14656c922e36fae81dcadf213a4df59d`; its merge parents are the synchronous system-notification checkpoint `3136ac200c4e818f12e6d103835ac2f72c13bef5` and exact reviewed `main` `b108027c90d493ae80bbd91ef5259cb692dc3154`. This is the one actual main merge, with all 26 conflict groups resolved and recorded in the draft PR. CP4 is not merged into `int/m05` in this run.

The checkpoint preserves shipped v8 byte-for-byte (blob `f492c9cadfba90fd6ffb38e051adf995eb7521a6`), adds v9 with the M05 rules numbered 23–25, retains both Assist and priority panel appends, wires the triage draft callback, and removes only the two CP4 strict-xfail markers after their unchanged scenario bodies passed. Author evidence: 105 final targeted tests passed; 15 synchronous system regressions also passed; mypy checked 216 source files; all five import contracts and normal gitleaks/Ruff/format hooks passed. Fresh peer/QA/TL reviews remain pending; these results are not integration approval.

[CI run 40775826448](https://github.com/Khumoyun1307/personal-ai-assistant/actions/runs/40775826448) **failed**: test shards 2 and 5 and the aggregate check are red; lint, images, deploy scripts and shards 1/3/4 passed. The draft remains unmerged. Exact failure names and concise diagnostics are recorded on #134/#132 and in the latest #43 stop summary. No implementation loop was reopened after the stop request. No full local suite was run.

The five reported failures are:

- `test_conversation_modes::test_assist_is_settable_by_tool_and_panel_and_undoable`: old M04 keyboard expectation omits the new priority row.
- `test_settings_m05_panels::{test_the_panel_shows_the_contacts_priority_ticked,test_choosing_vip_on_the_panel_sets_the_contacts_priority_and_audits_it}`: old M05 keyboard/slice expectations omit Assist or include the adjacent Delete row.
- `test_system_tasks::test_an_undelivered_alert_is_tried_again`: its unfiltered audit assertion now reads a preclaim row with no `delivered` field.
- `test_mark_uncertain::test_an_uncertain_alert_lost_after_tx_b_is_sent_by_a_later_sweep`: the old raised-channel recovery expectation conflicts with CP4's unresolved claim blocking replay. **Needs a TL safety disposition after inspecting the crash point and contracts; do not weaken an invariant/regression assertion simply to make CI green.** Preserve meaningful coverage for definite non-delivery retry and uncertain/crashed delivery suppression.

**Next TL, in order:**

1. Read the latest clean-stop summary on #43, #132 and its draft PR, then fetch the saved unit branch. Recreate an isolated author/reviewer worktree from the recorded head as needed; inspect status first and never reset/discard unexpected work. Use a separate `COMPOSE_PROJECT_NAME` (`jack-cp4` for the author).
2. Resolve the recorded CI failures in the author branch using only directly relevant short local tests, then push for the full CI gate. Run fresh independent Neryn peer and Antony QA reviews of the fixed exact CP4 head, then TL contract/safety review. Inspect the recorded conflict-resolution map, system-notification claims/locks/retries, byte-identical shipped v8, new v9, both panel appends and both CP4 scenarios. No reviewer from this run has approved CP4. Fix only accepted review blockers under the one-fix-round rule; keep every review/recheck independent of the author. Session setup follows §7 and the owner's current instructions.
3. Verify current-base CI is green before merging the CP4 unit into `int/m05` (P2-T30). Open the final integration PR as a draft for the owner when ready. Do not merge that final PR, run production or real-model evals, or mark either milestone done. The v9 retrieval rerun still needs specific owner approval before shipping.
4. Keep `triage.assist_trigger` off through the seven-day M04 trial. Resume deferred live/eval/owner gates only within their explicit authorizations. Update the interim [M04](../../../milestones/M04.md) and [M05](../../../milestones/M05.md) reports only with evidence actually obtained.

**Cleanup boundary:** preserve the unmerged CP4 remote branch and its commits. Remove only this run's verified disposable worktrees and synthetic Compose resources after their work is saved. Existing shared development resources and the separate P3 worktrees are outside this cleanup. The root checkout returns to clean `main`; no role agents or test commands should remain active at close.

The reusable scripts remain in the TL memory folder. They target the earlier Claude workflow runtime; this Codex run follows their author → fresh peer/QA → one fix round → recheck steps through role agents. Standing role/session policy in §7 is unchanged; any session-specific QA arrangement comes from the owner's current prompt.

