"""Retained privacy enforcement metadata; deliberately no content or tenant FK.

The opaque account identity and tombstones must survive account erasure until
the last restorable snapshot is verifiably destroyed (C9). Never age-prune them.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Identity, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, IdPk, UtcTimestamp

SCOPE_CHECK = (
    "scope IN ('control', 'managed', 'conversation', 'contact', 'everything', "
    "'message', 'memory', 'notification_source', 'account', 'retention_review')"
)
OPERATION_CHECK = "operation IN ('delete', 'purge', 'revoke_ai_control', 'revoke_ai_managed')"
SUBJECT_CHECK = (
    "(scope IN ('conversation', 'contact', 'message', 'memory', 'notification_source')) = "
    "(subject_recovery_id IS NOT NULL)"
)
PROCESSOR_CHECK = (
    "(operation = 'delete' AND processor_id IS NULL) OR "
    "(operation = 'purge' AND scope = 'message' AND processor_id IS NULL) OR "
    "(operation = 'revoke_ai_control' AND scope = 'control' "
    "AND processor_id IS NOT NULL AND processor_id = 'OpenAI') OR "
    "(operation = 'revoke_ai_managed' AND scope = 'managed' "
    "AND processor_id IS NOT NULL AND processor_id = 'OpenAI')"
)


class PrivacyJournalHead(Base):
    __tablename__ = "privacy_journal_heads"
    __table_args__ = (
        CheckConstraint("sequence >= 0", name="sequence"),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest"),
    )

    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    account_recovery_id: Mapped[UUID] = mapped_column(primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    digest: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("repeat('0', 64)")
    )


class PrivacyDecision(Base):
    __tablename__ = "privacy_journal"
    __table_args__ = (
        UniqueConstraint("account_recovery_id", "sequence"),
        UniqueConstraint("decision_id"),
        CheckConstraint("sequence > 0", name="sequence"),
        CheckConstraint(SCOPE_CHECK, name="scope"),
        CheckConstraint(OPERATION_CHECK, name="operation"),
        CheckConstraint(SUBJECT_CHECK, name="subject"),
        CheckConstraint(PROCESSOR_CHECK, name="processor"),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("previous_digest ~ '^[0-9a-f]{64}$'", name="previous_digest"),
    )

    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decision_id: Mapped[UUID] = mapped_column(nullable=False)
    account_recovery_id: Mapped[UUID] = mapped_column(nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    subject_recovery_id: Mapped[UUID | None] = mapped_column(nullable=True)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    processor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[UtcTimestamp]
    previous_digest: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
