"""S03.2 verification review (2026-10-25): items A-E and the hardening
items, each pinned by a test that failed before its fix.

A. Every disconnected interval overlapping a catch-up gets a caveat (from
   `business_connection_events`, by event time).
B. The store's locks, not only its re-check: a store started while a
   deletion holds its locks waits, then finds the source changed.
C. A deletion invalidating the rolling summary during an attention check is
   a change (retry, then the notice), not a lost race.
D. The discard reason is carried and the notice worded per reason.
E. `summarize_period` checks Ignore mode and bot chats before the call.
Also: the period digest comes from the rows read; the cap note does not
say "older"; retention sweep and managed-data deletion take locks in the
§9.4 order (no deadlock with ingestion).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.agent.summarize import SummaryStatus
from assistant.agent.tools.common import summary_view
from assistant.conversations import service
from assistant.conversations.models import Message
from assistant.llm.port import StopReason
from assistant.memory import summaries as summaries_module
from assistant.memory.summaries import StoreResult, store_rolling
from assistant.privacy.deletion import delete_managed_data
from assistant.privacy.retention import sweep_message_content
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import summarizer
from tests.integration.business_env import (
    CONTACT,
    NOW,
    Env,
    business_message,
    deliver,
    environment,
)
from tests.integration.test_summaries import (
    TEXT_A,
    TEXT_A2,
    account_id,
    echo,
    everything,
    seed,
    summary_requests,
    summary_rows,
    ts,
)
from tests.integration.test_summaries_review import (
    _attention,
    catch_up,
    delete,
    during,
    stored_texts,
    with_texts,
)

pytestmark = pytest.mark.integration

DAY = timedelta(days=1)
OCT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
async def senv(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url, summaries=True) as built:
        yield built


# ============================================================ A. off intervals
async def connection_history(env: Env, *events: tuple[str, datetime, bool]) -> None:
    """Replace the account's connection history with `(change, at, enabled)`."""
    owner = await account_id(env)
    async with env.factory() as session, session.begin():
        connection = (
            await session.execute(
                text("SELECT connection_id FROM app.business_connections WHERE account_id = :a"),
                {"a": owner},
            )
        ).scalar_one()
        await session.execute(
            text("DELETE FROM app.business_connection_events WHERE account_id = :a"), {"a": owner}
        )
        for change, at, enabled in events:
            await session.execute(
                text(
                    "INSERT INTO app.business_connection_events (account_id, connection_id, "
                    "payload_digest, rights, is_enabled, change, source, observed_at) VALUES "
                    "(:a, :c, 'synthetic', '[]'::jsonb, :e, :ch, 'update', :t)"
                ),
                {"a": owner, "c": connection, "e": enabled, "ch": change, "t": at},
            )
        await session.execute(
            text("UPDATE app.business_connections SET connected_at = :t WHERE account_id = :a"),
            {"a": owner, "t": events[0][1]},
        )


async def test_a_disconnected_interval_inside_the_period_gets_a_caveat(senv: Env) -> None:
    """Connected 1 Oct, removed 5 Oct, reconnected 12 Oct: a catch-up since
    3 Oct must not read 5-12 Oct as complete."""
    await seed(senv)
    await connection_history(
        senv,
        ("connected", OCT, True),
        ("disabled", OCT + 4 * DAY, False),
        ("reconnected", OCT + 11 * DAY, True),
    )
    fake = FakeLLM()
    fake.parsed = [echo, echo]

    result = await catch_up(senv, fake, since="2026-10-03 00:00")

    lines = result.data["coverage_caveats"]
    off = [line for line in lines if line.startswith("The business connection was off")]
    assert off == [
        "The business connection was off from 2026-10-05 09:00 to 2026-10-12 09:00: "
        "messages sent in between were not observed and are missing."
    ]
    assert not any(line.startswith("Coverage began") for line in lines)


async def test_a_disconnected_interval_outside_the_period_gives_no_caveat(senv: Env) -> None:
    await seed(senv)
    await connection_history(
        senv,
        ("connected", OCT, True),
        ("disabled", OCT + 4 * DAY, False),
        ("reconnected", OCT + 11 * DAY, True),
    )
    fake = FakeLLM()
    fake.parsed = [echo, echo]

    result = await catch_up(senv, fake, since="2026-10-13 00:00")

    assert not any("was off" in line for line in result.data["coverage_caveats"])


