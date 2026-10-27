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
    "actions",
    "accounts",
    "agent_run_steps",
    "agent_runs",
    "audit_log",
    "business_connection_events",
    "business_connections",
    "consents",
    "control_turns",
    "conversation_summaries",
    "foreign_connections",
    "inbound_updates",
    "llm_calls",
    "memory_items",
    "pending_inputs",
    "system_state",
    "tool_confirmations",
    "tasks",
    "conversation_triage",
    "automations",
    "automation_occurrences",
    "digests",
    "notifications",
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


def test_0011_conversation_summaries_reverse(scratch_url: str) -> None:
    """S03.2: one rolling summary per conversation, one period row per range
    (a NULL end counts as one value), the kind/bounds CHECKs, cascades from
    the conversation, and a symmetric downgrade."""
    run_alembic(scratch_url, "base")
    run_alembic(scratch_url, "0011")
    assert "conversation_summaries" in tables_in(scratch_url, "app")
    assert {"uq_conversation_summaries_rolling", "uq_conversation_summaries_period"} <= _indexes(
        scratch_url, "conversation_summaries"
    )
    _seed_message(scratch_url, "synthetic")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        row = conn.execute(
            "SELECT account_id, id FROM app.conversations WHERE business_chat_id = 77"
        ).fetchone()
        assert row is not None
        account_id, conversation_id = row
        insert = (
            "INSERT INTO app.conversation_summaries (account_id, conversation_id, kind, "
            "period_start, period_end, summary) VALUES (%s, %s, %s, %s, %s, '{}')"
        )
        conn.execute(insert, (account_id, conversation_id, "rolling", None, None))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, (account_id, conversation_id, "rolling", None, None))
        start = "2026-10-18 08:00+00"
        conn.execute(insert, (account_id, conversation_id, "period", start, None))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, (account_id, conversation_id, "period", start, None))
        conn.execute(insert, (account_id, conversation_id, "period", start, "2026-10-18 09:00+00"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (account_id, conversation_id, "weekly", None, None))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (account_id, conversation_id, "rolling", start, None))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (account_id, conversation_id, "period", None, None))
        with pytest.raises(psycopg.errors.CheckViolation):  # review nit: end after start
            conn.execute(insert, (account_id, conversation_id, "period", start, start))
        conn.execute("DELETE FROM app.messages WHERE conversation_id = %s", (conversation_id,))
        conn.execute("DELETE FROM app.conversations WHERE id = %s", (conversation_id,))
        left = conn.execute("SELECT count(*) FROM app.conversation_summaries").fetchone()
        assert left == (0,)

    run_alembic(scratch_url, "0010", down=True)
    assert "conversation_summaries" not in tables_in(scratch_url, "app")
    run_alembic(scratch_url, "head")


