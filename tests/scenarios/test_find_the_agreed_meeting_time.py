"""Scenario (S03.1): "Find the conversation where we agreed on the meeting time".

End to end with FakeTelegram, FakeLLM and the fake clock: a contact writes in
Uzbek Cyrillic, the owner answers, the owner later asks in Uzbek Latin in the
control chat. The scripted model resolves the name, searches with Latin and
Cyrillic variants, and answers citing contact, time and quote. Pinned:

- the Latin question finds the Cyrillic message (normaliser v2);
- the model saw the contact's words only inside untrusted blocks;
- the run is tainted (it read contact text) and still answered;
- the reply turn records the conversation and contact it drew on;
- nothing changed state, and every model call was metered.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from assistant.accounts.models import ControlTurn
from assistant.agent.models import AgentRun, LlmCall
from assistant.agent.runtime import RunStatus
from assistant.llm.port import LLMRequest, ToolResultItem
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import args, ask, note_rows, one_id, runtime
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    connect,
    deliver,
    grant_managed,
)

pytestmark = pytest.mark.integration


def tool_results(request: LLMRequest) -> list[dict[str, object]]:
    return [json.loads(i.output) for i in request.items if isinstance(i, ToolResultItem)]


async def test_the_owner_finds_where_the_meeting_time_was_agreed(env: Env) -> None:
    await connect(env)
    await grant_managed(env)
    await deliver(
        env,
        business_message(
            message_id=1,
            message_text="Эртага учрашувни соат 15:00 га белгилаймизми?",
            first_name="Камола",
            date=T_BASE,
        ),
        business_message(
            from_id=OWNER,
            chat_id=CONTACT,
            message_id=2,
            message_text="Ha, soat 15:00 da kelishdik",
            first_name="Камола",
            date=T_BASE + 120,
        ),
    )
    conversation_id = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    contact_id = await one_id(
        env, "SELECT contact_id FROM app.conversations WHERE id = :i", i=conversation_id
    )

    turn = await ask(env, "Kamola bilan uchrashuv vaqtini qayerda kelishgan edik?")
    fake = FakeLLM(
        tool_response(("r1", "resolve_contact", args(name="Kamola", limit=None))),
        tool_response(
            (
                "s1",
                "search_messages",
                args(
                    queries=[
                        "uchrashuv soat",
                        "учрашув соат",
                        "soat 15:00",
                        "встреча время",
                        "meeting time",
                    ],
                    contact_id=contact_id,
                    conversation_id=None,
                    since=None,
                    until=None,
                    limit=5,
                ),
            )
        ),
        text_response(
            "Kamola 15:00 ni taklif qildi, siz «Ha, soat 15:00 da kelishdik» deb javob berdingiz."
        ),
    )
    channel = FakeChannel()

    outcome = await runtime(env, fake, channel).run(turn)

    assert outcome.status is RunStatus.SUCCEEDED
    assert channel.sends == [
        "Kamola 15:00 ni taklif qildi, siz «Ha, soat 15:00 da kelishdik» deb javob berdingiz."
    ]
    [resolved] = tool_results(fake.requests[1])
    assert resolved["candidates"][0]["contact_id"] == contact_id  # type: ignore[index]
    search = tool_results(fake.requests[2])[-1]
    found = [r["text"] for r in search["results"]]  # type: ignore[union-attr, index]
    assert any("учрашувни" in str(t) for t in found)  # Latin query, Cyrillic message
    assert any("kelishdik" in str(t) for t in found)
    for item in search["results"]:  # type: ignore[union-attr]
        assert str(item["text"]).startswith("<untrusted source=")  # type: ignore[index]
    assert search["coverage"]["rule"]  # type: ignore[index]

    async with env.factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
        reply = (
            await session.execute(select(ControlTurn).where(ControlTurn.role == "assistant"))
        ).scalar_one()
        calls = (await session.execute(select(LlmCall))).scalars().all()
    assert run.tainted is True
    assert run.prompt_version == "owner_request.v2"
    assert reply.refs == {"conversation_ids": [conversation_id], "contact_ids": [contact_id]}
    assert reply.contains_managed is True
    assert len(calls) == 3
    assert await note_rows(env) == 0
