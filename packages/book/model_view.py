"""Per-stock Excel models on the Analysis tab: find, render, open, edit.

PM, 2026-11-02, after the JSW steel model was built: *"can we have a function
of sorts where we get the live excel on the analysis tab beside each company?
I want to open and view it at whim, ask you here to edit it and it updates
there."* So three paths, one file:

- VIEW. The Analysis card renders the workbook's SAVED values with its own
  fonts, fills and number formats, sheet by sheet, and polls the file's
  mtime while open, so a save from anywhere (Excel, or an edit from chat)
  re-renders without a reload. It reads what Excel cached at the last save.
  A workbook written by openpyxl and never opened in Excel has no cached
  values; that is reported, never rendered as blanks.
- OPEN. `open_in_excel()` hands the file to Windows (`os.startfile`), so the
  button on the page opens the real workbook in desktop Excel. The server
  binds 127.0.0.1, so this only ever opens a file on the PM's own machine,
  and only a file this module found under data/analysis/cases/.
- EDIT (from chat). `--set` drives Excel over COM. If the workbook is already
  open in the PM's Excel, the edit goes INTO that open window and is saved
  there, so the PM watches it change and no second copy exists to conflict
  with. Otherwise Excel opens it hidden, edits, recalculates, saves, closes.
  Never openpyxl for an edit: it would drop every cached value, and with the
  file open in Excel the PM's next save would silently overwrite the edit.

A model is any `data/analysis/cases/<entity>/*_model.xlsx` (gitignored, like
every case file). Adding one is a file, never a code change.

    python packages/book/model_view.py                     # list models
    python packages/book/model_view.py --get jsw_steel "Valuation!B21"
    python packages/book/model_view.py --set jsw_steel "Segments!L18=7.3" "Valuation!B15=7.5"
    python packages/book/model_view.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
CASES = REPO / "data" / "analysis" / "cases"
ENTITY_RE = re.compile(r"^[a-z0-9_]+$")
MAX_ROWS, MAX_COLS = 400, 40          # a case model, not a data dump

_cache: dict = {}                     # (path, mtime) -> parsed workbook


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def models(base: pathlib.Path = CASES) -> dict:
    """entity -> {file, mtime, open}. One model per entity: the newest."""
    out = {}
    for p in sorted(base.glob("*/*_model.xlsx")):
        if p.name.startswith("~$"):
            continue
        ent = p.parent.name
        st = p.stat()
        if ent in out and out[ent]["mtime"] >= st.st_mtime:
            continue
        out[ent] = {"file": p.name, "mtime": st.st_mtime,
                    "saved": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    "open": is_open(p)}
    return out


def path_for(entity: str, base: pathlib.Path = CASES) -> pathlib.Path | None:
    """The model file for an entity, or None. Refuses anything that is not a
    plain entity id, so a request can never walk out of the cases folder."""
    if not entity or not ENTITY_RE.match(entity):
        return None
    m = models(base).get(entity)
    return base / entity / m["file"] if m else None


def is_open(p: pathlib.Path) -> bool:
    """Excel keeps an owner file `~$<name>` beside a workbook it has open."""
    return (p.parent / ("~$" + p.name[2:])).exists() or (p.parent / ("~$" + p.name)).exists()


# --------------------------------------------------------------------------
# number formats: enough of Excel's grammar for what a case model uses
# --------------------------------------------------------------------------
def _sections(fmt: str) -> list[str]:
    out, cur, q = [], "", False
    for ch in fmt:
        if ch == '"':
            q = not q
        if ch == ";" and not q:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


def fmt_value(v, fmt: str | None) -> str:
    """Render a cached value the way Excel would show it, for the formats the
    case models use: thousands separators, decimals, %, parentheses for
    negatives, a quoted zero section ("-"), quoted prefixes and suffixes."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%d-%b-%Y")
    if not isinstance(v, (int, float)):
        return str(v)
    if not fmt or fmt == "General":
        if float(v).is_integer():
            return f"{int(v):,}" if abs(v) >= 10000 else str(int(v))
        return f"{v:,.2f}" if abs(v) >= 1000 else f"{v:.4g}"
    # Excel re-saves '(#,##0)' as '\(#,##0\)': a backslash escapes ONE
    # literal character. Quote it so the rest of the parser sees a literal.
    fmt = re.sub(r"\\(.)", r'"\1"', fmt)
    secs = _sections(fmt)
    if v > 0 or len(secs) == 1:
        sec, x = secs[0], v
    elif v < 0:
        sec, x = (secs[1], -v) if len(secs) > 1 else (secs[0], v)
    else:
        sec, x = (secs[2] if len(secs) > 2 else secs[0]), v
    lit = re.findall(r'"([^"]*)"', sec)
    core = re.sub(r'"[^"]*"', "", sec)
    if not re.search(r"[0#]", core):          # a pure-literal section, e.g. "-"
        return "".join(lit)
    pct = "%" in core
    if pct:
        x *= 100
    m = re.search(r"\.([0#]+)", core)
    dec = len(m.group(1)) if m else 0
    num = f"{x:,.{dec}f}" if "," in core else f"{x:.{dec}f}"
    head = re.match(r'^([^0#,.]*)', sec)
    pre = re.sub(r'"', "", head.group(1)) if head else ""
    tail = re.search(r'([^0#,.%]*)$', sec)
    post = re.sub(r'"', "", tail.group(1)) if tail else ""
    return f"{pre}{num}{'%' if pct else ''}{post}"


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------
def _hex(color) -> str | None:
    try:
        if color is None or color.type != "rgb" or not isinstance(color.rgb, str):
            return None
        return color.rgb[-6:]
    except Exception:
        return None


