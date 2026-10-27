"""The day-end watch over every dictated leg and its case agent.

PM, 2026-10-26: "can we create an orchestrator agent that monitors all
company-agents? It will update all prices on a daily basis, check each sector
periodically for updates (say IT on tuesday, steel on wednesday etc; look at
mails, news, give feedback at market end)."

The repo's split holds: THIS SCRIPT DOES THE ARITHMETIC AND THE FLAGS, the
orchestrator skill (.claude/skills/orchestrator) does the reading — the mail
and news sweep, dispatching the case agents, the report. A Claude Code
subagent cannot start another subagent, so the orchestrator is the main
session running that skill (the `case-orchestrator` scheduled task), not one
more file in .claude/agents.

Three things, every weekday after the close:

  --prices   today's closes for every Yahoo candidate (the book legs and their
             sector peers), the same load refresh.py runs at 08:00 on a
             shorter range.
  --day      every leg: the 1-day stock / sector / own split, and for a leg
             with a case, the base/bear/bull juice re-marked at today's price
             plus the lean if the PM set one. Compared with the previous
             day-end file, it raises flags (own move, juice move, consensus
             revision, a print due, a broken build, a deal cross-check due).
             Writes data/orchestrator/day_<date>.json.
  --sector   the rota's groups for today (or named ones): per leg, the move,
             consensus change and stored mail/brief mentions since the group's
             last review, the search terms for the mail and news sweep, whether
             it has an agent. Writes data/orchestrator/sector_<date>_<slug>.json.
  --mark     records a group's review as done (data/orchestrator/state.json),
             which is where the next review's window starts.

A flag is NOT a re-mark. The PM's standing rule is "re-mark the target only on
information, never on price": a flagged move gets a pair-check, and only a
check that finds information sends the leg's agent back to its case.

Reads the store and the case files; writes only under data/orchestrator/
(and `prices`, through yahoo_prices, on --prices). Never a lean, an edge or a
call.

    python packages/book/orchestrator.py --rota
    python packages/book/orchestrator.py --prices
    python packages/book/orchestrator.py --day [--date 2026-10-26]
    python packages/book/orchestrator.py --sector [today|"IT LARGECAP,IT MIDCAP"]
    python packages/book/orchestrator.py --mark "IT LARGECAP,IT MIDCAP"
    python packages/book/orchestrator.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "adapters"))
import analysis  # noqa: E402
import case_brief as cb  # noqa: E402

REPO = analysis.REPO
OUT = REPO / "data" / "orchestrator"
STATE = OUT / "state.json"
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# --------------------------------------------------------------- config
def load_cfg() -> dict:
    return yaml.safe_load(analysis.BOOK_YAML.read_text(encoding="utf-8"))


def check_rota(cfg: dict) -> list[str]:
    """Every dictated group on exactly one weekday, and nothing else there.
    A group missing from the rota would never be reviewed and nothing else
    would notice — the silent shape — so every run refuses it."""
    groups = list((cfg.get("pairs") or {}).keys())
    rota = (cfg.get("orchestrator") or {}).get("rota") or {}
    probs, seen = [], {}
    for day, gs in rota.items():
        if day not in DAYS[:5]:
            probs.append(f"rota day {day!r} is not a weekday (Mon..Fri)")
        for g in gs or []:
            if g not in groups:
                probs.append(f"rota {day}: {g!r} is not a group in specs/book.yaml pairs")
            elif g in seen:
                probs.append(f"{g!r} sits on both {seen[g]} and {day}")
            else:
                seen[g] = day
    probs += [f"{g!r} is on no rota day, so it would never be reviewed" for g in groups if g not in seen]
    return probs


def groups_for(cfg: dict, d: dt.date) -> list[str]:
    return list(((cfg.get("orchestrator") or {}).get("rota") or {}).get(DAYS[d.weekday()]) or [])


def agent_file(token: str) -> pathlib.Path:
    return cb.AGENTS / f"{cb.agent_name(token)}.md"


def watched(legs: list[dict], has=None) -> tuple[list[dict], list[dict]]:
    """(legs with an agent, legs without). PM, 2026-10-26: "do not create
    anything for stocks with no agents build yet" — the orchestrator sweeps,
    flags and files material ONLY for a stock whose agent exists. A leg joins
    the watch the day its agent file appears; nothing here lists it by hand."""
    has = has or (lambda tok: agent_file(tok).exists())
    yes = [g for g in legs if has(g["token"])]
    return yes, [g for g in legs if not has(g["token"])]


def has_case(ent: str) -> bool:
    return (cb.CASES / ent / "build.json").exists()


def last_agent_run(conn, ent: str) -> str | None:
    r = conn.execute("SELECT MAX(created_at) FROM book_analysis WHERE entity_id=? AND via LIKE 'agent:%'",
                     (ent,)).fetchone()
    return r[0][:10] if r and r[0] else None


def live_lean(conn, ent: str) -> float | None:
    """The PM's lean, if one is set: the latest `price` entry's juice_pct."""
    for e in reversed(analysis._entries(conn, ent)):
        if e["step"] == "price":
            v = (e.get("data") or {}).get("juice_pct")
            return v if isinstance(v, (int, float)) else None
    return None


def next_print(conn, ent: str, today: dt.date) -> str | None:
    """The nearest result date not yet passed: results_calendar first, then
    the IT deal list's next_result."""
    r = conn.execute("SELECT MIN(event_date) FROM results_calendar WHERE entity_id=? AND event_date>=? "
                     "AND status!='reported'", (ent, today.isoformat())).fetchone()
    if r and r[0]:
        return r[0]
    f = cb.CASES / ent / "deals.json"
    if f.exists():
        nr = (json.loads(f.read_text(encoding="utf-8")) or {}).get("next_result")
        if nr and nr >= today.isoformat():
            return nr
    return None


