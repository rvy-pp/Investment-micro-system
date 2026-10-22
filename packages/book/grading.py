"""Forward grading of the book's pairs — is pair SELECTION an edge?

PM, 2026-10-05, after the sizing backtests showed no timing edge: *"Can I
assume that making pairs due to the close fundamental tracking I do for
companies is the edge I can work on?"* — then, on the plan: *"go ahead, grade
at exit and 13w, 1 to 1 pair"*. This module is that plan. It picks nothing,
sizes nothing and touches no score; it keeps the record.

WHAT IS GRADED
--------------
A UNIT is one long name against one short name, as dictated in
specs/book.yaml `pairs` (a multi-leg pair would expand into every long x short
1v1 — none exists today). An EPISODE of a unit is a maximal run of STORED
snapshots on which the long token is held long AND the short token held short
(any IMS tag; options excluded). Absence from a stored snapshot, or a flip,
ends it — the same convention book_io uses for entry anchors, judged on
stored snapshot dates so a quiet fortnight between pastes cannot split one.

  entry  = the OPEN of the episode's first snapshot date (book_io's since-
           entry rule, PM 2026-09-07). An episode starting on the FIRST
           stored snapshot is flagged `pre_capture`: the real entry is
           earlier and unknown, so 04-Sep is the clock start, not the trade.
  exit   = the OPEN of the first stored snapshot without it (the position
           left during that day). `exit_gap` when sessions passed between
           the last snapshot holding it and that one.
  13w    = the last session ON OR BEFORE entry + 91 days, graded whether or
           not the pair was still held (PM: grade at exit AND at 13w — the
           pair is a thesis, and its 13 weeks say whether the thesis worked;
           the exit grade says whether the exit added to it).

THE THREE NUMBERS PER GRADE
---------------------------
spread    long return minus short return, total-return (dividends added
          back: a futures leg carries no ex-date drop, and a cash leg is paid
          it — TR is the right proxy for both; see the backtest note in git).
xs        the spread minus beta x Nifty over the window. beta is the OLS
          slope of the pair's daily spread on the Nifty over the 250 sessions
          BEFORE entry (never after — look-ahead), withheld under 60. A pair
          that wins only because it is net short beta in a falling market
          made a market call, not a selection call. HIT = xs > 0.
rank      the pair's spread percentile among EVERY ordered long/short pair
          that could have been built from the same sector's F&O names on the
          same entry open, over the same window. The set holds every pair AND
          its reverse, so it is symmetric about zero: rank > 50 exactly when
          the raw spread > 0. What rank adds is HOW MUCH of the sector's
          opportunity was captured — the best possible pick is 100 — on one
          scale across sectors and regimes. Withheld under 4 names (mining
          has 2: its rank would be 0 or 100, which is not a measurement).

FROZEN, NEVER REVISED
---------------------
A grade is written to book_pair_grades ONCE, when its end date has a close
in the store, and never updated (INSERT OR IGNORE). An open episode shows a
PROVISIONAL to-date grade computed on the fly and stored nowhere. A grade
whose window holds a >25% one-day move on a leg is WITHHELD, not written —
the store's closes are split-adjusted only as of fetch time, so a split
inside a window would read as a crash (invariant 7: withhold, do not guess).

THE CALL LOG
------------
book_calls is append-only: thesis, falsifier, conviction, expected 13w spread.
A call set on or before entry + CALL_GRACE_DAYS is `at_entry`; anything later
is `late` and is graded but reported apart — a thesis written after the move
began is not a forecast. The September cohort's calls can only be late.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import sqlite3
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "packages" / "adapters"))

HORIZON_DAYS = 91            # 13 weeks
CALL_GRACE_DAYS = 3          # a call logged at the evening review of entry day (or over a weekend) counts
BETA_LOOKBACK = 250          # sessions before entry
BETA_MIN_OBS = 60
MIN_UNIVERSE = 4             # names; below it the rank is withheld
FNO_LOOKBACK_DAYS = 30       # a symbol traded in fo_oi within this many days before entry is F&O
JUMP_LIMIT = 0.25            # a one-day close move beyond this withholds the grade
CONVICTIONS = ("low", "medium", "high")

# Sector of each spec file's roster; IT has no spec and lives in `entities`.
SPEC_SECTOR = {"aluminium.yaml": "non_ferrous", "steel.yaml": "steel",
               "cement.yaml": "cement", "mining.yaml": "mining",
               "ems.yaml": "ems"}

DDL = """
CREATE TABLE IF NOT EXISTS book_calls (
    id           INTEGER PRIMARY KEY,
    pair         TEXT NOT NULL CHECK (length(pair) > 0),
    long_token   TEXT NOT NULL CHECK (length(long_token) > 0),
    short_token  TEXT NOT NULL CHECK (length(short_token) > 0),
    set_at       TEXT NOT NULL,           -- local ISO datetime; append-only
    thesis       TEXT NOT NULL CHECK (length(trim(thesis)) > 0),
    falsifier    TEXT NOT NULL CHECK (length(trim(falsifier)) > 0),
    conviction   TEXT NOT NULL CHECK (conviction IN ('low','medium','high')),
    exp_ret_pct  REAL,                    -- expected 13w spread, %; NULL = not stated
    source       TEXT NOT NULL CHECK (source IN ('page','review','backfill'))
) STRICT;
CREATE TABLE IF NOT EXISTS book_pair_grades (
    pair         TEXT NOT NULL,
    long_eid     TEXT NOT NULL,
    short_eid    TEXT NOT NULL,
    entry_date   TEXT NOT NULL,
    horizon      TEXT NOT NULL CHECK (horizon IN ('13w','exit')),
    end_date     TEXT NOT NULL,
    long_pct     REAL NOT NULL,
    short_pct    REAL NOT NULL,
    spread_pct   REAL NOT NULL,
    nifty_pct    REAL,
    beta         REAL,
    beta_n       INTEGER,
    xs_pct       REAL,                    -- beta-stripped spread; NULL when beta withheld
    rank_pct     REAL,                    -- NULL when the universe is too small
    n_names      INTEGER NOT NULL,
    universe     TEXT NOT NULL,           -- JSON list of entity ids ranked against
    call_id      INTEGER,
    call_timing  TEXT NOT NULL CHECK (call_timing IN ('at_entry','late','none')),
    exp_ret_pct  REAL,
    flags        TEXT NOT NULL DEFAULT '[]',
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (pair, entry_date, horizon)
) STRICT;
CREATE TABLE IF NOT EXISTS equity_dividends (
    entity_id    TEXT NOT NULL,
    ex_date      TEXT NOT NULL CHECK (ex_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    amount       REAL NOT NULL CHECK (amount > 0),   -- per share, listing currency
    source       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (entity_id, ex_date)
) STRICT;
"""


def connect(db: str | None = None) -> sqlite3.Connection:
    if db is None:
        import book_io
        conn = book_io.connect()
    else:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


# ------------------------------------------------------------------ config

def load_config(conn) -> dict:
    """Units, token->entity map, entity->sector, entity->NSE symbol."""
    import yaml
    cfg = yaml.safe_load((REPO / "specs" / "book.yaml").read_text(encoding="utf-8")) or {}
    tmap = {str(k): v for k, v in (cfg.get("ticker_map") or {}).items()}
    units = []
    for sector, plist in (cfg.get("pairs") or {}).items():
        for sp in plist or []:
            L = [str(x) for x in sp.get("long") or []]
            S = [str(x) for x in sp.get("short") or []]
            for lt in L:
                for st in S:
                    name = str(sp["name"]) if (len(L), len(S)) == (1, 1) else f"{sp['name']}:{lt}/{st}"
                    units.append({"pair": name, "heading": sector, "L": lt, "S": st,
                                  "long": tmap.get(lt), "short": tmap.get(st)})
    # pairs that have left the dictation but carry a call keep being graded
    known = {u["pair"] for u in units}
    for r in conn.execute("SELECT pair, long_token, short_token, MAX(id) FROM book_calls GROUP BY pair"):
        if r[0] not in known:
            units.append({"pair": r[0], "heading": "RETIRED", "L": r[1], "S": r[2],
                          "long": tmap.get(r[1]), "short": tmap.get(r[2])})
    sector_of, symbol_of = {}, {}
    from yahoo_prices import CANDIDATES
    for f, sec in SPEC_SECTOR.items():
        p = REPO / "specs" / "entities" / f
        if not p.exists():
            continue
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for e in d.get("entities") or []:
            if isinstance(e, dict) and e.get("id"):
                sector_of[e["id"]] = sec
    for r in conn.execute("SELECT id, sector FROM entities WHERE sector='it'"):
        sector_of[r[0]] = "it"
    for eid in sector_of:
        c = CANDIDATES.get(eid)
        if c:
            symbol_of[eid] = c[0][0].replace(".NS", "")
    return {"units": units, "tmap": tmap, "sector_of": sector_of, "symbol_of": symbol_of}


# ------------------------------------------------------------------ data

class Store:
    """Prices, dividends, Nifty and the session calendar, loaded once."""

    def __init__(self, conn, eids, as_of: str | None = None):
        self.as_of = as_of or "9999-12-31"
        self.px, self.op = {}, {}
        for e in eids:
            rows = conn.execute(
                "SELECT date, open, close, volume FROM prices WHERE entity_id=? "
                "AND date<=? AND date>='2025-06-01' ORDER BY date", (e, self.as_of)).fetchall()
            # zero-volume O=H=L=C rows are Yahoo phantoms for shut days
            self.px[e] = {r[0]: r[2] for r in rows if (r[3] or 0) > 0 or r[3] is None}
            self.op[e] = {r[0]: r[1] for r in rows if r[1] is not None and ((r[3] or 0) > 0 or r[3] is None)}
        self.div = {}
        for r in conn.execute("SELECT entity_id, ex_date, amount FROM equity_dividends WHERE ex_date<=?",
                              (self.as_of,)):
            self.div.setdefault(r[0], []).append((r[1], r[2]))
        self.nifty = dict(conn.execute(
            "SELECT date, close FROM flow_series WHERE series_id='nifty' AND date<=? "
            "AND date>='2025-01-01' ORDER BY date", (self.as_of,)).fetchall())
        cal = set()
        for e in eids:
            cal |= set(self.px[e])
        self.sessions = sorted(cal)
        self.latest = self.sessions[-1] if self.sessions else None

    def tr(self, eid, d0, d1, end="close"):
        """Total return from the OPEN of d0 to the close (or open) of d1.
        Dividends with ex-date in (d0, d1] are added back; an ex-date ON d0
        is not owed, because the entry open is already ex."""
        p0 = self.op.get(eid, {}).get(d0)
        p1 = (self.px if end == "close" else self.op).get(eid, {}).get(d1)
        if not p0 or not p1:
            return None
        dv = sum(a for x, a in self.div.get(eid, []) if d0 < x <= d1)
        return (p1 + dv) / p0 - 1

    def jump(self, eid, d0, d1) -> bool:
        s = [d for d in self.sessions if d0 <= d <= d1 and d in self.px.get(eid, {})]
        c = self.px[eid]
        return any(abs(c[b] / c[a] - 1) > JUMP_LIMIT for a, b in zip(s, s[1:]))


def session_on_or_before(sessions, day):
    lo = [d for d in sessions if d <= day]
    return lo[-1] if lo else None


# ------------------------------------------------------------------ episodes

def episodes(conn, unit, as_of=None):
    """Runs of stored snapshots on which L is long and S is short."""
    import book_io
    snaps = [r[0] for r in conn.execute(
        "SELECT snap_date FROM book_snapshots WHERE snap_date<=? ORDER BY snap_date",
        (as_of or "9999-12-31",))]
    held = {}
    for d, root, side in conn.execute(
            "SELECT snap_date, root, side FROM book_positions WHERE snap_date<=?",
            (as_of or "9999-12-31",)):
        if book_io.is_option_root(root):
            continue
        held.setdefault(d, set()).add(((root.split() or [""])[0], side))
    out, cur = [], None
    for i, d in enumerate(snaps):
        live = (unit["L"], "L") in held.get(d, set()) and (unit["S"], "S") in held.get(d, set())
        if live and cur is None:
            cur = {"entry": d, "last": d, "pre_capture": i == 0}
        elif live:
            cur["last"] = d
        elif cur is not None:
            cur["exit"] = d
            out.append(cur)
            cur = None
    if cur is not None:
        cur["exit"] = None
        out.append(cur)
    return out


# ------------------------------------------------------------------ calls

def calls_for(conn, pair):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM book_calls WHERE pair=? ORDER BY set_at, id", (pair,))]


def pick_call(calls, entry):
    """The at-entry call (latest set on or before entry + grace), else the
    earliest later one, flagged late."""
    limit = (dt.date.fromisoformat(entry) + dt.timedelta(days=CALL_GRACE_DAYS)).isoformat()
    pre = [c for c in calls if c["set_at"][:10] <= limit]
    if pre:
        return pre[-1], "at_entry"
    if calls:
        return calls[0], "late"
    return None, "none"


def save_call(conn, pair, long_token, short_token, thesis, falsifier, conviction,
              exp_ret_pct=None, source="page", now=None) -> dict:
    thesis, falsifier = (thesis or "").strip(), (falsifier or "").strip()
    conviction = (conviction or "").strip().lower()
    if not thesis:
        return {"error": "a thesis is required"}
    if not falsifier:
        return {"error": "a falsifier is required (what would make this call wrong)"}
    if conviction not in CONVICTIONS:
        return {"error": f"conviction must be one of {', '.join(CONVICTIONS)}"}
    if exp_ret_pct in ("", None):
        exp = None
    else:
        try:
            exp = float(exp_ret_pct)
        except (TypeError, ValueError):
            return {"error": f"expected return {exp_ret_pct!r} is not a number"}
        if not math.isfinite(exp) or abs(exp) > 100:
            return {"error": "expected 13w spread must be a finite % within ±100"}
    if source not in ("page", "review", "backfill"):
        return {"error": f"bad source {source!r}"}
    at = now or dt.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO book_calls (pair, long_token, short_token, set_at, thesis, falsifier, "
        "conviction, exp_ret_pct, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (pair, long_token, short_token, at, thesis, falsifier, conviction, exp, source))
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "pair": pair, "set_at": at}


# ------------------------------------------------------------------ grading

def fno_names(conn, cfg, sector, entry):
    lo = (dt.date.fromisoformat(entry) - dt.timedelta(days=FNO_LOOKBACK_DAYS)).isoformat()
    live = {r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM fo_oi WHERE date BETWEEN ? AND ?", (lo, entry))}
    return sorted(e for e, s in cfg["sector_of"].items()
                  if s == sector and cfg["symbol_of"].get(e) in live)


def pair_beta(store, L, S, entry):
    before = [d for d in store.sessions if d < entry][-(BETA_LOOKBACK + 1):]
    xs, ys = [], []
    for a, b in zip(before, before[1:]):
        try:
            s = (store.px[L][b] / store.px[L][a] - 1) - (store.px[S][b] / store.px[S][a] - 1)
            n = store.nifty[b] / store.nifty[a] - 1
        except (KeyError, ZeroDivisionError):
            continue
        xs.append(n)
        ys.append(s)
    if len(xs) < BETA_MIN_OBS:
        return None, len(xs)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    if vx == 0:
        return None, len(xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx, len(xs)


def grade(conn, cfg, store, unit, ep, horizon, end_date, end="close", call=None, timing="none"):
    """One grade (frozen or provisional). Returns a dict, or {'withheld': reason}."""
    L, S, d0 = unit["long"], unit["short"], ep["entry"]
    if not L or not S:
        return {"withheld": "a leg has no entity in specs/book.yaml ticker_map"}
    flags = []
    if ep.get("pre_capture"):
        flags.append("pre_capture")
    if ep.get("exit_gap"):
        flags.append("exit_gap")
    for e in (L, S):
        if store.jump(e, d0, end_date):
            return {"withheld": f"{e} moved >{JUMP_LIMIT:.0%} in one day inside the window "
                                f"— a split or demerger reads as a crash; check before trusting"}
    rl, rs = store.tr(L, d0, end_date, end), store.tr(S, d0, end_date, end)
    if rl is None or rs is None:
        return {"withheld": f"no {'open' if end == 'open' else 'close'}/entry open for a leg "
                            f"between {d0} and {end_date}"}
    spread = rl - rs
    beta, nb = pair_beta(store, L, S, d0)
    prev = session_on_or_before([d for d in store.nifty if d < d0], d0)
    nend = session_on_or_before([d for d in store.nifty if d < end_date] if end == "open"
                                else list(store.nifty), end_date)
    npct = (store.nifty[nend] / store.nifty[prev] - 1) if prev and nend else None
    xs = (spread - beta * npct) if (beta is not None and npct is not None) else None
    if beta is None:
        flags.append("beta_withheld")
    sector = cfg["sector_of"].get(L) or cfg["sector_of"].get(S)
    names = set(fno_names(conn, cfg, sector, d0)) | {L, S}
    rets = {}
    for e in sorted(names):
        if e not in store.px:
            continue
        if store.jump(e, d0, end_date):
            continue
        r = store.tr(e, d0, end_date, end)
        if r is not None:
            rets[e] = r
    rank = None
    if len(rets) >= MIN_UNIVERSE and L in rets and S in rets:
        alts = [rets[a] - rets[b] for a in rets for b in rets if a != b and (a, b) != (L, S)]
        below = sum(1 for v in alts if v < spread)
        ties = sum(1 for v in alts if v == spread)
        rank = 100.0 * (below + 0.5 * ties) / len(alts)
    else:
        flags.append("universe_small")
    return {"pair": unit["pair"], "long_eid": L, "short_eid": S, "entry_date": d0,
            "horizon": horizon, "end_date": end_date,
            "long_pct": rl * 100, "short_pct": rs * 100, "spread_pct": spread * 100,
            "nifty_pct": None if npct is None else npct * 100, "beta": beta, "beta_n": nb,
            "xs_pct": None if xs is None else xs * 100, "rank_pct": rank,
            "n_names": len(rets), "universe": sorted(rets),
            "call_id": call["id"] if call else None, "call_timing": timing,
            "exp_ret_pct": call["exp_ret_pct"] if call else None, "flags": flags}


def _units_eids(cfg):
    return sorted({e for e in cfg["sector_of"]} | {u["long"] for u in cfg["units"] if u["long"]}
                  | {u["short"] for u in cfg["units"] if u["short"]})


def build(conn, as_of=None, freeze=False, cfg=None, store=None) -> dict:
    """Every unit's episodes with frozen / due / provisional grades."""
    cfg = cfg or load_config(conn)
    store = store or Store(conn, _units_eids(cfg), as_of)
    frozen = {(r["pair"], r["entry_date"], r["horizon"]): dict(r) for r in conn.execute(
        "SELECT * FROM book_pair_grades WHERE end_date<=?", (as_of or "9999-12-31",))}
    rows, written, withheld = [], [], []
    for u in cfg["units"]:
        calls = calls_for(conn, u["pair"])
        for ep in episodes(conn, u, as_of):
            call, timing = pick_call(calls, ep["entry"])
            if ep["exit"]:
                held_s = [d for d in store.sessions if ep["last"] < d < ep["exit"]]
                ep["exit_gap"] = len(held_s) > 0
            due13 = (dt.date.fromisoformat(ep["entry"]) + dt.timedelta(days=HORIZON_DAYS)).isoformat()
            row = {"pair": u["pair"], "heading": u["heading"], "L": u["L"], "S": u["S"],
                   "long": u["long"], "short": u["short"], "entry": ep["entry"],
                   "exit": ep["exit"], "pre_capture": ep["pre_capture"], "due_13w": due13,
                   "call": ({k: call[k] for k in ("id", "set_at", "thesis", "falsifier",
                                                  "conviction", "exp_ret_pct", "source")}
                            if call else None),
                   "call_timing": timing, "calls_n": len(calls)}
            for hz, end_d, end_kind in (
                    ("13w", session_on_or_before(store.sessions, due13)
                     if store.latest and store.latest >= due13 else None, "close"),
                    ("exit", ep["exit"] if ep["exit"] and store.latest and
                     ep["exit"] <= store.latest else None, "open")):
                key = (u["pair"], ep["entry"], hz)
                if key in frozen:
                    g = frozen[key]
                    g["universe"] = json.loads(g["universe"])
                    g["flags"] = json.loads(g["flags"])
                    g["frozen"] = True
                    row["g" + hz] = g
                    continue
                if end_d is None:
                    row["g" + hz] = None
                    continue
                g = grade(conn, cfg, store, u, ep, hz, end_d, end_kind, call, timing)
                if "withheld" in g:
                    row["g" + hz] = g
                    withheld.append((u["pair"], hz, g["withheld"]))
                    continue
                g["frozen"] = False
                if freeze:
                    conn.execute(
                        "INSERT OR IGNORE INTO book_pair_grades (pair, long_eid, short_eid, "
                        "entry_date, horizon, end_date, long_pct, short_pct, spread_pct, "
                        "nifty_pct, beta, beta_n, xs_pct, rank_pct, n_names, universe, call_id, "
                        "call_timing, exp_ret_pct, flags, computed_at) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (g["pair"], g["long_eid"], g["short_eid"], g["entry_date"], hz,
                         g["end_date"], g["long_pct"], g["short_pct"], g["spread_pct"],
                         g["nifty_pct"], g["beta"], g["beta_n"], g["xs_pct"], g["rank_pct"],
                         g["n_names"], json.dumps(g["universe"]), g["call_id"],
                         g["call_timing"], g["exp_ret_pct"], json.dumps(g["flags"]),
                         dt.datetime.now().isoformat(timespec="seconds")))
                    g["frozen"] = True
                    written.append((u["pair"], ep["entry"], hz))
                row["g" + hz] = g
            # provisional to-date, open episodes only
            row["to_date"] = None
            if not ep["exit"] and store.latest and store.latest > ep["entry"]:
                g = grade(conn, cfg, store, u, ep, "13w", store.latest, "close", call, timing)
                row["to_date"] = g
            rows.append(row)
    if freeze:
        conn.commit()
    return {"as_of": store.latest, "rows": rows, "scoreboard": scoreboard(rows),
            "written": written, "withheld": withheld,
            "rules": {"horizon_days": HORIZON_DAYS, "min_universe": MIN_UNIVERSE,
                      "beta_lookback": BETA_LOOKBACK, "beta_min_obs": BETA_MIN_OBS,
                      "call_grace_days": CALL_GRACE_DAYS}}


