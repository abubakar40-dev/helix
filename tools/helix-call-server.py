#!/usr/bin/env python3
"""HELIX call service - signaling, TURN config, PWA host, transcript intake.

Runs on the free OCI E2.1.Micro (954 MB), so: one dependency (aiohttp), no
database, no build step. Every completed call is written to calls/<id>.json and
pulled by the laptop's helix-call-memory.py into HELIX's shared state.db.

Media is WebRTC; this process only brokers SDP/ICE and hands out TURN
credentials. It never touches audio.
"""
import asyncio, json, os, re, secrets, time

from aiohttp import web, WSMsgType

HERE = os.path.dirname(os.path.abspath(__file__))
CALLS = os.path.join(HERE, "calls")
TASKS = os.path.join(HERE, "tasks")
WEB = os.path.join(HERE, "helix-call-web") if os.path.isdir(os.path.join(HERE, "helix-call-web")) else os.path.join(HERE, "web")
MEMORY = os.path.join(HERE, "memory.json")
os.makedirs(CALLS, exist_ok=True)
os.makedirs(TASKS, exist_ok=True)


def queue_task(args):
    """A voice-assigned task waits here until the laptop turns it into a kanban task.

    The kanban lives on the laptop, not here, so the box records the request and
    the laptop drains it. Queued rather than dropped: the laptop may be asleep.
    """
    rec = dict(args, queued_at=time.time())
    with open(os.path.join(TASKS, f"{secrets.token_hex(4)}.json"), "w") as f:
        json.dump(rec, f)
    log_line(f"[task] queued '{args.get('title','')}' -> {args.get('assignee','?')}")
    return rec


def recall_local(query, limit=5):
    """Answer a recall tool call from the memory the laptop has synced down."""
    try:
        with open(MEMORY) as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return "shared memory has not been synced to the box yet"
    q = (query or "").lower()
    hits = [e for e in entries if q in json.dumps(e).lower()][:limit]
    if not hits:
        return "nothing in shared memory matches that"
    return "; ".join(f"[{h.get('when','?')}] {h.get('text','')[:160]}" for h in hits)


def log_line(m):
    print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

PUBLIC_IP = os.environ.get("HELIX_PUBLIC_IP", "127.0.0.1")
HOSTNAME = os.environ.get("HELIX_HOSTNAME", PUBLIC_IP)

rooms = {}          # call_id -> set of websocket
pending = {}        # call_id -> list of queued messages for a peer not yet joined
TURN_USER, TURN_PASS = "helix", ""


def load_turn_credentials():
    """Read the same credentials coturn uses; never duplicated or invented."""
    global TURN_USER, TURN_PASS
    path = "/etc/turnserver.conf"
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r"^user=([^:]+):(.+)$", line.strip())
                if m:
                    TURN_USER, TURN_PASS = m.group(1), m.group(2)
                    return
    except OSError:
        pass
    TURN_USER, TURN_PASS = "helix", ""


async def ws_handler(request):
    call_id = request.match_info["call_id"]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    peers = rooms.setdefault(call_id, set())
    is_first = not peers
    peers.add(ws)
    request.app["log"](f"[ws] {call_id} joined ({len(peers)} peers)")

    # a peer arriving late still needs whatever it missed
    for msg in pending.get(call_id, []):
        await ws.send_str(msg)

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            data = msg.data
            if is_first and len(peers) == 1:
                pending[call_id] = []          # now drained, stop replaying
            for peer in peers:
                if peer is not ws and not peer.closed:
                    await peer.send_str(data)
        elif msg.type == WSMsgType.ERROR:
            break

    peers.discard(ws)
    request.app["log"](f"[ws] {call_id} left ({len(peers)} peers)")
    if not peers:
        rooms.pop(call_id, None)
        pending.pop(call_id, None)
    return ws


async def turn_handler(request):
    """ICE servers. STUN is free, TURN needs auth and only matters on hard NATs."""
    cfg = [{"urls": ["stun:stun.l.google.com:19302", f"stun:{HOSTNAME}:3478"]}]
    if TURN_PASS:
        cfg.append({"urls": f"turn:{HOSTNAME}:3478?transport=udp",
                    "username": TURN_USER, "credential": TURN_PASS})
    return web.json_response({"iceServers": cfg, "callId": request.query.get("call", "")})


