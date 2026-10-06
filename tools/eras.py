#!/usr/bin/env python3
"""Books of Eras — a structured historical knowledge store for Helix.

Requirements §23 asks for "a structured historical knowledge database … not just a
folder of random books", so this stores *entities with types, dates and relations*,
not documents. Wikidata is the source: CC0, free, no key, already structured.

Deliberately does NOT use the Wikidata Query Service. Measured: WDQS returns
429 "Aggressively rate-limiting to 1 req / min … during active wdqs outage",
which makes a multi-category seed take hours and hammers a service already
struggling. Everything here uses the ordinary MediaWiki/Wikidata APIs.

Three things this learned the hard way, all measured rather than assumed:

1. **Notability is English article byte size, not sitelink count.** Sitelinks is
   a *language-coverage* proxy, so it promoted figures from linguistically broad
   countries — a Spanish politician with 12 recorded offices and a technology
   executive with 4 outranked genuinely notable officeholders. English article
   length measures how much English Wikipedia actually says about the topic.

2. **Wikipedia's broad occupation categories are near-empty of pages.** Measured:
   `Category:Mathematicians` holds 2 pages, `Category:Painters` 2,
   `Category:Historians` 2, and `Category:Presidents` 17 — mostly
   "President (government title)" and "Benjamin Harrison voice recording". The
   members live in subcategories (27 under Mathematicians, one holding 1,285
   pages), so `gather` descends one level when a category is too thin.

3. **Wikidata dates carry precision, and discarding it fabricates tenure.**
   Truman's presidency is `+194 -> +195`, which is *decade* precision — 1940s to
   1950s. Read as literal years it becomes 194–195, so asking who was President
   in 1945 would exclude the man who was. `span()` expands partial dates into
   the interval they actually denote.

Shape agreed with the human: a curated seed across §23's categories that is fully
offline afterwards, plus on-demand expansion so coverage keeps growing.

  eras seed [--per N] [--only d1,d2]    fetch/refresh the curated seed
  eras positions [--limit N]            backfill P39 tenure for seeded figures
  eras ask "question"                   answer from the store
  eras office <position> [--year Y]     who held an office, and when
  eras get Q42                          one entity and its relations
  eras find "term"                      search
  eras stats                            what is in here

Stdlib only: sqlite3, urllib, json, argparse, re, time.
"""
import argparse, json, os, re, sqlite3, sys, time, urllib.parse, urllib.request
import urllib.error

DB = os.path.expanduser("~/.hermes/eras.db")
UA = "HelixBooksOfEras/1.0 (local assistant; personal use)"
WIKI = "https://en.wikipedia.org/w/api.php"
WDAPI = "https://www.wikidata.org/w/api.php"

MIN_INTERVAL = 5.0      # measured, not guessed: at 2.0s a 5-domain re-seed spent
                         # six minutes taking 429s and completed zero domains.
