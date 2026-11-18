"""The owned agent loop for `owner_request` runs (ARCHITECTURE §7.2, ADR-07a).

One owner control-chat turn in, one reply out. The loop is small and explicit
so that it can stay provider-neutral, persist every step, enforce budgets and
scope tools per run:

    for each iteration (bounded):
        typing indicator → LLM call → check stop reason
        refusal / filtered       → safe reply
        truncated                → retry once with a larger budget, else fail
        tool calls               → execute all, append all results, continue
        text                     → cite sources, reply
    budget or deadline exhausted → partial reply with a note

A reply's `[msg:<id>]` markers become sources read from the database, and
markers for messages no tool returned in the run are removed (S03.7 B3,
`agent/citations.py`). Without `ai_managed`, the chat-reading tools are
offered as blocked stubs; a run that called one gets a fixed "chat access is
off" line and the `/privacy managed` card (B5, D104).

Messages within a run are append-only: the model's output is passed back
unchanged. Across runs, context is rebuilt from `control_turns`.

Consent is checked here for a friendly answer, and again — as the wall — by
the consent wrapper around the LLM client on every call (§6 invariant 9).
Nothing is held in a database transaction across an LLM or Telegram call.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, ControlTurn
from assistant.accounts.service import (
    CONSENT_AI_CONTROL,
    CONSENT_AI_MANAGED,
    ConsentMissingError,
    active_consent,
)
from assistant.actions.activity import (
    ActionReceipt,
    ReceiptPage,
    action_receipts,
    conversation_names,
    live_cards,
    refresh_receipts,
)
from assistant.actions.lifecycle import warning_card_filter
from assistant.actions.models import Action
from assistant.actions.request_models import TERMINAL_REQUEST_STATES, OwnerRequest
from assistant.actions.requests import (
    REQUEST_TTL,
    OwnerContinuation,
    SourceUnavailable,
    load_request,
    load_source,
)
from assistant.actions.states import LIVE_CARD, ActionOrigin, ActionState, StateReason
from assistant.agent import incident_trace, owner_texts, prompt_registry
from assistant.agent.citations import apply_citations
from assistant.agent.context import (
    OWNER_HISTORY_TURNS,
    ContextPart,
    build_owner_request,
    carries_third_party,
    render_continuation,
    render_receipts,
    select_owner_context,
)
from assistant.agent.locales import SupportedLocale, supported_locale
from assistant.agent.models import AgentRun
from assistant.agent.owner_requests import (
    prepare_owner_request,
    refresh_owner_request,
    selected_request,
)
from assistant.agent.persistence import (
    AgentRunRepository,
    ControlTurnRepository,
    LlmCallRepository,
    account_of_turn,
    load_owner_continuation,
    load_preference_reply_source,
    month_start,
)
from assistant.agent.ports import ControlChannel
from assistant.agent.receipt_texts import no_action_text, receipt_lines
from assistant.agent.summarize import Summarizer
from assistant.agent.tools.executor import StepRecorder, TaintRecorder, ToolExecutor
from assistant.agent.tools.owner_composition import PROPOSE_OWNER_MESSAGE_FLAG
from assistant.agent.tools.registry import (
    CapabilityClass,
    CapabilityTier,
    OwnerTurn,
    ToolContext,
    ToolRefs,
    ToolRegistry,
    ToolServices,
)
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.control_texts import text as operational_text
from assistant.core.model_routes import processor_for_model
from assistant.db.uow import transaction
from assistant.inbox.connection_alerts import send_managed_consent_card
from assistant.llm.port import (
    InputItem,
    LLMClient,
    LLMError,
    LLMErrorKind,
    LLMRequest,
    LLMResponse,
    Provenance,
    StopReason,
    TextItem,
)

if TYPE_CHECKING:
    from assistant.actions.config import ActionsConfig
    from assistant.actions.ports import ApprovalCardPresenter
    from assistant.actions.proposals import OwnerProposalFence
    from assistant.agent.drafting import (  # type: ignore[import-untyped, unused-ignore]
        CardReviser,
        Drafter,
    )
from assistant.memory import items
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

RUN_TYPE = "owner_request"
#: Owner-scope memory items preloaded into an owner run (newest first).
OWNER_MEMORY_ITEMS = 30


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Budgets and model settings for owner runs (ARCHITECTURE §7.2, §7.4)."""

    model: str
    effort: str | None = "low"
    max_output_tokens: int = 2000
    max_output_tokens_ceiling: int = 8000
    max_iterations: int = 8
    run_token_budget: int = 60_000
    run_timeout: timedelta = timedelta(minutes=5)
    call_timeout_seconds: float = 120.0
    monthly_budget_usd: Decimal | None = None
    enabled_flags: frozenset[str] = frozenset()


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"
    BUDGET_EXCEEDED = "budget_exceeded"
    SKIPPED = "skipped"
    """Nothing to do, or nobody to reply to; no model call. No run row is
    written, except for a question deleted before its run (a `failed` row,
    `NOTE_TURN_DELETED`, so its notice goes out once)."""


#: The question's text was gone when its run started (S03.5, M03 D88).
NOTE_TURN_DELETED = "turn_text_deleted"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: RunStatus
    run_id: int | None = None
    reply_message_id: int | None = None
    note: str | None = None
    account_id: int | None = None
    ack_latency_ms: int | None = None
    #: Messages the reply cites, verified against what the run's tools
    #: returned (S03.7 B3), and how many markers were removed as unverified.
    cited_message_ids: tuple[int, ...] = ()
    unverified_citations: int = 0
    #: A chat-reading tool was called while chat access is off (B5).
    chat_access_off: bool = False


# Owner-facing texts. Each says plainly what happened (§6 invariant 11).
#: The question's text was deleted before its run (an owner deletion that ran
#: first, or retention): never answer a blank (S03.5 fix round 2, M03 D88).
TEXT_TURN_DELETED = (
    "Your message was deleted before I could read it (a deletion ran first), so I "
    "have not answered it. Please send it again."
)
TEXT_NEEDS_CONSENT = (
    "I cannot answer with AI until you agree to it. Send /start to see what that means."
)
TEXT_BUDGET_REACHED = (
    "This month's AI budget is used up, so I am not calling the model. "
    "/status and /settings still work."
)
TEXT_PROVIDER_DOWN = (
    "I cannot reach the AI provider right now, so I could not answer. "
    "Please try again in a few minutes. /status still works."
)
TEXT_INTERNAL_ERROR = (
    "Something went wrong on my side and I could not answer. The problem has been logged."
)
TEXT_REFUSED = "I can't help with that request."
TEXT_TOO_LONG = "My answer would have been too long. Could you ask for something narrower?"
TEXT_LIMITS = (
    "I stopped before finishing because this request hit my time or step limit. "
    "Try a narrower question."
)
TEXT_EMPTY = "I don't have an answer to that."


