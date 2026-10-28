"""No sequence of owner, contact and clock events sends twice or sends
unapproved text (S04.1 Â§5; invariants 1 and 6).

A hypothesis state machine drives one synthetic chat through proposals,
approvals (current and stale tokens), edits, rejections, contact and owner
messages and expiry, and executes approved actions through the real
executor (S04.4): Tx A, the gateway with `FakeTelegramSender` scripted per
attempt to answer OK, OK with a pending identity, 429, nothing (ambiguous)
or a refusal, then Tx B; `system.mark_uncertain` runs between steps. Same
invariants as S04.1's harness, plus: no `delivery_uncertain` action is ever
claimed again.

Hypothesis state machines are synchronous, so each machine owns an event
loop and a database engine. Derandomised and bounded; no wall clock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from assistant.accounts.models import AuditLogEntry
from assistant.actions.executor import ActionExecutor
from assistant.actions.lifecycle import (
    apply_new_version,
    approve,
    cancel_edit,
    expire_due,
    reject,
    start_edit,
    text_hash,
)
from assistant.actions.models import Action
from assistant.actions.policy_loader import KEY_INGESTION_BARRIER
from assistant.actions.proposals import propose
from assistant.actions.states import ActionOrigin, ActionState
from assistant.conversations.models import Conversation, Message
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import SystemState
from assistant.inbox.notify import OwnerAlerts
from assistant.telegram.business_targets import BusinessTargetResolver
from assistant.telegram.gateway import OutboundGateway
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock, FakeSleep
from tests.fakes.sender import FakeTelegramSender, network_error, telegram_error
from tests.integration.actions_env import CONFIG, Chat, add_message, request, seed_chat, seed_turn

pytestmark = pytest.mark.integration

T = TypeVar("T")
START = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
RESET = "TRUNCATE app.accounts, app.inbound_updates, app.system_state CASCADE"


@dataclass
class Sent:
    """An attempt Telegram may have delivered (it answered OK, or not at all)."""

    action_id: int
    version: int
    text: str
    claimed_at: datetime


#: What scripted Telegram answers an attempt with (S04.4 re-run).
TELEGRAM_OUTCOMES = ("ok", "pending_identity", "rate_limited", "ambiguous", "refused")
POSSIBLY_DELIVERED = frozenset({"ok", "pending_identity", "ambiguous"})


def scripted(outcome: str, message_id: int) -> Exception | int:
    match outcome:
        case "ok":
            return message_id
        case "pending_identity":
            return 0
        case "rate_limited":
            return telegram_error("Too Many Requests", status_code=429, retry_after=1)
        case "ambiguous":
            return network_error()
        case _:
            return telegram_error("Forbidden: bot was blocked by the user", status_code=403)


@dataclass
class Basis:
    """What the action was proposed against, for the staleness invariant."""

    shown_through: int | None
    takeover_ref: datetime


class ActionMachine(RuleBasedStateMachine):
    url: str = ""
    #: Sends across all examples: the machine must reach the claim, or the
    #: invariants would hold vacuously.
    total_sends: int = 0

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.engine: AsyncEngine = build_engine(self.url)
        self.factory: async_sessionmaker[AsyncSession] = build_session_factory(self.engine)
        self.clock = FakeClock(START)
        self.chat: Chat | None = None
        self.tokens: list[str] = []
        self.sends: list[Sent] = []
        self.basis: dict[int, Basis] = {}
        self.texts = 0
        # S04.4: the real executor, its gateway resolving real targets, and
        # Telegram scripted per attempt.
        self.sender = FakeTelegramSender()
        self.message_ids = 4_000_000
        self.executor = ActionExecutor(
            session_factory=self.factory,
            clock=self.clock,
            config=CONFIG,
            sender=OutboundGateway(
                self.sender,
                self.clock,
                sleep=FakeSleep(),
                targets=BusinessTargetResolver(self.factory),
            ),
            alerts=OwnerAlerts(self.factory, FakeChannel(), self.clock),
            scheduler=None,
        )

    def run(self, awaitable: Awaitable[T]) -> T:
        return self.loop.run_until_complete(awaitable)

    def teardown(self) -> None:
        ActionMachine.total_sends += len(self.sends)
        self.run(self.engine.dispose())
        self.loop.close()

    # ------------------------------------------------------------- set-up
    @initialize()
    def seed(self) -> None:
        async def go() -> Chat:
            async with self.factory() as session, session.begin():
                await session.execute(text(RESET))
                session.add(
                    SystemState(
                        key=KEY_INGESTION_BARRIER,
                        value={
                            "process_run_id": CONFIG.process_run_id,
                            "caught_up_at": START.isoformat(),
                        },
                    )
                )
                return await seed_chat(session, self.clock.now())

        self.chat = self.run(go())

    @property
    def c(self) -> Chat:
        assert self.chat is not None
        return self.chat

    def next_text(self) -> str:
        self.texts += 1
        return f"Synthetic reply number {self.texts}"

    async def current_token(self, session: AsyncSession) -> str | None:
        return await session.scalar(
            select(Action.approval_token).where(
                Action.conversation_id == self.c.conversation_id,
                Action.state == ActionState.AWAITING_APPROVAL.value,
            )
        )

    async def live(self, session: AsyncSession) -> Action | None:
        return await session.scalar(
            select(Action).where(
                Action.conversation_id == self.c.conversation_id,
                Action.state.in_(["awaiting_approval", "revising"]),
            )
        )

    # ------------------------------------------------------------- events
    @rule(dictated=st.booleans())
    def propose_reply(self, dictated: bool) -> None:
        self.run(self.proposal(dictated))

    @rule(dictated=st.booleans())
    def propose_and_approve(self, dictated: bool) -> None:
        """The common happy path, as one step, so short examples reach a send."""
        self.run(self.proposal(dictated))
        self.approve_card(stale=False, twice=False)

    async def proposal(self, dictated: bool) -> None:
        async with self.factory() as session, session.begin():
            # A draft answers the newest contact message, as S04.2's does.
            newest = await session.scalar(
                select(func.max(Message.id)).where(
                    Message.conversation_id == self.c.conversation_id,
                    Message.direction == "contact",
                )
            )
            fields: dict[str, Any] = {
                "text": self.next_text(),
                "base_message_id": newest,
                "shown_through_message_id": newest,
            }
            if dictated:
                # The owner's words are fresh through "now": `propose` fills it in.
                fields["shown_through_message_id"] = None
                fields["origin"] = ActionOrigin.OWNER_DICTATED
                fields["trigger_turn_id"] = await seed_turn(session, self.c.account_id)
            result = await propose(
                session, request(self.c, **fields), clock=self.clock, config=CONFIG
            )
            row = await session.get(Action, result.action_id)
            assert row is not None
            base_sent = await self.base_sent_at(session, row)
            self.basis[row.id] = Basis(
                shown_through=row.shown_through_message_id,
                takeover_ref=row.created_at if dictated or base_sent is None else base_sent,
            )
            if row.approval_token:
                self.tokens.append(row.approval_token)

    async def base_sent_at(self, session: AsyncSession, row: Action) -> datetime | None:
        if row.base_message_id is None:
            return None
        return await session.scalar(
            select(Message.sent_at).where(Message.id == row.base_message_id)
        )

    @rule(stale=st.booleans(), twice=st.booleans())
    def approve_card(self, stale: bool, twice: bool) -> None:
        async def go() -> None:
            for _ in range(2 if twice else 1):
                async with self.factory() as session, session.begin():
                    token = (
                        self.tokens[0]
                        if stale and self.tokens
                        else await self.current_token(session)
                    )
                    if token is None:
                        return
                    await approve(
                        session, account_id=self.c.account_id, token=token, now=self.clock.now()
                    )

        self.run(go())

    @rule()
    def edit_card(self) -> None:
        async def go() -> None:
            async with self.factory() as session, session.begin():
                token = await self.current_token(session)
                if token is not None:
                    await start_edit(
                        session, account_id=self.c.account_id, token=token, now=self.clock.now()
                    )

        self.run(go())

    @rule(stale=st.booleans(), cancel=st.booleans())
    def finish_edit(self, stale: bool, cancel: bool) -> None:
        async def go() -> None:
            async with self.factory() as session, session.begin():
                row = await self.live(session)
                if row is None or row.state != "revising":
                    return
                version = row.version + (1 if stale else 0)
                if cancel:
                    token = await cancel_edit(
                        session,
                        account_id=self.c.account_id,
                        action_id=row.id,
                        expected_version=version,
                        now=self.clock.now(),
                    )
                else:
                    result = await apply_new_version(
                        session,
                        account_id=self.c.account_id,
                        action_id=row.id,
                        expected_version=version,
                        text=self.next_text(),
                        draft_run_id=None,
                        draft_flags=None,
                        shown_through_message_id=row.shown_through_message_id,
                        base_message_id=row.base_message_id,
                        now=self.clock.now(),
                    )
                    token = result.token
                if token is not None:
                    self.tokens.append(token)

        self.run(go())

    @rule()
    def reject_card(self) -> None:
        async def go() -> None:
            async with self.factory() as session, session.begin():
                token = await self.current_token(session)
                if token is not None:
                    await reject(
                        session, account_id=self.c.account_id, token=token, now=self.clock.now()
                    )

        self.run(go())

    @rule()
    def contact_writes(self) -> None:
        self.clock.advance(seconds=30)
        self.run(self.message("contact", self.clock.now()))

    @rule(after_base=st.booleans())
    def owner_writes(self, after_base: bool) -> None:
        self.clock.advance(seconds=30)
        sent_at = self.clock.now() if after_base else START - timedelta(hours=2)
        self.run(self.message("owner_manual", sent_at))

    async def message(self, direction: str, sent_at: datetime) -> None:
        async with self.factory() as session, session.begin():
            await add_message(
                session,
                self.c.account_id,
                self.c.conversation_id,
                direction=direction,
                sent_at=sent_at,
            )
            if direction == "contact":
                conversation = await session.get(Conversation, self.c.conversation_id)
                assert conversation is not None
                conversation.last_contact_msg_at = sent_at

    @rule(hours=st.sampled_from([1, 4, 8]))
    def time_passes(self, hours: int) -> None:
        self.clock.advance(hours=hours)

        async def go() -> None:
            async with self.factory() as session, session.begin():
                await expire_due(session, now=self.clock.now())

        self.run(go())

    @rule(outcome=st.sampled_from(TELEGRAM_OUTCOMES))
    def execute_sends(self, outcome: str) -> None:
        self.run(self.execute_approved(outcome))

    @rule()
    def sweep_uncertain(self) -> None:
        """`system.mark_uncertain` after its threshold."""
        self.clock.advance(seconds=CONFIG.mark_uncertain_after.total_seconds() + 1)
        self.run(self.executor.mark_uncertain())

    async def execute_approved(self, outcome: str) -> None:
        """The real executor (S04.4) against Telegram scripted to answer
        `outcome`: a send, a 429, no answer, or a definite refusal."""
        async with self.factory() as session:
            approved = list(
                await session.scalars(
                    select(Action.id).where(
                        Action.conversation_id == self.c.conversation_id,
                        Action.state == ActionState.APPROVED.value,
                    )
                )
            )
        for action_id in approved:
            async with self.factory() as session:
                version = await session.scalar(select(Action.version).where(Action.id == action_id))
            before = len(self.sender.plain_sends)
            self.message_ids += 1
            self.sender.plain_script = [scripted(outcome, self.message_ids)]
            await self.executor.execute(action_id)
            self.sender.plain_script = []
            if len(self.sender.plain_sends) > before and outcome in POSSIBLY_DELIVERED:
                # Telegram may hold it: from here on this action counts as sent.
                text = self.sender.plain_sends[-1]["text"]
                assert version is not None
                self.sends.append(Sent(action_id, version, text, self.clock.now()))

    # ------------------------------------------------------------ invariants
    @invariant()
    def never_twice_never_unapproved_never_stale(self) -> None:
        if self.chat is None or not self.sends:
            return

        async def check() -> None:

            ids = [sent.action_id for sent in self.sends]
            assert len(ids) == len(set(ids)), "an action was sent twice"
            async with self.factory() as session:
                for sent in self.sends:
                    row = await session.get(Action, sent.action_id)
                    assert row is not None
                    assert row.approved_text_hash == text_hash(sent.text), "unapproved text sent"
                    approvals = await session.scalars(
                        select(AuditLogEntry.details).where(
                            AuditLogEntry.subject_type == "action",
                            AuditLogEntry.subject_id == sent.action_id,
                            AuditLogEntry.event == "action_approved",
                        )
                    )
                    assert sent.version in [details["version"] for details in approvals]
                    claimed_at = sent.claimed_at
                    basis = self.basis[sent.action_id]
                    newer_contact = await session.scalar(
                        select(Message.id)
                        .where(
                            Message.conversation_id == self.c.conversation_id,
                            Message.direction == "contact",
                            Message.id > (basis.shown_through or 0),
                        )
                        .order_by(Message.id)
                        .limit(1)
                    )
                    owner_after = await session.scalar(
                        select(Message.id)
                        .where(
                            Message.conversation_id == self.c.conversation_id,
                            Message.direction == "owner_manual",
                            Message.sent_at > basis.takeover_ref,
                            Message.sent_at <= (claimed_at or self.clock.now()),
                        )
                        .limit(1)
                    )
                    assert owner_after is None, "sent after the owner took over"
                    if newer_contact is not None:
                        # Only a contact message ingested after the claim is allowed.
                        message_at = await session.scalar(
                            select(Message.sent_at).where(Message.id == newer_contact)
                        )
                        assert message_at is not None
                        assert message_at > claimed_at, "sent over a newer contact message"

        self.run(check())

    @invariant()
    def no_uncertain_action_is_ever_sent_again(self) -> None:
        """S04.4: once an attempt is `delivery_uncertain`, nothing claims
        that action again (invariant 6)."""
        if self.chat is None:
            return

        async def check() -> None:
            async with self.factory() as session:
                rows = (
                    await session.execute(
                        select(AuditLogEntry.subject_id, AuditLogEntry.id, AuditLogEntry.event)
                        .where(
                            AuditLogEntry.subject_type == "action",
                            AuditLogEntry.event.in_(
                                ["action_delivery_uncertain", "action_claimed"]
                            ),
                        )
                        .order_by(AuditLogEntry.id)
                    )
                ).all()
            uncertain: set[int] = set()
            for action_id, _, event in rows:
                if event == "action_delivery_uncertain":
                    uncertain.add(action_id)
                else:
                    assert action_id not in uncertain, "an uncertain action was claimed again"

        self.run(check())


def test_no_event_sequence_sends_twice_or_sends_unapproved_text(migrated_url: str) -> None:
    ActionMachine.url = migrated_url
    ActionMachine.total_sends = 0
    run_state_machine_as_test(
        ActionMachine,
        settings=settings(
            derandomize=True,
            max_examples=20,
            stateful_step_count=20,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        ),
    )
    assert ActionMachine.total_sends > 0, "no example reached a send"
