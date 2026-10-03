"""The Results tab's arithmetic: sell-side estimates against reported prints,
per company, per quarter — the store for FORECASTING a result before it lands
and GRADING it after.

    python packages/results/results_io.py --load data/results/staging/<file>.json
    python packages/results/results_io.py --load-all          # every staged file, idempotent
    python packages/results/results_io.py --report shree      # one name, every period
    python packages/results/results_io.py --selftest

THERE IS NO NEW NUMBER TABLE, DELIBERATELY. Estimates go in `estimates`, whose
schema comment has said "feeds the Projections tab" since the day it was
written — this is that tab. Reported prints go in `observations` with
factor='actual', the convention concall-ingest and
specs/extracted/steel_actuals.json already use, so the Q1FY27 steel and
aluminium prints that P4 grades against appear here without being re-keyed.
Both tables carry a NOT NULL quote: invariant 1 holds on BOTH sides of every
estimate-vs-actual row. The only new table is `results_calendar` (WHEN a print
lands), because neither existing table has a place for a date that is not a
fact about the business.

Nothing here reaches `prices` or `pillar_scores`. The Results tab is a READ
surface plus a loader; no pillar reads `estimates` rows from a house broker
(the consensus panel and valuation_pe filter on broker='consensus_yahoo' /
'bloomberg', and those two names are REFUSED as brokers here so the two
populations can never blur).

THE THREE GUARDS THAT MATTER, each with an acceptance and a rejection case in
--selftest, per the GLOB lesson:

  * A file loads ALL-OR-NOTHING. One bad row refuses the whole file, with the
    row and the reason named. A half-loaded file is the partially-corrected
    shape from the aluminium clean-up: it looks fixed and is not.
  * UNITS MUST AGREE within (entity, period, metric) — across the file AND
    against what the store already holds. Kotak prints "Rs41.5bn" and Emkay
    "Rs4,150 cr"; a median over the two is 2,095.75 of nothing, plausible and
    wrong, and nothing downstream would complain. The refusal prints both
    units so the author converts BEFORE the number is stored. The quote keeps
    the source's own figure, so the conversion is auditable.
  * AN ESTIMATE DATED AFTER THE PRINT IS NOT A FORECAST. It is kept and shown
    (a post-result revision is information) but excluded from the consensus
    the actual is graded against, and marked `post_print`. Including it would
    be the effective_from look-ahead: the surprise would shrink toward zero
    because the "estimate" already knew the answer.

`better` per metric says which direction is a beat: revenue up is good, cost
per tonne up is not. Capex has no direction and its surprise carries no
verdict — a company under-spending its capex guide is not "beating".
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import math
import pathlib
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
STAGING = REPO / "data" / "results" / "staging"
SCHEMA = REPO / "packages" / "core" / "schema.sql"

EXTRACTOR = "results-v1"

# Brokers that are ADAPTER outputs, not houses. yahoo_estimates.py and
# bbg_pe2y.py write these; the consensus panel filters on them. Refused as a
# `broker` value here so a hand-loaded row can never be mistaken for a feed.
MACHINE_BROKERS = {"consensus_yahoo", "bloomberg"}

# metric -> (label, which direction is a BEAT). None = no verdict.
METRICS: dict[str, tuple[str, str | None]] = {
    "revenue":               ("Revenue",            "higher"),
    "ebitda":                ("EBITDA",             "higher"),
    "ebit":                  ("EBIT",               "higher"),
    "pat":                   ("PAT",                "higher"),
    "eps":                   ("EPS",                "higher"),
    "ebitda_margin_pct":     ("EBITDA margin",      "higher"),
    "ebit_margin_pct":       ("EBIT margin",        "higher"),
    "gross_margin_pct":      ("Gross margin",       "higher"),
    "volume":                ("Volume",             "higher"),
    "realisation":           ("Realisation",        "higher"),
    "ebitda_per_t":          ("EBITDA/t",           "higher"),
    "cost_per_t":            ("Cost/t",             "lower"),
    "revenue_growth_cc_pct": ("Revenue growth (cc)", "higher"),
    "order_inflow":          ("Order inflow",       "higher"),
    "order_book":            ("Order book",         "higher"),
    "capex":                 ("Capex",              None),
    "net_debt":              ("Net debt",           "lower"),
}

# Display order inside a period: the P&L first, then the operating drivers.
METRIC_ORDER = list(METRICS)

_PERIOD = re.compile(r"^(?:Q([1-4])FY(\d{2})|FY(\d{2}))$")
# The digests write "1QFY27", "1QFY27E", "2QFY27F", "FY27E" — all the same
# period. Normalised on the way IN so the pivot has one key per quarter.
_ALT = re.compile(r"^([1-4])Q\s*FY(\d{2})[EF]?$", re.I)
_ALT_FY = re.compile(r"^FY(\d{2})[EF]$", re.I)


# ----------------------------------------------------------------- helpers

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_period(p) -> str | None:
    """'Q1FY27' | '1QFY27E' | 'FY27E' -> canonical; anything else -> None."""
    if not isinstance(p, str):
        return None
    s = p.strip().upper()
    if _PERIOD.match(s):
        return s
    m = _ALT.match(s)
    if m:
        return f"Q{m.group(1)}FY{m.group(2)}"
    m = _ALT_FY.match(s)
    if m:
        return f"FY{m.group(1)}"
    return None


def period_key(p: str) -> tuple[int, int]:
    """Sort key: (fiscal year, quarter), FY itself sorting AFTER its Q4 so a
    year's full-year row sits under its quarters."""
    m = _PERIOD.match(p)
    if not m:
        return (0, 0)
    if m.group(3):
        return (int(m.group(3)), 5)
    return (int(m.group(2)), int(m.group(1)))


