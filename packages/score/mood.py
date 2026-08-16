"""P3 mood — broker posture and policy, damped by how much conviction is behind it.

The PM's framing, which the design follows literally: mood can be stale, but
INTENSITY tells you whether it will persist and whether it will bite. So mood is
not one number — it is a direction and a strength, and the strength decides how
far the direction is allowed to move the score.

TWO CHANNELS, because a stock's mood is not only about the stock:

  COMPANY   broker upgrades, downgrades, initiations, target-price moves
  SECTOR    policy and macro that lifts or sinks the whole group — a duty, a
            levy repealed, a supply disruption. A name with no broker action at
            all still has mood if its sector policy turned favourable.

INTENSITY IS BREADTH, NOT VOLUME. Four notes from one house is one opinion
repeated; one note each from four houses is the street moving. The gate weights
distinct brokers far above raw event count for exactly that reason.

DECAY, NOT A WINDOW. Mood fades; it does not fall off a cliff on day 31. Same
reasoning as the EWMA fix in the bridge — a window would make the score jump
when an old upgrade aged out, which is movement with no new information.
Mood's half-life is deliberately LONGER than price (30d vs 10d): a rating change
keeps colouring how a name trades for months.

WHY THE GATE MATTERS. Without it, one stale note from one broker swings the
score as hard as a co-ordinated street-wide upgrade. That is precisely the
failure the PM described — mood that looks decisive when nothing is behind it.

Usage:
    python packages/score/mood.py --peer-group aluminium_primary
    python packages/score/mood.py --peer-group aluminium_primary --detail
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bridge import load_specs  # noqa: E402
from scoring import score as to_score, solve_k  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

HALF_LIFE_DAYS = 30.0        # mood outlives price moves
MOOD_ANCHOR, SCORE_ANCHOR, P = 2.0, 4.0, 1.5   # "two clean upgrades reads a 4"

ACTION_WEIGHT = {"upgrade": 1.0, "downgrade": 1.0, "initiate": 0.7,
                 "tp_change": 0.5, "reiterate": 0.2}
POSITIVE = {"BUY", "ADD", "ACCUMULATE", "OUTPERFORM", "OVERWEIGHT"}
NEGATIVE = {"SELL", "REDUCE", "UNDERPERFORM", "UNDERWEIGHT"}
POLICY_WEIGHT = 0.8


def decay(age_days: float) -> float:
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def direction_of(action: str, rating: str | None) -> int:
    if action == "upgrade":
        return 1
    if action == "downgrade":
        return -1
    r = (rating or "").upper()
    return 1 if r in POSITIVE else -1 if r in NEGATIVE else 0


def gate(n_brokers: int, n_events: int) -> float:
    """How far mood may move from neutral. Breadth counts ~3x an extra event."""
    return 1.0 - math.exp(-(0.40 * n_brokers + 0.15 * n_events))


def company_mood(conn, eid: str, as_of: str):
    rows = conn.execute(
        "SELECT action_date, broker, action, rating_to, quote FROM broker_actions "
        "WHERE entity_id=? AND action_date<=? ORDER BY action_date", (eid, as_of)
    ).fetchall()
    raw, brokers, events = 0.0, set(), []
    for d, broker, action, rating, quote in rows:
        age = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(d)).days
        w = ACTION_WEIGHT.get(action, 0.2) * decay(age)
        sign = direction_of(action, rating)
        if sign == 0:
            continue
        raw += sign * w
        brokers.add(broker)
        events.append((d, broker, action, rating, sign, w, quote))
    return raw, brokers, events


def policy_mood(conn, eid: str, sector: str, as_of: str):
    """Policy / macro, at SECTOR level and at COMPANY level.

    Both matter and they are genuinely different: a repealed state levy lifts
    everyone in the sector, while a competitor's refinery going down lifts one
    name specifically. Reading only the sector would miss the Alunorte case,
    where a global supply shock was a NALCO story rather than an aluminium one.
    """
    rows = conn.execute(
        "SELECT as_of, direction, value_num, quote, entity_id FROM observations "
        "WHERE entity_id IN (?, ?) AND factor='policy' AND as_of<=? "
        "ORDER BY as_of", (f"sector_{sector}", eid, as_of)).fetchall()
    raw, events = 0.0, []
    for d, direction, mag, quote, who in rows:
        age = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(d)).days
        w = POLICY_WEIGHT * (mag or 1.0) * decay(age)
        raw += (direction or 0) * w
        scope = "sector" if who.startswith("sector_") else "company"
        events.append((d, direction, w, quote, scope))
    return raw, events


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer-group", default="aluminium_primary")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()

    entities, _u, _f = load_specs()
    conn = sqlite3.connect(DB)
    as_of = a.as_of or conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    k = solve_k("hill", MOOD_ANCHOR, SCORE_ANCHOR, P)

    print(f"{a.peer_group} · mood as of {as_of} · half-life {HALF_LIFE_DAYS:g}d\n")
    print(f"{'entity':16}{'broker':>8}{'policy':>8}{'raw':>8}{'brokers':>8}"
          f"{'events':>7}{'gate':>6}{'eff':>7}{'MOOD':>7}")
    print("-" * 82)

    for ent in sorted(entities.values(), key=lambda e: e["id"]):
        if ent.get("peer_group") != a.peer_group:
            continue
        eid = ent["id"]
        c_raw, brokers, events = company_mood(conn, eid, as_of)
        s_raw, pol = policy_mood(conn, eid, ent.get("sector") or "", as_of)
        raw = c_raw + s_raw
        g = gate(len(brokers), len(events) + len(pol))
        eff = raw * g
        score = to_score(eff, k, "hill", P)
        print(f"{eid:16}{c_raw:>+8.2f}{s_raw:>+8.2f}{raw:>+8.2f}"
              f"{len(brokers):>8}{len(events) + len(pol):>7}{g:>6.2f}"
              f"{eff:>+7.2f}{score:>7.2f}")

        if a.detail:
            for d, broker, action, rating, sign, w, quote in events[-4:]:
                print(f"{'':4}{d}  {broker:10} {action:10} {str(rating):8} "
                      f"{sign:+d} w={w:.2f}")
                print(f"{'':6}\"{quote[:96]}\"")
            for d, direction, w, quote, scope in pol[-3:]:
                print(f"{'':4}{d}  {'POLICY-'+scope:17} {'':1} {direction:+d} w={w:.2f}")
                print(f"{'':6}\"{quote[:96]}\"")

    print(f"\ngate = 1 - exp(-(0.40*brokers + 0.15*events)); breadth counts ~3x an")
    print(f"extra note from the same house. Anchor: effective mood {MOOD_ANCHOR:g}"
          f" reads {SCORE_ANCHOR:.1f}.")
    print("Decay, not a window — an old upgrade fades rather than dropping out.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
