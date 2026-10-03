#!/usr/bin/env python3
"""Clean-room check. NO monkey-patching at all.

The previous audit overrode the WebSocket `binaryType` setter without forwarding
it, so the real socket kept its default `"blob"`, `new Int16Array(blob)` produced
zero samples, and `createBuffer` threw — i.e. the failure I was "finding" was
manufactured by my own probe. This run patches nothing and trusts only what the
page reports about itself: its own log panel, its `#answer` text, and its
`audio out` readout.
"""
import asyncio, json, subprocess, sys, time, urllib.request

CHROME, PORT, PROF = "/usr/bin/google-chrome", 9341, "/tmp/helix-clean"
URL = "http://127.0.0.1:8787/voice-core/?clean=1"

READ = """
(function(){
  function t(id){ var e=document.getElementById(id); return e?e.textContent:null; }
  return JSON.stringify({
    link:t('linkstate'), audioOut:t('audioctx'), state:t('state'),
    heard:t('final'), said:t('answer'), mic:t('miclabel'),
    logs: Array.prototype.map.call(document.querySelectorAll('#log .l'),
      function(d){ return d.textContent.trim(); }).slice(0,14)
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
            tgt = next((t for t in targets() if t.get("type") == "page"
                        and "voice-core" in t.get("url", "")), None)
        except Exception:
            pass
        if tgt:
            break
    if not tgt:
        print("  FAIL: no page"); proc.terminate(); return 2

    from websockets.asyncio.client import connect
    try:
        async with connect(tgt["webSocketDebuggerUrl"], max_size=None) as ws:
            n = [0]
            async def js(e):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": "Runtime.evaluate",
                    "params": {"expression": e, "returnByValue": True}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == n[0]:
                        r = m.get("result", {})
                        if "exceptionDetails" in r:
                            return "EXC:" + json.dumps(r["exceptionDetails"])[:200]
                        v = r.get("result", {}).get("value")
                        return v if v is not None else "NULL"

            r = None
            for _ in range(24):
                await asyncio.sleep(2)
                r = json.loads(await js(READ))
                if str(r["link"]).startswith("live") or "error" in str(r["link"]).lower():
                    break
            await asyncio.sleep(2)
            r = json.loads(await js(READ))
            print(f"  link      : {r['link']}")
            print(f"  audio out : {r['audioOut']}")
            print(f"  mic       : {r['mic']}")
            print("  initial log:")
            for l in r["logs"]:
                print(f"    {l[:110]}")

            if not str(r["link"]).startswith("live"):
                print("\n  page never reached 'live' - nothing else to test")
                print("  link:", r["link"], "| audio out:", r["audioOut"])
                return 1

            print("\n  waking, then speaking into the page (unpatched, as a user would)")
            await js("document.getElementById('micbar').click()")
            await asyncio.sleep(1)
            say = subprocess.Popen(
                ["aplay", "-q", "-D", "default",
                 "/home/abubakargcx/easy-diffusion/tmp/opencode/voice.wav"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(22)
            say.terminate()
            await asyncio.sleep(3)

            r = json.loads(await js(READ))
            print(f"\n  audio out : {r['audioOut']}   state: {r['state']}")
            print(f"  heard     : {str(r['heard'])[:80]!r}")
            print(f"  said      : {str(r['said'])[:110]!r}")
            print("  page log after a full turn:")
            for l in r["logs"]:
                print(f"    {l[:112]}")

            failed = [l for l in r["logs"] if "playback failed" in l.lower()]
            spoke = r["said"] not in (None, "—", "")
            print(f"\n  playback errors : {failed if failed else 'NONE'}")
            print(f"  model replied   : {spoke}")
            print(f"  VERDICT: {'PLAYBACK CLEAN' if not failed else 'PLAYBACK FAILED'}"
                  f" / {'reply seen' if spoke else 'no reply'}")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


sys.exit(asyncio.run(main()))