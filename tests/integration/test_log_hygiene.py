"""Logs never contain content or secrets — RG-10, extended for S02.4.

A full scenario (onboarding, connection, managed messages with a canary in
the text and the contact's name, an edit, a deletion, `/status`, the panel,
the decline cards, a deletion) plus injected failures that each carry the
canary — a unique violation, a NOT NULL violation, a pydantic validation
failure, a Telegram 400, an LLM 400, and a network error whose message holds
the Bot API URL with the token — must leave neither the canary nor the token
in the application's log output. Never weaken: a leak here is a leak in
production.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from assistant.accounts import service
from assistant.accounts.models import Account
from assistant.agent.persistence import DbCallRecorder, DbConsentReader
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    default_registry,
)
from assistant.config import Settings
from assistant.core.routing import Route
from assistant.db.uow import transaction
from assistant.inbox.notification_types import NotificationKind, NotificationRefs, Priority
from assistant.inbox.notifications import NotificationDelivery, enqueue
from assistant.inbox.processor import DeterministicError
from assistant.jobs.app import build_app
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient
from assistant.llm.port import LLMError, LLMErrorKind
from assistant.observability.logging import configure_logging, get_logger
from assistant.telegram.control.agent_channel import GatewayControlChannel
from assistant.telegram.gateway import OutboundGateway
from assistant.telegram.ingress.poller import Poller
from assistant.telegram.ingress.ports import TelegramTransientError
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock, FakeSleep
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.fakes.sender import network_error, telegram_error
from tests.fakes.telegram import FakeUpdateSource
from tests.integration.agent_env import NOTE_FLAG, PRODUCTION_FLAGS, SET_NOTE
from tests.integration.business_env import (
    OWNER,
    Env,
    business_message,
    connect,
    deleted_messages,
    deliver,
    grant_managed,
    press,
    private_message,
    run_deletion_jobs,
)
from tests.integration.execution_env import approved, caught_up, harness, seed
from tests.integration.p2_factories import seed_triage

pytestmark = pytest.mark.integration

CANARY = "CANARY-SYNTH-7f3a"
TOKEN = "8123456789:AAH1bQ7xKqY-ZZ_pLm3nOpQrStUvWxYz012"
VALID_DB_URL = "postgresql+psycopg://user:pw@db:5432/assistant"


@pytest.fixture
def json_logs(clean_env: None, capsys: pytest.CaptureFixture[str]) -> pytest.CaptureFixture[str]:
    configure_logging(Settings(_env_file=None, database_url=VALID_DB_URL, log_format="json"))  # type: ignore[call-arg]
    return capsys


def _assert_clean(output: str, records: list[logging.LogRecord]) -> None:
    assert output, "the scenario must have logged something"
    assert CANARY not in output
    assert TOKEN not in output
    assert "/bot" not in output
    for record in records:
        rendered = f"{record.getMessage()} {record.__dict__}"
        assert CANARY not in rendered, record.name
        assert TOKEN not in rendered, record.name


async def _owner(env: Env) -> Account:
    from sqlalchemy import select

    async with env.factory() as session:
        return (
            await session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()


async def test_rg10_full_scenario_logs_are_content_free(
    env: Env,
    json_logs: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await connect(env)
    await deliver(env, business_message(message_id=1, message_text=CANARY, first_name=CANARY))
    await deliver(env, business_message(message_id=1, message_text=f"{CANARY} 2", edit=True))
    await deliver(env, business_message(message_id=2, message_text=f"{CANARY} 3", username=CANARY))
    await deliver(env, deleted_messages(message_ids=[2]))
    await deliver(env, private_message(OWNER, f"/status {CANARY}"))
    await deliver(env, private_message(OWNER, "/start bizChat7000000005"))
    await deliver(env, private_message(OWNER, f"/start bizChat{CANARY}"))
    await deliver(env, private_message(OWNER, "/privacy managed"))
    await deliver(env, private_message(OWNER, f"no {CANARY}"))  # free text, not an answer
    await press(env, "managed_consent", "decline")
    await press(env, "managed_delete", "delete")
    await run_deletion_jobs(env)  # queued under the control lock (M03 D83)
    await press(env, "managed_mode", "stop")
    await deliver(env, private_message(OWNER, f"/settings timezone {CANARY}"))
    get_logger("tests").info("scenario_done", conversation_id=1)

    _assert_clean(json_logs.readouterr().out, caplog.records)


async def test_injected_failures_never_log_the_canary_or_the_token(
    env: Env,
    json_logs: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await connect(env)
    handlers = env.processor._handlers
    business = handlers[Route.BUSINESS]

    # 1. A unique violation inside a handler (a DBAPIError with the value in
    #    its DETAIL line, which terse logging and redaction must both drop).
    async def unique_violation(session: Any, row: Any, decision: Any) -> None:
        await session.execute(
            text("INSERT INTO app.system_state (key, value) VALUES (:k, '{}'::jsonb)"),
            {"k": CANARY},
        )
        await session.execute(
            text("INSERT INTO app.system_state (key, value) VALUES (:k, '{}'::jsonb)"),
            {"k": CANARY},
        )

    # 2. A NOT NULL violation.
    async def not_null_violation(session: Any, row: Any, decision: Any) -> None:
        await session.execute(
            text("INSERT INTO app.contacts (account_id, display_name) VALUES (NULL, :n)"),
            {"n": CANARY},
        )

    # 3. A deterministic error whose message is the canary.
    async def exploding(session: Any, row: Any, decision: Any) -> None:
        raise DeterministicError(CANARY)

    for handler in (unique_violation, not_null_violation, exploding):
        env.processor._handlers = {**handlers, Route.BUSINESS: handler}
        update = business_message(message_text=CANARY)
        for _ in range(3):
            await deliver(env, update)
    env.processor._handlers = {**handlers, Route.BUSINESS: business}

    # 4. A Telegram 400 whose description carries the canary, on a control send.
    env.sender.fail_with = telegram_error(f"Bad Request: {CANARY}", status_code=400)
    await deliver(env, private_message(OWNER, "/help"))
    env.sender.fail_with = None

    # 5. A pydantic validation failure in tool arguments, and an LLM 400 whose
    #    message carries the canary, through the real runtime.
    account = await _owner(env)
    async with env.factory() as session, session.begin():
        turn = await service.record_control_turn(
            session, account_id=account.id, role="owner", text=f"question {CANARY}"
        )
        turn_id = turn.id
    fake = FakeLLM(
        tool_response(("c1", "get_status", f'{{"unexpected": "{CANARY}"}}')),
        LLMError(LLMErrorKind.BAD_REQUEST, code=f"provider said {CANARY}"),
    )
    runtime = OwnerRequestRuntime(
        session_factory=env.factory,
        llm=ConsentGatedLLMClient(
            MeteredLLMClient(fake, DbCallRecorder(env.factory, env.clock)),
            DbConsentReader(env.factory),
        ),
        channel=FakeChannel(),
        registry=default_registry(),
        clock=env.clock,
        config=AgentConfig(
            model="gpt-6-luna",
            monthly_budget_usd=Decimal("5"),
            enabled_flags=frozenset({"tool.get_status"}),
        ),
    )
    await runtime.run(turn_id)

    # 6. A network error whose message holds the Bot API URL with the token.
    source = FakeUpdateSource()
    poller = Poller(source, env.factory, FakeClock())
    await poller.startup()

    async def get_updates(**kwargs: Any) -> list[dict[str, Any]]:
        # As aiogram would: the failing URL, token included, in the message.
        raise TelegramTransientError(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates failed: {CANARY}"
        )

    source.get_updates = get_updates  # type: ignore[method-assign]
    with pytest.raises(TelegramTransientError):
        await poller.poll_once()
    get_logger("tests").warning("injection_done", exc_type="TelegramTransientError")

    _assert_clean(json_logs.readouterr().out, caplog.records)


async def test_library_logs_and_job_failures_are_redacted_too(
    migrated_url: str, json_logs: pytest.CaptureFixture[str]
) -> None:
    """S02.6: stdlib logging (procrastinate, asyncio, aiogram) used to bypass
    the redaction chain and reach Python's last-resort handler, `str(exc)`
    and traceback included. Now it is rendered by the same chain: the record
    appears, the canary and the token do not."""
    leak = f"{CANARY} https://api.telegram.org/bot{TOKEN}/getUpdates"

    # 1. A real job failure in a real worker, the exception carrying both.
    app = build_app(migrated_url)

    @app.task(name="tests.exploding_job", queue="system")
    async def exploding_job(row_id: int) -> None:
        raise ValueError(leak)

    async with app.open_async():
        await exploding_job.defer_async(row_id=1)
        await app.run_worker_async(
            queues=["system"], wait=False, install_signal_handlers=False, listen_notify=False
        )

    # 2. Library loggers as they really write: aiogram's %-style error with
    #    the exception as an argument, asyncio's pre-formatted message with the
    #    exception's repr, and a warning that quotes a URL.
    try:
        raise RuntimeError(leak)
    except RuntimeError as error:
        exc = error
    logging.getLogger("aiogram.dispatcher").error(
        "Failed to fetch updates - %s: %s", type(exc).__name__, exc
    )
    logging.getLogger("asyncio").error(
        f"Task exception was never retrieved\nexception={exc!r}",  # noqa: G004 - as asyncio does
        exc_info=exc,
    )
    logging.getLogger("aiogram.event").warning(f"retrying after {leak}")  # noqa: G004

    output = json_logs.readouterr().out
    assert CANARY not in output
    assert TOKEN not in output
    assert "/bot" not in output
    assert '"logger": "aiogram.dispatcher"' in output
    assert "Failed to fetch updates - %s: %s" in output  # the template survives
    assert '"logger": "asyncio"' in output
    assert '"exc_type": "RuntimeError"' in output
    assert '"logger": "aiogram.event"' in output
    assert '"logger": "procrastinate.' in output  # the job failure was logged, redacted


async def test_postgres_runs_with_terse_error_logging(env: Env) -> None:
    """The database log is the other place a value can leak: a NOT NULL
    violation's DETAIL line quotes the failing row. The Compose and CI
    databases must run terse (ARCHITECTURE §14.4)."""
    async with env.factory() as session:
        verbosity = (await session.execute(text("SHOW log_error_verbosity"))).scalar_one()
        statement = (await session.execute(text("SHOW log_min_error_statement"))).scalar_one()
        logged = (await session.execute(text("SHOW log_statement"))).scalar_one()
    assert verbosity == "terse"
    assert statement == "panic"
    assert logged == "none"


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _lookup_bug(_ctx: ToolContext, _args: _NoArgs) -> ToolOutcome:
    """A programming error inside a handler: a `KeyError` carrying the canary."""
    missing: dict[str, Any] = {}
    return ToolOutcome("ok", missing[CANARY])


#: Test-only: a read tool whose handler has a lookup bug.
LOOKUP_BUG = ToolDefinition(
    name="test_lookup_bug",
    description="Test-only read tool with a bug.",
    args_model=_NoArgs,
    capability=CapabilityClass.READ,
    handler=_lookup_bug,
    release_flag="tool.test_lookup_bug",
)
#: Ten characters, so it reaches `date.fromisoformat`, whose message quotes it.
DAY_CANARY = CANARY[:10]


def _remember(**values: Any) -> str:
    return json.dumps(
        {
            "content": "SYNTH",
            "kind": "fact",
            "contact_id": None,
            "conversation_id": None,
            "applies_to_replies": None,
            "replaces_memory_id": None,
            **values,
        }
    )


def _tool_lines(output: str) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    return [
        line
        for line in lines
        if line.get("event") in {"agent_tool_call_failed", "agent_tool_call_not_run"}
    ]


async def test_every_tool_call_that_does_not_succeed_logs_one_content_free_line(
    env: Env,
    json_logs: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M03 fix round C2: a failed tool call used to leave no trace at all.
    Each one now writes exactly one line with a fixed code, through the real
    runtime and the real log pipeline — and the canary, in a tool name, the
    arguments, a stdlib error message, an exception and a held call, appears
    nowhere (RG-10)."""
    caplog.set_level(logging.DEBUG)
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_id=1, message_text=CANARY, first_name="Kamola"))
    account = await _owner(env)
    async with env.factory() as session, session.begin():
        turn = await service.record_control_turn(
            session, account_id=account.id, role="owner", text=f"question {CANARY}"
        )
        turn_id = turn.id
        unknown = int(
            (
                await session.execute(text("SELECT coalesce(max(id), 0) + 1000 FROM app.contacts"))
            ).scalar_one()
        )
    search = {"queries": ["synthetic"], "contact_id": None, "conversation_id": None}
    fake = FakeLLM(
        tool_response(
            ("f1", f"{CANARY}_tool", "{}"),  # not offered (and not registered)
            ("f2", "list_memories", f"{{not json {CANARY}"),
            ("f3", "remember", _remember(content=f"{CANARY} " + "x" * 400)),  # too long
            (
                "f4",
                "search_messages",
                json.dumps({**search, "since": DAY_CANARY, "until": None, "limit": None}),
            ),
            ("f5", "remember", _remember(contact_id=unknown)),  # not this account's
            ("f6", "test_lookup_bug", "{}"),
            ("f7", "resolve_contact", json.dumps({"name": "Kamola", "limit": None})),  # taints
            # Held (the run is tainted now), and too large to show on a card.
            ("f8", "test_set_note", json.dumps({"note": CANARY * 40})),
        ),
        text_response("Synthetic answer."),
    )
    registry = default_registry()
    registry.register(SET_NOTE)
    registry.register(LOOKUP_BUG)
    runtime = OwnerRequestRuntime(
        session_factory=env.factory,
        llm=ConsentGatedLLMClient(
            MeteredLLMClient(fake, DbCallRecorder(env.factory, env.clock)),
            DbConsentReader(env.factory),
        ),
        channel=FakeChannel(),
        registry=registry,
        clock=env.clock,
        config=AgentConfig(
            model="gpt-6-luna",
            monthly_budget_usd=Decimal("5"),
            enabled_flags=PRODUCTION_FLAGS | {NOTE_FLAG, LOOKUP_BUG.release_flag},
        ),
    )
    outcome = await runtime.run(turn_id)
    assert outcome.status.value == "succeeded"

    output = json_logs.readouterr().out
    lines = _tool_lines(output)
    assert [(line["event"], line["tool"], line["code"]) for line in lines] == [
        ("agent_tool_call_failed", "(unknown)", "not_offered"),
        ("agent_tool_call_failed", "list_memories", "args_not_json"),
        ("agent_tool_call_failed", "remember", "args_invalid"),
        ("agent_tool_call_failed", "search_messages", "args_invalid"),
        ("agent_tool_call_failed", "remember", "not_found"),
        ("agent_tool_call_failed", "test_lookup_bug", "internal_lookup"),
        ("agent_tool_call_not_run", "test_set_note", "hold:args_too_large"),
    ]
    assert [line["level"] for line in lines] == ["warning"] * 6 + ["info"]
    assert all(line["run_id"] > 0 and line["account_id"] == account.id for line in lines)
    assert (lines[2]["fields"], lines[3]["fields"]) == (["content"], ["since"])
    assert (lines[4]["exc_type"], lines[5]["exc_type"]) == ("NotInAccountError", "KeyError")
    _assert_clean(output, caplog.records)
    assert DAY_CANARY not in output


