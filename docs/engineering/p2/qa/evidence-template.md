# Evidence template — live window / acceptance run

*Copy this block into a new section of `docs/milestones/M04.md` or `M05.md` (the TL pastes it the same day). Content-free only: ids, numbers, states, reason codes, hashes "match/no match", counts, times. Never paste message text, names, usernames or log lines that could carry content.*

```markdown
### <Run name> — <YYYY-MM-DD>

| | |
|---|---|
| Run | <M04 live window part 1 / part 2 · M05 friends' session · digest day N · restore test · LV-08 reopen · trial day N · eval run> |
| Date and time (owner's timezone) | <start>–<end> |
| Commit deployed | <main commit hash> (int merge PR #<n>) |
| CI evidence (automated checks) | <run URL + tested commit; passes / failures / skips / xfails, or not applicable> |
| Alembic head | <revision> |
| Flags on | <names only> |
| Owner go-ahead | <date/time and a short reference to the owner's message in the conversation> |
| People | Owner (clients: <phone/Desktop/second account>), TL (server), Antony (recording)<, F1, F2> |
| Chat(s) | <"main ↔ second account" / "main ↔ F1"> (no names) |

**Steps**

| # | Step (checklist ref) | Verification output (content-free) | Expected | Result |
|---|---|---|---|---|
| 1.1 | <ref> | <e.g. action 812: awaiting_approval v1 agent_proposal> | <…> | PASS / FAIL / SKIPPED / BLOCKED |
| … | | | | |

**Summary checks**

- `trial-check --since <start>`: <PASS/FAIL, counts>
- `p2-verify script <prefix> --expect <range>`: <all present / missing numbers>
- Marker scope: <direction, exact expected numbers; control-only and deleted markers excluded with earlier evidence linked>
- Restart step (if run): <claim not reached / completed during graceful stop / interrupted after claim; observed action and attempt ids; link deterministic RG-05 CI evidence separately>
- M05 policy setup (if run): <actual quiet-hours preset, urgent bypass, cap, prior standard-alert count and observation interval; skipped model-dependent cases remain pending>
- NFR-05 (if run): <sample counts, triage completion p95 seconds, urgent delivery p95 seconds; no samples = not evaluable>
- Calendar checks (if run): <trial day or digest day, owner-local date, scheduled and delivered times; prior consecutive-day evidence links>
- Spend (eval runs only): <USD> of the P2 USD 2 cap (running total <USD>)
- Eval status (if run): <pre-screen / owner-confirmed; reviewed item counts and metric denominators; gate not evaluable until owner confirmation>

**Deviations** — anything done differently from the script, and why.

**Pending checks** — skipped or blocked steps, their automated counterparts and the planned live follow-up. A skipped step is never recorded as a live PASS. Record the settings restored and any test monitors stopped at cleanup.

**Failures and follow-ups**

| # | What failed | Evidence (ids, states, timeline from `p2-verify action <id>`) | Severity (stop-the-line / blocking / open item) | Owner of the fix | Issue |
|---|---|---|---|---|---|

**Sign-off** — Antony (recorded) · TL (reviewed) · Owner (accepted / not yet)
```
