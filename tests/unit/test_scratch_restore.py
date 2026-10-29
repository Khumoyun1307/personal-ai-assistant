"""The scratch launcher cannot launch against an ambiguous URL or leak it."""

import os
from typing import NoReturn

import pytest
from tools.acceptance import scratch_restore


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://synthetic:private-canary@db/original?dbname=original",
        "postgresql+psycopg://synthetic:private-canary@db/original?database=original",
        "sqlite:///private-canary",
        "postgresql+psycopg://synthetic:private-canary@db",
        "private-canary",
    ],
)
def test_unsupported_url_is_rejected_before_any_child_or_mutating_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    calls: list[str] = []

    def forbidden(_executable: str, _args: list[str]) -> NoReturn:
        calls.append(_executable)
        raise AssertionError("must reject before exec")

    monkeypatch.setattr(os, "execv", forbidden)
    assert scratch_restore.main(["post-restore", "2026-10-28T02:00:00Z"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-canary" not in output.err
    assert output.err.startswith("scratch_restore_failed type=")
    assert os.environ["DATABASE_URL"] == url
    assert calls == []


def test_child_exit_status_is_not_caught_or_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://synthetic:synthetic@db/original")

    def child(_executable: str, _args: list[str]) -> NoReturn:
        raise SystemExit(7)

    monkeypatch.setattr(os, "execv", child)
    with pytest.raises(SystemExit) as error:
        scratch_restore.main(["restore-check"])
    assert error.value.code == 7
