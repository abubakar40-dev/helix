#!/usr/bin/env bash
# Launch the HELIX A1 Always Free VM, retrying past "Out of host capacity".
#
# Why this exists: A1 capacity is scarce and the error is transient. Oracle's own
# advice (Resolving Out of Host Capacity) is "wait a while and try again" and
# "try another availability domain". So: loop, rotate ADs, stop on anything that
# is not capacity.
#
# Non-capacity errors (auth, bad subnet, quota, tenancy limit) exit immediately.
# Retrying those just hammers the API and hides the real fault.
#
# A duplicate launch is a billing event (two of the Always Free allowance), so
# before each attempt we adopt an existing instance with the same display-name
# rather than creating a second one.

set -uo pipefail

# ---------------------------------------------------------------- config
REGION="${OCI_REGION:-me-abudhabi-1}"
SHAPE="${OCI_SHAPE:-VM.Standard.A1.Flex}"
OCPUS="${OCI_OCPUS:-2}"
MEMORY_GB="${OCI_MEMORY_GB:-12}"
DISPLAY_NAME="${OCI_DISPLAY_NAME:-helix-a1}"
VCN_NAME="${OCI_VCN_NAME:-helix-vcn}"
SUBNET_NAME="${OCI_SUBNET_NAME:-public}"
SSH_KEY="${OCI_SSH_KEY:-$HOME/.ssh/helix_oracle.pub}"
UBUNTU="${OCI_UBUNTU_VERSION:-24.04 LTS}"
LOG="${OCI_LOG:-$(cd "$(dirname "$0")/.." && pwd)/logs/oci-a1-launch.log}"

# Always Free A1 allowance: 1,500 OCPU-h and 9,000 GB-h per month == 2 OCPU + 12 GB 24x7.
FREE_OCPUS=2
FREE_MEMORY_GB=12

BACKOFF_MIN="${OCI_BACKOFF_MIN:-60}"    # first wait after a capacity miss
BACKOFF_MAX="${OCI_BACKOFF_MAX:-300}"   # ceiling, reached after a few misses
MAX_ATTEMPTS="${OCI_MAX_ATTEMPTS:-0}"   # 0 = unlimited
DEADLINE_MIN="${OCI_DEADLINE_MIN:-0}"   # 0 = unlimited
RUNNING_TIMEOUT="${OCI_RUNNING_TIMEOUT:-900}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1 (try --help)" >&2; exit 64 ;;
  esac
done

mkdir -p "$(dirname "$LOG")" 2>/dev/null
STARTED_AT=$SECONDS
ATTEMPT=0
AD_INDEX=0
AD_LIST=()

# ---------------------------------------------------------------- logging
log() { printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG"; }
die() { log "FATAL: $*"; exit 1; }

# --max-retries 1: do not let the SDK retry behind our back, we own the pacing.
OCI_ARGS=(--region "$REGION")
[[ -n "${OCI_PROFILE:-}" ]] && OCI_ARGS+=(--profile "$OCI_PROFILE")
o() { oci "${OCI_ARGS[@]}" --max-retries 1 "$@"; }

# OCI writes errors as JSON on stderr; fall back to raw text when it is not JSON.
err_field() { printf '%s' "$1" | jq -r ".${2} // empty" 2>/dev/null; }

# Retry only what is genuinely transient.
is_capacity_error() {
  local json=$1 msg code status blob
  msg=$(err_field "$json" message); code=$(err_field "$json" code); status=$(err_field "$json" status)
  [[ -z "$msg" && -z "$code" ]] && msg="$json"
  blob=$(printf '%s %s' "$msg" "$code" | tr '[:upper:]' '[:lower:]')
  case "$blob" in
    *"out of host capacity"*|*"out of capacity"*|*"insufficient capacity"*) return 0 ;;
  esac
  [[ "$status" == "429" ]] && return 0
  return 1
}

# ---------------------------------------------------------------- preflight
echo "===== preflight =====" | tee -a "$LOG"

command -v oci >/dev/null || die "oci CLI not installed (see the walkthrough)"
command -v jq  >/dev/null || die "jq not installed (sudo apt install jq)"
log "oci      : $(oci --version 2>&1 | head -1)"
log "region   : $REGION"
log "shape    : $SHAPE  ${OCPUS} OCPU / ${MEMORY_GB} GB"
log "instance : $DISPLAY_NAME"
log "log file : $LOG"

