"""P3 valuation — SPOT EV/EBITDA against the name's own history.

    EV            = market cap + net debt
    spot EBITDA   = the base quarter's EBITDA, RE-MARKED at today's commodity
                    prices, annualised
    multiple      = EV / spot EBITDA
    score         = how far that sits from the name's own usual level

WHY SPOT AND NOT REPORTED. Reported EBITDA describes a quarter that is already
over. For a commodity name where alumina moved 5% and LME moved -4% since that
quarter closed, last quarter's EBITDA is not what you are buying. Spot-marking
answers the question actually being asked: at today's prices, what am I paying?

HOW THE RE-MARK WORKS — it reuses the bridge, so there is one arithmetic in the
system, not two:

    spot_ebitda(d) = base_ebitda + bridge(prices at d  vs  base-quarter average)

The base quarter's AVERAGE price is the reference, not its closing price: the
reported EBITDA was earned across the quarter, so the average is what produced
it.

THE HISTORY IS SPOT-MARKED TOO, and this is the part that is easy to get wrong.
Comparing today's SPOT multiple to a history of REPORTED multiples compares two
different measures and the percentile is meaningless. Every historical date is
re-marked with the same arithmetic, so the comparison is like with like.

SCORED ON Z, NOT PERCENTILE. The PM's test is "drastically different from usual"
— that is a question about MAGNITUDE of deviation, which a percentile cannot
express: the 5th percentile is the same rank whether it is half a standard
deviation cheap or three. z is unbounded, so it goes through the hill curve,
consistent with the rule used everywhere: unbounded quantities get squashed,
already-bounded ones (probabilities, percentiles) map linearly.

    cheap vs own history (negative z) -> HIGH score

Usage:
    python packages/score/valuation.py --peer-group aluminium_primary
    python packages/score/valuation.py --peer-group aluminium_primary --detail
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bridge import load_specs, run_bridge, _series_in_store  # noqa: E402
from scoring import score as to_score, solve_k  # noqa: E402
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "core"))
from corporate_actions import confirmed_cut  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
CR = 1e7
JUMP = 0.15                 # single-session move that means a corporate action

# "one standard deviation cheap reads as a 4" — the same anchor idea as the
# EBITDA curve, stated in the desk's own terms rather than as a constant.
Z_ANCHOR, SCORE_ANCHOR, P = 1.0, 4.0, 1.5

MIN_SPOT_EBITDA_FRAC = 0.25  # below this the multiple explodes and is not a
                             # valuation signal, it is a distress signal


def price_series(conn, eid: str) -> list[tuple[str, float]]:
    return conn.execute(
        "SELECT date, close FROM prices WHERE entity_id=? ORDER BY date",
        (eid,)).fetchall()


def after_corporate_action(rows, eid: str):
    """Drop history before the last CONFIRMED action — a z against a pre-demerger
    series compares two different companies.

    CONFIRMED, not "any 15% step". This truncated on the latest jump of any kind
    until 2026-08-25, which cut five of nine names' valuation history at a REAL
    market move: sail and nalco at the 2024-06-04 election selloff, jindal_steel
    at the 2022-05-21 steel export duty, jsw_steel and hindalco at the COVID
    crash and its bounce. sail was scoring a z over 511 days against a spec that
    says lookback_days: 1260.

    api/tape.py already had this right and said why in its own comment. The two
    files answered the same question opposite ways on the same data; the
    allow-list now lives in ONE place, core/corporate_actions.py, and both import
    it.
    """
    return confirmed_cut(eid, rows, JUMP)


def spec_lookback(peer_group: str, default: int = 1260) -> int:
    """`pillar_3.lookback_days` for a peer group, from specs/sectors/*.yaml.

    Read from the spec rather than hardcoded, because it was hardcoded nowhere
    and read nowhere — the value sat in three sector specs and no code consulted
    any of them.

    !! THE UNIT IS AMBIGUOUS IN THE SPEC AND THIS IS NOT RESOLVED SILENTLY.
    !! aluminium_primary.yaml annotates it `lookback_days: 1260  # ~5y`, and 1260
    !! is exactly 5 years of TRADING days (252 x 5). As CALENDAR days it is 3.45
    !! years. Those are different reference windows and the comment only fits one
    !! of them.
    !!
    !! Calendar days is used, because CLAUDE.md's standing gotcha is explicit —
    !! "Windows are CALENDAR DAYS, not row counts" — and taking 1260 ROWS off a
    !! series is exactly the row-count bug that rule exists to stop. The cost is
    !! that the delivered window is ~3.45y against a comment claiming ~5y.
    !!
    !! So either the 1260 or the `# ~5y` is wrong, and it is a PM call which. To
    !! get a genuine 5 years, set lookback_days: 1825. Do NOT "fix" this by
    !! switching to row counts.
    """
    f = REPO / "specs" / "sectors"
    try:
        import yaml
        for path in sorted(f.glob("*.yaml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            groups = doc.get("peer_groups") or [doc.get("peer_group")]
            if peer_group in (groups or []):
                v = ((doc.get("pillar_3") or doc.get("layer2", {}).get("valuation")
                      or {}).get("lookback_days"))
                if v:
                    return int(v)
    except Exception:
        pass
    return default


def apply_lookback(rows, lookback_days: int | None):
    """Keep the most recent `lookback_days` CALENDAR days.

    `lookback_days: 1260` is written in every sector spec's pillar_3 block and
    was read by NOTHING — the same shape as `effective_from`, which CLAUDE.md
    flags as written everywhere and read nowhere. So the reference window was
    "all clean history", which is not what the spec says and differs per name by
    however far back its listing goes: tata_steel was measured over 1,767 days
    and hindustan_zinc over 566, with no rule choosing either.

    Calendar days, not row counts, per the CLAUDE.md gotcha: the store mixes
    daily equities with monthly series, so N rows back is N months on a monthly
    one.
    """
    if not lookback_days or not rows:
        return rows
    import datetime as _dt
    last = _dt.date.fromisoformat(rows[-1][0])
    floor = (last - _dt.timedelta(days=int(lookback_days))).isoformat()
    return [r for r in rows if r[0] >= floor]


def quarter_average(conn, link: str, start: str, end: str) -> float | None:
    r = conn.execute(
        "SELECT AVG(close) FROM prices WHERE entity_id=? AND date BETWEEN ? AND ?",
        (link, start, end)).fetchone()
    return r[0] if r and r[0] is not None else None


def spot_multiple_series(conn, ent: dict, fin: dict, units: dict,
                         qstart: str, qend: str, usdinr: float,
                         lookback_days: int | None = 1260,
                         as_of: str | None = None):
    """[(date, multiple, spot_ebitda)] — EV / spot-marked EBITDA, daily."""
    # TWO DENOMINATORS, NOT ONE, and the distinction is load-bearing.
    #
    # `base_ebitda` is the ECONOMICS denominator: it must match the entity's
    # spec lines, because pct_of_ebitda asks "how big is this move against the
    # earnings the lines describe". For Hindalco those lines are India aluminium
    # UPSTREAM only, so base_ebitda is the upstream segment.
    #
    # Valuation asks a different question. `ev` below is consolidated by
    # construction — all the shares and all the net debt — so dividing it by a
    # SEGMENT EBITDA produces a multiple for a company that does not exist. When
    # Hindalco's base_ebitda was corrected from a consolidated guess (32,000) to
    # the upstream segment (29,560) on 2026-08-24, this silently re-rated it from
    # 11.21x to 12.29x on no news at all — a pure denominator artefact.
    #
    # So a name whose economics are specced at segment level carries an explicit
    # `valuation_ebitda` for the whole entity. Everyone else omits it and falls
    # back to base_ebitda, where the two questions have the same answer.
    shares = fin.get("shares_outstanding")
    base = fin.get("valuation_ebitda") or fin.get("base_ebitda")
    if not shares or not base:
        return [], None, None, "no shares_outstanding or base_ebitda"

    net_debt = fin.get("net_debt") or 0.0
    px = price_series(conn, ent["id"])
    # AS-OF FIRST, THEN THE LOOKBACK. Order matters and getting it backwards is a
    # backfill-only bug that would never show on a single date. apply_lookback
    # anchors on the LAST row it is given, so trimming to as_of afterwards (which
    # is what run_scores did) anchored every historical window on TODAY and then
    # filtered it down — leaving a 2-year-old as_of with a nearly empty series and
    # a z computed off a handful of rows. Trim to as_of here and the window is the
    # lookback ending at as_of, which is what it must be.
    if as_of:
        px = [r for r in px if r[0] <= as_of]
    cut, cut_pct = after_corporate_action(px, ent["id"])
    if cut:
        px = [r for r in px if r[0] >= cut]
    px = apply_lookback(px, lookback_days)
    if len(px) < 20:
        return [], cut, cut_pct, "too little clean history"

    links = {ln["price_link"] for ln in
             (ent.get("outputs") or []) + (ent.get("inputs") or [])
             if ln.get("price_link")}
    qavg = {l: quarter_average(conn, l, qstart, qend) for l in links}
    available = _series_in_store()

    # driver prices by date, carried forward so a monthly series does not blank
    drv: dict[str, dict[str, float]] = {}
    for l in links:
        drv[l] = dict(price_series(conn, l))

    def price_at(link: str, d: str):
        s = drv.get(link) or {}
        ks = [k for k in s if k <= d]
        return s[max(ks)] if ks else None

    out = []
    for d, close in px:
        shocks = {}
        for l in links:
            p_now, p_ref = price_at(l, d), qavg.get(l)
            if p_now is not None and p_ref:
                shocks[l] = p_now - p_ref
        r = run_bridge(ent, shocks, units, base, usdinr, available | set(shocks))
        spot = base + r["d_ebitda_cr"]
        if spot < base * MIN_SPOT_EBITDA_FRAC:
            continue                      # distress, not a valuation reading
        ev = close * shares / CR + net_debt
        out.append((d, ev / spot, spot))
    return out, cut, cut_pct, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer-group", default="aluminium_primary")
    # Defaults come from base_financials.yaml, never from a literal here — the
    # EBITDA print and its price reference window describe the same quarter and
    # must not be settable independently by accident.
    _bq = (load_specs()[2].get("base_quarter") or {})
    ap.add_argument("--qstart", default=_bq.get("start"),
                    help=f"base quarter start (spec: {_bq.get('label','?')})")
    ap.add_argument("--qend", default=_bq.get("end"))
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()

    entities, units, fin = load_specs()
    fins = fin["companies"]
    usdinr = fin["usdinr"]
    conn = sqlite3.connect(DB)
    k = solve_k("hill", Z_ANCHOR, SCORE_ANCHOR, P)

    print(f"{a.peer_group} · spot EV/EBITDA · base quarter {a.qstart}..{a.qend}\n")
    print(f"{'entity':16}{'spot EBITDA':>12}{'vs base':>9}{'EV':>10}"
          f"{'mult':>7}{'mean':>7}{'sd':>6}{'z':>7}{'P3':>7}  basis")
    print("-" * 104)

    for ent in sorted(entities.values(), key=lambda e: e["id"]):
        if ent.get("peer_group") != a.peer_group:
            continue
        eid = ent["id"]
        f = fins.get(eid, {})
        ser, cut, cut_pct, err = spot_multiple_series(
            conn, ent, f, units, a.qstart, a.qend, usdinr,
            lookback_days=spec_lookback(a.peer_group))
        if err or not ser:
            print(f"{eid:16}{'—':>12}{'—':>9}{'—':>10}{'—':>7}{'—':>7}"
                  f"{'—':>6}{'—':>7}{'—':>7}  WITHHELD: {err or 'no series'}")
            continue

        mults = [m for _, m, _ in ser]
        d_now, m_now, spot_now = ser[-1]
        mean = statistics.fmean(mults)
        sd = statistics.pstdev(mults) or 1e-9
        z = (m_now - mean) / sd
        score = to_score(-z, k, "hill", P)     # cheap (negative z) scores HIGH
        base = f.get("base_ebitda", 0)
        ev_now = m_now * spot_now
        note = []
        if cut:
            note.append(f"from {cut} ({cut_pct:+.0f}% corp action)")
        if not f.get("net_debt"):
            note.append("EV~mcap")
        print(f"{eid:16}{spot_now:>12,.0f}{spot_now/base-1:>8.0%}"
              f"{ev_now:>10,.0f}{m_now:>7.2f}{mean:>7.2f}{sd:>6.2f}"
              f"{z:>+7.2f}{score:>7.2f}  {len(ser)}d"
              + ("  " + ", ".join(note) if note else ""))

        if a.detail:
            lo, hi = min(mults), max(mults)
            print(f"{'':16}range {lo:.2f}–{hi:.2f}; spot EBITDA is "
                  f"{spot_now/base-1:+.0%} vs the reported base of {base:,.0f}cr")

    print(f"\nz measured against each name's OWN spot-marked history, so today and")
    print(f"history are the same measure. Anchor: z = -{Z_ANCHOR:g} (one sd cheap)"
          f" reads {SCORE_ANCHOR:.1f}.")
    print("Hill curve because z is unbounded — 'drastically different' is a")
    print("question about magnitude, which a percentile cannot express.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
