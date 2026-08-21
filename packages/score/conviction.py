"""The composite as the specs describe it: economics sets direction, the rest scale it.

WHY NOT A WEIGHTED AVERAGE. `combined.py` blends four pillars at 0.45/0.25/0.15/
0.15, which contradicts this repo's own architecture in two places:

    README.md:91  "L2 never sets direction. Summing consensus mood into a score
                   is how a system ends up recommending the most crowded name —
                   the bullishness IS the reason the move already happened. It
                   adjusts conviction and size, and can veto entry."
    README.md:95  "L3 is a permission layer, not another additive term."

Under an average, mood at 0.15 CAN decide the sign on its own. It is setting
direction, which the spec forbids.

It is also a category error. CLAUDE.md's own table says the pillars determine
different KINDS of thing — economics gives direction + size, valuation gives
conviction, guidance gives a forward view, the gate gives permission. Averaging a
direction with a conviction is arithmetically fine and means nothing. The tell
that something is wrong is that `combined.py` has to report `spread` separately
to warn you the average lost information.

THE FORM

    size = (economics - 3) x f(valuation) x g(guidance) x gate

  economics - 3   the ONLY term carrying a sign. Positive = the economics
                  improved, negative = worsened, zero = nothing happened.
  f(valuation)    conviction multiplier. Cheap amplifies, expensive shrinks.
  g(guidance)     forward-view multiplier. Delivering amplifies, missing shrinks.
  gate            permission, 0 or 1. Not built yet, so 1 with a note.

Properties this has and the average does not:

  - Mood and valuation CANNOT create a signal, only scale one. Direction comes
    from the business, never from sentiment.
  - Positive economics into an expensive stock is a SMALL position by
    construction, not a mid-range score needing a spread column to explain it.
  - A withheld pillar is a multiplier of 1.0 — neutral by construction rather
    than by renormalising a denominator.
  - "Nothing is happening" maps to ~zero size instead of a confident-looking 3.

MULTIPLIER SHAPE, and why it is bounded. A multiplier is capped in [MIN, MAX] so
no single qualifier can dominate or invert the call. Valuation at 5.0 cannot turn
a weak economics signal into a strong one — it can at most double it. Nothing
inverts: a qualifier that could flip the sign would be setting direction again.

WHAT THIS IS NOT. It is not validated. Seven independent observations cannot
separate multiplicative from additive any more than they can separate a 0.45
weight from 0.40. This is a bet on the stated architecture, not on evidence — but
the average is ALSO unvalidated and additionally contradicts the spec. Given a
choice of two unvalidated rules, prefer the one the design argues for.

Usage:
    python packages/score/conviction.py
    python packages/score/conviction.py --as-of 2026-08-14 --compare
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

MULT_MIN, MULT_MAX = 0.40, 1.60   # no qualifier may dominate or invert
AVG_W = {"economics": 0.45, "valuation": 0.25, "mood": 0.15, "guidance": 0.15}


def multiplier(score: float | None, strength: float = 0.30) -> tuple[float, str]:
    """Map a 1-5 qualifier onto a bounded multiplier centred on 1.0.

    A withheld pillar returns exactly 1.0: it neither helps nor hurts, which is
    the honest treatment of "we do not know" for a term whose job is to scale.
    Note this differs from the average, where a missing pillar changes the
    DENOMINATOR and therefore silently reweights everything else.
    """
    if score is None:
        return 1.0, "withheld -> neutral"
    m = 1.0 + (score - 3.0) / 2.0 * strength * 2.0
    return max(MULT_MIN, min(MULT_MAX, m)), f"{score:.2f}"


def conviction(pillars: dict) -> dict:
    econ = pillars.get("economics")
    if econ is None:
        return {"size": None, "why": "economics withheld — nothing sets direction"}
    base = econ - 3.0                      # the only signed term
    fv, sv = multiplier(pillars.get("valuation"))
    fg, sg = multiplier(pillars.get("guidance"))
    # Mood is deliberately NOT a term. Per README:91 it adjusts conviction and
    # can veto, but this repo has no veto mechanism yet, and folding it in as a
    # multiplier would let sentiment amplify a call — which is the failure the
    # line warns about. Reported, unused.
    gate = 1.0                             # regime gate not built (docs/FLOWS.md)
    size = base * fv * fg * gate
    return {"size": size, "base": base, "f_val": fv, "f_guid": fg,
            "gate": gate, "s_val": sv, "s_guid": sg,
            "why": "economics x valuation x guidance x gate"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DB)
    as_of = a.as_of or conn.execute(
        "SELECT MAX(as_of) FROM pillar_scores WHERE pillar='composite' "
        "AND score IS NOT NULL").fetchone()[0]

    names = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM pillar_scores WHERE as_of=? "
        "AND pillar='composite' ORDER BY entity_id", (as_of,))]
    rows = []
    for eid in names:
        p = {k: v for k, v in conn.execute(
            "SELECT pillar, score FROM pillar_scores WHERE as_of=? AND entity_id=?",
            (as_of, eid))}
        c = conviction(p)
        got = {k: p.get(k) for k in AVG_W if p.get(k) is not None}
        wsum = sum(AVG_W[k] for k in got)
        avg = sum(p[k] * AVG_W[k] for k in got) / wsum if got else None
        rows.append((eid, p, c, avg, wsum))

    print(f"conviction sizing vs weighted average — {as_of}\n")
    print(f"{'entity':16}{'econ':>7}{'base':>7}{'x val':>8}{'x guid':>8}"
          f"{'gate':>6}{'SIZE':>9}   {'avg':>6}{'cov':>6}")
    print("-" * 80)
    for eid, p, c, avg, wsum in sorted(rows, key=lambda r: -(r[2]["size"] or -99)):
        if c["size"] is None:
            print(f"{eid:16}{'—':>7}   {c['why']}")
            continue
        print(f"{eid:16}{p['economics']:>7.2f}{c['base']:>+7.2f}"
              f"{c['f_val']:>8.2f}{c['f_guid']:>8.2f}{c['gate']:>6.0f}"
              f"{c['size']:>+9.3f}   {avg:>6.2f}{wsum:>6.2f}")

    print(f"""
HOW TO READ IT. `SIZE` is a signed position size in arbitrary units, not a 1-5
score. Zero means do nothing. Its SIGN comes only from economics; valuation and
guidance can make a call bigger or smaller but never flip it.

Compare the two right-hand columns against SIZE. The average compresses
everything toward 3 and needs `spread` to tell you when it is hiding a fight.
Sizing puts the disagreement in the number: positive economics into an expensive
name is a small position, and it looks small.

MOOD IS DELIBERATELY ABSENT. README:91 says it adjusts conviction and can veto,
not that it scales the call. Folding it in as a multiplier would let sentiment
amplify a signal, which is the exact failure that line warns about. It is
computed and shown elsewhere; it does not enter sizing until there is a veto
mechanism to attach it to.

THE GATE IS 1 EVERYWHERE because the regime layer is not built (docs/FLOWS.md).
That is a placeholder, not a judgement that permission is always granted.""")

    if a.compare:
        print("\nRANK AGREEMENT")
        by_size = [r[0] for r in sorted(rows, key=lambda r: -(r[2]["size"] or -99))]
        by_avg = [r[0] for r in sorted(rows, key=lambda r: -(r[3] or -99))]
        print(f"  by SIZE : {' > '.join(by_size)}")
        print(f"  by avg  : {' > '.join(by_avg)}")
        print(f"  {'IDENTICAL ranking' if by_size == by_avg else 'RANKINGS DIFFER'}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
