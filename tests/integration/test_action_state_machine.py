"""No sequence of owner, contact and clock events sends twice or sends
unapproved text (S04.1 §5; invariants 1 and 6).

A hypothesis state machine drives one synthetic chat through proposals,
approvals (current and stale tokens), edits, rejections, contact and owner
messages and expiry, and executes approved actions through a minimal Tx A
harness: the §9.4 locks, `load_context` + `decide`, the conditional claim
`approved → executing`, a recorded "send", then `executing → succeeded`.
S04.4 swaps the harness for the real executor and re-runs this machine.

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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, AuditLogEntry
from assistant.actions.lifecycle import (
    apply_new_version,
    approve,
    cancel_edit,
    expire_due,
    reject,
    sendable_length,
    start_edit,
    text_hash,
    transition,
)
from assistant.actions.models import Action
from assistant.actions.policy_loader import KEY_INGESTION_BARRIER, load_context
from assistant.actions.proposals import propose
from assistant.actions.states import ActionKind, ActionOrigin, ActionState
from assistant.conversations.models import Conversation, Message
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import SystemState
from assistant.policy.context import ActionFacts, Outcome, Phase
from assistant.policy.engine import decide, execution_target
from tests.fakes.clock import FakeClock
from tests.integration.actions_env import CONFIG, Chat, add_message, request, seed_chat, seed_turn

pytestmark = pytest.mark.integration

T = TypeVar("T")
START = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
RESET = "TRUNCATE app.accounts, app.inbound_updates, app.system_state CASCADE"


@dataclass
class Sent:
    action_id: int
    version: int
    text: str


@dataclass
class Basis:
    """What the action was proposed against, for the staleness invariant."""

    shown_through: int | None
    takeover_ref: datetime


class ActionMachine(RuleBasedStateMachine):
    url: str = ""

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

    def run(self, awaitable: Awaitable[T]) -> T:
        return self.loop.run_until_complete(awaitable)

    def teardown(self) -> None:
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
        async def go() -> None:
            async with self.factory() as session, session.begin():
                fields: dict[str, Any] = {"text": self.next_text()}
                if dictated:
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

        self.run(go())

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

    @rule()
    def execute_sends(self) -> None:
        self.run(self.execute_approved())

    async def execute_approved(self) -> None:
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
            claimed = await self.tx_a(action_id)
            if claimed is None:
                continue
            self.sends.append(claimed)  # the "send", outside any transaction
            async with self.factory() as session, session.begin():
                await transition(
                    session,
                    account_id=self.c.account_id,
                    action_id=action_id,
                    from_state=ActionState.EXECUTING,
                    to_state=ActionState.SUCCEEDED,
                    reason=None,
                    now=self.clock.now(),
                    values={"attempt_id": "attempt"},
                )

    async def tx_a(self, action_id: int) -> Sent | None:
        """The claim: §9.4 lock order, then policy, then a conditional update."""
        now = self.clock.now()
        async with self.factory() as session, session.begin():
            await session.execute(
                select(Account.id).where(Account.id == self.c.account_id).with_for_update(read=True)
            )
            await session.execute(
                select(Conversation.id)
                .where(Conversation.id == self.c.conversation_id)
                .with_for_update(read=True)
            )
            row = await session.scalar(
                select(Action).where(Action.id == action_id).with_for_update()
            )
            if row is None or row.state != ActionState.APPROVED.value or row.text is None:
                return None
            facts = ActionFacts(
                action_id=row.id,
                kind=ActionKind(row.kind),
                origin=ActionOrigin(row.origin),
                state=ActionState(row.state),
                version=row.version,
                text_length=sendable_length(row.text),
                created_at=row.created_at,
                expires_at=row.expires_at,
                shown_through_message_id=row.shown_through_message_id,
                owner_retry=False,
            )
            context = await load_context(
                session,
                account_id=self.c.account_id,
                conversation_id=self.c.conversation_id,
                facts=facts,
                base_message_id=row.base_message_id,
                target_message_id=None,
                owner_requested=row.origin == "owner_dictated" or row.trigger_turn_id is not None,
                phase=Phase.EXECUTION,
                now=now,
                config=CONFIG,
                ai_generated=row.origin == "agent_proposal",
                process_run_id=CONFIG.process_run_id,
            )
            decision = decide(context)
            if decision.outcome is not Outcome.ALLOW_AUTO:
                target = execution_target(decision)
                if target is not None:
                    await transition(
                        session,
                        account_id=self.c.account_id,
                        action_id=row.id,
                        from_state=ActionState.APPROVED,
                        to_state=target[0],
                        reason=target[1],
                        now=now,
                    )
                return None
            if not await transition(
                session,
                account_id=self.c.account_id,
                action_id=row.id,
                from_state=ActionState.APPROVED,
                to_state=ActionState.EXECUTING,
                reason=None,
                now=now,
                expected_version=row.version,
                values={"attempt_id": "attempt", "attempt_started_at": now},
            ):
                return None
            return Sent(row.id, row.version, row.text)

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
                    claimed_at = await session.scalar(
                        select(AuditLogEntry.at).where(
                            AuditLogEntry.subject_id == sent.action_id,
                            AuditLogEntry.event == "action_claimed",
                        )
                    )
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
                        assert claimed_at is not None
                        assert message_at is not None
                        assert message_at > claimed_at, "sent over a newer contact message"

        self.run(check())


def test_no_event_sequence_sends_twice_or_sends_unapproved_text(migrated_url: str) -> None:
    ActionMachine.url = migrated_url
    run_state_machine_as_test(
        ActionMachine,
        settings=settings(
            derandomize=True,
            max_examples=40,
            stateful_step_count=25,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        ),
    )
