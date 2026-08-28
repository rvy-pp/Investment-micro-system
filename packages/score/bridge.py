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
from datetime import date as _dt_date

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


def load_accumulation() -> tuple[str, float]:
    """(method, half_life) from the spec. The spec has always said EWMA; the
    code used a trailing window until 2026-08-16, and that gap accounted for
    roughly half of all daily score movement."""
    s = yaml.safe_load(SCORING_SPEC.read_text(encoding="utf-8"))
    acc = s.get("accumulation") or {}
    return acc.get("method", "ewma"), float(acc.get("half_life_days", 10))


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
    """Volume an input line is applied to.

    Resolves an output ITEM name first, then falls back to a `basis_group` — a
    label several output lines share when they are CO-PRODUCTS: the same tonnes
    sold in more than one form, over one cost base. In that case the volumes are
    SUMMED.

    WHY THIS EXISTS. Every multi-output entity here used to be a primary line
    plus a BY-PRODUCT (nalco/hindalco `alumina_surplus`, hzl `silver_byproduct`),
    which does not consume the primary's inputs — so naming the primary in
    `basis_item` was correct. SAIL's 50/50 flat/long split was the first
    co-product case, and it broke silently and BULLISHLY: two 8.32mt output lines
    with inputs still basis'd on `rebar` returned 8.32mt instead of 16.64mt, which
    HALVED the entire cost side while leaving revenue whole. Measured at the time:
    coking coal -11.38% -> -5.69%, iron ore -0.77% -> -0.38%, with coverage still
    reporting 6/6 and nothing raising.

    The first fix was spec-only — write each input line twice, once per
    basis_item. It restored pct_of_ebitda exactly but left `d_ebitda_per_t`
    dividing by one leg's 8.32mt while d_ebitda covered all 16.64mt, i.e. 2x too
    high, in the one sector where EBITDA/t is the unit the sell-side quotes. It
    also left four input lines where two would look natural, which is a standing
    invitation to reintroduce the bug by tidying.

    Backward compatible by construction: an entity with no `basis_group` anywhere
    resolves exactly as before, so aluminium and zinc are untouched.
    """
    outs = entity.get("outputs") or []
    for out in outs:
        if out["item"] == basis_item:
            return out.get("volume")
    grouped = [o.get("volume") for o in outs
               if o.get("basis_group") == basis_item and o.get("volume") is not None]
    return sum(grouped) if grouped else None