def _wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * (c - h), 1), round(100 * (c + h), 1)]


def scoreboard(rows) -> dict:
    """Over frozen grades only. Hit = beta-stripped spread > 0 (raw spread
    when beta is withheld, flagged). Late calls are counted apart."""
    out = {}
    for hz in ("13w", "exit"):
        gs = [r["g" + hz] for r in rows if r.get("g" + hz) and r["g" + hz].get("frozen")]
        for scope, sel in (("all", gs), ("at_entry", [g for g in gs if g["call_timing"] == "at_entry"])):
            hits = [((g["xs_pct"] if g["xs_pct"] is not None else g["spread_pct"]) > 0) for g in sel]
            ranks = [g["rank_pct"] for g in sel if g.get("rank_pct") is not None]
            n = len(sel)
            mr = sum(ranks) / len(ranks) if ranks else None
            sd = (math.sqrt(sum((x - mr) ** 2 for x in ranks) / (len(ranks) - 1))
                  if len(ranks) > 1 else None)
            out[f"{hz}_{scope}"] = {
                "n": n, "hits": sum(hits), "hit_rate": (100 * sum(hits) / n) if n else None,
                "hit_ci": _wilson(sum(hits), n), "mean_rank": mr, "n_ranked": len(ranks),
                # a percentile cannot leave 0..100; at n=2 the normal band did
                # (10.9..115 on the first two exits), so it is clamped
                "rank_ci": ([max(0.0, round(mr - 1.96 * sd / math.sqrt(len(ranks)), 1)),
                             min(100.0, round(mr + 1.96 * sd / math.sqrt(len(ranks)), 1))]
                            if sd is not None else None),
                "mean_xs": (sum(g["xs_pct"] for g in sel if g["xs_pct"] is not None)
                            / max(1, sum(1 for g in sel if g["xs_pct"] is not None))) if n else None}
    prov = [r["to_date"] for r in rows if r.get("to_date") and "withheld" not in r["to_date"]]
    out["open"] = {"n": len(prov),
                   "ahead": sum(1 for g in prov if (g["xs_pct"] if g["xs_pct"] is not None
                                                    else g["spread_pct"]) > 0)}
    return out


