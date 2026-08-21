"""One-off: remove non-LME rows from lme_aluminium so the series is one measure.

    python packages/core/clean_lme_aluminium.py            # report
    python packages/core/clean_lme_aluminium.py --apply

WHY. westmetall.py loaded 161 rows of real LME cash into lme_aluminium, and the
bridge did not use a single one of them. Its 30-day window resolved to:

    start 2026-05-23  3,720.28  source NULL   <- a SATURDAY
    end   2026-08-21  3,344.75  source NULL   <- Yahoo CME, written that morning

Both endpoints were leftover CME values, so the shock was still CME-to-CME and
the whole load was cosmetic. A partially-corrected series is worse than an
uncorrected one: it looks fixed.

TWO CLASSES REMOVED, both provably not LME cash-settlement:

  WEEKEND ROWS (66 in the westmetall window). The LME does not settle on a
  Saturday or a Sunday, so a weekend cash-settlement row is not an observation.
  These carry the CME value forward and score dates DO land on them — the
  pillar_scores as_of runs on calendar days, 61 dates over a 62-day span.

  ORPHANED CME WEEKDAYS. Dates inside the westmetall span with no westmetall row
  and no other owner. yahoo_prices.py no longer writes this series at all
  (CANDIDATES["lme_aluminium"] = [] as of 2026-08-21), so nothing will replace
  them and they would sit as permanent CME islands in an LME series.

ONLY lme_aluminium. lme_zinc is deliberately untouched even though it also has 66
weekend rows: those came from the Daily Metals Pack, which carries a full
calendar and forward-fills its own LME cash, so its weekends are the same measure
as its weekdays. Aluminium's were a DIFFERENT measure, which is the whole problem.

NOT A DELETION OF HISTORY. Rows outside the westmetall span (pre-2026-01-02,
~4,494 of them) are left exactly as they are. They are still a pack/CME mixture
and are still wrong in the same way, but there is no LME cash source reaching
back that far, so removing them would leave a hole rather than a correction.
That limitation is real and is recorded in CLAUDE.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
ENTITY = "lme_aluminium"
KEEP_SOURCE = "westmetall"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    span = conn.execute(
        "SELECT MIN(date), MAX(date) FROM prices WHERE entity_id=? AND source=?",
        (ENTITY, KEEP_SOURCE)).fetchone()
    if not span or not span[0]:
        print(f"no {KEEP_SOURCE} rows for {ENTITY} — run westmetall.py --load first",
              file=sys.stderr)
        return 1
    lo, hi = span
    print(f"{ENTITY}: {KEEP_SOURCE} span {lo} .. {hi}\n")

    rows = conn.execute(
        "SELECT date, close, source FROM prices WHERE entity_id=? AND date>=? "
        "ORDER BY date", (ENTITY, lo)).fetchall()
    weekend, orphan = [], []
    for d, v, s in rows:
        if s == KEEP_SOURCE:
            continue
        (weekend if dt.date.fromisoformat(d).weekday() > 4 else orphan).append((d, v, s))

    print(f"{'to remove':14}{'n':>5}   examples")
    print("-" * 72)
    print(f"{'weekend':14}{len(weekend):>5}   " +
          ", ".join(f"{d} {v:,.0f}" for d, v, _ in weekend[:3]))
    print(f"{'orphan CME':14}{len(orphan):>5}   " +
          ", ".join(f"{d} {v:,.0f}" for d, v, _ in orphan[:5]))
    kept = sum(1 for _d, _v, s in rows if s == KEEP_SOURCE)
    print(f"\n{kept} westmetall rows kept; "
          f"{conn.execute('SELECT COUNT(*) FROM prices WHERE entity_id=? AND date<?',(ENTITY,lo)).fetchone()[0]:,} "
          f"rows before {lo} untouched")

    if not a.apply:
        print("\nreport only — pass --apply to delete")
        conn.close()
        return 0

    doomed = [d for d, _v, _s in weekend + orphan]
    conn.executemany("DELETE FROM prices WHERE entity_id=? AND date=?",
                     [(ENTITY, d) for d in doomed])
    conn.commit()
    print(f"\ndeleted {len(doomed)} rows")
    left = conn.execute(
        "SELECT COALESCE(source,'(null)'), COUNT(*) FROM prices WHERE entity_id=? "
        "AND date>=? GROUP BY source", (ENTITY, lo)).fetchall()
    print("in-span sources now: " + ", ".join(f"{s}×{n}" for s, n in left))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
