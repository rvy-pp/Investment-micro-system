"""Mood, read past broker bias — what they DO, not what they say.

THE PROBLEM, in the PM's words: some brokers hold a rating forever. If the stock
goes against them the rating stays and the target price quietly comes down, with
a reason attached. `mood.py` reads `rating_to` and never reads `tp_to` at all, so
a perma-BUY reiteration contributes +0.2 x decay every time it is published while
a 4% target cut under an unchanged rating contributes nothing.

This repo's own data shows it plainly:

    hindalco  Emkay  ADD -> ADD    TP 1200 -> 1150   rating unchanged, -4.2%
    nalco     Emkay  REDUCE->REDUCE TP 370 -> 340    rating unchanged, -8.1%
    vaml      Emkay  BUY -> BUY    TP 550 -> 550     x4, identical, no news
    vedanta   Emkay  BUY -> BUY    TP 350 -> 350     x3, identical, no news

Current mood counts the seven VAML/vedanta reiterations as seven positive events
and the two target cuts as nothing.

FOUR MEASURES, all deterministic so they can recompute daily.

1. IMPLIED UPSIDE, NOT TARGET LEVEL.  upside = TP/price - 1, and what matters is
   its CHANGE. This handles the perma-BUY case without special-casing it: a 10%
   target cut while the stock fell 15% means upside ROSE — the broker is more
   bullish relative to the market, not less. A target held flat through a rally is
   a de-facto downgrade and reads as one.

   It also means mood moves DAILY on price alone, with no publication. That is
   correct — a stale target against a rallying stock is information — but see the
   warning at the bottom.

2. ROLL-FORWARD IS FLAGGED, NOT DECOMPOSED.  A target price is a multiple on a
   base period: "TP Rs550 = 6.0x FY28E EV/EBITDA". Roll the base to FY29E and, if
   FY29 EBITDA sits 10% above FY28 in the same model, the target rises 10% at an
   unchanged multiple. No new view, no new estimate — the calendar moved.

   The informative decomposition is  dTP = d(multiple) + d(estimates) + roll,
   and only the first two carry anything. We cannot compute the roll term without
   the broker's model, so a target change across a base-period roll is FLAGGED
   and discounted rather than estimated. Inventing the split would be worse than
   discounting it.

   The inverse is the sharp case and is handled by the same flag: a roll with NO
   target increase is bearish, because the free lift was available and did not
   appear, which means estimates came down.

3. A BROKER'S RATING IS WEIGHTED BY HOW MUCH THEY VARY IT.  A constant carries no
   information. Rating entropy per broker, normalised, with MIN_OBS before anyone
   is penalised — n=2 all-SELL is not evidence of a perma-bear. Target moves keep
   full weight regardless, because that is the thing they do vary.

4. A REITERATION WITH NO CHANGE IS NOT AN EVENT.  Same defect P4 had, in another
   pillar: 14 of Emkay's 18 actions are reiterations, and each one currently opens
   the breadth gate wider. No rating change and no target change means nothing
   happened.

Usage:
    python packages/score/mood_bias.py --peer-group aluminium_primary
    python packages/score/mood_bias.py --broker-credibility
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

HALF_LIFE_DAYS = 30.0
MIN_OBS = 5            # below this, no broker is judged a perma-caller
ROLL_DISCOUNT = 0.25   # weight kept on a target change across a base-period roll
POSITIVE = {"BUY", "ADD", "ACCUMULATE", "OUTPERFORM", "OVERWEIGHT"}
NEGATIVE = {"SELL", "REDUCE", "UNDERPERFORM", "UNDERWEIGHT"}


def decay(age_days: float) -> float:
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def norm_rating(r: str | None) -> str | None:
    if not r:
        return None
    u = r.strip().upper()
    return "POS" if u in POSITIVE else "NEG" if u in NEGATIVE else "NEU"


def broker_credibility(conn) -> dict[str, dict]:
    """Normalised rating entropy per broker. 0 = always says the same thing.

    A broker whose rating never varies contributes a constant, and a constant is
    not a signal. Their TARGET moves are untouched by this weight.
    """
    rows = conn.execute("SELECT broker, rating_to FROM broker_actions").fetchall()
    by: dict[str, list[str]] = {}
    for b, r in rows:
        nr = norm_rating(r)
        if nr:
            by.setdefault(b, []).append(nr)
    out = {}
    for b, rs in by.items():
        n = len(rs)
        counts: dict[str, int] = {}
        for r in rs:
            counts[r] = counts.get(r, 0) + 1
        h = -sum((v / n) * math.log(v / n) for v in counts.values())
        hmax = math.log(3)                      # POS / NEU / NEG
        ent = h / hmax if hmax else 0.0
        # Too few observations to judge: assume informative rather than punish.
        weight = 1.0 if n < MIN_OBS else max(0.15, ent)
        out[b] = {"n": n, "mix": counts, "entropy": ent, "rating_weight": weight,
                  "judged": n >= MIN_OBS}
    return out


def price_on(conn, eid: str, d: str) -> float | None:
    r = conn.execute("SELECT close FROM prices WHERE entity_id=? AND date<=? "
                     "ORDER BY date DESC LIMIT 1", (eid, d)).fetchone()
    return r[0] if r else None


def events(conn, eid: str, as_of: str, cred: dict) -> list[dict]:
    rows = conn.execute(
        "SELECT id,action_date,broker,action,rating_from,rating_to,tp_from,tp_to,"
        "base_period,quote FROM broker_actions WHERE entity_id=? AND action_date<=? "
        "ORDER BY action_date", (eid, as_of)).fetchall()
    prev_base: dict[str, str] = {}
    out = []
    for (_i, d, br, act, rf, rt, tpf, tpt, base, q) in rows:
        age = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(d)).days
        dk = decay(age)
        rating_moved = norm_rating(rf) != norm_rating(rt) if rf else act in (
            "upgrade", "downgrade", "initiate")
        tp_moved = (tpf is not None and tpt is not None and tpf != tpt)

        # 4. nothing changed -> not an event
        dead = (act == "reiterate" and not rating_moved and not tp_moved)

        # 2. base-period roll
        rolled = bool(base and prev_base.get(br) and base != prev_base[br])
        if base:
            prev_base[br] = base

        # 1. change in implied upside, which is the honest signal
        d_upside = None
        if tp_moved:
            p_now = price_on(conn, eid, d)
            if p_now:
                # Approximate the prior anchor with the price at the previous
                # action by this broker; fall back to today's price, which
                # isolates the target move alone.
                pr = conn.execute(
                    "SELECT action_date FROM broker_actions WHERE entity_id=? "
                    "AND broker=? AND action_date<? ORDER BY action_date DESC "
                    "LIMIT 1", (eid, br, d)).fetchone()
                p_then = price_on(conn, eid, pr[0]) if pr else p_now
                d_upside = (tpt / p_now - 1.0) - (tpf / (p_then or p_now) - 1.0)

        rw = cred.get(br, {}).get("rating_weight", 1.0)
        out.append(dict(date=d, broker=br, action=act, rating_from=rf,
                        rating_to=rt, tp_from=tpf, tp_to=tpt, base=base,
                        rolled=rolled, dead=dead, rating_moved=rating_moved,
                        tp_moved=tp_moved, d_upside=d_upside, decay=dk,
                        rating_weight=rw, quote=q))
    return out


def contribution(e: dict) -> tuple[float, str]:
    """Signed, weighted contribution plus a one-word reason."""
    if e["dead"]:
        return 0.0, "no change"
    w = e["decay"]
    # A target move is the primary signal and needs no rating weight.
    if e["tp_moved"] and e["d_upside"] is not None:
        v = e["d_upside"] * 10.0            # ~10pp of upside == one clean unit
        if e["rolled"]:
            return v * w * ROLL_DISCOUNT, "tp move, ROLL-discounted"
        return v * w, "tp move"
    if e["rating_moved"]:
        # DIRECTION COMES FROM THE RATINGS, NOT THE `action` LABEL. Backfilling
        # rating_from exposed that the label is unreliable, which was invisible
        # while the from-side was empty:
        #   hindalco Emkay 2026-06-17  "downgrade"  ADD -> ADD   (nothing moved)
        #   hindzinc Kotak 2026-07-28  "reiterate"  SELL -> BUY  (a real upgrade)
        #   nalco    Kotak 2026-08-05  "upgrade"    BUY -> BUY   (nothing moved)
        # plus VAML carrying 3 Emkay and 2 ICICI "initiate" rows — coverage
        # cannot be initiated five times. Trusting the label would have scored
        # an upgrade on an unchanged rating and missed a real SELL->BUY.
        #
        # A rank on the rating itself is checkable; a label is somebody's
        # summary. Where they disagree, believe the ratings.
        RANK = {"NEG": -1, "NEU": 0, "POS": 1}
        a, b = RANK.get(norm_rating(e["rating_from"])), RANK.get(norm_rating(e["rating_to"]))
        if a is not None and b is not None:
            sign = 1 if b > a else -1 if b < a else 0
        else:
            nr = norm_rating(e["rating_to"])      # an initiation has no from-side
            sign = 1 if nr == "POS" else -1 if nr == "NEG" else 0
        if sign == 0:
            return 0.0, "label says move, ratings say no"
        return sign * w * e["rating_weight"], "rating move"
    return 0.0, "nothing to read"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer-group", default=None)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--broker-credibility", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    as_of = a.as_of or conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    cred = broker_credibility(conn)

    if a.broker_credibility:
        print(f"broker rating dispersion — a constant carries no information\n")
        print(f"{'broker':14}{'n':>4}{'mix':>26}{'entropy':>9}{'rating wt':>11}  note")
        for b, c in sorted(cred.items(), key=lambda x: -x[1]["n"]):
            mix = "/".join(f"{k}:{v}" for k, v in sorted(c["mix"].items()))
            note = "" if c["judged"] else f"n<{MIN_OBS}, not judged"
            print(f"{b:14}{c['n']:>4}{mix:>26}{c['entropy']:>9.3f}"
                  f"{c['rating_weight']:>11.2f}  {note}")
        return 0

    names = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM broker_actions ORDER BY entity_id")]
    print(f"mood, bias-adjusted — as of {as_of}\n")
    for eid in names:
        evs = events(conn, eid, as_of, cred)
        old = sum({"upgrade": 1.0, "downgrade": 1.0, "initiate": 0.7,
                   "tp_change": 0.5, "reiterate": 0.2}.get(e["action"], 0.2)
                  * e["decay"] * (1 if norm_rating(e["rating_to"]) == "POS"
                                  else -1 if norm_rating(e["rating_to"]) == "NEG" else 0)
                  for e in evs)
        new, dead, rolled = 0.0, 0, 0
        print(f"  {eid}")
        for e in evs:
            v, why = contribution(e)
            new += v
            dead += e["dead"]
            rolled += e["rolled"]
            if e["dead"]:
                continue
            tp = (f"{e['tp_from']:.0f}->{e['tp_to']:.0f}" if e["tp_moved"]
                  else f"{e['tp_to']:.0f}" if e["tp_to"] else "—")
            up = f"{e['d_upside']*100:+.1f}pp" if e["d_upside"] is not None else "—"
            print(f"    {e['date']}  {e['broker']:12}{e['action']:11}"
                  f"{str(e['rating_from'] or ''):>8}->{str(e['rating_to'] or ''):<12}"
                  f"TP {tp:>13}  dUpside {up:>8}  ={v:>+6.2f}  {why}")
        print(f"    {'':10}{dead} dead reiteration(s) dropped, {rolled} roll-flagged")
        print(f"    {'':10}OLD raw {old:>+6.2f}   ADJUSTED raw {new:>+6.2f}\n")

    print("""WHAT CHANGED AND WHY IT MATTERS

The old raw counts every published rating, so a broker who says BUY forever adds
to mood every time they repeat themselves. The adjusted raw reads target moves as
changes in implied upside, ignores reiterations that changed nothing, discounts
target moves that ride a base-period roll, and weights a rating by how much that
broker actually varies it.

A WARNING worth carrying: measure 1 makes mood move DAILY on price alone, because
implied upside changes when the stock moves and the target does not. That is
genuinely informative, but P1 already turned out to be a mirror of the price. If
mood becomes a second one, the composite is double-counting the tape. Test it the
same way: correlation with the move already past versus the move still ahead.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
