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
    # North European HRC (Kotak metals pack col 34, "Germany HRC US$/t"): the
    # price an overseas segment built as volume x price reads (Tata's
    # Netherlands, PM 2026-11-05). Deck rows appear only for a config whose
    # `driven` block names it, so a JSW build is untouched.
    "hrc_de": ("hrc_germany", "USD/t"),
}
# The India deck every model carries; a driven overseas segment adds its own
# series (DRIVER_SERIES) behind it. Anything else a config names is refused.
BASE_DECK = ("hrc", "cc", "io", "usdinr", "nmdc")
DRIVER_SERIES = {"hrc_de": ("HRC Germany, North Europe", "USD/t", "mn"),
                 "cc": None, "io": None}          # cc/io are already on the base deck


def deck_keys(cfg: dict) -> list[str]:
    """Commodity keys this config's workbook shows: the base deck, then every
    series a driven overseas segment reads. Refuses a key with no series."""
    out = list(BASE_DECK)
    for s in cfg.get("fx_segments", []):
        D = s.get("driven")
        if not D:
            continue
        for k in [D["price"]["key"]] + [c["key"] for c in D["costs"]]:
            if k not in DRIVER_SERIES:
                raise SystemExit(f"{s['name']}: driven series '{k}' is not one steel_model knows "
                                 f"({', '.join(DRIVER_SERIES)})")
            if k not in out:
                out.append(k)
    return out
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
    "date": "dd-mmm-yyyy",
    "m": "0.0",
    "w": '0.000;(0.000);"-"',
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


def cnote(cfg: dict, key: str, default: str) -> str:
    """A company-specific comment: the config's `notes` entry, else the default (JSW's text,
    so a JSW rebuild reads as it always has)."""
    return cfg.get("notes", {}).get(key, default)


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
# the rolling 12-month-forward block (PM, 2026-11-05: "Make a second block")
# --------------------------------------------------------------------------
# The block above values at a FIXED fiscal year-end (B4 valuation year, B5 the
# net-debt quarter one year earlier), so its horizon shrinks every month and
# jumps a year when B4/B5 are retyped. This one always values 12 months after
# the price date: EBITDA = each quarter's adjusted EBITDA x the share of its
# DAYS inside [valuation date, +12 months), net debt interpolated straight-line
# between the quarter-ends either side of the valuation date. Every quarter's
# cash is in exactly one place: before the date it has moved net debt, after
# it it is EBITDA - the rule the fixed block keeps by pairing FY28 with
# Q4FY27. The fixed block is untouched and stays the one model_view reads.
ROLL_TITLE = "Rolling 12-month-forward target: the date moves with the price date"
ROLL_TP = "Expected price, rolling 12 months forward (Rs)"
# Rows of the fixed block this block links to. add_rolling() checks the labels
# before writing into a live workbook, so a moved row refuses rather than
# linking to the wrong number.
ROLL_LINKS = {"A5": "Net debt as of (quarter)", "A12": "Core EV today: market cap + net debt - JV stake (Rs cr)",
              "A18": "Core multiple, ex JV (x)", "A22": "Plus: CWIP for capacity not yet earning (Rs cr)"}
HELP_COL = 7                       # the quarter table sits in G..N beside the block


