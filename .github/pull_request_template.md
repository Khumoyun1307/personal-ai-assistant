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
