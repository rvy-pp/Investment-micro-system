"""Stage a sector of the PinPOINT results-preview dashboard into the Results
store, one cited row per number the broker gave.

    python packages/results/preview_dashboard.py --sector IT --exclude Jefferies --list
    python packages/results/preview_dashboard.py --sector IT --exclude Jefferies --stage
    python packages/results/results_io.py --load data/results/staging/<file>.json
    python packages/results/preview_dashboard.py --selftest

PM, 2026-10-10: "In the estimates vs print sub tab in the results tab, make
something like this. Make for IT atleast, and the brokers we have (we dont
have jeferries)." The source is OneDrive - PinPOINT/India Q2 FY 27 Results
Dashboard.html, a desk-built page that transcribes each house's preview into
a `const DATA = {...}` literal: sector -> [broker block {meta, notes,
stocks[]}]. It is DATA here, not methodology, the same standing the vault's
Broker Mails corpus has.

THE ROWS GO INTO `estimates` THROUGH results_io's LOADER, never around it.
That keeps the three guards (all-or-nothing, units agree, no post-print
forecast) and means a staged preview is graded against the print by the same
code as every other estimate. The quote names the house, the report title and
date, the company, the field and the figure in the source's own unit (Rs mn),
and says it came via the dashboard, so the citation points at a document a
person can open.

WHAT IS DELIBERATELY NOT LOADED, each because it is not a number the house
stated:

  * patM / patMPY / patMY, opmPY, prevOpm. The dashboard computes PAT margin
    from PAT / revenue and the year-ago margins from stated growth; prevOpm
    is a reported ACTUAL for another quarter, not an estimate for this one.
  * Any field flagged ebcA / ebcG (derived from revenue x margin).
  * Nomura's EBIT and EBIT YoY. Its block's own notes say "computed by us
    from INR revenue x EBIT margin" without setting the flags — so the skip
    is listed by name in DERIVED below rather than trusted to the flag.
  * vsConsRev / vsConsDiff: a house's quote of SOMEONE ELSE'S consensus.
  * Names outside the roster (Cyient, Hexaware, Sagility, ...). A name is
    added to `entities` by its spec or adapter, never by a loader; they are
    listed by --list so the omission is visible.

TWO FIELD TRAPS, both met in the IT section on the first read:

  * `ebitda` IS EBIT for an IT block (marginType "EBIT"), EXCEPT MOSL's
    email, where the TCS row's note says the EBITDA margin is 27.1% while
    `opm` is the ~25% EBIT margin — 732,113 x 25% = 183,028, not the
    198,273 stored. Loaded as EBIT it would be a 8% overstatement that reads
    as a bullish house. FIELD_AS lists the override.
  * Margin deltas arrive as bps, sometimes a string ("-104bps"). Stored in
    PERCENTAGE POINTS (unit "pp") so results_io grades them on its existing
    pp branch; the board renders bps.

Wipro and Cyient are SEGMENT figures in most blocks (Wipro IT services,
Cyient DET). The basis per (house, company) rides in the source's `meta`,
never in the unit, so the board can tag it without a unit split refusing a
cross-house comparison of the same line.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
STAGING = REPO / "data" / "results" / "staging"
DASHBOARD = pathlib.Path.home() / "OneDrive - PinPOINT" / "India Q2 FY 27 Results Dashboard.html"
RAW_PATH = "OneDrive - PinPOINT/India Q2 FY 27 Results Dashboard.html"

# Bloomberg equity ticker -> entity_id. The IMS futures roots in
# specs/book.yaml are a DIFFERENT key (COFOR vs COFORGE, LTM vs LTIM).
TICKERS = {
    "TCS IN": "tcs", "INFO IN": "infosys", "HCLT IN": "hcl_tech",
    "WPRO IN": "wipro", "TECHM IN": "tech_mahindra", "LTIM IN": "ltimindtree",
    "PSYS IN": "persistent", "COFORGE IN": "coforge", "MPHL IN": "mphasis",
    "KPITTECH IN": "kpit", "TELX IN": "tata_elxsi", "LTTS IN": "ltts",
    "OFSS IN": "ofss",
}

# dashboard field -> (metric suffix or full metric, scale, unit, label in quote)
# A "{m}" metric takes the block's margin type (ebit for IT, ebitda elsewhere).
FIELDS = [
    ("usdRev",  "revenue_usd",           1.0,  "USD mn", "revenue US$mn"),
    ("ccQQ",    "revenue_growth_cc_pct", 1.0,  "%",      "revenue growth cc QoQ %"),
    ("usdQoQ",  "revenue_usd_qoq_pct",   1.0,  "%",      "US$ revenue QoQ %"),
    ("rev",     "revenue",               0.1,  "INR cr", "revenue Rs mn"),
    ("revQoQ",  "revenue_qoq_pct",       1.0,  "%",      "revenue QoQ %"),
    ("revG",    "revenue_yoy_pct",       1.0,  "%",      "revenue YoY %"),
    ("opm",     "{m}_margin_pct",        1.0,  "%",      "{M} margin %"),
    ("opmQoQ",  "{m}_margin_qoq_pp",     0.01, "pp",     "{M} margin change QoQ bps"),
    ("opmY",    "{m}_margin_yoy_pp",     0.01, "pp",     "{M} margin change YoY bps"),
    ("ebitda",  "{m}",                   0.1,  "INR cr", "{M} Rs mn"),
    ("ebitdaG", "{m}_yoy_pct",           1.0,  "%",      "{M} YoY %"),
    ("pat",     "pat",                   0.1,  "INR cr", "PAT Rs mn"),
    ("patQoQ",  "pat_qoq_pct",           1.0,  "%",      "PAT QoQ %"),
    ("patG",    "pat_yoy_pct",           1.0,  "%",      "PAT YoY %"),
]

# (house, field) the block's notes say the dashboard computed, not the house
DERIVED = {("Nomura", "ebitda"), ("Nomura", "ebitdaG")}
# (house, field) -> margin type the field actually carries
FIELD_AS = {("MOSL", "ebitda"): "ebitda", ("MOSL", "ebitdaG"): "ebitda"}
# (house, ticker) -> ISO date, where one block spans several mails
ROW_DATE = {("MOSL", "TCS IN"): "2026-10-08", ("MOSL", "HCLT IN"): "2026-10-09"}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def house(broker: str) -> tuple[str, bool]:
    """'Avendus Spark (email)' -> ('Avendus Spark', True)."""
    b = broker.strip()
    em = b.lower().endswith("(email)")
    return re.sub(r"\s*\(email\)\s*$", "", b, flags=re.I).strip(), em


def parse_date(s: str) -> str | None:
    """'28 Sep 2026' / '08 Oct 2026 / 09 Oct 2026' (-> the LATER) / 'Sep 2026'
    (-> None: a month is not a date, and as_of must be one)."""
    out = []
    for m in re.finditer(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", s or ""):
        mon = MONTHS.get(m.group(2).lower())
        if mon:
            out.append(dt.date(int(m.group(3)), mon, int(m.group(1))))
    return max(out).isoformat() if out else None


def bps(v):
    """opmY arrives as a number OR '-104bps' / '+20bps'."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(v))
    return float(m.group(0)) if m else None


