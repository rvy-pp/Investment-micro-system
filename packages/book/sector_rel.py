"""Each book leg's rolling 2-month (60-day) move with its SECTOR taken out.

PM, 2026-10-21, on the Book tab's Positions table: *"MTD is useless. Put it in
commas like: TCS: +xx.x%, INFY: -yy.y%"* and *"Calculate sector part
intrinsically"*. So the `mtd` column carries, per pair, each leg's stock-
specific move over the last 60 calendar days (90 until the PM cut it to two
months, same day) — the residual after the sector
explains what it can. It came out of the TCS case (2026-10-20): its +14.1%
post-result rally was +12.6pp sector and +1.5pp TCS, which is the question this
column answers for every leg every day.

THE SPLIT (price_story.benchmark_test's, applied to a book leg)
---------------------------------------------------------------
  basket  = equal-weighted daily returns of the leg's sector F&O names,
            EXCLUDING the leg itself. Regressing a stock on a basket that holds
            it reads a slice of itself back as "sector".
  sector  = the grading universe: specs/entities -> sector, IT from the
            entities table, F&O = traded in fo_oi within 30 days of as_of
            (grading.load_config / fno_names — one definition of "the sector's
            names" for the Book tab, not two).
  beta    = OLS of the leg's daily return on the basket's over the 250
            sessions BEFORE the window opens. Fitting on the window itself
            would let the move being explained set its own beta. Under 60
            sessions (VAML lists 2026-06-15) beta defaults to 1.0, FLAGGED.
  rel_pct = stock window return - beta x basket window return, percentage POINTS,
            both compounded over the same sessions, so stock = sector + rel
            exactly — the invariant a reader can audit from the hover.

Windows are CALENDAR days (CLAUDE.md: the store mixes cadences). Prices are
clipped to as_of, so the Positions date picker replays it. Zero-volume
O=H=L=C rows are Yahoo phantoms for shut days and are dropped, as in
grading.Store. A basket with no peers (a one-name sector) withholds: there is
nothing to strip, and a rel equal to the raw move would read as "all stock".

Read-only; writes nothing. `python packages/book/sector_rel.py --selftest`.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

WINDOW_DAYS = 60          # calendar days, rolling (PM, 2026-10-21: "make it 2 instead of 3 months")
BETA_SESSIONS = 250       # sessions before the window opens
BETA_MIN = 60
# build() loads window + 420 calendar days: enough for 250 sessions before the window


def _rets(px: dict) -> dict:
    d = sorted(px)
    return {d[i]: px[d[i]] / px[d[i - 1]] - 1 for i in range(1, len(d)) if px[d[i - 1]]}


QUORUM = 0.5      # share of the basket that must print for a day to count


def _basket(rets: dict, members: list[str]) -> dict:
    """Equal-weighted daily return over whichever members printed both ends
    of that day. A day under QUORUM of the members is ABSENT, not a thin
    basket: on 2026-10-22 only the two pair legs had intraday bars loaded,
    so each leg's "sector" that day was its own pair partner alone (TechM's
    -2.1% standing in for eleven IT names). basket_index uses the same rule."""
    days = sorted(set().union(*(rets[m].keys() for m in members))) if members else []
    need = max(1, -(-len(members) * QUORUM // 1))
    out = {}
    for d in days:
        xs = [rets[m][d] for m in members if d in rets[m]]
        if len(xs) >= need:
            out[d] = sum(xs) / len(xs)
    return out


def _beta(pairs) -> tuple[float, float] | None:
    n = len(pairs)
    if n < BETA_MIN:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if not sxx or not syy:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    return sxy / sxx, sxy * sxy / (sxx * syy)


def split(px: dict, peers: dict[str, dict], start: str, end: str) -> dict | None:
    """The arithmetic on plain {date: close} dicts — testable without a DB.
    `start` is the window's anchor session (the close the window runs from)."""
    pr = {m: _rets(p) for m, p in peers.items()}
    pr = {m: r for m, r in pr.items() if r}
    if not pr:
        return None
    bk = _basket(pr, sorted(pr))
    # Both legs of the split run between the SAME dates: the dates where the
    # stock has a close AND the basket has a level. The basket's level
    # compounds every basket day, so a session the stock is missing is carried
    # by the next interval on BOTH sides. The first version paired returns day
    # by day and silently dropped any basket day the stock lacked:
    # LTIMindtree has no 2026-10-21 close (Yahoo hole), so its 22-Oct return
    # spanned two sessions while the basket's 21-Oct IT rally vanished, and
    # the 7-day split read sector -0.0% / own +1.4% while every peer was up
    # 5-9%, and the Book tab's 60-day own move read -3.4 against a true -6.4.
    # Caught by move_check's first live run (2026-10-22) disagreeing with the
    # peers' own moves.
    lvl, x = {}, 1.0
    for d in sorted(bk):
        x *= 1 + bk[d]
        lvl[d] = x
    common = [d for d in sorted(px) if d in lvl and px[d]]
    iv = [(common[i - 1], common[i]) for i in range(1, len(common))]
    rs = {b: px[b] / px[a] - 1 for a, b in iv}
    bs = {b: lvl[b] / lvl[a] - 1 for a, b in iv}
    win = [d for d in sorted(rs) if start < d <= end]
    if not win:
        return None
    est = [d for d in sorted(rs) if d <= start][-BETA_SESSIONS:]
    fit = _beta([(bs[d], rs[d]) for d in est])
    beta, r2, flags = (fit[0], fit[1], []) if fit else (1.0, None, ["beta_default"])
    s = b = 1.0
    for d in win:
        s *= 1 + rs[d]
        b *= 1 + bs[d]
    stock, sector = round((s - 1) * 100, 1), round(beta * (b - 1) * 100, 1)
    # rel is the difference of the two ROUNDED figures so the hover's three
    # numbers sum exactly (price_story.decompose's convention).
    return {"rel_pct": round(stock - sector, 1), "stock_pct": stock, "sector_pct": sector,
            "basket_pct": round((b - 1) * 100, 1), "beta": round(beta, 2),
            "r2": None if r2 is None else round(r2, 2), "n_est": len(est),
            "sessions": len(win), "from": start, "to": win[-1],
            "peers": sorted(pr), "flags": flags}


