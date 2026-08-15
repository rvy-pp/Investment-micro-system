"""Validate and load extracted guidance + evidence, then score P4.

THE MODEL EXTRACTS, THIS CODE VALIDATES AND SCORES. Extraction output is JSON;
every row must carry a verbatim quote or the schema rejects it.

A DISTINCTION THE CANDIDATE SCAN CANNOT MAKE: "reiterates" matches both
  management: "Guidance reiterated: 1.1mt refined metal"      -> guidance
  broker:     "BofA reiterates Underperform, PO Rs515"        -> broker_actions
These are different claims about different actors. A broker restating its
rating says nothing about whether MANAGEMENT hits its numbers, and letting it
into the guidance table would corrupt the track record that P4 is built on.
`source_actor` is therefore required on every row and validated.

P4 SCORING IS LINEAR, unlike P1+P2. The hill curve exists to squash an
UNBOUNDED quantity (EBITDA % change) onto a bounded scale. A confidence is
already a calibrated probability in [0,1]; squashing it again would distort a
number that is already in the right units. score = 1 + 4 * confidence.

Usage:
    python packages/extract/load_guidance.py --file specs/extracted/hzl_guidance.json
    python packages/extract/load_guidance.py --score
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

NEUTRAL_PRIOR = 0.5      # used until a company has resolved commitments
EVIDENCE_STRENGTH = 1.2  # log-odds per unit of evidence weight


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: pathlib.Path) -> int:
    doc = json.loads(path.read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    n_g = n_e = n_b = 0

    for src in doc.get("sources", []):
        conn.execute(
            "INSERT OR IGNORE INTO sources (id,kind,origin,title,source_date,"
            "captured_at,raw_path) VALUES (?,?,?,?,?,?,?)",
            (src["id"], src["kind"], src.get("origin"), src.get("title"),
             src["source_date"], now(), src["raw_path"]),
        )

    for g in doc.get("guidance", []):
        if g.get("source_actor") != "management":
            raise ValueError(
                f"guidance row for {g['entity_id']} has source_actor="
                f"{g.get('source_actor')!r}. Only MANAGEMENT commitments belong "
                f"in `guidance`; a broker rating goes to broker_actions."
            )
        cur = conn.execute(
            "INSERT INTO guidance (entity_id,source_id,issued_date,period,metric,"
            "target_type,target_value,target_low,target_high,target_dir,unit,"
            "quote,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (g["entity_id"], g["source_id"], g["issued_date"], g["period"],
             g["metric"], g["target_type"], g.get("target_value"),
             g.get("target_low"), g.get("target_high"), g.get("target_dir"),
             g.get("unit"), g["quote"], g.get("status", "open"), now()),
        )
        g["_id"] = cur.lastrowid
        n_g += 1

        for ev in g.get("evidence", []):
            conn.execute(
                "INSERT INTO guidance_evidence (guidance_id,source_id,as_of,"
                "direction,weight,quote,created_at) VALUES (?,?,?,?,?,?,?)",
                (g["_id"], ev["source_id"], ev["as_of"], ev["direction"],
                 ev["weight"], ev["quote"], now()),
            )
            n_e += 1

    for b in doc.get("broker_actions", []):
        conn.execute(
            "INSERT INTO broker_actions (source_id,entity_id,broker,action_date,"
            "action,rating_from,rating_to,tp_from,tp_to,currency,quote,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (b["source_id"], b["entity_id"], b["broker"], b["action_date"],
             b["action"], b.get("rating_from"), b.get("rating_to"),
             b.get("tp_from"), b.get("tp_to"), b.get("currency", "INR"),
             b["quote"], now()),
        )
        n_b += 1

    conn.commit()
    conn.close()
    print(f"loaded {n_g} guidance, {n_e} evidence, {n_b} broker actions")
    return 0


def score() -> int:
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT g.id, g.entity_id, g.period, g.metric, g.target_type, "
        "g.target_value, g.target_low, g.target_high, g.unit, g.status, g.quote "
        "FROM guidance g WHERE g.status='open' ORDER BY g.entity_id, g.period"
    ).fetchall()
    if not rows:
        print("no open guidance loaded")
        return 0

    track = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT entity_id, hit_rate, n_resolved FROM v_guidance_track_record")}

    print(f"{'entity':16} {'period':6} {'metric':14} {'target':>12} {'unit':20} {'for':>3} {'against':>7} {'conf':>6} {'P4':>5}")
    print("-" * 92)

    by_entity: dict[str, list[float]] = {}
    for gid, eid, period, metric, ttype, tval, tlo, thi, unit, status, quote in rows:
        ev = conn.execute(
            "SELECT direction, weight, quote FROM guidance_evidence "
            "WHERE guidance_id=? ORDER BY as_of", (gid,)
        ).fetchall()
        n_for = sum(1 for d, _, _ in ev if d > 0)
        n_against = sum(1 for d, _, _ in ev if d < 0)

        prior, n_res = track.get(eid, (None, 0))
        p0 = prior if (prior is not None and n_res >= 3) else NEUTRAL_PRIOR
        # log-odds update: neutral prior + signed, weighted evidence
        lo = math.log(p0 / (1 - p0))
        for d, w, _ in ev:
            lo += d * w * EVIDENCE_STRENGTH
        conf = 1 / (1 + math.exp(-lo))

        def fmt(v: float) -> str:
            # 1100000 renders as 1.1e+06 under %g, which is unreadable in a
            # tonnage column. Scale into the natural unit instead.
            if abs(v) >= 1e6:
                return f"{v/1e6:g}m"
            if abs(v) >= 1e3:
                return f"{v:,.0f}"
            return f"{v:g}"

        target = (f"{fmt(tlo)}-{fmt(thi)}" if ttype == "range"
                  else fmt(tval) if tval is not None else "—")
        u = (unit or "")[:18]
        p4 = 1 + 4 * conf
        by_entity.setdefault(eid, []).append(p4)
        print(f"{eid:16} {period:6} {metric:14} {target:>12} {u:20} "
              f"{n_for:>3} {n_against:>7} {conf:>6.2f} {p4:>5.2f}")

    print("\nP4 by entity (mean across open commitments):")
    for eid, scores in sorted(by_entity.items()):
        print(f"  {eid:16} {sum(scores)/len(scores):.2f}   "
              f"({len(scores)} open commitment{'s' if len(scores) > 1 else ''})")

    if not track:
        print("\nNOTE: no RESOLVED commitments yet, so every prior is the neutral "
              "0.5 rather than the company's own hit rate. P4 currently reflects "
              "evidence only. The track record — the half that makes this "
              "predictive — accumulates as commitments resolve.")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.file:
        return load(pathlib.Path(a.file))
    if a.score:
        return score()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
