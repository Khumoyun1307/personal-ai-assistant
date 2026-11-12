# C2 — Main-model proposals, exact copying and version authorship

**D-S6 boundary:** an `owner_request` run may hold several chats. It writes owner-requested messages using that context. The **recipient + full text approval card**, followed by policy re-check, is the gate. Contact-triggered runs remain tool-less and isolated to one conversation/contact; proactive Assist keeps `Drafter.draft` / `draft_reply`. No owner-requested compose, revision, Edit or Re-draft may call `Drafter.draft_owner` after cutover. Historical prompts/runs remain readable; no new `draft_owner` prompt.

## Strict model-facing APIs

H4 implementation (2026-11-10, validation pending): `agent/tools/owner_composition.py`
implements the generated proposal and flat revision schemas below. The default-off
`tool.propose_owner_message` flag selects these through `ToolRegistry.for_run`,
removes `propose_reply` from offered tools and switches `revise_draft` to its flat
API; flag off retains the H3/legacy schemas and behavior. Application-set H9
authorship, shared durable effects and nullable C2 receipts apply. Generated
staging/revision requires the typed `OwnerProposalFence` seam in
`actions/proposals.py`, injected through bootstrap/runtime/ToolServices. H10 now
implements it in `actions/owner_fence.py` (`StoredOwnerProposalFence`); missing
checkers still fail closed in explicitly unwired callers. Its check validates
account/run/request binding, unions the run's observed dependencies and locks them
in sorted order before request/action settlement. Source changes warn under D-S17
with Approve anyway / Re-draft / Discard; foreign, forged and unrelated fence IDs
share one unavailable result. Its bind method atomically associates the original
request time, generating run and immutable fence with the committed action version
and post-settlement request CAS version. A literal revision may pass a null fence
ID for a source-only application snapshot, preserving retained artifact dependencies,
but never bypasses the checker. H6 owns prompt activation and H8
card-button cutover. No prompt bytes or migration change in H4; repaired paid
journey/retrieval evals must pass before enabling the flag.

H3 implementation note (2026-11-09): `propose_message` adopts the source API below under its existing flag. Revision 0018 (after I8's 0017) adds the unique audit-effect key index and declares `COMPATIBILITY = "breaking"` under I23's existing-field constraint rule; source proposals and literal revisions share the immutable run/call effect identity independently of mutable draft flags, including cross-tool retries through fresh executors. Until H4/H8 activate the final flat revision API, `revise_draft` retains its one-card selector and legacy word-bound/instruction fields, replacing `append_text` with nullable `literal={request_id, request_version, expected_version, edit, source_ids}` (`edit=literal_append|literal_replace`). Both literal operations use the same C1 copy service; this seam does not implement model-written proposal/rewrite or change a used prompt. H6 must activate the next unused prompt with the current strict schemas. Lead/CI validation, including migration up/down, is pending.

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

**H3 wire compatibility (owner fix-round instruction, 2026-11-09):** the C2 status below is exposed as the additional `proposal_status` field. The top-level tool `status` retains active `owner_request.v10` rule 19 vocabulary: a created approval card is `awaiting_approval` with top-level `action_id`; an exact reuse reports the current artifact status and `reused=true`; blocked cards remain `blocked` with their reason. Source validation failures, missing input, unavailable/stale artifacts and conflicts are `not_drafted` with a fixed reason and applied=false, preserving the nullable artifact requirements below. Literal revisions keep `revised` on success/exact reuse and `not_drafted` on failure. Neither tool emits a new C2 status word as its top-level status before prompt cutover. The executor logs approval-card results as `agent_tool_call_succeeded`, never `agent_tool_call_not_run`. This compatibility mapping changes no effect, conflict or approval semantics.

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

**H3 effect-row compatibility repair (2026-11-09):** effect identities remain in the existing indexed audit storage with `subject_type=tool_effect`, separate from the action lifecycle's `subject_type=action`. Lookups retain compatibility with older identity rows. A changed source-proposal retry includes the original scoped action receipt rather than returning a null artifact; it stages no new text/version. Shared run/call uniqueness and cross-tool conflict checks remain enforced. Deterministic literal edits still record `action_revision_requested` before `action_edited`, with no additional action lifecycle event for the identity row.

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

**H3 review repair (2026-11-09):** C1's `SelectSuggestion` binds the validated owner instruction source to the exact ordered suggestion-source IDs; only the latest retained binding is accepted. Merely occurring after a suggestion is insufficient selection proof. Literal-revision replay resolves the original account/call or retried owner-turn effect before selecting any requested card; changing cards/requests returns a conflict about the original artifact. Terminal and already-revising literal targets use the same C2 compatibility result union (`not_drafted`, `proposal_status=not_available`, an actual relevant action or NULL, NULL effect_version, applied=false).

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
