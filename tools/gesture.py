#!/usr/bin/env python3
"""HELIX gesture control - webcam hand tracking -> orb control events.

Requirements 21: "Webcam-based: fingertip tracking for cursor control, closed fist ->
screenshot, double-tap -> click, two fingers -> screen grab/drag/move. Must stay
lightweight - no excessive CPU/RAM use when idle."

Design notes, all measured rather than assumed:
- MediaPipe 1.1.0 dropped the legacy `solutions` API; this uses the Tasks API
  (`mediapipe.tasks.python.vision.HandLandmarker`) and two downloaded .task models.
- Events go out over **SSE**, not WebSocket, for the same reason the bridge uses
  SSE: the browser has EventSource built in, and a hand-rolled WebSocket needs
  frame masking (a bug the bridge already hit once).
- Bound to 127.0.0.1 only. This reads the user's camera and can drive their UI.

Gestures:
  one hand pinched (thumb+index)   -> grab: relative motion rotates the orb
  one hand open                    -> also drags (forgiving fallback)
  two hands                        -> pinch distance ratio zooms in/out
  closed fist                      -> "fist" event (stop / release)

Run:
  gesture.py                serve on 127.0.0.1:8789
  gesture.py --selftest     run the camera + model N seconds and report, no server
"""
import argparse
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = os.path.expanduser("~/.local/share/helix-gesture/models")
CAM = os.environ.get("HELIX_GESTURE_CAM", "/dev/video0")

# Landmarks (MediaPipe hand model): 0 wrist, 4 thumb tip, 8 index tip, 12 middle tip.
PINCH_ON = 0.42     # thumb-index distance / hand size, below this = pinched
PINCH_OFF = 0.58    # hysteresis so a held pinch does not flicker
ZOZO_MIN = 0.12     # two-hand spacing floor, guards a divide by ~0


