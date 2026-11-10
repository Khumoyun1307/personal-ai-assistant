"""Backup recovery markers use synthetic dumps and a deterministic shell clock."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DUMP_FILE = "20261109T000000Z.dump.age"
COMPLETED_AT = "2026-11-09T00:00:05Z"


def run_backup(tmp_path: Path, *, fail_dump: bool = False) -> subprocess.CompletedProcess[str]:
    harness = r"""
set -euo pipefail
date() {
  local instant=2026-11-09T00:00:00Z
  if [ -e "$WORK/dump-finished" ]; then instant=2026-11-09T00:00:05Z; fi
  command date -u -d "$instant" "${@:2}"
}
pg_dump() {
  printf '%s\n' synthetic-dump
  touch "$WORK/dump-finished"
  [ "$FAIL_DUMP" = 0 ]
}
age() { cat; }
mv() {
  printf '%s\n' "$*" >> "$WORK/renames"
  command mv "$@"
}
export -f date pg_dump age mv
bash "$BACKUP_SCRIPT" once
"""
    script = tmp_path / "harness.sh"
    script.write_text(harness, encoding="utf-8")
    return subprocess.run(  # noqa: S603 — real script with fake dump/encryption/clock only
        ["/bin/bash", str(script)],
        text=True,
        capture_output=True,
        check=False,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "WORK": str(tmp_path),
            "BACKUP_DIR": str(tmp_path),
            "BACKUP_SCRIPT": str(ROOT / "deploy/backup/backup.sh"),
            "AGE_RECIPIENT": "synthetic-recipient",
            "MIN_DUMP_BYTES": "1",
            "FAIL_DUMP": str(int(fail_dump)),
        },
    )


def test_completed_backup_records_start_filename_separately_from_completion_time(
    tmp_path: Path,
) -> None:
    result = run_backup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "last_success").read_text("utf-8") == f"{COMPLETED_AT}\n"
    assert (tmp_path / "last_success_file").read_text("utf-8") == f"{DUMP_FILE}\n"
    assert (tmp_path / "daily" / DUMP_FILE).read_text("utf-8") == "synthetic-dump\n"
    assert not (tmp_path / "daily/20261109T000005Z.dump.age").exists()
    assert not (tmp_path / "last_success_file.part").exists()
    renames = (tmp_path / "renames").read_text("utf-8").splitlines()
    assert renames[-2:] == [
        f"{tmp_path}/last_success_file.part {tmp_path}/last_success_file",
        f"{tmp_path}/last_success.part {tmp_path}/last_success",
    ]


def test_failed_dump_preserves_both_success_markers(tmp_path: Path) -> None:
    (tmp_path / "last_success").write_text("2026-11-08T00:00:05Z\n", encoding="utf-8")
    (tmp_path / "last_success_file").write_text("20261108T000000Z.dump.age\n", encoding="utf-8")

    result = run_backup(tmp_path, fail_dump=True)

    assert result.returncode != 0
    assert (tmp_path / "last_success").read_text("utf-8") == "2026-11-08T00:00:05Z\n"
    assert (tmp_path / "last_success_file").read_text("utf-8") == "20261108T000000Z.dump.age\n"
    assert not list((tmp_path / "daily").iterdir())
