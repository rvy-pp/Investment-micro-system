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

# CANONICAL HOUSE -> the patterns that name it, longest form first so
# "Bank of America" is not shadowed by "BofA" and "JP Morgan" not by "JPM".
#
# !! WHY THIS EXISTS: BROKER ATTRIBUTION WAS BULLET-LEVEL AND RESOLVED BY LIST
# !! POSITION, WHICH MISATTRIBUTED EVERY MULTI-HOUSE ROUND-UP.
# !!
# !! `scan()` matched houses against the whole BULLET and `collect()` took
# !! brokers[0]. Because the comprehension iterates BROKERS, brokers[0] is the
# !! house earliest in THE LIST, not the one the sentence actually names. "Ambit"
# !! sat at index 0, "Kotak" at 2, "JPM" at 5 — so they won everything.
# !!
# !! Measured on the store before the fix (2026-08-26):
# !!
# !!   jindal_steel / "Ambit" / 2026-07-28 — four rows, quotes naming IIFL,
# !!       Nomura, ICICI Securities and Ambit. All four recorded as Ambit.
# !!   jsw_steel / "Kotak" / 2026-07-21 — quotes naming Nomura, Elara, Avendus
# !!       Spark and MS. All recorded as Kotak.
# !!   sail / "JPM" / 2026-07-29 — quotes naming Elara and BofA. Recorded as JPM.
# !!
# !! THE DAMAGE WAS NOT MAINLY DUPLICATION, WHICH IS WHAT IT LOOKED LIKE. Four
# !! houses collapsing into one wrong house destroys BREADTH, and mood.py's gate
# !! weights breadth ~3x an extra note from the same house
# !! (1 - exp(-(0.40*brokers + 0.15*events))). So the notes carrying the MOST
# !! independent opinion — sector round-ups — were the ones whose breadth was
# !! most understated, and the recorded house was usually wrong too.
# !!
# !! The entity side of this was already fixed: extract_broker_actions.named_in()
# !! resolves the COMPANY per sentence and falls back to bullet tags. The same
# !! idea was never applied to brokers.
BROKER_PATTERNS = [
    ("Morgan Stanley", r"Morgan Stanley|\bMS\b"),
    ("JPMorgan",       r"JP ?Morgan|\bJPM\b"),
    ("BofA",           r"Bank of America|\bBofA\b"),
    ("Goldman",        r"Goldman(?: Sachs)?|\bGS\b"),
    ("Avendus Spark",  r"Avendus(?: Spark)?"),
    ("ICICI",          r"ICICI(?: Securities| Direct)?"),
    ("Motilal Oswal",  r"Motilal(?: Oswal)?|\bMOSL\b"),
    ("Kotak",          r"\bKotak\b|\bKIE\b"),
    ("Ambit",          r"\bAmbit\b"),
    ("Emkay",          r"\bEmkay\b"),
    ("Jefferies",      r"\bJefferies\b"),
    ("CLSA",           r"\bCLSA\b"),
    ("Nomura",         r"\bNomura\b"),
    ("UBS",            r"\bUBS\b"),
    ("Citi",           r"\bCiti\w*\b"),
    ("Macquarie",      r"\bMacquarie\b"),
    ("HSBC",           r"\bHSBC\b"),
    ("PhillipCapital", r"\bPhillip ?Cap\w*\b"),
    ("Investec",       r"\bInvestec\b"),
    ("Axis",           r"\bAxis\b"),
    ("Antique",        r"\bAntique\b"),
    ("Elara",          r"\bElara\b"),
    ("IIFL",           r"\bIIFL\b"),
    ("Systematix",     r"\bSystematix\b"),
    ("Nuvama",         r"\bNuvama\b"),
    ("Incred",         r"\bIn ?Cred\b"),
    ("B&K",            r"B&K"),
]


def brokers_in(text: str) -> list[str]:
    """Canonical houses named in `text`, in the order they APPEAR IN THE TEXT.

    Text order, not list order — the whole point. Returns each house once.
    """
    hits = []
    for canon, pat in BROKER_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            hits.append((m.start(), canon))
    hits.sort()
    out = []
    for _pos, canon in hits:
        if canon not in out:
            out.append(canon)
    return out


# Kept for the probe output and for callers that want the flat list.
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
            # Bullet-level houses are now only the FALLBACK. The sentence's own
            # house wins, resolved in text order — see the note on
            # BROKER_PATTERNS for what list-order attribution was doing.
            bullet_brokers = brokers_in(line)
            for sent in sentences(line):
                tp = TP.search(sent)
                rating = RATING.search(sent)
                if not (tp or rating):
                    continue
                sent_brokers = brokers_in(sent)
                out.append((date, tags, sent_brokers or bullet_brokers, sent,
                            tp.group(1) if tp else None,
                            rating.group(1).upper() if rating else None,
                            bool(sent_brokers)))
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
