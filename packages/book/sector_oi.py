"""Sector positioning from futures OI, for the daily review.

PM, 2026-11-20: "an analysis on each sector, looking at the OI in the daily
review? Not a dtd, but a 1-2 month analysis that shows how each sector is in
flavour", then "keep it more concise, % numbers dont mean anything on an
individual basis on stocks. What I like: read column on a long-term basis. I
also would like weekly sharp movements in sectors".

So the output is ONE LINE PER SECTOR: a long-term read in words, and what the
last week did, flagged only when it is sharp. No per-stock percentages are
printed. Reads the `oi` table (every name on the Flows > OI page) and `fo_oi`
(the whole F&O market, for each sector's share of it). Read-only: it writes
no table and scores nothing.

THE LONG-TERM READ, per sector, over one and two expiry cycles:
  - direction = the sign of the median OI change across the sector's names
    against the sign of their average price change. Up/up longs building,
    up/down shorts building, down/up shorts covering, down/down longs leaving.
    A move inside the dead-band (OI_BAND, PX_BAND) is not given a direction.
  - flavour = the sector's share of ALL stock-futures OI by value, now against
    two cycles ago. Share is immune to the expiry sawtooth and to the market
    level, because every sector rolls on the same day.

THE ANCHORS ARE CYCLE-MATCHED, NOT "21 SESSIONS AGO". OI across all expiries
drops the session AFTER expiry (2026-10-27 expiry: Rs5.26 lakh cr that day,
4.85 the next) and rebuilds through the month. A fixed 21-session lookback
from the first day of a cycle lands on the last day of the previous one and
reads the roll as everyone leaving. The anchor is the session the same number
of sessions into the previous cycle (and the one before for two months).

THE WEEK: the same two numbers over 5 sessions, set against the sector's own
trailing 63 weekly readings. SHARP_Z (1.5 sd) or more on either is flagged.
A 5-session window that spans a roll is never read and never enters the
history: on a roll week the column says so.

Usage:
    python packages/book/sector_oi.py              # the table, latest session
    python packages/book/sector_oi.py --date 2026-11-12
    python packages/book/sector_oi.py --json
    python packages/book/sector_oi.py --selftest
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import statistics as st
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

WEEK = 5            # sessions
HIST = 63           # weekly readings behind the sharp test
MIN_HIST = 20
SHARP_Z = 1.5
OI_BAND = 3.0       # % median OI change below which OI is "unchanged"
PX_BAND = 1.5       # % average price change below which price is "flat"
SHARE_BAND = 10.0   # % relative change in market share that counts
WEEK_OI_BAND = 1.0  # the same dead-bands for a 5-session window
WEEK_PX_BAND = 0.75

ORDER = ["non_ferrous", "steel", "cement", "mining", "ems", "it", "power"]
LABEL = {"non_ferrous": "Non-ferrous", "steel": "Steel", "cement": "Cement",
         "mining": "Mining", "ems": "EMS", "it": "IT", "power": "Power"}
MERGE = {"aluminium": "non_ferrous", "zinc": "non_ferrous"}   # the tab's group
# A sector whose halves are different businesses is read by half when the
# halves disagree. Power: generation and transmission against equipment.
SUBGROUPS = {"power": {
    "utilities": ["ntpc", "tata_power", "jsw_energy", "nhpc", "adani_power",
                  "adani_green", "power_grid", "adani_energy_solutions"],
    "equipment": ["waaree_energies", "premier_energies", "suzlon",
                  "inox_wind"]}}

PHRASE = {"LB": "longs building", "SB": "shorts building",
          "SC": "shorts covering", "LU": "longs leaving",
          "OI+": "OI building on a flat price",
          "OI-": "OI leaving on a flat price",
          "PX+": "price up, positioning unchanged",
          "PX-": "price down, positioning unchanged",
          "FLAT": "no clear move"}


# ---------------------------------------------------------------- pure parts
def quadrant(d_oi: float, d_px: float, oi_band: float = OI_BAND,
             px_band: float = PX_BAND) -> str:
    oi_on, px_on = abs(d_oi) >= oi_band, abs(d_px) >= px_band
    if oi_on and px_on:
        return (("LB" if d_px > 0 else "SB") if d_oi > 0
                else ("SC" if d_px > 0 else "LU"))
    if oi_on:
        return "OI+" if d_oi > 0 else "OI-"
    if px_on:
        return "PX+" if d_px > 0 else "PX-"
    return "FLAT"


def roll_sessions(dates: list[str], expiries: list[str]) -> list[int]:
    """Index of the first session AFTER each expiry: where a new cycle
    starts and the expired contract's OI is gone."""
    out = []
    for e in sorted(expiries):
        i = next((k for k, d in enumerate(dates) if d > e), None)
        if i is not None and i > 0 and dates[i - 1] <= e:
            out.append(i)
    return sorted(set(out))


