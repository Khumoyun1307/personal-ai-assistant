# Handoff — Junior engineers (D1, D2)

*Start here, and come back to this page whenever you are unsure. You are D1 or D2, a junior engineer on David's team. Your units are small and exactly specified. Follow the spec literally; when it does not say, ask — do not guess.*

## 1. Your role

You build bounded pieces of P3: synthetic eval data, pure helpers, read tools, control-chat screens and cards, and ops SQL and tooling. Each unit has a **mentor** from another team (a Josh or Edvard engineer, or Senior QA) who reviews your PR strictly, and **QD1**, who checks it. David is your lead and launches you.

The foundation does the hard wiring for you: every job your units need is a shell that F0-R pre-registers, every port you call has a no-op default, and new screens and status blocks plug into registries. You write a handler, a module, a renderer or data, and register it on the lines your spec names.

## 2. Read first, in order

1. [AGENTS.md](../../../../AGENTS.md) — all of it. §6, §8 ("evaluation datasets are synthetic or hand-written only") and §9 ("coding agents never read real Telegram content") are absolute.
2. [WORKFLOW](../WORKFLOW.md) §2 (safety), §3 (your workspace), §4 (branches), §5 (tests), §6 (done, review), §9 (datasets).
3. [README](../README.md) §3 (your rows in the index) and [ORG](../ORG.md) §6 (mentoring).
4. The spec of the unit you are about to do. Each has **Allowed files**, **Forbidden**, **Shared lists you must update**, **Step-by-step**, named tests with exact values, **Truthfulness edge cases**, an **Acceptance checklist** and **When to stop and ask**. Read the whole spec before starting.
5. The existing code the spec tells you to copy patterns from.

## 3. Your work, in order

Units go on `p3/<unit-id>` from `int/p3` → `int/p3`. Before CP1 (D+1) a code unit stacks as a draft on the F0 branch the spec names and is retargeted after the merge (P2-T31). Pre-start data goes from `main` → `main` (WORKFLOW §4).

**D1**

| # | Unit | Starts when | Mentor | Spec |
|---|---|---|---|---|
| 1 | DATA-E4a — E4 v1.1 part a (a081–a140) | Only when David says the owner allowed pre-start work | J3 (your first 10 records, before you write the rest) | [DATA-E4a](../specs/DATA-E4a.md) |
| 2 | STAT-ACT — `/status` and `/activity` P3 blocks | D0 | J1 | [STAT-ACT](../specs/STAT-ACT.md) |
| 3 | M6-CATCH — end-of-delegation catch-up builder and renderer | D+1 | J1 | [M6-CATCH](../specs/M6-CATCH.md) |
| 4 | DATA-FIX — the owner's native-review corrections; v1.2 by D+3 | As the owner's review lands | E3 | [DATA-FIX](../specs/DATA-FIX.md) |
| 5 | RD-DEL — `list_delegations` read tool | D+2 | J3 | [RD-DEL](../specs/RD-DEL.md) |
| 6 | SET-P3 — `/settings` P3 screens | D+2..D+3 | J3 (plus an E1 read) | [SET-P3](../specs/SET-P3.md) |
| 7 | OPS-RATE — weekly usefulness rating (Sunday 19:00 owner-local, at most one per ISO week) | D+4 | E3 | [OPS-RATE](../specs/OPS-RATE.md) |

**D2**

| # | Unit | Starts when | Mentor | Spec |
|---|---|---|---|---|
| 1 | DATA-E2 — E2 v1.1 label fixes and additions | Only when David says the owner allowed pre-start work | E3 | [DATA-E2](../specs/DATA-E2.md) |
| 2 | DATA-E4b — E4 v1.1 part b (a141–a200) | DATA-E4a merged | J3 | [DATA-E4b](../specs/DATA-E4b.md) |
| 3 | RRULE — recurrence mapper and local-time echo | D0 | E1 | [RRULE](../specs/RRULE.md) |
| 4 | OPS-AV — availability recorder and NFR SQL | D0..D+1 | J2 | [OPS-AV](../specs/OPS-AV.md) |
| 5 | SENS — sensitive-category filter | D+1..D+2 | E2 | [SENS](../specs/SENS.md) |
| 6 | COMMIT-DUE — due-commitment handler | D+2 | E1 | [COMMIT-DUE](../specs/COMMIT-DUE.md) |
| 7 | RD-M7 — `list_tasks` and `list_automations` read tools | D+2..D+3 | E1 | [RD-M7](../specs/RD-M7.md) |
| 8 | M7-MRV — memory review cards | D+4 | E2 | [M7-MRV](../specs/M7-MRV.md) |
| 9 | OPS-RT3s — restore-test P3 check script | D+4 | SQ | [OPS-RT3s](../specs/OPS-RT3s.md) |
| 10 | DATA-E3F — E3 follow-up-style monitor cases | D+5 | E3 | [DATA-E3F](../specs/DATA-E3F.md) |

If your pre-start data is not finished at D0, do RRULE, OPS-AV and STAT-ACT first (other units depend on them), then return to the data.

## 4. Strict boundaries