@dataclass
class _RunState:
    account_id: int
    chat_id: int
    run_id: int
    turn_id: int
    items: list[InputItem]
    instructions: str
    iterations: int = 0
    tokens: int = 0
    #: Whether anything managed reached the model; the reply inherits it.
    used_managed: bool = False
    #: Third-party text is in the context (§7.1); persisted on `agent_runs`.
    tainted: bool = False
    #: Diagnostic only: the current turn or a tool result carried such text.
    #: from_tainted_run remains authoritative for derived prose (D-S3).
    directly_tainted: bool = False
    #: Conversations/contacts the tool results drew on; written to the reply turn.
    refs: ToolRefs = field(default_factory=ToolRefs)
    #: When the owner's message arrived, and how long until the typing indicator.
    received_at: datetime | None = None
    ack_latency_ms: int | None = None
    #: The owner's fixed-wording locale and timezone, for cited sources.
    locale: SupportedLocale = "en"
    timezone: str = "UTC"
    #: A tool returned `chat_access_off` in this run (B5).
    chat_access_off: bool = False
    #: `ai_managed` is not granted at all: the consent card can help (B5).
    may_ask_managed: bool = False
    cited_ids: tuple[int, ...] = ()
    unverified_citations: int = 0
    continuation: OwnerContinuation | None = None
    action_ids: set[int] = field(default_factory=set)
    #: Artifacts a tool result named this run; they, not merely-referenced
    #: cards, are what the final answer is about when there are any.
    touched_ids: set[int] = field(default_factory=set)
    receipts: dict[int, ActionReceipt] = field(default_factory=dict)
    artifact_reads: dict[int, int] = field(default_factory=dict)
    receipt_correction: bool = False
    final_receipts: dict[int, ActionReceipt] | None = None
    no_action_results: list[tuple[str, str | None]] = field(default_factory=list)
    #: H8: this run re-drafts that card version for its own request.
    redraft: _Redraft | None = None


@dataclass(frozen=True, slots=True)
class _Redraft:
    """An owner's Re-draft tap (H8): ids from the job, never text."""

    action_id: int
    request_id: int
    expected_version: int
    #: The tap's audit row: each tap is one attempt; a retried job keeps it.
    tap_id: int | None = None

    @property
    def trigger_ref(self) -> str:
        tap = "" if self.tap_id is None else f":{self.tap_id}"
        return f"redraft:{self.action_id}:{self.expected_version}{tap}"


#: The only state-changing tool a Re-draft run may call.
REDRAFT_PROPOSAL_TOOL = "propose_owner_message"
#: Card states an owner Re-draft may replace (the press accepts the same).
_REDRAFT_STATES = frozenset(
    {state.value for state in LIVE_CARD} | {ActionState.OUTDATED.value, ActionState.EXPIRED.value}
)


def _managed_refs(parts: tuple[ContextPart, ...]) -> ToolRefs:
    """Deletion refs from supplied managed parts, never omitted history."""
    refs = ToolRefs()
    for part in parts:
        if part.managed:
            refs = refs | ToolRefs(frozenset(part.conversation_ids), frozenset(part.contact_ids))
    return refs


