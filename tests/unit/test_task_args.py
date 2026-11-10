"""I11 semantic validation runs before the confirmation gate."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from assistant.agent.tools.tasks import ListTasksArgs, UpdateTaskArgs


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": 0},
        {"task_id": 2**63},
        {"operation": "complete", "title": "A correction"},
        {"operation": "cancel", "change_due": True},
        {"operation": "correct"},
        {"operation": "correct", "title": " "},
        {"operation": "correct", "title": "x" * 401},
        {"due_at": "2026-11-10 10:00", "change_due": False},
        {"operation": "correct", "change_due": True, "due_at": "not a date"},
        {"operation": "correct", "change_due": True, "due_at": "2026-11-10 25:00"},
    ],
)
def test_semantically_invalid_updates_are_refused(changes: dict[str, Any]) -> None:
    args: dict[str, Any] = {
        "task_id": 1,
        "operation": "complete",
        "title": None,
        "due_at": None,
        "change_due": False,
    }
    with pytest.raises(ValidationError):
        UpdateTaskArgs.model_validate(args | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"stale_after_days": 0},
        {"stale_after_days": 3651},
        {"limit": 0},
        {"limit": 51},
        {"conversation_id": -1},
        {"after_task_id": 2**63},
    ],
)
def test_semantically_invalid_lists_are_refused(changes: dict[str, Any]) -> None:
    args: dict[str, Any] = {
        "conversation_id": None,
        "stale_after_days": 30,
        "stale_only": False,
        "after_task_id": None,
        "limit": 50,
    }
    with pytest.raises(ValidationError):
        ListTasksArgs.model_validate(args | changes)
