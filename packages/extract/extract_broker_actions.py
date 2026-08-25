"""Deterministically extract broker rating / target-price actions into the store.

The pattern here is regular enough that code can do it: a named broker, a
rating word, and often a target price, in one sentence. Every row keeps its
verbatim quote, so a wrong parse is visible rather than buried — which is what
makes deterministic extraction acceptable for this and not for policy, where
direction genuinely needs judgment.

ACTION AND DIRECTION ARE DIFFERENT THINGS, and conflating them was the trap:
  UPGRADE  to Neutral   -> action improves, rating still not positive
  REITERATE Underperform -> no new information, but the standing view is negative
So `action` records what the broker DID and `direction` records which way it
points. A reiterate carries the sign of its rating at low weight; an upgrade
carries a strong positive regardless of the level it moved to.

Usage:
    python packages/extract/extract_broker_actions.py --dry
    python packages/extract/extract_broker_actions.py --load
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from broker_candidates import BROKERS, RATING, TP, scan  # noqa: E402

POSITIVE = {"BUY", "ADD", "ACCUMULATE", "OUTPERFORM", "OVERWEIGHT"}
NEGATIVE = {"SELL", "REDUCE", "UNDERPERFORM", "UNDERWEIGHT"}

# Ordered: the first match wins, so UPGRADE beats the MAINTAIN that often
# appears later in the same sentence.
# Case-INSENSITIVE. An earlier version anchored on upper-case forms, so
# "initiates BUY" fell through to the reiterate default and 49 of 58 actions
# were mislabelled as reiterations.
ACTION_PATTERNS = [
    ("upgrade",   r"upgrade[sd]?\b"),
    ("downgrade", r"downgrade[sd]?\b|\bcuts? to\b|\brevises? to (?:reduce|sell)\b"),
    ("initiate",  r"\binitiat(?:e|es|ed|ing|ion)\b"),
    ("tp_change", r"\b(?:TP|FV|PO)\s*(?:raised|cut|lowered|hiked|trimmed)\b|"
                  r"\b(?:raised|cut|lowered) (?:TP|FV|PO)\b"),
    ("reiterate", r"reiterat\w*\b|\bmaintain\w*\b|\bretain\w*\b|\bkeeps?\b|"
                  r"\bstays?\b|\bheld\b"),
]

# Sentence-level company detection. Bullet-level tags are correct for guidance,
# where the numbers land several sentences after the tag — but they OVER-ATTRIBUTE
# here: a bullet tagged "#Hindalco #Nalco" gave "NALCO REDUCE TP Rs370" to
# Hindalco as well. When a sentence names its own company, that wins; bullet tags
# are only the fallback.
#
# Order matters: VAML is checked before Vedanta, or "Vedanta Aluminium" would
# match the parent.
NAME_PATTERNS = [
    ("vaml",           r"\bVAML\b|\bVedanta Alumini?um\b"),
    ("hindustan_zinc", r"\bHindustan Zinc\b|\bHZL\b|#HZ\b"),
    ("vedanta",        r"\bVedanta Ltd\b|\bVEDL\b|\bVedanta\b"),
    ("hindalco",       r"\bHindalco\b"),
    ("nalco",          r"\bNALCO\b|\bNational Alumini?um\b"),
    # --- steel, added 2026-08-25 ---
    # Every shorthand here was CONFIRMED IN CONTEXT in the 48 digests before
    # being written, not inferred from the ticker. TSL -> Tata Steel ("TSL India
    # +Rs2,500/t EBITDA/t expansion"), JSL -> Jindal Stainless ("BUY: JSL >
    # Shyam"), JSPL -> Jindal Steel (the pre-rename Jindal Steel & Power, still
    # the shorthand brokers use), JSTL -> JSW Steel.
    #
    # THERE IS DELIBERATELY NO BARE `\bJSW\b` PATTERN, and it costs real
    # attributions — "SELL: JSW > Tata Steel > JSPL > NMDC > Hindalco" names JSW
    # Steel and will not be picked up. JSW CEMENT is also in the vault's coverage
    # and appears ~29 times in these same digests, so a bare JSW would hand
    # cement calls to the steel entity. The `targets` intersection below does NOT
    # protect against this: on a #JSWCement bullet `jsw_steel` is not in `tags`,
    # so the intersection is empty and the `or named` fallback keeps the wrong
    # entity anyway. A miss is the correct trade here.
    #
    # NO BARE `\bTata\b` EITHER — #TataElxsi is in the IT coverage and in these
    # same digests.
    ("tata_steel",       r"\bTata Steel\b|\bTSL\b|#TataSteel\b"),
    ("jsw_steel",        r"\bJSW Steel\b|\bJSTL\b|#JSWSteel\b"),
    ("jindal_stainless", r"\bJindal Stainless\b|\bJSL\b|#JindalStainless\b"),
    ("jindal_steel",     r"\bJindal Steel\b|\bJSPL\b|#JindalSteel\b"),
    ("sail",             r"\bSAIL\b|#SAIL\b"),
    ("shyam_metalics",   r"\bShyam\b|#ShyamMetalics\b"),
    ("apl_apollo",       r"\bAPL ?Apollo\b|#APLApollo\b"),
]


def named_in(sent: str) -> list[str]:
    """Companies explicitly named in this sentence, most specific first."""
    found: list[str] = []
    for eid, pat in NAME_PATTERNS:
        if re.search(pat, sent, re.I):
            # 'Vedanta' inside 'Vedanta Aluminium' must not also claim the parent
            if eid == "vedanta" and "vaml" in found:
                continue
            found.append(eid)
    return found

# Direction of a target-price move stated in words.
TP_UP = re.compile(r"\b(?:raised|raises|hiked|up(?:graded)? to|increased)\b", re.I)
TP_DOWN = re.compile(r"\b(?:cut|cuts|lowered|reduced|trimmed)\b", re.I)


def classify(sent: str, rating: str | None) -> tuple[str, int, float]:
    """(action, direction, weight)."""
    action = "reiterate"
    for name, pat in ACTION_PATTERNS:
        if re.search(pat, sent, re.I):
            action = name
            break

    pol = 1 if rating in POSITIVE else -1 if rating in NEGATIVE else 0

    if action == "upgrade":
        return action, +1, 1.0
    if action == "downgrade":
        return action, -1, 1.0
    if action == "initiate":
        return action, pol, 0.7
    if action == "tp_change":
        if TP_UP.search(sent):
            return action, +1, 0.5
        if TP_DOWN.search(sent):
            return action, -1, 0.5
        return action, pol, 0.3
    # reiterate / maintain: no new information, but the standing view has a sign
    return "reiterate", pol, 0.2


def collect(entity: str | None = None):
    rows = []
    for date, tags, brokers, sent, tp, rating in scan(entity):
        if not brokers:
            continue          # broker_actions.broker is NOT NULL, and an
                              # unattributable call cannot be deduplicated
        action, direction, weight = classify(sent, rating)
        if direction == 0 and action == "reiterate":
            continue          # a reiterated HOLD says nothing
        # Sentence-level naming wins. Falling back to bullet tags is only safe
        # when the bullet has ONE tag — otherwise "Top SELL: TATA, SAIL." inside
        # a bullet tagged #Hindalco #Nalco gets recorded as a Hindalco sell.
        # A multi-tag bullet whose sentence names none of them is genuinely
        # ambiguous, so it is dropped rather than guessed.
        named = named_in(sent)
        targets = [e for e in named if e in tags] or named
        if not targets:
            targets = tags if len(tags) == 1 else []
        for eid in targets:
            rows.append({
                "entity_id": eid, "broker": brokers[0], "action_date": date,
                "action": action, "rating_to": rating,
                "tp_to": float(tp.replace(",", "")) if tp else None,
                "direction": direction, "weight": weight,
                "quote": re.sub(r"\s+", " ", sent)[:400],
            })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--entity")
    a = ap.parse_args()

    rows = collect(a.entity)
    print(f"{len(rows)} attributable broker actions\n")
    by_action: dict[str, int] = {}
    by_entity: dict[str, list[int]] = {}
    for r in rows:
        by_action[r["action"]] = by_action.get(r["action"], 0) + 1
        by_entity.setdefault(r["entity_id"], []).append(r["direction"])
    print("  by action:  " + "  ".join(f"{k}={v}" for k, v in sorted(by_action.items())))
    print("\n  by entity:")
    for eid, dirs in sorted(by_entity.items()):
        print(f"    {eid:16} {len(dirs):>3} actions   "
              f"+{sum(1 for d in dirs if d > 0)} / -{sum(1 for d in dirs if d < 0)}")

    if a.dry:
        print("\n  sample:")
        for r in rows[:6]:
            print(f"    {r['action_date']}  {r['entity_id']:14} {r['broker']:10} "
                  f"{r['action']:10} {str(r['rating_to']):12} tp={r['tp_to']}")
            print(f"      \"{r['quote'][:110]}\"")
        return 0

    if a.load:
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA foreign_keys = ON")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("DELETE FROM broker_actions WHERE source_id='digest-auto'")
        conn.execute(
            "INSERT OR IGNORE INTO sources (id,kind,origin,title,source_date,"
            "captured_at,raw_path) VALUES "
            "('digest-auto','broker_note','digest','Auto-extracted broker actions',"
            "?,?,'Broker Mails/')", (rows[-1]["action_date"] if rows else "2026-08-14", now))
        n = 0
        for r in rows:
            conn.execute(
                "INSERT INTO broker_actions (source_id,entity_id,broker,action_date,"
                "action,rating_to,tp_to,currency,quote,created_at) "
                "VALUES ('digest-auto',?,?,?,?,?,?,'INR',?,?)",
                (r["entity_id"], r["broker"], r["action_date"], r["action"],
                 r["rating_to"], r["tp_to"], r["quote"], now))
            n += 1
        conn.commit()
        conn.close()
        print(f"\nloaded {n} broker actions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
