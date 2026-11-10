"""Release selection and rollback use synthetic infrastructure only."""

import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OLD = "a" * 40
NEW = "b" * 40
OLDER = "c" * 40


def run_release(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    helper = ROOT / "deploy/release.sh"
    harness = r"""
set -euo pipefail
cd "$WORK"
mkdir -p deploy
source "$HELPER"
RELEASE_HEALTH_TIMEOUT_SECONDS=1
RELEASE_POLL_SECONDS=1
sleep() { SECONDS=$((SECONDS + 1)); }
git() {
  printf 'git %s\n' "$*" >> calls
  case "$1" in
    rev-parse) echo "$SELECTED" ;;
    merge-base) [ "${FOREIGN:-0}" = 0 ] ;;
    archive) echo synthetic-build-context ;;
    show) [ "${LEGACY_ENV:-0}" = 0 ] && echo 'env_file: ../.env.app' ;;
  esac
}
docker() {
  printf 'docker %s\n' "$*" >> calls
  case "$*" in
    'image inspect --format'*)
      [ "${EXISTING:-0}" = 1 ] || return 1
      case "$*" in
        *assistant-backup:aaaaaaaaaaaa|*assistant:aaaaaaaaaaaa) printf 'a%.0s' {1..40}; echo ;;
        *) printf 'b%.0s' {1..40}; echo ;;
      esac ;;
    build*) cat >/dev/null; [ "${FAIL_BUILD:-0}" = 0 ] ;;
    'image inspect '*) [ "${FAIL_INSPECT:-0}" = 0 ] ;;
    *migration_compatibility*)
      [ "${UNKNOWN_COMPATIBILITY:-0}" = 0 ] || return 1
      echo "${COMPATIBILITY:-expand}" ;;
    'inspect --format {{.State.Running}} {{.State.ExitCode}} '*)
      echo "${STOPPED_STATE:-false 0}" ;;
    'inspect --format {{.State.Running}} '*) echo "${INITIAL_RUNNING:-true}" ;;
    run*) echo "${HEADS:-0042} (head)"; [ "${FAIL_HEADS:-0}" = 0 ] ;;
  esac
}
compose() {
  printf 'compose %s image=%s backup=%s\n' "$*" "$ASSISTANT_IMAGE" "$BACKUP_IMAGE" >> calls
  case "$*" in
    'up -d --wait --no-build db') [ "${FAIL_DB:-0}" = 0 ] ;;
    'run --rm --no-deps -T db-roles') [ "${FAIL_ROLES:-0}" = 0 ] ;;
    'stop -t 180 app')
      [ "${FAIL_MARKER:-0}" = 0 ] || ln -s /dev/full deploy/release-in-progress
      [ "${FAIL_STOP:-0}" = 0 ] ;;
    'start app') [ "${FAIL_RECOVERY:-0}" = 0 ] ;;
    'ps -a -q app') echo synthetic-app ;;
    *'SELECT string_agg(version_num'*) echo "${DB_HEADS:-0042}" ;;
    *'SELECT count(*) >= 3'*)
      if [[ "$ASSISTANT_IMAGE" = assistant:aaaaaaaaaaaa ]]; then echo t
      else echo "${WORKERS_READY:-t}"; fi ;;
    *'app timeout 25 python'*)
      [ "${FAIL_HEALTH:-0}" = 0 ] || [[ "$ASSISTANT_IMAGE" = assistant:aaaaaaaaaaaa ]] ;;
    *'cat /backups/last_success'*) echo 2026-11-09T00:00:00Z ;;
    'run --rm --no-deps -T backup once') [ "${FAIL_BACKUP:-0}" = 0 ] ;;
    'run --rm --no-deps -T migrate')
      [ "${FAIL_MIGRATE:-0}" = 0 ] || return 1
      DB_HEADS="${HEADS:-0042}" ;;
    *'--remove-orphans app')
      [ "${FAIL_STARTUP:-0}" = 0 ] || [[ "$ASSISTANT_IMAGE" = assistant:aaaaaaaaaaaa ]] ;;
  esac
}
"""
    path = tmp_path / "harness.sh"
    path.write_text(harness + script, encoding="utf-8")
    return subprocess.run(  # noqa: S603 — fixed helper with fakes, no Docker or network
        ["/bin/bash", str(path)],
        text=True,
        capture_output=True,
        check=False,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "WORK": str(tmp_path),
            "HELPER": str(helper),
            "SELECTED": NEW,
        },
    )


