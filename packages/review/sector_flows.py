"""F2 methodology test: does OI travel between sectors, and who takes delivery.

READ-ONLY over fo_oi / deliveries / fo_sector_map. Prints the test the PM
specified 2026-09-04: sector-summed OI behaviour daily, OI migration between
sectors, volume the same way, then cash-delivery percentages by sector and
stock with spike detection.

THE TWO AGGREGATES ARE DIFFERENT MEASUREMENTS, kept separate on purpose:

  stock (positioning LEVEL)   sector oi_value = sum(oi_shares x close). Moves
                              with price even when nobody trades — a 2% rally
                              lifts it 2% with zero new positioning. Good for
                              SHARE-of-book questions, bad for flow questions.
  flow (NEW positioning)      sector oi_flow = sum(oi_chg x close) per day —
                              the rupee value of positions actually added or
                              removed that day. This is the "travel" measure.

EXPIRY IS THE STRUCTURAL ARTEFACT: on monthly-expiry days oi_flow is hugely
negative everywhere at once (contracts die, rolls re-open next month). Those
days are DETECTED (total flow below a robust threshold) and excluded from the
migration stats rather than modelled — an excluded known artefact beats a
fitted correction.

Usage:
    python packages/review/sector_flows.py             # the full report
    python packages/review/sector_flows.py --stock X   # one name's history
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import sqlite3
import statistics as st
import sys
from collections import defaultdict

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

CR = 1e7  # rupees per crore


def load():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    smap = {r["symbol"]: r["sector"] for r in
            conn.execute("SELECT symbol, sector FROM fo_sector_map")}
    fo = [dict(r) for r in conn.execute(
        "SELECT * FROM fo_oi ORDER BY date")]
    dv = [dict(r) for r in conn.execute(
        "SELECT * FROM deliveries ORDER BY date")]
    conn.close()
    return smap, fo, dv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock")
    ap.add_argument("--days", type=int, default=0,
                    help="restrict the report window (default: all)")
    args = ap.parse_args()

    smap, fo, dv = load()
    dates = sorted({r["date"] for r in fo})
    if args.days:
        dates = dates[-args.days:]
        keep = set(dates)
        fo = [r for r in fo if r["date"] in keep]
        dv = [r for r in dv if r["date"] in keep]
    if len(dates) < 30:
        print(f"only {len(dates)} days loaded - backfill first")
        return 1

    if args.stock:
        return stock_report(args.stock, fo, dv)

    # ---- sector aggregates per day -----------------------------------------
    oi_val = defaultdict(lambda: defaultdict(float))    # date -> sector -> cr
    oi_flow = defaultdict(lambda: defaultdict(float))
    vol_val = defaultdict(lambda: defaultdict(float))
    for r in fo:
        sec = smap.get(r["symbol"], "unmapped")
        oi_val[r["date"]][sec] += r["oi_shares"] * r["close"] / CR
        oi_flow[r["date"]][sec] += r["oi_chg"] * r["close"] / CR
        vol_val[r["date"]][sec] += r["turnover"] / CR

    sectors = sorted({s for d in oi_val.values() for s in d},
                     key=lambda s: -oi_val[dates[-1]].get(s, 0))

    # ---- expiry detection ---------------------------------------------------
    tot_flow = {d: sum(oi_flow[d].values()) for d in dates}
    med = st.median(tot_flow.values())
    mad = st.median([abs(v - med) for v in tot_flow.values()]) or 1.0
    expiry = {d for d in dates if (tot_flow[d] - med) / (1.4826 * mad) < -4}
    clean = [d for d in dates if d not in expiry]

    print(f"{len(dates)} sessions {dates[0]} .. {dates[-1]}, "
          f"{len(smap)} names, {len(sectors)} sectors; "
          f"{len(expiry)} expiry-roll days excluded from flow stats\n")

    # ---- 1. the sector book: level + trend ----------------------------------
    print(f"== SECTOR POSITIONING (futures OI in Rs cr, {dates[-1]}) ==")
    tot_now = sum(oi_val[dates[-1]].values())
    d20 = dates[-21] if len(dates) > 21 else dates[0]
    tot_20 = sum(oi_val[d20].values()) or 1
    print(f"{'sector':34s} {'oi cr':>10s} {'share':>7s} {'share 20d ago':>14s} "
          f"{'d-share':>8s} {'vol share':>10s}")
    for s in sectors:
        now = oi_val[dates[-1]].get(s, 0)
        sh = 100 * now / tot_now
        sh20 = 100 * oi_val[d20].get(s, 0) / tot_20
        vsh = 100 * vol_val[dates[-1]].get(s, 0) / (sum(vol_val[dates[-1]].values()) or 1)
        print(f"{s:34s} {now:10,.0f} {sh:6.2f}% {sh20:13.2f}% "
              f"{sh - sh20:+7.2f}pp {vsh:9.2f}%")

    # ---- 2. OI travel: which sectors trade positioning against each other ---
    # Daily net-new positioning per sector, expiry days excluded, as a share of
    # that day's gross flow. "Travel" = a persistent negative correlation
    # between two sectors' daily flows: money leaving one as it enters the other.
    flows = {s: [] for s in sectors}
    for d in clean[1:]:
        gross = sum(abs(v) for v in oi_flow[d].values()) or 1
        for s in sectors:
            flows[s].append(oi_flow[d].get(s, 0) / gross)

    print("\n== OI TRAVEL: strongest sector-pair anticorrelations of daily "
          "positioning flow ==")
    pairs = []
    big = [s for s in sectors
           if 100 * oi_val[dates[-1]].get(s, 0) / tot_now > 2][:12]
    for i in range(len(big)):
        for j in range(i + 1, len(big)):
            a, b = flows[big[i]], flows[big[j]]
            ma, mb = st.mean(a), st.mean(b)
            sa, sb = st.pstdev(a), st.pstdev(b)
            if not sa or not sb:
                continue
            r = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a) / (sa * sb)
            pairs.append((r, big[i], big[j]))
    pairs.sort()
    for r, a, b in pairs[:6]:
        print(f"  {r:+.2f}  {a}  <->  {b}")
    print("  (near 0 = flows are independent; strongly negative = genuine "
          "rotation channel)")

    # ---- 3. recent flow: last 5 clean sessions ------------------------------
    print("\n== NET NEW POSITIONING, last 5 non-expiry sessions (Rs cr) ==")
    last5 = clean[-5:]
    rows = [(s, sum(oi_flow[d].get(s, 0) for d in last5)) for s in sectors]
    for s, v in sorted(rows, key=lambda x: -x[1]):
        if abs(v) < 50:
            continue
        print(f"  {s:34s} {v:+10,.0f}")

    # ---- 4. deliveries: sector level ----------------------------------------
    # Value-weighted delivery %, so one illiquid name cannot set a sector's tone.
    dsec = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))  # date->sec->[dv, tv]
    for r in dv:
        sec = smap.get(r["symbol"], "unmapped")
        cell = dsec[r["date"]][sec]
        cell[0] += r["deliv_qty"] * r["close"]
        cell[1] += r["ttl_qty"] * r["close"]
    ddates = sorted(dsec)

    def sec_dp(d, s):
        dvv, tvv = dsec[d][s]
        return 100 * dvv / tvv if tvv else None

    print("\n== DELIVERY %, value-weighted (5d avg vs own history) ==")
    print(f"{'sector':34s} {'5d avg':>8s} {'1y avg':>8s} {'z':>6s}")
    zrows = []
    for s in sectors:
        hist = [sec_dp(d, s) for d in ddates]
        hist = [x for x in hist if x is not None]
        if len(hist) < 60:
            continue
        cur = st.mean(hist[-5:])
        mu, sd = st.mean(hist[:-5]), st.pstdev(hist[:-5])
        zz = (cur - mu) / sd if sd else 0
        zrows.append((zz, s, cur, mu))
    for zz, s, cur, mu in sorted(zrows, key=lambda x: -x[0]):
        mark = " <-- elevated" if zz >= 2 else (" <-- depressed" if zz <= -2 else "")
        print(f"{s:34s} {cur:7.1f}% {mu:7.1f}% {zz:+6.1f}{mark}")

    # ---- 5. stock-level delivery spikes -------------------------------------
    # Someone taking delivery = HIGH delivery VALUE vs the stock's own year,
    # not high percentage alone (a dead stock's 90% of nothing is nothing).
    print("\n== STOCKS: delivery-value spikes, last 5 sessions vs own year ==")
    by_sym = defaultdict(list)
    for r in dv:
        by_sym[r["symbol"]].append(r)
    hits = []
    for sym, rows_ in by_sym.items():
        if len(rows_) < 120:
            continue
        vals = [r["deliv_qty"] * r["close"] / CR for r in rows_]
        base = vals[:-5]
        mu, sd = st.mean(base), st.pstdev(base)
        if not sd:
            continue
        cur = st.mean(vals[-5:])
        zz = (cur - mu) / sd
        dp5 = st.mean([r["deliv_per"] for r in rows_[-5:]])
        dp1y = st.mean([r["deliv_per"] for r in rows_[:-5]])
        if zz >= 2.5:
            hits.append((zz, sym, smap.get(sym, "?"), cur, mu, dp5, dp1y))
    hits.sort(reverse=True)
    print(f"{'symbol':14s} {'sector':30s} {'z':>5s} {'5d dv cr/day':>13s} "
          f"{'1y avg':>8s} {'dp 5d':>7s} {'dp 1y':>7s}")
    for zz, sym, sec, cur, mu, dp5, dp1y in hits[:20]:
        print(f"{sym:14s} {sec:30s} {zz:5.1f} {cur:13,.1f} {mu:8,.1f} "
              f"{dp5:6.1f}% {dp1y:6.1f}%")
    if not hits:
        print("  none at z >= 2.5")
    return 0


def stock_report(sym: str, fo, dv) -> int:
    sym = sym.upper()
    f = [r for r in fo if r["symbol"] == sym]
    d = [r for r in dv if r["symbol"] == sym]
    if not f:
        print(f"{sym}: not in fo_oi")
        return 1
    print(f"{sym}: {len(f)} FO days, {len(d)} delivery days\n")
    print("last 15 sessions:")
    dd = {r["date"]: r for r in d}
    for r in f[-15:]:
        dr = dd.get(r["date"])
        oi_cr = r["oi_shares"] * r["close"] / CR
        fl_cr = r["oi_chg"] * r["close"] / CR
        print(f"  {r['date']}  close {r['close']:9,.1f}  oi {oi_cr:8,.0f}cr "
              f"({fl_cr:+7,.0f})  " +
              (f"deliv {dr['deliv_per']:5.1f}% "
               f"({dr['deliv_qty'] * dr['close'] / CR:6,.1f}cr)" if dr else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
