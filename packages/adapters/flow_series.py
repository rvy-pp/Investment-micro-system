"""Cross-asset closes for the Flows F1 regime read -> flow_series table.

WHAT THIS IS. The five series specs/flows.yaml names (S&P 500, SOX, IGV,
gold, US 10Y yield), fetched through yahoo_prices.fetch() — which carries
the identity guard and the close-pairing fix — and written to the dedicated
flow_series table.

WHAT THIS IS NOT. It never writes `prices`. Anything in `prices` becomes
bridge-shockable and a candidate for the store clock; these series are the
tape the regime is read off, not costs or realisations. Same wall as
morning_markets.py, one layer down (these persist, because the regime needs
ten years of history and a transition matrix cannot be recomputed from a
JSON that only holds this morning).

THE PARTIAL-DAY GUARD. Yahoo's chart API includes the CURRENT session as a
row dated today (UTC) while it is still trading — verified 2026-09-02, a
2026-09-02 row arrived during India daytime, hours before the US open.
Classifying it would stamp a regime on a day that has not happened. Any row
dated >= today (UTC) is dropped. Consequence: the newest state is always
the last COMPLETED US session — T-1 from India's morning, which is exactly
the read "how did overnight set us up".

Usage:
    python packages/adapters/flow_series.py --load                # 3mo top-up
    python packages/adapters/flow_series.py --load --range 10y    # backfill
    python packages/adapters/flow_series.py --print               # newest rows
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402
from yahoo_prices import fetch  # noqa: E402

DB = REPO / "data" / "ims.db"
SPEC = REPO / "specs" / "flows.yaml"


def load_spec() -> dict:
    with open(SPEC, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load(rng: str = "3mo") -> int:
    spec = load_spec()
    today_utc = dt.datetime.now(dt.timezone.utc).date().isoformat()
    captured = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    conn = sqlite3.connect(DB)
    total = 0
    # india_series ride along: display/evidence inputs for the weekly panel,
    # never regime-state inputs. Same table, same partial-day guard.
    todo = dict(spec["series"]) | dict(spec.get("india_series") or {})
    for sid, s in todo.items():
        try:
            rows = fetch(s["symbol"], rng=rng, name_pattern=s["name_pattern"])
        except Exception as e:
            print(f"  FAIL {sid:8s} {s['symbol']:6s} {e}")
            continue
        # Partial-day guard — see module docstring.
        live = [r for r in rows if r[0] >= today_utc]
        rows = [r for r in rows if r[0] < today_utc]
        n = 0
        for d, close in rows:
            cur = conn.execute(
                "INSERT INTO flow_series (series_id, date, close, source, captured_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT (series_id, date) DO UPDATE SET "
                "close=excluded.close, captured_at=excluded.captured_at",
                (sid, d, close, "yahoo", captured))
            n += cur.rowcount
        conn.commit()
        total += n
        dropped = f"  (dropped {len(live)} live partial row)" if live else ""
        print(f"  ok   {sid:8s} {s['symbol']:6s} {len(rows):5d} rows "
              f"{rows[0][0]} .. {rows[-1][0]}{dropped}")
    conn.close()
    return total


def show() -> None:
    conn = sqlite3.connect(DB)
    for sid, first, last, n in conn.execute(
            "SELECT series_id, MIN(date), MAX(date), COUNT(*) "
            "FROM flow_series GROUP BY series_id ORDER BY series_id"):
        close = conn.execute(
            "SELECT close FROM flow_series WHERE series_id=? AND date=?",
            (sid, last)).fetchone()[0]
        print(f"  {sid:8s} {n:5d} rows  {first} .. {last}  last {close:,.2f}")
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--range", default="3mo", help="Yahoo range: 3mo, 1y, 10y")
    ap.add_argument("--print", action="store_true", dest="show")
    args = ap.parse_args()
    if args.load:
        load(args.range)
    if args.show or not args.load:
        show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