def read_dashboard(path: pathlib.Path) -> dict:
    """The `const DATA = {...};` literal. Pure JSON in the file as shipped;
    refused rather than eval'd if that ever stops being true."""
    txt = path.read_text(encoding="utf-8")
    m = re.search(r"^const DATA = (\{.*\});?\s*$", txt, re.M)
    if not m:
        raise SystemExit(f"no `const DATA = {{...}}` line in {path}")
    return json.loads(m.group(1))


def _fmt_src(v) -> str:
    if isinstance(v, str):
        return v
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}"


def stage(D: dict, sector: str, exclude: set[str], known: set[str] | None = None) -> dict:
    """Build a results_io staging doc for one dashboard sector."""
    if sector not in D:
        raise SystemExit(f"sector {sector!r} not in the dashboard: {sorted(D)}")
    sources, ests, skipped = [], [], {"excluded": [], "unrostered": {}, "undated": []}
    for blk in D[sector]:
        meta = blk["meta"]
        name, email = house(meta["broker"])
        if name.lower() in exclude:
            skipped["excluded"].append(meta["broker"])
            continue
        bdate = parse_date(meta.get("date", ""))
        quarter = meta.get("quarter", "")
        mt = (meta.get("marginType") or ("EBIT" if sector == "IT" else "EBITDA")).lower()
        sid = "preview-%s-%s-%s" % (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
                                    quarter.lower(), bdate or "undated")
        basis, notes = {}, {}
        for d in blk["stocks"]:
            eid = TICKERS.get(d.get("ticker"))
            if eid is None or (known is not None and eid not in known):
                skipped["unrostered"].setdefault(d.get("name"), []).append(name)
                continue
            seg = d.get("basis") == "Segment"
            basis[eid] = {"basis": d.get("basis") or "Consol",
                          "src": d.get("basisSrc") or "inferred",
                          "note": d.get("basisNote") or "",
                          "segment": d.get("name") if seg else None}
            if d.get("note"):
                notes[eid] = d["note"]
            as_of = ROW_DATE.get((name, d.get("ticker"))) or bdate
            if not as_of:
                skipped["undated"].append(f"{name} {d.get('name')}")
                continue
            for fld, metric, scale, unit, lab in FIELDS:
                if (name, fld) in DERIVED:
                    continue
                if fld == "ebitda" and d.get("ebcA") or fld == "ebitdaG" and d.get("ebcG"):
                    continue
                raw = d.get(fld)
                v = bps(raw) if fld in ("opmY", "opmQoQ") else raw
                if v is None or v == "" or isinstance(v, bool):
                    continue
                m_t = FIELD_AS.get((name, fld), mt)
                ests.append({
                    "entity_id": eid, "broker": name, "as_of": as_of,
                    "period": quarter, "metric": metric.format(m=m_t),
                    "value_num": round(float(v) * scale, 6), "unit": unit,
                    "source_id": sid,
                    "quote": "%s, '%s' (%s): %s%s %s %s = %s [via India Q2 FY27 Results Dashboard]" % (
                        meta["broker"], meta.get("report", ""), meta.get("date", ""),
                        d.get("name"), " (%s)" % d.get("basis") if seg else "",
                        quarter, lab.format(M=m_t.upper()), _fmt_src(raw))})
        sources.append({
            "id": sid, "kind": "broker_email" if email else "broker_note",
            "origin": name, "title": meta.get("report") or f"{name} {quarter} preview",
            "source_date": bdate, "raw_path": RAW_PATH,
            "meta": {"preview": True, "sector": sector, "quarter": quarter,
                     "date_text": meta.get("date"), "email": email,
                     "margin_type": mt.upper(), "units": meta.get("units"),
                     "block_notes": blk.get("notes") or "",
                     "basis": basis, "notes": notes}})
    return {"note": f"{sector} {', '.join(sorted({s['meta']['quarter'] for s in sources}))} "
                    f"previews from the PinPOINT results-preview dashboard; houses excluded: "
                    f"{', '.join(skipped['excluded']) or 'none'}",
            "sources": sources, "estimates": ests, "_skipped": skipped}