def rolling_cells(top: int, n: int, adj: str, nd: str, div: str) -> list[dict]:
    """Every cell of the block, as {cell, value, fmt, color, bold, italic, size,
    fill, wrap}. One list for both writers: the builder (openpyxl) and
    add_rolling() (Excel over COM into a live workbook). `adj`, `nd`, `div`
    are the one-row ranges of adjusted EBITDA (P&L), net debt and dividends
    paid (Cash flow); `n` is the number of quarters in them."""
    cells = []
    h0, h1 = top + 2, top + 1 + n                       # quarter table rows
    G, H, I, J, K, L, M, N = (get_column_letter(HELP_COL + k) for k in range(8))
    Ir, Kr, Gr = f"${I}${h0}:${I}${h1}", f"${K}${h0}:${K}${h1}", f"${G}${h0}:${G}${h1}"

    def put(ref, value, fmt=None, color=None, inp=False, **kw):
        if color is None:
            if isinstance(value, str) and value.startswith("="):
                color = GREEN if re.search(r"!|Qrow|CMP|Shares_mn|JV_stake", value) else BLACK
            elif inp:
                color = BLUE
        cells.append({"cell": ref, "value": value, "fmt": fmt, "color": color, **kw})

    put(f"A{top}", ROLL_TITLE, bold=True)
    rows = [   # key, label, formula (B), fmt, yellow input?, note (E)
        ("p", "Price date", "=IF(ISNUMBER(CMP_date),CMP_date,DATE(VALUE(LEFT(CMP_date,4)),"
         "VALUE(MID(CMP_date,6,2)),VALUE(MID(CMP_date,9,2))))", "date", False,
         "CMP_date on Inputs, which the orchestrator's day-end run rewrites with each close."),
        ("ov", "Valuation date override (blank = 12 months after the price date)", None, "date", True,
         "Type a date to value at a fixed point instead (e.g. 31-Mar-2028). Blank rolls with the price date."),
        ("d", "Valuation date used", "=IF(ISNUMBER({ov}),{ov},EDATE({p},12))", "date", False,
         "Moves with every price update, so the target never ages and never jumps a year when the fixed "
         "block's valuation year is retyped."),
        ("hz", "Horizon from the price date (months)", "=ROUND(({d}-{p})/365.25*12,1)", "m", False, ""),
        ("we", "EBITDA window: 12 months from the valuation date, ending", "=EDATE({d},12)-1", "date", False, ""),
        ("cov", "Months of that window inside the model",
         "=ROUND((MIN({we}+1,MAX(" + Ir + ")+1)-{d})/({we}+1-{d})*12,1)", "m", False,
         "Must read 12. Less means the window runs past the model's last quarter: extend last_quarter in "
         "the config (a rebuild, so carry your yellow cells across)."),
        ("e", "Adjusted EBITDA in that window (Rs cr)",
         f"=SUMPRODUCT(${J}${h0}:${J}${h1},${N}${h0}:${N}${h1})", "cr", False,
         f"Each quarter's adjusted EBITDA x the share of its days inside the window (column {N}). "
         "The two quarters the window cuts through are split by days."),
        ("nd", "Net debt at the valuation date (Rs cr)",
         "=IF(INDEX(IR,MATCH({d},IR,1))={d},INDEX(KR,MATCH({d},IR,1)),"
         "INDEX(KR,MATCH({d},IR,1))+(INDEX(KR,MATCH({d},IR,1)+1)-INDEX(KR,MATCH({d},IR,1)))"
         "*({d}-INDEX(IR,MATCH({d},IR,1)))/(INDEX(IR,MATCH({d},IR,1)+1)-INDEX(IR,MATCH({d},IR,1))))",
         "cr", False, "Straight line between the quarter-end net debt either side of the date. Each quarter's "
         "cash sits in one place only: before the date it has already moved net debt, after it it is the "
         "EBITDA above."),
        ("ntm", "Adjusted EBITDA, 12 months from the price date (Rs cr)",
         f"=SUMPRODUCT(${J}${h0}:${J}${h1},${M}${h0}:${M}${h1})", "cr", False, f"Weights in column {M}."),
        ("mt", "Core EV / EBITDA today, on the next 12 months", "=B12/{ntm}", "x2", False,
         "Today's core EV (the block above) on the same rolling basis, to set beside the target multiple."),
        ("m", "Target core multiple, ex JV (x)", "=B18", "x2", True,
         "Linked to the core multiple above. A house multiple is a 12-month-forward multiple at the house's "
         "own target date, struck on one fiscal year's consensus; applied here it assumes that forward "
         "multiple still holds at the later date. Type over the link to re-strike it on this window."),
        ("ev", "Target core EV (Rs cr)", "={m}*{e}", "cr", False, ""),
        ("lnd", "Less: net debt at the valuation date (Rs cr)", "=-{nd}", "cr", False, ""),
        ("jv", "Plus: JV stake (Rs cr)", "=JV_stake", "cr", False, ""),
        ("cw", "Plus: CWIP (Rs cr)", "=B22", "cr", False, "Linked to the CWIP line above."),
        ("eq", "Equity value (Rs cr)", "=SUM({ev}:{cw})", "cr", False, ""),
        ("tp", ROLL_TP, "={eq}*10/Shares_mn", "rs", False, ""),
        ("up", "Upside vs CMP, rolling", "={tp}/CMP-1", "pct", False, ""),
        ("dv", "Dividends paid before the valuation date (Rs/share)",
         "=-SUMPRODUCT(" + f"${L}${h0}:${L}${h1}" + ",--(IR>{p}),--(IR<={d}))*10/Shares_mn", "rs", False,
         "Forecast dividends in quarters ending after the price date and on or before the valuation date. "
         "The price above is ex those dividends; the holder has them."),
        ("tr", "Total return to the valuation date, incl. those dividends", "=({tp}+{dv})/CMP-1", "pct", False, ""),
        ("fd", "For comparison, the fixed block's price is for", "=INDEX(IR,MATCH(B5,GR,0))", "date", False,
         "The end of the quarter in 'Net debt as of' above."),
        ("fh", "Fixed block's horizon from the price date (months)", "=ROUND(({fd}-{p})/365.25*12,1)", "m",
         False, ""),
    ]
    at = {k: f"B{top + 1 + i}" for i, (k, *_) in enumerate(rows)}
    for i, (k, lab, f, fmt, inp, note) in enumerate(rows):
        r = top + 1 + i
        bold = k in ("tp", "up")
        put(f"A{r}", lab, bold=bold)
        v = None
        if f:
            v = f
            for key, ref in at.items():
                v = v.replace("{" + key + "}", ref)
            v = v.replace("IR", Ir).replace("KR", Kr).replace("GR", Gr)
        put(f"B{r}", v, fmt, bold=bold, fill="FFFF00" if inp else None, inp=inp)
        if note:
            put(f"E{r}", note, italic=True, size=8, color="595959")
    # the quarter table, one row per quarter of the model
    put(f"{G}{top}", "Quarters behind the rolling block", bold=True)
    for k, h in enumerate(("Quarter", "Starts", "Ends", "Adj EBITDA", "Net debt at end", "Dividends paid",
                           "Weight: 12m from price date", "Weight: 12m from valuation date")):
        put(f"{get_column_letter(HELP_COL + k)}{top + 1}", h, bold=True, size=8, wrap=True)
    for k in range(1, n + 1):
        r = h0 + k - 1
        q = f"{G}{r}"
        put(q, f"=INDEX(Qrow,{k})")
        put(f"{H}{r}", f"=DATE(1999+VALUE(RIGHT({q},2)),3*VALUE(MID({q},2,1))+1,1)", "date")
        put(f"{I}{r}", f"=DATE(1999+VALUE(RIGHT({q},2)),3*VALUE(MID({q},2,1))+4,1)-1", "date")
        put(f"{J}{r}", f"=INDEX({adj},{k})", "cr")
        put(f"{K}{r}", f"=INDEX({nd},{k})", "cr")
        put(f"{L}{r}", f"=INDEX({div},{k})", "cr")
        put(f"{M}{r}", f"=MAX(0,MIN({I}{r}+1,EDATE({at['p']},12))-MAX({H}{r},{at['p']}))/({I}{r}+1-{H}{r})", "w")
        put(f"{N}{r}", f"=MAX(0,MIN({I}{r}+1,{at['we']}+1)-MAX({H}{r},{at['d']}))/({I}{r}+1-{H}{r})", "w")
    return cells


