"""P2 routes extend control dispatch without changing existing precedence."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import service
from assistant.accounts.models import Account, PendingInput
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.telegram.control.dispatcher import ControlDispatcher
from assistant.telegram.control.handlers import CardPress, HandlerResult, IncomingMessage
from assistant.telegram.control.routes import ControlRoutes
from assistant.telegram.gateway import OutboundGateway
from tests.fakes.clock import FakeClock, FakeSleep
from tests.fakes.sender import FakeTelegramSender
from tests.integration.business_env import OWNER, business_message, connect, deliver, environment
from tests.integration.test_control_chat import CHAT, message, onboard

pytestmark = pytest.mark.integration


@dataclass
class Recorder:
    calls: list[str] = field(default_factory=list)

    async def command(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        scheduler: JobScheduler | None,
        account: Account,
        incoming: IncomingMessage,
        argument: str,
    ) -> HandlerResult:
        self.calls.append("command:" + argument)
        return HandlerResult(True, "synthetic", None)

    async def pending(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        scheduler: JobScheduler | None,
        account: Account,
        incoming: IncomingMessage,
        pending: PendingInput,
    ) -> HandlerResult:
        self.calls.append("pending")
        return HandlerResult(True, "synthetic", None)

    async def callback(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        scheduler: JobScheduler | None,
        account: Account,
        press: CardPress,
    ) -> HandlerResult:
        self.calls.append("callback:" + press.op)
        return HandlerResult(True, "synthetic", None)


def dispatcher(recorder: Recorder) -> ControlDispatcher:
    clock = FakeClock()
    gateway = OutboundGateway(FakeTelegramSender(), clock, sleep=FakeSleep())
    return ControlDispatcher(
        gateway,
        clock,
        routes=ControlRoutes(
            commands={"synthetic": recorder.command},
            pending_inputs={"synthetic": recorder.pending},
            callbacks={"syn": recorder.callback},
        ),
    )


async def arm(session: AsyncSession, account: Account) -> None:
    await service.set_pending_input(
        session, account_id=account.id, kind="synthetic", clock=FakeClock()
    )


async def test_a_registered_callback_kind_is_dispatched_with_the_press(
    db_session: AsyncSession,
) -> None:
    recorder = Recorder()
    target = dispatcher(recorder)
    await onboard(db_session, target)
    result = await target.dispatch_callback(
        db_session,
        callback_id="synthetic-cb",
        from_id=584113992,
        data="syn:synthetic:ok",
        chat_id=CHAT,
        message_id=3,
    )
    assert result.handled
    assert recorder.calls == ["callback:ok"]


async def test_an_unknown_kind_still_answers_no_longer_active(db_session: AsyncSession) -> None:
    recorder = Recorder()
    target = dispatcher(recorder)
    await onboard(db_session, target)
    result = await target.dispatch_callback(
        db_session,
        callback_id="synthetic-cb",
        from_id=584113992,
        data="zzz:synthetic:ok",
        chat_id=CHAT,
        message_id=3,
    )
    assert not result.handled
    assert recorder.calls == []


async def test_a_registered_pending_input_consumes_the_next_owner_message(
    db_session: AsyncSession,
) -> None:
    recorder = Recorder()
    target = dispatcher(recorder)
    account = await onboard(db_session, target)
    await arm(db_session, account)
    await target.dispatch(db_session, message("synthetic response", message_id=3))
    assert recorder.calls == ["pending"]


async def test_a_forwarded_message_never_answers_a_registered_pending_input(
    db_session: AsyncSession,
) -> None:
    from dataclasses import replace

    recorder = Recorder()
    target = dispatcher(recorder)
    account = await onboard(db_session, target)
    await arm(db_session, account)
    await target.dispatch(
        db_session, replace(message("synthetic forwarded", message_id=3), forwarded=True)
    )
    assert recorder.calls == []


async def test_a_command_cancels_a_registered_pending_input(db_session: AsyncSession) -> None:
    recorder = Recorder()
    target = dispatcher(recorder)
    account = await onboard(db_session, target)
    await arm(db_session, account)
    await target.dispatch(db_session, message("/synthetic argument", message_id=3))
    assert recorder.calls == ["command:argument"]
    assert (
        await service.get_pending_input(db_session, account_id=account.id, clock=FakeClock())
        is None
    )


async def test_a_registered_command_runs(db_session: AsyncSession) -> None:
    recorder = Recorder()
    target = dispatcher(recorder)
    await onboard(db_session, target)
    await target.dispatch(db_session, message("/synthetic argument", message_id=3))
    assert recorder.calls == ["command:argument"]


async def test_existing_cards_and_commands_behave_as_before(db_session: AsyncSession) -> None:
    from assistant.agent.confirmations import TOOL_CONFIRM_CARD
    from assistant.memory.undo import UNDO_CARD
    from assistant.telegram.control import panels

    recorder = Recorder()
    target = dispatcher(recorder)
    account = await onboard(db_session, target)
    result = await target.dispatch(db_session, message("/pause", message_id=3))
    assert result.result.handled
    assert account.autonomy_paused
    assert recorder.calls == []
    result = await target.dispatch(db_session, message("/settings", message_id=4))
    assert result.result.handled
    assert recorder.calls == []
    # Built-in callback kinds keep precedence even if an extension tries to
    # register one. Consent already passed through onboarding above.
    kinds = (service.MANAGED_CONSENT_CARD, TOOL_CONFIRM_CARD, UNDO_CARD, panels.SETTINGS_CARD)
    target._routes = ControlRoutes(callbacks=dict.fromkeys(kinds, recorder.callback))
    for kind in kinds:
        result = await target.dispatch_callback(
            db_session,
            callback_id="synthetic-cb",
            from_id=584113992,
            data=f"{kind}:synthetic:ok",
            chat_id=CHAT,
            message_id=5,
        )
        assert not result.handled
        assert recorder.calls == []


async def test_business_updates_never_reach_registered_routes(migrated_url: str) -> None:
    recorder = Recorder()
    routes = ControlRoutes(
        commands={"synthetic": recorder.command}, pending_inputs={"synthetic": recorder.pending}
    )
    async with environment(migrated_url, routes=routes) as env:
        await connect(env)
        await deliver(env, business_message(from_id=OWNER, message_text="/synthetic argument"))
        assert recorder.calls == []
        async with env.factory() as session, session.begin():
            from sqlalchemy import select

            account = await session.scalar(select(Account).where(Account.telegram_user_id == OWNER))
            assert account is not None
            pending = await service.set_pending_input(
                session, account_id=account.id, kind="synthetic", clock=env.clock
            )
            pending_id = pending.id
        await deliver(
            env, business_message(from_id=OWNER, message_text="synthetic pending response")
        )
        async with env.factory() as session:
            assert await session.get(PendingInput, pending_id) is not None
        assert recorder.calls == []


async def test_a_tool_whose_service_is_missing_answers_not_available(migrated_url: str) -> None:
    from datetime import UTC, datetime

    from tests.integration.test_summaries import catch_up

    async with environment(migrated_url) as env:
        await connect(env)
        await deliver(
            env, business_message(date=int(datetime(2026, 10, 18, 9, tzinfo=UTC).timestamp()))
        )
        result = await catch_up(env, None, since="2026-10-18 00:00", until=None)
        assert result.data["conversations"] == []
        assert [item["reason"] for item in result.data["not_summarized"]] == [
            "summaries_unavailable"
        ]
