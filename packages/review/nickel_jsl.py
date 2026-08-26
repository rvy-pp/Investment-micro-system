"""Does Jindal Stainless trade on LME nickel? A lead-lag correlation test.

WHY THIS EXISTS. specs/entities/steel.yaml withholds JSL's economics on a claim:
nickel is a 1-2 month LAGGED PASS-THROUGH via the surcharge mechanism, partly
hedged by captive Indonesian NPI, so a nickel COST line would carry the wrong
sign. That claim came from the vault's Fact Base — a document, not a measurement.
This tests it against prices, which is the only thing that can contradict it.

WHAT WOULD CONFIRM THE PASS-THROUGH STORY
    Contemporaneous correlation near zero or POSITIVE. If nickel is passed
    through, a nickel rally is not a margin hit, so the equity should not fall on
    it. A positive contemporaneous reading is consistent with the surcharge
    mechanism plus an inventory gain.

WHAT WOULD REFUTE IT
    A clear NEGATIVE contemporaneous correlation — nickel up, JSL down — which is
    what a genuine unhedged cost exposure looks like. That would mean the spec is
    withholding for the wrong reason and a cost line is defensible after all.

WHAT WOULD LOCATE THE LAG
    A peak at nickel LEADING by 4-8 weeks. That is the specific shape the 1-2
    month surcharge lag predicts, and it is the reason this is a lead-lag scan
    rather than a single correlation.

THE CONTROL IS THE POINT, NOT AN EXTRA. Every Indian metal name correlates with
every base metal to some degree — they share a global risk factor, the rupee, and
the same domestic industrial cycle. So "JSL correlates 0.25 with nickel" means
nothing on its own. The test is whether JSL correlates with nickel MORE than
tata_steel and jsw_steel do, since neither of those consumes nickel in any
quantity. Without that comparison this measures metals beta and calls it a
stainless finding.

An aluminium control runs alongside for the same reason in the other direction:
if JSL tracks lme_aluminium about as well as it tracks lme_nickel, then neither is
a specific exposure.

METHOD, and the choices are the project's own:

  WEEKLY, LAST PRINT OF THE WEEK. packages/review/weekly.py settled this: "take
  the week's CLOSING print, not its average. Averaging lost on every row — a
  weekly mean is centred on Wednesday, adding lag to a signal already built from
  a difference of levels." Daily returns are mostly microstructure noise; monthly
  leaves too few points to resolve a 4-8 week lag.

  LOG RETURNS, not levels. Two trending series correlate on the trend and say
  nothing about co-movement. Level correlation is reported separately and
  explicitly labelled as the thing not to read.

  JSL'S HISTORY IS SPLIT AT 2015-11-19. Its close went 56.35 -> 22.05, -60.9%, at
  the JSL/JSHL restructuring. That is an unconfirmed corporate action and is NOT
  in core/corporate_actions.CONFIRMED_ACTIONS, so nothing else truncates there —
  but a return series that crosses it contains one -61% week that is not a return.
  The default window therefore starts 2016-01-01. `--full` runs the whole history
  for comparison and the difference is reported rather than hidden.

Usage:
    python packages/review/nickel_jsl.py
    python packages/review/nickel_jsl.py --from 2021-01-01
    python packages/review/nickel_jsl.py --full
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import sqlite3

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

JSL_ACTION = "2015-11-19"   # -60.9%, JSL/JSHL restructuring, unconfirmed
DEFAULT_FROM = "2016-01-01"

EQUITIES = ["jindal_stainless", "tata_steel", "jsw_steel"]
DRIVERS = ["lme_nickel", "lme_aluminium"]


def weekly_closes(conn, eid: str, start: str, end: str) -> dict:
    """{iso_monday: close} using the LAST print of each ISO week."""
    rows = conn.execute(
        "SELECT date, close FROM prices WHERE entity_id=? AND close IS NOT NULL "
        "AND date BETWEEN ? AND ? ORDER BY date", (eid, start, end)).fetchall()
    out: dict[str, float] = {}
    for d, c in rows:
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        out[f"{y}-W{w:02d}"] = c          # later date overwrites -> last of week
    return out


def log_returns(series: dict) -> dict:
    ks = sorted(series)
    out = {}
    for a, b in zip(ks, ks[1:]):
        if series[a] > 0 and series[b] > 0:
            out[b] = math.log(series[b] / series[a])
    return out


def pearson(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def aligned(dr: dict, eq: dict, lag: int):
    """Driver return at week t-lag against equity return at week t.

    lag > 0 means the DRIVER LEADS: nickel moves, the equity responds `lag` weeks
    later. That is the sign convention the surcharge story makes a prediction
    about, so getting it backwards would invert the whole conclusion.
    """
    ks = sorted(set(dr) & set(eq))
    idx = {k: i for i, k in enumerate(sorted(set(dr) | set(eq)))}
    order = sorted(set(dr) | set(eq))
    xs, ys = [], []
    for k in ks:
        i = idx[k] - lag
        if 0 <= i < len(order):
            dk = order[i]
            if dk in dr:
                xs.append(dr[dk])
                ys.append(eq[k])
    return xs, ys


def t_stat(r: float, n: int) -> float:
    if r is None or n < 3 or abs(r) >= 1:
        return 0.0
    return r * math.sqrt((n - 2) / (1 - r * r))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default=DEFAULT_FROM)
    ap.add_argument("--to", dest="end", default="2026-12-31")
    ap.add_argument("--full", action="store_true",
                    help="ignore the 2015 corporate-action cut")
    ap.add_argument("--max-lag", type=int, default=12)
    a = ap.parse_args()
    start = "2010-01-01" if a.full else a.start

    conn = sqlite3.connect(DB)
    wk = {e: log_returns(weekly_closes(conn, e, start, a.end))
          for e in EQUITIES + DRIVERS}

    print(f"LME NICKEL vs JINDAL STAINLESS — weekly log returns, "
          f"{start} .. {a.end}")
    if not a.full:
        print(f"  starting {DEFAULT_FROM}: JSL's close went 56.35 -> 22.05 "
              f"(-60.9%) on {JSL_ACTION}, the JSL/JSHL restructuring. A return "
              f"series crossing it carries one -61% week that is not a return.")
    n_jsl = len(wk["jindal_stainless"])
    print(f"  {n_jsl} weekly observations\n")

    # ---- levels, reported only to be dismissed --------------------------
    lv_n = weekly_closes(conn, "lme_nickel", start, a.end)
    lv_j = weekly_closes(conn, "jindal_stainless", start, a.end)
    ks = sorted(set(lv_n) & set(lv_j))
    r_lv = pearson([lv_n[k] for k in ks], [lv_j[k] for k in ks])
    print(f"LEVEL correlation: {r_lv:+.3f} over {len(ks)} weeks")
    print("  DO NOT READ THIS AS CO-MOVEMENT. Two trending series correlate on")
    print("  the trend. It is here so nobody computes it later and is misled.\n")

    # ---- the lead-lag scan ----------------------------------------------
    print("CONTEMPORANEOUS AND LAGGED, weekly log returns.")
    print("  lag > 0 = the DRIVER LEADS by that many weeks.\n")
    hdr = f"{'lag':>4}"
    for e in EQUITIES:
        hdr += f"{e.replace('_',' ')[:14]:>16}"
    print(hdr + "     <- vs lme_nickel")
    print("-" * (4 + 16 * len(EQUITIES) + 22))
    best = {}
    for lag in range(-a.max_lag, a.max_lag + 1):
        line = f"{lag:>4}"
        for e in EQUITIES:
            xs, ys = aligned(wk["lme_nickel"], wk[e], lag)
            r = pearson(xs, ys)
            if r is None:
                line += f"{'—':>16}"
                continue
            line += f"{r:>+11.3f}({len(xs):>3})"
            if e not in best or abs(r) > abs(best[e][1]):
                best[e] = (lag, r, len(xs))
        mark = "   <- contemporaneous" if lag == 0 else ""
        print(line + mark)

    print("\nSTRONGEST |r| PER NAME vs lme_nickel")
    for e in EQUITIES:
        lag, r, n = best[e]
        print(f"  {e:18} r {r:+.3f} at lag {lag:+d}w  "
              f"(n={n}, |t|={abs(t_stat(r, n)):.2f})")

    # ---- the aluminium control -----------------------------------------
    print("\nALUMINIUM CONTROL — contemporaneous, same weeks.")
    print("  If JSL tracks aluminium about as well as nickel, neither is a")
    print("  SPECIFIC exposure and both are metals beta.")
    for e in EQUITIES:
        row = f"  {e:18}"
        for d in DRIVERS:
            xs, ys = aligned(wk[d], wk[e], 0)
            r = pearson(xs, ys)
            row += f"  {d:16} {r:+.3f}" if r is not None else f"  {d:16}    —"
        print(row)

    # ---- the discriminating number -------------------------------------
    xs, ys = aligned(wk["lme_nickel"], wk["jindal_stainless"], 0)
    r_jsl = pearson(xs, ys) or 0.0
    peers = []
    for e in ("tata_steel", "jsw_steel"):
        xs2, ys2 = aligned(wk["lme_nickel"], wk[e], 0)
        rr = pearson(xs2, ys2)
        if rr is not None:
            peers.append(rr)
    peer_avg = sum(peers) / len(peers) if peers else 0.0
    print(f"\nTHE DISCRIMINATING NUMBER")
    print(f"  JSL vs nickel, contemporaneous        {r_jsl:+.3f}")
    print(f"  carbon-steel peers vs nickel, average {peer_avg:+.3f}")
    print(f"  excess attributable to stainless      {r_jsl - peer_avg:+.3f}")
    print("  Neither Tata nor JSW consumes nickel in any quantity, so their")
    print("  correlation IS the shared-factor baseline. Only the excess can be")
    print("  a stainless-specific nickel exposure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
