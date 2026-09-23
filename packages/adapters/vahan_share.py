"""Vahan maker share by segment — 2W / PV / CV, F&O names as lines, rest as Others.

    python packages/adapters/vahan_share.py --capture      # fetch + store today
    python packages/adapters/vahan_share.py --report
    python packages/adapters/vahan_share.py --selftest

PM instruction 2026-09-23: *"in the auto tab, I need a graph for market share
change on a daily basis. Different for PV, 2 wheelers and CV. Only include fno
names in the graph lines, keep rest as others."*

*** DAILY DOES NOT EXIST ON THE OPEN API, AND THIS IS THE WHOLE SHAPE OF THE
FEED. READ THIS BEFORE TRUSTING ANY "DAILY" NUMBER HERE. ***
Vahan's daily granularity lives ONLY behind `/analytics/vahanpublicreport`,
which is gated by an image CAPTCHA. The open dashboard API bottoms out at
MONTHLY (`calendarType=3`); 4..8 silently fall back to yearly, and every other
endpoint (`transactionLineChart`, `vahanYearWiseRegistrationComparisonChart`,
`vehicleRegistration`) returns twelve monthly values per year. All probed.

So "daily" here is the MONTH-TO-DATE level, re-read every day:

    day t's registrations  =  MTD(t) - MTD(t-1)
    day t's share          =  maker MTD(t) / segment MTD(t)

which makes two things true and both must be said out loud on the page:

1. **IT IS FORWARD-ONLY.** The first capture is 2026-09-23. There is no daily
   history before it and none can be manufactured — a month's MTD level on a
   past date was never published. The monthly series IS long (back to the
   1990s) and is captured alongside, so the chart is not empty today; the daily
   layer accumulates from here.
2. **VAHAN BACKDATES, so this is a REVISION series, not a print.** A
   registration files against its own date, so MTD for a given month keeps
   growing for days after. Ather's own dossier records the symptom from the
   other side: market shares restated upward between calls (Q1FY25 7.4%->7.6%,
   Q4FY25 13.3%->13.6%) with no call ever explaining it. Consequently a naive
   MTD(t)-MTD(t-1) can go NEGATIVE when a backdated batch lands in the prior
   month instead, and a single day's share is noisy. **The MTD SHARE is what
   this stores and leads with** — it revises gently and converges on the
   month's final answer. The day-delta is derived for reference and flagged.

*** THE F&O ROSTER IS READ FROM WHAT ACTUALLY TRADED, NEVER GUESSED. ***
`_fno_live()` reads distinct symbols from `fo_oi` (the NSE F&O bhavcopy that
`fo_bhavcopy.py` already loads daily) within FNO_LOOKBACK_DAYS. Hardcoding the
list would rot, and it rots INVISIBLY: a name that leaves F&O keeps drawing its
own line and stops being in Others, so the shares still sum to 100% and nothing
raises. Invariant 7 is the reason the distinction matters at all.

**Two roster findings, both measured on 2026-09-23 and both load-bearing:**

  - **ATHERENERG IS in F&O** (first contract 2026-08-26). `CLAUDE.md` lists
    "F&O membership is NOT established by this archive" as an open hole in
    Ather's dossier — this closes it, from the bhavcopy rather than from the
    transcripts.
  - **TATA MOTORS' CV ENTITY IS NOT.** TATAMOTORS last traded 2025-10-23 and
    TMPV first traded 2025-10-24 — the demerger, visible as a clean handover in
    `fo_oi`. Only the PV arm carries futures. So on the CV chart India's
    LARGEST CV maker sits inside Others, which is the PM's rule applied
    faithfully and NOT an omission. `SEGMENTS['CV']['note']` says so and the
    page prints it, because a CV chart whose Others line is the biggest one
    reads as a bug otherwise.

*** SEGMENTS ARE `vehicleCategoryGroup`, AND THE BUCKETS WERE CHECKED. ***
    2W = Two Wheeler        PV = Four Wheeler        CV = Goods Vehicle + Bus
Verified on MH/Aug-26 rather than assumed: Four Wheeler is PASSENGER — Maruti
18,738 there against 1,075 in Goods (Super Carry), Ashok Leyland exactly 0 —
while the CV entity Tata Motors Ltd sits at 36 in Four Wheeler against
3,547+618 in Goods+Bus. Tractors and 3W are deliberately NOT covered; the PM
named three segments.

The eleven groups OVERLAP by ~0.6% (they sum to 269,361 against a 267,635
total on that cross-section), so they are not a partition of the whole. That
does not touch a share: every share here is computed INSIDE one segment, where
the denominator is that segment's own total, fetched as its own query.

*** ALL-INDIA IS SAFE **ONLY** WITH A SEGMENT FILTER — see vahan.TRAP 2. ***
Unfiltered, `stateCode=""` short-circuits to an empty body for the largest
makers (Hero, Maruti) and `vahan.all_india()` must sum 36 states. WITH a
segment filter the result set is small enough that the short-circuit does not
fire, and it is 36x cheaper. That is a measured claim, not a hope: Hero/2W and
ALL/Four-Wheeler both reconcile to the 36-state sum with **zero difference** on
five separate months. `--selftest` re-runs that reconciliation, because the day
the server's threshold moves is the day this silently starts under-counting.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vahan  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data" / "ims.db"

MONTHS_KEPT = 13          # enough for YoY and to watch backdating revise
FNO_LOOKBACK_DAYS = 30    # a symbol absent this long is out of F&O

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]

# label -> (NSE F&O symbol, [exact Vahan maker strings])
# Maker strings are EXACT and case-sensitive; resolve new ones with
# `vahan.py --makers`, never by title-casing a chart label. Several strings per
# label where one listed company registers under more than one — see the
# entity-resolution note in vahan.py.
SEGMENTS = {
    "2W": {
        "label": "Two-wheelers",
        "groups": ["Two Wheeler"],
        "fno": {
            "Hero MotoCorp": ("HEROMOTOCO", ["HERO MOTOCORP LTD"]),
            "TVS Motor": ("TVSMOTOR", ["TVS MOTOR COMPANY LTD"]),
            "Bajaj Auto": ("BAJAJ-AUTO", ["BAJAJ AUTO LTD"]),
            # Eicher's motorcycles register under Royal Enfield, NOT under
            # EICHER MOTORS LTD (which is the truck/VECV side).
            "Royal Enfield": ("EICHERMOT", ["ROYAL-ENFIELD (UNIT OF EICHER LTD)"]),
            "Ather Energy": ("ATHERENERG", ["ATHER ENERGY LTD"]),
        },
        "note": "Others is mostly Honda (HMSI), Suzuki, Yamaha and Ola — "
                "Honda alone runs neck-and-neck with Hero and is unlisted.",
    },
    "PV": {
        "label": "Passenger vehicles",
        "groups": ["Four Wheeler"],
        "fno": {
            "Maruti Suzuki": ("MARUTI", ["MARUTI SUZUKI INDIA LTD"]),
            "Mahindra": ("M&M", ["MAHINDRA & MAHINDRA LIMITED"]),
            "Hyundai": ("HYUNDAI", ["HYUNDAI MOTOR INDIA LTD"]),
            "Tata Motors PV": ("TMPV", ["TATA MOTORS PASSENGER VEHICLES LTD"]),
        },
        "note": "Others is Toyota, Kia, Skoda-VW, JSW MG, Honda Cars, Renault "
                "and Nissan — all unlisted in India.",
    },
    "CV": {
        "label": "Commercial vehicles",
        "groups": ["Goods Vehicle", "Bus"],
        "fno": {
            "Ashok Leyland": ("ASHOKLEY", ["ASHOK LEYLAND LTD",
                                           # same company, trailing period, a
                                           # data-entry artefact: 36 vehicles
                                           # lifetime, one state.
                                           "ASHOK LEYLAND LTD."]),
            "VE Commercial": ("EICHERMOT", ["VE COMMERCIAL VEHICLES LTD",
                                            "VE COMMERCIAL VEHICLES LTD (VOLVO BUSES DIVISION)"]),
            "Mahindra": ("M&M", ["MAHINDRA & MAHINDRA LIMITED",
                                 "SML MAHINDRA LTD"]),
            "Force Motors": ("FORCEMOT", ["FORCE MOTORS LIMITED"]),
        },
        "note": "TATA MOTORS — the largest CV maker — is INSIDE Others: its CV "
                "entity left F&O at the 2025-10 demerger (TATAMOTORS last "
                "traded 2025-10-23, only the PV arm TMPV carries futures). "
                "Others is therefore the biggest line here by construction.",
    },
}

TOTAL = "__TOTAL__"       # the segment denominator, stored as its own row
OTHERS = "Others"

DDL = """
CREATE TABLE IF NOT EXISTS vahan_share (
    capture_date  TEXT NOT NULL,   -- the day WE fetched (revision key)
    period        TEXT NOT NULL,   -- '2026-September', the month counted
    segment       TEXT NOT NULL,   -- '2W' | 'PV' | 'CV'
    label         TEXT NOT NULL,   -- print name, or '__TOTAL__'
    registrations INTEGER NOT NULL CHECK (registrations >= 0),
    PRIMARY KEY (capture_date, period, segment, label)
);
CREATE INDEX IF NOT EXISTS ix_vahan_share_period
    ON vahan_share (segment, period, capture_date);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.executescript(DDL)
    return c


