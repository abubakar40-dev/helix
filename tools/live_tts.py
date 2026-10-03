#!/usr/bin/env python3
"""One-shot text -> audio via Gemini Live, for the bridge's TTS chain.

Deliberately separate from tools/live_server.py: that is the persistent
bidirectional session for the Voice Core panel. This is the *stateless* tier -
text in, one WAV out - so `/tts` can use Live as the highest-quality tier and
still fall back to edge-tts / Piper / espeak when it is unreachable.

  stdin : the text to speak
  argv  : --lang en|ur --voice NAME --out PATH
  stdout: nothing on success; diagnostics go to stderr
"""
import argparse, asyncio, os, re, struct, sys, wave

from google import genai
from google.genai import types

MODEL = os.environ.get("HELIX_LIVE_MODEL", "gemini-3.8-live")
OUT_RATE = 24000                       # RECEIVE_SAMPLE_RATE, same as the old Jarvis


def read_key():
    env = os.path.expanduser("~/.hermes/.env")
    try:
        with open(env, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^\s*GEMINI_API_KEY\s*=\s*(\S+)\s*$", line)
                if m:
                    return m.group(1).strip().strip('"\'')
    except OSError:
        pass
    return os.environ.get("GEMINI_API_KEY", "").strip()


async def speak(text, lang, voice):
    key = read_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
        system_instruction=(
            "Speak the text you are given naturally and at a normal pace. "
            "Do not add commentary, greetings or sign-offs. "
            + ("Reply in Urdu." if lang == "ur" else "")),
    )
    pcm = bytearray()
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        await session.send_client_content(
            turns=[types.Content(role="user", parts=[types.Part(text=text)])],
            turn_complete=True)
        while True:
            got = False
            async for msg in session.receive():
                if msg.data:
                    pcm += msg.data
                    got = True
                sc = msg.server_content
                if sc is not None and getattr(sc, "turn_complete", False):
                    return bytes(pcm)
            if not got:
                return bytes(pcm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="en")
    ap.add_argument("--voice", default="Charon")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    text = sys.stdin.read().strip()
    if not text:
        print("empty text", file=sys.stderr)
        return 1
    if len(text) > 2000:
        text = text[:2000]
    try:
        pcm = asyncio.run(speak(text, a.lang, a.voice))
    except Exception as e:
        print(f"live-tts: {type(e).__name__}: {e}"[:200], file=sys.stderr)
        return 1
    # Guard against "succeeded" with nothing in it: the old failure class was a
    # tier reporting success while producing no audio.
    if len(pcm) < 2048:
        print(f"live-tts: only {len(pcm)} bytes of audio", file=sys.stderr)
        return 1
    with wave.open(a.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(pcm)
    print(f"{len(pcm)} bytes, {len(pcm)/2/OUT_RATE:.2f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())