def test_0012_memory_items_reverse(scratch_url: str) -> None:
    """S03.3: memory items scoped by exactly the FK their scope names, the
    enum CHECKs, an archived item carries its time, one item per (run, tool
    call), cascades from contact and conversation, and a symmetric downgrade."""
    run_alembic(scratch_url, "base")
    run_alembic(scratch_url, "0012")
    assert "memory_items" in tables_in(scratch_url, "app")
    assert "uq_memory_items_run_id_tool_call_id" in _constraints(scratch_url, "memory_items")
    _seed_message(scratch_url, "synthetic")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        row = conn.execute(
            "SELECT account_id, id, contact_id FROM app.conversations WHERE business_chat_id = 77"
        ).fetchone()
        assert row is not None
        account_id, conversation_id, contact_id = row
        run = conn.execute(
            "INSERT INTO app.agent_runs (account_id, run_type, model) "
            "VALUES (%s, 'owner_request', 'gpt-6-luna') RETURNING id",
            (account_id,),
        ).fetchone()
        assert run is not None
        insert = (
            "INSERT INTO app.memory_items (account_id, scope, contact_id, conversation_id, "
            "kind, content, status, archived_at, applies_to_replies, run_id, tool_call_id) "
            "VALUES (%s, %s, %s, %s, %s, 'synthetic note', %s, %s, %s, %s, %s)"
        )

        def add(
            scope: str,
            contact: int | None,
            conversation: int | None,
            *,
            kind: str = "fact",
            status: str = "active",
            archived_at: str | None = None,
            applies: bool = False,
            run_id: int | None = None,
            call: str | None = None,
        ) -> None:
            conn.execute(
                insert,
                (
                    account_id,
                    scope,
                    contact,
                    conversation,
                    kind,
                    status,
                    archived_at,
                    applies,
                    run_id,
                    call,
                ),
            )

        add("owner", None, None, kind="instruction", applies=True, run_id=run[0], call="c1")
        add("contact", contact_id, None)
        add("conversation", None, conversation_id, kind="preference")
        add("owner", None, None, status="archived", archived_at="2026-10-25 08:00+00")
        with pytest.raises(psycopg.errors.UniqueViolation):  # one item per tool call
            add("owner", None, None, run_id=run[0], call="c1")
        for bad in (
            lambda: add("owner", contact_id, None),  # owner scope names a contact
            lambda: add("contact", None, None),  # contact scope without the contact
            lambda: add("contact", contact_id, conversation_id),  # two targets
            lambda: add("conversation", None, None),
            lambda: add("delegation", None, None),  # not built yet
            lambda: add("owner", None, None, kind="shareable"),  # R2
            lambda: add("owner", None, None, status="archived"),  # no archived_at
            lambda: add("owner", None, None, archived_at="2026-10-25 08:00+00"),
            lambda: add("owner", None, None, kind="fact", applies=True),  # instructions only
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                bad()
        conn.execute("DELETE FROM app.agent_runs WHERE id = %s", (run[0],))
        kept = conn.execute(
            "SELECT count(*) FROM app.memory_items WHERE run_id IS NULL AND tool_call_id = 'c1'"
        ).fetchone()
        assert kept == (1,)  # the run's accounting goes after a year; the note stays
        conn.execute("DELETE FROM app.messages WHERE conversation_id = %s", (conversation_id,))
        conn.execute("DELETE FROM app.conversations WHERE id = %s", (conversation_id,))
        conn.execute("DELETE FROM app.contacts WHERE id = %s", (contact_id,))
        left = conn.execute(
            "SELECT scope, count(*) FROM app.memory_items GROUP BY scope ORDER BY scope"
        ).fetchall()
        assert left == [("owner", 2)]

    run_alembic(scratch_url, "0011", down=True)
    assert "memory_items" not in tables_in(scratch_url, "app")
    run_alembic(scratch_url, "head")


def _constraints(url: str, table: str) -> set[str]:
    with psycopg.connect(libpq(url)) as conn:
        rows = conn.execute(
            "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'app' AND t.relname = %s",
            (table,),
        ).fetchall()
    return {row[0] for row in rows}


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


def test_there_is_exactly_one_alembic_head() -> None:
    from alembic.script import ScriptDirectory

    from tests.integration.conftest import alembic_config

    assert ScriptDirectory.from_config(alembic_config()).get_heads() == ["0014"]


def test_0013_actions_reverse(scratch_url: str) -> None:
    run_alembic(scratch_url, "head")
    run_alembic(scratch_url, "0012", down=True)
    run_alembic(scratch_url, "0013")
    assert "actions" in tables_in(scratch_url, "app")
    assert {"action_id"} <= _columns(scratch_url, "messages")
    assert {"pending_draft_at"} <= _columns(scratch_url, "conversations")
    assert {
        "approved_text_hash",
        "sent_text_hash",
        "draft_flags",
        "revision_turn_id",
        "card_state",
    } <= _columns(scratch_url, "actions")
    assert {"ux_actions_live_card", "ix_actions_card_unsynced"} <= _indexes(scratch_url, "actions")
    run_alembic(scratch_url, "0012", down=True)
    assert "actions" not in tables_in(scratch_url, "app")
    assert "action_id" not in _columns(scratch_url, "messages")
    assert "pending_draft_at" not in _columns(scratch_url, "conversations")
    run_alembic(scratch_url, "head")


def test_0014_inbox_intelligence_reverse(scratch_url: str) -> None:
    run_alembic(scratch_url, "head")
    run_alembic(scratch_url, "0013", down=True)
    run_alembic(scratch_url, "0014")
    tables = {
        "tasks",
        "conversation_triage",
        "automations",
        "automation_occurrences",
        "digests",
        "notifications",
    }
    assert tables <= tables_in(scratch_url, "app")
    assert "priority" in _columns(scratch_url, "contacts")
    assert {"pending_triage_at", "pending_monitor_at"} <= _columns(scratch_url, "conversations")
    run_alembic(scratch_url, "0013", down=True)
    assert not tables & tables_in(scratch_url, "app")
    assert "priority" not in _columns(scratch_url, "contacts")
    assert not {"pending_triage_at", "pending_monitor_at"} & _columns(scratch_url, "conversations")
    run_alembic(scratch_url, "head")


def _phase_conversation(conn: psycopg.Connection, owner: int) -> tuple[int, int]:
    account = conn.execute(
        "INSERT INTO app.accounts (telegram_user_id) VALUES (%s) RETURNING id", (owner,)
    ).fetchone()
    assert account is not None
    contact = conn.execute(
        "INSERT INTO app.contacts (account_id, telegram_user_id) VALUES (%s, 424242) RETURNING id",
        (account[0],),
    ).fetchone()
    assert contact is not None
    conversation = conn.execute(
        "INSERT INTO app.conversations (account_id, contact_id, business_chat_id) "
        "VALUES (%s, %s, 424242) RETURNING id",
        (account[0], contact[0]),
    ).fetchone()
    assert conversation is not None
    return account[0], conversation[0]


def test_one_live_card_per_conversation_is_enforced_by_the_database(scratch_url: str) -> None:
    run_alembic(scratch_url, "head")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        account, conversation = _phase_conversation(conn, 81001)
        sql = (
            "INSERT INTO app.actions (account_id, conversation_id, kind, origin, state) "
            "VALUES (%s, %s, 'send_message', 'agent_proposal', %s)"
        )
        conn.execute(sql, (account, conversation, "awaiting_approval"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(sql, (account, conversation, "revising"))
        conn.execute("UPDATE app.actions SET state = 'outdated' WHERE account_id = %s", (account,))
        conn.execute(sql, (account, conversation, "revising"))
        conn.execute("DELETE FROM app.accounts WHERE id = %s", (account,))


def test_an_agent_task_is_unique_per_conversation_kind_and_first_source(scratch_url: str) -> None:
    run_alembic(scratch_url, "head")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        account, conversation = _phase_conversation(conn, 81002)
        sql = (
            "INSERT INTO app.tasks (account_id, conversation_id, kind, title, created_by, "
            "first_source_message_id) VALUES (%s, %s, %s, 'Synthetic task', %s, %s)"
        )
        conn.execute(sql, (account, conversation, "open_question", "agent", 111))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(sql, (account, conversation, "open_question", "agent", 111))
        conn.execute(sql, (account, conversation, "open_question", "agent", 112))
        conn.execute(sql, (account, conversation, "decision", "agent", 111))
        conn.execute(sql, (account, conversation, "open_question", "owner", 111))
        conn.execute("DELETE FROM app.accounts WHERE id = %s", (account,))


def test_notification_dedupe_key_is_unique_per_account(scratch_url: str) -> None:
    run_alembic(scratch_url, "head")
    with psycopg.connect(libpq(scratch_url), autocommit=True) as conn:
        account, _ = _phase_conversation(conn, 81003)
        other, _ = _phase_conversation(conn, 81004)
        sql = (
            "INSERT INTO app.notifications (account_id, kind, priority, dedupe_key) "
            "VALUES (%s, 'triage_urgent', 'urgent', 'triage:1:2:urgent')"
        )
        conn.execute(sql, (account,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(sql, (account,))
        conn.execute(sql, (other,))
        conn.execute("DELETE FROM app.accounts WHERE id IN (%s, %s)", (account, other))
