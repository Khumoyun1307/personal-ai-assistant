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
    "business_connection_events",
    "business_connections",
    "consents",
    "control_turns",
    "foreign_connections",
    "inbound_updates",
    "llm_calls",
    "pending_inputs",
    "system_state",
    "tool_confirmations",
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


def _seed_message(url: str, search_key: str | None) -> None:
    with psycopg.connect(libpq(url), autocommit=True) as conn:
        account = conn.execute(
            "INSERT INTO app.accounts (telegram_user_id) VALUES (4242) "
            "ON CONFLICT (telegram_user_id) DO UPDATE SET status = accounts.status RETURNING id"
        ).fetchone()
        assert account is not None
        contact = conn.execute(
            "INSERT INTO app.contacts (account_id, telegram_user_id) VALUES (%s, 77) "
            "ON CONFLICT ON CONSTRAINT uq_contacts_account_id_telegram_user_id "
            "DO UPDATE SET is_bot = false RETURNING id",
            (account[0],),
        ).fetchone()
        assert contact is not None
        conversation = conn.execute(
            "INSERT INTO app.conversations (account_id, business_chat_id, contact_id) "
            "VALUES (%s, 77, %s) "
            "ON CONFLICT ON CONSTRAINT uq_conversations_account_id_business_chat_id "
            "DO UPDATE SET mode = conversations.mode RETURNING id",
            (account[0], contact[0]),
        ).fetchone()
        assert conversation is not None
        conn.execute(
            "INSERT INTO app.messages (account_id, conversation_id, direction, sent_at, "
            "content_type, text, search_key) VALUES (%s, %s, 'contact', now(), 'text', %s, %s)",
            (account[0], conversation[0], search_key, search_key),
        )


def _columns(url: str, table: str) -> set[str]:
    with psycopg.connect(libpq(url)) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'app' AND table_name = %s",
            (table,),
        ).fetchall()
    return {row[0] for row in rows}


def _indexes(url: str, table: str) -> set[str]:
    with psycopg.connect(libpq(url)) as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'app' AND tablename = %s",
            (table,),
        ).fetchall()
    return {row[0] for row in rows}


def test_0007_search_vector_follows_the_key_and_reverses(scratch_url: str) -> None:
    """S03.1: `search_tsv` is generated (existing rows too), NULL with a NULL
    key, indexed by GIN, with a trigram GIN on the key; downgrade removes all."""
    run_alembic(scratch_url, "base")
    run_alembic(scratch_url, "0006")
    _seed_message(scratch_url, "ozbekiston galaba")
    run_alembic(scratch_url, "0007")

    assert "search_tsv" in _columns(scratch_url, "messages")
    assert {"ix_messages_search_tsv", "ix_messages_search_key_trgm"} <= _indexes(
        scratch_url, "messages"
    )
    _seed_message(scratch_url, None)
    with psycopg.connect(libpq(scratch_url)) as conn:
        rows = conn.execute(
            "SELECT search_key, search_tsv::text FROM app.messages ORDER BY id"
        ).fetchall()
        assert rows == [("ozbekiston galaba", "'galaba':2 'ozbekiston':1"), (None, None)]
        matched = conn.execute(
            "SELECT count(*) FROM app.messages "
            "WHERE search_tsv @@ to_tsquery('simple', 'ozbek:*') "
            "AND word_similarity('galaba', search_key) > 0.9"
        ).fetchone()
        assert matched == (1,)

    run_alembic(scratch_url, "0006", down=True)
    assert "search_tsv" not in _columns(scratch_url, "messages")
    assert not {"ix_messages_search_tsv", "ix_messages_search_key_trgm"} & _indexes(
        scratch_url, "messages"
    )
    with psycopg.connect(libpq(scratch_url)) as conn:
        assert (
            conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'").fetchone() is None
        )
    run_alembic(scratch_url, "head")