# --------------------------------------------------------------- prices
def load_prices(rng: str = "1mo") -> int:
    """Every Yahoo candidate, short range: the book legs AND their sector
    peers, since the own-vs-sector split needs the basket's close too. Through
    prices_io like every other write, so Yahoo never overwrites a pack."""
    import yahoo_prices
    return yahoo_prices.load(rng)


# --------------------------------------------------------------- the day
def leg_day(conn, leg: dict, as_of: str, split: dict, today: dt.date) -> dict:
    ent = leg["entity"]
    px = analysis._close(conn, ent)
    fy1 = analysis.fy_label(today, 1)
    rec = {"entity": ent, "name": leg["name"], "token": leg["token"], "side": leg["side"],
           "group": leg["group"], "pairs": leg["pairs"],
           "close": px[1] if px else None, "close_date": px[0] if px else None,
           "agent": agent_file(leg["token"]).exists(), "case": has_case(ent),
           "last_agent_run": last_agent_run(conn, ent), "lean": live_lean(conn, ent),
           "next_print": next_print(conn, ent, today)}
    s = split.get(ent) or {}
    rec["move"] = ({k: s[k] for k in ("stock_pct", "sector_pct", "rel_pct", "from", "to")}
                   if "rel_pct" in s else {"withheld": s.get("withheld", "no split")})
    rev = cb.revisions(conn, ent, fy1)
    rec["consensus"] = {"fy": fy1, "eps": rev["eps"], "as_of": rev["as_of"]} if rev else None
    if rec["case"]:
        b = cb.brief(ent, conn, today)
        hfy = b.get("headline_fy", fy1)
        rec["headline_fy"] = hfy
        rec["build_problems"] = b.get("build_problems") or []
        rec["juice"] = {k: ((v.get("juice") or {}).get(hfy) or {}).get("juice_pct")
                        for k, v in (b.get("scenarios") or {}).items()}
        if b.get("multiple") == "EV/EBITDA":          # no Yahoo line: the build's own consensus
            c = (b.get("consensus") or {}).get(hfy)
            rec["consensus"] = {"fy": hfy, "ebitda_cr": c} if c else None
        cc = ((b.get("deals") or {}).get("cross_check")) or {}
        rec["cross_check_open"] = bool(cc.get("open"))
    return rec


def flags(cur: dict, prev: dict | None, th: dict, as_of: str, today: dt.date) -> list[str]:
    """The day's flags for one leg. Pure, so the selftest can drive it."""
    out = []
    m = cur.get("move") or {}
    if "rel_pct" in m and abs(m["rel_pct"]) >= th["move_flag_pp"]:
        out.append(f"own move {m['rel_pct']:+.1f}pp (stock {m['stock_pct']:+.1f}%, sector {m['sector_pct']:+.1f}%) "
                   f"-> pair-check {', '.join(cur['pairs'])}")
    if cur.get("close_date") and cur["close_date"] < as_of:
        out.append(f"no close for {as_of} (last {cur['close_date']})")
    if cur.get("build_problems"):
        out.append("BUILD REFUSED: " + "; ".join(cur["build_problems"])[:200])
    if cur.get("cross_check_open"):
        out.append("deal CROSS-CHECK DUE before the print")
    np_ = cur.get("next_print")
    if np_:
        days = (dt.date.fromisoformat(np_) - today).days
        if days <= th["result_soon_days"]:
            out.append(f"prints {np_} ({days}d)" if days else f"prints TODAY ({np_})")
    if prev:
        jb, pb = (cur.get("juice") or {}).get("base"), (prev.get("juice") or {}).get("base")
        if isinstance(jb, (int, float)) and isinstance(pb, (int, float)) and abs(jb - pb) >= th["juice_flag_pp"]:
            out.append(f"base juice {pb:+.1f}% -> {jb:+.1f}% since {prev.get('_date')}")
        c, p = cur.get("consensus") or {}, prev.get("consensus") or {}
        k = "eps" if "eps" in c else "ebitda_cr"
        if c.get(k) and p.get(k) and c.get("fy") == p.get("fy"):
            chg = (c[k] / p[k] - 1) * 100
            if abs(chg) >= th["revision_flag_pct"]:
                out.append(f"{c['fy']} consensus {k} {chg:+.1f}% since {prev.get('_date')}")
    return out