def state(sha: str, heads: str = "0042") -> str:
    return f"{sha}\tassistant:{sha[:12]}\tassistant-backup:{sha[:12]}\t{heads}\n"


def seed(tmp_path: Path, current: str = NEW, previous: str = OLD, heads: str = "0042") -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "release-state").write_text(state(current, heads) + state(previous), encoding="utf-8")


def test_compose_run_never_uses_unsupported_no_build_flag() -> None:
    paths = sorted((ROOT / "deploy").glob("*.sh"))
    paths.extend(sorted((ROOT / "deploy/runbooks").glob("*.md")))
    paths.extend(sorted((ROOT / "deploy/smoke").glob("*.sh")))
    for path in paths:
        source = path.read_text("utf-8").replace("\\\n", " ")
        for line in source.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if "--no-build" not in line:
                continue
            tokens = shlex.split(line)
            if "run" in tokens:
                assert "--no-build" not in tokens, f"{path.relative_to(ROOT)}: {line}"


@pytest.mark.parametrize("failure", ["FAIL_BUILD=1", "FAIL_INSPECT=1", "FAIL_HEADS=1", "FAIL_DB=1"])
def test_preparation_failure_keeps_the_old_app_running(tmp_path: Path, failure: str) -> None:
    seed(tmp_path, current=OLD)
    result = run_release(
        tmp_path, f"release_load\nCOMPATIBILITY=breaking\n{failure}\nrelease_up {NEW}\n"
    )
    assert result.returncode != 0
    calls = (tmp_path / "calls").read_text("utf-8")
    assert "stop -t 180 app" not in calls
    assert " migrate" not in calls
    assert not (tmp_path / "deploy/release-in-progress").exists()
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == state(OLD) + state(OLD)


@pytest.mark.parametrize("failure", ["FAIL_STOP=1", "FAIL_MARKER=1"])
def test_failure_before_migration_restarts_existing_app(tmp_path: Path, failure: str) -> None:
    seed(tmp_path, current=OLD, previous=OLDER)
    result = run_release(tmp_path, f"release_load\n{failure}\nrelease_up {NEW}\n")
    assert result.returncode != 0
    assert "unbound variable" not in result.stderr
    calls = (tmp_path / "calls").read_text("utf-8")
    assert (
        f"compose start app image=assistant:{OLD[:12]} backup=assistant-backup:{OLD[:12]}" in calls
    )
    assert " migrate" not in calls
    assert "--remove-orphans app" not in calls
    assert not (tmp_path / "deploy/release-in-progress").exists()
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == state(OLD) + state(OLDER)


def test_preexisting_incomplete_transition_never_auto_restarts_old_app(tmp_path: Path) -> None:
    seed(tmp_path)
    marker = tmp_path / "deploy/release-in-progress"
    marker.write_text(OLD + "\n", encoding="utf-8")
    result = run_release(tmp_path, f"release_load\nFAIL_STOP=1\nrelease_up {NEW}\n")
    assert result.returncode != 0
    calls = (tmp_path / "calls").read_text("utf-8")
    assert "compose start app" not in calls
    assert marker.read_text("utf-8") == OLD + "\n"


def test_recovery_failure_is_reported_and_deploy_still_fails(tmp_path: Path) -> None:
    seed(tmp_path)
    result = run_release(
        tmp_path, f"release_load\nFAIL_STOP=1\nFAIL_RECOVERY=1\nrelease_up {NEW}\n"
    )
    assert result.returncode != 0
    assert "unbound variable" not in result.stderr
    assert "could not restart existing app" in result.stderr
    assert " migrate" not in (tmp_path / "calls").read_text("utf-8")


def test_successful_deploy_clears_an_existing_incomplete_marker(tmp_path: Path) -> None:
    seed(tmp_path)
    (tmp_path / "deploy/release-in-progress").write_text(OLD + "\n", encoding="utf-8")
    result = run_release(tmp_path, f"release_load\nrelease_up {NEW}\n")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "deploy/release-in-progress").exists()


