"""Regress one name's daily returns on another's — the empirical check on a
modelled beta.

The zinc spec's test_1_thumb_rule: the desk says VEDL is a low-beta Hindustan
Zinc. The economics spec makes that claim arithmetically via volumes and
base_ebitda. This measures what the tape actually did, so the claim is checked
rather than asserted.

If modelled and observed disagree, one of three things is true and it matters
which:
  - base_ebitda or volumes are wrong          -> fix the spec
  - the gap is valuation/discount driven      -> it belongs in P3, not P1
  - the sample is too short to say            -> report r2, do not overclaim

Usage:
    python packages/score/observed_beta.py --y vedanta --x hindustan_zinc
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"


def returns(conn, eid: str, since: str | None = None) -> dict[str, float]:
    rows = conn.execute(
        "SELECT date, close FROM prices WHERE entity_id=? AND date>=? ORDER BY date",
        (eid, since or "0000-00-00"),
    ).fetchall()
    out = {}
    for (d0, c0), (d1, c1) in zip(rows, rows[1:]):
        if c0:
            out[d1] = c1 / c0 - 1.0
    return out


def ols(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    beta = sxy / sxx if sxx else 0.0
    alpha = my - beta * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (alpha + beta * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return beta, alpha, r2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--y", required=True, help="dependent (e.g. vedanta)")
    ap.add_argument("--x", required=True, help="independent (e.g. hindustan_zinc)")
    ap.add_argument("--since", default=None,
                    help="exclude data before this date, e.g. after a demerger")
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    ry, rx = returns(conn, a.y, a.since), returns(conn, a.x, a.since)
    conn.close()

    days = sorted(set(ry) & set(rx))
    if len(days) < 20:
        print(f"only {len(days)} overlapping days — too few to regress",
              file=sys.stderr)
        return 1

    xs = [rx[d] for d in days]
    ys = [ry[d] for d in days]
    beta, alpha, r2 = ols(xs, ys)

    # annualised idiosyncratic vol of the residual, for context on the r2
    print(f"{a.y} regressed on {a.x}")
    print(f"  overlapping days : {len(days)}  ({days[0]} -> {days[-1]})")
    print(f"  observed beta    : {beta:.2f}")
    print(f"  r2               : {r2:.2f}")
    print(f"  daily alpha      : {alpha*100:+.3f}%")
    if r2 < 0.3:
        print("  NOTE: low r2 — the pair is driven mostly by something other than\n"
              "        this factor, so the beta is a weak hedge ratio.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
