"""Load cited price observations, then show the resolved carry-forward series.

Usage:
    python packages/extract/load_observations.py --file specs/extracted/cp_coke_prices.json
    python packages/extract/load_observations.py --show cp_coke
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(REPO / "packages" / "core"))
from series import observation_series, value_on  # noqa: E402

EXTRACTOR = "manual-v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: pathlib.Path) -> int:
    doc = json.loads(path.read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")

    for s in doc.get("sources", []):
        conn.execute(
            "INSERT OR IGNORE INTO sources (id,kind,origin,title,source_date,"
            "captured_at,raw_path) VALUES (?,?,?,?,?,?,?)",
            (s["id"], s["kind"], s.get("origin"), s.get("title"),
             s["source_date"], now(), s["raw_path"]))

    # explicit entities first, so a sector pseudo-entity gets kind 'macro'
    # rather than the 'commodity' default below
    for e in doc.get("entities", []):
        conn.execute(
            "INSERT OR IGNORE INTO entities (id,kind,name) VALUES (?,?,?)",
            (e["id"], e["kind"], e["name"]))

    n = 0
    for o in doc.get("observations", []):
        conn.execute(
            "INSERT OR IGNORE INTO entities (id,kind,name) VALUES (?,?,?)",
            (o["entity_id"], "commodity", o["entity_id"]))
        conn.execute(
            "INSERT INTO observations (source_id,entity_id,as_of,factor,metric,"
            "value_num,unit,period,direction,confidence,quote,extractor_version,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o["source_id"], o["entity_id"], o["as_of"], o["factor"], o["metric"],
             o.get("value_num"), o.get("unit"), o.get("period"),
             o.get("direction"),
             o["confidence"], o["quote"], EXTRACTOR, now()))
        n += 1

    conn.commit()
    conn.close()
    print(f"loaded {n} observations from {path.name}")
    return 0


def show(metric: str, as_of: str | None) -> int:
    conn = sqlite3.connect(DB)
    raw = conn.execute(
        "SELECT as_of, value_num, unit, period FROM observations "
        "WHERE factor='price' AND metric=? ORDER BY as_of", (metric,)).fetchall()
    pts = observation_series(conn, metric)

    print(f"{metric}: {len(raw)} raw observations -> {len(pts)} distinct levels")
    if len(raw) != len(pts):
        print(f"  ({len(raw) - len(pts)} restatement(s) deduplicated — "
              f"the same fact reported twice is one fact)")
    print()
    print(f"  {'as_of':12} {'cited':>12} {'unit':6} {'period':8} -> {'level':>10}")
    print("  " + "-" * 58)
    lv = {p.date: p.value for p in pts}
    for as_of_r, val, unit, period in raw:
        lvl = f"{lv[as_of_r]:,.2f}" if as_of_r in lv else "(dedup)"
        print(f"  {as_of_r:12} {val:>12,.2f} {(unit or ''):6} {(period or ''):8} -> {lvl:>10}")

    today = as_of or conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    cur = value_on(pts, today)
    print()
    if cur:
        flag = "  <- STALE, gate conviction" if cur.stale_days > 15 else ""
        print(f"  carried forward to {today}: {cur.value:,.2f} "
              f"(from {cur.date}, {cur.stale_days}d old){flag}")
    else:
        print(f"  no value resolvable at {today}")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--show")
    ap.add_argument("--as-of")
    a = ap.parse_args()
    if a.file:
        return load(pathlib.Path(a.file))
    if a.show:
        return show(a.show, a.as_of)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
