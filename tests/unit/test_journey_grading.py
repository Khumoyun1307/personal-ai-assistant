"""A fluent FakeLLM answer must never make a wrong artifact pass its oracle."""

import hashlib
from dataclasses import replace

import pytest
from aiogram.exceptions import TelegramNetworkError
from pydantic import ValidationError

from tests.fakes.sender import FakeTelegramSender, network_error
from tests.journeys.grading import CardArtifact, assert_card, assert_claim
from tests.journeys.matrix import MATRIX, owner_input
from tests.journeys.models import LOCALES, JourneyCase

EXPECTED = CardArtifact(11, 22, "CANARY-SYNTH X1 — Oʻzbek!", 2, "rejected", "owner_dictated", None)


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": 12},
        {"conversation_id": 23},
        {"text": "CANARY-SYNTH x1 — O'zbek!"},
        {"version": 1},
        {"state": "succeeded"},
        {"origin": "agent_proposal"},
        {"reason": "takeover"},
    ],
)
def test_wrong_recipient_exact_text_version_or_state_fails_grading(
    changes: dict[str, object],
) -> None:
    actual = replace(EXPECTED, **changes)
    [field] = changes
    with pytest.raises(AssertionError, match=f"card {field}: expected") as caught:
        assert_card(actual, EXPECTED)
    assert str(caught.value) == (
        f"card {field}: expected {getattr(EXPECTED, field)!r}, actual {getattr(actual, field)!r}"
    )


@pytest.mark.parametrize(
    "state",
    [
        "awaiting_approval",
        "approved",
        "executing",
        "rejected",
        "outdated",
        "cancelled",
        "blocked",
        "failed",
        "expired",
        "delivery_uncertain",
    ],
)
def test_absence_from_live_cards_does_not_prove_sent(state: str) -> None:
    with pytest.raises(AssertionError, match=f"claimed status for state {state!r}: expected"):
        assert_claim(replace(EXPECTED, state=state), claimed_version=2, claimed_status="sent")


def test_purged_or_changed_version_never_proves_content() -> None:
    body_hash = hashlib.sha256((EXPECTED.text or "").encode()).hexdigest()
    assert_claim(
        EXPECTED, claimed_version=2, claimed_status="not_sent", claimed_text_hash=body_hash
    )
    with pytest.raises(AssertionError, match="card text for content claim: expected"):
        assert_claim(
            replace(EXPECTED, text=None),
            claimed_version=2,
            claimed_status="not_sent",
            claimed_text_hash=body_hash,
        )
    with pytest.raises(AssertionError, match="claimed card version: expected 1, actual 2"):
        assert_claim(EXPECTED, claimed_version=1, claimed_status="not_sent")


def test_generated_cross_chat_leak_fails_even_with_correct_recipient_and_state() -> None:
    expected = replace(EXPECTED, origin="agent_proposal", text="SYNTH ask about the book")
    leaked = replace(expected, text=f"{expected.text} CANARY-SYNTH-NORA-PRIVATE-CODE")
    with pytest.raises(AssertionError, match="card text: expected"):
        assert_card(leaked, expected)
    with pytest.raises(AssertionError, match="claimed card text SHA-256: expected"):
        assert_claim(
            leaked,
            claimed_version=expected.version,
            claimed_status="not_sent",
            claimed_text_hash=hashlib.sha256((expected.text or "").encode()).hexdigest(),
        )


def test_case_format_rejects_executable_strings_and_unknown_steps() -> None:
    with pytest.raises(ValidationError):
        JourneyCase.model_validate(
            {
                "case_id": "hostile-fixture",
                "locale": "en",
                "steps": [{"kind": "eval", "code": "raise RuntimeError"}],
                "deterministic_checks": [],
            }
        )
    with pytest.raises(ValidationError):
        JourneyCase.model_validate(
            {
                "case_id": "unknown-setup",
                "locale": "en",
                "setup": [{"kind": "directory", "alias": "k", "name": "Synthetic", "id": 1}],
                "steps": [],
                "deterministic_checks": [],
            }
        )


def test_frozen_matrix_has_every_row_and_locale_without_real_content() -> None:
    assert [row["id"] for row in MATRIX] == [f"J{index:02}" for index in range(1, 29)]
    for row in MATRIX:
        assert set(row) == {"id", *LOCALES}
        for locale in LOCALES:
            assert owner_input(row["id"], locale)


async def test_only_successful_control_delivery_can_support_a_visible_reply_claim() -> None:
    sender = FakeTelegramSender()
    await sender.send_message(chat_id=7000000001, text="CANARY-SYNTH delivered.")
    sender.fail_with = network_error()
    with pytest.raises(TelegramNetworkError):
        await sender.send_message(chat_id=7000000001, text="CANARY-SYNTH uncertain.")
    assert len(sender.sends) == 2
    assert len(sender.delivered_control_sends) == 1
    assert sender.delivered_control_sends[0]["text"] == "CANARY-SYNTH delivered."
