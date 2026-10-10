#!/usr/bin/env python3
"""Measure the HELIX console against the reference layout at 1509x812.

Drives a headless Brave over CDP. stdlib only.
Usage: measure.py <cdp-port>
"""
import base64
import json
import socket
import struct
import sys
import time
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9336
W, H = 1509, 812

TARGET = {
    "titlebar":    (0, 0, 1509, 35),
    "left":        (12, 47, 316, 752),
    "town":        (340, 366, 643, 433),
    "right":       (995, 47, 504, 752),
    "cam":         (28, 62, 284, 137),
    "globe":       (28, 211, 284, 427),
    "hl":          (28, 652, 284, 134),
    "nodes":       (358, 88, 142, 219),
    "orb":         (653, 54, 299, 286),
    "composer":    (1010, 738, 474, 47),
    "consoleHead": (995, 47, 504, 47),
}

EXPR = """(() => { const W=innerWidth,H=innerHeight;
 const q=s=>{const e=document.querySelector(s); if(!e)return null;
   const r=e.getBoundingClientRect();
   return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)};};
 const ov=[];
 document.querySelectorAll('#top,main,#status,.panel,#orbwrap,#globe,#townCanvas,#composer,#headlines,#nodes')
  .forEach(el=>{const r=el.getBoundingClientRect();
   if(r.right>W+1||r.bottom>H+1)
     ov.push((el.id||el.className)+' r='+Math.round(r.right)+' b='+Math.round(r.bottom));});
 return {viewport:W+'x'+H,
   spec:{titlebar:q('#top'),left:q('#left'),centre:q('#top'),town:q('#town'),
     right:q('#console'),cam:q('#camPanel'),globe:q('#globePanel'),hl:q('#hlPanel'),
     nodes:q('#nodes'),orb:q('#orbwrap'),composer:q('#composer'),
     consoleHead:q('#console .ph')},
   overflow:ov}; })()"""


def main():
    tabs = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/json/list", timeout=10))
    # Any page will do: the target is navigated below. Filtering on "console"
    # made the tool fail whenever the browser happened to be left on the globe.
    page = [t for t in tabs if t.get("type") == "page"]
    if not page:
        print(json.dumps({"error": "no page target"}))
        return 1
    ws = page[0]["webSocketDebuggerUrl"].split("://", 1)[1]
    hostport, path = ws.split("/", 1)
    host, port = hostport.split(":")
    s = socket.create_connection((host, int(port)), timeout=25)
    s.settimeout(90)
    key = base64.b64encode(b"0123456789abcdef").decode()
    s.send(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)

    def read_exact(n):
        out = b""
        while len(out) < n:
            chunk = s.recv(n - len(out))
            if not chunk:
                raise ConnectionError("socket closed")
            out += chunk
        return out

    def recv_frame():
        """Read one WebSocket frame (server->client frames are NOT masked).

        Parsing the frame header is mandatory: a raw recv() returns b"\x81J{...}"
        and json.loads() on that always raises, so a naive loop discards every
        response and then reports a bogus timeout. That bug cost several rounds.
        """
        b0, b1 = read_exact(2)
        opcode = b0 & 0x0F
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", read_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", read_exact(8))[0]
        masked = bool(b1 & 0x80)
        mask = read_exact(4) if masked else None
        payload = read_exact(ln) if ln else b""
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 0x8:
            raise ConnectionError("websocket closed by peer")
        return payload

    seq = [0]

    def cmd(method, params=None):
        seq[0] += 1
        payload = json.dumps({"id": seq[0], "method": method,
                              "params": params or {}}).encode()
        n = len(payload)
        head = b"\x81"
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        s.send(head + b"\x00\x00\x00\x00" + payload)
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                raw = recv_frame()
            except (socket.timeout, ConnectionError):
                return None
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("id") == seq[0]:
                return d.get("result", d.get("error"))
        return None

    # No Emulation override: it hung and desynced the socket on this Brave
    # build. The window is launched at the right size instead, and the
    # measurement asserts on innerWidth/innerHeight so a wrong size cannot pass.
    #
    # Navigate rather than reload: snap Brave runs a single instance and reuses
    # the first debug port, so a "fresh" browser cannot be launched. Reload also
    # throws away the execution context this socket is attached to.
    # Frame parsing was broken earlier, which is almost certainly why this
    # command appeared to hang. Retry it now: it is the only way to hit an exact
    # 812px viewport on a window that reports 675.
    emu = cmd("Emulation.setDeviceMetricsOverride",
              {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})
    print("emulation:", "ok" if emu else "unavailable")
    url = sys.argv[2] if len(sys.argv) > 2 else ""
    if url:
        cmd("Page.enable")
        cmd("Network.enable")
        cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        cmd("Page.navigate", {"url": url})
        time.sleep(14)
    res = cmd("Runtime.evaluate", {"expression": EXPR, "returnByValue": True})
    val = (res or {}).get("result", {}).get("value") if res else None
    if not val:
        print(json.dumps({"error": "evaluate failed", "raw": res}))
        s.close()
        return 1

    print("%-13s %-19s %-19s %s" % ("element", "TARGET x,y,w,h",
                                    "ACTUAL x,y,w,h", "delta"))
    print("-" * 82)
    worst = 0
    for key, (tx, ty, tw, th) in TARGET.items():
        a = val["spec"].get(key)
        if not a:
            print("%-13s MISSING" % key)
            continue
        d = (a["x"] - tx, a["y"] - ty, a["w"] - tw, a["h"] - th)
        worst = max(worst, max(abs(v) for v in d))
        print("%-13s %-19s %-19s dx=%+4d dy=%+4d dw=%+4d dh=%+4d"
              % (key, "%d,%d,%d,%d" % (tx, ty, tw, th),
                 "%d,%d,%d,%d" % (a["x"], a["y"], a["w"], a["h"]), *d))
    print("\nviewport      : %s" % val["viewport"])
    print("worst delta   : %dpx" % worst)
    print("overflow      : %s" % (val["overflow"] or "none"))
    shot = cmd("Page.captureScreenshot", {"format": "png"})
    if shot and "data" in shot:
        with open("/tmp/helix-verify.png", "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print("screenshot    : /tmp/helix-verify.png")
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())