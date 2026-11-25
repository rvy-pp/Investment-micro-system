"""A quarterly IPP model in Excel: capacity -> generation -> EBITDA per unit -> P&L -> cash -> EV/EBITDA.

PM, 2026-11-25: *"jsw energy agent: valuation table, model. Search downloads
for models."* `genco_model.py` drives a REGULATED generator off its regulated
equity, with fuel passed through. JSW Energy is mostly contracted, part
merchant and growing by commissioning: ~15GW operating and ~15GW under
construction at September 2026. What moves its earnings is how many MW are
running, how hard they run (PLF, which for hydro and wind is half the story
and wholly seasonal) and what each unit earns. So this builder has its own
driver, and any other IPP (Tata Power's generation book, Adani Power, NHPC
outside its regulated tariff) can reuse it.

THE DRIVER, PER BUCKET (thermal, hydro, renewables; a config may name more):

- Capacity. Operating MW are typed for reported quarters. Each fiscal year's
  ADDITION plan (yellow, on Inputs) is spread over its forecast quarters by
  the quarter weights, net of what is already reported, so a plan line never
  double-counts a commissioned MW. Average MW = (opening + closing) / 2.
- Generation = average MW x hours in the quarter x PLF. A reported quarter
  measures its PLF; a forecast takes the SAME QUARTER A YEAR EARLIER plus a
  drift (pp, yellow). Hydro runs at ~90% in Q2 and ~14% in Q4; wind at ~46%
  and ~16%. A trailing average would put the monsoon into the winter.
- EBITDA per unit (Rs/kWh, incl. the entity-level other income the company's
  segment table carries) and revenue per unit, on a basis the config names
  per bucket: `yoy` (the year-ago quarter: hydro, whose capacity charges make
  the per-unit figure seasonal) or `trailing4` (thermal and renewables, where
  the year-ago quarter carries a structure that has since changed - Utkal's
  move to long-term PPAs, an O2 one-off in Q4FY26). Plus a drift, yellow.
- Others (transmission, storage, green hydrogen, unallocated, eliminations)
  is the PLUG on reported quarters - reported revenue and EBITDA + other income
  less the three buckets - so the buckets always add back to the P&L. A
  forecast takes its trailing four quarters.

Consolidated: EBITDA = the buckets' EBITDA incl. other income, less
consolidated other income (trailing four quarters). Depreciation = average
operating MW x a rate per MW (the latest reported quarter's, carried, plus a
drift). Interest = opening gross debt x a rate (the P&L rate, after interest
capitalised on the construction book). Tax and the minority share are
constants measured on the latest half-year. EPS on the basic share count.

Cash: CFO = PAT + minority + depreciation (no working capital). Capex follows
the fiscal-year plan. GROSS DEBT IS THE PLUG: it rises by capex + dividends
- CFO - equity raised; cash holds at the last reported balance. For a
developer spending Rs20,000cr+ a year against Rs7,000cr of CFO that is the
honest shape: the funding gap shows up as debt, and so as interest. A cash
plug with a fixed debt share would let cash go negative and understate
interest. Debt, cash and net worth are typed at balance-sheet dates and the
roll resets to them.

Valuation reuses genco_model's layout: P/E in column B, EV/EBITDA in column C
with every house's target and the multiple it implies (`genco_model.ev_block`),
the basis the Analysis card reads typed in B from build.json.

    python packages/book/ipp_model.py --config data/analysis/cases/jsw_energy/model_config.json \\
        --out data/analysis/cases/jsw_energy/JSW_Energy_model.xlsx --recalc
    python packages/book/ipp_model.py --selftest

The workbook is built ONCE; after that it is the working surface (type the
new quarter into the blue rows, flip its status on Inputs). A rebuild
overwrites the PM's yellow cells, so `--out` refuses an existing file without
`--force`. Edits from chat go through `model_view.py --set`.
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
from genco_model import EV_HEAD, _rows, ev_block, ev_inputs  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]

# consolidated lines every actual quarter must carry, Rs crore
LINES = ("sales", "fuel", "employee", "other_exp", "depreciation", "other_income", "interest", "jv_share",
         "exceptional", "pbt", "tax", "minority", "pat")
# reported only at balance-sheet dates: typed where known
SPARSE = ("capex", "gross_debt", "cash", "net_worth")
BASES = ("yoy", "trailing4")
SEG = "Capacity & generation"


def crossfoot(cfg: dict, tol: float = 3.0) -> list[str]:
    """Every actual quarter must foot before anything is written: PBT from its
    components, PAT = PBT - tax - minority. A bucket table that does not cover
    the reported quarters, a capacity typed on a forecast quarter, weights that
    do not sum to one, or too little history for a year-ago PLF all refuse here,
    rather than becoming a plausible price."""
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
    n_act = qs.index(cfg["actual_through"]) + 1
    want = qs[:n_act]
    if acts != want:
        errs.append(f"consolidated quarters {acts} are not {want[0]}..{want[-1]}")
    if n_act < 4:
        errs.append("fewer than four reported quarters: a forecast's year-ago PLF would not exist")
    for k in SPARSE:
        for q in (cfg.get("sparse") or {}).get(k, {}):
            if q not in acts:
                errs.append(f"sparse.{k}: {q} is not an actual quarter")
    for q in acts:
        pbt = (c["sales"][q] - c["fuel"][q] - c["employee"][q] - c["other_exp"][q] - c["depreciation"][q]
               + c["other_income"][q] - c["interest"][q] + c["jv_share"][q] + c["exceptional"][q])
        if abs(pbt - c["pbt"][q]) > tol:
            errs.append(f"{q}: PBT components {pbt:,.1f} vs reported {c['pbt'][q]:,.1f}")
        pat = c["pbt"][q] - c["tax"][q] - c["minority"][q]
        if abs(pat - c["pat"][q]) > tol:
            errs.append(f"{q}: PBT - tax - minority {pat:,.1f} vs PAT {c['pat'][q]:,.1f}")
    keys = [b["key"] for b in cfg["buckets"]]
    if len(set(keys)) != len(keys) or "others" in keys:
        errs.append(f"bucket keys must be unique and not 'others': {keys}")
    for b in cfg["buckets"]:
        for k in ("mw", "gen_mu", "revenue", "ebitda_oi"):
            if set(b.get(k, {})) != set(acts):
                errs.append(f"bucket {b['key']}.{k}: quarters are not the reported quarters")
        if b.get("unit_basis") not in BASES:
            errs.append(f"bucket {b['key']}.unit_basis: one of {BASES}, got {b.get('unit_basis')!r}")
        if b.get("rev_basis", b.get("unit_basis")) not in BASES:
            errs.append(f"bucket {b['key']}.rev_basis: one of {BASES}, got {b.get('rev_basis')!r}")
        for fy in b.get("plan_mw", {}):
            if not fy.startswith("FY"):
                errs.append(f"bucket {b['key']}.plan_mw: {fy!r} is not a fiscal year")
    for k in ("weights_mw", "weights_capex"):
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
    cs, sp = cfg["consolidated"], cfg.get("sparse") or {}
    BK = cfg["buckets"]
    typed = lambda d: {q: d[q] for q in qs if q in act and q in d}
    wb = Workbook()
    wb.remove(wb.active)
    c_ = col

    def A(i):
        return f'Inputs!{col(i)}$6="A"'

    def rng(sheet, r):
        return f"'{sheet}'!$D${r}:${last}${r}"

    def prev4(sheet, r, i):
        return f"AVERAGE('{sheet}'!{col(max(0, i - 4))}{r}:{col(i - 1)}{r})"

    fyc = lambda i: f"Inputs!{col(i)}$5"

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
    ws.row_dimensions[8].height = 60
    r_qstart = inp.row("Quarter start", "date", {q: dt.date.fromisoformat(quarter_dates(q)[0]) for q in qs}, fmt="date")
    r_qend = inp.row("Quarter end", "date", {q: dt.date.fromisoformat(quarter_dates(q)[1]) for q in qs}, fmt="date")
    r_hours = inp.row("Hours in the quarter", "h",
                      lambda i, q, r: f"=({c_(i)}{r_qend}-{c_(i)}{r_qstart}+1)*24", fmt="t", kind="local")
    H = lambda i: f"Inputs!{c_(i)}${r_hours}"

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

    const("Shares_mn", "Shares outstanding (basic)", C["shares_mn"], "mn")
    const("CMP", "Current market price", C["cmp"], "rs")
    const("CMP_date", "Price date", C["cmp_date"], None)
    const("Tax_rate", "Effective tax rate on PBT", C["tax_rate"], "pct", yellow=True)
    const("Int_rate", "Interest rate on opening gross debt (P&L, after capitalised interest)", C["int_rate"], "pct", yellow=True)
    const("MI_share", "Minority share of profit after tax", C["mi_share"], "pct", yellow=True)
    const("DPS", "Dividend per share, paid in Q2 (Rs)", C["dps"], "rs", yellow=True)
    const("Dep_drift", "Depreciation-rate drift per quarter (Rs cr per MW per year)", C["dep_drift"], "w", yellow=True)
    const("Target_pe", "Target P/E multiple (the P/E column of Valuation reads this)", C["target_pe"], "x2", yellow=True)
    for b in BK:
        k = b["key"].capitalize()
        const(f"PLF_drift_{k}", f"{b['label']}: PLF drift on the year-ago quarter (pp)", b["plf_drift"], "pct", yellow=True)
        const(f"Unit_drift_{k}", f"{b['label']}: EBITDA-per-unit drift on its basis (Rs/kWh)", b["unit_drift"], "rs", yellow=True)

    inp.section(f"Opening balance sheet, {O['as_of']} (before the first quarter)")
    const("Open_debt", "Gross debt", O["gross_debt"], "cr")
    const("Open_cash", "Cash, bank and liquid investments (the company's net-debt basis)", O["cash"], "cr")
    const("Open_NW", "Net worth attributable to owners", O["net_worth"], "cr")
    for b in BK:
        const(f"Open_MW_{b['key'].capitalize()}", f"{b['label']}: operating capacity (MW)", b["open_mw"], "t")

    def plan(title, entries, nm_years, nm_vals, fmt="cr"):
        inp.section(title)
        first = inp.r
        for fy, e in entries.items():
            ws.cell(inp.r, 1, fy).font = Font(name=FONT)
            c = ws.cell(inp.r, 3, e["value"])
            c.font = Font(name=FONT, color=BLUE)
            c.fill = YELLOW
            c.number_format = FMT[fmt]
            ws.cell(inp.r, 4, e["source"]).font = Font(name=FONT, size=8, italic=True, color="595959")
            inp.r += 1
        name(wb, nm_years, f"Inputs!$A${first}:$A${inp.r - 1}")
        name(wb, nm_vals, f"Inputs!$C${first}:$C${inp.r - 1}")

    for b in BK:
        k = b["key"].capitalize()
        plan(f"{b['label']}: capacity ADDED per fiscal year (MW) - the driver", b["plan_mw"], f"MWYears_{k}", f"MWPlan_{k}", "t")
    plan("Capex plan, group (Rs crore per fiscal year)", P["capex"], "CapexYears", "CapexPlan")
    plan("Equity raised, forecast quarters only (Rs crore per fiscal year)", P["equity"], "EqYears", "EqPlan")

    inp.section("Quarter shapes: share of a year's plan in each quarter")
    wmw, wcpx = P["weights_mw"]["value"], P["weights_capex"]["value"]
    qn = lambda q: int(q[1]) - 1
    r_wmw = inp.row("Capacity-addition weight by quarter", "share", {q: wmw[qn(q)] for q in qs}, fmt="pct",
                    fill=lambda i, q: True)
    r_wcpx = inp.row("Capex and equity weight by quarter", "share", {q: wcpx[qn(q)] for q in qs}, fmt="pct",
                     fill=lambda i, q: True)
    inp.r += 1
    for lab, src in (("Capacity weight", P["weights_mw"]["source"]), ("Capex weight", P["weights_capex"]["source"])):
        ws.cell(inp.r, 1, f"{lab}: {src}").font = Font(name=FONT, size=8, italic=True, color="595959")
        inp.r += 1

    # ---------------------------------------------------------------- Capacity & generation
    SG = Sheet(wb, SEG, qs, f"{cfg['company']} - capacity, generation and EBITDA by bucket",
               "Reported rows typed (the company's capacity, net generation and entity-wise revenue and EBITDA incl. "
               "other income). Forecast: generation = average MW x hours x the year-ago PLF; EBITDA = generation x "
               "EBITDA per unit. Yellow rows override a forecast quarter. MW, MU (million kWh), Rs/kWh, Rs crore.")
    SG.header()
    refs = {}
    for b in BK:
        k = b["key"].capitalize()
        lab = b["label"]
        SG.section(f"{lab} - reported (typed)")
        r_mw_t = SG.row(f"{lab}: operating capacity, reported (closing)", "MW", typed(b["mw"]), fmt="t")
        r_gen_t = SG.row(f"{lab}: net generation, reported", "MU", typed(b["gen_mu"]), fmt="t")
        r_rev_t = SG.row(f"{lab}: revenue, reported", "Rs cr", typed(b["revenue"]))
        r_e_t = SG.row(f"{lab}: EBITDA incl. other income, reported", "Rs cr", typed(b["ebitda_oi"]))
        # a separate row, so the model row's SUMIFS never ranges over itself (a circular
        # reference in Excel even when the criteria exclude the cell; genco_model's finding)
        r_add_t = SG.row(f"{lab}: MW added, reported", "MW",
                         lambda i, q, r, rt=r_mw_t, k=k: f"=IF({A(i)},{c_(i)}{rt}-"
                         + (f"Open_MW_{k}" if i == 0 else f"{c_(i - 1)}{rt}") + ',"")', fmt="t", kind="local")
        SG.section(f"{lab} - the PM's overrides (yellow; blank = the model's own number)")
        r_plf_ov = SG.row(f"{lab}: PLF override", "%", {}, fmt="pct", fill=lambda i, q: q not in act)
        r_eu_ov = SG.row(f"{lab}: EBITDA per unit override", "Rs/kWh", {}, fmt="rs", fill=lambda i, q: q not in act)
        SG.section(f"{lab} - model")

        def add(i, q, r, rt=r_add_t, k=k):
            plan_cell = f"IFERROR(INDEX(MWPlan_{k},MATCH({fyc(i)},MWYears_{k},0)),0)"
            done = f'SUMIFS({rng(SEG, rt)},FYrow,{fyc(i)},Status,"A")'
            wsum = f'SUMIFS({rng("Inputs", r_wmw)},FYrow,{fyc(i)},Status,"F")'
            return f"=IF({A(i)},{c_(i)}{rt},IF({wsum}=0,0,({plan_cell}-{done})*Inputs!{c_(i)}{r_wmw}/{wsum}))"

        r_add = SG.row(f"{lab}: MW added", "MW", add, fmt="t",
                       comment="Forecast = (the fiscal year's plan on Inputs - the MW already reported that year) x this "
                               "quarter's weight / the weights of the year's forecast quarters.")
        r_open = SG.row(f"{lab}: operating capacity, opening", "MW",
                        lambda i, q, r, k=k: f"=Open_MW_{k}" if i == 0 else f"={c_(i - 1)}{r + 1}", fmt="t")
        r_mw = SG.row(f"{lab}: operating capacity, closing", "MW",
                      lambda i, q, r, rt=r_mw_t, ro=r_open, ra=r_add: f"=IF({A(i)},{c_(i)}{rt},{c_(i)}{ro}+{c_(i)}{ra})",
                      fmt="t", bold=True)
        r_avg = SG.row(f"{lab}: operating capacity, average", "MW",
                       lambda i, q, r, ro=r_open, rm=r_mw: f"=({c_(i)}{ro}+{c_(i)}{rm})/2", fmt="t")

        def plf(i, q, r, rg=r_gen_t, ra=r_avg, ov=r_plf_ov, k=k):
            meas = f"IF({c_(i)}{ra}=0,0,{c_(i)}{rg}*1000/({c_(i)}{ra}*{H(i)}))"
            base = f"{c_(i - 4)}{r}" if i >= 4 else "0"
            return f"=IF({A(i)},{meas},IF(ISNUMBER({c_(i)}{ov}),{c_(i)}{ov},{base}+PLF_drift_{k}))"

        r_plf = SG.row(f"{lab}: PLF", "%", plf, fmt="pct", fill=lambda i, q: q not in act,
                       comment="Reported quarter: net generation / (average MW x hours). Forecast: the SAME QUARTER A "
                               "YEAR EARLIER plus the drift on Inputs, unless the yellow override row carries a number.")
        r_gen = SG.row(f"{lab}: net generation", "MU",
                       lambda i, q, r, rg=r_gen_t, ra=r_avg, rp=r_plf: f"=IF({A(i)},{c_(i)}{rg},{c_(i)}{ra}*{H(i)}*{c_(i)}{rp}/1000)",
                       fmt="t", bold=True)
        # EBITDA per unit and revenue per unit may sit on different bases: renewables'
        # EBITDA per unit is seasonal (solar-heavy Q4 earns more a unit than windy Q2),
        # while one quarter's revenue per unit can carry a one-off (O2, Q4FY26: Rs5.39/kWh
        # on low-margin revenue) that the year-ago rule would repeat a year later.
        ubasis, rbasis = b["unit_basis"], b.get("rev_basis", b["unit_basis"])

        def per_unit(i, q, r, num, rgen, drift=None, ov=None, basis=None):
            meas = f"IF({c_(i)}{rgen}=0,0,{c_(i)}{num}*10/{c_(i)}{rgen})"
            if basis == "yoy":
                base = f"{c_(i - 4)}{r}" if i >= 4 else "0"
            else:
                base = prev4(SEG, r, i) if i else "0"
            fc = base + (f"+{drift}" if drift else "")
            if ov:
                fc = f"IF(ISNUMBER({c_(i)}{ov}),{c_(i)}{ov},{fc})"
            return f"=IF({A(i)},{meas},{fc})"

        r_ru = SG.row(f"{lab}: revenue per unit", "Rs/kWh",
                      lambda i, q, r, rr=r_rev_t, rg=r_gen_t, bs=rbasis: per_unit(i, q, r, rr, rg, basis=bs), fmt="rs",
                      comment=f"Reported quarter: revenue x 10 / generation. Forecast basis: {rbasis}"
                              + (f" ({b['rev_why']})." if b.get("rev_why") else "."))
        r_eu = SG.row(f"{lab}: EBITDA per unit (incl. other income)", "Rs/kWh",
                      lambda i, q, r, re=r_e_t, rg=r_gen_t, k=k, ov=r_eu_ov, bs=ubasis:
                      per_unit(i, q, r, re, rg, f"Unit_drift_{k}", ov, basis=bs),
                      fmt="rs", fill=lambda i, q: q not in act,
                      comment=f"Reported quarter: EBITDA incl. other income x 10 / generation. Forecast basis: {ubasis} "
                              f"({b.get('unit_why', '')}), plus the drift on Inputs, unless overridden.")
        r_rev = SG.row(f"{lab}: revenue", "Rs cr",
                       lambda i, q, r, rr=r_rev_t, rg=r_gen, ru=r_ru: f"=IF({A(i)},{c_(i)}{rr},{c_(i)}{rg}*{c_(i)}{ru}/10)")
        r_e = SG.row(f"{lab}: EBITDA incl. other income", "Rs cr",
                     lambda i, q, r, re=r_e_t, rg=r_gen, ru=r_eu: f"=IF({A(i)},{c_(i)}{re},{c_(i)}{rg}*{c_(i)}{ru}/10)",
                     bold=True)
        SG.row(f"{lab}: EBITDA margin", "%",
               lambda i, q, r, re=r_e, rr=r_rev: f"=IF({c_(i)}{rr}=0,0,{c_(i)}{re}/{c_(i)}{rr})", fmt="pct")
        refs[b["key"]] = {"mw": r_mw, "avg": r_avg, "gen": r_gen, "rev": r_rev, "e": r_e, "plf": r_plf}

    # Others: the plug on reported quarters, filled once the P&L rows exist
    SG.section("Others (transmission, storage, green hydrogen, unallocated, eliminations)")
    r_orev_ov = SG.row("Others: revenue override", "Rs cr", {}, fill=lambda i, q: q not in act)
    r_oe_ov = SG.row("Others: EBITDA incl. other income override", "Rs cr", {}, fill=lambda i, q: q not in act)
    r_orev = SG.row("Others: revenue (reported quarter: the plug to reported revenue)", "Rs cr", lambda i, q, r: None)
    r_oe = SG.row("Others: EBITDA incl. other income (reported quarter: the plug)", "Rs cr", lambda i, q, r: None, bold=True)
    SG.section("Totals")
    tsum = lambda key: (lambda i, q, r: "=" + "+".join(f"{c_(i)}{refs[b['key']][key]}" for b in BK))
    r_tmw = SG.row("Operating capacity, closing", "MW", tsum("mw"), fmt="t", bold=True)
    r_tavg = SG.row("Operating capacity, average", "MW", tsum("avg"), fmt="t")
    r_tgen = SG.row("Net generation", "MU", tsum("gen"), fmt="t", bold=True)
    r_trev = SG.row("Revenue", "Rs cr", lambda i, q, r: tsum("rev")(i, q, r) + f"+{c_(i)}{r_orev}", bold=True)
    r_te = SG.row("EBITDA incl. other income", "Rs cr", lambda i, q, r: tsum("e")(i, q, r) + f"+{c_(i)}{r_oe}", bold=True)
    SEGR = lambda r, i: f"'{SEG}'!{c_(i)}{r}"

    # ---------------------------------------------------------------- P&L
    PL = Sheet(wb, "P&L", qs, f"{cfg['company']} - consolidated P&L",
               "Reported rows typed; model rows read them while the status is A. EBITDA = the buckets' EBITDA incl. "
               "other income less other income. Rs crore.")
    PL.header()
    PL.section("Reported (typed)")
    r_sales_t = PL.row("Revenue from operations, reported", "Rs cr", typed(cs["sales"]))
    r_fuel_t = PL.row("Fuel and purchased power, reported", "Rs cr", typed(cs["fuel"]))
    r_emp_t = PL.row("Employee cost, reported", "Rs cr", typed(cs["employee"]))
    r_oth_t = PL.row("Other expenses, reported", "Rs cr", typed(cs["other_exp"]))
    r_dep_t = PL.row("Depreciation, reported", "Rs cr", typed(cs["depreciation"]))
    r_oi_t = PL.row("Other income, reported", "Rs cr", typed(cs["other_income"]))
    r_int_t = PL.row("Finance cost, reported", "Rs cr", typed(cs["interest"]))
    r_jv_t = PL.row("Share of associates and JVs, reported", "Rs cr", typed(cs["jv_share"]))
    r_xo_t = PL.row("Exceptional items, reported (pre-tax)", "Rs cr", typed(cs["exceptional"]))
    r_tax_t = PL.row("Tax, reported", "Rs cr", typed(cs["tax"]))
    r_mi_t = PL.row("Minority interest, reported", "Rs cr", typed(cs["minority"]))
    r_pat_t = PL.row("PAT attributable to owners, reported", "Rs cr", typed(cs["pat"]))
    PL.section("The PM's overrides (yellow; blank = the model's own number)")
    r_oi_ov = PL.row("Other income override", "Rs cr", {}, fill=lambda i, q: q not in act)
    PL.ws.cell(PL.r, 1, "Capacity is overridden on Inputs (the fiscal-year plan); PLF and EBITDA per unit on "
                        f"'{SEG}'.").font = Font(name=FONT, size=8, italic=True, color="595959")
    PL.r += 1
    PL.section("Model")
    r_rev = PL.row("Revenue from operations", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_sales_t},{SEGR(r_trev, i)})", bold=True)
    r_oi = PL.row("Other income", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_oi_t},IF(ISNUMBER({c_(i)}{r_oi_ov}),{c_(i)}{r_oi_ov},"
                                  + ("0" if i == 0 else prev4("P&L", r, i)) + "))")
    r_ebitda = PL.row("EBITDA", "Rs cr",
                      lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_sales_t}-{c_(i)}{r_fuel_t}-{c_(i)}{r_emp_t}-{c_(i)}{r_oth_t},"
                                      f"{SEGR(r_te, i)}-{c_(i)}{r_oi})", bold=True,
                      comment="Reported quarter: revenue less fuel and purchased power, employee and other expenses. "
                              "Forecast: the buckets' EBITDA incl. other income less consolidated other income.")
    PL.row("EBITDA margin", "%", lambda i, q, r: f"=IF({c_(i)}{r_rev}=0,0,{c_(i)}{r_ebitda}/{c_(i)}{r_rev})", fmt="pct")
    PL.row("EBITDA growth, year on year", "%",
           lambda i, q, r: None if i < 4 else f"={c_(i)}{r_ebitda}/{c_(i - 4)}{r_ebitda}-1", fmt="pct")
    r_depr = PL.row("Depreciation x 4 / average operating MW", "Rs cr/MW",
                    lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_dep_t}*4/{SEGR(r_tavg, i)},"
                                    + ("0" if i == 0 else f"{c_(i - 1)}{r}+Dep_drift") + ")", fmt="w",
                    comment="The latest reported quarter's rate per average operating MW, carried, plus Dep_drift "
                            "(Inputs). A trailing average of a rate that is moving puts the bias straight into EPS "
                            "(power_model.py's finding).")
    r_dep = PL.row("Depreciation", "Rs cr",
                   lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_dep_t},{SEGR(r_tavg, i)}*{c_(i)}{r_depr}/4)")
    r_int = PL.row("Finance cost", "Rs cr", lambda i, q, r: None)          # filled once the Cash flow rows exist
    r_jv = PL.row("Share of associates and JVs", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_jv_t}," + ("0" if i == 0 else prev4("P&L", r, i)) + ")")
    r_xo = PL.row("Exceptional items (pre-tax)", "Rs cr", lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_xo_t},0)")
    r_pbt = PL.row("PBT", "Rs cr",
                   lambda i, q, r: f"={c_(i)}{r_ebitda}-{c_(i)}{r_dep}+{c_(i)}{r_oi}-{c_(i)}{r_int}+{c_(i)}{r_jv}+{c_(i)}{r_xo}",
                   bold=True)
    r_tax = PL.row("Tax", "Rs cr", lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_tax_t},{c_(i)}{r_pbt}*Tax_rate)")
    PL.row("Tax rate", "%", lambda i, q, r: f"=IF({c_(i)}{r_pbt}=0,0,{c_(i)}{r_tax}/{c_(i)}{r_pbt})", fmt="pct")
    r_mi = PL.row("Minority interest", "Rs cr",
                  lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_mi_t},({c_(i)}{r_pbt}-{c_(i)}{r_tax})*MI_share)")
    r_pat = PL.row("PAT attributable to owners", "Rs cr",
                   lambda i, q, r: f"={c_(i)}{r_pbt}-{c_(i)}{r_tax}-{c_(i)}{r_mi}", bold=True)
    PL.row("Check: model PAT less reported (actual quarters)", "Rs cr",
           lambda i, q, r: f"=IF({A(i)},{c_(i)}{r_pat}-{c_(i)}{r_pat_t},\"\")")
    PL.row("Check: buckets' EBITDA incl. other income less (EBITDA + other income)", "Rs cr",
           lambda i, q, r: f"={SEGR(r_te, i)}-{c_(i)}{r_ebitda}-{c_(i)}{r_oi}",
           comment="Zero in every quarter: on a reported quarter Others is the plug, on a forecast EBITDA is defined "
                   "from the buckets.")
    r_eps = PL.row("EPS (Rs)", "Rs", lambda i, q, r: f"={c_(i)}{r_pat}*10/Shares_mn", fmt="rs", bold=True)
    D = "DATE(LEFT(CMP_date,4),MID(CMP_date,6,2),RIGHT(CMP_date,2))"
    r_w = PL.row("Weight of this quarter in the 12 months from the price date", "share",
                 lambda i, q, r: (f"=MAX(0,MIN(Inputs!{c_(i)}{r_qend},{D}+364)-MAX(Inputs!{c_(i)}{r_qstart},{D})+1)"
                                  f"/(Inputs!{c_(i)}{r_qend}-Inputs!{c_(i)}{r_qstart}+1)"), fmt="w")

    # the Others plug, now that the reported P&L rows exist
    for i, q in enumerate(qs):
        bsum_rev = "+".join(f"{c_(i)}{refs[b['key']]['rev']}" for b in BK)
        bsum_e = "+".join(f"{c_(i)}{refs[b['key']]['e']}" for b in BK)
        fr = (f"=IF({A(i)},'P&L'!{c_(i)}{r_sales_t}-({bsum_rev}),IF(ISNUMBER({c_(i)}{r_orev_ov}),{c_(i)}{r_orev_ov},"
              + ("0" if i == 0 else prev4(SEG, r_orev, i)) + "))")
        fe = (f"=IF({A(i)},'P&L'!{c_(i)}{r_sales_t}-'P&L'!{c_(i)}{r_fuel_t}-'P&L'!{c_(i)}{r_emp_t}-'P&L'!{c_(i)}{r_oth_t}"
              f"+'P&L'!{c_(i)}{r_oi_t}-({bsum_e}),IF(ISNUMBER({c_(i)}{r_oe_ov}),{c_(i)}{r_oe_ov},"
              + ("0" if i == 0 else prev4(SEG, r_oe, i)) + "))")
        for rr, f, bold in ((r_orev, fr, False), (r_oe, fe, True)):
            c = SG.ws.cell(rr, FIRST_COL + i, f)
            c.font = Font(name=FONT, color=GREEN, bold=bold)
            c.number_format = FMT["cr"]

    # ---------------------------------------------------------------- Cash flow
    CF = Sheet(wb, "Cash flow", qs, f"{cfg['company']} - cash, debt and net worth",
               "CFO = PAT + minority + depreciation (no working capital). Capex follows the fiscal-year plan. GROSS DEBT "
               "IS THE PLUG (capex + dividends - CFO - equity raised); cash holds at the last reported balance. Debt, "
               "cash and net worth are typed at balance-sheet dates and the roll resets to them. Rs crore.")
    CF.header()
    CF.section("Reported (typed; blank where no balance sheet was published)")
    r_capex_t = CF.row("Capex, reported (blank where the quarter is not disclosed)", "Rs cr", typed(sp.get("capex", {})))
    r_debt_t = CF.row("Gross debt, reported (closing)", "Rs cr", typed(sp.get("gross_debt", {})))
    r_cash_t = CF.row("Cash, bank and liquid investments, reported (closing)", "Rs cr", typed(sp.get("cash", {})))
    r_nw_t = CF.row("Net worth attributable to owners, reported (closing)", "Rs cr", typed(sp.get("net_worth", {})))
    r_weff = CF.row("Capex weight still to place (0 where the quarter is typed)", "share",
                    lambda i, q, r: f"=IF(ISNUMBER({c_(i)}{r_capex_t}),0,Inputs!{c_(i)}{r_wcpx})", fmt="pct")
    CF.section("Model")

    def capex(i, q, r):
        plan_cell = f"IFERROR(INDEX(CapexPlan,MATCH({fyc(i)},CapexYears,0)),0)"
        done = f"SUMIFS({rng('Cash flow', r_capex_t)},FYrow,{fyc(i)})"
        wsum = f"SUMIFS({rng('Cash flow', r_weff)},FYrow,{fyc(i)})"
        return (f"=IF(ISNUMBER({c_(i)}{r_capex_t}),{c_(i)}{r_capex_t},"
                f"IF({wsum}=0,0,({plan_cell}-{done})*{c_(i)}{r_weff}/{wsum}))")

    r_capex = CF.row("Capex", "Rs cr", capex, bold=True,
                     comment="A typed quarter is used as typed. Every other quarter takes its weight's share of the fiscal "
                             "year's plan on Inputs, less the quarters typed.")
    r_cfo = CF.row("Cash from operations (PAT + minority + depreciation)", "Rs cr",
                   lambda i, q, r: f"='P&L'!{c_(i)}{r_pat}+'P&L'!{c_(i)}{r_mi}+'P&L'!{c_(i)}{r_dep}")
    r_fcf = CF.row("Free cash flow", "Rs cr", lambda i, q, r: f"={c_(i)}{r_cfo}-{c_(i)}{r_capex}", bold=True)
    r_div = CF.row("Dividends paid", "Rs cr",
                   lambda i, q, r: f"=IF(LEFT(Inputs!{c_(i)}$4,2)=\"Q2\",DPS*Shares_mn/10,0)",
                   comment="DPS (Inputs) x shares, in Q2 after the AGM.")

    def equity(i, q, r):
        plan_cell = f"IFERROR(INDEX(EqPlan,MATCH({fyc(i)},EqYears,0)),0)"
        wsum = f'SUMIFS({rng("Inputs", r_wcpx)},FYrow,{fyc(i)},Status,"F")'
        return f"=IF({A(i)},0,IF({wsum}=0,0,{plan_cell}*Inputs!{c_(i)}{r_wcpx}/{wsum}))"

    r_eq = CF.row("Equity raised (forecast quarters; reported ones sit inside the typed balance sheet)", "Rs cr", equity)
    r_cash = CF.row("Cash, bank and liquid investments, closing (held at the last reported)", "Rs cr",
                    lambda i, q, r: f"=IF(ISNUMBER({c_(i)}{r_cash_t}),{c_(i)}{r_cash_t},"
                                    + ("Open_cash" if i == 0 else f"{c_(i - 1)}{r}") + ")")
    r_ddebt = CF.row("Change in gross debt (the plug)", "Rs cr",
                     lambda i, q, r: f"=IF(ISNUMBER({c_(i)}{r_debt_t}),{c_(i)}{r_debt_t}-"
                                     + ("Open_debt" if i == 0 else f"{c_(i - 1)}{r + 1}")
                                     + f",{c_(i)}{r_capex}+{c_(i)}{r_div}-{c_(i)}{r_cfo}-{c_(i)}{r_eq}+{c_(i)}{r_cash}-"
                                     + ("Open_cash" if i == 0 else f"{c_(i - 1)}{r_cash}") + ")",
                     comment="Typed debt where a balance sheet is published. Otherwise capex + dividends - CFO - equity "
                             "raised + the change in cash: the funding gap lands on debt, and so on interest.")
    r_debt = CF.row("Gross debt, closing", "Rs cr",
                    lambda i, q, r: "=" + ("Open_debt" if i == 0 else f"{c_(i - 1)}{r}") + f"+{c_(i)}{r_ddebt}", bold=True)
    r_nd = CF.row("Net debt, closing", "Rs cr", lambda i, q, r: f"={c_(i)}{r_debt}-{c_(i)}{r_cash}", bold=True)
    r_nw = CF.row("Net worth attributable to owners, closing", "Rs cr",
                  lambda i, q, r: f"=IF(ISNUMBER({c_(i)}{r_nw_t}),{c_(i)}{r_nw_t},"
                                  + ("Open_NW" if i == 0 else f"{c_(i - 1)}{r}")
                                  + f"+'P&L'!{c_(i)}{r_pat}-{c_(i)}{r_div}+{c_(i)}{r_eq})", bold=True)
    CF.row("Net debt / annualised EBITDA", "x",
           lambda i, q, r: f"=IF('P&L'!{c_(i)}{r_ebitda}=0,0,{c_(i)}{r_nd}/('P&L'!{c_(i)}{r_ebitda}*4))", fmt="x2")
    CF.row("Gross debt / net worth", "x", lambda i, q, r: f"={c_(i)}{r_debt}/{c_(i)}{r_nw}", fmt="x2")
    CF.row("Book value per share (Rs)", "Rs", lambda i, q, r: f"={c_(i)}{r_nw}*10/Shares_mn", fmt="rs")

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
    an["A2"] = "Sums of the quarters; a year with any forecast quarter is marked F."
    an["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    an.column_dimensions["A"].width = 52
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
    arows = []
    for b in BK:
        arows.append((f"{b['label']}: capacity, year end (MW)", lambda L, r=refs[b['key']]['mw']: f"={yend(SEG, r, L + '$4')}", "t"))
    arows += [("Operating capacity, year end (MW)", lambda L: f"={yend(SEG, r_tmw, L + '$4')}", "t")]
    for b in BK:
        arows.append((f"{b['label']}: net generation (MU)", sm(SEG, refs[b['key']]['gen']), "t"))
    arows += [("Net generation (MU)", sm(SEG, r_tgen), "t")]
    for b in BK:
        arows.append((f"{b['label']}: EBITDA incl. other income (Rs cr)", sm(SEG, refs[b['key']]['e']), "cr"))
    arows += [
        ("Others: EBITDA incl. other income (Rs cr)", sm(SEG, r_oe), "cr"),
        ("Revenue from operations (Rs cr)", sm("P&L", r_rev), "cr"),
        ("EBITDA (Rs cr)", sm("P&L", r_ebitda), "cr"),
        ("Other income (Rs cr)", sm("P&L", r_oi), "cr"),
        ("Depreciation (Rs cr)", sm("P&L", r_dep), "cr"),
        ("Finance cost (Rs cr)", sm("P&L", r_int), "cr"),
        ("PAT attributable to owners (Rs cr)", sm("P&L", r_pat), "cr"),
        ("EPS (Rs)", sm("P&L", r_eps), "rs"),
        ("Capex (Rs cr)", sm("Cash flow", r_capex), "cr"),
        ("Free cash flow (Rs cr)", sm("Cash flow", r_fcf), "cr"),
        ("Gross debt, year end (Rs cr)", lambda L: f"={yend('Cash flow', r_debt, L + '$4')}", "cr"),
        ("Net debt, year end (Rs cr)", lambda L: f"={yend('Cash flow', r_nd, L + '$4')}", "cr"),
        ("Net debt / EBITDA", lambda L: f"={yend('Cash flow', r_nd, L + '$4')}/SUMIFS({rng('P&L', r_ebitda)},FYrow,{L}$4)", "x2"),
        ("Net worth, year end (Rs cr)", lambda L: f"={yend('Cash flow', r_nw, L + '$4')}", "cr"),
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
    va["A1"] = f"{cfg['company']} - the valuation year's earnings to an expected price"
    va["A1"].font = Font(name=FONT, bold=True, size=13)
    va["A2"] = ("For the PM's eye. The system's juice comes from case_brief.py (median house-implied multiple on street "
                "EBITDA, sigma range); the model's earnings are the cross-check.")
    va["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")
    street = cfg.get("street") or {}
    vfy = cfg.get("valuation_fy", "FY28")
    vrows = [
        ("Valuation basis", "P/E", None, "inp", "Read by the Analysis card (model_view): the column headed with this word."),
        ("Valuation year", vfy, None, "inp", "Sum of that year's four quarters."),
        ("Quarters in the valuation year", "=COUNTIFS(FYrow,B5)", None, "f", "Must read 4."),
        ("PAT, valuation year (Rs cr)", f"=SUMIFS({rng('P&L', r_pat)},FYrow,B5)", "cr", "f", ""),
        ("EPS of the valuation year (Rs)", "=B7*10/Shares_mn", "rs", "f", ""),
        ("P/E today, on the valuation year", "=CMP/B8", "x2", "f", ""),
        ("Target P/E multiple (x)", "=Target_pe", "x2", "key",
         "Inputs!Target_pe: the median of the houses' implied P/E (target / consensus EPS of the valuation year)."),
        ("Expected price (Rs)", "=B8*B10", "rs", "f", ""),
        ("Upside vs CMP", "=B11/CMP-1", "pct", "f", ""),
        ("Dividend per share in the valuation year (Rs)", f"=SUMIFS({rng('Cash flow', r_div)},FYrow,B5)*10/Shares_mn", "rs", "f", ""),
        ("Total return vs CMP, price plus that dividend", "=(B11+B13)/CMP-1", "pct", "f", ""),
        ("", None, None, "h", ""),
        ("Rolling four quarters forward from the price date", None, None, "h",
         "EPS of the 12 months starting on the price date, each quarter weighted by the share of its days inside the "
         "window (the P&L's weight row), at the same target P/E."),
        ("Price date", f"={D}", "date", "f", ""),
        ("EPS, next four quarters from the price date (Rs)", f"=SUMPRODUCT({rng('P&L', r_w)},{rng('P&L', r_eps)})", "rs", "f", ""),
        ("Expected price, rolling 12 months forward (Rs)", "=B18*B10", "rs", "f", ""),
        ("Upside vs CMP, rolling", "=B19/CMP-1", "pct", "f", ""),
    ]
    for k, (lab, v, fmt, kind, note) in enumerate(vrows):
        r = 4 + k
        bold = lab in ("Expected price (Rs)", "Upside vs CMP") or kind == "h"
        va.cell(r, 1, lab).font = Font(name=FONT, bold=bold)
        if v is not None:
            c = va.cell(r, 2, v)
            is_f = isinstance(v, str) and v.startswith("=")
            linked = is_f and any(t in v for t in ("!", "FYrow", "Qrow", "Status", "Shares_mn", "CMP", "Target_"))
            c.font = Font(name=FONT, color=(GREEN if linked else BLACK) if is_f else BLUE, bold=bold)
            if fmt:
                c.number_format = FMT[fmt]
            if kind in ("inp", "key"):
                c.fill = YELLOW
        if note:
            va.cell(r, 5, note).font = Font(name=FONT, size=8, italic=True, color="595959")
    stb = 4 + len(vrows) + 1
    fy_list = [fy for fy in ("FY27", "FY28", "FY29") if fy in {fy_of(q) for q in qs}]
    for blk, (key, unit, fmt, rowref) in enumerate((("eps", "Rs", "rs", r_eps), ("ebitda_cr", "Rs cr", "cr", r_ebitda))):
        word = "EPS" if key == "eps" else "EBITDA"
        top = stb + blk * (len(fy_list) + 2)
        for j, h in enumerate((f"Street check: consensus {word} (type in) against the model", "Street", "Model",
                               "Model vs street")):
            va.cell(top, 1 + j, h).font = Font(name=FONT, bold=True)
        for k, fy in enumerate(fy_list):
            r = top + 1 + k
            va.cell(r, 1, f"Consensus {word} {fy} ({unit})").font = Font(name=FONT)
            s = (street.get(fy) or {})
            c = va.cell(r, 2, s.get(key))
            c.fill = YELLOW
            c.font = Font(name=FONT, color=BLUE)
            c.number_format = FMT[fmt]
            c = va.cell(r, 3, f"=SUMIFS({rng('P&L', rowref)},FYrow,\"{fy}\")")
            c.font = Font(name=FONT, color=GREEN)
            c.number_format = FMT[fmt]
            c = va.cell(r, 4, f'=IF(ISNUMBER(B{r}),C{r}/B{r}-1,"")')
            c.number_format = FMT["pct"]
            if s.get("source"):
                va.cell(r, 5, s["source"]).font = Font(name=FONT, size=8, italic=True, color="595959")

    bj = out.parent / "build.json"
    evi = ev_inputs(cfg if cfg.get("house_tps") else
                    json.loads(bj.read_text(encoding="utf-8")) if bj.exists() else None)
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
    for rr, item in enumerate(cfg.get("sources", []), start=2):
        for j, v in enumerate(item):
            c = so.cell(rr, 1 + j, v)
            c.font = Font(name=FONT, size=9)
            c.alignment = Alignment(wrap_text=True, vertical="top")

    wb.move_sheet("README", offset=-wb.index(wb["README"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return {"quarters": N, "actual": n_act}


def readme_lines(cfg, qs, n_act):
    bk = ", ".join(f"{b['label'].lower()} ({b['unit_basis']}"
                   + (f"; revenue per unit {b['rev_basis']}" if b.get("rev_basis", b["unit_basis"]) != b["unit_basis"] else "")
                   + ")" for b in cfg["buckets"])
    return [
        (f"{cfg['company']} - quarterly model", "t"),
        (f"Built {cfg['built']} by packages/book/ipp_model.py from {cfg['entity']}/model_config.json. "
         f"Actuals {qs[0]} to {qs[n_act - 1]}; forecasts to {qs[-1]}. Consolidated, Rs crore.", ""),
        ("", ""),
        ("How it flows", "h"),
        ("Inputs (timeline, constants, each bucket's capacity-addition plan, the capex and equity plans, quarter shapes) "
         f"-> {SEG} (per bucket: MW added, operating MW, PLF, net generation, revenue and EBITDA per unit; Others; "
         "totals) -> P&L (EBITDA = the buckets less other income; depreciation per MW; interest on opening debt) -> "
         "Cash flow (CFO, capex, dividends, debt as the plug, cash, net worth) -> Annual -> Valuation (P/E in column B, "
         "EV/EBITDA and the houses' targets in column C).", ""),
        ("", ""),
        ("Colours", "h"),
        ("Blue = typed. Black = formula. Green = link to another sheet. Yellow = a key assumption or the PM's override. "
         "Status row: A = reported quarter, F = forecast.", ""),
        ("", ""),
        ("Rolling a new quarter", "h"),
        (f"Type the quarter's reported lines into the blue 'Reported' blocks on {SEG} (capacity, generation, the "
         "entity-wise revenue and EBITDA incl. other income from the results presentation), P&L and, at a balance-sheet "
         "date, Cash flow; then flip its status on Inputs row 6 from F to A. Every model row switches to the reported "
         "value and the next year's same quarter re-bases on it.", "yellow"),
        ("", ""),
        ("What the PM overrides", "h"),
        ("The capacity-addition plan per bucket and fiscal year (Inputs) is the lever that moves earnings. PLF and "
         f"EBITDA per unit have yellow override rows per bucket on {SEG} for a visible-quarter view; Others and other "
         "income have their own. The drifts on Inputs (PLF pp, EBITDA per unit Rs/kWh) are zero until there is a view.", ""),
        ("", ""),
        ("The forecast rules, and why", "h"),
        ("Generation = average MW x hours x the SAME QUARTER A YEAR EARLIER's PLF: hydro and wind are seasonal and a "
         f"trailing average would put the monsoon into the winter. EBITDA per unit by bucket: {bk}. "
         + (cfg.get("unit_note") or ""), ""),
        ("", ""),
        ("What the model leaves out, on purpose", "h"),
        (cfg.get("leaves_out") or
         "Tariffs plant by plant and contract by contract, merchant prices, fuel costs: each bucket's EBITDA per unit "
         "carries them, measured on reported quarters. Working capital. Interest capitalised on the construction book "
         "beyond what the P&L rate already nets. Acquisitions not yet announced.", ""),
        ("", ""),
        ("Where the actuals come from", "h"),
        (cfg.get("readme_history") or "The Sources sheet names the document for every line.", ""),
    ]


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------
def _fixture() -> dict:
    qs = ["Q1FY26", "Q2FY26", "Q3FY26", "Q4FY26"]
    e = lambda v, u="", s="t": {"value": v, "unit": u, "source": s}
    lines = dict(sales=(5000, 5200, 4000, 4500), fuel=(1500, 1400, 1200, 1300), employee=(150, 160, 160, 170),
                 other_exp=(550, 560, 570, 600), depreciation=(700, 750, 780, 800), other_income=(200, 180, 150, 300),
                 interest=(1300, 1400, 1450, 1500), jv_share=(3, 2, 3, 2), exceptional=(0, 0, -60, 0))
    cons = {k: dict(zip(qs, v)) for k, v in lines.items()}
    pbt = {q: cons["sales"][q] - cons["fuel"][q] - cons["employee"][q] - cons["other_exp"][q] - cons["depreciation"][q]
           + cons["other_income"][q] - cons["interest"][q] + cons["jv_share"][q] + cons["exceptional"][q] for q in qs}
    tax = {q: round(pbt[q] * 0.2, 1) for q in qs}
    mi = {q: round((pbt[q] - tax[q]) * 0.1, 1) for q in qs}
    cons.update(pbt=pbt, tax=tax, minority=mi, pat={q: pbt[q] - tax[q] - mi[q] for q in qs})
    mk = lambda key, lab, basis, mw, gen, rev, eb, plan: {
        "key": key, "label": lab, "unit_basis": basis, "unit_why": "t", "open_mw": e(mw[0] - 10),
        "plf_drift": e(0.0), "unit_drift": e(0.0),
        "mw": dict(zip(qs, mw)), "gen_mu": dict(zip(qs, gen)), "revenue": dict(zip(qs, rev)),
        "ebitda_oi": dict(zip(qs, eb)), "plan_mw": {"FY26": e(mw[-1] - mw[0] + 10), "FY27": e(plan)}}
    return {"company": "Fixture IPP", "entity": "fixture_ipp", "built": "2026-11-25",
            "first_quarter": "Q1FY26", "last_quarter": "Q4FY27", "actual_through": "Q4FY26", "valuation_fy": "FY27",
            "basis": {q: "t" for q in qs},
            "buckets": [
                mk("thermal", "Thermal", "trailing4", [5000, 5000, 5000, 5000], [7500, 6800, 6900, 7800],
                   [3200, 2900, 2600, 3000], [1500, 1350, 1200, 1700], 600),
                mk("hydro", "Hydro", "yoy", [1500, 1500, 1500, 1500], [1900, 3000, 800, 450],
                   [350, 480, 200, 180], [330, 460, 170, 140], 0),
                {**mk("renewables", "Renewables", "yoy", [4000, 4200, 4400, 4600], [2600, 3100, 1500, 1700],
                      [1000, 1200, 600, 700], [900, 1100, 480, 560], 2000), "rev_basis": "trailing4"}],
            "consolidated": cons,
            "sparse": {"gross_debt": {"Q4FY26": 70000.0}, "cash": {"Q4FY26": 6000.0}, "net_worth": {"Q4FY26": 31000.0},
                       "capex": {"Q2FY26": 9000.0}},
            "constants": {"shares_mn": e(1800.0), "cmp": e(500.0), "cmp_date": e("2026-11-24"), "tax_rate": e(0.2),
                          "int_rate": e(0.08), "mi_share": e(0.1), "dps": e(2.0), "dep_drift": e(0.0),
                          "target_pe": e(30.0)},
            "opening": {"as_of": "31-Mar-2025", "gross_debt": e(60000), "cash": e(5000), "net_worth": e(28000)},
            "plans": {"capex": {"FY26": e(25000), "FY27": e(20000)}, "equity": {"FY27": e(1000)},
                      "weights_mw": e([0.25, 0.25, 0.25, 0.25]), "weights_capex": e([0.2, 0.25, 0.25, 0.3])},
            "street": {"FY27": {"eps": 15.0, "ebitda_cr": 13000.0, "source": "t"}},
            "house_tps": [{"house": "C", "tp": 600, "date": "2026-11-02", "basis": "t"}, {"house": "A", "tp": 500},
                          {"house": "B", "tp": 540}],
            "sources": [["a", "b", "c"]]}


def selftest() -> int:
    fails = 0

    def chk(label, got, want=True):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))

    cp = lambda o: json.loads(json.dumps(o))
    print("ipp_model selftest")
    fx = _fixture()
    chk("footed fixture accepted", crossfoot(fx), [])
    bad = cp(fx); bad["consolidated"]["tax"]["Q1FY26"] += 9                     # a mistyped tax
    chk("a mistyped tax refused (PAT no longer foots)", len(crossfoot(bad)), 1)
    bad = cp(fx); bad["consolidated"]["fuel"]["Q2FY26"] = 1410
    chk("a mistyped fuel line refused (PBT no longer foots)", len(crossfoot(bad)), 1)
    bad = cp(fx); bad["plans"]["weights_mw"]["value"] = [0.25, 0.25, 0.25, 0.2]
    chk("weights that do not sum to one refused", len(crossfoot(bad)), 1)
    bad = cp(fx); bad["sparse"]["gross_debt"]["Q1FY27"] = 1.0
    chk("a balance-sheet figure typed on a forecast quarter refused", len(crossfoot(bad)), 1)
    bad = cp(fx); del bad["buckets"][1]["gen_mu"]["Q3FY26"]
    chk("a bucket missing a reported quarter refused", len(crossfoot(bad)), 1)
    bad = cp(fx); bad["buckets"][2]["unit_basis"] = "last"
    chk("an unknown per-unit basis refused", len(crossfoot(bad)), 1)
    bad = cp(fx); bad["actual_through"] = "Q3FY26"
    for k in list(bad["consolidated"]):
        bad["consolidated"][k].pop("Q4FY26")
    for b in bad["buckets"]:
        for k in ("mw", "gen_mu", "revenue", "ebitda_oi"):
            b[k].pop("Q4FY26")
    bad["sparse"] = {}
    chk("fewer than four reported quarters refused (no year-ago PLF)", len(crossfoot(bad)), 1)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ipp_selftest_"))
    p0 = tmp / "Fixture_model.xlsx"
    info = build(fx, p0)
    chk("built: quarters, actual", (info["quarters"], info["actual"]), (8, 4))
    wb = load_workbook(p0)
    chk("sheets", wb.sheetnames, ["README", "Inputs", SEG, "P&L", "Cash flow", "Annual", "Valuation", "Sources"])
    chk("CMP, CMP_date and the plans are workbook names (model_view.set_cmp writes CMP)",
        all(n in wb.defined_names for n in ("CMP", "CMP_date", "Target_pe", "MWPlan_Renewables", "CapexPlan")))
    import model_view
    vr = model_view._val_rows(wb["Valuation"])
    chk("model_view finds the target, basis, year, EPS and typed P/E rows by label",
        sorted(vr), ["basis", "eps", "pe_typed", "tp", "year"])
    chk("without an EV/EBITDA build the card reads column B (P/E)", model_view._val_col(wb["Valuation"], vr), 2)
    evfx = cp(fx); evfx.update(valuation="ev_ebitda", net_debt_cr=64000.0, net_debt_asof="Sep-26")
    pe_ = tmp / "Ev_model.xlsx"
    build(evfx, pe_)
    ve = load_workbook(pe_)["Valuation"]
    chk("an EV/EBITDA case types the basis and its own net debt, and the card reader takes column C",
        (ve.cell(vr["basis"], 2).value, ve.cell(vr["eps"], 3).value, model_view._val_col(ve, vr)),
        ("EV/EBITDA", 64000.0, 3))
    sg = wb[SEG]; R = _rows(sg)
    self_ref = [c.coordinate for name_ in ("Thermal: MW added", "Renewables: MW added") for c in sg[R[name_]]
                if isinstance(c.value, str) and f"${R[name_]}:" in c.value]
    chk("the MW-added rows' SUMIFS never range over themselves (no circular reference)", self_ref, [])
    plf_f = sg.cell(R["Hydro: PLF"], FIRST_COL + 6).value           # Q3FY27, a forecast
    chk("a forecast PLF reads the SAME quarter a year earlier (Q3FY27 -> Q3FY26, column F)",
        ",F" in plf_f and "+PLF_drift_Hydro" in plf_f, True)
    eu_f = sg.cell(R["Renewables: EBITDA per unit (incl. other income)"], FIRST_COL + 6).value
    ru_f = sg.cell(R["Renewables: revenue per unit"], FIRST_COL + 6).value
    chk("EBITDA and revenue per unit keep their own bases (renewables: EBITDA yoy, revenue trailing4)",
        ("AVERAGE" not in eu_f and f",F{R['Renewables: EBITDA per unit (incl. other income)']}+" in eu_f,
         "AVERAGE(" in ru_f), (True, True))
    bad = cp(fx); bad["buckets"][2]["rev_basis"] = "median"
    chk("an unknown revenue-per-unit basis refused", len(crossfoot(bad)), 1)

    try:
        fx2 = cp(fx); fx2["buckets"][1]["plf_drift"]["value"] = 0.05            # +5pp hydro PLF
        p1 = tmp / "Fixture_plf_model.xlsx"
        build(fx2, p1)
        recalc(p0.resolve()); recalc(p1.resolve())
    except Exception as ex:            # no Excel: the structural checks above still stand
        print(f"  skip Excel recalculation ({type(ex).__name__}): numbers not checked")
        print(f"ipp_model selftest: {'FAILED ' + str(fails) if fails else 'all checks passed'}")
        return 1 if fails else 0
    chk("no error cells after recalculation", errors(p0), [])
    v0, v1 = load_workbook(p0, data_only=True), load_workbook(p1, data_only=True)
    val = lambda w, sheet, label, i: w[sheet].cell(_rows(w[sheet])[label], FIRST_COL + i).value
    chk("model PAT equals reported on every actual quarter",
        [round(val(v0, "P&L", "Check: model PAT less reported (actual quarters)", i), 6) for i in range(4)], [0] * 4)
    chk("the buckets add back to EBITDA + other income in every quarter",
        [round(val(v0, "P&L", "Check: buckets' EBITDA incl. other income less (EBITDA + other income)", i), 6)
         for i in range(8)], [0] * 8)
    fy27 = sum(val(v0, SEG, "Renewables: MW added", i) for i in range(4, 8))
    chk("a year's MW additions sum to its plan", round(fy27, 6), 2000.0)
    i = 6
    avg, hrs = val(v0, SEG, "Hydro: operating capacity, average", i), val(v0, "Inputs", "Hours in the quarter", i)
    chk("forecast generation = average MW x hours x the year-ago PLF",
        round(val(v0, SEG, "Hydro: net generation", i), 6),
        round(avg * hrs * val(v0, SEG, "Hydro: PLF", 2) / 1000, 6))
    chk("forecast EBITDA per unit on a trailing4 basis = the mean of the four quarters before it",
        round(val(v0, SEG, "Thermal: EBITDA per unit (incl. other income)", 4), 9),
        round(sum(val(v0, SEG, "Thermal: EBITDA per unit (incl. other income)", k) for k in range(4)) / 4, 9))
    chk("a +5pp hydro PLF drift lifts the forecast hydro PLF by exactly 5pp",
        round(val(v1, SEG, "Hydro: PLF", 6) - val(v0, SEG, "Hydro: PLF", 6), 9), 0.05)
    g0 = val(v0, SEG, "Hydro: net generation", 6)
    chk("...and moves hydro generation, and EBITDA with it",
        val(v1, SEG, "Hydro: net generation", 6) > g0 and
        val(v1, "P&L", "EBITDA", 6) > val(v0, "P&L", "EBITDA", 6), True)
    cf = lambda lab, k: val(v0, "Cash flow", lab, k)
    k = 5
    plug = (cf("Capex", k) + cf("Dividends paid", k) - cf("Cash from operations (PAT + minority + depreciation)", k)
            - cf("Equity raised (forecast quarters; reported ones sit inside the typed balance sheet)", k))
    chk("gross debt is the plug: change = capex + dividends - CFO - equity, cash held",
        (round(cf("Change in gross debt (the plug)", k), 6), cf("Cash, bank and liquid investments, closing (held at the last reported)", k)),
        (round(plug, 6), 6000.0))
    chk("interest = opening gross debt x the rate / 4",
        round(val(v0, "P&L", "Finance cost", 5), 6), round(cf("Gross debt, closing", 4) * 0.08 / 4, 6))
    chk("the typed balance sheet stands on its quarter",
        (cf("Gross debt, closing", 3), cf("Net worth attributable to owners, closing", 3)), (70000.0, 31000.0))
    V = _rows(v0["Valuation"])
    eps27 = sum(val(v0, "P&L", "EPS (Rs)", k) for k in range(4, 8))
    chk("expected price on P/E = the valuation year's EPS x the target P/E",
        round(v0["Valuation"].cell(V["Expected price (Rs)"], 2).value, 6), round(eps27 * 30.0, 6))
    chk("the median EV/EBITDA multiple round-trips to the median target",
        round(v0["Valuation"].cell(V["Expected price (Rs)"], 3).value, 6), 540.0)
    print(f"ipp_model selftest: {'FAILED ' + str(fails) if fails else 'all checks passed'}")
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
