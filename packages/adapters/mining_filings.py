"""Mining primary-source feeds — Coal India monthly filings + NMDC circulars/filings.

Six series, all source 'filing' (prices_io rank 40), all COARSE — monthly or
event-dated — so none of them can ever set the store clock (series.py measures
cadence; SILENT_BUGS 8b).

    coalindia_offtake_ttm_mt        trailing-12M offtake, mt/yr, month-end stamped
    coal_eauction_realisation_inr   notified_base x (1 + SWMA premium), INR/t
    coal_fsa_realisation_inr        blended FSA realisation, INR/t, quarterly cited
    nmdc_sales_ttm_mt               trailing-12M iron ore sales, mt/yr
    nmdc_lumps_inr                  Baila lump 65.5% FOR, EX-ROYALTY basis, INR/t
    nmdc_fines_inr                  Baila fines 64% FOR, EX-ROYALTY basis, INR/t

THE TTM FORMULA NEEDS NO HISTORY CHAIN. Every CIL filing carries this-year AND
last-year, monthly AND fiscal-YTD, so

    TTM(month m) = FY_prev_total + (FYTD_m - FYTD_LY_m)

is computable from ONE document plus one anchor. Gaps (an unparseable OCR month)
cost that month's point, never the months after it. A March row's FYTD is itself
the next FY anchor.

WHY TTM AND NOT THE RAW MONTHLY PRINT. The bridge multiplies a volume-effect
line's EBITDA/t by the series DELTA. A raw monthly series' month-over-month
delta is mostly monsoon seasonality — every September would read as a demand
collapse. The TTM delta is exactly (this month minus the same month last year):
the YoY increment the brokers themselves quote, in annualised tonnes.

TWO BASIS GUARDS, both load-bearing:

  1. NMDC changed its circular basis on 2026-01-09 — through Nov-2025 the FOR
     prices are INCLUSIVE of Royalty+DMF+NMET, from Jan-2026 EXCLUSIVE. The
     apparent Rs1,000/t Jan-26 "cut" (5,600 -> 4,600 lumps) is ~18% of basis
     redefinition, consistent with 15% royalty x 1.32 DMF/NMET gross-up.
     Loading both under one id would book that redefinition as a price crash,
     so ONLY basis == "ex_royalty" rows load, and the fetch path re-checks the
     basis sentence of every new circular rather than trusting the label.
  2. Month-end stamping is min(month_end, capture/filing date) and a future
     date is refused — the cement-pack lesson (SILENT_BUGS 8): an in-progress
     period stamped at month end moves as_of ahead of every equity close.

SOURCES AND THEIR LAG, measured 2026-08-29:
  coalindia.in/performance/physical/           timely (~1st of next month)
  coalindia.in/performance/swma-e-auction-data timely
  nmdc.co.in CMS API                           ~6 MONTHS STALE (newest: Feb-26)
so NMDC's recent months and price changes come from the desk ledger
specs/extracted/mining_prints.json, cited to the digests that carried the
filings. --fetch will pick the website rows back up whenever NMDC catches up;
identical months collide at equal rank and simply overwrite with the same value.

Usage:
    python packages/adapters/mining_filings.py            # probe: fetch + parse, write nothing
    python packages/adapters/mining_filings.py --fetch    # refresh data/staging/mining/
    python packages/adapters/mining_filings.py --load     # staging + ledger -> prices
    python packages/adapters/mining_filings.py --selftest
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
import sys
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))
import prices_io  # noqa: E402

DB = REPO / "data" / "ims.db"
STAGING = REPO / "data" / "staging" / "mining"
LEDGER = REPO / "specs" / "extracted" / "mining_prints.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

CIL_PHYSICAL = "https://www.coalindia.in/performance/physical/"
CIL_SWMA = "https://www.coalindia.in/performance/swma-e-auction-data/"
NMDC_API = "https://www.nmdc.co.in/cms-admin/api/"
NMDC_DOCS = "https://www.nmdc.co.in/cms-admin"
# Ships in the site's own Angular bundle (main.*.js, globalService.apiKey) —
# a public read key, not a secret.
NMDC_KEY = "weryewrtuewshwfuyrtgergg"

SERIES = {
    "coalindia_offtake_ttm_mt": "CIL offtake, trailing 12M, mt/yr",
    "coal_eauction_realisation_inr": "CIL e-auction realisation (notified x (1+SWMA premium)) INR/t",
    "coal_fsa_realisation_inr": "CIL blended FSA realisation INR/t (quarterly cited)",
    "nmdc_sales_ttm_mt": "NMDC iron ore sales, trailing 12M, mt/yr",
    "nmdc_lumps_inr": "NMDC Baila lump 65.5% FOR ex-royalty INR/t",
    "nmdc_fines_inr": "NMDC Baila fines 64% FOR ex-royalty INR/t",
}

# plausibility bands — a delta block or a mis-parsed cell cannot pass these
SANE_PRICE_INR = (500.0, 20_000.0)
SANE_TTM_MT = {"coalindia_offtake_ttm_mt": (500.0, 1200.0),
               "nmdc_sales_ttm_mt": (30.0, 90.0)}


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def _month_end(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    nxt = dt.date(y + (m == 12), (m % 12) + 1, 1)
    return (nxt - dt.timedelta(days=1)).isoformat()


def _stamp(month: str, today: dt.date | None = None) -> str:
    """min(month_end, today) — never a future row (SILENT_BUGS 8)."""
    today = today or dt.date.today()
    end = _month_end(month)
    return min(end, today.isoformat())


# ---------------------------------------------------------------------------
# fetch — Coal India (timely) and NMDC (lagging, but re-checked every run)
# ---------------------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"])}


def _title_month(title: str) -> str | None:
    """'... for the month of July'26 and ...' -> '2026-07'."""
    t = title.lower().replace("’", "'")
    m = re.search(r"month of\s+([a-z]+)[a-z]*\.?\s*'?\s*(\d{2,4})", t)
    if not m:
        m = re.search(r"for\s+([a-z]+)\s*'?(\d{2,4})", t)
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3])
    if not mon:
        return None
    yr = int(m.group(2))
    yr = yr + 2000 if yr < 100 else yr
    return f"{yr:04d}-{mon:02d}"