def _closes(conn, eids, lo, hi) -> dict:
    out = {}
    for e in eids:
        rows = conn.execute(
            "SELECT date, close, volume FROM prices WHERE entity_id=? AND date BETWEEN ? AND ? "
            "AND close IS NOT NULL ORDER BY date", (e, lo, hi)).fetchall()
        out[e] = {d: c for d, c, v in rows if (v or 0) > 0 or v is None}
    return out


def build(conn, eids, as_of: str | None = None, cfg: dict | None = None,
          window_days: int = WINDOW_DAYS) -> dict:
    """{entity_id: split | {"withheld": reason}} for every leg entity.
    `window_days` defaults to the Book column's 60; move_check.py asks for
    short windows (7 / 30) with the same split, so "sector" means one thing."""
    import grading
    cfg = cfg or grading.load_config(conn)
    eids = [e for e in dict.fromkeys(eids) if e]
    if not as_of:                     # the latest close any leg carries
        q = ",".join("?" * len(eids))
        as_of = conn.execute(f"SELECT MAX(date) FROM prices WHERE entity_id IN ({q})", eids).fetchone()[0]
    end = dt.date.fromisoformat(as_of)
    lo = (end - dt.timedelta(days=window_days + 420)).isoformat()
    wstart = (end - dt.timedelta(days=window_days)).isoformat()
    universe = {}
    for e in eids:
        sec = cfg["sector_of"].get(e)
        universe[e] = [m for m in grading.fno_names(conn, cfg, sec, as_of) if m != e] if sec else []
    px = _closes(conn, sorted(set(eids) | {m for v in universe.values() for m in v}), lo, as_of)
    out = {}
    for e in eids:
        sec = cfg["sector_of"].get(e)
        if not sec:
            out[e] = {"withheld": "no sector for this name"}
            continue
        if not universe[e]:
            out[e] = {"withheld": f"no other F&O name in {sec}"}
            continue
        anchor = [d for d in px.get(e, {}) if d <= wstart]
        if not anchor:
            out[e] = {"withheld": "no close at the window start"}
            continue
        r = split(px[e], {m: px[m] for m in universe[e]}, max(anchor), as_of)
        out[e] = ({**r, "sector": sec, "symbol": cfg["symbol_of"].get(e)} if r
                  else {"withheld": "no common sessions with the basket"})
    return out


