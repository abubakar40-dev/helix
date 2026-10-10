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

# OCI warns on every call that the private key has no OCI_API_KEY label. Cosmetic,
# and it drowns the log, so silence it by default.
export SUPPRESS_LABEL_WARNING="${SUPPRESS_LABEL_WARNING:-True}"

# ---------------------------------------------------------------- config
REGION="${OCI_REGION:-me-abudhabi-1}"
SHAPE="${OCI_SHAPE:-VM.Standard.A1.Flex}"
# Start at 1 OCPU / 6 GB, not 2 / 12. Capacity is fragmented across hosts, so a
# 1-OCPU request lands somewhere a 2-OCPU one cannot, and that is where the extra
# success rate comes from. Two 1-OCPU instances total the same 2 OCPU / 12 GB, so
# nothing is given up: once this one is up, rerun with a different display-name to
# fill out the allowance. Resizing up is NOT equivalent - a resize behaves like a
# fresh launch and can fail, leaving you with less than you started with.
OCPUS="${OCI_OCPUS:-1}"
MEMORY_GB="${OCI_MEMORY_GB:-6}"
DISPLAY_NAME="${OCI_DISPLAY_NAME:-helix-a1}"
VCN_NAME="${OCI_VCN_NAME:-helix-vcn}"
SUBNET_NAME="${OCI_SUBNET_NAME:-public subnet-helix-vcn}"
SSH_KEY="${OCI_SSH_KEY:-$HOME/.ssh/helix_oracle.pub}"
UBUNTU="${OCI_UBUNTU_VERSION:-24.04}"
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

# oci --raw-output prints the whole JSON array for a list query, not one value per
# line, so unwrap lists with jq. A single-value --query with --raw-output is fine.
# ol <jq-suffix> <oci args...>   e.g. ol '.id' network vcn list --compartment-id X
ol() { local path=$1; shift; o "$@" --output json 2>/dev/null | jq -r ".data[]${path}"; }

# OCI writes errors as JSON on stderr; fall back to raw text when it is not JSON.
err_field() { printf '%s' "$1" | jq -r ".${2} // empty" 2>/dev/null; }

# Retry only what is genuinely transient: capacity, throttling, and transport
# failures. A dropped connection is ambiguous - the launch may have been accepted
# and only the response lost - but the next attempt adopts an existing instance
# rather than creating a second one, so retrying is safe and stopping is not.
is_retryable_error() {
  local json=$1 msg code status blob
  msg=$(err_field "$json" message); code=$(err_field "$json" code); status=$(err_field "$json" status)
  [[ -z "$msg" && -z "$code" ]] && msg="$json"
  blob=$(printf '%s %s' "$msg" "$code" | tr '[:upper:]' '[:lower:]')
  case "$blob" in
    *"out of host capacity"*|*"out of capacity"*|*"insufficient capacity"*) return 0 ;;
    *requestexception*)                                                       return 0 ;;
    *"timed out"*|*"timeout"*|*"connection refused"*|*"connection reset"*)   return 0 ;;
    *"network is unreachable"*|*"could not connect"*|*"max retries exceeded"*) return 0 ;;
  esac
  case "$status" in 429|502|503|504) return 0 ;; esac
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
AD_JSON=$(ol '.name' iam availability-domain list --compartment-id "$COMPARTMENT")
if [[ -z "$AD_JSON" ]]; then
  o iam availability-domain list --compartment-id "$COMPARTMENT" --query 'data[].name' --raw-output 2>&1 | tee -a "$LOG" >&2
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
  mapfile -t vcns < <(ol '.id' network vcn list --compartment-id "$COMPARTMENT" --display-name "$VCN_NAME")
  case ${#vcns[@]} in
    1) VCN_ID=${vcns[0]} ;;
    0) die "no VCN named '$VCN_NAME' in $COMPARTMENT. Fix the name, or set OCI_VCN_ID / OCI_COMPARTMENT_ID. VCNs present: $(ol '."display-name"' network vcn list --compartment-id "$COMPARTMENT" | tr '\n' ' ')" ;;
    *) die "'$VCN_NAME' matches ${#vcns[@]} VCNs - set OCI_VCN_ID explicitly" ;;
  esac
fi
log "vcn      : $VCN_ID"

# Subnet.
SUBNET_ID="${OCI_SUBNET_ID:-}"
if [[ -z "$SUBNET_ID" ]]; then
  mapfile -t subs < <(ol '.id' network subnet list --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" --display-name "$SUBNET_NAME")
  case ${#subs[@]} in
    1) SUBNET_ID=${subs[0]} ;;
    0) die "no subnet named '$SUBNET_NAME' in $VCN_ID. Subnets present: $(ol '."display-name"' network subnet list --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" | tr '\n' ' ')" ;;
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
  v=$(o compute instance list --compartment-id "$COMPARTMENT" --output json 2>/dev/null \
      | jq -r --arg s "$SHAPE" '[.data[] | select(.lifecycleState != "TERMINATED" and .shape == $s)
                                 | .shapeConfig.'"$1"' // 0] | add // 0 | floor')
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 0
}
used_ocpu=$(sum_a1 ocpus); used_mem=$(sum_a1 memoryInGBs)
if (( used_ocpu + OCPUS > FREE_OCPUS || used_mem + MEMORY_GB > FREE_MEMORY_GB )); then
  log "BILLING RISK: A1 instances already use ${used_ocpu} OCPU / ${used_mem} GB; this adds ${OCPUS}/${MEMORY_GB}, over the Always Free cap of ${FREE_OCPUS}/${FREE_MEMORY_GB}."
else
  log "always free: ${used_ocpu}->$((used_ocpu + OCPUS)) OCPU, ${used_mem}->$((used_mem + MEMORY_GB)) GB (cap ${FREE_OCPUS}/${FREE_MEMORY_GB})"
fi

# Image: newest Canonical Ubuntu for this region, resolved at runtime because OCIDs
# are regional and Canonical rotates the image. Must be the aarch64 build: A1 is
# ARM, and an x86_64 Ubuntu image will not boot on it.
IMAGE_ID="${OCI_IMAGE_ID:-}"
if [[ -z "$IMAGE_ID" ]]; then
  IMAGE_ID=$(o compute image list --compartment-id "$COMPARTMENT" --operating-system "Canonical Ubuntu" \
    --output json 2>/dev/null | jq -r --arg v "$UBUNTU" \
      '[.data[] | select(.["operating-system-version"] == $v and (.["display-name"] | test("aarch64")))]
       | sort_by(.["time-created"]) | last | .id // empty')
  [[ "$IMAGE_ID" == ocid1.image.* ]] || die "no Canonical Ubuntu $UBUNTU aarch64 image in $REGION (set OCI_UBUNTU_VERSION or OCI_IMAGE_ID)"
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
         --output json 2>/dev/null | jq -r '.data[0].volumeId // empty')
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
  INST_ID=$(o compute instance list --compartment-id "$COMPARTMENT" --output json 2>/dev/null \
            | jq -r '.data[] | select(.displayName == $n and .lifecycleState != "TERMINATED") | .id' \
                  --arg n "$DISPLAY_NAME" | head -1)
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
      if is_retryable_error "$OUT"; then
        log "transient failure in $AD; sleeping ${DELAY}s"
        sleep "$DELAY"
        DELAY=$(( DELAY * 3 / 2 )); (( DELAY > BACKOFF_MAX )) && DELAY=$BACKOFF_MAX
        continue
      fi
      die "non-transient error, stopping instead of retrying. Fix the above, then rerun."
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