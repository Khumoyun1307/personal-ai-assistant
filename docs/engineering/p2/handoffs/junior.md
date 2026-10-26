# Handoff — Junior (junior engineer)

*Start here, and come back to this page whenever you are unsure. Your units are small and exactly specified. Follow the spec literally; when it does not say, ask — do not guess.*

## 1. Your role

You author the synthetic evaluation datasets (E2, E3, E4) and build four small, well-bounded pieces of code: the schedule maths for reminders and digests (S05.3a), the activity log (S04.5), and the M05 settings panels (S05.2P). Each has a mentor who reviews your work: **Viro** for S04.5, E2D and E4D; **Eng4** for S05.3a, S05.2P and E3D.

## 2. Read first, in order

1. [AGENTS.md](../../../../AGENTS.md) — all of it. The rules in §6, §8 ("evaluation datasets are synthetic or hand-written only") and §9 ("coding agents never read real Telegram content") are absolute.
2. [WORKFLOW](../WORKFLOW.md) §2 (safety), §3 (your workspace), §4 (branches), §5 (tests), §6 (done, review), §9 (datasets).
3. [README](../README.md) §3 (your rows in the index) and [ORG](../ORG.md) §6 (mentoring).
4. The spec of the unit you are about to do — each has **Allowed files**, **Forbidden**, **Step-by-step**, **Acceptance checklist** and **When to stop and ask** sections. Read the whole spec before starting.

## 3. Your work, in order

| # | Unit | Starts when | Branch → base | Spec |
|---|---|---|---|---|
| 1 | **E2D** E2 draft dataset (data only) | Only when the TL says the owner allowed pre-start work | `evals/e2-dataset` from `main` → `main` (or `int/m04` after CP1) | [E2 §A](../specs/E2-drafts-eval.md) |
| 2 | **E3D** E3 triage dataset (data only) | Same | `evals/e3-dataset` from `main` → `main` (or `int/m05`) | [E3 §A](../specs/E3-triage-eval.md) |
| 3 | **S05.3a** Schedule maths | "M03 OK" (the TL tells you) | `m05/s05.3a-schedule-math` from `main`; rebase onto `int/m05` at CP1 | [S05.3a](../specs/S05.3a-schedule-math.md) |
| 4 | **S04.5** Activity log | CP1 (F0 merged) | `m04/s04.5-activity-log` → `int/m04` | [S04.5](../specs/S04.5-activity-log.md) |
| 5 | **S05.2P** M05 settings panels | CP1 | `m05/s05.2p-settings-panels` → `int/m05` | [S05.2P](../specs/S05.2P-settings-panels.md) |
| 6 | **E4D** E4 adversarial dataset (optional) | When 1–5 are done | `evals/e4-dataset` from `main` → `main` | [E4](../specs/E4-adversarial-data.md) |

If the datasets are not finished when "M03 OK" arrives, do S05.3a first (Eng4's S05.3b needs it), then return to the datasets.

## 4. Strict boundaries

- **Edit only the files your spec lists under "Allowed files".** Creating a new file not in the list, editing any other file, adding a dependency or changing a contract is a blocking review finding.
- **Never touch:** `migrations/`, `policy/`, `actions/` (except `actions/activity.py` in S04.5), `telegram/gateway.py`, `telegram/control/dispatcher.py`, `agent/prompts/`, `agent/runtime.py`, `agent/tools/executor.py`, `agent/schemas.py`, `agent/context.py`, `config.py`, `privacy/`, `jobs/`, consent code, `AGENTS.md`, any `docs/` file except where your spec says, E1 and the retrieval dataset.
- **Datasets:** every name, number, address, company and message is invented by you. Never copy text from anywhere real. Never paste anything from a real chat. Uzbek/Russian quality will be reviewed by the owner later; do your best and do not worry about perfection (P2-O11).
- **Never read** `.env`, `var/`, backups, or database rows with message text. Never run anything against the production bot or server. Never run a live eval (Antony or the TL do that, with the owner's go-ahead).

## 5. How to work on a unit

1. Set up your directory once (WORKFLOW §3.1) with `$env:COMPOSE_PROJECT_NAME = 'junior'`, then run the capability check (§3.2); tell the owner if anything fails.
2. Move your unit's issue to `status:in-progress`.
3. Follow the spec's **Step-by-step** in order. After each step, do its check (usually: run the named test file and see it pass or fail as the step says).
4. Write each named test first and see it fail; then make it pass.
5. Run only your unit's tests and `ruff`/`mypy` on your files (commands in WORKFLOW §5).
6. Open a draft PR with the template; fill every section honestly ("not run locally" if your session cannot run Docker).
7. Tick the spec's **Acceptance checklist** in the PR description; label `needs-review:<mentor>` and `needs-qa`; move the issue to `status:in-review`.
8. Fix blocking findings in one round (WORKFLOW §6.4). If the mentor says to stop, stop — they finish it.

## 6. When to stop and ask (on the unit's issue, mentioning your mentor)

- The spec seems to need a file that is not in "Allowed files".
- A test the spec names cannot pass without changing something outside your files.
- Two parts of the spec (or the spec and AGENTS.md) disagree.
- You are not sure whether something is real data.
- You have spent more than the unit's size (~1.5 days) and are not close.

## 7. Every session

Start: read your issue's comments, `needs-review:junior` (if a reviewer asked you something), the TL's daily summary. End: comment on your issue — done, next, blocked.

## 8. Session notes

Run the capability check first (WORKFLOW §3.2) and report missing capabilities to the owner. Your tool may have no secret guard: you are the guard (WORKFLOW §2).