def spans_roll(start: int, end: int, rolls: list[int]) -> bool:
    """True when the change from dates[start] to dates[end] crosses a roll."""
    return any(start < r <= end for r in rolls)


def cycle_anchor(i: int, rolls: list[int], back: int) -> int | None:
    """The session as many sessions into the cycle `back` cycles ago as
    session i is into its own. Clamped to that cycle's last session."""
    starts = [r for r in rolls if r <= i]
    if len(starts) < back + 1:
        return None
    cur = starts[-1]
    prev = starts[-1 - back]
    prev_end = starts[-back] - 1
    return min(prev + (i - cur), prev_end)


def change(series: dict[str, dict[int, tuple[float, float]]], ids: list[str],
           a: int, b: int) -> dict | None:
    """Median OI change and average price change, a -> b, over the ids that
    have both ends. None under one name."""
    d_oi, d_px = [], []
    for e in ids:
        s = series.get(e) or {}
        if a in s and b in s and s[a][0] and s[a][1]:
            d_oi.append((s[b][0] / s[a][0] - 1) * 100)
            d_px.append((s[b][1] / s[a][1] - 1) * 100)
    if not d_oi:
        return None
    return {"oi": st.median(d_oi), "px": st.mean(d_px), "n": len(d_oi),
            "per_name": list(zip(d_oi, d_px))}


def breadth(ch: dict, q: str) -> int:
    """Names whose own OI and price moved the sector's way, by SIGN (band 0):
    the dead-band belongs to the sector reading, and a name a hair inside it
    still agrees with the direction."""
    return sum(1 for o, p in ch["per_name"] if quadrant(o, p, 0, 0) == q)


def week_read(series, ids, i: int, rolls: list[int]) -> dict:
    """The 5 sessions ending at i against the trailing HIST weekly readings."""
    if i - WEEK < 0:
        return {"state": "no_history"}
    if spans_roll(i - WEEK, i, rolls):
        return {"state": "roll"}
    cur = change(series, ids, i - WEEK, i)
    if cur is None:
        return {"state": "no_data"}
    hist = []
    for j in range(i - 1, max(i - 1 - HIST, WEEK - 1), -1):
        if spans_roll(j - WEEK, j, rolls):
            continue
        h = change(series, ids, j - WEEK, j)
        if h:
            hist.append((h["oi"], h["px"]))
    q = quadrant(cur["oi"], cur["px"], WEEK_OI_BAND, WEEK_PX_BAND)
    out = {"state": "ok", "q": q, "oi": cur["oi"], "px": cur["px"],
           "n": cur["n"], "same": breadth(cur, q),
           "z_oi": None, "z_px": None, "sharp": False, "n_hist": len(hist)}
    if len(hist) >= MIN_HIST:
        sd_o = st.stdev(h[0] for h in hist)
        sd_p = st.stdev(h[1] for h in hist)
        out["z_oi"] = cur["oi"] / sd_o if sd_o else None
        out["z_px"] = cur["px"] / sd_p if sd_p else None
        out["sharp"] = any(z is not None and abs(z) >= SHARP_Z
                           for z in (out["z_oi"], out["z_px"]))
    # how many consecutive earlier weeks (non-overlapping) read the same way
    run, j = 1, i - WEEK
    while j - WEEK >= 0 and q in ("LB", "SB", "SC", "LU"):
        if spans_roll(j - WEEK, j, rolls):
            break
        p = change(series, ids, j - WEEK, j)
        if not p or quadrant(p["oi"], p["px"], WEEK_OI_BAND,
                             WEEK_PX_BAND) != q:
            break
        run += 1
        j -= WEEK
    out["run"] = run
    return out