def window_weights(qs: list[str], start: str) -> dict:
    """Python oracle for the block's day weights: each quarter's share of its
    days inside [start, start + 12 months). The selftest and the live check
    compare the sheet against this."""
    d0 = dt.date.fromisoformat(start)
    d1 = d0.replace(year=d0.year + 1)
    out = {}
    for q in qs:
        a, b = (dt.date.fromisoformat(x) for x in quarter_dates(q))
        b1 = b + dt.timedelta(days=1)
        w = max(0, (min(b1, d1) - max(a, d0)).days) / (b1 - a).days
        if w:
            out[q] = w
    return out


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def driven_segment(seg: "Sheet", s: dict, a_rows: dict, A) -> tuple[int, int]:
    """An overseas segment as VOLUME x PRICE (PM, 2026-11-05, Tata's Netherlands).

    NSR/t and cost/t each move from a BASE: the average of the last four
    reported quarters, with the drivers' average over the SAME four quarters.
    A forecast NSR = base NSR + pass-through x (lagged HRC - base HRC), and
    cost/t = base cost + sum(intensity x market share x (lagged price - base
    price)). The last quarter alone is NOT the base because NL's Q1FY27 carries
    a plant outage (EBITDA EUR4mn on 1.40mt): rolling from it would hold the
    outage's cost per tonne in every forecast quarter. A PM view (EBITDA/t) for
    a quarter re-bases the next one, as India's does. Returns the revenue and
    EBITDA rows in the segment's own currency."""
    K, ccy, D, dr = s["key"].upper(), s["ccy"], s["driven"], s["_drv"]
    for need in ("sales_mt", "revenue_mn", "ebitda_mn"):
        if need not in a_rows:
            raise SystemExit(f"{s['name']}: a driven segment needs {need} actuals")
    ws, qs = seg.ws, seg.qs
    seg.section(f"{s['name']}: the PM's view for the visible quarter (yellow, optional)")
    r_pms = seg.row("PM view: sales", "mt", lambda i, q, r: None, fmt="mt")
    r_pme = seg.row("PM view: EBITDA per tonne", f"{ccy}/t", lambda i, q, r: None, fmt="t")
    known = s.get("known_sales", {})
    for r, fmt in ((r_pms, "mt"), (r_pme, "t")):
        for i, q in enumerate(qs):
            if q in s["sales_mt"]:
                continue
            c = ws.cell(r, FIRST_COL + i)
            c.fill, c.number_format, c.font = YELLOW, FMT[fmt], Font(name=FONT, color=BLUE)
            # A quarter that has ended with deliveries published but no result
            # (NL Q2FY27: the company's production update): the published
            # volume, prefilled, as the deck does for an ended quarter's prices.
            if r == r_pms and q in known:
                c.value = known[q]["value"]
                c.comment = Comment(f"{known[q]['source']}. Clear it to fall back to the trailing four.", "steel_model")
    seg.section(f"{s['name']}: model (volume x price; bases are the last four reported quarters)")
    r0 = seg.r
    rb = r0 + 10                                    # 8 model rows, then a section (2 rows)
    rbN, rbC, rbP = rb, rb + 1, rb + 2
    rbK = {k: rb + 3 + j for j, (k, _) in enumerate(dr["costs"])}
    aS, aR, aE = a_rows["sales_mt"], a_rows["revenue_mn"], a_rows["ebitda_mn"]
    pe = dr["price"]
    r_sales = seg.row("Sales", "mt",
                      lambda i, q, r: (f'=IF({A(i)},{col(i)}{aS},IF({col(i)}{r_pms}<>"",{col(i)}{r_pms},'
                                       + (trailing(i, r)[1:] if i >= 4 else "0") + "))"),
                      fmt="mt", bold=True,
                      comment="Forecast: your view (yellow, above) if typed, else the trailing four quarters.")
    s["_sales_row"] = r_sales
    r_nsr = seg.row("Net sales realisation (NSR)", f"{ccy}/t",
                    lambda i, q, r: (f"=IF({A(i)},{col(i)}{aR}/{col(i)}{aS},"
                                     + (f"{prev(i, rbN)}+{K}_PricePT*(Inputs!{col(i)}{pe}-{prev(i, rbP)}))" if i else "0)")),
                    fmt="t",
                    comment=f"Forecast = base NSR + pass-through x (lagged {D['price']['label']} in {ccy} - its base).")

    def cost_terms(i):
        return "+".join(f"{K}_{k}_Int*{K}_{k}_Mkt*(Inputs!{col(i)}{er}-{prev(i, rbK[k])})" for k, er in dr["costs"])
    r_cost = seg.row("Cost per tonne (model)", f"{ccy}/t",
                     lambda i, q, r: (f"=IF({A(i)},({col(i)}{aR}-{col(i)}{aE})/{col(i)}{aS},"
                                      + (f"{prev(i, rbC)}+{cost_terms(i)})" if i else "0)")),
                     fmt="t",
                     comment="Forecast = base cost + sum over the cost drivers of intensity x share bought at market "
                             f"x (lagged price in {ccy} - its base).")
    r_eptm = seg.row("EBITDA per tonne (model)", f"{ccy}/t", lambda i, q, r: f"={col(i)}{r_nsr}-{col(i)}{r_cost}",
                     fmt="t", kind="local")
    r_ept = seg.row("EBITDA per tonne (used)", f"{ccy}/t",
                    lambda i, q, r: f'=IF({A(i)},{col(i)}{r_eptm},IF({col(i)}{r_pme}<>"",{col(i)}{r_pme},{col(i)}{r_eptm}))',
                    fmt="t", bold=True)
    r_costu = seg.row("Cost per tonne (used)", f"{ccy}/t", lambda i, q, r: f"={col(i)}{r_nsr}-{col(i)}{r_ept}",
                      fmt="t", kind="local")
    r_rv = seg.row(f"Revenue ({ccy} mn) = sales x NSR", f"{ccy} mn",
                   lambda i, q, r: f"=IF({A(i)},{col(i)}{aR},{col(i)}{r_sales}*{col(i)}{r_nsr})", fmt="mn")
    r_ev = seg.row(f"EBITDA ({ccy} mn) = sales x EBITDA/t", f"{ccy} mn",
                   lambda i, q, r: f"=IF({A(i)},{col(i)}{aE},{col(i)}{r_sales}*{col(i)}{r_ept})", fmt="mn", bold=True)
    seg.section(f"{s['name']}: bases (average of the last four reported quarters; a PM view re-bases)")
    assert seg.r == rb, (seg.r, rb)

    def avg4(i, row, sheet=""):
        return f"AVERAGE({sheet}{col(max(0, i - 3))}{row}:{sheet}{col(i)}{row})"

    def base(label, own, used_cell):
        return seg.row(label, f"{ccy}/t",
                       lambda i, q, r: (f'=IF({A(i)},{own(i)},IF({col(i)}{r_pme}<>"",{used_cell(i)},'
                                        + (prev(i, r) if i else "0") + "))"),
                       fmt="t", kind="local")
    base("Base NSR", lambda i: avg4(i, r_nsr), lambda i: f"{col(i)}{r_nsr}")
    base("Base cost per tonne", lambda i: avg4(i, r_cost), lambda i: f"{col(i)}{r_costu}")
    base(f"Base {D['price']['label']}", lambda i: avg4(i, pe, "Inputs!"), lambda i: f"Inputs!{col(i)}{pe}")
    for c_, (k, er) in zip(D["costs"], dr["costs"]):
        base(f"Base {c_['label']}", lambda i, er=er: avg4(i, er, "Inputs!"), lambda i, er=er: f"Inputs!{col(i)}{er}")
    return r_rv, r_ev


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
    # PM, 2026-11-04, after reading JPM's JSW model: "Update our debt method like
    # them, dividends and all." JPM holds gross debt flat, lets cash balance the
    # sheet, and takes dividends (a payout of PAT) and working capital (turnover
    # days) out of operating cash. Ours had neither, so every rupee of FCF repaid
    # debt and net debt fell 44,482 -> 31,579 by FY28 against JPM's flat ~55k.
    const("Payout", "Dividend payout: share of the previous FY's PAT, paid in Q2",
          C.get("dividend_payout", {"value": 0.15, "unit": "%",
                                    "source": "JPM JSW model (Financial Tables Annual, payout ratio FY27E-FY29E 0.15). "
                                              "JSW pays its final dividend after the July AGM, so the year's payout "
                                              "lands in the next fiscal year's Q2."}),
          "pct", yellow=True)
    const("WC_days", "Net working capital, days of revenue",
          C.get("wc_days", {"value": 58, "unit": "days",
                            "source": "JPM JSW model, FY26 consolidated: inventories 327,650 + debtors 112,600 - "
                                      "creditors 146,660 = 293,590 Rs mn on sales 1,854,700 = 57.8 days (FY27E 57.4)."}),
          None, yellow=True)

    inp.section("Driver sensitivities (India)")
    S = cfg["sensitivities"]
    for key, label, fmt, y in (
            ("hrc_pass_through", "NSR pass-through of the HRC move", "fx", True),
            ("hrc_lag_q", "HRC lag", None, True),
            ("cc_intensity", "Coking coal per tonne of steel", "fx", False),
            ("cc_market_pct", "Coking coal bought at seaborne prices", "pct", False),
            ("cc_lag_q", "Coking coal lag", None, True),
            ("io_intensity", "Iron ore per tonne of steel", "fx", False),
            ("io_market_pct", "Iron ore bought at market", "pct", False),
            ("io_pass_through", "Iron ore pass-through (of the NMDC fines move)", "fx", False),
            ("io_lag_q", "Iron ore lag", None, True),
            ("other_cost_drift", "Other cost drift", "t", True),
            ("util", "Utilisation (default for forecast quarters)", "pct", True),
            ("sales_to_prod", "Sales / production (default)", "fx", True),
            ("ramp_step", "New capacity ramp per quarter", "pct", False)):
        # cc_market_pct (2026-11-04, Tata Steel): the share of coking coal bought at
        # the seaborne benchmark. JSW imports all of it (1.0, the default, so a JSW
        # config without the key builds exactly as before); Tata imports ~78% and
        # the captive West Bokaro / Jharia balance is cost-based, so a seaborne move
        # reaches 0.70 x 0.78 of a tonne, not 0.70.
        entry = S.get(key) if key != "cc_market_pct" else S.get(key, {
            "value": 1.0, "unit": "%", "source": "default: all coking coal imported at the seaborne benchmark"})
        const(key.replace("_q", "").title().replace("_", ""), label, entry, fmt, yellow=y)
    # the names above: HrcPassThrough, HrcLag, CcIntensity, CcMarketPct, CcLag, IoIntensity,
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
    # every overseas currency the segments price in (EUR for JSW's Italy; EUR and
    # GBP for Tata's Netherlands and UK), from the config's fx_history
    for key in fxh:
        if key != "usdinr":
            ccy = key[:3].upper()
            deck.append((key, f"{ccy}INR", f"INR/{ccy}", "fx"))
    # series a driven overseas segment reads (Tata NL: German HRC), after the FX rows
    for key in deck_keys(cfg)[len(BASE_DECK):]:
        deck.append((key, *DRIVER_SERIES[key]))
    for key, label, unit, fmt in deck:
        if key in hist:
            h, spot, note = hist[key]["q"], hist[key]["fwd"], fwd_note(hist[key])
        else:
            h = {q: fxh[key][q] for q in qs if q in fxh[key]}
            spot, note = fxh[key]["spot"], f"Spot {cfg['built']} ({fxh[key]['source']})"
        r_act = inp.row(f"{label}: actual quarterly average", unit, {q: h.get(q) for q in qs if q in act}, fmt=fmt)
        r_ovr = inp.row(f"{label}: override (your view)", unit, lambda i, q, r: None, fmt=fmt)
        asof = hist[key]["spot_date"] if key in hist else fxh[key].get("spot_date", cfg["built"])
        for i, q in enumerate(qs):
            if q not in act:
                ws.cell(r_ovr, FIRST_COL + i).fill = YELLOW
                ws.cell(r_ovr, FIRST_COL + i).number_format = FMT[fmt]
                ws.cell(r_ovr, FIRST_COL + i).font = Font(name=FONT, color=BLUE)
                # A quarter that has ENDED but not been reported (Tata on 4-Nov:
                # Q2FY27 prints 7-Nov) has a known average. Holding the running
                # quarter's average there instead would price Q2 off Q3's tape.
                # Prefilled as an override, so flipping the status to A later
                # swaps in the typed actual and nothing else changes.
                if q in h and quarter_dates(q)[1] < asof:
                    ws.cell(r_ovr, FIRST_COL + i).value = h[q]
                    ws.cell(r_ovr, FIRST_COL + i).comment = Comment(
                        f"{q} has ended and is unreported: its own average, prefilled at build. "
                        "Clear it to fall back to the forward basis.", "steel_model")
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
            if q in hn["q"] and quarter_dates(q)[1] < cfg["built"]:     # a step series: its last circular date is not "today"
                ws.cell(r_nm_ovr, FIRST_COL + i).value = hn["q"][q]       # ended, unreported (see the deck)
    ws.cell(r_nm_act, 3, hn["spot"]).font = Font(name=FONT, color=BLUE)
    ws.cell(r_nm_act, 3).number_format = FMT["t"]
    ws.cell(r_nm_act, 3).comment = Comment(f"Latest NMDC circular {hn['spot_date']} (prices table, nmdc_fines_inr)", "steel_model")
    name(wb, "Spot_nmdc", f"Inputs!$C${r_nm_act}")
    r_nm_used = inp.row("NMDC fines: used", "Rs/t",
                        lambda i, q, r: f'=IF({col(i)}$6="A",{col(i)}{r_nm_act},IF({col(i)}{r_nm_ovr}<>"",{col(i)}{r_nm_ovr},Spot_nmdc))',
                        fmt="t", bold=True, kind="local")
    r_nm_eff = inp.row("NMDC fines, lagged (the ore cost driver)", "Rs/t ore",
                       lambda i, q, r: "=" + lagged(i, r_nm_used, "IoLag"), fmt="t", kind="local")

    # Overseas segments built as VOLUME x PRICE (PM, 2026-11-05, Tata's
    # Netherlands: "can we start with volume*price instead of direct forecast of
    # revenue? Add rows for EU's iron ore, HRC etc"). Each named price is turned
    # into the segment's own currency at the quarter's cross (USD per EUR =
    # EURINR / USDINR) and lagged; the Segments sheet moves NSR and cost per
    # tonne off these rows. A config with no `driven` block writes nothing here.
    for s in cfg["fx_segments"]:
        D = s.get("driven")
        if not D:
            continue
        K, ccy = s["key"].upper(), s["ccy"]
        inp.section(f"{s['name']}: drivers in {ccy} per tonne (volume x price sets its revenue and EBITDA)")
        const(f"{K}_PricePT", f"{D['price']['label']}: NSR pass-through of its move", D["price"]["pass_through"],
              "fx", yellow=True)
        const(f"{K}_PriceLag", f"{D['price']['label']}: lag", D["price"]["lag_q"], None, yellow=True)
        for c_ in D["costs"]:
            ck = c_["key"]
            const(f"{K}_{ck}_Int", f"{c_['label']} per tonne of steel sold", c_["intensity"], "fx")
            const(f"{K}_{ck}_Mkt", f"{c_['label']} bought at market prices", c_["market_pct"], "pct")
            const(f"{K}_{ck}_Lag", f"{c_['label']}: lag", c_["lag_q"], None, yellow=True)
        r_x = inp.row(f"USD per {ccy} (= {ccy}INR / USDINR)", f"USD/{ccy}",
                      lambda i, q, r, f=used[s["fx"]]: f"={col(i)}{f}/{col(i)}{used['usdinr']}",
                      fmt="w", kind="local")

        def eff(key, lag, label, rx=r_x, ccy=ccy):
            return inp.row(f"{label} in {ccy}, lagged", f"{ccy}/t",
                           lambda i, q, r: "=" + lagged(i, used[key], lag) + "/" + lagged(i, rx, lag),
                           fmt="t", kind="local")
        s["_drv"] = {"price": eff(D["price"]["key"], f"{K}_PriceLag", D["price"]["label"]),
                     "costs": [(c_["key"], eff(c_["key"], f"{K}_{c_['key']}_Lag", c_["label"])) for c_ in D["costs"]]}

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
                     comment=cnote(cfg, "util", "Actual quarters: India production x4 / capacity (memo; FY25 capacity was "
                             "lower and BF-3 was down Jul-Nov 2025). Forecast: type over the yellow cells."))
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
                             + (f"{prev(i, r_cost_used)}+CcIntensity*CcMarketPct*(Inputs!{col(i)}{r_cc_eff}-Inputs!{prev(i, r_cc_eff)})"
                                f"+IoIntensity*IoMarketPct*IoPassThrough*(Inputs!{col(i)}{r_nm_eff}-Inputs!{prev(i, r_nm_eff)})"
                                f"+OtherCostDrift)" if i else "0)")),
            fmt="t",
            comment="Forecast = last quarter's cost (as used) + coal intensity x share bought at market x change in lagged coal (Rs/t) "
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
        if s.get("driven"):
            r_rv, r_ev = driven_segment(seg, s, a_rows, A)
            fxrow = used[s["fx"]]
            fx_rev_rows.append(seg.row("Revenue", "Rs cr", lambda i, q, r, a=r_rv: f"={col(i)}{a}*Inputs!{col(i)}{fxrow}/10"))
            fx_e_rows.append(seg.row("EBITDA", "Rs cr", lambda i, q, r, a=r_ev: f"={col(i)}{a}*Inputs!{col(i)}{fxrow}/10",
                                     bold=True))
            continue
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
    # every overseas segment that reports tonnes (JSW: the US only; Tata: NL and UK)
    os_sales = [s["_sales_row"] for s in cfg["fx_segments"] if "_sales_row" in s]
    r_cons_sales = seg.row("Consolidated sales (India + overseas steel)", "mt",
                           lambda i, q, r: f"={col(i)}{r_sales_u}" + "".join(f"+{col(i)}{x}" for x in os_sales),
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
                  comment=cnote(cfg, "jv_share", "Forecast = trailing two quarters: FY26 proforma lines carry BPSL's pre-JV profit, "
                          "so a longer window would read the wrong business."))
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
                       comment=cnote(cfg, "capex_actual", "Blank where the decks give no quarterly figure (FY25 was 14,656; 9M FY26 was "
                               "10,018); the residual below then absorbs it."))
    r_wc_in = cf.row("Other cash flows: your view (beyond dividends and working capital)", "Rs cr",
                     lambda i, q, r: None,
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
    r_wcm = cf.row("Working capital movement", "Rs cr",
                   lambda i, q, r: (f"=IF({A(i)},0,-WC_days/365*4*('P&L'!{col(i)}{r_rev}-'P&L'!{prev(i, r_rev)}))"
                                    if i else "=0"),
                   comment="Forecast = -(WC_days / 365) x the change in annualised revenue (quarter x 4): JPM's "
                           "turnover-days method on one net figure. Revenue up ties cash up; revenue down releases "
                           "it. Actual quarters: inside the residual below.")
    r_div = cf.row("Dividends paid", "Rs cr",
                   lambda i, q, r: (f'=IF({A(i)},0,IF(LEFT({col(i)}$4,2)="Q2",-Payout*SUMIFS('
                                    f"'P&L'!$D${r_pat}:${last}${r_pat},FYrow,"
                                    f'"FY"&TEXT(VALUE(RIGHT({col(i)}$5,2))-1,"00")),0))'),
                   comment=cnote(cfg, "dividend", "Forecast = Payout x the previous fiscal year's PAT, paid in Q2 (JSW declares its final "
                           "dividend with Q4 results and pays after the July AGM). Actual quarters: inside the "
                           "residual below."))
    r_wc = cf.row("Working capital, dividends and other", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{prev(i, r_nd_a) if i else 'Open_ND'}-{col(i)}{r_nd_a}-{col(i)}{r_fcf},"
                                  f"{col(i)}{r_wcm}+{col(i)}{r_div}+N({col(i)}{r_wc_in}))",
                  comment=cnote(cfg, "residual", "Actual: whatever reconciles reported net debt (it holds the BPSL slump sale in Q4FY26, "
                          "JFE's second tranche in Q1FY27, dividends and undisclosed capex). Forecast: working "
                          "capital + dividends + the yellow row, negative for an outflow."))
    cf.section("Net debt")
    r_nd = cf.row("Net debt (end of quarter)", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_nd_a},"
                                  + (f"{prev(i, r)}" if i else "Open_ND") + f"-{col(i)}{r_fcf}-{col(i)}{r_wc})",
                  bold=True)
    r_cash = cf.row("Cash and equivalents", "Rs cr",
                    lambda i, q, r: (f"=IF({A(i)},{col(i)}{r_cash_a},"
                                     + (f"{prev(i, r_nd)}+{prev(i, r)}-{col(i)}{r_nd})" if i else "0)")),
                    comment="Forecast = the balancing item, JPM's method: gross debt is held flat at the last "
                            "reported level, so cash = opening gross debt - net debt. A negative figure means the "
                            "plan needs new borrowing.")
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
        "Forecast = Int_rate x OPENING gross debt / 4. Gross debt is held flat at the last reported level "
        "(JPM's method), so forecast finance cost is constant. "
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
         cnote(cfg, "jv_ev", "The JV is outside EBITDA, so it comes out of EV too; otherwise today's multiple carries it and the "
         "target adds it again (about Rs64/share of upside from nothing).")),
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
         cnote(cfg, "jv_stake", "JFE's price for 50% of JSW JFE Steel. Added once: the JV sits below EBITDA and is out of the core "
         "multiple above.")),
        ("Plus: CWIP for capacity not yet earning (Rs cr)", 0, "cr", "key",
         cnote(cfg, "cwip", "Capex already spent on projects that produce after the valuation year (Utkal, JVML Phase-II). Zero by "
         "default: the decks give no per-project spend to date.")),
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
    for c in rolling_cells(stb + 5, len(qs), rowsum("P&L", r_adj), nd_row, rowsum("Cash flow", r_div)):
        x = va[c["cell"]]
        x.value = c["value"]
        x.font = Font(name=FONT, color=c.get("color") or BLACK, bold=c.get("bold", False),
                      italic=c.get("italic", False), size=c.get("size") or 10)
        if c.get("fmt"):
            x.number_format = FMT[c["fmt"]]
        if c.get("fill"):
            x.fill = PatternFill("solid", fgColor=c["fill"])
        if c.get("wrap"):
            x.alignment = Alignment(wrap_text=True, vertical="top")
    for k in range(8):
        va.column_dimensions[get_column_letter(HELP_COL + k)].width = 12

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
    ]
    L += [(t, "") for t in cfg.get("readme_history") or [t for t, _ in [
        ("BPSL's steel business was deconsolidated on 27-Mar-2026 into JSW JFE Steel (50:50 with JFE). FY26 quarters "
         "use the company's own proforma ex-BPSL figures. Q3FY26 proforma is derived (FY26 PF less the other three "
         "quarters); its EBITDA agrees with reported-less-BPSL to Rs2cr. Q3FY25 and Q4FY25 are reported figures: the "
         "India segment is India Ops less BPSL's standalone numbers (revenue and sales read a little high, EBITDA "
         "is close), and BPSL sits as its own line so consolidated EBITDA still ties. Below EBITDA those two "
         "quarters include BPSL.", ""),
        ("The JV is equity-accounted (share of profit below EBITDA) and valued as a stake at JFE's purchase price; "
         "it is never inside EBITDA.", ""),
    ]]]
    L += [

        ("", ""),
        ("Forecast mechanics", "h"),
        (cnote(cfg, "readme_mechanics", "NSR/t moves with HRC (pass-through and lag on Inputs). Over the seven quarterly changes in the "
         "history, NSR moved 0.95 rupees per rupee of HRC in the same quarter, so 1.0 with no lag is the default; "
         "a one-quarter lag fitted worse. Cost/t moves with coking coal and iron ore in "
         "rupees, lagged a quarter, at specs/entities/steel.yaml's intensities (coal 0.70 t/t; ore 1.70 t/t x 70% "
         "bought at market x 1.0 pass-through of NMDC's notified fines price, time-weighted per quarter; PM 2026-11-03. "
         "Seaborne 62% stays on Inputs as a cross-check only: it fell 2% in Q1FY27 while NMDC rose 18%)."), ""),
        (README_DECK + " Overseas segments, the plugs and other income roll the trailing four quarters; the JV share rolls "
         "the trailing two.", ""),
        (cnote(cfg, "readme_volume", "Volume = capacity x utilisation x sales/production. Dolvi Phase-III (+5mtpa) commissions by Sep'27 and "
         "ramps from Q3FY28; Kadapa (+1mtpa) in FY29. D&A scales with capacity; finance cost is a rate on "
         "opening gross debt (no circularity). Capex follows the fiscal-year plan on Inputs."), ""),
        ("Your two numbers for the visible quarter go in the yellow 'PM view' rows on Segments. The following "
         "quarter rolls from your cost, so a view carries forward; later quarters stay mechanical.", ""),
        ("", ""),
        (README_FWD_HEAD, "h"),
    ]
    at = L.index((README_FWD_HEAD, "h")) - 1        # before the blank line that opens the price list
    for s in cfg.get("fx_segments", []):
        D = s.get("driven")
        if not D:
            continue
        costs = "; ".join(f"{c['label']} {c['intensity']['value']} t/t x {c['market_pct']['value']:.0%} at market, "
                          f"lag {c['lag_q']['value']}" for c in D["costs"])
        L.insert(at, (f"{s['name']} is volume x price, in {s['ccy']}: revenue = sales x NSR, EBITDA = sales x "
                      f"(NSR - cost per tonne). NSR moves {D['price']['pass_through']['value']} x the change in "
                      f"{D['price']['label']} (lag {D['price']['lag_q']['value']}); cost per tonne moves with {costs}. "
                      f"Every price is converted at the quarter's USD per {s['ccy']}. Both move from a base: the "
                      "average of the last four reported quarters, against the drivers' average over the same "
                      "quarters. Sales are your yellow view, else the trailing four. Sources for each constant are "
                      "beside it on Inputs.", ""))
        at += 1
    for k, (sid, unit) in COMMODITIES.items():
        if k in sp and k in deck_keys(cfg):
            L.append((f"{sid}: {sp[k]['fwd']:,} {unit}. {fwd_note(sp[k])}", ""))
    L += [("", ""),
          ("Not used", "h"),
          (cnote(cfg, "readme_not_used", "No broker model's numbers are in this file. Every actual is from JSW's results decks (Sources sheet)."), "")]
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
    # An ENDED but unreported quarter takes its own average as a prefilled
    # override (Tata on 4-Nov: Q2FY27 prints 7-Nov); the running quarter does
    # not (acceptance and rejection). Q2's override cell must carry Q2's HRC
    # average, and Q3's must stay empty so it reads the forward basis.
    cfgt = REPO / "data/analysis/cases/tata_steel/model_config.json"
    if cfgt.exists():
        from openpyxl import load_workbook
        ct = json.loads(cfgt.read_text(encoding="utf-8"))
        chk("Tata config cross-foots", crossfoot(ct), [])
        qt = quarters(ct["first_quarter"], ct["last_quarter"])
        ht = commodity_history(qt)                     # the other deck series from the store; HRC is the fixture
        ht["hrc"] = {"q": {"Q2FY27": 59800.0, "Q3FY27": 63984.0}, "spot": 63600.0, "spot_date": "2026-11-04",
                     "fwd": 63984.0, "fwd_q": "Q3FY27", "fwd_days": 24}
        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td) / "t.xlsx"
            build(ct, out, ht)
            wi = load_workbook(out)["Inputs"]
            r = next(r for r in range(1, wi.max_row + 1) if wi.cell(r, 1).value == "HRC India: override (your view)")
            c2, c3 = FIRST_COL + qt.index("Q2FY27"), FIRST_COL + qt.index("Q3FY27")
            chk("an ended, unreported quarter holds its own average", wi.cell(r, c2).value, 59800.0)
            chk("the running quarter is left to the forward basis", wi.cell(r, c3).value, None)
            r = next(r for r in range(1, wi.max_row + 1) if wi.cell(r, 1).value == "GBPINR: override (your view)")
            chk("a second overseas currency gets its own deck rows", wi.cell(r, c2).value, ct["fx_history"]["gbpinr"]["Q2FY27"])
            # a driven overseas segment (NL, volume x price): its price series gets
            # deck rows, revenue is sales x NSR on a forecast quarter, the ended
            # quarter's published deliveries are prefilled and the next is not
            if any(s.get("driven") for s in ct["fx_segments"]):
                labs = {wi.cell(r, 1).value for r in range(1, wi.max_row + 1)}
                chk("the driven price series has deck rows", "HRC Germany, North Europe: used" in labs, True)
                ws_ = load_workbook(out)["Segments"]
                rows = {ws_.cell(r, 1).value: r for r in range(1, ws_.max_row + 1)}
                # first draft of this check looked "Sales" up by label and got the
                # UK's row (two segments carry one); the NL rows sit 6 and 5 above
                rr = rows["Revenue (EUR mn) = sales x NSR"]
                rs, rn = rr - 6, rr - 5
                chk("the rows above revenue are sales and NSR", (ws_.cell(rs, 1).value, ws_.cell(rn, 1).value),
                    ("Sales", "Net sales realisation (NSR)"))
                c4 = FIRST_COL + qt.index("Q4FY27")
                L4 = col(c4 - FIRST_COL)
                chk("a forecast quarter's revenue is sales x NSR", ws_.cell(rr, c4).value.split(",")[-1],
                    f"{L4}{rs}*{L4}{rn})")
                rp = next(r for r in range(rs - 1, 0, -1) if ws_.cell(r, 1).value == "PM view: sales")
                known = next(s for s in ct["fx_segments"] if s.get("driven"))["known_sales"]
                chk("an ended quarter's published deliveries are prefilled", ws_.cell(rp, c2).value,
                    known.get("Q2FY27", {}).get("value"))
                chk("the running quarter is not", ws_.cell(rp, c3).value, None)
            va = load_workbook(out)["Valuation"]
            tr = next((r for r in range(1, va.max_row + 1) if va.cell(r, 1).value == ROLL_TITLE), None)
            chk("the builder writes the rolling block", tr is not None, True)
            if tr:
                chk("its quarter table covers every quarter", va.cell(tr + 1 + len(qt), HELP_COL).value,
                    f"=INDEX(Qrow,{len(qt)})")
    bad = {"fx_segments": [{"name": "X", "driven": {"price": {"key": "lme_zinc"}, "costs": []}}]}
    try:
        deck_keys(bad)
        chk("a driven series steel_model does not know is refused", "accepted", "refused")
    except SystemExit:
        chk("a driven series steel_model does not know is refused", "refused", "refused")
    chk("a config with no driven segment keeps the base deck", deck_keys({"fx_segments": [{"name": "US"}]}),
        list(BASE_DECK))
    # the rolling block's day weights: 4-Nov-2027 to 3-Nov-2028 cuts Q3FY28
    # (58 of its 92 days left) and Q3FY29 (34 of 92), and the weighted days
    # add back to the window exactly (366: 2028 is a leap year)
    w = window_weights(qs, "2027-11-04")
    chk("window cuts the first quarter by days", round(w["Q3FY28"] * 92), 58)
    chk("and the last", round(w["Q3FY29"] * 92), 34)
    chk("three whole quarters between", [w[q] for q in ("Q4FY28", "Q1FY29", "Q2FY29")], [1.0, 1.0, 1.0])
    days = sum(v * ((dt.date.fromisoformat(quarter_dates(q)[1]) - dt.date.fromisoformat(quarter_dates(q)[0])).days + 1)
               for q, v in w.items())
    chk("weighted days add back to the window", round(days, 6), 366)
    rc = rolling_cells(41, 18, "'P&L'!$D$23:$U$23", "'Cash flow'!$D$27:$U$27", "'Cash flow'!$D$20:$U$20")
    labels = {c["value"] for c in rc if c["cell"].startswith("A")}
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import model_view
    chk("no rolling label shadows a label model_view reads", labels & set(model_view.VAL_LABELS.values()), set())
    byc = {c["cell"]: c for c in rc}
    chk("the override cell is a yellow blank", (byc["B43"]["value"], byc["B43"]["fill"]), (None, "FFFF00"))
    chk("the target multiple links the fixed block's core multiple", byc["B52"]["value"], "=B18")
    chk("the quarter table's last row reads the 18th quarter", byc["G60"]["value"], "=INDEX(Qrow,18)")
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


