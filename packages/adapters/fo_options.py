"""Stock-options layer of the NSE F&O bhavcopy: OI-weighted strikes + ATM IV.

PM, 2026-09-30, handing over a vault-era "Options OI" setup guide: *"can we set
up something similar in our positioning tab? Let the futures remain there,
just add the weighted average graph. Include IV %ile."* So this is the data
half of that, and ONLY the parts asked for — the strike-level OI chart of the
guide is not built yet.

The source is the SAME zip fo_bhavcopy.py already downloads for futures
(FinInstrmTp 'STO' rows in the UDiFF file). Nothing on this path reaches
`prices` or `pillar_scores`: options are a positioning read, a timing veto,
never a fundamental call — same standing as the futures OI beside it.

One row per (symbol, date, live expiry) in `fo_options`:

  call_oi / put_oi        total open calls / puts, SHARES (OpnIntrst is shares)
  call_wtd / put_wtd      Σ(strike × OI) / Σ OI — the "weighted average range"
  fut                     that expiry's futures settle; spot, lot ride along
  atm_strike, iv_*        Black-76 implied vol at the strike nearest `fut`

WHY IV IS COMPUTED ONLY FROM LEGS THAT TRADED. Measured on the first look at
the file (HINDALCO, 29-Sep-2026): the Nov-23 960 call has OI 0, volume 0 and
ClsPric 134.55, against 16.95 for the TRADED Oct 980 call. NSE carries an
untraded option's close forward, so its premium is a stale number that solves
to a plausible-looking and entirely fictitious volatility — the silent-bugs
shape. A leg with TtlTradgVol == 0 therefore gets NO IV; the ATM strike is the
nearest one (within ATM_BAND of fut) where at least one leg traded, and
atm_iv is the mean of whichever legs qualified (n in iv_legs). Absent, not
guessed.

Contracts expiring ON the session date are dropped: they are settled, not open
(the guide's rule, and the reason XpryDt <= date is skipped).

Usage:
    python packages/adapters/fo_options.py --load --days 400   # backfill
    python packages/adapters/fo_options.py --load              # top up
    python packages/adapters/fo_options.py --status
    python packages/adapters/fo_options.py --selftest
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import math
import sqlite3
import sys
import time
import zipfile

import fo_bhavcopy as fb

RATE = 0.065        # discount rate for Black-76, the guide's constant
ATM_BAND = 0.05     # ATM strike must sit within 5% of that expiry's futures

DDL = """
CREATE TABLE IF NOT EXISTS fo_options (
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    expiry     TEXT NOT NULL,
    fut        REAL,                 -- this expiry's futures settle (NULL: no future listed)
    spot       REAL,
    lot        REAL,
    call_oi    REAL NOT NULL,        -- shares
    call_wtd   REAL,                 -- Σ strike×call OI / Σ call OI; NULL if no open calls
    put_oi     REAL NOT NULL,
    put_wtd    REAL,
    atm_strike REAL,
    iv_call    REAL,                 -- decimal (0.25 = 25%); NULL = leg did not trade
    iv_put     REAL,
    atm_iv     REAL,
    iv_legs    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, date, expiry),
    CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (expiry GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
) STRICT;
"""


# ------------------------------------------------------------ Black-76 ----

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def b76(F: float, K: float, T: float, s: float, call: bool) -> float:
    sq = s * math.sqrt(T)
    d1 = (math.log(F / K) + s * s * T / 2) / sq
    d2 = d1 - sq
    df = math.exp(-RATE * T)
    if call:
        return df * (F * _ncdf(d1) - K * _ncdf(d2))
    return df * (K * _ncdf(-d2) - F * _ncdf(-d1))


def implied_vol(price: float, F: float, K: float, T: float,
                call: bool) -> float | None:
    """Bisection on sigma in [0.5%, 400%]. None when the premium sits at or
    below intrinsic (no time value to solve for) or above the 400% price."""
    if not (price > 0 and F > 0 and K > 0 and T > 0):
        return None
    intrinsic = math.exp(-RATE * T) * max(0.0, (F - K) if call else (K - F))
    if not price > intrinsic + 0.01:
        return None
    lo, hi = 0.005, 4.0
    if b76(F, K, T, hi, call) < price:
        return None
    for _ in range(60):
        m = (lo + hi) / 2
        if b76(F, K, T, m, call) > price:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2


# --------------------------------------------------------------- parse ----

def parse_options(raw: bytes, iso: str) -> list[dict]:
    """Aggregate one day's STO rows per (symbol, live expiry)."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    rd = csv.DictReader(io.TextIOWrapper(zf.open(zf.namelist()[0]),
                                         encoding="utf-8"))
    fut: dict[tuple, float] = {}
    agg: dict[tuple, dict] = {}
    for r in rd:
        tp = r.get("FinInstrmTp")
        if tp == "STF":
            try:
                fut[(r["TckrSymb"], r["XpryDt"])] = float(r["SttlmPric"])
            except ValueError:
                pass
            continue
        if tp != "STO":
            continue
        exp = r["XpryDt"]
        if exp <= iso:                     # expiring today: settled, not open
            continue
        key = (r["TckrSymb"], exp)
        a = agg.get(key)
        if a is None:
            a = agg[key] = {"co": 0.0, "cs": 0.0, "po": 0.0, "ps": 0.0,
                            "spot": float(r["UndrlygPric"] or 0) or None,
                            "lot": float(r["NewBrdLotQty"] or 0) or None,
                            "k": {}}
        k = float(r["StrkPric"])
        oi = float(r["OpnIntrst"] or 0)
        call = r["OptnTp"] == "CE"
        if oi:
            if call:
                a["co"] += oi; a["cs"] += oi * k
            else:
                a["po"] += oi; a["ps"] += oi * k
        vol = float(r["TtlTradgVol"] or 0)
        if vol > 0:                        # only traded premiums feed IV
            leg = a["k"].setdefault(k, [None, None])
            leg[0 if call else 1] = float(r["ClsPric"] or 0)

    t0 = dt.date.fromisoformat(iso)
    out = []
    for (sym, exp), a in agg.items():
        if not (a["co"] or a["po"]):
            continue
        F = fut.get((sym, exp))
        row = {"symbol": sym, "date": iso, "expiry": exp, "fut": F,
               "spot": a["spot"], "lot": a["lot"],
               "call_oi": a["co"], "call_wtd": a["cs"] / a["co"] if a["co"] else None,
               "put_oi": a["po"], "put_wtd": a["ps"] / a["po"] if a["po"] else None,
               "atm_strike": None, "iv_call": None, "iv_put": None,
               "atm_iv": None, "iv_legs": 0}
        T = (dt.date.fromisoformat(exp) - t0).days / 365.0
        if F and T > 0 and a["k"]:
            for k in sorted(a["k"], key=lambda s: abs(s - F)):
                if abs(k / F - 1) > ATM_BAND:
                    break
                c, p = a["k"][k]
                ivc = implied_vol(c, F, k, T, True) if c is not None else None
                ivp = implied_vol(p, F, k, T, False) if p is not None else None
                legs = [v for v in (ivc, ivp) if v is not None]
                if legs:
                    row.update(atm_strike=k, iv_call=ivc, iv_put=ivp,
                               atm_iv=sum(legs) / len(legs), iv_legs=len(legs))
                    break
        out.append(row)
    return out


