"""Value at Risk for the PM's book — the Book tab's Risk sub-tab (2026-10-25).

PM: "Can we calculate the VaR for our book?", then "build VaR into the Book
tab as a tested module, recomputed from each snapshot".

WHAT IS MEASURED. Today's book, held constant in DOLLARS, run through the
legs' own past daily returns. Exposure per leg = the IMS MV% x NAV of the
snapshot, summed over contracts of one entity. That MV is the true USD
exposure of a futures leg: on the five snapshots up to 2026-10-21 the IMS day
P&L divided by (MV x the stored close-to-close move) has a MEDIAN OF 0.96
across 116 legs (the spread is intraday trading and close timing). No lot
sizes or multipliers exist here and none are needed — the chain-basis rule.

Every number is computed from ONE series, the book's daily P&L
pnl_t = sum_i x_i r_it, which is why nothing here builds a covariance matrix:
for a fixed exposure x, x' S x IS the variance of that series, and the EWMA
(RiskMetrics) variance is exactly sum_t w_t pnl_t^2. Component VaR needs one
more pass per leg, cov(r_i, pnl). Pure stdlib, like kelly.py.

Methods (parameters in specs/book.yaml `var:`):
- HISTORICAL over the last 250 and 500 common sessions: VaR = the loss at the
  (1-c) empirical quantile, numpy's 'linear' convention; ES = the mean loss
  on the days at or beyond it.
- EWMA, lambda 0.94 over 500 sessions, normal: the HEADLINE, because it is
  the only method that weights the current regime. It is also the most
  conservative of the three on today's book ($4,851 at 99% against $3,534 and
  $4,010 historical, 2026-10-23). First ex-ante backtest, 27 usable snapshot
  pairs to 2026-10-21: no 99% break on either method; EWMA 95% broke twice
  (15-Sep -$3,561, 06-Oct -$3,345) against 1.35 expected. NOTE: 15-Sep DOES
  break TODAY's 1y historical 99% ($3,534) — measured with hindsight that is
  a break, ex-ante ($4,163 on 11-Sep) it is not. Only the ex-ante one counts.
- 10 DAYS two ways, and they disagree on purpose: overlapping 10-session sums
  of the 500-session P&L, against sqrt(10) x the 1-day EWMA. The spreads
  partly mean-revert (kelly.py measured a pooled 13w variance ratio of 0.646),
  so sqrt-time overstates; both are printed so the gap is visible.
- STRESS: today's book replayed through named episodes. Legs not yet listed
  contribute zero and the coverage is printed beside every row — the GFC row
  has 20 of 32 legs priced and UNDERSTATES.
- BACKTEST: for each pair of consecutive stored snapshots (a, b), the VaR
  computed from a's positions and prices up to a, against the IMS day P&L
  summed over b's positions. Ex-ante by construction. Two looseness points,
  both stated in the payload: the IMS DTD includes intraday trading and the
  option hedges (which the forecast excludes), and when a and b are more than
  one session apart DTD covers only b's last day.

Session hygiene is basket_index's, imported not copied: phantom sessions (a
shut exchange answered by Yahoo with o=h=l=c, volume 0) and non-quorum dates
are out, and a session counts only when EVERY leg printed (all-or-nothing),
the chain running on across a hole. A CONFIRMED corporate action
(core/corporate_actions) is a zero return for that leg that day — VEDL's
2026-04-30 demerger would otherwise enter as a -65% day, i.e. a +65% GAIN on
today's short. VEDL before that date is the pre-demerger conglomerate; it is
flagged `pre_demerger_history`, the kelly.py convention, not excluded.

Excluded and NAMED, never netted: option roots (`book_io.is_option_root`;
priced by nothing in `prices`) and tickers with no ticker_map entry. FX is not
modelled: futures carry FX only on their P&L, and the three cash lines
(Dalmia, Syrma, LTTS — ~1.2% of NAV on 2026-10-21) are immaterial at this
book's size; the payload says so rather than implying a full FX model.

Nothing here writes. No var table, for basket_index's reason: it recomputes
from `prices` and `book_positions` in about a second, and a stored copy would
be a second thing that can be stale.
"""
from __future__ import annotations

import datetime as dt
import math
import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))
sys.path.insert(0, str(REPO / "packages" / "book"))

SPEC = REPO / "specs" / "book.yaml"

DEFAULTS = {
    "confidence": [0.95, 0.99, 0.999],
    "windows": [250, 500],
    "ewma_lambda": 0.94,
    "ewma_sessions": 500,
    "horizons": [{"label": "1 week", "days": 5}, {"label": "10 days", "days": 10},
                 {"label": "1 month", "days": 21}],
    "horizon_lookback": 1250,
    "horizon_headline": "ewma_vr",
    "headline": {"method": "ewma", "confidence": 0.99},
    "jump_flag": 0.25,
    "min_sessions": 60,
    "stress": [],
}


def _spec() -> tuple[dict, dict, dict]:
    import yaml
    cfg = yaml.safe_load(SPEC.read_text(encoding="utf-8")) or {}
    return ({str(k): str(v) for k, v in (cfg.get("ticker_map") or {}).items()},
            {str(k): str(v) for k, v in (cfg.get("names") or {}).items()},
            {**DEFAULTS, **(cfg.get("var") or {})})


# ------------------------------------------------------------- statistics --

