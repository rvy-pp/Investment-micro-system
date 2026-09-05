"""F2 sector flows, the data layer: full-universe F&O OI + cash deliveries.

Three NSE static-file feeds, all answering plain urllib (probed 2026-09-04),
none needing the cookie-guarded site API:

  FO bhavcopy   nsearchives.../BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip
                UDiFF format. Stock futures = FinInstrmTp 'STF'. ~210
                underlyings, ~630 contract rows/day across expiries.
  delivery bhav nsearchives.../sec_bhavdata_full_DDMMYYYY.csv
                cash-market DELIV_QTY / DELIV_PER per stock. SERIES EQ only.
  sector map    fo_mktlots.csv (the F&O roster) x the Nifty 500 list's
                Industry column (20 NSE macro sectors). F&O names outside
                the N500 land as 'unmapped', never guessed.

UNITS, verified before a single row was stored: OpnIntrst is UNDERLYING
SHARES, TtlTradgVol is CONTRACTS, TtlTrfVal is RUPEES (ABCAPITAL lot value
~12.3L reconciles TtlTrfVal/TtlTradgVol against the ~3,100-share lot).
Cross-stock sums are therefore done in RUPEES downstream, never in shares.

A missing date is a holiday or a not-yet-published day: both fetchers treat
404 as "no session", never as an error. The newest available day is T-1
(NSE publishes after the close) — same T-1-by-design as the vault OI path.

No raw zips are kept: the NSE archive host is itself the dated, immutable,
re-fetchable record, and 250 days x ~2MB would bloat data/ for no
provenance gain.

Usage:
    python packages/adapters/fo_bhavcopy.py --map           # (re)build sector map
    python packages/adapters/fo_bhavcopy.py --load          # top up to T-1
    python packages/adapters/fo_bhavcopy.py --load --days 365   # backfill
    python packages/adapters/fo_bhavcopy.py --status
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import pathlib
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0 Safari/537.36")}
FO_URL = ("https://nsearchives.nseindia.com/content/fo/"
          "BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
DELIV_URL = ("https://nsearchives.nseindia.com/products/content/"
             "sec_bhavdata_full_{d:%d%m%Y}.csv")
LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
N500_URL = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"

PAUSE = 0.6   # between archive requests on a backfill — be a polite client


def _get(url: str, timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None          # holiday / not published yet
        raise


# ------------------------------------------------------------------ parse ----

def parse_fo(raw: bytes) -> list[dict]:
    """One day's stock-future rows, aggregated per symbol across expiries."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    rows = csv.DictReader(io.TextIOWrapper(zf.open(zf.namelist()[0]),
                                           encoding="utf-8"))
    agg: dict[str, dict] = {}
    for r in rows:
        if r.get("FinInstrmTp") != "STF":
            continue
        sym = r["TckrSymb"]
        a = agg.setdefault(sym, {"oi": 0.0, "chg": 0.0, "vol": 0.0,
                                 "trf": 0.0, "n": 0, "best_vol": -1.0,
                                 "close": None})
        vol = float(r["TtlTradgVol"] or 0)
        a["oi"] += float(r["OpnIntrst"] or 0)
        a["chg"] += float(r["ChngInOpnIntrst"] or 0)
        a["vol"] += vol
        a["trf"] += float(r["TtlTrfVal"] or 0)
        a["n"] += 1
        # close = the FRONT contract's, identified by max volume — first-row
        # would quote a back month (the GIFT-Nifty lesson, same host family).
        if vol > a["best_vol"]:
            a["best_vol"] = vol
            a["close"] = float(r["ClsPric"] or 0)
    return [{"symbol": s, **a} for s, a in agg.items() if a["close"]]


def parse_deliv(raw: bytes, keep: set[str]) -> list[dict]:
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")))
    out = []
    for r in rows:
        r = {k.strip(): (v or "").strip() for k, v in r.items()}
        if r.get("SERIES") != "EQ" or r.get("SYMBOL") not in keep:
            continue
        try:
            out.append({
                "symbol": r["SYMBOL"],
                "ttl": float(r["TTL_TRD_QNTY"]),
                "dq": float(r["DELIV_QTY"]),
                "dp": float(r["DELIV_PER"]),
                "to": float(r["TURNOVER_LACS"]),
                "close": float(r["CLOSE_PRICE"]),
            })
        except (ValueError, KeyError):
            continue   # '-' rows (no delivery data) are absent, not zero
    return out


# ------------------------------------------------------------------- load ----

# Coverage names OUTSIDE the F&O roster still need delivery rows (the PM's
# coverage-limited deliveries view, 2026-09-05): Dalmia, LTTS, Hindustan
# Copper, Jindal Stainless etc. are listed but not in current F&O. Their
# tickers come from yahoo_prices.CANDIDATES (the .NS entries — the repo's
# existing id->ticker source of truth) plus the IT watch names.
IT_TICKERS = {"INFY", "TCS", "HCLTECH", "WIPRO", "TECHM", "LTIM", "COFORGE",
              "MPHASIS", "PERSISTENT", "OFSS", "TATAELXSI", "KPITTECH", "LTTS"}


def coverage_symbols() -> set[str]:
    syms = set(IT_TICKERS)
    try:
        from yahoo_prices import CANDIDATES
        for cands in CANDIDATES.values():
            for c in cands:
                s = c[0] if isinstance(c, tuple) else c
                if isinstance(s, str) and s.endswith(".NS"):
                    syms.add(s[:-3])
                    break
    except Exception:
        pass
    return syms


