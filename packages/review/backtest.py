"""L5 review — does a pillar score predict the forward RELATIVE move?

THE GATE THIS ANSWERS, AND THE ONE IT DOES NOT. CLAUDE.md holds that nothing
should extend to a second sector until the stored scores are tested against
realised moves. This is that test. It is deliberately built as a FALSIFIER, not
as a validator: with 40 dates over 5 names in one correlated complex there are
roughly 40/h independent observations, so nothing here can confirm that a weight
of 0.45 beats 0.40. What it CAN catch is a pillar whose sign is consistently
wrong, which is the failure worth finding before anything is built on top.

WHY RELATIVE AND NOT ABSOLUTE. The five names are one aluminium/zinc complex and
their absolute returns are dominated by a common sector factor. Testing absolute
return would mostly measure "was the complex up", which no pillar claims to
predict. The book is long/short, so each name's return is demeaned across the
FULL five-name universe — an equal-weight sector hedge, which is the benchmark
the book actually trades against. Demeaning over only the names that happen to
carry a score would move the benchmark around as coverage changes.

RANK CORRELATION, NOT LEVELS. The scoring curve is flat in the tails by design
(invariant 4 — score the spread, do not spread the scores), so score DIFFERENCES
understate real gaps and a levels regression would be measuring the curve as
much as the signal. Spearman on the cross-section avoids that.

Usage:
    python packages/review/backtest.py
    python packages/review/backtest.py --horizons 1,3,5,10 --json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

# The tradeable book. Reporting units (novelis) carry economics but are never
# scored, so they are not part of the hedge universe.
UNIVERSE = ["hindalco", "hindustan_zinc", "nalco", "vaml", "vedanta"]
PILLARS = ["composite", "economics", "valuation", "mood", "guidance"]

# A cross-section needs at least three names for a rank correlation to carry any
# information at all; with two it can only ever be +1 or -1.
MIN_NAMES = 3

# VEDL's price history has an UNADJUSTED demerger on this date (773.60 ->
# 271.55, -64.9%). Any return window crossing it compares two different
# companies. The score window starts well after, but assert rather than assume —
# a later backfill of earlier scores would silently reintroduce it.
VEDL_DEMERGER = "2026-04-30"


def load(conn: sqlite3.Connection) -> tuple[list[str], dict, dict]:
    """Return (calendar, closes[(eid,date)], scores[(pillar,date)][eid])."""
    ph = ",".join("?" * len(UNIVERSE))

    rows = list(conn.execute(
        f"SELECT entity_id, date, close FROM prices "
        f"WHERE entity_id IN ({ph}) AND close IS NOT NULL", UNIVERSE))
    closes = {(e, d): c for e, d, c in rows}

    # One shared trading calendar, and it must BE shared — a per-entity calendar
    # would make "h trading days forward" mean different dates for different
    # names, which silently misaligns the cross-section.
    by_entity = {e: sorted(d for (x, d) in closes if x == e) for e in UNIVERSE}
    cal = by_entity[UNIVERSE[0]]
    ragged = {e: len(v) for e, v in by_entity.items() if v != cal}

    scores: dict[tuple[str, str], dict[str, float]] = {}
    for pillar, as_of, eid, score in conn.execute(
            f"SELECT pillar, as_of, entity_id, score FROM pillar_scores "
            f"WHERE score IS NOT NULL AND entity_id IN ({ph})", UNIVERSE):
        scores.setdefault((pillar, as_of), {})[eid] = score

    # Trim the calendar to the span the scores actually cover, so `ragged`
    # reports misalignment where it matters rather than at VAML's 2026-06-15
    # listing date.
    score_dates = sorted({d for (_, d) in scores})
    if score_dates:
        lo = score_dates[0]
        cal = [d for d in cal if d >= lo]
        ragged = {e: len(v) for e, v in by_entity.items()
                  if [d for d in v if d >= lo] != cal}
    if ragged:
        raise SystemExit(
            f"price calendars disagree inside the score window: {ragged} vs "
            f"{len(cal)} for {UNIVERSE[0]}. Align them before trusting a result.")
    return cal, closes, scores


def ranks(vals: list[float]) -> list[float]:
    """Average ranks, so ties do not fabricate an ordering.

    Ties are real here: hindalco and vaml both composite 2.37 on 2026-08-14.
    Assigning them an arbitrary order would invent information.
    """
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Pearson on average ranks. None when either side is entirely tied."""
    rx, ry = ranks(xs), ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def fwd_relative(cal: list[str], closes: dict, t: int, h: int,
                 universe: list[str] | None = None) -> dict[str, float] | None:
    """Forward return over h TRADING days, demeaned across the full universe.

    `universe` is threaded explicitly rather than read from the module global so
    that leave-one-out can rebuild the HEDGE as well as the ranking. Dropping a
    name from the ranking but still demeaning against it would answer a question
    nobody asked.

    Trading-day steps, not calendar arithmetic: the calendar is verified shared
    above, so index arithmetic on it is well defined for every name.
    """
    if t + h >= len(cal):
        return None
    d0, d1 = cal[t], cal[t + h]
    # Only meaningful if VEDL is actually in the book being measured — the guard
    # is about one name's tape, not about the date. Firing it universe-blind
    # killed a P1 run over a universe that excluded vedanta entirely.
    if "vedanta" in (universe or UNIVERSE) and d0 < VEDL_DEMERGER <= d1:
        raise SystemExit(f"window {d0}..{d1} crosses the VEDL demerger "
                         f"({VEDL_DEMERGER}) — returns would be meaningless.")
    rets = {}
    for e in (universe or UNIVERSE):
        a, b = closes.get((e, d0)), closes.get((e, d1))
        if a is None or b is None or a == 0:
            return None                      # incomplete hedge, skip the date
        rets[e] = b / a - 1.0
    mean = sum(rets.values()) / len(rets)
    return {e: r - mean for e, r in rets.items()}


