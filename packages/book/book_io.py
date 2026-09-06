"""The PM's actual book — daily IMS position snapshots, stored pair-wise.

    python packages/book/book_io.py --parse data/book/staging/raw_2026-09-04.tsv --date 2026-09-04
    python packages/book/book_io.py --load  data/book/staging/2026-09-04.json
    python packages/book/book_io.py --report            # latest snapshot
    python packages/book/book_io.py --selftest

Driven by the `daily_review` skill. The PM pastes the IMS's tab-separated
export; THIS module parses, validates, persists and does every piece of
arithmetic. Nothing here is transcribed by a model — the paste is the input,
the cross-foot against the export's own subtotal rows is the transcription
check.

THE FORMAT is the one specified in the vault's
`Portfolio Management/IMS-Spec.md` (PM-confirmed 2026-08-09), minus the
trailing Momentum MV% column, and minus the Cost column from 2026-09-06 on
(the PM is removing it; the parser accepts both shapes). Row types: sector
aggregates (blank ticker, label in cell 2), positions (ticker in cell 1),
the N.A. bucket (realized P&L of closed positions PLUS costs — currency,
rollover, fixed; zero MV by construction), and a final book-total row.

Position columns after the LONG/SHORT flag, always this order:

    MV%  BetaMV%  DTD_PNL  DTD_PNL%  DTD_TRADING_PNL  MTD_PNL  YTD_PNL
    MTD_PNL%  YTD_PNL%  GMV%          (all PNL in USD; % = fraction of NAV)

NAV is DERIVED per snapshot as DTD_PNL / DTD_PNL% (median over rows where
both are non-zero) — never hardcoded, per the spec.

WHY THIS EXISTS AT ALL — two things the company IMS cannot show:

1. PAIRS. The IMS lists positions one by one; the PM tags each with a pair
   name ("IT 5") and more than two legs can share a tag. The unit of thought
   is the pair, so the display is one line per pair.
2. ROLLOVERS. Bloomberg futures tickers reset monthly (TATA=U6 -> TATA=V6)
   and the per-ticker YTD PNL resets with them (the closed contract's
   realized P&L falls into N.A.) — so pair inception and since-inception P&L
   are invisible there. Here the contract token is stripped to a stable ROOT
   and YTD P&L is CHAINED: a contract change between snapshots freezes the
   old contract's last-seen YTD PNL into `realized`; total = frozen + live.

CHAIN RULES, each one load-bearing:

- A snapshot is the FULL book: a root absent from an intervening snapshot was
  closed, so absence also splits a segment (a reopened position's YTD PNL
  restarts). Splits key on STORED SNAPSHOT DATES, never calendar days — this
  tool runs on demand, and a quiet fortnight must not split a held contract
  and double-count.
- A YEAR BOUNDARY between consecutive appearances also splits: YTD resets on
  Jan 1 with no ticker change on cash positions (futures never span it —
  monthly contracts).
- The chain is only as good as the cadence: a roll across a snapshot gap
  wider than ROLL_GAP_DAYS means the dying contract's final P&L was last
  seen early — flagged `gap_risk`, never "corrected".
- ASSUMED, TO VERIFY ON THE FIRST OBSERVED ROLL: the new contract's YTD PNL
  starts near zero (realized P&L of the old contract goes to N.A., not into
  the new ticker). Evidence so far: KAYNE opened in-window shows
  DTD == MTD == YTD to the last digit. If a roll ever arrives with the new
  ticker CARRYING the old P&L, the chain double-counts — check the first one.

Options (`XXXX IS MM/DD/YY C####/P#### Equity`) carry no '=' token, so every
series is its own root — which matches the PM's 2026-08-09 instruction to
treat options as separate convexity positions, never delta-netted.

Never writes to `prices`, `pillar_scores` or anything a pillar reads.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
STAGING = REPO / "data" / "book" / "staging"

DDL = """
CREATE TABLE IF NOT EXISTS book_snapshots (
    snap_date   TEXT PRIMARY KEY
                CHECK (snap_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    source_file TEXT NOT NULL,
    loaded_at   TEXT NOT NULL,
    n_positions INTEGER NOT NULL,
    pnl_basis   TEXT NOT NULL DEFAULT 'contract_itd'
                CHECK (pnl_basis IN ('contract_itd','daily')),
    nav         REAL,                   -- derived: DTD PNL / DTD PNL%, USD
    na_pnl_dtd  REAL,                   -- the N.A. bucket: closed positions'
    na_pnl_mtd  REAL,                   -- realized P&L + costs (currency,
    na_pnl_ytd  REAL,                   -- rollover, fixed) — the operation's
    note        TEXT                    -- cost of carry, tracked not attributed
) STRICT;

CREATE TABLE IF NOT EXISTS book_positions (
    snap_date   TEXT NOT NULL REFERENCES book_snapshots(snap_date)
                ON DELETE CASCADE,
    pair_tag    TEXT NOT NULL,          -- as written on the IMS ('IT 5'); 'UNTAGGED' if blank
    ticker_raw  TEXT NOT NULL,          -- verbatim, contract code and all
    root        TEXT NOT NULL,          -- ticker with the =U6-style token stripped: stable identity
    contract    TEXT,                   -- 'U6' etc; NULL for cash / options
    cap         TEXT,                   -- LARGE CAP / MID CAP as the IMS buckets it
    side        TEXT NOT NULL CHECK (side IN ('L','S')),
    qty         REAL NOT NULL CHECK (qty > 0),   -- lots (futures) / shares (cash)
    cost        REAL,                   -- avg entry, INR — column being removed from the export
    mv_pct      REAL,                   -- signed market value, fraction of NAV
    beta_mv_pct REAL,                   -- beta-adjusted MV%; options: delta-equivalent exposure
    pnl_dtd     REAL,                   -- USD, day
    pnl_dtd_trading REAL,               -- USD, intraday trading component
    pnl_mtd     REAL,                   -- USD, month-to-date
    pnl         REAL,                   -- USD, YTD per ticker — THE CHAIN BASIS
    note        TEXT,
    PRIMARY KEY (snap_date, pair_tag, ticker_raw)
) STRICT;

CREATE TABLE IF NOT EXISTS book_pair_reviews (
    id            INTEGER PRIMARY KEY,
    review_date   TEXT NOT NULL,
    pair_tag      TEXT NOT NULL,
    verdict       TEXT NOT NULL CHECK (verdict IN ('hold','add','trim','exit','watch')),
    thesis_intact INTEGER CHECK (thesis_intact IN (0,1)),
    note          TEXT NOT NULL CHECK (length(note) > 0),
    created_at    TEXT NOT NULL
) STRICT;
"""

RE_CONTRACT = re.compile(r"=([FGHJKMNQUVXZ]\d{1,2}|\d{1,2})(?=\s|$)")


def normalise_ticker(raw: str) -> tuple[str, str | None]:
    """'TATA=U6 IS Equity' -> ('TATA IS Equity', 'U6'). Option tickers carry
    no '=' token and pass through whole — each series its own identity."""
    raw = " ".join(str(raw).split())
    m = RE_CONTRACT.search(raw)
    if not m:
        return raw, None
    root = " ".join((raw[: m.start()] + raw[m.end():]).split())
    return root, m.group(1)


def display_name(root: str) -> str:
    return re.sub(r"\s+(IS|IN|IB)?\s*(Equity|Index|Comdty|Curncy)$",
                  "", root, flags=re.I).strip() or root


def _migrate(conn: sqlite3.Connection) -> None:
    """v1 tables (2026-09-05, pre-IMS-format) had avg_price/last_price/mv.
    Empty ones are dropped and recreated; a populated one refuses — data
    would be silently reshaped otherwise."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(book_positions)")}
    if cols and "mv_pct" not in cols:
        n = conn.execute("SELECT COUNT(*) FROM book_positions").fetchone()[0]
        if n:
            raise RuntimeError(
                "book_positions has v1 rows; migrate by hand before loading")
        conn.executescript(
            "DROP TABLE book_positions; DROP TABLE book_snapshots;")
        conn.commit()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(DDL)
    return conn


