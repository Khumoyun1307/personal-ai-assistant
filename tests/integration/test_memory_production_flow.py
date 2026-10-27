"""M03 fix round C2: notes through the production wiring.

Owner checklist step 8 (production, 2026-10-27): "remember" answered "saving
failed" and reading the notes answered with an error; both runs succeeded, no
card was shown and nothing was logged. Here the real `OwnerRequestRuntime`
runs with the production registry, the verbatim production flags line, both
consents and a history window that carries other people's text (as after
steps 5-7), and FakeLLM does what a real model does: `resolve_contact`
first, then the memory tool with the ids the candidate carries.

- `remember` with both ids copied from the candidate is a note about the
  contact: a confirmation card in a tainted run (Confirm saves exactly one,
  echoed with Undo), saved at once in a clean run;
- `list_memories` after `resolve_contact` lists the contact's notes and their
  chat's, with `scope` "contact" or with both ids (never one filter hiding
  the other's notes);
- an id that is not the account's is still "not found" (§6 invariant 12) and
  now writes exactly one content-free warning.

Synthetic text only; FakeLLM stands in for the provider (AGENTS §11).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from assistant.accounts.models import Account, ControlTurn, PendingInput
from assistant.agent.models import AgentRun, ToolConfirmation
from assistant.agent.tools.registry import default_registry
from assistant.conversations.models import Contact
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest, LLMResponse, ToolResultItem
from assistant.memory import items as memory_items
from assistant.memory.models import MemoryItem
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import PRODUCTION_FLAGS, applier, args, ask, one_id, runtime
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    grant_managed,
)

pytestmark = pytest.mark.integration

TOOL_EVENTS = {"agent_tool_call_failed", "agent_tool_call_not_run"}


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


def production(env: Env, fake: FakeLLM, channel: FakeChannel) -> Any:
    """As `bootstrap.build_owner_runtime`: the production registry (no
    test-only tools) and the production flags line."""
    return runtime(env, fake, channel, flags=PRODUCTION_FLAGS, registry=default_registry())


async def seed(env: Env) -> None:
    """A connected owner with `ai_managed`, and two contacts with messages."""
    await connect(env)
    await grant_managed(env)
    await deliver(
        env,
        business_message(
            message_id=11, message_text="Synthetic: design is mine", first_name="Kamola"
        ),
        business_message(
            chat_id=OTHER_CONTACT,
            from_id=OTHER_CONTACT,
            message_id=21,
            message_text="Synthetic other text",
            first_name="Botir",
            date=T_BASE + 60,
        ),
    )


async def kamola_ids(env: Env) -> tuple[int, int]:
    contact = await one_id(
        env, "SELECT id FROM app.contacts WHERE telegram_user_id = :u", u=CONTACT
    )
    chat = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    return contact, chat


def results(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(item.output) for item in request.items if isinstance(item, ToolResultItem)]


def remember_args(content: str, **extra: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "content": content,
        "kind": "fact",
        "contact_id": None,
        "conversation_id": None,
        "applies_to_replies": False,
        "replaces_memory_id": None,
    }
    values.update(extra)
    return values


def list_args(**extra: Any) -> dict[str, Any]:
    return {"contact_id": None, "conversation_id": None, "scope": None, "limit": None, **extra}


RESOLVE = ("c1", "resolve_contact", args(name="Kamola", limit=None))


def from_candidate(
    call_id: str, tool: str, values: dict[str, Any], copy: tuple[str, ...]
) -> Callable[[LLMRequest], LLMResponse]:
    """The model's next call, with the ids named in `copy` taken from the
    first candidate `resolve_contact` returned (as a real model does)."""

    def step(request: LLMRequest) -> LLMResponse:
        candidate = results(request)[-1]["candidates"][0]
        copied = {key: candidate[key] for key in copy}
        return tool_response((call_id, tool, json.dumps({**values, **copied})))

    return step


async def earlier_chat_question(env: Env) -> None:
    """Steps 5-7 before step 8: an owner request that read a chat. Its reply
    carries other people's text, so the next run starts tainted."""
    _, chat = await kamola_ids(env)
    turn = await ask(env, "What did Kamola write?")
    fake = FakeLLM(
        tool_response(
            (
                "h1",
                "get_conversation",
                args(conversation_id=chat, contact_id=None, cursor=None, limit=None),
            )
        ),
        text_response("Synthetic answer [msg:11]."),
    )
    outcome = await production(env, fake, FakeChannel()).run(turn)
    assert outcome.status.value == "succeeded"
    async with env.factory() as session:
        replies = (
            await session.execute(select(ControlTurn).where(ControlTurn.role == "assistant"))
        ).scalars()
        assert any(reply.third_party for reply in replies)


