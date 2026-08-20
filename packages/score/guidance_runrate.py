"""P4 guidance, scored from ACTUALS AGAINST TARGETS rather than from opinion.

WHAT THIS REPLACES, and why. The existing P4 reads `guidance_evidence.direction`
and `.weight` — a hand-assigned 0-1 judgement — and never touches the target
numbers at all. So HZL's committed 1.1mt, 680t and $975-1,000/t sit in the store
unused while the score is a sigmoid over votes somebody typed in. Two problems:

  1. `weight` IS a judgement written into the store. Every other pillar computes
     from cited numbers; this one asks the extractor how it feels. P1 has no
     weights and that is the design, not an omission.
  2. Three of the five evidence rows on HZL are "guidance reiterated". Management
     repeating itself is not information. Scoring it +0.3 means confidence drifts
     UP because they keep talking, which is the inverse of invariant 3.

Here the number does the work: an actual, cited, against a target, cited.

THE ARITHMETIC

    achieved   = (sum of REPORTED periods / count of REPORTED periods) * n_periods
    gap        = (achieved / target - 1) * polarity
    confidence = sigmoid(gap * elapsed_fraction * SHARPNESS)

TWO DIFFERENT NOTIONS OF "HOW FAR IN" AND THEY MUST NOT BE CONFLATED. The
annualisation divides by periods REPORTED — a statement about the data in hand.
The confidence weight uses time ELAPSED — a statement about how much of the year
is left to fix a gap. The first version used elapsed time for both and halved
HZL's single reported quarter, turning a -5.5% shortfall into -52.7%.

`polarity` is +1 where more is better (volume) and -1 where less is better (cost,
capex). Without it a cost beat scores as a miss.

`elapsed_fraction` is the honest part. A 5% shortfall in Q1 is weak evidence —
one bad quarter happens. The same shortfall in Q3 is nearly decisive. Confidence
therefore SHARPENS as the period runs out, with no per-name parameter to tune.
Early in a year the score sits near neutral however good the quarter was, which
is correct: they have not done the hard part yet.

WITHHOLDING IS THE DEFAULT. No cited actual for a metric means no score for that
commitment — invariant 7. A guidance line with a target and no actual is not
neutral evidence, it is an unanswered question, and the two must not read the
same. A `direction` target (Gamsberg commissioning "flat") has nothing to
compute a run-rate from and is withheld too; those belong on the evidence path.

Usage:
    python packages/score/guidance_runrate.py --entity hindustan_zinc
    python packages/score/guidance_runrate.py --entity hindustan_zinc --as-of 2026-12-31
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

SHARPNESS = 12.0     # how hard a given gap bites once the period is fully elapsed

# More is better (+1) or less is better (-1). A metric absent here is REFUSED
# rather than assumed: guessing the sign of a cost target inverts the score.
POLARITY = {
    "volume": +1, "silver_volume": +1, "alumina_volume": +1, "ebitda": +1,
    "ebitda_per_t": +1, "margin": +1, "realisation": +1,
    "cost_per_t": -1, "capex": -1, "net_debt": -1,
}


def fy_window(period: str) -> tuple[dt.date, dt.date, int] | None:
    """'FY27' -> Apr 2026..Mar 2027, 4 quarters. 'Q2FY27' -> Jul..Sep 2026, 1.

    Indian fiscal years run April to March, so FY27 STARTS in calendar 2026.
    Reading FY27 as calendar 2027 shifts every window by nine months and every
    elapsed_fraction with it.
    """
    p = period.upper().replace(" ", "")
    q = None
    if p.startswith("Q") and "FY" in p:
        q, p = int(p[1]), p[p.index("FY"):]
    if not p.startswith("FY"):
        return None
    try:
        yy = int(p[2:])
    except ValueError:
        return None
    end_cal = 2000 + yy                       # FY27 ends March 2027
    start = dt.date(end_cal - 1, 4, 1)
    if q:
        s = dt.date(end_cal - 1, 4, 1) + dt.timedelta(days=0)
        m = 4 + 3 * (q - 1)
        yr = end_cal - 1 + (0 if m <= 12 else 1)
        m = m if m <= 12 else m - 12
        s = dt.date(yr, m, 1)
        e_m, e_y = m + 3, yr
        if e_m > 12:
            e_m, e_y = e_m - 12, yr + 1
        return s, dt.date(e_y, e_m, 1) - dt.timedelta(days=1), 1
    return start, dt.date(end_cal, 3, 31), 4


def gap_of(g: dict, achieved: float, pol: int) -> tuple[float | None, str]:
    """Signed gap against the target, positive = better than promised.

    A RANGE IS A BAND, NOT A MIDPOINT, and this distinction is not cosmetic.
    VAML guided aluminium COP $1,650-1,700/t and delivered $1,698 — the concall
    grades that "Delivered (in band)". Measuring against the $1,675 midpoint
    scores it 1.4% BEHIND, penalising a company for landing in the upper half of
    its own stated range. Management committed to the band, so anywhere inside it
    is the commitment met: gap 0, confidence 0.5, score 3.0 = "did what they
    said". Outside the band, distance is measured to the NEAREST EDGE, because
    that is the promise that was broken.

    A point target has no band, so it is a plain ratio.
    """
    if g["target_type"] == "point":
        t = g["target_value"]
        if not t:
            return None, "point target with no value"
        return (achieved / t - 1.0) * pol, f"point {t:g}"
    if g["target_type"] == "range":
        lo, hi = g["target_low"], g["target_high"]
        if lo is None or hi is None:
            return None, "range target missing an edge"
        if lo <= achieved <= hi:
            return 0.0, f"IN BAND {lo:g}-{hi:g} — commitment met"
        edge = lo if achieved < lo else hi
        return (achieved / edge - 1.0) * pol, f"outside band {lo:g}-{hi:g}, vs edge {edge:g}"
    return None, "direction target — no number to run-rate against"


# ---------------------------------------------------------------------------
# callable form, used by run_scores.py so there is ONE P4 arithmetic
# ---------------------------------------------------------------------------

# Guidance whose target embeds a mid-year capacity ramp. Annualising a quarter
# from BEFORE the ramp starts assumes a flat run-rate the company never promised
# and reports a large false miss: vedanta's Gamsberg FY27 280-300kt depends on
# Phase 2 starting August 2026, so Q1's 45kt (Phase 1 only) annualises to 180kt
# and scores -35.7% — while the concall grades Phase 1 "Delivered". Withheld
# until the ramp has been running long enough for an annualisation to mean
# something. Keyed (entity, metric) -> the date the ramp begins.
RAMP_DEPENDENT = {
    ("vedanta", "volume"): "2026-08-01",   # Gamsberg Phase 2
}
RAMP_GRACE_DAYS = 120      # ~one full quarter of the ramp before annualising


def score_entity(conn, entity_id: str, as_of: str) -> tuple:
    """(score, mean_confidence, detail, withheld_reason) for one entity+date."""
    as_of_d = dt.date.fromisoformat(as_of)
    gs = conn.execute(
        "SELECT * FROM guidance WHERE entity_id=? AND status='open' "
        "AND issued_date<=? ORDER BY id", (entity_id, as_of)).fetchall()
    if not gs:
        return None, None, None, "no open guidance"

    confs, detail, skipped = [], {}, []
    for g in gs:
        g = dict(zip([c[0] for c in conn.execute(
            "SELECT * FROM guidance LIMIT 0").description], g)) if not isinstance(
            g, dict) and not hasattr(g, "keys") else dict(g)
        label = f"{g['metric']}:{g['period']}"
        if g["target_type"] == "direction":
            skipped.append(f"{label} direction")
            continue
        win = fy_window(g["period"])
        pol = POLARITY.get(g["metric"])
        if win is None or pol is None:
            skipped.append(f"{label} unmappable")
            continue
        ramp = RAMP_DEPENDENT.get((entity_id, g["metric"]))
        if ramp and (as_of_d - dt.date.fromisoformat(ramp)).days < RAMP_GRACE_DAYS:
            skipped.append(f"{label} ramp-dependent")
            continue
        acts = conn.execute(
            "SELECT value_num, period FROM observations WHERE entity_id=? "
            "AND factor='actual' AND metric=? AND as_of<=?",
            (entity_id, g["metric"], as_of)).fetchall()
        acts = [r for r in acts if r[1] and g["period"][-4:] in r[1]]
        if not acts:
            skipped.append(f"{label} no actual")
            continue
        start, end, nper = win
        frac = max(0.0, min(1.0, (min(as_of_d, end) - start).days /
                            (end - start).days))
        by_period = {r[1]: r[0] for r in acts}
        vals = list(by_period.values())
        cumulative = g["metric"] == "volume" or g["metric"].endswith("_volume")
        achieved = ((sum(vals) / len(vals)) * nper if cumulative
                    else sum(vals) / len(vals))
        gap, _how = gap_of(g, achieved, pol)
        if gap is None:
            skipped.append(f"{label} no target")
            continue
        c = 1.0 / (1.0 + math.exp(-gap * frac * SHARPNESS))
        confs.append(c)
        detail[label] = round(gap, 4)

    if not confs:
        return None, None, None, "; ".join(skipped) or "nothing computable"
    conf = sum(confs) / len(confs)
    return (1.0 + 4.0 * conf, conf,
            {"gaps": detail, "n": len(confs), "skipped": skipped}, None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", required=True)
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    a = ap.parse_args()
    as_of = dt.date.fromisoformat(a.as_of)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    gs = conn.execute(
        "SELECT * FROM guidance WHERE entity_id=? AND status='open' "
        "AND issued_date<=? ORDER BY id", (a.entity, a.as_of)).fetchall()
    if not gs:
        print(f"{a.entity}: no open guidance on or before {a.as_of}")
        return 0

    print(f"{a.entity}   as of {a.as_of}\n")
    scored, withheld = [], []
    for g in gs:
        win = fy_window(g["period"])
        label = f"{g['metric']} {g['period']}"
        if g["target_type"] == "direction":
            withheld.append((label, "direction target — no number to run-rate against"))
            continue
        if win is None:
            withheld.append((label, f"cannot parse period {g['period']!r}"))
            continue
        pol = POLARITY.get(g["metric"])
        if pol is None:
            withheld.append((label, f"metric {g['metric']!r} has no polarity — "
                                    f"refusing to guess whether more is better"))
            continue

        start, end, nper = win
        # Cited actuals for this metric and period-set, from observations.
        acts = conn.execute(
            "SELECT as_of, value_num, period, quote FROM observations "
            "WHERE entity_id=? AND factor='actual' AND metric=? AND as_of<=? "
            "ORDER BY as_of", (a.entity, g["metric"], a.as_of)).fetchall()
        acts = [r for r in acts if r["period"] and g["period"][-4:] in r["period"]]
        if not acts:
            withheld.append((label, "no cited actual yet — unanswered, not neutral"))
            continue

        elapsed_days = (min(as_of, end) - start).days
        total_days = (end - start).days
        frac = max(0.0, min(1.0, elapsed_days / total_days))

        # ANNUALISE BY PERIODS REPORTED, NOT BY TIME ELAPSED. These are two
        # different quantities and conflating them is a large silent error: the
        # first version divided one reported quarter by round(0.39*4)=2 because
        # 39% of the year had passed, halving HZL's 260 KT to a 520 KT
        # annualised figure and reporting -52.7% against the 1.1 Mt target. The
        # concall's own arithmetic — "Q1 260 KT annualises ~1.04 Mt" — says
        # -5.5%. It produced a confident 1.32 score for a company 5% behind.
        #
        # Time elapsed still governs CONFIDENCE (how much of the year is left to
        # fix a gap). It must never govern the annualisation, which is a
        # statement about the data in hand.
        #
        # Distinct PERIODS, not row count: two brokers citing the same Q1 must
        # not read as two quarters of output.
        by_period: dict[str, float] = {}
        for r in acts:
            by_period[r["period"]] = r["value_num"]
        n_reported = len(by_period)
        vals = list(by_period.values())

        # A rate metric (cost per tonne) is an average across reported periods;
        # a volume is a cumulative total to be scaled to the full period set.
        cumulative = g["metric"] == "volume" or g["metric"].endswith("_volume")
        achieved = ((sum(vals) / n_reported) * nper if cumulative
                    else sum(vals) / n_reported)
        gap, how = gap_of(dict(g), achieved, pol)
        if gap is None:
            withheld.append((label, how))
            continue
        conf = 1.0 / (1.0 + math.exp(-gap * frac * SHARPNESS))
        score = 1.0 + 4.0 * conf
        scored.append((label, achieved, gap, frac, conf, score, acts))

        print(f"  {label}")
        print(f"    target                   ({how})")
        print(f"    achieved    {achieved:>12,.2f}   from {n_reported} reported "
              f"period(s), {'annualised x' + str(nper) if cumulative else 'averaged'}")
        print(f"    gap         {gap*100:>+11.1f}%   ({'ahead' if gap>0 else 'behind'}, "
              f"polarity {pol:+d})")
        print(f"    period      {start} .. {end}   {frac*100:.0f}% elapsed")
        print(f"    confidence  {conf:>12.3f}   -> P4 score {score:.2f}")
        for r in acts:
            print(f"      [{r['as_of']}] {r['period']}  {r['value_num']:,.2f}  "
                  f"\"{r['quote'][:72]}\"")
        print()

    if withheld:
        print("  WITHHELD")
        for lab, why in withheld:
            print(f"    {lab:28} {why}")
        print()

    if scored:
        conf = sum(s[4] for s in scored) / len(scored)
        print(f"  P4 = {1+4*conf:.2f}   mean confidence {conf:.3f} over "
              f"{len(scored)} computable commitment(s), {len(withheld)} withheld")
        print(f"\n  Note the elapsed weighting: at {scored[0][3]*100:.0f}% through the "
              f"period a\n  {scored[0][2]*100:+.1f}% gap moves confidence only to "
              f"{scored[0][4]:.3f}. The same gap at\n  100% elapsed would give "
              f"{1/(1+math.exp(-scored[0][2]*SHARPNESS)):.3f}.")
    else:
        print("  P4 WITHHELD — no commitment has a cited actual to measure against.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
