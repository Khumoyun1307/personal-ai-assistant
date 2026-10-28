"""S05.1 §3.6, §3.8: when triage runs — the ingestion listener, the
`inbox.triage` job's trailing debounce, the reconciler — over the real inbox,
jobs table and Postgres.

Required tests, findable by name: `test_a_burst_is_debounced_into_one_triage_run`,
`test_the_reconciler_recovers_a_lost_triage_defer`.

Synthetic text only; FakeLLM stands in for the provider (AGENTS §11).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from assistant.agent.triage import TriageConfig, Triager, TriageStatus
from assistant.core.jobs import TRIAGE_TASK
from assistant.inbox.triage_store import TriageRepository
from assistant.inbox.triage_trigger import TriageIngestionListener
from assistant.jobs.reconcile_inbox import triage_reconciler
from assistant.jobs.tasks_inbox import TriageJob
from assistant.llm.port import LLMRequest
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import gated, one_id
from tests.integration.business_env import (
    CONTACT,
    NOW,
    OWNER,
    Env,
    business_message,
    connect,
    deleted_messages,
    deliver,
    environment,
    grant_managed,
)

pytestmark = pytest.mark.integration

DEBOUNCE = timedelta(seconds=20)
#: Message dates, before the FakeClock's noon.
AT = datetime(2026, 10, 18, 11, 0, tzinfo=UTC)


def ts(minutes: int) -> int:
    return int((AT + timedelta(minutes=minutes)).timestamp())


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    """The environment with the triage listener wired, as `_wire_m05` does
    with flag `triage.run` on (its clock is the environment's own)."""
    listener = _Late()
    async with environment(migrated_url, listeners=[listener]) as built:
        assert built.scheduler is not None
        listener.bind(
            TriageIngestionListener(scheduler=built.scheduler, clock=built.clock, debounce=DEBOUNCE)
        )
        yield built


class _Late:
    """The listener needs the environment's scheduler and clock, which exist
    only once the environment is built; it is bound right after."""

    def __init__(self) -> None:
        self._inner: TriageIngestionListener | None = None

    def bind(self, inner: TriageIngestionListener) -> None:
        self._inner = inner

    async def on_message(self, session: Any, change: Any) -> None:
        assert self._inner is not None
        await self._inner.on_message(session, change)

    async def on_deletion(self, session: Any, change: Any) -> None:
        assert self._inner is not None
        await self._inner.on_deletion(session, change)


async def chat(env: Env) -> int:
    await connect(env, date=ts(-60))
    await grant_managed(env)
    await deliver(env, business_message(message_id=11, message_text="SYNTH 1", date=ts(0)))
    return await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )


async def flag(env: Env, conversation: int) -> datetime | None:
    async with env.factory() as session:
        value: datetime | None = (
            await session.execute(
                text("SELECT pending_triage_at FROM app.conversations WHERE id = :c"),
                {"c": conversation},
            )
        ).scalar_one()
        return value


async def triage_jobs(env: Env) -> list[tuple[int, str, datetime | None]]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT (args->>'conversation_id')::bigint, status::text, scheduled_at "
                "FROM procrastinate.procrastinate_jobs WHERE task_name = :t ORDER BY id"
            ),
            {"t": TRIAGE_TASK},
        )
        return [(int(r[0]), str(r[1]), r[2]) for r in rows.all()]


def job(env: Env, fake: FakeLLM) -> TriageJob:
    assert env.scheduler is not None
    triager = Triager(
        session_factory=env.factory,
        llm=gated(env, fake),
        clock=env.clock,
        config=TriageConfig(model="gpt-6-luna"),
        store=TriageRepository(clock=env.clock, scheduler=None),
    )
    return TriageJob(
        session_factory=env.factory,
        triager=triager,
        scheduler=env.scheduler,
        clock=env.clock,
        debounce=DEBOUNCE,
    )


def quiet_verdict(request: LLMRequest) -> dict[str, Any]:
    return {
        "language": "en",
        "needs_reply": False,
        "contains_question_to_owner": False,
        "requests_decision": False,
        "urgency": "low",
        "deadline": None,
        "commitments": [],
        "open_questions": [],
        "resolved_task_ids": [],
        "escalation_category": "fyi",
        "reasons": [],
        "script": "latin",
    }


