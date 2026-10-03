#!/usr/bin/env python3
"""Second pass: drive the PAGE's own socket (not a test one) and read its log.

The first CDP pass proved audio reaches the page and the AudioContext is
running, which falsified the suspended-context theory. This exercises the real
code path: the page's own WebSocket and its own playOut(), then reads the page's
log panel for a playback error — which it now logs instead of swallowing.
"""
import asyncio, json, subprocess, sys, time, urllib.request

CHROME, PORT, PROFILE = "/usr/bin/google-chrome", 9334, "/tmp/helix-cdp2"
URL = "http://127.0.0.1:8787/voice-core/?cdp=2"


def get(p):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{p}", timeout=5) as r:
        return json.load(r)


async def main():
    proc = subprocess.Popen([
        CHROME, "--headless=new", f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE}", "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required", "--no-sandbox",
        "--disable-dev-shm-usage", "--disable-gpu", URL,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    target = None
    for _ in range(40):
        time.sleep(0.5)
        try:
            target = next((t for t in get("/json/list")
                           if t.get("type") == "page" and "voice-core" in t.get("url", "")),
                          None)
        except Exception:
            pass
        if target:
            break
    if not target:
        print("  FAIL: no page"); proc.terminate(); return 2

    from websockets.asyncio.client import connect
    try:
        async with connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            mid = [0]
            async def cmd(method, **params):
                mid[0] += 1
                await ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == mid[0]:
                        return m
            async def js(e):
                if "JSON.stringify" not in e and "returnByValue" not in e:
                    e = "JSON.stringify((function(){" + e + "})())"
                r = await cmd("Runtime.evaluate", expression=e, returnByValue=True,
                              awaitPromise=True)
                if "exceptionDetails" in r:
                    return {"_err": str(r["exceptionDetails"])[:200]}
                v = r.get("result", {}).get("value")
                if isinstance(v, str) and v[:1] in "[{":
                    try: return json.loads(v)
                    except Exception: pass
                return v

            await cmd("Runtime.enable"); await cmd("Log.enable")
            await asyncio.sleep(8)

            READ = """
              var t = function(id){ var e=document.getElementById(id); return e?e.textContent:null };
              return { link:t('linkstate'), audio:t('audioctx'), state:t('state'),
                       heard:t('final'), said:t('answer'),
                       logs: Array.prototype.map.call(
                         document.querySelectorAll('#log .l'),
                         function(d){return d.textContent.trim()}).slice(0,8) };
            """
            print(f"  link: {(await js(READ)).get('link')}")

            # Count what the page actually attempts to play, and catch its errors.
            await js("""(()=>{ window.__plays=0; window.__errs=[];
              const CE = window.AudioContext.prototype.createBufferSource;
              window.AudioContext.prototype.createBufferSource = function(){
                window.__plays++;
                const s = CE.apply(this, arguments);
                const st = s.start.bind(s);
                s.start = function(){ try{ return st.apply(s, arguments); }
                                     catch(e){ window.__errs.push(String(e)); throw e; } };
                return s; };
              return 'instrumented';""")

            # Wake it, then play the recording out loud so the page's OWN mic
            # pump picks it up and drives the real path end to end.
            await js("document.getElementById('micbar').click()")
            await asyncio.sleep(1)
            print("  playing the recording through the speakers for the page's mic…")
            p = subprocess.Popen(["aplay", "-q", "-D", "default",
                                  "/home/abubakargcx/easy-diffusion/tmp/opencode/voice.wav"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(16)
            p.terminate()

            r = await js("""
              var t = function(id){ var e=document.getElementById(id); return e?e.textContent:null };
              return { plays: window.__plays, errs: (window.__errs||[]).slice(0,5),
                audio:t('audioctx'), state:t('state'), heard:t('final'), said:t('answer'),
                logs: Array.prototype.map.call(document.querySelectorAll('#log .l'),
                  function(d){return d.textContent.trim()}).slice(0,8) };
            """) or {}
            print(f"\n  createBufferSource calls (play attempts): {r.get('plays')}")
            print(f"  playback errors: {r.get('errs')}")
            print(f"  AudioContext: {r.get('audio')}   state: {r.get('state')}")
            print(f"  heard: {str(r.get('heard'))[:90]!r}")
            print(f"  said:  {str(r.get('said'))[:90]!r}")
            print("  page log:")
            for l in (r.get("logs") or []):
                print(f"    {l[:110]}")
            ok = r.get("plays", 0) > 0 and not r.get("errs")
            print(f"\n  VERDICT: {'PASS' if ok else 'FAIL'}")
            return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


sys.exit(asyncio.run(main()))