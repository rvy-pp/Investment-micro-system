"""Why did a pair leg move? Seven checks, run whenever the PM asks about a pair.

PM, 2026-10-22, after "if a leg falls/rises unnecessarily, it is either noise or
we are missing some information": *"Add these 5 + 2 blind spot checks. If I ever
[ask about] a pair, this should be checked and told."* The `pair-check` skill
runs this script and then does the two blind spots by hand.

A move against the target is one of three things: NOISE (flows, positioning,
nothing about the company), INFORMATION you could have seen (a preview, a
revision, a peer's print), or information you cannot see / a repricing of the
whole sector. In a pair the sector part largely cancels, so the question is
whether a leg moved RELATIVE to its sector, and if so, which check explains it.

  1 sector       stock = beta x sector basket + own, over the window
                 (sector_rel.split, the Book tab's own definition)
  2 consensus    Yahoo FY+1 / FY+2 EPS at the window start vs end
  3 mail         morning-brief bullets and staged mail naming the stock, plus
                 house estimates and analyst views logged inside the window
  4 positioning  OI change and the latest buildup / 3m percentile (`oi`)
  5 read-across  US bellwethers (ACN, CTSH — IT only), the sector's biggest
                 movers, and result bullets about peers in the sector's bucket
  6 block/bulk deals          NOT IN THE STORE — the skill checks NSE by hand
  7 conference/analyst meets NOT IN THE STORE — the skill searches mail/news

This script REPORTS; it never decides. A check "fires" past a named first-cut
threshold (below), and a leg whose own move is large with nothing firing is
labelled `unexplained so far` — never "noise", because absence of evidence is
not evidence (the Avalon dossier: 7 of 18 big moves had no reason in its
archive, and its deepest drawdown was a market factor).

Windows are CALENDAR days back from the latest close (CLAUDE.md). Mail dated
D is the morning before session D, so a window from close S to close E reads
briefs dated (S, E]. Read-only; writes nothing.

    python packages/book/move_check.py TECHM_LTM            # 7 and 30 days
    python packages/book/move_check.py TECHM_LTM --days 5
    python packages/book/move_check.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import pathlib
import re
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

# First-cut thresholds, named so the report can print them. Re-measure once a
# few dozen checks have been read against what actually explained the move.
OWN_PP = 1.5            # |own move| above this = the stock moved off its sector
REVISION_PCT = 1.0      # |FY EPS change| above this = an earnings change
OI_CHG_PCT = 5.0        # |OI change| above this = a positioning event
BELLWETHER_PCT = 3.0    # |summed ACN/CTSH move| above this = read-across
PEER_MOVE_PCT = 5.0     # a sector peer's move above this = possible read-across

BELLWETHERS = {"it": ["ACN", "CTSH"]}
RESULT_WORDS = re.compile(r"\b(?:[1-4]Q|Q[1-4])\s?(?:FY)?\d{0,2}\b|\bresults?\b|\bbeat\b|\bmiss\b"
                          r"|\bguidance\b|\bpreview\b", re.I)

# Extra names the store's tokens do not give. Measured 2026-10-22 on 49 briefs
# and 49 mail-staging files: "LTM" means LTIMindtree in 10 of 10 hits ("LTM
# IN", "[Kotak] LTM", "LTM: Weak FCF conversion"), BUT TechM's own case uses
# "LTM TCV" for last-twelve-months — a mail about TechM would otherwise count
# as LTIMindtree news. Hence the disqualifier below, not a lowercase match.
ALIASES = {"ltimindtree": ["LTIMindtree", "LTIM", "L&T Mindtree", "LTM Ltd", "LTM Limited"],
           "tech_mahindra": ["Tech Mahindra", "TechM", "Tech M"]}
LTM_METRIC = re.compile(r"\bLTM\s+(?:TCV|revenues?|EBITDA|EBIT|PAT|basis|deal\s+wins|attrition"
                        r"|period|FCF\s+(?!conversion))", re.I)


# ------------------------------------------------------------------ aliases
def patterns(ent: str, token: str | None, name: str | None, symbol: str | None) -> list[re.Pattern]:
    """Word-bounded patterns. Short all-caps tokens are case-SENSITIVE (a
    lowercase 'ltm' or 'techm' in prose is not a ticker); names are not."""
    out = []
    for a in [token, symbol, name, *ALIASES.get(ent, [])]:
        if not a:
            continue
        flags = 0 if (a.isupper() and len(a) <= 6) else re.I
        out.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", flags))
    return list({p.pattern + str(p.flags): p for p in out}.values())


def mentions(text: str, pats: list[re.Pattern]) -> bool:
    text = LTM_METRIC.sub(" ", text or "")
    return any(p.search(text) for p in pats)


# ------------------------------------------------------------------ the checks
def check_sector(conn, cfg, ent: str, as_of: str, days: int) -> dict:
    import sector_rel
    r = sector_rel.build(conn, [ent], as_of, cfg, window_days=days).get(ent) or {}
    if "withheld" in r or not r:
        return {"fired": False, "withheld": r.get("withheld", "no split"), "peers": []}
    return {**r, "fired": abs(r["rel_pct"]) > OWN_PP}


def _eps_at(conn, ent: str, fy: str, d: str):
    r = conn.execute("SELECT value_num FROM estimates WHERE entity_id=? AND broker='consensus_yahoo' "
                     "AND period=? AND metric='eps' AND as_of<=? ORDER BY as_of DESC LIMIT 1",
                     (ent, fy, d)).fetchone()
    return r[0] if r and r[0] and r[0] > 0 else None    # 0.0 is Yahoo's placeholder


def check_consensus(conn, ent: str, start: str, end: str) -> dict:
    import analysis
    e = dt.date.fromisoformat(end)
    rows = {}
    for fy in (analysis.fy_label(e), analysis.fy_label(e, 1)):
        a, b = _eps_at(conn, ent, fy, start), _eps_at(conn, ent, fy, end)
        rows[fy] = {"from": a, "to": b, "chg_pct": round((b / a - 1) * 100, 2) if a and b else None}
    have = [v["chg_pct"] for v in rows.values() if v["chg_pct"] is not None]
    if not have:
        return {"fired": False, "withheld": "no daily consensus for this name", "fy": rows}
    return {"fired": any(abs(x) >= REVISION_PCT for x in have), "fy": rows}


def _dated(path: str) -> str | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", path)
    return m.group(1) if m else None


def check_mail(conn, ent: str, pats, start: str, end: str, root: pathlib.Path) -> dict:
    hits = []
    for f in sorted(glob.glob(str(root / "data" / "morning" / "brief_*.json"))):
        d = _dated(f)
        if not d or not (start < d <= end):
            continue
        b = json.load(open(f, encoding="utf-8"))
        blocks = [(s.get("sector"), s.get("bullets") or []) for s in (b.get("mail") or {}).get("sectors") or []]
        blocks += [(k, v) for k, v in (b.get("global") or {}).items() if isinstance(v, list)]
        for sec, bullets in blocks:
            for x in bullets:
                if isinstance(x, dict) and mentions(json.dumps(x, ensure_ascii=False), pats):
                    hits.append({"date": d, "where": f"brief / {sec}", "text": x.get("text"),
                                 "source": x.get("source")})
    for f in sorted(glob.glob(str(root / "data" / "staging" / "mail_*.json"))):
        d = _dated(f)
        if not d or not (start < d <= end):
            continue
        for x in json.load(open(f, encoding="utf-8")) or []:
            if isinstance(x, dict) and mentions(f"{x.get('subject', '')} {x.get('summary', '')}", pats):
                hits.append({"date": d, "where": "mail", "text": x.get("subject"), "source": x.get("sender")})
    houses = conn.execute("SELECT broker, COUNT(*) FROM estimates WHERE entity_id=? AND as_of>? AND as_of<=? "
                          "AND broker NOT IN ('consensus_yahoo','bloomberg') GROUP BY broker",
                          (ent, start, end)).fetchall()
    try:
        views = conn.execute("SELECT firm, analyst, said_on, stance, note FROM results_analyst_views "
                             "WHERE entity_id=? AND said_on>? AND said_on<=? AND withdrawn=0",
                             (ent, start, end)).fetchall()
    except sqlite3.OperationalError:            # table created lazily by results_io
        views = []
    seen, uniq = set(), []
    for h in hits:                               # one mail can sit in two briefs
        k = (h["text"] or "")[:80]
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    return {"fired": bool(uniq or houses or views), "hits": uniq,
            "house_estimates": [{"broker": b, "rows": n} for b, n in houses],
            "analyst_views": [dict(zip(("firm", "analyst", "said_on", "stance", "note"), v)) for v in views]}


def check_positioning(conn, ent: str, start: str, end: str) -> dict:
    q = ("SELECT date, oi, price, buildup, oi_percentile, z_score_3m FROM oi WHERE entity_id=? "
         "AND date<=? AND oi IS NOT NULL ORDER BY date DESC LIMIT 1")
    a, b = conn.execute(q, (ent, start)).fetchone(), conn.execute(q, (ent, end)).fetchone()
    if not (a and b) or a[0] == b[0]:
        return {"fired": False, "withheld": "no OI history across the window"}
    chg = (b[1] / a[1] - 1) * 100 if a[1] else None
    px = (b[2] / a[2] - 1) * 100 if a[2] and b[2] else None
    # the classic four-way read of OI against price over the window
    kind = None
    if chg is not None and px is not None:
        kind = {(True, True): "long build-up", (True, False): "short build-up",
                (False, True): "short covering", (False, False): "long unwinding"}[(chg > 0, px > 0)]
    return {"fired": chg is not None and abs(chg) >= OI_CHG_PCT,
            "oi_chg_pct": None if chg is None else round(chg, 1), "price_chg_pct": None if px is None else round(px, 1),
            "window_read": kind, "latest_buildup": b[3], "oi_percentile_3m": b[4], "z_3m": b[5],
            "from": a[0], "to": b[0]}


def check_read_across(conn, cfg, ent: str, pats, sec_split: dict, start: str, end: str,
                      root: pathlib.Path) -> dict:
    sec = cfg["sector_of"].get(ent)
    out = {"bellwethers": {}, "peer_movers": [], "peer_results": []}
    for sym in BELLWETHERS.get(sec or "", []):
        # keyed on the QUOTE's own date: a weekend markets file repeats
        # Friday's close, and summing per FILE read ACN +16.0% over a week it
        # did not move that far (caught on the first live run, 2026-10-22)
        by_day = {}
        for f in sorted(glob.glob(str(root / "data" / "morning" / "markets_*.json"))):
            d = _dated(f)
            if not d or not (start < d <= end):
                continue
            m = json.load(open(f, encoding="utf-8"))
            for grp in (m.get("quotes") or {}).values():
                for qt in grp or []:
                    if qt.get("symbol") == sym and qt.get("chg_pct") is not None:
                        by_day[qt.get("date") or d] = qt["chg_pct"]
        if by_day:
            out["bellwethers"][sym] = {"sum_chg_pct": round(sum(by_day.values()), 1), "sessions": len(by_day)}
    # the sector's own biggest movers over the same window (the basket members)
    for p in sec_split.get("peers") or []:
        rows = conn.execute("SELECT date, close FROM prices WHERE entity_id=? AND date<=? AND close IS NOT NULL "
                            "ORDER BY date DESC LIMIT 1", (p, start)).fetchone(), \
               conn.execute("SELECT date, close FROM prices WHERE entity_id=? AND date<=? AND close IS NOT NULL "
                            "ORDER BY date DESC LIMIT 1", (p, end)).fetchone()
        if rows[0] and rows[1] and rows[0][1]:
            out["peer_movers"].append({"entity": p, "symbol": cfg["symbol_of"].get(p),
                                       "move_pct": round((rows[1][1] / rows[0][1] - 1) * 100, 1)})
    out["peer_movers"] = sorted(out["peer_movers"], key=lambda r: -abs(r["move_pct"]))[:3]
    # result bullets about OTHER names in the sector's brief bucket
    for f in sorted(glob.glob(str(root / "data" / "morning" / "brief_*.json"))):
        d = _dated(f)
        if not d or not (start < d <= end):
            continue
        b = json.load(open(f, encoding="utf-8"))
        for s in (b.get("mail") or {}).get("sectors") or []:
            if not _bucket_is(s.get("sector") or "", sec):
                continue
            for x in s.get("bullets") or []:
                t = x.get("text") or ""
                if RESULT_WORDS.search(t) and not mentions(t, pats):
                    out["peer_results"].append({"date": d, "text": t, "source": x.get("source")})
    out["peer_results"] = out["peer_results"][-5:]
    big_bw = any(abs(v["sum_chg_pct"]) >= BELLWETHER_PCT for v in out["bellwethers"].values())
    big_peer = any(abs(r["move_pct"]) >= PEER_MOVE_PCT for r in out["peer_movers"])
    out["fired"] = big_bw or big_peer or bool(out["peer_results"])
    return out


def _bucket_is(label: str, sec: str | None) -> bool:
    """The brief's sector buckets are display labels ("IT / AI-tech", "Non-ferrous")."""
    if not sec:
        return False
    norm = re.sub(r"[^a-z]", "", label.lower())
    return norm.startswith(re.sub(r"[^a-z]", "", sec.lower())[:4])


