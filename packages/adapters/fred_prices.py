"""L0 adapter — commodity series from FRED (IMF / World Bank pass-through).

Covers what Yahoo cannot: coal has no usable free futures series (CME MTF=F
resolves but returns no closes; every other coal code 404s), while FRED carries
the IMF's monthly assessed coal prices.

TWO GOTCHAS, both load-bearing:

1. FRED KILLS THE CONNECTION ON A BROWSER USER-AGENT — `read ECONNRESET`,
   reproducible. A curl-style UA or none at all returns 200 from the identical
   URL. This is the OPPOSITE of Yahoo, which requires a browser UA. Do not
   unify these two.

2. THESE SERIES ARE MONTHLY, NOT DAILY, and they are SEABORNE, NOT INDIAN
   DOMESTIC. An Indian smelter buys Coal India e-auction coal; the seaborne
   Australian price is a correlated substitute at the margin, not the same
   thing. Basis risk is real and is recorded on the series so any signal
   leaning on it can be flagged.

Usage:
    python packages/adapters/fred_prices.py --probe
    python packages/adapters/fred_prices.py --load
"""

from __future__ import annotations

import argparse
import csv
import io
import pathlib
import sqlite3
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
# NOT a browser UA — see gotcha 1.
FRED_UA = "curl/8.0"

# entity_id -> (fred_series_id, description, unit, basis_note)
SERIES = {
    "thermal_coal_seaborne": (
        "PCOALAUUSDM",
        "Coal, Australian thermal, IMF (monthly)",
        "USD/t",
        "SEABORNE Australian thermal. Indian smelters buy Coal India e-auction "
        "coal domestically; this is a correlated substitute at the margin, not "
        "the same price. Basis risk is real — flag signals that lean on it.",
    ),
    "thermal_coal_sa": (
        "PCOALSAUSDM",
        "Coal, South African thermal, IMF (monthly)",
        "USD/t",
        "Alternative seaborne benchmark; South African cargoes are a common "
        "Indian import source, so arguably a closer basis than Australian.",
    ),
    "coking_coal": (
        "PCOKEUSDM",
        "Coking coal, IMF (monthly)",
        "USD/t",
        "For the steel peer group when it is built; not used by aluminium.",
    ),
    "iron_ore": (
        "PIORECRUSDM",
        "Iron ore, China import CFR, IMF (monthly)",
        "USD/t",
        "For steel and NMDC when built.",
    ),
}


def fetch(series_id: str) -> list[tuple[str, float]]:
    req = urllib.request.Request(CSV_URL.format(sid=series_id),
                                 headers={"User-Agent": FRED_UA})
    with urllib.request.urlopen(req, timeout=25) as resp:
        text = resp.read().decode("utf-8", errors="replace")

    rows: list[tuple[str, float]] = []
    for rec in csv.DictReader(io.StringIO(text)):
        vals = list(rec.values())
        date, val = vals[0], vals[1]
        if not val or val in (".", ""):
            continue          # FRED marks missing observations with a dot
        rows.append((date, float(val)))
    if not rows:
        raise ValueError("no observations")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--since", default="2024-01-01")
    a = ap.parse_args()

    results = {}
    for eid, (sid, desc, unit, _) in SERIES.items():
        try:
            rows = fetch(sid)
            results[eid] = rows
            print(f"OK    {eid:24} {sid:14} {len(rows):>5} obs  "
                  f"{rows[0][0]} -> {rows[-1][0]}  last={rows[-1][1]:,.2f} {unit}")
        except Exception as exc:
            print(f"FAIL  {eid:24} {sid:14} {type(exc).__name__}: {str(exc)[:40]}")

    if a.probe:
        print("\nbasis notes:")
        for eid, (_, _, _, note) in SERIES.items():
            if eid in results:
                print(f"  {eid}:\n    {note}")
        return 0

    if a.load:
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA foreign_keys = ON")
        n = 0
        for eid, rows in results.items():
            conn.execute(
                "INSERT OR IGNORE INTO entities (id,kind,name) VALUES (?,?,?)",
                (eid, "commodity", SERIES[eid][1]),
            )
            keep = [(eid, d, v) for d, v in rows if d >= a.since]
            conn.executemany(
                "INSERT OR REPLACE INTO prices (entity_id,date,close,currency) "
                "VALUES (?,?,?,'USD')", keep,
            )
            n += len(keep)
        conn.commit()
        conn.close()
        print(f"\nloaded {n} rows since {a.since}")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
