"""M03 S03.3: memory items (owner instructions, preferences, facts, contact notes).

- `memory_items`: scoped by foreign key (ARCHITECTURE §10.3): `scope` names
  exactly which of `contact_id` / `conversation_id` is set (neither for
  `owner`), both ON DELETE CASCADE, so deleting a contact or a conversation
  deletes its notes. `content` is CONTENT-BEARING (§10.1): kept until deleted;
  a forgotten item is `archived` (never used again) and hard-deleted by the
  retention sweep once its Undo window has passed (§10.4); managed-data
  deletion and a bot-chat purge delete contact- and conversation-scoped items.
  `provenance` holds flags only. `(run_id, tool_call_id)` is unique: a
  repeated tool call saves nothing new; `run_id` is SET NULL when the run's
  accounting row expires after a year.
- `delegation` scope and the `shareable` kind arrive with delegations (M06)
  and FR-MEM-08 (R2); `agent_inferred` with the review flow (R2).

Hand-written and reviewed (AGENTS §12). Data-handling note for the
downgrade: it drops the table and with it every stored note (content);
nothing older reads them.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

APP = "app"


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("contact_id", sa.BigInteger, nullable=True),
        sa.Column("conversation_id", sa.BigInteger, nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'owner_stated'")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "provenance", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "applies_to_replies", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column("run_id", sa.BigInteger, nullable=True),
        sa.Column("tool_call_id", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_memory_items_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            [f"{APP}.contacts.id"],
            name="fk_memory_items_contact_id_contacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            [f"{APP}.conversations.id"],
            name="fk_memory_items_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{APP}.agent_runs.id"],
            name="fk_memory_items_run_id_agent_runs",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "scope IN ('owner', 'contact', 'conversation')", name="ck_memory_items_scope"
        ),
        sa.CheckConstraint(
            "kind IN ('instruction', 'preference', 'fact', 'style')", name="ck_memory_items_kind"
        ),
        sa.CheckConstraint("source IN ('owner_stated')", name="ck_memory_items_source"),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_memory_items_status"),
        sa.CheckConstraint(
            "(scope = 'owner' AND contact_id IS NULL AND conversation_id IS NULL) "
            "OR (scope = 'contact' AND contact_id IS NOT NULL AND conversation_id IS NULL) "
            "OR (scope = 'conversation' AND conversation_id IS NOT NULL AND contact_id IS NULL)",
            name="ck_memory_items_scope_target",
        ),
        sa.CheckConstraint(
            "(status = 'archived') = (archived_at IS NOT NULL)",
            name="ck_memory_items_archived_at",
        ),
        sa.CheckConstraint(
            "NOT applies_to_replies OR kind = 'instruction'",
            name="ck_memory_items_applies_to_replies",
        ),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_memory_items_run_id_tool_call_id"),
        sa.PrimaryKeyConstraint("id", name="pk_memory_items"),
        schema=APP,
    )
    op.create_index(
        "ix_memory_items_account_id_status", "memory_items", ["account_id", "status"], schema=APP
    )
    op.create_index("ix_memory_items_contact_id", "memory_items", ["contact_id"], schema=APP)
    op.create_index(
        "ix_memory_items_conversation_id", "memory_items", ["conversation_id"], schema=APP
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_conversation_id", "memory_items", schema=APP)
    op.drop_index("ix_memory_items_contact_id", "memory_items", schema=APP)
    op.drop_index("ix_memory_items_account_id_status", "memory_items", schema=APP)
    op.drop_table("memory_items", schema=APP)
