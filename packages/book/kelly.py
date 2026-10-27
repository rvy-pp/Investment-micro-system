"""Kelly sizing for the book's dictated pairs.

PM, 2026-09-25: *"Let's start with applying kelly criterion to the book."*
The RISK is measured here from `prices`; the default fraction is HALF Kelly
and the horizon is fixed at 13 weeks.

THE EDGE COMES FROM THE CASE AGENTS (PM, 2026-10-22): *"Instead of my view in
the kelly, we put the agents view from here. I will change my view on this
only. Remove all text from my side."* A pair's edge is the Analysis tab's
pair juice — the long leg's lean minus the short leg's (`data.juice_pct` on
each stock's live `price` entry, set by the PM through that stock's agent) —
read by agent_edges(). A pair gets an edge only when BOTH legs carry a lean.
The leans are to the next print (~mid-January for the IT names), which falls
inside the 13-week window; that is the PM's equivalence, stated in each
edge's note, not a scaling done here.

DEFAULT LEAN (PM, 2026-10-27: "for stocks with no lean, put medium by
default" = the base case, at the anchor on EV/EBITDA legs). A leg with a case
build and no PM lean carries its live base juice (analysis.default_lean), so
such a pair HAS an edge now. Every such leg is named in the edge's note as
"default base", and the leg carries source 'default_base', so a sized pair
never hides that half its view is a default.

Before that the edge was typed per pair on the Book tab and stored
append-only in `book_kelly_edges` via POST /api/kelly_edge (2026-09-25 →
2026-10-22). The route and the inputs are gone; the TABLE keeps its rows as
history and nothing reads it. Earlier still the edges lived in
specs/book.yaml kelly.edges; a non-empty key there is reported, not read.

The page recomputes in the browser from `calc` (shrunk covariance, per-pair
variances, today's sizes at full precision); app.html's kellyCalc() mirrors
compute() here.

THE SPLIT IS THE WHOLE DESIGN, and it is this repo's inversion again
---------------------------------------------------------------------
Kelly needs two things, an expected return and a covariance. The covariance
is measurable, reproducible and testable, so code measures it. The expected
return is not: every number this system computes that could stand in for it
was considered and REFUSED —

  the composite score gap   no composite is validated (CLAUDE.md: "Do not
                            read any composite in this system as validated").
                            Sizing on it would lever an unproven signal.
  the spread's own drift    a back-cast of TODAY'S roster — survivorship by
                            construction, and mostly momentum. Not a forecast.

So the edge is the PM's view — now expressed as the two legs' leans — with a
NOT-EMPTY `note` beside it naming both leans and their dates, the book-side
cousin of invariants 1 and 2 (no number without provenance). An edge with no
note is refused, not defaulted.

WHAT A "PAIR RETURN" IS HERE
----------------------------
r_t = mean(long legs' close-to-close return) − mean(short legs' return), on
sessions where EVERY leg printed. That is the P&L per unit of notional on EACH
side of a dollar-neutral pair, so the Kelly weight f is the notional per side
as a fraction of NAV, and the pair's gross is 2f. The book's real pairs are
NOT exactly dollar-neutral (TCS 29k long vs INFO 38k short on 23-09); the
imbalance is reported beside the size, never folded into it. INR against INR,
so the USD NAV's FX leg cancels to first order on a neutral pair.

Continuous Kelly: f* = Σ⁻¹ μ (per-session μ and Σ, any consistent period —
the ratio is period-free). Applied at `fraction`, default 0.5.

TWO SOLVES, BOTH SHOWN, because they answer different questions
---------------------------------------------------------------
  standalone   f = μ/σ² for the pair alone. Intuitive, and WRONG for this
               book in one specific way: legs are shared (DIXON short backs
               three longs, PSYS two, NACL two, LTTS two, JSTL two), so those
               pairs are strongly correlated and summing standalone sizes
               counts the same bet several times.
  joint        Σ⁻¹ μ over every pair that has an edge AND enough history.
               Correlated pairs split one bet between them; a pair can come
               out NEGATIVE — Kelly saying the edge is better expressed by the
               others and this one is a hedge. That is flagged `flip`, never
               clipped to zero (clipping re-solves nothing and silently
               changes every other weight's meaning).

THE REVERSE READ — useful on day one, with no edges entered
-----------------------------------------------------------
`implied_edge_pct`: the expected pair return over `horizon_weeks` at which
TODAY'S size would be exactly `fraction`-Kelly. μ = f_now·σ² / fraction. It
turns a size into a claim the PM can agree or disagree with ("the HZ_VEDL size
says I expect +X% in 13 weeks"). The joint version, Σ·f_now / fraction, is the
one that accounts for overlap; both are printed. It is ARITHMETIC ON THE
CURRENT SIZE, not a forecast, and the page says so.

COVARIANCE SHRINKAGE — measured, not chosen
-------------------------------------------
Σ⁻¹ amplifies sampling noise, worst exactly where this book lives: many
correlated pairs. The sample covariance is shrunk toward its own diagonal with
the Schäfer–Strimmer analytic intensity δ (target "D", unequal variances):

    δ = Σ_{i≠j} Var(s_ij) / Σ_{i≠j} s_ij²,   clipped to [0, 1]

The diagonal is untouched, so every STANDALONE size is unaffected by the
shrink — only the joint solve is. `shrink:` in the spec may pin a number
instead of `auto`; δ is printed either way.

Guards, each in `--selftest` with acceptance beside rejection (GLOB lesson)
--------------------------------------------------------------------------
- A pair with fewer than `min_sessions` in the lookback is STANDALONE ONLY
  and flagged: the joint solve runs on the dates COMMON to every member, so
  VAML_NACL (listed 2026-06-15) would otherwise cut every pair's history to
  ~70 sessions.
- Phantom sessions (every leg flat with zero volume — basket_index's test)
  and CONFIRMED corporate-action dates are dropped per pair. VEDL's
  2026-04-30 demerger is inside a two-year lookback and would otherwise enter
  HZ_VEDL's variance as a −65% day.
- An edge naming no dictated pair is reported as `unknown_edges`, not
  ignored: a renamed pair (the 23-09 flips) would otherwise silently lose its
  edge and read "no edge entered".
- A pair with a dictated leg off the book, or a leg with no entity, is
  skipped and named.

KNOWN, NOT FIXED: VEDL before 2026-04-30 is a different company (the
pre-demerger conglomerate). Its returns still enter HZ_VEDL's σ for the
pre-demerger part of the lookback; the flag `pre_demerger_history` says so.

Nothing here writes. There is no kelly table, for basket_index's reason: it
recomputes from `prices` and `book_positions` in well under a second, and a
stored copy would only be a second thing that can be stale.
"""
from __future__ import annotations