BLIND_SPOTS = [
    ("block_bulk", "block/bulk",
     "NOT IN THE STORE. Check NSE's bulk- and block-deal lists for the leg over the window "
     "(nseindia.com, Market Data > Bulk/Block deals, or the day's bulk.csv / block.csv)."),
    ("meetings", "meets/confs",
     "NOT IN THE STORE. Search mail (company name, 'conference', 'meet', 'NDR', 'fireside', "
     "'takeaways') and the news over the window."),
]


def label(leg: dict) -> str:
    """What the five data checks say together — a reading, never a decision."""
    sec = leg["checks"]["sector"]
    if sec.get("withheld"):
        own = None
    else:
        own = sec["rel_pct"]
    info = [k for k in ("consensus", "mail", "read_across") if leg["checks"][k].get("fired")]
    pos = leg["checks"]["positioning"].get("fired")
    if own is not None and abs(own) <= OWN_PP:
        return "moved with its sector"
    if info:
        return "stock-specific, with information: " + ", ".join(info)
    if pos:
        return "stock-specific, positioning only (flows?)"
    if own is None:
        return "no sector split — read the checks directly"
    return "stock-specific and UNEXPLAINED so far — do the two blind spots"


# ------------------------------------------------------------------ driver
def leg_report(conn, cfg, ent: str, token: str, name: str, as_of: str, days: int,
               root: pathlib.Path = REPO) -> dict:
    end = as_of
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=days)).isoformat()
    pats = patterns(ent, token, name, cfg["symbol_of"].get(ent))
    sec = check_sector(conn, cfg, ent, end, days)
    checks = {"sector": sec,
              "consensus": check_consensus(conn, ent, start, end),
              "mail": check_mail(conn, ent, pats, start, end, root),
              "positioning": check_positioning(conn, ent, start, end),
              "read_across": check_read_across(conn, cfg, ent, pats, sec, start, end, root)}
    leg = {"entity": ent, "token": token, "name": name, "window": [start, end], "days": days,
           "checks": checks, "blind_spots": [{"key": k, "check": t, "how": h} for k, t, h in BLIND_SPOTS]}
    leg["reading"] = label(leg)
    return leg


