# C3 — Action receipts and artifact-grounded reporting (Q3/H7)

The existing `LiveCardFact`/`live_cards` snapshot is useful but excludes terminal cards and reasons. Extend it; do not describe current code as having no card context. All facts below are **content-free**. Text, names, previews and model-written descriptions belong only in explicit artifact reads.

```python
Presentation = Literal[
    "not_attempted", "in_flight", "shown", "not_delivered", "uncertain", "unknown"
]


@dataclass(frozen=True)
class ActionReceipt:
    action_id: int
    conversation_id: int
    request_id: int | None
    state: ActionState
    state_reason: StateReason | None
    version: int
    authorship: AuthorshipKind | None
    mode: Literal["reply", "new_message", "revise"] | None
    answered_message_id: int | None
    text_available: bool
    presentation: Presentation
    presented_version: int | None
    presented_state: ActionState | None
    replaced_by_action_id: int | None
    observed_at: datetime


@dataclass(frozen=True)
class ReceiptPage:
    receipts: tuple[ActionReceipt, ...]
    omitted_live: int
    omitted_terminal: int


async def action_receipts(
    session: AsyncSession,
    *,
    account_id: int,
    request_ids: tuple[int, ...],
    conversation_ids: tuple[int, ...],
    action_ids: tuple[int, ...],
    now: datetime,
) -> ReceiptPage: ...
async def refresh_receipts(
    session: AsyncSession, *, account_id: int, action_ids: tuple[int, ...], now: datetime
) -> ReceiptPage: ...
```

## Which facts each owner turn receives

| Priority | Inclusion rule |
|---|---|
| Explicit artifact | Reply-to-selected action/version, pending revision, active request linkage and all action IDs referenced by this turn; include terminal/uncertain regardless of age, if retained. Never silently drop for a display cap. |
| Relevant conversations | Their current live cards plus their most recent terminal/uncertain action in the last 24 h. “Conversation” here is the selected business conversation; the root control dialogue may refer to several. |
| Recent control context | Actions linked to retained supplied history/request sources and newly produced by tools this run; include recent terminal state and supersession link. |
| Other live work | Account's existing bounded live-card list as fallback. Maximum 50 loaded/20 displayed may remain for fallback; report omitted counts. Explicit items take priority; if references themselves exceed cap, ask for narrower selection rather than falsely asserting completeness. |

Withhold managed-associated facts when the current managed-tool gate is closed, as current PF-15 does; return fixed access-off metadata without names/text. Structural receipt alone does not taint. `get_draft` is still a managed read; text is wrapped untrusted even for a dictated card (conservative read semantics).

## Freshness and final answer

| Point | Required behavior |
|---|---|
| Start | Inject current receipts beside C1 request facts, outside untrusted prose. Mark history as historical, not current state. |
| Proposal/revision/read result | Return observed action/version/reason; `requested_change_applied` distinguishes actual stage from conflict (C2). Never say “card shows it” before presentation evidence. |
| No-action proposal result | C2 `ProposalReceipt.action=NULL` means no artifact receipt is available for that result. Missing-content/invalid-source validation creates no action; narrate the fixed reason or question, never invent an action ID/version/state/presentation. Other live cards remain separate facts. Refresh only actual referenced/touched action IDs. |
| Before final status narration | Re-read all actions touched/referenced in the run. If a model final answer was formed from older facts, discard that **unsent final answer** and append the fresh receipts for a bounded correction call, or use deterministic receipt-only wording on exhaustion. Do not rewrite existing provider items. |
| Last race | Deterministic consequential status line uses the final re-read and says “as of” when needed. A later transition can always occur; never promise timeless state. No extra provider call on revoked consent/exhausted budget. |
| Content claim | Must call `get_draft(action_id)` or equivalent account-scoped artifact read and bind the result to the claimed version. Main-model authored text or historical assistant prose alone is not proof of the stored body. If version changes after read, re-read or omit claim. Purged text ⇒ text unavailable, never reconstruct from memory. |
| Unknown outcomes | Report unknown/missing distinctly; absence from live list does not imply sent, rejected or deleted. |

| Action / presentation | Permitted consequential wording |
|---|---|
| awaiting_approval + shown current version/state | Awaiting your approval. |
| awaiting_approval + not_attempted/in_flight | Proposal stored; card presentation pending. |
| awaiting_approval + not_delivered/uncertain | Approval card not delivered / delivery uncertain; recovery available through I27. No automatic replay of uncertain presentation. |
| approved/executing | Approved / sending attempt in progress; never sent. Respect sending-disabled outcome. |
| succeeded | Telegram confirmed sent (identity may separately be pending). |
| failed/expired/blocked/outdated/rejected/cancelled | Exact state + fixed state_reason; distinguish not delivered from obsolete text. |
| delivery_uncertain | Delivery uncertain; check chat; no automatic resend. |

## Presentation evidence and persistence seam

Q5 distinguishes successful edit / narrow not-modified equivalence from parse/missing-message refusal. A pointer or `card_state` alone never proves the current version was shown. Existing unresolved `approval_presentation_claimed` without definitive resolution yields in_flight until attempt timeout, then uncertain; it is not a successful shown receipt. `shown` requires full body plus current recipient/version controls, including split control-chat presentations.

H2/H7 extend existing **structural audit details** (no new table) with a `card_presented` event: `action_id: int`, `version: int`, `state: ActionState`, `control_message_id: int`, `claim_id: int|None`. Record successful current-version presentations/edits, not refusals; each message ID/version association remains resolvable account-locally. When edited in place, resolve a reply to that message to its latest successfully presented version; when replaced, keep the old association for stale detection. Pre-contract historical cards resolve only through the current action pointer; unavailable older mappings yield unavailable, not a guessed card. Full-text companion messages may carry the same association with `part: full_text|controls`.

| Storage | Type / nullability | Retention / deletion / consent |
|---|---|---|
| Audit keys above | Required ints/enums, nullable claim_id; strict allow-list JSON | Existing audit row 1 year; account scoped. Scope/everything purge removes mapping references as required; no retained source content. ai_control; managed read gates apply when resolving related artifacts. |
| ActionReceipt / ReceiptPage | In-memory typed values | No durable receipt cache. Do not copy text into history/trace/job metadata. I25 may retain bounded IDs/codes under audit retention. |

Regression first: no-action missing-content/invalid-source result produces truthful question/failure without invented card facts; terminal disappearance; state changes between tool result and final; changed version after content read; no text/name in injected receipts; parse refusal not shown; unresolved split presentation; foreign account reply mapping; explicit recovery never replays business send. Future I13 may link delivered assistant turns to trigger IDs; this contract does not claim it is already implemented.
