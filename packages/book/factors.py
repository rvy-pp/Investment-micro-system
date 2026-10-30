"""Factor exposures of the PM's book - the factor check (2026-10-30).

PM, after the gross conversation ("how do these Millennium PMs run 70-80%
gross consistently? We have trouble at 40s itself"): *"do the factor check
for our portfolio"*. A pod runs high gross because its residual risk per
unit of gross is small: beta, sector and style are hedged by rule and only
the stock-specific part is left. This module measures what is left HERE.

WHAT IS MEASURED. The latest snapshot's book held constant in DOLLARS (the
var.py exposure: IMS MV% x NAV per leg, options and unmapped tickers out and
named), run through the legs' own daily returns over the last LOOKBACK
common sessions - the same series var.py's VaR is built on. That P&L series
is regressed on four factor returns, all from flow_series (never prices):

    market   Nifty 50
    it       Nifty IT      minus Nifty 50     (the sector, net of market)
    metal    Nifty Metal   minus Nifty 50
    size     Nifty Midcap 150 minus Nifty 50  (loaded 2026-10-30, evidence only)

Coefficients are USD per 1% factor move, with OLS t-stats. The split that
matters is sd(P&L) against sd(residual): the share of the book's daily
variance the factors explain is the part a pod would have hedged away.

Two readings that a single regression cannot give are computed beside it:

- SECTOR NETS on the book's OWN sectors (grading's sector map, the same
  "sector" as the Book tab's 60d column and pair grading): long, short and
  net USD per sector, and the net scaled by each leg's beta to its own
  equal-weighted F&O basket (sector_rel's basket rule, quorum half). A
  pair that is long one cement name and short another at equal dollars can
  still be net long cement if the long's beta is higher.
- MOMENTUM TILT: each leg's 12-1 month return (sessions t-252 .. t-21, the
  academic definition) weighted by its dollars, longs against shorts,
  against the median of every company series in `prices`. There is no
  external momentum index with daily history on Yahoo (NIFTY200MOMENTM30.NS
  answers one row), and a factor built from the store's ~45 names would be
  mostly the book's own legs, so the tilt is reported as a tilt, not
  regressed.

NOT MEASURED, and said: value. There is no cross-sectional multiple history
in the store (the Yahoo consensus covers IT and EMS only, and screener's
trailing multiples cover nine names), so a value loading cannot be
estimated honestly and is not guessed.

Per-leg univariate betas to each factor are also returned: the book's
univariate beta to a factor is EXACTLY the dollar-weighted sum of the legs'
betas (linearity), so the "who carries this exposure" table adds up to the
headline by construction, and the selftest checks that it does.

Read-only, no table, basket_index's reason: it recomputes in about a second
and a stored copy would be a second thing that can be stale.

    python packages/book/factors.py              # report on the latest snapshot
    python packages/book/factors.py --selftest
"""
from __future__ import annotations

import math
import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))
sys.path.insert(0, str(REPO / "packages" / "book"))
sys.path.insert(0, str(REPO / "packages" / "adapters"))

LOOKBACK = 250        # sessions; the Nifty beta convention (sector_rel, grading)
RECENT = 60           # a second, noisier fit on the current regime
MOM_LONG, MOM_SKIP = 252, 21
BASKET_QUORUM = 0.5

FACTORS = [
    # key, label, flow_series id, net of nifty?
    ("market", "Nifty 50", "nifty", False),
    ("it", "Nifty IT - Nifty", "nifty_it", True),
    ("metal", "Nifty Metal - Nifty", "nifty_metal", True),
    ("size", "Midcap 150 - Nifty", "nifty_midcap150", True),
]


# ---------------------------------------------------------------- algebra --

