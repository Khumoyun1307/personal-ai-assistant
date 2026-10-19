"""M02 S02.1: business connection registry.

`business_connections` (one per account, keyed by account because Telegram's
connection id is mutable), `business_connection_events` (history: id rotation,
rights, enablement — content-free) and `foreign_connections` (non-tenant cache
of connections from non-allowlisted users, metadata only).

Hand-written and reviewed (AGENTS §12). No content-bearing column is added.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP = "app"


def _created(name: str) -> sa.Column:
    return sa.Column(
        name, sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _account_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["account_id"],
        [f"{APP}.accounts.id"],
        name=f"fk_{table}_account_id_accounts",
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "business_connections",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("connection_id", sa.Text, nullable=False),
        sa.Column("is_enabled", sa.Boolean, nullable=False),
        sa.Column("rights", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("capability_tier", sa.Text, nullable=False),
        sa.Column("user_chat_id", sa.BigInteger, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("connected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        _created("updated_at"),
        _account_fk("business_connections"),
        sa.UniqueConstraint("account_id", name="uq_business_connections_account_id"),
        sa.CheckConstraint(
            "status IN ('pending_owner', 'active', 'disabled', 'stale')",
            name="ck_business_connections_status",
        ),
        sa.CheckConstraint(
            "capability_tier IN ('none', 'observe_only', 'can_reply', 'extended')",
            name="ck_business_connections_capability_tier",
        ),
        schema=APP,
    )
    op.create_index(
        "ix_business_connections_connection_id",
        "business_connections",
        ["connection_id"],
        schema=APP,
    )

    op.create_table(
        "business_connection_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("connection_id", sa.Text, nullable=False),
        sa.Column("payload_digest", sa.Text, nullable=False),
        sa.Column("rights", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("is_enabled", sa.Boolean, nullable=False),
        sa.Column("change", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        _created("observed_at"),
        _account_fk("business_connection_events"),
        sa.CheckConstraint(
            "source IN ('update', 'bootstrap', 'onboarding')",
            name="ck_business_connection_events_source",
        ),
        schema=APP,
    )
    op.create_index(
        "ix_business_connection_events_account_id",
        "business_connection_events",
        ["account_id"],
        schema=APP,
    )
    op.create_index(
        "ix_business_connection_events_connection_id",
        "business_connection_events",
        ["connection_id"],
        schema=APP,
    )

    op.create_table(
        "foreign_connections",
        sa.Column("connection_id", sa.Text, primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger, nullable=False),
        _created("first_seen_at"),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        schema=APP,
    )


def downgrade() -> None:
    for table in ("foreign_connections", "business_connection_events", "business_connections"):
        op.drop_table(table, schema=APP)
