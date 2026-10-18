#!/usr/bin/env python3
"""Audit the GEOMETRY of the reference UI prototype.

Dimensions only. This deliberately does not read, copy or reproduce any of the
prototype's markup, styling or content - it drives the running page and reports
the bounding box of each block so HELIX can be sized to match.

Usage: audit-geometry.py <cdp-port> <url> [width] [height]
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
W = int(sys.argv[3]) if len(sys.argv) > 3 else 1509
H = int(sys.argv[4]) if len(sys.argv) > 4 else 812

# (label, selector) - labels describe POSITION IN THE LAYOUT, not the prototype's
# class names, so nothing is carried across.
#
# PROFILES maps a name to its selector list. Only geometry is ever read; the
# audit measures rendered boxes, it does not import or reproduce any markup.
PROFILES = {
    "reference": [
        ("app shell",        ".app-shell"),
        ("topbar",           ".topbar"),
        ("workspace",        ".workspace"),
        ("left column",      ".left-column"),
        ("center column",    ".center-column"),
        ("console panel",    ".console-panel"),
        ("media panel",      ".media-panel"),
        ("satellite panel",  ".satellite-panel"),
        ("headlines panel",  ".headlines-panel"),
        ("core panel",       ".core-panel"),
        ("town panel",       ".town-panel"),
        ("statusbar",        ".statusbar"),
        ("start button",     ".start-button"),
    ],
    "helix": [
        ("app shell",        "body"),
        ("topbar",           "#titlebar"),
        ("workspace",        "main"),
        ("left column",      "#left"),
        ("center column",    "#core"),
        ("console panel",    "#console"),
        ("media panel",      "#camPanel"),
        ("satellite panel",  "#globePanel"),
        ("headlines panel",  "#hlPanel"),
        ("core panel",       "#core"),
        ("town panel",       "#town"),
        ("statusbar",        "#status"),
        ("start button",     "#startbtn"),
    ],
}
PROFILE = sys.argv[5] if len(sys.argv) > 5 else "reference"
TARGETS = PROFILES[PROFILE]


def ws_connect(url):
    rest = url.split("://", 1)[1]
    hostport, path = rest.split("/", 1)
    host, port = hostport.split(":")
    s = socket.create_connection((host, int(port)), timeout=30)
    key = base64.b64encode(os.urandom(16)).decode()
    s.send(("GET /%s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    return s, buf.split(b"\r\n\r\n", 1)[1]


import os  # noqa: E402  (used by ws_connect)


def ws_send(s, obj):
    data = json.dumps(obj).encode()
    hdr = bytearray([0x81])
    n = len(data)
    mask = os.urandom(4)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
    hdr += mask
    hdr += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    s.send(bytes(hdr))


class WsClient:
    def __init__(self, url):
        self.s, self.pending = ws_connect(url)
        self.seq = 0

    def send(self, obj):
        ws_send(self.s, obj)

    def recv(self):
        def need(n):
            while len(self.pending) < n:
                chunk = self.s.recv(65536)
                if not chunk:
                    raise ConnectionError("websocket closed")
                self.pending += chunk
            out, self.pending = self.pending[:n], self.pending[n:]
            return out
        b0, b1 = need(2)
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", need(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", need(8))[0]
        return need(ln).decode(errors="replace")

    def cmd(self, method, params=None, wait=True):
        self.seq += 1
        msg_id = self.seq
        self.send({"id": msg_id, "method": method, "params": params or {}})
        if not wait:
            return None
        while True:
            d = json.loads(self.recv())
            if d.get("id") == msg_id:
                return d.get("result", {})


def main():
    tabs = json.load(urllib.request.urlopen(
        "http://127.0.0.1:%d/json/list" % PORT, timeout=10))
    pages = [t for t in tabs if t.get("type") == "page"]
    page = pages[0]
    ws_url = page["webSocketDebuggerUrl"]
    ws = WsClient(ws_url)

    ws.cmd("Page.enable")
    ws.cmd("Emulation.setDeviceMetricsOverride", {
        "width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})
    ws.cmd("Page.navigate", {"url": URL})
    time.sleep(14)   # vite dev + React mount + r3f canvas

    js = """
    (function(){
      var out = [];
      %s
      for (var i=0;i<%d;i++){
        var L = %s[i], sel = %s[i];
        var el = document.querySelector(sel);
        if(!el){ out.push({label:L, sel:sel, missing:true}); continue; }
        var r = el.getBoundingClientRect();
        var cs = getComputedStyle(el);
        out.push({label:L, sel:sel, x:Math.round(r.x), y:Math.round(r.y),
                  w:Math.round(r.width), h:Math.round(r.height),
                  gtc: cs.gridTemplateColumns, gtr: cs.gridTemplateRows,
                  pad: cs.padding, gap: cs.gap});
      }
      return JSON.stringify(out);
    })()
    """ % ("", len(TARGETS), json.dumps([t[0] for t in TARGETS]),
           json.dumps([t[1] for t in TARGETS]))

    res = ws.cmd("Runtime.evaluate", {"expression": js, "returnByValue": True})
    rows = json.loads(res["result"]["value"])
    print("  viewport %dx%d\n" % (W, H))
    print("  %-18s %6s %6s %6s %6s   %s" % ("block", "x", "y", "w", "h", "notes"))
    print("  " + "-" * 74)
    for r in rows:
        if r.get("missing"):
            print("  %-18s %s" % (r["label"], "NOT FOUND (" + r["sel"] + ")"))
            continue
        notes = []
        if r.get("gtc") and r["gtc"] != "none":
            notes.append("cols=" + r["gtc"])
        if r.get("gtr") and r["gtr"] != "none":
            notes.append("rows=" + r["gtr"])
        if r.get("pad") and r["pad"] not in ("0px", ""):
            notes.append("pad=" + r["pad"])
        if r.get("gap") and r["gap"] not in ("normal", "0px", ""):
            notes.append("gap=" + r["gap"])
        print("  %-18s %6s %6s %6s %6s   %s"
              % (r["label"], r["x"], r["y"], r["w"], r["h"], " ".join(notes)[:38]))


if __name__ == "__main__":
    main()
