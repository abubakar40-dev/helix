#!/usr/bin/env python3
"""Gemini Live voice proxy for Helix.

Why this exists: the Gemini API key must never be reachable from a browser, but
Live is a bidirectional WebSocket, so something has to sit in the middle. This
process holds the key and relays audio between the Voice Core page and Gemini
Live. It binds to 127.0.0.1 only.

Protocol, browser <-> here:
    binary  mic audio in   (PCM16 mono 16 kHz)
    binary  model audio out (PCM16 mono 24 kHz)
    text    JSON control: {"type": ready|state|transcript|reply|interrupted|error, ...}

Barge-in is handled by Gemini, not by us. The old Jarvis muted the mic for a
fixed window after TTS and discarded any transcript resembling the reply, which
is precisely why it could not be interrupted. Nothing here does that: mic audio
keeps flowing while the model speaks, and Google decides when to stop talking.
"""
import asyncio, json, os, re, sys

from google import genai
from google.genai import types
from websockets.asyncio.server import serve

MODEL = os.environ.get("HELIX_LIVE_MODEL", "gemini-3.8-live")
VOICE = os.environ.get("HELIX_LIVE_VOICE", "Charon")
HOST, PORT = "127.0.0.1", int(os.environ.get("HELIX_LIVE_PORT", "8788"))
ENV = os.path.expanduser("~/.hermes/.env")
RATE = 16000

SYSTEM = (
    "You are HELIX, the voice interface to a local multi-agent system on this "
    "machine. You are concise and direct: answer in one or two short sentences "
    "unless asked for detail. You can dispatch work to specialist agents "
    "(research, documents, system control, development, vision, messaging, "
    "career) but for anything long, say you are starting it rather than reading "
    "the whole result aloud. If the user talks over you, stop immediately and "
    "listen. You may speak English and Urdu. "
    # Lifted from the old Jarvis, which needed the same thing: the mic is muted
    # by default, so the model hears the wake word as an ordinary turn and would
    # otherwise answer it instead of waiting.
    "Wake-word mode: the microphone is muted by default. You will hear the "
    "words 'helix', 'hey helix' or 'hi helix' on their own. When you hear one "
    "of those activation cues, do not answer it — just say you are listening, "
    "briefly, and wait for the actual command."
)


def read_key():
    """Read GEMINI_API_KEY from the Hermes .env. Never logged, never returned."""
    try:
        with open(ENV, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^\s*GEMINI_API_KEY\s*=\s*(\S+)\s*$", line)
                if m:
                    return m.group(1).strip().strip('"\'')
    except OSError:
        pass
    return os.environ.get("GEMINI_API_KEY", "").strip()


async def handle(ws):
    key = read_key()
    if not key:
        await ws.send(json.dumps({"type": "error",
                                  "error": "GEMINI_API_KEY not set"}))
        await ws.close()
        return

    await ws.send(json.dumps({"type": "ready", "model": MODEL, "voice": VOICE}))
    # Per-connection counters. Without these the proxy is silent and a dead
    # audio path is indistinguishable from a quiet model.
    stats = {"in_frames": 0, "in_bytes": 0, "out_frames": 0, "out_bytes": 0}
    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription={},
        output_audio_transcription={},
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
        system_instruction=SYSTEM,
    )

    try:
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            await ws.send(json.dumps({"type": "state", "state": "connected"}))

            async def to_gemini():
                """Browser -> Gemini. Audio frames go straight through."""
                async for msg in ws:
                    if isinstance(msg, bytes):
                        stats["in_frames"] += 1
                        stats["in_bytes"] += len(msg)
                        if len(msg) < 2:
                            continue
                        try:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=msg, mime_type=f"audio/pcm;rate={RATE}"))
                        except Exception as e:            # session ended
                            return
                    elif isinstance(msg, str) and msg:
                        try:
                            await session.send_client_content(turns=msg)
                        except Exception:
                            return

            async def to_browser():
                """Gemini -> browser. Audio out as binary, everything else as JSON.

                `session.receive()` is a one-shot generator that ends after ONE model
                turn, so it must be re-entered per turn. Treating its end as "the
                conversation is over" tore the session down after the first reply.
                """
                while True:
                    try:
                        async for msg in session.receive():
                            sc = msg.server_content
                            if msg.data:
                                await ws.send(msg.data)
                            if sc is None:
                                continue
                            interim = getattr(sc, "interim_input_transcription", None)
                            if interim and interim.text:
                                await ws.send(json.dumps(
                                    {"type": "transcript", "final": False,
                                     "text": interim.text}))
                            it = getattr(sc, "input_transcription", None)
                            if it and it.text:
                                await ws.send(json.dumps(
                                    {"type": "transcript", "final": True,
                                     "text": it.text}))
                            ot = getattr(sc, "output_transcription", None)
                            if ot and ot.text:
                                await ws.send(json.dumps(
                                    {"type": "reply", "text": ot.text}))
                            if getattr(sc, "interrupted", False):
                                # Google's own signal that we were cut off mid-sentence.
                                await ws.send(json.dumps({"type": "interrupted"}))
                            if getattr(sc, "turn_complete", False):
                                await ws.send(json.dumps({"type": "state",
                                                          "state": "listening"}))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        return
            # Tear down only when the BROWSER goes away. The reader is long-lived and
            # must not be allowed to end the conversation, or the session dies right
            # after the first reply.
            reader = asyncio.create_task(to_browser())
            try:
                await to_gemini()          # returns when the browser disconnects
            finally:
                reader.cancel()
                secs = max(1, round(stats["in_bytes"] / 32000))
                print(f"[live] {stats['out_frames']} audio frames / {stats['out_bytes']:,} B "
                      f"out ({stats['out_bytes']/48000:.1f}s @24k mono) | "
                      f"{stats['in_frames']} frames / {stats['in_bytes']:,} B in ~{secs}s", flush=True)
    except Exception as e:
        try:
            await ws.send(json.dumps({"type": "error", "error": str(e)[:200]}))
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def main():
    if not read_key():
        print("FATAL: GEMINI_API_KEY not found", file=sys.stderr)
        return 1
    async with serve(handle, HOST, PORT, max_size=None):
        print(f"Gemini Live proxy on ws://{HOST}:{PORT}  model={MODEL} voice={VOICE}")
        await asyncio.Future()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
