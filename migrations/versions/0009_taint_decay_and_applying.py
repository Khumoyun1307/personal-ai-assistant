"""M03 S03.1 review round 2: taint decay and the `applying` state.

- `control_turns.from_tainted_run` (bool, default false, structural): an
  assistant reply written by a tainted run. Such a reply is always shown to
  the model as untrusted; only a reply whose run was tainted by its OWN
  inputs (then `third_party` is set as well) taints later runs, so taint
  decays once the original source leaves the history window (M03 D56).
- `tool_confirmations.status` gains `applying`: the applier claims
  `confirmed → applying` before running the handler and finishes
  `applied`/`failed`; a row left `applying` (the job was cancelled mid-way)
  is failed by the reconciler and the owner told (review item 7).

Hand-written and reviewed (AGENTS §12). No content-bearing column. The
downgrade turns any `applying` row into `failed` before restoring the
narrower CHECK (no data loss: the outcome of such a row is unknown anyway).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

APP = "app"
OLD = "('pending', 'confirmed', 'applied', 'failed', 'rejected', 'expired')"
NEW = "('pending', 'confirmed', 'applying', 'applied', 'failed', 'rejected', 'expired')"
#: The name 0008 actually created: the metadata naming convention prefixes
#: `ck_<table>_` to the given name (as for every CHECK since 0001). Kept as
#: is, so up and down stay symmetric.
CHECK = "ck_tool_confirmations_ck_tool_confirmations_status"


def _replace_check(allowed: str) -> None:
    op.execute(f'ALTER TABLE app.tool_confirmations DROP CONSTRAINT "{CHECK}"')
    op.execute(
        f'ALTER TABLE app.tool_confirmations ADD CONSTRAINT "{CHECK}" CHECK (status IN {allowed})'
    )


def upgrade() -> None:
    op.add_column(
        "control_turns",
        sa.Column("from_tainted_run", sa.Boolean, nullable=False, server_default=sa.text("false")),
        schema=APP,
    )
    _replace_check(NEW)


def downgrade() -> None:
    op.execute("UPDATE app.tool_confirmations SET status = 'failed' WHERE status = 'applying'")
    _replace_check(OLD)
    op.drop_column("control_turns", "from_tainted_run", schema=APP)