CONFIG="${OCI_CONFIG_FILE:-$HOME/.oci/config}"
[[ -r "$CONFIG" ]] || die "no readable $CONFIG - add an API key in the Console first"
[[ "$(stat -c '%a' "$CONFIG")" == "600" ]] || log "WARN: $CONFIG is mode $(stat -c '%a' "$CONFIG"), OCI wants 600; run: chmod 600 $CONFIG"

[[ -r "$SSH_KEY" ]] || die "no SSH public key at $SSH_KEY - set OCI_SSH_KEY=/path/to/key.pub"
log "ssh key  : $SSH_KEY"

# Compartment: the tenancy root unless told otherwise.
COMPARTMENT="${OCI_COMPARTMENT_ID:-}"
if [[ -z "$COMPARTMENT" ]]; then
  COMPARTMENT=$(awk -F= '/^[[:space:]]*tenancy[[:space:]]*=/ {gsub(/[[:space:]]/,"",$2); print $2; exit}' "$CONFIG")
  [[ -n "$COMPARTMENT" ]] || die "no tenancy in $CONFIG and no OCI_COMPARTMENT_ID set"
fi
log "compartment: $COMPARTMENT"

# Auth check and AD discovery in one call.
AD_JSON=$(o iam availability-domain list --compartment-id "$COMPARTMENT" --query 'data[].name' --raw-output 2>&1)
if [[ $? -ne 0 ]]; then
  printf '%s\n' "$AD_JSON" | tee -a "$LOG" >&2
  die "IAM call failed - wrong OCID/fingerprint/region, or the API key is not active yet"
fi
while IFS= read -r ad; do [[ -n "$ad" ]] && AD_LIST+=("$ad"); done <<<"$AD_JSON"
[[ ${#AD_LIST[@]} -gt 0 ]] || die "no availability domains in $REGION"
log "ADs      : ${AD_LIST[*]}"

# Always Free compute is only free in the tenancy HOME region. Not detectable from
# the CLI, so say it loudly every run.
log "CHECK: Always Free A1 is free ONLY in your tenancy HOME region. If $REGION is not home, this VM launches anyway but is BILLED."

o compute shape list --compartment-id "$COMPARTMENT" --query 'data[?shape==`'"$SHAPE"'`].shape' --raw-output 2>/dev/null | grep -q . \
  || die "$SHAPE is not offered in $REGION"

# VCN.
VCN_ID="${OCI_VCN_ID:-}"
if [[ -z "$VCN_ID" ]]; then
  mapfile -t vcns < <(o network vcn list --compartment-id "$COMPARTMENT" --display-name "$VCN_NAME" --query 'data[].id' --raw-output 2>/dev/null)
  case ${#vcns[@]} in
    1) VCN_ID=${vcns[0]} ;;
    0) die "no VCN named '$VCN_NAME' in $COMPARTMENT. Fix the name, or set OCI_VCN_ID / OCI_COMPARTMENT_ID. VCNs present: $(o network vcn list --compartment-id "$COMPARTMENT" --query 'data[]."display-name"' --raw-output 2>/dev/null | tr '\n' ' ')" ;;
    *) die "'$VCN_NAME' matches ${#vcns[@]} VCNs - set OCI_VCN_ID explicitly" ;;
  esac
fi
log "vcn      : $VCN_ID"

# Subnet.
SUBNET_ID="${OCI_SUBNET_ID:-}"
if [[ -z "$SUBNET_ID" ]]; then
  mapfile -t subs < <(o network subnet list --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" --display-name "$SUBNET_NAME" --query 'data[].id' --raw-output 2>/dev/null)
  case ${#subs[@]} in
    1) SUBNET_ID=${subs[0]} ;;
    0) die "no subnet named '$SUBNET_NAME' in $VCN_ID. Subnets present: $(o network subnet list --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" --query 'data[]."display-name"' --raw-output 2>/dev/null | tr '\n' ' ')" ;;
    *) die "'$SUBNET_NAME' matches ${#subs[@]} subnets - set OCI_SUBNET_ID explicitly" ;;
  esac
