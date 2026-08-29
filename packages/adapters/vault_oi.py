"""L0 adapter — futures OI positioning from the existing vault pipeline.

The vault's OI fetcher already runs daily and writes Coverage/<sector>/<name>/
OI History.md with computed percentiles, z-scores and buildup classification.
That is data, not the old scoring methodology, so it is worth reusing rather
than rebuilding an NSE fetcher.

FILE SHAPE: YAML frontmatter carrying the CURRENT snapshot (percentiles,
buildup, lot size, spot), then a newest-first markdown table of daily rows.
Both are ingested — the table for history, the frontmatter for today's derived
metrics, which are not recomputable from the table alone.

TWO HORIZONS, BOTH KEPT. The vault publishes buildup over 15d AND 3m, and they
routinely disagree: a name can be short-covering over 15d inside a 3-month
short build. That disagreement is the interesting state, so collapsing to one
would throw away the signal.

NOT EVERY NAME IS IN F&O. VAML carries `status: not_in_fno` — newly listed, no
futures. It gets no OI rows at all rather than zeros, because zero OI and no
OI market are different facts.

Usage:
    python packages/adapters/vault_oi.py --probe
    python packages/adapters/vault_oi.py --load
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sqlite3
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
VAULT = pathlib.Path(
    r"C:\Users\rajvaibhav.yadav\OneDrive - PinPOINT\Obsidian Vault\Coverage")

# vault folder name -> our entity id
NAMES = {
    "Hindalco": "hindalco",
    "NALCO": "nalco",
    "Hindustan Zinc": "hindustan_zinc",
    "Vedanta Ltd": "vedanta",
    "Vedanta Aluminium": "vaml",
    # --- steel, added 2026-08-25 ---
    # The glob is `*/<folder>/OI History.md`, so these resolve under
    # Coverage/Steel/ without naming the sector. Checked for collisions across
    # all six coverage sectors first: none of these folder names appears twice,
    # which matters because collect() takes hits[0] and would silently bind to
    # whichever sector the glob walked first.
    "Tata Steel": "tata_steel",
    "JSW Steel": "jsw_steel",
    "Jindal Steel": "jindal_steel",
    "SAIL": "sail",
    "Jindal Stainless": "jindal_stainless",
    "Shyam Metalics": "shyam_metalics",
    "APL Apollo Tubes": "apl_apollo",
    # --- cement, added 2026-08-28, same collision check run: none of these
    # four folder names appears under any other Coverage/<sector>/. Only the
    # F&O names are mapped — the vault marks JK Cement, Ramco, Nuvoco, Star
    # and JSW Cement `status: not_in_fno`, and vault_oi already skips that
    # status, but an unmapped name never even resolves a folder.
    "UltraTech": "ultratech",
    "Ambuja": "ambuja",
    "Shree Cement": "shree",
    "Dalmia Bharat": "dalmia",
    # --- mining, added 2026-08-29, same collision check run: none of these
    # four folder names appears under any other Coverage/<sector>/. Hindustan
    # Copper and Lloyds Metals carry `status: not_in_fno` (price-only files)
    # and are skipped by the status check below — mapped anyway so the day a
    # contract lists, the load starts without an edit here.
    "NMDC": "nmdc",
    "Coal India": "coal_india",
    "Hindustan Copper": "hindustan_copper",
    "Lloyds Metals": "lloyds_metals",
    # --- ems, added 2026-08-30, same collision check run: none of these six
    # folder names appears under any other Coverage/<sector>/. Syrma SGS and
    # Avalon carry `status: not_in_fno` and are skipped by the status check —
    # mapped anyway so a future F&O listing starts loading without an edit
    # here (the mining convention).
    "Dixon": "dixon",
    "Amber": "amber",
    "Kaynes": "kaynes",
    "PG Electroplast": "pg_electroplast",
    "Syrma SGS": "syrma_sgs",
    "Avalon": "avalon",
}

BUILDUP_OK = {"long_buildup", "short_buildup", "short_covering",
              "long_unwinding", "neutral"}


def iso(d: str) -> str:
    dd, mm, yy = d.strip().split("-")
    return f"{yy}-{mm}-{dd}"


def num(s: str):
    s = (s or "").strip().replace(",", "").replace("+", "")
    if s in ("", "-", "—", "–"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse(path: pathlib.Path) -> tuple[dict, list[dict]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm: dict[str, str] = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if m:
        for line in m.group(1).split("\n"):
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip().strip('"')

    rows = []
    for line in text.split("\n"):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5 or not re.match(r"\d{2}-\d{2}-\d{4}", cells[0]):
            continue
        rows.append({
            "date": iso(cells[0]),
            "oi": num(cells[1]),
            "oi_chg_lots": num(cells[2]),
            "price": num(cells[3]),
            "price_chg_pct": num(cells[4]),
        })
    return fm, rows


def collect():
    out = []
    for folder, eid in NAMES.items():
        hits = list(VAULT.glob(f"*/{folder}/OI History.md"))
        if not hits:
            out.append((eid, None, {}, []))
            continue
        fm, rows = parse(hits[0])
        out.append((eid, hits[0], fm, rows))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--load", action="store_true")
    a = ap.parse_args()

    found = collect()
    print(f"{'entity':16} {'rows':>5} {'latest':12} {'fetched':12} "
          f"{'buildup 3m':16} {'pctile':>7}  note")
    print("-" * 92)
    for eid, path, fm, rows in found:
        if path is None:
            print(f"{eid:16} {'-':>5} {'-':12} {'-':12} {'FILE NOT FOUND':16}")
            continue
        if fm.get("status") == "not_in_fno":
            print(f"{eid:16} {'-':>5} {'-':12} {fm.get('last_fetched','?'):12} "
                  f"{'NOT IN F&O':16} {'-':>7}  no futures — no rows, not zeros")
            continue
        latest = rows[0]["date"] if rows else "-"
        print(f"{eid:16} {len(rows):>5} {latest:12} {fm.get('last_fetched','?'):12} "
              f"{fm.get('buildup_3m','?'):16} {fm.get('percentile_3m','?'):>7}  "
              f"15d={fm.get('buildup_15d','?')}")

    if not a.load:
        return 0

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    n = 0
    for eid, path, fm, rows in found:
        if path is None or fm.get("status") == "not_in_fno" or not rows:
            continue
        lot = int(float(fm["lot_size"])) if fm.get("lot_size") else None
        latest_date = rows[0]["date"]

        for r in rows:
            # OI change is published in lots; derive pct rather than assume it
            prev = (r["oi"] - r["oi_chg_lots"]
                    if r["oi"] is not None and r["oi_chg_lots"] is not None else None)
            chg_pct = (r["oi_chg_lots"] / prev * 100.0) if prev else None
            is_latest = r["date"] == latest_date
            bu3 = fm.get("buildup_3m") if is_latest else None
            bu15 = fm.get("buildup_15d") if is_latest else None
            conn.execute(
                "INSERT OR REPLACE INTO oi (entity_id,date,oi,oi_chg_lots,"
                "oi_chg_pct,price,price_chg_pct,lot_size,buildup,buildup_15d,"
                "oi_percentile,oi_percentile_15d,z_score_3m,pct_vs_median_3m,"
                "lookback_days,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, r["date"], r["oi"], r["oi_chg_lots"], chg_pct, r["price"],
                 r["price_chg_pct"], lot,
                 bu3 if bu3 in BUILDUP_OK else None,
                 bu15 if bu15 in BUILDUP_OK else None,
                 num(fm.get("percentile_3m")) if is_latest else None,
                 num(fm.get("percentile_15d")) if is_latest else None,
                 num(fm.get("z_score_3m")) if is_latest else None,
                 num(fm.get("pct_vs_median_3m")) if is_latest else None,
                 63 if is_latest else None, "vault_oi_history"))
            n += 1
    conn.commit()
    conn.close()
    print(f"\nloaded {n} OI rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
