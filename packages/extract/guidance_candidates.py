"""Find management commitments and their supporting/undermining evidence.

P4 asks "will management hit what they said". That needs two kinds of sentence:

  COMMITMENT  a dated, numeric target with a period
              "Guidance reiterated: 1.1mt refined metal, 680t silver,
               zinc cost $975-1,000/t"

  EVIDENCE    a later datapoint that supports or undermines an open commitment
              "Zinc COP ex-royalty -6% qoq to $851/t"

Both must be quoted verbatim — guidance.quote and guidance_evidence.quote are
NOT NULL in the schema, so an uncited commitment cannot be stored.

Usage:
    python packages/extract/guidance_candidates.py --entity HZ --show
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from candidates import VAULT, iso_date, sentences  # noqa: E402

COMMITMENT = [
    r"\bguidance\b", r"\bguides?\b", r"\bguided\b", r"\breiterat",
    r"\btargets?\b", r"\bexpects? to\b", r"\bon track\b", r"\bmaintains? (?:its )?(?:FY|target)",
    r"\bFY\d\d\w* (?:target|guide)", r"\baims? (?:for|to)\b",
]

EVIDENCE = [
    r"\bahead of\b", r"\bbehind\b", r"\bmiss(?:ed|es)?\b", r"\bbeat\b",
    r"\bin line\b", r"\bdelay", r"\bramp(?:-| )?up\b", r"\bcommission",
    r"\bran? at\b", r"\bachiev", r"\bshortfall\b", r"\bslippage\b",
    r"\bCOP\b", r"\bcost of production\b", r"\butilisation\b", r"\butilization\b",
]

# Tag -> entity_id. The digests tag companies with #Hashtags.
TAGS = {
    "#HZ": "hindustan_zinc", "#HindustanZinc": "hindustan_zinc",
    "#Nalco": "nalco", "#NALCO": "nalco",
    "#Hindalco": "hindalco",
    "#VAML": "vaml",
    "#Vedanta": "vedanta",
    # --- steel, added 2026-08-25 ---
    # Taken from a frequency count of every hashtag in the 48 digests, not
    # guessed: #TataSteel 73, #JSWSteel 72, #JindalSteel 64, #SAIL 33,
    # #APLApollo 23, #ShyamMetalics 15, #JindalStainless 9.
    #
    # DO NOT ADD THE SECTOR TAGS (#Steel 42, #Mining 25, #Aluminium 36).
    # Matching is `t in line` substring containment, so "#Steel" would match
    # inside "#TataSteel", "#JSWSteel" and "#JindalSteel" and silently tag
    # three companies' bullets with a fourth entity. The company tags are safe
    # under containment in both directions — in particular "#JindalSteel" does
    # not appear inside "#JindalStainless".
    "#TataSteel": "tata_steel",
    "#JSWSteel": "jsw_steel",
    "#JindalSteel": "jindal_steel",
    "#SAIL": "sail",
    "#JindalStainless": "jindal_stainless",
    "#ShyamMetalics": "shyam_metalics",
    "#APLApollo": "apl_apollo",
    # NOT MAPPED, deliberately, pending a scope decision: #NMDC 37,
    # #CoalIndia 30, #LloydsMetals 9. All three are steel INPUT suppliers and
    # appear constantly in the steel bullets — NMDC sets the domestic iron ore
    # price the mills pay. They are in the vault's Mining coverage, not Steel,
    # and mapping a tag to an entity_id that does not exist in `entities` would
    # produce extraction candidates that cannot be loaded. Add the entity first
    # if the peer group is widened to include the ore leg.
}

NUM = r"\d"


def scan(kind: str, entity: str | None):
    """Tag attribution is BULLET-level, not sentence-level.

    In these digests each bullet is one long line beginning with its #Tags, and
    the substantive numbers land several sentences later. Matching tags per
    sentence found 1 HZL commitment out of a file that plainly contains several
    — the tag simply is not in the same sentence as the guidance. Sentences
    inherit the tags of the bullet they belong to.
    """
    pats = COMMITMENT if kind == "commitment" else EVIDENCE
    out = []
    for path in sorted(VAULT.glob("*.md"), key=lambda p: iso_date(p.name) if re.match(r"\d{2}-\d{2}-\d{4}\.md", p.name) else ""):
        if not re.match(r"\d{2}-\d{2}-\d{4}\.md", path.name):
            continue
        date = iso_date(path.name)
        for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
            if not line.strip() or line.startswith("#") and len(line) < 40:
                continue                       # section header, not a bullet
            tags = sorted({TAGS[t] for t in TAGS if t in line})
            if not tags or (entity and entity not in tags):
                continue
            for sent in sentences(line):
                if not re.search(NUM, sent):
                    continue
                if not any(re.search(p, sent, re.I) for p in pats):
                    continue
                out.append((date, tags, sent))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity")
    ap.add_argument("--kind", choices=["commitment", "evidence"], default="commitment")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args()

    rows = scan(a.kind, a.entity)
    print(f"{a.kind}: {len(rows)} candidate sentences"
          f"{' for ' + a.entity if a.entity else ''}\n")
    if a.show:
        for d, tags, s in rows[:a.limit]:
            s = re.sub(r"\s+", " ", s)
            print(f"{d}  [{','.join(tags)}]")
            print(f"    {s[:260]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
