"""A quarterly regulated-GENERATOR model in Excel: regulated equity -> P&L -> cash -> P/E -> price.

PM, 2026-11-18: *"let's build the ntpc-agent ... let's start building the
excel model as well."* `power_model.py` drives a transmission utility off its
gross block. A regulated generator is the same idea with two differences that
change the sheet, so it has its own builder (NHPC and the regulated half of
any other genco can reuse it):

- FUEL IS PASSED THROUGH. About half of NTPC's revenue is the energy charge,
  which recovers fuel cost rupee for rupee. A revenue yield on the asset base
  would therefore move with coal prices and generation, which earn nothing.
  The model splits sales into fuel (carried from the same quarter a year
  earlier, times 1 + Fuel_growth) and the MARGIN OVER FUEL (capacity charges,
  incentives, the regulatory deferral movement, other operating income).
  Fuel moves sales and cost equally and no profit line; the selftest holds
  that.
- THE DRIVER IS REGULATED EQUITY, the number the company reports each quarter
  and the CERC return is paid on. Each fiscal year's ADDITION to consolidated
  regulated equity (yellow, on Inputs) is spread over its forecast quarters,
  net of what is already reported. Margin over fuel = average regulated
  equity x a YIELD measured on every reported quarter and carried as the
  trailing four quarters' average plus a drift. Employee and other expenses
  are a ratio on the same base; depreciation is the latest quarter's rate,
  carried. The model does not rebuild the tariff plant by plant.

Everything else follows power_model.py: other income and the JV share are
trailing averages, interest is a rate on OPENING gross debt, tax a rate on
PBT, the minority interest a trailing share of profit, exceptional items a
typed constant (zero). Gross debt grows by a share of capex, dividends are a
payout of the previous fiscal year's PAT, cash is the plug, net worth rolls
with PAT less dividends. Capex, debt and net worth are reported only at
balance-sheet dates, so those typed rows are SPARSE: a model row reads the
typed figure where there is one and rolls where there is none.

Sales here INCLUDE the net movement in regulatory deferral balances (the
brokers' presentation), so the tax line is the tax on that PBT. The filed
P&L shows the movement after tax; on that layout PBT swings by Rs2,000cr a
quarter while PAT does not, and a tax rate fitted to it is meaningless.

A Memo sheet carries typed reference rows (the standalone-to-consolidated
bridge, standalone regulated equity, units, capacity). Nothing reads it.

Valuation is P/E on the valuation year's EPS at a typed target multiple (the
median house-implied P/E, which case_brief.py recomputes), with the houses'
own route beside it as a cross-check: P/BV on book ex the listed renewables
subsidiary, plus that stake per share.

    python packages/book/genco_model.py --config data/analysis/cases/ntpc/model_config.json \\
        --out data/analysis/cases/ntpc/NTPC_model.xlsx --recalc
    python packages/book/genco_model.py --selftest
    python packages/book/genco_model.py --add-ev-ebitda ntpc     # EV/EBITDA beside the P/E, live, through Excel

EV/EBITDA sits BESIDE the P/E on the Valuation sheet (column C, with the
houses' targets and the multiple each implies in a table under the sheet)
whenever build.json carries `house_tps`: see `ev_block`. The basis the
Analysis card reads stays P/E.

The workbook is built ONCE; after that it is the working surface (type the
new quarter into the blue rows, flip its status on Inputs). A rebuild
overwrites the PM's yellow cells, so `--out` refuses an existing file
without `--force`. Edits from chat go through `model_view.py --set`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import tempfile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from steel_model import (BLACK, BLUE, FIRST_COL, FMT, FONT, GREEN, HEAD, YELLOW,  # noqa: E402
                         Sheet, col, errors, fy_of, name, quarter_dates, quarters, recalc)

REPO = pathlib.Path(__file__).resolve().parents[2]

# consolidated lines every actual quarter must carry, Rs crore
LINES = ("sales", "fuel", "employee", "other_exp", "depreciation", "interest", "other_income", "pbt", "tax",
         "jv_share", "minority", "exceptional", "pat", "reg_equity", "dividends_paid")
# reported only at balance-sheet dates (or not by quarter at all): typed where known
SPARSE = ("capex", "gross_debt", "net_worth", "cash_reported")
RE = "Regulated equity"


def crossfoot(cfg: dict, tol: float = 3.0) -> list[str]:
    """Every actual quarter must foot before anything is written: PBT from its
    components, PAT = PBT - tax + JV share - minority + exceptional. A mistyped
    number refuses here rather than becoming a plausible price. The plan
    weights must sum to one, or a year's plan would be silently scaled."""
    c = cfg["consolidated"]
    errs = []
    acts = list(c.get("pat", {}))
    for k in LINES:
        if k not in c:
            errs.append(f"consolidated.{k}: missing")
        elif set(c[k]) != set(acts):
            errs.append(f"consolidated.{k}: quarters differ from pat")
    if errs:
        return errs
    qs = quarters(cfg["first_quarter"], cfg["last_quarter"])
    want = qs[:qs.index(cfg["actual_through"]) + 1]
    if acts != want:
        errs.append(f"consolidated quarters {acts} are not {want[0]}..{want[-1]}")
    for k in SPARSE:
        for q in c.get(k, {}):
            if q not in acts:
                errs.append(f"consolidated.{k}: {q} is not an actual quarter")
    for q in acts:
        pbt = (c["sales"][q] - c["fuel"][q] - c["employee"][q] - c["other_exp"][q] - c["depreciation"][q]
               - c["interest"][q] + c["other_income"][q])
        if abs(pbt - c["pbt"][q]) > tol:
            errs.append(f"{q}: PBT components {pbt:,.1f} vs reported {c['pbt'][q]:,.1f}")
        pat = c["pbt"][q] - c["tax"][q] + c["jv_share"][q] - c["minority"][q] + c["exceptional"][q]
        if abs(pat - c["pat"][q]) > tol:
            errs.append(f"{q}: PBT - tax + JV - minority + exceptional {pat:,.1f} vs PAT {c['pat'][q]:,.1f}")
    for k in ("weights_reg_equity", "weights_capex"):
        w = cfg["plans"][k]["value"]
        if len(w) != 4 or abs(sum(w) - 1.0) > 1e-6:
            errs.append(f"plans.{k}: four weights summing to 1.0, got {w}")
    ds = cfg["constants"]["div_split"]["value"]
    if len(ds) != 4 or abs(sum(ds) - 1.0) > 1e-6:
        errs.append(f"constants.div_split: four shares summing to 1.0, got {ds}")
    return errs


