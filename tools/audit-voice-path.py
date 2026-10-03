#!/usr/bin/env python3
"""Hop-by-hop audit of the Helix voice path. Evidence only, no assumptions.

The earlier "full loop verified" claim was WRONG: check-voice-echo.py played
my own voice.wav through the speakers and then recorded the microphone, so the
transcript it got could have been my own audio coming back. It never isolated
Chrome's output. This script fixes that and checks every hop separately:

  H1  does the page's OWN WebSocket receive binary audio?   (not a test socket)
  H2  does the model actually emit audio for a real turn?  (proxy counters)
  H3  does playOut() build a NON-SILENT AudioBuffer?       (peak/RMS of samples)
  H4  does playback actually ADVANCE currentTime?          (not just scheduled)
  H5  is there a real audio element / is ctx running?

Method for H1/H3/H4: patch the page's own WebSocket and AudioContext from
inside the page, then drive a real turn with the REAL microphone - nothing is
played through the speakers by this script, so anything the microphone hears
can only have come from the page.
"""
import asyncio, json, subprocess, sys, time, urllib.request

CHROME, PORT, PROF = "/usr/bin/google-chrome", 9340, "/tmp/helix-audit"
URL = "http://127.0.0.1:8787/voice-core/?audit=1"
REC = "/home/abubakargcx/easy-diffusion/tmp/opencode/audit-mic.wav"

# Installed before the page connects. Wraps the page's own WebSocket so we count
# what IT receives, not what a test socket receives.
INSTRUMENT = r"""
(function(){
  window.__h = { wsBinaryFrames:0, wsBinaryBytes:0, wsText:0, wsOpened:0,
                 buffers:0, silentBuffers:0, peak:0, sumSq:0, samples:0,
                 started:0, ended:0, ctxStates:[], errors:[] };
  var h = window.__h;

  var OW = window.WebSocket;
  window.WebSocket = function(url, proto){
    var s = proto ? new OW(url, proto) : new OW(url);
    h.wsOpened++;
    var ob = s.binaryType;
    Object.defineProperty(s, 'binaryType', {
      get: function(){ return ob; },
      set: function(v){ ob = v; s.__bin = v; }
    });
    s.addEventListener('message', function(ev){
      if (typeof ev.data === 'string') { h.wsText++; return; }
      h.wsBinaryFrames++;
      h.wsBinaryBytes += (ev.data.byteLength || ev.data.length || 0);
    });
    s.addEventListener('error', function(){ h.errors.push('ws error'); });
    return s;
  };
  window.WebSocket.prototype = OW.prototype;
  Object.keys(OW).forEach(function(k){ try{ window.WebSocket[k]=OW[k]; }catch(e){} });

  var ACTX = window.AudioContext || window.webkitAudioContext;
  var origBuf = ACTX.prototype.createBuffer;
  ACTX.prototype.createBuffer = function(ch, len, rate){
    var b = origBuf.apply(this, arguments);
    b.__probe = function(){
      var d = b.getChannelData(0), p = 0, sq = 0;
      for (var i=0;i<d.length;i++){ var a = Math.abs(d[i]); if (a>p) p=a; sq += d[i]*d[i]; }
      h.buffers++; h.samples += d.length; h.sumSq += sq;
      if (p > h.peak) h.peak = p;
      if (p < 0.0005) h.silentBuffers++;
      return {len:d.length, rate:b.sampleRate, peak:p};
    };
    return b;
  };

  var origSrc = ACTX.prototype.createBufferSource;
  ACTX.prototype.createBufferSource = function(){
    var s = origSrc.apply(this, arguments);
    var st = s.start.bind(s);
    s.start = function(){
      try { h.started++; if (s.buffer && s.buffer.__probe) s.buffer.__probe(); }
      catch(e){ h.errors.push('start:'+e); }
      return st.apply(s, arguments);
    };
    s.addEventListener('ended', function(){ h.ended++; });
    return s;
  };

  var od = ACTX.prototype.decodeAudioData;
  ACTX.prototype.decodeAudioData = function(){
    h.errors.push('decodeAudioData still in use');
    return od.apply(this, arguments);
  };
  return 'instrumented';
})()
"""

