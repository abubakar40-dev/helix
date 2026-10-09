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
    "/": "shell/index.html",
    "/agent-town/": "agent-town/index.html",
    "/world-monitor/": "world-monitor/index.html",
    "/voice-core/": "voice-core/index.html",
    "/console/": "console/index.html",
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
    ("sysmon", "sysmon", "System Monitor"),
    ("career", "career", "Career"),
    ("document", "document", "Documents"),
]

# A task in one of these states means the agent is busy right now.
BUSY = "running"
# Unfinished-but-not-busy. A profile with any of these and nothing running is blocked.
STUCK = "blocked"


def _connect():
    # read-only URI so a bug here can never write to the board
    return sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=2)


def _profile_state(conn, pid):
    """(status, current_task) for one profile id."""
    busy = conn.execute(
        "SELECT id,title FROM tasks WHERE assignee=? AND status=? "
        "ORDER BY started_at DESC LIMIT 1", (pid, BUSY)
    ).fetchone()
    if busy:
        return "working", {"id": busy[0], "title": busy[1]}

    stuck = conn.execute(
        "SELECT id,title FROM tasks WHERE assignee=? AND status=? "
        "ORDER BY created_at DESC LIMIT 1", (pid, STUCK)
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
    out.update(runtime_info())
    return out


def runtime_info():
    """Model actually in use + live context usage, for the status bar.

    The status bar had a `model` field that nothing ever wrote, so it read "—"
    forever. Both numbers here are measured, not labelled: the model is the
    resolved `model.default` of the profile that owns the gateway, and the token
    count is `gateway_routing.last_prompt_tokens` - the same column the watchdog
    reads. Never raises: a status bar is not worth a 500.
    """
    info = {"model": "", "ctx": None, "ctx_max": 200000}
    try:
        import yaml  # only needed here; the rest of the bridge stays stdlib
        cfg = os.path.join(HERMES_HOME, "config.yaml")
        if os.path.exists(cfg):
            info["model"] = (yaml.safe_load(open(cfg)) or {}).get("model", {}).get("default", "")
    except Exception:
        pass
    # last_prompt_tokens is NOT a column: gateway_routing is
    # (scope, session_key, entry_json, updated_at) and the number lives inside
    # the JSON. I first queried it as a column; the sqlite error was swallowed
    # by a bare except and the status bar reported a confident, invented 0.
    # A wrong measurement dressed as a real one is the failure mode this project
    # keeps paying for, so a failure here reports "unknown", never a number.
    info["ctx"] = None
    try:
        db = os.path.join(HERMES_HOME, "state.db")
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        best = 0
        for (ej,) in c.execute("SELECT entry_json FROM gateway_routing "
                               "ORDER BY updated_at DESC LIMIT 25"):
            try:
                best = max(best, int(json.loads(ej).get("last_prompt_tokens") or 0))
            except (TypeError, ValueError):
                continue
        c.close()
        info["ctx"] = best
    except Exception:
        info["ctx"] = None
    return info


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


def _count(path, pattern="*.md"):
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
        "memory": sum(_count(os.path.join(h, "memories")) for h in profiles),
        "skills": sum(1 for p in os.listdir(os.path.join(HERMES_HOME, "skills"))
                      if os.path.isdir(os.path.join(HERMES_HOME, "skills", p))),
        "soul": sum(1 for h in profiles
                    if os.path.isfile(os.path.join(h, "SOUL.md"))),
        "settings": sum(1 for h in profiles
                        if os.path.isfile(os.path.join(h, "config.yaml"))),
    }


CAM_PROCS = []          # live ffmpeg camera processes, so a tab close can reap them
CAM_DEV = "/dev/video0"


def _cam_kill_all():
    for p in list(CAM_PROCS):
        try:
            p.kill()
        except OSError:
            pass
    CAM_PROCS.clear()


