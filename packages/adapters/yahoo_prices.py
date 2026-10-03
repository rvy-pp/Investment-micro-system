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
# The same endpoint addressed by an explicit epoch window instead of a range
# token. It exists because of the trap documented on fetch_bars: `range=max`
# SILENTLY CHANGES THE INTERVAL, and no range token reaches past 20 years.
CHART_SPAN = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
              "?period1={p1}&period2={p2}&interval=1d")
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
    # --- equities: ems, added 2026-08-30 ---
    # All six resolved before being written here (the four F&O names verified
    # against meta.longName on the chart API, Syrma/Avalon via yahoo_search.py).
    # The name patterns matter twice in this set: "Amber" alone is a common
    # word (Amber Road, amber alerts) so the pattern requires "enterprises",
    # and "Avalon" alone matches AVALON HOLDINGS (AWX) and the aircraft lessor
    # the digests themselves flag — the pattern requires "technologies".
    "dixon":            [("DIXON.NS", r"dixon\s*tech")],
    "amber":            [("AMBER.NS", r"amber\s*enterprises")],
    "kaynes":           [("KAYNES.NS", r"kaynes\s*tech")],
    "pg_electroplast":  [("PGEL.NS", r"pg\s*electroplast")],
    "syrma_sgs":        [("SYRMA.NS", r"syrma\s*sgs")],
    "avalon":           [("AVALON.NS", r"avalon\s*tech")],
    # --- equities: IT, added 2026-09-01 (PM: forward P/E tab, no scoring) ---
    # All name-verified before being written here, and the search paid AGAIN:
    # a bare "Infosys" search returns HCL INFOSYSTEMS LTD first, whose
    # lowercase name CONTAINS "infosys" — the pattern requires "infosys ltd/
    # limited" precisely to reject "infosystems". LTIMindtree is the odd one:
    # THE COMPANY RENAMED ITSELF "LTM LIMITED" and Yahoo carries only the new
    # symbol — LTIM.NS / LTIMINDTREE.NS / LTI.NS all 404, and a Yahoo search
    # for "LTIMindtree" or "Mindtree" returns NOTHING (found 2026-09-02, PM
    # supplied the ticker). LTM.NS was verified against the BSE code
    # 540005.BO before adoption: same closes to a few rupees on every
    # overlapping day (28th 4,675 vs 4,680; 31st 4,540 vs 4,542.5). The BSE
    # code stays as the fallback; the entity id stays `ltimindtree`.
    "infosys":          [("INFY.NS", r"infosys\s+(limited|ltd)")],
    "tcs":              [("TCS.NS", r"tata\s*consultancy")],
    "hcl_tech":         [("HCLTECH.NS", r"hcl\s*tech")],
    "wipro":            [("WIPRO.NS", r"wipro")],
    "tech_mahindra":    [("TECHM.NS", r"tech\s*mahindra")],
    "ltimindtree":      [("LTM.NS", r"\bltm\b|ltimindtree"),
                         ("540005.BO", r"ltimindtree|\bltm\b")],
    "persistent":       [("PERSISTENT.NS", r"persistent\s*systems")],
    "coforge":          [("COFORGE.NS", r"coforge")],
    "mphasis":          [("MPHASIS.NS", r"mphasis")],
    "kpit":             [("KPITTECH.NS", r"kpit")],
    "tata_elxsi":       [("TATAELXSI.NS", r"tata\s*elxsi")],
    "ofss":             [("OFSS.NS", r"oracle\s*fin")],
    "ltts":             [("LTTS.NS", r"l\s*&\s*t\s*tech")],
    # --- equities: auto, added 2026-09-19 (PM: Ather + an Auto tab) ---
    # Resolved with yahoo_search.py before being written here, per the VAML
    # lesson. The search returns THREE rows for "Ather Energy" and only one
    # is the ordinary line: ATHERENERG-BL.NS is the BL (trade-for-trade /
    # block) series on the same company, a thin parallel listing whose closes
    # are NOT the ones the tape quotes. The pattern cannot reject it (same
    # longName), so the symbol is pinned explicitly and the -BL series is
    # recorded in REJECTED below.
    #
    # NOTE THE DATE FLOOR: Ather IPO'd 2025-05-06. There is no price before
    # that and `range` tokens longer than the listing return only what exists,
    # so a "5y" ask is not an error here — it is simply 1.4 years. Anything
    # that treats a short history as a fetch failure is wrong about this name.
    "ather":            [("ATHERENERG.NS", r"ather\s*energy")],
    # Eicher added 2026-09-20 with its dossier. Name-verified first, and the
    # pattern requires "eicher" precisely because the company the market means
    # is the HOLDCO (Royal Enfield + the 54.4% VECV JV), not "Eicher
    # Engineering" or any of the group's unlisted arms.
    #
    # UNLIKE ather, THIS IS A LONG HISTORY AND IT HAS A SPLIT IN IT: 1:10 on
    # 2020-08-25 (face value Rs10 -> Re1). Yahoo adjusts splits (it does not
    # adjust demergers - see the VEDL note above), and the series was scanned
    # for >35% single-day moves after loading to confirm the adjustment took.
    "eicher":           [("EICHERMOT.NS", r"eicher\s*motors")],
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
    "dixon", "amber", "kaynes", "pg_electroplast", "syrma_sgs", "avalon",
    "infosys", "tcs", "hcl_tech", "wipro", "tech_mahindra", "ltimindtree",
    "persistent", "coforge", "mphasis", "kpit", "tata_elxsi", "ofss", "ltts",
    "ather", "eicher",
}

