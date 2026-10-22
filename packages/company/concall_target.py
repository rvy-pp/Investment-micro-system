"""Resolve a /concall request — "tcs q2fy27" — to everything the skill needs.

The /concall skill (`.claude/skills/concall/`) is agent-driven: it reads the
Google Finance call transcript in the browser and writes the analyst note. The
parts that are NOT judgement live here, per the repo's split (scripts do the
mapping, the agent does the reading):

  * which company the PM meant ("tcs", "TCS", "tata consultancy", "ltim")
  * the Google Finance symbol      — yahoo_prices.CANDIDATES' .NS symbol
  * the vault folder               — engine._concall_dir, else the NSE symbol
  * the quarter, normalised        — "q2fy27" / "2QFY27" / "Q2 FY27" -> Q2 FY27
  * the Google label to expect     — "Fiscal Q2 2027 earnings call"
  * the prior quarter's note       — Q1 rolls the FY back (Indian FY, Apr-Mar)
  * whether this quarter's note already exists

The Google symbol and the vault folder are DIFFERENT KEYS and must not be
merged: LTIMindtree trades as LTM (it renamed itself "LTM Limited") while its
vault folder kept LTIM_Concall; VAML's folder is VEDANTALUM_Concall. Deriving
one from the other silently writes a second folder beside the first, and the
Results tab's Concall notes sub-tab would show only one of them.

Usage:
    python packages/company/concall_target.py tcs q2fy27
    python packages/company/concall_target.py --selftest
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "api"))
sys.path.insert(0, str(REPO / "packages" / "adapters"))

QTR = re.compile(r"^\s*(?:q\s*([1-4])\s*[-_ ]?\s*fy\s*'?(\d{2}|\d{4})"
                 r"|([1-4])\s*q\s*fy\s*'?(\d{2}|\d{4}))\s*[ef]?\s*$", re.I)


def norm_quarter(s: str):
    """'q2fy27' / 'Q2 FY27' / '2QFY27' / 'Q2FY2027' -> (2, 27), else None."""
    m = QTR.match(s or "")
    if not m:
        return None
    q = int(m.group(1) or m.group(3))
    fy = m.group(2) or m.group(4)
    fy = int(fy[-2:])
    return q, fy


def prior(q: int, fy: int):
    return (4, fy - 1) if q == 1 else (q - 1, fy)


def _roster():
    import engine
    return engine, engine._results_roster()


def _google_symbol(eid: str, nse: str | None):
    import yahoo_prices as yp
    for sym, _pat in yp.CANDIDATES.get(eid, []):
        if sym.endswith(".NS"):
            return sym[:-3]
    return nse if nse and re.fullmatch(r"[A-Z0-9&-]+", nse) else None


def resolve_company(query: str):
    """-> (matches, how). Exact keys first; fuzzy only when nothing exact hits,
    and a fuzzy hit on more than one name is returned as ambiguous rather than
    picked — the wrong company's call would be graded against the right
    company's prior note."""
    import yahoo_prices as yp
    engine, roster = _roster()
    q = query.strip().lower()
    qn = re.sub(r"[^a-z0-9]", "", q)
    exact = []
    for r in roster.values():
        keys = {r["id"], re.sub(r"[^a-z0-9]", "", r["id"])}
        if r.get("nse"):
            keys.add(r["nse"].lower())
        t, _f = engine._concall_dir(r["id"], r.get("nse"))
        if t:
            keys.add(t.lower())
        g = _google_symbol(r["id"], r.get("nse"))
        if g:
            keys.add(g.lower())
        if q in keys or qn in keys:
            exact.append(r)
    if exact:
        return exact, "exact"
    fuzzy = []
    for r in roster.values():
        pats = [p for _s, p in yp.CANDIDATES.get(r["id"], [])]
        if any(re.search(p, q, re.I) for p in pats) or q in r["name"].lower():
            fuzzy.append(r)
    return fuzzy, "fuzzy"


