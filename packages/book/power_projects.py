"""Power Grid's project list, read from the CEA's monthly progress reports and
reconciled to the model's asset base.

PM, 2026-11-13: *"In the assets sheet, get the projects list, their capex
requirement, how much has been capitalized, the commissioning date/completed.
Make it match with the asset base, any leftover/spillover report it back."*

No broker model carries a project list with costs and dates (the three in
Downloads stop at totals and the subsidiary roster), and the company's deck
gives totals only. The Central Electricity Authority does: three monthly
reports on cea.nic.in, Reports > Transmission Schemes.

| report | what it gives |
|---|---|
| TBCB under construction | every project, its estimated cost, SPV transfer date, original and anticipated completion, and per ELEMENT the progress and the date each was charged |
| TBCB completed | every finished project, its cost and each element's actual commissioning month |
| RTM under construction | the regulated (cost-plus) projects, same shape; only those with a line or a substation |

Only PGCIL's rows are kept. Each under-construction report's own summary row
(PGCIL: number of projects and cost) is the transcription guard: a parse whose
count or cost disagrees refuses, the IMS cross-foot rule.

Two things are ESTIMATES and are labelled so everywhere they appear:

- **Capitalised to date.** The CEA gives no rupees spent or capitalised per
  project, only which elements are charged. A project's capitalised figure is
  cost x (charged elements / all elements), every element weighted equally;
  an element partly charged (one of two transformers, one circuit of two)
  counts its share. A substation and a line are not equal in cost, so this
  is a count, not a valuation. It is tested rather than trusted: summed by
  the quarter each element was charged, it is set against the capitalisation
  the company reported.
- **Cost.** The CEA's cost is the scheme's estimated cost (for TBCB the
  bid-process estimate), not what Power Grid will book.

Writes `data/analysis/cases/power_grid/projects.json` (gitignored) and keeps
the PDFs in `sources/cea/` beside it. Nothing here reaches `prices`, a pillar
or the model's forecast; `power_model.py --add-projects` puts it on the
Assets sheet as a read-only block.

    python packages/book/power_projects.py --fetch          # latest month's three reports, then --build
    python packages/book/power_projects.py --build          # parse the stored PDFs -> projects.json
    python packages/book/power_projects.py --report         # the reconciliation
    python packages/book/power_projects.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import pathlib
import re
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
CASE = REPO / "data" / "analysis" / "cases" / "power_grid"
SRC = CASE / "sources" / "cea"
OUT = CASE / "projects.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# The listing page carries every report's download link (a UUID per file).
# The links a web search returns for these reports (wp-content/uploads/...,
# and old view-pdf UUIDs) were all 404 on 2026-11-13; only this page's
# current links work, so the fetch always starts here.
LIST_URL = ("https://cea.nic.in/reports/dashboardView?headingId=107&subHeadingId=1003"
            "&subSubHeadingId=10053&title=Transmission%20Schemes&lang=en")
DL_URL = "https://cea.nic.in/api/dashboardReports/download-pdf/en/{}"
AGENCY = "PGCIL"
KINDS = ("tbcb_uc", "rtm_uc", "tbcb_done")
MON = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                   "dec"), 1)}

# The five SPVs POWERGRID InvIT bought at its May-2021 IPO. They stay on the
# CEA's completed list under PGCIL but are NOT in the consolidated gross
# block, so the reconciliation leaves them out. Matched on scheme text, which
# is stable month to month; the serial number is not.
DIVESTED = (
    ("Vizag", r"Srikakulam PP\s*[–-]\s*Vemagiri"),
    ("Kala Amb", r"NRSS-XXXI \(Part-A\)|Kala ?amb"),
    ("Parli", r"Gadarwara.*Part-B"),
    ("Warora", r"Gadarwara.*Part-A"),
    ("Jabalpur", r"Vindhyachal-\s*V\b"),
)


# --------------------------------------------------------------------------
# small parsers
# --------------------------------------------------------------------------
def num(s) -> float | None:
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def month(s: str) -> dt.date | None:
    """'Oct - 26', 'Oct-26', 'Jan-2014' -> the first of that month."""
    m = re.search(r"([A-Za-z]{3})[a-z]*\s*[-’' ]\s*(\d{2,4})", s or "")
    if not m or m.group(1).lower() not in MON:
        return None
    y = int(m.group(2))
    return dt.date(y + 2000 if y < 100 else y, MON[m.group(1).lower()], 1)


def iso(s: str) -> str | None:
    d = month(s)
    return d.isoformat() if d else None


def quarter(d: dt.date | None) -> str | None:
    """Indian fiscal quarter label, the model's own: Apr-Jun = Q1, FY ends March."""
    if d is None:
        return None
    fy = d.year + 1 if d.month >= 4 else d.year
    return f"Q{(d.month - 4) % 12 // 3 + 1}FY{fy % 100:02d}"


