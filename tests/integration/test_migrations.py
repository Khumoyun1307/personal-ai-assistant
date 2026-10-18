"""Migrations apply and reverse against a real PostgreSQL (AGENTS §12).

Runs against its own throwaway database, because it destroys the schema.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
import sqlalchemy as sa

from tests.integration.conftest import (
    create_scratch_database,
    drop_scratch_database,
    run_alembic,
)

pytestmark = pytest.mark.integration

SCRATCH = "assistant_migration_test"

EXPECTED_TABLES = {
    "accounts",
    "agent_run_steps",
    "agent_runs",
    "audit_log",
    "consents",
    "control_turns",
    "inbound_updates",
    "llm_calls",
    "pending_inputs",
    "system_state",
}


@pytest.fixture(scope="module")
def scratch_url(test_database_url: str) -> Iterator[str]:
    url = create_scratch_database(test_database_url, SCRATCH)
    yield url
    drop_scratch_database(test_database_url, SCRATCH)


def libpq(url: str) -> str:
    return (
        sa.engine.make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


def tables_in(url: str, schema: str) -> set[str]:
    with psycopg.connect(libpq(url)) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,)
        ).fetchall()
    return {row[0] for row in rows}


def test_upgrade_creates_every_table_in_the_app_schema(scratch_url: str) -> None:
    run_alembic(scratch_url, "head")
    assert tables_in(scratch_url, "app") >= EXPECTED_TABLES


def test_procrastinate_lives_in_its_own_schema(scratch_url: str) -> None:
    """ARCHITECTURE §10.1. Its SQL is unqualified, so search_path must be set.

    If this regresses, procrastinate's tables silently land in `public` and the
    two schemas drift apart between environments.
    """
    run_alembic(scratch_url, "head")
    jobs_tables = tables_in(scratch_url, "procrastinate")
    assert "procrastinate_jobs" in jobs_tables
    assert not any(name.startswith("procrastinate_") for name in tables_in(scratch_url, "public"))


def test_downgrade_then_upgrade_round_trips(scratch_url: str) -> None:
    run_alembic(scratch_url, "head")
    run_alembic(scratch_url, "base")

    # Only alembic's own bookkeeping survives: revision 0001 deliberately does
    # not drop the `app` schema, because the version table lives in it.
    assert tables_in(scratch_url, "app") == {"alembic_version"}
    assert tables_in(scratch_url, "procrastinate") == set()

    run_alembic(scratch_url, "head")
    assert tables_in(scratch_url, "app") >= EXPECTED_TABLES


def test_identity_primary_keys_reject_a_client_supplied_id(scratch_url: str) -> None:
    """`GENERATED ALWAYS` means a client cannot collide with the generator."""
    run_alembic(scratch_url, "head")
    with (
        psycopg.connect(libpq(scratch_url), autocommit=True) as conn,
        pytest.raises(psycopg.errors.GeneratedAlways),
    ):
        conn.execute("INSERT INTO app.accounts (id, telegram_user_id) VALUES (999, 42)")


def test_timestamps_are_timezone_aware(scratch_url: str) -> None:
    """A naive column would silently store server-local time (§10.1)."""
    run_alembic(scratch_url, "head")
    with psycopg.connect(libpq(scratch_url)) as conn:
        rows = conn.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'app' AND data_type LIKE 'timestamp%'
            """
        ).fetchall()
    naive = [(t, c) for t, c, kind in rows if "without time zone" in kind]
    assert naive == [], f"naive timestamp columns: {naive}"


def test_every_tenant_table_has_an_account_id(scratch_url: str) -> None:
    """AGENTS §6.12. The two deliberate exceptions are named explicitly."""
    run_alembic(scratch_url, "head")
    exempt = {
        "alembic_version",
        "accounts",  # the tenant root itself: its `id` IS the account id
        "system_state",  # deployment state, belongs to no tenant
        "agent_run_steps",  # scoped transitively via run_id -> agent_runs
    }
    with psycopg.connect(libpq(scratch_url)) as conn:
        rows = conn.execute(
            """
            SELECT t.tablename
            FROM pg_tables t
            WHERE t.schemaname = 'app'
              AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns c
                WHERE c.table_schema = 'app' AND c.table_name = t.tablename
                  AND c.column_name = 'account_id')
            """
        ).fetchall()
    missing = {row[0] for row in rows} - exempt
    assert missing == set(), f"tenant tables without account_id: {missing}"
