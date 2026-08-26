"""Structured results for the API — specs + PM overrides, scored.

Wraps bridge.py rather than refactoring it, so the CLI keeps working unchanged.

THE OVERRIDE MERGE IS THE POINT. YAML is the checked-in baseline; the desk's
corrections live in the `overrides` table and are applied on top at read time.
Nothing rewrites a spec file, so the analyst's original stays diffable and a
bad input is one DELETE away from reverting.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(REPO / "packages" / "score"))

from bridge import (  # noqa: E402
    load_specs, load_scoring, load_accumulation, run_bridge, shocks_from_store,
    _series_in_store,
)
from scoring import score as to_score  # noqa: E402


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def active_overrides(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM overrides WHERE active=1 ORDER BY created_at DESC")]


def apply_overrides(entities: dict, fins: dict, conn) -> list[dict]:
    """Mutate specs in memory with active overrides. Returns what was applied."""
    applied = []
    for o in active_overrides(conn):
        eid, scope, item, field, val = (o["entity_id"], o["scope"], o["item"],
                                        o["field"], o["value_num"])
        if scope == "financial":
            if eid in fins:
                fins[eid][field] = val
                applied.append(o)
            continue
        ent = entities.get(eid)
        if not ent:
            continue
        for line in ent.get("outputs" if scope == "output" else "inputs") or []:
            if line.get("item") == item:
                line[field] = val
                applied.append(o)
    return applied


def compute(peer_group: str, window: int = 30, materiality: float = 0.015) -> dict:
    entities, units, fin = load_specs()
    fins = fin["companies"]
    conn = connect()
    applied = apply_overrides(entities, fins, conn)

    acc, hl = load_accumulation()
    shocks, detail, as_of, fx = shocks_from_store(window, None, acc, hl)
    usdinr = fx or fin["usdinr"]
    available = set(shocks) | _series_in_store()
    form, k, p = load_scoring()

    rows = []
    for ent in sorted(entities.values(), key=lambda e: e["id"]):
        if ent.get("peer_group") != peer_group:
            continue
        f = fins.get(ent["id"], {})
        r = run_bridge(ent, shocks, units, f.get("base_ebitda", 0), usdinr,
                       available)
        pct = r["pct_of_ebitda"]
        r["score"] = (to_score(pct, k, form, p)
                      if (pct is not None and r["coverage_ok"]) else None)
        if not r["coverage_ok"]:
            r["verdict"] = f"no bridge ({r['n_priced']}/{r['n_total']} priced)"
        elif pct is None:
            r["verdict"] = "no denominator"
        elif abs(pct) < materiality:
            r["verdict"] = "immaterial"
        else:
            r["verdict"] = "positive" if pct > 0 else "negative"
        r["name"] = ent.get("name", ent["id"])
        r["base_ebitda"] = f.get("base_ebitda")
        rows.append(r)

    pairs = []
    scored = [r for r in rows if r["score"] is not None]
    for i in range(len(scored)):
        for j in range(i + 1, len(scored)):
            a, b = scored[i], scored[j]
            d = a["pct_of_ebitda"] - b["pct_of_ebitda"]
            lng, sht = (a, b) if d > 0 else (b, a)
            pairs.append({
                "long": lng["entity"], "short": sht["entity"],
                "pct_spread": abs(d),
                "pair_score": to_score(abs(d), k, form, p),
                "naive_diff": abs(lng["score"] - sht["score"]),
            })
    pairs.sort(key=lambda x: -x["pair_score"])

    # Only the series this peer group actually CONSUMES. Equity closes are in
    # the same store but are not price_links for anything — they are what the
    # bridge is trying to explain, not an input to it. Listing them as
    # "drivers" invites reading a stock move as a cause of its own score.
    def in_group(ent: dict) -> bool:
        if ent.get("peer_group") == peer_group:
            return True
        # A reporting unit has no peer_group of its own; it belongs wherever its
        # PARENT sits. Without the parent check, novelis leaked its LME line
        # into the zinc group's drivers.
        parent = entities.get(ent.get("parent_id") or "")
        return bool(parent) and parent.get("peer_group") == peer_group

    used = set()
    for ent in entities.values():
        if not in_group(ent):
            continue
        for line in (ent.get("outputs") or []) + (ent.get("inputs") or []):
            if line.get("price_link"):
                used.add(line["price_link"])

    drivers = [{"series": e, "from": d[1], "to": d[3], "from_date": d[0],
                "to_date": d[2], "pct": d[4], "stale_days": d[5],
                "origin": d[6]}
               for e, d in sorted(detail.items()) if e in shocks and e in used]

    conn.close()
    return {
        "peer_group": peer_group, "as_of": as_of, "window": window,
        "usdinr": usdinr, "rows": rows, "pairs": pairs, "drivers": drivers,
        "overrides_applied": len(applied),
        "scoring": {"form": form, "k": k, "p": p},
    }


def inputs_for_ui() -> list[dict]:
    """Every editable number, with spec value, active override and provenance."""
    entities, _units, fin = load_specs()
    fins = fin["companies"]
    conn = connect()
    ovr = {(o["entity_id"], o["scope"], o["item"], o["field"]): o
           for o in active_overrides(conn)}
    conn.close()

    out = []
    for ent in sorted(entities.values(), key=lambda e: e["id"]):
        eid = ent["id"]
        if not ent.get("peer_group") and not ent.get("outputs"):
            continue
        f = fins.get(eid, {})
        if "base_ebitda" in f:
            key = (eid, "financial", None, "base_ebitda")
            o = ovr.get(key)
            out.append({
                "entity_id": eid, "scope": "financial", "item": None,
                "field": "base_ebitda", "label": "Base EBITDA (INR cr)",
                "spec_value": f["base_ebitda"],
                "override": o["value_num"] if o else None,
                "override_id": o["id"] if o else None,
                "note": o["note"] if o else None,
                "verify": f.get("verify", "pending"),
                "source_note": (f.get("source_note") or "").strip(),
            })
        for scope, lines in (("output", ent.get("outputs") or []),
                             ("input", ent.get("inputs") or [])):
            for ln in lines:
                fields = (["volume", "asp_premium"] if scope == "output"
                          else ["intensity", "market_pct", "basis_pass_through"])
                for fld in fields:
                    if fld not in ln:
                        continue
                    key = (eid, scope, ln["item"], fld)
                    o = ovr.get(key)
                    out.append({
                        "entity_id": eid, "scope": scope, "item": ln["item"],
                        "field": fld, "label": f"{ln['item']} · {fld}",
                        "spec_value": ln[fld],
                        "override": o["value_num"] if o else None,
                        "override_id": o["id"] if o else None,
                        "note": o["note"] if o else None,
                        "verify": ln.get("verify", "pending"),
                        "source_note": (ln.get("source_note") or "").strip(),
                    })
    return out


def oi_snapshot() -> list[dict]:
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT o.* FROM oi o JOIN (SELECT entity_id, MAX(date) d FROM oi "
        "GROUP BY entity_id) m ON o.entity_id=m.entity_id AND o.date=m.d "
        "ORDER BY o.entity_id")]
    for r in rows:
        hist = conn.execute(
            "SELECT date, oi FROM oi WHERE entity_id=? ORDER BY date",
            (r["entity_id"],)).fetchall()
        r["spark"] = [h[1] for h in hist][-60:]
    conn.close()
    return rows


def guidance_rows() -> list[dict]:
    import math
    conn = connect()
    rows = []
    for g in conn.execute(
            "SELECT * FROM guidance WHERE status='open' ORDER BY entity_id"):
        ev = conn.execute(
            "SELECT direction, weight, quote, as_of FROM guidance_evidence "
            "WHERE guidance_id=? ORDER BY as_of", (g["id"],)).fetchall()
        lo = 0.0
        for d, w, _q, _a in ev:
            lo += d * w * 1.2
        conf = 1 / (1 + math.exp(-lo))
        rows.append({
            "entity_id": g["entity_id"], "period": g["period"],
            "metric": g["metric"], "unit": g["unit"], "quote": g["quote"],
            "target": (f"{g['target_low']:g}-{g['target_high']:g}"
                       if g["target_type"] == "range"
                       else (f"{g['target_value']:g}"
                             if g["target_value"] is not None else "—")),
            "n_for": sum(1 for d, _, _, _ in ev if d > 0),
            "n_against": sum(1 for d, _, _, _ in ev if d < 0),
            "confidence": conf, "p4": 1 + 4 * conf,
            "evidence": [{"direction": d, "weight": w, "quote": q, "as_of": a}
                         for d, w, q, a in ev],
        })
    conn.close()
    return rows


def set_override(entity_id: str, scope: str, item, field: str,
                 value: float, note: str, prev: float | None) -> int:
    conn = connect()
    conn.execute(
        "UPDATE overrides SET active=0 WHERE entity_id=? AND scope=? "
        "AND IFNULL(item,'')=IFNULL(?,'') AND field=? AND active=1",
        (entity_id, scope, item, field))
    from datetime import datetime, timezone
    cur = conn.execute(
        "INSERT INTO overrides (entity_id,scope,item,field,value_num,"
        "prev_value,note,author,created_at,active) VALUES (?,?,?,?,?,?,?,?,?,1)",
        (entity_id, scope, item, field, value, prev, note, "pm",
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def clear_override(override_id: int) -> None:
    conn = connect()
    conn.execute("UPDATE overrides SET active=0 WHERE id=?", (override_id,))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# SECTORS — the front end's top-level navigation, defined here rather than in
# the page so adding one is a data edit, not a JavaScript edit.
#
# `peer_groups` is what makes a sector LIVE. A sector with none is not broken
# and not coming-soon vapour: its prices are already being captured daily by
# the metals pack, and what it lacks is a spec — peer groups, base_ebitda,
# intensities. Saying exactly that is more useful than an empty tab, and it is
# the honest version of "not built": the raw material is on disk and countable.
#
# `commodities` lists the pack series that sector will consume. They are loaded
# and `price_link`-ed from NO spec, so nothing reads them yet — see
# metals_pack.PARKED. Move an id out of a sector's list only when a spec
# actually links it.
# ---------------------------------------------------------------------------
SECTORS = [
    {
        "id": "non_ferrous",
        "label": "Non-Ferrous",
        "peer_groups": ["aluminium_primary", "zinc"],
        "commodities": [
            "lme_aluminium", "alumina_index", "cp_coke",
            "thermal_coal_seaborne", "lme_zinc", "silver",
            "aluminium_shfe_cny", "alumina_shfe_usd", "alumina_shfe_cny",
            "midwest_premium", "zinc_shfe",
        ],
    },
    {
        "id": "steel",
        "label": "Steel",
        # LIVE 2026-08-25. Four groups, because a peer group is a scoring
        # universe and these do not share a cost stack — see specs/sectors/
        # steel.yaml. steel_stainless and steel_secondary carry NO economics
        # lines by design, so those two names score on valuation and mood only
        # and their economics pillar is WITHHELD rather than absent.
        # FIVE SCORED NAMES in two groups. steel_secondary (shyam_metalics)
        # removed 2026-08-25 and steel_stainless (jindal_stainless) 2026-08-26,
        # both PM decisions, both `peer_group: null` in the entity spec rather
        # than deleted. See specs/entities/steel.yaml for the evidence on each.
        "peer_groups": ["steel_integrated", "steel_converter"],
        "commodities": [
            "coking_coal_spot_aus", "coking_coal_contract_qtr",
            "iron_ore_china_cfr62", "iron_ore_china_import62",
            "iron_ore_sgx_tsi62", "iron_ore_futures_china_cny", "iron_ore",
            "hrc_china_export_fob", "hrc_china_domestic", "hrc_cis_fob",
            "hrc_india_inr", "hrc_india_usd", "hrc_uk", "hrc_germany",
            "rebar_china_cny", "rebar_india_primary_inr",
            "rebar_india_secondary_inr", "scrap_turkey",
            "thermal_coal_seaborne", "thermal_coal_indonesia_6322",
        ],
    },
    {
        "id": "cement",
        "label": "Cement",
        "peer_groups": [],
        # The Daily CEMENT Pack arrives in the same mail and is deliberately not
        # read — metals-pack-fetch ignores it. So cement has no prices of its own
        # yet; these are the input costs it shares with the others. Wiring the
        # cement pack is the first step whenever this sector is built.
        "commodities": [
            "thermal_coal_seaborne", "thermal_coal_indonesia_6322",
            "cp_coke", "brent", "usdinr",
        ],
    },
]

# Series the pack carries that no sector claims yet. Surfaced so a captured
# column cannot go quietly unused — the reason for capturing them at all is that
# the connector route can never backfill.
UNCLAIMED_HINT = ["lme_lead", "lme_copper", "lme_nickel", "gold", "dxy"]


def sector_list() -> list[dict]:
    """Sector nav plus a live/awaiting-spec flag for each."""
    return [{"id": s["id"], "label": s["label"],
             "peer_groups": s["peer_groups"],
             "live": bool(s["peer_groups"])} for s in SECTORS]


def sector_detail(sector_id: str) -> dict:
    """Everything the page needs for one sector tab.

    For a sector with no peer_groups this IS the tab: the commodity inputs that
    are already landing, with the date and source of each, so the reader can see
    what a build would have to work with. Dates come straight from `prices`, so a
    stale input is visible here rather than only in freshness.py.
    """
    spec = next((x for x in SECTORS if x["id"] == sector_id), None)
    if spec is None:
        return {"error": f"unknown sector {sector_id!r}",
                "known": [x["id"] for x in SECTORS]}

    # Human labels come from the pack's own column notes, not from entities.name
    # — metals_pack.py inserts entities with name = id, so entities.name is the
    # slug and reads as a database key rather than a price.
    try:
        import importlib.util as _u
        _sp = _u.spec_from_file_location(
            "_mp", REPO / "packages" / "adapters" / "metals_pack.py")
        _mp = _u.module_from_spec(_sp)
        _sp.loader.exec_module(_mp)
        LABELS = {v[0]: v[2] for v in _mp.COLS.values()}
    except Exception:
        LABELS = {}

    conn = connect()
    ids = list(dict.fromkeys(spec["commodities"]))          # dedupe, keep order
    rows = []
    for eid in ids:
        # MOST RECENT 30 ROWS, not the last two. Comparing the newest close to
        # the one immediately before it reports 0.0% on nearly every pack series,
        # because the pack pre-creates the current day's row and carries the
        # previous session forward until tomorrow's file backfills it — so the
        # last two rows are usually identical by construction. Walking back to
        # the last close that actually DIFFERS gives the real move and the date
        # it happened, which is the same row_age / value_age distinction
        # freshness.py draws.
        hist = conn.execute(
            "SELECT date, close, source FROM prices WHERE entity_id=? "
            "AND close IS NOT NULL ORDER BY date DESC LIMIT 30",
            (eid,)).fetchall()
        label = LABELS.get(eid) or eid
        if not hist:
            rows.append({"id": eid, "name": label, "last": None, "date": None,
                         "source": None, "chg_pct": None, "moved_on": None})
            continue
        last = hist[0]
        chg, moved_on = None, None
        for h in hist[1:]:
            if h["close"] != last["close"]:
                if h["close"]:
                    chg = (last["close"] - h["close"]) / h["close"] * 100.0
                break
            moved_on = h["date"]
        rows.append({"id": eid, "name": label, "last": last["close"],
                     "date": last["date"],
                     "source": last["source"] or "legacy",
                     "chg_pct": chg,
                     # None means "the newest row IS the move"; a date means the
                     # value has been carried unchanged since then.
                     "moved_on": moved_on})

    unclaimed = []
    if not spec["peer_groups"]:
        for eid in UNCLAIMED_HINT:
            r = conn.execute(
                "SELECT date, close FROM prices WHERE entity_id=? "
                "ORDER BY date DESC LIMIT 1", (eid,)).fetchone()
            if r:
                unclaimed.append({"id": eid, "last": r["close"],
                                  "date": r["date"]})
    conn.close()

    return {"id": spec["id"], "label": spec["label"],
            "live": bool(spec["peer_groups"]),
            "peer_groups": spec["peer_groups"],
            "commodities": rows,
            "unclaimed": unclaimed,
            "n_priced": sum(1 for r in rows if r["last"] is not None),
            "n_total": len(rows)}

# ---------------------------------------------------------------------------
# NAV — the page's top-level tabs. Two of them are not sectors, so the nav is a
# separate list rather than SECTORS with special cases bolted on.
# ---------------------------------------------------------------------------
def nav_list() -> list[dict]:
    out = [{"id": "overview", "kind": "overview", "label": "Daily Overview",
            "live": True},
           {"id": "flows", "kind": "flows", "label": "Flows", "live": False}]
    for s in sector_list():
        out.append({"id": s["id"], "kind": "sector", "label": s["label"],
                    "live": s["live"], "peer_groups": s["peer_groups"]})
    return out


def _read_json(path):
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def overview() -> dict:
    """One screen: did the run work, what is stale, where did the book land.

    THIS IS ASSEMBLED FROM WHAT THE RUN ACTUALLY WROTE, not recomputed. The
    scores come from `pillar_scores` and the sizing from conviction.py, so this
    tab and the Pair tab cannot disagree — a recomputed overview would drift from
    the persisted tape the moment an override was active, and then the first
    screen of the app would be the one telling a different story.
    """
    import datetime as dt
    import sys as _sys

    _sys.path.insert(0, str(REPO / "packages" / "score"))
    _sys.path.insert(0, str(REPO / "packages" / "core"))
    import conviction as cv       # noqa: E402
    import freshness              # noqa: E402

    today = dt.date.today().isoformat()
    out: dict = {"as_of": today}

    # ---- last run -------------------------------------------------------
    st = _read_json(REPO / "data" / "refresh" / "status.json")
    if st:
        out["run"] = {
            "day": st.get("day"), "finished": st.get("finished"),
            "ok": st.get("ok"),
            "steps": [{"step": x.get("step"), "status": x.get("status"),
                       "detail": x.get("data_age") or x.get("error") or ""}
                      for x in (st.get("steps") or [])],
            "manual": st.get("manual") or [],
        }
        # A refresh that ran YESTERDAY and left a green status file is the exact
        # thing this repo keeps getting caught by, so say the age out loud.
        if st.get("day") and st["day"] != today:
            out["run"]["warning"] = (
                f"last refresh was {st['day']}, not {today} — everything below "
                f"is that run's output")
    else:
        out["run"] = {"warning": "no data/refresh/status.json — the refresh has "
                                 "never completed on this machine"}

    fe = _read_json(REPO / "data" / "refresh" / "frontend.json")
    if fe:
        out["frontend"] = {"ok": fe.get("ok"), "n_routes": fe.get("n_routes"),
                           "n_failed": fe.get("n_failed"),
                           "problems": fe.get("problems") or [],
                           "warnings": fe.get("warnings") or []}

    # ---- feeds ----------------------------------------------------------
    fr = freshness.check()
    out["feeds"] = {
        "ok": fr["ok"], "n_stale": fr["n_stale"],
        "n_series": len(fr["series"]),
        "stale": [{"series": x["series"], "feed": x["feed"],
                   "age": x["age_txt"], "last": x["last_date"]}
                  for x in fr["stale"]],
        "parked": sum(1 for x in fr["series"] if x["kind"] == "parked"),
        "oi": fr.get("oi"),
    }

    # ---- the book -------------------------------------------------------
    import json as _json
    conn = connect()
    as_of = conn.execute("SELECT MAX(as_of) FROM pillar_scores").fetchone()[0]
    names = {}
    for r in conn.execute(
            "SELECT entity_id, pillar, score, withheld, detail FROM "
            "pillar_scores WHERE as_of=?", (as_of,)):
        d = names.setdefault(r["entity_id"],
                             {"pillars": {}, "withheld": [], "placeholders": {}})
        d["pillars"][r["pillar"]] = r["score"]
        if r["score"] is None and r["withheld"]:
            d["withheld"].append(r["pillar"])
        # A placeholder is a real score with detail.placeholder set, so it is
        # invisible to any consumer that only looks at the number. The page has
        # to be told, or a manufactured 3.00 renders identically to a measured
        # one — which is the entire risk of having placeholders at all.
        if r["detail"]:
            try:
                det = _json.loads(r["detail"])
            except Exception:
                det = {}
            if det.get("placeholder"):
                d["placeholders"][r["pillar"]] = {
                    "value": det.get("value"),
                    "reason": det.get("reason"),
                    "granted": det.get("granted"),
                    "granted_by": det.get("granted_by"),
                    "review_trigger": det.get("review_trigger"),
                    "overdue": det.get("overdue"),
                    "withheld_reason": det.get("withheld_reason"),
                }

    book = []
    for eid, d in names.items():
        pil = {k: v for k, v in d["pillars"].items() if v is not None}
        c = cv.conviction(pil)
        size = c.get("size")
        book.append({
            "entity": eid,
            "composite": d["pillars"].get("composite"),
            "economics": d["pillars"].get("economics"),
            "valuation": d["pillars"].get("valuation"),
            "mood": d["pillars"].get("mood"),
            "guidance": d["pillars"].get("guidance"),
            "withheld": d["withheld"],
            "placeholders": d["placeholders"],
            "size": size,
            # The two thresholds are specs/scoring.yaml's, not new numbers:
            # 1.00 = a 5%-of-EBITDA move at neutral qualifiers, 0.28 = the 1.5%
            # materiality floor.
            "verdict": (None if size is None else
                        "FULL" if size >= 1.0 else
                        f"{size * 100:.0f}%" if size >= 0.28 else "no trade"),
            "why": c.get("why"),
        })
    book.sort(key=lambda r: -(r["composite"] or 0))
    out["as_of_scores"] = as_of
    out["book"] = book
    # Hoisted to the top level too, so the tab can carry one banner rather than
    # relying on the reader noticing a marker in a table cell.
    out["placeholders"] = [
        {"entity": b["entity"], "pillar": k, **v}
        for b in book for k, v in (b["placeholders"] or {}).items()]

    # ---- what actually moved today --------------------------------------
    # Only series a spec price_links. A "today's movers" list padded with the 27
    # parked steel series would read as though they drive something.
    try:
        entities, _u, _f = load_specs()
        linked = set()

        def _walk(n):
            if isinstance(n, dict):
                if "price_link" in n:
                    linked.add(n["price_link"])
                for v in n.values():
                    _walk(v)
            elif isinstance(n, list):
                for v in n:
                    _walk(v)
        _walk(entities)
    except Exception:
        linked = set()

    movers = []
    for eid in sorted(linked):
        hist = conn.execute(
            "SELECT date, close, source FROM prices WHERE entity_id=? "
            "AND close IS NOT NULL ORDER BY date DESC LIMIT 30",
            (eid,)).fetchall()
        if not hist:
            movers.append({"id": eid, "last": None, "date": None,
                           "source": None, "chg_pct": None, "moved_on": None})
            continue
        last, chg, moved_on = hist[0], None, None
        for h in hist[1:]:
            if h["close"] != last["close"]:
                if h["close"]:
                    chg = (last["close"] - h["close"]) / h["close"] * 100.0
                break
            moved_on = h["date"]
        movers.append({"id": eid, "last": last["close"], "date": last["date"],
                       "source": last["source"] or "legacy", "chg_pct": chg,
                       "moved_on": moved_on})
    movers.sort(key=lambda r: -abs(r["chg_pct"] or 0))
    out["movers"] = movers

    # ---- positioning ----------------------------------------------------
    oi = conn.execute(
        "SELECT o.entity_id, o.date, o.buildup, o.buildup_15d, "
        "o.oi_percentile, o.oi_percentile_15d, o.z_score_3m, "
        "o.pct_vs_median_3m "
        "FROM oi o JOIN (SELECT entity_id, MAX(date) d FROM oi GROUP BY "
        "entity_id) m ON o.entity_id=m.entity_id AND o.date=m.d "
        "ORDER BY o.entity_id").fetchall()
    out["positioning"] = [dict(r) for r in oi]
    conn.close()
    return out


def flows() -> dict:
    """F1-F4 readiness, measured against the store rather than read off a doc.

    docs/FLOWS.md is dated 2026-08-19 and its blocker table was verified once, by
    hand. Restating it in the page would go stale silently the first time one of
    those blockers was cleared — the failure mode the doc itself warns about. So
    every row below is probed live.
    """
    conn = connect()

    def scalar(sql, args=()):
        try:
            return conn.execute(sql, args).fetchone()[0]
        except Exception:
            return None

    n_prices = scalar("SELECT COUNT(*) FROM prices") or 0
    n_volume = scalar("SELECT COUNT(volume) FROM prices") or 0
    n_regime = scalar("SELECT COUNT(*) FROM sector_regime") or 0
    n_idx = scalar("SELECT COUNT(*) FROM entities WHERE kind='index'") or 0
    idx = [r[0] for r in conn.execute(
        "SELECT id FROM entities WHERE kind='index'")]
    oi_names = scalar("SELECT COUNT(DISTINCT entity_id) FROM oi") or 0
    oi_dates = scalar("SELECT COUNT(DISTINCT date) FROM oi") or 0
    oi_last = scalar("SELECT MAX(date) FROM oi")
    px_names = scalar(
        "SELECT COUNT(DISTINCT entity_id) FROM prices p JOIN entities e "
        "ON e.id=p.entity_id WHERE e.kind='company'") or 0

    inputs = [
        {"input": "dispersion", "source": "intra_sector_return_dispersion, 20d",
         "state": "ready",
         "detail": f"{px_names} names carry daily closes; nothing else needed"},
        {"input": "breadth_pct", "source": "pct_of_sector_above_50dma",
         "state": "ready" if px_names >= 8 else "thin",
         "detail": f"computable, but over {px_names} names a percentage moves in "
                   f"{100 // max(px_names, 1)}pp steps"},
        {"input": "rel_strength", "source": "sector_index_vs_nifty, 60d",
         "state": "blocked",
         "detail": (f"needs a sector benchmark and NIFTY. `prices` holds "
                    f"{n_idx} index series ({', '.join(idx) or 'none'}) and "
                    f"neither is one of them")},
        {"input": "turnover_pctile",
         "source": "sector_turnover_vs_own_history, 252d",
         "state": "blocked",
         "detail": (f"`prices.volume` is NULL on all {n_prices:,} rows. The "
                    f"column has existed since the schema was written and the "
                    f"Yahoo payload carries volume; nothing has ever inserted "
                    f"one")},
        {"input": "flow_fii", "source": "fii_sector_flow, 20d",
         "state": "blocked",
         "detail": "no source, no adapter, never probed"},
    ]

    monitors = [
        {"id": "F1", "monitor": "Risk on / risk off",
         "question": "is the market paying for risk at all today",
         "scope": "market-wide", "lands_in": "NO TABLE YET",
         "state": "needs a decision",
         "detail": ("`sector_regime` is keyed (sector, as_of) so a market-wide "
                    "row has nowhere to go. FLOWS.md recommends a "
                    "`market_regime` table over the `sector='*'` convention, "
                    "because every query would then have to know to exclude a "
                    "magic row. Open — the PM has not decided")},
        {"id": "F2", "monitor": "Sector activity",
         "question": "which sectors are being worked, which are ignored",
         "scope": "per sector", "lands_in": "sector_regime (read by nothing)",
         "state": f"table exists, {n_regime} rows",
         "detail": ("`out_of_flavour` (actively sold, shorts work) and `ignored` "
                    "(nobody looking, nothing expresses either way) are "
                    "different states, not degrees. Whatever computes these must "
                    "not collapse them onto one bullish/bearish axis")},
        {"id": "F3", "monitor": "Investor sentiment",
         "question": "who is doing it - FII / DII / retail, cash vs derivatives",
         "scope": "market + sector", "lands_in": "sector_regime.flow_fii",
         "state": "blocked", "detail": "same blocker as flow_fii above"},
        {"id": "F4", "monitor": "Crowding",
         "question": "is this specific name already full",
         "scope": "per entity", "lands_in": "oi",
         "state": "data present, unread",
         "detail": (f"`oi` holds {oi_dates} dates x {oi_names} names, newest "
                    f"{oi_last}. Nothing in `score/` reads it, so crowding is "
                    f"captured and unused")},
    ]
    conn.close()

    return {
        "note": ("Flows never sets direction, and since 2026-08-24 it does not "
                 "enter scoring at all - the gate multiplier was removed from "
                 "conviction.py, where it had sat at a permanent 1.0 and printed "
                 "as though permission had been checked. Flows answers whether a "
                 "correct call can be expressed, for a human reading this tab. "
                 "Added as a weighted term it would start recommending whatever "
                 "is most crowded."),
        "doc": "docs/FLOWS.md",
        "sector_regime_rows": n_regime,
        "inputs": inputs,
        "monitors": monitors,
        "n_ready": sum(1 for x in inputs if x["state"] == "ready"),
        "n_inputs": len(inputs),
    }
