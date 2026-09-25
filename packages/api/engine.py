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
sys.path.insert(0, str(REPO / "packages" / "adapters"))
from vault_oi import raw_pct_3m  # noqa: E402  — one percentile definition


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
    # Sector + display name: specs first (modelled names), then the entities
    # table (OI-only names — IT since 2026-08-31 — whose rows vault_oi
    # ensure-inserts with the vault folder name and sector), then "other" so
    # nothing ever vanishes from the Positioning tab for want of a label.
    try:
        entities, _u, _f = load_specs()
    except Exception:
        entities = {}
    db_ent = {r["id"]: r for r in conn.execute(
        "SELECT id, name, sector FROM entities")}
    for r in rows:
        e = entities.get(r["entity_id"]) or {}
        d = dict(db_ent.get(r["entity_id"]) or {})
        r["sector"] = e.get("sector") or d.get("sector") or "other"
        r["name"] = e.get("name") or d.get("name") or r["entity_id"]
    for r in rows:
        hist = conn.execute(
            "SELECT date, oi FROM oi WHERE entity_id=? ORDER BY date",
            (r["entity_id"],)).fetchall()
        r["spark"] = [h[1] for h in hist][-60:]
        # The 3m percentile is RECOMPUTED here from the same rows the chart
        # draws, not trusted from the stored column (PM 2026-09-15: raw 3m
        # rank, never the vault's cycle-normalised frontmatter). The loader
        # writes the same number, but a stored value can outlive the rows it
        # was ranked against — Dalmia carried a frontmatter 46th on rows whose
        # OI is NULL because the vault file stopped at 25-Aug. Same rule as
        # vault_oi.raw_pct_3m, one definition: strict-below over the last 63
        # sessions. NULL latest OI ⇒ no rank, not a stale one.
        r["oi_percentile"] = raw_pct_3m(
            [{"oi": h[1]} for h in reversed(hist)])
    conn.close()
    return rows


def oi_history(entity_id: str) -> dict:
    """Full daily OI + futures-price series for ONE name — the Positioning
    tab's expandable OI-vs-time chart (the Sensibull grammar: OI area against
    the price line, so buildup is visible as a relation rather than a label).

    Whole history on purpose: ~100 rows per name today, a few KB. Windowing
    belongs in the client, which already has the data to do it."""
    conn = connect()
    rows = [{"date": r["date"], "oi": r["oi"], "price": r["price"]}
            for r in conn.execute(
                "SELECT date, oi, price FROM oi WHERE entity_id=? "
                "ORDER BY date", (entity_id,))]
    ent = conn.execute("SELECT name FROM entities WHERE id=?",
                       (entity_id,)).fetchone()
    lot = conn.execute(
        "SELECT lot_size FROM oi WHERE entity_id=? AND lot_size IS NOT NULL "
        "ORDER BY date DESC LIMIT 1", (entity_id,)).fetchone()
    conn.close()
    # Display name: specs first, entities second — the same resolution
    # oi_snapshot uses. Modelled names store their SLUG in entities.name;
    # only the OI-only names (IT) carry a display name there.
    try:
        entities, _u, _f = load_specs()
    except Exception:
        entities = {}
    name = ((entities.get(entity_id) or {}).get("name")
            or (ent["name"] if ent else None) or entity_id)
    return {
        "entity_id": entity_id,
        "name": name,
        "lot_size": (lot["lot_size"] if lot else None),
        "rows": rows,
    }


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

def cement_watch() -> dict:
    """IndiaMART day-to-day cement ask move, for the Overview banner.

    A WATCH, NOT A SCORE, and the API keeps that separation visible: this reads
    `cement_watch`, never `prices`, and no pillar consumes it. It exists because
    the Kotak pack's prints land ~15 days late (PM), so a multi-region move in
    dealer asks is knowable well before the priced series shows it.

    Returns `{state: no_data|calibrating|live, ...}`. `calibrating` is a real
    state, not an error: the panel's own day-to-day noise has never been
    measured, so a threshold set today would be invented rather than observed.
    The page must SAY it is calibrating rather than render an empty banner —
    silence would read as "no move", which is the one thing it does not mean.
    """
    import importlib.util as _u
    try:
        sp = _u.spec_from_file_location(
            "_im", REPO / "packages" / "adapters" / "indiamart_cement.py")
        mod = _u.module_from_spec(sp)
        sp.loader.exec_module(mod)
    except Exception as e:
        return {"state": "error", "regions": [], "alerts": [],
                "note": f"cement watch unavailable: {type(e).__name__}: {e}"}
    conn = connect()
    try:
        rep = mod.report(conn)
        # THE LEVEL SERIES, for the chart on the Cement tab (PM, 2026-09-20:
        # the ask chart sits under the score chart and above the price chart).
        # It rides on THIS route rather than a new one, because it is the same
        # table and the Overview already fetches this — and because a new route
        # would have to be added to export_static.py's enumeration or the vault
        # copy would render that panel empty.
        #
        # MEAN, not median: see indiamart_cement.summarize(). ~20 dates x 5
        # regions, so the whole series is cheaper than a second round trip.
        by: dict[str, dict] = {}
        dates: list[str] = []
        for d, r, m, n in conn.execute(
                "SELECT capture_date, region, mean_bag, n_listings "
                "FROM cement_watch ORDER BY capture_date, region"):
            if d not in dates:
                dates.append(d)
            by.setdefault(r, {})[d] = (m, n)
        rep["series"] = {
            "dates": dates,
            "regions": {r: {"mean": [round(v[d][0], 2) if d in v else None
                                     for d in dates],
                            "n": [v[d][1] if d in v else None for d in dates]}
                        for r, v in sorted(by.items())},
        }
        return rep
    except Exception as e:
        return {"state": "error", "regions": [], "alerts": [],
                "note": f"cement watch failed: {type(e).__name__}: {e}"}
    finally:
        conn.close()


def auto_share() -> dict:
    """Vahan maker share by segment for the Auto tab — 2W / PV / CV.

    PM, 2026-09-23: *"in the auto tab, I need a graph for market share change
    on a daily basis. Different for PV, 2 wheelers and CV. Only include fno
    names in the graph lines, keep rest as others."*

    TWO SERIES, AND THE PAGE MUST NOT BLUR THEM, because they have different
    lengths for a reason that is not fixable:

      `monthly` — share of each completed month. Long, available immediately,
                  read at the LATEST capture so backdated revisions are
                  reflected rather than frozen at first sight.
      `daily`   — share of the running month-to-date, one point per capture.
                  FORWARD-ONLY. Vahan publishes no daily granularity on the
                  open API (see vahan_share.py: the report builder that does is
                  CAPTCHA-gated), so a day's share simply did not exist before
                  we started reading it. It cannot be backfilled, and on the
                  first day it is a single point with no change to show.

    The reason both are here: a chart carrying only `daily` would have been
    empty on the day it was asked for, and a chart carrying only `monthly`
    would not answer the question. `daily.has_change` is false until there are
    two captures, and the page says which state it is in rather than drawing a
    one-point line that reads as a flat market.

    Others is DERIVED in vahan_share.shares() as total - sum(named), so the
    lines always sum to 100% and every unlisted maker is inside it. Read
    SEGMENTS[*]['note'] before interpreting a chart — on CV especially, where
    Tata Motors sits in Others because its CV entity left F&O at the demerger.
    """
    import importlib.util as _u
    try:
        sp = _u.spec_from_file_location(
            "_vs", REPO / "packages" / "adapters" / "vahan_share.py")
        mod = _u.module_from_spec(sp)
        sp.loader.exec_module(mod)
    except Exception as e:
        return {"state": "error", "segments": [],
                "note": f"vahan share unavailable: {type(e).__name__}: {e}"}
    conn = connect()
    try:
        caps = [r[0] for r in conn.execute(
            "SELECT DISTINCT capture_date FROM vahan_share ORDER BY capture_date")]
        if not caps:
            return {"state": "no_data", "segments": [], "captures": 0,
                    "note": "no Vahan capture yet — run "
                            "packages/adapters/vahan_share.py --capture"}
        cur = mod._recent_months(1)[0]      # the in-progress month
        out = []
        for seg, cfg in mod.SEGMENTS.items():
            labels = list(cfg["fno"].keys()) + [mod.OTHERS]

            # MONTHLY — each period at its most recent capture.
            periods = mod._months_sorted({r[0] for r in conn.execute(
                "SELECT DISTINCT period FROM vahan_share WHERE segment=?", (seg,))})
            mseries = {k: [] for k in labels}
            mtotal = []
            for p in periods:
                s = mod.shares(conn, seg, period=p)
                mtotal.append(s.get("total") if s else None)
                for k in labels:
                    mseries[k].append(round(s["share_pct"][k], 3)
                                      if s and k in s["share_pct"] else None)

            # DAILY — the running month's MTD share, one point per capture.
            dseries = {k: [] for k in labels}
            dtotal = []
            for cd in caps:
                s = mod.shares(conn, seg, period=cur, capture_date=cd)
                dtotal.append(s.get("total") if s else None)
                for k in labels:
                    dseries[k].append(round(s["share_pct"][k], 3)
                                      if s and k in s["share_pct"] else None)

            # THE MONTH-END FORECAST. Cheap now that the skew fit reads
            # `vahan_share` rather than the network (~0.3s for all three), so
            # it rides on this route rather than a second one — the
            # cement_watch precedent, and a new route would have to be added
            # to export_static.py or the vault copy would render it empty.
            try:
                fc = mod.forecast(conn, seg)
            except Exception as e:                      # noqa: BLE001
                fc = {"state": "error", "note": f"{type(e).__name__}: {e}"}

            out.append({
                "id": seg, "label": cfg["label"], "note": cfg["note"],
                # MHCV draws no residual line (PM). The share arithmetic still
                # uses the FULL segment as its denominator, so the drawn lines
                # deliberately do not sum to 100 — suppressing the line must
                # not quietly rebase the others onto a smaller universe.
                "no_others": bool(cfg.get("no_others")),
                "forecast": fc,
                "fno": [{"label": k, "symbol": v[0]} for k, v in cfg["fno"].items()],
                "others_label": mod.OTHERS,
                "monthly": {"periods": periods, "series": mseries,
                            "total": mtotal, "current": cur},
                "daily": {"dates": caps, "series": dseries, "total": dtotal,
                          "month": cur, "has_change": len(caps) > 1},
            })
        return {"state": "live", "segments": out, "captures": len(caps),
                "first_capture": caps[0], "last_capture": caps[-1],
                "current_month": cur}
    except Exception as e:
        return {"state": "error", "segments": [],
                "note": f"vahan share failed: {type(e).__name__}: {e}"}
    finally:
        conn.close()


