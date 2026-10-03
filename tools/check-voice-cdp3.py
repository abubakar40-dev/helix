#!/usr/bin/env python3
"""Does the PAGE's own playOut() actually schedule audio? (third CDP pass)

Pass 1 falsified my suspended-AudioContext theory: getUserMedia reported
ok/state=live, link said "live", audio said "running", and 3.3 MB of binary
audio arrived. So the remaining question is narrow and answerable: does the
page's own playOut() reach createBufferSource().start() without throwing?

We monkey-patch createBufferSource to count calls and catch anything start()
throws, then read the page's own log panel.
"""
import asyncio, json, subprocess, sys, time, urllib.request

CHROME, PORT, PROF = "/usr/bin/google-chrome", 9336, "/tmp/helix-cdp4"
URL = "http://127.0.0.1:8787/voice-core/?cdp=4"

PROBE = """
window.__plays = 0; window.__errs = [];
(function(){
  var AC = window.AudioContext;
  var proto = AC.prototype;
  var orig = proto.createBufferSource;
  proto.createBufferSource = function(){
    window.__plays++;
    var s = orig.apply(this, arguments);
    var start = s.start.bind(s);
    s.start = function(){
      try { return start.apply(s, arguments); }
      catch(e){ window.__errs.push('start: '+e); throw e; }
    };
    return s;
  };
  var od = proto.decodeAudioData;
  proto.decodeAudioData = function(){
    window.__decode = (window.__decode||0) + 1;
    return od.apply(this, arguments);
  };
  return 'patched';
})()
"""

READ = """
(function(){
  function t(id){ var e=document.getElementById(id); return e ? e.textContent : null; }
  return JSON.stringify({
    plays: window.__plays, errs: window.__errs.slice(0,5),
    decodeCalls: window.__decode || 0,
    link: t('linkstate'), audio: t('audioctx'), state: t('state'),
    heard: t('final'), said: t('answer'),
    logs: Array.prototype.map.call(document.querySelectorAll('#log .l'),
      function(d){ return d.textContent.trim(); }).slice(0,10)
  });
})()
"""


def targets():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
        return json.load(r)


async def main():
    proc = subprocess.Popen([
        CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROF}", "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required", "--no-sandbox",
        "--disable-gpu", URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tgt = None
    for _ in range(50):
        time.sleep(0.5)
        try:
            tgt = next((t for t in targets()
                        if t.get("type") == "page" and "voice-core" in t.get("url", "")), None)
        except Exception:
            pass
        if tgt:
            break
    if not tgt:
        print("  FAIL: page never appeared"); proc.terminate(); return 2
    print("  page up")

    from websockets.asyncio.client import connect
    try:
        async with connect(tgt["webSocketDebuggerUrl"], max_size=None) as ws:
            n = [0]
            async def js(expr):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": "Runtime.evaluate",
                                          "params": {"expression": expr,
                                                     "returnByValue": True}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == n[0]:
                        r = m.get("result", {})
                        if "exceptionDetails" in r:
                            return "EXC:" + json.dumps(r["exceptionDetails"])[:300]
                        v = r.get("result", {}).get("value")
                        return v if v is not None else "NULL:" + json.dumps(r)[:300]

            await js(PROBE)
            print("  instrumented createBufferSource")
            await asyncio.sleep(7)
            print(f"  link now: {json.loads(await js(READ)).get('link')}")

            await js("document.getElementById('micbar').click()")
            await asyncio.sleep(1)
            print("  awake; speaking the recording aloud for the page's own mic…")
            p = subprocess.Popen(["aplay", "-q", "-D", "default",
                                  "/home/abubakargcx/easy-diffusion/tmp/opencode/voice.wav"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(18)
            p.terminate()
            await asyncio.sleep(2)

            r = json.loads(await js(READ))
            print(f"\n  createBufferSource calls : {r['plays']}")
            print(f"  decodeAudioData calls    : {r['decodeCalls']}  (should be 0 now)")
            print(f"  start() errors           : {r['errs']}")
            print(f"  AudioContext             : {r['audio']}   state: {r['state']}")
            print(f"  heard: {str(r['heard'])[:88]!r}")
            print(f"  said:  {str(r['said'])[:88]!r}")
            print("  page log:")
            for l in r["logs"]:
                print(f"    {l[:112]}")
            ok = r["plays"] > 0 and not r["errs"]
            print(f"\n  VERDICT: {'PASS - the page schedules playback' if ok else 'FAIL - no playback attempted'}")
            return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


sys.exit(asyncio.run(main()))