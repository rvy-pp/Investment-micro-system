"""L0 adapter — the vault's concall analysis into `concall_commitments`.

THE JUICE FOR P4, and it was sitting unused while the pillar ran on seven
hand-picked digest lines and a flat 0.50 prior.

`AI Insights/<TICKER>_Concall/Concall - <TICKER> - Q<n> FY<yy>.md` is a per-quarter
analysis of the earnings call, 42 companies deep, and its structure IS the P4
model already written down:

    ## Did they deliver? (prior commitments -> actual)   <- the PRIOR, graded
    | Said (prior) | Actual | Verdict |
    Credibility: High — Across 16 graded commitments, 10 delivered, 2 partial...

    ## New commitments -> next quarter's scorecard       <- what to SCORE
    **Hard (numbers/timelines):**
    **Soft (directional):**

Thirteen quarters for each of the four steel mills, back to FY24 and forward to
Q1 FY27. So management's delivery rate can be COUNTED rather than assessed, which
is what lets P4 keep the project's inversion intact: the model extracted and
graded the facts, deterministic code counts them.

NOT Hindsight.md, which looks like the same thing. That file is `type: hindsight`,
written by the DEPRECATED vault dashboard, and it stopped updating — last touched
18 Aug. A prior built on a frozen file silently ages into a constant while looking
like a measurement. The concall directory carries Q1 FY27 notes, so it is live.

ONLY THE DELIVERY LEDGER IS LOADED HERE, deliberately. The "New commitments"
section is prose with embedded numbers ("Q1 FY27 coking coal cost: up $12-15/t")
and belongs in `guidance` as a parsed numeric target with a verbatim quote — that
is an L1 extraction with a schema that rejects uncited rows, not a regex job. This
adapter does the part that is honestly mechanical.

VERDICTS ARE CLASSIFIED BY WORD, NOT BY EMOJI. The corpus mixes real emoji with
GitHub shortcodes — `:white_check_mark:` in one file and the character in the next,
sometimes both in one file — and matching glyphs would drop rows silently, which
is the worst kind of parse failure because the tally still looks plausible. Every
verdict cell contains exactly one of: Delivered, Partial, Missed, Not yet due.

Usage:
    python packages/adapters/vault_concall.py --probe
    python packages/adapters/vault_concall.py --load
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
VAULT = pathlib.Path(
    r"C:\Users\rajvaibhav.yadav\OneDrive - PinPOINT\Obsidian Vault\AI Insights")

# NSE ticker used by the concall directory -> our entity_id. Only names that
# exist in `entities` are listed; a missing one would violate the FK.
TICKERS = {
    "TATASTEEL": "tata_steel",
    "JSWSTEEL": "jsw_steel",
    "JINDALSTEL": "jindal_steel",
    "SAIL": "sail",
    "APLAPOLLO": "apl_apollo",
    "SHYAMMETL": "shyam_metalics",
    "HINDALCO": "hindalco",
    "NATIONALUM": "nalco",
    "HINDZINC": "hindustan_zinc",
    "VEDL": "vedanta",
    "VEDANTALUM": "vaml",
}

# Order matters: "Not yet due" must be tested BEFORE "due", and "Partial" before
# "Delivered" because a partial row often reads "Partial — Rs 30,000 crores
# delivered", which contains the word Delivered.
VERDICT_WORDS = [
    ("not_due", re.compile(r"not\s+yet\s+due", re.I)),
    ("partial", re.compile(r"\bpartial", re.I)),
    ("missed", re.compile(r"\bmissed\b|\bnot\s+met\b|\bunaddressed\b", re.I)),
    ("delivered", re.compile(r"\bdelivered\b|\bmet\b|\bachieved\b", re.I)),
]

DELIVER_HDR = re.compile(r"^##.*Did they deliver", re.I | re.M)
NEXT_HDR = re.compile(r"^##\s", re.M)
FNAME = re.compile(r"Concall - (?P<t>[A-Z]+) - Q(?P<q>\d) FY(?P<y>\d\d)", re.I)


def classify(cell: str):
    for name, pat in VERDICT_WORDS:
        if pat.search(cell):
            return name
    return None


# TWO DOCUMENT TYPES SHARE THESE DIRECTORIES and only one is gradeable. 201 of
# 216 notes are structured analyses with a delivery table; the other 15 are raw
# dumps opening "==== EARNINGS TRANSCRIPT ANALYSIS ====" with no graded section.
# Six of Tata Steel's thirteen quarters are the raw kind, which is why its graded
# count is 35 against JSW's 167 — that asymmetry is the CORPUS, not a parse bug,
# and it took a file-by-file check to tell the difference.
#
# The distinction is reported rather than swallowed, because "no delivery table
# because this is a transcript" and "no delivery table because the format
# changed" look identical from the row count, and the second one silently shrinks
# every prior built on it.
TRANSCRIPT = re.compile(r"EARNINGS TRANSCRIPT ANALYSIS", re.I)


def parse_file(path: pathlib.Path):
    """([(said, actual, verdict)], tally, kind)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    m = DELIVER_HDR.search(text)
    if not m:
        kind = "transcript" if TRANSCRIPT.search(text[:4000]) else "UNRECOGNISED"
        return [], None, kind
    rest = text[m.end():]
    nxt = NEXT_HDR.search(rest)
    block = rest[: nxt.start()] if nxt else rest

    rows = []
    for line in block.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        said, actual, verdict = cells[0], cells[1], cells[2]
        # header and separator rows
        if said.lower().startswith("said") or set(said) <= set("-: "):
            continue
        v = classify(verdict)
        if not v or not said or not actual:
            continue
        # strip markdown bold so the UNIQUE key is stable if emphasis changes
        said = re.sub(r"\*+", "", said).strip()
        actual = re.sub(r"\*+", "", actual).strip()
        rows.append((said, actual, v))

    tally = None
    tm = re.search(r"^Credibility:\s*(.+)$", block, re.M)
    if tm:
        tally = re.sub(r"\s+", " ", tm.group(1)).strip()[:400]
    return rows, tally, "analysis"