# --------------------------------------------------------------------------
# parse — the IMS tab-separated export -> canonical staging doc
# --------------------------------------------------------------------------

NUM_TAIL = ["mv_pct", "beta_mv_pct", "pnl_dtd", "pnl_dtd_pct",
            "pnl_dtd_trading", "pnl_mtd", "pnl_ytd", "pnl_mtd_pct",
            "pnl_ytd_pct", "gmv_pct"]


def _floats(cells: list[str]) -> list[float]:
    return [float(c) for c in cells if c.strip() != ""]


def parse_ims_tsv(text: str, date: str, source_file: str = "paste") -> dict:
    """The IMS export, verbatim, to a canonical staging doc.

    Row shapes (tab-separated):
      position   TICKER, [Group], [Cost], CAP, PAIR, QTY, LONG|SHORT, <10 nums>
                 (Cost optional — being removed from the export 2026-09-06 on;
                 an 11th trailing number is the spec's Momentum MV%, ignored)
      sector agg blank ticker, label in cell 2 ('N.A.' is the costs bucket)
      book total no ticker, no label, 10 nums — used for the cross-foot
    """
    positions, na_row, total_row, sector_rows = [], None, None, []
    warnings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        cells = line.split("\t")
        first = cells[0].strip()
        if first:                                   # ---- position row
            up = [c.strip().upper() for c in cells]
            try:
                i = next(k for k, c in enumerate(up) if c in ("LONG", "SHORT"))
            except StopIteration:
                raise ValueError(f"line {lineno}: ticker row without "
                                 f"LONG/SHORT flag: {first!r}")
            nums = _floats(cells[i + 1:])
            if len(nums) == len(NUM_TAIL) + 1:
                nums = nums[:len(NUM_TAIL)]         # Momentum MV% present
            if len(nums) != len(NUM_TAIL):
                raise ValueError(f"line {lineno} ({first}): {len(nums)} "
                                 f"numeric cells after LONG/SHORT, expected "
                                 f"{len(NUM_TAIL)}")
            qty = float(cells[i - 1])
            pair = cells[i - 2].strip()
            cap = cells[i - 3].strip()
            # Cost is whatever numeric sits between Group and CAP — absent is fine
            mid = _floats(cells[1:i - 3])
            if len(mid) > 1:
                raise ValueError(f"line {lineno} ({first}): {len(mid)} numeric "
                                 f"cells before CAP, expected Cost or nothing")
            d = dict(zip(NUM_TAIL, nums))
            positions.append({
                "ticker": first, "cap": cap, "pair": pair, "qty": qty,
                "side": "L" if up[i] == "LONG" else "S",
                "cost": mid[0] if mid else None, **d,
            })
        else:                                       # ---- aggregate rows
            label = next((c.strip() for c in cells[1:] if c.strip() and
                          not _is_num(c)), None)
            nums = [float(c) for c in cells if c.strip() and _is_num(c)]
            if label == "N.A.":
                na_row = dict(zip(NUM_TAIL, nums)) if len(nums) >= 10 else None
            elif label:
                sector_rows.append((label, nums))
            elif len(nums) == len(NUM_TAIL) or len(nums) == len(NUM_TAIL) + 1:
                total_row = dict(zip(NUM_TAIL, nums))

    if not positions:
        raise ValueError("no position rows found — not an IMS export?")

    # NAV: DTD / DTD% per the spec, median over usable rows
    navs = [p["pnl_dtd"] / p["pnl_dtd_pct"] for p in positions
            if p["pnl_dtd_pct"] and abs(p["pnl_dtd_pct"]) > 1e-12]
    nav = round(statistics.median(navs), 0) if navs else None
    if navs and (max(navs) - min(navs)) / statistics.median(navs) > 0.01:
        warnings.append(f"NAV disagrees across rows: {min(navs):,.0f} .. "
                        f"{max(navs):,.0f} — check the paste")

    # THE CROSS-FOOT — the transcription check. The export's own total row
    # must equal the sum of positions + N.A.; a silent copy slip fails here.
    if total_row:
        for k, tol in (("mv_pct", 1e-9), ("gmv_pct", 1e-9),
                       ("pnl_dtd", 0.02), ("pnl_mtd", 0.02), ("pnl_ytd", 0.02)):
            got = sum(p[k] for p in positions) + ((na_row or {}).get(k) or 0)
            want = total_row[k]
            if abs(got - want) > tol:
                raise ValueError(f"cross-foot FAILED on {k}: positions+N.A. "
                                 f"sum to {got!r}, total row says {want!r} — "
                                 f"a row is missing or mis-pasted")
    else:
        warnings.append("no book-total row — cross-foot skipped")

    return {
        "date": date, "source_file": source_file,
        "pnl_basis": "contract_itd", "nav": nav,
        "na": {k: (na_row or {}).get(k) for k in
               ("pnl_dtd", "pnl_mtd", "pnl_ytd")} if na_row else None,
        "positions": positions, "warnings": warnings,
        "n_sector_rows": len(sector_rows),
    }


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------

