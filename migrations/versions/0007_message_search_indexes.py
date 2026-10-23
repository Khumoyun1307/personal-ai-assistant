"""M03 S03.1: message search indexes.

- `pg_trgm` (in `public`, where the runtime `search_path` finds it) for fuzzy
  and substring matching on `messages.search_key`;
- `messages.search_tsv`, a STORED generated column
  `to_tsvector('simple', search_key)`, so it can never drift from the key: a
  NULL key (purged, deleted, media without caption) is a NULL vector, and the
  retention sweep and deletion need no change;
- GIN on `search_tsv` (word/prefix queries) and a trigram GIN on
  `search_key`.

`search_tsv` is CONTENT-BEARING (derived from message text): listed in
ARCHITECTURE §10.1 and `privacy/content_columns.py`, purged with
`search_key` (§10.4). Adding a STORED generated column rewrites `messages`
once; at personal scale that is seconds.

Hand-written and reviewed (AGENTS §12). Downgrade is symmetric.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-23
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute(
        "ALTER TABLE app.messages ADD COLUMN search_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, search_key)) STORED"
    )
    op.execute("CREATE INDEX ix_messages_search_tsv ON app.messages USING gin (search_tsv)")
    op.execute(
        "CREATE INDEX ix_messages_search_key_trgm ON app.messages "
        "USING gin (search_key public.gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.ix_messages_search_key_trgm")
    op.execute("DROP INDEX IF EXISTS app.ix_messages_search_tsv")
    op.execute("ALTER TABLE app.messages DROP COLUMN IF EXISTS search_tsv")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
