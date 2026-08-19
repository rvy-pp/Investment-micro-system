"""Re-model one structural change and say what it is worth.

THE PRODUCT, not a utility. A catalyst reaches everyone at the same time — the
Alunorte cut, a coal block commissioning, a pot line trip. What does not reach
everyone at the same time is the QUANTIFIED consequence, because working out
that NALCO's coal `market_pct` going 1.00 -> 0.60 halves its coal beta, and what
that is worth at current prices, is slow and error-prone by hand. The bridge does
it exactly. That is the whole edge: precision of re-modelling, not speed.

TWO NUMBERS, and the second is usually the interesting one:

  IMMEDIATE    what the change does to today's score, on today's 30-day shocks.
               Often small — a market_pct change creates no shock of its own.
  SENSITIVITY  what it does to the name's EXPOSURE. "A 10% coal move used to cost
               1.5% of EBITDA and now costs 0.9%." This is the durable statement
               and the one worth writing down.

Reporting only the first understates a structural change; reporting only the
second overstates its immediacy.

MATERIALITY IS NOT A JUDGEMENT CALL. The sector spec already carries
`min_pct_of_ebitda` (0.015) — the desk's own threshold, already argued about.
A change clearing it is a signal; one that does not is a note.

EMITTING REQUIRES A FALSIFIER. `signals.falsifier` is NOT NULL with a non-empty
CHECK, so --emit without --falsifier is refused before the insert, not by it.
That constraint is the only thing standing between a catalyst log and a list of
interesting things somebody noticed.

Usage:
    python packages/score/whatif.py --entity nalco \\
        --set thermal_coal_seaborne.market_pct=0.60 \\
        --because "Utkal blocks commissioning, announced 2026-07-15"

    ... --emit --falsifier "wrong if the ramp slips past Q4 or captive lands <0.25"
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
import sqlite3
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bridge import (load_specs, load_scoring, load_accumulation, run_bridge,  # noqa
                    shocks_from_store, _series_in_store)
from scoring import score as to_score  # noqa

SPEC_VERSION = "0.5.0"
LOOKBACK = 30
PROBE = 0.10          # the sensitivity probe: a +10% move in one driver


def sector_threshold(entity: dict) -> float:
    import yaml
    pg = entity.get("peer_group") or ""
    for f in (REPO / "specs" / "sectors").glob("*.yaml"):
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if d.get("peer_group") == pg or f.stem == pg:
            for k in ("materiality", "layer1", "thresholds"):
                blk = d.get(k) or {}
                if isinstance(blk, dict) and "min_pct_of_ebitda" in blk:
                    return float(blk["min_pct_of_ebitda"])
            if "min_pct_of_ebitda" in d:
                return float(d["min_pct_of_ebitda"])
    return 0.015


def apply_override(ent: dict, spec: str) -> tuple[dict, str, str, float]:
    """`item.field=value` -> a copy of the entity with that field replaced."""
    try:
        lhs, val = spec.split("=", 1)
        item, field = lhs.rsplit(".", 1)
        newval = float(val)
    except ValueError:
        raise SystemExit(f"--set must be item.field=value, got {spec!r}")
    out = copy.deepcopy(ent)
    hit = None
    for kind in ("outputs", "inputs"):
        for ln in (out.get(kind) or []):
            if ln.get("item") == item:
                if field not in ln:
                    raise SystemExit(
                        f"{item} has no field '{field}'. Fields present: "
                        f"{sorted(k for k in ln if not k.startswith('_'))}")
                hit = (kind, ln.get(field))
                ln[field] = newval
    if hit is None:
        items = [l.get("item") for k in ("outputs", "inputs") for l in (out.get(k) or [])]
        raise SystemExit(f"no line '{item}' on this entity. Lines: {items}")
    return out, item, field, hit[1]


def sensitivity(ent, units, base, usdinr, avail, driver, lvl):
    r = run_bridge(ent, {driver: lvl * PROBE}, units, base, usdinr, avail | {driver})
    return (r["pct_of_ebitda"] or 0.0) * 100


def code_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                              capture_output=True, text=True).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", required=True)
    ap.add_argument("--set", dest="sets", action="append", required=True,
                    help="item.field=value, repeatable")
    ap.add_argument("--because", required=True,
                    help="the disclosure and ITS DATE — not the date it happens")
    ap.add_argument("--from", dest="from_date", default=None,
                    help="effective date of the structural change")
    ap.add_argument("--as-of", default=None, help="pricing date (default: latest)")
    ap.add_argument("--emit", action="store_true", help="write a signals row")
    ap.add_argument("--falsifier", default=None, help="required with --emit")
    ap.add_argument("--conviction", default="medium",
                    choices=["low", "medium", "high"])
    a = ap.parse_args()

    if a.emit and not (a.falsifier or "").strip():
        raise SystemExit(
            "--emit requires --falsifier.\n"
            "signals.falsifier is NOT NULL with a non-empty CHECK, so this would "
            "be rejected by the database anyway. It is refused here so the reason "
            "is legible: a catalyst you cannot be wrong about is not a signal, it "
            "is a note.")

    ents, units, fin = load_specs()
    form, k, p = load_scoring()
    acc, hl = load_accumulation()
    avail = _series_in_store()
    if a.entity not in ents:
        raise SystemExit(f"unknown entity {a.entity!r}. Known: {sorted(ents)}")
    ent = ents[a.entity]
    base = fin["companies"].get(a.entity, {}).get("base_ebitda")
    if not base:
        raise SystemExit(f"{a.entity} has no base_ebitda — every number would be "
                         f"silently rescaled. Run packages/core/preflight.py.")

    conn = sqlite3.connect(DB)
    as_of = a.as_of or conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    lvl = {e: v for e, v in conn.execute(
        "SELECT entity_id, close FROM prices p WHERE date=(SELECT MAX(date) FROM "
        "prices WHERE entity_id=p.entity_id)")}
    shocks, _d, _r, fx = shocks_from_store(LOOKBACK, as_of, acc, hl)
    usdinr = fx or fin["usdinr"]

    after = ent
    changed = []
    for spec in a.sets:
        after, item, field, old = apply_override(after, spec)
        drv = next((l.get("price_link") for kd in ("outputs", "inputs")
                    for l in (after.get(kd) or []) if l.get("item") == item), None)
        changed.append((item, field, old, spec.split("=")[1], drv))

    before_r = run_bridge(ent, shocks, units, base, usdinr, avail | set(shocks))
    after_r = run_bridge(after, shocks, units, base, usdinr, avail | set(shocks))
    b_pct = (before_r["pct_of_ebitda"] or 0.0)
    a_pct = (after_r["pct_of_ebitda"] or 0.0)
    b_s, a_s = to_score(b_pct, k, form, p), to_score(a_pct, k, form, p)
    thr = sector_threshold(ent)

    print(f"{a.entity}   priced {as_of}   base EBITDA Rs{base:,.0f} cr")
    print(f"because: {a.because}")
    if a.from_date:
        print(f"effective from: {a.from_date}")
    print()
    print("CHANGE")
    for item, field, old, new, drv in changed:
        print(f"  {item}.{field}   {old}  ->  {new}    (priced off {drv})")

    print("\nIMMEDIATE — on the last 30 days of price moves")
    print(f"  {'dEBITDA':16}{before_r['d_ebitda_cr']:>+12,.0f} cr  ->"
          f"{after_r['d_ebitda_cr']:>+12,.0f} cr")
    print(f"  {'% of base':16}{b_pct*100:>+12.2f} %   ->{a_pct*100:>+12.2f} %")
    print(f"  {'score':16}{b_s:>12.2f}     ->{a_s:>12.2f}")

    print(f"\nSENSITIVITY — what a +{PROBE:.0%} move in each affected driver is worth")
    print(f"  {'driver':26}{'before':>10}{'after':>10}{'change':>10}")
    worst = 0.0
    for _i, _f, _o, _n, drv in changed:
        if not drv or drv not in lvl:
            continue
        sb = sensitivity(ent, units, base, usdinr, avail, drv, lvl[drv])
        sa = sensitivity(after, units, base, usdinr, avail, drv, lvl[drv])
        print(f"  {drv:26}{sb:>+10.2f}%{sa:>+10.2f}%{sa-sb:>+10.2f}%")
        worst = max(worst, abs(sa - sb))

    print(f"\nMATERIALITY — sector threshold {thr:.1%} of EBITDA")
    imm = abs(a_pct - b_pct)
    print(f"  immediate change   {imm*100:>6.2f}%   "
          f"{'CLEARS' if imm >= thr else 'below'}")
    print(f"  sensitivity change {worst:>6.2f}%   "
          f"{'CLEARS' if worst/100 >= thr else 'below'}")
    verdict = (imm >= thr) or (worst / 100 >= thr)
    print(f"  => {'SIGNAL' if verdict else 'note only — record it, do not flag it'}")

    if not a.emit:
        print("\nNothing written. Re-run with --emit --falsifier \"...\" to log it.")
        conn.close()
        return 0

    direction = "long" if (a_pct - b_pct) >= 0 else "short"
    thesis = (f"{'; '.join(f'{i}.{f} {o}->{n}' for i, f, o, n, _ in changed)}"
              f" | {a.because}")
    conn.execute(
        "INSERT INTO signals (as_of,kind,long_entity,short_entity,direction,"
        "conviction,thesis,falsifier,driving_item,l1_pct_of_ebitda,l3_gated,"
        "spec_version,code_sha,created_at) "
        "VALUES (?,'single',?,NULL,?,?,?,?,?,?,0,?,?,?)",
        (a.from_date or as_of, a.entity if direction == "long" else None,
         direction, a.conviction, thesis[:900], a.falsifier.strip(),
         changed[0][0], worst, SPEC_VERSION, code_sha(),
         dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    conn.commit()
    sid = conn.execute("SELECT MAX(id) FROM signals").fetchone()[0]
    conn.close()
    print(f"\nsignal {sid} written — {direction}, conviction {a.conviction}")
    print("  It is now gradeable. `outcomes` takes signal_id and a horizon.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
