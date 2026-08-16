"""Replay the bridge day by day — the score path, not a single snapshot.

Answers "how does the score move if we run this daily", and does it by
computing rather than describing.

IT ALSO EXPOSES THE ROLL-OFF ARTEFACT. A trailing window has TWO moving ends.
Tomorrow the newest price is added AND the oldest drops out, so the score
changes even on a day when nothing at all happened — the drop-out alone moves
it. That is score movement with no new information, and it is exactly why
specs/scoring.yaml specifies EWMA accumulation. The bridge does not implement
EWMA yet, so this tool measures how big the artefact currently is instead of
leaving it as an assertion.

Usage:
    python packages/score/history.py --peer-group aluminium_primary --days 40
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bridge import (  # noqa: E402
    load_specs, load_scoring, run_bridge, shocks_from_store, _series_in_store,
)
from scoring import score as to_score  # noqa: E402


def trading_days(n: int) -> list[str]:
    conn = sqlite3.connect(REPO / "data" / "ims.db")
    rows = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM prices WHERE entity_id='lme_aluminium' "
        "ORDER BY date DESC LIMIT ?", (n,))]
    conn.close()
    return sorted(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer-group", default="aluminium_primary")
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--accum", choices=["window","ewma"], default="window")
    ap.add_argument("--half-life", type=float, default=10.0)
    a = ap.parse_args()

    entities, units, fin = load_specs()
    fins = fin["companies"]
    form, k, p = load_scoring()
    available = _series_in_store()
    members = [e for e in entities.values() if e.get("peer_group") == a.peer_group]
    members.sort(key=lambda e: e["id"])

    days = trading_days(a.days)
    print(f'{a.peer_group} · {a.accum}' + (f' hl={a.half_life:g}d' if a.accum=='ewma' else f' {a.window}d window') + f' · {len(days)} sessions\n')
    hdr = f"{'date':12}" + "".join(f"{e['id']:>12}" for e in members) + \
          f"{'':4}" + "".join(f"{'Δ'+e['id'][:6]:>10}" for e in members)
    print(hdr)
    print("-" * len(hdr))

    prev: dict[str, float] = {}
    path: dict[str, list[float]] = {e["id"]: [] for e in members}
    moves: dict[str, list[float]] = {e["id"]: [] for e in members}

    for d in days:
        shocks, _detail, as_of, fx = shocks_from_store(a.window, d, a.accum, a.half_life)
        usdinr = fx or fin["usdinr"]
        avail = set(shocks) | available
        line, dline = f"{d:12}", ""
        for e in members:
            f = fins.get(e["id"], {})
            r = run_bridge(e, shocks, units, f.get("base_ebitda", 0), usdinr, avail)
            pct = r["pct_of_ebitda"]
            s = (to_score(pct, k, form, p)
                 if (pct is not None and r["coverage_ok"]) else None)
            line += f"{'—' if s is None else f'{s:.2f}':>12}"
            if s is not None:
                path[e["id"]].append(s)
                if e["id"] in prev:
                    mv = s - prev[e["id"]]
                    moves[e["id"]].append(mv)
                    dline += f"{mv:+10.2f}"
                else:
                    dline += f"{'':>10}"
                prev[e["id"]] = s
            else:
                dline += f"{'':>10}"
        print(line + "    " + dline)

    print("\nday-to-day movement:")
    print(f"  {'entity':14} {'range':>14} {'mean |Δ|':>10} {'max |Δ|':>9} "
          f"{'days moved >0.10':>18}")
    for e in members:
        eid = e["id"]
        ps, mv = path[eid], moves[eid]
        if not ps:
            continue
        big = sum(1 for m in mv if abs(m) > 0.10)
        print(f"  {eid:14} {f'{min(ps):.2f}–{max(ps):.2f}':>14} "
              f"{sum(abs(m) for m in mv)/max(len(mv),1):>10.3f} "
              f"{max((abs(m) for m in mv), default=0):>9.2f} "
              f"{f'{big}/{len(mv)}':>18}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