def _bgr(hexrgb: str | None) -> int | None:
    if not hexrgb:
        return None
    r, g, b = (int(hexrgb[k:k + 2], 16) for k in (0, 2, 4))
    return r + g * 256 + b * 65536


def add_rolling(entity: str, force: bool = False) -> int:
    """Write the rolling block into an EXISTING workbook through Excel, so the
    PM's yellow cells survive (a rebuild would overwrite them) and an open
    window takes the edit in place. It reads the ranges it needs off the
    workbook's own formulas and refuses when the fixed block's rows have moved."""
    from openpyxl import load_workbook
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import model_view
    p = model_view.path_for(entity)
    if p is None:
        raise SystemExit(f"no model for {entity!r}")
    wb = load_workbook(p)
    va = wb["Valuation"]
    for ref, lab in ROLL_LINKS.items():
        if (va[ref].value or "").strip() != lab:
            raise SystemExit(f"Valuation!{ref} reads {va[ref].value!r}, not {lab!r}: the fixed block has moved; "
                             "refusing to link the rolling block to the wrong rows")
    stb = next((r for r in range(1, va.max_row + 1)
                if str(va.cell(r, 1).value or "").startswith("Street check")), None)
    if stb is None:
        raise SystemExit("no 'Street check' row on the Valuation sheet")
    top = stb + 5
    m7 = re.search(r"SUMIFS\((.+?),FYrow", str(va["B7"].value))
    m11 = re.search(r"INDEX\((.+?),COUNTIF", str(va["B11"].value))
    if not (m7 and m11):
        raise SystemExit("cannot read the EBITDA / net-debt ranges off Valuation!B7 and B11")
    adj, nd = m7.group(1), m11.group(1)
    cf = wb["Cash flow"]
    rdiv = next((r for r in range(1, cf.max_row + 1) if cf.cell(r, 1).value == "Dividends paid"), None)
    if rdiv is None:
        raise SystemExit("no 'Dividends paid' row on the Cash flow sheet")
    first, last = re.search(r"\$([A-Z]+)\$\d+:\$([A-Z]+)\$\d+", nd).groups()
    div = f"'Cash flow'!${first}${rdiv}:${last}${rdiv}"
    inp, n = wb["Inputs"], 0
    while inp.cell(4, FIRST_COL + n).value:
        n += 1
    cells = rolling_cells(top, n, adj, nd, div)
    if va.cell(top, 1).value == ROLL_TITLE and not force:
        raise SystemExit(f"the rolling block is already at row {top}; --force rewrites it (its override cell too)")
    if va.cell(top, 1).value != ROLL_TITLE:
        busy = [c["cell"] for c in cells if va[c["cell"]].value not in (None, "")]
        if busy:
            raise SystemExit(f"cells the block needs are not empty: {busy[:8]}")
    ops = [{"sheet": "Valuation", "cell": c["cell"], "mode": "set", "value": c["value"], "font": FONT,
            "numfmt": FMT[c["fmt"]] if c.get("fmt") else None, "fcolor": _bgr(c.get("color") or BLACK),
            "bold": bool(c.get("bold")), "italic": bool(c.get("italic")), "size": c.get("size") or 10,
            "fill": _bgr(c.get("fill")), "wrap": bool(c.get("wrap"))} for c in cells]
    ops += [{"sheet": "Valuation", "cell": f"{get_column_letter(HELP_COL + k)}1", "mode": "width", "value": 12}
            for k in range(8)]
    rows = {c["value"]: c["cell"] for c in cells if c["cell"].startswith("A")}
    want = ["Valuation date used", "Horizon from the price date (months)", "Months of that window inside the model",
            "Adjusted EBITDA in that window (Rs cr)", "Net debt at the valuation date (Rs cr)",
            "Target core multiple, ex JV (x)", ROLL_TP, "Upside vs CMP, rolling",
            "Dividends paid before the valuation date (Rs/share)",
            "Total return to the valuation date, incl. those dividends"]
    ops += [{"sheet": "Valuation", "cell": "B" + rows[w][1:], "mode": "get", "value": None} for w in want]
    res = model_view.excel(entity, ops)
    out = res["cells"] if isinstance(res["cells"], list) else [res["cells"]]
    print(f"{p.name}: rolling block written at Valuation!A{top}" + (" (into the open window)" if res.get("attached") else ""))
    for w, o in zip(want, out[-len(want):]):
        print(f"  {w}: {o['value']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh-deck", metavar="ENTITY",
                    help="write the current quarter's average to date into an existing workbook, via Excel")
    ap.add_argument("--add-rolling", metavar="ENTITY",
                    help="write the rolling 12-month-forward target block into an existing workbook, via Excel")
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
    if a.add_rolling:
        return add_rolling(a.add_rolling, a.force)
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
