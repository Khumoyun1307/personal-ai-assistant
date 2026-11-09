"""Explicit production role upgrade/preflight; never called by the runtime.

The maintenance container alone receives the init/admin URL and generated
passwords. No Settings instance loads application/provider credentials here.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from assistant.db.url import libpq_dsn

SCHEMAS = ("app", "procrastinate")
MIGRATION_ROLE = "assistant_migrate"
APP_ROLE = "assistant_app"
BACKUP_ROLE = "assistant_backup"


class RoleSetupError(Exception):
    """A content-free deployment prerequisite failed."""


class RolePasswords(BaseModel):
    model_config = ConfigDict(frozen=True)

    migration: SecretStr
    app: SecretStr
    backup: SecretStr

    @field_validator("migration", "app", "backup")
    @classmethod
    def _hex_password(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"[0-9a-f]{64}", value.get_secret_value()):
            raise ValueError("generated database passwords must be 64 hex characters")
        return value


def _transfer_schema(conn: psycopg.Connection[tuple[object, ...]], schema: str) -> None:
    """Transfer only application objects, never other databases or extensions."""
    conn.execute(
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
            sql.Identifier(schema), sql.Identifier(MIGRATION_ROLE)
        )
    )
    conn.execute(
        sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
            sql.Identifier(schema), sql.Identifier(MIGRATION_ROLE)
        )
    )
    relations = conn.execute(
        "SELECT c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f') "
        "ORDER BY c.relkind = 'S', c.oid",
        (schema,),
    ).fetchall()
    kinds = {
        "r": "TABLE",
        "p": "TABLE",
        "S": "SEQUENCE",
        "v": "VIEW",
        "m": "MATERIALIZED VIEW",
        "f": "FOREIGN TABLE",
    }
    for name, kind in relations:
        conn.execute(
            sql.SQL("ALTER {} {}.{} OWNER TO {}").format(
                sql.SQL(kinds[str(kind)]),
                sql.Identifier(schema),
                sql.Identifier(str(name)),
                sql.Identifier(MIGRATION_ROLE),
            )
        )
    routines = conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s",
        (schema,),
    ).fetchall()
    for (signature,) in routines:
        # regprocedure is server-quoted, schema-qualified SQL, never user input.
        conn.execute(
            sql.SQL("ALTER ROUTINE {} OWNER TO {}").format(
                sql.SQL(str(signature)), sql.Identifier(MIGRATION_ROLE)
            )
        )
    enums = conn.execute(
        "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = %s AND t.typtype IN ('e', 'd')",
        (schema,),
    ).fetchall()
    for (name,) in enums:
        conn.execute(
            sql.SQL("ALTER TYPE {}.{} OWNER TO {}").format(
                sql.Identifier(schema), sql.Identifier(str(name)), sql.Identifier(MIGRATION_ROLE)
            )
        )


def configure_roles(database_url: str, passwords: RolePasswords) -> None:
    """Idempotent, transactional fresh-install/existing-database upgrade.

    Ownership stays with the migration role on restore (--no-owner). Default
    ACLs are installed before migrations. Re-run setup after a schema is
    deliberately dropped/recreated (default ACLs belong to that schema).
    """
    with psycopg.connect(libpq_dsn(database_url), connect_timeout=10) as conn:
        for role, password in (
            (MIGRATION_ROLE, passwords.migration),
            (APP_ROLE, passwords.app),
            (BACKUP_ROLE, passwords.backup),
        ):
            existing = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if existing is None:
                conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
            memberships = conn.execute(
                "SELECT 1 FROM pg_auth_members WHERE member = "
                "(SELECT oid FROM pg_roles WHERE rolname = %s)",
                (role,),
            ).fetchone()
            if memberships is not None:
                raise RoleSetupError("production roles must have no role memberships")
            conn.execute(
                sql.SQL(
                    "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password.get_secret_value()))
            )
        row = conn.execute("SELECT current_database()").fetchone()
        assert row is not None
        database = sql.Identifier(str(row[0]))
        conn.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                database, sql.Identifier(MIGRATION_ROLE)
            )
        )
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database))
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM {}, {}").format(
                database, sql.Identifier(APP_ROLE), sql.Identifier(BACKUP_ROLE)
            )
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                database, sql.Identifier(APP_ROLE), sql.Identifier(BACKUP_ROLE)
            )
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute(
            sql.SQL("REVOKE ALL ON SCHEMA public FROM {}, {}").format(
                sql.Identifier(APP_ROLE), sql.Identifier(BACKUP_ROLE)
            )
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}, {}").format(
                sql.Identifier(APP_ROLE), sql.Identifier(BACKUP_ROLE)
            )
        )
        conn.execute(
            sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(
                sql.Identifier(MIGRATION_ROLE)
            )
        )
        for schema in SCHEMAS:
            _transfer_schema(conn, schema)
            namespace = sql.Identifier(schema)
            conn.execute(
                sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC, {}, {}").format(
                    namespace, sql.Identifier(APP_ROLE), sql.Identifier(BACKUP_ROLE)
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(
                    namespace, sql.Identifier(APP_ROLE), sql.Identifier(BACKUP_ROLE)
                )
            )
            for objects in ("TABLES", "SEQUENCES", "FUNCTIONS"):
                conn.execute(
                    sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA {} FROM PUBLIC, {}, {}").format(
                        sql.SQL(objects),
                        namespace,
                        sql.Identifier(APP_ROLE),
                        sql.Identifier(BACKUP_ROLE),
                    )
                )
        # PUBLIC's global function default must be revoked globally: an
        # in-schema REVOKE cannot override a global default GRANT.
        defaults = sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} ").format(
            sql.Identifier(MIGRATION_ROLE)
        )
        for objects in ("TABLES", "SEQUENCES", "FUNCTIONS"):
            conn.execute(
                defaults
                + sql.SQL("REVOKE ALL ON {} FROM PUBLIC, {}, {}").format(
                    sql.SQL(objects), sql.Identifier(APP_ROLE), sql.Identifier(BACKUP_ROLE)
                )
            )
        for schema in SCHEMAS:
            prefix = sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} ").format(
                sql.Identifier(MIGRATION_ROLE), sql.Identifier(schema)
            )
            for objects in ("TABLES", "SEQUENCES", "FUNCTIONS"):
                conn.execute(
                    prefix
                    + sql.SQL("REVOKE ALL ON {} FROM PUBLIC, {}, {}").format(
                        sql.SQL(objects), sql.Identifier(APP_ROLE), sql.Identifier(BACKUP_ROLE)
                    )
                )
            for role, tables, sequences in (
                (APP_ROLE, "SELECT, INSERT, UPDATE, DELETE", "USAGE, SELECT"),
                (BACKUP_ROLE, "SELECT", "SELECT"),
            ):
                for objects, privileges in (("TABLES", tables), ("SEQUENCES", sequences)):
                    grant = sql.SQL("GRANT {} ON {} TO {}").format(
                        sql.SQL(privileges), sql.SQL(objects), sql.Identifier(role)
                    )
                    # Existing objects and future objects get the same ACL.
                    conn.execute(prefix + grant)
                    conn.execute(
                        sql.SQL("GRANT {} ON ALL {} IN SCHEMA {} TO {}").format(
                            sql.SQL(privileges),
                            sql.SQL(objects),
                            sql.Identifier(schema),
                            sql.Identifier(role),
                        )
                    )
            grant_functions = sql.SQL("GRANT EXECUTE ON FUNCTIONS TO {}").format(
                sql.Identifier(APP_ROLE)
            )
            conn.execute(prefix + grant_functions)
            conn.execute(
                sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(APP_ROLE)
                )
            )


def verify_roles(database_url: str, passwords: RolePasswords) -> None:
    """Authenticate each service before stopping the running app for deploy."""
    base = make_url(database_url)
    for role, password in (
        (MIGRATION_ROLE, passwords.migration),
        (APP_ROLE, passwords.app),
        (BACKUP_ROLE, passwords.backup),
    ):
        url = base.set(username=role, password=password.get_secret_value()).render_as_string(
            hide_password=False
        )
        with psycopg.connect(libpq_dsn(url), connect_timeout=10) as conn:
            flags = conn.execute(
                "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication "
                "OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            if flags != (False,):
                raise RoleSetupError("production role has administrative privileges")
            membership = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = "
                "(SELECT oid FROM pg_roles WHERE rolname = current_user))"
            ).fetchone()
            if membership != (False,):
                raise RoleSetupError("production role has a role membership")
            if role == MIGRATION_ROLE:
                owner = conn.execute(
                    "SELECT pg_get_userbyid(datdba) = current_user FROM pg_database "
                    "WHERE datname = current_database()"
                ).fetchone()
                if owner != (True,):
                    raise RoleSetupError("migration role must own the database")
                continue
            ddl = conn.execute(
                "SELECT has_database_privilege(current_database(), 'CREATE') OR "
                "has_database_privilege(current_database(), 'TEMP') OR "
                "has_schema_privilege('public', 'CREATE') OR "
                "has_schema_privilege('app', 'CREATE') OR "
                "has_schema_privilege('procrastinate', 'CREATE')"
            ).fetchone()
            if ddl != (False,):
                raise RoleSetupError("runtime/dump role has DDL privileges")
            missing = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname IN "
                "('app', 'procrastinate') AND NOT has_table_privilege("
                "quote_ident(schemaname) || '.' || quote_ident(tablename), 'SELECT'))"
            ).fetchone()
            if missing != (False,):
                raise RoleSetupError("service table grants are missing")
            owned = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE n.nspname IN ('app', 'procrastinate') "
                "AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f') "
                "AND pg_get_userbyid(c.relowner) = current_user)"
            ).fetchone()
            if owned != (False,):
                raise RoleSetupError("runtime/dump role owns database objects")
            # WHERE predicates can be reordered: a plain AND relkind = 'S'
            # does not keep has_sequence_privilege from receiving an index OID.
            sequences = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE n.nspname IN ('app', 'procrastinate') "
                "AND c.relkind = 'S' AND CASE WHEN c.relkind = 'S' "
                "THEN NOT has_sequence_privilege(c.oid, 'SELECT') ELSE FALSE END)"
            ).fetchone()
            if sequences != (False,):
                raise RoleSetupError("service sequence grants are missing")
            if role == BACKUP_ROLE:
                functions = conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n "
                    "ON n.oid = p.pronamespace WHERE n.nspname IN ('app', 'procrastinate') "
                    "AND has_function_privilege(p.oid, 'EXECUTE'))"
                ).fetchone()
                if functions != (False,):
                    raise RoleSetupError("dump role can execute application functions")
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                grants = conn.execute(
                    "SELECT bool_and(has_table_privilege("
                    "quote_ident(schemaname) || '.' || quote_ident(tablename), %s)) "
                    "FROM pg_tables WHERE schemaname IN ('app', 'procrastinate')",
                    (privilege,),
                ).fetchone()
                if role == APP_ROLE and grants not in ((True,), (None,)):
                    raise RoleSetupError("app DML grants are missing")
                if role == BACKUP_ROLE:
                    writable = conn.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname IN "
                        "('app', 'procrastinate') AND has_table_privilege("
                        "quote_ident(schemaname) || '.' || quote_ident(tablename), %s))",
                        (privilege,),
                    ).fetchone()
                    if writable != (False,):
                        raise RoleSetupError("dump role has write privileges")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("setup", "check"))
    parser.add_argument("--database", choices=("assistant", "restore_test"))
    args = parser.parse_args()
    try:
        url = os.environ["DATABASE_URL"]
        if args.database:
            url = make_url(url).set(database=args.database).render_as_string(hide_password=False)
        passwords = RolePasswords(
            migration=SecretStr(os.environ["MIGRATION_DB_PASSWORD"]),
            app=SecretStr(os.environ["APP_DB_PASSWORD"]),
            backup=SecretStr(os.environ["BACKUP_DB_PASSWORD"]),
        )
        if args.operation == "setup":
            configure_roles(url, passwords)
        verify_roles(url, passwords)
    except (KeyError, ValueError, ArgumentError, RoleSetupError, psycopg.Error) as error:
        # Even PostgreSQL diagnostics can contain passwords/URLs. Names only.
        print(
            f"Database roles unavailable ({type(error).__name__}); "
            "follow runbooks/deploy.md I24 role upgrade before deploying",
            file=sys.stderr,
        )
        return 1
    print("OK: production database roles verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