def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    """Gauss-Jordan with partial pivoting; A is square and small."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-14:
            raise ValueError("singular design matrix")
        M[c], M[p] = M[p], M[c]
        pv = M[c][c]
        M[c] = [v / pv for v in M[c]]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c]
                M[r] = [rv - f * cv for rv, cv in zip(M[r], M[c])]
    return [M[i][n] for i in range(n)]


def _inverse(A: list[list[float]]) -> list[list[float]]:
    n = len(A)
    cols = [_solve(A, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def ols(y: list[float], X: list[list[float]]) -> dict:
    """y on X with an intercept. Returns coef (intercept first), se, t, r2,
    sd of y and of the residual. Plain OLS standard errors."""
    n, k = len(y), len(X[0]) + 1
    Z = [[1.0] + row for row in X]
    XtX = [[sum(Z[t][i] * Z[t][j] for t in range(n)) for j in range(k)] for i in range(k)]
    Xty = [sum(Z[t][i] * y[t] for t in range(n)) for i in range(k)]
    beta = _solve(XtX, Xty)
    fit = [sum(b * z for b, z in zip(beta, row)) for row in Z]
    res = [yi - fi for yi, fi in zip(y, fit)]
    ssr = sum(r * r for r in res)
    my = sum(y) / n
    sst = sum((yi - my) ** 2 for yi in y)
    dof = max(n - k, 1)
    s2 = ssr / dof
    inv = _inverse(XtX)
    se = [math.sqrt(max(s2 * inv[i][i], 0.0)) for i in range(k)]
    return {"coef": beta, "se": se,
            "t": [b / s if s else 0.0 for b, s in zip(beta, se)],
            "r2": 1 - ssr / sst if sst else 0.0, "n": n,
            "sd_y": math.sqrt(sst / n) if n else 0.0,
            "sd_res": math.sqrt(ssr / n) if n else 0.0}


def beta1(y: list[float], x: list[float]) -> float | None:
    n = len(x)
    if n < 2:
        return None
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((a - mx) ** 2 for a in x)
    if not vx:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / vx


# ------------------------------------------------------------------- data --

def factor_returns(conn, dates: list[str]) -> tuple[dict, list[str], list[str]]:
    """{factor_key: [return % per interval]} on the book's common intervals.
    An interval a factor cannot price (no close at either end) is dropped
    from EVERY factor so the design matrix stays rectangular; the dropped
    dates are returned."""
    closes = {}
    for key, _, sid, _ in FACTORS:
        closes[sid] = dict(conn.execute(
            "SELECT date, close FROM flow_series WHERE series_id=? ORDER BY date", (sid,)).fetchall())
    raw = {key: [] for key, *_ in FACTORS}
    keep, dropped = [], []
    for a, b in zip(dates, dates[1:]):
        row = {}
        ok = True
        for key, _, sid, net in FACTORS:
            c = closes[sid]
            if a not in c or b not in c or not c[a]:
                ok = False
                break
            row[key] = (c[b] / c[a] - 1) * 100
        if not ok:
            dropped.append(b)
            continue
        for key, _, sid, net in FACTORS:
            raw[key].append(row[key] - row["market"] if net else row[key])
        keep.append(b)
    return raw, keep, dropped


def momentum(bars: dict, grid: list[str], eid: str) -> float | None:
    """12-1 month return: close at session t-21 over close at t-252."""
    s = [d for d in grid if d in bars.get(eid, {})]
    if len(s) <= MOM_LONG:
        return None
    a, b = s[-MOM_LONG - 1], s[-MOM_SKIP - 1]
    return bars[eid][b][3] / bars[eid][a][3] - 1


def basket_returns(bars: dict, members: list[str], dates: list[str]) -> dict:
    """Equal-weighted return of whichever members printed both ends, quorum
    half - sector_rel._basket on the book's own interval grid."""
    need = max(1, math.ceil(len(members) * BASKET_QUORUM))
    out = {}
    for a, b in zip(dates, dates[1:]):
        xs = [bars[m][b][3] / bars[m][a][3] - 1 for m in members
              if a in bars.get(m, {}) and b in bars.get(m, {}) and bars[m][a][3]]
        if len(xs) >= need:
            out[b] = sum(xs) / len(xs)
    return out


# ------------------------------------------------------------------ build --

