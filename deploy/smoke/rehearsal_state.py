"""Synthetic state for the A4 restore rehearsal (deploy/smoke/restore-rehearsal.sh).

Runs inside a one-off container of the production app image against the
rehearsal stack's throwaway database. It never starts the bot, poller or
workers and never builds API clients. Every row is synthetic; output is
fixed check names, ids, counts and states only.

    python rehearsal_state.py seed --user ID      accounts, consents, chats, cards, a queued job
    python rehearsal_state.py after-backup --user ID
                                                  post-backup owner decisions and a completed send
    python rehearsal_state.py check --user ID --mode replay|purge
                                                  invariants after restore + reconciliation
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Every mapped model, as in migrations/env.py, so foreign keys resolve.
import assistant.actions.request_models
import assistant.agent.models
import assistant.automation.models
import assistant.inbox.models
import assistant.memory.models
import assistant.privacy.journal_models  # noqa: F401
from assistant.accounts import service
from assistant.accounts.models import Account, Consent
from assistant.actions.models import Action
from assistant.actions.states import ActionState
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import SystemClock
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.privacy.deletion import delete_conversation_data

PENDING = ("proposed", "awaiting_approval", "revising", "approved", "scheduled", "executing")
# Contacts/chat ids are offset from the synthetic owner id; never real ids.
DELETED_CHAT_OFFSET = 100
KEPT_CHAT_OFFSET = 200
Factory = async_sessionmaker[AsyncSession]


def now() -> datetime:
    return datetime.now(UTC)


async def _account(session: AsyncSession, user: int) -> Account:
    account = await session.scalar(select(Account).where(Account.telegram_user_id == user))
    if account is None:
        raise SystemExit(f"rehearsal_state_failed reason=unknown_account user={user}")
    return account


async def _conversation(session: AsyncSession, account_id: int, chat_id: int) -> Conversation:
    row = await session.scalar(
        select(Conversation).where(
            Conversation.account_id == account_id, Conversation.business_chat_id == chat_id
        )
    )
    if row is None:
        raise SystemExit("rehearsal_state_failed reason=unknown_conversation")
    return row


async def seed(factory: Factory, user: int) -> None:
    at = now()
    async with factory() as session, session.begin():
        account = Account(
            telegram_user_id=user,
            control_chat_id=user,
            status="active",
            timezone="UTC",
            policy_notice_version=service.POLICY_VERSION,
            created_at=at,
        )
        session.add(account)
        await session.flush()
        for kind in (service.CONSENT_AI_CONTROL, service.CONSENT_AI_MANAGED):
            session.add(
                Consent(
                    account_id=account.id,
                    kind=kind,
                    policy_version=service.POLICY_VERSION,
                    processors=list(service.AI_PROCESSORS),
                    granted_at=at,
                )
            )
        conversations: dict[str, Conversation] = {}
        for label, offset in (("deleted", DELETED_CHAT_OFFSET), ("kept", KEPT_CHAT_OFFSET)):
            contact = Contact(
                account_id=account.id,
                telegram_user_id=user + offset,
                display_name="Synthetic rehearsal contact",
                is_bot=False,
                first_seen_at=at,
                updated_at=at,
            )
            session.add(contact)
            await session.flush()
            conversation = Conversation(
                account_id=account.id, contact_id=contact.id, business_chat_id=user + offset
            )
            session.add(conversation)
            await session.flush()
            conversations[label] = conversation
            session.add(
                Message(
                    account_id=account.id,
                    conversation_id=conversation.id,
                    telegram_message_id=1,
                    direction="contact",
                    observed_via="new",
                    content_type="text",
                    text="Synthetic rehearsal text",
                    sent_at=at,
                    created_at=at,
                    updated_at=at,
                )
            )
        # One card per state on the kept chat; the unique live-card index
        # allows one revising row, which the other live states already cover.
        ids: dict[str, int] = {}
        for state in ActionState:
            if state is ActionState.REVISING:
                continue
            action = Action(
                account_id=account.id,
                conversation_id=conversations["kept"].id,
                kind="send_message",
                origin="agent_proposal",
                state=state.value,
                text="Synthetic rehearsal card",
                approval_token=f"rehearsal-{user}"
                if state is ActionState.AWAITING_APPROVAL
                else None,
                version=1,
                created_at=at,
                updated_at=at,
            )
            session.add(action)
            await session.flush()
            ids[state.value] = action.id
        # A queued job in the dump: post-restore must drop it (no replayed work).
        await session.execute(
            text(
                "INSERT INTO procrastinate.procrastinate_jobs (queue_name, task_name, args) "
                "VALUES ('rehearsal', 'rehearsal.synthetic', '{}'::jsonb)"
            )
        )
    print(
        f"seeded account={account.id} awaiting_action={ids['awaiting_approval']} "
        f"executing_action={ids['executing']} actions={len(ids)}"
    )


async def after_backup(factory: Factory, user: int) -> None:
    clock = SystemClock()
    async with factory() as session, session.begin():
        account = await _account(session, user)
        revoked = await service.revoke_consent(
            session, account, kind=service.CONSENT_AI_MANAGED, clock=clock
        )
        deleted = await _conversation(session, account.id, user + DELETED_CHAT_OFFSET)
        await delete_conversation_data(
            session, account_id=account.id, conversation_id=deleted.id, actor="owner", clock=clock
        )
        # The owner approved the card and it was delivered after the dump. The
        # restored copy still says awaiting_approval: it must never send again.
        card = await session.scalar(
            select(Action).where(
                Action.account_id == account.id, Action.state == ActionState.AWAITING_APPROVAL
            )
        )
        if card is None:
            raise SystemExit("rehearsal_state_failed reason=no_awaiting_card")
        card.state = ActionState.SUCCEEDED.value
        card.approval_token = None
        card.updated_at = now()
    print(f"after_backup revoked_managed={revoked} deleted_conversation=1 sent_action={card.id}")


async def check(factory: Factory, user: int, mode: str) -> int:
    results: list[tuple[str, bool]] = []
    async with factory() as session:
        account = await _account(session, user)
        gate = await session.get(SystemState, KEY_RESTORE_PENDING)
        results.append(("restore_gate_open", gate is None))
        states = dict(
            (
                await session.execute(
                    select(Action.state, func.count())
                    .where(Action.account_id == account.id)
                    .group_by(Action.state)
                )
            ).all()
        )
        results.append(("no_pending_or_executing_actions", not any(states.get(s) for s in PENDING)))
        reasons = dict(
            (
                await session.execute(
                    select(Action.state, Action.state_reason).where(
                        Action.account_id == account.id,
                        Action.state.in_(("cancelled", "delivery_uncertain")),
                        Action.state_reason == "restored_from_backup",
                    )
                )
            ).all()
        )
        # Restored executing card: uncertain, never retried. Restored
        # awaiting/approved/scheduled/proposed cards: cancelled, never sent.
        results.append(("restored_executing_is_uncertain", "delivery_uncertain" in reasons))
        results.append(("restored_pending_cards_cancelled", "cancelled" in reasons))
        results.append(
            (
                "no_approval_token_survives",
                not await session.scalar(
                    select(func.count())
                    .select_from(Action)
                    .where(Action.account_id == account.id, Action.approval_token.is_not(None))
                ),
            )
        )
        for kind in (service.CONSENT_AI_CONTROL, service.CONSENT_AI_MANAGED):
            state = await service.active_consent(session, account.id, kind=kind)
            results.append((f"{kind}_not_granted", not state.granted))
        jobs = await session.scalar(
            text(
                "SELECT count(*) FROM procrastinate.procrastinate_jobs "
                "WHERE status IN ('todo', 'doing')"
            )
        )
        results.append(("no_queued_jobs", jobs == 0))

        async def content(chat_offset: int) -> int:
            conversation = await _conversation(session, account.id, user + chat_offset)
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.conversation_id == conversation.id, Message.text.is_not(None))
                )
                or 0
            )

        results.append(("post_backup_deletion_applied", await content(DELETED_CHAT_OFFSET) == 0))
        kept = await content(KEPT_CHAT_OFFSET)
        if mode == "replay":
            # Replay is scoped: the untouched chat keeps its content.
            results.append(("unrelated_chat_content_kept", kept == 1))
        else:
            results.append(("complete_purge_cleared_all_content", kept == 0))
    failed = 0
    for name, passed in results:
        print(f"check {name} {'PASS' if passed else 'FAIL'}")
        failed += not passed
    return int(failed != 0)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("seed", "after-backup", "check"):
        command = sub.add_parser(name)
        command.add_argument("--user", type=int, required=True)
        if name == "check":
            command.add_argument("--mode", choices=("replay", "purge"), required=True)
    args = parser.parse_args()
    if not 7_000_000_000 <= args.user < 7_000_100_000:
        raise SystemExit("rehearsal_state_failed reason=non_synthetic_user")
    engine = build_engine(os.environ["DATABASE_URL"])
    factory = build_session_factory(engine)
    try:
        if args.command == "seed":
            await seed(factory, args.user)
        elif args.command == "after-backup":
            await after_backup(factory, args.user)
        else:
            return await check(factory, args.user, args.mode)
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    # Never use developer settings even if exported; nothing here needs them.
    os.environ.pop("BOT_TOKEN", None)
    os.environ.pop("OPENAI_API_KEY", None)
    sys.exit(asyncio.run(main()))