def run(pillar: str, h: int, cal: list[str], closes: dict, scores: dict,
        universe: list[str] | None = None) -> dict:
    uni = universe or UNIVERSE
    idx = {d: i for i, d in enumerate(cal)}
    ics: list[tuple[str, float]] = []
    spreads: list[float] = []

    for (p, d), by_e in sorted(scores.items()):
        if p != pillar or d not in idx:
            continue
        rel = fwd_relative(cal, closes, idx[d], h, uni)
        if rel is None:
            continue
        names = [e for e in by_e if e in rel]
        if len(names) < MIN_NAMES:
            continue
        ic = spearman([by_e[e] for e in names], [rel[e] for e in names])
        if ic is not None:
            ics.append((d, ic))
        # The tradeable expression: top-scored long, bottom-scored short.
        top = max(names, key=lambda e: by_e[e])
        bot = min(names, key=lambda e: by_e[e])
        if by_e[top] != by_e[bot]:
            spreads.append(rel[top] - rel[bot])

    vals = [v for _, v in ics]
    res = {"pillar": pillar, "h": h, "n_dates": len(vals),
           "mean_ic": None, "sd_ic": None, "t_naive": None, "t_adj": None,
           "hit_rate": None, "n_indep": None, "mean_ic_indep": None,
           "mean_spread_pct": None, "n_spread": len(spreads)}
    if not vals:
        return res

    res["mean_ic"] = statistics.fmean(vals)
    res["hit_rate"] = sum(1 for v in vals if v > 0) / len(vals)
    if spreads:
        res["mean_spread_pct"] = statistics.fmean(spreads) * 100.0
    if len(vals) > 1:
        sd = statistics.stdev(vals)
        res["sd_ic"] = sd
        if sd > 0:
            res["t_naive"] = res["mean_ic"] / (sd / len(vals) ** 0.5)
            # Overlapping windows share h-1 days of return, so the naive t is
            # inflated by roughly sqrt(h). Deflating it is still generous, but
            # it stops the headline number being read as significance.
            res["t_adj"] = res["t_naive"] / h ** 0.5

    # Non-overlapping subsample: stride h, averaged over every start offset, so
    # the answer does not depend on which Monday the sample happens to begin.
    sub = [statistics.fmean(vals[o::h]) for o in range(min(h, len(vals)))
           if len(vals[o::h]) > 1]
    if sub:
        res["n_indep"] = len(vals) // h
        res["mean_ic_indep"] = statistics.fmean(sub)
    return res


