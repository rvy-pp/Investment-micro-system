"""Assemble the data behind the P1 explainer page."""
import json, math, pathlib, sqlite3, statistics, sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "score"))
from bridge import (load_specs, load_scoring, load_accumulation, run_bridge,  # noqa
                    shocks_from_store, _series_in_store)
from scoring import score as to_score, solve_k  # noqa

AS_OF = "2026-08-18"
ALUM = ["nalco", "hindalco", "vaml"]
SILVER = ["hindustan_zinc", "vedanta"]

ents, units, fin = load_specs()
form, k, p = load_scoring()
acc, hl = load_accumulation()
av = _series_in_store()
shocks, _d, _r, fx = shocks_from_store(30, AS_OF, acc, hl)
usdinr = fx or fin["usdinr"]

conn = sqlite3.connect(REPO / "data" / "ims.db")
lvl = {e: c for e, c in conn.execute(
    "SELECT entity_id, close FROM prices p WHERE date=(SELECT MAX(date) FROM prices "
    "WHERE entity_id=p.entity_id)")}

# ---- 1. the 30-day shocks that drive everything ----
DRIVERS = ["lme_aluminium", "alumina_index", "thermal_coal_seaborne", "cp_coke",
           "lme_zinc", "silver", "usdinr"]
shock_rows = []
for s in DRIVERS:
    if s in shocks:
        base = lvl.get(s)
        shock_rows.append(dict(driver=s, delta=shocks[s], level=base,
                               pct=(shocks[s] / base * 100) if base else None))

# ---- 2. full bridge per company ----
books = {}
for book, names in (("aluminium", ALUM), ("silver / zinc", SILVER)):
    rows = []
    for eid in names:
        ent = ents[eid]
        be = fin["companies"][eid]["base_ebitda"]
        r = run_bridge(ent, shocks, units, be, usdinr, av | set(shocks))
        pct = r["pct_of_ebitda"]
        rows.append(dict(
            entity=eid, base_ebitda=be, d_ebitda_cr=r["d_ebitda_cr"],
            pct=pct * 100 if pct is not None else None,
            score=to_score(pct, k, form, p) if pct is not None else None,
            coverage=f'{r["n_priced"]}/{r["n_total"]}',
            lines=[dict(kind=l["kind"], item=l["item"], driver=l.get("driver"),
                        market_pct=l.get("market_pct"), impact=l["impact_cr"])
                   for l in r["lines"]]))
    books[book] = rows

# ---- 3. the spec table: intensity x market_pct per line ----
import yaml
spec = yaml.safe_load(open(REPO / "specs/entities/aluminium.yaml", encoding="utf-8"))
struct = {}
for e in spec.get("entities", []):
    if e["id"] not in ALUM + SILVER:
        continue
    ls = []
    for kind in ("outputs", "inputs"):
        for ln in (e.get(kind) or []):
            ls.append(dict(kind=kind[:-1], item=ln.get("item"),
                           driver=ln.get("price_link"),
                           intensity=ln.get("intensity"),
                           unit=ln.get("intensity_unit"),
                           market_pct=ln.get("market_pct"),
                           pass_through=ln.get("basis_pass_through"),
                           note=(ln.get("source_note") or "")[:96]))
    struct[e["id"]] = ls

# ---- 4. the hill curve ----
curve = [dict(x=x, score=to_score(x, k, form, p))
         for x in [i / 1000 for i in range(-200, 201, 2)]]

# ---- 5. score history ----
hist = {}
for eid, as_of, s in conn.execute(
        "SELECT entity_id, as_of, score FROM pillar_scores WHERE pillar='economics' "
        "AND score IS NOT NULL AND as_of>='2021-01-01' ORDER BY as_of"):
    hist.setdefault(eid, []).append([as_of, round(s, 3)])

# ---- 6. per-driver sensitivity: +10% in one input ----
sens = {}
for eid in ALUM + SILVER:
    ent = ents[eid]
    be = fin["companies"][eid]["base_ebitda"]
    row = {}
    for s in ("lme_aluminium", "alumina_index", "thermal_coal_seaborne", "cp_coke",
              "lme_zinc", "silver"):
        base = lvl.get(s)
        if not base:
            continue
        r = run_bridge(ent, {s: base * 0.10}, units, be, usdinr, av | {s})
        row[s] = (r["pct_of_ebitda"] or 0) * 100
    sens[eid] = row

conn.close()
out = dict(as_of=AS_OF, k=k, p=p, form=form, usdinr=usdinr,
           shocks=shock_rows, books=books, struct=struct, curve=curve,
           hist=hist, sens=sens)
(pathlib.Path(__file__).parent / "_p1.json").write_text(json.dumps(out, default=str))
print("ok", {b: len(v) for b, v in books.items()}, "hist", {a: len(b) for a, b in hist.items()})
