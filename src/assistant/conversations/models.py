"""Managed conversations: contacts, conversations, messages, coverage gaps
(ARCHITECTURE §6.5, §6.6, §10.3).

Every table here is tenant-owned (`account_id NOT NULL`, AGENTS §6.12).
CONTENT-BEARING columns (ARCHITECTURE §10.1, retention §10.4):
`contacts.display_name`, `contacts.username`, `messages.text` (which also
holds a media caption), `messages.media`, `messages.search_key`. Everything
else is ids, enums, flags and timestamps.

Keys follow the verified Telegram facts: a conversation is
`(account_id, business_chat_id)` — never `chat_id` alone, because business
chat ids live in the owner's numbering, not the bot's (AGENTS §7); a message
is `(conversation_id, telegram_message_id)`, with NULL meaning "sent, identity
pending" (ARCHITECTURE §6.8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk

CONVERSATION_KINDS = ("business",)
CONVERSATION_MODES = ("ignore", "observe", "assist")
AVAILABILITY_STATES = ("active", "possibly_paused")
AUTONOMY_PAUSE_REASONS = ("takeover", "ingest_error")
MESSAGE_DIRECTIONS = ("contact", "owner_manual", "owner_automated", "assistant_sent")
MESSAGE_OBSERVED_VIA = ("new", "edit", "deletion")
GAP_KINDS = ("outage", "restore", "dead_letter")
GAP_CERTAINTIES = ("delayed", "possible", "definite")

#: The default for a new conversation (PRD §22 Q-06; owner-configurable in M03).
DEFAULT_MODE = "observe"


def _enum_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


def _account_fk() -> Mapped[int]:
    return mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )


class Contact(Base):
    """A person the owner talks to. Lives inside the owner's tenant.

    CONTENT-BEARING: `display_name`, `username` (kept while the contact has
    retained data; cleared by per-contact deletion — §10.4).
    """

    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "telegram_user_id", name="uq_contacts_account_id_telegram_user_id"
        ),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = _account_fk()
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_bot: Mapped[bool] = mapped_column(nullable=False, server_default=sql_text("false"))
    first_seen_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]


class Conversation(Base):
    """One managed 1:1 chat on the owner's account."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "business_chat_id", name="uq_conversations_account_id_business_chat_id"
        ),
        Index("ix_conversations_account_id_contact_id", "account_id", "contact_id"),
        _enum_check("kind", CONVERSATION_KINDS, "kind"),
        _enum_check("mode", CONVERSATION_MODES, "mode"),
        _enum_check("availability", AVAILABILITY_STATES, "availability"),
        CheckConstraint(
            "autonomy_paused_reason IS NULL OR "
            "autonomy_paused_reason IN ('takeover', 'ingest_error')",
            name="ck_conversations_autonomy_paused_reason",
        ),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = _account_fk()
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'business'"))
    business_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    contact_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.contacts.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'observe'"))
    availability: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'active'")
    )
    #: Telegram `date` of the newest *new* contact message — the reply-window
    #: anchor (§6.6). Edits never move it (TF §12 LV-08/LV-15).
    last_contact_msg_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_owner_msg_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_assistant_msg_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    #: `date` of the first message we stored: everything before it is outside
    #: coverage (there is no history API — AGENTS §7).
    coverage_started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    autonomy_paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Set when content arrived that no AI run has seen yet; consumed by the
    #: summary scheduler once `ai_managed` consent is active (§14.3).
    pending_ai_since: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    #: Newest `sent_at` purged by retention without a summary (§10.4).
    summary_gap_before: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]


class Message(Base):
    """One message in a managed conversation, as observed.

    CONTENT-BEARING: `text` (also the caption), `media`, `search_key`. A
    deletion or the retention sweep nulls them and keeps the row as a
    tombstone (`deleted_at` / `purged_at`), so ids stay unique and a late
    edit cannot resurrect content.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "telegram_message_id",
            name="uq_messages_conversation_id_telegram_message_id",
        ),
        Index(
            "ix_messages_account_id_conversation_id_sent_at",
            "account_id",
            "conversation_id",
            "sent_at",
        ),
        Index(
            "ix_messages_content_sent_at",
            "sent_at",
            postgresql_where=sql_text("text IS NOT NULL OR media IS NOT NULL"),
        ),
        _enum_check("observed_via", MESSAGE_OBSERVED_VIA, "observed_via"),
        CheckConstraint(
            "direction IS NULL OR direction IN "
            "('contact', 'owner_manual', 'owner_automated', 'assistant_sent')",
            name="ck_messages_direction",
        ),
        # A row with no direction or date is only ever a tombstone.
        CheckConstraint(
            "deleted_at IS NOT NULL OR (direction IS NOT NULL AND sent_at IS NOT NULL)",
            name="ck_messages_tombstone_or_complete",
        ),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = _account_fk()
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.conversations.id", ondelete="CASCADE"), nullable=False
    )
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_via: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'new'")
    )
    #: Telegram `date` — never receipt time (§6.5 step 3). `created_at` is receipt.
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    reply_to_telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Structural media facts only: kind, file_unique_id, size, mime, duration,
    #: dimensions. Never `file_id` (a download capability), never a file name.
    media: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    search_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    script: Mapped[str | None] = mapped_column(Text, nullable=True)
    lang_guess: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Hash of the content, for no-op edit detection. Nulled with the content:
    #: the hash of a short message is guessable.
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_update_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.inbound_updates.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]


class CoverageGap(Base):
    """A range of time in which messages may be missing (§6.6, FR-OBS-05).

    Content-free. `inbound_update_id` makes a dead-letter gap idempotent: the
    same failed row never produces two gaps.
    """

    __tablename__ = "coverage_gaps"
    __table_args__ = (
        Index("ix_coverage_gaps_account_id_conversation_id", "account_id", "conversation_id"),
        Index(
            "uq_coverage_gaps_inbound_update_id",
            "inbound_update_id",
            unique=True,
            postgresql_where=sql_text("inbound_update_id IS NOT NULL"),
        ),
        _enum_check("kind", GAP_KINDS, "kind"),
        _enum_check("certainty", GAP_CERTAINTIES, "certainty"),
        CheckConstraint("at_risk_from <= at_risk_to", name="ck_coverage_gaps_range"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = _account_fk()
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.conversations.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    certainty: Mapped[str] = mapped_column(Text, nullable=False)
    at_risk_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    at_risk_to: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    inbound_update_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.inbound_updates.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
