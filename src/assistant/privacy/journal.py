"""Ids-only privacy journal validation and restore reconciliation (C9/I20).

Network and age operations belong to the backup container. This module accepts
only the authenticated plaintext pipe, validates the complete chain before any
effect, and never calls a provider or Telegram. A local append is not an off-site
acknowledgement; deletion receipts and health output say so explicitly.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import column, delete, func, select, text, update
from sqlalchemy import table as sql_table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, AuditLogEntry, Consent
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate, NotificationSource
from assistant.memory.models import MemoryItem
from assistant.memory.summaries import invalidate_covering
from assistant.privacy.journal_models import PrivacyDecision, PrivacyJournalHead

ZERO_DIGEST = "0" * 64
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Scope = Literal[
    "control",
    "managed",
    "conversation",
    "contact",
    "everything",
    "message",
    "memory",
    "notification_source",
    "account",
    "retention_review",
]
Operation = Literal["delete", "purge", "revoke_ai_control", "revoke_ai_managed"]


class JournalRecoveryError(RuntimeError):
    """Fixed, content-free reason; caller must leave quarantine in place."""


class PrivacyDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    version: Literal[1] = 1
    sequence: int = Field(gt=0, strict=True)
    decision_id: UUID
    account_recovery_id: UUID
    scope: Scope
    subject_recovery_id: UUID | None
    operation: Operation
    processor_id: Literal["OpenAI"] | None
    decided_at: AwareDatetime
    previous_digest: Digest
    digest: Digest

    @model_validator(mode="after")
    def validate_scope(self) -> PrivacyDecisionRecord:
        scoped = self.scope in {
            "conversation",
            "contact",
            "message",
            "memory",
            "notification_source",
        }
        if scoped != (self.subject_recovery_id is not None):
            raise ValueError("journal_subject_invalid")
        if self.operation == "purge" and self.scope != "message":
            raise ValueError("journal_operation_unsupported")
        if self.operation in {"delete", "purge"}:
            if self.processor_id is not None:
                raise ValueError("journal_processor_invalid")
        elif self.processor_id is None or (self.scope, self.operation) not in {
            ("control", "revoke_ai_control"),
            ("managed", "revoke_ai_managed"),
        }:
            raise ValueError("journal_processor_invalid")
        return self

    def calculated_digest(self) -> str:
        # Matches append_privacy_decision exactly; all free-form strings are
        # forbidden by schema. Delimiters cannot occur inside UUIDs/enums.
        material = "|".join(
            (
                "1",
                str(self.sequence),
                str(self.decision_id),
                str(self.account_recovery_id),
                self.scope,
                str(self.subject_recovery_id) if self.subject_recovery_id else "",
                self.operation,
                self.processor_id or "",
                self.decided_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                self.previous_digest,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class JournalHead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    account_id: int = Field(gt=0, strict=True)
    account_recovery_id: UUID
    sequence: int = Field(ge=0, strict=True)
    digest: Digest


class JournalSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    checked_at: AwareDatetime
    heads: tuple[JournalHead, ...]
    records: tuple[PrivacyDecisionRecord, ...]


class PrivacyRecoveryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    restore_id: UUID
    backup_taken_at: AwareDatetime
    reconciled_through: AwareDatetime
    mechanism: Literal["independent_journal", "manual", "hybrid"]
    account_id: int = Field(gt=0, strict=True)
    deletion_reconciliation: Literal["complete", "unknown"]
    consent_reset: bool
    retention_complete: bool
    owner_confirmed_at: AwareDatetime | None
    journal_head: Digest | None


def recovery_may_open(
    evidence: PrivacyRecoveryEvidence | None,
    *,
    restore_id: UUID | str,
    account_id: int,
    cutoff: datetime | None = None,
) -> bool:
    if evidence is None:
        return False
    try:
        expected_restore = UUID(str(restore_id))
    except ValueError:
        return False
    if evidence.restore_id != expected_restore or evidence.account_id != account_id:
        return False
    if cutoff is None or cutoff.tzinfo is None:
        return False
    if not evidence.backup_taken_at <= cutoff <= evidence.reconciled_through:
        return False
    if (
        evidence.deletion_reconciliation != "complete"
        or not evidence.consent_reset
        or not evidence.retention_complete
    ):
        return False
    if evidence.mechanism == "independent_journal":
        return evidence.journal_head is not None
    return evidence.owner_confirmed_at is not None and evidence.owner_confirmed_at >= cutoff


async def anchors(session: AsyncSession) -> tuple[JournalHead, ...]:
    rows = await session.scalars(
        select(PrivacyJournalHead).order_by(PrivacyJournalHead.account_recovery_id)
    )
    return tuple(JournalHead.model_validate(row) for row in rows)


async def export_segment(session: AsyncSession, *, checked_at: datetime) -> JournalSegment:
    """Account ids/enums/timestamps only; never used to export message content."""
    rows = await session.scalars(
        select(PrivacyDecision).order_by(
            PrivacyDecision.account_recovery_id, PrivacyDecision.sequence
        )
    )
    return JournalSegment(
        checked_at=checked_at,
        heads=await anchors(session),
        records=tuple(PrivacyDecisionRecord.model_validate(row) for row in rows),
    )


async def export_account(session: AsyncSession, *, account_id: int) -> list[dict[str, object]]:
    rows = await session.scalars(
        select(PrivacyDecision)
        .where(
            PrivacyDecision.account_id == account_id,
            PrivacyDecision.account_recovery_id
            == select(Account.recovery_id).where(Account.id == account_id).scalar_subquery(),
        )
        .order_by(PrivacyDecision.sequence)
    )
    return [PrivacyDecisionRecord.model_validate(row).model_dump(mode="json") for row in rows]


def verify_segment(
    segment: JournalSegment, *, backup_heads: tuple[JournalHead, ...], cutoff: datetime
) -> None:
    """Validate all records, including the prefix, before replaying a single effect."""
    if segment.checked_at < cutoff:
        raise JournalRecoveryError("journal_stale_tail")
    heads = {head.account_recovery_id: head for head in segment.heads}
    if len(heads) != len(segment.heads) or not heads:
        raise JournalRecoveryError("journal_missing")
    seen: set[UUID] = set()
    positions = dict.fromkeys(heads, 0)
    digests = dict.fromkeys(heads, ZERO_DIGEST)
    checkpoints = {(identity, 0): ZERO_DIGEST for identity in heads}
    for record in segment.records:
        identity = record.account_recovery_id
        if identity not in heads or record.decision_id in seen:
            raise JournalRecoveryError("journal_identity_mismatch")
        seen.add(record.decision_id)
        if record.sequence != positions[identity] + 1:
            raise JournalRecoveryError("journal_sequence_gap")
        if (
            record.previous_digest != digests[identity]
            or record.digest != record.calculated_digest()
        ):
            raise JournalRecoveryError("journal_checksum_failed")
        if record.decided_at > segment.checked_at:
            raise JournalRecoveryError("journal_cutoff_invalid")
        positions[identity] = record.sequence
        digests[identity] = record.digest
        checkpoints[identity, record.sequence] = record.digest
    for identity, head in heads.items():
        if (positions[identity], digests[identity]) != (head.sequence, head.digest):
            raise JournalRecoveryError("journal_incomplete_tail")
    for anchor in backup_heads:
        backup_head = heads.get(anchor.account_recovery_id)
        if backup_head is None:
            raise JournalRecoveryError("journal_missing")
        if backup_head.account_id != anchor.account_id:
            raise JournalRecoveryError("journal_missing")
        if checkpoints.get((anchor.account_recovery_id, anchor.sequence)) != anchor.digest:
            raise JournalRecoveryError("journal_backup_anchor_mismatch")


async def _replay_record(
    session: AsyncSession, record: PrivacyDecisionRecord, clock: Clock
) -> None:
    from assistant.conversations.events import ObservedDeletion
    from assistant.conversations.service import record_deletion
    from assistant.privacy.deletion import (
        delete_contact_data,
        delete_conversation_data,
        delete_everything,
        delete_managed_data,
    )
    from assistant.privacy.owner_requests import purge_request_sources
    from assistant.privacy.retention import purge_message_content

    account = await session.scalar(
        select(Account).where(Account.recovery_id == record.account_recovery_id)
    )
    if account is None:
        return  # account created after snapshot, or already erased
    aid = account.id
    if record.operation == "purge":
        # Reuse ordinary retention effects: expire the source and request
        # proof, preserving rolling summaries that already covered it.
        await purge_message_content(
            session,
            clock,
            account_ids=[aid],
            message_filter=Message.recovery_id == record.subject_recovery_id,
        )
    elif record.operation != "delete":
        kind = "ai_control" if record.operation == "revoke_ai_control" else "ai_managed"
        await session.execute(
            update(Consent)
            .where(Consent.account_id == aid, Consent.kind == kind, Consent.revoked_at.is_(None))
            .values(revoked_at=record.decided_at)
        )
    elif record.scope in {"everything", "account"}:
        await delete_everything(session, account_id=aid, actor="system", clock=clock)
        if record.scope == "account":
            await session.execute(delete(Account).where(Account.id == aid))
    elif record.scope == "managed":
        await delete_managed_data(session, account_id=aid, actor="system", clock=clock)
    elif record.scope in {"conversation", "contact"}:
        model = Conversation if record.scope == "conversation" else Contact
        subject = await session.scalar(
            select(model.id).where(
                model.account_id == aid, model.recovery_id == record.subject_recovery_id
            )
        )
        if subject is not None:
            if record.scope == "conversation":
                await delete_conversation_data(
                    session, account_id=aid, conversation_id=subject, actor="system", clock=clock
                )
            else:
                await delete_contact_data(
                    session, account_id=aid, contact_id=subject, actor="system", clock=clock
                )
    elif record.scope == "message":
        row = (
            await session.execute(
                select(Message.id, Message.conversation_id, Message.telegram_message_id).where(
                    Message.account_id == aid, Message.recovery_id == record.subject_recovery_id
                )
            )
        ).one_or_none()
        if row is not None:
            message_id, conversation_id, telegram_message_id = row
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.account_id == aid, Conversation.id == conversation_id
                )
            )
            if conversation is None or telegram_message_id is None:
                raise JournalRecoveryError("journal_subject_unresolvable")
            await record_deletion(
                session,
                account_id=aid,
                event=ObservedDeletion(conversation.business_chat_id, (telegram_message_id,)),
                clock=clock,
            )
            # Post-restore retention may already have emptied the payload
            # while preserving its rolling summary. The journal proves an
            # explicit deletion, so invalidate even without new content loss.
            # record_deletion holds the conversation lock until replay commits.
            await invalidate_covering(session, conversation, (message_id,), now=clock.now())
    elif record.scope == "notification_source":
        await session.execute(
            delete(NotificationSource).where(
                NotificationSource.account_id == aid,
                NotificationSource.recovery_id == record.subject_recovery_id,
            )
        )
    elif record.scope == "memory":
        await session.execute(
            delete(MemoryItem).where(
                MemoryItem.account_id == aid, MemoryItem.recovery_id == record.subject_recovery_id
            )
        )
        # Held requests may contain a copy of a note. Their proof is no
        # longer valid after erasure; clear the account's held requests.
        await purge_request_sources(
            session, account_id=aid, now=clock.now(), everything=True, remove=True, locked=True
        )
    elif record.scope == "retention_review":
        raise JournalRecoveryError("journal_retention_review_required")
    else:
        raise JournalRecoveryError("journal_operation_unsupported")


async def reconcile(
    factory: async_sessionmaker[AsyncSession], clock: Clock, *, segment: JournalSegment
) -> None:
    """Replay is one transaction. A retry cannot grant consent or duplicate effects."""
    try:
        async with transaction(factory) as uow:
            session = uow.session
            marker = await session.get(SystemState, KEY_RESTORE_PENDING, with_for_update=True)
            if marker is None:
                # Already reconciled: repeated replay has no effects.
                return
            if marker.value.get("maintenance_complete") is not True:
                raise JournalRecoveryError("restore_maintenance_incomplete")
            backup_heads = tuple(
                JournalHead.model_validate(value) for value in marker.value["journal_anchors"]
            )
            if marker.value.get("journal_supported") is not True:
                raise JournalRecoveryError("journal_backup_unsupported")
            cutoff = datetime.fromisoformat(marker.value["recovery_cutoff"])
            if segment.checked_at > clock.now():
                raise JournalRecoveryError("journal_cutoff_invalid")
            verify_segment(segment, backup_heads=backup_heads, cutoff=cutoff)
            await session.execute(text("SET LOCAL app.privacy_replay = 'on'"))
            positions = {head.account_recovery_id: head.sequence for head in backup_heads}
            for record in segment.records:
                if record.sequence > positions.get(record.account_recovery_id, 0):
                    await _replay_record(session, record, clock)
            # Preserve the independent chain in the recovered DB, so the next
            # ship extends it rather than publishing the rewound snapshot.
            for head in segment.heads:
                current = await session.get(PrivacyJournalHead, head.account_recovery_id)
                if current is not None and current.account_id != head.account_id:
                    raise JournalRecoveryError("journal_identity_mismatch")
                if current is None:
                    current = PrivacyJournalHead(**head.model_dump())
                    session.add(current)
                current.sequence = head.sequence
                current.digest = head.digest
                existing = set(
                    await session.scalars(
                        select(PrivacyDecision.sequence).where(
                            PrivacyDecision.account_recovery_id == head.account_recovery_id
                        )
                    )
                )
                for record in segment.records:
                    if (
                        record.account_recovery_id == head.account_recovery_id
                        and record.sequence not in existing
                    ):
                        session.add(
                            PrivacyDecision(
                                account_id=head.account_id,
                                **record.model_dump(exclude={"version"}),
                            )
                        )
            await _finish(
                session, marker, clock, mechanism="independent_journal", heads=segment.heads
            )
    except JournalRecoveryError as error:
        async with transaction(factory) as uow:
            marker = await uow.session.get(SystemState, KEY_RESTORE_PENDING)
            if marker is not None:
                marker.value = {**marker.value, "privacy_reason": str(error)}
        raise


async def complete_purge(
    factory: async_sessionmaker[AsyncSession], clock: Clock, *, restore_id: UUID
) -> None:
    """Owner's conservative fallback. Requires the exact current restore UUID."""
    from assistant.automation.models import Automation
    from assistant.privacy.content_columns import CONTENT_COLUMNS
    from assistant.privacy.deletion import delete_everything
    from assistant.privacy.retention import sweep_all

    async with transaction(factory) as uow:
        session = uow.session
        marker = await session.get(SystemState, KEY_RESTORE_PENDING, with_for_update=True)
        if marker is None or marker.value.get("restore_id") != str(restore_id):
            raise JournalRecoveryError("restore_identity_mismatch")
        if marker.value.get("maintenance_complete") is not True:
            raise JournalRecoveryError("restore_maintenance_incomplete")
        for account in (await session.scalars(select(Account).order_by(Account.id))).all():
            await delete_everything(session, account_id=account.id, actor="owner", clock=clock)
            # Normal deletion keeps the digest schedule. The explicit complete
            # restore purge also removes its content-classified spec, even if
            # malformed; the preference reconciler can rebuild a fresh rule.
            await session.execute(delete(Automation).where(Automation.account_id == account.id))
        # Pending/raw, including unrouted inbox payloads, may still contain
        # erased content. Quarantine allows dropping all queued payloads.
        await session.execute(text("UPDATE app.inbound_updates SET payload = NULL"))
        for table_name, column_name in sorted(CONTENT_COLUMNS):
            target = sql_table(table_name, column(column_name), schema="app")
            remaining = await session.scalar(
                select(func.count()).select_from(target).where(target.c[column_name].is_not(None))
            )
            if remaining != 0:
                raise JournalRecoveryError("privacy_purge_incomplete")
        marker.value = {**marker.value, "purge_complete": True}
    await sweep_all(factory, clock)
    async with transaction(factory) as uow:
        marker = await uow.session.get(SystemState, KEY_RESTORE_PENDING, with_for_update=True)
        if marker is None or marker.value.get("restore_id") != str(restore_id):
            raise JournalRecoveryError("restore_identity_mismatch")
        await _finish(uow.session, marker, clock, mechanism="complete_purge")