def _pdf_text(data: bytes) -> str:
    from io import BytesIO
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(BytesIO(data)).pages)


def parse_cil_offtake(text: str) -> dict | None:
    """CIL-total row pairs from a production/offtake filing (row-major layout).

    Two 'CIL <6 numbers>' rows: production first, OFFTAKE second. A month-only
    filing (Apr) has monthly columns only; then FYTD == monthly for April.
    OCR-scanned months (May/Jun-26 were) fail here and are hand-entered into
    staging with the digest corroboration recorded — a mangled OCR digit
    passing a regex is exactly the silent-arithmetic shape, so no OCR fixups.
    """
    rows = re.findall(
        r"^CIL\s+(-?[\d.]+)\s+(-?[\d.]+)\s+-?[\d.]+\s+(-?[\d.]+)\s+(-?[\d.]+)\s+-?[\d.]+\s*$",
        text, re.M)
    if len(rows) >= 2:
        off = rows[1]  # production block prints first, offtake second
        return {"offtake": float(off[0]), "offtake_ly": float(off[1]),
                "fytd": float(off[2]), "fytd_ly": float(off[3])}
    short = re.findall(r"^CIL\s+(-?[\d.]+)\s+(-?[\d.]+)\s+-?[\d.]+\s*$", text, re.M)
    if len(short) >= 2:  # month-only (April) layout
        off = short[1]
        return {"offtake": float(off[0]), "offtake_ly": float(off[1]),
                "fytd": float(off[0]), "fytd_ly": float(off[1])}
    return None


def parse_cil_swma(text: str) -> dict | None:
    """CIL-total monthly premium %% and allocated lakh tonnes from a SWMA filing.

    First 'Qty. allocated' / '%% increase over Notified Price' lines are the
    MONTH table; the second pair is fiscal-YTD. The CIL total is the LAST value
    on the line; the first-format filing (Feb-26) had no CIL column, in which
    case the volume-weighted mean across subsidiaries is used.
    """
    qty_lines = re.findall(r"Qty\.?\s*allocated \(in Lakh Tonnes\)([^\n]+)", text)
    prem_lines = re.findall(r"% increase over Notified Price([^\n]+)", text)
    if not qty_lines or not prem_lines:
        return None
    qtys = [float(x) for x in re.findall(r"-?[\d.]+", qty_lines[0])]
    prems = [float(x) for x in re.findall(r"-?[\d.]+", prem_lines[0])]
    if not qtys or not prems:
        return None
    if len(prems) == 9:          # 8 subsidiaries + CIL total
        return {"premium_pct": prems[-1], "alloc_lt": qtys[-1]}
    if len(prems) == len(qtys) == 8:   # first-format filing: no total column
        w = sum(q * p for q, p in zip(qtys, prems)) / (sum(qtys) or 1)
        return {"premium_pct": round(w, 1), "alloc_lt": round(sum(qtys), 2)}
    return None


