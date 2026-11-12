# C6 — Source revision fence and owner-intent precedence (H10/H11)
**Owner amendment D-S17, 2026-11-10:** source freshness is advisory for owner-approved messages. Stale results during generation and changed dependencies after card creation show a fixed warning with Approve anyway / Re-draft / Discard. Approval binds to the exact shown version/hash; deleted reply targets send standalone. Old tokens never approve a replacement. Ordinary approvals still receive execution freshness checks and recover to a warned approval path on a mismatch. Autonomous work retains conservative rejection; explicit privacy erasure clears derived bodies/fence links. This amendment supersedes the owner-stale rejection language below, without changing C7 reader settlement or H11's background precedence work.

D-S10 amends P2 R3: request time, not late action creation time, is the takeover reference. Source IDs detect arrival, not edits. A read snapshot must survive neither changed content nor overtaking owner intent. No lock spans a model call.

```python
class ConversationFence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: int
    content_revision: int
    owner_intent_revision: int
    shown_through_message_id: int | None
    newest_owner_manual_id: int | None


class ContextFence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: int | None  # None for proactive Assist
    request_version: int | None
    requested_at: datetime
    conversations: list[ConversationFence]


async def capture_fence(
    session: AsyncSession,
    *,
    account_id: int,
    run_id: int,
    request: OwnerRequestView | None,
    reads: tuple[ObservedConversationRead, ...],
) -> int: ...
async def check_fence(
    session: AsyncSession,
    *,
    account_id: int,
    fence_id: int,
    request_id: int | None,
    expected_request_version: int | None,
) -> FenceResult: ...
```

`ObservedConversationRead` is application-created `{conversation_id, content_revision, shown_through_message_id, newest_owner_manual_id}` read in the **same snapshot** as returned content. Never stamp current revision onto old text. A model-facing fence id must resolve to **this account, this generating run and this request/version**; another same-account run's fence is not reusable proof. Internal Assist settlement uses its own captured run fence. `FenceResult = current | stale(source_edited|source_deleted|new_contact_message|takeover|owner_intent_superseded|request_changed|source_unavailable)`. Sources outside the output recipient are still dependencies if their content reached the generating model (D-S6).

## Persistence (structural only)

| Field / table | Type / nullability / scope | Retention and cascade / consent |
|---|---|---|
| conversations.content_revision | bigint NOT NULL DEFAULT 0, CHECK ≥0 | Same conversation row; monotonically advanced on meaningful retained input changes, never reset on purge. Account scoped. No content. |
| conversations.owner_intent_revision | bigint NOT NULL DEFAULT 0, CHECK ≥0 | Same row; increase on explicit owner task selection/revision/cancellation and manual takeover. Same account. |
| context_fences.id / account_id | bigint identity PK NOT NULL / bigint NOT NULL FK | Account CASCADE; new H10 table. Metadata ≤1 year from created_at, but keep references valid until linked pending work terminates; no retention extension for content. |
| context_fences.run_id | bigint NULL FK agent_runs ON DELETE SET NULL | Account-scoped run; legacy/null cannot grant owner authority. |
| context_fences.facts | jsonb NOT NULL, strict ContextFence | References/enums/counters/timestamp only; request/conv/source deletion invalidates and removes scoped refs. No text/hash of arbitrary model prose. |
| context_fences.created_at | timestamptz NOT NULL | UTC; metadata sweep. |
| actions.context_fence_id | bigint NULL FK context_fences ON DELETE SET NULL | Existing action row retention; NULL legacy means no revision proof for generated text; warn and offer exact-version owner approval under D-S17; autonomous work fails closed. |
| actions.requested_at | timestamptz NULL | Immutable owner-request time; proactive uses its captured trigger reference. Legacy uses existing conservative rule until regenerated, not a fabricated old request time. |
| actions.owner_request_id | bigint NULL FK owner_requests ON DELETE SET NULL | Account scoped; clear with request deletion; origin alone never proves explicit-current task. |

Metadata loaded into provider requires ai_control; any referenced content retains original managed class. Scope/managed/everything deletion cancels affected unclaimed work, removes fence links and clears existing derived actions.text under existing cascade. Physical account deletion cascades these fence records. No copied content table. H10 schema foundation can prepare independently; H4 accepts the typed seam while flag off, H10 runtime hooks land after H7 and **before candidate activation/release**, H8/H11 consume completed checks. **I2/I4 must wait for the merged H10 PR**, including content_revision and ingestion/edit/deletion/retention hooks; C7 cannot reject changes during a reader call without that foundation. Preparatory work alone does not satisfy this prerequisite. A stub/missing fence cannot pass.

## Revision rules

