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
| **Barge-in** | **VERIFIED for real** — on both TTS engines, see below |
| Recognition | **verified** — whisper.cpp, no audio leaves the machine |
| TTS online-first | **verified** — edge-tts when online |
| TTS offline fallback | **VERIFIED with no internet** — Piper, see below |

## Barge-in

Interrupt = `audioEl.pause()` mid-playback on a server-side TTS `<audio>` element.
Verified on both engines (`edge-tts` cut at 1.66s of 9.24s, `piper` at 1.39s of 9.30s).
The full evidence chain, the misdiagnoses and the honest limits are in
`docs/PROGRESS.md` — read it there rather than here, it is the canonical record.

The one thing to keep in mind: there is **no microphone in this environment**, so "talk
over Helix" has never been exercised end to end. The interrupt half is proven; the
capture half is not.

## Why not `hermes-live-voice`

Three independent blockers, so this is not re-litigated: local provider is **Apple
Silicon only** (this is x86_64) and needs **≥12 GB RAM** (this box has 6.9 GB);
OpenAI Realtime is **paid**; Gemini Live routes audio to **Google**, which §5.3
forbids. Details in `docs/PROGRESS.md`.

## Privacy

Recognition is always local: **whisper.cpp**, nothing leaves the machine.

**Speaking is online-first, by request.** Text goes to Microsoft's free Edge
read-aloud endpoint (no key, no account) and falls back to **Piper, which is fully
offline**, the moment there is no internet. Use the "Force offline" selector to keep
every word local regardless. Urdu offline falls to espeak-ng, since Piper has no Urdu
model.

| | online | offline |
|---|---|---|
| English | edge-tts `en-GB-SoniaNeural` | Piper `en_GB-alba-medium` |
| Urdu | edge-tts `ur-PK-UzmaNeural` | espeak-ng `ur` |
| last resort | — | espeak-ng (cannot fail on a normal install) |

The response carries `X-Helix-TTS-Engine`, and the page logs which engine actually
spoke, so the fallback is visible rather than assumed.

## The one CDN dependency

Three.js loads from unpkg via an import map in `index.html`. The copy vendored in the
old Jarvis archive is a 2021 UMD build whose addons cannot satisfy the ES imports that
`orbScene.js` uses. To run fully offline, vendor three + `examples/jsm/` locally and
repoint the two import-map entries.

## orbScene.js is generated, not hand-edited

It is **gitignored** — build it once after cloning. Source:
`~/jarvis-old-archive/JarvisHeadFolder/JARVIS_fixed_v2/JARVIS/homescreen background/lib/orbScene.ts`
(858 lines). Regenerate with:

    ~/gods-eye-view/node_modules/.bin/esbuild "<that path>" \
      --bundle --format=esm --target=es2020 --loader:.ts=ts \
      --external:three --external:three/addons/* --outfile=orbScene.js

Takes ~10ms. The old archive is reference-only and is not modified.

## Known gaps
- No wake word (spec §9 lists one)
- **No microphone in this environment**, so talk-over-Helix is untested end to end
- ~7s per voice command; see the latency note below

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
