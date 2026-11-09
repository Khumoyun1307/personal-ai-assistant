# C7 — Review coverage and shared operation budgets

Frozen seams for I2/I4/I6 and I15/I16. These repairs do not change model routing or authorize paid evaluations. The existing newest-first 60/30-message selection and 2,000-character clipping cannot certify complete review. Distinguish data unavailable to the bot from retained data not yet reviewed.

**Merge prerequisite:** H10 must merge before I2 or I4 (and therefore I6). Its C6 `conversations.content_revision` migration and ingestion/edit/deletion/retention update hooks do not exist in the baseline. Preparatory reader work may use this contract, but neither reader repair may merge against a stub revision or omit the during-call revision check. H10 retains one PR; its context hooks follow H7 in the shared-file lane. I3 separately implements reassessment behind an already-settled cursor.

## Review coverage model

```python
class ReviewPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: int
    next_char: int  # next unreviewed Python code-point offset
    content_revision: int


class ReviewRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_message_id: int
    last_message_id: int
    reason: Literal[
        "backlog",
        "partial_text",
        "source_changed",
        "deleted",
        "retention",
        "provider_failed",
        "invalid_output",
        "budget",
        "no_consent",
    ]


@dataclass(frozen=True)
class ReviewBatch:
    progress_id: int
    expected_version: int
    conversation_id: int
    snapshot_revision: int
    source_parts: tuple[MessagePart, ...]
    through: ReviewPosition | None
    complete_message_ids: tuple[int, ...]
    omitted: tuple[ReviewRange, ...]


@dataclass(frozen=True)
class MessagePart:
    message_id: int
    start_char: int
    end_char: int
    total_chars: int
    text: str
    source_hash: str


async def next_review_batch(
    session: AsyncSession, *, account_id: int, progress_id: int, max_messages: int, max_chars: int
) -> ReviewBatch: ...
async def settle_review(
    session: AsyncSession,
    *,
    account_id: int,
    batch: ReviewBatch,
    result: ValidatedReview | ReviewFailure,
    now: datetime,
) -> ReviewSettlement: ...
```

`ValidatedReview` = route-specific sanitized summary/triage/monitor result plus application-certified reviewed parts; `ReviewFailure` = fixed reason + retryability; `ReviewSettlement = applied | partial | stale | deferred | degraded`. Model output cannot certify unseen characters. `MessagePart.text` exists only in memory under managed consent; jobs carry progress/conversation IDs and enums only.

## Coverage persistence owned by I2/I4/I6

`review_progress` (new structural table, common tenant conventions):

| Column | SQL type / nullability | Meaning |
|---|---|---|
| `id`, `account_id` | bigint identity PK NOT NULL; bigint NOT NULL FK | Account scoped. |
| `conversation_id` | bigint NOT NULL FK conversations ON DELETE CASCADE | One conversation only. |
| `reader` | text NOT NULL CHECK summary/triage/monitor | Independent route coverage. |
| `monitor_id` | bigint NULL FK automations ON DELETE CASCADE | Required iff reader=monitor; scoped monitor covers this chat. |
| `scope_key` | text NOT NULL DEFAULT 'rolling' | `rolling` for background/triage/monitor; period summary uses application-computed `period:<first_id>:<last_id>` from its frozen requested range. UNIQUE NULLS NOT DISTINCT(account_id, conversation_id, reader, monitor_id, scope_key). No arbitrary prose. |
| `requested_range` | jsonb NULL | Period summary only: strict `{start_at: UTC datetime string\|None, end_at: UTC datetime string\|None, first_message_id: int\|None, last_message_id: int\|None}`. Snapshot-bounded range; NULL for rolling/triage/monitor. |
| `version` | integer NOT NULL DEFAULT 1 | Settlement CAS. |
| `position` | jsonb NULL, strict ReviewPosition | NULL = no reviewed retained text. A partial message is not a completed through-ID. |
| `source_revision` | bigint NOT NULL DEFAULT 0 | C6 revision for last successful settlement. |
| `pending_ranges` | jsonb NOT NULL DEFAULT '[]', list[ReviewRange] | Merge adjacent/overlapping intervals of same reason; bounded 64, collapse to wider conservative range on overflow, never lose unreviewed work. |
| `status` | text NOT NULL CHECK pending/current/partial/degraded/blocked | Blocked e.g. no consent; current only if complete and source revision current. |
| `attempts` | integer NOT NULL DEFAULT 0 | Nonnegative attempts for same failed batch, reset only on success/new work/explicit retry. |
| `retry_at` | timestamptz NULL | Bounded backoff trigger; no long future job lock. |
| `last_error` | text NULL CHECK fixed ReviewRange reason or refusal/truncated | Never provider exception text. |
| `created_at`, `updated_at` | timestamptz NOT NULL | UTC. |

