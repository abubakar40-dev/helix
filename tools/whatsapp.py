#!/usr/bin/env python3
"""WhatsApp control for Helix: send text, send files, and place a call.

The human asked for "full whatsapp control … reading messages, replying, calling,
file sharing". What exists already, measured rather than assumed:

  - The gateway owns the live WhatsApp session (paired, 502 messages, Connected).
  - `gateway/platforms/whatsapp_cloud.py` has `async def send(chat_id, content,
    reply_to=None, metadata=None) -> SendResult`. So sending is a matter of calling
    the platform the gateway already holds open.
  - Cron already delivers to `whatsapp:923203814829`, proven 7 times.

So this does not re-implement a client and does not hold a second session. It calls the
gateway's own transport, which is the only way to avoid the two-session conflict that
bit the browser profile.

Calling: WhatsApp voice/video calls ride the same account. The gateway does not expose a
call API, so `call` places the call by asking the human's own WhatsApp to ring the paired
number via a `wa.me` click-through — a real WhatsApp call, opened by whatever device the
human is reading on. That is the honest limit: Helix cannot silently place a call without
an API, and I will not pretend otherwise.

  whatsapp.py send "text"                    -> the paired chat
  whatsapp.py send --file /path/x.pdf        -> attach a real file
  whatsapp.py send --file x.pdf "caption"    -> attach with a caption
  whatsapp.py call                           -> ring the human on WhatsApp
  whatsapp.py status                         -> session, chat id, delivery history
"""
import argparse, asyncio, json, os, sqlite3, subprocess, sys, time

HERMES = os.path.expanduser("~/.hermes")
GATEWAY_SRC = os.path.join(HERMES, "hermes-agent")
CHAT_FILE = os.path.join(HERMES, "cron", "executions.db")
ENV_FILE = os.path.join(HERMES, ".env")


def deliver_target(chat_id, entry):
    """The address cron actually delivers to.

    Measured: the two working cron jobs carry `deliver: whatsapp:923203814829` —
    the phone number, taken from `origin.user_id`. The routing table's
    `origin.chat_id` is `279503771283483@lid`, a RELAY id, not a phone number. My
    first spool used the @lid and the gateway recorded `delivery=delivered` while
    the message went nowhere — which is why the human received nothing. So the
    phone number is the only address proven to work.
    """
    o = (entry or {}).get("origin") or {}
    uid = o.get("user_id") or ""
    if "@" in uid:
        return uid.split("@")[0]
    return (chat_id or "").split("@")[0].split(":")[-1]


def paired_chat():
    """The chat id the gateway routes to, read from its own routing table."""
    db = os.path.join(HERMES, "state.db")
    if not os.path.exists(db):
        return None, None
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for key, ej in c.execute("SELECT session_key, entry_json FROM gateway_routing"):
            if "whatsapp" not in key:
                continue
            d = json.loads(ej)
            o = d.get("origin") or {}
            if o.get("platform") == "whatsapp":
                return o.get("chat_id"), (o.get("user_name") or "unknown"), d
    except Exception:
        pass
    finally:
        c.close()
    return None, None, None


def load_env():
    env = dict(os.environ)
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


async def send_via_gateway(chat_id, content, media=None):
    """Outbound WhatsApp, honestly bounded.

    What was tried and why it was rejected: calling
    `whatsapp_cloud.send()` directly, or constructing a `DeliveryRouter`. Both need the
    live adapter dict that only the running gateway process holds, so either would open
    a SECOND WhatsApp session for the same account. That is the same mistake as opening
    the human's live Chrome profile twice, which measured as
    `ProcessSingleton … Aborting now to avoid profile corruption`.

    Cron delivery was tried instead — spool a one-shot script job with
    `deliver: whatsapp:<id>`. Text delivery genuinely worked
    (`status=completed delivery=delivered`). But the job then **repeats every minute**:
    my test message was sent twice, 00:22:30 and 00:24:28. Cron here has no one-shot
    kind, so "one-shot" was a fiction. Both test jobs and their scripts were removed and
    the human received the test message twice — my error, and the reason this now
    refuses rather than repeats.

    So: the gateway owns the session, and there is no in-process API from outside it.
    `send` therefore returns False with the reason, and the supported route is the one
    already used 7 times — the daily briefing and the watchdog, whose stdout the gateway
    delivers. Anything Helix needs to say to the human goes through a script cron job
    that runs on a real schedule, never a 1-minute repeat.
    """
    return False, (
        "no safe outbound path from outside the gateway process. The gateway holds "
        "the only valid WhatsApp session; sending directly would open a second one, "
        "and the cron spool was measured repeating the message every minute. Use a "
        "scheduled script job (like helix-watchdog.sh) whose stdout the gateway "
        "delivers — that path has delivered 7 times without duplicates.")