import datetime as dt
import math
import pathlib
import random
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))
sys.path.insert(0, str(REPO / "packages" / "book"))

DB = REPO / "data" / "ims.db"
SPEC = REPO / "specs" / "book.yaml"

SESSIONS_PER_WEEK = 5
SESSIONS_PER_YEAR = 252

DEFAULTS = {"fraction": 0.5, "lookback_days": 730, "min_sessions": 120,
            "horizon_weeks": 13, "shrink": "auto",
            "horizon_variance": "measured", "vr_lookback_days": 7300,
            "vr_min_sessions": 750}


# ---------------------------------------------------------------- spec ----

def _spec() -> dict:
    import yaml
    cfg = yaml.safe_load(SPEC.read_text(encoding="utf-8")) or {}
    k = {**DEFAULTS, **(cfg.get("kelly") or {})}
    k["spec_edges"] = k.pop("edges", None) or {}
    return k


def agent_edges(aview: dict, H: float) -> dict:
    """{pair: {ret_pct, horizon_weeks, note, set_at, legs}} from the Analysis
    view (packages/book/analysis.py view()): pair juice = long lean − short
    lean. A pair with a leg that has no lean is ABSENT — half a pair's view
    is not a pair view, and an absent edge is not a zero one."""
    by = {s["entity"]: s for s in aview.get("stocks") or []}
    out = {}
    for p in aview.get("pairs") or []:
        if p.get("pair_juice") is None:
            continue
        legs = []
        for side, ents in (("L", p["long"]), ("S", p["short"])):
            for e in ents:
                s = by.get(e) or {}
                pr = (s.get("steps") or {}).get("price") or {}
                legs.append({"side": side, "entity": e, "name": s.get("name", e),
                             "juice_pct": s.get("juice"), "as_of": pr.get("as_of"),
                             "horizon": s.get("horizon"), "source": s.get("juice_source") or "pm"})
        txt = " − ".join(
            " / ".join(f"{l['name']} {l['juice_pct']:+.1f}% "
                       + ("(default base)" if l["source"] == "default_base" else f"({l['as_of']})")
                       for l in legs if l["side"] == sd) for sd in ("L", "S"))
        dflt = any(l["source"] == "default_base" for l in legs)
        out[p["name"]] = {
            "ret_pct": p["pair_juice"], "horizon_weeks": H, "legs": legs, "default": dflt,
            "set_at": max((l["as_of"] or "") for l in legs) or None,
            "note": f"case agents: {txt}; each lean is to the next print, "
                    f"inside the {H:g}-week window"
                    + ("; a default base is the headline-year case juice (at the anchor on "
                       "EV/EBITDA legs), taken as the window's edge until a lean is picked" if dflt else "")}
    return out


def parse_edges(raw: dict, default_h: float) -> tuple[dict, list]:
    """{pair: {ret_pct, horizon_weeks?, note}} -> ({pair: edge}, problems).

    Refuses rather than defaults: a missing note, a non-number, a horizon
    <= 0. A refused edge is ABSENT from the solve and listed, never zeroed —
    a zero edge is a claim ("no expected return"), an absent one is not.
    """
    ok, bad = {}, []
    for name, e in (raw or {}).items():
        e = e or {}
        note = str(e.get("note") or "").strip()
        try:
            ret = float(e.get("ret_pct"))
            h = float(e.get("horizon_weeks", default_h))
        except (TypeError, ValueError):
            bad.append(f"{name}: ret_pct/horizon_weeks not a number")
            continue
        if not note:
            bad.append(f"{name}: no note — an edge needs its provenance")
            continue
        if h <= 0:
            bad.append(f"{name}: horizon_weeks must be > 0")
            continue
        ok[str(name)] = {"ret_pct": ret, "horizon_weeks": h, "note": note,
                         # per SESSION, the unit every Σ here is in
                         "mu": ret / 100.0 / (h * SESSIONS_PER_WEEK)}
    return ok, bad


# ---------------------------------------------------------- linear alg ----

def _solve(A: list[list[float]], b: list[float]) -> list[float] | None:
    """A x = b by Gaussian elimination with partial pivoting. None if singular.

    Pure Python on purpose: the matrix is at most ~20x20 and nothing else in
    packages/ imports numpy — a new dependency for one solve is not worth it.
    """
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-18:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            if f:
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x


def _matvec(A, x):
    return [sum(a * b for a, b in zip(row, x)) for row in A]


def covariance(series: list[list[float]], shrink="auto") -> tuple[list, float]:
    """Sample covariance of aligned series, shrunk toward its diagonal.

    `series[i]` is pair i's returns on the SAME dates as every other. Returns
    (Σ, δ). δ is Schäfer–Strimmer's analytic intensity unless `shrink` pins
    it. The diagonal is never shrunk, so standalone sizes are unaffected.
    """
    p = len(series)
    n = len(series[0]) if p else 0
    if n < 3:
        raise ValueError(f"covariance needs >= 3 aligned sessions, got {n}")
    means = [sum(s) / n for s in series]
    dev = [[x - m for x in s] for s, m in zip(series, means)]
    S = [[0.0] * p for _ in range(p)]
    num = den = 0.0
    for i in range(p):
        for j in range(i, p):
            w = [a * b for a, b in zip(dev[i], dev[j])]
            wbar = sum(w) / n
            s = n / (n - 1) * wbar
            S[i][j] = S[j][i] = s
            if i != j:
                var_s = n / (n - 1) ** 3 * sum((x - wbar) ** 2 for x in w)
                num += 2 * var_s
                den += 2 * s * s
    if shrink == "auto":
        d = 0.0 if den == 0 else max(0.0, min(1.0, num / den))
    else:
        d = max(0.0, min(1.0, float(shrink)))
    for i in range(p):
        for j in range(p):
            if i != j:
                S[i][j] *= (1 - d)
    return S, d


