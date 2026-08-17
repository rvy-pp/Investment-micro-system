"""What is actually IN the store, and what only exists at runtime.

Worth having as a command rather than a memory: the gap between "we have a
database" and "the database holds the system's output" is exactly where a
project convinces itself it is further along than it is.
"""

import pathlib
import sqlite3

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
conn = sqlite3.connect(REPO / "data" / "ims.db")

GROUPS = {
    "L0 raw inputs":      ["sources", "entities", "prices", "oi"],
    "L1 cited facts":     ["observations", "broker_actions", "estimates",
                           "guidance", "guidance_evidence"],
    "L3 computed output": ["pillar_scores", "bridge_runs", "bridge_lines",
                           "bridge_results", "economics", "market_layer",
                           "guidance_confidence", "sector_regime", "signals"],
    "L5 review":          ["outcomes"],
    "PM input":           ["overrides", "positions"],
}

# An empty table means one of two quite different things, and reporting both as
# a bare "EMPTY" is what made this command lie: it listed every L3 table as
# empty while omitting `pillar_scores`, the one table that holds the output.
# Read as "the system computes nothing", when in fact it computes and stores
# all four pillars. Anything empty is annotated here or it is a real gap.
SUPERSEDED = {   # computed, but not persisted HERE — the result reaches pillar_scores
    "bridge_runs":         "bridge.py runs the margin bridge in memory, unjournalled",
    "bridge_lines":        "per-line detail is NOT kept — rerun bridge.py to see it",
    "bridge_results":      "only the aggregate reaches pillar_scores.detail",
    "economics":           "intensities are authored in specs/entities/*.yaml",
    "market_layer":        "valuation.py re-marks EV/EBITDA at spot each run",
    "guidance_confidence": "-> pillar_scores (guidance)",
}
NOT_BUILT = {    # nothing computes these yet — genuine gaps, in project order
    "estimates":     "no consensus source ingested",
    "sector_regime": "the in flavour / out of flavour gate",
    "signals":       "no directional call with a falsifier is emitted",
    "outcomes":      "nothing grades signals — needs `signals` first",
    "positions":     "no book ingestion",
}

# Retained on purpose even while empty: init_db.py exercises their CHECK
# constraints as the guard tests for invariants 1 and 2 (no number without a
# citation, no intensity without provenance). Dropping them removes the tests.
GUARDED = {"economics", "signals"}

tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}

size_mb = (REPO / "data" / "ims.db").stat().st_size / 1e6
print(f"data/ims.db — {size_mb:.1f} MB, {len(tables)} tables\n")

for group, names in GROUPS.items():
    print(f"{group}")
    for t in names:
        if t not in tables:
            print(f"   {t:22} (missing)")
            continue
        n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        rng = ""
        for col in ("date", "as_of", "action_date", "source_date"):
            try:
                lo, hi = conn.execute(
                    f"SELECT MIN({col}), MAX({col}) FROM {t}").fetchone()
                if lo:
                    rng = f"   {lo} .. {hi}"
                break
            except sqlite3.OperationalError:
                continue
        if n:
            mark = "  "
        elif t in SUPERSEDED:
            mark = f"  empty — superseded: {SUPERSEDED[t]}"
        elif t in NOT_BUILT:
            mark = f"  EMPTY — NOT BUILT: {NOT_BUILT[t]}"
        else:
            mark = "  EMPTY — unannotated: a real gap, or add it to the notes above"
        star = " *" if t in GUARDED else ""
        print(f"   {t:20}{star:2} {n:>6} rows{mark}{rng}")
    print()

gaps = [t for t in NOT_BUILT if t in tables
        and not conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]]
print("* retained while empty — init_db.py uses its CHECKs as a guard test\n")
print(f"not built yet ({len(gaps)}): {', '.join(gaps)}" if gaps
      else "every planned table now holds rows")

conn.close()
