"""I2 / C7: contiguous triage review over the real inbox and Postgres.

Required tests, findable by name: a backlog drains oldest-first without
skipping a retained message across repeated bounded runs and restarts; the
cursor never passes an unexamined range; an edit, a deletion or a late content
gain during the model call never settles the cursor as current; a progress
deletion or sweep keeps the verdict and clears BOTH linkage fields.

Synthetic text only; FakeLLM stands in for the provider (AGENTS §11).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from assistant.agent.triage import TriageStatus
from assistant.inbox import review_progress
from assistant.inbox.models import ConversationTriage, ReviewProgress
from assistant.inbox.triage_store import BURST_LIMIT, ApplyResult
from assistant.llm.port import LLMRequest
from tests.fakes.llm import FakeLLM
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    Env,
    business_message,
    deleted_messages,
    deliver,
    environment,
)
from tests.integration.test_triage import (
    AT,
    burst_text,
    message_ids,
    seed,
    stored_triager,
    triage_requests,
    ts,
    verdict,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def tenv(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


def quiet(request: LLMRequest) -> dict[str, Any]:
    return verdict(
        needs_reply=False,
        contains_question_to_owner=False,
        escalation_category="fyi",
        urgency="low",
        reasons=[],
    )


async def flood(env: Env, count: int, *, first_id: int, minute: int, owner: bool = False) -> None:
    """`count` retained messages from the contact (or the owner), in order."""
    for offset in range(count):
        await deliver(
            env,
            business_message(
                message_id=first_id + offset,
                from_id=OWNER if owner else CONTACT,
                message_text=f"SYNTH-A bulk {first_id + offset}",
                first_name="Kamola",
                date=ts(minute + offset),
            ),
        )


async def progress_of(env: Env, conversation: int) -> ReviewProgress | None:
    async with env.factory() as session:
        return (
            await session.execute(
                select(ReviewProgress).where(ReviewProgress.conversation_id == conversation)
            )
        ).scalar_one_or_none()


async def cursor(env: Env, conversation: int) -> int | None:
    position = review_progress.position_of(await progress_of(env, conversation))
    return None if position is None else position.message_id


async def verdict_of(env: Env, conversation: int) -> ConversationTriage | None:
    async with env.factory() as session:
        return (
            await session.execute(
                select(ConversationTriage).where(ConversationTriage.conversation_id == conversation)
            )
        ).scalar_one_or_none()


def burst_ids(request: LLMRequest) -> list[int]:
    return [int(i) for i in re.findall(r"^\[msg:(\d+)\]", burst_text(request), flags=re.MULTILINE)]


# ============================================================ backlog drain
async def test_a_backlog_drains_oldest_first_without_skipping_a_retained_message(
    tenv: Env,
) -> None:
    """R3-03: 132 unreviewed messages, 60 per run, a fresh `Triager` each run
    (a restart). Each run reads the oldest unreviewed; together they read every
    retained message exactly once, in order, and the cursor never passes one
    that was not shown."""
    a, _ = await seed(tenv)
    await flood(tenv, 130, first_id=100, minute=10)
    everything_ids = await message_ids(tenv, a)
    assert len(everything_ids) == 132
    fake = FakeLLM()
    fake.parsed = [quiet, quiet, quiet]

    seen: list[int] = []
    expected_cursors = [everything_ids[59], everything_ids[119], everything_ids[131]]
    for index, expected in enumerate(expected_cursors):
        outcome = await stored_triager(tenv, fake).triage(a)
        assert outcome.status is TriageStatus.TRIAGED
        request = triage_requests(fake)[index]
        shown = burst_ids(request)
        assert shown == everything_ids[len(seen) : len(seen) + len(shown)]  # contiguous
        seen += shown
        assert outcome.through == expected
        assert await cursor(tenv, a) == expected
        stored = await verdict_of(tenv, a)
        assert stored is not None
        assert stored.covers_through_message_id == expected
        progress = await progress_of(tenv, a)
        assert progress is not None
        assert (stored.review_progress_id, stored.review_version) == (progress.id, progress.version)
        header = burst_text(request).splitlines()[0]
        if index < 2:
            assert "newer messages are not shown" in header
            assert progress.status == "pending"  # backlog remains: not current
        else:
            assert header == "New messages to triage:"
            assert progress.status == "current"
    assert seen == everything_ids
    assert len(everything_ids) > BURST_LIMIT

    again = await stored_triager(tenv, fake).triage(a)  # drained: nothing left
    assert again.status is TriageStatus.UP_TO_DATE
    assert len(triage_requests(fake)) == 3


async def test_a_backlog_nobody_could_triage_still_drains_without_a_call(tenv: Env) -> None:
    """60+ owner-only messages (no open task) in front of contact messages: the
    first runs make no model call but move the cursor over what they read, or
    the same 60 would be re-read forever and the contact's messages never be."""
    a, _ = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [quiet, quiet]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    await flood(tenv, 65, first_id=200, minute=20, owner=True)
    await flood(tenv, 3, first_id=300, minute=90)
    ids = await message_ids(tenv, a)
    owner_block = ids[2:67]

    skipped = await stored_triager(tenv, fake).triage(a)
    assert skipped.status is TriageStatus.UP_TO_DATE
    assert len(triage_requests(fake)) == 1  # no new call
    assert await cursor(tenv, a) == owner_block[59]

    last = await stored_triager(tenv, fake).triage(a)
    assert last.status is TriageStatus.TRIAGED
    assert burst_ids(triage_requests(fake)[1]) == ids[2 + 60 :]
    assert await cursor(tenv, a) == ids[-1]