async def test_a_failed_alert_delivery_logs_no_content(
    env: Env,
    json_logs: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S05.2 (RG-10): an alert whose rendered text carries the canary — the
    contact's name and the verdict's reason — fails once with a Telegram 400
    that echoes it and once with a network error; neither leaves it in a log."""
    caplog.set_level(logging.DEBUG)
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_id=1, message_text="synthetic", first_name=CANARY))
    async with env.factory() as session, session.begin():
        conversation = (
            await session.execute(text("SELECT id, account_id FROM app.conversations"))
        ).one()
        await seed_triage(
            session,
            account_id=conversation.account_id,
            conversation_id=conversation.id,
            urgency="urgent",
            escalation_category="urgent_decision",
            reasons=[f"reason {CANARY}"],
        )
    delivery = NotificationDelivery(
        session_factory=env.factory,
        channel=GatewayControlChannel(OutboundGateway(env.sender, env.clock, sleep=FakeSleep())),
        clock=env.clock,
        scheduler=env.scheduler,  # type: ignore[arg-type]
        enabled=True,
    )
    failures = (
        telegram_error(f"Bad Request: can't parse entities: {CANARY}", status_code=400),
        network_error(f"Request timeout error {CANARY}"),
    )
    for i, failure in enumerate(failures):
        async with transaction(env.factory) as uow:
            notification = await enqueue(
                uow.session,
                account_id=conversation.account_id,
                kind=NotificationKind.TRIAGE_URGENT,
                priority=Priority.URGENT,
                dedupe_key=f"hygiene:{i}",
                refs=NotificationRefs(conversation_ids=(conversation.id,)),
                clock=env.clock,
                conversation_id=conversation.id,
            )
        assert notification is not None
        env.sender.fail_with = failure
        result = await delivery.deliver(notification)
        env.sender.fail_with = None
        assert result.status in ("retry", "uncertain")
    assert any(CANARY in call["text"] for call in env.sender.sends)  # it was rendered
    _assert_clean(json_logs.readouterr().out, caplog.records)


async def test_rg10_business_sends_and_an_ambiguous_send_log_no_content(
    env: Env,
    json_logs: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S04.4: the executor and the gateway's business path log ids, states
    and codes only — a sent reply's text, an ambiguous one's, and a network
    error carrying the Bot API URL never reach the logs."""
    caplog.set_level(logging.DEBUG)
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    sent = await approved(env, chat, text=f"{CANARY} sent reply")
    env.clock.advance(seconds=1)
    uncertain = await approved(env, chat, text=f"{CANARY} uncertain reply")
    h.sender.plain_script = [
        4242,
        network_error(f"https://api.telegram.org/bot{TOKEN}/sendMessage {CANARY}"),
    ]

    await h.execute(sent)
    await h.execute(uncertain)

    output = json_logs.readouterr().out
    assert "action_claimed" in output
    assert "action_outcome_recorded" in output
    _assert_clean(output, caplog.records)
