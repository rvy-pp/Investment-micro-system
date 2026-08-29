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
import re
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))

import prices_io  # noqa: E402
DB = REPO / "data" / "ims.db"

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
# Yahoo wants a browser UA; without one it returns 401/429.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# entity_id -> [(symbol, name_must_match)], best first.
#
# THE NAME PATTERN IS THE REAL GUARD. Instrument type does not discriminate:
# ALA=F reports ALTSYMBOL and is a perfectly live alumina series, while ZN=F
# reports FUTURE and is the 10-Year T-Note. Only the name catches that — a
# series whose name does not mention the thing you think you are buying is the
# wrong series, whatever else it reports.
CANDIDATES: dict[str, list[tuple[str, str]]] = {
    # --- equities (the book) ---
    "hindalco":       [("HINDALCO.NS", r"hindalco")],
    "nalco":          [("NATIONALUM.NS", r"national|nalco")],
    "hindustan_zinc": [("HINDZINC.NS", r"hindustan\s*zinc")],
    "vedanta":        [("VEDL.NS", r"vedanta")],
    # Resolved with yahoo_search.py, not guessed. Three invented variants
    # (VEDANTAALUMINIUM.NS / VEDALUM.NS / VDLALUM.NS) all 404'd while the real
    # ticker was the obvious one. Use the search endpoint first, always.
    "vaml":           [("VAML.NS", r"vedanta\s*aluminium")],
    # --- equities: steel, added 2026-08-25 ---
    # All seven resolved with yahoo_search.py before being written here, per the
    # VAML lesson. Two are worth noting because a guess would have missed them:
    # Jindal Stainless is JSL.NS (not JINDALSTNLS), and Jindal Steel renamed from
    # Jindal Steel & Power — JINDALSTEL.NS still resolves and Yahoo now returns
    # the name "JINDAL STEEL LIMITED", so the pattern must not require "power".
    "tata_steel":       [("TATASTEEL.NS", r"tata\s*steel")],
    "jsw_steel":        [("JSWSTEEL.NS", r"jsw\s*steel")],
    "jindal_steel":     [("JINDALSTEL.NS", r"jindal\s*steel")],
    "sail":             [("SAIL.NS", r"steel\s*authority")],
    "jindal_stainless": [("JSL.NS", r"jindal\s*stainless")],
    "shyam_metalics":   [("SHYAMMETL.NS", r"shyam\s*metalics|shyam\s*metal")],
    "apl_apollo":       [("APLAPOLLO.NS", r"apl\s*apollo")],
    # --- equities: cement, added 2026-08-28 ---
    # All four resolved with yahoo_search.py first, per the VAML lesson — and
    # the search PAID twice on this set: "ambuja" alone returns GUJARAT AMBUJA
    # EXPORTS (GAEL) first, and "dalmia bharat" returns DALMIA BHARAT SUGAR
    # (DALMIASUG) above the cement company. Both are different listed
    # companies a guessed pattern could have accepted; the name regexes below
    # are written to reject them.
    "ultratech":  [("ULTRACEMCO.NS", r"ultratech\s*cement")],
    "ambuja":     [("AMBUJACEM.NS", r"ambuja\s*cement")],
    "shree":      [("SHREECEM.NS", r"shree\s*cement")],
    "dalmia":     [("DALBHARAT.NS", r"dalmia\s*bharat\s*(?!sug)")],
    # --- equities: mining, added 2026-08-29 ---
    # All four resolved with yahoo_search.py first, and the search paid AGAIN:
    # a bare "NMDC" query returns NMDC STEEL LIMITED (NSLNISP — the demerged
    # steel plant, a different listed company and the "NSL" whose receivables
    # sit on NMDC's own balance sheet) ABOVE the miner. The name pattern
    # requires "ltd" precisely to reject "NMDC STEEL LIMITED".
    "nmdc":             [("NMDC.NS", r"nmdc\s*ltd")],
    "coal_india":       [("COALINDIA.NS", r"coal\s*india")],
    "hindustan_copper": [("HINDCOPPER.NS", r"hindustan\s*copper")],
    "lloyds_metals":    [("LLOYDSME.NS", r"lloyds\s*metals")],
    # --- fx ---
    "usdinr":         [("USDINR=X", r"usd\s*/?\s*inr")],
    "usdcny":         [("CNY=X", r"usd\s*/?\s*cny")],
    # --- exchange-traded ---
    "alumina_index":  [("ALA=F", r"alumina")],      # Alumina FOB Australia (Platts).
                                                    # Platts-settled, not Fastmarkets MB,
                                                    # but both assess the same physical
                                                    # market and track closely.
    # lme_aluminium: REMOVED 2026-08-21. It was ("ALI=F", r"alumin") — CME
    # Aluminum, used as a proxy for LME. That is invariant 6 exactly: "a proxy is
    # never aliased to the thing it proxies", the rule this same file honours for
    # zinc (zinc_shfe, never lme_zinc). The gap is not cosmetic — ALI=F embeds a
    # Midwest premium and read 3,324.25 against real LME cash of 3,182.00 on
    # 2026-08-20, +142 USD/t or +4.5%, and across 161 overlapping dates the store
    # was a BIMODAL MIXTURE of the two: 70 dates within 30 USD/t of LME cash and
    # 65 beyond 80, depending on whether Yahoo or the pack wrote last.
    #
    # Real LME cash now comes from packages/adapters/westmetall.py, day-delayed.
    # Do not re-add ALI=F here. If a CME series is ever wanted it must be its own
    # entity_id (cme_aluminium_midwest), and per the note in metals_pack.py an
    # unused series should not be loaded at all.
    "lme_aluminium":  [],
    "midwest_premium": [("AUP=F", r"aluminum\s*mw|midwest")],   # USD/lb, not /t
    "silver":         [("SI=F", r"silver")],
    # NO FREE ZINC ON YAHOO — see REJECTED. Zinc now comes from Wind ZN.SHF via
    # packages/adapters/wind_zinc.py, loaded as `zinc_shfe` (ex-VAT, USD/t).
    # Deliberately NOT loaded as `lme_zinc`: it is a Chinese domestic contract
    # and must not be mistaken for the LME benchmark.
    "lme_zinc":             [],
    # --- assessed prices, no public feed ---
    "thermal_coal_eauction": [],
    "cp_coke":              [],
    "can_sheet_spread":     [],
    "al_scrap_midwest":     [],
}

