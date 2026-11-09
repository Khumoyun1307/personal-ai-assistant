"""I24: production grants against a disposable PostgreSQL database only.

The backup Compose smoke test additionally runs PostgreSQL 18's pg_dump with
these credentials and restores its output; no host client is required here.
"""

import asyncio
from collections.abc import Iterator

import psycopg
import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from assistant.db.production_roles import (
    RolePasswords,
    RoleSetupError,
    configure_roles,
    verify_roles,
)
from tests.integration.conftest import (
    create_scratch_database,
    drop_scratch_database,
    run_alembic,
)

pytestmark = pytest.mark.integration
PASSWORDS = RolePasswords(
    migration=SecretStr("a" * 64),
    app=SecretStr("b" * 64),
    backup=SecretStr("c" * 64),
)
SCRATCH = "assistant_privileges_test"


def role_url(url: str, role: str, password: SecretStr) -> str:
    return (
        sa.engine.make_url(url)
        .set(username=role, password=password.get_secret_value())
        .render_as_string(hide_password=False)
    )


def libpq(url: str) -> str:
    return sa.engine.make_url(url).set(drivername="postgresql").render_as_string(False)


@pytest.fixture(scope="module")
def restricted_database(test_database_url: str) -> Iterator[str]:
    url = create_scratch_database(test_database_url, SCRATCH)
    try:
        # Exercise the upgrade path: existing objects belong to the init role.
        run_alembic(url, "head")
        configure_roles(url, PASSWORDS)
        configure_roles(url, PASSWORDS)
        verify_roles(url, PASSWORDS)
        yield url
    finally:
        drop_scratch_database(test_database_url, SCRATCH)
        # Default ACLs disappear with scratch. Roles are cluster-wide.
        with psycopg.connect(libpq(test_database_url), autocommit=True) as conn:
            for role in ("assistant_app", "assistant_backup", "assistant_migrate"):
                conn.execute(
                    psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(role))
                )


