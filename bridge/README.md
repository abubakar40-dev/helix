# Bridge

Exposes live Hermes/Kanban state to the UI panels. Read-only, localhost-only.

## Run

Now a managed service — starts on login, survives logout (linger is on):

    systemctl --user status helix-bridge
    journalctl --user -u helix-bridge -f
    systemctl --user restart helix-bridge
    systemctl --user disable --now helix-bridge   # undo

Unit: `~/.config/systemd/user/helix-bridge.service`.

To run it by hand instead (don't — the unit will fight you for port 8787):

    python3 ~/helix/bridge/server.py            # http://127.0.0.1:8787
    python3 ~/helix/bridge/server.py --selftest # check the status rules, no server needed

## Endpoints

| | |
|---|---|
| `GET /` | the Agent Town page (served from `~/helix/ui/agent-town/`) |
| `GET /state` | one JSON snapshot |
| `GET /events` | SSE stream, pushes a snapshot whenever it changes (polls the DB every 1s) |
| `GET /cortex` | counts behind the Voice Core nodes (memory/skills/soul/settings) |
| `POST /say` | **write path** — creates a Kanban task for the CEO from a transcript |
| `POST /stt?lang=` | **write path** — local speech-to-text via whisper.cpp (empty lang = auto) |

## Design notes

- **Reads `~/.hermes/kanban.db` directly, SQLite read-only** (`mode=ro` — verified:
  writes raise `attempt to write a readonly database`). No Hermes internals, no API,
  no auth needed. A bug in here cannot corrupt the board.
- **SSE, not WebSocket.** The UI only receives pushed state, and `EventSource` is
  built into every browser. A WebSocket would add a dependency and a handshake for
  the same result.
- **`/say` and `/stt` are the only write endpoints.** Everything else reads. Both
  require the `X-Helix-Voice: 1` header so a random web page cannot make a browser
  POST to localhost; the preflight is never answered, so the request is never sent.
  `/stt` also validates `lang` against `[a-z]{2}(-[a-z]+)?`.
- **Bound to `127.0.0.1` explicitly.** Verified: reachable on loopback (200), refused
  on the LAN address `192.168.100.97:8787`. Never widen this — Hermes's own routes
  assume no auth.
- **Stdlib only.** No pip install, no venv, nothing to keep in sync.

## Status rules

| status | when |
|---|---|
| `working` | the profile has a task in `running` |
| `blocked` | not running, but has at least one `blocked` task |
| `idle` | neither |

A profile that just finished work shows `blocked` if it still has stuck tasks on the
board — the desk shows *which* task, so it is not ambiguous. That is honest: the work
really is stuck. Refine later only if it proves noisy in practice.

## Not done yet

- `helix` (CEO) is in the payload but has no desk yet; the UI is scoped to two desks.
- No auth. Safe only because it binds loopback — do not widen the bind without adding
  auth first (nothing on Hermes's own routes assumes it either).
