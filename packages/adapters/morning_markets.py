"""Pre-market global snapshot for the Daily Overview's morning brief.

WHAT THIS IS. The deterministic half of the morning brief: US overnight
closes, the semis complex, the two IT bellwethers, GIFT Nifty against the
Nifty close, and entity-keyed headlines to ground the "why did it move"
bullets. Stdlib only, unattended-safe, ~10s.

WHAT THIS IS NOT. It never writes to `prices` or anywhere else in ims.db.
ACN or the SOX in the `prices` table would become bridge-shockable
(`_series_in_store()`), the exact reason cement_pack refuses to load its own
Valuation sheet and yahoo_estimates writes to `estimates`. Output is a dated
JSON under data/morning/ that the API serves for DISPLAY, nothing more.

The interpretive half — broker-mail actionables per sector, and the "reason"
prose behind an ACN/CTSH move — is the agent's job (.claude/skills/
morning-brief), because it needs the M365 MCP and judgment. Same split as
mail-fetch / mail_watch, in the same order: python first, agent on top.

GIFT NIFTY comes from NSE IX's own site API, discovered 2026-08-30 by
watching the page's XHR (the HTML is a 5KB JS shell — nothing to scrape).
`/api/market-rate?type=derivatives` answers plain urllib in ~0.5s with no
token, although the page itself calls /api/generate-token first. If this
starts failing with 401/403, that free ride ended; check whether the token
call became a prerequisite before assuming the endpoint moved.

Rows there arrive DUPLICATED (each contract appears twice with the same
values) and carry two expiries. Front contract = max volume, never "first
row". On 2026-08-29 the Oct contract had traded 23 lots against Sep's
46,485 — first-row would have quoted a stale back-month print.

Usage:
    python packages/adapters/morning_markets.py            # fetch + write + print
    python packages/adapters/morning_markets.py --print    # re-print today's file
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO / "data" / "morning"
sys.path.insert(0, str(REPO / "packages" / "adapters"))

from yahoo_prices import fetch, UA  # noqa: E402  (the debugged close-pairing)

# (group, yahoo symbol, label, name_pattern). The name pattern is the identity
# guard — see yahoo_prices.py: instrument type does not discriminate, the name
# does. All verified via yahoo_search.py 2026-08-30, not guessed.
WATCH: list[tuple[str, str, str, str]] = [
    ("us",    "^GSPC", "S&P 500",       r"S&P 500"),
    ("us",    "^IXIC", "Nasdaq",        r"NASDAQ"),
    ("us",    "^DJI",  "Dow",           r"Dow"),
    ("semis", "^SOX",  "SOX (semis)",   r"PHLX Semiconductor"),
    ("semis", "NVDA",  "Nvidia",        r"NVIDIA"),
    ("semis", "AMD",   "AMD",           r"Advanced Micro"),
    ("semis", "TSM",   "TSMC (ADR)",    r"Taiwan Semiconductor"),
    ("semis", "AVGO",  "Broadcom",      r"Broadcom"),
    ("it",    "ACN",   "Accenture",     r"Accenture"),
    ("it",    "CTSH",  "Cognizant",     r"Cognizant"),
]

# Entity-keyed news queries. ENTITIES, never topics: Yahoo's search endpoint
# is entity-keyed, not a search engine — "nonfarm payrolls" style topical
# queries return generic market filler that LOOKS like results. A headline
# that does not name its entity is dropped below, and an empty list after
# that filter means NO SIGNAL, not "nothing happened".
NEWS_QUERIES: list[tuple[str, str, list[str]]] = [
    # (key, query, on-entity match terms, lowercase)
    ("ACN",  "Accenture",  ["accenture"]),
    ("CTSH", "Cognizant",  ["cognizant"]),
    ("NVDA", "Nvidia",     ["nvidia"]),
    ("AVGO", "Broadcom",   ["broadcom"]),
    ("TSM",  "TSMC",       ["tsmc", "taiwan semi"]),
    ("AMD",  "AMD",        ["amd "]),
]

SEARCH = ("https://query2.finance.yahoo.com/v1/finance/search"
          "?q={q}&quotesCount=0&newsCount=8&listsCount=0")
NSEIX_DERIV = "https://www.nseix.com/api/market-rate?type=derivatives"
NSEIX_NIFTY = "https://www.nseix.com/api/nifty-market-rate"


def _get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------- quotes ----

def quote(symbol: str, label: str, pattern: str) -> dict:
    """Last close + prior close -> day change. 1mo range survives holidays."""
    rows = fetch(symbol, rng="1mo", name_pattern=pattern)
    if len(rows) < 2:
        raise ValueError(f"{symbol}: {len(rows)} close(s) in a month")
    (d_prev, c_prev), (d_last, c_last) = rows[-2], rows[-1]
    return {
        "symbol": symbol, "label": label,
        "date": d_last, "close": round(c_last, 2),
        "prev_date": d_prev,
        "chg_pct": round((c_last / c_prev - 1.0) * 100, 2),
    }


# ------------------------------------------------------------ gift nifty ----

def gift_nifty() -> dict:
    """Front GIFT Nifty future + the gap to the Nifty 50 close.

    The GAP is the pre-market number — GIFT trades ~21h, so its overnight
    drift against yesterday's NSE close is what "how are we opening" means.
    """
    doc = _get_json(NSEIX_DERIV)
    rows = [r for r in (doc.get("data") or [])
            if r.get("INSTRUMENTTYPE") == "FUTIDX" and r.get("SYMBOL") == "NIFTY"]
    if not rows:
        raise ValueError("nseix: no NIFTY FUTIDX rows")

    # Dedup the doubled rows, then front contract by volume — never row order.
    by_exp: dict[str, dict] = {}
    for r in rows:
        by_exp.setdefault(str(r.get("EXPIRYDATE")), r)
    front = max(by_exp.values(),
                key=lambda r: float(r.get("CONTRACTSTRADED") or 0))

    last = float(str(front.get("LASTPRICE")).replace(",", ""))
    chg_pct = float(str(front.get("PERCHANGE") or "0").replace(",", "") or 0)
    stamp = str(front.get("TIMESTMP") or "")

    spot = None
    try:
        nrows = _get_json(NSEIX_NIFTY)
        n = nrows[0] if isinstance(nrows, list) and nrows else {}
        spot = {
            "close": float(str(n.get("OI_CLOSE_INDEX_VAL"
                                     )).replace(",", "")),
            "stamp": str(n.get("FULLTIMESTAMP") or ""),
        }
    except Exception as exc:                       # gap is optional, level is not
        spot = {"error": f"{type(exc).__name__}: {exc}"}

    out = {"last": last, "chg_pct": chg_pct, "stamp": stamp,
           "expiry": str(front.get("EXPIRYDATE")),
           "volume": int(float(front.get("CONTRACTSTRADED") or 0)),
           "nifty_close": spot}

    # Plausibility is RELATIVE — a futures print >10% off the spot close is a
    # wrong contract or a parse slip, not a market move worth rendering.
    if spot and "close" in spot:
        gap = last - spot["close"]
        if abs(gap) > 0.10 * spot["close"]:
            raise ValueError(f"gift {last} vs nifty close {spot['close']}: "
                             f"gap {gap:+.0f} implausible — wrong row?")
        out["gap_pts"] = round(gap, 1)
        out["gap_pct"] = round(gap / spot["close"] * 100, 2)
    return out


# ----------------------------------------------------------------- news -----

def news(key: str, query: str, terms: list[str]) -> list[dict]:
    doc = _get_json(SEARCH.format(q=urllib.parse.quote(query)))
    out = []
    for n in (doc.get("news") or []):
        title = str(n.get("title") or "")
        if not any(t in title.lower() for t in terms):
            continue                       # off-entity filler — drop, not keep
        ts = n.get("providerPublishTime")
        when = (dt.datetime.fromtimestamp(ts, dt.timezone.utc)
                .strftime("%Y-%m-%d %H:%MZ") if ts else "")
        out.append({"title": title, "publisher": str(n.get("publisher") or ""),
                    "when": when, "link": str(n.get("link") or "")})
    return out


# ----------------------------------------------------------------- main -----

def build() -> dict:
    today = dt.date.today().isoformat()
    out: dict = {"date": today,
                 "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                 "quotes": {"us": [], "semis": [], "it": []},
                 "news": {}, "errors": []}

    for group, sym, label, pat in WATCH:
        try:
            out["quotes"][group].append(quote(sym, label, pat))
        except Exception as exc:           # a dead symbol must be SAID, not skipped
            out["errors"].append(f"{sym}: {type(exc).__name__}: {exc}")

    try:
        out["gift_nifty"] = gift_nifty()
    except Exception as exc:
        out["gift_nifty"] = {"error": f"{type(exc).__name__}: {exc}"}
        out["errors"].append(f"gift_nifty: {exc}")

    for key, q, terms in NEWS_QUERIES:
        try:
            out["news"][key] = news(key, q, terms)
        except Exception as exc:
            out["news"][key] = []
            out["errors"].append(f"news {key}: {type(exc).__name__}: {exc}")

    return out


def write(doc: dict) -> pathlib.Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"markets_{doc['date']}.json"
    p.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return p


def show(doc: dict) -> None:
    def line(q):
        arrow = "+" if q["chg_pct"] >= 0 else ""
        return (f"  {q['label']:<14} {q['close']:>10,.2f}  "
                f"{arrow}{q['chg_pct']:.2f}%  ({q['date']})")
    for grp, title in (("us", "US"), ("semis", "Semis"), ("it", "IT bellwethers")):
        print(title)
        for q in doc["quotes"][grp]:
            print(line(q))
    g = doc.get("gift_nifty") or {}
    if "last" in g:
        gap = (f"  gap {g['gap_pts']:+,.0f} pts ({g['gap_pct']:+.2f}%) vs Nifty "
               f"close {g['nifty_close']['close']:,.2f}" if "gap_pts" in g else "")
        print(f"GIFT Nifty\n  {g['last']:,.2f}  {g['chg_pct']:+.2f}%  "
              f"[{g['expiry']}, {g['volume']} lots, {g['stamp']}]{gap}")
    else:
        print(f"GIFT Nifty\n  UNAVAILABLE: {g.get('error')}")
    for key, items in (doc.get("news") or {}).items():
        if items:
            print(f"news {key}: " + " | ".join(
                f"{x['title'][:70]} [{x['publisher']}]" for x in items[:3]))
    for e in doc.get("errors") or []:
        print(f"ERROR {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="print_only",
                    help="re-print today's file without fetching")
    args = ap.parse_args()

    if args.print_only:
        p = OUT_DIR / f"markets_{dt.date.today().isoformat()}.json"
        if not p.exists():
            print(f"no {p.name} — run without --print first", file=sys.stderr)
            return 1
        show(json.loads(p.read_text(encoding="utf-8")))
        return 0

    doc = build()
    p = write(doc)
    show(doc)
    print(f"\nwrote {p}")
    # Errors are non-fatal on purpose: a dead ^SOX must not cost the PM the
    # other nine tiles at 8am. They are IN the file, so the page shows them.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
