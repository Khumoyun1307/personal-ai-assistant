#!/usr/bin/env bash
# Generate the backup encryption key pair once (runbooks/provision.md §B2).
#
#   age-key.sh        (inside the backup image: `just prod-age-key`)
#
# Writes var/cloud/age-identity.txt (mode 600) if it does not exist and prints
# only the PUBLIC key. The private key never goes to the server: the owner
# moves the file into a password manager after the first restore test
# (runbooks/restore.md §A) and deletes it from the laptop.
set -euo pipefail

CLOUD_DIR="${CLOUD_DIR:-/work/var/cloud}"
identity="$CLOUD_DIR/age-identity.txt"

mkdir -p "$CLOUD_DIR"
if [ ! -f "$identity" ]; then
  (umask 077 && age-keygen -o "$identity" 2> /dev/null)
  chown --reference="$CLOUD_DIR" "$identity" 2> /dev/null || true
  echo "generated var/cloud/age-identity.txt (private; never copy it to the server)" >&2
fi
public="$(age-keygen -y "$identity")"
[[ "$public" == age1* ]] || { echo "ERROR: var/cloud/age-identity.txt holds no age identity" >&2; exit 1; }
printf 'age public key (AGE_RECIPIENT): %s\n' "$public"