def long_read(q2: str | None, q1: str | None, share_rel: float | None,
              split: dict[str, str] | None = None) -> str:
    """The long-term column, in words."""
    if q2 is None and q1 is None:
        return "not enough history"
    if share_rel is not None and share_rel >= SHARE_BAND:
        lead = "Gaining interest"
    elif share_rel is not None and share_rel <= -SHARE_BAND:
        lead = "Losing interest"
    else:
        lead = "Steady interest"
    if split and len(set(split.values())) > 1:
        body = "split: " + ", ".join(f"{k} {PHRASE[v]}"
                                     for k, v in split.items())
    elif q2 is None or q2 == q1:
        body = PHRASE[q1]
    elif q1 is None:
        body = PHRASE[q2]
    else:
        body = (f"{PHRASE[q2]} over two months, then {PHRASE[q1]} in the "
                f"last month")
    return f"{lead}; {body}"


def week_words(w: dict) -> str:
    if w["state"] == "roll":
        return "expiry week, not read"
    if w["state"] != "ok":
        return "no reading"
    names = (("both" if w["n"] == 2 else "all " + str(w["n"]))
             if w["same"] == w["n"] else f"{w['same']} of {w['n']}")
    text = PHRASE[w["q"]]
    if w["q"] in ("LB", "SB", "SC", "LU"):
        # The sector reading is a median and a mean, so it can point a way
        # that most of its names did not go (EMS, 2026-11-19: "shorts
        # building" with one name of five doing it). Under half, say mixed.
        if w["same"] * 2 < w["n"] and not w["sharp"]:
            return "Mixed, no common move"
        text += f" ({names} names)"
        if w["run"] >= 2:
            text += f", week {w['run']} running"
    if w["sharp"]:
        z = max((abs(z), lbl) for z, lbl in ((w["z_oi"] or 0, "OI"),
                                             (w["z_px"] or 0, "price")))
        text = f"SHARP: {text}; {z[1]} move {z[0]:.1f} sd of a normal week"
    return text[0].upper() + text[1:]


# ------------------------------------------------------------------- the store
def _sectors(conn) -> dict[str, tuple[str, str]]:
    """entity id -> (sector, name) for every name with OI rows: specs first,
    then the entities table (engine.oi_snapshot's resolution)."""
    sys.path.insert(0, str(REPO / "packages" / "api"))
    try:
        import engine
        specs, _u, _f = engine.load_specs()
    except Exception:
        specs = {}
    db = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT id, name, sector FROM entities")}
    out = {}
    for (eid,) in conn.execute("SELECT DISTINCT entity_id FROM oi "
                               "WHERE oi IS NOT NULL"):
        e = specs.get(eid) or {}
        sec = e.get("sector") or (db.get(eid) or (None, None))[1] or "other"
        out[eid] = (MERGE.get(sec, sec),
                    e.get("name") or (db.get(eid) or (eid,))[0] or eid)
    return out


