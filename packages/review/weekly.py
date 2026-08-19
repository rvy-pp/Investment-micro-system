"""P1 scored on WEEKLY AVERAGE commodity prices, rebalanced weekly.

THE HYPOTHESIS. The monthly test samples the commodity level on one day a month,
so a spike on the sampling date becomes the whole signal and the same week
sampled a day earlier gives a different answer. A weekly MEAN uses every print in
the week, which removes that sampling luck without slowing the signal down —
it should be a strictly better estimate of the same underlying move.

WHAT IS HELD CONSTANT so the comparison is fair: the same bridge, the same specs,
the same hill curve, the same three names, the same long-top/short-bottom rule,
the same 2:1 and 1:1 sizings. Only the sampling frequency and the shock
construction change.

TWO WEEKLY VARIANTS, because they answer different questions:
  average  shock = mean(this week's prints) - mean(last week's)
  last     shock = last print this week - last print last week
If `average` beats `last`, the gain is from noise reduction. If they are the
same, weekly sampling per se was the thing that mattered and the averaging is
decoration.

Usage:
    python packages/review/weekly.py
    python packages/review/weekly.py --holds 4,8,13,26
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "score"))

from bridge import load_specs, load_scoring, run_bridge, _series_in_store  # noqa
from scoring import score as to_score  # noqa

TRIO = ["nalco", "hindalco", "vaml"]
CO = {"nalco": "NALCO", "hindalco": "Hindalco", "vaml": "VAML",
      "hindustan_zinc": "Hindustan Zinc", "vedanta": "Vedanta"}
# Only 2021+ is structurally comparable — Hindalco's Mahan/Aditya smelters ramped
# 2013-16 and tripled its Indian capacity, and every spec is effective_from
# 2026-04-01. See the audit workbook README.
START = "2021-01-01"
DRIVERS = ["lme_aluminium", "alumina_index", "thermal_coal_seaborne", "cp_coke",
           "silver", "usdinr", "lme_zinc"]


def monday(d: str) -> str:
    x = dt.date.fromisoformat(d)
    return (x - dt.timedelta(days=x.weekday())).isoformat()


def load(conn):
    px = {}
    for e, d, c in conn.execute("SELECT entity_id,date,close FROM prices "
                                "WHERE close IS NOT NULL AND date>=?", (START,)):
        px.setdefault(e, {})[d] = c
    return px


def weekly(px, eid, how):
    """{week_monday: level} using either the mean of the week or its last print."""
    buckets: dict[str, list[tuple[str, float]]] = {}
    for d, v in px.get(eid, {}).items():
        buckets.setdefault(monday(d), []).append((d, v))
    out = {}
    for w, pts in buckets.items():
        pts.sort()
        out[w] = statistics.fmean(v for _, v in pts) if how == "average" else pts[-1][1]
    return out


def build_scores(px, how, ents, units, fin, form, k, p, avail):
    """Score every company on every week, from week-on-week shocks."""
    wk = {d: weekly(px, d, how) for d in DRIVERS}
    weeks = sorted(set.intersection(*[set(v) for v in wk.values() if v]))
    fx_w = wk.get("usdinr", {})
    out: dict[str, dict[str, float]] = {}
    for prev, cur in zip(weeks, weeks[1:]):
        shocks = {}
        for d in DRIVERS:
            if d == "usdinr":
                continue
            a, b = wk[d].get(prev), wk[d].get(cur)
            if a is not None and b is not None:
                shocks[d] = b - a
        if not shocks:
            continue
        usdinr = fx_w.get(cur) or fin["usdinr"]
        for eid in TRIO + ["hindustan_zinc", "vedanta"]:
            ent = ents.get(eid)
            if not ent:
                continue
            be = fin["companies"].get(eid, {}).get("base_ebitda", 0)
            r = run_bridge(ent, shocks, units, be, usdinr, avail | set(shocks))
            pct = r["pct_of_ebitda"]
            if pct is not None and r["coverage_ok"]:
                out.setdefault(cur, {})[eid] = to_score(pct, k, form, p)
    return out


def fwd(px, eid, entry, weeks_held):
    """Return over `weeks_held` weeks from the first session on/after entry."""
    real = "vedanta" if (eid == "vaml" and entry < "2026-06-15") else eid
    days = sorted(px.get(real, {}))
    nxt = [d for d in days if d >= entry]
    if not nxt:
        return None
    d0 = nxt[0]
    target = (dt.date.fromisoformat(d0) + dt.timedelta(weeks=weeks_held)).isoformat()
    later = [d for d in days if d >= target]
    if not later:
        return None
    d1 = later[0]
    if real == "vedanta" and d0 < "2026-04-30" <= d1:
        return None
    a, b = px[real][d0], px[real][d1]
    return b / a - 1 if a else None


def trades(px, scores, hold, wl, ws_):
    pl = []
    for w in sorted(scores):
        have = {e: v for e, v in scores[w].items() if e in TRIO}
        if len(have) < 2:
            continue
        lng = max(have, key=lambda e: have[e])
        srt = min(have, key=lambda e: have[e])
        if lng == srt:
            continue
        rl, rs = fwd(px, lng, w, hold), fwd(px, srt, w, hold)
        if rl is None or rs is None:
            continue
        pl.append((wl * rl - ws_ * rs) / (wl + ws_) * 100)
    return pl


def show(pl, hold, label, sizing):
    if len(pl) < 8:
        return
    sd = statistics.stdev(pl)
    # Weekly entries held `hold` weeks overlap almost completely, so the naive
    # count massively overstates the sample. Independent windows is what matters.
    print(f"  {label:20}{sizing:>8}{hold:>6}{len(pl):>6}{len(pl)//hold:>8}"
          f"{sum(1 for x in pl if x > 0)/len(pl)*100:>7.0f}"
          f"{statistics.fmean(pl):>+9.2f}{statistics.median(pl):>+9.2f}"
          f"{statistics.fmean(pl)/sd if sd else 0:>8.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holds", default="4,8,13,26")
    a = ap.parse_args()
    holds = [int(x) for x in a.holds.split(",")]

    conn = sqlite3.connect(REPO / "data" / "ims.db")
    px = load(conn)
    conn.close()
    ents, units, fin = load_specs()
    form, k, p = load_scoring()
    avail = _series_in_store()

    built = {how: build_scores(px, how, ents, units, fin, form, k, p, avail)
             for how in ("average", "last")}
    for how, sc in built.items():
        ws_ = sorted(sc)
        print(f"{how:8} weekly scores: {len(ws_)} weeks  {ws_[0]} .. {ws_[-1]}")

    print(f"\n{'  variant':20}{'sizing':>8}{'hold':>6}{'n':>6}{'indep':>8}"
          f"{'win%':>7}{'avg%':>9}{'med%':>9}{'sharpe':>8}")
    print("  " + "-" * 79)
    for how in ("average", "last"):
        for hold in holds:
            for sizing, (wl, wsx) in (("2:1", (2, 1)), ("1:1 MV", (1, 1))):
                show(trades(px, built[how], hold, wl, wsx), hold,
                     f"weekly {how}", sizing)
        print()

    # Does averaging change the ranking at all, or only the score level?
    A, L = built["average"], built["last"]
    both = [w for w in A if w in L]
    same = 0
    for w in both:
        fa = {e: v for e, v in A[w].items() if e in TRIO}
        fl = {e: v for e, v in L[w].items() if e in TRIO}
        if len(fa) < 2 or len(fl) < 2:
            continue
        same += (max(fa, key=lambda e: fa[e]) == max(fl, key=lambda e: fl[e]))
    print(f"  average vs last pick the same LONG leg on {same}/{len(both)} weeks "
          f"({same/len(both)*100:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
