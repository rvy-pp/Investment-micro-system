"""Consensus forward EPS from Yahoo's earningsTrend, into `estimates`.

EMS (and later IT / autos) scores P3 on FORWARD P/E, and the denominator —
what the street expects the company to earn — is not derivable from any price
series. Yahoo's quoteSummary `earningsTrend` module carries the consensus:
current-FY and next-FY EPS estimates with analyst counts, plus the estimate's
own 7/30/60/90-days-ago values, which make revision momentum computable from
a single capture with no accumulated history.

Cross-validated against the digests before being trusted (2026-08-29):
PhillipCapital values PGEL at "35x FY28E EPS of Rs16" — Yahoo FY28E consensus
16.56; CLSA calls Dixon "52x FY28 PE demanding" at CMP ~14,500 — Yahoo FY28E
281.6 puts it at 52.0x. Two houses, two names, both land on the consensus.

WHY `estimates` AND NOT `prices`. A consensus EPS is not a price. Anything in
`prices` becomes a series `bridge._series_in_store()` counts as priceable and
shockable — the exact reason cement_pack.py refuses to load the DIPP volume
sheet. The `estimates` table has the right shape (entity/broker/period/metric,
quote NOT NULL) and was empty since the schema was written; this is its first
writer.

AUTH: quoteSummary returns 401 to a bare request. The crumb dance — hit
fc.yahoo.com for a cookie, then /v1/test/getcrumb — is stdlib-only and needs
no login, so this runs unattended (unlike Wind/M365, which are interactively
authenticated MCP servers).

Two rows per period are quarterly (0q/+1q) and carry nAnalysts=1 on every EMS
name — single-analyst placeholders, not consensus. Only 0y/+1y are loaded.

Usage:
    python packages/adapters/yahoo_estimates.py --fetch
    python packages/adapters/yahoo_estimates.py --load
    python packages/adapters/yahoo_estimates.py --selftest
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import json
import pathlib
import re
import sqlite3
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "adapters"))
from yahoo_prices import UA  # noqa: E402

DB = REPO / "data" / "ims.db"
STAGING = REPO / "data" / "staging" / "estimates"

# earningsTrend carries the forward consensus; defaultKeyStatistics carries
# trailingEps, so the panel can show the NORMAL (trailing) P/E beside the
# forward one (PM request 2026-08-30). Trailing is display-only — the EMS
# score stays on the forward blend, because trailing E is distorted exactly
# where it matters here (Dixon's carries a one-off, Amber's is a trough that
# reads 294x).
Q = ("https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
     "?modules=earningsTrend,defaultKeyStatistics&crumb={crumb}")

# entity_id -> Yahoo symbol. EMS plus IT — the P/E-scored / P/E-watched
# sectors. The EMS symbols are the same ones yahoo_prices.CANDIDATES resolved
# by name; keep the two in agreement when a name is added.
SYMS = {
    "dixon":           "DIXON.NS",
    "amber":           "AMBER.NS",
    "kaynes":          "KAYNES.NS",
    "pg_electroplast": "PGEL.NS",
    "syrma_sgs":       "SYRMA.NS",
    "avalon":          "AVALON.NS",
    # --- IT, added 2026-09-01 (PM: 1-yr fwd P/E first, scoring later). All
    # resolved via yahoo_search / name-verified on the chart meta — bare
    # "Infosys" search returns HCL INFOSYSTEMS first (the bare-HCL class), so
    # none of these were guessed. LTIMindtree joined a day late under LTM.NS:
    # the company RENAMED ITSELF "LTM LIMITED", which is why every search for
    # its old name returned nothing (PM supplied the ticker 2026-09-02;
    # closes cross-checked against BSE 540005.BO before adoption).
    "ltimindtree":     "LTM.NS",
    "infosys":         "INFY.NS",
    "tcs":             "TCS.NS",
    "hcl_tech":        "HCLTECH.NS",
    "wipro":           "WIPRO.NS",
    "tech_mahindra":   "TECHM.NS",
    "persistent":      "PERSISTENT.NS",
    "coforge":         "COFORGE.NS",
    "mphasis":         "MPHASIS.NS",
    "kpit":            "KPITTECH.NS",
    "tata_elxsi":      "TATAELXSI.NS",
    "ofss":            "OFSS.NS",
    "ltts":            "LTTS.NS",
}

# Only the two annual periods. Yahoo also returns 0q/+1q rows, but on every
# EMS name they carry numberOfAnalysts=1 — a single house's quarterly model,
# not a consensus — and loading them would let a one-analyst number wear the
# same label as a 25-analyst one.
PERIODS = ("0y", "+1y")

# epsTrend lag fields, stored as their own metric rows so revision momentum is
# a query, not a re-fetch. '90daysAgo' is the momentum window the scorer uses.
LAGS = ("7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo")


def _opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    return op


def _crumb(op) -> str:
    try:
        op.open("https://fc.yahoo.com", timeout=15)
    except Exception:
        pass  # 404 is expected; the Set-Cookie is what matters
    c = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb",
                timeout=15).read().decode().strip()
    if not c or len(c) > 32:
        raise ValueError(f"crumb dance failed: {c!r}")
    return c


def _raw(d: dict | None, key: str):
    v = (d or {}).get(key)
    return v.get("raw") if isinstance(v, dict) else v


def fy_label(end_date: str) -> str:
    """'2027-03-31' -> 'FY27'. Indian FY, labelled by its closing year.

    Refuses an end date that is not a March fiscal close rather than guessing:
    a company on a December year-end would silently get an off-by-one label.
    All six EMS names close in March; revisit if a non-March name is added.
    """
    m = re.fullmatch(r"(\d{4})-03-\d{2}", end_date or "")
    if not m:
        raise ValueError(f"not a March fiscal year end: {end_date!r}")
    return f"FY{int(m.group(1)) % 100:02d}"


def fetch(out_dir: pathlib.Path = STAGING) -> pathlib.Path:
    op = _opener()
    crumb = _crumb(op)
    today = dt.date.today().isoformat()
    doc: dict = {"captured": today, "source": "yahoo quoteSummary earningsTrend",
                 "entities": {}}
    for eid, sym in SYMS.items():
        try:
            blob = json.load(op.open(Q.format(sym=sym, crumb=crumb), timeout=20))
            res0 = blob["quoteSummary"]["result"][0]
            trend = res0.get("earningsTrend", {}).get("trend", [])
            trailing_eps = _raw(res0.get("defaultKeyStatistics") or {},
                                "trailingEps")
        except Exception as exc:
            print(f"  {eid:16} FETCH FAILED {type(exc).__name__}: {str(exc)[:60]}")
            doc["entities"][eid] = {"symbol": sym, "error": str(exc)[:200]}
            continue
        rows = []
        for t in trend:
            if t.get("period") not in PERIODS:
                continue
            et = t.get("epsTrend") or {}
            rev = t.get("epsRevisions") or {}
            est = t.get("earningsEstimate") or {}
            rows.append({
                "period":      t.get("period"),
                "end_date":    t.get("endDate"),
                "eps":         _raw(et, "current"),
                "lags":        {k: _raw(et, k) for k in LAGS},
                "n_analysts":  _raw(est, "numberOfAnalysts"),
                "up_30d":      _raw(rev, "upLast30days"),
                "down_30d":    _raw(rev, "downLast30days"),
                "growth":      _raw(t, "growth"),
            })
        doc["entities"][eid] = {"symbol": sym, "trend": rows,
                                "trailing_eps": trailing_eps}
        got = ", ".join(f"{r['period']}={r['eps']}(n={r['n_analysts']})"
                        for r in rows)
        print(f"  {eid:16} {got}, ttm={trailing_eps}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"yahoo_estimates_{today}.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"staged -> {path}")
    return path


def latest_staging() -> pathlib.Path | None:
    files = sorted(STAGING.glob("yahoo_estimates_????-??-??.json"))
    return files[-1] if files else None


def load(path: pathlib.Path | None = None) -> int:
    path = path or latest_staging()
    if path is None:
        print("nothing staged — run --fetch first")
        return 0
    doc = json.loads(path.read_text(encoding="utf-8"))
    # Capture date from the FILENAME, not today: loading an old staging file
    # must not stamp its rows with the load date (the cement-pack rule — the
    # date a value was knowable, not the date it was processed).
    m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", path.name)
    if not m:
        raise ValueError(f"staging filename carries no date: {path.name}")
    as_of = m.group(1)
    if as_of > dt.date.today().isoformat():
        raise ValueError(f"refusing future capture date {as_of}")

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    sid = f"yahoo-est-{as_of}"
    conn.execute(
        "INSERT OR IGNORE INTO sources (id,kind,origin,title,source_date,"
        "captured_at,raw_path) VALUES (?,?,?,?,?,?,?)",
        (sid, "consensus", "yahoo",
         "Yahoo earningsTrend consensus EPS", as_of, now,
         str(path.relative_to(REPO))))
    # idempotent per capture: same file reloaded replaces its own rows
    conn.execute("DELETE FROM estimates WHERE source_id=?", (sid,))

    n = 0
    for eid, ent in (doc.get("entities") or {}).items():
        # Trailing EPS under its own period label so it can never be mistaken
        # for a fiscal-year consensus row (compute_row selects periods by the
        # FY prefix, so 'TTM' is invisible to the forward blend by shape).
        if ent.get("trailing_eps") is not None:
            conn.execute(
                "INSERT INTO estimates (source_id,entity_id,broker,as_of,"
                "period,metric,value_num,unit,quote,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, eid, "consensus_yahoo", as_of, "TTM", "eps_ttm",
                 float(ent["trailing_eps"]), "INR/sh",
                 f"Yahoo trailingEps {ent['trailing_eps']} "
                 f"(reported TTM, captured {as_of})", now))
            n += 1
        for r in ent.get("trend") or []:
            if r.get("eps") is None:
                continue
            try:
                fy = fy_label(r.get("end_date"))
            except ValueError as exc:
                print(f"  {eid:16} SKIPPED: {exc}")
                continue
            quote = (f"Yahoo consensus {r['end_date']}: EPS {r['eps']}, "
                     f"{r.get('n_analysts') or '?'} analysts, revisions 30d "
                     f"+{r.get('up_30d') or 0}/-{r.get('down_30d') or 0} "
                     f"(captured {as_of})")
            rows = [("eps", r["eps"])]
            for k in LAGS:
                v = (r.get("lags") or {}).get(k)
                if v is not None:
                    rows.append((f"eps_{k.replace('daysAgo', 'd_ago')}", v))
            if r.get("n_analysts") is not None:
                rows.append(("n_analysts", r["n_analysts"]))
            for metric, val in rows:
                conn.execute(
                    "INSERT INTO estimates (source_id,entity_id,broker,as_of,"
                    "period,metric,value_num,unit,quote,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, eid, "consensus_yahoo", as_of, fy, metric,
                     float(val), "INR/sh" if metric != "n_analysts" else "count",
                     quote, now))
                n += 1
    conn.commit()
    conn.close()
    print(f"loaded {n} estimate rows from {path.name} as source {sid}")
    return n


def selftest() -> int:
    ok = True
    # fy_label accepts a March close and refuses everything else — a guard
    # needs an acceptance test, not only a rejection test (the GLOB lesson).
    assert fy_label("2027-03-31") == "FY27"
    assert fy_label("2028-03-31") == "FY28"
    for bad in ("2027-12-31", "", None, "2027-03"):
        try:
            fy_label(bad)  # type: ignore[arg-type]
            print(f"FAIL: fy_label accepted {bad!r}")
            ok = False
        except (ValueError, TypeError):
            pass
    # a canned trend row survives the load-shaping
    t = {"period": "0y", "endDate": "2027-03-31",
         "epsTrend": {"current": {"raw": 183.56}, "90daysAgo": {"raw": 166.27}},
         "epsRevisions": {"upLast30days": {"raw": 18}},
         "earningsEstimate": {"numberOfAnalysts": {"raw": 25}}}
    assert _raw(t["epsTrend"], "current") == 183.56
    assert _raw(t["epsTrend"], "90daysAgo") == 166.27
    print("selftest OK" if ok else "selftest FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--file", type=pathlib.Path, default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.fetch:
        fetch()
    if a.load:
        load(a.file)
    if not (a.fetch or a.load):
        print(__doc__)
