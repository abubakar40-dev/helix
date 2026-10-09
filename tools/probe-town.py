#!/usr/bin/env python3
"""Probe the live Agent Town canvas: pixel stats + an ASCII luminance map.

Rendering the room brighter in the palette did not make it look brighter on
screen, which means either the canvas is not painting what I think it is, or
something is compositing over it. Measure instead of squinting at a PNG.

Usage: probe-town.py <cdp-port> [url]
"""
import base64
import json
import socket
import struct
import sys
import time
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9411
URL = sys.argv[2] if len(sys.argv) > 2 else ""

EXPR = r"""(() => {
 const c = document.querySelector('#townCanvas');
 if (!c) return {error: 'no canvas'};
 const g = c.getContext('2d');
 const d = g.getImageData(0, 0, c.width, c.height).data;
 let sum=0, n=0, bright=0, mid=0;
 const hist = {};
 for (let k=0; k<d.length; k+=4){
   const v=(d[k]+d[k+1]+d[k+2])/3; sum+=v; n++;
   if (v>90) bright++; else if (v>45) mid++;
   const key=d[k]+','+d[k+1]+','+d[k+2]; hist[key]=(hist[key]||0)+1;
 }
 const top=Object.entries(hist).sort((a,b)=>b[1]-a[1]).slice(0,8);
 // ASCII map so the layout of the room is readable as text
 const W=c.width,H=c.height,cols=76,rows=30,ramp=" .:-=+*#%@";
 let map='';
 for(let ry=0;ry<rows;ry++){
   let line='';
   for(let cx=0;cx<cols;cx++){
     const x=Math.floor(cx*W/cols), y=Math.floor(ry*H/rows);
     let s=0,m=0;
     for(let dy=0;dy<H/rows;dy+=3) for(let dx=0;dx<W/cols;dx+=3){
       const k=((Math.min(H-1,y+dy|0))*W+(Math.min(W-1,x+dx|0)))*4;
       s+=(d[k]+d[k+1]+d[k+2])/3; m++;
     }
     line+=ramp[Math.min(9,Math.floor(s/m/255*10))];
   }
   map+=line+'\n';
 }
 return {size:W+'x'+H, css:c.style.width+'x'+c.style.height,
   meanLuma:Math.round(sum/n), brightPct:+(bright/n*100).toFixed(1),
   midPct:+(mid/n*100).toFixed(1), distinct:Object.keys(hist).length,
   topColors:top, map};
})()"""


def connect():
    tabs = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/json/list", timeout=10))
    pg = [t for t in tabs if t.get("type") == "page"
          and "console" in (t.get("url") or "")]
    if not pg:
        raise SystemExit("no console tab")
    ws = pg[0]["webSocketDebuggerUrl"].split("://", 1)[1]
    hp, path = ws.split("/", 1)
    host, port = hp.split(":")
    s = socket.create_connection((host, int(port)), timeout=30)
    s.settimeout(90)
    key = base64.b64encode(b"0123456789abcdef").decode()
    s.send(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hp, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    return s


def make_cmd(s):
    seq = [0]

    def read_exact(n):
        out = b""
        while len(out) < n:
            ch = s.recv(n - len(out))
            if not ch:
                raise ConnectionError("closed")
            out += ch
        return out

    def frame():
        b0, b1 = read_exact(2)
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", read_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", read_exact(8))[0]
        m = read_exact(4) if b1 & 0x80 else None
        pay = read_exact(ln) if ln else b""
        if m:
            pay = bytes(c ^ m[i % 4] for i, c in enumerate(pay))
        return pay

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
        end = time.time() + 90
        while time.time() < end:
            try:
                d = json.loads(frame())
            except Exception:
                continue
            if d.get("id") == seq[0]:
                return d.get("result", d.get("error"))
        return None

    return cmd


def main():
    s = connect()
    cmd = make_cmd(s)
    if URL:
        cmd("Page.enable")
        cmd("Network.enable")
        cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        cmd("Page.navigate", {"url": URL})
        time.sleep(14)
    res = cmd("Runtime.evaluate", {"expression": EXPR, "returnByValue": True})
    v = (res or {}).get("result", {}).get("value")
    if not v:
        print(json.dumps({"error": "evaluate failed"}))
        return 1
    print("canvas     : %s (css %s)" % (v["size"], v["css"]))
    print("mean luma  : %s" % v["meanLuma"])
    print("bright >90 : %s%%" % v["brightPct"])
    print("mid   >45  : %s%%" % v["midPct"])
    print("colours    : %s" % v["distinct"])
    print("top        : %s" % ", ".join("%s x%d" % (c, n) for c, n in v["topColors"]))
    print("\n" + v["map"])
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())