def norm_unit(u) -> str:
    return re.sub(r"\s+", " ", str(u or "").strip()).lower()


def _is_date(s) -> bool:
    if not isinstance(s, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return False
    try:
        dt.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _connect(db=None) -> sqlite3.Connection:
    conn = sqlite3.connect(db or DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ----------------------------------------------------------------- loading

class Refusal(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def _validate(doc: dict, conn: sqlite3.Connection, today: dt.date) -> dict:
    """Check every row before anything is written. Returns the normalised doc
    or raises Refusal listing EVERY problem (not just the first — an author
    fixing a 20-row file should not need twenty round trips)."""
    problems: list[str] = []
    known_ent = {r[0] for r in conn.execute("SELECT id FROM entities")}
    known_src = {r[0] for r in conn.execute("SELECT id FROM sources")}

    srcs = list(doc.get("sources") or [])
    if doc.get("source"):
        srcs.append(doc["source"])
    default_src = doc["source"]["id"] if doc.get("source") else None
    for i, s in enumerate(srcs):
        for k in ("id", "kind", "source_date", "raw_path"):
            if not s.get(k):
                problems.append(f"sources[{i}]: missing {k!r}")
        if s.get("source_date") and not _is_date(s["source_date"]):
            problems.append(f"sources[{i}]: source_date {s['source_date']!r} "
                            f"is not an ISO date")
    file_src = {s["id"] for s in srcs if s.get("id")}

    def near(eid):
        c = difflib.get_close_matches(str(eid), sorted(known_ent), n=3, cutoff=0.5)
        return f" — did you mean {', '.join(c)}?" if c else ""

    # units seen in THIS file per (entity, period, metric), for the
    # within-file half of the unit guard
    units_seen: dict[tuple, dict[str, str]] = {}

    def common(row, where):
        eid = row.get("entity_id")
        if eid not in known_ent:
            problems.append(f"{where}: entity {eid!r} is not in `entities`"
                            f"{near(eid)} — a name is added to the roster by "
                            f"its spec or adapter, never guessed in here")
        per = norm_period(row.get("period"))
        if per is None:
            problems.append(f"{where}: period {row.get('period')!r} must be "
                            f"Q1-Q4FYyy or FYyy (1QFY27E and FY27E are accepted "
                            f"and normalised)")
        row["period"] = per
        met = row.get("metric")
        if met not in METRICS:
            problems.append(f"{where}: metric {met!r} not in the vocabulary "
                            f"{sorted(METRICS)} — add it to METRICS with a "
                            f"`better` direction rather than inventing a key")
        v = row.get("value_num")
        if not isinstance(v, (int, float)) or isinstance(v, bool) \
                or not math.isfinite(float(v)):
            problems.append(f"{where}: value_num {v!r} is not a finite number")
        if not str(row.get("quote") or "").strip():
            problems.append(f"{where}: quote is empty — no number without a "
                            f"citation (invariant 1)")
        sid = row.get("source_id") or default_src
        row["source_id"] = sid
        if not sid:
            problems.append(f"{where}: no source_id and the file has no "
                            f"default `source`")
        elif sid not in file_src and sid not in known_src:
            problems.append(f"{where}: source_id {sid!r} is neither in this "
                            f"file's `sources` nor already in the store")
        d = row.get("as_of")
        if not _is_date(d):
            problems.append(f"{where}: as_of {d!r} is not an ISO date")
        elif dt.date.fromisoformat(d) > today:
            problems.append(f"{where}: as_of {d} is in the future — a note "
                            f"cannot be dated after today (look-ahead guard)")
        if not str(row.get("unit") or "").strip():
            problems.append(f"{where}: unit is empty — state it (INR cr, "
                            f"INR/t, t, %, USD mn, INR/sh)")
        if eid in known_ent and per and met in METRICS:
            k = (eid, per, met)
            u = norm_unit(row.get("unit"))
            units_seen.setdefault(k, {})
            units_seen[k].setdefault(u, where)

    ests = list(doc.get("estimates") or [])
    for i, r in enumerate(ests):
        where = f"estimates[{i}]"
        common(r, where)
        b = str(r.get("broker") or "").strip()
        if not b:
            problems.append(f"{where}: broker is empty — no estimate without "
                            f"a named house (invariant 7)")
        elif b.lower() in MACHINE_BROKERS:
            problems.append(f"{where}: broker {b!r} is an adapter's name "
                            f"(yahoo_estimates / bbg_pe2y write it); a house "
                            f"estimate cannot borrow it")
        r["broker"] = b

    acts = list(doc.get("actuals") or [])
    for i, r in enumerate(acts):
        common(r, f"actuals[{i}]")

    cal = list(doc.get("calendar") or [])
    for i, r in enumerate(cal):
        where = f"calendar[{i}]"
        eid = r.get("entity_id")
        if eid not in known_ent:
            problems.append(f"{where}: entity {eid!r} is not in `entities`{near(eid)}")
        per = norm_period(r.get("period"))
        if per is None:
            problems.append(f"{where}: period {r.get('period')!r} malformed")
        r["period"] = per
        if not _is_date(r.get("event_date")):
            problems.append(f"{where}: event_date {r.get('event_date')!r} is "
                            f"not an ISO date")
        if r.get("status") not in ("expected", "confirmed", "reported"):
            problems.append(f"{where}: status {r.get('status')!r} must be "
                            f"expected|confirmed|reported")
        if not str(r.get("quote") or "").strip():
            problems.append(f"{where}: quote is empty")
        sid = r.get("source_id") or default_src
        r["source_id"] = sid
        if not sid or (sid not in file_src and sid not in known_src):
            problems.append(f"{where}: source_id {sid!r} unknown")

    # --- the unit guard, both halves -------------------------------------
    for (eid, per, met), seen in units_seen.items():
        if len(seen) > 1:
            problems.append(
                f"UNIT CONFLICT inside the file for {eid} {per} {met}: "
                + "; ".join(f"{u!r} at {w}" for u, w in seen.items())
                + " — convert to ONE unit before loading; keep the source's "
                  "figure in the quote")
        stored = {norm_unit(r[0]) for r in conn.execute(
            "SELECT unit FROM estimates WHERE entity_id=? AND period=? AND metric=? "
            "UNION SELECT unit FROM observations WHERE entity_id=? AND period=? "
            "AND metric=? AND factor='actual'",
            (eid, per, met, eid, per, met))} - {""}
        # Only the dominant unit of that cell matters; a stored row with an
        # empty unit is legacy and does not veto.
        clash = set(seen) - stored
        if stored and clash:
            problems.append(
                f"UNIT CONFLICT with the store for {eid} {per} {met}: file "
                f"says {sorted(clash)}, store holds {sorted(stored)} — state "
                f"the value in the stored unit")

    if not ests and not acts and not cal:
        problems.append("file carries no estimates, actuals or calendar rows")
    if problems:
        raise Refusal(problems)
    return {"sources": srcs, "estimates": ests, "actuals": acts, "calendar": cal}


def load(path: pathlib.Path, conn: sqlite3.Connection | None = None,
         today: dt.date | None = None) -> dict:
    """Load one staged file. All-or-nothing; idempotent on re-run."""
    own = conn is None
    conn = conn or _connect()
    today = today or dt.date.today()
    doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    d = _validate(doc, conn, today)
    ts = now()
    added = {"sources": 0, "estimates": 0, "actuals": 0, "calendar": 0,
             "skipped": 0, "file": str(path)}
    try:
        for s in d["sources"]:
            cur = conn.execute(
                "INSERT OR IGNORE INTO sources (id,kind,origin,title,source_date,"
                "captured_at,raw_path,meta) VALUES (?,?,?,?,?,?,?,?)",
                (s["id"], s["kind"], s.get("origin"), s.get("title"),
                 s["source_date"], ts, s["raw_path"],
                 json.dumps(s["meta"]) if s.get("meta") else None))
            added["sources"] += cur.rowcount
        for r in d["estimates"]:
            dup = conn.execute(
                "SELECT 1 FROM estimates WHERE source_id=? AND entity_id=? AND "
                "broker=? AND period=? AND metric=? AND quote=? LIMIT 1",
                (r["source_id"], r["entity_id"], r["broker"], r["period"],
                 r["metric"], r["quote"])).fetchone()
            if dup:
                added["skipped"] += 1
                continue
            conn.execute(
                "INSERT INTO estimates (source_id,entity_id,broker,as_of,period,"
                "metric,value_num,unit,quote,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["source_id"], r["entity_id"], r["broker"], r["as_of"],
                 r["period"], r["metric"], float(r["value_num"]),
                 r["unit"].strip(), r["quote"].strip(), ts))
            added["estimates"] += 1
        for r in d["actuals"]:
            # Same identity rule as load_observations: the verbatim quote.
            dup = conn.execute(
                "SELECT 1 FROM observations WHERE source_id=? AND entity_id=? "
                "AND factor='actual' AND metric=? AND IFNULL(period,'')=? "
                "AND quote=? LIMIT 1",
                (r["source_id"], r["entity_id"], r["metric"], r["period"],
                 r["quote"])).fetchone()
            if dup:
                added["skipped"] += 1
                continue
            conn.execute(
                "INSERT INTO observations (source_id,entity_id,as_of,factor,metric,"
                "value_num,value_text,unit,period,direction,confidence,quote,"
                "extractor_version,created_at) VALUES (?,?,?,'actual',?,?,NULL,?,?,"
                "NULL,?,?,?,?)",
                (r["source_id"], r["entity_id"], r["as_of"], r["metric"],
                 float(r["value_num"]), r["unit"].strip(), r["period"],
                 float(r.get("confidence", 0.9)), r["quote"].strip(),
                 EXTRACTOR, ts))
            added["actuals"] += 1
        for r in d["calendar"]:
            cur = conn.execute(
                "INSERT OR IGNORE INTO results_calendar (entity_id,period,"
                "event_date,status,source_id,quote,created_at) VALUES (?,?,?,?,?,?,?)",
                (r["entity_id"], r["period"], r["event_date"], r["status"],
                 r["source_id"], r["quote"].strip(), ts))
            added["calendar"] += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if own:
            conn.close()
    return added


def load_all(folder: pathlib.Path = STAGING) -> list[dict]:
    out = []
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not files:
        print(f"no staged files under {folder}")
        return out
    for f in files:
        try:
            a = load(f)
            out.append(a)
            print(f"  {f.name:<44} +{a['estimates']} est  +{a['actuals']} act  "
                  f"+{a['calendar']} cal  ({a['skipped']} already stored)")
        except Refusal as r:
            out.append({"file": str(f), "refused": r.problems})
            print(f"  {f.name:<44} REFUSED, nothing written:")
            for p in r.problems:
                print(f"      - {p}")
    return out


# ----------------------------------------------------------------- reading

def _median(xs):
    return statistics.median(xs) if xs else None


def periods_for(conn: sqlite3.Connection, entity_id: str,
                today: dt.date | None = None) -> list[dict]:
    """Every period with anything stored for one entity, newest first.

    Each period: status, calendar, and a metric list where each metric carries
    the broker rows (latest per broker used for consensus; older ones kept as
    `revisions`), the consensus over PRE-PRINT rows only, the actual, and the
    surprise with its verdict.
    """
    today = today or dt.date.today()
    ests = [dict(r) for r in conn.execute(
        "SELECT id, broker, as_of, period, metric, value_num, unit, quote, "
        "source_id FROM estimates WHERE entity_id=? AND broker NOT IN "
        "('consensus_yahoo','bloomberg') ORDER BY as_of, id", (entity_id,))]
    acts_raw = [dict(r) for r in conn.execute(
        "SELECT id, as_of, period, metric, value_num, unit, quote, source_id "
        "FROM observations WHERE entity_id=? AND factor='actual' AND period "
        "IS NOT NULL AND value_num IS NOT NULL ORDER BY as_of, id", (entity_id,))]
    # Dedupe exact re-loads (same source, period, metric, quote) — steel_actuals
    # was loaded three times before load_observations grew its identity check.
    seen, acts = set(), []
    for a in acts_raw:
        k = (a["source_id"], a["period"], a["metric"], a["quote"])
        if k in seen:
            continue
        seen.add(k)
        acts.append(a)
    yahoo = {}
    for r in conn.execute(
            "SELECT period, value_num, as_of FROM estimates WHERE entity_id=? AND "
            "broker='consensus_yahoo' AND metric='eps' ORDER BY as_of", (entity_id,)):
        yahoo[r["period"]] = {"eps": r["value_num"], "as_of": r["as_of"]}
    cal = {}
    for r in conn.execute(
            "SELECT period, event_date, status, quote, source_id FROM results_calendar "
            "WHERE entity_id=? ORDER BY created_at, id", (entity_id,)):
        cal[r["period"]] = dict(r)           # newest notice wins

    periods = sorted({e["period"] for e in ests} | {a["period"] for a in acts}
                     | set(cal) | {p for p in yahoo if p.startswith("FY")},
                     key=period_key, reverse=True)
    src_meta = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, origin, title, source_date, raw_path FROM sources WHERE id IN (%s)"
        % ",".join("?" * len({*[e["source_id"] for e in ests],
                               *[a["source_id"] for a in acts]}) or "''"),
        list({*[e["source_id"] for e in ests], *[a["source_id"] for a in acts]}))}

    out = []
    for per in periods:
        pe = [e for e in ests if e["period"] == per]
        pa = [a for a in acts if a["period"] == per]
        metrics = sorted({e["metric"] for e in pe} | {a["metric"] for a in pa},
                         key=lambda m: METRIC_ORDER.index(m) if m in METRIC_ORDER
                         else len(METRIC_ORDER))
        mrows = []
        for m in metrics:
            me = [e for e in pe if e["metric"] == m]
            ma = [a for a in pa if a["metric"] == m]
            label, better = METRICS.get(m, (m, None))
            # actual: the latest as_of; several DISTINCT values are a flag.
            actual = None
            act_flag = None
            if ma:
                ma_sorted = sorted(ma, key=lambda a: (a["as_of"], a["id"]))
                last = ma_sorted[-1]
                actual = {"value": last["value_num"], "unit": last["unit"],
                          "as_of": last["as_of"], "quote": last["quote"],
                          "source_id": last["source_id"],
                          "source": src_meta.get(last["source_id"], {})}
                distinct = sorted({round(a["value_num"], 6) for a in ma})
                if len(distinct) > 1:
                    act_flag = ("%d prints disagree: %s — latest shown, none "
                                "corrected" % (len(distinct),
                                              ", ".join(f"{v:g}" for v in distinct)))
            # estimates: latest per broker; pre-print only for consensus
            cutoff = actual["as_of"] if actual else None
            latest: dict[str, dict] = {}
            rows = []
            for e in me:
                post = bool(cutoff and e["as_of"] > cutoff)
                rows.append({"broker": e["broker"], "value": e["value_num"],
                             "unit": e["unit"], "as_of": e["as_of"],
                             "quote": e["quote"], "source_id": e["source_id"],
                             "source": src_meta.get(e["source_id"], {}),
                             "post_print": post})
                if not post:
                    prev = latest.get(e["broker"])
                    if prev is None or (e["as_of"], e["id"]) > (prev["as_of"], prev["id"]):
                        latest[e["broker"]] = e
            rows.sort(key=lambda r: (r["as_of"], r["broker"]), reverse=True)
            vals = [v["value_num"] for v in latest.values()]
            cons = None
            if vals:
                cons = {"median": _median(vals), "mean": sum(vals) / len(vals),
                        "low": min(vals), "high": max(vals), "n": len(vals),
                        "brokers": sorted(latest),
                        "as_of_first": min(v["as_of"] for v in latest.values()),
                        "as_of_last": max(v["as_of"] for v in latest.values())}
            surprise = None
            if actual and cons:
                is_pp = m.endswith("_pct") or norm_unit(actual["unit"]) in ("%", "pct", "pp")
                if is_pp:
                    diff = actual["value"] - cons["median"]
                    surprise = {"kind": "pp", "value": diff}
                elif cons["median"] != 0:
                    surprise = {"kind": "pct",
                                "value": (actual["value"] - cons["median"])
                                / abs(cons["median"]) * 100.0}
                if surprise is not None:
                    d = actual["value"] - cons["median"]
                    if better is None or abs(d) < 1e-12:
                        surprise["beat"] = None
                    else:
                        surprise["beat"] = (d > 0) if better == "higher" else (d < 0)
            unit = (actual or {}).get("unit") or (rows[0]["unit"] if rows else None)
            mrows.append({"metric": m, "label": label, "better": better,
                          "unit": unit, "estimates": rows, "consensus": cons,
                          "actual": actual, "actual_flag": act_flag,
                          "surprise": surprise,
                          "n_post_print": sum(1 for r in rows if r["post_print"])})
        has_act = any(r["actual"] for r in mrows)
        c = cal.get(per)
        if has_act:
            status = "reported"
        elif c and dt.date.fromisoformat(c["event_date"]) >= today:
            status = "scheduled"
        elif mrows:
            status = "estimates only"
        else:
            status = "calendar only"
        out.append({"period": per, "status": status,
                    "calendar": c, "metrics": mrows,
                    "yahoo_eps": yahoo.get(per),
                    "n_estimates": len(pe), "n_actuals": len(pa),
                    "brokers": sorted({e["broker"] for e in pe})})
    return out


