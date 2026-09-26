"""Kelly sizing for the book's dictated pairs.

PM, 2026-09-25: *"Let's start with applying kelly criterion to the book."*
Settled the same day: the EDGE is the PM's, entered per pair ON THE BOOK TAB
(PM, same day: "just put returns and comments then and there") and stored
append-only in `book_kelly_edges`; the RISK is measured here from `prices`;
the default fraction is HALF Kelly and the horizon is fixed at 13 weeks.

The page recomputes live in the browser as a return is typed — `calc` in the
payload carries the shrunk covariance, the per-pair variances and today's
sizes at full precision, and app.html's kellyCalc() mirrors compute() here.
Saving POSTs /api/kelly_edge -> save_edge(). The edges briefly lived in
specs/book.yaml kelly.edges; that key is no longer read, and a non-empty one
is reported rather than silently ignored.

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

So the edge is the PM's view, typed in, with a NOT-EMPTY `note` beside it —
the book-side cousin of invariants 1 and 2 (no number without provenance). An
edge with no note is refused, not defaulted.

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
            "horizon_weeks": 13, "shrink": "auto"}


# ---------------------------------------------------------------- spec ----

def _spec() -> dict:
    import yaml
    cfg = yaml.safe_load(SPEC.read_text(encoding="utf-8")) or {}
    k = {**DEFAULTS, **(cfg.get("kelly") or {})}
    k["spec_edges"] = k.pop("edges", None) or {}
    return k


def _book_conn():
    import book_io
    return book_io.connect()          # runs the DDL, so the table exists


def stored_edges(conn) -> dict:
    """{pair: {ret_pct, horizon_weeks, note, set_at}} — latest row per pair,
    cleared pairs (ret_pct NULL) omitted."""
    out = {}
    for r in conn.execute(
            "SELECT pair, ret_pct, horizon_weeks, note, set_at FROM "
            "book_kelly_edges ORDER BY id"):
        if r[1] is None:
            out.pop(r[0], None)
        else:
            out[r[0]] = {"ret_pct": r[1], "horizon_weeks": r[2],
                         "note": r[3], "set_at": r[4]}
    return out


def save_edge(pair: str, ret_pct, note: str, known: set,
              conn=None) -> dict:
    """Validate and append one edge. ret_pct None/'' clears it.

    Same refusals as parse_edges — a note is required to SET an edge — plus
    the pair must be a dictated one TODAY: an edge filed under a pair name
    that no longer exists would sit in the table reading as live.
    """
    if pair not in known:
        return {"error": f"{pair!r} is not a dictated pair"}
    note = (note or "").strip()
    H = float(_spec()["horizon_weeks"])
    if ret_pct in (None, ""):
        # the clear row must not carry the old thesis as if it were current
        ret, note = None, "cleared on the Book tab"
    else:
        ok, bad = parse_edges({pair: {"ret_pct": ret_pct, "note": note,
                                      "horizon_weeks": H}}, H)
        if bad:
            return {"error": bad[0]}
        ret = ok[pair]["ret_pct"]
    own = conn is None
    conn = conn or _book_conn()
    try:
        conn.execute(
            "INSERT INTO book_kelly_edges (pair, ret_pct, horizon_weeks, "
            "note, set_at) VALUES (?,?,?,?,?)",
            (pair, ret, H, note,
             dt.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"ok": True, "pair": pair, "ret_pct": ret, "note": note}


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
            shrink="auto") -> dict:
    """The arithmetic, separated from the store so the selftest can drive it.

    pairs: [{label, f_now}] — f_now is CURRENT notional per side / NAV.
    rets:  {label: {date: r}}.
    edges: parse_edges output.
    """
    H = horizon_weeks * SESSIONS_PER_WEEK
    rows = {}
    for p in pairs:
        lab, r = p["label"], rets.get(p["label"]) or {}
        xs = list(r.values())
        n = len(xs)
        row = {"label": lab, "n_sessions": n, "f_now": p["f_now"],
               "flags": list(p.get("flags") or [])}
        if n >= 3:
            m = sum(xs) / n
            var = sum((x - m) ** 2 for x in xs) / (n - 1)
            row["sigma_d"] = math.sqrt(var)
            row["sigma_ann_pct"] = math.sqrt(var * SESSIONS_PER_YEAR) * 100
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
          spec: dict | None = None) -> dict:
    """The /api/book `kelly` block, from engine.book_view's dictated view."""
    from corporate_actions import CONFIRMED_ACTIONS
    spec = spec or _spec()
    fraction = float(spec["fraction"])
    H = float(spec["horizon_weeks"])
    econn = _book_conn()
    try:
        raw = stored_edges(econn)
    finally:
        econn.close()
    edges, bad = parse_edges(raw, H)
    if spec.get("spec_edges"):
        bad.append("specs/book.yaml kelly.edges is no longer read — enter "
                   "edges on the Book tab (" + ", ".join(spec["spec_edges"]) + ")")
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
    finally:
        if own:
            conn.close()

    pairs, rets, skipped = [], {}, []
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

    out = compute(pairs, rets, edges, fraction, int(spec["min_sessions"]), H,
                  spec.get("shrink", "auto"))
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
        "min_sessions": int(spec["min_sessions"]),
        "shrink": spec.get("shrink", "auto"),
        "n_edges": len([e for e in edges if e in known]),
        "edges_set_at": {k_: raw[k_]["set_at"] for k_ in edges if k_ in raw},
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

    # 11. the edge store: append-only, latest wins, clear removes ---------
    import book_io
    mem = sqlite3.connect(":memory:")
    mem.executescript(book_io.DDL)
    known = {"HZ_VEDL", "JSTL_TATA"}
    check("error" in save_edge("TYPO", 5, "x", known, mem),
          "save refuses a pair that is not dictated")
    check("error" in save_edge("HZ_VEDL", 5, "  ", known, mem),
          "save refuses an edge with no comment")
    check(save_edge("HZ_VEDL", 5, "first view", known, mem).get("ok")
          and save_edge("HZ_VEDL", 7, "revised", known, mem).get("ok")
          and stored_edges(mem)["HZ_VEDL"]["ret_pct"] == 7,
          "a revised edge replaces the live one (acceptance)")
    save_edge("HZ_VEDL", None, "", known, mem)
    n_rows = mem.execute("SELECT COUNT(*) FROM book_kelly_edges").fetchone()[0]
    check("HZ_VEDL" not in stored_edges(mem) and n_rows == 3,
          "clearing removes the live edge and keeps the history")

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
