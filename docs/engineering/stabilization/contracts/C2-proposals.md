# C2 — Main-model proposals, exact copying and version authorship

**D-S6 boundary:** an `owner_request` run may hold several chats. It writes owner-requested messages using that context. The **recipient + full text approval card**, followed by policy re-check, is the gate. Contact-triggered runs remain tool-less and isolated to one conversation/contact; proactive Assist keeps `Drafter.draft` / `draft_reply`. No owner-requested compose, revision, Edit or Re-draft may call `Drafter.draft_owner` after cutover. Historical prompts/runs remain readable; no new `draft_owner` prompt.

## Strict model-facing APIs

```python
class ProposeOwnerMessageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: int
    request_version: int
    conversation_id: int
    mode: Literal["reply", "new_message"]
    answered_message_id: int | None
    text: str  # main model's complete generated body
    context_fence_id: int  # application-issued read snapshot, C6


class ProposeSourceMessageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: int
    request_version: int
    conversation_id: int
    answered_message_id: int | None
    source_ids: list[int]  # C1 source rows; not text to compare/correct


class ReviseDraftArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: int
    request_version: int
    action_id: int
    expected_version: int
    edit: Literal["rewrite", "literal_replace", "literal_append"]
    text: str | None  # required only for rewrite
    source_ids: list[int]  # nonempty only for literal edits
    context_fence_id: int | None  # required for rewrite; source/target checks still apply otherwise
```

| Tool | Capability / runs / tier | Flag (default **off**) | Result / implementation seam |
|---|---|---|---|
| `propose_owner_message` (new H4) | PROPOSE / owner_request only / CONTROL_ONLY | `tool.propose_owner_message` | Stage generated text through `actions.proposals.propose`; no nested writer. |
| `propose_message` (H3 replacement schema above) | PROPOSE / owner_request only / CONTROL_ONLY | Existing `tool.propose_message` | Application copies validated C1 source_ids in order. |
| `revise_draft` (new schema above) | PROPOSE / owner_request only / CONTROL_ONLY | Existing `tool.revise_draft` | Main model reads actual artifact, supplies full rewrite; lifecycle applies version CAS. |
| `propose_reply` (legacy) | Retired from offered tools at H6 cutover | Existing flag never enables old writer after cutover | Return fixed `not_available`/replacement-tool instruction if called; no silent fallback. |

Register all APIs in `registry.py`; test strict schemas, semantic invalid arguments, tier/run/flag filters, foreign IDs and retries. Update **all** flag catalogues and offered-tool expectations in `tests/integration/agent_env.py`. Managed-consent-off stubs read nothing; active proposals preserve current managed consent requirement. Receipts can be content-free without changing tool admission.

## Validation and idempotency

```python
async def stage_owner_proposal(
    session: AsyncSession,
    *,
    account_id: int,
    run_id: int,
    tool_call_id: str,
    proposal: ValidatedOwnerProposal,
) -> ProposalReceipt: ...
async def apply_owner_revision(
    session: AsyncSession,
    *,
    account_id: int,
    run_id: int,
    tool_call_id: str,
    revision: ValidatedOwnerRevision,
) -> ProposalReceipt: ...
```

`ValidatedOwnerProposal` contains request id/version, scoped recipient, mode/target, resolved body, application-computed `VersionAuthorship`, C6 fence and request time. `ValidatedOwnerRevision` additionally carries action_id/expected_version. These are domain values, never model-set approval/policy flags.