_last = [0.0]

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS entity (
  qid TEXT PRIMARY KEY, label TEXT, description TEXT, kind TEXT,
  birth INTEGER, death INTEGER, date INTEGER, sitelink TEXT,
  notability INTEGER,   -- English Wikipedia article size in BYTES, not sitelinks
  source TEXT, fetched_at INTEGER
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

# §23's domains. Each lists Wikipedia categories; gather() descends into their
# subcategories because the broad ones hold almost no pages directly.
SEEDS = [
    ("philosophy",   ["Ancient Greek philosophers", "20th-century philosophers",
                      "Philosophers", "British philosophers", "German philosophers"]),
    ("science",      ["Physicists", "Chemists", "Biologists", "Astronomers",
                      "Computer scientists"]),
    ("mathematics",  ["Mathematicians by century", "Mathematicians by field",
                      "Mathematicians"]),
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

# Wikidata time: "+1945-01-01T00:00:00Z" is a year, "+194" is a decade, "+194-05"
# a month. Precision is implied by trailing significance and is NOT optional to
# read: it is the difference between "1945" and "somewhere in the 1940s".
TIME = re.compile(r"^([+-])(\d{1,6})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")


def throttle():
    gap = time.time() - _last[0]
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last[0] = time.time()


def get(base, params, tries=3):
    """Polite GET with a real User-Agent. On 429 it backs off hard and gives up
    rather than hammering — that is what got Wikimedia to throttle me at all."""
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


def span(t):
    """A Wikidata timestamp as the (start, end) interval it actually denotes,
    in YYYYMMDD integers so a year, a month and a day are all comparable.

    Wikidata writes unknown components as `00`, and that is NOT optional to read.
    Measured: Bismarck's early offices are `+1835-00-00` — year precision. An
    earlier version required a day component before accepting a date, so every
    year-precision tenure was silently discarded; and a version that treated `00`
    as month 0 encoded 1835 as 183500, which then compared as later than 1945 and
    excluded Bismarck from every year query.
    """
    m = TIME.match(t or "")
    if not m:
        return None, None
    sign, y, mo, day = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    mo_i = int(mo) if mo and int(mo) else 0
    d_i = int(day) if day and int(day) else 0
    base = y * 10000 + mo_i * 100 + d_i
    if sign == "-":
        base = -base            # BCE: more negative is earlier
    if mo_i == 0:
        return base, base + 1231        # year precision -> the whole year
    if d_i == 0:
        return base, base + 31          # month precision -> the whole month
    return base, base                   # day precision -> that day


def yr_year(i):
    """Render a YYYYMMDD integer back to a readable year for display."""
    if i is None:
        return ""
    y = i // 10000 if i >= 0 else -((-i) // 10000)
    return str(y)


def year(t):
    """The year of a timestamp at whatever precision it carries."""
    m = TIME.match(t or "")
    if not m:
        return None
    y = int(m.group(2))
    return -y if m.group(1) == "-" else y


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    for pid, pl in PREDICATES.items():
        c.execute("INSERT OR REPLACE INTO predicate VALUES (?,?)", (pid, pl))
    c.commit()
    return c


def _members(cat, cmtype, cap):
    out, cont = [], None
    while len(out) < cap:
        p = {"action": "query", "format": "json", "list": "categorymembers",
             "cmtitle": f"Category:{cat}", "cmtype": cmtype, "cmlimit": "200"}
        if cont:
            p["cmcontinue"] = cont
        d = get(WIKI, p)
        if not d:
            break
        out += [m["title"] for m in d.get("query", {}).get("categorymembers", [])]
        cont = d.get("continue", {}).get("cmcontinue")
        if not cont:
            break
    return out[:cap]


def members_with_facts(cat, want, cont=None):
    """One round trip per 200 category members, giving each member's Wikidata QID
    *and* its English article size together.

    This exists because of a measured failure, not for tidiness: fetching titles,
    then QIDs, then sizes separately took ~8 requests per domain, which at any
    polite interval drew 429s constantly — and `get()` gave up quietly, so the seed
    was silently storing a partial domain. Fewer requests is the only real fix.
    """
    p = {"action": "query", "format": "json", "generator": "categorymembers",
         "gcmtitle": f"Category:{cat}", "gcmtype": "page", "gcmlimit": "200",
         "prop": "pageprops|revisions", "ppprop": "wikibase_item",
         "rvprop": "size", "rvslots": "main"}
    if cont:
        p["gcmcontinue"] = cont
    d = get(WIKI, p)
    if not d:
        return None, None
    out = {}
    for pg in d.get("query", {}).get("pages", {}).values():
        rev = (pg.get("revisions") or [{}])[0]
        out[pg["title"]] = {"qid": (pg.get("pageprops") or {}).get("wikibase_item"),
                            "size": rev.get("size") or 0}
    return out, d.get("continue", {}).get("gcmcontinue")


def gather(cat, want, max_subs=10):
    """{title: {qid, size}} for a category, descending one level into
    subcategories when the category itself is thin.

    Measured reason this is necessary: `Category:Mathematicians` holds 2 pages and
    27 subcategories, the largest of which holds 1,285. `Category:Presidents` holds
    17, mostly "President (government title)" and "Benjamin Harrison voice
    recording" — the actual presidents are one level down. Thin domains were never
    a ranking problem.
    """
    facts = {}
    cont = None
    while len(facts) < want:
        got, cont = members_with_facts(cat, want, cont)
        if got is None:
            print(f"    !! lost a page batch for {cat} to throttling", file=sys.stderr)
            break
        facts.update(got)
        if not cont:
            break
    for sub in ([] if len(facts) >= want else _members(cat, "subcat", max_subs)):
        if len(facts) >= want:
            break
        got, _ = members_with_facts(sub[9:], want - len(facts))
        if got is None:
            print(f"    !! lost a page batch for {sub}", file=sys.stderr)
            continue
        facts.update(got)
    return facts


def titles_to_qids(titles):
    qids = {}
    for i in range(0, len(titles), 50):
        d = get(WIKI, {"action": "query", "format": "json", "prop": "pageprops",
                       "ppprop": "wikibase_item", "redirects": "1",
                       "titles": "|".join(titles[i:i + 50])})
        if not d:
            print(f"    !! lost a title->QID batch of {len(titles[i:i+50])} to "
                  f"throttling", file=sys.stderr)
            continue
        for p in d.get("query", {}).get("pages", {}).values():
            q = (p.get("pageprops") or {}).get("wikibase_item")
            if q:
                qids[q] = p["title"]
    return qids


def article_sizes(titles):
    """English Wikipedia article size in bytes, batched 50 titles per call.
    This is the notability signal, chosen over sitelink count because sitelinks
    measures language coverage rather than importance."""
    sizes = {}
    for i in range(0, len(titles), 50):
        d = get(WIKI, {"action": "query", "format": "json", "prop": "revisions",
                       "rvprop": "size", "rvslots": "main", "redirects": "1",
                       "titles": "|".join(titles[i:i + 50])})
        if not d:
            continue
        for p in d.get("query", {}).get("pages", {}).values():
            rev = (p.get("revisions") or [{}])[0]
            if rev.get("size") is not None:
                sizes[p["title"]] = rev["size"]
    return sizes


def fetch_entities(qids):
    out = {}
    for i in range(0, len(qids), 50):
        d = get(WDAPI, {"action": "wbgetentities", "format": "json",
                        "ids": "|".join(qids[i:i + 50]),
                        "props": "labels|descriptions|claims|sitelinks",
                        "languages": "en|enwiki"})
        if d:
            out.update(d.get("entities") or {})
    return out


def store_claims(c, qid, claims, cap=8):
    """Persist the properties in PREDICATES, with P580/P582 tenure qualifiers
    expanded into real intervals by span()."""
    n = 0
    for pid in PREDICATES:
        # P39 is NOT truncated. Wikidata does not order positions held by
        # seniority — Bismarck's list starts with Referendar and auscultator, so
        # an 8-claim cap kept his early jobs and silently discarded "Imperial
        # chancellor", which is the only row anyone asks for.
        lim = 40 if pid == "P39" else cap
        for cl in claims.get(pid, [])[:lim]:
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
                        s, e = span(ql["datavalue"]["value"]["time"])
                    except Exception:
                        continue
                    if key == "s" and qs is None:
                        qs = s
                    if key == "e" and qe is None:
                        qe = e
                    break
            c.execute("INSERT OR IGNORE INTO relation VALUES (?,?,?,?,?)",
                      (qid, pid, v["id"], qs, qe))
            n += 1
    return n


def seed(per, only, refresh, prune=False):
    c = db()
    grand = 0
    for kind, cats in SEEDS:
        if only and kind not in only:
            continue
        facts = {}
        for cat in cats:
            facts.update(gather(cat, per * 4))
        if not facts:
            print(f"  {kind:12} no category members at all")
            continue
        sizes = {t: v["size"] for t, v in facts.items()}
        qids = {v["qid"]: t for t, v in facts.items() if v["qid"]}
        if not qids:
            print(f"  {kind:12} {len(facts)} members but none has a Wikidata item")
            continue
        ents = fetch_entities(list(qids))
        enwiki = {}
        for qid, ent in ents.items():
            t = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
            if t:
                enwiki[qid] = t
        # `sizes` already came back with gather(), keyed by the exact English
        # title. Do not re-fetch: that was the request volume being cut.
        rows, claims_by_qid = [], {}
        for qid, ent in ents.items():
            if "missing" in ent:
                continue
            t = enwiki.get(qid)
            label = ((ent.get("labels") or {}).get("en") or {}).get("value") or t
            if not label:
                continue
            desc = ((ent.get("descriptions") or {}).get("en") or {}).get("value")
            # List and index pages sit inside these categories and are never
            # people — "list of historians by area of study" ranked above real
            # historians purely on article length. Checked on both title and label
            # because Wikidata sometimes gives a list item a proper name.
            if any(re.match(r"(?i)^(list|lists|index|timeline|outline|glossary)\b", x)
                   for x in (t, label) if x):
                continue
            # Fictional people are not historical ones. Measured: 41 rows in the
            # war domain were "fictional character from …", reached through the
            # Generals subcategories, and ranked high on article length.
            # Not just the literal phrase: a Shakespeare character is described
            # as "character in Shakespeare's Othello; a Moorish soldier…", and the
            # play itself as "theatrical play by William Shakespeare" — neither
            # says "fictional character", and both landed in `war`.
            if desc and re.search(r"(?i)fictional|character in|\b(play|novel|opera|"
                                  r"film|album|musical|poem|song|series)\b.{0,20}"
                                  r"\bby\b", desc):
                continue
            claims = ent.get("claims") or {}
            birth = death = None
            for pid, key in (("P569", "b"), ("P570", "d")):
                y = year((claims.get(pid) or [{}])[0]
                         .get("mainsnak", {}).get("datavalue", {})
                         .get("value", {}).get("time")) if claims.get(pid) else None
                if key == "b" and birth is None:
                    birth = y
                if key == "d" and death is None:
                    death = y
            rows.append((qid, label, desc, kind, birth, death, None,
                         f"https://en.wikipedia.org/wiki/{t}" if t else None,
                         sizes.get(t, 0),
                         f"https://www.wikidata.org/wiki/{qid}", int(time.time())))
            claims_by_qid[qid] = claims
        rows.sort(key=lambda r: -(r[8] or 0))
        kept, written = 0, set()
        for r in rows:
            if kept >= per:
                break
            if not refresh and c.execute("SELECT 1 FROM entity WHERE qid=?",
                                         (r[0],)).fetchone():
                continue
            c.execute("INSERT OR REPLACE INTO entity VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
            # Claims are stored ONLY for rows actually kept. Storing them while
            # building the candidate list wrote ~11k relations pointing at
            # entities that were never inserted, because the top-N filter runs
            # afterwards.
            store_claims(c, r[0], claims_by_qid[r[0]])
            written.add(r[0])
            kept += 1
        c.commit()
        # Anything of this kind that did not make the cut is stale, not history.
        # Without this a re-ranked domain silently unions old and new rows, which
        # is what left Bismarck's politics row overwritten by a 'linked' stub.
        if prune and refresh:
            if written:
                stale = [q[0] for q in c.execute(
                    "SELECT qid FROM entity WHERE kind=? AND qid NOT IN (%s)"
                    % ",".join("?" * len(written)), (kind, *sorted(written)))]
            else:
                stale = [q[0] for q in c.execute(
                    "SELECT qid FROM entity WHERE kind=?", (kind,))]
            for q in stale:
                c.execute("DELETE FROM relation WHERE src=? OR dst=?", (q, q))
            c.executemany("DELETE FROM entity WHERE qid=?", [(q,) for q in stale])
            c.commit()
            if stale:
                print(f"  {'':12} pruned {len(stale)} stale rows")
        grand += kept
        top = ", ".join(f"{r[1]} ({r[8]//1024}K)" for r in rows[:3] if r[8])
        print(f"  {kind:12} {kept:4d} kept of {len(facts):4d} members — {top}")
    print(f"\n  {grand} written. store: {stats_line(c)}")
    return c


def stats_line(c):
    e = c.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
    r = c.execute("SELECT COUNT(*) FROM relation").fetchone()[0]
    return f"{e} entities, {r} relations, {os.path.getsize(DB)/1e6:.1f} MB"


def stats(c):
    print(f"  {DB}\n  {stats_line(c)}  (notability = EN article bytes)\n\n  by domain:")
    for k, n in c.execute("SELECT kind,COUNT(*) FROM entity GROUP BY kind ORDER BY 2 DESC"):
        print(f"    {k or '?':14} {n}")
    print("\n  most connected:")
    for q, n in c.execute("SELECT src,COUNT(*) n FROM relation GROUP BY src ORDER BY n DESC LIMIT 8"):
        lab = c.execute("SELECT label FROM entity WHERE qid=?", (q,)).fetchone()
        print(f"    {(lab[0] if lab else q)[:44]:46} {n}")


def positions(c, limit=0, all_=False):
    """Backfill P39 tenure for every seeded figure. Fetches only what is missing,
    so it is cheap to re-run."""
    have = {r[0] for r in c.execute("SELECT DISTINCT src FROM relation WHERE pred='P39'")}
    # Seeded domain figures only, by default. Sweeping every 'linked' and
    # 'expanded' entity as well meant 9,296 fetches to find P39 on places, offices
    # and relatives that do not have any.
    people = [r[0] for r in c.execute(
        "SELECT qid FROM entity WHERE kind NOT IN ('office','linked','expanded')"
        + (" LIMIT %d" % limit if limit else ""))]
    todo = people if all_ else [q for q in people if q not in have]
    if not todo:
        print("  every stored figure already has positions")
        return 0
    print(f"  {len(todo)} figures to check")
    n = 0
    for i in range(0, len(todo), 50):
        for qid, ent in fetch_entities(todo[i:i + 50]).items():
            if "missing" in ent:
                continue
            n += store_claims(c, qid, ent.get("claims") or {}, cap=99)
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
            # INSERT OR IGNORE, never OR REPLACE: this function exists only to
            # NAME things relations point at. Overwriting here silently degraded
            # fully-seeded figures into name-only stubs.
            c.execute("INSERT OR IGNORE INTO entity VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (qid, lab, desc, "linked", None, None, None,
                       f"https://en.wikipedia.org/wiki/{sl}" if sl else None,
                       0, f"https://www.wikidata.org/wiki/{qid}", 0))
            n += 1
        c.commit()
    return n


def position_head(label):
    """The head phrase of a Wikidata position label, i.e. the part before the
    first ' of '.

    Wikidata names positions 'Head of Jurisdiction' — 'President of Slovakia',
    'Chancellor of the Exchequer', 'Minister President of Prussia'. That makes the
    head phrase the part that actually identifies the office. Everything after it
    qualifies the office, or belongs to a different concept entirely.

    This is what fixes `office President` returning Lu Muzhen, whose position is
    'spouse of the president of the Republic of China': head 'spouse', so no
    longer a presidency. Plain word-boundary matching does NOT fix that case —
    'president' is a whole word in that string — which is why the head phrase is
    needed rather than \\b alone.
    """
    if not label:
        return ""
    return re.split(r"\s+of\s+", label, maxsplit=1)[0]


def office(c, office_name, yr=None, n=15):
    """Who held a position, optionally during a given year. Matches the POSITION's
    name; the earlier version matched the holder's, so it could never match."""
    like = f"%{office_name}%"
    rows = c.execute("""SELECT h.label, h.qid, pos.label, pos.description,
                               r.qual_start, r.qual_end
                        FROM relation r
                        JOIN entity pos ON pos.qid = r.dst
                        JOIN entity h   ON h.qid   = r.src
                        WHERE r.pred = 'P39'
                          AND (pos.label LIKE ? OR pos.description LIKE ?)""",
                     (like, like)).fetchall()
    # LIKE is only a cheap indexed prefilter — it cannot see word boundaries, and
    # matching the whole label matches qualifiers and unrelated roles.
    #
    # Matching is on the position's LABEL head only. Matching its description as
    # well was tried and reverted: descriptions are sentences rather than
    # 'Head of Jurisdiction' names, so 'spouse of the president of Taiwan'
    # re-admitted the exact false positive the head rule exists to remove.
    term = re.compile(r"\b" + re.escape(office_name) + r"\b", re.I)
    rows = [r for r in rows if term.search(position_head(r[2]))]
    if yr:
        y = int(yr)
        lo, hi = y * 10000 + 101, y * 10000 + 1231   # any day within that year
        def held(a, b):
            # Undated offices are excluded, not assumed: `office President --year
            # 1945` was answering with people whose office had no dates at all.
            # Intervals come from span(), so year-precision tenures count too.
            if a is None and b is None:
                return False
            return (b is None or b >= lo) and (a is None or a <= hi)
        rows = [r for r in rows if held(r[4], r[5])]
    if not rows:
        print(f"  no stored holder of '{office_name}'" + (f" during {yr}" if yr else ""))
        print("  try: eras find <name>, or ask research to expand it")
        return False
    rows.sort(key=lambda r: -(c.execute("SELECT notability FROM entity WHERE qid=?",
                                       (r[1],)).fetchone()[0] or 0))
    for holder, hq, pos, _desc, a, b in rows[:n]:
        ten = (f"{yr_year(a)}-{yr_year(b)}" if a is not None and b is not None
               else f"from {yr_year(a)}" if a is not None
               else f"until {yr_year(b)}" if b is not None else "")
        print(f"  {holder:32} {pos[:38]:40} {ten}")
    return True


STOPWORDS = {
    "who", "whom", "whose", "what", "which", "when", "where", "why", "how",
    "was", "were", "is", "are", "am", "be", "been", "being", "did", "does",
    "do", "has", "have", "had", "the", "and", "for", "with", "from", "that",
    "this", "these", "those", "there", "their", "his", "her", "its", "you",
    "your", "she", "they", "them", "him", "about", "into", "over", "under",
    "during", "tell", "please", "give", "list", "name", "called", "known",
}


def ask(c, question, allow_expand):
    # Question words carry no subject and actively misfire as substrings: asked
    # "who was Otto von Bismarck", the token "who" matched "Doctor Who", and
    # because that counted as a hit, expansion never ran and the real answer was
    # never fetched.
    terms = [t for t in re.findall(r"[A-Za-z][A-Za-z\-']{2,}", question)
             if t.lower() not in STOPWORDS]
    seen, hits = set(), []
    for t in terms:
        for qid, lab, desc, kind, b in c.execute(
                "SELECT qid,label,description,kind,birth FROM entity "
                "WHERE label LIKE ? ORDER BY length(label) LIMIT 12", (f"%{t}%",)):
            if qid in seen:
                continue
            # LIKE cannot see word boundaries, and that is not a detail: asked
            # "who was Otto von Bismarck", "otto" matched *Giotto* and the real
            # subject was never fetched. LIKE stays as the indexed prefilter; this
            # is what makes the match mean something.
            if not re.search(r"\b" + re.escape(t) + r"\b", lab or "", re.I):
                continue
            seen.add(qid)
            hits.append((qid, lab, desc, kind, b))
    strong = [h for h in hits
              if sum(bool(re.search(r"\b" + re.escape(t) + r"\b", h[1] or "", re.I))
                     for t in terms) >= min(2, len(terms))]
    if (not strong) and allow_expand:
        got = expand(c, terms[:6])
        print(f"  (expanded {got} new entities from Wikidata)")
        return ask(c, question, False)
    # Multi-token matches first: "Otto von Bismarck" beats the given name "Otto".
    def score(h):
        return -sum(bool(re.search(r"\b" + re.escape(t) + r"\b", h[1] or "", re.I))
                    for t in terms)
    hits.sort(key=score)
    if not hits:
        print("NOT_IN_STORE")
        return False
    for qid, lab, desc, kind, b in hits[:8]:
        print(f"  {lab}{f' ({b})' if b else ''}  [{kind}]  {qid}")
        if desc:
            print(f"      {desc[:110]}")
        for p, d in c.execute("""SELECT p,d FROM relation r JOIN predicate p ON p.id=r.pred
                                 WHERE r.src=? LIMIT 5""", (qid,)):
            dl = c.execute("SELECT label FROM entity WHERE qid=?", (d,)).fetchone()
            print(f"      {p}: {dl[0] if dl else d}")
    return True


def expand(c, terms):
    """Fetch and cache entities the store lacks.

    Tries the whole phrase and adjacent bigrams as well as single tokens, all in
    one request: asked "who was Otto von Bismarck", matching the loose token
    "otto" alone fetched *Otto*, the given name, instead of the man.
    """
    have = {t.lower() for (t,) in c.execute("SELECT label FROM entity")}
    cands = [" ".join(terms)]
    cands += [" ".join(terms[i:i + 2]) for i in range(len(terms) - 1)]
    cands += terms
    want = list(dict.fromkeys(t for t in cands if t and t.lower() not in have))
    if not want:
        return 0
    ents = fetch_entities(list(titles_to_qids(want[:50])))
    n = 0
    for qid, ent in ents.items():
        if "missing" in ent:
            continue
        lab = ((ent.get("labels") or {}).get("en") or {}).get("value") or qid
        desc = ((ent.get("descriptions") or {}).get("en") or {}).get("value")
        sl = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
        c.execute("INSERT OR REPLACE INTO entity VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (qid, lab, desc, "expanded", None, None, None,
                   f"https://en.wikipedia.org/wiki/{sl}" if sl else None,
                   article_sizes([sl]).get(sl, 0) if sl else 0,
                   f"https://www.wikidata.org/wiki/{qid}", int(time.time())))
        store_claims(c, qid, ent.get("claims") or {})
        n += 1
    c.commit()
    return n


def get_entity(c, qid):
    r = c.execute("SELECT qid,label,description,kind,birth,death,notability,source "
                  "FROM entity WHERE qid=?", (qid,)).fetchone()
    if not r:
        return False
    print(f"\n  {r[1]}  ({r[0]})   [{r[3]}]   {r[6]//1024 if r[6] else 0}K article")
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
    rows = c.execute("""SELECT qid,label,description,kind,birth,notability FROM entity
                        WHERE label LIKE ? OR description LIKE ?
                        ORDER BY length(label) LIMIT ?""", (like, like, n)).fetchall()
    for qid, lab, desc, kind, b, nb in rows:
        print(f"  {(lab or qid)[:40]:42} {kind or '':11}{f'({b})' if b else '':8}"
              f"{(str(nb//1024) + 'K') if nb else '':5}  {(desc or '')[:38]}")
    if not rows:
        print("  nothing yet — run: eras seed")


def main():
    ap = argparse.ArgumentParser(prog="eras")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--per", type=int, default=170)
    s.add_argument("--only", help="comma-separated domains, e.g. politics,war")
    s.add_argument("--refresh", action="store_true")
    s.add_argument("--prune", action="store_true",
                   help="delete rows of a re-seeded domain that no longer make the cut")
    a = sub.add_parser("ask"); a.add_argument("question")
    a.add_argument("--no-expand", action="store_true")
    g = sub.add_parser("get"); g.add_argument("qid")
    f = sub.add_parser("find"); f.add_argument("term")
    f.add_argument("-n", type=int, default=20)
    o = sub.add_parser("office"); o.add_argument("position"); o.add_argument("--year")
    ps = sub.add_parser("positions"); ps.add_argument("--limit", type=int, default=0)
    ps.add_argument("--all", dest="all_", action="store_true",
                    help="re-fetch every figure, not just those with no P39 yet")
    sub.add_parser("stats")
    args = ap.parse_args()
    c = db()
    if args.cmd == "seed":
        seed(args.per, set(args.only.split(",")) if args.only else None,
             args.refresh, args.prune)
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
        n = positions(c, args.limit, args.all_)
        m = resolve_dangling(c)
        print(f"  {n} position records, {m} names resolved")
        print(f"  store: {stats_line(c)}")


if __name__ == "__main__":
    main()