async def test_a_burst_is_debounced_into_one_triage_run(env: Env) -> None:
    conversation = await chat(env)
    env.clock.advance(seconds=5)
    await deliver(env, business_message(message_id=12, message_text="SYNTH 2", date=ts(1)))
    env.clock.advance(seconds=5)
    await deliver(env, business_message(message_id=13, message_text="SYNTH 3", date=ts(2)))
    # One flag (from the first message) and one queued job for the burst.
    assert await flag(env, conversation) == NOW
    assert [(c, s) for c, s, _ in await triage_jobs(env)] == [(conversation, "todo")]

    fake = FakeLLM()
    fake.parsed = [quiet_verdict]
    # 10 s after the last message: still inside the debounce, so the job only
    # re-defers itself to 20 s after it (the first job is `todo` here, so the
    # re-defer finds the queueing lock taken — in production the running job
    # is `doing` and it goes through).
    assert await job(env, fake).triage(conversation) is None
    assert fake.requests == []
    env.clock.advance(seconds=25)
    outcome = await job(env, fake).triage(conversation)
    assert outcome is not None
    assert outcome.status is TriageStatus.TRIAGED
    assert len(fake.requests) == 1  # one run for the three messages
    assert await flag(env, conversation) is None


async def test_owner_messages_flag_only_a_chat_with_an_open_task(env: Env) -> None:
    conversation = await chat(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET pending_triage_at = NULL WHERE id = :c"),
            {"c": conversation},
        )
    await deliver(
        env, business_message(message_id=12, from_id=OWNER, message_text="SYNTH ok", date=ts(5))
    )
    assert await flag(env, conversation) is None  # nothing an answer could close
    account = await one_id(
        env, "SELECT account_id FROM app.conversations WHERE id = :c", c=conversation
    )
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO app.tasks (account_id, conversation_id, kind, title, created_by) "
                "VALUES (:a, :c, 'open_question', 'Synthetic', 'agent')"
            ),
            {"a": account, "c": conversation},
        )
    await deliver(
        env, business_message(message_id=13, from_id=OWNER, message_text="SYNTH ha", date=ts(6))
    )
    assert await flag(env, conversation) == env.clock.now()


async def test_ignored_chats_and_automatic_messages_set_no_flag(env: Env) -> None:
    conversation = await chat(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE app.conversations SET pending_triage_at = NULL, mode = 'ignore' "
                "WHERE id = :c"
            ),
            {"c": conversation},
        )
    await deliver(env, business_message(message_id=12, message_text="SYNTH x", date=ts(5)))
    assert await flag(env, conversation) is None
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'observe' WHERE id = :c"),
            {"c": conversation},
        )
    await deliver(
        env,
        business_message(
            message_id=13,
            from_id=OWNER,
            message_text="SYNTH away",
            date=ts(6),
            extra={"is_from_offline": True},
        ),
    )
    assert await flag(env, conversation) is None


async def test_the_reconciler_recovers_a_lost_triage_defer(env: Env) -> None:
    """Flag set, job lost (a crash between the commit and the defer): after
    30 s the reconciler re-creates it; with a job queued, nothing more."""
    conversation = await chat(env)
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM procrastinate.procrastinate_jobs"))
    reconcile = triage_reconciler()
    async with env.factory() as session:
        assert await reconcile(session, env.clock.now()) == []  # too young
    env.clock.advance(seconds=31)
    async with env.factory() as session:
        (request,) = await reconcile(session, env.clock.now())
    assert (request.task, dict(request.args), request.queueing_lock) == (
        TRIAGE_TASK,
        {"conversation_id": conversation},
        f"triage:{conversation}",
    )
    assert env.scheduler is not None
    await env.scheduler.defer(request.task, args=request.args, queueing_lock=request.queueing_lock)
    async with env.factory() as session:
        assert await reconcile(session, env.clock.now()) == []


async def test_the_reconciler_skips_chats_without_managed_consent(env: Env) -> None:
    conversation = await chat(env)
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM procrastinate.procrastinate_jobs"))
        await session.execute(
            text("UPDATE app.consents SET revoked_at = now() WHERE kind = 'ai_managed'")
        )
    env.clock.advance(seconds=31)
    async with env.factory() as session:
        assert await triage_reconciler()(session, env.clock.now()) == []
    assert conversation


# ------------------------------------------------- the flag after a verdict
async def settled_chat(env: Env) -> int:
    """A chat whose first burst is triaged: a verdict, and no flag."""
    conversation = await chat(env)
    env.clock.advance(seconds=25)
    fake = FakeLLM()
    fake.parsed = [quiet_verdict]
    outcome = await job(env, fake).triage(conversation)
    assert outcome is not None
    assert outcome.status is TriageStatus.TRIAGED
    assert await flag(env, conversation) is None
    return conversation