def previous_doc(before: str) -> dict | None:
    files = sorted(f for f in OUT.glob("day_*.json") if f.stem[4:] < before)
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def previous_day(before: str) -> dict | None:
    doc = previous_doc(before)
    if not doc:
        return None
    return {r["entity"]: {**r, "_date": doc["date"]} for r in doc["legs"]}


# --------------------------------------------------------------- watches
# PM, 2026-10-27, on UTCEM_SRCM: "It loses if the market decides Shree deserves
# UltraTech's multiple, or if petcoke normalises quickly. Watch for both through
# Q3: the November dealer price checks, Shree's trade share, and petcoke. On a
# daily basis." A watch is a named risk to one pair, with its thresholds in
# specs/book.yaml orchestrator.watches. The script computes the numbers it can
# (the multiple gap, petcoke, the pack's monthly price against each build's
# path); the skill runs the narrow mail search for dealer checks and trade
# share. A watch flag is a prompt to read, never a re-mark.
MON = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def build_path(build: dict) -> dict[str, float]:
    """{'2026-10': Rs/bag, ...} from each quarter's bridge.price_path_rs_bag.
    Only plain month keys count: 'Q2_avg' and 'Sep_exit' are the previous
    quarter's levels, and 'Sep_exit'[:3] would otherwise read as September."""
    out = {}
    for q, rec in (build.get("quarters") or {}).items():
        m = re.match(r"Q[1-4]FY(\d\d)$", q)
        path = ((rec or {}).get("bridge") or {}).get("price_path_rs_bag") or {}
        if not m or not path:
            continue
        fy = 2000 + int(m.group(1))
        for k, v in path.items():
            if "_" in k or k[:3] not in MON:
                continue
            mo = MON[k[:3]]
            out[f"{fy - 1 if mo >= 4 else fy}-{mo:02d}"] = float(v)
    return out


def pack_mix(conn, weights: dict, as_of: str) -> dict[str, dict]:
    """{'YYYY-MM': {'bag': Rs/bag, 'source': ...}} for months where every
    weighted region has a print. prices holds Rs per TONNE (cement_pack
    multiplies the sheet's Rs/bag by 20 on load), so /20 gives back the bag
    price the builds' paths use. The latest row in a month wins: the current
    month is a month-to-date average, re-stamped when it changes."""
    per = {}
    for reg in weights:
        rows = conn.execute("SELECT date, close, source FROM prices WHERE entity_id=? AND date<=? ORDER BY date",
                            (f"cement_price_{reg}_inr", as_of)).fetchall()
        per[reg] = {d[:7]: (c / 20.0, s) for d, c, s in rows}
    months = set.intersection(*(set(v) for v in per.values())) if per else set()
    tot = sum(weights.values())
    return {m: {"bag": sum(w * per[r][m][0] for r, w in weights.items()) / tot,
                "source": sorted({per[r][m][1] or "?" for r in weights})} for m in sorted(months)}


def _series_at(conn, sid: str, on: str) -> tuple[str, float] | None:
    r = conn.execute("SELECT date, close FROM prices WHERE entity_id=? AND date<=? ORDER BY date DESC LIMIT 1",
                     (sid, on)).fetchone()
    return (r[0], r[1]) if r else None


def ev_multiple(conn, ent: str, today: dt.date) -> dict | None:
    b = cb.brief(ent, conn, today)
    hfy, ev = b.get("headline_fy"), b.get("ev") or {}
    c = (b.get("consensus") or {}).get(hfy)
    if b.get("multiple") != "EV/EBITDA" or not c or not ev.get("ev_cr"):
        return None
    return {"fy": hfy, "x": ev["ev_cr"] / c, "price": ev.get("price"), "price_date": ev.get("price_date")}


