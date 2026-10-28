"""No business send path exists outside `actions/executor.py` (S04.4 §3.10).

AGENTS §6 invariants 1 and 2: nothing sends, edits or deletes as the owner
without the executor's committed claim and its policy re-check right before.
import-linter cannot see method calls, so this scans the AST of `src/`: the
gateway's business methods are called only by the executor, and the
sender's plain-text methods only by the gateway. A false positive is cheap,
a miss is not.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "assistant"

BUSINESS_METHODS = frozenset(
    {
        "send_business_text",
        "send_business_text_with_connection",
        "edit_assistant_message",
        "delete_own_business_messages",
    }
)
PLAIN_SENDER_METHODS = frozenset(
    {"send_plain_message", "edit_plain_message_text", "delete_business_messages"}
)


def calls_by_file(methods: frozenset[str]) -> dict[str, list[str]]:
    """Every call `<anything>.<method>(...)` in `src/`, by relative path."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in methods
            ):
                relative = path.relative_to(SRC).as_posix()
                found.setdefault(relative, []).append(f"line {node.lineno}: .{node.func.attr}()")
    return found


def test_only_the_executor_calls_business_sends() -> None:
    found = calls_by_file(BUSINESS_METHODS)
    outside = {path: calls for path, calls in found.items() if path != "actions/executor.py"}
    assert outside == {}, f"business sends outside the executor: {outside}"
    # Not vacuous: the executor is where the one send happens.
    assert any(
        ".send_business_text_with_connection()" in call
        for call in found.get("actions/executor.py", [])
    )


def test_only_the_gateway_calls_the_plain_sender_methods() -> None:
    found = calls_by_file(PLAIN_SENDER_METHODS)
    outside = {path: calls for path, calls in found.items() if path != "telegram/gateway.py"}
    assert outside == {}, f"plain business calls outside the gateway: {outside}"
    assert set(found) == {"telegram/gateway.py"}