def quantile(xs: list[float], p: float) -> float:
    """numpy.quantile(xs, p) with the default 'linear' method."""
    s = sorted(xs)
    if not s:
        raise ValueError("quantile of nothing")
    h = (len(s) - 1) * p
    lo = math.floor(h)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (h - lo) * (s[hi] - s[lo])


def hist_var(pnl: list[float], c: float) -> tuple[float, float]:
    """(VaR, ES) as positive losses at confidence c."""
    v = -quantile(pnl, 1 - c)
    tail = [x for x in pnl if x <= -v]
    return v, -sum(tail) / len(tail)


def ewma_weights(n: int, lam: float) -> list[float]:
    """Oldest first; the newest observation carries the largest weight."""
    w = [lam ** (n - 1 - t) for t in range(n)]
    s = sum(w)
    return [x / s for x in w]


def ewma_sd(pnl: list[float], lam: float) -> float:
    w = ewma_weights(len(pnl), lam)
    return math.sqrt(sum(wi * x * x for wi, x in zip(w, pnl)))


def thin(n: int, c: float) -> bool:
    """Fewer than one observation expected beyond the level: a historical
    'quantile' there is just the worst day (or window) in the sample."""
    return n * (1 - c) < 1


def filtered_residuals(pnl: list[float], lam: float, warm: int = 60) -> list[float]:
    """pnl_t / sigma_{t-1}, sigma the running EWMA known BEFORE day t."""
    if len(pnl) <= warm:
        return []
    v = sum(p * p for p in pnl[:warm]) / warm
    out = []
    for p in pnl[warm:]:
        if v > 0:
            out.append(p / math.sqrt(v))
        v = lam * v + (1 - lam) * p * p
    return out


def z_of(c: float) -> float:
    return statistics.NormalDist().inv_cdf(c)


def normal_es(sd: float, c: float) -> float:
    z = z_of(c)
    return sd * math.exp(-z * z / 2) / math.sqrt(2 * math.pi) / (1 - c)


def binom_tail(n: int, k: int, p: float) -> float:
    """P(X >= k), X ~ Bin(n, p): how surprising k breaks of a VaR are."""
    return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))


# ------------------------------------------------------------------- data --

def exposures(conn, snap: str, tmap: dict) -> dict:
    """Signed USD per entity at one snapshot, with what was left out."""
    from book_io import is_option_root
    nav = conn.execute("SELECT nav FROM book_snapshots WHERE snap_date=?",
                       (snap,)).fetchone()[0]
    legs: dict = {}
    options, unmapped = [], []
    for root, side, mv, bmv, contract in conn.execute(
            "SELECT root, side, mv_pct, beta_mv_pct, contract FROM book_positions "
            "WHERE snap_date=? ORDER BY root", (snap,)):
        if is_option_root(root):
            options.append({"root": root, "side": side,
                            "mv_usd": round((mv or 0) * nav, 0)})
            continue
        tok = (root.split() or [""])[0]
        eid = tmap.get(tok) or tmap.get(root)
        if not eid:
            unmapped.append({"root": root, "side": side,
                             "mv_usd": round((mv or 0) * nav, 0)})
            continue
        g = legs.setdefault(eid, {"usd": 0.0, "beta_usd": 0.0, "cash_usd": 0.0,
                                  "tokens": []})
        g["usd"] += (mv or 0) * nav
        g["beta_usd"] += (bmv or 0) * nav
        if not contract:            # a cash line: the only legs with FX on notional
            g["cash_usd"] += (mv or 0) * nav
        if tok not in g["tokens"]:
            g["tokens"].append(tok)
    return {"nav": nav, "legs": legs, "options": options, "unmapped": unmapped}


def load_bars(conn, eids, upto: str | None):
    bars, vols = {}, {}
    for e in eids:
        bars[e], vols[e] = {}, {}
        for d, o, h, lo, c, v in conn.execute(
                "SELECT date, open, high, low, close, volume FROM prices "
                "WHERE entity_id=? AND close IS NOT NULL AND date<=? ORDER BY date",
                (e, upto or "9999-12-31")):
            bars[e][d] = (o, h, lo, c)
            vols[e][d] = v
    return bars, vols


def session_grid(bars, vols) -> list[str]:
    """Real sessions over the union of legs — basket_index's two filters."""
    import basket_index
    first = {e: min(b) for e, b in bars.items() if b}
    live, _, _ = basket_index.sessions(bars, vols, first, 0.5)
    return live


def common_returns(bars, grid, eids, n_max: int, actions: dict):
    """The last n_max returns between sessions where EVERY leg printed.

    Returns (dates, R, zeroed) — R[t][i] is leg i's simple return into
    dates[t]. A confirmed action day is a zero for that leg, listed.
    """
    common = []
    for d in reversed(grid):
        if all(d in bars[e] for e in eids):
            common.append(d)
            if len(common) > n_max:
                break
    common.reverse()
    dates, R, zeroed = [], [], []
    for a, b in zip(common, common[1:]):
        row = []
        for e in eids:
            if b in (actions.get(e) or {}):
                row.append(0.0)
                zeroed.append({"entity_id": e, "date": b,
                               "reason": actions[e][b]})
            else:
                row.append(bars[e][b][3] / bars[e][a][3] - 1)
        dates.append(b)
        R.append(row)
    return dates, R, zeroed


# --------------------------------------------------------------- measures --

