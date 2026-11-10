# I1 triage evidence candidate — evaluation pending

Synthetic E3 bursts dataset v1.1 changes `b037` (older completion of a different obligation) and `b039` (injected closure with an outstanding task). No real Telegram data. The frozen manifest is updated; there are still 150 bursts, now 41 task decisions (20 resolved, 21 unresolved).

Candidate: `triage.v2`, `TriageResultV2`, route/model unchanged, `triage.resolution_evidence` enabled. The E3 target selects this candidate explicitly and records its prompt label. Production defaults to the archived v1 prompt/schema while the flag is off. No provider call, paid eval, language review or quality pass was performed for I1.

Before enabling the flag, obtain run-specific owner spend approval, confirm the changed labels, run the full affected triage eval through the existing E3 runner, and record cost, exact configuration and results. Historical v1 results are not evidence for v2. The A1 conversation gate (J17 extension) and owner playtest remain required under D-S16.

## Result — 2026-11-10 (owner approved the spend)

Full E3 run on `gpt-6-luna` with `triage.v2` (`triage.resolution_evidence` candidate): [report](2026-11-10-triage-gpt-6-luna.md), trial [report](2026-11-10-triage-gpt-6-luna-trial.md). Cost $0.0588 total (trial $0.0006, full $0.0582; worst case $0.5910 from `python -m evals run triage --estimate-only`, which calls no model).

| Metric | Threshold | Pre-screen |
|---|---|---|
| Urgent/decision recall | ≥ 90 % | 55/55 (100 %) |
| False-alert rate | ≤ 20 % | 11/66 (16.7 %) |
| False-closure rate | ≤ 10 % | 0/21 (0 %) |
| Resolution recall | ≥ 80 % | 20/20 (100 %) |
| Suspicious recall (info) | — | 14/15 (93.3 %) |

All thresholds pass on the pre-screen; consistent with the 2026-10-29 v1 run (false alerts 16.9 %, false closure 0/20, resolution 20/20). The gate column stays "not evaluable" because the owner has not confirmed the E3 labels, as for the shipped P2 triage prompt. Luna stays the route model (no higher tier needed). Decision: enable `triage.resolution_evidence` in the next batched deploy once the A1 conversation gate is green and the owner playtest passes (D-S16); confirming the E3 labels stays the owner's option, as for the shipped P2 triage prompt.
