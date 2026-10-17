"""Database URL helpers.

procrastinate's PsycopgConnector takes a libpq DSN, not a SQLAlchemy URL, so the
one configured `DATABASE_URL` is translated here rather than configured twice
(ARCHITECTURE §15.2).
"""

from __future__ import annotations

from sqlalchemy.engine import make_url


def libpq_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy URL to the libpq DSN procrastinate expects."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
