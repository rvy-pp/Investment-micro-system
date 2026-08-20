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


def after_corporate_action(rows):
    """Drop history before the last step change — a percentile or z against a
    pre-demerger series compares two different companies."""
    for (d0, c0), (d1, c1) in zip(reversed(rows[:-1]), reversed(rows[1:])):
        if c0 and abs(c1 / c0 - 1) >= JUMP:
            return d1, (c1 / c0 - 1) * 100
    return None, None


def quarter_average(conn, link: str, start: str, end: str) -> float | None:
    r = conn.execute(
        "SELECT AVG(close) FROM prices WHERE entity_id=? AND date BETWEEN ? AND ?",
        (link, start, end)).fetchone()
    return r[0] if r and r[0] is not None else None


def spot_multiple_series(conn, ent: dict, fin: dict, units: dict,
                         qstart: str, qend: str, usdinr: float):
    """[(date, multiple, spot_ebitda)] — EV / spot-marked EBITDA, daily."""
    shares, base = fin.get("shares_outstanding"), fin.get("base_ebitda")
    if not shares or not base:
        return [], None, None, "no shares_outstanding or base_ebitda"

    net_debt = fin.get("net_debt") or 0.0
    px = price_series(conn, ent["id"])
    cut, cut_pct = after_corporate_action(px)
    if cut:
        px = [r for r in px if r[0] >= cut]
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
            conn, ent, f, units, a.qstart, a.qend, usdinr)
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