def _load(p: pathlib.Path):
    from openpyxl import load_workbook
    key = (str(p), p.stat().st_mtime)
    if key in _cache:
        return _cache[key]
    vals = load_workbook(p, data_only=True)
    forms = load_workbook(p, data_only=False)
    _cache.clear()                            # one model at a time is plenty
    _cache[key] = (vals, forms)
    return _cache[key]


def render(entity: str, sheet: str | None = None, since: float | None = None,
           base: pathlib.Path = CASES) -> dict:
    p = path_for(entity, base)
    if p is None:
        return {"error": f"no model for {entity!r}"}
    mtime = p.stat().st_mtime
    head = {"entity": entity, "file": p.name, "mtime": mtime,
            "saved": dt.datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
            "open": is_open(p)}
    if since is not None and abs(since - mtime) < 1e-6:
        return {**head, "unchanged": True}
    vals, forms = _load(p)
    names = vals.sheetnames
    sh = sheet if sheet in names else names[0]
    wv, wf = vals[sh], forms[sh]
    nr, nc = min(wv.max_row, MAX_ROWS), min(wv.max_column, MAX_COLS)
    widths = []
    from openpyxl.utils import get_column_letter
    for c in range(1, nc + 1):
        d = wf.column_dimensions.get(get_column_letter(c))
        widths.append(round(d.width, 1) if d is not None and d.width else 9.0)
    rows, n_formula, n_uncached = [], 0, 0
    for r in range(1, nr + 1):
        row = []
        for c in range(1, nc + 1):
            cf, cv = wf.cell(r, c), wv.cell(r, c)
            f = cf.value if isinstance(cf.value, str) and cf.value.startswith("=") else None
            v = cv.value
            if f:
                n_formula += 1
                if v is None:
                    n_uncached += 1
            cell = {}
            txt = fmt_value(v, cf.number_format)
            if txt:
                cell["t"] = txt
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cell["n"] = 1
                if v < 0:
                    cell["neg"] = 1
            if f:
                cell["f"] = f
            font = cf.font
            if font is not None:
                if font.b:
                    cell["b"] = 1
                if font.i:
                    cell["i"] = 1
                col = _hex(font.color)
                if col and col != "000000":
                    cell["c"] = col
            fill = cf.fill
            if fill is not None and fill.fill_type == "solid":
                fc = _hex(fill.fgColor)
                if fc and fc != "FFFFFF":
                    cell["bg"] = fc
            if cf.comment is not None:
                cell["note"] = cf.comment.text[:600]
            row.append(cell)
        while row and not row[-1]:
            row.pop()
        rows.append(row)
    while rows and not rows[-1]:
        rows.pop()
    fr = wf.freeze_panes
    return {**head, "sheets": names, "sheet": sh, "rows": rows, "widths": widths,
            "freeze": fr if isinstance(fr, str) else None,
            "formulas": n_formula,
            # every formula blank = a file nobody recalculated (openpyxl-written).
            # A few blanks are formulas that return "" (IF(x="","",...)).
            "no_cache": n_formula > 0 and n_uncached == n_formula,
            "truncated": wv.max_row > MAX_ROWS or wv.max_column > MAX_COLS}


