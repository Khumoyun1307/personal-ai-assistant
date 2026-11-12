"""The owner agent loop against a real database (ARCHITECTURE §7.2, S01.4).

Wired as in production — consent gate → metering → model — with FakeLLM in
place of OpenAI. Pinned claims:

- no model call without `ai_control` consent (§6 invariant 9);
- the loop terminates on iterations, tokens and wall-clock time;
- tool results go back in one batch, after the model's own output, unchanged;
- refusals, truncation and provider outages give an honest reply, no crash;
- every model call is metered in `llm_calls`; every run is recorded.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import Account, ControlTurn
from assistant.actions.activity import action_receipts, refresh_receipts
from assistant.actions.models import Action
from assistant.agent import runtime as rt
from assistant.agent.models import AgentRun, AgentRunStep, LlmCall
from assistant.agent.persistence import DbCallRecorder, DbConsentReader
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime, RunStatus
from assistant.agent.tools.executor import ToolExecutor
from assistant.agent.tools.registry import ToolOutcome, default_registry
from assistant.agent.tools.replies import source_failure
from assistant.conversations.models import Contact, Conversation
from assistant.core.control_texts import text
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.uow import transaction
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    LLMResponse,
    Provenance,
    ProviderItems,
    StopReason,
    TextItem,
    ToolCall,
    ToolResultItem,
)
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock
from tests.fakes.llm import FakeLLM, stop_response, text_response, tool_response

pytestmark = pytest.mark.integration


def read_draft_response(action_id: int) -> LLMResponse:
    return tool_response(
        ("read", "get_draft", json.dumps({"action_id": action_id, "conversation_id": None}))
    )


async def receipt_card(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, turn_id: int
) -> int:
    async with transaction(factory) as uow:
        turn = await uow.session.get(ControlTurn, turn_id)
        assert turn is not None
        contact = Contact(account_id=turn.account_id, telegram_user_id=7000000099)
        uow.session.add(contact)
        await uow.session.flush()
        conversation = Conversation(
            account_id=turn.account_id, contact_id=contact.id, business_chat_id=7000000099
        )
        uow.session.add(conversation)
        await uow.session.flush()
        action = Action(
            account_id=turn.account_id,
            conversation_id=conversation.id,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            version=1,
            text="SYNTH actual stored body",
            created_at=clock.now(),
            updated_at=clock.now(),
        )
        uow.session.add(action)
        await uow.session.flush()
        return action.id


@pytest.mark.parametrize("change", ["cancelled", "outdated", "purged", "version"])
async def test_h7_final_refresh_discards_an_answer_based_on_an_old_artifact(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, change: str
) -> None:
    turn_id = await owner_turn(factory, clock, managed_consent=True)
    action_id = await receipt_card(factory, clock, turn_id)

    async def race(_request: LLMRequest) -> Any:
        async with transaction(factory) as uow:
            action = await uow.session.get(Action, action_id)
            assert action is not None
            if change == "purged":
                action.text = None
            elif change == "version":
                action.version = 2
                action.text = "SYNTH replacement body"
            else:
                action.state = change
                action.state_reason = (
                    "autonomy_paused" if change == "cancelled" else "source_edited"
                )
        return text_response("STALE: here is your pending draft, SYNTH actual stored body")

    fake = FakeLLM(
        read_draft_response(action_id),
        race,
        text_response("The artifact changed; its old contents are unavailable."),
    )
    channel = FakeChannel()
    await build(
        factory,
        clock,
        fake,
        channel,
        enabled_flags=frozenset({"tool.propose_owner_message", "tool.get_draft"}),
    ).run(turn_id)
    assert "STALE" not in channel.sends[-1]
    assert "SYNTH actual stored body" not in channel.sends[-1]
    assert len(fake.requests) == 3
    correction = fake.requests[-1]
    assert any("Action receipts" in i.text for i in correction.items if isinstance(i, TextItem))
    assert all(tool.name == "get_draft" for tool in correction.tools)


@pytest.mark.parametrize("revoked", [False, True])
async def test_h7_exhausted_or_revoked_run_uses_fresh_receipt_without_another_call(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, *, revoked: bool
) -> None:
    turn_id = await owner_turn(factory, clock, managed_consent=True)
    action_id = await receipt_card(factory, clock, turn_id)

    async def race(_request: LLMRequest) -> Any:
        async with transaction(factory) as uow:
            action = await uow.session.get(Action, action_id)
            assert action is not None
            action.state = "delivery_uncertain"
            action.state_reason = "ambiguous"
            if revoked:
                account = await uow.session.get(Account, action.account_id)
                assert account is not None
                await service.revoke_consent(uow.session, account, kind="ai_control", clock=clock)
        return text_response("STALE sent")

    fake = FakeLLM(
        read_draft_response(action_id),
        race,
    )
    channel = FakeChannel()
    await build(
        factory,
        clock,
        fake,
        channel,
        max_iterations=8 if revoked else 2,
        enabled_flags=frozenset({"tool.propose_owner_message", "tool.get_draft"}),
    ).run(turn_id)
    assert len(fake.requests) == 2
    assert "STALE" not in channel.sends[-1]
    # The owner reads plain catalogue wording, never ids, enum names or timestamps.
    assert text("receipt_uncertain", "en", who="") in channel.sends[-1]
    assert "delivery_uncertain" not in channel.sends[-1]
    assert "action " not in channel.sends[-1]
    assert "202" not in channel.sends[-1]


@pytest.mark.parametrize(
    ("proposal_status", "reason", "expected"),
    [
        ("needs_input", "content", "What would you like the message to say?"),
        ("invalid_arguments", "source_unavailable", "the selected source is unavailable"),
    ],
)
async def test_h7_no_action_result_never_borrows_an_unrelated_live_card(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    proposal_status: str,
    reason: str,
    expected: str,
) -> None:
    turn_id = await owner_turn(factory, clock, managed_consent=True)
    action_id = await receipt_card(factory, clock, turn_id)

    async def no_action(_executor: ToolExecutor, _call: ToolCall) -> tuple[ToolOutcome, str | None]:
        return source_failure(proposal_status, reason), None

    monkeypatch.setattr(ToolExecutor, "_run", no_action)
    fake = FakeLLM(
        tool_response(("proposal", "propose_owner_message", "{}")),
        text_response("INVENTED here is your draft, sent"),
    )
    channel = FakeChannel()
    await build(
        factory,
        clock,
        fake,
        channel,
        enabled_flags=frozenset({"tool.propose_owner_message"}),
    ).run(turn_id)
    assert expected in channel.sends[-1]
    assert "INVENTED" not in channel.sends[-1]
    assert "action " not in channel.sends[-1]
    assert "version" not in channel.sends[-1]
    assert len(await rows(factory, Action)) == 1
    assert (await rows(factory, Action))[0].id == action_id
    # Fallback context is permitted, but NULL results never get a refresh receipt.
    [result] = [i for i in fake.requests[-1].items if isinstance(i, ToolResultItem)]
    assert json.loads(result.output)["action"] is None
    after_result = list(fake.requests[-1].items)[list(fake.requests[-1].items).index(result) + 1 :]
    assert not any("Action receipts" in i.text for i in after_result if isinstance(i, TextItem))


async def test_h7_pause_after_proposal_does_not_turn_stored_work_into_sent(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.actions import autonomy

    turn_id = await owner_turn(factory, clock, managed_consent=True)
    action_id = await receipt_card(factory, clock, turn_id)

    async def proposal(_executor: ToolExecutor, _call: ToolCall) -> tuple[ToolOutcome, str | None]:
        return (
            ToolOutcome(
                "awaiting_approval",
                {
                    "action": {"action_id": action_id},
                    "proposal_status": "staged",
                    "requested_change_applied": True,
                },
            ),
            None,
        )

    monkeypatch.setattr(ToolExecutor, "_run", proposal)

    async def pause_after_proposal(_request: LLMRequest) -> Any:
        async with transaction(factory) as uow:
            turn = await uow.session.get(ControlTurn, turn_id)
            assert turn is not None
            await autonomy.pause(
                uow.session,
                account_id=turn.account_id,
                now=clock.now(),
                source=autonomy.SOURCE_COMMAND,
            )
        return text_response("INVENTED sent, done; the card says SYNTH made-up contents")

    fake = FakeLLM(tool_response(("stage", "propose_owner_message", "{}")), pause_after_proposal)
    channel = FakeChannel()
    await build(
        factory,
        clock,
        fake,
        channel,
        enabled_flags=frozenset({"tool.propose_owner_message"}),
    ).run(turn_id)
    assert len(fake.requests) == 2
    assert "INVENTED" not in channel.sends[-1]
    assert "made-up contents" not in channel.sends[-1]
    assert text("receipt_awaiting_pending", "en", who="") in channel.sends[-1]
    assert text("receipt_unread", "en") in channel.sends[-1]
    assert "action " not in channel.sends[-1]
    assert "version" not in channel.sends[-1]
    [action] = await rows(factory, Action)
    assert action.state == "awaiting_approval"


async def test_h7_explicit_terminal_receipts_bypass_fallback_cap_and_foreign_ids(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock, managed_consent=True)
    action_id = await receipt_card(factory, clock, turn_id)
    async with transaction(factory) as uow:
        original = await uow.session.get(Action, action_id)
        assert original is not None
        original.state = "rejected"
        original.state_reason = "owner_rejected"
        original.updated_at = clock.now() - timedelta(days=2)
        for _ in range(25):
            uow.session.add(
                Action(
                    account_id=original.account_id,
                    conversation_id=original.conversation_id,
                    kind="send_message",
                    origin="owner_dictated",
                    state="approved",
                    version=1,
                    text="SYNTH unrelated",
                    created_at=clock.now(),
                    updated_at=clock.now(),
                )
            )
        account_id = original.account_id
        foreign = Account(telegram_user_id=7000000088)
        uow.session.add(foreign)
        await uow.session.flush()
        foreign_turn = ControlTurn(account_id=foreign.id, role="owner", text="SYNTH foreign")
        uow.session.add(foreign_turn)
        await uow.session.flush()
        foreign_turn_id = foreign_turn.id
    foreign_id = await receipt_card(factory, clock, foreign_turn_id)
    async with factory() as session:
        page = await action_receipts(
            session, account_id=account_id, action_ids=(action_id, foreign_id), now=clock.now()
        )
        unavailable = await refresh_receipts(
            session, account_id=account_id, action_ids=(foreign_id,), now=clock.now()
        )
    assert unavailable.receipts == ()
    assert len(page.receipts) == 21
    assert action_id in {r.action_id for r in page.receipts}
    assert foreign_id not in {r.action_id for r in page.receipts}
    assert page.omitted_live == 5
    assert page.omitted_terminal == 0


async def test_tool_context_carries_the_answered_turn_id(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant.agent.tools.registry import ToolContext

    seen: list[ToolContext] = []

    def record_context(**kwargs: Any) -> ToolContext:
        context = ToolContext(**kwargs)
        seen.append(context)
        return context

    monkeypatch.setattr(rt, "ToolContext", record_context)
    turn_id = await owner_turn(factory, clock)
    drafter, presenter, config = object(), object(), object()
    await build(
        factory,
        clock,
        FakeLLM(text_response("Synthetic answer.")),
        FakeChannel(),
        phase_services={
            "drafter": drafter,
            "approval_cards": presenter,
            "actions_config": config,
            "notifications_enabled": True,
        },
    ).run(turn_id)
    assert len(seen) == 1
    assert seen[0].turn_id == turn_id
    services = seen[0].services
    assert services.drafter is drafter
    assert services.approval_cards is presenter
    assert services.actions_config is config
    assert services.notifications_enabled is True


OWNER = 7000000001
CHAT = 7000000001
STATUS_FLAG = frozenset({"tool.get_status"})


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE app.accounts CASCADE")
    yield build_session_factory(engine)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE app.accounts CASCADE")
    await engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


async def owner_turn(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    *,
    consent: bool = True,
    managed_consent: bool = False,
    history: list[tuple[str, str, bool]] = (),  # type: ignore[assignment]
    text: str = "Synthetic question?",
) -> int:
    async with transaction(factory) as uow:
        session = uow.session
        account, _ = await service.get_or_create_account(
            session, telegram_user_id=OWNER, clock=clock
        )
        await service.set_control_chat(session, account, CHAT)
        if consent:
            await service.grant_consent(
                session,
                account,
                kind="ai_control",
                policy_version="v0",
                processors=["OpenAI"],
                clock=clock,
            )
        if managed_consent:
            await service.grant_consent(
                session,
                account,
                kind="ai_managed",
                policy_version="v0",
                processors=["OpenAI"],
                clock=clock,
            )
        for role, past_text, managed in history:
            await service.record_control_turn(
                session,
                account_id=account.id,
                role=role,
                text=past_text,
                contains_managed=managed,
            )
        turn = await service.record_control_turn(
            session, account_id=account.id, role="owner", text=text, telegram_message_id=42
        )
        return turn.id


def build(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    fake: FakeLLM,
    channel: FakeChannel,
    **config: Any,
) -> OwnerRequestRuntime:
    llm = ConsentGatedLLMClient(
        MeteredLLMClient(fake, DbCallRecorder(factory, clock)), DbConsentReader(factory)
    )
    phase_services = config.pop("phase_services", {})
    settings: dict[str, Any] = {"model": "gpt-6-luna"}
    settings.update(config)
    return OwnerRequestRuntime(
        session_factory=factory,
        llm=llm,
        channel=channel,
        registry=default_registry(),
        clock=clock,
        config=AgentConfig(**settings),
        **phase_services,
    )


async def rows(factory: async_sessionmaker[AsyncSession], model: Any) -> list[Any]:
    async with factory() as session:
        return list((await session.execute(select(model).order_by(model.id))).scalars().all())


# ------------------------------------------------------------------ happy path
async def test_an_owner_turn_gets_a_reply_and_everything_is_recorded(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake, channel = FakeLLM(text_response("Synthetic answer.", tokens=300)), FakeChannel()

    outcome = await build(factory, clock, fake, channel).run(turn_id)

    assert outcome.status is RunStatus.SUCCEEDED
    assert channel.sends == ["Synthetic answer."]
    assert channel.events == ["typing", "send"]
    [run] = await rows(factory, AgentRun)
    assert (run.status, run.iterations, run.prompt_version) == ("succeeded", 1, "owner_request.v10")
    assert run.trigger_ref == f"turn:{turn_id}"
    assert run.finished_at is not None
    reply = (await rows(factory, ControlTurn))[-1]
    assert (reply.role, reply.text, reply.run_id) == ("assistant", "Synthetic answer.", run.id)
    assert reply.telegram_message_id == outcome.reply_message_id
    [call] = await rows(factory, LlmCall)
    assert (call.run_id, call.purpose, call.input_tokens, call.output_tokens) == (
        run.id,
        "owner_request",
        300,
        100,
    )
    assert call.cost_estimate == Decimal("0.000080")


async def test_the_request_carries_the_prompt_profile_and_history(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(
        factory,
        clock,
        history=[("owner", "Earlier synthetic", False), ("assistant", "Reply", False)],
    )
    fake = FakeLLM(text_response("ok"))

    await build(factory, clock, fake, FakeChannel()).run(turn_id)

    [request] = fake.requests
    assert "personal assistant" in request.instructions
    assert "timezone: UTC" in request.instructions
    assert [(i.role, i.text) for i in request.items if isinstance(i, TextItem)] == [
        ("user", "Earlier synthetic"),
        ("assistant", "Reply"),
        ("user", "Synthetic question?"),
    ]
    assert request.prompt_cache_key == "owner_request:owner_request.v10"
    assert request.effort == "low"


@pytest.mark.parametrize(
    ("tag", "profile_line"),
    [
        ("uz-Latn", "- preferred language: uz-Latn (Uzbek written in Latin letters)\n"),
        ("uz-Cyrl", "- preferred language: uz-Cyrl (Uzbek written in Cyrillic letters)\n"),
        ("ru", "- preferred language: ru (Russian)\n"),
        ("kk", "- preferred language: kk\n"),
        (None, "- preferred language: not set\n"),
    ],
)
async def test_the_owner_profile_spells_out_the_language_and_script(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, tag: str | None, profile_line: str
) -> None:
    """S03.7 fix round: 10 of 33 agent-mode replies were not in the owner's uz-Latn.
    The bare tag is spelled out as the summarize context does (S03.6)."""
    turn_id = await owner_turn(factory, clock)
    async with transaction(factory) as uow:
        account = (await uow.session.execute(select(Account))).scalar_one()
        account.locale_pref = tag
    fake = FakeLLM(text_response("ok"))

    await build(factory, clock, fake, FakeChannel()).run(turn_id)

    [request] = fake.requests
    assert profile_line in request.instructions


# --------------------------------------------------------------------- consent
async def test_without_consent_the_model_is_never_called(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock, consent=False)
    fake, channel = FakeLLM(), FakeChannel()

    outcome = await build(factory, clock, fake, channel).run(turn_id)

    assert outcome.status is RunStatus.SKIPPED
    assert fake.requests == []
    assert channel.sends == [rt.TEXT_NEEDS_CONSENT]
    assert await rows(factory, AgentRun) == []
    assert await rows(factory, LlmCall) == []


async def test_managed_history_is_excluded_without_managed_consent(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(
        factory, clock, history=[("assistant", "Synthetic managed summary", True)]
    )
    fake = FakeLLM(text_response("ok"))

    await build(factory, clock, fake, FakeChannel()).run(turn_id)

    texts = [i.text for i in fake.requests[0].items if isinstance(i, TextItem)]
    assert "Synthetic managed summary" not in texts


async def test_stale_managed_history_is_omitted_even_with_managed_consent(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(
        factory,
        clock,
        managed_consent=True,
        history=[("assistant", "Synthetic managed summary", True)],
    )
    fake = FakeLLM(text_response("ok"))

    await build(factory, clock, fake, FakeChannel()).run(turn_id)

    managed = [i for i in fake.requests[0].items if i.provenance is Provenance.MANAGED]
    # D-S3 (2026-11-09): old managed prose must be explicitly reselected.
    assert managed == []
    assert all(
        "Synthetic managed summary" not in i.text
        for i in fake.requests[0].items
        if isinstance(i, TextItem)
    )
    reply = (await rows(factory, ControlTurn))[-1]
    assert reply.contains_managed is False


# ----------------------------------------------------------------------- tools
async def test_a_tool_call_round_trip_appends_output_then_all_results(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    seen: list[LLMRequest] = []

    def second_call(request: LLMRequest) -> Any:
        seen.append(request)
        return text_response("You are active.")

    fake = FakeLLM(tool_response(("c1", "get_status", "{}")), second_call)

    outcome = await build(factory, clock, fake, FakeChannel(), enabled_flags=STATUS_FLAG).run(
        turn_id
    )

    assert outcome.status is RunStatus.SUCCEEDED
    assert [t.name for t in fake.requests[0].tools] == ["get_status"]
    tail = seen[0].items[-2:]
    assert isinstance(tail[0], ProviderItems)
    assert isinstance(tail[1], ToolResultItem)
    assert tail[1].call_id == "c1"
    assert '"status": "ok"' in tail[1].output
    assert '"connection": {"present": false' in tail[1].output  # S02.3: the StatusReport
    [step] = await rows(factory, AgentRunStep)
    assert (step.tool_name, step.status, step.seq) == ("get_status", "ok", 1)
    [run] = await rows(factory, AgentRun)
    assert run.iterations == 2
    assert len(await rows(factory, LlmCall)) == 2


async def test_tools_are_not_offered_while_their_flag_is_off(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake = FakeLLM(text_response("ok"))

    await build(factory, clock, fake, FakeChannel()).run(turn_id)

    assert fake.requests[0].tools == ()


# --------------------------------------------------------------------- budgets
async def test_the_loop_stops_at_the_iteration_limit(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake = FakeLLM(*(tool_response((f"c{i}", "get_status", "{}")) for i in range(3)))
    channel = FakeChannel()

    outcome = await build(
        factory, clock, fake, channel, max_iterations=3, enabled_flags=STATUS_FLAG
    ).run(turn_id)

    assert outcome.status is RunStatus.FAILED
    assert len(fake.requests) == 3
    assert channel.sends == [rt.TEXT_LIMITS]
    [run] = await rows(factory, AgentRun)
    assert (run.status, run.error_class, run.iterations) == ("failed", "run_limits", 3)


async def test_the_loop_stops_when_the_token_budget_is_spent(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake = FakeLLM(tool_response(("c1", "get_status", "{}"), tokens=60))

    outcome = await build(
        factory, clock, fake, FakeChannel(), run_token_budget=50, enabled_flags=STATUS_FLAG
    ).run(turn_id)

    assert outcome.status is RunStatus.FAILED
    assert len(fake.requests) == 1


async def test_the_loop_stops_at_the_wall_clock_deadline(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)

    def slow(_request: LLMRequest) -> Any:
        clock.advance(minutes=6)
        return tool_response(("c1", "get_status", "{}"))

    fake = FakeLLM(slow)

    outcome = await build(
        factory,
        clock,
        fake,
        FakeChannel(),
        run_timeout=timedelta(minutes=5),
        enabled_flags=STATUS_FLAG,
    ).run(turn_id)

    assert outcome.status is RunStatus.FAILED
    assert len(fake.requests) == 1


async def test_a_reached_monthly_budget_means_no_model_call(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    async with transaction(factory) as uow:
        account = (await uow.session.execute(select(Account))).scalar_one()
        uow.session.add(
            LlmCall(
                account_id=account.id,
                purpose="owner_request",
                model="gpt-6-luna",
                cost_estimate=Decimal("5.00"),
                created_at=clock.now(),
            )
        )
    fake, channel = FakeLLM(), FakeChannel()

    outcome = await build(factory, clock, fake, channel, monthly_budget_usd=Decimal("5")).run(
        turn_id
    )

    assert outcome.status is RunStatus.BUDGET_EXCEEDED
    assert fake.requests == []
    assert channel.sends == [rt.TEXT_BUDGET_REACHED]


# -------------------------------------------------------- honest failure modes
async def test_a_refusal_gets_a_safe_reply(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    channel = FakeChannel()

    outcome = await build(factory, clock, FakeLLM(stop_response(StopReason.REFUSAL)), channel).run(
        turn_id
    )

    assert outcome.status is RunStatus.REFUSED
    assert channel.sends == [rt.TEXT_REFUSED]


async def test_a_provider_outage_is_reported_and_nothing_crashes(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    channel = FakeChannel()

    outcome = await build(factory, clock, FakeLLM(LLMError(LLMErrorKind.UNAVAILABLE)), channel).run(
        turn_id
    )

    assert outcome.status is RunStatus.FAILED
    assert channel.sends == [rt.TEXT_PROVIDER_DOWN]
    [run] = await rows(factory, AgentRun)
    assert run.error_class == "llm_unavailable"
    assert await rows(factory, LlmCall) == []


async def test_truncated_output_is_retried_once_with_a_larger_budget(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake = FakeLLM(stop_response(StopReason.MAX_TOKENS), text_response("Shorter answer."))
    channel = FakeChannel()

    outcome = await build(factory, clock, fake, channel, max_output_tokens=1000).run(turn_id)

    assert outcome.status is RunStatus.SUCCEEDED
    assert [r.max_output_tokens for r in fake.requests] == [1000, 2000]
    assert channel.sends == ["Shorter answer."]


async def test_an_undelivered_reply_is_recorded_as_such(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)

    await build(factory, clock, FakeLLM(text_response("ok")), FakeChannel(deliver=False)).run(
        turn_id
    )

    [run] = await rows(factory, AgentRun)
    assert run.error_class == "reply_not_delivered"


async def test_an_assistant_turn_never_starts_a_run(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    await owner_turn(factory, clock)
    async with transaction(factory) as uow:
        account = (await uow.session.execute(select(Account))).scalar_one()
        turn = await service.record_control_turn(
            uow.session, account_id=account.id, role="assistant", text="x"
        )
    fake = FakeLLM()

    outcome = await build(factory, clock, fake, FakeChannel()).run(turn.id)

    assert outcome.status is RunStatus.SKIPPED
    assert fake.requests == []


# ------------------------------------------------- re-delivered jobs (S01.5)
async def test_a_redelivered_job_for_an_answered_turn_does_not_answer_twice(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake, channel = FakeLLM(text_response("Only once.")), FakeChannel()
    runtime = build(factory, clock, fake, channel)

    first = await runtime.run(turn_id)
    second = await runtime.run(turn_id)

    assert first.status is RunStatus.SUCCEEDED
    assert (second.status, second.note) == (RunStatus.SKIPPED, "already_answered")
    assert channel.sends == ["Only once."]
    assert len(fake.requests) == 1


async def test_an_interrupted_run_is_marked_and_the_turn_is_answered(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    async with transaction(factory) as uow:
        account = (await uow.session.execute(select(Account))).scalar_one()
        uow.session.add(
            AgentRun(
                account_id=account.id,
                run_type="owner_request",
                trigger_ref=f"turn:{turn_id}",
                model="gpt-6-luna",
                status="running",
                started_at=clock.now(),
            )
        )
    channel = FakeChannel()

    outcome = await build(factory, clock, FakeLLM(text_response("Answered now.")), channel).run(
        turn_id
    )

    assert outcome.status is RunStatus.SUCCEEDED
    assert outcome.account_id == account.id
    assert channel.sends == ["Answered now."]
    first, second = await rows(factory, AgentRun)
    assert (first.status, first.error_class) == ("failed", "interrupted")
    assert second.status == "succeeded"


async def test_acknowledgement_latency_is_measured_from_message_receipt(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    await owner_turn(factory, clock)
    async with transaction(factory) as uow:
        account = (await uow.session.execute(select(Account))).scalar_one()
        turn = await service.record_control_turn(
            uow.session,
            account_id=account.id,
            role="owner",
            text="Synthetic",
            created_at=clock.now(),
        )
    clock.advance(seconds=1.25)  # inbox + job pickup, as the owner experiences it

    outcome = await build(factory, clock, FakeLLM(text_response("ok")), FakeChannel()).run(turn.id)

    assert outcome.ack_latency_ms == 1250