| Check | Required outcome |
|---|---|
| Request/recipient | Ready active request; recipient matches its selected contact/conversation; supplied ids all in account. Candidates in ToolRefs alone prove no selection. |
| Substance | compose without owner brief ⇒ `needs_input(content)`, no invented message. Reply needs read target; new_message requires answered_message_id=NULL. |
| Text | Preserve body exactly; no strip, Unicode folding or punctuation repair. Reject empty/all-whitespace or >existing `sendable_length` limit (4096 UTF-16 units); never split business text. Check request word bounds with existing deterministic word counter. Mismatch ⇒ `invalid_arguments(length_not_met)` and no new version; bounded model retry uses C7. |
| Literal | Source belongs to request/account, correct role/purpose, hash and offsets valid, source retained. Copy `field[start:end]`, **not** model text. Single span for body; multiple spans concatenate with **no invented separator** (spaces/newlines must be source characters). Command words are excluded by explicit literal-body selection, not substring coincidence. |
| Fuzzy matching | May assist location/identity UI only; never proves dictation. `X1` ≠ `x1`, `Max` ≠ `Mah`, straight/curly apostrophes differ; NFC/NFKC, whitespace, case and x/h folds cannot authorize content. |
| Same `(run_id, tool_call_id)` | Same canonical semantic args ⇒ same effect reference, fresh current C3 facts, no new action/version. Different args ⇒ `idempotency_conflict`, no effect. Executor's in-memory `_done` must check digest too. |
| Different call, same request/turn | Semantically identical proposal ⇒ existing artifact, `reused_exact`. Different text/mode/target/sources/recipient ⇒ `proposal_conflict` with existing action/version; requested change was **not** staged. Explicit revision with expected_version required. |
| Q4 compatibility | Interim `existing_draft_conflict` maps to final `proposal_conflict`; both mean no new requested artifact. No caller treats either as `reused_exact` or as successful requested text. Never fix by enabling last-call-wins. |
| Settlement | C6 validates sources, takeover and precedence under proposal locks; blocked/stale/conflicting work never replaces a live valid card. |

Canonical semantic identity includes exact body hash, source ids and proofs, mode/target/recipient, request id/version and edit/base version; key order and transport-only call ids do not alter it. Store digest/effect IDs in structural audit details, not body/args copies. Exact retries after source retention return artifact facts, never regenerate deleted text. After any race, return current facts, not original successful-state prose.

Lookup order: validate schema/account scope and existing effect key/digest first; an exact completed retry returns current artifact facts **before** requiring the old request version still to be current. For a new effect, validate active request/version/sources/fence before staging. Persist `effect_action_id: int`, `effect_version: int`, `run_id: int`, `tool_call_id: str` (bounded provider identifier), `semantic_digest: str` in an ids-only `tool_effect_recorded` audit event in the same transaction as the effect; serialize/recheck under the proposal/revision lock and enforce a unique partial expression index on `(account_id, details.run_id, details.tool_call_id)` for that event. Existing actions `(run_id, tool_call_id)` uniqueness remains. The old effect version and the current receipt version are separate: a retry after later edits cannot claim its original body is still the current artifact. Missing/purged artifacts yield factual unavailable/no-longer-pending, never replay.

`ProposalReceipt` contains an **optional** [C3 ActionReceipt](C3-receipts-and-card-facts.md); it does not inherit mandatory action fields:

```python
@dataclass(frozen=True)
class ProposalReceipt:
    status: Literal[
        "staged",
        "revised",
        "reused_exact",
        "needs_input",
        "proposal_conflict",
        "idempotency_conflict",
        "stale",
        "blocked",
        "not_available",
        "invalid_arguments",
    ]
    reason: ProposalReason | None  # fixed enum, never arbitrary error prose
    request_id: int | None  # only a validated, account-scoped request
    request_version: int | None  # present iff request_id is present
    action: ActionReceipt | None
    effect_version: int | None  # version committed by this idempotency key, if any
    requested_change_applied: bool
```

| Result | Action/effect requirements |
|---|---|
| staged / revised | `action` required; `effect_version` is the committed version; applied=true. |
| reused_exact | `action` required for a retained artifact; applied=false. Same-key retry reports original effect_version plus current receipt; a different-key exact reuse has no new effect_version. Purged artifact instead returns not_available with action=NULL. |
| proposal_conflict (Q4 interim existing_draft_conflict) | Existing scoped `action` required, applied=false; no effect_version for the rejected call. Its requested text was not staged. |
| needs_input / invalid_arguments before staging | `action=NULL`, `effect_version=NULL`, applied=false. Missing content or invalid/missing source cannot create an action, version, presentation record or fabricated receipt. Existing unrelated live cards remain separate C3 facts. Invalid/foreign request IDs return both request fields NULL. |
| idempotency_conflict / stale / blocked / not_available | applied=false; `action` is present only if an actual relevant scoped artifact can be read. `effect_version` is present only for an already recorded effect of this same key, never inferred from an unrelated card. Otherwise NULL. |

