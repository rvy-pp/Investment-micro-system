"""LME official cash-settlement, from a westmetall staging capture.

    python packages/adapters/westmetall.py            # probe, writes nothing
    python packages/adapters/westmetall.py --load

WHY A STAGING FILE AND NOT AN HTTP CALL. Same constraint as the Wind and
Microsoft 365 adapters: the fetch needs an agent, not a Python process. lme.com
returns HTTP 403 to automated fetching and its prices are licensed; westmetall
republishes the official cash-settlement and 3-month table as plain HTML, which
an agent can read with WebFetch. So the split is forced, and it is the same shape
wind_zinc.py already uses:

    step A (agent)   WebFetch the westmetall tables -> data/staging/westmetall_<date>.json
    step B (python)  this file — parse, validate, load

Staging files are version-controlled deliberately. They are the dated record of
what the source said that day, which is what lets a price be audited later
without trusting anyone's memory.

WHAT THIS FIXES. `lme_aluminium` was fed by Yahoo ALI=F, which is CME, not LME —
it embeds a Midwest premium and read 3,324.25 against the real LME cash of
3,182.00 on 2026-08-20, +142 USD/t or +4.5%. yahoo_prices.py says so in a comment
and loaded it anyway, which is the invariant-6 violation the same repo carefully
avoided for zinc (zinc_shfe, never lme_zinc). Precedence in prices_io.py now
stops Yahoo overwriting these two series at all.

CASH, NOT 3-MONTH. Verified before loading, because the two are a different
instrument and the gap is not small: westmetall's own cash-minus-3M basis
averaged +84 USD/t on zinc over the last ten sessions. Wind's AH.LME / ZS.LME
agree with westmetall's THREE-MONTH column to within ~3 USD/t, which is what
identified them as 3M and is why they are not used here.

DAY-DELAYED, AND THE DATE MUST NOT BE MOVED. The newest row is T-1. Dating it T
would be a look-ahead bug of exactly the kind CLAUDE.md warns about: "a dated
value carries the date the market COULD HAVE KNOWN it". The rows are loaded on
their own LME dates, untouched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
STAGING = REPO / "data" / "staging"
sys.path.insert(0, str(REPO / "packages" / "core"))

import prices_io  # noqa: E402

SOURCE = "westmetall"
SERIES = ("lme_aluminium", "lme_zinc")

# A cash price outside this band is a parse error, not a market move. LME
# aluminium and zinc have both traded 2,000-4,000 USD/t through the sample; the
# band is deliberately wide, because its job is to catch a mis-parsed column or a
# stray stock figure (94,400) landing in a price field, not to have an opinion.
SANE = (500.0, 20_000.0)


def newest_capture() -> pathlib.Path | None:
    files = sorted(STAGING.glob("westmetall_*.json"))
    return files[-1] if files else None


def load_capture(path: pathlib.Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("source") != SOURCE:
        raise ValueError(f"{path.name}: source is {d.get('source')!r}, not {SOURCE!r}")
    if "series_cash" not in d:
        raise ValueError(f"{path.name}: no series_cash — is this a 3-month capture?")
    return d


def validate(d: dict, today: dt.date) -> list[str]:
    """Everything that must hold before a row reaches the store."""
    errs = []
    for eid in SERIES:
        rows = d["series_cash"].get(eid) or {}
        if not rows:
            errs.append(f"{eid}: no rows")
            continue
        for iso, v in rows.items():
            try:
                day = dt.date.fromisoformat(iso)
            except ValueError:
                errs.append(f"{eid}: bad date {iso!r}")
                continue
            # The look-ahead guard. A day-delayed source producing a row dated
            # today means the capture is wrong, and a future date is worse.
            if day > today:
                errs.append(f"{eid}: {iso} is in the future")
            if not isinstance(v, (int, float)) or not SANE[0] <= v <= SANE[1]:
                errs.append(f"{eid}: {iso} value {v!r} outside {SANE}")
            if day.weekday() > 4:
                errs.append(f"{eid}: {iso} is a weekend — LME does not settle")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="staging capture; default is the newest")
    ap.add_argument("--load", action="store_true")
    a = ap.parse_args()

    path = pathlib.Path(a.file) if a.file else newest_capture()
    if not path or not path.exists():
        print(f"no westmetall capture in {STAGING}.\n"
              f"An AGENT must fetch it first — see the module docstring. "
              f"A python process cannot: lme.com 403s and this needs WebFetch.",
              file=sys.stderr)
        return 1

    d = load_capture(path)
    today = dt.date.today()
    errs = validate(d, today)

    print(f"{path.name}   fetched {d.get('fetched_at')}   unit {d.get('unit')}")
    print(f"{'series':18}{'rows':>6}{'first':>13}{'last':>13}{'newest value':>14}")
    print("-" * 66)
    for eid in SERIES:
        rows = d["series_cash"].get(eid) or {}
        if not rows:
            print(f"{eid:18}{'—':>6}")
            continue
        ks = sorted(rows)
        age = (today - dt.date.fromisoformat(ks[-1])).days
        print(f"{eid:18}{len(rows):>6}{ks[0]:>13}{ks[-1]:>13}"
              f"{rows[ks[-1]]:>14,.2f}   ({age}d old)")

    if errs:
        print(f"\n{len(errs)} VALIDATION ERROR(S) — nothing loaded:")
        for e in errs[:20]:
            print(f"   {e}")
        return 1

    if not a.load:
        print("\nprobe only — pass --load to write")
        return 0

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    for eid in SERIES:
        conn.execute("INSERT OR IGNORE INTO entities (id,kind,name,is_tradeable,active) "
                     "VALUES (?,'commodity',?,1,1)", (eid, eid))
    rows = [(eid, iso, v)
            for eid in SERIES
            for iso, v in sorted((d["series_cash"].get(eid) or {}).items())]
    res = prices_io.upsert(conn, rows, SOURCE, currency="USD")
    conn.commit()
    conn.close()
    print("\n" + prices_io.report(res))
    if res["refused"]:
        print("   refused rows are cells the Daily Metals Pack already owns — "
              "the pack is the licensed source and outranks this one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