async def _finish(
    session: AsyncSession,
    marker: SystemState,
    clock: Clock,
    *,
    mechanism: Literal["independent_journal", "complete_purge", "manual"],
    heads: tuple[JournalHead, ...] = (),
) -> None:
    from assistant.privacy.post_restore import KEY_RESTORE_NOTICE

    # Restored raw updates can contain a second copy of erased text and can
    # replay old consent answers. Discard their payloads before any role runs.
    await session.execute(
        update(InboundUpdate)
        .where(InboundUpdate.status.in_(("pending", "failed")))
        .values(payload=None, status="ignored", processed_at=clock.now(), error_code=None)
    )
    restore_id = UUID(marker.value["restore_id"])
    cutoff = datetime.fromisoformat(marker.value["recovery_cutoff"])
    if cutoff.tzinfo is None or cutoff > clock.now():
        raise JournalRecoveryError("journal_cutoff_invalid")
    source_heads = {head.account_recovery_id: head.digest for head in heads}
    for account in await session.scalars(select(Account).order_by(Account.id)):
        active = await session.scalar(
            select(Consent.id)
            .where(Consent.account_id == account.id, Consent.revoked_at.is_(None))
            .limit(1)
        )
        evidence = PrivacyRecoveryEvidence(
            restore_id=restore_id,
            backup_taken_at=datetime.fromisoformat(marker.value["backup_taken_at"]),
            reconciled_through=cutoff,
            mechanism="independent_journal" if mechanism == "independent_journal" else "manual",
            account_id=account.id,
            deletion_reconciliation="complete",
            consent_reset=active is None,
            retention_complete=marker.value.get("maintenance_complete") is True,
            owner_confirmed_at=clock.now() if mechanism != "independent_journal" else None,
            journal_head=source_heads.get(account.recovery_id),
        )
        if not recovery_may_open(
            evidence, restore_id=restore_id, account_id=account.id, cutoff=cutoff
        ):
            raise JournalRecoveryError("privacy_evidence_incomplete")
        session.add(
            AuditLogEntry(
                account_id=account.id,
                at=clock.now(),
                actor="system",
                event="privacy_reconciled",
                details={
                    "evidence": evidence.model_dump(mode="json"),
                    "recovery_method": mechanism,
                },
            )
        )
    notice = await session.get(SystemState, KEY_RESTORE_NOTICE)
    payload = marker.value["recovery_notice"]
    if notice is None:
        session.add(SystemState(key=KEY_RESTORE_NOTICE, value=payload))
    else:
        notice.value = payload
    blocked = await session.get(SystemState, "privacy_journal_upload_blocked")
    if blocked is not None:
        if mechanism == "independent_journal":
            await session.delete(blocked)
        else:
            blocked.value = {"reason": "journal_copy_inventory_required"}
    await session.delete(marker)


