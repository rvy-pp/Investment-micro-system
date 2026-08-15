"""Find broker rating / target-price actions for P3.

P3 asks "is it already in the price". Two inputs available from the digests:
  CONSENSUS TP GAP   upside from spot to the mean broker target
  RATING DISPERSION  how split the street is

A target price is only usable if the BROKER IS NAMED — an anonymous "TP Rs722"
cannot be deduplicated, so restating the same call three times would look like
three independent opinions and inflate a consensus. broker_actions.broker is
NOT NULL for exactly this reason.

Usage:
    python packages/extract/broker_candidates.py --entity nalco --show
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from candidates import VAULT, iso_date, sentences  # noqa: E402
from guidance_candidates import TAGS  # noqa: E402

# TP / fair value / price objective, with a number
TP = re.compile(
    r"\b(?:TP|PO|FV|target price|fair value|price objective)\b[\s:]*"
    r"(?:Rs\.?|INR)?\s?([\d,]+(?:\.\d+)?)", re.I)

RATING = re.compile(
    r"\b(BUY|SELL|HOLD|ADD|REDUCE|NEUTRAL|OUTPERFORM|UNDERPERFORM|"
    r"OVERWEIGHT|UNDERWEIGHT|EQUAL[- ]WEIGHT|ACCUMULATE)\b", re.I)

# Broker names seen in these digests. A named list beats a generic
# capitalised-word heuristic, which picks up analyst surnames and plant names.
BROKERS = [
    "Ambit", "Emkay", "Kotak", "KIE", "Jefferies", "JPM", "JP Morgan",
    "Morgan Stanley", "BofA", "Bank of America", "CLSA", "Nomura", "UBS",
    "Citi", "Goldman", "GS", "Macquarie", "HSBC", "Avendus Spark", "Avendus",
    "PhillipCapital", "Investec", "Motilal", "MOSL", "ICICI", "Axis",
    "Antique", "Elara", "IIFL", "Systematix", "Nuvama", "Incred", "B&K",
]


def scan(entity: str | None):
    out = []
    for path in sorted(VAULT.glob("*.md"), key=lambda p: iso_date(p.name) if re.match(r"\d{2}-\d{2}-\d{4}\.md", p.name) else ""):
        if not re.match(r"\d{2}-\d{2}-\d{4}\.md", path.name):
            continue
        date = iso_date(path.name)
        for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
            if not line.strip():
                continue
            tags = sorted({TAGS[t] for t in TAGS if t in line})
            if not tags or (entity and entity not in tags):
                continue
            # broker attribution is line-level, like the tags
            brokers = [b for b in BROKERS if re.search(rf"\b{re.escape(b)}\b", line)]
            for sent in sentences(line):
                tp = TP.search(sent)
                rating = RATING.search(sent)
                if not (tp or rating):
                    continue
                out.append((date, tags, brokers, sent,
                            tp.group(1) if tp else None,
                            rating.group(1).upper() if rating else None))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()

    rows = scan(a.entity)
    named = [r for r in rows if r[2]]
    print(f"{len(rows)} rating/TP sentences, {len(named)} with a named broker"
          f"{' for ' + a.entity if a.entity else ''}")
    if a.show:
        print()
        for d, tags, brokers, s, tp, rating in rows[:a.limit]:
            s = re.sub(r"\s+", " ", s)
            b = ",".join(brokers) if brokers else "UNATTRIBUTED"
            print(f"{d}  [{','.join(tags)}]  broker={b}  tp={tp}  rating={rating}")
            print(f"    {s[:190]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
