"""LME official cash-settlement, fetched directly. Cron-safe, no agent.

    python packages/adapters/westmetall.py                 # probe, writes nothing
    python packages/adapters/westmetall.py --load
    python packages/adapters/westmetall.py --year 2024 --load     # backfill

WHY THIS IS NOT AN AGENT STEP, correcting an earlier version of this file.
lme.com returns HTTP 403 to everything — browser UA, curl UA, no UA — and its
prices are licensed. From that I concluded LME needed an agent with WebFetch and
built this as a staging-capture adapter on the wind_zinc.py pattern. That was
wrong, and wrong in a way worth recording: I inferred the constraint from one
blocked host without testing the mirror. **westmetall answers plain stdlib
urllib with HTTP 200 in ~1.5s.** No agent, no MCP, no login — an ordinary cron
adapter like yahoo_prices.py.

That distinction matters for scheduling. Wind and Microsoft 365 are
interactively-authenticated MCP servers and genuinely can fail unattended — the
vault's own daily-morning-orchestrator carries a KNOWN CAVEAT saying exactly
that about /mail-read. This is not in that category and never was.

It also removes a real hazard. The agent version had a model transcribe 161 rows
per metal out of a rendered table and into JSON. Nothing in the validator can
catch 3,182 read as 3,812 — it is in range, on a weekday, not in the future. The
model is now out of the number path entirely, which is what the repo's core
inversion asks for.

THE COLUMN GUARD IS THE IMPORTANT PART. The table is
`date | Cash-Settlement | 3-month | stock`, and cash vs 3-month is a real
instrument difference — westmetall's own zinc basis averaged +84 USD/t. Parsing
by POSITION would silently load the 3-month as cash the day they reorder
columns. So the cash column is located by matching its HEADER TEXT, and a header
that does not match exactly one column is a hard failure rather than a fallback.

DAY-DELAYED. The newest row is T-1. Dating it T would be a look-ahead bug of the
kind the effective_from rule warns about. Rows are loaded on their own LME dates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sqlite3
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
STAGING = REPO / "data" / "staging"
sys.path.insert(0, str(REPO / "packages" / "core"))

import prices_io  # noqa: E402

SOURCE = "westmetall"
BASE = "https://www.westmetall.com/en/markdaten.php?action=table&field={field}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# entity_id -> westmetall field slug
FIELDS = {"lme_aluminium": "LME_Al_cash", "lme_zinc": "LME_Zn_cash"}

# The header the CASH column must carry. Matched case-insensitively as a
# substring, and it must hit EXACTLY ONE column — see the module docstring.
CASH_HEADER = "cash-settlement"

# ...AND it must sit here: `date | cash | 3-month | stock`.
#
# BELT AND BRACES, because the header check alone has a hole. Mutating the page
# four ways, three are caught loudly (header renamed, header duplicated, table
# removed) but a header/data DESYNC is not: matching "cash-settlement" at index 2
# while the data at index 2 is still the 3-month returns 3,183.00 for 3,182.00 —
# in range, right date, wrong instrument. Exactly the silent class.
#
# Pinning the position converts every layout change into a loud failure. If
# westmetall genuinely reorders its columns this WILL fire on a correct page —
# that is intended. A person should look and move the constant, rather than the
# adapter quietly deciding which column is cash today.
EXPECTED_CASH_COL = 1

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

# A cash price outside this band is a parse error, not a market move. Wide on
# purpose: its job is to catch a mis-parsed column or a stock figure (246,925)
# landing in a price field, not to have a view on the market.
SANE = (500.0, 20_000.0)


def fetch(field: str, year: int | None = None, timeout: int = 30) -> str:
    url = BASE.format(field=field) + (f"&year={year}" if year else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse(html: str) -> tuple[dict[str, float], str]:
    """Return ({iso_date: cash}, header_text). Raises on any ambiguity."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    cells = [[re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
              for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
             for r in rows]
    cells = [c for c in cells if c]
    if not cells:
        raise ValueError("no table rows found — page structure changed")

    header = cells[0]
    hits = [i for i, h in enumerate(header) if CASH_HEADER in h.lower()]
    if len(hits) != 1:
        raise ValueError(
            f"expected exactly one column matching {CASH_HEADER!r}, found "
            f"{len(hits)} in {header!r}. Refusing to guess — cash and 3-month "
            f"differ by ~84 USD/t and loading the wrong one is silent.")
    col = hits[0]
    if col != EXPECTED_CASH_COL:
        raise ValueError(
            f"cash column is at index {col}, expected {EXPECTED_CASH_COL}. "
            f"Header: {header!r}. The layout changed — refusing rather than "
            f"guessing, because reading the 3-month as cash is silent and worth "
            f"~84 USD/t. Verify the page and move EXPECTED_CASH_COL.")

    out: dict[str, float] = {}
    for row in cells[1:]:
        if len(row) <= col:
            continue
        m = re.match(r"^\s*(\d{1,2})\.\s*([A-Za-z]+)\s+(\d{4})\s*$", row[0])
        if not m:
            continue
        day, mon, yr = m.group(1), m.group(2), m.group(3)
        if mon not in MONTHS:
            continue
        raw = row[col].replace(",", "").strip()
        if not raw or raw in {"-", "n.a."}:
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        out[f"{yr}-{MONTHS[mon]:02d}-{int(day):02d}"] = val
    if not out:
        raise ValueError("header parsed but no data rows — page structure changed")
    return out, header[col]