# ---------------------------------------------------------------- store ----

COLS = ("symbol", "date", "expiry", "fut", "spot", "lot", "call_oi",
        "call_wtd", "put_oi", "put_wtd", "atm_strike", "iv_call", "iv_put",
        "atm_iv", "iv_legs")


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def store(conn: sqlite3.Connection, raw: bytes, iso: str) -> int:
    """Called by fo_bhavcopy.load on the zip it already downloaded, so the
    daily run fetches each file once."""
    ensure(conn)
    rows = parse_options(raw, iso)
    conn.execute("DELETE FROM fo_options WHERE date=?", (iso,))
    conn.executemany(
        f"INSERT INTO fo_options ({','.join(COLS)}) VALUES "
        f"({','.join('?' * len(COLS))})",
        [tuple(r[c] for c in COLS) for r in rows])
    conn.commit()
    return len(rows)


def load(days_back: int = 10) -> None:
    conn = sqlite3.connect(fb.DB)
    ensure(conn)
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM fo_options")}
    today = dt.date.today()
    n = n_skip = 0
    for k in range(days_back, 0, -1):
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
        print(f"  {d}  {m} symbol-expiries", flush=True)
    conn.close()
    print(f"loaded {n} option days ({n_skip} dates had no session/file)")


def status() -> None:
    conn = sqlite3.connect(fb.DB)
    ensure(conn)
    r = conn.execute("SELECT COUNT(DISTINCT date), MIN(date), MAX(date), "
                     "COUNT(DISTINCT symbol), SUM(atm_iv IS NOT NULL), COUNT(*) "
                     "FROM fo_options").fetchone()
    print(f"  fo_options  {r[0]} days  {r[1]} .. {r[2]}  {r[3]} symbols  "
          f"IV on {r[4]} of {r[5]} symbol-expiries")
    conn.close()


