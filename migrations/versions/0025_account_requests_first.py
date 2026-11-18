"""Delete an account's owner requests before its actions.

An action and its owner request can reference each other (`actions.
owner_request_id`, `owner_requests.action_id`). On a physical account
delete, the actions cascade used to delete the requests from inside
`unlink_owner_request_action` (a BEFORE DELETE trigger on actions); the
requests' `ON DELETE SET NULL` then updated actions that the same cascade
was still to delete, and PostgreSQL refused the statement. Removing the
requests in a BEFORE DELETE trigger on the account finishes that SET NULL
in an earlier command than the actions cascade. Structural only: no column,
no content.

Revision ID: 0025
Revises: 0024
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

# Older executables neither need nor observe the trigger; it only lets the
# existing account cascade finish.
COMPATIBILITY = "expand"


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION app.delete_account_owner_requests() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM app.owner_requests WHERE account_id = OLD.id;
            RETURN OLD;
        END;
        $$
    """)
    # Named after `privacy_account_delete` so the journal trigger runs first.
    op.execute("""
        CREATE TRIGGER unlink_account_owner_requests
        BEFORE DELETE ON app.accounts
        FOR EACH ROW EXECUTE FUNCTION app.delete_account_owner_requests()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER unlink_account_owner_requests ON app.accounts")
    op.execute("DROP FUNCTION app.delete_account_owner_requests()")
