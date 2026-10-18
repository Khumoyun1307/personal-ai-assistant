"""The probe's structural view is what an agent may read during live tests.

AGENTS §9: an agent driving the probe never sees real chat content. These tests
pin that `describe_update` keeps ids, flags, timestamps, rights and content
kinds, and drops text, captions, names, usernames and file ids. `tools/` is
outside `src/`, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

PROBE_PATH = Path(__file__).resolve().parents[2] / "tools" / "probe" / "probe.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_describe", PROBE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_describe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load()

SECRETS = ("Synthetic secret words", "Alisher", "alisher_x", "AgACAgIAAxkB-fake", "+998901234567")


def _record(update_type: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "update",
        "update_type": update_type,
        "observed_at": "2026-10-18T10:00:00+00:00",
        "payload": {"update_id": 501, update_type: body},
    }


def _line(record: dict[str, Any]) -> str:
    return probe.format_description(probe.describe_update(record))


def test_business_message_keeps_structure_and_drops_content() -> None:
    record = _record(
        "business_message",
        {
            "message_id": 77,
            "date": 1792288725,
            "business_connection_id": "conn-1",
            "chat": {
                "id": 111,
                "type": "private",
                "first_name": "Alisher",
                "username": "alisher_x",
            },
            "from": {"id": 222, "is_bot": False, "first_name": "Alisher", "username": "alisher_x"},
            "sender_business_bot": {"id": 999, "is_bot": True, "first_name": "Alisher"},
            "is_from_offline": True,
            "caption": "Synthetic secret words",
            "photo": [{"file_id": "AgACAgIAAxkB-fake", "file_unique_id": "u1"}],
            "contact": {"phone_number": "+998901234567", "first_name": "Alisher"},
        },
    )

    line = _line(record)

    for secret in SECRETS:
        assert secret not in line
    for expected in (
        "message_id=77",
        "chat_id=111",
        "from_id=222",
        "sender_business_bot_id=999",
        "is_from_offline=True",
        "connection_id=conn-1",
        "'photo'",
    ):
        assert expected in line


def test_plain_text_is_hidden_but_a_start_deep_link_is_shown() -> None:
    prose = _record(
        "message", {"message_id": 1, "chat": {"id": 5}, "from": {"id": 5}, "text": SECRETS[0]}
    )
    deep_link = _record(
        "message",
        {"message_id": 2, "chat": {"id": 5}, "from": {"id": 5}, "text": "/start bizChat12345"},
    )

    assert SECRETS[0] not in _line(prose)
    assert "command=/start <param>" in _line(deep_link)
    assert "start_param=bizChat12345" in _line(deep_link)


def test_connection_lists_only_granted_rights_and_flags_absent_rights() -> None:
    granted = _record(
        "business_connection",
        {
            "id": "conn-1",
            "user": {"id": 222, "first_name": "Alisher", "username": "alisher_x"},
            "user_chat_id": 222,
            "date": 1792288000,
            "is_enabled": True,
            "rights": {
                "can_reply": True,
                "can_read_messages": True,
                "can_delete_all_messages": False,
            },
        },
    )
    no_rights = _record("business_connection", {"id": "conn-2", "is_enabled": False})

    line = _line(granted)
    assert "rights=['can_read_messages', 'can_reply']" in line
    assert "Alisher" not in line
    assert "rights=absent" in _line(no_rights)


def test_withheld_record_shows_nothing_but_its_type() -> None:
    withheld = {
        "update_type": "business_message",
        "note": "chat not allow-listed; payload withheld",
    }

    assert _line(withheld) == "[business_message] withheld=True"


def test_sender_is_read_from_aiogram_field_name() -> None:
    # The dump is aiogram's model_dump without aliases, so `from` is `from_user`.
    record = _record(
        "business_message",
        {"message_id": 3, "chat": {"id": 111}, "from_user": {"id": 222, "is_bot": False}},
    )

    assert "from_id=222" in _line(record)