async def complete_manual(
    factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    restore_id: UUID,
    account_ids: frozenset[int],
    owner_confirmed: bool,
) -> None:
    """Explicit operator attestation after reissuing every known deletion.

    Missing knowledge is not an attestation: the runbook requires purge instead.
    Confirmation is bound to all accounts in this restore, never an old consent.
    """
    from assistant.privacy.retention import sweep_all

    if not owner_confirmed:
        raise JournalRecoveryError("privacy_owner_confirmation_required")
    async with transaction(factory) as uow:
        await _manual_marker(uow.session, restore_id=restore_id, account_ids=account_ids)
    await sweep_all(factory, clock)
    async with transaction(factory) as uow:
        marker = await _manual_marker(uow.session, restore_id=restore_id, account_ids=account_ids)
        await _finish(uow.session, marker, clock, mechanism="manual")


async def _manual_marker(
    session: AsyncSession, *, restore_id: UUID, account_ids: frozenset[int]
) -> SystemState:
    marker = await session.get(SystemState, KEY_RESTORE_PENDING, with_for_update=True)
    if marker is None or marker.value.get("restore_id") != str(restore_id):
        raise JournalRecoveryError("restore_identity_mismatch")
    if marker.value.get("maintenance_complete") is not True:
        raise JournalRecoveryError("restore_maintenance_incomplete")
    if frozenset(await session.scalars(select(Account.id))) != account_ids:
        raise JournalRecoveryError("restore_account_mismatch")
    return marker


