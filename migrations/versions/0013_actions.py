"""P2 F0: owner-voice actions, send audit and drafting trigger.

Hand-written and reviewed. CONTENT: actions.text follows message-content
retention and all privacy deletion scopes. Data-handling note: downgrade
deletes every action and its owner-voice text; message records survive.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None
APP = "app"


def _fk(table: str, column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{APP}.{target}"],
        name=f"fk_{table}_{column}_{target.split('.')[0]}",
        ondelete=ondelete,
    )


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "actions",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("conversation_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("state_reason", sa.Text, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("text", sa.Text, nullable=True),
        sa.Column("approved_text_hash", sa.Text, nullable=True),
        sa.Column("sent_text_hash", sa.Text, nullable=True),
        sa.Column("base_message_id", sa.BigInteger, nullable=True),
        sa.Column("shown_through_message_id", sa.BigInteger, nullable=True),
        sa.Column("target_message_id", sa.BigInteger, nullable=True),
        sa.Column("run_id", sa.BigInteger, nullable=True),
        sa.Column("tool_call_id", sa.Text, nullable=True),
        sa.Column("draft_run_id", sa.BigInteger, nullable=True),
        sa.Column("trigger_turn_id", sa.BigInteger, nullable=True),
        sa.Column("revision_turn_id", sa.BigInteger, nullable=True),
        sa.Column("draft_flags", postgresql.JSONB, nullable=True),
        sa.Column("policy_decision", postgresql.JSONB, nullable=True),
        sa.Column("approval_token", sa.Text, nullable=True),
        sa.Column("card_message_id", sa.BigInteger, nullable=True),
        sa.Column("card_state", sa.Text, nullable=True),
        _ts("expires_at"),
        sa.Column("attempt_id", sa.Text, nullable=True),
        _ts("attempt_started_at"),
        _ts("retry_at"),
        sa.Column("telegram_message_id", sa.BigInteger, nullable=True),
        sa.Column("identity_pending", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("error_class", sa.Text, nullable=True),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        _ts("decided_at"),
        _ts("executed_at"),
        _fk("actions", "account_id", "accounts.id", ondelete="CASCADE"),
        _fk("actions", "conversation_id", "conversations.id", ondelete="CASCADE"),
        _fk("actions", "base_message_id", "messages.id", ondelete="SET NULL"),
        _fk("actions", "target_message_id", "messages.id", ondelete="SET NULL"),
        _fk("actions", "run_id", "agent_runs.id", ondelete="SET NULL"),
        _fk("actions", "draft_run_id", "agent_runs.id", ondelete="SET NULL"),
        _fk("actions", "trigger_turn_id", "control_turns.id", ondelete="SET NULL"),
        _fk("actions", "revision_turn_id", "control_turns.id", ondelete="SET NULL"),
        sa.CheckConstraint(
            "kind IN ('send_message','edit_message','delete_own')", name="ck_actions_kind"
        ),
        sa.CheckConstraint(
            "origin IN ('owner_dictated','agent_proposal','delegation_auto')",
            name="ck_actions_origin",
        ),
        sa.CheckConstraint(
            "state IN ('proposed','awaiting_approval','revising','approved','scheduled',"
            "'executing','succeeded','failed','delivery_uncertain','rejected','outdated',"
            "'expired','cancelled','blocked')",
            name="ck_actions_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_actions_version"),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_actions_run_id_tool_call_id"),
        sa.UniqueConstraint("approval_token", name="uq_actions_approval_token"),
        schema=APP,
    )
    op.create_index(
        "ux_actions_live_card",
        "actions",
        ["conversation_id"],
        unique=True,
        schema=APP,
        postgresql_where=sa.text("state IN ('awaiting_approval','revising')"),
    )
    op.create_index(
        "ix_actions_account_id_created_at", "actions", ["account_id", "created_at"], schema=APP
    )
    op.create_index(
        "ix_actions_conversation_id_state", "actions", ["conversation_id", "state"], schema=APP
    )
    op.create_index(
        "ix_actions_inflight",
        "actions",
        ["state", "attempt_started_at"],
        schema=APP,
        postgresql_where=sa.text("state IN ('approved','executing','delivery_uncertain')"),
    )
    op.create_index(
        "ix_actions_expiring",
        "actions",
        ["expires_at"],
        schema=APP,
        postgresql_where=sa.text("state IN ('awaiting_approval','revising','approved')"),
    )
    op.create_index(
        "ix_actions_trigger_turn_id_conversation_id",
        "actions",
        ["trigger_turn_id", "conversation_id"],
        schema=APP,
        postgresql_where=sa.text("trigger_turn_id IS NOT NULL"),
    )
    op.create_index(
        "ix_actions_card_unsynced",
        "actions",
        ["account_id"],
        schema=APP,
        postgresql_where=sa.text(
            "card_message_id IS NOT NULL AND card_state IS DISTINCT FROM state"
        ),
    )
    op.add_column("messages", sa.Column("action_id", sa.BigInteger, nullable=True), schema=APP)
    op.create_foreign_key(
        "fk_messages_action_id_actions",
        "messages",
        "actions",
        ["action_id"],
        ["id"],
        source_schema=APP,
        referent_schema=APP,
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_messages_action_id",
        "messages",
        ["action_id"],
        schema=APP,
        postgresql_where=sa.text("action_id IS NOT NULL"),
    )
    op.add_column("conversations", _ts("pending_draft_at"), schema=APP)


def downgrade() -> None:
    op.drop_column("conversations", "pending_draft_at", schema=APP)
    op.drop_index("ix_messages_action_id", "messages", schema=APP)
    op.drop_constraint("fk_messages_action_id_actions", "messages", type_="foreignkey", schema=APP)
    op.drop_column("messages", "action_id", schema=APP)
    op.drop_table("actions", schema=APP)