def load_snapshot(doc: dict | pathlib.Path, replace: bool = False,
                  conn: sqlite3.Connection | None = None) -> dict:
    """Persist one canonical staging doc (dict, or path to its JSON).
    Refuses rather than repairs: malformed input goes back to the parse."""
    own = conn is None
    conn = conn or connect()
    src_name = None
    if isinstance(doc, pathlib.Path):
        src_name = doc.name
        doc = json.loads(doc.read_text(encoding="utf-8"))

    date = doc.get("date") or ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError(f"date {date!r} is not YYYY-MM-DD")
    if date > dt.date.today().isoformat():
        raise ValueError(f"date {date} is in the future")
    basis = doc.get("pnl_basis", "contract_itd")
    if basis not in ("contract_itd", "daily"):
        raise ValueError(f"pnl_basis {basis!r}")
    rows = doc.get("positions") or []
    if not rows:
        raise ValueError("no positions — an empty book is a statement the "
                         "PM makes, not a file this loads silently")

    existing = conn.execute(
        "SELECT n_positions FROM book_snapshots WHERE snap_date=?",
        (date,)).fetchone()
    if existing and not replace:
        raise ValueError(f"snapshot {date} already loaded "
                         f"({existing['n_positions']} positions); "
                         f"re-run with --replace to overwrite")

    warnings = list(doc.get("warnings") or [])
    parsed = []
    seen: set[tuple[str, str]] = set()
    for i, p in enumerate(rows):
        raw = str(p.get("ticker") or "").strip()
        if not raw:
            raise ValueError(f"position {i}: no ticker")
        qty = p.get("qty")
        if qty is None:
            raise ValueError(f"{raw}: no qty")
        qty = float(qty)
        side = p.get("side")
        if side not in ("L", "S"):
            if qty == 0:
                raise ValueError(f"{raw}: qty 0 and no side — cannot infer")
            side = "L" if qty > 0 else "S"
        qty = abs(qty)
        if qty == 0:
            raise ValueError(f"{raw}: qty 0")
        pair = str(p.get("pair") or "").strip() or "UNTAGGED"
        if pair == "UNTAGGED":
            warnings.append(f"{raw}: no pair tag — filed under UNTAGGED")
        root, contract = normalise_ticker(raw)
        key = (pair, raw)
        if key in seen:
            raise ValueError(f"duplicate row for pair {pair!r} ticker {raw!r}")
        seen.add(key)
        parsed.append((date, pair, raw, root, contract, p.get("cap"), side,
                       qty, p.get("cost"), p.get("mv_pct"),
                       p.get("beta_mv_pct"), p.get("pnl_dtd"),
                       p.get("pnl_dtd_trading"), p.get("pnl_mtd"),
                       p.get("pnl_ytd", p.get("pnl")), p.get("note")))

    na = doc.get("na") or {}
    with conn:
        if existing:
            # explicit, not via ON DELETE CASCADE — cascade silently does
            # nothing on a connection whose foreign_keys pragma is off
            conn.execute("DELETE FROM book_positions WHERE snap_date=?", (date,))
            conn.execute("DELETE FROM book_snapshots WHERE snap_date=?", (date,))
        conn.execute(
            "INSERT INTO book_snapshots (snap_date, source_file, loaded_at, "
            "n_positions, pnl_basis, nav, na_pnl_dtd, na_pnl_mtd, na_pnl_ytd, "
            "note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (date, doc.get("source_file") or src_name or "?",
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             len(parsed), basis, doc.get("nav"), na.get("pnl_dtd"),
             na.get("pnl_mtd"), na.get("pnl_ytd"), doc.get("note")))
        conn.executemany(
            "INSERT INTO book_positions (snap_date, pair_tag, ticker_raw, "
            "root, contract, cap, side, qty, cost, mv_pct, beta_mv_pct, "
            "pnl_dtd, pnl_dtd_trading, pnl_mtd, pnl, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", parsed)
    out = {"date": date, "n_positions": len(parsed), "pnl_basis": basis,
           "nav": doc.get("nav"), "warnings": warnings}
    if own:
        conn.close()
    return out


