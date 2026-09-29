# Bridge

Exposes live Hermes/Kanban state to the UI panels. Read-only, localhost-only.

## Run

    python3 ~/helix/bridge/server.py            # http://127.0.0.1:8787
    python3 ~/helix/bridge/server.py --selftest # check the status rules, no server needed

## Endpoints

| | |
|---|---|
| `GET /` | the Agent Town page (served from `~/helix/ui/agent-town/`) |
| `GET /state` | one JSON snapshot |
| `GET /events` | SSE stream, pushes a snapshot whenever it changes (polls the DB every 1s) |

## Design notes

- **Reads `~/.hermes/kanban.db` directly, SQLite read-only** (`mode=ro` — verified:
  writes raise `attempt to write a readonly database`). No Hermes internals, no API,
  no auth needed. A bug in here cannot corrupt the board.
- **SSE, not WebSocket.** The UI only receives pushed state, and `EventSource` is
  built into every browser. A WebSocket would add a dependency and a handshake for
  the same result.
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

- Not a systemd service — it dies with the shell. The gateway survives logout via
  linger; if the bridge should too, wrap it the same way. Do this when it matters,
  not before.
- `helix` (CEO) is in the payload but has no desk yet; the UI is scoped to two desks.
