"""The combined score, and where the pillars disagree.

The PM's stated use: one combined score, find where names differ widely, then
pick pairs by hand. So this reports BOTH the composite and the disagreement
behind it — because two names averaging 3.2 are different propositions when one
is 3.2 across the board and the other is economics 4.4 against valuation 1.9.

Reads persisted rows, so it is a query rather than a recomputation. That is the
point of having a store: the same numbers the review layer will grade later.

Usage:
    python packages/score/combined.py
    python packages/score/combined.py --history nalco
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
PILLARS = ["economics", "valuation", "mood", "guidance"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--history")
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    # The latest date with a COMPOSITE, not the latest date in the table.
    # backfill_p1.py writes the economics pillar alone over a long history, so
    # MAX(as_of) is an economics-only date with no composite on it — which made
    # this command crash rather than show the last full cross-section.
    as_of = a.as_of or conn.execute(
        "SELECT MAX(as_of) FROM pillar_scores WHERE pillar='composite' "
        "AND score IS NOT NULL").fetchone()[0]
    if as_of is None:
        print("no composite has ever been written — run run_scores.py first")
        return 1

    if a.history:
        print(f"{a.history} — composite history\n")
        print(f"{'date':12}{'econ':>7}{'val':>7}{'mood':>7}{'guid':>7}"
              f"{'COMP':>8}{'spread':>8}")
        print("-" * 56)
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT as_of FROM pillar_scores ORDER BY as_of")]
        for d in dates:
            row = {p: s for p, s in conn.execute(
                "SELECT pillar, score FROM pillar_scores WHERE as_of=? AND entity_id=?",
                (d, a.history))}
            vals = [row.get(p) for p in PILLARS]
            got = [v for v in vals if v is not None]
            spread = (max(got) - min(got)) if len(got) > 1 else None
            cells = "".join(f"{v:>7.2f}" if v is not None else f"{'—':>7}"
                            for v in vals)
            comp = row.get("composite")
            print(f"{d:12}{cells}"
                  f"{comp if comp is None else f'{comp:>8.2f}':>8}"
                  f"{spread if spread is None else f'{spread:>8.2f}':>8}")
        conn.close()
        return 0

    print(f"combined scores as of {as_of}\n")
    print(f"{'entity':16}{'econ':>7}{'val':>7}{'mood':>7}{'guid':>7}"
          f"{'COMP':>8}{'spread':>8}  disagreement")
    print("-" * 86)

    ents = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM pillar_scores WHERE as_of=? ORDER BY entity_id",
        (as_of,))]
    rows = []
    for eid in ents:
        row = {p: (s, w) for p, s, w in conn.execute(
            "SELECT pillar, score, withheld FROM pillar_scores "
            "WHERE as_of=? AND entity_id=?", (as_of, eid))}
        vals = [row.get(p, (None, None))[0] for p in PILLARS]
        got = [v for v in vals if v is not None]
        spread = (max(got) - min(got)) if len(got) > 1 else None
        comp = row.get("composite", (None, None))[0]
        cells = "".join(f"{v:>7.2f}" if v is not None else f"{'—':>7}" for v in vals)

        flag = ""
        if spread is not None and spread >= 1.5:
            hi = PILLARS[vals.index(max(got))]
            lo = PILLARS[vals.index(min(got))]
            flag = f"WIDE — {hi} {max(got):.1f} vs {lo} {min(got):.1f}"
        elif spread is not None and spread >= 0.8:
            flag = "moderate"
        # A withheld composite prints as a dash. The previous expression
        # evaluated to the bare None and then applied :>8 to it, so any date
        # with an unscored name raised TypeError instead of showing the row.
        c_txt = f"{comp:>8.2f}" if comp is not None else f"{'—':>8}"
        s_txt = f"{spread:>8.2f}" if spread is not None else f"{'—':>8}"
        print(f"{eid:16}{cells}{c_txt}{s_txt}  {flag}")
        if comp is not None:
            rows.append((eid, comp))

    if len(rows) > 1:
        rows.sort(key=lambda r: -r[1])
        print(f"\nranked: " + "  >  ".join(f"{e} {c:.2f}" for e, c in rows))
        best, worst = rows[0], rows[-1]
        print(f"widest composite gap: {best[0]} {best[1]:.2f} vs "
              f"{worst[0]} {worst[1]:.2f}  = {best[1]-worst[1]:.2f}")

    print("\nA withheld pillar is NOT scored 3.0 — the composite renormalises over")
    print("the pillars that exist, so missing data cannot masquerade as neutral.")
    print("Spread is the interesting column: a name where economics and valuation")
    print("point opposite ways is a different trade from one where they agree.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