# --------------------------------------------------------------------------
# the chain — rollover-proof P&L per (pair, root)
# --------------------------------------------------------------------------

# A roll observed across a snapshot gap wider than this many calendar days is
# flagged: the frozen P&L may miss the old contract's last days.
ROLL_GAP_DAYS = 7


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _chain_leg(rows: list[sqlite3.Row], basis: str,
               all_dates: list[str]) -> dict:
    """rows: one (pair, root)'s appearances, date-ascending. all_dates: every
    stored snapshot date in the window, ascending. See module docstring for
    the split rules (roll / absence / year boundary)."""
    if basis == "daily":
        total = sum(r["pnl"] or 0.0 for r in rows)
        return {"total": total, "realized": None, "live": None,
                "rolls": 0, "gap_risk": False, "last": rows[-1]}
    realized = 0.0
    rolls = 0
    gap_risk = False
    prev = None
    for r in rows:
        if prev is not None:
            d0, d1 = prev["snap_date"], r["snap_date"]
            absent = any(d0 < d < d1 for d in all_dates)
            rolled = (r["contract"] or "") != (prev["contract"] or "")
            year_reset = d0[:4] != d1[:4]           # YTD resets on Jan 1
            if rolled or absent or year_reset:
                realized += prev["pnl"] or 0.0
                if rolled:
                    rolls += 1
                    if (_date(d1) - _date(d0)).days > ROLL_GAP_DAYS:
                        gap_risk = True
        prev = r
    live = rows[-1]["pnl"] or 0.0
    return {"total": realized + live, "realized": realized, "live": live,
            "rolls": rolls, "gap_risk": gap_risk, "last": rows[-1]}