def summary(conn: sqlite3.Connection, entity_ids: list[str]) -> dict[str, dict]:
    """Cheap per-entity counts for the roster picker: what is stored, the
    newest period, the next calendar date."""
    if not entity_ids:
        return {}
    q = ",".join("?" * len(entity_ids))
    out = {e: {"n_estimates": 0, "n_actuals": 0, "periods": set(),
               "brokers": set(), "next_event": None} for e in entity_ids}
    for r in conn.execute(
            f"SELECT entity_id, period, broker FROM estimates WHERE entity_id IN ({q}) "
            f"AND broker NOT IN ('consensus_yahoo','bloomberg')", entity_ids):
        o = out[r["entity_id"]]
        o["n_estimates"] += 1
        o["periods"].add(r["period"])
        o["brokers"].add(r["broker"])
    for r in conn.execute(
            f"SELECT entity_id, period FROM observations WHERE entity_id IN ({q}) "
            f"AND factor='actual' AND period IS NOT NULL", entity_ids):
        o = out[r["entity_id"]]
        o["n_actuals"] += 1
        o["periods"].add(r["period"])
    today = dt.date.today().isoformat()
    for r in conn.execute(
            f"SELECT entity_id, period, MIN(event_date) d FROM results_calendar "
            f"WHERE entity_id IN ({q}) AND event_date >= ? GROUP BY entity_id",
            [*entity_ids, today]):
        out[r["entity_id"]]["next_event"] = {"period": r["period"], "date": r["d"]}
    for e, o in out.items():
        ps = sorted(o["periods"], key=period_key)
        o["periods"] = ps
        o["latest_period"] = ps[-1] if ps else None
        o["brokers"] = sorted(o["brokers"])
        o["has_data"] = bool(ps) or o["next_event"] is not None
    return out


