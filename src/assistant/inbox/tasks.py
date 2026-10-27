"""Open loops found by triage: commitments, questions and decisions
(ARCHITECTURE §10.3 `tasks`; S05.1, P2-T19).

Tasks are created only by triage in P2 (`created_by = 'agent'`); the owner's
own tasks and reminders are M07. A task's `title` is model-written from
contact text: a content column, shown wrapped as untrusted wherever it
reaches a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: Task kinds triage creates (the `tasks.kind` CHECK also allows `follow_up`
#: and `reminder`, which are M07's).
OWNER_COMMITMENT = "owner_commitment"
CONTACT_COMMITMENT = "contact_commitment"
OPEN_QUESTION = "open_question"
DECISION = "decision"


@dataclass(frozen=True, slots=True)
class TaskView:
    """One task as the triage context, the attention list and the digest see
    it: ids, enums and times, plus its title."""

    task_id: int
    conversation_id: int | None
    kind: str
    title: str
    due_at: datetime | None
    status: str
    source_message_ids: tuple[int, ...]
    created_at: datetime
    closed_at: datetime | None = None