# ============================================================ stale settlement
async def test_an_edit_during_the_call_never_settles_the_cursor_as_current(tenv: Env) -> None:
    a, _ = await seed(tenv)
    first, second = await message_ids(tenv, a)
    fake = FakeLLM()

    async def edit_then_answer(request: LLMRequest) -> dict[str, Any]:
        await deliver(
            tenv,
            business_message(
                message_id=12,
                message_text="SYNTH-A edited while the model ran",
                first_name="Kamola",
                date=ts(2),
                edit=True,
            ),
        )
        return quiet(request)

    fake.parsed = [edit_then_answer, quiet]
    outcome = await stored_triager(tenv, fake).triage(a)
    assert outcome.status is TriageStatus.DISCARDED
    assert outcome.discard_reason is ApplyResult.CHANGED
    assert await verdict_of(tenv, a) is None
    assert await progress_of(tenv, a) is None  # coverage did not move

    retry = await stored_triager(tenv, fake).triage(a)  # reads the edited text
    assert retry.status is TriageStatus.TRIAGED
    assert "edited while the model ran" in burst_text(triage_requests(fake)[1])
    assert await cursor(tenv, a) == second
    assert first < second


async def test_a_deletion_during_the_call_leaves_the_cursor_where_it_was(tenv: Env) -> None:
    a, _ = await seed(tenv)
    _, second = await message_ids(tenv, a)
    fake = FakeLLM()
    fake.parsed = [quiet, quiet]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    await flood(tenv, 2, first_id=400, minute=30)
    third, fourth = (await message_ids(tenv, a))[2:]

    async def delete_then_answer(request: LLMRequest) -> dict[str, Any]:
        await deliver(tenv, deleted_messages(message_ids=[400]))
        return quiet(request)

    fake.parsed = [delete_then_answer]
    outcome = await stored_triager(tenv, fake).triage(a)
    assert outcome.status is TriageStatus.DISCARDED
    assert await cursor(tenv, a) == second  # not moved to `fourth`
    assert third < fourth


