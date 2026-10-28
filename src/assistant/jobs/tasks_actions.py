"""M04 job registration point; bodies are added by their owning units."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from procrastinate import App

from assistant.core.jobs import DRAFT_TASK
from assistant.jobs.app import QUEUE_LLM

if TYPE_CHECKING:
    from assistant.agent.drafting import AssistDraftJob


@dataclass(frozen=True, slots=True)
class ActionsJobHandlers:
    """M04 handlers, populated as the corresponding units land."""

    #: `replies.draft` (S04.2 §3.9); None registers nothing.
    assist_drafts: AssistDraftJob | None = None


def register_actions_tasks(app: App, handlers: ActionsJobHandlers | None) -> None:
    """Omitted groups and handlers register nothing."""
    if handlers is None:
        return
    _register_replies_draft(app, handlers)


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
