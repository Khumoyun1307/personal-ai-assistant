# P3 QA folder

Placeholder. **QA-P3** (owner slot **SQ**, Senior QA, cross-team; branch `p3/qa-p3` → `main`, before D0) delivers the phase-P3 test plan, regression matrix, live checklists and trial-log template here:

| File (planned) | Content | Unit |
|---|---|---|
| `test-plan.md` | Scope, layers, who checks what, entry and exit criteria per checkpoint, the scenario packages QA-S6, QA-S7 and QA-X with the units that turn each scenario green, and the `P3-nn` marker scheme | QA-P3 |
| `regression-matrix.md` | Every AGENTS §6 invariant and RG test (RG-13/14/15/16 new; RG-08/09/11/17/18/19/20/21 extended) mapped to its named test, its owning unit and the scenario that covers it end to end. The scenario column cites the IDs listed in the "Named tests" section of [QA-S6](../specs/QA-S6.md), [QA-S7](../specs/QA-S7.md) and [QA-X](../specs/QA-X.md) (for example RG-13 → B11, RG-14 → B12, B13, B15, RG-15 → K3, RG-16 → K4, RG-19 → C1, C2, C5, C7, C8, C14, C15, RG-20 → F2, G2, G6, G11, H2, H4, N4); every invariant that has no end-to-end scenario says which integration test carries it and why | QA-P3 |
| `live-checklist-M06.md` | Owner-run live checks for M06: shadow window, J5 (with the M05 §7 burst test), `/pause` timed from the command, the button and the stop word; expected content-free evidence | QA-P3 |
| `live-checklist-M07.md` | Owner-run live checks for M07: J7 legs c → b → a, reminders across a restart, the export inspection (owner only), memory review cards | QA-P3 |
| `trial-log-template.md`, `evidence-template.md` | The 14-day trial log (dates, flags on, daily `trial-check` result, counts and error classes, owner notes) and the evidence template; never message content | QA-P3 |
| `eval-ops-runbook.md` | How GATE-RUN starts, records and reports each S06.5 run | QA-P3 |
| `p3-verifier.md` | How `p3-verify trial-check` fails closed and supersedes the P2 verifier for `delegation_auto` | OPS-TV3 |
| `evidence/` | Content-free evidence of deploys, live windows, the restore check and the trial | LIVE-P3 |

Until QA-P3 lands, the scenario specs are [QA-S6](../specs/QA-S6.md), [QA-S7](../specs/QA-S7.md) and [QA-X](../specs/QA-X.md), and the P2 material in `../../p2/qa/` is the format reference.

## Scenario packages

Three packages under `tests/scenarios/`, each written strict-xfail first (`reason="awaiting <unit id>"`), merged into `int/p3` by D+1.5 and fully written (no body-less skeletons):

| Package | Spec | Owner | Scenarios | Covers |
|---|---|---|---|---|
| `m06/` | [QA-S6](../specs/QA-S6.md) | QJ1 | A1–A14, B1–B18, C1–C17, D1–D4, E1–E6, F1–F4, the J5 journey, helper self-tests, hygiene | Every IMPLEMENTATION_PLAN §8 named test (S06.1–S06.4), the RG-13, RG-14 and RG-19 scenario parts, J5, J8, J9, J10, J12 |
| `m07/` | [QA-S7](../specs/QA-S7.md) | QE1 | G1–G13, H1–H10, J1–J9, K1–K9, N1–N4, L1, helper self-tests, hygiene | Every IMPLEMENTATION_PLAN §9 named test (S07.1–S07.4), the RG-15, RG-16 and RG-20 scenario parts, J7 with both outcomes and the window-closed path |
| `p3_cross/` | [QA-X](../specs/QA-X.md) | SQ | X1a–X7a (X4a–X4c, X5a–X5c), helper self-tests, hygiene | Delegation + nudge, takeover vs nudge, `/pause` vs reminders, deletion across delegations, tasks and automations (R1 #7), restore with a delegation, consent revoke and Ignore mode across both teams |

Rules every package follows (the specs hold the detail):

- **A scenario is green only through a unit.** The unit named in "Turns green" is the last unit the scenario needs. A scenario that passes on the CP1 base is unmarked.
- **Marker removal (P3-T86).** A feature PR never edits a scenario package. When a strict XPASS turns a feature PR red, the package's QA owner (QJ1, QE1 or SQ) pushes one commit to the feature unit's branch that removes exactly the markers that pass. Nobody deletes or weakens a scenario.
- **Marker audit.** SQ runs the audit of QA-X §3.8 before CP-A and before R1: no scenario stays marked for a merged unit; a failing §6 behaviour is blocking.
- **Positive controls.** A scenario that asserts an absence also asserts a positive outcome, so it cannot pass vacuously.
- **Content-free evidence.** Assertions and failure messages use ids, states, codes and counts; evidence lists scenario names and outcomes only.

Rules: no file here ever contains real chat content, names, usernames, production ids or secrets (AGENTS §9). Only the TL or SQ starts a live run, and only with the owner's go-ahead for that run.