def pair_report(pair: str, days_list=(7, 30), as_of: str | None = None, conn=None) -> dict:
    import grading
    import yaml
    own = conn is None
    conn = conn or grading.connect()
    try:
        cfg = grading.load_config(conn)
        u = next((x for x in cfg["units"] if x["pair"].upper() == pair.upper()), None)
        if not u:
            raise SystemExit(f"{pair!r} is not a dictated pair (specs/book.yaml pairs)")
        names = (yaml.safe_load((REPO / "specs" / "book.yaml").read_text(encoding="utf-8")) or {}).get("names") or {}
        legs = [("long", u["long"], u["L"]), ("short", u["short"], u["S"])]
        if not as_of:
            q = ",".join("?" * 2)
            as_of = conn.execute(f"SELECT MAX(date) FROM prices WHERE entity_id IN ({q})",
                                 [u["long"], u["short"]]).fetchone()[0]
        out = {"pair": u["pair"], "as_of": as_of, "windows": []}
        for d in days_list:
            def run(at):
                legs_ = []
                for side, ent, tok in legs:
                    lr = leg_report(conn, cfg, ent, tok, names.get(tok, tok), at, d)
                    lr["side"] = side
                    legs_.append(lr)
                return legs_
            w = {"days": d, "legs": run(as_of)}
            # Both legs must be measured to the SAME close or the pair figure
            # mixes spans (TechM to 21-Oct against LTIMindtree to 19-Oct on the
            # first run: a Yahoo hole plus a sector without today's bars).
            tos = [g["checks"]["sector"].get("to") for g in w["legs"]]
            if all(tos) and len(set(tos)) > 1:
                w = {"days": d, "legs": run(min(tos)), "aligned_to": min(tos)}
            L, S = (w["legs"][0]["checks"]["sector"], w["legs"][1]["checks"]["sector"])
            if "stock_pct" in L and "stock_pct" in S:
                w["pair_move_pp"] = round(L["stock_pct"] - S["stock_pct"], 1)
                w["pair_own_pp"] = round(L["rel_pct"] - S["rel_pct"], 1)
            out["windows"].append(w)
        out["thresholds"] = {"own_pp": OWN_PP, "revision_pct": REVISION_PCT, "oi_chg_pct": OI_CHG_PCT,
                             "bellwether_pct": BELLWETHER_PCT, "peer_move_pct": PEER_MOVE_PCT}
        return out
    finally:
        if own:
            conn.close()


