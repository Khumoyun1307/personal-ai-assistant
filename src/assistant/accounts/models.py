"""Tenant root and owner-facing state (ARCHITECTURE §10.3).

`accounts` is the tenant root: every other tenant-owned row points at it, and
every query is scoped by it (AGENTS §6.12). One account = one owner's whole
world. A contact is *not* an account — it is a row inside the owner's tenant.
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
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk, JsonDict, TelegramId

ACCOUNT_STATUSES = ("onboarding", "active", "blocked", "deleted")
CONSENT_KINDS = ("ai_control", "ai_managed")
CONNECTION_STATUSES = ("pending_owner", "active", "disabled", "stale")
CAPABILITY_TIERS = ("none", "observe_only", "can_reply", "extended")
CONNECTION_EVENT_SOURCES = ("update", "bootstrap", "onboarding")
CONTROL_TURN_ROLES = ("owner", "assistant")
AUDIT_ACTORS = ("owner", "agent", "system")


def _enum_check(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    """Enums are text + CHECK, so adding a value is a one-line migration."""
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


class Account(Base):
    """One owner. The tenant root."""

    __tablename__ = "accounts"
    __table_args__ = (_enum_check("status", ACCOUNT_STATUSES, "status"),)

    id: Mapped[IdPk]
    telegram_user_id: Mapped[TelegramId] = mapped_column(unique=True, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="onboarding")
    #: The owner's private chat with the bot. NULL until they press Start —
    #: until then the bot cannot reach them at all (TF §12 LV-09).
    control_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")
    locale_pref: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings: Mapped[JsonDict] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'")
    )
    autonomy_paused: Mapped[bool] = mapped_column(nullable=False, server_default=sql_text("false"))
    #: The policy version whose update notice is settled for this owner
    #: (delivered, or sent with an unknown outcome). NULL: none yet. The
    #: durable "already told" marker; audit rows are swept after a year
    #: (`inbox.policy_notice`, M02 D59).
    policy_notice_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]


class Consent(Base):
    """Append-only consent record; current = latest row with no `revoked_at`.

    Gates every LLM call (§6 invariant 9). Never updated in place: revoking
    sets `revoked_at`, and granting again inserts a new row, so the history of
    what was agreed to, and when, survives.
    """

    __tablename__ = "consents"
    __table_args__ = (
        _enum_check("kind", CONSENT_KINDS, "kind"),
        Index("ix_consents_account_id_kind", "account_id", "kind"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    #: Named processors (e.g. the LLM provider). SEC-02: consent names them.
    #: A JSON array, not an object — the order people are told about matters.
    processors: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'[]'")
    )
    granted_at: Mapped[CreatedAt]
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class BusinessConnection(Base):
    """The owner's Chat Automation connection to this bot (ARCHITECTURE §6.4).

    One row per account — an account can connect one bot. Keyed by account,
    not by Telegram's `connection_id`, because that id is **mutable**: a
    reconnect issues a new one (TF §12 LV-05). The history of ids lives in
    `business_connection_events`.
    """

    __tablename__ = "business_connections"
    __table_args__ = (
        _enum_check("status", CONNECTION_STATUSES, "status"),
        _enum_check("capability_tier", CAPABILITY_TIERS, "capability_tier"),
        Index("ix_business_connections_connection_id", "connection_id"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app.accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    connection_id: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(nullable=False)
    #: Granted right names, e.g. ["can_reply", "can_read_messages"]. Empty when
    #: `rights` was absent or the connection is disabled: no permissions (§6.7).
    rights: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'[]'")
    )
    capability_tier: Mapped[str] = mapped_column(Text, nullable=False)
    user_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    #: Telegram's `date` for the current connection id. A reconnect moves it
    #: forward; rights and chat-list edits do not (TF §12 LV-05). Used to
    #: refuse a late event about an older id.
    connected_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at: Mapped[CreatedAt]


class BusinessConnectionEvent(Base):
    """One observed connection state, for audit and id rotation.

    Content-free: ids, rights, flags and a digest of the payload — never the
    owner's name or username.
    """

    __tablename__ = "business_connection_events"
    __table_args__ = (
        _enum_check("source", CONNECTION_EVENT_SOURCES, "source"),
        Index("ix_business_connection_events_account_id", "account_id"),
        Index("ix_business_connection_events_connection_id", "connection_id"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    rights: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'[]'")
    )
    is_enabled: Mapped[bool] = mapped_column(nullable=False)
    #: What the event changed (`connected`, `rights_changed`, …) — see
    #: `accounts.connections.ConnectionChange`.
    change: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[CreatedAt]


class ForeignConnection(Base):
    """A connection from someone who is not an allowlisted owner.

    Deliberately **not** a tenant table: there is no account, and there must
    never be one (§6 invariant 12). Metadata only, so later updates on the id
    are rejected without asking Telegram again. Purged 30 days after
    `last_seen_at` (ARCHITECTURE §10.4).
    """

    __tablename__ = "foreign_connections"

    connection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_seen_at: Mapped[CreatedAt]
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class ControlTurn(Base):
    """One message in the owner's conversation with the assistant.

    CONTENT-BEARING (`text`, `quoted_text`) — listed in ARCHITECTURE §10.1,
    retention §10.4.
    """

    __tablename__ = "control_turns"
    __table_args__ = (
        _enum_check("role", CONTROL_TURN_ROLES, "role"),
        Index("ix_control_turns_account_id_created_at", "account_id", "created_at"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: True when this turn was produced from managed-conversation content.
    #: Such turns are excluded from context when `ai_managed` is not active
    #: (§6 invariant 9).
    contains_managed: Mapped[bool] = mapped_column(nullable=False, server_default=sql_text("false"))
    #: True when the owner's message carries third-party text: it was
    #: forwarded (then `text` itself is the third party's), or it quotes or
    #: replies to a message from another chat (`quoted_text`). On an
    #: assistant turn: the run that wrote it was tainted by its OWN inputs
    #: (it may restate such text). A run that sees such a turn is tainted
    #: (§6 invariant 3b, §7.1).
    third_party: Mapped[bool] = mapped_column(nullable=False, server_default=sql_text("false"))
    #: An assistant reply written by a tainted run, for whatever reason (own
    #: or inherited taint). Always shown to the model as untrusted; it taints
    #: later runs only if `third_party` is set too, so taint decays (D56).
    from_tainted_run: Mapped[bool] = mapped_column(nullable=False, server_default=sql_text("false"))
    #: The quoted or replied-to third-party text, so the agent understands
    #: what the owner refers to. CONTENT-BEARING.
    quoted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Conversation/contact ids this turn drew on, so deletion can purge it.
    refs: Mapped[JsonDict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'"))
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]


class PendingInput(Base):
    """Multi-step control-chat UI state.

    Lives in Postgres, not aiogram FSM (AGENTS §7): it must survive restarts
    and be visible to jobs.
    """

    __tablename__ = "pending_inputs"
    __table_args__ = (
        Index("ix_pending_inputs_account_id_kind", "account_id", "kind"),
        Index(
            "uq_pending_inputs_token",
            "token",
            unique=True,
            postgresql_where=sql_text("token IS NOT NULL"),
        ),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Set for a confirmation card (single-use button token, §6.7); NULL for
    #: a plain "answer with your next message" question.
    token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[CreatedAt]


class AuditLogEntry(Base):
    """Append-only, owner-visible record of what happened (FR-ACT-01, SEC-11).

    `details` holds ids and enums only — never message content.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        _enum_check("actor", AUDIT_ACTORS, "actor"),
        Index("ix_audit_log_account_id_at", "account_id", "at"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[CreatedAt]
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'")
    )
