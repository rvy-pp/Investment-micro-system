"""The persisted score tape — pillar_scores joined to prices, for the pair view.

READS, NEVER RECOMPUTES. These are the same rows combined.py reads and the
review layer will grade later. engine.py is the other half of the API and does
the opposite: it re-runs bridge.py in memory so the Inputs tab's override
what-if loop works. Keeping them separate is deliberate — a number on the pair
chart must be one that was actually stored, or the chart and the backtest are
describing different systems.

Consequence to state honestly in the UI: this view is only as fresh as the last
run_scores.py. as_of_max is returned for exactly that reason.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(REPO / "packages" / "score"))

from bridge import load_specs, load_scoring          # noqa: E402
from scoring import solve_k                          # noqa: E402

PILLARS = ["composite", "economics", "valuation", "mood", "guidance"]

# A single-session move this large in a large-cap is a corporate action until
# proven otherwise. Same threshold as adapters/check_corporate_actions.py; kept
# in sync by hand because that script is a CLI and this is a request path.
JUMP = 0.15

# CONFIRMED corporate actions — these BREAK the chart line.
#
# Everything else the JUMP scan finds is a CANDIDATE and gets a tick mark only.
# The distinction is load-bearing and was nearly missed. Scanning the five names
# back to 2011 at 15% returns eleven hits, and all but one are REAL MARKET
# MOVES, not corporate actions:
#
#   hindalco       2020-03-23, 2020-04-07   COVID crash and rebound
#   nalco          2024-06-04               Indian election result day
#   hindustan_zinc 2024-04-09/05-10/05-21   the 2024 HZL rally
#   vedanta        2020-03-23, 2020-10-12   COVID; the failed delisting
#
# Breaking the line at those would erase genuine performance — a −20% election
# day is exactly the kind of relative move a pair chart exists to show. Only the
# VEDL demerger is a mechanical step: 773.60 -> 271.55 on 2026-04-30, −64.9%,
# four entities demerged 1:1 with a 1 May 2026 record date. Documented in
# CLAUDE.md and detected by adapters/check_corporate_actions.py.
#
# Add to this list only when the action is verified against a filing. A wrong
# entry here silently deletes a real return from the chart.
CONFIRMED_ACTIONS = {
    "vedanta": {"2026-04-30": "1:1 demerger of four entities, record date 1 May 2026"},
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# curves — so the client can SCORE THE SPREAD instead of spreading the scores
# ---------------------------------------------------------------------------

def curves() -> dict:
    """Per-pillar curve parameters, and whether a pair spread can be scored.

    specs/scoring.yaml pairs.rule: score_the_spread says the naive difference of
    two bounded scores understates the tails, and adds: "it will apply to the P3
    and P4 scores too, so do the same there." Doing so needs each pillar's OWN
    k, because each anchors on a different quantity:

        economics   x = pct_of_ebitda      anchor 0.05 -> 4.0   (k = 0.05)
        valuation   x = -z                 anchor 1.00 -> 4.0   (k = 1.00)
        mood        x = effective mood     anchor 2.00 -> 4.0   (k = 2.00)

    Reusing the economics k = 0.05 on a z-score would read a 1.4sd valuation gap
    as a 28x-anchor move and pin the pair at 5.0. The three anchors are NOT
    interchangeable, which is the whole reason this returns per-pillar values
    rather than one k.

    Two pillars cannot be spread-scored, for different reasons, and the UI must
    say which is which rather than showing a bare number:

      guidance   scores LINEARLY (1 + 4*confidence). A linear map has no tail
                 compression, so score_long - score_short is already exact.
                 Nothing to correct.
      composite  is a blend of four scores and stores raw = NULL. There is no
                 single x to difference, so the naive difference is the only
                 option available and it carries the understatement that
                 scoring.yaml warns about.
    """
    form, k_econ, p = load_scoring()
    return {
        "economics": {"form": form, "k": k_econ, "p": p, "sign": 1,
                      "spreadable": True,
                      "unit": "% of EBITDA",
                      "why": "raw is pct_of_ebitda; k is the scoring.yaml anchor"},
        "valuation": {"form": "hill", "k": solve_k("hill", 1.0, 4.0, 1.5),
                      "p": 1.5, "sign": -1,
                      "spreadable": True,
                      "unit": "z (sd vs own history)",
                      "why": "raw is z; cheap (negative z) scores HIGH, hence sign -1"},
        "mood":      {"form": "hill", "k": solve_k("hill", 2.0, 4.0, 1.5),
                      "p": 1.5, "sign": 1,
                      "spreadable": True,
                      "unit": "effective mood (post-gate)",
                      "why": "raw is gated mood; anchor is two clean upgrades"},
        "guidance":  {"form": "linear_exact", "k": None, "p": None, "sign": 1,
                      "spreadable": False,
                      "unit": "confidence",
                      "why": "P4 is linear (1 + 4*conf), so the plain difference "
                             "is already exact — there is no compression to undo"},
        "composite": {"form": None, "k": None, "p": None, "sign": 1,
                      "spreadable": False,
                      "unit": None,
                      "why": "a blend of four scores; raw is NULL, so no single x "
                             "exists to difference. The plain difference is all "
                             "there is, and it understates in the tails"},
    }


# ---------------------------------------------------------------------------
# corporate actions — a step in an unadjusted series is not a return
# ---------------------------------------------------------------------------

def corporate_actions(conn, entity_ids: list[str]) -> dict[str, dict]:
    """Price steps, split into CONFIRMED (break the line) and CANDIDATE (tick).

    Drawing a relative-performance line through the VEDL demerger reports a 65%
    loss on the long leg that nobody experienced. Splicing it out silently would
    be worse — a plausible number with no trace. So a confirmed action BREAKS
    the series and the UI names the cause.

    A candidate is only a jump the scan noticed. Most are real moves (see
    CONFIRMED_ACTIONS above), so they are marked and drawn THROUGH.
    """
    out: dict[str, dict] = {}
    for eid in entity_ids:
        rows = conn.execute(
            "SELECT date, close FROM prices WHERE entity_id=? ORDER BY date",
            (eid,)).fetchall()
        confirmed, candidates = [], []
        known = CONFIRMED_ACTIONS.get(eid, {})
        for (d0, c0), (d1, c1) in zip(rows, rows[1:]):
            if not c0 or abs(c1 / c0 - 1) < JUMP:
                continue
            rec = {"date": d1, "prev_date": d0, "from": c0, "to": c1,
                   "pct": (c1 / c0 - 1) * 100}
            if d1 in known:
                confirmed.append({**rec, "why": known[d1]})
            else:
                candidates.append(rec)
        # A confirmed action with no matching jump means the list is stale
        # against the tape — surface it rather than quietly finding nothing.
        missing = sorted(set(known) - {c["date"] for c in confirmed})
        if confirmed or candidates or missing:
            out[eid] = {"confirmed": confirmed, "candidates": candidates,
                        "unmatched": missing}
    return out


# ---------------------------------------------------------------------------
# refresh status — so a dead scheduled task cannot look like a quiet market
# ---------------------------------------------------------------------------

def refresh_status() -> dict:
    """What packages/refresh.py last did, for the page header.

    THE FAILURE THIS EXISTS TO PREVENT: a scheduled task that stops running is
    invisible. The server keeps serving, the charts keep drawing, the scores are
    simply old — and old scores look exactly like unchanged scores, because this
    system has no decay by design (invariant 3). Without this the operator would
    have to notice a date, on a page whose whole job is to make dates unremarkable.

    A MISSING status file is reported as "never run", not as an empty dict. The
    absence is the finding.
    """
    p = REPO / "data" / "refresh" / "status.json"
    if not p.exists():
        return {"state": "never", "why": "no data/refresh/status.json — "
                                         "packages/refresh.py has not run"}
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"state": "unreadable", "why": f"{type(exc).__name__}: {exc}"}
    failed = [x["step"] for x in s.get("steps", []) if x.get("status") == "fail"]
    # feeds_ok is reported SEPARATELY from state, never folded into it. A run
    # can succeed against inputs that stopped printing days ago, and collapsing
    # the two into one green light is precisely the failure this whole function
    # exists to prevent — found 2026-08-21 with the light green over zinc_shfe
    # and OI both 5 trading days stale.
    return {
        "state": "ok" if s.get("ok") else "failed",
        "day": s.get("day"),
        "finished": s.get("finished"),
        "failed_steps": failed,
        "feeds_ok": s.get("feeds_ok"),
        "stale_feeds": s.get("stale_feeds", []),
        "worst_feed_age": s.get("worst_feed_age"),
        "skipped_steps": [x["step"] for x in s.get("steps", [])
                          if x.get("status") == "skipped"],
        "manual": s.get("manual", []),
    }


# ---------------------------------------------------------------------------
# the tape
# ---------------------------------------------------------------------------

def tape(pillar: str = "composite", since: str | None = None) -> dict:
    if pillar not in PILLARS:
        raise ValueError(f"unknown pillar {pillar!r}; expected one of {PILLARS}")

    entities, _units, _fin = load_specs()
    groups = {e["id"]: e.get("peer_group") for e in entities.values()
              if e.get("peer_group")}
    names = {e["id"]: e.get("name", e["id"]) for e in entities.values()}

    conn = connect()
    where = "pillar=?" + (" AND as_of>=?" if since else "")
    args = (pillar, since) if since else (pillar,)
    rows = conn.execute(
        "SELECT entity_id, as_of, score, raw, detail, withheld "
        "FROM pillar_scores WHERE " + where + " ORDER BY entity_id, as_of", args
    ).fetchall()

    by_ent: dict[str, list[dict]] = {}
    for r in rows:
        by_ent.setdefault(r["entity_id"], []).append(dict(r))

    ents = sorted(by_ent)
    # Prices are TRADING days; pillar_scores as_of runs on calendar days (61
    # dates over a 62-day span). A plain join drops every weekend score and
    # silently thins the tape by ~30%, which reads as a sparser signal rather
    # than as a join bug. Forward-fill instead: the last close ON OR BEFORE the
    # score date, which is also what the desk could actually have traded on.
    closes: dict[str, list[tuple[str, float]]] = {}
    for eid in ents:
        closes[eid] = [(r[0], r[1]) for r in conn.execute(
            "SELECT date, close FROM prices WHERE entity_id=? AND close IS NOT NULL "
            "ORDER BY date", (eid,))]

    series = {}
    for eid in ents:
        px, i, last, last_d = closes.get(eid, []), 0, None, None
        pts = []
        for r in by_ent[eid]:
            d = r["as_of"]
            while i < len(px) and px[i][0] <= d:
                last, last_d = px[i][1], px[i][0]
                i += 1
            try:
                det = json.loads(r["detail"]) if r["detail"] else None
            except (TypeError, json.JSONDecodeError):
                det = None
            pts.append({
                "d": d,
                "score": r["score"],
                "raw": r["raw"],
                "close": last,
                # How stale the close is on this score date. A price that has
                # not printed for days is not the same evidence as today's.
                "px_date": last_d,
                "withheld": r["withheld"],
                "detail": det,
            })
        series[eid] = {
            "entity_id": eid,
            "name": names.get(eid, eid),
            "peer_group": groups.get(eid),
            "points": pts,
            "n": len(pts),
            "first": pts[0]["d"] if pts else None,
            "last": pts[-1]["d"] if pts else None,
            "n_scored": sum(1 for p in pts if p["score"] is not None),
        }

    ca = corporate_actions(conn, ents)

    # Coverage per pillar, so the UI can say WHY switching to composite shortens
    # the chart from five years to two months instead of just silently doing it.
    coverage = {}
    for p in PILLARS:
        r = conn.execute(
            "SELECT COUNT(DISTINCT as_of), MIN(as_of), MAX(as_of) "
            "FROM pillar_scores WHERE pillar=?", (p,)).fetchone()
        coverage[p] = {"dates": r[0], "first": r[1], "last": r[2]}

    as_of_max = conn.execute(
        "SELECT MAX(as_of) FROM pillar_scores WHERE pillar='composite' "
        "AND score IS NOT NULL").fetchone()[0]
    conn.close()

    return {
        "pillar": pillar,
        "as_of_max": as_of_max,
        "refresh": refresh_status(),
        "series": series,
        "order": ents,
        "corporate_actions": ca,
        "curves": curves(),
        "coverage": coverage,
        "peer_groups": sorted({g for g in groups.values() if g}),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pillar", default="composite", choices=PILLARS)
    ap.add_argument("--since")
    a = ap.parse_args()
    t = tape(a.pillar, a.since)
    print("pillar " + t["pillar"] + "  ·  store as_of " + str(t["as_of_max"]))
    print(f"{'entity':16}{'peer_group':20}{'n':>5}{'scored':>8}  span")
    print("-" * 74)
    for eid in t["order"]:
        s = t["series"][eid]
        print(f"{eid:16}{str(s['peer_group']):20}{s['n']:>5}{s['n_scored']:>8}"
              f"  {s['first']} .. {s['last']}")
    print("\ncoverage by pillar:")
    for p, c in t["coverage"].items():
        print(f"   {p:11}{c['dates']:>6} dates   {c['first']} .. {c['last']}")
    print("\ncurves:")
    for p, c in t["curves"].items():
        kk = "—" if c["k"] is None else f"{c['k']:.4g}"
        print(f"   {p:11} spreadable={str(c['spreadable']):5} k={kk:>8} "
              f"sign={c['sign']:+d}  {c['unit']}")
    if t["corporate_actions"]:
        print("\nprice steps:")
        for e, ca in t["corporate_actions"].items():
            for c in ca["confirmed"]:
                print(f"   {e:16}BREAK      {c['date']}  {c['pct']:+.1f}%  {c['why']}")
            for c in ca["candidates"]:
                print(f"   {e:16}candidate  {c['date']}  {c['pct']:+.1f}%  "
                      f"(drawn through — real move until shown otherwise)")
            for d in ca["unmatched"]:
                print(f"   {e:16}UNMATCHED  {d}  confirmed action with no jump "
                      f"in the tape — CONFIRMED_ACTIONS may be stale")
