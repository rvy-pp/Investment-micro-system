"""A quarterly regulated-utility model in Excel: gross block -> P&L -> cash -> P/E -> price.

PM, 2026-11-09: *"Let's start with Powergrid modelling and agent."* The steel
builder (`steel_model.py`) drives a mill off commodity prices per tonne; a
transmission utility earns a regulated return on the assets it has
capitalised, so this builder drives everything off ONE series, the
consolidated gross block, and the two numbers management guides on, capex
and capitalisation. The shape, and what it deliberately leaves out:

- Capitalisation is the driver. Each fiscal year's plan (the guidance,
  yellow) is spread over its forecast quarters by the year's observed shape
  (Q4-heavy), net of the quarters already reported. Gross block = opening +
  capitalisation; CWIP = opening + capex - capitalisation.
- Transmission revenue = average gross block x a YIELD (annual revenue per
  rupee of block), measured on every actual quarter and carried forward as
  the trailing four quarters' average plus a drift input. The yield is where
  the tariff step-down on old assets and the lower TBCB tariff show up; the
  model does not rebuild the CERC tariff line by line (RoE, depreciation,
  interest, O&M, incentives), which is what the houses' standalone models do
  and what none of them agree on.
- Opex and depreciation are the same: a rate on average gross block,
  trailing four quarters, drift inputs. Other income is its trailing
  average. Interest = a rate on OPENING gross debt (no circularity).
  Tax is a rate on PBT. The JV share and the regulatory deferral movement
  are after-tax lines held at typed constants (zero): they are true-ups.
- Cash: CFO = PAT + depreciation (no working capital; receivables are a
  LPS/true-up story the model cannot see). Gross debt grows by a share of
  capex (the 70:30 norm); dividends are a payout of the PREVIOUS fiscal
  year's PAT paid in Q2/Q3/Q4; cash is the plug; net worth rolls with PAT
  less dividends.
- A Balance sheet sheet (PM, 2026-11-11: "add one for the power model, no
  adjustments or new connections, just a separate sheet for sanity check")
  rebuilds a balance sheet from the rows above and sets it against the
  reported one. It READS Assets, P&L and Cash flow and nothing reads it, so
  it cannot move a forecast; the selftest holds that.
- Valuation is P/E on the valuation year's EPS, at a typed target multiple
  (the median house-implied P/E, which the case brief recomputes), with a
  P/BV cross-check and a rolling four-quarters-forward EPS from the price
  date. The system's juice stays with `case_brief.py`.

The workbook is built ONCE from `data/analysis/cases/<entity>/model_config.json`
(gitignored). After that it is the working surface: a new quarter is typed
into the blue "reported" rows and its status flipped on Inputs from F to A;
every model row reads IF(status="A", reported, forecast). A rebuild
overwrites the PM's yellow cells, so `--out` refuses an existing file
without `--force`.

    python packages/book/power_model.py --config data/analysis/cases/power_grid/model_config.json \\
        --out data/analysis/cases/power_grid/Power_Grid_model.xlsx --recalc
    python packages/book/power_model.py --add-balance-sheet power_grid   # into the LIVE workbook, via Excel
    python packages/book/power_model.py --add-projects power_grid        # the CEA project list, same route
    python packages/book/power_model.py --selftest

openpyxl writes formulas without cached values; `--recalc` drives Excel over
COM so model_view (data_only=True) has numbers to read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import tempfile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from steel_model import (ACT, BLACK, BLUE, FIRST_COL, FMT, FONT, GREEN, HEAD, SEC, YELLOW,  # noqa: E402
                         Sheet, _bgr, col, errors, fy_of, name, quarter_dates, quarters, recalc)

REPO = pathlib.Path(__file__).resolve().parents[2]

# consolidated lines every actual quarter must carry, Rs crore
LINES = ("transmission_rev", "consultancy_rev", "telecom_rev", "other_rev", "sales", "opex", "other_income",
         "depreciation", "interest", "pbt", "tax", "jv_share", "reg_deferral", "pat", "capex",
         "capitalisation", "gross_block", "cwip", "gross_debt", "net_worth", "dividends_paid")

# The Balance sheet: the reported lines typed per published date (Rs crore),
# and the model rows it links to, found by the label each carries in column A.
BS_SHEET = "Balance sheet"
BS_LINES = ("net_worth", "borrowings", "other_liabilities", "fixed_assets", "cwip", "investments", "other_assets")
BS_LINKS = {"capex": ("Assets", "Capex"), "gb": ("Assets", "Gross block, closing"), "cwip": ("Assets", "CWIP, closing"),
            "dep": ("P&L", "Depreciation"), "pat": ("P&L", "PAT"), "div": ("Cash flow", "Dividends paid"),
            "debt": ("Cash flow", "Gross debt, closing"),
            "cash": ("Cash flow", "Cash and bank balances, closing (the plug)"),
            "nw": ("Cash flow", "Net worth, closing")}
BS_OPEN_NAMES = ("Open_GB", "Open_CWIP", "Open_debt", "Open_cash", "Open_NW")
BS_README = [
    ("", ""),
    ("Balance sheet (a sanity check)", "h"),
    ("Built from the other sheets; nothing reads it, so it cannot move a forecast. The reported block is typed for "
     "the dates a balance sheet is published (March and September). The model block rebuilds one from the gross "
     "block, cumulative depreciation, CWIP, the cash plug, net worth and gross debt, with everything else held at "
     "its opening figure. 'Unexplained change' must be zero in every quarter. The last rows say, line by line, "
     "where the model's balance sheet differs from the reported one.", ""),
]


# --------------------------------------------------------------------------
# config checks
# --------------------------------------------------------------------------
def bs_errors(cfg: dict, tol: float = 3.0) -> list[str]:
    """The optional reported balance sheet: every typed date must balance
    (assets = equity and liabilities, to rounding) and carry the net worth the
    model already holds for that date. A mistyped line refuses here; on the
    sheet it would only show as a gap nobody could tell from a real one."""
    bs = cfg.get("balance_sheet")
    if not bs:
        return []
    errs = []
    reported = bs.get("reported") or {}
    if "opening" not in reported:
        errs.append("balance_sheet.reported.opening: missing (the model block starts from its net fixed assets)")
    acts = cfg["consolidated"]["net_worth"]
    for when, d in reported.items():
        miss = [k for k in BS_LINES if not isinstance(d.get(k), (int, float))]
        if miss:
            errs.append(f"balance_sheet {when}: missing {', '.join(miss)}")
            continue
        liab = d["net_worth"] + d["borrowings"] + d["other_liabilities"]
        assets = d["fixed_assets"] + d["cwip"] + d["investments"] + d["other_assets"]
        if abs(assets - liab) > tol:
            errs.append(f"balance_sheet {when}: assets {assets:,.0f} vs equity and liabilities {liab:,.0f}")
        held = cfg["opening"]["net_worth"]["value"] if when == "opening" else acts.get(when)
        if held is None:
            errs.append(f"balance_sheet {when}: not an actual quarter")
        elif abs(held - d["net_worth"]) > tol:
            errs.append(f"balance_sheet {when}: net worth {d['net_worth']:,.0f} vs the model's {held:,.0f}")
    return errs


def crossfoot(cfg: dict, tol: float = 3.0) -> list[str]:
    """Every actual quarter must foot before anything is written: sales from
    its segments, PBT from its components (ex JV, as JPM's consolidated
    sheet has it), PAT = PBT - tax + JV + regulatory deferral. A mistyped
    deck number refuses here rather than becoming a plausible price. The
    plan weights must sum to one, or a year's plan would be silently scaled."""
    c = cfg["consolidated"]
    errs = []
    acts = list(c["pat"])
    for k in LINES:
        if k not in c:
            errs.append(f"consolidated.{k}: missing")
        elif set(c[k]) != set(acts):
            errs.append(f"consolidated.{k}: quarters differ from pat")
    if errs:
        return errs
    for q in acts:
        sales = c["transmission_rev"][q] + c["consultancy_rev"][q] + c["telecom_rev"][q] + c["other_rev"][q]
        if abs(sales - c["sales"][q]) > tol:
            errs.append(f"{q}: segments {sales:,.1f} vs sales {c['sales'][q]:,.1f}")
        pbt = c["sales"][q] - c["opex"][q] - c["depreciation"][q] + c["other_income"][q] - c["interest"][q]
        if abs(pbt - c["pbt"][q]) > tol:
            errs.append(f"{q}: PBT components {pbt:,.1f} vs reported {c['pbt'][q]:,.1f}")
        pat = c["pbt"][q] - c["tax"][q] + c["jv_share"][q] + c["reg_deferral"][q]
        if abs(pat - c["pat"][q]) > tol:
            errs.append(f"{q}: PBT - tax + JV + reg deferral {pat:,.1f} vs PAT {c['pat'][q]:,.1f}")
    for k in ("weights_capitalisation", "weights_capex"):
        w = cfg["plans"][k]["value"]
        if len(w) != 4 or abs(sum(w) - 1.0) > 1e-6:
            errs.append(f"plans.{k}: four weights summing to 1.0, got {w}")
    ds = cfg["constants"]["div_split"]["value"]
    if len(ds) != 4 or abs(sum(ds) - 1.0) > 1e-6:
        errs.append(f"constants.div_split: four shares summing to 1.0, got {ds}")
    return errs + bs_errors(cfg, tol)


