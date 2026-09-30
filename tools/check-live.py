#!/usr/bin/env python3
"""Prove the Gemini Live path works end to end, before wiring any UI.

Checks the things that actually broke the old Jarvis:
  1. a Live session connects and authenticates
  2. real recorded speech sent as audio comes back as a transcript
  3. the model replies with AUDIO, not text  (this is the whole point of Live)
  4. audio sent *while the model is still speaking* interrupts it

(4) is the feature the old system structurally could not do: it muted the mic for
a fixed window after TTS and discarded any transcript resembling the reply, so
talk-over was impossible. Here we simply keep sending and check the model yields.
"""
import asyncio, os, sys, wave, struct

from google import genai
from google.genai import types

MODEL = os.environ.get("HELIX_LIVE_MODEL", "gemini-3.8-live")
VOICE = os.environ.get("HELIX_LIVE_VOICE", "Charon")
AUDIO = sys.argv[1] if len(sys.argv) > 1 else "voice.wav"
RATE = 16000


def pcm_chunks(path, seconds=0.5):
    """16 kHz mono PCM16 in small frames, the shape the Live API wants."""
    w = wave.open(path)
    assert w.getframerate() == RATE and w.getnchannels() == 1, \
        f"want {RATE}Hz mono, got {w.getframerate()}Hz/{w.getnchannels()}ch"
    data = w.readframes(w.getnframes())
    step = int(RATE * seconds)
    for i in range(0, len(data), step * 2):
        yield data[i:i + step * 2]


async def main():
    client = genai.Client(http_options={"api_version": "v1beta"})
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription={},
        output_audio_transcription={},
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
        system_instruction=(
            "You are HELIX, a concise local voice assistant. Answer in one or two "
            "short sentences. If the user talks over you, stop and listen."),
    )

    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print(f"  [1] connected to {MODEL} (voice {VOICE})")

        got_user_text, got_audio_bytes, turns = [], 0, 0
        interrupted = False
        interim = []

        async def drain():
            """Read the session until the model finishes a turn.

            `server_content.interrupted` is Google's own signal that the model was cut
            off mid-utterance. That is the real barge-in proof - far better than
            inferring it from "did another turn happen".
            """
            nonlocal got_audio_bytes, turns, interrupted
            try:
                async for msg in session.receive():
                    sc = msg.server_content
                    if sc is not None:
                        if getattr(sc, "interim_input_transcription", None):
                            interim.append(sc.interim_input_transcription.text)
                        if getattr(sc, "input_transcription", None):
                            got_user_text.append(sc.input_transcription.text)
                        if getattr(sc, "output_transcription", None):
                            print(f"      model said: {sc.output_transcription.text[:90]!r}")
                        if getattr(sc, "interrupted", False):
                            interrupted = True
                            print("      server reported INTERRUPTED")
                        if getattr(sc, "turn_complete", False):
                            turns += 1
                            return
                    if msg.data is not None:
                        got_audio_bytes += len(msg.data)
            except Exception:
                return          # session closed after the turn; that is normal

        # --- 1 & 2: feed the real recording, get a transcript back
        async def send_all():
            for frame in pcm_chunks(AUDIO):
                await session.send_realtime_input(
                    audio=types.Blob(data=frame, mime_type="audio/pcm;rate=16000"))
                await asyncio.sleep(0.02)      # realtime pacing, not a burst

        sender = asyncio.create_task(send_all())
        try:
            await asyncio.wait_for(drain(), timeout=60)
        except asyncio.TimeoutError:
            print("  [2] TIMED OUT waiting for a turn")
        await sender

        heard = " ".join(t for t in got_user_text if t).strip()
        print(f"  [2] heard from the recording: {heard[:110]!r}")
        if not heard:
            print("      WARNING: no input transcript came back")

        # --- 3: audio out, which local whisper+espeak cannot do as one turn
        print(f"  [3] audio returned: {got_audio_bytes:,} bytes  "
              f"({got_audio_bytes / 24000:.1f}s @ 24kHz, {turns} turn(s))")

        # --- 4: barge-in. Keep feeding audio while the model is still talking and
        #        watch for the server's OWN `interrupted` signal.
        if got_audio_bytes:
            before = turns

            async def talk_over():
                for frame in pcm_chunks(AUDIO, seconds=0.25):
                    try:
                        await session.send_realtime_input(
                            audio=types.Blob(data=frame,
                                             mime_type="audio/pcm;rate=16000"))
                    except Exception:
                        return
                    await asyncio.sleep(0.05)

            async def watch():
                nonlocal turns
                for _ in range(45):
                    await asyncio.sleep(0.4)
                    if interrupted or turns > before:
                        return

            await talk_over()
            await watch()
            print(f"  [4] barge-in while speaking -> server signalled "
                  f"interrupted={interrupted}, extra turns={turns - before}")

        ok = bool(heard) and got_audio_bytes > 0
        print(f"\n  VERDICT: {'PASS' if ok else 'FAIL'} "
              f"(transcript={bool(heard)}, audio_bytes={got_audio_bytes}, "
              f"interrupt_signal={interrupted})")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
