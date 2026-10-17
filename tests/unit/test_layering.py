"""Layering rules that import-linter cannot express (AGENTS §5)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "assistant"

#: Throwaway trees that production code must never import.
THROWAWAY_ROOTS = {"tools", "spikes", "evals"}


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_src_never_imports_throwaway_code() -> None:
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in THROWAWAY_ROOTS:
                    offenders.append(f"{path.relative_to(SRC)}: {name}")
    assert offenders == [], f"src/ imports throwaway code: {offenders}"


def test_every_package_has_a_docstring() -> None:
    """Each package states its layer, so the contracts stay legible."""
    missing = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("__init__.py")
        if not ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert missing == []