def concentration(pillar: str, h: int, cal: list[str], closes: dict,
                  scores: dict) -> list[dict]:
    """Per-name mean score, mean rank and mean forward relative return.

    THIS TABLE IS NOT OPTIONAL, and it is printed with the IC rather than behind
    a flag, because the IC alone is genuinely misleading at this sample size. On
    the first run the headline read "every pillar is negative across horizons",
    which looks like an inverted spec. It is one name: the top-ranked name posted
    the worst relative move over a single two-month window, while the
    second-ranked name posted the best. An IC aggregated over five correlated
    names cannot tell those two situations apart. This table can.
    """
    idx = {d: i for i, d in enumerate(cal)}
    agg: dict[str, dict[str, list[float]]] = {}
    for (p, d), by_e in scores.items():
        if p != pillar or d not in idx:
            continue
        fw = fwd_relative(cal, closes, idx[d], h)
        if fw is None:
            continue
        present = [e for e in UNIVERSE if e in by_e]
        if len(present) < MIN_NAMES:
            continue
        rk = dict(zip(present, ranks([by_e[e] for e in present])))
        for e in present:
            a = agg.setdefault(e, {"s": [], "r": [], "f": []})
            a["s"].append(by_e[e])
            a["r"].append(rk[e])
            a["f"].append(fw[e])
    # LEAVE-ONE-OUT, because the obvious per-name label is not good enough. The
    # first version tagged each name "supports"/"AGAINST" on whether its rank and
    # its return agreed in SIGN. That reported 3 of 5 AGAINST and read as a broad
    # failure — while dropping one name (and only that one) flipped every horizon
    # positive. A sign label ignores magnitude; a name can disagree by 0.26pp and
    # be tagged identically to one disagreeing by 1.21pp. This measures what the
    # IC actually does without each name, which is the question being asked.
    full = run(pillar, h, cal, closes, scores, UNIVERSE)["mean_ic"]
    out = []
    for e, a in agg.items():
        rest = [x for x in UNIVERSE if x != e]
        loo = run(pillar, h, cal, closes, scores, rest)["mean_ic"]
        out.append({"entity": e, "n": len(a["s"]),
                    "mean_score": statistics.fmean(a["s"]),
                    "mean_rank": statistics.fmean(a["r"]),
                    "mean_fwd_rel_pct": statistics.fmean(a["f"]) * 100.0,
                    "ic_without": loo,
                    "ic_delta": (loo - full) if (loo is not None
                                                 and full is not None) else None})
    return sorted(out, key=lambda r: -r["mean_score"])


