"""P2 F0: inbox intelligence and automation storage.

Hand-written and reviewed. CONTENT: tasks.title/details,
conversation_triage.reasons and automations.spec have registry, retention
and deletion coverage. Notifications contain only references and enums.
Data-handling note: downgrade drops all task text, triage reasons and
automation specifications, as well as their metadata and occurrence ledger.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None
APP = "app"


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True)


def _ts(name: str, *, nullable: bool = True, created: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.TIMESTAMP(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if created else None,
    )


def _fk(table: str, column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{APP}.{target}"],
        name=f"fk_{table}_{column}_{target.split('.')[0]}",
        ondelete=ondelete,
    )


def _check(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    choices = ",".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({choices})", name=f"ck_{table}_{column}")


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("priority", sa.Text, nullable=False, server_default=sa.text("'normal'")),
        schema=APP,
    )
    op.create_check_constraint(
        "ck_contacts_priority", "contacts", "priority IN ('vip','normal','low')", schema=APP
    )
    op.add_column("conversations", _ts("pending_triage_at"), schema=APP)
    op.add_column("conversations", _ts("pending_monitor_at"), schema=APP)
    op.create_table(
        "tasks",
        _id(),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("conversation_id", sa.BigInteger, nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("details", sa.Text, nullable=True),
        _ts("due_at"),
        _ts("snoozed_until"),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("closed_reason", sa.Text, nullable=True),
        sa.Column(
            "source_message_ids", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column("first_source_message_id", sa.BigInteger, nullable=True),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("run_id", sa.BigInteger, nullable=True),
        _ts("created_at", nullable=False, created=True),
        _ts("updated_at", nullable=False, created=True),
        _ts("closed_at"),
        _fk("tasks", "account_id", "accounts.id", ondelete="CASCADE"),
        _fk("tasks", "conversation_id", "conversations.id", ondelete="CASCADE"),
        _fk("tasks", "run_id", "agent_runs.id", ondelete="SET NULL"),
        _check(
            "tasks",
            "kind",
            (
                "owner_commitment",
                "contact_commitment",
                "open_question",
                "follow_up",
                "reminder",
                "decision",
            ),
        ),
        _check("tasks", "status", ("open", "done", "cancelled", "expired")),
        _check("tasks", "created_by", ("owner", "agent")),
        schema=APP,
    )
    op.create_index(
        "ux_tasks_agent_dedupe",
        "tasks",
        ["conversation_id", "kind", "first_source_message_id"],
        unique=True,
        schema=APP,
        postgresql_where=sa.text("created_by = 'agent'"),
    )
    op.create_index(
        "ix_tasks_account_id_status_due_at", "tasks", ["account_id", "status", "due_at"], schema=APP
    )
    op.create_index(
        "ix_tasks_conversation_id_status", "tasks", ["conversation_id", "status"], schema=APP
    )
    op.create_table(
        "conversation_triage",
        _id(),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("conversation_id", sa.BigInteger, nullable=False),
        sa.Column("covers_through_message_id", sa.BigInteger, nullable=False),
        sa.Column("needs_reply", sa.Boolean, nullable=False),
        sa.Column("contains_question", sa.Boolean, nullable=False),
        sa.Column("requests_decision", sa.Boolean, nullable=False),
        sa.Column("urgency", sa.Text, nullable=False),
        _ts("deadline_at"),
        sa.Column("escalation_category", sa.Text, nullable=False),
        sa.Column("reasons", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("language", sa.Text, nullable=True),
        sa.Column("script", sa.Text, nullable=True),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("run_id", sa.BigInteger, nullable=True),
        _ts("created_at", nullable=False, created=True),
        _ts("updated_at", nullable=False, created=True),
        _fk("conversation_triage", "account_id", "accounts.id", ondelete="CASCADE"),
        _fk("conversation_triage", "conversation_id", "conversations.id", ondelete="CASCADE"),
        _fk("conversation_triage", "run_id", "agent_runs.id", ondelete="SET NULL"),
        sa.UniqueConstraint("conversation_id", name="uq_conversation_triage_conversation_id"),
        _check("conversation_triage", "urgency", ("low", "normal", "high", "urgent")),
        _check(
            "conversation_triage",
            "escalation_category",
            ("none", "urgent_decision", "needs_owner", "fyi", "suspicious"),
        ),
        _check("conversation_triage", "script", ("latin", "cyrillic", "mixed", "other")),
        schema=APP,
    )
    op.create_index(
        "ix_conversation_triage_account_id", "conversation_triage", ["account_id"], schema=APP
    )
    op.create_table(
        "automations",
        _id(),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("task_id", sa.BigInteger, nullable=True),
        sa.Column("spec", postgresql.JSONB, nullable=False),
        sa.Column("refs", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
        sa.Column("rrule", sa.Text, nullable=True),
        _ts("run_at"),
        sa.Column("timezone", sa.Text, nullable=False),
        sa.Column("tz_policy", sa.Text, nullable=False),
        _ts("next_run_at"),
        _ts("last_run_at"),
        _ts("expires_at"),
        _ts("confirmed_at"),
        sa.Column("run_id", sa.BigInteger, nullable=True),
        sa.Column("tool_call_id", sa.Text, nullable=True),
        _ts("created_at", nullable=False, created=True),
        _ts("updated_at", nullable=False, created=True),
        _ts("ended_at"),
        _fk("automations", "account_id", "accounts.id", ondelete="CASCADE"),
        _fk("automations", "task_id", "tasks.id", ondelete="SET NULL"),
        _fk("automations", "run_id", "agent_runs.id", ondelete="SET NULL"),
        _check(
            "automations",
            "kind",
            ("reminder", "monitor", "follow_up", "scheduled_summary", "delegation_end", "digest"),
        ),
        _check("automations", "status", ("active", "paused", "completed", "cancelled", "expired")),
        _check("automations", "tz_policy", ("follow_owner", "fixed")),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_automations_run_id_tool_call_id"),
        schema=APP,
    )
    op.create_index(
        "ix_automations_due",
        "automations",
        ["next_run_at"],
        schema=APP,
        postgresql_where=sa.text("status = 'active' AND next_run_at IS NOT NULL"),
    )
    op.create_index(
        "ix_automations_account_id_kind_status",
        "automations",
        ["account_id", "kind", "status"],
        schema=APP,
    )
    op.create_table(
        "automation_occurrences",
        _id(),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("automation_id", sa.BigInteger, nullable=False),
        _ts("occurrence_at", nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("note_code", sa.Text, nullable=True),
        _ts("fired_at"),
        _ts("created_at", nullable=False, created=True),
        _fk("automation_occurrences", "account_id", "accounts.id", ondelete="CASCADE"),
        _fk("automation_occurrences", "automation_id", "automations.id", ondelete="CASCADE"),
        _check("automation_occurrences", "status", ("claimed", "fired", "skipped")),
        sa.UniqueConstraint(
            "automation_id",
            "occurrence_at",
            name="uq_automation_occurrences_automation_id_occurrence_at",
        ),
        schema=APP,
    )
    op.create_index(
        "ix_automation_occurrences_account_id", "automation_occurrences", ["account_id"], schema=APP
    )
    op.create_table(
        "digests",
        _id(),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        _ts("period_start", nullable=False),
        _ts("period_end", nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("message_id", sa.BigInteger, nullable=True),
        _ts("sent_at"),
        _ts("created_at", nullable=False, created=True),
        _ts("updated_at", nullable=False, created=True),
        _fk("digests", "account_id", "accounts.id", ondelete="CASCADE"),
        _check("digests", "status", ("building", "sent", "failed", "uncertain")),
        sa.UniqueConstraint(
            "account_id",
            "period_start",
            "period_end",
            name="uq_digests_account_id_period_start_period_end",
        ),
        schema=APP,
    )
    op.create_table(
        "notifications",
        _id(),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("alert_class", sa.Text, nullable=False, server_default=sa.text("'standard'")),
        sa.Column("priority", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'queued'")),
        sa.Column("dedupe_key", sa.Text, nullable=False),
        sa.Column("refs", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("conversation_id", sa.BigInteger, nullable=True),
        sa.Column("contact_id", sa.BigInteger, nullable=True),
        sa.Column("reason_code", sa.Text, nullable=True),
        sa.Column(
            "bypass_quiet_hours", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        _ts("deliver_after"),
        sa.Column("digest_id", sa.BigInteger, nullable=True),
        sa.Column("message_id", sa.BigInteger, nullable=True),
        sa.Column("feedback", sa.Text, nullable=True),
        _ts("created_at", nullable=False, created=True),
        _ts("updated_at", nullable=False, created=True),
        _ts("sent_at"),
        _fk("notifications", "account_id", "accounts.id", ondelete="CASCADE"),
        _fk("notifications", "conversation_id", "conversations.id", ondelete="SET NULL"),
        _fk("notifications", "contact_id", "contacts.id", ondelete="SET NULL"),
        _fk("notifications", "digest_id", "digests.id", ondelete="SET NULL"),
        _check("notifications", "alert_class", ("system", "standard")),
        _check("notifications", "priority", ("low", "normal", "high", "urgent")),
        _check(
            "notifications",
            "status",
            ("queued", "sent", "suppressed", "batched", "failed", "uncertain"),
        ),
        _check("notifications", "feedback", ("useful", "not_useful")),
        sa.UniqueConstraint(
            "account_id", "dedupe_key", name="uq_notifications_account_id_dedupe_key"
        ),
        schema=APP,
    )
    op.create_index(
        "ix_notifications_account_id_status_deliver_after",
        "notifications",
        ["account_id", "status", "deliver_after"],
        schema=APP,
    )


def downgrade() -> None:
    for table in (
        "notifications",
        "digests",
        "automation_occurrences",
        "automations",
        "conversation_triage",
        "tasks",
    ):
        op.drop_table(table, schema=APP)
    op.drop_column("conversations", "pending_monitor_at", schema=APP)
    op.drop_column("conversations", "pending_triage_at", schema=APP)
    op.drop_constraint("ck_contacts_priority", "contacts", type_="check", schema=APP)
    op.drop_column("contacts", "priority", schema=APP)
