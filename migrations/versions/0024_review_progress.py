"""Review coverage cursor and verdict linkage (C7, I2).

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# New structural table plus nullable verdict columns, and a BEFORE DELETE trigger
# that keeps the verdict's paired metadata consistent. Old executables never read
# or write them and a verdict they store has NULL linkage (unproven coverage).
COMPATIBILITY = "expand"

APP = "app"


def upgrade() -> None:
    op.create_table(
        "review_progress",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("reader", sa.Text(), nullable=False),
        sa.Column("monitor_id", sa.BigInteger(), nullable=True),
        sa.Column("scope_key", sa.Text(), server_default="rolling", nullable=False),
        sa.Column("requested_range", postgresql.JSONB(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("position", postgresql.JSONB(), nullable=True),
        sa.Column("source_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "pending_ranges",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_review_progress_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            [f"{APP}.conversations.id"],
            name="fk_review_progress_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["monitor_id"],
            [f"{APP}.automations.id"],
            name="fk_review_progress_monitor_id_automations",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "reader IN ('summary', 'triage', 'monitor')", name="ck_review_progress_reader"
        ),
        sa.CheckConstraint(
            "(reader = 'monitor') = (monitor_id IS NOT NULL)", name="ck_review_progress_monitor_id"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'current', 'partial', 'degraded', 'blocked')",
            name="ck_review_progress_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_review_progress_version"),
        sa.CheckConstraint("attempts >= 0", name="ck_review_progress_attempts"),
        sa.CheckConstraint("source_revision >= 0", name="ck_review_progress_source_revision"),
        sa.CheckConstraint(
            "last_error IS NULL OR last_error IN ('backlog', 'partial_text', 'source_changed', "
            "'deleted', 'retention', 'provider_failed', 'invalid_output', 'budget', "
            "'no_consent', 'refusal', 'truncated')",
            name="ck_review_progress_last_error",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_progress"),
        schema=APP,
    )
    op.create_index("ix_review_progress_account_id", "review_progress", ["account_id"], schema=APP)
    op.create_index(
        "uq_review_progress_scope",
        "review_progress",
        ["account_id", "conversation_id", "reader", "monitor_id", "scope_key"],
        unique=True,
        schema=APP,
        postgresql_nulls_not_distinct=True,
    )
    op.add_column(
        "conversation_triage", sa.Column("review_progress_id", sa.BigInteger()), schema=APP
    )
    op.add_column("conversation_triage", sa.Column("review_version", sa.Integer()), schema=APP)
    op.create_foreign_key(
        "fk_conversation_triage_review_progress_id_review_progress",
        "conversation_triage",
        "review_progress",
        ["review_progress_id"],
        ["id"],
        source_schema=APP,
        referent_schema=APP,
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_conversation_triage_review_link",
        "conversation_triage",
        "(review_progress_id IS NULL) = (review_version IS NULL)",
        schema=APP,
    )
    # ON DELETE SET NULL on the id alone would violate the paired-null check, so
    # every deletion path (sweep, scoped erasure, cascades) clears both fields on
    # the retained verdict first, atomically with the progress row's removal.
    op.execute("""
        CREATE FUNCTION app.clear_deleted_review_link() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE app.conversation_triage
            SET review_progress_id = NULL, review_version = NULL
            WHERE account_id = OLD.account_id AND review_progress_id = OLD.id;
            RETURN OLD;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER clear_deleted_review_link
        BEFORE DELETE ON app.review_progress
        FOR EACH ROW EXECUTE FUNCTION app.clear_deleted_review_link()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER clear_deleted_review_link ON app.review_progress")
    op.execute("DROP FUNCTION app.clear_deleted_review_link()")
    op.drop_constraint(
        "ck_conversation_triage_review_link", "conversation_triage", type_="check", schema=APP
    )
    op.drop_constraint(
        "fk_conversation_triage_review_progress_id_review_progress",
        "conversation_triage",
        type_="foreignkey",
        schema=APP,
    )
    op.drop_column("conversation_triage", "review_version", schema=APP)
    op.drop_column("conversation_triage", "review_progress_id", schema=APP)
    op.drop_index("uq_review_progress_scope", "review_progress", schema=APP)
    op.drop_index("ix_review_progress_account_id", "review_progress", schema=APP)
    op.drop_table("review_progress", schema=APP)
