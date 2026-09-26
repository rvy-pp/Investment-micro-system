"""One writer for the `prices` table, with source precedence.

WHY THIS EXISTS. Four adapters wrote to `prices` with INSERT OR REPLACE, so the
last one to run owned every overlapping date and nothing recorded who that was.
The collision is real, not theoretical: yahoo_prices.py overwrote the Daily
Metals Pack's usdinr on 2026-08-15, 95.4300 -> 95.6470, silently. And
`lme_aluminium` — a series whose name says LME and whose pack source is LME cash
— has been carrying Yahoo's ALI=F, which is CME and ran +142 USD/t (+4.5%)
against actual LME cash on 2026-08-20, because it embeds a Midwest premium.

The table had no `source` column, so "the pack is authoritative" could not even
be expressed. It can now.

PRECEDENCE, highest wins. A write is REFUSED when a higher-ranked source already
holds that (entity_id, date):

    metals_pack  licensed, hand-dropped, the desk's own reference
    westmetall   real LME cash-settlement, free, day-delayed
    wind         Wind terminal
    fred         monthly fallback
    yahoo        exchange proxies and equities

Equal rank overwrites — yahoo re-running intraday must be able to improve its own
close. A legacy row with source NULL ranks 0, so anything may replace it; that is
deliberate, because those rows are exactly the ones of unknown provenance.

NOT A DECAY MECHANISM AND NOT A MERGE. It never blends two sources into one
number. Each (entity_id, date) holds exactly one source's value, and which one is
now recorded rather than being a function of what ran last.
"""

from __future__ import annotations

import sqlite3

# Rank, highest wins. Add a source here before using it; an unregistered source
# raises rather than silently ranking 0, because a typo'd source name would
# otherwise quietly become the lowest-priority writer and lose every race.
PRECEDENCE = {
    # Regulatory filings parsed from the issuer's own documents (Coal India
    # monthly production/offtake and SWMA e-auction filings, NMDC price
    # circulars and monthly production/sales) — plus the desk ledger rows in
    # specs/extracted/mining_prints.json that carry the SAME filings' numbers
    # via broker digests while the issuer's website lags. One rank for both on
    # purpose: they are one measure from one primary source, and when the
    # website catches up the direct parse overwrites the digest-cited row with
    # the same value rather than being refused below it.
    "filing": 40,
    "metals_pack": 40,
    # Same broker, same mail, same licensed provenance as metals_pack, so the
    # same rank. They cannot collide in practice — the cement pack supplies only
    # `cement_price_*`, which nothing else carries — but ranking it below would
    # be a claim about relative quality that is not true, and ranking is what
    # decides a future overlap.
    "cement_pack": 40,
    "westmetall":  30,
    "wind":        20,
    "fred":        10,
    "yahoo":        5,
}


