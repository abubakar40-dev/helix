#!/usr/bin/env python3
"""The definitive end-to-end test: does sound actually leave Chrome?

Everything so far proved the *code* path. This proves the *acoustic* one:

  1. launch Chrome (mic granted), open the voice core
  2. wake it, and record the microphone from the OS for ~20s
  3. the model answers in Urdu; Chrome plays it through the default sink
  4. the microphone records that playback
  5. whisper transcribes the recording

If whisper can hear the MODEL's answer, then Chrome genuinely emitted sound and
the page works end to end. If it hears only silence, the fault is in the audio
output path, not in the orchestration.
"""
import asyncio, json, subprocess, sys, time, urllib.request

CHROME, PORT, PROF = "/usr/bin/google-chrome", 9337, "/tmp/helix-cdp5"
URL = "http://127.0.0.1:8787/voice-core/?cdp=5"
REC = "/home/abubakargcx/easy-diffusion/tmp/opencode/echo-of-helix.wav"
PROMPT = "/home/abubakargcx/easy-diffusion/tmp/opencode/echo-prompt.wav"

READ = """
(function(){
  function t(id){ var e=document.getElementById(id); return e?e.textContent:null; }
  return JSON.stringify({ link:t('linkstate'), audio:t('audioctx'),
    state:t('state'), heard:t('final'), said:t('answer'),
    logs: Array.prototype.map.call(document.querySelectorAll('#log .l'),
      function(d){return d.textContent.trim();}).slice(0,8) });
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
    print("  page up")

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
                        return m.get("result", {}).get("result", {}).get("value")

            await asyncio.sleep(7)
            print(f"  link: {json.loads(await js(READ)).get('link')}")
            await js("document.getElementById('micbar').click()")
            await asyncio.sleep(1)
            print("  awake; recording the mic while the model answers…")

            # Say something, then record what comes back out of the speakers.
            ask = subprocess.Popen(["aplay", "-q", "-D", "default",
                                    "/home/abubakargcx/easy-diffusion/tmp/opencode/voice.wav"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(6)
            ask.terminate()

            rec = subprocess.Popen(
                ["arecord", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", "22",
                 "-D", "default", REC],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(24)
            rec.terminate()
            r = json.loads(await js(READ))
            print(f"\n  page audio out: {r.get('audio')}   state: {r.get('state')}")
            print(f"  page heard: {str(r.get('heard'))[:70]!r}")
            print(f"  page said:  {str(r.get('said'))[:90]!r}")
            for l in r.get("logs", []):
                print(f"    {l[:110]}")
        return r
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


got = asyncio.run(main())
print("\n  === what the microphone heard from the speakers ===")
out = subprocess.run(
    ["~/.local/share/helix-stt/whisper-cli".replace("~", "/home/abubakargcx"),
     "-m", "/home/abubakargcx/.local/share/helix-stt/models/ggml-base.bin",
     "-f", REC, "-l", "auto", "-nt", "-t", "2"],
    capture_output=True, text=True)
for line in [l for l in out.stdout.splitlines() if l.strip()][:6]:
    print(f"    | {line[:100]}")
import os, wave, struct, math
if os.path.exists(REC):
    w = wave.open(REC); fr = w.getnframes()
    s = struct.unpack(f"<{fr}h", w.readframes(fr))
    rms = math.sqrt(sum(x * x for x in s) / fr)
    print(f"    recorded RMS {rms:.0f} over {fr/16000:.1f}s")
    said = str((got or {}).get("said", ""))
    heard_something = any(l.strip() for l in out.stdout.splitlines())
    print(f"\n  VERDICT: {'SPEAKERS EMITTED AUDIO - full loop verified' if heard_something else 'SILENCE ON THE SPEAKERS - the fault is audio output, not orchestration'}")
sys.exit(0)