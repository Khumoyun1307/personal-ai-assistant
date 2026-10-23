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

import logging
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from assistant.accounts import service
from assistant.accounts.models import Account
from assistant.agent.persistence import DbCallRecorder, DbConsentReader
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.tools.registry import default_registry
from assistant.config import Settings
from assistant.core.routing import Route
from assistant.inbox.processor import DeterministicError
from assistant.jobs.app import build_app
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient
from assistant.llm.port import LLMError, LLMErrorKind
from assistant.observability.logging import configure_logging, get_logger
from assistant.telegram.ingress.poller import Poller
from assistant.telegram.ingress.ports import TelegramTransientError
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock
from tests.fakes.llm import FakeLLM, tool_response
from tests.fakes.sender import FakeTelegramError
from tests.fakes.telegram import FakeUpdateSource
from tests.integration.business_env import (
    OWNER,
    Env,
    business_message,
    connect,
    deleted_messages,
    deliver,
    press,
    private_message,
)

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
    env.sender.fail_with = FakeTelegramError(f"Bad Request: {CANARY}", status_code=400)
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
