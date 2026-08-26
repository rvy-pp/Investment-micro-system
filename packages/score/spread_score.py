"""P1 for a CONVERTER: score the input/substitute spread, not a margin bridge.

WHY A SECOND BASIS FOR ECONOMICS EXISTS AT ALL.

The margin bridge answers "did this company's own cost and revenue lines move".
For APL Apollo it answers correctly and uselessly. Output and input both link to
hrc_india_inr at ~1:1, so the bridge nets to ~0 — and measurement confirmed the
equity nets to ~0 on HRC too (+0.103 against tata_steel's +0.076 and jsw_steel's
+0.109 over 3y; see packages/review/driver_beta.py). A converter with brand
pricing power passes coil through. Its EBITDA/t is genuinely near-constant:
"EBITDA/t Rs5,522 flat qoq (+18% yoy) despite West Asia war, energy inflation,
dealer destocking".

SO THE PER-TONNE MARGIN IS THE WRONG QUANTITY. What moves a converter's earnings
is VOLUME, and what moves volume is its feedstock cost against its COMPETITORS'
feedstock cost. APL Apollo buys primary hot-rolled coil; the small tube makers it
competes with buy cheaper secondary/re-rolled sheet — "patra" in the trade. The
digests name this as the thing to watch, verbatim: "key watch: steel price outlook
and impact of widening HRC-Patra gap".

    spread WIDE    APL's premium feedstock is dear against what secondary
                   players pay -> it loses share -> volumes fall
    spread NARROW  the premium is cheap in relative terms -> it gains share

PM instruction, 2026-08-26: model APL Apollo on volumes via this spread and score
it 1-5 directly, not through pct_of_ebitda.

---------------------------------------------------------------------------
THE PROXY, AND IT IS NOT ALIASED TO THE THING IT PROXIES (invariant 6)
---------------------------------------------------------------------------
There is no patra series in the pack. `rebar_india_secondary_inr` is used as the
substitute leg, and it is NOT the same product: patra is secondary FLAT sheet,
this is secondary rebar 12mm, a LONG product. Both are secondary-route
(induction-furnace / re-rolled) steel, so they track the same secondary price
level, which is the economically relevant thing — but the form differs and the
spec says so rather than renaming the series. If a secondary-flat assessment ever
lands, repoint `substitute` at it; do not rename this one.

---------------------------------------------------------------------------
WHAT IS VERIFIED AND WHAT IS NOT — read before trusting the score
---------------------------------------------------------------------------
NOT CONFIRMED BY WEEKLY PRICES, and that is the expected result rather than
evidence against. Change in the spread against APL Apollo's weekly returns gives
r = +0.010 over 3y and -0.017 since 2016, i.e. nothing. A share-loss mechanism
plays out over QUARTERS through volume, so weekly repricing is the wrong
instrument for it and a null there neither supports nor refutes.

CORROBORATED ONCE, in the right direction and at the right timescale: the spread
currently sits at z +1.09 (1y) / +1.90 (3y) — wide — and APL Apollo has just
printed 745kt against 800-900kt guided, volumes -6% yoy, with management's
15-20% growth guide "expected to be cut to single digits". One observation, the
correct sign.

THE TEST THAT WOULD SETTLE IT is the spread against REPORTED VOLUMES, quarter by
quarter. `observations` holds exactly one volume actual for this name today
(Q1FY27, 745kt), so it cannot be run yet. It becomes runnable at ~6-8 quarters.
Until then this is a PM-specified structural model with one corroborating
observation, and it is labelled that way in the spec.

---------------------------------------------------------------------------
THE ARITHMETIC
---------------------------------------------------------------------------
    spread(d) = price(long, d) - price(substitute, d)
    z         = (spread(d) - mean) / sd     over `lookback_days` ending at d
    score     = hill(-z)                    wide spread -> LOW score

Z, NOT A RUPEE LEVEL, because the spread has no natural scale and has ranged
-7,300 to +22,250 since 2011. And the SAME hill curve and anchor as P3 valuation
(z = 1.0 reads 4.0), because both are "how unusual is this against its own
history" — using a different curve for the same question would make two scores
that look alike mean different things.

CALENDAR-DAY lookback, per the CLAUDE.md gotcha: row counts on a series carrying
weekend rows are not days.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from scoring import score as to_score, solve_k  # noqa: E402

Z_ANCHOR, SCORE_ANCHOR, P = 1.0, 4.0, 1.5      # same as valuation, deliberately
MIN_POINTS = 60


def spread_series(conn, long_id: str, sub_id: str, upto: str,
                  lookback_days: int):
    floor = (dt.date.fromisoformat(upto)
             - dt.timedelta(days=int(lookback_days))).isoformat()
    rows = conn.execute(
        "SELECT a.date, a.close - b.close FROM prices a JOIN prices b "
        "ON a.date = b.date WHERE a.entity_id=? AND b.entity_id=? "
        "AND a.close IS NOT NULL AND b.close IS NOT NULL "
        "AND a.date BETWEEN ? AND ? ORDER BY a.date",
        (long_id, sub_id, floor, upto)).fetchall()
    return [(d, v) for d, v in rows]


def score_entity(conn, ent: dict, as_of: str) -> tuple:
    """(score, z, detail, withheld) using the entity's declared spread basis."""
    cfg = (ent.get("economics_spread") or {})
    long_id, sub_id = cfg.get("long"), cfg.get("substitute")
    if not long_id or not sub_id:
        return None, None, None, "no economics_spread declared"
    look = int(cfg.get("lookback_days") or 1260)

    ser = spread_series(conn, long_id, sub_id, as_of, look)
    if len(ser) < MIN_POINTS:
        return None, None, None, (
            f"only {len(ser)} overlapping {long_id}/{sub_id} points in "
            f"{look}d (need {MIN_POINTS})")
    vals = [v for _d, v in ser]
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    if sd <= 0:
        return None, None, None, "spread has no variance in the window"
    cur = vals[-1]
    z = (cur - mean) / sd

    # WIDE IS BAD: a wide spread means the substitute is cheap relative to this
    # company's feedstock, so it loses volume. `worse_if_wide: false` would
    # describe a company that BENEFITS from a wide spread — a secondary producer.
    sign = -1.0 if cfg.get("worse_if_wide", True) else 1.0
    k = solve_k("hill", Z_ANCHOR, SCORE_ANCHOR, P)
    s = to_score(sign * z, k, "hill", P)
    return s, z, {
        "basis": "spread_z",
        "spread": round(cur, 1),
        "legs": f"{long_id} - {sub_id}",
        "z": round(z, 2),
        "mean": round(mean, 1),
        "sd": round(sd, 1),
        "n": len(vals),
        "lookback_days": look,
        "direction": "wide is bad" if sign < 0 else "wide is good",
    }, None