def _reference_ids(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {i for i in value if type(i) is int and i > 0}


def _scope(state: _RunState) -> set[int]:
    return state.touched_ids or state.action_ids


def _receipt_changes(
    previous: dict[int, ActionReceipt], current: dict[int, ActionReceipt], action_ids: set[int]
) -> bool:
    for action_id in action_ids:
        before, after = previous.get(action_id), current.get(action_id)
        if before is None or after is None:
            if before != after:
                return True
        elif replace(before, observed_at=after.observed_at) != after:
            return True
    return False


@dataclass(frozen=True, slots=True)
class _Started:
    account_id: int
    chat_id: int
    no_consent: bool = False
    turn_deleted: bool = False
    state: _RunState | None = None
    managed_consent: bool = False
    over_budget: bool = False
    #: The turn's words and flags for the tools (PF-11).
    owner_turn: OwnerTurn | None = None


class OwnerRequestRuntime:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMClient,
        channel: ControlChannel,
        registry: ToolRegistry,
        clock: Clock,
        config: AgentConfig,
        summarizer: Summarizer | None = None,
        drafter: Drafter | None = None,
        approval_cards: ApprovalCardPresenter | None = None,
        actions_config: ActionsConfig | None = None,
        notifications_enabled: bool = False,
        card_reviser: CardReviser | None = None,
        owner_fence: OwnerProposalFence | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._channel = channel
        self._registry = registry
        self._clock = clock
        self._config = config
        #: The `summarize` run type, for the catch-up and attention tools.
        self._summarizer = summarizer
        self._drafter = drafter
        self._approval_cards = approval_cards
        self._actions_config = actions_config
        self._notifications_enabled = notifications_enabled
        #: `revise_draft`'s path to the card's revision machinery (PF-13).
        self._card_reviser = card_reviser
        self._owner_fence = owner_fence

    async def run(self, turn_id: int) -> RunOutcome:
        return await self._run(turn_id, None)

    async def redraft(
        self,
        *,
        action_id: int,
        request_id: int,
        expected_version: int,
        tap_id: int | None = None,
    ) -> RunOutcome:
        """H8: an owner's Re-draft tap resumes that card's own request.

        The job carries ids only. The request's operation, owner sources,
        recipient, target and constraints are reloaded here and re-validated
        before any model call; the main owner model then writes a fresh
        version through the same proposal tools as the original task.
        """
        if PROPOSE_OWNER_MESSAGE_FLAG not in self._config.enabled_flags:
            # Never deferred with the flag off; a stale job does nothing.
            logger.warning("owner_redraft_not_enabled", action_id=action_id)
            return RunOutcome(RunStatus.SKIPPED, note="redraft_not_enabled")
        return await self._run(None, _Redraft(action_id, request_id, expected_version, tap_id))

    async def _run(self, turn_id: int | None, redraft: _Redraft | None) -> RunOutcome:
        prompt = prompt_registry.active(RUN_TYPE, enabled_flags=self._config.enabled_flags)
        started = await self._start(turn_id, prompt, redraft)
        if isinstance(started, RunOutcome):
            return started
        if started.turn_deleted:
            # No model call; the run row recorded in `_start` makes the notice
            # at most once (a re-run is "already answered").
            await self._channel.send(
                account_id=started.account_id, chat_id=started.chat_id, text=TEXT_TURN_DELETED
            )
            return RunOutcome(
                RunStatus.SKIPPED, note=NOTE_TURN_DELETED, account_id=started.account_id
            )
        if started.no_consent:
            # No run row and no model call: there is nothing to account for.
            await self._channel.send(
                account_id=started.account_id, chat_id=started.chat_id, text=TEXT_NEEDS_CONSENT
            )
            return RunOutcome(RunStatus.SKIPPED, note="no_consent")
        state = started.state
        assert state is not None  # set whenever consent is present
        if started.over_budget:
            return await self._finish(
                state, TEXT_BUDGET_REACHED, RunStatus.BUDGET_EXCEEDED, "monthly_budget"
            )

        offered = self._registry.for_run(
            RUN_TYPE,
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=self._config.enabled_flags,
            managed_consent=started.managed_consent,
            # Without managed consent, the chat tools are blocked stubs: a
            # chat question is answered "access is off", not blind (B5).
            stub_managed_tools=True,
        )
        if redraft is not None:
            # A Re-draft only rewrites the tapped card: the original turn is
            # replayed as context, never re-run as configure/control/memory
            # instructions. Reads and the generated proposal remain.
            offered = {
                name: tool
                for name, tool in offered.items()
                if tool.capability is CapabilityClass.READ or name == REDRAFT_PROPOSAL_TOOL
            }
        executor = ToolExecutor(
            context=ToolContext(
                account_id=state.account_id,
                run_id=state.run_id,
                run_type=RUN_TYPE,
                turn_id=state.turn_id,
                owner_turn=started.owner_turn,
                continuation=state.continuation,
                tainted=state.tainted,
                managed_ok=started.managed_consent,
                redraft_of=(
                    None if redraft is None else (redraft.action_id, redraft.expected_version)
                ),
                services=ToolServices(
                    session_factory=self._session_factory,
                    clock=self._clock,
                    monthly_budget_usd=self._config.monthly_budget_usd,
                    channel=self._channel,
                    summarizer=self._summarizer,
                    drafter=self._drafter,
                    approval_cards=self._approval_cards,
                    actions_config=self._actions_config,
                    notifications_enabled=self._notifications_enabled,
                    enabled_flags=self._config.enabled_flags,
                    card_reviser=self._card_reviser,
                    owner_fence=self._owner_fence,
                ),
            ),
            offered=offered,
            record_step=self._step_recorder(state.account_id, state.run_id),
            clock=self._clock,
            on_taint=self._taint_recorder(state),
            refs=state.refs,
            registered=frozenset(tool.name for tool in self._registry.all()),
        )
        executor.trace = await incident_trace.start_trace(self._session_factory, executor.context)
        return await self._loop(state, executor, prompt.label)

    async def _start(
        self,
        turn_id: int | None,
        prompt: prompt_registry.Prompt,
        redraft: _Redraft | None = None,
    ) -> _Started | RunOutcome:
        """Load the turn, check consent and budget, create the run row. One transaction."""
        async with transaction(self._session_factory) as uow:
            session = uow.session
            account_id: int | None = None
            if redraft is None:
                assert turn_id is not None
                account_id = await account_of_turn(session, turn_id)
                trigger_ref = f"turn:{turn_id}"
            else:
                # The request's latest owner turn carries the original task;
                # its sources outside recent history are reloaded below.
                located = (
                    await session.execute(
                        select(Action.account_id, OwnerRequest.last_turn_id)
                        .outerjoin(
                            OwnerRequest,
                            (OwnerRequest.account_id == Action.account_id)
                            & (OwnerRequest.id == redraft.request_id),
                        )
                        .where(Action.id == redraft.action_id)
                    )
                ).one_or_none()
                if located is None:
                    # The card itself is gone: there is nothing to restore.
                    return RunOutcome(RunStatus.SKIPPED, note="redraft_not_found")
                if located.last_turn_id is None:
                    return await self._redraft_failed(
                        session, located.account_id, redraft, prompt, "request_unavailable"
                    )
                account_id, turn_id = located.account_id, located.last_turn_id
                trigger_ref = redraft.trigger_ref
            if account_id is None or turn_id is None:
                return RunOutcome(RunStatus.SKIPPED, note="turn_not_found")
            turn = await ControlTurnRepository(session, account_id).get(turn_id)
            account = await session.get(Account, account_id)
            if turn is None or account is None or turn.role != "owner":
                self._restore_card(session, redraft)
                return RunOutcome(RunStatus.SKIPPED, note="not_an_owner_turn")
            if account.control_chat_id is None:
                self._restore_card(session, redraft)
                return RunOutcome(RunStatus.SKIPPED, note="no_control_chat")
            chat_id = account.control_chat_id

            # A job can run twice (stalled-worker recovery, reconciler). A turn
            # that already got an answer must not get a second one; a run cut
            # off mid-way (no answer recorded) is marked and done again. A
            # Re-draft's key is its tap, so each tap is answered once.
            previous = await AgentRunRepository(session, account_id).for_trigger(trigger_ref)
            if any(run.status != "running" for run in previous):
                self._restore_card(session, redraft)
                return RunOutcome(RunStatus.SKIPPED, note="already_answered", account_id=account_id)
            for interrupted in previous:
                interrupted.status = RunStatus.FAILED.value
                interrupted.error_class = "interrupted"
                interrupted.finished_at = self._clock.now()
            redraft_continuation: OwnerContinuation | None = None
            if redraft is not None:
                checked = await self._check_redraft(session, account_id, redraft, turn, prompt)
                if isinstance(checked, RunOutcome):
                    return checked
                redraft_continuation = checked
            if turn.text is None and turn.quoted_text is None:
                # On record before the notice goes out (a run that called no
                # model): a re-run of this job finds it and stays silent, as
                # for an answered turn (S03.5 fix round 3, E).
                AgentRunRepository(session, account_id).add(
                    AgentRun(
                        run_type=RUN_TYPE,
                        trigger_ref=f"turn:{turn_id}",
                        model=self._config.model,
                        effort=self._config.effort,
                        prompt_version=prompt.label,
                        status=RunStatus.FAILED.value,
                        started_at=self._clock.now(),
                        finished_at=self._clock.now(),
                        error_class=NOTE_TURN_DELETED,
                    )
                )
                return _Started(account_id, chat_id, turn_deleted=True)

            control = await active_consent(session, account_id, kind=CONSENT_AI_CONTROL)
            if not control.granted:
                return _Started(account_id, chat_id, no_consent=True)
            managed_state = await active_consent(session, account_id, kind=CONSENT_AI_MANAGED)
            # Defence in depth behind the consent wrapper: managed tools and
            # managed history only when the consent names the processor that
            # serves this route's model (§14.3, M03 D48).
            managed_ok = (
                managed_state.granted
                and processor_for_model(self._config.model) in managed_state.processors
            )
            over_budget = await self._over_budget(session, account_id)
            run = AgentRunRepository(session, account_id).add(
                AgentRun(
                    run_type=RUN_TYPE,
                    trigger_ref=trigger_ref,
                    model=self._config.model,
                    effort=self._config.effort,
                    prompt_version=prompt.label,
                    status="running",
                    started_at=self._clock.now(),
                )
            )
            await session.flush()
            history = await ControlTurnRepository(session, account_id).recent_before(
                turn_id, limit=OWNER_HISTORY_TURNS, include_managed=managed_ok
            )
            if redraft_continuation is not None:
                continuation = redraft_continuation
            else:
                load_continuation = (
                    prepare_owner_request
                    if PROPOSE_OWNER_MESSAGE_FLAG in self._config.enabled_flags
                    else load_owner_continuation
                )
                continuation = await load_continuation(
                    session,
                    account_id=account_id,
                    turn=turn,
                    now=self._clock.now(),
                    managed_allowed=managed_ok,
                )
            preference_capture_enabled = "tool.remember_owner_sources" in self._config.enabled_flags
            receipt_page: ReceiptPage | None = None
            referenced_actions: set[int] = set()
            if PROPOSE_OWNER_MESSAGE_FLAG in self._config.enabled_flags and managed_ok:
                selected = select_owner_context(
                    history=history,
                    turn=turn,
                    request=continuation.request,
                    validated_sources=continuation.sources,
                    managed_allowed=managed_ok,
                )
                supplied_turns = {i for part in selected.parts for i in part.source_turn_ids}
                request_ids: set[int] = set()
                conversations: set[int] = set()
                for supplied in [*history, turn]:
                    if supplied.id not in supplied_turns:
                        continue
                    refs = supplied.refs or {}
                    referenced_actions.update(_reference_ids(refs.get("revision_action_id")))
                    referenced_actions.update(_reference_ids(refs.get("action_ids")))
                    request_ids.update(_reference_ids(refs.get("owner_request_ids")))
                    conversations.update(_reference_ids(refs.get("conversation_ids")))
                current_refs = turn.refs or {}
                current_actions = _reference_ids(current_refs.get("revision_action_id"))
                current_actions.update(_reference_ids(current_refs.get("action_ids")))
                current_requests = _reference_ids(current_refs.get("owner_request_ids"))
                if continuation.selection.action_id is not None:
                    referenced_actions.add(continuation.selection.action_id)
                    current_actions.add(continuation.selection.action_id)
                if redraft is not None:
                    referenced_actions.add(redraft.action_id)
                    current_actions.add(redraft.action_id)
                if continuation.request is not None:
                    request_ids.add(continuation.request.request_id)
                    current_requests.add(continuation.request.request_id)
                    if continuation.request.conversation_id is not None:
                        conversations.add(continuation.request.conversation_id)
                receipt_page = await action_receipts(
                    session,
                    account_id=account_id,
                    request_ids=tuple(request_ids),
                    conversation_ids=tuple(conversations),
                    action_ids=tuple(referenced_actions),
                    now=self._clock.now(),
                )
                # Request-linked artifacts are explicit; fallback live cards
                # are context only, never substituted for a tool's NULL action.
                referenced_actions = {
                    r.action_id
                    for r in receipt_page.receipts
                    if r.action_id in current_actions or r.request_id in current_requests
                }
            context = build_owner_request(
                prompt=prompt,
                account=account,
                history=history,
                turn=turn,
                now=self._clock.now(),
                managed_allowed=managed_ok,
                continuation=continuation,
                preferences=(
                    await items.effective_owner_preferences(
                        session,
                        account_id=account_id,
                        conversation_id=(
                            None
                            if continuation.request is None
                            else continuation.request.conversation_id
                        ),
                        purpose="owner_request",
                        managed_allowed=managed_ok,
                    )
                    if preference_capture_enabled
                    else ()
                ),
                preference_capture_enabled=preference_capture_enabled,
                preference_reply_source=(
                    await load_preference_reply_source(session, account_id=account_id, turn=turn)
                    if preference_capture_enabled
                    else None
                ),
                # The owner's own saved items (owner scope; S03.3, FR-CTL-01).
                memory=[
                    item
                    for item in await items.owner_items(
                        session, account_id, limit=OWNER_MEMORY_ITEMS, include_managed=managed_ok
                    )
                    if not (
                        preference_capture_enabled
                        and item.owner_source_exact
                        and not item.tainted
                        and not item.managed
                        and item.kind != "fact"
                    )
                ],
                # Which approval cards are live, content-free (PF-15); only
                # with `ai_managed`, like the chat tools that act on them.
                live_cards=(
                    await live_cards(session, account_id=account_id) if managed_ok else None
                ),
                receipts=receipt_page,
                redraft_of=(
                    None if redraft is None else (redraft.action_id, redraft.expected_version)
                ),
            )
            # Tainted from the start when the context already carries
            # third-party text or managed content (§7.1, M03 D45).
            run.tainted = context.tainted
            state = _RunState(
                account_id=account_id,
                chat_id=chat_id,
                run_id=run.id,
                turn_id=turn.id,
                items=list(context.items),
                instructions=context.instructions,
                used_managed=any(item.provenance is Provenance.MANAGED for item in context.items),
                tainted=context.tainted,
                directly_tainted=context.directly_tainted,
                # The reply may restate supplied managed parts; carrying their
                # refs lets a chat purge redact that reply too (§10.4). H2's
                # structural request/card linkage survives independently.
                refs=_managed_refs(context.parts) | ToolRefs.from_json(turn.refs),
                # A Re-draft answers a tap, not this turn's arrival.
                received_at=turn.created_at if redraft is None else None,
                locale=supported_locale(account.locale_pref),
                timezone=account.timezone,
                may_ask_managed=not managed_state.granted,
                continuation=continuation,
                action_ids=referenced_actions,
                receipts=(
                    {} if receipt_page is None else {r.action_id: r for r in receipt_page.receipts}
                ),
                redraft=redraft,
            )
            return _Started(
                account_id,
                chat_id,
                state=state,
                managed_consent=managed_ok,
                over_budget=over_budget,
                owner_turn=OwnerTurn(
                    id=turn.id,
                    text=turn.text,
                    third_party=carries_third_party(turn),
                    contains_managed=bool(turn.contains_managed),
                    quoted=(
                        turn.third_party is True
                        and turn.owner_text_verified is True
                        and turn.quoted_text is not None
                        and turn.from_tainted_run is False
                    ),
                    owner_text_verified=bool(turn.owner_text_verified),
                ),
            )

    async def _check_redraft(
        self,
        session: AsyncSession,
        account_id: int,
        redraft: _Redraft,
        turn: ControlTurn,
        prompt: prompt_registry.Prompt,
    ) -> OwnerContinuation | RunOutcome:
        """Re-validate the tapped card's own task before any model call (H8).

        Nothing is inferred from the newest chat messages: the recipient,
        standalone/reply mode, explicit target and owner sources all come
        from the persisted request. Each no-card outcome is reported once.
        """
        now = self._clock.now()
        action = await session.scalar(
            select(Action).where(Action.account_id == account_id, Action.id == redraft.action_id)
        )
        request = await session.scalar(
            select(OwnerRequest).where(
                OwnerRequest.account_id == account_id, OwnerRequest.id == redraft.request_id
            )
        )
        assert action is not None  # located in this transaction
        if request is None or action.owner_request_id != request.id:
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "request_unavailable"
            )
        if request.action_id is not None and request.action_id != action.id:
            # A later artifact already answers this request (a retried job
            # after its proposal committed, or newer owner work): no duplicate.
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "already_replaced"
            )
        if (
            action.version != redraft.expected_version
            or action.state not in _REDRAFT_STATES
            or action.origin == ActionOrigin.DELEGATION_AUTO.value
        ):
            return await self._redraft_failed(session, account_id, redraft, prompt, "stale")
        if action.state_reason == StateReason.SUPERSEDED.value:
            # A newer card already replaced this one; never revive its task.
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "already_replaced"
            )
        newer = await session.scalar(
            select(Action.id)
            .where(
                Action.account_id == account_id,
                Action.conversation_id == action.conversation_id,
                Action.id > action.id,
                Action.owner_request_id.is_distinct_from(request.id),
                or_(
                    Action.owner_request_id.is_not(None),
                    Action.trigger_turn_id.is_not(None),
                    Action.origin == ActionOrigin.OWNER_DICTATED.value,
                ),
                or_(Action.state.in_([s.value for s in LIVE_CARD]), warning_card_filter()),
            )
            .limit(1)
        )
        if newer is not None:
            # Newer owner work in this chat wins; this tap must not retire it.
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "already_replaced"
            )
        # C1 expiry: an awaiting request follows its card's age; unfinished
        # ones end 24 h after the original request. A tap never extends it.
        expired = (
            request.expires_at <= now
            if request.state == "awaiting_approval"
            else request.requested_at + REQUEST_TTL <= now
        )
        if request.state in TERMINAL_REQUEST_STATES or expired:
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "request_unavailable"
            )
        if turn.text is None and turn.quoted_text is None:
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "source_unavailable"
            )
        recipient = await session.scalar(
            select(Conversation.id)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(
                Conversation.account_id == account_id,
                Contact.account_id == account_id,
                Conversation.id == request.conversation_id,
                Contact.id == request.contact_id,
                Contact.is_bot.is_(False),
                Conversation.mode != "ignore",
            )
        )
        if recipient is None or recipient != action.conversation_id:
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "recipient_unavailable"
            )
        # A reply keeps its chosen message, even outside the newest window.
        # A missing, deleted or purged target is reported; never replaced. A
        # standalone request stays standalone whatever its old card answered.
        target = request.target_message_id
        if target is not None and action.base_message_id not in (None, target):
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "target_unavailable"
            )
        if request.operation == "reply" or target is not None:
            retained = (
                None
                if target is None
                else await session.scalar(
                    select(Message.id).where(
                        Message.account_id == account_id,
                        Message.conversation_id == recipient,
                        Message.id == target,
                        Message.direction == "contact",
                        Message.text.is_not(None),
                        Message.deleted_at.is_(None),
                        Message.purged_at.is_(None),
                    )
                )
            )
            if retained is None:
                return await self._redraft_failed(
                    session, account_id, redraft, prompt, "target_unavailable"
                )
        view = await load_request(session, account_id=account_id, request_id=request.id)
        assert view is not None
        for source in view.sources:
            loaded = await load_source(session, account_id=account_id, source=source)
            if isinstance(loaded, SourceUnavailable):
                return await self._redraft_failed(
                    session, account_id, redraft, prompt, "source_unavailable"
                )
        control = await active_consent(session, account_id, kind=CONSENT_AI_CONTROL)
        managed = await active_consent(session, account_id, kind=CONSENT_AI_MANAGED)
        if (
            not control.granted
            or not managed.granted
            or processor_for_model(self._config.model) not in managed.processors
        ):
            return await self._redraft_failed(session, account_id, redraft, prompt, "no_consent")
        if await self._over_budget(session, account_id):
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "budget_exceeded"
            )
        # The request's state, version, focus and other requests' questions
        # are untouched here: only a staged replacement links the request
        # (`propose_owner_message`), so a no-card outcome changes nothing.
        continuation = await load_owner_continuation(
            session,
            account_id=account_id,
            turn=turn,
            now=now,
            managed_allowed=True,
            selection=selected_request(view),
        )
        if continuation.selection.status != "selected" or continuation.request is None:
            return await self._redraft_failed(
                session, account_id, redraft, prompt, "request_unavailable"
            )
        return continuation

    async def _redraft_failed(
        self,
        session: AsyncSession,
        account_id: int,
        redraft: _Redraft,
        prompt: prompt_registry.Prompt,
        code: str,
    ) -> RunOutcome:
        """Record the outcome under the tap's key, then restore and tell once."""
        repository = AgentRunRepository(session, account_id)
        answered = [
            run
            for run in await repository.for_trigger(redraft.trigger_ref)
            if run.status != "running" and run.error_class != "interrupted"
        ]
        if answered:
            self._restore_card(session, redraft)
            return RunOutcome(RunStatus.SKIPPED, note="already_answered", account_id=account_id)
        repository.add(
            AgentRun(
                run_type=RUN_TYPE,
                trigger_ref=redraft.trigger_ref,
                model=self._config.model,
                effort=self._config.effort,
                prompt_version=prompt.label,
                status=RunStatus.FAILED.value,
                started_at=self._clock.now(),
                finished_at=self._clock.now(),
                error_class=f"redraft_{code}",
            )
        )
        if self._approval_cards is not None:
            self._approval_cards.redraft_failed_after_commit(session, redraft.action_id, code)
        logger.info("owner_redraft_not_run", account_id=account_id, code=code)
        return RunOutcome(RunStatus.SKIPPED, note=code, account_id=account_id)

    def _restore_card(self, session: AsyncSession, redraft: _Redraft | None) -> None:
        """A Re-draft exit without a new attempt shows the card's current state."""
        if redraft is not None and self._approval_cards is not None:
            self._approval_cards.sync_after_commit(session, redraft.action_id)

    async def _loop(
        self, state: _RunState, executor: ToolExecutor, prompt_label: str
    ) -> RunOutcome:
        deadline = self._clock.now() + self._config.run_timeout
        tools = tuple(tool.spec() for tool in executor.offered.values())

        while state.iterations < self._config.max_iterations:
            remaining = (deadline - self._clock.now()).total_seconds()
            if remaining <= 0 or state.tokens >= self._config.run_token_budget:
                break
            state.iterations += 1
            await self._channel.typing(account_id=state.account_id, chat_id=state.chat_id)
            if state.ack_latency_ms is None and state.received_at is not None:
                # M01 acceptance 1: p95 of (typing indicator minus message received).
                state.ack_latency_ms = int(
                    (self._clock.now() - state.received_at).total_seconds() * 1000
                )
                logger.info(
                    "agent_ack",
                    account_id=state.account_id,
                    run_id=state.run_id,
                    ack_latency_ms=state.ack_latency_ms,
                )
            request = LLMRequest(
                account_id=state.account_id,
                run_id=state.run_id,
                purpose=RUN_TYPE,
                model=self._config.model,
                instructions=state.instructions,
                items=tuple(state.items),
                tools=(
                    tuple(
                        t.spec()
                        for t in executor.offered.values()
                        if t.capability is CapabilityClass.READ
                    )
                    if state.receipt_correction
                    else tools
                ),
                effort=self._config.effort,
                max_output_tokens=self._config.max_output_tokens,
                timeout_seconds=min(self._config.call_timeout_seconds, remaining),
                prompt_cache_key=f"{RUN_TYPE}:{prompt_label}",
            )
            try:
                response = await self._call(request, state)
            except ConsentMissingError:
                return await self._finish(
                    state, TEXT_NEEDS_CONSENT, RunStatus.FAILED, "consent_missing"
                )
            except LLMError as error:
                if error.kind is LLMErrorKind.NOT_ADMITTED:  # I16 priced admission
                    budget = error.code == "monthly_budget"
                    return await self._finish(
                        state,
                        TEXT_BUDGET_REACHED if budget else TEXT_INTERNAL_ERROR,
                        RunStatus.BUDGET_EXCEEDED if budget else RunStatus.FAILED,
                        "monthly_budget" if budget else f"llm_not_admitted_{error.code}",
                    )
                text = (
                    TEXT_PROVIDER_DOWN
                    if error.kind in {LLMErrorKind.UNAVAILABLE, LLMErrorKind.RATE_LIMITED}
                    else TEXT_INTERNAL_ERROR
                )
                return await self._finish(state, text, RunStatus.FAILED, f"llm_{error.kind.value}")

            match response.stop_reason:
                case StopReason.REFUSAL | StopReason.CONTENT_FILTER:
                    return await self._finish(
                        state, TEXT_REFUSED, RunStatus.REFUSED, response.stop_reason.value
                    )
                case StopReason.MAX_TOKENS:
                    return await self._finish(state, TEXT_TOO_LONG, RunStatus.FAILED, "max_tokens")
                case StopReason.OTHER:
                    return await self._finish(
                        state, TEXT_INTERNAL_ERROR, RunStatus.FAILED, "stop_other"
                    )
                case StopReason.TOOL_CALLS:
                    if response.output_items is not None:
                        state.items.append(response.output_items)
                    # All results of one turn go back together, before the next call.
                    results = await executor.execute_all(response.tool_calls)
                    state.items.extend(results)
                    state.refs = executor.refs
                    state.directly_tainted = state.directly_tainted or executor.saw_untrusted
                    state.chat_access_off = executor.saw_chat_access_off
                    if any(result.provenance is Provenance.MANAGED for result in results):
                        state.used_managed = True
                    if (
                        PROPOSE_OWNER_MESSAGE_FLAG in self._config.enabled_flags
                        and state.continuation is not None
                        and state.continuation.request is not None
                    ):
                        await self._refresh_request(state, executor)
                    if PROPOSE_OWNER_MESSAGE_FLAG in self._config.enabled_flags:
                        state.action_ids.update(executor.action_ids)
                        state.touched_ids.update(executor.action_ids)
                        state.artifact_reads.update(executor.artifact_reads)
                        state.no_action_results = list(executor.no_action_results)
                        page = await self._observe_receipts(state)
                        state.receipts.update({r.action_id: r for r in page.receipts})
                        if page.receipts:
                            state.items.append(
                                TextItem("user", render_receipts(page), Provenance.OWNER_CONTROL)
                            )
                case StopReason.COMPLETED:
                    if await self._correct_receipts(state, executor, deadline):
                        continue
                    text = self._reply_text(state, executor, response.text)
                    return await self._finish(state, text, RunStatus.SUCCEEDED)

        return await self._finish(state, TEXT_LIMITS, RunStatus.FAILED, "run_limits")

    async def _observe_receipts(self, state: _RunState) -> ReceiptPage:
        async with transaction(self._session_factory) as uow:
            return await refresh_receipts(
                uow.session,
                account_id=state.account_id,
                action_ids=tuple(sorted(state.action_ids)),
                now=self._clock.now(),
            )

    async def _correct_receipts(
        self, state: _RunState, executor: ToolExecutor, deadline: datetime
    ) -> bool:
        if PROPOSE_OWNER_MESSAGE_FLAG not in self._config.enabled_flags:
            return False
        page = await self._observe_receipts(state)
        current = {r.action_id: r for r in page.receipts}
        changed = _receipt_changes(state.receipts, current, _scope(state))
        state.final_receipts = current
        if not changed or state.receipt_correction:
            return False
        if (
            state.iterations >= self._config.max_iterations
            or state.tokens >= self._config.run_token_budget
            or self._clock.now() >= deadline
        ):
            return False
        async with transaction(self._session_factory) as uow:
            control = await active_consent(uow.session, state.account_id, kind=CONSENT_AI_CONTROL)
            managed = await active_consent(uow.session, state.account_id, kind=CONSENT_AI_MANAGED)
            if (
                not control.granted
                or not managed.granted
                or processor_for_model(self._config.model) not in managed.processors
                or await self._over_budget(uow.session, state.account_id)
            ):
                return False
        state.receipt_correction = True
        state.receipts.update(current)
        state.items.append(
            TextItem(
                "user",
                "The unsent final answer used older artifact facts. Discard it. "
                "Use these fresh receipts; missing artifacts are unavailable. "
                "Only read tools remain available. For content/topic/tone claims, read "
                "get_draft for the actual current version first. If text is unavailable, "
                "say so without reconstructing it. Otherwise give status facts only.\n"
                + render_receipts(page),
                Provenance.OWNER_CONTROL,
            )
        )
        # The offer check must agree with the correction request, so even a
        # scripted/model-invented write is refused during the repair pass.
        executor.offered = {
            name: t for name, t in executor.offered.items() if t.capability is CapabilityClass.READ
        }
        return True

    async def _refresh_request(self, state: _RunState, executor: ToolExecutor) -> None:
        """A recipient tool can fill the last slot; expose the new CAS version.

        Append only structural facts after the entire tool-result batch. Owner
        source text was already supplied at start; refreshing never loads more
        contact text or changes taint, permissions, approval or send authority.
        """
        previous = state.continuation
        assert previous is not None
        async with transaction(self._session_factory) as uow:
            current = await refresh_owner_request(
                uow.session,
                account_id=state.account_id,
                continuation=previous,
                now=self._clock.now(),
            )
        state.continuation = current
        executor.context = replace(executor.context, continuation=current)
        if current != previous:
            state.items.append(
                TextItem(
                    "user",
                    render_continuation(current, managed_allowed=executor.context.managed_ok),
                    Provenance.OWNER_CONTROL,
                )
            )

    def _reply_text(self, state: _RunState, executor: ToolExecutor, model_text: str | None) -> str:
        """The model's answer with verified sources (B3), plus the fixed
        "chat access is off" line when the run needed the chats (B5)."""
        cited = apply_citations(
            (model_text or "").strip(),
            executor.citable,
            locale=state.locale,
            timezone=state.timezone,
        )
        state.cited_ids, state.unverified_citations = cited.cited_ids, cited.unverified
        text = cited.text.strip()
        if state.chat_access_off:
            line = owner_texts.CHAT_ACCESS_OFF[state.locale]
            return f"{text}\n\n{line}" if text else line
        return text or TEXT_EMPTY

    async def _call(self, request: LLMRequest, state: _RunState) -> LLMResponse:
        """One call, retried once with a larger output budget if truncated (AGENTS §8).

        Never continued by prefilling an assistant turn: the retry is the same
        request with more room, or the run fails.
        """
        response = await self._llm.complete(request)
        state.tokens += response.usage.total
        if response.stop_reason is StopReason.MAX_TOKENS:
            larger = min(request.max_output_tokens * 2, self._config.max_output_tokens_ceiling)
            if larger > request.max_output_tokens:
                response = await self._llm.complete(replace(request, max_output_tokens=larger))
                state.tokens += response.usage.total
        return response

    async def _over_budget(self, session: AsyncSession, account_id: int) -> bool:
        limit = self._config.monthly_budget_usd
        if limit is None:
            return False
        spent = await LlmCallRepository(session, account_id).spend_since(
            month_start(self._clock.now())
        )
        return spent >= limit

    def _taint_recorder(self, state: _RunState) -> TaintRecorder:
        """Persist the taint the moment it happens, not at the end of the run:
        a crash mid-run must not leave a tainted run looking clean."""

        async def record() -> None:
            state.tainted = True
            async with transaction(self._session_factory) as uow:
                run = await AgentRunRepository(uow.session, state.account_id).get(state.run_id)
                if run is not None:
                    run.tainted = True

        return record

    def _step_recorder(self, account_id: int, run_id: int) -> StepRecorder:
        async def record(tool_name: str, digest: str | None, status: str, latency_ms: int) -> None:
            async with transaction(self._session_factory) as uow:
                await AgentRunRepository(uow.session, account_id).add_step(
                    run_id,
                    tool_name=tool_name,
                    args_digest=digest,
                    status=status,
                    latency_ms=latency_ms,
                )

        return record

    async def _finish(
        self,
        state: _RunState,
        text: str,
        status: RunStatus,
        error_class: str | None = None,
    ) -> RunOutcome:
        """Reply to the owner, then close the run and record the assistant turn."""
        failure = text if status is not RunStatus.SUCCEEDED else None
        if PROPOSE_OWNER_MESSAGE_FLAG in self._config.enabled_flags:
            scope = _scope(state)
            page = await self._observe_receipts(state)
            current = {r.action_id: r for r in page.receipts}
            baseline = state.final_receipts if state.final_receipts is not None else state.receipts
            changed = _receipt_changes(baseline, current, scope)
            stale_answer = _receipt_changes(state.receipts, current, scope)
            in_scope = [r for r in page.receipts if r.action_id in scope]
            unread = any(
                not r.text_available or state.artifact_reads.get(r.action_id) != r.version
                for r in in_scope
            )
            # The model's own (localized) answer stands while every artifact
            # it could speak about is unchanged and was read at this version;
            # otherwise the status comes from the final observation alone.
            if scope and (changed or stale_answer or unread or status is not RunStatus.SUCCEEDED):
                names: dict[int, str | None] = {}
                if len(in_scope) > 1:
                    async with transaction(self._session_factory) as uow:
                        names = await conversation_names(
                            uow.session,
                            account_id=state.account_id,
                            conversation_ids=tuple(r.conversation_id for r in in_scope),
                        )
                lines = receipt_lines(in_scope, state.locale, names)
                if in_scope:
                    state.refs = state.refs | ToolRefs(
                        conversation_ids=frozenset(r.conversation_id for r in in_scope)
                    )
                    state.used_managed = True
                if scope - current.keys():
                    lines.append(operational_text("receipt_missing", state.locale))
                text = "\n".join(lines)
                state.cited_ids, state.unverified_citations = (), 0
                if unread and all(r.text_available for r in in_scope):
                    text += "\n" + operational_text("receipt_unread", state.locale)
                if any(not r.text_available for r in in_scope):
                    text = owner_texts.DRAFT_TEXT_UNAVAILABLE[state.locale] + "\n" + text
                if state.redraft is not None and failure is not None:
                    # H8-5: a failed Re-draft says why no new card came,
                    # beside the receipt of the card that is still current.
                    text = failure + "\n" + text
            if state.no_action_results:
                questions = [
                    no_action_text(result, reason, state.locale)
                    for result, reason in state.no_action_results
                ]
                text = "\n".join(questions + ([text] if state.action_ids else []))
                if not state.action_ids:
                    state.cited_ids, state.unverified_citations = (), 0
            state.final_receipts = current
        if error_class is not None and status is not RunStatus.SUCCEEDED:
            logger.warning(
                "agent_run_not_succeeded",
                account_id=state.account_id,
                run_id=state.run_id,
                status=status.value,
                error_class=error_class,
            )
        delivery = await self._channel.send(
            account_id=state.account_id, chat_id=state.chat_id, text=text
        )
        async with transaction(self._session_factory) as uow:
            run = await AgentRunRepository(uow.session, state.account_id).get(state.run_id)
            if run is not None:
                run.status = status.value
                run.iterations = state.iterations
                run.finished_at = self._clock.now()
                run.error_class = error_class or (None if delivery.ok else "reply_not_delivered")
            if state.redraft is not None and self._approval_cards is not None:
                # The tapped card showed "drafting": show its current state
                # again (restored choices, or superseded by the replacement).
                self._approval_cards.sync_after_commit(uow.session, state.redraft.action_id)
            # Link the delivered answer to this turn's durable request,
            # without storing a model summary or adding delivery state.
            request_ids = list(
                await uow.session.scalars(
                    select(OwnerRequest.id)
                    .where(
                        OwnerRequest.account_id == state.account_id,
                        or_(
                            OwnerRequest.trigger_turn_id == state.turn_id,
                            OwnerRequest.last_turn_id == state.turn_id,
                        ),
                    )
                    .order_by(OwnerRequest.id)
                )
            )
            continuation_refs = (
                state.continuation.as_refs() if state.continuation is not None else {}
            )
            if request_ids:
                continuation_refs = {"owner_request_ids": request_ids}
            ControlTurnRepository(uow.session, state.account_id).add(
                ControlTurn(
                    role="assistant",
                    text=text,
                    telegram_message_id=delivery.message_id,
                    # A reply built from managed content is itself managed
                    # content: excluded later if `ai_managed` is revoked.
                    contains_managed=state.used_managed,
                    # Historical flags remain evidence. D-S3 drops derived
                    # prose from default cross-run context; if selected again,
                    # from_tainted_run taints regardless of third_party.
                    third_party=state.directly_tainted,
                    from_tainted_run=state.tainted,
                    run_id=state.run_id,
                    # Which conversations/contacts the reply drew on, so a
                    # deletion of one of them can find this turn (§10.4).
                    refs={
                        **state.refs.as_json(),
                        **continuation_refs,
                        **(
                            {"action_ids": sorted(state.final_receipts or {})}
                            if PROPOSE_OWNER_MESSAGE_FLAG in self._config.enabled_flags
                            else {}
                        ),
                    },
                )
            )
        if (
            status is RunStatus.SUCCEEDED
            and delivery.ok
            and state.chat_access_off
            and state.may_ask_managed
        ):
            # After the reply, once per run (`_finish` runs once): the same
            # card `/privacy managed` sends, through the control channel.
            await send_managed_consent_card(
                self._session_factory,
                self._clock,
                self._channel,
                account_id=state.account_id,
                chat_id=state.chat_id,
            )
        await incident_trace.trace_finished_run(self._session_factory, state, status.value)  # I25
        logger.info(
            "agent_run_finished",
            account_id=state.account_id,
            run_id=state.run_id,
            status=status.value,
            iterations=state.iterations,
            delivered=delivery.ok,
            cited=len(state.cited_ids),
            unverified_citations=state.unverified_citations,
            chat_access_off=state.chat_access_off,
        )
        return RunOutcome(
            status,
            state.run_id,
            delivery.message_id,
            account_id=state.account_id,
            ack_latency_ms=state.ack_latency_ms,
            cited_message_ids=state.cited_ids,
            unverified_citations=state.unverified_citations,
            chat_access_off=state.chat_access_off,
        )
