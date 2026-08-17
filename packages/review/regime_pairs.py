"""Monthly aluminium REGIME model, ported from the vault's Portfolio Management work.

Source: `Portfolio Management/Aluminium/Aluminium Monthly Tracker & Pair Rankings.md`
and `aluminium_pair_analysis_v2.py`. This is the PM's own prior design and it is
structurally different from the pillar scores in four ways that matter -- each one
being a reason the daily-score backtest found nothing:

  MONTHLY, NOT DAILY     signal is a month-on-month change in a commodity level.
                         The pillar scores are daily with a 30-day EWMA, which
                         buries a monthly turn in noise.
  SIGN PAIR, NOT SIZE    the regime is the pair of SIGNS (alu, alumina), not a
                         magnitude. R2 and R3 are the divergent regimes and they
                         are 38% of months -- exactly where an up/down or a
                         single blended score cannot see the alumina dimension.
  RANK BY ARCHETYPE      each regime maps to a fixed ordering of the three names,
                         from cost structure, not from a computed number.
  LONG HORIZONS          the edge sits at 60-150 days. The daily backtest topped
                         out at 20 and would not have seen it.

WHY FRED MONTHLY AND NOT THE YAHOO FUTURES. The vault reads PALUMUSDM and the
alumina PPI WPU10230101. Both are monthly published levels, so neither carries a
front-month roll. `alumina_index` (ALA=F) in this repo has a -21.3% roll on
2025-02-03 that is not a price move; using it here would manufacture a regime
flip. Do not substitute it.

SIZING IS 2:1 AND THAT IS LOAD-BEARING. The vault's summary records "alpha
collapses at equal weight". Alpha is capital-normalised:

    alpha = (w_long * r_long - w_short * r_short) / (w_long + w_short)

Usage:
    python packages/review/regime_pairs.py
    python packages/review/regime_pairs.py --sizing 1:1
"""

from __future__ import annotations

import argparse
import csv
import io
import pathlib
import sqlite3
import statistics
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd=2023-07-01&coed=2026-12-31"
# FRED kills the connection on a browser User-Agent (ECONNRESET). Yahoo requires
# the opposite. Do not unify these.
FRED_UA = "curl/8.0"

ALU, ALUMINA = "PALUMUSDM", "WPU10230101"
BLEND = (0.60, 0.40)

# Regime -> ranking, best first. From the vault's cost-structure argument:
#   R1 both up      NALCO double tailwind (captive alumina + sells surplus)
#   R2 alu up, alumina down   smelter margin expands; VAML buys alumina -> best
#   R3 alu down, alumina up   VAML double-squeezed; NALCO sells alumina -> best
#   R4 both down    HNDL most insulated (Novelis conversion spread)
RANKS = {
    "R1": ["nalco", "vaml", "hindalco"],
    "R2": ["vaml", "nalco", "hindalco"],
    "R3": ["nalco", "hindalco", "vaml"],
    "R4": ["hindalco", "vaml", "nalco"],
}
# The vault's regime-conditional rotation (90d): the pair to actually trade.
ROTATION = {"R1": ("nalco", "hindalco"), "R2": ("vaml", "hindalco"),
            "R3": ("nalco", "vaml"), "R4": ("hindalco", "nalco")}

HORIZONS = [10, 30, 60, 90, 135, 150]
# VAML listed 2026-06-15; before that the vault used VEDL as the aluminium proxy,
# since pre-demerger VEDL contained the aluminium business.
VAML_PROXY_BEFORE = ("vaml", "vedanta", "2026-06-15")


def fred(sid: str) -> dict[str, float]:
    req = urllib.request.Request(FRED.format(sid=sid), headers={"User-Agent": FRED_UA})
    txt = urllib.request.urlopen(req, timeout=30).read().decode()
    out = {}
    for row in list(csv.reader(io.StringIO(txt)))[1:]:
        if len(row) >= 2 and row[1] not in (".", ""):
            out[row[0][:7]] = float(row[1])
    return out


def regimes() -> list[dict]:
    alu, alm = fred(ALU), fred(ALUMINA)
    months = sorted(set(alu) & set(alm))
    out = []
    for prev, cur in zip(months, months[1:]):
        da = alu[cur] / alu[prev] - 1.0
        dl = alm[cur] / alm[prev] - 1.0
        r = "R1" if (da > 0 and dl > 0) else "R2" if (da > 0 and dl <= 0) \
            else "R3" if (da <= 0 and dl > 0) else "R4"
        out.append({"month": cur, "alu_mom": da * 100, "alumina_mom": dl * 100,
                    "blended_mom": (BLEND[0] * da + BLEND[1] * dl) * 100,
                    "regime": r, "rank": RANKS[r]})
    return out


