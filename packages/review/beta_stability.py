"""Is a hedge ratio a constant, or just the last two months?

`pairs.py` recommends sizing at the observed beta, fitted on 45-77 days because
that is all the SCORES cover. Prices go back two years. A beta fitted on 45 days
and quoted to two decimals is a claim about stability that nobody checked, and a
hedge ratio that moves is worse than no hedge ratio -- you rebalance into noise.

This regresses the same pairs on the full two years and on rolling windows, so
the recommendation can be stated with a range instead of a spurious decimal.

WHAT CANNOT BE EXTENDED, and why the two live trades are not both here:

  vedanta    demerged 2026-04-30 (773.60 -> 271.55). Anything earlier is a
             different company that also owned aluminium and oil & gas. The
             zinc pair therefore has 77 days and cannot have more until time
             passes. NOT a data-sourcing problem.
  vaml       listed 2026-06-15. 46 days exist in the world.

So only hindalco / nalco / hindustan_zinc carry clean two-year tapes, and of the
two live trades only long nalco / short hindalco can be checked at all.

Usage:
    python packages/review/beta_stability.py
    python packages/review/beta_stability.py --window 60
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

CLEAN = ["hindalco", "nalco", "hindustan_zinc"]
PAIRS = [("nalco", "hindalco"), ("hindustan_zinc", "hindalco"),
         ("hindustan_zinc", "nalco")]

# Jumps flagged by check_corporate_actions.py on the extended tape. These are
# front-month futures rolls, not price moves, and a bridge that reads them as
# shocks would book a 21% alumina crash that never happened. Equity-on-equity
# betas below do not touch these series -- recorded so the next person does not
# quietly extend the ECONOMICS pillar across them.
CONTRACT_BREAKS = {
    "alumina_index":   [("2025-02-03", -21.3)],
    "midwest_premium": [("2025-02-03", 18.9), ("2025-06-02", 53.8),
                        ("2025-06-03", -15.3)],
    "silver":          [("2026-01-30", -31.3)],
    "vedanta":         [("2026-04-30", -64.9)],   # demerger, not a roll
}


def rets(conn, eid: str) -> dict[str, float]:
    rows = conn.execute("SELECT date, close FROM prices WHERE entity_id=? "
                        "AND close IS NOT NULL ORDER BY date", (eid,)).fetchall()
    return {d1: c1 / c0 - 1.0 for (d0, c0), (d1, c1) in zip(rows, rows[1:]) if c0}


def beta(xs, ys) -> tuple[float, float]:
    n = len(xs)
    if n < 20:
        return float("nan"), float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((v - mx) ** 2 for v in xs)
    if not sxx:
        return float("nan"), float("nan")
    b = sum((a - mx) * (c - my) for a, c in zip(xs, ys)) / sxx
    al = my - b * mx
    sst = sum((c - my) ** 2 for c in ys)
    ssr = sum((c - (al + b * a)) ** 2 for a, c in zip(xs, ys))
    return b, (1 - ssr / sst if sst else float("nan"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=60)
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    R = {e: rets(conn, e) for e in CLEAN}
    conn.close()

    print(f"rolling {a.window}-day beta of LONG leg on SHORT leg, two-year tape\n")
    print(f"{'pair':34}{'full':>7}{'r2':>6}{'min':>7}{'p25':>7}{'med':>7}"
          f"{'p75':>7}{'max':>7}{'last':>7}")
    print("-" * 89)
    for y, x in PAIRS:
        days = sorted(set(R[y]) & set(R[x]))
        xs = [R[x][d] for d in days]
        ys = [R[y][d] for d in days]
        b_full, r2 = beta(xs, ys)

        roll = []
        for i in range(a.window, len(days) + 1):
            bw, _ = beta(xs[i - a.window:i], ys[i - a.window:i])
            if bw == bw:
                roll.append(bw)
        if not roll:
            continue
        q = statistics.quantiles(roll, n=4)
        print(f"{'long '+y+' / short '+x:34}{b_full:>7.2f}{r2:>6.2f}"
              f"{min(roll):>7.2f}{q[0]:>7.2f}{q[1]:>7.2f}{q[2]:>7.2f}"
              f"{max(roll):>7.2f}{roll[-1]:>7.2f}")

    days = sorted(set(R["nalco"]) & set(R["hindalco"]))
    print(f"\n{len(days)} overlapping days  {days[0]} .. {days[-1]}")

    print("""
WHAT THIS CHANGES.

`pairs.py` fits its ratio on the 45 days the SCORES cover. Compare that number
to the `min`..`max` column here before trusting its second decimal. If the
rolling beta ranges over a wide band, the honest recommendation is a round
number in the middle of the band, rebalanced rarely -- not a precise ratio
refitted often, which just churns the book against estimation noise.

The two-year tape cannot lengthen the SCORE backtest. Three of four pillars are
built from the broker digests, and that corpus starts 2026-06-17 -- 47 files,
all June-August 2026. Prices were never the binding constraint.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