async def test_a_connection_still_disabled_is_reported_from_the_event_time(senv: Env) -> None:
    """The open interval is the inactive-connection caveat, dated by the
    disabling event — not by `updated_at`, which a rights change also moves."""
    await seed(senv)
    await connection_history(senv, ("connected", OCT, True), ("disabled", OCT + 14 * DAY, False))
    async with senv.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE app.business_connections SET status = 'disabled', is_enabled = false, "
                "updated_at = :t"
            ),
            {"t": NOW},
        )
    fake = FakeLLM()
    fake.parsed = [echo, echo]

    result = await catch_up(senv, fake, since="2026-10-10 00:00")

    lines = result.data["coverage_caveats"]
    assert any(
        line.startswith("The business connection is not active (disabled) since 2026-10-15 09:00")
        for line in lines
    )
    assert not any("was off from" in line for line in lines)


# ============================================================ B. lock order
async def wait_for_lock_waiter(env: Env, *, deadline: float = 15.0) -> None:
    """Until some backend waits on a lock (a condition, not a timer)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while True:
        async with env.factory() as session:
            waiting = (
                await session.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
                )
            ).scalar_one()
        if waiting:
            return
        if loop.time() > end:
            raise AssertionError("no backend started waiting on a lock")
        await asyncio.sleep(0.05)


async def source_of(env: Env, conversation: int) -> dict[int, str | None]:
    async with env.factory() as session:
        rows = await session.execute(
            select(Message.id, Message.content_hash).where(Message.conversation_id == conversation)
        )
        return {int(i): h for i, h in rows.all()}


async def store_task(env: Env, conversation: int, source: dict[int, str | None]) -> StoreResult:
    owner = await account_id(env)
    async with env.factory() as session, session.begin():
        return await store_rolling(
            session,
            account_id=owner,
            conversation_id=conversation,
            based_on=None,
            source=source,
            summary={"overview": f"{TEXT_A} {TEXT_A2}"},
            covers_through=max(source),
            added_messages=len(source),
            prompt_version="summarize.v1",
            model="gpt-6-luna",
            now=NOW,
        )


async def test_a_store_waits_for_a_deletion_holding_the_conversation_lock(senv: Env) -> None:
    a, _ = await seed(senv)
    source = await source_of(senv, a)
    owner = await account_id(senv)
    deletion: AsyncSession = senv.factory()
    await deletion.begin()
    try:
        # A deletion as `record_deletion` does it: conversation row first.
        await deletion.execute(
            text("SELECT id FROM app.conversations WHERE id = :c FOR UPDATE"), {"c": a}
        )
        await deletion.execute(
            text(
                "UPDATE app.messages SET text = NULL, content_hash = NULL, search_key = NULL, "
                "deleted_at = now() WHERE conversation_id = :c AND telegram_message_id = 12"
            ),
            {"c": a},
        )
        store = asyncio.create_task(store_task(senv, a, source))
        await wait_for_lock_waiter(senv)
        assert not store.done()  # blocked behind the deletion, not racing it
        await deletion.commit()
    finally:
        await deletion.close()

    assert await store is StoreResult.CHANGED
    assert await summary_rows(senv) == []
    assert owner


async def test_a_store_waits_for_managed_data_deletion(senv: Env) -> None:
    a, _ = await seed(senv)
    source = await source_of(senv, a)
    owner = await account_id(senv)
    deletion: AsyncSession = senv.factory()
    await deletion.begin()
    try:
        await delete_managed_data(deletion, account_id=owner, clock=senv.clock)
        store = asyncio.create_task(store_task(senv, a, source))
        await wait_for_lock_waiter(senv)
        assert not store.done()
        await deletion.commit()
    finally:
        await deletion.close()

    assert await store is StoreResult.CHANGED
    assert await summary_rows(senv) == []
    assert "SYNTH-A" not in await stored_texts(senv)


# ============================================================ C. invalidated rolling
async def test_an_attention_check_whose_rolling_summary_was_invalidated_is_a_change(
    senv: Env,
) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [with_texts]
    await summarizer(senv, fake).refresh(a)  # rolling covers 11 and 12
    senv.clock.advance(minutes=5)
    await deliver(senv, business_message(message_id=13, message_text="SYNTH-A uch", date=ts(9)))

    def per_chat(request: Any) -> Any:
        if "SYNTH-B" in everything(request):
            return echo(request)
        return hooks.pop(0)(request)

    # Both calls see a covered message deleted (it drops the rolling summary).
    hooks: list[Any] = [during(delete(senv, 11)), during(delete(senv, 12))]
    fake.parsed = [per_chat, per_chat, per_chat]

    result = await _attention(senv, fake)

    by_id = {e["conversation_id"]: e for e in result.data["conversations"]}
    assert by_id[a]["check"] == "changed_during_summary"
    assert len(result.data["notices"]) == 1
    assert "deleted or edited" in result.data["notices"][0]
    assert len([r for r in summary_requests(fake) if "SYNTH-A" in everything(r)]) == 3


# ============================================================ D. reasons
async def expire(env: Env, telegram_message_id: int) -> Callable[[], Awaitable[None]]:
    async def action() -> None:
        async with env.factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE app.messages SET text = NULL, media = NULL, search_key = NULL, "
                    "content_hash = NULL, purged_at = now() WHERE telegram_message_id = :m "
                    "AND conversation_id IN (SELECT id FROM app.conversations "
                    "WHERE business_chat_id = :c)"
                ),
                {"m": telegram_message_id, "c": CONTACT},
            )

    return action


def ignore(env: Env) -> Callable[[], Awaitable[None]]:
    async def action() -> None:
        async with env.factory() as session, session.begin():
            await session.execute(
                text("UPDATE app.conversations SET mode = 'ignore' WHERE business_chat_id = :c"),
                {"c": CONTACT},
            )

    return action


def bot_chat(env: Env) -> Callable[[], Awaitable[None]]:
    async def action() -> None:
        owner = await account_id(env)
        async with env.factory() as session, session.begin():
            await service.mark_bot_chat(
                session, account_id=owner, business_chat_id=CONTACT, clock=env.clock
            )

    return action


@pytest.mark.parametrize(
    ("kind", "reason", "words"),
    [
        ("expired", "expired_during_summary", "expire under the retention setting"),
        ("ignore", "set_to_ignore_during_summary", "was set to Ignore mode"),
        ("bot", "bot_chat_during_summary", "is a chat with a bot"),
    ],
)
async def test_the_notice_names_why_a_chat_was_left_out(
    senv: Env, kind: str, reason: str, words: str
) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    if kind == "expired":
        fake.parsed = [during(await expire(senv, 12)), during(await expire(senv, 11))]
    elif kind == "ignore":
        fake.parsed = [during(ignore(senv))]
    else:
        fake.parsed = [during(bot_chat(senv))]

    result = await catch_up(senv, fake, conversation_id=a)

    assert [(n["conversation_id"], n["reason"]) for n in result.data["not_summarized"]] == [
        (a, reason)
    ]
    [notice] = result.data["notices"]
    assert words in notice
    assert "deleted or edited" not in notice
    dumped = json.dumps(result.data)
    assert TEXT_A not in dumped
    assert TEXT_A2 not in dumped
    assert await summary_rows(senv) == []


# ============================================================ E. mode before the call
async def test_a_period_summary_of_an_ignored_or_bot_chat_makes_no_call(senv: Env) -> None:
    a, _ = await seed(senv)
    async with senv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'ignore' WHERE id = :c"), {"c": a}
        )
    fake = FakeLLM()

    outcome = await summarizer(senv, fake).summarize_period(
        account_id=await account_id(senv),
        conversation_id=a,
        start=NOW - timedelta(hours=4),
        end=NOW,
        open_ended=True,
        parent_run_id=None,
    )

    assert outcome.status is SummaryStatus.SKIPPED
    assert fake.requests == []


# ============================================================ also
async def test_a_small_period_takes_its_digest_from_the_rows_it_read(
    senv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One statement for both: no window between the digest and the texts."""
    a, _ = await seed(senv)

    async def separate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a separate fingerprint statement for a small range")

    monkeypatch.setattr(summaries_module, "_period_fingerprint", separate)
    owner = await account_id(senv)
    async with senv.factory() as session:
        source = await summaries_module.period_source(
            session, owner, a, start=NOW - timedelta(hours=4), end=NOW, limit=300
        )
    assert source.total == 2
    assert source.digest is not None


