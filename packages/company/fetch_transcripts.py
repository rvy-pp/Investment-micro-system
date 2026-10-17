"""Find, download and text-extract a company's earnings-call transcripts.

Transport only. It resolves NOTHING about what the calls mean — that is the
agent's half, per the standing split in this repo: the agent decides, the
script executes.

    python packages/company/fetch_transcripts.py AVALON --slug avalon-technologies
    python packages/company/fetch_transcripts.py AVALON --list      # no download
    python packages/company/fetch_transcripts.py --selftest

Screener.in's company page carries a Concalls block whose "Transcript" links
point at the COMPANY'S OWN IR host (or BSE) — public PDFs, no login, no token.
Plain stdlib urllib with a browser User-Agent gets HTTP 200. That is the whole
mechanism, and it is worth stating because lme.com's 403 once led this repo to
conclude a source needed an agent when a mirror answered urllib in 1.5s.

FOUR TRAPS, all met on the first company run (Avalon, 14 calls):

1. **Rows duplicate.** The same transcript URL appears twice in the block for
   some quarters. Dedupe on URL, not on date — two different calls can share a
   month label ("May 2024") when a transcript is re-filed.

2. **The quarter is not in the link.** It is derived from the MONTH the call
   was held: Aug -> Q1, Nov -> Q2, Feb -> Q3, May/Jun -> Q4. The fiscal year
   rolls with it (Aug-2023 is Q1FY24; Feb-2024 is Q3FY24). Filenames that
   carry a quarter are inconsistent across hosts and brokers — do not parse
   them.

3. **Some transcripts are BROKER-hosted** (MOFSL, JM Financial) rather than
   company-hosted. Same call, different provenance, and the moderator wording
   differs enough to break a naive speaker regex. The host is recorded.

4. **The first page is often a covering letter** to BSE/NSE, not the call.
   Flagged here, skipped by the reader — the note spec says so explicitly.

Text extraction keeps `--- page N ---` markers because every downstream
citation is a page number. A second cleaned copy strips the repeated
header/footer furniture (Page N of M, the company name line, the date line)
which is ~5% of the bytes and 100% noise to a reader.
"""
from __future__ import annotations

import argparse
import html
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
COMPANIES = REPO / "data" / "companies"

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Call month -> (quarter, fiscal-year offset from the calendar year).
# Jun is a late Q4 call (Avalon's first, 05-Jun-2023 for FY23).
#
# JULY IS AMBIGUOUS, AND THE DEFAULT IS WRONG FOR EVERY LARGE IT NAME. The
# default map reads a July call as a late Q4 (an industrial reporting FY
# results in July). Indian IT reports inside two weeks of quarter-end, so
# TCS/Infosys/Wipro/HCL/Coforge hold their Q1 call in July, Q2 in October,
# Q3 in January, Q4 in April. Coforge's dossier (2026-09-15) had to relabel
# every Q1 call by hand because of this. `--early` swaps in EARLY_Q, which
# differs in exactly one key: Jul -> Q1 of the NEXT fiscal year. Nothing else
# moves, and the choice is the CALLER's — the script cannot know a company's
# reporting cadence from its symbol. Check the transcript cover anyway.
MONTH_Q = {"Aug": ("Q1", 1), "Sep": ("Q1", 1),
           "Nov": ("Q2", 1), "Dec": ("Q2", 1),
           "Feb": ("Q3", 0), "Jan": ("Q3", 0), "Mar": ("Q3", 0),
           "May": ("Q4", 0), "Jun": ("Q4", 0), "Jul": ("Q4", 0),
           "Apr": ("Q4", 0), "Oct": ("Q2", 1)}
EARLY_Q = {**MONTH_Q, "Jul": ("Q1", 1)}


def _get(url: str, timeout: int = 60) -> bytes:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read()


