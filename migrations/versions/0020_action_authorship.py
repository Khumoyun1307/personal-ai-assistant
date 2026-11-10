"""Current-version action authorship, without an archived body.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-09

Follows I20's privacy journal and stable recovery identities (revision 0019).

Data handling: only known generated origins are backfilled. Dictation has
no retroactive source proof. Downgrade drops structural authorship only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

# Additive nullable metadata and a backfill confined to that new column;
# existing readers/writers and existing-field constraints are unchanged.
COMPATIBILITY = "expand"


def upgrade() -> None:
    op.add_column(
        "actions", sa.Column("authorship", postgresql.JSONB(), nullable=True), schema="app"
    )
    op.execute(
        "UPDATE app.actions SET authorship = jsonb_build_object("
        "'version', version, 'kind', 'assistant_written', 'source_ids', '[]'::jsonb, "
        "'parent_version', CASE WHEN version = 1 THEN NULL ELSE version - 1 END, "
        "'retains_generated_text', true) "
        "WHERE origin IN ('agent_proposal', 'delegation_auto')"
    )


def downgrade() -> None:
    op.drop_column("actions", "authorship", schema="app")
