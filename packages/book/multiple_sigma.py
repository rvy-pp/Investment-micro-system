"""The case range: σ of a stock's OWN multiple, measured over 13-week moves.

PM, 2026-10-28, after the IT band (±0.5pp growth / ±50bps margin) turned out
to be one sector-wide size with no evidence behind it: "How about using
standard deviation on the multiples, 1 sigma back for bear, 1 sigma up for
bull? This will minimize random broker bias", then "trailing 90 weeks is
fine" and "lets do 13 week then". So a case's bear and bull are its base EPS
at the anchor multiple x e^-σ and x e^+σ, where σ is the standard deviation
of the 13-week log changes in the stock's own trailing multiple over the last
90 weeks (`specs/book.yaml case_bands`).

WHY 13-WEEK CHANGES AND NOT THE LEVEL. Over the 90 weeks to 2026-10-28 the IT
large caps halved their multiple (TCS 29.2x -> 13.9x low, Infosys 27.9x ->
13.0x), so the σ of the LEVEL was ~21% for both and measured mostly the trend.
The 13-week change σ was 11.1% / 12.5%. For names that did not trend (HCL
Tech 13.9% / 13.9%, TechM 12.3% / 12.9%) the two agree, which is the check
that the level σ was the trend. 13 weeks is also the Kelly horizon.

THE HISTORY IS TRAILING, THE ANCHOR IS FORWARD. Screener carries a trailing
P/E (and EV/EBITDA) history; no free source carries a forward one. Only the
σ is taken from it, in log terms, so it is a percentage swing applied to the
forward anchor. Trailing EPS includes one-offs (TCS's Q3FY26/Q1FY27 charges),
which add a little noise; stated, not adjusted.

Where it lives: `data/analysis/multiples/<entity>.json` (gitignored, like the
cases), one weekly series per leg (the last print of each ISO week), with the
fetch date. Nothing here reaches `prices`, `estimates` or a pillar.

    python packages/book/multiple_sigma.py --fetch            # every mapped leg
    python packages/book/multiple_sigma.py --fetch tcs infosys
    python packages/book/multiple_sigma.py                    # σ table
    python packages/book/multiple_sigma.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import re
import statistics as st
import sys
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
STORE = REPO / "data" / "analysis" / "multiples"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
# Screener's chart queries and the dataset each returns.
QUERY = {"pe": ("Price+to+Earning-Median+PE-EPS", "Price to Earning"),
         "ev_ebitda": ("EV+Multiple-Median+EV+Multiple-EBITDA", "EV Multiple")}
# entity -> screener symbol. Resolved by screener's own search, not guessed:
# LTIMindtree is LTM on screener now ("LTM Ltd"); LTIM returns 404.
SYMBOLS = {"tcs": "TCS", "infosys": "INFY", "wipro": "WIPRO", "hcl_tech": "HCLTECH",
           "tech_mahindra": "TECHM", "ltimindtree": "LTM", "persistent": "PERSISTENT",
           "coforge": "COFORGE", "mphasis": "MPHASIS", "tata_elxsi": "TATAELXSI",
           "kpit": "KPITTECH", "ultratech": "ULTRACEMCO", "shree": "SHREECEM",
           "ambuja": "AMBUJACEM", "dalmia": "DALBHARAT"}
DAYS = 1095        # three years fetched; the window takes what it needs
MAX_AGE_DAYS = 35  # a 90-week σ barely moves in a month; older than this is withheld


def _get(url: str) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()


def weekly(points: list) -> list[tuple[str, float]]:
    """Screener returns daily points; keep the LAST positive print of each ISO
    week. Blank, zero and negative multiples are dropped (a loss year prints a
    negative P/E, which has no log)."""
    wk = {}
    for d, v in points:
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if x <= 0:
            continue
        wk[dt.date.fromisoformat(d).isocalendar()[:2]] = (d, x)
    return [wk[k] for k in sorted(wk)]


def fetch(ent: str, kind: str, today: dt.date | None = None) -> dict:
    sym = SYMBOLS.get(ent)
    if not sym:
        raise SystemExit(f"{ent}: no screener symbol in multiple_sigma.SYMBOLS — add it from screener's search")
    page = _get(f"https://www.screener.in/company/{sym}/consolidated/").decode("utf-8", "replace")
    m = re.search(r'data-company-id="(\d+)"', page) or re.search(r'data-warehouse-id="(\d+)"', page)
    if not m:
        raise SystemExit(f"{ent}: no company id on screener's page for {sym}")
    q, key = QUERY[kind]
    js = json.loads(_get(f"https://www.screener.in/api/company/{m.group(1)}/chart/?q={q}&days={DAYS}&consolidated=true"))
    pts = {d["metric"]: d["values"] for d in js.get("datasets", [])}.get(key)
    note = ""
    if not pts:
        # Tata Elxsi, 2026-10-29: the combined query ('Price to Earning-Median
        # PE-EPS') came back with all THREE series EMPTY for company id 3366,
        # consolidated or not, at 30/365/1095 days, while the bare 'Price to
        # Earning' query returned the full 248-point daily history, and
        # Coforge's combined query answered normally. The EPS leg appears to
        # empty the whole response for this filer. The bare metric is the same
        # series, so it is taken instead and the source says so. Caught by the
        # fetch refusing, not by a wrong number.
        # The bare query answered WITHOUT the consolidated flag (the probe that
        # found it); with it, still empty. Try the flag first, then without.
        for tail in ("&consolidated=true", "", "&consolidated=false"):
            js = json.loads(_get(f"https://www.screener.in/api/company/{m.group(1)}/chart/?q={key.replace(' ', '+')}&days={DAYS}{tail}"))
            pts = {d["metric"]: d["values"] for d in js.get("datasets", [])}.get(key)
            if pts:
                note = f"; bare-metric query{tail or ' (no consolidated flag)'}, the combined one returned no series"
                break
    if not pts:
        raise SystemExit(f"{ent}: screener returned no {key!r} series")
    doc = {"entity": ent, "symbol": sym, "kind": kind, "source": f"screener.in chart {key} (consolidated){note}",
           "fetched": (today or dt.date.today()).isoformat(), "weekly": weekly(pts)}
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / f"{ent}.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc


def sigma_of(series: list[tuple[str, float]], window_weeks: int, change_weeks: int) -> dict | None:
    """σ of the overlapping change_weeks log changes inside the last
    window_weeks weekly prints. None when the window cannot hold a change —
    withheld, never guessed from a shorter history."""
    w = series[-window_weeks:]
    if len(w) < window_weeks or len(w) <= change_weeks + 1:
        return None
    lg = [math.log(v) for _, v in w]
    ch = [lg[i + change_weeks] - lg[i] for i in range(len(lg) - change_weeks)]
    return {"sigma": st.pstdev(ch), "n_changes": len(ch), "first": w[0][0], "last": w[-1][0],
            "last_value": w[-1][1], "sigma_level": st.pstdev(lg)}


def sigma(ent: str, kind: str, window_weeks: int, change_weeks: int, today: dt.date | None = None) -> dict:
    """The case range's σ for one leg, from the stored history. Always returns
    a dict; `sigma` is None (and `why` says why) when it must be withheld:
    no history, the wrong multiple, too short, or older than MAX_AGE_DAYS."""
    today = today or dt.date.today()
    f = STORE / f"{ent}.json"
    base = {"sigma": None, "window_weeks": window_weeks, "change_weeks": change_weeks}
    if not f.exists():
        return {**base, "why": f"no multiple history — run multiple_sigma.py --fetch {ent}"}
    doc = json.loads(f.read_text(encoding="utf-8"))
    if doc.get("kind") != kind:
        return {**base, "why": f"stored history is {doc.get('kind')!r}, the case runs on {kind!r} — refetch"}
    age = (today - dt.date.fromisoformat(doc["fetched"])).days
    if age > MAX_AGE_DAYS:
        return {**base, "fetched": doc["fetched"],
                "why": f"history fetched {doc['fetched']} ({age}d ago, over {MAX_AGE_DAYS}) — refetch"}
    s = sigma_of([tuple(x) for x in doc["weekly"]], window_weeks, change_weeks)
    if s is None:
        return {**base, "fetched": doc["fetched"],
                "why": f"{len(doc['weekly'])} weekly prints, the window needs {window_weeks}"}
    return {**base, **s, "fetched": doc["fetched"], "symbol": doc["symbol"], "source": doc["source"]}


def _legs() -> list[tuple[str, str, dict | None]]:
    """(entity, multiple kind, sector band) for every leg with a case build."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import case_brief
    groups = {r["entity"]: r["group"] for r in case_brief.analysis.roster()}
    return [(f.parent.name, case_brief.case_valuation(groups[f.parent.name]), case_brief.case_band(groups[f.parent.name]))
            for f in sorted(case_brief.CASES.glob("*/build.json")) if f.parent.name in groups]


