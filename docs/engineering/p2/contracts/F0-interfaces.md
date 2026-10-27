# F0 contract — interfaces between the P2 work units

*Normative. F0 (Neryn) creates every type, constant, protocol and registration point below — with real implementations where marked **F0**, and with signatures only where a later unit implements them. Units build against these names. After CP1, a change is an interface change request (ORG §5). Paths are under `src/assistant/` unless stated. Layering (`.importlinter`, AGENTS §5) is binding: domain packages (`accounts`, `conversations`, `policy`, `actions`, `memory`, `inbox`, `automation`, `privacy`) never import `aiogram`, `openai`, `procrastinate`, `telegram`, `llm`, `jobs` or `agent`; `policy` additionally never imports `sqlalchemy` or `db`, **directly or indirectly**; only `bootstrap.py` instantiates adapters.*

## 1. Module map

| Module | Created by | Implemented by | Purpose |
|---|---|---|---|
| `core/reply_window.py` | F0 | F0 | Pure 24-hour window maths (P2-T02) |
| `core/delivery.py` | F0 | F0 | `ErrorClass`, `SendResult` moved here (P2-T03) |
| `core/jobs.py` (additions) | F0 | F0 | P2 task names, lock helpers, `DraftReason` (§6) |
| `conversations/listeners.py` | F0 | F0 | `IngestionListener`, `MessageChange`, `DeletionChange` (§5) |
| `actions/models.py` | F0 | F0 | `Action` ORM |
| `actions/states.py` | F0 | F0 | States, transitions, reasons, UI labels (§2) |
| `actions/lifecycle.py` | F0 | F0 (bulk helpers) + S04.1 (single-action transitions) | §8.1 |
| `actions/ports.py` | F0 | — (protocols) | `BusinessSender`, `ApprovalCardPresenter` (§4, §8.2) |
| `actions/config.py` | F0 | F0 | `ActionsConfig` (from `Settings`) |
| `actions/proposals.py` | F0 (signature) | S04.1 | `propose(...)` (§8.1) |
| `actions/policy_loader.py` | — | S04.1 | Loads a `PolicyContext` from locked rows |
| `actions/executor.py` | — | S04.4 | At-most-once execution (ARCHITECTURE §9.4) |
| `actions/ingestion.py` | — | S04.2 | `ActionsIngestionListener` |
| `actions/drafts.py` | F0 | F0 (`assist_trigger`), S04.2 (`request_draft`) | `assist_trigger(flags) -> "off" \| "ingestion" \| "triage"` — **the single switch** (§10), and `request_draft(...)` (defers `replies.draft`), used by S04.2, S04.3 (Re-draft) and S05.1 |
| `actions/activity.py` | — | S04.5 | Activity read model |
| `policy/context.py` | F0 | F0 (types only) | §3 |
| `policy/rules.py`, `policy/engine.py` | — | S04.1 | Rules and `decide` |
| `agent/draft_schemas.py`, `agent/draft_context.py`, `agent/drafting.py`, `agent/prompts/draft_reply.v1.md` | — | S04.2 | `draft_reply` run |
| `agent/tools/replies.py` | — | S04.2 | `propose_reply`, `propose_message` |
| `agent/tools/autonomy.py` | — | S04.4 | `pause_autonomy`, `resume_autonomy` |
| `agent/tools/activity.py` | — | S04.5 | `get_activity` |
| `telegram/control/routes.py` | F0 | F0 | Callback / pending-input / command registries (§7) |
| `telegram/control/approvals.py` | — | S04.3 | Approval cards, presses, edit input, card sync, `ApprovalCardPresenter` |
| `telegram/control/activity.py` | — | S04.5 | `/activity` |
| `telegram/business_targets.py` | — | S04.4 | Resolves the current connection id and chat id for a `BusinessChat` |
| `jobs/tasks_actions.py` | F0 (empty `register_actions_tasks`) | S04.2–S04.4 | M04 job registration |
| `jobs/reconcile_actions.py` | — | S04.2, S04.4 | M04 reconcilers (§6.3) |
| `inbox/models.py` (additions) | F0 | F0 | `Task`, `ConversationTriage`, `Digest`, `Notification` ORM |
| `inbox/notification_types.py` | F0 | F0 | `NotificationKind`, `Priority`, `AlertClass`, `NotificationRefs` (§8.3) |
| `inbox/notifications.py` | F0 (signature) | S05.2 | `enqueue(...)`, delivery |
| `inbox/notification_policy.py` | — | S05.2 | Pure delivery decision |
| `inbox/tasks.py`, `inbox/triage_store.py`, `inbox/triage_trigger.py` | — | S05.1 | Tasks, verdicts, triage listener |
| `inbox/digest.py` | — | S05.3b | Deterministic digest |
| `automation/models.py` | F0 | F0 | `Automation`, `AutomationOccurrence` ORM |
| `automation/rules.py` | F0 (`RuleBase`) | S05.3b (`ScheduleRule`), S05.4 (`MonitorRule`) | Persisted rule models with `schema_version` |
| `automation/repository.py` | F0 | F0 | Account-scoped CRUD (§8.4) |
| `automation/schedule.py` | — | S05.3a | Pure next-occurrence maths |
| `automation/schedules.py` | — | S05.3b | Reconciler, fire, occurrence ledger |
| `automation/monitors.py`, `automation/monitor_trigger.py` | — | S05.4 | Monitor matching, listener |
| `agent/triage_schemas.py`, `agent/triage_context.py`, `agent/triage.py`, `agent/prompts/triage.v1.md` | — | S05.1 | `triage` run |
| `agent/monitor_schemas.py`, `agent/monitor_eval.py`, `agent/prompts/evaluate_monitor.v1.md` | — | S05.4 | `evaluate_monitor` run |
| `agent/tools/monitors.py` | — | S05.4 | `create_monitor`, `list_monitors`, `stop_monitor` |
| `agent/tools/priority.py` | — | S05.2 | `set_contact_priority` |
| `telegram/control/alerts.py` | — | S05.2 | Alert rendering, delivery, feedback press |
| `telegram/control/monitors.py` | — | S05.4 | Monitor card presses |
| `telegram/control/digest.py` | — | S05.3b | `/digest`, digest rendering |
| `jobs/tasks_inbox.py`, `jobs/reconcile_inbox.py` | F0 (empty registration) | S05.1, S05.2 | M05 job registration and reconcilers |
| `jobs/tasks_automation.py` | F0 (empty registration) | S05.3b, S05.4 | Automation and monitor jobs |

