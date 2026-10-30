"""PF-15 (ISSUE-003, -006, -009): the owner run's working context.

An owner run now sees which approval cards are live (ids, version, state,
origin, mode — never their text, never a name) and, on each earlier turn,
the ids that turn concerned (`control_turns.refs`), so the model can point at
the chat and card the owner means. The block is content-free and leaves the
consent provenance and the taint semantics exactly as they were. Every text
here is synthetic.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from assistant.accounts.models import Account, ControlTurn
from assistant.actions.activity import LiveCardFact
from assistant.agent import prompt_registry
from assistant.agent.context import (
    LIVE_CARDS_SHOWN,
    build_owner_request,
    render_live_cards,
    render_refs,
)
from assistant.llm.port import Provenance

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
#: Synthetic texts that must never reach the block.
SECRET_DRAFT = "SYNTH draft text Dilnoza must not see"
SECRET_NAME = "Xurshida Synthetic"


def turn(**kwargs: Any) -> ControlTurn:
    values: dict[str, Any] = {
        "role": "owner",
        "text": "Synthetic owner text",
        "third_party": False,
        "quoted_text": None,
        "contains_managed": False,
        "from_tainted_run": False,
        "refs": {},
    }
    values.update(kwargs)
    return ControlTurn(**values)


def account() -> Account:
    return Account(telegram_user_id=1, timezone="Asia/Tashkent", locale_pref="uz-Latn")


def card(**changes: Any) -> LiveCardFact:
    values: dict[str, Any] = {
        "action_id": 41,
        "conversation_id": 7,
        "version": 2,
        "state": "awaiting_approval",
        "origin": "agent_proposal",
        "mode": "reply",
        "answers_message": True,
    }
    values.update(changes)
    return LiveCardFact(**values)


def build(**changes: Any) -> Any:
    values: dict[str, Any] = {
        "prompt": prompt_registry.active("owner_request"),
        "account": account(),
        "history": [],
        "turn": turn(text="Synthetic current request"),
        "now": NOW,
        "managed_allowed": True,
    }
    values.update(changes)
    return build_owner_request(**values)


# ------------------------------------------------------------ the block
def test_the_live_cards_block_carries_ids_and_states_only() -> None:
    text = render_live_cards(
        [
            card(),
            card(
                action_id=42,
                conversation_id=9,
                version=1,
                state="revising",
                origin="owner_dictated",
                mode=None,
                answers_message=False,
            ),
        ]
    )
    assert "action 41" in text
    assert "chat 7" in text
    assert "version 2" in text
    assert "awaiting_approval" in text
    assert "agent_proposal" in text
    assert "mode reply" in text
    assert "action 42" in text
    assert "chat 9" in text
    assert "revising" in text
    assert "owner_dictated" in text
    lines = text.splitlines()
    assert "answers a specific message" in lines[1]
    assert "answers a specific message" not in lines[2]


def test_the_block_never_shows_a_mode_outside_the_fixed_set() -> None:
    """`draft_flags` is jsonb: a value that is not a known mode is never
    copied into the prompt (content-free by construction)."""
    text = render_live_cards([card(mode=SECRET_DRAFT)])
    assert SECRET_DRAFT not in text
    assert "mode" not in text.splitlines()[1]


def test_the_block_says_none_when_no_card_is_live() -> None:
    text = render_live_cards([])
    assert "none" in text.lower()


def test_the_block_is_capped_and_counts_the_rest() -> None:
    cards = [card(action_id=100 + i, conversation_id=i + 1) for i in range(LIVE_CARDS_SHOWN + 3)]
    text = render_live_cards(cards)
    assert f"action {100 + LIVE_CARDS_SHOWN - 1}" in text
    assert f"action {100 + LIVE_CARDS_SHOWN}" not in text
    assert "3 more" in text


def test_the_block_is_digits_enums_and_fixed_words_only() -> None:
    """Content-free: nothing but the fixed wording, enum values and ids."""
    text = render_live_cards([card(), card(action_id=5, state="revising", mode="new_message")])
    allowed = re.compile(r"^[A-Za-z0-9_ ,.;:()\-'\n]*$")
    assert allowed.fullmatch(text), text


# ------------------------------------------------------------ refs on turns
def test_refs_render_as_ids_only_and_ignore_anything_else() -> None:
    note = render_refs(
        {
            "conversation_ids": [7, 9],
            "contact_ids": [3],
            # Batch A's Edit-button turn writes a list (approvals.py, PF-01).
            "revision_action_id": [41],
            "something_else": [SECRET_NAME],
            "alert_kind": SECRET_DRAFT,
        }
    )
    assert note is not None
    assert "chats 7, 9" in note
    assert "contacts 3" in note
    assert "card 41" in note
    assert SECRET_NAME not in note
    assert SECRET_DRAFT not in note


def test_a_revision_turn_names_its_card_in_either_shape() -> None:
    assert render_refs({"revision_action_id": [41]}) == (
        "(application note, not part of the message: this turn concerned card 41)"
    )
    assert "cards 41, 42" in (render_refs({"revision_action_id": [41, 42]}) or "")
    # A bare integer is still read; a string or a bool never is.
    assert "card 41" in (render_refs({"revision_action_id": 41}) or "")
    assert render_refs({"revision_action_id": "41"}) is None
    assert render_refs({"revision_action_id": [True]}) is None


def test_refs_with_no_ids_render_nothing() -> None:
    assert render_refs({}) is None
    assert render_refs({"conversation_ids": []}) is None
    assert render_refs({"conversation_ids": ["7"], "contact_ids": [True]}) is None


def test_a_history_turn_shows_its_refs_outside_the_untrusted_wrapper() -> None:
    reply = turn(
        role="assistant",
        text="Synthetic earlier reply",
        from_tainted_run=True,
        contains_managed=True,
        refs={"conversation_ids": [7], "contact_ids": [3]},
    )
    context = build(history=[reply])
    rendered = context.items[0].text
    assert 'trust="untrusted"' in rendered
    closing = rendered.rindex("</")
    assert rendered.index("chats 7") > closing  # the application's note, not the reply's text


def test_the_current_turn_carries_no_refs_note() -> None:
    context = build(turn=turn(text="Synthetic current request", refs={"conversation_ids": [7]}))
    assert "chats 7" not in context.items[-1].text


# ------------------------------------------------------------ placement
def test_the_block_comes_after_history_and_before_the_current_turn() -> None:
    history = [turn(text="Synthetic earlier request"), turn(role="assistant", text="Synthetic ok")]
    context = build(history=history, live_cards=[card()])
    texts = [item.text for item in context.items]
    assert texts[0] == "Synthetic earlier request"
    assert "action 41" in texts[2]
    assert texts[3] == "Synthetic current request"
    assert context.items[2].role == "user"
    # Content-free and written by the application: it carries no managed text.
    assert context.items[2].provenance is Provenance.OWNER_CONTROL


def test_the_instructions_stay_the_same_with_or_without_the_block() -> None:
    """Prompt caching: the stable prefix does not move."""
    plain = build()
    with_cards = build(live_cards=[card()])
    assert plain.instructions == with_cards.instructions
    assert "action 41" not in with_cards.instructions


def test_no_block_without_live_card_facts() -> None:
    context = build(live_cards=None)
    assert len(context.items) == 1
    assert not any("Live approval cards" in item.text for item in context.items)


def test_no_block_without_ai_managed_even_when_facts_are_given() -> None:
    """Cards are about the owner's chats with other people: without
    `ai_managed` not even their ids are shown (defence in depth, inv. 9)."""
    context = build(managed_allowed=False, live_cards=[card()])
    assert not any("action 41" in item.text for item in context.items)


def test_no_refs_note_without_ai_managed() -> None:
    """Edit-button turns (PF-01) are not managed content but carry chat and
    card ids; without `ai_managed` those ids are not shown either."""
    edit_turn = turn(
        text="Synthetic edit instruction",
        refs={"conversation_ids": [7], "contact_ids": [3], "revision_action_id": [41]},
    )
    context = build(managed_allowed=False, history=[edit_turn])
    [earlier] = [item for item in context.items if "Synthetic edit instruction" in item.text]
    assert earlier.text == "Synthetic edit instruction"
    shown = build(history=[edit_turn])
    assert any("card 41" in item.text for item in shown.items)


# ------------------------------------------------------------ taint unchanged
def test_the_block_and_refs_never_change_taint() -> None:
    clean = [turn(text="Synthetic earlier request", refs={"conversation_ids": [7]})]
    assert build(history=clean, live_cards=[card()]).tainted is False
    forwarded = [turn(text="Synthetic forward", third_party=True)]
    tainted = build(history=forwarded, live_cards=[card()])
    baseline = build(history=forwarded)
    assert (tainted.tainted, tainted.directly_tainted) == (
        baseline.tainted,
        baseline.directly_tainted,
    )
    assert tainted.tainted is True
    assert tainted.directly_tainted is False


def test_provenance_of_every_other_item_is_unchanged() -> None:
    history = [
        turn(text="Synthetic earlier request"),
        turn(role="assistant", text="Synthetic managed reply", contains_managed=True),
    ]
    with_cards = build(history=history, live_cards=[card()])
    without = build(history=history)
    shown = [item for item in with_cards.items if "Live approval cards" not in item.text]
    assert [item.provenance for item in shown] == [item.provenance for item in without.items]
    assert [item.role for item in shown] == [item.role for item in without.items]