def auto_inventory() -> dict:
    """Channel inventory flow for the Auto tab: SIAM wholesale minus Vahan retail.

    PM, 2026-09-25: "Create a subtab ... for inventory tracking as frequent as
    the data gets for whatever granularity is available."

    MONTHLY, PV AND 2W, AND THAT IS THE FLOOR THE DATA SETS. Wholesale arrives
    once a month from SIAM, ~15 days after month end; retail is daily but a
    daily flow needs a daily wholesale, which does not exist anywhere free. CV
    is absent because SIAM publishes no monthly CV; 3W because Vahan's count is
    dominated by e-rickshaw makers who are not SIAM members.

    FLOW, NOT A LEVEL. Each month's `net` is what went into (or out of) dealer
    stock. There is deliberately NO running cumulative: retail includes makers
    SIAM does not (Ola Electric ~8-16k/month), so every month reads slightly low
    and a cumulative would drift ~150k a year into apparent destocking that did
    not happen. The monthly flow is 1-2% biased; the cumulative would be wrong.
    """
    import importlib.util as _u
    try:
        sp = _u.spec_from_file_location(
            "_sw", REPO / "packages" / "adapters" / "siam_wholesale.py")
        mod = _u.module_from_spec(sp)
        sp.loader.exec_module(mod)
    except Exception as e:
        return {"state": "error", "segments": [],
                "note": f"channel unavailable: {type(e).__name__}: {e}"}
    conn = connect()
    try:
        segs = []
        labels = {"2W": "Two-wheelers", "PV": "Passenger vehicles"}
        for seg in ("2W", "PV"):
            ch = mod.channel(conn, seg)
            segs.append({"id": seg, "label": labels[seg], "months": ch})
        latest = max((c["months"][-1]["period"] for c in segs if c["months"]),
                     key=mod._key, default=None)
        if not any(c["months"] for c in segs):
            return {"state": "no_data", "segments": segs,
                    "note": "no SIAM wholesale or Vahan retail stored yet"}
        return {"state": "live", "segments": segs, "latest": latest}
    except Exception as e:
        return {"state": "error", "segments": [],
                "note": f"channel failed: {type(e).__name__}: {e}"}
    finally:
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
        # LIVE 2026-08-28. One peer group where steel needed two: the four
        # scored names share a cost stack (same kiln, same bought fuels) and
        # differ by REVENUE REGION, which lives on the entity output lines
        # (regional price_links) rather than in the grouping. Four scored of
        # nine — ultratech, ambuja, shree, dalmia were the F&O names when
        # scored (Dalmia left F&O late Aug 2026 and was dropped from OI
        # tracking 2026-09-15; its scoring stands untouched); the other
        # five are peer_group: null per invariant 7. See
        # specs/sectors/cement.yaml for the validation runs.
        "peer_groups": ["cement"],
        "commodities": [
            "cement_price_india_inr", "cement_price_north_inr",
            "cement_price_central_inr", "cement_price_east_inr",
            "cement_price_west_inr", "cement_price_south_inr",
            "thermal_coal_seaborne", "thermal_coal_indonesia_6322",
            "cp_coke", "brent", "usdinr",
        ],
        # An awaiting-spec tab with a chart, because this sector's output price
        # is the one series in the store worth LOOKING at before any spec
        # exists: five regions on one axis is the comparison the pack was
        # captured for. Declared as data so the next sector with a chartable
        # series is a dict edit, not an engine edit.
        #
        # `divide: 20` — the store holds Rs/TONNE (bridge arithmetic needs it);
        # the desk, the pack and every broker note quote Rs/50kg BAG. The API
        # serves the display unit so the page never grows unit arithmetic.
        "chart": {
            "ids": ["cement_price_north_inr", "cement_price_central_inr",
                    "cement_price_east_inr", "cement_price_west_inr",
                    "cement_price_south_inr", "cement_price_india_inr"],
            "divide": 20.0,
            "unit": "Rs/bag",
            # Title and caption are DATA, like everything else here — they
            # were hardcoded in app.html until mining's chart rendered under
            # cement's "by region" heading and its Kotak-vs-Nomura caption.
            "title": "Cement price by region · Rs/bag · monthly",
            "caption": "Kotak pack levels. These run ~Rs30/bag above Nomura's "
                       "trade-only channel checks — a basis difference, "
                       "recorded in adapters/cement_pack.py; the deltas are "
                       "what matter. The current month is a month-to-date "
                       "average dated at its capture.",
            # Drawn as a dated rule, same visual as the pair chart's corporate
            # actions, but the meaning is the opposite and the caption says so:
            # PM-confirmed REAL move (2026-08-28), not an artefact to break on.
            "marks": [{"d": "2025-10-31",
                       "label": "Oct-2025 — real ~9% drop, PM-confirmed"}],
        },
        # Embed the IndiaMART watch payload on this tab too. Same source as
        # /api/cement_watch — the Overview banner answers "did something move
        # today"; this tab is where the per-region detail belongs.
        "watch": True,
    },
    {
        "id": "mining",
        "label": "Mining",
        # LIVE 2026-08-29. Two peer groups: mining_bulk (nmdc, coal_india —
        # the two F&O names, administered/auction domestic bulk) and
        # mining_copper (hindustan_copper alone — LME-linked, NOT in F&O,
        # scored on explicit PM instruction as a cash-only expression; its
        # singleton group keeps every default pair inside mining_bulk).
        # lloyds_metals is peer_group: null ("Leave Lloyds Metals", PM).
        # See specs/sectors/mining.yaml for the validation runs.
        "peer_groups": ["mining_bulk", "mining_copper"],
        "commodities": [
            "nmdc_lumps_inr", "nmdc_fines_inr", "nmdc_sales_ttm_mt",
            "coal_eauction_realisation_inr", "coal_fsa_realisation_inr",
            "coalindia_offtake_ttm_mt",
            "lme_copper", "iron_ore_china_cfr62", "thermal_coal_seaborne",
            "brent", "usdinr",
        ],
        # NMDC's own circular sequence — the sector's administered output
        # price, one axis, Rs/t as filed (ex-royalty basis from 2026-01-09;
        # earlier inclusive-basis circulars are deliberately NOT in the
        # series, so the chart starts where the current measure starts).
        "chart": {
            "ids": ["nmdc_lumps_inr", "nmdc_fines_inr"],
            "divide": 1.0,
            "unit": "Rs/t",
            "title": "NMDC administered iron ore price · Rs/t · per circular",
            "caption": "NMDC's own circulars, Baila lump 65.5% / fines 64%, "
                       "FOR EX-ROYALTY basis — the series starts 2026-01-09 "
                       "because that circular changed the basis (earlier "
                       "prints include royalty+DMF+NMET, ~18% higher, and are "
                       "deliberately not loaded; adapters/mining_filings.py). "
                       "Steps are change events, not daily carries.",
            "marks": [{"d": "2026-08-08",
                       "label": "Aug-26 circular — 2nd consecutive cut"}],
        },
    },
    {
        "id": "ems",
        "label": "EMS",
        # LIVE 2026-08-30. One peer group, four F&O names — and the first
        # NON-COMMODITY sector: P3 is FORWARD P/E vs growth (peer-relative,
        # packages/score/valuation_pe.py), mood rides beside it, economics
        # and guidance withhold by construction. There is deliberately no
        # margin bridge — these are converters, and the binding cost drivers
        # the digests name (copper-clad laminate, resin) have no series at
        # all. See specs/sectors/ems.yaml.
        "peer_groups": ["ems_assemblers"],
        # "Maybe some commodity prices" (PM, 2026-08-29) lands here: the cost
        # complex is DISPLAYED as context, linked from no spec, scoring
        # nothing. usdinr earns its row twice over — both PGEL and Amber name
        # rupee depreciation as a live margin driver in the digests.
        "commodities": [
            "lme_copper", "lme_aluminium", "hrc_india_inr",
            "brent", "usdinr",
        ],
        "chart": {
            "ids": ["lme_copper"],
            "divide": 1.0,
            "unit": "USD/t",
            "title": "LME copper · USD/t · the most-cited EMS input",
            "caption": "Context, not a driver: EMS economics is deliberately "
                       "not bridged (pass-through contracts, unsourced "
                       "intensities — specs/entities/ems.yaml). Copper is the "
                       "input the digests name first for RAC/PCBA cost "
                       "pressure; aluminium, HRC, brent and USDINR sit in the "
                       "table below.",
            "marks": [],
        },
        # The tab's real content: the consensus panel — forward P/E, growth,
        # PEG and estimate-revision momentum per name, live from `estimates`.
        "estimates": True,
    },
    {
        "id": "it",
        "label": "IT",
        # PM 2026-09-01: "rather than scoring, we will look at 1 year forward
        # p/e ratios" — so IT gets a tab whose content IS the consensus panel,
        # and deliberately nothing else: no specs, no peer groups, no pillars.
        # The roster lives only in the `entities` table (vault_oi.UNMODELLED
        # ensure-inserts it for the OI FK); consensus_panel falls back to that
        # table when a sector has no spec entities. Closes land in `prices`
        # via yahoo_prices (invariant 7 keeps unscored names out of every
        # scoring path — peer_group stays NULL).
        "peer_groups": [],
        # USDINR is the one macro series the IT notes cite as a margin driver;
        # context only, linked from nothing.
        "commodities": ["usdinr"],
        "estimates": True,
        # Display sub-groups for the consensus panel/scatter — the vault
        # coverage convention, NOT peer groups (nothing scores against them).
        # LTIMindtree trades as LTM.NS since its rename to "LTM Limited"
        # (found 2026-09-02); the entity id stays `ltimindtree`.
        "est_groups": {
            "IT Large Cap": ["tcs", "infosys", "hcl_tech", "wipro",
                             "tech_mahindra", "ltimindtree"],
            "IT Mid Cap":   ["persistent", "coforge", "mphasis", "ofss"],
            "IT ER&D":      ["kpit", "tata_elxsi", "ltts"],
        },
    },
    {
        "id": "auto",
        "label": "Auto",
        # Added 2026-09-19 on PM instruction, together with Ather Energy's
        # dossier. It is a READ tab and deliberately nothing else: no specs,
        # no peer groups, no pillars, no consensus panel. The only content is
        # the Company historical analysis sub-view plus the context chart
        # below — exactly the state IT was in on 2026-08-31, and the honest
        # one, because nothing here has been through a scoring design.
        #
        # `peer_groups: []` is what keeps it out of every scoring path, and
        # invariant 7 does the rest: `ather` lands in `entities` via
        # yahoo_prices.EQUITIES with peer_group NULL, so no pillar, no pair
        # and no backtest can reach it. Do NOT add a peer group here to make
        # the tab look finished.
        "peer_groups": [],
        # Context only, linked from no spec — and the reason the list looks
        # thin is the finding, not an omission. See the raw-materials verdict
        # in data/companies/ather-energy/dossier.json: the input that
        # actually moved Ather's margins is LITHIUM and the NMC cathode
        # complex (management: $8/kg -> $24/kg, cells +30-50%, a commodity
        # index +46% over five quarters, a 5.6pp hit to Q1FY27 gross margin)
        # and NONE of it has a series in this store. In that same quarter
        # every series listed here FELL — aluminium -12.8%, nickel -4.9%,
        # HRC -2.2%, brent -43.7%. A cost overlay built on these would have
        # read the worst commodity quarter in the company's listed life as a
        # TAILWIND. They are displayed so a reader can see that for
        # themselves; they are not a cost model and must not become one.
        "commodities": [
            "lme_aluminium", "lme_copper", "lme_nickel",
            "hrc_india_inr", "brent", "usdinr",
        ],
        # BRENT, not a cost series — and that is the point of this chart.
        # For an ICE maker crude is an input; for an EV two-wheeler maker it
        # is the DEMAND driver, and it is the one management names first
        # (Q1FY27: petrol/diesel availability fears and rising pump prices as
        # structural tailwinds). Charting a cost series here would assert the
        # cost story the evidence refuses.
        "chart": {
            "ids": ["brent"],
            "divide": 1.0,
            "unit": "USD/bbl",
            "title": "Brent · USD/bbl · the demand driver, not a cost",
            "caption": "Crude is displayed because for an electric "
                       "two-wheeler maker it sits on the REVENUE side: "
                       "Ather's Q1FY27 call names petrol availability fears "
                       "and rising pump prices among four structural "
                       "tailwinds, and brent is +102% over Ather's listed "
                       "life against the stock's +442%. The caution is that "
                       "the tape does not corroborate it at short horizons — "
                       "21-day returns correlate -0.149, the generic "
                       "risk-off sign, so this is a stated driver with level "
                       "evidence and no short-horizon evidence. Nothing "
                       "here is linked from a spec and nothing scores.",
            "marks": [],
        },
    },
]