# THE EQUITY ROSTER — one definition. It was written out twice as a literal
# tuple, in load() and in _kind_of(), and adding a name to CANDIDATES without
# editing BOTH inserted it into `entities` with kind='commodity'. That is the
# silent-arithmetic shape again: no error, a plausible-looking row, and
# _series_in_store() then counts an equity as a priceable input series.
EQUITIES = {
    "hindalco", "nalco", "hindustan_zinc", "vedanta", "vaml",
    "tata_steel", "jsw_steel", "jindal_steel", "sail",
    "jindal_stainless", "shyam_metalics", "apl_apollo",
    "ultratech", "ambuja", "shree", "dalmia",
    "nmdc", "coal_india", "hindustan_copper", "lloyds_metals",
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
# instrumentType is recorded but NOT used to reject: ALTSYMBOL covers both a
# dead 2019 contract (ZNC=F) and a live alumina series (ALA=F). Liveness and
# the name pattern do the discriminating.


def fetch(symbol: str, rng: str = "3mo",
          name_pattern: str | None = None) -> list[tuple[str, float]]:
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

    # IDENTITY: the series must be named like the thing we think we are buying.
    # This, not instrumentType, is what stops ZN=F (10-Year T-Note) being loaded
    # as zinc.
    name = str(meta.get("shortName") or meta.get("longName") or "")
    if name_pattern and not re.search(name_pattern, name, re.I):
        raise ValueError(f"name {name!r} does not match /{name_pattern}/ "
                         f"— wrong instrument")

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
        for sym, pat in syms:
            try:
                series = fetch(sym, rng, pat)
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
        for sym, pat in syms:
            try:
                series = fetch(sym, rng, pat)
            except Exception:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO entities (id,kind,name,is_tradeable,active) "
                "VALUES (?,?,?,?,1)",
                (eid, _kind_of(eid), eid, 1),
            )
            # Through prices_io, not INSERT OR REPLACE. Yahoo is the LOWEST-ranked
            # source, so it can fill a cell nobody owns but can never overwrite the
            # metals pack or westmetall. It used to silently win every race by
            # running last — it overwrote the pack's usdinr on 2026-08-15,
            # 95.4300 -> 95.6470.
            res = prices_io.upsert(conn, [(eid, d, c) for d, c in series], "yahoo")
            if res["refused"]:
                print(f"   {eid}: {res['refused']} rows kept from a higher-ranked "
                      f"source, {res['wrote']} written")
            n_rows += res["wrote"]
            break
    conn.commit()
    conn.close()
    return n_rows


def _kind_of(eid: str) -> str:
    if eid in EQUITIES:
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
