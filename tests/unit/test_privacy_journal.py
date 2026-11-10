"""C9 chain verification rejects missing prefixes and suffixes before replay."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from assistant.privacy.journal import (
    ZERO_DIGEST,
    JournalHead,
    JournalRecoveryError,
    JournalSegment,
    PrivacyDecisionRecord,
    PrivacyRecoveryEvidence,
    recovery_may_open,
    verify_segment,
)

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
ACCOUNT = UUID("00000000-0000-0000-0000-000000000001")
DECISION = UUID("00000000-0000-0000-0000-000000000002")


def decision() -> PrivacyDecisionRecord:
    row = PrivacyDecisionRecord(
        sequence=1,
        decision_id=DECISION,
        account_recovery_id=ACCOUNT,
        scope="everything",
        subject_recovery_id=None,
        operation="delete",
        processor_id=None,
        decided_at=NOW,
        previous_digest=ZERO_DIGEST,
        digest=ZERO_DIGEST,
    )
    return row.model_copy(update={"digest": row.calculated_digest()})


def test_complete_chain_and_exact_backup_checkpoint_are_required() -> None:
    row = decision()
    head = JournalHead(account_id=1, account_recovery_id=ACCOUNT, sequence=1, digest=row.digest)
    segment = JournalSegment(checked_at=NOW, heads=(head,), records=(row,))
    verify_segment(segment, backup_heads=(head,), cutoff=NOW)
    bad_anchor = head.model_copy(update={"digest": "1" * 64})
    with pytest.raises(JournalRecoveryError, match="journal_backup_anchor_mismatch"):
        verify_segment(segment, backup_heads=(bad_anchor,), cutoff=NOW)


def test_hash_chain_without_an_independent_tail_cannot_certify_a_restore() -> None:
    row = decision()
    head = JournalHead(account_id=1, account_recovery_id=ACCOUNT, sequence=2, digest=row.digest)
    segment = JournalSegment(checked_at=NOW, heads=(head,), records=(row,))
    with pytest.raises(JournalRecoveryError, match="journal_incomplete_tail"):
        verify_segment(segment, backup_heads=(), cutoff=NOW)


def test_missing_backup_account_head_fails_closed() -> None:
    row = decision()
    head = JournalHead(account_id=1, account_recovery_id=ACCOUNT, sequence=1, digest=row.digest)
    missing = JournalHead(
        account_id=2, account_recovery_id=DECISION, sequence=0, digest=ZERO_DIGEST
    )
    segment = JournalSegment(checked_at=NOW, heads=(head,), records=(row,))
    with pytest.raises(JournalRecoveryError, match="journal_missing"):
        verify_segment(segment, backup_heads=(missing,), cutoff=NOW)


def test_stale_complete_chain_does_not_establish_the_cutoff() -> None:
    row = decision()
    head = JournalHead(account_id=1, account_recovery_id=ACCOUNT, sequence=1, digest=row.digest)
    segment = JournalSegment(checked_at=NOW, heads=(head,), records=(row,))
    with pytest.raises(JournalRecoveryError, match="journal_stale_tail"):
        verify_segment(segment, backup_heads=(), cutoff=NOW + timedelta(seconds=1))


@pytest.mark.parametrize("field", ["text", "name", "username", "token", "content"])
def test_record_schema_rejects_every_content_field(field: str) -> None:
    payload = decision().model_dump(mode="json")
    payload[field] = "synthetic"
    with pytest.raises(ValidationError):
        PrivacyDecisionRecord.model_validate(payload)


@pytest.mark.parametrize(
    "scope", ["contact", "conversation", "memory", "message", "notification_source"]
)
def test_scoped_deletion_requires_a_stable_subject_uuid(scope: str) -> None:
    payload = decision().model_dump(mode="json")
    payload["scope"] = scope
    with pytest.raises(ValidationError, match="journal_subject_invalid"):
        PrivacyDecisionRecord.model_validate(payload)


def test_withdrawal_cannot_become_a_grant_or_an_unnamed_processor() -> None:
    payload = decision().model_dump(mode="json")
    payload.update(operation="revoke_ai_control", scope="control")
    with pytest.raises(ValidationError, match="journal_processor_invalid"):
        PrivacyDecisionRecord.model_validate(payload)
    payload.update(operation="grant_ai_control", processor_id="OpenAI")
    with pytest.raises(ValidationError):
        PrivacyDecisionRecord.model_validate(payload)


def test_retention_purge_is_a_distinct_message_only_operation() -> None:
    payload = decision().model_dump(mode="json")
    payload.update(operation="purge", scope="message", subject_recovery_id=str(DECISION))
    record = PrivacyDecisionRecord.model_validate(payload)
    assert record.operation == "purge"
    deletion = record.model_copy(update={"operation": "delete"})
    assert record.calculated_digest() != deletion.calculated_digest()
    payload.update(processor_id="OpenAI")
    with pytest.raises(ValidationError, match="journal_processor_invalid"):
        PrivacyDecisionRecord.model_validate(payload)


@pytest.mark.parametrize(
    "scope", ["everything", "account", "conversation", "memory", "notification_source", "control"]
)
def test_retention_purge_cannot_erase_other_scopes(scope: str) -> None:
    payload = decision().model_dump(mode="json")
    payload.update(
        operation="purge",
        scope=scope,
        subject_recovery_id=(
            str(DECISION) if scope in {"conversation", "memory", "notification_source"} else None
        ),
    )
    with pytest.raises(ValidationError, match="journal_operation_unsupported"):
        PrivacyDecisionRecord.model_validate(payload)


def test_recovery_evidence_is_bound_to_restore_account_and_cutoff() -> None:
    evidence = PrivacyRecoveryEvidence(
        restore_id=DECISION,
        backup_taken_at=NOW - timedelta(hours=1),
        reconciled_through=NOW,
        mechanism="manual",
        account_id=1,
        deletion_reconciliation="complete",
        consent_reset=True,
        retention_complete=True,
        owner_confirmed_at=NOW,
        journal_head=None,
    )
    assert recovery_may_open(evidence, restore_id=DECISION, account_id=1, cutoff=NOW)
    assert not recovery_may_open(evidence, restore_id=DECISION, account_id=1)
    assert not recovery_may_open(evidence, restore_id=ACCOUNT, account_id=1, cutoff=NOW)
    assert not recovery_may_open(evidence, restore_id=DECISION, account_id=2, cutoff=NOW)
    assert not recovery_may_open(
        evidence, restore_id=DECISION, account_id=1, cutoff=NOW + timedelta(seconds=1)
    )
    assert not recovery_may_open(
        evidence.model_copy(update={"retention_complete": False}),
        restore_id=DECISION,
        account_id=1,
        cutoff=NOW,
    )
    assert not recovery_may_open(
        evidence.model_copy(update={"owner_confirmed_at": None}),
        restore_id=DECISION,
        account_id=1,
        cutoff=NOW,
    )