def target(company: str, quarter: str) -> dict:
    nq = norm_quarter(quarter)
    if not nq:
        return {"ok": False, "error": f"cannot read quarter {quarter!r} — "
                "expected e.g. q2fy27, Q2 FY27, 2QFY27"}
    q, fy = nq
    engine, _roster_ = _roster()
    from vault_concall import VAULT as AI
    matches, how = resolve_company(company)
    if len(matches) > 1:
        return {"ok": False, "error": "ambiguous company",
                "candidates": [{"id": r["id"], "name": r["name"]} for r in matches]}
    if matches:
        r = matches[0]
        gsym = _google_symbol(r["id"], r.get("nse"))
        tick, folder = engine._concall_dir(r["id"], r.get("nse"))
        tick = tick or gsym
        ent = {"entity": r["id"], "name": r["name"], "sector": r["sector"],
               "resolved": how}
    else:
        # Not in the coverage roster. Accept the query AS an NSE symbol — the
        # Google page names the company, and the skill must check that name
        # before reading a word of the call.
        if not re.fullmatch(r"[A-Za-z0-9&-]{2,20}", company.strip()):
            return {"ok": False, "error": f"no covered company matches {company!r}"}
        gsym = tick = company.strip().upper()
        ent = {"entity": None, "name": None, "sector": None,
               "resolved": "as_symbol — not in the coverage roster; verify the "
                           "company name on the Google page"}
    if not gsym:
        return {"ok": False, "error": f"{ent['entity']}: no NSE symbol known "
                "(not listed on NSE?) — Google Finance has nothing to open"}
    folder = AI / f"{tick}_Concall"
    label = f"Q{q} FY{fy:02d}"
    pq, pfy = prior(q, fy)
    plabel = f"Q{pq} FY{pfy:02d}"
    note = folder / f"Concall - {tick} - {label}.md"
    pnote = folder / f"Concall - {tick} - {plabel}.md"
    return {
        "ok": True, **ent,
        "quarter": label, "ticker": tick, "google_symbol": gsym,
        "google_url": f"https://www.google.com/finance/quote/{gsym}:NSE?tab=earnings",
        "google_label": f"Fiscal Q{q} 20{fy:02d} earnings call",
        "folder": str(folder), "note_path": str(note), "note_exists": note.exists(),
        "prior_quarter": plabel, "prior_path": str(pnote),
        "prior_exists": pnote.exists(),
    }


def _selftest() -> int:
    ok = 0

    def eq(a, b, what):
        nonlocal ok
        assert a == b, f"{what}: {a!r} != {b!r}"
        ok += 1

    # quarter parsing — acceptance and rejection (the GLOB lesson)
    for s in ["q2fy27", "Q2 FY27", "2QFY27", "Q2FY2027", "q2 fy'27", "2QFY27E"]:
        eq(norm_quarter(s), (2, 27), s)
    for s in ["q5fy27", "fy27", "q2", "", "Q2 2027"]:
        eq(norm_quarter(s), None, f"reject {s!r}")
    # Q1 rolls the FY back
    eq(prior(1, 27), (4, 26), "prior of Q1"); eq(prior(3, 27), (2, 27), "prior of Q3")

    # company resolution — the two keys that differ must stay different
    t = target("tcs", "q2fy27")
    eq((t["entity"], t["google_symbol"], t["ticker"], t["google_label"]),
       ("tcs", "TCS", "TCS", "Fiscal Q2 2027 earnings call"), "tcs")
    t = target("ltimindtree", "q1fy27")
    eq((t["google_symbol"], t["ticker"]), ("LTM", "LTIM"), "LTM trades, LTIM folder")
    t = target("vaml", "q1fy27")
    eq((t["google_symbol"], t["ticker"]), ("VAML", "VEDANTALUM"), "vaml folder")
    eq(target("TATASTEEL", "q1fy27")["entity"], "tata_steel", "by NSE symbol")
    eq(target("tata consultancy", "q2fy27")["entity"], "tcs", "by name pattern")
    # existing-note detection against the real vault
    t = target("tata steel", "q1fy27")
    eq((t["note_exists"], t["prior_exists"], t["prior_quarter"]),
       (True, True, "Q4 FY26"), "tata steel Q1 FY27 note on file")
    # an off-roster symbol is accepted but flagged; garbage is refused
    t = target("RELIANCE", "q2fy27")
    eq((t["ok"], t["entity"], t["ticker"]), (True, None, "RELIANCE"), "off-roster")
    assert t["resolved"].startswith("as_symbol"); ok += 1
    eq(target("tata consultancy services ltd.", "q9")["ok"], False, "bad quarter")
    eq(target("!!", "q2fy27")["ok"], False, "garbage name")
    print(f"concall_target selftest: {ok} checks passed")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(_selftest())
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    print(json.dumps(target(" ".join(sys.argv[1:-1]), sys.argv[-1]), indent=2))
