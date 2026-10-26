"""S03.3 message retention: rollup before the ceiling, hard purge at it
(ARCHITECTURE §10.4; PRD R-07, FR-PRV-03).

The required S03.3 tests (IMPLEMENTATION_PLAN §5):

- with no `ai_managed` consent, after 91 simulated days no content older
  than 90 days remains and the summary gap is recorded —
  `test_without_managed_consent_after_91_days_no_old_content_remains_and_the_gap_is_recorded`;
- with consent, the rollup precedes the purge —
  `test_with_managed_consent_the_rollup_precedes_the_purge`.

Plus: the purge does not wait for a rollup that fails (provider outage),
and records the gap; forgotten memory items go once the Undo window passes.
Synthetic text only; FakeLLM stands in for the provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.conversations.models import Conversation, Message
from assistant.inbox.notify import OwnerAlerts
from assistant.jobs.tasks import JobHandlers
from assistant.llm.port import LLMError, LLMErrorKind, LLMRequest
from assistant.memory import items
from assistant.memory.models import ConversationSummary, MemoryItem
from tests.fakes.channel import FakeChannel
from tests.fakes.deletions import RecordingDeleter
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import one_id, request_texts, summarizer
from tests.integration.business_env import (
    CONTACT,
    NOW,
    OWNER,
    Env,
    business_message,
    connect,
    deliver,
    environment,
    grant_managed,
)

pytestmark = pytest.mark.integration

CANARY = "CANARY-RETAIN-9c2e"
SENT = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


def handlers(env: Env, fake: FakeLLM, *, summaries_enabled: bool = True) -> JobHandlers:
    assert env.scheduler is not None

    class NotUsed:
        async def run(self, turn_id: int) -> Any:
            raise AssertionError("not used")

        async def apply(self, confirmation_id: int) -> Any:
            raise AssertionError("not used")

    return JobHandlers(
        session_factory=env.factory,
        clock=env.clock,
        scheduler=env.scheduler,
        owner_runtime=NotUsed(),
        alerts=OwnerAlerts(env.factory, FakeChannel(), env.clock),
        monthly_budget_usd=None,
        heartbeat_url=None,
        confirmation_applier=NotUsed(),
        summary_refresher=summarizer(env, fake),
        data_deleter=RecordingDeleter(),
        summaries_enabled=summaries_enabled,
    )


async def seed(env: Env, *, managed: bool) -> int:
    await connect(env)
    if managed:
        await grant_managed(env)
    for n, minutes in ((1, 0), (2, 5)):
        await deliver(
            env,
            business_message(
                message_id=n,
                message_text=f"{CANARY} message {n}",
                first_name=CANARY,
                date=int((SENT + timedelta(minutes=minutes)).timestamp()),
            ),
        )
    # (Control-chat turns carry their receipt time from the database clock,
    # not the FakeClock; their 90-day sweep is `test_system_tasks.py`'s.)
    return await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )


async def columns_containing(env: Env, needle: str) -> list[str]:
    """Every text/jsonb/tsvector column of the app schema holding `needle`."""
    async with env.factory() as session:
        columns = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'app' "
                    "AND data_type IN ('text', 'jsonb', 'character varying', 'json', 'tsvector')"
                )
            )
        ).all()
    hits = []
    for table, column in columns:
        async with env.factory() as session:
            found = await session.execute(
                text(
                    f'SELECT 1 FROM app."{table}" '  # noqa: S608 - catalogue identifiers
                    f'WHERE "{column}"::text ILIKE :needle LIMIT 1'
                ),
                {"needle": f"%{needle}%"},
            )
            if found.first() is not None:
                hits.append(f"{table}.{column}")
    return sorted(hits)


async def conversation(env: Env, conversation_id: int) -> Conversation:
    async with env.factory() as session:
        return (
            await session.execute(select(Conversation).where(Conversation.id == conversation_id))
        ).scalar_one()


async def refresh_jobs(env: Env) -> list[tuple[int, str, datetime | None]]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT (args->>'conversation_id')::bigint, status::text, scheduled_at "
                "FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = 'memory.refresh_summary' ORDER BY id"
            )
        )
        return [(int(r[0]), str(r[1]), r[2]) for r in rows.all()]


def summary_citing_everything(request: LLMRequest) -> dict[str, Any]:
    import re

    ids = [int(i) for i in re.findall(r"^\[(\d+)\]", request_texts(request), flags=re.MULTILINE)]
    return {
        "overview": "Synthetic rollup overview.",
        "open_questions_to_owner": [],
        "commitments": [],
        "decisions_requested": [],
        "key_facts": [{"text": "Synthetic fact", "message_ids": ids}],
        "needs_reply": False,
        "needs_reply_reason": None,
        "language": "uz",
    }


async def test_without_managed_consent_after_91_days_no_old_content_remains_and_the_gap_is_recorded(
    env: Env,
) -> None:
    conversation_id = await seed(env, managed=False)
    assert "messages.text" in await columns_containing(env, CANARY)
    fake = FakeLLM()
    env.clock.set(NOW + timedelta(days=91))
    await handlers(env, fake).retention_sweep()

    assert await columns_containing(env, CANARY) == []
    assert fake.requests == []  # nothing reached a model without consent
    assert await refresh_jobs(env) == []
    async with env.factory() as session:
        rows = (
            (
                await session.execute(
                    select(Message).where(Message.conversation_id == conversation_id)
                )
            )
            .scalars()
            .all()
        )
    assert all(m.purged_at is not None for m in rows)
    assert (await conversation(env, conversation_id)).summary_gap_before == SENT + timedelta(
        minutes=5
    )


async def test_with_managed_consent_the_rollup_precedes_the_purge(env: Env) -> None:
    conversation_id = await seed(env, managed=True)
    fake = FakeLLM()
    jobs = handlers(env, fake)

    # Day 79: outside the rollup window, nothing is scheduled.
    env.clock.set(NOW + timedelta(days=79))
    await jobs.retention_sweep()
    assert await refresh_jobs(env) == []

    # Day 81: within ten days of the ceiling, the rollup is due now.
    env.clock.set(NOW + timedelta(days=81))
    report = await jobs.retention_sweep()
    assert report.rollups_scheduled == 1
    [(job_conversation, status, scheduled_at)] = await refresh_jobs(env)
    assert (job_conversation, status) == (conversation_id, "todo")
    assert scheduled_at is None or scheduled_at <= env.clock.now()
    assert report.message_contents == 0  # nothing purged yet

    fake.parsed.append(summary_citing_everything)
    await jobs.refresh_summary(conversation_id)
    [rollup_call] = fake.requests
    assert f"{CANARY} message 1" in request_texts(rollup_call)  # read before the purge

    env.clock.set(NOW + timedelta(days=91))
    report = await jobs.retention_sweep()
    assert report.message_contents == 2
    async with env.factory() as session:
        rolling = (
            await session.execute(
                select(ConversationSummary).where(ConversationSummary.kind == "rolling")
            )
        ).scalar_one()
        texts = (
            (
                await session.execute(
                    select(Message.text).where(Message.conversation_id == conversation_id)
                )
            )
            .scalars()
            .all()
        )
    assert texts == [None, None]
    assert rolling.summary["overview"] == "Synthetic rollup overview."
    # The rollup covered everything that expired: no gap to report.
    assert (await conversation(env, conversation_id)).summary_gap_before is None


async def test_the_purge_does_not_wait_for_a_rollup_that_failed(env: Env) -> None:
    """Provider outage: the ceiling holds, and the gap is recorded."""
    conversation_id = await seed(env, managed=True)
    fake = FakeLLM()
    jobs = handlers(env, fake)
    env.clock.set(NOW + timedelta(days=81))
    await jobs.retention_sweep()
    fake.parsed.append(LLMError(LLMErrorKind.UNAVAILABLE))
    await jobs.refresh_summary(conversation_id)
    env.clock.set(NOW + timedelta(days=91))
    report = await jobs.retention_sweep()
    assert report.message_contents == 2
    assert await columns_containing(env, f"{CANARY} message") == []
    assert (await conversation(env, conversation_id)).summary_gap_before == SENT + timedelta(
        minutes=5
    )


async def test_no_rollup_without_the_refresh_flag(env: Env) -> None:
    """D61: background summaries are off by default; then there is no rollup
    and the purge records the gap."""
    conversation_id = await seed(env, managed=True)
    jobs = handlers(env, FakeLLM(), summaries_enabled=False)
    env.clock.set(NOW + timedelta(days=81))
    report = await jobs.retention_sweep()
    assert report.rollups_scheduled == 0
    assert await refresh_jobs(env) == []
    env.clock.set(NOW + timedelta(days=91))
    await jobs.retention_sweep()
    assert (await conversation(env, conversation_id)).summary_gap_before is not None


async def test_forgotten_memory_is_deleted_once_the_undo_window_passes(env: Env) -> None:
    await connect(env)
    account_id = await one_id(
        env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER
    )
    async with env.factory() as session, session.begin():
        kept = await items.save(
            session,
            account_id=account_id,
            content="SYNTH kept",
            kind="fact",
            actor="owner",
            now=env.clock.now(),
        )
        gone = await items.save(
            session,
            account_id=account_id,
            content="SYNTH gone",
            kind="fact",
            actor="owner",
            now=env.clock.now(),
        )
        await items.forget(
            session,
            account_id=account_id,
            memory_id=gone.item.memory_id,
            actor="owner",
            now=env.clock.now(),
        )
    jobs = handlers(env, FakeLLM())
    env.clock.advance(hours=23)
    await jobs.retention_sweep()
    async with env.factory() as session:
        assert len((await session.execute(select(MemoryItem))).scalars().all()) == 2
    env.clock.advance(hours=2)
    report = await jobs.retention_sweep()
    assert report.forgotten_memories == 1
    async with env.factory() as session:
        left = (await session.execute(select(MemoryItem.content))).scalars().all()
    assert left == ["SYNTH kept"]
    assert kept.item.memory_id is not None
