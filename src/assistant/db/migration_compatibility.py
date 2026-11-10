"""Classify an upgrade graph without importing or executing migration modules.

Used inside an immutable release image with networking disabled. Missing or
malformed metadata is an operator error, never permission to restart old code.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# Applied migrations are immutable. Only these legacy revisions use this
# registry; new migrations declare COMPATIBILITY in their own module.
LEGACY_COMPATIBILITY: dict[str, str] = {
    "0001": "expand",
    "0002": "expand",
    "0003": "breaking",
    "0004": "expand",
    "0005": "breaking",
    "0006": "expand",
    "0007": "expand",
    "0008": "expand",
    "0009": "breaking",
    "0010": "expand",
    "0011": "expand",
    "0012": "expand",
    "0013": "expand",
    "0014": "expand",
    "0015": "breaking",
    "0016": "expand",
}


@dataclass(frozen=True)
class Revision:
    parents: tuple[str, ...]
    compatibility: str


def load_graph(directory: Path) -> dict[str, Revision]:
    graph: dict[str, Revision] = {}
    for path in sorted(directory.glob("*.py")):
        values: dict[str, object] = {}
        for node in ast.parse(path.read_text("utf-8")).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                    "COMPATIBILITY",
                    "depends_on",
                }:
                    if target.id in values:
                        raise ValueError("duplicate migration metadata")
                    values[target.id] = ast.literal_eval(node.value)
        name = values.get("revision")
        parent = values.get("down_revision")
        marker = values.get("COMPATIBILITY")
        if not isinstance(name, str) or not name or name in graph:
            raise ValueError("invalid or duplicate revision")
        if "COMPATIBILITY" in values:
            if name in LEGACY_COMPATIBILITY:
                raise ValueError("migration classified both in-file and in legacy registry")
        else:
            marker = LEGACY_COMPATIBILITY.get(name)
        if "down_revision" not in values or values.get("depends_on") is not None:
            raise ValueError("missing parent or unsupported migration dependency")
        if marker not in ("expand", "breaking"):
            raise ValueError("every migration must have expand or breaking compatibility")
        if parent is None:
            parents: tuple[str, ...] = ()
        elif isinstance(parent, str):
            parents = (parent,)
        elif isinstance(parent, tuple) and all(isinstance(item, str) for item in parent):
            parents = parent
        else:
            raise ValueError("invalid migration parents")
        assert isinstance(marker, str)
        graph[name] = Revision(parents, marker)
    if not graph:
        raise ValueError("missing migration graph")
    return graph


def classify(graph: dict[str, Revision], before: str, after: str) -> str:
    def ancestors(heads: str) -> set[str]:
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError("cyclic migration graph")
            if name in visited:
                return
            if name not in graph:
                raise ValueError("unknown migration revision")
            visiting.add(name)
            for parent in graph[name].parents:
                visit(parent)
            visiting.remove(name)
            visited.add(name)

        if heads != "base":
            for head in heads.split(","):
                visit(head)
        return visited

    old = ancestors(before)
    new = ancestors(after)
    if not old <= new:
        raise ValueError("migration path is not a forward upgrade")
    if any(graph[item].compatibility == "breaking" for item in new - old):
        return "breaking"
    return "expand"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: migration_compatibility BEFORE_HEADS AFTER_HEADS", file=sys.stderr)
        return 2
    try:
        graph = load_graph(Path(__file__).resolve().parents[3] / "migrations/versions")
        result = classify(graph, sys.argv[1], sys.argv[2])
    except ValueError, SyntaxError, OSError:
        print("ERROR: migration compatibility unknown; forward-fix required", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