async def test_a_message_that_gains_content_inside_the_batch_during_the_call_is_not_certified(
    tenv: Env,
) -> None:
    """A placeholder (no text yet) between two shown messages gains text while
    the model runs: it was never read, so the range through the newest message
    cannot be certified even though every shown hash is unchanged."""
    a, _ = await seed(tenv)
    first, second = await message_ids(tenv, a)
    async with tenv.factory() as session, session.begin():
        account = await session.scalar(
            text("SELECT account_id FROM app.conversations WHERE id = :c"), {"c": a}
        )
        await session.execute(
            text(
                "INSERT INTO app.messages (account_id, conversation_id, direction, sent_at, "
                "content_type) VALUES (:a, :c, 'contact', :t, 'text')"
            ),
            {"a": account, "c": a, "t": AT + timedelta(minutes=3)},
        )
    await flood(tenv, 1, first_id=500, minute=4)
    placeholder = (await message_ids(tenv, a))[2]
    fake = FakeLLM()

    async def gain_content_then_answer(request: LLMRequest) -> dict[str, Any]:
        async with tenv.factory() as session, session.begin():
            await session.execute(
                text("UPDATE app.messages SET text = 'SYNTH-A late text' WHERE id = :m"),
                {"m": placeholder},
            )
            await session.execute(
                text(
                    "UPDATE app.conversations SET content_revision = content_revision + 1 "
                    "WHERE id = :c"
                ),
                {"c": a},
            )
        return quiet(request)

    fake.parsed = [gain_content_then_answer, quiet]
    outcome = await stored_triager(tenv, fake).triage(a)
    assert outcome.status is TriageStatus.DISCARDED
    assert outcome.discard_reason is ApplyResult.CHANGED
    assert await progress_of(tenv, a) is None
    retry = await stored_triager(tenv, fake).triage(a)
    assert retry.status is TriageStatus.TRIAGED
    assert placeholder in burst_ids(triage_requests(fake)[1])
    assert first < second < placeholder


async def test_a_new_arrival_during_the_call_does_not_discard_the_reviewed_batch(
    tenv: Env,
) -> None:
    """The revision moves when a message arrives, but the arrival lies beyond
    the batch: the verified, unchanged batch settles, and the arrival is left
    for the next run (status pending, never `current`)."""
    a, _ = await seed(tenv)
    _, second = await message_ids(tenv, a)
    fake = FakeLLM()

    async def arrive_then_answer(request: LLMRequest) -> dict[str, Any]:
        await flood(tenv, 1, first_id=600, minute=50)
        return quiet(request)

    fake.parsed = [arrive_then_answer, quiet]
    outcome = await stored_triager(tenv, fake).triage(a)
    assert outcome.status is TriageStatus.TRIAGED
    assert await cursor(tenv, a) == second
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert progress.status == "pending"
    again = await stored_triager(tenv, fake).triage(a)
    assert again.status is TriageStatus.TRIAGED
    assert (await message_ids(tenv, a))[-1] in burst_ids(triage_requests(fake)[1])


async def test_a_settlement_that_raced_ahead_is_not_overwritten(tenv: Env) -> None:
    """Compare-and-swap on the progress version: another settlement that already
    covers this batch stands; one that changed the row without covering it
    discards ours. Either way the stale verdict is not stored."""
    a, b = await seed(tenv)
    _, second = await message_ids(tenv, a)
    fake = FakeLLM()

    async def settle_elsewhere(through: int | None, conversation: int) -> None:
        async with tenv.factory() as session, session.begin():
            account = await session.scalar(
                text("SELECT account_id FROM app.conversations WHERE id = :c"), {"c": conversation}
            )
            position = (
                None if through is None else f'{{"message_id": {through}, "content_revision": 0}}'
            )
            await session.execute(
                text(
                    "INSERT INTO app.review_progress (account_id, conversation_id, reader, "
                    "position) VALUES (:a, :c, 'triage', CAST(:p AS jsonb))"
                ),
                {"a": account, "c": conversation, "p": position},
            )

    async def ahead(request: LLMRequest) -> dict[str, Any]:
        await settle_elsewhere(second, a)
        return quiet(request)

    async def sideways(request: LLMRequest) -> dict[str, Any]:
        await settle_elsewhere(None, b)
        return quiet(request)

    fake.parsed = [ahead, sideways]
    raced = await stored_triager(tenv, fake).triage(a)
    assert raced.status is TriageStatus.RACED
    changed = await stored_triager(tenv, fake).triage(b)
    assert changed.status is TriageStatus.DISCARDED
    assert changed.discard_reason is ApplyResult.CHANGED
    assert await verdict_of(tenv, a) is None
    assert await verdict_of(tenv, b) is None


