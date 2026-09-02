"""IT coverage — 1-year-forward P/E cross-section (the pre-scoring look).

PM instruction 2026-09-01: for IT, start from 1-yr forward P/E rather than
scoring — graph the cross-section and see whether relative valuations make
sense large-vs-large, mid-vs-mid. This script is the numbers half; it decides
nothing and persists nothing.

The forward multiple is the same construct Bloomberg's BEst 1yr-fwd P/E uses:
close / time-weighted FY1/FY2 consensus blend. There is no BBG feed on this
machine; the denominator is the Yahoo earningsTrend consensus captured daily
by adapters/yahoo_estimates.py (cross-validated against broker-cited
multiples for EMS on 2026-08-29 and for IT on 2026-09-01 — see the digest
citations in the commit message that added IT to SYMS).

Closes are fetched LIVE from the Yahoo chart API, not read from `prices` —
the IT names are deliberately absent from `prices` (OI-only sector, no specs,
nothing bridge-shockable). Pairing closes with their own timestamps and
taking the last two is the macro-fetch lesson: `chartPreviousClose` is the
close before the RANGE, not the prior session.

Sub-sector split follows the vault coverage convention:
    large  TCS, Infosys, HCLTech, Wipro, Tech Mahindra   (+LTIM, see below)
    mid    Persistent, Coforge, Mphasis, OFSS
    er&d   KPIT, Tata Elxsi, LTTS

LTIMindtree rode a rename: the company is now "LTM LIMITED" and Yahoo carries
only the new symbol (LTM.NS) — the old-name searches that came back empty on
2026-09-01 were the rename, not a missing listing. Closed 2026-09-02.

Usage:
    python packages/review/it_forward_pe.py            # table
    python packages/review/it_forward_pe.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "adapters"))
sys.path.insert(0, str(REPO / "packages" / "score"))
from yahoo_estimates import SYMS  # noqa: E402
from yahoo_prices import UA  # noqa: E402
from valuation_pe import _blend, _fy_end, MIN_ANALYSTS  # noqa: E402

DB = REPO / "data" / "ims.db"

GROUPS = {
    "IT Large Cap": ["tcs", "infosys", "hcl_tech", "wipro", "tech_mahindra",
                     "ltimindtree"],
    "IT Mid Cap":   ["persistent", "coforge", "mphasis", "ofss"],
    "IT ER&D":      ["kpit", "tata_elxsi", "ltts"],
}
# The gap closed 2026-09-02: LTIMindtree RENAMED ITSELF "LTM LIMITED", and
# Yahoo carries the new symbol (LTM.NS, 37-analyst consensus) while every
# search for the old name returns nothing. Kept as an empty dict rather than
# deleted so the render loop's gap path stays exercised-able.
NO_CONSENSUS: dict[str, str] = {}

CHART = ("https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
         "?range=5d&interval=1d")


def live_close(sym: str) -> tuple[str, float]:
    """(date, close) of the last completed session — last non-null close,
    paired with its own timestamp (never chartPreviousClose)."""
    req = urllib.request.Request(CHART.format(sym=sym),
                                 headers={"User-Agent": UA})
    doc = json.load(urllib.request.urlopen(req, timeout=20))
    res = doc["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
    if not pairs:
        raise ValueError(f"{sym}: no closes in 5d window")
    t, c = pairs[-1]
    return dt.datetime.fromtimestamp(t).date().isoformat(), float(c)


def consensus(conn: sqlite3.Connection, eid: str) -> dict | None:
    cap = conn.execute(
        "SELECT MAX(as_of) FROM estimates WHERE entity_id=? AND "
        "broker='consensus_yahoo'", (eid,)).fetchone()[0]
    if not cap:
        return None
    out: dict = {"as_of": cap}
    for period, metric, val in conn.execute(
            "SELECT period, metric, value_num FROM estimates WHERE entity_id=?"
            " AND broker='consensus_yahoo' AND as_of=?", (eid, cap)):
        out.setdefault(period, {})[metric] = val
    return out


def compute(conn: sqlite3.Connection, eid: str) -> dict:
    est = consensus(conn, eid)
    if not est:
        return {"eid": eid, "error": "no consensus capture"}
    px_date, close = live_close(SYMS[eid])
    d0 = dt.date.fromisoformat(px_date)
    fys = sorted((k for k in est if k.startswith("FY")), key=_fy_end)
    live = [p for p in fys if _fy_end(p) >= d0]
    if len(live) < 2:
        return {"eid": eid, "error": f"need two open FYs, have {live}"}
    fy1, fy2 = live[0], live[1]
    e1, e2 = est[fy1].get("eps"), est[fy2].get("eps")
    n1 = int(est[fy1].get("n_analysts") or 0)
    if e1 is None or e2 is None or e1 <= 0:
        return {"eid": eid, "error": f"unusable consensus {fy1}={e1} {fy2}={e2}"}
    eps_12mf, w = _blend(d0, _fy_end(fy1), e1, e2)
    g = e2 / e1 - 1.0
    row = {
        "eid": eid, "symbol": SYMS[eid], "px_date": px_date,
        "close": round(close, 2), "capture": est["as_of"],
        "fy1": fy1, "fy2": fy2, "eps_fy1": round(e1, 2),
        "eps_fy2": round(e2, 2), "n_analysts": n1, "w_fy1": round(w, 3),
        "eps_12mf": round(eps_12mf, 2), "fwd_pe": round(close / eps_12mf, 1),
        "growth": round(g, 4),
        "peg": round(close / eps_12mf / (g * 100), 2) if g > 0 else None,
        "thin": n1 < MIN_ANALYSTS,
    }
    e_ttm = (est.get("TTM") or {}).get("eps_ttm")
    if e_ttm and e_ttm > 0:
        row["ttm_pe"] = round(close / e_ttm, 1)
    l1 = est[fy1].get("eps_90d_ago")
    l2 = est[fy2].get("eps_90d_ago")
    if l1 and l2:
        old = w * l1 + (1 - w) * l2
        if old > 0:
            row["rev_90d"] = round(eps_12mf / old - 1.0, 4)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DB)
    out: dict = {"generated": dt.date.today().isoformat(), "groups": {}}
    for gname, eids in GROUPS.items():
        out["groups"][gname] = [compute(conn, e) for e in eids]
    out["gaps"] = NO_CONSENSUS
    conn.close()

    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    for gname, rows in out["groups"].items():
        print(f"\n{gname}")
        print(f"{'name':16}{'close':>10}{'EPS 12mf':>10}{'fwdPE':>7}"
              f"{'ttmPE':>7}{'growth':>8}{'PEG':>6}{'rev90d':>8}{'n':>4}")
        print("-" * 76)
        for r in rows:
            if "error" in r:
                print(f"{r['eid']:16}  -- {r['error']}")
                continue
            rev = f"{r['rev_90d']:+.1%}" if r.get("rev_90d") is not None else "—"
            peg = f"{r['peg']:.2f}" if r.get("peg") is not None else "—"
            flag = " THIN" if r["thin"] else ""
            print(f"{r['eid']:16}{r['close']:>10,.0f}{r['eps_12mf']:>10.2f}"
                  f"{r['fwd_pe']:>7.1f}{r.get('ttm_pe', 0):>7.1f}"
                  f"{r['growth']:>8.1%}{peg:>6}{rev:>8}{r['n_analysts']:>4}"
                  f"{flag}")
    for eid, why in NO_CONSENSUS.items():
        print(f"\nGAP: {eid} — {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
