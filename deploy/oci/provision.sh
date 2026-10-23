#!/usr/bin/env bash
# Provision the production host on Oracle Cloud (owner decision H1, 2026-10-22).
#
#   provision.sh --email <owner@example.com> [--wait] [--dry-run]
#
# Runs inside the official OCI CLI container (`just oci-provision`), with the
# owner's API key at /oci (var/cloud/oci, read-only) and the repository at
# /work. Idempotent: every resource is looked up by display name first and
# only created when absent, so it is safe to re-run after any failure.
#
# Creates, in the home region (must be eu-frankfurt-1):
#   compartment  alviro
#   VCN          alviro-vcn 10.0.0.0/16, internet gateway alviro-igw,
#                route table alviro-rt (0.0.0.0/0 → igw), security list
#                alviro-sl (inbound TCP 22 only), public subnet alviro-public
#   instance     alviro: VM.Standard.A1.Flex, 1 OCPU / 6 GB, latest Canonical
#                Ubuntu 24.04 aarch64 platform image, 50 GB boot volume,
#                in-transit encryption, the agent's SSH key, cloud-init
#   volume       alviro-data: 50 GB block volume, paravirtualized attachment
#                at /dev/oracleoci/oraclevdb (consistent device path)
#   budget       alviro-monthly-usd1: 1 (rate-card currency) per month on the
#                whole tenancy, alert e-mails to --email on any actual spend
#                and on a forecast above the budget
#
# "Out of host capacity" (common for A1 in busy regions) is retried across
# the availability domains with bounded backoff; --wait keeps retrying for up
# to WAIT_HOURS (default 12) and is meant to run in the background. HTTP 429
# (TooManyRequests, which long --wait runs can trigger) is retried the same
# way, after checking that no instance was launched meanwhile.
# Output: status lines, names, OCIDs and the public IP only (AGENTS §9).
set -euo pipefail

# shellcheck source=deploy/oci/lib.sh
. "$(dirname "$0")/lib.sh"

REGION_EXPECTED="eu-frankfurt-1"
COMPARTMENT_NAME="alviro"
INSTANCE_NAME="alviro"
SHAPE="VM.Standard.A1.Flex"
OCPUS="${OCPUS:-1}"
MEMORY_GB="${MEMORY_GB:-6}"
BOOT_GB="${BOOT_GB:-50}"
DATA_GB="${DATA_GB:-50}"
DATA_DEVICE="/dev/oracleoci/oraclevdb"
PV_TRANSIT="${PV_TRANSIT_ENCRYPTION:-true}"
BUDGET_NAME="alviro-monthly-usd1"
WAIT_HOURS="${WAIT_HOURS:-12}"

email=""
wait_mode=0
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --email) email="${2:-}"; shift 2 ;;
    --wait) wait_mode=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    *) die "unknown argument (usage: provision.sh --email <address> [--wait] [--dry-run])" ;;
  esac
