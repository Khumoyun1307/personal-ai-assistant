"""The P2 verification wrapper mounts only the reviewed tooling read-only."""

import subprocess
from pathlib import Path

import pytest


def test_p2_verify_uses_a_one_off_read_only_tool_mount() -> None:
    source = (Path(__file__).resolve().parents[2] / "deploy/server.sh").read_text("utf-8")
    block = source.split("  p2-verify)", 1)[1].split(";;", 1)[0]
    assert "require_data_volume" in block
    assert "run --rm --no-deps -T" in block
    assert 'tools/acceptance:/app/tools/acceptance:ro"' in block
    assert 'app python tools/acceptance/p2_verify.py "$@"' in block


def test_trial_check_uses_the_same_read_only_mount_and_passes_arguments_unchanged() -> None:
    source = (Path(__file__).resolve().parents[2] / "deploy/server.sh").read_text("utf-8")
    assert "  trial-check)" in source, "the daily trial check needs its own server action"
    block = source.split("  trial-check)", 1)[1].split(";;", 1)[0]
    assert "require_data_volume" in block
    assert "run --rm --no-deps -T" in block
    assert 'tools/acceptance:/app/tools/acceptance:ro"' in block
    assert 'app python tools/acceptance/p2_verify.py trial-check "$@"' in block


def test_server_usage_lists_the_daily_trial_check() -> None:
    source = (Path(__file__).resolve().parents[2] / "deploy/server.sh").read_text("utf-8")
    usage = next(line for line in source.splitlines() if 'echo "usage: deploy/server.sh ' in line)
    assert "trial-check ARGS" in usage


@pytest.mark.parametrize("failure", ["", "post-restore", "release", "actions", "restore-check"])
def test_restore_test_verifies_only_scratch_and_always_drops_it(
    tmp_path: Path, failure: str
) -> None:
    source = (Path(__file__).resolve().parents[2] / "deploy/server.sh").read_text("utf-8")
    block = source.split("  restore-test)\n", 1)[1].split("\n  restore)", 1)[0]
    log = tmp_path / "calls"
    # Execute the real branch with deterministic fake infrastructure. No Docker,
    # credentials, backup, Telegram API or actual database is involved.
    harness = r"""
set -euo pipefail
require_data_volume() { :; }
compose() {
  printf '%s\n' "$*" >> "$CALLS"
  case "$*" in
    *'ls -1'*) echo /backups/daily/20261028T020000Z.dump.age ;;
    *'stat -c'*) echo 123 ;;
    *'restore.sh'*) cat >/dev/null ;;
    *'post-restore'*) [ "$FAILURE" != post-restore ] ;;
    *'p2_verify.py'*)
      [ "$FAILURE" != release ] || [[ "$*" != *release* ]]
      [ "$FAILURE" != actions ] || [[ "$*" != *actions* ]] ;;
    *'tools.acceptance.restore_verify'*) [ "$FAILURE" != restore-check ] ;;
  esac
}
db_client() { printf '%s\n' "$*" >> "$CALLS"; }
case restore-test in
  restore-test)
"""
    script = tmp_path / "restore-test.sh"
    script.write_text(harness + block + "\nesac\n", encoding="utf-8")
    result = subprocess.run(  # noqa: S603 — reviewed branch and synthetic infrastructure only
        ["/bin/bash", str(script)],
        input="AGE-SECRET-KEY-1-SYNTHETIC\n",
        text=True,
        capture_output=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "CALLS": str(log), "FAILURE": failure},
        check=False,
    )
    assert log.exists(), result.stderr
    calls = log.read_text("utf-8").splitlines()
    assert result.returncode == (1 if failure else 0), result.stderr
    assert calls[-1] == "dropdb --if-exists restore_test"
    one_off = [call for call in calls if call.startswith("run ")]
    assert all('DATABASE_URL="${DATABASE_URL%/*}/restore_test"' in call for call in one_off)
    assert all("--no-deps" in call for call in one_off)
    assert not any("assistant run" in call or call.startswith("up ") for call in calls)
    if not failure:
        for mode in ("release", "actions"):
            call = next(call for call in one_off if f"p2_verify.py {mode}" in call)
            assert "tools/acceptance:/app/tools/acceptance:ro" in call
        assert "2026-10-26T02:00:00Z" in next(
            call for call in one_off if "p2_verify.py actions" in call
        )
        assert any("tools.acceptance.restore_verify" in call for call in one_off)
        assert "restore_test dropped" in result.stdout
    else:
        assert "restore verification passed" not in result.stdout