def concall_rows(symbol: str, month_q: dict | None = None) -> list[dict]:
    """Dated Transcript links off screener.in's Concalls block, deduped."""
    month_q = month_q or MONTH_Q
    page = _get(f"https://www.screener.in/company/{symbol}/consolidated/",
                timeout=45).decode("utf-8", "replace")
    i = page.lower().find("concall")
    if i < 0:
        raise SystemExit(f"no Concalls block on screener for {symbol!r} — "
                         f"check the symbol, or the company has no calls filed")
    block = page[i:i + 20000]
    rows, seen = [], set()
    for li in re.split(r"<li[^>]*>", block):
        dm = re.search(r">\s*([A-Z][a-z]{2})\s+(20\d\d)\s*<", li)
        tm = re.search(r'href="([^"]+)"[^>]*>\s*(?:<[^>]+>\s*)*Transcript', li)
        if not (dm and tm):
            continue
        url = html.unescape(tm.group(1))
        if url in seen:
            continue                      # trap 1: rows duplicate
        seen.add(url)
        mon, yr = dm.group(1), int(dm.group(2))
        if mon not in month_q:
            print(f"  ! unmapped call month {mon} {yr} — skipped", file=sys.stderr)
            continue
        q, off = month_q[mon]             # trap 2: quarter comes from the month
        host = re.sub(r"^www\.", "", (re.search(r"https?://([^/]+)", url)
                                      or re.match("", "")).group(1))
        rows.append({"label": f"{q}FY{str(yr + off)[2:]}", "called": f"{mon} {yr}",
                     "month": mon, "year": yr, "url": url, "host": host,
                     # trap 3: broker-hosted is the same call, other provenance
                     "provenance": "company_ir" if "bseindia" not in host
                                   and "nseindia" not in host else "exchange"})
    rows.sort(key=lambda r: (r["year"], ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                         "Jul", "Aug", "Sep", "Oct", "Nov",
                                         "Dec"].index(r["month"])))
    return rows


# Unicode directional-override and zero-width marks. Some filings (anything
# exported through Google Docs, in practice) wrap EVERY word in U+202D…U+202C,
# and those have to come out before any word count means anything.
_BIDI = re.compile(r"[‪-‮​‎‏­]")


def _page_text(page) -> tuple[str, bool]:
    """One page's text, and whether the de-spacing fallback had to be used.

    TRAP 5, FOUND ON EICHER 2026-09-20 AND IT IS THE SILENT-ARITHMETIC SHAPE.
    PyMuPDF's default `get_text()` reads inter-word spacing from the PDF's own
    encoding. Three of Eicher's seventeen filings (Q1/Q2/Q3 FY25, all exported
    through Google Docs) encode each word as its own directional-override run
    with NO space between the runs, so the default extractor returned
    `Ourtotalsalesstoodatabout2,27,736motorcyclesinthisquarteras`.

    Nothing raised. The file existed, `scanned` was correctly False, every
    NUMBER survived intact, and the only symptom was a word count 55% low
    (4,353 against a real ~9,700) — which reads as "that was a short call",
    not as "the extractor is broken". A dossier built on it would have quoted
    management in run-together prose and under-read three quarters.

    The fallback rebuilds each line from `get_text("words")`, which derives
    word boundaries from GLYPH POSITIONS rather than from encoded spaces, and
    joins them itself. It is used ONLY where the default output is materially
    worse, because words-mode re-orders text on a multi-column page and the
    other fourteen files extract correctly as they are. Do not make it the
    default.
    """
    default = _BIDI.sub("", page.get_text())
    ws = page.get_text("words")            # (x0,y0,x1,y1,word,block,line,no)
    if not ws:
        return default, False
    # Same content, one word per tuple — so this is the honest word count.
    if len(default.split()) >= 0.75 * len(ws):
        return default, False
    lines, cur, key = [], [], None
    for w in sorted(ws, key=lambda w: (w[5], w[6], w[7])):
        if (w[5], w[6]) != key:
            if cur:
                lines.append(" ".join(cur))
            cur, key = [], (w[5], w[6])
        cur.append(_BIDI.sub("", w[4]))
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines), True


def extract(pdf: pathlib.Path, text_dir: pathlib.Path,
            clean_dir: pathlib.Path) -> dict:
    """PDF -> page-marked text + a boilerplate-stripped copy."""
    import fitz                                            # PyMuPDF
    doc = fitz.open(pdf)
    pages, n_respaced = [], 0
    for n, p in enumerate(doc, 1):
        txt, fixed = _page_text(p)
        n_respaced += fixed
        pages.append(f"--- page {n} ---\n" + txt)
    raw = "\n".join(pages)
    text_dir.mkdir(parents=True, exist_ok=True)
    (text_dir / (pdf.name + ".txt")).write_text(raw, encoding="utf-8")

    out, page = [], 0
    for ln in raw.splitlines():
        s = ln.strip()
        m = re.match(r"^--- page (\d+) ---$", s)
        if m:
            page = int(m.group(1))
            out.append(f"[p{page}]")
            continue
        if not s or re.match(r"^Page \d+ of \d+$", s):
            continue
        if re.match(r"^(January|February|March|April|May|June|July|August|"
                    r"September|October|November|December) \d{1,2}, 20\d\d$", s):
            continue
        out.append(s)
    clean_dir.mkdir(parents=True, exist_ok=True)
    (clean_dir / (pdf.stem + ".txt")).write_text("\n".join(out), encoding="utf-8")

    head = re.sub(r"\s+", " ", pages[0])[:300].lower()
    return {"pages": doc.page_count, "words": len(raw.split()),
            # Reported, never hidden: a run that silently repaired half a
            # filing is a run whose word counts mean two different things.
            "respaced_pages": n_respaced,
            # trap 4: a covering letter is not the call
            "cover_letter": bool(re.search(r"\bsub:|sirs,|listing department|"
                                           r"corporate relationship", head)),
            # a scanned filing extracts almost nothing - refuse, never guess
            "scanned": len(raw.split()) < 400 * doc.page_count / 15}


