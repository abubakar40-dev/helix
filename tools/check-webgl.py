#!/usr/bin/env python3
"""Does WebKit2GTK actually give us WebGL on this machine?

The console's orb is a Three.js WebGL scene. GTK's WebKit2GTK is not
Chromium: WebGL support depends on the build and on whether GPU acceleration
is available, and it fails silently - you get a black canvas rather than an
error. So it has to be measured, not assumed.

Loads a page that tries to create a real WebGL context, then reports what it
got back. Run:  python3 tools/check-webgl.py
"""
import os
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, GLib  # noqa: E402

PAGE = """
<html><body><script>
var out = {};
try {
  var c = document.createElement('canvas');
  var gl = c.getContext('webgl2') || c.getContext('webgl');
  if (!gl) { out.result = 'NO CONTEXT'; }
  else {
    var dbg = gl.getExtension('WEBGL_debug_renderer_info');
    out.result = 'OK';
    out.version = gl.getParameter(gl.VERSION);
    out.renderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : '(masked)';
    out.maxTexture = gl.getParameter(gl.MAX_TEXTURE_SIZE);
  }
} catch (e) { out.result = 'THREW: ' + e.message; }
document.title = JSON.stringify(out);
</script></body></html>
"""

RESULT = {}


def on_load(view, ev):
    # Only FINISHED. load-changed also fires COMMITTED, which arrives before the
    # inline script has run - reading the title there returned "{}" and looked
    # exactly like WebGL failing.
    if ev != WebKit2.LoadEvent.FINISHED:
        return
    # One extra turn of the loop so the title set by the script is visible.
    GLib.timeout_add(400, lambda: (RESULT.update(title=view.get_title()),
                                   Gtk.main_quit(), False)[2])


def main():
    w = WebKit2.WebView()
    w.connect("load-changed", on_load)
    w.load_html(PAGE, "about:blank")
    GLib.timeout_add_seconds(20, lambda: (Gtk.main_quit(), False)[1])
    Gtk.main()

    raw = RESULT.get("title") or "{}"
    print("  raw:", raw)
    try:
        import json
        d = json.loads(raw)
    except ValueError:
        print("\n  SELFTEST: FAIL (no usable title)")
        return 1

    ok = d.get("result") == "OK"
    print("\n  webgl        :", d.get("result"))
    print("  version      :", d.get("version"))
    print("  renderer     :", d.get("renderer"))
    print("  max texture  :", d.get("maxTexture"))
    print("\n  SELFTEST:", "PASS" if ok else "FAIL - the orb cannot render here")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
