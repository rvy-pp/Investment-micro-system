"""Stock-futures basis: each live contract's close against the cash close.

PM, 2026-10-25: *"In the daily tab, create a futures vs cash sub tab that
updates daily at day end for each stock in the coverage ... %premium/discount
(in red) for this month's future as well as the next and next to next if
available."* This is the data half; engine.fo_basis_view is the transport and
app.html's Overview "Futures vs cash" sub-tab the face.

The source is the SAME NSE F&O UDiFF zip fo_bhavcopy.py downloads (the 'STF'
rows), so the morning run fetches each file once. One row per (symbol, date,
live expiry) in `fo_basis`. Nothing here reaches `prices` or `pillar_scores`:
a basis is a carry read, never a fundamental call.

WHY CLOSE, NOT SETTLE. fo_options.fut is the SETTLEMENT price, and for a
contract that did not trade in the last half hour NSE sets settlement to a
THEORETICAL price (spot x carry). Measured on 23-Oct-2026: Kaynes, PGEL and
APL Apollo's Dec contracts all "printed" exactly +1.06% — the model rate,
not a market. ClsPric is the last traded price. Both are stored; the view
uses close and says when a contract is thin.

WHY VOLUME RIDES ALONG. The far month is a few lots. Ambuja's Dec on
23-Oct-2026 traded 4 contracts against 14,031 in Nov and printed BELOW Nov,
which read as a calendar inversion and was only a stale handful of trades
(checked the same day: no record date, no dividend before 29-Dec). A basis
on a contract nobody traded is a number nobody could deal at, so the view
greys any contract under THIN_CONTRACTS and blanks one with zero volume.

Contracts expiring ON the session date are dropped (fo_options' rule): at the
close they are settled, not open, and "this month" becomes the next one.

Day end: NSE publishes the zip in the evening (~18:00-19:00 IST), so unlike
fo_bhavcopy.load (which walks to YESTERDAY) `load` here includes TODAY. A
404 before publication is a skip, not an error; the morning refresh's
bhavcopy step fills anything the evening run missed.

Usage:
    python packages/adapters/fo_basis.py --load              # top up, incl. today
    python packages/adapters/fo_basis.py --load --days 30    # backfill
    python packages/adapters/fo_basis.py --status
    python packages/adapters/fo_basis.py --selftest
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import sqlite3
import sys
import time
import zipfile

import fo_bhavcopy as fb

THIN_CONTRACTS = 100    # under this many contracts traded the print is flagged thin
# PM, 2026-10-25: "add a glow up in the tab if there is any stock with a 3% or
# more premium, just so that I remember to look". RAW premium (the number the
# cell shows), not annualised. A thin or untraded print never lights it: the
# glow is a reminder to act, and a 4-lot print is not a price anyone can act on.
GLOW_PCT = 3.0

DDL = """
CREATE TABLE IF NOT EXISTS fo_basis (
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    expiry     TEXT NOT NULL,
    close      REAL,                 -- ClsPric, last traded; NULL if absent
    settle     REAL,                 -- SttlmPric; theoretical when untraded
    spot       REAL NOT NULL,        -- UndrlygPric, the cash close
    vol        REAL NOT NULL,        -- TtlTradgVol, CONTRACTS
    oi         REAL NOT NULL,        -- OpnIntrst, SHARES
    lot        REAL,
    PRIMARY KEY (symbol, date, expiry),
    CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (expiry GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
) STRICT;
"""

COLS = ("symbol", "date", "expiry", "close", "settle", "spot", "vol", "oi", "lot")


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse(raw: bytes, iso: str) -> list[dict]:
    zf = zipfile.ZipFile(io.BytesIO(raw))
    rd = csv.DictReader(io.TextIOWrapper(zf.open(zf.namelist()[0]), encoding="utf-8"))
    out = []
    for r in rd:
        if r.get("FinInstrmTp") != "STF":
            continue
        exp = r["XpryDt"]
        if exp <= iso:                       # expiring today: settled, not open
            continue
        spot = _f(r.get("UndrlygPric"))
        if spot is None:
            continue
        out.append({"symbol": r["TckrSymb"], "date": iso, "expiry": exp,
                    "close": _f(r.get("ClsPric")), "settle": _f(r.get("SttlmPric")),
                    "spot": spot, "vol": float(r.get("TtlTradgVol") or 0),
                    "oi": float(r.get("OpnIntrst") or 0),
                    "lot": _f(r.get("NewBrdLotQty"))})
    return out


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def store(conn: sqlite3.Connection, raw: bytes, iso: str) -> int:
    """Called by fo_bhavcopy.load on the zip it already downloaded."""
    ensure(conn)
    rows = parse(raw, iso)
    if not rows:
        return 0
    conn.execute("DELETE FROM fo_basis WHERE date=?", (iso,))
    conn.executemany(
        f"INSERT INTO fo_basis ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
        [tuple(r[c] for c in COLS) for r in rows])
    conn.commit()
    return len(rows)


def load(days_back: int = 7) -> None:
    conn = sqlite3.connect(fb.DB)
    ensure(conn)
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM fo_basis")}
    today = dt.date.today()
    n = n_skip = 0
    for k in range(days_back, -1, -1):       # -1: TODAY too, the day-end run
        d = today - dt.timedelta(days=k)
        if d.weekday() > 4 or d.isoformat() in have:
            continue
        try:
            raw = fb._get(fb.FO_URL.format(d=d), timeout=60)
        except Exception as e:
            print(f"  {d} fetch failed ({type(e).__name__}), skipped")
            raw = None
        time.sleep(fb.PAUSE)
        if raw is None:
            n_skip += 1
            continue
        m = store(conn, raw, d.isoformat())
        n += 1
        print(f"  {d}  {m} contracts", flush=True)
    conn.close()
    print(f"loaded {n} basis days ({n_skip} dates had no session/file yet)")


def status() -> None:
    conn = sqlite3.connect(fb.DB)
    ensure(conn)
    r = conn.execute("SELECT COUNT(DISTINCT date), MIN(date), MAX(date), "
                     "COUNT(DISTINCT symbol) FROM fo_basis").fetchone()
    print(f"  fo_basis  {r[0]} days  {r[1]} .. {r[2]}  {r[3]} symbols")
    conn.close()


# ------------------------------------------------------------------ view

def view(conn: sqlite3.Connection, roster: list[dict], date: str | None = None) -> dict:
    """Latest session's basis for each roster name, contracts in expiry order.

    roster: [{"id", "name", "sector", "symbol"}] — the caller's coverage list.
    A name with no futures that day is returned under `no_futures`, never
    dropped silently (a missing row would read as "no premium")."""
    ensure(conn)
    if date is None:
        date = conn.execute("SELECT MAX(date) FROM fo_basis").fetchone()[0]
    if date is None:
        return {"date": None, "expiries": [], "rows": [], "no_futures": [r["name"] for r in roster]}
    prev = conn.execute("SELECT MAX(date) FROM fo_basis WHERE date < ?", (date,)).fetchone()[0]
    d0 = dt.date.fromisoformat(date)
    exps = [r[0] for r in conn.execute(
        "SELECT DISTINCT expiry FROM fo_basis WHERE date=? ORDER BY expiry", (date,))][:3]
    rows, none = [], []
    for e in roster:
        got = {r[0]: r for r in conn.execute(
            "SELECT expiry, close, settle, spot, vol, oi FROM fo_basis "
            "WHERE symbol=? AND date=?", (e["symbol"], date))}
        if not got:
            none.append(e["name"])
            continue
        pv = {r[0]: r for r in conn.execute(
            "SELECT expiry, close, spot, vol FROM fo_basis WHERE symbol=? AND date=?",
            (e["symbol"], prev))} if prev else {}
        spot = next(iter(got.values()))[3]
        cells = []
        for x in exps:
            g = got.get(x)
            days = (dt.date.fromisoformat(x) - d0).days
            if g is None:
                cells.append(None)
                continue
            _, close, settle, sp, vol, oi = g
            # zero volume: the close is a carried print and the settle a model
            # price, so neither is a basis anyone could trade — blank, said so
            if not vol or close is None:
                cells.append({"expiry": x, "days": days, "pct": None, "vol": vol,
                              "oi": oi, "fut": None, "untraded": True})
                continue
            pct = (close / sp - 1) * 100
            p = pv.get(x)
            prev_pct = ((p[1] / p[2] - 1) * 100) if p and p[1] and p[2] and p[3] else None
            cells.append({"expiry": x, "days": days, "fut": close, "pct": pct,
                          "ann": pct * 365 / days if days > 0 else None,
                          "prev_pct": prev_pct, "vol": vol, "oi": oi,
                          "thin": vol < THIN_CONTRACTS, "untraded": False,
                          "theoretical_settle": abs((settle or close) - close) > 1e-9})
        rows.append({"id": e["id"], "name": e["name"], "sector": e["sector"],
                     "symbol": e["symbol"], "spot": spot, "cells": cells})
    alerts = [{"symbol": r["symbol"], "expiry": c["expiry"], "pct": c["pct"]}
              for r in rows for c in r["cells"]
              if c and c.get("pct") is not None and not c["thin"]
              and c["pct"] >= GLOW_PCT]
    return {"date": date, "prev_date": prev, "thin_contracts": THIN_CONTRACTS,
            "glow_pct": GLOW_PCT, "alerts": alerts,
            "expiries": [{"expiry": x, "days": (dt.date.fromisoformat(x) - d0).days}
                         for x in exps],
            "rows": rows, "no_futures": none}


# ------------------------------------------------------------- selftest

def selftest() -> int:
    fails = 0

    def ok(cond, msg):
        nonlocal fails
        print(("  ok   " if cond else "  FAIL ") + msg)
        fails += 0 if cond else 1

    hdr = ["FinInstrmTp", "TckrSymb", "XpryDt", "ClsPric", "SttlmPric",
           "UndrlygPric", "TtlTradgVol", "OpnIntrst", "NewBrdLotQty", "OptnTp"]
    rows = [
        ["STF", "ABC", "2026-10-27", "101", "101", "100", "5000", "9000", "100", ""],
        ["STF", "ABC", "2026-11-23", "99", "99", "100", "40", "100", "100", ""],    # discount, thin
        ["STF", "ABC", "2026-12-29", "130", "101.2", "100", "0", "0", "100", ""],   # untraded, stale close
        ["STF", "ABC", "2026-10-23", "100", "100", "100", "900", "0", "100", ""],   # expires today
        ["STO", "ABC", "2026-10-27", "5", "5", "100", "10", "10", "100", "CE"],     # option, ignored
    ]
    buf = io.StringIO()
    w = csv.writer(buf); w.writerow(hdr); w.writerows(rows)
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("fo.csv", buf.getvalue())
    p = parse(zb.getvalue(), "2026-10-23")
    ok(len(p) == 3, f"three live futures parsed, option + expiring-today dropped ({len(p)})")
    ok(all(r["expiry"] > "2026-10-23" for r in p), "no contract expiring on the session date")

    conn = sqlite3.connect(":memory:")
    ensure(conn)
    store(conn, zb.getvalue(), "2026-10-23")
    v = view(conn, [{"id": "abc", "name": "Abc", "sector": "x", "symbol": "ABC"},
                    {"id": "zzz", "name": "Zzz", "sector": "x", "symbol": "ZZZ"}])
    ok(v["date"] == "2026-10-23" and len(v["expiries"]) == 3, "latest date, three expiries")
    c = v["rows"][0]["cells"] if v["rows"] else [None] * 3
    ok(c[0] and abs(c[0]["pct"] - 1.0) < 1e-9 and not c[0]["thin"],
       f"near premium +1.00% from CLOSE vs spot ({c[0] and c[0]['pct']})")
    ok(c[0] and abs(c[0]["ann"] - 1.0 * 365 / 4) < 1e-9, "annualised on calendar days to expiry")
    ok(c[1] and c[1]["pct"] < 0 and c[1]["thin"], "next month: discount, flagged thin (<100 contracts)")
    ok(c[2] and c[2]["untraded"] and c[2]["pct"] is None,
       "zero-volume far month is BLANK — its stale 130 close is never read as +30%")
    ok(v["no_futures"] == ["Zzz"], "a roster name with no futures is listed, not dropped")
    ok(v["alerts"] == [], "no glow at +1.00% (below the 3% line)")
    # acceptance AND rejection for the glow: +3.0% traded lights it; a thin
    # +5% and an untraded +30% close must not
    rows2 = [["STF", "ABC", "2026-10-27", "103", "103", "100", "5000", "9000", "100", ""],
             ["STF", "ABC", "2026-11-23", "105", "105", "100", "40", "100", "100", ""],
             ["STF", "ABC", "2026-12-29", "130", "101.2", "100", "0", "0", "100", ""]]
    buf = io.StringIO()
    w = csv.writer(buf); w.writerow(hdr); w.writerows(rows2)
    zb2 = io.BytesIO()
    with zipfile.ZipFile(zb2, "w") as z:
        z.writestr("fo.csv", buf.getvalue())
    store(conn, zb2.getvalue(), "2026-10-23")
    a = view(conn, [{"id": "abc", "name": "Abc", "sector": "x", "symbol": "ABC"}])["alerts"]
    ok(len(a) == 1 and a[0]["expiry"] == "2026-10-27" and abs(a[0]["pct"] - 3.0) < 1e-9,
       f"glow: +3.0% traded lights, thin +5% and untraded +30% do not ({a})")
    print(f"{'PASS' if not fails else 'FAIL'} ({fails} failures)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.load:
        load(a.days)
    if a.status or not a.load:
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
