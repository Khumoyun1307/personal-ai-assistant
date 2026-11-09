# Stabilization — M02–M05 repairs before P3

**Status: approved by the owner, 2026-11-09 (D-S1).** P3 (M06/M07) is on hold until stabilization Phase 4 acceptance and the owner's resume decision. This phase repairs complete owner journeys, alert/task correctness, privacy recovery, and operations; it does not authorize general M06/M07 implementation.

| Read | Purpose |
|---|---|
| [Review, 2026-11-09](review-2026-11-09.md) | Read-only synthesis, findings, original recommendations, scenarios and playtest |
| [Raw reviews R0–R4](reviews/) | Supporting research and reviewer evidence; preserved as historical analysis |
| [DECISIONS.md](DECISIONS.md) | Owner answers D-S1–D-S14, superseded rulings and required document changes |
| [PLAN.md](PLAN.md) | Amended work units, dependencies, parallel lanes and acceptance |
| [Contracts C1–C9](contracts/README.md) | Typed target interfaces, storage/privacy boundaries and open I20 recovery choice |
| [Unit briefs](specs/README.md) / [synthetic cases](cases/README.md) | H1–H13/Q4 implementation criteria and multilingual A1 acceptance scenarios |
| [Milestone status](../../IMPLEMENTATION_PLAN.md#14-working-agreement-per-milestone) | Authorization and milestone reporting |
| [P3 hold](../p3/README.md) | Deferred plan to rebaseline after acceptance |

The review is analysis, not the approved design. **D-S6 is the owner's “one brain” choice, differing from the review recommendation:** the main chat model composes/revises owner-requested messages with the conversation context; a strict propose tool stages its text for approval. Exact dictation copies verified owner source spans. The separate tool-less writer remains for proactive Assist; future delegated contact-triggered writing stays separately scoped. Every business send still needs the owner's approval-card tap and the policy re-check on this owner-requested path.

| Work now | Status |
|---|---|
| P0-1 — Decision record and amended plan | merged #240 |
| P0-2 — Contracts and acceptance specification | in progress |
| Q1 — Read effective settings | merged #236 |
| I7 — Durable held-alert delivery | in review #239 |
| I19 — Restore quarantine | merged #238 |
| I21 — Durable backups | merged #237 |
| Other units | approved scope; start only after their listed dependencies/contracts |

## Workflow

| Step | Rule |
|---|---|
| Implementation | One Codex implementation agent per unit, one isolated worktree/branch `stabilize/<unit>`, one focused PR targeting `main`. No P2/P3 integration branch for stabilization. |
| Dependencies | Dependent units start from merged foundations. The lead sequences shared files and prompt activation as specified in PLAN. |
| Handoff | Implementation agents leave changes uncommitted and provide a file with changes, relevant test commands, docs, risks and proposed commit text. They do not push, open PRs, contact services or read real data. |
| Review and merge | The lead reviews, runs short relevant checks in the dev container, fixes findings, commits/opens the unit PR and merges after clean review and green CI. CI is the full-suite gate; docs-only PRs follow the existing CI skip rule. |
| Evidence | Regression first; preserve safety tests; update affected docs in the unit. Used prompts are immutable; gated prompt/schema changes need affected synthetic evals before shipping. |
| Live work | Deploys need the owner's go-ahead. Paid evals and production checks need their specific authorization; implementation approval is not live-run approval. |

## Acceptance gates

D-S14 approves the review's thresholds; these supplement existing gates and do not mark any milestone done.

| Gate | Required evidence |
|---|---|
| Safety and exactness | 100% deterministic pass for recipient, literal dictation, account/consent isolation, approval/version, policy and at-most-once scenarios; zero observed unauthorized effects. Contact-triggered isolation remains unchanged under D-S6. |
| Core journeys | All four reported conversation patterns succeed in at least three repeated trials per applicable locale; no re-pasting already supplied dictation. |
| Intent and language | At least 95% acceptable completion per language on the broader owner-confirmed journey set; every failure visible. Review Uzbek Latin/Cyrillic, Russian, English and applicable mixed-language cases. |
| Clarification | No unnecessary question for unique strong matches in core cases; broader unnecessary-clarification rate ≤5%. Asking for missing content is correct. |
| Honesty | No false sent/done/active or wrong-artifact content claim; uncertain/partial outcomes are explicit. |
| Latency and cost | Complete-request p50/p95 and cost include clarification, retries and nested work; meet existing NFRs or record an explicit owner exception. |
| Recovery | Demonstrate backup catch-up/upload recovery, privacy reconciliation, schema-aware release recovery and visible degraded work; measure RPO/RTO against NFR-09. |
| Release | Green CI, affected full evals, owner intent/language review, clean independent review and owner-reviewed M04/M05 acceptance reports. P3 resumes only by owner decision after Phase 4. |

No new live verification, model switch, deployment, or completed acceptance is claimed by this documentation.