## 2. Actions: states, transitions, reasons (`actions/states.py`, F0)

```python
class ActionKind(StrEnum):
    SEND_MESSAGE = "send_message"
    EDIT_MESSAGE = "edit_message"
    DELETE_OWN = "delete_own"


class ActionOrigin(StrEnum):
    OWNER_DICTATED = "owner_dictated"
    AGENT_PROPOSAL = "agent_proposal"
    DELEGATION_AUTO = "delegation_auto"


class ActionState(StrEnum):
    PROPOSED
    AWAITING_APPROVAL
    REVISING
    APPROVED
    SCHEDULED
    EXECUTING
    SUCCEEDED
    FAILED
    DELIVERY_UNCERTAIN
    REJECTED
    OUTDATED
    EXPIRED
    CANCELLED
    BLOCKED
```

`TRANSITIONS: frozenset[tuple[ActionState, ActionState]]` is ARCHITECTURE §9.4's diagram (including `scheduled`, used only by policy tests in P2) **plus `executing → expired`** (a `WINDOW_CLOSED` send result) **and `proposed → cancelled`** (R4; S04.4 updates the diagram). `actions/states.py` and `actions/__init__.py` import only the standard library, so `policy` may import `assistant.actions.states` (R5; import-linter contract `actions-states-is-pure` forbids `sqlalchemy` and `assistant.db`). Derived sets:
- `LIVE_CARD = {awaiting_approval, revising}` (the partial unique index).
- `PENDING = {proposed, awaiting_approval, revising, approved, scheduled}` (cancellable; never been sent).
- `TERMINAL = {succeeded, failed, rejected, outdated, expired, cancelled, blocked}`.
- `delivery_uncertain` is settled for the owner but may still move to `succeeded`/`failed` from a late result **of the same attempt** (Tx B). Nothing ever moves it back to `approved` or `executing`.

Every state change goes through one function that checks the pair against `TRANSITIONS` and uses a conditional `UPDATE … WHERE id = :id AND state = :from [AND version = :v]` (no read-modify-write), sets `updated_at`, writes one content-free audit row (§11), and returns whether it applied. A pair not in the table raises (a bug).

`StateReason` codes (stored in `actions.state_reason`; each has a fixed owner-facing explanation in one table in `actions/states.py`, English in P2, P2-O11): `superseded`, `new_contact_message`, `takeover`, `owner_edit`, `source_edited`, `source_deleted`, `rights_changed`, `connection_disabled`, `connection_invalid`, `ignore_mode`, `data_deleted`, `consent_revoked`, `ingest_error`, `restored_from_backup`, `window_closed`, `max_age`, `owner_rejected`, `policy_denied`, `sending_disabled`, `too_long`, `autonomy_paused`, `sent`, `not_delivered`, `ambiguous`, `stale_executing`, `late_result`.

**Owner-facing labels** (FR-ACT-02: drafts and sent messages always visually distinct): awaiting approval → "Draft — awaiting your approval"; revising → "Revising…"; approved/executing → "Sending…"; succeeded → "Sent ✓ <local time>"; failed → "Not sent — <reason>"; delivery_uncertain → "Delivery uncertain — please check the chat"; outdated → "Outdated — <reason>"; expired → "Expired — <reason>"; rejected → "Rejected"; cancelled → "Cancelled — <reason>"; blocked → "Not sendable — <reason>". Wording may be polished by the owning unit; the distinctions may not.

## 3. Policy (`policy/context.py` types by F0; `policy/rules.py` + `policy/engine.py` by S04.1)

`policy` imports only the standard library, `core` and `assistant.actions.states` (P2-T02, R5). The window maths it needs is `core/reply_window.py`:

```python
REPLY_WINDOW = timedelta(hours=24); SAFETY_MARGIN = timedelta(minutes=5)
def closes_at(anchor: datetime | None) -> datetime | None
def is_open(anchor: datetime | None, now: datetime) -> bool   # anchor = last contact message date
```
`conversations/window.py` keeps its public functions and delegates to these (single place, AGENTS §7).

```python
class Phase(StrEnum): PROPOSAL = "proposal"; EXECUTION = "execution"
class Outcome(StrEnum): ALLOW_AUTO = "allow_auto"; REQUIRE_APPROVAL = "require_approval"; DENY = "deny"; WAIT = "wait"  # P2-T06

@dataclass(frozen=True, slots=True)
class AccountSnapshot:      account_id: int; status: str; autonomy_paused: bool; managed_consent_ok: bool
@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:   exists: bool; status: str | None; is_enabled: bool; can_reply: bool
@dataclass(frozen=True, slots=True)
class ConversationSnapshot: conversation_id: int; account_id: int; mode: str; availability: str; is_bot: bool
                            last_contact_msg_at: datetime | None; last_owner_msg_at: datetime | None
                            autonomy_paused_reason: str | None
@dataclass(frozen=True, slots=True)
class Freshness:            newest_owner_manual_sent_at: datetime | None   # max sent_at of owner_manual rows
                            newest_contact_message_id: int | None          # max messages.id of contact rows
                            base_message_sent_at: datetime | None
                            base_message_present: bool                     # not deleted/purged
                            target_is_assistant_sent: bool | None          # edit/delete only; None otherwise
@dataclass(frozen=True, slots=True)
class IngestionState:       pending_inbound: int; barrier_passed: bool
    # barrier_passed (R1): system_state['ingestion_barrier'] holds {"process_run_id", "caught_up_at"}.
    # bootstrap.running_process (F0) generates process_run_id and commits it with caught_up_at = null
    # BEFORE any role starts; the poller sets caught_up_at for that run id when a poll returns fewer than
    # `limit` updates (S04.4). Passed <=> stored run id == this process's run id AND caught_up_at is set.
    # A process without the poller role never passes (fail closed).
@dataclass(frozen=True, slots=True)
class ActionFacts:          action_id: int | None; kind: ActionKind; origin: ActionOrigin; state: ActionState
                            version: int; text_length: int; created_at: datetime; expires_at: datetime | None
                            shown_through_message_id: int | None; owner_retry: bool
@dataclass(frozen=True, slots=True)
class PolicyContext:        now: datetime; phase: Phase; account: AccountSnapshot; connection: ConnectionSnapshot
                            conversation: ConversationSnapshot; freshness: Freshness; ingestion: IngestionState
                            action: ActionFacts; ai_generated: bool; sending_enabled: bool
                            delegation: None = None    # M06 adds DelegationSnapshot
@dataclass(frozen=True, slots=True)
class CheckResult:          name: str; passed: bool; reason: str          # reason = a StateReason-style code
@dataclass(frozen=True, slots=True)
class Decision:             outcome: Outcome; checks: tuple[CheckResult, ...]
def decide(ctx: PolicyContext) -> Decision        # S04.1; pure, total, never raises on valid input
```

