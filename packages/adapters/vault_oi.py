"""L0 adapter — futures OI positioning from the existing vault pipeline.

The vault's OI fetcher already runs daily and writes Coverage/<sector>/<name>/
OI History.md with computed percentiles, z-scores and buildup classification.
That is data, not the old scoring methodology, so it is worth reusing rather
than rebuilding an NSE fetcher.

FILE SHAPE: YAML frontmatter carrying the CURRENT snapshot (percentiles,
buildup, lot size, spot), then a newest-first markdown table of daily rows.
Both are ingested — the table for history, the frontmatter for today's derived
metrics that are not recomputable from the table alone (buildup, z-score, %
vs median, lot size).

THE 3M PERCENTILE IS COMPUTED HERE, NOT COPIED (PM ruling 2026-09-15). The
vault's `percentile_3m` is EXPIRY-CYCLE-NORMALISED: it splits the window at
every >8% single-day OI drop, divides each day by its own cycle's median and
ranks the ratios. That answers "high within the current expiry cycle?", not
"high over three months?" — Coforge printed 95th with OI 8.6% BELOW the 3m
median and a -0.7σ z-score in the same frontmatter block, and 10 of 31 names
sat on the opposite side of 50 from their raw rank. Where the drop detector
found only one cycle it silently fell back to raw, so the column mixed two
definitions across rows. `oi_percentile` is now the plain rank of the latest
OI among the last 63 sessions in the table (strict-below / n), the number the
OI-vs-time chart actually shows.

TWO HORIZONS, BOTH KEPT. The vault publishes buildup over 15d AND 3m, and they
routinely disagree: a name can be short-covering over 15d inside a 3-month
short build. That disagreement is the interesting state, so collapsing to one
would throw away the signal.

NOT EVERY NAME IS IN F&O. VAML carries `status: not_in_fno` — newly listed, no
futures. It gets no OI rows at all rather than zeros, because zero OI and no
OI market are different facts.

A SECOND SOURCE SINCE 2026-11-20: THE NSE BHAVCOPY, for names the vault
fetcher does not carry (PM: "add syrma to OI. Add power sector as well below
IT: all names, including premier, waaree, suzlon, inox wind, and the gencos,
transcos"). See BHAVCOPY below. `--load-bhavcopy` builds their `oi` rows from
`fo_oi`, which must already hold the day, so refresh.py runs it AFTER the
F&O bhavcopy and options steps, not beside `--load`.

Usage:
    python packages/adapters/vault_oi.py --probe
    python packages/adapters/vault_oi.py --load
    python packages/adapters/vault_oi.py --load-bhavcopy
    python packages/adapters/vault_oi.py --selftest
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
    # Dalmia Bharat REMOVED 2026-09-15 (PM: "it's no longer in F&O"). Its
    # vault file kept stamping last_fetched daily but the table stopped at
    # 25-08-2026, so the loader was carrying a stale frontmatter percentile
    # onto NULL-OI rows. Invariant 7: no row for a name not in F&O. Its oi
    # rows were deleted from the store the same day; scoring is untouched.
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
    # --- IT, added 2026-08-31 on the PM's instruction ("add OI for IT").
    # Same collision check run: none of these thirteen folder names appears
    # under any other Coverage/<sector>/. IT is NOT a modelled sector — no
    # specs, no scores — so these ids exist only in UNMODELLED below, which
    # ensure-inserts their `entities` rows at load (the FK on oi.entity_id
    # would otherwise reject every row). LTTS carries `status: not_in_fno`
    # and is skipped by the status check; mapped anyway per the mining
    # convention so an F&O listing starts loading without an edit here.
    "Infosys": "infosys",
    "TCS": "tcs",
    "HCL Technologies": "hcl_tech",
    "Wipro": "wipro",
    "Tech Mahindra": "tech_mahindra",
    "LTIMindtree": "ltimindtree",
    "Persistent Systems": "persistent",
    "Coforge": "coforge",
    "Mphasis": "mphasis",
    "KPIT Technologies": "kpit",
    "Tata Elxsi": "tata_elxsi",
    "OFSS": "ofss",
    "LTTS": "ltts",
}

# OI-tracked names with NO spec and NO pillar — positioning context only.
# Their `entities` rows are created here (kind 'company', sector set, NO
# peer_group, so invariant 7 keeps them out of every scoring path), and the
# display name comes from the vault folder rather than the slug. Modelled
# names must never appear in this dict: their entity rows belong to init_db.
UNMODELLED: dict[str, tuple[str, str]] = {
    "infosys": ("Infosys", "it"),
    "tcs": ("TCS", "it"),
    "hcl_tech": ("HCL Technologies", "it"),
    "wipro": ("Wipro", "it"),
    "tech_mahindra": ("Tech Mahindra", "it"),
    "ltimindtree": ("LTIMindtree", "it"),
    "persistent": ("Persistent Systems", "it"),
    "coforge": ("Coforge", "it"),
    "mphasis": ("Mphasis", "it"),
    "kpit": ("KPIT Technologies", "it"),
    "tata_elxsi": ("Tata Elxsi", "it"),
    "ofss": ("OFSS", "it"),
    "ltts": ("LTTS", "it"),
}

# OI names loaded from `fo_oi` (the NSE bhavcopy), NOT from a vault file.
# entity id -> (NSE symbol, display name, sector, insert_entity).
#
# WHY A SECOND SOURCE. The vault's OI fetcher belongs to the old system and is
# not edited from here; on 2026-11-20 it still stamped Syrma `not_in_fno`
# (futures since 2026-10-28) and has no Power folder at all. `fo_oi` already
# holds every F&O symbol daily, and it is THE SAME NUMBER the vault prints:
# Dixon 2026-11-19 is 2,471,550 / +235,850 in both. The latest row's derived
# columns were reproduced from the stored rows for all 30 vault names before
# this was written, and all 30 matched to the printed digit:
#   z_score_3m       (oi - mean) / SAMPLE sd over the last 63 sessions
#   pct_vs_median_3m oi / median(last 63) - 1, in %
#   buildup          sign of OI change x sign of price change, newest row
#                    against the 63rd-newest (long buildup = both up)
# TWO DIFFERENCES FROM A VAULT ROW, both stated rather than hidden:
#   - price is the FRONT FUTURES close (fo_oi.close); the vault prints the
#     underlying. Dixon: 13,012 against 13,054. One basis for all of these
#     names, and four of them have no cash close in `prices` at all.
#   - under 63 sessions the z, the median gap and the buildup are WITHHELD.
#     Syrma had 16 sessions on the day it was added; a "3-month" label over
#     three weeks would be a different statistic under the same name.
#
# insert_entity is True only for the four renewable-EQUIPMENT names, which
# have no spec (specs/entities/power.yaml is the eight utilities). Their
# `entities` rows are created here with sector 'power', NO peer_group and
# active = 0: OI only, off the Results roster, out of every scoring path.
BHAVCOPY: dict[str, tuple[str, str, str, bool]] = {
    "syrma_sgs":              ("SYRMA", "Syrma SGS", "ems", False),
    # gencos
    "ntpc":                   ("NTPC", "NTPC", "power", False),
    "tata_power":             ("TATAPOWER", "Tata Power", "power", False),
    "jsw_energy":             ("JSWENERGY", "JSW Energy", "power", False),
    "nhpc":                   ("NHPC", "NHPC", "power", False),
    "adani_power":            ("ADANIPOWER", "Adani Power", "power", False),
    "adani_green":            ("ADANIGREEN", "Adani Green", "power", False),
    # transcos
    "power_grid":             ("POWERGRID", "Power Grid", "power", False),
    "adani_energy_solutions": ("ADANIENSOL", "Adani Energy Solutions",
                               "power", False),
    # renewable equipment
    "waaree_energies":        ("WAAREEENER", "Waaree Energies", "power", True),
    "premier_energies":       ("PREMIERENE", "Premier Energies", "power", True),
    "suzlon":                 ("SUZLON", "Suzlon", "power", True),
    "inox_wind":              ("INOXWIND", "Inox Wind", "power", True),
}

BUILDUP_OK = {"long_buildup", "short_buildup", "short_covering",
              "long_unwinding", "neutral"}

LOOKBACK_3M = 63  # sessions — the vault's own 3m window, kept for comparability


def raw_pct_3m(rows_newest_first: list[dict],
               n: int = LOOKBACK_3M) -> float | None:
    """Percentile rank (0-100) of the latest OI among the last LOOKBACK_3M
    sessions, strict-below count over n, rounded — the same rank the vault
    used BEFORE its cycle normalisation, so 41 here means what 41 meant
    there. Raw on purpose: the expiry sawtooth is real, but the chart shows
    raw OI and the Mag cell is raw, so the rank must be too."""
    vals = [r["oi"] for r in rows_newest_first[:n]
            if r.get("oi") is not None]
    if not vals or rows_newest_first[0].get("oi") is None:
        return None
    cur = rows_newest_first[0]["oi"]
    return float(round(sum(1 for v in vals if v < cur) / len(vals) * 100))


def derive_3m(rows_newest_first: list[dict],
              n: int = LOOKBACK_3M) -> dict:
    """z, median gap and buildup for the newest row, the vault's definitions
    (see BHAVCOPY). All three are None under n sessions."""
    out = {"z": None, "pct_med": None, "buildup": None}
    w = [r for r in rows_newest_first[:n] if r.get("oi") is not None]
    if len(w) < n:
        return out
    import statistics as st
    v = [r["oi"] for r in w]
    sd = st.stdev(v)
    med = st.median(v)
    if sd:
        out["z"] = round((v[0] - st.mean(v)) / sd, 2)
    if med:
        out["pct_med"] = round((v[0] / med - 1) * 100, 1)
    d_oi = v[0] - v[-1]
    p0, p1 = w[0].get("price"), w[-1].get("price")
    if p0 is None or p1 is None:
        return out
    d_p = p0 - p1
    if d_oi == 0 or d_p == 0:
        out["buildup"] = "neutral"
    elif d_oi > 0:
        out["buildup"] = "long_buildup" if d_p > 0 else "short_buildup"
    else:
        out["buildup"] = "short_covering" if d_p > 0 else "long_unwinding"
    return out


def load_bhavcopy(conn: sqlite3.Connection) -> list[tuple[str, int, str]]:
    """`oi` rows for every BHAVCOPY name, from fo_oi. Returns
    [(entity_id, rows written, note)]. A symbol with no fo_oi rows writes
    nothing and says so: no futures market and zero OI are different facts."""
    report = []
    for eid, (sym, disp, sector, insert) in BHAVCOPY.items():
        if insert:
            conn.execute(
                "INSERT OR IGNORE INTO entities (id, kind, name, sector, "
                "active) VALUES (?, 'company', ?, ?, 0)", (eid, disp, sector))
        if not conn.execute("SELECT 1 FROM entities WHERE id=?",
                            (eid,)).fetchone():
            report.append((eid, 0, "NO entities ROW - spec not initialised"))
            continue
        src = conn.execute(
            "SELECT date, oi_shares, oi_chg, close FROM fo_oi WHERE symbol=? "
            "ORDER BY date", (sym,)).fetchall()
        if not src:
            report.append((eid, 0, f"no fo_oi rows for {sym}"))
            continue
        try:
            lot = conn.execute(
                "SELECT lot FROM fo_options WHERE symbol=? AND lot IS NOT NULL "
                "ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
        except sqlite3.OperationalError:
            lot = None
        lot = int(lot[0]) if lot and lot[0] else None
        rows = []
        prev_close = None
        for d, oi, chg, close in src:
            prev = oi - chg
            rows.append({
                "date": d, "oi": oi, "oi_chg_lots": chg,
                "oi_chg_pct": (chg / prev * 100.0) if prev else None,
                "price": close,
                "price_chg_pct": (round((close / prev_close - 1) * 100, 1)
                                  if prev_close else None)})
            prev_close = close
        newest = rows[::-1]
        d3 = derive_3m(newest)
        pct = raw_pct_3m(newest)
        for r in rows:
            last = r is rows[-1]
            conn.execute(
                "INSERT OR REPLACE INTO oi (entity_id,date,oi,oi_chg_lots,"
                "oi_chg_pct,price,price_chg_pct,lot_size,buildup,buildup_15d,"
                "oi_percentile,oi_percentile_15d,z_score_3m,pct_vs_median_3m,"
                "lookback_days,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, r["date"], r["oi"], r["oi_chg_lots"], r["oi_chg_pct"],
                 r["price"], r["price_chg_pct"], lot,
                 d3["buildup"] if last else None, None,
                 pct if last else None, None,
                 d3["z"] if last else None, d3["pct_med"] if last else None,
                 LOOKBACK_3M if last else None, "fo_bhavcopy"))
        note = f"{sym} to {rows[-1]['date']}"
        if len(rows) < LOOKBACK_3M:
            note += (f"; {len(rows)} sessions, 3m read withheld until "
                     f"{LOOKBACK_3M}")
        report.append((eid, len(rows), note))
    conn.commit()
    return report


def selftest() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(("PASS " if cond else "FAIL ") + label)

    def fx(n, oi0, doi, p0, dp):   # newest first; oldest is (oi0, p0)
        return [{"oi": oi0 + doi * i, "price": p0 + dp * i}
                for i in range(n - 1, -1, -1)]
    up_dn = derive_3m(fx(63, 1000, 10, 500, -1))
    check("OI up, price down over 63 sessions is a short buildup",
          up_dn["buildup"] == "short_buildup")
    check("OI down, price up is short covering",
          derive_3m(fx(63, 5000, -10, 500, 1))["buildup"] == "short_covering")
    check("both up is a long buildup; both down a long unwinding",
          derive_3m(fx(63, 1000, 10, 500, 1))["buildup"] == "long_buildup"
          and derive_3m(fx(63, 5000, -10, 500, -1))["buildup"]
          == "long_unwinding")
    # 1000..1620 step 10: mean 1310, sample sd 183.30, newest 1620
    check("z is on the SAMPLE sd (1.69), median gap +23.7%",
          up_dn["z"] == 1.69 and up_dn["pct_med"] == 23.7)
    short = derive_3m(fx(16, 1000, 10, 500, 1))
    check("16 sessions: z, median gap and buildup all withheld",
          short == {"z": None, "pct_med": None, "buildup": None})
    check("a flat OI window has no z (sd 0), never a division error",
          derive_3m(fx(63, 1000, 0, 500, 1))["z"] is None)
    check("no BHAVCOPY id is also a vault-loaded IT id",
          not set(BHAVCOPY) & set(UNMODELLED))
    # LIVE: the definitions reproduce what the vault itself printed.
    if DB.exists():
        conn = sqlite3.connect(DB)
        got = conn.execute(
            "SELECT entity_id, z_score_3m, pct_vs_median_3m, buildup, date "
            "FROM oi o WHERE source='vault_oi_history' AND z_score_3m IS NOT "
            "NULL AND date=(SELECT MAX(date) FROM oi WHERE entity_id="
            "o.entity_id)").fetchall()
        bad = []
        for eid, z, pm, bu, d in got:
            h = [{"oi": a, "price": b} for a, b in conn.execute(
                "SELECT oi, price FROM oi WHERE entity_id=? AND oi IS NOT "
                "NULL ORDER BY date DESC LIMIT 63", (eid,))]
            m = derive_3m(h)
            if m["buildup"] is None:
                continue
            if (abs(m["z"] - z) > 0.011 or abs(m["pct_med"] - pm) > 0.11
                    or m["buildup"] != bu):
                bad.append((eid, (z, pm, bu), m))
        conn.close()
        check(f"live: derive_3m reproduces the vault's own latest row on "
              f"{len(got)} names" + (f" - MISMATCH {bad}" if bad else ""),
              got and not bad)
    else:
        print("SKIP live reproduction (no store)")
    print("selftest", "OK" if ok else "FAILED")
    return 0 if ok else 1


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
    ap.add_argument("--load-bhavcopy", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.load_bhavcopy:
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA foreign_keys = ON")
        rep = load_bhavcopy(conn)
        conn.close()
        for eid, n, note in rep:
            print(f"{eid:24} {n:>5}  {note}")
        print(f"\nloaded {sum(n for _, n, _ in rep)} OI rows from fo_oi")
        return 1 if any(n == 0 for _, n, _ in rep) else 0

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
        pct = raw_pct_3m(rows) if rows else None
        print(f"{eid:16} {len(rows):>5} {latest:12} {fm.get('last_fetched','?'):12} "
              f"{fm.get('buildup_3m','?'):16} "
              f"{('%.0f' % pct) if pct is not None else '?':>7}  "
              f"vault_fm={fm.get('percentile_3m','?')} 15d={fm.get('buildup_15d','?')}")

    if not a.load:
        return 0

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    # OI-only names need an entities row or the FK rejects every OI insert.
    # OR IGNORE, so a slug that later gains a real spec (and an init_db row)
    # is never overwritten from here.
    for eid, (disp, sector) in UNMODELLED.items():
        conn.execute(
            "INSERT OR IGNORE INTO entities (id, kind, name, sector) "
            "VALUES (?, 'company', ?, ?)", (eid, disp, sector))
    n = 0
    for eid, path, fm, rows in found:
        if path is None or fm.get("status") == "not_in_fno" or not rows:
            continue
        lot = int(float(fm["lot_size"])) if fm.get("lot_size") else None
        latest_date = rows[0]["date"]
        pct_3m = raw_pct_3m(rows)  # ours, not fm["percentile_3m"] — see header

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
                 pct_3m if is_latest else None,
                 num(fm.get("percentile_15d")) if is_latest else None,
                 num(fm.get("z_score_3m")) if is_latest else None,
                 num(fm.get("pct_vs_median_3m")) if is_latest else None,
                 LOOKBACK_3M if is_latest else None, "vault_oi_history"))
            n += 1
    conn.commit()
    conn.close()
    print(f"\nloaded {n} OI rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
