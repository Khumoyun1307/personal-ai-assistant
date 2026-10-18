"""procrastinate's schema, vendored as a revision (ADR-06, AGENTS §12).

procrastinate ships SQL, not models, so its schema is applied here to keep one
migration history. Its SQL is schema-unqualified, so it is applied with
`search_path` pointed at the `procrastinate` schema (ARCHITECTURE §10.1).

**Re-vendor on every procrastinate upgrade**: add a new revision applying its
migration SQL, and review it by hand. Never edit this file after it has run.

The downgrade drops by name prefix rather than listing ~40 objects: every
object procrastinate creates is prefixed `procrastinate_` (verified in the M00
jobs spike), so the prefix is a reliable and upgrade-proof handle.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18
"""

from __future__ import annotations

from alembic import op
from procrastinate.schema import SchemaManager

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JOBS = "procrastinate"

_DROP_BY_PREFIX = f"""
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT 'DROP TABLE IF EXISTS {JOBS}.' || quote_ident(tablename) || ' CASCADE' AS stmt
        FROM pg_tables WHERE schemaname = '{JOBS}' AND tablename LIKE 'procrastinate%'
    LOOP EXECUTE obj.stmt; END LOOP;

    FOR obj IN
        SELECT 'DROP FUNCTION IF EXISTS {JOBS}.' || quote_ident(p.proname)
               || '(' || pg_get_function_identity_arguments(p.oid) || ') CASCADE' AS stmt
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = '{JOBS}' AND p.proname LIKE 'procrastinate%'
    LOOP EXECUTE obj.stmt; END LOOP;

    FOR obj IN
        SELECT 'DROP TYPE IF EXISTS {JOBS}.' || quote_ident(t.typname) || ' CASCADE' AS stmt
        FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = '{JOBS}' AND t.typname LIKE 'procrastinate%'
    LOOP EXECUTE obj.stmt; END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{JOBS}"')
    # Apply procrastinate's unqualified SQL into its own schema.
    op.execute(f'SET LOCAL search_path TO "{JOBS}"')
    op.execute(SchemaManager.get_schema())
    op.execute("SET LOCAL search_path TO DEFAULT")


def downgrade() -> None:
    op.execute(_DROP_BY_PREFIX)
    op.execute(f'DROP SCHEMA IF EXISTS "{JOBS}" CASCADE')