def closes(conn):
    d = {}
    for e, dt_, c in conn.execute("SELECT entity_id,date,close FROM prices "
                                  "WHERE close IS NOT NULL"):
        d.setdefault(e, {})[dt_] = c
    return d


def resolve(eid: str, on: str) -> str:
    a, b, cut = VAML_PROXY_BEFORE
    return b if (eid == a and on < cut) else eid


def fwd(px, eid: str, entry: str, h: int) -> float | None:
    """Return over the next h TRADING days from the first session on/after entry."""
    e = resolve(eid, entry)
    days = sorted(px.get(e, {}))
    nxt = [d for d in days if d >= entry]
    if not nxt:
        return None
    i = days.index(nxt[0])
    if i + h >= len(days):
        return None
    a, b = px[e][days[i]], px[e][days[i + h]]
    # VEDL's tape is not comparable across its demerger.
    if e == "vedanta" and days[i] < "2026-04-30" <= days[i + h]:
        return None
    return b / a - 1.0 if a else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizing", default="2:1")
    a = ap.parse_args()
    wl, ws = (float(x) for x in a.sizing.split(":"))

    conn = sqlite3.connect(DB)
    px = closes(conn)
    conn.close()
    regs = regimes()

    print(f"regime months: {len(regs)}   {regs[0]['month']} .. {regs[-1]['month']}"
          f"   sizing {a.sizing}")
    dist = {r: sum(1 for x in regs if x["regime"] == r) for r in RANKS}
    print("distribution: " + "  ".join(
        f"{k} {v} ({v/len(regs)*100:.0f}%)" for k, v in dist.items()))

    print(f"\nREGIME-CONDITIONAL ROTATION — trade the vault's pair for each regime")
    print(f"{'h':>5}{'n':>5}{'avg a%':>9}{'win%':>7}{'sharpe':>8}{'best%':>8}{'worst%':>8}")
    for h in HORIZONS:
        al = []
        for r in regs:
            lng, srt = ROTATION[r["regime"]]
            entry = f"{r['month']}-01"
            rl, rs = fwd(px, lng, entry, h), fwd(px, srt, entry, h)
            if rl is None or rs is None:
                continue
            al.append((wl * rl - ws * rs) / (wl + ws) * 100)
        if len(al) < 3:
            continue
        sd = statistics.stdev(al)
        print(f"{h:>5}{len(al):>5}{statistics.fmean(al):>+9.2f}"
              f"{sum(1 for x in al if x>0)/len(al)*100:>7.0f}"
              f"{statistics.fmean(al)/sd if sd else 0:>8.3f}"
              f"{max(al):>+8.2f}{min(al):>+8.2f}")

    print(f"\nBY REGIME @ 90d — the vault's headline is R3 / nalco-vaml")
    print(f"{'regime':8}{'pair':22}{'n':>4}{'win%':>7}{'avg a%':>9}")
    for rg in ("R1", "R2", "R3", "R4"):
        lng, srt = ROTATION[rg]
        al = []
        for r in [x for x in regs if x["regime"] == rg]:
            entry = f"{r['month']}-01"
            rl, rs = fwd(px, lng, entry, 90), fwd(px, srt, entry, 90)
            if rl is not None and rs is not None:
                al.append((wl * rl - ws * rs) / (wl + ws) * 100)
        if not al:
            print(f"{rg:8}{lng+'/'+srt:22}{0:>4}")
            continue
        print(f"{rg:8}{lng+'/'+srt:22}{len(al):>4}"
              f"{sum(1 for x in al if x>0)/len(al)*100:>7.0f}"
              f"{statistics.fmean(al):>+9.2f}")

    print(f"\nlatest 6 months")
    print(f"{'month':9}{'alu%':>8}{'alumina%':>10}{'blend%':>9}{'reg':>5}  ranking")
    for r in regs[-6:]:
        print(f"{r['month']:9}{r['alu_mom']:>+8.2f}{r['alumina_mom']:>+10.2f}"
              f"{r['blended_mom']:>+9.2f}{r['regime']:>5}  {' > '.join(r['rank'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