def build(as_of: str | None = None) -> dict:
    conn = sqlite3.connect(DB)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM fo_oi ORDER BY date")]
    if as_of:
        dates = [d for d in dates if d <= as_of]
    if len(dates) < WEEK + 2:
        conn.close()
        return {"error": "fo_oi has too little history"}
    idx = {d: k for k, d in enumerate(dates)}
    i = len(dates) - 1
    try:
        expiries = [r[0] for r in conn.execute(
            "SELECT DISTINCT expiry FROM fo_options")]
    except sqlite3.OperationalError:
        expiries = []
    rolls = roll_sessions(dates, expiries)
    sec = _sectors(conn)
    series: dict[str, dict[int, tuple[float, float]]] = {}
    for eid, d, oi, px in conn.execute(
            "SELECT entity_id, date, oi, price FROM oi WHERE oi IS NOT NULL "
            "AND price IS NOT NULL"):
        if d in idx:
            series.setdefault(eid, {})[idx[d]] = (oi, px)
    a1, a2 = cycle_anchor(i, rolls, 1), cycle_anchor(i, rolls, 2)
    tot = {k: conn.execute("SELECT SUM(oi_shares*close) FROM fo_oi WHERE "
                           "date=?", (dates[k],)).fetchone()[0]
           for k in (i, a1, a2) if k is not None}
    conn.close()

    by_sec: dict[str, list[str]] = {}
    for eid, (s, _n) in sec.items():
        by_sec.setdefault(s, []).append(eid)
    rows = []
    for s in ORDER + sorted(set(by_sec) - set(ORDER)):
        ids = by_sec.get(s)
        if not ids:
            continue
        c1 = change(series, ids, a1, i) if a1 is not None else None
        c2 = change(series, ids, a2, i) if a2 is not None else None
        q1 = quadrant(c1["oi"], c1["px"]) if c1 else None
        q2 = quadrant(c2["oi"], c2["px"]) if c2 else None
        share_rel = None
        anchor = a2 if a2 is not None else a1
        if anchor is not None:
            live = [e for e in ids if i in series.get(e, {})
                    and anchor in series[e]]
            now = sum(series[e][i][0] * series[e][i][1] for e in live)
            then = sum(series[e][anchor][0] * series[e][anchor][1]
                       for e in live)
            if then and tot.get(anchor) and tot.get(i):
                share_rel = ((now / tot[i]) / (then / tot[anchor]) - 1) * 100
        split = None
        if s in SUBGROUPS and a1 is not None:
            split = {}
            for g, gids in SUBGROUPS[s].items():
                gc = change(series, [e for e in gids if e in ids], a1, i)
                if gc:
                    split[g] = quadrant(gc["oi"], gc["px"])
        w = week_read(series, ids, i, rolls)
        short = sorted(sec[e][1] for e in ids
                       if a1 is None or a1 not in series.get(e, {})
                       or i not in series.get(e, {}))
        rows.append({"sector": s, "label": LABEL.get(s, s.title()),
                     "n": len(ids), "q_1m": q1, "q_2m": q2,
                     "share_rel_pct": share_rel, "split": split,
                     "read": long_read(q2, q1, share_rel, split),
                     "week": w, "week_text": week_words(w),
                     "left_out": short})
    return {"as_of": dates[i],
            "anchor_1m": dates[a1] if a1 is not None else None,
            "anchor_2m": dates[a2] if a2 is not None else None,
            "week_from": dates[i - WEEK], "rows": rows}


def render(r: dict) -> str:
    if r.get("error"):
        return r["error"]
    out = [f"Sector positioning, week to {r['as_of']} "
           f"(long-term read against {r['anchor_1m']} and {r['anchor_2m']}, "
           f"the same point in the last two expiry cycles)", "",
           "| Sector | 1-2 month read | This week |", "|---|---|---|"]
    for x in r["rows"]:
        out.append(f"| {x['label']} | {x['read']} | {x['week_text']} |")
    left = sorted({n for x in r["rows"] for n in x["left_out"]})
    if left:
        out += ["", "Too little futures history to count yet: "
                + ", ".join(left) + "."]
    return "\n".join(out)


