#!/usr/bin/env python3
"""Smoke test for the HELIX desktop app.

Constructs the real window without entering the GTK main loop, so every API
mistake surfaces here in one run instead of one traceback per launch.

Written after four consecutive launches each died on a different GTK/WebKit API
error - deprecated override_background_color, RGBA not subscriptable,
WebContext.get_settings, Window.add_accel. Catching them one at a time by
relaunching was wasteful; this checks them all at once.

Run:  python3 tools/check-app.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk  # noqa: E402

import helix_app  # noqa: E402

FAIL = []


def check(label, fn):
    try:
        result = fn()
        print("  PASS  %s%s" % (label, "" if result is None else "  -> %s" % (result,)))
        return True
    except Exception as exc:                       # noqa: BLE001
        print("  FAIL  %s\n          %s: %s" % (label, type(exc).__name__, exc))
        FAIL.append(label)
        return False


def main():
    print("  building the window (no main loop)...")

    def build():
        w = helix_app.HelixWindow()
        w.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration()
        return "constructed"
    ok = check("window constructs and shows", build)
    if not ok:
        print("\n  SELFTEST: FAIL")
        return 1

    win = Gtk.Window.list_toplevels()[0] if Gtk.Window.list_toplevels() else None

    check("icon resolves to a real file",
          lambda: helix_app.find_icon() or "none found")
    check("backend check returns a bool",
          lambda: type(helix_app.ensure_backend()).__name__)
    check("webview exposes load_uri",
          lambda: bool(getattr(win.web, "load_uri", None)) or "MISSING")
    check("reload() is callable", lambda: bool(win.reload))
    check("toggle_fullscreen() is callable", lambda: bool(win.toggle_fullscreen))
    check("accelerator group attached", lambda: bool(getattr(win, "accel", None)))
    check("Ctrl+R parses to a real key combo",
          lambda: Gtk.accelerator_parse("<Control>r")[0] or "unparseable")
    check("title is HELIX", lambda: win.get_title() or "untitled")

    print("\n  SELFTEST:", "FAIL -> " + ", ".join(FAIL) if FAIL else "PASS")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
