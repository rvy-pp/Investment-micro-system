"""Confirm what a Yahoo commodity symbol ACTUALLY is before trusting its level.

A symbol that returns a plausible number is not the same as the right series.
ALI=F could be LME-linked or a US Midwest premium contract; ZNC=F could be
stale or quoted in different units. Either mistake silently rescales every
signal that depends on it, so check currency, instrument type and liveness
rather than eyeballing the level.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from yahoo_prices import CHART, UA  # noqa: E402

SYMS = ["ALI=F", "ZNC=F", "ZN=F", "SI=F", "XAGUSD=X", "USDINR=X", "HG=F"]

for sym in SYMS:
    try:
        req = urllib.request.Request(CHART.format(sym=sym, rng="1mo"),
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            doc = json.load(r)
        m = doc["chart"]["result"][0]["meta"]
        q = doc["chart"]["result"][0]["indicators"]["quote"][0]
        closes = [c for c in (q.get("close") or []) if c is not None]
        stamps = doc["chart"]["result"][0].get("timestamp") or []
        last_day = (dt.datetime.fromtimestamp(stamps[-1], dt.timezone.utc).date()
                    if stamps else None)
        # distinct closes: a live series moves; a stale contract does not
        distinct = len(set(round(c, 4) for c in closes))
        print(f"{sym:10} {m.get('currency','?'):5} "
              f"{m.get('instrumentType','?'):8} "
              f"name={str(m.get('shortName') or m.get('longName'))[:34]:36} "
              f"last={closes[-1] if closes else None!s:>10} "
              f"day={last_day} distinct={distinct}/{len(closes)}")
    except Exception as exc:
        print(f"{sym:10} FAIL {type(exc).__name__}: {exc}")