def watch_numbers(conn, w: dict, as_of: str, today: dt.date) -> dict:
    out = {"pair": w["pair"], "until": w.get("until"), "why": w.get("why")}
    g = w.get("multiple_gap")
    if g:
        lo, sh = ev_multiple(conn, g["long"], today), ev_multiple(conn, g["short"], today)
        out["gap"] = ({"long": g["long"], "short": g["short"], "long_x": lo["x"], "short_x": sh["x"], "fy": lo["fy"],
                       "pct": (sh["x"] / lo["x"] - 1) * 100}
                      if lo and sh else {"withheld": "no EV/EBITDA on one leg"})
    p = w.get("petcoke")
    if p:
        now = _series_at(conn, p["series"], as_of)
        ago = _series_at(conn, p["series"], (dt.date.fromisoformat(as_of) - dt.timedelta(days=28)).isoformat())
        out["petcoke"] = ({"date": now[0], "usd": now[1], "chg_4w_pct": (now[1] / ago[1] - 1) * 100 if ago else None}
                          if now else {"withheld": f"no {p['series']} price"})
    cp = w.get("cement_price")
    if cp:
        legs = {}
        for ent, wts in (cp.get("mix") or {}).items():
            f = cb.CASES / ent / "build.json"
            path = build_path(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else {}
            mix = pack_mix(conn, wts, as_of)
            m = max(mix) if mix else None
            legs[ent] = {"month": m, "bag": mix[m]["bag"] if m else None, "source": mix[m]["source"] if m else None,
                         "path": path.get(m) if m else None, "path_months": path}
        out["cement"] = legs
    if w.get("trade_share"):
        out["trade_share"] = w["trade_share"]
    out["mail_terms"] = w.get("mail_terms") or []
    return out


def watch_flags(cur: dict, prev: dict | None, w: dict, today: dt.date) -> list[str]:
    """The day's flags for one watch. Pure, so the selftest can drive it."""
    out = []
    if w.get("until") and today.isoformat() > w["until"]:
        out.append(f"watch EXPIRED {w['until']}: extend or drop it in specs/book.yaml")
    g, gs = cur.get("gap") or {}, w.get("multiple_gap") or {}
    if "pct" in g:
        lbl = f"{g['short']} {g['short_x']:.2f}x vs {g['long']} {g['long_x']:.2f}x {g['fy']} EV/EBITDA = {g['pct']:+.1f}%"
        if g["pct"] >= gs["alert_pct"]:
            out.append(f"ALERT multiple gap at/over par: {lbl}")
        elif g["pct"] >= gs["warn_pct"]:
            out.append(f"WARN multiple gap narrowed past {gs['warn_pct']:+.0f}%: {lbl}")
        pg = ((prev or {}).get("gap") or {}).get("pct")
        if isinstance(pg, (int, float)) and abs(g["pct"] - pg) >= gs["move_pp"]:
            out.append(f"multiple gap moved {pg:+.1f}% -> {g['pct']:+.1f}% in a day")
    pc, ps = cur.get("petcoke") or {}, w.get("petcoke") or {}
    if "usd" in pc:
        if pc["usd"] <= ps["alert_usd"]:
            out.append(f"ALERT petcoke ${pc['usd']:.0f} <= ${ps['alert_usd']} (normalising)")
        elif pc["usd"] <= ps["warn_usd"]:
            out.append(f"WARN petcoke ${pc['usd']:.0f} <= ${ps['warn_usd']}")
        if isinstance(pc.get("chg_4w_pct"), (int, float)) and pc["chg_4w_pct"] <= ps["drop_pct_4w"]:
            out.append(f"petcoke {pc['chg_4w_pct']:+.1f}% in 4 weeks (fast fall)")
    warn = (w.get("cement_price") or {}).get("warn_pct")
    for ent, c in (cur.get("cement") or {}).items():
        if not c.get("month"):
            continue
        pm_ = (((prev or {}).get("cement") or {}).get(ent) or {}).get("month")
        vs = (c["bag"] / c["path"] - 1) * 100 if c.get("path") else None
        txt = f"{ent} mix {c['month']} Rs{c['bag']:.1f}/bag" + (f" vs build Rs{c['path']:.1f} ({vs:+.1f}%)" if vs is not None else "")
        if pm_ and c["month"] > pm_:
            out.append(f"NEW pack month: {txt}")
        if vs is not None and warn is not None and vs <= warn:
            out.append(f"WARN pack below the build path: {txt}")
    return out


def watches_day(conn, cfg: dict, as_of: str, today: dt.date, prev_doc: dict | None) -> list[dict]:
    prev = {x["pair"]: x for x in ((prev_doc or {}).get("watches") or [])}
    out = []
    for w in (cfg.get("orchestrator") or {}).get("watches") or []:
        if w.get("since") and today.isoformat() < w["since"]:
            continue
        cur = watch_numbers(conn, w, as_of, today)
        cur["flags"] = watch_flags(cur, prev.get(w["pair"]), w, today)
        out.append(cur)
    return out


def render_watches(ws: list[dict]) -> list[str]:
    L = []
    for x in ws:
        L += ["", f"WATCH {x['pair']} (until {x.get('until')})"]
        g = x.get("gap") or {}
        L.append(f"  multiple  " + (f"{g['short']} {g['short_x']:.2f}x / {g['long']} {g['long_x']:.2f}x {g['fy']} "
                                    f"EV/EBITDA -> gap {g['pct']:+.1f}%" if "pct" in g else g.get("withheld", "-")))
        pc = x.get("petcoke") or {}
        L.append("  petcoke   " + (f"${pc['usd']:.0f} ({pc['date']})" + (f", {pc['chg_4w_pct']:+.1f}% in 4 weeks"
                                    if isinstance(pc.get("chg_4w_pct"), (int, float)) else "")
                                    if "usd" in pc else pc.get("withheld", "-")))
        for ent, c in (x.get("cement") or {}).items():
            pm_ = c.get("path_months") or {}
            nxt = ", ".join(f"{k} (build Rs{pm_[k]:.1f})" for k in sorted(pm_) if not c.get("month") or k > c["month"])
            got = (f"{c['month']} Rs{c['bag']:.1f}/bag ({'/'.join(c['source'])})"
                   + (f" vs build Rs{c['path']:.1f}" if c.get("path") else "")) if c.get("month") else "no pack month"
            L.append(f"  pack      {ent}: {got}" + (f"; awaiting {nxt}" if nxt else ""))
        t = x.get("trade_share")
        if t:
            L.append(f"  trade     {t['entity']} {t['last_pct']}% ({t['as_of']}); >= {t['against_pair_at']}% works against "
                     f"the pair, <= {t['for_pair_at']}% for it; next: {t['next']}")
        L.append(f"  mail      search today: {' | '.join(x.get('mail_terms') or [])}")
        L += [f"  FLAG {f}" for f in x["flags"]] or ["  no watch flags"]
    return L


def day(today: dt.date | None = None, write: bool = True) -> dict:
    import grading
    import sector_rel
    today = today or dt.date.today()
    cfg = load_cfg()
    probs = check_rota(cfg)
    if probs:
        raise SystemExit("rota refused:\n  " + "\n  ".join(probs))
    th = {k: v for k, v in cfg["orchestrator"].items() if k not in ("rota", "watches")}
    legs, unwatched = watched(analysis.roster(cfg))
    if not legs:
        raise SystemExit("no leg has a case agent yet; nothing to watch")
    conn = grading.connect()
    try:
        eids = [g["entity"] for g in legs]
        q = ",".join("?" * len(eids))
        as_of = conn.execute(f"SELECT MAX(date) FROM prices WHERE entity_id IN ({q}) AND date<=?",
                             [*eids, today.isoformat()]).fetchone()[0]
        # window_days=1: the anchor is the last close BEFORE as_of, so a Monday
        # reads against Friday and a post-holiday day against the last session.
        split = sector_rel.build(conn, eids, as_of, grading.load_config(conn), window_days=1)
        prev = previous_day(today.isoformat())
        recs = []
        for g in legs:
            r = leg_day(conn, g, as_of, split, today)
            r["flags"] = flags(r, (prev or {}).get(g["entity"]), th, as_of, today)
            recs.append(r)
        watches = watches_day(conn, cfg, as_of, today, previous_doc(today.isoformat()))
    finally:
        conn.close()
    doc = {"date": today.isoformat(), "as_of": as_of, "session_today": as_of == today.isoformat(),
           "sector_day": groups_for(cfg, today), "unwatched": len(unwatched), "previous": (next(iter(prev.values()))["_date"] if prev else None),
           "legs": recs, "watches": watches}
    if write:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"day_{today.isoformat()}.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    return doc


def render_day(doc: dict) -> str:
    L = [f"DAY-END {doc['date']}  closes to {doc['as_of']}"
         + ("" if doc["session_today"] else "  (NO SESSION TODAY: holiday, or closes not in yet)")
         + f"  sector day: {', '.join(doc['sector_day']) or 'none'}"
         + f"  vs {doc['previous'] or 'no previous day-end'}",
         f"{len(doc['legs'])} legs with a case agent; {doc.get('unwatched', 0)} without one are not watched", ""]
    L.append(f"{'leg':14} {'side':5} {'stock':>6} {'sect':>6} {'own':>6}  {'bear':>6} {'base':>6} {'bull':>6} {'lean':>6}  agent")
    cur = None
    for r in doc["legs"]:
        if r["group"] != cur:
            cur = r["group"]
            L.append(f"-- {cur}")
        m = r["move"]
        f = (lambda v: f"{v:+6.1f}" if isinstance(v, (int, float)) else f"{'-':>6}")
        j = r.get("juice") or {}
        ag = ("agent" if r["agent"] else "no agent") + (f", last run {r['last_agent_run']}" if r["last_agent_run"] else "")
        L.append(f"{r['token']:14} {r['side']:5} {f(m.get('stock_pct'))} {f(m.get('sector_pct'))} {f(m.get('rel_pct'))}  "
                 f"{f(j.get('bear'))} {f(j.get('base'))} {f(j.get('bull'))} {f(r.get('lean'))}  {ag}")
    fl = [(r["token"], x) for r in doc["legs"] for x in r["flags"]]
    L += ["", f"FLAGS ({len(fl)})"] + [f"  {t:8} {x}" for t, x in fl] + ([] if fl else ["  none"])
    L += render_watches(doc.get("watches") or [])
    return "\n".join(L)


# --------------------------------------------------------------- the sector
def _slug(groups: list[str]) -> str:
    return "_".join(re.sub(r"[^a-z0-9]+", "-", g.lower()).strip("-") for g in groups)


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"sector_runs": {}}