async def transcript_handler(request):
    """Browser posts the finished call here; it is the durable record."""
    call_id = request.match_info["call_id"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="expected JSON")
    if not isinstance(body, dict) or "turns" not in body:
        raise web.HTTPBadRequest(text="expected {call_id, started_at, turns}")
    body.setdefault("call_id", call_id)
    body.setdefault("started_at", time.time())
    path = os.path.join(CALLS, f"{secrets.token_hex(6)}.json")
    with open(path, "w") as f:
        json.dump(body, f)
    request.app["log"](f"[call] stored {len(body['turns'])} turns -> {os.path.basename(path)}")
    return web.json_response({"ok": True, "turns": len(body["turns"])})


async def calls_handler(request):
    """The laptop pulls these to fold calls into shared memory."""
    out = []
    for name in sorted(os.listdir(CALLS)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(CALLS, name)) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return web.json_response({"calls": out})


async def voice_handler(request):
    """One call: browser PCM audio in, Gemini Live out, transcript recorded."""
    import sys
    sys.path.insert(0, HERE)
    from aiohttp import WSMsgType as T
    import helix_voice

    call_id = request.match_info["call_id"]
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    log_line(f"[voice] {call_id} connecting to {helix_voice.MODEL}")
    started = time.time()

    async def relay(msg):
        # audio frames browser->gemini, control messages browser->us
        if msg.type == T.BINARY:
            await ws.send_bytes(msg.data)

    try:
        turns = await helix_voice.run_voice(
            ws, None,
            on_task=queue_task,
            on_recall=recall_local,
        )
    except Exception as e:
        log_line(f"[voice] {call_id} ended: {type(e).__name__}: {e}")
        turns = []
    finally:
        if turns:
            path = os.path.join(CALLS, f"{secrets.token_hex(6)}.json")
            with open(path, "w") as f:
                json.dump({"call_id": call_id, "started_at": started,
                           "ended_at": time.time(),
                           # sessions.title is UNIQUE downstream, so it needs a stamp
                           "title": "Phone call " + time.strftime("%Y-%m-%d %H:%M",
                                                                 time.localtime(started)),
                           "turns": turns}, f)
            log_line(f"[call] {call_id}: stored {len(turns)} turns")
        try:
            await ws.close()
        except Exception:
            pass
    return ws


async def tasks_handler(request):
    """The laptop drains tasks the AI assigned by voice."""
    out = []
    for name in sorted(os.listdir(TASKS)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(TASKS, name)) as f:
                rec = json.load(f)
            rec["file"] = name
            out.append(rec)
        except (OSError, ValueError):
            continue
    return web.json_response({"tasks": out})


async def task_done(request):
    """Called by the laptop once a queued task really became a kanban task."""
    name = os.path.basename(request.match_info["name"])
    try:
        os.remove(os.path.join(TASKS, name))
        return web.json_response({"ok": True})
    except OSError:
        return web.json_response({"ok": False})


async def memory_handler(request):
    """Laptop pushes shared memory down so the AI can answer from it."""
    if request.method == "PUT":
        body = await request.json()
        with open(MEMORY, "w") as f:
            json.dump(body.get("entries", []), f)
        return web.json_response({"ok": True, "entries": len(body.get("entries", []))})
    entries = []
    try:
        with open(MEMORY) as f:
            entries = json.load(f)
    except (OSError, ValueError):
        pass
    return web.json_response({"entries": entries})


def make_app():
    load_turn_credentials()
    app = web.Application()
    app["log"] = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

    app.router.add_get("/", lambda r: web.FileResponse(os.path.join(WEB, "index.html")))
    app.router.add_get("/turn", turn_handler)
    app.router.add_get("/calls", calls_handler)
    app.router.add_post("/call/{call_id}/transcript", transcript_handler)
    app.router.add_get("/ws/{call_id}", ws_handler)
    app.router.add_get("/voice/{call_id}", voice_handler)
    app.router.add_get("/tasks", tasks_handler)
    app.router.add_get("/task-done/{name}", task_done)
    app.router.add_route("*", "/memory", memory_handler)
    app.router.add_static("/app/", WEB, show_index=False)

    async def index(req):
        return web.FileResponse(os.path.join(WEB, "index.html"))
    app.router.add_get("/index.html", index)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("HELIX_PORT", "8080"))
    web.run_app(make_app(), host="127.0.0.1", port=port)