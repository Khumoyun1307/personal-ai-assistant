#!/usr/bin/env bash
# Run deploy/server.sh on the production host as `deploy`.
#
#   ssh.sh <ip> <action> [args…]     e.g. ssh.sh 203.0.113.10 status
#
# Used by `just deploy` and `just prod-*`; runs in the OCI CLI container
# (it has ssh) with the agent key from var/cloud/ssh.
set -euo pipefail

# shellcheck source=deploy/oci/lib.sh
. "$(dirname "$0")/lib.sh"

ip="${1:-}"
require_ip "$ip"
shift
[ $# -gt 0 ] || die "usage: ssh.sh <ip> <deploy/server.sh action> [args…]"

prepare_ssh
command="cd $PROD_DIR && bash deploy/server.sh $(printf '%q ' "$@")"
if [ -n "${SSH_STDIN_FILE:-}" ]; then
  # e.g. the age identity for restore-test: streamed, never copied to disk.
  [ -r "$SSH_STDIN_FILE" ] || die "$(basename "$SSH_STDIN_FILE") is not readable"
  remote "$ip" "$command" < "$SSH_STDIN_FILE"
else
  remote "$ip" "$command" < /dev/null
fi