def universe(conn: sqlite3.Connection) -> set[str]:
    return ({r[0] for r in conn.execute("SELECT symbol FROM fo_sector_map")}
            | coverage_symbols())


def load(days_back: int = 10, refill_deliv: bool = False) -> None:
    conn = sqlite3.connect(DB)
    keep = universe(conn)
    if not keep:
        print("sector map is empty - run --map first")
        return
    have_fo = {r[0] for r in conn.execute("SELECT DISTINCT date FROM fo_oi")}
    # refill: re-fetch delivery files for already-loaded dates too — needed
    # when the keep-set GROWS (coverage names added 2026-09-05); INSERT OR
    # REPLACE makes it idempotent for the rows that already exist.
    have_dv = set() if refill_deliv else {
        r[0] for r in conn.execute("SELECT DISTINCT date FROM deliveries")}
    today = dt.date.today()
    n_fo = n_dv = n_skip = 0
    for k in range(days_back, 0, -1):
        d = today - dt.timedelta(days=k)
        if d.weekday() > 4:
            continue
        iso = d.isoformat()
        if iso not in have_fo:
            # one slow NSE response must not kill a 250-day walk — a
            # TimeoutError here ended the first refill halfway (2026-09-05)
            try:
                raw = _get(FO_URL.format(d=d))
            except Exception as e:
                print(f"  {iso} FO fetch failed ({type(e).__name__}), skipped")
                raw = None
            time.sleep(PAUSE)
            if raw is None:
                n_skip += 1
            else:
                for a in parse_fo(raw):
                    conn.execute(
                        "INSERT OR REPLACE INTO fo_oi (symbol, date, oi_shares,"
                        " oi_chg, vol_contracts, turnover, close, n_expiries)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (a["symbol"], iso, a["oi"], a["chg"], a["vol"],
                         a["trf"], a["close"], a["n"]))
                conn.commit()
                n_fo += 1
        if iso not in have_dv:
            try:
                raw = _get(DELIV_URL.format(d=d))
            except Exception as e:
                print(f"  {iso} deliv fetch failed ({type(e).__name__}), skipped")
                raw = None
            time.sleep(PAUSE)
            if raw is not None:
                for a in parse_deliv(raw, keep):
                    conn.execute(
                        "INSERT OR REPLACE INTO deliveries (symbol, date,"
                        " ttl_qty, deliv_qty, deliv_per, turnover_lacs, close)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (a["symbol"], iso, a["ttl"], a["dq"], a["dp"],
                         a["to"], a["close"]))
                conn.commit()
                n_dv += 1
    conn.close()
    print(f"loaded {n_fo} FO days, {n_dv} delivery days "
          f"({n_skip} dates had no session/file)")


def build_map() -> None:
    raw = _get(LOTS_URL)
    fo_syms = set()
    for line in raw.decode("utf-8", "replace").splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[1]:
            continue
        # Index futures and repeated header lines ride in the same file —
        # 'NIFTY' in the UNDERLYING catches every index (FINNIFTY included),
        # and 'symbol' catches the mid-file header repeats.
        if "NIFTY" in parts[0].upper() or parts[1].lower() == "symbol":
            continue
        fo_syms.add(parts[1])
    n500 = {}
    raw = _get(N500_URL)
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))):
        n500[r["Symbol"].strip()] = r["Industry"].strip()

    conn = sqlite3.connect(DB)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    n_mapped = n_un = 0
    for s in sorted(fo_syms):
        sec = n500.get(s)
        conn.execute(
            "INSERT INTO fo_sector_map (symbol, sector, source, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT (symbol) DO UPDATE SET "
            "sector=excluded.sector, source=excluded.source, "
            "updated_at=excluded.updated_at "
            # a hand-set row must survive a rebuild
            "WHERE fo_sector_map.source != 'manual'",
            (s, sec or "unmapped", "nifty500" if sec else "unmapped", now))
        if sec:
            n_mapped += 1
        else:
            n_un += 1
    conn.commit()
    un = [r[0] for r in conn.execute(
        "SELECT symbol FROM fo_sector_map WHERE sector='unmapped'")]
    conn.close()
    print(f"F&O universe {len(fo_syms)} names: {n_mapped} mapped via Nifty 500, "
          f"{n_un} unmapped{': ' + ', '.join(un) if un else ''}")


def status() -> None:
    conn = sqlite3.connect(DB)
    for t in ("fo_oi", "deliveries"):
        r = conn.execute(f"SELECT COUNT(DISTINCT date), MIN(date), MAX(date),"
                         f" COUNT(DISTINCT symbol) FROM {t}").fetchone()
        print(f"  {t:12s} {r[0]:4d} days  {r[1]} .. {r[2]}  {r[3]} symbols")
    n = conn.execute("SELECT COUNT(*), SUM(sector='unmapped') "
                     "FROM fo_sector_map").fetchone()
    print(f"  sector map   {n[0]} symbols ({n[1]} unmapped)")
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--refill-deliv", action="store_true",
                    help="re-fetch delivery files for already-loaded dates "
                         "(after the keep-set grows)")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.map:
        build_map()
    if a.load:
        load(a.days, refill_deliv=a.refill_deliv)
    if a.status or not (a.map or a.load):
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
