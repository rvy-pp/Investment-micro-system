"""Resolve a price series from continuous feeds AND episodic cited observations.

Feeds print every session. Research-sourced series print when a broker happens
to mention them — cp_coke appears on 10 of 44 days. Both have to resolve
through one interface or the bridge cannot mix them.

THREE RULES, and getting any of them wrong produces a plausible wrong number.

1. CARRY FORWARD, DO NOT INTERPOLATE. Between observations the last cited level
   stands. It did not drift smoothly to the next print; nobody observed it, so
   the honest value is the last one seen. Interpolating would invent price
   action and, worse, would make the bridge emit small daily deltas out of
   nothing — the exact decay-manufactures-motion failure this system exists to
   avoid.

2. A RESTATEMENT IS NOT A NEW DATAPOINT. Two brokers quoting the same -7% qoq
   is one fact observed twice. It refreshes staleness but must not move the
   level twice. Deduplicated on (metric, period, value).

3. STORE WHAT WAS SAID, DERIVE THE REST. Research states levels
   ("to USD147/t") AND relative moves ("-7% qoq"). Storing a derived level as
   though it were cited would put an uncited number in the store. So a relative
   observation is stored as a relative one and chained here, in code, off the
   most recent anchor.

STALENESS IS REPORTED, NEVER DECAYED. A carried-forward level that is 40 days
old is still the best available estimate — but a signal leaning on it should
not carry full conviction. So the age travels with the value and the caller
gates on it.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass


@dataclass
class Point:
    date: str          # date the VALUE is about (as_of), not when it was read
    value: float
    origin: str        # 'feed' | 'cited'
    stale_days: int = 0


def _iso(d: str) -> _dt.date:
    return _dt.date.fromisoformat(d)


def observation_series(conn: sqlite3.Connection, metric: str) -> list[Point]:
    """Build a level series for `metric` from cited observations.

    Absolute observations set the level. Relative ones (unit '%') multiply the
    running level forward. Restatements of an identical (period, value) are
    dropped — see rule 2.
    """
    rows = conn.execute(
        "SELECT as_of, value_num, unit, period, quote FROM observations "
        "WHERE factor='price' AND metric=? AND supersedes_id IS NULL "
        "AND value_num IS NOT NULL ORDER BY as_of",
        (metric,),
    ).fetchall()

    out: list[Point] = []
    level: float | None = None
    seen: set[tuple] = set()

    for as_of, value, unit, period, _quote in rows:
        key = (period, round(value, 6), (unit or "").strip())
        if key in seen:
            continue                       # restatement, not new information
        seen.add(key)

        if (unit or "").strip() == "%":
            if level is None:
                continue                   # a % move with no anchor is unusable
            level = level * (1.0 + value / 100.0)
        else:
            level = value
        out.append(Point(as_of, level, "cited"))
    return out


def feed_series(conn: sqlite3.Connection, entity_id: str) -> list[Point]:
    return [Point(d, v, "feed") for d, v in conn.execute(
        "SELECT date, close FROM prices WHERE entity_id=? ORDER BY date",
        (entity_id,))]


def resolve(conn: sqlite3.Connection, series_id: str) -> list[Point]:
    """Feed if one exists, otherwise the cited series. Never both.

    Mixing them would let a monthly broker estimate overwrite an exchange
    close on the days it happens to land.
    """
    feed = feed_series(conn, series_id)
    return feed if feed else observation_series(conn, series_id)


def value_on(points: list[Point], date: str) -> Point | None:
    """Carried-forward value as of `date`, with its age attached."""
    prior = [p for p in points if p.date <= date]
    if not prior:
        return None
    p = prior[-1]
    return Point(p.date, p.value, p.origin,
                 stale_days=(_iso(date) - _iso(p.date)).days)


def ewma_delta(points: list[Point], as_of: str, half_life_days: float = 10.0,
               horizon_days: int = 90):
    """Exponentially-weighted accumulated move — the window's replacement.

    A trailing window has TWO moving ends, so day over day:

        change = [new price move] + [old price falling out of the back]

    The second term moves the score with no new information and is
    indistinguishable from real news in the output. Measured on this store it
    is 48.6% of daily movement for LME aluminium and 54% for alumina, and on
    44% of days it EXCEEDED the news. Half the score's daily motion was noise
    by construction.

    An EWMA has no back end. Each daily change enters at full weight and decays
    smoothly; nothing ever drops off a cliff. `horizon_days` only bounds the
    arithmetic — at a 10-day half-life a 90-day-old move carries 2^-9, which is
    a rounding error, not a truncation.
    """
    cutoff = (_iso(as_of) - _dt.timedelta(days=horizon_days)).isoformat()
    pts = [p for p in points if cutoff <= p.date <= as_of]
    if len(pts) < 2:
        return None

    lam = 0.5 ** (1.0 / half_life_days)
    total = 0.0
    for older, newer in zip(pts, pts[1:]):
        age = (_iso(as_of) - _iso(newer.date)).days
        total += (newer.value - older.value) * (lam ** age)

    newest = pts[-1]
    stale = (_iso(as_of) - _iso(newest.date)).days
    return total, Point(newest.date, newest.value, newest.origin, stale), pts[0]


def delta_over(points: list[Point], as_of: str, window_days: int):
    """(delta, new_point, old_point) over a CALENDAR window, or None.

    Returns None when both ends resolve to the same observation — no move was
    observed, which is different from a move of zero and must not be reported
    as one.
    """
    old_date = (_iso(as_of) - _dt.timedelta(days=window_days)).isoformat()
    new = value_on(points, as_of)
    old = value_on(points, old_date)
    if not new or not old or new.date == old.date:
        return None
    return new.value - old.value, new, old


# ---------------------------------------------------------------------------
# The clock. Which date the system is scoring "as of".
#
# WHY THIS IS NOT `SELECT MAX(date) FROM prices`. That is what run_scores.py and
# bridge.py both did, and it was right for as long as every series in `prices`
# printed every session. It stopped being right on 2026-08-27, when the cement
# pack landed: its current-month column is a month-TO-DATE average stamped at
# the CAPTURE date, so `MAX(date)` became today while every equity and commodity
# close was still yesterday's. run_scores.py would then have persisted scores
# dated 27 Aug built entirely from 26 Aug prices, against 1,407 stored dates
# where score-date and price-date are the same day. Nothing would have raised;
# the score column would simply have shifted a day relative to the prices it is
# made of, and the backtest aligns forward returns on that date.
#
# PM decision 2026-08-27: a coarse series contributes its SHOCK but must not set
# the CLOCK.
#
# CADENCE IS MEASURED, NOT LISTED. An explicit list of monthly ids would be
# correct today and silently wrong the first time somebody adds a monthly series
# without reading this — which is the same maintenance failure as an unregistered
# unit or an unregistered price source.
#
# AND IT IS THE MEDIAN GAP, NOT THE LAST GAP. My first version compared only the
# two most recent prints, which works every day of the month except one: on
# 1 September the cement series holds 2026-08-31 and 2026-09-01, one day apart,
# and would be classified DAILY at precisely the month seam. A median over the
# last several prints reads ~30 days there and every other day.
DAILY_MAX_GAP_DAYS = 7          # a real daily series gaps only over weekends/holidays
_CADENCE_SAMPLE = 8             # prints to measure the gap over


def is_daily(conn: sqlite3.Connection, entity_id: str) -> bool:
    """True when this series prints about every session.

    Fewer than three prints is treated as NOT daily: a series that cannot be
    shown to be daily must not be allowed to set the clock. Withhold rather
    than guess.
    """
    ds = [r[0] for r in conn.execute(
        "SELECT date FROM prices WHERE entity_id=? ORDER BY date DESC LIMIT ?",
        (entity_id, _CADENCE_SAMPLE))]
    if len(ds) < 3:
        return False
    gaps = sorted((_iso(ds[i]) - _iso(ds[i + 1])).days for i in range(len(ds) - 1))
    median = gaps[len(gaps) // 2]
    return median <= DAILY_MAX_GAP_DAYS


def latest_daily_date(conn: sqlite3.Connection) -> str | None:
    """The newest close on a series that prints daily. The system's `as_of`.

    Falls back to plain MAX(date) if nothing qualifies, so a store holding only
    coarse series still resolves rather than returning None into a date field.
    """
    best = None
    for (eid,) in conn.execute("SELECT DISTINCT entity_id FROM prices"):
        if not is_daily(conn, eid):
            continue
        d = conn.execute("SELECT MAX(date) FROM prices WHERE entity_id=?",
                         (eid,)).fetchone()[0]
        if d and (best is None or d > best):
            best = d
    return best or conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]


def _selftest() -> int:
    """python packages/core/series.py --selftest

    The cadence guard, tested BOTH ways. Per the GLOB lesson in CLAUDE.md a
    guard needs an acceptance test as well as a rejection test — that bug passed
    every test it had because the tests only checked that bad rows were refused.

    `cement_seam` is the case that killed the first implementation: on 1
    September the cement series holds 2026-08-31 and 2026-09-01, ONE day apart,
    so a last-gap test classifies it daily on exactly that day and the clock
    jumps. `holiday` is the mirror risk — a genuine daily series with one long
    break must NOT be demoted.
    """
    import calendar
    import datetime as d

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE prices (entity_id TEXT, date TEXT, close REAL)")

    def month_ends(n: int, upto: d.date) -> list[str]:
        out, y, m = [], upto.year, upto.month
        for _ in range(n):
            out.append(d.date(y, m, calendar.monthrange(y, m)[1]).isoformat())
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        return sorted(out)

    d0 = d.date(2026, 6, 1)
    daily = [(d0 + d.timedelta(days=i)).isoformat() for i in range(90)
             if (d0 + d.timedelta(days=i)).weekday() < 5]
    fixtures = {
        "cement_seam": month_ends(12, d.date(2026, 8, 31)) + ["2026-09-01"],
        "cement_mid":  month_ends(12, d.date(2026, 7, 31)) + ["2026-08-27"],
        "equity":      daily,
        "holiday":     daily[:-10] + ["2026-08-26"],
        "sparse":      ["2026-08-25", "2026-08-26"],
    }
    for eid, ds in fixtures.items():
        conn.executemany("INSERT INTO prices VALUES (?,?,100)",
                         [(eid, x) for x in ds])

    expect = {"cement_seam": False, "cement_mid": False,
              "equity": True, "holiday": True, "sparse": False}
    bad = 0
    for eid, want in expect.items():
        got = is_daily(conn, eid)
        ok = got == want
        bad += not ok
        print(f"  {eid:14} is_daily={str(got):5} expected={str(want):5} "
              f"{'ok' if ok else '*** FAIL ***'}")
    clock = latest_daily_date(conn)
    ok = clock != "2026-09-01"
    bad += not ok
    print(f"  latest_daily_date -> {clock}  "
          f"{'ok (not the coarse seam date)' if ok else '*** FAIL ***'}")
    print("selftest: " + ("PASS" if not bad else f"{bad} FAILURE(S)"))
    return 0 if not bad else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_selftest() if "--selftest" in sys.argv else
                     print("usage: series.py --selftest") or 0)