async def reset_after_prune(
    factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    inventory_receipt: UUID,
    copies_destroyed: bool,
) -> None:
    """Explicit last-copy receipt only; time passing can never prune a tombstone.

    The runbook requires both the app and sidecar stopped and independently
    confirmed destruction of ALL old snapshots and remote journal objects.
    """
    if not copies_destroyed:
        raise JournalRecoveryError("journal_copy_inventory_required")
    async with transaction(factory) as uow:
        session = uow.session
        if await session.get(SystemState, KEY_RESTORE_PENDING) is not None:
            raise JournalRecoveryError("privacy_reconciliation_required")
        account_ids = tuple(await session.scalars(select(Account.id).order_by(Account.id)))
        await session.execute(delete(PrivacyDecision))
        await session.execute(
            delete(PrivacyJournalHead).where(
                PrivacyJournalHead.account_recovery_id.not_in(select(Account.recovery_id))
            )
        )
        await session.execute(update(PrivacyJournalHead).values(sequence=0, digest=ZERO_DIGEST))
        for account_id in account_ids:
            session.add(
                AuditLogEntry(
                    account_id=account_id,
                    at=clock.now(),
                    actor="owner",
                    event="privacy_journal_pruned",
                    details={
                        "inventory_receipt": str(inventory_receipt),
                        "last_copy_destroyed": True,
                    },
                )
            )
        blocked = await session.get(SystemState, "privacy_journal_upload_blocked")
        if blocked is not None:
            await session.delete(blocked)
