#!/usr/bin/env bash
# Prepare the provisioned host for the stack (runbooks/provision.md §B3).
#
#   bootstrap-host.sh <ip> prepare   cloud-init finished, Docker works, data
#                                    volume formatted (first time only) and
#                                    mounted at /mnt/data, server deploy key
#   (host)  gh repo deploy-key add var/cloud/server-deploy-key.pub  (read-only)
#   bootstrap-host.sh <ip> clone     checkout of main in /opt/assistant
#
# `just prod-bootstrap <ip>` runs all three steps. Runs inside the OCI CLI
# container (it has ssh); connects as `deploy` with var/cloud/ssh/id_ed25519.
# Idempotent. Output is status lines only.
set -euo pipefail

# shellcheck source=deploy/oci/lib.sh
. "$(dirname "$0")/lib.sh"

ip="${1:-}"
phase="${2:-}"
require_ip "$ip"
DATA_GB="${DATA_GB:-50}"

prepare_ssh

prepare() {
  log "waiting for SSH as $DEPLOY_USER@$ip"
  wait_for_ssh "$ip"
  log "waiting for cloud-init to finish (first boot installs Docker; a few minutes)"
  remote "$ip" 'sudo cloud-init status --wait > /dev/null 2>&1 || true; test -f /var/lib/alviro-cloud-init-done' \
    || die "cloud-init did not complete; inspect with: sudo cloud-init status --long (on the server)"

  log "host checks"
  remote "$ip" 'bash -s' <<'REMOTE'
set -euo pipefail
printf 'arch: %s, os: %s\n' "$(uname -m)" "$(. /etc/os-release && echo "$PRETTY_NAME")"
printf 'docker: %s, compose: %s\n' "$(docker version --format '{{.Server.Version}}')" "$(docker compose version --short)"
sudo ufw status | head -n 1
sudo sshd -T 2>/dev/null | grep -Ei '^(passwordauthentication|permitrootlogin) ' | tr '\n' ' '; echo
systemctl is-active chrony fail2ban unattended-upgrades docker | tr '\n' ' '; echo
if [ -n "$(swapon --show --noheadings)" ]; then echo "WARNING: swap is enabled (must be off or encrypted)"; else echo "swap: none"; fi
REMOTE

  log "data volume"
  remote "$ip" "sudo DATA_GB=$DATA_GB bash -s" <<'REMOTE'
set -euo pipefail
dev=/dev/oracleoci/oraclevdb   # consistent device path chosen at attach time
[ -e "$dev" ] || { echo "ERROR: $dev missing: is alviro-data attached?" >&2; exit 1; }
real="$(readlink -f "$dev")"
root_src="$(findmnt -no SOURCE /)"
root_disk="$(lsblk -no PKNAME "$root_src" | head -n 1)"
[ "$(basename "$real")" != "$root_disk" ] || { echo "ERROR: $dev resolves to the boot disk" >&2; exit 1; }
size="$(lsblk -bdno SIZE "$real")"
[ "$size" = "$((DATA_GB * 1024 * 1024 * 1024))" ] \
  || { echo "ERROR: $dev is $size bytes, expected ${DATA_GB} GiB" >&2; exit 1; }
if ! blkid -p "$real" > /dev/null 2>&1; then
  # Only a device with no filesystem and no partition table is formatted.
  mkfs.ext4 -q -L alviro-data "$real"
  echo "formatted $real (ext4, label alviro-data)"
fi
[ "$(blkid -s TYPE -o value "$real")" = "ext4" ] || { echo "ERROR: $real holds no ext4 filesystem" >&2; exit 1; }
uuid="$(blkid -s UUID -o value "$real")"
mkdir -p /mnt/data
if ! grep -q "^UUID=$uuid " /etc/fstab; then
  echo "UUID=$uuid /mnt/data ext4 defaults,nofail,noatime 0 2" >> /etc/fstab
  systemctl daemon-reload
fi
mountpoint -q /mnt/data || mount /mnt/data
[ "$(findmnt -no SOURCE /mnt/data)" = "$real" ] || { echo "ERROR: /mnt/data is not on $real" >&2; exit 1; }
install -d -m 0700 -o 999 -g 999 /mnt/data/postgres
echo "data: /mnt/data on $real ($((size / 1024 / 1024 / 1024)) GiB, OCI block volume, encrypted at rest), postgres dir ready"
REMOTE

  log "server deploy key for GitHub (read-only)"
  remote "$ip" 'bash -s' <<'REMOTE'
set -euo pipefail
umask 077
mkdir -p ~/.ssh
[ -f ~/.ssh/github_deploy ] || ssh-keygen -q -t ed25519 -N "" -C "alviro-server-deploy" -f ~/.ssh/github_deploy
if ! grep -q '^Host github.com' ~/.ssh/config 2>/dev/null; then
  printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
fi
# GitHub's host keys from its published metadata, not trust-on-first-use.
curl -fsSL https://api.github.com/meta \
  | python3 -c 'import json,sys; [print("github.com", k) for k in json.load(sys.stdin)["ssh_keys"]]' \
  > ~/.ssh/known_hosts.github
grep -q 'known_hosts.github' ~/.ssh/config \
  || printf '  UserKnownHostsFile ~/.ssh/known_hosts ~/.ssh/known_hosts.github\n' >> ~/.ssh/config
REMOTE
  remote "$ip" 'cat ~/.ssh/github_deploy.pub' > "$CLOUD_DIR/server-deploy-key.pub"
  own_like_checkout "$CLOUD_DIR/server-deploy-key.pub"
  log "public deploy key saved to var/cloud/server-deploy-key.pub; next: gh repo deploy-key add (read-only)"
}

clone() {
  remote "$ip" "PROD_DIR=$PROD_DIR REPO_SLUG=$REPO_SLUG bash -s" <<'REMOTE'
set -euo pipefail
# `ssh -T git@github.com` exits 1 even when the key is accepted; the
# greeting says whether it was.
greeting="$(ssh -o BatchMode=yes -T git@github.com 2>&1 || true)"
case "$greeting" in
  *"successfully authenticated"*) echo "GitHub accepts the server deploy key" ;;
  *)
    echo "ERROR: GitHub does not accept the server deploy key yet (add var/cloud/server-deploy-key.pub as a read-only deploy key)" >&2
    exit 1
    ;;
esac
sudo install -d -o deploy -g deploy "$PROD_DIR"
if [ -d "$PROD_DIR/.git" ]; then
  git -C "$PROD_DIR" fetch --quiet origin main
  git -C "$PROD_DIR" merge --ff-only --quiet origin/main
else
  git clone --quiet --branch main "git@github.com:$REPO_SLUG.git" "$PROD_DIR"
fi
echo "checkout: $PROD_DIR at $(git -C "$PROD_DIR" log -1 --format='%h %cs') on $(git -C "$PROD_DIR" rev-parse --abbrev-ref HEAD)"
docker info --format 'docker ok: {{.ServerVersion}} {{.Architecture}}, data root {{.DockerRootDir}}'
REMOTE
}

case "$phase" in
  prepare) prepare ;;
  clone) clone ;;
  *) die "usage: bootstrap-host.sh <ip> prepare|clone" ;;
esac
