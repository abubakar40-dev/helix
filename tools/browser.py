#!/usr/bin/env python3
"""Drive Helix's own Chrome, on a private copy of the logged-in profile.

Why this exists rather than the harness browser: the harness browser denies
getUserMedia and cannot reach the human's existing logins, and this whole feature
depends on being logged in. Brave is snap-confined (its profile lives under
~/snap/brave/<rev>/.config/...), Chrome's does not.

The human's live profile is never opened. Chrome locks a profile directory, and
two instances on one profile corrupts it — measured: a second launch died with
"ProcessSingleton … Aborting now to avoid profile corruption". So the cookies and
preferences are COPIED to a scratch dir and Chrome runs there. The copy is what
gets logged in; the original is read-only input.

Per-browser routing, because the human asked for it explicitly:
    youtube, instagram  -> Brave
    everything else     -> Chrome

  browser open linkedin.com            # routes by rule
  browser open youtube.com             # -> Brave
  browser open --chrome linkedin.com   # force
  browser shot /tmp/x.png              # screenshot the live tab
  browser text                          # read the page as text
  browser eval "document.title"        # run JS in the page
  browser click "text=Sign in"         # click by visible text
  browser type "hello"                 # type into the focused field
  browser key Enter
  browser status                        # is it up, which browser, logged in where

Stdlib only: json, socket, base64, struct, time, subprocess, shutil, urllib.
"""
import argparse, base64, json, os, re, shutil, socket, struct, subprocess
import sys, time, urllib.request

PORT = 9333
CDP = f"http://127.0.0.1:{PORT}"
LAUNCH_TIMEOUT = 45

# The human's rule, verified against real cookies: Brave holds the YouTube and
# Instagram logins (23 and 8 cookies), Chrome holds LinkedIn and GitHub.
ROUTES = {
    "youtube.com": "brave",
    "instagram.com": "brave",
}

PROFILES = {
    "chrome": "~/.config/google-chrome",
    "brave": "~/snap/brave/current/.config/BraveSoftware/Brave-Browser",
}
# Snap Brave keeps a revision dir (690, 686, …); resolve the real one.
# Which Chrome sub-profile carries which login. Measured by reading SESSION
# cookies, not by counting cookies: a profile full of analytics cookies looks
# logged in and is not. `chrome/Default` has 7 linkedin cookies but ZERO session
# cookies (li_at/JSESSIONID/liap) — it is not logged in. `Profile 1` has 3 session
# cookies and is. Counting cookies would have picked the wrong profile.
# Which Chrome sub-profile to drive, per site.
#
# Measured, and the measurement contradicted the obvious assumption. Counting
# cookies says `chrome/Default` is the logged-in LinkedIn profile (7 cookies vs
# Profile 1's 23 — no, Profile 1 has more, but Default is the one Chrome opens by
# default). Reading SESSION cookies says otherwise, and session cookies are what
# actually authenticate:
#
#   chrome/Default   linkedin: 7 cookies, 0 session cookies  -> NOT logged in
#   chrome/Profile 1 linkedin: 23 cookies, 3 session cookies -> logged in once,
#                                                            cookies last written
#                                                            2026-09-26, and that
#                                                            session is now stale
#
# So `linkedin.com -> Profile 1` is recorded, but the stale session means LinkedIn
# redirects to /login/ either way. `browser open` reports the real outcome so this
# is never mistaken for working; see `browser status` for the session-cookie check.
CHROME_SUBPROFILE = {"linkedin.com": "Profile 1", "github.com": "Profile 1"}
DEFAULT_SUBPROFILE = "Profile 1"
# Sites whose session Helix has confirmed working end-to-end.
VERIFIED_LOGIN = {"youtube.com", "instagram.com"}
BRAVE_BIN = "brave"
CHROME_BIN = "google-chrome"
SCRATCH = "/tmp/helix-browser"
# Copied because these carry the login. Everything else in the profile is a
# preference we do not need and that could carry stale state.
COPIED = ["Cookies", "Cookies-journal", "Login Data", "Login Data For Account",
          "Preferences", "Web Data", "Local State"]


def route_for(url):
    for host, browser in ROUTES.items():
        if host in url:
            return browser
    return "chrome"


def real_profile(browser):
    """Snap revision dirs change (690 -> 691), so resolve rather than hardcode."""
    base = os.path.expanduser(PROFILES[browser])
    if browser == "brave":
        cur = os.path.expanduser("~/snap/brave/current/.config/BraveSoftware/Brave-Browser")
        if os.path.isdir(cur):
            return cur
    return base


def scratch_for(browser):
    return os.path.join(SCRATCH, browser)


