#!/usr/bin/env bash
# HELIX desktop launcher.
#
# One click -> the console opens in its own chromeless window, like a normal
# app. Closing that window quits it; reopening reloads the UI fresh (the bridge
# sends `Cache-Control: no-cache, must-revalidate`, so nothing is served from
# the browser cache).
#
# Deliberately NOT Electron or PySide6. That would mean bundling a second
# browser engine and a second copy of the frontend we already have. A .desktop
# entry plus --app mode is the native Linux way to present a web app as an app,
# and it adds zero dependencies.
#
# It also starts the backend if it is not already up, so the app works even
# after a reboot without opening a terminal first.
set -u

URL="http://127.0.0.1:8787/console/"
PROFILE="${XDG_DATA_HOME:-$HOME/.local/share}/helix-app-profile"
LOG="$HOME/.local/share/helix-app.log"

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >>"$LOG"; }

# ── 1. backend up ────────────────────────────────────────────────────────────
# systemd user units, so they survive logout. Anything already running is left
# alone; nothing is restarted, because restarting would drop live state.
for unit in helix-bridge helix-globe helix-live; do
  if ! systemctl --user is-active --quiet "$unit" 2>/dev/null; then
    say "starting $unit"
    systemctl --user start "$unit" 2>/dev/null
  fi
done

# ── 2. wait for the bridge to actually answer ────────────────────────────────
# Starting the unit returns long before the socket is listening. Poll rather
# than sleep a fixed amount, so a slow start does not open a blank window.
ready=0
for _ in $(seq 1 40); do
  if curl -sf -o /dev/null --max-time 2 "$URL"; then ready=1; break; fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  say "bridge never answered $URL"
  # Still open it: the page renders its own "bridge down" state, which is more
  # useful than silently doing nothing when the user clicked an icon.
fi

# ── 3. open the app window ───────────────────────────────────────────────────
# --app= gives a window with no tabs, no address bar and no browser chrome, so
# it reads as an application rather than a web page.
#
# Browser order is deliberate, and was measured rather than guessed:
#
#   chrome  works as a fully independent instance. A dedicated --user-data-dir
#           is honoured, so the app has its own process and quits with its
#           window. Verified: 15 processes on the private profile.
#   brave   the snap wrapper forces its own --user-data-dir and prints
#           "Opening in existing browser session", ignoring ours. So no private
#           profile is passed: it opens an app window inside the Brave session
#           the user already has running, which still looks like an app.
#   xdg-open  last resort, a normal browser tab.

open_chrome() {
  mkdir -p "$PROFILE"
  "$1" --user-data-dir="$PROFILE" \
        --no-first-run --no-default-browser-check \
        --window-size=1509,812 --window-position=0,40 \
        --app="$URL" >/dev/null 2>&1 &
}

open_existing() {
  "$1" --no-first-run --no-default-browser-check \
        --window-size=1509,812 --app="$URL" >/dev/null 2>&1 &
}

for b in google-chrome brave brave-browser chromium chromium-browser; do
  command -v "$b" >/dev/null 2>&1 || continue
  case "$b" in
    brave|brave-browser) say "launching $b (app window in the running session)"; open_existing "$b" ;;
    *)                   say "launching $b --app (private profile $PROFILE)"; open_chrome "$b" ;;
  esac
  exit 0
done

say "no supported browser found, falling back to xdg-open"
command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL"
exit 1
