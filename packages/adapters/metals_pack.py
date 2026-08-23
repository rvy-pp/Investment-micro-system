"""L0 adapter — the broker's Daily Metals Pack workbook into `prices`.

WHY THIS SUPERSEDES SEVERAL YAHOO SERIES. The pack carries ASSESSED and CASH
prices, daily back to 2010, where the repo had been using front-month futures as
proxies. Three consequences, all of which were live defects:

  alumina_index   was ALA=F, which carries a -21.3% front-month ROLL on
                  2025-02-03 that is not a price move. The pack's Australia FOB
                  assessment has no roll. `backfill_p1.py` was excluding 30-day
                  windows around that date to avoid booking a crash that never
                  happened; with this series those exclusions are unnecessary.
  lme_aluminium   was ALI=F (CME), which carries a Midwest premium basis and read
                  3,355.50 against the digest's LME 3,310.50 on the same day.
                  This is the LME cash price the specs actually reference.
  lme_zinc        HAD NO FEED AT ALL. yahoo_prices lists it under RESEARCH_SOURCED
                  with "no live free feed found; PRIMARY driver of the zinc peer
                  group, so this gap blocks that group's daily run". It is column
                  2 here, 4,722 rows from 2010. The zinc book is now runnable on
                  the real benchmark rather than the SHFE proxy.
  cp_coke         specified in aluminium.yaml with a 0.40 t/t intensity and never
                  priced, so anode cost silently contributed ZERO to every bridge.

VERIFIED CLEAN: LME aluminium and LME zinc show zero >=15% daily jumps across 16
years. Alumina, coal and coke DO jump, but those are real events -- Rusal
sanctions 2018-04-19 (+34.9%), Guinea coup 2021-09-09 (+24.9%), the 2021 energy
crisis -- not contract artefacts. Do not "clean" them.

A CORRECTION THIS SOURCE FORCED: silver's -26.4% on 2026-01-30 appears in BOTH
this spot assessment and Yahoo's SI=F. It is therefore a genuine move, and the
earlier classification of it as a front-month roll in beta_stability.py was
wrong.

Usage:
    python packages/adapters/metals_pack.py --file "<path.xlsx>" --probe
    python packages/adapters/metals_pack.py --file "<path.xlsx>" --load
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))

import prices_io  # noqa: E402
DB = REPO / "data" / "ims.db"
SHEET = "Daily prices "          # NOTE the trailing space; it is in the file
HEADER_ROW = 18                  # data starts at 19

# workbook column index -> (entity_id, kind, note)
# Only series the specs actually reference are loaded. The pack also carries
# steel, iron ore, copper, nickel and rebar, deliberately left out: nothing in
# the aluminium or zinc specs links to them, and loading unused series makes
# `_series_in_store()` claim coverage the bridge cannot use.
COLS = {
    1:  ("lme_aluminium", "commodity", "LME Aluminium cash US$/t"),
    2:  ("lme_zinc", "commodity", "LME Zinc cash US$/t — was UNPRICED"),
    11: ("thermal_coal_seaborne", "commodity", "Richards Bay thermal US$/t"),
    13: ("silver", "commodity", "Silver spot US$/toz"),
    15: ("usdinr", "fx", "INR:USD"),
    16: ("brent", "commodity", "Brent US$/bbl"),
    20: ("alumina_index", "commodity", "Alumina Australia FOB US$/t — assessed"),
    24: ("cp_coke", "commodity", "Pet coke US$/t — was UNPRICED"),
    32: ("usdcny", "fx", "USDCNY"),
}


EPOCH = dt.date(1899, 12, 30)      # Excel's serial-date origin


def read_tsv(path: pathlib.Path) -> dict[str, dict[str, float]]:
    """Read the MCP's text extraction of the same workbook.

    WHY A SECOND READER. The pack arrives as an email attachment, and the
    Microsoft 365 MCP returns attachments as TAB-SEPARATED TEXT, not as bytes —
    so there is no .xlsx on disk to hand to openpyxl. The column indices are
    identical to the workbook (verified: col1 LME aluminium, col2 zinc, col11
    Richards Bay, col13 silver, col15 INR, col20 alumina, col24 pet coke,
    col32 USDCNY), so COLS is shared and only the parsing differs.

    DATES COME BACK AS EXCEL SERIALS. 40182 is 2010-01-04, counted from
    1899-12-30 — the 1900 leap-year bug means the origin is the 30th, not the
    31st. Reading them as ISO or off-by-one shifts the entire series by a day,
    which would look like nothing at all on a chart.

    TRUNCATION IS EXPECTED AND MATTERS. The MCP caps a read at ~200k characters,
    which is ~830 of the pack's ~4,760 rows, and it keeps the OLDEST rows. So a
    single extraction cannot rebuild history — it is for the daily increment,
    where only the last row is needed. `--load` on a truncated file is still
    safe because prices are INSERT OR REPLACE keyed on (entity_id, date), but
    the caller should know it is topping up, not backfilling.
    """
    import csv
    out: dict[str, dict[str, float]] = {v[0]: {} for v in COLS.values()}
    with open(path, encoding="utf-8") as f:
        rows = list(csv.reader(f, delimiter="	"))
    for row in rows:
        if not row or not row[0].strip():
            continue
        try:                                  # a data row starts with a serial
            serial = int(float(row[0]))
        except ValueError:
            continue                          # sheet banner or header line
        if not 20000 < serial < 60000:        # ~1954..2064, rejects stray ints
            continue
        iso = (EPOCH + dt.timedelta(days=serial)).isoformat()
        for j, (eid, _k, _n) in COLS.items():
            if j >= len(row):
                continue
            raw = row[j].strip()
            if not raw or raw.startswith("#"):     # '#N/A' is a gap, not a price
                continue
            try:
                v = float(raw)
            except ValueError:
                continue
            if v > 0:
                out[eid][iso] = v
    return out


def read(path: pathlib.Path) -> dict[str, dict[str, float]]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[SHEET]
    out: dict[str, dict[str, float]] = {v[0]: {} for v in COLS.values()}
    for row in ws.iter_rows(min_row=HEADER_ROW + 1, values_only=True):
        d = row[0]
        if not isinstance(d, dt.datetime):
            continue
        iso = d.date().isoformat()
        for j, (eid, _k, _n) in COLS.items():
            v = row[j] if j < len(row) else None
            # '#N/A' arrives as a string; a zero or negative assessment is a gap
            # in the pack, not a price. Both must be dropped rather than loaded.
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                out[eid][iso] = float(v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True,
                    help=".xlsx from disk, or .tsv/.txt from the MCP extraction")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--load", action="store_true")
    a = ap.parse_args()

    f = pathlib.Path(a.file)
    series = read_tsv(f) if f.suffix.lower() in (".tsv", ".txt") else read(f)

    if a.probe or not a.load:
        print(f"{'entity':24}{'rows':>7}  span")
        for j, (eid, _k, note) in sorted(COLS.items()):
            ks = sorted(series[eid])
            # A series can legitimately be EMPTY in a given extraction: cp_coke
            # starts 2018, so a truncated read covering 2010-2013 has none of
            # it. Report the gap rather than indexing into nothing.
            span = f"{ks[0]} .. {ks[-1]}" if ks else "— no rows in this file —"
            print(f"{eid:24}{len(ks):>7}  {span}   {note}")
        if not a.load:
            print("\nprobe only — pass --load to write")
            return 0

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    n = 0
    for j, (eid, kind, note) in sorted(COLS.items()):
        # is_tradeable=1: the schema requires a parent_id for an untradeable entity,
        # and a bare commodity has none. peer_group stays NULL so it is never scored.
        conn.execute("INSERT OR IGNORE INTO entities (id,kind,name,is_tradeable,active) "
                     "VALUES (?,?,?,1,1)", (eid, kind, eid))
        rows = [(eid, d, c) for d, c in sorted(series[eid].items())]
        # metals_pack is the HIGHEST-ranked source: licensed, hand-dropped, the
        # desk's own reference. Nothing may overwrite a cell it owns.
        res = prices_io.upsert(conn, rows, "metals_pack")
        n += res["wrote"]
    conn.commit()
    conn.close()
    print(f"\nloaded {n:,} price rows into {DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
