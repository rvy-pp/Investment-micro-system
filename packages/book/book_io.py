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

TWO PRECISIONS (2026-09-30). The raw export prints full floats. The daily
routine instead copies the ON-SCREEN Portfolio grid: a header row, '%' and
',' formatted cells (to 0.01% of NAV and $1), sector labels with row counts
('STEEL (5)'), a 'Total' label, and a '# Stats: GAV 3.01m' comment. Both are
accepted; a display paste is cross-footed at rounding tolerance, checked
per sector against its printed count and subtotal, takes NAV from GAV, and
is recorded as precision 'display'. See DISPLAY_UNIT below.

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

-- The anchor/audit log: one row per position event, written at load time by
-- diffing each snapshot against its stored predecessor. 'entered', 'reopened'
-- and 'flipped' RESET the %-since-entry anchor; 'retagged' and 'resized'
-- explicitly do NOT (logged to prove continuity — the 2026-09-07 IT retag
-- silently re-anchored two legs before this table existed); 'closed' ends a
-- streak. Rebuild any time with --rebuild-log; rows are derived, never edited.
CREATE TABLE IF NOT EXISTS book_anchor_log (
    id         INTEGER PRIMARY KEY,
    snap_date  TEXT NOT NULL,
    root       TEXT NOT NULL,
    event      TEXT NOT NULL CHECK (event IN
               ('entered','reopened','flipped','closed','retagged','resized')),
    side       TEXT CHECK (side IN ('L','S')),   -- after the event; NULL when closed
    qty        REAL,
    cost       REAL,                             -- IMS Cost at the event row, if printed
    detail     TEXT,                             -- 'IT 5 -> IT 4', 'qty 2 -> 4', 'S -> L'
    created_at TEXT NOT NULL
) STRICT;

-- Entry-day OPEN per anchor streak — the %-since-entry base (PM 2026-09-07:
-- "keep average entry cost as the open price on entry day. The purpose is to
-- see if the pair has worked out in thesis"). Fetched once per (root, entry
-- day) by fetch_entry_anchors, guarded against the stored close of the same
-- date. Display falls back to the IMS avg cost, then the entry-day close,
-- when a row is missing here.
CREATE TABLE IF NOT EXISTS book_entry_anchors (
    root        TEXT NOT NULL,
    anchor_date TEXT NOT NULL,
    open_price  REAL NOT NULL CHECK (open_price > 0),
    source      TEXT NOT NULL,       -- the Yahoo symbol the open came from
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (root, anchor_date)
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

-- Kelly edges, entered on the Book tab (PM, 2026-09-25). Append-only: the
-- latest row per pair is the live edge, earlier rows are what the edge WAS
-- and when. ret_pct NULL = the edge was cleared. packages/book/kelly.py
-- reads and writes it; nothing a pillar reads.
CREATE TABLE IF NOT EXISTS book_kelly_edges (
    id            INTEGER PRIMARY KEY,
    pair          TEXT NOT NULL CHECK (length(pair) > 0),
    ret_pct       REAL,
    horizon_weeks REAL NOT NULL CHECK (horizon_weeks > 0),
    note          TEXT NOT NULL CHECK (length(note) > 0),
    set_at        TEXT NOT NULL
) STRICT;

-- CLOSED CONTRACTS — the FLAT / FLAT_IN_MONTH / FLAT_IN_YEAR rows of a
-- full-year export (2026-09-28 on). Not positions: every contract of the
-- year that is no longer held, with its FINAL realised P&L as the IMS
-- prints it. PM, 2026-09-29: "Add the ytds simply" — with these stored, a
-- pair's YTD is the IMS's own YTD column summed over its live AND closed
-- contracts, and the roll ledger (_ytd_leg) is not needed for that
-- snapshot. Kept apart from book_positions because a snapshot's positions
-- are the FULL BOOK and absence there means CLOSED.
CREATE TABLE IF NOT EXISTS book_closed_contracts (
    snap_date   TEXT NOT NULL,
    pair_tag    TEXT NOT NULL,
    ticker_raw  TEXT NOT NULL,
    root        TEXT NOT NULL,
    contract    TEXT,
    flat_kind   TEXT NOT NULL,
    pnl_dtd     REAL,
    pnl_mtd     REAL,
    pnl_ytd     REAL,
    PRIMARY KEY (snap_date, pair_tag, ticker_raw)
) STRICT;
"""

RE_CONTRACT = re.compile(r"=([FGHJKMNQUVXZ]\d{1,2}|\d{1,2})(?=\s|$)")

# CUSTODY-WRAPPER ALIASES — hand-verified, and an ALLOW-LIST on purpose.
#
# On 2026-09-09 the IMS relabelled the four cash CFD legs without the
# positions changing at all: 'VAML IN Equity' -> 'VAML IN CFD PPB PTF',
# same for DALBHARA, SYRMA and LTTS. Same qty, same side, same pair tag,
# and — the decisive evidence — a CONTINUOUS YTD: VAML ran -236.74 (09-08)
# -> +11.65 (09-09) on a +252.41 day. A genuine close-and-reopen resets YTD
# to that day's DTD, and none of the four did.
#
# WHAT IT COST BEFORE THIS EXISTED (caught by diffing the live roster
# against the stored snapshot, not by any test): the root is the chain's
# identity, so four relabelled legs read as four CLOSES plus four ENTRIES —
# the since-inception P&L restarts at zero and _entry_anchor re-anchors the
# %-since-entry to today, the exact failure the 2026-09-07 IT retag caused.
#
# It is an allow-list, like tape.CONFIRMED_ACTIONS, and must stay one: a
# generic "strip any trailing venue words" rule would cheerfully merge two
# genuinely different instruments, and nothing downstream would complain.
# ticker_raw keeps the verbatim string, so the CFD fact is never lost.
ROOT_ALIASES = {"CFD PPB PTF": "Equity"}


def normalise_ticker(raw: str) -> tuple[str, str | None]:
    """'TATA=U6 IS Equity' -> ('TATA IS Equity', 'U6'). Option tickers carry
    no '=' token and pass through whole — each series its own identity.
    A ROOT_ALIASES custody wrapper is folded back to its canonical form."""
    raw = " ".join(str(raw).split())
    for wrapper, canon in ROOT_ALIASES.items():
        if raw.upper().endswith(" " + wrapper.upper()):
            raw = raw[: -len(wrapper)].rstrip() + " " + canon
            break
    m = RE_CONTRACT.search(raw)
    if not m:
        return raw, None
    root = " ".join((raw[: m.start()] + raw[m.end():]).split())
    return root, m.group(1)


# AN OPTION IS NOT AN EQUITY LEG, AND ITS FIRST TOKEN IS THE TRAP. On
# 2026-09-29 the book carried `LTM IS 10/27/26 C4100 Equity` (a call, 10
# lots) beside `LTM=V6 IS Equity` (the future, -4). normalise_ticker keeps
# them as separate ROOTS, correctly — but every consumer that resolves a leg
# by `root.split()[0]` gets `LTM` for both and merges them: the Book view
# folded the call into the LTM short (qty 14, side flipped to L, since-entry
# re-anchored to 0.0%), and the index admitted LTIMindtree as a LONG member
# and dropped it from the short side (roster 16/16 -> 17/15). Plausible on
# the page, wrong, and nothing raised. So option roots are recognised by
# their own shape and kept OUT of token-keyed aggregation: they are hedges
# on a leg, reported beside the book, never netted into it (the IMS-Spec
# no-delta-netting rule at the display layer).
RE_OPTION = re.compile(r"\b(IS|IN|IB)\s+\d{2}/\d{2}/\d{2}\s+[CP]\d+(\.\d+)?\b", re.I)


def is_option_root(root: str) -> bool:
    """'HZ IS 10/27/26 P570 Equity' -> True; 'HZ IS Equity' -> False."""
    return RE_OPTION.search(str(root)) is not None


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


def _add_na_lumped_column(conn: sqlite3.Connection) -> None:
    """book_snapshots.na_lumped — 1 when the export's N.A. bucket carries a
    FLAT_* member (the positions-only export's `FLAT_POS` row, 2026-09-29):
    every closed contract of the year LUMPED into N.A. with the costs."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(book_snapshots)")}
    if "na_lumped" not in cols:
        conn.execute("ALTER TABLE book_snapshots ADD COLUMN na_lumped INTEGER")
        conn.commit()


