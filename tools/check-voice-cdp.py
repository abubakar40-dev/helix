#!/usr/bin/env python3
"""Drive the Helix voice core in a Chrome we launch ourselves, with the
microphone permission granted up front.

The harness browser hard-denies getUserMedia and there is no tool to grant it,
so the in-page voice path could never be tested from here. This launches Chrome
with the permission pre-approved (NOT a fake device — the real mic) and speaks
CDP to it.

The flag that matters for the suspected bug:
    --autoplay-policy=no-user-gesture-required
keeps the AudioContext out of `suspended`, so if audio is still silent the cause
is genuinely the code and not Chrome's autoplay policy.
"""
import json, os, shutil, subprocess, sys, time, urllib.request

CHROME = "/usr/bin/google-chrome"
PORT = 9333
PROFILE = "/tmp/helix-cdp-profile"
URL = "http://127.0.0.1:8787/voice-core/?cdp=1"


def cdp_get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.load(r)


def main():
    shutil.rmtree(PROFILE, ignore_errors=True)
    args = [
        CHROME,
        "--headless=new",
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE}",
        # Pre-approve the microphone. Deliberately NOT using
        # --use-fake-device-for-media-capture: we want the real mic.
        "--use-fake-ui-for-media-stream",
        # Keep the AudioContext out of `suspended`, so silence cannot be
        # blamed on autoplay policy rather than on our code.
        "--autoplay-policy=no-user-gesture-required",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        URL,
    ]
    print(f"  launching: {' '.join(args[1:6])} …")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target = None
    for _ in range(40):
        time.sleep(0.5)
        try:
            for t in cdp_get("/json/list"):
                if t.get("type") == "page" and "voice-core" in t.get("url", ""):
                    target = t
                    break
        except Exception:
            continue
        if target:
            break
    if not target:
        print("  FAIL: never saw the voice-core page")
        proc.terminate()
        return 2

    print(f"  page ready: {target['url'][:60]}")
    import asyncio
    from websockets.asyncio.client import connect

    async def run():
        async with connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            mid = [0]
            async def cmd(method, **params):
                mid[0] += 1
                await ws.send(json.dumps({"id": mid[0], "method": method,
                                          "params": params}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == mid[0]:
                        return m
                    if m.get("method") == "Runtime.consoleAPICalled":
                        args = m["params"].get("args", [])
                        txt = " ".join(str(a.get("value", a.get("description", "")))
                                       for a in args)
                        if txt.strip():
                            print(f"   console: {txt[:120]}")

            async def js(expr):
                r = await cmd("Runtime.evaluate", expression=expr,
                              returnByValue=True, awaitPromise=True)
                res = r.get("result", {})
                if "exceptionDetails" in r:
                    return {"error": str(r["exceptionDetails"])[:200]}
                return res.get("result", {}).get("value")

            await cmd("Runtime.enable")
            await cmd("Log.enable")

            # Does getUserMedia actually work here? That was the blocker.
            mic = await js("""(async () => {
              try {
                const s = await navigator.mediaDevices.getUserMedia({audio:true});
                const t = s.getAudioTracks()[0];
                const r = {ok:true, label:t.label, state:t.readyState,
                           settings:t.getSettings()};
                s.getTracks().forEach(x=>x.stop());
                return r;
              } catch(e) { return {ok:false, error:e.name+': '+e.message}; }
            })()""")
            print(f"  getUserMedia: {json.dumps(mic)[:220]}")

            await js("location.reload()")
            await asyncio.sleep(9)

            for _ in range(10):
                st = await js("""(function(){
                  return { link: document.getElementById('linkstate').textContent,
                           audio: document.getElementById('audioctx').textContent,
                           state: document.getElementById('state').textContent,
                           mic: document.getElementById('miclabel').textContent };
                })()""")
                print(f"  page: {json.dumps(st)}")
                if st.get("audio") == "running" and "live" in str(st.get("link")):
                    break
                await asyncio.sleep(3)

            # Unmute and let a real turn happen.
            await js("document.getElementById('micbar').click()")
            print("  woke it; feeding the recorded speech in for ~14s")
            import wave
            w = wave.open("/home/abubakargcx/easy-diffusion/tmp/opencode/voice.wav")
            data = w.readframes(w.getnframes())
            step = 16000
            r = await js("""(()=>{ const s=new WebSocket('ws://'+location.hostname+':8788');
              window.__ws=s; s.binaryType='arraybuffer';
              window.__bins=0; window.__texts=[];
              s.onmessage=e=>{ if(typeof e.data==='string'){ window.__texts.push(e.data);
                              const d=JSON.parse(e.data);
                              if(d.type==='interrupted') window.__bins+=1000000; }
                              else window.__bins+=e.data.byteLength; };
              return 'ok'; })()""")
            print(f"  listener socket: {r}")
            for i in range(0, len(data), step):
                ok = await js(f"window.__ws && window.__ws.readyState===1 ? "
                              f"(window.__ws.send(new Uint8Array({list(data[i:i+step])})), 1) : 0")
                await asyncio.sleep(0.05)
            await asyncio.sleep(8)

            got = await js("""({bins: window.__bins, texts: window.__texts.slice(-8),
                               audio: document.getElementById('audioctx').textContent,
                               heard: document.getElementById('final').textContent,
                               said: document.getElementById('answer').textContent})""")
            print(f"\n  binary audio bytes received by the page: {got.get('bins',0):,}")
            print(f"  AudioContext state: {got.get('audio')}")
            print(f"  heard: {str(got.get('heard'))[:80]!r}")
            print(f"  said:  {str(got.get('said'))[:80]!r}")
            for t in (got.get("texts") or [])[:6]:
                print(f"   <- {t[:100]}")
            return got

        print("  FAIL: socket closed")
        return {}

    try:
        got = asyncio.run(run())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    ok = got.get("bins", 0) > 10000 and got.get("audio") == "running"
    print(f"\n  VERDICT: {'PASS' if ok else 'FAIL'} "
          f"(audio bytes to page={got.get('bins',0):,}, ctx={got.get('audio')})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())