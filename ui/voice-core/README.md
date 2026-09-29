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

## Privacy warning, deliberately not hidden

Chrome's Web Speech API sends microphone audio to **Google** for recognition. §5.3
says keep data away from Google. The page shows a permanent banner about this. The
fix is a local backend — `drakulavich/kesha-voice-kit` (25 languages, no API key) or
Vosk — which is not installed yet. Do not use this panel for private speech until then.

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
- No Urdu / mixed-language handling — `recog.lang` is just the browser default
- Barge-in and mic capture need real audio hardware to verify