def test_the_cap_note_does_not_claim_the_left_out_items_are_older() -> None:
    view = summary_view({"items_omitted": 3}, contact_name=None, locale="en", authors={})
    assert "older" not in view["note"]
    assert view["items_omitted"] == 3


async def test_the_retention_sweep_does_not_deadlock_with_ingestion(senv: Env) -> None:
    """§9.4: conversation before messages. The sweep used to lock message
    rows first and the conversation second, the reverse of ingestion."""
    a, _ = await seed(senv)
    senv.clock.advance(days=91)  # everything is past retention
    ingestion: AsyncSession = senv.factory()
    await ingestion.begin()
    try:
        await ingestion.execute(
            text("SELECT id FROM app.conversations WHERE id = :c FOR UPDATE"), {"c": a}
        )
        sweep = asyncio.create_task(sweep_message_content(senv.factory, senv.clock))
        await wait_for_lock_waiter(senv)
        # Ingestion now touches a message row of the same chat (an edit).
        await ingestion.execute(
            text(
                "UPDATE app.messages SET edited_at = now() "
                "WHERE conversation_id = :c AND telegram_message_id = 11"
            ),
            {"c": a},
        )
        await ingestion.commit()
    finally:
        await ingestion.close()

    assert await sweep >= 2


async def test_managed_data_deletion_does_not_deadlock_with_ingestion(senv: Env) -> None:
    """Ingestion holds the conversation and then inserts a message (a key
    share on the account through the foreign key); managed-data deletion
    must not hold the account FOR UPDATE while it waits for that
    conversation."""
    a, _ = await seed(senv)
    owner = await account_id(senv)
    ingestion: AsyncSession = senv.factory()
    await ingestion.begin()

    async def wipe() -> None:
        async with senv.factory() as session, session.begin():
            await delete_managed_data(session, account_id=owner, clock=senv.clock)

    try:
        await ingestion.execute(
            text("SELECT id FROM app.conversations WHERE id = :c FOR UPDATE"), {"c": a}
        )
        deletion = asyncio.create_task(wipe())
        await wait_for_lock_waiter(senv)
        await ingestion.execute(
            text(
                "INSERT INTO app.messages (account_id, conversation_id, telegram_message_id, "
                "direction, sent_at, content_type, text) VALUES "
                "(:a, :c, 99, 'contact', now(), 'text', 'SYNTH-A late')"
            ),
            {"a": owner, "c": a},
        )
        await ingestion.commit()
    finally:
        await ingestion.close()

    await deletion
    assert "SYNTH-A" not in await stored_texts(senv)
    async with senv.factory() as session:
        left = (
            await session.execute(
                text("SELECT count(*) FROM app.messages WHERE account_id = :a"), {"a": owner}
            )
        ).scalar_one()
    assert left == 0


# ============================================================ call count
async def test_a_refresh_truncated_twice_counts_both_calls(senv: Env) -> None:
    """MAX_TOKENS, a retry with a larger budget, MAX_TOKENS again: two paid
    calls, and the outcome must say two (budget alerts read it)."""
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [StopReason.MAX_TOKENS, StopReason.MAX_TOKENS]

    outcome = await summarizer(senv, fake).refresh(a)

    assert outcome.status is SummaryStatus.FAILED
    assert len(summary_requests(fake)) == 2
    assert outcome.calls == 2


async def test_a_period_summary_truncated_twice_counts_both_calls(senv: Env) -> None:
    a, _ = await seed(senv)
    fake = FakeLLM()
    fake.parsed = [StopReason.MAX_TOKENS, StopReason.MAX_TOKENS]

    outcome = await summarizer(senv, fake).summarize_period(
        account_id=await account_id(senv),
        conversation_id=a,
        start=NOW - timedelta(hours=4),
        end=NOW,
        open_ended=True,
        parent_run_id=None,
    )

    assert outcome.status is SummaryStatus.FAILED
    assert len(summary_requests(fake)) == 2
    assert outcome.calls == 2
