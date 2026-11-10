"""I20 regression: a rewound database cannot undo privacy decisions."""

from __future__ import annotations

import argparse
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from assistant.accounts import preferences, service
from assistant.accounts.models import Account, Consent
from assistant.conversations.events import ObservedDeletion
from assistant.conversations.models import Conversation, Message
from assistant.conversations.service import record_deletion
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.db.uow import transaction
from assistant.inbox.models import NotificationSource
from assistant.memory.models import ConversationSummary
from assistant.privacy import journal, recovery_cli, retention
from assistant.privacy.deletion import (
    DeletionReport,
    delete_conversation_data,
    delete_everything,
    delete_managed_data,
)
from assistant.privacy.journal_models import PrivacyDecision, PrivacyJournalHead
from assistant.privacy.post_restore import post_restore
from tests.integration.business_env import Env, business_message, connect, deliver, grant_managed
from tests.integration.p2_factories import (
    seed_account,
    seed_conversation,
    seed_notification,
    seed_triage,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("decision", ["delete", "purge", "purge_then_delete"])
async def test_replay_distinguishes_message_expiry_from_explicit_deletion(
    env: Env, decision: str
) -> None:
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message())
    async with transaction(env.factory) as uow:
        account = (await uow.session.scalars(select(Account))).one()
        account_id = account.id
        message = (await uow.session.scalars(select(Message))).one()
        conversation = await uow.session.get(Conversation, message.conversation_id)
        assert conversation is not None
        assert message.telegram_message_id is not None
        snapshot = {
            column.key: getattr(message, column.key)
            for column in Message.__table__.columns
            if not column.primary_key and column.computed is None and column.identity is None
        }
        taken_at = env.clock.now()
        anchor = (await journal.anchors(uow.session))[0]
        summary = ConversationSummary(
            account_id=account_id,
            conversation_id=conversation.id,
            kind="rolling",
            covers_through_message_id=message.id,
            message_count=1,
            summary={"overview": "Synthetic information covered by the rolling summary"},
            created_at=taken_at,
            updated_at=taken_at,
        )
        uow.session.add(summary)
        await uow.session.flush()
        summary_snapshot = {
            column.key: getattr(summary, column.key)
            for column in ConversationSummary.__table__.columns
            if not column.primary_key
        }
        business_chat_id = conversation.business_chat_id
        telegram_message_id = message.telegram_message_id
    if decision != "delete":
        env.clock.advance(days=91)
        assert await retention.sweep_message_content(env.factory, env.clock) == 1
        async with env.factory() as session:
            assert await session.scalar(select(ConversationSummary.id)) is not None
    if decision != "purge":
        async with transaction(env.factory) as uow:
            await record_deletion(
                uow.session,
                account_id=account_id,
                event=ObservedDeletion(business_chat_id, (telegram_message_id,)),
                clock=env.clock,
            )
            if decision == "delete":
                assert await uow.session.scalar(select(ConversationSummary.id)) is None
    if decision == "delete":
        env.clock.advance(days=91)
    # Restore both backed-up rows without assigning identity primary keys.
    async with transaction(env.factory) as uow:
        segment = await journal.export_segment(uow.session, checked_at=env.clock.now())
        assert segment.records[-1].scope == "message"
        expected_operations = {
            "delete": ["delete"],
            "purge": ["purge"],
            "purge_then_delete": ["purge", "delete"],
        }
        assert [record.operation for record in segment.records] == expected_operations[decision]
        await uow.session.execute(
            delete(ConversationSummary).where(ConversationSummary.account_id == account_id)
        )
        await uow.session.execute(delete(Message).where(Message.account_id == account_id))
        restored_message = Message(**snapshot)
        uow.session.add(restored_message)
        await uow.session.flush()
        summary_snapshot["covers_through_message_id"] = restored_message.id
        uow.session.add(ConversationSummary(**summary_snapshot))
        await uow.session.execute(delete(PrivacyDecision))
        await uow.session.execute(
            update(PrivacyJournalHead)
            .where(PrivacyJournalHead.account_recovery_id == anchor.account_recovery_id)
            .values(sequence=anchor.sequence, digest=anchor.digest)
        )
        uow.session.add(SystemState(key=KEY_RESTORE_PENDING, value={}))
    await post_restore(env.factory, env.clock, backup_taken_at=taken_at)
    async with env.factory() as session:
        restored = (await session.scalars(select(Message))).one()
        assert restored.text is None
        assert restored.media is None
        assert restored.purged_at is not None
        assert await session.scalar(select(ConversationSummary.id)) is not None
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is not None
    await journal.reconcile(env.factory, env.clock, segment=segment)
    await journal.reconcile(env.factory, env.clock, segment=segment)
    async with env.factory() as session:
        restored = (await session.scalars(select(Message))).one()
        assert restored.text is None
        assert restored.media is None
        assert restored.purged_at is not None
        conversation = await session.get(Conversation, restored.conversation_id)
        assert conversation is not None
        if decision == "purge":
            summary = (await session.scalars(select(ConversationSummary))).one()
            assert summary.summary == summary_snapshot["summary"]
            assert restored.deleted_at is None
            assert conversation.summary_gap_before is None
        else:
            assert await session.scalar(select(ConversationSummary.id)) is None
            assert restored.deleted_at is not None
            assert conversation.summary_gap_before == restored.sent_at
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is None


