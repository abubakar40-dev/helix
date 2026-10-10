#!/usr/bin/env bash
# Self-check for tools/oci-a1-launch.sh. Runs a fake `oci` on PATH, so it needs no
# tenancy, no credentials and no network.
#
#   bash tools/check-oci-retry.sh
#
# Covers the two things that actually matter: which OCI errors are retried (only
# transient capacity ones), and that the loop adopts instead of double-launching.

set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TARGET="$HERE/oci-a1-launch.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
FAILED=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILED=1; }

# ---------------------------------------------------------------- 1. classifier
# is_capacity_error decides retry-vs-stop. Retrying a non-capacity error hides the
# real fault and hammers the API; stopping on a capacity error gives up the VM.
# Extract verbatim from the script so the check cannot drift from what runs.
extract_fn() { # fn-name: one-liner functions end on their opening line
  local n
  n=$(grep -n "^$1() {" "$TARGET" | head -1 | cut -d: -f1)
  [[ -n "$n" ]] || return 1
  if sed -n "${n}p" "$TARGET" | grep -q '}[[:space:]]*$'; then
    sed -n "${n}p" "$TARGET"
  else
    sed -n "${n},\$p" "$TARGET" | sed -n '1,/^}/p'
  fi
}
extract_fn err_field > "$TMP/clf.sh" || { fail "no err_field in $TARGET"; exit 1; }
extract_fn is_capacity_error >> "$TMP/clf.sh" || { fail "no is_capacity_error in $TARGET"; exit 1; }
# shellcheck disable=SC1091
. "$TMP/clf.sh" || { fail "could not extract is_capacity_error from $TARGET"; exit 1; }
type -t is_capacity_error >/dev/null || { fail "is_capacity_error did not load"; exit 1; }

check_err() { # desc, json, want(retry|stop)
  local got; is_capacity_error "$2" && got=retry || got=stop
  [[ "$got" == "$3" ]] && pass "$1 -> $got" || fail "$1 -> got $got, want $3"
}

check_err "Out of host capacity (InternalError/500)" '{"code":"InternalError","message":"Out of host capacity.","status":500}' retry
check_err "LimitExceeded out of capacity"             '{"code":"LimitExceeded","message":"Out of capacity for shape VM.Standard.A1.Flex","status":409}' retry
check_err "429 throttling"                            '{"code":"TooManyRequests","message":"Rate limit exceeded","status":429}' retry
check_err "capacity as plain text, not JSON"          'ServiceError: Out of host capacity' retry
check_err "auth failure"                              '{"code":"NotAuthorizedOrNotFound","message":"Authorization failed","status":404}' stop
check_err "wrong fingerprint"                         '{"code":"InvalidApiKey","message":"The API key fingerprint does not match","status":401}' stop
check_err "compartment quota exceeded"                '{"code":"LimitExceeded","message":"Compartment quota exceeded for instance count","status":409}' stop
check_err "subnet forbids public IP"                  '{"code":"InvalidParameter","message":"assignPublicIp cannot be set when prohibitPublicIpOnVnic is true","status":400}' stop
check_err "Always Free limit reached"                 '{"code":"LimitExceeded","message":"The Always Free limit for A1 has been reached","status":409}' stop