# ----------------------------------------------------------------- report

def report(entity_id: str) -> int:
    conn = _connect()
    ps = periods_for(conn, entity_id)
    if not ps:
        print(f"{entity_id}: nothing stored")
        return 0
    for p in ps:
        c = p["calendar"]
        print(f"\n{entity_id}  {p['period']}  [{p['status']}]"
              + (f"  print {c['event_date']} ({c['status']})" if c else ""))
        for m in p["metrics"]:
            cons = m["consensus"]
            a = m["actual"]
            s = m["surprise"]
            line = f"  {m['label']:<20} {m['unit'] or '':<10}"
            line += (f" cons {cons['median']:>12,.2f} (n={cons['n']}, "
                     f"{cons['low']:,.2f}..{cons['high']:,.2f})" if cons
                     else " cons            —           ")
            line += f"   actual {a['value']:>12,.2f}" if a else "   actual            —"
            if s:
                v = f"{s['value']:+.1f}{'pp' if s['kind'] == 'pp' else '%'}"
                verdict = {True: "BEAT", False: "MISS", None: "—"}[s.get("beat")]
                line += f"   {v:>8} {verdict}"
            if m["n_post_print"]:
                line += f"   ({m['n_post_print']} post-print est excluded)"
            if m["actual_flag"]:
                line += f"   !! {m['actual_flag']}"
            print(line)
            for r in m["estimates"]:
                print(f"      {r['broker']:<16} {r['value']:>12,.2f}  {r['as_of']}"
                      + ("  post-print" if r["post_print"] else "")
                      + f"  — {r['quote'][:70]}")
    return 0