def _selftest() -> None:
    import random
    ok = 0
    rnd = random.Random(7)
    days = [(dt.date(2025, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(520)]
    start = days[-91]
    # peers: two noisy random walks; the stock is 1.3x their mean plus a
    # constant +0.05%/day of its own inside the window and noise before it
    pa, pb, st = {}, {}, {}
    a = b = s = 100.0
    for i, d in enumerate(days):
        ra, rb = rnd.gauss(0, 0.012), rnd.gauss(0, 0.012)
        own = 0.0005 if d > start else rnd.gauss(0, 0.002)
        a, b = a * (1 + ra), b * (1 + rb)
        s *= 1 + 1.3 * (ra + rb) / 2 + own
        pa[d], pb[d], st[d] = a, b, s
    r = split(st, {"a": pa, "b": pb}, start, days[-1])
    assert abs(r["beta"] - 1.3) < 0.05, r["beta"]; ok += 1                       # beta recovered pre-window
    assert round(r["sector_pct"] + r["rel_pct"], 1) == r["stock_pct"]; ok += 1   # the columns sum
    own_90 = ((1.0005 ** 90) - 1) * 100
    assert abs(r["rel_pct"] - own_90) < 1.5, (r["rel_pct"], own_90); ok += 1     # residual = the stock's own move
    assert r["sessions"] == 90 and not r["flags"]; ok += 1
    # acceptance AND rejection on the beta window: a short history defaults to
    # 1.0 and SAYS so; a long one does not
    short = {d: v for d, v in st.items() if d >= days[-120]}
    rs_ = split(short, {"a": pa, "b": pb}, start, days[-1])
    assert rs_["beta"] == 1.0 and rs_["flags"] == ["beta_default"]; ok += 1
    # the stock is never its own benchmark: a peer set that excludes it gives
    # a different (and the right) beta than one that includes it
    with_self = split(st, {"a": pa, "b": pb, "self": st}, start, days[-1])
    assert with_self["beta"] != r["beta"]; ok += 1
    # no peers -> None (withheld by the caller), never "rel = raw move"
    assert split(st, {}, start, days[-1]) is None; ok += 1
    # a day the basket lacks is dropped from the window, not filled
    gap = {k: {d: v for d, v in p.items() if d != days[-5]} for k, p in (("a", pa), ("b", pb))}
    assert split(st, gap, start, days[-1])["sessions"] == 89; ok += 1
    # ...and the mirror case, the one that actually happened (LTIMindtree,
    # 2026-10-21): the STOCK lacks a day the basket has. The basket must
    # compound THROUGH it, so stock and sector still cover the same span.
    # A big basket move on the missing day is the case that exposed the bug.
    pa2, pb2 = dict(pa), dict(pb)
    pa2[days[-5]] = pa2[days[-6]] * 1.10
    pb2[days[-5]] = pb2[days[-6]] * 1.10
    for k in days[-4:]:
        pa2[k], pb2[k] = pa[k] * 1.10 * pa[days[-6]] / pa[days[-5]], pb[k] * 1.10 * pb[days[-6]] / pb[days[-5]]
    hole = {d: v for d, v in st.items() if d != days[-5]}
    full = split(st, {"a": pa2, "b": pb2}, start, days[-1])
    holed = split(hole, {"a": pa2, "b": pb2}, start, days[-1])
    assert abs(holed["basket_pct"] - full["basket_pct"]) < 1e-9, (holed["basket_pct"], full["basket_pct"]); ok += 1
    assert holed["stock_pct"] == full["stock_pct"] and holed["sessions"] == 89; ok += 1
    # quorum: a day only one of four peers printed is no basket (rejection);
    # a day three of four printed is (acceptance)
    r4 = {m: {"d1": 0.01, "d2": 0.02} for m in "wxyz"}
    r4["w"]["d3"] = -0.05
    for m in "wxy":
        r4[m]["d4"] = 0.03
    bq = _basket(r4, list("wxyz"))
    assert "d3" not in bq and abs(bq["d4"] - 0.03) < 1e-12; ok += 1
    # DB path: phantom zero-volume rows dropped, prices clipped to as_of
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE prices (entity_id TEXT, date TEXT, close REAL, volume REAL)")
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?)",
                     [("x", "2026-01-01", 10, 5), ("x", "2026-01-02", 10, 0),
                      ("x", "2026-01-03", 11, 5), ("x", "2026-01-04", 12, 5)])
    c = _closes(conn, ["x"], "2026-01-01", "2026-01-03")
    assert c["x"] == {"2026-01-01": 10, "2026-01-03": 11}; ok += 1
    print(f"sector_rel selftest: {ok} checks passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.path.insert(0, str(REPO / "packages" / "adapters"))
        import grading
        conn = grading.connect()
        cfg = grading.load_config(conn)
        eids = sorted({u[k] for u in cfg["units"] for k in ("long", "short") if u.get(k)})
        for e, r in build(conn, eids, sys.argv[1] if len(sys.argv) > 1 else None, cfg).items():
            if "withheld" in r:
                print(f"{e:18} withheld: {r['withheld']}")
            else:
                print(f"{e:18} {r['symbol'] or '':11} rel {r['rel_pct']:+6.1f}  stock {r['stock_pct']:+6.1f}"
                      f"  sector {r['sector_pct']:+6.1f} (beta {r['beta']}, {len(r['peers'])} peers)"
                      f"  {r['from']}..{r['to']} {' '.join(r['flags'])}")
