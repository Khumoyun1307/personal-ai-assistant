#!/usr/bin/env bash
# Shared helpers for deploy/oci/*.sh (sourced, not executed).
#
# The scripts run inside the official OCI CLI image (ghcr.io/oracle/oci-cli,
# pinned in the justfile), which already ships bash, ssh, ssh-keygen, jq,
# curl and python3. The justfile mounts the repository at /work, var/cloud at
# /cloud (CLOUD_DIR) and, for provision.sh only, var/cloud/oci at /oci
# (read-only); see the `oci-*` and `prod-*` recipes.
#
# Output rules (AGENTS §9): status lines, resource names, OCIDs and the
# public IP only. No secret ever reaches stdout, stderr or `set -x`.

# shellcheck disable=SC2034  # variables are used by the sourcing scripts
WORK="${WORK:-/work}"
CLOUD_DIR="${CLOUD_DIR:-$WORK/var/cloud}"
SSH_DIR="$CLOUD_DIR/ssh"
SSH_KEY="$SSH_DIR/id_ed25519"
KNOWN_HOSTS="$SSH_DIR/known_hosts"
STATE_FILE="$CLOUD_DIR/oci-state.env"
DEPLOY_USER="deploy"
PROD_DIR="/opt/assistant"
REPO_SLUG="Khumoyun1307/personal-ai-assistant"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Files created from inside the container belong to the owner of var/cloud,
# not to root (matters on Linux hosts; a no-op on Docker Desktop).
own_like_checkout() {
  chown --reference="$CLOUD_DIR" "$@" 2>/dev/null || true
}

ensure_ssh_key() {
  mkdir -p "$SSH_DIR"
  if [ ! -f "$SSH_KEY" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "alviro-agent" -f "$SSH_KEY"
    own_like_checkout "$SSH_DIR" "$SSH_KEY" "$SSH_KEY.pub"
    log "generated agent SSH key var/cloud/ssh/id_ed25519 (public part: id_ed25519.pub)"
  fi
  [ -f "$SSH_KEY.pub" ] || die "var/cloud/ssh/id_ed25519.pub is missing"
}

# ssh refuses a private key that a Windows bind mount shows as world-readable,
# so each run uses a 0600 copy inside the container's private /tmp. Call
# prepare_ssh once, in the main shell (not in a $(...) subshell).
SSH_KEY_COPY=""
prepare_ssh() {
  [ -f "$SSH_KEY" ] || die "var/cloud/ssh/id_ed25519 is missing (run just oci-provision first)"
  SSH_KEY_COPY="$(mktemp)"
  install -m 0600 "$SSH_KEY" "$SSH_KEY_COPY"
  trap 'rm -f -- "$SSH_KEY_COPY"' EXIT
  touch "$KNOWN_HOSTS"
  own_like_checkout "$KNOWN_HOSTS"
}

# remote <ip> <command…> — run a command on the server as `deploy`.
# First contact records the host key (trust on first use) in
# var/cloud/ssh/known_hosts; later runs refuse a changed key.
remote() {
  local ip="$1"
  shift
  [ -n "$SSH_KEY_COPY" ] || die "prepare_ssh was not called"
  ssh -i "$SSH_KEY_COPY" \
    -o IdentitiesOnly=yes \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    -o StrictHostKeyChecking=accept-new \
    -o ConnectTimeout=15 \
    -o ServerAliveInterval=30 \
    -o BatchMode=yes \
    -o LogLevel=ERROR \
    "$DEPLOY_USER@$ip" "$@"
}

# Wait until SSH answers as deploy (cloud-init creates the user).
wait_for_ssh() {
  local ip="$1" tries="${2:-60}" i
  for ((i = 1; i <= tries; i++)); do
    if remote "$ip" true 2>/dev/null; then
      return 0
    fi
    sleep 10
  done
  die "no SSH as $DEPLOY_USER@$ip after $((tries * 10)) s"
}

require_ip() {
  [[ "${1:-}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "usage: $(basename "$0") <server public IPv4>"
}