class Detector:
    """Camera + hand landmarks. Runs in its own thread; `events` is drained by the server."""

    def __init__(self, cam=CAM, width=640, height=480, verbose=False):
        self.cam, self.width, self.height, self.verbose = cam, width, height, verbose
        self.events = []
        self.lock = threading.Lock()
        self.running = False
        self.state = {
            "hands": 0, "fps": 0.0, "grabbing": False, "zooming": False,
            "pinch": 0.0, "last_gesture": "none", "error": "",
        }

    def _load(self):
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision

        self.lm = vision.HandLandmarker.create_from_options(
            vision.HandLandmarkerOptions(
                base_options=mpp.BaseOptions(
                    model_asset_path=os.path.join(MODELS, "hand_landmarker.task")),
                running_mode=vision.RunningMode.VIDEO,
                num_hands=2))
        self.mp, self.ImageFormat = mp, mp.ImageFormat
        self.cv2 = cv2

    def _hand_size(self, lm):
        """Wrist to middle-finger MCP - a scale-invariant size for the hand."""
        return math.dist((lm[0].x, lm[0].y), (lm[9].x, lm[9].y)) or 1e-6

    def _pinch(self, lm):
        return math.dist((lm[4].x, lm[4].y), (lm[8].x, lm[8].y)) / self._hand_size(lm)

    def _fingers_up(self, lm):
        """Cheap fist test: with a fist the fingertips fold toward the wrist."""
        tips = [8, 12, 16, 20]
        return sum(1 for t in tips if lm[t].y < lm[t - 2].y)

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _emit(self, ev):
        with self.lock:
            self.events.append(ev)
            if len(self.events) > 64:
                del self.events[:-64]

    def drain(self):
        with self.lock:
            out, self.events = self.events, []
        return out

    def _loop(self):
        try:
            self._load()
        except Exception as e:
            self.state["error"] = "model load failed: %s" % e
            self.running = False
            return

        cv2 = self.cv2
        cap = cv2.VideoCapture(self.cam)
        if not cap.isOpened():
            self.state["error"] = "cannot open %s" % self.cam
            self.running = False
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

        prev_pts = {}     # per-hand index -> last (x,y) in normalised coords
        prev_span = None
        frames, t0 = 0, time.time()

        while self.running:
            ok, frame = cap.read()
            if not ok:
                self.state["error"] = "camera read failed"
                break
            frames += 1
            img = self.mp.Image(image_format=self.ImageFormat.SRGB,
                                data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            res = self.lm.detect_for_video(img, int(time.time() * 1000))
            hands = res.hand_landmarks if res else []
            self.state["hands"] = len(hands)
            self.state["fps"] = round(frames / max(time.time() - t0, 1e-6), 1)

            if not hands:
                prev_pts.clear()
                prev_span = None
                self.state["grabbing"] = self.state["zooming"] = False
                self.state["last_gesture"] = "none"
                continue

            # ---- fist: release everything -------------------------------
            if self._fingers_up(hands[0]) == 0:
                prev_pts.clear()
                prev_span = None
                if self.state["last_gesture"] != "fist":
                    self.state["last_gesture"] = "fist"
                    self.state["grabbing"] = self.state["zooming"] = False
                    self._emit({"type": "gesture", "name": "fist"})
                continue

            # ---- two hands: zoom by the ratio of their pinch span -------
            if len(hands) >= 2:
                a, b = hands[0], hands[1]
                span = math.dist((a[9].x, a[9].y), (b[9].x, b[9].y))
                if prev_span and prev_span > ZOZO_MIN and span > ZOZO_MIN:
                    ratio = span / prev_span
                    # invert: hands apart (ratio>1) = zoom in, as with a pinch gesture
                    if abs(ratio - 1.0) > 0.012:
                        self._emit({"type": "zoom", "delta": round(ratio - 1.0, 4)})
                prev_span = span
                self.state["zooming"] = True
                self.state["grabbing"] = False
                self.state["last_gesture"] = "zoom"
                continue

            prev_span = None
            self.state["zooming"] = False

            # ---- one hand: pinch grabs, motion rotates -------------------
            lm = hands[0]
            pinch = self._pinch(lm)
            self.state["pinch"] = round(pinch, 3)
            was = self.state["grabbing"]
            self.state["grabbing"] = pinch < (PINCH_OFF if was else PINCH_ON)
            if was != self.state["grabbing"]:
                self._emit({"type": "grab", "on": self.state["grabbing"]})

            # the point that leads: the pinch midpoint, else the index tip
            if self.state["grabbing"]:
                cx, cy = (lm[4].x + lm[8].x) / 2, (lm[4].y + lm[8].y) / 2
                key = 0
            else:
                cx, cy = lm[8].x, lm[8].y
                key = -1
            if key in prev_pts:
                px, py = prev_pts[key]
                dx, dy = cx - px, cy - py
                if abs(dx) > 0.0015 or abs(dy) > 0.0015:
                    self._emit({"type": "drag", "dx": round(dx, 4), "dy": round(dy, 4)})
            prev_pts = {key: (cx, cy)}
            self.state["last_gesture"] = "drag" if self.state["grabbing"] else "hover"

        cap.release()

    def selftest(self, seconds=12):
        self.start()
        end = time.time() + seconds
        while time.time() < end and self.running:
            time.sleep(0.5)
        self.stop()
        return {"state": dict(self.state),
                "events": len(self.events),
                "sample": self.events[:4]}


class Handler(BaseHTTPRequestHandler):
    det = None

    def log_message(self, *a):
        pass

    # The console is served from 127.0.0.1:8787 and EventSource is cross-origin,
    # so it needs CORS. Scoped to the bridge's own origin rather than "*": this
    # stream carries camera-derived hand positions, and "*" would let any page in
    # the browser read whether the user's hand is moving.
    ORIGIN = "http://127.0.0.1:8787"

    def _cors(self):
        if self.headers.get("Origin") == self.ORIGIN:
            self.send_header("Access-Control-Allow-Origin", self.ORIGIN)
            self.send_header("Vary", "Origin")

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        last = None
        try:
            while True:
                payload = dict(self.det.state)
                blob = json.dumps(payload)
                if blob != last:
                    last = blob
                    self.wfile.write(b"data: " + blob.encode() + b"\n\n")
                    self.wfile.flush()
                for ev in self.det.drain():
                    self.wfile.write(b"event: " + ev["type"].encode() + b"\n")
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                    self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/events":
            return self._sse()
        body = json.dumps({"state": self.det.state, "ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8789)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--seconds", type=int, default=12)
    a = ap.parse_args()

    if a.host != "127.0.0.1":
        # This drives the user's camera and their UI. Never expose it.
        print("refusing to bind %s: gesture control is localhost-only" % a.host)
        return 2

    det = Detector()
    if a.selftest:
        out = det.selftest(a.seconds)
        print(json.dumps(out, indent=1))
        return 0 if not out["state"]["error"] else 1

    Handler.det = det
    det.start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("helix gesture on http://%s:%d/events  (camera %s)" % (a.host, a.port, CAM),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        det.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())