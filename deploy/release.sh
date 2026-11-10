#!/usr/bin/env bash
# Release protocol: 1
# Sourced by server.sh, from the checkout root. State is data, never shell code.
# Caller holds deploy/.release-lock across checkout, migration and restart.

release_error() { echo "ERROR: $*" >&2; exit 1; }
release_sha() { [[ "$1" =~ ^[0-9a-f]{40}$ ]] || release_error "a full commit SHA is required"; }
release_row() {
  [ "$#" = 4 ] || release_error "invalid release state"
  release_sha "$1"
  [ "$2" = "assistant:${1:0:12}" ] || release_error "invalid app image in release state"
  [ "$3" = "assistant-backup:${1:0:12}" ] || release_error "invalid backup image in release state"
  [[ "$4" =~ ^[a-zA-Z0-9_]+(,[a-zA-Z0-9_]+)*$ ]] || release_error "invalid migration heads"
}
release_load() {
  current=() previous=()
  local rows=()
  if [ -f deploy/release-state ]; then
    mapfile -t rows < deploy/release-state
    [[ "${#rows[@]}" = 1 || "${#rows[@]}" = 2 ]] || release_error "invalid release state"
    IFS=$'\t' read -r -a current <<< "${rows[0]}"
    release_row "${current[@]}"
    if [ "${#rows[@]}" = 2 ]; then
      IFS=$'\t' read -r -a previous <<< "${rows[1]}"
      release_row "${previous[@]}"
    fi
  fi
  # Before the first recorded release only, keep the legacy tags available to
  # status/backup/restore operations. Deploy always supplies release tags.
  export ASSISTANT_IMAGE="${current[1]:-assistant:prod}"
  export BACKUP_IMAGE="${current[2]:-assistant-backup:prod}"
}
release_write() {
  # Same filesystem + rename: readers never observe half of a release pair.
  local tmp
  tmp="$(mktemp deploy/.release-state.XXXXXX)"
  printf '%s\t%s\t%s\t%s\n' "${current[@]}" > "$tmp"
  if [ "${#previous[@]}" != 0 ]; then
    printf '%s\t%s\t%s\t%s\n' "${previous[@]}" >> "$tmp"
  fi
  mv -- "$tmp" deploy/release-state
}
release_status() {
  printf 'deployed: %s app=%s backup=%s heads=%s\n' \
    "${current[0]:-unrecorded}" "${current[1]:-assistant:prod}" \
    "${current[2]:-assistant-backup:prod}" "${current[3]:-unknown}"
  printf 'previous: %s app=%s backup=%s heads=%s\n' \
    "${previous[0]:-none}" "${previous[1]:-none}" "${previous[2]:-none}" "${previous[3]:-unknown}"
  if [ -e deploy/release-in-progress ]; then
    echo 'WARNING: incomplete release transition; rollback blocked; forward-fix required'
  fi
}
release_select() {
  local sha="${1:-}"
  release_sha "$sha"
  git fetch --quiet origin +refs/heads/main:refs/remotes/origin/main
  [ "$(git rev-parse --verify "$sha^{commit}")" = "$sha" ] || release_error "commit not found"
  git merge-base --is-ancestor "$sha" origin/main || release_error "SHA is not on origin/main"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    release_error "tracked server files were modified"
  fi
  git checkout --detach "$sha"
}
release_build() {
  local sha="$1" image="$2" kind="$3" revision
  if revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image" 2>/dev/null)"; then
    [ "$revision" = "$sha" ] || release_error "release tag collision: $image"
    return
  fi
  # A git archive excludes untracked files, local settings and secrets. Tags
  # already present are never rebuilt or reassigned. No image pruning here.
  if [ "$kind" = app ]; then
    git archive "$sha" | docker build --label "org.opencontainers.image.revision=$sha" \
      --target prod -t "$image" -f deploy/Dockerfile -
  else
    git archive "$sha:deploy/backup" | docker build --label "org.opencontainers.image.revision=$sha" \
      -t "$image" -
  fi
}
release_heads() {
  local output heads
  # `heads` reads the image's migration graph, never env.py or the database.
  output="$(docker run --rm --network none --entrypoint alembic "$1" heads)" \
    || release_error "cannot read image migration heads"
  heads="$(printf '%s\n' "$output" | awk '{print $1}' | LC_ALL=C sort -u | paste -sd, -)"
  [[ "$heads" =~ ^[a-zA-Z0-9_]+(,[a-zA-Z0-9_]+)*$ ]] || release_error "cannot determine migration heads"
  printf '%s\n' "$heads"
}
release_require_image() {
  # Compose run has no --no-build flag. Inspect first so a missing selected
  # image cannot trigger an implicit build (notably for the migrate service).
  docker image inspect "$1" > /dev/null 2>&1 || release_error "required image missing: $1"
}
release_options() {
  local option value
  for option in RELEASE_HEALTH_TIMEOUT_SECONDS RELEASE_POLL_SECONDS RELEASE_DRAIN_SECONDS; do
    if [ -v "$option" ]; then
      value="${!option}"
      [[ "$value" =~ ^[1-9][0-9]{0,4}$ ]] || release_error "invalid positive integer: $option"
    fi
  done
}
release_sql() {
  # Fixed structural queries only. No content-bearing columns or credentials.
  # shellcheck disable=SC2016 # expanded by the container shell
  compose exec -T db timeout 10 sh -c \
    'exec psql -XAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' sh "$1"
}
release_database_heads() {
  local heads
  if ! heads="$(release_sql "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM app.alembic_version" 2>/dev/null)"; then
    [ "$(release_sql "SELECT to_regclass('app.alembic_version') IS NULL")" = t ] || return 1
    heads=base
  fi
  [[ "$heads" =~ ^[a-zA-Z0-9_]+(,[a-zA-Z0-9_]+)*$ ]] || return 1
  printf '%s\n' "$heads"
}
release_compatibility() {
  docker run --rm --network none --entrypoint python "$1" \
    -m assistant.db.migration_compatibility "$2" "$3"
}
release_wait() {
  local deadline=$((SECONDS + ${RELEASE_HEALTH_TIMEOUT_SECONDS:-180})) ready
  while [ "$SECONDS" -lt "$deadline" ]; do
    ready="$(release_sql "SELECT count(*) >= 3 FROM procrastinate.procrastinate_workers WHERE last_heartbeat >= '$release_started'::timestamptz AND last_heartbeat > now() - interval '30 seconds'" 2>/dev/null)" || ready=f
    if [ "$ready" = t ] && compose exec -T app timeout 25 python -m assistant healthcheck >/dev/null 2>&1; then
      echo 'app healthcheck passed; three workers ready'
      return 0
    fi
    sleep "${RELEASE_POLL_SECONDS:-2}"
  done
  echo 'ERROR: app health/worker readiness timed out' >&2
  return 1
}
release_drain() {
  local container stopped running=false
  container="$(compose ps -a -q app)"
  if [ -n "$container" ]; then
    running="$(docker inspect --format '{{.State.Running}}' "$container")"
  fi
  compose stop -t "${RELEASE_DRAIN_SECONDS:-180}" app
  if [ -n "$container" ]; then
    stopped="$(docker inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$container")"
    if [ "$running" = true ]; then
      [ "$stopped" = 'false 0' ] || release_error "app did not drain cleanly; migration refused"
    else
      # Forward fixes must work after a previous failed/crashed process. There
      # is no in-flight work to drain in an already stopped container.
      [[ "$stopped" = false\ * ]] || release_error "app is still running; migration refused"
    fi
  fi
}
release_forward_procedure() {
  echo 'ERROR: services stopped; pre-deploy backup retained; no automatic downgrade' >&2
  echo 'Forward-fix: deploy a reviewed exact-SHA CI-passed repair compatible with the current schema.' >&2
  echo 'Or restore the pre-deploy backup via runbooks/restore.md B, reconcile privacy while quarantined, then deploy compatible code.' >&2
  echo 'See deploy/pre-deploy-backup and runbooks/deploy.md; do not delete the incomplete marker.' >&2
}
release_failed() {
  local status="$1" actual
  [ "$status" != 0 ] || return 0
  current=("${release_up_old_current[@]}")
  previous=("${release_up_old_previous[@]}")
  if [ "$release_up_incomplete" = true ]; then
    compose stop app backup || true
    release_forward_procedure
    return "$status"
  fi
  if [ "$release_up_migration_started" = false ]; then
    echo 'ERROR: deploy failed before migration; restarting existing app container' >&2
    export ASSISTANT_IMAGE="${current[1]:-assistant:prod}" BACKUP_IMAGE="${current[2]:-assistant-backup:prod}"
    if compose start app; then
      compose start backup || echo 'ERROR: could not restart backup sidecar' >&2
      rm -f -- deploy/release-in-progress
    else
      echo 'ERROR: could not restart existing app; operator recovery required' >&2
    fi
    return "$status"
  fi
  # Stop the candidate before any recovery; never leave two pollers alive.
  compose stop app backup || return "$status"
  if [ "$release_up_compatibility" = expand ] && [ "${#current[@]}" != 0 ]; then
    actual="$(release_database_heads)" || { release_forward_procedure; return "$status"; }
    # Covers rolled-back/partially applied expand upgrades; an unknown graph
    # or reverse edge is never treated as compatible.
    if [ "$(release_compatibility "$release_up_image" "${current[3]}" "$actual")" = expand ]; then
      export ASSISTANT_IMAGE="${current[1]}" BACKUP_IMAGE="${current[2]}"
      release_require_image "$ASSISTANT_IMAGE"
      release_require_image "$BACKUP_IMAGE"
      git checkout --detach "${current[0]}"
      release_started="$(date -u +%FT%T.%NZ)"
      if compose up -d --no-deps --no-build --remove-orphans app && release_wait; then
        # Keep both services on the recorded release. Preserve the marker and
        # stop the recovered app as well if the backup sidecar cannot restart.
        if ! compose up -d --no-deps --no-build --remove-orphans backup; then
          compose stop app backup || true
          release_forward_procedure
          return "$status"
        fi
        current[3]="$actual"
        release_write
        rm -f -- deploy/release-in-progress
        echo 'ERROR: release failed; previous image recovered on compatible schema' >&2
        return "$status"
      fi
    fi
  fi
  compose stop app backup || true
  release_forward_procedure
  return "$status"
}
release_up() {
  # Keep release arrays and image exports in the caller's shell. Save its EXIT
  # trap so a successful transition does not leave our recovery handler behind.
  local sha="$1" heads exit_trap database_heads
  release_options
  # Bash may unwind function locals before running EXIT on an errexit failure.
  # Keep every recovery flag in shell scope, initialized before the trap exists.
  release_up_migration_started=false
  release_up_incomplete=false
  release_up_compatibility=breaking
  release_up_old_current=("${current[@]}")
  release_up_old_previous=("${previous[@]}")
  release_sha "$sha"
  [ ! -e .env.pending ] || release_error "incomplete settings push; repeat prod-env-push before deploying"
  [ "$(git rev-parse HEAD)" = "$sha" ] || release_error "checkout is not the selected release"
  export ASSISTANT_IMAGE="assistant:${sha:0:12}" BACKUP_IMAGE="assistant-backup:${sha:0:12}"
  release_build "$sha" "$ASSISTANT_IMAGE" app
  release_build "$sha" "$BACKUP_IMAGE" backup
  release_require_image "$ASSISTANT_IMAGE"
  release_require_image "$BACKUP_IMAGE"
  heads="$(release_heads "$ASSISTANT_IMAGE")"
  release_up_image="$ASSISTANT_IMAGE"
  # Authenticate the restricted roles before stopping the old poller. Setup
  # is an explicit operator step; a deploy never silently changes passwords.
  compose up -d --wait --no-build db
  compose run --rm --no-deps -T db-roles \
    || release_error "database roles missing/outdated; follow runbooks/deploy.md I24 role upgrade"
  database_heads="$(release_database_heads)" || release_error "cannot read database heads"
  release_up_compatibility="$(release_compatibility "$ASSISTANT_IMAGE" "$database_heads" "$heads")" \
    || release_error "unknown migration path; forward-fix required"
  [[ "$release_up_compatibility" = expand || "$release_up_compatibility" = breaking ]] \
    || release_error "invalid migration compatibility"
  [ ! -e deploy/release-in-progress ] || release_up_incomplete=true
  if [ "${#current[@]}" != 0 ] && [ "${current[3]}" != "$database_heads" ]; then
    release_up_incomplete=true
  fi
  # Before migration, the existing container is still schema-compatible. Start
  # that container rather than recreating from the candidate checkout/images.
  # An older incomplete transition may have changed the DB: never auto-recover it.
  exit_trap="$(trap -p EXIT)"
  trap 'release_failed "$?"' EXIT
  release_drain
  # Freeze the old sidecar so it cannot rotate the recovery dump while stopped.
  compose stop backup
  if [ "$release_up_incomplete" = false ]; then
    compose run --rm --no-deps -T backup once
    compose run --rm --no-deps -T --entrypoint sh backup -c 'cat /backups/last_success' > deploy/pre-deploy-backup
  fi
  # Persist before any possible migration. Failed/interrupted transitions
  # cannot use an older, apparently compatible pair to authorize rollback.
  printf '%s\n' "$sha" > deploy/release-in-progress
  release_up_migration_started=true
  compose run --rm --no-deps -T migrate
  [ "$(release_database_heads)" = "$heads" ] || release_error "migration did not reach selected heads"
  # The pre-deploy backup initialized the shared volume. Keep the loop stopped
  # until readiness passes, so even a long migration cannot rotate that dump.
  release_started="$(date -u +%FT%T.%NZ)"
  compose up -d --no-deps --no-build --remove-orphans app
  release_wait || release_error "new release failed readiness; recovering"
  compose up -d --no-deps --no-build --remove-orphans backup
  compose ps
  if [ "${current[0]:-}" != "$sha" ]; then
    previous=("${current[@]}")
  fi
  current=("$sha" "$ASSISTANT_IMAGE" "$BACKUP_IMAGE" "$heads")
  release_write
  rm -- deploy/release-in-progress
  release_status
  trap - EXIT
  if [ -n "$exit_trap" ]; then
    # This is shell-generated trap output, never release-state data.
    eval "$exit_trap"
  fi
}
release_rollback() {
  local sha image revision actual target_heads compatibility exit_trap old_current=()
  release_options
  [ ! -e .env.pending ] || release_error "incomplete settings push; repeat prod-env-push before rollback"
  [ ! -e deploy/release-in-progress ] || release_error "incomplete transition; forward-fix required (runbooks/deploy.md)"
  [ "${#previous[@]}" != 0 ] || release_error "no recorded previous release; forward-fix required"
  sha="${previous[0]}"
  for image in "${previous[1]}" "${previous[2]}"; do
    revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")" \
      || release_error "previous release image missing; forward-fix required"
    [ "$revision" = "$sha" ] || release_error "previous release image missing or replaced; forward-fix required"
  done
  actual="$(release_database_heads)" || release_error "cannot read database heads; rollback refused"
  [ "$actual" = "${current[3]}" ] || release_error "database heads differ from release state; forward-fix required"
  target_heads="$(release_heads "${previous[1]}")"
  # After a compatible rollback, current's image can have an older graph than
  # the database. Either retained image may supply the complete graph.
  compatibility="$(release_compatibility "${current[1]}" "$target_heads" "$actual" 2>/dev/null)" \
    || compatibility="$(release_compatibility "${previous[1]}" "$target_heads" "$actual")" \
    || release_error "unknown migration path; rollback refused; forward-fix required"
  [ "$compatibility" = expand ] || release_error "breaking migrations between target image and database; rollback refused; forward-fix required"
  git show "$sha:deploy/compose.prod.yml" | grep -F 'env_file: ../.env.app' > /dev/null \
    || release_error "previous release predates service environment separation; forward-fix required"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    release_error "tracked server files were modified"
  fi
  exit_trap="$(trap -p EXIT)"
  trap 'if [ "$?" != 0 ]; then compose stop app backup || true; release_forward_procedure; fi' EXIT
  printf '%s\n' "$sha" > deploy/release-in-progress
  release_drain
  git checkout --detach "$sha"
  export ASSISTANT_IMAGE="${previous[1]}" BACKUP_IMAGE="${previous[2]}"
  # No dependency startup: in particular, never run migrate during rollback.
  compose up -d --no-deps --no-build --remove-orphans backup
  release_started="$(date -u +%FT%T.%NZ)"
  compose up -d --no-deps --no-build --remove-orphans app
  release_wait || release_error "rollback failed readiness; services stopped for operator recovery"
  compose ps
  old_current=("${current[@]}")
  current=("${previous[@]}")
  # Fourth field tracks actual schema, which is deliberately not downgraded.
  current[3]="$actual"
  previous=("${old_current[@]}")
  release_write
  rm -- deploy/release-in-progress
  release_status
  trap - EXIT
  if [ -n "$exit_trap" ]; then eval "$exit_trap"; fi
}