def _add_total_columns(conn: sqlite3.Connection) -> None:
    """The export's own BOOK-TOTAL row, stored per snapshot (2026-09-29).

    PM: "actual ytd is 3065" — the IMS's book YTD. The pairs cannot sum to
    it after a roll: the dying contracts' last sessions move into N.A. as one
    lump with no per-stock split. So the Book strip's headline YTD reads the
    IMS's own total row instead of a sum we compute. Older snapshots predate
    the columns and read NULL; the page falls back to the pair sum for them.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(book_snapshots)")}
    for c in ("total_pnl_dtd", "total_pnl_mtd", "total_pnl_ytd"):
        if cols and c not in cols:
            conn.execute(f"ALTER TABLE book_snapshots ADD COLUMN {c} REAL")
    conn.commit()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(DDL)
    _add_total_columns(conn)
    _add_na_lumped_column(conn)
    return conn


# --------------------------------------------------------------------------
# parse — the IMS tab-separated export -> canonical staging doc
# --------------------------------------------------------------------------

# The IMS direction flag. FLAT_IN_YEAR arrived with the full export on
# 2026-09-09 and is NOT a position — see the row-kind note in
# parse_ims_tsv, which is where the reasoning lives.
#
# FLAT arrived on 2026-09-21, THE FIRST OBSERVED ROLL: HZ=U6 and VEDL=U6
# printed as `0  FLAT` with DTD/MTD/YTD all non-zero (they traded that
# session), beside the fresh HZ=V6 / VEDL=V6 rows. It is the SAME-DAY form
# of FLAT_IN_YEAR — a contract that is no longer a position but still
# carries its final realised P&L, and it cross-foots. The only difference
# is that DTD and MTD are not yet zero. Routed identically; refusing it
# would have blocked the one snapshot the roll check exists for.
#
# FLAT_IN_MONTH arrived on 2026-09-28 (the September roll): the U6 contracts,
# closed this month, print `0  FLAT_IN_MONTH` with DTD 0 or small, MTD and
# YTD non-zero. Same kind again — a closed contract carrying its realised
# P&L, cross-footing — so it routes with the other two.
DIRECTIONS = ("LONG", "SHORT", "FLAT_IN_YEAR", "FLAT_IN_MONTH", "FLAT")
FLAT_FLAGS = ("FLAT_IN_YEAR", "FLAT_IN_MONTH", "FLAT")

NUM_TAIL = ["mv_pct", "beta_mv_pct", "pnl_dtd", "pnl_dtd_pct",
            "pnl_dtd_trading", "pnl_mtd", "pnl_ytd", "pnl_mtd_pct",
            "pnl_ytd_pct", "gmv_pct"]


def _num(s: str) -> float:
    """One numeric cell. The raw export prints plain floats
    (0.009707266986559803); the ON-SCREEN grid prints display-rounded text
    ('0.97%', '1,109', '-0.02%', '0%'). Both read to the same units: a
    trailing % is divided by 100 so every % stays a fraction of NAV."""
    t = s.strip().replace(",", "").replace("\u2212", "-")
    if t.endswith("%"):
        return float(t[:-1]) / 100.0
    return float(t)


def _is_display(s: str) -> bool:
    """A cell in the display-rounded shape (a % sign or a thousands comma)."""
    t = s.strip()
    return t.endswith("%") or bool(re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", t))


def _floats(cells: list[str]) -> list[float]:
    return [_num(c) for c in cells if c.strip() != ""]


# ---- DISPLAY-ROUNDED PASTES (PM, 2026-09-30: "accept rounded numbers.
# Output in the end will be more or less same") ------------------------------
#
# The daily routine copies the IMS Portfolio grid off the screen, which
# prints % to 2 decimals (0.01% of NAV) and dollars to whole units. Those
# rows cannot cross-foot to 1e-9 — each row is rounded on its own and the
# total is rounded once — so a display paste gets a tolerance sized to the
# rounding it actually carries: half a display unit per summed row, plus one
# for the total. What that gives up (a dropped row smaller than the slack
# can pass the sum check) is taken back by a check the raw export never
# needed: the grid prints each sector's row COUNT ("STEEL (5)"), and every
# sector's members must match that count exactly and cross-foot to its
# subtotal. NAV cannot come from DTD / DTD% at 2 decimals (616 / 0.02% reads
# 3.08m), so it comes from the stats header's GAV when the paste carries it.
# Downstream the YTD ledger works on cumulative deltas, which telescope, so
# the rounding stays within about $0.5 per leg and never compounds.
DISPLAY_UNIT = {"mv_pct": 1e-4, "gmv_pct": 1e-4, "beta_mv_pct": 1e-4,
                "pnl_dtd": 1.0, "pnl_mtd": 1.0, "pnl_ytd": 1.0,
                "pnl_dtd_trading": 1.0}
RE_GAV = re.compile(r"\bGAV\s+([\d.,]+)\s*([kmb]?)\b", re.I)
RE_COUNT = re.compile(r"^(.*?)\s*\((\d+)\)$")


def _display_tol(key: str, n_rows: int) -> float:
    return 0.5 * DISPLAY_UNIT[key] * (n_rows + 1) + 1e-9


# ---- THE NUMERIC TAIL HAS TWO ORDERS, AND THE FILE DOES NOT SAY WHICH ----
#
# Through 2026-09-25 the export printed  MV% βMV% DTD DTD% DTD_trading MTD YTD
# MTD% YTD% GMV%  (NUM_TAIL, the IMS-Spec order). On 2026-09-29 — the mass
# September roll, 28 contracts to =V6 in one paste — the same columns arrived
# as  MV% βMV% DTD DTD% MTD YTD DTD_trading MTD% YTD% GMV%: the trading column
# moved from slot 5 to slot 7. There is no header row, and the cross-foot
# CANNOT catch it — every column still sums to its total under either order —
# so reading it in the spec order files MTD as trading and YTD as MTD, passes
# every guard, and the tab shows a plausible book that is wrong. That is the
# SILENT_BUGS shape exactly, which is why the order is DETECTED and never
# assumed:
#
#   trading is ZERO on every row that did not trade that day, so whichever
#   of slot 5 / slot 7 is zero on a clear majority of position rows is the
#   trading column. Neither clearly zero, or both, is REFUSED — a paste the
#   loader cannot read is a paste that goes back to the PM, never a guess.
#
# Verified on the 2026-09-29 export before any code was written: the LTM
# call opened that day reads -177.12 / -177.12 / -176.66 in slots 5/6/7 —
# a same-day position must have MTD == YTD, so 5 and 6 are MTD and YTD and
# the near-equal 7 is the intraday trading figure — and the ALUMINIUM sector
# row's slot 5 (32.71) is exactly the sum of its three legs' slot-5 values.
TAIL_TRADING_FIRST = "trading_mtd_ytd"   # NUM_TAIL as written (IMS-Spec)
TAIL_TRADING_LAST = "mtd_ytd_trading"    # the order since 2026-09-29
_ORDER_MAJORITY = 0.6


def detect_tail_order(positions: list[dict]) -> str:
    """Which of the two tail orders a parsed paste is in, from the data.

    `positions` are records already mapped with NUM_TAIL (spec order), so
    slot 5 sits in `pnl_dtd_trading` and slot 7 in `pnl_ytd`. Raises on an
    ambiguous paste rather than picking one.
    """
    n = len(positions)
    if n == 0:
        raise ValueError("cannot detect the numeric-tail order: no position rows")
    z5 = sum(1 for p in positions if abs(p["pnl_dtd_trading"]) < 1e-9)
    z7 = sum(1 for p in positions if abs(p["pnl_ytd"]) < 1e-9)
    first_ok = z5 / n >= _ORDER_MAJORITY and z7 / n < _ORDER_MAJORITY
    last_ok = z7 / n >= _ORDER_MAJORITY and z5 / n < _ORDER_MAJORITY
    if first_ok:
        return TAIL_TRADING_FIRST
    if last_ok:
        return TAIL_TRADING_LAST
    raise ValueError(
        f"cannot tell which numeric column is DTD_trading: slot 5 is zero on "
        f"{z5}/{n} position rows and slot 7 on {z7}/{n} — the export's column "
        f"order is ambiguous; re-export or state the order, do not load")


def remap_tail_trading_last(rec: dict) -> None:
    """In place: a record read in spec order, re-keyed for the trading-last
    order. slot 5 -> MTD, slot 6 -> YTD, slot 7 -> DTD_trading."""
    s5, s6, s7 = rec["pnl_dtd_trading"], rec["pnl_mtd"], rec["pnl_ytd"]
    rec["pnl_mtd"], rec["pnl_ytd"], rec["pnl_dtd_trading"] = s5, s6, s7


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
    flat, na_members = [], []
    warnings: list[str] = []
    display = False          # any display-rounded cell seen -> display paste
    gav = None               # from a '# Stats: GAV 3.01m' comment line
    header = None            # the grid's own header row, when copied
    sector = None            # [label, declared_count, nums, members]
    sectors: list[list] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):           # ---- comment / stats
            m = RE_GAV.search(line)
            if m:
                mult = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2).lower()]
                gav = float(m.group(1).replace(",", "")) * mult
            continue
        cells = line.split("\t")
        first = cells[0].strip()
        if first.lower() == "ticker":               # ---- grid header row
            header = [c.strip().lower() for c in cells]
            continue
        if any(_is_display(c) for c in cells):
            display = True
        if first:                                   # ---- position row
            up = [c.strip().upper() for c in cells]
            try:
                i = next(k for k, c in enumerate(up) if c in DIRECTIONS)
            except StopIteration:
                raise ValueError(f"line {lineno}: ticker row without a "
                                 f"direction flag ({'/'.join(DIRECTIONS)}): "
                                 f"{first!r}")
            nums = _floats(cells[i + 1:])
            if len(nums) == len(NUM_TAIL) + 1:
                nums = nums[:len(NUM_TAIL)]         # Momentum MV% present
            if len(nums) != len(NUM_TAIL):
                raise ValueError(f"line {lineno} ({first}): {len(nums)} "
                                 f"numeric cells after the direction flag, "
                                 f"expected {len(NUM_TAIL)}")
            qty = _num(cells[i - 1])
            pair = cells[i - 2].strip()
            cap = cells[i - 3].strip()
            # Cost is whatever numeric sits between Group and CAP — absent is fine
            mid = _floats(cells[1:i - 3])
            if len(mid) > 1:
                raise ValueError(f"line {lineno} ({first}): {len(mid)} numeric "
                                 f"cells before CAP, expected Cost or nothing")
            d = dict(zip(NUM_TAIL, nums))
            rec = {"ticker": first, "cap": cap, "pair": pair, "qty": qty,
                   "cost": mid[0] if mid else None, **d}

            # --- THREE ROW KINDS, AND ONLY THE FIRST IS A POSITION --------
            #
            # The 2026-09-09 paste is the FULL IMS sheet; the first three
            # snapshots were positions-only. Two extra kinds arrived with
            # it, and both must stay OUT of `positions`, because a snapshot
            # is the FULL BOOK and absence from it means CLOSED — anything
            # admitted here becomes a live leg of a pair.
            #
            # FLAT_IN_YEAR: a contract closed EARLIER THIS YEAR. qty 0,
            #   MV 0, DTD 0, MTD 0, and a non-zero YTD that is its final
            #   realised P&L. Mostly the pre-capture monthly rolls (=N6 Jul
            #   and =Q6 Aug against the live =U6 Sep), plus expired options.
            #   Real money, and it does cross-foot — but it is PRE-CAPTURE
            #   CARRY, so it is recorded and never mixed into the chained
            #   since-inception figure (the `carry:` rule in book.yaml). It
            #   would also fail the schema outright: qty > 0, side in L/S.
            #
            #   IT ALSO SETTLES THE STANDING WARNING IN THE DOCSTRING ABOVE.
            #   HNDL=U6 carries YTD -417.44 while HNDL=Q6 (-2,212.65) and
            #   HNDL=N6 (-321.52) sit on their own rows: a fresh contract
            #   does NOT inherit the dead one's P&L, so the chain does not
            #   double-count. One correction to that docstring — the dead
            #   contract's P&L does not fall into N.A. either. N.A. YTD is
            #   -285.44 against +430.32 of FLAT_IN_YEAR; it stays on its own
            #   row, which the earlier exports simply did not print.
            #
            # N.A. MEMBERS: the constituents of the costs bucket (USD/INR
            #   Curncy; the $3.0m cash line alone is 99.8% of book MV%).
            #   The N.A. AGGREGATE row already equals them to machine
            #   precision, so counting both double-counts the whole bucket.
            if pair.upper() == "N.A.":
                na_members.append({**rec, "flat_kind": up[i]}
                                  if up[i] in FLAT_FLAGS else rec)
            elif up[i] in FLAT_FLAGS:
                # FLAT (closed TODAY, DTD/MTD non-zero) and FLAT_IN_YEAR
                # (closed earlier, DTD 0) are one kind: not a position,
                # real money, cross-foots. The `flat_kind` is kept so the
                # roll check can tell a same-day close from old carry.
                flat.append({**rec, "flat_kind": up[i]})
            else:
                positions.append(
                    {**rec, "side": "L" if up[i] == "LONG" else "S"})
            if sector is not None:          # the stored record, so a re-key
                sector[3].append(               # of the tail reaches it too
                    (na_members if pair.upper() == "N.A." else
                     flat if up[i] in FLAT_FLAGS else positions)[-1])
        else:                                       # ---- aggregate rows
            label = next((c.strip() for c in cells[1:] if c.strip() and
                          not _is_num(c)), None)
            nums = [_num(c) for c in cells if c.strip() and _is_num(c)]
            declared = None
            if label:
                m = RE_COUNT.match(label)
                if m:                               # 'STEEL (5)' -> 'STEEL', 5
                    label, declared = m.group(1).strip(), int(m.group(2))
            if label and label.lower() == "total":  # the grid labels its total
                label = None
            if label:
                sector = [label, declared, dict(zip(NUM_TAIL, nums)), []]
                sectors.append(sector)
            else:
                sector = None
            if label == "N.A.":
                na_row = dict(zip(NUM_TAIL, nums)) if len(nums) >= 10 else None
            elif label:
                sector_rows.append((label, nums))
            elif len(nums) == len(NUM_TAIL) or len(nums) == len(NUM_TAIL) + 1:
                total_row = dict(zip(NUM_TAIL, nums))

    if not positions:
        raise ValueError("no position rows found — not an IMS export?")

    # ---- THE COLUMN ORDER, DETECTED — see TAIL_TRADING_* above ------------
    # Every record so far is keyed in NUM_TAIL (spec) order. If the paste is
    # in the trading-LAST order that arrived on 2026-09-29, re-key EVERY
    # numeric record the same way — positions, FLAT rows, N.A. members, the
    # N.A. aggregate and the total row — so the cross-foot below compares
    # like with like. An ambiguous paste raises here and loads nothing.
    tail_order = detect_tail_order(positions)
    if tail_order == TAIL_TRADING_LAST:
        for rec in positions + flat + na_members:
            remap_tail_trading_last(rec)
        if na_row:
            remap_tail_trading_last(na_row)
        if total_row:
            remap_tail_trading_last(total_row)
        for sec in sectors:
            if len(sec[2]) >= len(NUM_TAIL):
                remap_tail_trading_last(sec[2])
        warnings.append(
            "numeric tail is in the trading-LAST order (MV% βMV% DTD DTD% MTD "
            "YTD DTD_trading …, seen since 2026-09-29): MTD/YTD/trading "
            "re-keyed from slots 5/6/7 — detected from the data, not assumed")

    # A copied header names the order outright; the data must agree with it.
    if header and "dtd trading pnl" in header and "mtd pnl" in header:
        said = (TAIL_TRADING_LAST
                if header.index("dtd trading pnl") > header.index("mtd pnl")
                else TAIL_TRADING_FIRST)
        if said != tail_order:
            raise ValueError(f"header says the numeric tail is {said} but the "
                             f"data reads as {tail_order} — do not load")

    # NAV: DTD / DTD% per the spec, median over usable rows
    navs = [p["pnl_dtd"] / p["pnl_dtd_pct"] for p in positions
            if p["pnl_dtd_pct"] and abs(p["pnl_dtd_pct"]) > 1e-12]
    nav = round(statistics.median(navs), 0) if navs else None
    if display:
        # 2-decimal % makes DTD/DTD% useless (±25% per row); GAV is 3 s.f.
        if gav:
            nav = round(gav, 0)
            warnings.append(f"display-rounded paste: NAV {nav:,.0f} taken from "
                            f"the header GAV (3 significant figures)")
        else:
            best = max((p for p in positions if abs(p["pnl_ytd_pct"]) > 0),
                       key=lambda p: abs(p["pnl_ytd_pct"]), default=None)
            nav = (round(best["pnl_ytd"] / best["pnl_ytd_pct"], -3)
                   if best else None)
            warnings.append("display-rounded paste with no GAV line: NAV is a "
                            "rough YTD/YTD% estimate — USD columns approximate")
    elif navs and (max(navs) - min(navs)) / statistics.median(navs) > 0.01:
        warnings.append(f"NAV disagrees across rows: {min(navs):,.0f} .. "
                        f"{max(navs):,.0f} — check the paste")

    # THE CROSS-FOOT — the transcription check. The export's own total row
    # must equal the sum of positions + N.A.; a silent copy slip fails here.
    if total_row:
        for k, tol in (("mv_pct", 1e-9), ("gmv_pct", 1e-9),
                       ("pnl_dtd", 0.02), ("pnl_mtd", 0.02), ("pnl_ytd", 0.02)):
            # positions + FLAT_IN_YEAR + the N.A. AGGREGATE — never the
            # N.A. members, which the aggregate already sums. Verified on
            # the 2026-09-09 export: all five keys reconcile, ~1e-13 on the
            # dollar columns.
            got = (sum(p[k] for p in positions) + sum(f[k] for f in flat)
                   + ((na_row or {}).get(k) or 0))
            want = total_row[k]
            if display:
                tol = _display_tol(k, len(positions) + len(flat)
                                   + (1 if na_row else 0))
            if abs(got - want) > tol:
                raise ValueError(f"cross-foot FAILED on {k}: positions+N.A. "
                                 f"sum to {got!r}, total row says {want!r} — "
                                 f"a row is missing or mis-pasted")
    else:
        warnings.append("no book-total row — cross-foot skipped")

    # DISPLAY PASTES ONLY: every sector's printed row count, and its subtotal.
    if display:
        for label, declared, snums, members in sectors:
            if declared is not None and declared != len(members):
                raise ValueError(f"sector {label}: header says {declared} rows, "
                                 f"paste has {len(members)} — a row is missing "
                                 f"or duplicated")
            if len(snums) < len(NUM_TAIL) or not members:
                continue
            for k in ("mv_pct", "gmv_pct", "pnl_dtd", "pnl_mtd", "pnl_ytd"):
                got = sum(r[k] for r in members)
                if abs(got - snums[k]) > _display_tol(k, len(members)):
                    raise ValueError(f"sector {label} cross-foot FAILED on {k}: "
                                     f"rows sum to {got!r}, subtotal says "
                                     f"{snums[k]!r}")
        if not any(sec[1] is not None for sec in sectors):
            warnings.append("display-rounded paste without '(n)' sector counts "
                            "— only the loose total cross-foot guarded it")
        warnings.append(
            "display-rounded paste (on-screen grid): % to 0.01% of NAV, USD to "
            "$1 — cross-foot run at rounding tolerance plus per-sector row "
            "counts; figures agree with the raw export to within rounding")

    if flat:
        same_day = [f for f in flat if f.get("flat_kind") == "FLAT"]
        older = [f for f in flat if f.get("flat_kind") != "FLAT"]
        if older:
            warnings.append(
                f"{len(older)} FLAT_IN_YEAR rows (contracts closed earlier "
                f"this year), YTD {sum(f['pnl_ytd'] for f in older):+,.0f} — "
                f"recorded as pre-capture carry, NOT chained")
        if same_day:
            warnings.append(
                f"{len(same_day)} FLAT rows (contracts closed TODAY: "
                + ", ".join(f['ticker'] for f in same_day)
                + f"), final YTD {sum(f['pnl_ytd'] for f in same_day):+,.0f}"
                f" — recorded, NOT chained; the chain freezes the LAST-SEEN"
                f" YTD, so compare the two on a roll")
    if na_members:
        warnings.append(
            f"{len(na_members)} N.A. member rows folded into the bucket "
            f"aggregate (counting both would double-count it)")
    aliased = sorted({p['ticker'] for p in positions
                      if normalise_ticker(p['ticker'])[0] != p['ticker']
                      and RE_CONTRACT.search(p['ticker']) is None})
    if aliased:
        warnings.append("custody wrapper aliased to keep one chain root: "
                        + ", ".join(aliased))

    return {
        "date": date, "source_file": source_file,
        "pnl_basis": "contract_itd", "nav": nav,
        "na": {k: (na_row or {}).get(k) for k in
               ("pnl_dtd", "pnl_mtd", "pnl_ytd")} if na_row else None,
        # the export's own book-total row (already re-keyed if the tail was
        # in the trading-last order) — the IMS's book YTD, stored as printed
        "total": {k: (total_row or {}).get(k) for k in
                  ("pnl_dtd", "pnl_mtd", "pnl_ytd")} if total_row else None,
        "positions": positions, "warnings": warnings,
        "n_sector_rows": len(sector_rows),
        # Recorded, never chained — see the row-kind note above.
        "flat_in_year": flat, "na_members": na_members,
        # N.A. carries the year's closed contracts LUMPED (FLAT_POS) rather
        # than listing them — see pair_report's carried basis
        "na_lumped": any(m.get("flat_kind") for m in na_members),
        # Which of the two numeric-tail orders this paste arrived in —
        # DETECTED (see TAIL_TRADING_*), recorded so the staging file says
        # how its own numbers were read.
        "tail_order": tail_order,
        # 'display' = copied off the on-screen grid (rounded); 'full' = export
        "precision": "display" if display else "full",
    }


def _is_num(s: str) -> bool:
    try:
        _num(s)
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
            conn.execute("DELETE FROM book_closed_contracts WHERE snap_date=?", (date,))
            conn.execute("DELETE FROM book_snapshots WHERE snap_date=?", (date,))
        conn.execute(
            "INSERT INTO book_snapshots (snap_date, source_file, loaded_at, "
            "n_positions, pnl_basis, nav, na_pnl_dtd, na_pnl_mtd, na_pnl_ytd, "
            "note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (date, doc.get("source_file") or src_name or "?",
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             len(parsed), basis, doc.get("nav"), na.get("pnl_dtd"),
             na.get("pnl_mtd"), na.get("pnl_ytd"), doc.get("note")))
        if any(r[1] == "na_lumped" for r in
               conn.execute("PRAGMA table_info(book_snapshots)")):
            conn.execute("UPDATE book_snapshots SET na_lumped=? WHERE snap_date=?",
                         (1 if doc.get("na_lumped") else 0, date))
        tot = doc.get("total") or {}
        if tot and any(r[1] == "total_pnl_ytd" for r in
                       conn.execute("PRAGMA table_info(book_snapshots)")):
            conn.execute(
                "UPDATE book_snapshots SET total_pnl_dtd=?, total_pnl_mtd=?, "
                "total_pnl_ytd=? WHERE snap_date=?",
                (tot.get("pnl_dtd"), tot.get("pnl_mtd"), tot.get("pnl_ytd"), date))
        conn.executemany(
            "INSERT INTO book_positions (snap_date, pair_tag, ticker_raw, "
            "root, contract, cap, side, qty, cost, mv_pct, beta_mv_pct, "
            "pnl_dtd, pnl_dtd_trading, pnl_mtd, pnl, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", parsed)
        closed_rows = []
        for f in doc.get("flat_in_year") or []:
            raw = str(f.get("ticker") or "").strip()
            root, contract = normalise_ticker(raw)
            closed_rows.append((date, str(f.get("pair") or "").strip() or "UNTAGGED",
                                raw, root, contract, f.get("flat_kind") or "FLAT_IN_YEAR",
                                f.get("pnl_dtd"), f.get("pnl_mtd"), f.get("pnl_ytd")))
        conn.executemany(
            "INSERT INTO book_closed_contracts (snap_date, pair_tag, ticker_raw, "
            "root, contract, flat_kind, pnl_dtd, pnl_mtd, pnl_ytd) "
            "VALUES (?,?,?,?,?,?,?,?,?)", closed_rows)
    events = _log_anchor_events(conn, date)
    nxt = conn.execute("SELECT MIN(snap_date) FROM book_snapshots "
                       "WHERE snap_date>?", (date,)).fetchone()[0]
    if nxt:
        # a backfill load changes the successor's predecessor — re-diff that
        # seam too, or its logged events describe a gap that no longer exists
        _log_anchor_events(conn, nxt)
    out = {"date": date, "n_positions": len(parsed), "pnl_basis": basis,
           "nav": doc.get("nav"), "warnings": warnings,
           "anchor_events": events}
    if own:
        conn.close()
    return out


def _log_anchor_events(conn: sqlite3.Connection, date: str) -> list[dict]:
    """Diff snapshot `date` against its stored predecessor and (re)write the
    book_anchor_log rows for `date`. Idempotent — delete-and-rewrite per
    date, so --replace and --rebuild-log never duplicate. Returns the events.

    Exists because of the 2026-09-07 IT retag: MPHL (IT 5 -> IT 4) and TELX
    (IT 5 -> IT 6) moved tags with nothing recording it, and the %-since-entry
    anchor silently reset. The anchor bug is fixed in _entry_anchor; this log
    is the audit trail so the NEXT unrecorded change is visible, not inferred.
    """
    snaps = [r[0] for r in conn.execute(
        "SELECT snap_date FROM book_snapshots ORDER BY snap_date")]
    if date not in snaps:
        return []
    i = snaps.index(date)
    prev = snaps[i - 1] if i else None
    cur = {r["root"]: r for r in conn.execute(
        "SELECT * FROM book_positions WHERE snap_date=?", (date,))}
    prv = {r["root"]: r for r in conn.execute(
        "SELECT * FROM book_positions WHERE snap_date=?", (prev,))} if prev else {}
    ev: list[tuple] = []
    for root, r in sorted(cur.items()):
        p = prv.get(root)
        if p is None:
            seen = conn.execute(
                "SELECT 1 FROM book_positions WHERE root=? AND snap_date<? "
                "LIMIT 1", (root, date)).fetchone()
            ev.append((root, "reopened" if seen else "entered",
                       r["side"], r["qty"], r["cost"], None))
            continue
        if p["side"] != r["side"]:
            ev.append((root, "flipped", r["side"], r["qty"], r["cost"],
                       f"{p['side']} -> {r['side']}"))
        if p["pair_tag"] != r["pair_tag"]:
            ev.append((root, "retagged", r["side"], r["qty"], r["cost"],
                       f"{p['pair_tag']} -> {r['pair_tag']}"))
        if (p["qty"] or 0) != (r["qty"] or 0):
            ev.append((root, "resized", r["side"], r["qty"], r["cost"],
                       f"qty {p['qty']:g} -> {r['qty']:g}"))
    for root, p in sorted(prv.items()):
        if root not in cur:
            ev.append((root, "closed", None, None, None,
                       f"was {p['side']} qty {p['qty']:g}"))
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM book_anchor_log WHERE snap_date=?", (date,))
        conn.executemany(
            "INSERT INTO book_anchor_log (snap_date, root, event, side, qty, "
            "cost, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
            [(date, *e, now) for e in ev])
    return [{"root": e[0], "event": e[1], "side": e[2], "qty": e[3],
             "cost": e[4], "detail": e[5]} for e in ev]


def rebuild_anchor_log(conn: sqlite3.Connection | None = None) -> int:
    """Replay every stored snapshot into book_anchor_log (derived rows only)."""
    own = conn is None
    conn = conn or connect()
    n = 0
    for r in conn.execute("SELECT snap_date FROM book_snapshots ORDER BY 1"):
        n += len(_log_anchor_events(conn, r[0]))
    if own:
        conn.close()
    return n


def _pick_anchor_open(day: tuple | None, stored_close: float | None) -> float:
    """Validate a fetched (open, close) for the anchor date, or raise.

    The wrong-symbol class (ZN=F was the T-note): a fetched number being
    PLAUSIBLE proves nothing. The identity check is the fetched CLOSE
    matching the close already stored in `prices` for the same entity and
    date to 0.5% — same instrument, same day, by construction. The open is
    then also range-checked against that close (a >20% intraday gap on an
    Indian large/mid cap is a data error, not a market day).
    """
    if not day or day[0] is None:
        raise ValueError("no open printed for the anchor date")
    o, c = day
    if stored_close is None:
        raise ValueError("no stored close to verify the symbol against")
    if c is None or abs(c / stored_close - 1) > 0.005:
        raise ValueError(f"fetched close {c} vs stored {stored_close} "
                         f"— wrong symbol or date, refused")
    if abs(o / stored_close - 1) > 0.20:
        raise ValueError(f"open {o} implausible against close {stored_close}")
    return float(o)


def fetch_entry_anchors(conn: sqlite3.Connection | None = None) -> dict:
    """Fetch and persist the OPEN of each live leg's entry day.

    PM rule 2026-09-07 (superseding the avg-cost anchor of the same day):
    the %-since-entry base is the OPEN PRICE ON THE ENTRY DAY — the column
    asks "has the pair worked out in thesis since the trade went on", which
    the IMS avg cost cannot answer: adds and trims blend into it, and for a
    position entered before capture it dates from before the book was
    observed (SYRMA read +18.7% from its long-ago cost, +12.7% from its
    entry-day open — same trade, different question).

    Opens come from the same Yahoo chart endpoint the closes in `prices`
    come from, resolved specs/book.yaml ticker_map -> yahoo_prices
    CANDIDATES, and every row passes _pick_anchor_open's stored-close
    identity check before persisting. Failures are reported and skipped —
    the display then falls back to IMS cost, then the entry-day close.
    Writes book_entry_anchors only; never touches `prices`.
    """
    import urllib.request
    own = conn is None
    conn = conn or connect()
    sys.path.insert(0, str(REPO / "packages" / "adapters"))
    import yahoo_prices as yp
    import yaml
    cfg = yaml.safe_load((REPO / "specs" / "book.yaml")
                         .read_text(encoding="utf-8")) or {}
    tmap = cfg.get("ticker_map") or {}

    latest = conn.execute(
        "SELECT MAX(snap_date) FROM book_snapshots").fetchone()[0]
    out = {"fetched": [], "have": [], "failed": []}
    if not latest:
        if own:
            conn.close()
        return out
    snaps = [r[0] for r in conn.execute(
        "SELECT snap_date FROM book_snapshots ORDER BY 1")]
    chart_cache: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT DISTINCT root FROM book_positions WHERE snap_date=? "
            "ORDER BY root", (latest,)):
        root = r["root"]
        anch = _entry_anchor(conn, root, latest, snaps)
        adate = anch["snap_date"]
        if conn.execute("SELECT 1 FROM book_entry_anchors WHERE root=? AND "
                        "anchor_date=?", (root, adate)).fetchone():
            out["have"].append(root)
            continue
        tok = (root.split() or [""])[0]
        eid = tmap.get(tok) or tmap.get(root)
        sym = (yp.CANDIDATES.get(eid) or [(None, None)])[0][0] if eid else None
        if not sym:
            out["failed"].append((root, "no entity/symbol mapping"))
            continue
        try:
            if sym not in chart_cache:
                days_back = (dt.date.today() - _date(adate)).days
                rng = "3mo" if days_back < 80 else "1y"
                req = urllib.request.Request(
                    yp.CHART.format(sym=sym, rng=rng),
                    headers={"User-Agent": yp.UA})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    doc = json.load(resp)
                res = doc["chart"]["result"][0]
                q = res["indicators"]["quote"][0] or {}
                byday = {}
                for ts, o, c in zip(res.get("timestamp") or [],
                                    q.get("open") or [], q.get("close") or []):
                    d = dt.datetime.fromtimestamp(
                        ts, dt.timezone.utc).date().isoformat()
                    byday[d] = (o, c)
                chart_cache[sym] = byday
            stored = conn.execute(
                "SELECT close FROM prices WHERE entity_id=? AND date=?",
                (eid, adate)).fetchone()
            o = _pick_anchor_open(chart_cache[sym].get(adate),
                                  stored["close"] if stored else None)
            with conn:
                conn.execute(
                    "INSERT INTO book_entry_anchors (root, anchor_date, "
                    "open_price, source, fetched_at) VALUES (?,?,?,?,?)",
                    (root, adate, o, sym, dt.datetime.now(dt.timezone.utc)
                     .isoformat(timespec="seconds")))
            out["fetched"].append((root, adate, o))
        except Exception as e:                      # noqa: BLE001 — report, never guess
            out["failed"].append((root, str(e)))
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


# YTD_ROLL_TOL: at a roll the ledger below advances by the DAY figure, not by
# the new contract's YTD. The two AGREE when the roll happened on that very
# session — a fresh contract's first day IS its whole accrual. A gap between
# them means one of two things and both need a human, so it is flagged and
# never corrected: either the roll happened on a session that was never
# snapshotted (the dying contract's tail is then under-captured, the existing
# `gap_risk` case), or the new ticker CARRIES the old contract's P&L — the
# case this repo has always flagged as ASSUMED AND UNVERIFIED, and the one
# that makes a YTD-column chain silently DOUBLE COUNT.
YTD_ROLL_TOL = 1.0                          # USD


def _ytd_leg(rows: list[sqlite3.Row], all_dates: list[str],
              basis: str = "contract_itd") -> dict:
    """A running CALENDAR-year-to-date ledger for one (pair, root) that WE own.

    PM instruction 2026-09-18: the Book tab shows YTD, not the day figure,
    "which you calculate daily — the input I give expires after rolling."

    The ledger is seeded ONCE from the IMS YTD column and then never reads it
    as a level again; every later snapshot only ever ADDS an increment. That
    is what makes it roll-proof: a column that resets cannot reset a number it
    is no longer being read into.

    Which increment, and why each one:

      same contract, same year -> += (r.pnl - prev.pnl)
          The YTD column is CUMULATIVE, so its delta spans every session in
          between INCLUDING ones that were never snapshotted. Measured on the
          live book: summing the DAY column across the stored snapshots gives
          +4,124.78 where the cumulative delta gives +3,774.02 — a 350.75 hole
          that is exactly Monday 2026-09-14, an NSE session with no snapshot.
          Accumulating the day figure would have lost it silently. Do not.

      contract changed (roll) -> += r.pnl_dtd
          The new ticker's YTD restarts, so its delta against the old one is
          meaningless. The DAY figure is not a level and cannot reset, so it
          is the safe bridge — and unlike the new contract's YTD it CANNOT
          double count if the fresh ticker turns out to carry the old P&L.
          Falls back to r.pnl when the day figure is missing.

      leg absent from an intervening snapshot (closed, then REOPENED)
                              -> += r.pnl
          Not a roll. The closed position's realized P&L left for the N.A.
          bucket, so the reopened ticker's YTD is genuinely its own accrual
          and cannot double count. It also covers more than one day, so here
          the level beats the day figure.

      year boundary -> the ledger RESETS, because that is what YTD means.
          Seeds from r.pnl on a cash line (correct: it is the new year's
          accrual) but from r.pnl_dtd when the contract ALSO changed, so that
          a December-entered contract cannot import December into January.
          UNVERIFIED until the first January — flagged, not trusted.

    Returns the ledger plus the flags that a human has to look at.
    """
    if basis == "daily":                  # a day-summing basis has no levels
        return {"ytd": sum(r["pnl"] or 0.0 for r in rows), "flags": []}
    flags: list[str] = []
    ledger = rows[0]["pnl"] or 0.0        # seed: carries pre-capture P&L, per
    prev = rows[0]                        # PM ruling 2026-09-18 (true YTD)
    for r in rows[1:]:
        d0, d1 = prev["snap_date"], r["snap_date"]
        rolled = (r["contract"] or "") != (prev["contract"] or "")
        absent = any(d0 < d < d1 for d in all_dates)
        year_reset = d0[:4] != d1[:4]
        dtd, ytd = r["pnl_dtd"], r["pnl"]
        # NO FLAGS (PM, 2026-09-29): "why is there question marks in ytd? Add
        # the ytds simply, let it be. I just need a rough estimate. Add the
        # last ytd with todays change." The arithmetic below already IS that
        # rule; what went is the `ytd?` chip it used to raise. Known and
        # accepted: on a roll across an un-snapshotted session the day figure
        # misses that session (the 2026-09-29 mass roll: Monday's V6 accrual,
        # ~$931 book-wide), so the figure is a rough YTD, as asked.
        # A ROLL BRIDGES WITH THE NEW CONTRACT'S OWN YTD, not the day figure
        # (PM, 2026-09-29: "I did miss a few uploads in between"). The day
        # figure covers one session; the new contract's YTD covers every
        # session since it opened, INCLUDING ones never pasted. On the
        # 2026-09-29 mass roll the contracts opened on Monday, which had no
        # paste, and the day figure dropped ~$931 of their Monday P&L.
        # The day figure was chosen as a guard against a fresh ticker
        # CARRYING the old contract's P&L; that was verified NOT to happen on
        # all 28 observed rolls (MTD == YTD on every =V6 row, 2026-09-21 and
        # 2026-09-29), so the guard is retired. If a future roll ever shows a
        # new contract's MTD != YTD, this line double counts — check that.
        if year_reset:
            ledger = (ytd or 0.0)
        elif rolled:
            ledger += ytd if ytd is not None else (dtd or 0.0)
        elif absent:
            # closed and REOPENED — not a roll. The old position's realized
            # P&L left for N.A., so the new ticker's YTD is genuinely its own
            # accrual since the reopen: no double-count risk, and it covers
            # more than the day figure does. Take the level, not the day.
            ledger += (ytd or 0.0)
        else:
            ledger += (ytd or 0.0) - (prev["pnl"] or 0.0)
        prev = r
    return {"ytd": ledger, "flags": flags}


def _leg_rows(conn, pair: str, root: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM book_positions WHERE pair_tag=? AND root=? "
        "ORDER BY snap_date", (pair, root)).fetchall()


def _entry_anchor(conn, root: str, as_of: str,
                  snaps: list[str]) -> sqlite3.Row:
    """The row whose date/cost anchor %-since-entry for this root.

    PM rule (2026-09-07): the anchor is the day-entered price and it stays
    put through resizes and pair-tag changes; it resets only when the
    direction flips or the root sits out a stored snapshot (closed for a
    day). Keyed on ROOT across tags ON PURPOSE — the first version keyed on
    (pair_tag, root), so the 2026-09-07 IT retag (MPHL IT 5->IT 4, TELX
    IT 5->IT 6) silently re-anchored both legs on that day's close and
    printed ret 0.0%; caught by the PM reading the pair %s as wrong, not by
    any test. Absence is judged on STORED SNAPSHOT DATES, the chain's own
    convention — a quiet fortnight between runs must not reset an anchor.
    """
    rows = conn.execute(
        "SELECT snap_date, side, cost FROM book_positions WHERE root=? "
        "AND snap_date<=? ORDER BY snap_date", (root, as_of)).fetchall()
    dates = [d for d in snaps if d <= as_of]
    anchor = rows[-1]
    for i in range(len(rows) - 1, 0, -1):
        cur, prv = rows[i], rows[i - 1]
        if prv["side"] != cur["side"] or any(
                prv["snap_date"] < d < cur["snap_date"] for d in dates):
            break
        anchor = prv
    return anchor


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

    # ---- THE YTD BASIS -----------------------------------------------------
    # A FULL-YEAR export (FLAT_IN_YEAR rows present, 2026-09-28 on) lists
    # every contract of the year, closed ones included, so a pair's YTD is
    # just the IMS YTD column summed over its live and closed contracts (PM,
    # 2026-09-29: "Add the ytds simply"). No roll bridging, nothing to go
    # wrong at a roll. Closed rows attach to the live leg with the same ROOT
    # (HZ=U6 -> HZ=V6); a closed OPTION attaches to its underlying's live
    # future in the same tag (first token); anything else stays at pair
    # level as `closed_unmatched`, named, never guessed onto a leg. Older,
    # positions-only snapshots keep the _ytd_leg ledger.
    def _closed_at(d):
        return [dict(r) for r in conn.execute(
            "SELECT * FROM book_closed_contracts WHERE snap_date=?", (d,))]

    def _full_year(rows):
        return any(r["flat_kind"] in ("FLAT_IN_YEAR", "FLAT_IN_MONTH") for r in rows)

    closed_rows = _closed_at(as_of)
    ims_sum = _full_year(closed_rows)
    ytd_basis = "ims_sum" if ims_sum else "ledger"
    carried = {"pnl_ytd": 0.0, "pnl_mtd": 0.0}
    # ---- CARRIED: a positions-only export after a full-year one ----------
    # The 2026-09-29 export lists live positions only and LUMPS every closed
    # contract of the year, with the costs, into one N.A. row (`FLAT_POS`,
    # +9,581). A closed contract's P&L is FINAL, so each one's YTD is carried
    # from the latest full-year snapshot of the same year (09-28's listing),
    # plus any same-day FLAT rows since, plus the last-seen YTD of anything
    # live since the base that has left the book without a closed row. The
    # carried total is then taken back OUT of N.A., so N.A. is left as the
    # costs alone and the book still sums to the IMS total row exactly.
    # Checked on 09-29: 09-28 closed +10,067.14 with N.A. -488.57 is +9,578.57
    # against FLAT_POS +9,581.19 — the 2.62 is the day's currency move.
    if not ims_sum and "na_lumped" in meta.keys() and meta["na_lumped"]:
        base = next((d for d in reversed(hist[:-1])
                     if d[:4] == as_of[:4] and _full_year(_closed_at(d))), None)
        if base:
            keyed: dict = {}
            span = [d for d in hist if base <= d <= as_of]
            for d in span:
                for r in _closed_at(d):
                    keyed[(r["pair_tag"], r["ticker_raw"])] = {**r, "_d": d}
            # keyed on the ROOT, never ticker_raw: on 09-29 the four cash legs
            # came back as `... IN Equity` after `... IN CFD PPB PTF` on 09-28,
            # and a ticker_raw test read all four as having LEFT the book and
            # carried their YTD (-4,370) out of N.A. twice over (N.A. showed
            # +3,885 against a true ~-486). The book total still matched, which
            # is why only the split could show it.
            live_now = {(r["pair_tag"], r["root"]) for r in conn.execute(
                "SELECT pair_tag, root FROM book_positions WHERE snap_date=?",
                (as_of,))}
            for d in span[:-1]:
                closed_since = {(c["pair_tag"], c["root"]) for c in keyed.values()
                                if c["_d"] >= d}
                for r in conn.execute(
                        "SELECT * FROM book_positions WHERE snap_date=?", (d,)):
                    k = (r["pair_tag"], r["root"])
                    if k in live_now or k in closed_since:
                        continue
                    k = (r["pair_tag"], r["ticker_raw"])
                    keyed[k] = {"snap_date": d, "pair_tag": r["pair_tag"],
                                "ticker_raw": r["ticker_raw"], "root": r["root"],
                                "contract": r["contract"], "flat_kind": "LEFT_BOOK",
                                "pnl_dtd": 0.0, "pnl_mtd": r["pnl_mtd"],
                                "pnl_ytd": r["pnl"], "_d": d}
            closed_rows = []
            for r in keyed.values():
                if r["_d"] != as_of:
                    # from an earlier day: no DTD today, and its MTD only
                    # counts inside the month it was earned
                    r["pnl_dtd"] = 0.0
                    if r["_d"][:7] != as_of[:7]:
                        r["pnl_mtd"] = 0.0
                    carried["pnl_ytd"] += r["pnl_ytd"] or 0.0
                    carried["pnl_mtd"] += r["pnl_mtd"] or 0.0
                closed_rows.append(r)
            ims_sum = True
            ytd_basis = "ims_sum_carried"
    closed_by_tag: dict = {}
    for r in (closed_rows if ims_sum else []):
        closed_by_tag.setdefault(r["pair_tag"], []).append(r)

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
            yl = _ytd_leg([x for x in rows if x["snap_date"] <= as_of],
                          hist, basis)
            gap_risk = gap_risk or ch["gap_risk"]
            rolls += ch["rolls"]
            anch = _entry_anchor(conn, r["root"], as_of, snaps)
            ao = conn.execute(
                "SELECT open_price FROM book_entry_anchors WHERE root=? AND "
                "anchor_date=?", (r["root"], anch["snap_date"])).fetchone()
            legs.append({
                "name": display_name(r["root"]), "root": r["root"],
                "ticker_now": r["ticker_raw"], "contract": r["contract"],
                "cap": r["cap"], "side": r["side"], "qty": r["qty"],
                # the price anchor for %-since-entry, in precedence order:
                # entry_open (the entry day's OPEN — PM rule 2026-09-07: "the
                # purpose is to see if the pair has worked out in thesis"),
                # then entry_cost (the IMS avg cost at the anchor capture),
                # then the close on first_seen. _entry_anchor keeps the
                # anchor DATE fixed through resizes and retags; it resets
                # only on a flip or a day out of the book.
                "first_seen": anch["snap_date"], "entry_cost": anch["cost"],
                "entry_open": ao["open_price"] if ao else None,
                "cost": r["cost"], "mv_pct": r["mv_pct"],
                "mv_usd": (round(r["mv_pct"] * nav, 0)
                           if r["mv_pct"] is not None and nav else None),
                "beta_mv_pct": r["beta_mv_pct"],
                "pnl_dtd": r["pnl_dtd"], "pnl_mtd": r["pnl_mtd"],
                "pnl_live": r["pnl"], "pnl_total": round(ch["total"], 2),
                # OUR ledger, not the IMS YTD column — see _ytd_leg; on a
                # full-year export replaced below by the IMS sum
                "pnl_ytd": round(yl["ytd"], 2), "ytd_flags": yl["flags"],
                "rolls": ch["rolls"], "note": r["note"],
            })
        unmatched = []
        if ims_sum:
            for x in legs:
                x["pnl_ytd"] = x["pnl_live"] or 0.0      # the IMS YTD column
                x["closed_ytd"] = x["closed_mtd"] = x["closed_dtd"] = 0.0
                x["closed_contracts"] = []
                x["ytd_flags"] = []
            for c in closed_by_tag.get(pair, []):
                tok = (c["root"].split() or [""])[0]
                hit = next((x for x in legs if x["root"] == c["root"]), None)
                if hit is None and is_option_root(c["root"]):
                    hit = next((x for x in legs if not is_option_root(x["root"])
                                and (x["root"].split() or [""])[0] == tok), None)
                if hit is None:
                    unmatched.append({"ticker": c["ticker_raw"], "root": c["root"],
                                      "flat_kind": c["flat_kind"],
                                      "pnl_dtd": c["pnl_dtd"] or 0.0,
                                      "pnl_mtd": c["pnl_mtd"] or 0.0,
                                      "pnl_ytd": c["pnl_ytd"] or 0.0})
                    continue
                hit["closed_ytd"] += c["pnl_ytd"] or 0.0
                hit["closed_mtd"] += c["pnl_mtd"] or 0.0
                hit["closed_dtd"] += c["pnl_dtd"] or 0.0
                hit["closed_contracts"].append(c["ticker_raw"])
            for x in legs:
                x["pnl_ytd"] = round(x["pnl_ytd"] + x["closed_ytd"], 2)
                x["pnl_mtd"] = (x["pnl_mtd"] or 0.0) + x["closed_mtd"]
                x["pnl_dtd"] = (x["pnl_dtd"] or 0.0) + x["closed_dtd"]
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
            # the calendar-YTD ledger we maintain (replaced the day figure on
            # the page, PM 2026-09-18). Flags ride with it and are printed.
            "pnl_ytd": round(sum(x["pnl_ytd"] or 0 for x in legs), 2),
            "ytd_flags": [f for x in legs for f in (x["ytd_flags"] or [])],
            "gross_pct": gross_pct, "net_pct": net_pct,
            "gross_usd": (round(gross_pct * nav, 0)
                          if gross_pct and nav else None),
            "rolls": rolls, "gap_risk": gap_risk,
            "legs": legs, "closed_unmatched": unmatched,
        })
        if unmatched:
            for k in ("pnl_ytd", "pnl_mtd", "pnl_dtd"):
                pairs[-1][k] = round(pairs[-1][k] + sum(u[k] for u in unmatched), 2)

    pairs.sort(key=lambda p: -(p["pnl_total"] if p["pnl_total"] is not None else 0))
    out = {"as_of": as_of, "prev": prev, "pnl_basis": basis, "nav": nav,
           "na": {"pnl_dtd": meta["na_pnl_dtd"],
                  "pnl_mtd": (meta["na_pnl_mtd"] - carried["pnl_mtd"]
                              if meta["na_pnl_mtd"] is not None else None),
                  "pnl_ytd": (meta["na_pnl_ytd"] - carried["pnl_ytd"]
                              if meta["na_pnl_ytd"] is not None else None)},
           # the IMS's own book-total row for the as_of snapshot (NULL on
           # snapshots loaded before 2026-09-29) — the headline YTD
           "ims_total": {k: (meta[f"total_{k}"] if f"total_{k}" in meta.keys()
                             else None) for k in ("pnl_dtd", "pnl_mtd", "pnl_ytd")},
           "snapshots": len(hist), "pairs": pairs, "closed": closed,
           # "ims_sum" on a full-year export (YTD = IMS column summed over
           # live + closed contracts), "ledger" otherwise — see above
           "ytd_basis": ytd_basis,
           # the IMS's own N.A. figures; `na` above has the carried closed
           # contracts taken back out on the carried basis
           "na_ims": {"pnl_dtd": meta["na_pnl_dtd"], "pnl_mtd": meta["na_pnl_mtd"],
                      "pnl_ytd": meta["na_pnl_ytd"]},
           "closed_carried": {k: round(v, 2) for k, v in carried.items()},
           # closed contracts in a tag with NO live position today — their
           # YTD would otherwise fall out of every pair
           "closed_tags": [{"pair": t, "pnl_ytd": round(sum(r["pnl_ytd"] or 0
                            for r in rows), 2),
                            "tickers": [r["ticker_raw"] for r in rows]}
                           for t, rows in sorted(closed_by_tag.items())
                           if t not in live_pairs],
           "book_pnl_total": round(sum(p["pnl_total"] for p in pairs)
                                   + sum(c["pnl_total"] for c in closed), 2),
           # the as_of snapshot's position events (entered/reopened/flipped
           # reset the %-since-entry anchor; retagged/resized do not; closed
           # ends a streak) — the page and --report say them out loud
           "anchor_log": [{"root": r["root"], "name": display_name(r["root"]),
                           "event": r["event"], "side": r["side"],
                           "qty": r["qty"], "cost": r["cost"],
                           "detail": r["detail"]}
                          for r in conn.execute(
                              "SELECT * FROM book_anchor_log WHERE snap_date=? "
                              "ORDER BY event, root", (as_of,))]}
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
              f"P&L {p['pnl_total']:+,.0f}  ytd {p['pnl_ytd']:+,.0f}  "
              f"mtd {p['pnl_mtd']:+,.0f}{flags}")
    for c in rep["closed"]:
        print(f"  {c['pair']:<12} CLOSED {c['inception']} → {c['last_seen']} "
              f"({c['days']}d)  final P&L {c['pnl_total']:+,.0f}")
    log = rep.get("anchor_log") or []
    if log and rep["snapshots"] > 1:      # first snapshot: every root 'entered'
        print(f"  events {rep['as_of']}:")
        for e in log:
            d = f" ({e['detail']})" if e.get("detail") else ""
            print(f"    {e['event']:<9} {e['name']}{d}")


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

    # ---- the FULL export shape (arrived 2026-09-09) -----------------------
    # FLAT_IN_YEAR rows, N.A. member rows and a custody-wrapper rename all
    # landed in one paste. ACCEPTANCE tests, not only rejection ones: the
    # GLOB lesson is that a guard which only ever refuses looks correct
    # while silently refusing everything valid.
    fl = [0, 0, 0, 0, 0, 0, 77.0, 0, 0.000026, 0]   # closed earlier in year
    m1 = [0.5, 0, -1.0, -0.0000003, 0, -2.0, -4.0, -0.0000007, -0.0000013, 0.5]
    m2 = [0.25, 0, -2.0, -0.0000007, 0, -4.0, -5.0, -0.0000013, -0.0000017, 0.25]
    nab = [a + b for a, b in zip(m1, m2)]           # bucket == its members
    tot2 = [a + b + c + d for a, b, c, d in zip(n1, n2, fl, nab)]
    full = "\n".join([
        row("TCS=U6 IS Equity", None, "LARGE CAP", "IT 9", 2, "LONG", n1),
        row("TCS=Q6 IS Equity", None, "LARGE CAP", "IT 9", 0, "FLAT_IN_YEAR", fl),
        row("WPRO=U6 IS Equity", None, "LARGE CAP", "IT 9", -4, "SHORT", n2),
        "\t".join(["", "N.A.", "", "", "", "", ""] + [repr(x) for x in nab]),
        row("USD Curncy", None, "N.A.", "N.A.", 3000000, "LONG", m1),
        row("INR Curncy", None, "N.A.", "N.A.", 1000, "LONG", m2),
        "\t".join(["", "", "", "", "", "", ""] + [repr(x) for x in tot2]),
    ])
    d2 = parse_ims_tsv(full, "2026-09-09", "selftest")   # must ACCEPT
    assert len(d2["positions"]) == 2, d2["positions"]    # not 3, not 5
    assert len(d2["flat_in_year"]) == 1 and len(d2["na_members"]) == 2
    assert d2["flat_in_year"][0]["pnl_ytd"] == 77.0
    assert d2["na"]["pnl_ytd"] == -9.0                   # bucket, once
    assert {p["ticker"] for p in d2["positions"]} == {
        "TCS=U6 IS Equity", "WPRO=U6 IS Equity"}
    # ...and the cross-foot must still BITE with the new kinds present
    try:
        parse_ims_tsv("\n".join(full.splitlines()[1:]), "2026-09-09")
        raise AssertionError("cross-foot passed with the FLAT row dropped")
    except ValueError as e:
        assert "cross-foot" in str(e)

    # ---- the SAME-DAY FLAT row (arrived 2026-09-21, the first roll) -------
    # A contract closed on the paste's own session prints `0  FLAT` with
    # DTD/MTD/YTD all NON-ZERO — unlike FLAT_IN_YEAR, whose DTD is 0. It
    # must be accepted, kept OUT of positions, and counted in the
    # cross-foot; and a ticker row with no flag at all must still refuse.
    fd = [0, 0, 5.22, 0.0000017, 217.68, 475.35, 1659.09, 0.00016, 0.00055, 0]
    tot3 = [a + b + c + d for a, b, c, d in zip(n1, n2, fd, nab)]
    roll = "\n".join([
        row("TCS=U6 IS Equity", None, "LARGE CAP", "IT 9", 2, "LONG", n1),
        row("HZ=U6 IS Equity", None, "LARGE CAP", "ZINC 1", 0, "FLAT", fd),
        row("WPRO=U6 IS Equity", None, "LARGE CAP", "IT 9", -4, "SHORT", n2),
        "\t".join(["", "N.A.", "", "", "", "", ""] + [repr(x) for x in nab]),
        "\t".join(["", "", "", "", "", "", ""] + [repr(x) for x in tot3]),
    ])
    d3 = parse_ims_tsv(roll, "2026-09-21", "selftest")   # must ACCEPT
    assert len(d3["positions"]) == 2, d3["positions"]
    assert len(d3["flat_in_year"]) == 1
    assert d3["flat_in_year"][0]["flat_kind"] == "FLAT"
    assert d3["flat_in_year"][0]["pnl_ytd"] == 1659.09
    assert d3["flat_in_year"][0]["pnl_dtd"] == 5.22      # same-day: DTD != 0
    assert any("closed TODAY" in w for w in d3["warnings"]), d3["warnings"]
    try:   # drop the FLAT row: the cross-foot must bite on DTD as well as YTD
        parse_ims_tsv("\n".join(roll.splitlines()[:1] + roll.splitlines()[2:]),
                      "2026-09-21")
        raise AssertionError("cross-foot passed with the same-day FLAT row dropped")
    except ValueError as e:
        assert "cross-foot" in str(e)
    try:   # REJECTION: a flag the IMS has never printed is still refused
        parse_ims_tsv(roll.replace("\tFLAT\t", "\tCLOSED\t"), "2026-09-21")
        raise AssertionError("accepted a ticker row with an unknown flag")
    except ValueError as e:
        assert "direction flag" in str(e)

    # ---- the numeric tail's TWO orders (2026-09-29, the mass roll) ---------
    # Same numbers, trading column moved from slot 5 to slot 7. The parser
    # must (a) leave a spec-order paste untouched — d3 above IS that check,
    # its rows all carry trading in slot 5 — (b) re-key a trading-LAST paste
    # so MTD/YTD/trading land in the right fields, and (c) REFUSE a paste
    # where neither slot is clearly the trading column. The cross-foot cannot
    # do any of this: it sums columns, and both orders sum.
    assert d3["positions"][0]["pnl_dtd_trading"] == 0 and d3["positions"][0]["pnl_mtd"] == n1[5]
    # (b): the 09-29 shape — slots 5/6 are MTD/YTD (equal, opened this
    # month), slot 7 the trading figure, zero on untraded rows.
    m1_ = [0.01, 0.009, 52.56, 0.0000175, -151.42, -151.42, 0, -0.00005, -0.00005, 0.01]
    m2_ = [-0.013, -0.012, -140.84, -0.0000469, 526.57, 526.57, 0, 0.000175, 0.000175, 0.013]
    m3_ = [0.0004, 0.006, -177.12, -0.0000590, -177.12, -177.12, -176.66, -0.000059, -0.000059, 0.0004]
    tot4 = [a + b + c for a, b, c in zip(m1_, m2_, m3_)]
    last = "\n".join([
        row("HNDL=V6 IS Equity", None, "LARGE CAP", "ALUMINIUM 1", 4, "LONG", m1_),
        row("NACL=V6 IS Equity", None, "MID CAP", "ALUMINIUM 1", -6, "SHORT", m2_),
        row("LTM IS 10/27/26 C4100 Equity", None, "LARGE CAP", "IT 3", 10, "LONG", m3_),
        "\t".join(["", "", "", "", "", "", ""] + [repr(x) for x in tot4]),
    ])
    d4 = parse_ims_tsv(last, "2026-09-29", "selftest")     # must ACCEPT + re-key
    assert any("trading-LAST" in w for w in d4["warnings"]), d4["warnings"]
    assert d4["tail_order"] == TAIL_TRADING_LAST
    p = {x["ticker"]: x for x in d4["positions"]}
    assert p["NACL=V6 IS Equity"]["pnl_mtd"] == 526.57 and p["NACL=V6 IS Equity"]["pnl_ytd"] == 526.57
    assert p["NACL=V6 IS Equity"]["pnl_dtd_trading"] == 0
    assert p["LTM IS 10/27/26 C4100 Equity"]["pnl_dtd_trading"] == -176.66
    assert p["LTM IS 10/27/26 C4100 Equity"]["pnl_ytd"] == -177.12
    assert d3["tail_order"] == TAIL_TRADING_FIRST        # (a), stated explicitly
    # (c) REJECTION: half the rows zero in slot 5, half in slot 7 — no majority
    a1 = [0.01, 0.009, 1.0, 0.0, 0, 5.0, 7.0, 0.0, 0.0, 0.01]      # trading-first shape
    a2 = [0.01, 0.009, 1.0, 0.0, 5.0, 7.0, 0, 0.0, 0.0, 0.01]      # trading-last shape
    amb = "\n".join([
        row("TCS=V6 IS Equity", None, "LARGE CAP", "IT 2", 6, "LONG", a1),
        row("WPRO=V6 IS Equity", None, "LARGE CAP", "IT 2", -7, "SHORT", a2),
    ])
    try:
        parse_ims_tsv(amb, "2026-09-29")
        raise AssertionError("accepted a paste whose column order is ambiguous")
    except ValueError as e:
        assert "ambiguous" in str(e), e

    # the custody-wrapper alias: same root, so no close/reopen and no
    # re-anchor. 'VAML IN Equity' -> 'VAML IN CFD PPB PTF' on 2026-09-09.
    assert normalise_ticker("VAML IN CFD PPB PTF") == ("VAML IN Equity", None)
    assert normalise_ticker("VAML IN Equity") == ("VAML IN Equity", None)
    assert normalise_ticker("TATA=U6 IS Equity") == ("TATA IS Equity", "U6")
    # an UNKNOWN wrapper must NOT be folded — the allow-list is the point
    assert normalise_ticker("VAML IN SWAP XYZ") == ("VAML IN SWAP XYZ", None)

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

    # THE YTD LEDGER (PM 2026-09-18) — ACCEPTANCE side first: on a BENIGN roll
    # (fresh contract starts at ~0, day figure == its whole accrual) and on a
    # reopen, the ledger must land exactly on the chain. If these two ever
    # disagree the ledger has drifted, not improved.
    assert it5["pnl_ytd"] == 7400.0, it5["pnl_ytd"]
    assert met["pnl_ytd"] == 1500.0, met["pnl_ytd"]
    assert it5["ytd_flags"] == [], it5["ytd_flags"]

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
    # ...and this is where since-inception and YEAR-to-date must DIVERGE: the
    # ledger resets on Jan 1 because that is what YTD means. 250, not 4250.
    assert x1["pnl_ytd"] == 250.0, x1["pnl_ytd"]
    assert x1["ytd_flags"] == []           # no chips since 2026-09-29 (PM)

    # GAP BRIDGING: a week with no snapshot. The cumulative YTD delta spans
    # every session in between; SUMMING THE DAY FIGURE would have booked only
    # the last one. Measured on the live book this was Monday 2026-09-14 and
    # a 350.75 hole, which is why the day column could not simply be summed.
    c5 = sqlite3.connect(":memory:"); c5.row_factory = sqlite3.Row
    c5.executescript(DDL)
    load_snapshot({"date": "2026-08-03", "source_file": "t", "positions":
                   [P("G 1", "TCS=U6 IS Equity", 1, 0.0, dtd=0.0)]}, conn=c5)
    load_snapshot({"date": "2026-08-10", "source_file": "t", "positions":
                   [P("G 1", "TCS=U6 IS Equity", 1, 5000.0, dtd=200.0)]},
                  conn=c5)
    g1 = pair_report(c5)["pairs"][0]
    assert g1["pnl_ytd"] == 5000.0, g1["pnl_ytd"]       # not 200, not 5200
    assert g1["ytd_flags"] == []

    # THE DOUBLE-COUNT GUARD — the REJECTION side, and the reason the ledger
    # bridges a roll with the DAY figure rather than the new contract's YTD.
    # CLAUDE.md flags it as ASSUMED AND UNVERIFIED that a fresh contract's YTD
    # starts near zero. Here it does NOT: V6 carries U6's 5,000. The old chain
    # freezes 5,000 and then adds 5,200 -> 10,200, counting the same P&L
    # twice. The ledger takes the day figure, lands on 5,200, and FLAGS it.
    c6 = sqlite3.connect(":memory:"); c6.row_factory = sqlite3.Row
    c6.executescript(DDL)
    load_snapshot({"date": "2026-08-03", "source_file": "t", "positions":
                   [P("H 1", "TCS=U6 IS Equity", 1, 5000.0, dtd=0.0)]}, conn=c6)
    load_snapshot({"date": "2026-08-04", "source_file": "t", "positions":
                   [P("H 1", "TCS=V6 IS Equity", 1, 5200.0, dtd=200.0)]},
                  conn=c6)
    h1 = pair_report(c6)["pairs"][0]
    assert h1["pnl_total"] == 10200.0, h1["pnl_total"]  # the chain's failure
    # SINCE 2026-09-29 the ledger bridges a roll with the new contract's own
    # YTD (it covers un-pasted sessions), so on this fixture — a new ticker
    # that DOES carry the old P&L, which has never been observed in 28 real
    # rolls — the ledger double counts exactly as the chain does. Kept as a
    # documented failure mode, not a guarantee.
    assert h1["pnl_ytd"] == 10200.0, h1["pnl_ytd"]
    assert h1["ytd_flags"] == []           # no chips since 2026-09-29 (PM)
    # ...and the case the change is FOR: a roll on a session with no paste.
    # V6 opened on the missing day and earned 300 there plus 100 on the
    # pasted day; the ledger must take all 400, not the day's 100.
    c7 = sqlite3.connect(":memory:"); c7.row_factory = sqlite3.Row
    c7.executescript(DDL)
    load_snapshot({"date": "2026-09-25", "source_file": "t", "positions":
                   [P("R 1", "TCS=U6 IS Equity", 1, 1000.0, dtd=10.0)]}, conn=c7)
    load_snapshot({"date": "2026-09-29", "source_file": "t", "positions":
                   [P("R 1", "TCS=V6 IS Equity", 1, 400.0, dtd=100.0)]}, conn=c7)
    r1 = pair_report(c7)["pairs"][0]
    assert r1["pnl_ytd"] == 1400.0, r1["pnl_ytd"]       # 1000 + all of V6's 400

    # ENTRY ANCHOR: fixed at the day entered, through resizes and retags;
    # resets only on a direction flip or a day out of the book (PM 2026-09-07)
    c4 = sqlite3.connect(":memory:")
    c4.row_factory = sqlite3.Row
    c4.executescript(DDL)
    Q = lambda pair, tk, qty, cost=None: {
        "pair": pair, "ticker": tk, "qty": qty, "cost": cost,
        "pnl_ytd": 0.0, "pnl_dtd": 0.0, "pnl_mtd": 0.0,
        "mv_pct": 0.002 * (1 if qty > 0 else -1)}
    load_snapshot({"date": "2026-09-01", "source_file": "t", "positions": [
        Q("A 1", "MPHL=U6 IS Equity", 1, 2455.0),     # will be RETAGGED
        Q("B 1", "TELX=U6 IS Equity", -2, 3711.0),    # will be RESIZED
        Q("C 1", "SAIL=U6 IS Equity", -1, 176.0),     # will be FLIPPED
        Q("D 1", "DIXON=U6 IS Equity", -1, 100.0),    # will sit a day OUT
    ]}, conn=c4)
    load_snapshot({"date": "2026-09-02", "source_file": "t", "positions": [
        Q("A 2", "MPHL=U6 IS Equity", 1),             # retag, cost dropped
        Q("B 1", "TELX=U6 IS Equity", -4),            # resize, cost dropped
        Q("C 1", "SAIL=U6 IS Equity", 1, 188.0),      # short -> long
    ]}, conn=c4)                                      # DIXON absent = closed
    load_snapshot({"date": "2026-09-03", "source_file": "t", "positions": [
        Q("A 2", "MPHL=U6 IS Equity", 1),
        Q("B 1", "TELX=U6 IS Equity", -4),
        Q("C 1", "SAIL=U6 IS Equity", 1),
        Q("D 1", "DIXON=U6 IS Equity", -1, 120.0),    # reopened
    ]}, conn=c4)
    leg = lambda rep, pair: next(
        p for p in rep["pairs"] if p["pair"] == pair)["legs"][0]
    r4 = pair_report(c4)
    mphl = leg(r4, "A 2")
    assert (mphl["first_seen"], mphl["entry_cost"]) == ("2026-09-01", 2455.0), \
        f"retag reset the anchor: {mphl['first_seen']} {mphl['entry_cost']}"
    telx = leg(r4, "B 1")
    assert (telx["first_seen"], telx["entry_cost"]) == ("2026-09-01", 3711.0), \
        f"resize reset the anchor: {telx['first_seen']} {telx['entry_cost']}"
    sail = leg(r4, "C 1")
    assert (sail["first_seen"], sail["entry_cost"]) == ("2026-09-02", 188.0), \
        f"flip did NOT reset the anchor: {sail['first_seen']}"
    dixn = leg(r4, "D 1")
    assert (dixn["first_seen"], dixn["entry_cost"]) == ("2026-09-03", 120.0), \
        f"a day out did NOT reset the anchor: {dixn['first_seen']}"

    # ANCHOR LOG: every event above was recorded at load time ----------------
    logged = lambda d: {(r["event"], r["root"]) for r in c4.execute(
        "SELECT event, root FROM book_anchor_log WHERE snap_date=?", (d,))}
    assert logged("2026-09-02") == {
        ("retagged", "MPHL IS Equity"), ("resized", "TELX IS Equity"),
        ("flipped", "SAIL IS Equity"), ("closed", "DIXON IS Equity")}, \
        logged("2026-09-02")
    assert logged("2026-09-03") == {("reopened", "DIXON IS Equity")}, \
        logged("2026-09-03")
    assert len(logged("2026-09-01")) == 4          # baseline: all 'entered'
    n_before = c4.execute("SELECT COUNT(*) FROM book_anchor_log").fetchone()[0]
    rebuild_anchor_log(c4)                          # idempotent — no duplicates
    assert c4.execute("SELECT COUNT(*) FROM book_anchor_log").fetchone()[0] \
        == n_before
    assert r4["anchor_log"] and r4["anchor_log"][0]["event"] == "reopened"

    # ENTRY-DAY OPEN: exposed when stored, keyed to the STREAK's anchor date;
    # and _pick_anchor_open accepts only a close-verified fetch --------------
    c4.execute("INSERT INTO book_entry_anchors VALUES "
               "('MPHL IS Equity','2026-09-01',2400.0,'MPHASIS.NS','t')")
    c4.execute("INSERT INTO book_entry_anchors VALUES "
               "('MPHL IS Equity','2026-09-03',9999.0,'MPHASIS.NS','t')")
    c4.commit()                     # the 09-03 row must NOT win — wrong date
    mphl = leg(pair_report(c4), "A 2")
    assert mphl["entry_open"] == 2400.0, mphl["entry_open"]
    telx = leg(pair_report(c4), "B 1")
    assert telx["entry_open"] is None               # nothing stored -> fallback
    assert _pick_anchor_open((2410.0, 2452.0), 2455.0) == 2410.0
    for day, sc in [(None, 2455.0),                 # no data for the date
                    ((2410.0, 2452.0), None),       # nothing stored to verify
                    ((2410.0, 2600.0), 2455.0),     # close mismatch: wrong symbol
                    ((1500.0, 2455.0), 2455.0)]:    # open implausible vs close
        try:
            _pick_anchor_open(day, sc)
            raise AssertionError(f"accepted bad anchor open: {day} vs {sc}")
        except ValueError:
            pass

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

    # display-rounded paste (the on-screen grid, 2026-09-30): header row,
    # '%' and ',' cells, '(n)' sector counts, a 'Total' label, GAV in a
    # comment. Accept it; refuse a dropped row (count) and a mangled cell.
    disp = "\n".join([
        "# Stats: GAV 3.01m | DTD 0.06%",
        "\t".join(["Ticker", "Group", "Tag3", "Tag Pair", "Qty", "LSF",
                   "MV %", "Beta MV %", "DTD PNL", "DTD PNL%", "MTD PNL",
                   "YTD PNL", "DTD Trading PNL", "MTD PNL%", "YTD PNL%",
                   "GMV %"]),
        "\t".join(["", "IT 9 (2)", "", "", "", "", "0.38%", "0.20%", "-1,003",
                   "-0.03%", "1,200", "1,500", "0", "0.04%", "0.05%", "2.62%"]),
        "\t".join(["TCS=V6 IS Equity", "", "LARGE CAP", "IT 9", "6", "LONG",
                   "1.50%", "1.02%", "-1,234", "-0.04%", "2,100", "2,400", "0",
                   "0.07%", "0.08%", "1.50%"]),
        "\t".join(["WPRO=V6 IS Equity", "", "LARGE CAP", "IT 9", "-7", "SHORT",
                   "-1.12%", "-0.82%", "231", "0.01%", "-900", "-900", "0",
                   "-0.03%", "-0.03%", "1.12%"]),
        "\t".join(["", "N.A. (1)", "", "", "", "", "0.00%", "0.00%", "0", "0%",
                   "5,677", "9,586", "0", "0.19%", "0.32%", "0.00%"]),
        "\t".join(["FLAT_POS", "", "N.A.", "N.A.", "0", "FLAT_IN_YEAR",
                   "0.00%", "0.00%", "0", "0%", "5,677", "9,586", "0", "0.19%",
                   "0.32%", "0.00%"]),
        "\t".join(["", "Total", "", "", "", "", "0.38%", "0.20%", "-1,003",
                   "-0.03%", "6,877", "11,086", "0", "0.23%", "0.37%", "2.62%"]),
    ])
    dd = parse_ims_tsv(disp, "2026-09-30", "selftest")
    assert dd["precision"] == "display" and len(dd["positions"]) == 2
    assert dd["nav"] == 3010000.0, dd["nav"]
    assert dd["tail_order"] == TAIL_TRADING_LAST
    tp = dd["positions"][0]
    assert tp["qty"] == 6 and abs(tp["mv_pct"] - 0.015) < 1e-12
    assert tp["pnl_mtd"] == 2100 and tp["pnl_ytd"] == 2400
    assert dd["total"]["pnl_ytd"] == 11086 and dd["na"]["pnl_ytd"] == 9586
    assert parse_ims_tsv(tsv, "2026-08-03")["precision"] == "full"
    dl = disp.splitlines()
    for bad, why in (("\n".join(dl[:3] + dl[4:]), "row"),           # dropped
                     (disp.replace("2,400", "2,900", 1), "cross-foot")):
        try:
            parse_ims_tsv(bad, "2026-09-30")
            raise AssertionError(f"display paste accepted: {why}")
        except ValueError as e:
            assert why in str(e), e

    add_review("IT 5", "hold", "thesis intact; sized right", True, conn=conn)
    assert reviews(conn=conn, pair="IT 5")[0]["verdict"] == "hold"
    print("selftest OK — ticker/option parse, TSV parse (both cost shapes), "
          "full export shape (FLAT_IN_YEAR + N.A. members + custody alias, "
          "accept & reject), "
          "NAV derivation, cross-foot accept+reject, display-rounded paste "
          "(accept, count + sector cross-foot reject), roll chain, closed pair, "
          "cadence (no-split + reopen), year boundary, YTD ledger "
          "(benign roll == chain, year reset, gap bridge, double-count "
          "guard), entry anchor "
          "(retag/resize keep, flip/day-out reset), anchor log "
          "(events + rebuild idempotence), entry-day open "
          "(streak-keyed + fetch guards accept/reject), refusal paths, reviews")


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
    ap.add_argument("--rebuild-log", action="store_true",
                    help="replay all stored snapshots into book_anchor_log")
    ap.add_argument("--fetch-anchors", action="store_true",
                    help="fetch entry-day opens for live legs (Yahoo)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    def _fetch_anchors_step():
        try:
            fa = fetch_entry_anchors()
            for root, adate, o in fa["fetched"]:
                print(f"  anchor open {display_name(root)}: {o:g} ({adate})")
            for root, why in fa["failed"]:
                print(f"  ⚠ anchor open {display_name(root)}: {why} "
                      f"— falls back to IMS cost / entry-day close")
            if fa["have"] and not fa["fetched"] and not fa["failed"]:
                print(f"  anchor opens: all {len(fa['have'])} already stored")
        except Exception as e:                      # noqa: BLE001
            print(f"  ⚠ entry-anchor fetch failed entirely: {e}")

    if a.selftest:
        _selftest()
    elif a.rebuild_log:
        print(f"rebuilt book_anchor_log: {rebuild_anchor_log()} events")
    elif a.fetch_anchors:
        _fetch_anchors_step()
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
        _fetch_anchors_step()       # new entries need their entry-day open
        _print_report(pair_report())
    elif a.load:
        res = load_snapshot(pathlib.Path(a.load), replace=a.replace)
        print(f"loaded {res['date']}: {res['n_positions']} positions "
              f"(basis {res['pnl_basis']})")
        for w in res["warnings"]:
            print(f"  ⚠ {w}")
        _fetch_anchors_step()
        _print_report(pair_report())
    elif a.report:
        _print_report(pair_report(
            as_of=None if a.report == "latest" else a.report))
    else:
        ap.print_help()