def parse_nmdc_monthly(text: str) -> dict | None:
    """Total row of an NMDC production & sales filing: 8 numbers —
    [prod, prod_ly, sales, sales_ly, cumprod, cumprod_ly, cumsales, cumsales_ly]."""
    m = re.search(r"Total\s+((?:-?\d+\.\d+\s+){7}-?\d+\.\d+)", text)
    if not m:
        return None
    v = [float(x) for x in m.group(1).split()]
    return {"prod": v[0], "prod_ly": v[1], "sales": v[2], "sales_ly": v[3],
            "cumprod": v[4], "cumprod_ly": v[5],
            "cumsales": v[6], "cumsales_ly": v[7]}


def parse_nmdc_circular(text: str) -> dict | None:
    """Lump/fines FOR prices plus the BASIS SENTENCE — the sentence decides
    whether the row may load at all (see the module header)."""
    lump = re.search(r"Lump[^\n]*?([\d,]{4,7})\s*[/-]", text)
    fines = re.search(r"Fines[^\n]*?([\d,]{4,7})\s*[/-]", text)
    if not (lump and fines):
        return None
    if re.search(r"inclusive of Royalty", text, re.I):
        basis = "incl_royalty"
    elif re.search(r"exclusive of Royalty", text, re.I):
        basis = "ex_royalty"
    else:
        basis = "unknown"       # refuse downstream — never guess a basis
    eff = re.search(r"w\.?e\.?f\.?\s*\.?\s*([\dA-Za-z .thstnrd-]+?\d{4})", text)
    return {"lumps": int(lump.group(1).replace(",", "")),
            "fines": int(fines.group(1).replace(",", "")),
            "basis": basis,
            "date_raw": eff.group(1).strip() if eff else None}


def _nmdc_api(coll: str, index: int = 1, pagesize: int = 10) -> list[dict]:
    body = urllib.parse.urlencode(
        {"lang": "En", "index": index, "pagesize": pagesize}).encode()
    req = urllib.request.Request(
        NMDC_API + coll, data=body,
        headers={**UA, "ApiKey": NMDC_KEY,
                 "Content-Type": "application/x-www-form-urlencoded"})
    doc = json.loads(urllib.request.urlopen(req, timeout=25).read().decode())
    return (doc.get("data") or {}).get("list") or []


def _read_staging(name: str) -> dict:
    f = STAGING / name
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _write_staging(name: str, doc: dict) -> None:
    STAGING.mkdir(parents=True, exist_ok=True)
    (STAGING / name).write_text(json.dumps(doc, indent=1), encoding="utf-8")


