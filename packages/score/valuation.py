"""P3 valuation — where a name trades against its OWN history.

    commodities        EV/EBITDA = (price x shares + net_debt) / base_ebitda
    IT / EMS / autos   P/E       = price / eps
The metric is a per-company spec choice (`valuation_metric`), because the right
multiple differs by sector and forcing one on all of them would be wrong for
half the book.

WHY COMPUTED RATHER THAN FETCHED. Wind carries Indian prices and share counts
but no fundamentals — pe_ttm, pb_lf and every balance-sheet field come back
empty for .BO tickers while working normally for a Chinese control, and Yahoo's
fundamentals endpoint now 401s. So the multiple is built from price x shares,
which has a genuine advantage: it moves DAILY, so it has a history to take a
percentile against. A broker-quoted multiple is episodic and cannot.

SCORED LINEARLY, NOT THROUGH THE HILL CURVE. The rule across the system: an
UNBOUNDED quantity (EBITDA impact) goes through the hill squash; an ALREADY
BOUNDED one (a probability, a percentile) maps linearly, because squashing a
number that is already in the right units distorts it. Same reasoning as P4.

    cheap vs own history  -> HIGH score (attractive to own)
    expensive             -> LOW score
    score = 3 + 2 * (50 - percentile) / 50

CAVEAT ON HISTORY LENGTH: the percentile is only as meaningful as the price
history behind it. Six months is thin — it says "cheap for this year", not
"cheap for this cycle". Reported alongside the score, never hidden.

Usage:
    python packages/score/valuation.py --peer-group aluminium_primary
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bridge import load_specs  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
CR = 1e7


JUMP = 0.15   # a single-session move this large is a corporate action


def history_start(rows: list[tuple[str, float]]) -> tuple[str | None, float | None]:
    """First date AFTER the most recent corporate action, if any.

    A valuation percentile against contaminated history is worse than no
    percentile: VEDL's raw multiple range came out 4.50-14.00 because the
    pre-demerger price sits in the same series as the post-demerger one, on the
    same EBITDA base. That put it at the 10th percentile — "historically very
    cheap" — when the comparison was simply to a different, four-businesses-
    larger company. Percentiles must run only on the current entity.
    """
    for (d0, c0), (d1, c1) in zip(reversed(rows[:-1]), reversed(rows[1:])):
        if c0 and abs(c1 / c0 - 1) >= JUMP:
            return d1, (c1 / c0 - 1) * 100
    return None, None


def multiple_series(conn, eid: str, fin: dict):
    """Daily valuation multiple, truncated at the last corporate action.

    Returns (series, cut_date, cut_pct).
    """
    shares = fin.get("shares_outstanding")
    ebitda = fin.get("base_ebitda")
    metric = fin.get("valuation_metric", "ev_ebitda")
    if not shares or not ebitda:
        return [], None, None
    net_debt = fin.get("net_debt") or 0.0

    rows = conn.execute(
        "SELECT date, close FROM prices WHERE entity_id=? ORDER BY date",
        (eid,)).fetchall()
    cut, cut_pct = history_start(rows)
    if cut:
        rows = [r for r in rows if r[0] >= cut]
    out = []
    for d, px in rows:
        mcap_cr = px * shares / CR
        if metric == "ev_ebitda":
            out.append((d, (mcap_cr + net_debt) / ebitda))
        elif metric == "pe":
            eps = fin.get("eps")
            if eps:
                out.append((d, px / eps))
    return out, cut, cut_pct


def percentile(series: list[float], value: float) -> float:
    if not series:
        return 50.0
    below = sum(1 for s in series if s < value)
    return 100.0 * below / len(series)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--peer-group", default="aluminium_primary")
    a = ap.parse_args()

    entities, _units, fin = load_specs()
    fins = fin["companies"]
    conn = sqlite3.connect(DB)

    print(f"{a.peer_group}\n")
    print(f"{'entity':16}{'metric':>11}{'now':>9}{'min':>8}{'max':>8}"
          f"{'%ile':>7}{'P3':>7}  basis")
    print("-" * 88)

    any_row = False
    for ent in sorted(entities.values(), key=lambda e: e["id"]):
        if ent.get("peer_group") != a.peer_group:
            continue
        eid = ent["id"]
        f = fins.get(eid, {})
        ser, cut, cut_pct = multiple_series(conn, eid, f)
        if not ser:
            why = ("no shares_outstanding" if not f.get("shares_outstanding")
                   else "no base_ebitda")
            print(f"{eid:16}{f.get('valuation_metric','—'):>11}"
                  f"{'—':>9}{'—':>8}{'—':>8}{'—':>7}{'—':>7}  WITHHELD: {why}")
            continue
        any_row = True
        vals = [v for _, v in ser]
        now = vals[-1]
        pct = percentile(vals, now)
        score = 3.0 + 2.0 * (50.0 - pct) / 50.0
        approx = "" if f.get("net_debt") else "  EV~mcap (net_debt unset)"
        cutnote = (f"  from {cut} after {cut_pct:+.0f}% corp action" if cut else "")
        print(f"{eid:16}{f.get('valuation_metric','—'):>11}{now:>9.2f}"
              f"{min(vals):>8.2f}{max(vals):>8.2f}{pct:>7.0f}{score:>7.2f}"
              f"  {len(vals)}d{cutnote}{approx}")

    if any_row:
        print("\nCheap vs own history scores HIGH. Linear, not the hill curve —")
        print("a percentile is already bounded, so squashing it again distorts it.")
        print("History is ~6 months: this says 'cheap for this year', not 'cheap")
        print("for this cycle'. Net debt at 0 understates EV, so a levered name's")
        print("multiple reads too cheap until it is filled in on the Inputs tab.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
