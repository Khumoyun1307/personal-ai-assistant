#!/usr/bin/env bash
# Real containers, synthetic process/schema only; no credentials or live APIs.
set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
project="alviro-recovery-$$"
images=()
host_path() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
compose() { docker compose -p "$project" -f "$(host_path "$tmp/compose.yml")" "$@"; }
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  if [ "${#images[@]}" != 0 ]; then docker image rm "${images[@]}" >/dev/null 2>&1 || true; fi
  rm -rf -- "$tmp"
}
trap cleanup EXIT
cd "$root"
# Reuse the repository's locked dependencies; no new pip installs.
docker build --target prod -f deploy/Dockerfile -t assistant:i23-rehearsal-base .
cat > "$tmp/compose.yml" <<'YAML'
services:
  db:
    image: pgvector/pgvector:pg18
    environment:
      POSTGRES_USER: rehearsal
      POSTGRES_DB: rehearsal
      POSTGRES_PASSWORD: placeholder
    healthcheck:
      test: [CMD-SHELL, pg_isready -U rehearsal -d rehearsal]
      interval: 1s
      timeout: 5s
      retries: 30
  app:
    image: ${ASSISTANT_IMAGE}
    command: [python, -m, assistant, run]
  migrate:
    image: ${ASSISTANT_IMAGE}
    command: [python, -m, assistant, migrate]
  db-roles:
    image: ${ASSISTANT_IMAGE}
    command: ["true"]
  backup:
    image: ${BACKUP_IMAGE}
    command: [loop]
    volumes: [recovery-backup:/backups]
volumes:
  recovery-backup:
YAML

# Only source selection/build are simulated. Runtime Compose, drain, live
# schema queries, compatibility, health polling and recovery are the real helper.
# shellcheck source=deploy/release.sh
source "$root/deploy/release.sh"
git() {
  case "$1" in
    rev-parse) echo "$candidate" ;;
    checkout|diff) : ;;
    *) return 1 ;;
  esac
}
release_build() { release_require_image "$2"; }
release_heads() { docker run --rm --network none --entrypoint cat "$1" /app/heads; }

for compatibility in expand breaking; do
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  mkdir -p "$tmp/$compatibility/deploy" "$tmp/build"
  old="$(printf '%s-old' "$tmp/$compatibility" | sha256sum | cut -c1-40)"
  candidate="$(printf '%s-new' "$tmp/$compatibility" | sha256sum | cut -c1-40)"
  cp "$root/deploy/smoke/recovery_app.py" "$tmp/build/main.py"
  cat > "$tmp/build/Dockerfile" <<'DOCKER'
FROM assistant:i23-rehearsal-base
COPY main.py /app/src/assistant/__main__.py
COPY versions/ /app/recovery-versions/
COPY heads /app/heads
USER root
RUN rm -rf /app/migrations/versions && mv /app/recovery-versions /app/migrations/versions
USER assistant
DOCKER
  mkdir -p "$tmp/build/versions"
  rm -f -- "$tmp/build/versions/new.py"
  printf 'revision = "0042"\ndown_revision = None\nCOMPATIBILITY = "expand"\n' > "$tmp/build/versions/old.py"
  printf '0042\n' > "$tmp/build/heads"
  docker build --label "org.opencontainers.image.revision=$old" -t "assistant:${old:0:12}" "$(host_path "$tmp/build")"
  images+=("assistant:${old:0:12}")
  printf 'revision = "0043"\ndown_revision = "0042"\nCOMPATIBILITY = "%s"\n' "$compatibility" > "$tmp/build/versions/new.py"
  printf '0043\n' > "$tmp/build/heads"
  printf '\nUSER root\nRUN touch /app/fail-health\nUSER assistant\n' >> "$tmp/build/Dockerfile"
  docker build --label "org.opencontainers.image.revision=$candidate" -t "assistant:${candidate:0:12}" "$(host_path "$tmp/build")"
  images+=("assistant:${candidate:0:12}")
  # Synthetic backup image provides only a placeholder recovery marker.
  cat > "$tmp/build/Dockerfile.backup" <<'DOCKER'
FROM busybox:1.37
COPY backup.sh /backup.sh
ENTRYPOINT ["sh", "/backup.sh"]
DOCKER
  cat > "$tmp/build/backup.sh" <<'BACKUP'
#!/bin/sh
set -eu
if [ "$1" = once ]; then
  mkdir -p /backups/daily
  printf '%s\n' synthetic-dump > /backups/daily/20261109T000000Z.dump.age
  printf '%s\n' 20261109T000000Z.dump.age > /backups/last_success_file
  printf '%s\n' 2026-11-09T00:00:05Z > /backups/last_success
else
  trap 'exit 0' TERM
  while :; do sleep 1 & wait $!; done
fi
BACKUP
  for sha in "$old" "$candidate"; do
    docker build --label "org.opencontainers.image.revision=$sha" -t "assistant-backup:${sha:0:12}" -f "$tmp/build/Dockerfile.backup" "$(host_path "$tmp/build")"
    images+=("assistant-backup:${sha:0:12}")
  done
  cd "$tmp/$compatibility"
  printf '%s\tassistant:%s\tassistant-backup:%s\t0042\n' "$old" "${old:0:12}" "${old:0:12}" > deploy/release-state
  release_load
  compose up -d --wait db
  compose run --rm --no-deps -T migrate
  compose up -d --no-deps app backup
  release_started="$(date -u +%FT%TZ)"
  release_wait
  # Use an explicit subshell in a background job: unlike an `if (release_up)`
  # condition, this preserves errexit inside every function under test.
  (set -e; RELEASE_HEALTH_TIMEOUT_SECONDS=5; release_up "$candidate") > rehearsal.log 2>&1 &
  job=$!
  status=0
  wait "$job" || status=$?
  [ "$status" != 0 ] || release_error 'rehearsal candidate unexpectedly succeeded'
  cat rehearsal.log
  [ "$(release_database_heads)" = 0043 ] || release_error 'synthetic upgrade was not retained'
  recorded="$(cut -f1 deploy/release-state | head -n1)"
  [ "$recorded" = "$old" ] || release_error 'failed candidate recorded as current'
  if [ "$compatibility" = expand ]; then
    [ ! -e deploy/release-in-progress ] || release_error 'expand recovery stayed incomplete'
    app_container="$(compose ps -q app)"
    [ "$(docker inspect --format '{{.Config.Image}}' "$app_container")" = "assistant:${old:0:12}" ] \
      || release_error 'wrong image recovered'
    compose exec -T app python -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8080").read() == b"synthetic previous image serving"'
  else
    [ -e deploy/release-in-progress ] || release_error 'breaking failure lost marker'
    [ "$(cat deploy/pre-deploy-backup)" = 20261109T000000Z.dump.age ] || release_error 'wrong recovery filename'
    [ -z "$(compose ps --status running -q app backup)" ] || release_error 'unsafe services running'
    grep -q 'Forward-fix:' rehearsal.log || release_error 'forward procedure missing'
  fi
  echo "PASS: $compatibility failure recovery (synthetic only)"
done
