# P2 workflow — how work moves from spec to production

*Binding for every engineer and every subagent an engineer starts. It adds P2 specifics to AGENTS.md; AGENTS.md still applies in full. If anything here conflicts with AGENTS.md, AGENTS.md wins and you tell the TL.*

## 1. Before you write code

1. Read [AGENTS.md](../../../AGENTS.md) completely (§6 invariants, §9 data rules and §11 test rules are where mistakes hurt most).
2. Read this phase's [README](README.md), [ORG](ORG.md), this file, both [contracts](contracts/), your [handoff](handoffs/) and your unit's spec.
3. Check the **Definition of Ready** (§6.1). If it is not met, say so on the unit's issue and work on something that is ready.

## 2. Safety rules (apply to you and to every subagent you run)

These are the rules most likely to be broken by accident. Repeat them in every brief you give a subagent.

- **Never read real data or secrets.** Do not open, print, grep, cat, `type` or `Get-Content` `.env`, `var/`, backups, probe dumps, or database rows holding message content; do not run `docker compose config`, `env` or `printenv`. Permission to *run* a command is never permission to *read* content (AGENTS §9). Codex and Gemini sessions have no guard hook for this: you are the guard.
- **Never use the production bot (Alviro) token or the production server** unless your unit's ops spec says so *and* the owner gave a per-run go-ahead in the conversation. Never call `getUpdates` with any token from a laptop while another process polls it.
- **No real Telegram or real model calls in tests.** Use `tests/fakes/` (AGENTS §11). Live evals only as WORKFLOW §11 says.
- **Never weaken or delete a regression test** (IMPLEMENTATION_PLAN §10 RG-01…RG-21) or a test guarding an AGENTS §6 invariant. If one fails, the code is wrong until the TL says otherwise.
- **Never run the full test suite or `just check` locally** (AGENTS §11): CI is the gate.
- **Never `--no-verify`, never rewrite pushed history, never push to `main` or an `int/*` branch, never merge** (the TL merges unit PRs and P2's other PRs into `main`; the owner merges `int/m04` and `int/m05` into `main` — P2-O13).
- **No sends as the owner outside `actions.execute` + policy**; no aiogram shortcuts; no `drop_pending_updates=True`; no tool that sends, edits, deletes or marks read (AGENTS §6, §7, §17).
- **Subagents** work only inside your unit's file boundaries, in your worktree/clone or in their own worktree branched from yours, each with its **own `COMPOSE_PROJECT_NAME`** (two pytest runs must never share a compose project). They do not push, merge, open PRs or touch the production server. You review every line they produce before it goes into your PR; you are accountable for it.

## 3. Your workspace

### 3.1 Worktree or clone, and a compose project of your own

Every engineer works in their own directory with their own branch and their own Docker Compose project (so your database and tests never collide with anyone else's, ARCHITECTURE §15.1).

```powershell
# From the main checkout (never switch branches there: other sessions use it).
git fetch origin
# A worktree (Claude sessions; any tool that can write the shared .git):
git worktree add ..\alviro-wt\<you>-<unit> -b <branch> origin/<base>
# OR a separate clone (if your tool's sandbox cannot write the shared .git):
git clone https://github.com/Khumoyun1307/personal-ai-assistant ..\alviro-<you>
cd ..\alviro-<you>; git switch -c <branch> origin/<base>

# In your directory, every session, before any just/docker command:
$env:COMPOSE_PROJECT_NAME = '<you>'        # neryn | viro | jack | junior | antony (subagents: <you>-2, <you>-3 …)
just setup                                  # first time: placeholder .env from .env.example, image, dependencies
just hooks                                  # pre-commit hook (gitleaks + ruff in the container)
```
The `.env` that `just setup` creates holds placeholders only; you never need real values (tests use fakes). Never copy the owner's `.env`. Never `git checkout` another branch in a directory where a container run is in progress (the container bind-mounts the tree).

### 3.2 Session capability check (P2-O12)

At the start of your first session, check that your environment can do what this workflow needs, and post the result on your first unit's issue:

| Capability | Check | Needed for |
|---|---|---|
| Docker reachable | `docker version` and `just test tests/unit/test_clock.py` | Running relevant tests locally |
| Commit in your directory | a throwaway commit on your branch, then `git reset --soft HEAD~1` | Committing your work |
| Push and GitHub CLI | `git push -u origin <branch>` (empty branch is fine) and `gh auth status` | Draft PRs, CI, issues, reviews |
| Read the repo's docs | open this file | — |

If something fails, **tell the owner** in your session ("my session cannot reach Docker / commit / push"); the owner fixes it. Do not work around a missing capability (for example by asking another engineer to commit for you) unless the owner tells you to. Until it is fixed, you may continue writing code and leave it uncommitted, and say so on the issue.

## 4. Branches and pull requests (P2-O3)

| Branch | Created from | PR base | Who merges |
|---|---|---|---|
| `p2/f0-foundation` | `main` (after "M03 OK") | `main` | TL (CP1, P2-O13) |
| `int/m04`, `int/m05` | `main` right after F0 merges | `main` (once per milestone) | Owner (CP3, CP5) |
| `m04/<unit>` | `int/m04` | `int/m04` | TL |
| `m05/<unit>` | `int/m05` | `int/m05` | TL |
| `evals/e2-dataset`, `evals/e3-dataset` | `main` | `main` if merged before CP1, otherwise `int/m04` / `int/m05` | TL |
| `evals/e4-dataset`, `qa/p2-plan`, `ops/lv08-probe` | `main` | `main` | TL (P2-O13) |
| `qa/m04-scenarios`, `ops/trial-verifier` | `int/m04` | `int/m04` | TL |
| `qa/m05-scenarios` | `int/m05` | `int/m05` | TL |

- **Depending on an unmerged unit:** branch from that unit's branch and set your PR base to it; after it merges into the int branch, rebase onto the int branch and retarget your PR. Never merge someone else's branch into yours.
- **Units started before CP1** (S04.2 part a, S05.1 run part, S05.2 policy function, S05.3a, the datasets): branch from `main`; at CP1 rebase onto the int branch and open the PR against it. To get CI before CP1, open a **draft PR against `main`** titled `[retarget to int/m0x]`; the TL retargets it at CP1 and it is never merged into `main` (P2-T31).
- **Draft PR as soon as the unit is ready for CI**, with the template (§4.1). Mark it ready for review when the Definition of Done is met. Push to keep CI running; avoid pushing every few minutes (each push costs a full CI run: lint, three parallel test shards, the deploy-script checks and the amd64/arm64 image builds).
- **Commits:** small, Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`), message via `git commit -F <file>` when it has more than one line, ending with your tool's attribution line if it uses one. Never commit `.env`, `var/`, dumps or real data.

### 4.1 Pull request template (`.github/pull_request_template.md`)

```markdown
## Unit
WU: <id> · Spec: docs/engineering/p2/specs/<file> · Issue: #<n>

## What changed
<short; reference spec sections>

## Safety invariants touched (AGENTS §6)
- [ ] none  - [ ] 1 policy before send  - [ ] 2 no direct-send tool  - [ ] 3a/3b untrusted content
- [ ] 4 isolation  - [ ] 5 control/business boundary  - [ ] 6 at-most-once  - [ ] 7 fail closed
- [ ] 8 Telegram rules  - [ ] 9 consent  - [ ] 10 kill switch  - [ ] 11 honest status  - [ ] 12 tenant scoping
For each ticked box: the test that proves it still holds.

## Tests
- Added (red first): <names>
- Relevant tests run locally: <command + result>, or "not run locally: <reason>; CI is the gate"
- Regression tests (RG-xx) added/affected: <list>

## Data
- [ ] Migration (number from the schema steward; up/down tested)  - [ ] New content column (registry + §10.4 + sweep + cascade + tests in this PR)
- [ ] Logs stay content-free

## Docs
- ARCHITECTURE sections updated: <list or none>  · Decisions for the milestone report: <text or none>

## Interface changes
- [ ] none  - [ ] approved ICR: <link>
```

## 5. Testing

- **Relevant tests only, locally** (AGENTS §11): the files for the code you changed and the tests listed in your spec, plus `ruff check`/`ruff format --check`/`mypy` on changed packages and `lint-imports`. If the relevant set would take about 15 minutes or more, do not run it: push and let CI run it.
- Commands (in your directory, with your `COMPOSE_PROJECT_NAME`): `just test tests/unit/test_x.py tests/integration/test_y.py -q`; `docker compose -f compose.dev.yml run --rm app uv run ruff check src/assistant/actions`; `… uv run mypy src/assistant/actions`; `… uv run lint-imports`.
- **One pytest process at a time per compose project.**
- **Red first.** New behaviour and every bug fix start with a failing test that you run and see fail (AGENTS §11). Specs list the tests by name; you may add more, never fewer.
- **Determinism:** `FakeClock`, scripted `FakeLLM`, `FakeTelegramSender`; no sleeps for timing; interleaving tests use explicit barriers.
- **QA's acceptance scenarios arrive before the features** (QA-S4/QA-S5): each is marked `@pytest.mark.xfail(strict=True, reason="awaiting <WU>")`, so CI stays green while it fails and turns red the moment it starts passing. The unit that makes a scenario pass removes its marker in the same PR (strict xfail forces this). Never delete or weaken a scenario to make it pass.
- **If your session cannot run Docker** (§3.2) and the owner has not fixed it yet: write the tests, push, and let CI run them; say so in the PR. Reviewers then rely on CI.

## 6. Ready, done, review, fix

### 6.1 Definition of Ready (a unit may start when)
- Its spec exists and its dependencies are merged into its base branch (or it branches from the dependency per §4).
- Contracts it consumes are on its base branch (after CP1: on `int/*`).
- Its issue is assigned and labelled `status:ready`.

### 6.2 Definition of Done (a unit's PR may be marked ready for review when)
- Everything in IMPLEMENTATION_PLAN §1.5 (global DoD) that applies.
- Every acceptance criterion in the spec is met and every listed test exists and passes in CI.
- Relevant tests ran locally (or §5's exception is stated); ruff, format, mypy and import contracts clean on changed packages.
- No content in logs; flags default off; new settings in `.env.example`.
- ARCHITECTURE sections the spec names are updated in the same PR (your sections only, F0-interfaces §12).
- The PR description carries the decisions and test list for the milestone report.

### 6.3 Review procedure
1. The author requests reviews by labelling the PR `needs-review:<peer>` and `needs-qa` (and `needs-review:tl` comes with every PR).
2. **Peer review** (ORG §3): correctness, design, spec adherence, test adequacy. Read the diff; run only the relevant tests if needed.
3. **QA review** (Antony): invariants checklist, test coverage against the spec's list and the RG matrix, negative and race cases, log hygiene, docs.
4. **TL review**: architecture, contracts, safety; merges into `int/*` when green.
5. Reviewers post findings as PR review comments, each labelled **blocking** (an invariant, a spec requirement, a failing or missing required test, a correctness bug) or **non-blocking**. Non-blocking findings the author does not fix go to the milestone's open items.

### 6.4 One fix round (P2-T18)
After a review or an eval round, the author fixes the blocking findings in **one** round. If new non-safety findings appear after that, they go to the milestone's open items (with evidence and the suggested fix) and the unit finishes. A broken safety invariant is always fixed before merge, however many rounds it takes.

## 7. Integration (TL)

1. **Merge into `int/*`** when: CI green on the PR's head (**while CI is disabled, P2-O15: the TL's local `just check` on the exact merge result is green instead**); peer, QA and TL reviews approved; no blocking findings open; the PR is rebased on the current int branch (or merges cleanly); the PR's latest CI run tested a merge with the **current** int head — if the int branch moved since, the TL closes and reopens the PR so GitHub tests a fresh merge, and waits for green (P2-T30). Merge with a merge commit (keeps the unit's history); never squash a unit with migrations.
2. **After each merge into `int/*`:** the TL posts the merge on the integration issue; open PRs targeting that branch rebase when their author next pushes.
3. **Conflict canary at each checkpoint:** the TL does a local trial merge of `int/m04` and `int/m05` (no push), and of each int branch with `main`; conflicts are resolved in the owning unit's next PR, never in the canary.
4. **Int → `main`** (CP3, CP5): the TL opens the PR, CI runs the full suite, Antony runs the milestone's checklist items that need a merged build, the owner merges.
5. **After a milestone merges:** the TL removes merged branches' worktrees and compose projects of TL-owned work, updates local `main`, and tells engineers to do the same for theirs (`$env:COMPOSE_PROJECT_NAME='<you>'; just down-hard` in the removed directory).

## 8. GitHub coordination (P2-O4)

**Labels** (created by the TL at CP0):
```powershell
gh label create "wu" --color 5319e7; gh label create "status:ready" --color 0e8a16; gh label create "status:in-progress" --color fbca04
gh label create "status:blocked" --color b60205; gh label create "status:in-review" --color 1d76db; gh label create "status:done" --color 0e8a16
gh label create "stream:m04" --color c5def5; gh label create "stream:m05" --color bfdadc; gh label create "stream:evals" --color d4c5f9; gh label create "stream:qa" --color f9d0c4; gh label create "stream:ops" --color fef2c0
gh label create "needs-review:neryn"; gh label create "needs-review:viro"; gh label create "needs-review:jack"; gh label create "needs-review:junior"; gh label create "needs-review:tl"; gh label create "needs-qa"
gh label create "icr" --color d93f0b; gh label create "owner-gate" --color b60205
gh label create "eng:neryn"; gh label create "eng:viro"; gh label create "eng:jack"; gh label create "eng:junior"; gh label create "eng:antony"; gh label create "eng:tl"
```
**Issues:** one per WU (title `"<WU>: <title>"`, body = link to the spec and the dependency list, labels `wu`, `eng:<owner>` (P2-T29), stream, status), one `P2 integration` issue (TL posts merges, checkpoints and the daily summary), one issue per owner gate (`owner-gate`). The TL creates them at CP0 from the README index.

**Status flow:** `status:ready` → `status:in-progress` (assignee starts) → `status:in-review` (PR ready) → `status:done` (merged into its base). `status:blocked` with a comment naming the blocker and who must act.

**Every session starts with:** `gh pr list --label "needs-review:<you>"` (Antony: `--label needs-qa`) — not `review-requested:@me`, which cannot tell engineers apart on the shared account; your issues (`gh issue list --label "eng:<you>"`); the integration issue's (#43) last summary. Every session ends with a comment on your issue: what is done, what is next, anything blocked.

## 9. Datasets (E2D, E3D, E4D)

- Synthetic only; never derived from real chats (AGENTS §8; ADR-16). Invented names; no real people, phone numbers, addresses or companies.
- New files only under `evals/datasets/e2/`, `e3/`, `e4/`; never edit E1 or the retrieval set (M03 owns them).
- `evals/datasets/MANIFEST.sha256` is regenerated with `python -m evals.manifest --write` (in the container) **only after rebasing on the current base**, in the same commit as the data change, because S03.7 also changes it.
- Dataset schema models and their load-time integrity checks live in `evals/schema.py` additions owned by the target unit (E2T/E3T); the data unit validates against them before pushing.

## 10. Deployment, flags and live windows

**Deploy** (`just deploy <ip>`, ARCHITECTURE §15.4) only from `main`, by the TL with the owner's go-ahead for that deploy. Every P2 flag is off after deploy. The owner (or the TL on the owner's word) enables flags on the server's `FEATURE_FLAGS` (`just prod-env-push` path; never printed) and restarts.

**Enable order — M04 (CP3):** (1) `tool.get_activity`, `tool.pause_autonomy`, `tool.resume_autonomy`; (2) `tool.propose_message`, `tool.propose_reply` with `actions.send` still **off** ("drafts only": cards, edits and expiry work; Approve ends "not sendable" with copy-ready text, P2-T21); (3) live window part 1 (cards, edits, expiry, copy-ready); (4) `actions.send` on; live window part 2 (approve → sent, double tap, restart between claim and send, outdating, takeover); (5) `assist.drafts` on for chosen chats; trial starts.

**Enable order — M05 (CP5):** (1) `notifications.deliver` (system alerts were already on); (2) `triage.run`; (3) `tool.set_contact_priority`, `digest.daily`; (4) `monitors.run`, `tool.create_monitor`, `tool.list_monitors`, `tool.stop_monitor`; (5) `triage.assist_trigger` **only after the M04 trial ends**.

**Live windows** (P2-O7): business-chat checks run on Alviro with the owner present. Antony prepares the script ([qa/live-checklist-M04.md](qa/live-checklist-M04.md), [M05](qa/live-checklist-M05.md)) with numbered client steps (which account, which chat), the content-free verification queries (`deploy/server.sh` actions, like `verify-r0`), and the expected results; the TL runs the server-side commands; the owner does the client taps; results are recorded the same day in the milestone report. Nobody reads message content during a window; verification uses ids, counts, states and hashes only.

## 11. Evals

- Budget: USD 2 for the whole phase (P2-O8). Each live run needs the owner's go-ahead in the conversation and starts with `--estimate-only`, then `--limit 1`, then the full run.
- Live runs are started by Antony or the TL (never by a subagent), on the dev configuration, with the hard in-flight budget cap (`BudgetedLLMClient`).
- Results go to `docs/evals/<date>-<set>-<model>.md/json`; a separate pre-review (a fresh agent, read-only) follows each full run in the `e1-prereview` format; gate results count only owner-confirmed items (IMPLEMENTATION_PLAN §10.1).
- Model comparison (M05 triage and `evaluate_monitor`, IMPLEMENTATION_PLAN §7): Luna first; `gpt-6-sol` then `gpt-6-astra` only if Luna misses a threshold; record quality and cost per 100 items and the decision.

## 12. Living with M03's last changes

- M03's lead owns `M03*.md`, the M03 row of IMPLEMENTATION_PLAN §1.4, E1 and the retrieval set. Do not edit them.
- S03.7 changes `agent/runtime.py`, `agent/tools/registry.py`, `agent/tools/executor.py`, `agent/schemas.py`, `agent/context.py`, `agent/prompt_registry.py` and others. P2 code starts after S03.7 is on `main` ("M03 OK"), so you build on its final shape: read those files on `main` when you start, not this phase's descriptions of them.
- If S03.7 left something that blocks your unit, raise it on your issue; the TL talks to the M03 lead or the owner.