# ---------------------------------------------- horizon variance ratio ----
#
# THE 13-WEEK VARIANCE IS NOT 65 x THE DAILY ONE ON THESE SPREADS (PM ruling
# 2026-10-06: "yes, fix the 13-week variance"). Until this date compute()
# sized off the daily σ² with the edge spread per session, which is the same
# as assuming 13-week variance = 65 x daily variance — i.e. that a spread's
# daily moves are independent. They are not: pairs of same-sector names
# partly mean-revert inside a quarter. Measured over each pair's history since
# 2007, the 13-week variance runs at ~0.66 of 65 x daily (13w σ ~0.82 of the
# √65-scaled daily σ). Kelly divides by variance, so every size was ~1.5x too
# SMALL and every implied edge ~1.5x too BIG — plausible numbers, nothing
# raised: the SILENT_BUGS shape. Wrong value 1.00, right value ~0.66; caught
# by testing the √time assumption directly rather than by any symptom.
#
# The ratio is the Lo–MacKinlay overlapping-window variance ratio, bias-
# corrected for the overlap and for the estimated mean. ONE POOLED RATIO, NOT
# ONE PER PAIR, and that is measured rather than chosen: across the 16 pairs
# the dispersion of the per-pair ratios (0.0325) equals what sampling noise
# alone produces (0.0326), so the between-pair variance τ² is 0 and the
# per-pair numbers (0.42 .. 1.13) are noise around one value. Each pair is
# shrunk toward the pooled ratio with weight τ²/(τ² + se²) — the same
# "measured, not chosen" rule as the covariance δ — so the day a pair's ratio
# becomes distinguishable it keeps it, and today they all take the pool.
#
# What is scaled and what is not: the LEVEL of σ² still comes from the
# lookback window (recent risk); only the daily -> 13-week SHAPE comes from
# the long history, which is the only place with enough non-overlapping
# quarters to measure it. Correlations are untouched (Σ_ij x √(r_i r_j)), so
# with one pooled ratio the joint PROPORTIONS — the "at today's gross" column
# — do not move at all; only absolute sizes and implied edges do.

def variance_ratio(xs: list[float], q: int) -> float | None:
    """Lo–MacKinlay (1988) VR(q) with overlapping q-sums, unbiased form:
    σ²_c / σ²_a, where σ²_c = Σ_k (S_k − S_{k−q} − qμ)² / m and
    m = q(T−q+1)(1−q/T). 1.0 for independent increments; < 1 mean-reverting.
    None below 2q observations (fewer than two non-overlapping windows)."""
    T = len(xs)
    if T < 2 * q:
        return None
    mu = sum(xs) / T
    sa = sum((x - mu) ** 2 for x in xs) / (T - 1)
    if sa <= 0:
        return None
    S = [0.0]
    for x in xs:
        S.append(S[-1] + x)
    m = q * (T - q + 1) * (1 - q / T)
    sc = sum((S[k] - S[k - q] - q * mu) ** 2 for k in range(q, T + 1)) / m
    return sc / sa


def vr_se2(T: int, q: int) -> float:
    """Asymptotic sampling variance of VR(q) under independent increments
    (Lo–MacKinlay): 2(2q−1)(q−1) / (3qT). A floor — fat tails and volatility
    clustering only widen it, which only strengthens the case for pooling."""
    return 2 * (2 * q - 1) * (q - 1) / (3 * q * T)


def horizon_ratios(raw: dict[str, tuple[float | None, int]], q: int,
                   min_sessions: int, pin=None) -> dict:
    """{label: (raw VR or None, n sessions)} -> the ratio each pair is sized on.

    Pairs with >= min_sessions estimate the pool (inverse-variance weighted
    mean) and τ²; each then takes w·own + (1−w)·pool, w = τ²/(τ²+se²). A pair
    under min_sessions takes the pool, flagged. `pin` (a number) overrides
    everything — 1.0 restores the old √time assumption. No qualifying pair at
    all -> 1.0 with basis 'none', stated rather than silently assumed.
    """
    out = {"q": q, "pool": None, "tau2": None, "n_pool": 0, "pairs": {}}
    if pin is not None:
        p = float(pin)
        out.update(pool=p, basis="pinned")
        for lab, (v, n) in raw.items():
            out["pairs"][lab] = {"raw": v, "n": n, "w": 0.0, "ratio": p,
                                 "basis": "pinned"}
        return out
    own = {lab: (v, n) for lab, (v, n) in raw.items()
           if v is not None and n >= min_sessions}
    if not own:
        out["basis"] = "none"
        for lab, (v, n) in raw.items():
            out["pairs"][lab] = {"raw": v, "n": n, "w": 0.0, "ratio": 1.0,
                                 "basis": "none"}
        return out
    se2 = {lab: vr_se2(n, q) for lab, (v, n) in own.items()}
    wsum = sum(1 / s for s in se2.values())
    pool = sum(own[lab][0] / se2[lab] for lab in own) / wsum
    vals = [v for v, _ in own.values()]
    mv = sum(vals) / len(vals)
    disp = sum((v - mv) ** 2 for v in vals) / len(vals)
    tau2 = max(0.0, disp - sum(se2.values()) / len(se2))
    out.update(pool=pool, tau2=tau2, n_pool=len(own), basis="measured",
               dispersion=disp, mean_se2=sum(se2.values()) / len(se2))
    for lab, (v, n) in raw.items():
        if lab in own:
            w = tau2 / (tau2 + se2[lab]) if tau2 > 0 else 0.0
            out["pairs"][lab] = {"raw": v, "n": n, "w": w,
                                 "ratio": w * v + (1 - w) * pool,
                                 # w = 0 is the pool exactly; say so
                                 "basis": "shrunk" if w > 0 else "pooled"}
        else:
            out["pairs"][lab] = {"raw": v, "n": n, "w": 0.0, "ratio": pool,
                                 "basis": "pooled_short"}
    return out


# ------------------------------------------------------------- series ----

def _load_bars(conn, eids, start):
    bars, vols = {}, {}
    for e in eids:
        bars[e], vols[e] = {}, {}
        for r in conn.execute(
                "SELECT date, open, high, low, close, volume FROM prices "
                "WHERE entity_id=? AND date>=? AND close IS NOT NULL "
                "ORDER BY date", (e, start)):
            bars[e][r[0]] = (r[1], r[2], r[3], r[4])
            vols[e][r[0]] = r[5]
    return bars, vols


