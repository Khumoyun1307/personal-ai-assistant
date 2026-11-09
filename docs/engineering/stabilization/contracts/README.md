# Stabilization interface contracts

Specification baseline: owner decisions **2026-11-09**, current M02–M05 interfaces, schema head `0014`. These are implementation contracts, **not as-built or live-verified claims**. [DECISIONS](../DECISIONS.md) overrides historical review recommendations, especially D-S6. Implementation units update the architecture and affected historical decision records when they land. No prompt, migration or runtime behavior changes in P0-2.

| Contract | Producers / consumers |
|---|---|
| [C1 — Request state](C1-request-state.md) | H1/H2 foundation; every owner-writing unit; I13/I14/A1 |
| [C2 — Proposals and authorship](C2-proposals.md) | Q4 → H3 → H9 → H4; H6/H8 |
| [C3 — Receipts and card facts](C3-receipts-and-card-facts.md) | Q3/Q5 → H7; H8/I13/I27 |
| [C4 — Taint lifecycle](C4-taint-lifecycle.md) | H2 → H12 → H4/H6/H13 |
| [C5 — Contact resolution](C5-contact-resolution.md) | H5; H2 selection; H6 routing |
| [C6 — Freshness and precedence](C6-freshness-and-precedence.md) | H10 → H8/H11/I3 |
| [C7 — Coverage and budgets](C7-coverage-and-budgets.md) | I2/I4/I6/I15/I16; shared call boundaries |
| [C8 — Trusted preferences](C8-preferences.md) | Q1; H13 → H6; scoped Assist loaders |
| [C9 — Privacy recovery options](C9-privacy-recovery-options.md) | I19 → owner choice → I20/A4 |
| [Acceptance cases](../cases/README.md) | A1 executable evaluator; A2/A3 evidence |
| [Unit specifications](../specs/README.md) | H1–H13 and Q4, merged in PLAN lane order |

## Shared conventions

| Concern | Binding rule |
|---|---|
| Typed sketches | Python 3.14; `Id = int` means positive signed 64-bit, rejecting booleans. `datetime` means aware UTC. Pydantic model fields are all required, nullable where shown; `ConfigDict(extra="forbid")`; semantic validators after schema validation. Use existing `strict_schema`, `ArgumentRuleError`, and account repositories. Sketches omit imports only. |
| Ports and layering | Domain code never imports agent/adapters; source/request values live in a domain/shared module and are passed into `agent` loaders. Only bootstrap constructs adapters. No new dependency or external processor. |
| Persistence | Identity bigint keys; every new tenant table has indexed `account_id bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE`. Composite `(account_id, id)` FKs/uniques, or equally strict scoped validation under locks, prevent cross-account edges. Every repository method requires `account_id`; foreign and missing IDs have the same public outcome. |
| Authority | Request bookkeeping and proposal staging grant no send/configuration authority. Tainted mutations keep confirmation; approval is always a card tap for the **current action version**. No new business method or retry path. |
| Locking | Account → conversation(s), sorted by id → request → action → dependent metadata. Extend the existing order consistently across callers; never acquire a conversation after locking a request/action. Unbound request-only updates use account → request. Commit before model/Telegram I/O. Owner jobs retain `acct:{id}:control`. |
| Privacy | No text in jobs, logs, receipts, run steps or structural JSON. Source hashes are integrity metadata, never sufficient authorization or proof of authorship by themselves. Managed items keep `MANAGED` provenance; provider consent is checked on each call. |
| Schema/retention delivery | Each schema-owning PR updates ARCHITECTURE §10.1/10.3/10.4, `privacy/content_columns.py`, retention/export/deletion paths and schema/cascade tests, including structural columns. No new content is implicitly hidden in metadata. |
| Version allocation | Allocate migration and prompt versions against merged `main` at implementation time. Never reserve a used prompt or P3's planned `0015_p3`; P3 is on hold. H6's steward activates the next unused `owner_request` version; D-S6 requires no new `draft_owner` version. |

## Compatibility seams to preserve

| Existing interface | Contract delta |
|---|---|
| `OwnerTurn.instruction` rejects a whole quoting turn | Validate its separate owner `text` spans; keep `quoted_text` untrusted (C1/C4). |
| `ProposalRequest`, `propose`, `_live_for_turn` | Add request/fence/authorship facts; different intent is a conflict, not successful reuse (C2/C6). |
| `revise_draft` → `CardReviser` → `Drafter.draft_owner` | Main owner model supplies revised text; lifecycle CAS still rotates the token (C2). |
| `LiveCardFact`, `live_cards`, `get_draft` | Content-free fresh receipts plus separate artifact reads (C3). |
| `shown_through_message_id` | Preserve arrival watermark; supplement it with revision fences and explicit incomplete coverage (C6/C7). |
| `memory_items.provenance` | Backward-compatible source-verified owner preference addition; no blanket trust upgrade (C8). |
| `LLMRequest`, `ToolServices`, `ToolExecutor` | Shared operation allowance and account-wide admission at every provider attempt (C7). |

Only C9 requires a new owner design choice. Wording approval and run-specific paid/live authorizations remain acceptance requirements, not permission inferred by these contracts.
