"""The probe sanitiser is what keeps real chat data out of the repository.

AGENTS §9: fixtures are produced by a deterministic sanitiser that keeps an
allow-list of structural fields and replaces every string, name, username,
caption and file id. These tests pin that contract. `tools/` is outside `src/`,
so it is loaded by path — nothing in `src/` may import it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SANITIZE_PATH = Path(__file__).resolve().parents[2] / "tools" / "probe" / "sanitize.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("probe_sanitize", SANITIZE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sanitize = _load()


#: Shaped like a real business_message update, with invented content.
SAMPLE: dict[str, Any] = {
    "kind": "update",
    "update_type": "business_message",
    "payload": {
        "update_id": 884412233,
        "business_message": {
            "message_id": 4471,
            "business_connection_id": "KxQ2k9_realLookingConnectionId",
            "date": 1792300000,
            "text": "Salom, ertaga soat uchda uchrashamizmi?",
            "from": {
                "id": 584113992,
                "is_bot": False,
                "first_name": "Dilnoza",
                "last_name": "Karimova",
                "username": "dilnoza_k",
                "language_code": "uz",
            },
            "chat": {
                "id": 584113992,
                "type": "private",
                "first_name": "Dilnoza",
                "username": "dilnoza_k",
            },
            "entities": [{"type": "bold", "offset": 0, "length": 5}],
            "photo": [
                {
                    "file_id": "AgACAgIAAxkBAAIBY2_realFileId",
                    "file_unique_id": "AQADuso_real",
                    "width": 1280,
                    "height": 720,
                    "file_size": 84213,
                }
            ],
        },
    },
}

SECRETS = [
    "Salom, ertaga soat uchda uchrashamizmi?",
    "Dilnoza",
    "Karimova",
    "dilnoza_k",
    "AgACAgIAAxkBAAIBY2_realFileId",
    "AQADuso_real",
    "KxQ2k9_realLookingConnectionId",
]


@pytest.fixture
def cleaned() -> dict[str, Any]:
    result: dict[str, Any] = sanitize.Sanitizer().walk(SAMPLE)
    return result


def test_no_original_string_survives_anywhere(cleaned: dict[str, Any]) -> None:
    rendered = json.dumps(cleaned, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in rendered, f"{secret!r} leaked into the fixture"


def test_structural_enums_are_kept(cleaned: dict[str, Any]) -> None:
    message = cleaned["payload"]["business_message"]
    assert message["chat"]["type"] == "private"
    assert message["entities"][0]["type"] == "bold"
    assert message["from"]["language_code"] == "uz"
    assert cleaned["update_type"] == "business_message"


def test_structure_and_non_identifying_numbers_survive(cleaned: dict[str, Any]) -> None:
    message = cleaned["payload"]["business_message"]
    assert message["date"] == 1792300000  # timestamps matter to window tests
    assert message["from"]["is_bot"] is False
    assert message["photo"][0]["width"] == 1280
    assert message["photo"][0]["file_size"] == 84213
    assert message["entities"][0]["offset"] == 0


def test_entity_offsets_stay_valid_because_utf16_length_is_preserved() -> None:
    original = "Salom 👋 ertaga"  # the emoji is two UTF-16 code units
    cleaned_text = sanitize.Sanitizer().fake_text("text", original)
    assert len(cleaned_text.encode("utf-16-le")) == len(original.encode("utf-16-le"))


def test_identifiers_are_remapped_consistently(cleaned: dict[str, Any]) -> None:
    """`from.id` and `chat.id` were the same person; they must stay equal."""
    message = cleaned["payload"]["business_message"]
    assert message["from"]["id"] == message["chat"]["id"]
    assert message["from"]["id"] != 584113992


def test_sanitizing_twice_gives_the_same_output() -> None:
    first = json.dumps(sanitize.Sanitizer().walk(SAMPLE), sort_keys=True)
    second = json.dumps(sanitize.Sanitizer().walk(SAMPLE), sort_keys=True)
    assert first == second


def test_negative_chat_ids_stay_negative() -> None:
    cleaned_group = sanitize.Sanitizer().walk({"chat": {"id": -1001234567890, "type": "group"}})
    assert cleaned_group["chat"]["id"] < 0


def test_unknown_string_keys_are_replaced_not_passed_through() -> None:
    """Default-deny: a key the sanitiser has never seen must not leak."""
    cleaned_new = sanitize.Sanitizer().walk({"some_future_field": "Real Person Name"})
    assert "Real Person Name" not in json.dumps(cleaned_new)
