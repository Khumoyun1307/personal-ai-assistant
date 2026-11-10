"""Control reply targets and component provenance; no copied content.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-09

Legacy mixed turns remain unclassified. Downgrade removes structural metadata
only; it does not change retained owner/quoted text or actions.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "control_turns",
        sa.Column("reply_to_telegram_message_id", sa.BigInteger(), nullable=True),
        schema="app",
    )
    op.add_column(
        "control_turns",
        sa.Column("owner_text_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("control_turns", "owner_text_verified", schema="app")
    op.drop_column("control_turns", "reply_to_telegram_message_id", schema="app")
