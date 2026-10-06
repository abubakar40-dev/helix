#!/usr/bin/env python3
"""Regression check for eras office position matching.

Run:  python3 tools/check-office-match.py

Guards the bug where `eras office President` returned Lu Muzhen, whose position is
"spouse of the president of the Republic of China" — a substring match on the
whole position label. The fix matches the LABEL HEAD (the part before the first
" of ", which is how Wikidata names positions: Head of Jurisdiction) at word
boundaries, and deliberately does NOT match the position's description.

Exits non-zero on failure.
"""
import contextlib
import io
import re
import sys

sys.path.insert(0, "/home/abubakargcx/helix/tools")
import eras  # noqa: E402


def capture(fn):
    """Run office() and return its printed rows."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return [l for l in buf.getvalue().splitlines() if l.strip()]

# (position label, query term, should match?)
CASES = [
    # the reported bug
    ("spouse of the president of the Republic of China", "President", False),
    ("spouse of the President of the People's Republic of China", "President", False),
    # real presidencies must still match
    ("President of Slovakia", "President", True),
    ("President of the Federal Executive Council of Yugoslavia", "President", True),
    ("President of Iceland", "President", True),
    ("Minister President of Prussia", "President", True),
    ("Minister President of Prussia", "Minister President", True),
    # the term must match anywhere in the head, not only at its start —
    # "office chancellor" has to find "Imperial chancellor"
    ("Imperial chancellor", "chancellor", True),
    ("Chancellor of the Exchequer", "chancellor", True),
    # unrelated roles
    ("Member of the Reichstag of the German Empire", "President", False),
    ("Member of parliament", "chancellor", False),
    ("Referendar", "President", False),
    # a query that itself contains " of " must be reduced the same way, or
    # `office "President of Nauru"` silently finds nothing
    ("President of Nauru", "President of Nauru", True),
    ("President of Slovakia", "President of Nauru", True),
    ("President of Slovakia", "spouse of the president", False),
]


def main():
    fails = []
    for label, term, want in CASES:
        head = eras.position_head(label)
        q = eras.position_head(term) or term
        got = bool(re.search(r"\b" + re.escape(q) + r"\b", head, re.I))
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} head={head[:40]:42} "
              f"query={term:20} -> {got} (want {want})")
        if not ok:
            fails.append((label, term, want, got))

    print(f"\n  {len(CASES) - len(fails)}/{len(CASES)} label-head cases correct")

    # Now exercise office() ITSELF rather than reimplementing its filter. An
    # earlier version of this check duplicated the matching logic inline, and
    # therefore still passed after the fix in office() was reverted — a check
    # that cannot fail is worse than no check.
    c = eras.db()
    out = capture(lambda: eras.office(c, "President", "1945"))
    print(f"\n  office(c, 'President', '1945') returned {len(out)} row(s):")
    for line in out:
        print(f"    {line}")

    spouses = [l for l in out if re.search(r"(?i)spouse", l)]
    if spouses:
        fails.append(("office()", "President", 0, len(spouses)))
        print(f"  FAIL: {len(spouses)} spouse-of-president row(s) in office() output, "
              f"e.g. {spouses[0].strip()[:60]}")
    else:
        print("  ok   no 'spouse of the president…' row in office() output")

    if not out:
        fails.append(("office()", "President", ">0", 0))
        print("  FAIL: office() returned nothing — the filter is too strict")
    else:
        print("  ok   office() still returns real presidencies")

    # Regression guard: the flagship queries must keep working.
    for term, yr, who in (("chancellor", "1871", "Bismarck"),
                          ("Minister President", "1865", "Bismarck")):
        got = capture(lambda t=term, y=yr: eras.office(c, t, y))
        hit = any(who in l for l in got)
        print(f"  {'ok  ' if hit else 'FAIL'} office({term!r}, {yr!r}) "
              f"{'finds ' + who if hit else 'lost ' + who}")
        if not hit:
            fails.append((f"office({term},{yr})", who, True, False))

    if fails:
        print(f"\nFAILED ({len(fails)})")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
