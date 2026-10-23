"""Forwarded and quoted control-chat messages become third-party turns (S03.1).

Through the real inbox: the turn is stored with `third_party` (and the quote
in `quoted_text`), queued for the agent like any free text, and a forwarded
message is never run as a command or stop word (invariant 3b).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from assistant.accounts.models import Account, ControlTurn
from assistant.accounts.service import active_consent
from tests.integration.business_env import OWNER, Env, deliver, job_names, onboard, private_message

pytestmark = pytest.mark.integration

ORIGIN = {"type": "hidden_user", "date": 1792354000, "sender_user_name": "Synthetic"}


def forwarded(text: str, **extra: Any) -> dict[str, Any]:
    update = private_message(OWNER, text)
    update["message"].update({"forward_origin": ORIGIN, **extra})
    return update


async def owner_turns(env: Env) -> list[ControlTurn]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(ControlTurn).where(ControlTurn.role == "owner").order_by(ControlTurn.id)
                )
            )
            .scalars()
            .all()
        )


async def test_a_forwarded_message_is_stored_as_a_third_party_turn(env: Env) -> None:
    await onboard(env)
    await deliver(env, forwarded("Synthetic forwarded request"))
    [turn] = await owner_turns(env)
    assert (turn.text, turn.third_party, turn.quoted_text) == (
        "Synthetic forwarded request",
        True,
        None,
    )
    assert "agent.owner_request" in await job_names(env)


async def test_a_forwarded_command_is_never_executed(env: Env) -> None:
    await onboard(env)
    await deliver(env, forwarded("/privacy revoke"))
    await deliver(env, forwarded("stop"))
    async with env.factory() as session:
        account = (
            await session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()
        consent = await active_consent(session, account.id, kind="ai_control")
    assert consent.granted is True
    assert account.autonomy_paused is False
    assert [t.text for t in await owner_turns(env)] == ["/privacy revoke", "stop"]


async def test_a_quote_from_another_chat_is_kept_with_the_turn(env: Env) -> None:
    await onboard(env)
    update = private_message(OWNER, "What did they mean here?")
    update["message"].update(
        {
            "quote": {"text": "Synthetic quoted line", "position": 0},
            "external_reply": {"origin": ORIGIN},
        }
    )
    await deliver(env, update)
    [turn] = await owner_turns(env)
    assert (turn.text, turn.third_party, turn.quoted_text) == (
        "What did they mean here?",
        True,
        "Synthetic quoted line",
    )


async def test_a_plain_message_is_not_third_party(env: Env) -> None:
    await onboard(env)
    await deliver(env, private_message(OWNER, "Synthetic plain question"))
    [turn] = await owner_turns(env)
    assert (turn.third_party, turn.quoted_text) == (False, None)
