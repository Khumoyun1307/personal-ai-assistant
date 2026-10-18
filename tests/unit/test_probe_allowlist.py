"""The probe's chat allow-list decides what real content reaches disk.

It must fail closed (AGENTS §6.7): a full payload is written only when we are
certain we may write it. `tools/` is outside `src/`, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROBE_PATH = Path(__file__).resolve().parents[2] / "tools" / "probe" / "probe.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_main", PROBE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_main"] = module
    spec.loader.exec_module(module)
    return module


probe = _load()

OBSERVED = {7001, 7002}


@pytest.mark.parametrize(
    ("chats", "listed", "expected", "why"),
    [
        ({7001}, OBSERVED, True, "an allow-listed chat"),
        ({7001, 9999}, OBSERVED, True, "one party is allow-listed"),
        ({9999}, OBSERVED, False, "a chat we were not told to observe"),
        ({7001}, set(), False, "NO allow-list configured: must withhold, not dump"),
        (set(), OBSERVED, False, "no identifiable chat id: withhold"),
        (set(), set(), False, "nothing known at all: withhold"),
    ],
)
def test_full_capture_only_when_certain(
    chats: set[int], listed: set[int], expected: bool, why: str
) -> None:
    assert probe.should_capture_full(chats, listed) is expected, why


def test_unlisted_record_carries_no_content() -> None:
    payload = {
        "update_id": 42,
        "business_message": {
            "text": "Salom, bu yerda haqiqiy matn",
            "from": {"id": 9999, "first_name": "Dilnoza"},
            "chat": {"id": 9999, "type": "private", "username": "dilnoza_k"},
        },
    }
    record = probe.redact_for_unlisted(payload, "business_message")
    rendered = json.dumps(record, ensure_ascii=False)
    for secret in ("Salom", "Dilnoza", "dilnoza_k", "9999"):
        assert secret not in rendered
    assert record["update_id"] == 42
    assert record["update_type"] == "business_message"
    assert record["chat_id_hashes"] == [probe.hash_id(9999)]


def test_chat_ids_are_found_wherever_they_appear() -> None:
    payload = {
        "business_connection": {
            "id": "abc",
            "user_chat_id": 5150,
            "user": {"id": 5150},
        }
    }
    assert probe.extract_chat_ids(payload) == {5150}


def test_allowed_updates_is_explicit_and_covers_business() -> None:
    """The server remembers the last list; an omission silently loses updates."""
    for required in (
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
    ):
        assert required in probe.ALLOWED_UPDATES
