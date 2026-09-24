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
import pathlib
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


# ---------------------------------------------------------------------------
# MONTH SHAPE — the intra-month accrual curve, and the forecast built on it.
#
# THE QUESTION: given registrations from the 1st to today, what will the month
# finish at? The naive answer is MTD/days * days_in_month, and it is wrong in
# two separate ways that pull in opposite directions.
#
# 1. CALENDAR DAYS ARE THE WRONG UNIT. RTOs shut on Sunday. Measured on Hero in
#    September 2026: days 20-23 ran 13,466/CALENDAR day against 14,188 over
#    days 1-19, which reads as a slowdown. Per WORKING day the same window is
#    17,955 against 15,857 — a 13% ACCELERATION. The entire apparent slowdown
#    was one Sunday. Always work in working days; the SIGN of the trend depends
#    on it.
#
# 2. THE MONTH IS BACK-LOADED, BY SEGMENT, AND MILDLY. Harvested from the
#    CAPTCHA-gated report builder (the files in data/staging/vahan/), as
#    observed_fraction / working_day_fraction, where 1.00 = no skew:
#
#        cut          2W      PV      CV
#        Jul-26 d15   0.995   0.927   1.006
#        Aug-26 d15   0.911   0.846   0.940
#        Aug-26 d24   0.983   0.912   0.983
#
#    PV is the most back-loaded in EVERY month — month-end dealer push shows up
#    in cars far more than in two-wheelers — which is why the skew is per
#    segment and not one market-wide number.
#
#    THE SKEW IS A CURVE, NOT A CONSTANT: it relaxes toward 1.0 as the cut-off
#    approaches month end, because by then the late surge has already been
#    counted. 2W goes 0.911 at d15 to 0.983 at d24. Applying a d15 factor on
#    day 24 over-forecasts, so cut_day is a KEY here and never averaged over.
#
# *** AND THE FESTIVE MONTHS ARE A DIFFERENT ANIMAL ENTIRELY. ***
# Sep-2025 at d24 reads 0.722 / 0.659 / 0.873 — far outside the normal-month
# range above. Navratri began 22 Sep 2025, so that month's last nine days were
# the festive ramp; October 2025 two-wheelers then printed +140.7% m/m. A
# forecast built on Sep-2025's shape said Sep-2026 would be +75% YoY against a
# market running +20-30% in July and August. THE FESTIVE EFFECT KEYS ON THE
# LUNAR CALENDAR, NOT THE GREGORIAN MONTH, so year-on-year anchoring — which
# cancels shape only when the shape repeats — BREAKS whenever the festival
# moves between months. skew() REFUSES such a month rather than averaging it
# into the normal-month fit, and names the one it dropped.
# ---------------------------------------------------------------------------

SHAPE_DDL = """
CREATE TABLE IF NOT EXISTS vahan_month_shape (
    period   TEXT NOT NULL,          -- '2026-August', the month measured
    cut_day  INTEGER NOT NULL,       -- counted from the 1st THROUGH this day
    segment  TEXT NOT NULL,
    label    TEXT NOT NULL,          -- print name, or '__TOTAL__'
    partial  INTEGER NOT NULL CHECK (partial >= 0),
    source   TEXT NOT NULL CHECK (length(source) > 0),  -- provenance
    PRIMARY KEY (period, cut_day, segment, label)
);
"""

# Outside this band a month is treated as festive-distorted and excluded from
# the normal-month fit. Set from the observed normal months (0.846..1.006) with
# room either side; Sep-2025's 0.659 sits far outside it.
SKEW_NORMAL = (0.80, 1.15)


def _wd_fraction(period: str, cut: int) -> float:
    """Working days (ex-Sunday) through `cut`, over the month's total.

    Sundays only. Public holidays are NOT modelled and that is deliberate — a
    holiday calendar is one more thing to maintain and wrong the first year
    nobody updates it, the same call basket_index.py made about exchange
    holidays. Whatever a holiday does to the shape is absorbed into the
    measured skew instead, which is fitted from real months.
    """
    import calendar as _c
    y = int(period.split("-")[0])
    mn = list(_c.month_name).index(period.split("-")[1])
    dim = _c.monthrange(y, mn)[1]
    first = dt.date(y, mn, 1)

    def w(n):
        return sum(1 for i in range(n) if (first + dt.timedelta(i)).weekday() != 6)
    return w(min(cut, dim)) / w(dim)