# --------------------------------------------------------------------------
# the model's own target (PM, 2026-11-04: "add a number to the analysis tab
# that shows the expected upside via the model as well")
# --------------------------------------------------------------------------
# Read by LABEL in the Valuation sheet's column A, never by address: rows move
# every time a line is added (the JV rows moved everything by three on 4-Nov).
VAL_LABELS = {"tp": "Expected price (Rs)", "year": "Valuation year",
              "nd_quarter": "Net debt as of (quarter)", "multiple": "Core multiple, ex JV (x)",
              "multiple_typed": "Target EV/EBITDA multiple"}


def _val_rows(ws) -> dict:
    want = {v: k for k, v in VAL_LABELS.items()}
    out = {}
    for r in range(1, min(ws.max_row, 80) + 1):
        lab = ws.cell(r, 1).value
        if isinstance(lab, str) and lab.strip() in want:
            out.setdefault(want[lab.strip()], r)
    return out


def _name_cell(wb, name: str) -> tuple[str, str] | None:
    """A workbook-level defined name -> (sheet, A1 cell without $), or None."""
    dn = wb.defined_names.get(name)
    if dn is None:
        return None
    for sheet, ref in dn.destinations:
        return sheet, ref.replace("$", "")
    return None


def valuation(entity: str, close: tuple[str, float] | None = None, base: pathlib.Path = CASES) -> dict | None:
    """The workbook's expected price as of its LAST SAVE, and the upside on
    `close` (date, price) - the same latest close the Analysis tab's juice is
    marked at, so the two upsides are on one price. None when there is no
    model. A workbook never recalculated has no cached values: that is an
    error, never a zero."""
    p = path_for(entity, base)
    if p is None:
        return None
    vals, forms = _load(p)
    if "Valuation" not in vals.sheetnames:
        return {"error": "the workbook has no Valuation sheet"}
    ws = vals["Valuation"]
    rows = _val_rows(ws)
    if "tp" not in rows:
        return {"error": "no 'Expected price (Rs)' row on the Valuation sheet"}
    out = {k: ws.cell(r, 2).value for k, r in rows.items()}
    if not isinstance(out.get("tp"), (int, float)):
        return {"error": "the expected price has no saved value: recalculate the workbook in Excel"}
    nc = _name_cell(forms, "CMP")
    nd = _name_cell(forms, "CMP_date")
    out["model_cmp"] = vals[nc[0]][nc[1]].value if nc else None
    out["model_cmp_date"] = str(vals[nd[0]][nd[1]].value) if nd else None
    out["saved"] = models(base)[entity]["saved"]
    out["tp_cell"] = f"Valuation!B{rows['tp']}"
    if close and close[1]:
        out["close_date"], out["close"] = close[0], close[1]
        out["upside_pct"] = round((out["tp"] / close[1] - 1) * 100, 2)
    return out


def set_cmp(entity: str, price: float, date: str, base: pathlib.Path = CASES) -> dict:
    """Write the day's close into the model's CMP and CMP_date (and the source
    note beside CMP) through Excel, recalculate, save, and return the model's
    new expected price. Through Excel because openpyxl would drop every cached
    value; into the PM's open window when the workbook is open."""
    p = path_for(entity, base)
    if p is None:
        return {"error": f"no model for {entity!r}"}
    _, forms = _load(p)
    nc, nd = _name_cell(forms, "CMP"), _name_cell(forms, "CMP_date")
    rows = _val_rows(forms["Valuation"]) if "Valuation" in forms.sheetnames else {}
    if not nc or not nd or "tp" not in rows:
        return {"error": "the workbook has no CMP / CMP_date names or no expected-price row"}
    from openpyxl.utils.cell import column_index_from_string, coordinate_from_string, get_column_letter
    letter, row = coordinate_from_string(nc[1])
    src_cell = f"{get_column_letter(column_index_from_string(letter) + 1)}{row}"   # the source note, one column right
    ops = [{"sheet": nc[0], "cell": nc[1], "mode": "set", "value": float(price)},
           # The leading apostrophe keeps the date TEXT: Excel otherwise turns
           # "2026-11-04" into a date serial (46330), unlike every other build.
           {"sheet": nd[0], "cell": nd[1], "mode": "set", "value": "'" + date},
           {"sheet": nc[0], "cell": src_cell, "mode": "set",
            "value": f"prices table, {entity} close {date} (Yahoo); set by the orchestrator"},
           {"sheet": "Valuation", "cell": f"B{rows['tp']}", "mode": "get", "value": None}]
    # `base` reaches excel() too. The first test run passed a scratch copy's
    # folder to everything EXCEPT this call, so excel() resolved the REAL model,
    # found it open and wrote the test close into the PM's window (2026-11-04).
    res = excel(entity, ops, base)
    cells = res["cells"] if isinstance(res["cells"], list) else [res["cells"]]
    tp_txt = cells[-1]["value"]
    try:
        tp = float(str(tp_txt).replace(",", ""))
    except ValueError:
        return {"error": f"expected price reads {tp_txt!r} after the update", "attached": res["attached"]}
    return {"tp": tp, "attached": res["attached"],
            "upside_pct": round((tp / float(price) - 1) * 100, 2), "close": float(price), "close_date": date}