def test_deploy_pins_checkout_and_keeps_both_release_images(tmp_path: Path) -> None:
    seed(tmp_path, current=OLD, previous=OLDER)
    # Persist again from the caller: a subshell can write the right file while
    # leaving stale arrays and image exports behind in the parent shell.
    result = run_release(
        tmp_path,
        f"release_load\nrelease_select {NEW}\nrelease_up {NEW}\nrelease_write\ncompose ps\n",
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text("utf-8")
    assert f"checkout --detach {NEW}" in calls
    assert "pull" not in calls
    assert f"assistant:{NEW[:12]}" in calls
    assert f"assistant-backup:{NEW[:12]}" in calls
    assert "--no-build" in calls
    assert "prune" not in calls
    assert "--network none" in calls
    assert "stop -t 180 app" in calls
    assert calls.index(f"docker image inspect assistant:{NEW[:12]}") < calls.index(
        "stop -t 180 app"
    )
    assert calls.index(f"docker image inspect assistant-backup:{NEW[:12]}") < calls.index(
        "stop -t 180 app"
    )
    assert calls.index("--network none") < calls.index("stop -t 180 app")
    assert calls.index("run --rm --no-deps -T db-roles") < calls.index("stop -t 180 app")
    assert calls.index("stop -t 180 app") < calls.index("run --rm --no-deps -T migrate")
    assert calls.count("compose up -d --wait --no-build db") == 1
    assert calls.splitlines()[-1] == (
        f"compose ps image=assistant:{NEW[:12]} backup=assistant-backup:{NEW[:12]}"
    )
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == state(NEW) + state(OLD)
    assert not (tmp_path / "deploy/release-in-progress").exists()


@pytest.mark.parametrize("next_command", ["true", "false"])
def test_successful_deploy_restores_the_callers_exit_trap(
    tmp_path: Path, next_command: str
) -> None:
    seed(tmp_path, current=OLD, previous=OLDER)
    result = run_release(
        tmp_path,
        f"release_load\ntrap 'echo caller-exit' EXIT\nrelease_up {NEW}\n{next_command}\n",
    )
    assert result.returncode == (0 if next_command == "true" else 1), result.stderr
    assert result.stdout.endswith("caller-exit\n")
    assert "compose start app" not in (tmp_path / "calls").read_text("utf-8")
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == state(NEW) + state(OLD)


@pytest.mark.parametrize("sha", ["", "main", "-bad", "f" * 39])
def test_server_rejects_non_full_sha_before_fetch(tmp_path: Path, sha: str) -> None:
    result = run_release(tmp_path, f"release_select '{sha}'\n")
    assert result.returncode != 0
    assert not (tmp_path / "calls").exists()


def test_server_refuses_sha_outside_main_without_checkout(tmp_path: Path) -> None:
    result = run_release(tmp_path, f"FOREIGN=1\nrelease_select {NEW}\n")
    assert result.returncode != 0
    calls = (tmp_path / "calls").read_text("utf-8")
    assert "fetch" in calls
    assert "checkout" not in calls
    assert "docker" not in calls


@pytest.mark.parametrize("failure", ["FAIL_MIGRATE=1", "FAIL_STARTUP=1"])
def test_failed_transition_keeps_old_state_and_blocks_rollback(
    tmp_path: Path, failure: str
) -> None:
    seed(tmp_path)
    before = (tmp_path / "deploy/release-state").read_text("utf-8")
    result = run_release(
        tmp_path, f"release_load\nCOMPATIBILITY=breaking\n{failure}\nrelease_up {NEW}\n"
    )
    assert result.returncode != 0
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == before
    assert (tmp_path / "deploy/release-in-progress").exists()
    assert "compose start app" not in (tmp_path / "calls").read_text("utf-8")
    blocked = run_release(tmp_path, "release_load\nrelease_rollback\n")
    assert blocked.returncode != 0
    assert "forward-fix" in blocked.stderr


def test_rollback_uses_previous_images_without_build_or_migration(tmp_path: Path) -> None:
    seed(tmp_path)
    result = run_release(tmp_path, "release_load\nEXISTING=1\nrelease_rollback\n")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text("utf-8")
    assert f"checkout --detach {OLD}" in calls
    assert f"image=assistant:{OLD[:12]}" in calls
    assert f"backup=assistant-backup:{OLD[:12]}" in calls
    assert "--no-deps --no-build" in calls
    assert "docker build" not in calls
    assert " migrate" not in calls
    assert "fetch" not in calls
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == state(OLD) + state(NEW)


@pytest.mark.parametrize("problem", ["missing", "pending", "malformed"])
def test_rollback_fails_closed_before_switching(tmp_path: Path, problem: str) -> None:
    seed(tmp_path)
    if problem == "missing":
        (tmp_path / "deploy/release-state").write_text(state(NEW), encoding="utf-8")
    elif problem == "pending":
        (tmp_path / "deploy/release-in-progress").touch()
    elif problem == "malformed":
        (tmp_path / "deploy/release-state").write_text(
            "$(touch should-not-exist)\n", encoding="utf-8"
        )
    result = run_release(tmp_path, "release_load\nrelease_rollback\n")
    assert result.returncode != 0
    assert not (tmp_path / "calls").exists()
    assert not (tmp_path / "should-not-exist").exists()


def test_redeploy_preserves_previous_release(tmp_path: Path) -> None:
    seed(tmp_path)
    result = run_release(tmp_path, f"release_load\nrelease_up {NEW}\n")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == state(NEW) + state(OLD)


def test_missing_roles_refuse_deploy_before_app_stop(tmp_path: Path) -> None:
    seed(tmp_path)
    result = run_release(tmp_path, f"release_load\nFAIL_ROLES=1\nrelease_up {NEW}\n")
    assert result.returncode != 0
    assert "I24 role upgrade" in result.stderr
    calls = (tmp_path / "calls").read_text("utf-8")
    assert "stop -t 180 app" not in calls
    assert not (tmp_path / "deploy/release-in-progress").exists()


def test_incomplete_settings_push_blocks_deploy_before_external_commands(tmp_path: Path) -> None:
    seed(tmp_path)
    (tmp_path / ".env.pending").touch()
    result = run_release(tmp_path, f"release_load\nrelease_up {NEW}\n")
    assert result.returncode != 0
    assert "incomplete settings push" in result.stderr
    assert not (tmp_path / "calls").exists()


def test_rollback_cannot_restore_shared_superuser_environment(tmp_path: Path) -> None:
    seed(tmp_path)
    result = run_release(tmp_path, "release_load\nEXISTING=1\nLEGACY_ENV=1\nrelease_rollback\n")
    assert result.returncode != 0
    assert "environment separation" in result.stderr
    assert "stop -t 180 app" not in (tmp_path / "calls").read_text("utf-8")


def test_release_status_is_content_free(tmp_path: Path) -> None:
    seed(tmp_path)
    result = run_release(tmp_path, "release_load\nrelease_status\n")
    assert result.returncode == 0, result.stderr
    assert NEW in result.stdout
    assert OLD in result.stdout
    assert f"assistant:{NEW[:12]}" in result.stdout
    assert f"assistant-backup:{OLD[:12]}" in result.stdout


def test_ssh_bootstraps_the_selected_script_under_a_release_lock() -> None:
    source = (ROOT / "deploy/oci/ssh.sh").read_text("utf-8")
    assert "exec 9>deploy/.release-lock && flock -n 9" in source
    assert "git checkout --detach" in source
    assert "git merge-base --is-ancestor" in source
    assert "_deploy" in source


def test_ssh_ignores_the_old_server_script_and_keeps_the_selected_sha(tmp_path: Path) -> None:
    source = (ROOT / "deploy/oci/ssh.sh").read_text("utf-8")
    branch = source.split('if [ "$1" = deploy ]; then\n', 1)[1].split("\nfi\n", 1)[0]
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/server.sh").write_text("exit 88\n", encoding="utf-8")
    (tmp_path / "selected-server").write_text(
        'set -eu\nprintf "selected %s\\n" "$*" >> "$CALLS"\n'
        # A distinct open-file description cannot claim the inherited lock.
        "if flock -n deploy/.release-lock -c true; then exit 89; fi\n",
        encoding="utf-8",
    )
    harness = f"""
set -euo pipefail
cd "$WORK"
PROD_DIR="$WORK"
die() {{ echo "$*" >&2; exit 1; }}
git() {{
  printf 'git %s\\n' "$*" >> "$CALLS"
  case "$1" in
    merge-base) [ "$3" = {NEW} ] ;;
    checkout) [ "$3" = {NEW} ]; cp selected-server deploy/server.sh ;;
  esac
}}
set -- deploy {NEW}
{branch}
eval "$command"
"""
    script = tmp_path / "ssh-harness.sh"
    script.write_text(harness, encoding="utf-8")
    calls_file = tmp_path / "calls"
    result = subprocess.run(  # noqa: S603 — real selection branch, fake git and server
        ["/bin/bash", str(script)],
        text=True,
        capture_output=True,
        check=False,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "WORK": str(tmp_path),
            "CALLS": str(calls_file),
        },
    )
    assert result.returncode == 0, result.stderr
    calls = calls_file.read_text("utf-8")
    assert f"selected _deploy {NEW}" in calls
    assert "pull" not in calls


