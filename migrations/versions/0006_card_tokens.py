"""M02 S02.4: confirmation cards.

`pending_inputs.token` — the unguessable id a control-chat button carries
(ARCHITECTURE §6.7, `accounts/callbacks.py`). NULL for a plain pending text
question; set for a card, which is single-use and resolved by a tap or by
expiry. Partial unique index so a token can never match two cards.

Hand-written and reviewed (AGENTS §12). No content-bearing column is added.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP = "app"


def upgrade() -> None:
    op.add_column("pending_inputs", sa.Column("token", sa.Text, nullable=True), schema=APP)
    op.create_index(
        "uq_pending_inputs_token",
        "pending_inputs",
        ["token"],
        unique=True,
        schema=APP,
        postgresql_where=sa.text("token IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_pending_inputs_token", table_name="pending_inputs", schema=APP)
    op.drop_column("pending_inputs", "token", schema=APP)