def measure(x: list[float], R: list[list[float]], dates: list[str], cfg: dict) -> dict:
    """Every method on one exposure vector. Pure arithmetic — the selftest's
    target."""
    pnl = [sum(xi * ri for xi, ri in zip(x, r)) for r in R]
    confs = [float(c) for c in cfg["confidence"]]
    out = {"methods": [], "n_returns": len(pnl)}
    for n in cfg["windows"]:
        n = int(n)
        p = pnl[-n:]
        if len(p) < n:
            continue
        m = {"key": f"hist{n}", "label": f"historical, last {n} sessions",
             "n": len(p), "start": dates[-len(p)], "end": dates[-1],
             "var": {}, "es": {}, "thin": {}}
        for c in confs:
            v, es = hist_var(p, c)
            m["var"][f"{c:g}"], m["es"][f"{c:g}"] = round(v, 0), round(es, 0)
            m["thin"][f"{c:g}"] = thin(len(p), c)
        out["methods"].append(m)
    ne = int(cfg["ewma_sessions"])
    lam = float(cfg["ewma_lambda"])
    pe = pnl[-ne:]
    sd = ewma_sd(pe, lam)
    m = {"key": "ewma", "label": f"EWMA λ {lam:g}, normal", "n": len(pe),
         "start": dates[-len(pe)], "end": dates[-1], "sd": round(sd, 0),
         "var": {}, "es": {}}
    for c in confs:
        m["var"][f"{c:g}"] = round(z_of(c) * sd, 0)
        m["es"][f"{c:g}"] = round(normal_es(sd, c), 0)
    out["methods"].append(m)
    out["ewma_sd"] = sd
    # The normal assumes thin tails, which matters most at 99.9%. The book's
    # own P&L, each day divided by the EWMA sd known the day before (filtered
    # residuals, so a calm year and a wild one are on one scale), says how fat
    # they really are: excess kurtosis, and the share of days beyond the
    # normal's 99.9% line (the normal expects 0.1%).
    res = filtered_residuals(pnl, lam)
    if len(res) >= 250:
        # FILTERED HISTORICAL: today's EWMA sd x the book's own standardised
        # tail. Keeps the fat tails the normal drops (on 2026-10-23, 0.33% of
        # 907 days fell beyond the normal's 99.9% line against 0.1%), on
        # today's vol rather than an average year's. Uses every common
        # session, so its 99.9% is less thin than a 1y window's — still
        # flagged when under one observation lies beyond it.
        m = {"key": "fhs", "label": "filtered historical (EWMA × own tail)",
             "n": len(res), "start": dates[-len(res)], "end": dates[-1],
             "var": {}, "es": {}, "thin": {}}
        for c in confs:
            v, es = hist_var(res, c)
            m["var"][f"{c:g}"], m["es"][f"{c:g}"] = round(v * sd, 0), round(es * sd, 0)
            m["thin"][f"{c:g}"] = thin(len(res), c)
        out["methods"].append(m)
    if len(res) >= 100:
        mu = sum(res) / len(res)
        var_ = sum((r - mu) ** 2 for r in res) / len(res)
        k = sum((r - mu) ** 4 for r in res) / len(res) / var_ ** 2 - 3
        z999 = z_of(0.999)
        out["tails"] = {"n": len(res), "excess_kurtosis": round(k, 2),
                        "beyond_normal_999_pct": round(
                            sum(1 for r in res if r < -z999) / len(res) * 100, 2)}

    # Multi-day horizons. Three estimates each, because each fails differently:
    #   hist     overlapping h-session sums over ALL common history — the real
    #            path, but n/h independent windows only (a month: ~46 on today's
    #            966 sessions), so its 99.9% is the worst month, flagged thin
    #   sqrt     the 1-day EWMA x sqrt(h): today's vol, iid scaling — overstates
    #            when the spreads mean-revert, as they do (kelly.py's 0.646)
    #   ewma_vr  the 1-day EWMA x sqrt(h x VR(h)), VR measured on the same long
    #            history: today's vol with the book's own horizon scaling
    pl = pnl[-int(cfg.get("horizon_lookback", len(pnl))):]
    var1 = sum(p * p for p in pl) / len(pl)
    out["horizons"] = []
    for hz in cfg["horizons"]:
        h = int(hz["days"])
        ph = [sum(pl[i:i + h]) for i in range(len(pl) - h + 1)]
        if len(pl) // h < 10:
            continue
        vr = (sum(p * p for p in ph) / len(ph)) / (h * var1)
        indep = len(pl) // h
        out["horizons"].append({
            "label": hz["label"], "days": h, "n_windows": len(ph), "n_independent": indep,
            "start": dates[-len(pl)], "end": dates[-1], "vr": round(vr, 3),
            "hist": {f"{c:g}": round(-quantile(ph, 1 - c), 0) for c in confs},
            "hist_thin": {f"{c:g}": thin(indep, c) for c in confs},
            "sqrt": {f"{c:g}": round(z_of(c) * sd * math.sqrt(h), 0) for c in confs},
            "ewma_vr": {f"{c:g}": round(z_of(c) * sd * math.sqrt(h * vr), 0) for c in confs},
        })
    worst = sorted(range(len(pe)), key=lambda i: pe[i])[:5]
    off = len(pnl) - len(pe)
    out["worst_days"] = [{"date": dates[off + i], "pnl": round(pe[i], 0)}
                         for i in worst]
    out["pnl"] = pnl
    return out


