# P3 QA folder

**QA-P3 delivered for review** (2026-10-30, issue #198). Owner slot **SQ**, Senior QA, cross-team; branch `p3/qa-p3` → `main`, before D0. These are plans and blank templates, not executed acceptance evidence. Review: TL and both senior team leads ([senior-qa §3 item 1](../handoffs/senior-qa.md#3-your-work-in-order)).

| File | Content | Unit / status |
|---|---|---|
| [test-plan.md](test-plan.md) | Layers, responsibilities, checkpoint entry/exit, every scenario's exact name and last unit, marker rules | QA-P3 — delivered |
| [regression-matrix.md](regression-matrix.md) | All 14 invariants and RG-01–21; P3 extensions, scenario IDs and C9's two real partial-delivery test names under RG-20 | QA-P3 — delivered |
| [live-checklist-M06.md](live-checklist-M06.md) | Shadow, J5, M05 §7 burst and three timed pause sources | QA-P3 — delivered |
| [live-checklist-M07.md](live-checklist-M07.md) | J7 c → b → a, restart reminders, owner-only export inspection, review cards and restore | QA-P3 — delivered |
| [trial-log-template.md](trial-log-template.md) | Soak, T0 freeze, 14 daily entries, day-7/14 NFRs, ratings and clock restarts | QA-P3 — delivered |
| [evidence-template.md](evidence-template.md) | Safe run/checkpoint evidence and eval cost addendum; no message content or production identifiers | QA-P3 — delivered |
| [eval-ops-runbook.md](eval-ops-runbook.md) | Per-run approvals/costs, pre-screen, frozen run, three QE2 checks, sheets and CP-G | QA-P3 — delivered |
| [lv16-runbook.md](lv16-runbook.md) | TL's Alviro rate retest at D+6 after shadow, before J7; tool/production allowance/run-approval gates and enable-4 guard (P3-T168) | QA-P3 — delivered; probe not run |
| `p3-verifier.md` | Fail-closed verifier and P2 supersession | OPS-TV3 — later unit |
| `evidence/`, `trial-log.md` | Actual deploy/window/restore/soak/trial results | LIVE-P3 — later unit |

The scenario specs remain [QA-S6](../specs/QA-S6.md), [QA-S7](../specs/QA-S7.md) and [QA-X](../specs/QA-X.md); the P2 material in `../../p2/qa/` is the format reference. QA-P3 does not implement tests, run Docker/tests/probes/evals, or commit/push. C9 is mapped, not a claim that GW-ESC is closed (decisions §B10 P3-T165).

## Scenario packages

Three packages under `tests/scenarios/`, each written strict-xfail first (`reason="awaiting <unit id>"`), merged into `int/p3` by D+1.5 and fully written (no body-less skeletons):

| Package | Spec | Owner | Scenarios | Covers |
|---|---|---|---|---|
| `m06/` | [QA-S6](../specs/QA-S6.md) | QJ1 | A1–A14, B1–B20 (65 IDs), C1–C17, D1–D4, E1–E6, F1–F4, the J5 journey, helper self-tests, hygiene | The IP §8 scenario tests owned by this package (S06.1–S06.4), the RG-13, RG-14 and RG-19 scenario parts, J5, J8, J9, J10, J12 |
| `m07/` | [QA-S7](../specs/QA-S7.md) | QE1 | G1–G13, H1–H10, J1–J9, K1–K9, N1–N4, L1, helper self-tests, hygiene | The IP §9 scenario tests owned by this package (S07.1–S07.4), the RG-15, RG-16 and RG-20 scenario parts, J7 with both outcomes and the window-closed path |
| `p3_cross/` | [QA-X](../specs/QA-X.md) | SQ | X1a/b, X2a/b, X3a/b, X4a/b/c, X5a/b/c, X6a, X7a (14 IDs), helper self-tests, hygiene | Delegation + nudge, takeover vs nudge, `/pause` vs reminders, deletion across delegations, tasks and automations (R1 #7), restore with a delegation, consent revoke and Ignore mode across both teams |

IP §8 S06.1's post-restore ending of delegations and named chat/contact deletion ending their delegations belong to QA-X X5 and X4 respectively; IP §9 S07.2's semantic-expectation refusal belongs to M7-FUP's unit test (FUP-1), outside QA-S7's scenario ownership.

Rules every package follows (the specs hold the detail):

- **A scenario is green only through a unit.** The unit named in "Turns green" is the last unit the scenario needs. A scenario that passes on the CP1 base is unmarked.
- **Marker removal (P3-T86).** A feature PR never edits a scenario package. When a strict XPASS turns a feature PR red, the package's QA owner (QJ1, QE1 or SQ) pushes one commit to the feature unit's branch that removes exactly the markers that pass. Nobody deletes or weakens a scenario.
- **Marker audit.** SQ runs the audit of QA-X §3.8 before CP-A and before R1: no scenario stays marked for a merged unit; a failing §6 behaviour is blocking.
- **Positive controls.** A scenario that asserts an absence also asserts a positive outcome, so it cannot pass vacuously.
- **Content-free evidence.** Assertions and failure messages use ids, states, codes and counts; evidence lists scenario names and outcomes only.

Rules: no file here ever contains real chat content, names, usernames, production ids or secrets (AGENTS §9; launch boundary). Real evals and production windows need the owner's go-ahead for each run; only TL or SQ starts evals, and TL runs all server operations. LV-16 runs on Alviro at D+6 into the owner's second-account chat after shadow and before J7, subject to the merged tool change, owner-approved AGENTS §9 production allowance and go-ahead for that run (P3-T168).

Native-language review is E2/E4 v1 by D+1 and v1.1 additions by D+2 (P3-T166); the SENS term list goes with v1 (P3-T167). Later merges are reviewed by D+3 12:00; DATA-FIX v1.2 remains due D+3 18:00 before the D+4 freeze.
