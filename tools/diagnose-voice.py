#!/usr/bin/env python3
"""Single-pass voice diagnostic. EVIDENCE ONLY — makes no code changes.

Collects, in order, with ISO timestamps at every boundary so every log line can
be correlated to a phase:
  A. TTS endpoint, engine=auto AND a forced fallback tier
  B. Browser: console, panel state, from-Gemini counter, AudioBufferSourceNode
     instrumentation, and a separate <audio> element probe (the panel has none)
  C. Proxy + bridge logs for the same window

Duration is read with ffprobe from the decoded media, never computed from a
formula. play() is called inside a try/catch that reports the DOMException name.

No human voice is used: the prompt played into the mic is synthetic TTS.
"""
import asyncio, json, os, subprocess, sys, time, urllib.request
from datetime import datetime

CHROME, PORT, PROF = "/usr/bin/google-chrome", 9360, "/tmp/helix-diag"
URL = "http://127.0.0.1:8787/voice-core/?diag=1"
OUT = "/tmp/helix-diag-out"
BRIDGE = "http://127.0.0.1:8787/tts"

os.makedirs(OUT, exist_ok=True)


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hdr(t):
    print(f"\n{'='*74}\n{t}\n{'='*74}")


def post_tts(text, lang, engine):
    """Phase A. Returns a dict of everything observed on the wire."""
    payload = json.dumps({"text": text, "lang": lang, "engine": engine})
    path = os.path.join(OUT, f"tts-{engine}.bin")
    cmd = ["curl", "-s", "-o", path, "-D", path + ".hdr", "-w",
           "%{http_code}|%{size_download}|%{content_type}|%{time_total}",
           "-m", "240", "-X", "POST", BRIDGE,
           "-H", "Content-Type: application/json", "-H", "X-Helix-Voice: 1",
           "--data-binary", "@-"]
    r = subprocess.run(cmd, input=payload, capture_output=True, text=True)
    code, size, ctype, tt = (r.stdout.split("|") + [""] * 4)[:4]
    hs = {}
    for line in open(path + ".hdr", errors="replace"):
        if ":" in line:
            k, _, v = line.partition(":")
            hs[k.strip().lower()] = v.strip()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name,"
         "stream=codec_name,sample_rate,channels", "-of", "json", path],
        capture_output=True, text=True)
    try:
        media = json.loads(probe.stdout)
        dur = media["format"].get("duration")
        st = media["streams"][0] if media.get("streams") else {}
    except Exception:
        dur, st = None, {}
    return {
        "request": {"endpoint": BRIDGE, "engine": engine, "lang": lang,
                    "payload_bytes": len(payload), "text": text},
        "response": {"status": code, "bytes": size, "content_type": ctype,
                     "wall_seconds": tt, "saved_as": path,
                     "X-Helix-TTS-Engine": hs.get("x-helix-tts-engine"),
                     "X-Helix-TTS-Skipped": hs.get("x-helix-tts-skipped"),
                     "Content-Type": hs.get("content-type"),
                     "all_headers": hs},
        "media": {"duration_seconds_ffprobe": dur,
                  "container": media.get("format", {}).get("format_name")
                  if 'media' in dir() else None,
                  "codec": st.get("codec_name"), "sample_rate": st.get("sample_rate"),
                  "channels": st.get("channels")},
    }


