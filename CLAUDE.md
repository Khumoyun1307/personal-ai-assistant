# CLAUDE.md

All repository instructions live in AGENTS.md (shared with other coding agents). This file imports it for Claude Code compatibility and adds one Claude-only section: model and effort routing.

@AGENTS.md

## Model and effort routing (Claude Code only)

Owner decision 2026-10-29. When you delegate work (Agent tool `model`/`effort`, workflow `agent()` options, or a role agent you brief), choose the model and effort by the kind of task. This is an explicit instruction, so set `effort` as listed. The main session's model stays the owner's choice (`/model`); these rows govern delegated work and the model you recommend.

| Kind of task | Model | Effort |
|---|---|---|
| Very simple file access and doc handling: copying docs, mechanical checks (grep, counts, link and format checks). Not writing or planning documents | Haiku 5.5 (`haiku`) | default |
| Well-scoped tasks with a clear definition of done: everyday coding, standard features, unit tests, simple refactors | Sonnet 5.5 (`sonnet`) | medium |
| Harder bugs; tasks that need more thinking | Sonnet 5.5 (`sonnet`) | high |
| Normal complex features and multi-file edits; planning, specs and design documents | Opus 5.5 (`opus`) | medium |
| Bugs in complex features; hard bugs | Opus 5.5 (`opus`) | high |
| Still not solved after 2–3 attempts at the level above | Opus 5.5 (`opus`) | xhigh, then max |

- Start at the lowest row that fits. Escalate one step only after a failed attempt, and note the escalation in the PR description or handoff.
- A review gets the row of the work it reviews.
- AGENTS.md still applies in full at every level; a cheaper model never relaxes a safety rule or a test requirement.
