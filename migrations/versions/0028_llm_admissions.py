"""Priced call admission: one structural row per provider attempt (I16, C7).

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

# A new, content-free table; older executables neither read nor write it.
COMPATIBILITY = "expand"

_STATUSES = "('reserved', 'dispatched', 'completed', 'released', 'billing_uncertain')"
_ERROR_CODES = (
    "('not_dispatched', 'rate_limited', 'auth', 'bad_request', 'provider_error', "
    "'timeout', 'connection', 'invalid_output', 'interrupted', 'stale_dispatch', "
    "'bound_exceeded', 'bound_exceeded_reviewed')"
)


def upgrade() -> None:
    usd = sa.Numeric(18, 9)
    op.create_table(
        "llm_admissions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("llm_call_id", sa.BigInteger(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("price_version", sa.Text(), nullable=False),
        sa.Column("month_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("input_bound", sa.BigInteger(), nullable=False),
        sa.Column("output_bound", sa.BigInteger(), nullable=False),
        sa.Column("reserved_usd", usd, nullable=False),
        sa.Column("charged_usd", usd, nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_admissions"),
        # Accounting exception to the CASCADE convention: account removal is
        # blocked while admissions remain (C7 retention).
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["app.accounts.id"],
            name="fk_llm_admissions_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["app.agent_runs.id"],
            name="fk_llm_admissions_run_id_agent_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id"],
            ["app.llm_calls.id"],
            name="fk_llm_admissions_llm_call_id_llm_calls",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("account_id", "attempt_id", name="uq_llm_admissions_account_attempt"),
        sa.CheckConstraint(f"status IN {_STATUSES}", name="ck_llm_admissions_status"),
        sa.CheckConstraint(
            f"error_code IS NULL OR error_code IN {_ERROR_CODES}",
            name="ck_llm_admissions_error_code",
        ),
        sa.CheckConstraint(
            "input_bound >= 0 AND output_bound >= 0", name="ck_llm_admissions_bounds"
        ),
        sa.CheckConstraint(
            "reserved_usd >= 0 AND (charged_usd IS NULL OR charged_usd >= 0)",
            name="ck_llm_admissions_usd",
        ),
        schema="app",
    )
    op.create_index(
        "ix_llm_admissions_account_id_month_start",
        "llm_admissions",
        ["account_id", "month_start"],
        schema="app",
    )
    op.create_index(
        "ix_llm_admissions_status_updated_at",
        "llm_admissions",
        ["status", "updated_at"],
        schema="app",
    )
    # ON DELETE SET NULL from agent_runs: the year-old run sweep finds rows by it.
    op.create_index(
        "ix_llm_admissions_run_id",
        "llm_admissions",
        ["run_id"],
        schema="app",
    )
    # Settlement links one llm_calls row; spend excludes linked rows by it.
    op.create_index(
        "ix_llm_admissions_llm_call_id",
        "llm_admissions",
        ["llm_call_id"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("llm_admissions", schema="app")
