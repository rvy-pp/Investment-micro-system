"""Resolve a company name to its actual Yahoo symbol.

Guessing symbols is how VAML stayed unresolved through three rounds of
VEDANTAALUMINIUM.NS / VEDALUM.NS / VDLALUM.NS, all 404. Yahoo has a search
endpoint; use it instead of inventing tickers.

Usage:
    python packages/adapters/yahoo_search.py "Vedanta Aluminium"
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SEARCH = ("https://query2.finance.yahoo.com/v1/finance/search"
          "?q={q}&quotesCount=20&newsCount=0&listsCount=0")


def search(term: str) -> list[dict]:
    url = SEARCH.format(q=urllib.parse.quote(term))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        doc = json.load(r)
    return doc.get("quotes") or []


if __name__ == "__main__":
    for term in sys.argv[1:]:
        print(f"=== {term!r} ===")
        try:
            hits = search(term)
        except Exception as exc:
            print(f"  FAILED {type(exc).__name__}: {exc}")
            continue
        if not hits:
            print("  no matches")
        for q in hits:
            print(f"  {str(q.get('symbol')):22} "
                  f"{str(q.get('quoteType')):10} "
                  f"{str(q.get('exchange')):8} "
                  f"{str(q.get('shortname') or q.get('longname'))[:52]}")
