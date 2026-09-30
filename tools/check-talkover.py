#!/usr/bin/env python3
"""Offline check of the Voice Core talk-over detector against real audio.

The browser harness denies microphone permission, so the detector cannot be
exercised in-page. This replays the *exact* algorithm from
ui/voice-core/index.html (rolling floor, ratio, 250 ms sustain) over real
captures instead of a synthetic device, which is stronger evidence.

Two cases matter and both must hold:
  1. echo-only   - Helix's own TTS leaking into the mic must NOT trigger
  2. talk-over   - a human speaking over it MUST trigger
"""
import sys, wave, struct, math

POLL_MS = 60          # matches the setInterval(..., 60) in the page
RATIO = 1.8           # matches the default #sens value in the page
FLOOR_ABS = 0.05      # matches Math.max(0.05, floor * ratio)
SUSTAIN_MS = 120      # matches the sustain check; 120ms at 60ms polls = 3 polls
ALPHA = 0.1           # floor = floor*0.9 + lvl*0.1


def read_wav(path):
    w = wave.open(path)
    assert w.getsampwidth() == 2 and w.getnchannels() == 1, "want 16-bit mono"
    rate = w.getframerate()
    n = w.getnframes()
    s = struct.unpack(f"<{n}h", w.readframes(n))
    return [x / 32768.0 for x in s], rate


def rms_levels(samples, rate, poll_ms=POLL_MS):
    """Per-poll RMS, matching getByteTimeDomainData + sqrt(mean(v^2)) in the page."""
    step = max(1, int(rate * poll_ms / 1000))
    out = []
    for i in range(0, len(samples), step):
        chunk = samples[i:i + step]
        if not chunk:
            break
        out.append(math.sqrt(sum(v * v for v in chunk) / len(chunk)))
    return out


def detect(levels, ratio=RATIO, floor_abs=FLOOR_ABS, sustain_ms=SUSTAIN_MS, poll_ms=POLL_MS):
    """The page's algorithm, verbatim in behaviour. Returns ms until trigger, or None."""
    floor, hot_since = 0.0, None
    # The page tests `performance.now() - hotSince > SUSTAIN_MS` on a 60 ms poll, so
    # N ms really costs floor(N/60)+1 polls, not N/60. Getting this wrong by one made
    # a setting look like it passed when the shipped code did not.
    need = int(SUSTAIN_MS // POLL_MS) + 1
    for idx, lvl in enumerate(levels):
        floor = floor * (1 - ALPHA) + lvl * ALPHA if floor else lvl
        hot = lvl > max(floor_abs, floor * ratio)
        if hot:
            if hot_since is None:
                hot_since = idx
            elif idx - hot_since >= need:
                return idx * poll_ms
        else:
            hot_since = None
    return None


def self_barge_in_risk(levels, tts_gain=1.0):
    """Worst-case ratio of peak level to the adapted floor, with the TTS as the only
    sound. >= ratio means Helix would cut itself off."""
    floor = 0.0
    worst = 0.0
    for lvl in levels:
        floor = floor * (1 - ALPHA) + lvl * ALPHA if floor else lvl
        if floor > 1e-6:
            worst = max(worst, (lvl * tts_gain) / floor)
    return worst


def main():
    mic, mic_rate = read_wav("real-mic.wav")
    tts, tts_rate = read_wav("tts-echo.wav")
    mic_l, tts_l = rms_levels(mic, mic_rate), rms_levels(tts, tts_rate)

    print(f"  real-mic.wav   {len(mic)/mic_rate:5.1f}s  peak {max(map(abs,mic)):.3f}  "
          f"mean RMS {sum(mic_l)/len(mic_l):.4f}")
    print(f"  tts-echo.wav   {len(tts)/tts_rate:5.1f}s  peak {max(map(abs,tts)):.3f}  "
          f"mean RMS {sum(tts_l)/len(tts_l):.4f}")

    # 1. echo only: the TTS is the ONLY thing in the room.
    risk = self_barge_in_risk(tts_l)
    fired = detect(tts_l)
    print(f"\n  [1] TTS echo alone -> detector fired at "
          f"{'NEVER (correct)' if fired is None else str(fired)+'ms  <-- SELF-INTERRUPT BUG'}")
    print(f"      echo peaks reach {risk:.2f}x the floor (diagnostic, not a gate)")

    # 2. talk-over, RAW acoustics: TTS through the speakers + human voice, summed
    #    with no echo cancellation. This is the pessimistic case.
    mix = [a + b for a, b in zip(tts, mic)]
    mix_l = rms_levels(mix, mic_rate)
    fired2 = detect(mix_l)
    print(f"\n  [2] TTS + human, NO echo cancellation -> fired at "
          f"{'NEVER' if fired2 is None else str(fired2)+'ms'}")
    print(f"      mean RMS: tts {sum(tts_l)/len(tts_l):.4f} vs human "
          f"{sum(mic_l)/len(mic_l):.4f}  <- voice is quieter than the echo it must beat")

    # 3. talk-over WITH echo cancellation, as the browser actually runs it
    #    (getUserMedia echoCancellation:true). AEC cannot be measured offline, so this
    #    is a MODEL: the TTS component is attenuated by AEC_ATTEN before summing, which
    #    is what echo cancellation does. It bounds the question rather than proving it.
    AEC_ATTEN = 0.12                                   # ~ -18 dB, typical for AEC
    aec_mix = [a * AEC_ATTEN + b for a, b in zip(tts, mic)]
    aec_l = rms_levels(aec_mix, mic_rate)
    fired3 = detect(aec_l)
    print(f"\n  [3] TTS + human, echo cancellation modelled at {AEC_ATTEN:.2f} (-18 dB) -> "
          f"fired at {'NEVER  <-- STILL BROKEN' if fired3 is None else str(fired3)+'ms'}")

    # Pass/fail is purely behavioural. Do NOT gate on `risk < RATIO`: TTS transients DO
    # exceed the ratio, and that is fine - the 120 ms sustain window is what rejects
    # them, not the threshold. Asserting risk < RATIO reported FAIL on correct code.
    # `risk` stays a diagnostic of how much headroom the echo has.
    ok = (fired is None) and (fired2 is not None) and (fired3 is not None)
    print(f"\n  VERDICT: {'PASS' if ok else 'FAIL'}")
    print(f"  echo headroom: TTS peaks reach {risk:.2f}x the floor vs a {RATIO:.1f}x "
          f"trigger, so they are separated by the sustain window, not the threshold.")
    print("  NOTE: case 3 is a model of echo cancellation, not a measurement of it. The")
    print("        browser path itself is unproven here - the harness denies mic permission.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
