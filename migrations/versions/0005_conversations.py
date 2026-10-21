"""M02 S02.2: managed conversations.

`contacts`, `conversations`, `messages` and `coverage_gaps` (ARCHITECTURE
§10.3), plus the link from `inbound_updates.message_id` to the message it
became, so a deletion can clear the raw payload too (§6.5 step 6).

Hand-written and reviewed (AGENTS §12). Content-bearing columns added:
`contacts.display_name`, `contacts.username`, `messages.text`,
`messages.media`, `messages.search_key` — each listed in ARCHITECTURE §10.1
with a §10.4 retention row and covered by the retention sweep.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP = "app"


def _created(name: str) -> sa.Column:
    return sa.Column(
        name, sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=True)


def _fk(table: str, column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    referred = target.split(".")[0]
    return sa.ForeignKeyConstraint(
        [column], [f"{APP}.{target}"], name=f"fk_{table}_{column}_{referred}", ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger, nullable=False),
        sa.Column("display_name", sa.Text, nullable=True),
        sa.Column("username", sa.Text, nullable=True),
        sa.Column("is_bot", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _created("first_seen_at"),
        _created("updated_at"),
        _fk("contacts", "account_id", "accounts.id", ondelete="CASCADE"),
        sa.UniqueConstraint(
            "account_id", "telegram_user_id", name="uq_contacts_account_id_telegram_user_id"
        ),
        schema=APP,
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False, server_default=sa.text("'business'")),
        sa.Column("business_chat_id", sa.BigInteger, nullable=False),
        sa.Column("contact_id", sa.BigInteger, nullable=False),
        sa.Column("mode", sa.Text, nullable=False, server_default=sa.text("'observe'")),
        sa.Column("availability", sa.Text, nullable=False, server_default=sa.text("'active'")),
        _ts("last_contact_msg_at"),
        _ts("last_owner_msg_at"),
        _ts("last_assistant_msg_at"),
        _ts("coverage_started_at"),
        sa.Column("autonomy_paused_reason", sa.Text, nullable=True),
        _ts("pending_ai_since"),
        _ts("summary_gap_before"),
        _created("created_at"),
        _created("updated_at"),
        _fk("conversations", "account_id", "accounts.id", ondelete="CASCADE"),
        _fk("conversations", "contact_id", "contacts.id", ondelete="CASCADE"),
        sa.UniqueConstraint(
            "account_id", "business_chat_id", name="uq_conversations_account_id_business_chat_id"
        ),
        sa.CheckConstraint("kind IN ('business')", name="ck_conversations_kind"),
        sa.CheckConstraint(
            "mode IN ('ignore', 'observe', 'assist')", name="ck_conversations_mode"
        ),
        sa.CheckConstraint(
            "availability IN ('active', 'possibly_paused')", name="ck_conversations_availability"
        ),
        sa.CheckConstraint(
            "autonomy_paused_reason IS NULL OR "
            "autonomy_paused_reason IN ('takeover', 'ingest_error')",
            name="ck_conversations_autonomy_paused_reason",
        ),
        schema=APP,
    )
    op.create_index(
        "ix_conversations_account_id_contact_id",
        "conversations",
        ["account_id", "contact_id"],
        schema=APP,
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("conversation_id", sa.BigInteger, nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger, nullable=True),
        sa.Column("direction", sa.Text, nullable=True),
        sa.Column("observed_via", sa.Text, nullable=False, server_default=sa.text("'new'")),
        _ts("sent_at"),
        _ts("edited_at"),
        _ts("deleted_at"),
        _ts("purged_at"),
        sa.Column("reply_to_telegram_message_id", sa.BigInteger, nullable=True),
        sa.Column("content_type", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=True),
        sa.Column("media", postgresql.JSONB, nullable=True),
        sa.Column("search_key", sa.Text, nullable=True),
        sa.Column("script", sa.Text, nullable=True),
        sa.Column("lang_guess", sa.Text, nullable=True),
        sa.Column("content_hash", sa.Text, nullable=True),
        sa.Column("source_update_id", sa.BigInteger, nullable=True),
        _created("created_at"),
        _created("updated_at"),
        _fk("messages", "account_id", "accounts.id", ondelete="CASCADE"),
        _fk("messages", "conversation_id", "conversations.id", ondelete="CASCADE"),
        _fk("messages", "source_update_id", "inbound_updates.id", ondelete="SET NULL"),
        sa.UniqueConstraint(
            "conversation_id",
            "telegram_message_id",
            name="uq_messages_conversation_id_telegram_message_id",
        ),
        sa.CheckConstraint(
            "observed_via IN ('new', 'edit', 'deletion')", name="ck_messages_observed_via"
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN "
            "('contact', 'owner_manual', 'owner_automated', 'assistant_sent')",
            name="ck_messages_direction",
        ),
        sa.CheckConstraint(
            "deleted_at IS NOT NULL OR (direction IS NOT NULL AND sent_at IS NOT NULL)",
            name="ck_messages_tombstone_or_complete",
        ),
        schema=APP,
    )
    op.create_index(
        "ix_messages_account_id_conversation_id_sent_at",
        "messages",
        ["account_id", "conversation_id", "sent_at"],
        schema=APP,
    )
    op.create_index(
        "ix_messages_content_sent_at",
        "messages",
        ["sent_at"],
        schema=APP,
        postgresql_where=sa.text("text IS NOT NULL OR media IS NOT NULL"),
    )

    op.create_table(
        "coverage_gaps",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("conversation_id", sa.BigInteger, nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("certainty", sa.Text, nullable=False),
        sa.Column("at_risk_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("at_risk_to", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("inbound_update_id", sa.BigInteger, nullable=True),
        _created("created_at"),
        _ts("resolved_at"),
        _fk("coverage_gaps", "account_id", "accounts.id", ondelete="CASCADE"),
        _fk("coverage_gaps", "conversation_id", "conversations.id", ondelete="CASCADE"),
        _fk("coverage_gaps", "inbound_update_id", "inbound_updates.id", ondelete="SET NULL"),
        sa.CheckConstraint(
            "kind IN ('outage', 'restore', 'dead_letter')", name="ck_coverage_gaps_kind"
        ),
        sa.CheckConstraint(
            "certainty IN ('delayed', 'possible', 'definite')", name="ck_coverage_gaps_certainty"
        ),
        sa.CheckConstraint("at_risk_from <= at_risk_to", name="ck_coverage_gaps_range"),
        schema=APP,
    )
    op.create_index(
        "ix_coverage_gaps_account_id_conversation_id",
        "coverage_gaps",
        ["account_id", "conversation_id"],
        schema=APP,
    )
    op.create_index(
        "uq_coverage_gaps_inbound_update_id",
        "coverage_gaps",
        ["inbound_update_id"],
        unique=True,
        schema=APP,
        postgresql_where=sa.text("inbound_update_id IS NOT NULL"),
    )

    # The raw update → stored message link (§6.5 step 6). The column has
    # existed since 0001; it gains its foreign key and an index now that the
    # target table exists.
    op.create_foreign_key(
        "fk_inbound_updates_message_id_messages",
        "inbound_updates",
        "messages",
        ["message_id"],
        ["id"],
        source_schema=APP,
        referent_schema=APP,
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_inbound_updates_message_id", "inbound_updates", ["message_id"], schema=APP
    )

    op.execute(
        "INSERT INTO app.system_state (key, value) VALUES "
        "('normalizer_version', '{\"version\": 1}'::jsonb) "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.system_state WHERE key = 'normalizer_version'")
    op.drop_index("ix_inbound_updates_message_id", table_name="inbound_updates", schema=APP)
    op.drop_constraint(
        "fk_inbound_updates_message_id_messages", "inbound_updates", schema=APP, type_="foreignkey"
    )
    for table in ("coverage_gaps", "messages", "conversations", "contacts"):
        op.drop_table(table, schema=APP)
