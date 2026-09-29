# Voice Core

Spec §16.2 / §9. Central orb with Memory / Skills / Soul / Settings branches, and
barge-in (interrupt Helix mid-sentence).

## What's real here, and what is not

| | status |
|---|---|
| Orb rendering | **verified** — reused Three.js scene, bloom, 460x485 canvas, 0 console errors |
| Node graph + counts | **verified** — `/cortex`, counted off the filesystem |
| Transcript → Helix | **verified end-to-end** — `/say` → Kanban task → CEO ran it → replied `UI_PATH_OK` |
| CSRF guard on the write path | **verified** — with header 200, without 403 |
| **Barge-in** | **implemented, NOT verified** — no audio device in the dev environment |
| **Local/private STT** | **not done** — browser STT ships audio to Google, see below |

Do not record barge-in as working until it has been *heard* working on a machine with
a speaker and a microphone.

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

### Rebuilding it
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
Expect **~5s per command**. whisper pads every input to a 30-second encoder window, so
there is a **~4.4s fixed cost per request** regardless of clip length, plus ~0.26s per
second of audio. `whisper-server` was tried and **rejected**: it only skips the model
load (~0.5s) while holding ~300 MB resident permanently. Measured both ways before
discarding it.

If ~5s is too slow, the lever is a different model, not a resident server.