def components(x: list[float], R: list[list[float]], lam: float, n: int,
               c: float) -> tuple[list[float], float]:
    """Euler contributions to the EWMA VaR at c; they sum to it exactly."""
    Rw = R[-n:]
    w = ewma_weights(len(Rw), lam)
    pnl = [sum(xi * ri for xi, ri in zip(x, r)) for r in Rw]
    sd = math.sqrt(sum(wt * p * p for wt, p in zip(w, pnl)))
    z = z_of(c)
    comp = []
    for i, xi in enumerate(x):
        cov = sum(wt * r[i] * p for wt, r, p in zip(w, Rw, pnl))
        comp.append(xi * cov / sd * z if sd else 0.0)
    return comp, z * sd


def stress(x: dict, bars, grid, actions, episodes) -> list[dict]:
    """Today's dollars through named episodes; a leg not yet listed is zero."""
    out = []
    for ep in episodes:
        a, b = str(ep["start"]), str(ep["end"])
        ds = [d for d in grid if a <= d <= b]
        priced = [e for e in x if bars[e] and min(bars[e]) <= a]
        cum = peak = 0.0
        mdd, worst = 0.0, (None, 0.0)
        for d0, d1 in zip(ds, ds[1:]):
            r = 0.0
            for e, usd in x.items():
                if d0 in bars[e] and d1 in bars[e] and d1 not in (actions.get(e) or {}):
                    r += usd * (bars[e][d1][3] / bars[e][d0][3] - 1)
            if r < worst[1]:
                worst = (d1, r)
            cum += r
            peak = max(peak, cum)
            mdd = min(mdd, cum - peak)
        out.append({"label": ep["label"], "start": a, "end": b,
                    "sessions": max(len(ds) - 1, 0),
                    "legs_priced": len(priced), "legs_total": len(x),
                    "worst_day": {"date": worst[0], "pnl": round(worst[1], 0)},
                    "peak_to_trough": round(mdd, 0), "cum": round(cum, 0)})
    return out


# ------------------------------------------------------------------ build --

def _at(conn, snap, upto, tmap, cfg, bars_all, vols_all, grid_all, actions,
        long_history=False):
    """VaR of one snapshot's book with prices up to `upto` (None = all)."""
    ex = exposures(conn, snap, tmap)
    eids = sorted(ex["legs"])
    # bars are NOT clipped: common_returns only walks grid dates, and the
    # grid is clipped, so no date after `upto` can be read
    bars = {e: bars_all[e] for e in eids}
    grid = [d for d in grid_all if not upto or d <= upto]
    n_max = max([int(n) for n in cfg["windows"]] + [int(cfg["ewma_sessions"])]
                + ([int(cfg["horizon_lookback"])] if long_history else []))
    dates, R, zeroed = common_returns(bars, grid, eids, n_max, actions)
    x = [ex["legs"][e]["usd"] for e in eids]
    return ex, eids, bars, grid, dates, R, zeroed, x


