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
import datetime as dt
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bridge import (load_specs, load_scoring, load_accumulation, run_bridge,  # noqa: E402
                    shocks_from_store, _series_in_store)
from scoring import score as to_score, solve_k  # noqa: E402
import mood as mood_mod  # noqa: E402
import valuation as val_mod  # noqa: E402
import guidance_runrate as gr  # noqa: E402

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


def score_one_date(conn, as_of: str, sha: str,
                   skip_gate: bool = False) -> int:
    entities, units, fin = load_specs()
    fins = fin["companies"]
    form, k, p = load_scoring()
    acc, hl = load_accumulation()
    available = _series_in_store()
    # One definition of the base quarter, from base_financials.yaml. It used to
    # be a literal here while base_ebitda lived in the spec, so the two could
    # drift apart silently and mark one quarter's earnings against another
    # quarter's prices.
    bq = fin.get("base_quarter") or {}
    if not (bq.get("start") and bq.get("end")):
        raise SystemExit("base_financials.yaml has no base_quarter{start,end}; "
                         "P3 valuation cannot re-mark without it.")
    n = 0

    shocks, _detail, _resolved, fx = shocks_from_store(30, as_of, acc, hl)
    usdinr = fx or fin["usdinr"]

    # ---- THE STALENESS GATE ------------------------------------------------
    # docs/DAILY_MONITORING.md, Tier 1: "On any staleness breach: report it, and
    # withhold the affected scores. Invariant 7 — withhold rather than guess. A
    # stale price is not a flat price."
    #
    # That was written 2026-08-18 and never implemented. On 2026-08-21 this file
    # produced five economics scores on inputs 4 to 81 days stale, with ZERO
    # withheld rows, while freshness.py printed a warning nobody was required to
    # read. On a cron nobody reads it at all.
    #
    # Withholding is not a cost here — it is the product. A recorded withholding
    # says "we could not tell", which the review layer can grade. A score
    # computed on a dead feed says "nothing is happening", which is a claim, and
    # a false one: carry-forward means a broken source and a quiet market are
    # byte-identical in the store (cp_coke has 3 distinct closes in 30 rows).
    #
    # PER ENTITY, not global. A stale coal print must not withhold a zinc name
    # that never consumes coal, so the gate resolves each entity's own
    # price_links and only fires on the ones it actually reads.
    stale_series: dict[str, str] = {}
    if not skip_gate:
        sys.path.insert(0, str(REPO / "packages" / "core"))
        import freshness
        fr = freshness.check(dt.date.fromisoformat(as_of))
        stale_series = {x["series"]: x["age_txt"] for x in fr["stale"]
                        if x["series"] != "oi"}   # OI is not a bridge input
        if stale_series:
            print(f"staleness gate: {len(stale_series)} series over threshold — "
                  + ", ".join(f"{k} ({v})" for k, v in sorted(stale_series.items())))


    def consumed_by(ent: dict) -> set[str]:
        """price_links this entity reads, including through its reporting units."""
        out = set()
        for e in [ent] + [x for x in entities.values()
                          if x.get("parent_id") == ent["id"]]:
            for ln in (e.get("outputs") or []) + (e.get("inputs") or []):
                if ln.get("price_link"):
                    out.add(ln["price_link"])
        return out

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
        # Gate first: a stale INPUT invalidates the bridge before coverage does.
        # coverage_ok asks "is every line priced at all", which a carried-forward
        # dead feed answers yes to.
        bad = sorted(consumed_by(ent) & set(stale_series))
        r = run_bridge(ent, shocks, units, f.get("base_ebitda", 0), usdinr,
                       available | set(shocks))
        pct = r["pct_of_ebitda"]
        if bad:
            put(conn, as_of, eid, "economics", None, pct, None,
                "stale inputs: " + "; ".join(f"{s} {stale_series[s]}" for s in bad),
                sha)
        elif pct is not None and r["coverage_ok"]:
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
            conn, ent, f, units, bq["start"], bq["end"], usdinr)
        ser = [x for x in ser if x[0] <= as_of]
        # P3 re-marks EV/EBITDA at the CURRENT price, so a stale equity close
        # means it is re-marking at an old price and calling it spot.
        if eid in stale_series:
            put(conn, as_of, eid, "valuation", None, None, None,
                f"stale price: {eid} {stale_series[eid]}", sha)
        elif len(ser) >= 20:
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
        # RUN-RATE ARITHMETIC, replacing the hand-weighted evidence vote that was
        # here until 2026-08-20. The old version summed guidance_evidence
        # direction x weight through a sigmoid and never read target_value at
        # all, so HZL's committed 1.1mt / 680t / $975-1,000 played no part in
        # scoring HZL's own guidance. Worse, three of its five evidence rows were
        # "guidance reiterated" — management repeating itself drifting confidence
        # UPWARD, the inverse of invariant 3. It scored HZL 3.61 while the company
        # was behind on both volume commitments.
        #
        # Now: actual vs target, cited both ends, weighted by how much of the
        # period has elapsed. guidance_evidence is no longer read here; it is for
        # facts arithmetic cannot reach (a regulatory clearance), not for numbers
        # it can.
        gscore, gconf, gdetail, gwithheld = gr.score_entity(conn, eid, as_of)
        if gscore is not None:
            parts["guidance"] = gscore
            put(conn, as_of, eid, "guidance", gscore, gconf, gdetail, None, sha)
        else:
            put(conn, as_of, eid, "guidance", None, None, None, gwithheld, sha)
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
    # A BACKFILL MUST SKIP THE GATE. freshness.check() measures age against the
    # date being scored, so every historical date is "stale" by construction —
    # scoring 2021 would withhold all of it. The gate is about TODAY's feeds
    # being alive, which is only a meaningful question for a current run.
    ap.add_argument("--skip-gate", action="store_true",
                    help="score even on stale feeds (implied by --backfill)")
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
        total += score_one_date(conn, d, sha, skip_gate=a.skip_gate or bool(a.backfill))
        conn.commit()
    print(f"wrote {total} pillar_scores rows across {len(dates)} dates "
          f"({dates[0]} .. {dates[-1]}) at spec {SPEC_VERSION} / {sha}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