def camera_stream(handler, dev=None, size="320x240", fps=5):
    """multipart/x-mixed-replace MJPEG straight from the webcam, for an <img>.

    Verified before building: /dev/video0 is a Microdia Integrated_Webcam_HD,
    ffmpeg captures it directly at 320x240/5fps for ~9% CPU and ~308 KB/s.

    Started only on request and reaped when the client goes away, so the camera is
    not left running with the UI closed. Bound by the bridge, which is
    localhost-only - the same control every other Helix endpoint relies on.
    """
    dev = dev or CAM_DEV
    if not os.path.exists(dev):
        return handler.send_error(404, f"no camera at {dev}")
    # One stream at a time. Three concurrent /cam requests had three ffmpeg
    # processes all holding /dev/video0, and a fourth client then got zero bytes
    # back - the camera was contended, not broken.
    _cam_kill_all()
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "v4l2", "-video_size", size, "-framerate", str(fps),
             "-i", dev, "-f", "mpjpeg", "-q:v", "6", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    except FileNotFoundError:
        return handler.send_error(500, "ffmpeg not installed")
    CAM_PROCS.append(proc)
    # HTTP/1.0 framing on purpose. Under HTTP/1.1 an endless body with no
    # Content-Length is sent as `transfer-encoding: chunked`, and browsers cannot
    # decode a chunked multipart image stream - the request came back 200 with
    # the right content-type and still failed to produce a frame. Delimiting the
    # body by connection close is what multipart/x-mixed-replace expects.
    handler.protocol_version = "HTTP/1.0"
    handler.send_response(200)
    handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    # ffmpeg's mpjpeg output is a raw concatenation of whole JPEGs, so the
    # frames have to be found by their SOI/EOI markers. My first version read
    # fixed 4096-byte chunks and declared each one a frame: every part was a
    # fragment, there were 0 complete JPEGs in a 760KB capture, and the browser
    # correctly refused to decode it. curl "worked" only because it dumps bytes.
    SOI, EOI = b"\xff\xd8", b"\xff\xd9"
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            buf += chunk
            # cut off anything before a start-of-frame marker
            i = buf.find(SOI)
            if i > 0:
                buf = buf[i:]
            elif i < 0:
                buf = buf[-1:]          # keep 1 byte: a marker may straddle reads
                continue
            # emit every complete frame currently buffered
            while True:
                j = buf.find(EOI, 2)
                if j < 0:
                    break
                frame, buf = buf[:j + 2], buf[j + 2:]
                handler.wfile.write(b"--FRAME\r\n")
                handler.wfile.write(b"Content-Type: image/jpeg\r\nContent-Length: "
                                    + str(len(frame)).encode() + b"\r\n\r\n")
                handler.wfile.write(frame)
                handler.wfile.write(b"\r\n")
            # never let a stalled frame grow without bound
            if len(buf) > 4 << 20:
                buf = buf[-2:]
    except (BrokenPipeError, ConnectionResetError):
        pass          # the browser tab closed; that is the normal way this ends
    finally:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
        if proc in CAM_PROCS:
            CAM_PROCS.remove(proc)


