"""The P2 verification wrapper mounts only the reviewed tooling read-only."""

from pathlib import Path


def test_p2_verify_uses_a_one_off_read_only_tool_mount() -> None:
    source = (Path(__file__).resolve().parents[2] / "deploy/server.sh").read_text("utf-8")
    block = source.split("  p2-verify)", 1)[1].split(";;", 1)[0]
    assert "require_data_volume" in block
    assert "run --rm --no-deps -T" in block
    assert 'tools/acceptance:/app/tools/acceptance:ro"' in block
    assert 'app python tools/acceptance/p2_verify.py "$@"' in block
