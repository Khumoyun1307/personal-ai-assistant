"""Ids-only privacy outbox and stable recovery identities (I20).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-09

Downgrade discards recovery metadata, not content. All remaining snapshots
then require manual reconciliation or full purge; do not downgrade in service.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

# New tables and defaulted recovery ids are additive, but triggers on existing
# tables make old writes depend on journal writes and their constraints. Keep
# those atomic privacy guarantees; old-code recovery is not expand-compatible.
COMPATIBILITY = "breaking"

IDENTITIES = (
    "accounts",
    "conversations",
    "contacts",
    "messages",
    "memory_items",
    "notification_sources",
)
SCOPE_CHECK = (
    "scope IN ('control', 'managed', 'conversation', 'contact', 'everything', "
    "'message', 'memory', 'notification_source', 'account', 'retention_review')"
)
OPERATION_CHECK = "operation IN ('delete', 'purge', 'revoke_ai_control', 'revoke_ai_managed')"
SUBJECT_CHECK = (
    "(scope IN ('conversation', 'contact', 'message', 'memory', 'notification_source')) = "
    "(subject_recovery_id IS NOT NULL)"
)
PROCESSOR_CHECK = (
    "(operation = 'delete' AND processor_id IS NULL) OR "
    "(operation = 'purge' AND scope = 'message' AND processor_id IS NULL) OR "
    "(operation = 'revoke_ai_control' AND scope = 'control' "
    "AND processor_id IS NOT NULL AND processor_id = 'OpenAI') OR "
    "(operation = 'revoke_ai_managed' AND scope = 'managed' "
    "AND processor_id IS NOT NULL AND processor_id = 'OpenAI')"
)


def upgrade() -> None:
    for table in IDENTITIES:
        op.add_column(
            table,
            sa.Column("recovery_id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")),
            schema="app",
        )
        op.create_unique_constraint(f"uq_{table}_recovery_id", table, ["recovery_id"], schema="app")
    op.create_table(
        "privacy_journal_heads",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("account_recovery_id", sa.Uuid(), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("digest", sa.Text(), nullable=False, server_default=sa.text("repeat('0', 64)")),
        sa.CheckConstraint("sequence >= 0", name="sequence"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest"),
        schema="app",
    )
    op.create_index("ix_privacy_journal_heads_account_id", "privacy_journal_heads", ["account_id"], schema="app")
    op.create_table(
        "privacy_journal",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("account_recovery_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("subject_recovery_id", sa.Uuid(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("processor_id", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("previous_digest", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.UniqueConstraint("account_recovery_id", "sequence"),
        sa.CheckConstraint("sequence > 0", name="sequence"),
        sa.CheckConstraint(SCOPE_CHECK, name="scope"),
        sa.CheckConstraint(OPERATION_CHECK, name="operation"),
        sa.CheckConstraint(SUBJECT_CHECK, name="subject"),
        sa.CheckConstraint(PROCESSOR_CHECK, name="processor"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="digest"),
        sa.CheckConstraint("previous_digest ~ '^[0-9a-f]{64}$'", name="previous_digest"),
        schema="app",
    )
    op.create_index("ix_privacy_journal_account_id", "privacy_journal", ["account_id"], schema="app")
    op.execute("""
        INSERT INTO app.privacy_journal_heads (account_id, account_recovery_id)
        SELECT id, recovery_id FROM app.accounts;

        CREATE FUNCTION app.append_privacy_decision(
            aid bigint, arid uuid, sc text, subject uuid, operation text,
            processor text, decided timestamptz
        ) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE h app.privacy_journal_heads; did uuid; material text; dg text;
        BEGIN
            IF current_setting('app.privacy_replay', true) = 'on'
                OR EXISTS (SELECT 1 FROM app.system_state WHERE key = 'restore_pending')
                THEN RETURN; END IF;
            INSERT INTO app.privacy_journal_heads (account_id, account_recovery_id)
                VALUES (aid, arid) ON CONFLICT (account_recovery_id) DO NOTHING;
            SELECT * INTO STRICT h FROM app.privacy_journal_heads
                WHERE account_recovery_id = arid FOR UPDATE;
            IF h.account_id <> aid THEN
                RAISE EXCEPTION 'privacy_identity_mismatch';
            END IF;
            did := gen_random_uuid();
            material := concat_ws('|', '1', (h.sequence + 1)::text, did::text,
                arid::text, sc, coalesce(subject::text, ''), operation,
                coalesce(processor, ''),
                to_char(decided AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                h.digest);
            dg := encode(sha256(convert_to(material, 'UTF8')), 'hex');
            INSERT INTO app.privacy_journal
                (account_id, sequence, decision_id, account_recovery_id, scope,
                 subject_recovery_id, operation, processor_id, decided_at, previous_digest, digest)
                VALUES (aid, h.sequence + 1, did, arid, sc, subject, operation,
                        processor, decided, h.digest, dg);
            UPDATE app.privacy_journal_heads SET sequence = h.sequence + 1, digest = dg
                WHERE account_recovery_id = arid;
        END $$;

        CREATE FUNCTION app.capture_privacy_decision() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE aid bigint; arid uuid; sc text; subject uuid; operation text := 'delete';
                processor text; decided timestamptz;
        BEGIN
            IF current_setting('app.privacy_replay', true) = 'on' THEN
                IF TG_WHEN = 'BEFORE' THEN RETURN OLD; END IF;
                RETURN NULL;
            END IF;
            decided := coalesce(nullif(current_setting('app.privacy_decided_at', true), '')::timestamptz,
                                CURRENT_TIMESTAMP);
            IF TG_TABLE_NAME = 'accounts' THEN
                IF TG_OP = 'INSERT' THEN
                    INSERT INTO app.privacy_journal_heads (account_id, account_recovery_id)
                        VALUES (NEW.id, NEW.recovery_id);
                    RETURN NULL;
                END IF;
                aid := OLD.id; arid := OLD.recovery_id;
                IF TG_OP = 'UPDATE' AND TG_NARGS > 0 THEN
                    -- Settings values are deliberately not copied into the
                    -- journal. A rewound retention policy needs explicit
                    -- reconciliation, even if its sequence is continuous.
                    sc := 'retention_review';
                ELSE
                    IF TG_OP = 'UPDATE' THEN
                        IF NEW.status <> 'deleted' THEN RETURN NULL; END IF;
                    END IF;
                    sc := 'account';
                    UPDATE app.inbound_updates SET payload = NULL WHERE account_id = aid;
                    UPDATE app.inbound_updates SET payload = NULL
                        WHERE account_id IS NULL AND
                        (CASE WHEN kind IN ('message', 'edited_message')
                            THEN payload->kind->'chat'->>'id'
                         WHEN kind = 'callback_query'
                            THEN coalesce(payload->kind->'from'->>'id',
                                          payload->kind->'from_user'->>'id')
                         END) IN (OLD.telegram_user_id::text, OLD.control_chat_id::text);
                END IF;
            ELSE
                IF TG_OP = 'DELETE' THEN aid := OLD.account_id;
                ELSE aid := NEW.account_id; END IF;
                SELECT recovery_id INTO arid FROM app.accounts WHERE id = aid;
                -- A cascading account delete already appended its everything tombstone.
                IF arid IS NULL THEN RETURN NULL; END IF;
                IF TG_TABLE_NAME = 'consents' THEN
                    IF NEW.revoked_at IS NULL OR OLD.revoked_at IS NOT NULL THEN RETURN NULL; END IF;
                    sc := CASE NEW.kind WHEN 'ai_control' THEN 'control' ELSE 'managed' END;
                    operation := 'revoke_' || NEW.kind;
                    processor := 'OpenAI'; decided := NEW.revoked_at;
                ELSIF TG_TABLE_NAME = 'audit_log' THEN
                    IF NEW.event = 'managed_data_deleted' THEN sc := 'managed';
                    ELSIF NEW.event = 'data_deleted' THEN
                        sc := NEW.details->>'scope';
                        IF sc = 'conversation' THEN
                            SELECT recovery_id INTO subject FROM app.conversations
                                WHERE account_id = aid AND id = NEW.subject_id;
                        ELSIF sc = 'contact' THEN
                            SELECT recovery_id INTO subject FROM app.contacts
                                WHERE account_id = aid AND id = NEW.subject_id;
                        END IF;
                    ELSE RETURN NULL; END IF;
                    decided := NEW.at;
                ELSIF TG_TABLE_NAME = 'messages' THEN
                    IF TG_OP = 'UPDATE' THEN
                        -- Explicit deletion wins even when expiry changes too.
                        IF OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN
                            decided := NEW.deleted_at;
                        ELSIF OLD.purged_at IS NULL AND NEW.purged_at IS NOT NULL THEN
                            operation := 'purge'; decided := NEW.purged_at;
                        ELSE
                            RETURN NULL;
                        END IF;
                    END IF;
                    sc := 'message'; subject := OLD.recovery_id;
                ELSIF TG_TABLE_NAME = 'memory_items' THEN
                    sc := 'memory'; subject := OLD.recovery_id;
                ELSIF TG_TABLE_NAME = 'notification_sources' THEN
                    sc := 'notification_source'; subject := OLD.recovery_id;
                END IF;
            END IF;
            PERFORM app.append_privacy_decision(aid, arid, sc, subject, operation, processor, decided);
            IF TG_WHEN = 'BEFORE' THEN RETURN OLD; END IF;
            RETURN NULL;
        END $$;

        CREATE TRIGGER privacy_account_insert AFTER INSERT ON app.accounts
            FOR EACH ROW EXECUTE FUNCTION app.capture_privacy_decision();
        CREATE TRIGGER privacy_account_delete BEFORE DELETE ON app.accounts
            FOR EACH ROW EXECUTE FUNCTION app.capture_privacy_decision();
        CREATE TRIGGER privacy_account_status AFTER UPDATE OF status ON app.accounts
            FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
            EXECUTE FUNCTION app.capture_privacy_decision();
        CREATE TRIGGER privacy_retention_review AFTER UPDATE OF settings ON app.accounts
            FOR EACH ROW WHEN (
                (OLD.settings->'retention_days') IS DISTINCT FROM (NEW.settings->'retention_days')
            ) EXECUTE FUNCTION app.capture_privacy_decision('retention');
        CREATE TRIGGER privacy_consent AFTER UPDATE OF revoked_at ON app.consents
            FOR EACH ROW EXECUTE FUNCTION app.capture_privacy_decision();
        CREATE TRIGGER privacy_delete_audit AFTER INSERT ON app.audit_log
            FOR EACH ROW EXECUTE FUNCTION app.capture_privacy_decision();
        CREATE TRIGGER privacy_message_delete AFTER DELETE ON app.messages
            FOR EACH ROW EXECUTE FUNCTION app.capture_privacy_decision();
        CREATE TRIGGER privacy_message_purge AFTER UPDATE OF deleted_at, purged_at ON app.messages
            FOR EACH ROW EXECUTE FUNCTION app.capture_privacy_decision();
        CREATE TRIGGER privacy_memory_delete AFTER DELETE ON app.memory_items
            FOR EACH ROW EXECUTE FUNCTION app.capture_privacy_decision();
        CREATE TRIGGER privacy_notification_source_delete AFTER DELETE ON app.notification_sources
            FOR EACH ROW EXECUTE FUNCTION app.capture_privacy_decision();
    """)


def downgrade() -> None:
    for table, triggers in (
        ("accounts", ("privacy_account_insert", "privacy_account_delete", "privacy_account_status", "privacy_retention_review")),
        ("consents", ("privacy_consent",)),
        ("audit_log", ("privacy_delete_audit",)),
        ("messages", ("privacy_message_delete", "privacy_message_purge")),
        ("memory_items", ("privacy_memory_delete",)),
        ("notification_sources", ("privacy_notification_source_delete",)),
    ):
        for trigger in triggers:
            op.execute(f"DROP TRIGGER {trigger} ON app.{table}")
    op.execute("DROP FUNCTION app.capture_privacy_decision()")
    op.execute("DROP FUNCTION app.append_privacy_decision(bigint, uuid, text, uuid, text, text, timestamptz)")
    op.drop_table("privacy_journal", schema="app")
    op.drop_table("privacy_journal_heads", schema="app")
    for table in reversed(IDENTITIES):
        op.drop_constraint(f"uq_{table}_recovery_id", table, type_="unique", schema="app")
        op.drop_column(table, "recovery_id", schema="app")