def fetch(write: bool = True) -> None:
    today = dt.date.today().isoformat()

    # ---- Coal India: production/offtake ------------------------------------
    doc = _read_staging("cil_offtake.json")
    have = {r["month"] for r in doc.get("months", [])}
    try:
        html = _get(CIL_PHYSICAL).decode("utf-8", "replace")
        pairs = re.findall(r'<a[^>]+href="(https://[^"]+\.pdf)"[^>]*>(.*?)</a>',
                           html, re.S)
        for href, raw_title in pairs[:6]:      # newest few only — history is staged
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_title)).strip()
            month = _title_month(title)
            if not month or month in have or month < "2026-04":
                continue
            got = parse_cil_offtake(_pdf_text(_get(href, 40)))
            if not got:
                print(f"   CIL {month}: filing found but UNPARSEABLE (OCR scan?) — "
                      f"hand-enter into specs/extracted/mining_prints.json cil_months\n"
                      f"      {href}")
                continue
            row = {"month": month, **got, "doc": href.rsplit('/', 1)[-1],
                   "how": f"mining_filings.py --fetch {today}"}
            doc["months"].append(row)
            if month.endswith("-03"):
                # a March filing's FYTD IS the finished FY total — next anchor
                fy = f"fy{int(month[:4])}_offtake_mt"
                doc.setdefault("fy_anchors", {})[fy] = got["fytd"]
            have.add(month)
            print(f"   CIL {month}: offtake {got['offtake']} vs {got['offtake_ly']} LY"
                  f"  (FYTD {got['fytd']} / {got['fytd_ly']})")
        if write:
            doc["months"] = sorted(doc["months"], key=lambda r: r["month"])
            _write_staging("cil_offtake.json", doc)
    except Exception as e:
        print(f"   CIL physical page fetch failed ({type(e).__name__}: {e}) — "
              f"staged history unaffected")

    # ---- Coal India: SWMA e-auction ----------------------------------------
    doc = _read_staging("cil_swma.json")
    have = {r["month"] for r in doc.get("months", [])}
    try:
        html = _get(CIL_SWMA).decode("utf-8", "replace")
        pairs = re.findall(r'<a[^>]+href="(https://[^"]+\.pdf)"[^>]*>(.*?)</a>',
                           html, re.S)
        for href, raw_title in pairs[:4]:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_title)).strip()
            month = _title_month(title)
            if not month or month in have:
                continue
            got = parse_cil_swma(_pdf_text(_get(href, 40)))
            if not got:
                print(f"   SWMA {month}: filing found but unparseable — hand-enter "
                      f"into mining_prints.json cil_swma_months\n      {href}")
                continue
            doc["months"].append({"month": month, **got,
                                  "doc": href.rsplit('/', 1)[-1],
                                  "how": f"mining_filings.py --fetch {today}"})
            have.add(month)
            print(f"   SWMA {month}: premium {got['premium_pct']}%, "
                  f"allocated {got['alloc_lt']} LT")
        if write:
            doc["months"] = sorted(doc["months"], key=lambda r: r["month"])
            _write_staging("cil_swma.json", doc)
    except Exception as e:
        print(f"   CIL SWMA page fetch failed ({type(e).__name__}: {e})")

    # ---- NMDC: monthly filings + circulars (site lags ~6 months) -----------
    doc = _read_staging("nmdc_prod_sales.json")
    have = {r["month"] for r in doc.get("months", [])}
    try:
        newest = _nmdc_api("investornews/productiondetails", 1, 5)
        n_new = 0
        for item in newest:
            m = re.search(r"upto (\w+),?\s*(\d{4})", item["title"])
            if not m or not MONTHS.get(m.group(1).lower()[:3]):
                continue
            month = f"{int(m.group(2)):04d}-{MONTHS[m.group(1).lower()[:3]]:02d}"
            if month in have:
                continue
            got = parse_nmdc_monthly(_pdf_text(_get(NMDC_DOCS + item["url"], 40)))
            if got:
                doc["months"].append({"month": month, **got,
                                      "doc": f"NMDC CMS productiondetails id {item['id']}"})
                have.add(month); n_new += 1
                print(f"   NMDC {month}: sales {got['sales']} vs {got['sales_ly']} LY")
        if not n_new:
            latest = max(have) if have else "none"
            print(f"   NMDC site still lagging (newest staged month {latest}) — "
                  f"recent months live in mining_prints.json")
        elif write:
            doc["months"] = sorted(doc["months"], key=lambda r: r["month"])
            _write_staging("nmdc_prod_sales.json", doc)
    except Exception as e:
        print(f"   NMDC CMS fetch failed ({type(e).__name__}: {e})")

    doc = _read_staging("nmdc_circulars.json")
    have = {r["effective"] for r in doc.get("circulars", [])}
    try:
        n_new = 0
        for item in _nmdc_api("investornews/ironoresales", 1, 5):
            got = parse_nmdc_circular(_pdf_text(_get(NMDC_DOCS + item["url"], 40)))
            if not got or not got["date_raw"]:
                continue
            eff = _parse_date(got["date_raw"])
            if not eff or eff in have:
                continue
            doc["circulars"].append({"effective": eff, "lumps": got["lumps"],
                                     "fines": got["fines"], "basis": got["basis"],
                                     "doc": f"NMDC CMS ironoresales id {item['id']}"})
            have.add(eff); n_new += 1
            print(f"   NMDC circular {eff}: {got['lumps']}/{got['fines']} ({got['basis']})")
        if n_new and write:
            doc["circulars"] = sorted(doc["circulars"], key=lambda r: r["effective"])
            _write_staging("nmdc_circulars.json", doc)
    except Exception as e:
        print(f"   NMDC circular fetch failed ({type(e).__name__}: {e})")


def _parse_date(raw: str) -> str | None:
    raw = re.sub(r"(\d)(st|nd|rd|th)", r"\1", raw.strip())
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# load — staging + ledger -> prices, source 'filing'
# ---------------------------------------------------------------------------


