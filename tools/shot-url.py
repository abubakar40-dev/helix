#!/usr/bin/env python3
"""Screenshot a URL at an exact size, via CDP.

Used to test globe URL variants (hud visibility, scene framing) at the real panel
size instead of guessing from source.

Usage: shot-url.py <cdp-port> <url> <out.png> [width] [height] [settle-seconds]
"""
import base64
import json
import socket
import struct
import sys
import time
import urllib.request

PORT = int(sys.argv[1])
URL = sys.argv[2]
OUT = sys.argv[3]
W = int(sys.argv[4]) if len(sys.argv) > 4 else 284
H = int(sys.argv[5]) if len(sys.argv) > 5 else 427
SETTLE = float(sys.argv[6]) if len(sys.argv) > 6 else 22


def main():
    tabs = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/json/list", timeout=10))
    page = [t for t in tabs if t.get("type") == "page"][0]
    ws = page[0] if isinstance(page, tuple) else page
    url = ws["webSocketDebuggerUrl"].split("://", 1)[1]
    hp, path = url.split("/", 1)
    host, port = hp.split(":")
    s = socket.create_connection((host, int(port)), timeout=30)
    s.settimeout(120)
    key = base64.b64encode(b"0123456789abcdef").decode()
    s.send(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hp, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    seq = [0]

    def rd(n):
        out = b""
        while len(out) < n:
            c = s.recv(n - len(out))
            if not c:
                raise ConnectionError
            out += c
        return out

    def frame():
        b0, b1 = rd(2)
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", rd(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", rd(8))[0]
        m = rd(4) if b1 & 0x80 else None
        p = rd(ln) if ln else b""
        if m:
            p = bytes(c ^ m[i % 4] for i, c in enumerate(p))
        return p

    def cmd(method, params=None):
        seq[0] += 1
        p = json.dumps({"id": seq[0], "method": method,
                        "params": params or {}}).encode()
        n = len(p)
        h = b"\x81"
        if n < 126:
            h += bytes([0x80 | n])
        elif n < 65536:
            h += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            h += bytes([0x80 | 127]) + struct.pack(">Q", n)
        s.send(h + b"\x00\x00\x00\x00" + p)
        end = time.time() + 120
        while time.time() < end:
            try:
                d = json.loads(frame())
            except Exception:
                continue
            if d.get("id") == seq[0]:
                return d.get("result", d.get("error"))
        return None

    cmd("Emulation.setDeviceMetricsOverride",
        {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})
    cmd("Page.enable")
    cmd("Runtime.enable")
    cmd("Page.navigate", {"url": URL})
    time.sleep(SETTLE)
    res = cmd("Runtime.evaluate", {"expression": """(() => ({
        url: location.href,
        canvases: document.querySelectorAll('canvas').length,
        hud: !!document.querySelector('#hud, .hud, [class*=hud]'),
        visibleText: (document.body.innerText||'').slice(0, 300).replace(/\\s+/g,' ')
    }))()""", "returnByValue": True})
    v = (res or {}).get("result", {}).get("value")
    print(json.dumps(v, indent=1) if v else "(evaluate failed)")
    shot = cmd("Page.captureScreenshot", {"format": "png"})
    if shot and "data" in shot:
        with open(OUT, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print("wrote", OUT)
    s.close()


if __name__ == "__main__":
    main()