fi
log "subnet   : $SUBNET_ID"
[[ "$(o network subnet get --subnet-id "$SUBNET_ID" --query 'data."prohibit-public-ip-on-vnic"' --raw-output 2>/dev/null)" == "True" ]] \
  && log "WARN: this subnet prohibits public IPs, so --assign-public-ip true will fail. Point OCI_SUBNET_ID at a public subnet."

# Always Free headroom: an A1 already running means this launch goes over the cap.
# `instance list` has no --shape filter, so select on the shape field client-side.
# Coerced to a number so a surprise response cannot quietly disable the check below.
sum_a1() {
  local v
  v=$(o compute instance list --compartment-id "$COMPARTMENT" \
      --query 'data[?lifecycleState!="TERMINATED" && shape==`'"$SHAPE"'`].shapeConfig.'"$1" \
      --raw-output 2>/dev/null \
      | jq -s 'map(select(type == "number")) | add // 0 | floor' 2>/dev/null)
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 0
}
used_ocpu=$(sum_a1 ocpus); used_mem=$(sum_a1 memoryInGBs)
if (( used_ocpu + OCPUS > FREE_OCPUS || used_mem + MEMORY_GB > FREE_MEMORY_GB )); then
  log "BILLING RISK: A1 instances already use ${used_ocpu} OCPU / ${used_mem} GB; this adds ${OCPUS}/${MEMORY_GB}, over the Always Free cap of ${FREE_OCPUS}/${FREE_MEMORY_GB}."
else
  log "always free: ${used_ocpu}->$((used_ocpu + OCPUS)) OCPU, ${used_mem}->$((used_mem + MEMORY_GB)) GB (cap ${FREE_OCPUS}/${FREE_MEMORY_GB})"
fi

# Image: latest Canonical Ubuntu for this region, resolved at runtime because OCIDs
# are regional and Canonical rotates the image.
IMAGE_ID="${OCI_IMAGE_ID:-}"
if [[ -z "$IMAGE_ID" ]]; then
  IMAGE_ID=$(o compute image list --compartment-id "$COMPARTMENT" --operating-system "Canonical Ubuntu" \
    --query 'sort_by(data[?"operating-system-version"==`'"$UBUNTU"'`],&time_created)[-1].id' --raw-output 2>/dev/null)
  [[ "$IMAGE_ID" == ocid1.image.* ]] || die "no Canonical Ubuntu '$UBUNTU' image in $REGION (check OCI_UBUNTU_VERSION, or set OCI_IMAGE_ID)"
fi
log "image    : $IMAGE_ID"

# Wait for RUNNING. 0 = running, 1 = dead (capacity died after launch), 2 = timeout.
wait_running() {
  local id=$1 limit=$((SECONDS + RUNNING_TIMEOUT)) state misses=0
  while (( SECONDS < limit )); do
    state=$(o compute instance get --instance-id "$id" --query 'data."lifecycle-state"' --raw-output 2>/dev/null)
    case "$state" in
      RUNNING) return 0 ;;
      TERMINATED|TERMINATING|ERROR) return 1 ;;
      "") (( ++misses > 5 )) && return 1 ;;   # transient, but do not spin forever
      *) misses=0 ;;
    esac
    log "  ... state=$state"
    sleep 10
  done
  return 2
}

# A dead instance leaves its boot volume behind, and boot volumes count against the
# 200 GB Always Free block storage quota. Past that they are billed, and a long retry
# run can reach it, so reclaim it. Best effort: a failure here must not stop the loop.
cleanup_boot_volume() {
  local bvid
  # There is no `oci compute boot-volume` and no --instance-id on `bv volume list`;
  # go via the instance's volume attachments instead. A VM we just launched has
  # exactly one, so the first entry is the boot volume.
  bvid=$(o compute volume-attachment list --compartment-id "$COMPARTMENT" --instance-id "$1" \
         --query 'data[].volumeId' --raw-output 2>/dev/null | grep '^ocid1\.volume\.' | head -1)
  [[ -z "$bvid" ]] && return 0
  if o bv volume delete --volume-id "$bvid" --force >/dev/null 2>&1; then
    log "  reclaimed orphaned boot volume $bvid"
  else
    log "  WARN: could not delete boot volume $bvid - remove it in the Console"
  fi
}