def load_shape(conn: sqlite3.Connection, path) -> dict:
    """Parse one report-builder export into `vahan_month_shape`.

    A maker x vehicleCategoryGroup pivot whose TITLE ROW carries its own date
    range — which is read from the file rather than the filename, because the
    filename is whatever the browser called the download and four of these
    arrived as `..._all_records (2).xlsx`.

    Guarded: the file's own Total column must equal the sum of its segment
    cells on EVERY row. A shifted or mis-parsed column fails there rather than
    becoming a plausible skew factor nobody can re-check.
    """
    import re
    import openpyxl
    path = pathlib.Path(path)
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True)["Maker Report"]
    rows = list(ws.iter_rows(values_only=True))
    title = rows[0][0] or ""
    m = re.search(r"\((\d{2} \w{3} \d{4}) to (\d{2} \w{3} \d{4})\)", title)
    if not m:
        raise ValueError(f"{path.name}: no date range in the title row: {title!r}")
    a = dt.datetime.strptime(m.group(1), "%d %b %Y").date()
    b = dt.datetime.strptime(m.group(2), "%d %b %Y").date()
    if a.day != 1:
        raise ValueError(f"{path.name}: range starts {a}, not the 1st — the shape "
                         "fit needs a CUMULATIVE month-to-date")
    if (a.year, a.month) != (b.year, b.month):
        raise ValueError(f"{path.name}: range spans two months ({a}..{b})")
    period = f"{a.year}-{MONTH_NAMES[a.month - 1]}"

    hdr = rows[2]
    col = {h: i for i, h in enumerate(hdr) if h}
    body = [r for r in rows[3:] if r and r[0]]
    if "Total" not in col:
        raise ValueError(f"{path.name}: no Total column — wrong report?")
    bad = sum(1 for r in body
              if sum(r[i] or 0 for h, i in col.items() if h not in ("Maker", "Total"))
              != (r[col["Total"]] or 0))
    if bad:
        raise ValueError(f"{path.name}: cross-foot FAILED on {bad} of {len(body)} "
                         "rows — segments do not sum to the file's own Total")

    # PER-MAKER, not just per-segment. A segment-wide skew applied to every
    # maker in it CANCELS OUT of a share: forecast_share = (mtd/f)/(total/f) =
    # mtd/total, so the projected share would equal today's share exactly and a
    # forecast line on the share chart would be flat by construction. The
    # makers genuinely differ — Aug-2026 d24 runs Tata Motors PV 0.830 against
    # Maruti 0.912, a 10pp spread — and that difference IS the forecast.
    by_maker = {str(r[0]).strip().upper(): r for r in body}
    conn.executescript(SHAPE_DDL)
    out = {}
    for seg, cfg in SEGMENTS.items():
        missing = [g for g in cfg["groups"] if g not in col]
        if missing:
            raise ValueError(f"{path.name}: missing column(s) {missing} for {seg}")

        def cell(row):
            return sum(row[col[g]] or 0 for g in cfg["groups"])

        seg_part = sum(cell(r) for r in body)
        rows_out = [(TOTAL, seg_part)]
        named = 0
        for lab, (_sym, makers) in cfg["fno"].items():
            v = sum(cell(by_maker[m.upper()]) for m in makers if m.upper() in by_maker)
            named += v
            rows_out.append((lab, v))
        # Others is the RESIDUAL here too, exactly as in shares(), so the parts
        # always add back to the segment and no maker can be double counted.
        if named > seg_part:
            raise ValueError(f"{path.name}: {seg} named makers ({named:,}) exceed "
                             f"the segment total ({seg_part:,})")
        rows_out.append((OTHERS, seg_part - named))
        for lab, v in rows_out:
            conn.execute("INSERT OR REPLACE INTO vahan_month_shape "
                         "(period, cut_day, segment, label, partial, source) "
                         "VALUES (?,?,?,?,?,?)",
                         (period, b.day, seg, lab, v, path.name))
        out[seg] = seg_part
    conn.commit()
    return {"period": period, "cut_day": b.day, "makers": len(body),
            "segments": out, "source": path.name}


