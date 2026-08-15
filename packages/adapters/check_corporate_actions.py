"""Detect unadjusted corporate actions in a price series.

A demerger, spin-off, split or large special dividend puts a step change in an
UNADJUSTED price series. Every downstream calculation that crosses it is then
wrong in a way that looks like a market move:

  - a regression picks up a huge spurious alpha
  - a valuation percentile compares a company to a different company
  - a backfill "predicts" a drop that was never a drop

VEDL is the live case: four entities demerged 1:1 with a 1 May 2026 record date
and June listings, so the parent's quoted price steps down mechanically. Nothing
about that is a return.

Usage:
    python packages/adapters/check_corporate_actions.py
    python packages/adapters/check_corporate_actions.py --entity vedanta --detail
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

# A single-session move this large in a large-cap is a corporate action until
# proven otherwise.
JUMP = 0.15


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--threshold", type=float, default=JUMP)
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    ents = ([a.entity] if a.entity else
            [r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM prices")])

    found_any = False
    for eid in sorted(ents):
        rows = conn.execute(
            "SELECT date, close FROM prices WHERE entity_id=? ORDER BY date", (eid,)
        ).fetchall()
        jumps = []
        for (d0, c0), (d1, c1) in zip(rows, rows[1:]):
            if c0 and abs(c1 / c0 - 1) >= a.threshold:
                jumps.append((d0, c0, d1, c1, (c1 / c0 - 1) * 100))
        if jumps:
            found_any = True
            print(f"{eid}: {len(jumps)} jump(s) >= {a.threshold:.0%}")
            for d0, c0, d1, c1, pct in jumps:
                print(f"   {d0} {c0:>9,.2f}  ->  {d1} {c1:>9,.2f}   {pct:+.1f}%")
            print("   ^ treat as a corporate action until confirmed otherwise;\n"
                  "     series is NOT comparable across this date")
        elif a.detail:
            print(f"{eid}: clean (no move >= {a.threshold:.0%})")

    if not found_any and not a.detail:
        print(f"no single-session moves >= {a.threshold:.0%} in any series")
        print("NOTE: absence of a jump does NOT prove absence of a corporate\n"
              "action. Yahoo often back-adjusts the whole history instead,\n"
              "which leaves no step at all but silently rewrites older levels —\n"
              "so a 'clean' series can still be non-comparable across a demerger.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