# Symbols probed and DELIBERATELY rejected. Kept so nobody re-adds them.
REJECTED = {
    "ATHERENERG-BL.NS":
        "THE SAME COMPANY ON THE BL (block/trade-for-trade) SERIES. Yahoo "
        "returns it beside ATHERENERG.NS for the same longName, so the name "
        "pattern cannot separate them — the symbol is pinned instead. Its "
        "closes are a thin parallel book, not the tape the PM quotes.",
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


def fetch_bars(symbol: str, rng: str = "3mo",
               name_pattern: str | None = None
               ) -> list[tuple[str, float, dict]]:
    """Return [(iso_date, close, bars)] ascending, bars = open/high/low/volume.

    Raises if the series fails validation. Validation is not optional. A symbol
    returning a plausible NUMBER is not the same as the right series: ZNC=F
    returned a perfectly reasonable 3950.00 that was a frozen 2019 contract, and
    ZN=F returns T-note prices under a zinc-looking ticker. Both would have
    silently rescaled a whole peer group.

    THE CLOSE IS THE ONLY REQUIRED FIELD, and that asymmetry is deliberate.
    The endpoint has always returned the full quote block; `fetch()` simply
    dropped four fifths of it, which is why every one of the 201,267 rows in
    `prices` carried a NULL open until 2026-09-11. A bar with a missing leg
    still yields a usable close, so the close is kept and the bar is dropped —
    never the reverse, and never a bar back-filled from a neighbouring field.
    """
    # `rng` accepts an ISO DATE as well as a Yahoo range token. A date goes
    # through the explicit-epoch URL, which is the only way to reach past 20
    # years and the only way to get daily bars for a deep window at all.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", rng):
        p1 = int(dt.datetime.fromisoformat(rng + "T00:00:00+00:00").timestamp())
        p2 = int(dt.datetime.now(dt.timezone.utc).timestamp())
        url = CHART_SPAN.format(sym=symbol, p1=p1, p2=p2)
    else:
        url = CHART.format(sym=symbol, rng=rng)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        doc = json.load(resp)

    result = (doc.get("chart") or {}).get("result")
    if not result:
        raise ValueError("no result block")
    r = result[0]
    meta = r.get("meta") or {}

    # GRANULARITY IS NOT WHAT WE ASKED FOR, AND YAHOO DOES NOT SAY SO.
    # `interval=1d` is a REQUEST, not a contract. Measured 2026-09-17 with
    # interval=1d on every call:
    #     range=max, TATASTEEL.NS -> dataGranularity '1mo', 370 month-END bars
    #                                spanning 1996..2026
    #     range=max, VAML.NS      -> dataGranularity '1h',  481 hourly bars
    #                                collapsing to 69 distinct dates
    # Both parse perfectly and both would have been stored as DAILY rows: 370
    # monthly candles written as sessions, or a date carrying five bars of
    # which the last one silently wins. Nothing downstream could see it —
    # the closes are real closes, just of the wrong period. That is the
    # silent-arithmetic shape (docs/SILENT_BUGS.md), and it is the same trap
    # as the chartPreviousClose/range=5d note at the top of this file: the
    # numbers are right and the PERIOD is wrong.
    #
    # So the interval is verified, not assumed. Use an explicit ISO date for
    # deep history (period1/period2 holds '1d' out to 31 years); range tokens
    # hold '1d' up to and including '20y' and break at 'max'.
    gran = str(meta.get("dataGranularity") or "")
    if gran and gran != "1d":
        raise ValueError(
            f"granularity {gran!r}, not '1d' — Yahoo overrode interval=1d for "
            f"range={rng!r}. Pass an ISO date (period1/period2) instead; "
            f"'max' returns monthly for long histories and hourly for short ones")

    stamps = r.get("timestamp") or []
    q = (r["indicators"]["quote"][0] or {})
    closes = q.get("close") or []
    opens, highs, lows = q.get("open") or [], q.get("high") or [], q.get("low") or []
    vols = q.get("volume") or []

    def _at(seq, i):
        return seq[i] if i < len(seq) and seq[i] is not None else None

    out = []
    for i, (ts, close) in enumerate(zip(stamps, closes)):
        if close is None:
            continue
        d = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
        out.append((d, float(close), {"open": _at(opens, i), "high": _at(highs, i),
                                      "low": _at(lows, i), "volume": _at(vols, i)}))
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

    ratio = len(set(round(c, 6) for _, c, _b in out)) / len(out)
    if ratio < MIN_DISTINCT_RATIO:
        raise ValueError(f"frozen: only {ratio:.0%} distinct closes "
                         f"— dead or illiquid contract")

    return out


def fetch(symbol: str, rng: str = "3mo",
          name_pattern: str | None = None) -> list[tuple[str, float]]:
    """Return [(iso_date, close)] ascending — the close-only view of fetch_bars.

    Kept as the published signature because flow_series.py and
    morning_markets.py consume the 2-tuple. It DELEGATES rather than
    re-implementing: the identity/stale/frozen guards above are the ones that
    stop a T-note being loaded as zinc, and a second copy of them would be a
    second thing to keep in step.
    """
    return [(d, c) for d, c, _bars in fetch_bars(symbol, rng, name_pattern)]


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


def load(rng: str = "3mo", only: set[str] | None = None) -> int:
    """Fetch and store CANDIDATES. `only` restricts to those entity ids.

    `only` exists for the deep backfill of OHLC bars (the Book tab's index
    needs history the daily 3mo window does not reach). Pointing a long range
    at the WHOLE candidate list would also rewrite two years of commodity
    closes as a side effect of wanting equity bars — so the subset is named
    explicitly rather than the range just being widened.

    `rng` may be a Yahoo range token or an ISO DATE. Use the date for anything
    deep: see the granularity note in fetch_bars — `max` is not daily.
    """
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    n_rows = 0
    for eid, syms in CANDIDATES.items():
        if only is not None and eid not in only:
            continue
        for sym, pat in syms:
            try:
                series = fetch_bars(sym, rng, pat)
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
            #
            # Bars ride along from 2026-09-11 (the Book tab's basket candles).
            # They are refused wherever the close is refused — prices_io never
            # lets one row carry two sources' numbers.
            res = prices_io.upsert(
                conn, [(eid, d, c, b) for d, c, b in series], "yahoo")
            if res["refused"]:
                print(f"   {eid}: {res['refused']} rows kept from a higher-ranked "
                      f"source, {res['wrote']} written")
            if res.get("bad_bars"):
                print(f"   {eid}: {res['bad_bars']} incoherent bar(s) dropped, "
                      f"closes kept")
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
    ap.add_argument("--range", default="3mo",
                    help="a Yahoo range token, or an ISO DATE for deep history "
                         "(period1/period2 — the only way past 20y, and the "
                         "only way to get daily bars at all: see fetch_bars on "
                         "why 'max' is not daily)")
    ap.add_argument("--only", default=None,
                    help="comma-separated entity ids. REQUIRED in practice for "
                         "a deep --range: pointing a long range at the whole "
                         "candidate list rewrites years of commodity closes as "
                         "a side effect of wanting equity bars. This is how a "
                         "newly entered book leg gets the history the index "
                         "needs — without it the leg 'lists' on whatever date "
                         "the daily 3mo window happens to start, and the "
                         "back-cast shows it joining there.")
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
        only = ({e.strip() for e in a.only.split(",") if e.strip()}
                if a.only else None)
        if only:
            unknown = sorted(only - set(CANDIDATES))
            if unknown:
                print(f"not in CANDIDATES (no symbol, so no prices and no "
                      f"place in any index): {', '.join(unknown)}")
                return 2
        n = load(a.range, only=only)
        print(f"loaded {n} price rows into {DB}"
              + (f" for {', '.join(sorted(only))}" if only else ""))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