def _full_month(conn: sqlite3.Connection, segment: str, period: str,
                label: str = TOTAL) -> int:
    """The month's completed total. Only the partial-month NUMERATOR ever needs
    a human; this half is ordinary data.

    READ FROM `vahan_share` FIRST, network second. The daily capture already
    stores each segment's TOTAL for the last 13 months, so the number is
    usually sitting in the store — and fitting the skew over the network cost
    20-30 HTTP round trips on every page load, which is not a page. Falling
    back to the fetch keeps a month older than MONTHS_KEPT usable.

    REFUSES THE CURRENT MONTH. Its stored TOTAL is a month-to-DATE, and
    dividing a partial by a partial would produce a fraction near 1.0 — a
    "no skew" reading that is pure arithmetic, on the one month where the
    answer matters most.
    """
    if period == _recent_months(1)[0]:
        raise ValueError(f"{period} is the current month — its total is a "
                         "month-to-date and cannot be a denominator")
    if label == OTHERS:
        # Others is a residual on both sides; derive it rather than storing a
        # second version that could drift from the one shares() computes.
        tot = _full_month(conn, segment, period, TOTAL)
        named = sum(_full_month(conn, segment, period, k)
                    for k in SEGMENTS[segment]["fno"])
        return max(tot - named, 0)
    row = conn.execute(
        "SELECT SUM(registrations) FROM vahan_share WHERE segment=? AND period=? "
        "AND label=? AND capture_date=(SELECT MAX(capture_date) FROM vahan_share "
        "WHERE segment=? AND period=?)",
        (segment, period, label, segment, period)).fetchone()
    if row and row[0]:
        return int(row[0])
    if label != TOTAL:
        return 0
    return sum(_fetch("", [g], {period}).get(period, 0)
               for g in SEGMENTS[segment]["groups"])


def skew(conn: sqlite3.Connection, segment: str, cut_day: int,
         label: str = TOTAL) -> dict:
    """observed / working-day fraction, fitted at THIS cut day.

    Returns 1.0 with `basis: none` when no month was harvested at this cut. That
    is the honest fallback — it degrades to plain working-day extrapolation —
    and it is REPORTED rather than interpolated across a curve we hold two
    points on.
    """
    conn.executescript(SHAPE_DDL)
    rows = conn.execute(
        "SELECT period, partial FROM vahan_month_shape "
        "WHERE segment=? AND cut_day=? AND label=? ORDER BY period",
        (segment, cut_day, label)).fetchall()
    # FESTIVITY IS A PROPERTY OF THE MONTH, NOT OF ONE SEGMENT, so a period is
    # judged across ALL segments and excluded from every one of them together.
    # The first draft tested each segment on its own and kept Sep-2025 for CV
    # (0.873, inside the band) while dropping it for 2W (0.722) and PV (0.659) —
    # so CV's skew was fitted on a festive month and a normal one averaged
    # together, 0.928 instead of 0.983. Nothing about the number looked wrong.
    # Commercial buyers genuinely care less about an auspicious date than
    # retail ones do, which is exactly why the per-segment test passed and is
    # exactly why it must not be the test.
    festive = set()
    for per, in conn.execute(
            "SELECT DISTINCT period FROM vahan_month_shape WHERE cut_day=?",
            (cut_day,)).fetchall():
        for sg in SEGMENTS:
            row = conn.execute(
                "SELECT partial FROM vahan_month_shape "
                "WHERE segment=? AND cut_day=? AND period=? AND label=?",
                (sg, cut_day, per, TOTAL)).fetchone()
            if not row:
                continue
            t = _full_month(conn, sg, per)
            if not t:
                continue
            rr = (row[0] / t) / _wd_fraction(per, cut_day)
            if not (SKEW_NORMAL[0] <= rr <= SKEW_NORMAL[1]):
                festive.add(per)
                break

    used, dropped = [], []
    for period, partial in rows:
        tot = _full_month(conn, segment, period, label)
        if not tot or partial > tot:
            dropped.append(f"{period} (partial exceeds the month total)")
            continue
        r = (partial / tot) / _wd_fraction(period, cut_day)
        if period in festive:
            dropped.append(f"{period} ({r:.3f}, festive month)")
            continue
        used.append((period, r))
    if not used:
        return {"skew": 1.0, "basis": "none", "n": 0, "months": [],
                "dropped": dropped, "cut_day": cut_day, "label": label}
    return {"skew": sum(r for _, r in used) / len(used), "basis": "fitted",
            "n": len(used), "months": [f"{p} {r:.3f}" for p, r in used],
            "dropped": dropped, "cut_day": cut_day, "label": label}