async def test_expired_notification_snapshot_stays_deleted_after_replay(env: Env) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        account = (await uow.session.scalars(select(Account))).one()
        account_id = account.id
        chat = await seed_conversation(uow.session, account_id=account_id)
        await seed_triage(
            uow.session,
            account_id=account_id,
            conversation_id=chat.id,
            updated_at=env.clock.now(),
        )
        notification = await seed_notification(
            uow.session, account_id=account_id, conversation_id=chat.id
        )
        untouched = await seed_notification(
            uow.session, account_id=account_id, conversation_id=chat.id
        )
        untouched_id = untouched.id
        source = await uow.session.get(NotificationSource, notification.id)
        assert source is not None
        notification_id = notification.id
        recovery_id = source.recovery_id
        snapshot = {
            column.key: getattr(source, column.key)
            for column in NotificationSource.__table__.columns
            if not column.primary_key
        }
        anchor = (await journal.anchors(uow.session))[0]
        await uow.session.execute(
            text("SELECT set_config('app.privacy_decided_at', :at, true)"),
            {"at": env.clock.now().isoformat()},
        )
        await uow.session.execute(
            delete(NotificationSource).where(
                NotificationSource.account_id == account_id,
                NotificationSource.notification_id == notification_id,
            )
        )
        segment = await journal.export_segment(uow.session, checked_at=env.clock.now())
        assert segment.records[-1].scope == "notification_source"
        assert segment.records[-1].subject_recovery_id == recovery_id
    async with transaction(env.factory) as uow:
        uow.session.add(NotificationSource(notification_id=notification_id, **snapshot))
        await uow.session.execute(delete(PrivacyDecision))
        await uow.session.execute(
            update(PrivacyJournalHead)
            .where(PrivacyJournalHead.account_recovery_id == anchor.account_recovery_id)
            .values(sequence=anchor.sequence, digest=anchor.digest)
        )
        uow.session.add(SystemState(key=KEY_RESTORE_PENDING, value={}))
    await post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    await journal.reconcile(env.factory, env.clock, segment=segment)
    await journal.reconcile(env.factory, env.clock, segment=segment)
    async with env.factory() as session:
        assert await session.get(NotificationSource, notification_id) is None
        assert await session.get(NotificationSource, untouched_id) is not None
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is None


