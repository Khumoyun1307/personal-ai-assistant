"""Integration fixtures: a real PostgreSQL, migrated once per session.

AGENTS §11: integration tests use a real PostgreSQL 18, never SQLite. Each test
runs inside a transaction that is rolled back, so tests cannot see each other's
rows and the schema is migrated only once.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.db.engine import build_engine, build_session_factory
from tests.integration.business_env import Env, environment

ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def run_alembic(url: str, target: str, *, down: bool = False) -> None:
    """Run a migration against `url`. Sync on purpose: env.py owns the loop.

    `base` always downgrades; any other target upgrades unless `down`.
    """
    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = url
    try:
        if target == "base" or down:
            command.downgrade(alembic_config(), target)
        else:
            command.upgrade(alembic_config(), target)
    finally:
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous


def admin_url(base_url: str) -> str:
    return (
        sa.engine.make_url(base_url)
        .set(drivername="postgresql", database="postgres")
        .render_as_string(hide_password=False)
    )


def create_scratch_database(base_url: str, name: str) -> str:
    """A throwaway database, for tests that must destroy the schema."""
    with psycopg.connect(admin_url(base_url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    return sa.engine.make_url(base_url).set(database=name).render_as_string(hide_password=False)


def drop_scratch_database(base_url: str, name: str) -> None:
    with psycopg.connect(admin_url(base_url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def migrated_url(test_database_url: str) -> str:
    """The shared test database, migrated to head once for the session."""
    run_alembic(test_database_url, "base")
    run_alembic(test_database_url, "head")
    return test_database_url


@pytest.fixture
async def db_session(migrated_url: str) -> AsyncIterator[AsyncSession]:
    """A session in a transaction that is always rolled back."""
    engine = build_engine(migrated_url)
    factory = build_session_factory(engine)
    session = factory()
    await session.begin()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    """The S02 business-traffic environment (`tests/integration/business_env.py`)."""
    async with environment(migrated_url) as built:
        yield built
