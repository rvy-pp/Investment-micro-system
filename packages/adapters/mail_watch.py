"""Daily structural-event watch over broker mail.

WHAT IT IS FOR. Not speed — a BSE filing or a broker note reaches everyone at
once, and P1 has already been shown to be a mirror rather than a forecast
(CLAUDE.md, "What P1 is"). This exists for the two things that ARE tractable:

  completeness     the structural line buried in 200 emails on a busy day
  quantification   "Utkal commissioning" is a headline; what it does to
                   market_pct, and therefore to EBITDA, is the work

It flags. It does not score, does not write to the store, and does not decide
anything. A hit is routed to a human, who decides whether a spec parameter moved.

THE AGENT/PYTHON SPLIT IS FORCED, not a design choice. The Microsoft 365 MCP is
callable by the AGENT, never by a Python process — the same constraint CLAUDE.md
records for the Wind MCP. So:

  step A (agent)   name-scoped searches -> data/staging/mail_YYYY-MM-DD.json
  step B (this)    filter, keyword-match, report

Staging files are version-controlled on purpose. They are the dated record of
what the mailbox returned that morning, which is what lets a catalyst be graded
later without having to trust anyone's memory of what was knowable when.

EXTENDING TO A NEW SECTOR is meant to be two edits and no code:
  1. add the entities to specs/entities/<sector>.yaml as usual
  2. add a keyword block to KEYWORDS below under the sector's name
Watch terms are DERIVED from the specs — company name, id, nse_symbol — so a new
name starts being watched as soon as it is modelled. Nothing here hardcodes
aluminium.

Usage:
    python packages/adapters/mail_watch.py --terms          # what would be searched
    python packages/adapters/mail_watch.py                  # today's staging
    python packages/adapters/mail_watch.py --date 2026-08-19
    python packages/adapters/mail_watch.py --digests        # dry-run over vault digests
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
STAGING = REPO / "data" / "staging"
sys.path.insert(0, str(REPO / "packages" / "score"))

# Structural vocabulary. "*" applies to every sector; the rest key off the
# entity's `sector` field. These are EVENT words, not opinion words — a rating
# change is broker_actions' job (P3 mood), not this watch's.
KEYWORDS: dict[str, list[str]] = {
    "*": [
        r"commission", r"ramp[- ]?up", r"ramping", r"shut ?down", r"shutdown",
        r"outage", r"maintenance", r"capacity expansion", r"brownfield",
        r"greenfield", r"capex", r"captive", r"linkage", r"e-?auction",
        r"hedg", r"realisation", r"realization", r"offtake", r"long[- ]term contract",
        r"production volume", r"sales volume", r"debottleneck", r"force majeure",
        r"strike", r"lockout", r"stake sale", r"demerger", r"merger",
    ],
    "aluminium": [
        r"pot ?line", r"smelter", r"refinery", r"alumina", r"bauxite", r"anode",
        r"utkal", r"lanjigarh", r"jharsuguda", r"angul", r"damanjodi", r"novelis",
    ],
    # Add a block per sector as it is modelled. Nothing else needs to change.
    "steel": [
        r"blast furnace", r"pellet plant", r"coking coal", r"iron ore mine",
        r"DRI", r"EAF", r"hot strip mill", r"cold rolling",
    ],
    "cement": [
        r"kiln", r"clinker", r"grinding unit", r"WHRS", r"pet ?coke",
        r"limestone", r"split grinding",
    ],
    # added 2026-08-30 — mining went live 2026-08-29 without this block (the
    # "add a block per sector" rule above was missed), ems the day after.
    "mining": [
        r"circular", r"SWMA", r"premium", r"royalty", r"MMDR", r"dispatch",
        r"evacuation", r"washery", r"overburden",
    ],
    "ems": [
        r"PLI", r"anchor customer", r"order book", r"order win", r"ODM",
        r"OSAT", r"display fab", r"backward integration", r"component",
        r"smart meter", r"compressor", r"PCB", r"copper[- ]clad", r"JV",
    ],
}

# Aliases the spec fields cannot supply. Keep SMALL — anything derivable should
# be derived. `nse_symbol` is NEEDS_CONFIRMATION for vaml, hence the explicit
# entry rather than a silent gap.
EXTRA_ALIASES: dict[str, list[str]] = {
    "nalco": ["NALCO", "NATIONALUM"],
    "vaml": ["VAML", "Vedanta Aluminium", "Vedanta Alum"],
    "hindustan_zinc": ["HZL", "HINDZINC"],
    "vedanta": ["VEDL", "Vedanta Ltd"],
    "hindalco": ["HNDL"],
}


def watch_list() -> list[dict]:
    """One entry per scoreable entity, with its search terms and keyword set."""
    from bridge import load_specs
    ents, _units, _fin = load_specs()
    out = []
    for eid, e in sorted(ents.items()):
        if not e.get("peer_group"):
            continue                       # reporting units are not searched
        terms = {e.get("name", ""), eid.replace("_", " ")}
        sym = e.get("nse_symbol") or ""
        if sym and "NEEDS" not in sym.upper():
            terms.add(sym)
        terms |= set(EXTRA_ALIASES.get(eid, []))
        sector = e.get("sector") or "*"
        kws = KEYWORDS["*"] + KEYWORDS.get(sector, [])
        out.append({"entity": eid, "sector": sector,
                    "terms": sorted(t for t in terms if t),
                    "keywords": kws})
    return out


def scan(records: list[dict], watch: list[dict]) -> list[dict]:
    """Match records against entity terms, then against structural keywords.

    A record must hit BOTH: a name we cover AND an event word. Either alone is
    noise — "commissioning" appears in every steel note ever written, and a
    Hindalco mention with no event is just coverage.
    """
    hits = []
    for rec in records:
        blob = " ".join(str(rec.get(k) or "") for k in ("subject", "summary"))
        low = blob.lower()
        for w in watch:
            if not any(t.lower() in low for t in w["terms"]):
                continue
            found = sorted({m for kw in w["keywords"]
                            for m in re.findall(kw, low, re.I)})
            if not found:
                continue
            hits.append({"entity": w["entity"], "sector": w["sector"],
                         "matched": found[:6],
                         "date": (rec.get("receivedDateTime") or "")[:10],
                         "sender": (rec.get("sender") or "").split("@")[-1],
                         "subject": (rec.get("subject") or "")[:110],
                         "summary": (rec.get("summary") or "")[:220]})
    return hits


def from_staging(day: str) -> tuple[list[dict], str | None]:
    f = STAGING / f"mail_{day}.json"
    if not f.exists():
        return [], (f"mail staging for {day}: MISSING\n"
                    f"  expected {f}\n"
                    f"  the agent search step did not run — MAIL IS NOT BEING "
                    f"MONITORED today.\n"
                    f"  This is reported loudly on purpose: a silent skip looks "
                    f"identical to a quiet day.")
    doc = json.loads(f.read_text(encoding="utf-8"))
    return (doc if isinstance(doc, list) else doc.get("records", [])), None


def from_digests(n: int = 12) -> list[dict]:
    """Dry-run input: the vault's broker digests, newest first.

    Lets the filter be exercised today, before the agent step exists. NOT a
    production path — those digests come from the deprecated vault pipeline.
    """
    vault = pathlib.Path(r"C:\Users\rajvaibhav.yadav\OneDrive - PinPOINT"
                         r"\Obsidian Vault\Broker Mails")
    if not vault.exists():
        return []
    files = []
    for p in vault.glob("*.md"):
        try:                                # DD-MM-YYYY, never plain sorted()
            d = dt.datetime.strptime(p.stem, "%d-%m-%Y").date()
        except ValueError:
            continue
        files.append((d, p))
    out = []
    for d, p in sorted(files, reverse=True)[:n]:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for para in [x.strip() for x in txt.split("\n") if len(x.strip()) > 60]:
            out.append({"subject": para[:110], "summary": para[:400],
                        "receivedDateTime": d.isoformat(), "sender": "vault-digest"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--terms", action="store_true", help="print the search plan")
    ap.add_argument("--digests", action="store_true", help="dry-run over vault digests")
    a = ap.parse_args()

    watch = watch_list()

    if a.terms:
        print("SEARCH PLAN — one query per entity, derived from the specs\n")
        for w in watch:
            print(f"  {w['entity']:16} [{w['sector']}]  {' | '.join(w['terms'])}")
        print(f"\n{len(watch)} queries/day. The agent runs these with "
              f"afterDateTime=yesterday, strips uri/webLink, and writes\n"
              f"  {STAGING / 'mail_<date>.json'}")
        secs = sorted({w["sector"] for w in watch})
        print(f"\nsectors watched: {', '.join(secs)}")
        missing = [s for s in secs if s not in KEYWORDS]
        if missing:
            print(f"NOTE: no keyword block for {missing} — those entities get the "
                  f"common set only. Add one to KEYWORDS.")
        return 0

    if a.digests:
        records = from_digests()
        print(f"DRY RUN over vault digests — {len(records)} paragraphs\n")
    else:
        records, err = from_staging(a.date)
        if err:
            print(err, file=sys.stderr)
            return 1

    hits = scan(records, watch)
    if not hits:
        print(f"{a.date}: no structural hits across {len(watch)} names "
              f"({len(records)} records scanned)")
        return 0

    seen = set()
    print(f"{a.date}: {len(hits)} structural hit(s)\n")
    for h in hits:
        key = (h["entity"], h["subject"])
        if key in seen:
            continue
        seen.add(key)
        print(f"  [{h['entity']}] {h['date']}  {h['sender']}")
        print(f"    matched : {', '.join(h['matched'])}")
        print(f"    subject : {h['subject']}")
        print(f"    context : {h['summary'][:180]}")
        print()
    print("Nothing was written to the store. Read the hit, and if a spec "
          "parameter moved, record it with its DISCLOSURE date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
