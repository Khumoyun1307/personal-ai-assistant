"""I11: minimal owner task controls. Synthetic data; no external APIs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from assistant.accounts.models import Account, AuditLogEntry
from assistant.agent.tools.registry import (
    CapabilityTier,
    ToolContext,
    ToolServices,
    default_registry,
)
from assistant.agent.tools.tasks import (
    ListTasksArgs,
    UpdateTaskArgs,
    list_tasks_tool,
    update_task_tool,
)
from assistant.core.errors import NotInAccountError
from assistant.db.uow import transaction
from assistant.inbox import task_controls
from assistant.inbox.models import Task
from assistant.memory.undo import UndoStatus, undo
from assistant.privacy.p2_deletion import purge_p2
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import TASK_FLAGS, applier, ask, runtime
from tests.integration.business_env import Env, environment, grant_managed
from tests.integration.p2_factories import seed_account, seed_conversation
from tests.integration.test_task_expiry import add_task, chat
from tests.integration.test_tool_confirmations import confirm

pytestmark = pytest.mark.integration


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


def context(env: Env, account_id: int, *, call_id: str = "c1") -> ToolContext:
    return ToolContext(
        account_id=account_id,
        run_id=7,
        run_type="owner_request",
        tainted=False,
        managed_ok=True,
        call_id=call_id,
        services=ToolServices(
            session_factory=env.factory,
            clock=env.clock,
            monthly_budget_usd=None,
            channel=FakeChannel(),
        ),
    )


async def read(env: Env, account_id: int, task_id: int) -> Task:
    async with env.factory() as session:
        return await task_controls.get(session, account_id=account_id, task_id=task_id)


async def audit(env: Env, account_id: int) -> list[AuditLogEntry]:
    async with env.factory() as session:
        rows = await session.scalars(
            select(AuditLogEntry)
            .where(AuditLogEntry.account_id == account_id)
            .where(AuditLogEntry.event == task_controls.EVENT_TASK_UPDATED)
            .order_by(AuditLogEntry.id)
        )
        return list(rows.all())


@pytest.mark.parametrize(("operation", "expected"), [("complete", "done"), ("cancel", "cancelled")])
async def test_owner_can_close_with_undo(env: Env, operation: str, expected: str) -> None:
    ids = await chat(env)
    task_id = await add_task(env, ids, "owner_commitment")
    ctx = context(env, ids[0])
    result = await update_task_tool(
        ctx,
        UpdateTaskArgs.model_validate(
            {
                "task_id": task_id,
                "operation": operation,
                "title": None,
                "due_at": None,
                "change_due": False,
            }
        ),
    )
    assert result.status == "ok"
    assert result.data["changed"] is True
    assert "Undo button" in result.data["echo"]
    assert (await read(env, ids[0], task_id)).status == expected
    [entry] = await audit(env, ids[0])
    assert "Synthetic" not in json.dumps(entry.details)
    async with transaction(env.factory) as uow:
        undone = await undo(uow.session, account_id=ids[0], change_id=entry.id, clock=env.clock)
    assert undone.status == UndoStatus.UNDONE
    restored = await read(env, ids[0], task_id)
    assert restored.status == "open"
    assert restored.closed_at is None
    assert restored.closed_reason is None
    async with transaction(env.factory) as uow:
        twice = await undo(uow.session, account_id=ids[0], change_id=entry.id, clock=env.clock)
    assert twice.status == UndoStatus.SUPERSEDED


async def test_correct_title_and_due_date_and_undo_preserves_original_details(env: Env) -> None:
    ids = await chat(env)
    task_id = await add_task(env, ids, "contact_commitment")
    async with transaction(env.factory) as uow:
        account = await uow.session.get(Account, ids[0])
        assert account is not None
        account.timezone = "Asia/Tashkent"
        row = await task_controls.get(uow.session, account_id=ids[0], task_id=task_id)
        row.details = "Synthetic original details"
    result = await update_task_tool(
        context(env, ids[0]),
        UpdateTaskArgs(
            task_id=task_id,
            operation="correct",
            title="Corrected synthetic task",
            due_at="2026-11-10 12:30",
            change_due=True,
        ),
    )
    assert result.data["changed"] is True
    row = await read(env, ids[0], task_id)
    assert row.title == "Corrected synthetic task"
    assert row.due_at == datetime(2026, 11, 10, 7, 30, tzinfo=UTC)
    assert row.status == "open"
    [entry] = await audit(env, ids[0])
    assert "Corrected" not in json.dumps(entry.details)
    async with transaction(env.factory) as uow:
        undone = await undo(uow.session, account_id=ids[0], change_id=entry.id, clock=env.clock)
    assert undone.status == UndoStatus.UNDONE
    restored = await read(env, ids[0], task_id)
    assert restored.title == "Synthetic"
    assert restored.due_at is None
    assert restored.details == "Synthetic original details"


async def test_clear_due_date_and_later_correction_supersedes_undo(env: Env) -> None:
    ids = await chat(env)
    task_id = await add_task(env, ids, "contact_commitment", due_at=env.clock.now())
    await update_task_tool(
        context(env, ids[0]),
        UpdateTaskArgs(
            task_id=task_id, operation="correct", title=None, due_at=None, change_due=True
        ),
    )
    [first] = await audit(env, ids[0])
    assert (await read(env, ids[0], task_id)).due_at is None
    await update_task_tool(
        context(env, ids[0], call_id="c2"),
        UpdateTaskArgs(
            task_id=task_id,
            operation="correct",
            title="Later correction",
            due_at=None,
            change_due=False,
        ),
    )
    async with transaction(env.factory) as uow:
        outcome = await undo(uow.session, account_id=ids[0], change_id=first.id, clock=env.clock)
    assert outcome.status == UndoStatus.SUPERSEDED
    assert (await read(env, ids[0], task_id)).title == "Later correction"


async def test_task_change_retry_does_not_reapply_after_undo(env: Env) -> None:
    ids = await chat(env)
    task_id = await add_task(env, ids, "contact_commitment")
    ctx = context(env, ids[0])
    args = UpdateTaskArgs(
        task_id=task_id, operation="complete", title=None, due_at=None, change_due=False
    )
    await update_task_tool(ctx, args)
    [entry] = await audit(env, ids[0])
    async with transaction(env.factory) as uow:
        await undo(uow.session, account_id=ids[0], change_id=entry.id, clock=env.clock)
    retried = await update_task_tool(ctx, args)
    assert retried.data["changed"] is False
    assert retried.data["reason"] == "already_applied"
    assert (await read(env, ids[0], task_id)).status == "open"
    assert len(await audit(env, ids[0])) == 1


async def test_triage_evidence_supersedes_a_correction_undo(env: Env) -> None:
    ids = await chat(env)
    task_id = await add_task(env, ids, "owner_commitment")
    await update_task_tool(
        context(env, ids[0]),
        UpdateTaskArgs(
            task_id=task_id,
            operation="correct",
            title="Owner correction",
            due_at=None,
            change_due=False,
        ),
    )
    [entry] = await audit(env, ids[0])
    async with transaction(env.factory) as uow:
        row = await task_controls.get(uow.session, account_id=ids[0], task_id=task_id)
        row.title = "New synthetic evidence"
        row.updated_at = env.clock.now() + timedelta(minutes=1)
    async with transaction(env.factory) as uow:
        outcome = await undo(uow.session, account_id=ids[0], change_id=entry.id, clock=env.clock)
    assert outcome.status == UndoStatus.SUPERSEDED
    assert (await read(env, ids[0], task_id)).title == "New synthetic evidence"


async def test_closed_tasks_are_never_reopened_by_a_correction(env: Env) -> None:
    ids = await chat(env)
    task_id = await add_task(env, ids, "owner_commitment", status="done")
    result = await update_task_tool(
        context(env, ids[0]),
        UpdateTaskArgs(
            task_id=task_id,
            operation="correct",
            title="Attempted correction",
            due_at=None,
            change_due=False,
        ),
    )
    assert result.status == "error"
    assert result.data["changed"] is False
    assert result.data["reason"] == "not_open"
    assert (await read(env, ids[0], task_id)).status == "done"
    assert await audit(env, ids[0]) == []


async def test_list_open_tasks_includes_undated_overdue_and_stale_review(env: Env) -> None:
    ids = await chat(env)
    undated = await add_task(
        env, ids, "owner_commitment", created_at=env.clock.now() - timedelta(days=31)
    )
    overdue = await add_task(
        env, ids, "contact_commitment", due_at=env.clock.now() - timedelta(days=1)
    )
    await add_task(env, ids, "decision", status="done")
    args = ListTasksArgs(
        conversation_id=None, stale_after_days=30, stale_only=False, after_task_id=None, limit=1
    )
    result = await list_tasks_tool(context(env, ids[0]), args)
    [item] = result.data["tasks"]
    assert item["task_id"] == undated
    assert item["due_state"] == "no_due_date"
    assert item["needs_review"] is True
    assert '<untrusted source="task_title"' in item["title"]
    assert result.taints is True
    assert result.contains_managed is True
    assert result.refs.conversation_ids == frozenset({ids[1]})
    page = await list_tasks_tool(
        context(env, ids[0]),
        args.model_copy(update={"after_task_id": result.data["next_after_task_id"]}),
    )
    [item] = page.data["tasks"]
    assert item["task_id"] == overdue
    assert item["due_state"] == "overdue"
    assert item["needs_review"] is False
    assert page.data["next_after_task_id"] is None
    stale = await list_tasks_tool(
        context(env, ids[0]), args.model_copy(update={"stale_only": True})
    )
    assert [item["task_id"] for item in stale.data["tasks"]] == [undated]
    assert (await read(env, ids[0], undated)).status == "open"


async def test_foreign_task_ids_are_not_found_and_list_is_scoped(env: Env) -> None:
    ids = await chat(env)
    async with transaction(env.factory) as uow:
        other = await seed_account(uow.session)
        conversation = await seed_conversation(uow.session, account_id=other.id)
        foreign = Task(
            account_id=other.id,
            conversation_id=conversation.id,
            kind="owner_commitment",
            title="Foreign synthetic task",
            created_by="agent",
            created_at=env.clock.now(),
            updated_at=env.clock.now(),
        )
        uow.session.add(foreign)
        await uow.session.flush()
        foreign_id = foreign.id
    with pytest.raises(NotInAccountError):
        await update_task_tool(
            context(env, ids[0]),
            UpdateTaskArgs(
                task_id=foreign_id,
                operation="complete",
                title=None,
                due_at=None,
                change_due=False,
            ),
        )
    result = await list_tasks_tool(
        context(env, ids[0]),
        ListTasksArgs(
            conversation_id=None,
            stale_after_days=30,
            stale_only=False,
            after_task_id=None,
            limit=50,
        ),
    )
    assert result.data["tasks"] == []
    assert await audit(env, ids[0]) == []


async def test_tainted_task_change_is_a_card_then_applies_with_undo(env: Env) -> None:
    ids = await chat(env)
    await grant_managed(env)
    task_id = await add_task(env, ids, "owner_commitment")
    turn = await ask(env, "List tasks then cancel the incorrect one")
    fake = FakeLLM(
        tool_response(
            (
                "list",
                "list_tasks",
                json.dumps(
                    {
                        "conversation_id": None,
                        "stale_after_days": 30,
                        "stale_only": False,
                        "after_task_id": None,
                        "limit": 50,
                    }
                ),
            )
        ),
        tool_response(
            (
                "change",
                "update_task",
                json.dumps(
                    {
                        "task_id": task_id,
                        "operation": "cancel",
                        "title": None,
                        "due_at": None,
                        "change_due": False,
                    }
                ),
            )
        ),
        text_response("Please confirm the change."),
    )
    channel = FakeChannel()
    await runtime(env, fake, channel, flags=TASK_FLAGS).run(turn)
    assert (await read(env, ids[0], task_id)).status == "open"
    assert "Confirm a change?" in channel.sends[0]
    assert "Synthetic" in channel.sends[0]
    assert await audit(env, ids[0]) == []
    confirmation_id = await confirm(env)
    applied_channel = FakeChannel()
    applied = await applier(env, applied_channel, flags=TASK_FLAGS).apply(confirmation_id)
    assert applied.status == "applied"
    assert (await read(env, ids[0], task_id)).status == "cancelled"
    assert [button.label for button in applied_channel.buttons[0]] == ["Undo"]


async def test_deleted_task_snapshot_cannot_be_restored_by_undo(env: Env) -> None:
    ids = await chat(env)
    task_id = await add_task(env, ids, "contact_commitment")
    await update_task_tool(
        context(env, ids[0]),
        UpdateTaskArgs(
            task_id=task_id,
            operation="correct",
            title="Synthetic correction",
            due_at=None,
            change_due=False,
        ),
    )
    [entry] = await audit(env, ids[0])
    async with transaction(env.factory) as uow:
        counts = await purge_p2(
            uow.session,
            account_id=ids[0],
            conversation_ids=[ids[1]],
            contact_ids=[],
            scope="scoped",
            now=env.clock.now(),
        )
        assert counts["tasks"] == 1
    async with transaction(env.factory) as uow:
        outcome = await undo(uow.session, account_id=ids[0], change_id=entry.id, clock=env.clock)
    assert outcome.status == UndoStatus.SUPERSEDED


def test_task_tools_default_off_and_managed_read_is_a_blocked_stub() -> None:
    registry = default_registry()
    assert (
        registry.for_run(
            "owner_request",
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=frozenset(),
            managed_consent=True,
        )
        == {}
    )
    offered = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=TASK_FLAGS,
        managed_consent=False,
        stub_managed_tools=True,
    )
    assert offered["list_tasks"].returns_managed is False
    assert offered["list_tasks"].handler is not list_tasks_tool