def test_app_can_write_but_cannot_create_drop_or_become_owner(restricted_database: str) -> None:
    url = role_url(restricted_database, "assistant_app", PASSWORDS.app)
    with psycopg.connect(libpq(url), autocommit=True) as conn:
        conn.execute("INSERT INTO app.system_state (key, value) VALUES ('i24', '{}')")
        conn.execute("UPDATE app.system_state SET value = '1' WHERE key = 'i24'")
        row = conn.execute("SELECT value FROM app.system_state WHERE key = 'i24'").fetchone()
        assert row == (1,)
        conn.execute("DELETE FROM app.system_state WHERE key = 'i24'")
        for statement in (
            "CREATE TABLE app.i24_forbidden (value int)",
            "CREATE TABLE public.i24_forbidden (value int)",
            "CREATE SCHEMA i24_forbidden",
            "CREATE TEMP TABLE i24_forbidden (value int)",
            "DROP TABLE app.accounts CASCADE",
            "SET ROLE assistant_migrate",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_role_upgrade_and_preflight_distinguish_sequences_from_indexes(
    restricted_database: str,
) -> None:
    with psycopg.connect(libpq(restricted_database), autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE app.i24_relation_kinds ("
            "identity_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "serial_value bigserial UNIQUE)"
        )
    try:
        configure_roles(restricted_database, PASSWORDS)
        configure_roles(restricted_database, PASSWORDS)
        verify_roles(restricted_database, PASSWORDS)
        with psycopg.connect(libpq(restricted_database), autocommit=True) as conn:
            kinds = conn.execute(
                "SELECT c.relkind, count(*) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'app' AND c.relname LIKE 'i24_relation_kinds%' "
                "GROUP BY c.relkind"
            ).fetchall()
            assert dict(kinds) == {"r": 1, "S": 2, "i": 2}
            owners = conn.execute(
                "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'app' AND c.relname LIKE 'i24_relation_kinds%'"
            ).fetchall()
            assert owners == [("assistant_migrate",)]
            conn.execute(
                "REVOKE SELECT ON SEQUENCE app.i24_relation_kinds_identity_id_seq "
                "FROM assistant_backup"
            )
        with pytest.raises(RoleSetupError, match="sequence grants"):
            verify_roles(restricted_database, PASSWORDS)
    finally:
        with psycopg.connect(libpq(restricted_database), autocommit=True) as conn:
            conn.execute("DROP TABLE app.i24_relation_kinds")


def test_backup_can_read_every_dumped_table_but_cannot_write(restricted_database: str) -> None:
    url = role_url(restricted_database, "assistant_backup", PASSWORDS.backup)
    with psycopg.connect(libpq(url), autocommit=True) as conn:
        tables = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname IN ('app', 'procrastinate')"
        ).fetchall()
        assert len(tables) > 10
        for schema, table in tables:
            conn.execute(
                psycopg.sql.SQL("SELECT * FROM {}.{} LIMIT 0").format(
                    psycopg.sql.Identifier(schema), psycopg.sql.Identifier(table)
                )
            )
        functions = conn.execute(
            "SELECT bool_or(has_function_privilege(p.oid, 'EXECUTE')) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'procrastinate'"
        ).fetchone()
        assert functions == (False,)
        for statement in (
            "INSERT INTO app.system_state (key, value) VALUES ('forbidden', '{}')",
            "DELETE FROM procrastinate.procrastinate_jobs",
            "DROP TABLE app.accounts CASCADE",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_migration_role_owns_existing_objects_and_future_grants(restricted_database: str) -> None:
    url = role_url(restricted_database, "assistant_migrate", PASSWORDS.migration)
    # Applying migrations never needs Telegram/provider settings or superuser.
    run_alembic(url, "head")
    with psycopg.connect(libpq(url), autocommit=True) as conn:
        conn.execute("ALTER TABLE app.system_state ADD COLUMN i24_probe int")
        conn.execute("ALTER TABLE app.system_state DROP COLUMN i24_probe")
        # New tables/sequences inherit grants without a second admin upgrade.
        conn.execute("CREATE TABLE app.i24_future (value int GENERATED ALWAYS AS IDENTITY)")
        conn.execute("CREATE TABLE procrastinate.i24_future (value int)")
        conn.execute("CREATE FUNCTION app.i24_future_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'")
    verify_roles(restricted_database, PASSWORDS)
    app_url = role_url(restricted_database, "assistant_app", PASSWORDS.app)
    with psycopg.connect(libpq(app_url), autocommit=True) as conn:
        account = conn.execute(
            "INSERT INTO app.accounts (telegram_user_id) VALUES (240024) RETURNING id"
        ).fetchone()
        assert account is not None
        conn.execute("DELETE FROM app.accounts WHERE id = %s", (account[0],))
        conn.execute("INSERT INTO app.i24_future DEFAULT VALUES")
        conn.execute("INSERT INTO procrastinate.i24_future VALUES (1)")
        assert conn.execute("SELECT app.i24_future_fn()").fetchone() == (1,)
    backup_url = role_url(restricted_database, "assistant_backup", PASSWORDS.backup)
    with psycopg.connect(libpq(backup_url), autocommit=True) as conn:
        assert conn.execute("SELECT value FROM app.i24_future").fetchone() == (1,)
        assert conn.execute("SELECT last_value FROM app.i24_future_value_seq").fetchone() == (1,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT app.i24_future_fn()")


async def test_procrastinate_can_defer_and_execute_without_ddl(restricted_database: str) -> None:
    from assistant.jobs.app import build_app

    app = build_app(role_url(restricted_database, "assistant_app", PASSWORDS.app))
    received: list[int] = []

    @app.task(name="i24.synthetic", queue="i24")
    async def synthetic(value: int) -> None:
        received.append(value)

    async with asyncio.timeout(20), app.open_async():
        await synthetic.defer_async(value=24)
        await app.run_worker_async(
            queues=["i24"],
            concurrency=1,
            wait=False,
            install_signal_handlers=False,
        )
    assert received == [24]


def test_preflight_denies_privilege_drift_and_setup_repairs_it(restricted_database: str) -> None:
    try:
        with psycopg.connect(libpq(restricted_database), autocommit=True) as conn:
            conn.execute("GRANT CREATE ON SCHEMA app TO assistant_app")
        with pytest.raises(RoleSetupError, match="DDL"):
            verify_roles(restricted_database, PASSWORDS)
        configure_roles(restricted_database, PASSWORDS)
        with psycopg.connect(libpq(restricted_database), autocommit=True) as conn:
            conn.execute("REVOKE SELECT ON app.system_state FROM assistant_backup")
        with pytest.raises(RoleSetupError, match="table grants"):
            verify_roles(restricted_database, PASSWORDS)
    finally:
        configure_roles(restricted_database, PASSWORDS)
    verify_roles(restricted_database, PASSWORDS)