# ------------------------------------------------------------------ dividends

def fetch_dividends(conn, eids, rng="2y") -> dict:
    """Ex-dates and amounts from Yahoo chart events -> equity_dividends.
    Network; tolerant per symbol. Not every day matters, so run_daily calls
    this at most once per calendar day."""
    from yahoo_prices import UA, CANDIDATES
    got, bad = 0, []
    now = dt.datetime.now().isoformat(timespec="seconds")
    for e in eids:
        c = CANDIDATES.get(e)
        if not c:
            continue
        sym = c[0][0]
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}"
               f"&interval=1d&events=div")
        try:
            doc = json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20))
            r = doc["chart"]["result"][0]
            gran = (r.get("meta") or {}).get("dataGranularity")
            if gran and gran != "1d":
                raise ValueError(f"granularity {gran}")
            for ts, v in ((r.get("events") or {}).get("dividends") or {}).items():
                d = (dt.datetime.fromtimestamp(int(ts), dt.timezone.utc)
                     + dt.timedelta(hours=5, minutes=30)).date().isoformat()
                amt = float(v.get("amount") or 0)
                if amt > 0:
                    conn.execute("INSERT OR REPLACE INTO equity_dividends VALUES (?,?,?,?,?)",
                                 (e, d, amt, "yahoo:" + sym, now))
                    got += 1
        except Exception as ex:                       # noqa: BLE001
            bad.append(f"{e}: {type(ex).__name__}: {ex}")
        time.sleep(0.2)
    conn.commit()
    return {"rows": got, "failed": bad}