def build(conn: sqlite3.Connection | None = None, as_of: str | None = None,
          view: list[dict] | None = None, live: bool = True) -> dict:
    """The Risk sub-tab payload for the snapshot on or before `as_of`.

    `live` = prices run to the latest close (the Book's live view, the
    sector_rel convention); otherwise they stop at the snapshot date, so a
    replay shows the VaR that could have been computed that day. `view` is
    engine.book_view's dictated-pair list, used only to attribute.
    """
    from corporate_actions import CONFIRMED_ACTIONS
    own = conn is None
    if own:
        import book_io
        conn = book_io.connect()
    try:
        tmap, names, cfg = _spec()
        snaps = [r[0] for r in conn.execute(
            "SELECT snap_date FROM book_snapshots WHERE snap_date<=? ORDER BY snap_date",
            (as_of or "9999-12-31",))]
        if not snaps:
            return {"error": "no snapshot on or before " + str(as_of)}
        snap = snaps[-1]
        upto = None if live else snap
        # every entity any snapshot held, so the backtest reuses one load
        union = set()
        for s in snaps:
            union |= set(exposures(conn, s, tmap)["legs"])
        bars_all, vols_all = load_bars(conn, sorted(union), upto)
        grid_all = session_grid(bars_all, vols_all)
        actions = CONFIRMED_ACTIONS

        ex, eids, bars, grid, dates, R, zeroed, x = _at(
            conn, snap, upto, tmap, cfg, bars_all, vols_all, grid_all, actions,
            long_history=True)
        if len(R) < int(cfg["min_sessions"]):
            return {"error": f"only {len(R)} common sessions for {snap}'s legs"}
        nav = ex["nav"]
        m = measure(x, R, dates, cfg)
        lam, ne = float(cfg["ewma_lambda"]), int(cfg["ewma_sessions"])
        hc = float(cfg["headline"]["confidence"])
        comp, total = components(x, R, lam, ne, hc)
        tok_of = {e: ex["legs"][e]["tokens"][0] for e in eids}

        def standalone(vec):
            p = [sum(v * r for v, r in zip(vec, row)) for row in R[-ne:]]
            return z_of(hc) * ewma_sd(p, lam)

        legs = []
        for i, e in enumerate(eids):
            vec = [x[j] if j == i else 0.0 for j in range(len(eids))]
            legs.append({"entity_id": e, "token": tok_of[e],
                         "name": names.get(tok_of[e], tok_of[e]),
                         "usd": round(x[i], 0), "contrib": round(comp[i], 0),
                         "standalone": round(standalone(vec), 0)})

        # ---- attribution to the PM's dictated pairs, by the view's shares
        # A SHARE OF 0 IS A SHARE. NALCO sits in HNDL_NACL and VAML_NACL;
        # with VAML off the book its share in VAML_NACL is 0.0, and the first
        # draft's `share or 1.0` turned that into a WHOLE NALCO — the pair
        # contributions summed to $4,929 against a $4,851 book (2026-10-25,
        # caught by summing them). engine rounds shares to 3dp, so an
        # entity's shares are renormalised when they already sum to ~1.
        def _w(lg):
            sh = lg.get("share")
            return 1.0 if sh is None else float(sh)
        tot_share: dict = {}
        for p in view or []:
            for lg in p.get("legs") or []:
                if lg.get("entity_id") in ex["legs"]:
                    tot_share[lg["entity_id"]] = tot_share.get(lg["entity_id"], 0.0) + _w(lg)
        pairs, used = [], {e: 0.0 for e in eids}
        for p in view or []:
            vec = [0.0] * len(eids)
            members = []
            for lg in p.get("legs") or []:
                e = lg.get("entity_id")
                if e not in ex["legs"]:
                    continue
                w = _w(lg)
                if abs(tot_share[e] - 1) < 0.01:
                    w /= tot_share[e]
                if w <= 0:
                    continue
                i = eids.index(e)
                vec[i] += w * x[i]
                used[e] += w
                members.append({"token": lg.get("token"), "name": lg.get("name"),
                                "side": "L" if x[i] > 0 else "S",
                                "side_mismatch": bool(lg.get("side_mismatch")),
                                "usd": round(w * x[i], 0)})
            if not members:
                continue
            ctb = sum(vec[i] / x[i] * comp[i] for i in range(len(eids)) if x[i])
            pairs.append({"label": p.get("label"), "sector": p.get("sector"),
                          "gross_usd": round(sum(abs(v) for v in vec), 0),
                          "contrib": round(ctb, 0),
                          "standalone": round(standalone(vec), 0),
                          "legs": members})
        rest = [i for i, e in enumerate(eids) if used[e] < 0.999]
        if view is not None and rest:
            vec = [(1 - used[eids[i]]) * x[i] if i in rest else 0.0
                   for i in range(len(eids))]
            pairs.append({"label": "not in a dictated pair", "sector": None,
                          "unassigned": True,
                          "gross_usd": round(sum(abs(v) for v in vec), 0),
                          "contrib": round(sum((1 - used[eids[i]]) * comp[i] for i in rest), 0),
                          "standalone": round(standalone(vec), 0),
                          "legs": [{"token": tok_of[eids[i]],
                                    "name": names.get(tok_of[eids[i]], tok_of[eids[i]]),
                                    "side": "L" if x[i] > 0 else "S",
                                    "usd": round(vec[i], 0)} for i in rest]})
        pairs.sort(key=lambda r: -r["contrib"])

        # ---- flags
        jf = float(cfg["jump_flag"])
        jumps = [{"entity_id": eids[i], "date": d, "ret_pct": round(r[i] * 100, 1)}
                 for d, r in zip(dates, R) for i in range(len(eids))
                 if abs(r[i]) > jf]
        pre_dm = [e for e, acts in actions.items() if e in ex["legs"]
                  and any(dates[0] < a <= dates[-1] for a in acts)]

        # ---- backtest: snapshot a's VaR against b's IMS day P&L
        rows, bt_skipped = [], []
        for a, b in zip(snaps, snaps[1:]):
            exa, eida, _, _, da, Ra, _, xa = _at(
                conn, a, a, tmap, cfg, bars_all, vols_all, grid_all, actions)
            # A leg that listed recently (VAML, 2026-06-15) shortens every
            # leg's common history. Under min_sessions the snapshot is skipped
            # and counted; above it EWMA runs on what exists and the 1y
            # historical is withheld (None), never computed on fewer days.
            if len(Ra) < int(cfg["min_sessions"]):
                bt_skipped.append({"from": a, "to": b, "why": "short history",
                                   "history": len(Ra)})
                continue
            gap = sum(1 for d in grid_all if a < d <= b)
            if gap == 0:
                # a paste on an exchange holiday (02-Oct Gandhi Jayanti, 20-Oct
                # Dussehra): DTD is ~0 by construction, a free non-break
                bt_skipped.append({"from": a, "to": b, "why": "no session between"})
                continue
            pa = [sum(v * r for v, r in zip(xa, row)) for row in Ra]
            sd = ewma_sd(pa[-ne:], lam)
            nh = int(cfg["windows"][0])
            h1 = pa[-nh:] if len(pa) >= nh else None
            real = conn.execute("SELECT SUM(pnl_dtd) FROM book_positions WHERE snap_date=?",
                                (b,)).fetchone()[0] or 0.0
            row = {"from": a, "to": b, "sessions": gap, "realised": round(real, 0),
                   "history": len(Ra)}
            for c in [float(c) for c in cfg["confidence"]]:
                k = f"{c:g}"
                row["ewma_" + k] = round(z_of(c) * sd, 0)
                row["hist_" + k] = round(hist_var(h1, c)[0], 0) if h1 else None
            rows.append(row)
        bt = {"rows": rows, "n": len(rows), "skipped": bt_skipped, "summary": {}}
        for meth in ("ewma", "hist"):
            for c in [float(c) for c in cfg["confidence"]]:
                k = f"{c:g}"
                rs = [r for r in rows if r[f"{meth}_{k}"] is not None]
                br = [r["to"] for r in rs if r["realised"] < -r[f"{meth}_{k}"]]
                bt["summary"][f"{meth}_{k}"] = {
                    "n": len(rs), "breaks": len(br), "dates": br,
                    "expected": round(len(rs) * (1 - c), 2),
                    "p_at_least": round(binom_tail(len(rs), len(br), 1 - c), 3)}

        head = next(mm for mm in m["methods"] if mm["key"] == cfg["headline"]["method"])
        hk = f"{hc:g}"
        return {
            "snap": snap, "upto": dates[-1], "live": live, "nav": nav,
            "n_legs": len(eids),
            "gross_usd": round(sum(abs(v) for v in x), 0),
            "net_usd": round(sum(x), 0),
            "beta_net_usd": round(sum(ex["legs"][e]["beta_usd"] for e in eids), 0),
            "headline": {"method": head["key"], "label": head["label"],
                         "confidence": hc, "var": head["var"][hk],
                         "es": head["es"][hk], "check_sum": round(total, 0)},
            "methods": m["methods"], "horizons": m["horizons"],
            "tails": m.get("tails"),
            "horizon_headline": cfg["horizon_headline"],
            "worst_days": m["worst_days"],
            "legs": sorted(legs, key=lambda r: -r["contrib"]),
            "pairs": pairs,
            "diversification": {"sum_standalone": round(sum(l["standalone"] for l in legs), 0),
                                "book": head["var"][hk]},
            "stress": stress({e: ex["legs"][e]["usd"] for e in eids}, bars_all,
                             grid_all, actions, cfg.get("stress") or []),
            "backtest": bt,
            "excluded": {"options": ex["options"], "unmapped": ex["unmapped"]},
            "flags": {"zeroed": zeroed, "jumps": jumps,
                      "pre_demerger_history": pre_dm},
            "cash_legs_usd": round(sum(ex["legs"][e]["cash_usd"] for e in eids), 0),
            "params": {k: cfg[k] for k in ("confidence", "windows", "ewma_lambda",
                                           "ewma_sessions", "horizon_lookback",
                                           "jump_flag", "min_sessions")},
        }
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------- selftest --