def build(conn: sqlite3.Connection | None = None, as_of: str | None = None) -> dict:
    import var
    import grading
    from corporate_actions import CONFIRMED_ACTIONS
    own = conn is None
    if own:
        import book_io
        conn = book_io.connect()
    try:
        tmap, names, _ = var._spec()
        cfg = grading.load_config(conn)
        snaps = [r[0] for r in conn.execute(
            "SELECT snap_date FROM book_snapshots WHERE snap_date<=? ORDER BY snap_date",
            (as_of or "9999-12-31",))]
        if not snaps:
            return {"error": "no snapshot"}
        snap = snaps[-1]
        ex = var.exposures(conn, snap, tmap)
        eids = sorted(ex["legs"])
        nav = ex["nav"]
        x = {e: ex["legs"][e]["usd"] for e in eids}
        tok = {e: ex["legs"][e]["tokens"][0] for e in eids}
        sector_of = {e: cfg["sector_of"].get(e, "other") for e in eids}

        # the legs' own bars, plus every F&O name of each sector for the baskets
        latest = conn.execute("SELECT MAX(date) FROM prices WHERE entity_id=?", (eids[0],)).fetchone()[0]
        members: dict[str, list[str]] = {}
        for sec in sorted(set(sector_of.values())):
            if sec == "other":
                continue
            members[sec] = grading.fno_names(conn, cfg, sec, latest)
        universe = sorted(set(eids) | {m for v in members.values() for m in v})
        bars, vols = var.load_bars(conn, universe, None)
        grid = var.session_grid({e: bars[e] for e in eids}, {e: vols[e] for e in eids})
        dates, R, zeroed = var.common_returns({e: bars[e] for e in eids}, grid, eids,
                                              LOOKBACK, CONFIRMED_ACTIONS)
        if len(R) < RECENT:
            return {"error": f"only {len(R)} common sessions"}
        pnl = [sum(x[e] * r[i] for i, e in enumerate(eids)) for r in R]
        leg_r = {e: [r[i] * 100 for r in R] for i, e in enumerate(eids)}   # in %

        # factor returns on the same intervals: the anchor is the common
        # session before dates[0] (common_returns' first interval starts there)
        anchor = [d for d in grid if d < dates[0] and all(d in bars[e] for e in eids)][-1]
        fdates = [anchor] + dates
        fr, keep, dropped = factor_returns(conn, fdates)
        kept = set(keep)
        idx = [i for i, d in enumerate(dates) if d in kept]
        y = [pnl[i] for i in idx]
        X = [[fr[k][j] for k, *_ in FACTORS] for j in range(len(keep))]
        leg_y = {e: [leg_r[e][i] for i in idx] for e in eids}

        def fit(n):
            res = ols(y[-n:], X[-n:])
            return {"n": res["n"], "start": keep[-n], "end": keep[-1],
                    "r2": round(res["r2"], 3),
                    "sd_pnl": round(res["sd_y"], 0), "sd_resid": round(res["sd_res"], 0),
                    "vol_pnl_pct": round(res["sd_y"] * math.sqrt(250) / nav * 100, 2),
                    "vol_resid_pct": round(res["sd_res"] * math.sqrt(250) / nav * 100, 2),
                    "factors": [{"key": k, "label": lab,
                                 "usd_per_pct": round(res["coef"][i + 1], 0),
                                 "se": round(res["se"][i + 1], 0),
                                 "t": round(res["t"][i + 1], 2),
                                 "hedge_notional_usd": round(-res["coef"][i + 1] * 100, 0)}
                                for i, (k, lab, *_) in enumerate(FACTORS)]}
        joint = fit(len(y))
        recent = fit(RECENT)

        # univariate: per factor, book beta and each leg's beta; leg betas
        # dollar-weighted sum to the book's (linearity - selftest checks it)
        uni = []
        for k, lab, *_ in FACTORS:
            f = fr[k]
            b_book = beta1(y, f)
            legs = []
            for e in eids:
                b = beta1(leg_y[e], f)
                legs.append({"entity_id": e, "token": tok[e],
                             "beta": round(b, 3) if b is not None else None,
                             "usd_per_pct": round(x[e] * b / 100, 0) if b is not None else None})
            legs.sort(key=lambda r: -abs(r["usd_per_pct"] or 0))
            uni.append({"key": k, "label": lab,
                        "usd_per_pct": round(b_book, 0) if b_book is not None else None,
                        "sd_factor_pct": round(statistics.pstdev(f), 3),
                        "legs": legs})

        # sector nets, raw and basket-beta-adjusted
        sectors = []
        for sec in sorted(set(sector_of.values())):
            legs_in = [e for e in eids if sector_of[e] == sec]
            rows, net_b, withheld = [], 0.0, []
            for e in legs_in:
                own_b = None
                if sec in members:
                    # the basket EXCLUDING the leg, sector_rel's rule
                    bk_ex = basket_returns(bars, [m for m in members[sec] if m != e], fdates)
                    pairs = [(bk_ex[d], leg_r[e][i]) for i, d in enumerate(dates) if d in bk_ex]
                    if len(pairs) >= 60:
                        own_b = beta1([p[1] for p in pairs], [p[0] * 100 for p in pairs])
                if own_b is None:
                    withheld.append(tok[e])
                net_b += x[e] * (own_b if own_b is not None else 1.0)
                rows.append({"token": tok[e], "usd": round(x[e], 0),
                             "basket_beta": round(own_b, 2) if own_b is not None else None})
            L = sum(x[e] for e in legs_in if x[e] > 0)
            S = sum(x[e] for e in legs_in if x[e] < 0)
            sectors.append({"sector": sec, "n_basket": len(members.get(sec, [])),
                            "long_usd": round(L, 0), "short_usd": round(S, 0),
                            "net_usd": round(L + S, 0), "gross_usd": round(L - S, 0),
                            "net_beta_usd": round(net_b, 0),
                            "beta_withheld": withheld, "legs": rows})

        # momentum tilt
        mom = {e: momentum(bars, grid, e) for e in eids}
        companies = {r[0] for r in conn.execute("SELECT id FROM entities WHERE kind='company'")}
        uni_mom = [m for m in (momentum(bars, grid, e) for e in bars if e in companies)
                   if m is not None]

        def wavg(side):
            xs = [(abs(x[e]), mom[e]) for e in eids if mom[e] is not None and (x[e] > 0) == side]
            w = sum(a for a, _ in xs)
            return sum(a * m for a, m in xs) / w if w else None
        mom_long, mom_short = wavg(True), wavg(False)
        tilt = {"long_12_1_pct": round(mom_long * 100, 1) if mom_long is not None else None,
                "short_12_1_pct": round(mom_short * 100, 1) if mom_short is not None else None,
                "universe_median_pct": round(statistics.median(uni_mom) * 100, 1) if uni_mom else None,
                "universe_n": len(uni_mom),
                "withheld": [tok[e] for e in eids if mom[e] is None],
                "legs": sorted([{"token": tok[e], "usd": round(x[e], 0),
                                 "mom_12_1_pct": round(mom[e] * 100, 1)}
                                for e in eids if mom[e] is not None],
                               key=lambda r: -r["mom_12_1_pct"])}

        gross = sum(abs(v) for v in x.values())
        net = sum(x.values())
        ims_beta_net = sum(ex["legs"][e]["beta_usd"] for e in eids)
        return {"snapshot": snap, "nav": nav, "as_of": dates[-1],
                "gross_usd": round(gross, 0), "net_usd": round(net, 0),
                "gross_pct": round(gross / nav * 100, 2), "net_pct": round(net / nav * 100, 2),
                "ims_beta_net_usd": round(ims_beta_net, 0),
                "n_legs": len(eids),
                "excluded": {"options": ex["options"], "unmapped": ex["unmapped"]},
                "sessions": len(dates), "factor_sessions": len(keep), "dropped_dates": dropped,
                "zeroed": zeroed,
                "joint": joint, "recent": recent, "univariate": uni,
                "sectors": sectors, "momentum": tilt,
                "not_measured": ["value: no cross-sectional multiple history in the store"]}
    finally:
        if own:
            conn.close()


