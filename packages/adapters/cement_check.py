"""L0 adapter — Kotak's monthly cement channel-check note, as an EARLY print of
the cement pack's own regional series.

    python packages/adapters/cement_check.py --load-all          # refresh.py step
    python packages/adapters/cement_check.py --file data/staging/cement_check_2026-09-23.json [--load]
    python packages/adapters/cement_check.py --selftest

WHY THIS EXISTS — FOUND, NOT ASSUMED (sweep of September 2026 mail, 2026-10-01).
Kotak's "[Kotak] Construction Materials: ..." mail of 23-Sep-2026 printed
September's regional m/m changes as +3.5/+3.0/+2.1/+1.3/+1.0% (E/S/W/N/C) and
+2.2% all-India. The Daily Cement Pack carried EXACTLY those numbers when its
September column first appeared — on 30-Sep. The pack is the spreadsheet form
of that note, so the note is the same measure, from the same broker, ~7 days
earlier. That week is the whole point: cement economics sat on August's soft
print until the 30th and then jumped ~2.5 score points in one session.

THE SAME SERIES IDS, A DIFFERENT SOURCE TAG. Invariant 6 forbids aliasing a
PROXY to the thing it proxies. This is not a proxy: same broker, same channel
checks, same numbers, so it writes `cement_price_<region>_inr` — but as
source `kotak_check` (rank 35, below `cement_pack`), so provenance stays
visible on every row and the pack wins any collision.

THE OTHER HOUSES ARE DELIBERATELY NOT LOADED. The same sweep read Nuvama,
Nomura, JPM, IIFL and Elara. Early-month notes are ANNOUNCED hikes and
overstated September 2-10x (Nuvama: Central +Rs35-40/bag; realised +Rs3).
Late-month notes agree on all-India but disagree on regions (East: Kotak
+3.5%, JPM and IIFL flat). Different basis too (Nomura quotes trade ~Rs330,
Kotak ~Rs359). Only Kotak's note is the pack's own number.

HOW A LEVEL IS BUILT. The note gives a m/m PERCENT, not a level. Level =
the pack's stored level for the PRIOR month x (1 + pct/100). The percent is
printed to one decimal, so the rebuilt level can sit up to ~0.05% (~Rs3.5/t)
off the pack's — immaterial to a shock, and the pack replaces it anyway.

STAMPED AT THE MAIL DATE, NEVER THE MONTH END. The note is knowable the day it
lands (the `effective_from` rule: the date the market COULD HAVE KNOWN it).
`min(month_end, mail_date)`, the cement_pack._stamp convention. A future mail
date is refused.

THE PACK SUPERSEDES. Two ways, both tested:
  1. This loader skips a month the pack already carries (any `cement_pack` row
     dated in it) and prints how far its rebuilt level is from the pack's.
  2. `cement_pack.py --load` DELETES `kotak_check` rows for every month it
     carries before writing. Without that, the 23rd (check) and the 30th
     (pack) would sit side by side as two "prints" of one month.

ALL-OR-NOTHING. Six regions or nothing; |pct| > 15 refused (October 2025's
GST cut, the largest move in 149 months, was -11.4% at its worst region, so 15
admits every real month and refuses a Rs/bag figure read as a percent); a
missing prior-month base refuses the file rather than guessing one.

Staging file shape (written by the full-refresh skill, Step 2b'):
    data/staging/cement_check_<mail date YYYY-MM-DD>.json
    {"month": "2026-09",
     "received": "2026-09-23T08:49:54+05:30",
     "sender": "siddharth.mehrotra@kotak.com",
     "subject": "[Kotak] Construction Materials: Prices firm up, trail cost pressures",
     "mom_pct": {"east": 3.5, "south": 3.0, "west": 2.1, "north": 1.3,
                 "central": 1.0, "india": 2.2},
     "quote": "<the sentence(s) the numbers come from, verbatim>"}
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))
import prices_io  # noqa: E402

DB = REPO / "data" / "ims.db"
STAGE = REPO / "data" / "staging"
SOURCE = "kotak_check"

REGIONS = {
    "north":   "cement_price_north_inr",
    "central": "cement_price_central_inr",
    "east":    "cement_price_east_inr",
    "west":    "cement_price_west_inr",
    "south":   "cement_price_south_inr",
    "india":   "cement_price_india_inr",
}
MAX_ABS_PCT = 15.0


def _month_bounds(month: str) -> tuple[dt.date, dt.date]:
    y, m = (int(x) for x in month.split("-"))
    return dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])


def validate(doc: dict, mail_date: dt.date) -> list[str]:
    """Every problem with the file, empty when it may load."""
    bad = []
    for k in ("month", "received", "subject", "mom_pct", "quote"):
        if not doc.get(k):
            bad.append(f"missing or empty '{k}'")
    if bad:
        return bad
    if "kotak" not in (doc.get("sender", "") + doc["subject"]).lower():
        bad.append("not a Kotak note — only Kotak's check IS the pack's number")
    try:
        first, _last = _month_bounds(doc["month"])
    except ValueError:
        return bad + [f"month {doc['month']!r} is not YYYY-MM"]
    if mail_date < first:
        bad.append(f"mail dated {mail_date} predates the month it reports")
    if mail_date > dt.date.today():
        bad.append(f"mail dated {mail_date} is in the future")
    pct = doc["mom_pct"]
    missing = sorted(set(REGIONS) - set(pct))
    if missing:
        bad.append(f"regions missing: {missing} (all six or nothing)")
    for r, v in pct.items():
        if r not in REGIONS:
            bad.append(f"unknown region {r!r}")
        elif not isinstance(v, (int, float)) or abs(v) > MAX_ABS_PCT:
            bad.append(f"{r}: {v!r} is not a plausible m/m percent "
                       f"(|x| <= {MAX_ABS_PCT})")
    return bad


def build(conn: sqlite3.Connection, doc: dict, mail_date: dt.date):
    """-> (rows, superseded, problems). rows = [(eid, date, level)]."""
    first, last = _month_bounds(doc["month"])
    stamp = min(last, mail_date).isoformat()
    rows, problems, superseded = [], [], {}
    for r, eid in REGIONS.items():
        pack = conn.execute(
            "SELECT date, close FROM prices WHERE entity_id=? AND source="
            "'cement_pack' AND date>=? AND date<=? ORDER BY date DESC LIMIT 1",
            (eid, first.isoformat(), last.isoformat())).fetchone()
        base = conn.execute(
            "SELECT date, close FROM prices WHERE entity_id=? AND source="
            "'cement_pack' AND date<? ORDER BY date DESC LIMIT 1",
            (eid, first.isoformat())).fetchone()
        if base is None or base[0][:7] != (first - dt.timedelta(days=1)).isoformat()[:7]:
            problems.append(f"{eid}: no pack level for the month before "
                            f"{doc['month']} to apply {doc['mom_pct'][r]:+}% to")
            continue
        level = base[1] * (1 + doc["mom_pct"][r] / 100.0)
        if pack is not None:
            superseded[eid] = (level, pack[1])
        else:
            rows.append((eid, stamp, level))
    return rows, superseded, problems


def load_file(conn: sqlite3.Connection, path: pathlib.Path, write: bool) -> int:
    mail_date = dt.date.fromisoformat(path.stem.replace("cement_check_", ""))
    doc = json.loads(path.read_text(encoding="utf-8"))
    bad = validate(doc, mail_date)
    if bad:
        print(f"REFUSED {path.name}:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    rows, superseded, problems = build(conn, doc, mail_date)
    if problems:
        print(f"REFUSED {path.name} (whole file):\n  " + "\n  ".join(problems),
              file=sys.stderr)
        return 1
    if superseded:
        worst = max(abs(a / b - 1) for a, b in superseded.values()) * 100
        print(f"{path.name}: {doc['month']} already in the pack for "
              f"{len(superseded)} series — skipped; rebuilt level within "
              f"{worst:.2f}% of the pack's")
    if rows:
        print(f"{path.name}: {doc['month']} early print, stamped {rows[0][1]}")
        for eid, d, c in rows:
            print(f"  {eid:26} {c:>9,.1f} Rs/t  ({c / 20:.1f}/bag)")
        if write:
            res = prices_io.upsert(conn, rows, SOURCE)
            print("  " + prices_io.report(res))
    return 0


def _selftest() -> int:
    fails = 0

    def check(name, ok):
        nonlocal fails
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        fails += 0 if ok else 1

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE prices (entity_id TEXT, date TEXT, open REAL, "
                 "high REAL, low REAL, close REAL, volume REAL, currency TEXT, "
                 "source TEXT, PRIMARY KEY (entity_id, date))")
    for eid in REGIONS.values():
        conn.execute("INSERT INTO prices (entity_id,date,close,source) VALUES "
                     "(?,?,?,'cement_pack')", (eid, "2026-08-31", 7000.0))
    good = {"month": "2026-09", "received": "2026-09-23T08:49:54+05:30",
            "sender": "siddharth.mehrotra@kotak.com",
            "subject": "[Kotak] Construction Materials: Prices firm up",
            "mom_pct": {"east": 3.5, "south": 3.0, "west": 2.1, "north": 1.3,
                        "central": 1.0, "india": 2.2},
            "quote": "Prices in September 2026 changed by +3.5%/..."}
    d = dt.date(2026, 9, 23)

    # ACCEPTANCE — per the GLOB lesson a guard needs both directions.
    check("valid file passes", validate(good, d) == [])
    rows, sup, prob = build(conn, good, d)
    check("six rows, no problems", len(rows) == 6 and not prob and not sup)
    check("stamped at the mail date, not month end",
          all(r[1] == "2026-09-23" for r in rows))
    east = dict((e, c) for e, _d, c in rows)["cement_price_east_inr"]
    check("level = prior pack level x (1+pct)", abs(east - 7245.0) < 1e-6)

    # REJECTION
    check("missing region refused",
          validate({**good, "mom_pct": {k: v for k, v in good["mom_pct"].items()
                                        if k != "east"}}, d) != [])
    check("a Rs/bag figure read as % refused",
          validate({**good, "mom_pct": {**good["mom_pct"], "east": 12.0 * 2}}, d) != [])
    check("non-Kotak note refused",
          validate({**good, "sender": "x@iiflcap.com", "subject": "IIFL cement"}, d) != [])
    check("empty quote refused", validate({**good, "quote": ""}, d) != [])
    check("mail before its month refused",
          validate(good, dt.date(2026, 8, 30)) != [])
    check("future mail refused",
          validate(good, dt.date.today() + dt.timedelta(days=1)) != [])
    oct_doc = {**good, "month": "2026-11"}
    _r, _s, prob = build(conn, oct_doc, dt.date(2026, 11, 20))
    check("no prior-month base -> refused, not guessed", len(prob) == 6)

    # A note arriving after month end stamps at month end, never in the next month.
    rows2, _s, _p = build(conn, good, dt.date(2026, 10, 2))
    check("late mail stamps at month end", rows2[0][1] == "2026-09-30")

    # SUPERSEDED — the pack already carries the month.
    for eid in REGIONS.values():
        conn.execute("INSERT INTO prices (entity_id,date,close,source) VALUES "
                     "(?,?,?,'cement_pack')", (eid, "2026-09-30", 7240.0))
    rows3, sup3, _p = build(conn, good, d)
    check("pack month present -> nothing written, all superseded",
          rows3 == [] and len(sup3) == 6)
    print(f"{'PASS' if not fails else 'FAIL'} — {fails} failure(s)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--load-all", action="store_true",
                    help="every staged cement_check_*.json; idempotent")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    if a.load_all:
        files = sorted(STAGE.glob("cement_check_*.json"))
        if not files:
            print("no cement_check staging files — nothing to do")
            return 0
        rc = max(load_file(conn, f, True) for f in files)
    elif a.file:
        rc = load_file(conn, pathlib.Path(a.file), a.load)
        if not a.load:
            print("probe only — pass --load to write")
    else:
        ap.error("give --file, --load-all or --selftest")
    conn.commit()
    conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