async def assert_settled(env: Env, conversation: int) -> None:
    """No flag, so nothing for the reconciler to re-create once every queued
    job has run (here: is gone) and the 30 s grace has passed."""
    assert await flag(env, conversation) is None
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM procrastinate.procrastinate_jobs"))
    env.clock.advance(seconds=31)
    async with env.factory() as session:
        assert await triage_reconciler()(session, env.clock.now()) == []


async def a_location(env: Env) -> None:
    """Stored with neither text nor media (FR-MEM-05): no run can show it."""
    await deliver(
        env,
        business_message(
            message_id=12,
            message_text=None,
            date=ts(5),
            extra={"location": {"latitude": 0.0, "longitude": 0.0}},
        ),
    )


async def a_deleted_message(env: Env) -> None:
    await deliver(env, business_message(message_id=12, message_text="SYNTH oops", date=ts(5)))
    await deliver(env, deleted_messages(message_ids=[12]))


@pytest.mark.parametrize("arrive", [a_location, a_deleted_message], ids=["location", "deleted"])
async def test_a_message_triage_cannot_read_never_keeps_the_flag(
    env: Env, arrive: Callable[[Env], Awaitable[None]]
) -> None:
    """Viro on #92: the flag counts only what a run could read. A contact's
    location, or a message deleted before the run, sets the flag; the run
    finds nothing new and clears it. Otherwise the reconciler re-creates the
    job every minute until the contact writes again."""
    conversation = await settled_chat(env)
    env.clock.advance(seconds=5)
    await arrive(env)
    assert await flag(env, conversation) == env.clock.now()
    env.clock.advance(seconds=25)
    fake = FakeLLM()
    outcome = await job(env, fake).triage(conversation)
    assert outcome is not None
    assert (outcome.status, outcome.calls) == (TriageStatus.UP_TO_DATE, 0)
    await assert_settled(env, conversation)


async def test_a_burst_whose_newest_message_is_deleted_during_the_call_settles(env: Env) -> None:
    """The verdict is discarded and the next run reads the rest; the deleted
    message after it keeps no flag."""
    conversation = await chat(env)
    env.clock.advance(seconds=5)
    await deliver(env, business_message(message_id=12, message_text="SYNTH 2", date=ts(1)))
    env.clock.advance(seconds=25)

    async def delete_then_answer(request: LLMRequest) -> dict[str, Any]:
        await deliver(env, deleted_messages(message_ids=[12]))
        return quiet_verdict(request)

    fake = FakeLLM()
    fake.parsed = [delete_then_answer, quiet_verdict]
    first = await job(env, fake).triage(conversation)
    assert first is not None
    assert first.status is TriageStatus.DISCARDED
    assert await flag(env, conversation) is not None  # what remains is still to triage
    second = await job(env, fake).triage(conversation)
    assert second is not None
    assert (second.status, second.calls) == (TriageStatus.TRIAGED, 1)
    await assert_settled(env, conversation)


async def test_an_automatic_message_during_the_call_keeps_no_flag(env: Env) -> None:
    """An away/greeting message triggers no triage (the listener), so one that
    arrives during a call keeps no flag either: the next burst reads it."""
    conversation = await chat(env)
    env.clock.advance(seconds=25)

    async def away_then_answer(request: LLMRequest) -> dict[str, Any]:
        await deliver(
            env,
            business_message(
                message_id=12,
                from_id=OWNER,
                message_text="SYNTH away",
                date=ts(1),
                extra={"is_from_offline": True},
            ),
        )
        return quiet_verdict(request)

    fake = FakeLLM()
    fake.parsed = [away_then_answer]
    outcome = await job(env, fake).triage(conversation)
    assert outcome is not None
    assert outcome.status is TriageStatus.TRIAGED
    await assert_settled(env, conversation)


async def test_a_contact_message_during_the_call_keeps_the_flag_for_the_next_run(
    env: Env,
) -> None:
    """The counterpart: a message triage can read keeps the flag, at its
    arrival time, and the job re-defers one debounce after it."""
    conversation = await chat(env)
    async with env.factory() as session, session.begin():  # the running job is not `todo`
        await session.execute(text("DELETE FROM procrastinate.procrastinate_jobs"))
    env.clock.advance(seconds=25)
    arrived = env.clock.now()

    async def write_then_answer(request: LLMRequest) -> dict[str, Any]:
        await deliver(env, business_message(message_id=12, message_text="SYNTH 2", date=ts(1)))
        return quiet_verdict(request)

    fake = FakeLLM()
    fake.parsed = [write_then_answer]
    outcome = await job(env, fake).triage(conversation)
    assert outcome is not None
    assert outcome.status is TriageStatus.TRIAGED
    assert await flag(env, conversation) == arrived
    assert await triage_jobs(env) == [(conversation, "todo", arrived + DEBOUNCE)]