| Retention / deletion / consent | Rule |
|---|---|
| Metadata retention | Rolling/triage: while source scope retained; active monitor: while active, ended ≤1 year after end. Period summary progress: existing 30-day period-cache lifetime. No source text retention extension; delete orphaned progress in hourly sweep. Independent period readers never advance a rolling/background cursor. |
| Cascades | Account/conversation/contact/all/managed deletion removes affected progress. Retention purge advances **availability gap**, not reviewed-through success; removing message rows cannot erase a missed-review warning. Restore invalidates current state under quarantine. |
| Consent | Provider loads require ai_control + processor-specific ai_managed. No consent ⇒ blocked/pending, no call, no successful cursor movement. Structural status can be displayed deterministically. |
| Existing verdicts | `conversation_triage` and `conversation_summaries` gain `review_progress_id bigint NULL FK ... ON DELETE SET NULL`, `review_version integer NULL` (both present/absent together). Existing content reasons/summary keep their retention/cascades; NULL legacy metadata is unproven coverage until recomputed. Update schema/content registry even though new fields are structural. |

## Contiguity, completeness and failures

| Case | Required behavior |
|---|---|
| Backlog exceeds batch limit | Load **oldest unreviewed** retained messages in ingestion ID order, present chronologically with IDs; repeatedly drain. Never set cursor to newest sampled ID past omitted retained data. Earlier context is labelled previously reviewed, not new coverage. |
| Several semantic monitors | Each monitor has its own progress/confirmation boundary. Batch together only those sharing the same review interval, or explicitly certify per-monitor complete subranges; process oldest pending groups first within C7 allowance. A newer/faster monitor's cursor cannot clear another's failed range. Positive evidence must be after that monitor's confirmation and inside its supplied new range. |
| Long message | Chunk whole retained text with exact start/end/total and bounded context overlap. Cursor advances only to first unreviewed character; message complete only when contiguous union covers `[0,total_chars)`. Deadline beyond char 2000 must be examined or explicitly pending. |
| Multi-chunk meaning | Preserve chronology and context; aggregate validated route results before certifying whole message. A prefix summary/triage/monitor result is partial, cannot assert global “no request/no match”. Deterministic keyword/media readers stay separate. |
| Negative verdict | Whole certified interval required; incomplete content cannot settle no-match/needs_reply=false for unseen remainder. A valid positive match may notify once with evidence before full drain; dedupe prevents duplicates. |
| Edit/deletion during call | C6 revision mismatch discards stale result; unchanged source subranges may be reused only with verified hashes, never guessed. |
| Edit behind cursor | Queue changed source ID/range for reassessment (I3), mark dependent verdict/task/attention stale immediately. Cursor position alone never establishes currentness. |
| Refusal/invalid/truncation/failure | No successful review movement. Retry at most 3 attempts per same batch (including existing one truncation retry as a call attempt); then degraded with recoverable pending range and owner-visible fixed reason. Never clear flag and call it successfully checked. |
| Budget/consent/provider outage | Keep durable pending interval; use bounded retry/admission, no paid hot loop. Distinguish active-with-review-gap from healthy current monitor. Explicit retry/manual inspection may resume. |
| One-shot monitor | Becomes completed only for validated evidence-backed positive; no-match failure cannot complete it. Preserve notification/event identity under I7, independently of digest. |
| Retention before review | Record deleted/expired coverage gap, no claim of complete review. Owner sees partial/gap; no history API assumption. |

