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
    """Write (entity_id, date, close) rows, refusing to lower a cell's source.

    rows: iterable of (entity_id, date, close).
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

    wrote, refused, unchanged = 0, [], 0
    for eid, d, close in rows:
        if close is None:
            continue
        cur = existing.get((eid, d), "__absent__")
        if cur != "__absent__":
            rank = PRECEDENCE.get(cur, 0) if cur else 0
            if rank > mine:
                refused.append((eid, d, cur))
                continue
        conn.execute(
            "INSERT INTO prices (entity_id,date,close,currency,source) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(entity_id,date) DO UPDATE SET "
            "close=excluded.close, currency=COALESCE(excluded.currency,currency), "
            "source=excluded.source",
            (eid, d, float(close), currency, source))
        wrote += 1
    conn.commit()
    return {"wrote": wrote, "refused": len(refused),
            "refused_detail": refused[:20], "unchanged": unchanged,
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
    return s
