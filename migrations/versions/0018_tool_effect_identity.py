"""Unique tenant-scoped tool effect keys (C2 / stabilization H3).

Index only: no new content or copies of tool arguments. Downgrade removes
the uniqueness guard without deleting audit records or action bodies.
Control-turn reply/provenance columns belong exclusively to revision 0016;
neither direction of this migration changes them.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-09
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

# The unique index constrains existing audit_log fields. Under I23, constraints
# on existing storage are conservatively breaking, even for a filtered event.
COMPATIBILITY = "breaking"


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX uq_audit_log_tool_effect_key
        ON app.audit_log (account_id, (details ->> 'run_id'), (details ->> 'tool_call_id'))
        WHERE event = 'tool_effect_recorded'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.uq_audit_log_tool_effect_key")
