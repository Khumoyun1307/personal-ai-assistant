"""M03 S03.1: third-party control turns and tool confirmations.

- `control_turns.third_party` (bool, default false): the owner's message
  carried forwarded or quoted third-party text (taint, ARCHITECTURE §7.1);
- `control_turns.quoted_text` (text, NULL): the quoted or replied-to text —
  CONTENT-BEARING (§10.1), cleared by the control-turn retention sweep and
  by managed-data deletion together with `text`;
- `tool_confirmations`: a model-initiated state change in a tainted run,
  held for the owner's tap. `args` is CONTENT-BEARING and nulled once the
  call is decided, applied or expired (§10.4). One row per
  `(run_id, tool_call_id)`, so a retried call never makes a second card.

Hand-written and reviewed (AGENTS §12). Downgrade is symmetric and drops the
new content columns (no data-handling concern: nothing older reads them).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP = "app"
STATUSES = "('pending', 'confirmed', 'applied', 'failed', 'rejected', 'expired')"


def upgrade() -> None:
    op.add_column(
        "control_turns",
        sa.Column("third_party", sa.Boolean, nullable=False, server_default=sa.text("false")),
        schema=APP,
    )
    op.add_column("control_turns", sa.Column("quoted_text", sa.Text, nullable=True), schema=APP)

    op.create_table(
        "tool_confirmations",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("run_id", sa.BigInteger, nullable=True),
        sa.Column("tool_call_id", sa.Text, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("args", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_tool_confirmations_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{APP}.agent_runs.id"],
            name="fk_tool_confirmations_run_id_agent_runs",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_tool_confirmations_run_id_call_id"),
        sa.CheckConstraint(f"status IN {STATUSES}", name="ck_tool_confirmations_status"),
        schema=APP,
    )
    op.create_index(
        "ix_tool_confirmations_account_id_status",
        "tool_confirmations",
        ["account_id", "status"],
        schema=APP,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_confirmations_account_id_status", table_name="tool_confirmations", schema=APP
    )
    op.drop_table("tool_confirmations", schema=APP)
    op.drop_column("control_turns", "quoted_text", schema=APP)
    op.drop_column("control_turns", "third_party", schema=APP)
