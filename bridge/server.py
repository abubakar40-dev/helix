#!/usr/bin/env python3
"""Helix bridge: live Hermes/Kanban state -> UI, over SSE.

Read-only. Binds 127.0.0.1 only. No deps beyond the stdlib.

    python3 server.py [--port 8787] [--selftest]

Why SSE and not WebSocket: the UI only ever needs to *receive* pushed state, and
every browser has EventSource built in. A WebSocket here would be a dependency
plus a handshake protocol to get the same thing.
"""

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
UI_INDEX = os.path.join(HERE, "..", "ui", "agent-town", "index.html")
KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")

HOST = "127.0.0.1"  # never 0.0.0.0 - see spec: no auth assumed on Hermes routes

# Profile id -> display name. 'default' is the CEO, display-renamed "helix".
PROFILES = [
    ("default", "helix", "CEO / Orchestrator"),
    ("research", "research", "Research"),
    ("pc", "pc", "PC Control"),
]

# A task in one of these states means the agent is busy right now.
BUSY_STATUSES = ("running",)
# Unfinished-but-not-busy. A profile with any of these and nothing running is blocked.
STUCK_STATUSES = ("blocked",)


def _connect():
    # read-only URI so a bug here can never write to the board
    return sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=2)


def _profile_state(conn, pid):
    """(status, current_task) for one profile id."""
    busy = conn.execute(
        "SELECT id,title FROM tasks WHERE assignee=? AND status IN (%s) "
        "ORDER BY started_at DESC LIMIT 1" % ",".join("?" * len(BUSY_STATUSES)),
        (pid, *BUSY_STATUSES),
    ).fetchone()
    if busy:
        return "working", {"id": busy[0], "title": busy[1]}

    stuck = conn.execute(
        "SELECT id,title FROM tasks WHERE assignee=? AND status IN (%s) "
        "ORDER BY created_at DESC LIMIT 1" % ",".join("?" * len(STUCK_STATUSES)),
        (pid, *STUCK_STATUSES),
    ).fetchone()
    if stuck:
        return "blocked", {"id": stuck[0], "title": stuck[1]}

    return "idle", None


def snapshot():
    """The whole UI payload. Called on every poll; must be cheap and never raise."""
    out = {"ts": int(time.time()), "profiles": [], "events": []}
    conn = _connect()
    try:
        for pid, name, role in PROFILES:
            status, task = _profile_state(conn, pid)
            out["profiles"].append(
                {"id": pid, "name": name, "role": role, "status": status, "task": task}
            )
        out["events"] = [
            {"task": r[0], "kind": r[1], "detail": r[2], "at": r[3]}
            for r in conn.execute(
                "SELECT e.task_id, e.kind, COALESCE(e.payload,''), e.created_at "
                "FROM task_events e JOIN tasks t ON t.id = e.task_id "
                "ORDER BY e.id DESC LIMIT 25"
            )
        ]
    finally:
        conn.close()
    return out


def _selftest():
    """Check the status-derivation rules against a throwaway DB. No Hermes involved."""
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        c = sqlite3.connect(path)
        c.execute(
            "CREATE TABLE tasks (id TEXT,title TEXT,assignee TEXT,status TEXT,"
            "created_at INTEGER,started_at INTEGER)"
        )
        rows = [
            ("t1", "running task", "pc", "running", 1, 1),
            ("t2", "old stuck", "research", "blocked", 1, None),
            ("t3", "finished", "helix", "done", 1, None),
        ]
        c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?)", rows)
        c.commit()
        c.close()

        def state(pid):
            global KANBAN_DB
            old, KANBAN_DB = KANBAN_DB, path
            try:
                return _profile_state(_connect(), pid)
            finally:
                KANBAN_DB = old

        assert state("pc")[0] == "working", state("pc")
        assert state("pc")[1]["id"] == "t1"
        # a profile with only a blocked task and nothing running is blocked
        assert state("research")[0] == "blocked", state("research")
        # unknown profile, nothing assigned -> idle
        assert state("nobody")[0] == "idle", state("nobody")
        # running beats blocked: same profile cannot be both
        assert state("pc")[0] != "blocked"
        print("selftest OK: working / blocked / idle all derive correctly")
    finally:
        os.unlink(path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep the terminal readable
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send_file(UI_INDEX)
        if self.path == "/state":
            return self._send_json(snapshot())
        if self.path == "/events":
            return self._sse()
        self.send_error(404)

    def _send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            return self.send_error(500, f"UI missing: {e}")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = None
        try:
            while True:
                payload = json.dumps(snapshot())
                if payload != last:
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    last = payload
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser navigated away; normal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if not os.path.exists(KANBAN_DB):
        sys.exit(f"kanban db not found: {KANBAN_DB}")

    srv = ThreadingHTTPServer((HOST, args.port), Handler)
    srv.daemon_threads = True
    print(f"helix bridge on http://{HOST}:{args.port}  (ctrl-c to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