# --------------------------------------------------------------------------
# open and edit (Windows, desktop Excel)
# --------------------------------------------------------------------------
def file_bytes(entity: str, base: pathlib.Path = CASES) -> dict:
    """The workbook as it sits on disk, for the card's Download button
    (PM, 2026-11-03). That is the LAST SAVE: an edit still unsaved in the PM's
    Excel window is not in it, the same basis the viewer renders."""
    p = path_for(entity, base)
    if p is None:
        return {"error": f"no model for {entity!r}"}
    m = models(base)[entity]
    return {"file": p.name, "saved": m["saved"], "open": m["open"], "bytes": p.read_bytes()}


def open_in_excel(entity: str) -> dict:
    p = path_for(entity)
    if p is None:
        return {"error": f"no model for {entity!r}"}
    os.startfile(str(p))                      # noqa: S606 - a file this module found
    return {"ok": True, "file": p.name}


_PS_EDIT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:IMS_MODEL_PATH
$ops = Get-Content -Raw -LiteralPath $env:IMS_MODEL_OPS | ConvertFrom-Json
$xl = $null; $attached = $false; $wb = $null
try { $xl = [System.Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application') } catch { }
if ($xl -ne $null) {
  foreach ($w in $xl.Workbooks) { if ($w.FullName -ieq $path) { $wb = $w; $attached = $true } }
}
if ($wb -eq $null) {
  $xl = New-Object -ComObject Excel.Application
  $xl.Visible = $false; $xl.DisplayAlerts = $false
  $wb = $xl.Workbooks.Open($path)
}
$out = @()
try {
  foreach ($op in $ops) {
    $rng = $wb.Worksheets.Item($op.sheet).Range($op.cell)
    $before = $rng.Formula
    if ($op.mode -eq 'set') {
      if ($op.value -is [string] -and $op.value.StartsWith('=')) { $rng.Formula = $op.value }
      elseif ($op.value -eq $null) { $rng.ClearContents() | Out-Null }
      elseif ($op.value -is [string]) { $rng.Value2 = $op.value }
      # [double], because PowerShell 5.1's ConvertFrom-Json turns 63983.33
      # into a [decimal] and COM refuses it ("Specified cast is not valid").
      # Integers and 7.3 had passed, so nothing had caught it (2026-11-03).
      else { $rng.Value2 = [double]$op.value }
    }
    elseif ($op.mode -eq 'note') {
      if ($rng.Comment -ne $null) { $rng.Comment.Delete() }
      $rng.AddComment([string]$op.value) | Out-Null
    }
    $out += [pscustomobject]@{ sheet = $op.sheet; cell = $op.cell; before = $before;
                               formula = $rng.Formula; value = $rng.Text }
  }
  # @() matters: in PowerShell 5.1 a single match is NOT an array, has no
  # .Count, and the save was skipped - the first edit read back "1,000" and
  # was then discarded by Close($false), with nothing raising (2026-11-02).
  if (@($ops | Where-Object { $_.mode -ne 'get' }).Count -gt 0) {
    $xl.Calculate(); $wb.Save()
    foreach ($o in $out) { $o.value = $wb.Worksheets.Item($o.sheet).Range($o.cell).Text }
  }
} finally {
  if (-not $attached) { $wb.Close($false); $xl.Quit() }
}
[pscustomobject]@{ attached = $attached; cells = $out } | ConvertTo-Json -Depth 4 -Compress
"""


def parse_ops(specs: list[str], mode: str) -> list[dict]:
    """'Sheet!A1=value' (set) or 'Sheet!A1' (get). Values: a number, a formula
    starting with '=', an empty string to clear, else text. A sheet name with
    spaces is fine ('Cash flow!D20=0')."""
    ops = []
    for s in specs:
        m = re.match(r"^(.+?)!([A-Z]{1,3}[0-9]{1,5})(?:=(.*))?$", s, re.S)
        if not m:
            raise ValueError(f"cannot read {s!r}: want Sheet!A1" + ("=value" if mode == "set" else ""))
        sheet, cell, raw = m.group(1).strip("'"), m.group(2), m.group(3)
        if mode == "set" and raw is None:
            raise ValueError(f"{s!r} has no '=value'")
        val = None
        if mode == "set" and raw != "":
            if raw.startswith("="):
                val = raw
            else:
                try:
                    val = float(raw.replace(",", "")) if re.match(r"^-?[\d,]*\.?\d+(e-?\d+)?$", raw.strip()) else raw
                except ValueError:
                    val = raw
        ops.append({"sheet": sheet, "cell": cell, "mode": mode, "value": val})
    return ops


def excel(entity: str, ops: list[dict], base: pathlib.Path = CASES) -> dict:
    p = path_for(entity, base)
    if p is None:
        raise SystemExit(f"no model for {entity!r}")
    before = p.stat().st_mtime
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(ops, fh)
        opsf = fh.name
    try:
        env = {**os.environ, "IMS_MODEL_PATH": str(p.resolve()), "IMS_MODEL_OPS": opsf}
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_EDIT],
                           capture_output=True, text=True, env=env, timeout=240)
    finally:
        os.unlink(opsf)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        hint = (" Excel is busy: a cell is probably being edited (press Enter or Esc there) "
                "or a dialog is open.") if "0x8001010A" in r.stderr or "RPC_E" in r.stderr else ""
        raise SystemExit("Excel edit failed:" + hint + "\n  " + "\n  ".join(msg[-6:]))
    res = json.loads(r.stdout.strip().splitlines()[-1])
    # The acceptance check for an edit is the FILE, not Excel's echo: Excel
    # happily reports the new value of a workbook it is about to discard.
    if any(o["mode"] != "get" for o in ops) and p.stat().st_mtime <= before:
        raise SystemExit("Excel reported the edit but the file was not saved; nothing changed on disk")
    return res


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

    print("model_view selftest")
    cr = '#,##0;(#,##0);"-"'
    chk("crore positive", fmt_value(48427.97, cr), "48,428")
    chk("crore negative in parentheses", fmt_value(-5932.4, cr), "(5,932)")
    chk("crore zero is a dash", fmt_value(0, cr), "-")
    chk("percent", fmt_value(0.0189, '0.0%;(0.0%);"-"'), "1.9%")
    chk("multiple", fmt_value(6.885, '0.0"x"'), "6.9x")
    chk("signed multiple shift", fmt_value(-1, '+0.0"x";-0.0"x";"base"'), "-1.0x")
    chk("signed shift zero", fmt_value(0, '+#,##0;-#,##0;"base"'), "base")
    chk("rupees two decimals", fmt_value(1269.218, '#,##0.00;(#,##0.00);"-"'), "1,269.22")
    chk("text passes through", fmt_value("FY28", "General"), "FY28")
    chk("general integer", fmt_value(4, "General"), "4")
    chk("Excel's escaped parentheses", fmt_value(-38804.2, r'#,##0;\(#,##0\);"-"'), "(38,804)")

    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        (base / "acme").mkdir()
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "P&L"
        ws["A1"], ws["B1"], ws["C1"] = "EBITDA", 100.0, "=B1*2"
        wb.save(base / "acme" / "Acme_model.xlsx")
        chk("model discovered", list(models(base)), ["acme"])
        chk("not open without an owner file", models(base)["acme"]["open"], False)
        (base / "acme" / "~$me_model.xlsx").write_text("x")
        chk("open when Excel's owner file exists", models(base)["acme"]["open"], True)
        chk("owner file is not a model", list(models(base)), ["acme"])
        chk("traversal refused", path_for("../acme", base), None)
        chk("unknown entity refused", render("nope", base=base).get("error"), "no model for 'nope'")
        v = render("acme", base=base)
        chk("typed value renders", v["rows"][0][1]["t"], "100")
        chk("a never-recalculated file is flagged", (v["formulas"], v["no_cache"]), (1, True))
        chk("unchanged short-circuits", render("acme", since=v["mtime"], base=base).get("unchanged"), True)
        fb = file_bytes("acme", base)
        chk("download is the file on disk", (fb["file"], fb["bytes"]),
            ("Acme_model.xlsx", (base / "acme" / "Acme_model.xlsx").read_bytes()))
        chk("download refuses a traversal", file_bytes("../acme", base).get("error"), "no model for '../acme'")
        chk("no Valuation sheet is an error, not a number", valuation("acme", base=base).get("error"),
            "the workbook has no Valuation sheet")
        chk("no model, no valuation", valuation("nope", base=base), None)
        # a model with a Valuation sheet: labels read by NAME, rows anywhere
        from openpyxl.workbook.defined_name import DefinedName
        (base / "beta").mkdir()
        wb2 = Workbook()
        inp = wb2.active
        inp.title = "Inputs"
        inp["C12"], inp["C13"] = 1320.0, "2026-11-03"
        for nm, ref in (("CMP", "Inputs!$C$12"), ("CMP_date", "Inputs!$C$13")):
            wb2.defined_names[nm] = DefinedName(nm, attr_text=ref)
        va = wb2.create_sheet("Valuation")
        for r, (lab, v) in enumerate((("Valuation year", "FY28"), ("Net debt as of (quarter)", "Q4FY27"),
                                      ("Target EV/EBITDA multiple", 9.85), ("Core multiple, ex JV (x)", 9.455),
                                      ("Expected price (Rs)", 1458.24)), start=7):
            va.cell(r, 1, lab), va.cell(r, 2, v)
        wb2.save(base / "beta" / "Beta_model.xlsx")
        vv = valuation("beta", close=("2026-11-04", 1336.0), base=base)
        chk("expected price read by its label", (vv["tp"], vv["tp_cell"], vv["year"], vv["nd_quarter"]),
            (1458.24, "Valuation!B11", "FY28", "Q4FY27"))
        chk("upside on the close given, not the model's own CMP", (vv["upside_pct"], vv["model_cmp"]), (9.15, 1320.0))
        va["B11"] = None
        wb2.save(base / "beta" / "Beta_model.xlsx")
        chk("an unsaved expected price is an error, never zero",
            "recalculate" in valuation("beta", base=base).get("error", ""), True)
    chk("set spec parsed", parse_ops(["Cash flow!D20=-1,500"], "set"),
        [{"sheet": "Cash flow", "cell": "D20", "mode": "set", "value": -1500.0}])
    chk("formula kept as a formula", parse_ops(["Valuation!B15==B13+1"], "set")[0]["value"], "=B13+1")
    chk("empty value clears", parse_ops(["Segments!L18="], "set")[0]["value"], None)
    try:
        parse_ops(["Segments L18=3"], "set")
        chk("malformed spec refused", "accepted", "refused")
    except ValueError:
        chk("malformed spec refused", "refused", "refused")
    print(f"{'PASS' if not fails else 'FAIL'} ({fails} failures)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("entity", nargs="?")
    ap.add_argument("--set", nargs="+", metavar="SHEET!CELL=VALUE")
    ap.add_argument("--get", nargs="+", metavar="SHEET!CELL")
    ap.add_argument("--open", action="store_true", help="open the workbook in desktop Excel")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.entity:
        for e, m in models().items():
            print(f"{e:16} {m['file']:32} saved {m['saved']}{'  (open in Excel)' if m['open'] else ''}")
        return 0
    if a.open:
        print(open_in_excel(a.entity))
        return 0
    if a.set or a.get:
        res = excel(a.entity, parse_ops(a.set, "set") if a.set else parse_ops(a.get, "get"))
        where = "in the open Excel window" if res["attached"] else "in a hidden Excel instance"
        print(f"{'edited' if a.set else 'read'} {where}")
        cells = res["cells"] if isinstance(res["cells"], list) else [res["cells"]]
        for c in cells:
            chg = f"  (was {c['before']})" if a.set and c["before"] != c["formula"] else ""
            print(f"  {c['sheet']}!{c['cell']}: {c['formula']}  ->  {c['value']}{chg}")
        return 0
    m = models().get(a.entity)
    print(json.dumps(m, indent=1) if m else f"no model for {a.entity!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