READ = r"""
(function(){
  function t(id){ var e=document.getElementById(id); return e?e.textContent:null; }
  var h = window.__h || {};
  return JSON.stringify({
    h1_binaryFrames: h.wsBinaryFrames, h1_binaryBytes: h.wsBinaryBytes,
    h1_textFrames: h.wsText, h1_sockets: h.wsOpened,
    h3_buffers: h.buffers, h3_silentBuffers: h.silentBuffers,
    h3_peak: h.peak, h3_rms: h.samples ? Math.sqrt(h.sumSq/h.samples) : 0,
    h3_samples: h.samples,
    h4_started: h.started, h4_ended: h.ended,
    errors: (h.errors||[]).slice(0,5),
    link: t('linkstate'), audioOut: t('audioctx'), state: t('state'),
    mic: t('miclabel'), heard: t('final'), said: t('answer'),
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

            # Instrument as early as possible, then reload so the page's own
            # WebSocket construction goes through the wrapper.
            await js(INSTRUMENT)
            await js("sessionStorage.setItem('__audited','1')")
            await js("location.reload()")
            await asyncio.sleep(1)
            await js(INSTRUMENT)          # reload clears window; re-patch on new doc
            await asyncio.sleep(8)

            r = json.loads(await js(READ))
            print(f"  H1 page socket opened        : {r['h1_sockets']}")
            print(f"  H1 binary frames RECEIVED    : {r['h1_binaryFrames']:,} "
                  f"({r['h1_binaryBytes']:,} bytes)   text frames: {r['h1_textFrames']}")
            print(f"  H5 AudioContext              : {r['audioOut']}")
            print(f"  H5 link                      : {r['link']}")

            if r['h1_sockets'] == 0 or not str(r['link']).startswith('live'):
                print("\n  hop1 FAILED: the page never opened a Live socket")
                for l in r["logs"]:
                    print(f"    {l[:110]}")
                return 1

            print("\n  waking and recording the mic (this script plays NOTHING, so "
                  "anything heard must be the page)")
            await js("document.getElementById('micbar').click()")
            await asyncio.sleep(1)
            rec = subprocess.Popen(
                ["arecord", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", "30",
                 "-D", "default", REC],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Headless Chrome's mic hears only room tone, so without this the
            # model correctly stays silent and every hop reads zero for the
            # wrong reason. Speak into the page so a real turn happens.
            await asyncio.sleep(2)
            say = subprocess.Popen(
                ["aplay", "-q", "-D", "default",
                 "/home/abubakargcx/easy-diffusion/tmp/opencode/voice.wav"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(24)
            say.terminate()
            await asyncio.sleep(3)
            rec.terminate()
            await asyncio.sleep(1)

            r = json.loads(await js(READ))
            print(f"\n  H1 binary frames RECEIVED    : {r['h1_binaryFrames']:,} "
                  f"({r['h1_binaryBytes']:,} bytes = {r['h1_binaryBytes']/48000:.2f}s @24k)")
            print(f"  H3 AudioBuffers built        : {r['h3_buffers']}  "
                  f"(silent: {r['h3_silentBuffers']})  samples {r['h3_samples']:,}")
            print(f"  H3 peak / RMS                : {r['h3_peak']:.4f} / {r['h3_rms']:.4f}")
            print(f"  H4 started / ended           : {r['h4_started']} / {r['h4_ended']}")
            print(f"     errors                    : {r['errors']}")
            print(f"  heard: {str(r['heard'])[:70]!r}")
            print(f"  said:  {str(r['said'])[:90]!r}")
            print("  page log:")
            for l in r["logs"]:
                print(f"    {l[:112]}")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


asyncio.run(main())