def pair_returns(L: list, S: list, bars: dict, vols: dict,
                 actions: dict) -> tuple[dict, dict]:
    """{date: r} for one pair, plus what was dropped and why.

    A date counts only where every leg printed; the return runs from the
    previous such date, so a missing print widens an interval rather than
    fabricating a zero. Phantom dates (every leg flat, zero volume) and
    confirmed corporate-action dates are dropped — the latter AFTER the
    return is formed, so the day after an action is not measured against the
    pre-action price either.
    """
    import basket_index
    legs = L + S
    common = sorted(set.intersection(*(set(bars[e]) for e in legs)))
    first = {e: min(bars[e]) for e in legs if bars[e]}
    sub_b = {e: bars[e] for e in legs}
    sub_v = {e: vols[e] for e in legs}
    live, phantom, _ = basket_index.sessions(sub_b, sub_v, first, 0.0)
    live = set(live)
    dates = [d for d in common if d in live]
    cut = {d for e in legs for d in (actions.get(e) or {})}
    out = {}
    for d0, d1 in zip(dates, dates[1:]):
        if d1 in cut:
            continue
        rl = [bars[e][d1][3] / bars[e][d0][3] - 1 for e in L]
        rs = [bars[e][d1][3] / bars[e][d0][3] - 1 for e in S]
        out[d1] = ((sum(rl) / len(rl)) if rl else 0.0) - (
            (sum(rs) / len(rs)) if rs else 0.0)
    dropped = {"phantom": sorted(p["date"] for p in phantom
                                 if p["date"] in set(common)),
               "actions": sorted(cut & set(dates))}
    return out, dropped


# -------------------------------------------------------------- engine ----

def compute(pairs: list[dict], rets: dict[str, dict], edges: dict,
            fraction: float, min_sessions: int, horizon_weeks: float,
            shrink="auto", vr: dict[str, float] | None = None) -> dict:
    """The arithmetic, separated from the store so the selftest can drive it.

    pairs: [{label, f_now}] — f_now is CURRENT notional per side / NAV.
    rets:  {label: {date: r}}.
    edges: parse_edges output.
    vr:    {label: horizon variance ratio} (horizon_ratios). Missing / None
           = 1.0, the old √time assumption. Applied as a DAILY-EQUIVALENT
           variance (ratio x daily σ²), so everything downstream — μ per
           session, the browser's kellyCalc — is unchanged in form: the page
           mirrors this function by reading the scaled var/cov from `calc`.
    """
    H = horizon_weeks * SESSIONS_PER_WEEK
    vr = vr or {}
    rows = {}
    for p in pairs:
        lab, r = p["label"], rets.get(p["label"]) or {}
        xs = list(r.values())
        n = len(xs)
        row = {"label": lab, "n_sessions": n, "f_now": p["f_now"],
               "flags": list(p.get("flags") or [])}
        if n >= 3:
            m = sum(xs) / n
            var_raw = sum((x - m) ** 2 for x in xs) / (n - 1)
            ratio = float(vr.get(lab, 1.0))
            var = var_raw * ratio            # 13-week-consistent, per session
            row["sigma_d"] = math.sqrt(var)
            # the DAILY σ as measured (the column says "daily closes") ...
            row["sigma_ann_pct"] = math.sqrt(var_raw * SESSIONS_PER_YEAR) * 100
            # ... and the σ Kelly actually sizes on, over the horizon
            row["sigma_h_pct"] = math.sqrt(var * H) * 100
            row["vr"] = ratio
            row["var_d"] = var
            # size -> the edge that would make it exactly `fraction`-Kelly
            row["implied_edge_pct"] = (p["f_now"] * var / fraction) * H * 100
        if n < min_sessions:
            row["flags"].append("short_history")
        e = edges.get(lab)
        row["edge"] = ({k: e[k] for k in ("ret_pct", "horizon_weeks", "note")}
                       if e else None)
        if e and "var_d" in row and row["var_d"] > 0:
            row["f_standalone"] = fraction * e["mu"] / row["var_d"]
        rows[lab] = row

    # ---- joint: every pair with enough history, on COMMON dates ----------
    pool = [lab for lab, r in rows.items()
            if r["n_sessions"] >= min_sessions and r.get("var_d")]
    joint = {"members": [], "delta": None, "n_common": 0}
    if len(pool) >= 1:
        common = sorted(set.intersection(*(set(rets[l]) for l in pool)))
        joint["n_common"] = len(common)
        if len(common) >= min_sessions:
            series = [[rets[l][d] for d in common] for l in pool]
            Sig, delta = covariance(series, shrink)
            # horizon scaling: variances by r_i, covariances by √(r_i r_j) —
            # correlations untouched. With all ratios 1.0 this is a no-op,
            # which the selftest pins.
            sq = [math.sqrt(float(vr.get(l, 1.0))) for l in pool]
            Sig = [[Sig[i][j] * sq[i] * sq[j] for j in range(len(pool))]
                   for i in range(len(pool))]
            joint["delta"] = delta
            joint["_pool"], joint["_cov"] = pool, Sig
            # the REVERSE read over the whole book: Σ f_now / fraction
            fnow = [rows[l]["f_now"] for l in pool]
            mu_imp = _matvec(Sig, fnow)
            for l, m in zip(pool, mu_imp):
                rows[l]["implied_edge_joint_pct"] = m / fraction * H * 100
            # the FORWARD solve over pairs the PM gave an edge
            ed = [i for i, l in enumerate(pool) if l in edges]
            if ed:
                A = [[Sig[i][j] for j in ed] for i in ed]
                mu = [edges[pool[i]]["mu"] for i in ed]
                x = _solve(A, mu)
                if x is None:
                    joint["error"] = "covariance singular over the edged pairs"
                else:
                    f = [fraction * v for v in x]
                    for i, fi in zip(ed, f):
                        rows[pool[i]]["f_joint"] = fi
                        if fi < 0:
                            rows[pool[i]]["flags"].append("flip")
                    joint["members"] = [pool[i] for i in ed]
                    # SAME RISK BUDGET, KELLY PROPORTIONS. Continuous Kelly on
                    # a pair spread levers hard (a +5%/13w edge on an 19%-vol
                    # spread is ~270% of NAV a side at half Kelly), so the
                    # absolute size is rarely the binding answer. What
                    # survives any budget is the ALLOCATION: Σ⁻¹μ rescaled so
                    # the edged pairs keep exactly today's combined gross.
                    # effective_fraction is the Kelly multiple that implies.
                    now_g = sum(abs(rows[pool[i]]["f_now"]) for i in ed)
                    kel_g = sum(abs(v) for v in f)
                    if kel_g > 0:
                        sc = now_g / kel_g
                        joint["budget_scale"] = sc
                        joint["effective_fraction"] = fraction * sc
                        for i, fi in zip(ed, f):
                            rows[pool[i]]["f_budget"] = fi * sc
                    # expected growth and vol of the target, per year
                    Sf = _matvec(A, f)
                    er = sum(a * b for a, b in zip(f, mu))
                    vr = sum(a * b for a, b in zip(f, Sf))
                    joint["exp_ret_ann_pct"] = er * SESSIONS_PER_YEAR * 100
                    joint["vol_ann_pct"] = math.sqrt(
                        max(vr, 0) * SESSIONS_PER_YEAR) * 100
                    joint["growth_ann_pct"] = (er - vr / 2) * SESSIONS_PER_YEAR * 100
                    joint["gross_pct"] = sum(2 * abs(v) for v in f) * 100
                    # the CURRENT book on the same edges, for comparison
                    fn = [rows[pool[i]]["f_now"] for i in ed]
                    Sn = _matvec(A, fn)
                    en = sum(a * b for a, b in zip(fn, mu))
                    vn = sum(a * b for a, b in zip(fn, Sn))
                    joint["now_exp_ret_ann_pct"] = en * SESSIONS_PER_YEAR * 100
                    joint["now_vol_ann_pct"] = math.sqrt(
                        max(vn, 0) * SESSIONS_PER_YEAR) * 100
                    joint["now_growth_ann_pct"] = (en - vn / 2) * SESSIONS_PER_YEAR * 100
        else:
            joint["error"] = (f"only {len(common)} sessions common to the "
                              f"{len(pool)} pairs — below min_sessions")
    for r in rows.values():
        for k in ("f_standalone", "f_joint", "f_now", "f_budget"):
            if r.get(k) is not None:
                r[k.replace("f_", "gross_") + "_pct"] = 2 * r[k] * 100
        r["_var"] = r.pop("var_d", None)
        r.pop("sigma_d", None)
    return {"rows": [rows[p["label"]] for p in pairs], "joint": joint}


