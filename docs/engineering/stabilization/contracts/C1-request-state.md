# C1 — Durable owner request (H1/H2)

H1 implementation (2026-11-09): [models](../../../../src/assistant/actions/request_models.py), [service](../../../../src/assistant/actions/requests.py), [privacy helpers](../../../../src/assistant/privacy/owner_requests.py), [migration 0015](../../../../migrations/versions/0015_owner_requests.py), [regressions](../../../../tests/integration/test_owner_requests.py). Time-dependent service operations take explicit `now` (aware UTC) from the caller's Clock. This foundation is inert until H2 and subsequent runtime units adopt it; H5 candidate storage/binding is implemented in revision 0021 (unverified); H11 additions remain unimplemented. Validation, PostgreSQL up/down, lint/type checks and manual Telegram checks are unrun in the restricted implementation worktree and remain lead/CI gates.

Implements D-S2/D-S4; consumed by H3–H13, I13/I14 and A1. Existing `pending_inputs` remains the UI question/card mechanism; it does not own task substance. Request state survives process restart, not source retention/deletion. No copied request text, question text or model summary is stored here.

**H6 runtime adoption (2026-11-10; unverified):** the default-off
`tool.propose_owner_message` flag now also gates `agent/owner_requests.py` at
owner-run start. Deterministic explicit messaging intent opens C1 requests;
H2-selected continuations add validated source spans, with scoped trigger/source
lookup for restart identity. H5 binds parsed unique strong recipients only under
managed consent. Request/source IDs enter existing continuation context, and
post-tool structural refresh exposes current CAS versions/readiness. Explicit
retained-message reads can fill a reply target; page reads cannot choose it.
No model-facing opening tool, content column, migration or send authority is
added. Flag off retains the prior loader. Container/CI/journey validation and
the paid acceptance gates remain pending; no used prompt bytes are changed.

## Domain model and persistence

H2 implementation adds control reply/component metadata in revision `0016`, account-local card/turn/question selection in `actions/continuations.py`, and `agent.persistence.load_owner_continuation`. `ToolContext.continuation` exposes typed request facts and validated source spans separately from text-free rendered metadata. `Continuation.basis` reports `explicit_reply`, `pending_question` or `focused_request`; unavailable managed-associated facts use `managed_access_off`. A missing question row can be rebuilt without changing request/action versions or extending the TTL. Successful card associations use C3's `card_presented` audit event. H3/H4/H5/H6/H12 consume these seams; prompts and propose tools are unchanged in H2. Restricted-worktree tests/migrations/lint/type checks and manual reply-to checks remain unrun.

```python
Operation = Literal["dictate", "compose", "reply", "revise"]
RequestState = Literal[
    "collecting",
    "ready",
    "preparing",
    "awaiting_approval",
    "suspended",
    "completed",
    "cancelled",
    "superseded",
    "expired",
]
MissingSlot = Literal["recipient", "content", "target", "constraints"]


class SourceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: int
    field: Literal["text", "quoted_text"]
    start: int
    end: int
    field_sha256: str


class RequestSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    span: SourceSpan
    purpose: Literal["instruction", "literal_body", "constraint", "quote", "suggestion"]


@dataclass(frozen=True)
class OwnerRequestView:
    request_id: int
    version: int
    operation: Operation
    state: RequestState
    contact_id: int | None
    conversation_id: int | None
    missing_slot: MissingSlot | None
    action_id: int | None
    action_version: int | None
    target_message_id: int | None
    sources: tuple[RequestSource, ...]
    requested_at: datetime
    expires_at: datetime
    source_ids: tuple[int, ...]  # same order as sources; scoped database IDs
    selection_basis: str | None  # H5: actual recipient selection, never candidate refs
```

`owner_requests` (H1; all columns structural):

