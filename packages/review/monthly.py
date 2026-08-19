"""The pillar score, sampled MONTHLY and held long — the vault's frequency.

The daily backtest found nothing. Before concluding the economics pillar has no
information, run it the way the vault's model runs: rank at month end, hold 30 to
150 trading days, size 2:1. If the signal is real but slow, this finds it; if it
is still flat here, the pillar and not the frequency is the problem.

It also answers the question that matters more than either backtest: DOES THE
PILLAR SCORE AGREE WITH THE REGIME RANK? Both claim to rank the same three names
off the same two commodities. If they agree, the new system reproduces the vault's
model and can extend it to zinc. If they disagree, one of them is wrong and the
regime model is the one with 74% at 90 days.

Alpha is capital-normalised exactly as the vault does it, so the numbers are
directly comparable:

    alpha = (w_long * r_long - w_short * r_short) / (w_long + w_short)

Usage:
    python packages/review/monthly.py
    python packages/review/monthly.py --sizing 1:1
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from regime_pairs import regimes, closes, fwd, ROTATION  # noqa: E402

TRIO = ["nalco", "hindalco", "vaml"]
HORIZONS = [10, 30, 60, 90, 135, 150]


def month_end_scores(conn, pillar: str) -> dict[str, dict[str, float]]:
    """Last available score in each calendar month, per entity."""
    out: dict[str, dict[str, float]] = {}
    for as_of, eid, s in conn.execute(
            "SELECT as_of, entity_id, score FROM pillar_scores WHERE pillar=? "
            "AND score IS NOT NULL ORDER BY as_of", (pillar,)):
        out.setdefault(as_of[:7], {})[eid] = s     # later date overwrites
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizing", default="2:1")
    ap.add_argument("--pillar", default="economics")
    a = ap.parse_args()
    wl, ws = (float(x) for x in a.sizing.split(":"))

    conn = sqlite3.connect(DB)
    px = closes(conn)
    ms = month_end_scores(conn, a.pillar)
    conn.close()
    regs = {r["month"]: r for r in regimes()}

    months = sorted(m for m in ms if len(ms[m]) >= 2 and m in regs)
    print(f"pillar={a.pillar} sampled month-end   {len(months)} months "
          f"{months[0]} .. {months[-1]}   sizing {a.sizing}")

    # ---- 1. score-ranked pair: long best-scored, short worst-scored ----
    print(f"\nSCORE-RANKED PAIR (long top, short bottom of whatever scored)")
    print(f"{'h':>5}{'n':>5}{'avg a%':>9}{'win%':>7}{'sharpe':>8}")
    for h in HORIZONS:
        al = []
        for m in months:
            have = {e: v for e, v in ms[m].items() if e in TRIO}
            if len(have) < 2:
                continue
            lng = max(have, key=lambda e: have[e])
            srt = min(have, key=lambda e: have[e])
            if lng == srt:
                continue
            rl, rs = fwd(px, lng, f"{m}-01", h), fwd(px, srt, f"{m}-01", h)
            if rl is None or rs is None:
                continue
            al.append((wl * rl - ws * rs) / (wl + ws) * 100)
        if len(al) < 3:
            continue
        sd = statistics.stdev(al)
        print(f"{h:>5}{len(al):>5}{statistics.fmean(al):>+9.2f}"
              f"{sum(1 for x in al if x>0)/len(al)*100:>7.0f}"
              f"{statistics.fmean(al)/sd if sd else 0:>8.3f}")

    # ---- 2. do the two models agree on the trade? ----
    print(f"\nAGREEMENT — pillar's top pick vs the regime's long leg")
    agree = tot = 0
    rows = []
    for m in months:
        have = {e: v for e, v in ms[m].items() if e in TRIO}
        if len(have) < 2:
            continue
        lng = max(have, key=lambda e: have[e])
        rg = regs[m]["regime"]
        want = ROTATION[rg][0]
        tot += 1
        agree += lng == want
        rows.append((m, rg, lng, want, lng == want))
    print(f"  agree on {agree} of {tot} months ({agree/tot*100:.0f}%)")
    by_reg: dict[str, list[bool]] = {}
    for _, rg, _, _, ok in rows:
        by_reg.setdefault(rg, []).append(ok)
    for rg in sorted(by_reg):
        v = by_reg[rg]
        print(f"    {rg}: {sum(v)}/{len(v)}")
    print("  last 8 months:")
    for m, rg, lng, want, ok in rows[-8:]:
        print(f"    {m}  {rg}  pillar says {lng:9} regime says {want:9} "
              f"{'ok' if ok else 'DIFFER'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
