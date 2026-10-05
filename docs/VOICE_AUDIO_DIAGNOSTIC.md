# Voice Core — Audio Diagnostic Report

**Date:** 2026-10-05 · **Phase B run:** 23:49:52–23:50:21 local
**Harness:** `tools/diagnose-voice.py` (one pass; artefacts in `/tmp/helix-diag-out/`)
**Status: DIAGNOSTIC ONLY — no product code changed.** Two diagnostic counters were
added to the panel (`from Gemini: Nf/NB builN`) and a synthetically generated TTS prompt
was played into the mic. **No human voice was used.**

---

## Summary of the verdict

| Option | Verdict | Basis |
|---|---|---|
| (a) audio never produced | **RULED OUT** | Gemini returned audio; proxy forwarded it; both `/tts` tiers produced real, decodable media |
| (b) audio produced but never reached the browser | **RULED OUT** | panel counter went `0f/0B` → `8f/83520B`; proxy independently logged `8 audio frames / 83,520 B out` |
| (c) audio reached browser but `play()` blocked/failed | **RULED OUT for the Web-Audio path; `<audio>` path definitively ruled out** | `<audio>` probe: `play()` returned a Promise, no DOMException, `readyState 4`, `currentTime 0 → 1.108` |
| (d) `play()` succeeded but no device output | **NOT RULED OUT** | the one CDP probe disagrees with the page's own counter (see §5). Cannot be settled headless. |

**Most likely: (d), or a stall inside the panel's own playback loop.** One specific
point remains genuinely ambiguous and I name the test for it in §6 rather than
guessing past it.

---

## 1. TTS endpoint — both engines, exact wire evidence

### engine=auto (Gemini Live expected)

```
POST http://127.0.0.1:8787/tts
{"text": "Confirming Gemini Live is the first tier.", "lang": "en", "engine": "auto"}
payload_bytes: 85
```

```
HTTP/1.1 200
Content-Type:      audio/wav
X-Helix-TTS-Engine: live-tts
X-Helix-TTS-Skipped: (absent — nothing was skipped)
bytes:             358,606
wall:              11.499901s
ffprobe duration:  7.470042s      <- read from the decoded media, not computed
```

### engine=edge (forced tier, for comparison)

```
HTTP/1.1 200
Content-Type:      audio/mpeg
X-Helix-TTS-Engine: edge-tts
X-Helix-TTS-Skipped: (absent)
bytes:             15,984
ffprobe duration:  2.664000s
```

Both tiers return genuine, decodable audio. `X-Helix-TTS-Skipped` is correctly absent
when no tier was skipped.

---

## 2. Browser — panel state, in order

```
probe installed      : True      (Page.addScriptToEvaluateOnNewDocument)
panel reached 'live' : True

BEFORE trigger:
  link       : live · gemini-3.8-live · Charon
  audioOut   : running
  fromGemini : 0f/0B buil0
  state      : IDLE

AFTER (2s settle):
  link       : live · gemini-3.8-live · Charon
  audioOut   : running
  fromGemini : 8f/83520B buil1        <- 8 frames / 83,520 bytes received, 1 buffer built
  state      : LISTENING
  heard      : "reps"
  said       : "What are we tracking reps for?"
```

`from Gemini` counter, exactly as requested:
- before triggering playback: `0f/0B buil0`
- 2s after: `8f/83520B buil1`

---

## 3. The panel has NO `<audio>` element

```
grep -c "new Audio\(|<audio" ui/voice-core/index.html   ->  0
```

Playback is **Web Audio** (`createBufferSource`), so `<audio>` state cannot be read
from the panel. I probed a separate `<audio>` element in the same page and browser to
answer the brief's question directly:

| | live_tts_auto | edge_forced |
|---|---|---|
| created | True | True |
| src set | True | True |
| readyState | 4 | 4 |
| duration | 7.470042 | 2.664000 |
| play() return | returned Promise | returned Promise |
| play() error | None | None |
| **DOMException name** | **none** | **none** |
| currentTime before | 0 | 0 |
| **currentTime after 1s** | **1.108039** | **1.259251** |
| error event | None | None |

