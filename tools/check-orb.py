#!/usr/bin/env python3
"""Does the real console page actually render inside a WebKit2 webview?

check-webgl.py proved a WebGL context can be created. That is not the same as
"the Three.js orb draws": the orb still has to import over the network, compile
its shaders, and leave non-trivial pixels on the canvas. A WebGL page can pass
the first test and still show a black box.

So this loads the REAL console, then reads back from the page itself: whether
the orb scene initialised, and how bright the rendered canvas pixels actually
are. Sampling the canvas is the rendered output, not a hopeful status line.

Run:  python3 tools/check-orb.py
"""
import json
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, GLib  # noqa: E402

URL = "http://127.0.0.1:8787/console/"
SETTLE = 9          # seconds for three.js to import + build the scene

PROBE = """
(function () {
  var o = window.helixOrb, c = document.querySelector('#orb canvas');
  var t = document.querySelector('#townCanvas');
  var out = {
    orbExists: !!o,
    orbCanvas: c ? [c.width, c.height] : null,
    townCanvas: t ? [t.width, t.height] : null,
    headlines: document.querySelectorAll('#headlines .it').length,
    toolCards: document.querySelectorAll('#toolsView .call').length,
    nodes: document.querySelectorAll('.node').length,
    links: document.querySelectorAll('#links path').length,
    luma: null, nonBlack: null
  };
  try {
    var gl = c && (c.getContext('webgl2') || c.getContext('webgl'));
    if (gl) {
      var w = c.width, h = c.height, px = new Uint8Array(w * h * 4);
      gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
      var sum = 0, lit = 0, n = 0;
      for (var i = 0; i < px.length; i += 16) {
        var l = (px[i]*299 + px[i+1]*587 + px[i+2]*114) / 1000;
        sum += l; n++;
        if (l > 12) lit++;
      }
      out.luma = +(sum / n).toFixed(2);
      out.nonBlack = +(100 * lit / n).toFixed(1);
    } else { out.glError = 'no context'; }
  } catch (e) { out.glError = String(e); }
  return JSON.stringify(out);
})()
"""

state = {}


def finished(view, ev):
    if ev == WebKit2.LoadEvent.FINISHED:
        GLib.timeout_add_seconds(SETTLE, run_probe)


def run_probe():
    def got(web, task, *extra):
        # run_javascript's callback is invoked as (webview, result, user_data).
        # run_javascript_finish returns a JavascriptResult wrapper, not the value
        # itself - the string has to be pulled out of it explicitly.
        try:
            res = web.run_javascript_finish(task)
            val = res.get_js_value()
            raw = val.to_string()
            state["js"] = bytes(raw).decode() if isinstance(raw, GLib.Bytes) else str(raw)
        except Exception as exc:                     # noqa: BLE001
            state["err"] = str(exc)
        Gtk.main_quit()
        return False

    WEB.run_javascript(PROBE, None, got, None)
    return False


WEB = WebKit2.WebView()
# Connected BEFORE the load starts. Connecting it from inside run_probe meant
# run_probe was the only caller of finished and the only thing that could
# connect finished - a deadlock that returned "no result" instead of a crash.
WEB.connect("load-changed", finished)


def main():
    WEB.load_uri(URL)
    GLib.timeout_add_seconds(45, Gtk.main_quit)
    Gtk.main()

    raw = state.get("js")
    if isinstance(raw, GLib.Bytes):
        raw = bytes(raw).decode()
    if not raw:
        print("  no result:", state.get("err"))
        print("\n  SELFTEST: FAIL")
        return 1
    try:
        d = json.loads(raw)
    except ValueError:
        print("  unparseable:", raw[:300])
        print("\n  SELFTEST: FAIL")
        return 1

    print("  orb scene   :", d.get("orbExists"))
    print("  orb canvas  :", d.get("orbCanvas"))
    print("  town canvas :", d.get("townCanvas"))
    print("  node pills  :", d.get("nodes"))
    print("  connectors  :", d.get("links"), "paths")
    print("  headlines   :", d.get("headlines"), "items")
    print("  tool cards  :", d.get("toolCards"))
    print("  orb luma    :", d.get("luma"), "mean")
    print("  orb lit     :", d.get("nonBlack"), "% of sampled pixels")
    if d.get("glError"):
        print("  gl error    :", d["glError"])

    ok = bool(d.get("orbExists")) and (d.get("nonBlack") or 0) > 1.0
    print("\n  SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
