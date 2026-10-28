"""M04 job registration (F0-interfaces §6; ARCHITECTURE §12.2).

- `actions.execute(action_id)` — queue `outbound`; no lock and no queueing
  lock (Tx A's row locks order it); re-deferred with `schedule_at` on WAIT
  and after a 429; retried only for DB-transient errors before the claim.
- `system.mark_uncertain` — queue `system`, every minute, qlock = its name.
- `replies.draft(conversation_id, reason)` — queue `llm` (S04.2 §3.9).
- `replies.revise(action_id, turn_id)` — queue `llm`, no lock, no retry; its
  queueing lock `revise:{action_id}` is set at defer (S04.3 §3.4).
- `system.expire_actions`, `system.sync_action_cards` — queue `system`, every
  minute, qlock = the name (S04.3 §3.5).

Never `retry=True`, and never a retry after the claim: the executor catches
everything once the claim has committed, and a re-run stops at "not
approved", so even an unexpected retry cannot send twice (S04.4 §3.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from procrastinate import App, RetryStrategy
from sqlalchemy.exc import OperationalError

from assistant.core.jobs import DRAFT_TASK, EXECUTE_TASK, REVISE_TASK
from assistant.jobs.app import QUEUE_LLM, QUEUE_OUTBOUND, QUEUE_SYSTEM

if TYPE_CHECKING:
    from assistant.agent.drafting import AssistDraftJob

MARK_UNCERTAIN_TASK = "system.mark_uncertain"
EXPIRE_ACTIONS_TASK = "system.expire_actions"
SYNC_ACTION_CARDS_TASK = "system.sync_action_cards"

#: Only database hiccups, which can only be raised before Tx A commits
#: (nothing was claimed, so nothing can have been sent). procrastinate counts
#: previous attempts from 0: two retries, three attempts in all.
EXECUTE_RETRY = RetryStrategy(
    max_attempts=2, exponential_wait=2, retry_exceptions={OperationalError}
)


class ActionRunner(Protocol):
    """`actions/executor.py::ActionExecutor`."""

    async def execute(self, action_id: int) -> object: ...

    async def mark_uncertain(self) -> int: ...


class CardJobs(Protocol):
    """`telegram/control/approvals.py::ApprovalCards`."""

    async def expire_due(self) -> int: ...

    async def sync_cards(self) -> int: ...


class RevisionRunner(Protocol):
    """`telegram/control/approvals.py::RevisionJob`."""

    async def run(self, action_id: int, turn_id: int) -> str: ...


@dataclass(frozen=True, slots=True)
class ActionsJobHandlers:
    """M04 handlers, populated as the corresponding units land."""

    #: S04.4: verified execution and the uncertain-attempt sweep.
    executor: ActionRunner | None = None
    #: `replies.draft` (S04.2 §3.9); None registers nothing.
    assist_drafts: AssistDraftJob | None = None
    #: S04.3: card expiry and sync (system queue).
    cards: CardJobs | None = None
    #: S04.3: `replies.revise` (needs the model, so only where it runs).
    revisions: RevisionRunner | None = None


def register_actions_tasks(app: App, handlers: ActionsJobHandlers | None) -> None:
    """Bind the M04 tasks whose handlers are wired; omitted ones register nothing."""
    if handlers is None:
        return
    if handlers.executor is not None:
        _register_execution(app, handlers.executor)
    _register_replies_draft(app, handlers)
    if handlers.cards is not None:
        _register_cards(app, handlers.cards)
    if handlers.revisions is not None:
        _register_revise(app, handlers.revisions)


def _register_execution(app: App, executor: ActionRunner) -> None:
    @app.task(name=EXECUTE_TASK, queue=QUEUE_OUTBOUND, retry=EXECUTE_RETRY)
    async def execute(action_id: int) -> None:
        await executor.execute(action_id)

    @app.periodic(cron="* * * * *")
    @app.task(name=MARK_UNCERTAIN_TASK, queue=QUEUE_SYSTEM, queueing_lock=MARK_UNCERTAIN_TASK)
    async def mark_uncertain(timestamp: int) -> None:
        await executor.mark_uncertain()


def _register_replies_draft(app: App, handlers: ActionsJobHandlers) -> None:
    drafts = handlers.assist_drafts
    if drafts is None:
        return

    @app.task(name=DRAFT_TASK, queue=QUEUE_LLM)
    async def replies_draft(conversation_id: int, reason: str) -> None:
        # Queueing lock `draft:{id}` (or `redraft:{id}`) and `schedule_at` are
        # set at defer; never a `lock` on a future-scheduled job (AGENTS §13).
        # No procrastinate retry: a model call is not repeated blindly (cost),
        # and a lost or failed proactive job leaves `pending_draft_at` for the
        # drafts reconciler (S04.2 §3.9).
        await drafts.run(conversation_id, reason)


def _register_cards(app: App, cards: CardJobs) -> None:
    @app.periodic(cron="* * * * *")
    @app.task(name=EXPIRE_ACTIONS_TASK, queue=QUEUE_SYSTEM, queueing_lock=EXPIRE_ACTIONS_TASK)
    async def expire_actions(timestamp: int) -> None:
        await cards.expire_due()

    @app.periodic(cron="* * * * *")
    @app.task(name=SYNC_ACTION_CARDS_TASK, queue=QUEUE_SYSTEM, queueing_lock=SYNC_ACTION_CARDS_TASK)
    async def sync_action_cards(timestamp: int) -> None:
        await cards.sync_cards()


def _register_revise(app: App, revisions: RevisionRunner) -> None:
    @app.task(name=REVISE_TASK, queue=QUEUE_LLM)
    async def replies_revise(action_id: int, turn_id: int) -> None:
        # No procrastinate retry: a model call is not repeated blindly; a
        # lost or crashed revision is re-deferred once by the revising
        # reconciler, and given up after 30 minutes (S04.3 §3.4).
        await revisions.run(action_id, turn_id)