def qkey(q: str) -> tuple[str, str]:
    """Sort key for 'Q3FY27': fiscal year, then quarter."""
    return q[4:], q[:2]


def months_between(a: dt.date | None, b: dt.date | None) -> int | None:
    return None if a is None or b is None else (b.year - a.year) * 12 + b.month - a.month


DATE = re.compile(r"(?:charged|commissioned|energi[sz]ed)\s*(?:on|w\.?e\.?f\.?)?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})",
                  re.I)


def charged(element: str, remark: str) -> tuple[float, list[dt.date]]:
    """(share of the element charged, the dates it was charged on).

    0 with no charge date. With dates: transformers count units ('2x1500 MVA'
    in the element, '1x1500 MVA ICT Charged' in the remark is half), circuits
    count circuits ('D/c' line, 'Ckt-1 Charged' alone is half), and a remark
    still saying 'balance' with nothing countable is half. Otherwise whole."""
    dates = []
    for d, m, y in DATE.findall(remark or ""):
        y = int(y)
        try:
            dates.append(dt.date(y + 2000 if y < 100 else y, int(m), int(d)))
        except ValueError:
            pass
    if not dates:
        return 0.0, []
    # every 'NxM MVA' in the element counts: Koppal-II's '2x1500 MVA, 400/220 kV,
    # 2x500 MVA' is four transformers, and '1x1500 ... 2x500 ... Charged' is three
    # of them. Reading only the first pattern called it 3 of 2, capped to whole.
    units = sum(int(n) for n in re.findall(r"(\d+)\s*[xX×]\s*\d+\s*MVA", element or ""))
    done_units = re.findall(r"(\d+)\s*[xX×]\s*\d+\s*MVA[^.]{0,40}?charged", remark or "", re.I)
    if units > 1 and done_units:
        return min(1.0, sum(int(k) for k in done_units) / units), dates
    ckts = set(re.findall(r"ckt[\s.#-]*(\d)[^.]{0,20}?charged", remark or "", re.I))
    if ckts and re.search(r"\bD/?c\b", element or "", re.I):
        return min(1.0, len(ckts) / 2), dates
    if re.search(r"balance|balnce|remaining|partial", remark or "", re.I):
        return 0.5, dates
    return 1.0, dates


