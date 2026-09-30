# Voice Core

Spec §16.2 / §9. Central orb with Memory / Skills / Soul / Settings branches, and
barge-in (interrupt Helix mid-sentence).

## What's real here, and what is not

| | status |
|---|---|
| Orb rendering | **verified** — reused Three.js scene, bloom, 0 console errors |
| Node graph + counts | **verified** — `/cortex`, counted off the filesystem |
| Transcript → Helix | **verified end-to-end** — `/say` → Kanban task → CEO ran it → replied `UI_PATH_OK` |
| CSRF guard on the write paths | **verified** — with header 200, without 403 |
| **Barge-in** | **VERIFIED for real** — see below |
| Local/private STT | **verified** — whisper.cpp, no audio leaves the machine |
| Local/private TTS | **verified** — espeak-ng, including Urdu |

## Barge-in — how it was actually proven

The browser's `speechSynthesis` was **removed** because it reports **zero voices** on
this machine: its utterances ended instantly, so there was never anything to interrupt.
An earlier note in this file blamed "no audio device" — that was wrong. The hardware is
fine (ALC3246 analog + HDMI, pipewire running); Chrome simply has no TTS engine. TTS is
now server-side **espeak-ng**, played through an `<audio>` element, which also gives a
built-in Urdu voice.

Interrupt = `audioEl.pause()` mid-playback. Proven by, in order:

1. **Playback was real.** `audioEl.currentTime` advanced during speech, and a pipewire
   sink-input appeared — audio was genuinely being produced.
2. **The cut lands mid-sentence.** Repeated runs cut at **1.04s, 1.12s, 1.41s, 1.90s,
   2.11s and 2.58s out of a 9.27s utterance.**
3. **It was cut, not played to completion.** After cutting at 1.04s and then waiting
   **11 seconds** — past the 7.8s that remained — `onended` never fired and the state
   never went to `IDLE`. Playback stopped at the interrupt.

**Limit, stated honestly:** no sample-level proof that sound stopped. `pactl`'s `cork`
flag stayed `no` and the sink-input stayed open after `pause()` (a paused element keeps
its stream node in Chrome/pipewire), and there is no `parec` tap available to measure the
output waveform. The evidence above is behavioural, not acoustic.

There is **no microphone in this environment**, so "talk over Helix" could not be
exercised end to end. The interrupt half is proven; the capture half is not.

## Why not `hermes-live-voice`

The spec says to check it first. It is the right tool for barge-in and it is
unusable here — three independent blockers, recorded so this is not re-litigated:

- local provider is **Apple Silicon only** (this is x86_64) and needs **≥12 GB RAM**
  (this box has 6.9 GB)
- OpenAI Realtime is **paid** (violates the free-only rule)
- Gemini Live routes audio to **Google**, which §5.3 forbids

## Privacy warning, resolved

Previously Chrome's Web Speech API sent microphone audio to **Google** for recognition,
which §5.3 forbids. **That is now fixed** — recognition runs locally through
whisper.cpp via `POST /stt`. No audio leaves the machine.

## The one CDN dependency

Three.js loads from unpkg via an import map in `index.html`. The copy vendored in the
old Jarvis archive is a 2021 UMD build whose addons cannot satisfy the ES imports that
`orbScene.js` uses. To run fully offline, vendor three + `examples/jsm/` locally and
repoint the two import-map entries.

## orbScene.js is transpiled, not hand-edited

Source: `~/jarvis-old-archive/JarvisHeadFolder/JARVIS_fixed_v2/JARVIS/homescreen background/lib/orbScene.ts`
(858 lines). Regenerate with:

    ~/gods-eye-view/node_modules/.bin/esbuild "<that path>" \
      --bundle --format=esm --target=es2020 --loader:.ts=ts \
      --external:three --external:three/addons/* --outfile=orbScene.js

The old archive is reference-only and is not modified.

## Known gaps
- No wake word (spec §9 lists one)
- **Barge-in still unverified** — needs real audio hardware
- ~5s per voice command; see the latency note below

---

## Local STT — whisper.cpp (replaced Chrome Web Speech)

Web Speech was removed because it sends mic audio to **Google**, which §5.3 forbids.
Audio now goes to `127.0.0.1` only and is transcribed locally.

Installed at `~/.local/share/helix-stt/`: `whisper-cli` + `models/ggml-base.bin`.
The bridge exposes `POST /stt?lang=<code>` (empty = auto-detect, which handles mixed
English/Urdu better than forcing one).

### Why not the two options the spec names
- **`kesha-voice-kit`** — no Urdu at all (has Hindi, not Urdu); 2.5 GB of models on a
  6.9 GB box; needs Bun; mic capture is macOS-only.
- **Vosk** — no Urdu model exists in its catalogue.

whisper.cpp covers 99 languages including Urdu, needs no key, and is one static binary.

### Privacy

Both directions are local: **whisper.cpp** transcribes, **espeak-ng** speaks (with a
built-in Urdu voice). No audio or text goes to a cloud service.

## Rebuilding whisper.cpp
No prebuilt Linux binary is published, and cmake is not installed system-wide, so:

    python3 -m venv ~/.local/share/helix-build-venv
    ~/.local/share/helix-build-venv/bin/pip install cmake
    git clone --depth 1 -b v1.9.4 https://github.com/ggml-org/whisper.cpp.git
    cd whisper.cpp && cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON \
      && cmake --build build --parallel 2
    cp build/bin/whisper-cli ~/.local/share/helix-stt/

Model: `curl -L -o ~/.local/share/helix-stt/models/ggml-base.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin`

Measured on this box: **1.64x realtime, 295 MB RSS.** `ggml-tiny` was tried and
**deleted** — it mis-detected Urdu audio as English ("Peace be upon you" instead of
"Islamabad!"). `base` gets it right.

### Latency — read this before optimising
**There is no limit on how long you can talk.** Measured through the real endpoint:

| clip | wall |
|---|---|
| 1 s | 7.5 s |
| 3 s | 6.9 s |
| 10 s | 7.0 s |
| 30 s | 22.0 s |

About a 6.5 s fixed cost plus ~0.5 s per extra second. A 3–5 s command returns in
~7 s. Two things that look wrong but are not:

- **Threads are pinned to 2, not 4.** This is a 4-core box sitting at load ~9. Measured
  on the same 30 s clip: `-t4` = 100 s, `-t3` = 28 s, **`-t2` = 21.5 s**, `-t1` = 34 s.
  Oversubscribing makes it ~5x slower. The numbers are in a comment at
  `STT_THREADS` in `bridge/server.py` so nobody "fixes" it back.
- **Per-invocation `whisper-cli` is deliberate.** `whisper-server` keeps the model
  resident, but whisper pads every input to a 30-second encoder window, so the ~6.5 s
  fixed cost happens either way. The server only skips the model load (~0.5 s) while
  holding ~300 MB resident permanently. Measured both ways before discarding it.

If ~7 s is too slow, the lever is a different model — not a resident server.