def forecast(conn: sqlite3.Connection, segment: str,
             asof: dt.date | None = None) -> dict:
    """Project the current month from its month-to-date level."""
    asof = asof or dt.date.today()
    period = f"{asof.year}-{MONTH_NAMES[asof.month - 1]}"
    s = shares(conn, segment, period=period)
    if not s:
        return {"state": "no_data", "segment": segment, "period": period}
    fw = _wd_fraction(period, asof.day)
    seg_sk = skew(conn, segment, asof.day)
    by = {}
    for k, v in s["counts"].items():
        sk = skew(conn, segment, asof.day, k)
        # Fall back to the SEGMENT skew when a maker has no fit of its own —
        # better than 1.0, which would assert this maker alone is not
        # back-loaded when every one of its peers is.
        use = sk if sk["basis"] == "fitted" else seg_sk
        by[k] = {"mtd": v, "forecast": round(v / (fw * use["skew"])),
                 "skew": round(use["skew"], 4),
                 "skew_basis": "own" if sk["basis"] == "fitted"
                               else ("segment" if seg_sk["basis"] == "fitted" else "none")}
    # THE TOTAL IS THE SUM OF THE PARTS, never the segment fitted separately —
    # otherwise the forecast SHARES would not add to 100% and the chart would
    # be drawing an arithmetic impossibility.
    tot = sum(x["forecast"] for x in by.values())
    for k in by:
        by[k]["share_pct"] = round(100.0 * by[k]["forecast"] / tot, 3) if tot else None
    return {"state": "live", "segment": segment, "period": period,
            "capture_date": s["capture_date"], "asof_day": asof.day,
            "month_end": _month_end(period),
            "wd_fraction": round(fw, 4), "skew": round(seg_sk["skew"], 4),
            "skew_basis": seg_sk["basis"], "skew_n": seg_sk["n"],
            "skew_months": seg_sk["months"], "skew_dropped": seg_sk["dropped"],
            "fraction": round(fw * seg_sk["skew"], 4), "mtd_total": s["total"],
            "forecast_total": tot, "by_label": by}


