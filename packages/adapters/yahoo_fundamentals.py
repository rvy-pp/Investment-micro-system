"""Probe what valuation inputs Yahoo will give us for the coverage names.

P3 needs EV/EBITDA for commodities and P/E for IT / EMS / autos. Neither is
computable from a price series alone:

    EV/EBITDA = (market cap + net debt) / EBITDA
    P/E       = price / EPS

So we need shares outstanding (or market cap) and net debt. This checks what is
actually available before the scorer is designed around it.

Usage:
    python packages/adapters/yahoo_fundamentals.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from yahoo_prices import UA  # noqa: E402

Q = ("https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
     "?modules=defaultKeyStatistics,financialData,summaryDetail")

SYMS = {
    "hindalco": "HINDALCO.NS", "nalco": "NATIONALUM.NS",
    "hindustan_zinc": "HINDZINC.NS", "vedanta": "VEDL.NS", "vaml": "VAML.NS",
}

WANT = [
    ("sharesOutstanding", "defaultKeyStatistics"),
    ("enterpriseValue", "defaultKeyStatistics"),
    ("enterpriseToEbitda", "defaultKeyStatistics"),
    ("trailingEps", "defaultKeyStatistics"),
    ("forwardPE", "defaultKeyStatistics"),
    ("totalDebt", "financialData"),
    ("totalCash", "financialData"),
    ("ebitda", "financialData"),
    ("marketCap", "summaryDetail"),
    ("trailingPE", "summaryDetail"),
]


def get(sym: str) -> dict:
    req = urllib.request.Request(Q.format(sym=sym), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        doc = json.load(r)
    res = (doc.get("quoteSummary") or {}).get("result") or []
    return res[0] if res else {}


def val(blob: dict, mod: str, key: str):
    m = blob.get(mod) or {}
    v = m.get(key)
    if isinstance(v, dict):
        return v.get("raw")
    return v


if __name__ == "__main__":
    hdr = f"{'entity':16}" + "".join(f"{k[:13]:>15}" for k, _ in WANT)
    print(hdr)
    print("-" * len(hdr))
    for eid, sym in SYMS.items():
        try:
            blob = get(sym)
        except Exception as exc:
            print(f"{eid:16} FAILED {type(exc).__name__}: {str(exc)[:50]}")
            continue
        line = f"{eid:16}"
        for key, mod in WANT:
            v = val(blob, mod, key)
            if v is None:
                line += f"{'—':>15}"
            elif abs(v) >= 1e7:
                line += f"{v/1e7:>14,.0f}c"      # crore
            else:
                line += f"{v:>15,.2f}"
        print(line)
    print("\n'c' suffix = INR crore. '—' = not provided by Yahoo.")
