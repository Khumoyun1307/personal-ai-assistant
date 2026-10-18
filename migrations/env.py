"""Alembic environment (async).

The URL comes from the application settings, never from alembic.ini — the URL
contains a password and alembic.ini is committed.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing the model modules registers every table on Base.metadata.
# Without these imports autogenerate would cheerfully propose dropping them.
import assistant.accounts.models
import assistant.agent.models
import assistant.db.models
import assistant.inbox.models  # noqa: F401
from assistant.db.base import APP_SCHEMA, JOBS_SCHEMA, Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    """Prefer an explicit override (tests), else the configured DATABASE_URL."""
    override = os.environ.get("ALEMBIC_DATABASE_URL")
    if override:
        return override
    from assistant.config import Settings

    return Settings().database_url.get_secret_value()  # type: ignore[call-arg]


def _include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    """Keep autogenerate out of procrastinate's schema.

    Its tables are vendored SQL, not SQLAlchemy models, so autogenerate would
    see them as unexpected and propose dropping them (AGENTS §12).
    """
    if type_ == "table":
        schema = getattr(obj, "schema", None)
        return schema != JOBS_SCHEMA
    return True


def do_run_migrations(connection: Connection) -> None:
    # Alembic creates its version table in `app` before revision 0001 runs,
    # so the schema has to exist first. Idempotent, and harmless once 0001 has
    # created it for real.
    connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{APP_SCHEMA}"')
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=APP_SCHEMA,
        include_schemas=True,
        include_object=_include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        # Required: SQLAlchemy 2.x opens an implicit transaction on connect, so
        # without this the whole migration is rolled back on close — silently,
        # with alembic still exiting 0.
        await connection.commit()
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=APP_SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