@pytest.mark.parametrize(
    "scope", ["conversation", "conversation_row", "managed", "everything", "account", "purge"]
)
async def test_notification_snapshots_follow_privacy_deletion(env: Env, scope: str) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        account = (await uow.session.scalars(select(Account))).one()
        account_id = account.id
        chat = await seed_conversation(uow.session, account_id=account_id)
        await seed_triage(
            uow.session,
            account_id=account_id,
            conversation_id=chat.id,
            updated_at=env.clock.now(),
        )
        await seed_notification(uow.session, account_id=account_id, conversation_id=chat.id)
        assert await uow.session.scalar(select(NotificationSource.notification_id)) is not None
        if scope == "conversation":
            await delete_conversation_data(
                uow.session,
                account_id=account_id,
                conversation_id=chat.id,
                actor="owner",
                clock=env.clock,
            )
        elif scope == "conversation_row":
            await uow.session.execute(
                delete(Conversation).where(
                    Conversation.account_id == account_id, Conversation.id == chat.id
                )
            )
        elif scope == "managed":
            await delete_managed_data(uow.session, account_id=account_id, clock=env.clock)
        elif scope == "everything":
            await delete_everything(
                uow.session, account_id=account_id, actor="owner", clock=env.clock
            )
        elif scope == "account":
            await uow.session.execute(delete(Account).where(Account.id == account_id))
    if scope == "purge":
        await post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
        async with env.factory() as session:
            marker = await session.get(SystemState, KEY_RESTORE_PENDING)
            assert marker is not None
            restore_id = UUID(marker.value["restore_id"])
        await journal.complete_purge(env.factory, env.clock, restore_id=restore_id)
    async with env.factory() as session:
        assert await session.scalar(select(NotificationSource.notification_id)) is None


async def test_backup_revoke_delete_restore_replay_is_idempotent(env: Env) -> None:
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message())
    async with transaction(env.factory) as uow:
        account = (await uow.session.scalars(select(Account))).one()
        account_id = account.id
        message = (await uow.session.scalars(select(Message))).one()
        snapshot = {
            column.key: getattr(message, column.key)
            for column in Message.__table__.columns
            if not column.primary_key and column.computed is None and column.identity is None
        }
        anchor = await journal.anchors(uow.session)
        taken_at = env.clock.now()
        await service.revoke_consent(
            uow.session, account, kind=service.CONSENT_AI_MANAGED, clock=env.clock
        )
        await delete_managed_data(uow.session, account_id=account_id, clock=env.clock)
        segment = await journal.export_segment(uow.session, checked_at=env.clock.now())
        other = await seed_account(uow.session)
        other_chat = await seed_conversation(uow.session, account_id=other.id)
        other_message = Message(
            account_id=other.id,
            conversation_id=other_chat.id,
            telegram_message_id=7,
            direction="contact",
            observed_via="new",
            content_type="text",
            text="Synthetic other-account text",
            sent_at=env.clock.now(),
            created_at=env.clock.now(),
            updated_at=env.clock.now(),
        )
        uow.session.add(other_message)
        await uow.session.flush()
        other_message_uuid = other_message.recovery_id
        # Include the untouched account in both snapshot anchors and source
        # head; replay must not apply the first account's deletion to it.
        other_head = await journal.anchors(uow.session)
        other_head = tuple(head for head in other_head if head.account_id == other.id)
        anchor = (*anchor, *other_head)
        segment = segment.model_copy(update={"heads": (*segment.heads, *other_head)})
    # Snapshot restoration uses copied synthetic fields, never identity PKs.
    async with transaction(env.factory) as uow:
        uow.session.add(Message(**snapshot))
        await uow.session.execute(update(Consent).values(revoked_at=None))
        for head in anchor:
            await uow.session.execute(
                delete(PrivacyDecision).where(
                    PrivacyDecision.account_recovery_id == head.account_recovery_id,
                    PrivacyDecision.sequence > head.sequence,
                )
            )
            await uow.session.execute(
                update(PrivacyJournalHead)
                .where(PrivacyJournalHead.account_recovery_id == head.account_recovery_id)
                .values(sequence=head.sequence, digest=head.digest)
            )
        uow.session.add(SystemState(key=KEY_RESTORE_PENDING, value={}))
    await post_restore(env.factory, env.clock, backup_taken_at=taken_at)
    async with env.factory() as session:
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is not None
    await journal.reconcile(env.factory, env.clock, segment=segment)
    await journal.reconcile(env.factory, env.clock, segment=segment)
    async with env.factory() as session:
        assert (
            await session.scalar(select(Message.id).where(Message.account_id == account_id)) is None
        )
        other_row = await session.scalar(
            select(Message).where(Message.recovery_id == other_message_uuid)
        )
        assert other_row is not None
        assert other_row.text == "Synthetic other-account text"
        assert not (
            await service.active_consent(session, account_id, kind=service.CONSENT_AI_MANAGED)
        ).granted
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is None