# Series the pack carries that no sector claims yet. Surfaced so a captured
# column cannot go quietly unused — the reason for capturing them at all is that
# the connector route can never backfill.
# lme_copper left 2026-08-29 — mining claims it (hindustan_copper).
UNCLAIMED_HINT = ["lme_lead", "lme_nickel", "gold", "dxy"]


def consensus_panel(sector_id: str, as_of: str | None = None) -> dict:
    """The consensus-estimates table for a sector that declares one (EMS/IT).

    LIVE-COMPUTED from `estimates` + latest closes via
    valuation_pe.compute_row — the same arithmetic the scorer persists, so
    the panel and the pillar cannot use two different formulas. It is NOT
    the persisted score: the panel shows today's close against the latest
    capture (including one dated after the last scored close), which is what
    the desk wants to LOOK at; the persisted P3 stays strictly as-of and is
    what the tape shows. The unscored names (peer_group null — Syrma,
    Avalon) appear too, flagged: their consensus is captured daily even
    though nothing scores them.

    `as_of` (PM 2026-09-03: "let me view with dates in past") replays the
    panel POINT-IN-TIME: the consensus capture on or before that date against
    the close on or before that date, BBG capture and drift likewise. Nothing
    is snapshotted — the dated captures in `estimates` ARE the history, so a
    past date is a recomputation, not a saved copy, and it is identical every
    time. History floor: the first capture (2026-08-29 EMS / 2026-09-01 IT);
    earlier dates withhold honestly. A bad or future date falls back to today.
    """
    import datetime as _dt
    import valuation_pe as _vpe  # packages/score is on sys.path above

    entities, _u, _f = load_specs()
    ents = [e for e in entities.values() if e.get("sector") == sector_id
            and e.get("kind") == "company"]
    conn = connect()
    tracked_only = False
    if not ents:
        # A sector with no specs at all (IT): the roster lives only in the
        # `entities` table. Nothing here is scored, so every row is display.
        tracked_only = True
        ents = [{"id": r["id"], "name": r["name"],
                 "peer_group": r["peer_group"]}
                for r in conn.execute(
                    "SELECT id, name, peer_group FROM entities "
                    "WHERE sector=? AND kind='company' AND active=1",
                    (sector_id,))]
    spec = next((x for x in SECTORS if x["id"] == sector_id), {})
    group_of = {eid: g for g, eids in (spec.get("est_groups") or {}).items()
                for eid in eids}
    # Bloomberg 2-yr fwd P/E — a hand-captured screenshot feed (bbg_pe2y.py).
    # THE CAPTURE PINS AN IMPLIED 24-MONTH EPS (close on the capture date /
    # captured multiple), and the panel re-marks that EPS at each day's close —
    # so the 2-yr view stays price-current between screenshots (PM instruction
    # 2026-09-02: "keep updating the BBG 2 year numbers daily"). What CANNOT
    # self-update is the EPS itself, so each row also carries the drift of the
    # Yahoo consensus since the capture date: when the street has moved its
    # numbers, the pinned BBG EPS has probably moved too, and the page warns.
    today = _dt.date.today().isoformat()
    view = today
    if as_of:
        try:
            view = min(_dt.date.fromisoformat(as_of).isoformat(), today)
        except ValueError:
            pass
    pe2y_asof = conn.execute(
        "SELECT MAX(as_of) FROM estimates WHERE broker='bloomberg' "
        "AND metric='pe_fwd_2y' AND as_of<=?", (view,)).fetchone()[0]
    pe2y = dict(conn.execute(
        "SELECT entity_id, value_num FROM estimates WHERE broker='bloomberg' "
        "AND metric='pe_fwd_2y' AND as_of=?", (pe2y_asof,))) if pe2y_asof else {}

    def _pe2y_marked(eid: str, row: dict
                     ) -> tuple[float | None, float | None, float | None]:
        """(re-marked 2-yr multiple, implied 24m EPS, EPS drift since capture).

        The PM's stated mechanism (2026-09-02): back-calculate the 2-yr EPS
        from the capture-day price and captured multiple, then re-price it
        with each new day's close. The EPS is the stable half — 2-yr
        estimates move once or twice a quarter — so it is served explicitly
        rather than left as internal arithmetic."""
        p2 = pe2y.get(eid)
        if p2 is None:
            return None, None, None
        cap_close = conn.execute(
            "SELECT close FROM prices WHERE entity_id=? AND date<=? "
            "ORDER BY date DESC LIMIT 1", (eid, pe2y_asof)).fetchone()
        live, implied_eps = p2, None
        if cap_close and cap_close[0] and row.get("close"):
            implied_eps = cap_close[0] / p2
            live = row["close"] / implied_eps
        drift = None
        cap_est = _vpe.consensus_asof(conn, eid, pe2y_asof)
        if cap_est:
            deltas = []
            for fy, now in ((row.get("fy1"), row.get("eps_fy1")),
                            (row.get("fy2"), row.get("eps_fy2"))):
                old = (cap_est.get(fy) or {}).get("eps")
                if old and now:
                    deltas.append(now / old - 1.0)
            if deltas:
                drift = max(deltas, key=abs)
        return live, implied_eps, drift
    rows, pegs = [], []
    for ent in sorted(ents, key=lambda e: e["id"]):
        # require_growth=False: the growth floor is a scoring gate; the panel
        # still shows a low-growth name's multiple, with peg=None.
        row, why = _vpe.compute_row(conn, ent["id"], view,
                                    require_growth=False)
        scored = bool(ent.get("peer_group")) and not tracked_only
        state = ("tracked" if tracked_only
                 else "scored" if scored else "not in F&O")
        if row is None:
            rows.append({"id": ent["id"], "name": ent.get("name") or ent["id"],
                         "scored": scored, "state": state,
                         "group": group_of.get(ent["id"]),
                         "pe_2y": pe2y.get(ent["id"]), "withheld": why})
            continue
        # The display median must stay the SCORER's median: a low-growth name
        # now carries a (flagged) display PEG, but the scorer withholds it,
        # so it must not tilt the median the green/red colouring compares to.
        if scored and row["peg"] is not None and row["growth"] >= _vpe.MIN_GROWTH:
            pegs.append(row["peg"])
        p2_live, p2_eps, p2_drift = _pe2y_marked(ent["id"], row)
        rows.append({
            "id": ent["id"], "name": ent.get("name") or ent["id"],
            "scored": scored, "state": state,
            "group": group_of.get(ent["id"]),
            "pe_2y": round(p2_live, 1) if p2_live is not None else None,
            "eps_2y_implied": (round(p2_eps, 2)
                               if p2_eps is not None else None),
            "pe_2y_drift": (round(p2_drift, 4)
                            if p2_drift is not None else None),
            "close": row["close"], "px_date": row["px_date"],
            "fy1": row["fy1"], "fy2": row["fy2"],
            "eps_fy1": round(row["eps_fy1"], 2),
            "eps_fy2": round(row["eps_fy2"], 2),
            "n_analysts": int(row["n_analysts"]),
            "fwd_pe": round(row["fwd_pe"], 1),
            "ttm_pe": (round(row["ttm_pe"], 1)
                       if row.get("ttm_pe") is not None else None),
            "growth": round(row["growth"], 4),
            "peg": (round(row["peg"], 3)
                    if row["peg"] is not None else None),
            "rev_90d": (round(row["rev_90d"], 4)
                        if row["rev_90d"] is not None else None),
            "capture": row["capture"],
        })
    _dates = conn.execute(
        "SELECT DISTINCT as_of FROM estimates WHERE broker='consensus_yahoo' "
        "ORDER BY as_of").fetchall()
    conn.close()
    med = None
    if len(pegs) >= _vpe.MIN_GROUP:
        import statistics as _st
        med = round(_st.median(pegs), 3)
    return {"rows": rows, "peg_median": med,
            "anchor_ratio": _vpe.PEG_ANCHOR_RATIO,
            "min_analysts": _vpe.MIN_ANALYSTS,
            # display-group order for the scatter's colour assignment; empty
            # for a sector that declares none (EMS)
            "groups": list((spec.get("est_groups") or {}).keys()),
            # capture date of the Bloomberg 2-yr fwd screenshot feed (None
            # when nothing loaded — the toggle then does not render)
            "pe_2y_asof": pe2y_asof,
            # the replayed date (== today unless a past date was asked for)
            # and the capture dates that exist — the page's date picker uses
            # the first as its floor
            "as_of_view": view,
            "as_of_is_today": view == today,
            "today": today,
            "est_dates": [r[0] for r in _dates]}


