"""The P2 verification wrapper mounts only the reviewed tooling read-only."""

from pathlib import Path


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
