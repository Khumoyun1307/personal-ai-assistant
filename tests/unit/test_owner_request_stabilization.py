"""H6: the selected prompt and strict tool API must cut over together."""

from hashlib import sha256

import pytest

from assistant.agent import prompt_registry
from assistant.agent.owner_requests import classify_message
from assistant.agent.tools.owner_composition import (
    PROPOSE_OWNER_MESSAGE_FLAG,
    ProposeOwnerMessageArgs,
    ReviseOwnerDraftArgs,
)
from assistant.agent.tools.registry import CapabilityTier, default_registry

LEGACY_HASH = "9b0442834a0765f43454e0e9b650264121f70a1134c2c756b9c432a595172efe"
WRITER_FLAGS = frozenset(
    {"tool.propose_reply", "tool.propose_message", "tool.revise_draft", "tool.get_draft"}
)


@pytest.mark.parametrize(
    ("text", "operation", "recipient", "body"),
    [
        ("Write to Kamola.", "compose", "Kamola", None),
        ("Kamolaga yoz.", "compose", "Kamola", None),
        ("Камолага ёз.", "compose", "Камола", None),
        ("Напиши Камоле.", "compose", "Камоле", None),
        ("Tell Kamola exactly: X1 — e\u0301!\n  Max", "dictate", "Kamola", "X1 — e\u0301!\n  Max"),
        ("Kamolaga aynan yoz: X1.", "dictate", "Kamola", "X1."),
        ("Камолага айнан ёз: X1.", "dictate", "Камола", "X1."),
        ("Напиши Камоле дословно: X1.", "dictate", "Камоле", "X1."),
        ("Камоле дословно: X1.", "dictate", "Камоле", "X1."),
        ("New message to Kamola: ask about Friday.", "compose", "Kamola", None),
        ("Kamolaga yangi xabar yoz: kitobni so‘ra.", "compose", "Kamola", None),
        ("Reply to Kamola.", "reply", "Kamola", None),
        ("Kamolaga kitob haqida yoz.", "compose", "Kamola", None),
        ('Tell Kamola exactly: "X1  Max"', "dictate", "Kamola", "X1  Max"),
        ("Prepare a reply to Kamola.", "reply", "Kamola", None),
        ("Подготовь ответ Камоле.", "reply", "Камоле", None),
        ("Kamolaning savoliga javob tayyorla.", "reply", "Kamola", None),
        ("Камоланинг саволига жавоб тайёрла.", "reply", "Камола", None),
    ],
)
def test_explicit_message_intent_keeps_exact_source_offsets(
    text: str, operation: str, recipient: str, body: str | None
) -> None:
    intent = classify_message(text)
    assert intent is not None
    assert intent.operation == operation
    assert intent.recipient == recipient
    if body is None:
        assert intent.literal_span is None
    else:
        assert intent.literal_span is not None
        start, end = intent.literal_span
        assert text[start:end] == body


@pytest.mark.parametrize(
    "text",
    [
        "What did Kamola write?",
        "Search for Kamola",
        "Set my language to Russian",
        "Shorten the list; do not write to anyone.",
        "How do I write to Kamola?",
        "Do not send a message to Kamola: X1.",
        "Kamola",
        "Камоле дословно?",
    ],
)
def test_non_messaging_turn_has_no_new_request_intent(text: str) -> None:
    assert classify_message(text) is None


@pytest.mark.parametrize(
    ("text", "forms"),
    [
        ("Напиши Камоле.", {"Камола", "Камоля"}),
        ("Подготовь ответ Ивану.", {"Иван", "Ивана"}),
        ("Напиши Марии дословно: X1.", {"Мария"}),
        ("Напиши Камоле Зиё.", {"Камола Зиё", "Камоля Зиё"}),
    ],
)
def test_russian_dative_recipient_offers_nominative_lookup_forms(
    text: str, forms: set[str]
) -> None:
    intent = classify_message(text)
    assert intent is not None
    assert set(intent.recipient_forms) == forms


@pytest.mark.parametrize("text", ["Write to Kamola.", "Kamolaga yoz.", "Камолага ёз."])
def test_non_russian_recipient_has_no_alternative_lookup_forms(text: str) -> None:
    intent = classify_message(text)
    assert intent is not None
    assert intent.recipient_forms == ()


@pytest.mark.parametrize("main_writer", [False, True])
@pytest.mark.parametrize("managed", [False, True])
def test_selected_prompt_matches_the_offered_writer_contract(
    main_writer: bool, managed: bool
) -> None:
    flags = WRITER_FLAGS | ({PROPOSE_OWNER_MESSAGE_FLAG} if main_writer else set())
    prompt = prompt_registry.active("owner_request", enabled_flags=flags)
    offered = default_registry().for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=flags,
        managed_consent=managed,
        stub_managed_tools=True,
    )
    assert prompt.label == ("owner_request.v11" if main_writer else "owner_request.v10")
    assert ("propose_owner_message" in offered) is main_writer
    assert ("propose_reply" in offered) is not main_writer
    assert "propose_message" in offered
    assert "get_draft" in offered
    revision = offered["revise_draft"]
    if main_writer:
        assert offered["propose_owner_message"].args_model is ProposeOwnerMessageArgs
        assert revision.args_model is ReviseOwnerDraftArgs
        assert set(revision.spec().parameters["properties"]) == {
            "request_id",
            "request_version",
            "action_id",
            "expected_version",
            "edit",
            "text",
            "source_ids",
            "context_fence_id",
        }
    else:
        assert "literal" in revision.spec().parameters["properties"]
        assert sha256(prompt_registry.load_bytes("owner_request", "v10")).hexdigest() == LEGACY_HASH
        assert prompt.text == prompt_registry.load("owner_request", "v10").text
    if not managed:
        assert all(not tool.returns_managed for tool in offered.values())


def test_prompt_selection_has_no_cached_flag_leak_and_keeps_historical_lookup() -> None:
    legacy = prompt_registry.active("owner_request")
    candidate = prompt_registry.active(
        "owner_request", enabled_flags=frozenset({PROPOSE_OWNER_MESSAGE_FLAG})
    )
    assert candidate.label == "owner_request.v11"
    assert prompt_registry.active("owner_request") is legacy
    assert prompt_registry.active("owner_request", enabled_flags=WRITER_FLAGS) is legacy
    assert candidate is prompt_registry.load("owner_request", "v11")
    for number in range(1, 12):
        assert prompt_registry.load_bytes("owner_request", f"v{number}")
    assert prompt_registry.active("draft_owner").label == "draft_owner.v3"
    assert prompt_registry.active("draft_reply").label == "draft_reply.v1"


@pytest.mark.parametrize(
    "run_type", ["triage", "draft_reply", "summarize", "extract_memory", "evaluate_monitor"]
)
def test_main_writer_flag_grants_no_contact_triggered_tools(run_type: str) -> None:
    assert (
        default_registry().for_run(
            run_type,
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=WRITER_FLAGS | {PROPOSE_OWNER_MESSAGE_FLAG},
            managed_consent=True,
        )
        == {}
    )
