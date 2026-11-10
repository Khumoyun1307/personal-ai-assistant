# I1 triage evidence candidate — evaluation pending

Synthetic E3 bursts dataset v1.1 changes `b037` (older completion of a different obligation) and `b039` (injected closure with an outstanding task). No real Telegram data. The frozen manifest is updated; there are still 150 bursts, now 41 task decisions (20 resolved, 21 unresolved).

Candidate: `triage.v2`, `TriageResultV2`, route/model unchanged, `triage.resolution_evidence` enabled. The E3 target selects this candidate explicitly and records its prompt label. Production defaults to the archived v1 prompt/schema while the flag is off. No provider call, paid eval, language review or quality pass was performed for I1.

Before enabling the flag, obtain run-specific owner spend approval, confirm the changed labels, run the full affected triage eval through the existing E3 runner, and record cost, exact configuration and results. Historical v1 results are not evidence for v2. The A1 conversation gate (J17 extension) and owner playtest remain required under D-S16.