def ensure_source_column(conn: sqlite3.Connection) -> bool:
    """Add prices.source if missing. Idempotent; safe on a live store.

    Done here rather than in a migration script so every adapter gets the column
    whether or not init_db.py has been re-run. Existing rows keep source NULL,
    which ranks 0 — see the module docstring.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prices)")}
    if "source" in cols:
        return False
    conn.execute("ALTER TABLE prices ADD COLUMN source TEXT")
    conn.commit()
    return True


def upsert(conn: sqlite3.Connection, rows, source: str,
           currency: str | None = None) -> dict:
    """Write price rows, refusing to lower a cell's source.

    rows: iterable of (entity_id, date, close) — or (entity_id, date, close,
    bars), where `bars` is a mapping carrying any of open/high/low/volume for
    that SAME bar. The 4th element is a mapping rather than three more
    positional floats on purpose: (o,h,l) and (h,l,o) are both plausible
    orders and a transposed high/low would draw an inverted candle that no
    guard here could see. A legacy 3-tuple caller is unaffected.

    OHLC IS WRITTEN ONLY WHERE THE SAME SOURCE ALSO WINS THE CLOSE. A cell a
    higher-ranked source already owns is refused whole, bars included — this
    writer never merges two sources into one row (see the module docstring),
    and a metals_pack close wearing a Yahoo high would be exactly that: a row
    whose `source` column names one feed while half its numbers come from
    another. The columns had been NULL on all 201,267 rows since the schema
    was written, because yahoo_prices.fetch() kept only the close.

    Returns counts so a caller can REPORT what it declined to overwrite —
    a refused write must be visible, or this becomes another silent rule.
    """
    if source not in PRECEDENCE:
        raise ValueError(
            f"unknown price source {source!r}; register it in "
            f"prices_io.PRECEDENCE (known: {sorted(PRECEDENCE)})")
    ensure_source_column(conn)
    mine = PRECEDENCE[source]

    existing = {}
    for eid, d, src in conn.execute("SELECT entity_id, date, source FROM prices"):
        existing[(eid, d)] = src

    wrote, refused, unchanged, bad_bars = 0, [], 0, []
    for row in rows:
        eid, d, close = row[0], row[1], row[2]
        bars = row[3] if len(row) > 3 else None
        if close is None:
            continue
        cur = existing.get((eid, d), "__absent__")
        if cur != "__absent__":
            rank = PRECEDENCE.get(cur, 0) if cur else 0
            if rank > mine:
                refused.append((eid, d, cur))
                continue
        o = h = lo = vol = None
        if bars:
            o, h, lo = bars.get("open"), bars.get("high"), bars.get("low")
            vol = bars.get("volume")
            o = float(o) if o is not None else None
            h = float(h) if h is not None else None
            lo = float(lo) if lo is not None else None
            vol = float(vol) if vol is not None else None
            # A bar must CONTAIN its own close, or it is not this bar. Yahoo
            # has been seen returning a null high beside a live close on a
            # halted session; a high below the close would draw a candle with
            # the wick on the wrong side and nothing downstream would raise.
            # The bar is dropped, the close is kept, and the drop is REPORTED.
            vals = [v for v in (o, h, lo) if v is not None]
            if (len(vals) != 3 or h < lo or h < close or lo > close
                    or min(vals) <= 0):
                bad_bars.append((eid, d, {"open": o, "high": h, "low": lo,
                                          "close": close}))
                o = h = lo = None
        conn.execute(
            "INSERT INTO prices (entity_id,date,open,high,low,close,volume,"
            "currency,source) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_id,date) DO UPDATE SET "
            "close=excluded.close, "
            # COALESCE, so a close-only writer (the commodity feeds have no
            # bars at all) never blanks an OHLC row it is allowed to touch.
            # It can only retain THIS cell's own previously-fetched bar.
            "open=COALESCE(excluded.open,open), "
            "high=COALESCE(excluded.high,high), "
            "low=COALESCE(excluded.low,low), "
            "volume=COALESCE(excluded.volume,volume), "
            "currency=COALESCE(excluded.currency,currency), "
            "source=excluded.source",
            (eid, d, o, h, lo, float(close), vol, currency, source))
        wrote += 1
    conn.commit()
    return {"wrote": wrote, "refused": len(refused),
            "refused_detail": refused[:20], "unchanged": unchanged,
            "bad_bars": len(bad_bars), "bad_bars_detail": bad_bars[:20],
            "source": source}


def report(res: dict) -> str:
    s = f"{res['wrote']:,} rows written as source={res['source']}"
    if res["refused"]:
        by = {}
        for _eid, _d, src in res["refused_detail"]:
            by[src] = by.get(src, 0) + 1
        s += (f"; {res['refused']:,} REFUSED — a higher-ranked source already "
              f"holds those cells ("
              + ", ".join(f"{k or 'unknown'}×{v}" for k, v in sorted(by.items()))
              + ")")
    if res.get("bad_bars"):
        s += (f"; {res['bad_bars']:,} bar(s) DROPPED as incoherent (high/low "
              f"did not contain the close) — the closes were kept")
    return s


# --------------------------------------------------------------------------
# selftest — BOTH directions, per the GLOB lesson
#
# `GLOB '____-__-__'` passed every test it had and rejected every valid date,
# because the tests only checked that bad rows are REFUSED. So each guard below
# is tested twice: that it refuses the broken bar AND that it accepts the good
# one. A bar guard that rejected everything would look identical, in the store,
# to the NULL columns this code exists to fill.
# --------------------------------------------------------------------------

def _selftest() -> int:
    import tempfile, os
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r}, want {want!r}")

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE entities (id TEXT PRIMARY KEY)")
    conn.execute("""CREATE TABLE prices (
        entity_id TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL,
        low REAL, close REAL NOT NULL, volume REAL, currency TEXT, source TEXT,
        PRIMARY KEY (entity_id, date))""")

    def row(eid, d):
        return conn.execute("SELECT open,high,low,close,volume,source FROM "
                            "prices WHERE entity_id=? AND date=?",
                            (eid, d)).fetchone()

    # 1. ACCEPTANCE — a good bar lands whole. Without this the rest is vacuous.
    good = {"open": 100.0, "high": 105.0, "low": 99.0, "volume": 1234.0}
    upsert(conn, [("a", "2026-09-10", 102.0, good)], "yahoo")
    check("good bar stored", row("a", "2026-09-10")[:5],
          (100.0, 105.0, 99.0, 102.0, 1234.0))

    # 2. LEGACY 3-tuple still writes, and leaves OHLC NULL rather than raising.
    upsert(conn, [("b", "2026-09-10", 50.0)], "yahoo")
    check("legacy 3-tuple", row("b", "2026-09-10")[:4], (None, None, None, 50.0))

    # 3. A close-only rewrite must NOT blank an existing bar (the COALESCE).
    #    The commodity feeds carry no bars at all and re-run daily over the
    #    same cells; without this they would erase every candle nightly.
    upsert(conn, [("a", "2026-09-10", 103.0)], "yahoo")
    check("close-only keeps bar", row("a", "2026-09-10")[:4],
          (100.0, 105.0, 99.0, 103.0))

    # 4. REJECTION — high below the close is not this bar. Close kept, bar dropped.
    res = upsert(conn, [("c", "2026-09-10", 200.0,
                         {"open": 190.0, "high": 195.0, "low": 188.0})], "yahoo")
    check("incoherent bar dropped", row("c", "2026-09-10")[:4],
          (None, None, None, 200.0))
    check("incoherent bar counted", res["bad_bars"], 1)

    # 5. REJECTION — transposed high/low. The reason bars arrive as a MAPPING
    #    and not three positional floats; this is the shape that ordering slip
    #    would produce, and it would draw an inverted candle.
    res = upsert(conn, [("d", "2026-09-10", 100.0,
                         {"open": 100.0, "high": 98.0, "low": 105.0})], "yahoo")
    check("transposed h/l dropped", row("d", "2026-09-10")[:4],
          (None, None, None, 100.0))

    # 6. REJECTION — a partial bar (null high) is dropped whole, never patched
    #    from a neighbouring field.
    upsert(conn, [("e", "2026-09-10", 10.0,
                   {"open": 10.0, "high": None, "low": 9.0})], "yahoo")
    check("partial bar dropped", row("e", "2026-09-10")[:4],
          (None, None, None, 10.0))

    # 7. PRECEDENCE — a higher-ranked source's cell refuses the bar too. A
    #    metals_pack close wearing a Yahoo high is exactly the two-sources-in-
    #    one-row merge this module forbids.
    upsert(conn, [("f", "2026-09-10", 3000.0)], "metals_pack")
    res = upsert(conn, [("f", "2026-09-10", 3100.0, good)], "yahoo")
    check("refused cell unchanged", row("f", "2026-09-10")[:4],
          (None, None, None, 3000.0))
    check("refusal reported", res["refused"], 1)
    check("refused source held", row("f", "2026-09-10")[5], "metals_pack")

    # 8. ACCEPTANCE at equal rank — yahoo must still improve its own bar
    #    intraday, or the live session's candle freezes at its first print.
    upsert(conn, [("a", "2026-09-11", 100.0,
                   {"open": 100.0, "high": 101.0, "low": 99.0})], "yahoo")
    upsert(conn, [("a", "2026-09-11", 104.0,
                   {"open": 100.0, "high": 106.0, "low": 99.0})], "yahoo")
    check("equal rank updates bar", row("a", "2026-09-11")[:4],
          (100.0, 106.0, 99.0, 104.0))

    conn.close()
    os.unlink(path)

    for f in fails:
        print("  FAIL " + f)
    print("prices_io selftest: all 8 check groups clean" if not fails
          else f"prices_io selftest: {len(fails)} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_selftest())