# ---------------------------------------------------------------- 2. fake oci
mkdir -p "$TMP/bin"
cat > "$TMP/bin/oci" <<'MOCK'
#!/usr/bin/env bash
n=$(cat "$MOCK_STATE" 2>/dev/null || echo 0)
[[ "$1" == "--version" ]] && { echo "3.94.2"; exit 0; }
while [[ $# -gt 0 ]]; do
  case "$1" in --region|--max-retries|--profile) shift 2; continue ;; esac
  break
done
cmd=(); while (( $# )); do case "$1" in --*|-*) break ;; esac; cmd+=("$1"); shift; done
set -- "${cmd[@]}"
case "$1 $2" in
  # The real CLI wraps list output in {"data":[...]} and prints the whole array for
  # a list query even with --raw-output. An unfaithful mock hid a whole class of
  # parsing bugs, so mirror the real envelope exactly.
  "iam availability-domain") echo '{"data":[{"name":"Abcd:ME-ABUDHABI-1-AD-1"},{"name":"Abcd:ME-ABUDHABI-1-AD-2"}]}' ;;
  "compute shape") echo '{"data":[{"shape":"VM.Standard.A1.Flex"}]}' ;;
  "network vcn")   echo '{"data":[{"id":"ocid1.vcn.oc1.me-abudhabi-1.helixvcn","display-name":"helix-vcn"}]}' ;;
  "network subnet")
    if [[ "$3" == "get" ]]; then echo '{"data":{"prohibit-public-ip-on-vnic":false}}'
    else echo '{"data":[{"id":"ocid1.subnet.oc1.me-abudhabi-1.public","display-name":"public"}]}'; fi ;;
  "compute image") echo '{"data":[{"id":"ocid1.image.oc1.me-abudhabi-1.ubuntu2404","operating-system-version":"24.04","display-name":"Canonical-Ubuntu-24.04-aarch64-2026.09.18-0","time-created":"2026-09-21T00:00:00Z"}]}' ;;
  "compute instance") case "$3" in
      launch)
        n=$((n+1)); echo "$n" > "$MOCK_STATE"
        if (( n <= ${MOCK_LAUNCH_FAILS:-0} )); then
          printf '{"code":"%s","message":"%s","status":%s}\n' \
            "${MOCK_ERR_CODE:-InternalError}" "${MOCK_ERR_MSG:-Out of host capacity.}" \
            "${MOCK_ERR_STATUS:-500}" >&2
          exit 1
        fi
        echo "ocid1.instance.oc1.me-abudhabi-1.fresh$n" ;;
      get) echo "${MOCK_GET_STATE:-RUNNING}" ;;
      list)
        if [[ -n "${MOCK_EXISTING:-}" ]]; then
          echo "{\"data\":[{\"id\":\"$MOCK_EXISTING\",\"displayName\":\"helix-a1\",\"lifecycleState\":\"RUNNING\",\"shape\":\"VM.Standard.A1.Flex\",\"shapeConfig\":{\"ocpus\":1,\"memoryInGBs\":6}}]}"
        elif [[ -n "${MOCK_A1_OCPU:-}" ]]; then
          echo "{\"data\":[{\"id\":\"ocid1.instance.oc1.me-abudhabi-1.old\",\"displayName\":\"other-a1\",\"lifecycleState\":\"RUNNING\",\"shape\":\"VM.Standard.A1.Flex\",\"shapeConfig\":{\"ocpus\":$MOCK_A1_OCPU,\"memoryInGBs\":6}}]}"
        else
          echo '{"data":[]}'
        fi ;;
      list-vnics) echo "158.101.42.77" ;;
      *) echo '{}' ;;
    esac ;;
  "compute volume-attachment")
    [[ "$3" == "delete" ]] && exit 0
    echo '{"data":[{"volumeId":"ocid1.volume.oc1.me-abudhabi-1.orphan"}]}' ;;
  "bv volume") exit 0 ;;
  *) echo '{}' ;;
esac
exit 0
MOCK
chmod +x "$TMP/bin/oci"

printf '[DEFAULT]\nuser=ocid1.user.oc1..aaa\nfingerprint=41:84:23:c0:50:06:c0:b9:00:7d:be:4d:f5:e9:2a:85\n' \
  > "$TMP/oci.config"
printf 'tenancy=ocid1.tenancy.oc1..aaa\nregion=me-abudhabi-1\n' >> "$TMP/oci.config"
chmod 600 "$TMP/oci.config"
: > "$TMP/key.pub"

run() { # desc, then env assignments, then "--" then script args
  local d=$1; shift
  local envs=() args=() seen=0 a
  for a in "$@"; do
    if   [[ "$a" == "--" ]]; then seen=1
    elif (( seen ));         then args+=("$a")
    else                           envs+=("$a"); fi
  done
  MOCK_STATE="$TMP/state"; export MOCK_STATE; rm -f "$TMP/state"
  env PATH="$TMP/bin:$PATH" OCI_CONFIG_FILE="$TMP/oci.config" OCI_LOG="$TMP/run.log" \
      OCI_SSH_KEY="$TMP/key.pub" OCI_BACKOFF_MIN=1 OCI_BACKOFF_MAX=1 \
      OCI_RUNNING_TIMEOUT=25 "${envs[@]}" timeout 90 bash "$TARGET" "${args[@]}" 2>&1
}

