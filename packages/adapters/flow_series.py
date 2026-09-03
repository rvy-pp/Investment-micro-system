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

# NSE's own EOD index history. Answers plain urllib with NO cookie dance
# (verified 2026-09-03) — the same free ride as nseix's market-rate API, and
# the same caveat: if it starts returning 403, check whether a token dance
# became a prerequisite before assuming the endpoint moved. It exists here
# because YAHOO'S DAILY HISTORY FOR NSE SECTORALS STOPPED AT 2026-07-17 while
# its live quote kept updating — a series that looks alive and has quietly
# stopped accruing history, the exact shape the freshness layer exists for.
NSE_HIST = ("https://www.nseindia.com/api/historicalOR/indicesHistory"
            "?indexType={q}&from={frm}&to={to}")
NSE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _nse_history(nse_name: str, frm: "dt.date", to: "dt.date") -> list[tuple[str, float]]:
    """[(iso_date, close)] ascending from NSE's indicesHistory."""
    import gzip
    import io as _io
    import json as _json
    import urllib.parse
    import urllib.request

    url = NSE_HIST.format(q=urllib.parse.quote(nse_name),
                          frm=frm.strftime("%d-%m-%Y"),
                          to=to.strftime("%d-%m-%Y"))
    req = urllib.request.Request(url, headers={"User-Agent": NSE_UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=_io.BytesIO(raw)).read()
    doc = _json.loads(raw)
    out = []
    for rec in (doc.get("data") or []):
        name = str(rec.get("EOD_INDEX_NAME") or "").strip().upper()
        want = nse_name.upper()
        # NSE echoes some indices under their SHORT name — 'NIFTY INFRA' comes
        # back for the index queried as 'NIFTY INFRASTRUCTURE' (hit 2026-09-03).
        # A prefix match keeps the wrong-index protection: a genuinely different
        # index is not a prefix of the request. The 0.1% overlap-agreement guard
        # in nse_gap_fill stays the hard check either way.
        if not (name == want or want.startswith(name)):
            raise ValueError(f"asked {nse_name!r}, got {name!r} — wrong index")
        d = dt.datetime.strptime(rec["EOD_TIMESTAMP"].title(), "%d-%b-%Y").date()
        close = float(rec["EOD_CLOSE_INDEX_VAL"])
        if close <= 0 or d.weekday() > 4:
            continue
        out.append((d.isoformat(), close))
    out.sort()
    return out


def nse_gap_fill(conn: sqlite3.Connection, sid: str, nse_name: str,
                 today_utc: str, captured: str) -> int:
    """Top up sid's tail from NSE, from the last stored date to yesterday.

    The consistency guard is the load-bearing part: on every date BOTH
    sources hold, the closes must agree to 0.1% or the whole fill is refused
    — the failure this catches is fetching the wrong index under a right-
    looking name, which would otherwise splice two different series."""
    last = conn.execute("SELECT MAX(date) FROM flow_series WHERE series_id=?",
                        (sid,)).fetchone()[0]
    if not last:
        return 0  # Yahoo backfills first; NSE only ever fills a tail
    today = dt.date.fromisoformat(today_utc)
    frm = dt.date.fromisoformat(last) - dt.timedelta(days=7)  # overlap on purpose
    if last >= (today - dt.timedelta(days=1)).isoformat():
        return 0
    rows = _nse_history(nse_name, frm, today)
    rows = [r for r in rows if r[0] < today_utc]
    if not rows:
        return 0
    stored = dict(conn.execute(
        "SELECT date, close FROM flow_series WHERE series_id=? AND date>=?",
        (sid, frm.isoformat())))
    for d, c in rows:
        if d in stored and abs(c / stored[d] - 1.0) > 0.001:
            raise ValueError(f"{sid}: NSE {nse_name!r} disagrees with stored "
                             f"close on {d} ({c} vs {stored[d]}) — refusing "
                             f"the whole fill")
    n = 0
    for d, c in rows:
        if d in stored:
            continue  # agreement verified above; keep the original source row
        conn.execute(
            "INSERT INTO flow_series (series_id, date, close, source, captured_at) "
            "VALUES (?,?,?,?,?)",
            (sid, d, c, "nse", captured))
        n += 1
    conn.commit()
    return n


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
    # NSE tail fill for the India indices Yahoo has stopped carrying daily.
    for sid, s in todo.items():
        if not s.get("nse_name"):
            continue
        try:
            n = nse_gap_fill(conn, sid, s["nse_name"], today_utc, captured)
            if n:
                last = conn.execute("SELECT MAX(date) FROM flow_series WHERE "
                                    "series_id=?", (sid,)).fetchone()[0]
                print(f"  nse  {sid:8s} +{n} rows -> {last}")
                total += n
        except Exception as e:
            print(f"  nse  {sid:8s} gap-fill failed: {e}")
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