async def notes(env: Env) -> list[MemoryItem]:
    async with env.factory() as session:
        return list(
            (await session.execute(select(MemoryItem).order_by(MemoryItem.id))).scalars().all()
        )


async def save_note(env: Env, content: str, **target: int) -> int:
    async with transaction(env.factory) as uow:
        account_id = await one_id(
            env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER
        )
        saved = await memory_items.save(
            uow.session,
            account_id=account_id,
            content=content,
            kind="fact",
            actor="owner",
            now=env.clock.now(),
            **target,
        )
    return saved.item.memory_id


def tool_lines(logs: list[Any]) -> list[Any]:
    return [entry for entry in logs if entry["event"] in TOOL_EVENTS]


# ------------------------------------------------------------ remember
async def test_remember_with_both_ids_from_resolve_contact_is_held_for_a_card(env: Env) -> None:
    """Step 8 in production: the run is tainted (the history window, and
    `resolve_contact`'s names), so the note is a card first; Confirm saves
    exactly one note about the contact, echoed with Undo."""
    await seed(env)
    await earlier_chat_question(env)
    kamola, chat = await kamola_ids(env)
    turn = await ask(env, "Remember that Kamola handles the design")
    fake = FakeLLM(
        tool_response(RESOLVE),
        from_candidate(
            "c2",
            "remember",
            remember_args("SYNTH Kamola handles the design"),
            ("contact_id", "conversation_id"),
        ),
        text_response("Please confirm."),
    )
    channel = FakeChannel()
    with capture_logs() as logs:
        outcome = await production(env, fake, channel).run(turn)

    assert outcome.status.value == "succeeded"
    [candidate] = results(fake.requests[1])[0]["candidates"]
    assert (candidate["contact_id"], candidate["conversation_id"]) == (kamola, chat)
    held = results(fake.requests[2])[-1]
    assert held["status"] == "awaiting_confirmation", held
    assert "Confirm a change?" in channel.sends[0]
    assert [b.label for b in channel.buttons[0]] == ["Confirm", "Reject"]
    assert await notes(env) == []
    # The card shows the normalised arguments: what Confirm applies.
    async with env.factory() as session:
        row = (await session.execute(select(ToolConfirmation))).scalar_one()
        card = (
            await session.execute(select(PendingInput).where(PendingInput.kind == "tool_confirm"))
        ).scalar_one()
    assert row.tool_name == "remember"
    assert row.args is not None
    assert (row.args["contact_id"], row.args["conversation_id"]) == (kamola, None)
    # A held call is logged as not run (not as a failure), once.
    [line] = tool_lines(logs)
    assert (line["event"], line["tool"], line["code"]) == (
        "agent_tool_call_not_run",
        "remember",
        "hold:card_sent",
    )

    await deliver(env, callback(f"tool_confirm:{card.token}:confirm"))
    applied_channel = FakeChannel()
    applied = await applier(env, applied_channel, flags=PRODUCTION_FLAGS).apply(row.id)
    assert applied.status == "applied"
    [note] = await notes(env)
    assert (note.scope, note.contact_id, note.conversation_id) == ("contact", kamola, None)
    assert note.status == "active"
    assert [b.label for b in applied_channel.buttons[0]] == ["Undo"]


async def test_remember_with_both_ids_in_a_clean_run_saves_a_contact_note(env: Env) -> None:
    await seed(env)
    kamola, chat = await kamola_ids(env)
    turn = await ask(env, "Remember that Kamola handles the design")
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "remember",
                json.dumps(
                    remember_args("SYNTH handles design", contact_id=kamola, conversation_id=chat)
                ),
            )
        ),
        text_response("Saved."),
    )
    channel = FakeChannel()
    await production(env, fake, channel).run(turn)

    [saved] = results(fake.requests[1])
    assert saved["status"] == "ok", saved
    assert (saved["scope"], saved["contact_id"], saved["conversation_id"]) == (
        "contact",
        kamola,
        None,
    )
    async with env.factory() as session:
        run = (await session.execute(select(AgentRun))).scalar_one()
    assert run.tainted is False
    [note] = await notes(env)
    assert (note.scope, note.contact_id) == ("contact", kamola)
    assert [b.label for b in channel.buttons[0]] == ["Undo"]


