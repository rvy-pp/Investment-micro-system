"""P1+P2 margin bridge — the arithmetic that turns a price move into a margin move.

Deterministic. No model, no weights, no normalisation. For each company:

    d_revenue = SUM over output lines:  volume * d_price
    d_cost    = SUM over input lines:   basis_volume * intensity * market_pct * d_price
    d_ebitda  = d_revenue - d_cost
    pct_of_ebitda = d_ebitda / base_ebitda        <- the materiality test

market_pct is what makes this per-company: a captive input contributes ZERO to
cost however far its market price moves.

Usage:
    python packages/score/bridge.py --shock alumina_index=+40
    python packages/score/bridge.py --shock lme_zinc=+200 --shock silver=+2
    python packages/score/bridge.py --peer-group aluminium_primary --shock alumina_index=+40
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

# so the script runs from any cwd, not just packages/score/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from scoring import score as to_score, solve_k  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SPECS = REPO / "specs" / "entities"
SCORING_SPEC = REPO / "specs" / "scoring.yaml"


def load_scoring() -> tuple[str, float, float]:
    """Return (form, k, p) from the desk-wide scoring spec."""
    s = yaml.safe_load(SCORING_SPEC.read_text(encoding="utf-8"))
    form, p = s["form"], s["p"]
    k = solve_k(form, s["anchor"]["x_ref"], s["anchor"]["score_ref"], p)
    return form, k, p


# ---------------------------------------------------------------------------
# spec loading
# ---------------------------------------------------------------------------


def load_specs() -> tuple[dict, dict, dict]:
    """Return (entities_by_id, reference_units, base_financials)."""
    entities: dict[str, dict] = {}
    units: dict[str, str] = {}

    for path in sorted(SPECS.glob("*.yaml")):
        if path.name == "base_financials.yaml":
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for ref in doc.get("reference_entities") or []:
            units[ref["id"]] = ref.get("unit", "")
        for ent in doc.get("entities") or []:
            entities[ent["id"]] = ent

    fin = yaml.safe_load((SPECS / "base_financials.yaml").read_text(encoding="utf-8"))
    return entities, units, fin


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------


def to_inr(delta: float, unit: str, usdinr: float) -> float:
    """Convert a per-unit price delta into INR. USD-quoted series need the FX leg."""
    return delta * usdinr if unit.upper().startswith("USD") else delta


def basis_volume(entity: dict, basis_item: str) -> float | None:
    for out in entity.get("outputs") or []:
        if out["item"] == basis_item:
            return out.get("volume")
    return None


def run_bridge(
    entity: dict,
    shocks: dict[str, float],
    units: dict[str, str],
    base_ebitda_cr: float,
    usdinr: float,
) -> dict:
    """Bridge one entity. Returns per-line detail plus the roll-up, in INR crore."""
    CR = 1e7  # 1 crore
    lines: list[dict] = []
    d_revenue = d_cost = 0.0
    n_priced = n_total = 0

    # A line is PRICEABLE if it has a resolvable price series and a volume to
    # apply it to. Whether that series actually MOVED is a separate question:
    # in a scenario run an unshocked line is deliberately flat, not missing.
    # Conflating the two makes the coverage gate fire on every scenario.
    for out in entity.get("outputs") or []:
        n_total += 1
        link, vol = out.get("price_link"), out.get("volume")
        priceable = bool(link) and vol is not None
        if not priceable:
            lines.append({"kind": "output", "item": out["item"], "priced": False,
                          "moved": False})
            continue
        n_priced += 1
        delta = shocks.get(link, 0.0)
        impact = vol * to_inr(delta, units.get(link, ""), usdinr) / CR
        d_revenue += impact
        lines.append(
            {"kind": "output", "item": out["item"], "priced": True,
             "moved": link in shocks, "driver": link, "delta": delta,
             "impact_cr": impact}
        )

    for inp in entity.get("inputs") or []:
        n_total += 1
        link = inp.get("price_link")
        bvol = basis_volume(entity, inp.get("basis_item", ""))
        priceable = bool(link) and bvol is not None
        if not priceable:
            lines.append({"kind": "input", "item": inp["item"], "priced": False,
                          "moved": False})
            continue
        n_priced += 1
        delta = shocks.get(link, 0.0)
        # market_pct is the whole point: captive supply contributes nothing
        impact = (bvol * inp["intensity"] * inp.get("market_pct", 1.0)
                  * to_inr(delta, units.get(link, ""), usdinr) / CR)
        d_cost += impact
        lines.append(
            {"kind": "input", "item": inp["item"], "priced": True,
             "moved": link in shocks, "driver": link, "delta": delta,
             "market_pct": inp.get("market_pct"), "impact_cr": -impact}
        )

    d_ebitda = d_revenue - d_cost
    primary_vol = basis_volume(entity, _primary(entity)) or 0.0

    return {
        "entity": entity["id"],
        "lines": lines,
        "d_revenue_cr": d_revenue,
        "d_cost_cr": d_cost,
        "d_ebitda_cr": d_ebitda,
        "d_ebitda_per_t": (d_ebitda * 1e7 / primary_vol) if primary_vol else None,
        "pct_of_ebitda": (d_ebitda / base_ebitda_cr) if base_ebitda_cr else None,
        "n_priced": n_priced,
        "n_total": n_total,
        "coverage_ok": n_total > 0 and n_priced / n_total >= 0.70,
    }


def _primary(entity: dict) -> str:
    outs = entity.get("outputs") or []
    return outs[0]["item"] if outs else ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_shock(s: str) -> tuple[str, float]:
    key, _, val = s.partition("=")
    return key.strip(), float(val)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shock", action="append", default=[],
                    help="series=delta, e.g. alumina_index=+40")
    ap.add_argument("--peer-group", default=None)
    ap.add_argument("--materiality", type=float, default=0.015)
    args = ap.parse_args()

    if not args.shock:
        print("no --shock given; nothing to compute", file=sys.stderr)
        return 2

    shocks = dict(parse_shock(s) for s in args.shock)
    entities, units, fin = load_specs()
    usdinr = fin["usdinr"]
    fins = fin["companies"]

    targets = [
        e for e in entities.values()
        if e.get("peer_group") and (not args.peer_group or e["peer_group"] == args.peer_group)
    ]
    # reporting units carry economics but are not scored; include them for visibility
    units_only = [e for e in entities.values() if not e.get("peer_group") and e.get("outputs")]

    print(f"SHOCK: {', '.join(f'{k} {v:+g}' for k, v in shocks.items())}"
          f"   (USDINR {usdinr})")
    print(f"materiality threshold: {args.materiality:.1%} of EBITDA\n")

    form, k, p = load_scoring()
    print(f"score: {form} form, k={k:.4f}, p={p}  (from specs/scoring.yaml)\n")

    header = (f"{'entity':16} {'d_rev':>9} {'d_cost':>9} {'d_EBITDA':>10} {'/t':>9} "
              f"{'%EBITDA':>9} {'SCORE':>6}  verdict")
    print(header)
    print("-" * len(header))

    rows = []
    for ent in sorted(targets, key=lambda e: e["id"]):
        f = fins.get(ent["id"], {})
        r = run_bridge(ent, shocks, units, f.get("base_ebitda", 0), usdinr)

        pct = r["pct_of_ebitda"]
        # A score with no trustworthy bridge behind it would be worse than none.
        r["score"] = to_score(pct, k, form, p) if (pct is not None and r["coverage_ok"]) else None
        rows.append((ent, r))

        if not r["coverage_ok"]:
            verdict = f"NO BRIDGE ({r['n_priced']}/{r['n_total']} lines priced)"
        elif pct is None:
            verdict = "no denominator"
        elif abs(pct) < args.materiality:
            verdict = "immaterial -> no trade"
        else:
            verdict = "POSITIVE" if pct > 0 else "NEGATIVE"

        per_t = f"{r['d_ebitda_per_t']:,.0f}" if r["d_ebitda_per_t"] else "-"
        pct_s = f"{pct:+.2%}" if pct is not None else "-"
        sc_s = f"{r['score']:.2f}" if r["score"] is not None else "-"
        print(f"{ent['id']:16} {r['d_revenue_cr']:>9,.0f} {r['d_cost_cr']:>9,.0f} "
              f"{r['d_ebitda_cr']:>+10,.0f} {per_t:>9} {pct_s:>9} {sc_s:>6}  {verdict}")

    if units_only:
        print("\nreporting units (carry economics, never scored):")
        for ent in sorted(units_only, key=lambda e: e["id"]):
            f = fins.get(ent["id"], {})
            r = run_bridge(ent, shocks, units, f.get("base_ebitda", 0), usdinr)
            pct = r["pct_of_ebitda"]
            pct_s = f"{pct:+.2%}" if pct is not None else "-"
            print(f"  {ent['id']:14} {r['d_ebitda_cr']:>+10,.0f} cr {pct_s:>9}"
                  f"   ({r['n_priced']}/{r['n_total']} priced)")

    # line-level decomposition — why, not just what
    print("\nline decomposition (INR cr) — only lines that moved:")
    for ent, r in rows:
        moved = [ln for ln in r["lines"] if ln.get("moved")]
        unpriced = [ln for ln in r["lines"] if not ln.get("priced")]
        if not moved and not unpriced:
            continue
        print(f"  {ent['id']}:")
        for ln in moved:
            mp = f"  market_pct {ln['market_pct']:.2f}" if ln["kind"] == "input" else ""
            print(f"     {ln['kind']:6} {ln['item']:22} {ln['impact_cr']:>+9,.0f}{mp}")
        for ln in unpriced:
            print(f"     {ln['kind']:6} {ln['item']:22} {'unpriced':>9}"
                  f"   <- no price series; counts against coverage")

    # spread view — a long/short desk trades the difference
    if len(rows) >= 2:
        # SCORE THE SPREAD, DO NOT SPREAD THE SCORES.
        #
        # The curve is deliberately flat in the tails, so subtracting two scores
        # that both sit at 9-11% of EBITDA compresses a real 1.75pp gap into
        # 0.09 score points — understating the trade by ~3.5x. Feeding the
        # SPREAD through the same curve puts the pair on the same readable 1-5
        # scale while preserving its true magnitude. A pair is its own instrument
        # and deserves its own score, not the difference of two others.
        print("\npair scores — the spread run through the SAME curve:")
        pairs = []
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if a[1]["score"] is None or b[1]["score"] is None:
                    continue
                d_pct = a[1]["pct_of_ebitda"] - b[1]["pct_of_ebitda"]
                lng, sht = (a, b) if d_pct > 0 else (b, a)
                pairs.append((to_score(abs(d_pct), k, form, p), lng, sht,
                              abs(d_pct)))
        for pair_score, lng, sht, pct_spread in sorted(pairs, reverse=True,
                                                       key=lambda t: t[0]):
            naive = abs(lng[1]["score"] - sht[1]["score"])
            print(f"  long {lng[0]['id']:14} / short {sht[0]['id']:14} "
                  f"PAIR {pair_score:.2f}   ({pct_spread:.2%} EBITDA)"
                  f"   [naive score-difference would read {naive:.2f}]")

    # Implied beta — relative EBITDA sensitivity to THIS shock, vs the most
    # exposed name. Makes a claim like "VEDL is a low-beta HZL" checkable
    # rather than asserted, and shows what a hedge ratio should actually be.
    scored = [(e, r) for e, r in rows if r["score"] is not None
              and r["pct_of_ebitda"] is not None]
    if len(scored) >= 2:
        ref_e, ref_r = max(scored, key=lambda t: abs(t[1]["pct_of_ebitda"]))
        ref = ref_r["pct_of_ebitda"]
        print(f"\nimplied beta to this shock (vs {ref_e['id']} = 1.00):")
        for e, r in sorted(scored, key=lambda t: -abs(t[1]["pct_of_ebitda"])):
            print(f"  {e['id']:16} {r['pct_of_ebitda'] / ref:>6.2f}x")
        print("  note: beta is set by base_ebitda and volumes — correct those,"
              " never add a fudge factor")

    return 0


if __name__ == "__main__":
    sys.exit(main())