def _selftest() -> None:
    import random
    fails = 0

    def check(cond, msg):
        nonlocal fails
        print(("  ok   " if cond else "  FAIL ") + msg)
        fails += 0 if cond else 1

    # quantile convention == numpy 'linear' (values checked by hand)
    xs = [5, 1, 4, 2, 3]
    check(quantile(xs, 0.0) == 1 and quantile(xs, 1.0) == 5, "quantile endpoints")
    check(abs(quantile(xs, 0.1) - 1.4) < 1e-12, "quantile interpolates (0.1 -> 1.4)")
    check(abs(quantile(list(range(1, 101)), 0.01) - 1.99) < 1e-12,
          "quantile 1% of 1..100 is 1.99, numpy's value")

    # historical VaR / ES on a known tail
    pnl = [0.0] * 95 + [-1, -2, -3, -4, -5]
    v, es = hist_var(pnl, 0.95)
    check(abs(v - 0.05) < 1e-12, f"hist VaR95 at the interpolated 5th pct ({v:.3f})")
    v, es = hist_var(pnl, 0.99)
    check(abs(v - 4.01) < 1e-9 and abs(es - 5.0) < 1e-12, "hist VaR99 4.01, ES99 = the worst day")
    # ACCEPTANCE beside rejection: ES is never below VaR
    rng = random.Random(7)
    g = [rng.gauss(0, 1) for _ in range(2000)]
    for c in (0.95, 0.99):
        v, es = hist_var(g, c)
        check(es >= v, f"ES >= VaR at {c}")

    # EWMA: lam -> 1 is the plain zero-mean RMS; weights sum to 1, newest heaviest
    w = ewma_weights(50, 0.94)
    check(abs(sum(w) - 1) < 1e-12 and w[-1] > w[0], "EWMA weights normalised, newest heaviest")
    rms = math.sqrt(sum(x * x for x in g) / len(g))
    check(abs(ewma_sd(g, 1.0) - rms) < 1e-12, "EWMA at lambda 1 is the RMS")
    # the portfolio-series EWMA equals x' S_ewma x built the long way
    R = [[rng.gauss(0, 0.02), rng.gauss(0, 0.015), rng.gauss(0, 0.01)] for _ in range(300)]
    for r in R:                       # induce correlation
        r[1] += 0.5 * r[0]
    x = [1000.0, -800.0, 300.0]
    wv = ewma_weights(len(R), 0.94)
    S = [[sum(wt * r[i] * r[j] for wt, r in zip(wv, R)) for j in range(3)] for i in range(3)]
    long_way = math.sqrt(sum(x[i] * S[i][j] * x[j] for i in range(3) for j in range(3)))
    pnl = [sum(a * b for a, b in zip(x, r)) for r in R]
    check(abs(ewma_sd(pnl, 0.94) - long_way) < 1e-9,
          "EWMA on the P&L series == sqrt(x' S x): no covariance matrix needed")

    # Euler components sum exactly to the total, and a hedge reads negative
    comp, tot = components(x, R, 0.94, 300, 0.99)
    check(abs(sum(comp) - tot) < 1e-9, "component VaR sums to the book VaR")
    check(abs(tot - z_of(0.99) * long_way) < 1e-9, "component total == z * EWMA sd")
    hedge = [1000.0, -300.0, 0.0]           # leg 1 carries half of leg 0: a partial hedge
    comp_h, _ = components(hedge, R, 0.94, 300, 0.99)
    check(comp_h[1] < 0, "a leg that hedges the book contributes NEGATIVE VaR")
    check(z_of(0.99) * ewma_sd([sum(a * b for a, b in zip(hedge, r)) for r in R], 0.94)
          < sum(abs(c) for c in comp_h) + 1e-9, "diversification: book <= sum of |components|")

    # common_returns: all-or-nothing spans a hole; a confirmed action is zero
    bars = {"a": {"d1": (0, 0, 0, 100.0), "d2": (0, 0, 0, 110.0), "d3": (0, 0, 0, 121.0),
                  "d4": (0, 0, 0, 121.0)},
            "b": {"d1": (0, 0, 0, 50.0), "d3": (0, 0, 0, 55.0), "d4": (0, 0, 0, 20.0)}}
    dates, Rc, zeroed = common_returns(bars, ["d1", "d2", "d3", "d4"], ["a", "b"], 10,
                                       {"b": {"d4": "test demerger"}})
    check(dates == ["d3", "d4"], "a session one leg lacks is skipped, not filled")
    check(abs(Rc[0][0] - 0.21) < 1e-12 and abs(Rc[0][1] - 0.10) < 1e-12,
          "the return across the hole is the COMMON interval for every leg")
    check(Rc[1][1] == 0.0 and zeroed and zeroed[0]["date"] == "d4",
          "a confirmed corporate action is a zero return, and listed")
    # ACCEPTANCE: an unconfirmed big move is KEPT (drawn through)
    _, Rk, zk = common_returns(bars, ["d1", "d2", "d3", "d4"], ["a", "b"], 10, {})
    check(abs(Rk[1][1] - (20 / 55 - 1)) < 1e-12 and not zk,
          "an unconfirmed jump is kept, not auto-excluded")

    # measure(): shapes, 10-day, worst days
    cfg = {**DEFAULTS, "windows": [100, 200], "ewma_sessions": 200}
    dts = [f"t{i:03d}" for i in range(len(R))]
    m = measure(x, R, dts, cfg)
    keys = [mm["key"] for mm in m["methods"]]
    check(keys == ["hist100", "hist200", "ewma"], f"methods present {keys}")
    check(all(mm["var"]["0.999"] >= mm["var"]["0.99"] >= mm["var"]["0.95"]
              for mm in m["methods"]), "VaR99.9 >= VaR99 >= VaR95 for every method")
    h100 = m["methods"][0]
    check(h100["thin"]["0.999"] and not h100["thin"]["0.95"],
          "100 sessions: 99.9% is flagged thin (0.1 obs beyond it), 95% is not")
    check(thin(250, 0.999) and not thin(1000, 0.999) and not thin(500, 0.99),
          "thin = fewer than ONE observation expected beyond the level")
    check(m["worst_days"][0]["pnl"] == round(min(m["pnl"][-200:]), 0), "worst day is the minimum")
    # sqrt-time is exact for iid; overlapping hist should be in its neighbourhood
    hzs = {hz["days"]: hz for hz in m["horizons"]}
    check(sorted(hzs) == [5, 10, 21], f"horizons present {sorted(hzs)}")
    hz = hzs[10]
    check(0.5 < hz["hist"]["0.99"] / hz["sqrt"]["0.99"] < 1.6,
          "10-day overlapping vs sqrt-time agree to within noise on iid data")
    check(all(0.6 < hzs[h]["vr"] < 1.5 for h in hzs), "iid data: variance ratio near 1")
    check(hzs[21]["hist_thin"]["0.999"] and not hzs[5]["hist_thin"]["0.95"],
          "a month's 99.9% on 14 independent windows is thin; a week's 95% is not")
    # MEAN REVERSION: an MA(1) with theta -0.5 has VR -> 0.25/1.25 = 0.2, so
    # sqrt-time must overstate and the VR-scaled estimate must come in under it
    e = [rng.gauss(0, 0.01) for _ in range(1201)]
    Rm = [[e[t] - 0.5 * e[t - 1]] for t in range(1, 1201)]
    mm = measure([1000.0], Rm, [f"m{i:04d}" for i in range(1200)],
                 {**DEFAULTS, "windows": [250], "ewma_sessions": 500})
    h21 = next(hz for hz in mm["horizons"] if hz["days"] == 21)
    check(h21["vr"] < 0.4, f"mean-reverting P&L: VR(21) {h21['vr']} well under 1")
    check(h21["ewma_vr"]["0.99"] < 0.7 * h21["sqrt"]["0.99"],
          "mean-reverting P&L: VR-scaled month VaR is well under sqrt-time")
    # tails: a constant-vol gaussian has ~zero excess kurtosis
    tl = mm["tails"]
    check(abs(tl["excess_kurtosis"]) < 1.0, f"gaussian P&L excess kurtosis {tl['excess_kurtosis']} ~ 0")
    meth = {x["key"]: x for x in mm["methods"]}
    check("fhs" in meth and 0.8 < meth["fhs"]["var"]["0.99"] / meth["ewma"]["var"]["0.99"] < 1.25,
          "gaussian P&L: filtered historical 99% ~ the EWMA normal's")
    # FAT TAILS: 1 day in 50 is a x5 shock. The normal's 99.9% must come in
    # under the filtered historical one, and kurtosis must say so
    g2 = [rng.gauss(0, 0.01) * (5 if rng.random() < 0.02 else 1) for _ in range(1500)]
    mf = measure([1000.0], [[v] for v in g2], [f"f{i:04d}" for i in range(1500)],
                 {**DEFAULTS, "windows": [250], "ewma_sessions": 500})
    mf_m = {x["key"]: x for x in mf["methods"]}
    check(mf_m["fhs"]["var"]["0.999"] > mf_m["ewma"]["var"]["0.999"],
          "fat-tailed P&L: filtered historical 99.9% exceeds the normal's")
    check(mf["tails"]["excess_kurtosis"] > 2 and mf["tails"]["beyond_normal_999_pct"] > 0.1,
          "fat-tailed P&L: kurtosis and the beyond-99.9% share both flag it")
    fr = filtered_residuals([1.0, -1.0] * 40, 0.94, warm=10)
    check(len(fr) == 70 and all(abs(abs(f) - 1) < 1e-9 for f in fr),
          "filtered residuals use the sd known the day BEFORE, |r| = 1 on a flat-vol path")
    # a window longer than the history is SKIPPED, never silently shortened
    m2 = measure(x, R[:150], dts[:150], cfg)
    check([mm["key"] for mm in m2["methods"]] == ["hist100", "ewma"],
          "a 200-session window on 150 returns is withheld, not truncated")

    # backtest arithmetic
    check(abs(binom_tail(30, 0, 0.01) - 1.0) < 1e-12, "P(X>=0) = 1")
    check(abs(binom_tail(1, 1, 0.05) - 0.05) < 1e-12, "P(X>=1 | n=1) = p")

    print(("SELFTEST PASS" if not fails else f"SELFTEST: {fails} FAILED"))
    sys.exit(1 if fails else 0)


