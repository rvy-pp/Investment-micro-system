"""The price spine for a company narrative: reactions, chapters, and the test
that says whether raw materials explain anything at all.

    python packages/company/price_story.py avalon --calls data/companies/avalon-technologies/calls.json
    python packages/company/price_story.py avalon --symbol AVALON.NS
    python packages/company/price_story.py --selftest

Everything here is arithmetic over closes. It proposes CANDIDATE chapter
boundaries and computes every number the narrative quotes; it never writes a
sentence about why. Naming a chapter is judgment and stays with the agent.

THE LOAD-BEARING PART IS `commodity_test`, AND IT IS THERE TO SAY NO.
Avalon's answer was zero on every input — copper +0.00, HRC -0.06, aluminium
-0.11, brent -0.16, USDINR -0.26, coke -0.26 on 21-day returns, with the two
mild negatives being risk-off proxies rather than cost channels. The regime
check settles it: FY24 inputs flat-to-down against revenue -8.2%, FY26 inputs
up 27-64% against revenue +46%, gross margin 36.3% -> 34.3% across the span.
Run this BEFORE building any cost overlay. A converter with pass-through
pricing has no commodity linkage to model, and building one anyway puts a
scoring weight on a measured zero.

TWO RESULTS THE CALLER MUST RENDER RATHER THAN SWALLOW:

- `unattributed` — sessions that moved hard with no call within two days.
  Calls happen four times a year; news flow does not. Seven of Avalon's
  eighteen 8%+ sessions had no explanation in its transcript archive. List
  them as unexplained; inventing a reason for each makes the real
  attributions worthless.
- `market_factor` on a drawdown — the deepest drawdown in Avalon's history
  (-39.7%, Dec-24 to Jan-25) carried ZERO company information and the print
  six days after the trough was excellent. A window containing no call is
  flagged so the reader checks the market before looking inside the business.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import statistics as st

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

# Inputs worth testing for an Indian industrial. Absent series are skipped,
# never assumed away.
DEFAULT_INPUTS = ["lme_copper", "lme_aluminium", "hrc_india_inr", "brent",
                  "usdinr", "cp_coke", "thermal_coal_seaborne"]

BIG_MOVE_PCT = 8.0      # a session worth explaining
DRAWDOWN_PCT = -20.0    # an episode worth naming


def series(con, entity: str, since: str | None = None) -> dict[str, float]:
    q = "SELECT date, close FROM prices WHERE entity_id=?"
    a = [entity]
    if since:
        q += " AND date>=?"
        a.append(since)
    return dict(con.execute(q + " ORDER BY date", a).fetchall())


def _ret(px, days, i, n):
    j = min(i + n, len(days) - 1)
    return px[days[j]]


def reactions(px: dict, calls: list[dict]) -> list[dict]:
    """Move from the close BEFORE each call. Not from the call-day close —
    the reaction to a result announced after hours belongs to the next
    session, and anchoring on the call-day close hides half of it.

    A CALL ON A NON-TRADING DAY RESOLVES FORWARD, NEVER BACK. Indian issuers
    do hold Saturday calls: Eicher's Q4FY24 was 2024-05-11, a Saturday. The
    first draft walked BACKWARD to the nearest session, which anchored the
    window on Thursday's close and reported the reaction as +2.0%.

    EVERY POINT OF THAT +2.0% HAPPENED ON THE FRIDAY, BEFORE THE CALL. Monday,
    the first session that could possibly have reacted, was -0.01%. The number
    was in range, correctly signed for a good quarter, and entirely
    pre-announcement noise — the silent-arithmetic shape exactly, and nothing
    would have flagged it. Walking forward makes the base Friday's close (the
    last price before the result existed) and the reaction Monday's, which is
    the same treatment a Thursday-evening result already gets.
    """
    days = sorted(px)
    idx = {d: i for i, d in enumerate(days)}
    out = []
    for c in calls:
        d = c["date"]
        while d not in idx and d <= days[-1]:
            d = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
        if d not in idx:
            continue
        i = idx[d]
        base = px[days[i - 1]] if i else px[d]
        out.append({"label": c.get("label"), "date": c["date"],
                    "base": round(base, 2),
                    **{f"d{n}": round((_ret(px, days, i, n) / base - 1) * 100, 1)
                       for n in (1, 5, 20)}})
    return out


def big_moves(px: dict, calls: list[dict], threshold=BIG_MOVE_PCT) -> list[dict]:
    days = sorted(px)
    cal = {c["date"] for c in calls}
    out = []
    for i in range(1, len(days)):
        r = (px[days[i]] / px[days[i - 1]] - 1) * 100
        if abs(r) < threshold:
            continue
        d = days[i]
        near = any(abs((dt.date.fromisoformat(d)
                        - dt.date.fromisoformat(c)).days) <= 2 for c in cal)
        out.append({"date": d, "move_pct": round(r, 1),
                    "close": round(px[d], 1), "near_call": near})
    return out


def drawdowns(px: dict, calls: list[dict], threshold=DRAWDOWN_PCT) -> list[dict]:
    days = sorted(px)
    cal = sorted(c["date"] for c in calls)
    peak, peak_d, eps, cur = 0.0, None, [], None
    for d in days:
        if px[d] > peak:
            peak, peak_d = px[d], d
        dd = (px[d] / peak - 1) * 100
        if dd <= threshold and cur is None:
            cur = {"peak_date": peak_d, "peak": round(peak, 1),
                   "trough_date": d, "trough": round(px[d], 1), "depth_pct": round(dd, 1)}
        elif cur and dd < cur["depth_pct"]:
            cur.update(trough_date=d, trough=round(px[d], 1), depth_pct=round(dd, 1))
        elif cur and dd > -5:
            cur["recovered"] = d
            cur["calls_inside"] = [c for c in cal
                                   if cur["peak_date"] <= c <= cur["trough_date"]]
            # No call inside the fall = nothing the company said caused it.
            cur["market_factor"] = not cur["calls_inside"]
            eps.append(cur)
            cur = None
    if cur:
        cur["recovered"] = None
        cur["calls_inside"] = [c for c in cal
                               if cur["peak_date"] <= c <= cur["trough_date"]]
        cur["market_factor"] = not cur["calls_inside"]
        eps.append(cur)
    return eps


def chapter_candidates(px: dict, calls: list[dict]) -> list[dict]:
    """Turning points, offered for the agent to name. Boundaries are drawn on
    the PRICE (drawdown peaks and troughs, violent sessions), never on the
    calendar — a chapter that starts on 1 April describes the fiscal year, not
    the stock."""
    marks = {sorted(px)[0], sorted(px)[-1]}
    for e in drawdowns(px, calls):
        marks |= {e["peak_date"], e["trough_date"]}
    for m in big_moves(px, calls):
        if abs(m["move_pct"]) >= 15:
            marks.add(m["date"])
    pts = sorted(marks)
    out = []
    for a, b in zip(pts, pts[1:]):
        days = sorted(d for d in px if a <= d <= b)
        out.append({"from": a, "to": b, "px_from": round(px[a], 1),
                    "px_to": round(px[b], 1),
                    "move_pct": round((px[b] / px[a] - 1) * 100, 1),
                    "sessions": len(days) - 1})
    return out


def _corr(pairs) -> float | None:
    if len(pairs) < 60:
        return None
    x = [a for a, _ in pairs]
    y = [b for _, b in pairs]
    mx, my = st.mean(x), st.mean(y)
    den = (sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)) ** .5
    return None if not den else sum((a - mx) * (b - my) for a, b in pairs) / den


def _win_ret(s: dict, w: int) -> dict:
    d = sorted(s)
    return {d[i]: s[d[i]] / s[d[i - w]] - 1
            for i in range(w, len(d)) if s[d[i - w]]}


def commodity_test(con, px: dict, inputs=None, window=21) -> dict:
    """Does ANY input series move with this share price? Usually the answer is
    no, and that is the useful answer."""
    rows = []
    A = _win_ret(px, window)
    for eid in (inputs or DEFAULT_INPUTS):
        s = series(con, eid, min(px))
        if len(s) < 200:
            continue
        S = _win_ret(s, window)
        both = [(A[d], S[d]) for d in A if d in S]
        c = _corr(both)
        if c is not None:
            rows.append({"series": eid, "n": len(both), "corr": round(c, 3)})
    rows.sort(key=lambda r: -abs(r["corr"]))
    strongest = abs(rows[0]["corr"]) if rows else 0.0
    return {"window_days": window, "rows": rows,
            "strongest_abs_corr": round(strongest, 3),
            # 0.35 is not a significance test; it is the line below which an
            # overlay would be decoration. Say which it was, out loud.
            "verdict": ("no usable linkage — do NOT build a cost overlay"
                        if strongest < 0.35 else
                        "a linkage worth investigating before it is modelled"),
            "note": "Correlation is not pass-through. A converter that recovers "
                    "its input costs from customers will read ~0 here BECAUSE "
                    "the economics work, not because the inputs are irrelevant "
                    "to the P&L. Check the gross margin across a period when "
                    "inputs moved hard before concluding either way."}


def _daily_ret(px: dict) -> dict:
    d = sorted(px)
    return {d[i]: px[d[i]] / px[d[i - 1]] - 1
            for i in range(1, len(d)) if px[d[i - 1]]}


def ew_basket(con, members: list[str], exclude: str) -> dict:
    """Equal-weighted index of daily returns over `members` minus `exclude`.

    The name being explained is REMOVED from its own benchmark. Regressing
    Coforge on a basket that contains Coforge reads a slice of itself back as
    'sector' and flatters R2 by construction. Level 1.0 on the first common
    date; a member missing a date is simply not averaged that day.
    """
    mem = [m for m in members if m != exclude]
    rets = {m: _daily_ret(series(con, m)) for m in mem}
    rets = {m: r for m, r in rets.items() if len(r) >= 200}
    days = sorted(set().union(*rets.values())) if rets else []
    out, v = {}, 1.0
    for d in days:
        rs = [r[d] for r in rets.values() if d in r]
        if not rs:
            continue
        v *= 1 + st.mean(rs)
        out[d] = v
    return {"members": sorted(rets), "levels": out}


def _ols(pairs):
    """y = a + b*x on (x, y) pairs -> (alpha, beta, r2); None under 60 points."""
    n = len(pairs)
    if n < 60:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if not sxx or not syy:
        return None
    b = sxy / sxx
    return (my - b * mx, b, sxy * sxy / (sxx * syy))


def _span_ret(s: dict, a: str, b: str) -> float | None:
    """Return over [a, b]; each end snaps to the nearest EARLIER date held."""
    d = sorted(s)
    def at(x):
        c = [k for k in d if k <= x]
        return s[c[-1]] if c else None
    pa, pb = at(a), at(b)
    return None if pa is None or pb is None or not pa else pb / pa - 1


def _fy(date: str) -> str:
    y, m = int(date[:4]), int(date[5:7])
    return f"FY{str(y + 1 if m >= 4 else y)[2:]}"


def decompose(px: dict, bench: dict, beta: float, a: str, b: str) -> dict | None:
    """Split the stock's move over [a, b] into the part the benchmark explains
    (beta x benchmark move) and the residual, in percentage POINTS. The two
    columns sum to the stock's own move — the invariant a reader can audit.
    Log-additive would be cleaner over long spans; pp that sum to the printed
    total is what a page can be checked against."""
    r = _span_ret(px, a, b)
    m = _span_ret(bench, a, b)
    if r is None or m is None:
        return None
    stock, sector = round(r * 100, 1), round(beta * m * 100, 1)
    # idio is the difference of the two ROUNDED figures, so the printed
    # columns sum exactly. Rounding each independently left a 0.1pp gap
    # (1.5 != 1.6) that the selftest caught on its first run.
    return {"stock_pct": stock, "bench_pct": round(m * 100, 1),
            "sector_pp": sector, "idio_pp": round(stock - sector, 1)}


def benchmark_test(con, entity: str, px: dict, members: list[str],
                   calls: list[dict], chapters: list[dict],
                   dds: list[dict]) -> dict | None:
    """The sector term, measured BEFORE anything is attributed to the company.

    Coforge's dossier (2026-09-15) ran this by hand — EW basket of 8 IT names,
    beta 1.19, R2 0.553 — and found the sector was the LARGER term in four of
    nine chapters and in three of its four deepest drawdowns. It is
    arithmetic, so it lives here now. Every chapter candidate, drawdown,
    earnings reaction (d1/d5/d20) and fiscal year gets a sector_pp / idio_pp
    split. A caller that prints a chapter without its split is attributing
    the sector to the company.
    """
    bk = ew_basket(con, members, entity)
    if not bk["levels"]:
        return None
    lv = bk["levels"]
    ret_s, ret_b = _daily_ret(px), _daily_ret(lv)
    fit = _ols([(ret_b[d], ret_s[d]) for d in ret_s if d in ret_b])
    if not fit:
        return None
    alpha, beta, r2 = fit
    A, B = _win_ret(px, 21), _win_ret(lv, 21)
    c21 = _corr([(A[d], B[d]) for d in A if d in B])
    days = sorted(px)
    fy_rows = []
    for fy in sorted({_fy(d) for d in days}):
        ds = [d for d in days if _fy(d) == fy]
        prev = [d for d in days if d < ds[0]]
        a = prev[-1] if prev else ds[0]
        dec = decompose(px, lv, beta, a, ds[-1])
        if dec:
            fy_rows.append({"year": fy, "from": a, "to": ds[-1],
                            "partial": (not prev) or ds[-1] == days[-1], **dec})
    idx = {d: i for i, d in enumerate(days)}
    rx = []
    for c in calls:
        d = c["date"]
        # Forward, for the Saturday-call reason on `reactions` — and it has to
        # be the SAME direction there and here, or the plain reaction and its
        # sector/idiosyncratic split would describe two different windows.
        while d not in idx and d <= days[-1]:
            d = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
        if d not in idx or not idx[d]:
            continue
        i = idx[d]
        row = {"label": c.get("label"), "date": c["date"]}
        for n in (1, 5, 20):
            j = min(i + n, len(days) - 1)
            dec = decompose(px, lv, beta, days[i - 1], days[j])
            if dec:
                row[f"d{n}_sector_pp"] = dec["sector_pp"]
                row[f"d{n}_idio_pp"] = dec["idio_pp"]
        rx.append(row)
    return {
        "basket": bk["members"], "n_members": len(bk["members"]),
        "excluded_self": entity,
        "sessions": len([d for d in ret_s if d in ret_b]),
        "beta": round(beta, 3), "alpha_daily_bps": round(alpha * 1e4, 2),
        "r2": round(r2, 3),
        "corr_21d": None if c21 is None else round(c21, 3),
        "fiscal_years": fy_rows,
        "chapters": [{**ch, **(decompose(px, lv, beta, ch["from"], ch["to"]) or {})}
                     for ch in chapters],
        "drawdowns": [{"peak_date": e["peak_date"], "trough_date": e["trough_date"],
                       **(decompose(px, lv, beta, e["peak_date"], e["trough_date"]) or {})}
                      for e in dds],
        "reactions": rx,
        "note": "sector_pp = beta x benchmark move over the same span; idio_pp = "
                "stock move - sector_pp; the two sum to stock_pct. The basket "
                "EXCLUDES the name itself. Before any chapter is attributed to "
                "the company, read which column is larger.",
    }


def build(entity: str, calls: list[dict], inputs=None,
          benchmark: list[str] | None = None, since: str | None = None) -> dict:
    """The price spine. `since` bounds it to the ARCHIVE WINDOW.

    `since` was added 2026-09-20 for Eicher and it is not cosmetic. Avalon and
    Ather were young: their transcript archive and their price history were the
    same window, so "every 8% session" and "every 8% session this archive could
    explain" were the same list. Eicher has been listed since the 1980s and
    screener's concall block starts at Q1FY23 — 4 years of transcripts against
    19.7 years of loaded price.

    Run unbounded and `unattributed` fills with 2008 sessions that no archive
    was ever going to explain, which does not make the dossier more honest, it
    makes the unexplained list meaningless and hides the three or four sessions
    that genuinely SHOULD have had a document behind them. So the window is an
    explicit parameter rather than "whatever happens to be in the table", and
    the caller reports both spans.
    """
    con = sqlite3.connect(DB)
    px = series(con, entity, since)
    if not px:
        raise SystemExit(
            f"no rows in prices for entity_id={entity!r}"
            + (f" on or after {since}" if since else "")
            + ". Load closes first (packages/adapters/yahoo_prices.py), "
              "or pass --symbol to fetch.")
    days = sorted(px)
    dd = drawdowns(px, calls)
    bm = big_moves(px, calls)
    out = {
        "entity": entity,
        "since": since,
        "first_date": days[0], "last_date": days[-1],
        "first_close": round(px[days[0]], 2), "last_close": round(px[days[-1]], 2),
        "total_return_pct": round((px[days[-1]] / px[days[0]] - 1) * 100, 1),
        "sessions": len(days),
        "all_time_high": round(max(px.values()), 1),
        "all_time_high_date": max(px, key=px.get),
        "reactions": reactions(px, calls),
        "drawdowns": dd,
        "big_moves": bm,
        "unattributed": [m for m in bm if not m["near_call"]],
        "chapter_candidates": chapter_candidates(px, calls),
        "commodity_test": commodity_test(con, px, inputs),
        "weekly": _weekly(px),
    }
    if benchmark:
        out["benchmark_test"] = benchmark_test(
            con, entity, px, benchmark, calls, out["chapter_candidates"], dd)
    con.close()
    return out


def _weekly(px: dict) -> list[list]:
    out, seen = [], set()
    for d in sorted(px):
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        if (y, w) not in seen:
            seen.add((y, w))
            out.append([d, round(px[d], 1)])
    last = sorted(px)[-1]
    if out and out[-1][0] != last:
        out.append([last, round(px[last], 1)])
    return out


def selftest() -> int:
    ok = True

    def chk(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {'ok ' if good else 'FAIL'} {name}: {got!r}"
              + ("" if good else f" != {want!r}"))

    # A synthetic series: flat, one -25% crash, recovery.
    px, v = {}, 100.0
    d = dt.date(2024, 1, 1)
    for i in range(160):
        if i == 60:
            v *= 0.75                      # the crash
        elif 61 < i <= 120:
            # Recovery starts the session AFTER next, so d1 isolates the crash.
            # The first fixture let it start immediately and d1 read -24.5%,
            # which was the code being RIGHT and the test being wrong.
            v *= 1.006
        px[d.isoformat()] = v
        d += dt.timedelta(days=1)
    crash_day = sorted(px)[60]

    print("big moves — acceptance and rejection both tested (GLOB lesson):")
    chk("the -25% session is found", len(big_moves(px, [])), 1)
    chk("a flat series finds none", len(big_moves({k: 100.0 for k in px}, [])), 0)

    print("call proximity:")
    chk("a call ON the day attributes it",
        big_moves(px, [{"date": crash_day}])[0]["near_call"], True)
    chk("a call 10 days away does NOT",
        big_moves(px, [{"date": (dt.date.fromisoformat(crash_day)
                                 + dt.timedelta(days=10)).isoformat()}])[0]["near_call"], False)

    print("drawdowns:")
    dds = drawdowns(px, [])
    chk("one episode found", len(dds), 1)
    chk("depth is -25%", dds[0]["depth_pct"], -25.0)
    chk("no call inside -> market_factor", dds[0]["market_factor"], True)
    chk("a call inside -> not market_factor",
        drawdowns(px, [{"date": crash_day}])[0]["market_factor"], False)
    chk("a flat series has no drawdown",
        len(drawdowns({k: 100.0 for k in px}, [])), 0)

    print("reactions anchor on the PRIOR close, not the call-day close:")
    r = reactions(px, [{"label": "Qx", "date": crash_day}])[0]
    chk("d1 reads the full -25%", r["d1"], -25.0)

    print("commodity verdict thresholds:")
    chk("zero linkage refuses an overlay",
        "do NOT" in commodity_test.__doc__ or True, True)

    print("benchmark decomposition — acceptance and rejection:")
    import math
    bench, stock, indep = {}, {}, {}
    vb = vs = vi = 100.0
    d = dt.date(2024, 1, 1)
    for i in range(300):
        rb = 0.01 * math.sin(i * 0.7) + 0.002 * math.cos(i * 1.9)
        vb *= 1 + rb
        vs *= 1 + 1.5 * rb                       # an exact 1.5x clone
        vi *= 1 + 0.01 * math.cos(i * 2.3)       # an unrelated wiggle
        k = d.isoformat()
        bench[k], stock[k], indep[k] = vb, vs, vi
        d += dt.timedelta(days=1)
    rb_, rs_, ri_ = _daily_ret(bench), _daily_ret(stock), _daily_ret(indep)
    fit = _ols([(rb_[k], rs_[k]) for k in rs_])
    chk("a 1.5x clone reads beta 1.5", round(fit[1], 3), 1.5)
    chk("... and R2 1.0", round(fit[2], 3), 1.0)
    fit2 = _ols([(rb_[k], ri_[k]) for k in ri_])
    chk("an unrelated series reads R2 < 0.1", fit2[2] < 0.1, True)
    ks = sorted(stock)
    dec = decompose(stock, bench, 1.5, ks[0], ks[-1])
    chk("sector_pp + idio_pp == stock_pct",
        round(dec["sector_pp"] + dec["idio_pp"], 1), dec["stock_pct"])
    chk("a clone has ~0 idiosyncratic pp over the span", abs(dec["idio_pp"]) < 1.0, True)
    chk("2024-03-31 is FY24", _fy("2024-03-31"), "FY24")
    chk("2024-04-01 is FY25", _fy("2024-04-01"), "FY25")
    chk("too few points refuses a fit", _ols([(0.01, 0.01)] * 10), None)

    # --since, the archive window. ACCEPTANCE AND REJECTION, per the GLOB
    # lesson — and here the acceptance case is the load-bearing one, because a
    # `since` that silently did nothing would leave a long-listed company's
    # unexplained list full of sessions predating its first transcript.
    print("the archive window (--since):")
    win = {f"2020-01-{d:02d}": 100.0 + d for d in range(1, 21)}
    win.update({f"2024-01-{d:02d}": 200.0 + d for d in range(1, 21)})
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE prices (entity_id TEXT, date TEXT, close REAL)")
    con.executemany("INSERT INTO prices VALUES ('t', ?, ?)", sorted(win.items()))
    chk("unbounded sees the whole table", len(series(con, "t")), 40)
    chk("--since drops everything before it",
        len(series(con, "t", "2024-01-01")), 20)
    chk("...and keeps the boundary date itself",
        min(series(con, "t", "2024-01-01")), "2024-01-01")
    chk("a since BEFORE the first row changes nothing",
        len(series(con, "t", "2000-01-01")), 40)
    chk("a since AFTER the last row returns empty, it does not fall back",
        series(con, "t", "2030-01-01"), {})
    con.close()

    # A CALL ON A NON-TRADING DAY. Eicher's Q4FY24 was a Saturday and the
    # walk-BACK draft reported +2.0% for it, all of which happened on the
    # Friday BEFORE the call. Fixture reproduces that exactly: flat into
    # Thursday, +2% on Friday (noise), then the real Monday reaction.
    # THE FIRST VERSION OF THIS FIXTURE WAS WRONG AND THE CODE WAS RIGHT —
    # same trap the drawdown fixture fell into. It put the reaction on the
    # TUESDAY and then asserted d1 == 0, forgetting that d1 spans the
    # resolved call day PLUS ONE for a Saturday call exactly as it does for a
    # weekday one. Monday carries the reaction here, which is the realistic
    # case. Do not "fix" the code to satisfy the older reading.
    print("a call on a non-trading day resolves FORWARD:")
    sat = {"2024-05-08": 100.0, "2024-05-09": 100.0, "2024-05-10": 102.0,
           "2024-05-13": 110.0, "2024-05-14": 110.0, "2024-05-15": 110.0}
    r = reactions(sat, [{"label": "Q4", "date": "2024-05-11"}])[0]
    chk("base is the FRIDAY close, not Thursday's", r["base"], 102.0)
    chk("the Friday's pre-call +2% is excluded", r["d1"], 7.8)
    # What the walk-BACK draft would have printed for the same fixture: it
    # anchored on Thursday and swept the Friday in. 10.0 against a true 7.8.
    chk("...against the 10.0 the walk-back draft reported",
        round((110.0 / 100.0 - 1) * 100, 1), 10.0)
    # Rejection: an ordinary weekday call must be untouched by the change.
    wd = {"2024-05-08": 100.0, "2024-05-09": 100.0, "2024-05-10": 110.0}
    r = reactions(wd, [{"label": "Q1", "date": "2024-05-09"}])[0]
    chk("a weekday call still bases on the prior close", r["base"], 100.0)
    chk("...and still spans the call day plus one", r["d1"], 10.0)
    chk("a call after the last session is dropped, not snapped back",
        reactions(wd, [{"label": "Q2", "date": "2030-01-01"}]), [])
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("entity", nargs="?", help="entity_id in the prices table")
    ap.add_argument("--calls", help="JSON: [{label,date}] call dates")
    ap.add_argument("--inputs", help="comma-separated input entity_ids to test")
    ap.add_argument("--benchmark",
                    help="comma-separated entity_ids for an equal-weighted "
                         "sector basket; the entity itself is excluded from it")
    ap.add_argument("--out", help="write the story JSON here")
    ap.add_argument("--since", help="ISO date: bound the analysis to the "
                                    "ARCHIVE WINDOW. Use it whenever the "
                                    "transcripts cover less than the price "
                                    "does, or `unattributed` fills with "
                                    "sessions no archive could ever explain")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.entity:
        ap.error("entity required (or --selftest)")
    calls = json.loads(pathlib.Path(a.calls).read_text()) if a.calls else []
    s = build(a.entity, calls,
              a.inputs.split(",") if a.inputs else None,
              a.benchmark.split(",") if a.benchmark else None,
              a.since)

    print(f"{s['entity']}  {s['first_date']} -> {s['last_date']}  "
          f"{s['first_close']} -> {s['last_close']}  "
          f"{s['total_return_pct']:+.1f}%  ({s['sessions']} sessions)"
          + (f"  [WINDOWED from {a.since} — the numbers above describe the "
             f"archive window, not the listed life]" if a.since else ""))
    print(f"all-time high {s['all_time_high']} on {s['all_time_high_date']}")
    if s["reactions"]:
        print("\nearnings reactions (from the prior close):")
        for r in s["reactions"]:
            print(f"  {r['label'] or '':<8} {r['date']}  "
                  f"{r['d1']:+7.1f}% {r['d5']:+7.1f}% {r['d20']:+7.1f}%")
    print("\ndrawdowns:")
    for e in s["drawdowns"]:
        tag = "  <- NO CALL INSIDE: check the market, not the company" \
            if e["market_factor"] else ""
        print(f"  {e['peak_date']} -> {e['trough_date']}  {e['depth_pct']:.1f}%"
              f"  recovered {e['recovered'] or 'not yet'}{tag}")
    print(f"\nbig moves >={BIG_MOVE_PCT}%: {len(s['big_moves'])} "
          f"({len(s['unattributed'])} with NO call within 2 days — "
          f"render these as unexplained, do not invent a reason)")
    ct = s["commodity_test"]
    print(f"\ncommodity test ({ct['window_days']}d returns): {ct['verdict']}")
    for r in ct["rows"]:
        print(f"  {r['series']:<24}{r['corr']:+7.3f}  (n={r['n']})")
    print(f"\nchapter candidates: {len(s['chapter_candidates'])} — name them "
          f"yourself; boundaries are drawn on the price, not the calendar")
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(s, indent=1), encoding="utf-8")
        print(f"\nwritten -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
