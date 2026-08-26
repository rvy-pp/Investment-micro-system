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
conviction, guidance gives a forward view. Averaging a direction with a
conviction is arithmetically fine and means nothing. (CLAUDE.md's table lists a
fourth row, the in-flavour/out-of-flavour gate, as "permission". That is now
Flows' business and not a scoring term at all — see below.) The tell
that something is wrong is that `combined.py` has to report `spread` separately
to warn you the average lost information.

THE FORM

    size = (economics - 3) x f(valuation) x g(guidance)

  economics - 3   the ONLY term carrying a sign. Positive = the economics
                  improved, negative = worsened, zero = nothing happened.
  f(valuation)    conviction multiplier. Cheap amplifies, expensive shrinks.
  g(guidance)     forward-view multiplier. Delivering amplifies, missing shrinks.

THE L3 REGIME GATE IS GONE, removed 2026-08-24 at the PM's instruction. It used
to sit here as a fourth term, permanently 1.0 with a "not built yet" note.

Removing it is not a loss of function, because it never had any: a term fixed at
1.0 for the life of the file multiplied nothing. What it did do was misdescribe
the model. Every printed SIZE carried a `gate` column of 1, which reads as "checked
and permitted" rather than "never computed" — and the docstring promised a
permission layer the code did not have. A placeholder that looks like a measurement
is the failure shape this repo keeps finding, and this was one sitting in the
sizing formula itself.

Flows keeps its own section (docs/FLOWS.md, the Flows tab) and stays OUT of
scoring by design. Its whole argument for existing is that a flow reading added as
a weighted term makes the system recommend whatever is most crowded. So Flows
answers "can this be expressed" for a human reading the tab; it does not multiply
into a number.

Properties this has and the average does not:

  - Mood and valuation CANNOT create a signal, only scale one. Direction comes
    from the business, never from sentiment.
  - Positive economics into an expensive stock is a SMALL position by
    construction, not a mid-range score needing a spread column to explain it.
  - A withheld pillar is a multiplier of 1.0 — neutral by construction rather
    than by renormalising a denominator. NOTE this is the honest use of a 1.0:
    it is a real answer to "we do not know about a scaling term". The old `gate`
    was a 1.0 standing in for a computation that had never been written, which
    is a different thing and is why it is gone.
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
# Kept in step with run_scores.WEIGHTS by hand — two copies of one set of
# numbers, which is its own hazard. Guidance zeroed and the 0.15 redistributed
# equally, PM decision 2026-08-25; see the long note in run_scores.py.
#
# NOTE THIS FILE USES GUIDANCE TWICE, and only ONE use is zeroed. AVG_W is the
# comparison average and now excludes guidance. The `multiplier()` path below
# still applies g(guidance) to the SIZE. That inconsistency is deliberate and
# flagged, not resolved: dropping guidance from sizing is a separate call.
AVG_W = {"economics": 0.50, "valuation": 0.30, "mood": 0.20, "guidance": 0.00}


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
    #
    # No `gate` term. See the docstring: it was a permanent 1.0 that made an
    # uncomputed permission layer look like a checked one. Flows is a section,
    # not a coefficient.
    size = base * fv * fg
    return {"size": size, "base": base, "f_val": fv, "f_guid": fg,
            "s_val": sv, "s_guid": sg,
            "why": "economics x valuation x guidance"}


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
        # `if wsum` not `if got` — with a zero weight in the table, `got` can be
        # non-empty while wsum is 0.0.
        avg = sum(p[k] * AVG_W[k] for k in got) / wsum if wsum else None
        rows.append((eid, p, c, avg, wsum))

    print(f"conviction sizing vs weighted average — {as_of}\n")
    print(f"{'entity':16}{'econ':>7}{'base':>7}{'x val':>8}{'x guid':>8}"
          f"{'SIZE':>9}   {'avg':>6}{'cov':>6}")
    print("-" * 80)
    for eid, p, c, avg, wsum in sorted(rows, key=lambda r: -(r[2]["size"] or -99)):
        if c["size"] is None:
            print(f"{eid:16}{'—':>7}   {c['why']}")
            continue
        print(f"{eid:16}{p['economics']:>7.2f}{c['base']:>+7.2f}"
              f"{c['f_val']:>8.2f}{c['f_guid']:>8.2f}"
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

THERE IS NO GATE TERM. It was removed 2026-08-24: it had been a permanent 1.0,
which printed as though permission had been checked when it had never been
computed. Flows (docs/FLOWS.md, and its own tab) answers whether a call can be
expressed, for a human — it never multiplies into SIZE.""")

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
