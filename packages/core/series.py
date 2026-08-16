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