def _leg_rows(conn, pair: str, root: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM book_positions WHERE pair_tag=? AND root=? "
        "ORDER BY snap_date", (pair, root)).fetchall()


def pair_report(conn: sqlite3.Connection | None = None,
                as_of: str | None = None) -> dict:
    """Everything the Book tab and the daily_review chat display need."""
    own = conn is None
    conn = conn or connect()
    snaps = [r["snap_date"] for r in conn.execute(
        "SELECT snap_date FROM book_snapshots ORDER BY snap_date")]
    if not snaps:
        if own:
            conn.close()
        return {"as_of": None, "pairs": [], "closed": [], "snapshots": 0}
    as_of = as_of or snaps[-1]
    if as_of not in snaps:
        raise ValueError(f"no snapshot for {as_of}; have {snaps[-1]} latest")
    meta = conn.execute("SELECT * FROM book_snapshots WHERE snap_date=?",
                        (as_of,)).fetchone()
    basis, nav = meta["pnl_basis"], meta["nav"]
    hist = [d for d in snaps if d <= as_of]
    prev = hist[-2] if len(hist) > 1 else None

    all_pairs = [r["pair_tag"] for r in conn.execute(
        "SELECT DISTINCT pair_tag FROM book_positions WHERE snap_date<=? "
        "ORDER BY pair_tag", (as_of,))]
    live_pairs = {r["pair_tag"] for r in conn.execute(
        "SELECT DISTINCT pair_tag FROM book_positions WHERE snap_date=?",
        (as_of,))}

    def pair_total(pair: str, upto: str) -> float:
        roots = [r["root"] for r in conn.execute(
            "SELECT DISTINCT root FROM book_positions WHERE pair_tag=? "
            "AND snap_date<=?", (pair, upto))]
        dates = [d for d in snaps if d <= upto]
        t = 0.0
        for root in roots:
            rows = [r for r in _leg_rows(conn, pair, root)
                    if r["snap_date"] <= upto]
            t += _chain_leg(rows, basis, dates)["total"]
        return t

    pairs, closed = [], []
    for pair in all_pairs:
        dates = [r["snap_date"] for r in conn.execute(
            "SELECT DISTINCT snap_date FROM book_positions WHERE pair_tag=? "
            "AND snap_date<=? ORDER BY snap_date", (pair, as_of))]
        inception, last_seen = dates[0], dates[-1]
        total = pair_total(pair, as_of)

        if pair not in live_pairs:
            # leg names from the last snapshot that carried the pair — the tag
            # itself is internal bookkeeping and never printed on the page
            last_legs = conn.execute(
                "SELECT side, root FROM book_positions WHERE pair_tag=? AND "
                "snap_date=? ORDER BY side, root", (pair, last_seen)).fetchall()
            closed.append({"pair": pair, "inception": inception,
                           "last_seen": last_seen,
                           "days": (_date(last_seen) - _date(inception)).days,
                           "pnl_total": round(total, 2),
                           "long": [display_name(r["root"]) for r in last_legs
                                    if r["side"] == "L"],
                           "short": [display_name(r["root"]) for r in last_legs
                                     if r["side"] == "S"]})
            continue

        legs, rolls, gap_risk = [], 0, False
        for r in conn.execute(
                "SELECT * FROM book_positions WHERE pair_tag=? AND snap_date=? "
                "ORDER BY side, root", (pair, as_of)):
            rows = _leg_rows(conn, pair, r["root"])
            ch = _chain_leg([x for x in rows if x["snap_date"] <= as_of],
                            basis, hist)
            gap_risk = gap_risk or ch["gap_risk"]
            rolls += ch["rolls"]
            legs.append({
                "name": display_name(r["root"]), "root": r["root"],
                "ticker_now": r["ticker_raw"], "contract": r["contract"],
                "cap": r["cap"], "side": r["side"], "qty": r["qty"],
                "cost": r["cost"], "mv_pct": r["mv_pct"],
                "mv_usd": (round(r["mv_pct"] * nav, 0)
                           if r["mv_pct"] is not None and nav else None),
                "beta_mv_pct": r["beta_mv_pct"],
                "pnl_dtd": r["pnl_dtd"], "pnl_mtd": r["pnl_mtd"],
                "pnl_live": r["pnl"], "pnl_total": round(ch["total"], 2),
                "rolls": ch["rolls"], "note": r["note"],
            })
        gross_pct = sum(abs(x["mv_pct"]) for x in legs
                        if x["mv_pct"] is not None) or None
        net_pct = sum(x["mv_pct"] for x in legs
                      if x["mv_pct"] is not None) or None
        pairs.append({
            "pair": pair, "inception": inception,
            "days": (_date(as_of) - _date(inception)).days,
            "n_legs": len(legs),
            "long": [x["name"] for x in legs if x["side"] == "L"],
            "short": [x["name"] for x in legs if x["side"] == "S"],
            "pnl_total": round(total, 2),
            # the IMS's own day/month figures, summed over live legs — not
            # derived from the chain
            "pnl_dtd": round(sum(x["pnl_dtd"] or 0 for x in legs), 2),
            "pnl_mtd": round(sum(x["pnl_mtd"] or 0 for x in legs), 2),
            "gross_pct": gross_pct, "net_pct": net_pct,
            "gross_usd": (round(gross_pct * nav, 0)
                          if gross_pct and nav else None),
            "rolls": rolls, "gap_risk": gap_risk,
            "legs": legs,
        })

    pairs.sort(key=lambda p: -(p["pnl_total"] if p["pnl_total"] is not None else 0))
    out = {"as_of": as_of, "prev": prev, "pnl_basis": basis, "nav": nav,
           "na": {"pnl_dtd": meta["na_pnl_dtd"], "pnl_mtd": meta["na_pnl_mtd"],
                  "pnl_ytd": meta["na_pnl_ytd"]},
           "snapshots": len(hist), "pairs": pairs, "closed": closed,
           "book_pnl_total": round(sum(p["pnl_total"] for p in pairs)
                                   + sum(c["pnl_total"] for c in closed), 2)}
    if own:
        conn.close()
    return out


