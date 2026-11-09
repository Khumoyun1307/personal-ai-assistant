# Phase 2 implementation briefs

These briefs implement [PLAN](../PLAN.md), [owner decisions](../DECISIONS.md), and [C1–C9](../contracts/README.md). One unit/PR to main, leave implementation-agent work uncommitted. No real data, API, Docker or server access in restricted worktrees. The lead runs the short relevant tests below inside the dev container; CI is the full-suite gate. Tests listed as **new** are to be written by the owning unit, not files added by P0-2.

| Unit | Brief | Merge prerequisites / lane |
|---|---|---|
| Q4 | [Honest proposal conflicts](Q4.md) | Scope approval; replies lane first |
| H1 | [Durable request foundation](H1.md) | P0-1/P0-2; schema lane first |
| H2 | [Continuation and reply targets](H2.md) | H1; owner ingress/context; registry after Q1/Q3 |
| H3 | [Literal copying](H3.md) | H1/H2/Q4; replies then H9 |
| H4 | [Main-model proposal tool](H4.md) | H1/H2/H3/H9/H12; replies/runtime/registry before H6 |
| H5 | [Contact verdict](H5.md) | P0-2, H1 binding; pure matching may prepare separately; registry after H4 |
| H6 | [Main-chat prompt activation](H6.md) | H2–H5/H9/H12/H13; one prompt steward |
| H7 | [Artifact-grounded reporting](H7.md) | H1/Q3, integrate after H6; runtime/cards before H8 |
| H8 | [Task-preserving Re-draft](H8.md) | H1/H4/H6/H7/H9/H10; runtime/replies/cards before H11 |
| H9 | [Per-version authorship](H9.md) | H1/H3 and Q2/Q5; provenance seam before H4 |
| H10 | [Source freshness](H10.md) | P0-2; foundation may prepare early, context hooks after H7; full PR merges before I2/I4/I3/H8/H11 |
| H11 | [Owner precedence](H11.md) | H1/H10; runtime/cards after H8; before I13 |
| H12 | [Taint lifecycle](H12.md) | H1/H2; runtime/context before H4/H6 |
| H13 | [Trusted preferences](H13.md) | H1/H4/H12 contracts; registry after H5, memory before I12/H6 |

Shared-file order is binding even when conceptual dependencies permit preparation. H4/H6 may land disabled/testable before H10 runtime hooks; **no repaired candidate is activated/released with a missing freshness fence**. H10 standalone foundation can prepare earlier, but do not skip H7→H10 hooks→H8 ordering. I2/I4 must wait for merged H10, including the revision migration and all input update hooks, before their own merge; a frozen interface or preparatory edits alone do not fulfill the prerequisite. This is a seam completion gate, not authorization to move unrelated runtime edits ahead of their lane.

## Common acceptance for every code unit

| Requirement | Evidence |
|---|---|
| Regression first | Add a failing service/integration test reproducing the specified defect before fix. No existing safety regression weakened/deleted. Update undesirable historical expectations only with the dated owner decision. |
| Determinism | FakeClock/FakeLLM/FakeChannel; PostgreSQL 18, no SQLite; no timing sleeps; no explicitly assigned identity PK. |
| New/changed tools | Strict Pydantic args, semantic-invalid and foreign-ID tests, registry run/tier/flag default-off checks, idempotent retry. Update flag catalogues/exact offered-tool tests in `tests/integration/agent_env.py`. |
| Prompts | No used prompt edits. H6 steward allocates next unused owner_request version on merged main (v10 currently active; never assume v11 is still free). Other units supply rule/schema requirements through steward. **D-S6 creates no new draft_owner version.** Keep historical assets resolvable. |
| Docs/storage | Update affected ARCHITECTURE sections and historical supersession notes in owning PR. Each new/changed persisted model follows contract type/nullability/account/consent/retention/cascade rules, schema registry/export/sweep tests and migration up/down. Allocate next revision on merged main. |
| Validation handoff | Exact relevant pytest invocation, lint/type check scope, unrun steps, changed docs and risks. Never claim tests/evals/manual checks passed without running. No notes/handoff files in repo. |
| Safety | Preserve policy/approval/version/committed claim, tainted-mutation cards, contact-triggered isolation, consent and content-free logs. Every send remains outside tools. |

Commands in briefs use `docker compose -f compose.dev.yml run --rm app uv run pytest ...` for the **lead**, not permission for restricted implementation agents. Paths labelled new must exist after that implementation. Full affected paid evals/owner language review are separate authorized acceptance work, never implied by fixture tests.
