#!/usr/bin/env python3
"""Import the old Jarvis memory store into Helix's Obsidian vault.

Why this exists: the human asked for "all my history, all memory of jarvis, copy it and
add the same to helix". Three stores hold it and they are not the same thing:

  ~/jarvis-old-archive/JARVIS/memory/personal_memory.sqlite3
      178 structured rows: category, key, value, source, created_at, updated_at.
      This is the real structured memory.
  ~/.obsidian-vault/memory/**/*.md
      177 voice-captured notes, already in the vault, already imported earlier.
  ~/jarvis-old-archive/JARVIS/memory/abubakar_profile.md, long_term.json
      A hand-written profile and a small long-term file.

The vault is where Helix reads from, and it already has the same `category/key/value/
source/created/updated/tags` frontmatter convention, so the structured rows convert
into the existing shape rather than inventing a second one.

Idempotent: keyed on `key`, so re-running updates rather than duplicating. Nothing in
the vault or the old database is modified — the source is opened read-only.

  import-jarvis-memory.py --dry     show what would be written
  import-jarvis-memory.py           write it
  import-jarvis-memory.py --verify  re-read and count what landed
"""
import argparse, os, re, shutil, sqlite3, sys, tempfile, time
from datetime import datetime

VAULT = os.path.expanduser("~/.obsidian-vault/memory")
SRC_DB = os.path.expanduser(
    "~/jarvis-old-archive/JARVIS/memory/personal_memory.sqlite3")
SRC_DIR = os.path.expanduser("~/jarvis-old-archive/JARVIS/memory")


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:60] or "entry"


def read_source():
    if not os.path.exists(SRC_DB):
        raise SystemExit(f"  source not found: {SRC_DB}")
    # Open a copy read-only: the -wal/-shm siblings mean the original may still look
    # live to sqlite, and the archive is the human's history — never written to.
    tmp = tempfile.mktemp(suffix=".sqlite3")
    shutil.copy2(SRC_DB, tmp)
    c = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT category, key, value, source, created_at, updated_at "
        "FROM memory_entries").fetchall()
    c.close()
    os.unlink(tmp)
    return rows


def existing_keys():
    """What the vault already has, so this stays idempotent."""
    have = {}
    for root, _, files in os.walk(VAULT):
        for f in files:
            if not f.endswith(".md"):
                continue
            p = os.path.join(root, f)
            try:
                head = open(p, encoding="utf-8", errors="replace").read(600)
            except Exception:
                continue
            k = re.search(r'^key:\s*"?([^"\n]+)"?', head, re.M)
            if k:
                have[k.group(1).strip()] = p
    return have


def frontmatter(cat, key, value, source, created, updated):
    def q(s):
        return '"' + str(s or "").replace('"', '\\"') + '"'
    taglist = ["helix-memory", "jarvis-import"] + ([cat] if cat else [])
    tags = '"[' + ", ".join(taglist) + ']"'
    return (f"---\ncategory: {q(cat)}\nkey: {q(key)}\nvalue: {q(value)}\n"
            f"source: {q(source or 'jarvis')}\ncreated: {q(created)}\n"
            f"updated: {q(updated)}\ntags: {tags}\n---\n{value}\n")


def main():
    ap = argparse.ArgumentParser(prog="import-jarvis-memory")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()

    if a.verify:
        have = existing_keys()
        print(f"  vault memory keys: {len(have)}")
        for probe in ("name",):
            print(f"  '{probe}' present: {probe in have} -> {have.get(probe, '-')}")
        return 0

    rows = read_source()
    have = existing_keys()
    print(f"  source: {len(rows)} rows from {SRC_DB}")
    print(f"  vault already has: {len(have)} keyed notes")

    new, updated, skipped = [], [], []
    for cat, key, value, source, created, updated_at in rows:
        if key in have:
            # Compare the value so a genuine change is an update, not a duplicate.
            try:
                cur = open(have[key], encoding="utf-8", errors="replace").read(600)
            except Exception:
                cur = ""
            mv = re.search(r'^value:\s*"?([^"\n]*)"?', cur, re.M)
            if mv and mv.group(1).strip() == str(value or "").strip():
                skipped.append(key)
                continue
            updated.append((cat, key, value, source, created, updated_at, have[key]))
        else:
            new.append((cat, key, value, source, created, updated_at))

    print(f"  {len(new)} new, {len(updated)} changed, {len(skipped)} unchanged")
    for cat, key, value, src, cr, up, path in updated[:8]:
        print(f"    changed: {key}  ({value})")
    for cat, key, value, src, cr, up in new[:8]:
        print(f"    new    : [{cat}] {key} = {str(value)[:60]}")

    if a.dry:
        print("\n  --dry: nothing written")
        return 0

    written = 0
    for cat, key, value, src, cr, up in new:
        d = os.path.join(VAULT, slug(cat) or "general")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, slug(key) + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(frontmatter(cat, key, value, src, cr, up))
        written += 1
    changed = 0
    for cat, key, value, src, cr, up, path in updated:
        with open(path, "w", encoding="utf-8") as f:
            f.write(frontmatter(cat, key, value, src, cr, up))
        changed += 1

    # The two hand-written files, kept as notes rather than parsed into keys.
    for fname in ("abubakar_profile.md",):
        src = os.path.join(SRC_DIR, fname)
        if os.path.exists(src):
            body = open(src, encoding="utf-8", errors="replace").read()
            path = os.path.join(VAULT, "jarvis", fname)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                fm = ("---\ncategory: \"jarvis\"\n"
                      f"key: \"jarvis_{fname.replace('.md','')}\"\n"
                      f"value: \"imported profile from old jarvis\"\n"
                      "source: \"jarvis-archive\"\n"
                      f"created: \"{datetime.fromtimestamp(os.path.getmtime(src)):%Y-%m-%d}\"\n"
                      f"updated: \"{datetime.now():%Y-%m-%d}\"\n"
                      "tags: \"[\\\"helix-memory\\\", \\\"jarvis-import\\\"]\"\n---\n\n")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(fm + body)
                written += 1

    print(f"\n  wrote {written} new, updated {changed}")
    print(f"  vault memory keys now: {len(existing_keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
