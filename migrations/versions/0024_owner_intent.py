"""Bind selected owner requests to their conversation intent.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

COMPATIBILITY = "expand"


def upgrade() -> None:
    op.add_column(
        "owner_requests", sa.Column("intent_revision", sa.BigInteger(), nullable=True), schema="app"
    )
    op.add_column(
        "owner_requests",
        sa.Column("manual_watermark", sa.BigInteger(), nullable=True),
        schema="app",
    )
    op.create_check_constraint(
        op.f("ck_owner_requests_intent_revision"),
        "owner_requests",
        "intent_revision >= 0",
        schema="app",
    )
    op.create_check_constraint(
        op.f("ck_owner_requests_manual_watermark"),
        "owner_requests",
        "manual_watermark >= 0",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_owner_requests_intent_revision"), "owner_requests", type_="check", schema="app"
    )
    op.drop_constraint(
        op.f("ck_owner_requests_manual_watermark"), "owner_requests", type_="check", schema="app"
    )
    op.drop_column("owner_requests", "manual_watermark", schema="app")
    op.drop_column("owner_requests", "intent_revision", schema="app")