def sector_list() -> list[dict]:
    """Sector nav plus a live/awaiting-spec flag for each."""
    return [{"id": s["id"], "label": s["label"],
             "peer_groups": s["peer_groups"],
             "live": bool(s["peer_groups"])} for s in SECTORS]


def sector_detail(sector_id: str, est_date: str | None = None) -> dict:
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
    # The cement pack is a second adapter with its own labels. Merged rather
    # than special-cased so the Cement tab does not render six rows of raw
    # slugs — the exact thing the comment above says entities.name would do.
    try:
        import importlib.util as _u2
        _sc = _u2.spec_from_file_location(
            "_cp", REPO / "packages" / "adapters" / "cement_pack.py")
        _cp = _u2.module_from_spec(_sc)
        _sc.loader.exec_module(_cp)
        LABELS.update({eid: f"Cement price, {name}, Rs/t (monthly)"
                       for eid, name in _cp.ROWS.values()})
    except Exception:
        pass
    # Mining's six filing series, same mechanism — mining_filings.SERIES is
    # already the id -> human-label map, so it is merged rather than restated.
    # Without this the Mining tab rendered six rows of raw slugs, the exact
    # failure the two blocks above exist to prevent.
    try:
        import importlib.util as _u3
        _sm = _u3.spec_from_file_location(
            "_mf", REPO / "packages" / "adapters" / "mining_filings.py")
        _mf = _u3.module_from_spec(_sm)
        _sm.loader.exec_module(_mf)
        LABELS.update(_mf.SERIES)
    except Exception:
        pass

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

    # Full history for the tab's chart, where the sector declares one.
    # Divided into the DISPLAY unit here (Rs/t -> Rs/bag for cement) so the
    # page never carries unit arithmetic — the 20x is exactly the class of
    # constant that gets inverted silently in a second place.
    price_chart = None
    cfg = spec.get("chart")
    if cfg:
        div = cfg.get("divide", 1.0)
        series = []
        for eid in cfg["ids"]:
            pts = conn.execute(
                "SELECT date, close FROM prices WHERE entity_id=? "
                "AND close IS NOT NULL ORDER BY date", (eid,)).fetchall()
            series.append({"id": eid,
                           "pts": [{"d": p["date"], "v": p["close"] / div}
                                   for p in pts]})
        price_chart = {"unit": cfg["unit"], "series": series,
                       "marks": cfg.get("marks", []),
                       # served, not hardcoded in the page — see the cement
                       # chart dict's note
                       "title": cfg.get("title") or f"{spec['label']} prices",
                       "caption": cfg.get("caption") or ""}
    conn.close()

    return {"id": spec["id"], "label": spec["label"],
            "live": bool(spec["peer_groups"]),
            "peer_groups": spec["peer_groups"],
            "commodities": rows,
            "unclaimed": unclaimed,
            "price_chart": price_chart,
            # cement_watch() opens its own connection, hence after conn.close()
            "watch": cement_watch() if spec.get("watch") else None,
            # consensus_panel() likewise
            "estimates": (consensus_panel(spec["id"], est_date)
                          if spec.get("estimates") else None),
            "n_priced": sum(1 for r in rows if r["last"] is not None),
            "n_total": len(rows)}

# ---------------------------------------------------------------------------
# NAV — the page's top-level tabs. Two of them are not sectors, so the nav is a
# separate list rather than SECTORS with special cases bolted on.
# ---------------------------------------------------------------------------
def nav_list() -> list[dict]:
    out = [{"id": "overview", "kind": "overview", "label": "Daily Overview",
            "live": True},
           # The Book got its own tab 2026-08-30 (PM: the Overview is the
           # morning read; the book will be reworked separately). Same data
           # as before — /api/overview's book block — different address.
           {"id": "book", "kind": "book", "label": "The Book", "live": True},
           # live since 2026-09-02: F1 computes and persists; F2-F4 still scoped.
           {"id": "flows", "kind": "flows", "label": "Flows", "live": True}]
    # Company coverage (2026-09-14) is NOT a sector and NOT a scoring path:
    # dossiers are built from earnings-call transcripts alone and feed no
    # pillar; invariant 7 is untouched because nothing here reaches
    # pillar_scores. It WAS a top-level tab. PM 2026-09-16: "sits out like a
    # sore spot. Keep it within the sectors only" — so each sector carries
    # its own dossiers and the page renders them as a "Company historical
    # analysis" sub-view of that sector. A dossier whose sector cannot be
    # resolved is listed on `company_unplaced` (on the overview entry) rather
    # than dropped: a company that vanished from the page because a key was
    # missing is the silent shape.
    companies = company_list()
    for s in sector_list():
        spec = next(x for x in SECTORS if x["id"] == s["id"])
        mine = [c for c in companies if c["sector"] == s["id"]]
        out.append({"id": s["id"], "kind": "sector", "label": s["label"],
                    "live": s["live"], "peer_groups": s["peer_groups"],
                    # The page shows the Prices & Watch sub-view only where a
                    # sector declares content for it — cement and ems today.
                    "has_chart": bool(spec.get("chart")),
                    "has_watch": bool(spec.get("watch")),
                    "has_estimates": bool(spec.get("estimates")),
                    "has_company": bool(mine),
                    "companies": mine})
    unplaced = [c for c in companies if c["sector"] is None]
    if unplaced:
        out[0]["company_unplaced"] = unplaced
    # Results — LAST, per the PM (2026-09-22: "add a Results tab in the end").
    # Sell-side estimates against reported prints, sector -> company -> quarter.
    # A read surface over `estimates` + `observations`(actual) +
    # `results_calendar`; packages/results/results_io.py owns the arithmetic
    # and the loader. Not a sector (no peer groups, no pillars) and not
    # sector-scoped in the page: it carries its own sector/company picker.
    out.append({"id": "results", "kind": "results", "label": "Results",
                "live": True})
    return out


def _read_json(path):
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _price_linked_ids() -> set:
    """Every series some spec price_links — the modelled inputs, nothing else.

    Shared by overview()'s what-moved table and input_history(), so the
    expandable chart can only ever be asked for a series that table shows.
    Errors collapse to an empty set: a specs problem must not 500 the page.
    """
    try:
        entities, _u, _f = load_specs()
    except Exception:
        return set()
    linked: set = set()

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
    return linked


def input_history(entity_id: str) -> dict:
    """Price series for ONE modelled input — the Overview's what-moved rows
    expand into this chart on click, the OI-row grammar (2026-09-08, PM).

    SCOPE GUARD: only ids the what-moved table itself lists (price_linked in
    some spec) are served — the endpoint mirrors the table, so it cannot be
    used to chart a parked column and make it read as if it drives something.

    Two years of rows, not all of them: the table's question is "what moved
    lately"; a 2011-to-date cement series at monthly cadence renders as a
    plausible-looking staircase that hides the recent move the click was
    about. The full span is stated in the payload so the cut is visible.

    `source` rides on every row because several series change custody
    mid-stream (pack -> westmetall on lme_zinc, NULL -> yahoo on
    alumina_index) and a shock whose endpoints have different sources is
    worth checking before trusting — the chart captions the mix.
    """
    if entity_id not in _price_linked_ids():
        return {"error": f"'{entity_id}' is not a modelled input "
                         "(nothing price_links it)"}
    conn = connect()
    span = conn.execute(
        "SELECT MIN(date) a, MAX(date) b, COUNT(*) n FROM prices "
        "WHERE entity_id=? AND close IS NOT NULL", (entity_id,)).fetchone()
    if not span or not span["n"]:
        conn.close()
        return {"error": f"no priced rows for '{entity_id}'"}
    rows = conn.execute(
        "SELECT date, close, source FROM prices WHERE entity_id=? "
        "AND close IS NOT NULL AND date >= date(?, '-730 day') "
        "ORDER BY date", (entity_id, span["b"])).fetchall()
    conn.close()
    return {"id": entity_id,
            "rows": [{"date": r["date"], "close": r["close"],
                      "source": r["source"] or "legacy"} for r in rows],
            "first_ever": span["a"], "n_ever": span["n"]}


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
    linked = _price_linked_ids()

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


