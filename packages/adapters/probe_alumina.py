"""Hunt for a fetchable alumina series.

Alumina is the primary driver of the NALCO/VAML pair and has no source yet.
CME lists an Alumina FOB Australia (Fastmarkets MB) futures contract; LME lists
one too. Probe every plausible symbol and report what each one ACTUALLY is —
identity and liveness, not just whether a number comes back.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from yahoo_prices import CHART, UA  # noqa: E402

CANDIDATES = [
    # CME/COMEX alumina product codes. TradingView shows COMEX-ALA1! for the
    # Platts-settled contract, so ALA is the product code; the Fastmarkets MB
    # contract is COMEX rulebook ch.196 and may carry a different code.
    "ALA=F", "ALA1=F", "ALAF=F", "ALB=F", "AMB=F", "MBA=F",
    "ALU=F", "AUP=F", "ALD=F", "ALM=F", "AAL=F",
    "ALUM=F", "ALUA=F", "AO=F", "ALZ=F",
    "ALI=F",   # known-good control: CME Aluminum, proves the probe works
]

print(f"{'symbol':10} {'ccy':4} {'type':10} {'name':40} {'last':>12} {'day':>12} {'live'}")
print("-" * 100)
for sym in CANDIDATES:
    try:
        req = urllib.request.Request(CHART.format(sym=sym, rng="1mo"),
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            doc = json.load(r)
        res = doc["chart"]["result"][0]
        m = res.get("meta") or {}
        q = res["indicators"]["quote"][0]
        closes = [c for c in (q.get("close") or []) if c is not None]
        stamps = res.get("timestamp") or []
        last_day = (dt.datetime.fromtimestamp(stamps[-1], dt.timezone.utc).date()
                    .isoformat() if stamps else "-")
        distinct = len(set(round(c, 6) for c in closes)) if closes else 0
        live = f"{distinct}/{len(closes)} distinct"
        name = str(m.get("shortName") or m.get("longName") or "?")[:38]
        print(f"{sym:10} {str(m.get('currency'))[:4]:4} "
              f"{str(m.get('instrumentType'))[:10]:10} {name:40} "
              f"{closes[-1] if closes else 0:>12,.2f} {last_day:>12} {live}")
    except Exception as exc:
        print(f"{sym:10} -- {type(exc).__name__}: {str(exc)[:50]}")