def run_daily(conn) -> dict:
    cfg = load_config(conn)
    last = conn.execute("SELECT MAX(fetched_at) FROM equity_dividends").fetchone()[0]
    div = None
    if not last or last[:10] < dt.date.today().isoformat():
        div = fetch_dividends(conn, _units_eids(cfg))
    res = build(conn, freeze=True, cfg=cfg)
    return {"dividends": div, "written": res["written"], "withheld": res["withheld"],
            "as_of": res["as_of"], "scoreboard": res["scoreboard"]}


# ------------------------------------------------------------------ report

def report(res) -> None:
    print(f"pair grading as of {res['as_of']}")
    for r in res["rows"]:
        td = r.get("to_date") or {}
        f = lambda v, s="": "—" if v is None else f"{v:+.2f}{s}"
        t = ("withheld: " + td["withheld"]) if "withheld" in td else (
            f"spread {f(td.get('spread_pct'), '%')}  xs {f(td.get('xs_pct'), '%')}  "
            f"rank {'—' if td.get('rank_pct') is None else f'{td['rank_pct']:.0f}'}")
        g13 = r.get("g13w")
        s13 = "due " + r["due_13w"] if not g13 else (
            "withheld" if "withheld" in g13 else f"13w xs {f(g13.get('xs_pct'), '%')} rank "
            f"{'—' if g13.get('rank_pct') is None else round(g13['rank_pct'])}")
        print(f"  {r['pair']:<14} {r['entry']}{' (pre-capture)' if r['pre_capture'] else ''}"
              f"{' exit ' + r['exit'] if r['exit'] else ''}  call {r['call_timing']:<8} "
              f"{t if not r['exit'] else ''}  | {s13}")
    sb = res["scoreboard"]
    print("scoreboard (frozen grades):", json.dumps({k: {kk: v[kk] for kk in ('n', 'hit_rate', 'mean_rank')}
                                                     for k, v in sb.items() if k != 'open'}))
    print("open episodes ahead (provisional):", sb["open"])