| Column | SQL type / nullability | Meaning / constraint |
|---|---|---|
| `id`, `account_id` | bigint identity PK NOT NULL; bigint NOT NULL FK | Tenant rules in [shared conventions](README.md). |
| `version` | integer NOT NULL DEFAULT 1 | Optimistic request CAS; increment for slot/source/target changes. |
| `operation`, `state` | text NOT NULL, CHECK enums above | Not an execution state machine. |
| `focused` | boolean NOT NULL DEFAULT false | Partial UNIQUE(account_id) WHERE focused; one implicit continuation, several cards allowed. Terminal rows cannot be focused. |
| `trigger_turn_id`, `last_turn_id` | bigint NOT NULL FK control_turns ON DELETE CASCADE | Original request and latest owner continuation, same account. UNIQUE(account_id, trigger_turn_id). Privacy service invalidates linked work before deleting source rows. |
| `contact_id`, `conversation_id` | bigint NULL FK contacts/conversations, ON DELETE CASCADE | Both NULL or both set; conversation must belong to contact/account. Scope deletion invalidates linked work before physical cascade. |
| `selection_basis` | text NULL | `unique_strong` / `owner_choice` / `explicit_reply`; NULL iff recipient absent. |
| `resolution_candidates` (H5 addition) | jsonb NULL | Strict C5 candidate pairs/order/request version; no names/query. H5 owns additive migration and cleanup, not H1. |
| `intent_revision` (H11 addition) | bigint NULL, CHECK ≥0 | Conversation owner-intent token assigned to this selected explicit request; H11 owns additive migration and clears with recipient linkage. |
| `missing_slot` | text NULL CHECK MissingSlot | Non-NULL only in collecting/suspended; one useful question at a time. |
| `question_input_id` | bigint NULL FK pending_inputs ON DELETE SET NULL | `pending_inputs.ref_id` points to this request for kind `owner_request`; recreate missing UI from state. |
| `action_id`, `action_version` | bigint NULL FK actions ON DELETE SET NULL; integer NULL | Both present or both NULL; linkage only, never approval. Every action-row deletion helper clears **both** columns first in the same transaction (test sweeper/account/scoped paths), so FK nulling cannot violate paired CHECK. |
| `target_message_id` | bigint NULL FK messages ON DELETE SET NULL | Explicit reply target, same conversation. Privacy/deletion helper invalidates task before unlink; NULL is never permission to choose newest. |
| `constraints` | jsonb NOT NULL DEFAULT '{}' | Strict `RequestConstraints`: `min_words: int\|None`, `max_words: int\|None` (1–1000, min≤max), `reply_language: str\|None` (supported tag); no prose. Original constraints also retain source spans. |
| `requested_at`, `updated_at`, `expires_at` | timestamptz NOT NULL | requested_at = original turn's persisted arrival time; immutable; expiry below. |
| `ended_at` | timestamptz NULL | Set on terminal transition. |
| `reason` | text NULL CHECK fixed codes | `owner_cancelled`, `topic_switch`, `source_unavailable`, `recipient_changed`, `superseded`, `max_age`, `action_terminal`, `restored_from_backup`. |

`owner_request_sources` (H1; structural references only):

| Column | SQL type / nullability | Constraint |
|---|---|---|
| `id`, `account_id` | bigint identity PK NOT NULL; bigint NOT NULL FK | Tenant scoped. |
| `request_id` | bigint NOT NULL FK owner_requests ON DELETE CASCADE | Same account. |
| `ordinal` | integer NOT NULL | ≥0; UNIQUE(account_id, request_id, ordinal), bounded to 64 spans/request. |
| `turn_id` | bigint NOT NULL FK control_turns ON DELETE CASCADE | Removing a required source invalidates request first. |
| `field`, `purpose` | text NOT NULL CHECK enums above | Quoted field only `quote`; assistant-role text only `suggestion`; owner instructions/body never from assistant or forwarded text. |
| `start_char`, `end_char` | integer NOT NULL | `0 ≤ start < end`; Python Unicode code-point offsets, end exclusive, **not UTF-16 offsets**. |
| `field_sha256` | text NOT NULL | SHA-256 of original entire selected field UTF-8; checked before every load/copy; no normalization. |

