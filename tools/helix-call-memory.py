#!/usr/bin/env python3
"""Write a phone call into HELIX's SHARED memory, not a parallel store.

Every channel (cli, cron, kanban, whatsapp) already lives in ~/.hermes/state.db as
a `sessions` row plus `messages`. A phone call is just another channel, so it goes
in there too, with source='phone'. Triggers on the messages table keep messages_fts
and messages_fts_trigram in sync, which means HELIX's existing search finds phone
calls immediately - no new index, no second history that can drift.

Writes are append-only and idempotent on call_id, so a resync is harmless.
"""
import argparse, json, os, sqlite3, sys, time

HERMES = os.path.expanduser("~/.hermes")
DB = os.path.join(HERMES, "state.db")

SCHEMA = """
create table if not exists phone_calls (
  call_id    text primary key,
  started_at real not null,
  ended_at   real,
  session_id text,
  synced_at  real not null
)
"""


def ingest(db_path, call):
    """Insert one call as a session + messages. Returns the session id."""
    call_id = call["call_id"]
    started = float(call.get("started_at") or time.time())
    ended = float(call.get("ended_at") or started)
    # matches the shape of the ids hermes already uses: YYYYMMDD_HHMMSS_hex
    sid = "call_%s_%s" % (time.strftime("%Y%m%d_%H%M%S", time.gmtime(started)),
                          call_id.replace("-", "")[:8])

    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("pragma foreign_keys=on")
        con.execute(SCHEMA)
        if con.execute("select 1 from phone_calls where call_id=?", (call_id,)).fetchone():
            return sid  # already synced
        turns = call.get("turns") or []
        # sessions.title is UNIQUE, so a fixed title would fail on the second call
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
        title = call.get("title") or f"Phone call {stamp}"
        if not str(title).endswith(stamp):
            title = f"{title} {stamp}"
        con.execute(
            "insert into sessions (id, source, started_at, ended_at, message_count,"
            " title, title_source, display_name, chat_type) values (?,?,?,?,?,?,?,?,?)",
            (sid, "phone", started, ended or None, len(turns),
             title, "phone", "Phone", "call"))
        for i, t in enumerate(turns):
            # roles must be ones hermes knows; anything else breaks its renderers
            role = "assistant" if t.get("who") in ("assistant", "helix", "bot") else "user"
            ts = started + float(t.get("at") or i)
            con.execute(
                "insert into messages (session_id, role, content, timestamp) values (?,?,?,?)",
                (sid, role, t.get("text") or "", ts))
        con.execute("insert into phone_calls (call_id, started_at, ended_at, session_id, synced_at)"
                    " values (?,?,?,?,?)", (call_id, started, ended or None, sid, time.time()))
        con.commit()
        return sid
    finally:
        con.close()


def recall(db_path, query, limit=8):
    """Search across ALL channels - cli, cron, kanban and phone - at once."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        rows = con.execute(
            "select s.source, datetime(m.timestamp,'unixepoch'), m.role,"
            "       substr(m.content,1,240)"
            "  from messages_fts f"
            "  join messages m on m.id = f.rowid"
            "  join sessions s on s.id = m.session_id"
            " where messages_fts match ? order by m.timestamp desc limit ?",
            (query, limit)).fetchall()
        return [{"when": r[1], "source": r[0], "role": r[2], "text": r[3]} for r in rows]
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--recall", help="search shared history across every channel")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--call", help="path to a call JSON file to ingest")
    a = ap.parse_args()

    if a.call:
        with open(a.call) as f:
            print("ingested as session", ingest(a.db, json.load(f)))
        return
    if a.recall:
        hits = recall(a.db, a.recall, a.limit)
        if not hits:
            print("no matches in shared history")
        for h in hits:
            print("[%s] %s %s: %s" % (h["source"], h["when"], h["role"], h["text"]))
        return
    ap.print_help()


if __name__ == "__main__":
    main()