def sector(groups: list[str], today: dt.date | None = None, write: bool = True) -> dict:
    import grading
    import move_check
    today = today or dt.date.today()
    cfg = load_cfg()
    bad = [g for g in groups if g not in (cfg.get("pairs") or {})]
    if bad:
        raise SystemExit(f"not a dictated group: {bad}")
    th = cfg["orchestrator"]
    runs = load_state()["sector_runs"]
    legs, unwatched = watched([g for g in analysis.roster(cfg) if g["group"] in groups])
    mine = {g["entity"] for g in legs}
    pairs = [p for p in analysis.dictated_pairs(cfg)        # a pair is reviewed if EITHER leg is watched
             if p["group"] in groups and mine & {*p["long"], *p["short"]}]
    conn = grading.connect()
    try:
        gcfg = grading.load_config(conn)
        out_legs = []
        for g in legs:
            ent = g["entity"]
            start = runs.get(g["group"]) or (today - dt.timedelta(days=th["default_window_days"])).isoformat()
            days = max((today - dt.date.fromisoformat(start)).days, 1)
            sym = gcfg["symbol_of"].get(ent)
            pats = move_check.patterns(ent, g["token"], g["name"], sym)
            sec = move_check.check_sector(conn, gcfg, ent, today.isoformat(), days)
            out_legs.append({
                "entity": ent, "name": g["name"], "token": g["token"], "side": g["side"], "group": g["group"],
                "pairs": g["pairs"], "window_from": start,
                "agent": cb.agent_name(g["token"]) if agent_file(g["token"]).exists() else None,
                "case": has_case(ent), "last_agent_run": last_agent_run(conn, ent),
                "next_print": next_print(conn, ent, today),
                "search_terms": sorted({x for x in [g["name"], g["token"], sym, *move_check.ALIASES.get(ent, [])] if x}),
                "move": {k: sec.get(k) for k in ("stock_pct", "sector_pct", "rel_pct", "from", "to", "withheld") if k in sec},
                "consensus": move_check.check_consensus(conn, ent, start, today.isoformat()),
                "stored_mail": move_check.check_mail(conn, ent, pats, start, today.isoformat(), REPO)})
    finally:
        conn.close()
    doc = {"date": today.isoformat(), "groups": groups, "pairs": [p["name"] for p in pairs],
           "unwatched": len(unwatched),
           "material_dir": str((OUT / "material" / today.isoformat()).relative_to(REPO)).replace("\\", "/"),
           "legs": out_legs}
    if write:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"sector_{today.isoformat()}_{_slug(groups)}.json").write_text(
            json.dumps(doc, indent=1, default=str), encoding="utf-8")
    return doc