def run(symbol: str, slug: str | None, listing_only: bool,
        early: bool = False) -> int:
    rows = concall_rows(symbol, EARLY_Q if early else MONTH_Q)
    if early:
        print("month map: EARLY reporter (Jul -> Q1 of the next FY)")
    print(f"{symbol}: {len(rows)} transcripts "
          f"{rows[0]['label']} ({rows[0]['called']}) -> "
          f"{rows[-1]['label']} ({rows[-1]['called']})")
    if listing_only:
        for r in rows:
            print(f"  {r['label']:<8} {r['called']:<9} {r['provenance']:<11} {r['url']}")
        return 0

    slug = slug or symbol.lower()
    base = COMPANIES / slug
    src, txt, cln = base / "sources", base / "sources" / "_text", base / "sources" / "_clean"
    src.mkdir(parents=True, exist_ok=True)
    man, bad = [], 0
    for r in rows:
        name = f"transcript_{r['label']}_{r['month']}{r['year']}.pdf"
        p = src / name
        if not p.exists():
            try:
                b = _get(r["url"])
            except urllib.error.HTTPError as e:
                print(f"  {r['label']:<8} FAILED http {e.code}"); bad += 1; continue
            except Exception as e:
                print(f"  {r['label']:<8} FAILED {type(e).__name__}"); bad += 1; continue
            if b[:5] != b"%PDF-":
                print(f"  {r['label']:<8} REFUSED — not a PDF "
                      f"({b[:20]!r}); host may have served an error page")
                bad += 1
                continue
            p.write_bytes(b)
        meta = extract(p, txt, cln)
        flags = " ".join(k for k in ("cover_letter", "scanned") if meta[k])
        if meta.get("respaced_pages"):
            flags += f" respaced:{meta['respaced_pages']}p"
        print(f"  {r['label']:<8} {r['called']:<9} {meta['pages']:>3}p "
              f"{meta['words']:>7,}w  {r['provenance']:<11} {flags}")
        man.append({**r, "file": name, **meta})

    (base / "transcripts.json").write_text(
        json.dumps(man, indent=1), encoding="utf-8")
    gaps = _gaps([m["label"] for m in man])
    print(f"\n{len(man)} filed, {bad} failed -> {base}")
    print("quarter sequence: " + ("CONTIGUOUS, no gaps" if not gaps
                                  else "GAPS at " + ", ".join(gaps)))
    print("The oldest call is the company's FIRST as a listed entity unless a "
          "gap is listed above — say so in the dossier, because 'all of them' "
          "and 'a sample' are different claims.")
    return 0


def _gaps(labels: list[str]) -> list[str]:
    """Missing quarters inside the span. A gap is a fact about coverage."""
    def key(l):
        q = int(l[1]); fy = int(l[4:])
        return fy * 4 + q
    if not labels:
        return []
    ks = sorted(key(l) for l in labels)
    have = set(ks)
    out = []
    for k in range(ks[0], ks[-1] + 1):
        if k not in have:
            # key = fy*4 + q, so fy is (k-1)//4 for EVERY quarter. The first
            # version used k//4, which is fy+1 whenever q == 4: a missing
            # Q4FY26 printed as "Q4FY27". Caught on the LTTS run; the old
            # selftest only checked the COUNT of gaps, not the label.
            out.append(f"Q{(k - 1) % 4 + 1}FY{(k - 1) // 4}")
    return out