def selftest() -> None:
    ok = 0
    # weekly(): the last print of each ISO week, blanks and non-positives dropped
    pts = [["2026-01-05", "10"], ["2026-01-07", "11"], ["2026-01-09", ""], ["2026-01-12", "-3"],
           ["2026-01-13", "12"], ["2026-01-20", None], ["2026-01-21", "0"], ["2026-01-22", "13.5"]]
    w = weekly(pts)
    assert w == [("2026-01-07", 11.0), ("2026-01-13", 12.0), ("2026-01-22", 13.5)], w; ok += 1
    # sigma_of(): hand-checked on a series with known 2-week log changes
    s = [("d%02d" % i, v) for i, v in enumerate([10, 11, 12, 11, 10, 12])]
    lg = [math.log(v) for _, v in s]
    want = st.pstdev([lg[i + 2] - lg[i] for i in range(4)])
    got = sigma_of(s, 6, 2)
    assert abs(got["sigma"] - want) < 1e-12 and got["n_changes"] == 4; ok += 1
    # the window takes the LAST n prints, not the first
    s2 = [("a", 100.0), ("b", 1.0)] + s
    assert abs(sigma_of(s2, 6, 2)["sigma"] - want) < 1e-12; ok += 1
    # a trend does not inflate the change σ: a steady -1%/week slide has
    # change σ 0 and a large level σ (why the level was rejected)
    tr = [(f"w{i}", 30 * 0.99 ** i) for i in range(90)]
    t = sigma_of(tr, 90, 13)
    assert t["sigma"] < 1e-9 and t["sigma_level"] > 0.2; ok += 1
    # rejection: a window the history cannot fill is withheld, never shortened
    assert sigma_of(s, 7, 2) is None and sigma_of(s, 6, 5) is None; ok += 1
    # sigma(): withheld states from the store, each with its reason
    import tempfile
    global STORE
    keep = STORE
    with tempfile.TemporaryDirectory() as td:
        STORE = pathlib.Path(td)
        assert sigma("x", "pe", 6, 2)["sigma"] is None; ok += 1                         # no file
        doc = {"entity": "x", "symbol": "X", "kind": "pe", "source": "t", "fetched": "2026-10-28",
               "weekly": [list(p) for p in s]}
        (STORE / "x.json").write_text(json.dumps(doc))
        today = dt.date(2026, 10, 28)
        assert abs(sigma("x", "pe", 6, 2, today)["sigma"] - want) < 1e-12; ok += 1      # acceptance
        assert "ev_ebitda" in sigma("x", "ev_ebitda", 6, 2, today)["why"]; ok += 1       # wrong multiple
        old = today + dt.timedelta(days=MAX_AGE_DAYS + 1)
        assert sigma("x", "pe", 6, 2, old)["sigma"] is None; ok += 1                    # stale
        assert sigma("x", "pe", 6, 2, today + dt.timedelta(days=MAX_AGE_DAYS))["sigma"] is not None; ok += 1  # at the edge
    STORE = keep
    print(f"multiple_sigma selftest: {ok} checks passed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fetch", nargs="*", metavar="ENTITY", help="fetch history (no names: every leg with a case)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    legs = _legs()
    if a.fetch is not None:
        kinds = {e: k for e, k, _ in legs}
        for ent in (a.fetch or list(kinds)):
            for attempt in range(3):
                try:
                    d = fetch(ent, kinds.get(ent, "pe"))
                    print(f"{ent:14s} {d['symbol']:11s} {d['kind']:9s} {len(d['weekly'])} weeks to {d['weekly'][-1][0]}")
                    break
                except urllib.error.HTTPError as e:   # screener throttles: 429 on the 7th-9th request
                    print(f"{ent}: HTTP {e.code}, retrying"); time.sleep(20)
            time.sleep(5)
    print(f"{'leg':14s} {'mult':9s} {'σ 13w':>7s} {'σ level':>8s}  window")
    for ent, kind, band in legs:
        ms = (band or {}).get("multiple_sigma")
        if not ms:
            print(f"{ent:14s} {kind:9s} no σ range: its sector band is {band!r}")
            continue
        s = sigma(ent, kind, ms["window_weeks"], ms["change_weeks"])
        if s["sigma"] is None:
            print(f"{ent:14s} {kind:9s} withheld: {s['why']}")
        else:
            print(f"{ent:14s} {kind:9s} {100*s['sigma']:6.1f}% {100*s['sigma_level']:7.1f}%  {s['first']}..{s['last']} (fetched {s['fetched']})")


if __name__ == "__main__":
    main()