@pytest.mark.parametrize(
    "failure", ["missing", "gap", "checksum", "tail", "subsecond_tail", "foreign"]
)
async def test_invalid_journal_keeps_restore_quarantined(env: Env, failure: str) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        account = (await uow.session.scalars(select(Account))).one()
        await service.revoke_consent(
            uow.session, account, kind=service.CONSENT_AI_CONTROL, clock=env.clock
        )
        segment = await journal.export_segment(uow.session, checked_at=env.clock.now())
    await post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    data = segment.model_dump(mode="json")
    if failure == "missing":
        data["heads"] = []
    elif failure == "gap":
        data["records"] = []
    elif failure == "checksum":
        data["records"][0]["digest"] = "1" * 64
    elif failure == "foreign":
        data["records"][0]["account_recovery_id"] = "00000000-0000-0000-0000-000000000099"
    elif failure == "subsecond_tail":
        data["checked_at"] = (env.clock.now() - timedelta(microseconds=1)).isoformat()
    else:
        data["checked_at"] = (env.clock.now() - timedelta(days=1)).isoformat()
    with pytest.raises(journal.JournalRecoveryError):
        await journal.reconcile(
            env.factory, env.clock, segment=journal.JournalSegment.model_validate(data)
        )
    async with env.factory() as session:
        marker = await session.get(SystemState, KEY_RESTORE_PENDING)
        assert marker is not None
        if failure == "subsecond_tail":
            assert marker.value["privacy_reason"] == "journal_stale_tail"


async def test_account_delete_retains_only_opaque_tombstones(env: Env) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        account = (await uow.session.scalars(select(Account))).one()
        account_id = account.id
        await uow.session.execute(delete(Account).where(Account.id == account_id))
    async with env.factory() as session:
        rows = (
            await session.scalars(select(PrivacyDecision).order_by(PrivacyDecision.sequence))
        ).all()
        assert rows
        assert rows[-1].scope == "account"
        assert rows[-1].operation == "delete"
        assert rows[-1].account_id == account_id
        assert rows[-1].subject_recovery_id is None


def test_journal_columns_never_contain_content() -> None:
    assert set(PrivacyDecision.__table__.columns.keys()) == {
        "id",
        "account_id",
        "sequence",
        "decision_id",
        "account_recovery_id",
        "scope",
        "subject_recovery_id",
        "operation",
        "processor_id",
        "decided_at",
        "previous_digest",
        "digest",
    }


async def test_local_outbox_rolls_back_with_the_withdrawal(env: Env) -> None:
    await connect(env)
    async with env.factory() as session:
        before = await journal.anchors(session)

    async def rollback_withdrawal() -> None:
        async with transaction(env.factory) as uow:
            account = (await uow.session.scalars(select(Account))).one()
            await service.revoke_consent(
                uow.session, account, kind=service.CONSENT_AI_CONTROL, clock=env.clock
            )
            assert await uow.session.scalar(select(PrivacyDecision.id)) is not None
            raise RuntimeError("synthetic transaction failure")

    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        await rollback_withdrawal()
    async with env.factory() as session:
        assert await journal.anchors(session) == before
        assert await session.scalar(select(PrivacyDecision.id)) is None
        state = await service.active_consent(
            session, before[0].account_id, kind=service.CONSENT_AI_CONTROL
        )
        assert state.granted


async def test_manual_confirmation_is_restore_and_account_scoped(env: Env) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        other = await seed_account(uow.session)
        other_id = other.id
    await post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    async with env.factory() as session:
        marker = await session.get(SystemState, KEY_RESTORE_PENDING)
        assert marker is not None
        restore_id = UUID(marker.value["restore_id"])
    with pytest.raises(journal.JournalRecoveryError, match="owner_confirmation_required"):
        await journal.complete_manual(
            env.factory,
            env.clock,
            restore_id=restore_id,
            account_ids=frozenset({other_id}),
            owner_confirmed=False,
        )
    with pytest.raises(journal.JournalRecoveryError, match="restore_account_mismatch"):
        await journal.complete_manual(
            env.factory,
            env.clock,
            restore_id=restore_id,
            account_ids=frozenset({other_id}),
            owner_confirmed=True,
        )
    async with env.factory() as session:
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is not None


