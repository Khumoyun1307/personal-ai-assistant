"""procrastinate's schema, vendored as an Alembic revision (ADR-06).

Every object procrastinate creates is prefixed `procrastinate_`, so the
downgrade can drop them generically instead of listing ~40 objects by hand.
Re-vendor (a new revision) on every procrastinate upgrade — AGENTS §12.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op
from procrastinate.schema import SchemaManager

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_DROP_ALL = """
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT 'DROP TABLE IF EXISTS ' || quote_ident(tablename) || ' CASCADE' AS stmt
        FROM pg_tables
        WHERE schemaname = current_schema() AND tablename LIKE 'procrastinate%'
    LOOP EXECUTE obj.stmt; END LOOP;

    FOR obj IN
        SELECT 'DROP FUNCTION IF EXISTS ' || quote_ident(p.proname)
               || '(' || pg_get_function_identity_arguments(p.oid) || ') CASCADE' AS stmt
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = current_schema() AND p.proname LIKE 'procrastinate%'
    LOOP EXECUTE obj.stmt; END LOOP;

    FOR obj IN
        SELECT 'DROP TYPE IF EXISTS ' || quote_ident(t.typname) || ' CASCADE' AS stmt
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = current_schema() AND t.typname LIKE 'procrastinate%'
    LOOP EXECUTE obj.stmt; END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(SchemaManager.get_schema())


def downgrade() -> None:
    op.execute(_DROP_ALL)
