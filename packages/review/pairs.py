"""Two books, traded as pairs, with a hedge ratio rather than 1:1 notional.

THE BOOKS ARE SEPARATE AND DO NOT NETT AGAINST EACH OTHER.

  zinc       hindustan_zinc  vs  vedanta      (opco vs holdco)
  aluminium  hindalco, nalco, vaml            VAML LONG ONLY -- no borrow yet

A hedge ratio is quoted as: for every Rs 100 of the long leg, short Rs X of the
other. Rs 100 long / Rs 115 short is written 1.15.

WHAT "OPTIMAL" MEANS HERE, BECAUSE THE TWO ANSWERS DISAGREE AND ONLY ONE IS
TRUSTWORTHY.

  MIN-VARIANCE   the ratio that makes the spread flattest -- it is the OLS beta
                 of one leg on the other. This is a hedging question: how much
                 of the short leg cancels the common factor. It does not look at
                 which way the pair paid.

  MAX-RETURN     the ratio that made the most money in-sample. This is NOT a
                 hedge ratio. With two legs and one common factor, raising w
                 until the P&L peaks just tilts the book net-short whichever leg
                 fell. It is reported ONLY so the gap between the two is visible;
                 taking it is fitting 40 days of noise.

If those two ratios are far apart, the trustworthy reading is min-variance, and
the distance between them is a measure of how much of the in-sample return came
from a directional tilt rather than from the pair.

Usage:
    python packages/review/pairs.py
    python packages/review/pairs.py --hold 10
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

ZINC = ["hindustan_zinc", "vedanta"]
ALUMINIUM = ["hindalco", "nalco", "vaml"]

# VAML listed 2026-06-15 and has no borrow. It can be a LONG leg and never a
# short one -- a hard constraint, not a preference, so it is enforced in the
# selection rather than left to the score.
NO_BORROW = {"vaml"}

# VEDL's tape carries an unadjusted demerger here (773.60 -> 271.55). Betas and
# returns must not cross it.
VEDL_DEMERGER = "2026-04-30"


def load(conn):
    rows = conn.execute("SELECT entity_id, date, close FROM prices "
                        "WHERE close IS NOT NULL").fetchall()
    closes = {(e, d): c for e, d, c in rows}
    scores: dict[str, dict[str, float]] = {}
    for as_of, eid, s in conn.execute(
            "SELECT as_of, entity_id, score FROM pillar_scores "
            "WHERE pillar='composite' AND score IS NOT NULL"):
        scores.setdefault(as_of, {})[eid] = s
    return closes, scores


def cal_for(closes, names: list[str], since: str) -> list[str]:
    days = None
    for e in names:
        d = {d for (x, d) in closes if x == e and d > since}
        days = d if days is None else (days & d)
    return sorted(days or [])


def ols_beta(closes, y: str, x: str, cal: list[str]) -> tuple[float, float]:
    """Beta of y on x, plus r2. Daily returns over the shared calendar."""
    ry, rx = [], []
    for d0, d1 in zip(cal, cal[1:]):
        a0, a1 = closes.get((y, d0)), closes.get((y, d1))
        b0, b1 = closes.get((x, d0)), closes.get((x, d1))
        if None in (a0, a1, b0, b1) or not a0 or not b0:
            continue
        ry.append(a1 / a0 - 1.0)
        rx.append(b1 / b0 - 1.0)
    n = len(rx)
    if n < 20:
        return float("nan"), float("nan")
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((v - mx) ** 2 for v in rx)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    beta = sxy / sxx if sxx else float("nan")
    alpha = my - beta * mx
    ss_tot = sum((b - my) ** 2 for b in ry)
    ss_res = sum((b - (alpha + beta * a)) ** 2 for a, b in zip(rx, ry))
    r2 = 1 - ss_res / ss_tot if ss_tot else float("nan")
    return beta, r2


def hedge_ratios(closes, universe: list[str], cal: list[str]) -> dict:
    """beta(long on short) for every ORDERED pair — the direction-aware ratio.

    TWO TRAPS, both of which I walked into on the first pass:

    1. 1/beta IS NOT THE REVERSE HEDGE RATIO. beta(y|x)*beta(x|y) = r2, so with
       r2 = 0.45 the reverse of a 0.87 beta is 0.52, not 1.15. Inverting is only
       right when the two legs are perfectly correlated, which is exactly when
       you would not need a hedge ratio at all. Regress both ways.
    2. THE RATIO FOLLOWS THE LONG LEG, WHICH THE SCORE CHOOSES DAILY. Quoting one
       number per pair silently assumes a direction. The score goes long vedanta
       on 35 of 36 dates, so a ratio derived for long-HZL was answering a trade
       the book never put on.

    P&L = 100*r_long - 100*w*r_short, and r_long = b*r_short + e, so the common
    factor cancels at w = b = beta(long on short). That is the whole derivation.
    """
    out = {}
    for lng in universe:
        for srt in universe:
            if lng == srt:
                continue
            b, r2 = ols_beta(closes, lng, srt, cal)
            out[(lng, srt)] = {"beta": b, "r2": r2}
    return out


def trades(closes, scores, cal: list[str], universe: list[str],
           hold: int, w: float | dict) -> list[tuple[str, str, str, float]]:
    """One trade per score date: long the best-scored, short the worst SHORTABLE.

    Returns (date, long, short, pnl) where pnl is per Rs 100 of the long leg,
    so w scales the short leg's notional directly.
    """
    idx = {d: i for i, d in enumerate(cal)}
    out = []
    for d in sorted(scores):
        if d not in idx or idx[d] + hold >= len(cal):
            continue
        have = {e: s for e, s in scores[d].items() if e in universe}
        shortable = {e: s for e, s in have.items() if e not in NO_BORROW}
        if len(have) < 2 or not shortable:
            continue
        lng = max(have, key=lambda e: have[e])
        srt = min(shortable, key=lambda e: shortable[e])
        if lng == srt:
            # The best-scored name is also the only shortable one: take the
            # next-best shortable as the short, else there is no trade.
            rest = {e: s for e, s in shortable.items() if e != lng}
            if not rest:
                continue
            srt = min(rest, key=lambda e: rest[e])
        d0, d1 = cal[idx[d]], cal[idx[d] + hold]
        try:
            rl = closes[(lng, d1)] / closes[(lng, d0)] - 1.0
            rs = closes[(srt, d1)] / closes[(srt, d0)] - 1.0
        except (KeyError, ZeroDivisionError):
            continue
        # A dict of ordered-pair betas resolves the ratio per trade, so a book
        # that flips direction gets the right hedge on each side.
        wt = w[(lng, srt)]["beta"] if isinstance(w, dict) else w
        if wt != wt:                       # NaN beta — too few days to regress
            continue
        out.append((d, lng, srt, 100.0 * rl - 100.0 * wt * rs))
    return out


def summarise(ts) -> dict:
    p = [t[3] for t in ts]
    if not p:
        return {"n": 0}
    return {"n": len(p), "win": sum(1 for v in p if v > 0) / len(p),
            "avg": statistics.fmean(p), "sd": statistics.stdev(p) if len(p) > 1 else 0.0,
            "total": sum(p)}


def sweep(closes, scores, cal, universe, hold):
    """w that minimises spread volatility, and w that maximised return."""
    grid = [0.25 + 0.01 * i for i in range(226)]        # 0.25 .. 2.50
    best_var, best_ret = None, None
    for w in grid:
        s = summarise(trades(closes, scores, cal, universe, hold, w))
        if not s["n"]:
            continue
        if best_var is None or s["sd"] < best_var[1]["sd"]:
            best_var = (w, s)
        if best_ret is None or s["avg"] > best_ret[1]["avg"]:
            best_ret = (w, s)
    return best_var, best_ret


def report(name, closes, scores, cal, universe, hold, ratios: dict[str, float]):
    print(f"\n{'='*74}\n{name}   hold {hold} trading days   "
          f"{cal[0]} .. {cal[-1]}\n{'='*74}")
    print(f"{'sizing':30}{'ratio':>7}{'n':>5}{'win%':>7}{'avg Rs':>9}"
          f"{'sd':>8}{'total Rs':>10}")
    for label, w in ratios.items():
        s = summarise(trades(closes, scores, cal, universe, hold, w))
        shown = "per-leg" if isinstance(w, dict) else f"{w:.2f}"
        if not s["n"]:
            print(f"{label:30}{shown:>7}   no trades")
            continue
        print(f"{label:30}{shown:>7}{s['n']:>5}{s['win']*100:>7.0f}"
              f"{s['avg']:>+9.2f}{s['sd']:>8.2f}{s['total']:>+10.1f}")
    bv, br = sweep(closes, scores, cal, universe, hold)
    if bv:
        print(f"{'-- flattest spread (in-samp)':30}{bv[0]:>7.2f}{bv[1]['n']:>5}"
              f"{bv[1]['win']*100:>7.0f}{bv[1]['avg']:>+9.2f}{bv[1]['sd']:>8.2f}"
              f"{bv[1]['total']:>+10.1f}")
    if br:
        print(f"{'-- max return  (OVERFIT)':30}{br[0]:>7.2f}{br[1]['n']:>5}"
              f"{br[1]['win']*100:>7.0f}{br[1]['avg']:>+9.2f}{br[1]['sd']:>8.2f}"
              f"{br[1]['total']:>+10.1f}")
    ts = trades(closes, scores, cal, universe, hold, list(ratios.values())[0])
    from collections import Counter
    mix = Counter((t[1], t[2]) for t in ts)
    print("  which pair the score actually picked: "
          + ", ".join(f"{l}/{s} x{n}" for (l, s), n in mix.most_common()))


def borrow_cost(closes, scores, cal, universe, hold, betas):
    """What the no-borrow constraint costs, by lifting it and re-running.

    This is the aluminium book's headline, not a footnote. The score's preferred
    short IS the name that cannot be shorted, on most dates — so the constraint
    does not trim the book, it removes its best idea and leaves a substitute that
    behaves quite differently under hedging.
    """
    global NO_BORROW
    keep = set(NO_BORROW)
    rows = []
    for banned, label in ((keep, "VAML unshortable (reality)"),
                          (set(), "if VAML were shortable")):
        NO_BORROW = banned
        for w, wl in ((1.0, "1:1"), (betas, "beta-neutral")):
            rows.append((label, wl,
                         summarise(trades(closes, scores, cal, universe, hold, w))))
    NO_BORROW = keep

    low = tot = 0
    for d, sc in scores.items():
        have = {e: s for e, s in sc.items() if e in universe}
        if len(have) < 2:
            continue
        tot += 1
        low += min(have, key=lambda e: have[e]) in keep
    print(f"\n  BORROW CONSTRAINT — the score wants to short vaml on {low} of {tot} "
          f"dates ({low/tot*100:.0f}%),")
    print("  which is exactly the trade that cannot be put on.")
    for label, wl, s in rows:
        if s["n"]:
            print(f"    {label:28}{wl:14}win {s['win']*100:>3.0f}%  "
                  f"avg{s['avg']:>+6.2f}  total{s['total']:>+7.1f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=int, default=5)
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    closes, scores = load(conn)
    conn.close()

    zcal = cal_for(closes, ZINC, VEDL_DEMERGER)
    acal = cal_for(closes, ALUMINIUM, "2026-06-15")

    zbeta = hedge_ratios(closes, ZINC, zcal)
    abeta = hedge_ratios(closes, ALUMINIUM, acal)

    print("direction-aware hedge ratios — beta(LONG leg on SHORT leg).")
    print("note both directions are regressed separately; 1/beta would be wrong.")
    for label, m, days in (("zinc", zbeta, len(zcal)), ("aluminium", abeta, len(acal))):
        for (lng, srt), v in m.items():
            if v["beta"] != v["beta"]:
                continue
            flag = "   <- weak hedge, mostly idiosyncratic" if v["r2"] < 0.35 else ""
            print(f"  long {lng:16} short {srt:16} w={v['beta']:>5.2f}  "
                  f"(r2 {v['r2']:.2f}, {days}d){flag}")

    report("ZINC BOOK   hindustan_zinc vs vedanta", closes, scores, zcal, ZINC,
           a.hold, {"equal notional 1:1": 1.00,
                    "beta-neutral (direction-aware)": zbeta,
                    "half-hedged": 0.50})

    report("ALUMINIUM BOOK   VAML long-only", closes, scores, acal, ALUMINIUM,
           a.hold, {"equal notional 1:1": 1.00,
                    "beta-neutral (direction-aware)": abeta,
                    "half-hedged": 0.50})
    borrow_cost(closes, scores, acal, ALUMINIUM, a.hold, abeta)

    print(f"""
{'='*74}
READ THE RATIO COLUMN AGAINST THE sd COLUMN, NOT THE total COLUMN.

`sd` is how much the pair bounces around trade to trade -- that is what the
hedge ratio is for. `total` over {len(scores)} score dates on one sector is a single
episode and will not repeat. If "max return" sits far from "flattest spread",
the difference is a directional bet the ratio smuggled in, not a better hedge.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
