"""Ordered structural recipient candidates for an owner request.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

COMPATIBILITY = "expand"


def upgrade() -> None:
    op.add_column(
        "owner_requests",
        sa.Column("resolution_candidates", postgresql.JSONB(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("owner_requests", "resolution_candidates", schema="app")