# ── browser-side JS ────────────────────────────────────────────────────────
JS_INSTRUMENT = r"""
(function(){
  window.__d = { abBuffers:0, abSilent:0, abPeak:0, abSumSq:0, abSamples:0,
                 srcStarted:0, srcEnded:0, errs:[], consoleReady:true };
  var d = window.__d, A = window.AudioContext || window.webkitAudioContext;
  var ob = A.prototype.createBuffer;
  A.prototype.createBuffer = function(c,len,rate){
    var b = ob.apply(this, arguments);
    var measure = function(){
      var ch = b.getChannelData(0), pk=0, sq=0;
      for (var i=0;i<ch.length;i++){ var a=Math.abs(ch[i]); if(a>pk)pk=a; sq+=ch[i]*ch[i]; }
      d.abBuffers++; d.abSamples+=ch.length; d.abSumSq+=sq;
      if (pk>d.abPeak) d.abPeak=pk;
      if (pk<0.005) d.abSilent++;
    };
    b.__measure = measure; return b;
  };
  var os = A.prototype.createBufferSource;
  A.prototype.createBufferSource = function(){
    var s = os.apply(this, arguments), st = s.start.bind(s);
    s.start = function(){
      try { d.srcStarted++; if (s.buffer && s.buffer.__measure) s.buffer.__measure(); }
      catch(e){ d.errs.push('start:'+e); }
      return st.apply(s, arguments);
    };
    s.addEventListener('ended', function(){ d.srcEnded++; });
    return s;
  };
  // Panel readouts, read on demand.
  window.__readPanel = function(){
    function t(id){ var e=document.getElementById(id); return e?e.textContent:null; }
    return { link:t('linkstate'), audioOut:t('audioctx'), fromGemini:t('playstat'),
             state:t('state'), heard:t('final'), said:t('answer'),
             logs: Array.prototype.map.call(document.querySelectorAll('#log .l'),
               function(e){return e.textContent.trim();}) };
  };
  return 'instrumented';
})()
"""

# An <audio> element probe. The panel uses none; this answers the brief's
# question about one directly, in the same page, same browser.
JS_AUDIO_PROBE = r"""
(async function(){
  var url = arguments[0];
  return await new Promise(function(resolve){
    var a = new Audio();
    var out = { created:true, src_set:false, readyState:null, duration:null,
                play_return:null, play_error:null, play_error_name:null,
                currentTime_before:null, currentTime_after_1s:null,
                error_event:null, networkState:null };
    a.src = url; out.src_set = true; a.preload = 'auto';
    a.addEventListener('error', function(){
      out.error_event = a.error ? (a.error.name + ': ' + a.error.message) : 'error event';
    });
    a.addEventListener('loadedmetadata', function(){
      out.readyState = a.readyState; out.duration = a.duration;
      out.currentTime_before = a.currentTime;
      var pr;
      try { pr = a.play(); out.play_return = 'returned ' + (pr && pr.constructor ? pr.constructor.name : String(pr)); }
      catch (e) { out.play_return = 'threw'; out.play_error = e && e.message;
                  out.play_error_name = (e && e.name) || 'Error'; }
      setTimeout(function(){
        out.currentTime_after_1s = a.currentTime;
        out.readyState_after = a.readyState;
        resolve(out);
      }, 1000);
    });
    setTimeout(function(){ if (out.duration === null) resolve(out); }, 8000);
  });
})()
"""


def targets():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
        return json.load(r)


