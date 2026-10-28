"""The CI test split (`tools/ci/split_tests.py`, owner 2026-10-27).

A file left out of every shard would silently never run, so the split must
cover every collected test file exactly once, the same way on every run.
`tools/` is outside `src/`, so the script is loaded by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SPLIT_PATH = Path(__file__).resolve().parents[2] / "tools" / "ci" / "split_tests.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("split_tests", SPLIT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["split_tests"] = module
    spec.loader.exec_module(module)
    return module


split_tests = _load()


@pytest.mark.parametrize("shards", [1, 2, 3, 4, 5])
def test_every_test_file_runs_in_exactly_one_shard(shards: int) -> None:
    files = split_tests.collected_files()
    buckets = split_tests.split(files, shards)

    assigned = [path for bucket in buckets for path in bucket]
    assert sorted(assigned) == sorted(files)
    assert len(assigned) == len(set(assigned))
    assert Path(__file__).resolve() in assigned


def test_the_split_is_the_same_on_every_run_and_balanced() -> None:
    files = split_tests.collected_files()
    first = split_tests.split(files, 3)

    assert split_tests.split(list(reversed(files)), 3) == first
    loads = [sum(split_tests.weight(path) for path in bucket) for bucket in first]
    heaviest_file = max(split_tests.weight(path) for path in files)
    assert max(loads) - min(loads) <= heaviest_file


def test_the_command_prints_one_shard_and_refuses_a_bad_index(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert split_tests.main(["--shards", "3", "--index", "2"]) == 0
    printed = capsys.readouterr().out.split()
    expected = split_tests.split(split_tests.collected_files(), 3)[1]
    assert printed == [path.relative_to(split_tests.ROOT).as_posix() for path in expected]

    with pytest.raises(SystemExit):
        split_tests.main(["--shards", "3", "--index", "4"])
