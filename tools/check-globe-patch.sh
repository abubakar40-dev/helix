#!/usr/bin/env bash
# The Helix World Monitor iframes gods-eye-view, which ships framing headers
# (X-Frame-Options: DENY, CSP frame-ancestors 'none') that block exactly that.
# They are removed locally in build/vite.js. A `git pull` or `git checkout`
# there can silently drop that edit, and the symptom is a blank globe panel
# with no error anywhere -- so this checks for it instead of relying on memory.
#
#   ./check-globe-patch.sh        report only, non-zero if the patch is missing
#   ./check-globe-patch.sh --fix  re-apply if missing, then report
#
# Idempotent: safe to run repeatedly, and a no-op when the patch is present.
set -uo pipefail

REPO="${GODSEYE_REPO:-/home/abubakargcx/gods-eye-view}"
FILE="$REPO/build/vite.js"
MARKER="ponytail: local Helix edit"

status() { printf '  %s\n' "$1"; }

if [ ! -f "$FILE" ]; then
  status "FAIL  $FILE does not exist"
  exit 2
fi

# Already patched? A no-op is success.
if grep -qF "$MARKER" "$FILE"; then
  status "OK    frame headers removed, globe can be iframed ($FILE)"
  exit 0
fi

if [ "${1:-}" != "--fix" ]; then
  status "MISSING  $FILE still sets X-Frame-Options / frame-ancestors."
  status "          The Helix World Monitor panel will render blank."
  status "          Re-run with --fix, or see docs/PROGRESS.md."
  exit 1
fi

# Re-apply: drop the two header lines and the CSP line, then leave a marker so
# this script is idempotent next time. Backs up first.
cp "$FILE" "$FILE.bak-helix"
python3 - "$FILE" "$MARKER" <<'PY'
import re, sys
path, marker = sys.argv[1], sys.argv[2]
src = open(path, encoding="utf-8").read()
before = src
# Remove the framing headers, whether or not they carry a comment.
src = re.sub(r"\n\s*//[^\n]*X-Frame-Options[^\n]*", "", src)
# The upstream comment claiming these headers protect Provider Settings becomes false
# once they are gone; leaving it above an empty object misleads whoever reads it next.
src = re.sub(r"\n\s*//\s*These headers protect the document[^\n]*", "", src)
src = re.sub(r"\n\s*'X-Frame-Options':\s*'[^']*',?", "", src)
src = re.sub(r"\n\s*\"X-Frame-Options\":\s*\"[^\"]*\",?", "", src)
src = re.sub(r"\n\s*'Content-Security-Policy':\s*(\"[^\"]*\"|'[^']*'),?", "", src)
src = re.sub(r"\n\s*\"Content-Security-Policy\":\s*(\"[^\"]*\"|'[^']*'),?", "", src)
if src == before:
    sys.exit("no header lines matched -- refusing to touch the file")
open(path, "w", encoding="utf-8").write(src)
print(f"  patched {path}")
PY

if ! grep -qF "$MARKER" "$FILE"; then
  # Append the marker comment so the next run short-circuits.
  printf '\n// %s\n' "$MARKER — headers removed by check-globe-patch.sh so Helix can iframe this app." \
    >> "$FILE"
fi

grep -qE "'X-Frame-Options'|frame-ancestors" "$FILE" && {
  status "FAIL  header lines still present after patching; inspect $FILE"
  exit 1
}

status "OK    re-applied (backup at $FILE.bak-helix)"
