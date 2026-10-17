"""Domain baseline: a stand-in for the conversation row a job is deferred with.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spike_conversations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("note", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("spike_conversations")