def build_rows(today: dt.date | None = None) -> list[tuple[str, str, float]]:
    """(entity_id, date, value) for every series, from staging + ledger."""
    today = today or dt.date.today()
    rows: list[tuple[str, str, float]] = []
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))

    # ---- CIL offtake TTM ----------------------------------------------------
    cil = _read_staging("cil_offtake.json")
    anchors = cil.get("fy_anchors", {})
    fy_total = {2026: anchors.get("fy2026_offtake_mt"),
                2025: anchors.get("fy2025_offtake_mt")}
    for k, v in anchors.items():
        m = re.match(r"fy(\d{4})_offtake_mt", k)
        if m:
            fy_total[int(m.group(1))] = v
    for fy_year, total in sorted(fy_total.items()):
        if total:
            rows.append(("coalindia_offtake_ttm_mt", f"{fy_year}-03-31", float(total)))
    for r in cil.get("months", []) + ledger.get("cil_months", []):
        month = r["month"]
        if month.endswith("-03"):
            continue                      # the anchor row already carries March
        fy_prev = int(month[:4]) + (1 if int(month[5:7]) >= 4 else 0) - 1
        anchor = fy_total.get(fy_prev)
        if not anchor:
            continue
        ttm = anchor + r["fytd"] - r["fytd_ly"]
        rows.append(("coalindia_offtake_ttm_mt", _stamp(month, today), round(ttm, 1)))

    # ---- CIL e-auction realisation -------------------------------------------
    swma = _read_staging("cil_swma.json")
    base = float(swma.get("notified_base_inr_per_t") or 0)
    if base:
        for r in swma.get("months", []) + ledger.get("cil_swma_months", []):
            val = base * (1 + r["premium_pct"] / 100.0)
            rows.append(("coal_eauction_realisation_inr",
                         _stamp(r["month"], today), round(val, 1)))

    # ---- CIL FSA realisation (ledger only, quarterly cited) ------------------
    for r in ledger.get("cil_fsa_realisation", []):
        rows.append(("coal_fsa_realisation_inr", r["date"], float(r["value"])))

    # ---- NMDC sales TTM -------------------------------------------------------
    nm = _read_staging("nmdc_prod_sales.json")
    fy_sales: dict[int, float] = {}
    for a in ledger.get("nmdc_fy_anchors", []):
        yr = int(a["fy_end"][:4])
        fy_sales[yr] = float(a["ttm_sales_mt"])
        rows.append(("nmdc_sales_ttm_mt", a["fy_end"], float(a["ttm_sales_mt"])))
    staged = {r["month"]: {"fytd_sales": r["cumsales"],
                           "fytd_sales_ly": r["cumsales_ly"]}
              for r in nm.get("months", []) if not r["month"].endswith("-03")}
    manual = {r["month"]: r for r in ledger.get("nmdc_months", [])}
    for month, r in sorted({**staged, **manual}.items()):
        fy_prev = int(month[:4]) + (1 if int(month[5:7]) >= 4 else 0) - 1
        anchor = fy_sales.get(fy_prev)
        if not anchor:
            continue
        ttm = anchor + r["fytd_sales"] - r["fytd_sales_ly"]
        rows.append(("nmdc_sales_ttm_mt", _stamp(month, today), round(ttm, 2)))

    # ---- NMDC administered prices — THE BASIS GUARD --------------------------
    circ = _read_staging("nmdc_circulars.json")
    refused = 0
    for r in (circ.get("circulars", []) + ledger.get("nmdc_prices", [])):
        if r.get("basis") != "ex_royalty":
            refused += 1                  # incl_royalty is a DIFFERENT measure
            continue
        rows.append(("nmdc_lumps_inr", r["effective"], float(r["lumps"])))
        rows.append(("nmdc_fines_inr", r["effective"], float(r["fines"])))
    if refused:
        print(f"   {refused} pre-2026 circular(s) on the incl_royalty basis "
              f"refused — invariant 6, see the module header")

    # ---- validation: sane ranges, no future dates ----------------------------
    out = []
    for eid, d, v in rows:
        if d > today.isoformat():
            print(f"   REFUSED future-dated row {eid} {d}")
            continue
        lo, hi = SANE_TTM_MT.get(eid) or (SANE_PRICE_INR if eid.endswith("_inr")
                                          else (0.0, 1e9))
        if not (lo <= v <= hi):
            print(f"   REFUSED implausible {eid} {d} = {v} (sane {lo}..{hi})")
            continue
        out.append((eid, d, v))
    return out


