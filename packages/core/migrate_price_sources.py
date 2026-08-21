"""One-off: stamp prices.source on rows written before the column existed.

    python packages/core/migrate_price_sources.py            # report
    python packages/core/migrate_price_sources.py --apply

Four adapters wrote `prices` with INSERT OR REPLACE and no source column, so
provenance was never recorded. Precedence in prices_io.py cannot work until the
existing rows say who wrote them, because an unstamped row ranks 0 and ANY source
may replace it — including replacing the licensed Daily Metals Pack with a free
mirror.

ONLY UNAMBIGUOUS CLAIMS ARE STAMPED. Everything else is left NULL on purpose. A
wrong stamp is worse than no stamp: it would permanently protect a bad cell from
being corrected, which is the opposite of what precedence is for.

  lme_zinc -> metals_pack
      The pack is the only writer that can have produced it. yahoo_prices.py
      lists CANDIDATES["lme_zinc"] = [] with "NO FREE ZINC ON YAHOO"; wind_zinc.py
      loads zinc_shfe and is explicit that it must NOT be aliased to lme_zinc;
      fred_prices.py covers coal. Corroborated numerically: the store agrees with
      westmetall's LME CASH column to a mean absolute 21.89 USD/t over 159
      overlapping dates, with 1 date beyond 80 — i.e. it is cash, which is what
      the pack carries.

  lme_aluminium -> LEFT NULL, deliberately
      Genuinely mixed and not recoverable. The pack writes LME cash and
      yahoo_prices.py writes ALI=F, which is CME and embeds a Midwest premium;
      Yahoo re-runs daily over a 3-month range, so it has overwritten an unknown
      subset. The evidence is a bimodal gap against westmetall cash: 70 of 161
      dates within 30 USD/t (pack-like) and 65 beyond 80 (CME-like), mean
      absolute 119.68. Leaving it NULL lets westmetall replace the mixture with a
      single consistent LME cash series, which is the point.

  everything else -> LEFT NULL
      Equities, FX, silver, brent and the assessed series all have more than one
      plausible writer, or none worth protecting. They can be stamped later by
      the adapters themselves as those re-run.
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(REPO / "packages" / "core"))

import prices_io  # noqa: E402

# series -> (source, why). Add a row only when ONE writer is possible.
CLAIMS = {
    "lme_zinc": ("metals_pack",
                 "sole possible writer: Yahoo has no zinc candidate, Wind writes "
                 "zinc_shfe, FRED writes coal. Matches westmetall CASH to 21.89 "
                 "mean abs over 159 dates"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    added = prices_io.ensure_source_column(conn)
    if added:
        print("added prices.source column")

    total = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    unstamped = conn.execute(
        "SELECT COUNT(*) FROM prices WHERE source IS NULL").fetchone()[0]
    print(f"{total:,} price rows, {unstamped:,} with no source\n")

    print(f"{'series':18}{'rows':>8}{'to stamp':>10}  source")
    print("-" * 76)
    plan = []
    for eid, (src, _why) in sorted(CLAIMS.items()):
        n = conn.execute(
            "SELECT COUNT(*) FROM prices WHERE entity_id=? AND source IS NULL",
            (eid,)).fetchone()[0]
        tot = conn.execute("SELECT COUNT(*) FROM prices WHERE entity_id=?",
                           (eid,)).fetchone()[0]
        print(f"{eid:18}{tot:>8,}{n:>10,}  {src}")
        plan.append((eid, src, n))

    left = conn.execute(
        "SELECT entity_id, COUNT(*) FROM prices WHERE source IS NULL "
        "AND entity_id NOT IN (%s) GROUP BY entity_id ORDER BY entity_id"
        % ",".join("?" * len(CLAIMS)), tuple(CLAIMS)).fetchall()
    print(f"\nleft NULL on purpose (rank 0, any source may replace):")
    for eid, n in left:
        print(f"   {eid:22}{n:>8,}")

    if not a.apply:
        print("\nreport only — pass --apply to write")
        conn.close()
        return 0

    for eid, src, _n in plan:
        conn.execute("UPDATE prices SET source=? WHERE entity_id=? AND source IS NULL",
                     (src, eid))
    conn.commit()
    done = conn.execute(
        "SELECT source, COUNT(*) FROM prices WHERE source IS NOT NULL "
        "GROUP BY source").fetchall()
    print("\nstamped:")
    for s, n in done:
        print(f"   {s:22}{n:>8,}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