def render(rep: dict) -> str:
    L = [f"MOVE CHECK  {rep['pair']}  as of {rep['as_of']}  "
         f"(thresholds: own {OWN_PP}pp, revision {REVISION_PCT}%, OI {OI_CHG_PCT}%, "
         f"bellwether {BELLWETHER_PCT}%, peer {PEER_MOVE_PCT}%)"]
    for w in rep["windows"]:
        L.append("")
        L.append(f"== {w['days']} days" + (f"   pair {w['pair_move_pp']:+.1f}pp, of which own {w['pair_own_pp']:+.1f}pp"
                                            if "pair_move_pp" in w else "")
                 + (f"   (both legs aligned to {w['aligned_to']}, the last close they and their sector share)"
                    if w.get("aligned_to") else ""))
        for g in w["legs"]:
            c = g["checks"]
            L.append(f"-- {g['side'].upper()} {g['name']} ({g['window'][0]} -> {g['window'][1]}): {g['reading']}")
            s = c["sector"]
            L.append("   1 sector       " + (f"withheld: {s['withheld']}" if s.get("withheld") else
                     f"stock {s['stock_pct']:+.1f} = sector {s['sector_pct']:+.1f} (beta {s['beta']}, "
                     f"{len(s['peers'])} peers) + own {s['rel_pct']:+.1f}"
                     + (f"  [split runs to {s['to']}: the last day both the stock and a quorum of its sector printed]"
                        if s.get("to") and s["to"] != g["window"][1] else "")
                     + ("  <-" if s["fired"] else "")))
            cs = c["consensus"]
            L.append("   2 consensus    " + (f"withheld: {cs['withheld']}" if cs.get("withheld") else "  ".join(
                f"{fy} {v['from']:.2f}->{v['to']:.2f} ({v['chg_pct']:+.2f}%)" if v["chg_pct"] is not None
                else f"{fy} n/a" for fy, v in cs["fy"].items()) + ("  <-" if cs["fired"] else "")))
            m = c["mail"]
            L.append(f"   3 mail         {len(m['hits'])} mention(s), {len(m['house_estimates'])} house estimate set(s), "
                     f"{len(m['analyst_views'])} analyst view(s)" + ("  <-" if m["fired"] else ""))
            for h in m["hits"][-6:]:
                L.append(f"       {h['date']} [{h['where']}] {(h['text'] or '')[:130]}")
            p = c["positioning"]
            L.append("   4 positioning  " + (f"withheld: {p['withheld']}" if p.get("withheld") else
                     f"OI {p['oi_chg_pct']:+.1f}% vs price {p['price_chg_pct']:+.1f}% = {p['window_read']}; "
                     f"latest {p['latest_buildup']}, 3m pct {p['oi_percentile_3m']}, z {p['z_3m']}"
                     + ("  <-" if p["fired"] else "")))
            r = c["read_across"]
            bw = ", ".join(f"{k} {v['sum_chg_pct']:+.1f}%" for k, v in r["bellwethers"].items()) or "none for this sector"
            pm = ", ".join(f"{x['symbol'] or x['entity']} {x['move_pct']:+.1f}%" for x in r["peer_movers"]) or "n/a"
            L.append(f"   5 read-across  bellwethers {bw}; top peer moves {pm}; "
                     f"{len(r['peer_results'])} peer result bullet(s)" + ("  <-" if r["fired"] else ""))
            for x in r["peer_results"]:
                L.append(f"       {x['date']} {(x['text'] or '')[:130]}")
            for i, b in enumerate(g["blind_spots"], 6):
                L.append(f"   {i} {b['check'][:12]:12} {b['how'][:150]}")
    return "\n".join(L)


