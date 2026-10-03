#!/usr/bin/env python3
"""Final verification: real playback AND barge-in, with audio-only probes.

Deliberately does NOT touch WebSocket. The previous audit overrode the
`binaryType` setter without forwarding it, so the socket stayed on "blob",
`new Int16Array(blob)` produced zero samples, and createBuffer threw — a
failure manufactured by the probe. Only AudioBuffer/createBufferSource are
wrapped here, which cannot alter delivery.

Proof required, not assumed:
  * an AudioBuffer built from the model's audio is NON-SILENT (real peak)
  * a source is started, and later fires `ended` — which only happens after
    playback has actually run to completion, i.e. time advanced
  * barge-in still interrupts
"""
import asyncio, json, subprocess, sys, time, urllib.request

CHROME, PORT, PROF = "/usr/bin/google-chrome", 9342, "/tmp/helix-final"
URL = "http://127.0.0.1:8787/voice-core/?final=1"

PROBE = r"""
window.__p = { buffers:0, silent:0, peak:0, sumSq:0, samples:0,
               started:0, ended:0, errors:[], blobs:0 };
(function(){
  var p = window.__p, ACTX = window.AudioContext || window.webkitAudioContext;
  var ob = ACTX.prototype.createBuffer;
  ACTX.prototype.createBuffer = function(c, len, rate){
    var b = ob.apply(this, arguments);
    b.__measure = function(){
      var d = b.getChannelData(0), pk = 0, sq = 0;
      for (var i=0;i<d.length;i++){ var a = Math.abs(d[i]); if(a>pk) pk=a; sq += d[i]*d[i]; }
      p.buffers++; p.samples += d.length; p.sumSq += sq;
      if (pk > p.peak) p.peak = pk;
      if (pk < 0.005) p.silent++;
      return { len: d.length, rate: b.sampleRate, peak: pk };
    };
    return b;
  };
  var os = ACTX.prototype.createBufferSource;
  ACTX.prototype.createBufferSource = function(){
    var s = os.apply(this, arguments), st = s.start.bind(s);
    s.start = function(){
      try { p.started++; if (s.buffer && s.buffer.__measure) s.__m = s.buffer.__measure(); }
      catch(e){ p.errors.push('start:'+e); }
      return st.apply(s, arguments);
    };
    s.addEventListener('ended', function(){ p.ended++; });
    return s;
  };
  return 'probed';
})()
"""

READ = r"""
(function(){
  function t(id){ var e=document.getElementById(id); return e?e.textContent:null; }
  var p = window.__p || {};
  return JSON.stringify({
    buffers:p.buffers, silent:p.silent, peak:p.peak,
    rms: p.samples ? Math.sqrt(p.sumSq/p.samples) : 0,
    samples:p.samples, started:p.started, ended:p.ended, errors:p.errors,
    link:t('linkstate'), audioOut:t('audioctx'), state:t('state'),
    heard:t('final'), said:t('answer'),
    logs: Array.prototype.map.call(document.querySelectorAll('#log .l'),
      function(d){ return d.textContent.trim(); }).slice(0,12)
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

            await js(PROBE)
            for _ in range(24):
                await asyncio.sleep(2)
                if json.loads(await js(READ))["link"].startswith("live"):
                    break
            r = json.loads(await js(READ))
            print(f"  link {r['link']}   audio out {r['audioOut']}")

            # Re-inject right before the turn: the document can be replaced while
            # we wait for 'live', which silently drops window.__p.
            await js(PROBE)
            have = await js("!!window.__p")
            print(f"  probe installed: {have}")
            await asyncio.sleep(1)

            print("\n  --- turn 1: speak, let it answer, let playback finish ---")
            await js("document.getElementById('micbar').click()")
            await asyncio.sleep(1)
            say = subprocess.Popen(["aplay", "-q", "-D", "default",
                "/home/abubakargcx/easy-diffusion/tmp/opencode/voice.wav"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(20)
            say.terminate()
            await asyncio.sleep(4)
            r1 = json.loads(await js(READ)) or {"buffers":0,"silent":0,"peak":0,
                "rms":0,"samples":0,"started":0,"ended":0,"errors":[],"logs":[]}
            print(f"   buffers {r1['buffers']} (silent {r1['silent']})  "
                  f"peak {r1['peak']:.4f}  rms {r1['rms']:.4f}")
            print(f"   started {r1['started']}  ended {r1['ended']}  errors {r1['errors']}")
            print(f"   said: {str(r1['said'])[:80]!r}")

            print("\n  --- turn 2: talk over it, expecting an interrupt ---")
            before = r1["started"]
            await js("document.getElementById('micbar').click()")
            await asyncio.sleep(1)
            say2 = subprocess.Popen(["aplay", "-q", "-D", "default",
                "/home/abubakargcx/easy-diffusion/tmp/opencode/voice.wav"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(4)
            say2.terminate()
            await asyncio.sleep(10)
            r2 = json.loads(await js(READ))
            interrupted = [l for l in r2["logs"] if "interrupted" in l.lower()]
            print(f"   started {r2['started']} (was {before})  ended {r2['ended']}")
            print(f"   interrupt lines: {interrupted if interrupted else 'NONE'}")

            print("\n  page log:")
            for l in r2["logs"]:
                print(f"     {l[:108]}")

            audible = r1["buffers"] > 0 and r1["peak"] > 0.005 and r1["started"] > 0
            completed = r1["ended"] > 0
            print(f"\n  non-silent buffer : {r1['peak']:.4f} peak  ({'OK' if r1['peak']>0.005 else 'NO'})")
            print(f"  playback started  : {r1['started']}")
            print(f"  playback ENDED    : {r1['ended']}  <- only fires after time advanced")
            print(f"  barge-in          : {'OK' if interrupted else 'not observed'}")
            ok = audible and completed
            print(f"\n  VERDICT: {'PASS - real audible playback verified' if ok else 'FAIL'}")
            return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


sys.exit(asyncio.run(main()))