def test_existing_release_tags_are_reused_without_building(tmp_path: Path) -> None:
    seed(tmp_path)
    result = run_release(tmp_path, f"release_load\nEXISTING=1\nrelease_up {NEW}\n")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text("utf-8")
    assert "docker build" not in calls


def test_release_tag_collision_is_refused(tmp_path: Path) -> None:
    result = run_release(
        tmp_path,
        f"release_load\nEXISTING=1\nrelease_build {OLD} assistant:{NEW[:12]} app\n",
    )
    assert result.returncode != 0
    assert "collision" in result.stderr
    assert "docker build" not in (tmp_path / "calls").read_text("utf-8")


def test_missing_previous_image_cannot_trigger_a_rebuild(tmp_path: Path) -> None:
    seed(tmp_path)
    result = run_release(tmp_path, "release_load\nrelease_rollback\n")
    assert result.returncode != 0
    calls = (tmp_path / "calls").read_text("utf-8")
    assert "build" not in calls
    assert "checkout" not in calls


@pytest.mark.parametrize("setup", ["HEADS='?'", "FAIL_HEADS=1"])
def test_unknown_image_heads_refuse_before_migration(tmp_path: Path, setup: str) -> None:
    seed(tmp_path)
    result = run_release(tmp_path, f"release_load\n{setup}\nrelease_up {NEW}\n")
    assert result.returncode != 0
    assert "migrate" not in (tmp_path / "calls").read_text("utf-8")
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == state(NEW) + state(OLD)


