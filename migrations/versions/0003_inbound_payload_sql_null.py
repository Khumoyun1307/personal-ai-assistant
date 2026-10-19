"""Cleared inbound payloads are SQL NULL, not JSON null (data migration).

Until this revision, clearing `inbound_updates.payload` through the ORM stored
a JSON `null` value instead of SQL NULL (SQLAlchemy's JSON default). No
content survived either way, but `payload IS NULL` checks — the retention
sweep, audits — did not see those rows as empty. The model now uses
`JSONB(none_as_null=True)`; this revision rewrites the rows already stored.

Data-handling note (AGENTS §12): no content is changed or exposed; a JSON
`null` becomes SQL NULL. The downgrade is a deliberate no-op: SQL NULL is the
correct state for either code version.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE app.inbound_updates SET payload = NULL WHERE payload = 'null'::jsonb")


def downgrade() -> None:
    # Nothing to undo: SQL NULL is valid, and correct, for the older code too.
    pass