# ----------------------------------------------------------------- report --

def report(p: dict) -> None:
    if "error" in p:
        print("factors:", p["error"])
        return
    nav = p["nav"]

    def pc(v):
        return f"{v / nav * 100:+.2f}%"
    print(f"FACTOR CHECK  snapshot {p['snapshot']}  prices to {p['as_of']}  NAV ${nav:,.0f}")
    print(f"  gross ${p['gross_usd']:,.0f} ({p['gross_pct']}% NAV)   net ${p['net_usd']:,.0f} "
          f"({p['net_pct']}% NAV)   IMS beta-net ${p['ims_beta_net_usd']:,.0f}   legs {p['n_legs']}")
    if p["excluded"]["options"] or p["excluded"]["unmapped"]:
        print("  excluded:", ", ".join(o["root"] for o in p["excluded"]["options"] + p["excluded"]["unmapped"]))
    for key, title in (("joint", "JOINT REGRESSION"), ("recent", "RECENT")):
        j = p[key]
        print(f"\n{title}  {j['n']} sessions {j['start']} .. {j['end']}   R2 {j['r2']}")
        print(f"  book daily sd ${j['sd_pnl']:,.0f} -> residual ${j['sd_resid']:,.0f}   "
              f"annualised {j['vol_pnl_pct']}% -> {j['vol_resid_pct']}% of NAV")
        for f in j["factors"]:
            print(f"  {f['label']:22s} ${f['usd_per_pct']:>8,.0f} per 1%   t {f['t']:+5.2f}   "
                  f"hedge ${f['hedge_notional_usd']:>+10,.0f} ({pc(f['hedge_notional_usd'])} NAV)")
    print("\nUNIVARIATE - who carries it (USD per 1% of the factor; legs sum to the book)")
    for u in p["univariate"]:
        top = ", ".join(f"{l['token']} {l['usd_per_pct']:+,.0f}" for l in u["legs"][:5]
                        if l["usd_per_pct"] is not None)
        print(f"  {u['label']:22s} book ${u['usd_per_pct']:>+8,.0f}  (factor sd {u['sd_factor_pct']}%/day)   {top}")
    print("\nSECTOR NETS  (net_beta = dollars x each leg's beta to its own F&O basket)")
    for s in p["sectors"]:
        w = f"   beta=1 for {', '.join(s['beta_withheld'])}" if s["beta_withheld"] else ""
        print(f"  {s['sector']:12s} long ${s['long_usd']:>9,.0f}  short ${s['short_usd']:>10,.0f}  "
              f"net ${s['net_usd']:>+9,.0f} ({pc(s['net_usd'])})  net_beta ${s['net_beta_usd']:>+9,.0f}"
              f"  basket n={s['n_basket']}{w}")
    m = p["momentum"]
    print(f"\nMOMENTUM TILT  12-1 month return, dollar-weighted: longs {m['long_12_1_pct']:+.1f}%  "
          f"shorts {m['short_12_1_pct']:+.1f}%  universe median {m['universe_median_pct']:+.1f}% (n={m['universe_n']})")
    if m["withheld"]:
        print("  withheld (under 253 sessions):", ", ".join(m["withheld"]))
    print("  top:", ", ".join(f"{l['token']} {l['mom_12_1_pct']:+.0f}" for l in m["legs"][:4]),
          "  bottom:", ", ".join(f"{l['token']} {l['mom_12_1_pct']:+.0f}" for l in m["legs"][-4:]))
    print("\nNOT MEASURED:", "; ".join(p["not_measured"]))
    if p["dropped_dates"]:
        print(f"  {len(p['dropped_dates'])} intervals dropped (factor close missing):",
              ", ".join(p["dropped_dates"][:5]))


