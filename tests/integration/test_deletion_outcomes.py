"""S03.5 fix round 3: what a button deletion job tells the owner, whatever happens.

The press said "Deleting now… you get a receipt" (M03 D83). The job then
decides its outcome once — deleted, or not — and says it (invariant 11):

- a failure anywhere, the first read included, is reported (verifier item A);
- an error raised by the commit itself is "could not confirm", never
  "nothing was deleted" (item C);
- a re-run of a job that already reported a failure (a stalled worker's
  retry) deletes nothing behind that message (item D);
- the failure message offers a way to ask again that works for its scope —
  a fresh Delete card for the managed data after `/privacy revoke` (item B);
- two runs of one job at the same moment delete and report once (own
  inspection: the check and the deletion were two transactions).

Synthetic text only.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from assistant.accounts.models import AuditLogEntry
from assistant.privacy import deletion
from assistant.privacy.requests import NOT_FOUND_TEXT, RequestedDeletions
from assistant.telegram.control import panels
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import (
    OWNER,
    Env,
    callback,
    deliver,
    environment,
    grant_managed,
    press,
    private_message,
    run_deletion_jobs,
)
from tests.integration.test_conversation_modes import open_panel, tap
from tests.integration.test_deletion_cards import deletions, queued_job_args, seed, stored

pytestmark = pytest.mark.integration


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def tap_delete_chat(env: Env) -> None:
    await open_panel(env)
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "chat")


def runner(env: Env, channel: FakeChannel) -> RequestedDeletions:
    return RequestedDeletions(env.factory, env.clock, channel)


async def end_failed(env: Env) -> None:
    """What the worker does with a job that raised: it ends `failed`."""
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE procrastinate.procrastinate_jobs SET status = 'failed' "
                "WHERE task_name = 'privacy.delete_data' AND status = 'todo'"
            )
        )


async def failure_markers(env: Env) -> list[dict[str, Any]]:
    async with env.factory() as session:
        rows = await session.execute(
            select(AuditLogEntry.details).where(AuditLogEntry.event == "data_deletion_failed")
        )
        return [dict(details) for (details,) in rows.all()]


async def wait_for_a_lock_wait(env: Env, *, deadline: float = 20.0) -> None:
    """Poll until another session of this database waits on a lock (no
    assertion depends on elapsed time; the deadline only bounds a failure)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while True:
        async with env.factory() as session:
            waiting = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND pid <> pg_backend_pid()"
                    )
                )
            ).scalar_one()
        if waiting:
            return
        if loop.time() > end:
            raise AssertionError("the second run never waited for the first")
        await asyncio.sleep(0.02)


