"""PF-14: `owner_request.v10` (owner decisions D1-D4 and D7 of 2026-10-30).

v9 word for word except the rules the P2 playtest showed wrong (rule 3
gains one sentence, rules 18 and 19 are rewritten), plus the identity, the
intent preamble and rules 26-28. Rules are checked by their meaning-bearing
phrases, not by exact wording elsewhere (AGENTS §8).
"""

from __future__ import annotations

import re

from assistant.agent import prompt_registry


def _rule(prompt: str, number: int) -> str:
    start = prompt.index(f"\n{number}. ")
    end = prompt.find(f"\n{number + 1}. ", start)
    return prompt[start : end if end != -1 else None]


V9 = prompt_registry.load("owner_request", "v9").text
V10 = prompt_registry.load("owner_request", "v10").text


def test_owner_request_v10_is_active_and_v1_to_v9_remain() -> None:
    assert prompt_registry.ACTIVE["owner_request"] == "v10"
    assert prompt_registry.active("owner_request").label == "owner_request.v10"
    for number in range(1, 10):
        assert prompt_registry.load("owner_request", f"v{number}").text


def test_v10_keeps_v9_word_for_word_except_the_changed_rules() -> None:
    assert V10.startswith(V9.split("\n\nRules:")[0])  # the opening line is unchanged
    for number in range(1, 26):
        if number in (18, 19):
            continue
        if number == 3:
            assert _rule(V10, 3).startswith(_rule(V9, 3))
            continue
        assert _rule(V10, number) == _rule(V9, number), number
    for number in (26, 27, 28):
        assert _rule(V10, number)
    assert f"\n{29}. " not in V10


def test_v10_states_the_identity() -> None:
    identity = V10.split("\n\nRules:")[0]
    assert "your name is Alviro" in identity
    assert "AI assistant" in identity
    assert "Never claim to be ChatGPT, another product or a person" in identity
    assert "AI model from OpenAI" in identity
    assert "owner's language" in identity


def test_v10_routes_by_intent_and_never_refuses_an_explicit_request() -> None:
    rule18 = _rule(V10, 18)
    assert "always carried out (with `propose_reply`, or with `propose_message` as below)" in rule18
    assert 'never answer it with "no reply needed"' in rule18
    assert "`answered_message_id`" in rule18
    assert "`get_conversation`" in rule18
    assert "mode `new_message`" in rule18
    assert "`propose_message` with exactly those words" in rule18
    assert "never the owner's command words" in rule18
    assert "never words addressed to you" in rule18
    assert "that suggestion's exact text" in rule18
    assert "make no card" in rule18
    assert "listing contacts drafts and proposes nothing" in rule18
    assert "`revise_draft` on that chat's live card, never a new propose" in rule18
    assert "`append_text`" in rule18
    assert "call `get_draft`" in rule18
    assert "never from memory" in rule18
    assert "`resolve_contact`" in rule18
    assert "before any propose or revise call" in rule18
    assert "card's Edit button" in rule18
    assert "`revise_draft`" in _rule(V10, 3)


def test_v10_rule_19_relays_statuses_truthfully() -> None:
    rule19 = _rule(V10, 19)
    assert "never a send" in rule19
    assert "specifically" in rule19
    assert "never claim a property of a draft" in rule19
    assert "Never tell the owner to ask again in a new message" in rule19
    assert "live status" in rule19
    assert '"not sent yet"' in rule19
    for status in ("awaiting_approval", "revised", "blocked", "not_drafted", "not_revised"):
        assert f"`{status}`" in rule19, status
    # v9's advice to ask again is gone everywhere.
    assert "they can ask again in a new message" not in V10


def test_v10_working_context_contacts_and_lists() -> None:
    assert "written by the application" in _rule(V10, 26)
    assert "never infer a draft's content" in _rule(V10, 26)
    rule27 = _rule(V10, 27)
    assert "chat partners" in rule27
    assert '"weak"' in rule27
    assert "confirm" in rule27
    rule28 = _rule(V10, 28)
    assert "edits the list and drafts or proposes nothing" in rule28
    assert "ask one short question" in rule28


def test_v10_has_no_timestamp_or_identifier() -> None:
    assert not re.search(r"\d{4}-\d{2}-\d{2}", V10)
    assert not re.search(r"\d{5,}", V10)
    assert not re.search(r"(?<![\w`])@\w", V10)