def activity(limit=40):
    """Real tool calls, newest first, for the Console panel.

    Read straight out of state.db: `messages.tool_calls` holds the arguments the
    agent actually passed, the matching `role='tool'` row holds the result.
    Nothing here is synthesised - if Hermes did not call a tool, there is nothing
    to show.

    **Every profile's state.db is read, not just the default one.** Each Hermes
    profile keeps its own state.db, so reading only `~/.hermes/state.db` showed
    the CEO's activity and nothing else, all of it labelled `default`. Measured:
    228 tool rows in the default db against 630 across all nine - the single-db
    version silently dropped 64% of what the system actually did. The profile
    directory name is the real profile label; `sessions.profile_name` is
    `default` in every row and so cannot be used for this.
    """
    # The default profile's db sits in ~/.hermes itself; basename would label it
    # ".hermes", so it is named explicitly as the CEO.
    dbs = [("helix", os.path.join(HERMES_HOME, "state.db"))]
    pd = os.path.join(HERMES_HOME, "profiles")
    if os.path.isdir(pd):
        dbs += [(p, os.path.join(pd, p, "state.db"))
                for p in sorted(os.listdir(pd))
                if os.path.isfile(os.path.join(pd, p, "state.db"))]
    calls, prof = [], {}
    for profile, db in dbs:
        if not os.path.exists(db):
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            for (mid, sid, tcs, ts, model, title) in c.execute(
                    "SELECT m.id, m.session_id, m.tool_calls, m.timestamp, s.model, s.title "
                    "FROM messages m LEFT JOIN sessions s ON s.id = m.session_id "
                    "WHERE m.role='assistant' AND m.tool_calls IS NOT NULL "
                    "  AND m.tool_calls != '' ORDER BY m.id DESC LIMIT ?", (limit,)).fetchall():
                try:
                    parsed = json.loads(tcs)
                except (TypeError, ValueError):
                    continue
                if not isinstance(parsed, list):
                    continue
                for tc in parsed:
                    cid = tc.get("id") or tc.get("call_id")
                    fn = (tc.get("function") or {}).get("name") or tc.get("name") or "?"
                    args = (tc.get("function") or {}).get("arguments", tc.get("arguments", ""))
                    result = ""
                    if cid:
                        r = c.execute("SELECT content FROM messages WHERE tool_call_id=? "
                                      "AND role='tool' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
                        if r:
                            result = r[0] or ""
                    prof[profile] = prof.get(profile, 0) + 1
                    calls.append({
                        "id": mid, "ts": ts, "profile": profile, "tool": fn,
                        "args": _short(args, 600), "result": _short(result, 700),
                        "session": sid, "model": model or "", "title": title or "",
                    })
        except sqlite3.Error:
            continue
        finally:
            c.close()
    calls.sort(key=lambda x: x["ts"] or 0, reverse=True)
    return {"calls": calls[:limit], "profiles": prof, "ts": time.time()}


def _short(s, n):
    """Trim for display without pretending it was that short."""
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + f"… [+{len(s)-n} chars]"


def task(task_id):
    """Read a Kanban task back: status plus the worker's own summary.

    /say returns a task id and then nothing ever read the result, so a voice
    request produced silence. This is that missing return leg, so Voice Core can
    wait for the answer and speak it instead of guessing.
    """
    task_id = (task_id or "").strip()
    if not re.fullmatch(r"t_[0-9a-f]{4,32}", task_id):
        return {"ok": False, "error": "bad task id"}
    out = subprocess.run(["hermes", "kanban", "show", task_id],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return {"ok": False, "error": (out.stderr or out.stdout).strip()[:200]}
    text = out.stdout
    status = ""
    m = re.search(r"^\s*status:\s*(\S+)", text, re.M)
    if m:
        status = m.group(1)
    # The completed event carries the authoritative summary; prefer it over the
    # "Latest summary" block, which can lag or be truncated in a long listing.
    summary = ""
    ms = re.search(r"'summary':\s*\"(.*?)\"\s*(?:,|\})", text, re.S)
    if ms:
        summary = ms.group(1)
    if not summary:
        m2 = re.search(r"Latest summary:\s*\n(.+)", text)
        if m2:
            summary = m2.group(1).strip()
    return {"ok": True, "task": task_id, "status": status or "unknown",
            "summary": summary.strip()[:1200]}


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
# ponytail: 2 threads, NOT nproc, and that is deliberate. This box is 4 cores sitting
# at load ~9 (desktop app). Same 30s clip, measured through /stt on this machine:
#     -t 4 (whisper's default) -> 100.0s   0.30x realtime
#     -t 3                      ->  28.1s   1.07x
#     -t 2                      ->  21.5s   1.40x   <-- this
#     -t 1                      ->  34.4s   0.87x
# Oversubscribing the cores is ~5x SLOWER. It reads like a bug; it is not. Re-measure
# before changing it, and only if the box stops being busy.
STT_THREADS = "2"
# ponytail: 2 threads, NOT nproc, and that is deliberate. This box is 4 cores sitting
# at load ~9 (desktop app). Same 30s clip, measured through /stt on this machine:
#     -t 4 (whisper's default) -> 100.0s   0.30x realtime
#     -t 3                      ->  28.1s   1.07x
#     -t 2                      ->  21.5s   1.40x   <-- this
#     -t 1                      ->  34.4s   0.87x
# Oversubscribing the cores is ~5x SLOWER. It reads like a bug; it is not. Re-measure
# before changing it, and only if the box stops being busy.


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


TTS_DIR = os.path.expanduser("~/.local/share/helix-tts")
EDGE_TTS = os.path.expanduser("~/.local/share/helix-tts-venv/bin/edge-tts")
PIPER = os.path.join(TTS_DIR, "piper", "piper")
PIPER_LIB = os.path.join(TTS_DIR, "piper")
PIPER_MODEL = os.path.join(TTS_DIR, "voices", "en_GB-alan-medium.onnx")
# Gemini Live TTS tier. google-genai lives in its own venv, so this shells out
# exactly like every other tier; the key is read there, never in this process.
# A LIST, not a joined string: _render hands argv straight to subprocess, so a
# space-joined path would be exec'd as one filename with a space in it. That is
# why the Live tier failed silently and the chain fell through to edge-tts.
LIVE_TTS = ["/home/abubakargcx/.local/share/helix-live-venv/bin/python",
            "/home/abubakargcx/helix/tools/live_tts.py"]
ESPEAK = "espeak-ng"

# Male voices throughout, per the human's preference for a deep voice. The previous
# set was female (Sonia/Alba) on both engines, so offline speech was female too --
# switching only edge would have left Piper disagreeing with it.
# edge-tts ids are Microsoft's; Piper's is a British male model. Both free, no key.
# Deeper alternatives if Thomas ever sounds too flat: en-GB-RyanNeural (younger),
# en_US-ChristopherNeural (news-reader authority), en_GB-northern_english_male-medium.
TTS_VOICES = {
    "edge":   {"en": "en-GB-ThomasNeural", "ur": "ur-PK-AsadNeural"},
    "piper":  {"en": "en_GB-alan-medium",  "ur": None},          # no Urdu model
    "espeak": {"en": "en-gb",              "ur": "ur"},
}


def _render(label, build, ext, mime, stdin=None, env=None, timeout=60):
    """Run a TTS binary to a temp file and return (bytes, mime, error).

    `build(out_path)` returns the argv, so the path the binary writes to and the
    path this function checks are always the same one. One helper instead of three
    near-identical functions: temp file, subprocess, "did it really produce audio"
    guard, read, unlink.
    """
    out = tempfile.mktemp(suffix=ext)
    try:
        p = subprocess.run(build(out), input=stdin, capture_output=True,
                           text=bool(stdin), timeout=timeout, env=env)
        if p.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) < 512:
            # `text=bool(stdin)` makes subprocess return str, not bytes. Calling
            # .decode() on it raised AttributeError and killed the whole request
            # instead of falling through to the next tier - i.e. a tier failing
            # produced a dead connection, not a fallback. Latent until the Live
            # tier became the first one to pass stdin.
            err = p.stderr if isinstance(p.stderr, str) else \
                (p.stderr or b"").decode(errors="replace")
            return None, mime, f"{label}: " + ((err or "").strip()[-120:] or "no audio")
        with open(out, "rb") as f:
            return f.read(), mime, None
    except subprocess.TimeoutExpired:
        return None, mime, f"{label} timed out"
    except OSError as e:
        return None, mime, f"{label}: {e}"
    finally:
        if os.path.exists(out):
            os.unlink(out)


def _voice(engine, lang):
    return TTS_VOICES[engine].get(lang) or TTS_VOICES[engine]["en"]


def _tts_live(text, lang):
    """Tier 0: Gemini Live, one text turn -> one WAV. Stateless, unlike the
    panel's persistent session. Shells out because google-genai lives in its own
    venv; every other tier is a local binary and this is not."""
    return _render("live-tts",
                   lambda out: [*LIVE_TTS, "--lang", lang, "--out", out],
                   ".wav", "audio/wav", stdin=text, timeout=120)


def _tts_edge(text, lang):
    """Online primary: Microsoft's free Edge read-aloud endpoint. No key needed."""
    if not os.path.isfile(EDGE_TTS):
        return None, None, "edge-tts not installed"
    return _render("edge-tts",
                   lambda o: [EDGE_TTS, "--voice", _voice("edge", lang), "--text", text,
                              "--write-media", o],
                   ".mp3", "audio/mpeg", timeout=45)


def _tts_piper(text, lang):
    """Offline fallback: fully local neural TTS. Measured 0.26x realtime on this box."""
    if not (os.path.isfile(PIPER) and os.path.isfile(PIPER_MODEL)):
        return None, None, "piper not installed"
    if not TTS_VOICES["piper"].get(lang):
        return None, None, f"no piper voice for '{lang}'"
    return _render("piper",
                   lambda o: [PIPER, "--model", PIPER_MODEL, "--output_file", o],
                   ".wav", "audio/wav", stdin=text,
                   env=dict(os.environ, LD_LIBRARY_PATH=PIPER_LIB), timeout=90)


def _tts_espeak(text, lang):
    """Last resort, always available, robotic. Never let the panel go mute."""
    return _render("espeak",
                   lambda o: [ESPEAK, "-v", _voice("espeak", lang), "-s", "165", "-w", o, text],
                   ".wav", "audio/wav", timeout=60)


def tts(text, lang="en", engine="auto"):
    """Online-first TTS with a local fallback.

    Returns (audio_bytes, mime, engine_used, error, skipped). `skipped` names
    the tiers that failed before the one that worked. Tried in order until one
    produces **real audio** — a tier that returns nothing is a failure, never a
    silent success, which is the bug class this project keeps getting bitten by.

      1. live-tts  — Gemini Live, best quality; sends text to Google (see §5.3)
      2. edge-tts  — online, free, no key, natural quality
      3. piper     — offline neural TTS, British male voice
      4. espeak-ng — offline, robotic, but cannot fail on a normal install

    A chain, not a switch: Live being unreachable or rate-limited must still
    leave three working tiers behind it.
    """
    text = (text or "").strip()
    if not text:
        return None, None, None, "empty text", ""
    if len(text) > 2000:
        return None, None, None, "text too long", ""
    if not re.fullmatch(r"[a-z]{2,3}(-[a-z]{2,4})?", lang or ""):
        return None, None, None, "bad lang", ""
    lang = lang.split("-")[0]

    order = {
        "auto":  [("live-tts", _tts_live), ("edge-tts", _tts_edge),
                  ("piper", _tts_piper), ("espeak", _tts_espeak)],
        "live":  [("live-tts", _tts_live), ("edge-tts", _tts_edge),
                  ("piper", _tts_piper), ("espeak", _tts_espeak)],
        "edge":  [("edge-tts", _tts_edge), ("piper", _tts_piper),
                  ("espeak", _tts_espeak)],
        "piper": [("piper", _tts_piper), ("espeak", _tts_espeak)],
    }.get(engine or "auto")
    if not order:
        return None, None, None, "bad engine", ""

    tried, skipped = [], []
    for name, fn in order:
        audio, mime, err = fn(text, lang)
        if audio:
            # Surface what was skipped. A silent fallback is indistinguishable
            # from success, which is the bug class that keeps recurring here.
            # This is the 5th slot, NOT `err` - `err` must stay falsy on success
            # or every good response turns into a 400.
            return audio, mime, name, None, "; ".join(skipped)[:300]
        reason = (err or "no audio").replace("\n", " ")[:90]
        tried.append(f"{name}: {reason}")
        skipped.append(f"{name}={reason}")
    return None, None, None, "; ".join(tried)[:300], ""


def _selftest():
    """Check the status-derivation rules against a throwaway DB. No Hermes involved."""
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
        if path == "/task":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send_json(task((qs.get("id") or [""])[0]))
        if path == "/headlines":
            return self._send_json(headlines())
        if path == "/cortex":
            return self._send_json(cortex())
        if path == "/activity":
            return self._send_json(activity())
        if path == "/cam":
            return camera_stream(self)
        if path == "/cam/stop":
            _cam_kill_all()
            return self._send_json({"ok": True, "stopped": True})
        if path == "/events":
            return self._sse()
        self.send_error(404)

    def do_POST(self):
        # /say is a WRITE path (it creates a Kanban task), unlike everything else
        # here. Any page in any browser can POST to 127.0.0.1, so requiring a custom
        # header forces a CORS preflight that we never answer — a foreign page then
        # cannot make the browser send it.
        if self.path.split("?", 1)[0] not in ("/say", "/stt", "/tts"):
            return self.send_error(404)
        if self.headers.get("X-Helix-Voice") != "1":
            return self._send_json({"ok": False, "error": "missing X-Helix-Voice header"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        is_stt = self.path.split("?", 1)[0] == "/stt"
        is_tts = self.path.split("?", 1)[0] == "/tts"
        # Different caps: a JSON note is tiny, audio is not. A single 64KB cap on
        # both capped /stt at ~2 seconds of audio, which looked like "STT broken".
        limit = 64_000 if not is_stt else MAX_AUDIO
        if n > limit:
            return self._send_json({"ok": False, "error": "body too large"}, 413)
        lang = ""
        if "?" in self.path:
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            lang = (q.get("lang") or [""])[0]
        body = self.rfile.read(n) if n else b""
        try:
            if is_tts:
                try:
                    payload = json.loads(body or b"{}")
                except ValueError:
                    return self._send_json({"ok": False, "error": "bad json"}, 400)
                audio, mime, used, err, skipped = tts(payload.get("text"),
                                                      payload.get("lang") or "en",
                                                      payload.get("engine") or "auto")
                if err:
                    return self._send_json({"ok": False, "error": err}, 400)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("X-Helix-TTS-Engine", used)
                # A fallback that happens silently looks exactly like success.
                if skipped:
                    self.send_header("X-Helix-TTS-Skipped", skipped)
                self.send_header("X-Helix-TTS-Lang", payload.get("lang") or "en")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
                return
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