def build(view: list[dict], nav: float, conn: sqlite3.Connection | None = None,
          spec: dict | None = None, aview: dict | None = None) -> dict:
    """The /api/book `kelly` block, from engine.book_view's dictated view."""
    from corporate_actions import CONFIRMED_ACTIONS
    spec = spec or _spec()
    fraction = float(spec["fraction"])
    H = float(spec["horizon_weeks"])
    import analysis
    if aview is None:
        aview = analysis.view()
    raw = agent_edges(aview, H)
    edges, bad = parse_edges(raw, H)
    if spec.get("spec_edges"):
        bad.append("specs/book.yaml kelly.edges is no longer read — edges come "
                   "from the case agents' leans (" + ", ".join(spec["spec_edges"]) + ")")
    own = conn is None
    if own:
        conn = sqlite3.connect(DB)
    try:
        eids = sorted({l["entity_id"] for p in view for l in p["legs"]
                       if l.get("entity_id")})
        # the window ends at the book's own latest close, not today: the
        # tape clock, never the calendar (a Saturday run must not shorten it)
        end = conn.execute(
            f"SELECT MAX(date) FROM prices WHERE entity_id IN "
            f"({','.join('?' * len(eids))})", eids).fetchone()[0] if eids else None
        end = end or dt.date.today().isoformat()
        start = (dt.date.fromisoformat(end)
                 - dt.timedelta(days=int(spec["lookback_days"]))).isoformat()
        bars, vols = _load_bars(conn, eids, start)
        # the long history, for the daily -> 13-week variance ratio only
        hv = spec.get("horizon_variance", "measured")
        lstart = (dt.date.fromisoformat(end)
                  - dt.timedelta(days=int(spec["vr_lookback_days"]))).isoformat()
        lbars, lvols = (_load_bars(conn, eids, lstart) if hv == "measured"
                        else (None, None))
    finally:
        if own:
            conn.close()

    pairs, rets, skipped = [], {}, []
    raw_vr: dict[str, tuple] = {}
    for p in view:
        lab = p["label"]
        if p.get("missing"):
            skipped.append({"label": lab, "why": "dictated leg off the book: "
                            + ", ".join(p["missing"])})
            continue
        L = [l["entity_id"] for l in p["legs"] if l["side"] == "L"]
        S = [l["entity_id"] for l in p["legs"] if l["side"] == "S"]
        if not L or not S or None in L + S or any(not bars.get(e) for e in L + S):
            skipped.append({"label": lab, "why": "a leg has no entity or no prices"})
            continue
        r, dropped = pair_returns(L, S, bars, vols, CONFIRMED_ACTIONS)
        rets[lab] = r
        if lbars is not None:
            lr, _ = pair_returns(L, S, lbars, lvols, CONFIRMED_ACTIONS)
            lx = [lr[d] for d in sorted(lr)]
            raw_vr[lab] = (variance_ratio(lx, int(H * SESSIONS_PER_WEEK)), len(lx))
        lg = sum((l["gross_usd"] or 0) * (l.get("share") or 1)
                 for l in p["legs"] if l["side"] == "L")
        sg = sum((l["gross_usd"] or 0) * (l.get("share") or 1)
                 for l in p["legs"] if l["side"] == "S")
        flags = []
        # pre-demerger history of a confirmed-action leg is a different company
        for e in L + S:
            for d in CONFIRMED_ACTIONS.get(e) or {}:
                if r and min(r) < d:
                    flags.append("pre_demerger_history")
        pairs.append({"label": lab, "sector": p.get("sector"),
                      "f_now": (lg + sg) / 2 / nav if nav else 0.0,
                      "long_usd": round(lg), "short_usd": round(sg),
                      "dropped": dropped, "flags": sorted(set(flags))})

    q = int(H * SESSIONS_PER_WEEK)
    if hv == "measured":
        hr = horizon_ratios(raw_vr, q, int(spec["vr_min_sessions"]))
    else:   # a pinned number; 1.0 restores the old sqrt-time assumption
        hr = horizon_ratios({p["label"]: (None, 0) for p in pairs}, q, 0,
                            pin=float(hv))
    for p in pairs:
        if (hr["pairs"].get(p["label"]) or {}).get("basis") == "pooled_short":
            p["flags"].append("vr_pooled")
    out = compute(pairs, rets, edges, fraction, int(spec["min_sessions"]), H,
                  spec.get("shrink", "auto"),
                  vr={lab: v["ratio"] for lab, v in hr["pairs"].items()})
    # full-precision inputs for the browser's live recompute: rounding a
    # variance of ~1.4e-4 to 4dp below would zero it
    j = out["joint"]
    out["calc"] = {
        "fraction": fraction, "horizon_weeks": H,
        "sessions_per_week": SESSIONS_PER_WEEK,
        "sessions_per_year": SESSIONS_PER_YEAR,
        "pool": j.pop("_pool", []), "cov": j.pop("_cov", None),
        "var": {r["label"]: r.pop("_var") for r in out["rows"]},
        "f_now": {r["label"]: r["f_now"] for r in out["rows"]},
    }
    meta = {p["label"]: p for p in pairs}
    for r in out["rows"]:
        m = meta[r["label"]]
        r.update(sector=m["sector"], long_usd=m["long_usd"],
                 short_usd=m["short_usd"], dropped=m["dropped"])
        for k, v in list(r.items()):
            if isinstance(v, float):
                r[k] = round(v, 4)
    for k, v in list(out["joint"].items()):
        if isinstance(v, float):
            out["joint"][k] = round(v, 4)
    known = {p["label"] for p in view}
    out.update({
        "fraction": fraction, "horizon_weeks": H,
        "lookback_days": int(spec["lookback_days"]), "window": [start, end],
        "horizon_variance": {
            "basis": hr.get("basis"), "pool": hr.get("pool"),
            "sd_ratio": (math.sqrt(hr["pool"]) if hr.get("pool") else None),
            "tau2": hr.get("tau2"), "dispersion": hr.get("dispersion"),
            "mean_se2": hr.get("mean_se2"), "n_pool": hr.get("n_pool"),
            "from": lstart if hv == "measured" else None,
            "min_sessions": int(spec["vr_min_sessions"]),
            "pairs": {lab: {k: (round(v, 4) if isinstance(v, float) else v)
                            for k, v in d.items()}
                      for lab, d in hr["pairs"].items()}},
        "min_sessions": int(spec["min_sessions"]),
        "shrink": spec.get("shrink", "auto"),
        "n_edges": len([e for e in edges if e in known]),
        "edges_source": "case agents (long lean − short lean, Analysis tab)",
        "edges_set_at": {k_: raw[k_]["set_at"] for k_ in edges if k_ in raw},
        "edge_legs": {k_: raw[k_]["legs"] for k_ in edges if k_ in raw},
        "bad_edges": bad,
        "unknown_edges": sorted(e for e in edges if e not in known),
        "skipped": skipped,
    })
    return out