_BUBBLES_CACHE: dict = {}


def oi_bubbles() -> dict:
    """F2 sector-positioning bubbles: per-sector daily OI level (Rs cr) and
    new-positioning flow, full F&O universe, for the Flows tab's animation.

    Aggregates ~55k fo_oi rows; memoised on (max date, row count) so the
    once-per-morning data change pays the ~80ms and every reload after is a
    dict lookup — the page must render in ms and identically per reload.
    """
    conn = connect()
    key = conn.execute("SELECT MAX(date), COUNT(*) FROM fo_oi").fetchone()
    key = (key[0], key[1])
    if _BUBBLES_CACHE.get("key") == key:
        conn.close()
        return _BUBBLES_CACHE["val"]

    smap = {r[0]: r[1] for r in
            conn.execute("SELECT symbol, sector FROM fo_sector_map")}
    lvl: dict = {}
    flw: dict = {}
    for sym, d, oi, chg, close in conn.execute(
            "SELECT symbol, date, oi_shares, oi_chg, close FROM fo_oi "
            "ORDER BY date"):
        sec = smap.get(sym)
        if not sec:
            continue  # names that left the F&O universe — tiny tail
        lvl.setdefault(d, {}).setdefault(sec, 0.0)
        flw.setdefault(d, {}).setdefault(sec, 0.0)
        lvl[d][sec] += oi * close / 1e7
        flw[d][sec] += chg * close / 1e7
    conn.close()
    dates = sorted(lvl)
    if not dates:
        return {"error": "fo_oi is empty - run fo_bhavcopy.py --map --load"}
    sectors = sorted({s for d in lvl.values() for s in d},
                     key=lambda s: -lvl[dates[-1]].get(s, 0))
    short = {
        "Financial Services": "Financials", "Capital Goods": "Cap Goods",
        "Information Technology": "IT", "Metals & Mining": "Metals",
        "Oil Gas & Consumable Fuels": "Oil & Gas", "Healthcare": "Health",
        "Automobile and Auto Components": "Autos",
        "Telecommunication": "Telecom",
        "Fast Moving Consumer Goods": "FMCG", "Power": "Power",
        "Consumer Services": "Cons Svcs", "Consumer Durables": "Durables",
        "Services": "Services", "Construction Materials": "Cement",
        "Realty": "Realty", "Construction": "Constr", "Chemicals": "Chems",
        "Textiles": "Textiles",
    }
    val = {
        "dates": dates,
        "total": [round(sum(lvl[d].values())) for d in dates],
        "sectors": [{
            "name": s, "short": short.get(s, s[:9]),
            "lvl": [round(lvl[d].get(s, 0)) for d in dates],
            "flw": [round(flw[d].get(s, 0)) for d in dates],
        } for s in sectors],
    }
    _BUBBLES_CACHE.update(key=key, val=val)
    return val


# deliveries_coverage() lived here for part of 2026-09-05 and was removed
# the same day at the PM's instruction ("no insights"). The deliveries
# table keeps loading; review/sector_flows.py still reads it.


def oi_movers(date: str | None, win: int = 5) -> dict:
    """Top and bottom stock-level positioning flows over `win` sessions ending
    at `date` (default: newest). Flow = sum(oi_chg x close)/1e7 — the rupee
    value of positions actually added or removed, the same measure as the
    bubbles' fill. pct = that net against the name's OI at the window start,
    so a 200cr add to a small name reads louder than to HDFC Bank."""
    win = max(1, min(int(win or 5), 60))
    conn = connect()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM fo_oi ORDER BY date")]
    if not dates:
        conn.close()
        return {"error": "fo_oi is empty"}
    if date not in dates:
        date = dates[-1]
    i1 = dates.index(date)
    i0 = max(0, i1 - win + 1)
    d0, d1 = dates[i0], dates[i1]
    smap = {r[0]: r[1] for r in
            conn.execute("SELECT symbol, sector FROM fo_sector_map")}
    flows: dict[str, float] = {}
    base: dict[str, float] = {}
    for sym, d, oi, chg, close in conn.execute(
            "SELECT symbol, date, oi_shares, oi_chg, close FROM fo_oi "
            "WHERE date>=? AND date<=?", (d0, d1)):
        if sym not in smap:
            continue
        flows[sym] = flows.get(sym, 0.0) + chg * close / 1e7
        if d == d0:
            base[sym] = oi * close / 1e7
    conn.close()
    rows = sorted(flows.items(), key=lambda kv: kv[1])

    def fmt(sym, v):
        b = base.get(sym)
        return {"symbol": sym, "sector": smap.get(sym, "?"),
                "flow_cr": round(v),
                "pct_of_oi": round(100 * v / b, 1) if b else None}
    return {"date_from": d0, "date_to": d1, "win": win,
            "in": [fmt(s, v) for s, v in reversed(rows[-6:])],
            "out": [fmt(s, v) for s, v in rows[:6]]}


def _mark_repeated_bullets(brief: dict, mdir, brief_date: str) -> None:
    """Mark brief bullets whose MAIL already appeared in an earlier brief.

    The sweep window is 24h and the brief runs daily, so the windows overlap:
    a note received yesterday morning is a legitimate candidate two mornings
    running, and the agent that writes the brief cannot be trusted to
    remember what it showed before (a fresh agent writes each brief). This is
    the deterministic guard — PM instruction 2026-09-08: a mail shown before
    is MARKED stale, never dropped, so the page greys it rather than hiding
    that the mail exists.

    IDENTITY IS THE MAIL, NOT THE WORDS ALONE. Bullets are rewritten each
    day, so neither the text nor the source line identifies a mail by
    itself; the match combines them. Sources ("Broker — subject") are
    normalised (case, punctuation, FW:/RE: tokens, the "(+N more in
    Outlook)" suffix) and compared by prefix either way (>=20 chars) because
    agents truncate long subjects differently between days. Text overlap is
    shared-token share against the smaller bullet (words >=3 chars) — the
    same mail re-bulleted keeps its numbers even when the sentence is
    rewritten.

    A source is ONE-OFF when it appeared on exactly one previous brief date
    — dated subjects ("week ended 04 Sep 2026") key each issue separately,
    so a weekly's new issue is one-off too. A RECURRING source ("Basic
    materials - daily news and prices") shows up every morning with new
    content and gets no benefit of the doubt. A bullet is stale when its
    source matches a previous bullet's and:

      1. received HH:MM also matches — for a recurring source the text must
         echo too (>=0.25 overlap), because consecutive dailies land minutes
         apart and can collide on HH:MM; for a one-off source the time match
         settles it.
      2. no time match, one-off source, text echoes (>=0.25) — the forwarded
         copy: yesterday's brief cited the original Nomura steel weekly at
         03:08, today's agent bulleted the FW: of the same issue at 11:23.

    Calibrated 2026-09-08 against the real 09-07/09-08 pair: marks the
    repeated Kotak items (same mail, same minute, echoed text), the FW:'d
    Nomura weekly and the re-run CLSA VRRR note; leaves the NEW day's Kotak
    daily (08:56 vs 08:53, different content) fresh.

    Writes `seen_on` (the latest earlier brief date) onto matching bullets;
    everything else is untouched. Marking must never break the page: any
    failure leaves the brief unmarked.
    """
    import re as _re
    try:
        sectors = (brief.get("mail") or {}).get("sectors") or []
        if not sectors:
            return

        def norm(s):
            s = (s or "").lower()
            s = _re.sub(r"\(\+\d+ more[^)]*\)", " ", s)
            s = _re.sub(r"\b(fw|re|fwd)\b", " ", s)
            s = _re.sub(r"[^a-z0-9]+", " ", s).strip()
            return s

        def toks(s):
            return {w for w in norm(s).split() if len(w) >= 3}

        def match(a, b):
            return len(a) >= 20 and len(b) >= 20 and (
                a.startswith(b) or b.startswith(a))

        def echo(a, b):
            if not a or not b:
                return False
            return len(a & b) / min(len(a), len(b)) >= 0.25

        # previous briefs, newest first, at most 7 — the overlap is one day
        # by construction, the rest is insurance against a skipped morning
        prev = sorted(
            (m.group(1) for p in mdir.glob("brief_*.json")
             if (m := _re.match(r"brief_(\d{4}-\d{2}-\d{2})\.json$", p.name))
             and m.group(1) < brief_date),
            reverse=True)[:7]
        seen = []            # (norm_source, received, text_tokens, brief_date)
        src_dates: dict = {}  # norm_source -> set of brief dates
        for d in prev:
            doc = _read_json(mdir / f"brief_{d}.json") or {}
            for sec in (doc.get("mail") or {}).get("sectors") or []:
                for b in sec.get("bullets") or []:
                    ns = norm(b.get("source"))
                    if not ns:
                        continue
                    seen.append((ns, (b.get("received") or "").strip(),
                                 toks(b.get("text")), d))
                    src_dates.setdefault(ns, set()).add(d)

        for sec in sectors:
            for b in sec.get("bullets") or []:
                ns = norm(b.get("source"))
                rc = (b.get("received") or "").strip()
                tk = toks(b.get("text"))
                if not ns:
                    continue
                hit = None
                for ps, pr, pt, pd in seen:
                    if not match(ns, ps):
                        continue
                    one_off = len(src_dates.get(ps, ())) == 1
                    same = ((rc and rc == pr and (one_off or echo(tk, pt)))
                            or (one_off and echo(tk, pt)))
                    if same and (hit is None or pd > hit):
                        hit = pd
                if hit:
                    b["seen_on"] = hit
    except Exception:
        pass  # a marking failure must never take the brief down with it


