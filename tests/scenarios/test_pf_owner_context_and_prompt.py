"""PF-15 and PF-14 through the production wiring (ISSUE-001/002/003/006/007/009).

PF-15: a later owner run sees the live card it can act on (content-free:
ids and states, never the draft's text) and the ids the earlier turn
concerned. PF-14: the active `owner_request` prompt carries the identity and
the intent-routing rules, and the draft tools are offered exactly when their
release flags are on. The real inbox, owner runtime and tool registry; only
the clock, the transport and the model are fake. Every text is synthetic.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from assistant.accounts.models import ControlTurn
from assistant.agent.context import carries_third_party
from assistant.agent.models import AgentRun
from assistant.llm.port import LLMRequest, Provenance, TextItem
from tests.fakes.llm import text_response
from tests.integration.agent_env import ask
from tests.scenarios.p2_drive import DRAFT, FLAGS, Scenario, m04_environment, run_queued

pytestmark = pytest.mark.integration

DRAFT_FLAGS = frozenset({"tool.revise_draft", "tool.get_draft"})
CONTENT_FREE = re.compile(r"^[A-Za-z0-9_ ,.;:()\-'\n]*$")


@pytest.fixture
async def s(migrated_url: str) -> AsyncIterator[Scenario]:
    async with m04_environment(migrated_url, flags=FLAGS | DRAFT_FLAGS) as scenario:
        yield scenario


def owner_requests(s: Scenario) -> list[LLMRequest]:
    return [r for r in s.fake.requests if r.purpose == "owner_request"]


async def plain_turn(s: Scenario, said: str = "Synthetic follow-up about the draft.") -> None:
    s.fake.queue(text_response("Synthetic answer."))
    await ask(s.env, said)
    assert await run_queued(s, "agent.owner_request") == 1


def live_block(request: LLMRequest) -> TextItem:
    [block] = [
        item
        for item in request.items
        if isinstance(item, TextItem) and item.text.startswith("Live approval cards now")
    ]
    return block


async def runs(s: Scenario) -> list[AgentRun]:
    async with s.env.factory() as session:
        return list(
            await session.scalars(
                select(AgentRun)
                .where(AgentRun.account_id == s.account_id, AgentRun.run_type == "owner_request")
                .order_by(AgentRun.id)
            )
        )


# ------------------------------------------------------------ PF-15
async def test_a_later_run_sees_the_live_card_without_its_text(s: Scenario) -> None:
    row = await s.propose()
    await plain_turn(s)

    request = owner_requests(s)[-1]
    block = live_block(request)
    assert f"action {row.id}: chat {s.conversation_id}" in block.text
    assert "version 1" in block.text
    assert "awaiting_approval" in block.text
    assert "mode reply" in block.text
    assert DRAFT not in block.text  # never the draft's text
    assert CONTENT_FREE.fullmatch(block.text), block.text
    assert block.provenance is Provenance.OWNER_CONTROL
    texts = [item for item in request.items if isinstance(item, TextItem)]
    # After the history, right before the owner's new message.
    assert texts[-2] is block
    assert texts[-1].text == "Synthetic follow-up about the draft."
    # The earlier reply names the chat it concerned (application note).
    earlier = texts[-3]
    assert earlier.role == "assistant"
    assert f"chats {s.conversation_id}" in earlier.text


async def test_the_block_shows_none_once_the_card_is_no_longer_live(s: Scenario) -> None:
    row = await s.propose()
    async with s.env.factory() as session:
        action = await session.get(type(row), row.id)
        assert action is not None
        action.state = "rejected"
        await session.commit()
    await plain_turn(s)
    block = live_block(owner_requests(s)[-1])
    assert block.text.endswith("none.")
    assert f"action {row.id}" not in block.text


async def test_the_working_context_does_not_change_taint(s: Scenario) -> None:
    """The later run's taint is exactly what its context carries (the §7.1
    rule, unchanged): the live-cards block and the refs notes never taint
    and never clear taint."""
    await s.propose()
    await plain_turn(s)
    _, second = await runs(s)
    async with s.env.factory() as session:
        turns = list(
            await session.scalars(
                select(ControlTurn)
                .where(ControlTurn.account_id == s.account_id)
                .order_by(ControlTurn.id)
            )
        )
    current = next(t for t in turns if t.text == "Synthetic follow-up about the draft.")
    history = [t for t in turns if t.id < current.id]
    assert second.tainted == any(carries_third_party(t) for t in [*history, current])


# ------------------------------------------------------------ PF-14
async def test_the_active_prompt_carries_identity_and_routing(s: Scenario) -> None:
    await plain_turn(s, "Synthetic: what is your name?")
    instructions = owner_requests(s)[-1].instructions
    assert "Alviro" in instructions
    assert "Never claim to be ChatGPT, another product or a person" in instructions
    assert "AI model from OpenAI" in instructions
    for phrase in (
        'never answer it with "no reply needed"',
        "`revise_draft`",
        "`get_draft`",
        "mode `new_message`",
        "`answered_message_id`",
        "make no card",
        "Never tell the owner to ask again in a new message",
        "chat partners",
        "card's Edit button",
    ):
        assert phrase in instructions, phrase
    assert "ask again in a new message" not in instructions.replace(
        "Never tell the owner to ask again in a new message", ""
    )


async def test_the_draft_tools_are_offered_with_their_flags_on(s: Scenario) -> None:
    await plain_turn(s)
    offered = {tool.name for tool in owner_requests(s)[-1].tools}
    assert {"propose_reply", "propose_message", "revise_draft", "get_draft"} <= offered


async def test_the_draft_tools_are_not_offered_with_their_flags_off(migrated_url: str) -> None:
    async with m04_environment(migrated_url, flags=FLAGS) as s:
        await plain_turn(s)
        request = owner_requests(s)[-1]
    offered = {tool.name for tool in request.tools}
    assert {"propose_reply", "propose_message"} <= offered
    assert not offered & {"revise_draft", "get_draft"}
    # The prompt's fallback for that case is there.
    assert "When `revise_draft` or `get_draft` is not among your tools" in request.instructions
