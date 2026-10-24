"""M02 review: a durable marker for the privacy-policy update notice.

- `accounts.policy_notice_version` (text, NULL, structural): the policy
  version whose one-time update notice is settled for this owner —
  delivered, or sent with an unknown outcome (never resent). The notice
  used to be deduplicated only by its `audit_log` row, which the retention
  sweep deletes after a year, so it would have been sent again every year
  (M02 D59). NULL means no notice settled yet.

Hand-written and reviewed (AGENTS §12). No content-bearing column. No
backfill: no deployment has sent the v4 notice yet, and the notice itself
also settles from a delivered audit row it finds. The downgrade drops the
column (the audit rows remain, so nothing is lost within their year).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

APP = "app"


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("policy_notice_version", sa.Text, nullable=True),
        schema=APP,
    )


def downgrade() -> None:
    op.drop_column("accounts", "policy_notice_version", schema=APP)