def render_sector(doc: dict) -> str:
    if not doc["legs"]:
        return (f"SECTOR REVIEW {doc['date']}: {', '.join(doc['groups'])}: no leg has a case agent, "
                f"nothing to review ({doc['unwatched']} legs not watched). Do not --mark.")
    L = [f"SECTOR REVIEW {doc['date']}: {', '.join(doc['groups'])}  "
         f"({len(doc['legs'])} legs with an agent, {doc['unwatched']} without, not watched)",
         f"pairs: {', '.join(doc['pairs'])}", f"material -> {doc['material_dir']}/<entity>.md", ""]
    for g in doc["legs"]:
        m = g["move"]
        mv = (f"stock {m['stock_pct']:+.1f}% sector {m['sector_pct']:+.1f}% own {m['rel_pct']:+.1f}pp"
              if "rel_pct" in m else f"move withheld: {m.get('withheld')}")
        cons = [f"{fy} {v['chg_pct']:+.2f}%" for fy, v in (g["consensus"].get("fy") or {}).items()
                if v.get("chg_pct") is not None]
        sm = g["stored_mail"]
        L.append(f"{g['token']} ({g['name']}, {g['side']}, {', '.join(g['pairs'])}) since {g['window_from']}")
        L.append(f"   agent: {g['agent']}" + (f", last run {g['last_agent_run']}" if g["last_agent_run"] else "")
                 + (f"   next print {g['next_print']}" if g["next_print"] else ""))
        L.append(f"   {mv}   consensus: {', '.join(cons) or 'no daily feed'}")
        L.append(f"   stored mail/brief hits {len(sm['hits'])}, house estimates {len(sm['house_estimates'])}, "
                 f"analyst views {len(sm['analyst_views'])}")
        L.append(f"   search: {' | '.join(g['search_terms'])}")
    return "\n".join(L)


def mark(groups: list[str], today: dt.date | None = None) -> dict:
    cfg = load_cfg()
    bad = [g for g in groups if g not in (cfg.get("pairs") or {})]
    if bad:
        raise SystemExit(f"not a dictated group: {bad}")
    st = load_state()
    for g in groups:
        st["sector_runs"][g] = (today or dt.date.today()).isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")
    return st


