"""Does an equity actually respond to the commodity its spec says drives it?

A spec asserts an exposure — "APL Apollo BUYS hot-rolled coil, so rising HRC is a
COST"; "iron ore is captive for Tata and bought by JSW". Those are claims about
the world, taken from filings and broker notes, and prices can contradict them.
This is the contradiction test.

GENERALISED FROM packages/review/nickel_jsl.py after running the same analysis by
hand for a second name. That file stays as the written-up nickel/JSL case; this
one is the reusable instrument, and the method notes there apply here verbatim:
weekly log returns on the week's CLOSING print, never levels, and a CONTROL.

THE CONTROL IS THE WHOLE TEST, not an extra. Every Indian metal name correlates
with every commodity to some degree — a shared global risk factor, the rupee, one
domestic industrial cycle. "APL Apollo correlates +0.10 with HRC" means nothing
alone. It only means something against names whose exposure to that driver is
KNOWN and OPPOSITE, or known to be absent.

WHAT IT HAS ALREADY OVERTURNED, both recorded in specs/entities/steel.yaml:

  jindal_stainless / lme_nickel   spec withheld economics claiming nickel is a
      lagged pass-through. Prices agreed and went further: JSL loads +0.021 on
      nickel over 3y against tata_steel's +0.209 — LESS than names consuming none.
  apl_apollo / hrc_india_inr      spec called it "THE HRC MIRROR... the cleanest
      mirror pair in the sector" on the reasoning that a converter is short coil.
      Prices REFUTE it: +0.103 over 3y, statistically indistinguishable from
      tata_steel's +0.076 and jsw_steel's +0.109 — the same sign and size as the
      names that SELL the stuff. There is no measurable short-HRC position to
      trade against a mill.

READ THE SIGN BEFORE THE MAGNITUDE. A claimed COST exposure predicts a NEGATIVE
correlation. Positive-and-similar-to-peers means the driver reaches the equity as
sector beta, not as a cost line, and a bridge line of either sign will add noise.

Usage:
    python packages/review/driver_beta.py --driver hrc_india_inr \\
        --equities apl_apollo tata_steel jsw_steel
    python packages/review/driver_beta.py --driver lme_nickel \\
        --equities jindal_stainless tata_steel jsw_steel --from 2016-01-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from nickel_jsl import (weekly_closes, log_returns, pearson, aligned,  # noqa: E402
                        t_stat)

DEFAULT_FROM = "2023-08-26"     # ~3y; the recent-regime window


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", required=True)
    ap.add_argument("--equities", nargs="+", required=True,
                    help="FIRST is the subject; the rest are controls")
    ap.add_argument("--from", dest="start", default=DEFAULT_FROM)
    ap.add_argument("--to", dest="end", default="2026-12-31")
    ap.add_argument("--max-lag", type=int, default=8)
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    dr = log_returns(weekly_closes(conn, a.driver, a.start, a.end))
    if not dr:
        print(f"no history for {a.driver} in {a.start}..{a.end}")
        return 1
    wk = {e: log_returns(weekly_closes(conn, e, a.start, a.end))
          for e in a.equities}
    missing = [e for e, v in wk.items() if not v]
    if missing:
        print(f"no history for: {', '.join(missing)}")
        return 1

    subject, controls = a.equities[0], a.equities[1:]
    print(f"{a.driver} vs {len(a.equities)} equities · weekly log returns · "
          f"{a.start} .. {a.end}\n")
    print(f"{'equity':20}{'r':>9}{'n':>6}{'|t|':>7}  {'best lag':>9}{'r@lag':>9}")
    print("-" * 62)
    rs = {}
    for e in a.equities:
        xs, ys = aligned(dr, wk[e], 0)
        r = pearson(xs, ys)
        rs[e] = r
        best = max(((lag, pearson(*aligned(dr, wk[e], lag)))
                    for lag in range(-a.max_lag, a.max_lag + 1)),
                   key=lambda t: abs(t[1] or 0.0))
        tag = "  <- subject" if e == subject else ""
        print(f"{e:20}{r:>+9.3f}{len(xs):>6}{abs(t_stat(r, len(xs))):>7.2f}"
              f"  {best[0]:>+9d}{best[1]:>+9.3f}{tag}")

    if controls:
        cv = [rs[e] for e in controls if rs[e] is not None]
        avg = sum(cv) / len(cv) if cv else 0.0
        print(f"\n  subject                {rs[subject]:+.3f}")
        print(f"  controls, average      {avg:+.3f}")
        print(f"  excess over controls   {rs[subject] - avg:+.3f}")
        print("\n  A claimed COST exposure predicts a NEGATIVE subject reading.")
        print("  Positive and close to the controls means the driver arrives as")
        print("  sector beta, not as a cost line — and a bridge line of EITHER")
        print("  sign would add noise rather than signal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