def build(cfg: dict, out: pathlib.Path) -> dict:
    errs = crossfoot(cfg)
    if errs:
        raise SystemExit("config does not cross-foot:\n  " + "\n  ".join(errs))
    qs = quarters(cfg["first_quarter"], cfg["last_quarter"])
    n_act = qs.index(cfg["actual_through"]) + 1
    act = set(qs[:n_act])
    N = len(qs)
    last = col(N - 1)
    C, O, P = cfg["constants"], cfg["opening"], cfg["plans"]
    cs = cfg["consolidated"]
    typed = lambda key: {q: cs[key][q] for q in qs if q in act and q in cs.get(key, {})}
    wb = Workbook()
    wb.remove(wb.active)

    def A(i):
        return f'Inputs!{col(i)}$6="A"'

    def rng(sheet, r):
        return f"'{sheet}'!$D${r}:${last}${r}"

    def prev4(sheet, r, i):
        lo = max(0, i - 4)
        return f"AVERAGE('{sheet}'!{col(lo)}{r}:{col(i - 1)}{r})"

    # ---------------------------------------------------------------- README
    rd = wb.create_sheet("README")
    rd.column_dimensions["A"].width = 120
    for k, (txt, style) in enumerate(readme_lines(cfg, qs, n_act), start=1):
        c = rd.cell(k, 1, txt)
        c.font = Font(name=FONT, bold=style == "h", size=13 if style == "t" else 10)
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
        ws.cell(6, FIRST_COL + i, "A" if q in act else "F").font = Font(name=FONT, bold=True, color="FFFF00")
    ws.cell(6, 3, "<- flip F to A").font = Font(name=FONT, size=8, color="FFFFFF")
    ws.cell(7, 1, "Quarter index").font = Font(name=FONT, size=9, color="595959")
    for i in range(N):
        ws.cell(7, FIRST_COL + i, i + 1).font = Font(name=FONT, size=9, color="595959")
    name(wb, "Qrow", f"Inputs!$D$4:${last}$4")
    name(wb, "FYrow", f"Inputs!$D$5:${last}$5")
    name(wb, "Status", f"Inputs!$D$6:${last}$6")
    inp.r = 8
    inp.row("Basis of the actuals", "", {q: cfg["basis"].get(q) for q in qs if q in act}, fmt="cr")
    for i in range(N):
        cell = ws.cell(8, FIRST_COL + i)
        cell.font = Font(name=FONT, size=7, color=BLUE)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[8].height = 72
    r_qstart = inp.row("Quarter start", "date", {q: dt.date.fromisoformat(quarter_dates(q)[0]) for q in qs}, fmt="date")
    r_qend = inp.row("Quarter end", "date", {q: dt.date.fromisoformat(quarter_dates(q)[1]) for q in qs}, fmt="date")

    inp.section("Company constants (A label, B unit, C value, D source)")

    def const(key, label, entry, fmt, yellow=False):
        r = inp.r
        ws.cell(r, 1, label).font = Font(name=FONT)
        ws.cell(r, 2, entry.get("unit", "")).font = Font(name=FONT, size=9, color="595959")
        c = ws.cell(r, 3, entry["value"])
        c.font = Font(name=FONT, color=BLUE)
        if fmt:
            c.number_format = FMT[fmt]
        if yellow:
            c.fill = YELLOW
        ws.cell(r, 4, entry.get("source", "")).font = Font(name=FONT, size=8, italic=True, color="595959")
        name(wb, key, f"Inputs!$C${r}")
        inp.r += 1

    const("Shares_mn", "Shares outstanding", C["shares_mn"], "mn")
    const("CMP", "Current market price", C["cmp"], "rs")
    const("CMP_date", "Price date", C["cmp_date"], None)
    const("Tax_rate", "Effective tax rate on PBT", C["tax_rate"], "pct", yellow=True)
    const("Int_rate", "Interest rate on opening gross debt (P&L, after capitalised interest)", C["int_rate"], "pct", yellow=True)
    const("Debt_share", "Share of capex funded by new debt", C["debt_share"], "pct", yellow=True)
    const("Payout", "Dividend payout: share of the previous FY's PAT", C["payout"], "pct", yellow=True)
    const("Exceptional", "Exceptional / prior-period items per quarter (after tax)", C["exceptional"], "cr", yellow=True)
    const("Fuel_growth", "Fuel cost growth on the same quarter a year earlier (passed through)", C["fuel_growth"], "pct", yellow=True)
    const("Margin_drift", "Margin-yield drift per quarter (pp of annual yield on regulated equity)", C["margin_drift"], "pct", yellow=True)
    const("Opex_drift", "Opex-ratio drift per quarter (pp of annual ratio)", C["opex_drift"], "pct", yellow=True)
    const("Dep_drift", "Depreciation-rate drift per quarter (pp of annual rate)", C["dep_drift"], "pct", yellow=True)
    const("Target_pe", "Target P/E multiple (the Valuation sheet reads this)", C["target_pe"], "x2", yellow=True)
    const("Target_pbv", "Target P/BV on book ex the listed subsidiary, cross-check only", C["target_pbv"], "x2", yellow=True)
    const("Sub_book_ps", "Listed subsidiary: investment carried in that book, per share", C["sub_book_ps"], "rs", yellow=True)
    const("Sub_value_ps", "Listed subsidiary: value of the stake, per share", C["sub_value_ps"], "rs", yellow=True)

    inp.section(f"Opening balance sheet, {O['as_of']} (before the first quarter)")
    const("Open_RE", "Regulated equity, consolidated", O["reg_equity"], "cr")
    const("Open_debt", "Gross debt", O["gross_debt"], "cr")
    const("Open_cash", "Cash and bank balances", O["cash"], "cr")
    const("Open_NW", "Net worth", O["net_worth"], "cr")
    const("Open_FYPAT", "PAT of the fiscal year before the first quarter (base for its dividend)", O["fy_pat"], "cr")

    def plan(title, key, nm_years, nm_vals):
        inp.section(title)
        first = inp.r
        for fy, e in P[key].items():
            ws.cell(inp.r, 1, fy).font = Font(name=FONT)
            c = ws.cell(inp.r, 3, e["value"])
            c.font = Font(name=FONT, color=BLUE)
            c.fill = YELLOW
            c.number_format = FMT["cr"]
            ws.cell(inp.r, 4, e["source"]).font = Font(name=FONT, size=8, italic=True, color="595959")
            inp.r += 1
        name(wb, nm_years, f"Inputs!$A${first}:$A${inp.r - 1}")
        name(wb, nm_vals, f"Inputs!$C${first}:$C${inp.r - 1}")

    plan("Addition to consolidated regulated equity (Rs crore per fiscal year) - the driver of everything",
         "reg_equity_addition", "REYears", "REPlan")
    plan("Capex plan, group (Rs crore per fiscal year)", "capex", "CapexYears", "CapexPlan")

    inp.section("Quarter shapes: share of a year's plan in each quarter, and of the year's dividend")
    wre, wcpx, dsp = P["weights_reg_equity"]["value"], P["weights_capex"]["value"], C["div_split"]["value"]
    qn = lambda q: int(q[1]) - 1
    r_wre = inp.row("Regulated-equity addition weight by quarter", "share", {q: wre[qn(q)] for q in qs}, fmt="pct",
                    note="note below", fill=lambda i, q: True)
    r_wcpx = inp.row("Capex weight by quarter", "share", {q: wcpx[qn(q)] for q in qs}, fmt="pct",
                     note="note below", fill=lambda i, q: True)
    r_dsp = inp.row("Dividend timing: share of the previous FY's payout paid", "share", {q: dsp[qn(q)] for q in qs},
                    fmt="pct", note="note below", fill=lambda i, q: True)
    inp.r += 1
    for lab, src in (("Regulated-equity weight", P["weights_reg_equity"]["source"]),
                     ("Capex weight", P["weights_capex"]["source"]), ("Dividend timing", C["div_split"]["source"])):
        ws.cell(inp.r, 1, f"{lab}: {src}").font = Font(name=FONT, size=8, italic=True, color="595959")
        inp.r += 1

    # ---------------------------------------------------------------- Regulated equity
    AS = Sheet(wb, RE, qs, f"{cfg['company']} - regulated equity and capex",
               "Regulated equity is typed for reported quarters; forecasts add the fiscal-year plan on Inputs. Capex is "
               "typed where a quarter is disclosed and takes the year's plan where it is not. Rs crore.")
    AS.header()
    AS.section("Reported (typed)")
    r_re_t = AS.row("Regulated equity, consolidated, reported (closing)", "Rs cr", typed("reg_equity"))
    r_capex_t = AS.row("Capex, group, reported (blank where the quarter is not disclosed)", "Rs cr", typed("capex"))
    # a separate row, so the model row's SUMIFS never ranges over itself (a circular reference in Excel
    # even when the criteria exclude the cell)
    r_add_t = AS.row("Addition to regulated equity, reported", "Rs cr",
                     lambda i, q, r: f"=IF({A(i)},{col(i)}{r_re_t}-" + ("Open_RE" if i == 0 else f"{col(i - 1)}{r_re_t}") + ',"")',
                     kind="local")
    r_weff = AS.row("Capex weight still to place (0 where the quarter is typed)", "share",
                    lambda i, q, r: f"=IF(ISNUMBER({col(i)}{r_capex_t}),0,Inputs!{col(i)}{r_wcpx})", fmt="pct")
    AS.section("Model")
    fyc = lambda i: f"Inputs!{col(i)}$5"

    def re_add(i, q, r):
        plan_cell = f"IFERROR(INDEX(REPlan,MATCH({fyc(i)},REYears,0)),0)"
        done = f'SUMIFS({rng(RE, r_add_t)},FYrow,{fyc(i)},Status,"A")'
        wsum = f'SUMIFS({rng("Inputs", r_wre)},FYrow,{fyc(i)},Status,"F")'
        return f"=IF({A(i)},{col(i)}{r_add_t},IF({wsum}=0,0,({plan_cell}-{done})*Inputs!{col(i)}{r_wre}/{wsum}))"

    r_add = AS.row("Addition to regulated equity", "Rs cr", re_add, bold=True,
                   comment="The driver. Forecast = (the year's plan on Inputs - the quarters already reported) x this "
                           "quarter's weight / the weights of the year's forecast quarters.")
    r_re_open = AS.row("Regulated equity, opening", "Rs cr",
                       lambda i, q, r: "=Open_RE" if i == 0 else f"={col(i - 1)}{r + 1}")
    r_re = AS.row("Regulated equity, closing", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_re_t},{col(i)}{r_re_open}+{col(i)}{r_add})", bold=True)
    r_re_avg = AS.row("Regulated equity, average", "Rs cr", lambda i, q, r: f"=({col(i)}{r_re_open}+{col(i)}{r_re})/2")
    AS.row("Regulated equity growth, year on year", "%",
           lambda i, q, r: None if i < 4 else f"={col(i)}{r_re}/{col(i - 4)}{r_re}-1", fmt="pct")

    def capex(i, q, r):
        plan_cell = f"IFERROR(INDEX(CapexPlan,MATCH({fyc(i)},CapexYears,0)),0)"
        done = f"SUMIFS({rng(RE, r_capex_t)},FYrow,{fyc(i)})"
        wsum = f"SUMIFS({rng(RE, r_weff)},FYrow,{fyc(i)})"
        return (f"=IF(ISNUMBER({col(i)}{r_capex_t}),{col(i)}{r_capex_t},"
                f"IF({wsum}=0,0,({plan_cell}-{done})*{col(i)}{r_weff}/{wsum}))")

    r_capex = AS.row("Capex, group", "Rs cr", capex, bold=True,
                     comment="A typed quarter is used as typed. Every other quarter takes its weight's share of the "
                             "fiscal year's plan on Inputs, less the quarters typed. For a year already over, the plan "
                             "line IS the reported annual total.")
    AS.row("Fiscal-year addition to regulated equity to date", "Rs cr",
           lambda i, q, r: f"=SUMIFS({rng(RE, r_add)},FYrow,{fyc(i)},Inputs!$D$7:${last}$7,\"<=\"&Inputs!{col(i)}$7)")
    REA = lambda i: f"'{RE}'!{col(i)}{r_re_avg}"

    # ---------------------------------------------------------------- P&L
    PL = Sheet(wb, "P&L", qs, f"{cfg['company']} - consolidated P&L",
               "Reported rows typed; model rows read them while the status is A. Fuel is passed through. The margin over "
               "fuel, opex and depreciation run off average regulated equity. Sales include the regulatory deferral "
               "movement. Rs crore.")
    PL.header()
    PL.section("Reported (typed)")
    r_sales_t = PL.row("Sales incl. regulatory deferral movement, reported", "Rs cr", typed("sales"))
    r_fuel_t = PL.row("Fuel cost, reported", "Rs cr", typed("fuel"))
    r_emp_t = PL.row("Employee cost, reported", "Rs cr", typed("employee"))
    r_oth_t = PL.row("Other expenses, reported", "Rs cr", typed("other_exp"))
    r_dep_t = PL.row("Depreciation, reported", "Rs cr", typed("depreciation"))
    r_int_t = PL.row("Finance cost, reported", "Rs cr", typed("interest"))
    r_oi_t = PL.row("Other income, reported", "Rs cr", typed("other_income"))
    r_tax_t = PL.row("Tax, reported", "Rs cr", typed("tax"))
    r_jv_t = PL.row("Share of JV profit, reported (after tax)", "Rs cr", typed("jv_share"))
    r_mi_t = PL.row("Minority interest, reported", "Rs cr", typed("minority"))
    r_xo_t = PL.row("Exceptional / prior-period items, reported (after tax)", "Rs cr", typed("exceptional"))
    r_pat_t = PL.row("PAT attributable to owners, reported", "Rs cr", typed("pat"))

    PL.section("The PM's overrides (yellow; blank = the model's own number)")
    r_mg_ov = PL.row("Margin over fuel override", "Rs cr", {}, fill=lambda i, q: q not in act)
    r_oi_ov = PL.row("Other income override", "Rs cr", {}, fill=lambda i, q: q not in act)
    PL.ws.cell(PL.r, 1, "Regulated-equity additions are overridden on Inputs (the fiscal-year plan).").font = \
        Font(name=FONT, size=8, italic=True, color="595959")
    PL.r += 1

    c_ = col
    pre_t = lambda i: (f"({c_(i)}{r_sales_t}-{c_(i)}{r_fuel_t}-{c_(i)}{r_emp_t}-{c_(i)}{r_oth_t}-{c_(i)}{r_dep_t}"
                       f"-{c_(i)}{r_int_t}+{c_(i)}{r_oi_t}-{c_(i)}{r_tax_t})")
    PL.section("Drivers (annualised, on average regulated equity)")
    r_my = PL.row("Margin yield: (sales - fuel) x 4 / average regulated equity", "%",
                  lambda i, q, r: f"=IF({A(i)},({c_(i)}{r_sales_t}-{c_(i)}{r_fuel_t})*4/{REA(i)},"
                                  + ("0" if i == 0 else f"{prev4('P&L', r, i)}+Margin_drift") + ")",
                  fmt="pct", fill=lambda i, q: q not in act,
                  comment="Capacity charges, incentives, the regulatory deferral movement and other operating income per "
                          "rupee of regulated equity. Measured on every reported quarter; carried as the trailing four "
                          "quarters' average plus Margin_drift (Inputs).")
    r_opxr = PL.row("Employee and other expenses x 4 / average regulated equity", "%",
                    lambda i, q, r: f"=IF({A(i)},({c_(i)}{r_emp_t}+{c_(i)}{r_oth_t})*4/{REA(i)},"
                                    + ("0" if i == 0 else f"{prev4('P&L', r, i)}+Opex_drift") + ")", fmt="pct")
    # the latest reported quarter's rate, carried (power_model.py's finding: a trailing average of a
    # rate that is trending puts the bias straight into EPS)
    r_depr = PL.row("Depreciation x 4 / average regulated equity", "%",
                    lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_dep_t}*4/{REA(i)},"
                                    + ("0" if i == 0 else f"{c_(i - 1)}{r}+Dep_drift") + ")", fmt="pct",
                    comment="The latest reported quarter's rate, carried, plus Dep_drift (Inputs). Renewables carry more "
                            "depreciation per rupee of regulated equity than thermal, so the rate rises with their share.")
    r_mir = PL.row("Minority interest / profit after tax before JVs", "%",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_mi_t}/{pre_t(i)},"
                                   + ("0" if i == 0 else prev4('P&L', r, i)) + ")", fmt="pct")

    PL.section("Revenue")
    r_fuel = PL.row("Fuel cost (passed through)", "Rs cr",
                    lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_fuel_t},"
                                    + (f"{c_(i - 4)}{r}" if i >= 4 else ("0" if i == 0 else f"{c_(i - 1)}{r}"))
                                    + "*(1+Fuel_growth))",
                    comment="Forecast: the same quarter a year earlier x (1 + Fuel_growth). It enters sales and cost "
                            "equally, so no profit line moves with it.")
    r_mg = PL.row("Margin over fuel", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_sales_t}-{c_(i)}{r_fuel_t},IF(ISNUMBER({c_(i)}{r_mg_ov}),"
                                  f"{c_(i)}{r_mg_ov},{REA(i)}*{c_(i)}{r_my}/4))", bold=True)
    r_rev = PL.row("Sales incl. regulatory deferral movement", "Rs cr",
                   lambda i, q, r: f"={c_(i)}{r_fuel}+{c_(i)}{r_mg}", bold=True)
    PL.row("Margin over fuel growth, year on year", "%",
           lambda i, q, r: None if i < 4 else f"={c_(i)}{r_mg}/{c_(i - 4)}{r_mg}-1", fmt="pct")

    PL.section("Costs and profit")
    r_opx = PL.row("Employee and other expenses", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_emp_t}+{c_(i)}{r_oth_t},{REA(i)}*{c_(i)}{r_opxr}/4)")
    r_ebitda = PL.row("EBITDA", "Rs cr", lambda i, q, r: f"={c_(i)}{r_mg}-{c_(i)}{r_opx}", bold=True)
    r_dep = PL.row("Depreciation", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_dep_t},{REA(i)}*{c_(i)}{r_depr}/4)")
    r_ebit = PL.row("EBIT", "Rs cr", lambda i, q, r: f"={c_(i)}{r_ebitda}-{c_(i)}{r_dep}")
    r_oi = PL.row("Other income", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_oi_t},IF(ISNUMBER({c_(i)}{r_oi_ov}),{c_(i)}{r_oi_ov},"
                                  + ("0" if i == 0 else prev4('P&L', r, i)) + "))")
    r_int = PL.row("Finance cost", "Rs cr", lambda i, q, r: None)   # filled once the Cash flow rows exist
    r_pbt = PL.row("PBT (before the JV share)", "Rs cr",
                   lambda i, q, r: f"={c_(i)}{r_ebit}+{c_(i)}{r_oi}-{c_(i)}{r_int}", bold=True)
    r_tax = PL.row("Tax", "Rs cr", lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_tax_t},{c_(i)}{r_pbt}*Tax_rate)")
    PL.row("Tax rate", "%", lambda i, q, r: f"=IF({c_(i)}{r_pbt}=0,0,{c_(i)}{r_tax}/{c_(i)}{r_pbt})", fmt="pct")
    r_jv = PL.row("Share of JV profit (after tax)", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_jv_t}," + ("0" if i == 0 else prev4('P&L', r, i)) + ")")
    r_mi = PL.row("Minority interest", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_mi_t},({c_(i)}{r_pbt}-{c_(i)}{r_tax})*{c_(i)}{r_mir})")
    r_xo = PL.row("Exceptional / prior-period items (after tax)", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_xo_t},Exceptional)")
    r_pat = PL.row("PAT attributable to owners", "Rs cr",
                   lambda i, q, r: f"={c_(i)}{r_pbt}-{c_(i)}{r_tax}+{c_(i)}{r_jv}-{c_(i)}{r_mi}+{c_(i)}{r_xo}", bold=True)
    PL.row("Check: model PAT less reported (actual quarters)", "Rs cr",
           lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_pat}-{c_(i)}{r_pat_t},\"\")")
    PL.row("PAT growth, year on year", "%",
           lambda i, q, r: None if i < 4 else f"={c_(i)}{r_pat}/{c_(i - 4)}{r_pat}-1", fmt="pct")
    r_eps = PL.row("EPS (Rs)", "Rs", lambda i, q, r: f"={c_(i)}{r_pat}*10/Shares_mn", fmt="rs", bold=True)
    PL.row("PAT x 4 / average regulated equity", "%", lambda i, q, r: f"={c_(i)}{r_pat}*4/{REA(i)}", fmt="pct",
           comment="Not the regulated RoE: consolidated PAT includes renewables, mining, trading, JVs and other "
                   "income. A check on the level, quarter to quarter.")
    r_roe = PL.row("Return on equity, annualised (PAT x 4 / average net worth)", "%", lambda i, q, r: None, fmt="pct")
    D = "DATE(LEFT(CMP_date,4),MID(CMP_date,6,2),RIGHT(CMP_date,2))"
    r_w = PL.row("Weight of this quarter in the 12 months from the price date", "share",
                 lambda i, q, r: (f"=MAX(0,MIN(Inputs!{c_(i)}{r_qend},{D}+364)-MAX(Inputs!{c_(i)}{r_qstart},{D})+1)"
                                  f"/(Inputs!{c_(i)}{r_qend}-Inputs!{c_(i)}{r_qstart}+1)"), fmt="w")

    # ---------------------------------------------------------------- Cash flow
    CF = Sheet(wb, "Cash flow", qs, f"{cfg['company']} - cash, debt and net worth",
               "CFO = PAT + minority + depreciation (no working capital). Gross debt grows by Debt_share x capex; "
               "dividends are Payout x the previous fiscal year's PAT on the Inputs timing; cash is the plug. Debt and "
               "net worth are typed at balance-sheet dates and rolled between them. Rs crore.")
    CF.header()
    CF.section("Reported (typed; blank where no balance sheet was published)")
    r_debt_t = CF.row("Gross debt, reported (closing)", "Rs cr", typed("gross_debt"))
    r_nw_t = CF.row("Net worth, reported (closing)", "Rs cr", typed("net_worth"))
    r_div_t = CF.row("Dividends paid, reported", "Rs cr", typed("dividends_paid"))
    r_cash_t = CF.row("Cash and bank balances, reported where disclosed", "Rs cr", typed("cash_reported"))
    CF.section("Model")
    r_cfo = CF.row("Cash from operations (PAT + minority + depreciation)", "Rs cr",
                   lambda i, q, r: f"='P&L'!{c_(i)}{r_pat}+'P&L'!{c_(i)}{r_mi}+'P&L'!{c_(i)}{r_dep}")
    r_cfcapex = CF.row("Capex", "Rs cr", lambda i, q, r: f"=-'{RE}'!{c_(i)}{r_capex}")
    r_fcf = CF.row("Free cash flow", "Rs cr", lambda i, q, r: f"={c_(i)}{r_cfo}+{c_(i)}{r_cfcapex}", bold=True)

    def prev_fy_pat(i):
        pfy = f'("FY"&(VALUE(RIGHT({fyc(i)},2))-1))'
        return f"IF(COUNTIF(FYrow,{pfy})=0,Open_FYPAT,SUMIFS({rng('P&L', r_pat)},FYrow,{pfy}))"

    r_div = CF.row("Dividends paid", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_div_t},Payout*{prev_fy_pat(i)}*Inputs!{c_(i)}{r_dsp})",
                   comment="Forecast: Payout (Inputs) x the PREVIOUS fiscal year's PAT x this quarter's share of the "
                           "year's dividend (Inputs timing row).")
    r_ddebt = CF.row("Change in gross debt", "Rs cr",
                     lambda i, q, r: f"=IF(ISNUMBER({c_(i)}{r_debt_t}),{c_(i)}{r_debt_t}-"
                                     + ("Open_debt" if i == 0 else f"{c_(i - 1)}{r + 1}")
                                     + f",Debt_share*'{RE}'!{c_(i)}{r_capex})")
    r_debt = CF.row("Gross debt, closing", "Rs cr",
                    lambda i, q, r: "=" + ("Open_debt" if i == 0 else f"{c_(i - 1)}{r}") + f"+{c_(i)}{r_ddebt}", bold=True)
    r_cash = CF.row("Cash and bank balances, closing (the plug)", "Rs cr",
                    lambda i, q, r: "=" + ("Open_cash" if i == 0 else f"{c_(i - 1)}{r}")
                                    + f"+{c_(i)}{r_fcf}-{c_(i)}{r_div}+{c_(i)}{r_ddebt}")
    CF.row("Check: modelled cash less reported, where disclosed", "Rs cr",
           lambda i, q, r: f"=IF(ISNUMBER({c_(i)}{r_cash_t}),{c_(i)}{r_cash}-{c_(i)}{r_cash_t},\"\")",
           comment="The roll ignores working capital, regulatory assets and other flows, so the gap is what the "
                   "balance sheet absorbed.")
    r_nd = CF.row("Net debt, closing", "Rs cr", lambda i, q, r: f"={c_(i)}{r_debt}-{c_(i)}{r_cash}", bold=True)
    r_nw = CF.row("Net worth, closing", "Rs cr",
                  lambda i, q, r: f"=IF(ISNUMBER({c_(i)}{r_nw_t}),{c_(i)}{r_nw_t},"
                                  + ("Open_NW" if i == 0 else f"{c_(i - 1)}{r}")
                                  + f"+'P&L'!{c_(i)}{r_pat}-{c_(i)}{r_div})", bold=True)
    CF.row("Gross debt / net worth", "x", lambda i, q, r: f"={c_(i)}{r_debt}/{c_(i)}{r_nw}", fmt="x2")
    CF.row("Book value per share (Rs)", "Rs", lambda i, q, r: f"={c_(i)}{r_nw}*10/Shares_mn", fmt="rs")

    for i, q in enumerate(qs):
        c = PL.ws.cell(r_int, FIRST_COL + i,
                       f"=IF({A(i)},{c_(i)}{r_int_t}," + ("Open_debt" if i == 0 else f"'Cash flow'!{c_(i - 1)}{r_debt}")
                       + "*Int_rate/4)")
        c.font = Font(name=FONT, color=GREEN)
        c.number_format = FMT["cr"]
        c = PL.ws.cell(r_roe, FIRST_COL + i,
                       f"={c_(i)}{r_pat}*4/((" + ("Open_NW" if i == 0 else f"'Cash flow'!{c_(i - 1)}{r_nw}")
                       + f"+'Cash flow'!{c_(i)}{r_nw})/2)")
        c.font = Font(name=FONT, color=GREEN)
        c.number_format = FMT["pct"]

    # ---------------------------------------------------------------- Memo (typed; nothing reads it)
    memo = cfg.get("memo") or {}
    if memo:
        ME = Sheet(wb, "Memo", qs, f"{cfg['company']} - reference rows",
                   "Typed for reported quarters. Nothing in the model reads this sheet.")
        ME.header()
        for sec in memo.get("sections", []):
            ME.section(sec["title"])
            for rw in sec["rows"]:
                ME.row(rw["label"], rw.get("unit", ""), {q: v for q, v in rw["values"].items() if q in act},
                       fmt=rw.get("fmt", "cr"))
            if sec.get("note"):
                ME.ws.cell(ME.r, 1, sec["note"]).font = Font(name=FONT, size=8, italic=True, color="595959")
                ME.r += 1

    # ---------------------------------------------------------------- Annual
    an = wb.create_sheet("Annual")
    an["A1"] = f"{cfg['company']} - fiscal-year summary"
    an["A1"].font = Font(name=FONT, bold=True, size=13)
    an["A2"] = "Sums of the quarters; a year with any forecast quarter is marked F."
    an["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    an.column_dimensions["A"].width = 46
    fys = sorted({fy_of(q) for q in qs if sum(fy_of(x) == fy_of(q) for x in qs) == 4})
    for j, fy in enumerate(fys):
        L = get_column_letter(2 + j)
        c = an.cell(4, 2 + j, fy)
        c.font = Font(name=FONT, bold=True, color="FFFFFF")
        c.fill = HEAD
        c.alignment = Alignment(horizontal="center")
        an.column_dimensions[L].width = 12
        s = an.cell(5, 2 + j, f'=IF(COUNTIFS(FYrow,{L}4,Status,"F")>0,"F","A")')
        s.font = Font(name=FONT, bold=True)
        s.alignment = Alignment(horizontal="center")
    an.cell(4, 1, "Fiscal year").font = Font(name=FONT, bold=True, color="FFFFFF")
    an.cell(4, 1).fill = HEAD
    an.cell(5, 1, "Status").font = Font(name=FONT, bold=True)
    yend = lambda sheet, r, ref: f"INDEX({rng(sheet, r)},MATCH(\"Q4\"&{ref},Qrow,0))"
    sm = lambda sheet, r: (lambda L: f"=SUMIFS({rng(sheet, r)},FYrow,{L}$4)")
    arows = [
        ("Addition to regulated equity (Rs cr)", sm(RE, r_add), "cr"),
        ("Regulated equity, year end (Rs cr)", lambda L: f"={yend(RE, r_re, L + '$4')}", "cr"),
        ("Capex, group (Rs cr)", sm(RE, r_capex), "cr"),
        ("Sales incl. regulatory deferral (Rs cr)", sm("P&L", r_rev), "cr"),
        ("Fuel cost (Rs cr)", sm("P&L", r_fuel), "cr"),
        ("Margin over fuel (Rs cr)", sm("P&L", r_mg), "cr"),
        ("EBITDA (Rs cr)", sm("P&L", r_ebitda), "cr"),
        ("PAT attributable to owners (Rs cr)", sm("P&L", r_pat), "cr"),
        ("EPS (Rs)", sm("P&L", r_eps), "rs"),
        ("Dividends paid (Rs cr)", sm("Cash flow", r_div), "cr"),
        ("Gross debt, year end (Rs cr)", lambda L: f"={yend('Cash flow', r_debt, L + '$4')}", "cr"),
        ("Net worth, year end (Rs cr)", lambda L: f"={yend('Cash flow', r_nw, L + '$4')}", "cr"),
        ("Book value per share, year end (Rs)", lambda L: f"={yend('Cash flow', r_nw, L + '$4')}*10/Shares_mn", "rs"),
        ("Return on average equity",
         lambda L: f"=SUMIFS({rng('P&L', r_pat)},FYrow,{L}$4)/(({yend('Cash flow', r_nw, L + '$4')}"
                   f"+IFERROR(INDEX({rng('Cash flow', r_nw)},MATCH(\"Q4\"&{L}$4,Qrow,0)-4),Open_NW))/2)", "pct"),
        ("Margin yield on regulated equity, average of the year", lambda L: f"=AVERAGEIFS({rng('P&L', r_my)},FYrow,{L}$4)", "pct"),
    ]
    for k, (lab, fn, fmt) in enumerate(arows):
        r = 6 + k
        an.cell(r, 1, lab).font = Font(name=FONT)
        for j in range(len(fys)):
            c = an.cell(r, 2 + j, fn(get_column_letter(2 + j)))
            c.font = Font(name=FONT, color=GREEN)
            c.number_format = FMT[fmt]

    # ---------------------------------------------------------------- Valuation
    va = wb.create_sheet("Valuation")
    for L, w in (("A", 60), ("B", 14), ("C", 14), ("D", 14), ("E", 74)):
        va.column_dimensions[L].width = w
    va["A1"] = f"{cfg['company']} - P/E to an expected price"
    va["A1"].font = Font(name=FONT, bold=True, size=13)
    va["A2"] = ("For the PM's eye. The system's juice comes from case_brief.py (median house-implied P/E, sigma range); "
                "the two must agree on the earnings.")
    va["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    street = cfg.get("street") or {}
    vfy = cfg.get("valuation_fy", "FY28")
    sub = cfg.get("listed_subsidiary", "the listed subsidiary")
    vrows = [
        ("Valuation basis", "P/E", None, "inp", "Read by the Analysis card (model_view): a P/E model, not EV/EBITDA."),     # 4
        ("Valuation year", vfy, None, "inp", "Sum of that year's four quarters of PAT."),                                   # 5
        ("Quarters in the valuation year", "=COUNTIFS(FYrow,B5)", None, "f", "Must read 4."),                               # 6
        ("PAT, valuation year (Rs cr)", f"=SUMIFS({rng('P&L', r_pat)},FYrow,B5)", "cr", "f", ""),                           # 7
        ("EPS of the valuation year (Rs)", "=B7*10/Shares_mn", "rs", "f", ""),                                              # 8
        ("P/E today, on the valuation year", "=CMP/B8", "x2", "f", ""),                                                     # 9
        ("Target P/E multiple (x)", "=Target_pe", "x2", "key",
         "Inputs!Target_pe: the median of the houses' implied P/E (TP / consensus EPS), which case_brief.py recomputes "
         "from build.json house_tps. Retype after a re-strike, or put your own multiple here."),                           # 10
        ("Expected price (Rs)", "=B8*B10", "rs", "f", ""),                                                                  # 11
        ("Upside vs CMP", "=B11/CMP-1", "pct", "f", ""),                                                                    # 12
        ("Dividend per share in the valuation year (Rs)", f"=SUMIFS({rng('Cash flow', r_div)},FYrow,B5)*10/Shares_mn", "rs", "f", ""),  # 13
        ("Total return vs CMP, price plus that dividend", "=(B11+B13)/CMP-1", "pct", "f", ""),                              # 14
        ("", None, None, "h", ""),                                                                                          # 15
        ("Cross-check: the houses' route (book value plus the listed subsidiary)", None, None, "h",
         f"The houses value the regulated book on P/BV and add the stake in {sub}. Shown, never the target."),             # 16
        ("Book value per share at the end of the valuation year (Rs)", f"={yend('Cash flow', r_nw, 'B5')}*10/Shares_mn", "rs", "f", ""),  # 17
        (f"Less: investment in {sub} carried in that book, per share (Rs)", "=Sub_book_ps", "rs", "key", "Inputs!Sub_book_ps."),          # 18
        ("Book value per share ex the listed subsidiary (Rs)", "=B17-B18", "rs", "f", ""),                                  # 19
        ("Target P/BV on that book (x)", "=Target_pbv", "x2", "key", "Inputs!Target_pbv."),                                 # 20
        (f"Plus: value of the stake in {sub}, per share (Rs)", "=Sub_value_ps", "rs", "key", "Inputs!Sub_value_ps."),       # 21
        ("Price on the houses' route (Rs)", "=B19*B20+B21", "rs", "f", ""),                                                 # 22
        ("Implied P/E of that price", "=B22/B8", "x2", "f", ""),                                                            # 23
        ("", None, None, "h", ""),                                                                                          # 24
        ("Rolling four quarters forward from the price date", None, None, "h",
         "EPS of the 12 months starting on the price date, each quarter weighted by the share of its days inside the "
         "window (the P&L's weight row), at the same target P/E."),                                                        # 25
        ("Price date", f"={D}", "date", "f", ""),                                                                           # 26
        ("EPS, next four quarters from the price date (Rs)", f"=SUMPRODUCT({rng('P&L', r_w)},{rng('P&L', r_eps)})", "rs", "f", ""),  # 27
        ("Expected price, rolling 12 months forward (Rs)", "=B27*B10", "rs", "f", ""),                                      # 28
        ("Upside vs CMP, rolling", "=B28/CMP-1", "pct", "f", ""),                                                           # 29
    ]
    for k, (lab, v, fmt, kind, note) in enumerate(vrows):
        r = 4 + k
        bold = lab in ("Expected price (Rs)", "Upside vs CMP") or kind == "h"
        va.cell(r, 1, lab).font = Font(name=FONT, bold=bold)
        if v is not None:
            c = va.cell(r, 2, v)
            is_f = isinstance(v, str) and v.startswith("=")
            linked = is_f and any(t in v for t in ("!", "FYrow", "Qrow", "Status", "Shares_mn", "CMP", "Target_", "Open_", "Sub_"))
            c.font = Font(name=FONT, color=(GREEN if linked else BLACK) if is_f else BLUE, bold=bold)
            if fmt:
                c.number_format = FMT[fmt]
            if kind in ("inp", "key"):
                c.fill = YELLOW
        if note:
            va.cell(r, 5, note).font = Font(name=FONT, size=8, italic=True, color="595959")
    sr = 4 + len(vrows) + 1
    va.cell(sr, 1, "Sensitivity: expected price (Rs) by P/E and by the valuation year's EPS").font = Font(name=FONT, bold=True)
    va.cell(sr + 1, 1, "EPS shift  \\  P/E shift").font = Font(name=FONT, size=9)
    for j, dm in enumerate((-2, -1, 0, 1, 2)):
        c = va.cell(sr + 1, 2 + j, dm)
        c.font = Font(name=FONT, color=BLUE)
        c.number_format = '+0.0"x";-0.0"x";"base"'
    for k, de in enumerate((-0.10, -0.05, 0, 0.05, 0.10)):
        r = sr + 2 + k
        c = va.cell(r, 1, de)
        c.font = Font(name=FONT, color=BLUE)
        c.number_format = '+0%;-0%;"base"'
        for j in range(5):
            L = get_column_letter(2 + j)
            cc = va.cell(r, 2 + j, f"=($B$10+{L}${sr + 1})*$B$8*(1+$A{r})")
            cc.font = Font(name=FONT, color=GREEN)
            cc.number_format = FMT["rs"]
    stb = sr + 8
    for j, h in enumerate(("Street check: consensus EPS (type in) against the model", "Street", "Model", "Model vs street")):
        va.cell(stb, 1 + j, h).font = Font(name=FONT, bold=True)
    for k, fy in enumerate(("FY27", "FY28", "FY29")):
        r = stb + 1 + k
        va.cell(r, 1, f"Consensus EPS {fy} (Rs)").font = Font(name=FONT)
        s = (street.get(fy) or {})
        c = va.cell(r, 2, s.get("eps"))
        c.fill = YELLOW
        c.font = Font(name=FONT, color=BLUE)
        c.number_format = FMT["rs"]
        c = va.cell(r, 3, f"=SUMIFS({rng('P&L', r_eps)},FYrow,\"{fy}\")")
        c.font = Font(name=FONT, color=GREEN)
        c.number_format = FMT["rs"]
        c = va.cell(r, 4, f'=IF(ISNUMBER(B{r}),C{r}/B{r}-1,"")')
        c.number_format = FMT["pct"]
        if s.get("source"):
            va.cell(r, 5, s["source"]).font = Font(name=FONT, size=8, italic=True, color="595959")

    evi = ev_inputs(cfg if cfg.get("house_tps") else
                    json.loads((out.parent / "build.json").read_text(encoding="utf-8")) if (out.parent / "build.json").exists() else None)
    if evi["houses"]:
        ev_block(va, evi["houses"], rng("P&L", r_ebitda), rng("Cash flow", r_nd), basis=evi["basis"], net_debt=evi["net_debt"])

    # ---------------------------------------------------------------- Sources
    so = wb.create_sheet("Sources")
    for L, w in (("A", 48), ("B", 22), ("C", 120)):
        so.column_dimensions[L].width = w
    for j, h in enumerate(("Item", "Quarters", "Where it comes from")):
        c = so.cell(1, 1 + j, h)
        c.font = Font(name=FONT, bold=True, color="FFFFFF")
        c.fill = HEAD
    rr = 2
    for item in cfg.get("sources", []):
        for j, v in enumerate(item):
            c = so.cell(rr, 1 + j, v)
            c.font = Font(name=FONT, size=9)
            c.alignment = Alignment(wrap_text=True, vertical="top")
        rr += 1
    rr += 1
    so.cell(rr, 1, "Results documents").font = Font(name=FONT, bold=True)
    rr += 1
    for q, u in (cfg.get("decks") or {}).items():
        so.cell(rr, 1, q).font = Font(name=FONT, size=9)
        c = so.cell(rr, 3, u)
        c.font = Font(name=FONT, size=9, color=BLUE, underline="single")
        c.hyperlink = u
        rr += 1

    wb.move_sheet("README", offset=-wb.index(wb["README"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return {"quarters": N, "actual": n_act}


# --------------------------------------------------------------------------
# EV/EBITDA beside the P/E, and the houses' targets as implied multiples
# --------------------------------------------------------------------------
EV_HEAD = "Houses' targets and the multiple each one implies"
EV_PE_LABELS = ("Valuation basis", "Valuation year", "Quarters in the valuation year", "PAT, valuation year (Rs cr)",
                "EPS of the valuation year (Rs)", "P/E today, on the valuation year", "Target P/E multiple (x)",
                "Expected price (Rs)", "Upside vs CMP", "Dividend per share in the valuation year (Rs)",
                "Total return vs CMP, price plus that dividend")


def ev_inputs(build: dict | None) -> dict:
    """What ev_block takes from a build.json: the targets, and for a case that
    RUNS on EV/EBITDA (specs/book.yaml case_valuation_names) the basis the
    Analysis card should read and the case's own sourced net debt, so the
    workbook and case_brief.py bridge enterprise value to equity with one
    figure."""
    b = build or {}
    ev = b.get("valuation") == "ev_ebitda"
    nd = (b.get("net_debt_cr"), b.get("net_debt_asof") or "the case build") if ev and isinstance(b.get("net_debt_cr"), (int, float)) else None
    return {"houses": b.get("house_tps") or [], "basis": "EV/EBITDA" if ev else None, "net_debt": nd}


def ev_block(va, houses: list[dict], ebitda_rng: str, netdebt_rng: str, top: int | None = None,
             basis: str | None = None, net_debt: tuple | None = None) -> dict:
    """PM, 2026-11-22: *"can we do an ev/ebitda analysis ... Get the implied
    EV/ebitda for all houses, make a column at the side of the P/E"*.

    Column C of the P/E block carries the same steps on EV/EBITDA (column D
    says what each cell is), and a table under the sheet lists every house's
    target with the P/E and the EV/EBITDA it implies. Column B is not touched:
    model_view and the Analysis card read B, and the basis stays P/E.

    EV = price x shares + net debt at the LAST REPORTED quarter, for today's
    multiple and for every house alike, so the median multiple times the
    EBITDA less that net debt is the median target exactly. EBITDA is the
    MODEL's valuation-year figure: no consensus EBITDA is stored for a name
    outside the daily capture. Minority interests and the JV investments are
    not adjusted, on either side. The implied P/E uses the typed consensus
    EPS of the valuation year, as case_brief.py does.

    `basis` ("EV/EBITDA", PM 2026-11-22: "Switch to EV/EBITDA") is typed into
    the basis cell in column B, the one cell of the P/E block this changes.
    model_view._val_col then reads the column whose header in the row above
    matches it, so the Analysis card prints column C. `net_debt` (value,
    as-of text) types the case build's sourced net debt in place of the
    model's last reported quarter: the model's cash is a plug with no working
    capital and ran Rs11,800cr above the reported figure at March 2026.

    Writes into `va` and returns the cells it wrote (the live writer turns
    them into Excel operations)."""
    L = _rows(va)
    miss = [x for x in EV_PE_LABELS if x not in L]
    cons = sorted(r for lab, r in L.items() if lab.startswith("Consensus EPS "))
    if miss or not cons:
        raise SystemExit("cannot place the EV/EBITDA column: the Valuation sheet lacks " + ", ".join(miss or ["the consensus EPS rows"]))
    rb, ry, rq, rpat, reps, rpe, rtg, rtp, rup, rdv, rtr = (L[x] for x in EV_PE_LABELS)
    hs = sorted((h for h in houses if isinstance(h.get("tp"), (int, float))), key=lambda h: h["tp"])
    top = top or va.max_row + 2
    h1, hn = top + 2, top + 1 + len(hs)
    done: list[str] = []
    va.column_dimensions["D"].width = 46

    def put(r, c, v, fmt=None, color=BLACK, bold=False, fill=False, note=False):
        cell = va.cell(r, c, v)
        cell.font = (Font(name=FONT, size=8, italic=True, color="595959") if note
                     else Font(name=FONT, color=color, bold=bold))
        if fmt:
            cell.number_format = FMT[fmt]
        if fill:
            cell.fill = YELLOW
        done.append(cell.coordinate)

    # the column beside the P/E
    put(rb - 1, 2, "P/E", bold=True)
    put(rb - 1, 3, "EV/EBITDA", bold=True)
    put(rb - 1, 4, "What the EV/EBITDA column holds", note=True)
    put(rb, 3, "EV/EBITDA")
    if basis:
        put(rb, 2, basis, color=BLUE, fill=True)
        put(rb, 4, f"B{rb} is the basis the Analysis card reads: it takes the column headed with that word (P/E or EV/EBITDA).", note=True)
    else:
        put(rb, 4, "Shown beside the P/E. The Analysis card reads the column B4 names (P/E).", note=True)
    put(ry, 3, f"=B{ry}", color=GREEN)
    put(rq, 3, f"=B{rq}", color=GREEN)
    put(rpat, 3, f"=SUMIFS({ebitda_rng},FYrow,B{ry})", "cr", GREEN)
    put(rpat, 4, "EBITDA, valuation year (Rs cr): the model's")
    if net_debt:
        put(reps, 3, float(net_debt[0]), "cr", BLUE, fill=True)
        put(reps, 4, f"Net debt (Rs cr), as of {net_debt[1]}")        # model_view reads the date after "as of"
    else:
        put(reps, 3, f'=LOOKUP(2,1/(Status="A"),{netdebt_rng})', "cr", GREEN)
        put(reps, 4, "Net debt (Rs cr), as of the last reported quarter")
    put(rpe, 3, f"=(CMP*Shares_mn/10+C{reps})/C{rpat}", "x2", GREEN)
    put(rpe, 4, "EV/EBITDA today, on the valuation year")
    put(rtg, 3, f"=MEDIAN(D{h1}:D{hn})", "x2", GREEN, bold=True)
    put(rtg, 4, "Target EV/EBITDA (x): the houses' median, below")
    put(rtp, 3, f"=(C{rtg}*C{rpat}-C{reps})*10/Shares_mn", "rs", bold=True)
    put(rtp, 4, "Expected price on EV/EBITDA (Rs)", bold=True)
    put(rup, 3, f"=C{rtp}/CMP-1", "pct", GREEN, bold=True)
    put(rdv, 3, f"=B{rdv}", "rs", GREEN)
    put(rtr, 3, f"=(C{rtp}+C{rdv})/CMP-1", "pct", GREEN)

    # the houses
    eps = f'INDEX($B${cons[0]}:$B${cons[-1]},MATCH("Consensus EPS "&$B${ry}&" (Rs)",$A${cons[0]}:$A${cons[-1]},0))'
    put(top, 1, EV_HEAD, bold=True)
    put(top, 5, "Implied P/E = target / the consensus EPS typed above for the valuation year. Implied EV/EBITDA = "
                f"(target x shares + the net debt in C{reps}) / the MODEL's valuation-year EBITDA; "
                "minorities and JV investments are not adjusted. Retype a target after a re-strike.", note=True)
    for j, h in enumerate(("House", "Target (Rs)", "Implied P/E", "Implied EV/EBITDA", "Date, rating and basis")):
        put(top + 1, 1 + j, h, bold=True)
    for k, h in enumerate(hs):
        r = h1 + k
        put(r, 1, str(h.get("house", "")))
        put(r, 2, float(h["tp"]), "rs", BLUE, fill=True)
        put(r, 3, f"=B{r}/{eps}", "x2")
        put(r, 4, f"=(B{r}*Shares_mn/10+$C${reps})/$C${rpat}", "x2", GREEN)
        put(r, 5, ", ".join(str(x) for x in (h.get("date"), h.get("basis")) if x), note=True)
    foot = [("Median", "MEDIAN"), ("Mean", "AVERAGE")]
    for k, (lab, fn) in enumerate(foot):
        r = hn + 1 + k
        put(r, 1, lab, bold=True)
        for c, f in ((2, "rs"), (3, "x2"), (4, "x2")):
            Lc = get_column_letter(c)
            put(r, c, f"={fn}({Lc}{h1}:{Lc}{hn})", f, bold=True)
    r = hn + 3
    put(r, 1, "Highest over lowest")
    for c in (2, 3, 4):
        Lc = get_column_letter(c)
        put(r, c, f"=MAX({Lc}{h1}:{Lc}{hn})/MIN({Lc}{h1}:{Lc}{hn})-1", "pct")
    put(r, 5, "Debt is most of the enterprise value, so the same spread of targets is a much narrower spread of EV/EBITDA.", note=True)
    r = hn + 4
    put(r, 1, "At today's price")
    put(r, 2, "=CMP", "rs", GREEN)
    put(r, 3, f"=CMP/{eps}", "x2", GREEN)
    put(r, 4, f"=C{rpe}", "x2", GREEN)
    return {"top": top, "first": h1, "last": hn, "end": hn + 4, "cells": done, "tp_cell": f"C{rtp}", "target_cell": f"C{rtg}"}


def add_ev_ebitda(entity: str, force: bool = False, base: pathlib.Path | None = None) -> int:
    """Write the EV/EBITDA column and the house table into an EXISTING workbook
    through Excel, so the PM's yellow cells survive and an open window takes
    the edit in place. Targets come from build.json `house_tps` beside the
    workbook. Only Valuation columns C and D beside the P/E block, row 3 and
    the table under the sheet are written."""
    import model_view
    from power_model import _bgr
    base = base or model_view.CASES
    p = model_view.path_for(entity, base)
    if p is None:
        raise SystemExit(f"no model for {entity!r}")
    bj = p.parent / "build.json"
    evi = ev_inputs(json.loads(bj.read_text(encoding="utf-8")) if bj.exists() else None)
    houses = evi["houses"]
    if not houses:
        raise SystemExit(f"{bj} carries no house_tps: nothing to imply a multiple from")
    live = load_workbook(p)
    va, inp = live["Valuation"], live["Inputs"]
    n = 0
    while inp.cell(4, FIRST_COL + n).value:
        n += 1
    last = get_column_letter(FIRST_COL + n - 1)
    PLr, CFr = _rows(live["P&L"]), _rows(live["Cash flow"])
    if "EBITDA" not in PLr or "Net debt, closing" not in CFr:
        raise SystemExit("the workbook has no EBITDA or net debt row; refusing to guess")
    old = next((r for r in range(1, va.max_row + 1) if va.cell(r, 1).value == EV_HEAD), None)
    if old and not force:
        raise SystemExit(f"the EV/EBITDA block is already in {p.name}; --force rewrites it (its typed targets too)")
    ops = []
    if old:
        ops.append({"sheet": "Valuation", "cell": f"A{old}", "mode": "values",
                    "value": [[None] * 5 for _ in range(va.max_row - old + 1)]})
        for r in range(old, va.max_row + 1):
            for c in range(1, 6):
                va.cell(r, c).value = None
    info = ev_block(va, houses, f"'P&L'!$D${PLr['EBITDA']}:${last}${PLr['EBITDA']}",
                    f"'Cash flow'!$D${CFr['Net debt, closing']}:${last}${CFr['Net debt, closing']}", top=old,
                    basis=evi["basis"], net_debt=evi["net_debt"])
    rgb = lambda c: c.rgb[-6:] if c is not None and isinstance(c.rgb, str) else None
    ops.append({"sheet": "Valuation", "cell": "D1", "mode": "width", "value": va.column_dimensions["D"].width})
    for co in info["cells"]:
        c = va[co]
        v = c.value
        if isinstance(v, str) and not v.startswith("="):
            v = "'" + v
        solid = c.fill is not None and c.fill.fill_type == "solid"
        ops.append({"sheet": "Valuation", "cell": co, "mode": "set", "value": v, "font": c.font.name or FONT,
                    "numfmt": None if c.number_format == "General" else c.number_format,
                    "fcolor": _bgr(rgb(c.font.color) or BLACK), "bold": bool(c.font.b), "italic": bool(c.font.i),
                    "size": c.font.sz, "fill": _bgr(rgb(c.fill.fgColor)) if solid else None})
    res = model_view.excel(entity, ops, base)
    print(f"{p.name}: EV/EBITDA column and {info['last'] - info['first'] + 1} houses written to Valuation, "
          f"{len(ops)} operations" + (" (into the open window)" if res.get("attached") else ""))
    for note in res.get("notes") or []:
        print(f"  NOTE {note}")
    v = load_workbook(p, data_only=True)["Valuation"]
    R = _rows(v)
    g = lambda lab, c: v.cell(R[lab], c).value
    print(f"  basis {g('Valuation basis', 2)}; today {g('P/E today, on the valuation year', 3):.2f}x, target {g('Target P/E multiple (x)', 3):.2f}x, "
          f"expected price Rs{g('Expected price (Rs)', 3):.1f} (P/E route Rs{g('Expected price (Rs)', 2):.1f})")
    bad = errors(p)
    for b in bad[:20]:
        print(f"  ERROR CELL {b}")
    return 1 if bad else 0


def readme_lines(cfg, qs, n_act):
    return [
        (f"{cfg['company']} - quarterly model", "t"),
        (f"Built {cfg['built']} by packages/book/genco_model.py from {cfg['entity']}/model_config.json. "
         f"Actuals {qs[0]} to {qs[n_act - 1]}; forecasts to {qs[-1]}. Consolidated, Rs crore.", ""),
        ("", ""),
        ("How it flows", "h"),
        ("Inputs (timeline, constants, the regulated-equity and capex plans, quarter shapes) -> Regulated equity "
         "(additions, closing and average regulated equity, capex) -> P&L (fuel passed through; margin over fuel = "
         "average regulated equity x yield; opex and depreciation as rates on the same base; interest on opening "
         "debt) -> Cash flow (FCF, dividends, debt, cash, net worth) -> Annual -> Valuation (P/E on the valuation "
         "year's EPS; the houses' book-value route as a cross-check; rolling 12 months).", ""),
        ("", ""),
        ("Colours", "h"),
        ("Blue = typed. Black = formula. Green = link to another sheet. Yellow = a key assumption or the PM's override. "
         "Status row: A = reported quarter, F = forecast.", ""),
        ("", ""),
        ("Rolling a new quarter", "h"),
        ("Type the quarter's reported lines into the blue 'Reported (typed)' blocks on Regulated equity, P&L and Cash "
         "flow, then flip its status on Inputs row 6 from F to A. Every model row switches to the reported value and "
         "the next quarter's drivers re-base. Debt, net worth and capex are typed only when a balance sheet or the "
         "figure is published; blank is fine. Update the fiscal-year plans on Inputs when guidance moves.", "yellow"),
        ("", ""),
        ("What the PM overrides", "h"),
        ("The regulated-equity addition plan (Inputs, per fiscal year) is the lever that moves earnings. Margin over "
         "fuel and other income have yellow override rows on P&L for a visible-quarter view. Margin_drift, Opex_drift "
         "and Dep_drift (Inputs) say how fast the per-rupee economics change; all are zero until there is a view. "
         "Fuel_growth moves sales and cost together and no profit line.", ""),
        ("", ""),
        ("What the model leaves out, on purpose", "h"),
        ("The tariff plant by plant (RoE, incentives, under-recoveries, O&M norms): the margin yield on regulated "
         "equity carries all of it, measured on reported quarters. Generation, PLF and coal: they move fuel, which is "
         "passed through. Standalone versus subsidiaries and JVs: the model is consolidated; the Memo sheet shows the "
         "reported bridge. Working capital, receivables and regulatory assets. The listed renewables subsidiary is "
         "inside the consolidated lines and appears separately only in the valuation cross-check.", ""),
        ("", ""),
        ("Where the actuals come from", "h"),
        (cfg.get("readme_history") or "The Sources sheet names the document for every line; the Inputs basis row says "
                                      "what each quarter is.", ""),
    ]


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def _fixture() -> dict:
    qs = ["Q1FY26", "Q2FY26"]
    lines = dict(sales=(48885.4, 45911.5), fuel=(26346.0, 24568.0), employee=(1584.4, 1580.4),
                 other_exp=(6555.1, 5821.7), depreciation=(4587.0, 4815.5), interest=(3467.5, 3432.5),
                 other_income=(755.8, 476.3), pbt=(7101.2, 6169.7), tax=(1769.3, 1749.6), jv_share=(476.6, 580.1),
                 minority=(97.9, 158.5), exceptional=(300.0, 225.0), pat=(6010.6, 5066.7),
                 reg_equity=(111393.0, 116022.0), dividends_paid=(0.0, 3248.5))
    e = lambda v, u="", s="t": {"value": v, "unit": u, "source": s}
    return {"company": "Fixture Genco", "entity": "fixture_genco", "built": "2026-11-18",
            "first_quarter": "Q1FY26", "last_quarter": "Q4FY27", "actual_through": "Q2FY26", "valuation_fy": "FY27",
            "basis": {q: "t" for q in qs},
            "constants": {"shares_mn": e(9696.7), "cmp": e(321.8), "cmp_date": e("2026-11-17"), "tax_rate": e(0.24),
                          "int_rate": e(0.058), "debt_share": e(0.7), "payout": e(0.34),
                          "div_split": e([0, 0.4, 0.3, 0.3]), "exceptional": e(0), "fuel_growth": e(0.0),
                          "margin_drift": e(0), "opex_drift": e(0), "dep_drift": e(0), "target_pe": e(13.0),
                          "target_pbv": e(1.75), "sub_book_ps": e(7.7), "sub_value_ps": e(48.7)},
            "opening": {"as_of": "31-Mar-2025", "reg_equity": e(108790), "gross_debt": e(247580), "cash": e(11460),
                        "net_worth": e(184070), "fy_pat": e(22320)},
            "plans": {"reg_equity_addition": {"FY26": e(11529), "FY27": e(8931)},
                      "capex": {"FY26": e(50987), "FY27": e(61363)},
                      "weights_reg_equity": e([0.25, 0.25, 0.25, 0.25]), "weights_capex": e([0.2, 0.25, 0.25, 0.3])},
            "consolidated": {**{k: {q: v[j] for j, q in enumerate(qs)} for k, v in lines.items()},
                             "gross_debt": {"Q2FY26": 255000.0}, "capex": {"Q1FY26": 9000.0}},
            "memo": {"sections": [{"title": "Bridge", "rows": [{"label": "Standalone PAT", "unit": "Rs cr",
                                                                "values": {"Q1FY26": 4774.7, "Q2FY26": 4653.3}}]}]},
            "street": {"FY27": {"eps": 26.0, "source": "t"}},
            "house_tps": [{"house": "C", "tp": 460, "date": "2026-11-02", "basis": "t"}, {"house": "A", "tp": 355},
                          {"house": "B", "tp": 400}],
            "sources": [["a", "b", "c"]], "decks": {}}


def _rows(ws) -> dict:
    return {ws.cell(r, 1).value: r for r in range(1, ws.max_row + 1) if isinstance(ws.cell(r, 1).value, str)}


def selftest() -> int:
    fails = 0

    def chk(label, got, want=True):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))

    print("genco_model selftest")
    fx = _fixture()
    chk("footed fixture accepted", crossfoot(fx), [])
    bad = json.loads(json.dumps(fx)); bad["consolidated"]["tax"]["Q1FY26"] = 1796.3          # transposed digit
    chk("transposed tax digit refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["consolidated"]["fuel"]["Q2FY26"] = 24658.0
    chk("mistyped fuel refused (PBT no longer foots)", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["plans"]["weights_capex"]["value"] = [0.2, 0.25, 0.25, 0.25]
    chk("weights that do not sum to one refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["consolidated"]["gross_debt"]["Q3FY26"] = 1.0
    chk("a sparse figure typed on a forecast quarter refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); del bad["consolidated"]["minority"]
    chk("a missing line refused", len(crossfoot(bad)), 1)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="genco_selftest_"))
    p0 = tmp / "Fixture_model.xlsx"
    info = build(fx, p0)
    chk("built: quarters, actual", (info["quarters"], info["actual"]), (8, 2))
    wb = load_workbook(p0)
    chk("sheets", wb.sheetnames, ["README", "Inputs", RE, "P&L", "Cash flow", "Memo", "Annual", "Valuation", "Sources"])
    chk("CMP and CMP_date are workbook names (model_view.set_cmp writes them)",
        all(n in wb.defined_names for n in ("CMP", "CMP_date", "Target_pe", "REPlan")))
    import model_view
    vr = model_view._val_rows(wb["Valuation"])
    chk("model_view finds the target, basis, year, EPS and typed P/E rows by label",
        sorted(vr), ["basis", "eps", "pe_typed", "tp", "year"])
    chk("the basis row says P/E", wb["Valuation"].cell(vr["basis"], 2).value, "P/E")
    bare = json.loads(json.dumps(fx)); del bare["house_tps"]
    pb = tmp / "Bare_model.xlsx"
    build(bare, pb)
    vb, vh = load_workbook(pb)["Valuation"], wb["Valuation"]
    chk("no house targets, no EV/EBITDA block", EV_HEAD in _rows(vb), False)
    # B3 is the block's own "P/E" header over the existing column, the one cell of column B it writes
    chk("the EV/EBITDA block leaves column B (bar its header) and every other sheet's formulas as they were",
        [f"{w.title}!{c.coordinate}" for w in load_workbook(pb).worksheets for row in w.iter_rows() for c in row
         if not (w.title == "Valuation" and c.row == vr["basis"] - 1) and (w.title != "Valuation" or c.column == 2) and c.value != wb[w.title][c.coordinate].value], [])
    chk("...and the header is what it wrote there", vh.cell(vr["basis"] - 1, 2).value, "P/E")
    chk("nothing outside the block reads it",
        [c.coordinate for row in vh.iter_rows(max_row=_rows(vh)[EV_HEAD] - 1) for c in row
         if c.column not in (3, 4) and isinstance(c.value, str) and c.value.startswith("=")
         and re.search(r"\$?[CD]\$?(%d|%d)\b" % (vr["tp"], vr["pe_typed"]), c.value)], [])
    evfx = json.loads(json.dumps(fx)); evfx.update(valuation="ev_ebitda", net_debt_cr=250000.0, net_debt_asof="Sep-26")
    pe_ = tmp / "Ev_model.xlsx"
    build(evfx, pe_)
    ve = load_workbook(pe_)["Valuation"]
    chk("an EV/EBITDA case types the basis and its own net debt; the card reader takes column C, and B without it",
        (ve.cell(vr["basis"], 2).value, ve.cell(vr["eps"], 3).value, ve.cell(vr["eps"], 4).value,
         model_view._val_col(ve, vr), model_view._val_col(vh, vr), model_view._val_col(vb, vr)),
        ("EV/EBITDA", 250000.0, "Net debt (Rs cr), as of Sep-26", 3, 2, 2))
    chk("...and changes nothing else in column B",
        [c.coordinate for row in ve.iter_rows(min_col=2, max_col=2) for c in row
         if c.row != vr["basis"] and c.value != vh[c.coordinate].value], [])
    reads_memo = [f"{ws.title}!{c.coordinate}" for ws in wb.worksheets if ws.title != "Memo"
                  for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=") and "Memo" in c.value]
    chk("nothing reads the Memo sheet", reads_memo, [])
    pl = wb["P&L"]; R = _rows(pl)
    fuel_f = [pl.cell(R["Fuel cost (passed through)"], FIRST_COL + i).value for i in (2, 4)]
    chk("fuel: a forecast inside the first year carries the previous quarter, later ones the year-ago quarter",
        [fuel_f[0].split(",")[2][0], fuel_f[1].split(",")[2][0]], ["E", "D"])   # the third IF argument: column E is the quarter before F, D the year before H
    rs = wb[RE]; RR = _rows(rs)
    self_ref = [c.coordinate for c in rs[RR["Addition to regulated equity"]] if isinstance(c.value, str)
                and f"${RR['Addition to regulated equity']}:" in c.value]
    chk("the addition row's SUMIFS never ranges over itself (no circular reference)", self_ref, [])

    # ---- the numbers, when Excel is on the box
    try:
        fx2 = json.loads(json.dumps(fx)); fx2["constants"]["fuel_growth"]["value"] = 0.25
        p1 = tmp / "Fixture_fuel_model.xlsx"
        build(fx2, p1)
        recalc(p0.resolve()); recalc(p1.resolve())
    except Exception as ex:            # no Excel: the structural checks above still stand
        print(f"  skip Excel recalculation ({type(ex).__name__}): numbers not checked")
        print(f"genco_model selftest: {'FAILED ' + str(fails) if fails else 'all checks passed'}")
        return 1 if fails else 0
    chk("no error cells after recalculation", errors(p0), [])
    v0, v1 = load_workbook(p0, data_only=True), load_workbook(p1, data_only=True)
    val = lambda w, sheet, label, i: w[sheet].cell(_rows(w[sheet])[label], FIRST_COL + i).value
    chk("model PAT equals reported on both actual quarters",
        [round(val(v0, "P&L", "Check: model PAT less reported (actual quarters)", i), 6) for i in (0, 1)], [0, 0])
    chk("regulated equity closes on the typed figure in actual quarters",
        [val(v0, RE, "Regulated equity, closing", i) for i in (0, 1)], [111393.0, 116022.0])
    fy26 = sum(val(v0, RE, "Addition to regulated equity", i) for i in range(4))
    fy27 = sum(val(v0, RE, "Addition to regulated equity", i) for i in range(4, 8))
    chk("each year's additions sum to its plan (reported quarters netted off)", (round(fy26, 4), round(fy27, 4)), (11529.0, 8931.0))
    cap26 = [val(v0, RE, "Capex, group", i) for i in range(4)]
    chk("capex: the typed quarter stands, the rest of the year's plan is spread over the untyped quarters",
        (cap26[0], round(sum(cap26), 4), round(cap26[1] / cap26[3], 4)), (9000.0, 50987.0, round(0.25 / 0.3, 4)))
    chk("gross debt: rolls by Debt_share x capex, then lands on the typed figure",
        (round(val(v0, "Cash flow", "Gross debt, closing", 0), 2), val(v0, "Cash flow", "Gross debt, closing", 1)),
        (247580 + 0.7 * 9000, 255000.0))
    pat0 = [val(v0, "P&L", "PAT attributable to owners", i) for i in range(8)]
    pat1 = [val(v1, "P&L", "PAT attributable to owners", i) for i in range(8)]
    sal0 = [val(v0, "P&L", "Sales incl. regulatory deferral movement", i) for i in range(8)]
    sal1 = [val(v1, "P&L", "Sales incl. regulatory deferral movement", i) for i in range(8)]
    chk("fuel is passed through: +25% fuel moves no quarter's PAT", max(abs(a - b) for a, b in zip(pat0, pat1)) < 1e-6)
    chk("...and does move forecast sales", sal1[3] > sal0[3] * 1.05)
    V = _rows(v0["Valuation"]); vv = lambda lab: v0["Valuation"].cell(V[lab], 2).value
    eps27 = sum(val(v0, "P&L", "EPS (Rs)", i) for i in range(4, 8))
    chk("expected price = the valuation year's EPS x the target P/E",
        round(vv("Expected price (Rs)"), 6), round(eps27 * 13.0, 6))
    chk("the houses' route: (book ex subsidiary) x P/BV + the stake",
        round(vv("Price on the houses' route (Rs)"), 6),
        round((vv("Book value per share at the end of the valuation year (Rs)") - 7.7) * 1.75 + 48.7, 6))
    hr = V[EV_HEAD]
    imp = {v0["Valuation"].cell(r, 1).value: v0["Valuation"].cell(r, 4).value for r in range(hr + 2, hr + 5)}
    ebitda27 = sum(val(v0, "P&L", "EBITDA", i) for i in range(4, 8))
    nd = val(v0, "Cash flow", "Net debt, closing", 1)
    chk("houses sorted by target; implied EV/EBITDA = (target x shares + last reported net debt) / model EBITDA",
        [(k, round(x, 6)) for k, x in imp.items()],
        [(k, round((tp * 969.67 + nd) / ebitda27, 6)) for k, tp in (("A", 355), ("B", 400), ("C", 460))])
    chk("the median multiple round-trips to the median target", round(v0["Valuation"].cell(V["Expected price (Rs)"], 3).value, 6), 400.0)
    chk("implied P/E = target / the typed consensus EPS of the valuation year",
        round(v0["Valuation"].cell(hr + 3, 3).value, 6), round(400 / 26.0, 6))
    chk("forecast margin = average regulated equity x the trailing yield",
        round(val(v0, "P&L", "Margin over fuel", 2), 4),
        round(val(v0, RE, "Regulated equity, average", 2)
              * (val(v0, "P&L", "Margin yield: (sales - fuel) x 4 / average regulated equity", 0)
                 + val(v0, "P&L", "Margin yield: (sales - fuel) x 4 / average regulated equity", 1)) / 2 / 4, 4))
    print(f"genco_model selftest: {'FAILED ' + str(fails) if fails else 'all checks passed'}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--force", action="store_true", help="overwrite an existing workbook (loses its edits)")
    ap.add_argument("--recalc", action="store_true", help="open in Excel over COM, recalculate, save, check errors")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--add-ev-ebitda", metavar="ENTITY",
                    help="write the EV/EBITDA column and the house table into the live workbook through Excel")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.add_ev_ebitda:
        return add_ev_ebitda(a.add_ev_ebitda, a.force)
    if not (a.config and a.out):
        ap.error("--config and --out are required")
    if a.out.exists() and not a.force:
        raise SystemExit(f"{a.out} exists and may carry your edits; pass --force to rebuild it")
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    info = build(cfg, a.out)
    print(f"wrote {a.out}: {info['quarters']} quarters, {info['actual']} actual")
    if a.recalc:
        recalc(a.out.resolve())
        bad = errors(a.out)
        print(f"recalculated in Excel; {len(bad)} error cells" + "".join(f"\n  {b}" for b in bad[:30]))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