def spv_of(name: str) -> str | None:
    m = re.search(r"SPV(?:\s*Name)?\s*[:-]?\s*([^)(,]+?(?:Limited|Ltd\.?|LTD|LIMITED))", name or "", re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


# --------------------------------------------------------------------------
# reading the reports
# --------------------------------------------------------------------------
def table_rows(pdf: pathlib.Path) -> list[list[str]]:
    import fitz
    out = []
    for page in fitz.open(pdf):
        for t in page.find_tables().tables:
            for r in t.extract():
                out.append([re.sub(r"\s+", " ", (c or "").replace("\n", " ")).strip() for c in r])
    return out


def summary_pgcil(rows: list[list[str]]) -> tuple[int, float]:
    """The report's own first-page summary row for PGCIL: (projects, cost)."""
    for r in rows:
        if r and r[0] == AGENCY and len(r) < 12:
            vals = [num(x) for x in r[1:] if num(x) is not None]
            return int(vals[0]), vals[-1]
    raise ValueError("no PGCIL summary row")


def parse_uc(rows: list[list[str]], route: str, as_of: str) -> list[dict]:
    """Under-construction projects (17-column table), PGCIL only."""
    out, cur = [], None
    for r in rows:
        if len(r) != 17:
            continue
        if r[0].isdigit():
            cur = None
            if r[2] != AGENCY:
                continue
            cur = {"route": route, "list": "under construction", "serial": int(r[0]), "name": r[1],
                   "spv": spv_of(r[1]), "cost_cr": num(r[5]), "spv_date": r[6] or None,
                   "target_original": iso(r[7]), "target_anticipated": iso(r[8]), "elements": [], "as_of": as_of}
            out.append(cur)
        elif cur is not None and r[1]:
            share, dates = charged(r[1], r[16])
            cur["elements"].append({"name": r[1], "ckm": num(r[3]), "mva": num(r[4]), "target_anticipated": iso(r[8]),
                                    "progress": {"civil": r[13] or None, "eqpt_received": r[14] or None,
                                                 "erection": r[15] or None},
                                    "share_charged": share, "charged_on": [d.isoformat() for d in dates],
                                    "remark": r[16] or None})
    return out


def parse_done(rows: list[list[str]], as_of: str) -> list[dict]:
    """Completed TBCB projects (10-column table), PGCIL only. Each element's
    'Actual COD' is a month; the project's is its own row's."""
    out, cur = [], None
    for r in rows:
        if len(r) != 10 or r[1].startswith("FY 20") or r[0].startswith("Sl"):
            continue
        if r[0].isdigit():
            cur = None
            if r[2] != AGENCY:
                continue
            cur = {"route": "TBCB", "list": "completed", "serial": int(r[0]), "name": r[1], "spv": spv_of(r[1]),
                   "cost_cr": num(r[5]), "target_original": iso(r[8]), "actual_cod": iso(r[9]), "elements": [],
                   "as_of": as_of}
            out.append(cur)
        elif cur is not None and r[1]:
            d = iso(r[9])
            cur["elements"].append({"name": r[1], "ckm": num(r[3]), "mva": num(r[4]), "share_charged": 1.0 if d else 0.0,
                                    "charged_on": [d] if d else []})
    return out


def finish(p: dict) -> dict:
    """Derived fields: status, capitalised (estimate), still to capitalise, delay, quarter."""
    els = p["elements"]
    if p["list"] == "completed":
        p["capitalised_est_cr"] = p["cost_cr"]
        p["status"] = "completed"
        p["completion"] = p.get("actual_cod")
        text = p["name"] + " " + " ".join(e["name"] for e in els)
        div = next((n for n, rx in DIVESTED if re.search(rx, text, re.I)), None)
        if div:
            p["status"] = "completed, divested to InvIT"
            p["divested"] = div
    else:
        share = sum(e["share_charged"] for e in els) / len(els) if els else 0.0
        p["capitalised_est_cr"] = round((p["cost_cr"] or 0) * share, 1)
        p["status"] = "part commissioned" if share > 0 else "under construction"
        p["completion"] = p.get("target_anticipated")
    p["to_capitalise_cr"] = round((p["cost_cr"] or 0) - p["capitalised_est_cr"], 1)
    p["elements_charged"] = round(sum(e["share_charged"] for e in els), 2)
    p["elements_total"] = len(els)
    end = dt.date.fromisoformat(p["completion"]) if p.get("completion") else None
    start = dt.date.fromisoformat(p["target_original"]) if p.get("target_original") else None
    p["delay_months"] = months_between(start, end)
    p["completion_quarter"] = quarter(end)
    return p


def capitalised_by_quarter(projects: list[dict], include_divested: bool = False) -> dict[str, float]:
    """The estimate, by the quarter each element was charged in: an element's
    share of its project's cost lands on its charge dates, split equally."""
    out: dict[str, float] = {}
    for p in projects:
        if p.get("divested") and not include_divested:
            continue
        n = len(p["elements"]) or 1
        for e in p["elements"]:
            ds = e["charged_on"]
            for d in ds:
                q = quarter(dt.date.fromisoformat(d))
                out[q] = out.get(q, 0.0) + (p["cost_cr"] or 0) / n * e["share_charged"] / len(ds)
    return {k: round(v, 1) for k, v in sorted(out.items(), key=lambda kv: qkey(kv[0]))}


def build(files: dict[str, pathlib.Path], month_label: str) -> dict:
    """Parse the three reports, check each against its own PGCIL summary row."""
    out, checks = [], []
    for kind in KINDS:
        rows = table_rows(files[kind])
        if kind == "tbcb_done":
            got = parse_done(rows, month_label)
            checks.append({"report": kind, "projects": len(got), "cost_cr": round(sum(p["cost_cr"] or 0 for p in got), 1),
                           "summary": "no per-agency summary row in this report"})
        else:
            got = parse_uc(rows, "TBCB" if kind == "tbcb_uc" else "RTM", month_label)
            n, cost = summary_pgcil(rows)
            pc = round(sum(p["cost_cr"] or 0 for p in got), 1)
            # the count must match exactly; the cost within half a crore per
            # project, because the report prints each cost rounded and totals the
            # unrounded figures (September 2026 TBCB: 1,59,348 parsed, 1,59,349 printed)
            if len(got) != n or abs(pc - cost) > 0.5 * n:
                raise SystemExit(f"{kind}: parsed {len(got)} projects / Rs{pc:,.0f}cr against the report's own PGCIL row "
                                 f"{n} / Rs{cost:,.0f}cr; refusing a partial list")
            checks.append({"report": kind, "projects": len(got), "cost_cr": pc,
                           "summary": f"report says {n} / Rs{cost:,.0f}cr, matches"})
        out += [finish(p) for p in got]
    return {"as_of": month_label, "source": "CEA monthly progress reports, Transmission Schemes (cea.nic.in)",
            "files": {k: v.name for k, v in files.items()}, "checks": checks, "projects": out,
            "capitalised_by_quarter_est": capitalised_by_quarter(out)}


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://cea.nic.in/"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def listing(page: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<tr class="report-item"\s+data-date="([\d-]+)".*?</tr>', page, re.S):
        row = m.group(0)
        name = re.search(r'report-name-cell">(.*?)</td>', row, re.S)
        uid = re.search(r"/api/dashboardReports/download-pdf/en/([0-9a-f-]{36})", row)
        if name and uid:
            out.append({"month": m.group(1), "name": html.unescape(re.sub(r"\s+", " ", name.group(1))).strip(),
                        "uuid": uid.group(1)})
    return out


def kind_of(name: str) -> str | None:
    """The completed TBCB report is titled with or without '(Completed
    Projects)' depending on the month (September 2026: without)."""
    n = name.lower()
    if "tariff based competitive bidding" in n:
        return "tbcb_uc" if "under construction" in n else "tbcb_done"
    if "regulated tariff mechanism" in n and "under construction" in n:
        return "rtm_uc"
    return None


def latest_complete(items: list[dict]) -> tuple[str, dict[str, str]]:
    """The newest month that has all three reports."""
    by: dict[str, dict[str, str]] = {}
    for it in items:
        k = kind_of(it["name"])
        if k:
            by.setdefault(it["month"], {}).setdefault(k, it["uuid"])
    for m in sorted(by, reverse=True):
        if len(by[m]) == 3:
            return m, by[m]
    raise SystemExit("no month on the CEA page carries all three reports")


def fetch() -> tuple[str, dict[str, pathlib.Path]]:
    m, ids = latest_complete(listing(_get(LIST_URL).decode("utf-8", "replace")))
    SRC.mkdir(parents=True, exist_ok=True)
    files = {}
    for k, uid in ids.items():
        p = SRC / f"{k}_{m}.pdf"
        if not p.exists():
            b = _get(DL_URL.format(uid))
            if not b.startswith(b"%PDF"):
                raise SystemExit(f"{k} {m}: the download is not a PDF ({len(b)} bytes)")
            p.write_bytes(b)
        files[k] = p
    return m, files


def local(month_label: str | None = None) -> tuple[str, dict[str, pathlib.Path]]:
    months = sorted({p.stem.rsplit("_", 1)[1] for p in SRC.glob("*_*.pdf")}, reverse=True)
    for m in ([month_label] if month_label else months):
        files = {k: SRC / f"{k}_{m}.pdf" for k in KINDS}
        if all(p.exists() for p in files.values()):
            return m, files
    raise SystemExit(f"no complete set of CEA reports in {SRC}; run --fetch")


# --------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------
def reconcile(data: dict, cfg: dict) -> dict:
    """Everything the PM asked to have reported back, from projects.json and
    the `projects_reconcile` block of model_config.json (the company's own
    totals, each sourced)."""
    P = [p for p in data["projects"] if not p.get("divested")]
    R = cfg.get("projects_reconcile") or {}
    tb_uc = [p for p in P if p["route"] == "TBCB" and p["list"] != "completed"]
    rtm_uc = [p for p in P if p["route"] == "RTM"]
    done = [p for p in P if p["list"] == "completed"]
    s = lambda ps, k: round(sum(p.get(k) or 0 for p in ps), 1)
    out = {"as_of": data["as_of"]}
    # 1. in service: what the TBCB projects say is built, against the TBCB part of the gross block
    g_c, g_s = (R.get("gfa_consolidated") or {}).get("value"), (R.get("gfa_standalone") or {}).get("value")
    in_service = round(s(done, "cost_cr") + s(tb_uc, "capitalised_est_cr"), 1)
    out["tbcb_block"] = {"consolidated_less_standalone": None if None in (g_c, g_s) else round(g_c - g_s, 1),
                         "completed_projects_cost": s(done, "cost_cr"), "completed_projects": len(done),
                         "part_commissioned_est": s(tb_uc, "capitalised_est_cr"), "listed_in_service": in_service}
    if None not in (g_c, g_s):
        out["tbcb_block"]["gap"] = round(g_c - g_s - in_service, 1)
    # 2. works in hand: what the company says is unfinished, against what the CEA lists
    wih = R.get("works_in_hand") or {}
    out["works_in_hand"] = {}
    for route, ps in (("TBCB", tb_uc), ("RTM", rtm_uc)):
        listed = s(ps, "to_capitalise_cr")
        w = (wih.get(route) or {}).get("value")
        out["works_in_hand"][route] = {"company": w, "listed_cost": s(ps, "cost_cr"), "listed_projects": len(ps),
                                       "listed_to_capitalise": listed,
                                       "leftover": None if w is None else round(w - listed, 1)}
    # 3. history: the element-dated estimate against the capitalisation the company reported
    est = capitalised_by_quarter(P)
    rep = (cfg.get("consolidated") or {}).get("capitalisation") or {}
    out["history"] = [{"quarter": q, "reported": v, "listed_est": est.get(q, 0.0)}
                      for q, v in sorted(rep.items(), key=lambda kv: qkey(kv[0]))]
    # 4. forward: what the listed projects still have to capitalise, by anticipated quarter
    fwd: dict[str, float] = {}
    for p in tb_uc + rtm_uc:
        q = p["completion_quarter"] or "undated"
        fwd[q] = fwd.get(q, 0.0) + p["to_capitalise_cr"]
    out["forward_by_quarter"] = {k: round(v, 1) for k, v in sorted(fwd.items(), key=lambda kv: qkey(kv[0]))}
    # 5. spillover: first due in one fiscal year, now anticipated in a later one
    spill = []
    for p in tb_uc + rtm_uc:
        o, a = p.get("target_original"), p.get("completion")
        if not (o and a):
            continue
        fo, fa = quarter(dt.date.fromisoformat(o))[2:], quarter(dt.date.fromisoformat(a))[2:]
        if fa > fo:
            spill.append({"name": p["name"], "route": p["route"], "from": fo, "to": fa, "cost_cr": p["cost_cr"],
                          "to_capitalise_cr": p["to_capitalise_cr"], "delay_months": p["delay_months"]})
    out["spillover"] = sorted(spill, key=lambda x: -x["to_capitalise_cr"])
    return out


def report(rec: dict) -> str:
    f = lambda v: "n/a" if v is None else f"{v:,.0f}"
    b = rec["tbcb_block"]
    L = [f"Power Grid projects, CEA reports for {rec['as_of']} (Rs crore)", "",
         "In service: the TBCB part of the consolidated gross block",
         f"  consolidated less standalone gross block   {f(b['consolidated_less_standalone']):>10}",
         f"  {b['completed_projects']} completed TBCB projects, CEA cost     {f(b['completed_projects_cost']):>10}",
         f"  part-commissioned projects, estimated      {f(b['part_commissioned_est']):>10}",
         f"  gap                                        {f(b.get('gap')):>10}", "",
         "Works in hand against the CEA's lists"]
    for route, w in rec["works_in_hand"].items():
        L.append(f"  {route:<5} company {f(w['company']):>9}  CEA lists {w['listed_projects']} projects, "
                 f"{f(w['listed_to_capitalise'])} still to capitalise  leftover {f(w['leftover'])}")
    L += ["", "Capitalisation by quarter: reported, and the listed projects' element-dated estimate"]
    for h in rec["history"]:
        L.append(f"  {h['quarter']}  reported {f(h['reported']):>8}  listed est. {f(h['listed_est']):>8}")
    L += ["", "Still to capitalise, by anticipated completion quarter"]
    for q, v in rec["forward_by_quarter"].items():
        L.append(f"  {q:<7} {f(v):>9}")
    sp = rec["spillover"]
    L += ["", f"Spillover: {len(sp)} projects now anticipated in a later fiscal year than first scheduled, "
              f"{f(sum(x['to_capitalise_cr'] for x in sp))} still to capitalise"]
    for x in sp[:20]:
        L.append(f"  {x['route']:<4} {x['from']}->{x['to']}  {f(x['to_capitalise_cr']):>7}  +{x['delay_months']}m  {x['name'][:78]}")
    return "\n".join(L)


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def selftest() -> int:
    fails = 0

    def chk(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": got {got!r}, want {want!r}"))

    chk("month: 'Oct - 26'", month("Oct - 26"), dt.date(2026, 10, 1))
    chk("month: 'Jan-2014'", month("Jan-2014"), dt.date(2014, 1, 1))
    chk("month: blank is None", month(""), None)
    chk("quarter: Oct-26 is Q3FY27", quarter(dt.date(2026, 10, 1)), "Q3FY27")
    chk("quarter: Mar-27 is Q4FY27", quarter(dt.date(2027, 3, 1)), "Q4FY27")
    chk("quarter: Apr-27 is Q1FY28", quarter(dt.date(2027, 4, 1)), "Q1FY28")
    chk("no charge date, nothing charged", charged("Raichur 765 kV D/c line", "Stringing RoW (2.7 km)")[0], 0.0)
    chk("a line charged whole", charged("Koppal-II PS - Raichur 765 kV D/c line", "Tx. Line charged on 30.07.2026"),
        (1.0, [dt.date(2026, 7, 30)]))
    chk("one transformer of two is half",
        charged("Augmentation by 2x1500 MVA, 765/400 kV ICTs", "1x1500 MVA ICT Charged on 22.09.2026")[0], 0.5)
    chk("two kinds of transformer in one element count together (3 of 4)",
        charged("Establishment of 765/400 kV 2x1500 MVA, 400/220 kV, 2x500 MVA Koppal-II",
                "1x1500 MVA ICT Charged on 31.07.2026. 2x500 MVA ICT Charged on 23.09.2026 Balnce work under progress.")[0],
        0.75)
    chk("both transformers on two dates is whole",
        charged("400/220 kV, 2x500 MVA Gadag-II", "1x500 MVA ICT Charged on 02.08.2026 1x500 MVA ICT Charged on "
                "27.09.2026 Balnce work under progress.")[0], 1.0)
    chk("one circuit of a D/c line is half", charged("Gadag-II - Koppal-II 400 kV D/c line", "Ckt-2 Charged on 31.07.2026")[0],
        0.5)
    chk("both circuits is whole",
        charged("Gadag-II - Koppal-II 400 kV D/c line", "Ckt-2 Charged on 31.07.2026. Ckt-1 Charged on 02.08.2026")[0], 1.0)
    chk("'balance' with nothing countable is half",
        charged("Bidar substation", "Charged on 01.08.2026; balance under progress")[0], 0.5)
    chk("SPV name read", spv_of("Scheme X, (SPV: POWERGRID Vataman Transmission Ltd)"), "POWERGRID Vataman Transmission Ltd")
    # a fixture shaped like the reports: a PGCIL project and a private one, elements under each
    hdr = ["1", "Scheme A (SPV: POWERGRID Alpha Transmission Ltd)", "PGCIL", "", "", "1,000", "Dec-23", "Dec-25",
           "Oct - 26"] + [""] * 8
    e1 = ["", "Alpha 765 kV D/c line", "PGCIL", "200", "", "", "Dec-23", "Dec-25", "Jul - 26", "", "", "", "", "", "", "",
          "Tx. Line charged on 30.07.2026"]
    e2 = ["", "2x1500 MVA Alpha PS", "PGCIL", "", "3000", "", "Dec-23", "Dec-25", "Oct - 26", "", "", "", "", "90%", "", "",
          "Work under progress"]
    oth = ["2", "Scheme B", "Adani", "", "", "5,000", "Dec-23", "Dec-25", "Dec - 26"] + [""] * 8
    oth_e = ["", "Beta line", "Adani", "10", "", "", "", "", "Dec - 26", "", "", "", "", "", "", "", "charged on 01.01.2026"]
    got = [finish(p) for p in parse_uc([hdr, e1, e2, oth, oth_e], "TBCB", "2026-09")]
    chk("only PGCIL's projects are kept", [p["name"][:8] for p in got], ["Scheme A"])
    p = got[0]
    chk("a private project's elements do not attach to PGCIL's", p["elements_total"], 2)
    chk("capitalised = cost x charged share (one of two elements)", p["capitalised_est_cr"], 500.0)
    chk("still to capitalise", p["to_capitalise_cr"], 500.0)
    chk("status", p["status"], "part commissioned")
    chk("delay in months", p["delay_months"], 10)
    chk("completion quarter", p["completion_quarter"], "Q3FY27")
    chk("estimate lands in the quarter the element was charged", capitalised_by_quarter(got), {"Q2FY27": 500.0})
    chk("the report's own PGCIL row reads", summary_pgcil([["PGCIL", "1", "200", "3,000", "", "", "1,000", "", ""]]),
        (1, 1000.0))
    done = [["1", "Transmission system for Strengthening in Southern Region", "PGCIL", "", "", "1,180", "AP", "",
             "Aug-2016", "Sep - 16"],
            ["", "Srikakulam PP – Vemagiri-II Pooling Station 765kV D/c line", "PGCIL", "1,336", "", "", "", "", "",
             "Sep - 16"],
            ["2", "Scheme C", "PGCIL", "", "", "300", "", "", "Jan-2025", "Mar - 25"],
            ["", "C line", "PGCIL", "100", "", "", "", "", "", "Mar - 25"]]
    dn = [finish(p) for p in parse_done(done, "2026-09")]
    chk("a divested SPV is named", (dn[0]["status"], dn[0].get("divested")), ("completed, divested to InvIT", "Vizag"))
    chk("a completed project is fully capitalised", (dn[1]["capitalised_est_cr"], dn[1]["to_capitalise_cr"]), (300.0, 0.0))
    chk("a divested project adds nothing to the quarterly estimate", capitalised_by_quarter(dn), {"Q4FY25": 300.0})
    cfg = {"projects_reconcile": {"gfa_consolidated": {"value": 2000}, "gfa_standalone": {"value": 1000},
                                  "works_in_hand": {"TBCB": {"value": 900}}},
           "consolidated": {"capitalisation": {"Q2FY27": 600}}}
    rec = reconcile({"as_of": "2026-09", "projects": got + dn}, cfg)
    chk("in service excludes the divested SPV", rec["tbcb_block"]["listed_in_service"], 800.0)
    chk("the in-service gap", rec["tbcb_block"]["gap"], 200.0)
    chk("works-in-hand leftover", rec["works_in_hand"]["TBCB"]["leftover"], 400.0)
    chk("history pairs reported with the estimate", rec["history"], [{"quarter": "Q2FY27", "reported": 600, "listed_est": 500.0}])
    chk("a project slipping a fiscal year is spillover", [(x["from"], x["to"]) for x in rec["spillover"]], [("FY26", "FY27")])
    items = [{"month": "2026-09", "name": "Monthly Progress Report of Transmission Projects awarded through Tariff Based "
                                           "Competitive Bidding Route (Under Construction Projects)", "uuid": "a"},
             {"month": "2026-09", "name": "Monthly Progress Report of Transmission Projects awarded through Tariff Based "
                                           "Competitive Bidding Route", "uuid": "b"},
             {"month": "2026-08", "name": "Monthly Progress Report of Transmission Projects Awarded through Regulated "
                                           "Tariff Mechanism (Under Construction Projects)", "uuid": "c"},
             {"month": "2026-09", "name": "Monthly Progress Report of Central Funded Schemes", "uuid": "d"}]
    try:
        latest_complete(items)
        chk("a month missing a report is refused", "accepted", "refused")
    except SystemExit:
        chk("a month missing a report is refused", "refused", "refused")
    items.append({"month": "2026-09", "name": items[2]["name"], "uuid": "e"})
    chk("the newest complete month is chosen", latest_complete(items),
        ("2026-09", {"tbcb_uc": "a", "tbcb_done": "b", "rtm_uc": "e"}))
    print(f"{'PASS' if not fails else 'FAIL'} ({fails} failures)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="download the latest month's three reports, then build")
    ap.add_argument("--build", action="store_true", help="parse the stored reports into projects.json")
    ap.add_argument("--month", help="YYYY-MM, a stored month (default: the newest complete one)")
    ap.add_argument("--report", action="store_true", help="print the reconciliation")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.fetch:
        m, files = fetch()
        print(f"CEA reports for {m}: " + ", ".join(p.name for p in files.values()))
    if a.build or a.fetch:
        m, files = local(a.month)
        data = build(files, m)
        OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        for c in data["checks"]:
            print(f"  {c['report']:<10} {c['projects']:>3} projects  Rs{c['cost_cr']:>10,.0f}cr  {c['summary']}")
        print(f"wrote {OUT}")
    if a.report:
        data = json.loads(OUT.read_text(encoding="utf-8"))
        cfg = json.loads((CASE / "model_config.json").read_text(encoding="utf-8"))
        print(report(reconcile(data, cfg)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
