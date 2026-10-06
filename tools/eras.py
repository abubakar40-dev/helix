#!/usr/bin/env python3
"""Books of Eras — a structured historical knowledge store for Helix.

Requirements §23 asks for "a structured historical knowledge database … not just a
folder of random books", so this stores *entities with types, dates and relations*,
not documents. Wikidata is the source: CC0, free, no key, already structured.

Deliberately does NOT use the Wikidata Query Service. Measured: WDQS currently
returns 429 "Aggressively rate-limiting to 1 req / min … during active wdqs
outage", which makes a multi-category seed take hours and hammers a service that
is already struggling. Everything here uses the ordinary MediaWiki/Wikidata APIs
instead, which serve the same data and are not throttled that way.

Shape agreed with the human: a curated seed across §23's categories that is fully
offline afterwards, plus on-demand expansion so coverage keeps growing.

  eras seed [--per N]        fetch/refresh the curated seed (idempotent)
  eras ask "question"        answer from the store
  eras get Q42               one entity and its relations
  eras find "term"           search
  eras stats                 what is in here

Stdlib only: sqlite3, urllib, json, argparse, re, time.
"""
import argparse, json, os, re, sqlite3, sys, time, urllib.parse, urllib.request

DB = os.path.expanduser("~/.hermes/eras.db")
UA = "HelixBooksOfEras/1.0 (local assistant; personal use)"
WIKI = "https://en.wikipedia.org/w/api.php"
WDAPI = "https://www.wikidata.org/w/api.php"

MIN_INTERVAL = 2.0          # measured: 1.2s still trips 429 during the outage
_last = [0.0]

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS entity (
  qid TEXT PRIMARY KEY, label TEXT, description TEXT, kind TEXT,
  birth INTEGER, death INTEGER, date INTEGER, sitelink TEXT,
  notability INTEGER, source TEXT, fetched_at INTEGER
);
CREATE TABLE IF NOT EXISTS relation (src TEXT, pred TEXT, dst TEXT,
  qual_start INTEGER, qual_end INTEGER,
  PRIMARY KEY (src, pred, dst, qual_start, qual_end));