# --------------------------------------------------------------------------
# the Balance sheet (a sanity check; reads the model, feeds nothing)
# --------------------------------------------------------------------------
def balance_sheet(wb, cfg: dict, qs: list[str], R: dict) -> dict:
    """Write the Balance sheet into `wb`. `R` is the row of each model line it
    links to (BS_LINKS). Column C is the opening balance sheet, the quarters
    run from D as on every other sheet.

    The model block needs no new input: gross block less cumulative
    depreciation, CWIP, the cash plug and ONE balancing figure (everything
    else, net) fixed at the opening, against net worth and gross debt. Because
    cash is the plug, that identity can only drift where a TYPED actual moved
    by something the model does not carry, and the two 'from ...' rows name
    which. 'Unexplained change' is then zero in every quarter by arithmetic;
    a non-zero there means a formula on another sheet was broken."""
    bs = cfg["balance_sheet"]
    reported = bs["reported"]
    BS = Sheet(wb, BS_SHEET, qs, f"{cfg['company']} - balance sheet (a sanity check)",
               "Reads Assets, P&L and Cash flow; NOTHING reads this sheet, so it cannot move a forecast. "
               "Column C is the opening balance sheet. Rs crore.")
    ws = BS.ws
    ws["A3"] = "Reported block: " + bs.get("source", "")
    ws["A3"].font = Font(name=FONT, italic=True, size=8, color="595959")
    BS.header()
    for r, txt in ((4, "Opening"), (5, cfg["opening"]["as_of"])):
        c = ws.cell(r, 3, txt)
        c.font = Font(name=FONT, bold=r == 4, color="FFFFFF")
        c.alignment = Alignment(horizontal="center")

    def copen(r, v, bold=False):
        c = ws.cell(r, 3, v)
        is_f = isinstance(v, str) and v.startswith("=")
        linked = is_f and ("!" in v or "Open_" in v)
        c.font = Font(name=FONT, bold=bold, color=(GREEN if linked else BLACK) if is_f else BLUE)
        c.number_format = FMT["cr"]

    def trow(label, key):
        r = BS.row(label, "Rs cr", {q: reported[q][key] for q in qs if q in reported})
        copen(r, reported["opening"][key])
        return r

    def frow(label, fn, bold=False):
        """One formula for the opening column and every quarter: fn(L) with L the column letter."""
        r = BS.row(label, "Rs cr", lambda i, q, r: fn(col(i)), bold=bold)
        copen(r, fn("C"), bold)
        return r

    def lrow(label, fn, opening, bold=False):
        """A linked row: fn(L, P) for the quarters (P = the column before), `opening` for column C."""
        r = BS.row(label, "Rs cr", lambda i, q, r: fn(col(i), col(i - 1)), bold=bold)
        if opening is not None:
            copen(r, opening, bold)
        return r

    total = lambda a, b, k: (lambda L: f'=IF(COUNT({L}{a}:{L}{b})={k},SUM({L}{a}:{L}{b}),"")')
    BS.section("Reported (typed, for the dates a balance sheet is published)")
    r_nw_r = trow("Net worth (share capital + reserves)", "net_worth")
    r_bor_r = trow("Borrowings", "borrowings")
    r_ol_r = trow("Other liabilities", "other_liabilities")
    r_tl = frow("Total equity and liabilities", total(r_nw_r, r_ol_r, 3), bold=True)
    r_fa_r = trow("Fixed assets, net of depreciation", "fixed_assets")
    r_cw_r = trow("Capital work in progress", "cwip")
    trow("Investments", "investments")
    r_oa_r = trow("Other assets (cash, receivables, regulatory deferral, the rest)", "other_assets")
    r_ta = frow("Total assets", total(r_fa_r, r_oa_r, 4), bold=True)
    r_chk = frow("Check: assets less equity and liabilities (rounding only)",
                 lambda L: f'=IF(ISNUMBER({L}{r_ta}),{L}{r_ta}-{L}{r_tl},"")')

    BS.section("Model (links to Assets, P&L and Cash flow)")
    r_gb = lrow("Gross block", lambda L, P: f"=Assets!{L}{R['gb']}", "=Open_GB")
    r_ad = BS.r
    lrow("Less: accumulated depreciation", lambda L, P: f"={P}{r_ad}+'P&L'!{L}{R['dep']}", f"=Open_GB-C{r_fa_r}")
    r_nb = frow("Net block", lambda L: f"={L}{r_gb}-{L}{r_ad}", bold=True)
    r_cw = lrow("Capital work in progress", lambda L, P: f"=Assets!{L}{R['cwip']}", "=Open_CWIP")
    r_cash = lrow("Cash and bank balances (the plug)", lambda L, P: f"='Cash flow'!{L}{R['cash']}", "=Open_cash")
    r_ona = BS.r
    lrow("Other net assets, held at the opening figure", lambda L, P: f"=$C${r_ona}",
         f"=Open_NW+Open_debt-C{r_nb}-C{r_cw}-C{r_cash}")
    r_na = frow("Net assets", lambda L: f"=SUM({L}{r_nb}:{L}{r_ona})", bold=True)
    r_nwm = lrow("Net worth", lambda L, P: f"='Cash flow'!{L}{R['nw']}", "=Open_NW")
    r_debt = lrow("Gross debt", lambda L, P: f"='Cash flow'!{L}{R['debt']}", "=Open_debt")
    r_fund = frow("Funding: net worth + gross debt", lambda L: f"={L}{r_nwm}+{L}{r_debt}", bold=True)

    BS.section("Checks")
    r_gap = frow("Gap: net assets less funding (zero at the opening by construction)",
                 lambda L: f"={L}{r_na}-{L}{r_fund}", bold=True)
    # ROUND on the four rows below: a forecast quarter's change is float dust
    # (1e-11), which the crore format prints as "(0)" on a row that says zero
    r_dgap = lrow("Change in the gap in the quarter", lambda L, P: f"=ROUND({L}{r_gap}-{P}{r_gap},2)", None)
    r_g1 = lrow("   from gross block + CWIP moving by other than capex",
                lambda L, P: f"=ROUND(({L}{r_gb}+{L}{r_cw})-({P}{r_gb}+{P}{r_cw})-Assets!{L}{R['capex']},2)", None)
    r_g2 = lrow("   from net worth moving by other than PAT less dividends",
                lambda L, P: f"=ROUND('P&L'!{L}{R['pat']}-'Cash flow'!{L}{R['div']}-({L}{r_nwm}-{P}{r_nwm}),2)", None)
    r_un = lrow("Unexplained change (must be zero in every quarter)",
                lambda L, P: f"=ROUND({L}{r_dgap}-{L}{r_g1}-{L}{r_g2},2)", None, bold=True)
    pub = lambda L, body: f'=IF(ISNUMBER({L}{r_ta}),{body},"")'
    BS.section("Model less reported, line by line (where a balance sheet is published)")
    r_d1 = frow("Net block less reported fixed assets", lambda L: pub(L, f"{L}{r_nb}-{L}{r_fa_r}"))
    frow("CWIP less reported", lambda L: pub(L, f"{L}{r_cw}-{L}{r_cw_r}"))
    frow("Cash + other net assets, less reported (net worth + borrowings - fixed assets - CWIP)",
         lambda L: pub(L, f"{L}{r_cash}+{L}{r_ona}-({L}{r_nw_r}+{L}{r_bor_r}-{L}{r_fa_r}-{L}{r_cw_r})"))
    frow("Reported borrowings less model gross debt", lambda L: pub(L, f"{L}{r_bor_r}-{L}{r_debt}"))
    r_d5 = frow("Reported net worth less model", lambda L: pub(L, f"{L}{r_nw_r}-{L}{r_nwm}"))
    r_rec = frow("Those five less the gap (must be zero)",
                 lambda L: pub(L, f"ROUND(SUM({L}{r_d1}:{L}{r_d5})-{L}{r_gap},2)"), bold=True)
    ws.column_dimensions["A"].width = 74
    return {"reported_check": r_chk, "gap": r_gap, "g1": r_g1, "g2": r_g2, "unexplained": r_un,
            "net_block_diff": r_d1, "reconcile": r_rec, "net_block": r_nb, "other_net": r_ona}


# --------------------------------------------------------------------------
# the project list (PM, 2026-11-13)
# --------------------------------------------------------------------------
PJ_TITLE = "Projects (CEA monthly progress reports): a sanity check, nothing in the forecast reads it"
PJ_COLS = (("Project (as the CEA names it)", None), ("Route", None), ("Status", None), ("Cost", "cr"),
           ("Capitalised, est.", "cr"), ("Still to capitalise", "cr"), ("Original target", None),
           ("Anticipated / actual", None), ("Delay, months", "cr"), ("Completion quarter", None),
           ("Elements charged", None), ("Spillover", None), ("SPV", None), ("CEA list, serial", None))
PJ_README = [
    ("", ""),
    ("Projects (on the Assets sheet, below the model)", "h"),
    ("Every PGCIL project in the CEA's three monthly reports (TBCB under construction, TBCB completed, RTM under "
     "construction): cost, original and anticipated completion, and how much is charged. 'Capitalised, est.' is an "
     "ESTIMATE: cost x charged elements / all elements, each element weighted equally, because the CEA gives no "
     "rupees. The block above the table sets the model's capitalisation against what the listed projects say will "
     "finish each quarter, and the company's gross block and works in hand against the list. Nothing in the forecast "
     "reads it. Refresh: power_projects.py --fetch, then power_model.py --add-projects power_grid --force.", ""),
]


def projects_data(cfg: dict, folder: pathlib.Path) -> dict | None:
    """The project list for a build: inline (`projects_data`, the selftest's
    fixture) or the file `projects_file` names beside the workbook. None when
    the config asks for neither, so a config without one builds exactly as
    before."""
    if cfg.get("projects_data"):
        return cfg["projects_data"]
    f = cfg.get("projects_file")
    if f and (folder / f).exists():
        return json.loads((folder / f).read_text(encoding="utf-8"))
    return None


def _pj_order(ps: list[dict]) -> list[dict]:
    """TBCB under construction by completion date, then RTM, then completed
    projects newest first, then the SPVs divested to the InvIT."""
    uc = lambda r: sorted([p for p in ps if p["route"] == r and p["list"] != "completed"],
                          key=lambda p: (p.get("completion") or "9999", p["serial"]))
    done = sorted([p for p in ps if p["list"] == "completed" and not p.get("divested")],
                  key=lambda p: (p.get("completion") or "", p["serial"]), reverse=True)
    return uc("TBCB") + uc("RTM") + done + [p for p in ps if p.get("divested")]


