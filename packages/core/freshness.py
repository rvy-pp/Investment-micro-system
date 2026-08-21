"""Per-feed staleness. The check the refresh light was missing.

    python packages/core/freshness.py
    python packages/core/freshness.py --json

WHY THIS EXISTS. refresh.py can exit clean, run_scores.py can stamp today's
date, and the dashboard can show a green light over commodity inputs that are
five days old — because "the run succeeded" and "the inputs are current" are
different claims and only the first was ever checked. That is this repo's
signature failure shape in operational form: a plausible indicator, nothing
raised, nothing wrong on the surface. Found 2026-08-21 when the light read green
with zinc_shfe 5 trading days stale and OI 5 days stale.

TWO AGES, NOT ONE, and the distinction is load-bearing:

  row_age    trading days since the series had ANY row. Feed liveness. This is
             what gets a threshold, because a feed that stops printing is
             unambiguously broken.

  value_age  trading days since the close actually CHANGED. Information age.
             NOT thresholded, because a flat market is legitimately flat and
             invariant 3 says silence changes nothing.

They diverge hard on the assessed series. cp_coke had THREE distinct closes in
its last thirty rows and thermal_coal_seaborne six, because both are episodic
cited levels carried forward daily. Judging those on row_age alone reports them
as fresh when the last real assessment is weeks old; judging them on value_age
alone reports a quiet market as a broken feed. Report both, threshold the first,
and label the series so the reader knows which number means something.
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

# Thresholds in TRADING days, from docs/DAILY_MONITORING.md "Tier 1 — data
# freshness". Trading days, not calendar: on a calendar count every Monday reads
# two days stale and the indicator gets ignored, which is the only way an
# indicator can fail. Indian market holidays are NOT modelled, so a Diwali gap
# reads one day stale — that errs toward over-reporting, which is the safe side.
#
# kind: 'feed'     a live price feed; row_age is the meaningful number
#       'assessed' episodic cited level carried forward; value_age is
#       'monthly'  a monthly series; row_age in trading days is meaningless
FEEDS = {
    "hindalco":              ("Yahoo .NS",        2, "feed"),
    "nalco":                 ("Yahoo .NS",        2, "feed"),
    "hindustan_zinc":        ("Yahoo .NS",        2, "feed"),
    "vedanta":               ("Yahoo .NS",        2, "feed"),
    "vaml":                  ("Yahoo .NS",        2, "feed"),
    "usdinr":                ("Yahoo / pack",     2, "feed"),
    "usdcny":                ("Yahoo / pack",     2, "feed"),
    "lme_aluminium":         ("Yahoo ALI=F / pack", 3, "feed"),
    "alumina_index":         ("Yahoo ALA=F / pack", 3, "feed"),
    "midwest_premium":       ("Yahoo AUP=F",      3, "feed"),
    "silver":                ("Yahoo SI=F / pack", 3, "feed"),
    "lme_zinc":              ("Daily Metals Pack", 3, "feed"),
    "brent":                 ("Daily Metals Pack", 3, "feed"),
    "zinc_shfe":             ("Wind ZN.SHF",      3, "feed"),
    "cp_coke":               ("Daily Metals Pack", 3, "assessed"),
    "thermal_coal_seaborne": ("Daily Metals Pack", 3, "assessed"),
    "iron_ore":              ("IMF monthly",     45, "monthly"),
}
DEFAULT = ("unmapped", 3, "feed")

# OI is not in `prices`, so it is checked separately rather than being missed.
OI_THRESHOLD = 2


def trading_days(a: dt.date, b: dt.date) -> int:
    """Weekdays strictly after `a`, up to and including `b`."""
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def check(today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    conn = sqlite3.connect(DB)
    rows, worst = [], 0

    for (eid,) in conn.execute("SELECT DISTINCT entity_id FROM prices ORDER BY entity_id"):
        feed, limit, kind = FEEDS.get(eid, DEFAULT)
        hist = conn.execute(
            "SELECT date, close FROM prices WHERE entity_id=? AND close IS NOT NULL "
            "ORDER BY date", (eid,)).fetchall()
        if not hist:
            continue
        last_d, last_c = hist[-1]
        row_age = trading_days(dt.date.fromisoformat(last_d), today)

        # Walk back to the last date the value actually moved.
        changed_on = last_d
        for d, c in reversed(hist[:-1]):
            if c != last_c:
                break
            changed_on = d
        value_age = trading_days(dt.date.fromisoformat(changed_on), today)

        # A monthly series is never judged on trading days — 45 CALENDAR days is
        # the documented bar, and applying a 3-day rule would flag it forever.
        if kind == "monthly":
            cal = (today - dt.date.fromisoformat(last_d)).days
            stale = cal > limit
            age_txt = f"{cal} calendar days"
        else:
            stale = row_age > limit
            age_txt = f"{row_age} trading days"

        if stale:
            worst = max(worst, row_age)
        rows.append({
            "series": eid, "feed": feed, "kind": kind, "limit": limit,
            "last_date": last_d, "row_age": row_age,
            "value_changed_on": changed_on, "value_age": value_age,
            "stale": stale, "age_txt": age_txt,
        })

    # OI lives in its own table and was silently absent from every check until
    # 2026-08-21, when it turned out to be 5 trading days behind.
    oi_last = conn.execute("SELECT MAX(date) FROM oi").fetchone()[0]
    oi = None
    if oi_last:
        age = trading_days(dt.date.fromisoformat(oi_last), today)
        oi = {"series": "oi", "feed": "vault_oi.py", "kind": "feed",
              "limit": OI_THRESHOLD, "last_date": oi_last, "row_age": age,
              "stale": age > OI_THRESHOLD, "age_txt": f"{age} trading days"}
        if oi["stale"]:
            worst = max(worst, age)
    conn.close()

    stale = [r for r in rows if r["stale"]] + ([oi] if oi and oi["stale"] else [])
    return {
        "as_of": today.isoformat(),
        "series": rows,
        "oi": oi,
        "stale": stale,
        "n_stale": len(stale),
        "worst_age": worst,
        "ok": not stale,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = check()
    if a.json:
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1

    print(f"feed freshness as of {r['as_of']}\n")
    print(f"{'series':24}{'feed':22}{'last':>12}{'row':>6}{'value':>7}  note")
    print("-" * 88)
    for x in sorted(r["series"], key=lambda x: (-x["row_age"], x["series"])):
        note = ""
        if x["stale"]:
            note = f"STALE — limit {x['limit']}"
        elif x["kind"] == "assessed" and x["value_age"] > x["row_age"] + 2:
            note = f"assessed — unchanged {x['value_age']}d, carried forward"
        print(f"{x['series']:24}{x['feed']:22}{x['last_date']:>12}"
              f"{x['row_age']:>6}{x['value_age']:>7}  {note}")
    if r["oi"]:
        o = r["oi"]
        print(f"{'oi':24}{o['feed']:22}{o['last_date']:>12}{o['row_age']:>6}"
              f"{'':>7}  {'STALE — limit ' + str(o['limit']) if o['stale'] else ''}")

    print()
    if r["ok"]:
        print("all feeds within their thresholds")
    else:
        print(f"{r['n_stale']} feed(s) STALE, worst {r['worst_age']} trading days:")
        for x in r["stale"]:
            print(f"   {x['series']:24}{x['feed']:22}{x['age_txt']}")
        print("\ndocs/DAILY_MONITORING.md: report it, and withhold the affected")
        print("scores. A stale price is not a flat price.")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
