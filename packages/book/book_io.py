"""The PM's actual book — daily IMS position snapshots, stored pair-wise.

    python packages/book/book_io.py --load data/book/staging/2026-09-05.json
    python packages/book/book_io.py --report            # latest snapshot
    python packages/book/book_io.py --selftest

Driven by the `daily_review` skill: the agent parses whatever the PM uploads
(screenshot, xlsx, paste) into a canonical staging JSON; THIS module is the
deterministic half — it validates, persists and does every piece of
arithmetic. The model transcribes cited facts; code computes. Same inversion
as the pillars.

WHY THIS EXISTS AT ALL — two things the company IMS cannot show:

1. PAIRS. The IMS lists positions one by one; the PM tags each with a pair
   name ("IT 5") in a description field, and more than two legs can share a
   tag. The unit of thought is the pair, so the display is one line per pair.
2. ROLLOVERS. Bloomberg futures tickers reset every expiry (TATASTEEL=U6 ->
   TATASTEEL=V6), and with them the IMS's P&L column — so pair inception and
   since-inception P&L are invisible there. Here the contract token is
   stripped to a stable ROOT for identity, and P&L is CHAINED across rolls:
   when a root's contract changes between snapshots, the old contract's last
   seen P&L freezes into `realized` and the new contract starts a segment.
   total = sum(frozen segments) + live P&L.

THE CHAIN IS ONLY AS GOOD AS THE SNAPSHOT CADENCE. If a roll happens between
two captures, the dying contract's final P&L was never seen — the chain keeps
the last value it saw and the report flags the gap (`gap_risk`) instead of
pretending precision. Withhold-rather-than-guess applies to flags, not to the
total: an approximate total with a named gap beats no total.

P&L BASIS. `pnl` in the staging JSON is the P&L figure the IMS itself prints
for the position — never recomputed here (no lot sizes, no multipliers, no
silent-arithmetic surface). Basis is declared per snapshot:
  contract_itd  P&L since the CONTRACT was opened (resets at roll) — chained.
  daily         that day's P&L — summed; every missed day silently loses
                money from the total, so the report counts calendar gaps.
The first real snapshot calibrates which one the IMS prints; until the skill
has verified it, the default is contract_itd.

Never writes to `prices`, `pillar_scores` or anything a pillar reads.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
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
    note        TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS book_positions (
    snap_date   TEXT NOT NULL REFERENCES book_snapshots(snap_date)
                ON DELETE CASCADE,
    pair_tag    TEXT NOT NULL,          -- as written on the IMS ('IT 5'); 'UNTAGGED' if blank
    ticker_raw  TEXT NOT NULL,          -- verbatim from the snapshot, contract code and all
    root        TEXT NOT NULL,          -- ticker with the =U6-style token stripped: the stable identity
    contract    TEXT,                   -- 'U6' etc; NULL for cash equity
    side        TEXT NOT NULL CHECK (side IN ('L','S')),
    qty         REAL NOT NULL CHECK (qty > 0),
    avg_price   REAL,
    last_price  REAL,
    mv          REAL,                   -- market value as the IMS shows it
    pnl         REAL,                   -- the IMS's own P&L figure — never recomputed here
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

# Bloomberg-style contract token: '=U6' (month code + year digit[s]) or a
# bare '=1'-style generic. Month codes are the futures alphabet FGHJKMNQUVXZ.
# The root keeps everything else verbatim (exchange/asset suffix included) so
# 'TATASTEEL=U6 IS Equity' and 'TATASTEEL=V6 IS Equity' collapse to the same
# 'TATASTEEL IS Equity' while 'TATASTEEL IN Equity' stays distinct from it.
RE_CONTRACT = re.compile(r"=([FGHJKMNQUVXZ]\d{1,2}|\d{1,2})(?=\s|$)")


def normalise_ticker(raw: str) -> tuple[str, str | None]:
    """'TATASTEEL=U6 IS Equity' -> ('TATASTEEL IS Equity', 'U6')."""
    raw = " ".join(str(raw).split())
    m = RE_CONTRACT.search(raw)
    if not m:
        return raw, None
    root = " ".join((raw[: m.start()] + raw[m.end():]).split())
    return root, m.group(1)


def display_name(root: str) -> str:
    """What the pair line prints: the root minus Bloomberg boilerplate."""
    return re.sub(r"\s+(IS|IN|IB)?\s*(Equity|Index|Comdty|Curncy)$",
                  "", root, flags=re.I).strip() or root


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(DDL)
    return conn


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------

def load_snapshot(path: pathlib.Path, replace: bool = False,
                  conn: sqlite3.Connection | None = None) -> dict:
    """Validate and persist one staged snapshot JSON. Refuses rather than
    repairs: a malformed staging file goes back to the parse step."""
    own = conn is None
    conn = conn or connect()
    doc = json.loads(path.read_text(encoding="utf-8"))

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

    warnings: list[str] = []
    parsed = []
    seen: set[tuple[str, str]] = set()
    for i, p in enumerate(rows):
        raw = str(p.get("ticker") or "").strip()
        if not raw:
            raise ValueError(f"position {i}: no ticker")
        qty = p.get("qty")
        side = p.get("side")
        if qty is None:
            raise ValueError(f"{raw}: no qty")
        qty = float(qty)
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
        parsed.append((date, pair, raw, root, contract, side, qty,
                       p.get("avg_price"), p.get("last_price"),
                       p.get("mv"), p.get("pnl"), p.get("note")))

    with conn:
        if existing:
            # explicit, not via ON DELETE CASCADE — cascade silently does
            # nothing on a connection whose foreign_keys pragma is off, and
            # the UNIQUE violation that follows blames the wrong line
            conn.execute("DELETE FROM book_positions WHERE snap_date=?", (date,))
            conn.execute("DELETE FROM book_snapshots WHERE snap_date=?", (date,))
        conn.execute(
            "INSERT INTO book_snapshots (snap_date, source_file, loaded_at, "
            "n_positions, pnl_basis, note) VALUES (?,?,?,?,?,?)",
            (date, doc.get("source_file") or path.name,
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             len(parsed), basis, doc.get("note")))
        conn.executemany(
            "INSERT INTO book_positions (snap_date, pair_tag, ticker_raw, root, "
            "contract, side, qty, avg_price, last_price, mv, pnl, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", parsed)
    out = {"date": date, "n_positions": len(parsed), "pnl_basis": basis,
           "warnings": warnings}
    if own:
        conn.close()
    return out


# --------------------------------------------------------------------------
# the chain — rollover-proof P&L per (pair, root)
# --------------------------------------------------------------------------

def _chain_leg(rows: list[sqlite3.Row], basis: str,
               all_dates: list[str]) -> dict:
    """rows: one (pair, root)'s appearances, date-ascending. all_dates: every
    snapshot date in the window, ascending.

    contract_itd: a segment closes when the contract token changes (a roll) or
    when the root was ABSENT from an intervening snapshot (closed and
    reopened — the reopened position's IMS P&L restarts). A snapshot is the
    FULL book, so absence from one is a real close. Absence is tested against
    stored snapshot dates, NEVER against calendar days: this tool runs on
    demand, so a quiet fortnight between runs must not split a
    continuously-held contract and double-count its P&L.

    gap_risk marks a roll that happened across a snapshot gap of more than
    ROLL_GAP_DAYS calendar days — the dying contract's final P&L was last seen
    early, so the frozen figure may miss its closing days.
    """
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
            if rolled or absent:
                realized += prev["pnl"] or 0.0
                if rolled:
                    rolls += 1
                    if (_date(d1) - _date(d0)).days > ROLL_GAP_DAYS:
                        gap_risk = True
        prev = r
    live = rows[-1]["pnl"] or 0.0
    return {"total": realized + live, "realized": realized, "live": live,
            "rolls": rolls, "gap_risk": gap_risk, "last": rows[-1]}


# A roll observed across a snapshot gap wider than this many calendar days is
# flagged: the frozen P&L may miss the old contract's last days. ~a week
# tolerates a normal weekend + a missed session around expiry.
ROLL_GAP_DAYS = 7


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


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
    basis = conn.execute(
        "SELECT pnl_basis FROM book_snapshots WHERE snap_date=?",
        (as_of,)).fetchone()["pnl_basis"]
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
            closed.append({"pair": pair, "inception": inception,
                           "last_seen": last_seen,
                           "days": (_date(last_seen) - _date(inception)).days,
                           "pnl_total": round(total, 2)})
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
                "side": r["side"], "qty": r["qty"],
                "avg_price": r["avg_price"], "last_price": r["last_price"],
                "mv": r["mv"],
                "pnl_live": r["pnl"], "pnl_total": round(ch["total"], 2),
                "rolls": ch["rolls"], "note": r["note"],
            })
        gross = sum(abs(x["mv"]) for x in legs if x["mv"] is not None) or None
        day = round(total - pair_total(pair, prev), 2) if prev else None
        pairs.append({
            "pair": pair, "inception": inception,
            "days": (_date(as_of) - _date(inception)).days,
            "n_legs": len(legs),
            "long": [x["name"] for x in legs if x["side"] == "L"],
            "short": [x["name"] for x in legs if x["side"] == "S"],
            "pnl_total": round(total, 2), "pnl_day": day,
            "gross_mv": gross, "rolls": rolls, "gap_risk": gap_risk,
            "legs": legs,
        })

    pairs.sort(key=lambda p: -(p["pnl_total"] if p["pnl_total"] is not None else 0))
    out = {"as_of": as_of, "prev": prev, "pnl_basis": basis,
           "snapshots": len(hist), "pairs": pairs, "closed": closed,
           "book_pnl_total": round(sum(p["pnl_total"] for p in pairs), 2)}
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
    rows = [dict(r) for r in conn.execute(q + " ORDER BY review_date DESC, id DESC", args)]
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
    print(f"book as of {rep['as_of']}  ({rep['snapshots']} snapshots, "
          f"basis {rep['pnl_basis']})   total P&L {rep['book_pnl_total']:,.0f}")
    for p in rep["pairs"]:
        day = "" if p["pnl_day"] is None else f"  day {p['pnl_day']:+,.0f}"
        flags = ("  ⚠gap" if p["gap_risk"] else "") + \
                (f"  rolls×{p['rolls']}" if p["rolls"] else "")
        print(f"  {p['pair']:<12} L {'/'.join(p['long']) or '—':<28} "
              f"S {'/'.join(p['short']) or '—':<28} "
              f"since {p['inception']} ({p['days']}d)  "
              f"P&L {p['pnl_total']:+,.0f}{day}{flags}")
    for c in rep["closed"]:
        print(f"  {c['pair']:<12} CLOSED {c['inception']} → {c['last_seen']} "
              f"({c['days']}d)  final P&L {c['pnl_total']:+,.0f}")


def _selftest() -> None:
    # parse cases -----------------------------------------------------------
    cases = {
        "TATASTEEL=U6 IS Equity": ("TATASTEEL IS Equity", "U6"),
        "TATASTEEL=V6 IS Equity": ("TATASTEEL IS Equity", "V6"),
        "NIFTY=Z6 Index":         ("NIFTY Index", "Z6"),
        "INFY IS Equity":         ("INFY IS Equity", None),
        "JSTL=1 IS Equity":       ("JSTL IS Equity", "1"),
        "HNDL=U26 IS Equity":     ("HNDL IS Equity", "U26"),
    }
    for raw, want in cases.items():
        got = normalise_ticker(raw)
        assert got == want, f"{raw}: {got} != {want}"
    assert display_name("TATASTEEL IS Equity") == "TATASTEEL"
    assert display_name("NIFTY Index") == "NIFTY"

    # chained P&L over an in-memory store ------------------------------------
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)

    def snap(date, positions):
        f = pathlib.Path(STAGING) / f"_selftest_{date}.json"
        doc = {"date": date, "source_file": "selftest", "positions": positions}
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(doc), encoding="utf-8")
        try:
            load_snapshot(f, conn=conn)
        finally:
            f.unlink()

    P = lambda pair, tk, qty, pnl: {"pair": pair, "ticker": tk,
                                    "qty": qty, "pnl": pnl, "mv": abs(qty) * 100}
    # day 1: IT 5 is long TCS / short INFY on U6 contracts, one cash pair
    snap("2026-08-03", [P("IT 5", "TCS=U6 IS Equity", 100, 0),
                        P("IT 5", "INFY=U6 IS Equity", -200, 0),
                        P("MET 1", "HNDL IS Equity", 500, 1000)])
    # day 2: P&L accrues on the same contracts
    snap("2026-08-04", [P("IT 5", "TCS=U6 IS Equity", 100, 5000),
                        P("IT 5", "INFY=U6 IS Equity", -200, 2000),
                        P("MET 1", "HNDL IS Equity", 500, 1500)])
    # day 3: THE ROLL — U6 -> V6, IMS P&L resets near zero
    snap("2026-08-05", [P("IT 5", "TCS=V6 IS Equity", 100, -300),
                        P("IT 5", "INFY=V6 IS Equity", -200, 100),
                        P("MET 1", "HNDL IS Equity", 500, 1200)])
    # day 4: MET 1 gone (closed); IT 5 continues
    snap("2026-08-06", [P("IT 5", "TCS=V6 IS Equity", 100, 700),
                        P("IT 5", "INFY=V6 IS Equity", -200, -400)])

    rep = pair_report(conn, as_of="2026-08-06")
    it5 = next(p for p in rep["pairs"] if p["pair"] == "IT 5")
    # chained: TCS 5000 (frozen U6) + 700 (live V6); INFY 2000 + (-400)
    assert it5["pnl_total"] == 7300.0, it5["pnl_total"]
    assert it5["rolls"] == 2 and it5["inception"] == "2026-08-03"
    assert it5["days"] == 3 and not it5["gap_risk"]
    # day P&L vs the 09-03 total (5000-300 + 2000+100 = 6800)
    assert it5["pnl_day"] == 500.0, it5["pnl_day"]
    met = next(c for c in rep["closed"] if c["pair"] == "MET 1")
    assert met["pnl_total"] == 1200.0 and met["last_seen"] == "2026-08-05"
    # replay as of the roll day still works
    rep3 = pair_report(conn, as_of="2026-08-05")
    it5_3 = next(p for p in rep3["pairs"] if p["pair"] == "IT 5")
    assert it5_3["pnl_total"] == 6800.0

    # THE ON-DEMAND CADENCE CASES — the reason splits key on stored snapshot
    # dates, never calendar days. 16 quiet days, then one snapshot:
    #   IT 5 holds the SAME V6 contracts -> must NOT split (no double count);
    #   MET 1 reappears after being absent from the 09-04 snapshot -> genuine
    #   close-and-reopen, its frozen 1200 must survive the restart.
    snap("2026-08-22", [P("IT 5", "TCS=V6 IS Equity", 100, 900),
                        P("IT 5", "INFY=V6 IS Equity", -200, -500),
                        P("MET 1", "HNDL IS Equity", 500, 300)])
    rep = pair_report(conn)
    it5 = next(p for p in rep["pairs"] if p["pair"] == "IT 5")
    assert it5["pnl_total"] == 7400.0, it5["pnl_total"]   # 5000+900, 2000-500
    assert it5["rolls"] == 2                              # still just the U6->V6
    met = next(p for p in rep["pairs"] if p["pair"] == "MET 1")
    assert met["pnl_total"] == 1500.0, met["pnl_total"]   # 1200 frozen + 300

    # refusal paths, on their own store (the GLOB lesson: guards need
    # accepting AND rejecting cases)
    c2 = sqlite3.connect(":memory:")
    c2.row_factory = sqlite3.Row
    c2.executescript(DDL)
    f = STAGING / "_selftest_dup.json"
    f.write_text(json.dumps({"date": "2026-08-06", "source_file": "x",
                             "positions": [P("A", "X IS Equity", 1, 0)]}),
                 encoding="utf-8")
    try:
        load_snapshot(f, conn=c2)
        try:
            load_snapshot(f, conn=c2)
            raise AssertionError("duplicate date loaded without --replace")
        except ValueError:
            pass
        load_snapshot(f, replace=True, conn=c2)   # replace path accepts
    finally:
        f.unlink()
    assert any(p["pair"] == "A" for p in pair_report(c2)["pairs"])

    # reviews round-trip
    add_review("IT 5", "hold", "thesis intact; sized right", True, conn=conn)
    assert reviews(conn=conn, pair="IT 5")[0]["verdict"] == "hold"
    print("selftest OK — 6 parse cases, roll chain, closed pair, day P&L, "
          "replay, cadence (no-split + reopen), refusal paths, reviews")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--load", metavar="JSON", help="staged snapshot to persist")
    ap.add_argument("--replace", action="store_true",
                    help="allow overwriting an already-loaded date")
    ap.add_argument("--report", nargs="?", const="latest", metavar="DATE",
                    help="print the pair report (default: latest snapshot)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
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