Applied means an effect committed **in this call**. No-action failures create no `tool_effect_recorded` event; their content-free validation/run-step outcome is not an action effect. Consumers branch on `action is None`, never fill action_id/version/state with sentinels or infer a card from request linkage. Staged/revised still needs a card tap; **never “sent.”** Regression tests must exercise missing-content and invalid-source proposal responses with `action=NULL` and unchanged action/version/presentation counts; cover foreign request IDs without leaking facts and exact retries/conflicts with real fresh receipts.

## Authorship is per version (H9)

```python
AuthorshipKind = Literal[
    "owner_dictated", "owner_literal_edit", "assistant_written", "agent_proposal_selected"
]


class VersionAuthorship(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    kind: AuthorshipKind
    source_ids: list[int]
    parent_version: int | None
    retains_generated_text: bool
```

H9 adds `actions.authorship jsonb NULL` (strict sketch above; structural, current-version only). NULL is legacy/unknown and **never** proof of owner authorship. Backfill provably generated legacy origins as generated; old dictated rows that may have undergone AI revision are treated conservatively as generated/unknown unless source proof exists. Do not retrospectively manufacture spans. Current proof is atomically updated with version/text/token; content-free lifecycle audit records retain each version's kind/parent/source IDs, not old bodies. No action_versions text archive.

| Body operation | Version kind / generated treatment | Card label |
|---|---|---|
| App copies owner literal body | owner_dictated / false | **Your words** |
| Literal replacement of whole body | owner_literal_edit / false | **Your words** |
| App appends exact owner span to owner-only body | owner_literal_edit / false | **Your words** |
| App appends to generated/selected body | owner_literal_edit / true | **Written by the assistant**, with owner addition identified |
| Main model writes/rewrites, including dictated v1 → AI v2 | assistant_written / true | **Written by the assistant** |
| Owner explicitly selects retained assistant suggestion | agent_proposal_selected / true | **Written by the assistant** |

Suggestion copying requires an explicit owner selection source plus the exact scoped assistant artifact/span (C1 purpose suggestion); the application copies it; no arbitrary historical fuzzy substring. If suggestion inherited managed/untrusted prose, preserve provenance/consent, never upgrade it to owner wording.

`ai_generated = retains_generated_text OR kind in {assistant_written, agent_proposal_selected} OR legacy_unknown`. Rendering, consent-revocation cancellation, policy loading and execution use this **current-version** predicate, not immutable action origin. Keep `ActionOrigin` as original trigger classification; owner-requested status comes from request linkage. `apply_new_version` receives authorship/fence atomically. Cancel/failed revision preserves old text/authorship/version/expiry and rotates only the approval token; successful rewrite clears prior approval hash and rotates token. Old token cannot approve new text. No used prompt edit.

| Persistence field | Retention / deletion / consent |
|---|---|
| actions.authorship (new structural JSON) | Same row retention as actions: 1 year; delete/clear source refs on privacy purge; no source text pinned. Current body follows existing actions.text retention/cascades. Provider exposure of proof IDs needs ai_control; artifact text follows original managed provenance. |
| audit details for provenance/idempotency (additive strict keys) | Existing content-free audit retention 1 year; account/all deletion applies; scoped purge removes sensitive source links. No text, names, quoted fragments or arbitrary model flags. |

## Invariant preservation

| AGENTS §6 | Contract obligation |
|---|---|
| 1, 2 | Tools stage only; policy decides at proposal and immediately before `actions.execute`. |
| 3b | Contact/quoted/derived prose remains untrusted; configure/control gates unchanged; proposal is inert until tap. |
| 4 | D-S6 expressly permits multi-chat owner_request composition; contact-triggered context remains one-conversation/tool-less. Do not pass main transcript to Assist. |
| 6 | Version-bound tap → committed claim → one business attempt; uncertain never replayed. |
| 8 | Owner-approved work follows P2-O20, Telegram refuses once; no artificial initiation/window bypass. Autonomous precheck unchanged. |
| 9 | ai_control and every managed context item require current processor-specific consent, including retries; generated versions get cancellation/checks. |
| 11 | Receipts distinguish stored, presented, awaiting, blocked, failed and uncertain. Content claims require artifact read. |
| 13 | Owner-approved versions carry no autonomous disclosure; no path to delegation_auto or false human identity. |