def prepare(browser, url=""):
    """Copy the login-bearing files out of the live profile. Read-only on the
    original; the copy is what Chrome/Brave actually opens."""
    src = real_profile(browser)
    dst = scratch_for(browser)
    if not os.path.isdir(src):
        raise SystemExit(f"  no {browser} profile at {src}")
    os.makedirs(dst, exist_ok=True)
    # Chrome keeps cookies under <profile>/<sub>/, so the sub-profile that holds
    # the login must be flattened into the scratch dir's Default/.
    sub = subprofile_for(browser, url) or "Default"
    fromdir = os.path.join(src, sub)
    if not os.path.isdir(fromdir):
        fromdir = os.path.join(src, "Default")
    copied = 0
    for name in COPIED:
        # Local State lives at the profile root, the rest inside the sub-profile.
        for cand in (os.path.join(src, name), os.path.join(fromdir, name)):
            if os.path.exists(cand):
                shutil.copy2(cand, os.path.join(dst, name))
                copied += 1
                break
    if not copied:
        raise SystemExit(f"  copied 0 files from {src} — is {browser} installed?")
    return dst, copied


def alive():
    try:
        urllib.request.urlopen(CDP + "/json/version", timeout=4).read()
        return True
    except Exception:
        return False


def launch(browser, url=""):
    """Start the browser headless with CDP on the scratch profile."""
    if alive():
        return True
    dst, n = prepare(browser, url)
    binp = CHROME_BIN if browser == "chrome" else BRAVE_BIN
    cmd = [binp, "--headless=new", f"--remote-debugging-port={PORT}",
           f"--user-data-dir={dst}", "--no-first-run", "--no-default-browser-check",
           "--disable-gpu", "--no-sandbox", "--window-size=1280,1000",
           "about:blank"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < LAUNCH_TIMEOUT:
        if alive():
            return True
        time.sleep(1)
    return False


class Tab:
    """Minimal CDP websocket client. stdlib has no websocket, and the alternative
    was adding a dependency for one function."""

    def __init__(self):
        tabs = json.loads(urllib.request.urlopen(CDP + "/json/list", timeout=10).read())
        pages = [t for t in tabs if t["type"] == "page"]
        if not pages:
            raise SystemExit("  no page target")
        self.id = pages[0]["id"]
        key = base64.b64encode(os.urandom(16)).decode()
        self.s = socket.create_connection(("127.0.0.1", PORT), timeout=60)
        self.s.send((f"GET /devtools/page/{self.id} HTTP/1.1\r\n"
                     f"Host: 127.0.0.1:{PORT}\r\nUpgrade: websocket\r\n"
                     f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                     f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        self.s.settimeout(60)
        # Consume the 101 handshake off the wire. Leaving it in the buffer made
        # every later read start mid-response: measured, the first command's buffer
        # began "HTTP/1.1 101 …" and the JSON id could never be found, so evaluate
        # silently returned None with no error. Read headers until the blank line.
        self._buf = b""
        t0 = time.time()
        while b"\r\n\r\n" not in self._buf and time.time() - t0 < 15:
            try:
                self._buf += self.s.recv(4096)
            except socket.timeout:
                break
        if b"\r\n\r\n" in self._buf:
            self._buf = self._buf.split(b"\r\n\r\n", 1)[1]
        self._n = 0

    def _frame(self, msg):
        """Encode a client->server text frame.

        RFC 6455 §5.3: every client frame MUST be masked with a random 4-byte key.
        The server computes `mask ^ key` over the payload to read it, and silently
        drops anything unmasked. Measured symptom of getting this wrong: commands
        returned {} and evaluate came back None, with no error anywhere — the
        server simply never saw the request.
        """
        payload = msg.encode()
        n = len(payload)
        h = bytearray([0x81])
        if n < 126:
            h.append(0x80 | n)
        elif n < 65536:
            h.append(0x80 | 126)
            h.extend(struct.pack(">H", n))
        else:
            h.append(0x80 | 127)
            h.extend(struct.pack(">Q", n))
        key = os.urandom(4)
        h.extend(key)
        masked = bytes(payload[i] ^ key[i % 4] for i in range(n))
        return bytes(h) + masked

    def _read_until(self, needle, wait):
        """Accumulate frames until `needle` appears.

        CDP interleaves events with responses and splits payloads across frames, so
        a naive one-recv-per-response read desynchronises and then dies on a closed
        socket. Measured: the first version raised BrokenPipeError on the second
        command because the server had already sent frames we were not draining.
        The buffer persists on self so bytes are never dropped between commands.
        """
        t0 = time.time()
        while time.time() - t0 < wait:
            if needle in self._buf:
                break
            try:
                c = self.s.recv(262144)
            except socket.timeout:
                break
            except OSError:
                break
            if not c:
                break
            self._buf += c
        return self._buf

    @staticmethod
    def _parse(txt, i):
        """Pull the JSON object that follows `"id":i` out of a frame buffer.

        Brace-counting from the first "{" after the id lands INSIDE the object,
        because that first "{" belongs to the nested "result" member — so the
        count returns early and the outer wrapper is lost. Measured: cmd() came
        back as {"result":{...}} with the id stripped, one level short, which is
        why every evaluate returned None. Scanning from the object start that
        PRECEDES the id gives the whole envelope, and falling back to the id-based
        start keeps it working if the layout ever changes.
        """
        k = txt.find(f'"id":{i}')
        if k < 0:
            return {}
        for start in (txt.rfind("{", 0, k), txt.find("{", k)):
            if start < 0:
                continue
            depth, instr, esc = 0, False, False
            for j in range(start, len(txt)):
                ch = txt[j]
                if instr:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        instr = False
                    continue
                if ch == '"':
                    instr = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            got = json.loads(txt[start:j + 1])
                        except Exception:
                            continue
                        if "id" in got:
                            return got
                        return {"id": i, **got}
        return {}

    def cmd(self, method, params=None, wait=30):
        self._n += 1
        i = self._n
        try:
            self.s.send(self._frame(json.dumps({"id": i, "method": method,
                                                "params": params or {}})))
        except OSError as e:
            return {"error": f"socket closed: {e}"}
        txt = self._read_until(f'"id":{i}'.encode(), wait).decode("utf-8", "replace")
        return self._parse(txt, i)

    def drain(self, seconds=0.4):
        """Absorb pending events so the next command reads a clean stream."""
        self.s.settimeout(seconds)
        try:
            while True:
                if not self.s.recv(262144):
                    break
        except Exception:
            pass
        self.s.settimeout(60)

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def js(tab, expr, wait=30):
    """Evaluate JS and return the value.

    CDP nests the answer twice: `{"result":{"result":{"value":…}}}`, where the
    outer result is the command's return and the inner is the JS RemoteObject.
    Unwrapping only one level reads `{"type":"number"}` — a dict with no "value"
    key — and returns None. Measured symptom: evaluate worked but every answer was
    None, including 1+1.
    """
    r = tab.cmd("Runtime.evaluate",
                {"expression": expr, "returnByValue": True, "awaitPromise": True}, wait)
    outer = r.get("result") or {}
    if "exceptionDetails" in outer:
        return "JS ERROR: " + str(outer.get("exceptionDetails"))[:300]
    obj = outer.get("result") or {}
    if obj.get("subtype") == "error" or obj.get("type") == "undefined":
        return "JS ERROR: " + str(obj.get("description") or "undefined")[:300]
    v = obj.get("value")
    return v if v is not None else obj.get("description")


def open_url(url, browser, settle):
    if not launch(browser, url):
        raise SystemExit(f"  {browser} did not come up on CDP :{PORT}")
    t = Tab()
    t.cmd("Page.enable")
    t.cmd("Page.navigate", {"url": url}, wait=30)
    # A real page load is not instant and LinkedIn in particular redirects twice;
    # a fixed sleep would be guessing. Poll for a non-blank body instead.
    t0 = time.time()
    while time.time() - t0 < settle:
        time.sleep(2)
        t.drain(0.3)
        n = js(t, "document.body ? document.body.innerText.length : 0", 15)
        if isinstance(n, int) and n > 200:
            break
    info = js(t, "JSON.stringify({t:document.title,u:location.href,n:document.body.innerText.length})", 20)
    t.close()
    return info


def do_status():
    print(f"  CDP :{PORT} {'UP' if alive() else 'down'}")
    for b in ("chrome", "brave"):
        p = real_profile(b)
        ok = os.path.isdir(p)
        print(f"  {b:7} profile {'found' if ok else 'MISSING'}: {p}")
        for sub in ("Default", "Profile 1"):
            ck2 = os.path.join(p, sub, "Cookies")
            if not os.path.exists(ck2):
                continue
            try:
                import sqlite3 as _s3, tempfile as _tf
                tp2 = _tf.mktemp(); shutil.copy(ck2, tp2); c2 = _s3.connect(tp2)
                sess = {}
                for host in ("youtube.com","instagram.com","linkedin.com","github.com"):
                    sess[host] = c2.execute(
                        "SELECT COUNT(*) FROM cookies WHERE host_key LIKE ? AND ("
                        "name LIKE '%session%' OR name LIKE '%SID%' OR name LIKE '%LOGIN_INFO%'"
                        " OR name IN ('li_at','JSESSIONID','liap','ds_user_id'))",
                        ("%"+host,)).fetchone()[0]
                logins = [h for h,n in sess.items() if n]
                print(f"           sub {sub:10} real logins: "
                      f"{', '.join(logins) if logins else 'NONE'}")
                c2.close(); os.unlink(tp2)
            except Exception:
                pass
        if ok:
            ck = os.path.join(p, "Default", "Cookies")
            if not os.path.exists(ck):
                ck = os.path.join(p, "Cookies")
            if os.path.exists(ck):
                try:
                    import sqlite3, tempfile
                    tp = tempfile.mktemp()
                    shutil.copy(ck, tp)
                    c = sqlite3.connect(tp)
                    for h in sorted(set(ROUTES) | {"linkedin.com", "github.com"}):
                        n = c.execute("SELECT COUNT(*) FROM cookies WHERE host_key LIKE ?",
                                      ("%" + h,)).fetchone()[0]
                        mark = "logged in" if n > 3 else ("thin" if n else "-")
                        print(f"           {h:15} {n:3d} cookies  {mark}")
                    c.close()
                    os.unlink(tp)
                except Exception as e:
                    print(f"           (cookie read failed: {e})")
    print("\n  routing rule: " + ", ".join(f"{k} -> {v}" for k, v in ROUTES.items())
          + ", everything else -> chrome")


def main():
    ap = argparse.ArgumentParser(prog="helix-browser")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open")
    o.add_argument("url")
    o.add_argument("--chrome", action="store_true")
    o.add_argument("--brave", action="store_true")
    o.add_argument("--settle", type=int, default=40)
    for name in ("shot", "text", "eval", "click", "type", "key"):
        p = sub.add_parser(name)
        if name == "click":
            p.add_argument("target")
        if name == "type":
            p.add_argument("text")
        if name == "key":
            p.add_argument("keyname")
        if name == "eval":
            p.add_argument("expr")
        if name == "shot":
            p.add_argument("path")
    sub.add_parser("status")
    a = ap.parse_args()

    if a.cmd == "status":
        do_status()
        return
    if a.cmd == "open":
        b = "chrome" if a.chrome else ("brave" if a.brave else route_for(a.url))
        url = a.url if "://" in a.url else "https://" + a.url
        print(f"  routing {url} -> {b}")
        info = open_url(url, b, a.settle)
        print("  " + str(info))
        # A redirect to /login means the session is not usable. Say so, because a
        # caller must never read "opened linkedin.com" as "posted on LinkedIn".
        if "/login" in str(info) or "Log In or Sign Up" in str(info):
            print(f"  NOT LOGGED IN — {b} has no working session for that site. "
                  f"Log in once in that browser; Helix then drives it.")
        elif any(h in url for h in VERIFIED_LOGIN):
            print("  session OK (verified login)")
        return

    if not alive():
        raise SystemExit(f"  browser not running. start it: helix-browser open <url>")
    t = Tab()
    if a.cmd == "shot":
        r = t.cmd("Page.captureScreenshot", {"format": "png"})
        data = (r.get("result") or {}).get("data")
        if not data:
            raise SystemExit("  screenshot failed")
        open(a.path, "wb").write(base64.b64decode(data))
        print(f"  wrote {a.path} ({os.path.getsize(a.path)} bytes)")
    elif a.cmd == "text":
        print(js(t, "document.body.innerText", 30) or "")
    elif a.cmd == "eval":
        print(js(t, a.expr, 40))
    elif a.cmd == "click":
        # text=Foo is the same convention Chrome's own $x uses.
        expr = (f"(()=>{{const want={json.dumps(a.target.split('=',1)[1] if '=' in a.target else a.target)};"
                "const els=[...document.querySelectorAll('a,button,[role=button],input[type=submit]')];"
                "const hit=els.find(e=>(e.innerText||e.value||'').trim().includes(want));"
                f"if(hit){{hit.click();return 'clicked: '+(hit.innerText||hit.value).trim().slice(0,60);}}"
                "return 'NOT FOUND: '+want;})()")
        print(js(t, expr, 30))
    elif a.cmd == "type":
        for ch in a.text:
            js(t, f"(()=>{{const e=document.activeElement;e.value=(e.value||'')+{json.dumps(ch)};"
                  "e.dispatchEvent(new Event('input',{bubbles:true}));return 1;}})()", 10)
        print(f"  typed {len(a.text)} chars")
    elif a.cmd == "key":
        codes = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8}
        kc = codes.get(a.keyname)
        if kc is None:
            raise SystemExit(f"  only Enter/Tab/Escape/Backspace supported, got {a.keyname}")
        js(t, f"document.activeElement.dispatchEvent(new KeyboardEvent('keydown',"
              f"{{key:{json.dumps(a.keyname)},keyCode:{kc},bubbles:true}}));return 1", 15)
        print(f"  sent {a.keyname}")
    t.close()


if __name__ == "__main__":
    main()