# --------------------------------------------------------------- selftest
def _selftest() -> None:
    ok = 0
    base = {"pairs": {"A": [], "B": [], "C": []},
            "orchestrator": {"rota": {"Mon": ["A"], "Tue": ["B", "C"]}}}
    assert check_rota(base) == []; ok += 1                                  # acceptance
    miss = {**base, "orchestrator": {"rota": {"Mon": ["A"], "Tue": ["B"]}}}
    assert any("'C' is on no rota day" in p for p in check_rota(miss)); ok += 1
    dup = {**base, "orchestrator": {"rota": {"Mon": ["A", "C"], "Tue": ["B", "C"]}}}
    assert any("both Mon and Tue" in p for p in check_rota(dup)); ok += 1
    sat = {**base, "orchestrator": {"rota": {"Mon": ["A"], "Sat": ["B", "C"]}}}
    assert any("not a weekday" in p for p in check_rota(sat)); ok += 1
    ghost = {**base, "orchestrator": {"rota": {"Mon": ["A", "Z"], "Tue": ["B", "C"]}}}
    assert any("'Z' is not a group" in p for p in check_rota(ghost)); ok += 1
    assert groups_for(base, dt.date(2026, 10, 27)) == ["B", "C"] and groups_for(base, dt.date(2026, 10, 25)) == []; ok += 1
    assert check_rota(load_cfg()) == [], check_rota(load_cfg()); ok += 1     # the live spec

    th = {"move_flag_pp": 3.0, "juice_flag_pp": 1.0, "revision_flag_pct": 1.0, "result_soon_days": 7}
    t = dt.date(2026, 10, 26)
    quiet = {"pairs": ["X_Y"], "move": {"stock_pct": 2.0, "sector_pct": 1.5, "rel_pct": 0.5},
             "close_date": "2026-10-26", "juice": {"base": -6.0}, "consensus": {"fy": "FY28", "eps": 100.0}}
    prev = {"_date": "2026-10-23", "juice": {"base": -5.6}, "consensus": {"fy": "FY28", "eps": 100.5}}
    assert flags(quiet, prev, th, "2026-10-26", t) == []; ok += 1             # nothing crosses: no flag
    loud = {**quiet, "move": {"stock_pct": -5.0, "sector_pct": -1.0, "rel_pct": -4.0},
            "juice": {"base": -8.0}, "consensus": {"fy": "FY28", "eps": 98.0}, "next_print": "2026-10-29"}
    f = flags(loud, prev, th, "2026-10-26", t)
    assert len(f) == 4 and f[0].startswith("own move -4.0pp") and "pair-check X_Y" in f[0]; ok += 1
    assert any("base juice -5.6% -> -8.0%" in x for x in f) and any("consensus eps -2.5%" in x for x in f); ok += 1
    assert any(x == "prints 2026-10-29 (3d)" for x in f); ok += 1
    # a consensus that rolled to a new fiscal year is not a revision
    rolled = {**quiet, "consensus": {"fy": "FY29", "eps": 120.0}}
    assert flags(rolled, prev, th, "2026-10-26", t) == []; ok += 1
    stale = {**quiet, "close_date": "2026-10-23"}
    assert flags(stale, None, th, "2026-10-26", t) == ["no close for 2026-10-26 (last 2026-10-23)"]; ok += 1
    ev = {**quiet, "consensus": {"fy": "FY28", "ebitda_cr": 22000.0}}
    evp = {**prev, "consensus": {"fy": "FY28", "ebitda_cr": 22500.0}}
    assert any("consensus ebitda_cr -2.2%" in x for x in flags(ev, evp, th, "2026-10-26", t)); ok += 1
    assert _slug(["IT LARGECAP", "IT MIDCAP"]) == "it-largecap_it-midcap"; ok += 1
    lg = [{"token": "A"}, {"token": "B"}, {"token": "C"}]
    yes, no = watched(lg, has=lambda tk: tk != "B")
    assert [g["token"] for g in yes] == ["A", "C"] and [g["token"] for g in no] == ["B"]; ok += 1
    assert watched(lg, has=lambda tk: False) == ([], lg); ok += 1          # no agent: nothing watched

    # ---- watches (UTCEM_SRCM, PM 2026-10-27)
    bp = build_path({"quarters": {"Q3FY27": {"bridge": {"price_path_rs_bag": {
        "Q2_avg": 353.7, "Sep_exit": 358.3, "Oct": 372.7, "Nov": 371.9, "Dec": 370.7}}},
        "Q4FY27": {"bridge": {"price_path_rs_bag": {"Jan": 375.0}}}, "Q2FY27": {"kind": "actual"}}})
    # Sep_exit must NOT become 2026-09: it is last quarter's level (rejection beside acceptance)
    assert bp == {"2026-10": 372.7, "2026-11": 371.9, "2026-12": 370.7, "2027-01": 375.0}, bp; ok += 1
    import sqlite3
    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE TABLE prices (entity_id TEXT, date TEXT, close REAL, source TEXT)")
    mem.executemany("INSERT INTO prices VALUES (?,?,?,?)", [
        ("cement_price_north_inr", "2026-09-30", 7000.0, "cement_pack"),
        ("cement_price_east_inr", "2026-09-30", 7200.0, "cement_pack"),
        ("cement_price_north_inr", "2026-10-23", 7400.0, "kotak_check"),   # east has no October yet
        ("cement_price_north_inr", "2026-08-20", 6800.0, "cement_pack"),   # month-to-date, superseded
        ("cement_price_north_inr", "2026-08-31", 6900.0, "cement_pack"),
        ("cement_price_east_inr", "2026-08-31", 7100.0, "cement_pack")])
    pm = pack_mix(mem, {"north": 0.75, "east": 0.25}, "2026-10-27")
    assert list(pm) == ["2026-08", "2026-09"]; ok += 1                     # October needs both regions
    assert abs(pm["2026-09"]["bag"] - (0.75 * 350 + 0.25 * 360)) < 1e-9; ok += 1   # Rs/t / 20, weighted
    assert abs(pm["2026-08"]["bag"] - (0.75 * 345 + 0.25 * 355)) < 1e-9; ok += 1   # latest row in a month wins
    wspec = {"until": "2027-01-31", "multiple_gap": {"warn_pct": -5.0, "alert_pct": 0.0, "move_pp": 2.0},
             "petcoke": {"warn_usd": 170, "alert_usd": 150, "drop_pct_4w": -15.0}, "cement_price": {"warn_pct": -1.0}}
    gap = lambda p: {"short": "shree", "long": "ultratech", "short_x": 14.0, "long_x": 15.7, "fy": "FY28", "pct": p}
    calm = {"gap": gap(-10.9), "petcoke": {"usd": 189.0, "date": "2026-10-27", "chg_4w_pct": 17.0},
            "cement": {"shree": {"month": "2026-09", "bag": 358.3, "path": None}}}
    pcalm = {**calm}
    td = dt.date(2026, 10, 28)
    assert watch_flags(calm, pcalm, wspec, td) == []; ok += 1             # today's state: quiet
    assert watch_flags({**calm, "gap": gap(-4.0)}, pcalm, wspec, td)[0].startswith("WARN multiple gap"); ok += 1
    f2 = watch_flags({**calm, "gap": gap(0.4)}, pcalm, wspec, td)
    assert f2[0].startswith("ALERT multiple gap") and any("moved -10.9% -> +0.4%" in x for x in f2); ok += 1
    assert watch_flags({**calm, "gap": gap(-9.0)}, pcalm, wspec, td) == []; ok += 1   # 1.9pp: under move_pp
    assert watch_flags({**calm, "petcoke": {"usd": 165.0, "chg_4w_pct": -5.0}}, pcalm, wspec, td) == [
        "WARN petcoke $165 <= $170"]; ok += 1
    f3 = watch_flags({**calm, "petcoke": {"usd": 148.0, "chg_4w_pct": -20.0}}, pcalm, wspec, td)
    assert f3 == ["ALERT petcoke $148 <= $150 (normalising)", "petcoke -20.0% in 4 weeks (fast fall)"], f3; ok += 1
    octo = {**calm, "cement": {"shree": {"month": "2026-10", "bag": 366.0, "path": 372.7}}}
    f4 = watch_flags(octo, pcalm, wspec, td)
    assert len(f4) == 2 and f4[0].startswith("NEW pack month") and f4[1].startswith("WARN pack below"); ok += 1
    held = {**calm, "cement": {"shree": {"month": "2026-10", "bag": 371.0, "path": 372.7}}}
    assert [x[:14] for x in watch_flags(held, pcalm, wspec, td)] == ["NEW pack month"]; ok += 1   # -0.5%: no warn
    assert watch_flags(held, held, wspec, td) == []; ok += 1             # same month again: not new
    assert watch_flags(calm, pcalm, wspec, dt.date(2027, 2, 1))[0].startswith("watch EXPIRED"); ok += 1
    live = load_cfg()
    names = {p["name"] for p in analysis.dictated_pairs(live)}
    for w in live["orchestrator"].get("watches") or []:
        assert w["pair"] in names, f"watch on {w['pair']!r}, not a dictated pair"
        assert {"warn_pct", "alert_pct", "move_pp"} <= set(w.get("multiple_gap") or {"warn_pct": 0, "alert_pct": 0, "move_pp": 0})
    ok += 1
    print(f"orchestrator selftest: {ok} checks passed")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rota", action="store_true", help="print the weekday rota with each group's legs and agents")
    ap.add_argument("--prices", action="store_true", help="load today's closes (every Yahoo candidate)")
    ap.add_argument("--day", action="store_true", help="the day-end sweep over every leg")
    ap.add_argument("--sector", nargs="?", const="today", help='"today" (the rota) or comma-separated groups')
    ap.add_argument("--mark", help="comma-separated groups whose review is done today")
    ap.add_argument("--date", help="ISO date, default today")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    d = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    if a.selftest:
        return _selftest()
    if a.rota:
        cfg = load_cfg()
        for p in check_rota(cfg):
            print("REFUSED:", p)
        legs = analysis.roster(cfg)
        runs = load_state()["sector_runs"]
        for day_, gs in cfg["orchestrator"]["rota"].items():
            print(f"{day_}: {', '.join(gs)}")
            for g in gs:
                ls = [x for x in legs if x["group"] == g]
                n = sum(agent_file(x["token"]).exists() for x in ls)
                print(f"   {g}: {len(ls)} legs, {n} with an agent, last review {runs.get(g, 'never')}"
                      f"   [{', '.join(x['token'] + ('*' if agent_file(x['token']).exists() else '') for x in ls)}]")
        return
    if a.prices:
        print(f"prices: {load_prices()} rows written")
    if a.day:
        doc = day(d)
        print(json.dumps(doc, indent=1, default=str) if a.json else render_day(doc))
    if a.sector:
        groups = groups_for(load_cfg(), d) if a.sector == "today" else [g.strip() for g in a.sector.split(",")]
        if not groups:
            print(f"no sector on the rota for {DAYS[d.weekday()]} {d}")
        else:
            doc = sector(groups, d)
            print(json.dumps(doc, indent=1, default=str) if a.json else render_sector(doc))
    if a.mark:
        st = mark([g.strip() for g in a.mark.split(",")], d)
        print("reviewed:", json.dumps(st["sector_runs"]))


if __name__ == "__main__":
    main()