Rule names (fixed; S04.1 implements them per ARCHITECTURE §9.3 and the S04.1 spec): `account_active`, `connection_active`, `conversation_owned`, `conversation_available`, `window_open`, `no_takeover_since`, `not_stale` (execution), `not_already_executed` (execution), `not_expired` (execution), `inbox_drained` (execution → `WAIT`), `target_is_assistant_sent` (edit/delete only), `length_ok`, `consent_valid`, `sending_enabled` (execution only, P2-T21), `autonomy_not_paused` (autonomous origin only), `delegation_active` (autonomous origin only; always fails in P2).

Execution-phase outcome → state (S04.4 applies it in Tx A): failed `no_takeover_since` → `outdated`/`takeover`; `not_stale` → `outdated`/`new_contact_message`; `window_open` → `expired`/`window_closed`; `not_expired` → `expired`/`max_age`; `not_already_executed` → no change (someone else claimed it); `inbox_drained` → stays `approved`, job re-deferred in 3 s; anything else → `blocked`/the rule's reason. At execution, `ALLOW_AUTO` means "may send now" for **every** origin (an owner-approved action already carries its approval); `REQUIRE_APPROVAL` is a proposal-phase outcome only. Proposal phase: any failed send precondition → `blocked` (the card offers copy-ready text, FR-DRF-06); otherwise `REQUIRE_APPROVAL` (no `ALLOW_AUTO` can arise in P2: no delegations).

## 4. Delivery and business sending

**`core/delivery.py` (F0, P2-T03):** `ErrorClass` (every current member, including `OK`, `RATE_LIMITED`, `WINDOW_CLOSED`, `PEER_UNAVAILABLE`, `CONNECTION_INVALID`, `CHAT_UNREACHABLE`, `FORBIDDEN`, `BAD_REQUEST`, `CONFLICT`, `AMBIGUOUS`, `TRANSIENT` — move the enum unchanged) and `SendResult` (unchanged fields and properties `ok`, `delivery_uncertain`) move here; `telegram/errors.py` and `telegram/gateway.py` re-export them so existing imports and tests keep working.

**`actions/ports.py` (F0):**
```python
class BusinessSender(Protocol):  # implemented by telegram/gateway.py OutboundGateway (S04.4)
    async def send_business_text(
        self, *, account_id: int, conversation_id: int, text: str
    ) -> SendResult: ...
    async def edit_assistant_message(
        self, *, account_id: int, conversation_id: int, assistant_message_ref: int, text: str
    ) -> SendResult: ...
    async def delete_own_business_messages(
        self, *, account_id: int, conversation_id: int, assistant_message_refs: Sequence[int]
    ) -> SendResult: ...
```
Gateway semantics (S04.4): resolves the account's **current** `business_connection_id` and the conversation's `business_chat_id` at call time (`telegram/business_targets.py`, a short read); refuses locally — no Telegram call, `ErrorClass.BAD_REQUEST`, `error_code = "local_target_refused"` — when the connection is missing, not `active` or not enabled, or the conversation is not the account's; sends plain text (no parse mode), never split; per-chat throttle; classifies errors with `telegram/errors.py`; never logs text or URLs; `edit_assistant_message`/`delete_own_business_messages` refuse (locally, same code) any ref that is not an `assistant_sent` row of that conversation (RG-12). `send_text(BusinessChat, …)` keeps raising — business sends go only through `send_business_text`, called only by `actions/executor.py`.

## 5. Ingestion listeners (`conversations/listeners.py`, F0 — P2-T04)

```python
@dataclass(frozen=True, slots=True)
class MessageChange:
    account_id: int
    conversation_id: int
    message_id: int | None
    direction: Direction
    effect: Effect
    is_edit: bool
    metadata_only: bool
    mode: str
    sent_at: datetime
    content_changed: bool

    @property
    def new_message(self) -> bool: ...  # effect in (INSERTED, RESURFACED) and not is_edit


@dataclass(frozen=True, slots=True)
class DeletionChange:
    account_id: int
    conversation_id: int
    message_ids: tuple[int, ...]  # messages.id whose content this deletion removed
    assistant_sent_ids: tuple[int, ...]  # the subset that were assistant_sent
    metadata_only: bool


class IngestionListener(Protocol):
    async def on_message(self, session: AsyncSession, change: MessageChange) -> None: ...
    async def on_deletion(self, session: AsyncSession, change: DeletionChange) -> None: ...
```

- `BusinessPipeline` (F0) takes `listeners: Sequence[IngestionListener]` (built in `bootstrap.py`) and calls each, in order, after `record_message` / `record_deletion` succeeded, in the same transaction. Order: actions (M04), triage (M05), monitors (M05). `record_message`'s steps 1–5, the takeover pause and the summary trigger are unchanged.
- `record_message`'s outcome and `record_deletion`'s outcome gain the fields needed to build these changes (F0).
- **Listener rules:** no business-chat call, no model call, no commit; only writes in the given session and `db.uow.after_commit` callbacks — job defers and control-chat card updates through `ApprovalCardPresenter.sync_after_commit` (R16); the conversation row is already locked `FOR UPDATE` — lock only rows later in the §9.4 order (delegation, action); idempotent (an `inbox requeue` re-processes an update); fast (the inbox processor must stay quick, ARCHITECTURE §6.2); an exception fails the row like any handler error, so validate defensively.

