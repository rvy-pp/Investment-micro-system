r"""How much of the daily score movement is INFORMATION vs WINDOW ROLL-OFF.

A trailing window has two moving ends. Day over day:

    Δwindow = [P(today) - P(yesterday)]   +   [P(drop-2) - P(drop-1)]
              \_______ new information ___/     \___ roll-off artefact ___/

The second term is the old end falling out. It moves the score with no new
information at all, and it is indistinguishable from real news in the output.

This measures the split on the actual data rather than asserting it, because
the fix (EWMA, already specified in specs/scoring.yaml but not implemented) is
only worth the work if the artefact is actually large.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="lme_aluminium")
    ap.add_argument("--window", type=int, default=30)
    a = ap.parse_args()

    conn = sqlite3.connect(REPO / "data" / "ims.db")
    rows = conn.execute(
        "SELECT date, close FROM prices WHERE entity_id=? ORDER BY date",
        (a.series,)).fetchall()
    conn.close()
    px = {d: c for d, c in rows}
    dates = [d for d, _ in rows]

    def on_or_before(d: str):
        ks = [k for k in dates if k <= d]
        return px[ks[-1]] if ks else None

    news, roll = [], []
    for i in range(1, len(dates)):
        today, yday = dates[i], dates[i - 1]
        old_t = (dt.date.fromisoformat(today) - dt.timedelta(days=a.window)).isoformat()
        old_y = (dt.date.fromisoformat(yday) - dt.timedelta(days=a.window)).isoformat()
        pt, py = on_or_before(today), on_or_before(yday)
        ot, oy = on_or_before(old_t), on_or_before(old_y)
        if None in (pt, py, ot, oy):
            continue
        news.append(pt - py)          # genuinely new
        roll.append(oy - ot)          # the old end dropping out

    n = len(news)
    if not n:
        print("not enough overlapping history", file=sys.stderr)
        return 1

    mean_news = sum(abs(x) for x in news) / n
    mean_roll = sum(abs(x) for x in roll) / n
    share = mean_roll / (mean_news + mean_roll)
    dominated = sum(1 for a_, b_ in zip(news, roll) if abs(b_) > abs(a_))

    print(f"{a.series} · {a.window}-day window · {n} sessions\n")
    print(f"  mean |new information| per day   {mean_news:>8.2f}")
    print(f"  mean |roll-off| per day          {mean_roll:>8.2f}")
    print(f"  roll-off share of total movement {share:>8.1%}")
    print(f"  days where roll-off EXCEEDED the news  {dominated}/{n} "
          f"({dominated/n:.0%})")
    print()
    if share > 0.4:
        print("  => Roughly half the daily score movement carries NO new")
        print("     information. It is the window's old end falling out.")
        print("     EWMA accumulation (specs/scoring.yaml) removes this: old")
        print("     news fades smoothly instead of dropping off a cliff.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