# ------------------------------------------------------------------ selftest
def _selftest() -> None:
    import tempfile
    ok = 0
    # aliases: acceptance beside rejection (the GLOB lesson)
    lt = patterns("ltimindtree", "LTM", "LTIMindtree", "LTM")
    tm = patterns("tech_mahindra", "TECHM", "Tech Mahindra", "TECHM")
    assert mentions("JPM stays Neutral on LTM, PT Rs4,050", lt); ok += 1
    assert mentions("BofA - LTM IN: LTM: Weak FCF conversion", lt); ok += 1
    assert not mentions("TCV $1,023m; LTM TCV at $4,270m (+34.8% YoY)", lt), "last-twelve-months is not the company"; ok += 1
    assert not mentions("on an LTM basis margins were flat", lt); ok += 1
    assert not mentions("the ltm figure", lt), "lowercase prose is not a ticker"; ok += 1
    assert mentions("Tech Mahindra wins a large comms deal", tm) and mentions("TechM 2Q beat", tm); ok += 1
    assert not mentions("TECHMAHINDRA", tm) and not mentions("FinTechMarket", tm); ok += 1

    # windows: a brief dated the window START is excluded, the END included
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "data" / "morning").mkdir(parents=True)
    (root / "data" / "staging").mkdir(parents=True)
    def brief(d, sec, text):
        json.dump({"mail": {"sectors": [{"sector": sec, "bullets": [{"text": text, "source": "X"}]}]},
                   "global": {}}, open(root / "data" / "morning" / f"brief_{d}.json", "w", encoding="utf-8"))
    brief("2026-10-10", "IT / AI-tech", "LTM soft quarter")            # = start: out
    brief("2026-10-12", "IT / AI-tech", "LTM soft quarter, guidance cut")   # in
    brief("2026-10-15", "IT / AI-tech", "Infosys 2Q beat on margins")  # peer result, in
    brief("2026-10-18", "IT / AI-tech", "LTM again")                  # after end: out
    json.dump([{"subject": "LTM: Fireside chat takeaways", "summary": ""}],
              open(root / "data" / "staging" / "mail_2026-10-14.json", "w", encoding="utf-8"))
    json.dump({"quotes": {"it": [{"symbol": "ACN", "date": "2026-10-12", "chg_pct": -2.5},
                                 {"symbol": "CTSH", "date": "2026-10-12", "chg_pct": -1.0}]}},
              open(root / "data" / "morning" / "markets_2026-10-13.json", "w", encoding="utf-8"))
    json.dump({"quotes": {"it": [{"symbol": "ACN", "date": "2026-10-13", "chg_pct": -1.0}]}},
              open(root / "data" / "morning" / "markets_2026-10-14.json", "w", encoding="utf-8"))
    # a weekend-style file repeating the same quote date must not count twice
    json.dump({"quotes": {"it": [{"symbol": "ACN", "date": "2026-10-13", "chg_pct": -1.0}]}},
              open(root / "data" / "morning" / "markets_2026-10-15.json", "w", encoding="utf-8"))
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE estimates (entity_id TEXT, broker TEXT, as_of TEXT, period TEXT, metric TEXT, value_num REAL);
      CREATE TABLE oi (entity_id TEXT, date TEXT, oi REAL, price REAL, buildup TEXT, oi_percentile REAL, z_score_3m REAL);
      CREATE TABLE prices (entity_id TEXT, date TEXT, close REAL);""")
    m = check_mail(conn, "ltimindtree", lt, "2026-10-10", "2026-10-16", root)
    assert [h["date"] for h in m["hits"]] == ["2026-10-12", "2026-10-14"] and m["fired"]; ok += 1
    cfg = {"sector_of": {"ltimindtree": "it", "x": "it"}, "symbol_of": {"x": "XX"}}
    r = check_read_across(conn, cfg, "ltimindtree", lt, {"peers": []}, "2026-10-10", "2026-10-16", root)
    assert r["bellwethers"]["ACN"] == {"sum_chg_pct": -3.5, "sessions": 2} and r["fired"]; ok += 1
    assert [x["text"] for x in r["peer_results"]] == ["Infosys 2Q beat on margins"], "own news is not read-across"; ok += 1
    # a non-IT name has no bellwethers and an empty bucket — nothing fires
    r2 = check_read_across(conn, {"sector_of": {"nmdc": "mining"}, "symbol_of": {}}, "nmdc",
                           patterns("nmdc", "NMDC", "NMDC", "NMDC"), {"peers": []}, "2026-10-10", "2026-10-16", root)
    assert r2["bellwethers"] == {} and not r2["fired"]; ok += 1

    # consensus: the change across the window, Yahoo's 0.0 placeholder refused
    conn.executemany("INSERT INTO estimates VALUES (?,?,?,?,?,?)", [
        ("x", "consensus_yahoo", "2026-10-09", "FY27", "eps", 100.0),
        ("x", "consensus_yahoo", "2026-10-16", "FY27", "eps", 98.0),
        ("x", "consensus_yahoo", "2026-10-09", "FY28", "eps", 0.0),
        ("x", "consensus_yahoo", "2026-10-16", "FY28", "eps", 120.0)])
    c = check_consensus(conn, "x", "2026-10-10", "2026-10-16")
    assert c["fy"]["FY27"]["chg_pct"] == -2.0 and c["fy"]["FY28"]["chg_pct"] is None and c["fired"]; ok += 1
    assert check_consensus(conn, "nobody", "2026-10-10", "2026-10-16").get("withheld"); ok += 1

    # positioning: OI up with price down over the window = short build-up
    conn.executemany("INSERT INTO oi VALUES (?,?,?,?,?,?,?)", [
        ("x", "2026-10-09", 1000, 100, None, 40, 0.1), ("x", "2026-10-16", 1100, 95, "short_buildup", 80, 1.2)])
    p = check_positioning(conn, "x", "2026-10-10", "2026-10-16")
    assert p["window_read"] == "short build-up" and p["oi_chg_pct"] == 10.0 and p["fired"]; ok += 1
    assert check_positioning(conn, "nobody", "2026-10-10", "2026-10-16").get("withheld"); ok += 1

    # the reading: never "noise", and the sector explanation wins first
    def leg(rel, **fired):
        base = {k: {"fired": False} for k in ("consensus", "mail", "positioning", "read_across")}
        for k, v in fired.items():
            base[k]["fired"] = v
        return {"checks": {"sector": {"rel_pct": rel, "fired": abs(rel) > OWN_PP}, **base}}
    assert label(leg(0.8, mail=True)) == "moved with its sector"; ok += 1
    assert label(leg(-6.0, consensus=True)).startswith("stock-specific, with information: consensus"); ok += 1
    assert label(leg(-6.0, positioning=True)) == "stock-specific, positioning only (flows?)"; ok += 1
    assert "UNEXPLAINED" in label(leg(-6.0)) and "noise" not in label(leg(-6.0)); ok += 1
    print(f"move_check selftest: {ok} checks passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Seven-check move report for a dictated pair.")
    ap.add_argument("pair", nargs="?")
    ap.add_argument("--days", default="7,30", help="comma-separated calendar windows (default 7,30)")
    ap.add_argument("--as-of")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    elif not a.pair:
        ap.error("give a pair name, e.g. TECHM_LTM")
    else:
        rep = pair_report(a.pair, [int(x) for x in a.days.split(",")], a.as_of)
        print(json.dumps(rep, indent=1, default=str) if a.json else render(rep))
