"""Freeze check for the evaluation datasets.

`datasets/MANIFEST.sha256` lists the SHA-256 of every frozen dataset file in
`sha256sum` format, so either of these verifies it:

    uv run python -m evals.manifest            # exit 1 on any mismatch
    cd evals/datasets && sha256sum -c MANIFEST.sha256

Changing a frozen file is a version bump (v1 -> v1.1) recorded in
docs/evals/; only then regenerate with `uv run python -m evals.manifest --write`.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

DATASETS = Path(__file__).resolve().parent / "datasets"
MANIFEST = DATASETS / "MANIFEST.sha256"
#: Every file under datasets/ with one of these suffixes is frozen.
FROZEN_SUFFIXES = (".jsonl",)


def frozen_files() -> list[Path]:
    return sorted(p for p in DATASETS.rglob("*") if p.is_file() and p.suffix in FROZEN_SUFFIXES)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render() -> str:
    lines = [f"{_digest(p)}  {p.relative_to(DATASETS).as_posix()}" for p in frozen_files()]
    return "\n".join(lines) + "\n"


def problems() -> list[str]:
    """Mismatches between the manifest and the files on disk (empty when frozen)."""
    if not MANIFEST.exists():
        return [f"missing {MANIFEST.name}"]
    expected: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            expected[name.strip()] = digest
    actual = {p.relative_to(DATASETS).as_posix(): _digest(p) for p in frozen_files()}
    found = [f"changed: {n}" for n in sorted(expected) if n in actual and actual[n] != expected[n]]
    found += [f"missing: {n}" for n in sorted(set(expected) - set(actual))]
    found += [f"not in manifest: {n}" for n in sorted(set(actual) - set(expected))]
    return found


def main(argv: list[str]) -> int:
    if argv == ["--write"]:
        MANIFEST.write_text(render(), encoding="utf-8", newline="\n")
        return 0
    found = problems()
    for problem in found:
        print(problem)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