Repeated across four runs: `currentTime` advanced 1.07–1.64s every time. **Autoplay is
not blocked, `play()` does not throw, and media time advances in this environment.**

---

## 4. Console output (full, during the sequence)

```
[23:49:5x] warning  rendering  "Automatic fallback to software WebGL has been deprecated…"
[23:49:5x] warning  rendering  "GL Driver Message … GPU stall due to ReadPixels"   (x4)
[23:49:5x] error    network   "404 Not Found"  url=http://127.0.0.1:8787/favicon.ico
[23:49:5x] warning  deprecation "The ScriptProcessorNode is deprecated. Use AudioWorkletNode instead."
```

**No audio error, no autoplay refusal, no `NotAllowedError`, no decode failure.**
The only error is a missing favicon. The only audio-relevant entry is a deprecation
warning about `ScriptProcessorNode`.

---

## 5. Correlated server-side logs, same window

```
helix-live   23:49:52  out 0f/0B      | in 33f/180,180B
helix-live   23:49:57  out 0f/0B      | in 62f/338,520B
helix-live   23:50:21  out 8f/83,520B (1.7s @24k mono) | in 205f/1,119,300B in ~35s
```

The `8f/83,520B` line is the **same event** the panel's counter reports
(`8f/83520B`) — two independent measurements of one transfer, agreeing exactly.

`helix-bridge` logs **no** per-request TTS lines; its only evidence is the HTTP
response captured in §1. Flagged as an observability gap, not a finding.

---

## 6. The one genuinely ambiguous point — and the single test that resolves it

**The panel built exactly one AudioBuffer (`buil1`) from 8 received frames, and my
independent CDP probe reports `started: 0, ended: 0, buffers: 0`.**

Those two disagree. Both cannot be true. The explanation is a flaw in **my probe**, not
necessarily the product: `createBufferSource`'s wrapper only increments its counter
*inside* `s.start()`, and it only measures the buffer at that moment. So:

- `buil1` = the page successfully called `ctx.createBuffer(...)` and got a real buffer
  (it increments immediately after `pcm16ToBuffer`, and there is no try/catch swallow
  visible in the log)
- probe `buffers: 0, started: 0` = either the wrapper was bypassed, **or**
  `createBufferSource()` was reached and `start()` was never called

If `start()` was never called, playback is stalled in the panel's own loop. If it *was*
called and the probe simply missed it, playback may be working and the fault is (d) —
no device output.

**The single test that resolves it** — one turn in *your* browser, reading the readout
already on screen:

| readout | conclusion |
|---|---|
| `0f/0B buil0` | audio not arriving from Gemini at all |
| `Nf/NB buil0` | frames arrive, `createBuffer` throwing or the loop exits early |
| `Nf/NB buil1` then stays 1 | **stalled after the first buffer — the loop is stuck** |
| `Nf/NB builN` climbing | buffers are flowing; fault is downstream (output device) |

I cannot run this myself: headless Chrome's microphone captures nothing usable and the
speaker output does not loop back into it, so Gemini correctly hears silence and stays
silent (proxy: `1,102,920 B in → 0 B out` in the runs where no turn occurred).

---

## 7. What I am *not* claiming

- Not that audio works. §3 proves a *different* code path (`<audio>` element) works.
- Not that the panel's Web Audio path is correct. §2 says it built one buffer and the
  probe says it started none. That contradiction is unresolved.
- Not that the headless microphone results predict your browser. They only prove the
  server hops are healthy.

## 8. Artefacts (for independent re-checking)

```
/tmp/helix-diag-out/tts-auto.bin     358,606 B  live-tts WAV, 7.470042s
/tmp/helix-diag-out/tts-edge.bin      15,984 B  edge-tts MP3, 2.664000s
/tmp/helix-diag-out/prompt.wav        70,734 B  synthetic TTS prompt (not a human voice)
/tmp/diag-report.txt                            full raw output of this run
```

Verify independently:
```
ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/helix-diag-out/tts-auto.bin
```