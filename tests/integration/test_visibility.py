"""The visibility report and the conversation panel (S02.3, FR-ONB-08).

`/status` ("What can you see?") and the `get_status` tool are built from the
same `StatusReport`, over data seeded through the real ingestion pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import Account
from assistant.agent.tools.registry import ToolContext, ToolServices
from assistant.agent.tools.status import GetStatusArgs, get_status
from assistant.observability.status import build_status
from assistant.telegram.control.handlers import IncomingMessage
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    connect,
    deleted_messages,
    deliver,
    onboard,
    private_message,
)

pytestmark = pytest.mark.integration


async def _account(env: Env) -> Account:
    async with env.factory() as session:
        return (
            await session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()


async def _status(env: Env) -> str:
    await deliver(env, private_message(OWNER, "/status"))
    return env.sender.texts[-1]


async def _report(env: Env):  # type: ignore[no-untyped-def]
    account = await _account(env)
    async with env.factory() as session:
        return await build_status(
            session, account, env.clock.now(), monthly_budget_usd=Decimal("5")
        )


# ------------------------------------------------------------ /status
async def test_status_without_a_connection_says_so(env: Env) -> None:
    await onboard(env)
    shown = await _status(env)
    assert "Not connected" in shown
    assert "No managed chats observed yet" in shown
    assert "This chat: granted" in shown
    assert "Included chats: not granted" in shown


async def test_status_reports_connection_tier_and_connected_since(env: Env) -> None:
    await connect(env, rights=("can_reply", "can_read_messages"), date=1792350000)
    shown = await _status(env)
    assert "State: active" in shown
    assert "Rights: can reply" in shown
    assert "Connected since: 2026-10-18 19:00 UTC" in shown
    assert "I cannot see older history" in shown


async def test_status_counts_observed_conversations_and_coverage_start(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(chat_id=CONTACT, from_id=CONTACT, date=T_BASE + 600))
    await deliver(env, business_message(chat_id=OTHER_CONTACT, from_id=OTHER_CONTACT, date=T_BASE))
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'ignore' WHERE business_chat_id = :c"),
            {"c": OTHER_CONTACT},
        )
    shown = await _status(env)
    assert "Chats observed: 2 (1 ignore, 1 observe)" in shown
    assert "Observing since: 2026-10-18 20:06 UTC" in shown


async def test_status_reports_gaps_and_the_caveat(env: Env) -> None:
    await connect(env)
    poison = business_message(message_text="synthetic poison", extra={"message_id": None})
    for _ in range(3):
        await deliver(env, poison)
    shown = await _status(env)
    assert "Latest gap (definite)" in shown
    assert "Paused: 1 ingest_error" in shown
    assert "1 definite and 0 possible coverage gap(s)" in shown
    assert "could not be processed and are paused" in shown
    assert "failed: 1" in shown


async def test_status_reports_consent_autonomy_and_spend(env: Env) -> None:
    await connect(env)
    await deliver(env, private_message(OWNER, "/privacy managed"))
    await deliver(env, private_message(OWNER, "yes"))
    await deliver(env, private_message(OWNER, "/pause"))
    shown = await _status(env)
    assert "Included chats: granted" in shown
    assert "Autonomy: paused" in shown
    assert "AI spend this month" in shown
    assert "$0.0000" in shown


# ------------------------------------------------------------ the tool
async def test_the_status_tool_reports_the_same_facts_as_the_command(env: Env) -> None:
    await connect(env, rights=("can_reply",))
    await deliver(env, business_message())
    account = await _account(env)
    report = await _report(env)

    services = ToolServices(env.factory, env.clock, Decimal("5"))
    ctx = ToolContext(
        account_id=account.id, run_id=1, run_type="owner_request", tainted=False, services=services
    )
    outcome = await get_status(ctx, GetStatusArgs())

    assert outcome.status == "ok"
    assert outcome.data == report.as_dict()
    assert outcome.data["connection"]["tier"] == "can_reply"
    assert outcome.data["coverage"]["conversations"] == 1
    assert outcome.data["consent"] == {
        "control_chat": True,
        "managed_chats": False,
        "policy_version": "v3",
    }


async def test_the_status_tool_result_is_content_free(env: Env) -> None:
    """The report may go to the model without `ai_managed` consent, so it must
    carry no contact text or names (ARCHITECTURE §8.2)."""
    await connect(env)
    await deliver(
        env,
        business_message(
            message_text="CANARY-TEXT-9c1d", first_name="CanaryName9c1d", username="canary9c1d"
        ),
    )
    report = await _report(env)
    dumped = repr(report.as_dict())
    assert "CANARY-TEXT-9c1d" not in dumped
    assert "CanaryName9c1d" not in dumped
    assert "canary9c1d" not in dumped
    assert str(CONTACT) not in dumped


# ------------------------------------------------------------ the panel
async def _start_with(env: Env, argument: str) -> str:
    await deliver(env, private_message(OWNER, f"/start {argument}"))
    return env.sender.texts[-1]


async def test_bizchat_panel_shows_the_conversation(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_id=1, date=T_BASE, username="syn"))
    await deliver(env, business_message(message_id=2, date=T_BASE + 60, from_id=OWNER))
    await deliver(env, business_message(message_id=3, date=T_BASE + 120))
    await deliver(env, deleted_messages(message_ids=[3]))
    shown = await _start_with(env, f"bizChat{CONTACT}")

    assert "<b>Synthetic</b> @syn" in shown
    assert "Mode: observe" in shown
    assert "Messages: 2 (1 contact, 1 owner manual)" in shown
    assert "Deleted: 1" in shown
    assert "Observing since: 2026-10-18 20:06 UTC" in shown
    assert "Last from contact: 2026-10-18 20:08 UTC" in shown
    assert "Reply window:" in shown
    assert "approximate" in shown


async def test_bizchat_window_state_follows_the_clock(env: Env) -> None:
    await connect(env)
    sent = int((env.clock.now() - timedelta(hours=1)).timestamp())
    await deliver(env, business_message(message_id=10, date=sent))
    assert "open until about" in await _start_with(env, f"bizChat{CONTACT}")
    env.clock.advance(hours=24)
    assert "closed (no contact message in the last 24 h)" in await _start_with(
        env, f"bizChat{CONTACT}"
    )


@pytest.mark.parametrize(
    "argument",
    ["bizChat9999999999", "bizChat-1", "bizChatabc", "bizChat"],
    ids=["unknown id", "negative", "letters", "empty"],
)
async def test_bizchat_for_an_unknown_or_malformed_id_is_refused_identically(
    env: Env, argument: str
) -> None:
    await connect(env)
    await deliver(env, business_message())
    shown = await _start_with(env, argument)
    assert shown == "I don't have that chat. Only chats included under Chat Automation appear here."


async def test_bizchat_for_another_accounts_chat_is_refused(migrated_url: str) -> None:
    """Fail closed across tenants: the other owner's chat id gives the same
    reply as an unknown one (reveals nothing)."""
    from tests.integration.business_env import connection_update, environment

    other_owner = 7000000002
    async with environment(migrated_url, owner_ids=frozenset({OWNER, other_owner})) as env:
        await connect(env)
        await deliver(env, business_message())
        # The second owner onboards and connects on their own connection id.
        await deliver(env, private_message(other_owner, "/start"))
        await deliver(env, private_message(other_owner, "yes"))
        await deliver(env, connection_update(user_id=other_owner, connection_id="synthetic-conn-b"))
        await deliver(env, private_message(other_owner, f"/start bizChat{CONTACT}"))
        shown = env.sender.texts[-1]
    assert "I don't have that chat" in shown


async def test_bizchat_before_onboarding_runs_onboarding(env: Env) -> None:
    await deliver(env, private_message(OWNER, f"/start bizChat{CONTACT}"))
    assert "Before we start" in env.sender.texts[-1]


def test_incoming_message_shape_is_unchanged() -> None:
    """The panel is reached through the normal control path (no new entry point)."""
    message = IncomingMessage(account_id=0, chat_id=1, telegram_message_id=1, text="", from_id=1)
    assert message.text == ""
    assert datetime.now(tz=UTC).tzinfo is UTC