def load() -> int:
    rows = build_rows()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    prices_io.ensure_source_column(conn)
    for eid, name in SERIES.items():
        # is_tradeable=1, matching every other commodity insert here — the
        # schema CHECKs (is_tradeable = 1 OR parent_id IS NOT NULL), and an
        # INSERT OR IGNORE swallows the violation silently, leaving the FK on
        # `prices` to fail one line later. Found the hard way on first load.
        conn.execute("INSERT OR IGNORE INTO entities (id,kind,name,is_tradeable,active) "
                     "VALUES (?,?,?,1,1)", (eid, "commodity", name))
    n = 0
    for eid in SERIES:
        batch = [(e, d, v) for e, d, v in rows if e == eid]
        if not batch:
            continue
        res = prices_io.upsert(conn, batch, "filing")
        n += res["wrote"]
        note = f", {res['refused']} refused (higher rank)" if res["refused"] else ""
        print(f"   {eid:32} {res['wrote']:>3} rows{note}")
    conn.commit()
    conn.close()
    return n


# ---------------------------------------------------------------------------
# selftest — arithmetic and both guards, against the committed staging
# ---------------------------------------------------------------------------


def selftest() -> int:
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print(("   ok   " if cond else "   FAIL ") + msg)
        ok = ok and cond

    rows = build_rows(today=dt.date(2026, 8, 29))
    by = {}
    for eid, d, v in rows:
        by.setdefault(eid, {})[d] = v

    # CIL TTM: Jul-26 = 744.8 + (261.9 - 245.2) = 761.5; Mar-26 anchor row
    chk(by["coalindia_offtake_ttm_mt"].get("2026-07-31") == 761.5,
        "CIL TTM Jul-26 = 761.5 (anchor 744.8 + FYTD gap 16.7)")
    chk(by["coalindia_offtake_ttm_mt"].get("2026-03-31") == 744.8,
        "CIL FY26 anchor row = 744.8")
    # NMDC TTM: Feb-26 = 44.40 + (44.34 - 40.20) = 48.54; Jul-26 ledger month
    chk(by["nmdc_sales_ttm_mt"].get("2026-02-28") == 48.54,
        "NMDC TTM Feb-26 = 48.54")
    chk(by["nmdc_sales_ttm_mt"].get("2026-07-31") == 50.26,
        "NMDC TTM Jul-26 = 50.26 (FY26 anchor 50.14 + 0.12)")
    # e-auction: Jul-26 = 1614 x 1.41
    got = by["coal_eauction_realisation_inr"].get("2026-07-31")
    chk(got is not None and abs(got - 1614 * 1.41) < 0.5,
        f"e-auction realisation Jul-26 = {got} (1614 x 1.41)")
    # basis guard: no ex-royalty series row may predate 2026-01-09
    early = [d for d in by.get("nmdc_lumps_inr", {}) if d < "2026-01-09"]
    chk(not early, "no incl_royalty circular leaked into nmdc_lumps_inr")
    chk(by["nmdc_lumps_inr"].get("2026-08-08") == 5250.0,
        "NMDC lumps 08-Aug-26 = 5,250 (ledger)")
    # future-date refusal
    fut = [d for dd in by.values() for d in dd if d > "2026-08-29"]
    chk(not fut, "no future-dated rows")
    # circular basis parser: both directions
    chk(parse_nmdc_circular(
        "Lump (65.5%) – ₹ 4,700/- Per Ton.\nFines (64%) – ₹ 4,000/-\n"
        "These prices are FOR prices that are exclusive of Royalty, DMF")["basis"]
        == "ex_royalty", "basis parser reads 'exclusive'")
    chk(parse_nmdc_circular(
        "Lump – ₹ 6,100- Per Ton.\nFines – ₹ 5,250/-\n"
        "The above FOR prices are inclusive of Royalty, DMF, NMET")["basis"]
        == "incl_royalty", "basis parser reads 'inclusive' (and the 6,100- no-slash form)")
    print("selftest " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.fetch:
        fetch(write=True)
        return 0
    if a.load:
        n = load()
        print(f"loaded {n} rows into {DB}")
        return 0
    print("probe mode — fetch + parse, nothing written:")
    fetch(write=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
