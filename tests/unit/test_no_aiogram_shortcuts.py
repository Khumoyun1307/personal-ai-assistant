"""No aiogram shortcuts anywhere in `src/` (AGENTS §5, §7, §17).

`Message.answer()`, `.reply()`, `.edit_text()` and friends auto-inject a
`business_connection_id`, which makes them send **as the owner**. In a business
chat that is the worst failure this product has: a message going out under the
owner's name that no policy check ever saw.

import-linter cannot see method calls, so this scans the AST instead. It is the
test AGENTS §5 requires.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "assistant"

#: aiogram shortcut methods. Any call named like this on anything is reported;
#: a false positive is cheap, a miss is not.
BANNED_METHODS: frozenset[str] = frozenset(
    {
        "answer",
        "reply",
        "answer_photo",
        "answer_document",
        "answer_sticker",
        "answer_voice",
        "answer_video",
        "reply_photo",
        "reply_document",
        "edit_text",
        "edit_caption",
        "edit_reply_markup",
        "edit_media",
        "delete_reply_markup",
        "forward",
        "copy_to",
        "pin",
        "unpin",
        "react",
    }
)

#: The gateway is the single egress point, so its own vocabulary is allowed —
#: `answer_callback_query` is a Bot API method, not a message shortcut.
ALLOWED_FILES: frozenset[str] = frozenset(
    {
        "telegram/gateway.py",
    }
)


def python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def banned_calls_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in BANNED_METHODS:
            # `self.answer_callback(...)` on our own gateway is ours, not aiogram's.
            value = func.value
            if isinstance(value, ast.Name) and value.id in {"self", "gateway", "_gateway"}:
                continue
            found.append(f"line {node.lineno}: .{func.attr}()")
    return found


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.name))
def test_no_aiogram_shortcut_calls(path: Path) -> None:
    relative = path.relative_to(SRC).as_posix()
    if relative in ALLOWED_FILES:
        pytest.skip("the gateway is the single egress point")
    offenders = banned_calls_in(path)
    assert offenders == [], (
        f"{relative} calls an aiogram shortcut: {offenders}. "
        "Shortcuts auto-attach business_connection_id and send AS THE OWNER. "
        "Use OutboundGateway instead (AGENTS §17)."
    )


def test_the_scanner_actually_catches_a_shortcut(tmp_path: Path) -> None:
    """A guard that cannot fail is not a guard."""
    sample = tmp_path / "bad.py"
    sample.write_text("async def h(message):\n    await message.answer('hi')\n", "utf-8")
    assert banned_calls_in(sample) == ["line 2: .answer()"]


def test_control_package_exists_and_is_scanned() -> None:
    """If the control package moved, this suite must not silently pass."""
    control = SRC / "telegram" / "control"
    assert control.is_dir()
    assert any(path.name == "handlers.py" for path in control.iterdir())