def test_first_release_has_no_invented_previous_artifact(tmp_path: Path) -> None:
    result = run_release(tmp_path, f"release_load\nrelease_up {NEW}\n")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == state(NEW)


@pytest.mark.parametrize(
    "failure", ["FAIL_MIGRATE=1", "FAIL_STARTUP=1", "FAIL_HEALTH=1", "WORKERS_READY=f"]
)
def test_expand_failure_recovers_previous_image_without_recording_candidate(
    tmp_path: Path, failure: str
) -> None:
    seed(tmp_path, current=OLD, previous=OLDER)
    result = run_release(tmp_path, f"release_load\nHEADS=0043\n{failure}\nrelease_up {NEW}\n")
    assert result.returncode != 0
    assert "previous image recovered" in result.stderr
    actual = "0042" if failure == "FAIL_MIGRATE=1" else "0043"
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == (
        state(OLD, actual) + state(OLDER)
    )
    assert not (tmp_path / "deploy/release-in-progress").exists()
    calls = (tmp_path / "calls").read_text("utf-8")
    recovered = f"--remove-orphans app image=assistant:{OLD[:12]}"
    assert recovered in calls
    assert calls.index("backup once") < calls.index("-T migrate")
    assert "run --rm --no-deps -T migrate" in calls
    assert calls.count("run --rm --no-deps -T migrate") == 1


