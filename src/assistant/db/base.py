"""Declarative base and shared column types (ARCHITECTURE §10.1).

Conventions enforced here so no table can drift from them:

- everything lives in the PostgreSQL schema `app` (procrastinate owns its own);
- primary keys are `bigint generated always as identity`;
- timestamps are `timestamptz`, always UTC;
- constraints are named by convention, so migrations can drop them by name
  rather than by whatever PostgreSQL happened to generate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from sqlalchemy import BigInteger, MetaData, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, mapped_column

APP_SCHEMA = "app"
JOBS_SCHEMA = "procrastinate"

#: Deterministic constraint names. Without this, Alembic downgrades have to
#: guess at server-generated names and silently fail to drop things.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: `bigint generated always as identity` — not a sequence default, so a client
#: cannot supply its own id and collide with the generator.
IdPk = Annotated[
    int,
    mapped_column(BigInteger, primary_key=True, autoincrement=True, sort_order=-100),
]

#: Telegram ids are 64-bit and are NOT our primary keys.
TelegramId = Annotated[int, mapped_column(BigInteger)]

UtcTimestamp = Annotated[datetime, mapped_column(TIMESTAMP(timezone=True))]

CreatedAt = Annotated[
    datetime,
    mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False),
]

JsonDict = Annotated[dict[str, Any], mapped_column(JSONB)]


class Base(DeclarativeBase):
    """Declarative base for every application table."""

    metadata = MetaData(schema=APP_SCHEMA, naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        """Identity only — never column values, which may carry content."""
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"