def reviews(conn: sqlite3.Connection | None = None,
            pair: str | None = None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    q = "SELECT * FROM book_pair_reviews"
    args: tuple = ()
    if pair:
        q += " WHERE pair_tag=?"
        args = (pair,)
    rows = [dict(r) for r in conn.execute(
        q + " ORDER BY review_date DESC, id DESC", args)]
    if own:
        conn.close()
    return rows


def add_review(pair: str, verdict: str, note: str,
               thesis_intact: bool | None = None,
               review_date: str | None = None,
               conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO book_pair_reviews (review_date, pair_tag, verdict, "
            "thesis_intact, note, created_at) VALUES (?,?,?,?,?,?)",
            (review_date or dt.date.today().isoformat(), pair, verdict,
             None if thesis_intact is None else int(thesis_intact), note,
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        rid = cur.lastrowid
    if own:
        conn.close()
    return rid


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_report(rep: dict) -> None:
    if not rep["as_of"]:
        print("no snapshots loaded")
        return
    nav = f"  NAV {rep['nav']:,.0f}" if rep.get("nav") else ""
    print(f"book as of {rep['as_of']}  ({rep['snapshots']} snapshots, "
          f"basis {rep['pnl_basis']}){nav}   "
          f"chained P&L {rep['book_pnl_total']:+,.0f}")
    na = rep.get("na") or {}
    if na.get("pnl_ytd") is not None:
        print(f"  N.A. (closed + costs): DTD {na['pnl_dtd']:+,.0f}  "
              f"MTD {na['pnl_mtd']:+,.0f}  YTD {na['pnl_ytd']:+,.0f}")
    for p in rep["pairs"]:
        flags = ("  ⚠gap" if p["gap_risk"] else "") + \
                (f"  rolls×{p['rolls']}" if p["rolls"] else "")
        print(f"  {p['pair']:<12} L {'/'.join(p['long']) or '—':<24} "
              f"S {'/'.join(p['short']) or '—':<24} "
              f"since {p['inception']} ({p['days']}d)  "
              f"P&L {p['pnl_total']:+,.0f}  day {p['pnl_dtd']:+,.0f}  "
              f"mtd {p['pnl_mtd']:+,.0f}{flags}")
    for c in rep["closed"]:
        print(f"  {c['pair']:<12} CLOSED {c['inception']} → {c['last_seen']} "
              f"({c['days']}d)  final P&L {c['pnl_total']:+,.0f}")


def _selftest() -> None:
    # parse cases -----------------------------------------------------------
    cases = {
        "TATA=U6 IS Equity": ("TATA IS Equity", "U6"),
        "TATA=V6 IS Equity": ("TATA IS Equity", "V6"),
        "NIFTY=Z6 Index":    ("NIFTY Index", "Z6"),
        "LTTS IN Equity":    ("LTTS IN Equity", None),
        "JSTL=1 IS Equity":  ("JSTL IS Equity", "1"),
        "HNDL=U26 IS Equity": ("HNDL IS Equity", "U26"),
        # option: no '=' token -> own root, per the PM's no-delta-netting rule
        "HNDL IS 09/30/26 C1000 Equity": ("HNDL IS 09/30/26 C1000 Equity", None),
    }
    for raw, want in cases.items():
        got = normalise_ticker(raw)
        assert got == want, f"{raw}: {got} != {want}"
    assert display_name("TATA IS Equity") == "TATA"

    # TSV parse: 2 positions + sector row + N.A. + total, cross-foot true ----
    def row(t, cost, cap, pair, q, ls, nums):
        c = [t, "", str(cost) if cost is not None else "", cap, pair,
             str(q), ls] + [repr(x) for x in nums]
        if cost is None:
            c.pop(2)                                # the cost-removed shape
        return "\t".join(c)
    # NUM_TAIL: mv,beta,dtd,dtd%,dtdtr,mtd,ytd,mtd%,ytd%,gmv  (NAV 3e6)
    n1 = [0.002, 0.0018, -30.0, -0.00001, 0, -60.0, 120.0, -0.00002, 0.00004, 0.002]
    n2 = [-0.003, -0.0027, 60.0, 0.00002, 0, 90.0, -30.0, 0.00003, -0.00001, 0.003]
    na = [0, 0, -3.0, -0.000001, 0, -6.0, -9.0, -0.000002, -0.000003, 0]
    tot = [a + b + c for a, b, c in zip(n1, n2, na)]
    tsv = "\n".join([
        "\t".join(["", "IT MIDCAP", "", "", "", "", ""] +
                  [repr(a + b) for a, b in zip(n1, n2)]),
        row("TCS=U6 IS Equity", 3100.5, "LARGE CAP", "IT 9", 2, "LONG", n1),
        row("WPRO=U6 IS Equity", None, "LARGE CAP", "IT 9", -4, "SHORT", n2),
        "\t".join(["", "N.A.", "", "", "", "", ""] + [repr(x) for x in na]),
        "\t".join(["", "", "", "", "", "", ""] + [repr(x) for x in tot]),
    ])
    doc = parse_ims_tsv(tsv, "2026-08-03", "selftest")
    assert len(doc["positions"]) == 2 and doc["na"]["pnl_ytd"] == -9.0
    assert doc["nav"] == 3000000.0, doc["nav"]
    assert doc["positions"][0]["cost"] == 3100.5
    assert doc["positions"][1]["cost"] is None      # cost-removed shape parses
    assert doc["positions"][1]["side"] == "S" and doc["positions"][1]["qty"] == -4
    # cross-foot rejection: drop a row and the total must not reconcile
    bad = "\n".join(tsv.splitlines()[:2] + tsv.splitlines()[3:])
    try:
        parse_ims_tsv(bad, "2026-08-03")
        raise AssertionError("cross-foot passed with a missing row")
    except ValueError as e:
        assert "cross-foot" in str(e)

    # chained P&L over an in-memory store ------------------------------------
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)

    def snap(date, positions, nav=3000000.0):
        load_snapshot({"date": date, "source_file": "selftest", "nav": nav,
                       "positions": positions}, conn=conn)

    P = lambda pair, tk, qty, ytd, dtd=0.0: {
        "pair": pair, "ticker": tk, "qty": qty, "pnl_ytd": ytd,
        "pnl_dtd": dtd, "pnl_mtd": ytd, "mv_pct": 0.002 * (1 if qty > 0 else -1)}
    # day 1: IT 5 long TCS / short INFO on U6; MET 1 cash
    snap("2026-08-03", [P("IT 5", "TCS=U6 IS Equity", 1, 0),
                        P("IT 5", "INFO=U6 IS Equity", -2, 0),
                        P("MET 1", "HNDL IN Equity", 500, 1000)])
    snap("2026-08-04", [P("IT 5", "TCS=U6 IS Equity", 1, 5000),
                        P("IT 5", "INFO=U6 IS Equity", -2, 2000),
                        P("MET 1", "HNDL IN Equity", 500, 1500)])
    # day 3: THE ROLL — U6 -> V6, YTD resets near zero
    snap("2026-08-05", [P("IT 5", "TCS=V6 IS Equity", 1, -300, dtd=-300),
                        P("IT 5", "INFO=V6 IS Equity", -2, 100, dtd=100),
                        P("MET 1", "HNDL IN Equity", 500, 1200)])
    # day 4: MET 1 gone (closed); IT 5 continues
    snap("2026-08-06", [P("IT 5", "TCS=V6 IS Equity", 1, 700),
                        P("IT 5", "INFO=V6 IS Equity", -2, -400)])

    rep = pair_report(conn, as_of="2026-08-06")
    it5 = next(p for p in rep["pairs"] if p["pair"] == "IT 5")
    assert it5["pnl_total"] == 7300.0, it5["pnl_total"]  # 5000+700, 2000-400
    assert it5["rolls"] == 2 and it5["inception"] == "2026-08-03"
    assert not it5["gap_risk"]
    met = next(c for c in rep["closed"] if c["pair"] == "MET 1")
    assert met["pnl_total"] == 1200.0 and met["last_seen"] == "2026-08-05"
    rep3 = pair_report(conn, as_of="2026-08-05")
    it5_3 = next(p for p in rep3["pairs"] if p["pair"] == "IT 5")
    assert it5_3["pnl_total"] == 6800.0
    assert it5_3["pnl_dtd"] == -200.0               # IMS's own day figures

    # ON-DEMAND CADENCE: 16 quiet days, same contracts -> NO split; MET 1
    # reappears after an absence -> genuine reopen, frozen 1200 survives
    snap("2026-08-22", [P("IT 5", "TCS=V6 IS Equity", 1, 900),
                        P("IT 5", "INFO=V6 IS Equity", -2, -500),
                        P("MET 1", "HNDL IN Equity", 500, 300)])
    rep = pair_report(conn)
    it5 = next(p for p in rep["pairs"] if p["pair"] == "IT 5")
    assert it5["pnl_total"] == 7400.0 and it5["rolls"] == 2
    met = next(p for p in rep["pairs"] if p["pair"] == "MET 1")
    assert met["pnl_total"] == 1500.0, met["pnl_total"]

    # YEAR BOUNDARY: cash position spans Jan 1, same root, YTD resets --------
    c3 = sqlite3.connect(":memory:")
    c3.row_factory = sqlite3.Row
    c3.executescript(DDL)
    load_snapshot({"date": "2025-12-30", "source_file": "t", "positions":
                   [P("X 1", "LTTS IN Equity", 100, 4000)]}, conn=c3)
    load_snapshot({"date": "2026-01-05", "source_file": "t", "positions":
                   [P("X 1", "LTTS IN Equity", 100, 250)]}, conn=c3)
    x1 = pair_report(c3)["pairs"][0]
    assert x1["pnl_total"] == 4250.0, x1["pnl_total"]   # 4000 frozen + 250

    # refusal paths (the GLOB lesson: accepting AND rejecting cases) ---------
    c2 = sqlite3.connect(":memory:")
    c2.row_factory = sqlite3.Row
    c2.executescript(DDL)
    d = {"date": "2026-08-06", "source_file": "x",
         "positions": [P("A", "X IN Equity", 1, 0)]}
    load_snapshot(d, conn=c2)
    try:
        load_snapshot(d, conn=c2)
        raise AssertionError("duplicate date loaded without --replace")
    except ValueError:
        pass
    load_snapshot(d, replace=True, conn=c2)
    assert any(p["pair"] == "A" for p in pair_report(c2)["pairs"])

    add_review("IT 5", "hold", "thesis intact; sized right", True, conn=conn)
    assert reviews(conn=conn, pair="IT 5")[0]["verdict"] == "hold"
    print("selftest OK — ticker/option parse, TSV parse (both cost shapes), "
          "NAV derivation, cross-foot accept+reject, roll chain, closed pair, "
          "cadence (no-split + reopen), year boundary, refusal paths, reviews")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--parse", metavar="TSV",
                    help="raw IMS export to parse, stage and load")
    ap.add_argument("--date", metavar="YYYY-MM-DD",
                    help="snapshot date (required with --parse)")
    ap.add_argument("--load", metavar="JSON", help="staged snapshot to persist")
    ap.add_argument("--replace", action="store_true",
                    help="allow overwriting an already-loaded date")
    ap.add_argument("--report", nargs="?", const="latest", metavar="DATE",
                    help="print the pair report (default: latest snapshot)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    elif a.parse:
        if not a.date:
            ap.error("--parse needs --date")
        raw = pathlib.Path(a.parse)
        doc = parse_ims_tsv(raw.read_text(encoding="utf-8"), a.date, raw.name)
        STAGING.mkdir(parents=True, exist_ok=True)
        staged = STAGING / f"{a.date}.json"
        staged.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        res = load_snapshot(staged, replace=a.replace)
        print(f"parsed {len(doc['positions'])} positions, staged {staged.name}, "
              f"loaded (NAV {res['nav']:,.0f})" if res.get("nav") else
              f"parsed and loaded {res['n_positions']} positions")
        for w in res["warnings"]:
            print(f"  ⚠ {w}")
        _print_report(pair_report())
    elif a.load:
        res = load_snapshot(pathlib.Path(a.load), replace=a.replace)
        print(f"loaded {res['date']}: {res['n_positions']} positions "
              f"(basis {res['pnl_basis']})")
        for w in res["warnings"]:
            print(f"  ⚠ {w}")
        _print_report(pair_report())
    elif a.report:
        _print_report(pair_report(
            as_of=None if a.report == "latest" else a.report))
    else:
        ap.print_help()
