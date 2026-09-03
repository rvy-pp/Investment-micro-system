"""P3 valuation for non-commodity sectors — FORWARD P/E, growth-adjusted,
scored against the peer group.

    fwd EPS   = time-weighted blend of FY1/FY2 consensus (12-month-forward)
    fwd P/E   = close / fwd EPS                      (the headline multiple)
    growth    = FY2 consensus / FY1 consensus - 1    ("today till next year")
    PEG       = (close / FY2 EPS) / (growth x 100)
    raw       = ln(PEG / group median PEG)
    score     = hill(-raw)      cheap-for-its-growth scores HIGH

    THE PEG NUMERATOR IS THE FY2 MULTIPLE, NOT THE BLEND — desk convention,
    PM 2026-09-03 ("the P/E will be FY28E, the growth from today till next
    year"): you pay the FY2 multiple FOR the growth that delivers FY2, so
    numerator and denominator describe the same year. Until then it was the
    12m blend / same growth — a ~0-10% higher PEG, largest on high-growth
    names (the blend leans on the smaller FY1 EPS). The switch changes the
    EMS P3 raws from the next persist onward; stored rows keep their own
    code_sha stamp, which is what the stamp is for.

WHY NOT THE EV/EBITDA MACHINERY. EMS names are converters: they earn a
conversion margin on a mostly pass-through cost stack, so a commodity margin
bridge answers the wrong question (the APL Apollo / Novelis precedent), and
the desk values them on forward P/E — which is also how every broker note in
the digests quotes them ("52x FY28 PE demanding", "35x FY28E EPS of Rs16",
"45x FY28F P/E"). PM instruction 2026-08-29: score EMS "along the lines of
1 year fwd P/E valuations majorly".

WHY PEER-RELATIVE AND NOT OWN-HISTORY. The EV/EBITDA pillar scores a z
against the name's own history because that history is COMPUTABLE — prices
and a re-markable EBITDA exist for every past date. A forward P/E's
denominator is what the street expected AT THAT TIME, and no free source
carries historical consensus. Reconstructing it from realised earnings would
be look-ahead; holding today's estimate fixed across history would degrade
the whole thing to a price z. So the honest comparison available TODAY is
cross-sectional: is this name rich or cheap against its peers, per unit of
its own expected growth. yahoo_estimates.py captures the consensus daily, so
an own-history variant becomes computable once enough captures accumulate —
that is a design intention, recorded in specs/sectors/ems.yaml, not a
production feature.

CONSEQUENCE OF PEER-RELATIVE, stated because it will be noticed: a name's
score can move on a PEER's estimate revision with no news of its own. The
median and group size are carried in `detail` so any such move is auditable.

THE RAW IS A LOG-RATIO AND THAT IS WHAT MAKES IT SPREADABLE. For a pair,
raw_A - raw_B = ln(PEG_A / PEG_B): the group median cancels, so the pair
spread depends only on the two legs — the same property the z-raw has for
EV/EBITDA. Anchor: one leg at 1.5x the other's growth-adjusted multiple is
the desk's "act" threshold, so |ln(1.5)| reads 4.0 cheap / 2.0 rich.

GATES (withhold rather than guess, invariant 7):
    no consensus capture                -> withheld
    capture older than MAX_EST_AGE      -> withheld (a stale consensus is not
                                           a current one; the capture is daily
                                           so age means the feed broke)
    fewer than MIN_ANALYSTS on FY1      -> withheld (a 2-analyst "consensus"
                                           is one house's model wearing a label)
    FY1 or blended EPS <= 0             -> withheld (P/E undefined)
    growth below MIN_GROWTH             -> withheld (PEG explodes as g -> 0;
                                           a low-growth name needs a different
                                           question asked of it)
    fewer than MIN_GROUP names passing  -> ALL withheld (a median of two is
                                           just the other name)

Usage:
    python packages/score/valuation_pe.py --peer-group ems_assemblers
    python packages/score/valuation_pe.py --peer-group ems_assemblers --as-of 2026-08-29
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import re
import sqlite3
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bridge import load_specs  # noqa: E402
from scoring import score as to_score, solve_k  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

# One leg at 1.5x the group's growth-adjusted multiple reads 2.0 (rich) /
# its inverse reads 4.0 (cheap). Same hill form and p as everything else, so
# the shape argument is inherited; only the anchor quantity is this pillar's
# own — the rule specs/scoring.yaml states for any new P3/P4 raw.
PEG_ANCHOR_RATIO = 1.5
X_REF = math.log(PEG_ANCHOR_RATIO)
SCORE_ANCHOR = 4.0
P = 1.5

MAX_EST_AGE_DAYS = 30   # capture is daily; older means the feed broke
MIN_ANALYSTS = 5        # below this, "consensus" is one house's model
MIN_GROWTH = 0.05       # PEG explodes as g -> 0
MIN_GROUP = 3           # a median of two is just the other name


def spec_metrics(peer_group: str) -> list[str]:
    """`pillar_3.metrics` for a peer group, from specs/sectors/*.yaml.

    This makes `metrics:` a READ field for the first time — it was written in
    all five sector specs and consulted by nothing, the same shape as
    `lookback_days` before valuation.spec_lookback() and `effective_from`
    still. run_scores dispatches on it: `pe_forward_peg` routes here,
    anything else takes the EV/EBITDA path unchanged.
    """
    f = REPO / "specs" / "sectors"
    try:
        import yaml
        for path in sorted(f.glob("*.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            groups = doc.get("peer_groups") or [doc.get("peer_group")]
            if peer_group in (groups or []):
                m = (doc.get("pillar_3") or {}).get("metrics")
                if m:
                    return list(m)
    except Exception:
        pass
    return []


def _fy_end(label: str) -> dt.date:
    m = re.fullmatch(r"FY(\d{2})", label or "")
    if not m:
        raise ValueError(f"unparseable period label {label!r}")
    return dt.date(2000 + int(m.group(1)), 3, 31)


def _close_on(conn, eid: str, as_of: str):
    r = conn.execute(
        "SELECT date, close FROM prices WHERE entity_id=? AND date<=? "
        "ORDER BY date DESC LIMIT 1", (eid, as_of)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def consensus_asof(conn, eid: str, as_of: str) -> dict | None:
    """Latest consensus capture on or before as_of, shaped per period.

    {"as_of": capture_date, "FY27": {"eps": .., "eps_90d_ago": .., "n": ..},
     "FY28": {...}}
    """
    cap = conn.execute(
        "SELECT MAX(as_of) FROM estimates WHERE entity_id=? AND "
        "broker='consensus_yahoo' AND as_of<=?", (eid, as_of)).fetchone()[0]
    if not cap:
        return None
    out: dict = {"as_of": cap}
    for period, metric, val in conn.execute(
            "SELECT period, metric, value_num FROM estimates WHERE entity_id=? "
            "AND broker='consensus_yahoo' AND as_of=?", (eid, cap)):
        out.setdefault(period, {})[metric] = val
    return out


def _blend(as_of: dt.date, fy1_end: dt.date, e1: float, e2: float) -> tuple[float, float]:
    """12-month-forward EPS: the remaining share of FY1 plus the balance from
    FY2. Weight is calendar-exact and clamps at the April roll, so a capture
    read across the fiscal seam degrades to pure FY2 rather than going
    negative."""
    w = max(0.0, min(1.0, (fy1_end - as_of).days / 365.0))
    return w * e1 + (1 - w) * e2, w


def compute_row(conn, eid: str, as_of: str,
                require_growth: bool = True) -> tuple[dict | None, str | None]:
    """One name's forward P/E block, or (None, why_withheld).

    require_growth=False is the DISPLAY path (engine.consensus_panel): the
    growth floor is a scoring gate, not an arithmetic one — a forward P/E is
    perfectly defined at 4% growth, only the PEG explodes as g -> 0. IT's
    large caps (TCS/Infosys/Wipro) all sit under 5% FY27->FY28, and a panel
    that withheld three of five names would read as a broken feed. The row
    then carries peg=None where the ratio is meaningless; every scoring call
    keeps the default and withholds exactly as before."""
    px_date, close = _close_on(conn, eid, as_of)
    if close is None:
        return None, "no price"
    est = consensus_asof(conn, eid, as_of)
    if not est:
        return None, "no consensus estimates (run yahoo_estimates.py)"
    age = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(est["as_of"])).days
    if age > MAX_EST_AGE_DAYS:
        return None, f"stale consensus ({age}d old)"

    periods = sorted((k for k in est if k.startswith("FY")), key=_fy_end)
    # FY1 = first fiscal year still open at as_of; needs FY2 behind it to blend.
    d0 = dt.date.fromisoformat(as_of)
    live = [pl for pl in periods if _fy_end(pl) >= d0]
    if len(live) < 2:
        return None, f"need two open fiscal years, have {live}"
    fy1, fy2 = live[0], live[1]
    e1, e2 = est[fy1].get("eps"), est[fy2].get("eps")
    n1 = est[fy1].get("n_analysts") or 0
    if e1 is None or e2 is None:
        return None, f"missing eps for {fy1}/{fy2}"
    if n1 < MIN_ANALYSTS:
        return None, f"thin coverage (n={n1:.0f} on {fy1})"
    if e1 <= 0:
        return None, f"non-positive {fy1} consensus EPS ({e1})"

    eps_12mf, w = _blend(d0, _fy_end(fy1), e1, e2)
    if eps_12mf <= 0:
        return None, f"non-positive blended forward EPS ({eps_12mf:.2f})"
    g = e2 / e1 - 1.0
    if g < MIN_GROWTH and require_growth:
        return None, f"growth {g:+.1%} below {MIN_GROWTH:.0%}; PEG undefined there"

    fwd_pe = close / eps_12mf
    # PEG is computed whenever growth is positive — below MIN_GROWTH it is
    # DISPLAY-ONLY and flagged unstable (at 4% growth a 1pp revision swings
    # it ~0.7), and the scoring path never reaches here with low growth
    # (require_growth returns above). PM 2026-09-02: show it, don't hide it.
    # Numerator is the FY2 multiple per the docstring's desk convention.
    peg = (close / e2) / (g * 100.0) if (g > 0 and e2 > 0) else None

    # Trailing (normal) P/E — DISPLAY ONLY, PM request 2026-08-30. Never in
    # the score: trailing E is distorted exactly where it matters here
    # (Dixon's TTM carries a one-off so its trailing P/E reads BELOW its
    # forward; Amber's trough TTM reads ~294x). Shown so the forward multiple
    # can be judged against the number every screener quotes.
    ttm_pe = None
    e_ttm = (est.get("TTM") or {}).get("eps_ttm")
    if e_ttm and e_ttm > 0:
        ttm_pe = close / e_ttm

    # Revision momentum — the street repricing the P&L. DISPLAY ONLY: it is
    # not part of the score (it is a direction-of-estimates signal, not a
    # cheapness one) but it is the context a PEG must be read in — today
    # Dixon's PEG is the group's richest while its estimates are the only
    # ones RISING, and hiding that would make the score look like a verdict.
    rev_90d = None
    l1, l2 = est[fy1].get("eps_90d_ago"), est[fy2].get("eps_90d_ago")
    if l1 and l2:
        old = w * l1 + (1 - w) * l2
        if old > 0:
            rev_90d = eps_12mf / old - 1.0

    return {
        "eid": eid, "px_date": px_date, "close": close,
        "capture": est["as_of"], "age_days": age,
        "fy1": fy1, "fy2": fy2, "eps_fy1": e1, "eps_fy2": e2,
        "n_analysts": n1, "w_fy1": round(w, 3),
        "eps_12mf": eps_12mf, "fwd_pe": fwd_pe, "growth": g, "peg": peg,
        "rev_90d": rev_90d, "ttm_pe": ttm_pe,
    }, None


def scores_for_group(conn, group_ents: list[dict], as_of: str) -> dict:
    """{eid: (score, raw, detail, withheld)} for one peer group.

    Group-at-once by necessity: the raw is relative to the group median, so a
    single name's score does not exist in isolation.
    """
    rows, held = {}, {}
    for ent in group_ents:
        row, why = compute_row(conn, ent["id"], as_of)
        if row:
            rows[ent["id"]] = row
        else:
            held[ent["id"]] = why

    out = {}
    if len(rows) < MIN_GROUP:
        why = (f"peer cross-section too thin ({len(rows)} of "
               f"{len(group_ents)} names computable; need {MIN_GROUP})")
        for ent in group_ents:
            out[ent["id"]] = (None, None, None,
                              held.get(ent["id"], why) if ent["id"] in held
                              else why)
        return out

    med = statistics.median(r["peg"] for r in rows.values())
    k = solve_k("hill", X_REF, SCORE_ANCHOR, P)
    for eid, r in rows.items():
        raw = math.log(r["peg"] / med)
        s = to_score(-raw, k, "hill", P)   # cheap-for-growth scores HIGH
        detail = {
            "metric": "pe_forward_peg",
            "fwd_pe": round(r["fwd_pe"], 1),
            "eps_12mf": round(r["eps_12mf"], 2),
            "fy_pair": f"{r['fy1']}/{r['fy2']}",
            "growth": round(r["growth"], 4),
            "peg": round(r["peg"], 3),
            "peg_median": round(med, 3),
            "n_group": len(rows),
            "n_analysts": int(r["n_analysts"]),
            "consensus_as_of": r["capture"],
            "rev_90d": (round(r["rev_90d"], 4)
                        if r["rev_90d"] is not None else None),
            "ttm_pe": (round(r["ttm_pe"], 1)
                       if r["ttm_pe"] is not None else None),
        }
        out[eid] = (s, raw, detail, None)
    for eid, why in held.items():
        out[eid] = (None, None, None, why)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer-group", default="ems_assemblers")
    ap.add_argument("--as-of", default=None)
    a = ap.parse_args()
    entities, _units, _fin = load_specs()
    group = [e for e in entities.values() if e.get("peer_group") == a.peer_group]
    if not group:
        print(f"no entities in peer group {a.peer_group!r}")
        return 1
    conn = sqlite3.connect(DB)
    as_of = a.as_of or conn.execute(
        "SELECT MAX(date) FROM prices WHERE entity_id=?",
        (group[0]["id"],)).fetchone()[0]

    res = scores_for_group(conn, group, as_of)
    print(f"{a.peer_group} · forward P/E vs growth · as of {as_of}\n")
    print(f"{'entity':18}{'close':>9}{'EPS 12mf':>10}{'fwdPE':>8}{'growth':>8}"
          f"{'PEG':>7}{'raw':>8}{'P3':>6}{'rev90d':>8}  note")
    print("-" * 96)
    for eid in sorted(res):
        s, raw, det, wh = res[eid]
        if s is None:
            print(f"{eid:18}{'—':>9}{'—':>10}{'—':>8}{'—':>8}{'—':>7}"
                  f"{'—':>8}{'—':>6}{'—':>8}  WITHHELD: {wh}")
            continue
        row, _ = compute_row(conn, eid, as_of)
        rev = f"{det['rev_90d']:+.1%}" if det["rev_90d"] is not None else "—"
        print(f"{eid:18}{row['close']:>9,.0f}{det['eps_12mf']:>10.2f}"
              f"{det['fwd_pe']:>8.1f}{det['growth']:>8.1%}{det['peg']:>7.2f}"
              f"{raw:>+8.3f}{s:>6.2f}{rev:>8}  n={det['n_analysts']}, "
              f"{det['fy_pair']}")
    med = next((d["peg_median"] for _, _, d, _ in res.values() if d), None)
    if med is not None:
        print(f"\ngroup median PEG {med:.2f}; raw = ln(PEG/median), so a pair "
              f"spread is ln(PEG_a/PEG_b) — the median cancels.")
        print(f"Anchor: {PEG_ANCHOR_RATIO}x the group's growth-adjusted "
              f"multiple reads {6 - SCORE_ANCHOR:.1f}; its inverse reads "
              f"{SCORE_ANCHOR:.1f}. rev90d is context, not score.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
