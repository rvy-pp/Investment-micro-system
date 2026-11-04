"""A quarterly steel model in Excel: segments -> P&L -> cash -> EV/EBITDA -> price.

PM, 2026-11-02, after reading JPM's JSW model: *"we will construct an excel
of our own, create a basic model for quarters which can be updated on a
regular basis ... We can use it in other steel models as well."* The plan:
revenue and cost per region from sales and production, capex from concalls
to forecast free cash flow, EBITDA and PAT, net debt from the cash flows,
EV/EBITDA, an expected price against CMP.

This script BUILDS the workbook once, from a per-company config
(`data/analysis/cases/<entity>/model_config.json`, gitignored like every
case file). After that the workbook is the working surface: a new quarter is
rolled by typing its actuals into the blue input rows and flipping its status
cell on Inputs from F to A. Every model row reads `IF(status="A", actual,
forecast)`, so nothing else moves. Re-running the build OVERWRITES the PM's
edits, which is why `--out` refuses an existing file without `--force`.

THE SHAPE, and what each part deliberately leaves out:

- ONE primary segment (India) gets full drivers: capacity x utilisation ->
  production -> sales; NSR/t moves with HRC; cost/t moves with coking coal and
  iron ore through the SAME intensities, market shares and pass-throughs as
  `specs/entities/steel.yaml`, so the bridge and the Excel cannot disagree
  about one company. Lags are inputs (coal ~1 quarter of inventory).
- Overseas segments are EBITDA in their own currency x FX, rolled at the
  trailing four quarters. They are small and noisy; a driver build there would
  be precision the data does not have.
- A JV is equity-accounted below EBITDA and valued as a stake, never inside
  EBITDA (JPM's JSW model still carried JSW JFE at 100% in its subsidiary
  table, which is the error this avoids).
- The PM's two numbers for the visible quarter (sales, EBITDA/t) sit in
  yellow override rows. The next quarter rolls from the overridden cost, so a
  view carries forward instead of snapping back to the mechanical path.
- Commodities hold the CURRENT QUARTER'S Kotak metals-pack average to date
  unless overridden (PM, 2026-11-03), the basis of an actual quarter, so a
  running quarter converges on its actual. The model forecasts no price.
  `--refresh-deck <entity>` re-reads the store into an existing workbook.
- D&A scales with effective India capacity (capex -> commissioning -> D&A);
  finance cost is a rate on opening gross debt, so there is no circularity.

The valuation sheet is for the PM's eye. The system's juice stays with
`case_brief.py` (anchor = median house multiple, σ range); the two must agree,
and a reader that checks it is the next step, not this one.

    python packages/book/steel_model.py --config data/analysis/cases/jsw_steel/model_config.json \\
        --out data/analysis/cases/jsw_steel/JSW_Steel_model.xlsx
    python packages/book/steel_model.py --selftest

openpyxl writes formulas without cached values; open the file in Excel (or
run `--recalc`, which drives Excel over COM) before anything reads it with
`data_only=True`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
import statistics
import subprocess
import tempfile
import sys

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName

REPO = pathlib.Path(__file__).resolve().parents[2]
DB = REPO / "data" / "ims.db"

# metals-pack series the India drivers read; key -> (prices.entity_id, unit)
COMMODITIES = {
    "hrc": ("hrc_india_inr", "Rs/t"),
    "cc": ("coking_coal_spot_aus", "USD/t"),
    "io": ("iron_ore_china_cfr62", "USD/t"),
    "usdinr": ("usdinr", "INR/USD"),
    # NMDC's notified fines price, ex-royalty (mining_filings; basis break at
    # 2026-01-09, nothing earlier is loaded). It drives the ore cost line
    # (PM, 2026-11-03): seaborne x 0.50 got Q1FY27 backwards (NMDC +18%,
    # seaborne -2%) and could not see an NMDC hike at all.
    "nmdc": ("nmdc_fines_inr", "Rs/t"),
}
# Step series: stored only on the date a circular changes the price, so a
# quarter's average is TIME-WEIGHTED (the price in force each day), never the
# mean of the circular dates - three circulars in one month and one in the
# next would otherwise weight the first month three times.
STEP = {"nmdc"}

FONT = "Arial"
BLUE, GREEN, BLACK = "0000FF", "008000", "000000"
YELLOW = PatternFill("solid", fgColor="FFFF00")
HEAD = PatternFill("solid", fgColor="1F3864")
ACT = PatternFill("solid", fgColor="D9D9D9")
SEC = PatternFill("solid", fgColor="DDEBF7")
THIN = Border(bottom=Side(style="thin", color="808080"))

FMT = {
    "cr": '#,##0;(#,##0);"-"',
    "mt": '0.00;(0.00);"-"',
    "t": '#,##0;(#,##0);"-"',
    "mn": '#,##0.0;(#,##0.0);"-"',
    "pct": '0.0%;(0.0%);"-"',
    "x": '0.0"x"',
    "x2": '0.00"x"',
    "fx": "0.00",
    "rs": '#,##0.00;(#,##0.00);"-"',
}

FIRST_COL = 4                       # quarters start in column D

# Texts --refresh-deck finds again in a built workbook, so they live once.
DECK_TITLE = ("Commodity deck: actual quarters are Kotak metals-pack averages; forecasts hold the CURRENT "
              "quarter's average to date unless overridden")
README_DECK = ("Commodities hold the current quarter's Kotak metals-pack weekday average to date in every "
               "forecast quarter unless you type an override (PM, 2026-11-03; it was the latest single print "
               "until then), so a running quarter converges on its actual. NMDC holds the price in force. "
               "The model forecasts no price.")
README_FWD_HEAD = "Prices held for forecasts"


# --------------------------------------------------------------------------
# quarters
# --------------------------------------------------------------------------
def quarters(first: str, last: str) -> list[str]:
    """'Q3FY25'..'Q4FY29' inclusive, Indian fiscal quarters."""
    q, y = int(first[1]), int(first[4:])
    out = []
    while True:
        lab = f"Q{q}FY{y:02d}"
        out.append(lab)
        if lab == last:
            return out
        if len(out) > 80:
            raise ValueError(f"{last!r} never reached from {first!r}")
        q, y = (1, y + 1) if q == 4 else (q + 1, y)


def fy_of(label: str) -> str:
    return "FY" + label[4:]


def quarter_dates(label: str) -> tuple[str, str]:
    """Calendar span of an Indian fiscal quarter: Q1 = Apr-Jun of FY-1."""
    q, fy = int(label[1]), 2000 + int(label[4:])
    year = fy - 1 if q <= 3 else fy
    start_m = {1: 4, 2: 7, 3: 10, 4: 1}[q]
    start = dt.date(year, start_m, 1)
    end_m = start_m + 2
    end = (dt.date(year + (end_m // 12), end_m % 12 + 1, 1) - dt.timedelta(days=1))
    return start.isoformat(), end.isoformat()


def commodity_history(qs: list[str], db: pathlib.Path = DB) -> dict:
    """Weekday mean per quarter and the latest close, per COMMODITIES series.
    Weekday only: the metals pack forward-fills weekends, which would weight a
    quarter towards whatever printed on Fridays."""
    con = sqlite3.connect(db)
    out = {}
    for key, (sid, _) in COMMODITIES.items():
        per = {}
        for lab in qs:
            a, b = quarter_dates(lab)
            if key in STEP:
                pts = con.execute("SELECT date, close FROM prices WHERE entity_id=? AND date<=? "
                                  "ORDER BY date", (sid, b)).fetchall()
                d, end, daily = dt.date.fromisoformat(a), dt.date.fromisoformat(b), []
                while d <= end:
                    live = [c for t, c in pts if t <= d.isoformat()]
                    if live:
                        daily.append(live[-1])
                    d += dt.timedelta(days=1)
                if daily:                       # days before the first circular carry no price
                    per[lab] = round(statistics.mean(daily), 2)
                continue
            v = [r[0] for r in con.execute(
                "SELECT close FROM prices WHERE entity_id=? AND date BETWEEN ? AND ? "
                "AND strftime('%w',date) NOT IN ('0','6')", (sid, a, b))]
            if v:
                per[lab] = round(statistics.mean(v), 2)
        last = con.execute("SELECT date, close FROM prices WHERE entity_id=? "
                           "ORDER BY date DESC LIMIT 1", (sid,)).fetchone()
        if last is None:
            continue
        out[key] = {"q": per, "spot": last[1], "spot_date": last[0]}
        # What forecasts hold (PM, 2026-11-03: "how about regular HRC price
        # average? The one we get from kotak?"): the weekday average of the
        # quarter the latest print sits in, on the same basis as an actual
        # quarter, so a quarter's forecast converges on its actual as it runs
        # instead of jumping when the status flips. One day's print had been
        # the forecast; HRC's 63,600 on 3-Nov sat below the 63,976 average
        # since 1-Oct. A step series (NMDC) keeps the price in force: a
        # notified price IS the forward price until the next circular.
        cur = current_quarter(last[0])
        if key in STEP:
            fwd, n = last[1], None
        else:
            a, _ = quarter_dates(cur)
            v = [r[0] for r in con.execute(
                "SELECT close FROM prices WHERE entity_id=? AND date BETWEEN ? AND ? "
                "AND strftime('%w',date) NOT IN ('0','6')", (sid, a, last[0]))]
            fwd, n = (round(statistics.mean(v), 2), len(v)) if v else (last[1], 0)
        out[key].update(fwd=fwd, fwd_q=cur, fwd_days=n)
    con.close()
    return out


def current_quarter(day: str) -> str:
    """Indian fiscal quarter containing an ISO date."""
    d = dt.date.fromisoformat(day)
    q = {1: 4, 2: 4, 3: 4, 4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3}[d.month]
    fy = d.year + (1 if d.month >= 4 else 0)
    return f"Q{q}FY{fy % 100:02d}"


def fwd_note(h: dict, src: str = "Kotak metals pack") -> str:
    if h.get("fwd_days") is None:
        return f"Price in force: circular of {h['spot_date']}"
    return (f"{src} weekday average, {h['fwd_q']} to {h['spot_date']} ({h['fwd_days']} days); "
            f"latest print {h['spot']:,}")


# --------------------------------------------------------------------------
# config cross-foot: the transcription guard
# --------------------------------------------------------------------------
def crossfoot(cfg: dict, tol: float = 3.0) -> list[str]:
    """Every actual quarter must foot: PBT from its components, PAT = PBT - tax.
    A mistyped deck number shows up here, not as a plausible wrong price."""
    c = cfg["consolidated"]
    errs = []
    for q in c["pbt"]:
        pbt = (c["reported_ebitda"][q] + c["other_income"][q] - c["finance_cost"][q]
               - c["depreciation"][q] + c["jv_share"][q] + c["exceptional"][q])
        if abs(pbt - c["pbt"][q]) > tol:
            errs.append(f"{q}: PBT components {pbt:,.0f} vs reported {c['pbt'][q]:,.0f}")
        if abs(c["pbt"][q] - c["tax"][q] - c["pat"][q]) > tol:
            errs.append(f"{q}: PBT - tax != PAT")
        if q not in cfg["india"]["adj_ebitda_cr"]:
            errs.append(f"{q}: no India EBITDA")
    for k, v in cfg["india"].items():
        if set(v) != set(c["pbt"]):
            errs.append(f"india.{k}: quarters differ from consolidated")
    return errs


# --------------------------------------------------------------------------
# workbook helpers
# --------------------------------------------------------------------------
class Sheet:
    """A period sheet: labels in A, unit in B, note in C, quarters from D."""

    def __init__(self, wb, name, qs, title, sub):
        self.ws = wb.create_sheet(name)
        self.name, self.qs, self.r = name, qs, 1
        ws = self.ws
        ws["A1"] = title
        ws["A1"].font = Font(name=FONT, bold=True, size=13)
        ws["A2"] = sub
        ws["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
        ws.column_dimensions["A"].width = 46
        ws.column_dimensions["B"].width = 9
        ws.column_dimensions["C"].width = 12
        for i in range(len(qs)):
            ws.column_dimensions[col(i)].width = 10.5
        self.r = 4

    def ref(self, row, i, absolute=False, sheet=True):
        c = f"${col(i)}${row}" if absolute else f"{col(i)}{row}"
        return f"'{self.name}'!{c}" if sheet else c

    def header(self, status_from_inputs=True):
        ws = self.ws
        for r, lab in ((4, "Quarter"), (5, "Fiscal year"), (6, "Status (A actual / F forecast)")):
            ws.cell(r, 1, lab).font = Font(name=FONT, bold=True, color="FFFFFF")
            for cc in range(1, 4):
                ws.cell(r, cc).fill = HEAD
        for i, q in enumerate(self.qs):
            for r in (4, 5, 6):
                cell = ws.cell(r, FIRST_COL + i)
                cell.fill = HEAD
                cell.alignment = Alignment(horizontal="center")
            ws.cell(4, FIRST_COL + i, q).font = Font(name=FONT, bold=True, color="FFFFFF")
            ws.cell(5, FIRST_COL + i, fy_of(q)).font = Font(name=FONT, color="FFFFFF")
            if status_from_inputs:
                ws.cell(6, FIRST_COL + i, f"=Inputs!{col(i)}6").font = Font(name=FONT, bold=True, color="FFFFFF")
        ws.freeze_panes = "D7"
        self.r = 8

    def section(self, text):
        ws = self.ws
        self.r += 1
        ws.cell(self.r, 1, text).font = Font(name=FONT, bold=True)
        for c in range(1, FIRST_COL + len(self.qs)):
            ws.cell(self.r, c).fill = SEC
        self.r += 1

    def row(self, label, unit, values, fmt="cr", note=None, bold=False, kind="formula",
            fill=None, comment=None):
        """values: dict q->value (inputs), or callable(i, q, r) -> formula/value/None."""
        ws, r = self.ws, self.r
        ws.cell(r, 1, label).font = Font(name=FONT, bold=bold)
        ws.cell(r, 2, unit).font = Font(name=FONT, size=9, color="595959")
        if note:
            ws.cell(r, 3, note).font = Font(name=FONT, size=8, italic=True, color="595959")
        for i, q in enumerate(self.qs):
            v = values(i, q, r) if callable(values) else values.get(q)
            if v is None:
                continue
            cell = ws.cell(r, FIRST_COL + i, v)
            is_formula = isinstance(v, str) and v.startswith("=")
            if not is_formula:
                color = BLUE
            elif "!" in v and kind != "local":
                color = GREEN
            else:
                color = BLACK
            cell.font = Font(name=FONT, bold=bold, color=color)
            cell.number_format = FMT[fmt]
            if fill is not None and fill(i, q):
                cell.fill = YELLOW
        if comment:
            ws.cell(r, 1).comment = Comment(comment, "steel_model")
        self.r += 1
        return r


def col(i: int) -> str:
    return get_column_letter(FIRST_COL + i)


def name(wb, nm, ref):
    wb.defined_names[nm] = DefinedName(nm, attr_text=ref)


def prev(i, row, sheet=None):
    """Reference to the previous quarter's cell on `row`."""
    c = f"{col(i - 1)}{row}"
    return f"'{sheet}'!{c}" if sheet else c