def collect(only: str | None = None):
    out = []
    for tk, eid in TICKERS.items():
        if only and eid != only:
            continue
        d = VAULT / f"{tk}_Concall"
        if not d.is_dir():
            out.append((eid, None, [], None))
            continue
        for f in sorted(d.glob("*.md")):
            fm = FNAME.search(f.name)
            if not fm:
                continue
            period = f"Q{fm.group('q')}FY{fm.group('y')}"
            rows, tally, kind = parse_file(f)
            out.append((eid, period, rows, tally, f, kind))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--entity")
    a = ap.parse_args()

    found = [x for x in collect(a.entity) if len(x) == 6]
    if not found:
        print("no concall notes matched — check VAULT and TICKERS")
        return 1

    per_entity: dict[str, list[int]] = {}
    print(f"{'entity':16}{'call':9}{'rows':>5}{'del':>5}{'part':>5}{'miss':>5}"
          f"{'ndue':>5}")
    print("-" * 60)
    unrecognised = [(e, p, f.name) for e, p, _r, _t, f, k in found
                    if k == "UNRECOGNISED"]
    n_trans = sum(1 for x in found if x[5] == "transcript")
    for eid, period, rows, _tally, _f, _k in found:
        c = {k: sum(1 for r in rows if r[2] == k)
             for k in ("delivered", "partial", "missed", "not_due")}
        print(f"{eid:16}{period:9}{len(rows):>5}{c['delivered']:>5}"
              f"{c['partial']:>5}{c['missed']:>5}{c['not_due']:>5}")
        acc = per_entity.setdefault(eid, [0, 0, 0, 0, 0])
        acc[0] += len(rows)
        for i, k in enumerate(("delivered", "partial", "missed", "not_due")):
            acc[i + 1] += c[k]

    print(f"\n{'entity':16}{'graded':>8}{'del':>6}{'part':>6}{'miss':>6}"
          f"{'ndue':>6}{'rate':>8}")
    print("-" * 60)
    for eid, (n, d, p, m, nd) in sorted(per_entity.items()):
        # DUE = delivered + partial + missed. `not_due` is EXCLUDED from the
        # denominator: a commitment whose date has not arrived is not evidence
        # either way, and counting it as a non-delivery would penalise a company
        # for having long-dated plans. Partial counts as HALF.
        due = d + p + m
        rate = (d + 0.5 * p) / due if due else None
        r = "—" if rate is None else f"{rate:.2f}"
        print(f"{eid:16}{n:>8}{d:>6}{p:>6}{m:>6}{nd:>6}{r:>8}")

    if not a.load:
        print("\nprobe only — pass --load to write")
        return 0

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = skipped = 0
    for eid, period, rows, _tally, f, _k in found:
        for said, actual, verdict in rows:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO concall_commitments "
                    "(entity_id,call_period,said,actual,verdict,source_path,"
                    "created_at) VALUES (?,?,?,?,?,?,?)",
                    (eid, period, said, actual, verdict,
                     str(f.relative_to(VAULT.parent)), now))
                n += conn.total_changes and 1 or 0
            except sqlite3.IntegrityError as exc:
                skipped += 1
                print(f"  skipped {eid}/{period}: {exc}")
    conn.commit()
    got = conn.execute("SELECT COUNT(*) FROM concall_commitments").fetchone()[0]
    conn.close()
    print(f"\nconcall_commitments now holds {got} rows"
          + (f"; {skipped} skipped" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