def morning() -> dict:
    """The morning brief: pre-market globals + broker-mail actionables.

    ASSEMBLED FROM FILES, NEVER FETCHED HERE. morning_markets.py (python,
    unattended) writes markets_YYYY-MM-DD.json; the morning-brief skill
    (agent — it needs the M365 MCP and judgment) writes brief_YYYY-MM-DD.json.
    This endpoint only reads the newest of each and says HOW OLD it is. A
    request must never trigger a network fetch — the page has to render in
    milliseconds and identically on every reload, or the first screen becomes
    the flakiest one.

    STALENESS IS THE CONTRACT. Yesterday's brief rendering as this morning's
    is the same failure the refresh light exists for, so both halves carry
    their file date and a warning whenever it is not today's. The tab must
    render the warning, not just the bullets.
    """
    import datetime as dt
    import re as _re

    today = dt.date.today().isoformat()
    out: dict = {"as_of": today, "markets": None, "brief": None, "warnings": []}
    mdir = REPO / "data" / "morning"

    def newest(prefix: str):
        # Parse the date OUT of the name rather than trusting mtime (OneDrive
        # rewrites mtimes) or lexical order of anything non-ISO.
        best, best_d = None, None
        if mdir.exists():
            for p in mdir.glob(f"{prefix}_*.json"):
                m = _re.match(rf"{prefix}_(\d{{4}}-\d{{2}}-\d{{2}})\.json$",
                              p.name)
                if m and (best_d is None or m.group(1) > best_d):
                    best, best_d = p, m.group(1)
        return best, best_d

    for prefix, key, maker in (
            ("markets", "markets", "python packages/adapters/morning_markets.py"),
            ("brief", "brief", "the morning-brief skill (agent)")):
        p, d = newest(prefix)
        if p is None:
            out["warnings"].append(f"no {prefix} file yet — run {maker}")
            continue
        doc = _read_json(p)
        if doc is None:
            out["warnings"].append(f"{p.name} exists but does not parse")
            continue
        out[key] = doc
        if key == "brief":
            # deterministic repeat-mail marking — see _mark_repeated_bullets
            _mark_repeated_bullets(doc, mdir, d)
        if d != today:
            out["warnings"].append(
                f"{prefix} is from {d}, not {today} — run {maker}")
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

    n_mr = scalar("SELECT COUNT(*) FROM market_regime") or 0
    mr_last = scalar("SELECT MAX(as_of) FROM market_regime")

    monitors = [
        {"id": "F1", "monitor": "Risk on / risk off",
         "question": "is the market paying for risk at all today",
         "scope": "market-wide", "lands_in": "market_regime",
         "state": f"LIVE, {n_mr} sessions",
         "detail": (f"Built 2026-09-02 from the coach's framing: sign patterns "
                    f"of (equity, bond price, gold) -> 8 named states + a quiet "
                    f"overlay, a SOX-IGV rotation overlay, and a windowed "
                    f"flow-spell layer. Newest {mr_last}. specs/flows.yaml is "
                    f"the frozen method; regime.py --backtest is the evidence")},
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

    # ---- F1: the market regime read (built 2026-09-02) --------------------
    # Read from the PERSISTED table, matrix recomputed over it in-process —
    # a few ms over ~2.5k rows, and the page must never trigger a fetch.
    # The transition percentages are EMPIRICAL BASE RATES over the classified
    # history, not a model.
    f1 = None
    try:
        import datetime as _dt

        import regime as rg
        mr = conn.execute(
            "SELECT as_of, state, loud_state, quiet, z_eq, z_bond, z_gold, "
            "rot_z, rotation, flow_intensity, flow_driver, spell_quiet, "
            "spec_version FROM market_regime ORDER BY as_of").fetchall()
        if mr:
            days = [dict(r) for r in mr]
            tr = rg.transitions(days)
            last = days[-1]
            nxt = sorted(tr["pct"].get(last["state"], {}).items(),
                         key=lambda kv: -kv[1])
            spell_run = 0
            for d in reversed(days):
                if (d["spell_quiet"] is None
                        or d["spell_quiet"] != last["spell_quiet"]):
                    break
                spell_run += 1
            # US close T-1/T-2 is normal from India; beyond ~6 calendar days
            # the read is STALE and the tab must lead with that, not with a
            # regime nobody refreshed (invariant 3: old and unchanged must be
            # distinguishable).
            stale_days = (_dt.date.today()
                          - _dt.date.fromisoformat(last["as_of"])).days
            f1 = {
                **{k: last[k] for k in
                   ("as_of", "state", "loud_state", "quiet", "z_eq", "z_bond",
                    "z_gold", "rot_z", "rotation", "flow_intensity",
                    "flow_driver", "spell_quiet", "spec_version")},
                "spell_run": spell_run,
                "stale_days": stale_days,
                "stale": stale_days > 6,
                "n_observations": sum(tr["counts"].get(last["state"], {})
                                      .values()),
                "next": [{"state": s, "pct": round(v, 1),
                          "base": round(tr["base"].get(s, 0.0), 1)}
                         for s, v in nxt],
                "base": {s: round(v, 1) for s, v in tr["base"].items()},
                "n_days": tr["n_days"],
                "first": days[0]["as_of"],
                "strip": [{k: d[k] for k in
                           ("as_of", "state", "quiet", "spell_quiet",
                            "rotation")} for d in days[-15:]],
            }
    except Exception as e:  # the readiness tables must render regardless
        f1 = {"error": str(e)}

    # ---- W1: the week-scale read the tab LEADS with. Since 2026-09-08 the
    # lead is the ROLLING past week, updated every US session (PM: "update
    # daily... show weekly trend but calculate past week on a rolling
    # basis"), with the Friday-to-Friday layer kept as the trend strip and
    # the evidence base — superseding the 2026-09-03 Friday-week lead.
    # Computed on demand from flow_series — a pure function of stored prices
    # + the spec, a few ms; persisting it would only add a staleness mode.
    # f1 stays in the payload: the spell lives there and the review layer
    # will want the daily states.
    try:
        import regime as rg2
        w1 = rg2.weekly_view()
    except Exception as e:
        w1 = {"error": str(e)}
    conn.close()

    return {
        "w1": w1,
        "f1": f1,
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


def book_view() -> dict:
    """The PM's ACTUAL book — /api/book. Pair-wise IMS positions from the
    daily_review skill's snapshots, P&L chained across futures rolls by
    packages/book/book_io.py. Read-only here; nothing on this path writes.

    Model context is joined per leg through specs/book.yaml's ticker_map
    (Bloomberg root -> entity_id) — EMPTY until the desk fills it at
    calibration, because a guessed mapping glues the wrong model to a real
    position. An unmapped leg shows no composite, not a wrong one.
    """
    sys.path.insert(0, str(REPO / "packages" / "book"))
    import book_io
    rep = book_io.pair_report()

    cfg_path = REPO / "specs" / "book.yaml"
    tmap, carry, names, plabels = {}, {}, {}, {}
    if cfg_path.exists():
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        tmap = cfg.get("ticker_map") or {}
        carry = cfg.get("carry") or {}
        names = cfg.get("names") or {}
        # THE PM'S DICTATED PAIR LIST (06-09-2026) — the display truth. The
        # IMS tags are coarser clusters and stay internal.
        spec_pairs = cfg.get("pairs") or {}

    conn = connect()
    comp = {}
    as_of = conn.execute("SELECT MAX(as_of) FROM pillar_scores").fetchone()[0]
    if as_of:
        for r in conn.execute(
                "SELECT entity_id, score FROM pillar_scores "
                "WHERE as_of=? AND pillar='composite'", (as_of,)):
            comp[r["entity_id"]] = r["score"]

    last_review = {}
    for r in conn.execute(
            "SELECT pair_tag, verdict, review_date, thesis_intact FROM "
            "book_pair_reviews ORDER BY review_date, id"):
        last_review[r["pair_tag"]] = {
            "verdict": r["verdict"], "date": r["review_date"],
            "thesis_intact": r["thesis_intact"]}

    # The page never prints the pair tag (PM, 06-09-2026) — pairs display as
    # Long_Short built from specs/book.yaml `names` (e.g. Coforge_Persistent),
    # unless the PM dictated a label in `pair_labels`.
    def _label(longs: list, shorts: list) -> str:
        L, S = "+".join(longs), "+".join(shorts)
        if L and S:
            return f"{L}_{S}"
        return f"{S} (short)" if S else f"{L} (long)"

    def _close(eid: str, upto: str):
        r = conn.execute(
            "SELECT close FROM prices WHERE entity_id=? AND date<=? "
            "ORDER BY date DESC LIMIT 1", (eid, upto)).fetchone()
        return r["close"] if r else None

    for p in rep["pairs"]:
        for leg in p["legs"]:
            # the map keys on the ticker's first token (IMS-Spec convention);
            # full-root fallback covers any hand-added entries
            tok = (leg["root"].split() or [""])[0]
            eid = tmap.get(tok) or tmap.get(leg["root"]) or tmap.get(leg["name"])
            leg["entity_id"] = eid
            leg["composite"] = comp.get(eid) if eid else None
            leg["name"] = names.get(tok, leg["name"])
            # %-SINCE-ENTRY per leg: latest close vs the trade anchor, in
            # precedence order entry_open -> entry_cost -> first-seen close.
            # The anchor is the OPEN of the entry day (PM rule 2026-09-07:
            # "the purpose is to see if the pair has worked out in thesis" —
            # the IMS avg cost blends adds and pre-capture history, so it
            # answers a different question). book_io._entry_anchor keeps the
            # anchor date fixed through resizes and pair-tag changes; it
            # resets only on a direction flip or a day out of the book. All
            # INR against INR — no FX leg by construction.
            leg["ret_pct"] = None
            if eid:
                base = (leg.get("entry_open") or leg.get("entry_cost")
                        or _close(eid, leg["first_seen"]))
                now = _close(eid, rep["as_of"])
                if base and now:
                    leg["ret_pct"] = round((now / base - 1) * 100, 2)
        p["long"] = [x["name"] for x in p["legs"] if x["side"] == "L"]
        p["short"] = [x["name"] for x in p["legs"] if x["side"] == "S"]
        p["label"] = _label(p["long"], p["short"])
        # THE PAIR % (PM example: long +10%, short -5% -> pair +15%): average
        # of long legs' price moves MINUS average of short legs'. Averaging
        # per side generalises the 1v1 example to 3- and 4-leg pairs without
        # double-counting a side. None if any leg lacks a price.
        lr = [x["ret_pct"] for x in p["legs"] if x["side"] == "L"]
        sr = [x["ret_pct"] for x in p["legs"] if x["side"] == "S"]
        if all(r is not None for r in lr + sr) and (lr or sr):
            lm = sum(lr) / len(lr) if lr else 0.0
            sm = sum(sr) / len(sr) if sr else 0.0
            p["ret_pct"] = round(lm - sm, 2)
        else:
            p["ret_pct"] = None
        p["review"] = last_review.get(p["pair"])
        # carry: desk-stated P&L from before the first snapshot (specs/book.yaml)
        c = carry.get(p["pair"])
        if c:
            p["carry"] = c
            p["pnl_total_with_carry"] = round(
                p["pnl_total"] + (c.get("pnl") or 0), 2)
    conn.close()
    for c in rep["closed"]:
        c["long"] = [names.get(n, n) for n in c.get("long") or []]
        c["short"] = [names.get(n, n) for n in c.get("short") or []]
        c["label"] = _label(c["long"], c["short"])
    rep["scores_as_of"] = as_of
    rep["n_mapped"] = sum(1 for p in rep["pairs"] for x in p["legs"]
                          if x.get("entity_id"))

    # ---- the display view: the PM's dictated pairs over the stored legs ----
    # A leg may serve several pairs (TCS long vs three shorts). DOLLARS are
    # apportioned across a shared leg's pairs by the gross of the OPPOSITE
    # side of each pair (the long splits pro-rata over what it hedges), so
    # book totals still sum without double counting. PRICE-%s are never
    # apportioned — a % move is size-free. Falls back to the tag grouping
    # when no pairs are dictated.
    tokens: dict = {}
    for p in rep["pairs"]:
        for leg in p["legs"]:
            tok = (leg["root"].split() or [""])[0]
            t = tokens.setdefault(tok, {
                "token": tok, "name": leg["name"],
                "entity_id": leg["entity_id"], "composite": leg["composite"],
                "book_side": leg["side"], "qty": 0.0,
                "gross_pct": 0.0, "gross_usd": 0.0, "pnl_dtd": 0.0,
                "pnl_mtd": 0.0, "pnl_total": 0.0, "pnl_ytd": 0.0,
                "ytd_flags": [],
                "ret_pct": leg["ret_pct"], "first_seen": leg["first_seen"],
                "entry_cost": leg["entry_cost"],
                "entry_open": leg["entry_open"],
                "ticker_now": leg["ticker_now"], "rolls": 0, "gap": False})
            t["qty"] += leg["qty"] or 0
            t["gross_pct"] += abs(leg["mv_pct"] or 0)
            t["gross_usd"] += abs(leg["mv_usd"] or 0)
            t["pnl_dtd"] += leg["pnl_dtd"] or 0
            t["pnl_mtd"] += leg["pnl_mtd"] or 0
            t["pnl_total"] += leg["pnl_total"] or 0
            t["pnl_ytd"] += leg["pnl_ytd"] or 0
            t["ytd_flags"] += leg.get("ytd_flags") or []
            t["rolls"] += leg["rolls"] or 0
            t["gap"] = t["gap"] or p.get("gap_risk", False)
            t["first_seen"] = min(t["first_seen"], leg["first_seen"])

    if spec_pairs and tokens:
        view = []
        for sector, plist in spec_pairs.items():
            for sp in plist or []:
                view.append({"sector": sector, "label": str(sp.get("name")),
                             "L": [str(x) for x in (sp.get("long") or [])],
                             "S": [str(x) for x in (sp.get("short") or [])]})
        for v in view:
            v["_lg"] = sum(tokens[t]["gross_usd"] for t in v["L"] if t in tokens)
            v["_sg"] = sum(tokens[t]["gross_usd"] for t in v["S"] if t in tokens)
        member: dict = {}
        for v in view:
            for t in v["L"]:
                member.setdefault(t, []).append((v, "L"))
            for t in v["S"]:
                member.setdefault(t, []).append((v, "S"))

        def _share(tok: str, v: dict, side: str) -> float:
            mems = member[tok]
            if len(mems) == 1:
                return 1.0
            others = [(m["_sg"] if s == "L" else m["_lg"]) for m, s in mems]
            mine = v["_sg"] if side == "L" else v["_lg"]
            tot = sum(others)
            return (mine / tot) if tot > 0 else 1.0 / len(mems)

        out, assigned = [], set()
        for v in view:
            L = [tokens[t] for t in v["L"] if t in tokens]
            S = [tokens[t] for t in v["S"] if t in tokens]
            missing = [t for t in v["L"] + v["S"] if t not in tokens]
            if not L and not S:
                continue                    # pair fully off the book today
            assigned |= {t["token"] for t in L + S}
            agg = {"gross_pct": 0.0, "gross_usd": 0.0, "pnl_dtd": 0.0,
                   "pnl_mtd": 0.0, "pnl_total": 0.0, "pnl_ytd": 0.0}
            legs = []
            for side, arr in (("L", L), ("S", S)):
                for t in arr:
                    w = _share(t["token"], v, side)
                    for k in agg:
                        agg[k] += (t[k] or 0) * w
                    legs.append({**t, "side": side, "share": round(w, 3),
                                 "side_mismatch": t["book_side"] != side})
            lr = [t["ret_pct"] for t in L]
            sr = [t["ret_pct"] for t in S]
            ret = None
            if all(r is not None for r in lr + sr) and (lr or sr):
                lm = sum(lr) / len(lr) if lr else 0.0
                sm = sum(sr) / len(sr) if sr else 0.0
                ret = round(lm - sm, 2)
            inception = max(t["first_seen"] for t in L + S)
            c = carry.get(v["label"])
            out.append({
                "sector": v["sector"], "label": v["label"],
                "ret_pct": ret, "inception": inception,
                "days": (dt_date(rep["as_of"]) - dt_date(inception)).days,
                "gross_pct": round(agg["gross_pct"], 6),
                "gross_usd": round(agg["gross_usd"], 0),
                "pnl_dtd": round(agg["pnl_dtd"], 2),
                "pnl_mtd": round(agg["pnl_mtd"], 2),
                "pnl_total": round(agg["pnl_total"], 2),
                # the calendar-YTD ledger book_io maintains — NOT the IMS YTD
                # column, which resets at every roll (PM 2026-09-18)
                "pnl_ytd": round(agg["pnl_ytd"], 2),
                "ytd_flags": sorted({f for t in L + S
                                     for f in (t["ytd_flags"] or [])}),
                "pnl_total_with_carry": (round(agg["pnl_total"] +
                    (c.get("pnl") or 0), 2) if c else None),
                "carry": c,
                "rolls": sum(t["rolls"] for t in L + S),
                "gap_risk": any(t["gap"] for t in L + S),
                "missing": missing, "legs": legs,
                "review": last_review.get(v["label"]),
            })
        rep["view"] = out
        rep["sectors_order"] = list(spec_pairs.keys())
        rep["unassigned"] = sorted(
            [t for k, t in tokens.items() if k not in assigned],
            key=lambda t: -t["gross_usd"])
    else:
        # no dictated pairs: synthesize the view from the tag grouping
        rep["view"] = [{**p, "sector": (p["pair"].rstrip("0123456789 ") or
                                        p["pair"]).upper()}
                       for p in rep["pairs"]]
        rep["sectors_order"] = []
        rep["unassigned"] = []
    return rep


# ---------------------------------------------------------------------------
# the book's equal-weighted long and short indices — /api/book_index
# ---------------------------------------------------------------------------
#
# ONE METHOD, AND IT REPLACED TWO. Until 2026-09-17 this file carried
# `book_ohlc` (a portfolio-weighted back-cast of today's roster over a chosen
# window) and a first `book_index` (membership straight off the stored
# snapshots, so it could not begin before 2026-09-04). The PM's verdict on
# having both: "Let's rebuild the whole thing again. We get rid of both graph
# methods." Do not reintroduce either — the survivorship caveat that made two
# charts seem necessary is handled inside the one that replaced them, by
# freezing membership at a spec date instead of at today.
#
# The arithmetic lives in packages/book/basket_index.py, which owns the
# docstring, the guards and the selftest. This is a transport shim: everything
# here is a window onto a series computed there.


def book_index(rng: str = "5y") -> dict:
    """Equal-weighted long/short indices, windowed for display.

    Recomputed per request from `prices` and `book_positions` — there is no
    index table, so nothing here can be stale relative to the store. See
    basket_index.build for why that is cheap enough to prefer.
    """
    sys.path.insert(0, str(REPO / "packages" / "book"))
    import basket_index
    if rng not in basket_index.RANGES:
        return {"error": f"unknown range {rng!r}; known: "
                         + ", ".join(basket_index.RANGES)}
    conn = connect()
    try:
        payload = basket_index.build(conn, rng)
    finally:
        conn.close()
    return basket_index.window(payload, rng)


def dt_date(s: str):
    import datetime as _dt
    return _dt.date.fromisoformat(s)


# ---------------------------------------------------------------- company
# One company = one folder under data/companies/<slug>/, holding dossier.json
# plus the filed sources it was built from. Nothing here is hardcoded: the
# universe IS the set of folders, so adding a company is a directory, never a
# code change. Read-only; no adapter writes to it and no pillar reads it.
COMPANIES = REPO / "data" / "companies"


def _company_sector(doc: dict) -> tuple[str | None, str]:
    """Which sector tab a dossier renders under, and how that was decided.

    Order: the dossier's own `sector` key (the skill writes it since
    2026-09-16); else the entity spec whose `nse_symbol` matches the
    dossier's NSE ticker; else the `entities` table on the same symbol (the
    IT roster lives only there). Anything else is None — UNPLACED, surfaced
    on the nav, never guessed into a sector. Only ids in SECTORS count: a
    typo in the key must show up as unplaced, not as a tab nobody can open.
    """
    known = {x["id"] for x in SECTORS}
    sec = doc.get("sector")
    if sec in known:
        return sec, "dossier"
    nse = ((doc.get("tickers") or {}).get("nse") or "").strip().upper()
    if nse:
        try:
            entities, _u, _f = load_specs()
            for e in entities.values():
                if str(e.get("nse_symbol") or "").upper() == nse \
                        and e.get("sector") in known:
                    return e["sector"], "spec"
        except Exception:
            pass
        try:
            conn = connect()
            row = conn.execute(
                "SELECT sector FROM entities WHERE upper(nse_symbol)=? "
                "AND sector IS NOT NULL", (nse,)).fetchone()
            conn.close()
            if row and row[0] in known:
                return row[0], "entities"
        except Exception:
            pass
    return None, "unresolved"


def company_list(sector: str | None = None) -> list[dict]:
    """Every company folder holding a readable dossier.json, in folder order.

    `sector` filters to one tab's roster. Each row carries the sector it
    resolved to (None = unplaced) and `sector_via` naming the source.
    """
    if not COMPANIES.is_dir():
        return []
    out = []
    for d in sorted(COMPANIES.iterdir()):
        f = d / "dossier.json"
        if not f.is_file():
            continue
        doc = _read_json(f)
        if not doc:
            continue
        sec, via = _company_sector(doc)
        if sector is not None and sec != sector:
            continue
        out.append({"slug": doc.get("slug", d.name),
                    "company": doc.get("company", d.name),
                    "nse": (doc.get("tickers") or {}).get("nse"),
                    "sector": sec, "sector_via": via,
                    "as_of": doc.get("as_of"),
                    "quarters": len(doc.get("qoq_pnl") or [])})
    return out


def company_view(slug: str | None = None, sector: str | None = None) -> dict:
    """The dossier for one company, plus the roster so the picker can render.

    `sector` scopes the roster to one sector tab (PM 2026-09-16: company
    analysis lives inside the sectors only). An unknown slug falls back to
    the first company IN THAT SECTOR rather than erroring: the tab must never
    render empty because a remembered slug was deleted or belongs elsewhere.
    Dossiers with no resolvable sector ride along as `unplaced` so the page
    can say they exist.
    """
    roster = company_list(sector)
    if not roster:
        return {"error": ("no company dossier for sector %r" % sector
                          if sector else
                          "no company dossiers under data/companies/"),
                "companies": [], "sector_tab": sector,
                "unplaced": [c for c in company_list() if c["sector"] is None]}
    slugs = [c["slug"] for c in roster]
    pick = slug if slug in slugs else slugs[0]
    doc = _read_json(COMPANIES / pick / "dossier.json") or {}
    # Documents are listed FROM DISK, not from the dossier — a filed source the
    # dossier forgot to claim still has to be visible, per the archive rule.
    src = COMPANIES / pick / "sources"
    docs = []
    if src.is_dir():
        for f in sorted(src.glob("*.pdf")):
            note = src / "summaries" / (f.name + ".md")
            docs.append({"file": f.name,
                         "size_kb": round(f.stat().st_size / 1024),
                         "note": note.name if note.is_file() else None})
    doc["documents"] = docs
    doc["companies"] = roster
    doc["selected"] = pick
    doc["sector_tab"] = sector
    doc["unplaced"] = [c for c in company_list() if c["sector"] is None]
    return doc


# ---------------------------------------------------------------------------
# The Results tab — 2026-09-22. Sector -> company -> quarter; sell-side
# estimates against the reported print, and the calendar of prints to come.
#
# Transport shim over packages/results/results_io.py, which owns the
# consensus/surprise arithmetic, the loader and the selftest — the same split
# as book_index over basket_index. Nothing here writes; nothing here reaches
# `prices` or `pillar_scores`.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(REPO / "packages" / "results"))


def _results_roster() -> dict[str, dict]:
    """Every covered name, keyed by entity id, with the sector TAB it sits
    under — or None when nothing can place it.

    The roster is the UNION of three places a name can be known from,
    because no single one covers the book: the entity specs (the five scored
    sectors; a reporting unit such as Novelis takes its PARENT's sector), the
    `entities` table (IT lives only there), and the company dossiers (Ather
    and Eicher have no spec and a NULL sector in the table — the dossier's
    `sector` key is what puts them under Auto, joined on the NSE symbol via
    yahoo_prices.CANDIDATES). Nothing is guessed: a name none of the three
    places lands in `other`, listed rather than dropped.
    """
    from yahoo_prices import CANDIDATES as _CANDS  # adapters path is on sys.path
    known = {s["id"] for s in SECTORS}
    # A scored name's PEER GROUP names its tab. The spec `sector` key is the
    # coverage bucket and does not always match a tab id — aluminium.yaml says
    # `sector: aluminium` for six names whose tab is `non_ferrous` (and for
    # the two zinc names, whose peer group is `zinc`). Resolving through the
    # peer group first is what SECTORS itself does for scoring.
    pg2sec = {pg: s["id"] for s in SECTORS for pg in s["peer_groups"]}

    def place(peer_group, sector):
        return pg2sec.get(peer_group) or (sector if sector in known else None)

    rows: dict[str, dict] = {}
    entities, _u, _f = load_specs()
    for e in entities.values():
        if e.get("kind") not in ("company", "reporting_unit"):
            continue
        sec = place(e.get("peer_group"), e.get("sector"))
        par = e.get("parent_id")
        if e.get("kind") == "reporting_unit" and par in entities:
            p = entities[par]
            sec = place(p.get("peer_group"), p.get("sector")) or sec
        rows[e["id"]] = {"id": e["id"], "name": e.get("name") or e["id"],
                         "sector": sec,
                         "nse": e.get("nse_symbol"),
                         "unit_of": par if e.get("kind") == "reporting_unit" else None,
                         "scored": bool(e.get("peer_group"))}
    conn = connect()
    for r in conn.execute(
            "SELECT id, name, sector, nse_symbol, kind, parent_id, peer_group "
            "FROM entities WHERE kind IN ('company','reporting_unit') AND active=1"):
        if r["id"] in rows:
            if rows[r["id"]]["sector"] is None:
                rows[r["id"]]["sector"] = place(r["peer_group"], r["sector"])
            continue
        rows[r["id"]] = {"id": r["id"], "name": r["name"] or r["id"],
                         "sector": place(r["peer_group"], r["sector"]),
                         "nse": r["nse_symbol"],
                         "unit_of": r["parent_id"] if r["kind"] == "reporting_unit" else None,
                         "scored": bool(r["peer_group"])}
    conn.close()
    sym2eid = {}
    for eid, cands in _CANDS.items():
        for sym, _pat in cands or []:
            sym2eid.setdefault(sym.split(".")[0].upper(), eid)
    for c in company_list():
        eid = sym2eid.get(str(c.get("nse") or "").upper())
        if eid in rows:
            if not rows[eid]["nse"]:
                rows[eid]["nse"] = c.get("nse")
            if rows[eid]["sector"] is None and c["sector"] in known:
                rows[eid]["sector"] = c["sector"]
    return rows


def results_view(sector: str | None = None, entity: str | None = None) -> dict:
    """Payload for the Results tab.

    Always returns EVERY sector chip (a sector with nothing stored reads
    "0 of N names" rather than vanishing) and the full roster of the chosen
    sector (a name with no estimates yet is still offered, flagged, so the
    picker is the coverage list and not a list of what happens to be loaded).
    An unknown sector or entity falls back — first to the one holding data,
    then to the first — so a remembered selection can never render empty.
    """
    import datetime as _dt
    import results_io as _rio

    roster = _results_roster()
    conn = connect()
    sm = _rio.summary(conn, list(roster))
    secs = []
    for s in SECTORS:
        mine = [r for r in roster.values() if r["sector"] == s["id"]]
        secs.append({"id": s["id"], "label": s["label"], "n_names": len(mine),
                     "n_with_data": sum(1 for r in mine if sm[r["id"]]["has_data"])})
    other = [r for r in roster.values() if r["sector"] is None]
    if other:
        secs.append({"id": "other", "label": "Unplaced", "n_names": len(other),
                     "n_with_data": sum(1 for r in other if sm[r["id"]]["has_data"])})
    ids = [s["id"] for s in secs]
    if sector not in ids:
        sector = next((s["id"] for s in secs if s["n_with_data"]), ids[0])
    mine = [r for r in roster.values()
            if (r["sector"] == sector) or (sector == "other" and r["sector"] is None)]
    companies = []
    for r in sorted(mine, key=lambda x: (bool(x["unit_of"]), x["name"].lower())):
        s = sm[r["id"]]
        companies.append({**r, "has_data": s["has_data"],
                          "n_estimates": s["n_estimates"], "n_actuals": s["n_actuals"],
                          "latest_period": s["latest_period"],
                          "next_event": s["next_event"], "brokers": s["brokers"]})
    cids = [c["id"] for c in companies]
    if entity not in cids:
        entity = next((c["id"] for c in companies if c["has_data"]),
                      cids[0] if cids else None)
    periods = _rio.periods_for(conn, entity) if entity else []
    conn.close()
    sel = next((c for c in companies if c["id"] == entity), None)
    return {
        "as_of": _dt.date.today().isoformat(),
        "sectors": secs, "sector": sector,
        "companies": companies, "selected": entity, "company": sel,
        "periods": periods,
        "metrics": {m: {"label": lab, "better": bet}
                    for m, (lab, bet) in _rio.METRICS.items()},
        "totals": {"n_names": len(roster),
                   "n_with_data": sum(1 for v in sm.values() if v["has_data"])},
        "loader": "python packages/results/results_io.py --load "
                  "data/results/staging/<file>.json",
    }