CREATE TABLE IF NOT EXISTS predicate (id TEXT PRIMARY KEY, label TEXT);
CREATE INDEX IF NOT EXISTS ix_label ON entity(label);
CREATE INDEX IF NOT EXISTS ix_src  ON relation(src);
CREATE INDEX IF NOT EXISTS ix_dst  ON relation(dst);
"""

PREDICATES = {
    "P106": "occupation", "P27": "citizenship", "P19": "place of birth",
    "P20": "place of death", "P22": "father", "P25": "mother", "P26": "spouse",
    "P40": "child", "P737": "influenced by", "P135": "movement",
    "P101": "field of work", "P800": "notable work", "P361": "part of",
    "P585": "point in time", "P17": "country", "P131": "located in",
    "P279": "subclass of", "P1441": "present in work",
    "P39": "position held",   # 18 claims on Bismarck alone; without it
                              # "who was chancellor in 1871" is unanswerable
}

# §23's domains. Wikipedia categories, verified to exist. Curated rather than
# exhaustive: Wikidata holds 52,688 philosophers alone, and a category listing
# is alphabetical, so the seed is ranked by sitelink count (how many language
# versions an article has) to keep the notable figures.
SEEDS = [
    ("philosophy",   ["Ancient Greek philosophers", "20th-century philosophers",
                      "Philosophers", "British philosophers", "German philosophers"]),
    ("science",      ["Physicists", "Chemists", "Biologists", "Astronomers",
                      "Computer scientists"]),
    ("mathematics",  ["Mathematicians"]),
    ("medicine",     ["Physicians", "Surgeons"]),
    ("technology",   ["Inventors", "Engineers"]),
    ("literature",   ["Novelists", "Poets", "Playwrights", "Essayists"]),
    ("art",          ["Painters", "Sculptors", "Composers"]),
    ("economics",    ["Economists"]),
    ("politics",     ["Presidents", "Prime ministers", "Diplomats"]),
    ("war",          ["Military leaders", "Generals"]),
    ("religion",     ["Religious leaders", "Theologians"]),
    ("history",      ["Historians"]),
]

ISO = re.compile(r"^([+-]?)(\d{1,6})-")


def throttle():
    gap = time.time() - _last[0]
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last[0] = time.time()


def get(base, params, tries=3):
    """Polite GET with a real User-Agent. On 429 it backs off hard and gives up
    rather than hammering — that is what got Wikidata to throttle me earlier."""
    for i in range(tries):
        throttle()
        url = base + "?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 20 * (i + 1)
                print(f"    429 — backing off {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"    HTTP {e.code}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"    {e}", file=sys.stderr)
            return None
    return None


def year(v):
    m = ISO.match(v or "")
    if not m:
        return None
    return -int(m.group(2)) if m.group(1) == "-" else int(m.group(2))


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    c.execute("INSERT OR REPLACE INTO predicate VALUES ('P19','place of birth')")
    c.commit()
    return c


def category_titles(cat, limit):
    """Page through a category. Alphabetical, so the caller ranks afterwards."""
    out, cont = [], None
    while len(out) < limit:
        p = {"action": "query", "format": "json", "list": "categorymembers",
             "cmtitle": f"Category:{cat}", "cmtype": "page", "cmlimit": "200"}
        if cont:
            p["cmcontinue"] = cont
        d = get(WIKI, p)
        if not d:
            break
        out += [m["title"] for m in d["query"]["categorymembers"]]
        cont = d.get("continue", {}).get("cmcontinue")
        if not cont:
            break
    return out[:limit]


def titles_to_qids(titles):
    """Titles -> Wikidata QIDs via pageprops. 50 per call."""
    qids = {}
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        d = get(WIKI, {"action": "query", "format": "json", "prop": "pageprops",
                       "ppprop": "wikibase_item", "redirects": "1",
                       "titles": "|".join(batch)})
        if not d:
            continue
        for p in d.get("query", {}).get("pages", {}).values():
            q = (p.get("pageprops") or {}).get("wikibase_item")
            if q:
                qids[q] = p["title"]
    return qids


def fetch_entities(qids):
    """QIDs -> full facts. 50 per call."""
    out = {}
    for i in range(0, len(qids), 50):
        d = get(WDAPI, {"action": "wbgetentities", "format": "json",
                        "ids": "|".join(qids[i:i + 50]),
                        "props": "labels|descriptions|claims|sitelinks",
                        "languages": "en|enwiki"})
        if d:
            out.update(d.get("entities") or {})
    return out


def seed(per, refresh):
    c = db()
    for pid, pl in PREDICATES.items():
        c.execute("INSERT OR REPLACE INTO predicate VALUES (?,?)", (pid, pl))
    c.commit()
    grand = 0
    for kind, cats in SEEDS:
        titles = []
        for cat in cats:
            titles += category_titles(cat, per * 2)
        titles = list(dict.fromkeys(titles))
        if not titles:
            print(f"  {kind:12} no category members")
            continue
        qids = titles_to_qids(titles)
        ents = fetch_entities(list(qids))
        rows = []
        for qid, ent in ents.items():
            if "missing" in ent:
                continue
            label = ((ent.get("labels") or {}).get("en") or {}).get("value")
            desc = ((ent.get("descriptions") or {}).get("en") or {}).get("value")
            sitel = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
            # `sitelinks` is a dict of every wiki, not {count:N}. Its length
            # is the notability proxy: how many languages cover the topic.
            nsite = len(ent.get("sitelinks") or {})
            birth = death = None
            claims = ent.get("claims") or {}
            for pid, key in (("P569", "b"), ("P570", "d")):
                for cl in claims.get(pid, []):
                    try:
                        y = year(cl["mainsnak"]["datavalue"]["value"]["time"])
                    except Exception:
                        continue
                    if key == "b" and birth is None:
                        birth = y
                    if key == "d" and death is None:
                        death = y
                    break
            rows.append((qid, label, desc, kind, birth, death, None,
                         f"https://en.wikipedia.org/wiki/{sitel}" if sitel else None,
                         nsite, f"https://www.wikidata.org/wiki/{qid}",
                         int(time.time())))
            for pid in PREDICATES:
                for cl in claims.get(pid, [])[:8]:
                    try:
                        v = cl["mainsnak"]["datavalue"]["value"]
                    except Exception:
                        continue
                    if not (isinstance(v, dict) and v.get("id")):
                        continue
                    # P580/P582 qualify WHEN a position or event held, which is
                    # what makes tenure questions answerable at all.
                    qs = qe = None
                    for qp, key in (("P580", "s"), ("P582", "e")):
                        for ql in (cl.get("qualifiers") or {}).get(qp, []):
                            try:
                                y = year(ql["datavalue"]["value"]["time"])
                            except Exception:
                                continue
                            if key == "s" and qs is None:
                                qs = y
                            if key == "e" and qe is None:
                                qe = y
                            break
                    c.execute("INSERT OR IGNORE INTO relation VALUES (?,?,?,?,?)",
                              (qid, pid, v["id"], qs, qe))
        # Notability: keep the best-documented figures in this domain.
        rows.sort(key=lambda r: -(r[8] or 0))
        kept = 0
        for r in rows:
            if kept >= per:
                break
            have = c.execute("SELECT 1 FROM entity WHERE qid=?", (r[0],)).fetchone()
            if have and not refresh:
                continue
            c.execute("INSERT OR REPLACE INTO entity VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
            kept += 1
        c.commit()
        grand += kept
        best = rows[0][8] if rows else 0
        print(f"  {kind:12} {kept:4d} kept of {len(titles):4d} titles "
              f"(best sitelinks {best})")
    print(f"\n  {grand} written. store: {stats_line(c)}")
    return c


def stats_line(c):
    e = c.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    r = c.execute("SELECT COUNT(*) FROM relation").fetchone()[0]
    return f"{e} entities, {r} relations, {os.path.getsize(DB)/1e6:.1f} MB"


def stats(c):
    print(f"  {DB}\n  {stats_line(c)}\n\n  by domain:")
    for k, n in c.execute("SELECT kind,COUNT(*) FROM entity GROUP BY kind ORDER BY 2 DESC"):
        print(f"    {k or '?':14} {n}")
    print("\n  most connected:")
    for q, n in c.execute("SELECT src,COUNT(*) n FROM relation GROUP BY src ORDER BY n DESC LIMIT 8"):
        lab = c.execute("SELECT label FROM entity WHERE qid=?", (q,)).fetchone()
        print(f"    {(lab[0] if lab else q)[:44]:46} {n}")


def expand(c, terms):
    """On-demand: look up names the store does not have and cache them."""
    have = {t.lower() for (t,) in c.execute("SELECT label FROM entity")}
    want = [t for t in terms if t.lower() not in have]
    if not want:
        return 0
    qids = titles_to_qids(want)
    ents = fetch_entities(list(qids))
    n = 0
    for qid, ent in ents.items():
        if "missing" in ent:
            continue
        label = ((ent.get("labels") or {}).get("en") or {}).get("value") or qids.get(qid)
        desc = ((ent.get("descriptions") or {}).get("en") or {}).get("value")
        nsite = len(ent.get("sitelinks") or {})
        sitel = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
        c.execute("INSERT OR REPLACE INTO entity VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (qid, label, desc, "expanded", None, None, None,
                   f"https://en.wikipedia.org/wiki/{sitel}" if sitel else None,
                   nsite, f"https://www.wikidata.org/wiki/{qid}", int(time.time())))
        n += 1
    c.commit()
    return n


def ask(c, question, allow_expand):
    terms = re.findall(r"[A-Za-z][A-Za-z\-']{2,}", question)
    seen, hits = set(), []
    for t in terms:
        for qid, lab, desc, kind, b in c.execute(
                "SELECT qid,label,description,kind,birth FROM entity "
                "WHERE label LIKE ? ORDER BY length(label) LIMIT 6", (f"%{t}%",)):
            if qid in seen:
                continue
            seen.add(qid)
            hits.append((qid, lab, desc, kind, b))
    if not hits and allow_expand:
        got = expand(c, terms[:6])
        print(f"  (expanded {got} new entities from Wikidata)")
        return ask(c, question, False)
    if not hits:
        print("NOT_IN_STORE")
        return False
    for qid, lab, desc, kind, b in hits[:8]:
        yr = f" ({b})" if b else ""
        print(f"  {lab}{yr}  [{kind}]  {qid}")
        if desc:
            print(f"      {desc[:110]}")
        for p, d in c.execute("""SELECT p,d FROM relation r JOIN predicate p ON p.id=r.pred
                                 WHERE r.src=? LIMIT 5""", (qid,)):
            dl = c.execute("SELECT label FROM entity WHERE qid=?", (d,)).fetchone()
            print(f"      {p}: {dl[0] if dl else d}")
    return True


def positions(c, limit=0):
    """Backfill P39 (+ P580/P582 tenure) for every seeded figure.

    The seed predated P39 being in PREDICATES, so `office` only knew one career.
    Re-running the seed would re-fetch every label and description too; this
    fetches only what is missing.
    """
    have = {r[0] for r in c.execute("SELECT DISTINCT src FROM relation WHERE pred='P39'")}
    people = [r[0] for r in c.execute(
        "SELECT qid FROM entity WHERE kind NOT IN ('office','linked')"
        + (" LIMIT %d" % limit if limit else ""))]
    todo = [q for q in people if q not in have]
    if not todo:
        print("  every stored figure already has positions")
        return 0
    print(f"  {len(todo)} figures to check")
    n = 0
    for i in range(0, len(todo), 50):
        for qid, ent in fetch_entities(todo[i:i + 50]).items():
            if "missing" in ent:
                continue
            got = 0
            for cl in (ent.get("claims") or {}).get("P39", []):
                try:
                    v = cl["mainsnak"]["datavalue"]["value"]
                except Exception:
                    continue
                if not (isinstance(v, dict) and v.get("id")):
                    continue
                qs = qe = None
                for qp, key in (("P580", "s"), ("P582", "e")):
                    for ql in (cl.get("qualifiers") or {}).get(qp, []):
                        try:
                            y = year(ql["datavalue"]["value"]["time"])
                        except Exception:
                            continue
                        if key == "s" and qs is None:
                            qs = y
                        if key == "e" and qe is None:
                            qe = y
                        break
                c.execute("INSERT OR IGNORE INTO relation VALUES (?,?,?,?,?)",
                          (qid, "P39", v["id"], qs, qe))
                got += 1
            n += got
        c.commit()
        print(f"    {min(i + 50, len(todo))}/{len(todo)} fetched, {n} position records")
    return n


def resolve_dangling(c):
    """Give every QID a relation points at a name we can actually print."""
    dangling = [r[0] for r in c.execute("""
      SELECT DISTINCT src FROM relation WHERE src NOT IN (SELECT qid FROM entity)
      UNION
      SELECT DISTINCT dst FROM relation WHERE dst NOT IN (SELECT qid FROM entity)""")]
    if not dangling:
        return 0
    n = 0
    for i in range(0, len(dangling), 50):
        for qid, ent in fetch_entities(dangling[i:i + 50]).items():
            if "missing" in ent:
                continue
            lab = ((ent.get("labels") or {}).get("en") or {}).get("value") or qid
            desc = ((ent.get("descriptions") or {}).get("en") or {}).get("value")
            sl = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
            c.execute("INSERT OR REPLACE INTO entity VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (qid, lab, desc, "linked", None, None, None,
                       f"https://en.wikipedia.org/wiki/{sl}" if sl else None,
                       len(ent.get("sitelinks") or {}),
                       f"https://www.wikidata.org/wiki/{qid}", 0))
            n += 1
        c.commit()
    return n


def office(c, office_name, year=None, n=15):
    """Who held a position, optionally during a given year.

    Matches the POSITION's name, not the holder's — the earlier version did the
    opposite, so `office chancellor` could never match anything.
    """
    like = f"%{office_name}%"
    rows = c.execute("""SELECT h.label, h.qid, pos.label, r.qual_start, r.qual_end
                        FROM relation r
                        JOIN entity pos ON pos.qid = r.dst
                        JOIN entity h   ON h.qid   = r.src
                        WHERE r.pred = 'P39'
                          AND (pos.label LIKE ? OR pos.description LIKE ?)""",
                     (like, like)).fetchall()
    if year:
        y = int(year)
        def held(a, b):
            # Undated offices are excluded, not assumed. `office President
            # --year 1945` was answering with people whose office had no dates
            # at all, which asserts a tenure we simply do not have.
            if a is None and b is None:
                return False
            if a is not None and y < a:
                return False
            if b is not None and y > b:
                return False
            return True
        rows = [r for r in rows if held(r[3], r[4])]
    if not rows:
        print(f"  no stored holder of '{office_name}'"
              + (f" during {year}" if year else ""))
        print("  try: eras find <name>, or ask research to expand it")
        return False
    for holder, hq, pos, a, b in rows[:n]:
        ten = (f"{a}-{b}" if a is not None and b is not None
               else f"from {a}" if a is not None
               else f"until {b}" if b is not None else "")
        print(f"  {holder:32} {pos[:38]:40} {ten}")
    return True


def get_entity(c, qid):
    r = c.execute("SELECT qid,label,description,kind,birth,death,sitelink,source "
                  "FROM entity WHERE qid=?", (qid,)).fetchone()
    if not r:
        return False
    print(f"\n  {r[1]}  ({r[0]})   [{r[3]}]")
    if r[2]:
        print(f"    {r[2]}")
    bits = [f"b. {r[4]}" if r[4] is not None else "",
            f"d. {r[5]}" if r[5] is not None else ""]
    if any(bits):
        print(f"    {' · '.join(x for x in bits if x)}")
    print(f"    source: {r[7]}")
    for p, d in c.execute("""SELECT p,d FROM relation r JOIN predicate p ON p.id=r.pred
                             WHERE r.src=? LIMIT 25""", (qid,)):
        dl = c.execute("SELECT label FROM entity WHERE qid=?", (d,)).fetchone()
        print(f"      {p:20} -> {dl[0] if dl else d}")
    return True


def find(c, term, n=20):
    like = f"%{term}%"
    rows = c.execute("""SELECT qid,label,description,kind,birth FROM entity
                        WHERE label LIKE ? OR description LIKE ?
                        ORDER BY length(label) LIMIT ?""", (like, like, n)).fetchall()
    for qid, lab, desc, kind, b in rows:
        print(f"  {(lab or qid)[:40]:42} {kind or '':11}"
              f"{f'({b})' if b else ''}  {(desc or '')[:40]}")
    if not rows:
        print("  nothing yet — run: eras seed")


def main():
    ap = argparse.ArgumentParser(prog="eras")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--per", type=int, default=170)
    s.add_argument("--refresh", action="store_true")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--no-expand", action="store_true")
    g = sub.add_parser("get"); g.add_argument("qid")
    f = sub.add_parser("find"); f.add_argument("term"); f.add_argument("-n", type=int, default=20)
    o = sub.add_parser("office"); o.add_argument("position"); o.add_argument("--year")
    ps = sub.add_parser("positions"); ps.add_argument("--limit", type=int, default=0)
    sub.add_parser("stats")
    args = ap.parse_args()
    c = db()
    if args.cmd == "seed":
        seed(args.per, args.refresh)
    elif args.cmd == "stats":
        stats(c)
    elif args.cmd == "ask":
        ask(c, args.question, not args.no_expand)
    elif args.cmd == "get":
        if not get_entity(c, args.qid):
            print("  not stored")
    elif args.cmd == "find":
        find(c, args.term, args.n)
    elif args.cmd == "office":
        office(c, args.position, args.year)
    elif args.cmd == "positions":
        n = positions(c, args.limit)
        m = resolve_dangling(c)
        print(f"  {n} position records, {m} names resolved")
        print(f"  store: {stats_line(c)}")


if __name__ == "__main__":
    main()