async def browser_phase(audio_urls):
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
        proc.terminate()
        return None, [], "page never appeared"

    from websockets.asyncio.client import connect
    console = []
    result = {}
    try:
        async with connect(tgt["webSocketDebuggerUrl"], max_size=None) as ws:
            n = [0]

            async def cmd(method, **params):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method,
                                          "params": params}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == n[0]:
                        return m
                    meth = m.get("method", "")
                    if meth in ("Runtime.consoleAPICalled",
                                "Log.entryAdded", "Runtime.exceptionThrown"):
                        console.append({"t": ts(), "method": meth,
                                        "detail": json.dumps(m["params"])[:600]})

            async def js(expr, awaitp=False):
                r = await cmd("Runtime.evaluate", expression=expr,
                              returnByValue=True, awaitPromise=awaitp)
                if "exceptionDetails" in r.get("result", {}):
                    return "EXC:" + json.dumps(r["result"]["exceptionDetails"])[:300]
                return r.get("result", {}).get("result", {}).get("value")

            await cmd("Runtime.enable")
            await cmd("Log.enable")
            await cmd("Page.enable")
            await cmd("Page.addScriptToEvaluateOnNewDocument", source=JS_INSTRUMENT)
            await js("location.reload()")          # run the injected source
            await asyncio.sleep(2)
            result["instrument_installed"] = await js("typeof window.__readPanel === 'function'")

            result["panel_reached_live"] = False
            for _ in range(30):
                await asyncio.sleep(2)
                p = await js("window.__readPanel()")
                if isinstance(p, str):
                    continue
                if str(p.get("link", "")).startswith("live"):
                    result["panel_reached_live"] = True
                    break
            result["panel_on_live"] = await js("window.__readPanel()")

            # Trigger a real turn using a SYNTHETIC prompt (no human voice).
            result["before_play"] = await js("window.__readPanel()")
            await js("document.getElementById('micbar').click()")
            # Repeat the synthetic prompt: a single pass through the headless
            # mic/speaker loopback is dropped often enough to be useless.
            for _ in range(4):
                subprocess.run(["aplay", "-q", "-D", "default",
                                os.path.join(OUT, "prompt.wav")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                await asyncio.sleep(3)
            await asyncio.sleep(6)
            result["after_play"] = await js("window.__readPanel()")
            result["counters"] = await js("""(function(){
              var d = window.__d; return {buffers:d.abBuffers, silent:d.abSilent,
                peak:d.abPeak, rms:d.abSamples?Math.sqrt(d.abSumSq/d.abSamples):0,
                samples:d.abSamples, started:d.srcStarted, ended:d.srcEnded,
                errors:d.errs};
            })()""")
            await asyncio.sleep(2)
            result["counters_plus2s"] = await js("""(function(){
              var d = window.__d; return {buffers:d.abBuffers, started:d.srcStarted,
                ended:d.srcEnded};
            })()""")

            # <audio> element probe on each TTS artefact
            result["audio_element_probe"] = {}
            for name, path in audio_urls.items():
                with open(path, "rb") as fh:
                    b64 = subprocess.run(["base64", "-w0"], stdin=fh,
                                         capture_output=True, text=True).stdout
                mime = "audio/wav" if path.endswith(".wav") else "audio/mpeg"
                r = await js(JS_AUDIO_PROBE.replace("arguments[0]",
                                json.dumps(f"data:{mime};base64,{b64}")),
                             awaitp=True)
                result["audio_element_probe"][name] = r
            return result, console, None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


def main():
    hdr("PHASE 0  environment")
    for s in ("helix-bridge", "helix-live"):
        print(f"  {s:14} {subprocess.run(['systemctl','--user','is-active',s],capture_output=True,text=True).stdout.strip()}")
    print(f"  wall clock start: {ts()}")

    hdr("PHASE A  TTS endpoint, both engines")
    a_auto = post_tts("Confirming Gemini Live is the first tier.", "en", "auto")
    print(f"  [{ts()}] engine=auto")
    print(f"    request  : {json.dumps(a_auto['request'])[:150]}")
    print(f"    status   : {a_auto['response']['status']}   bytes={a_auto['response']['bytes']}"
          f"   {a_auto['response']['wall_seconds']}s")
    print(f"    X-Helix-TTS-Engine  : {a_auto['response']['X-Helix-TTS-Engine']}")
    print(f"    X-Helix-TTS-Skipped : {a_auto['response']['X-Helix-TTS-Skipped']}")
    print(f"    Content-Type        : {a_auto['response']['Content-Type']}")
    print(f"    ffprobe duration    : {a_auto['media']['duration_seconds_ffprobe']}s"
          f"   codec={a_auto['media']['codec']}  {a_auto['media']['sample_rate']}Hz"
          f"  ch={a_auto['media']['channels']}")

    a_edge = post_tts("This is the forced fallback tier.", "en", "edge")
    print(f"  [{ts()}] engine=edge (forced tier, for comparison)")
    print(f"    status   : {a_edge['response']['status']}   bytes={a_edge['response']['bytes']}")
    print(f"    X-Helix-TTS-Engine  : {a_edge['response']['X-Helix-TTS-Engine']}")
    print(f"    Content-Type        : {a_edge['response']['Content-Type']}")
    print(f"    ffprobe duration    : {a_edge['media']['duration_seconds_ffprobe']}s"
          f"   codec={a_edge['media']['codec']}")

    # synthetic prompt for the mic, so no human voice is involved
    # Synthetic prompt, via the bridge tier we already proved works. No human voice.
    subprocess.run(["bash", "-c",
        "curl -s -o " + OUT + "/prompt.raw -X POST http://127.0.0.1:8787/tts "
        "-H 'Content-Type: application/json' -H 'X-Helix-Voice: 1' "
        "-d '{\"text\":\"What is two plus two\",\"lang\":\"en\",\"engine\":\"edge\"}' >/dev/null 2>&1; "
        "ffmpeg -y -loglevel error -i " + OUT + "/prompt.raw -ar 16000 -ac 1 "
        + OUT + "/prompt.wav >/dev/null 2>&1"], check=False)
    print(f"  synthetic prompt: "
          f"{os.path.getsize(OUT + '/prompt.wav') if os.path.exists(OUT + '/prompt.wav') else 'FAILED'} bytes")

    hdr("PHASE B  browser (one session, console captured throughout)")
    urls = {"live_tts_auto": a_auto["response"]["saved_as"],
            "edge_forced": a_edge["response"]["saved_as"]}
    res, console, err = asyncio.run(browser_phase(urls))
    if err or res is None:
        print(f"  FAILED: {err}")
        return 2

    print(f"  probe installed      : {res.get('instrument_installed')}")
    print(f"  panel reached 'live' : {res['panel_reached_live']}")
    def safe(v):
        return v if isinstance(v, dict) else {"_unreadable": str(v)[:200]}
    print(f"\n  panel BEFORE playback trigger:")
    print(f"    {json.dumps(safe(res['before_play']))[:400]}")
    print(f"\n  panel AFTER (2s settle):")
    print(f"    {json.dumps(safe(res['after_play']))[:400]}")
    print(f"\n  AudioBufferSourceNode counters:")
    print(f"    {json.dumps(safe(res['counters']))}")
    print(f"    +2s later: {json.dumps(safe(res['counters_plus2s']))}")
    print(f"\n  'from Gemini' counter:")
    print(f"    before: {safe(res['before_play']).get('fromGemini')}"
          f"   after: {safe(res['after_play']).get('fromGemini')}")

    print(f"\n  <audio> ELEMENT probe (panel has none; probed separately):")
    for name, r in res["audio_element_probe"].items():
        print(f"    {name}:")
        for k, v in (r.items() if isinstance(r, dict) else [("raw", r)]):
            print(f"      {k:22} {v}")

    print(f"\n  full console during the sequence ({len(console)} entries):")
    for c in console:
        print(f"    [{c['t']}] {c['method']}: {c['detail'][:240]}")
    if not console:
        print("    (none)")

    hdr("PHASE C  proxy + bridge logs for the same window")
    for unit in ("helix-live", "helix-bridge"):
        print(f"\n  --- {unit} ---")
        out = subprocess.run(["journalctl", "--user", "-u", unit,
                              "--since", "-6min", "--no-pager"],
                             capture_output=True, text=True).stdout
        keep = [l for l in out.splitlines()
                if "[live]" in l or "TTS" in l or "Error" in l or "error" in l]
        for l in keep[-8:]:
            print("   ", l[:160])
        if not keep:
            print("    (no matching lines)")

    hdr("PHASE D  raw artefacts for independent re-checking")
    for name, path in urls.items():
        print(f"  {name}: {path}  {os.path.getsize(path)} bytes")
    print(f"  phase A ran {ts()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())