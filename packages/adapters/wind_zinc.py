"""L0 adapter — SHFE zinc (ZN.SHF) from Wind, converted to an LME-comparable
USD/t series.

WHY A STAGING FILE. The Wind MCP is callable by the agent, not by a Python
process, so the fetch and the load are separate steps by necessity:

    1. agent calls  get_wind_historical_data(ZN.SHF, close, ...)
    2. output is written to data/staging/zn_shf_close.csv
    3. this script converts and loads it

That boundary is deliberate rather than a workaround — the staging file is a
dated, inspectable record of exactly what Wind returned, which is the same
provenance discipline the rest of the system uses.

TWO ADJUSTMENTS, BOTH REQUIRED. SHFE zinc is NOT LME zinc:

  1. CURRENCY. Quoted in CNY/t. Converted at USDCNY (Yahoo CNY=X), per date,
     so an FX move is not mistaken for a zinc move.

  2. VAT. SHFE prices INCLUDE Chinese VAT at 13%; LME prices do not. Left in,
     the level is ~13% too high AND every delta is inflated by the same factor,
     which would overstate every zinc impact in the bridge. Divided out.

RESIDUAL BASIS, not adjusted and not hidden: SHFE is a Chinese DOMESTIC
contract. It carries import-arbitrage and local supply/demand that LME does
not, so the two can diverge for weeks. Good for direction and rough magnitude,
not a substitute for LME. Recorded as `zinc_shfe` — never as `lme_zinc` — so
nothing in the specs can silently mistake it for the benchmark.

Usage:
    python packages/adapters/wind_zinc.py --load
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))

import prices_io  # noqa: E402
DB = REPO / "data" / "ims.db"
STAGING = REPO / "data" / "staging" / "zn_shf_close.csv"

CHINA_VAT = 0.13
ENTITY = "zinc_shfe"


def fx_map(conn) -> dict[str, float]:
    return {d: v for d, v in conn.execute(
        "SELECT date, close FROM prices WHERE entity_id='usdcny' ORDER BY date")}


def last_on_or_before(fx: dict[str, float], date: str) -> float | None:
    keys = [k for k in fx if k <= date]
    return fx[max(keys)] if keys else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--vat", type=float, default=CHINA_VAT)
    a = ap.parse_args()

    if not STAGING.exists():
        print(f"staging file missing: {STAGING}\n"
              f"run the Wind MCP fetch first and write its output there",
              file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    fx = fx_map(conn)
    if not fx:
        print("no usdcny in prices — load CNY=X via the yahoo adapter first",
              file=sys.stderr)
        return 1

    rows, skipped = [], 0
    with STAGING.open(encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            date, cny = rec["date"], float(rec["close_cny"])
            rate = last_on_or_before(fx, date)
            if not rate:
                skipped += 1
                continue
            usd_ex_vat = (cny / (1.0 + a.vat)) / rate
            rows.append((ENTITY, date, round(usd_ex_vat, 2)))

    if not a.load:
        print(f"{len(rows)} rows would load ({skipped} skipped, no FX)")
        for r in rows[:3] + rows[-3:]:
            print(f"   {r[1]}  {r[2]:>9,.2f} USD/t")
        return 0

    conn.execute(
        "INSERT OR IGNORE INTO entities (id,kind,name) VALUES (?,?,?)",
        (ENTITY, "commodity", "Zinc SHFE, ex-VAT, USD/t (Wind ZN.SHF)"))
    prices_io.upsert(conn, [(r[0], r[1], r[2]) for r in rows], "wind",
                     currency="USD")
    conn.commit()

    first, last = rows[0], rows[-1]
    print(f"loaded {len(rows)} rows as {ENTITY} (USD/t, ex-VAT at {a.vat:.0%})")
    print(f"  {first[1]}  {first[2]:>9,.2f}")
    print(f"  {last[1]}  {last[2]:>9,.2f}")
    if skipped:
        print(f"  {skipped} rows skipped: no USDCNY on or before that date")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