def trailing(i, row, n=4):
    return f"=AVERAGE({col(i - n)}{row}:{col(i - 1)}{row})"


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def build(cfg: dict, out: pathlib.Path, hist: dict) -> dict:
    errs = crossfoot(cfg)
    if errs:
        raise SystemExit("config does not cross-foot:\n  " + "\n  ".join(errs))
    qs = quarters(cfg["first_quarter"], cfg["last_quarter"])
    n_act = qs.index(cfg["actual_through"]) + 1
    act = set(qs[:n_act])
    N = len(qs)
    last = col(N - 1)
    wb = Workbook()
    wb.remove(wb.active)

    def A(i):  # status test for column i, written against the Inputs status row
        return f'Inputs!{col(i)}$6="A"'

    # ---------------------------------------------------------------- README
    rd = wb.create_sheet("README")
    rd.column_dimensions["A"].width = 120
    lines = readme_lines(cfg, qs, n_act, hist)
    for k, (txt, style) in enumerate(lines, start=1):
        c = rd.cell(k, 1, txt)
        c.font = Font(name=FONT, bold=style == "h", size=13 if style == "t" else 10,
                      color=BLUE if style == "blue" else BLACK)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        if style == "yellow":
            c.fill = YELLOW

    # ---------------------------------------------------------------- Inputs
    inp = Sheet(wb, "Inputs", qs, f"{cfg['company']} - inputs and assumptions",
                "Blue = typed input. Black = formula. Green = link to another sheet. "
                "Yellow = key assumption or the PM's override. Rs crore unless stated.")
    ws = inp.ws
    inp.header(status_from_inputs=False)
    for i, q in enumerate(qs):
        c = ws.cell(6, FIRST_COL + i, "A" if q in act else "F")
        c.font = Font(name=FONT, bold=True, color="FFFF00")
    ws.cell(6, 3, "<- flip F to A").font = Font(name=FONT, size=8, color="FFFFFF")
    ws.cell(7, 1, "Quarter index").font = Font(name=FONT, size=9, color="595959")
    for i in range(N):
        ws.cell(7, FIRST_COL + i, i + 1).font = Font(name=FONT, size=9, color="595959")
    name(wb, "Qrow", f"Inputs!$D$4:${last}$4")
    name(wb, "FYrow", f"Inputs!$D$5:${last}$5")
    name(wb, "Status", f"Inputs!$D$6:${last}$6")
    name(wb, "Qidx", f"Inputs!$D$7:${last}$7")
    inp.r = 8
    inp.row("Basis of the actuals", "", {q: cfg["basis"].get(q) for q in qs if q in act}, fmt="cr")
    for i, q in enumerate(qs):
        cell = ws.cell(8, FIRST_COL + i)
        cell.font = Font(name=FONT, size=7, color=BLUE)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[8].height = 60

    # constants: A label, B unit, C value, D source
    inp.section("Company constants")
    const_rows = {}

    def const(key, label, entry, fmt, yellow=False):
        r = inp.r
        ws.cell(r, 1, label).font = Font(name=FONT)
        ws.cell(r, 2, entry.get("unit", "")).font = Font(name=FONT, size=9, color="595959")
        c = ws.cell(r, 3, entry["value"])
        c.font = Font(name=FONT, color=BLUE)
        c.number_format = FMT[fmt] if fmt else "General"
        if yellow:
            c.fill = YELLOW
        ws.cell(r, 4, entry.get("source", "")).font = Font(name=FONT, size=8, italic=True, color="595959")
        name(wb, key, f"Inputs!$C${r}")
        const_rows[key] = r
        inp.r += 1

    C = cfg["constants"]
    const("Shares_mn", "Shares outstanding", C["shares_mn"], "mn")
    const("CMP", "Current market price", C["cmp"], "rs")
    const("CMP_date", "Price date", C["cmp_date"], None)
    const("India_cap", "India steelmaking capacity today", C["india_capacity_mtpa"], "mt")
    const("Tax_rate", "Effective tax rate (ex JV share)", C["tax_rate"], "pct", yellow=True)
    const("Int_rate", "Interest rate on opening gross debt", C["int_rate"], "pct", yellow=True)
    const("JV_stake", "Value of the JV stake", C["jv_stake_cr"], "cr")
    o = cfg["opening_net_debt"]
    const("Open_ND", "Net debt before the first quarter", {"value": o["value"], "unit": "Rs cr",
                                                           "source": f"{o['as_of']}: {o['source']}"}, "cr")
    const("Guide_sales", "FY guidance: consolidated sales", C["fy27_sales_guide_mt"], "mt")
    const("Guide_prod", "FY guidance: consolidated production", C["fy27_prod_guide_mt"], "mt")

    inp.section("Driver sensitivities (India)")
    S = cfg["sensitivities"]
    for key, label, fmt, y in (
            ("hrc_pass_through", "NSR pass-through of the HRC move", "fx", True),
            ("hrc_lag_q", "HRC lag", None, True),
            ("cc_intensity", "Coking coal per tonne of steel", "fx", False),
            ("cc_lag_q", "Coking coal lag", None, True),
            ("io_intensity", "Iron ore per tonne of steel", "fx", False),
            ("io_market_pct", "Iron ore bought at market", "pct", False),
            ("io_pass_through", "Iron ore pass-through (of the NMDC fines move)", "fx", False),
            ("io_lag_q", "Iron ore lag", None, True),
            ("other_cost_drift", "Other cost drift", "t", True),
            ("util", "Utilisation (default for forecast quarters)", "pct", True),
            ("sales_to_prod", "Sales / production (default)", "fx", True),
            ("ramp_step", "New capacity ramp per quarter", "pct", False)):
        const(key.replace("_q", "").title().replace("_", ""), label, S[key], fmt, yellow=y)
    # the names above: HrcPassThrough, HrcLag, CcIntensity, CcLag, IoIntensity,
    # IoMarketPct, IoPassThrough, IoLag, OtherCostDrift, Util, SalesToProd, RampStep

    inp.section("Capex plan (Rs crore per fiscal year)")
    cp_first = inp.r
    for fy, e in cfg["capex_plan"].items():
        ws.cell(inp.r, 1, fy).font = Font(name=FONT)
        c = ws.cell(inp.r, 3, e["value"])
        c.font = Font(name=FONT, color=BLUE)
        c.fill = YELLOW
        c.number_format = FMT["cr"]
        ws.cell(inp.r, 4, e["source"]).font = Font(name=FONT, size=8, italic=True, color="595959")
        inp.r += 1
    name(wb, "CapexYears", f"Inputs!$A${cp_first}:$A${inp.r - 1}")
    name(wb, "CapexPlan", f"Inputs!$C${cp_first}:$C${inp.r - 1}")

    # commodity deck
    inp.section(DECK_TITLE)
    used = {}
    fxh = cfg.get("fx_history", {})
    deck = [("hrc", "HRC India", "Rs/t", "t"), ("cc", "Coking coal, Australia FOB", "USD/t", "mn"),
            ("io", "Iron ore 62% CFR China", "USD/t", "mn"), ("usdinr", "USDINR", "INR/USD", "fx")]
    if "eurinr" in fxh:
        deck.append(("eurinr", "EURINR", "INR/EUR", "fx"))
    for key, label, unit, fmt in deck:
        if key in hist:
            h, spot, note = hist[key]["q"], hist[key]["fwd"], fwd_note(hist[key])
        else:
            h = {q: fxh[key][q] for q in qs if q in fxh[key]}
            spot, note = fxh[key]["spot"], f"Spot {cfg['built']} ({fxh[key]['source']})"
        r_act = inp.row(f"{label}: actual quarterly average", unit, {q: h.get(q) for q in qs if q in act}, fmt=fmt)
        r_ovr = inp.row(f"{label}: override (your view)", unit, lambda i, q, r: None, fmt=fmt)
        for i, q in enumerate(qs):
            if q not in act:
                ws.cell(r_ovr, FIRST_COL + i).fill = YELLOW
                ws.cell(r_ovr, FIRST_COL + i).number_format = FMT[fmt]
                ws.cell(r_ovr, FIRST_COL + i).font = Font(name=FONT, color=BLUE)
        ws.cell(r_act, 3, spot).font = Font(name=FONT, color=BLUE)
        ws.cell(r_act, 3).number_format = FMT[fmt]
        ws.cell(r_act, 3).comment = Comment(note, "steel_model")
        name(wb, f"Spot_{key}", f"Inputs!$C${r_act}")
        used[key] = inp.row(
            f"{label}: used", unit,
            lambda i, q, r, ra=r_act, ro=r_ovr, k=key:
                f'=IF({col(i)}$6="A",{col(i)}{ra},IF({col(i)}{ro}<>"",{col(i)}{ro},Spot_{k}))',
            fmt=fmt, bold=True, kind="local")
    if "eurinr" not in used:
        used["eurinr"] = None

    def lagged(i, row, lag_name):
        return f"INDEX($D${row}:${last}${row},MAX(1,{col(i)}$7-{lag_name}))"

    inp.section("Effective (lagged) prices the India drivers read")
    r_hrc_eff = inp.row("HRC, lagged", "Rs/t", lambda i, q, r: "=" + lagged(i, used["hrc"], "HrcLag"),
                        fmt="t", kind="local")
    r_cc_eff = inp.row("Coking coal in rupees, lagged", "Rs/t coal",
                       lambda i, q, r: "=" + lagged(i, used["cc"], "CcLag") + "*" + lagged(i, used["usdinr"], "CcLag"),
                       fmt="t", kind="local")
    r_io_eff = inp.row("Iron ore in rupees, lagged (seaborne: cross-check only)", "Rs/t ore",
                       lambda i, q, r: "=" + lagged(i, used["io"], "IoLag") + "*" + lagged(i, used["usdinr"], "IoLag"),
                       fmt="t", kind="local")

    # Domestic ore, appended AFTER everything above so a workbook already in
    # use takes these rows without any existing cell moving (2026-11-03).
    inp.section("Domestic iron ore: NMDC fines, ex-royalty - this drives the ore cost line")
    hn = hist.get("nmdc") or {"q": {}, "spot": None, "spot_date": None}
    r_nm_act = inp.row("NMDC fines: actual quarterly average (time-weighted)", "Rs/t",
                       {q: hn["q"].get(q) for q in qs if q in act}, fmt="t")
    r_nm_ovr = inp.row("NMDC fines: override (your view)", "Rs/t", lambda i, q, r: None, fmt="t")
    for i, q in enumerate(qs):
        if q not in act:
            ws.cell(r_nm_ovr, FIRST_COL + i).fill = YELLOW
            ws.cell(r_nm_ovr, FIRST_COL + i).number_format = FMT["t"]
            ws.cell(r_nm_ovr, FIRST_COL + i).font = Font(name=FONT, color=BLUE)
    ws.cell(r_nm_act, 3, hn["spot"]).font = Font(name=FONT, color=BLUE)
    ws.cell(r_nm_act, 3).number_format = FMT["t"]
    ws.cell(r_nm_act, 3).comment = Comment(f"Latest NMDC circular {hn['spot_date']} (prices table, nmdc_fines_inr)", "steel_model")
    name(wb, "Spot_nmdc", f"Inputs!$C${r_nm_act}")
    r_nm_used = inp.row("NMDC fines: used", "Rs/t",
                        lambda i, q, r: f'=IF({col(i)}$6="A",{col(i)}{r_nm_act},IF({col(i)}{r_nm_ovr}<>"",{col(i)}{r_nm_ovr},Spot_nmdc))',
                        fmt="t", bold=True, kind="local")
    r_nm_eff = inp.row("NMDC fines, lagged (the ore cost driver)", "Rs/t ore",
                       lambda i, q, r: "=" + lagged(i, r_nm_used, "IoLag"), fmt="t", kind="local")

    # ---------------------------------------------------------------- Capacity
    cap = Sheet(wb, "Capacity", qs, f"{cfg['company']} - India capacity, utilisation and volume",
                "Capacity in mtpa; production and sales in mt per quarter. Projects add capacity "
                "from their commissioning quarter, ramping at RampStep per quarter.")
    cap.header()
    cws = cap.ws
    cap.section("Projects (mtpa added; commissioning quarter in column C)")
    proj_rows = []
    for p in cfg["projects"]:
        r = cap.r
        cws.cell(r, 1, p["name"]).font = Font(name=FONT)
        cws.cell(r, 2, p["mtpa"]).font = Font(name=FONT, color=BLUE)
        cws.cell(r, 2).fill = YELLOW
        cws.cell(r, 3, p["commission"]).font = Font(name=FONT, color=BLUE)
        cws.cell(r, 3).fill = YELLOW
        cws.cell(r, 1).comment = Comment(p["source"], "steel_model")
        for i in range(N):
            f = (f"=IFERROR(IF(Inputs!{col(i)}$7>=MATCH($C{r},Qrow,0),"
                 f"$B{r}*MIN(1,(Inputs!{col(i)}$7-MATCH($C{r},Qrow,0)+1)*RampStep),0),0)")
            c = cws.cell(r, FIRST_COL + i, f)
            c.font = Font(name=FONT, color=GREEN)
            c.number_format = FMT["mt"]
        proj_rows.append(r)
        cap.r += 1
    cap.section("Volume")
    r_capeff = cap.row("Effective India capacity", "mtpa",
                       lambda i, q, r: f"=India_cap+SUM({col(i)}{proj_rows[0]}:{col(i)}{proj_rows[-1]})",
                       fmt="mt", bold=True, kind="local")
    r_util = cap.row("Utilisation", "%",
                     lambda i, q, r: (f"=IFERROR(Segments!{col(i)}{{PROD_ACT}}*4/{col(i)}{r_capeff},0)"
                                      if q in act else "=Util"),
                     fmt="pct", fill=lambda i, q: q not in act,
                     comment="Actual quarters: India production x4 / capacity (memo; FY25 capacity was "
                             "lower and BF-3 was down Jul-Nov 2025). Forecast: type over the yellow cells.")
    r_prod = cap.row("Production (model)", "mt", lambda i, q, r: f"={col(i)}{r_capeff}/4*{col(i)}{r_util}",
                     fmt="mt", kind="local")
    r_ratio = cap.row("Sales / production", "x",
                      lambda i, q, r: (f"=IFERROR(Segments!{col(i)}{{SALES_ACT}}/Segments!{col(i)}{{PROD_ACT}},0)"
                                       if q in act else "=SalesToProd"),
                      fmt="fx", fill=lambda i, q: q not in act)
    r_sales = cap.row("Sales (model)", "mt", lambda i, q, r: f"={col(i)}{r_prod}*{col(i)}{r_ratio}",
                      fmt="mt", kind="local")

    # ---------------------------------------------------------------- Segments
    seg = Sheet(wb, "Segments", qs, f"{cfg['company']} - segment build",
                "Rs crore unless stated. India is driven; overseas is EBITDA in its own currency x FX; "
                "the plug is consolidation adjustments and small subsidiaries.")
    seg.header()
    I = cfg["india"]
    seg.section("India operations: actuals (type each new quarter here)")
    rP = seg.row("Crude steel production", "mt", {q: I["production_mt"].get(q) for q in qs}, fmt="mt")
    rS = seg.row("Steel sales", "mt", {q: I["sales_mt"].get(q) for q in qs}, fmt="mt")
    rR = seg.row("Revenue from operations", "Rs cr", {q: I["revenue_cr"].get(q) for q in qs})
    rE = seg.row("Adjusted EBITDA", "Rs cr", {q: I["adj_ebitda_cr"].get(q) for q in qs})
    # back-fill the Capacity formulas that needed these rows
    for r in (r_util, r_ratio):
        for i in range(N):
            c = cws.cell(r, FIRST_COL + i)
            if isinstance(c.value, str):
                c.value = c.value.replace("{PROD_ACT}", str(rP)).replace("{SALES_ACT}", str(rS))

    seg.section("India operations: the PM's view for the visible quarter (yellow, optional)")
    rPMs = seg.row("PM view: sales", "mt", lambda i, q, r: None, fmt="mt")
    rPMe = seg.row("PM view: EBITDA per tonne", "Rs/t", lambda i, q, r: None, fmt="t")
    for r, fmt in ((rPMs, "mt"), (rPMe, "t")):
        for i, q in enumerate(qs):
            if q not in act:
                c = seg.ws.cell(r, FIRST_COL + i)
                c.fill, c.number_format, c.font = YELLOW, FMT[fmt], Font(name=FONT, color=BLUE)

    seg.section("India operations: model")
    r_prod_u = seg.row("Production", "mt", lambda i, q, r: f"=IF({A(i)},{col(i)}{rP},Capacity!{col(i)}{r_prod})", fmt="mt")
    r_sales_u = seg.row("Sales", "mt",
                        lambda i, q, r: f'=IF({A(i)},{col(i)}{rS},IF({col(i)}{rPMs}<>"",{col(i)}{rPMs},Capacity!{col(i)}{r_sales}))',
                        fmt="mt", bold=True)
    r_nsr = seg.r
    seg.row("Net sales realisation (NSR)", "Rs/t",
            lambda i, q, r: (f"=IF({A(i)},{col(i)}{rR}/{col(i)}{rS}*10,"
                             + (f"{prev(i, r)}+HrcPassThrough*(Inputs!{col(i)}{r_hrc_eff}-Inputs!{prev(i, r_hrc_eff)}))" if i else "0)")),
            fmt="t")
    r_cost = seg.r
    r_cost_used = r_cost + 3                      # declared below; rows are fixed
    seg.row("Cost per tonne (model)", "Rs/t",
            lambda i, q, r: (f"=IF({A(i)},({col(i)}{rR}-{col(i)}{rE})/{col(i)}{rS}*10,"
                             + (f"{prev(i, r_cost_used)}+CcIntensity*(Inputs!{col(i)}{r_cc_eff}-Inputs!{prev(i, r_cc_eff)})"
                                f"+IoIntensity*IoMarketPct*IoPassThrough*(Inputs!{col(i)}{r_nm_eff}-Inputs!{prev(i, r_nm_eff)})"
                                f"+OtherCostDrift)" if i else "0)")),
            fmt="t",
            comment="Forecast = last quarter's cost (as used) + coal intensity x change in lagged coal (Rs/t) "
                    "+ ore intensity x market share x pass-through x change in lagged NMDC fines (Rs/t) + drift. "
                    "Intensities and shares are specs/entities/steel.yaml's.")
    r_ept_model = seg.row("EBITDA per tonne (model)", "Rs/t", lambda i, q, r: f"={col(i)}{r_nsr}-{col(i)}{r_cost}", fmt="t", kind="local")
    r_ept = seg.row("EBITDA per tonne (used)", "Rs/t",
                    lambda i, q, r: f'=IF({A(i)},{col(i)}{r_ept_model},IF({col(i)}{rPMe}<>"",{col(i)}{rPMe},{col(i)}{r_ept_model}))',
                    fmt="t", bold=True)
    assert seg.r == r_cost_used
    seg.row("Cost per tonne (used)", "Rs/t", lambda i, q, r: f"={col(i)}{r_nsr}-{col(i)}{r_ept}", fmt="t", kind="local")
    r_irev = seg.row("India revenue", "Rs cr", lambda i, q, r: f"=IF({A(i)},{col(i)}{rR},{col(i)}{r_sales_u}*{col(i)}{r_nsr}/10)")
    r_iebitda = seg.row("India adjusted EBITDA", "Rs cr",
                        lambda i, q, r: f"=IF({A(i)},{col(i)}{rE},{col(i)}{r_sales_u}*{col(i)}{r_ept}/10)", bold=True)

    def rolled(i, act_row, r, n=4):
        """IF(actual) the typed row, else the trailing n quarters of THIS row."""
        return f"=IF({A(i)},{col(i)}{act_row}," + (trailing(i, r, n)[1:] if i >= n else "0") + ")"

    rem_rev = rem_e = None
    if cfg.get("removed"):
        Rm = cfg["removed"]
        seg.section(Rm["name"] + " - actuals only, zero once gone")
        rem_rev = seg.row("Revenue", "Rs cr", {q: Rm["revenue_cr"].get(q) for q in qs})
        rem_e = seg.row("Adjusted EBITDA", "Rs cr", {q: Rm["adj_ebitda_cr"].get(q) for q in qs})

    fx_rev_rows, fx_e_rows = [], []
    for s in cfg["fx_segments"]:
        seg.section(f"{s['name']}: actuals in {s['ccy']} mn (type each new quarter here)")
        a_rows = {}
        for key, lab, unit, fmt in (("production_mt", "Production", "mt", "mt"), ("sales_mt", "Sales", "mt", "mt"),
                                    ("revenue_mn", "Revenue", s["ccy"] + " mn", "mn"),
                                    ("ebitda_mn", "EBITDA", s["ccy"] + " mn", "mn")):
            if key in s:
                a_rows[key] = seg.row(lab, unit, {q: s[key].get(q) for q in qs}, fmt=fmt)
        seg.section(f"{s['name']}: model (forecasts roll the trailing four quarters)")
        if "sales_mt" in a_rows:
            s["_sales_row"] = seg.row("Sales", "mt", lambda i, q, r, a=a_rows["sales_mt"]: rolled(i, a, r),
                                      fmt="mt", kind="local")
        r_rv = seg.row(f"Revenue ({s['ccy']} mn)", s["ccy"] + " mn",
                       lambda i, q, r, a=a_rows["revenue_mn"]: rolled(i, a, r), fmt="mn")
        r_ev = seg.row(f"EBITDA ({s['ccy']} mn)", s["ccy"] + " mn",
                       lambda i, q, r, a=a_rows["ebitda_mn"]: rolled(i, a, r), fmt="mn")
        fxrow = used[s["fx"]]
        fx_rev_rows.append(seg.row("Revenue", "Rs cr", lambda i, q, r, a=r_rv: f"={col(i)}{a}*Inputs!{col(i)}{fxrow}/10"))
        fx_e_rows.append(seg.row("EBITDA", "Rs cr", lambda i, q, r, a=r_ev: f"={col(i)}{a}*Inputs!{col(i)}{fxrow}/10", bold=True))

    CONS = cfg["consolidated"]
    seg.section("Consolidated: reported actuals (type each new quarter here)")
    r_crev_a = seg.row("Consolidated revenue (reported)", "Rs cr", {q: CONS["revenue_cr"].get(q) for q in qs})
    r_ce_a = seg.row("Consolidated adjusted EBITDA (reported)", "Rs cr", {q: CONS["adj_ebitda"].get(q) for q in qs})
    seg.section("Consolidation adjustments and other subsidiaries (plug on actuals, trailing four on forecasts)")
    parts_e = [r_iebitda] + ([rem_e] if rem_e else []) + fx_e_rows
    parts_r = [r_irev] + ([rem_rev] if rem_rev else []) + fx_rev_rows

    def plug(i, r, total_row, parts):
        return (f"=IF({A(i)},{col(i)}{total_row}-" + "-".join(f"{col(i)}{x}" for x in parts) + ","
                + (trailing(i, r)[1:] if i >= 4 else "0") + ")")
    r_plug_r = seg.row("Revenue plug", "Rs cr", lambda i, q, r: plug(i, r, r_crev_a, parts_r), kind="local",
                       comment="Actual = consolidated revenue less the segments (eliminations, coated and other "
                               "subsidiary residue). Forecast = trailing four quarters.")
    r_plug_e = seg.row("EBITDA plug", "Rs cr", lambda i, q, r: plug(i, r, r_ce_a, parts_e), kind="local")
    seg.section("Consolidated")
    r_cons_rev = seg.row("Consolidated revenue", "Rs cr",
                         lambda i, q, r: "=" + "+".join(f"{col(i)}{x}" for x in parts_r + [r_plug_r]), bold=True, kind="local")
    r_cons_e = seg.row("Consolidated adjusted EBITDA", "Rs cr",
                       lambda i, q, r: "=" + "+".join(f"{col(i)}{x}" for x in parts_e + [r_plug_e]), bold=True, kind="local")
    us = next((s for s in cfg["fx_segments"] if "_sales_row" in s), None)
    r_cons_sales = seg.row("Consolidated sales (India + overseas steel)", "mt",
                           lambda i, q, r: f"={col(i)}{r_sales_u}" + (f"+{col(i)}{us['_sales_row']}" if us else ""),
                           fmt="mt", kind="local")
    seg.row("India share of EBITDA", "%", lambda i, q, r: f"=IFERROR({col(i)}{r_iebitda}/{col(i)}{r_cons_e},0)", fmt="pct", kind="local")
    r_street = seg.row("Street consensus EBITDA (type in)", "Rs cr", lambda i, q, r: None)
    seg.row("Model vs street", "%", lambda i, q, r: f'=IF({col(i)}{r_street}="","",{col(i)}{r_cons_e}/{col(i)}{r_street}-1)',
            fmt="pct", kind="local")

    if cfg.get("jv"):
        J = cfg["jv"]
        seg.section(f"{J['name']} - memo, equity-accounted below EBITDA, never inside it")
        seg.row("Sales", "mt", {q: J["sales_mt"].get(q) for q in qs}, fmt="mt")
        seg.row("Adjusted EBITDA (100%)", "Rs cr", {q: J["adj_ebitda_cr"].get(q) for q in qs})
        seg.row("PAT (100%)", "Rs cr", {q: J.get("pat_cr", {}).get(q) for q in qs})

    # ---------------------------------------------------------------- P&L
    pl = Sheet(wb, "P&L", qs, f"{cfg['company']} - consolidated income statement",
               "Rs crore. Actual quarters are typed (blue); PBT and PAT are recomputed and checked against "
               "the reported figures.")
    pl.header()
    pl.section("Reported actuals (type each new quarter here; revenue and adjusted EBITDA go on Segments)")
    pa = {}
    for key, lab in (("reported_ebitda", "Reported EBITDA"), ("other_income", "Other income"),
                     ("finance_cost", "Finance cost"), ("depreciation", "Depreciation"),
                     ("jv_share", "Share of JV profit"), ("exceptional", "Exceptional items"),
                     ("pbt", "Profit before tax"), ("tax", "Tax"), ("pat", "Profit after tax")):
        pa[key] = pl.row(lab, "Rs cr", {q: CONS[key].get(q) for q in qs})
    pa["eps"] = pl.row("Diluted EPS", "Rs", {q: CONS["eps_reported"].get(q) for q in qs}, fmt="rs")

    pl.section("Income statement")
    r_rev = pl.row("Revenue from operations", "Rs cr", lambda i, q, r: f"=Segments!{col(i)}{r_cons_rev}", bold=True)
    r_adj = pl.row("Adjusted EBITDA", "Rs cr", lambda i, q, r: f"=Segments!{col(i)}{r_cons_e}", bold=True)
    pl.row("Adjusted EBITDA margin", "%", lambda i, q, r: f"=IFERROR({col(i)}{r_adj}/{col(i)}{r_rev},0)", fmt="pct", kind="local")
    r_fxa = pl.row("FX on long-term borrowings (reported less adjusted)", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{col(i)}{pa['reported_ebitda']}-{col(i)}{r_adj},0)",
                   comment="Unrealised FX on long-term borrowings; the company strips it out of adjusted EBITDA. "
                           "Zero in forecasts.")
    r_rep = pl.row("Reported EBITDA", "Rs cr", lambda i, q, r: f"={col(i)}{r_adj}+{col(i)}{r_fxa}", kind="local")
    r_oi = pl.row("Other income", "Rs cr", lambda i, q, r: rolled(i, pa["other_income"], r))
    r_fin = pl.row("Finance cost", "Rs cr", lambda i, q, r: "={FIN}")
    r_da = pl.row("Depreciation", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{pa['depreciation']},"
                                  + (f"{prev(i, r)}*Capacity!{col(i)}{r_capeff}/Capacity!{prev(i, r_capeff)})" if i else "0)"),
                  comment="Forecast D&A scales with effective India capacity, so a project adds depreciation "
                          "in the quarter it starts producing (capex -> commissioning -> D&A).")
    r_jv = pl.row("Share of JV profit", "Rs cr", lambda i, q, r: rolled(i, pa["jv_share"], r, 2),
                  comment="Forecast = trailing two quarters: FY26 proforma lines carry BPSL's pre-JV profit, "
                          "so a longer window would read the wrong business.")
    r_exc = pl.row("Exceptional items", "Rs cr", lambda i, q, r: f"=IF({A(i)},{col(i)}{pa['exceptional']},0)")
    r_pbt = pl.row("Profit before tax", "Rs cr",
                   lambda i, q, r: f"={col(i)}{r_rep}+{col(i)}{r_oi}-{col(i)}{r_fin}-{col(i)}{r_da}+{col(i)}{r_jv}+{col(i)}{r_exc}",
                   bold=True, kind="local")
    r_tax = pl.row("Tax", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{col(i)}{pa['tax']},Tax_rate*({col(i)}{r_pbt}-{col(i)}{r_jv}))")
    r_pat = pl.row("Profit after tax", "Rs cr", lambda i, q, r: f"={col(i)}{r_pbt}-{col(i)}{r_tax}", bold=True, kind="local")
    r_eps = pl.row("EPS (on today's share count, before minorities)", "Rs",
                   lambda i, q, r: f"={col(i)}{r_pat}*10/Shares_mn", fmt="rs")
    pl.section("Checks against the reported figures (should read zero on actual quarters)")
    r_rpbt = pl.row("PBT difference", "Rs cr",
                    lambda i, q, r: f"=IF({A(i)},{col(i)}{r_pbt}-{col(i)}{pa['pbt']},0)")
    pl.row("PAT difference", "Rs cr", lambda i, q, r: f"=IF({A(i)},{col(i)}{r_pat}-{col(i)}{pa['pat']},0)")

    # ---------------------------------------------------------------- Cash flow
    cf = Sheet(wb, "Cash flow", qs, f"{cfg['company']} - cash flow and net debt",
               "Rs crore. Actual quarters take the reported net debt; the residual row is everything the decks do "
               "not split out (working capital, dividends, JV proceeds, undisclosed capex).")
    cf.header()
    B = cfg["balance"]
    cf.section("Reported actuals (type each new quarter here)")
    r_nd_a = cf.row("Net debt, reported", "Rs cr", {q: B["net_debt_cr"].get(q) for q in qs},
                    comment=f"Opening net debt {cfg['opening_net_debt']['value']:,} at "
                            f"{cfg['opening_net_debt']['as_of']} ({cfg['opening_net_debt']['source']}).")
    r_cash_a = cf.row("Cash and equivalents, reported", "Rs cr", {q: B["cash_cr"].get(q) for q in qs})
    r_capex_a = cf.row("Capex, reported", "Rs cr", {q: B["capex_cr"].get(q) for q in qs},
                       comment="Blank where the decks give no quarterly figure (FY25 was 14,656; 9M FY26 was "
                               "10,018); the residual below then absorbs it.")
    r_wc_in = cf.row("Working capital, dividends, other: your view", "Rs cr", lambda i, q, r: None,
                     fill=lambda i, q: q not in act)
    for i, q in enumerate(qs):
        if q not in act:
            c = cf.ws.cell(r_wc_in, FIRST_COL + i)
            c.fill, c.number_format, c.font = YELLOW, FMT["cr"], Font(name=FONT, color=BLUE)
    cf.section("Free cash flow")
    r_ce = cf.row("Adjusted EBITDA", "Rs cr", lambda i, q, r: f"='P&L'!{col(i)}{r_adj}")
    cf.row("Less: tax", "Rs cr", lambda i, q, r: f"=-'P&L'!{col(i)}{r_tax}")
    cf.row("Less: finance cost", "Rs cr", lambda i, q, r: f"=-'P&L'!{col(i)}{r_fin}")
    cf.row("Plus: other income", "Rs cr", lambda i, q, r: f"='P&L'!{col(i)}{r_oi}")
    r_capex = cf.row("Less: capex", "Rs cr",
                     lambda i, q, r: f"=IF({A(i)},-{col(i)}{r_capex_a},"
                                     f'-(INDEX(CapexPlan,MATCH({col(i)}$5,CapexYears,0))'
                                     f'-SUMIFS($D{r_capex_a}:${last}{r_capex_a},FYrow,{col(i)}$5,Status,"A"))'
                                     f'/COUNTIFS(FYrow,{col(i)}$5,Status,"F"))',
                     comment="Forecast = the fiscal year's plan (Inputs) less capex already reported in that "
                             "year, spread over its remaining quarters.")
    r_fcf = cf.row("Free cash flow before working capital", "Rs cr",
                   lambda i, q, r: f"=SUM({col(i)}{r_ce}:{col(i)}{r_capex})", bold=True, kind="local")
    r_wc = cf.row("Working capital, dividends and other", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{prev(i, r_nd_a) if i else 'Open_ND'}-{col(i)}{r_nd_a}-{col(i)}{r_fcf},"
                                  f"N({col(i)}{r_wc_in}))",
                  comment="Actual: whatever reconciles reported net debt (it holds the BPSL slump sale in Q4FY26, "
                          "JFE's second tranche in Q1FY27, dividends and undisclosed capex). Forecast: the yellow "
                          "row above, negative for an outflow.")
    cf.section("Net debt")
    r_nd = cf.row("Net debt (end of quarter)", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_nd_a},"
                                  + (f"{prev(i, r)}" if i else "Open_ND") + f"-{col(i)}{r_fcf}-{col(i)}{r_wc})",
                  bold=True)
    r_cash = cf.row("Cash and equivalents", "Rs cr",
                    lambda i, q, r: f"=IF({A(i)},{col(i)}{r_cash_a}," + (prev(i, r) if i else "0") + ")")
    r_gross = cf.row("Gross debt", "Rs cr", lambda i, q, r: f"={col(i)}{r_nd}+{col(i)}{r_cash}", kind="local")
    cf.row("Net debt / LTM adjusted EBITDA", "x",
           lambda i, q, r: f"={col(i)}{r_nd}/SUM({col(i - 3)}{r_ce}:{col(i)}{r_ce})" if i >= 3 else None,
           fmt="x", kind="local")
    for i, q in enumerate(qs):
        c = pl.ws.cell(r_fin, FIRST_COL + i)
        c.value = (f"=IF({A(i)},{col(i)}{pa['finance_cost']},"
                   + (f"Int_rate*'Cash flow'!{prev(i, r_gross)}/4)" if i else "0)"))
        c.font = Font(name=FONT, color=GREEN)
    pl.ws.cell(r_fin, 1).comment = Comment(
        "Forecast = Int_rate x OPENING gross debt (net debt + cash at the previous quarter end) / 4. "
        "Opening, so finance cost and net debt do not chase each other round a circular reference.", "steel_model")

    # ---------------------------------------------------------------- Annual
    an = wb.create_sheet("Annual")
    an["A1"] = f"{cfg['company']} - fiscal-year summary"
    an["A1"].font = Font(name=FONT, bold=True, size=13)
    an["A2"] = "Sums of the quarters; a year with any forecast quarter is marked F."
    an["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    an.column_dimensions["A"].width = 40
    fys = sorted({fy_of(q) for q in qs if sum(fy_of(x) == fy_of(q) for x in qs) == 4})
    for j, fy in enumerate(fys):
        c = an.cell(4, 2 + j, fy)
        c.font = Font(name=FONT, bold=True, color="FFFFFF")
        c.fill = HEAD
        c.alignment = Alignment(horizontal="center")
        an.column_dimensions[get_column_letter(2 + j)].width = 12
        st = an.cell(5, 2 + j, f'=IF(COUNTIFS(FYrow,{get_column_letter(2 + j)}4,Status,"F")>0,"F","A")')
        st.font = Font(name=FONT, bold=True)
        st.alignment = Alignment(horizontal="center")
    an.cell(4, 1, "Fiscal year").font = Font(name=FONT, bold=True, color="FFFFFF")
    an.cell(4, 1).fill = HEAD
    an.cell(5, 1, "Status").font = Font(name=FONT, bold=True)

    def rowsum(sheet, r):
        return f"'{sheet}'!$D${r}:${last}${r}"

    arows = [
        ("India sales (mt)", lambda L: f"=SUMIFS({rowsum('Segments', r_sales_u)},FYrow,{L}$4)", "mt"),
        ("India EBITDA per tonne (Rs/t)",
         lambda L: f"=SUMIFS({rowsum('Segments', r_iebitda)},FYrow,{L}$4)/SUMIFS({rowsum('Segments', r_sales_u)},FYrow,{L}$4)*10", "t"),
        ("Consolidated sales (mt)", lambda L: f"=SUMIFS({rowsum('Segments', r_cons_sales)},FYrow,{L}$4)", "mt"),
        ("Revenue (Rs cr)", lambda L: f"=SUMIFS({rowsum('P&L', r_rev)},FYrow,{L}$4)", "cr"),
        ("Adjusted EBITDA (Rs cr)", lambda L: f"=SUMIFS({rowsum('P&L', r_adj)},FYrow,{L}$4)", "cr"),
        ("Profit after tax (Rs cr)", lambda L: f"=SUMIFS({rowsum('P&L', r_pat)},FYrow,{L}$4)", "cr"),
        ("EPS (Rs)", lambda L: f"=SUMIFS({rowsum('P&L', r_eps)},FYrow,{L}$4)", "rs"),
        ("Capex (Rs cr)", lambda L: f"=-SUMIFS({rowsum('Cash flow', r_capex)},FYrow,{L}$4)", "cr"),
        ("Free cash flow before WC (Rs cr)", lambda L: f"=SUMIFS({rowsum('Cash flow', r_fcf)},FYrow,{L}$4)", "cr"),
        ("Net debt, year end (Rs cr)",
         lambda L: f"=INDEX({rowsum('Cash flow', r_nd)},MATCH(\"Q4\"&{L}$4,Qrow,0))", "cr"),
    ]
    for k, (lab, fn, fmt) in enumerate(arows):
        r = 6 + k
        an.cell(r, 1, lab).font = Font(name=FONT)
        for j in range(len(fys)):
            L = get_column_letter(2 + j)
            c = an.cell(r, 2 + j, fn(L))
            c.font = Font(name=FONT, color=GREEN)
            c.number_format = FMT[fmt]
    an.cell(6 + len(arows) + 1, 1,
            "FY guidance check: consolidated sales for the guided year (Inputs) vs the model").font = Font(name=FONT, italic=True, size=9)
    gr = 6 + len(arows) + 2
    an.cell(gr, 1, "Guided consolidated sales, FY27 (mt)").font = Font(name=FONT)
    an.cell(gr, 2, "=Guide_sales").font = Font(name=FONT, color=GREEN)
    an.cell(gr + 1, 1, "Model consolidated sales, FY27 (mt)").font = Font(name=FONT)
    an.cell(gr + 1, 2, f"=SUMIFS({rowsum('Segments', r_cons_sales)},FYrow,\"FY27\")").font = Font(name=FONT, color=GREEN)
    for rr in (gr, gr + 1):
        an.cell(rr, 2).number_format = FMT["mt"]

    # ---------------------------------------------------------------- Valuation
    va = wb.create_sheet("Valuation")
    va.column_dimensions["A"].width = 52
    va.column_dimensions["B"].width = 14
    va.column_dimensions["C"].width = 14
    va.column_dimensions["D"].width = 14
    va.column_dimensions["E"].width = 70
    va["A1"] = f"{cfg['company']} - EV/EBITDA to an expected price"
    va["A1"].font = Font(name=FONT, bold=True, size=13)
    va["A2"] = ("For the PM's eye. The system's juice comes from case_brief.py (median house multiple, sigma range); "
                "the two must agree.")
    va["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    nd_row = rowsum("Cash flow", r_nd)
    vrows = [
        ("Valuation year", "FY28", None, "inp", "Sum of that year's four quarters of adjusted EBITDA."),
        ("Net debt as of (quarter)", "Q4FY27", None, "inp", "One year before the valuation year ends, the usual 12-month-forward convention."),
        ("Quarters in the valuation year", '=COUNTIFS(FYrow,B4)', None, "f", "Must read 4."),
        ("Adjusted EBITDA, valuation year (Rs cr)", f"=SUMIFS({rowsum('P&L', r_adj)},FYrow,B4)", "cr", "f", ""),
        ("PAT, valuation year (Rs cr)", f"=SUMIFS({rowsum('P&L', r_pat)},FYrow,B4)", "cr", "f", ""),
        ("EPS, valuation year (Rs)", "=B8*10/Shares_mn", "rs", "f", ""),
        ("Market cap at CMP (Rs cr)", "=CMP*Shares_mn/10", "cr", "f", ""),
        ("Net debt, latest actual (Rs cr)", f'=INDEX({nd_row},COUNTIF(Status,"A"))', "cr", "f", ""),
        ("Core EV today: market cap + net debt - JV stake (Rs cr)", "=B10+B11-JV_stake", "cr", "f",
         "The JV is outside EBITDA, so it comes out of EV too; otherwise today's multiple carries it and the "
         "target adds it again (about Rs64/share of upside from nothing)."),
        ("EV / EBITDA today, on the valuation year", "=B12/B7", "x", "f", ""),
        ("P/E today, on the valuation year", "=CMP/B9", "x", "f", ""),
        ("Target EV/EBITDA multiple", "=B13", "x", "key",
         "Defaults to TODAY's multiple, so the price below moves only on the model's EBITDA and cash. Replace with the "
         "median house-implied multiple after the target sweep (the agent drill), or your own. A house multiple "
         "has the JV inside it: put the EBITDA it was struck on in the next row."),
        # PM, 2026-11-04: "Let me put 9.85x as is. Let the TP calculation show the
        # correction of the JV." A house-implied multiple is (TP x shares + net
        # debt) / consensus EBITDA, with the JV INSIDE it. Applied to core EBITDA
        # with the JV added again below, it counted the JV twice: JSW's 9.85x
        # carries 15,750/39,877 = 0.39x of JV, Rs18,500cr at the model's FY28
        # EBITDA, so the target read Rs1,776 against a correct ~Rs1,700.
        ("EBITDA that multiple was struck on (Rs cr)", None, "cr", "key",
         "Only for a HOUSE multiple: each house's implied multiple is (TP x shares + net debt) / consensus EBITDA, "
         "with the JV inside it. Type the consensus EBITDA it was set on and the JV comes out in the next row. "
         "Blank = the multiple is already ex JV (the default, today's core multiple)."),
        ("Less: JV carried inside that multiple (x)", "=IF(ISNUMBER(B16),-JV_stake/B16,0)", "x2", "f",
         "The JV stake as a multiple of the EBITDA above. It comes out here because the JV is added back at its "
         "own value below; left in, it would grow with the model's EBITDA."),
        ("Core multiple, ex JV (x)", "=B15+B17", "x2", "f", ""),
        ("Target core EV (Rs cr)", "=B18*B7", "cr", "f", ""),
        ("Less: net debt at the date above (Rs cr)", f"=-INDEX({nd_row},MATCH(B5,Qrow,0))", "cr", "f", ""),
        ("Plus: JV stake (Rs cr)", "=JV_stake", "cr", "f",
         "JFE's price for 50% of JSW JFE Steel. Added once: the JV sits below EBITDA and is out of the core "
         "multiple above."),
        ("Plus: CWIP for capacity not yet earning (Rs cr)", 0, "cr", "key",
         "Capex already spent on projects that produce after the valuation year (Utkal, JVML Phase-II). Zero by "
         "default: the decks give no per-project spend to date."),
        ("Equity value (Rs cr)", "=SUM(B19:B22)", "cr", "f", ""),
        ("Expected price (Rs)", "=B23*10/Shares_mn", "rs", "f", ""),
        ("Upside vs CMP", "=B24/CMP-1", "pct", "f", ""),
        ("Implied P/E at the expected price", "=B24/B9", "x", "f", ""),
    ]
    for k, (lab, v, fmt, kind, note) in enumerate(vrows):
        r = 4 + k
        va.cell(r, 1, lab).font = Font(name=FONT, bold=lab in ("Expected price (Rs)", "Upside vs CMP"))
        c = va.cell(r, 2, v)
        is_f = isinstance(v, str) and v.startswith("=")
        linked = is_f and re.search(r"!|FYrow|Qrow|Status|Shares_mn|CMP|JV_stake", v)
        c.font = Font(name=FONT, color=(GREEN if linked else BLACK) if is_f else BLUE,
                      bold=lab in ("Expected price (Rs)", "Upside vs CMP"))
        if fmt:
            c.number_format = FMT[fmt]
        if kind in ("inp", "key"):
            c.fill = YELLOW
        if note:
            va.cell(r, 5, note).font = Font(name=FONT, size=8, italic=True, color="595959")
    sr = 4 + len(vrows) + 2
    va.cell(sr, 1, "Sensitivity: expected price (Rs) by multiple and India EBITDA/t in the valuation year").font = Font(name=FONT, bold=True)
    va.cell(sr + 1, 1, "India EBITDA/t shift (Rs/t)  \\  multiple shift").font = Font(name=FONT, size=9)
    for j, dm in enumerate((-1, 0, 1)):
        c = va.cell(sr + 1, 2 + j, dm)
        c.font = Font(name=FONT, color=BLUE)
        c.number_format = '+0.0"x";-0.0"x";"base"'
    india_sales_val = f"SUMIFS({rowsum('Segments', r_sales_u)},FYrow,$B$4)"
    for k, de in enumerate((-1000, 0, 1000)):
        r = sr + 2 + k
        c = va.cell(r, 1, de)
        c.font = Font(name=FONT, color=BLUE)
        c.number_format = '+#,##0;-#,##0;"base"'
        for j in range(3):
            L = get_column_letter(2 + j)
            f = (f"=(($B$18+{L}${sr + 1})*($B$7+$A{r}*{india_sales_val}/10)+$B$20+$B$21+$B$22)*10/Shares_mn")
            cc = va.cell(r, 2 + j, f)
            cc.font = Font(name=FONT, color=GREEN)
            cc.number_format = FMT["rs"]
    stb = sr + 6
    va.cell(stb, 1, "Street check (type consensus in)").font = Font(name=FONT, bold=True)
    for k, fy in enumerate(("FY27", "FY28")):
        r = stb + 1 + k
        va.cell(r, 1, f"Consensus adjusted EBITDA {fy} (Rs cr)").font = Font(name=FONT)
        va.cell(r, 2).fill = YELLOW
        va.cell(r, 2).number_format = FMT["cr"]
        c = va.cell(r, 3, f"=SUMIFS({rowsum('P&L', r_adj)},FYrow,\"{fy}\")")
        c.font = Font(name=FONT, color=GREEN)
        c.number_format = FMT["cr"]
        c = va.cell(r, 4, f'=IF(B{r}="","",C{r}/B{r}-1)')
        c.number_format = FMT["pct"]
    va.cell(stb, 3, "Model").font = Font(name=FONT, bold=True)
    va.cell(stb, 4, "Model vs street").font = Font(name=FONT, bold=True)

    # ---------------------------------------------------------------- Sources
    so = wb.create_sheet("Sources")
    so.column_dimensions["A"].width = 48
    so.column_dimensions["B"].width = 26
    so.column_dimensions["C"].width = 120
    for j, h in enumerate(("Item", "Quarters", "Where it comes from")):
        c = so.cell(1, 1 + j, h)
        c.font = Font(name=FONT, bold=True, color="FFFFFF")
        c.fill = HEAD
    rr = 2
    for item in cfg["sources"]:
        for j, v in enumerate(item):
            c = so.cell(rr, 1 + j, v)
            c.font = Font(name=FONT, size=9)
            c.alignment = Alignment(wrap_text=True, vertical="top")
        rr += 1
    rr += 1
    so.cell(rr, 1, "Results decks (BSE filings)").font = Font(name=FONT, bold=True)
    rr += 1
    for q, u in cfg["decks"].items():
        so.cell(rr, 1, q).font = Font(name=FONT, size=9)
        c = so.cell(rr, 3, u)
        c.font = Font(name=FONT, size=9, color=BLUE, underline="single")
        c.hyperlink = u
        rr += 1

    wb.move_sheet("README", offset=-wb.index(wb["README"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return {"quarters": len(qs), "actual": n_act, "rows": {"india_ebitda": r_iebitda, "cons_ebitda": r_cons_e,
                                                         "pat": r_pat, "nd": r_nd, "pbt_diff": r_rpbt}}


def readme_lines(cfg, qs, n_act, hist):
    sp = {k: v for k, v in hist.items()}
    L = [
        (f"{cfg['company']} - quarterly model", "t"),
        (f"Built {cfg['built']} by packages/book/steel_model.py from {cfg['entity']}/model_config.json. "
         f"Actuals {qs[0]} to {qs[n_act - 1]}; forecasts to {qs[-1]}.", ""),
        ("", ""),
        ("How it flows", "h"),
        ("Inputs (timeline, constants, sensitivities, commodity deck) -> Capacity (projects, utilisation, volume) -> "
         "Segments (India driven by NSR and cost per tonne; overseas EBITDA x FX; a plug) -> P&L -> Cash flow "
         "(FCF, net debt) -> Annual -> Valuation (EV/EBITDA to an expected price).", ""),
        ("", ""),
        ("Colours", "h"),
        ("Blue text: typed input. Black: formula on the same sheet. Green: link to another sheet.", "blue"),
        ("Yellow fill: a key assumption, or a cell for your view (the visible quarter's sales and EBITDA/t, "
         "commodity overrides, utilisation, capex, working capital, the target multiple).", "yellow"),
        ("", ""),
        ("Rolling a quarter forward (after each result)", "h"),
        ("Every sheet opens with a block of reported actuals (blue). Model rows read that block while the "
         "quarter's status is A and their forecast formula while it is F, so a new quarter is typing, not "
         "rewiring:", ""),
        ("1. Segments: India production, sales, revenue, adjusted EBITDA; each overseas segment's revenue and "
         "EBITDA in its own currency; consolidated revenue and adjusted EBITDA.", ""),
        ("2. P&L: reported EBITDA, other income, finance cost, D&A, JV share, exceptional items, PBT, tax, PAT, EPS.", ""),
        ("3. Cash flow: net debt, cash and capex.", ""),
        ("4. Inputs: the quarter's commodity averages in the 'actual' rows, then flip its status cell from F to A.", ""),
        ("5. Check that P&L's PBT and PAT differences read zero for the new quarter. Then extend last_quarter in "
         "the config if the horizon is getting short (that is a rebuild, so carry your yellow cells across).", ""),
        ("", ""),
        ("What the history is", "h"),
        ("BPSL's steel business was deconsolidated on 27-Mar-2026 into JSW JFE Steel (50:50 with JFE). FY26 quarters "
         "use the company's own proforma ex-BPSL figures. Q3FY26 proforma is derived (FY26 PF less the other three "
         "quarters); its EBITDA agrees with reported-less-BPSL to Rs2cr. Q3FY25 and Q4FY25 are reported figures: the "
         "India segment is India Ops less BPSL's standalone numbers (revenue and sales read a little high, EBITDA "
         "is close), and BPSL sits as its own line so consolidated EBITDA still ties. Below EBITDA those two "
         "quarters include BPSL.", ""),
        ("The JV is equity-accounted (share of profit below EBITDA) and valued as a stake at JFE's purchase price; "
         "it is never inside EBITDA.", ""),
        ("", ""),
        ("Forecast mechanics", "h"),
        ("NSR/t moves with HRC (pass-through and lag on Inputs). Over the seven quarterly changes in the "
         "history, NSR moved 0.95 rupees per rupee of HRC in the same quarter, so 1.0 with no lag is the default; "
         "a one-quarter lag fitted worse. Cost/t moves with coking coal and iron ore in "
         "rupees, lagged a quarter, at specs/entities/steel.yaml's intensities (coal 0.70 t/t; ore 1.70 t/t x 70% "
         "bought at market x 1.0 pass-through of NMDC's notified fines price, time-weighted per quarter; PM 2026-11-03. "
         "Seaborne 62% stays on Inputs as a cross-check only: it fell 2% in Q1FY27 while NMDC rose 18%).", ""),
        (README_DECK + " Overseas segments, the plugs and other income roll the trailing four quarters; the JV share rolls "
         "the trailing two.", ""),
        ("Volume = capacity x utilisation x sales/production. Dolvi Phase-III (+5mtpa) commissions by Sep'27 and "
         "ramps from Q3FY28; Kadapa (+1mtpa) in FY29. D&A scales with capacity; finance cost is a rate on "
         "opening gross debt (no circularity). Capex follows the fiscal-year plan on Inputs.", ""),
        ("Your two numbers for the visible quarter go in the yellow 'PM view' rows on Segments. The following "
         "quarter rolls from your cost, so a view carries forward; later quarters stay mechanical.", ""),
        ("", ""),
        (README_FWD_HEAD, "h"),
    ]
    for k, (sid, unit) in COMMODITIES.items():
        if k in sp:
            L.append((f"{sid}: {sp[k]['fwd']:,} {unit}. {fwd_note(sp[k])}", ""))
    L += [("", ""),
          ("Not used", "h"),
          ("No broker model's numbers are in this file. Every actual is from JSW's results decks (Sources sheet).", "")]
    return L


# --------------------------------------------------------------------------
# recalc through Excel (openpyxl writes no cached values)
# --------------------------------------------------------------------------
def recalc(path: pathlib.Path) -> None:
    ps = (f"$xl = New-Object -ComObject Excel.Application; $xl.Visible = $false; $xl.DisplayAlerts = $false; "
          f"try {{ $wb = $xl.Workbooks.Open('{path}'); $xl.CalculateFull(); $wb.Save(); $wb.Close($true) }} "
          f"finally {{ $xl.Quit(); [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($xl) }}")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=180)


def errors(path: pathlib.Path) -> list[str]:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    bad = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("#"):
                    bad.append(f"{ws.title}!{c.coordinate} {c.value}")
    return bad


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def selftest() -> int:
    fails = 0

    def chk(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))

    print("steel_model selftest")
    qs = quarters("Q3FY25", "Q4FY29")
    chk("quarter count Q3FY25..Q4FY29", len(qs), 18)
    chk("Q4 rolls the fiscal year", qs[1:3], ["Q4FY25", "Q1FY26"])
    chk("FY of Q1FY27", fy_of("Q1FY27"), "FY27")
    chk("Q1FY27 is Apr-Jun 2026", quarter_dates("Q1FY27"), ("2026-04-01", "2026-06-30"))
    chk("Q4FY26 is Jan-Mar 2026", quarter_dates("Q4FY26"), ("2026-01-01", "2026-03-31"))
    chk("Q3FY25 is Oct-Dec 2024", quarter_dates("Q3FY25"), ("2024-10-01", "2024-12-31"))
    try:
        quarters("Q3FY25", "Q5FY30")
        chk("unreachable last quarter refused", "accepted", "refused")
    except ValueError:
        chk("unreachable last quarter refused", "refused", "refused")
    # cross-foot: acceptance on a footed fixture, rejection on one wrong digit
    q = ["Q1FY27"]
    fx = {"consolidated": {k: {q[0]: v} for k, v in dict(
            reported_ebitda=9383, other_income=724, finance_cost=1712, depreciation=2137,
            jv_share=-98, exceptional=0, pbt=6160, tax=1464, pat=4696).items()},
          "india": {"adj_ebitda_cr": {q[0]: 9096}}}
    chk("footed fixture accepted", crossfoot(fx), [])
    fx["consolidated"]["depreciation"][q[0]] = 2173          # a transposed digit
    chk("transposed digit refused", len(crossfoot(fx)), 1)
    # step series (NMDC circulars): time-weighted, not the mean of circular dates.
    # Q2FY27 fixture: 4,850 in force from 1-Jun, 4,700 from 10-Jul, 4,500 from
    # 8-Aug and again 9-Sep. Days: (9 x 4,850 + 29 x 4,700 + 54 x 4,500) / 92 = 4,597.
    # The mean of the in-quarter circular dates (4,700 / 4,500 / 4,500) is 4,567.
    with tempfile.TemporaryDirectory() as td:
        db = pathlib.Path(td) / "t.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE prices (entity_id TEXT, date TEXT, close REAL)")
        con.executemany("INSERT INTO prices VALUES ('nmdc_fines_inr', ?, ?)",
                        [("2026-06-01", 4850), ("2026-07-10", 4700), ("2026-08-08", 4500), ("2026-09-09", 4500)])
        con.commit(); con.close()
        h = commodity_history(["Q4FY26", "Q2FY27"], db)
        chk("NMDC quarter is time-weighted", h["nmdc"]["q"]["Q2FY27"], round((9 * 4850 + 29 * 4700 + 54 * 4500) / 92, 2))
        chk("a quarter before the first circular has no price", "Q4FY26" in h["nmdc"]["q"], False)
        chk("a series with no rows is skipped, not a crash", "hrc" in h, False)
    # forecast basis: the current quarter's weekday average to date, not the last print
    chk("2-Nov-2026 is Q3FY27", current_quarter("2026-11-02"), "Q3FY27")
    chk("15-Jan-2027 is Q4FY27", current_quarter("2027-01-15"), "Q4FY27")
    chk("1-Apr-2026 is Q1FY27", current_quarter("2026-04-01"), "Q1FY27")
    with tempfile.TemporaryDirectory() as td:
        db = pathlib.Path(td) / "t.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE prices (entity_id TEXT, date TEXT, close REAL)")
        # Q2: Fri 25-Sep 60,000; Q3: Thu 1-Oct 64,000, Fri 2-Oct 65,000,
        # Sat 3-Oct 99,999 (forward-filled weekend, must not count), Mon 5-Oct 63,000
        con.executemany("INSERT INTO prices VALUES ('hrc_india_inr', ?, ?)",
                        [("2026-09-25", 60000), ("2026-10-01", 64000), ("2026-10-02", 65000),
                         ("2026-10-03", 99999), ("2026-10-05", 63000)])
        con.commit(); con.close()
        h = commodity_history(["Q2FY27", "Q3FY27", "Q4FY27"], db)["hrc"]
        chk("forecast holds the quarter-to-date weekday average", (h["fwd"], h["fwd_q"], h["fwd_days"]),
            (64000.0, "Q3FY27", 3))
        chk("not the last print", h["fwd"] != h["spot"], True)
        # deck_ops on a tiny built-shaped workbook
        wbt = Workbook()
        wi = wbt.active
        wi.title = "Inputs"
        for i, (q, s) in enumerate((("Q2FY27", "F"), ("Q3FY27", "F"), ("Q4FY27", "F"))):
            wi.cell(4, FIRST_COL + i, q)
            wi.cell(6, FIRST_COL + i, s)
        wi.cell(10, 1, "Commodity deck: old title")
        wi.cell(12, 3, 63000)
        wbt.defined_names["Spot_hrc"] = DefinedName("Spot_hrc", attr_text="Inputs!$C$12")
        ops, said = deck_ops(wbt, {"hrc": h})
        cells = {(o["cell"], o["mode"]): o["value"] for o in ops}
        chk("forecast cell takes the average", cells.get(("C12", "set")), 64000.0)
        chk("a finished quarter's empty actual is filled", cells.get(("D12", "set")), 60000.0)
        chk("and the PM is told to flip it", any("flip Q2FY27 to A" in s for s in said), True)
        chk("the running quarter's actual is left empty", ("E12", "set") in cells, False)
        chk("the deck title is rewritten", cells.get(("A10", "set")), DECK_TITLE)
        wi.cell(12, FIRST_COL, 59000)                      # a typed actual
        ops2, _ = deck_ops(wbt, {"hrc": h})
        chk("a filled actual is never overwritten", any(o["cell"] == "D12" for o in ops2), False)
    cfgp = REPO / "data/analysis/cases/jsw_steel/model_config.json"
    if cfgp.exists():
        chk("JSW config cross-foots", crossfoot(json.loads(cfgp.read_text(encoding="utf-8"))), [])
    print(f"{'PASS' if not fails else 'FAIL'} ({fails} failures)")
    return 1 if fails else 0


def deck_ops(wb, hist: dict) -> tuple[list[dict], list[str]]:
    """The cell edits that bring a built workbook's deck up to the store, and a
    line per change. Writes only the forecast-basis cells (column C of each
    Spot_* row), a FINISHED quarter's empty actual cell, and the two texts that
    describe the basis. Never an override cell, never a filled actual."""
    ws = wb["Inputs"]
    qs, status = [], []
    c = FIRST_COL
    while ws.cell(4, c).value:
        qs.append(ws.cell(4, c).value)
        status.append(ws.cell(6, c).value)
        c += 1
    ops, said = [], []

    def put(sheet, cell, value, mode="set"):
        ops.append({"sheet": sheet, "cell": cell, "mode": mode, "value": value})

    for key, h in hist.items():
        nm = f"Spot_{key}"
        if nm not in wb.defined_names:
            continue
        row = int(re.search(r"\$(\d+)$", wb.defined_names[nm].attr_text).group(1))
        old = ws.cell(row, 3).value
        if old != h["fwd"]:
            put("Inputs", f"C{row}", h["fwd"])
            said.append(f"{nm}: {old} -> {h['fwd']}")
        put("Inputs", f"C{row}", fwd_note(h), "note")
        for i, q in enumerate(qs):
            done = q in h["q"] and quarter_dates(q)[1] < h["spot_date"]
            if done and ws.cell(row, FIRST_COL + i).value in (None, ""):
                put("Inputs", f"{get_column_letter(FIRST_COL + i)}{row}", h["q"][q])
                said.append(f"{nm}: {q} actual average {h['q'][q]:,}"
                            + ("" if status[i] == "A" else f" (quarter ended: flip {q} to A)"))
    for r in range(1, ws.max_row + 1):
        v = ws.cell(r, 1).value
        if isinstance(v, str) and v.startswith("Commodity deck:") and v != DECK_TITLE:
            put("Inputs", f"A{r}", DECK_TITLE)
    if "README" in wb.sheetnames:
        rd = wb["README"]
        sids = {COMMODITIES[k][0]: (k, COMMODITIES[k][1]) for k in hist if k in COMMODITIES}
        for r in range(1, rd.max_row + 1):
            v = rd.cell(r, 1).value
            if not isinstance(v, str):
                continue
            if v.startswith("Commodities hold"):
                tail = v[v.find(" Overseas"):] if " Overseas" in v else ""
                put("README", f"A{r}", README_DECK + tail)
            elif v == "Spot used for forecasts":
                put("README", f"A{r}", README_FWD_HEAD)
            elif ":" in v and v.split(":")[0] in sids:
                k, unit = sids[v.split(":")[0]]
                put("README", f"A{r}", f"{v.split(':')[0]}: {hist[k]['fwd']:,} {unit}. {fwd_note(hist[k])}")
    return ops, said


def refresh_deck(entity: str) -> int:
    """Re-read the store into an existing workbook through Excel (COM), so an
    open workbook takes the edit in its own window and nothing the PM typed is
    lost (openpyxl would drop every cached value)."""
    from openpyxl import load_workbook
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import model_view
    p = model_view.path_for(entity)
    if p is None:
        raise SystemExit(f"no model for {entity!r}")
    wb = load_workbook(p)
    ws = wb["Inputs"]
    qs, c = [], FIRST_COL
    while ws.cell(4, c).value:
        qs.append(ws.cell(4, c).value)
        c += 1
    ops, said = deck_ops(wb, commodity_history(qs))
    res = model_view.excel(entity, ops)
    print(f"{p.name}: {len(ops)} cells written" + (" (into the open window)" if res.get("attached") else ""))
    for s in said or ["deck values unchanged; notes refreshed"]:
        print("  " + s)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh-deck", metavar="ENTITY",
                    help="write the current quarter's average to date into an existing workbook, via Excel")
    ap.add_argument("--config", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--force", action="store_true", help="overwrite an existing workbook (loses its edits)")
    ap.add_argument("--recalc", action="store_true", help="open in Excel over COM, recalculate, save, check errors")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.refresh_deck:
        return refresh_deck(a.refresh_deck)
    if not (a.config and a.out):
        ap.error("--config and --out are required")
    if a.out.exists() and not a.force:
        raise SystemExit(f"{a.out} exists and may carry your edits; pass --force to rebuild it")
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    qs = quarters(cfg["first_quarter"], cfg["last_quarter"])
    hist = commodity_history(qs)
    info = build(cfg, a.out, hist)
    print(f"wrote {a.out}: {info['quarters']} quarters, {info['actual']} actual")
    if a.recalc:
        recalc(a.out.resolve())
        bad = errors(a.out)
        print(f"recalculated in Excel; {len(bad)} error cells" + ("".join(f"\n  {b}" for b in bad[:30])))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