# ------------------------------------------------------------- selftest ----

def selftest() -> int:
    fails = 0

    def ok(cond, msg):
        nonlocal fails
        print(("  ok   " if cond else "  FAIL ") + msg)
        fails += 0 if cond else 1

    # IV round-trips a known sigma on both legs (acceptance)
    for call in (True, False):
        px = b76(1000, 1020, 30 / 365, 0.28, call)
        iv = implied_vol(px, 1000, 1020, 30 / 365, call)
        ok(iv is not None and abs(iv - 0.28) < 1e-6,
           f"IV round-trip {'call' if call else 'put'} 28% -> {iv}")
    # rejection: premium at intrinsic has no time value to solve
    ok(implied_vol(0.0, 1000, 900, 0.1, True) is None, "zero premium -> None")
    ok(implied_vol(99.0, 1000, 900, 0.1, True) is None,
       "premium below intrinsic -> None")

    # a synthetic bhavcopy: weighted strikes, stale-leg gate, expiring-today drop
    hdr = ["FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp",
           "OpnIntrst", "ClsPric", "SttlmPric", "TtlTradgVol", "UndrlygPric",
           "NewBrdLotQty"]
    F, T = 1000.0, 28 / 365
    c1000 = round(b76(F, 1000, T, 0.30, True), 2)
    p1000 = round(b76(F, 1000, T, 0.30, False), 2)
    rows = [
        ["STF", "ABC", "2026-10-28", "", "", "0", "", "1000", "10", "998", "500"],
        ["STO", "ABC", "2026-10-28", "1100", "CE", "300", "5", "5", "4", "998", "500"],
        ["STO", "ABC", "2026-10-28", "1000", "CE", "100", str(c1000), "", "9", "998", "500"],
        ["STO", "ABC", "2026-10-28", "1000", "PE", "100", str(p1000), "", "9", "998", "500"],
        ["STO", "ABC", "2026-10-28", "900", "PE", "300", "4", "4", "3", "998", "500"],
        # expiring on the session date: must not appear at all
        ["STO", "ABC", "2026-09-30", "1000", "CE", "999", "1", "1", "5", "998", "500"],
        # untraded, stale absurd premium: must yield NO iv
        ["STF", "XYZ", "2026-10-28", "", "", "0", "", "500", "10", "500", "100"],
        ["STO", "XYZ", "2026-10-28", "500", "CE", "50", "134.55", "", "0", "500", "100"],
        ["STO", "XYZ", "2026-10-28", "500", "PE", "50", "130.00", "", "0", "500", "100"],
    ]
    buf = io.StringIO()
    w = csv.writer(buf); w.writerow(hdr); w.writerows(rows)
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        z.writestr("fo.csv", buf.getvalue())
    out = {(r["symbol"], r["expiry"]): r
           for r in parse_options(zb.getvalue(), "2026-09-30")}
    ok(("ABC", "2026-09-30") not in out, "contract expiring today dropped")
    a = out.get(("ABC", "2026-10-28"))
    ok(a and abs(a["call_wtd"] - (1100 * 300 + 1000 * 100) / 400) < 1e-9,
       f"call wtd strike = 1075 ({a and a['call_wtd']})")
    ok(a and abs(a["put_wtd"] - (900 * 300 + 1000 * 100) / 400) < 1e-9,
       f"put wtd strike = 925 ({a and a['put_wtd']})")
    ok(a and a["atm_strike"] == 1000 and a["iv_legs"] == 2
       and abs(a["atm_iv"] - 0.30) < 0.002,
       f"ATM IV 30% at 1000, both legs ({a and a['atm_iv']})")
    x = out.get(("XYZ", "2026-10-28"))
    ok(x and x["atm_iv"] is None and x["iv_legs"] == 0,
       "untraded stale premium yields no IV (absent, not guessed)")
    ok(x and x["call_wtd"] == 500, "untraded leg still counts toward OI weights")
    print(f"{'PASS' if not fails else 'FAIL'} ({fails} failures)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--days", type=int, default=10)
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