# ------------------------------------------------------------ selftest ----

def _selftest() -> None:
    ok = 0

    def check(cond, msg):
        nonlocal ok
        if not cond:
            raise AssertionError(msg)
        ok += 1
        print("  ok ", msg)

    def gauss(seed, n, mu, sd):
        rg = random.Random(seed)
        return [rg.gauss(mu, sd) for _ in range(n)]

    days = [f"d{i:04d}" for i in range(600)]

    def ser(xs):
        return dict(zip(days, xs))

    def var(xs):
        m = sum(xs) / len(xs)
        return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)

    H = 13
    # 1. standalone is exactly fraction * mu / sigma^2 ----------------------
    a = gauss(1, 600, 0.0, 0.01)
    edges, bad = parse_edges({"A": {"ret_pct": 6.5, "horizon_weeks": 13,
                                    "note": "t"}}, H)
    res = compute([{"label": "A", "f_now": 0.02}], {"A": ser(a)}, edges,
                  0.5, 120, H)
    mu = 0.065 / 65
    r = res["rows"][0]
    check(abs(r["f_standalone"] - 0.5 * mu / var(a)) < 1e-12,
          "standalone f = fraction x mu / sigma^2")
    check(abs(r["f_joint"] - r["f_standalone"]) < 1e-12,
          "one pair: joint == standalone")

    # 2. horizon units: 26% over 52w == 6.5% over 13w -----------------------
    e2, _ = parse_edges({"A": {"ret_pct": 26, "horizon_weeks": 52,
                               "note": "t"}}, H)
    r2 = compute([{"label": "A", "f_now": 0.02}], {"A": ser(a)}, e2,
                 0.5, 120, H)["rows"][0]
    check(abs(r2["f_standalone"] - r["f_standalone"]) < 1e-12,
          "same annual edge on two horizons gives the same size")

    # 3. implied edge round-trips: edge := implied  ->  target == current ---
    imp = r["implied_edge_pct"]
    e3, _ = parse_edges({"A": {"ret_pct": imp, "horizon_weeks": H,
                               "note": "t"}}, H)
    r3 = compute([{"label": "A", "f_now": 0.02}], {"A": ser(a)}, e3,
                 0.5, 120, H)["rows"][0]
    check(abs(r3["f_standalone"] - 0.02) < 1e-9,
          "entering the implied edge reproduces today's size")

    # 4. two INDEPENDENT pairs: joint ~ standalone --------------------------
    b = gauss(2, 600, 0.0, 0.015)
    e4, _ = parse_edges({"A": {"ret_pct": 5, "note": "t"},
                         "B": {"ret_pct": 5, "note": "t"}}, H)
    res4 = compute([{"label": "A", "f_now": .02}, {"label": "B", "f_now": .02}],
                   {"A": ser(a), "B": ser(b)}, e4, 0.5, 120, H, shrink=0)
    ra, rb = res4["rows"]
    check(abs(ra["f_joint"] / ra["f_standalone"] - 1) < 0.1
          and abs(rb["f_joint"] / rb["f_standalone"] - 1) < 0.1,
          "independent pairs: joint within 10% of standalone")

    # 5. two NEAR-IDENTICAL pairs (a shared leg): joint must NOT double -----
    #    This is the case the joint solve exists for — DIXON short backing
    #    three longs. Summed standalone counts one bet twice.
    c = [x + y for x, y in zip(a, gauss(3, 600, 0.0, 0.001))]
    e5, _ = parse_edges({"A": {"ret_pct": 5, "note": "t"},
                         "C": {"ret_pct": 5, "note": "t"}}, H)
    res5 = compute([{"label": "A", "f_now": .02}, {"label": "C", "f_now": .02}],
                   {"A": ser(a), "C": ser(c)}, e5, 0.5, 120, H, shrink=0)
    x, y = res5["rows"]
    tot_joint = x["f_joint"] + y["f_joint"]
    check(abs(tot_joint / x["f_standalone"] - 1) < 0.1,
          "correlated pairs: joint TOTAL ~ one standalone, not two "
          f"({tot_joint:.3f} vs {x['f_standalone']:.3f})")

    # 6. a pair with a worse edge on a shared bet flips to a hedge ----------
    e6, _ = parse_edges({"A": {"ret_pct": 8, "note": "t"},
                         "C": {"ret_pct": 2, "note": "t"}}, H)
    res6 = compute([{"label": "A", "f_now": .02}, {"label": "C", "f_now": .02}],
                   {"A": ser(a), "C": ser(c)}, e6, 0.5, 120, H, shrink=0)
    check("flip" in res6["rows"][1]["flags"] and res6["rows"][1]["f_joint"] < 0,
          "the weaker of two near-identical bets comes out negative, flagged")
    check("flip" not in res6["rows"][0]["flags"],
          "...and the stronger one is not flagged (acceptance)")

    # 6b. the budget view keeps today's combined gross exactly ------------
    j4 = res4["joint"]
    gb = sum(abs(r_["f_budget"]) for r_ in res4["rows"])
    check(abs(gb - 0.04) < 1e-12 and abs(j4["effective_fraction"]
          - 0.5 * j4["budget_scale"]) < 1e-15,
          "budget view: Kelly proportions at today's combined gross")

    # 7. short history: standalone only, and it does not truncate the pool --
    short = dict(list(ser(b).items())[-70:])
    res7 = compute([{"label": "A", "f_now": .02}, {"label": "V", "f_now": .02}],
                   {"A": ser(a), "V": short}, e4 | {"V": e4["B"]},
                   0.5, 120, H)
    ra7, rv7 = res7["rows"]
    check("short_history" in rv7["flags"] and "f_joint" not in rv7
          and "f_standalone" in rv7, "short-history pair: standalone only, flagged")
    check(res7["joint"]["n_common"] == 600 and "f_joint" in ra7,
          "...and the rest of the pool keeps its full history")

    # 8. edge refusals — and an acceptance beside them ----------------------
    _, bad = parse_edges({"N": {"ret_pct": 5},
                          "Z": {"ret_pct": 5, "horizon_weeks": 0, "note": "x"},
                          "S": {"ret_pct": "big", "note": "x"}}, H)
    check(len(bad) == 3, "edge without note / zero horizon / non-number refused")
    good, bad = parse_edges({"G": {"ret_pct": -3, "note": "short thesis"}}, H)
    check("G" in good and not bad and good["G"]["horizon_weeks"] == H,
          "a negative edge with a note is accepted, default horizon applied")

    # 9. shrinkage: diagonal untouched, delta in [0,1], pinned value honoured
    S, d = covariance([a, c, b])
    check(0 <= d <= 1 and abs(S[0][0] - var(a)) < 1e-15,
          f"auto shrink delta={d:.3f} in [0,1], diagonal untouched")
    S0, d0 = covariance([a, b], shrink=0.25)
    Sr, _ = covariance([a, b], shrink=0)
    check(d0 == 0.25 and abs(S0[0][1] - 0.75 * Sr[0][1]) < 1e-18,
          "pinned shrink scales off-diagonals only")
    # near-noise pair series should shrink harder than a genuinely correlated one
    _, d_noise = covariance([a, b])
    _, d_corr = covariance([a, c])
    check(d_noise > d_corr, "independent series shrink more than correlated ones")

    # 10. pair_returns: phantom and action dates dropped, gaps widen --------
    bars = {"L": {}, "S": {}}
    vols = {"L": {}, "S": {}}
    px = {"L": [100, 101, 101, 103, 50, 51], "S": [200, 200, 200, 202, 204, 204]}
    ds = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-05",
          "2026-01-06", "2026-01-07"]
    for e in px:
        for d, c in zip(ds, px[e]):
            flat = d == "2026-01-03"
            bars[e][d] = (c, c if flat else c + 1, c if flat else c - 1, c)
            vols[e][d] = 0 if flat else 1000
    del bars["S"]["2026-01-05"], vols["S"]["2026-01-05"]   # S missing a print
    r10, drop = pair_returns(["L"], ["S"], bars, vols,
                             {"L": {"2026-01-06": "test split"}})
    check("2026-01-03" in drop["phantom"] and "2026-01-03" not in r10,
          "phantom session dropped")
    check("2026-01-06" not in r10 and "2026-01-06" in drop["actions"],
          "confirmed-action date dropped")
    check("2026-01-05" not in r10, "date with a missing leg skipped")
    check(abs(r10["2026-01-07"] - (51 / 50 - 1 - 0.0)) < 1e-12,
          "day after an action measured from the post-action close")
    check(abs(r10["2026-01-02"] - 0.01) < 1e-12,
          "ordinary session: long ret minus short ret (acceptance)")

    # 11. edges from the case agents' leans (2026-10-22) -------------------
    def stk(e, j, d):
        return {"entity": e, "name": e.upper(), "juice": j, "horizon": "to Q3",
                "steps": {"price": {"as_of": d}} if j is not None else {}}
    av = {"stocks": [stk("a", 2.9, "2026-10-22"), stk("b", 0.6, "2026-10-21"),
                     stk("c", 1.0, "2026-10-20"), stk("d", None, None)],
          "pairs": [{"name": "A_B", "long": ["a"], "short": ["b"], "pair_juice": 2.3},
                    {"name": "C_D", "long": ["c"], "short": ["d"], "pair_juice": None}]}
    ae = agent_edges(av, H)
    check(set(ae) == {"A_B"} and ae["A_B"]["ret_pct"] == 2.3
          and ae["A_B"]["set_at"] == "2026-10-22",
          "a pair with both leans becomes an edge, dated by its newest lean (acceptance)")
    check("C_D" not in ae, "a pair with a leg missing its lean has NO edge, not a zero one")
    pe, pb = parse_edges(ae, H)
    check("A_B" in pe and not pb and "A +2.9% (2026-10-22)" in pe["A_B"]["note"]
          and "B +0.6%" in pe["A_B"]["note"],
          "the agent edge passes parse_edges with both leans named in its note")

    # 12. the horizon variance ratio (2026-10-06) ---------------------------
    q = 65
    iid = gauss(12, 6000, 0.0, 0.01)
    v_iid = variance_ratio(iid, q)
    se = math.sqrt(vr_se2(6000, q))
    check(abs(v_iid - 1) < 3 * se,
          f"independent increments: VR {v_iid:.3f} within 3se ({se:.3f}) of 1 (acceptance)")
    # mean-reverting increments x_t = e_t - 0.5 e_(t-1): long-run VR is
    # (1-0.5)^2 / (1+0.25) = 0.2 — must be told apart from 1
    rg = random.Random(13)
    e = [rg.gauss(0, 0.01) for _ in range(6001)]
    mr = [e[t] - 0.5 * e[t - 1] for t in range(1, 6001)]
    v_mr = variance_ratio(mr, q)
    check(abs(v_mr - 0.2) < 0.08 and v_mr < 1 - 3 * se,
          f"mean-reverting increments: VR {v_mr:.3f} ~ 0.2, rejected as 1")
    check(variance_ratio(iid[:129], q) is None and variance_ratio(iid[:130], q) is not None,
          "VR needs two full windows: refused at 2q-1, given at 2q")

    # sizing: a ratio r divides the variance, so size x 1/r and implied edge x r
    e12, _ = parse_edges({"A": {"ret_pct": 6.5, "note": "t"}}, H)
    base = compute([{"label": "A", "f_now": 0.02}], {"A": ser(a)}, e12, 0.5, 120, H)["rows"][0]
    half = compute([{"label": "A", "f_now": 0.02}], {"A": ser(a)}, e12, 0.5, 120, H,
                   vr={"A": 0.5})["rows"][0]
    check(abs(half["f_standalone"] / base["f_standalone"] - 2) < 1e-12
          and abs(half["f_joint"] / base["f_joint"] - 2) < 1e-12,
          "ratio 0.5 doubles the standalone AND joint size exactly")
    check(abs(half["implied_edge_pct"] / base["implied_edge_pct"] - 0.5) < 1e-12,
          "...and halves the implied edge")
    check(abs(half["sigma_ann_pct"] - base["sigma_ann_pct"]) < 1e-12
          and abs(half["sigma_h_pct"] / base["sigma_h_pct"] - math.sqrt(0.5)) < 1e-12,
          "the daily σ column is unchanged; the horizon σ scales by √r")
    one = compute([{"label": "A", "f_now": 0.02}], {"A": ser(a)}, e12, 0.5, 120, H,
                  vr={"A": 1.0})["rows"][0]
    check(one["f_standalone"] == base["f_standalone"] and one["f_joint"] == base["f_joint"],
          "ratio 1.0 reproduces the pre-2026-10-06 sizes exactly (regression)")
    # one ratio for every pair: joint PROPORTIONS — the budget column — do not move
    r4a = compute([{"label": "A", "f_now": .02}, {"label": "B", "f_now": .02}],
                  {"A": ser(a), "B": ser(b)}, e4, 0.5, 120, H, shrink=0)
    r4b = compute([{"label": "A", "f_now": .02}, {"label": "B", "f_now": .02}],
                  {"A": ser(a), "B": ser(b)}, e4, 0.5, 120, H, shrink=0,
                  vr={"A": 0.66, "B": 0.66})
    check(all(abs(x["f_budget"] - y["f_budget"]) < 1e-12 for x, y in zip(r4a["rows"], r4b["rows"]))
          and abs(r4b["rows"][0]["f_joint"] / r4a["rows"][0]["f_joint"] - 1 / 0.66) < 1e-9,
          "a common ratio scales joint sizes by 1/r and leaves the allocation untouched")

    # pooling: noise-only dispersion -> everyone takes the pool ...
    T_ = 4000
    s_ = math.sqrt(vr_se2(T_, q))
    noisy = {f"P{i}": (0.66 + s_ * z, T_) for i, z in
             enumerate([-1.0, -0.5, 0.0, 0.5, 1.0, -0.2, 0.3, 0.1])}
    hr = horizon_ratios(noisy, q, 750)
    check(hr["tau2"] == 0 and all(p_["w"] == 0 and abs(p_["ratio"] - hr["pool"]) < 1e-12
                                  for p_ in hr["pairs"].values()),
          "dispersion no wider than noise: tau2 = 0, every pair takes the pool")
    # ... a genuinely different pair keeps (most of) its own
    spread_ = dict(noisy, OUT=(1.6, T_), OUT2=(0.2, T_))
    hr2 = horizon_ratios(spread_, q, 750)
    check(hr2["tau2"] > 0 and hr2["pairs"]["OUT"]["w"] > 0.5
          and hr2["pairs"]["OUT"]["ratio"] > hr2["pool"],
          "real between-pair dispersion: an outlier keeps most of its own ratio")
    hr3 = horizon_ratios(dict(noisy, NEW=(None, 70), YOUNG=(1.4, 500)), q, 750)
    check(hr3["pairs"]["NEW"]["basis"] == "pooled_short"
          and hr3["pairs"]["YOUNG"]["ratio"] == hr3["pool"],
          "a short-history pair takes the pool, flagged, whatever its own estimate")
    check(horizon_ratios({"A": (None, 50)}, q, 750)["pairs"]["A"]["ratio"] == 1.0
          and horizon_ratios(noisy, q, 750, pin=1.0)["pairs"]["P0"]["ratio"] == 1.0,
          "no qualifying pair -> 1.0 stated as basis 'none'; a pin overrides")

    print(f"\nkelly selftest: {ok} checks pass")