# -------------------------------------------------------------------- selftest
def selftest() -> int:
    ok = True

    def check(label, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(("PASS " if cond else "FAIL ") + label)

    check("quadrant: the four directions",
          [quadrant(10, 5), quadrant(10, -5), quadrant(-10, 5),
           quadrant(-10, -5)] == ["LB", "SB", "SC", "LU"])
    check("quadrant: a move inside the dead-band gets no direction",
          quadrant(1, 5) == "PX+" and quadrant(8, 0.5) == "OI+"
          and quadrant(-8, 0.5) == "OI-" and quadrant(1, 1) == "FLAT")

    dates = [f"2026-{m:02d}-{d:02d}" for m in (1, 2, 3) for d in range(1, 21)]
    rolls = roll_sessions(dates, ["2026-01-20", "2026-02-20"])
    check("a roll is the first session AFTER expiry", rolls == [20, 40])
    check("an expiry after the last stored session makes no roll",
          roll_sessions(dates, ["2026-03-20"]) == [])
    check("a window crossing the roll is a roll window, one ending on the "
          "expiry day is not",
          spans_roll(17, 20, rolls) and not spans_roll(14, 19, rolls)
          and not spans_roll(20, 25, rolls))
    check("cycle anchor: 3 sessions into this cycle -> 3 into the last, "
          "and into the one before",
          cycle_anchor(43, rolls, 1) == 23 and cycle_anchor(43, [0, 20, 40],
                                                            2) == 3)
    check("cycle anchor clamps to the previous cycle's last session",
          cycle_anchor(48, [0, 5, 40], 1) == 13 - 0 and
          cycle_anchor(50, [30, 35, 40], 1) == 39)
    check("cycle anchor is None without enough cycles",
          cycle_anchor(43, rolls, 2) is None)

    # a sector of 3 names: quiet for 70 sessions, then OI +2%/day on a
    # falling price for the last 5 (no rolls anywhere)
    import random
    rnd = random.Random(7)
    series = {}
    for e in ("a", "b", "c"):
        oi, px, s = 1000.0, 100.0, {}
        for k in range(76):
            if k > 70:
                oi *= 1.02
                px *= 0.99
            else:
                oi *= 1 + rnd.uniform(-0.002, 0.002)
                px *= 1 + rnd.uniform(-0.002, 0.002)
            s[k] = (oi, px)
        series[e] = s
    w = week_read(series, ["a", "b", "c"], 75, [])
    check("a week of OI up on a falling price is a SHARP short build in "
          "all names", w["state"] == "ok" and w["q"] == "SB" and w["sharp"]
          and w["same"] == 3 and week_words(w).startswith("SHARP"))
    # The first draft asserted "week 60 is not sharp" on the random walk and
    # failed: one quiet week in ~7 clears 1.5 sd by construction. The code
    # was right. A week with NO move is the rejection case that is certain.
    still = {e: dict(s) for e, s in series.items()}
    for e in still:
        for k in range(71, 76):
            still[e][k] = still[e][70]
    quiet = week_read(still, ["a", "b", "c"], 75, [])
    check("a week with no move is not sharp and has no direction",
          quiet["state"] == "ok" and not quiet["sharp"]
          and quiet["q"] == "FLAT")
    check("a sector direction under half its names share reads as mixed",
          week_words({"state": "ok", "q": "SB", "n": 5, "same": 1,
                      "sharp": False, "run": 1}) == "Mixed, no common move")
    check("a week spanning a roll is not read",
          week_read(series, ["a", "b", "c"], 75, [73])["state"] == "roll"
          and week_words({"state": "roll"}) == "expiry week, not read")
    w_hist = week_read(series, ["a", "b", "c"], 75, [40])
    check("a roll inside the history drops those windows, not the reading",
          w_hist["state"] == "ok" and w_hist["n_hist"] < w["n_hist"])
    check("a name with one end missing is left out of the change",
          change({"a": {0: (10, 10), 5: (11, 11)}, "b": {5: (9, 9)}},
                 ["a", "b"], 0, 5)["n"] == 1)

    check("read: rising share, same direction in both windows",
          long_read("SB", "SB", 25.0) == "Gaining interest; shorts building")
    check("read: the two windows disagree, and it says both",
          long_read("SC", "LU", -15.0) == "Losing interest; shorts covering "
          "over two months, then longs leaving in the last month")
    check("read: halves that disagree are named, halves that agree are not",
          "split: utilities shorts covering, equipment shorts building"
          in long_read("LU", "SC", -5.0, {"utilities": "SC",
                                          "equipment": "SB"})
          and "split" not in long_read("SC", "SC", 0.0,
                                       {"utilities": "SC", "equipment": "SC"}))
    check("every SUBGROUPS sector is on ORDER and no id sits in two halves",
          all(s in ORDER for s in SUBGROUPS) and all(
              len(sum(g.values(), [])) == len(set(sum(g.values(), [])))
              for g in SUBGROUPS.values()))
    if DB.exists():
        r = build()
        got = {x["sector"] for x in r.get("rows", [])}
        check(f"live: every sector on ORDER has a row and a week reading "
              f"({r.get('as_of')})", not r.get("error")
              and set(ORDER) <= got and all(x["week_text"] for x in r["rows"]))
    else:
        print("SKIP live build (no store)")
    print("selftest", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    r = build(a.date)
    print(json.dumps(r, indent=1, default=str) if a.json else render(r))
    return 1 if r.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
