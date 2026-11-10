"""Content-free, offline server commands for I20. Never creates API clients."""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.config import Settings
from assistant.core.clock import SystemClock
from assistant.core.errors import NotInAccountError
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.db.uow import transaction
from assistant.privacy import journal


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("replay")
    purge = sub.add_parser("purge")
    purge.add_argument("--restore-id", type=UUID, required=True)
    manual = sub.add_parser("manual-complete")
    manual.add_argument("--restore-id", type=UUID, required=True)
    manual.add_argument("--account-id", type=int, action="append", required=True)
    manual.add_argument("--owner-confirmed", action="store_true", required=True)
    deletion = sub.add_parser("delete")
    deletion.add_argument("--restore-id", type=UUID, required=True)
    deletion.add_argument("--account-id", type=int, required=True)
    deletion.add_argument(
        "--scope", choices=("conversation", "contact", "managed", "everything"), required=True
    )
    deletion.add_argument("--subject-id", type=int)
    prune = sub.add_parser("reset-after-prune")
    prune.add_argument("--inventory-receipt", type=UUID, required=True)
    prune.add_argument("--copies-destroyed", action="store_true", required=True)
    retention = sub.add_parser("retention")
    retention.add_argument("--restore-id", type=UUID, required=True)
    retention.add_argument("--account-id", type=int, required=True)
    retention.add_argument("--messages-days", type=int)
    retention.add_argument("--control-chat-days", type=int)
    retention.add_argument("--raw-updates-days", type=int)
    return result


async def run(args: argparse.Namespace, raw: str | None) -> int:
    settings = Settings()  # type: ignore[call-arg]
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    clock = SystemClock()
    try:
        if args.command == "status":
            async with factory() as session:
                marker = await session.get(SystemState, KEY_RESTORE_PENDING)
                if marker is not None:
                    try:
                        restore_id = str(UUID(str(marker.value.get("restore_id"))))
                    except ValueError:
                        restore_id = "unknown"
                    print(f"restore_id: {restore_id}")
                    print("privacy_reconciliation_required")
                blocked = await session.get(SystemState, "privacy_journal_upload_blocked")
                if marker is not None:
                    print("journal_upload: restore_pending")
                elif blocked is not None:
                    print("journal_upload: copy_inventory_required")
                else:
                    print("journal_upload: check_sidecar_acknowledgement")
                heads = await journal.anchors(session)
                print(f"journal_accounts: {len(heads)}")
        elif args.command == "replay":
            if raw is None:
                raise journal.JournalRecoveryError("journal_missing")
            segment = journal.JournalSegment.model_validate_json(raw)
            async with factory() as session:
                if await session.get(SystemState, KEY_RESTORE_PENDING) is None:
                    print("privacy_replay_not_required; restore_gate_already_absent")
                    return 0
            await journal.reconcile(factory, clock, segment=segment)
            print("privacy_reconciliation_complete; fresh_consent_required")
        elif args.command == "purge":
            await journal.complete_purge(factory, clock, restore_id=args.restore_id)
            print("privacy_purge_complete; fresh_consent_required; journal_shipping_blocked")
        elif args.command == "manual-complete":
            await journal.complete_manual(
                factory,
                clock,
                restore_id=args.restore_id,
                account_ids=frozenset(args.account_id),
                owner_confirmed=args.owner_confirmed,
            )
            print("privacy_manual_reconciliation_complete; fresh_consent_required")
        elif args.command == "reset-after-prune":
            await journal.reset_after_prune(
                factory,
                clock,
                inventory_receipt=args.inventory_receipt,
                copies_destroyed=args.copies_destroyed,
            )
            print("privacy_journal_reset; check_sidecar_acknowledgement")
        elif args.command == "retention":
            await _retention(args, factory)
            print("privacy_retention_updated; reconciliation_still_required")
        else:
            await _delete(args, factory, clock)
            print("privacy_local_deletion_complete; reconciliation_still_required")
    finally:
        await engine.dispose()
    return 0


async def _delete(
    args: argparse.Namespace,
    factory: async_sessionmaker[AsyncSession],
    clock: SystemClock,
) -> None:
    from assistant.privacy import deletion

    async with transaction(factory) as uow:
        marker = await uow.session.get(SystemState, KEY_RESTORE_PENDING)
        if marker is None or marker.value.get("restore_id") != str(args.restore_id):
            raise journal.JournalRecoveryError("restore_identity_mismatch")
        if args.scope == "conversation":
            if args.subject_id is None:
                raise journal.JournalRecoveryError("privacy_subject_required")
            await deletion.delete_conversation_data(
                uow.session,
                account_id=args.account_id,
                conversation_id=args.subject_id,
                actor="owner",
                clock=clock,
            )
        elif args.scope == "contact":
            if args.subject_id is None:
                raise journal.JournalRecoveryError("privacy_subject_required")
            await deletion.delete_contact_data(
                uow.session,
                account_id=args.account_id,
                contact_id=args.subject_id,
                actor="owner",
                clock=clock,
            )
        elif args.scope == "managed":
            await deletion.delete_managed_data(uow.session, account_id=args.account_id, clock=clock)
        else:
            await deletion.delete_everything(
                uow.session, account_id=args.account_id, actor="owner", clock=clock
            )


async def _retention(args: argparse.Namespace, factory: async_sessionmaker[AsyncSession]) -> None:
    from assistant.privacy import retention_policy

    values = {
        retention_policy.MESSAGES: args.messages_days,
        retention_policy.CONTROL_CHAT: args.control_chat_days,
        retention_policy.RAW_UPDATES: args.raw_updates_days,
    }
    if all(value is None for value in values.values()):
        raise journal.JournalRecoveryError("privacy_retention_values_required")
    async with transaction(factory) as uow:
        marker = await uow.session.get(SystemState, KEY_RESTORE_PENDING, with_for_update=True)
        if marker is None or marker.value.get("restore_id") != str(args.restore_id):
            raise journal.JournalRecoveryError("restore_identity_mismatch")
        account = await uow.session.get(Account, args.account_id, with_for_update=True)
        if account is None:
            raise NotInAccountError("unknown account")
        settings = dict(account.settings)
        for key, value in values.items():
            if value is not None:
                settings = retention_policy.with_override(settings, key, value)
        account.settings = settings


def main() -> int:
    args = parser().parse_args()
    # Read stdin before entering the event loop. No blocking I/O in async code.
    raw = sys.stdin.read() if args.command == "replay" else None
    try:
        return asyncio.run(run(args, raw))
    except journal.JournalRecoveryError as error:
        print(f"blocked: {error}", file=sys.stderr)
    except ValidationError:
        print("blocked: journal_or_configuration_invalid", file=sys.stderr)
    except ValueError:
        print("blocked: privacy_recovery_input_invalid", file=sys.stderr)
    except SQLAlchemyError, OSError, NotInAccountError:
        print("blocked: privacy_recovery_failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