H2 adds `control_turns.reply_to_telegram_message_id bigint NULL` (structural, account's control-chat namespace). Resolve to current card pointer or the scoped presentation mapping in C3; never accept a business message id as a control artifact. No new platform behavior is claimed: parse existing inbox payload metadata; fixture/manual verification remains H2's responsibility.

## Operations

```python
async def open_request(
    session: AsyncSession,
    *,
    account_id: int,
    turn_id: int,
    operation: Operation,
    sources: tuple[RequestSource, ...],
    now: datetime,
) -> OwnerRequestView: ...
async def update_request(
    session: AsyncSession,
    *,
    account_id: int,
    request_id: int,
    expected_version: int,
    update: RequestUpdate,
) -> RequestUpdateResult: ...
async def resolve_continuation(
    session: AsyncSession, *, account_id: int, turn_id: int, reply_to_message_id: int | None
) -> Continuation: ...
async def load_request(
    session: AsyncSession, *, account_id: int, request_id: int
) -> OwnerRequestView | None: ...
async def load_source(
    session: AsyncSession, *, account_id: int, source: RequestSource
) -> ValidatedSource | SourceUnavailable: ...
```

`RequestUpdate` is a discriminated union of `AddSources(sources)`, `SelectRecipient(contact_id, conversation_id, basis)`, `SelectSuggestion(instruction_source_id, source_ids)`, `SetQuestion(slot, input_id)`, `SetTarget(message_id)`, `SetConstraints(constraints)`, `LinkAction(action_id, version)`, `SetState(state, reason)`. Every variant carries only typed fields above. `RequestUpdateResult = applied(view) | stale(current_version) | unavailable(code)`. `Continuation = selected(request_id, version, action_id?, action_version?) | ambiguous(request_ids) | none | unavailable(code)`. No model-provided account id.

**H3 review repair (2026-11-09):** each source rendered in continuation metadata includes its scoped `source_id` beside its span/purpose, so proposal calls can use only model-visible references. `SelectSuggestion` is internal request bookkeeping, not a new agent tool: it records a classified owner selection of an exact ordered suggestion-source list. All referenced sources must be in this account/request, live, hash/bounds/role-valid; the instruction must be owner `text` classified as `instruction`, later than the selected assistant artifacts. Its strict content-free audit event `owner_request_suggestion_selected` uses subject `owner_request` and details `{kind: "select_suggestion", instruction_source_id, source_ids}`. The latest binding alone applies; chronology or a retained instruction without that binding is insufficient. Copying revalidates the binding's instruction and artifacts. No new table, column or prompt version is introduced.

Focus changes take account `FOR NO KEY UPDATE` **first**, then the normal conversation/request order, to serialize clearing old focus and setting new focus. Do not upgrade an account SHARE lock after acquiring conversation/request locks. Non-focus reads/settlement retain the shared conventions. If several unexpired questions are possible and neither explicit reply nor focused request identifies one, return ambiguous rather than picking newest arbitrarily.

| Operation | Minimum ready inputs |
|---|---|
| dictate | Selected recipient + owner literal_body span(s), exact source copy (C2). |
| compose | Selected recipient + owner content/purpose instruction; no brief ⇒ missing content, never infer topic from old chat. |
| reply | Selected recipient + explicitly selected retained contact message, or a freshly read identified latest contact target; no retained target ⇒ ask/explain. |
| revise | Explicit action/current version + owner change instruction or literal edit sources; keep recipient/mode/target unless owner explicitly changes the task. |

## Continuation and lifecycle

| Event | Required transition / behavior |
|---|---|
| New underspecified task | collecting; set focused; persist source refs before asking. No action. |
| Next own-text turn answers slot | Explicit reply target first; then active pending question; then focused request; only then bare-phrase search. Name/number/“shorter” may fill the slot. Forwarded content cannot answer as an owner instruction. |
| Reply directly to card | Resolve account-locally to action + presented version and linked request. Re-read current version; mismatch asks/returns stale, never edits a different current card. Unknown/deleted reply target is explicit unavailable, not fallback. |
| All required slots filled | ready → preparing (attempt marker only) → awaiting_approval after successful stage; restart may recover preparing by reloading artifacts, never assuming no effect. |
| Owner asks unrelated question | Suspend unfinished focused request, preserve its sources; answer new question. No automatic reuse of old body/recipient. Explicit return/reply may resume it. |
| Owner changes recipient/topic for new message | New request; old unfinished one suspended or superseded with explicit owner intent. Do not transfer body to a new recipient implicitly. |
| Cancel | Cancel named/focused unfinished request; cancel its unclaimed pending action using existing lifecycle. Never undo an executing/succeeded effect; report current receipt. |
| Action terminal | Reconcile request to completed (succeeded), or suspended with factual reason for recoverable outdated/failed/blocked/rejected/expired outcomes. A delivery_uncertain action suspends request with an uncertainty receipt and no retry authority; never describe it as approvable. |
| Expiry | collecting/ready/preparing/suspended expire 24 h after original request; continuation does not extend it. awaiting_approval follows linked action max-age and terminal handling, never extends action expiry. Explicit new request may reuse still-retained sources after validation. |
| Restore | Cancel request attempts and invalidate pending questions with `restored_from_backup`; no automatic resumption before privacy reconciliation. |

The 24 h request TTL bounds abandoned clarification metadata, not Telegram's reply window. It grants no send permission. Source retention may end earlier.

## Retention, deletion and migration

| Class / columns | Retention row for implementing PR | Cascade / consent |
|---|---|---|
| owner_requests (all structural columns) | Unfinished TTL above; terminal metadata ≤1 year from requested_at, hourly sweep. | Account/everything deletes rows. Conversation/contact/managed deletion cancels affected requests and removes source/link metadata; no executing action reset. Control deletion invalidates source-dependent requests. Metadata alone needs ai_control to enter a model; linked managed target reads need ai_managed. |
| owner_request_sources (all structural columns) | Until source loses text or request is deleted, ≤1 year; never pins source text. | Account/request/source row cascade; **text clearing** also invalidates/removes source refs in same privacy transaction/sweep. Quotes retain original managed/third-party consent class. |
| audit_log suggestion-selection binding (structural IDs/enums only) | Existing audit ceiling of 1 year; removed earlier with request/source purge or request sweep. | Account/everything deletion applies; scoped source/request erasure removes the binding in the same transaction. Account-scoped structural export includes `suggestion_selections`. No source text is copied or retained. |
| control_turns.reply_to_telegram_message_id | Same as control turn row; clear on control/all deletion and relevant scope redaction. | IDs only; ai_control, plus existing managed withholding when applicable. |
| Derived actions.text (existing content column) | Existing action-text retention, never extended by continuation. | Source deletion/clearing removes derived body and cancels pending action; executing state preserved. Explicit copied preference persistence is governed separately by C8. |

No content column is added by H1. If implementation copies text, it is a contract change requiring ARCHITECTURE §10.1/10.4, sweeper, export, consent and cascade tests in that PR. Test physical row deletion **and** retained-row text clearing. Migration up/down on PostgreSQL 18; identity keys never assigned in fixtures. Allocate the **next free revision on merged main at implementation time**: current head `0014`; P3's planned `0015_p3` is on hold and must rebaseline, not reserve that number.
