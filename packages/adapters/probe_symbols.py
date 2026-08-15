"""Generic symbol prober — identity and liveness for any Yahoo symbol.

Generalises probe_alumina.py. Reports what a symbol ACTUALLY is, so a candidate
is accepted on evidence rather than on a plausible-looking number.

Usage:
    python packages/adapters/probe_symbols.py MTF=F NCF=F ALW=F
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from yahoo_prices import CHART, UA  # noqa: E402


def describe(sym: str) -> str:
    try:
        req = urllib.request.Request(CHART.format(sym=sym, rng="3mo"),
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            doc = json.load(r)
        res = doc["chart"]["result"][0]
        m = res.get("meta") or {}
        q = res["indicators"]["quote"][0]
        closes = [c for c in (q.get("close") or []) if c is not None]
        stamps = res.get("timestamp") or []
        if not closes:
            return f"{sym:10} -- no closes"
        last_day = dt.datetime.fromtimestamp(stamps[-1], dt.timezone.utc).date()
        stale = (dt.date.today() - last_day).days
        distinct = len(set(round(c, 6) for c in closes))
        ratio = distinct / len(closes)
        name = str(m.get("shortName") or m.get("longName") or "?")[:40]
        flag = "LIVE " if (stale <= 5 and ratio >= 0.5) else "SUSPECT"
        return (f"{sym:10} {str(m.get('currency'))[:4]:4} "
                f"{str(m.get('instrumentType'))[:9]:9} {name:42} "
                f"{closes[-1]:>10,.2f} {last_day} stale={stale}d "
                f"{ratio:.0%} distinct  {flag}")
    except Exception as exc:
        return f"{sym:10} -- {type(exc).__name__}: {str(exc)[:46]}"


if __name__ == "__main__":
    for s in sys.argv[1:]:
        print(describe(s))