def selftest() -> int:
    ok = True

    def chk(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {'ok ' if good else 'FAIL'} {name}: {got!r}"
              + ("" if good else f" != {want!r}"))

    print("quarter derivation (trap 2) — both directions:")
    chk("Aug 2023 -> Q1FY24", f"{MONTH_Q['Aug'][0]}FY{str(2023 + MONTH_Q['Aug'][1])[2:]}", "Q1FY24")
    chk("Nov 2025 -> Q2FY26", f"{MONTH_Q['Nov'][0]}FY{str(2025 + MONTH_Q['Nov'][1])[2:]}", "Q2FY26")
    chk("Feb 2026 -> Q3FY26", f"{MONTH_Q['Feb'][0]}FY{str(2026 + MONTH_Q['Feb'][1])[2:]}", "Q3FY26")
    chk("May 2026 -> Q4FY26", f"{MONTH_Q['May'][0]}FY{str(2026 + MONTH_Q['May'][1])[2:]}", "Q4FY26")
    chk("Jun 2023 -> Q4FY23", f"{MONTH_Q['Jun'][0]}FY{str(2023 + MONTH_Q['Jun'][1])[2:]}", "Q4FY23")
    print("the July ambiguity — default and --early disagree on exactly one month:")
    chk("default: Jul 2026 -> Q4FY26", f"{MONTH_Q['Jul'][0]}FY{str(2026 + MONTH_Q['Jul'][1])[2:]}", "Q4FY26")
    chk("early:   Jul 2026 -> Q1FY27", f"{EARLY_Q['Jul'][0]}FY{str(2026 + EARLY_Q['Jul'][1])[2:]}", "Q1FY27")
    chk("early:   Apr 2026 -> Q4FY26 (unchanged)", f"{EARLY_Q['Apr'][0]}FY{str(2026 + EARLY_Q['Apr'][1])[2:]}", "Q4FY26")
    chk("early:   Oct 2025 -> Q2FY26 (unchanged)", f"{EARLY_Q['Oct'][0]}FY{str(2025 + EARLY_Q['Oct'][1])[2:]}", "Q2FY26")
    chk("the two maps differ only on Jul",
        sorted(k for k in MONTH_Q if MONTH_Q[k] != EARLY_Q[k]), ["Jul"])

    print("gap detection — acceptance AND rejection, per the GLOB lesson:")
    chk("contiguous run has no gaps",
        _gaps(["Q1FY25", "Q2FY25", "Q3FY25", "Q4FY25"]), [])
    chk("a hole is reported",
        _gaps(["Q1FY25", "Q3FY25"]), ["Q2FY25"])
    chk("a Q4 hole is labelled with ITS year, not the next",
        _gaps(["Q3FY26", "Q1FY27"]), ["Q4FY26"])
    chk("a year-end hole spanning the FY roll",
        _gaps(["Q4FY25", "Q2FY26"]), ["Q1FY26"])
    chk("single label has no gaps", _gaps(["Q1FY25"]), [])
    chk("empty has no gaps", _gaps([]), [])

    # TRAP 5 — the de-spacing fallback. ACCEPTANCE AND REJECTION BOTH, per the
    # GLOB lesson: a guard that only proves it fires has not been tested. The
    # rejection case is the one that matters here, because words-mode reorders
    # multi-column text, so firing on a healthy page would CORRUPT fourteen
    # files to repair three.
    class _P:
        def __init__(self, flat, words):
            self._flat, self._words = flat, words

        def get_text(self, mode=None):
            return self._words if mode == "words" else self._flat

    _w = [("Our", 0, 0, 0), ("total", 0, 0, 1), ("sales", 0, 0, 2),
          ("stood", 0, 0, 3), ("at", 0, 0, 4), ("2,27,736", 0, 0, 5)]
    _tup = [(0, 0, 0, 0, t, b, l, n) for t, b, l, n in _w]

    broken = _P("‭Our‬‭total‬‭sales‬"
                "‭stood‬‭at‬‭2,27,736‬", _tup)
    got, fixed = _page_text(broken)
    chk("a de-spaced page is repaired", got, "Our total sales stood at 2,27,736")
    chk("...and the repair is reported", fixed, True)

    healthy = _P("Our total sales stood at 2,27,736", _tup)
    got, fixed = _page_text(healthy)
    chk("a healthy page is LEFT ALONE", got, "Our total sales stood at 2,27,736")
    chk("...and is not reported as repaired", fixed, False)

    # A page with bidi marks but real spaces must NOT trip the fallback either
    # — the marks are cosmetic there and words-mode would reorder for nothing.
    marked = _P("‭Our total sales stood at 2,27,736‬", _tup)
    chk("bidi marks alone do not trigger the fallback",
        _page_text(marked)[1], False)
    chk("...but the marks are still stripped",
        _page_text(marked)[0], "Our total sales stood at 2,27,736")

    chk("an empty page cannot divide by zero", _page_text(_P("", []))[1], False)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol", nargs="?", help="NSE symbol as screener knows it")
    ap.add_argument("--slug", help="folder under data/companies/ (default: lowercased symbol)")
    ap.add_argument("--list", action="store_true", help="list only, download nothing")
    ap.add_argument("--early", action="store_true",
                    help="early reporter (Indian IT): a July call is Q1 of the "
                         "next FY, not a late Q4")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.symbol:
        ap.error("symbol required (or --selftest)")
    return run(a.symbol, a.slug, a.list, a.early)


if __name__ == "__main__":
    raise SystemExit(main())
