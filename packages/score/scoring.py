"""Map an EBITDA impact to the firm's 1-5 score, continuously.

THE PROBLEM WITH A DISCRETE 1-5: it is insensitive in the middle and jumpy at
the boundaries. A move from 2.9% to 3.1% of EBITDA should not "upgrade a 3 to a
4", and a 20% move should not read the same as an 8% one.

THE SHAPE WE WANT, in three properties:
  1. FLAT near zero        — daily price noise must not move the score
  2. STEEP in the decision zone (roughly 2-8% of EBITDA) — where trades are made
  3. FAT-TAILED            — never fully saturates, so extremes stay separable

Three candidate curves, all of the form  score = neutral + half_range * f(x):

  linear    f = clip(x/k, -1, 1)     fails all three: constant slope, hard clip
  tanh      f = tanh(x/k)            fails 1 (steepest AT zero) and 3 (saturates)
  hill      f = sgn(x)|x/k|^p        satisfies all three for p > 1:
                / (1 + |x/k|^p)      derivative at 0 is ZERO, tails decay as 1/x^p
                                     <-- RECOMMENDED, p = 1.5

CALIBRATION IS BY ANCHOR, NOT BY MAGIC CONSTANT. You state "an x_ref move in
EBITDA should read as score_ref" and k is solved for. That keeps the mapping
arguable in the desk's own language instead of hiding it in a coefficient.

Pleasant property of the hill form: at the anchor, k = x_ref exactly, for ANY p.
So p tunes the SHAPE (noise rejection vs tail separation) without moving the
anchor. Those two decisions stay independent, which is what makes it tunable.
"""

from __future__ import annotations

import math

NEUTRAL = 3.0        # firm convention: 3 = nothing happening
HALF_RANGE = 2.0     # so the scale spans [1, 5]


# ---------------------------------------------------------------------------
# curves
# ---------------------------------------------------------------------------


def _f_linear(x: float, k: float, p: float) -> float:
    return max(-1.0, min(1.0, x / k))


def _f_tanh(x: float, k: float, p: float) -> float:
    return math.tanh(x / k)


def _f_hill(x: float, k: float, p: float) -> float:
    if x == 0.0:
        return 0.0
    u = abs(x / k) ** p
    return math.copysign(u / (1.0 + u), x)


FORMS = {"linear": _f_linear, "tanh": _f_tanh, "hill": _f_hill}


# ---------------------------------------------------------------------------
# calibration — solve k from the anchor
# ---------------------------------------------------------------------------


def solve_k(form: str, x_ref: float, score_ref: float, p: float = 1.5) -> float:
    """k such that score(x_ref) == score_ref."""
    t = (score_ref - NEUTRAL) / HALF_RANGE          # target f value, in (0,1)
    if not 0.0 < t < 1.0:
        raise ValueError(f"score_ref {score_ref} must be strictly inside "
                         f"({NEUTRAL}, {NEUTRAL + HALF_RANGE})")
    if form == "hill":
        # t = u/(1+u) with u = (x_ref/k)^p  =>  u = t/(1-t)
        return x_ref / ((t / (1.0 - t)) ** (1.0 / p))
    if form == "tanh":
        return x_ref / math.atanh(t)
    if form == "linear":
        return x_ref / t
    raise ValueError(f"unknown form {form!r}")


def score(x: float, k: float, form: str = "hill", p: float = 1.5) -> float:
    """Map an EBITDA impact (as a FRACTION, 0.05 = +5%) to the 1-5 scale."""
    return NEUTRAL + HALF_RANGE * FORMS[form](x, k, p)


# ---------------------------------------------------------------------------
# accumulation — what x should actually be
# ---------------------------------------------------------------------------


def ewma_impact(daily_impacts: list[float], half_life_days: float = 10.0) -> float:
    """Exponentially-weighted sum of daily EBITDA impacts, most recent last.

    WHY NOT A FIXED WINDOW: a trailing 20-day sum changes every day purely
    because the oldest day drops out. That is a score move with no new
    information — exactly the artefact that makes a daily score untrustworthy.
    An EWMA has no cliff: old news fades smoothly instead of falling off.
    """
    lam = 0.5 ** (1.0 / half_life_days)
    total = 0.0
    for i, d in enumerate(reversed(daily_impacts)):
        total += d * (lam ** i)
    return total


def half_life_decay(days_since: float, half_life_days: float = 10.0) -> float:
    """Weight on a single impact `days_since` days old."""
    return 0.5 ** (days_since / half_life_days)


# ---------------------------------------------------------------------------
# calibration table — so the mapping can be argued with
# ---------------------------------------------------------------------------


def calibration_table(x_ref: float = 0.05, score_ref: float = 4.0,
                      p: float = 1.5) -> str:
    grid = [0.000, 0.002, 0.005, 0.010, 0.015, 0.025, 0.035, 0.050,
            0.075, 0.100, 0.150, 0.200, 0.300, 0.500]
    ks = {f: solve_k(f, x_ref, score_ref, p) for f in ("hill", "tanh", "linear")}

    out = [f"anchor: {x_ref:+.1%} of EBITDA reads as {score_ref:.1f}   (hill p={p})",
           "solved k:  " + "  ".join(f"{f}={v:.4f}" for f, v in ks.items()),
           "",
           f"{'ΔEBITDA':>9} | {'hill':>7} {'tanh':>7} {'linear':>7} | note",
           "-" * 62]

    notes = {
        0.000: "neutral",
        0.002: "noise    <- hill barely moves; tanh already drifting",
        0.005: "noise",
        0.015: "materiality floor",
        0.050: "ANCHOR",
        0.100: "big",
        0.200: "very big <- tanh saturated, hill still separating",
        0.500: "extreme  <- linear pinned at 5.0 since 10%",
    }
    for x in grid:
        row = f"{x:>8.1%} | " + " ".join(
            f"{score(x, ks[f], f, p):>7.2f}" for f in ("hill", "tanh", "linear")
        )
        out.append(f"{row} | {notes.get(x, '')}")

    # sensitivity: score points per 1% of EBITDA, locally
    out += ["", "local sensitivity (score points per +1% EBITDA):"]
    for x in (0.000, 0.010, 0.050, 0.150):
        h = 0.0005
        line = f"  at {x:>5.1%}: " + "  ".join(
            f"{f}={(score(x + h, ks[f], f, p) - score(x - h, ks[f], f, p)) / (2 * h) / 100:>5.2f}"
            for f in ("hill", "tanh", "linear")
        )
        out.append(line)
    return "\n".join(out)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor-x", type=float, default=0.05)
    ap.add_argument("--anchor-score", type=float, default=4.0)
    ap.add_argument("--p", type=float, default=1.5)
    a = ap.parse_args()
    print(calibration_table(a.anchor_x, a.anchor_score, a.p))