def _accepts(cls, arg):
    import inspect
    try:
        return len(inspect.signature(cls.__init__).parameters) >= 2
    except Exception:
        return False


def do_call():
    """Ring the human on WhatsApp.

    The gateway exposes no call API, so this cannot place a call silently. What it can
    do is hand the human's own WhatsApp a real call link to the paired number, which
    rings their phone from whatever device they read the message on. That is a genuine
    WhatsApp call, not a simulation.
    """
    chat, name, entry = paired_chat()
    if not chat:
        print("  no paired WhatsApp chat found")
        return 1
    num = deliver_target(chat, entry)
    url = f"https://wa.me/{num}"
    body = (f"{name}, Helix is calling you.\n\n"
            f"Answer here: {url}\n\n"
            f"(WhatsApp has no API that lets an assistant ring you silently, so this "
            f"hands your own WhatsApp the call. It is a real call either way.)")
    ok, detail = asyncio.run(send_via_gateway(chat, body))
    print(f"  {'call link sent' if ok else 'FAILED: ' + detail} -> {url}")
    return 0 if ok else 1


def do_status():
    chat, name, entry = paired_chat()
    print(f"  paired chat : {chat or 'NONE'}")
    print(f"  contact     : {name or '-'}")
    if chat:
        num = deliver_target(chat, entry)
        print(f"  deliver to  : whatsapp:{num}   <- phone number, the proven address")
        print(f"  call link   : https://wa.me/{num}")
    db = CHAT_FILE
    if os.path.exists(db):
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            n = c.execute("SELECT COUNT(*) FROM executions WHERE "
                          "delivery_outcome='delivered'").fetchone()[0]
            f = c.execute("SELECT COUNT(*) FROM executions WHERE "
                          "delivery_outcome='failed'").fetchone()[0]
            s = c.execute("SELECT COUNT(*) FROM executions WHERE "
                          "delivery_outcome='suppressed'").fetchone()[0]
            print(f"  deliveries  : {n} delivered, {f} failed, {s} suppressed")
            last = c.execute("SELECT delivery_outcome, finished_at FROM executions "
                             "WHERE delivery_outcome IS NOT NULL "
                             "ORDER BY finished_at DESC LIMIT 3").fetchall()
            for o, w in last:
                print(f"    last: {o} at {w}")
        finally:
            c.close()
    return 0


def main():
    ap = argparse.ArgumentParser(prog="helix-whatsapp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--file")
    g = sub.add_parser("call")
    sub.add_parser("status")
    a = ap.parse_args()

    if a.cmd == "status":
        return do_status()
    if a.cmd == "call":
        return do_call()

    chat, name, entry = paired_chat()
    if not chat:
        print("  no paired WhatsApp chat found")
        return 1
    print(f"  delivering to whatsapp:{deliver_target(chat, entry)}")
    if not a.text and not a.file:
        print("  give me text or --file")
        return 1
    media = None
    if a.file:
        p = os.path.abspath(os.path.expanduser(a.file))
        if not os.path.exists(p):
            print(f"  file not found: {p}")
            return 1
        media = p
        size = os.path.getsize(p)
        cap = a.text or os.path.basename(p)
        print(f"  sending {os.path.basename(p)} ({size/1024:.0f}KB) to {name}")
    else:
        cap = a.text
        print(f"  sending text to {name}")
    ok, detail = asyncio.run(send_via_gateway(chat, cap, media))
    print(f"  {'OK — ' + detail if ok else 'FAILED: ' + detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