No production default route cap changes in P0-2. Existing 60/30 message and 2000-character values become **batch/part limits**, never certification limits. Any reader prompt/schema change gets a new version through the steward and affected evals before shipping.

## Shared operation allowance (I15)

```python
class OperationBudget(Protocol):
    operation_id: UUID  # application UUID; shared by parent and child work
    deadline_at: datetime
    token_limit: int

    def remaining_seconds(self) -> float: ...
    def remaining_tokens(self) -> int: ...
    def check(self, *, stage: Literal["call", "tool", "stage", "retry"]) -> None: ...
    async def reserve_tokens(self, *, input_bound: int, output_bound: int) -> TokenLease: ...
    async def settle_tokens(
        self, lease: TokenLease, *, usage: Usage | None, uncertain: bool
    ) -> None: ...
    def cancel(self) -> None: ...
```

`TokenLease` contains operation_id, attempt UUID, input/output bounds, not content. Remaining allowance subtracts observed usage **and** all unsettled token reservations; child calls cannot each reserve the parent's full remainder. Exhaustion raises typed `OperationStopped(deadline|cancelled|tokens|monthly_budget|unpriced)`. Runtime creates the allowance; ToolContext/ToolServices and all nested summary/reader calls receive **the same object** (children may lower but never extend it). Each retry reserves anew; timeout=`min(route_timeout, remaining_seconds)`, recomputed before retry. Bound entire tool batches, serialization and post-model staging; stop before next mutation/card if cancelled/exhausted. Factual C3 reporting/finalization of already committed work remains allowed without another model call. A durable job restart gets an explicit new attempt budget, never replays committed effects; request metrics aggregate attempts and clarification turns (I13/I14). No model-owned deadline/counter.

## Priced admission (I16)

```python
@dataclass(frozen=True)
class CallAllowance:
    admission_id: int
    reserved_usd: Decimal
    input_bound: int
    output_bound: int


async def admit_call(
    session: AsyncSession,
    *,
    account_id: int,
    operation_id: UUID,
    run_id: int | None,
    attempt_id: UUID,
    model: str,
    input_bound: int,
    output_bound: int,
    now: datetime,
) -> CallAllowance | AdmissionDenied: ...
async def settle_call(
    session: AsyncSession,
    *,
    account_id: int,
    admission_id: int,
    outcome: CallOutcome,
    usage: Usage | None,
    actual_model: str | None,
    now: datetime,
) -> None: ...
```

`AdmissionDenied = no_consent | quarantined | monthly_budget | unpriced | invalid_bound`; `CallOutcome = not_dispatched | completed | definite_failure | billing_uncertain`. Shared wrapper order is consent/quarantine → operation check → atomic monthly reservation → provider attempt → usage settlement. Consent re-check immediately at actual call boundary; denied dispatch releases known-unused reservation. Every complete/parse route and nested call uses it; bootstrap only constructs wrappers.

`llm_admissions` (new structural table):