def _report() -> None:
    r = build()
    if r.get("error"):
        print("var error:", r["error"])
        return
    nav = r["nav"]
    pc = lambda v: f"${v:>8,.0f} ({v / nav * 100:5.2f}%)"
    h = r["headline"]
    print(f"VaR — snapshot {r['snap']}, returns to {r['upto']}, NAV ${nav:,.0f}, "
          f"{r['n_legs']} legs, gross ${r['gross_usd']:,.0f}, net ${r['net_usd']:,.0f}, "
          f"beta-net ${r['beta_net_usd']:,.0f}")
    print(f"HEADLINE 1d {h['confidence']:.0%} ({h['label']}): {pc(h['var'])}  ES {pc(h['es'])}")
    for m in r["methods"]:
        print(f"  {m['label']:32s} {m['start']}..{m['end']}  "
              + "  ".join(f"VaR{float(k) * 100:g}{'*' if m.get('thin', {}).get(k) else ''} {pc(v)}"
                          for k, v in m["var"].items())
              + "  " + "  ".join(f"ES{float(k) * 100:g} ${v:,.0f}" for k, v in m["es"].items()))
    if r.get("tails"):
        t = r["tails"]
        print(f"  tails: excess kurtosis {t['excess_kurtosis']}, {t['beyond_normal_999_pct']}% of "
              f"{t['n']} days beyond the normal's 99.9% line (normal: 0.1%)")
    print("horizons (VR = measured variance ratio):")
    for hz in r["horizons"]:
        for key, lbl in (("ewma_vr", "EWMA x sqrt(h VR)"), ("sqrt", "EWMA x sqrt(h)"),
                         ("hist", "historical overlap")):
            print(f"  {hz['label']:8s} VR {hz['vr']:.2f}  {lbl:20s} "
                  + "  ".join(f"{float(k) * 100:>4g} {pc(v)}{'*' if key == 'hist' and hz['hist_thin'][k] else ' '}"
                              for k, v in hz[key].items()))
    print("  * thin: fewer than one independent window beyond the level")
    print("worst days:", ", ".join(f"{w['date']} ${w['pnl']:,.0f}" for w in r["worst_days"]))
    print(f"\ncontribution to the headline VaR (sum of standalones "
          f"${r['diversification']['sum_standalone']:,.0f} vs book ${h['var']:,.0f}):")
    for p in r["pairs"] or [{"label": l["name"], "contrib": l["contrib"],
                             "standalone": l["standalone"], "gross_usd": abs(l["usd"])}
                            for l in r["legs"]]:
        print(f"  {p['label']:24s} gross ${p['gross_usd']:>7,.0f}  contrib ${p['contrib']:>6,.0f} "
              f"({p['contrib'] / h['var'] * 100:5.1f}%)  standalone ${p['standalone']:>6,.0f}")
    print("\nstress (today's dollars):")
    for s in r["stress"]:
        print(f"  {s['label']:28s} legs {s['legs_priced']}/{s['legs_total']}  worst day "
              f"{s['worst_day']['date']} {pc(s['worst_day']['pnl'])}  peak-to-trough {pc(s['peak_to_trough'])}")
    bt = r["backtest"]
    print(f"\nbacktest over {bt['n']} snapshot pairs:")
    for k, s in bt["summary"].items():
        print(f"  {k:9s} n {s['n']:3d}  breaks {s['breaks']} (expected {s['expected']})  "
              f"P(>=) {s['p_at_least']}  {', '.join(s['dates'])}")
    for k, v in r["excluded"].items():
        if v:
            print(f"EXCLUDED {k}:", ", ".join(o["root"] for o in v))
    f = r["flags"]
    if f["zeroed"]:
        print("zeroed (confirmed actions):", ", ".join(f"{z['entity_id']} {z['date']}" for z in f["zeroed"]))
    if f["jumps"]:
        print("kept, |move| > jump_flag:", ", ".join(f"{j['entity_id']} {j['date']} {j['ret_pct']}%" for j in f["jumps"]))
    if f["pre_demerger_history"]:
        print("pre-demerger history in window:", ", ".join(f["pre_demerger_history"]))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        _report()
