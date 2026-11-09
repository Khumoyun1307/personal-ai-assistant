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
release_up() {
  local sha="$1" heads
  release_sha "$sha"
  [ "$(git rev-parse HEAD)" = "$sha" ] || release_error "checkout is not the selected release"
  export ASSISTANT_IMAGE="assistant:${sha:0:12}" BACKUP_IMAGE="assistant-backup:${sha:0:12}"
  release_build "$sha" "$ASSISTANT_IMAGE" app
  release_build "$sha" "$BACKUP_IMAGE" backup
  heads="$(release_heads "$ASSISTANT_IMAGE")"
  # Persist before any possible migration. Failed/interrupted transitions
  # cannot use an older, apparently compatible pair to authorize rollback.
  printf '%s\n' "$sha" > deploy/release-in-progress
  compose stop app
  compose up -d --wait --no-build db
  compose run --rm --no-deps -T --no-build migrate
  # The backup image initializes ownership of the shared backups volume.
  compose up -d --no-deps --no-build --remove-orphans backup
  compose up -d --no-deps --no-build --remove-orphans app
  compose ps
  if [ "${current[0]:-}" != "$sha" ]; then
    previous=("${current[@]}")
  fi
  current=("$sha" "$ASSISTANT_IMAGE" "$BACKUP_IMAGE" "$heads")
  release_write
  rm -- deploy/release-in-progress
  release_status
}
release_rollback() {
  local sha image revision old_current=()
  [ ! -e deploy/release-in-progress ] || release_error "incomplete transition; forward-fix required (runbooks/deploy.md)"
  [ "${#previous[@]}" != 0 ] || release_error "no recorded previous release; forward-fix required"
  [ "${current[3]}" = "${previous[3]}" ] || release_error "migration heads differ; forward-fix required (runbooks/deploy.md; I23)"
  sha="${previous[0]}"
  for image in "${previous[1]}" "${previous[2]}"; do
    revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")" \
      || release_error "previous release image missing; forward-fix required"
    [ "$revision" = "$sha" ] || release_error "previous release image missing or replaced; forward-fix required"
  done
  [ "$(release_heads "${previous[1]}")" = "${previous[3]}" ] || release_error "previous image heads changed; forward-fix required"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    release_error "tracked server files were modified"
  fi
  printf '%s\n' "$sha" > deploy/release-in-progress
  compose stop app
  git checkout --detach "$sha"
  export ASSISTANT_IMAGE="${previous[1]}" BACKUP_IMAGE="${previous[2]}"
  # No dependency startup: in particular, never run migrate during rollback.
  compose up -d --no-deps --no-build --remove-orphans backup
  compose up -d --no-deps --no-build --remove-orphans app
  compose ps
  old_current=("${current[@]}")
  current=("${previous[@]}")
  previous=("${old_current[@]}")
  release_write
  rm -- deploy/release-in-progress
  release_status
}