| Column | SQL type / nullability |
|---|---|
| `id`, `account_id` | bigint identity PK NOT NULL; bigint NOT NULL FK accounts ON DELETE RESTRICT (account-scoped accounting exception to the shared CASCADE convention) |
| `operation_id`, `attempt_id` | uuid NOT NULL; uuid NOT NULL UNIQUE(account_id, attempt_id) |
| `run_id`, `llm_call_id` | bigint NULL FKs agent_runs/llm_calls ON DELETE SET NULL, same account |
| `model`, `price_version` | text NOT NULL (validated configured identifier / static version, never arbitrary prose) |
| `month_start` | timestamptz NOT NULL, UTC month of reservation |
| `input_bound`, `output_bound` | bigint NOT NULL, CHECK ≥0 |
| `reserved_usd`, `charged_usd` | numeric(18,9) NOT NULL ≥0; numeric(18,9) NULL ≥0 |
| `status` | text NOT NULL CHECK reserved/dispatched/completed/released/billing_uncertain |
| `created_at`, `updated_at` | timestamptz NOT NULL |
| `error_code` | text NULL, fixed classification only |

Retention: reconciled accounting follows the existing ≤1-year accounting retention; an unresolved reservation remains held and visible until explicit reconciliation, never automatically freed at timeout/month rollover or by content deletion. `delete_everything` preserves consents and accounting today: it must also preserve the minimal admission identity, account/month, attempt, reservation, status and settlement fields above, including an in-flight or billing-uncertain attempt. Scoped/content deletion removes source/run linkage where applicable (`run_id`/`llm_call_id` may become NULL), without releasing allowance, deleting the settlement identity or recreating it under a new attempt. No content is retained for accounting. Physical account removal is blocked while retained admissions remain; do not cascade them away. I16 documents accounting retention/export/deletion and eventual account-removal handling before code; it cannot silently remove outstanding liabilities. No additional provider consent class or processor: accounting settlement requires no content/provider call and remains possible after consent withdrawal.

| Admission rule | Required behavior |
|---|---|
| Unknown price | Fail production admission; no `None` → free cost. Conservative bound permitted only with a recorded owner-approved price definition. Verify official prices at implementation/eval time; no invented price for D-S7 candidate. |
| Bounds | Conservative input-token bound includes all serialized context/tools/cache charged at full price unless verified discount; output bound includes allowed generation/reasoning. No unbounded SDK retry. Disable implicit retries or account for every separately admitted attempt. |
| Concurrent work | Serialize reservation under account budget lock; sum reconciled charges + all outstanding reservations, without double-counting linked llm_calls. Account → admission order, commit before provider. Two jobs cannot both spend same remaining USD. |
| Uncertain billing/crash | Mark dispatched reservation uncertain; hold full reserve, conservative token charge. No automatic release or replay. Late response reconciles same attempt once. |
| Deletion during dispatch | Serialize deletion/link cleanup with settlement using the account → admission order. Once dispatched, content deletion or consent withdrawal cannot classify the call as unused. Retain the same admission/attempt and reserve; late response settles once even after source/run linkage is removed. Missing usage stays billing_uncertain; a second admission cannot spend that held allowance. A pre-dispatch release requires proof no provider attempt started. |
| Actual usage | Reconcile exactly once; lower actual frees difference. If actual exceeds a purported bound, record true cost, block further admission and surface bound failure; never hide overrun. Zero discretionary overshoot by default; any permitted bounded exception requires owner decision. |
| Month boundary | Outstanding earlier-month reservations remain held in that month; no timeout reset; usage reconciles against admission month with visible late accounting. |

Tests use FakeClock/FakeLLM and concurrent PostgreSQL transactions: oldest backlog drain/restart, tail-only evidence, invalid monitor output, edited covered source, edit/delete during paused reader call rejects stale settlement; nested calls plus truncation retry, cancelled multi-tool batch, two admissions for last allowance, unpriced model, unknown billing, late settlement. **Deletion-during-dispatch regression first:** pause FakeLLM after dispatch, run delete_everything/scoped deletion and consent withdrawal, verify source content/link cleanup but the same admission/reserve survives, deny competing spend, then settle a late response exactly once (or retain billing_uncertain if usage is unknown). Include physical account deletion restriction and accounting sweep/export tests. No real API runs. Update architecture §7.2/7.4/§10/§12/§14/§17 and affected P2 specs when implemented.