report_ip() {
  local ip
  ip=$(o compute instance list-vnics --instance-id "$1" --query 'data[0]."public-ip"' --raw-output 2>/dev/null)
  echo
  log "======================================================"
  log "LAUNCHED after $ATTEMPT attempt(s), $(((SECONDS - STARTED_AT) / 60)) min"
  log "public IP : ${ip:-<not assigned yet, check the Console>}"
  log "ssh       : ssh -i ${SSH_KEY%.pub} opc@${ip:-<ip>}"
  log "======================================================"
}

if (( DRY_RUN )); then
  log "DRY RUN: preflight passed, nothing launched."
  exit 0
fi

# ---------------------------------------------------------------- retry loop
trap 'log "interrupted after $ATTEMPT attempt(s); rerun to resume"; exit 130' INT TERM
echo "===== launch loop =====" | tee -a "$LOG"
DELAY=$BACKOFF_MIN

while :; do
  if (( DEADLINE_MIN > 0 )) && (( (SECONDS - STARTED_AT) / 60 >= DEADLINE_MIN )); then
    log "deadline of ${DEADLINE_MIN}m reached after $ATTEMPT attempt(s), giving up"; exit 4
  fi
  if (( MAX_ATTEMPTS > 0 && ATTEMPT >= MAX_ATTEMPTS )); then
    log "max attempts ($MAX_ATTEMPTS) reached, giving up"; exit 4
  fi

  ATTEMPT=$((ATTEMPT + 1))
  AD="${AD_LIST[$((AD_INDEX % ${#AD_LIST[@]}))]}"
  AD_INDEX=$((AD_INDEX + 1))
  log "--- attempt $ATTEMPT  AD=$AD  t+$(((SECONDS - STARTED_AT) / 60))m"

  # Adopt an existing instance instead of launching a duplicate. Guard on the OCID
  # shape so stray output is never mistaken for an instance id.
  INST_ID=$(o compute instance list --compartment-id "$COMPARTMENT" --display-name "$DISPLAY_NAME" \
            --query 'data[?lifecycleState!="TERMINATED"].id' --raw-output 2>/dev/null \
            | grep '^ocid1\.instance\.' | head -1)
  if [[ -n "$INST_ID" ]]; then
    log "existing instance $INST_ID found, adopting it"
  else
    OUT=$(o compute instance launch \
      --compartment-id "$COMPARTMENT" \
      --availability-domain "$AD" \
      --shape "$SHAPE" \
      --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMORY_GB}" \
      --image-id "$IMAGE_ID" \
      --subnet-id "$SUBNET_ID" \
      --display-name "$DISPLAY_NAME" \
      --assign-public-ip true \
      --ssh-authorized-keys-file "$SSH_KEY" \
      --query 'data.id' --raw-output 2>&1)
    if [[ "$OUT" == ocid1.instance.* ]]; then
      INST_ID=$OUT
      log "launch accepted: $INST_ID"
    else
      printf '%s\n' "$OUT" | sed 's/^/    /' | tee -a "$LOG"
      if is_capacity_error "$OUT"; then
        log "capacity miss in $AD; sleeping ${DELAY}s"
        sleep "$DELAY"
        DELAY=$(( DELAY * 3 / 2 )); (( DELAY > BACKOFF_MAX )) && DELAY=$BACKOFF_MAX
        continue
      fi
      die "non-capacity error, stopping instead of retrying. Fix the above, then rerun."
    fi
  fi

  wait_running "$INST_ID"; RC=$?
  case $RC in
    0) report_ip "$INST_ID"; exit 0 ;;
    2) log "launched but never reached RUNNING within ${RUNNING_TIMEOUT}s; left as-is, check the Console"; exit 3 ;;
    *) log "instance $INST_ID died after launch (capacity), retrying"
       cleanup_boot_volume "$INST_ID" ;;
  esac

  sleep "$DELAY"
  DELAY=$(( DELAY * 3 / 2 )); (( DELAY > BACKOFF_MAX )) && DELAY=$BACKOFF_MAX
done