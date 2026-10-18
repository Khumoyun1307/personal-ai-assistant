"""M01 S01.1 baseline: tenant root, inbox, control chat, agent accounting.

Hand-written and reviewed, not raw autogenerate output (AGENTS §12).

Revision ID: 0001
Revises:
Create Date: 2026-10-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP = "app"

#: Reused column definitions, so every table agrees on them.
_PK = sa.Column(
    "id", sa.BigInteger, sa.Identity(always=True), primary_key=True, nullable=False
)


def _created(name: str = "created_at") -> sa.Column:
    return sa.Column(name, sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False)


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{APP}"')

    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'onboarding'")),
        # NULL until the owner presses Start; the bot cannot reach them before
        # that at all (TF §12 LV-09).
        sa.Column("control_chat_id", sa.BigInteger, nullable=True),
        sa.Column("timezone", sa.Text, nullable=False, server_default=sa.text("'UTC'")),
        sa.Column("locale_pref", sa.Text, nullable=True),
        sa.Column("settings", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("autonomy_paused", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _created(),
        sa.UniqueConstraint("telegram_user_id", name="uq_accounts_telegram_user_id"),
        sa.CheckConstraint(
            "status IN ('onboarding', 'active', 'blocked', 'deleted')",
            name="ck_accounts_status",
        ),
        schema=APP,
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("run_type", sa.Text, nullable=False),
        sa.Column("trigger_ref", sa.Text, nullable=True),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("effort", sa.Text, nullable=True),
        sa.Column("prompt_version", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'running'")),
        sa.Column("iterations", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("tainted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _created("started_at"),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_class", sa.Text, nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_agent_runs_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "run_type IN ('owner_request', 'triage', 'draft_reply', 'summarize', "
            "'extract_memory', 'evaluate_monitor', 'interpret_instruction')",
            name="ck_agent_runs_run_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'refused', 'budget_exceeded')",
            name="ck_agent_runs_status",
        ),
        schema=APP,
    )
    op.create_index(
        "ix_agent_runs_account_id_started_at",
        "agent_runs",
        ["account_id", "started_at"],
        schema=APP,
    )

    op.create_table(
        "consents",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("policy_version", sa.Text, nullable=False),
        sa.Column("processors", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        _created("granted_at"),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_consents_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("kind IN ('ai_control', 'ai_managed')", name="ck_consents_kind"),
        schema=APP,
    )
    op.create_index("ix_consents_account_id_kind", "consents", ["account_id", "kind"], schema=APP)

    op.create_table(
        "control_turns",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger, nullable=True),
        # CONTENT-BEARING (ARCHITECTURE §10.1); retention §10.4.
        sa.Column("text", sa.Text, nullable=True),
        sa.Column(
            "contains_managed", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column("refs", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("run_id", sa.BigInteger, nullable=True),
        _created(),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_control_turns_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{APP}.agent_runs.id"],
            name="fk_control_turns_run_id_agent_runs",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("role IN ('owner', 'assistant')", name="ck_control_turns_role"),
        schema=APP,
    )
    op.create_index(
        "ix_control_turns_account_id_created_at",
        "control_turns",
        ["account_id", "created_at"],
        schema=APP,
    )

    op.create_table(
        "agent_run_steps",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("run_id", sa.BigInteger, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("tool_name", sa.Text, nullable=False),
        sa.Column("args_digest", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        _created(),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{APP}.agent_runs.id"],
            name="fk_agent_run_steps_run_id_agent_runs",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_run_steps_run_id_seq"),
        schema=APP,
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("run_id", sa.BigInteger, nullable=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cache_read_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cache_write_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cost_estimate", sa.Numeric(12, 6), nullable=True),
        sa.Column("request_id", sa.Text, nullable=True),
        _created(),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_llm_calls_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{APP}.agent_runs.id"],
            name="fk_llm_calls_run_id_agent_runs",
            ondelete="SET NULL",
        ),
        schema=APP,
    )
    op.create_index(
        "ix_llm_calls_account_id_created_at",
        "llm_calls",
        ["account_id", "created_at"],
        schema=APP,
    )

    op.create_table(
        "inbound_updates",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("bot_id", sa.BigInteger, nullable=False),
        sa.Column("update_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        # Nullable by design: an update arrives before we know whose it is, and
        # non-allowlisted senders never get one (§6 invariant 12).
        sa.Column("account_id", sa.BigInteger, nullable=True),
        sa.Column("message_id", sa.BigInteger, nullable=True),
        # CONTENT-BEARING (ARCHITECTURE §10.1); cleared at processing or ≤7 d.
        sa.Column("payload", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error_code", sa.Text, nullable=True),
        _created("received_at"),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_inbound_updates_account_id_accounts",
            ondelete="SET NULL",
        ),
        # The idempotency guarantee: a redelivered update is a no-op.
        sa.UniqueConstraint("bot_id", "update_id", name="uq_inbound_updates_bot_id_update_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'processed', 'ignored', 'rejected', 'failed')",
            name="ck_inbound_updates_status",
        ),
        schema=APP,
    )
    # Partial index: the claim query only ever looks at pending rows.
    op.create_index(
        "ix_inbound_updates_pending",
        "inbound_updates",
        ["received_at"],
        postgresql_where=sa.text("status = 'pending'"),
        schema=APP,
    )
    op.create_index(
        "ix_inbound_updates_account_id", "inbound_updates", ["account_id"], schema=APP
    )

    op.create_table(
        "pending_inputs",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("ref_id", sa.BigInteger, nullable=True),
        sa.Column("prompt_message_id", sa.BigInteger, nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        _created(),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_pending_inputs_account_id_accounts",
            ondelete="CASCADE",
        ),
        schema=APP,
    )
    op.create_index(
        "ix_pending_inputs_account_id_kind", "pending_inputs", ["account_id", "kind"], schema=APP
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        _created("at"),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("subject_type", sa.Text, nullable=True),
        sa.Column("subject_id", sa.BigInteger, nullable=True),
        sa.Column("details", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_audit_log_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("actor IN ('owner', 'agent', 'system')", name="ck_audit_log_actor"),
        schema=APP,
    )
    op.create_index("ix_audit_log_account_id_at", "audit_log", ["account_id", "at"], schema=APP)

    # Deployment state, deliberately not tenant-scoped.
    op.create_table(
        "system_state",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        _created("updated_at"),
        schema=APP,
    )


def downgrade() -> None:
    for table in (
        "system_state",
        "audit_log",
        "pending_inputs",
        "inbound_updates",
        "llm_calls",
        "agent_run_steps",
        "control_turns",
        "consents",
        "agent_runs",
        "accounts",
    ):
        op.drop_table(table, schema=APP)
    # The `app` schema itself is NOT dropped: alembic's own version table lives
    # in it, and dropping it here would delete the row alembic is about to
    # update, failing the downgrade. The schema is created by env.py as
    # bootstrap, so leaving it empty is the correct end state.