# --------------------------------------------------------------- selftest --

def _selftest() -> None:
    import random
    rnd = random.Random(11)
    fails = 0

    def check(cond, msg):
        nonlocal fails
        print(("  ok   " if cond else "  FAIL ") + msg)
        fails += 0 if cond else 1

    # 1. OLS recovers known loadings on a synthetic book
    n = 400
    f1 = [rnd.gauss(0, 1.0) for _ in range(n)]
    f2 = [rnd.gauss(0, 0.8) for _ in range(n)]
    y = [3.0 + 500 * a - 200 * b + rnd.gauss(0, 50) for a, b in zip(f1, f2)]
    r = ols(y, [[a, b] for a, b in zip(f1, f2)])
    check(abs(r["coef"][1] - 500) < 15 and abs(r["coef"][2] + 200) < 15,
          f"ols recovers 500 / -200 ({r['coef'][1]:.1f} / {r['coef'][2]:.1f})")
    check(r["r2"] > 0.95 and r["sd_res"] < r["sd_y"], "ols r2 and residual sd")
    check(abs(r["t"][1]) > 20, "ols t-stat on a real loading")
    # 2. a zero-loading factor reads ~0 with |t| small
    f3 = [rnd.gauss(0, 1.0) for _ in range(n)]
    r3 = ols(y, [[a, b, c] for a, b, c in zip(f1, f2, f3)])
    check(abs(r3["t"][3]) < 2.5, f"zero loading reads ~0 (t {r3['t'][3]:+.2f})")
    # 3. singular design refuses
    try:
        ols(y, [[a, a] for a in f1])
        check(False, "singular design refused")
    except ValueError:
        check(True, "singular design refused")
    # 4. linearity: dollar-weighted leg betas sum to the book beta exactly
    legs = {e: [rnd.gauss(0, 1) + k * 0.3 * a for a in f1] for k, e in enumerate("abc")}
    xs = {"a": 1000.0, "b": -700.0, "c": 300.0}
    book = [sum(xs[e] * legs[e][t] / 100 for e in xs) for t in range(n)]
    b_book = beta1(book, f1)
    b_sum = sum(xs[e] * beta1(legs[e], f1) / 100 for e in xs)
    check(abs(b_book - b_sum) < 1e-9, "leg betas x dollars sum to the book beta")
    # 5. beta1 on a constant factor withholds
    check(beta1(book, [1.0] * n) is None, "beta on a flat factor withholds")
    # 6. momentum: 12-1 skips the last 21 sessions
    grid = [f"d{i:04d}" for i in range(300)]
    bars = {"z": {d: (0, 0, 0, 100.0 * (1.01 ** i)) for i, d in enumerate(grid)}}
    m = momentum(bars, grid, "z")
    exp = (1.01 ** (299 - 21)) / (1.01 ** (299 - 252)) - 1
    check(abs(m - exp) < 1e-9, "momentum is close[t-21]/close[t-252]-1")
    check(momentum(bars, grid[:200], "z") is None, "momentum withheld under 253 sessions")
    # 7. basket quorum: a day under half the members is absent
    d = ["a", "b", "c"]
    bars = {"p": {"a": (0, 0, 0, 10.0), "b": (0, 0, 0, 11.0), "c": (0, 0, 0, 11.0)},
            "q": {"a": (0, 0, 0, 10.0), "b": (0, 0, 0, 9.0)},
            "r": {"a": (0, 0, 0, 10.0)}}
    bk = basket_returns(bars, ["p", "q", "r"], d)
    check(abs(bk["b"] - 0.0) < 1e-12 and "c" not in bk, "basket: 2 of 3 counts, 1 of 3 does not")
    # 8. factor_returns drops an interval every factor cannot price, together
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE flow_series (series_id TEXT, date TEXT, close REAL)")
    days = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    for key, _, sid, _ in FACTORS:
        for i, dd in enumerate(days):
            if sid == "nifty_midcap150" and dd == "2026-01-03":
                continue
            conn.execute("INSERT INTO flow_series VALUES (?,?,?)",
                         (sid, dd, 100.0 + i * (2 if sid == "nifty" else 1)))
    fr, keep, dropped = factor_returns(conn, days)
    check(keep == ["2026-01-02"] and dropped == ["2026-01-03", "2026-01-04"],
          "a missing factor close drops the interval for every factor")
    check(abs(fr["it"][0] - (1.0 - 2.0)) < 1e-9, "sector factor is net of nifty")
    check(len({len(v) for v in fr.values()}) == 1, "design matrix stays rectangular")
    print(f"\n{'ALL OK' if not fails else f'{fails} FAILED'}")
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--as-of")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        p = build(as_of=a.as_of)
        if a.json:
            import json
            print(json.dumps(p, indent=1, default=str))
        else:
            report(p)
