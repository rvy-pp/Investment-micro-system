"""Deterministic pre-filter: find sentences in the digests that could carry a
price level for a research-sourced series.

WHY A REGEX PASS BEFORE THE MODEL. The model's job is extraction, but sending
43 full digests to it is expensive and most of each file is irrelevant. Code
narrows to candidate sentences; the model reads only those and produces cited
observations. Cheap, and the narrowing step is auditable.

This also answers a question worth settling BEFORE building the pipeline: how
much of each series is actually present? A series mentioned twice in two months
is not a series, and no amount of extraction will make it one.

Usage:
    python packages/extract/candidates.py
    python packages/extract/candidates.py --series alumina_index --show
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

VAULT = pathlib.Path(
    r"C:\Users\rajvaibhav.yadav\OneDrive - PinPOINT\Obsidian Vault\Broker Mails"
)

# A candidate needs BOTH a series cue and a number nearby. "alumina refinery
# capacity" is not a price; "alumina spot $370/t" is.
NUM = r"(?:US\$|\$|USD|Rs\.?|INR)?\s?[\d,]+(?:\.\d+)?\s?(?:/t|/oz|%|bn|mn)?"

SERIES = {
    "lme_aluminium":  [r"LME alumini?um", r"\balumini?um\b.{0,30}(?:USD|US\$|\$)"],
    "alumina_index":  [r"\balumina\b.{0,60}(?:spot|price|index|realisation|FOB)",
                       r"(?:spot|price|index).{0,40}\balumina\b"],
    "lme_zinc":       [r"LME zinc", r"\bzinc\b.{0,40}(?:USD|US\$|\$|/t)"],
    "silver":         [r"\bsilver\b.{0,40}(?:USD|US\$|\$|/oz|price)"],
    "thermal_coal_eauction": [r"e-?auction.{0,60}(?:premium|realiz|realis|price|%)",
                              r"(?:premium|realiz|realis).{0,40}e-?auction"],
    "cp_coke":        [r"(?:CP|calcined|pet(?:roleum)?)\s?coke"],
    "can_sheet_spread": [r"can\s?sheet", r"conversion spread", r"Novelis.{0,40}spread"],
    "al_scrap_midwest": [r"scrap.{0,40}(?:spread|discount|price)",
                         r"(?:spread|discount).{0,30}scrap"],
}


def iso_date(name: str) -> str:
    d, m, y = name[:10].split("-")
    return f"{y}-{m}-{d}"


def sentences(text: str) -> list[str]:
    # digests are one long line per bullet; split on sentence enders and bullets
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z#*])|\n", text)
    return [p.strip() for p in parts if p.strip()]


def scan() -> dict[str, list[tuple[str, str]]]:
    found: dict[str, list[tuple[str, str]]] = {k: [] for k in SERIES}
    for path in sorted(VAULT.glob("*.md")):
        if not re.match(r"\d{2}-\d{2}-\d{4}\.md", path.name):
            continue
        date = iso_date(path.name)
        for sent in sentences(path.read_text(encoding="utf-8", errors="replace")):
            if not re.search(NUM, sent):
                continue
            for series, pats in SERIES.items():
                if any(re.search(p, sent, re.I) for p in pats):
                    found[series].append((date, sent))
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    if not VAULT.exists():
        print(f"digest folder not found: {VAULT}", file=sys.stderr)
        return 1

    found = scan()
    n_files = len([p for p in VAULT.glob("*.md")
                   if re.match(r"\d{2}-\d{2}-\d{4}\.md", p.name)])

    print(f"scanned {n_files} digests\n")
    print(f"{'series':24} {'hits':>5} {'days':>5} {'first':>12} {'last':>12}  verdict")
    print("-" * 82)
    for series, hits in SERIES.items():
        rows = found[series]
        days = sorted({d for d, _ in rows})
        if not rows:
            print(f"{series:24} {0:>5} {0:>5} {'-':>12} {'-':>12}  NO SOURCE")
            continue
        cover = len(days) / n_files
        verdict = ("usable series" if cover >= 0.5 else
                   "episodic — carry forward" if len(days) >= 4 else
                   "too sparse to be a series")
        print(f"{series:24} {len(rows):>5} {len(days):>5} {days[0]:>12} {days[-1]:>12}"
              f"  {verdict} ({cover:.0%} of days)")

    if a.series and a.show:
        print(f"\n--- {a.series} ---")
        for d, s in found[a.series]:
            s = re.sub(r"\s+", " ", s)
            print(f"{d}  {s[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
