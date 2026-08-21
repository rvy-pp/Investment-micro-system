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
import mood_bias as mb  # noqa: E402

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
    """Bias-adjusted broker mood: what they DO, not what they say.

    REPLACED THE RATING-ONLY READ 2026-08-21, at the PM's direction. The old
    version read `rating_to` and never read `tp_to` at all, so a broker holding a
    rating forever contributed +0.2 x decay on every republication, while a
    target cut under an unchanged rating contributed nothing. This repo's own
    data is full of exactly that:

        hindalco  Emkay  ADD -> ADD      TP 1200 -> 1150   -7.1pp upside
        nalco     Emkay  REDUCE->REDUCE  TP  370 ->  340   -9.8pp upside
        vaml      Emkay  BUY -> BUY      TP  550 ->  550    x4, no news
        vedanta   Emkay  BUY -> BUY      TP  350 ->  350    x3, no news

    The old read scored hindalco +0.96 and nalco +0.80 on those. Both are
    negative once the target moves are read. See mood_bias.py for the full
    derivation; this is the same arithmetic, inlined so there is one mood.
    """
    rows = conn.execute(
        "SELECT action_date, broker, action, rating_from, rating_to, tp_from, "
        "tp_to, base_period, quote FROM broker_actions "
        "WHERE entity_id=? AND action_date<=? ORDER BY action_date", (eid, as_of)
    ).fetchall()
    cred = mb.broker_credibility(conn)
    prev_base: dict[str, str] = {}
    raw, brokers, events = 0.0, set(), []
    for d, broker, action, rf, rt, tpf, tpt, base, quote in rows:
        age = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(d)).days
        dk = decay(age)
        nrf, nrt = mb.norm_rating(rf), mb.norm_rating(rt)
        rating_moved = (nrf != nrt) if rf else action in (
            "upgrade", "downgrade", "initiate")
        tp_moved = (tpf is not None and tpt is not None and tpf != tpt)

        # 4. A reiteration that changed nothing is not an event, and must not
        #    open the breadth gate either — so `continue` before touching
        #    `brokers`.
        if action == "reiterate" and not rating_moved and not tp_moved:
            continue

        rolled = bool(base and prev_base.get(broker) and base != prev_base[broker])
        if base:
            prev_base[broker] = base

        sign_w = 0.0
        why = ""
        if tp_moved:
            # 1. The change in IMPLIED UPSIDE, not the target level. A cut while
            #    the stock fell further is an upside INCREASE.
            p_now = mb.price_on(conn, eid, d)
            pr = conn.execute(
                "SELECT action_date FROM broker_actions WHERE entity_id=? AND "
                "broker=? AND action_date<? ORDER BY action_date DESC LIMIT 1",
                (eid, broker, d)).fetchone()
            p_then = mb.price_on(conn, eid, pr[0]) if pr else p_now
            if p_now and p_then:
                du = (tpt / p_now - 1.0) - (tpf / p_then - 1.0)
                sign_w = du * 10.0 * dk      # ~10pp of upside == one clean unit
                why = "tp"
                # 2. A target change riding a base-period roll is calendar, not
                #    view. Flagged and discounted; NOT decomposed, because the
                #    roll term needs the broker's own model.
                if rolled:
                    sign_w *= mb.ROLL_DISCOUNT
                    why = "tp/roll"
        elif rating_moved:
            # Direction from the RATING RANK, never the `action` label. The
            # label is unreliable and backfilling rating_from proved it:
            # "downgrade" ADD->ADD, "reiterate" SELL->BUY, "upgrade" BUY->BUY,
            # and five separate "initiate" rows on VAML.
            RANK = {"NEG": -1, "NEU": 0, "POS": 1}
            a, b = RANK.get(nrf), RANK.get(nrt)
            sign = (1 if b > a else -1 if b < a else 0) if (
                a is not None and b is not None) else (
                1 if nrt == "POS" else -1 if nrt == "NEG" else 0)
            if sign == 0:
                continue
            # 3. Weight the RATING by how much this broker varies it. A constant
            #    is not a signal. Target moves are untouched by this.
            sign_w = sign * dk * cred.get(broker, {}).get("rating_weight", 1.0)
            why = "rating"
        if sign_w == 0.0:
            continue
        raw += sign_w
        brokers.add(broker)
        events.append((d, broker, action, rt, 1 if sign_w > 0 else -1,
                       abs(sign_w), f"[{why}] {quote}"))
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
