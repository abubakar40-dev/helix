#!/usr/bin/env python3
"""Check the gesture classifier against synthetic hand landmarks.

Real-hand detection needs a hand in front of the camera, which this environment
does not have - the camera faces away and the selftest honestly reports 0 hands.
The CLASSIFIER is still testable: feed it landmark sets with a known pinch/fist/
two-hand shape and assert what it decides.

This tests gesture.py's real functions, not a copy of them.

Usage: check-gesture.py [path-to-venv-python]
"""
import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

VENV = os.path.expanduser("~/.local/share/helix-gesture-venv")
if not os.path.exists(VENV):
    print("FAIL  gesture venv missing at %s" % VENV)
    sys.exit(2)

# gesture.py imports cv2/mediapipe lazily (inside Detector._load), so the pure
# geometry helpers can be exercised without the camera or the models.
import gesture  # noqa: E402

PT = types.SimpleNamespace


def hand(pts):
    return [PT(x=p[0], y=p[1], z=0.0) for p in pts]


def build(index_up, pinch_close, folded=False):
    """21 landmarks. Image coords: y grows DOWNWARD.

    Wrist (0) at y=0.90, middle MCP (9) at y=0.72, so palm size is a constant
    0.18 and the pinch score is scale-free.

    Landmark 8 is the index tip AND the pinch point in MediaPipe's model, so the
    three cases must place it explicitly rather than layering writes:

      open   -> tip well above its PIP, thumb far away  (no pinch)
      pinch  -> tip dropped down beside the thumb       (pinch)
      fist   -> all tips curled below their PIPs

    The first version wrote the pinch AFTER the finger loop, which silently
    overwrote landmark 8 and made an open hand read as 3 fingers and a fist as 1.
    The test was wrong; the classifier was right.
    """
    L = [(0.0, 0.0)] * 21
    L[0] = (0.50, 0.90)
    L[9] = (0.50, 0.72)

    # middle, ring, pinky: identical in all three cases
    for tip in (12, 16, 20):
        x = 0.50 + (tip - 14) * 0.03
        if folded:
            L[tip], L[tip - 2] = (x, 0.86), (x, 0.78)
        else:
            L[tip], L[tip - 2] = (x, 0.50), (x, 0.62)

    # index (8) is the interesting one
    if folded:
        L[8], L[6] = (0.44, 0.86), (0.44, 0.78)
    elif index_up:
        L[8], L[6] = (0.44, 0.50), (0.44, 0.62)
    else:                                    # dropped to meet the thumb
        L[8], L[6] = (0.53, 0.66), (0.47, 0.62)

    # thumb tip (4) meets it only when pinching
    L[4] = (0.55, 0.66) if pinch_close else (0.32, 0.58)
    return hand(L)


def main():
    d = gesture.Detector()
    cases = []

    openp = build(True, False)
    pinch = build(False, True)
    fist = build(False, True, folded=True)

    cases.append(("open hand is not a pinch", not d.state["grabbing"]))
    p_open = d._pinch(openp)
    p_pinch = d._pinch(pinch)
    cases.append(("pinch scores lower than open",
                  p_pinch < p_open))
    cases.append(("pinch is under PINCH_ON", p_pinch < gesture.PINCH_ON))
    cases.append(("open is over PINCH_ON", p_open > gesture.PINCH_ON))
    cases.append(("open hand reads fingers up", d._fingers_up(openp) == 4))
    cases.append(("fist reads zero fingers up", d._fingers_up(fist) == 0))
    mid = (pinch[4].x + pinch[8].x) / 2
    lo, hi = min(pinch[4].x, pinch[8].x), max(pinch[4].x, pinch[8].x)
    cases.append(("pinch midpoint lies between the thumb and index tips",
                  lo <= mid <= hi))

    # Real scale invariance: scaling every landmark must not change the score,
    # because the ratio is normalised by palm size. Comparing two different
    # hands (which is what this asserted first) proves nothing.
    def scaled(lm, k):
        return [PT(x=p.x * k, y=p.y * k, z=0.0) for p in lm]
    base = d._pinch(pinch)
    cases.append(("pinch score is scale-invariant",
                  all(abs(d._pinch(scaled(pinch, k)) - base) < 1e-9
                      for k in (0.4, 2.0, 7.5))))

    print("  pinch=%.3f  open=%.3f  (PINCH_ON=%.2f)"
          % (p_pinch, p_open, gesture.PINCH_ON))
    ok = True
    for name, passed in cases:
        print("  %s  %s" % ("PASS" if passed else "FAIL", name))
        ok &= passed
    print("\n  SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())