# ------------------------------------------------------------ list_memories
@pytest.mark.parametrize(
    ("values", "copy"),
    [
        (list_args(scope="contact"), ("contact_id",)),
        (list_args(), ("contact_id", "conversation_id")),
        (list_args(scope="contact"), ("contact_id", "conversation_id")),
        (list_args(scope="conversation"), ("contact_id", "conversation_id")),
    ],
    ids=["contact_id+scope", "both_ids", "both_ids+scope", "both_ids+conversation_scope"],
)
async def test_what_do_you_know_about_a_contact_lists_their_notes(
    env: Env, values: dict[str, Any], copy: tuple[str, ...]
) -> None:
    """Step 8's "What do you know about Name?": the contact's note and their
    chat's note are listed; other people's and the owner's are not."""
    await seed(env)
    await earlier_chat_question(env)
    kamola, chat = await kamola_ids(env)
    botir = await one_id(
        env, "SELECT id FROM app.contacts WHERE telegram_user_id = :u", u=OTHER_CONTACT
    )
    about_her = await save_note(env, "SYNTH Kamola handles the design", contact_id=kamola)
    about_chat = await save_note(env, "SYNTH this chat is about the logo", conversation_id=chat)
    await save_note(env, "SYNTH Botir is in sales", contact_id=botir)
    await save_note(env, "SYNTH I prefer mornings")
    turn = await ask(env, "What do you know about Kamola?")
    fake = FakeLLM(
        tool_response(RESOLVE),
        from_candidate("c2", "list_memories", values, copy),
        text_response("Synthetic answer."),
    )
    await production(env, fake, FakeChannel()).run(turn)

    listed = results(fake.requests[2])[-1]
    assert listed["status"] == "ok", listed
    assert listed["count"] == 2
    assert {item["memory_id"] for item in listed["items"]} == {about_her, about_chat}


# ------------------------------------------------------------ not this account's
async def test_an_id_that_is_not_the_accounts_is_not_found_and_logged_once(env: Env) -> None:
    """Invariant 12 holds (the same answer for unknown and foreign ids), and
    each refusal now leaves one content-free warning instead of nothing."""
    await seed(env)
    await earlier_chat_question(env)
    async with env.factory() as session, session.begin():
        other = Account(telegram_user_id=7000000099, timezone="UTC")
        session.add(other)
        await session.flush()
        theirs = Contact(account_id=other.id, telegram_user_id=7000000098, display_name="SYNTH")
        session.add(theirs)
        await session.flush()
        foreign_contact = theirs.id
    unknown = await one_id(env, "SELECT coalesce(max(id), 0) + 1000 FROM app.contacts")
    turn = await ask(env, "Remember that Kamola handles the design")
    fake = FakeLLM(
        tool_response(
            RESOLVE,
            ("c2", "remember", json.dumps(remember_args("SYNTH", contact_id=foreign_contact))),
            ("c3", "list_memories", json.dumps(list_args(contact_id=unknown))),
        ),
        text_response("Not found."),
    )
    channel = FakeChannel()
    with capture_logs() as logs:
        outcome = await production(env, fake, channel).run(turn)

    assert outcome.status.value == "succeeded"
    _, remembered, listed = results(fake.requests[1])
    assert remembered == {"status": "error", "reason": "not found"}
    assert listed == {"status": "error", "reason": "not found"}
    assert not any("Confirm a change?" in text for text in channel.sends)
    async with env.factory() as session:
        assert (await session.execute(select(ToolConfirmation))).first() is None
    assert await notes(env) == []
    lines = tool_lines(logs)
    assert [(e["tool"], e["code"], e["exc_type"], e["log_level"]) for e in lines] == [
        ("remember", "not_found", "NotInAccountError", "warning"),
        ("list_memories", "not_found", "NotInAccountError", "warning"),
    ]
    assert all(e["event"] == "agent_tool_call_failed" for e in lines)
    assert "SYNTH" not in json.dumps(lines, default=str)
