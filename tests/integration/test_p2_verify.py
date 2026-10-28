"""P2 view verification on a synthetic PostgreSQL database only."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.connections import mark_stale
from assistant.accounts.models import AuditLogEntry, BusinessConnection
from assistant.actions.lifecycle import text_hash, transition
from assistant.actions.models import Action
from assistant.actions.states import ActionState, StateReason
from assistant.conversations.models import Message
from assistant.db.uow import transaction
from assistant.inbox.models import Digest
from assistant.privacy.content_columns import CONTENT_COLUMNS
from tests.integration.business_env import Env, business_message, deliver
from tests.integration.p2_factories import seed_account, seed_action, seed_conversation, seed_task
from tests.integration.test_data_deletion import OTHER_OWNER, env, hits, seed

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/acceptance/p2_verify.py"
CANARY = "p2qacontentcanary"
TRIAL_CHECKS = (
    "approved_before_claim",
    "hash_match",
    "one_send_per_action",
    "no_send_after_terminal",
    "recipient_matches",
    "no_orphan_sends",
)


@dataclass(frozen=True)
class TrialHistory:
    account_id: int
    conversation_id: int
    action_id: int
    attempt_id: str
    at: datetime


async def trial_message(
    session: AsyncSession,
    history: TrialHistory,
    *,
    telegram_id: int = 1234,
) -> Message:
    message = Message(
        account_id=history.account_id,
        conversation_id=history.conversation_id,
        action_id=history.action_id,
        telegram_message_id=telegram_id,
        direction="assistant_sent",
        observed_via="new",
        content_type="text",
        text=CANARY,
        content_hash=text_hash(CANARY),
        sent_at=history.at,
        created_at=history.at,
        updated_at=history.at,
    )
    session.add(message)
    await session.flush()
    return message


async def trial_history(
    env: Env,
    *,
    outcome: ActionState = ActionState.SUCCEEDED,
    rate_limited: bool = False,
    created_at: datetime | None = None,
    approved_at: datetime | None = None,
    telegram_id: int = 1234,
) -> TrialHistory:
    """Real transitions produce valid audit keys; corruption happens only in tests."""
    now = env.clock.now()
    async with transaction(env.factory) as uow:
        session = uow.session
        account = await seed_account(session, timezone="Asia/Tashkent")
        chat = await seed_conversation(session, account_id=account.id)
        action = await seed_action(
            session,
            account_id=account.id,
            conversation_id=chat.id,
            text=CANARY,
            created_at=created_at or now,
        )
        token = "TrialSeed1"

        async def move(
            before: ActionState,
            after: ActionState,
            *,
            values: dict[str, Any],
            reason: StateReason | None = None,
            attempt: str | None = None,
            at: datetime | None = None,
        ) -> None:
            assert await transition(
                session,
                account_id=account.id,
                action_id=action.id,
                from_state=before,
                to_state=after,
                expected_version=1,
                token=token if before is ActionState.AWAITING_APPROVAL else None,
                reason=reason,
                now=at or now,
                values=values,
                details={} if attempt is None else {"attempt_id": attempt},
            )

        await move(
            ActionState.PROPOSED,
            ActionState.AWAITING_APPROVAL,
            values={"approval_token": token},
            at=approved_at,
        )
        await move(
            ActionState.AWAITING_APPROVAL,
            ActionState.APPROVED,
            values={"approved_text_hash": text_hash(CANARY), "decided_at": approved_at or now},
            at=approved_at,
        )
        if rate_limited:
            limited_attempt = uuid4().hex
            await move(
                ActionState.APPROVED,
                ActionState.EXECUTING,
                values={"attempt_id": limited_attempt, "sent_text_hash": text_hash(CANARY)},
            )
            await move(
                ActionState.EXECUTING,
                ActionState.APPROVED,
                values={"attempt_id": None, "retry_at": now},
                attempt=limited_attempt,
            )
        attempt = uuid4().hex
        await move(
            ActionState.APPROVED,
            ActionState.EXECUTING,
            values={
                "attempt_id": attempt,
                "attempt_started_at": now,
                "sent_text_hash": text_hash(CANARY),
            },
        )
        if outcome is not ActionState.EXECUTING:
            await move(
                ActionState.EXECUTING,
                outcome,
                values={
                    "telegram_message_id": telegram_id
                    if outcome is ActionState.SUCCEEDED
                    else None,
                    "identity_pending": outcome is ActionState.SUCCEEDED and telegram_id == 0,
                    "executed_at": now,
                },
                reason=StateReason.SENT
                if outcome is ActionState.SUCCEEDED
                else StateReason.AMBIGUOUS,
                attempt=attempt,
            )
        history = TrialHistory(account.id, chat.id, action.id, attempt, now)
        if outcome is ActionState.SUCCEEDED:
            await trial_message(session, history, telegram_id=telegram_id)
        return history


async def run_trial(url: str, history: TrialHistory) -> subprocess.CompletedProcess[str]:
    return await run(
        url,
        "trial-check",
        "--since",
        (history.at - timedelta(minutes=1)).isoformat(),
        "--until",
        (history.at + timedelta(minutes=1)).isoformat(),
    )


def assert_trial(result: subprocess.CompletedProcess[str], *, passed: bool) -> None:
    assert result.returncode == (0 if passed else 1), result.stderr + result.stdout
    assert ("RESULT: PASS" if passed else "RESULT: FAIL") in result.stdout
    assert CANARY not in result.stdout + result.stderr
    for check in TRIAL_CHECKS:
        assert any(
            line.startswith((f"PASS {check}", f"FAIL {check}"))
            for line in result.stdout.splitlines()
        ), result.stdout


def assert_failed_check(result: subprocess.CompletedProcess[str], check: str) -> None:
    assert_trial(result, passed=False)
    assert f"FAIL {check}" in result.stdout


async def run(url: str, *args: str, flags: str = "") -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env={"DATABASE_URL": url, "FEATURE_FLAGS": flags, "UNRELATED_SETTING": CANARY},
        capture_output=True,
        text=True,
        check=False,
    )


async def test_trial_check_passes_a_clean_history(env: Env, migrated_url: str) -> None:
    history = await trial_history(env)
    result = await run_trial(migrated_url, history)
    assert_trial(result, passed=True)
    assert "state=succeeded count=1" in result.stdout
    assert "total_sends=1" in result.stdout
    assert all(f"FAIL {check}" not in result.stdout for check in TRIAL_CHECKS)


@pytest.mark.parametrize(
    ("damage", "rate_limited"),
    [(None, False), ("claim", False), ("all", False), ("claim", True)],
)
async def test_trial_check_requires_the_current_executing_claim(
    env: Env, migrated_url: str, damage: str | None, rate_limited: bool
) -> None:
    history = await trial_history(env, outcome=ActionState.EXECUTING, rate_limited=rate_limited)
    if damage is not None:
        async with transaction(env.factory) as uow:
            audits = list(
                await uow.session.scalars(
                    select(AuditLogEntry).where(
                        AuditLogEntry.account_id == history.account_id,
                        AuditLogEntry.subject_type == "action",
                        AuditLogEntry.subject_id == history.action_id,
                    )
                )
            )
            for audit in audits:
                if damage == "all" or (
                    audit.event == "action_claimed"
                    and audit.details.get("attempt_id") == history.attempt_id
                ):
                    await uow.session.delete(audit)
    # An in-flight send needs its current committed claim, even if an older
    # rate-limited attempt still has valid approval/claim evidence.
    result = await run_trial(migrated_url, history)
    assert_trial(result, passed=damage is None)
    if damage is not None:
        assert "FAIL one_send_per_action" in result.stdout


@pytest.mark.parametrize("damage", ["missing", "wrong_version", "later_timestamp", "later_id"])
async def test_trial_check_fails_a_send_without_approval(
    env: Env, migrated_url: str, damage: str
) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        approval = (
            await uow.session.scalars(
                select(AuditLogEntry).where(
                    AuditLogEntry.account_id == history.account_id,
                    AuditLogEntry.subject_type == "action",
                    AuditLogEntry.subject_id == history.action_id,
                    AuditLogEntry.event == "action_approved",
                )
            )
        ).one()
        if damage == "missing":
            await uow.session.delete(approval)
        elif damage == "wrong_version":
            approval.details = {**approval.details, "version": 2}
        elif damage == "later_timestamp":
            approval.at = history.at + timedelta(seconds=1)
        else:
            # Deliberately corrupt order, retaining the event emitted by transition.
            # Timestamps tie, but the replacement approval's audit id is after claim.
            replacement = AuditLogEntry(
                account_id=approval.account_id,
                at=approval.at,
                actor=approval.actor,
                event=approval.event,
                subject_type=approval.subject_type,
                subject_id=approval.subject_id,
                details=dict(approval.details),
            )
            await uow.session.delete(approval)
            await uow.session.flush()
            uow.session.add(replacement)
    assert_failed_check(await run_trial(migrated_url, history), "approved_before_claim")


async def test_trial_check_fails_an_autonomous_send_in_p2(env: Env, migrated_url: str) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        action = await uow.session.get(Action, history.action_id)
        assert action is not None
        action.origin = "delegation_auto"  # impossible P2 authorization, intentionally corrupted
    assert_failed_check(await run_trial(migrated_url, history), "approved_before_claim")


async def test_trial_check_never_uses_another_accounts_approval(
    env: Env, migrated_url: str
) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        other = await seed_account(uow.session)
        approval = (
            await uow.session.scalars(
                select(AuditLogEntry).where(
                    AuditLogEntry.account_id == history.account_id,
                    AuditLogEntry.subject_type == "action",
                    AuditLogEntry.subject_id == history.action_id,
                    AuditLogEntry.event == "action_approved",
                )
            )
        ).one()
        approval.account_id = other.id  # deliberately corrupt tenant attribution
    assert_failed_check(await run_trial(migrated_url, history), "approved_before_claim")


@pytest.mark.parametrize("damage", ["different", "missing_approval", "missing_sent"])
async def test_trial_check_fails_a_hash_mismatch(env: Env, migrated_url: str, damage: str) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        action = await uow.session.get(Action, history.action_id)
        assert action is not None
        if damage == "different":
            action.sent_text_hash = text_hash("Synthetic unapproved replacement")
        elif damage == "missing_approval":
            action.approved_text_hash = None
        else:
            action.sent_text_hash = None
    assert_failed_check(await run_trial(migrated_url, history), "hash_match")


async def test_trial_check_fails_two_message_rows_for_one_action(
    env: Env, migrated_url: str
) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        await trial_message(uow.session, history, telegram_id=1235)
    assert_failed_check(await run_trial(migrated_url, history), "one_send_per_action")


async def test_trial_check_allows_claims_after_rate_limits_but_fails_two_delivered_attempts(
    env: Env, migrated_url: str
) -> None:
    history = await trial_history(env, rate_limited=True)
    assert_trial(await run_trial(migrated_url, history), passed=True)
    async with transaction(env.factory) as uow:
        events = list(
            await uow.session.scalars(
                select(AuditLogEntry)
                .where(
                    AuditLogEntry.account_id == history.account_id,
                    AuditLogEntry.subject_id == history.action_id,
                    AuditLogEntry.subject_type == "action",
                    AuditLogEntry.event.in_(("action_claimed", "action_succeeded")),
                )
                .order_by(AuditLogEntry.at, AuditLogEntry.id)
            )
        )
        delivered_attempt = uuid4().hex
        # Intentional impossible second send: clone the real claim/outcome shape.
        for event in events:
            if event.details.get("attempt_id") != history.attempt_id:
                continue
            uow.session.add(
                AuditLogEntry(
                    account_id=event.account_id,
                    at=event.at,
                    actor=event.actor,
                    event=event.event,
                    subject_type=event.subject_type,
                    subject_id=event.subject_id,
                    details={**event.details, "attempt_id": delivered_attempt},
                )
            )
    assert_failed_check(await run_trial(migrated_url, history), "one_send_per_action")


@pytest.mark.parametrize(
    ("terminal", "before", "reason"),
    [
        (ActionState.OUTDATED, ActionState.AWAITING_APPROVAL, StateReason.NEW_CONTACT_MESSAGE),
        (ActionState.REJECTED, ActionState.AWAITING_APPROVAL, StateReason.OWNER_REJECTED),
        (ActionState.EXPIRED, ActionState.AWAITING_APPROVAL, StateReason.MAX_AGE),
        (ActionState.CANCELLED, ActionState.AWAITING_APPROVAL, StateReason.IGNORE_MODE),
        (ActionState.BLOCKED, ActionState.APPROVED, StateReason.POLICY_DENIED),
    ],
)
async def test_trial_check_fails_a_send_after_an_outdated_transition(
    env: Env,
    migrated_url: str,
    terminal: ActionState,
    before: ActionState,
    reason: StateReason,
) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        action = await uow.session.get(Action, history.action_id)
        assert action is not None
        # Deliberately impossible history, but emit the terminal event via lifecycle.
        action.state = before.value
        await uow.session.flush()
        assert await transition(
            uow.session,
            account_id=history.account_id,
            action_id=history.action_id,
            from_state=before,
            to_state=terminal,
            reason=reason,
            now=history.at - timedelta(seconds=1),
        )
        # Restore the corrupt success so a state-only checker cannot catch the fault.
        action.state = ActionState.SUCCEEDED.value
        action.state_reason = StateReason.SENT.value
    assert_failed_check(await run_trial(migrated_url, history), "no_send_after_terminal")


@pytest.mark.parametrize("foreign_account", [False, True])
async def test_trial_check_fails_a_wrong_recipient(
    env: Env, migrated_url: str, foreign_account: bool
) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        account_id = history.account_id
        if foreign_account:
            other = await seed_account(uow.session)
            account_id = other.id
        wrong = await seed_conversation(uow.session, account_id=account_id)
        sent = (
            await uow.session.scalars(select(Message).where(Message.action_id == history.action_id))
        ).one()
        # Both foreign-account and same-account/wrong-chat links must be rejected.
        sent.account_id = account_id
        sent.conversation_id = wrong.id
    assert_failed_check(await run_trial(migrated_url, history), "recipient_matches")


@pytest.mark.parametrize("damage", ["unlinked", "not_succeeded"])
async def test_trial_check_fails_an_orphan_assistant_sent_row(
    env: Env, migrated_url: str, damage: str
) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        if damage == "unlinked":
            sent = (
                await uow.session.scalars(
                    select(Message).where(Message.action_id == history.action_id)
                )
            ).one()
            sent.action_id = None
        else:
            action = await uow.session.get(Action, history.action_id)
            assert action is not None
            action.state = "delivery_uncertain"  # intentionally inconsistent with linked send
    assert_failed_check(await run_trial(migrated_url, history), "no_orphan_sends")


async def test_trial_check_lists_uncertain_actions_without_failing(
    env: Env, migrated_url: str
) -> None:
    history = await trial_history(env, outcome=ActionState.DELIVERY_UNCERTAIN)
    result = await run_trial(migrated_url, history)
    assert_trial(result, passed=True)
    assert "state=delivery_uncertain count=1" in result.stdout
    assert f"action={history.action_id}" in result.stdout
    assert "+05:00" in result.stdout
    assert "total_sends=0" in result.stdout


async def test_trial_check_counts_uncertain_then_late_ok_as_one_delivered_attempt(
    env: Env, migrated_url: str
) -> None:
    history = await trial_history(env, outcome=ActionState.DELIVERY_UNCERTAIN)
    async with transaction(env.factory) as uow:
        assert await transition(
            uow.session,
            account_id=history.account_id,
            action_id=history.action_id,
            from_state=ActionState.DELIVERY_UNCERTAIN,
            to_state=ActionState.SUCCEEDED,
            reason=StateReason.LATE_RESULT,
            now=history.at,
            values={"telegram_message_id": 1234},
            details={"attempt_id": history.attempt_id},
        )
        await trial_message(uow.session, history)
    assert_trial(await run_trial(migrated_url, history), passed=True)


async def test_trial_check_accepts_a_zero_id_send_linked_to_one_message(
    env: Env, migrated_url: str
) -> None:
    history = await trial_history(env, telegram_id=0)
    assert_trial(await run_trial(migrated_url, history), passed=True)


async def test_trial_check_includes_old_actions_with_recent_audit_and_earlier_approval(
    env: Env, migrated_url: str
) -> None:
    old = env.clock.now() - timedelta(days=2)
    history = await trial_history(env, created_at=old, approved_at=old)
    result = await run_trial(migrated_url, history)
    assert_trial(result, passed=True)
    assert f"action={history.action_id}" in result.stdout
    assert "state=succeeded count=1" in result.stdout


@pytest.mark.parametrize("missing", ["claim", "outcome", "outcome_attempt"])
async def test_trial_check_never_passes_a_success_with_missing_audit_facts(
    env: Env, migrated_url: str, missing: str
) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        event = "action_claimed" if missing == "claim" else "action_succeeded"
        query = select(AuditLogEntry).where(
            AuditLogEntry.account_id == history.account_id,
            AuditLogEntry.subject_type == "action",
            AuditLogEntry.subject_id == history.action_id,
            AuditLogEntry.event == event,
        )
        audit = (await uow.session.scalars(query)).one()
        if missing == "outcome_attempt":
            audit.details = {
                key: value for key, value in audit.details.items() if key != "attempt_id"
            }
        else:
            await uow.session.delete(audit)
    assert_trial(await run_trial(migrated_url, history), passed=False)


async def test_trial_check_reports_empty_history_and_respects_until(
    env: Env, migrated_url: str
) -> None:
    now = env.clock.now()
    empty = await run(
        migrated_url,
        "trial-check",
        "--since",
        (now - timedelta(minutes=1)).isoformat(),
        "--until",
        (now + timedelta(minutes=1)).isoformat(),
    )
    assert_trial(empty, passed=True)
    assert "total_sends=0" in empty.stdout
    assert "state=succeeded" not in empty.stdout
    first = await trial_history(env)
    env.clock.advance(minutes=10)
    await trial_history(env)
    scoped = await run_trial(migrated_url, first)
    assert_trial(scoped, passed=True)
    assert "state=succeeded count=1" in scoped.stdout
    assert "total_sends=1" in scoped.stdout


async def test_release_prints_flag_names_and_no_other_setting(env: Env, migrated_url: str) -> None:
    async with transaction(env.factory) as uow:
        account = await seed_account(uow.session)
        chat = await seed_conversation(uow.session, account_id=account.id)
        await seed_action(uow.session, account_id=account.id, conversation_id=chat.id)
    result = await run(
        migrated_url, "release", flags=" tool.get_activity ,actions.send, tool.get_activity "
    )
    assert result.returncode == 0, result.stderr
    assert "0014" in result.stdout
    assert "actions.send" in result.stdout
    assert "tool.get_activity" in result.stdout
    assert CANARY not in result.stdout + result.stderr
    assert "state=proposed count=1" in result.stdout


async def test_an_unknown_action_has_an_explicit_empty_timeline(
    env: Env, migrated_url: str
) -> None:
    unknown = 2**63 - 1
    result = await run(migrated_url, "action", str(unknown))
    assert result.returncode == 0, result.stderr
    assert f"action={unknown} events=0" in result.stdout


async def test_task_without_a_conversation_has_a_structural_missing_marker(
    env: Env, migrated_url: str
) -> None:
    async with transaction(env.factory) as uow:
        account = await seed_account(uow.session)
        task = await seed_task(uow.session, account_id=account.id, conversation_id=None)
        task_id, account_id = task.id, account.id
    result = await run(migrated_url, "tasks")
    assert result.returncode == 0, result.stderr
    [line] = [row for row in result.stdout.splitlines() if row.startswith(f"task={task_id} ")]
    assert f"account={account_id}" in line
    assert "conversation=-" in line
    assert "None" not in line


async def test_actions_view_lists_connection_staleness_without_content(
    env: Env, migrated_url: str
) -> None:
    history = await trial_history(env)
    async with transaction(env.factory) as uow:
        connection = BusinessConnection(
            account_id=history.account_id,
            connection_id=CANARY,
            is_enabled=True,
            rights=["can_reply"],
            capability_tier="can_reply",
            user_chat_id=7_000_000_001,
            status="active",
            connected_at=history.at,
            updated_at=history.at,
        )
        uow.session.add(connection)
        await uow.session.flush()
        assert await mark_stale(uow.session, history.account_id, history.at, connection_id=CANARY)
        connection_id = connection.id
    result = await run(
        migrated_url, "actions", "--since", (history.at - timedelta(minutes=1)).isoformat()
    )
    assert result.returncode == 0, result.stderr
    assert "event=connection_marked_stale" in result.stdout
    assert f"connection={connection_id}" in result.stdout
    assert f"account={history.account_id}" in result.stdout
    assert history.at.isoformat() in result.stdout
    assert CANARY not in result.stdout + result.stderr


async def test_every_mode_is_content_free(env: Env, migrated_url: str) -> None:
    seeded = await seed(env)
    async with transaction(env.factory) as uow:
        chat = await seed_conversation(uow.session, account_id=seeded.account_id)
        action = await seed_action(
            uow.session, account_id=seeded.account_id, conversation_id=chat.id, text=CANARY
        )
        action_id = action.id
        attempt_id = "ac72541741ae4efaba274b71f4f57ef6"
        token = "QAseed1234"
        for before, after, values in (
            (ActionState.PROPOSED, ActionState.AWAITING_APPROVAL, {"approval_token": token}),
            (
                ActionState.AWAITING_APPROVAL,
                ActionState.APPROVED,
                {"approved_text_hash": text_hash(CANARY)},
            ),
            (ActionState.APPROVED, ActionState.EXECUTING, {"attempt_id": attempt_id}),
            (
                ActionState.EXECUTING,
                ActionState.SUCCEEDED,
                {"sent_text_hash": text_hash(CANARY), "telegram_message_id": 1234},
            ),
        ):
            assert await transition(
                uow.session,
                account_id=seeded.account_id,
                action_id=action_id,
                from_state=before,
                to_state=after,
                reason=StateReason.SENT if after is ActionState.SUCCEEDED else None,
                now=env.clock.now(),
                token=token if after is ActionState.APPROVED else None,
                values=values,
                details={"note": CANARY},
            )
        uow.session.add(
            Digest(
                account_id=seeded.account_id,
                period_start=env.clock.now() - timedelta(days=1),
                period_end=env.clock.now(),
                status="sent",
                sent_at=env.clock.now(),
            )
        )
        await uow.session.flush()
        for table, column in sorted(CONTENT_COLUMNS):
            if column == "search_tsv":
                continue  # generated from search_key
            json_column = column in {"media", "args", "payload", "summary", "reasons", "spec"}
            value = (
                "jsonb_build_array(CAST(:canary AS text))"
                if column == "reasons"
                else "jsonb_build_object('canary', CAST(:canary AS text))"
                if json_column
                else ":canary"
            )
            await uow.session.execute(
                text(f'UPDATE app."{table}" SET "{column}" = {value}'),  # noqa: S608 - registry identifiers
                {"canary": CANARY},
            )
    assert await hits(env, CANARY) == {f"{t}.{c}" for t, c in CONTENT_COLUMNS}
    for args in (
        ("release",),
        ("trial-check",),
        ("actions",),
        ("action", str(action_id)),
        ("script", "P2-4", "--expect", "1-3"),
        ("notifications",),
        ("tasks",),
        ("digests",),
    ):
        result = await run(migrated_url, *args)
        # The canary seed deliberately lacks a linked send and valid outcome
        # attempt metadata: trial-check must diagnose it without exposing content.
        assert result.returncode == (1 if args[0] == "trial-check" else 0), result.stderr
        assert CANARY not in result.stdout + result.stderr
        expected_row = {
            "release": "state=",
            "trial-check": "RESULT: FAIL",
            "actions": "action=",
            "action": f"action={action_id} event=action_claimed",
            "script": "found=",
            "notifications": "notification=",
            "tasks": "task=",
            "digests": "digest=",
        }[args[0]]
        assert expected_row in result.stdout
        if args[0] == "actions":
            [sent] = [
                line
                for line in result.stdout.splitlines()
                if line.startswith(f"action={action_id} ")
            ]
            assert "hash_match=yes telegram_id=yes attempts=1" in sent
            assert f"account={seeded.account_id}" in sent
        if args[0] == "action":
            assert "attempt_id=ac725417-41ae-4efa-ba27-4b71f4f57ef6" in result.stdout
            assert "event=action_approved" in result.stdout
            assert "event=action_succeeded" in result.stdout
        if args[0] == "digests":
            assert "status=sent" in result.stdout
            assert f"sent_utc={env.clock.now().isoformat()}" in result.stdout
        row_prefix = {
            "action": "action=",
            "notifications": "notification=",
            "tasks": "task=",
            "digests": "digest=",
        }.get(args[0])
        if row_prefix is not None:
            rows = [line for line in result.stdout.splitlines() if line.startswith(row_prefix)]
            assert rows
            assert all(" account=" in line for line in rows)


async def test_script_mode_reports_found_missing_and_unexpected_numbers_only(
    env: Env, migrated_url: str
) -> None:
    from tests.integration.business_env import connect

    await connect(env)
    for number in (401, 403, 409):
        await deliver(
            env, business_message(message_id=number, message_text=f"P2-{number} {CANARY}")
        )
    result = await run(
        migrated_url, "script", "P2-4", "--expect", "401-403", "--direction", "contact"
    )
    assert result.returncode == 0, result.stderr
    assert "found=[401, 403, 409]" in result.stdout
    assert "missing=[402]" in result.stdout
    assert "unexpected=[409]" in result.stdout
    assert CANARY not in result.stdout + result.stderr
    empty = await run(migrated_url, "script", "P2-5", "--expect", "501")
    assert empty.returncode == 0
    assert "found=[] missing=[501] unexpected=[]" in empty.stdout


async def test_modes_run_read_only(migrated_url: str) -> None:
    verifier = importlib.import_module("tools.acceptance.p2_verify")

    def attempt_write() -> None:
        with verifier.open_database(migrated_url) as conn:
            assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
            assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
            assert conn.execute("SHOW statement_timeout").fetchone()[0] == "10s"
            assert conn.execute("SHOW lock_timeout").fetchone()[0] == "1s"
            assert conn.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("CREATE TABLE app.qa_should_never_exist (id bigint)")

    await asyncio.to_thread(attempt_write)


async def test_verification_reads_one_snapshot_while_a_writer_commits(
    env: Env, migrated_url: str
) -> None:
    history = await trial_history(env)
    verifier = importlib.import_module("tools.acceptance.p2_verify")

    def observe_snapshot() -> None:
        query = "SELECT card_state FROM app.actions WHERE account_id = %s AND id = %s"
        params = (history.account_id, history.action_id)
        writer_url = migrated_url.replace("postgresql+psycopg://", "postgresql://", 1)
        with verifier.open_database(migrated_url) as reader:
            assert reader.execute(query, params).fetchone() == (None,)
            with psycopg.connect(writer_url) as writer:
                writer.execute(
                    "UPDATE app.actions SET card_state = 'succeeded' "
                    "WHERE account_id = %s AND id = %s",
                    params,
                )
            # A separate committed write cannot split a verifier's audit snapshot.
            assert reader.execute(query, params).fetchone() == (None,)
        with psycopg.connect(writer_url) as observer:
            assert observer.execute(query, params).fetchone() == ("succeeded",)

    await asyncio.to_thread(observe_snapshot)


@pytest.mark.parametrize(
    "args",
    [
        ("action", "0"),
        ("action", "1;SELECT"),
        ("actions", "--since", "2026-10-27"),
        ("script", "P2-abc", "--expect", "1"),
        ("script", "P2-4", "--expect", "3-1"),
        ("script", "P2-4", "--expect", "1-99999999"),
        ("script", "P2-4", "--expect", "1,,2"),
    ],
)
def test_bad_arguments_exit_2_and_never_reach_sql(
    monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    verifier = importlib.import_module("tools.acceptance.p2_verify")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("bad arguments reached the database")

    monkeypatch.setattr(verifier.psycopg, "connect", forbidden)
    with pytest.raises(SystemExit) as error:
        verifier.main(list(args))
    assert error.value.code == 2


__all__ = ["OTHER_OWNER", "env"]