def run_bridge(
    entity: dict,
    shocks: dict[str, float],
    units: dict[str, str],
    base_ebitda_cr: float,
    usdinr: float,
    available: set[str] | None = None,
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
    # A price_link that EXISTS is not a price_link that has DATA. Checking only
    # for the field let novelis report 3/3 priced while its entire revenue side
    # (can_sheet_spread) had no series — a one-sided bridge presented as a
    # complete one, which is the most dangerous kind of wrong number.
    def has_data(link: str | None) -> bool:
        return bool(link) and (available is None or link in available)

    for out in entity.get("outputs") or []:
        n_total += 1
        link, vol = out.get("price_link"), out.get("volume")
        priceable = has_data(link) and vol is not None
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
        priceable = has_data(link) and bvol is not None
        if not priceable:
            lines.append({"kind": "input", "item": inp["item"], "priced": False,
                          "moved": False})
            continue
        n_priced += 1
        delta = shocks.get(link, 0.0)
        # Two separate haircuts, and conflating them would be a modelling error:
        #   market_pct         how much of the input is BOUGHT vs captive
        #   basis_pass_through how much of a PROXY benchmark's move reaches this
        #                      company's actual cost (seaborne coal -> Indian
        #                      administered domestic coal is ~0.35, not 1.0)
        impact = (bvol * inp["intensity"] * inp.get("market_pct", 1.0)
                  * inp.get("basis_pass_through", 1.0)
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
    """The key the per-tonne denominator is taken against.

    Returns the first output's `basis_group` when it has one, so d_ebitda_per_t
    divides by the CO-PRODUCT TOTAL rather than by whichever leg happens to be
    listed first. Without this SAIL's reported /t came out 2x too high — the
    numerator covered 16.64mt and the denominator 8.32mt. Falls back to the first
    output's item name, which is what every by-product entity still uses.
    """
    outs = entity.get("outputs") or []
    if not outs:
        return ""
    return outs[0].get("basis_group") or outs[0]["item"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_shock(s: str) -> tuple[str, float]:
    key, _, val = s.partition("=")
    return key.strip(), float(val)


def _series_in_store() -> set[str]:
    """Which series have a resolvable level — from a FEED or from cited
    observations. Both count toward coverage_ok: a level carried forward from
    research is a real level, just an older one."""
    import sqlite3

    db = REPO / "data" / "ims.db"
    if not db.exists():
        return set()
    conn = sqlite3.connect(db)
    out = {r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM prices")}
    out |= {r[0] for r in conn.execute(
        "SELECT DISTINCT metric FROM observations WHERE factor='price'")}
    conn.close()
    return out


def shocks_from_store(window: int, as_of: str | None = None,
                      accum: str = "window", half_life: float = 10.0
                      ) -> tuple[dict[str, float], dict[str, tuple], str, float]:
    """Real price deltas over `window` CALENDAR DAYS, read from the store.

    Calendar days, not row counts. An earlier version took N rows back, which
    silently spans N *months* on a monthly series: asking for 20 "sessions" of
    coal returned Oct-2024 -> Jun-2026, a 20-month move presented as a 20-day
    one. Row counting only works if every series shares a frequency, and this
    store deliberately mixes daily equities with monthly IMF assessments.

    For each series independently: the last print on or before as_of, against
    the last print on or before (as_of - window). Series report their own
    effective dates so a stale or coarse one is visible rather than assumed.
    """
    import datetime as _dt
    import sqlite3

    conn = sqlite3.connect(REPO / "data" / "ims.db")
    shocks: dict[str, float] = {}
    detail: dict[str, tuple] = {}

    # Resolve through packages/core/series.py so feeds and cited observations
    # share one interface. A cp_coke level carried forward from research is a
    # real level; it is simply older, and its age travels with it.
    sys.path.insert(0, str(REPO / "packages" / "core"))
    from series import resolve, delta_over, ewma_delta  # noqa: E402

    # THE CLOCK. Not MAX(date) — the cement pack stamps its in-progress month
    # at the capture date, so MAX(date) runs a day ahead of every daily close.
    # A coarse series contributes its shock, never the clock. SILENT_BUGS 8.
    from series import latest_daily_date  # noqa: E402
    latest = latest_daily_date(conn)
    as_of = as_of or latest

    ents = [r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM prices")]
    ents += [r[0] for r in conn.execute(
        "SELECT DISTINCT metric FROM observations WHERE factor='price'")]

    for eid in dict.fromkeys(ents):
        pts = resolve(conn, eid)
        got = (ewma_delta(pts, as_of, half_life) if accum == "ewma"
               else delta_over(pts, as_of, window))
        if not got:
            continue          # no move OBSERVED in the window — not a zero move
        d, new, old = got
        shocks[eid] = d
        detail[eid] = (old.date, old.value, new.date, new.value,
                       (new.value / old.value - 1) * 100, new.stale_days,
                       new.origin)

    fx_row = conn.execute(
        "SELECT close FROM prices WHERE entity_id='usdinr' AND date<=? "
        "ORDER BY date DESC LIMIT 1", (as_of,)
    ).fetchone()
    conn.close()
    # FX rates are conversion factors, not cost lines. They are applied inside
    # to_inr() and by the zinc adapter; feeding them in as shocks as well would
    # double-count the currency leg.
    for fx_id in ("usdinr", "usdcny"):
        shocks.pop(fx_id, None)
    return shocks, detail, as_of, (fx_row[0] if fx_row else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shock", action="append", default=[],
                    help="series=delta, e.g. alumina_index=+40")
    ap.add_argument("--peer-group", default=None)
    ap.add_argument("--materiality", type=float, default=0.015)
    ap.add_argument("--from-store", type=int, metavar="SESSIONS",
                    help="use real price deltas over N sessions instead of --shock")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--accum", choices=["window","ewma"], default=None,
                    help="override the spec accumulation method")
    args = ap.parse_args()

    entities, units, fin = load_specs()
    usdinr = fin["usdinr"]
    fins = fin["companies"]
    detail: dict[str, tuple] = {}

    if args.from_store:
        acc, hl = load_accumulation()
        if args.accum:
            acc = args.accum
        shocks, detail, as_of, fx = shocks_from_store(
            args.from_store, args.as_of, acc, hl)
        if fx:
            usdinr = fx          # live rate beats the spec fallback
        if not shocks:
            print("no prices in the store — run the yahoo adapter first",
                  file=sys.stderr)
            return 2
        how = (f"EWMA half-life {hl:g}d" if acc == "ewma"
               else f"{args.from_store}-calendar-day window")
        print(f"REAL accumulated move to {as_of} · {how}"
              f"   (USDINR {usdinr:.2f} live)")
        for eid, (d0, c0, d1, c1, pct, stale, origin) in sorted(detail.items()):
            if eid not in shocks:
                continue
            # a series whose newest print is far from as_of is contributing an
            # old move to a current signal — say so on the line itself
            src = "cited" if origin == "cited" else "     "
            warn = f"  <- STALE {stale}d" if stale > 7 else ""
            print(f"   {eid:22} {c0:>10,.2f} ({d0}) -> {c1:>10,.2f} ({d1})"
                  f"  {pct:+6.2f}% {src}{warn}")

        # Series present in the store but with NO new print inside the window
        # are treated as flat. That is correct, but silently omitting them
        # hides how old the underlying data is — a monthly series can sit
        # 75 days stale and contribute a confident zero.
        quiet = sorted(set(_series_in_store()) - set(shocks) - {"usdinr", "usdcny"})
        if quiet:
            print("   no new print in window (treated as flat):")
            import sqlite3 as _s
            _c = _s.connect(REPO / "data" / "ims.db")
            for eid in quiet:
                row = _c.execute("SELECT MAX(date) FROM prices WHERE entity_id=?",
                                 (eid,)).fetchone()
                last = row[0] if row else "?"
                age = ((_dt_date.fromisoformat(as_of) - _dt_date.fromisoformat(last)).days
                       if last else None)
                print(f"     {eid:22} last print {last}  ({age}d old)")
            _c.close()
        print()
    elif args.shock:
        shocks = dict(parse_shock(s) for s in args.shock)
        print(f"SHOCK: {', '.join(f'{k} {v:+g}' for k, v in shocks.items())}"
              f"   (USDINR {usdinr})")
    else:
        print("give --shock or --from-store", file=sys.stderr)
        return 2

    targets = [
        e for e in entities.values()
        if e.get("peer_group") and (not args.peer_group or e["peer_group"] == args.peer_group)
    ]
    # reporting units carry economics but are not scored; include them for visibility
    units_only = [e for e in entities.values() if not e.get("peer_group") and e.get("outputs")]

    print(f"materiality threshold: {args.materiality:.1%} of EBITDA")

    available = set(shocks) | _series_in_store()
    form, k, p = load_scoring()
    print(f"score: {form} form, k={k:.4f}, p={p}  (from specs/scoring.yaml)\n")

    header = (f"{'entity':16} {'d_rev':>9} {'d_cost':>9} {'d_EBITDA':>10} {'/t':>9} "
              f"{'%EBITDA':>9} {'SCORE':>6}  verdict")
    print(header)
    print("-" * len(header))

    rows = []
    for ent in sorted(targets, key=lambda e: e["id"]):
        f = fins.get(ent["id"], {})
        r = run_bridge(ent, shocks, units, f.get("base_ebitda", 0), usdinr, available)

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
            r = run_bridge(ent, shocks, units, f.get("base_ebitda", 0), usdinr, available)
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
