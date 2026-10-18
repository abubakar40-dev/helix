#!/usr/bin/env python3
"""HELIX - desktop application for the console.

A real, separate application: its own process, its own GTK window, its own icon
in the dock and taskbar. Not a browser window with the chrome hidden.

Why GTK + WebKit2GTK rather than Electron or a second GUI toolkit:
  * both `python3-gi` and `gir1.2-webkit2-4.1` are ALREADY installed as part of
    the standard Ubuntu desktop stack, so this adds zero dependencies;
  * WebKit is far lighter than a bundled Chromium - the console is one page, and
    Chrome held ~400MB per window on this 6.9GB box;
  * Electron would mean shipping a second browser engine to render a frontend
    that already exists.

Quit = close the window. Reopening reloads the UI; the bridge sends
`Cache-Control: no-cache, must-revalidate`, so nothing comes from a stale cache.

Keys: Ctrl+R reload, Ctrl+Q quit, F11 fullscreen, Ctrl+Shift+I dev tools.
"""
import os
import signal
import subprocess
import sys
import time
import urllib.request

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, Gdk, GLib, GdkPixbuf  # noqa: E402

URL = "http://127.0.0.1:8787/console/"
APP_NAME = "HELIX"
HERE = os.path.dirname(os.path.abspath(__file__))
UNITS = ("helix-bridge", "helix-globe", "helix-live")
LOG = os.path.expanduser("~/.local/share/helix-app.log")
GESTURE_OFF_URL = "http://127.0.0.1:8787/gesture"

# Window background matches --bg in the console CSS, so there is no white flash
# between the window opening and the page painting.
BG = "#060910"


def say(msg):
    try:
        with open(LOG, "a") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def ensure_backend():
    """Start any stopped user units, then wait for the bridge to really answer.

    Polling rather than sleeping a fixed amount: systemctl returns long before
    the socket is listening, and a fixed sleep opens a blank window on a slow
    start. If the bridge never answers we still open, because the console page
    renders its own "bridge down" state, which beats silently doing nothing.
    """
    for unit in UNITS:
        try:
            if subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit],
                              capture_output=True).returncode != 0:
                say("starting %s" % unit)
                subprocess.run(["systemctl", "--user", "start", unit],
                               capture_output=True)
        except OSError as exc:
            say("systemctl failed for %s: %s" % (unit, exc))

    for _ in range(40):
        try:
            urllib.request.urlopen(URL, timeout=2).read(1)
            say("bridge is up")
            return True
        except Exception:
            time.sleep(0.5)
    say("bridge did not answer %s; opening anyway" % URL)
    return False


def find_icon():
    """Largest installed HELIX icon, so the window and dock use the real logo."""
    base = os.path.expanduser("~/.local/share/icons/hicolor")
    for size in (512, 256, 128, 64, 48, 32):
        for cand in (os.path.join(base, "%dx%d/apps/helix-console.png" % (size, size)),
                     os.path.join(HERE, "helix-%d.png" % size)):
            if os.path.exists(cand):
                return cand
    svg = os.path.join(HERE, "helix-icon.svg")
    return svg if os.path.exists(svg) else None


class HelixWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_NAME)
        self.set_default_size(1509, 812)
        self.set_size_request(900, 600)
        self.set_position(Gtk.WindowPosition.CENTER)

        # Paint the window background to match --bg in the console CSS, so there
        # is no white flash between the window mapping and the page painting.
        # Done with a CssProvider: Gtk.Widget.override_background_color is
        # deprecated and its arg handling rejected a copied Gdk.RGBA.
        css = Gtk.CssProvider()
        css.load_from_data(b"window.helixbg { background-color: %s; }" % BG.encode())
        self.get_style_context().add_class("helixbg")
        self.get_style_context().add_provider(
            css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        icon = find_icon()
        if icon:
            try:
                self.set_icon(GdkPixbuf.Pixbuf.new_from_file(icon))
            except Exception as exc:
                say("icon load failed: %s" % exc)

        self.web = WebKit2.WebView()
        # Settings hang off the WebView, not the WebContext - asking the context
        # raises AttributeError. Dev tools on so Ctrl+Shift+I works; the rest are
        # best-effort because not every WebKit2GTK build exposes all of them, and
        # an unsupported cosmetic call must not stop the app from opening.
        s = self.web.get_settings()
        for label, apply in (
            ("developer extras", lambda: s.set_enable_developer_extras(True)),
            ("smooth scrolling", lambda: s.set_enable_smooth_scrolling(True)),
        ):
            try:
                apply()
            except (AttributeError, TypeError) as exc:
                say("setting unavailable (%s): %s" % (label, exc))
        self.add(self.web)

        # GTK3 has neither Window.add_accel nor AccelGroup.accel_activate, and
        # add_accelerator wants a real signal name on the widget (it is for menu
        # action paths), not an arbitrary label. For window-level shortcuts
        # AccelGroup.connect alone is the whole job.
        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        self.accel = accel
        for combo, cb in (
            ("<Control>r", self.reload),
            ("F5", self.reload),
            ("<Control>q", lambda *_: self.destroy()),
        ):
            keyval, mods = Gtk.accelerator_parse(combo)
            if not keyval:
                say("accelerator not parseable: %s" % combo)
                continue
            accel.connect(keyval, mods, Gtk.AccelFlags.VISIBLE, cb)
        self.connect("destroy", Gtk.main_quit)
        self.connect("delete-event", lambda *_: (self.destroy(), False)[1])

    def reload(self, *_):
        say("reload requested")
        self.web.reload()

    def toggle_fullscreen(self, *_):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()


def start_gesture():
    """Run gesture control as a CHILD of this app, not as a login service.

    It used to be `helix-gesture.service` under graphical-session.target, which
    meant it held /dev/video0 from login until logout - the camera stayed live
    long after HELIX was closed. That was reported as "it is still accessing the
    camera even though I closed the app".

    Owning it as a child process makes the lifetime structural: when this process
    exits, the gesture process does too, with no systemd unit left enabled to
    resurrect it.

    Returns the Popen handle, or None if it could not start.
    """
    script = os.path.join(HERE, "gesture.py")
    if not os.path.exists(script):
        say("gesture.py not found; gesture control unavailable")
        return None
    # Must use the gesture venv's interpreter, not sys.executable. The app runs
    # on /usr/bin/python3, which has PyGObject but not mediapipe - launching with
    # it gave "model load failed: No module named 'mediapipe'" and a silent
    # fps 0.0 detector that never touched the camera.
    venv_python = os.path.expanduser(
        "~/.local/share/helix-gesture-venv/bin/python")
    python = venv_python if os.path.exists(venv_python) else sys.executable
    if python == sys.executable:
        say("gesture venv missing; hand tracking will not work")
    try:
        proc = subprocess.Popen(
            [python, script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)      # own process group: kill the whole tree
        say("gesture control started as child pid %d with %s"
            % (proc.pid, python))
        return proc
    except OSError as exc:
        say("gesture control failed to start: %s" % exc)
        return None


def stop_gesture(proc):
    """Release the camera. Group-kill, so ffmpeg/whisper children go too."""
    if proc is None or proc.poll() is not None:
        return
    say("releasing camera (killing gesture pid %d)" % proc.pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except OSError:
                pass
        try:
            proc.wait(timeout=4)
            return
        except subprocess.TimeoutExpired:
            continue
    say("gesture did not exit")


def main():
    # Own process/app identity: without this GNOME groups the window under
    # whatever generic binary name and shows no matching dock icon.
    GLib.set_prgname("helix-console")
    GLib.set_application_name(APP_NAME)

    say("--- starting %s ---" % APP_NAME)
    ensure_backend()

    # Gesture is NOT started automatically. It used to run from login to logout
    # and hold the camera the whole time; now the console's GESTURE button starts
    # it through the bridge. Opening the app must not touch the camera.
    gesture = None

    def release_camera():
        """Backstop: even if something re-enabled gesture, quitting releases it.

        The bridge owns the gesture process now, so this asks it to stop. The
        local child handle is stopped too for the case where the bridge is down.
        """
        stop_gesture(gesture)
        try:
            req = urllib.request.Request(
                GESTURE_OFF_URL, data=b'{"on":false}',
                headers={"X-Helix-Voice": "1", "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=4).read()
            say("camera released via bridge on exit")
        except Exception as exc:               # noqa: BLE001
            say("camera release call failed: %s" % exc)

    def shutdown(*_):
        # Runs on window close AND on SIGTERM/SIGINT, so closing the window,
        # killing the app, or logging out all release the camera.
        release_camera()
        if Gtk.main_level() > 0:
            Gtk.main_quit()
        # Measured: Gtk.main_quit() released the camera but the process stayed
        # alive - WebKit's web-process keeps the loop referenced. The camera is
        # already released by this point, so exit hard rather than linger as a
        # windowless process holding nothing but memory.
        GLib.timeout_add(500, lambda: (os._exit(0), False)[1])

    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, shutdown)

    win = HelixWindow()
    win.connect("destroy", shutdown)
    win.show_all()
    win.web.load_uri(URL)
    Gtk.main()
    shutdown()
    say("exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