# ------------------------------------------------------------------ selftest

def _selftest() -> None:
    """Synthetic store. Acceptance AND rejection per rule (the GLOB lesson)."""
    ok = 0
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE prices (entity_id TEXT, date TEXT, open REAL, high REAL, low REAL,
                           close REAL, volume REAL, currency TEXT, source TEXT);
      CREATE TABLE flow_series (series_id TEXT, date TEXT, close REAL, source TEXT, captured_at TEXT);
      CREATE TABLE fo_oi (symbol TEXT, date TEXT);
      CREATE TABLE book_snapshots (snap_date TEXT);
      CREATE TABLE book_positions (snap_date TEXT, pair_tag TEXT, root TEXT, side TEXT);
    """ + DDL)
    days = [(dt.date(2025, 1, 1) + dt.timedelta(days=i)) for i in range(600)]
    days = [d.isoformat() for d in days if d.weekday() < 5]
    names = {"a": 0.0010, "b": -0.0005, "c": 0.0, "d": 0.0003, "e": -0.0002}
    # deterministic paths: name drift + a shared market wiggle; 'b' has beta 2
    lvl = {n: 100.0 for n in names}
    nif = 100.0
    for i, d in enumerate(days):
        m = 0.004 * math.sin(i / 3.0)
        nif *= 1 + m
        conn.execute("INSERT INTO flow_series VALUES ('nifty',?,?,'t','t')", (d, nif))
        for n, mu in names.items():
            beta = 2.0 if n == "b" else 1.0
            o = lvl[n]
            lvl[n] *= 1 + mu + beta * m
            conn.execute("INSERT INTO prices VALUES (?,?,?,?,?,?,1000,'INR','t')",
                         (n, d, o * 1.001, 0, 0, lvl[n]))
        conn.execute("INSERT INTO fo_oi VALUES ('A',?),('B',?),('C',?),('D',?)", (d, d, d, d))
    cfg = {"units": [{"pair": "A_B", "heading": "T", "L": "A", "S": "B", "long": "a", "short": "b"},
                     {"pair": "A_E", "heading": "T", "L": "A", "S": "E", "long": "a", "short": "e"}],
           "tmap": {}, "sector_of": {n: "t" for n in names},
           "symbol_of": {n: n.upper() for n in names}}
    snaps = days[300:330]
    for i, d in enumerate(snaps):
        conn.execute("INSERT INTO book_snapshots VALUES (?)", (d,))
        conn.execute("INSERT INTO book_positions VALUES (?,?,?,?)", (d, "T 1", "A IS Equity", "L"))
        if i < 20 or i > 22:                       # B leaves for 3 snapshots then returns
            conn.execute("INSERT INTO book_positions VALUES (?,?,?,?)", (d, "T 1", "B IS Equity", "S"))
        # the IMS option shape is MM/DD/YY — a first draft wrote 1/1/27, which
        # is_option_root rightly does not match; the fixture was wrong, not the code
        conn.execute("INSERT INTO book_positions VALUES (?,?,?,?)", (d, "T 1", "E IS 01/27/27 C100 Equity", "S"))

    import book_io as _b                         # noqa: F401  (is_option_root)
    # 1. episodes: absence splits, the second run starts after it; options ignored
    eps = episodes(conn, cfg["units"][0])
    assert [(e["entry"], e["exit"]) for e in eps] == [(snaps[0], snaps[20]), (snaps[23], None)], eps
    assert eps[0]["pre_capture"] and not eps[1]["pre_capture"]
    assert episodes(conn, cfg["units"][1]) == [], "an option leg must not make a pair live"
    ok += 3
    # 2. dividends: ex ON d0 not owed, ex inside the window added back
    st = Store(conn, list(names))
    d0, d1 = snaps[0], snaps[10]
    base = st.tr("a", d0, d1)
    conn.execute("INSERT INTO equity_dividends VALUES ('a',?,1.0,'t','t')", (d0,))
    conn.execute("INSERT INTO equity_dividends VALUES ('a',?,2.0,'t','t')", (snaps[5],))
    st = Store(conn, list(names))
    assert abs(st.tr("a", d0, d1) - (base + 2.0 / st.op["a"][d0])) < 1e-12
    ok += 1
    # 3. rank: symmetric universe -> rank > 50 iff spread > 0, and the best pair is ~100
    g = grade(conn, cfg, st, cfg["units"][0], eps[0], "exit", eps[0]["exit"], "open")
    assert g["spread_pct"] > 0 and g["rank_pct"] > 50, g
    rev = dict(cfg["units"][0], long="b", short="a", pair="B_A")
    g2 = grade(conn, cfg, st, rev, eps[0], "exit", eps[0]["exit"], "open")
    assert g2["spread_pct"] < 0 and g2["rank_pct"] < 50
    assert g["universe"] == ["a", "b", "c", "d"], g["universe"]   # 'e' is not F&O, not a leg
    ok += 3
    # 4. beta: 'b' has beta 2 and 'a' beta 1, so A_B's spread beta is ~ -1
    assert abs(g["beta"] + 1.0) < 0.05, g["beta"]
    ok += 1
    # 5. universe too small -> rank withheld, grade still given
    small = dict(cfg, sector_of={"a": "t", "b": "t"})
    g3 = grade(conn, small, st, cfg["units"][0], eps[0], "exit", eps[0]["exit"], "open")
    assert g3["rank_pct"] is None and "universe_small" in g3["flags"] and g3["spread_pct"] == g["spread_pct"]
    ok += 1
    # 6. jump guard: a 50% one-day drop inside the window withholds; outside it does not
    conn.execute("UPDATE prices SET close=close*0.5 WHERE entity_id='b' AND date>=?", (snaps[25],))
    st2 = Store(conn, list(names))
    assert "withheld" in grade(conn, cfg, st2, cfg["units"][0], eps[1], "13w", snaps[29])
    assert "withheld" not in grade(conn, cfg, st2, cfg["units"][0], eps[0], "exit", eps[0]["exit"], "open")
    ok += 2
    # 7. calls: at-entry within grace, late after it, refusals
    assert save_call(conn, "A_B", "A", "B", "", "x", "low")["error"]
    assert save_call(conn, "A_B", "A", "B", "t", " ", "low")["error"]
    assert save_call(conn, "A_B", "A", "B", "t", "f", "huge")["error"]
    assert save_call(conn, "A_B", "A", "B", "t", "f", "low", "abc")["error"]
    late = save_call(conn, "A_B", "A", "B", "late thesis", "f", "high", 5,
                     now=(dt.date.fromisoformat(snaps[0]) + dt.timedelta(days=10)).isoformat() + "T18:00:00")
    cs = calls_for(conn, "A_B")
    assert pick_call(cs, snaps[0]) == (cs[0], "late") and late["ok"]
    save_call(conn, "A_B", "A", "B", "on time", "f", "medium", 3,
              now=(dt.date.fromisoformat(snaps[0]) + dt.timedelta(days=2)).isoformat() + "T18:00:00")
    cs = calls_for(conn, "A_B")
    c, t = pick_call(cs, snaps[0])
    assert t == "at_entry" and c["thesis"] == "on time"
    assert pick_call([], snaps[0]) == (None, "none")
    ok += 6
    # 8. freezing: written once, never revised; nothing before the horizon
    conn.execute("UPDATE prices SET close=close*2 WHERE entity_id='b' AND date>=?", (snaps[25],))  # undo
    # rejection: viewed as of the last snapshot, 13w is not due yet
    asof = snaps[-1]
    res = build(conn, as_of=asof, freeze=True, cfg=cfg, store=Store(conn, list(names), asof))
    w1 = list(res["written"])
    assert any(h == "exit" for _, _, h in w1), w1
    assert not any(h == "13w" for _, _, h in w1), "13w must not grade before entry + 91 days"
    # acceptance: once the store runs past entry + 91 days, both episodes' 13w land
    res = build(conn, freeze=True, cfg=cfg, store=Store(conn, list(names)))
    assert sum(1 for _, _, h in res["written"] if h == "13w") == 2, res["written"]
    before = conn.execute("SELECT spread_pct FROM book_pair_grades WHERE horizon='exit'").fetchone()[0]
    conn.execute("UPDATE prices SET close=close*1.1 WHERE entity_id='a'")
    res2 = build(conn, freeze=True, cfg=cfg, store=Store(conn, list(names)))
    after = conn.execute("SELECT spread_pct FROM book_pair_grades WHERE horizon='exit'").fetchone()[0]
    assert before == after and not res2["written"], "a frozen grade must never be revised"
    ok += 4
    # 9. scoreboard: one frozen exit grade, counted; wilson band brackets the rate
    sb = res2["scoreboard"]["exit_all"]
    assert sb["n"] == 1 and sb["hit_ci"][0] <= sb["hit_rate"] <= sb["hit_ci"][1]
    ok += 1
    print(f"grading selftest: {ok} checks passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="fetch dividends (daily) and freeze due grades")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--fetch-divs", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--add-call", metavar="PAIR")
    ap.add_argument("--thesis")
    ap.add_argument("--falsifier")
    ap.add_argument("--conviction")
    ap.add_argument("--exp", help="expected 13w spread, %")
    ap.add_argument("--source", default="review")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        raise SystemExit(0)
    conn = connect()
    if a.add_call:
        cfg = load_config(conn)
        u = next((u for u in cfg["units"] if u["pair"] == a.add_call), None)
        if not u:
            raise SystemExit(f"{a.add_call!r} is not a dictated pair")
        print(json.dumps(save_call(conn, u["pair"], u["L"], u["S"], a.thesis, a.falsifier,
                                   a.conviction, a.exp, a.source)))
    if a.fetch_divs:
        print(json.dumps(fetch_dividends(conn, _units_eids(load_config(conn)))))
    if a.run:
        r = run_daily(conn)
        print(f"grading: {len(r['written'])} grade(s) frozen, {len(r['withheld'])} withheld"
              + (f", dividends {r['dividends']['rows']} rows" if r["dividends"] else ""))
        for w in r["withheld"]:
            print("  withheld:", *w)
    if a.report or not (a.run or a.add_call or a.fetch_divs):
        report(build(conn))