@pytest.mark.parametrize("failure", ["FAIL_MIGRATE=1", "FAIL_HEALTH=1", "WORKERS_READY=f"])
def test_breaking_failure_stops_safely_and_preserves_backup(tmp_path: Path, failure: str) -> None:
    seed(tmp_path, current=OLD, previous=OLDER)
    before = (tmp_path / "deploy/release-state").read_text("utf-8")
    result = run_release(
        tmp_path, f"release_load\nCOMPATIBILITY=breaking\n{failure}\nrelease_up {NEW}\n"
    )
    assert result.returncode != 0
    assert "no automatic downgrade" in result.stderr
    assert "Forward-fix" in result.stderr
    assert "restore" in result.stderr
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == before
    assert (tmp_path / "deploy/release-in-progress").exists()
    assert (tmp_path / "deploy/pre-deploy-backup").read_text("utf-8") == "2026-11-09T00:00:00Z\n"
    calls = (tmp_path / "calls").read_text("utf-8")
    assert "compose stop app backup" in calls
    assert f"--remove-orphans app image=assistant:{OLD[:12]}" not in calls
    assert "downgrade" not in calls


def test_rollback_across_expand_heads_preserves_actual_database_head(tmp_path: Path) -> None:
    seed(tmp_path, heads="0043")
    result = run_release(tmp_path, "release_load\nEXISTING=1\nDB_HEADS=0043\nrelease_rollback\n")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == (
        state(OLD, "0043") + state(NEW, "0043")
    )
    assert " migrate" not in (tmp_path / "calls").read_text("utf-8")


@pytest.mark.parametrize(
    "setup", ["COMPATIBILITY=breaking", "UNKNOWN_COMPATIBILITY=1", "DB_HEADS=0044"]
)
def test_rollback_refuses_breaking_unknown_or_drifted_schema(tmp_path: Path, setup: str) -> None:
    seed(tmp_path)
    result = run_release(tmp_path, f"release_load\nEXISTING=1\n{setup}\nrelease_rollback\n")
    assert result.returncode != 0
    assert "forward-fix" in result.stderr
    calls = (tmp_path / "calls").read_text("utf-8")
    assert "stop -t 180 app" not in calls
    assert "checkout" not in calls


def test_backup_failure_prevents_migration(tmp_path: Path) -> None:
    seed(tmp_path, current=OLD)
    result = run_release(tmp_path, f"release_load\nFAIL_BACKUP=1\nrelease_up {NEW}\n")
    assert result.returncode != 0
    assert " migrate" not in (tmp_path / "calls").read_text("utf-8")


def test_rollback_health_failure_does_not_swap_release_records(tmp_path: Path) -> None:
    seed(tmp_path, current=OLD, previous=NEW)
    before = (tmp_path / "deploy/release-state").read_text("utf-8")
    result = run_release(tmp_path, "release_load\nEXISTING=1\nFAIL_HEALTH=1\nrelease_rollback\n")
    assert result.returncode != 0
    assert (tmp_path / "deploy/release-state").read_text("utf-8") == before
    assert (tmp_path / "deploy/release-in-progress").exists()


def test_forced_drain_refuses_migration(tmp_path: Path) -> None:
    seed(tmp_path, current=OLD)
    result = run_release(tmp_path, f"release_load\nSTOPPED_STATE='false 137'\nrelease_up {NEW}\n")
    assert result.returncode != 0
    assert "did not drain cleanly" in result.stderr
    assert " migrate" not in (tmp_path / "calls").read_text("utf-8")


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_invalid_health_timeout_refuses_before_external_commands(
    tmp_path: Path, value: str
) -> None:
    seed(tmp_path)
    result = run_release(
        tmp_path, f"release_load\nRELEASE_HEALTH_TIMEOUT_SECONDS={value}\nrelease_up {NEW}\n"
    )
    assert result.returncode != 0
    assert "invalid positive integer" in result.stderr
    assert not (tmp_path / "calls").exists()


def test_forward_fix_can_start_from_an_already_failed_container(tmp_path: Path) -> None:
    seed(tmp_path)
    (tmp_path / "deploy/release-in-progress").touch()
    result = run_release(
        tmp_path,
        f"release_load\nINITIAL_RUNNING=false\nSTOPPED_STATE='false 1'\nrelease_up {NEW}\n",
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "deploy/release-in-progress").exists()