def reversal(cal: list[str], closes: dict, h: int) -> float | None:
    """IC(trailing h-day relative, forward h-day relative) for the universe.

    The control. A score correlated with the recent past will inherit whatever
    the tape does next, so a negative forward IC means something quite different
    depending on this number's sign. Worth computing before blaming a pillar.
    """
    ics = []
    for t in range(h, len(cal) - h):
        tr = fwd_relative(cal, closes, t - h, h)
        fw = fwd_relative(cal, closes, t, h)
        if tr is None or fw is None:
            continue
        ic = spearman([tr[e] for e in UNIVERSE], [fw[e] for e in UNIVERSE])
        if ic is not None:
            ics.append(ic)
    return statistics.fmean(ics) if ics else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="1,3,5,10")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    horizons = [int(x) for x in a.horizons.split(",") if x.strip()]

    conn = sqlite3.connect(DB)
    cal, closes, scores = load(conn)
    conn.close()

    score_dates = sorted({d for (_, d) in scores})
    out = [run(p, h, cal, closes, scores) for p in PILLARS for h in horizons]

    h_focus = 5 if 5 in horizons else horizons[len(horizons) // 2]
    conc = concentration("composite", h_focus, cal, closes, scores)
    rev = {h: reversal(cal, closes, h) for h in horizons}

    if a.json:
        print(json.dumps({"universe": UNIVERSE, "calendar_days": len(cal),
                          "score_dates": len(score_dates), "results": out,
                          "concentration": {"h": h_focus, "rows": conc},
                          "reversal_ic": rev}, indent=2))
        return 0

    print(f"forward RELATIVE move vs pillar score — Spearman IC")
    print(f"universe {len(UNIVERSE)} names, {len(score_dates)} score dates "
          f"{score_dates[0]} .. {score_dates[-1]}, "
          f"prices to {cal[-1]}\n")
    print(f"{'pillar':11} {'h':>3} {'dates':>6} {'meanIC':>8} {'sdIC':>7} "
          f"{'t_adj':>6} {'hit':>5} {'indep':>6} {'IC_ind':>8} {'L/S %':>7}")
    print("-" * 78)
    for r in out:
        if not r["n_dates"]:
            print(f"{r['pillar']:11} {r['h']:>3} {'—':>6}   (no testable cross-section)")
            continue
        f = lambda v, w, p: (f"{v:>{w}.{p}f}" if v is not None else f"{'—':>{w}}")
        print(f"{r['pillar']:11} {r['h']:>3} {r['n_dates']:>6} "
              f"{f(r['mean_ic'],8,3)} {f(r['sd_ic'],7,3)} {f(r['t_adj'],6,2)} "
              f"{f(r['hit_rate'],5,2)} {str(r['n_indep'] or '—'):>6} "
              f"{f(r['mean_ic_indep'],8,3)} {f(r['mean_spread_pct'],7,2)}")

    print(f"\nCONCENTRATION — composite, h={h_focus}. Read this BEFORE the table above.")
    print(f"{'name':16}{'meanScore':>10}{'meanRank':>9}{'fwdRel%':>9}"
          f"{'IC w/o':>8}{'delta':>8}")
    for r in conc:
        w = f"{r['ic_without']:>+8.3f}" if r["ic_without"] is not None else f"{'—':>8}"
        d = f"{r['ic_delta']:>+8.3f}" if r["ic_delta"] is not None else f"{'—':>8}"
        print(f"{r['entity']:16}{r['mean_score']:>10.2f}{r['mean_rank']:>9.2f}"
              f"{r['mean_fwd_rel_pct']:>9.2f}{w}{d}")
    print("\nreversal control  IC(trailing, forward): "
          + "  ".join(f"h={h}:{v:+.3f}" for h, v in rev.items() if v is not None))

    print(f"""
HOW TO READ THIS, AND HOW NOT TO.

meanIC is the average cross-sectional rank correlation between the score and the
next h trading days of RELATIVE return. Positive means the ranking pointed the
right way. `indep` is how many NON-overlapping windows the sample really holds —
that, not `dates`, is the sample size. t_adj deflates the naive t by sqrt(h) for
the overlap; it is still optimistic.

With {len(score_dates)} dates over {len(UNIVERSE)} correlated names, NOTHING here reaches significance
and no weight should be changed on it. A |t_adj| under about 2 is noise, and at
these sample sizes even a large IC will not clear it. The result worth acting on
is a pillar that is consistently NEGATIVE across horizons — that is a sign error
or an inverted spec, and it is visible at this sample size when a small edge is
not.

`guidance` covers one entity on most dates, so it usually has no cross-section
to rank at all. Its row is a coverage statement, not a verdict.

The CONCENTRATION table is the check on all of it. `IC w/o` is the headline IC
recomputed with that name removed from BOTH the ranking and the hedge; `delta`
is how much its presence moves the answer. One large positive delta means the
whole result is that name's episode over one window, and says nothing about the
model. Only when no single name dominates the delta is the ranking itself in
question.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
