#!/usr/bin/env python3
"""Pull finished calls off the box into HELIX's shared history.

Runs on the laptop, not the box: state.db lives here and nowhere else. Every call
becomes a source='phone' session that FTS already indexes, so HELIX recalls it
alongside cli, cron and kanban history with no separate store to drift.
"""
import json, os, subprocess, sys, urllib.parse, urllib.request

BOX = os.environ.get("HELIX_BOX", "https://129-151-148-112.sslip.io")
HERE = os.path.dirname(os.path.abspath(__file__))
INGEST = os.path.join(HERE, "helix-call-memory.py")


def drain_tasks(entries):
    """Turn voice-assigned tasks into real kanban tasks via hermes, the supported
    route. Direct SQL into the kanban is not attempted: whatsapp.py records that
    poking the gateway internals caused duplicate outbound messages."""
    made = []
    for t in entries:
        title = (t.get("title") or "").strip()
        body = (t.get("body") or "").strip()
        assignee = (t.get("assignee") or "").strip()
        if not title:
            continue
        cmd = ["hermes", "kanban", "create", title, "--body", body]
        if assignee:
            cmd += ["--assignee", assignee]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        ok = p.returncode == 0
        made.append({"title": title, "assignee": assignee, "ok": ok,
                     "out": (p.stdout or p.stderr).strip()[:160]})
        print(("created " if ok else "FAILED ") + title + (f" -> {assignee}" if assignee else ""))
    return made


def push_memory(limit=200):
    """Send shared history down so the AI can answer questions about past calls."""
    entries = subprocess.run([sys.executable, os.path.join(HERE, "helix-call-memory.py"),
                              "--recall", "the OR a I", "--limit", str(limit)],
                             capture_output=True, text=True)
    return entries.stdout


def main():
    with urllib.request.urlopen(f"{BOX}/calls", timeout=30) as r:
        calls = json.load(r).get("calls", [])
    for c in calls:
        p = subprocess.run([sys.executable, INGEST, "--call", "/dev/stdin"],
                           input=json.dumps(c), capture_output=True, text=True)
        print((p.stdout or p.stderr).strip())
    print(f"{len(calls)} call(s) synced into shared memory")

    try:
        with urllib.request.urlopen(f"{BOX}/tasks", timeout=30) as r:
            tasks = json.load(r).get("tasks", [])
        if tasks:
            made = drain_tasks(tasks)
            # only drop what actually landed, so a failure retries next run
            for t in tasks:
                if any(m["title"] == t.get("title") and m["ok"] for m in made):
                    with urllib.request.urlopen(
                            f"{BOX}/task-done/{urllib.parse.quote(t['file'])}", timeout=15):
                        pass
    except Exception as e:
        print("task drain skipped:", type(e).__name__, e)


if __name__ == "__main__":
    main()