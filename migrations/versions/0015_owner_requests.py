"""Durable owner request references; no copied content or runtime activation.

Hand-written. Downgrade drops request/source metadata only; existing control
turns and actions survive. Source content retention is never extended.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-09
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.owner_requests (
            id bigint GENERATED ALWAYS AS IDENTITY CONSTRAINT pk_owner_requests PRIMARY KEY,
            account_id bigint NOT NULL CONSTRAINT fk_owner_requests_account_id_accounts
                REFERENCES app.accounts(id) ON DELETE CASCADE,
            version integer NOT NULL DEFAULT 1 CONSTRAINT ck_owner_requests_version
                CHECK (version >= 1),
            operation text NOT NULL CONSTRAINT ck_owner_requests_operation
                CHECK (operation IN ('dictate','compose','reply','revise')),
            state text NOT NULL CONSTRAINT ck_owner_requests_state CHECK (state IN
                ('collecting','ready','preparing','awaiting_approval','suspended',
                 'completed','cancelled','superseded','expired')),
            focused boolean NOT NULL DEFAULT false,
            trigger_turn_id bigint NOT NULL
                CONSTRAINT fk_owner_requests_trigger_turn_id_control_turns
                REFERENCES app.control_turns(id) ON DELETE CASCADE,
            last_turn_id bigint NOT NULL CONSTRAINT fk_owner_requests_last_turn_id_control_turns
                REFERENCES app.control_turns(id) ON DELETE CASCADE,
            contact_id bigint CONSTRAINT fk_owner_requests_contact_id_contacts
                REFERENCES app.contacts(id) ON DELETE CASCADE,
            conversation_id bigint CONSTRAINT fk_owner_requests_conversation_id_conversations
                REFERENCES app.conversations(id) ON DELETE CASCADE,
            selection_basis text CONSTRAINT ck_owner_requests_selection_basis CHECK
                (selection_basis IN ('unique_strong','owner_choice','explicit_reply')),
            missing_slot text CONSTRAINT ck_owner_requests_missing_slot CHECK
                (missing_slot IN ('recipient','content','target','constraints')),
            question_input_id bigint CONSTRAINT fk_owner_requests_question_input_id_pending_inputs
                REFERENCES app.pending_inputs(id) ON DELETE SET NULL,
            action_id bigint CONSTRAINT fk_owner_requests_action_id_actions
                REFERENCES app.actions(id) ON DELETE SET NULL,
            action_version integer,
            target_message_id bigint CONSTRAINT fk_owner_requests_target_message_id_messages
                REFERENCES app.messages(id) ON DELETE SET NULL,
            constraints jsonb NOT NULL DEFAULT '{}',
            requested_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            ended_at timestamptz,
            reason text CONSTRAINT ck_owner_requests_reason CHECK
                (reason IN ('owner_cancelled','topic_switch','source_unavailable',
                'recipient_changed','superseded','max_age','action_terminal',
                'restored_from_backup')),
            CONSTRAINT uq_owner_requests_account_id_trigger_turn_id
                UNIQUE (account_id, trigger_turn_id),
            CONSTRAINT ck_owner_requests_recipient_pair CHECK (
                (contact_id IS NULL) = (conversation_id IS NULL) AND
                (contact_id IS NULL) = (selection_basis IS NULL)),
            CONSTRAINT ck_owner_requests_action_pair CHECK (
                (action_id IS NULL) = (action_version IS NULL) AND action_version >= 1),
            CONSTRAINT ck_owner_requests_question_state CHECK (
                missing_slot IS NULL OR state IN ('collecting','suspended')),
            CONSTRAINT ck_owner_requests_terminal_focus CHECK (
                NOT focused OR state NOT IN ('completed','cancelled','superseded','expired')),
            CONSTRAINT ck_owner_requests_ended_state CHECK (
                (ended_at IS NOT NULL) =
                (state IN ('completed','cancelled','superseded','expired')))
        )
        """,
    )
    op.execute(
        """
        CREATE TABLE app.owner_request_sources (
            id bigint GENERATED ALWAYS AS IDENTITY CONSTRAINT pk_owner_request_sources PRIMARY KEY,
            account_id bigint NOT NULL CONSTRAINT fk_owner_request_sources_account_id_accounts
                REFERENCES app.accounts(id) ON DELETE CASCADE,
            request_id bigint NOT NULL CONSTRAINT fk_owner_request_sources_request_id_owner_requests
                REFERENCES app.owner_requests(id) ON DELETE CASCADE,
            ordinal integer NOT NULL CONSTRAINT ck_owner_request_sources_ordinal
                CHECK (ordinal >= 0 AND ordinal < 64),
            turn_id bigint NOT NULL CONSTRAINT fk_owner_request_sources_turn_id_control_turns
                REFERENCES app.control_turns(id) ON DELETE CASCADE,
            field text NOT NULL CONSTRAINT ck_owner_request_sources_field
                CHECK (field IN ('text','quoted_text')),
            purpose text NOT NULL CONSTRAINT ck_owner_request_sources_purpose CHECK
                (purpose IN ('instruction','literal_body','constraint','quote','suggestion')),
            start_char integer NOT NULL,
            end_char integer NOT NULL,
            field_sha256 text NOT NULL CONSTRAINT ck_owner_request_sources_sha256
                CHECK (field_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_owner_request_sources_bounds
                CHECK (start_char >= 0 AND end_char > start_char),
            CONSTRAINT ck_owner_request_sources_quoted_field
                CHECK (field != 'quoted_text' OR purpose = 'quote'),
            CONSTRAINT uq_owner_request_sources_account_id_request_id_ordinal
                UNIQUE (account_id, request_id, ordinal)
        )
        """,
    )
    op.create_index("ix_owner_requests_account_id", "owner_requests", ["account_id"], schema="app")
    op.create_index(
        "ix_owner_requests_account_id_expires_at",
        "owner_requests",
        ["account_id", "expires_at"],
        schema="app",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_owner_requests_focused "
        "ON app.owner_requests(account_id) WHERE focused"
    )
    op.create_index(
        "ix_owner_request_sources_account_id", "owner_request_sources", ["account_id"], schema="app"
    )
    op.create_index(
        "ix_owner_request_sources_account_id_turn_id",
        "owner_request_sources",
        ["account_id", "turn_id"],
        schema="app",
    )
    # Account CASCADE can reach actions before owner_requests. Finish the
    # request cascade first when its parent is gone: updating it would fail
    # the account FK, and SET NULL alone would fail the paired-link CHECK.
    op.execute(
        """
        CREATE FUNCTION app.unlink_owner_request_action() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM app.accounts WHERE id = OLD.account_id) THEN
                DELETE FROM app.owner_requests WHERE account_id = OLD.account_id;
                RETURN OLD;
            END IF;
            UPDATE app.owner_requests SET action_id = NULL, action_version = NULL,
                version = version + 1
            WHERE account_id = OLD.account_id AND action_id = OLD.id;
            RETURN OLD;
        END
        $$
        """,
    )
    op.execute(
        "CREATE TRIGGER unlink_owner_request_action BEFORE DELETE ON app.actions "
        "FOR EACH ROW EXECUTE FUNCTION app.unlink_owner_request_action()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER unlink_owner_request_action ON app.actions")
    op.execute("DROP FUNCTION app.unlink_owner_request_action()")
    op.drop_table("owner_request_sources", schema="app")
    op.drop_table("owner_requests", schema="app")