H10 review repair (2026-11-10): deleting any run fence first persists `agent_runs.sources_invalidated = true` through revision 0023's delete trigger. This content-free marker follows run retention/account deletion and survives scoped fence cleanup. An invalidated run cannot capture a replacement fence or stage a new artifact using surviving proof. Attention and period-summary tools persist observations alongside each managed read, both before and after inline model calls; changing recipient/read tool cannot drop an earlier dependency. No locks span those calls.

Legacy source-stale OUTDATED cards without a token/warning are explicitly selected by the card reconciler and upgraded to D-S17 choices. Superseded, privacy-cleared, rights-failed and autonomous cards are excluded. While an owner Re-draft is pending, a warned original retains all three choices. If the legacy worker produces no replacement, skips a closed window or fails, it restores the current warned original and sends a factual explanation; H8 separately owns full retained-request re-drafting.

| Change | Revision / invalidation |
|---|---|
| New retained contact/owner input; meaningful content edit; content deletion/retention purge | Increment content_revision once in ingestion/privacy transaction while conversation locked. Duplicate replay, identical edit and no-op deletion do not increment. Derived assistant send recording alone need not invalidate its own proposal; manual takeover always does. |
| Main-model multi-chat reads | Merge application read sets. Same chat read at differing revisions requires fresh consistent read or marks fence stale; never silently replace an old observed revision with newest. |
| Change during model call | Compare snapshot revisions under locks before action creation/revision; never label stale text current; owner-reviewable text gets all three D-S17 choices. Explicit selected older message also loaded directly even outside newest-30 window. |
| Change after card creation | Invalidate pending generated dependencies, including non-base source messages and other chats read by owner model. C6 conversation revision is conservative: any relevant change in captured chat can invalidate. At execution re-check revision and arrival watermark again; an owner tap approving the warned exact version overrides source freshness only. |
| Literal standalone owner copy | No model/chat-source dependency if none was used; copy/source/recipient/takeover checks still apply, then snapshot current arrival watermark under lock. Explicit reply target adds dependency. Never invent a target if selected source is gone. |
| Previously covered triage/monitor/summary source edited | I3/C7 marks prior verdict stale and puts changed ID back in review, including IDs behind cursor. Cannot call it current from watermark alone. |

## Settlement protocol and precedence

| Step | Required behavior |
|---|---|
| Prepare | Persist C1 request/source references and original requested_at; capture known manual-message id and observed revisions. Release transaction. |
| Generate | Main owner model (or scoped proactive Assist) reads consent-checked context; C7 allowance. No DB locks/network transaction. |
| Settle | Account SHARE → all dependency/recipient conversations UPDATE in sorted id order → request → action. Re-read consent, request CAS, source retention/proofs, revisions, manual messages, expiry, selected target and owner-intent counters. Run proposal policy using these facts. |
| Manual takeover | Any manual message with sent_at later than requested_at **or** a newly ingested manual ID beyond captured manual watermark overtakes generated work (conservative same-second/delayed-ingestion protection). Existing send-time inbox barrier remains. Do not reset request time on retry, clarification or action insert. Explicit new owner work creates a new reference. |
| Proactive settlement | Must match captured owner_intent_revision; cannot replace explicit owner request/card started before or during generation. Existing live explicit card wins even if older than background attempt; blocked background work never supersedes it. |
| Assigning owner intent | Bind each explicit selected request/revision to `owner_requests.intent_revision` (C1, H11 addition). A request's own initial selection advances and records its token **before its generation fence is finalized**; it must not invalidate itself merely by selecting its recipient. Later competing intent changes the conversation token and fails settlement; do not overwrite old content revisions to make a fence pass. |
| Owner settlement | Same-request repeated call follows C2 conflict rules. New explicit owner task may supersede older unclaimed card under the one-live-card constraint, with old/new IDs in receipts. Pending approved/executing work is handled by existing lifecycle/claim, never silently overwritten. |
| Lost race | For source changes on owner-reviewable work, present a warning with all three D-S17 choices; for autonomous work, discard the stale result. Keep request source facts and report exact reason. Re-read/recompute only within bounded operation budget; no blind loop. |
| Execution | Existing version/hash/token and committed-claim protocol plus updated pure freshness facts. No business retries on uncertainty; a fence is no approval. |

H11 policy facts add `request_reference_at: datetime|None` and captured manual watermark to the pure context; do not make `policy/` perform I/O. Cancellation/rejection that expresses owner intent advances the counter so a late Assist cannot revive the rejected suggestion for the same burst. A genuinely new contact burst may initiate fresh proactive work only under current intent/mode conditions.

Tests: edit/delete base **and non-base** source during/after generation; same-second manual reply; stale reply-to-version; older retained target; background starts before owner, finishes after; double settle; multi-chat dependency change; duplicate input no revision bump; foreign fence; no lock held while FakeLLM blocks. Preserve one-live-card, policy, consent, version and at-most-once regressions.
