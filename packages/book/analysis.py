"""The Book tab's Analysis sub-tab: one checklist per STOCK, pairs derived.

PM, 2026-10-10: "I think we analyze individually. Pairs can change" — so the
work lives on the stock and survives any re-pairing; a pair is only
long juice minus short juice, read off whatever specs/book.yaml dictates today.
"start with the 32 stocks, chat first" — the roster is the 32 dictated legs and
every entry arrives through chat (`--add`), so the tab is read-only for now.

THE FIVE CHECKS (the flowchart agreed in chat, same day):

  priced   what's priced in — price, consensus FY+1 EPS, forward P/E, P/E
           relative to its sector. AUTOMATIC where a daily consensus exists
           (the IT and EMS names in `estimates`), otherwise a chat entry.
  qbuild   the quarter build — actuals, then the PM's view of the next quarter.
  h2       the later quarters against what the street has already assumed
           (e.g. TechM: consensus FY27 implies a +24% step-up in Q3/Q4).
  price    the PM's price, bear case and confidence — the JUICE lives here,
           as data.juice_pct (the lean) with optional juice_low/high/horizon.
  rerate   re-rating check: is the relative P/E move earnings or multiple,
           and is it a nameable, lasting re-rating or noise.

Plus free `note` entries for the log. The table `book_analysis` is APPEND-ONLY:
a revision is a new row and the latest row per (stock, step) is live, so the
whole history stays for the "where was I right" review the PM asked for.
A row needs a non-empty note (invariant 1's spirit — no number without words).

Nothing here writes to `prices`, `estimates` or anything a pillar reads.

    python packages/book/analysis.py --add '{"entity":"tech_mahindra","step":"qbuild",
        "status":"done","note":"Q2 PAT Rs1,554cr, EPS 17.50 — in line with street",
        "data":{"q2_eps":17.5}}'
    python packages/book/analysis.py --show tech_mahindra
    python packages/book/analysis.py --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import statistics as st
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
DB = REPO / "data" / "ims.db"
BOOK_YAML = REPO / "specs" / "book.yaml"

STEPS = [("priced", "What's priced in"), ("qbuild", "Quarter build"),
         ("h2", "Later quarters vs street"), ("price", "Price, bear, confidence"),
         ("rerate", "Re-rating check")]
STEP_KEYS = [k for k, _ in STEPS]
STATUSES = ("done", "partial", "open")

# Peer sets for relative P/E. A stock is measured against the names that have
# a daily consensus in its own sector; anything else has no automatic read.
# EMS names carry no `sector` in the entities table, hence listed by id.
EMS_IDS = ("dixon", "amber", "kaynes", "pg_electroplast", "syrma_sgs", "avalon")

DDL = """
CREATE TABLE IF NOT EXISTS book_analysis (
  id         INTEGER PRIMARY KEY,
  entity_id  TEXT NOT NULL,
  as_of      TEXT NOT NULL,
  step       TEXT NOT NULL CHECK (step IN ('priced','qbuild','h2','price','rerate','note')),
  status     TEXT NOT NULL CHECK (status IN ('done','partial','open')),
  data       TEXT,
  note       TEXT NOT NULL CHECK (length(trim(note)) > 0),
  via        TEXT NOT NULL DEFAULT 'chat',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""


def connect(path: pathlib.Path | str = DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(DDL)
    return conn


# --------------------------------------------------------------- roster
def roster(cfg: dict | None = None) -> list[dict]:
    """The dictated legs, in dictation order, one row per stock.

    Side and group come from specs/book.yaml `pairs` — the PM's own list — so
    the Analysis tab and the Positions tab cannot disagree about who is long.
    A token on both sides (none today) is reported, never silently resolved.
    """
    cfg = cfg or yaml.safe_load(BOOK_YAML.read_text(encoding="utf-8"))
    tmap, names = cfg.get("ticker_map") or {}, cfg.get("names") or {}
    # PM, 2026-10-27: cash names off the Analysis roster (Syrma kept). The
    # tokens live in specs/book.yaml analysis_exclude with the reason.
    skip = set(cfg.get("analysis_exclude") or {})
    out: dict[str, dict] = {}
    for group, pairs in (cfg.get("pairs") or {}).items():
        for p in pairs:
            for side in ("long", "short"):
                for tok in p.get(side) or []:
                    ent = tmap.get(tok)
                    if not ent or tok in skip:
                        continue
                    if ent in out:
                        if out[ent]["side"] != side:
                            out[ent]["side"] = "both"
                        out[ent]["pairs"].append(p["name"])
                        continue
                    out[ent] = {"entity": ent, "token": tok, "name": names.get(tok, tok),
                                "side": side, "group": group, "pairs": [p["name"]]}
    return list(out.values())


def expected_legs(cfg: dict | None = None) -> int:
    """How many stocks the dictation names, counting an UNMAPPED token too.

    roster() skips a token with no ticker_map entry, so len(roster()) falling
    short of this is the drift check. It replaced a hardcoded 32 on 2026-10-25,
    when PGEL_AMBER made it 33 — a constant would have flagged a correct
    dictation as a broken one.
    """
    cfg = cfg or yaml.safe_load(BOOK_YAML.read_text(encoding="utf-8"))
    tmap = cfg.get("ticker_map") or {}
    skip = set(cfg.get("analysis_exclude") or {})        # counted out by name, as roster() leaves them out
    return len({tmap.get(t) or f"UNMAPPED:{t}"
                for ps in (cfg.get("pairs") or {}).values() for p in ps
                for side in ("long", "short") for t in p.get(side) or [] if t not in skip})


def dictated_pairs(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or yaml.safe_load(BOOK_YAML.read_text(encoding="utf-8"))
    tmap = cfg.get("ticker_map") or {}
    return [{"name": p["name"], "group": g,
             "long": [tmap.get(t) for t in p.get("long") or []],
             "short": [tmap.get(t) for t in p.get("short") or []]}
            for g, ps in (cfg.get("pairs") or {}).items() for p in ps]


# --------------------------------------------------------------- priced in
def fy_label(d: dt.date, ahead: int = 0) -> str:
    """Indian fiscal year label: Apr-2026..Mar-2027 is FY27."""
    return f"FY{(d.year + (1 if d.month >= 4 else 0) + ahead) % 100:02d}"


def _consensus(conn, ent: str, fy: str):
    r = conn.execute("SELECT as_of, value_num FROM estimates WHERE entity_id=? AND "
                     "broker='consensus_yahoo' AND period=? AND metric='eps' "
                     "ORDER BY as_of DESC LIMIT 1", (ent, fy)).fetchone()
    # Yahoo's exact-0.0 is a placeholder, not an estimate (results_io._usable_eps).
    return (r["as_of"], r["value_num"]) if r and r["value_num"] and r["value_num"] > 0 else None


def _close(conn, ent: str):
    r = conn.execute("SELECT date, close FROM prices WHERE entity_id=? ORDER BY date DESC LIMIT 1",
                     (ent,)).fetchone()
    return (r["date"], r["close"]) if r else None


def _peers(conn, ent: str) -> list[str]:
    if ent in EMS_IDS:
        return list(EMS_IDS)
    row = conn.execute("SELECT sector FROM entities WHERE id=?", (ent,)).fetchone()
    if row and row["sector"] == "it":
        return [r["id"] for r in conn.execute("SELECT id FROM entities WHERE sector='it'")]
    return []


def priced_auto(conn, ent: str, today: dt.date | None = None) -> dict | None:
    """Price, consensus FY+1 EPS, forward P/E and P/E relative to the sector
    median — or None where no daily consensus exists for the name."""
    today = today or dt.date.today()
    fy0, fy1 = fy_label(today), fy_label(today, 1)
    px, c0, c1 = _close(conn, ent), _consensus(conn, ent, fy0), _consensus(conn, ent, fy1)
    if not (px and c1):
        return None
    pe = px[1] / c1[1]
    peer_pe = []
    for p in _peers(conn, ent):
        pp, pc = _close(conn, p), _consensus(conn, p, fy1)
        if pp and pc:
            peer_pe.append(pp[1] / pc[1])
    med = st.median(peer_pe) if len(peer_pe) >= 3 else None
    return {"price": round(px[1], 2), "price_date": px[0],
            "fy0": fy0, "eps_fy0": round(c0[1], 2) if c0 else None,
            "fy1": fy1, "eps_fy1": round(c1[1], 2), "consensus_as_of": c1[0],
            "pe_fy1": round(pe, 1),
            "eps_growth": round(c1[1] / c0[1] - 1, 4) if c0 else None,
            "sector_pe_median": round(med, 1) if med else None,
            "rel_pe": round(pe / med, 2) if med else None, "n_peers": len(peer_pe)}


# --------------------------------------------------------------- entries
def add_entry(conn, doc: dict, today: dt.date | None = None) -> int:
    """Append one chat entry. Refuses an unknown stock, step or status, an
    empty note, and juice that is not a number — each with the reason."""
    ents = {r["entity"] for r in roster()}
    ent, step = doc.get("entity"), doc.get("step")
    status = doc.get("status", "done" if step != "note" else "done")
    note = (doc.get("note") or "").strip()
    probs = []
    if ent not in ents:
        probs.append(f"entity {ent!r} is not one of the dictated legs")
    if step not in STEP_KEYS + ["note"]:
        probs.append(f"step {step!r} not in {STEP_KEYS + ['note']}")
    if status not in STATUSES:
        probs.append(f"status {status!r} not in {STATUSES}")
    if not note:
        probs.append("note is empty — every entry says what it is in words")
    data = doc.get("data") or {}
    for k in ("juice_pct", "juice_low", "juice_high"):
        if k in data and not isinstance(data[k], (int, float)):
            probs.append(f"data.{k} must be a number (percent), got {data[k]!r}")
    if probs:
        raise ValueError("; ".join(probs))
    as_of = doc.get("as_of") or (today or dt.date.today()).isoformat()
    cur = conn.execute("INSERT INTO book_analysis (entity_id, as_of, step, status, data, note, via) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (ent, as_of, step, status, json.dumps(data) if data else None, note,
                        doc.get("via", "chat")))
    conn.commit()
    return cur.lastrowid


def pm_lean(log: list[dict]) -> dict | None:
    """The `price` entry holding the PM's live lean: the latest one that CARRIES
    a juice_pct, unless a later price entry says {"clear_lean": true}.

    NOT the latest price entry. Agents log price entries with the range only
    (juice_low/high, never juice_pct), and the view used to read the lean off
    the latest one — so an agent re-striking its anchor AFTER the PM set a lean
    would have silently replaced, say, Wipro's -20.6% with the default base,
    and moved the Kelly edge with it. Nothing would have raised: a default lean
    is a valid state. Caught 2026-10-27 while wiring the daily target search,
    whose whole job is to send agents back to re-strike; no lean had been lost
    yet (every leg's last price entry carried its lean)."""
    for e in reversed(log):
        if e["step"] != "price":
            continue
        d = e.get("data") or {}
        if d.get("clear_lean"):
            return None
        if isinstance(d.get("juice_pct"), (int, float)):
            return e
    return None


def _entries(conn, ent: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM book_analysis WHERE entity_id=? ORDER BY as_of, id",
                        (ent,)).fetchall()
    return [dict(r, data=json.loads(r["data"]) if r["data"] else {}) for r in rows]


# --------------------------------------------------------------- view
# PM, 2026-10-27: "for stocks with no lean, put medium by default", which the PM
# then defined as the BASE case (the middle scenario), and on EV/EBITDA legs the
# base AT THE ANCHOR. So a stock with a case build and no PM lean takes its live
# base juice on the headline year as a DEFAULT lean, labelled
# juice_source='default_base' everywhere it travels (Analysis pane, pair juice,
# the Kelly edge note, the orchestrator). The PM's own juice_pct always wins.
# It is computed at read time, never written: a stored default would freeze at
# the day it was written while the base moves with the price.
_DEFAULT_LEAN: dict = {}


def default_lean(conn, ent: str, today: dt.date) -> float | None:
    key = (ent, today.isoformat(), id(conn))
    if key not in _DEFAULT_LEAN:
        import case_brief as cb                       # lazy: case_brief imports this module
        v = None
        if cb.load_build(ent):
            b = cb.brief(ent, conn, today, pairs=False)
            hfy = b.get("headline_fy")
            v = (((b.get("scenarios") or {}).get("base") or {}).get("juice") or {}).get(hfy, {}).get("juice_pct")
        _DEFAULT_LEAN[key] = v
    return _DEFAULT_LEAN[key]


def view(conn=None, today: dt.date | None = None, defaults: bool = True) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        today = today or dt.date.today()
        stocks = []
        for s in roster():
            log = _entries(conn, s["entity"])
            live = {}
            for e in log:                      # ordered, so the last one wins
                if e["step"] != "note":
                    live[e["step"]] = e
            auto = priced_auto(conn, s["entity"], today)
            steps = {}
            for k in STEP_KEYS:
                e = live.get(k)
                if e:
                    steps[k] = {"status": e["status"], "note": e["note"],
                                "as_of": e["as_of"], "data": e["data"]}
                elif k == "priced" and auto:
                    steps[k] = {"status": "done", "auto": True, "as_of": auto["price_date"],
                                "note": None, "data": auto}
                else:
                    steps[k] = {"status": "open"}
            pr = live.get("price")
            le = pm_lean(log)
            juice = le["data"]["juice_pct"] if le else None
            src = "pm" if juice is not None else None
            # The price step is DONE while a PM lean is live, whatever the
            # latest price entry says. Agents and re-strikes log the RANGE only,
            # with status `partial`, and the last entry used to set the status:
            # on 2026-10-28 the σ-range re-log showed Wipro (lean -20.6%) and
            # Infosys (lean -2.67%) as `partial` while both leans still sized
            # Kelly. The same shape pm_lean() fixed for the number itself.
            if le and "price" in steps and steps["price"].get("status") != "done":
                steps["price"] = {**steps["price"], "status": "done", "lean_from": le["as_of"]}
            if juice is None and defaults:
                juice = default_lean(conn, s["entity"], today)
                src = "default_base" if juice is not None else None
            stocks.append({**s, "priced_auto": auto, "steps": steps,
                           "juice": juice, "juice_source": src,
                           "juice_low": pr["data"].get("juice_low") if pr else None,
                           "juice_high": pr["data"].get("juice_high") if pr else None,
                           "horizon": pr["data"].get("horizon") if pr else None,
                           "updated": log[-1]["as_of"] if log else None,
                           "log": list(reversed(log))})
        by_ent = {s["entity"]: s for s in stocks}
        pairs = []
        for p in dictated_pairs():
            L = [by_ent.get(e, {}).get("juice") for e in p["long"]]
            S = [by_ent.get(e, {}).get("juice") for e in p["short"]]
            lj = st.mean(L) if L and all(v is not None for v in L) else None
            sj = st.mean(S) if S and all(v is not None for v in S) else None
            pairs.append({**p, "long_names": [by_ent[e]["name"] for e in p["long"] if e in by_ent],
                          "short_names": [by_ent[e]["name"] for e in p["short"] if e in by_ent],
                          "long_juice": lj, "short_juice": sj,
                          # A pair has juice only when BOTH sides do; half a
                          # pair's view is not a pair view.
                          "pair_juice": round(lj - sj, 2) if lj is not None and sj is not None else None})
        return {"as_of": today.isoformat(), "fy1": fy_label(today, 1),
                "steps": [{"key": k, "label": l} for k, l in STEPS],
                "stocks": stocks, "pairs": pairs,
                "n_auto": sum(1 for s in stocks if s["priced_auto"])}
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------- selftest
def _selftest() -> None:
    import tempfile, os
    ok = 0
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    try:
        conn = connect(tmp.name)
        conn.executescript("""
          CREATE TABLE entities (id TEXT PRIMARY KEY, sector TEXT);
          CREATE TABLE prices (entity_id TEXT, date TEXT, close REAL);
          CREATE TABLE estimates (entity_id TEXT, broker TEXT, period TEXT, metric TEXT,
                                  as_of TEXT, value_num REAL);""")
        it = ["tech_mahindra", "ltimindtree", "tcs", "infosys"]
        for i, e in enumerate(it):
            conn.execute("INSERT INTO entities VALUES (?, 'it')", (e,))
            conn.execute("INSERT INTO prices VALUES (?, '2026-10-09', ?)", (e, 100.0 * (i + 1)))
            conn.execute("INSERT INTO estimates VALUES (?, 'consensus_yahoo', 'FY28', 'eps', '2026-10-09', ?)",
                         (e, 10.0 * (i + 1) / (1 + 0.1 * i)))
            conn.execute("INSERT INTO estimates VALUES (?, 'consensus_yahoo', 'FY27', 'eps', '2026-10-09', ?)",
                         (e, 9.0 * (i + 1)))
        conn.execute("INSERT INTO entities VALUES ('sail', 'steel')")
        conn.execute("INSERT INTO prices VALUES ('sail', '2026-10-09', 150)")
        conn.commit()
        today = dt.date(2026, 10, 10)

        # fiscal labels, both sides of April
        assert fy_label(dt.date(2026, 10, 10)) == "FY27" and fy_label(dt.date(2026, 10, 10), 1) == "FY28"
        assert fy_label(dt.date(2026, 3, 31)) == "FY26"; ok += 2

        # priced in: automatic for a name with consensus, None without (acceptance + rejection)
        a = priced_auto(conn, "tech_mahindra", today)
        assert a and a["pe_fy1"] == 10.0 and a["n_peers"] == 4 and a["rel_pe"] is not None; ok += 1
        assert priced_auto(conn, "sail", today) is None; ok += 1
        # Yahoo's 0.0 placeholder is refused, not divided by
        conn.execute("INSERT INTO estimates VALUES ('sail','consensus_yahoo','FY28','eps','2026-10-09',0.0)")
        assert priced_auto(conn, "sail", today) is None; ok += 1

        # entries: refusals each name their reason
        for bad, why in [({"entity": "nope", "step": "qbuild", "note": "x"}, "dictated legs"),
                         ({"entity": "tech_mahindra", "step": "bogus", "note": "x"}, "step"),
                         ({"entity": "tech_mahindra", "step": "qbuild", "note": "  "}, "note is empty"),
                         ({"entity": "tech_mahindra", "step": "price", "note": "x",
                           "data": {"juice_pct": "4%"}}, "must be a number")]:
            try:
                add_entry(conn, bad, today); raise AssertionError(f"accepted {bad}")
            except ValueError as e:
                assert why in str(e), (why, str(e)); ok += 1

        # latest row per step is live; history kept
        add_entry(conn, {"entity": "tech_mahindra", "step": "price", "status": "partial",
                         "note": "first view", "data": {"juice_pct": 1.0}}, today)
        add_entry(conn, {"entity": "tech_mahindra", "step": "price", "status": "done",
                         "note": "revised", "data": {"juice_pct": 2.0, "juice_low": -1, "juice_high": 4}}, today)
        # defaults=False: the real case builds on disk would otherwise lend
        # their base juice to this fixture through default_lean()
        v = view(conn, today, defaults=False)
        tm = next(s for s in v["stocks"] if s["entity"] == "tech_mahindra")
        assert tm["juice"] == 2.0 and tm["steps"]["price"]["note"] == "revised" and len(tm["log"]) == 2; ok += 1
        assert tm["steps"]["priced"].get("auto") is True and tm["juice_source"] == "pm"; ok += 1

        # pair juice only when BOTH legs have juice
        p = next(x for x in v["pairs"] if x["name"] == "TECHM_LTM")
        assert p["pair_juice"] is None and p["long_juice"] == 2.0; ok += 1

        # PM 2026-10-27: no lean -> the base is the default lean, labelled; a PM
        # lean always wins over a default (injected through the cache)
        for e, val in (("ltimindtree", 6.5), ("tech_mahindra", 9.9)):
            _DEFAULT_LEAN[(e, today.isoformat(), id(conn))] = val
        vd = view(conn, today)
        lt = next(s for s in vd["stocks"] if s["entity"] == "ltimindtree")
        tm = next(s for s in vd["stocks"] if s["entity"] == "tech_mahindra")
        assert lt["juice"] == 6.5 and lt["juice_source"] == "default_base"; ok += 1
        assert tm["juice"] == 2.0 and tm["juice_source"] == "pm"; ok += 1      # the PM's lean wins
        p = next(x for x in vd["pairs"] if x["name"] == "TECHM_LTM")
        assert p["pair_juice"] == round(2.0 - 6.5, 2); ok += 1
        _DEFAULT_LEAN[("ltimindtree", today.isoformat(), id(conn))] = None  # no build: still no juice
        assert next(s for s in view(conn, today)["stocks"] if s["entity"] == "ltimindtree")["juice"] is None; ok += 1

        add_entry(conn, {"entity": "ltimindtree", "step": "price", "note": "short leg view",
                         "data": {"juice_pct": -1.5}}, today)
        p = next(x for x in view(conn, today)["pairs"] if x["name"] == "TECHM_LTM")
        assert p["pair_juice"] == 3.5; ok += 1     # long +2.0 minus short -1.5

        # an agent's re-strike logs the RANGE only, after the PM's lean: the lean
        # must survive it (it used to fall to the default base) and the range moves
        add_entry(conn, {"entity": "ltimindtree", "step": "price", "status": "partial", "via": "agent:ltm-agent",
                         "note": "anchor re-struck", "data": {"juice_low": -4.0, "juice_high": 1.0}}, today)
        lt = next(s for s in view(conn, today)["stocks"] if s["entity"] == "ltimindtree")
        assert lt["juice"] == -1.5 and lt["juice_source"] == "pm" and lt["juice_low"] == -4.0; ok += 1
        assert lt["steps"]["price"]["status"] == "done"; ok += 1                   # a live lean keeps the step done
        add_entry(conn, {"entity": "ltimindtree", "step": "price", "note": "PM drops the lean",
                         "data": {"clear_lean": True}}, today)
        lt = next(s for s in view(conn, today, defaults=False)["stocks"] if s["entity"] == "ltimindtree")
        assert lt["juice"] is None and lt["juice_source"] is None; ok += 1         # an explicit clear clears
        add_entry(conn, {"entity": "ltimindtree", "step": "price", "status": "partial", "via": "agent:ltm-agent",
                         "note": "range only", "data": {"juice_low": -4.0, "juice_high": 1.0}}, today)
        lt = next(s for s in view(conn, today, defaults=False)["stocks"] if s["entity"] == "ltimindtree")
        assert lt["steps"]["price"]["status"] == "partial"; ok += 1                # no lean: a range stays partial

        # roster is the dictated legs, every one mapped, sides from the dictation
        r = roster()
        assert len(r) == expected_legs(), (len(r), expected_legs())
        assert all(x["side"] != "both" for x in r), [x["entity"] for x in r if x["side"] == "both"]
        assert next(x for x in r if x["entity"] == "tech_mahindra")["side"] == "long"
        assert next(x for x in r if x["entity"] == "ltimindtree")["side"] == "short"; ok += 1
        # cash names off (PM 2026-10-27), Syrma kept; the count follows the exclusion
        fx = {"ticker_map": {"A": "a", "B": "b", "C": "c"},
              "pairs": {"G": [{"name": "A_B", "long": ["A"], "short": ["B"]},
                              {"name": "C_B", "long": ["C"], "short": ["B"]}]}}
        assert [x["entity"] for x in roster(fx)] == ["a", "b", "c"] and expected_legs(fx) == 3; ok += 1
        fx["analysis_exclude"] = {"C": "cash"}
        assert [x["entity"] for x in roster(fx)] == ["a", "b"] and expected_legs(fx) == 2; ok += 1
        live = {x["token"] for x in r}
        assert "SYRMA" in live and not live & {"DALBHARA", "LTTS"}, live; ok += 1
        conn.close()
    finally:
        os.unlink(tmp.name)
    print(f"analysis selftest: {ok} checks passed")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--add", help="JSON entry: entity, step, status, note, data, as_of")
    ap.add_argument("--show", help="print one stock's live checks and log")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.add:
        conn = connect()
        rid = add_entry(conn, json.loads(a.add))
        print(f"logged #{rid}")
        a.show = a.show or json.loads(a.add)["entity"]
        conn.close()
    if a.show:
        s = next((x for x in view()["stocks"] if x["entity"] == a.show), None)
        if not s:
            sys.exit(f"{a.show!r} is not one of the dictated legs")
        print(f"{s['name']} ({s['side']}, {s['group']})  juice: {s['juice']}")
        for k, lbl in STEPS:
            st_ = s["steps"][k]
            print(f"  [{st_['status']:7s}] {lbl}: {st_.get('note') or ('auto' if st_.get('auto') else '')}")
        for e in s["log"]:
            print(f"    {e['as_of']} {e['step']:7s} {e['note']}")


if __name__ == "__main__":
    main()