# ------------------------------------------------------------ A: every failure is told
async def test_a_deletion_whose_first_read_fails_still_tells_the_owner(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item A: the job's first read (the account, its audit row) failing on a
    database hiccup must not end the job silently after "you get a receipt"."""
    await seed(env)
    await tap_delete_chat(env)
    job_args = await queued_job_args(env)
    real = deletion.find_deletion
    calls = 0

    async def first_read_fails(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("synthetic", {}, Exception("connection lost"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(deletion, "find_deletion", first_read_fails)
    receipts = FakeChannel()
    with pytest.raises(OperationalError):
        await runner(env, receipts).run(**job_args)
    [report] = receipts.sends
    assert "nothing was deleted" in report
    assert await stored(env, "CHATK") > 0
    assert await deletions(env) == 0


# ------------------------------------------------------------ C: a commit that errors
async def test_a_deletion_whose_commit_errors_is_reported_as_unconfirmed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item C: every statement ran and the commit itself raised. Whether it
    went through is not ours to claim either way: "could not confirm",
    never "nothing was deleted"."""
    await seed(env)
    await tap_delete_chat(env)
    job_args = await queued_job_args(env)
    real = deletion.delete_conversation_data

    async def then_the_commit_is_lost(session: Any, **kwargs: Any) -> Any:
        report = await real(session, **kwargs)
        rollback = session.rollback

        async def lost() -> None:
            await rollback()
            raise OperationalError("synthetic", {}, Exception("connection lost in commit"))

        session.commit = lost
        return report

    monkeypatch.setattr(deletion, "delete_conversation_data", then_the_commit_is_lost)
    receipts = FakeChannel()
    with pytest.raises(OperationalError):
        await runner(env, receipts).run(**job_args)
    [report] = receipts.sends
    assert "either all of it was deleted or none of it" in report
    assert "nothing was deleted" not in report
    assert "Deleted:" not in report


# ------------------------------------------------------------ M03 follow-up 5
async def test_a_lookup_bug_inside_the_deletion_is_a_failure_not_not_found(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `NotInAccountError` means the chat or contact is gone (M03
    follow-up 5, closed by fix round C2). A `KeyError` raised after most of
    the deletion ran used to read as "no longer there" and commit the part
    that ran; now it rolls back and is reported as a failure."""
    await seed(env)
    await tap_delete_chat(env)
    job_args = await queued_job_args(env)

    async def lookup_bug(*args: Any, **kwargs: Any) -> int:
        raise KeyError("synthetic")

    monkeypatch.setattr(deletion, "_clear_names", lookup_bug)  # the deletion's last step
    receipts = FakeChannel()
    with pytest.raises(KeyError):
        await runner(env, receipts).run(**job_args)
    [report] = receipts.sends
    assert report != NOT_FOUND_TEXT
    assert "nothing was deleted" in report
    assert await stored(env, "CHATK") > 0
    assert await deletions(env) == 0
    [marker] = await failure_markers(env)
    assert marker["outcome"] == "failed"


# ------------------------------------------------------------ D: no deletion after "failed"
async def test_a_rerun_after_a_reported_failure_deletes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item D: the worker died after telling the owner "nothing was deleted"
    and before the job was marked failed; `system.retry_stalled_jobs` runs it
    again. It must not delete behind that message (nor say anything new)."""
    await seed(env)
    await tap_delete_chat(env)
    job_args = await queued_job_args(env)

    async def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(deletion, "_clear_names", broken)
    receipts = FakeChannel()
    with pytest.raises(RuntimeError):
        await runner(env, receipts).run(**job_args)
    [failed] = receipts.sends
    assert "nothing was deleted" in failed
    monkeypatch.undo()

    await runner(env, receipts).run(**job_args)  # the stalled worker's retry
    assert len(receipts.sends) == 1
    assert await stored(env, "CHATK") > 0
    assert await deletions(env) == 0
    # The decision is on record, content-free, keyed by the card.
    [marker] = await failure_markers(env)
    assert marker["card_id"] == job_args["card_id"]
    assert marker["outcome"] == "failed"
    assert all(isinstance(value, int | str) for value in marker.values())


# ------------------------------------------------------------ B: asking again works
async def test_a_failed_chat_deletion_points_to_the_chats_panel_only(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item B: "/privacy delete" deletes everything, not the chat the owner
    asked about, and "asking me" needs flags that are off by default: the
    message names the way that works for this scope."""
    await seed(env)
    await tap_delete_chat(env)

    async def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(deletion, "_clear_names", broken)
    receipts = FakeChannel()
    with pytest.raises(RuntimeError):
        await runner(env, receipts).run(**await queued_job_args(env))
    [report] = receipts.sends
    assert "nothing was deleted" in report
    assert "panel" in report
    assert "/privacy delete" not in report
    assert "asking me" not in report


async def test_a_failed_managed_deletion_offers_a_fresh_delete_card(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item B: after `/privacy revoke` the revocation card is used up, a
    second revoke has nothing to revoke and offers no card, and the agent
    may not run: the failure message itself carries a fresh Delete button.
    Tapping it queues the managed-data deletion again, without asking again
    how to continue (that was answered on the first card)."""
    await seed(env)
    await grant_managed(env)
    await deliver(env, private_message(OWNER, "/privacy revoke"))
    await press(env, "managed_delete", "delete")
    asked_how = sum("How should I continue?" in t for t in env.sender.texts)

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(deletion, "delete_managed_data", broken)
    receipts = FakeChannel()
    with pytest.raises(RuntimeError):
        await runner(env, receipts).run(**await queued_job_args(env))
    monkeypatch.undo()
    await end_failed(env)
    [report] = receipts.sends
    assert "nothing was deleted" in report
    [buttons] = receipts.buttons
    assert [button.label for button in buttons] == ["Delete"]
    assert buttons[0].callback_data.startswith("managed_delete:")
    assert await stored(env, "CHATK") > 0

    await deliver(env, callback(buttons[0].callback_data))
    retried = FakeChannel()
    assert await run_deletion_jobs(env, retried) == 1
    assert await stored(env, "CHATK") == 0
    [receipt] = retried.sends
    assert receipt.startswith("🗑 Deleted:")
    assert sum("How should I continue?" in t for t in env.sender.texts) == asked_how


# ------------------------------------------------------------ own inspection: one outcome
async def test_two_runs_of_one_deletion_job_at_once_delete_and_report_once(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled-worker retry can start while the first run still works (its
    worker only missed heartbeats). The audit-row check and the deletion were
    two transactions, so both runs deleted and both sent a receipt; now the
    check is made under the account lock the deletion holds until it commits."""
    await seed(env)
    await tap_delete_chat(env)
    job_args = await queued_job_args(env)
    receipts = FakeChannel()
    real = deletion._clear_names
    second: list[asyncio.Task[str]] = []

    async def while_the_second_run_starts(*args: Any, **kwargs: Any) -> int:
        if not second:
            second.append(asyncio.create_task(runner(env, receipts).run(**job_args)))
            await wait_for_a_lock_wait(env)
        return await real(*args, **kwargs)

    monkeypatch.setattr(deletion, "_clear_names", while_the_second_run_starts)
    await runner(env, receipts).run(**job_args)
    await second[0]
    assert await deletions(env) == 1
    assert len(receipts.sends) == 1
    assert receipts.sends[0].startswith("🗑 Deleted:")
