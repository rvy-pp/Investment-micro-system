"""L0 adapter — daily closes from Yahoo, into the `prices` table.

Dependency-free (stdlib urllib only), so it runs anywhere without a pip install.

TWO CLASSES OF SERIES, and the difference is architectural:

  MACHINE-FETCHABLE  equities, FX, exchange-traded futures. Complete series,
                     no citation needed, no model involved -> `prices` table.

  NOT PUBLICLY QUOTED  alumina index, Coal India e-auction premium, CP coke.
                     These are assessed prices behind paywalls. They reach the
                     system as CITED OBSERVATIONS extracted from broker research
                     -> `observations` table, with a verbatim quote.

That split is why the schema has both tables. Do not fabricate a proxy for the
second class: a wrong alumina series silently rescales every aluminium signal.

A BUG THIS DELIBERATELY AVOIDS: `meta.chartPreviousClose` is the close before
the START OF THE REQUESTED RANGE, not the prior session. Using it with
range=5d reports five-day moves as daily ones — plausible-looking numbers,
wrong by an order of magnitude, feeding a materiality threshold. This pairs
closes with their timestamps and takes the last two.

Usage:
    python packages/adapters/yahoo_prices.py --probe
    python packages/adapters/yahoo_prices.py --load --range 3mo
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
# Yahoo wants a browser UA; without one it returns 401/429.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# entity_id -> candidate Yahoo symbols, best first. Probed, not assumed.
CANDIDATES: dict[str, list[str]] = {
    # --- equities (the book) ---
    "hindalco":       ["HINDALCO.NS"],
    "nalco":          ["NATIONALUM.NS"],
    "hindustan_zinc": ["HINDZINC.NS"],
    "vedanta":        ["VEDL.NS"],
    "vaml":           ["VEDANTAALUMINIUM.NS", "VEDALUM.NS", "VDLALUM.NS"],
    # --- fx ---
    "usdinr":         ["USDINR=X", "INR=X"],
    # --- exchange-traded metals ---
    "lme_aluminium":  ["ALI=F"],     # CME Aluminum, USD/t. A PROXY for LME:
                                     # tracks it with a Midwest premium basis.
    "silver":         ["SI=F"],
    # NO ZINC CANDIDATE. Probed and rejected, see REJECTED below — this is a
    # real coverage gap for the zinc peer group, not an oversight.
    "lme_zinc":             [],
    # --- assessed prices, no public feed ---
    "alumina_index":        [],
    "thermal_coal_eauction": [],
    "cp_coke":              [],
    "can_sheet_spread":     [],
    "al_scrap_midwest":     [],
}

# Symbols probed and DELIBERATELY rejected. Kept so nobody re-adds them.
REJECTED = {
    "ZNC=F": "instrumentType ALTSYMBOL, name 'ZNC Future JUL 2019' — a dead 2019 "
             "contract. Returned a frozen 3950.00 with only 5 distinct closes in 23 "
             "sessions. Plausible level, no information.",
    "ZN=F":  "IS THE 10-YEAR T-NOTE FUTURE, not zinc. Would have fed bond prices "
             "into a zinc margin bridge at ~108.",
    "XAGUSD=X": "404.",
}

# Series that must arrive as cited observations from research, not a feed.
RESEARCH_SOURCED = {
    "lme_zinc": "LME zinc — no live free feed found; PRIMARY driver of the zinc "
                "peer group, so this gap blocks that group's daily run",
    "alumina_index": "Alumina FOB Australia — assessed price (Platts/Fastmarkets)",
    "thermal_coal_eauction": "Coal India e-auction premium — no public daily series",
    "cp_coke": "Calcined petroleum coke — assessed, contract-driven",
    "can_sheet_spread": "Can sheet conversion spread — Novelis disclosure / broker estimate",
    "al_scrap_midwest": "US Midwest scrap discount — assessed",
}

# --- validation thresholds -------------------------------------------------
MAX_STALE_DAYS = 5          # a live series prints at least weekly
MIN_DISTINCT_RATIO = 0.5    # a live series moves; a dead contract repeats
OK_INSTRUMENT_TYPES = {"FUTURE", "CURRENCY", "EQUITY", "INDEX", "ETF"}


def fetch(symbol: str, rng: str = "3mo") -> list[tuple[str, float]]:
    """Return [(iso_date, close)] ascending. Raises if the series fails validation.

    Validation is not optional. A symbol returning a plausible NUMBER is not the
    same as the right series: ZNC=F returned a perfectly reasonable 3950.00 that
    was a frozen 2019 contract, and ZN=F returns T-note prices under a
    zinc-looking ticker. Both would have silently rescaled a whole peer group.
    """
    req = urllib.request.Request(CHART.format(sym=symbol, rng=rng),
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        doc = json.load(resp)

    result = (doc.get("chart") or {}).get("result")
    if not result:
        raise ValueError("no result block")
    r = result[0]
    meta = r.get("meta") or {}
    stamps = r.get("timestamp") or []
    closes = (r["indicators"]["quote"][0] or {}).get("close") or []

    out = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        d = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
        out.append((d, float(close)))
    if not out:
        raise ValueError("no closes")

    itype = (meta.get("instrumentType") or "?").upper()
    if itype not in OK_INSTRUMENT_TYPES:
        raise ValueError(f"instrumentType {itype} (name={meta.get('shortName')!r}) "
                         f"— not a tradeable series")

    stale = (dt.date.today() - dt.date.fromisoformat(out[-1][0])).days
    if stale > MAX_STALE_DAYS:
        raise ValueError(f"stale: last print {out[-1][0]} ({stale}d ago)")

    ratio = len(set(round(c, 6) for _, c in out)) / len(out)
    if ratio < MIN_DISTINCT_RATIO:
        raise ValueError(f"frozen: only {ratio:.0%} distinct closes "
                         f"— dead or illiquid contract")

    return out


def probe(rng: str = "1mo") -> dict[str, dict]:
    findings: dict[str, dict] = {}
    for eid, syms in CANDIDATES.items():
        if not syms:
            findings[eid] = {"status": "no_candidate"}
            continue
        for sym in syms:
            try:
                series = fetch(sym, rng)
                last_d, last_c = series[-1]
                chg = None
                if len(series) >= 2:
                    chg = (last_c / series[-2][1] - 1.0) * 100.0
                findings[eid] = {"status": "ok", "symbol": sym, "n": len(series),
                                 "last_date": last_d, "last": last_c, "chg_pct": chg}
                break
            except (urllib.error.URLError, urllib.error.HTTPError,
                    ValueError, KeyError, TimeoutError) as exc:
                findings[eid] = {"status": "fail", "symbol": sym,
                                 "error": f"{type(exc).__name__}: {exc}"}
    return findings


def load(rng: str = "3mo") -> int:
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    n_rows = 0
    for eid, syms in CANDIDATES.items():
        for sym in syms:
            try:
                series = fetch(sym, rng)
            except Exception:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO entities (id,kind,name,is_tradeable,active) "
                "VALUES (?,?,?,?,1)",
                (eid, _kind_of(eid), eid, 1 if eid in ("hindalco", "nalco",
                 "hindustan_zinc", "vedanta", "vaml") else 1),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO prices (entity_id,date,close) VALUES (?,?,?)",
                [(eid, d, c) for d, c in series],
            )
            n_rows += len(series)
            break
    conn.commit()
    conn.close()
    return n_rows


def _kind_of(eid: str) -> str:
    if eid in ("hindalco", "nalco", "hindustan_zinc", "vedanta", "vaml"):
        return "company"
    if eid == "usdinr":
        return "fx"
    return "commodity"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--range", default="3mo")
    a = ap.parse_args()

    if a.probe:
        f = probe()
        print(f"{'entity':24} {'status':10} {'symbol':22} {'n':>4} {'last':>12} {'chg%':>7}")
        print("-" * 84)
        for eid, r in f.items():
            if r["status"] == "ok":
                chg = f"{r['chg_pct']:+.2f}" if r["chg_pct"] is not None else "-"
                print(f"{eid:24} {'OK':10} {r['symbol']:22} {r['n']:>4} "
                      f"{r['last']:>12,.2f} {chg:>7}")
            elif r["status"] == "no_candidate":
                print(f"{eid:24} {'NO FEED':10} {'-':22}")
            else:
                print(f"{eid:24} {'FAIL':10} {r.get('symbol','-'):22}  {r['error'][:34]}")

        gaps = [e for e, r in f.items() if r["status"] != "ok"]
        if gaps:
            print("\nSERIES WITH NO PUBLIC FEED — must arrive as cited observations")
            print("from broker research, NOT as a fabricated proxy:")
            for g in gaps:
                print(f"  {g:24} {RESEARCH_SOURCED.get(g, 'no candidate symbol found')}")
        return 0

    if a.load:
        n = load(a.range)
        print(f"loaded {n} price rows into {DB}")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
