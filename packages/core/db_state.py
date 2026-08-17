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
    "L3 computed output": ["bridge_runs", "bridge_lines", "bridge_results",
                           "economics", "market_layer", "guidance_confidence",
                           "sector_regime", "signals"],
    "L5 review":          ["outcomes"],
    "PM input":           ["overrides", "positions"],
}

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
        mark = "  " if n else "  EMPTY"
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
        print(f"   {t:22} {n:>6} rows{mark}{rng}")
    print()

conn.close()
