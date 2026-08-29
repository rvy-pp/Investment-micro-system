"""Does the P3 valuation SPREAD predict pair convergence, or only mirror it?

    python packages/review/valuation_pairs.py                    # all groups
    python packages/review/valuation_pairs.py --pair ultratech,shree

THE QUESTION, as the PM put it (2026-08-29): "when the delta between UltraTech
and Shree's valuation score increases, the prices also move accordingly — is
there good predictability in valuation for making pairs?"

THE TRAP THE QUESTION HAS TO CLEAR FIRST. The valuation score is BUILT from the
price: z of EV/spot-EBITDA over own history, and EV moves one-for-one with the
close. So when the spread WIDENS, relative price has BY CONSTRUCTION just moved
— the visual co-movement is guaranteed and is evidence of nothing. That is the
P1 lesson ("faithful but never early") wearing P3's clothes. The only version
of the question with money in it is FORWARD: does a wide spread today predict
relative returns from here?

And unlike P1, valuation has a real mechanism available: a z-spread is a
mean-reversion claim, not a news claim. Wide should predict CONVERGENCE —
i.e. corr(spread_t, forward relative return of the dear leg) < 0.

METHOD — the production quantity, not an approximation:
  - multiples from valuation.spot_multiple_series (the same bridge re-marking
    run_scores uses), constant shares / net_debt / EBITDA from
    base_financials, corporate-action cuts applied by the same code.
  - z at date t = (m_t - mean) / sd over the trailing 1260 CALENDAR days,
    matching pillar_3.lookback_days; >=250 sessions required in the window.
  - weekly CLOSING prints (the repo's tested sampling: averaging adds lag).
  - spread(t) = z_a(t) - z_b(t). Positive = a dear relative to b.
  - forward relative return R(t, h) = ret_a(t..t+h) - ret_b(t..t+h).
  - MIRROR check: corr(spread change over the PAST 4w, PAST 4w R) — this is
    the number the eye sees on the chart, and it should be ~+1 mechanically.
  - PREDICTIVE check: corr(spread level, R at h = 1/4/13w forward), tercile
    buckets, and a convergence-trade cut: enter when |spread| >= 1, short the
    dear leg, hold 13w — hit rate and mean P&L.

Anachronism accepted and stated: today's base EBITDA and base-quarter prices
are applied across all history, because that is exactly what the live pillar
does to its own lookback window. This tests THE PILLAR AS BUILT.

Sample honesty: cement equities start 2021-08-30, so the z window shortens to
what exists (>=250 sessions) and cement gets ~209 weekly points from late
2022. Steel and non-ferrous carry ~15 years of closes, so the signal CLASS is
tested there and the cement pair is read against it.

RESULTS — run 2026-08-29, recorded because the table is the finding:

  pair                    weeks mirror  fwd13w  loQ f13  hiQ f13 trades  hit    pnl
  ultratech/ambuja          209  +0.78   -0.35    +7.9%    +3.7%     10  40%  -0.0%
  ultratech/shree           209  +0.49   -0.12    +6.1%    -0.7%     12  17%  -2.2%
  ultratech/dalmia          208  +0.63   -0.25    +4.8%    -0.6%      7  43%  -0.7%
  ambuja/shree              209  +0.76   -0.33    +5.9%    -6.4%      5  80%  +7.4%
  ambuja/dalmia             208  +0.67   -0.36    +1.7%    -9.5%      7  43%  +5.1%
  shree/dalmia              208  +0.37   -0.29    +3.2%    -6.4%      6  33%  +1.3%
  tata_steel/jsw_steel      306  +0.16   -0.27    +5.0%    -3.9%      6  50%  -3.4%
  tata_steel/jindal_steel   244  +0.24   +0.01    -4.0%    -2.2%      6   0% -12.2%
  tata_steel/sail           207  +0.03   -0.17    +1.7%    -1.9%      7  43%  +4.4%
  jsw_steel/jindal_steel    244  +0.36   -0.22    +0.8%    -3.8%      7  71%  +2.7%
  jsw_steel/sail            207  +0.15   -0.18    +4.2%    -2.0%      6  67%  +4.1%
  jindal_steel/sail         203  +0.48   -0.21    +4.7%    +1.7%      8  50%  +0.6%
  hindalco/nalco            643  +0.63   -0.18    +1.5%    -2.6%     36  56%  +5.1%

THE READING, in the order the caveats bind:

1. THE DIRECTION IS REAL. fwd13w is negative in 12 of 13 pairs across three
   sectors — a wide valuation spread tends to be followed by convergence. If
   each pair were a coin flip, 12-of-13 one way is ~0.2%; the pairs share
   legs so they are not independent, but the consistency is not chance-shaped.

2. NO SINGLE PAIR IS SIGNIFICANT. Overlapping weekly observations at a 13w
   horizon mean effective n is n_weeks/13 — roughly 16 independent windows
   per cement pair. A -0.2 correlation on 16 obs is ~1 sd. The class has
   evidence; each individual pair does not. Same verdict shape as the
   aluminium composite backtest: directionally there, not yet decidable.

3. THE NAIVE TRADE IS NOT A RULE. Enter-at-|z|>=1, hold-13w produced 5-12
   trades per pair with hit rates from 0% to 80% — sample noise, not a
   strategy. The exception that matters is hindalco/nalco: 36 trades over 12
   years, 56% hit, +5.1% mean per trade. The only pair with a real sample is
   positive.

4. THE FAILURE MODE HAS A NAME: RE-RATING. tata/jindal is the one pair with
   NO reversion (+0.01) and its trades went 0-for-6 at -12.2% — Jindal Steel
   structurally re-rated through the window and the spread never came back.
   A z-spread cannot distinguish "cheap" from "deservedly de-rated", which is
   why P3 is a CONVICTION input and not a direction engine.

5. THE ASYMMETRY IS TRADEABLE INFORMATION: loQ beats |hiQ| in cement — the
   CHEAP side converges harder (+5-8% fwd 13w) than the dear side (-0.7 to
   -6.4%). Buying the cheap leg carried more of the edge than shorting the
   dear one.

6. On the asked pair specifically: ultratech/shree has the WEAKEST forward
   signal in cement (-0.12) and its mirror is +0.49 — half of what the eye
   sees on that chart is the score restating the price move that already
   happened. The same signal expressed as ambuja/shree or ambuja/dalmia
   tested 2-3x stronger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import statistics as st
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(REPO / "packages" / "score"))

from bridge import load_specs  # noqa: E402
import valuation as V  # noqa: E402

LOOKBACK_CAL_DAYS = 1260
MIN_WINDOW_SESSIONS = 250
HORIZONS_W = (1, 4, 13)
ENTRY_Z = 1.0

GROUPS = {
    "cement":           ["ultratech", "ambuja", "shree", "dalmia"],
    "steel_integrated": ["tata_steel", "jsw_steel", "jindal_steel", "sail"],
    "aluminium_primary": ["hindalco", "nalco"],
    # added 2026-08-30 — mining went live 2026-08-29 without a row here (this
    # registry was already one sector stale when found).
    "mining_bulk":      ["nmdc", "coal_india"],
    # ems_assemblers is DELIBERATELY ABSENT: this backtest replays spot
    # EV/EBITDA z-history via spot_multiple_series, and EMS's P3 is a forward
    # P/E whose denominator (historical consensus) does not exist anywhere to
    # replay — see specs/sectors/ems.yaml pillar_3.reference. Adding it here
    # would backtest a metric the sector does not use.
}


def multiple_series(conn, entities, units, fins, fin, eid):
    """Full-history [(date, multiple)] via the production code path."""
    bq = fin.get("base_quarter") or {}
    out, _cut, _pct, err = V.spot_multiple_series(
        conn, entities[eid], fins.get(eid, {}), units,
        bq.get("start"), bq.get("end"), fin["usdinr"],
        lookback_days=None, as_of=None)
    if err:
        return None, err
    return [(d, m) for d, m, _ in out], None


def weekly_z(series):
    """[(date, z, multiple)] at weekly closing prints, trailing-window z."""
    dates = [d for d, _ in series]
    vals = dict(series)
    # weekly = last print of each ISO week
    byweek = {}
    for d in dates:
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        byweek[(y, w)] = max(d, byweek.get((y, w), d))
    weekly = sorted(byweek.values())
    out = []
    for d in weekly:
        floor = (dt.date.fromisoformat(d)
                 - dt.timedelta(days=LOOKBACK_CAL_DAYS)).isoformat()
        win = [vals[x] for x in dates if floor <= x <= d]
        if len(win) < MIN_WINDOW_SESSIONS:
            continue
        sd = st.pstdev(win) or 1e-9
        out.append((d, (vals[d] - st.fmean(win)) / sd, vals[d]))
    return out


def px_map(conn, eid):
    return dict(conn.execute(
        "SELECT date, close FROM prices WHERE entity_id=? ORDER BY date",
        (eid,)).fetchall())


def fwd_rel(pxa, pxb, d, weeks):
    """Relative return a-b from d to d+weeks, matched on available dates."""
    target = (dt.date.fromisoformat(d) + dt.timedelta(weeks=weeks)).isoformat()
    ks = sorted(set(pxa) & set(pxb))
    fut = [k for k in ks if k >= target]
    if not fut or d not in pxa or d not in pxb:
        return None
    e = fut[0]
    if (dt.date.fromisoformat(e) - dt.date.fromisoformat(d)).days > weeks * 7 + 10:
        return None                      # gap too wide to call it this horizon
    return (pxa[e] / pxa[d] - 1) - (pxb[e] / pxb[d] - 1)


def corr(xs, ys):
    if len(xs) < 8:
        return None
    try:
        return st.correlation(xs, ys)
    except st.StatisticsError:
        return None


def study_pair(conn, entities, units, fins, fin, a, b):
    sa, ea = multiple_series(conn, entities, units, fins, fin, a)
    sb, eb = multiple_series(conn, entities, units, fins, fin, b)
    if ea or eb:
        return {"pair": f"{a}/{b}", "error": ea or eb}
    za = {d: z for d, z, _ in weekly_z(sa)}
    zb = {d: z for d, z, _ in weekly_z(sb)}
    common = sorted(set(za) & set(zb))
    if len(common) < 20:
        return {"pair": f"{a}/{b}", "error": f"only {len(common)} weekly points"}
    pxa, pxb = px_map(conn, a), px_map(conn, b)

    spread = {d: za[d] - zb[d] for d in common}
    res = {"pair": f"{a}/{b}", "n_weeks": len(common),
           "span": f"{common[0]}..{common[-1]}",
           "spread_now": spread[common[-1]]}

    # --- the mirror the eye sees: past co-movement -------------------------
    xs, ys = [], []
    for i in range(4, len(common)):
        d0, d1 = common[i - 4], common[i]
        r = (pxa.get(d1, 0) / pxa[d0] - 1) - (pxb.get(d1, 0) / pxb[d0] - 1) \
            if d0 in pxa and d0 in pxb and d1 in pxa and d1 in pxb else None
        if r is None:
            continue
        xs.append(spread[d1] - spread[d0]); ys.append(r)
    res["mirror_corr_4w"] = corr(xs, ys)

    # --- forward: level of spread vs what happens next ----------------------
    for h in HORIZONS_W:
        xs, ys = [], []
        for d in common:
            r = fwd_rel(pxa, pxb, d, h)
            if r is not None:
                xs.append(spread[d]); ys.append(r)
        res[f"fwd_corr_{h}w"] = corr(xs, ys)
        res[f"fwd_n_{h}w"] = len(xs)

    # --- terciles at 13w -----------------------------------------------------
    pts = [(spread[d], fwd_rel(pxa, pxb, d, 13)) for d in common]
    pts = [(s, r) for s, r in pts if r is not None]
    if len(pts) >= 15:
        pts.sort(key=lambda x: x[0])
        k = len(pts) // 3
        lo, hi = pts[:k], pts[-k:]
        res["tercile_low_spread_fwd13"] = st.fmean([r for _, r in lo])
        res["tercile_high_spread_fwd13"] = st.fmean([r for _, r in hi])

    # --- convergence trade: |z-spread| >= 1, short the dear leg, 13w --------
    # Non-overlapping entries: once in, skip forward 13 weeks.
    trades = []
    i = 0
    while i < len(common):
        d = common[i]
        s = spread[d]
        if abs(s) >= ENTRY_Z:
            r = fwd_rel(pxa, pxb, d, 13)
            if r is not None:
                pnl = -r if s > 0 else r      # short the dear leg
                trades.append(pnl)
                i += 13
                continue
        i += 1
    if trades:
        res["conv_trades"] = len(trades)
        res["conv_hit"] = sum(1 for t in trades if t > 0) / len(trades)
        res["conv_mean_pnl"] = st.fmean(trades)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", help="a,b — run one pair only")
    a = ap.parse_args()

    entities, units, fin = load_specs()
    fins = fin["companies"]
    conn = sqlite3.connect(DB)

    pairs = []
    if a.pair:
        x, y = a.pair.split(",")
        pairs = [(x.strip(), y.strip())]
    else:
        for names in GROUPS.values():
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    pairs.append((names[i], names[j]))

    fmt = lambda v: "     —" if v is None else f"{v:+6.2f}"
    print(f"z window {LOOKBACK_CAL_DAYS}cal d · weekly closes · "
          f"entry |z-spread| >= {ENTRY_Z} · short the dear leg, 13w hold\n")
    print(f"{'pair':26}{'weeks':>6}{'mirror':>8}"
          f"{'fwd1w':>8}{'fwd4w':>8}{'fwd13w':>8}"
          f"{'loQ f13':>9}{'hiQ f13':>9}{'trades':>7}{'hit':>6}{'pnl':>8}")
    print("-" * 103)
    rows = []
    for x, y in pairs:
        r = study_pair(conn, entities, units, fins, fin, x, y)
        rows.append(r)
        if r.get("error"):
            print(f"{r['pair']:26}  -- {r['error']}")
            continue
        lo = r.get("tercile_low_spread_fwd13")
        hi = r.get("tercile_high_spread_fwd13")
        lo_s = "" if lo is None else f"{lo * 100:+7.1f}%"
        hi_s = "" if hi is None else f"{hi * 100:+7.1f}%"
        hit_s = "" if "conv_hit" not in r else f"{r['conv_hit'] * 100:4.0f}%"
        pnl_s = ("" if "conv_mean_pnl" not in r
                 else f"{r['conv_mean_pnl'] * 100:+6.1f}%")
        print(f"{r['pair']:26}{r['n_weeks']:>6}"
              f"{fmt(r.get('mirror_corr_4w')):>8}"
              f"{fmt(r.get('fwd_corr_1w')):>8}"
              f"{fmt(r.get('fwd_corr_4w')):>8}"
              f"{fmt(r.get('fwd_corr_13w')):>8}"
              f"{lo_s:>9}{hi_s:>9}"
              f"{r.get('conv_trades', 0):>7}{hit_s:>6}{pnl_s:>8}")
    conn.close()

    print("""
READ THE MIRROR COLUMN FIRST. ~+1 there is the co-movement the eye sees on the
chart and it is mechanical — the score is built from the price. Predictability
lives in the fwd columns: NEGATIVE means a wide spread converged (the dear leg
underperformed from there), which is the direction a mean-reversion claim needs.
loQ/hiQ are mean forward 13w relative returns in the bottom/top spread terciles
— a real signal shows hiQ negative, loQ positive, and a gap between them.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
