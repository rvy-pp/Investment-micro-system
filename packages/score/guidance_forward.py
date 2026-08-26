"""P4 FORWARD — will management hit the commitments for a period NOT YET REPORTED.

WHY THIS EXISTS, AND WHAT IT DOES NOT REPLACE.

CLAUDE.md's pillar table defines P4 as "Will they hit the quarter? — forward
view". `guidance_runrate.py` does not answer that question. It computes

    achieved   = (sum of REPORTED periods / count reported) x n_periods
    gap        = achieved / target - 1
    confidence = sigmoid(gap x elapsed_fraction x SHARPNESS)

which needs a cited ACTUAL and sharpens as the period runs out. That answers
"given what has printed so far, are they on track in the period already
running" — a progress tracker on a period in flight. Useful, and NOT the
specified question.

The gap was visible in the output rather than in the code: Tata Steel's
commitments are Q2FY27 realisation -Rs1,500/t and coking coal +$5/t, i.e.
literally next quarter's guidance, and the run-rate scorer withheld them as
"no actual". For a next-quarter commitment there CAN be no actual until the
quarter ends, at which point it is history rather than a forward view. So the
one pillar meant to be forward-looking structurally could not score the one
kind of commitment that is forward-looking.

BOTH ARE KEPT. They answer different questions and the answers should be allowed
to disagree:

    guidance_runrate    in-flight period, >=1 reported     "are they on track"
    guidance_forward    unreported period, 0 reported      "will they hit it"

run_scores picks by whether the period has reported. A name can legitimately
have one and not the other; that is not a gap.

---------------------------------------------------------------------------
THE DESIGN CONSTRAINT THAT SHAPED THIS, AND IT IS THE INTERESTING PART
---------------------------------------------------------------------------
The obvious way to score a forward view off the vault's concall analysis is to
read the Thesis Drivers table in Coverage/<sector>/<name>/Knowledge Card.md and
use the `Stance` column (+ / - / 0). For Jindal Steel that column already says
what we want to know:

    | 6 | EXE | Slurry pipeline credibility | - | ~10th deadline; near-zero
      credibility for this commitment | Pipeline commissioned and delivers
      Rs 750-1,000/t savings | Jul 2026 |

THAT WOULD REINTRODUCE EXACTLY WHAT COMMIT 8a65338 REMOVED. The previous P4 was
deleted because it read `guidance_evidence.weight` — "a hand-assigned 0-1
judgement... Every other pillar computes from cited numbers; this one asks the
extractor how it feels." A `Stance` is the same object with a different name. If
P4 reads it, P4 is an opinion again and the whole inversion this project rests
on ("the model extracts cited facts, deterministic code scores them") is broken
for one pillar.

SO THE STANCE IS NOT SCORED. It is used only to SELECT which commitments are
execution commitments at all (Code == 'EXE'), which is a classification and not
a judgement about the outcome. The number comes from two deterministic places:

  PRIOR     the entity's DEMONSTRATED delivery rate, computed from guidance rows
            that have actually resolved (status in ('met','missed')). That is a
            counted fact, not an assessment. Shrunk toward NEUTRAL by count, so
            one resolved commitment does not become a track record.

  EVIDENCE  observable divergence from the guided path. Where a commitment names
            a metric this store carries a price series for, the guided delta can
            be compared against the REALISED delta so far in the period WITHOUT
            waiting for the company to report. Tata guided coking coal +$5/t for
            Q2FY27; the store knows what coking coal has actually done since the
            Q1 average. That is the forward test, and it needs no actual.

    confidence = sigmoid(logit(prior) + SUM(evidence))
    score      = 1 + 4 * confidence          (linear, per specs/scoring.yaml —
                                              a confidence is already bounded)

WITHHOLDING IS STILL THE DEFAULT. No prior and no observable evidence means no
score, per invariant 7. A commitment nobody can grade and nothing can observe is
an unanswered question, not a neutral one.

Usage:
    python packages/score/guidance_forward.py --entity tata_steel
    python packages/score/guidance_forward.py --peer-group steel_integrated
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

NEUTRAL_PRIOR = 0.5
PRIOR_SHRINK_N = 4.0     # resolved commitments needed before the record
                         # outweighs the neutral prior 1:1. Deliberately small
                         # but not 1: a single met commitment is not a habit.
EVIDENCE_SHARPNESS = 2.5  # logit units per 1.0 of relative divergence
MAX_EVIDENCE = 2.0        # cap, so one wild commodity print cannot pin the
                          # score at an extreme on its own

# (entity_id, metric) -> what the guide is a claim ABOUT.
#
# !! KEYED ON THE ENTITY, NOT ON THE METRIC ALONE, AND THE FIRST VERSION WAS NOT.
# !! It keyed on metric only, and `cost_per_t` means a different thing for every
# !! company: Tata's is imported coking coal, HZL's is zinc cost of production,
# !! NALCO's is aluminium CoP in rupees. So HZL's "$975-1,000/t" was differenced
# !! against COKING COAL and scored 4.12, and NALCO's "Rs1.6m/t" read as a
# !! "+1600000.0" guide against a 200 USD/t coal base. Plausible-looking output,
# !! no warning, three names scoring off a series with no connection to their
# !! guidance. Caught by reading the per-commitment detail rather than the score.
#
# !! `target_kind` IS THE SECOND HALF OF THE SAME BUG. Tata guides a CHANGE
# !! ("+$5/t coking coal in 2Q"); HZL guides a LEVEL ("$975-1,000/t cost of
# !! production"). The first version treated every target as a change, so a level
# !! of 987.5 became a claimed delta of +987.5. A level and a delta are not the
# !! same quantity and differencing one against the other is meaningless — the
# !! same numerator/denominator-basis class as VEDL's attributable-vs-consolidated
# !! caveat.
#
# UNDECLARED PAIRS WITHHOLD. There is no fallback and there must not be: a
# fallback here is a guess about what a company's cost guide refers to. Per
# invariant 7, withhold rather than guess.
#
# `worse_if_up` — a RISE in the driver makes the commitment HARDER to hit. True
# of a cost guide, false of a realisation guide.
#
# A VOLUME GUIDE CAN NEVER BE HERE. Nothing in `prices` observes tonnage, so a
# volume commitment gets the prior only and says so. Do not map one onto a price
# series to make it score.
OBSERVABLE = {
    ("tata_steel", "cost_per_t"): {
        "driver": "coking_coal_spot_aus", "worse_if_up": True,
        "target_kind": "delta",
        "note": "guides the qoq CHANGE in imported coking coal, in USD/t",
    },
    ("tata_steel", "margin"): {
        "driver": "hrc_india_inr", "worse_if_up": False,
        "target_kind": "delta",
        "note": "guides the qoq CHANGE in India realisation, in INR/t. HRC is a "
                "proxy for realisation, not the same series — Tata's realisation "
                "carries a value-added mix HRC does not.",
    },
    # ---------------------------------------------------------------------
    # DELIBERATELY ABSENT, with the reason, so nobody adds them casually:
    #
    #   (hindustan_zinc, cost_per_t)  zinc cost of production, a LEVEL in USD/t.
    #        No CoP series exists in the store. Its drivers are its own mining
    #        grade, power and royalty, not a traded price.
    #   (nalco, cost_per_t)           aluminium CoP, a LEVEL in INR/t. Same.
    #   (vaml, cost_per_t)            aluminium CoP, a LEVEL in USD/t. Same.
    #   (jsw_steel, cost_per_t)       coking coal inflation — WOULD map exactly
    #        like Tata's, but its only commitment is already resolved, so there
    #        is nothing open to score.
    #   any (*, volume)               unobservable, see above.
    #   any (*, capex)                a multi-year spend commitment; no series.
    # ---------------------------------------------------------------------
}


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1 - p))


# ---------------------------------------------------------------------------
# periods
# ---------------------------------------------------------------------------
def period_window(period: str):
    """(start, end, prior_start, prior_end) for 'Q2FY27' / 'FY27'.

    Indian fiscal year: FY27 is 2026-04-01 .. 2027-03-31, so Q1FY27 is
    Apr-Jun 2026. Getting this off by a quarter would compare a guide to the
    wrong window and the error would look like a company missing.
    """
    p = period.upper().replace("-", "")
    if p.startswith("FY") and len(p) == 4:
        y = 2000 + int(p[2:])
        return (dt.date(y - 1, 4, 1), dt.date(y, 3, 31),
                dt.date(y - 2, 4, 1), dt.date(y - 1, 3, 31))
    if len(p) == 6 and p[0] == "Q" and p[2:4] == "FY":
        q, y = int(p[1]), 2000 + int(p[4:])
        starts = {1: (y - 1, 4), 2: (y - 1, 7), 3: (y - 1, 10), 4: (y, 1)}
        sy, sm = starts[q]
        start = dt.date(sy, sm, 1)
        em = sm + 3
        ey = sy + (em - 1) // 12
        em = (em - 1) % 12 + 1
        end = dt.date(ey, em, 1) - dt.timedelta(days=1)
        pstart = start - dt.timedelta(days=92)
        return start, end, pstart, start - dt.timedelta(days=1)
    return None


# ---------------------------------------------------------------------------
# prior — demonstrated delivery, COUNTED not assessed
# ---------------------------------------------------------------------------
def delivery_prior(conn, entity_id: str, as_of: str):
    """(prior, n_resolved, detail). Shrunk toward NEUTRAL_PRIOR by count."""
    rows = conn.execute(
        "SELECT status FROM guidance WHERE entity_id=? AND status IN ('met','missed') "
        "AND IFNULL(resolved_date, issued_date) <= ?", (entity_id, as_of)).fetchall()
    n = len(rows)
    if not n:
        return NEUTRAL_PRIOR, 0, "no resolved commitments — neutral prior"
    met = sum(1 for r in rows if r[0] == "met")
    raw = met / n
    w = n / (n + PRIOR_SHRINK_N)
    prior = w * raw + (1 - w) * NEUTRAL_PRIOR
    return prior, n, f"{met}/{n} met, shrunk {raw:.2f}->{prior:.2f}"


# ---------------------------------------------------------------------------
# evidence — observable divergence from the guided path, no actual needed
# ---------------------------------------------------------------------------
def series_avg(conn, eid: str, start, end):
    r = conn.execute(
        "SELECT AVG(close) FROM prices WHERE entity_id=? AND date BETWEEN ? AND ? "
        "AND close IS NOT NULL", (eid, start.isoformat(), end.isoformat())).fetchone()
    return r[0] if r and r[0] is not None else None


def observable_evidence(conn, g: dict, as_of: str):
    """(evidence_logits, note) or (None, why not).

    Compares the GUIDED change against the change the market has ALREADY
    delivered in the elapsed part of the period. No company report required,
    which is the entire point.
    """
    spec = OBSERVABLE.get((g["entity_id"], g["metric"]))
    if not spec:
        return None, f"{g['entity_id']}/{g['metric']} not declared observable"
    win = period_window(g["period"])
    if not win:
        return None, f"period {g['period']} unmappable"
    start, end, pstart, pend = win
    as_of_d = dt.date.fromisoformat(as_of)
    if as_of_d < start:
        return None, "period has not started"
    prior_avg = series_avg(conn, spec["driver"], pstart, pend)
    so_far = series_avg(conn, spec["driver"], start, min(as_of_d, end))
    if not prior_avg or not so_far:
        return None, f"no {spec['driver']} history"

    guided = g.get("target_value")
    if guided is None:
        lo, hi = g.get("target_low"), g.get("target_high")
        if lo is None or hi is None:
            return None, "no point or range target"
        guided = (lo + hi) / 2.0

    # A DELTA GUIDE IS COMPARED TO A DELTA; A LEVEL GUIDE TO A LEVEL. Mixing them
    # is what produced the first version's nonsense — see the OBSERVABLE header.
    if spec["target_kind"] == "delta":
        realised = so_far - prior_avg
    elif spec["target_kind"] == "level":
        realised, prior_avg = so_far, guided or prior_avg
    else:
        return None, f"unknown target_kind {spec['target_kind']!r}"

    # Divergence relative to the PRIOR PERIOD LEVEL, so the number is unitless
    # and comparable across a USD/t coal guide and an INR/t realisation guide.
    # Dividing by the guide instead would explode whenever a guide is near zero.
    diverge = (realised - guided) / abs(prior_avg)
    ev = -EVIDENCE_SHARPNESS * diverge if spec["worse_if_up"] else \
        EVIDENCE_SHARPNESS * diverge
    ev = max(-MAX_EVIDENCE, min(MAX_EVIDENCE, ev))
    frac = (min(as_of_d, end) - start).days / max(1, (end - start).days)
    ev *= max(0.0, min(1.0, frac)) ** 0.5   # sqrt, so an early read counts for
                                            # something but not everything
    return ev, (f"{spec['driver']} guided {guided:+.1f} vs realised "
                f"{realised:+.1f} on a {prior_avg:,.0f} base "
                f"({frac:.0%} elapsed)")


# ---------------------------------------------------------------------------
def score_entity(conn, entity_id: str, as_of: str) -> tuple:
    """(score, confidence, detail, withheld_reason) for one entity+date."""
    # COLUMNS NAMED EXPLICITLY, and dicts built here rather than relying on the
    # caller having set conn.row_factory = sqlite3.Row. The CLI in this file sets
    # it; run_scores does not, so `SELECT *` plus dict(row) worked standalone and
    # raised TypeError inside the pipeline — a break that only appears on the path
    # that matters. guidance_runrate carries a defensive zip() for the same reason;
    # naming the columns is the version that cannot be got wrong.
    COLS = ("id", "entity_id", "period", "metric", "target_type", "target_value",
            "target_low", "target_high", "target_dir", "unit", "issued_date",
            "status", "quote")
    gs = [dict(zip(COLS, r)) for r in conn.execute(
        f"SELECT {', '.join(COLS)} FROM guidance WHERE entity_id=? "
        "AND status='open' AND issued_date<=? ORDER BY id",
        (entity_id, as_of)).fetchall()]
    if not gs:
        return None, None, None, "no open guidance"

    as_of_d = dt.date.fromisoformat(as_of)
    prior, n_res, prior_note = delivery_prior(conn, entity_id, as_of)

    confs, per, skipped = [], {}, []
    for g in gs:
        win = period_window(g["period"])
        if not win:
            skipped.append(f"{g['metric']}:{g['period']} unmappable period")
            continue
        start, end, _ps, _pe = win
        # FORWARD ONLY. A period that has already ENDED belongs to the run-rate
        # scorer, which grades it against a reported actual. Scoring a closed
        # period here would double-count it under a different question.
        if end < as_of_d:
            skipped.append(f"{g['metric']}:{g['period']} period closed")
            continue
        ev, note = observable_evidence(conn, g, as_of)
        if ev is None and n_res == 0:
            skipped.append(f"{g['metric']}:{g['period']} {note}, no track record")
            continue
        c = sigmoid(logit(prior) + (ev or 0.0))
        confs.append(c)
        per[f"{g['metric']}:{g['period']}"] = {
            "confidence": round(c, 3),
            "evidence_logits": None if ev is None else round(ev, 3),
            "why": note,
        }

    if not confs:
        return None, None, None, "; ".join(skipped) or "nothing forward-scoreable"
    conf = sum(confs) / len(confs)
    detail = {"prior": round(prior, 3), "prior_basis": prior_note,
              "n_resolved": n_res, "commitments": per}
    if skipped:
        detail["skipped"] = skipped
    return 1.0 + 4.0 * conf, conf, detail, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity")
    ap.add_argument("--peer-group")
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ents = [a.entity] if a.entity else [
        r[0] for r in conn.execute(
            "SELECT DISTINCT entity_id FROM guidance ORDER BY 1")]

    print(f"P4 FORWARD · as-of {a.as_of}\n")
    print(f"{'entity':18}{'prior':>7}{'n':>4}{'conf':>7}{'P4':>7}  detail")
    print("-" * 96)
    for eid in ents:
        s, c, d, w = score_entity(conn, eid, a.as_of)
        if s is None:
            print(f"{eid:18}{'—':>7}{'—':>4}{'—':>7}{'—':>7}  WITHHELD: {w}")
            continue
        print(f"{eid:18}{d['prior']:>7.2f}{d['n_resolved']:>4}{c:>7.2f}{s:>7.2f}"
              f"  {d['prior_basis']}")
        for k, v in d["commitments"].items():
            print(f"{'':18}  {k:26} conf {v['confidence']:.2f}  {v['why']}")
        for sk in d.get("skipped", []):
            print(f"{'':18}  skipped: {sk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
