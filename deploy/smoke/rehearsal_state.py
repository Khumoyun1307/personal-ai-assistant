"""Synthetic state for the A4 restore rehearsal (deploy/smoke/restore-rehearsal.sh).

Runs inside a one-off container of the production app image against the
rehearsal stack's throwaway database. It never starts the bot, poller or
workers and never builds API clients. Every row is synthetic; output is
fixed check names, ids, counts and states only.

    python rehearsal_state.py seed --user ID          account, consents, chats, a queued job
    python rehearsal_state.py cards --user ID --round N
                                                      one card per state + a D-S17 warning card;
                                                      prints `cards <state>=<id> ...`
    python rehearsal_state.py after-backup --user ID --sent ID
                                                      post-backup owner decisions; card ID sent
    python rehearsal_state.py check --user ID --mode replay|purge --cards "<cards line>"
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
from assistant.actions.lifecycle import is_warning_card
from assistant.actions.models import Action
from assistant.actions.states import ActionState
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import SystemClock
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.privacy.deletion import delete_conversation_data
from assistant.privacy.post_restore import KEY_RESTORE_NOTICE

PENDING = ("proposed", "awaiting_approval", "revising", "approved", "scheduled", "executing")
# Cards that post-restore must cancel (never send), and the one it must settle
# as uncertain (never re-execute). `warning` is an OUTDATED D-S17 warning card
# that still carries an approval token.
CANCELLED_KEYS = ("proposed", "awaiting_approval", "approved", "scheduled", "warning")
UNCERTAIN_KEY = "executing"
# Contacts/chat ids are offset from the synthetic owner id; never real ids.
DELETED_CHAT_OFFSET = 100
KEPT_CHAT_OFFSET = 200
CARDS_CHAT_OFFSET = 300
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


async def _chat(session: AsyncSession, account_id: int, chat_id: int, at: datetime) -> Conversation:
    contact = Contact(
        account_id=account_id,
        telegram_user_id=chat_id,
        display_name="Synthetic rehearsal contact",
        is_bot=False,
        first_seen_at=at,
        updated_at=at,
    )
    session.add(contact)
    await session.flush()
    conversation = Conversation(
        account_id=account_id, contact_id=contact.id, business_chat_id=chat_id
    )
    session.add(conversation)
    await session.flush()
    return conversation


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
        for offset in (DELETED_CHAT_OFFSET, KEPT_CHAT_OFFSET):
            conversation = await _chat(session, account.id, user + offset, at)
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
        # A queued job in the dump: post-restore must drop it (no replayed work).
        await session.execute(
            text(
                "INSERT INTO procrastinate.procrastinate_jobs (queue_name, task_name, args) "
                "VALUES ('rehearsal', 'rehearsal.synthetic', '{}'::jsonb)"
            )
        )
    print(f"seeded account={account.id}")


async def cards(factory: Factory, user: int, round_: int) -> None:
    """A fresh card set on its own chat, so each restore checks new cards."""
    at = now()
    ids: dict[str, int] = {}
    async with factory() as session, session.begin():
        account = await _account(session, user)
        chat = await _chat(session, account.id, user + CARDS_CHAT_OFFSET + round_, at)
        # One card per state; the unique live-card index allows one revising
        # row, which the other live states already cover.
        for state in ActionState:
            if state is ActionState.REVISING:
                continue
            ids[state.value] = await _card(session, account.id, chat.id, state, at, user, round_)
        ids["warning"] = await _card(
            session, account.id, chat.id, ActionState.OUTDATED, at, user, round_, warning=True
        )
        warning = await session.get(Action, ids["warning"])
        if warning is None or not is_warning_card(warning):
            raise SystemExit("rehearsal_state_failed reason=warning_card_not_seeded")
    print("cards " + " ".join(f"{key}={value}" for key, value in sorted(ids.items())))


async def _card(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    state: ActionState,
    at: datetime,
    user: int,
    round_: int,
    *,
    warning: bool = False,
) -> int:
    token = None
    if state is ActionState.AWAITING_APPROVAL:
        token = f"rehearsal-{user}-{round_}"
    elif warning:
        token = f"rehearsal-warning-{user}-{round_}"
    action = Action(
        account_id=account_id,
        conversation_id=conversation_id,
        kind="send_message",
        origin="agent_proposal",
        state=state.value,
        state_reason="source_edited" if warning else None,
        freshness_warning="source_edited" if warning else None,
        text="Synthetic rehearsal card",
        approval_token=token,
        version=1,
        created_at=at,
        updated_at=at,
    )
    session.add(action)
    await session.flush()
    return action.id


async def after_backup(factory: Factory, user: int, sent: int) -> None:
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
        # The owner approved this card and it was delivered after the dump. The
        # restored copy still says awaiting_approval: it must never send again.
        card = await session.get(Action, sent)
        if card is None or card.account_id != account.id or card.state != "awaiting_approval":
            raise SystemExit("rehearsal_state_failed reason=no_awaiting_card")
        card.state = ActionState.SUCCEEDED.value
        card.approval_token = None
        card.updated_at = now()
    print(f"after_backup revoked_managed={revoked} deleted_conversation=1 sent_action={sent}")


def parse_cards(line: str) -> dict[str, int]:
    pairs = dict(item.split("=", 1) for item in line.removeprefix("cards ").split())
    return {key: int(value) for key, value in pairs.items()}


async def check(factory: Factory, user: int, mode: str, seeded: dict[str, int]) -> int:
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
        notice = await session.get(SystemState, KEY_RESTORE_NOTICE)
        listed = (notice.value if notice else {}).get("actions", {}).get(str(account.id), {})
        noticed = {
            kind: {entry.get("id") for entry in listed.get(kind, [])}
            for kind in ("cancelled", "uncertain")
        }
        # Each seeded card by id: retired with the restore reason, no token,
        # no approval path, and named in the owner's restore notice.
        for key in (*CANCELLED_KEYS, UNCERTAIN_KEY):
            card = await session.get(Action, seeded[key])
            expected, kind = (
                ("delivery_uncertain", "uncertain")
                if key == UNCERTAIN_KEY
                else ("cancelled", "cancelled")
            )
            results.append(
                (
                    f"card_{key}_{expected}_no_token_in_notice",
                    card is not None
                    and card.account_id == account.id
                    and card.state == expected
                    and card.state_reason == "restored_from_backup"
                    and card.approval_token is None
                    and not is_warning_card(card)
                    and card.id in noticed[kind],
                )
            )
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
    for name in ("seed", "cards", "after-backup", "check"):
        command = sub.add_parser(name)
        command.add_argument("--user", type=int, required=True)
        if name == "cards":
            command.add_argument("--round", type=int, required=True)
        if name == "after-backup":
            command.add_argument("--sent", type=int, required=True)
        if name == "check":
            command.add_argument("--mode", choices=("replay", "purge"), required=True)
            command.add_argument("--cards", required=True)
    args = parser.parse_args()
    if not 7_000_000_000 <= args.user < 7_000_100_000:
        raise SystemExit("rehearsal_state_failed reason=non_synthetic_user")
    engine = build_engine(os.environ["DATABASE_URL"])
    factory = build_session_factory(engine)
    try:
        if args.command == "seed":
            await seed(factory, args.user)
        elif args.command == "cards":
            await cards(factory, args.user, args.round)
        elif args.command == "after-backup":
            await after_backup(factory, args.user, args.sent)
        else:
            return await check(factory, args.user, args.mode, parse_cards(args.cards))
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    # Never use developer settings even if exported; nothing here needs them.
    os.environ.pop("BOT_TOKEN", None)
    os.environ.pop("OPENAI_API_KEY", None)
    sys.exit(asyncio.run(main()))