def _report() -> None:
    sys.path.insert(0, str(REPO / "packages" / "api"))
    import engine
    bk = engine.book_view()
    k = bk.get("kelly") or {}
    if k.get("error"):
        print("kelly error:", k["error"])
        return
    j = k["joint"]
    print(f"Kelly x{k['fraction']}  window {k['window'][0]}..{k['window'][1]}"
          f"  horizon {k['horizon_weeks']:g}w  shrink delta {j.get('delta')}"
          f"  common {j.get('n_common')}  edges {k['n_edges']}")
    print(f"{'pair':16s} {'n':>4s} {'sig%':>6s} {'gr now':>7s} "
          f"{'imp%':>7s} {'impJ%':>7s} {'edge':>6s} {'grSA':>7s} {'grJ':>7s} {'grBud':>7s} flags")
    for r in k["rows"]:
        e = r.get("edge") or {}
        print(f"{r['label']:16s} {r['n_sessions']:4d} "
              f"{r.get('sigma_ann_pct', float('nan')):6.1f} "
              f"{r['gross_now_pct']:6.2f}% "
              f"{r.get('implied_edge_pct', float('nan')):+7.2f} "
              f"{r.get('implied_edge_joint_pct', float('nan')):+7.2f} "
              f"{e.get('ret_pct', float('nan')):+6.1f} "
              f"{r.get('gross_standalone_pct', float('nan')):6.1f}% "
              f"{r.get('gross_joint_pct', float('nan')):6.1f}% "
              f"{r.get('gross_budget_pct', float('nan')):6.2f}% "
              f"{','.join(r['flags'])}")
    if j.get("members"):
        print(f"joint target: gross {j['gross_pct']:.1f}%  exp {j['exp_ret_ann_pct']:+.1f}%/yr"
              f"  vol {j['vol_ann_pct']:.1f}%  growth {j['growth_ann_pct']:+.1f}%/yr")
        print(f"today, same edges: exp {j['now_exp_ret_ann_pct']:+.2f}%/yr"
              f"  vol {j['now_vol_ann_pct']:.2f}%  growth {j['now_growth_ann_pct']:+.2f}%/yr"
              f"  -> today is {j.get('effective_fraction', float('nan')):.4f}x Kelly")
    for s in k["skipped"]:
        print("skipped:", s)
    for b in k["bad_edges"]:
        print("REFUSED edge:", b)
    for u in k["unknown_edges"]:
        print("edge names no dictated pair:", u)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        _report()