def _month_end(period: str) -> str:
    import calendar as _c
    y = int(period.split("-")[0])
    mn = list(_c.month_name).index(period.split("-")[1])
    return dt.date(y, mn, _c.monthrange(y, mn)[1]).isoformat()


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

    print("\nMONTH SHAPE + FORECAST")
    # Working-day fraction: the unit that flips the sign of the observed trend.
    # Sep-2026 has 26 working days (ex-Sun); 21 have passed by the 24th.
    check("working-day fraction is not the calendar fraction",
          abs(_wd_fraction("2026-September", 24) - 21 / 26) < 1e-9
          and abs(_wd_fraction("2026-September", 24) - 24 / 30) > 0.005,
          f"wd={_wd_fraction('2026-September', 24):.4f} vs calendar={24/30:.4f}")
    check("a full month is fraction 1.0",
          abs(_wd_fraction("2026-August", 31) - 1.0) < 1e-9)

    shp = conn.execute("SELECT COUNT(*) FROM vahan_month_shape").fetchone()[0] \
        if conn.execute("SELECT name FROM sqlite_master WHERE name='vahan_month_shape'"
                        ).fetchone() else 0
    check("harvested month shapes are loaded", shp >= 3, f"{shp} rows")

    if shp:
        # THE FESTIVE EXCLUSION, which is the whole reason the forecast is not
        # +75%. Sep-2025 must be dropped from EVERY segment, including CV whose
        # own ratio (0.873) sits inside the normal band.
        for sg in SEGMENTS:
            sk = skew(conn, sg, 24)
            drop = " ".join(sk["dropped"])
            check(f"{sg}: Sep-2025 excluded as festive", "2025-September" in drop,
                  drop or "NOT dropped")
        sk = skew(conn, "2W", 24)
        check("2W skew@24 is the normal-month value, not a blend",
              abs(sk["skew"] - 0.983) < 0.01, f"{sk['skew']:.4f}")
        # The skew is a CURVE: d15 must be further from 1.0 than d24.
        s15, s24 = skew(conn, "PV", 15), skew(conn, "PV", 24)
        if s15["n"] and s24["n"]:
            check("skew relaxes toward 1.0 as the cut approaches month end",
                  abs(1 - s15["skew"]) > abs(1 - s24["skew"]),
                  f"PV d15={s15['skew']:.3f} d24={s24['skew']:.3f}")
        # Plausibility: the forecast must sit inside the YoY band July/August set.
        f2 = forecast(conn, "2W", dt.date(2026, 9, 24))
        sep25 = _full_month(conn, "2W", "2025-September")
        yoy = f2["forecast_total"] / sep25 - 1
        check("2W forecast YoY is inside the Jul/Aug band (+15%..+40%)",
              0.15 < yoy < 0.40, f"{100*yoy:+.1f}%")

        # THE DOTTED FORECAST LINE ONLY SAYS ANYTHING IF THESE DIFFER. A
        # segment-wide skew cancels out of a share — (mtd/f)/(total/f) =
        # mtd/total — so the projected share would equal today's exactly and
        # the line would be flat BY CONSTRUCTION, reading as "share will not
        # move" rather than "this cannot tell you". Measured spread at d24:
        # PV runs Tata 0.830 to Others 0.934.
        fp = forecast(conn, "PV", dt.date(2026, 9, 24))
        sk = {k: v["skew"] for k, v in fp["by_label"].items()}
        check("per-maker skews differ inside a segment",
              max(sk.values()) - min(sk.values()) > 0.02,
              f"spread {max(sk.values()) - min(sk.values()):.3f} over {len(sk)} names")
        check("every PV maker has its OWN fit, not the segment fallback",
              all(v["skew_basis"] == "own" for v in fp["by_label"].values()),
              str({k: v["skew_basis"] for k, v in fp["by_label"].items()}))
        # The total is the SUM of the parts, so the projected shares must add
        # to 100 — a chart drawing shares that do not is an arithmetic lie.
        tot = sum(v["share_pct"] for v in fp["by_label"].values())
        check("forecast shares sum to 100", abs(tot - 100.0) < 0.05, f"{tot:.3f}")

    # REJECTION: a range not starting on the 1st is not a month-to-date and the
    # whole fit is meaningless on it. Must refuse rather than mis-date.
    import tempfile
    try:
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Maker Report"
        ws["A1"] = "Maker and Vehicle Category Group Data for All State (05 Aug 2026 to 24 Aug 2026)"
        ws.append([]) if False else None
        for c, v in zip("ABCD", ["Maker", "Two Wheeler", "Four Wheeler", "Total"]):
            ws[f"{c}3"] = v
        ws["A4"], ws["B4"], ws["C4"], ws["D4"] = "X LTD", 1, 1, 2
        p2 = pathlib.Path(tempfile.gettempdir()) / "_vs_bad_start.xlsx"
        wb.save(p2)
        try:
            load_shape(sqlite3.connect(":memory:"), p2)
            check("a range not starting on the 1st is refused", False)
        except ValueError as e:
            check("a range not starting on the 1st is refused", "not the 1st" in str(e))

        # REJECTION: a cross-foot failure must refuse the file, not load a
        # plausible-but-wrong partial.
        ws["A1"] = "Maker and Vehicle Category Group Data for All State (01 Aug 2026 to 24 Aug 2026)"
        ws["D4"] = 99
        p3 = pathlib.Path(tempfile.gettempdir()) / "_vs_bad_foot.xlsx"
        wb.save(p3)
        try:
            load_shape(sqlite3.connect(":memory:"), p3)
            check("a cross-foot failure refuses the file", False)
        except ValueError as e:
            check("a cross-foot failure refuses the file", "cross-foot" in str(e))
    except ImportError:
        print("  SKIP  openpyxl not available for the rejection fixtures")


    print(f"\n{len(fails)} failure(s)" + (f": {fails}" if fails else ""))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--forecast", action="store_true",
                    help="project the current month from its month-to-date level")
    ap.add_argument("--load-shape", metavar="GLOB",
                    help="load report-builder exports (data/staging/vahan/*.xlsx)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    conn = _conn()
    if a.load_shape:
        import glob as _g
        n = 0
        for f in sorted(_g.glob(a.load_shape)):
            r = load_shape(conn, f)
            print(f"  {r['source']:44} {r['period']:16} d{r['cut_day']:<3} "
                  f"{r['makers']} makers")
            n += 1
        print(f"loaded {n} file(s)")
        return 0
    if a.forecast:
        for seg, cfg in SEGMENTS.items():
            f = forecast(conn, seg)
            if f["state"] != "live":
                print(f"{cfg['label']}: {f['state']}")
                continue
            print("")
            print(f"### {cfg['label']} — {f['period']} "
                  f"(MTD to day {f['asof_day']}, captured {f['capture_date']})")
            print(f"    fraction = working-day {f['wd_fraction']} x skew "
                  f"{f['skew']} = {f['fraction']}   [{f['skew_basis']}, "
                  f"n={f['skew_n']}]")
            if f["skew_months"]:
                print(f"    fitted on : {', '.join(f['skew_months'])}")
            if f["skew_dropped"]:
                print(f"    dropped   : {', '.join(f['skew_dropped'])}")
            if f["skew_basis"] == "none":
                print("    NO MONTH HARVESTED AT THIS CUT DAY — this is plain "
                      "working-day extrapolation, with no back-loading applied.")
            print(f"    {'':18}{'MTD':>11}{'FORECAST':>11}")
            print(f"    {'= segment total':18}{f['mtd_total']:>11,}"
                  f"{f['forecast_total']:>11,}")
            for k, v in f["by_label"].items():
                print(f"    {k:18}{v['mtd']:>11,}{v['forecast']:>11,}")
        return 0
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