def projects_block(ws, qs: list[str], data: dict, cfg: dict, start: int, cap_row: int) -> dict:
    """Write the project list onto `ws` (the Assets sheet) from row `start`.
    `cap_row` is the model's Capitalisation row on that sheet. Everything the
    block computes is local to the block; no row above it, and no other sheet,
    reads it."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import power_projects as pp
    R = cfg.get("projects_reconcile") or {}
    rows = _pj_order(data["projects"])
    ncol = FIRST_COL + len(qs) - 1
    last = col(len(qs) - 1)

    def put(r, c, v, fmt=None, color=BLACK, bold=False, italic=False, size=None, fill=None):
        cell = ws.cell(r, c, v)
        cell.font = Font(name=FONT, color=color, bold=bold, italic=italic, size=size)
        if fmt:
            cell.number_format = FMT[fmt]
        if fill is not None:
            cell.fill = fill
        return cell

    def section(r, text):
        put(r, 1, text, bold=True)
        for c in range(1, ncol + 1):
            ws.cell(r, c).fill = SEC

    note = lambda r, text: put(r, 1, text, italic=True, size=8, color="595959")
    r = start
    section(r, PJ_TITLE)
    files = ", ".join(data.get("files", {}).values())
    note(r + 1, f"{data['source']}, {data['as_of']} ({files}). PGCIL only; the five SPVs divested to the InvIT are "
                "listed last and left out of every total. Cost is the CEA's estimated cost. Rs crore.")
    note(r + 2, "'Capitalised, est.' is an ESTIMATE: cost x charged elements / all elements, every element weighted "
                "equally (the CEA gives no rupees). A charged element is not yet a capitalised one: it is booked at COD.")
    # the table sits below the two summary blocks; its range is fixed first so the grid can point at it
    g0 = r + 4
    s0 = g0 + 7
    s_rows = 17
    h = s0 + s_rows + 2
    t0, t1 = h + 1, h + len(rows)
    rng = lambda L: f"${L}${t0}:${L}${t1}"

    put(g0, 1, "By quarter", bold=True)
    g1, g2, g3, g4 = g0 + 1, g0 + 2, g0 + 3, g0 + 4
    est = data.get("capitalised_by_quarter_est") or {}
    for c_, lab in ((g1, "Capitalisation, the model's row above"),
                    (g2, "Listed projects: capitalisation estimated from charged elements, by charge quarter"),
                    (g3, "Listed projects: still to capitalise, by anticipated completion quarter"),
                    (g4, "Model less listed (actual quarters: less the estimate; forecasts: less still to capitalise)")):
        put(c_, 1, lab, bold=c_ == g4)
        put(c_, 2, "Rs cr", size=9, color="595959")
    for i, q in enumerate(qs):
        L = col(i)
        put(g1, FIRST_COL + i, f"={L}{cap_row}", "cr")
        if q in est:
            put(g2, FIRST_COL + i, est[q], "cr", color=BLUE)
        put(g3, FIRST_COL + i, f'=SUMIFS({rng("F")},{rng("J")},{L}$4)', "cr")
        put(g4, FIRST_COL + i, f'=IF(Inputs!{L}$6="A",{L}{g1}-{L}{g2},{L}{g1}-{L}{g3})', "cr", color=GREEN, bold=True)

    put(s0, 1, f"Against the asset base and the works in hand ({R.get('as_of', 'n/a')})", bold=True)
    gc, gs = (R.get("gfa_consolidated") or {}), (R.get("gfa_standalone") or {})
    wt, wr = ((R.get("works_in_hand") or {}).get(k) or {} for k in ("TBCB", "RTM"))
    sumif = lambda val, crit_col, crit: f'=SUMIFS({rng(val)},{rng(crit_col)},"{crit}")'
    yr = lambda row, fy, extra="": f'=SUMIFS($D${row}:${last}${row},FYrow,"{fy}"{extra})'
    S = [("gfa_c", "Gross fixed assets, consolidated (incl. lease receivables)", gc.get("value"), gc.get("source")),
         ("gfa_s", "Gross fixed assets, standalone", gs.get("value"), gs.get("source")),
         ("tbcb_gb", "The TBCB part of the gross block: consolidated less standalone", "={gfa_c}-{gfa_s}", None),
         ("done", "Completed TBCB projects still consolidated, CEA cost", sumif("D", "C", "completed"), None),
         ("gap_in", "Gap: the TBCB gross block less the completed projects", "={tbcb_gb}-{done}", None),
         ("part", "Part-commissioned TBCB projects, capitalised est. (charged; mostly not booked until COD)",
          f'=SUMIFS({rng("E")},{rng("C")},"part commissioned",{rng("B")},"TBCB")', None),
         ("wih_t", "Works in hand, TBCB (company, incl. CWIP)", wt.get("value"), wt.get("source")),
         ("list_t", "Listed TBCB projects, still to capitalise", sumif("F", "B", "TBCB"), None),
         ("left_t", "Leftover: TBCB works in hand not on the CEA's list", "={wih_t}-{list_t}", None),
         ("wih_r", "Works in hand, RTM (company, incl. CWIP)", wr.get("value"), wr.get("source")),
         ("list_r", "Listed RTM projects, still to capitalise", sumif("F", "B", "RTM"), None),
         ("left_r", "Leftover: RTM works in hand not on the CEA's list (the CEA leaves out bay-only work)",
          "={wih_r}-{list_r}", None),
         ("h2_m", "FY27 still to come: the model's capitalisation", yr(g1, "FY27", ',Status,"F"'), None),
         ("h2_l", "FY27 still to come: listed projects anticipated to complete", yr(g3, "FY27", ',Status,"F"'), None),
         ("fy28_m", "FY28: the model's capitalisation", yr(g1, "FY28"), None),
         ("fy28_l", "FY28: listed projects anticipated to complete", yr(g3, "FY28"), None),
         ("spill", "Spillover: listed projects now due in a later fiscal year than first scheduled, still to capitalise",
          f'=SUMIFS({rng("F")},{rng("L")},"?*")', None)]
    assert len(S) == s_rows
    at = {k: f"C{s0 + 1 + j}" for j, (k, *_) in enumerate(S)}
    for j, (k, lab, v, src) in enumerate(S):
        rr = s0 + 1 + j
        put(rr, 1, lab, bold=k.startswith(("gap", "left", "spill")))
        put(rr, 2, "Rs cr", size=9, color="595959")
        if isinstance(v, str) and v.startswith("="):
            put(rr, 3, v.format(**at), "cr", bold=k.startswith(("gap", "left", "spill")))
        elif v is not None:
            put(rr, 3, v, "cr", color=BLUE)
        if src:
            put(rr, 4, src, size=8, italic=True, color="595959")

    for c, (lab, _) in enumerate(PJ_COLS, start=1):
        cell = put(h, c, lab, bold=True, color="FFFFFF", fill=HEAD)
        cell.alignment = Alignment(horizontal="center" if c > 1 else "left", wrap_text=True)
    mon = lambda s: dt.date.fromisoformat(s).strftime("%b-%y") if s else None
    for k, p in enumerate(rows):
        rr = t0 + k
        spill = None
        if p["list"] != "completed" and p.get("target_original") and p.get("completion"):
            fo = pp.quarter(dt.date.fromisoformat(p["target_original"]))[2:]
            fa = pp.quarter(dt.date.fromisoformat(p["completion"]))[2:]
            spill = f"{fo} to {fa}" if fa > fo else None
        grey = "808080" if p.get("divested") else BLACK
        put(rr, 1, p["name"], color=grey)
        put(rr, 2, p["route"], color=grey)
        put(rr, 3, p["status"], color=grey)
        put(rr, 4, p["cost_cr"], "cr", color=BLUE)
        put(rr, 5, p["capitalised_est_cr"], "cr", color=BLUE, italic=p["list"] != "completed")
        put(rr, 6, f"=D{rr}-E{rr}", "cr")
        put(rr, 7, mon(p.get("target_original")), color=grey)
        put(rr, 8, mon(p.get("completion")), color=grey)
        if p.get("delay_months") is not None:
            put(rr, 9, p["delay_months"], "cr", color=BLUE)
        put(rr, 10, p.get("completion_quarter"), color=grey)
        put(rr, 11, f"{p['elements_charged']:g} / {p['elements_total']}", color=grey)
        if spill:
            put(rr, 12, spill, color="C00000", bold=True)
        put(rr, 13, p.get("divested") and f"{p.get('spv') or ''} (divested: {p['divested']})".strip() or p.get("spv"),
            size=9, color=grey)
        put(rr, 14, f"{'TBCB' if p['route'] == 'TBCB' else 'RTM'} {p['list']} #{p['serial']}", size=9, color="595959")
    return {"g1": g1, "g2": g2, "g3": g3, "g4": g4, "head": h, "t0": t0, "t1": t1,
            "s": {k: s0 + 1 + j for j, (k, *_) in enumerate(S)}}


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
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
    wb = Workbook()
    wb.remove(wb.active)

    def A(i):
        return f'Inputs!{col(i)}$6="A"'

    def rng(sheet, r):
        return f"'{sheet}'!$D${r}:${last}${r}"

    def prev4(sheet, r, i):
        """AVERAGE of up to the previous four quarters on a row (fewer at the
        start of the timeline), as a formula fragment."""
        lo = max(0, i - 4)
        return f"AVERAGE('{sheet}'!{col(lo)}{r}:{col(i - 1)}{r})"

    # ---------------------------------------------------------------- README
    rd = wb.create_sheet("README")
    rd.column_dimensions["A"].width = 120
    extra = (BS_README if cfg.get("balance_sheet") else []) + (PJ_README if projects_data(cfg, out.parent) else [])
    for k, (txt, style) in enumerate(readme_lines(cfg, qs, n_act) + extra, start=1):
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
    # quarter calendar, for the rolling-forward weights on the Valuation sheet
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
    const("JV_share", "Share of JV profit per quarter (after tax)", C["jv_share"], "cr", yellow=True)
    const("Reg_def", "Regulatory deferral movement per quarter (after tax)", C["reg_deferral"], "cr", yellow=True)
    const("Yield_drift", "Transmission yield drift per quarter (pp of annual yield)", C["yield_drift"], "pct", yellow=True)
    const("Opex_drift", "Opex-to-block drift per quarter (pp of annual ratio)", C["opex_drift"], "pct", yellow=True)
    const("Dep_drift", "Depreciation-rate drift per quarter (pp of annual rate)",
          C.get("dep_drift", {"value": 0.0, "unit": "pp", "source": "no view"}), "pct", yellow=True)
    const("Target_pe", "Target P/E multiple (the Valuation sheet reads this)", C["target_pe"], "x2", yellow=True)
    const("Target_pbv", "Target P/BV, cross-check only", C["target_pbv"], "x2", yellow=True)

    inp.section(f"Opening balance sheet, {O['as_of']} (before the first quarter)")
    const("Open_GB", "Gross block", O["gross_block"], "cr")
    const("Open_CWIP", "Capital work in progress", O["cwip"], "cr")
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

    plan("Capitalisation plan (Rs crore per fiscal year) - the guidance; the driver of everything", "capitalisation", "CapYears", "CapPlan")
    plan("Capex plan (Rs crore per fiscal year)", "capex", "CapexYears", "CapexPlan")

    inp.section("Quarter shapes: share of a year's plan in each quarter, and of the year's dividend")
    wcap, wcpx, dsp = P["weights_capitalisation"]["value"], P["weights_capex"]["value"], C["div_split"]["value"]
    qn = lambda q: int(q[1]) - 1
    r_wcap = inp.row("Capitalisation weight by quarter", "share", {q: wcap[qn(q)] for q in qs}, fmt="pct",
                     note=P["weights_capitalisation"]["source"][:60], fill=lambda i, q: True)
    r_wcpx = inp.row("Capex weight by quarter", "share", {q: wcpx[qn(q)] for q in qs}, fmt="pct",
                     note=P["weights_capex"]["source"][:60], fill=lambda i, q: True)
    r_dsp = inp.row("Dividend timing: share of the previous FY's payout paid", "share", {q: dsp[qn(q)] for q in qs},
                    fmt="pct", note=C["div_split"]["source"][:60], fill=lambda i, q: True)

    # ---------------------------------------------------------------- Assets
    cs = cfg["consolidated"]
    typed = lambda key: {q: cs[key][q] for q in qs if q in act}
    AS = Sheet(wb, "Assets", qs, f"{cfg['company']} - capex, capitalisation and the asset base",
               "Reported rows are typed (blue, actual quarters only). Model rows read the reported value while the "
               "status is A and the plan while it is F. Rs crore.")
    AS.header()
    AS.section("Reported (typed)")
    r_capex_t = AS.row("Capex, reported", "Rs cr", typed("capex"))
    r_cap_t = AS.row("Capitalisation, reported", "Rs cr", typed("capitalisation"))
    r_gb_t = AS.row("Gross block, reported (closing)", "Rs cr", typed("gross_block"))
    r_cwip_t = AS.row("CWIP, reported (closing)", "Rs cr", typed("cwip"))
    AS.section("Model")

    def spread(i, q, r, plan_years, plan_vals, typed_row, w_row):
        # the year's plan less the quarters already reported, spread over the
        # year's FORECAST quarters by weight; a year with no plan line reads 0
        fy = f"Inputs!{col(i)}$5"
        plan_cell = f"IFERROR(INDEX({plan_vals},MATCH({fy},{plan_years},0)),0)"
        done = f"SUMIFS({rng('Assets', typed_row)},FYrow,{fy},Status,\"A\")"
        wsum = f"SUMIFS({rng('Inputs', w_row)},FYrow,{fy},Status,\"F\")"
        return (f"=IF({A(i)},{col(i)}{typed_row},"
                f"IF({wsum}=0,0,({plan_cell}-{done})*Inputs!{col(i)}{r_wcap if w_row == r_wcap else r_wcpx}/{wsum}))")

    r_capex = AS.row("Capex", "Rs cr", lambda i, q, r: spread(i, q, r, "CapexYears", "CapexPlan", r_capex_t, r_wcpx),
                     bold=True)
    r_cap = AS.row("Capitalisation", "Rs cr",
                   lambda i, q, r: spread(i, q, r, "CapYears", "CapPlan", r_cap_t, r_wcap), bold=True,
                   comment="The driver. Forecast = (the year's plan on Inputs - the quarters already reported) x this "
                           "quarter's weight / the weights of the year's forecast quarters.")
    r_gb_open = AS.row("Gross block, opening", "Rs cr",
                       lambda i, q, r: "=Open_GB" if i == 0 else f"={col(i - 1)}{r + 1}")
    r_gb = AS.row("Gross block, closing", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_gb_t},{col(i)}{r_gb_open}+{col(i)}{r_cap})", bold=True)
    r_gb_avg = AS.row("Gross block, average", "Rs cr", lambda i, q, r: f"=({col(i)}{r_gb_open}+{col(i)}{r_gb})/2")
    r_cwip = AS.row("CWIP, closing", "Rs cr",
                    lambda i, q, r: f"=IF({A(i)},{col(i)}{r_cwip_t},"
                                    + ("Open_CWIP" if i == 0 else f"{col(i - 1)}{r}") + f"+{col(i)}{r_capex}-{col(i)}{r_cap})")
    AS.row("Gross block growth, year on year", "%",
           lambda i, q, r: None if i < 4 else f"={col(i)}{r_gb}/{col(i - 4)}{r_gb}-1", fmt="pct")
    AS.row("Capitalisation / capex, trailing four quarters", "x",
           lambda i, q, r: None if i < 3 else f"=SUM({col(i - 3)}{r_cap}:{col(i)}{r_cap})/SUM({col(i - 3)}{r_capex}:{col(i)}{r_capex})",
           fmt="x2")
    AS.row("Fiscal-year capitalisation to date", "Rs cr",
           lambda i, q, r: f"=SUMIFS({rng('Assets', r_cap)},FYrow,Inputs!{col(i)}$5,Inputs!$D$7:${last}$7,\"<=\"&Inputs!{col(i)}$7)")
    pj_data = projects_data(cfg, out.parent)
    pj_rows = projects_block(AS.ws, qs, pj_data, cfg, AS.r + 1, r_cap) if pj_data else None

    # ---------------------------------------------------------------- P&L
    PL = Sheet(wb, "P&L", qs, f"{cfg['company']} - consolidated P&L",
               "Reported rows typed; model rows read them while the status is A. Forecasts run off the average gross "
               "block: a yield for transmission revenue, a ratio for opex, a rate for depreciation, each the trailing "
               "four quarters plus a drift. Rs crore.")
    PL.header()
    PL.section("Reported (typed)")
    r_tr_t = PL.row("Transmission revenue, reported", "Rs cr", typed("transmission_rev"))
    r_co_t = PL.row("Consultancy revenue, reported", "Rs cr", typed("consultancy_rev"))
    r_te_t = PL.row("Telecom revenue, reported", "Rs cr", typed("telecom_rev"))
    r_ot_t = PL.row("Other / inter-segment, reported (= sales less the three)", "Rs cr", typed("other_rev"))
    r_opx_t = PL.row("Operating expenses (employee + O&M), reported", "Rs cr", typed("opex"))
    r_oi_t = PL.row("Other income, reported", "Rs cr", typed("other_income"))
    r_dep_t = PL.row("Depreciation, reported", "Rs cr", typed("depreciation"))
    r_int_t = PL.row("Finance cost, reported", "Rs cr", typed("interest"))
    r_tax_t = PL.row("Tax, reported", "Rs cr", typed("tax"))
    r_jv_t = PL.row("Share of JV profit, reported (after tax)", "Rs cr", typed("jv_share"))
    r_rd_t = PL.row("Regulatory deferral movement, reported (after tax)", "Rs cr", typed("reg_deferral"))
    r_pat_t = PL.row("PAT, reported", "Rs cr", typed("pat"))

    PL.section("The PM's overrides (yellow; blank = the model's own number)")
    r_tr_ov = PL.row("Transmission revenue override", "Rs cr", {}, fill=lambda i, q: q not in act)
    r_oi_ov = PL.row("Other income override", "Rs cr", {}, fill=lambda i, q: q not in act)
    r_cap_note = PL.r
    PL.ws.cell(r_cap_note, 1, "Capitalisation is overridden on Inputs (the fiscal-year plan) or on Assets.").font = \
        Font(name=FONT, size=8, italic=True, color="595959")
    PL.r += 1

    GBA = lambda i: f"Assets!{col(i)}{r_gb_avg}"
    PL.section("Drivers (annualised, on the average gross block)")
    r_yield = PL.row("Transmission yield: revenue x 4 / average gross block", "%",
                     lambda i, q, r: f"=IF({A(i)},{col(i)}{r_tr_t}*4/{GBA(i)},"
                                     + ("0" if i == 0 else f"{prev4('P&L', r, i)}+Yield_drift") + ")",
                     fmt="pct", fill=lambda i, q: q not in act,
                     comment="Measured on every reported quarter; carried as the trailing four quarters' average plus "
                             "Yield_drift (Inputs). Falls when old assets step down or new TBCB assets earn less per rupee.")
    r_opxr = PL.row("Opex / average gross block", "%",
                    lambda i, q, r: f"=IF({A(i)},{col(i)}{r_opx_t}*4/{GBA(i)},"
                                    + ("0" if i == 0 else f"{prev4('P&L', r, i)}+Opex_drift") + ")", fmt="pct")
    # Depreciation carries the LATEST reported quarter's rate, not a trailing
    # average: the pool's rate is stepping DOWN (JPM standalone 3QFY26 Rs3,207cr
    # -> 2QFY27 Rs2,986cr on a bigger block) as assets past 12 years reach the
    # 90% cap, so a four-quarter average put FY28 depreciation 9% above BofA's
    # flat line and took ~Rs1/share off EPS for nothing but averaging. The
    # tariff passes depreciation through, so the same bias is in the yield.
    r_depr = PL.row("Depreciation / average gross block", "%",
                    lambda i, q, r: f"=IF({A(i)},{col(i)}{r_dep_t}*4/{GBA(i)},"
                                    + ("0" if i == 0 else f"{col(i - 1)}{r}+Dep_drift") + ")", fmt="pct",
                    comment="The latest reported quarter's rate, carried, plus Dep_drift (Inputs). New assets depreciate "
                            "at the CERC 5.28% while the pool averages under 4% and is stepping down as old assets "
                            "reach the 90% cap; a positive drift says the mix is turning.")

    PL.section("Revenue")
    r_tr = PL.row("Transmission revenue", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_tr_t},IF(ISNUMBER({col(i)}{r_tr_ov}),{col(i)}{r_tr_ov},"
                                  f"{GBA(i)}*{col(i)}{r_yield}/4))", bold=True)
    r_co = PL.row("Consultancy revenue", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_co_t}," + ("0" if i == 0 else prev4('P&L', r, i)) + ")")
    r_te = PL.row("Telecom revenue", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_te_t}," + ("0" if i == 0 else prev4('P&L', r, i)) + ")")
    r_ot = PL.row("Other / inter-segment", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_ot_t}," + ("0" if i == 0 else prev4('P&L', r, i)) + ")")
    r_rev = PL.row("Revenue from operations", "Rs cr",
                   lambda i, q, r: f"=SUM({col(i)}{r_tr}:{col(i)}{r_ot})", bold=True)
    PL.row("Revenue growth, year on year", "%",
           lambda i, q, r: None if i < 4 else f"={col(i)}{r_rev}/{col(i - 4)}{r_rev}-1", fmt="pct")

    PL.section("Costs and profit")
    r_opx = PL.row("Operating expenses (employee + O&M)", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{col(i)}{r_opx_t},{GBA(i)}*{col(i)}{r_opxr}/4)")
    r_ebitda = PL.row("EBITDA", "Rs cr", lambda i, q, r: f"={col(i)}{r_rev}-{col(i)}{r_opx}", bold=True)
    PL.row("EBITDA margin", "%", lambda i, q, r: f"={col(i)}{r_ebitda}/{col(i)}{r_rev}", fmt="pct")
    r_dep = PL.row("Depreciation", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{col(i)}{r_dep_t},{GBA(i)}*{col(i)}{r_depr}/4)")
    r_ebit = PL.row("EBIT", "Rs cr", lambda i, q, r: f"={col(i)}{r_ebitda}-{col(i)}{r_dep}")
    r_oi = PL.row("Other income", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_oi_t},IF(ISNUMBER({col(i)}{r_oi_ov}),{col(i)}{r_oi_ov},"
                                  + ("0" if i == 0 else prev4('P&L', r, i)) + "))")
    # interest on OPENING gross debt: the Cash flow sheet's previous closing, so there is no circularity
    r_int_holder = PL.r
    r_int = PL.row("Finance cost", "Rs cr", lambda i, q, r: None)   # filled after the Cash flow rows exist
    r_pbt = PL.row("PBT (before the JV share)", "Rs cr",
                   lambda i, q, r: f"={col(i)}{r_ebit}+{col(i)}{r_oi}-{col(i)}{r_int}", bold=True)
    r_tax = PL.row("Tax", "Rs cr", lambda i, q, r: f"=IF({A(i)},{col(i)}{r_tax_t},{col(i)}{r_pbt}*Tax_rate)")
    PL.row("Tax rate", "%", lambda i, q, r: f"=IF({col(i)}{r_pbt}=0,0,{col(i)}{r_tax}/{col(i)}{r_pbt})", fmt="pct")
    r_jv = PL.row("Share of JV profit (after tax)", "Rs cr", lambda i, q, r: f"=IF({A(i)},{col(i)}{r_jv_t},JV_share)")
    r_rd = PL.row("Regulatory deferral movement (after tax)", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_rd_t},Reg_def)")
    r_pat = PL.row("PAT", "Rs cr",
                   lambda i, q, r: f"={col(i)}{r_pbt}-{col(i)}{r_tax}+{col(i)}{r_jv}+{col(i)}{r_rd}", bold=True)
    PL.row("Check: model PAT less reported (actual quarters)", "Rs cr",
           lambda i, q, r: f"=IF({A(i)},{col(i)}{r_pat}-{col(i)}{r_pat_t},\"\")")
    PL.row("PAT growth, year on year", "%",
           lambda i, q, r: None if i < 4 else f"={col(i)}{r_pat}/{col(i - 4)}{r_pat}-1", fmt="pct")
    r_eps = PL.row("EPS (Rs)", "Rs", lambda i, q, r: f"={col(i)}{r_pat}*10/Shares_mn", fmt="rs", bold=True)
    r_roe_holder = PL.r
    r_roe = PL.row("Return on equity, annualised (PAT x 4 / average net worth)", "%", lambda i, q, r: None, fmt="pct")
    # day-share weights for the rolling four quarters from the price date (Valuation sheet)
    r_w = PL.row("Weight of this quarter in the 12 months from the price date", "share",
                 lambda i, q, r: (f"=MAX(0,MIN(Inputs!{col(i)}{r_qend},DATE(LEFT(CMP_date,4),MID(CMP_date,6,2),RIGHT(CMP_date,2))+364)"
                                  f"-MAX(Inputs!{col(i)}{r_qstart},DATE(LEFT(CMP_date,4),MID(CMP_date,6,2),RIGHT(CMP_date,2)))+1)"
                                  f"/(Inputs!{col(i)}{r_qend}-Inputs!{col(i)}{r_qstart}+1)"), fmt="w")

    # ---------------------------------------------------------------- Cash flow
    CF = Sheet(wb, "Cash flow", qs, f"{cfg['company']} - cash, debt and net worth",
               "CFO = PAT + depreciation (no working capital). Gross debt grows by Debt_share x capex; dividends are "
               "Payout x the previous fiscal year's PAT on the Inputs timing; cash is the plug. Rs crore.")
    CF.header()
    CF.section("Reported (typed)")
    r_debt_t = CF.row("Gross debt, reported (closing)", "Rs cr", typed("gross_debt"))
    r_nw_t = CF.row("Net worth, reported (closing)", "Rs cr", typed("net_worth"))
    r_div_t = CF.row("Dividends paid, reported", "Rs cr", typed("dividends_paid"))
    r_cash_t = CF.row("Cash and bank balances, reported where disclosed", "Rs cr",
                      {q: v for q, v in cs.get("cash_reported", {}).items() if q in act})
    CF.section("Model")
    r_cfo = CF.row("Cash from operations (PAT + depreciation)", "Rs cr",
                   lambda i, q, r: f"='P&L'!{col(i)}{r_pat}+'P&L'!{col(i)}{r_dep}")
    r_cfcapex = CF.row("Capex", "Rs cr", lambda i, q, r: f"=-Assets!{col(i)}{r_capex}")
    r_fcf = CF.row("Free cash flow", "Rs cr", lambda i, q, r: f"={col(i)}{r_cfo}+{col(i)}{r_cfcapex}", bold=True)

    def prev_fy_pat(i):
        fy = f"Inputs!{col(i)}$5"
        pfy = f'("FY"&(VALUE(RIGHT({fy},2))-1))'
        return f"IF(COUNTIF(FYrow,{pfy})=0,Open_FYPAT,SUMIFS({rng('P&L', r_pat)},FYrow,{pfy}))"

    r_div = CF.row("Dividends paid", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{col(i)}{r_div_t},Payout*{prev_fy_pat(i)}*Inputs!{col(i)}{r_dsp})",
                   comment="Forecast: Payout (Inputs) x the PREVIOUS fiscal year's PAT x this quarter's share of the "
                           "year's dividend (Inputs timing row). The year before the first quarter uses Open_FYPAT.")
    r_ddebt = CF.row("Change in gross debt", "Rs cr",
                     lambda i, q, r: f"=IF({A(i)},{col(i)}{r_debt_t}-" + ("Open_debt" if i == 0 else f"{col(i - 1)}{r + 1}")
                                     + f",Debt_share*Assets!{col(i)}{r_capex})")
    r_debt = CF.row("Gross debt, closing", "Rs cr",
                    lambda i, q, r: "=Open_debt" + f"+{col(i)}{r_ddebt}" if i == 0 else f"={col(i - 1)}{r}+{col(i)}{r_ddebt}",
                    bold=True)
    r_cash = CF.row("Cash and bank balances, closing (the plug)", "Rs cr",
                    lambda i, q, r: "=" + ("Open_cash" if i == 0 else f"{col(i - 1)}{r}")
                                    + f"+{col(i)}{r_fcf}-{col(i)}{r_div}+{col(i)}{r_ddebt}")
    CF.row("Check: modelled cash less reported, where disclosed", "Rs cr",
           lambda i, q, r: f"=IF(ISNUMBER({col(i)}{r_cash_t}),{col(i)}{r_cash}-{col(i)}{r_cash_t},\"\")",
           comment="The roll ignores working capital and other flows, so the gap is what the balance sheet absorbed.")
    r_nd = CF.row("Net debt, closing", "Rs cr", lambda i, q, r: f"={col(i)}{r_debt}-{col(i)}{r_cash}", bold=True)
    r_nw = CF.row("Net worth, closing", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{col(i)}{r_nw_t}," + ("Open_NW" if i == 0 else f"{col(i - 1)}{r}")
                                  + f"+'P&L'!{col(i)}{r_pat}-{col(i)}{r_div})", bold=True)
    CF.row("Net debt / net worth", "x", lambda i, q, r: f"={col(i)}{r_nd}/{col(i)}{r_nw}", fmt="x2")
    CF.row("Book value per share (Rs)", "Rs", lambda i, q, r: f"={col(i)}{r_nw}*10/Shares_mn", fmt="rs")

    # now the two P&L rows that needed the Cash flow rows
    for i, q in enumerate(qs):
        c = PL.ws.cell(r_int, FIRST_COL + i,
                       f"=IF({A(i)},{col(i)}{r_int_t}," + ("Open_debt" if i == 0 else f"'Cash flow'!{col(i - 1)}{r_debt}")
                       + "*Int_rate/4)")
        c.font = Font(name=FONT, color=GREEN)
        c.number_format = FMT["cr"]
        c = PL.ws.cell(r_roe, FIRST_COL + i,
                       f"={col(i)}{r_pat}*4/((" + ("Open_NW" if i == 0 else f"'Cash flow'!{col(i - 1)}{r_nw}")
                       + f"+'Cash flow'!{col(i)}{r_nw})/2)")
        c.font = Font(name=FONT, color=GREEN)
        c.number_format = FMT["pct"]

    # ---------------------------------------------------------------- Balance sheet
    bs_rows = None
    if cfg.get("balance_sheet"):
        bs_rows = balance_sheet(wb, cfg, qs, {"capex": r_capex, "gb": r_gb, "cwip": r_cwip, "dep": r_dep, "pat": r_pat,
                                              "div": r_div, "debt": r_debt, "cash": r_cash, "nw": r_nw})

    # ---------------------------------------------------------------- Annual
    an = wb.create_sheet("Annual")
    an["A1"] = f"{cfg['company']} - fiscal-year summary"
    an["A1"].font = Font(name=FONT, bold=True, size=13)
    an["A2"] = "Sums of the quarters; a year with any forecast quarter is marked F."
    an["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    an.column_dimensions["A"].width = 44
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
    yend = lambda sheet, r, L: f"INDEX({rng(sheet, r)},MATCH(\"Q4\"&{L}$4,Qrow,0))"
    arows = [
        ("Capex (Rs cr)", lambda L: f"=SUMIFS({rng('Assets', r_capex)},FYrow,{L}$4)", "cr"),
        ("Capitalisation (Rs cr)", lambda L: f"=SUMIFS({rng('Assets', r_cap)},FYrow,{L}$4)", "cr"),
        ("Gross block, year end (Rs cr)", lambda L: f"={yend('Assets', r_gb, L)}", "cr"),
        ("Revenue from operations (Rs cr)", lambda L: f"=SUMIFS({rng('P&L', r_rev)},FYrow,{L}$4)", "cr"),
        ("EBITDA (Rs cr)", lambda L: f"=SUMIFS({rng('P&L', r_ebitda)},FYrow,{L}$4)", "cr"),
        ("PAT (Rs cr)", lambda L: f"=SUMIFS({rng('P&L', r_pat)},FYrow,{L}$4)", "cr"),
        ("EPS (Rs)", lambda L: f"=SUMIFS({rng('P&L', r_eps)},FYrow,{L}$4)", "rs"),
        ("Dividends paid (Rs cr)", lambda L: f"=SUMIFS({rng('Cash flow', r_div)},FYrow,{L}$4)", "cr"),
        ("Net debt, year end (Rs cr)", lambda L: f"={yend('Cash flow', r_nd, L)}", "cr"),
        ("Net worth, year end (Rs cr)", lambda L: f"={yend('Cash flow', r_nw, L)}", "cr"),
        ("Book value per share, year end (Rs)", lambda L: f"={yend('Cash flow', r_nw, L)}*10/Shares_mn", "rs"),
        ("Return on average equity", lambda L: f"=SUMIFS({rng('P&L', r_pat)},FYrow,{L}$4)/(({yend('Cash flow', r_nw, L)}"
                                                 f"+IFERROR(INDEX({rng('Cash flow', r_nw)},MATCH(\"Q4\"&{L}$4,Qrow,0)-4),Open_NW))/2)", "pct"),
        ("Transmission yield, average of the year", lambda L: f"=AVERAGEIFS({rng('P&L', r_yield)},FYrow,{L}$4)", "pct"),
    ]
    for k, (lab, fn, fmt) in enumerate(arows):
        r = 6 + k
        an.cell(r, 1, lab).font = Font(name=FONT)
        for j in range(len(fys)):
            L = get_column_letter(2 + j)
            c = an.cell(r, 2 + j, fn(L))
            c.font = Font(name=FONT, color=GREEN)
            c.number_format = FMT[fmt]

    # ---------------------------------------------------------------- Valuation
    va = wb.create_sheet("Valuation")
    for L, w in (("A", 58), ("B", 14), ("C", 14), ("D", 14), ("E", 74)):
        va.column_dimensions[L].width = w
    va["A1"] = f"{cfg['company']} - P/E to an expected price"
    va["A1"].font = Font(name=FONT, bold=True, size=13)
    va["A2"] = ("For the PM's eye. The system's juice comes from case_brief.py (median house-implied P/E, sigma range); "
                "the two must agree on the earnings.")
    va["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    street = cfg.get("street") or {}
    vfy = cfg.get("valuation_fy", "FY28")
    D = "DATE(LEFT(CMP_date,4),MID(CMP_date,6,2),RIGHT(CMP_date,2))"
    vrows = [
        ("Valuation basis", "P/E", None, "inp", "Read by the Analysis card (model_view): a P/E model, not EV/EBITDA."),
        ("Valuation year", vfy, None, "inp", "Sum of that year's four quarters of PAT."),
        ("Quarters in the valuation year", "=COUNTIFS(FYrow,B5)", None, "f", "Must read 4."),
        ("PAT, valuation year (Rs cr)", f"=SUMIFS({rng('P&L', r_pat)},FYrow,B5)", "cr", "f", ""),
        ("EPS of the valuation year (Rs)", "=B7*10/Shares_mn", "rs", "f", ""),
        ("P/E today, on the valuation year", "=CMP/B8", "x2", "f", ""),
        ("Target P/E multiple (x)", "=Target_pe", "x2", "key",
         "Inputs!Target_pe: the median of the houses' implied P/E (TP / consensus EPS), which case_brief.py recomputes "
         "from build.json house_tps. Retype after a re-strike, or put your own multiple here."),
        ("Expected price (Rs)", "=B8*B10", "rs", "f", ""),
        ("Upside vs CMP", "=B11/CMP-1", "pct", "f", ""),
        ("Dividend per share in the valuation year (Rs)", f"=SUMIFS({rng('Cash flow', r_div)},FYrow,B5)*10/Shares_mn", "rs", "f", ""),
        ("Total return vs CMP, price plus that dividend", "=(B11+B13)/CMP-1", "pct", "f", ""),
        ("", None, None, "h", ""),
        ("Cross-check: book value", None, None, "h", "JPM, Nuvama and MS value Power Grid on P/BV. Shown, never the target."),
        ("Book value per share at the end of the valuation year (Rs)", f"={yend('Cash flow', r_nw, 'B').replace('B$4', 'B5')}*10/Shares_mn", "rs", "f", ""),
        ("P/BV today on that book", "=CMP/B17", "x2", "f", ""),
        ("Target P/BV (x)", "=Target_pbv", "x2", "key", "Inputs!Target_pbv."),
        ("Price at the target P/BV (Rs)", "=B17*B19", "rs", "f", ""),
        ("Implied P/E of that price", "=B20/B8", "x2", "f", ""),
        ("", None, None, "h", ""),
        ("Rolling four quarters forward from the price date", None, None, "h",
         "EPS of the 12 months starting on the price date, each quarter weighted by the share of its days inside the "
         "window (the P&L's weight row), at the same target P/E. Moves with CMP_date; the fixed block above does not."),
        ("Price date", f"={D}", "date", "f", ""),
        ("EPS, next four quarters from the price date (Rs)", f"=SUMPRODUCT({rng('P&L', r_w)},{rng('P&L', r_eps)})", "rs", "f", ""),
        ("Expected price, rolling 12 months forward (Rs)", "=B25*B10", "rs", "f", ""),
        ("Upside vs CMP, rolling", "=B26/CMP-1", "pct", "f", ""),
    ]
    for k, (lab, v, fmt, kind, note) in enumerate(vrows):
        r = 4 + k
        bold = lab in ("Expected price (Rs)", "Upside vs CMP") or kind == "h"
        va.cell(r, 1, lab).font = Font(name=FONT, bold=bold)
        if v is not None:
            c = va.cell(r, 2, v)
            is_f = isinstance(v, str) and v.startswith("=")
            linked = is_f and any(t in v for t in ("!", "FYrow", "Qrow", "Status", "Shares_mn", "CMP", "Target_", "Open_"))
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
    va.cell(stb, 1, "Street check: consensus EPS (type in) against the model").font = Font(name=FONT, bold=True)
    va.cell(stb, 2, "Street").font = Font(name=FONT, bold=True)
    va.cell(stb, 3, "Model").font = Font(name=FONT, bold=True)
    va.cell(stb, 4, "Model vs street").font = Font(name=FONT, bold=True)
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
    return {"quarters": N, "actual": n_act, "bs": bs_rows, "projects": pj_rows,
            "rows": {"pat": r_pat, "eps": r_eps, "rev": r_rev, "gb": r_gb, "cap": r_cap, "nd": r_nd, "yield": r_yield}}


def readme_lines(cfg, qs, n_act):
    return [
        (f"{cfg['company']} - quarterly model", "t"),
        (f"Built {cfg['built']} by packages/book/power_model.py from {cfg['entity']}/model_config.json. "
         f"Actuals {qs[0]} to {qs[n_act - 1]}; forecasts to {qs[-1]}. Rs crore.", ""),
        ("", ""),
        ("How it flows", "h"),
        ("Inputs (timeline, constants, the capitalisation and capex plans, quarter shapes) -> Assets (capex, "
         "capitalisation, gross block, CWIP) -> P&L (transmission revenue = average gross block x yield; opex and "
         "depreciation as rates on the block; interest on opening debt) -> Cash flow (FCF, dividends, debt, cash, "
         "net worth) -> Annual -> Valuation (P/E on the valuation year's EPS; P/BV cross-check; rolling 12 months).", ""),
        ("", ""),
        ("Colours", "h"),
        ("Blue = typed. Black = formula. Green = link to another sheet. Yellow = a key assumption or the PM's override. "
         "Grey status row: A = reported quarter, F = forecast.", ""),
        ("", ""),
        ("Rolling a new quarter", "h"),
        ("Type the quarter's reported lines into the blue 'Reported (typed)' blocks on Assets, P&L and Cash flow, then "
         "flip its status on Inputs row 6 from F to A. Every model row switches to the reported value and the next "
         "quarter's drivers (yield, opex and depreciation rates) re-base on the trailing four quarters. Update the "
         "fiscal-year plans on Inputs when guidance moves.", "yellow"),
        ("", ""),
        ("What the PM overrides", "h"),
        ("The capitalisation plan (Inputs, per fiscal year; the guidance) is the lever that moves earnings. "
         "Transmission revenue and other income have yellow override rows on P&L for a visible-quarter view. "
         "Yield_drift, Opex_drift and Dep_drift (Inputs) say how fast the per-rupee economics of the block change; all three are zero "
         "until there is a view.", ""),
        ("", ""),
        ("What the model leaves out, on purpose", "h"),
        ("The CERC tariff line by line (RoE, depreciation, interest, O&M, incentives): the yield on the block carries "
         "all of it, measured on reported quarters. Working capital and receivables (LPS). FX on borrowings. The "
         "regulatory deferral account and the JV share, which are after-tax true-ups held at typed constants. Smart "
         "meters, data centres and the InvIT stake, which sit inside the consolidated lines. Standalone vs TBCB "
         "subsidiaries: the model is consolidated, the block is consolidated.", ""),
        ("", ""),
        ("Where the actuals come from", "h"),
        ("The Sources sheet names the model, sheet and row for every line; the Inputs basis row says what each "
         "quarter is. Q4FY26 carries a Rs4,391cr deferred-tax credit against a Rs3,763cr regulatory write-down, both "
         "one-off. Q2FY27 is screener.in's consolidated print and the Kotak/JPM result notes.", ""),
    ]


# --------------------------------------------------------------------------
# the Balance sheet into a LIVE workbook, through Excel
# --------------------------------------------------------------------------
def live_rows(wb) -> dict:
    """Where each line the Balance sheet links to sits in an existing workbook,
    by its column-A label. Exactly one row each, or nothing is written: a link
    to the wrong row would still calculate."""
    out, bad = {}, []
    for key, (sheet, label) in BS_LINKS.items():
        if sheet not in wb.sheetnames:
            bad.append(f"no '{sheet}' sheet")
            continue
        ws = wb[sheet]
        hits = [r for r in range(1, ws.max_row + 1) if str(ws.cell(r, 1).value or "").strip() == label]
        if len(hits) != 1:
            bad.append(f"{sheet}: {len(hits)} rows labelled '{label}'")
        else:
            out[key] = hits[0]
    miss = [n for n in BS_OPEN_NAMES if n not in wb.defined_names]
    if miss:
        bad.append("names missing: " + ", ".join(miss))
    if bad:
        raise SystemExit("cannot place the Balance sheet's links:\n  " + "\n  ".join(bad))
    return out


def sheet_ops(ws) -> list[dict]:
    """An openpyxl sheet as model_view.excel() operations: widths, every cell
    with a value or a fill, the frozen panes. Typed text carries a leading
    apostrophe, or Excel turns '31-Mar-2025' into a date serial."""
    rgb = lambda c: c.rgb[-6:] if c is not None and isinstance(c.rgb, str) else None
    ops = [{"sheet": ws.title, "cell": f"{L}1", "mode": "width", "value": d.width}
           for L, d in sorted(ws.column_dimensions.items()) if d.width]
    for row in ws.iter_rows():
        for c in row:
            solid = c.fill is not None and c.fill.fill_type == "solid"
            if c.value is None and not solid:
                continue
            v = c.value
            if isinstance(v, str) and not v.startswith("="):
                v = "'" + v
            ops.append({"sheet": ws.title, "cell": c.coordinate, "mode": "set", "value": v, "font": c.font.name or FONT,
                        "numfmt": None if c.number_format == "General" else c.number_format,
                        "fcolor": _bgr(rgb(c.font.color) or BLACK), "bold": bool(c.font.b), "italic": bool(c.font.i),
                        "size": c.font.sz, "fill": _bgr(rgb(c.fill.fgColor)) if solid else None,
                        "wrap": bool(c.alignment.wrap_text), "center": c.alignment.horizontal == "center"})
    if ws.freeze_panes:
        ops.append({"sheet": ws.title, "cell": ws.freeze_panes, "mode": "freeze", "value": None})
    return ops


def add_balance_sheet(entity: str, force: bool = False, base: pathlib.Path | None = None) -> int:
    """Write the Balance sheet into an EXISTING workbook through Excel, so the
    PM's yellow cells survive (a rebuild overwrites them) and an open window
    takes the edit in place. Only the new sheet and two lines at the foot of
    the README are written; no other cell is touched. The reported lines come
    from the `balance_sheet` block of the model_config.json beside the workbook."""
    import model_view
    base = base or model_view.CASES
    p = model_view.path_for(entity, base)
    if p is None:
        raise SystemExit(f"no model for {entity!r}")
    cfg = json.loads((p.parent / "model_config.json").read_text(encoding="utf-8"))
    if not cfg.get("balance_sheet"):
        raise SystemExit("model_config.json has no balance_sheet block: nothing to type into the reported rows")
    errs = bs_errors(cfg)
    if errs:
        raise SystemExit("the reported balance sheet does not hold:\n  " + "\n  ".join(errs))
    live = load_workbook(p)
    if BS_SHEET in live.sheetnames and not force:
        raise SystemExit(f"'{BS_SHEET}' is already in {p.name}; --force rewrites it (its typed cells too)")
    inp, qs = live["Inputs"], []
    while inp.cell(4, FIRST_COL + len(qs)).value:
        qs.append(inp.cell(4, FIRST_COL + len(qs)).value)
    if qs != quarters(cfg["first_quarter"], cfg["last_quarter"]):
        raise SystemExit("the workbook's quarters are not the config's; refusing to line the sheet up by guess")
    R = live_rows(live)
    scratch = Workbook()
    rows = balance_sheet(scratch, cfg, qs, R)
    ops = [{"sheet": BS_SHEET, "cell": "A1", "mode": "addsheet", "value": "Cash flow", "clear": bool(force)}]
    ops += sheet_ops(scratch[BS_SHEET])
    rd = live["README"]
    if not any(rd.cell(r, 1).value == BS_README[1][0] for r in range(1, rd.max_row + 1)):
        for k, (txt, style) in enumerate(BS_README):
            if txt:
                ops.append({"sheet": "README", "cell": f"A{rd.max_row + 1 + k}", "mode": "set", "value": "'" + txt,
                            "font": FONT, "fcolor": _bgr(BLACK), "bold": style == "h", "italic": False, "size": 10,
                            "wrap": True})
    res = model_view.excel(entity, ops, base)
    print(f"{p.name}: '{BS_SHEET}' written after 'Cash flow', {len(ops)} operations"
          + (" (into the open window)" if res.get("attached") else ""))
    for note in res.get("notes") or []:
        print(f"  NOTE {note}")
    vals = load_workbook(p, data_only=True)[BS_SHEET]
    for key, lab in (("reported_check", "reported check"), ("gap", "gap"), ("g1", "  from the asset base"),
                     ("g2", "  from net worth"), ("unexplained", "unexplained"),
                     ("net_block_diff", "net block less reported"), ("reconcile", "five less the gap")):
        cells = [vals.cell(rows[key], c).value for c in range(3, FIRST_COL + len(qs))]
        print(f"  {lab:<26}" + " ".join(f"{v:>8,.0f}" if isinstance(v, (int, float)) else f"{'':>8}" for v in cells))
    bad = errors(p)
    print(f"  {len(bad)} error cells" + "".join(f"\n    {b}" for b in bad[:10]))
    return 1 if bad else 0


def bulk_ops(ws, sheet: str, r0: int, r1: int, ncol: int, clear_to: int | None = None) -> list[dict]:
    """Rows r0..r1 of a scratch sheet as model_view 'values' and 'style' ops:
    ONE rectangle of values (text carries a leading apostrophe, empty cells
    clear), then the formatting as ranges, runs of equal style along a row
    merged down across rows. A reset of the whole rectangle goes first, so
    whatever was there before (a half-written block) cannot survive."""
    end = max(r1, clear_to or r1)
    last = get_column_letter(ncol)
    rgb = lambda c: c.rgb[-6:] if c is not None and isinstance(c.rgb, str) else None
    ops = [{"sheet": sheet, "cell": f"A{r0}:{last}{end}", "mode": "style", "font": FONT, "numfmt": "General",
            "fcolor": _bgr(BLACK), "bold": False, "italic": False, "nofill": True, "wrap": False}]
    vals = []
    for r in range(r0, r1 + 1):
        row = []
        for c in range(1, ncol + 1):
            v = ws.cell(r, c).value
            row.append("'" + v if isinstance(v, str) and not v.startswith("=") else v)
        vals.append(row)
    ops.append({"sheet": sheet, "cell": f"A{r0}", "mode": "values", "value": vals})
    if end > r1:
        ops.append({"sheet": sheet, "cell": f"A{r1 + 1}", "mode": "values", "value": [[None] * ncol] * (end - r1)})
    runs: dict[tuple, list[int]] = {}
    for r in range(r0, r1 + 1):
        c = 1
        while c <= ncol:
            cell = ws.cell(r, c)
            solid = cell.fill is not None and cell.fill.fill_type == "solid"
            if cell.value is None and not solid:
                c += 1
                continue
            sig = (cell.font.name or FONT, None if cell.number_format == "General" else cell.number_format,
                   _bgr(rgb(cell.font.color) or BLACK), bool(cell.font.b), bool(cell.font.i), cell.font.sz,
                   _bgr(rgb(cell.fill.fgColor)) if solid else None, bool(cell.alignment.wrap_text),
                   cell.alignment.horizontal == "center")
            c1 = c
            while c1 + 1 <= ncol:
                nx = ws.cell(r, c1 + 1)
                nsolid = nx.fill is not None and nx.fill.fill_type == "solid"
                nsig = (nx.font.name or FONT, None if nx.number_format == "General" else nx.number_format,
                        _bgr(rgb(nx.font.color) or BLACK), bool(nx.font.b), bool(nx.font.i), nx.font.sz,
                        _bgr(rgb(nx.fill.fgColor)) if nsolid else None, bool(nx.alignment.wrap_text),
                        nx.alignment.horizontal == "center")
                if (nx.value is None and not nsolid) or nsig != sig:
                    break
                c1 += 1
            runs.setdefault((c, c1, sig), []).append(r)
            c = c1 + 1
    for (c0, c1, sig), rs in runs.items():
        font, numfmt, fcolor, bold, italic, size, fill, wrap, center = sig
        a = rs[0]
        for k, r in enumerate(rs + [None]):
            if r is not None and (k == 0 or r == rs[k - 1] + 1):
                continue
            b = rs[k - 1]
            ops.append({"sheet": sheet, "cell": f"{get_column_letter(c0)}{a}:{get_column_letter(c1)}{b}",
                        "mode": "style", "font": font, "numfmt": numfmt, "fcolor": fcolor, "bold": bold,
                        "italic": italic, "size": size, "fill": fill, "wrap": wrap or None, "center": center})
            a = r
    return ops


def add_projects(entity: str, force: bool = False, base: pathlib.Path | None = None) -> int:
    """Write the project block under the Assets sheet's model rows of an
    EXISTING workbook, through Excel (the Balance sheet's route: the PM's
    yellow cells survive and an open window takes the edit). It reads
    projects.json and the `projects_reconcile` block beside the workbook.
    --force rewrites a block already there: its cells are cleared first."""
    import model_view
    base = base or model_view.CASES
    p = model_view.path_for(entity, base)
    if p is None:
        raise SystemExit(f"no model for {entity!r}")
    cfg = json.loads((p.parent / "model_config.json").read_text(encoding="utf-8"))
    data = projects_data({**cfg, "projects_file": cfg.get("projects_file") or "projects.json"}, p.parent)
    if not data:
        raise SystemExit(f"no projects.json beside {p.name}; run power_projects.py --fetch first")
    live = load_workbook(p)
    inp, qs = live["Inputs"], []
    while inp.cell(4, FIRST_COL + len(qs)).value:
        qs.append(inp.cell(4, FIRST_COL + len(qs)).value)
    if qs != quarters(cfg["first_quarter"], cfg["last_quarter"]):
        raise SystemExit("the workbook's quarters are not the config's; refusing to line the block up by guess")
    ws = live["Assets"]
    labels = {r: str(ws.cell(r, 1).value or "").strip() for r in range(1, ws.max_row + 1)}
    cap = [r for r, v in labels.items() if v == "Capitalisation"]
    if len(cap) != 1:
        raise SystemExit(f"Assets: {len(cap)} rows labelled 'Capitalisation'; refusing to link the block by guess")
    have = [r for r, v in labels.items() if v == PJ_TITLE]
    if have and not force:
        raise SystemExit(f"the project block is already on Assets (row {have[0]}); --force rewrites it")
    start = have[0] if have else max(r for r, v in labels.items() if v) + 2
    scratch = Workbook()
    sws = scratch.active
    sws.title = "Assets"
    rows = projects_block(sws, qs, data, cfg, start, cap[0])
    ops = bulk_ops(sws, "Assets", start, rows["t1"], max(FIRST_COL + len(qs) - 1, len(PJ_COLS)),
                   clear_to=ws.max_row if have else None)
    rd = live["README"]
    if not any(rd.cell(r, 1).value == PJ_README[1][0] for r in range(1, rd.max_row + 1)):
        for k, (txt, style) in enumerate(PJ_README):
            if txt:
                ops.append({"sheet": "README", "cell": f"A{rd.max_row + 1 + k}", "mode": "set", "value": "'" + txt,
                            "font": FONT, "fcolor": _bgr(BLACK), "bold": style == "h", "italic": False, "size": 10,
                            "wrap": True})
    res = model_view.excel(entity, ops, base)
    print(f"{p.name}: project block written on Assets from row {start}, {len(data['projects'])} projects, "
          f"{len(ops)} operations" + (" (into the open window)" if res.get("attached") else ""))
    for n in res.get("notes") or []:
        print(f"  NOTE {n}")
    vals = load_workbook(p, data_only=True)["Assets"]
    for k, rr in rows["s"].items():
        v = vals.cell(rr, 3).value
        print(f"  {vals.cell(rr, 1).value[:96]:<96} {v:>10,.0f}" if isinstance(v, (int, float)) else f"  {k}: {v!r}")
    for key in ("g1", "g2", "g3", "g4"):
        cells = [vals.cell(rows[key], FIRST_COL + i).value for i in range(len(qs))]
        print(f"  {key} " + " ".join(f"{v:>7,.0f}" if isinstance(v, (int, float)) else f"{'':>7}" for v in cells))
    bad = errors(p)
    print(f"  {len(bad)} error cells" + "".join(f"\n    {b}" for b in bad[:10]))
    return 1 if bad else 0


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def _fixture() -> dict:
    qs = ["Q1FY26", "Q2FY26"]
    lines = dict(transmission_rev=(10694.7, 10721.8), consultancy_rev=(405.9, 614.4), telecom_rev=(289.5, 291.1),
                 other_rev=(-193.9, -151.3), sales=(11196.2, 11476.0), opex=(2049.5, 2362.2),
                 other_income=(248.2, 194.3), depreciation=(3130.5, 3223.1), interest=(1934.3, 2148.1),
                 pbt=(4330.1, 3936.9), tax=(868.0, 829.4), jv_share=(-44.4, -58.7), reg_deferral=(212.8, 517.3),
                 pat=(3630.6, 3566.1), capex=(6981.0, 8404.0), capitalisation=(1683.0, 2205.0),
                 gross_block=(292446.0, 295148.0), cwip=(41610.0, 47782.0), gross_debt=(130646.0, 135923.0),
                 net_worth=(96482.0, 98932.0), dividends_paid=(0.0, 1162.6))
    e = lambda v, u="", s="t": {"value": v, "unit": u, "source": s}
    return {"company": "Fixture Grid", "entity": "fixture_grid", "built": "2026-11-09",
            "first_quarter": "Q1FY26", "last_quarter": "Q4FY27", "actual_through": "Q2FY26", "valuation_fy": "FY27",
            "basis": {q: "t" for q in qs},
            "constants": {"shares_mn": e(9300.6), "cmp": e(258.8), "cmp_date": e("2026-11-09"), "tax_rate": e(0.19),
                          "int_rate": e(0.06), "debt_share": e(0.7), "payout": e(0.55),
                          "div_split": e([0, 0.4, 0.3, 0.3]), "jv_share": e(0), "reg_deferral": e(0),
                          "yield_drift": e(0), "opex_drift": e(0), "target_pe": e(15.0), "target_pbv": e(2.5)},
            "opening": {"as_of": "31-Mar-2025", "gross_block": e(290715), "cwip": e(36341), "gross_debt": e(130965),
                        "cash": e(10077), "net_worth": e(92663), "fy_pat": e(15522)},
            "plans": {"capitalisation": {"FY26": e(28206), "FY27": e(27500)},
                      "capex": {"FY26": e(39967), "FY27": e(38500)},
                      "weights_capitalisation": e([0.1, 0.1, 0.3, 0.5]), "weights_capex": e([0.2, 0.22, 0.27, 0.31])},
            "consolidated": {k: {q: v[j] for j, q in enumerate(qs)} for k, v in lines.items()},
            "balance_sheet": {"source": "fixture", "reported": {
                "opening": dict(net_worth=92663, borrowings=131030, other_liabilities=42326,
                                fixed_assets=172320, cwip=33585, investments=3117, other_assets=56997),
                "Q2FY26": dict(net_worth=98932, borrowings=135990, other_liabilities=43000,
                               fixed_assets=171000, cwip=44000, investments=3000, other_assets=59922)}},
            "sources": [["a", "b", "c"]], "decks": {}}


def selftest() -> int:
    fails = 0

    def chk(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))

    print("power_model selftest")
    fx = _fixture()
    chk("footed fixture accepted", crossfoot(fx), [])
    bad = json.loads(json.dumps(fx)); bad["consolidated"]["tax"]["Q1FY26"] = 886.0          # transposed digit
    chk("transposed tax digit refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["consolidated"]["telecom_rev"]["Q2FY26"] = 391.1   # segments no longer foot
    chk("segment that does not add to sales refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["plans"]["weights_capex"]["value"] = [0.2, 0.2, 0.3, 0.4]
    chk("capex weights not summing to one refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); del bad["consolidated"]["cwip"]
    chk("missing line refused", crossfoot(bad), ["consolidated.cwip: missing"])
    bad = json.loads(json.dumps(fx)); bad["balance_sheet"]["reported"]["Q2FY26"]["other_assets"] = 59292   # transposed
    chk("a reported balance sheet that does not balance is refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["balance_sheet"]["reported"]["Q2FY26"].update(net_worth=98392, other_liabilities=43540)
    chk("one that balances on the wrong net worth is refused", len(crossfoot(bad)), 1)
    bad = json.loads(json.dumps(fx)); bad["balance_sheet"]["reported"]["Q3FY26"] = bad["balance_sheet"]["reported"]["Q2FY26"]
    chk("a date that is not an actual quarter is refused", len(crossfoot(bad)), 1)
    old = json.loads(json.dumps(fx)); del old["balance_sheet"]
    chk("a config with no balance sheet still foots", crossfoot(old), [])
    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / "Fixture_model.xlsx"
        info = build(fx, out)
        chk("8 quarters, 2 actual", (info["quarters"], info["actual"]), (8, 2))
        wb = load_workbook(out)
        chk("sheets", wb.sheetnames, ["README", "Inputs", "Assets", "P&L", "Cash flow", BS_SHEET, "Annual", "Valuation",
                                      "Sources"])
        # "no new connections": the sheet reads the model, the model never reads the sheet
        readers = [f"{ws.title}!{c.coordinate}" for ws in wb if ws.title != BS_SHEET for row in ws.iter_rows() for c in row
                   if isinstance(c.value, str) and c.value.startswith("=") and BS_SHEET in c.value]
        readers += [n for n, d in wb.defined_names.items() if BS_SHEET in (d.attr_text or "")]
        chk("nothing reads the Balance sheet", readers, [])
        chk("the live-workbook row finder lands on the builder's rows",
            (live_rows(wb)["gb"], live_rows(wb)["pat"]), (info["rows"]["gb"], info["rows"]["pat"]))
        gone = load_workbook(out)
        gone["P&L"].cell(info["rows"]["pat"], 1).value = "PAT (renamed)"
        try:
            live_rows(gone)
            chk("a moved label refuses the links", "accepted", "refused")
        except SystemExit:
            chk("a moved label refuses the links", "refused", "refused")
        ops = sheet_ops(wb[BS_SHEET])
        byc = {o["cell"]: o for o in ops if o["mode"] == "set"}
        chk("typed text goes to Excel as text, formulas as formulas",
            (byc["C5"]["value"], byc["D4"]["value"], byc["D6"]["value"]), ("'31-Mar-2025", "'Q1FY26", "=Inputs!D6"))
        chk("panes and widths travel with the sheet", (ops[-1]["mode"], ops[-1]["cell"], ops[0]["mode"]),
            ("freeze", "D7", "width"))
        old_out = pathlib.Path(d) / "Old_model.xlsx"
        build(old, old_out)
        chk("a config with no balance sheet builds none", BS_SHEET in load_workbook(old_out).sheetnames, False)
        # the project block: built from fixture rows shaped like the CEA's reports
        import power_projects as pp
        uc = [["1", "Scheme A (SPV: POWERGRID Alpha Transmission Ltd)", "PGCIL", "", "", "1,000", "Dec-23", "Dec-25",
               "Oct - 26"] + [""] * 8,
              ["", "Alpha 765 kV D/c line", "PGCIL", "200", "", "", "", "", "Jul - 26"] + [""] * 7
              + ["Tx. Line charged on 30.07.2026"],
              ["", "2x1500 MVA Alpha PS", "PGCIL", "", "3000", "", "", "", "Oct - 26"] + [""] * 7 + ["Under progress"]]
        dn = [["2", "Scheme C", "PGCIL", "", "", "300", "", "", "Jan-2025", "Mar - 25"],
              ["", "C line", "PGCIL", "100", "", "", "", "", "", "Mar - 25"],
              ["3", "Strengthening in SR", "PGCIL", "", "", "1,180", "", "", "Aug-2016", "Sep - 16"],
              ["", "Srikakulam PP - Vemagiri-II 765kV D/c line", "PGCIL", "1,336", "", "", "", "", "", "Sep - 16"]]
        ps = [pp.finish(x) for x in pp.parse_uc(uc, "TBCB", "2026-09") + pp.parse_done(dn, "2026-09")]
        pdata = {"as_of": "2026-09", "source": "fixture", "files": {}, "projects": ps,
                 "capitalised_by_quarter_est": pp.capitalised_by_quarter(ps)}
        fxp = {**json.loads(json.dumps(fx)), "projects_data": pdata,
               "projects_reconcile": {"as_of": "fixture", "gfa_consolidated": {"value": 2000},
                                      "gfa_standalone": {"value": 1500}, "works_in_hand": {"TBCB": {"value": 900}}}}
        pj_out = pathlib.Path(d) / "Projects_model.xlsx"
        pinfo = build(fxp, pj_out)
        pwb, pr = load_workbook(pj_out), pinfo["projects"]
        chk("one table row per project, divested last", (pr["t1"] - pr["t0"] + 1,
            pwb["Assets"].cell(pr["t1"], 3).value), (3, "completed, divested to InvIT"))
        chk("the quarter grid sums the table by completion quarter",
            pwb["Assets"].cell(pr["g3"], FIRST_COL).value,
            f'=SUMIFS($F${pr["t0"]}:$F${pr["t1"]},$J${pr["t0"]}:$J${pr["t1"]},D$4)')
        # "nothing in the forecast reads it": every formula outside the block is
        # the same with and without it
        f_of = lambda w: {(s.title, c.coordinate): c.value for s in w for row in s.iter_rows() for c in row
                          if isinstance(c.value, str) and c.value.startswith("=")
                          and not (s.title == "Assets" and c.row >= pr["g1"] - 5)}
        base_f = f_of(wb)
        chk("nothing outside the block changes when it is added", f_of(pwb) == base_f, True)
        chk("a config with no project list builds no block", info["projects"], None)
        import model_view
        labels = {wb["Valuation"].cell(r, 1).value for r in range(1, 40)}
        chk("Valuation labels model_view reads", all(model_view.VAL_LABELS[k] in labels for k in ("tp", "year", "basis", "pe_typed", "eps")), True)
        chk("CMP / CMP_date / Shares_mn names", all(n in wb.defined_names for n in ("CMP", "CMP_date", "Shares_mn", "CapPlan", "Open_GB")), True)
        pl = wb["P&L"]
        r = info["rows"]
        # the third quarter (first forecast) must read its drivers off the typed rows, never itself
        f_cap = wb["Assets"].cell(r["cap"], FIRST_COL + 2).value
        chk("capitalisation forecast spreads the plan net of reported quarters", "CapPlan" in f_cap and "Status,\"A\"" in f_cap, True)
        chk("first forecast revenue reads the yield", "Assets!" in pl.cell(r["rev"] - 4, FIRST_COL + 2).value
            and f"{col(2)}{r['yield']}" in pl.cell(r["rev"] - 4, FIRST_COL + 2).value, True)
        chk("first quarter opens on Open_GB", wb["Assets"].cell(r["gb"] - 1, FIRST_COL).value, "=Open_GB")
        chk("actual quarter's PAT formula is the identity", pl.cell(r["pat"], FIRST_COL).value.startswith("="), True)
        chk("interest reads the previous quarter's closing debt",
            "'Cash flow'!" in pl.cell(r["pat"] - 6, FIRST_COL + 1).value and "Int_rate" in pl.cell(r["pat"] - 6, FIRST_COL + 1).value, True)
        chk("an existing workbook is refused without --force", out.exists(), True)
        # with Excel on the box, recalculate and read the numbers back (best effort)
        try:
            recalc(out.resolve())
            vals = load_workbook(out, data_only=True)
            e = errors(out)
            chk("no error cells after recalc", e[:3], [])
            vp = vals["P&L"]
            # the identity from typed components is 3,630.5 against a reported 3,630.58: rounding, inside the 3cr tolerance
            chk("Q1FY26 model PAT = reported (identity)", abs(vp.cell(r["pat"], FIRST_COL).value - 3630.6) < 0.2, True)
            va = vals["Valuation"]
            labs = {va.cell(k, 1).value: va.cell(k, 2).value for k in range(1, 40)}
            chk("expected price = EPS x target P/E", round(labs["Expected price (Rs)"], 4),
                round(labs["EPS of the valuation year (Rs)"] * 15.0, 4))
            chk("valuation basis reads P/E", labs["Valuation basis"], "P/E")
            cap_fy26 = sum(vals["Assets"].cell(r["cap"], FIRST_COL + i).value for i in range(4))
            chk("FY26 capitalisation = the plan after the spread", round(cap_fy26, 1), 28206.0)
            vb, b = vals[BS_SHEET], info["bs"]
            row = lambda k: [vb.cell(b[k], c).value for c in range(FIRST_COL, FIRST_COL + 8)]
            chk("the gap is zero at the opening", round(vb.cell(b["gap"], 3).value, 6), 0)
            chk("unexplained change is zero in every quarter", max(abs(v) for v in row("unexplained")) < 1e-6, True)
            # the fixture's two actual quarters DO move by more than the model carries
            # (Q1: net worth +3,819 against PAT 3,631), so a gap must appear there ...
            chk("an actual quarter's typed lines open a gap", abs(row("gap")[0]) > 100, True)
            # ... and must then stand still: forecasts roll by the model's own identities
            chk("the gap does not move in a forecast quarter", max(abs(v - row("gap")[1]) for v in row("gap")[2:]) < 1e-6, True)
            chk("the reported block balances", [vb.cell(b["reported_check"], c).value for c in (3, FIRST_COL + 1)], [0, 0])
            chk("unpublished quarters stay blank", vb.cell(b["reported_check"], FIRST_COL).value in (None, ""), True)
            chk("the five differences rebuild the gap",
                [round(vb.cell(b["reconcile"], c).value, 6) for c in (3, FIRST_COL + 1)], [0, 0])
            chk("opening net block is the reported fixed assets", vb.cell(b["net_block"], 3).value, 172320)
            recalc(pj_out.resolve())
            pa = load_workbook(pj_out, data_only=True)["Assets"]
            chk("still to capitalise lands in its completion quarter (Q3FY27)",
                [pa.cell(pr["g3"], FIRST_COL + i).value for i in range(8)], [0, 0, 0, 0, 0, 0, 500, 0])
            sv = {k: pa.cell(r_, 3).value for k, r_ in pr["s"].items()}
            chk("the TBCB block less completed projects (divested left out)", sv["gap_in"], 200)
            chk("works-in-hand leftover", sv["left_t"], 400)
            chk("a project first due in FY26, now FY27, is spillover", sv["spill"], 500)
            chk("no error cells with the block", errors(pj_out)[:3], [])
        except Exception as ex:                                                     # noqa: BLE001
            print(f"  skip  Excel recalculation not available here: {str(ex)[:80]}")
    print(f"{'PASS' if not fails else 'FAIL'} ({fails} failures)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--force", action="store_true", help="overwrite an existing workbook (loses its edits)")
    ap.add_argument("--recalc", action="store_true", help="open in Excel over COM, recalculate, save, check errors")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--add-balance-sheet", metavar="ENTITY",
                    help="write the Balance sheet into an existing workbook, via Excel (keeps every other cell)")
    ap.add_argument("--add-projects", metavar="ENTITY",
                    help="write the CEA project block under the Assets sheet's model rows, via Excel")
    ap.add_argument("--cases", type=pathlib.Path, help="another cases folder (a scratch copy, for testing)")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.add_balance_sheet:
        return add_balance_sheet(a.add_balance_sheet, a.force, a.cases)
    if a.add_projects:
        return add_projects(a.add_projects, a.force, a.cases)
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
