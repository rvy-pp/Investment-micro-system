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
    args = ap.parse_args()

    if args.tune:
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
