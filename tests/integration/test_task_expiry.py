"""S05.1 §3.7: the hourly `system.expire_tasks` sweep — one test per rule,
over real Postgres. Synthetic titles only.

Required test, findable by name: `test_done_tasks_never_expire`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from assistant.db.uow import transaction
from assistant.inbox import tasks
from assistant.inbox.models import Notification, Task
from tests.integration.agent_env import one_id
from tests.integration.business_env import (
    CONTACT,
    NOW,
    Env,
    business_message,
    connect,
    deliver,
    environment,
)

pytestmark = pytest.mark.integration

#: The source message's Telegram date (`sent_at`), an hour before the clock.
SENT = datetime(2026, 10, 18, 11, 0, tzinfo=UTC)


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def chat(env: Env) -> tuple[int, int, int]:
    """(account, conversation, source message) for one synthetic chat."""
    await connect(env, date=int((SENT - timedelta(hours=1)).timestamp()))
    await deliver(
        env, business_message(message_id=11, message_text="SYNTH", date=int(SENT.timestamp()))
    )
    conversation = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    account = await one_id(
        env, "SELECT account_id FROM app.conversations WHERE id = :c", c=conversation
    )
    message = await one_id(
        env, "SELECT id FROM app.messages WHERE conversation_id = :c", c=conversation
    )
    return account, conversation, message


async def add_task(
    env: Env,
    ids: tuple[int, int, int],
    kind: str,
    *,
    created_at: datetime = NOW,
    due_at: datetime | None = None,
    sources: bool = True,
    created_by: str = "agent",
    status: str = "open",
) -> int:
    account, conversation, message = ids
    async with env.factory() as session, session.begin():
        task_id: int = (
            await session.execute(
                text(
                    "INSERT INTO app.tasks (account_id, conversation_id, kind, title, due_at, "
                    "status, created_by, created_at, source_message_ids, "
                    "first_source_message_id) "
                    "VALUES (:a, :c, :k, 'Synthetic', :due, :st, :by, :at, "
                    "CAST(:src AS jsonb), :first) RETURNING id"
                ),
                {
                    "a": account,
                    "c": conversation,
                    "k": kind,
                    "due": due_at,
                    "st": status,
                    "by": created_by,
                    "at": created_at,
                    "src": f"[{message}]" if sources else "[]",
                    # One row per (conversation, kind, first source) for agent tasks.
                    "first": message if sources and created_by == "agent" else None,
                },
            )
        ).scalar_one()
    return task_id


async def sweep_at(env: Env, at: datetime) -> int:
    env.clock.set(at)
    async with transaction(env.factory) as uow:
        return await tasks.expire_due(uow.session, clock=env.clock)


async def status(env: Env, task_id: int) -> tuple[str, str | None]:
    async with env.factory() as session:
        row = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        return row.status, row.closed_reason


async def test_a_question_expires_14_days_after_its_newest_source_message(env: Env) -> None:
    ids = await chat(env)
    # Created later than the message: the message's time is what counts.
    question = await add_task(env, ids, "open_question", created_at=NOW + timedelta(days=3))
    assert await sweep_at(env, SENT + timedelta(days=14) - timedelta(minutes=1)) == 0
    assert await status(env, question) == ("open", None)
    assert await sweep_at(env, SENT + timedelta(days=14, minutes=1)) == 1
    assert await status(env, question) == ("expired", tasks.EXPIRED)


async def test_a_decision_without_sources_expires_14_days_after_creation(env: Env) -> None:
    ids = await chat(env)
    decision = await add_task(env, ids, "decision", sources=False)
    assert await sweep_at(env, NOW + timedelta(days=13)) == 0
    assert await sweep_at(env, NOW + timedelta(days=14, minutes=1)) == 1
    assert (await status(env, decision))[0] == "expired"


async def test_a_dated_commitment_expires_7_days_after_its_due_time(env: Env) -> None:
    ids = await chat(env)
    due = NOW + timedelta(days=2)
    commitment = await add_task(env, ids, "owner_commitment", due_at=due)
    # Undated rule (30 days) does not apply to a dated commitment.
    assert await sweep_at(env, due + timedelta(days=6)) == 0
    assert await sweep_at(env, due + timedelta(days=7, minutes=1)) == 1
    assert (await status(env, commitment))[0] == "expired"


async def test_an_undated_commitment_expires_30_days_after_creation(env: Env) -> None:
    ids = await chat(env)
    commitment = await add_task(env, ids, "contact_commitment")
    assert await sweep_at(env, NOW + timedelta(days=29)) == 0
    assert await sweep_at(env, NOW + timedelta(days=30, minutes=1)) == 1
    assert (await status(env, commitment))[0] == "expired"


async def test_done_tasks_never_expire(env: Env) -> None:
    ids = await chat(env)
    done = await add_task(env, ids, "open_question", status="done")
    assert await sweep_at(env, NOW + timedelta(days=400)) == 0
    assert (await status(env, done))[0] == "done"


async def test_owner_created_tasks_and_reminders_never_expire(env: Env) -> None:
    ids = await chat(env)
    owner_made = await add_task(env, ids, "owner_commitment", created_by="owner", sources=False)
    reminder = await add_task(env, ids, "reminder", sources=False)
    assert await sweep_at(env, NOW + timedelta(days=400)) == 0
    assert (await status(env, owner_made))[0] == "open"
    assert (await status(env, reminder))[0] == "open"


async def test_expiry_is_silent(env: Env) -> None:
    ids = await chat(env)
    await add_task(env, ids, "contact_commitment")
    assert await sweep_at(env, NOW + timedelta(days=31)) == 1
    async with env.factory() as session:
        assert (await session.execute(select(Notification))).scalars().all() == []
