"""Keep run invalidation after source fence erasure.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

# The column is structural, but the delete trigger changes existing-table
# behavior. Older executables do not check this marker before staging content.
COMPATIBILITY = "breaking"


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("sources_invalidated", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema="app",
    )
    op.execute("""
        CREATE FUNCTION app.invalidate_erased_fence_run() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE app.agent_runs SET sources_invalidated = true
            WHERE account_id = OLD.account_id AND id = OLD.run_id;
            RETURN OLD;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER invalidate_erased_fence_run
        BEFORE DELETE ON app.context_fences
        FOR EACH ROW EXECUTE FUNCTION app.invalidate_erased_fence_run()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER invalidate_erased_fence_run ON app.context_fences")
    op.execute("DROP FUNCTION app.invalidate_erased_fence_run()")
    op.drop_column("agent_runs", "sources_invalidated", schema="app")