# ---------------------------------------------------------------- 3. end to end
out=$(run "x" MOCK_LAUNCH_FAILS=2)
grep -q 'Out of host capacity' <<<"$out" || fail "capacity error was not logged"
grep -q 'LAUNCHED'                     <<<"$out" || fail "loop did not report success"
grep -q '158.101.42.77'                <<<"$out" || fail "public IP was not printed"
ads=$(grep -o 'AD=Abcd:ME-ABUDHABI-1-AD-[12]' <<<"$out" | sort -u | wc -l)
[[ "$ads" -eq 2 ]] && pass "retried across availability domains ($ads)" \
                   || fail "expected both ADs to be tried, saw $ads"
pass "end-to-end: capacity miss -> retry -> success -> public IP"

out=$(run "x" MOCK_LAUNCH_FAILS=99 MOCK_ERR_MSG="Compartment quota exceeded" MOCK_ERR_CODE=LimitExceeded MOCK_ERR_STATUS=409)
grep -q 'non-capacity error, stopping' <<<"$out" && pass "quota error stops instead of retrying" \
                                             || fail "quota error did not stop the loop"

out=$(run "x" MOCK_LAUNCH_FAILS=99 MOCK_EXISTING=ocid1.instance.oc1.me-abudhabi-1.alreadyhere)
grep -q 'adopting it' <<<"$out" && pass "existing instance adopted" || fail "did not adopt existing instance"
grep -q 'launch accepted' <<<"$out" && fail "launched a duplicate despite an existing instance" \
                                  || pass "no duplicate launch"

out=$(run "x" MOCK_A1_OCPU=1 MOCK_LAUNCH_FAILS=0)
grep -q 'BILLING RISK' <<<"$out" && pass "warns when the launch would exceed Always Free" \
                               || fail "no BILLING RISK warning with an A1 already running"

out=$(run "x" MOCK_GET_STATE=TERMINATED MOCK_LAUNCH_FAILS=0 OCI_MAX_ATTEMPTS=2)
grep -q 'reclaimed orphaned boot volume' <<<"$out" && pass "reclaims boot volume from a dead instance" \
                                                 || fail "dead instance leaked its boot volume"

out=$(run "x" MOCK_LAUNCH_FAILS=99 OCI_MAX_ATTEMPTS=3); rc=$?
[[ "$(grep -c 'capacity miss' <<<"$out")" -eq 3 ]] && pass "OCI_MAX_ATTEMPTS bounds the loop" \
                                                    || fail "OCI_MAX_ATTEMPTS not honoured"
[[ "$rc" -eq 4 ]] && pass "giving up exits non-zero (no VM exists)" \
                  || fail "gave up but exited $rc, which reads as success"

out=$(run "x" MOCK_LAUNCH_FAILS=99 -- --dry-run)
grep -q 'nothing launched' <<<"$out" && ! grep -q 'capacity miss' <<<"$out" \
  && pass "--dry-run launches nothing" || fail "--dry-run launched something"

# ------------------------------------------------------- 4. real CLI surface
# Two bugs got through the mock: `oci compute boot-volume` does not exist (it is
# `oci bv volume`, and delete takes --volume-id), and `compute instance list` has
# no --shape filter. Both fail silently, so check them against the real binary.
if command -v oci >/dev/null; then
  while IFS='|' read -r cmd flag; do
    oci $cmd --help 2>&1 | grep -q -- "$flag" && pass "oci $cmd $flag exists" \
                                                  || fail "oci $cmd $flag is MISSING from the installed CLI"
  done <<'CMDS'
compute instance launch|--assign-public-ip
compute instance launch|--shape-config
compute instance launch|--ssh-authorized-keys-file
compute instance list|--display-name
compute volume-attachment list|--instance-id
bv volume delete|--volume-id
compute image list|--operating-system
CMDS
  # compare against code only, the comments name the commands on purpose
  code() { grep -vE '^[[:space:]]*#' "$TARGET"; }
  code | grep -q 'compute boot-volume' \
    && fail "script calls 'oci compute boot-volume', which does not exist" \
    || pass "script avoids the non-existent 'compute boot-volume'"
  code | grep -qE 'compute instance list.*--shape' \
    && fail "script uses an --shape filter that instance list does not support" \
    || pass "script avoids the unsupported instance-list --shape filter"
else
  printf 'SKIP  real-CLI surface check (oci not installed)\n'
fi

echo
(( FAILED )) && { echo "CHECK FAILED"; exit 1; }
echo "ALL CHECKS PASS"