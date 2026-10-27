"""Split the test files into balanced shards, one per CI job (owner, 2026-10-27).

Run by `.github/workflows/ci.yml`: each test job has its own PostgreSQL and
runs `pytest $(python tools/ci/split_tests.py --shards 3 --index N)`. Never
imported by `src/` (AGENTS §5).

A file's weight is its number of test functions; a test under a directory
that uses the database counts as `DB_TEST_WEIGHT` unit tests, since each one
resets and fills the database. Files go heaviest first to the lightest shard,
so the split is deterministic and every file lands in exactly one shard.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
#: Directories whose tests use the database (`TEST_DATABASE_URL`).
DB_DIRS = frozenset({"integration", "scenarios", "contract"})
DB_TEST_WEIGHT = 10
TEST_FUNCTION = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.MULTILINE)


def collected_files(tests: Path = TESTS) -> list[Path]:
    """Every file pytest collects under `tests/` (its default `test_*.py`)."""
    return sorted(tests.rglob("test_*.py"))


def weight(path: Path, tests: Path = TESTS) -> int:
    count = len(TEST_FUNCTION.findall(path.read_text(encoding="utf-8")))
    per_test = DB_TEST_WEIGHT if path.relative_to(tests).parts[0] in DB_DIRS else 1
    return max(count, 1) * per_test


def split(files: list[Path], shards: int, tests: Path = TESTS) -> list[list[Path]]:
    """Heaviest file first, each to the currently lightest shard."""
    buckets: list[list[Path]] = [[] for _ in range(shards)]
    loads = [0] * shards
    weighted = sorted(((weight(f, tests), f) for f in files), key=lambda wf: (-wf[0], wf[1]))
    for file_weight, file in weighted:
        lightest = loads.index(min(loads))
        buckets[lightest].append(file)
        loads[lightest] += file_weight
    return [sorted(bucket) for bucket in buckets]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--index", type=int, required=True, help="1-based")
    args = parser.parse_args(argv)
    if args.shards < 1 or not 1 <= args.index <= args.shards:
        parser.error("need --shards >= 1 and 1 <= --index <= --shards")
    shard = split(collected_files(), args.shards)[args.index - 1]
    sys.stdout.write("".join(f"{path.relative_to(ROOT).as_posix()}\n" for path in shard))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