# ============================================================ gaps and failures
async def test_a_message_gone_before_review_is_recorded_as_a_gap_not_as_examined(
    tenv: Env,
) -> None:
    a, _ = await seed(tenv)
    await flood(tenv, 1, first_id=700, minute=8)
    first, second, third = await message_ids(tenv, a)
    await deliver(tenv, deleted_messages(message_ids=[12]))
    fake = FakeLLM()
    fake.parsed = [quiet]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    assert burst_ids(triage_requests(fake)[0]) == [first, third]
    progress = await progress_of(tenv, a)
    assert progress is not None
    assert progress.status == "partial"
    assert progress.pending_ranges == [
        {"first_message_id": second, "last_message_id": second, "reason": "deleted"}
    ]


# ============================================================ progress deletion
async def _triaged_chat(tenv: Env) -> int:
    a, _ = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [lambda request: verdict(reasons=["Savol bor"])]
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.TRIAGED
    stored = await verdict_of(tenv, a)
    assert stored is not None
    assert stored.review_progress_id is not None
    assert stored.review_version is not None
    return a


async def _verdict_kept_unlinked(tenv: Env, conversation: int) -> None:
    stored = await verdict_of(tenv, conversation)
    assert stored is not None  # the verdict survives its progress row
    assert stored.reasons == ["Savol bor"]
    assert (stored.review_progress_id, stored.review_version) == (None, None)
    assert await progress_of(tenv, conversation) is None


async def test_deleting_progress_keeps_the_verdict_and_clears_both_link_fields(
    tenv: Env,
) -> None:
    a = await _triaged_chat(tenv)
    async with tenv.factory() as session, session.begin():
        account = await session.scalar(
            text("SELECT account_id FROM app.conversations WHERE id = :c"), {"c": a}
        )
        assert await review_progress.delete_progress(session, account_id=account) == 1
    await _verdict_kept_unlinked(tenv, a)


async def test_the_database_clears_both_link_fields_on_any_progress_deletion(
    tenv: Env,
) -> None:
    """Backstop for paths that bypass the service: a raw DELETE (or a cascade)
    cannot leave a dangling `review_version` that breaks the paired-null rule."""
    a = await _triaged_chat(tenv)
    async with tenv.factory() as session, session.begin():
        await session.execute(text("DELETE FROM app.review_progress"))
    await _verdict_kept_unlinked(tenv, a)


async def test_the_hourly_sweep_drops_orphaned_progress_and_keeps_the_verdict(
    tenv: Env,
) -> None:
    a = await _triaged_chat(tenv)
    async with tenv.factory() as session, session.begin():
        account = await session.scalar(
            text("SELECT account_id FROM app.conversations WHERE id = :c"), {"c": a}
        )
        assert await review_progress.sweep_orphans(session, account_id=account) == 0
        await session.execute(
            text("UPDATE app.conversations SET mode = 'ignore' WHERE id = :c"), {"c": a}
        )
        assert await review_progress.sweep_orphans(session, account_id=account) == 1
    await _verdict_kept_unlinked(tenv, a)
    # The legacy verdict still bounds the next burst: nothing is re-triaged.
    async with tenv.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'observe' WHERE id = :c"), {"c": a}
        )
    fake = FakeLLM()
    assert (await stored_triager(tenv, fake).triage(a)).status is TriageStatus.UP_TO_DATE
    assert fake.requests == []


async def test_the_verdict_link_fields_are_both_set_or_both_null(tenv: Env) -> None:
    a = await _triaged_chat(tenv)
    async with tenv.factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "UPDATE app.conversation_triage SET review_version = NULL "
                    "WHERE conversation_id = :c"
                ),
                {"c": a},
            )