def validate(series: dict[str, dict[str, float]], today: dt.date) -> list[str]:
    errs = []
    for eid, rows in series.items():
        if not rows:
            errs.append(f"{eid}: no rows")
            continue
        for iso, v in rows.items():
            d = dt.date.fromisoformat(iso)
            if d > today:
                errs.append(f"{eid}: {iso} is in the future")
            if d.weekday() > 4:
                errs.append(f"{eid}: {iso} is a weekend — the LME does not settle")
            if not SANE[0] <= v <= SANE[1]:
                errs.append(f"{eid}: {iso} value {v} outside {SANE}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, help="historical year; default is current")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--no-staging", action="store_true",
                    help="skip writing the dated capture")
    a = ap.parse_args()

    today = dt.date.today()
    series, headers, digests = {}, {}, {}
    for eid, field in FIELDS.items():
        try:
            html = fetch(field, a.year)
        except Exception as exc:
            print(f"{eid}: FETCH FAILED — {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        digests[eid] = hashlib.sha256(html.encode()).hexdigest()[:16]
        try:
            series[eid], headers[eid] = parse(html)
        except ValueError as exc:
            print(f"{eid}: PARSE FAILED — {exc}", file=sys.stderr)
            return 1

    print(f"{'series':16}{'rows':>6}{'first':>13}{'last':>13}{'newest':>12}   column")
    print("-" * 92)
    for eid, rows in series.items():
        ks = sorted(rows)
        age = (today - dt.date.fromisoformat(ks[-1])).days
        print(f"{eid:16}{len(rows):>6}{ks[0]:>13}{ks[-1]:>13}"
              f"{rows[ks[-1]]:>12,.2f}   {headers[eid]}  ({age}d)")

    errs = validate(series, today)
    if errs:
        print(f"\n{len(errs)} VALIDATION ERROR(S) — nothing loaded:")
        for e in errs[:20]:
            print(f"   {e}")
        return 1

    if not a.no_staging:
        STAGING.mkdir(parents=True, exist_ok=True)
        stamp = a.year or today.isoformat()
        p = STAGING / f"westmetall_{stamp}.json"
        p.write_text(json.dumps({
            "source": SOURCE,
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "fetched_by": "packages/adapters/westmetall.py — direct stdlib urllib, "
                          "no agent and no MCP",
            "urls": {e: BASE.format(field=f) + (f"&year={a.year}" if a.year else "")
                     for e, f in FIELDS.items()},
            "columns": headers,
            "html_sha256_16": digests,
            "unit": "USD per tonne",
            "note": "LME official CASH-SETTLEMENT, day-delayed (newest row is T-1). "
                    "The 3-month column is deliberately not captured: nothing "
                    "links to it and an unused series makes _series_in_store() "
                    "claim coverage the bridge cannot use.",
            "series_cash": series,
        }, indent=1, sort_keys=True), encoding="utf-8")
        print(f"\nstaging -> {p.relative_to(REPO)}")

    if not a.load:
        print("\nprobe only — pass --load to write")
        return 0

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    for eid in FIELDS:
        conn.execute("INSERT OR IGNORE INTO entities (id,kind,name,is_tradeable,active) "
                     "VALUES (?,'commodity',?,1,1)", (eid, eid))
    rows = [(eid, iso, v) for eid, s in series.items() for iso, v in sorted(s.items())]
    res = prices_io.upsert(conn, rows, SOURCE, currency="USD")
    conn.commit()
    conn.close()
    print("\n" + prices_io.report(res))
    if res["refused"]:
        print("   refused cells belong to the Daily Metals Pack, which is licensed "
              "and outranks this source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