def _known_entities() -> set[str]:
    import sqlite3
    c = sqlite3.connect(REPO / "data" / "ims.db")
    try:
        return {r[0] for r in c.execute("SELECT id FROM entities")}
    finally:
        c.close()


def selftest() -> int:
    fails = 0

    def check(ok, msg):
        nonlocal fails
        print(("  ok   " if ok else "  FAIL ") + msg)
        fails += 0 if ok else 1

    check(house("Avendus Spark (email)") == ("Avendus Spark", True), "email suffix stripped")
    check(house("Kotak") == ("Kotak", False), "plain house kept")
    check(parse_date("08 Oct 2026 / 09 Oct 2026") == "2026-10-09", "two dates -> the later")
    check(parse_date("1 Oct 2026") == "2026-10-01", "single-digit day")
    check(parse_date("Sep 2026") is None, "a bare month is NOT a date (refused, not guessed)")
    check(bps("-104bps") == -104.0 and bps("+20bps") == 20.0 and bps(-13) == -13.0, "bps string and number")
    check(bps(None) is None and bps("") is None, "empty bps -> None")
    D = {"IT": [
        {"meta": {"broker": "Kotak", "quarter": "2QFY27E", "date": "28 Sep 2026",
                  "report": "r", "marginType": "EBIT"}, "notes": "",
         "stocks": [{"name": "Infosys", "ticker": "INFO IN", "basis": "Consol", "rev": 494061,
                     "opm": 20.9, "opmY": "-8bps", "opmQoQ": -13, "ebitda": 103483,
                     "patM": 15.5, "prevOpm": 21.1},
                    {"name": "Cyient (DET)", "ticker": "CYL IN", "basis": "Segment", "rev": 1}]},
        {"meta": {"broker": "Nomura", "quarter": "2QFY27F", "date": "01 Oct 2026",
                  "report": "r", "marginType": "EBIT"}, "notes": "computed by us",
         "stocks": [{"name": "Infosys", "ticker": "INFO IN", "ebitda": 1, "ebitdaG": 2, "opm": 21.2}]},
        {"meta": {"broker": "MOSL (email)", "quarter": "2QFY27E", "date": "08 Oct 2026 / 09 Oct 2026",
                  "report": "r", "marginType": "EBIT"}, "notes": "",
         "stocks": [{"name": "TCS", "ticker": "TCS IN", "ebitda": 198273, "opm": 25}]},
        {"meta": {"broker": "Jefferies", "quarter": "2QFY27E", "date": "01 Oct 2026", "report": "r"},
         "stocks": [{"name": "TCS", "ticker": "TCS IN", "rev": 1}]}]}
    doc = stage(D, "IT", {"jefferies"})
    by = {(e["broker"], e["entity_id"], e["metric"]): e for e in doc["estimates"]}
    check(("Kotak", "infosys", "revenue") in by and by[("Kotak", "infosys", "revenue")]["value_num"] == 49406.1,
          "Rs mn -> INR cr (/10)")
    check(by[("Kotak", "infosys", "ebit_margin_yoy_pp")]["value_num"] == -0.08, "bps string -> pp")
    check(by[("Kotak", "infosys", "ebit_margin_qoq_pp")]["value_num"] == -0.13, "bps number -> pp")
    check(("Kotak", "infosys", "ebit") in by, "IT block's `ebitda` field lands as EBIT")
    check(not any(k[2].startswith("pat_margin") for k in by), "computed PAT margin NOT loaded")
    check(not any("prev" in k[2] for k in by), "prior-quarter actual NOT loaded as an estimate")
    check(("Nomura", "infosys", "ebit") not in by and ("Nomura", "infosys", "ebit_yoy_pct") not in by,
          "Nomura's dashboard-computed EBIT refused")
    check(("Nomura", "infosys", "ebit_margin_pct") in by, "Nomura's stated margin kept (acceptance)")
    check(("MOSL", "tcs", "ebitda") in by and ("MOSL", "tcs", "ebit") not in by,
          "MOSL's EBITDA is not mislabelled EBIT")
    check(by[("MOSL", "tcs", "ebitda")]["as_of"] == "2026-10-08", "per-row date override for a two-mail block")
    check(not any(e["broker"] == "Jefferies" for e in doc["estimates"]), "excluded house absent")
    check("Cyient (DET)" in doc["_skipped"]["unrostered"], "unrostered name listed, not loaded")
    check(all(e["quote"].strip() for e in doc["estimates"]), "every row carries a quote")
    print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sector", default="IT")
    ap.add_argument("--exclude", action="append", default=[],
                    help="house to leave out (repeatable), e.g. Jefferies")
    ap.add_argument("--file", type=pathlib.Path, default=DASHBOARD)
    ap.add_argument("--list", action="store_true", help="print what would be staged")
    ap.add_argument("--stage", action="store_true", help="write the staging JSON")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    D = read_dashboard(a.file)
    doc = stage(D, a.sector, {x.lower() for x in a.exclude}, _known_entities())
    sk = doc.pop("_skipped")
    for s in doc["sources"]:
        n = sum(1 for e in doc["estimates"] if e["source_id"] == s["id"])
        print(f"  {s['origin']:<16} {s['source_date']}  {len(s['meta']['basis']):>2} names  {n:>3} rows  {s['title'][:60]}")
    print(f"  {len(doc['estimates'])} rows from {len(doc['sources'])} houses")
    print(f"  excluded: {', '.join(sk['excluded']) or 'none'}")
    if sk["unrostered"]:
        print("  not in the roster (not loaded): " + "; ".join(
            f"{k} ({', '.join(v)})" for k, v in sorted(sk["unrostered"].items())))
    if sk["undated"]:
        print("  undated (not loaded): " + ", ".join(sk["undated"]))
    if a.stage:
        STAGING.mkdir(parents=True, exist_ok=True)
        q0 = sorted({s["meta"]["quarter"] for s in doc["sources"]})[0]
        m = re.match(r"([1-4])Q\s*FY(\d{2})", q0, re.I)          # 2QFY27E -> q2fy27
        q = f"q{m.group(1)}fy{m.group(2)}" if m else q0.lower()
        out = STAGING / f"{dt.date.today().isoformat()}_{q}_{a.sector.lower()}_previews.json"
        out.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"  wrote {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