# ----------------------------------------------------------------- selftest

def _fixture_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.executemany("INSERT INTO entities (id,kind,name,sector) VALUES (?,?,?,?)",
                     [("acme", "company", "Acme Steel", "steel"),
                      ("beta", "company", "Beta Cement", "cement")])
    return conn


def _write(tmp: pathlib.Path, name: str, doc: dict) -> pathlib.Path:
    p = tmp / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def selftest() -> int:
    import tempfile
    ok = 0
    T = dt.date(2026, 9, 22)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="results_selftest_"))
    SRC = [{"id": "t-preview", "kind": "broker_note", "origin": "digest",
            "title": "preview", "source_date": "2026-07-07", "raw_path": "x.md"},
           {"id": "t-print", "kind": "broker_note", "origin": "digest",
            "title": "result", "source_date": "2026-07-29", "raw_path": "y.md"},
           {"id": "t-post", "kind": "broker_note", "origin": "digest",
            "title": "post", "source_date": "2026-08-02", "raw_path": "z.md"}]

    def est(broker, metric, v, as_of, unit="INR cr", period="Q1FY27", sid="t-preview",
            quote=None):
        return {"entity_id": "acme", "broker": broker, "period": period,
                "metric": metric, "value_num": v, "unit": unit, "as_of": as_of,
                "source_id": sid, "quote": quote or f"{broker} {metric} {v}"}

    def act(metric, v, as_of="2026-07-29", unit="INR cr", period="Q1FY27",
            sid="t-print", quote=None):
        return {"entity_id": "acme", "period": period, "metric": metric,
                "value_num": v, "unit": unit, "as_of": as_of, "source_id": sid,
                "quote": quote or f"actual {metric} {v}"}

    # ------------------------------------------------------------ acceptance
    conn = _fixture_db()
    good = {"sources": SRC,
            "estimates": [
                est("Kotak", "ebitda", 100, "2026-07-05"),
                est("Kotak", "ebitda", 110, "2026-07-07",      # revision: latest wins
                    quote="Kotak revises to 110"),
                est("Nomura", "ebitda", 120, "2026-07-06"),
                est("Emkay", "ebitda", 130, "2026-07-08"),
                est("UBS", "ebitda", 500, "2026-08-02", sid="t-post",   # post-print
                    quote="UBS raises after the print"),
                est("Kotak", "cost_per_t", 500, "2026-07-07", unit="INR/t"),
                est("Kotak", "ebitda_margin_pct", 12.0, "2026-07-07", unit="%"),
                est("Kotak", "capex", 800, "2026-07-07"),
                est("Kotak", "ebitda", 90, "2026-07-07", period="2QFY27E",   # alt period
                    quote="2Q view"),
            ],
            "actuals": [act("ebitda", 121), act("cost_per_t", 480, unit="INR/t"),
                        act("ebitda_margin_pct", 11.5, unit="%"), act("capex", 700)],
            "calendar": [{"entity_id": "acme", "period": "Q2FY27",
                          "event_date": "2026-10-20", "status": "expected",
                          "source_id": "t-preview", "quote": "results calendar"}]}
    p = _write(tmp, "good.json", good)
    a = load(p, conn, T)
    assert (a["estimates"], a["actuals"], a["calendar"]) == (9, 4, 1), a
    ok += 1
    a2 = load(p, conn, T)
    assert a2["estimates"] == a2["actuals"] == a2["calendar"] == 0 and a2["skipped"] == 13, \
        f"re-load must add nothing: {a2}"
    ok += 1

    ps = periods_for(conn, "acme", T)
    byp = {x["period"]: x for x in ps}
    assert [x["period"] for x in ps] == ["Q2FY27", "Q1FY27"], [x["period"] for x in ps]
    ok += 1                                                     # newest first, alt normalised
    q1 = byp["Q1FY27"]
    assert q1["status"] == "reported"
    m = {r["metric"]: r for r in q1["metrics"]}
    e = m["ebitda"]
    # latest-per-broker: Kotak 110 (not 100), Nomura 120, Emkay 130; UBS post-print out
    assert e["consensus"]["n"] == 3 and e["consensus"]["median"] == 120, e["consensus"]
    assert e["consensus"]["low"] == 110, "Kotak's revision must replace its earlier number"
    assert e["n_post_print"] == 1 and any(r["post_print"] for r in e["estimates"])
    ok += 1
    assert abs(e["surprise"]["value"] - (121 - 120) / 120 * 100) < 1e-9 \
        and e["surprise"]["beat"] is True and e["surprise"]["kind"] == "pct"
    ok += 1
    c = m["cost_per_t"]
    assert c["surprise"]["value"] < 0 and c["surprise"]["beat"] is True, \
        "a LOWER cost/t than estimated is a beat"
    ok += 1
    mg = m["ebitda_margin_pct"]
    assert mg["surprise"]["kind"] == "pp" and abs(mg["surprise"]["value"] + 0.5) < 1e-9 \
        and mg["surprise"]["beat"] is False
    ok += 1
    assert m["capex"]["surprise"]["beat"] is None, "capex carries no verdict"
    ok += 1
    q2 = byp["Q2FY27"]
    assert q2["status"] == "scheduled" and q2["calendar"]["event_date"] == "2026-10-20"
    ok += 1
    # ordering: FY27 sorts after Q4FY27 and before Q1FY28
    assert period_key("Q4FY27") < period_key("FY27") < period_key("Q1FY28")
    assert norm_period("1QFY27E") == "Q1FY27" and norm_period("FY27E") == "FY27" \
        and norm_period("Q5FY27") is None and norm_period("H1FY27") is None
    ok += 1
    sm = summary(conn, ["acme", "beta"])
    assert sm["acme"]["has_data"] and not sm["beta"]["has_data"]
    assert sm["acme"]["next_event"] == {"period": "Q2FY27", "date": "2026-10-20"}
    ok += 1

    # the post-print guard the other way round: with NO actual, a late estimate
    # is an ordinary forecast and counts (acceptance side of the look-ahead rule)
    conn2 = _fixture_db()
    load(_write(tmp, "noact.json", {"sources": SRC, "estimates": [
        est("Kotak", "ebitda", 100, "2026-07-05"),
        est("UBS", "ebitda", 500, "2026-08-02", sid="t-post")]}), conn2, T)
    e2 = periods_for(conn2, "acme", T)[0]["metrics"][0]
    assert e2["consensus"]["n"] == 2 and e2["n_post_print"] == 0
    assert periods_for(conn2, "acme", T)[0]["status"] == "estimates only"
    ok += 1

    # conflicting actuals are FLAGGED, latest shown
    conn3 = _fixture_db()
    load(_write(tmp, "twoact.json", {"sources": SRC, "actuals": [
        act("ebitda", 121, as_of="2026-07-29"),
        act("ebitda", 125, as_of="2026-07-30", sid="t-post", quote="restated 125")]}),
        conn3, T)
    r3 = periods_for(conn3, "acme", T)[0]["metrics"][0]
    assert r3["actual"]["value"] == 125 and "2 prints disagree" in r3["actual_flag"]
    ok += 1

    # ------------------------------------------------------------ rejection
    def refused(name, doc, needle):
        nonlocal ok
        c = _fixture_db()
        before = c.execute("SELECT count(*) FROM estimates").fetchone()[0] \
            + c.execute("SELECT count(*) FROM observations").fetchone()[0] \
            + c.execute("SELECT count(*) FROM sources").fetchone()[0]
        try:
            load(_write(tmp, name, doc), c, T)
        except Refusal as r:
            joined = "\n".join(r.problems)
            assert needle in joined, f"{name}: expected {needle!r} in:\n{joined}"
            after = c.execute("SELECT count(*) FROM estimates").fetchone()[0] \
                + c.execute("SELECT count(*) FROM observations").fetchone()[0] \
                + c.execute("SELECT count(*) FROM sources").fetchone()[0]
            assert before == after, f"{name}: refused but wrote rows"
            ok += 1
            return
        raise AssertionError(f"{name}: should have been refused")

    base = lambda **k: {"sources": SRC, **k}                              # noqa: E731
    refused("unknown_entity.json", base(estimates=[
        dict(est("Kotak", "ebitda", 1, "2026-07-07"), entity_id="acmee")]), "did you mean acme")
    refused("bad_period.json", base(estimates=[
        est("Kotak", "ebitda", 1, "2026-07-07", period="Q5FY27")]), "period 'Q5FY27'")
    refused("bad_metric.json", base(estimates=[
        est("Kotak", "ebitdaa", 1, "2026-07-07")]), "not in the vocabulary")
    refused("empty_quote.json", base(estimates=[
        dict(est("Kotak", "ebitda", 1, "2026-07-07"), quote="  ")]), "quote is empty")
    refused("future.json", base(estimates=[
        est("Kotak", "ebitda", 1, "2026-09-23")]), "in the future")
    refused("machine_broker.json", base(estimates=[
        est("consensus_yahoo", "ebitda", 1, "2026-07-07")]), "adapter's name")
    refused("no_broker.json", base(estimates=[
        est("", "ebitda", 1, "2026-07-07")]), "broker is empty")
    refused("bad_source.json", base(estimates=[
        est("Kotak", "ebitda", 1, "2026-07-07", sid="nope")]), "neither in this file")
    refused("nan.json", base(estimates=[
        dict(est("Kotak", "ebitda", 1, "2026-07-07"), value_num="41.5bn")]),
        "not a finite number")
    # the unit guard, within the file ...
    refused("unit_file.json", base(estimates=[
        est("Kotak", "ebitda", 41.5, "2026-07-07", unit="INR bn"),
        est("Emkay", "ebitda", 4150, "2026-07-07", unit="INR cr")]),
        "UNIT CONFLICT inside the file")
    # ... and against the store: one bad row refuses the WHOLE file
    c4 = _fixture_db()
    load(_write(tmp, "seed.json", base(estimates=[
        est("Kotak", "ebitda", 4150, "2026-07-07", unit="INR cr")])), c4, T)
    try:
        load(_write(tmp, "unit_store.json", base(estimates=[
            est("Nomura", "ebitda", 4200, "2026-07-07", unit="INR cr"),      # fine
            est("Emkay", "ebitda", 41.5, "2026-07-07", unit="INR bn")])), c4, T)  # not
        raise AssertionError("store unit conflict not refused")
    except Refusal as r:
        assert "UNIT CONFLICT with the store" in "\n".join(r.problems)
        n = c4.execute("SELECT count(*) FROM estimates").fetchone()[0]
        assert n == 1, f"all-or-nothing violated: {n} rows after a refused file"
        ok += 1
    # a unit that merely differs in case/spacing is NOT a conflict (acceptance)
    load(_write(tmp, "unit_case.json", base(estimates=[
        est("Nomura", "ebitda", 4200, "2026-07-07", unit="inr  CR")])), c4, T)
    ok += 1
    refused("empty.json", base(), "no estimates, actuals or calendar")
    refused("cal_status.json", base(calendar=[
        {"entity_id": "acme", "period": "Q2FY27", "event_date": "2026-10-20",
         "status": "maybe", "source_id": "t-preview", "quote": "x"}]), "status 'maybe'")

    print(f"results_io selftest: {ok} checks passed")
    return 0


# ----------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--load", help="one staged JSON file")
    ap.add_argument("--load-all", action="store_true",
                    help=f"every *.json under {STAGING.relative_to(REPO)}")
    ap.add_argument("--report", metavar="ENTITY_ID")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.load:
        try:
            r = load(pathlib.Path(a.load))
        except Refusal as x:
            print("REFUSED — nothing written:")
            for p in x.problems:
                print(f"  - {p}")
            return 2
        print(f"loaded {a.load}: +{r['estimates']} estimates, +{r['actuals']} actuals, "
              f"+{r['calendar']} calendar rows, +{r['sources']} sources "
              f"({r['skipped']} already stored)")
        return 0
    if a.load_all:
        rs = load_all()
        return 2 if any("refused" in r for r in rs) else 0
    if a.report:
        return report(a.report)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
