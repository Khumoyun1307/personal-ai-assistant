"""The durable inbox (ARCHITECTURE §6.1, §10.3).

Every Telegram update is written here before anything else happens, and the
poller's offset only advances after the write commits. That is what makes
"crash mid-processing loses nothing" true.

Note the deliberate exception to tenant scoping: `account_id` is NULLABLE here.
An update arrives before we know whose it is, and updates from non-allowlisted
users never get one at all — their payload is cleared at processing and no
domain rows are created (§6 invariant 12).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk

INBOUND_STATUSES = ("pending", "processed", "ignored", "rejected", "failed")


class InboundUpdate(Base):
    """One raw Telegram update, exactly once.

    CONTENT-BEARING (`payload`) — ARCHITECTURE §10.1; cleared at processing for
    ignored/rejected rows, otherwise purged after 7 days (§10.4).
    """

    __tablename__ = "inbound_updates"
    __table_args__ = (
        # The idempotency guarantee: Telegram may redeliver an update after a
        # crash, and this is what makes the redelivery a no-op. Keyed by bot id
        # too, because dev and prod bots number their updates independently.
        UniqueConstraint("bot_id", "update_id", name="uq_inbound_updates_bot_id_update_id"),
        CheckConstraint(
            "status IN ('pending', 'processed', 'ignored', 'rejected', 'failed')",
            name="ck_inbound_updates_status",
        ),
        # The claim query: oldest pending first.
        Index(
            "ix_inbound_updates_pending",
            "received_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_inbound_updates_account_id", "account_id"),
    )

    id: Mapped[IdPk]
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: NULL until routed, and permanently NULL for non-allowlisted senders.
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="SET NULL"), nullable=True
    )
    #: Set once the update becomes a stored message, so deleting that message
    #: can clear this payload too (§6.5 step 6). The FK (revision 0005) is
    #: SET NULL: purging a message must not purge the audit of its arrival.
    message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app.messages.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        index=True,
    )
    #: `none_as_null`: clearing the payload must store SQL NULL, not a JSON
    #: `null` value, so `IS NULL` checks (retention, audits) see it as empty.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[CreatedAt]
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