- **Edit only the files your spec lists under "Allowed files"**, at the granularity it gives ("one handler line in `_wire_surfaces`" means exactly that). Creating a file not in the list, editing any other file, adding a dependency or changing a contract is a blocking review finding.
- **Never touch:** `policy/`; `actions/` (except the `Authorization` touch point in `actions/activity.py` that STAT-ACT names); `telegram/gateway.py`; `telegram/ingress/`; `migrations/`; `agent/prompts/` and `agent/prompt_registry.py`; `privacy/`; consent code (`llm/consent.py`); `config.py`; `core/`; `jobs/`; `bootstrap.py` except your registration lines in `_wire_surfaces`; `agent/runtime.py`, `agent/context.py`, `agent/schemas.py`, `agent/tools/executor.py`; `inbox/notification_policy.py`, `inbox/notifications.py`, `inbox/triage_store.py`; `AGENTS.md`; any `docs/` file your spec does not name; E1 and the retrieval dataset.
- **Use the seams, never rebuild them.** For a pre-registered job shell (COMMIT-DUE, OPS-RATE) write only the handler function. For a port with a no-op default, hide your row or fail closed when it is not wired; never wire it yourself.
- **Datasets:** every name, number, address, company and message is invented by you. Never copy text from anywhere real, never paste anything from a real chat. The owner reviews Uzbek and Russian quality later; do your best.
- **Never read** `.env`, `var/`, backups, or database rows with message text. Never run anything against the production bot or server. Never start a live eval (SQ or the TL do that, with the owner's go-ahead).
- **Never run `python`, `uv`, `pytest` or `alembic` on the Windows host**, not even for a one-off text edit (AGENTS §4). Edit files with your editor tools; run Python only in the dev container (`just test …`, `docker compose -f compose.dev.yml run --rm app …`).
- Never push to `main` or `int/p3`, never merge, never edit someone else's branch.

## 5. How to work on a unit

1. **Set up once** (WORKFLOW §3): your own worktree or clone, `$env:COMPOSE_PROJECT_NAME = 'd1'` (or `'d2'`), `just setup`, `just hooks`. In your first session run the capability check and tell David if anything fails.
2. **Start:** comment on the unit's issue that you started.
3. **Follow the Step-by-step in order.** After each step, do its check (usually: run the named test file and see it pass or fail as the step says).
4. **Red first:** write each named test, run it, and see it fail for the right reason (an assertion or `NotImplementedError`, not an import error); then make it pass. You may add tests, never fewer.
5. **Test only your unit:** its tests plus `ruff`, `ruff format --check` and `mypy` on your files (commands in WORKFLOW §5). Never the full suite or `just check`.
6. **Commit at each coherent step** with `git commit -F <message file>` (Conventional Commits) and push. Open a draft PR with the template; fill every section honestly ("not run locally: <reason>; CI is the gate" if your session cannot run Docker).
7. **Check your diff:** `git diff --stat <base>...HEAD` shows only allowed files. Tick the spec's Acceptance checklist in the PR description.
8. **When CI is green:** mark the PR ready, label it `needs-review:<your mentor's slot>` (for example `needs-review:j3`) and `needs-qa`, and comment on the issue.
9. **Fix blocking findings in one round,** push, and comment "fixes pushed: <commits>". If blocking findings remain after that round, your mentor finishes the unit; stop when told to.

**Data units, in addition:**
- Sample first: put your first 10 records in the draft PR and wait for the mentor's comment before writing the rest.
- Regenerate the manifest in **every** data commit (`python -m evals.manifest --write` in the container, after rebasing on the current base).
- Before each push: scan for real names (brands, platforms, banks, telcos, cities) in Latin and Cyrillic; check that labels agree within every translated variant (for example `must_not` against `safe_behaviour`); canaries are natural invented details, never literal `CANARY` markers.

## 6. When to stop and ask

Ask on the unit's issue, naming your mentor slot in plain text ("Question for J3:"), when:
- the spec seems to need a file that is not in "Allowed files";
- a named test cannot pass without changing something outside your files;
- an exact-set test fails that the spec's "Shared lists you must update" section does not name;
- two parts of the spec, or the spec and AGENTS.md or a contract, disagree;
- a port, job shell or notification kind the spec relies on does not exist on your base;
- you are not sure whether something is real data;
- you have spent more than the unit's size and are not close.

If you already made an out-of-list change, keep it in a separate last commit, explain it on the issue and wait for the ruling. If nobody answers within 2 h, David raises it with the TL.

## 7. Coding rules reviewers block on

- **No broad `except`** (no bare `except`, no `except Exception`): catch the specific exceptions your spec names.
- **No `assert` in production code**, neither for control flow nor for type narrowing: use `if …: raise <SpecificError>(…)` or a typed check.
- **Names exactly as the spec's API:** functions, parameters, keywords, enum values. Full type hints; `mypy --strict` clean.
- **No `datetime.now()`:** take the `Clock` or `now` the spec gives. Store and compare in UTC; use the owner's timezone only for display.
- **Content-free logs:** ids and counts only; never message text, names, usernames or captions.
- **Truthfulness:** a capped list reports the true total; "uncertain", "scheduled" and "queued" are never "sent"; "snoozed" is never "done"; a flag that is off shows "off"; a row whose port is not wired is hidden.
- **No Telegram calls:** renderers return text and buttons through the registries; nothing you write calls the Bot API or uses aiogram shortcuts.
- **GitHub:** every PR body, comment and review through `--body-file <file>`; never an inline body in PowerShell (backticks garble it); no bare `@names` (they notify real accounts); no recurring status comments.
- **Commits:** never `--no-verify`; never commit `.env`, `var/`, dumps or real data. Keep PRs to about 400 lines or fewer.

## 8. Every session

**Start:** read your unit issue's comments, review comments on your PRs (`gh pr list --label eng:<your slot>`), the latest posts on the `P3 integration` issue, and David's brief. **End:** comment on your issue — done, next, blocked — and stop test containers you no longer need (`just down`).
