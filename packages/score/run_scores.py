"""Compute all pillars for a date and PERSIST them. The system's daily write.

Until this existed the pillars printed and vanished, so there was no score
history — nothing to backtest, nothing for the review layer to grade, and no
way to ask whether the pillars disagreed last week.

Every row is stamped with spec_version and the git sha of the code that
produced it, so a scoring change is a re-run over history rather than a silent
rewrite of what the system used to think.

WITHHELD IS RECORDED, NOT SKIPPED. A name with no share count gets a row saying
so. Otherwise a gap in the history is ambiguous — did we not score it, or did
we score it and get nothing? The review layer needs to tell those apart.

Usage:
    python packages/score/run_scores.py --as-of 2026-08-15
    python packages/score/run_scores.py --backfill 60
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bridge import (load_specs, load_scoring, load_accumulation, run_bridge,  # noqa: E402
                    shocks_from_store, _series_in_store)
from scoring import score as to_score, solve_k  # noqa: E402
import mood as mood_mod  # noqa: E402
import valuation as val_mod  # noqa: E402

SPEC_VERSION = "0.5.0"
# Pillar weights for the composite. Economics leads because it is the only
# pillar measuring the thing itself; the rest qualify it.
WEIGHTS = {"economics": 0.45, "valuation": 0.25, "mood": 0.15, "guidance": 0.15}


def code_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                              capture_output=True, text=True).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put(conn, as_of, eid, pillar, score, raw, detail, withheld, sha):
    conn.execute(
        "INSERT OR REPLACE INTO pillar_scores (as_of,entity_id,pillar,score,raw,"
        "detail,withheld,spec_version,code_sha,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (as_of, eid, pillar, score, raw,
         json.dumps(detail) if detail else None, withheld,
         SPEC_VERSION, sha, now()))


def score_one_date(conn, as_of: str, sha: str) -> int:
    entities, units, fin = load_specs()
    fins = fin["companies"]
    form, k, p = load_scoring()
    acc, hl = load_accumulation()
    available = _series_in_store()
    n = 0

    shocks, _detail, _resolved, fx = shocks_from_store(30, as_of, acc, hl)
    usdinr = fx or fin["usdinr"]
    val_k = solve_k("hill", val_mod.Z_ANCHOR, val_mod.SCORE_ANCHOR, val_mod.P)
    mood_k = solve_k("hill", mood_mod.MOOD_ANCHOR, mood_mod.SCORE_ANCHOR, mood_mod.P)

    for ent in sorted(entities.values(), key=lambda e: e["id"]):
        pg = ent.get("peer_group")
        if not pg:
            continue
        eid = ent["id"]
        f = fins.get(eid, {})
        parts: dict[str, float] = {}

        # --- economics (P1+P2) ---
        r = run_bridge(ent, shocks, units, f.get("base_ebitda", 0), usdinr,
                       available | set(shocks))
        pct = r["pct_of_ebitda"]
        if pct is not None and r["coverage_ok"]:
            s = to_score(pct, k, form, p)
            parts["economics"] = s
            put(conn, as_of, eid, "economics", s, pct,
                {"d_ebitda_cr": round(r["d_ebitda_cr"], 1),
                 "priced": f"{r['n_priced']}/{r['n_total']}"}, None, sha)
        else:
            put(conn, as_of, eid, "economics", None, pct, None,
                f"coverage {r['n_priced']}/{r['n_total']}", sha)
        n += 1

        # --- valuation (P3a) ---
        ser, cut, _cp, err = val_mod.spot_multiple_series(
            conn, ent, f, units, "2026-04-01", "2026-06-30", usdinr)
        ser = [x for x in ser if x[0] <= as_of]
        if len(ser) >= 20:
            import statistics
            mults = [m for _, m, _ in ser]
            z = (mults[-1] - statistics.fmean(mults)) / (statistics.pstdev(mults) or 1e-9)
            s = to_score(-z, val_k, "hill", val_mod.P)
            parts["valuation"] = s
            put(conn, as_of, eid, "valuation", s, z,
                {"multiple": round(mults[-1], 2), "n": len(mults)}, None, sha)
        else:
            put(conn, as_of, eid, "valuation", None, None, None,
                err or "insufficient clean history", sha)
        n += 1

        # --- mood (P3b) ---
        c_raw, brokers, events = mood_mod.company_mood(conn, eid, as_of)
        s_raw, pol = mood_mod.policy_mood(conn, eid, ent.get("sector") or "", as_of)
        gate = mood_mod.gate(len(brokers), len(events) + len(pol))
        eff = (c_raw + s_raw) * gate
        if events or pol:
            s = to_score(eff, mood_k, "hill", mood_mod.P)
            parts["mood"] = s
            put(conn, as_of, eid, "mood", s, eff,
                {"brokers": len(brokers), "events": len(events) + len(pol),
                 "gate": round(gate, 2)}, None, sha)
        else:
            put(conn, as_of, eid, "mood", None, None, None, "no events", sha)
        n += 1

        # --- guidance (P4) ---
        g = conn.execute(
            "SELECT COUNT(*) FROM guidance WHERE entity_id=? AND status='open' "
            "AND issued_date<=?", (eid, as_of)).fetchone()[0]
        if g:
            import math
            tot, cnt = 0.0, 0
            for (gid,) in conn.execute(
                    "SELECT id FROM guidance WHERE entity_id=? AND status='open' "
                    "AND issued_date<=?", (eid, as_of)):
                lo = 0.0
                for d, w in conn.execute(
                        "SELECT direction, weight FROM guidance_evidence "
                        "WHERE guidance_id=? AND as_of<=?", (gid, as_of)):
                    lo += d * w * 1.2
                tot += 1 / (1 + math.exp(-lo))
                cnt += 1
            conf = tot / cnt
            s = 1 + 4 * conf
            parts["guidance"] = s
            put(conn, as_of, eid, "guidance", s, conf,
                {"commitments": cnt}, None, sha)
        else:
            put(conn, as_of, eid, "guidance", None, None, None,
                "no open guidance", sha)
        n += 1

        # --- composite: re-weighted over the pillars that ACTUALLY scored ---
        # Treating a withheld pillar as 3.0 would let missing data masquerade as
        # neutral evidence. Renormalising over what exists keeps the composite
        # honest, and `covered` records how much of the intended weight it rests on.
        if parts:
            wsum = sum(WEIGHTS[k_] for k_ in parts)
            comp = sum(parts[k_] * WEIGHTS[k_] for k_ in parts) / wsum
            put(conn, as_of, eid, "composite", comp, None,
                {"pillars": {k_: round(v, 2) for k_, v in parts.items()},
                 "covered": round(wsum, 2)}, None, sha)
        else:
            put(conn, as_of, eid, "composite", None, None, None,
                "no pillar scored", sha)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--backfill", type=int, default=0)
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    sha = code_sha()

    if a.backfill:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM prices WHERE entity_id='lme_aluminium' "
            "ORDER BY date DESC LIMIT ?", (a.backfill,))]
        dates = sorted(dates)
    else:
        dates = [a.as_of or conn.execute(
            "SELECT MAX(date) FROM prices").fetchone()[0]]

    total = 0
    for d in dates:
        total += score_one_date(conn, d, sha)
        conn.commit()
    print(f"wrote {total} pillar_scores rows across {len(dates)} dates "
          f"({dates[0]} .. {dates[-1]}) at spec {SPEC_VERSION} / {sha}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
