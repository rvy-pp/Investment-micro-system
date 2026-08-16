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
