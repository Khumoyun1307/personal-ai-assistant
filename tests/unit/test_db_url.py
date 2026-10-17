"""SQLAlchemy URL → libpq DSN for procrastinate (ARCHITECTURE §15.2)."""

from __future__ import annotations

from assistant.db.url import libpq_dsn


def test_driver_is_stripped_and_password_kept() -> None:
    dsn = libpq_dsn("postgresql+psycopg://assistant:hunter2@db:5432/assistant")
    assert dsn == "postgresql://assistant:hunter2@db:5432/assistant"


def test_query_parameters_survive() -> None:
    dsn = libpq_dsn("postgresql+psycopg://assistant:hunter2@db:5432/assistant?sslmode=require")
    assert dsn.startswith("postgresql://")
    assert "sslmode=require" in dsn