done
[[ "$email" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] \
  || die "--email <address> is required (budget alerts go there)"

: "${OCI_CLI_CONFIG_FILE:=/oci/config}"
export OCI_CLI_CONFIG_FILE OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING=True
[ -r "$OCI_CLI_CONFIG_FILE" ] || die "OCI config not readable at $OCI_CLI_CONFIG_FILE (var/cloud/oci/config)"

# The tenancy OCID is not secret; it is the root compartment.
tenancy="$(awk -F= '/^[[:space:]]*tenancy[[:space:]]*=/{gsub(/[[:space:]]/, "", $2); print $2; exit}' "$OCI_CLI_CONFIG_FILE")"
[[ "$tenancy" == ocid1.tenancy.* ]] || die "no tenancy=ocid1.tenancy... line in the OCI config"

ensure_ssh_key

workdir="$(mktemp -d)"
trap 'rm -rf -- "$workdir"' EXIT

# Render cloud-init with the agent's public key.
pubkey="$(cat "$SSH_KEY.pub")"
[[ "$pubkey" == ssh-ed25519\ * ]] || die "var/cloud/ssh/id_ed25519.pub is not an ed25519 public key"
sed "s|__DEPLOY_SSH_KEY__|${pubkey//|/}|" "$WORK/deploy/oci/cloud-init.yaml" > "$workdir/user-data.yaml"
grep -q '__DEPLOY_SSH_KEY__' "$workdir/user-data.yaml" && die "cloud-init rendering failed"

if [ "$dry_run" -eq 1 ]; then
  log "dry run: config and SSH key present, cloud-init rendered ($(wc -l < "$workdir/user-data.yaml") lines); no OCI call made"
  exit 0
fi

# --- helpers -----------------------------------------------------------------
# q <jmespath> <oci args…>: one value (or empty), raw.
q() {
  local query="$1"
  shift
  oci "$@" --query "$query" --raw-output 2>"$workdir/err" || {
    log "oci $1 $2 $3 failed: $(head -c 300 "$workdir/err" | tr '\n' ' ')"
    return 1
  }
}
state_set() {
  touch "$STATE_FILE"
  grep -v "^$1=" "$STATE_FILE" > "$workdir/state" || true
  printf '%s=%s\n' "$1" "$2" >> "$workdir/state"
  cat "$workdir/state" > "$STATE_FILE"
  own_like_checkout "$STATE_FILE"
}

# --- 1. authentication and home region -------------------------------------
home="$(q 'data[?"is-home-region"]|[0]."region-name"' iam region-subscription list --tenancy-id "$tenancy")" \
  || die "OCI authentication failed: check var/cloud/oci/config and the API key (runbooks/provision.md §A3)"
[ "$home" = "$REGION_EXPECTED" ] || die "home region is '$home', expected $REGION_EXPECTED (decision H1)"
log "authenticated; home region $home"

# --- 2. compartment ------------------------------------------------------------
comp="$(q "data[?name=='$COMPARTMENT_NAME' && \"lifecycle-state\"=='ACTIVE']|[0].id" \
  iam compartment list -c "$tenancy" --all)"
if [ -z "$comp" ] || [ "$comp" = "null" ]; then
  comp="$(q 'data.id' iam compartment create -c "$tenancy" --name "$COMPARTMENT_NAME" \
    --description "Alviro production" --wait-for-state ACTIVE)"
  log "created compartment $COMPARTMENT_NAME $comp"
  # IAM changes propagate asynchronously; give them a moment.
  sleep 30
else
  log "compartment $COMPARTMENT_NAME $comp"
fi
state_set OCI_COMPARTMENT_ID "$comp"

# find <list command…>: OCID of the AVAILABLE/ATTACHED/RUNNING resource named $NAME
by_name() {
  local name="$1"
  shift
  local id
  id="$(q "data[?\"display-name\"=='$name' && !contains(['TERMINATED','TERMINATING','DETACHED','DETACHING'], \"lifecycle-state\")]|[0].id" "$@" --all)"
  [ "$id" = "null" ] && id=""
  printf '%s' "$id"
}

# --- 3. network ----------------------------------------------------------------
vcn="$(by_name alviro-vcn network vcn list -c "$comp")"
if [ -z "$vcn" ]; then
  vcn="$(q 'data.id' network vcn create -c "$comp" --display-name alviro-vcn \
    --cidr-blocks '["10.0.0.0/16"]' --dns-label alviro --wait-for-state AVAILABLE)"
  log "created VCN alviro-vcn $vcn"
else
  log "VCN alviro-vcn $vcn"
fi

igw="$(by_name alviro-igw network internet-gateway list -c "$comp" --vcn-id "$vcn")"
if [ -z "$igw" ]; then
  igw="$(q 'data.id' network internet-gateway create -c "$comp" --vcn-id "$vcn" \
    --display-name alviro-igw --is-enabled true --wait-for-state AVAILABLE)"
  log "created internet gateway alviro-igw $igw"
else
  log "internet gateway alviro-igw $igw"
fi

rt="$(by_name alviro-rt network route-table list -c "$comp" --vcn-id "$vcn")"
route_rules="[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$igw\"}]"
if [ -z "$rt" ]; then
  rt="$(q 'data.id' network route-table create -c "$comp" --vcn-id "$vcn" \
    --display-name alviro-rt --route-rules "$route_rules" --wait-for-state AVAILABLE)"
  log "created route table alviro-rt $rt"
else
  log "route table alviro-rt $rt"
fi

# Inbound: TCP 22 only (decision H1); plus ICMP type 3 code 4 ("fragmentation
# needed"), which is not a service but keeps path-MTU discovery working (it
# is in OCI's default security list for the same reason). Outbound: all.
ingress='[{"protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","isStateless":false,"tcpOptions":{"destinationPortRange":{"min":22,"max":22}}},{"protocol":"1","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","isStateless":false,"icmpOptions":{"type":3,"code":4}}]'
egress='[{"protocol":"all","destination":"0.0.0.0/0","destinationType":"CIDR_BLOCK","isStateless":false}]'
sl="$(by_name alviro-sl network security-list list -c "$comp" --vcn-id "$vcn")"
if [ -z "$sl" ]; then
  sl="$(q 'data.id' network security-list create -c "$comp" --vcn-id "$vcn" \
    --display-name alviro-sl --ingress-security-rules "$ingress" \
    --egress-security-rules "$egress" --wait-for-state AVAILABLE)"
  log "created security list alviro-sl $sl (inbound TCP 22 only)"
else
  # Re-assert the rules, so a console edit cannot silently widen them.
  q 'data.id' network security-list update --security-list-id "$sl" \
    --ingress-security-rules "$ingress" --egress-security-rules "$egress" \
    --force --wait-for-state AVAILABLE > /dev/null
  log "security list alviro-sl $sl (rules re-asserted: inbound TCP 22 only)"
fi

subnet="$(by_name alviro-public network subnet list -c "$comp" --vcn-id "$vcn")"
if [ -z "$subnet" ]; then
  subnet="$(q 'data.id' network subnet create -c "$comp" --vcn-id "$vcn" \
    --display-name alviro-public --cidr-block 10.0.0.0/24 --dns-label pub \
    --route-table-id "$rt" --security-list-ids "[\"$sl\"]" \
    --prohibit-public-ip-on-vnic false --wait-for-state AVAILABLE)"
  log "created subnet alviro-public $subnet"
else
  log "subnet alviro-public $subnet"
fi
state_set OCI_SUBNET_ID "$subnet"

# --- 4. image ------------------------------------------------------------------
image="$(q "sort_by(data[?!contains(\"display-name\", 'Minimal')], &\"time-created\")[-1].id" \
  compute image list -c "$tenancy" --operating-system "Canonical Ubuntu" \
  --operating-system-version "24.04" --shape "$SHAPE" --lifecycle-state AVAILABLE --all)"
[[ "$image" == ocid1.image.* ]] || die "no Canonical Ubuntu 24.04 image for $SHAPE found"
image_name="$(q 'data."display-name"' compute image get --image-id "$image")"
log "image $image_name $image"

# --- 5. instance -----------------------------------------------------------------
mapfile -t ads < <(q 'data[].name' iam availability-domain list -c "$tenancy" | jq -r '.[]')
[ "${#ads[@]}" -gt 0 ] || die "no availability domains listed"

instance="$(by_name "$INSTANCE_NAME" compute instance list -c "$comp")"
launch_once() {
  # One attempt per availability domain. 0 = launched, 2 = out of capacity
  # everywhere, 3 = rate limited (HTTP 429 TooManyRequests; the round stops
  # there), 1 = any other error (not retried).
  local ad out
  for ad in "${ads[@]}"; do
    if out="$(oci compute instance launch -c "$comp" --availability-domain "$ad" \
        --display-name "$INSTANCE_NAME" --hostname-label alviro --shape "$SHAPE" \
        --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMORY_GB}" \
        --image-id "$image" --boot-volume-size-in-gbs "$BOOT_GB" \
        --subnet-id "$subnet" --assign-public-ip true \
        --is-pv-encryption-in-transit-enabled "$PV_TRANSIT" \
        --ssh-authorized-keys-file "$SSH_KEY.pub" \
        --user-data-file "$workdir/user-data.yaml" \
        --wait-for-state RUNNING --max-wait-seconds 1200 \
        --query 'data.id' --raw-output 2>"$workdir/err")"; then
      instance="$out"
      log "launched instance $INSTANCE_NAME $instance in $ad"
      return 0
    fi
    if grep -qi 'out of host capacity\|OutOfCapacity\|out of capacity' "$workdir/err"; then
      log "out of host capacity in $ad"
      continue
    fi
    if grep -qi 'TooManyRequests\|"status": 429' "$workdir/err"; then
      log "rate limited (HTTP 429) in $ad"
      return 3
    fi
    log "launch failed in $ad: $(head -c 300 "$workdir/err" | tr '\n' ' ')"
    return 1
  done
  return 2
}

if [ -z "$instance" ]; then
  deadline=$(($(date +%s) + WAIT_HOURS * 3600))
  attempts=0
  delay=60
  while true; do
    attempts=$((attempts + 1))
    rc=0
    launch_once || rc=$?
    [ "$rc" -eq 0 ] && break
    [ "$rc" -eq 1 ] && die "instance launch failed (not a capacity error)"
    reason="no capacity"
    if [ "$rc" -eq 3 ]; then
      # A 429 can also come from the --wait-for-state polling after the
      # launch was accepted: pick that instance up, never launch a second.
      instance="$(by_name "$INSTANCE_NAME" compute instance list -c "$comp")"
      if [ -n "$instance" ]; then
        log "instance $INSTANCE_NAME $instance was launched before the 429; waiting for RUNNING"
        oci compute instance get --instance-id "$instance" \
          --wait-for-state RUNNING --max-wait-seconds 1200 > /dev/null
        break
      fi
      reason="rate limited (HTTP 429)"
    fi
    if [ "$wait_mode" -eq 0 ] && [ "$attempts" -ge 3 ]; then
      die "launch still failing ($reason) after $attempts rounds; re-run with --wait to keep trying"
    fi
    [ "$(date +%s)" -ge "$deadline" ] && die "launch still failing ($reason) after ${WAIT_HOURS} h"
    log "round $attempts: $reason; next try in ${delay}s"
    sleep "$delay"
    # Backoff 60 s → 120 s → … capped at 15 min.
    delay=$((delay * 2 > 900 ? 900 : delay * 2))
  done
else
  log "instance $INSTANCE_NAME $instance"
fi
state_set OCI_INSTANCE_ID "$instance"
inst_ad="$(q 'data."availability-domain"' compute instance get --instance-id "$instance")"

# --- 6. data volume ------------------------------------------------------------
vol="$(by_name alviro-data bv volume list -c "$comp")"
if [ -z "$vol" ]; then
  vol="$(q 'data.id' bv volume create -c "$comp" --availability-domain "$inst_ad" \
    --display-name alviro-data --size-in-gbs "$DATA_GB" --wait-for-state AVAILABLE)"
  log "created block volume alviro-data $vol (${DATA_GB} GB, encrypted at rest by OCI)"
else
  log "block volume alviro-data $vol"
fi
state_set OCI_VOLUME_ID "$vol"

att="$(q "data[?\"volume-id\"=='$vol' && \"lifecycle-state\"=='ATTACHED']|[0].id" \
  compute volume-attachment list -c "$comp" --instance-id "$instance" --all)"
if [ -z "$att" ] || [ "$att" = "null" ]; then
  att="$(q 'data.id' compute volume-attachment attach-paravirtualized-volume \
    --instance-id "$instance" --volume-id "$vol" --device "$DATA_DEVICE" \
    --display-name alviro-data-attachment \
    --is-pv-encryption-in-transit-enabled "$PV_TRANSIT" --wait-for-state ATTACHED)"
  log "attached alviro-data at $DATA_DEVICE ($att)"
else
  log "alviro-data attached ($att)"
fi

# --- 7. budget -------------------------------------------------------------------
# Budgets live in the root compartment; this one targets the whole tenancy.
budget="$(q "data[?\"display-name\"=='$BUDGET_NAME']|[0].id" \
  budgets budget budget list -c "$tenancy" --target-type ALL --all)"
if [ -z "$budget" ] || [ "$budget" = "null" ]; then
  budget="$(q 'data.id' budgets budget budget create -c "$tenancy" --amount 1 \
    --reset-period MONTHLY --target-type COMPARTMENT --targets "[\"$tenancy\"]" \
    --display-name "$BUDGET_NAME" \
    --description "Always Free guard: any spend means something is not free" \
    --wait-for-state ACTIVE)"
  log "created budget $BUDGET_NAME $budget"
else
  log "budget $BUDGET_NAME $budget"
fi
ensure_alert() {
  local name="$1" type="$2" threshold="$3" threshold_type="$4" existing
  existing="$(q "data[?\"display-name\"=='$name']|[0].id" \
    budgets budget alert-rule list --budget-id "$budget" --all)"
  if [ -n "$existing" ] && [ "$existing" != "null" ]; then
    log "budget alert $name $existing"
    return 0
  fi
  existing="$(q 'data.id' budgets budget alert-rule create --budget-id "$budget" \
    --display-name "$name" --type "$type" --threshold "$threshold" \
    --threshold-type "$threshold_type" --recipients "$email" \
    --message "Alviro OCI tenancy: spend detected (budget $BUDGET_NAME). Check the Cost Analysis page.")"
  log "created budget alert $name $existing (mail to the owner's address)"
}
ensure_alert alviro-any-actual-spend ACTUAL 0.01 ABSOLUTE
ensure_alert alviro-forecast-over-budget FORECAST 100 PERCENTAGE

# --- 8. result -------------------------------------------------------------------
ip="$(q 'data[0]."public-ip"' compute instance list-vnics --instance-id "$instance")"
[[ "$ip" =~ ^[0-9.]+$ ]] || die "instance has no public IP"
state_set OCI_PUBLIC_IP "$ip"
log "done: instance $INSTANCE_NAME public IP $ip (state in var/cloud/oci-state.env)"
printf '%s\n' "$ip"
