#!/usr/bin/env python3
"""Helix bridge: live Hermes/Kanban state -> UI, over SSE.

Read-only. Binds 127.0.0.1 only. No deps beyond the stdlib.

    python3 server.py [--port 8787] [--selftest]

Why SSE and not WebSocket: the UI only ever needs to *receive* pushed state, and
every browser has EventSource built in. A WebSocket here would be a dependency
plus a handshake protocol to get the same thing.
"""

import argparse
import fnmatch
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "..", "ui")
# Explicit allowlist, not a generic static server: no path traversal, no surprises.
PAGES = {
    "/": "agent-town/index.html",
    "/world-monitor/": "world-monitor/index.html",
    "/voice-core/": "voice-core/index.html",
}
KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
HERMES_HOME = os.path.expanduser("~/.hermes")

# Keyless public RSS. Al Jazeera is deliberately absent - unreachable from this box.
FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Hacker News", "https://hnrss.org/frontpage"),
]
HEADLINE_TTL = 300  # seconds; be polite to the feeds and to a 7GB box
_headline_cache = {"at": 0, "data": None}

HOST = "127.0.0.1"  # never 0.0.0.0 - see spec: no auth assumed on Hermes routes

# Profile id -> display name. 'default' is the CEO, display-renamed "helix".
# Keep in sync with `hermes profile list`; a new profile wants a desk here too.
PROFILES = [
    ("default", "helix", "CEO / Orchestrator"),
    ("research", "research", "Research"),
    ("pc", "pc", "PC Control"),
    ("dev", "dev", "Development"),
    ("vision", "vision", "Vision"),
    ("comm", "comm", "Communication"),
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


def _one_feed(name, url, limit=12):
    """Fetch + parse one RSS feed. Never raises - a dead feed is not a dead endpoint."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Helix/1.0 (local)"})
        with urllib.request.urlopen(req, timeout=12) as r:
            root = ET.fromstring(r.read(2_000_000))
    except Exception as e:
        return {"source": name, "error": str(e)[:120], "items": []}

    items = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        if not t:
            continue
        items.append({
            "title": t,
            "link": (it.findtext("link") or "").strip(),
            "date": (it.findtext("pubDate") or "").strip(),
        })
        if len(items) >= limit:
            break
    return {"source": name, "error": None, "items": items}


def headlines():
    """RSS headlines for the World Monitor, cached briefly.

    stdlib only. No API key, no account, nothing to install - which is why
    currentslab/news-api-skills was not used (see docs/PROGRESS.md).
    """
    if _headline_cache["data"] and time.time() - _headline_cache["at"] < HEADLINE_TTL:
        return _headline_cache["data"]
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as ex:
        feeds = list(ex.map(lambda f: _one_feed(*f), FEEDS))
    data = {"ts": int(time.time()), "feeds": feeds,
            "total": sum(len(f["items"]) for f in feeds)}
    _headline_cache.update(at=time.time(), data=data)
    return data


def _count(path, pattern="*"):
    try:
        return len([p for p in os.listdir(path)
                    if fnmatch.fnmatch(os.path.basename(p), pattern)])
    except OSError:
        return 0


def cortex():
    """Real counts behind the Voice Core's Memory / Skills / Soul / Settings nodes.

    Counted off the filesystem, so the graph cannot drift from reality.
    """
    profiles = [HERMES_HOME]
    pd = os.path.join(HERMES_HOME, "profiles")
    if os.path.isdir(pd):
        profiles += [os.path.join(pd, p) for p in sorted(os.listdir(pd))
                     if os.path.isdir(os.path.join(pd, p))]
    return {
        # Counted across every profile, not just the default one, or the node
        # reads a misleading 0 when the per-profile dirs are where memory lives.
        "memory": sum(_count(os.path.join(h, "memories"), "*.md") for h in profiles),
        "skills": sum(1 for p in os.listdir(os.path.join(HERMES_HOME, "skills"))
                      if os.path.isdir(os.path.join(HERMES_HOME, "skills", p))),
        "soul": sum(1 for h in profiles
                    if os.path.isfile(os.path.join(h, "SOUL.md"))),
        "settings": sum(1 for h in profiles
                        if os.path.isfile(os.path.join(h, "config.yaml"))),
        "profiles": len(profiles),
    }


def say(text):
    """Create a Kanban task for the CEO from a voice transcript.

    Shells out to `hermes kanban create` rather than INSERTing into kanban.db: the
    board has claim locks, event rows and sequences that Hermes owns. Writing raw
    SQL here would create tasks the dispatcher does not understand.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty transcript"}
    if len(text) > 2000:
        return {"ok": False, "error": "transcript too long"}
    out = subprocess.run(
        ["hermes", "kanban", "create", text[:120] or "voice task",
         "--body-file", "-", "--assignee", "default", "--workspace", "scratch"],
        input=text, capture_output=True, text=True, timeout=90,
    )
    if out.returncode != 0:
        return {"ok": False, "error": (out.stderr or out.stdout).strip()[:200]}
    m = re.search(r"(t_[0-9a-f]+)", out.stdout)
    return {"ok": True, "task": m.group(1) if m else out.stdout.strip()[:80]}


STT_DIR = os.path.expanduser("~/.local/share/helix-stt")
WHISPER = os.path.join(STT_DIR, "whisper-cli")
WHISPER_MODEL = os.path.join(STT_DIR, "models", "ggml-base.bin")
MAX_AUDIO = 12 * 1024 * 1024  # ~12MB, roughly 3 minutes of opus
# 2 threads, not nproc. Measured on this 4-core box while the desktop app is running
# (load ~9): -t4 = 100s for 30s of audio, -t3 = 28s, -t2 = 21.5s, -t1 = 34s.
# Oversubscribing the cores makes it ~5x SLOWER. ponytail: revisit only if the box
# stops being busy, and re-measure before assuming.
STT_THREADS = "2"


def stt(audio: bytes, lang: str = ""):
    """Transcribe locally with whisper.cpp. No audio leaves the machine.

    base model: measured 1.64x realtime, 295MB RSS on this box. ggml-tiny was
    tried first and is not usable - it mis-detected Urdu audio as English.
    """
    if not (os.path.isfile(WHISPER) and os.path.isfile(WHISPER_MODEL)):
        return {"ok": False, "error": "whisper.cpp not installed"}
    if not audio:
        return {"ok": False, "error": "no audio"}
    # lang is a whisper code; '' means auto-detect, which is what handles
    # mixed English/Urdu better than forcing one.
    if lang and not re.fullmatch(r"[a-z]{2}(-[a-z]+)?", lang or ""):
        return {"ok": False, "error": "bad lang"}

    tmp = tempfile.mkdtemp(prefix="helix-stt-")
    src, wav = os.path.join(tmp, "in"), os.path.join(tmp, "out.wav")
    try:
        with open(src, "wb") as f:
            f.write(audio)
        # ffmpeg probes the content, so the extension does not matter. whisper
        # wants 16k mono PCM.
        p = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav],
            capture_output=True, timeout=60)
        if p.returncode != 0 or not os.path.isfile(wav):
            return {"ok": False, "error": "ffmpeg: " + p.stderr.decode()[:160]}

        cmd = [WHISPER, "-m", WHISPER_MODEL, "-nt", "-t", STT_THREADS, "-f", wav]
        if lang:
            cmd[1:1] = ["-l", lang]
        t0 = time.time()
        w = subprocess.run(cmd, capture_output=True, timeout=180)
        if w.returncode != 0:
            return {"ok": False, "error": w.stderr.decode()[:160]}
        text = " ".join(l.strip() for l in w.stdout.decode(errors="replace").splitlines() if l.strip())
        return {"ok": True, "text": text, "lang": lang or "auto",
                "ms": int((time.time() - t0) * 1000)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "whisper timed out"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
        # Match on the path only; a cache-busting query must not 404 the page.
        path = self.path.split("?", 1)[0]
        if path in PAGES:
            return self._send_file(os.path.join(UI_DIR, PAGES[path]))
        # Sibling assets for a page (voice-core pulls orbScene.js). Restricted to
        # realpaths under ui/ so "../" cannot escape.
        asset = os.path.realpath(os.path.join(UI_DIR, path.lstrip("/")))
        if (asset.startswith(os.path.realpath(UI_DIR) + os.sep)
                and os.path.isfile(asset)
                and os.path.splitext(asset)[1] in (".js", ".css", ".svg", ".json")):
            return self._send_file(asset)
        if path == "/state":
            return self._send_json(snapshot())
        if path == "/headlines":
            return self._send_json(headlines())
        if path == "/cortex":
            return self._send_json(cortex())
        if path == "/events":
            return self._sse()
        self.send_error(404)

    def do_POST(self):
        # /say is a WRITE path (it creates a Kanban task), unlike everything else
        # here. Any page in any browser can POST to 127.0.0.1, so requiring a custom
        # header forces a CORS preflight that we never answer — a foreign page then
        # cannot make the browser send it.
        if self.path.split("?", 1)[0] not in ("/say", "/stt"):
            return self.send_error(404)
        if self.headers.get("X-Helix-Voice") != "1":
            return self._send_json({"ok": False, "error": "missing X-Helix-Voice header"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        is_stt = self.path.split("?", 1)[0] == "/stt"
        # Different caps: a JSON note is tiny, audio is not. A single 64KB cap on
        # both capped /stt at ~2 seconds of audio, which looked like "STT broken".
        limit = MAX_AUDIO if is_stt else 64_000
        if n > limit:
            return self._send_json({"ok": False, "error": "body too large"}, 413)
        lang = ""
        if "?" in self.path:
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            lang = (q.get("lang") or [""])[0]
        body = self.rfile.read(n) if n else b""
        try:
            if is_stt:
                return self._send_json(stt(body, lang))
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return self._send_json({"ok": False, "error": "bad json"}, 400)
            return self._send_json(say(payload.get("text")))
        except subprocess.TimeoutExpired:
            return self._send_json({"ok": False, "error": "hermes timed out"}, 504)

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            return self.send_error(500, f"missing asset: {e}")
        # Must be right: module scripts are refused outright under a wrong MIME type.
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
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