async def test_purge_sweep_failure_keeps_gate_and_retry_finishes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant.privacy import retention

    await connect(env)
    await post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    async with env.factory() as session:
        marker = await session.get(SystemState, KEY_RESTORE_PENDING)
        assert marker is not None
        restore_id = UUID(marker.value["restore_id"])
    real_sweep = retention.sweep_all

    async def fail_sweep(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic sweep failure")

    monkeypatch.setattr(retention, "sweep_all", fail_sweep)
    with pytest.raises(RuntimeError, match="synthetic sweep failure"):
        await journal.complete_purge(env.factory, env.clock, restore_id=restore_id)
    async with env.factory() as session:
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is not None
    monkeypatch.setattr(retention, "sweep_all", real_sweep)
    await journal.complete_purge(env.factory, env.clock, restore_id=restore_id)
    async with env.factory() as session:
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is None
        # Unknown off-site history is never overwritten following fallback.
        assert await session.get(SystemState, "privacy_journal_upload_blocked") is not None


async def test_unknown_copy_inventory_blocks_tombstone_pruning(env: Env) -> None:
    await connect(env)
    receipt = UUID("00000000-0000-0000-0000-000000000001")
    async with transaction(env.factory) as uow:
        account = (await uow.session.scalars(select(Account))).one()
        await service.revoke_consent(
            uow.session, account, kind=service.CONSENT_AI_CONTROL, clock=env.clock
        )
    with pytest.raises(journal.JournalRecoveryError, match="journal_copy_inventory_required"):
        await journal.reset_after_prune(
            env.factory, env.clock, inventory_receipt=receipt, copies_destroyed=False
        )
    async with env.factory() as session:
        assert await session.scalar(select(PrivacyDecision.id)) is not None
    await journal.reset_after_prune(
        env.factory, env.clock, inventory_receipt=receipt, copies_destroyed=True
    )
    async with env.factory() as session:
        assert await session.scalar(select(PrivacyDecision.id)) is None


async def test_complete_purge_verifies_the_entire_content_registry(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant.privacy import deletion

    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message())
    await post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    async with env.factory() as session:
        marker = await session.get(SystemState, KEY_RESTORE_PENDING)
        assert marker is not None
        restore_id = UUID(marker.value["restore_id"])
    real_delete = deletion.delete_everything

    async def incomplete_delete(*args: object, **kwargs: object) -> DeletionReport:
        return DeletionReport(0, 0, 0, 0)

    monkeypatch.setattr(deletion, "delete_everything", incomplete_delete)
    with pytest.raises(journal.JournalRecoveryError, match="privacy_purge_incomplete"):
        await journal.complete_purge(env.factory, env.clock, restore_id=restore_id)
    async with env.factory() as session:
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is not None
        assert await session.scalar(select(Message.id)) is not None
    monkeypatch.setattr(deletion, "delete_everything", real_delete)
    await journal.complete_purge(env.factory, env.clock, restore_id=restore_id)
    async with env.factory() as session:
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is None
        assert await session.scalar(select(Message.id)) is None


async def test_append_failure_cannot_commit_a_withdrawal(env: Env) -> None:
    await connect(env)
    async with env.factory() as session:
        account = (await session.scalars(select(Account))).one()
        account_id = account.id
        recovery_id = account.recovery_id

    async def corrupt_head_and_withdraw() -> None:
        async with transaction(env.factory) as uow:
            # Corrupt only the transactional metadata to force append failure.
            await uow.session.execute(
                update(PrivacyJournalHead)
                .where(PrivacyJournalHead.account_recovery_id == recovery_id)
                .values(account_id=account_id + 1)
            )
            account = await uow.session.get(Account, account_id)
            assert account is not None
            await service.revoke_consent(
                uow.session, account, kind=service.CONSENT_AI_CONTROL, clock=env.clock
            )

    with pytest.raises(DBAPIError, match="privacy_identity_mismatch"):
        await corrupt_head_and_withdraw()
    async with env.factory() as session:
        state = await service.active_consent(session, account_id, kind=service.CONSENT_AI_CONTROL)
        assert state.granted
        assert await session.scalar(select(PrivacyDecision.id)) is None


async def test_reused_integer_id_never_aliases_a_retained_uuid_or_export(env: Env) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        account = (await uow.session.scalars(select(Account))).one()
        account_id = account.id
        retained_uuid = UUID("00000000-0000-0000-0000-000000000099")
        # A tombstone from a post-snapshot account whose integer ID can be
        # reused after restore. This is metadata, not an assigned identity PK.
        record = journal.PrivacyDecisionRecord(
            sequence=1,
            decision_id=UUID("00000000-0000-0000-0000-000000000098"),
            account_recovery_id=retained_uuid,
            scope="everything",
            subject_recovery_id=None,
            operation="delete",
            processor_id=None,
            decided_at=env.clock.now(),
            previous_digest=journal.ZERO_DIGEST,
            digest=journal.ZERO_DIGEST,
        )
        record = record.model_copy(update={"digest": record.calculated_digest()})
        uow.session.add(
            PrivacyJournalHead(
                account_id=account_id,
                account_recovery_id=retained_uuid,
                sequence=1,
                digest=record.digest,
            )
        )
        uow.session.add(
            PrivacyDecision(account_id=account_id, **record.model_dump(exclude={"version"}))
        )
        await service.revoke_consent(
            uow.session, account, kind=service.CONSENT_AI_CONTROL, clock=env.clock
        )
    async with env.factory() as session:
        exported = await journal.export_account(session, account_id=account_id)
        assert len(exported) == 1
        assert exported[0]["operation"] == "revoke_ai_control"
        assert exported[0]["account_recovery_id"] != str(retained_uuid)


async def test_post_backup_retention_change_requires_manual_reconciliation(env: Env) -> None:
    await connect(env)
    async with transaction(env.factory) as uow:
        account = (await uow.session.scalars(select(Account))).one()
        account_id = account.id
        old_settings = dict(account.settings)
        anchor = (await journal.anchors(uow.session))[0]
        await preferences.update(
            uow.session,
            account,
            retention={"control_chat": 14},
            actor="owner",
            clock=env.clock,
        )
        segment = await journal.export_segment(uow.session, checked_at=env.clock.now())
        assert segment.records[-1].scope == "retention_review"
    async with transaction(env.factory) as uow:
        uow.session.add(SystemState(key=KEY_RESTORE_PENDING, value={}))
        await uow.session.flush()
        await uow.session.execute(
            update(Account).where(Account.id == account_id).values(settings=old_settings)
        )
        await uow.session.execute(delete(PrivacyDecision))
        await uow.session.execute(
            update(PrivacyJournalHead)
            .where(PrivacyJournalHead.account_recovery_id == anchor.account_recovery_id)
            .values(sequence=anchor.sequence, digest=anchor.digest)
        )
    await post_restore(env.factory, env.clock, backup_taken_at=env.clock.now())
    with pytest.raises(journal.JournalRecoveryError, match="journal_retention_review_required"):
        await journal.reconcile(env.factory, env.clock, segment=segment)
    async with env.factory() as session:
        marker = await session.get(SystemState, KEY_RESTORE_PENDING)
        assert marker is not None
        restore_id = UUID(marker.value["restore_id"])
    await recovery_cli._retention(
        argparse.Namespace(
            restore_id=restore_id,
            account_id=account_id,
            messages_days=None,
            control_chat_days=14,
            raw_updates_days=None,
        ),
        env.factory,
    )
    await journal.complete_manual(
        env.factory,
        env.clock,
        restore_id=restore_id,
        account_ids=frozenset({account_id}),
        owner_confirmed=True,
    )
    async with env.factory() as session:
        account = await session.get(Account, account_id)
        assert account is not None
        assert account.settings["retention_days"]["control_chat"] == 14
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is None