def test_0008_third_party_turns_and_tool_confirmations_reverse(scratch_url: str) -> None:
    """S03.1: the two control-turn columns and `tool_confirmations` (unique per
    run and call, status CHECK) come and go symmetrically."""
    run_alembic(scratch_url, "head")
    assert {"third_party", "quoted_text"} <= _columns(scratch_url, "control_turns")
    assert "tool_confirmations" in tables_in(scratch_url, "app")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        account = conn.execute(
            "INSERT INTO app.accounts (telegram_user_id) VALUES (4343) RETURNING id"
        ).fetchone()
        assert account is not None
        insert = (
            "INSERT INTO app.tool_confirmations (account_id, tool_call_id, tool_name, status) "
            "VALUES (%s, %s, 'synthetic', %s)"
        )
        conn.execute(insert, (account[0], "c1", "pending"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (account[0], "c2", "done"))
        default = conn.execute(
            "INSERT INTO app.control_turns (account_id, role) VALUES (%s, 'owner') "
            "RETURNING third_party, quoted_text",
            (account[0],),
        ).fetchone()
        assert default == (False, None)

    run_alembic(scratch_url, "0007", down=True)
    assert not {"third_party", "quoted_text"} & _columns(scratch_url, "control_turns")
    assert "tool_confirmations" not in tables_in(scratch_url, "app")
    run_alembic(scratch_url, "head")


def test_0009_tainted_reply_flag_and_applying_state_reverse(scratch_url: str) -> None:
    """Review round 2: `control_turns.from_tainted_run` and the `applying`
    confirmation state come and go symmetrically."""
    run_alembic(scratch_url, "head")
    assert "from_tainted_run" in _columns(scratch_url, "control_turns")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        account = conn.execute(
            "INSERT INTO app.accounts (telegram_user_id) VALUES (4646) RETURNING id"
        ).fetchone()
        assert account is not None
        conn.execute(
            "INSERT INTO app.tool_confirmations (account_id, tool_call_id, tool_name, status) "
            "VALUES (%s, 'z1', 'synthetic', 'applying')",
            (account[0],),
        )
    # The `applying` row is kept: the downgrade must turn it `failed` (its
    # outcome is unknown) rather than trip the narrower CHECK (review 3 gap b).
    run_alembic(scratch_url, "0008", down=True)
    assert "from_tainted_run" not in _columns(scratch_url, "control_turns")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        kept = conn.execute(
            "SELECT status FROM app.tool_confirmations WHERE tool_call_id = 'z1'"
        ).fetchone()
        assert kept == ("failed",)
        conn.execute("DELETE FROM app.tool_confirmations WHERE tool_call_id = 'z1'")
    with (
        psycopg.connect(libpq(scratch_url), autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(
            "INSERT INTO app.tool_confirmations (account_id, tool_call_id, tool_name, status) "
            "SELECT id, 'z2', 'synthetic', 'applying' FROM app.accounts LIMIT 1"
        )
    run_alembic(scratch_url, "head")


def test_0010_policy_notice_marker_reverses(scratch_url: str) -> None:
    """M02 review: `accounts.policy_notice_version` is nullable (NULL for an
    existing owner, so the notice still goes out) and goes away on downgrade."""
    run_alembic(scratch_url, "base")
    run_alembic(scratch_url, "0009")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        conn.execute("INSERT INTO app.accounts (telegram_user_id) VALUES (4747)")
    run_alembic(scratch_url, "0010")

    assert "policy_notice_version" in _columns(scratch_url, "accounts")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        existing = conn.execute(
            "SELECT policy_notice_version FROM app.accounts WHERE telegram_user_id = 4747"
        ).fetchone()
        assert existing == (None,)
        conn.execute(
            "UPDATE app.accounts SET policy_notice_version = 'v4' WHERE telegram_user_id = 4747"
        )

    run_alembic(scratch_url, "0009", down=True)
    assert "policy_notice_version" not in _columns(scratch_url, "accounts")
    with psycopg.connect(libpq(scratch_url)) as conn:
        kept = conn.execute(
            "SELECT count(*) FROM app.accounts WHERE telegram_user_id = 4747"
        ).fetchone()
        assert kept == (1,)
    run_alembic(scratch_url, "head")


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
        "foreign_connections",  # non-owners: must never get an account (§6 invariant 12)
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
