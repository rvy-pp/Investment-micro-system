"""Flows F1 — classify every US session into a named regime; carry the odds.

The method is frozen in specs/flows.yaml (read its header first). This module
is the arithmetic: per-series daily moves normalised by their own trailing
sigma, three signs -> one of 8 named states, a quiet overlay on joint
magnitude, a rotation overlay on the SOX-IGV spread, and the empirical
transition matrix that turns "yesterday was X" into "today: 41% Y, 27% Z".

F1 NEVER SETS DIRECTION AND NEVER ENTERS SCORING. It is read by a human (and
by the review layer, later) to know which tool has edge today: in quiet
regimes the pillars carry; in loud ones flows run over fundamentals.

The transition percentages are EMPIRICAL BASE RATES over ~10 years, not a
model. They answer "given yesterday's state, how often did each state follow"
— nothing more. The matrix is recomputed from the full history on every
--persist, so it drifts as slowly as ten years of data can drift.

Usage:
    python packages/score/regime.py                 # newest read, from the table
    python packages/score/regime.py --persist       # classify all days -> market_regime
    python packages/score/regime.py --backtest      # matrix, lift, episodes
    python packages/score/regime.py --tune          # quiet_z grid (see spec: tuning protocol)
    python packages/score/regime.py --explain 2024-08-05
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "packages" / "adapters"))

import yaml  # noqa: E402

DB = REPO / "data" / "ims.db"
SPEC = REPO / "specs" / "flows.yaml"

# Order used for every printed table, worst tape last.
STATE_ORDER = ["quiet", "risk_on", "reflation", "goldilocks", "liquidity_rally",
               "risk_off", "degross", "stagflation_scare", "liquidation"]

# Dated episodes the taxonomy must not embarrass itself on. These are
# EYEBALL checks printed by --backtest, not assertions — the point is that a
# reader can see 2020-03 read as liquidation and 2024-08-05 as degross/risk_off
# before trusting the matrix. Dates are US sessions.
EPISODES = [
    ("2018-02-05", "VIX spike (XIV blow-up)"),
    ("2018-12-24", "Q4-2018 capitulation"),
    ("2020-03-09", "COVID first circuit-breaker"),
    ("2020-03-12", "COVID dash-for-cash (gold sold)"),
    ("2020-03-16", "COVID worst day"),
    ("2022-06-13", "CPI shock, 75bp repricing"),
    ("2024-08-05", "yen carry unwind"),
    ("2025-04-03", "tariff selloff day 1"),
    ("2025-04-09", "tariff pause rally"),
]


def load_spec() -> dict:
    with open(SPEC, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _series(conn: sqlite3.Connection, sid: str) -> list[tuple[str, float]]:
    return conn.execute(
        "SELECT date, close FROM flow_series WHERE series_id=? ORDER BY date",
        (sid,)).fetchall()


def _moves(rows: list[tuple[str, float]], kind: str) -> dict[str, float]:
    """date -> day move. Log return for levels; BASIS POINTS for a yield.

    A yield is already a rate: ln(4.79/4.78) would make a 1bp move at 1%
    read six times louder than the same 1bp at 6%. The market's unit for
    bond moves is bp, so bp it is.
    """
    out: dict[str, float] = {}
    for (d0, c0), (d1, c1) in zip(rows, rows[1:]):
        if kind == "yield_pct":
            out[d1] = (c1 - c0) * 100.0
        else:
            out[d1] = math.log(c1 / c0)
    return out


def _sigmas(moves: dict[str, float], window: int, min_obs: int) -> dict[str, float]:
    """date -> trailing sigma of the WINDOW moves ending t-1 — the day must
    not normalise itself, or a crash shrinks its own z."""
    dates = sorted(moves)
    out: dict[str, float] = {}
    for i, d in enumerate(dates):
        lo = max(0, i - window)
        past = [moves[x] for x in dates[lo:i]]
        if len(past) < min_obs:
            continue
        mu = sum(past) / len(past)
        sigma = math.sqrt(sum((x - mu) ** 2 for x in past) / len(past))
        if sigma > 0:
            out[d] = sigma
    return out


def _zseries(moves: dict[str, float], window: int, min_obs: int) -> dict[str, float]:
    sig = _sigmas(moves, window, min_obs)
    return {d: moves[d] / sig[d] for d in sig}


def classify_all(quiet_z: float | None = None) -> tuple[list[dict], dict]:
    """Every classifiable US session, ascending. quiet_z override is for --tune only."""
    spec = load_spec()
    p = spec["params"]
    qz = p["quiet_z"] if quiet_z is None else quiet_z

    conn = sqlite3.connect(DB)
    raw = {sid: _series(conn, sid) for sid in spec["series"]}
    conn.close()

    mv = {sid: _moves(raw[sid], spec["series"][sid]["kind"]) for sid in raw}
    z = {sid: _zseries(mv[sid], p["vol_window"], p["min_obs"]) for sid in mv}

    eq_id = spec["roles"]["eq"]
    bond_id = spec["roles"]["bond_from"]
    gold_id = spec["roles"]["gold"]
    hw_id, sw_id = spec["roles"]["rotation_pair"]

    # Rotation spread gets its own trailing sigma — SOX and IGV vols differ,
    # so normalising the raw return gap by either one alone mislabels.
    rot_moves = {d: mv[hw_id][d] - mv[sw_id][d]
                 for d in mv[hw_id] if d in mv[sw_id]}
    rot_zs = _zseries(rot_moves, p["vol_window"], p["min_obs"])

    days = []
    for d in sorted(set(z[eq_id]) & set(z[bond_id]) & set(z[gold_id])):
        z_eq = z[eq_id][d]
        z_bond = -z[bond_id][d]          # yield up = bond price down
        z_gold = z[gold_id][d]
        key = " ".join("+" if v >= 0 else "-" for v in (z_eq, z_bond, z_gold))
        loud = spec["states"][key]
        quiet = max(abs(z_eq), abs(z_bond), abs(z_gold)) < qz
        rz = rot_zs.get(d)
        rotation = None
        if rz is not None and abs(rz) >= p["rotation_z"]:
            rotation = "into_software" if rz < 0 else "into_hardware"
        days.append({
            "as_of": d, "state": "quiet" if quiet else loud, "loud_state": loud,
            "quiet": int(quiet), "z_eq": round(z_eq, 3), "z_bond": round(z_bond, 3),
            "z_gold": round(z_gold, 3),
            "rot_z": None if rz is None else round(rz, 3), "rotation": rotation,
        })

    # ---- the FLOW SPELL layer (spec: `spell`) ------------------------------
    # The coach's operative regime. Measured on the EQUITY complex only —
    # see specs/flows.yaml for why gold and bonds are scenario inputs but not
    # spell inputs, and why BOTH legs (net-flow intensity + violence guard)
    # are load-bearing.
    sp = spec["spell"]
    leg_moves = {"sp500": mv[eq_id], "sox": mv[hw_id], "rot": rot_moves}
    leg_sig = {k: _sigmas(m, p["vol_window"], p["min_obs"])
               for k, m in leg_moves.items()}
    leg_z = {k: {d: leg_moves[k][d] / s for d, s in leg_sig[k].items()}
             for k in leg_moves}
    w = sp["window"]
    axis = [d["as_of"] for d in days]
    for i, day in enumerate(days):
        d = day["as_of"]
        win = axis[max(0, i - w + 1): i + 1]
        best, driver, ok = None, None, True
        for leg in sp["series"]:
            if d not in leg_sig[leg]:
                ok = False
                break
            net = sum(leg_moves[leg][x] for x in win if x in leg_moves[leg])
            n_in = sum(1 for x in win if x in leg_moves[leg])
            inten = abs(net) / (leg_sig[leg][d] * math.sqrt(max(n_in, 1)))
            if best is None or inten > best:
                best, driver = inten, leg
        if not ok or best is None:
            day["flow_intensity"] = None
            day["flow_driver"] = None
            day["spell_quiet"] = None
            continue
        violent = any(abs(leg_z[leg].get(x, 0.0)) >= sp["violence_z"]
                      for leg in sp["series"] for x in win)
        day["flow_intensity"] = round(best, 3)
        day["flow_driver"] = driver
        day["spell_quiet"] = int(best < sp["intensity_max"] and not violent)
    return days, spec


def transitions(days: list[dict]) -> dict:
    """Counts and row-percentages of state_t -> state_t+1 over consecutive
    sessions. A gap > 7 calendar days (an unclassified stretch, not a normal
    weekend/holiday) breaks the chain rather than fabricating a transition."""
    counts: dict[str, dict[str, int]] = {}
    for a, b in zip(days, days[1:]):
        gap = (dt.date.fromisoformat(b["as_of"])
               - dt.date.fromisoformat(a["as_of"])).days
        if gap > 7:
            continue
        counts.setdefault(a["state"], {}).setdefault(b["state"], 0)
        counts[a["state"]][b["state"]] += 1
    pct = {}
    for s, row in counts.items():
        n = sum(row.values())
        pct[s] = {t: 100.0 * c / n for t, c in row.items()}
    base = {}
    total = len(days)
    for s in STATE_ORDER:
        n = sum(1 for x in days if x["state"] == s)
        if n:
            base[s] = 100.0 * n / total
    return {"counts": counts, "pct": pct, "base": base, "n_days": total}


def persist(days: list[dict], spec: dict) -> int:
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    n = 0
    for d in days:
        conn.execute(
            "INSERT INTO market_regime (as_of, state, loud_state, quiet, z_eq, "
            "z_bond, z_gold, rot_z, rotation, flow_intensity, flow_driver, "
            "spell_quiet, spec_version, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL) "
            "ON CONFLICT (as_of) DO UPDATE SET state=excluded.state, "
            "loud_state=excluded.loud_state, quiet=excluded.quiet, "
            "z_eq=excluded.z_eq, z_bond=excluded.z_bond, z_gold=excluded.z_gold, "
            "rot_z=excluded.rot_z, rotation=excluded.rotation, "
            "flow_intensity=excluded.flow_intensity, "
            "flow_driver=excluded.flow_driver, spell_quiet=excluded.spell_quiet, "
            "spec_version=excluded.spec_version, note=NULL",
            (d["as_of"], d["state"], d["loud_state"], d["quiet"], d["z_eq"],
             d["z_bond"], d["z_gold"], d["rot_z"], d["rotation"],
             d.get("flow_intensity"), d.get("flow_driver"),
             d.get("spell_quiet"), spec["version"]))
        n += 1
    conn.commit()
    conn.close()
    return n


def latest_view(days: list[dict]) -> dict:
    """The read the tab leads with: newest state, the quiet-spell share, and
    the empirical odds for the NEXT session given that state."""
    tr = transitions(days)
    last = days[-1]
    nxt = sorted(tr["pct"].get(last["state"], {}).items(),
                 key=lambda kv: -kv[1])
    n_from = sum(tr["counts"].get(last["state"], {}).values())
    recent = days[-20:]
    quiet_share = 100.0 * sum(d["quiet"] for d in recent) / len(recent)
    # How long has the current spell state held? Counted in sessions, walking
    # back until the spell flips or becomes unknown.
    spell_run = 0
    for d in reversed(days):
        if d.get("spell_quiet") is None or d["spell_quiet"] != last.get("spell_quiet"):
            break
        spell_run += 1
    return {
        "as_of": last["as_of"], "state": last["state"],
        "loud_state": last["loud_state"], "quiet": last["quiet"],
        "z_eq": last["z_eq"], "z_bond": last["z_bond"], "z_gold": last["z_gold"],
        "rot_z": last["rot_z"], "rotation": last["rotation"],
        "spell_quiet": last.get("spell_quiet"),
        "flow_intensity": last.get("flow_intensity"),
        "flow_driver": last.get("flow_driver"),
        "spell_run": spell_run,
        "quiet_share_20d": round(quiet_share, 0),
        "next": [{"state": s, "pct": round(v, 1)} for s, v in nxt],
        "n_observations": n_from,
        "base": {s: round(v, 1) for s, v in tr["base"].items()},
    }


# ---------------------------------------------------------------- weekly ----
# The read the tab leads with (PM ruling 2026-09-03: "daily is of no use,
# show a weekly analysis in the tab"). Same sign map and quiet threshold as
# the daily layer; only the sampling changes — last close of each ISO week,
# sigma over the trailing 52 completed weeks. Computed on demand from
# flow_series (~500 weeks, a few ms) rather than persisted: it is a pure
# function of stored prices + this spec, so persistence would only add a
# staleness mode.

def _wkidx(y: int, w: int) -> int:
    return dt.date.fromisocalendar(y, w, 1).toordinal() // 7


def _weekly_closes(px: dict[str, float]) -> dict[tuple, tuple]:
    """{(iso_year, iso_week): (last_date_in_week, close)}"""
    out: dict[tuple, tuple] = {}
    for d in sorted(px):
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        out[(y, w)] = (d, px[d])
    return out


def _weekly_moves(px: dict[str, float], kind: str) -> dict[tuple, float]:
    wc = _weekly_closes(px)
    keys = sorted(wc, key=lambda k: _wkidx(*k))
    out: dict[tuple, float] = {}
    for a, b in zip(keys, keys[1:]):
        if _wkidx(*b) - _wkidx(*a) > 1:
            continue  # a hole in the series must not fabricate a 2-week move
        c0, c1 = wc[a][1], wc[b][1]
        out[b] = (c1 - c0) * 100.0 if kind == "yield_pct" else math.log(c1 / c0)
    return out


def _wsigmas(mv: dict[tuple, float], window: int, min_obs: int) -> dict[tuple, float]:
    keys = sorted(mv, key=lambda k: _wkidx(*k))
    out: dict[tuple, float] = {}
    for i, k in enumerate(keys):
        past = [mv[x] for x in keys[max(0, i - window):i]]
        if len(past) < min_obs:
            continue
        mu = sum(past) / len(past)
        sd = math.sqrt(sum((x - mu) ** 2 for x in past) / len(past))
        if sd > 0:
            out[k] = sd
    return out


def classify_weeks() -> tuple[list[dict], dict, dict]:
    """Completed ISO weeks, ascending, plus the raw weekly moves for reuse.

    The newest week bucket is included only when it can take no more closes:
    its last print is on/after that week's Friday, or today (UTC) is already
    past that Friday. Otherwise it is the in-progress week — classifying it
    would stamp a state on a week that has not happened yet, the same
    partial-day rule one level up.
    """
    spec = load_spec()
    wp = spec["weekly"]
    conn = sqlite3.connect(DB)
    raw = {sid: dict(_series(conn, sid)) for sid in spec["series"]}
    conn.close()

    wmv = {sid: _weekly_moves(raw[sid], spec["series"][sid]["kind"])
           for sid in raw}
    eq, bond, gold = (spec["roles"]["eq"], spec["roles"]["bond_from"],
                      spec["roles"]["gold"])
    hw, sw = spec["roles"]["rotation_pair"]
    rot = {k: wmv[hw][k] - wmv[sw][k] for k in wmv[hw] if k in wmv[sw]}

    sig = {sid: _wsigmas(wmv[sid], wp["vol_window"], wp["min_obs"]) for sid in wmv}
    rsig = _wsigmas(rot, wp["vol_window"], wp["min_obs"])

    wc_eq = _weekly_closes(raw[eq])
    today = dt.datetime.now(dt.timezone.utc).date()

    weeks = []
    for k in sorted(set(sig[eq]) & set(sig[bond]) & set(sig[gold]),
                    key=lambda kk: _wkidx(*kk)):
        last_d = wc_eq.get(k, (None,))[0]
        if last_d is None:
            continue
        friday = dt.date.fromisocalendar(k[0], k[1], 5)
        if dt.date.fromisoformat(last_d) < friday and today <= friday:
            continue  # in-progress week
        z_eq = wmv[eq][k] / sig[eq][k]
        z_bond = -(wmv[bond][k] / sig[bond][k])
        z_gold = wmv[gold][k] / sig[gold][k]
        key = " ".join("+" if v >= 0 else "-" for v in (z_eq, z_bond, z_gold))
        loud = spec["states"][key]
        quiet = max(abs(z_eq), abs(z_bond), abs(z_gold)) < wp["quiet_z"]
        rz = (rot[k] / rsig[k]) if (k in rot and k in rsig) else None
        inten = max(abs(z_eq), abs(z_bond), abs(z_gold))
        weeks.append({
            "wk": f"{k[0]}-W{k[1]:02d}", "_k": k, "week_end": last_d,
            "state": "quiet" if quiet else loud, "loud_state": loud,
            "quiet": int(quiet),
            "z_eq": round(z_eq, 2), "z_bond": round(z_bond, 2),
            "z_gold": round(z_gold, 2),
            "rot_z": None if rz is None else round(rz, 2),
            "intensity": round(inten, 2), "grade": _grade(spec, inten),
        })
    return weeks, spec, {"moves": wmv, "sig": sig, "rot": rot, "rsig": rsig,
                         "raw": raw}


def _grade(spec: dict, v: float) -> str:
    it = spec.get("intensity") or {"moderate": 0.75, "strong": 1.5, "extreme": 2.5}
    if v >= it["extreme"]:
        return "extreme"
    if v >= it["strong"]:
        return "strong"
    if v >= it["moderate"]:
        return "moderate"
    return "faint"


def _rolling_weeks(raw: dict, sig: dict, spec: dict, n_days: int) -> list[dict]:
    """The last n US sessions, each read as a ROLLING week: the move over that
    series' own last 5 sessions, z'd against the latest completed-week sigma.

    This is the daily quantification of the weekly read — it does not wait for
    Friday. Two honesty notes baked in: consecutive readings share 4 of 5
    sessions (they autocorrelate by construction — display, never a backtest
    sample), and the sigma is the latest completed week's, so a vol regime
    change inside the current week shows up in the MOVE, not the divisor."""
    eq, bond, gold = (spec["roles"]["eq"], spec["roles"]["bond_from"],
                      spec["roles"]["gold"])
    p = spec["weekly"]
    last_sig = {}
    for sid in (eq, bond, gold):
        if not sig[sid]:
            return []
        last_sig[sid] = sig[sid][max(sig[sid], key=lambda k: _wkidx(*k))]

    cals = {sid: sorted(raw[sid]) for sid in (eq, bond, gold)}
    out = []
    for d in cals[eq][-n_days:]:
        zs = {}
        ok = True
        for sid in (eq, bond, gold):
            cal = cals[sid]
            import bisect as _b
            i = _b.bisect_right(cal, d) - 1
            if i < 5:
                ok = False
                break
            c1, c0 = raw[sid][cal[i]], raw[sid][cal[i - 5]]
            mv = ((c1 - c0) * 100.0 if spec["series"][sid]["kind"] == "yield_pct"
                  else math.log(c1 / c0))
            zs[sid] = mv / last_sig[sid]
        if not ok:
            continue
        z_eq, z_bond, z_gold = zs[eq], -zs[bond], zs[gold]
        key = " ".join("+" if v >= 0 else "-" for v in (z_eq, z_bond, z_gold))
        loud = spec["states"][key]
        inten = max(abs(z_eq), abs(z_bond), abs(z_gold))
        out.append({
            "date": d,
            "state": "quiet" if inten < p["quiet_z"] else loud,
            "loud_state": loud,
            "z_eq": round(z_eq, 2), "z_bond": round(z_bond, 2),
            "z_gold": round(z_gold, 2),
            "intensity": round(inten, 2), "grade": _grade(spec, inten),
        })
    return out


def _intensity_ladder(weeks: list[dict], spec: dict) -> dict:
    """Pooled family ladders, LIVE from the store: next Indian week after an
    up-family / down-family week at each grade. Computed rather than pasted so
    the numbers age with the data instead of fossilising a backtest."""
    conn = sqlite3.connect(DB)
    px = dict(_series(conn, "nifty"))
    conn.close()
    if not px:
        return {}
    nmv = _weekly_moves(px, "level")
    by_idx = {_wkidx(*k): v * 100.0 for k, v in nmv.items()}
    UP = ("risk_on", "reflation", "goldilocks", "liquidity_rally")
    DOWN = ("risk_off", "degross", "stagflation_scare", "liquidation")
    out = {}
    for fam, group in (("up", UP), ("down", DOWN)):
        fam_out = {}
        for g in ("moderate", "strong", "extreme"):
            v = [by_idx.get(_wkidx(*w["_k"]) + 1) for w in weeks
                 if w["loud_state"] in group and not w["quiet"]
                 and w.get("grade") == g]
            v = [x for x in v if x is not None]
            if len(v) < 10:
                fam_out[g] = {"n": len(v)}
                continue
            m = sum(v) / len(v)
            sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
            fam_out[g] = {"n": len(v), "mean": round(m, 2),
                          "hit": round(100.0 * sum(1 for x in v if x > 0) / len(v), 0),
                          "t": round(m / (sd / math.sqrt(len(v))), 1) if sd else None}
        out[fam] = fam_out
    return out


def weekly_transitions(weeks: list[dict]) -> dict:
    counts: dict[str, dict[str, int]] = {}
    for a, b in zip(weeks, weeks[1:]):
        if _wkidx(*b["_k"]) - _wkidx(*a["_k"]) > 1:
            continue
        counts.setdefault(a["state"], {}).setdefault(b["state"], 0)
        counts[a["state"]][b["state"]] += 1
    pct = {s: {t: 100.0 * c / sum(r.values()) for t, c in r.items()}
           for s, r in counts.items()}
    base = {}
    for s in STATE_ORDER:
        n = sum(1 for x in weeks if x["state"] == s)
        if n:
            base[s] = 100.0 * n / len(weeks)
    return {"counts": counts, "pct": pct, "base": base, "n_weeks": len(weeks)}


def _india_next_week(weeks: list[dict], spec: dict, min_n: int) -> dict:
    """Per state, the following ISO week's return on each India index —
    computed LIVE from flow_series so it heals as data accrues. A series
    missing recent weeks (nifty_metal goes stale on Yahoo for weeks at a
    time) simply contributes fewer pairs, never zeros."""
    conn = sqlite3.connect(DB)
    out: dict[str, dict] = {}
    for sid, scfg in (spec.get("india_series") or {}).items():
        if not scfg.get("evidence"):
            continue  # tape-only indices; nine evidence tables would be noise
        px = dict(_series(conn, sid))
        if not px:
            continue
        mv = _weekly_moves(px, "level")
        by_idx = {_wkidx(*k): v for k, v in mv.items()}
        per_state: dict[str, list] = {}
        for w in weeks:
            nxt = by_idx.get(_wkidx(*w["_k"]) + 1)
            if nxt is not None:
                per_state.setdefault(w["state"], []).append(nxt * 100.0)
        stats = {}
        for s, v in per_state.items():
            if len(v) < min_n:
                continue
            m = sum(v) / len(v)
            sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
            stats[s] = {"n": len(v), "mean": round(m, 2),
                        "hit": round(100.0 * sum(1 for x in v if x > 0) / len(v), 0),
                        "t": round(m / (sd / math.sqrt(len(v))), 1) if sd else None}
        out[sid] = {"stats": stats, "last_date": max(px)}
    conn.close()
    return out


def sector_tape(weeks: list[dict], spec: dict) -> dict:
    """Per-index weekly performance for the coverage sector tape — the first
    computed sliver of F2 (which sectors are being worked). All rows are
    anchored to the SAME ISO week (the regime's last completed one), so a
    series with no print that week shows a dash and its own last date in
    amber, never a silently older number. rel = minus the Nifty's same-week
    move — relative strength, the coach's sector-flow read."""
    last = weeks[-1]
    i = _wkidx(*last["_k"])
    conn = sqlite3.connect(DB)
    rows = []
    for sid, scfg in (spec.get("india_series") or {}).items():
        px = dict(_series(conn, sid))
        if not px:
            continue
        mv = _weekly_moves(px, "level")
        by_idx = {_wkidx(*k): v for k, v in mv.items()}
        wk = by_idx.get(i)
        wk4 = (sum(by_idx[j] for j in range(i - 3, i + 1)) * 100.0
               if all(j in by_idx for j in range(i - 3, i + 1)) else None)
        wc = _weekly_closes(px)
        prev, newest = wc.get(last["_k"]), max(px)
        wtd = (math.log(px[newest] / prev[1]) * 100.0
               if prev and newest > prev[0] else None)
        rows.append({
            "sid": sid, "label": scfg.get("label", sid),
            "covers": scfg.get("covers", ""),
            "wk_pct": None if wk is None else round(wk * 100.0, 2),
            "wk4_pct": None if wk4 is None else round(wk4, 2),
            "wtd_pct": None if wtd is None else round(wtd, 2),
            "last_date": newest,
            "stale": newest < last["week_end"],
        })
    conn.close()
    bench = next((r["wk_pct"] for r in rows if r["sid"] == "nifty"), None)
    for r in rows:
        r["rel_pp"] = (round(r["wk_pct"] - bench, 2)
                       if (bench is not None and r["wk_pct"] is not None
                           and r["sid"] != "nifty") else None)
    rows.sort(key=lambda r: (r["wk_pct"] is None, -(r["wk_pct"] or 0)))
    return {"week": last["wk"], "week_end": last["week_end"], "rows": rows}


def weekly_view() -> dict:
    """Everything the tab's weekly panel renders. JSON-safe."""
    weeks, spec, ctx = classify_weeks()
    if not weeks:
        return {"error": "no classified weeks - run flow_series.py --load"}
    tr = weekly_transitions(weeks)
    last = weeks[-1]

    nxt = sorted(tr["pct"].get(last["state"], {}).items(), key=lambda kv: -kv[1])
    india = _india_next_week(weeks, spec, spec["weekly"]["min_stat_n"])

    # ROLLING week (PM, 2026-09-04: "show quantification on a daily basis,
    # rolling window method"): each of the last N sessions read as a full
    # 5-session weekly window — replaces the old partial week-to-date chip,
    # which mixed 1-session and 4-session "weeks" into one z scale.
    rolling = _rolling_weeks(ctx["raw"], ctx["sig"], spec,
                             spec["weekly"].get("rolling_days", 10))

    stale_days = (dt.date.today()
                  - dt.date.fromisoformat(last["week_end"])).days
    return {
        "spec_version": spec["version"],
        "week": last["wk"], "week_end": last["week_end"],
        "state": last["state"], "loud_state": last["loud_state"],
        "z_eq": last["z_eq"], "z_bond": last["z_bond"],
        "z_gold": last["z_gold"], "rot_z": last["rot_z"],
        "intensity": last["intensity"], "grade": last["grade"],
        "stale_days": stale_days, "stale": stale_days > 10,
        "rolling": rolling,
        "ladder": _intensity_ladder(weeks, spec),
        "next": [{"state": s, "pct": round(v, 1),
                  "base": round(tr["base"].get(s, 0.0), 1)} for s, v in nxt],
        "n_observations": sum(tr["counts"].get(last["state"], {}).values()),
        "n_weeks": tr["n_weeks"], "first": weeks[0]["wk"],
        "persistence": {s: round(tr["pct"][s].get(s, 0.0) / tr["base"][s], 2)
                        for s in tr["pct"] if tr["base"].get(s)},
        "india": india,
        "sector_tape": sector_tape(weeks, spec),
        "strip": [{"wk": w["wk"], "state": w["state"]} for w in weeks[-52:]],
    }


# --------------------------------------------------------------- printing ----

def _fmt_day(d: dict) -> str:
    rot = f"  rot {d['rot_z']:+.2f} {d['rotation'] or ''}" if d["rot_z"] is not None else ""
    sq = d.get("spell_quiet")
    spell = "" if sq is None else ("  [quiet spell]" if sq else
                                   f"  [flow: {d.get('flow_driver')} "
                                   f"{d.get('flow_intensity'):.1f}]")
    return (f"{d['as_of']}  {d['state']:17s} (loud: {d['loud_state']:17s}) "
            f"eq {d['z_eq']:+.2f}  bond {d['z_bond']:+.2f}  gold {d['z_gold']:+.2f}{rot}{spell}")


def cmd_backtest() -> None:
    days, spec = classify_all()
    tr = transitions(days)
    print(f"classified {tr['n_days']} US sessions "
          f"({days[0]['as_of']} .. {days[-1]['as_of']}), spec {spec['version']}\n")

    print("state distribution (base rates):")
    for s in STATE_ORDER:
        if s in tr["base"]:
            n = sum(1 for x in days if x["state"] == s)
            print(f"  {s:17s} {tr['base'][s]:5.1f}%  ({n} days)")

    print("\ntransition matrix, row = yesterday, % = how often each state followed")
    print("(read: the row's biggest cells are the odds the tab quotes)\n")
    hdr = "  " + " " * 18 + "".join(f"{s[:7]:>9s}" for s in STATE_ORDER)
    print(hdr)
    for s in STATE_ORDER:
        if s not in tr["pct"]:
            continue
        row = tr["pct"][s]
        n = sum(tr["counts"][s].values())
        cells = "".join(f"{row.get(t, 0):8.1f} " for t in STATE_ORDER)
        print(f"  {s:17s} {cells}  n={n}")

    print("\nlift vs base rate (persistence diagonal is the story to check):")
    for s in STATE_ORDER:
        if s in tr["pct"] and s in tr["base"] and s in tr["pct"][s]:
            lift = tr["pct"][s][s] / tr["base"][s]
            print(f"  P({s} | {s}) = {tr['pct'][s][s]:5.1f}%  "
                  f"vs base {tr['base'][s]:5.1f}%  lift {lift:4.2f}x")

    print("\nepisode checks (eyeball these before trusting anything above):")
    idx = {d["as_of"]: d for d in days}
    for date, label in EPISODES:
        d = idx.get(date)
        print(f"  {label:34s} {_fmt_day(d) if d else date + '  NOT CLASSIFIED'}")

    print("\nflow-spell share by recent month (quiet = fundamentals carry):")
    months: dict[str, list] = {}
    for d in days:
        if d.get("spell_quiet") is not None:
            months.setdefault(d["as_of"][:7], []).append(d["spell_quiet"])
    for m in sorted(months)[-6:]:
        v = months[m]
        print(f"  {m}  {100.0 * sum(v) / len(v):5.1f}% quiet spell "
              f"({sum(v)}/{len(v)} sessions)")

    print("\nnewest 15 sessions:")
    for d in days[-15:]:
        print("  " + _fmt_day(d))


def cmd_tune() -> None:
    print("quiet_z grid — protocol per specs/flows.yaml: anchor is Aug-2026")
    print("mostly-quiet (the coach's dated observation), bounded by quiet not")
    print("swallowing the tape. NOT tuned on transition lift.\n")
    print("  quiet_z   overall-quiet   Aug-2026-quiet   loudest-month-quiet")
    for qz in (0.4, 0.5, 0.6, 0.75, 0.9, 1.0):
        days, _ = classify_all(quiet_z=qz)
        overall = 100.0 * sum(d["quiet"] for d in days) / len(days)
        aug = [d for d in days if d["as_of"].startswith("2026-08")]
        aug_q = 100.0 * sum(d["quiet"] for d in aug) / max(len(aug), 1)
        m2020 = [d for d in days if d["as_of"].startswith("2020-03")]
        m2020_q = 100.0 * sum(d["quiet"] for d in m2020) / max(len(m2020), 1)
        print(f"    {qz:.2f}      {overall:5.1f}%          {aug_q:5.1f}%"
              f"            {m2020_q:5.1f}%  (Mar-2020)")


def cmd_explain(date: str) -> None:
    days, _ = classify_all()
    idx = {d["as_of"]: d for d in days}
    if date not in idx:
        print(f"{date}: not classified (burn-in, non-trading day, or a series "
              f"had no close)")
        return
    d = idx[date]
    print(_fmt_day(d))
    tr = transitions(days)
    nxt = sorted(tr["pct"].get(d["state"], {}).items(), key=lambda kv: -kv[1])
    n = sum(tr["counts"].get(d["state"], {}).values())
    print(f"\nafter a {d['state']} day (n={n}), the next session was:")
    for s, v in nxt:
        print(f"  {v:5.1f}%  {s}")


def cmd_latest_from_table() -> None:
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT as_of, state, loud_state, quiet, z_eq, z_bond, z_gold, rot_z, "
        "rotation, spec_version FROM market_regime ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        print("market_regime is empty — run --persist first")
        return
    print(f"{row[0]}  {row[1]}  (loud: {row[2]}, quiet={row[3]})  "
          f"eq {row[4]:+.2f} bond {row[5]:+.2f} gold {row[6]:+.2f}  "
          f"spec {row[9]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", action="store_true",
                    help="classify the full history and write market_regime")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--explain", metavar="DATE")
    ap.add_argument("--weekly", action="store_true",
                    help="print the week-on-week read the tab leads with")
    args = ap.parse_args()

    if args.weekly:
        import json as _json
        v = weekly_view()
        print(f"last completed week {v['week']} (ended {v['week_end']}): "
              f"{v['state']} [{v['grade']}, max |z| {v['intensity']:.2f}]  "
              f"eq {v['z_eq']:+.2f} bond {v['z_bond']:+.2f} "
              f"gold {v['z_gold']:+.2f}"
              + (f" rot {v['rot_z']:+.2f}" if v.get('rot_z') is not None else ""))
        print("\nrolling week (each day = its own last 5 sessions):")
        for r in v.get("rolling", []):
            print(f"  {r['date']}  {r['state']:17s} [{r['grade']:8s} "
                  f"{r['intensity']:4.2f}]  eq {r['z_eq']:+.2f}  "
                  f"bond {r['z_bond']:+.2f}  gold {r['z_gold']:+.2f}")
        lad = v.get("ladder") or {}
        if lad:
            print("\nintensity ladder (pooled, next Indian week):")
            for fam in ("up", "down"):
                cells = " · ".join(
                    f"{g} {c['mean']:+.2f}% ({c['hit']:.0f}%, n={c['n']})"
                    if c.get("mean") is not None else f"{g} thin(n={c['n']})"
                    for g, c in lad[fam].items())
                print(f"  {fam:4s}: {cells}")
        print(f"\nnext week, from {v['n_observations']} prior "
              f"{v['state']} weeks:")
        for x in v["next"][:5]:
            print(f"  {x['pct']:5.1f}%  {x['state']}  (base {x['base']}%)")
        print("\nIndia next week given this state:")
        for sid, blk in v["india"].items():
            s = blk["stats"].get(v["state"])
            print(f"  {sid:12s} " + (f"{s['mean']:+.2f}%  hit {s['hit']:.0f}%  "
                  f"t {s['t']}  n={s['n']}" if s else "withheld (too few pairs)")
                  + f"   [series thru {blk['last_date']}]")
    elif args.tune:
        cmd_tune()
    elif args.backtest:
        cmd_backtest()
    elif args.explain:
        cmd_explain(args.explain)
    elif args.persist:
        days, spec = classify_all()
        n = persist(days, spec)
        v = latest_view(days)
        print(f"persisted {n} sessions, spec {spec['version']}")
        print(f"\nnewest: " + _fmt_day(days[-1]))
        print(f"quiet share, last 20 sessions: {v['quiet_share_20d']:.0f}%")
        print(f"next-session odds after a {v['state']} day "
              f"(n={v['n_observations']}):")
        for x in v["next"][:4]:
            print(f"  {x['pct']:5.1f}%  {x['state']}")
    else:
        cmd_latest_from_table()
    return 0


if __name__ == "__main__":
    sys.exit(main())
