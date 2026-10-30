<!-- Delete the section that does not apply. P3 (int/p3) PRs use the first; P2 tail PRs use the second. -->

# P3 (int/p3) PRs

## Unit
WU: <UNIT-ID> · Spec: docs/engineering/p3/specs/<UNIT-ID>.md · Issue: #<n> · Base: int/p3 | <stack base> | main
Tier: [ ] Tier-1A  [ ] other · QA lane: [ ] before merge  [ ] after merge

## What changed
<short; reference spec sections>

## Safety invariants touched (AGENTS §6)
- [ ] none  - [ ] 1 policy before send  - [ ] 2 no direct-send tool  - [ ] 3a/3b untrusted content
- [ ] 4 isolation  - [ ] 5 control/business boundary  - [ ] 6 at-most-once  - [ ] 7 fail closed
- [ ] 8 Telegram rules  - [ ] 9 consent  - [ ] 10 kill switch  - [ ] 11 honest status
- [ ] 12 tenant scoping  - [ ] 13 AI disclosure  - [ ] 14 forbidden capabilities
For each ticked box: the test that proves it still holds.

## Tier-1A mutation table (Tier-1A only; the author fills the first four columns, QA the last)
| # | Rule, lock or guard added | Mutation (removed or inverted) | Named test expected to fail | Killed? (QA) |
|---|---|---|---|---|
| 1 | | | | |
One mutation per added rule, lock or guard. A surviving mutation is blocking.

## Tests
- Added (red first): <names>
- Relevant tests run locally: <command + result>, or "not run locally: <reason>; CI is the gate"
- Regression tests (RG-xx) added or extended: <list>
- Strict-xfail markers removed (scenarios now passing): <list or none>

## Shared lists updated (WORKFLOW §6.5)
<list, or none>

## Data
- [ ] Migration (only J1 issues revision numbers; up/down tested; data-handling note in the commit)
- [ ] New content column (registry + ARCHITECTURE §10.4 + sweep + cascade + tests in this PR)
- [ ] Logs stay content-free (`test_log_hygiene_<unit>.py`)

## Prompts, flags and settings
- Prompt key and version: <none, or key + new version + eval re-run needed>
- New flags (default off) / settings (in `.env.example`): <list or none>

## Docs
- ARCHITECTURE sections updated: <list or none>
- Decisions for the milestone report: <text or none>

## Interface changes
- [ ] none  - [ ] approved ICR: <link to the ruling>
- Out-of-list edits (separate last commit): <commit + reason, or none>

## Open items for M06.md / M07.md
<non-blocking findings not fixed, with evidence and a suggested fix>

---

# P2 tail PRs

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
- [ ] Migration (number from the schema steward; up/down tested)
- [ ] New content column (registry + ARCHITECTURE §10.4 + sweep + cascade + tests in this PR)
- [ ] Logs stay content-free

## Docs
- ARCHITECTURE sections updated: <list or none>
- Decisions and test list for the milestone report: <text or none>

## Interface changes
- [ ] none  - [ ] approved ICR: <link>