## 6. Jobs (`core/jobs.py` constants by F0; tasks by the owning units)

### 6.1 Task catalogue (P2 additions)

| Constant | Task name | Args (ids/enums only) | Queue | Lock / queueing lock | Scheduling | Retry | Owner |
|---|---|---|---|---|---|---|---|
| `DRAFT_TASK` | `replies.draft` | `conversation_id: int`, `reason: str` (`DraftReason`) | `llm` | qlock `draft:{conversation_id}` | `schedule_at` = now + `DRAFT_DEBOUNCE_SECONDS`; trailing debounce by re-defer | none (reconciler recovers via `pending_draft_at`) | S04.2 |
| `REVISE_TASK` | `replies.revise` | `action_id: int`, `turn_id: int` | `llm` | qlock `revise:{action_id}` | immediate | none | S04.3 |
| `EXECUTE_TASK` | `actions.execute` | `action_id: int` | `outbound` | none (Tx A row locks order it) | immediate; re-deferred in 3 s on `WAIT`; at `retry_at` after a 429 | only DB-transient errors **before** Tx A commits; never after the gateway call; never `retry=True` | S04.4 |
| — | `system.expire_actions` | periodic | `system` | qlock = name | every minute | — | S04.3 |
| — | `system.sync_action_cards` | periodic | `system` | qlock = name | every minute | — | S04.3 |
| — | `system.mark_uncertain` | periodic | `system` | qlock = name | every minute | — | S04.4 |
| `TRIAGE_TASK` | `inbox.triage` | `conversation_id: int` | `llm` | qlock `triage:{conversation_id}`; no lock | `schedule_at` = now + `TRIAGE_DEBOUNCE_SECONDS`; trailing debounce | none (reconciler via `pending_triage_at`) | S05.1 |
| `NOTIFY_TASK` | `notify.deliver` | `notification_id: int` | `outbound` | qlock `notify:{notification_id}` | immediate or at `deliver_after` | bounded, transient only; an uncertain send is never retried | S05.2 |
| `FIRE_TASK` | `automation.fire` | `automation_id: int`, `occurrence_at: int` (epoch seconds UTC) | `outbound` | qlock `auto:{automation_id}:{occurrence_at}` | immediate (the reconciler defers when due) | bounded, transient only | S05.3b |
| `DIGEST_TASK` | `digest.build` | `account_id: int`, `period_end: int` (epoch seconds) | `llm` | qlock `digest:{account_id}:{period_end}` | deferred by `automation.fire` or `/digest` | none (the `digests` row dedupes) | S05.3b |
| `MONITOR_TASK` | `monitors.evaluate` | `conversation_id: int` | `llm` | qlock `mon:{conversation_id}` | `schedule_at` = now + `MONITOR_DEBOUNCE_SECONDS`; trailing debounce | none (reconciler via `pending_monitor_at`) | S05.4 |
| — | `system.reconcile_schedules` | periodic | `system` | qlock = name | every minute (also keeps each account's digest automation in step with `digest_time`) | — | S05.3b |
| — | `system.expire_tasks` | periodic | `system` | qlock = name | hourly | — | S05.1 |
| — | `system.expire_monitors` | periodic | `system` | qlock = name | hourly | — | S05.4 |

`class DraftReason(StrEnum): ASSIST_INGESTION = "assist_ingestion"; TRIAGE_NEEDS_REPLY = "triage_needs_reply"; REDRAFT = "redraft"; OWNER_REQUEST = "owner_request"; EVAL = "eval"` — the last two name the `Drafter`'s trigger (`propose_reply`, the E2 target) and are never `replies.draft` job arguments.

**Queueing locks and transactions:** a defer with a `queueing_lock` that conflicts raises a unique violation inside Postgres; `ProcrastinateScheduler.defer` catches `AlreadyEnqueued` and returns `None`, but a transaction passed as `connection` is already aborted by then (the S03.2 lesson in `SummaryRefreshTrigger`). So a queueing-locked defer either runs **after commit** (`db.uow.after_commit`, with a domain flag and a reconciler to recover a lost defer) or, when it must commit atomically with the domain write, **inside a SAVEPOINT** (`session.begin_nested()`), so a conflict rolls back only the savepoint. Defers without a queueing lock (e.g. `actions.execute` on Approve) may run in the transaction directly.

AGENTS §13 applies to all: payloads carry ids and enums only; handlers reload state and re-check conditions (consent, budget, mode, flags, the action's state) and are safe to run twice; no procrastinate `lock` on a future-scheduled job; no row lock held across a model or Telegram call.

### 6.2 Registration (F0, P2-T05)

`jobs/tasks.py::register_tasks(app, handlers)` keeps the existing tasks and then calls `register_actions_tasks(app, handlers.actions)`, `register_inbox_tasks(app, handlers.inbox)`, `register_automation_tasks(app, handlers.automation)` from `jobs/tasks_actions.py`, `jobs/tasks_inbox.py`, `jobs/tasks_automation.py`. F0 creates these with empty bodies and adds optional handler groups to `JobHandlers` (`actions: ActionsJobHandlers | None = None`, `inbox: …`, `automation: …`), each defined in its module. A group left `None` registers nothing.

### 6.3 Reconcilers (F0, P2-T05)

```python
@dataclass(frozen=True, slots=True)
class DeferRequest:
    task: str
    args: JobArgs
    queueing_lock: str | None = None
    lock: str | None = None
    schedule_at: datetime | None = None


Reconciler = Callable[[AsyncSession, datetime], Awaitable[list[DeferRequest]]]
```
`JobHandlers.reconcilers: tuple[Reconciler, ...] = ()`. `reconcile_pending` runs its existing steps unchanged, then each reconciler inside the same transaction, and defers the collected requests **after** the commit (a queueing-lock conflict means a job is already queued and is not an error). Reconcilers live in `jobs/reconcile_*.py` (they may read `procrastinate_jobs`). P2 reconcilers: drafts with `pending_draft_at` older than 30 s and no queued/doing `replies.draft` (S04.2); `approved` actions due (no `retry_at` in the future) with no queued/doing `actions.execute` (S04.4); triage flags (S05.1); monitor flags (S05.4); notifications `queued` for more than 30 s and due (S05.2).

## 7. Control chat (`telegram/control/routes.py`, F0)

```python
CallbackHandler = Callable[
    [AsyncSession, OutboundGateway, Clock, JobScheduler | None, Account, CardPress],
    Awaitable[HandlerResult],
]
PendingInputHandler = Callable[
    [
        AsyncSession,
        OutboundGateway,
        Clock,
        JobScheduler | None,
        Account,
        IncomingMessage,
        PendingInput,
    ],
    Awaitable[HandlerResult],
]
CommandHandler = Callable[
    [AsyncSession, OutboundGateway, Clock, JobScheduler | None, Account, IncomingMessage, str],
    Awaitable[HandlerResult],
]


@dataclass(frozen=True, slots=True)
class ControlRoutes:
    callbacks: Mapping[str, CallbackHandler] = field(default_factory=dict)
    pending_inputs: Mapping[str, PendingInputHandler] = field(default_factory=dict)
    commands: Mapping[str, CommandHandler] = field(default_factory=dict)
```
`ControlDispatcher` receives a `ControlRoutes` (built in `bootstrap.py`). The existing branches stay as they are; the registry is consulted **after** them for callback kinds, pending-input kinds and commands they do not handle. Precedence rules 1–4 are unchanged: a command cancels a pending text question; a stop word pauses; a pending input consumes the next owner message **unless the message is forwarded/third-party** (never an answer, M03 D49); otherwise free text goes to the agent. Every handler answers the press, does all database work in the transaction and queues Telegram calls with `db.uow.after_commit` (ARCHITECTURE §6.7 as built S02.6).

| Kind / command | Type | Ops / args | Owner |
|---|---|---|---|
| `act` | callback (token = `actions.approval_token`) | `ok` approve, `ed` edit, `rj` reject, `rd` re-draft (outdated/expired cards; token from a `pending_inputs` card), `cx` cancel an edit | S04.3 |
| `draft_edit` | pending input (`ref_id` = action id) | the owner's next message = replacement text or revision instruction (P2-T08) | S04.3 |
| `activity` | command | `/activity [today\|yesterday\|7d]` | S04.5 |
| `ntf` | callback (token on a `pending_inputs` card, `ref_id` = notification id) | `op` open the chat's panel, `nu` not useful, `us` useful | S05.2 |
| `mon` | callback (token on a `pending_inputs` card, `ref_id` = automation id) | `stop` | S05.4 |
| `digest` | command | `/digest` (on demand) | S05.3b |

Callback data stays `<kind>:<token>:<op>` ≤ 64 bytes (`accounts/callbacks.py`). The `act` handler resolves its token in two places: `actions.approval_token` for `ok`/`ed`/`rj` on a live card, and a `pending_inputs` card of kind `act` (`ref_id` = action id) for `cx` and `rd`, which act on actions without a live token. The product has no `setMyCommands` call today and P2 adds none; `/activity` (S04.5) and `/digest` (S05.3b) each add one line to the `/help` text in `handlers.py` (the only edit to that file outside F0).

## 8. Cross-stream services

### 8.1 Actions (`actions/lifecycle.py` bulk helpers — **F0**; `actions/proposals.py` — S04.1)

```python
# F0: conditional set-based transitions; each writes audit rows and returns the affected action ids.
async def outdate_pending(session, *, account_id: int, conversation_id: int, reason: StateReason,
                          include_approved: bool, now: datetime) -> list[int]
async def cancel_pending(session, *, account_id: int, conversation_ids: Sequence[int] | None,
                         reason: StateReason, now: datetime) -> list[int]   # None = whole account
async def clear_texts(session, *, account_id: int, conversation_ids: Sequence[int] | None) -> int

# S04.1
@dataclass(frozen=True, slots=True)
class ProposalRequest:
    account_id: int; conversation_id: int; origin: ActionOrigin; kind: ActionKind; text: str
    base_message_id: int | None; shown_through_message_id: int | None
    run_id: int | None; tool_call_id: str | None; trigger_turn_id: int | None; draft_run_id: int | None
    draft_flags: Mapping[str, Any] | None = None   # stored in actions.draft_flags (R8)
@dataclass(frozen=True, slots=True)
class ProposalResult:
    action_id: int; state: ActionState; reused: bool; superseded_action_id: int | None; decision: Decision
async def propose(session, request: ProposalRequest, *, clock: Clock, config: ActionsConfig) -> ProposalResult
```
S04.4 (also Neryn) adds origin-filtered and account-wide variants in the same module (`cancel_autonomous_pending`, `outdate_pending_for_account`, `cancel_pending_ai_generated`) with the same guarantees. A draft that became stale while the model ran is caught before `propose` by S04.2's `propose_draft` freshness check under the conversation lock; `not_stale` itself stays an execution rule.

`propose` semantics are specified in S04.1 (idempotency on `(run_id, tool_call_id)`, retried-turn reuse by `trigger_turn_id` + conversation, §9.4 lock order, supersede the live card, proposal-phase policy, token and `expires_at`). It never sends a card; callers call the presenter.

### 8.2 Approval cards (`actions/ports.py` protocol — F0; implementation S04.3)

```python
class ApprovalCardPresenter(Protocol):
    def present_after_commit(
        self, session: AsyncSession, action_id: int
    ) -> None: ...  # new card after commit
    def sync_after_commit(
        self, session: AsyncSession, action_id: int
    ) -> None: ...  # edit card to current state
```
Until S04.3 merges, S04.2 tests use a recording fake (`tests/fakes/approvals.py`, F0).

### 8.3 Notifications (`inbox/notification_types.py` — F0; `inbox/notifications.py::enqueue` — S05.2)

```python
class AlertClass(StrEnum): SYSTEM = "system"; STANDARD = "standard"
class Priority(StrEnum): LOW = "low"; NORMAL = "normal"; HIGH = "high"; URGENT = "urgent"
class NotificationKind(StrEnum):
    TRIAGE_URGENT = "triage_urgent"; TRIAGE_DECISION = "triage_decision"; TRIAGE_NEEDS_REPLY = "triage_needs_reply"
    TRIAGE_SUSPICIOUS = "triage_suspicious"; TASK_CLOSED = "task_closed"; COMMITMENT_DUE = "commitment_due"
    MONITOR_MATCH = "monitor_match"; SYSTEM = "system"   # SYSTEM rows carry an AlertKind in refs["alert_kind"]
@dataclass(frozen=True, slots=True)
class NotificationRefs:  # ids only
    conversation_ids: tuple[int, ...] = (); contact_ids: tuple[int, ...] = (); task_ids: tuple[int, ...] = ()
    message_ids: tuple[int, ...] = (); automation_ids: tuple[int, ...] = (); action_ids: tuple[int, ...] = ()
    alert_kind: str | None = None
async def enqueue(session, *, account_id: int, kind: NotificationKind, priority: Priority, dedupe_key: str,
                  refs: NotificationRefs, clock: Clock, alert_class: AlertClass = AlertClass.STANDARD,
                  conversation_id: int | None = None, contact_id: int | None = None,
                  bypass_quiet_hours: bool = False) -> int | None   # None = deduplicated
```
`enqueue` inserts the row (`ON CONFLICT (account_id, dedupe_key) DO NOTHING`) and registers an after-commit defer of `notify.deliver`; the policy decision happens at delivery. Dedupe keys are built from ids and enums only (e.g. `triage:{conversation_id}:{covers_through_message_id}:urgent`).

`inbox/notify.py::AlertKind` gains (F0): `DELIVERY_UNCERTAIN = "delivery_uncertain"`, `CONNECTION_STALE = "connection_stale"` (their texts are added by S04.4). M04 uses `OwnerAlerts.notify_owner` for them. At CP4, S05.2 routes `OwnerAlerts.notify_owner` itself through `notifications` (class `system`); **callers do not change**, dedupe keeps reading the `system_alert` audit rows, and an uncertain outcome counts as delivered (R10, P2-T24; S05.2 §3.5 is the definition).

### 8.4 Automations (`automation/repository.py` — F0)

```python
async def create(session, *, account_id: int, kind: str, spec: RuleBase, refs: Mapping[str, list[int]],
                 timezone: str, tz_policy: str, clock: Clock, rrule: str | None = None,
                 run_at: datetime | None = None, next_run_at: datetime | None = None,
                 expires_at: datetime | None = None, task_id: int | None = None,
                 run_id: int | None = None, tool_call_id: str | None = None,
                 confirmed_at: datetime | None = None) -> Automation        # idempotent on (run_id, tool_call_id)
async def get(session, *, account_id: int, automation_id: int) -> Automation | None
async def list_active(session, *, account_id: int, kind: str | None = None) -> list[Automation]
async def set_status(session, *, account_id: int, automation_id: int, status: str, clock: Clock) -> bool
```
`RuleBase(BaseModel)`: `schema_version: int`, `model_config = ConfigDict(extra="forbid", frozen=True)`. Prompt or schema changes never change persisted rule models (ARCHITECTURE §7.5).

### 8.5 Tasks and triage verdicts (S05.1)

`inbox/tasks.py` (`upsert_from_candidate`, `close`, `open_for_conversation`, `due_between`) and `inbox/triage_store.py` (`latest(session, account_id, conversation_ids)`) are owned by S05.1; S05.3b's digest reads them. Their signatures are fixed in the S05.1 spec §4 and announced on the S05.1 issue before S05.3b needs them.

## 9. Agent: run types, prompts, schemas, tools

| Run type | Module | Prompt | Output schema | Context builder | Owner |
|---|---|---|---|---|---|
| `draft_reply` (incl. revisions, P2-T08) | `agent/drafting.py` (`Drafter`) | `draft_reply.v1` | `agent/draft_schemas.py::DraftResult` | `agent/draft_context.py` | S04.2 |
| `triage` | `agent/triage.py` (`Triager`) | `triage.v1` | `agent/triage_schemas.py::TriageResult` | `agent/triage_context.py` | S05.1 |
| `evaluate_monitor` | `agent/monitor_eval.py` | `evaluate_monitor.v1` | `agent/monitor_schemas.py::MonitorVerdicts` | in the same module | S05.4 |
| `owner_request` (bump) | existing | next version on the int branch | — | existing | M04: S04.3; M05: S05.4 (P2-T17) |

Every contact-triggered run follows `agent/summarize.py` exactly: `LLMClient.parse` with `tools=()` (RG-17); consent (`ai_control` + `ai_managed` naming the route's processor) and budget checked before the `agent_runs` row; stop reason checked first; one truncation retry with double output budget, never prefilling; re-validation by our own model; deterministic sanitising; run row closed on every path; metered; context isolated to one conversation via `memory.items.contact_facing(...)` (invariant 4). Routes: `LLM_ROUTE_DRAFT_REPLY_MODEL`, `LLM_ROUTE_TRIAGE_MODEL`, `LLM_ROUTE_EVALUATE_MONITOR_MODEL` (exist in `config.py`).

New schemas and builders go in the new modules above, never into `agent/schemas.py` or `agent/context.py` (P2-T16). Prompts are `agent/prompts/<name>.vN.md`, registered in `agent/prompt_registry.py`; a used version is never edited (AGENTS §8).

**Tool plumbing (F0).** So that tool units never edit the registry's types: `ToolServices` (in `agent/tools/registry.py`) gains optional fields `drafter: Drafter | None = None` (S04.2's `agent/drafting.py`, imported under `TYPE_CHECKING`), `approval_cards: ApprovalCardPresenter | None = None`, `actions_config: ActionsConfig | None = None`, `notifications_enabled: bool = False`; `ToolContext` gains `turn_id: int | None = None`, which `OwnerRequestRuntime` sets to the control turn the run answers (a one-line change in `agent/runtime.py`, made by F0). A tool whose service is `None` returns a factual `"error"` ("not available") rather than failing.

**Tools** (registered in `agent/tools/registry.py::default_registry`; run type `owner_request`; tier `CONTROL_ONLY`; release flag default off; ARCHITECTURE §8.3 contract; AGENTS §8 test list for each):

| Tool | Class | Flag | `returns_managed` | Notes | Owner |
|---|---|---|---|---|---|
| `propose_reply` | propose | `tool.propose_reply` | yes | Drafts synchronously (P2-T09), creates an action, returns `awaiting_approval` or `blocked` — never "sent" | S04.2 |
| `propose_message` | propose | `tool.propose_message` | yes (result names the contact) | Owner-dictated exact text (R-05) | S04.2 |
| `set_conversation_mode` | configure | existing | existing | Accepts `assist` (P2-T11) | S04.2 |
| `pause_autonomy` | control | `tool.pause_autonomy` | no | Taint-exempt (already in `TAINT_EXEMPT_TOOLS`) | S04.4 |
| `resume_autonomy` | control | `tool.resume_autonomy` | no | Card-only (already in `CARD_ONLY_TOOLS`) | S04.4 |
| `get_activity` | read | `tool.get_activity` | yes | Contact names wrapped untrusted; taints | S04.5 |
| `get_attention_list` | read | existing | existing | v2: triage reasons and tasks | S05.1 |
| `set_contact_priority` | configure | `tool.set_contact_priority` | no | Echo with Undo (S03.3 pattern) | S05.2 |
| `create_monitor` | configure, `always_confirm` | `tool.create_monitor` | no | Args = strict `MonitorSpec` (P2-T14) | S05.4 |
| `list_monitors` | read | `tool.list_monitors` | yes | | S05.4 |
| `stop_monitor` | configure | `tool.stop_monitor` | no | | S05.4 |

## 10. Settings, preferences and flags

**Environment settings (F0 adds to `config.py` and `.env.example` with these defaults):** `APPROVAL_MAX_AGE_HOURS=12` (1–24); `DRAFT_DEBOUNCE_SECONDS=20` (5–300); `TRIAGE_DEBOUNCE_SECONDS=20` (5–300); `MONITOR_DEBOUNCE_SECONDS=20` (5–300); `LLM_DRAFT_REPLY_EFFORT=low`, `LLM_TRIAGE_EFFORT=low`, `LLM_EVALUATE_MONITOR_EFFORT=low` (same `Literal` as the existing effort settings; empty sends none); `MARK_UNCERTAIN_AFTER_SECONDS=120` (≥ 120; S04.4 uses `max(this, gateway timeout + max throttle wait + 30 s)`); `CONNECTION_STALE_ALERT_MINUTES=30`.

**`ActionsConfig` (`actions/config.py`, F0; built once in `bootstrap.py`):** `approval_max_age: timedelta`, `draft_debounce: timedelta`, `mark_uncertain_after: timedelta`, `connection_stale_alert_after: timedelta`, `sending_enabled: bool` (flag `actions.send`), `process_run_id: str` (R1), `assist_draft_trigger: Literal["off", "ingestion", "triage"]` = `actions/drafts.py::assist_trigger(flags)`: `"off"` without `assist.drafts`; `"triage"` when `assist.drafts`, `triage.run` and `triage.assist_trigger` are all on; otherwise `"ingestion"`. This is the **single** Assist-trigger switch (P2-O6): the M04 listener defers drafts only on `"ingestion"`, the M05 triage effects only on `"triage"`.

**Owner preferences (`accounts.settings`, validated in `accounts/preferences.py`; F0 adds keys, validators and defaults — PRD Q-07):** `quiet_hours` (`"HH:MM-HH:MM"` local, or `"off"`; default `"23:00-08:00"`); `urgent_bypass` (bool; default false); `digest_time` (`"HH:MM"` local or `"off"`; default `"21:00"`); `alert_cap_per_hour` (1–30; default 6). Reading an unset key gives the default; an invalid stored value reads as the default (fail safe, M03 D85 pattern). `preferences.update(...)` and `preferences.current(...)` accept and return the new keys (F0), with the existing audit/Undo behaviour.

**Release flags (`FEATURE_FLAGS`; all default off; enable order in WORKFLOW §10):**

| Flag | Gates | Owner |
|---|---|---|
| `tool.propose_message`, `tool.propose_reply`, `tool.get_activity`, `tool.pause_autonomy`, `tool.resume_autonomy` | The tools | S04.2/S04.5/S04.4 |
| `assist.drafts` | Proactive Assist drafting (ingestion trigger) | S04.2 |
| `actions.send` | `actions.execute` may call Telegram. Off ⇒ cards work normally (edit, reject, expiry), but an approved action fails rule `sending_enabled` in Tx A and ends `blocked`/`sending_disabled` with copy-ready text ("drafts only" mode, P2-T21) | S04.4 |
| `notifications.deliver` | Delivery of `standard` notifications (system alerts are never gated) | S05.2 |
| `triage.run` | `inbox.triage` jobs run | S05.1 |
| `triage.assist_trigger` | Triage replaces ingestion as the Assist draft trigger (needs `triage.run`) | S05.1 |
| `digest.daily` | Daily digest automation fires | S05.3b |
| `monitors.run` | Monitor matching and evaluation | S05.4 |
| `tool.set_contact_priority`, `tool.create_monitor`, `tool.list_monitors`, `tool.stop_monitor` | The tools | S05.2/S05.4 |

## 11. Audit events (content-free; `audit_log.event`)

M04 — the transition primitive writes exactly one event per applied transition (`subject_type = 'action'`, `subject_id` = action id, `details = {"from", "to", "version", "reason"}`, plus `attempt_id` for the claim and send outcomes; ids, enums and codes only — R15):

| Transition | Event |
|---|---|
| `proposed → awaiting_approval` | `action_proposed` |
| `proposed → blocked` | `action_blocked` |
| `proposed → cancelled` | `action_cancelled` |
| `awaiting_approval → revising` | `action_revision_requested` |
| `revising → awaiting_approval` (new version) | `action_edited` |
| `revising → awaiting_approval` (edit cancelled or revision failed; same version) | `action_revision_cancelled` |
| `awaiting_approval → approved` | `action_approved` |
| `awaiting_approval → rejected` | `action_rejected` |
| `* → outdated` | `action_outdated` (`action_superseded` when the reason is `superseded`) |
| `* → expired` | `action_expired` |
| `* → cancelled` | `action_cancelled` |
| `approved → blocked` | `action_blocked` |
| `approved → executing` | `action_claimed` |
| `executing → approved` (429) | `action_rate_limited` |
| `executing → succeeded` / `failed` / `delivery_uncertain` | `action_succeeded` / `action_failed` / `action_delivery_uncertain` |
| `delivery_uncertain → succeeded` / `failed` | `action_late_result` |

Other M04 events: `assistant_message_deleted`, `draft_skipped` and `draft_failed` (`subject_type = 'conversation'`), `autonomy_paused`, `autonomy_resumed`, `connection_marked_stale` (`subject_type = 'connection'`); existing `takeover_detected`, `source_edited_after_action` (ARCHITECTURE §6.5). M05: `task_created`, `task_closed`, `triage_completed` (counts only), `monitor_created`, `monitor_matched`, `monitor_completed`, `monitor_stopped`, `digest_sent`, `contact_priority_changed`, `automation_invalid` (a stored rule that no longer validates; the automation is paused).

## 12. File and section ownership (after CP1)

| File / area | May edit | Rule |
|---|---|---|
| `migrations/versions/*` | Neryn (schema steward) | Others ask for a revision number (P2-T01) |
| `privacy/content_columns.py`, `privacy/retention.py`, `privacy/deletion.py`, `privacy/post_restore.py` | Neryn; S05.3b adds the automations step to `post_restore.py` | Via ICR after CP1 |
| `core/*` | F0 only | Via ICR |
| `conversations/service.py`, `conversations/listeners.py`, `telegram/business.py` | Neryn | Listeners, not edits, for new reactions |
| `telegram/gateway.py`, `telegram/business_targets.py` | Neryn (S04.4) | |
| `telegram/control/dispatcher.py`, `routes.py`, `handlers.py` | F0; S04.4 (Neryn) for the `/pause`, `/resume` and stop-word replies moving after commit and for the consent-revoke call site; one `/help` line each for S04.5 and S05.3b | New handlers go in new modules registered in `routes` |
| `conversations/modes.py` | Viro (S04.2: Assist mode; leaving Assist clears `pending_draft_at`; switching to Ignore calls `actions.lifecycle.cancel_pending`) | S04.4 does not edit it |
| `actions/drafts.py`, `actions/ingestion.py` | Viro (S04.2) | S05.1 reads `assist_trigger`, never edits |
| `agent/untrusted.py` | Viro (S04.2 adds `Source.DRAFT`); others append `Source` members only | Append only |
| `telegram/control/panels.py` | Viro (conversation panel: Assist button), Junior (S05.2P: settings sections, priority row) | Separate functions; integrator resolves textual conflicts |
| `jobs/tasks.py`, `jobs/reconcile.py` | F0 only | Per-domain modules for everything new |
| `bootstrap.py` | M04 units inside `_wire_m04`, M05 units inside `_wire_m05`; S04.4 also sets the `Bot` session's 20 s request timeout and passes the owner allowlist to the poller (fast lane) | Nothing else outside the blocks |
| `config.py`, `.env.example` | F0 | New settings via ICR |
| `accounts/preferences.py` | F0 (M05 keys) | Via ICR |
| `agent/tools/registry.py` | Each tool's owner adds its import and list entry | Append only |
| `agent/prompt_registry.py` | Each run owner adds its entry; the `owner_request` entry only by the steward unit (P2-T17) | |
| `agent/runtime.py`, `agent/tools/executor.py`, `agent/schemas.py`, `agent/context.py` | Nobody without an ICR — except F0's `ToolContext.turn_id` line in `runtime.py` | S03.7 owns their current shape |
| `agent/tools/summaries.py`, `memory/catch_up.py` | S04.5 (catch-up activity block), S05.1 (attention v2) | Separate functions |
| `inbox/notify.py` | F0 (AlertKind members), S04.4 (the texts of its two kinds), S05.2 | |
| `memory/undo.py` | S05.2 (a revert branch for `contact_priority_changed`) | Add a branch; never change existing ones |
| `observability/status.py` | S04.4 (actions block), S05.2 (notifications block) | Separate blocks |
| `evals/datasets/**`, `MANIFEST.sha256` | Junior (E2D/E3D/E4D) | Manifest regenerated on rebase (WORKFLOW §9) |
| `evals/*.py` | Viro (E2T), Jack (E3T) | New modules `evals/drafts.py`, `evals/triage.py` |
| `docs/ARCHITECTURE.md` | Neryn: §6.5, §6.6, §6.8, §9.1–§9.4, §10, §12.2 (M04 rows), §13, §15.6; Viro: §6.7 (approval-card "as built" paragraph), §7.1 `draft_reply`, §7.3, §7.5 `DraftResult`, §8.2 M04 tools, §9.5; Jack: §7.1 `triage`, §7.5 `TriageResult`, §8.2 M05 rows (`get_attention_list` v2, `set_contact_priority`), §10.3 M05 rows, §12.2 (M05 rows), §12.4, §17; Viro (S05.4): monitor rows in §7 and §8.2; Junior: §8.2 `get_activity` row via mentor | Each unit edits only its sections, in its own PR |
| `docs/IMPLEMENTATION_PLAN.md` | §10 RG rows: Antony; everything else: TL | §1.4 statuses only with the owner's approval |
| `docs/milestones/M04.md`, `M05.md`, `docs/engineering/p2/**` | TL | Engineers put report text in their PR description |
| `AGENTS.md` | TL, with the owner's agreement | |
