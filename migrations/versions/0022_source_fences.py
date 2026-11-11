"""Source revision and generation fences (C6, H10).

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


# New structural metadata only. Old writers remain valid; the runtime fails closed
# for autonomous sends without proof. No content columns or journal changes.
COMPATIBILITY = "expand"


def upgrade() -> None:
    op.add_column("actions", sa.Column("freshness_warning", sa.Text()), schema="app")
    op.add_column(
        "actions",
        sa.Column("freshness_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema="app",
    )
    for name in ("content_revision", "owner_intent_revision"):
        op.add_column(
            "conversations",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
            schema="app",
        )
        op.create_check_constraint(
            op.f(f"ck_conversations_{name}"), "conversations", f"{name} >= 0", schema="app"
        )
    op.create_table(
        "context_fences",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["account_id"], ["app.accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["app.agent_runs.id"], ondelete="SET NULL"),
        schema="app",
    )
    op.create_index("ix_context_fences_account_id", "context_fences", ["account_id"], schema="app")
    op.add_column("actions", sa.Column("context_fence_id", sa.BigInteger()), schema="app")
    op.add_column("actions", sa.Column("requested_at", sa.DateTime(timezone=True)), schema="app")
    op.add_column("actions", sa.Column("owner_request_id", sa.BigInteger()), schema="app")
    op.create_foreign_key(
        "fk_actions_owner_request_id_owner_requests",
        "actions",
        "owner_requests",
        ["owner_request_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_actions_context_fence_id_context_fences",
        "actions",
        "context_fences",
        ["context_fence_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_column("actions", "freshness_override", schema="app")
    op.drop_column("actions", "freshness_warning", schema="app")
    op.drop_constraint("fk_actions_owner_request_id_owner_requests", "actions", schema="app")
    op.drop_column("actions", "owner_request_id", schema="app")
    op.drop_constraint("fk_actions_context_fence_id_context_fences", "actions", schema="app")
    op.drop_column("actions", "requested_at", schema="app")
    op.drop_column("actions", "context_fence_id", schema="app")
    op.drop_table("context_fences", schema="app")
    for name in ("owner_intent_revision", "content_revision"):
        op.drop_constraint(
            op.f(f"ck_conversations_{name}"), "conversations", type_="check", schema="app"
        )
        op.drop_column("conversations", name, schema="app")
