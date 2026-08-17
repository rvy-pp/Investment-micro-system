"""Does a company's own score predict that company's own forward return?

No pairs, no ranking, no hedge. One row per (company, date): the score, and what
the stock did over the next h trading days. Pooled across companies.

BOTH RETURN DEFINITIONS ARE REPORTED, because they answer different questions and
the gap between them is itself the finding:

  ABSOLUTE  what the stock did. This is what "predictability" means in plain
            terms, and it is what the desk actually earns on an outright.
  RELATIVE  return minus that day's average across the covered names. Strips the
            sector move. A score built from commodity shocks that hit every name
            at once can look predictive on absolute returns purely by tracking
            the sector, so absolute alone will flatter it.

BUCKETS, NOT JUST A CORRELATION. A correlation assumes the relationship is
monotone and roughly linear. The score is deliberately non-linear (flat near 3,
steep in the decision zone), so the useful question is whether the HIGH bucket
beats the LOW bucket, which a bucket table shows and a single r hides.

Usage:
    python packages/review/predictability.py
    python packages/review/predictability.py --pillar composite
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

BUCKETS = [(1.0, 2.5, "1.0-2.5  bearish"), (2.5, 2.9, "2.5-2.9  mild bear"),
           (2.9, 3.1, "2.9-3.1  neutral"), (3.1, 3.5, "3.1-3.5  mild bull"),
           (3.5, 5.01, "3.5-5.0  bullish")]

VEDL_DEMERGER = "2026-04-30"


def load(conn, pillar: str):
    closes = {(e, d): c for e, d, c in conn.execute(
        "SELECT entity_id, date, close FROM prices WHERE close IS NOT NULL")}
    cal: dict[str, list[str]] = {}
    for (e, d) in closes:
        cal.setdefault(e, []).append(d)
    for e in cal:
        cal[e].sort()
    rows = conn.execute(
        "SELECT as_of, entity_id, score FROM pillar_scores WHERE pillar=? "
        "AND score IS NOT NULL ORDER BY as_of", (pillar,)).fetchall()
    return closes, cal, rows


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = sum((a - mx) ** 2 for a in xs) ** 0.5
    dy = sum((b - my) ** 2 for b in ys) ** 0.5
    return num / (dx * dy) if dx and dy else None


def build(closes, cal, rows, h):
    """[(score, abs_ret, entity, date)] plus the day's mean for de-meaning."""
    out = []
    for as_of, eid, s in rows:
        c = cal.get(eid)
        if not c:
            continue
        try:
            i = c.index(as_of)
        except ValueError:
            continue
        if i + h >= len(c):
            continue
        d0, d1 = c[i], c[i + h]
        # VEDL's tape is not comparable across its demerger.
        if eid == "vedanta" and d0 < VEDL_DEMERGER <= d1:
            continue
        a, b = closes[(eid, d0)], closes[(eid, d1)]
        if not a:
            continue
        out.append((s, b / a - 1.0, eid, as_of))
    byday: dict[str, list[float]] = {}
    for s, r, e, d in out:
        byday.setdefault(d, []).append(r)
    means = {d: statistics.fmean(v) for d, v in byday.items()}
    return [(s, r, r - means[d], e, d) for s, r, r_, e, d in
            [(s, r, None, e, d) for s, r, e, d in out]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pillar", default="economics")
    ap.add_argument("--horizons", default="1,3,5,10,20,40")
    a = ap.parse_args()
    hs = [int(x) for x in a.horizons.split(",")]

    conn = sqlite3.connect(DB)
    closes, cal, rows = load(conn, a.pillar)
    conn.close()
    if not rows:
        print(f"no {a.pillar} scores")
        return 1

    ents = sorted({e for _, e, _ in rows})
    print(f"pillar={a.pillar}   {len(rows)} scored rows   "
          f"{rows[0][0]} .. {rows[-1][0]}   names: {', '.join(ents)}\n")

    print(f"{'h':>4}{'n':>7}{'r(abs)':>9}{'r(rel)':>9}{'meanAbs%':>10}"
          f"{'hi-lo abs%':>12}{'hi-lo rel%':>12}")
    print("-" * 63)
    tables = {}
    for h in hs:
        data = build(closes, cal, rows, h)
        if len(data) < 30:
            continue
        sc = [d[0] for d in data]
        ab = [d[1] for d in data]
        re = [d[2] for d in data]
        ra, rr = pearson(sc, ab), pearson(sc, re)
        lo = [d for d in data if d[0] < 2.9]
        hi = [d for d in data if d[0] > 3.1]
        hl_a = (statistics.fmean([d[1] for d in hi]) -
                statistics.fmean([d[1] for d in lo])) * 100 if lo and hi else None
        hl_r = (statistics.fmean([d[2] for d in hi]) -
                statistics.fmean([d[2] for d in lo])) * 100 if lo and hi else None
        f = lambda v, w, p: f"{v:>+{w}.{p}f}" if v is not None else f"{'—':>{w}}"
        print(f"{h:>4}{len(data):>7}{f(ra,9,3)}{f(rr,9,3)}"
              f"{statistics.fmean(ab)*100:>+10.2f}{f(hl_a,12,2)}{f(hl_r,12,2)}")
        tables[h] = data

    hpick = 20 if 20 in tables else max(tables)
    print(f"\nSCORE BUCKET vs forward return, h={hpick}")
    print(f"  {'bucket':22}{'n':>6}{'mean abs%':>11}{'mean rel%':>11}{'win%':>7}")
    for lo_, hi_, label in BUCKETS:
        sub = [d for d in tables[hpick] if lo_ <= d[0] < hi_]
        if not sub:
            print(f"  {label:22}{0:>6}")
            continue
        print(f"  {label:22}{len(sub):>6}"
              f"{statistics.fmean([d[1] for d in sub])*100:>+11.2f}"
              f"{statistics.fmean([d[2] for d in sub])*100:>+11.2f}"
              f"{sum(1 for d in sub if d[1] > 0)/len(sub)*100:>7.0f}")

    print("""
r(abs) vs r(rel) is the check that matters. If r(abs) is decent and r(rel) is
zero, the score is tracking the sector, not the company -- every name gets the
same commodity shock, so the score rises with a tape that was going to rise
anyway. Only r(rel) says the score picked THIS name out of its peers.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
