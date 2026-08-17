"""Backfill the ECONOMICS pillar (P1+P2) alone, over a long window.

WHY THIS EXISTS RATHER THAN `run_scores.py --backfill`. That computes all four
pillars. Three of them cannot be computed historically -- mood, guidance and
observations all come from the broker digests, whose corpus starts 2026-06-17,
and valuation is anchored to a hardcoded 2026-04-01..2026-06-30 multiple window
against an FY27 EBITDA base. Running the full scorer over a year would write
rows that LOOK like scores and are not. P1 is different: it is commodity prices
times authored intensities, so it genuinely extends as far as clean prices do.

CONTRACT ROLLS ARE THE REAL HAZARD. check_corporate_actions flags front-month
rolls that are not price moves. A 30-day shock window starting inside one books
the roll as a shock -- a 21% alumina "crash" that never happened, straight into
the pillar. Dates whose lookback spans a roll are marked contaminated and
excluded rather than silently scored.

Usage:
    python packages/score/backfill_p1.py --days 365
    python packages/score/backfill_p1.py --days 365 --write
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bridge import (load_specs, load_scoring, load_accumulation, run_bridge,  # noqa: E402
                    shocks_from_store, _series_in_store)
from scoring import score as to_score  # noqa: E402
from run_scores import code_sha, put, SPEC_VERSION  # noqa: E402

LOOKBACK = 30        # matches run_scores' shock window

# From check_corporate_actions.py on the two-year tape. Front-month futures
# rolls, NOT price moves. VEDL's demerger is handled separately as a hard wall.
ROLLS = [("alumina_index", "2025-02-03"), ("midwest_premium", "2025-02-03"),
         ("midwest_premium", "2025-06-02"), ("midwest_premium", "2025-06-03"),
         ("silver", "2026-01-30")]
VEDL_DEMERGER = "2026-04-30"
# VAML listed 2026-06-15. P1 is commodity prices x intensities and needs no
# stock price, so the bridge will happily score a company that did not trade --
# 229 dates of it on the first run. An economics score for an unlisted entity is
# not wrong arithmetic, it is an entity that could not be bought.
LISTED_FROM = {"vaml": "2026-06-15"}


def contaminated(as_of: str) -> list[str]:
    """Rolls whose break falls inside this date's 30-day shock lookback."""
    d1 = dt.date.fromisoformat(as_of)
    d0 = d1 - dt.timedelta(days=LOOKBACK)
    return [f"{s}@{r}" for s, r in ROLLS if d0 <= dt.date.fromisoformat(r) <= d1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    entities, units, fin = load_specs()
    fins = fin["companies"]
    form, k, p = load_scoring()
    acc, hl = load_accumulation()
    available = _series_in_store()
    sha = code_sha()

    cutoff = (dt.date.today() - dt.timedelta(days=a.days)).isoformat()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM prices WHERE entity_id='hindalco' AND date>=? "
        "ORDER BY date", (cutoff,))]

    stats = {"scored": 0, "withheld": 0, "skipped_roll": 0, "skipped_demerger": 0,
             "skipped_unlisted": 0}
    per_entity: dict[str, int] = {}
    for as_of in dates:
        bad = contaminated(as_of)
        for ent in sorted(entities.values(), key=lambda e: e["id"]):
            if not ent.get("peer_group"):
                continue
            eid = ent["id"]
            # VEDL before the demerger is a different company entirely.
            if eid == "vedanta" and as_of <= VEDL_DEMERGER:
                stats["skipped_demerger"] += 1
                continue
            if as_of < LISTED_FROM.get(eid, "0000-00-00"):
                stats["skipped_unlisted"] += 1
                continue
            if bad:
                stats["skipped_roll"] += 1
                continue
            shocks, _d, _r, fx = shocks_from_store(LOOKBACK, as_of, acc, hl)
            r = run_bridge(ent, shocks, units, fins.get(eid, {}).get("base_ebitda", 0),
                           fx or fin["usdinr"], available | set(shocks))
            pct = r["pct_of_ebitda"]
            if pct is not None and r["coverage_ok"]:
                s = to_score(pct, k, form, p)
                stats["scored"] += 1
                per_entity[eid] = per_entity.get(eid, 0) + 1
                if a.write:
                    put(conn, as_of, eid, "economics", s, pct,
                        {"d_ebitda_cr": round(r["d_ebitda_cr"], 1),
                         "priced": f"{r['n_priced']}/{r['n_total']}"}, None, sha)
            else:
                stats["withheld"] += 1
    if a.write:
        conn.commit()

    print(f"window {dates[0]} .. {dates[-1]}  ({len(dates)} trading days)")
    for k_, v in stats.items():
        print(f"  {k_:20} {v}")
    print("  scored per entity:", dict(sorted(per_entity.items())))
    if not a.write:
        print("\nDRY RUN — nothing written. Re-run with --write.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
