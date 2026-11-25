"""A quarterly WIND-TURBINE MAKER model in Excel: revenue plan -> segments -> EBITDA -> net debt -> EV/EBITDA -> price.

PM, 2026-11-25, on Inox Wind: *"can you build a model? I cant find jpm and ms
having models for this"*. No house model exists for this name, so every actual
comes from the filed results (screener's consolidated tables, the listed O&M
subsidiary's own quarter) and the earnings calls. The builder follows
genco_model.py / power_model.py (a config, live formulas, recalculated through
Excel, read by model_view), with a driver set of its own:

- TWO SEGMENTS, because the O&M book is about to change size. The listed O&M
  subsidiary (Inox Green) reports its own quarter, so O&M revenue and O&M
  EBITDA are typed from its filing and WTG & EPC is the consolidated figure
  LESS them. Wind World's 4.5GW portfolio consolidates into the O&M arm from
  Q3FY27 (~Rs120cr a quarter at first): a step on Inputs that the WTG ratios
  must not absorb. Inter-company sales between the two are not eliminated
  here (small; stated on README).
- THE DRIVER IS A WTG & EPC REVENUE PLAN PER FISCAL YEAR, not MW. Inox Wind
  stopped stating quarterly MW after Q2FY26 (it declined for Q4FY26 and
  Q1FY27) and now guides revenue (FY28 ~Rs7,500cr). A model run on MW would
  need MW history that was never published. The year's plan (yellow) is
  spread over its forecast quarters by a quarter shape, net of the reported
  quarters (the genco re_add mechanism).
- MW IS IMPLIED: WTG & EPC revenue / revenue per MW, where revenue per MW is
  measured ONLY on quarters whose MW was stated (any typed MW, MW-weighted)
  and then drifts. A yellow MW override per quarter reverses it: typed MW x
  revenue per MW becomes that quarter's WTG revenue. The order book rolls
  forward from the last reported book on that MW and an inflow run-rate, and
  a check row flags a quarter that executes more than its book.
- COSTS: material and "EPC, site and other" expenses as RATIOS to WTG
  revenue (trailing four quarters, revenue-weighted, plus a drift in
  percentage points); employee cost is the FIXED line, grown per quarter.
  O&M EBITDA = O&M revenue x an O&M margin (yellow).
- THE PM'S TWO NUMBERS for the visible quarter are yellow override rows on
  P&L: consolidated revenue and EBITDA margin. A revenue override is taken
  as the total (WTG revenue becomes the residual); a margin override sets
  EBITDA = revenue x margin. Blank = the model's own number.
- CASH: net working capital is typed at balance-sheet dates (screener: other
  assets less cash, less other liabilities, a wider measure than the
  company's) and carried as DAYS of trailing four quarters' revenue; it has
  made every year's free cash flow negative, so it is a yellow input, held at
  the last reported level until there is a view. Net debt rolls on EBITDA +
  other income - interest - tax - change in working capital - capex + equity
  raised, and is reset to borrowings less cash wherever a balance sheet was
  published. Gross debt = net debt + a cash balance held at the last
  reported level; interest is a rate on opening gross debt (it includes
  guarantee and LC charges, so it reads high).
- VALUATION is EV/EBITDA on the valuation year's EBITDA (consolidated, before
  other income: the case basis), at a typed target multiple (the median
  house-implied multiple, which case_brief.py recomputes), less net debt at a
  chosen quarter-end (one year earlier by default, the steel model's fixed
  block). A second line prices the same EBITDA through the CASE'S bridge
  (today's typed net debt), so the workbook and case_brief can be compared on
  one basis. Minority interest is not deducted, as in the case.

Actuals are screener's crore-rounded figures, so a reported quarter's
components foot to within Rs3cr, not to the rupee; `crossfoot` holds that and
refuses anything wider.

    python packages/book/wind_model.py --config data/analysis/cases/inox_wind/model_config.json \\
        --out data/analysis/cases/inox_wind/Inox_Wind_model.xlsx --recalc
    python packages/book/wind_model.py --selftest

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
import sys
import tempfile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from steel_model import (BLACK, BLUE, FIRST_COL, FMT, FONT, GREEN, HEAD, YELLOW,  # noqa: E402
                         Sheet, col, errors, fy_of, name, quarter_dates, quarters, recalc)

REPO = pathlib.Path(__file__).resolve().parents[2]

# reported consolidated lines, typed per actual quarter (Rs crore)
LINES = ("revenue", "material", "employee", "other_exp", "other_income", "exceptional", "interest",
         "depreciation", "pbt", "tax", "minority", "pat")
OM_LINES = ("om_revenue", "om_ebitda")          # the listed O&M subsidiary's own quarter
SPARSE = ("gross_debt", "cash", "nwc")          # balance-sheet dates only
MWFMT = '#,##0;(#,##0);"-"'


def crossfoot(cfg: dict, tol: float = 3.0) -> list[str]:
    """Every reported quarter must foot before anything is written: EBITDA from
    its cost lines (against the reported operating profit where typed), PBT
    from EBITDA and the lines below it, PAT = PBT - tax - minority. The
    actuals are crore-rounded, so the tolerance is Rs3cr; a transposed digit
    is far outside it. Quarter weights must sum to one, or a year's plan would
    be silently scaled. Revenue per MW needs at least one quarter whose MW was
    stated, and the order book rolls from the LAST reported quarter's book."""
    c, o = cfg["consolidated"], cfg["operations"]
    errs = []
    acts = list(c.get("pat", {}))
    for k in LINES:
        if k not in c:
            errs.append(f"consolidated.{k}: missing")
        elif set(c[k]) != set(acts):
            errs.append(f"consolidated.{k}: quarters differ from pat")
    for k in OM_LINES:
        if set(cfg["om"].get(k, {})) != set(acts):
            errs.append(f"om.{k}: quarters differ from pat")
    if errs:
        return errs
    qs = quarters(cfg["first_quarter"], cfg["last_quarter"])
    want = qs[:qs.index(cfg["actual_through"]) + 1]
    if acts != want:
        errs.append(f"consolidated quarters {acts} are not {want[0]}..{want[-1]}")
    for k in SPARSE:
        for q in cfg.get("balance_sheet", {}).get(k, {}):
            if q not in acts:
                errs.append(f"balance_sheet.{k}: {q} is not an actual quarter")
    for k in ("mw_executed", "inflow_mw", "order_book_mw"):
        for q in o.get(k, {}):
            if q not in acts:
                errs.append(f"operations.{k}: {q} is not an actual quarter")
    if not any(v and v > 0 for v in o.get("mw_executed", {}).values()):
        errs.append("operations.mw_executed: revenue per MW needs at least one quarter with stated MW")
    if acts and acts[-1] not in o.get("order_book_mw", {}):
        errs.append(f"operations.order_book_mw: the last reported quarter ({acts[-1]}) needs its book; "
                    "the forecast rolls from it")
    for q in acts:
        if c["revenue"][q] <= 0:
            errs.append(f"{q}: revenue must be positive")
            continue
        ebitda = c["revenue"][q] - c["material"][q] - c["employee"][q] - c["other_exp"][q]
        reported = cfg.get("reported_ebitda", {}).get(q)
        if reported is not None and abs(ebitda - reported) > tol:
            errs.append(f"{q}: EBITDA from cost lines {ebitda:,.1f} vs reported {reported:,.1f}")
        pbt = ebitda + c["other_income"][q] + c["exceptional"][q] - c["interest"][q] - c["depreciation"][q]
        if abs(pbt - c["pbt"][q]) > tol:
            errs.append(f"{q}: PBT components {pbt:,.1f} vs reported {c['pbt'][q]:,.1f}")
        pat = c["pbt"][q] - c["tax"][q] - c["minority"][q]
        if abs(pat - c["pat"][q]) > tol:
            errs.append(f"{q}: PBT - tax - minority {pat:,.1f} vs PAT to owners {c['pat'][q]:,.1f}")
        if cfg["om"]["om_revenue"][q] >= c["revenue"][q]:
            errs.append(f"{q}: O&M revenue is not inside consolidated revenue")
    for k in ("weights_revenue", "weights_inflow", "weights_capex"):
        w = cfg["plans"][k]["value"]
        if len(w) != 4 or abs(sum(w) - 1.0) > 1e-6:
            errs.append(f"plans.{k}: four weights summing to 1.0, got {w}")
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
    cs, om, ops, bs = cfg["consolidated"], cfg["om"], cfg["operations"], cfg.get("balance_sheet", {})
    typed = lambda d, key: {q: d[key][q] for q in qs if q in act and q in d.get(key, {})}
    wb = Workbook()
    wb.remove(wb.active)
    c_ = col

    def A(i):
        return f'Inputs!{col(i)}$6="A"'

    def rng(sheet, r):
        return f"'{sheet}'!$D${r}:${last}${r}"

    def prev4(sheet, r, i):
        lo = max(0, i - 4)
        return f"AVERAGE('{sheet}'!{col(lo)}{r}:{col(i - 1)}{r})"

    def wsum4(sheet, num, den, i):
        """Trailing four quarters' sum(num) / sum(den): a revenue-weighted ratio."""
        lo = max(0, i - 4)
        return (f"SUM('{sheet}'!{col(lo)}{num}:{col(i - 1)}{num})"
                f"/SUM('{sheet}'!{col(lo)}{den}:{col(i - 1)}{den})")

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
                "Yellow = key assumption or the PM's override. Rs crore and MW unless stated.")
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
    name(wb, "Qidx", f"Inputs!$D$7:${last}$7")
    inp.r = 8
    inp.row("Basis of the actuals", "", {q: cfg["basis"].get(q) for q in qs if q in act}, fmt="cr")
    for i in range(N):
        cell = ws.cell(8, FIRST_COL + i)
        cell.font = Font(name=FONT, size=7, color=BLUE)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[8].height = 60
    inp.row("Quarter start", "date", {q: dt.date.fromisoformat(quarter_dates(q)[0]) for q in qs}, fmt="date")
    inp.row("Quarter end", "date", {q: dt.date.fromisoformat(quarter_dates(q)[1]) for q in qs}, fmt="date")

    inp.section("Company constants (A label, B unit, C value, D source)")

    def const(key, label, entry, fmt, yellow=False):
        r = inp.r
        ws.cell(r, 1, label).font = Font(name=FONT)
        ws.cell(r, 2, entry.get("unit", "")).font = Font(name=FONT, size=9, color="595959")
        c = ws.cell(r, 3, entry["value"])
        c.font = Font(name=FONT, color=BLUE)
        if fmt:
            c.number_format = MWFMT if fmt == "mw" else FMT[fmt]
        if yellow:
            c.fill = YELLOW
        ws.cell(r, 4, entry.get("source", "")).font = Font(name=FONT, size=8, italic=True, color="595959")
        name(wb, key, f"Inputs!$C${r}")
        inp.r += 1

    const("Shares_mn", "Shares outstanding", C["shares_mn"], "mn")
    const("CMP", "Current market price", C["cmp"], "rs")
    const("CMP_date", "Price date", C["cmp_date"], None)
    const("Tax_rate", "Effective tax rate on PBT", C["tax_rate"], "pct", yellow=True)
    const("Int_rate", "Finance cost: annual rate on opening gross debt (incl. LC / guarantee charges)", C["int_rate"],
          "pct", yellow=True)
    const("RPM_drift", "Revenue per MW: drift per quarter", C["rpm_drift"], "pct", yellow=True)
    const("Mat_drift", "Material cost ratio: drift in pp of WTG revenue per quarter", C["mat_drift"], "pct", yellow=True)
    const("Oth_drift", "EPC, site and other cost ratio: drift in pp per quarter", C["oth_drift"], "pct", yellow=True)
    const("Emp_growth", "Employee cost growth per quarter (the fixed line)", C["emp_growth"], "pct", yellow=True)
    const("OM_growth", "O&M revenue growth per quarter, before the Wind World step", C["om_growth"], "pct", yellow=True)
    const("OM_margin", "O&M EBITDA margin on forecast quarters", C["om_margin"], "pct", yellow=True)
    const("OI_q", "Other income per forecast quarter", C["oi_q"], "cr", yellow=True)
    const("Dep_growth", "Depreciation growth per quarter", C["dep_growth"], "pct", yellow=True)
    const("WC_days", "Net working capital, days of trailing four quarters' revenue (forecast)", C["wc_days"], "m",
          yellow=True)
    const("Cash_hold", "Cash held on forecast quarters (gross debt = net debt + this)", C["cash_hold"], "cr", yellow=True)
    const("Target_ev", "Target EV/EBITDA multiple (the Valuation sheet reads this)", C["target_ev"], "x2", yellow=True)
    const("Case_nd", "Net debt in the case's bridge (build.json net_debt_cr)", C["case_nd"], "cr")

    inp.section(f"Opening balance sheet, {O['as_of']} (before the first quarter)")
    const("Open_debt", "Gross debt", O["gross_debt"], "cr")
    const("Open_cash", "Cash and equivalents", O["cash"], "cr")
    const("Open_NWC", "Net working capital and other (screener: other assets less cash, less other liabilities)",
          O["nwc"], "cr")

    def plan(title, key, nm_years, nm_vals, fmt="cr"):
        inp.section(title)
        first = inp.r
        for fy, e in P[key].items():
            ws.cell(inp.r, 1, fy).font = Font(name=FONT)
            c = ws.cell(inp.r, 3, e["value"])
            c.font = Font(name=FONT, color=BLUE)
            c.fill = YELLOW
            c.number_format = MWFMT if fmt == "mw" else FMT[fmt]
            ws.cell(inp.r, 4, e["source"]).font = Font(name=FONT, size=8, italic=True, color="595959")
            inp.r += 1
        name(wb, nm_years, f"Inputs!$A${first}:$A${inp.r - 1}")
        name(wb, nm_vals, f"Inputs!$C${first}:$C${inp.r - 1}")

    plan("WTG & EPC revenue per fiscal year (Rs crore) - the driver", "wtg_revenue", "WtgYears", "WtgPlan")
    plan("Order inflow per fiscal year, MW (a run-rate: each forecast quarter takes its weight's share)",
         "inflow_mw", "InYears", "InPlan", fmt="mw")
    plan("Capex per fiscal year (Rs crore)", "capex", "CapexYears", "CapexPlan")
    plan("Equity raised per fiscal year (Rs crore)", "equity", "EqYears", "EqPlan")

    inp.section("Quarter shapes: share of a year's plan in each quarter")
    wrv, win, wcx = (P[k]["value"] for k in ("weights_revenue", "weights_inflow", "weights_capex"))
    qn = lambda q: int(q[1]) - 1
    r_wrv = inp.row("WTG revenue weight by quarter", "share", {q: wrv[qn(q)] for q in qs}, fmt="pct",
                    fill=lambda i, q: True)
    r_win = inp.row("Order-inflow weight by quarter", "share", {q: win[qn(q)] for q in qs}, fmt="pct",
                    fill=lambda i, q: True)
    r_wcx = inp.row("Capex and equity weight by quarter", "share", {q: wcx[qn(q)] for q in qs}, fmt="pct",
                    fill=lambda i, q: True)
    r_ww = inp.row("Wind World O&M revenue added (Rs cr a quarter)", "Rs cr",
                   {q: cfg["wind_world"]["revenue"].get(q, 0) for q in qs if q not in act}, fmt="cr",
                   fill=lambda i, q: q not in act)
    inp.r += 1
    for lab, src in (("WTG revenue weight", P["weights_revenue"]["source"]),
                     ("Order-inflow weight", P["weights_inflow"]["source"]),
                     ("Capex weight", P["weights_capex"]["source"]),
                     ("Wind World", cfg["wind_world"]["source"])):
        ws.cell(inp.r, 1, f"{lab}: {src}").font = Font(name=FONT, size=8, italic=True, color="595959")
        inp.r += 1
    fyc = lambda i: f"Inputs!{col(i)}$5"

    # ---------------------------------------------------------------- P&L (built before Operations: MW reads it)
    PL = Sheet(wb, "P&L", qs, f"{cfg['company']} - consolidated P&L, by segment",
               "Reported rows typed; model rows read them while the status is A. WTG & EPC = consolidated less the "
               "listed O&M subsidiary. EBITDA is before other income (the case basis). Rs crore.")
    PL.header()
    PL.section("Reported (typed): consolidated")
    r_rev_t = PL.row("Revenue from operations, reported", "Rs cr", typed(cs, "revenue"))
    r_mat_t = PL.row("Material cost (incl. purchases and stock change), reported", "Rs cr", typed(cs, "material"))
    r_emp_t = PL.row("Employee cost, reported", "Rs cr", typed(cs, "employee"))
    r_oth_t = PL.row("EPC, O&M, site and other expenses, reported", "Rs cr", typed(cs, "other_exp"))
    r_oi_t = PL.row("Other income, reported (ex exceptional)", "Rs cr", typed(cs, "other_income"))
    r_xo_t = PL.row("Exceptional items, reported (pre-tax)", "Rs cr", typed(cs, "exceptional"))
    r_int_t = PL.row("Finance cost, reported", "Rs cr", typed(cs, "interest"))
    r_dep_t = PL.row("Depreciation, reported", "Rs cr", typed(cs, "depreciation"))
    PL.row("PBT, reported", "Rs cr", typed(cs, "pbt"))
    r_tax_t = PL.row("Tax, reported", "Rs cr", typed(cs, "tax"))
    r_mi_t = PL.row("Minority interest, reported (deducted)", "Rs cr", typed(cs, "minority"))
    r_pat_t = PL.row("PAT attributable to owners, reported", "Rs cr", typed(cs, "pat"))
    PL.section("Reported (typed): the listed O&M subsidiary's own quarter")
    r_omr_t = PL.row("O&M revenue (subsidiary's consolidated revenue)", "Rs cr", typed(om, "om_revenue"))
    r_ome_t = PL.row("O&M EBITDA (subsidiary's operating profit, before other income)", "Rs cr", typed(om, "om_ebitda"))
    PL.section("Reported (typed): MW executed, where the company or a house stated it")
    r_mw_t = PL.row("MW executed, stated (blank = not stated)", "MW", typed(ops, "mw_executed"), fmt="cr")

    PL.section("The PM's overrides (yellow; blank = the model's own number)")
    r_rev_ov = PL.row("Consolidated revenue override", "Rs cr", {}, fill=lambda i, q: q not in act)
    r_mgn_ov = PL.row("Consolidated EBITDA margin override", "%", {}, fmt="pct", fill=lambda i, q: q not in act)
    r_mw_ov = PL.row("MW executed override (WTG revenue = MW x revenue per MW)", "MW", {},
                     fill=lambda i, q: q not in act)
    PL.ws.cell(PL.r, 1, "The fiscal-year plans (WTG revenue, inflow, capex) are on Inputs.").font = \
        Font(name=FONT, size=8, italic=True, color="595959")
    PL.r += 1

    PL.section("O&M segment")
    r_omr = PL.row("O&M revenue", "Rs cr",
                   lambda i, q, r: (f"=IF({A(i)},{c_(i)}{r_omr_t},"
                                    + ("0" if i == 0 else
                                       (prev4('P&L', r_omr_t, i) if i == n_act
                                        else f"({c_(i - 1)}{r}-Inputs!{c_(i - 1)}{r_ww})"))
                                    + f"*(1+OM_growth)+Inputs!{c_(i)}{r_ww})"),
                   bold=True,
                   comment="Forecast: the first forecast quarter starts from the trailing four reported quarters; "
                           "later quarters grow the previous quarter's base (ex the Wind World step) by OM_growth. "
                           "The Wind World step (Inputs) is added on top.")
    r_ome = PL.row("O&M EBITDA", "Rs cr", lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_ome_t},{c_(i)}{r_omr}*OM_margin)")
    PL.row("O&M EBITDA margin", "%", lambda i, q, r: f"=IF({c_(i)}{r_omr}=0,0,{c_(i)}{r_ome}/{c_(i)}{r_omr})", fmt="pct")

    PL.section("WTG & EPC segment: reported and drivers")
    r_wtg_a = PL.row("WTG & EPC revenue, reported (consolidated less O&M)", "Rs cr",
                     lambda i, q, r: f'=IF({A(i)},{c_(i)}{r_rev_t}-{c_(i)}{r_omr_t},"")', kind="local")
    r_mat_a = PL.row("Material cost, reported (all in WTG & EPC)", "Rs cr",
                     lambda i, q, r: f'=IF({A(i)},{c_(i)}{r_mat_t},"")', kind="local")
    r_otw_a = PL.row("EPC, site and other, WTG & EPC: reported other expenses less O&M's costs", "Rs cr",
                     lambda i, q, r: f'=IF({A(i)},{c_(i)}{r_oth_t}-({c_(i)}{r_omr_t}-{c_(i)}{r_ome_t}),"")',
                     kind="local",
                     comment="All of the O&M subsidiary's costs are taken out of 'other expenses'; its small "
                             "employee line is therefore also inside the WTG employee line. Stated, not split.")
    stated = f"SUMIFS({rng('P&L', r_wtg_a)},{rng('P&L', r_mw_t)},\">0\")/SUMIFS({rng('P&L', r_mw_t)},{rng('P&L', r_mw_t)},\">0\")"
    r_rpm = PL.row("Revenue per MW (Rs cr / MW)", "Rs cr/MW",
                   lambda i, q, r: (f'=IF({A(i)},IF(ISNUMBER({c_(i)}{r_mw_t}),{c_(i)}{r_wtg_a}/{c_(i)}{r_mw_t},""),'
                                    + (stated if i == n_act else f"{c_(i - 1)}{r}") + "*(1+RPM_drift))"),
                   fmt="rs", fill=lambda i, q: q not in act,
                   comment="Reported: WTG & EPC revenue / MW, only where MW was stated. Forecast: every stated "
                           "quarter's revenue over its MW (MW-weighted), then x (1 + RPM_drift) a quarter. It carries "
                           "the turnkey / equipment-only mix: equipment supply realises less per MW.")
    r_matr = PL.row("Material cost / WTG & EPC revenue", "%",
                    lambda i, q, r: (f"=IF({A(i)},{c_(i)}{r_mat_a}/{c_(i)}{r_wtg_a},"
                                     + ("0" if i == 0 else (wsum4('P&L', r_mat_a, r_wtg_a, i) if i == n_act
                                                            else f"{c_(i - 1)}{r}") + "+Mat_drift") + ")"),
                    fmt="pct", fill=lambda i, q: q not in act,
                    comment="The swing line. Forecast: trailing four reported quarters, revenue-weighted, plus "
                            "Mat_drift (pp a quarter).")
    r_othr = PL.row("EPC, site and other / WTG & EPC revenue", "%",
                    lambda i, q, r: (f"=IF({A(i)},{c_(i)}{r_otw_a}/{c_(i)}{r_wtg_a},"
                                     + ("0" if i == 0 else (wsum4('P&L', r_otw_a, r_wtg_a, i) if i == n_act
                                                            else f"{c_(i - 1)}{r}") + "+Oth_drift") + ")"),
                    fmt="pct", fill=lambda i, q: q not in act)

    PL.section("WTG & EPC segment")

    def wtg(i, q, r):
        plan_cell = f"IFERROR(INDEX(WtgPlan,MATCH({fyc(i)},WtgYears,0)),0)"
        done = f'SUMIFS({rng("P&L", r_wtg_a)},FYrow,{fyc(i)},Status,"A")'
        wsum = f'SUMIFS({rng("Inputs", r_wrv)},FYrow,{fyc(i)},Status,"F")'
        share = f"IF({wsum}=0,0,MAX(0,{plan_cell}-{done})*Inputs!{c_(i)}{r_wrv}/{wsum})"
        return (f"=IF({A(i)},{c_(i)}{r_wtg_a},IF(ISNUMBER({c_(i)}{r_rev_ov}),{c_(i)}{r_rev_ov}-{c_(i)}{r_omr},"
                f"IF(ISNUMBER({c_(i)}{r_mw_ov}),{c_(i)}{r_mw_ov}*{c_(i)}{r_rpm},{share})))")

    r_wtg = PL.row("WTG & EPC revenue", "Rs cr", wtg, bold=True,
                   comment="Forecast = (the year's plan on Inputs - the quarters already reported) x this quarter's "
                           "weight / the weights of the year's forecast quarters. A revenue override makes it the "
                           "override less O&M; an MW override makes it MW x revenue per MW.")
    r_mat = PL.row("Material cost", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_mat_t},{c_(i)}{r_wtg}*{c_(i)}{r_matr})")
    r_otw = PL.row("EPC, site and other expenses", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_otw_a},{c_(i)}{r_wtg}*{c_(i)}{r_othr})")
    r_emp = PL.row("Employee cost (fixed)", "Rs cr",
                   lambda i, q, r: (f"=IF({A(i)},{c_(i)}{r_emp_t},"
                                    + ("0" if i == 0 else (prev4('P&L', r_emp_t, i) if i == n_act else f"{c_(i - 1)}{r}")
                                                           + "*(1+Emp_growth)") + ")"))
    r_wtge = PL.row("WTG & EPC EBITDA", "Rs cr",
                    lambda i, q, r: f"={c_(i)}{r_wtg}-{c_(i)}{r_mat}-{c_(i)}{r_otw}-{c_(i)}{r_emp}", bold=True)
    PL.row("WTG & EPC EBITDA margin", "%", lambda i, q, r: f"=IF({c_(i)}{r_wtg}=0,0,{c_(i)}{r_wtge}/{c_(i)}{r_wtg})",
           fmt="pct")

    PL.section("Consolidated")
    r_rev = PL.row("Revenue from operations", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_rev_t},{c_(i)}{r_wtg}+{c_(i)}{r_omr})", bold=True)
    PL.row("Revenue growth, year on year", "%", lambda i, q, r: None if i < 4 else f"={c_(i)}{r_rev}/{c_(i - 4)}{r_rev}-1",
           fmt="pct")
    r_ebitda = PL.row("EBITDA (before other income)", "Rs cr",
                      lambda i, q, r: (f"=IF({A(i)},{c_(i)}{r_rev_t}-{c_(i)}{r_mat_t}-{c_(i)}{r_emp_t}-{c_(i)}{r_oth_t},"
                                       f"IF(ISNUMBER({c_(i)}{r_mgn_ov}),{c_(i)}{r_rev}*{c_(i)}{r_mgn_ov},"
                                       f"{c_(i)}{r_wtge}+{c_(i)}{r_ome}))"), bold=True,
                      comment="Reported quarters: revenue less the three reported cost lines. Forecast: WTG & EPC "
                              "EBITDA + O&M EBITDA, unless the PM's margin override is typed (then revenue x margin; "
                              "the segment rows stop adding up to it, by design, and the check row shows by how much).")
    PL.row("EBITDA margin", "%", lambda i, q, r: f"=IF({c_(i)}{r_rev}=0,0,{c_(i)}{r_ebitda}/{c_(i)}{r_rev})",
           fmt="pct", bold=True)
    PL.row("Check: segments less consolidated (0 unless a margin override is typed)", "Rs cr",
           lambda i, q, r: f"={c_(i)}{r_wtge}+{c_(i)}{r_ome}-{c_(i)}{r_ebitda}")
    r_oi = PL.row("Other income", "Rs cr", lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_oi_t},OI_q)")
    r_xo = PL.row("Exceptional items (pre-tax)", "Rs cr", lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_xo_t},0)")
    r_dep = PL.row("Depreciation", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_dep_t}," + ("0" if i == 0 else f"{c_(i - 1)}{r}") + "*(1+Dep_growth))")
    r_int = PL.row("Finance cost", "Rs cr", lambda i, q, r: None)        # filled once Cash flow exists
    r_pbt = PL.row("PBT", "Rs cr",
                   lambda i, q, r: f"={c_(i)}{r_ebitda}+{c_(i)}{r_oi}+{c_(i)}{r_xo}-{c_(i)}{r_dep}-{c_(i)}{r_int}",
                   bold=True)
    r_tax = PL.row("Tax", "Rs cr", lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_tax_t},MAX(0,{c_(i)}{r_pbt})*Tax_rate)")
    r_mi = PL.row("Minority interest", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_mi_t}," + ("0" if i == 0 else prev4('P&L', r, i)) + ")",
                  comment="Forecast: the trailing four quarters' average. Mostly the outside holders' share of the "
                          "O&M subsidiary; it does not grow with the Wind World step here.")
    r_pat = PL.row("PAT attributable to owners", "Rs cr",
                   lambda i, q, r: f"={c_(i)}{r_pbt}-{c_(i)}{r_tax}-{c_(i)}{r_mi}", bold=True)
    PL.row("Check: model PAT less reported (actual quarters; screener rounds to the crore)", "Rs cr",
           lambda i, q, r: f'=IF({A(i)},{c_(i)}{r_pat}-{c_(i)}{r_pat_t},"")')
    r_eps = PL.row("EPS (Rs)", "Rs", lambda i, q, r: f"={c_(i)}{r_pat}*10/Shares_mn", fmt="rs", bold=True)

    # ---------------------------------------------------------------- Operations
    OPN = Sheet(wb, "Operations", qs, f"{cfg['company']} - MW and order book",
                "MW is IMPLIED on forecast quarters (WTG revenue / revenue per MW, P&L), stated where the company or a "
                "house gave it, blank where nobody did. The order book rolls forward from the last reported book.")
    OPN.header()
    OPN.section("Reported (typed)")
    r_in_t = OPN.row("Order inflow, stated", "MW", typed(ops, "inflow_mw"), fmt="cr")
    r_ob_t = OPN.row("Order book, closing, stated", "MW", typed(ops, "order_book_mw"), fmt="cr")
    OPN.section("Model")
    r_mw = OPN.row("MW executed (stated / implied)", "MW",
                   lambda i, q, r: (f"=IF({A(i)},IF(ISNUMBER('P&L'!{c_(i)}{r_mw_t}),'P&L'!{c_(i)}{r_mw_t},\"\"),"
                                    f"IF(ISNUMBER('P&L'!{c_(i)}{r_mw_ov}),'P&L'!{c_(i)}{r_mw_ov},"
                                    f"'P&L'!{c_(i)}{r_wtg}/'P&L'!{c_(i)}{r_rpm}))"), bold=True,
                   comment="Reported quarters: the stated MW, else blank (never back-solved). Forecast: the MW "
                           "override, else WTG & EPC revenue / revenue per MW.")
    r_in = OPN.row("Order inflow", "MW",
                   lambda i, q, r: (f"=IF({A(i)},IF(ISNUMBER({c_(i)}{r_in_t}),{c_(i)}{r_in_t},\"\"),"
                                    f"IFERROR(INDEX(InPlan,MATCH({fyc(i)},InYears,0)),0)*Inputs!{c_(i)}{r_win})"),
                   comment="Forecast: the fiscal year's inflow run-rate x this quarter's weight.")
    r_ob_open = OPN.row("Order book, opening", "MW",
                        lambda i, q, r: None if i < n_act else f"={c_(i - 1)}{r + 1}")
    r_ob = OPN.row("Order book, closing", "MW",
                   lambda i, q, r: (f"=IF({A(i)},IF(ISNUMBER({c_(i)}{r_ob_t}),{c_(i)}{r_ob_t},\"\"),"
                                    f"{c_(i)}{r_ob_open}+{c_(i)}{r_in}-{c_(i)}{r_mw})"), bold=True)
    OPN.row("Order book / next four quarters' MW (years of cover)", "x",
            lambda i, q, r: None if i + 4 > N or i < n_act - 1
            else f"=IFERROR({c_(i)}{r_ob}/SUM({c_(i + 1)}{r_mw}:{c_(i + 4)}{r_mw}),\"\")", fmt="x")
    OPN.row("Check: executes more than the book it opened with plus inflow", "",
            lambda i, q, r: None if i < n_act
            else f'=IF({c_(i)}{r_mw}>{c_(i)}{r_ob_open}+{c_(i)}{r_in},"OVER","")')

    # ---------------------------------------------------------------- Cash flow
    CF = Sheet(wb, "Cash flow", qs, f"{cfg['company']} - working capital, net debt",
               "Net debt rolls on EBITDA + other income - interest - tax - change in working capital - capex + equity "
               "raised, and resets to borrowings less cash wherever a balance sheet was published. Rs crore.")
    CF.header()
    CF.section("Reported (typed; blank where no balance sheet was published)")
    r_debt_t = CF.row("Gross debt, reported (closing)", "Rs cr", typed(bs, "gross_debt"))
    r_cash_t = CF.row("Cash and equivalents, reported (closing)", "Rs cr", typed(bs, "cash"))
    r_nwc_t = CF.row("Net working capital and other, reported (closing)", "Rs cr", typed(bs, "nwc"))
    CF.section("Working capital")
    r_ttm = CF.row("Revenue, trailing four quarters (annualised where fewer exist)", "Rs cr",
                   lambda i, q, r: f"=SUM('P&L'!{c_(max(0, i - 3))}{r_rev}:{c_(i)}{r_rev})*4/{min(i + 1, 4)}")
    r_days = CF.row("Net working capital, days of that revenue", "days",
                    lambda i, q, r: (f"=IF(ISNUMBER({c_(i)}{r_nwc_t}),{c_(i)}{r_nwc_t}*365/{c_(i)}{r_ttm},"
                                     + (f"Open_NWC*365/{c_(i)}{r_ttm}" if i == 0
                                        else f"IF({A(i)},{c_(i - 1)}{r},WC_days)") + ")"),
                    fmt="m", fill=lambda i, q: q not in act,
                    comment="Reported at balance-sheet dates; a reported quarter without a balance sheet holds the "
                            "previous days; forecasts take WC_days (Inputs).")
    r_nwc = CF.row("Net working capital, closing", "Rs cr",
                   lambda i, q, r: f"=IF(ISNUMBER({c_(i)}{r_nwc_t}),{c_(i)}{r_nwc_t},{c_(i)}{r_days}*{c_(i)}{r_ttm}/365)")
    r_dnwc = CF.row("Increase in working capital", "Rs cr",
                    lambda i, q, r: f"={c_(i)}{r_nwc}-" + ("Open_NWC" if i == 0 else f"{c_(i - 1)}{r_nwc}"))
    CF.section("Cash flow")

    def plan_q(i, years, vals):
        return f"=IFERROR(INDEX({vals},MATCH({fyc(i)},{years},0)),0)*Inputs!{c_(i)}{r_wcx}"

    r_capex = CF.row("Capex", "Rs cr", lambda i, q, r: plan_q(i, "CapexYears", "CapexPlan"),
                     comment="The fiscal-year plan on Inputs x this quarter's weight, on every quarter, reported or not: "
                             "the company does not report quarterly capex. For a past year the plan line is the "
                             "balance-sheet figure (fixed assets + CWIP + depreciation).")
    r_eq = CF.row("Equity raised", "Rs cr", lambda i, q, r: plan_q(i, "EqYears", "EqPlan"))
    r_ocf = CF.row("EBITDA + other income - interest - tax", "Rs cr",
                   lambda i, q, r: (f"='P&L'!{c_(i)}{r_ebitda}+'P&L'!{c_(i)}{r_oi}+'P&L'!{c_(i)}{r_xo}"
                                    f"-'P&L'!{c_(i)}{r_int}-'P&L'!{c_(i)}{r_tax}"))
    r_fcf = CF.row("Free cash flow (after working capital and capex)", "Rs cr",
                   lambda i, q, r: f"={c_(i)}{r_ocf}-{c_(i)}{r_dnwc}-{c_(i)}{r_capex}", bold=True)
    roll = lambda i: (("Open_debt-Open_cash" if i == 0 else f"{c_(i - 1)}{{nd}}") + f"-{c_(i)}{r_fcf}-{c_(i)}{r_eq}")
    has_bs = lambda i: f"AND(ISNUMBER({c_(i)}{r_debt_t}),ISNUMBER({c_(i)}{r_cash_t}))"
    r_nd = CF.row("Net debt, closing", "Rs cr",
                  lambda i, q, r: (f"=IF({has_bs(i)},{c_(i)}{r_debt_t}-{c_(i)}{r_cash_t},"
                                   + roll(i).format(nd=r) + ")"), bold=True,
                  comment="Typed where a balance sheet exists (borrowings less cash); rolled on free cash flow and "
                          "equity raised between them.")
    CF.row("Check: rolled net debt less reported, at balance-sheet dates", "Rs cr",
           lambda i, q, r: f'=IF({has_bs(i)},' + roll(i).format(nd=r_nd) + f'-{c_(i)}{r_nd},"")',
           comment="What the balance sheet absorbed beyond this roll (investments, acquisitions, leases, the wider "
                   "working-capital measure, crore rounding).")
    r_cash = CF.row("Cash, closing", "Rs cr",
                    lambda i, q, r: (f"=IF(ISNUMBER({c_(i)}{r_cash_t}),{c_(i)}{r_cash_t},"
                                     + ("Open_cash" if i == 0 else f"IF({A(i)},{c_(i - 1)}{r},Cash_hold)") + ")"))
    r_debt = CF.row("Gross debt, closing", "Rs cr", lambda i, q, r: f"={c_(i)}{r_nd}+{c_(i)}{r_cash}", bold=True)
    CF.row("Net debt / EBITDA (trailing four quarters)", "x",
           lambda i, q, r: None if i < 3 else f"={c_(i)}{r_nd}/SUM('P&L'!{c_(i - 3)}{r_ebitda}:{c_(i)}{r_ebitda})",
           fmt="x2")
    for i, q in enumerate(qs):
        c = PL.ws.cell(r_int, FIRST_COL + i,
                       f"=IF({A(i)},{c_(i)}{r_int_t}," + ("Open_debt" if i == 0 else f"'Cash flow'!{c_(i - 1)}{r_debt}")
                       + "*Int_rate/4)")
        c.font = Font(name=FONT, color=GREEN)
        c.number_format = FMT["cr"]

    # ---------------------------------------------------------------- Annual
    an = wb.create_sheet("Annual")
    an["A1"] = f"{cfg['company']} - fiscal-year summary"
    an["A1"].font = Font(name=FONT, bold=True, size=13)
    an["A2"] = ("Sums of the quarters; a year with any forecast quarter is marked F. MW for a reported year shows only "
                "when all four quarters were stated.")
    an["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    an.column_dimensions["A"].width = 46
    wb.move_sheet("Operations", offset=-(wb.index(wb["Operations"]) - wb.index(wb["P&L"])))
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
        ("MW executed (stated / implied)",
         lambda L: f'=IF(COUNTIFS(FYrow,{L}$4,{rng("Operations", r_mw)},">0")=4,SUMIFS({rng("Operations", r_mw)},FYrow,{L}$4),"n/s")',
         "cr"),
        ("Order book, year end, MW", lambda L: f'=IFERROR({yend("Operations", r_ob, L + "$4")},"")', "cr"),
        ("WTG & EPC revenue (Rs cr)", sm("P&L", r_wtg), "cr"),
        ("O&M revenue (Rs cr)", sm("P&L", r_omr), "cr"),
        ("Revenue (Rs cr)", sm("P&L", r_rev), "cr"),
        ("EBITDA, before other income (Rs cr)", sm("P&L", r_ebitda), "cr"),
        ("EBITDA margin", lambda L: f"=SUMIFS({rng('P&L', r_ebitda)},FYrow,{L}$4)/SUMIFS({rng('P&L', r_rev)},FYrow,{L}$4)",
         "pct"),
        ("  of which O&M EBITDA (Rs cr)", sm("P&L", r_ome), "cr"),
        ("PAT attributable to owners (Rs cr)", sm("P&L", r_pat), "cr"),
        ("EPS (Rs)", sm("P&L", r_eps), "rs"),
        ("Free cash flow (Rs cr)", sm("Cash flow", r_fcf), "cr"),
        ("Net working capital, year end (Rs cr)", lambda L: f"={yend('Cash flow', r_nwc, L + '$4')}", "cr"),
        ("Net debt, year end (Rs cr)", lambda L: f"={yend('Cash flow', r_nd, L + '$4')}", "cr"),
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
    for L, w in (("A", 62), ("B", 14), ("C", 14), ("D", 16), ("E", 16), ("F", 14), ("G", 70)):
        va.column_dimensions[L].width = w
    va["A1"] = f"{cfg['company']} - EV/EBITDA to an expected price"
    va["A1"].font = Font(name=FONT, bold=True, size=13)
    va["A2"] = ("For the PM's eye. The system's juice comes from case_brief.py (median house-implied EV/EBITDA, sigma "
                "range, today's net debt); 'Expected price on the case's bridge' is the comparable number.")
    va["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    street = cfg.get("street") or {}
    vfy = cfg.get("valuation_fy", "FY28")
    ndq = cfg.get("nd_quarter", "Q4FY27")
    vrows = [
        ("Valuation basis", "EV/EBITDA", None, "inp", "Read by the Analysis card (model_view)."),                       # 4
        ("Valuation year", vfy, None, "inp", "Sum of that year's four quarters of EBITDA (before other income)."),      # 5
        ("Quarters in the valuation year", "=COUNTIFS(FYrow,B5)", None, "f", "Must read 4."),                           # 6
        ("EBITDA, valuation year (Rs cr)", f"=SUMIFS({rng('P&L', r_ebitda)},FYrow,B5)", "cr", "f", ""),                # 7
        ("Net debt as of (quarter)", ndq, None, "inp",
         "A model quarter-end, one year before the valuation year ends by default: that year's cash is in "
         "EBITDA, the earlier quarters' cash in net debt."),                                                            # 8
        ("Net debt at that quarter (Rs cr)", f"=INDEX({rng('Cash flow', r_nd)},MATCH(B8,Qrow,0))", "cr", "f", ""),     # 9
        ("Market cap today (Rs cr)", "=CMP*Shares_mn/10", "cr", "f", "CMP x shares (Inputs)."),                         # 10
        ("EV today: market cap + the case's net debt (Rs cr)", "=B10+Case_nd", "cr", "f", "Inputs!Case_nd."),          # 11
        ("EV/EBITDA today, on the valuation year", "=B11/B7", "x2", "f", ""),                                           # 12
        ("Target EV/EBITDA multiple", "=Target_ev", "x2", "key",
         "Inputs!Target_ev: the median of the houses' implied EV/EBITDA ((TP x shares + net debt) / street EBITDA), "
         "which case_brief.py recomputes from build.json house_tps. Retype after a re-strike."),                         # 13
        ("Enterprise value at the target (Rs cr)", "=B7*B13", "cr", "f", ""),                                          # 14
        ("Equity value (Rs cr)", "=B14-B9", "cr", "f", "Minority interest not deducted (as in the case)."),            # 15
        ("Expected price (Rs)", "=B15*10/Shares_mn", "rs", "f", ""),                                                    # 16
        ("Upside vs CMP", "=B16/CMP-1", "pct", "f", ""),                                                                # 17
        ("Expected price on the case's bridge, today's net debt (Rs)", "=(B14-Case_nd)*10/Shares_mn", "rs", "f",
         "The same EBITDA and multiple through case_brief's bridge (Inputs!Case_nd), so the two are comparable."),      # 18
        ("Upside vs CMP, case bridge", "=B18/CMP-1", "pct", "f", ""),                                                   # 19
    ]
    for k, (lab, v, fmt, kind, note) in enumerate(vrows):
        r = 4 + k
        bold = lab in ("Expected price (Rs)", "Upside vs CMP")
        va.cell(r, 1, lab).font = Font(name=FONT, bold=bold)
        c = va.cell(r, 2, v)
        is_f = isinstance(v, str) and v.startswith("=")
        linked = is_f and any(t in v for t in ("!", "FYrow", "Qrow", "Shares_mn", "CMP", "Target_", "Case_"))
        c.font = Font(name=FONT, color=(GREEN if linked else BLACK) if is_f else BLUE, bold=bold)
        if fmt:
            c.number_format = FMT[fmt]
        if kind in ("inp", "key"):
            c.fill = YELLOW
        if note:
            va.cell(r, 7, note).font = Font(name=FONT, size=8, italic=True, color="595959")
    sr = 4 + len(vrows) + 1
    va.cell(sr, 1, "Sensitivity: expected price (Rs) by multiple and by the valuation year's EBITDA").font = \
        Font(name=FONT, bold=True)
    va.cell(sr + 1, 1, "EBITDA shift  \\  multiple shift").font = Font(name=FONT, size=9)
    for j, dm in enumerate((-2, -1, 0, 1, 2)):
        c = va.cell(sr + 1, 2 + j, dm)
        c.font = Font(name=FONT, color=BLUE)
        c.number_format = '+0.0"x";-0.0"x";"base"'
    for k, de in enumerate((-0.20, -0.10, 0, 0.10, 0.20)):
        r = sr + 2 + k
        c = va.cell(r, 1, de)
        c.font = Font(name=FONT, color=BLUE)
        c.number_format = '+0%;-0%;"base"'
        for j in range(5):
            L = get_column_letter(2 + j)
            cc = va.cell(r, 2 + j, f"=(($B$13+{L}${sr + 1})*$B$7*(1+$A{r})-$B$9)*10/Shares_mn")
            cc.font = Font(name=FONT, color=GREEN)
            cc.number_format = FMT["rs"]
    stb = sr + 8
    for j, h in enumerate(("Street check: street EBITDA and revenue (type in) against the model", "Street EBITDA",
                           "Model EBITDA", "Model vs street", "Street revenue", "Model revenue")):
        va.cell(stb, 1 + j, h).font = Font(name=FONT, bold=True)
    for k, fy in enumerate(("FY27", "FY28", "FY29")):
        r = stb + 1 + k
        va.cell(r, 1, fy).font = Font(name=FONT)
        s = (street.get(fy) or {})
        for cc_, key in ((2, "ebitda_cr"), (5, "revenue_cr")):
            c = va.cell(r, cc_, s.get(key))
            c.fill = YELLOW
            c.font = Font(name=FONT, color=BLUE)
            c.number_format = FMT["cr"]
        c = va.cell(r, 3, f"=SUMIFS({rng('P&L', r_ebitda)},FYrow,\"{fy}\")")
        c.font = Font(name=FONT, color=GREEN)
        c.number_format = FMT["cr"]
        c = va.cell(r, 4, f'=IF(ISNUMBER(B{r}),C{r}/B{r}-1,"")')
        c.number_format = FMT["pct"]
        c = va.cell(r, 6, f"=SUMIFS({rng('P&L', r_rev)},FYrow,\"{fy}\")")
        c.font = Font(name=FONT, color=GREEN)
        c.number_format = FMT["cr"]
        if s.get("source"):
            va.cell(r, 7, s["source"]).font = Font(name=FONT, size=8, italic=True, color="595959")
    hb = stb + 5
    houses = cfg.get("house_tps") or []
    if houses:
        for j, h in enumerate(("Houses' targets and the multiple each implies", "TP (Rs)", "Date",
                               "On street EBITDA", "On model EBITDA")):
            va.cell(hb, 1 + j, h).font = Font(name=FONT, bold=True)
        va.cell(hb, 7, "Implied = (TP x shares + the case's net debt) / the valuation year's EBITDA.").font = \
            Font(name=FONT, size=8, italic=True, color="595959")
        srow = stb + 1 + ("FY27", "FY28", "FY29").index(vfy) if vfy in ("FY27", "FY28", "FY29") else None
        for k, h in enumerate(houses):
            r = hb + 1 + k
            va.cell(r, 1, h["house"]).font = Font(name=FONT)
            c = va.cell(r, 2, h["tp"])
            c.font = Font(name=FONT, color=BLUE)
            c.number_format = FMT["rs"]
            va.cell(r, 3, h.get("date", "")).font = Font(name=FONT, color=BLUE)
            if srow:
                c = va.cell(r, 4, f'=IF(ISNUMBER($B${srow}),(B{r}*Shares_mn/10+Case_nd)/$B${srow},"")')
                c.number_format = FMT["x2"]
                c.font = Font(name=FONT, color=GREEN)
            c = va.cell(r, 5, f"=(B{r}*Shares_mn/10+Case_nd)/$B$7")
            c.number_format = FMT["x2"]
            c.font = Font(name=FONT, color=GREEN)

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

    wb.move_sheet("README", offset=-wb.index(wb["README"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return {"quarters": N, "actual": n_act,
            "rows": {"rev": r_rev, "ebitda": r_ebitda, "wtg": r_wtg, "omr": r_omr, "ome": r_ome, "wtge": r_wtge,
                     "rpm": r_rpm, "mw": r_mw, "ob": r_ob, "nd": r_nd, "mgn_ov": r_mgn_ov, "rev_ov": r_rev_ov,
                     "mw_ov": r_mw_ov, "pat": r_pat, "fcf": r_fcf, "dnwc": r_dnwc}}


def readme_lines(cfg, qs, n_act):
    return [
        (f"{cfg['company']} - quarterly model", "t"),
        (f"Built {cfg['built']} by packages/book/wind_model.py from {cfg['entity']}/model_config.json. "
         f"Actuals {qs[0]} to {qs[n_act - 1]}; forecasts to {qs[-1]}. Consolidated, Rs crore and MW.", ""),
        ("", ""),
        ("How it flows", "h"),
        ("Inputs (timeline, constants, the fiscal-year plans for WTG & EPC revenue, order inflow, capex and equity, "
         "quarter shapes, the Wind World step) -> P&L (O&M segment: the listed subsidiary's revenue and EBITDA; WTG & "
         "EPC segment: the revenue plan, material and EPC/other costs as ratios, employee cost fixed; consolidated "
         "EBITDA before other income; below EBITDA) -> Operations (MW implied by revenue per MW, order book roll) -> "
         "Cash flow (working-capital days, free cash flow, net debt) -> Annual -> Valuation (EV/EBITDA).", ""),
        ("", ""),
        ("Why revenue and not MW drives it", "h"),
        ("The company stopped stating quarterly MW after Q2FY26 and guides revenue instead. MW is implied from revenue "
         "per MW measured on the quarters where MW was stated; type MW in the override row on P&L to drive a quarter "
         "from MW instead.", ""),
        ("", ""),
        ("Colours", "h"),
        ("Blue = typed. Black = formula. Green = link to another sheet. Yellow = a key assumption or the PM's override. "
         "Status row: A = reported quarter, F = forecast.", ""),
        ("", ""),
        ("Rolling a new quarter", "h"),
        ("Type the quarter's reported lines into the blue 'Reported (typed)' blocks on P&L, Operations and Cash flow "
         "(the O&M subsidiary's revenue and operating profit, and MW if stated), then flip its status on Inputs row 6 "
         "from F to A. Every model row switches to the reported value and the next quarter's ratios re-base on the "
         "trailing four. Debt, cash and working capital are typed only at balance-sheet dates; blank is fine. The last "
         "reported quarter needs its order book (the roll starts there).", "yellow"),
        ("", ""),
        ("What the PM overrides", "h"),
        ("For the visible quarter: consolidated revenue and EBITDA margin, or MW executed (yellow rows on P&L). For the "
         "years: WTG & EPC revenue and order inflow per fiscal year, capex, equity raised (Inputs). The drifts (revenue "
         "per MW, material ratio, other-cost ratio) say how fast the unit economics move; all are zero until there is "
         "a view, so the forecast ratios are the trailing four quarters. WC_days is the cash lever.", ""),
        ("", ""),
        ("What the model leaves out, on purpose", "h"),
        ("The turnkey / equipment-only mix (revenue per MW carries it), milestone revenue recognition, inter-company "
         "sales between the O&M subsidiary and the parent, the O&M subsidiary's own employee line (inside the WTG "
         "employee line), commodity prices behind the material ratio, acquisitions by the O&M arm (a second ~2GW "
         "portfolio is due by March 2027), and minority interest in the valuation. Actuals are crore-rounded "
         "(screener), so a reported quarter foots to within Rs3cr.", ""),
        ("", ""),
        ("Where the actuals come from", "h"),
        (cfg.get("readme_history") or "The Sources sheet names the document for every line; the Inputs basis row says "
                                      "what each quarter is.", ""),
    ]


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def _fixture() -> dict:
    qs = ["Q1FY27", "Q2FY27"]
    lines = dict(revenue=(814.0, 1354.0), material=(385.0, 836.0), employee=(50.0, 58.0), other_exp=(227.0, 255.0),
                 other_income=(58.0, 23.0), exceptional=(0.0, 0.0), interest=(57.0, 63.0), depreciation=(59.0, 63.0),
                 pbt=(95.0, 103.0), tax=(31.0, 34.0), minority=(20.0, 10.0), pat=(44.0, 59.0))
    e = lambda v, u="", s="t": {"value": v, "unit": u, "source": s}
    return {"company": "Fixture Wind", "entity": "fixture_wind", "built": "2026-11-25",
            "first_quarter": "Q1FY27", "last_quarter": "Q4FY28", "actual_through": "Q2FY27", "valuation_fy": "FY28",
            "nd_quarter": "Q4FY27", "basis": {q: "t" for q in qs},
            "constants": {"shares_mn": e(1728.0), "cmp": e(55.46), "cmp_date": e("2026-11-20"), "tax_rate": e(0.3),
                          "int_rate": e(0.12), "rpm_drift": e(0.0), "mat_drift": e(0.0), "oth_drift": e(0.0),
                          "emp_growth": e(0.02), "om_growth": e(0.0), "om_margin": e(0.24), "oi_q": e(23.0),
                          "dep_growth": e(0.02), "wc_days": e(300.0), "cash_hold": e(900.0), "target_ev": e(11.3),
                          "case_nd": e(1575.0)},
            "opening": {"as_of": "31-Mar-2026", "gross_debt": e(1587.0), "cash": e(612.0), "nwc": e(3735.0)},
            "plans": {"wtg_revenue": {"FY27": e(4900.0), "FY28": e(6600.0)},
                      "inflow_mw": {"FY27": e(1500.0), "FY28": e(1500.0)},
                      "capex": {"FY27": e(500.0), "FY28": e(200.0)}, "equity": {"FY27": e(0.0)},
                      "weights_revenue": e([0.15, 0.25, 0.27, 0.33]), "weights_inflow": e([0.25, 0.25, 0.25, 0.25]),
                      "weights_capex": e([0.25, 0.25, 0.25, 0.25])},
            "wind_world": {"revenue": {"Q3FY27": 120.0, "Q4FY27": 120.0, "Q1FY28": 120.0, "Q2FY28": 120.0,
                                       "Q3FY28": 120.0, "Q4FY28": 120.0}, "source": "t"},
            "consolidated": {k: {q: v[j] for j, q in enumerate(qs)} for k, v in lines.items()},
            "reported_ebitda": {"Q1FY27": 152.0, "Q2FY27": 205.0},
            "om": {"om_revenue": {"Q1FY27": 43.0, "Q2FY27": 89.0}, "om_ebitda": {"Q1FY27": -1.0, "Q2FY27": 36.0}},
            "operations": {"mw_executed": {"Q2FY27": 256.0}, "inflow_mw": {"Q2FY27": 350.0},
                           "order_book_mw": {"Q2FY27": 3800.0}},
            "balance_sheet": {"gross_debt": {"Q2FY27": 2507.0}, "cash": {"Q2FY27": 932.0}, "nwc": {"Q2FY27": 4686.0}},
            "street": {"FY27": {"ebitda_cr": 965.9, "revenue_cr": 5485.0, "source": "t"},
                       "FY28": {"ebitda_cr": 1438.1, "revenue_cr": 7428.0, "source": "t"}},
            "house_tps": [{"house": "I-Sec", "tp": 85, "date": "2026-11-16"}],
            "sources": [["a", "b", "c"]]}


def _excel_ok() -> bool:
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Excel.Application"))
        return True
    except OSError:
        return False


def selftest() -> int:
    fails = 0

    def chk(label, got, want=True):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))

    def near(label, got, want, tol=1e-6):
        nonlocal fails
        ok = isinstance(got, (int, float)) and abs(got - want) <= tol
        fails += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r} +/- {tol})"))

    print("wind_model selftest")
    fx = _fixture()
    chk("footed fixture accepted", crossfoot(fx), [])
    bad = json.loads(json.dumps(fx)); bad["consolidated"]["tax"]["Q1FY27"] = 13.0           # transposed digit
    chk("transposed tax digit refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["consolidated"]["material"]["Q2FY27"] = 863.0
    chk("mistyped material refused (EBITDA and PBT no longer foot)", len(crossfoot(bad)), 2)
    bad = json.loads(json.dumps(fx)); bad["plans"]["weights_revenue"]["value"] = [0.15, 0.25, 0.27, 0.3]
    chk("weights that do not sum to one refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["balance_sheet"]["nwc"]["Q3FY27"] = 1.0
    chk("a balance-sheet figure typed on a forecast quarter refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); del bad["om"]["om_ebitda"]["Q2FY27"]
    chk("an O&M quarter missing refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["om"]["om_revenue"]["Q2FY27"] = 1400.0
    chk("O&M revenue larger than consolidated refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["operations"]["mw_executed"] = {}
    chk("no stated MW anywhere refused (revenue per MW has no base)", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["operations"]["order_book_mw"] = {"Q1FY27": 3000.0}
    chk("the last reported quarter without a book refused (the roll starts there)", len(crossfoot(bad)), 1)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="wind_selftest_"))
    p0 = tmp / "Fixture_model.xlsx"
    info = build(fx, p0)
    chk("built: quarters, actual", (info["quarters"], info["actual"]), (8, 2))
    wb = load_workbook(p0)
    chk("sheets", wb.sheetnames, ["README", "Inputs", "Operations", "P&L", "Cash flow", "Annual", "Valuation", "Sources"])
    chk("CMP and CMP_date are workbook names (model_view.set_cmp writes them)",
        all(n in wb.defined_names for n in ("CMP", "CMP_date", "Target_ev", "WtgPlan", "Case_nd")))
    import model_view
    vr = model_view._val_rows(wb["Valuation"])
    chk("model_view finds the target, basis, year, net-debt quarter and multiple rows by label",
        sorted(vr), ["basis", "multiple_typed", "nd_quarter", "tp", "year"])
    chk("the basis row says EV/EBITDA", wb["Valuation"].cell(vr["basis"], 2).value, "EV/EBITDA")
    chk("model_view reads column B (no side-by-side header)", model_view._val_col(wb["Valuation"], vr), 2)
    chk("a reported quarter with no stated MW has no MW formula result to back-solve (blank, not implied)",
        '""' in str(wb["Operations"].cell(info["rows"]["mw"], FIRST_COL).value), True)

    if not _excel_ok():
        print("  skip Excel recalculation: Excel is not on this machine")
        return 1 if fails else 0
    R = info["rows"]
    recalc(p0.resolve())
    chk("no error cells after recalculation", errors(p0), [])
    v = load_workbook(p0, data_only=True)
    pl, opn, cf, va = v["P&L"], v["Operations"], v["Cash flow"], v["Valuation"]
    cell = lambda ws, r, i: ws.cell(r, FIRST_COL + i).value
    near("a reported quarter's EBITDA is its reported lines (Q2FY27)", cell(pl, R["ebitda"], 1), 205.0)
    near("a reported quarter's PAT is the reported one within rounding", cell(pl, R["pat"], 1) - 59.0, 0.0, 3.0)
    # Excel saves an empty-string result, and openpyxl reads it back as None
    chk("a reported quarter with no stated MW shows blank MW (Q1FY27)", cell(opn, R["mw"], 0) in (None, ""), True)
    for i in (2, 5):
        near(f"segments = consolidated EBITDA with no override (quarter {i + 1})",
             cell(pl, R["wtge"], i) + cell(pl, R["ome"], i) - cell(pl, R["ebitda"], i), 0.0)
    wtg_h1 = (814 - 43) + (1354 - 89)
    near("WTG revenue plan net of reported, spread by weight (Q3FY27)", cell(pl, R["wtg"], 2),
         (4900 - wtg_h1) * 0.27 / 0.60)
    near("the fiscal year's WTG revenue sums to the plan", sum(cell(pl, R["wtg"], i) for i in range(4)), 4900.0)
    near("revenue per MW starts from the quarters with stated MW only", cell(pl, R["rpm"], 2), (1354 - 89) / 256)
    near("implied MW = WTG revenue / revenue per MW (Q3FY27)", cell(opn, R["mw"], 2),
         cell(pl, R["wtg"], 2) / cell(pl, R["rpm"], 2))
    near("order book rolls from the last reported book on inflow less implied MW (Q3FY27)", cell(opn, R["ob"], 2),
         3800 + 1500 * 0.25 - cell(opn, R["mw"], 2))
    near("net debt at the reported date is borrowings less cash", cell(cf, R["nd"], 1), 2507 - 932)
    near("net debt rolls on free cash flow (Q3FY27)", cell(cf, R["nd"], 2), cell(cf, R["nd"], 1) - cell(cf, R["fcf"], 2))
    e28 = sum(cell(pl, R["ebitda"], i) for i in range(4, 8))
    near("expected price = (multiple x FY28 EBITDA - net debt at Q4FY27) / shares",
         va.cell(vr["tp"], 2).value, (11.3 * e28 - cell(cf, R["nd"], 3)) * 10 / 1728.0)

    # the Wind World step adds O&M revenue x O&M margin to EBITDA and nothing to the WTG segment
    nw = json.loads(json.dumps(fx)); nw["wind_world"]["revenue"] = {}
    p1 = tmp / "NoWW_model.xlsx"
    build(nw, p1)
    recalc(p1.resolve())
    v1 = load_workbook(p1, data_only=True)["P&L"]
    near("Wind World step moves EBITDA by 120 x O&M margin (Q3FY27)",
         cell(pl, R["ebitda"], 2) - v1.cell(R["ebitda"], FIRST_COL + 2).value, 120 * 0.24)
    near("...and leaves WTG & EPC revenue alone", cell(pl, R["wtg"], 2) - v1.cell(R["wtg"], FIRST_COL + 2).value, 0.0)

    # the PM's overrides: revenue is the total, margin sets EBITDA, MW drives WTG revenue
    ov = load_workbook(p0)
    ov["P&L"].cell(R["rev_ov"], FIRST_COL + 2, 1500.0)
    ov["P&L"].cell(R["mgn_ov"], FIRST_COL + 2, 0.17)
    ov["P&L"].cell(R["mw_ov"], FIRST_COL + 3, 300.0)
    p2 = tmp / "Override_model.xlsx"
    ov.save(p2)
    recalc(p2.resolve())
    v2 = load_workbook(p2, data_only=True)["P&L"]
    near("revenue override is the consolidated revenue", v2.cell(R["rev"], FIRST_COL + 2).value, 1500.0)
    near("WTG revenue is the override less O&M", v2.cell(R["wtg"], FIRST_COL + 2).value,
         1500.0 - v2.cell(R["omr"], FIRST_COL + 2).value)
    near("margin override sets EBITDA = revenue x margin", v2.cell(R["ebitda"], FIRST_COL + 2).value, 255.0)
    near("an MW override drives WTG revenue = MW x revenue per MW (Q4FY27)", v2.cell(R["wtg"], FIRST_COL + 3).value,
         300.0 * v2.cell(R["rpm"], FIRST_COL + 3).value)
    print(f"wind_model selftest: {'FAILED ' + str(fails) if fails else 'all checks passed'}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--force", action="store_true", help="overwrite an existing workbook (loses its edits)")
    ap.add_argument("--recalc", action="store_true", help="open in Excel over COM, recalculate, save, check errors")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
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