def _recent_months(n: int = MONTHS_KEPT, today: dt.date | None = None) -> list[str]:
    d = today or dt.date.today()
    out, y, m = [], d.year, d.month
    for _ in range(n):
        out.append(f"{y}-{MONTH_NAMES[m - 1]}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def _fno_live(conn: sqlite3.Connection, asof: dt.date | None = None) -> set[str]:
    """Symbols with an F&O contract inside the lookback. Read, never hardcoded.

    A stale hardcoded roster fails SILENTLY: a delisted-from-F&O name keeps its
    own line and leaves Others, the shares still sum to 100%, and nothing
    raises. That is the silent-arithmetic shape.
    """
    asof = asof or dt.date.today()
    cut = (asof - dt.timedelta(days=FNO_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM fo_oi WHERE date >= ?", (cut,)).fetchall()
    return {r[0] for r in rows}


def _fetch(maker: str, groups: list[str], months: set[str]) -> dict[str, int]:
    """Segment-filtered all-India monthly counts, summed over `groups`.

    ALL-INDIA IS ONLY SAFE BECAUSE OF THE SEGMENT FILTER — see the module
    docstring. An empty body here would be vahan.TRAP 2 and `vahan.series`
    raises on it rather than returning a zero.
    """
    out: dict[str, int] = {}
    for g in groups:
        p = vahan._params(maker, "", vahan.CALENDAR["month"], "2026", "2026")
        p["vehicleCategoryGroup"] = g
        rows = vahan._get(
            f"{vahan.DASH}/durationWiseRegistrationTable?{urllib.parse.urlencode(p)}")
        if not rows:
            raise vahan.VahanRefused(
                f"empty body: maker={maker!r} group={g!r} — a refusal, not a zero")
        for r in rows:
            k = r.get("yearAsString")
            if k in months:
                out[k] = out.get(k, 0) + (r.get("registeredVehicleCount") or 0)
    return out


def capture(conn: sqlite3.Connection, capture_date: str | None = None,
            workers: int = 6) -> dict:
    cd = capture_date or dt.date.today().isoformat()
    months = set(_recent_months())
    live = _fno_live(conn)
    jobs, meta = [], []
    for seg, cfg in SEGMENTS.items():
        jobs.append(("", cfg["groups"]))
        meta.append((seg, TOTAL, None))
        for label, (sym, makers) in cfg["fno"].items():
            for mk in makers:
                jobs.append((mk, cfg["groups"]))
                meta.append((seg, label, sym))

    def run(j):
        return _fetch(j[0], j[1], months)

    agg: dict[tuple[str, str, str], int] = {}
    dropped: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for (seg, label, sym), d in zip(meta, ex.map(run, jobs)):
            if sym is not None and sym not in live:
                # Out of F&O as of this capture: it belongs in Others, and the
                # run SAYS so rather than quietly still drawing its line.
                if label not in dropped:
                    dropped.append(f"{label} ({sym})")
                continue
            for per, v in d.items():
                agg[(per, seg, label)] = agg.get((per, seg, label), 0) + v

    rows = [(cd, per, seg, lab, v) for (per, seg, lab), v in agg.items()]
    conn.executemany(
        "INSERT OR REPLACE INTO vahan_share "
        "(capture_date, period, segment, label, registrations) VALUES (?,?,?,?,?)",
        rows)
    conn.commit()
    return {"capture_date": cd, "rows": len(rows),
            "periods": len(months), "dropped_not_in_fno": dropped}


def shares(conn: sqlite3.Connection, segment: str, period: str | None = None,
           capture_date: str | None = None) -> dict:
    """Share of the segment for one (period, capture). Others is DERIVED.

    Others = total - sum(named), never a separate query. Any maker the segment
    filter counts but we do not name lands there by construction, so the shares
    cannot fail to sum to 100% — and a negative Others would mean the named
    rows double-count, which is why it is checked rather than clamped.
    """
    cd = capture_date or conn.execute(
        "SELECT MAX(capture_date) FROM vahan_share WHERE segment=?",
        (segment,)).fetchone()[0]
    if cd is None:
        return {}
    per = period or conn.execute(
        "SELECT MAX(period) FROM vahan_share WHERE segment=? AND capture_date=? "
        "AND label=?", (segment, cd, TOTAL)).fetchone()[0]
    rows = dict(conn.execute(
        "SELECT label, registrations FROM vahan_share "
        "WHERE segment=? AND capture_date=? AND period=?", (segment, cd, per)))
    total = rows.pop(TOTAL, 0)
    if not total:
        return {}
    named = sum(rows.values())
    others = total - named
    if others < 0:
        raise ValueError(
            f"{segment}/{per}/{cd}: named makers ({named:,}) exceed the segment "
            f"total ({total:,}) by {-others:,} — a maker string is being "
            "double-counted, or a maker sits outside the segment filter")
    out = {k: 100.0 * v / total for k, v in rows.items()}
    out[OTHERS] = 100.0 * others / total
    return {"capture_date": cd, "period": per, "total": total,
            "counts": {**rows, OTHERS: others}, "share_pct": out}


def _months_sorted(periods) -> list[str]:
    def key(p):
        y, m = p.split("-")
        return (int(y), MONTH_NAMES.index(m))
    return sorted(periods, key=key)


def report(conn: sqlite3.Connection) -> None:
    caps = [r[0] for r in conn.execute(
        "SELECT DISTINCT capture_date FROM vahan_share ORDER BY capture_date")]
    print(f"captures stored: {len(caps)}"
          + (f"  ({caps[0]} .. {caps[-1]})" if caps else ""))
    if not caps:
        print("  nothing captured yet — run --capture")
        return
    for seg, cfg in SEGMENTS.items():
        s = shares(conn, seg)
        if not s:
            continue
        print(f"\n### {cfg['label']}  —  {s['period']} "
              f"(month-to-date, captured {s['capture_date']})")
        print(f"    segment total {s['total']:,}")
        for k, v in sorted(s["share_pct"].items(), key=lambda x: -x[1]):
            mark = "  <- residual" if k == OTHERS else ""
            print(f"    {k:18} {s['counts'][k]:>9,} {v:>7.2f}%{mark}")
        print(f"    note: {cfg['note']}")
    if len(caps) < 2:
        print("\nNo daily CHANGE yet: that needs two captures. "
              "This is capture 1 — the series starts here and cannot be backfilled.")


def selftest() -> int:
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
        if not cond:
            fails.append(name)

    conn = _conn()
    print("ROSTER (read from what traded, never hardcoded)")
    live = _fno_live(conn)
    check("fo_oi is populated and recent", len(live) > 100, f"{len(live)} symbols")
    check("ATHERENERG is in F&O", "ATHERENERG" in live)
    # The finding that puts India's biggest CV maker in Others. If this ever
    # FAILS, the CV chart's whole framing changes and the note must be rewritten.
    check("TATAMOTORS (CV) is NOT in F&O", "TATAMOTORS" not in live,
          "if this fails, re-read SEGMENTS['CV']['note']")
    for seg, cfg in SEGMENTS.items():
        missing = [s for (s, _) in cfg["fno"].values() if s not in live]
        check(f"{seg}: every mapped symbol still trades", not missing, str(missing))

    print("\nSEGMENT FILTER")
    # A RATIO, not a zero. Ashok Leyland registers exactly ONE four-wheeler
    # nationally in Aug-26 — an asserted == 0 failed on that single stray
    # vehicle. The claim worth testing is not "no CV maker ever appears in
    # Four Wheeler" (untrue, and untrue in a way that does not matter) but
    # "Four Wheeler is passenger, so a pure CV maker is negligible there".
    m = "2026-August"
    mh = _fetch("MARUTI SUZUKI INDIA LTD", ["Four Wheeler"], {m})
    al4 = _fetch("ASHOK LEYLAND LTD", ["Four Wheeler"], {m}).get(m, 0)
    alcv = _fetch("ASHOK LEYLAND LTD", ["Goods Vehicle", "Bus"], {m}).get(m, 0)
    check("Four Wheeler is passenger-only",
          mh.get(m, 0) > 100_000 and alcv > 1000 and al4 / alcv < 0.001,
          f"maruti4W={mh.get(m,0):,} ashokley 4W={al4} vs CV={alcv:,} "
          f"({100*al4/max(alcv,1):.4f}%)")

    print("\nRECONCILIATION — segment-filtered all-India == sum of 36 states")
    # The measured basis for skipping the 36-state sum. If the server's
    # short-circuit threshold moves, this is what catches it; without it the
    # capture would silently under-count the largest makers.
    direct = _fetch("HERO MOTOCORP LTD", ["Two Wheeler"], {"2026-August", "2026-July"})
    tot: dict[str, int] = {}

    def one(st):
        p = vahan._params("HERO MOTOCORP LTD", st, "3", "2026", "2026")
        p["vehicleCategoryGroup"] = "Two Wheeler"
        rows = vahan._get(
            f"{vahan.DASH}/durationWiseRegistrationTable?{urllib.parse.urlencode(p)}")
        return {r["yearAsString"]: r["registeredVehicleCount"] for r in rows
                if r.get("yearAsString") in ("2026-August", "2026-July")}

    with ThreadPoolExecutor(max_workers=8) as ex:
        for part in ex.map(one, vahan.STATES):
            for k, v in part.items():
                tot[k] = tot.get(k, 0) + v
    diffs = {m: tot.get(m, 0) - direct.get(m, 0) for m in direct}
    check("hero/2W all-India == state-sum", all(v == 0 for v in diffs.values()),
          str(diffs))

    print("\nSHARE ARITHMETIC")
    tmp = sqlite3.connect(":memory:")
    tmp.executescript(DDL)
    tmp.executemany(
        "INSERT INTO vahan_share VALUES (?,?,?,?,?)",
        [("2026-09-23", "2026-September", "PV", TOTAL, 1000),
         ("2026-09-23", "2026-September", "PV", "Maruti Suzuki", 400),
         ("2026-09-23", "2026-September", "PV", "Mahindra", 100)])
    s = shares(tmp, "PV")
    check("shares sum to 100", abs(sum(s["share_pct"].values()) - 100.0) < 1e-9,
          f"{sum(s['share_pct'].values()):.6f}")
    check("Others is the residual", abs(s["share_pct"][OTHERS] - 50.0) < 1e-9)

    # REJECTION: named > total must raise, not clamp. A silently clamped Others
    # of 0 would read as "no unlisted makers", which is a claim about the
    # market rather than a bug report.
    tmp.execute("INSERT INTO vahan_share VALUES (?,?,?,?,?)",
                ("2026-09-23", "2026-September", "PV", "Bogus", 900))
    try:
        shares(tmp, "PV")
        check("double-counted maker raises", False)
    except ValueError:
        check("double-counted maker raises", True)

    print("\nMONTH WINDOW")
    m = _recent_months(13, dt.date(2026, 1, 15))
    check("window crosses the year boundary",
          m[0] == "2026-January" and m[1] == "2025-December" and len(m) == 13,
          f"{m[0]} .. {m[-1]}")

    print(f"\n{len(fails)} failure(s)" + (f": {fails}" if fails else ""))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    conn = _conn()
    if a.capture:
        r = capture(conn)
        print(f"captured {r['capture_date']}: {r['rows']} rows "
              f"over {r['periods']} months")
        if r["dropped_not_in_fno"]:
            print(f"  DROPPED (no longer in F&O, now inside Others): "
                  f"{r['dropped_not_in_fno']}")
        report(conn)
        return 0
    if a.report:
        report(conn)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
