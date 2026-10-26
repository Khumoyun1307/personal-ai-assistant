# Evidence template — live window / acceptance run

*Copy this block into a new section of `docs/milestones/M04.md` or `M05.md` (the TL pastes it the same day). Content-free only: ids, numbers, states, reason codes, hashes "match/no match", counts, times. Never paste message text, names, usernames or log lines that could carry content.*

```markdown
### <Run name> — <YYYY-MM-DD>

| | |
|---|---|
| Run | <M04 live window part 1 / part 2 · M05 friends' session · digest day N · restore test · LV-08 reopen · trial day N · eval run> |
| Date and time (owner's timezone) | <start>–<end> |
| Commit deployed | <main commit hash> (int merge PR #<n>) |
| Alembic head | <revision> |
| Flags on | <names only> |
| Owner go-ahead | <date/time and a short reference to the owner's message in the conversation> |
| People | Owner (clients: <phone/Desktop/second account>), TL (server), Antony (recording)<, F1, F2> |
| Chat(s) | <"main ↔ second account" / "main ↔ F1"> (no names) |

**Steps**

| # | Step (checklist ref) | Verification output (content-free) | Expected | Result |
|---|---|---|---|---|
| 1.1 | <ref> | <e.g. action 812: awaiting_approval v1 agent_proposal> | <…> | PASS / FAIL |
| … | | | | |

**Summary checks**

- `trial-check --since <start>`: <PASS/FAIL, counts>
- `p2-verify script <prefix> --expect <range>`: <all present / missing numbers>
- Spend (eval runs only): <USD> of the P2 USD 2 cap (running total <USD>)

**Deviations** — anything done differently from the script, and why.

**Failures and follow-ups**

| # | What failed | Evidence (ids, states, timeline from `p2-verify action <id>`) | Severity (stop-the-line / blocking / open item) | Owner of the fix | Issue |
|---|---|---|---|---|---|

**Sign-off** — Antony (recorded) · TL (reviewed) · Owner (accepted / not yet)
```
