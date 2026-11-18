"""I2 / C7 failure handling and coverage honesty, over the real inbox and Postgres.

Transient provider errors never degrade a chat; repeating errors count against
one batch (paid calls included) and escalate (halve, isolate a single message,
skip it as a recorded `unexaminable` gap, degrade when a context keeps failing);
legacy and first-review coverage is never reported as proven; deletion paths
(scoped, managed, everything, cascades, the hourly sweep) leave no dangling
progress link. Synthetic text only; FakeLLM stands in for the provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.agent.triage import TriageStatus
from assistant.db.uow import transaction
from assistant.inbox import review_progress
from assistant.inbox.models import ConversationTriage, ReviewProgress
from assistant.inbox.triage_store import ApplyResult
from assistant.llm.port import LLMError, LLMErrorKind, LLMRequest, StopReason
from assistant.privacy.p2_deletion import purge_p2
from assistant.privacy.retention import sweep_all
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import one_id
from tests.integration.business_env import (
    CONTACT,
    Env,
    business_message,
    connect,
    deliver,
    environment,
    grant_managed,
)
from tests.integration.test_triage import (
    burst_text,
    message_ids,
    seed,
    stored_triager,
    ts,
    verdict,
)
from tests.integration.test_triage_coverage import (
    burst_ids,
    cursor,
    flood,
    progress_of,
    quiet,
    verdict_of,
)

pytestmark = pytest.mark.integration

POISON = "SYNTH-POISON"


@pytest.fixture
async def tenv(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def account_of(env: Env, conversation: int) -> int:
    return await one_id(
        env, "SELECT account_id FROM app.conversations WHERE id = :c", c=conversation
    )


async def post(env: Env, message_id: int, body: str, minute: int) -> None:
    await deliver(
        env,
        business_message(
            message_id=message_id, message_text=body, first_name="Kamola", date=ts(minute)
        ),
    )


def invalid(request: LLMRequest) -> LLMError:
    return LLMError(LLMErrorKind.INVALID_OUTPUT)


def poison_check(request: LLMRequest) -> dict[str, Any] | LLMError:
    """Fails exactly when the poison message is in the burst."""
    return invalid(request) if POISON in burst_text(request) else quiet(request)


# ============================================================ M1: transient errors
async def test_a_transient_outage_never_degrades_the_chat_and_triage_resumes(
    tenv: Env,
) -> None:
    a, _ = await seed(tenv)
    _, second = await message_ids(tenv, a)
    fake = FakeLLM()
    fake.parsed = [LLMError(LLMErrorKind.UNAVAILABLE)] * 2 + [LLMError(LLMErrorKind.RATE_LIMITED)]
    for _ in range(3):
        outcome = await stored_triager(tenv, fake).triage(a)
        assert outcome.status is TriageStatus.FAILED
        tenv.clock.advance(minutes=6)  # past the short retry time each time
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert (progress.status, progress.attempts) == ("pending", 0)
    assert progress.position is None  # nothing was certified
    assert progress.retry_at is not None
    assert progress.retry_at <= tenv.clock.now()  # a few minutes, never a day

    fake.parsed = [LLMError(LLMErrorKind.UNAVAILABLE)]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.FAILED
    # Inside the short retry window nothing is attempted (no paid hot loop) ...
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.BACKED_OFF
    # ... and a few minutes later triage resumes and settles normally.
    tenv.clock.advance(minutes=6)
    fake.parsed = [quiet]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert (progress.status, progress.retry_at) == ("current", None)
    assert await cursor(tenv, a) == second


# ============================================================ S2: attempts per batch
async def test_attempts_count_against_one_batch_and_restart_when_the_batch_changes(
    tenv: Env,
) -> None:
    a, _ = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [invalid, invalid]
    for _ in range(2):
        assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.FAILED
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert progress.attempts == 2
    await flood(tenv, 1, first_id=800, minute=20)  # the same cursor, a longer batch
    fake.parsed = [invalid]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.FAILED
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert progress.attempts == 1  # a different batch starts again
    assert progress.position is None


async def test_the_truncation_retry_counts_as_an_attempt(tenv: Env) -> None:
    """One run that used its truncation retry paid for two calls: two attempts
    at that batch, so a second such run exhausts it and halves the batch."""
    a, _ = await seed(tenv)
    first, _second = await message_ids(tenv, a)
    fake = FakeLLM()
    fake.parsed = [StopReason.MAX_TOKENS] * 4
    first_run = await stored_triager(tenv, fake).triage(a)
    assert (first_run.status, first_run.calls) == (TriageStatus.FAILED, 2)
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert progress.attempts == 2
    await stored_triager(tenv, fake).triage(a)
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert (progress.attempts, progress.batch_limit) == (0, 1)  # halved: 2 -> 1
    fake.parsed = [quiet]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    assert burst_ids(fake.requests[-1]) == [first]


# ============================================================ S1: poison message
async def test_a_poison_message_is_isolated_and_skipped_as_a_recorded_gap(tenv: Env) -> None:
    a, _ = await seed(tenv)
    await post(tenv, 300, f"{POISON} text", 20)
    await post(tenv, 301, "SYNTH-A after", 21)
    first, second, poison, after = await message_ids(tenv, a)
    fake = FakeLLM()
    fake.parsed = [poison_check] * 40

    statuses: list[TriageStatus] = []
    for _ in range(20):
        outcome = await stored_triager(tenv, fake).triage(a)
        statuses.append(outcome.status)
        if outcome.status is TriageStatus.UP_TO_DATE:
            break
    assert statuses[-1] is TriageStatus.UP_TO_DATE
    assert len([r for r in fake.requests if r.purpose == "triage"]) <= 14  # bounded cost
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert await cursor(tenv, a) == after  # the rest of the chat was reviewed
    skipped = [r for r in review_progress.ranges_of(progress) if r.reason == "unexaminable"]
    assert [(r.first_message_id, r.last_message_id) for r in skipped] == [(poison, poison)]
    assert progress.status == "partial"  # never `current` over a skipped message
    reviewed = {i for request in fake.requests for i in burst_ids(request)}
    assert {first, second, after} <= reviewed
    stored = await verdict_of(tenv, a)
    assert stored is not None
    assert stored.covers_through_message_id == after
    assert (progress.skip_streak, progress.attempts) == (0, 0)


async def test_a_context_that_fails_every_message_degrades_instead_of_skipping_the_chat(
    tenv: Env,
) -> None:
    a, _ = await seed(tenv)
    await flood(tenv, 4, first_id=900, minute=20)
    ids = await message_ids(tenv, a)
    fake = FakeLLM()
    fake.parsed = [invalid] * 60
    for _ in range(40):
        outcome = await stored_triager(tenv, fake).triage(a)
        if outcome.status is TriageStatus.BACKED_OFF:
            break
    assert outcome.status is TriageStatus.BACKED_OFF
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert progress.status == "degraded"
    assert progress.retry_at is not None
    ranges = review_progress.ranges_of(progress)
    skipped = {
        i
        for r in ranges
        if r.reason == "unexaminable"
        for i in ids
        if r.first_message_id <= i <= r.last_message_id
    }
    assert skipped == set(ids[:3])  # three skips, then the review stops
    assert {r.reason for r in ranges} == {"unexaminable", "invalid_output"}
    assert await cursor(tenv, a) == ids[2]
    paid = len(fake.requests)
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.BACKED_OFF
    assert len(fake.requests) == paid


# ============================================================ S3: unproven coverage
async def test_a_legacy_verdict_without_progress_settles_with_unproven_coverage(
    tenv: Env,
) -> None:
    a, _ = await seed(tenv)
    first, second = await message_ids(tenv, a)
    async with tenv.factory() as session, session.begin():
        session.add(
            ConversationTriage(
                account_id=await account_of(tenv, a),
                conversation_id=a,
                covers_through_message_id=second,
                needs_reply=False,
                contains_question=False,
                requests_decision=False,
                urgency="low",
                escalation_category="none",
                reasons=[],
                prompt_version="triage.v1",
                model="gpt-6-luna",
            )
        )
    await flood(tenv, 1, first_id=950, minute=30)
    fake = FakeLLM()
    fake.parsed = [quiet]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    assert burst_ids(fake.requests[0]) == [(await message_ids(tenv, a))[-1]]  # not re-read
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert [r.model_dump() for r in review_progress.ranges_of(progress)] == [
        {"first_message_id": first, "last_message_id": second, "reason": "unproven"}
    ]
    assert progress.status == "partial"  # never `current` on the legacy claim


async def test_a_first_review_with_a_backlog_records_the_pre_window_history(
    tenv: Env,
) -> None:
    await connect(tenv, date=ts(-5 * 1440))
    await grant_managed(tenv)
    await post(tenv, 1000, "SYNTH-A long ago", -3 * 1440)
    for offset in range(70):
        await post(tenv, 1001 + offset, f"SYNTH-A n{offset}", 10 + offset)
    a = await one_id(
        tenv, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    old, *window = await message_ids(tenv, a)
    fake = FakeLLM()
    fake.parsed = [quiet, quiet]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    assert burst_ids(fake.requests[0]) == window[:60]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    assert burst_ids(fake.requests[1]) == window[60:]
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert [r.model_dump() for r in review_progress.ranges_of(progress)] == [
        {"first_message_id": old, "last_message_id": old, "reason": "unproven"}
    ]
    assert progress.status == "partial"


# ============================================================ m2: verdict ahead of cursor
async def test_a_verdict_ahead_of_the_cursor_does_not_make_every_run_lose_a_race(
    tenv: Env,
) -> None:
    a, _ = await seed(tenv)
    first, second = await message_ids(tenv, a)
    fake = FakeLLM()
    fake.parsed = [quiet, quiet]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    # A rollback to an older executable left the verdict ahead of the position.
    async with tenv.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE app.review_progress SET position = "
                "jsonb_build_object('message_id', CAST(:m AS bigint), 'next_char', 0, "
                "'content_revision', 0) WHERE conversation_id = :c"
            ),
            {"m": first, "c": a},
        )
    await flood(tenv, 1, first_id=960, minute=40)
    third = (await message_ids(tenv, a))[-1]
    outcome = await stored_triager(tenv, fake).triage(a)
    assert outcome.status is TriageStatus.TRIAGED  # not RACED
    assert burst_ids(fake.requests[-1]) == [third]  # second is not re-read
    assert await cursor(tenv, a) == third
    assert second < third


# ============================================================ S4: expiry, deletion paths
async def test_a_message_that_expires_during_the_call_discards_the_verdict(tenv: Env) -> None:
    a, _ = await seed(tenv)
    first, _second = await message_ids(tenv, a)
    fake = FakeLLM()

    async def expire_then_answer(request: LLMRequest) -> dict[str, Any]:
        async with tenv.factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE app.messages SET text = NULL, content_hash = NULL, "
                    "purged_at = now() WHERE id = :m"
                ),
                {"m": first},
            )
        return quiet(request)

    fake.parsed = [expire_then_answer]
    outcome = await stored_triager(tenv, fake).triage(a)
    assert outcome.status is TriageStatus.DISCARDED
    assert outcome.discard_reason is ApplyResult.EXPIRED
    assert await progress_of(tenv, a) is None
    assert await verdict_of(tenv, a) is None


async def _populated(tenv: Env) -> tuple[int, int, int]:
    """Two chats, each triaged (a verdict linked to its progress row)."""
    a, b = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [lambda request: verdict(reasons=["Savol bor"])] * 2
    for chat in (a, b):
        assert (await stored_triager(tenv, fake).triage(chat)).status is TriageStatus.TRIAGED
    for chat in (a, b):
        stored = await verdict_of(tenv, chat)
        assert stored is not None
        assert stored.review_progress_id is not None
    return await account_of(tenv, a), a, b


async def _rows(tenv: Env) -> tuple[set[int], set[int]]:
    async with tenv.factory() as session:
        progress = set(
            (await session.execute(select(ReviewProgress.conversation_id))).scalars().all()
        )
        verdicts = set(
            (await session.execute(select(ConversationTriage.conversation_id))).scalars().all()
        )
    return progress, verdicts


@pytest.mark.parametrize("scope", ["scoped", "managed", "everything"])
async def test_privacy_deletion_removes_progress_with_the_verdicts(tenv: Env, scope: Any) -> None:
    account, a, b = await _populated(tenv)
    async with transaction(tenv.factory) as uow:
        await purge_p2(
            uow.session,
            account_id=account,
            conversation_ids=[a] if scope == "scoped" else None,
            contact_ids=[],
            scope=scope,
            now=tenv.clock.now(),
        )
    progress, verdicts = await _rows(tenv)
    expected = {b} if scope == "scoped" else set()
    assert progress == expected
    assert verdicts == expected


async def test_conversation_and_account_cascades_leave_no_progress(tenv: Env) -> None:
    """The BEFORE DELETE trigger runs inside the combined cascade (progress and
    verdict deleted by the same statement) without error or leftovers."""
    account, a, b = await _populated(tenv)
    async with tenv.factory() as session, session.begin():
        await session.execute(text("DELETE FROM app.conversations WHERE id = :c"), {"c": a})
    assert await _rows(tenv) == ({b}, {b})
    async with tenv.factory() as session, session.begin():
        # Raw updates reference messages without a cascade; physical account
        # deletion clears them first (as the privacy deletion does).
        await session.execute(
            text("DELETE FROM app.inbound_updates WHERE account_id = :a"), {"a": account}
        )
        await session.execute(text("DELETE FROM app.accounts WHERE id = :a"), {"a": account})
    assert await _rows(tenv) == (set(), set())


async def test_the_real_hourly_sweep_drops_orphaned_progress_and_keeps_the_verdict(
    tenv: Env,
) -> None:
    _, a, b = await _populated(tenv)
    async with tenv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'ignore' WHERE id = :c"), {"c": a}
        )
    await sweep_all(tenv.factory, tenv.clock)
    progress, verdicts = await _rows(tenv)
    assert (progress, verdicts) == ({b}, {a, b})
    stored = await verdict_of(tenv, a)
    assert stored is not None
    assert (stored.review_progress_id, stored.review_version) == (None, None)
    assert stored.reasons == ["Savol bor"]
