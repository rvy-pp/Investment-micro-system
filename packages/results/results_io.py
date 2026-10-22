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
    "revenue_growth_cc_pct": ("Revenue QoQ (cc)",   "higher"),
    "order_inflow":          ("Order inflow",       "higher"),
    "order_book":            ("Order book",         "higher"),
    "capex":                 ("Capex",              None),
    "net_debt":              ("Net debt",           "lower"),
    # Preview-board lines (2026-10-10, preview_dashboard.py). Growths are %
    # and margin CHANGES are stored in pp (unit "pp"), so both grade on the
    # existing pp branch: a print of +1.9% QoQ against a street of +1.5% is
    # +0.4pp, which is what a growth surprise means.
    "revenue_usd":           ("Revenue (US$)",      "higher"),
    "revenue_usd_qoq_pct":   ("Revenue QoQ (US$)",  "higher"),
    "revenue_qoq_pct":       ("Revenue QoQ",        "higher"),
    "revenue_yoy_pct":       ("Revenue YoY",        "higher"),
    "ebitda_yoy_pct":        ("EBITDA YoY",         "higher"),
    "ebit_yoy_pct":          ("EBIT YoY",           "higher"),
    "ebitda_margin_qoq_pp":  ("EBITDA margin Δ QoQ", "higher"),
    "ebitda_margin_yoy_pp":  ("EBITDA margin Δ YoY", "higher"),
    "ebit_margin_qoq_pp":    ("EBIT margin Δ QoQ",  "higher"),
    "ebit_margin_yoy_pp":    ("EBIT margin Δ YoY",  "higher"),
    "pat_qoq_pct":           ("PAT QoQ",            "higher"),
    "pat_yoy_pct":           ("PAT YoY",            "higher"),
}

# Display order inside a period: the P&L first, then the operating drivers.
_FIRST = ["revenue", "revenue_usd", "revenue_growth_cc_pct", "revenue_usd_qoq_pct",
          "revenue_qoq_pct", "revenue_yoy_pct",
          "ebitda", "ebitda_yoy_pct", "ebitda_margin_pct", "ebitda_margin_qoq_pp",
          "ebitda_margin_yoy_pp", "ebit", "ebit_yoy_pct", "ebit_margin_pct",
          "ebit_margin_qoq_pp", "ebit_margin_yoy_pp",
          "pat", "pat_qoq_pct", "pat_yoy_pct", "eps"]
METRIC_ORDER = _FIRST + [m for m in METRICS if m not in _FIRST]

# The preview board's metric tabs (PM, 2026-10-10: "the ebitda margin, %, qoq
# growth and yoy growth should be one sub sub tab instead of 4"). ONE tab per
# P&L line, every way of reading that line side by side — the source
# dashboard had ten single-metric buttons. `lead` is the column the board
# ranks and charts on. "{m}" is the sector's margin line (ebit for IT).
BOARD_GROUPS = [
    {"id": "revenue", "label": "Revenue", "lead": "revenue_growth_cc_pct",
     "metrics": ["revenue_growth_cc_pct", "revenue_usd_qoq_pct", "revenue_usd",
                 "revenue_qoq_pct", "revenue_yoy_pct", "revenue"]},
    {"id": "margin", "label": "{M} & margin", "lead": "{m}_margin_pct",
     "metrics": ["{m}_margin_pct", "{m}_margin_qoq_pp", "{m}_margin_yoy_pp",
                 "{m}", "{m}_yoy_pct"]},
    {"id": "pat", "label": "PAT", "lead": "pat_yoy_pct",
     "metrics": ["pat", "pat_qoq_pct", "pat_yoy_pct"]},
]

# The "in line" band, used for every verdict that needs one: the print vs the
# street, an analyst's number vs the street (its implied stance), and a stance
# graded at the print. ONE copy, sent in the payload — app.html reads it from
# there rather than carrying a twin constant that can drift.
INLINE_PCT, INLINE_PP = 1.0, 0.2

# ---------------------------------------------------------- analyst views
# PM, 2026-10-06: "capitalize on the view of the analyst of the firm. Since I
# talk with a lot of analysts who make previews, can we incorporate their
# view and buildup before actual previews, or change if something changes
# beforehand?" — then: "make the analyst and preview analysis in the results
# tab".
#
# A view is the PM's dated note of a CONVERSATION, not a published estimate,
# so it lives in its own table and never in `estimates`: the street median the
# print is graded against must stay what the houses actually published.
# Append-only — a changed view is a new row, a withdrawn one is a row with
# withdrawn=1; the latest per (firm, analyst, entity, period, metric) is live.
# A view carries a NUMBER (value + unit) or a STANCE vs the street, or both;
# the note is required (no number without provenance — the cousin of
# invariants 1 and 2, as for Kelly edges).
VIEWS_DDL = """
CREATE TABLE IF NOT EXISTS results_analyst_views (
    id           INTEGER PRIMARY KEY,
    entity_id    TEXT NOT NULL REFERENCES entities (id),
    period       TEXT NOT NULL,
    metric       TEXT NOT NULL,
    firm         TEXT NOT NULL CHECK (length(trim(firm)) > 0),
    analyst      TEXT NOT NULL CHECK (length(trim(analyst)) > 0),
    said_on      TEXT NOT NULL CHECK (said_on GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    value_num    REAL,
    unit         TEXT,
    stance       TEXT CHECK (stance IS NULL OR stance IN ('above','inline','below')),
    withdrawn    INTEGER NOT NULL DEFAULT 0 CHECK (withdrawn IN (0, 1)),
    note         TEXT NOT NULL CHECK (length(trim(note)) > 0),
    entered_via  TEXT NOT NULL,
    entered_at   TEXT NOT NULL,
    CHECK (withdrawn = 1 OR value_num IS NOT NULL OR stance IS NOT NULL),
    CHECK (value_num IS NULL OR length(trim(IFNULL(unit, ''))) > 0)
) STRICT;
CREATE INDEX IF NOT EXISTS ix_resview ON results_analyst_views
    (entity_id, period, metric);
"""
STANCES = ("above", "inline", "below")

# "Followed through": the house's next published number lands within this of
# what the analyst told you. Same scale as the in-line band, doubled — a
# published figure is a rounded model output, a conversation is rougher.
FOLLOW_PCT, FOLLOW_PP = 2.0, 0.4

# Firm names are typed by hand on two different paths (the staged previews'
# `broker`, the PM's view form). "Kotak" and "Kotak Institutional Equities"
# and "KIE" are one house; matching them by raw string would show every view
# as "never published" — plausible and wrong. Normalised, never guessed:
# an alias is listed here, by name, when it is met.
_FIRM_SUFFIX = re.compile(r"(institutionalequities|institutional|securities|"
                          r"capital|research|equities|financial|limited|ltd|"
                          r"india|global)$")
FIRM_ALIASES = {"kie": "kotak", "kotaksecurities": "kotak",
                "jmfinancial": "jm", "jmfl": "jm", "ms": "morganstanley",
                "gs": "goldmansachs", "jpm": "jpmorgan", "jpmorganchase": "jpmorgan",
                "iifl": "iifl", "mosl": "motilaloswal", "mofsl": "motilaloswal"}


def firm_key(name) -> str:
    s = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    if s in FIRM_ALIASES:
        return FIRM_ALIASES[s]
    for _ in range(3):                      # "kotak securities india ltd"
        t = _FIRM_SUFFIX.sub("", s)
        if t == s or not t:
            break
        s = t
    return FIRM_ALIASES.get(s, s)

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
    conn.executescript(VIEWS_DDL)            # the views table owns its DDL here
    return conn


def period_of(d: dt.date) -> str:
    """The Indian fiscal quarter CONTAINING a date: Apr-Jun Q1 ... Jan-Mar Q4,
    the year labelled by its March close."""
    q = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3}.get(d.month, 4)
    fy = d.year + 1 if d.month >= 4 else d.year
    return f"Q{q}FY{fy % 100:02d}"


def season_periods(today: dt.date) -> list[str]:
    """[last ENDED quarter, the one in progress] — what an analyst is
    previewing in results season, and the one after it. On 2026-10-06 that is
    Q2FY27 (Jul-Sep, reports in Oct-Nov) then Q3FY27."""
    cur = period_of(today)
    prev = period_of(today - dt.timedelta(days=92))
    return [prev, cur] if prev != cur else [cur]


def _diff(value, base, is_pp: bool):
    """value vs base as (kind, number): pp for a margin, % otherwise."""
    if value is None or base is None:
        return None
    if is_pp:
        return {"kind": "pp", "value": value - base}
    if base == 0:
        return None
    return {"kind": "pct", "value": (value - base) / abs(base) * 100.0}


def _stance_of(d) -> str | None:
    """A diff -> above / inline / below, on the INLINE band."""
    if d is None:
        return None
    band = INLINE_PP if d["kind"] == "pp" else INLINE_PCT
    if abs(d["value"]) < band:
        return "inline"
    return "above" if d["value"] > 0 else "below"


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


# ----------------------------------------------------------- analyst views

def save_view(conn: sqlite3.Connection, d: dict, today: dt.date | None = None,
              via: str = "page") -> dict:
    """Validate and append ONE analyst view. Returns {ok, id, ...} or {error}.

    Refuses rather than defaults, every problem listed at once. The same unit
    guard as a staged file: a view in INR bn against a stored INR cr preview
    would make "vs street" read 1% for a number 100x out.
    `d['withdraw']` true appends a withdrawal of the live view for that key
    (the note says why); it needs a live view to withdraw.
    """
    today = today or dt.date.today()
    p: list[str] = []
    eid = str(d.get("entity_id") or "").strip()
    if not conn.execute("SELECT 1 FROM entities WHERE id=?", (eid,)).fetchone():
        p.append(f"entity {eid!r} is not in `entities`")
    per = norm_period(d.get("period"))
    if per is None:
        p.append(f"period {d.get('period')!r} must be Q1-Q4FYyy or FYyy")
    met = d.get("metric")
    if met not in METRICS:
        p.append(f"metric {met!r} is not in the vocabulary")
    firm = str(d.get("firm") or "").strip()
    analyst = str(d.get("analyst") or "").strip()
    if not firm:
        p.append("firm is empty — a view needs the house it came from")
    elif firm.lower() in MACHINE_BROKERS:
        p.append(f"firm {firm!r} is an adapter's name, not a house")
    if not analyst:
        p.append("analyst is empty — name who said it")
    said = str(d.get("said_on") or today.isoformat()).strip()
    if not _is_date(said):
        p.append(f"said_on {said!r} is not an ISO date")
    elif dt.date.fromisoformat(said) > today:
        p.append(f"said_on {said} is in the future")
    note = str(d.get("note") or "").strip()
    if not note:
        p.append("note is empty — say what was said and why it matters")
    withdraw = bool(d.get("withdraw"))
    val, unit, stance = d.get("value_num"), str(d.get("unit") or "").strip(), d.get("stance")
    if val in ("", None):
        val = None
    else:
        try:
            val = float(val)
            if not math.isfinite(val):
                raise ValueError
        except (TypeError, ValueError):
            p.append(f"value {d.get('value_num')!r} is not a number")
            val = None
    stance = (str(stance).strip().lower() or None) if stance not in (None, "") else None
    if stance is not None and stance not in STANCES:
        p.append(f"stance {stance!r} must be one of {STANCES}")
    if withdraw:
        val, unit, stance = None, "", None
    else:
        if val is None and stance is None:
            p.append("give a number or a stance vs the street (above / in line / below)")
        if val is not None and not unit:
            p.append("a number needs its unit (INR cr, INR/t, %, t, USD mn, INR/sh)")
    if not p and val is not None:
        stored = {norm_unit(r[0]) for r in conn.execute(
            "SELECT unit FROM estimates WHERE entity_id=? AND period=? AND metric=? "
            "AND broker NOT IN ('consensus_yahoo','bloomberg') "
            "UNION SELECT unit FROM observations WHERE entity_id=? AND period=? "
            "AND metric=? AND factor='actual' "
            "UNION SELECT unit FROM results_analyst_views WHERE entity_id=? AND "
            "period=? AND metric=? AND value_num IS NOT NULL",
            (eid, per, met) * 3)} - {""}
        if stored and norm_unit(unit) not in stored:
            p.append(f"unit {unit!r} disagrees with what is stored for {eid} "
                     f"{per} {met}: {sorted(stored)} — state it in that unit")
    if not p and withdraw:
        live = _live_views(conn, eid, per, met)
        if (firm_key(firm), analyst.lower()) not in live:
            p.append(f"no live view from {analyst} ({firm}) on {eid} {per} "
                     f"{METRICS[met][0]} to withdraw")
    if p:
        return {"error": "; ".join(p), "problems": p}
    ts = now()
    cur = conn.execute(
        "INSERT INTO results_analyst_views (entity_id,period,metric,firm,analyst,"
        "said_on,value_num,unit,stance,withdrawn,note,entered_via,entered_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, per, met, firm, analyst, said, val, unit or None, stance,
         1 if withdraw else 0, note, via, ts))
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "entity_id": eid, "period": per,
            "metric": met, "withdrawn": withdraw, "entered_at": ts}


def _views(conn, entity_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM results_analyst_views WHERE entity_id=? "
        "ORDER BY said_on, id", (entity_id,))]


def _latest_views(rows: list[dict]) -> dict:
    """{(firm_key, analyst_lower): latest row} — withdrawn rows included, so
    the caller can tell a withdrawn view from one never given."""
    out = {}
    for r in sorted(rows, key=lambda r: (r["said_on"], r["id"])):
        out[(firm_key(r["firm"]), r["analyst"].strip().lower())] = r
    return out


def _live_views(conn, eid, per, met) -> dict:
    rows = [r for r in _views(conn, eid) if r["period"] == per and r["metric"] == met]
    return {k: r for k, r in _latest_views(rows).items() if not r["withdrawn"]}


def _view_block(vrows: list[dict], ests: list[dict], cons, actual,
                is_pp: bool) -> dict | None:
    """Everything the page says about analyst views on ONE metric of ONE
    period: each live view against the street, against what the house then
    PUBLISHED, and against the print; plus the median of the numbers (the
    'whisper') and the history of superseded / withdrawn rows.
    """
    if not vrows:
        return None
    cutoff = actual["as_of"] if actual else None
    street = cons["median"] if cons else None
    latest = _latest_views(vrows)
    live, history = [], []
    for r in sorted(vrows, key=lambda r: (r["said_on"], r["id"]), reverse=True):
        k = (firm_key(r["firm"]), r["analyst"].strip().lower())
        is_live = latest.get(k) is r and not r["withdrawn"]
        if not is_live:
            history.append({"firm": r["firm"], "analyst": r["analyst"],
                            "said_on": r["said_on"], "value": r["value_num"],
                            "unit": r["unit"], "stance": r["stance"],
                            "withdrawn": bool(r["withdrawn"]), "note": r["note"],
                            "superseded": latest.get(k) is not r})
            continue
        post = bool(cutoff and r["said_on"] > cutoff)
        vs = _diff(r["value_num"], street, is_pp)
        implied = _stance_of(vs)
        stance = r["stance"] or implied
        # what the same house PUBLISHED after the conversation (pre-print)
        pub = sorted([e for e in ests
                      if firm_key(e["broker"]) == k[0] and e["as_of"] >= r["said_on"]
                      and not (cutoff and e["as_of"] > cutoff)],
                     key=lambda e: (e["as_of"], e["id"]))
        published = None
        if pub:
            e = pub[0]
            published = {"value": e["value_num"], "as_of": e["as_of"],
                         "broker": e["broker"], "quote": e["quote"],
                         "lead_days": (dt.date.fromisoformat(e["as_of"])
                                       - dt.date.fromisoformat(r["said_on"])).days}
            if r["value_num"] is not None:
                g = _diff(e["value_num"], r["value_num"], is_pp)
                published["vs_view"] = g
                tol = FOLLOW_PP if is_pp else FOLLOW_PCT
                published["followed"] = (g is not None and abs(g["value"]) <= tol)
            elif r["stance"] and street is not None:
                published["followed"] = (
                    _stance_of(_diff(e["value_num"], street, is_pp)) == r["stance"])
        graded = None
        if actual and not post:
            a = actual["value"]
            graded = {}
            if r["value_num"] is not None:
                ev, es = _diff(r["value_num"], a, is_pp), _diff(street, a, is_pp)
                graded["err"] = ev
                graded["street_err"] = es
                if ev is not None and es is not None:
                    graded["closer"] = abs(ev["value"]) < abs(es["value"])
            if stance and street is not None:
                truth = _stance_of(_diff(a, street, is_pp))
                graded["actual_stance"] = truth
                graded["stance_hit"] = (truth == stance)
        live.append({"id": r["id"], "firm": r["firm"], "analyst": r["analyst"],
                     "said_on": r["said_on"], "value": r["value_num"],
                     "unit": r["unit"], "stance": r["stance"],
                     "implied_stance": implied, "vs_street": vs,
                     "note": r["note"], "via": r["entered_via"],
                     "post_print": post, "published": published,
                     "graded": graded})
    nums = [v["value"] for v in live if v["value"] is not None and not v["post_print"]]
    whisper = None
    if nums:
        med = _median(nums)
        whisper = {"median": med, "n": len(nums), "low": min(nums),
                   "high": max(nums), "vs_street": _diff(med, street, is_pp)}
    tally = {s: sum(1 for v in live if not v["post_print"]
                    and (v["stance"] or v["implied_stance"]) == s) for s in STANCES}
    return {"live": live, "history": history, "whisper": whisper,
            "stances": tally, "n_live": len(live)}


def analyst_scorecard(conn: sqlite3.Connection) -> list[dict]:
    """Per (firm, analyst), across every name and period: how often their
    number beat the street at the print, how often the stance was right, and
    whether the house published what was said. Recomputed on read from the
    views and the prints — there is no scorecard table to go stale."""
    ents = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM results_analyst_views")]
    acc: dict[tuple, dict] = {}
    for eid in ents:
        for per in periods_for(conn, eid):
            for m in per["metrics"]:
                vb = m.get("views")
                if not vb:
                    continue
                for v in vb["live"]:
                    k = (firm_key(v["firm"]), v["analyst"].strip().lower())
                    a = acc.setdefault(k, {"firm": v["firm"], "analyst": v["analyst"],
                                           "n_views": 0, "n_graded": 0, "n_closer": 0,
                                           "abs_err": [], "street_abs_err": [],
                                           "n_stance": 0, "n_stance_hit": 0,
                                           "n_published": 0, "n_followed": 0,
                                           "lead_days": [], "names": set(),
                                           "last_said": v["said_on"]})
                    a["n_views"] += 1
                    a["names"].add(eid)
                    a["last_said"] = max(a["last_said"], v["said_on"])
                    g = v.get("graded") or {}
                    if "closer" in g:
                        a["n_graded"] += 1
                        a["n_closer"] += bool(g["closer"])
                        # the error AVERAGE is % only: a margin's pp error and a
                        # money line's % error are different scales, and their
                        # mean is a number of nothing. "closer" counts both.
                        if g["err"]["kind"] == "pct":
                            a["abs_err"].append(abs(g["err"]["value"]))
                            a["street_abs_err"].append(abs(g["street_err"]["value"]))
                    if "stance_hit" in g:
                        a["n_stance"] += 1
                        a["n_stance_hit"] += bool(g["stance_hit"])
                    pb = v.get("published")
                    if pb:
                        a["n_published"] += 1
                        a["n_followed"] += bool(pb.get("followed"))
                        a["lead_days"].append(pb["lead_days"])
    out = []
    for a in acc.values():
        out.append({**{k: a[k] for k in ("firm", "analyst", "n_views", "n_graded",
                                         "n_closer", "n_stance", "n_stance_hit",
                                         "n_published", "n_followed", "last_said")},
                    "names": sorted(a["names"]),
                    "mean_abs_err": (sum(a["abs_err"]) / len(a["abs_err"])
                                     if a["abs_err"] else None),
                    "street_mean_abs_err": (sum(a["street_abs_err"]) / len(a["street_abs_err"])
                                            if a["street_abs_err"] else None),
                    "median_lead_days": _median(a["lead_days"])})
    out.sort(key=lambda x: (-x["n_graded"], -x["n_views"], x["firm"].lower()))
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
    views = _views(conn, entity_id)
    cal = {}
    for r in conn.execute(
            "SELECT period, event_date, status, quote, source_id FROM results_calendar "
            "WHERE entity_id=? ORDER BY created_at, id", (entity_id,)):
        cal[r["period"]] = dict(r)           # newest notice wins

    periods = sorted({e["period"] for e in ests} | {a["period"] for a in acts}
                     | set(cal) | {p for p in yahoo if p.startswith("FY")}
                     | {v["period"] for v in views},
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
        pv = [v for v in views if v["period"] == per]
        metrics = sorted({e["metric"] for e in pe} | {a["metric"] for a in pa}
                         | {v["metric"] for v in pv},
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
            mv = [v for v in pv if v["metric"] == m]
            unit = ((actual or {}).get("unit") or (rows[0]["unit"] if rows else None)
                    or next((v["unit"] for v in mv if v["unit"]), None))
            is_pp_m = m.endswith("_pct") or norm_unit(unit) in ("%", "pct", "pp")
            vblock = _view_block(mv, me, cons, actual, is_pp_m)
            mrows.append({"views": vblock,"metric": m, "label": label, "better": better,
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
        elif any(r["consensus"] for r in mrows):
            status = "estimates only"
        elif mrows:
            status = "analyst views only"
        elif c:
            status = "calendar only"
        else:
            # only Yahoo's machine consensus. Reachable for every book leg
            # since 2026-10-06 (the capture widened); labelling it "calendar
            # only" claimed a result date nobody had loaded.
            status = "consensus only"
        out.append({"period": per, "status": status,
                    "calendar": c, "metrics": mrows,
                    "yahoo_eps": yahoo.get(per),
                    "n_estimates": len(pe), "n_actuals": len(pa),
                    "n_views": sum((r["views"] or {}).get("n_live", 0) for r in mrows),
                    "brokers": sorted({e["broker"] for e in pe})})
    return out


def sector_board(conn: sqlite3.Connection, names: list[dict],
                 period: str | None = None, today: dt.date | None = None) -> dict:
    """The preview board: one quarter, one sector, every house side by side.

    `names` is the sector's roster rows ({id, name, nse}). Per (house, name,
    metric) the house's LATEST PRE-PRINT estimate for the quarter; per
    (name, metric) the print, and the street median the same way periods_for
    builds it — so the board and the per-company panel cannot disagree about
    a consensus. Basis (consol / standalone / segment) and each house's
    per-name note come from the source's `meta` (preview_dashboard.py writes
    them there).

    The period defaults to the newest SEASON quarter holding a house estimate
    for this sector, else the newest any; a board for a quarter with nothing
    in it is returned empty with the reason, never filled from another one.
    """
    today = today or dt.date.today()
    ids = [n["id"] for n in names]
    empty = {"period": None, "periods": [], "houses": [], "rows": [], "groups": [],
             "metrics": {}, "margin_type": None}
    if not ids:
        return empty
    q = ",".join("?" * len(ids))
    est = [dict(r) for r in conn.execute(
        f"SELECT id, source_id, entity_id, broker, as_of, period, metric, value_num, "
        f"unit, quote FROM estimates WHERE entity_id IN ({q}) AND broker NOT IN "
        f"('consensus_yahoo','bloomberg')", ids)]
    pers = sorted({e["period"] for e in est if not e["period"].startswith("FY")},
                  key=period_key, reverse=True)
    if not pers:
        return empty
    season = [p for p in season_periods(today) if p in pers]
    per = period if period in pers else (season[0] if season else pers[0])
    est = [e for e in est if e["period"] == per]
    acts: dict[tuple, dict] = {}
    for r in conn.execute(
            f"SELECT entity_id, metric, value_num, unit, as_of, quote FROM observations "
            f"WHERE entity_id IN ({q}) AND factor='actual' AND period=? AND value_num "
            f"IS NOT NULL ORDER BY as_of, id", [*ids, per]):
        acts[(r["entity_id"], r["metric"])] = dict(r)      # latest wins
    srcs = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, kind, origin, title, source_date, meta FROM sources WHERE id IN (%s)"
        % (",".join("?" * len({e["source_id"] for e in est})) or "''"),
        list({e["source_id"] for e in est}))}
    for s in srcs.values():
        try:
            s["meta"] = json.loads(s["meta"]) if s["meta"] else {}
        except ValueError:
            s["meta"] = {}

    # latest pre-print estimate per (house, entity, metric)
    latest: dict[tuple, dict] = {}
    for e in est:
        a = acts.get((e["entity_id"], e["metric"]))
        if a and e["as_of"] > a["as_of"]:
            continue                       # post-print: not a forecast
        k = (e["broker"], e["entity_id"], e["metric"])
        if k not in latest or (e["as_of"], e["id"]) > (latest[k]["as_of"], latest[k]["id"]):
            latest[k] = e
    houses: dict[str, dict] = {}
    for (b, eid, m), e in latest.items():
        h = houses.setdefault(b, {"house": b, "as_of": e["as_of"], "sources": {},
                                  "names": set(), "n": 0})
        h["as_of"] = max(h["as_of"], e["as_of"])
        h["names"].add(eid)
        h["n"] += 1
        s = srcs.get(e["source_id"]) or {}
        h["sources"][e["source_id"]] = {"title": s.get("title"), "date": s.get("source_date"),
                                        "email": s.get("kind") == "broker_email",
                                        "notes": (s.get("meta") or {}).get("block_notes", "")}
    mts = {(srcs.get(e["source_id"]) or {}).get("meta", {}).get("margin_type") for e in est} - {None}
    mt = "EBIT" if "EBIT" in mts or any(e["metric"].startswith("ebit_") for e in est) else "EBITDA"
    m = mt.lower()

    rows = []
    for n in names:
        eid = n["id"]
        cells: dict[str, dict] = {}
        basis: dict[str, dict] = {}
        notes: dict[str, str] = {}
        dates: dict[str, str] = {}
        for (b, e2, met), e in latest.items():
            if e2 != eid:
                continue
            cells.setdefault(b, {})[met] = e["value_num"]
            dates[b] = max(dates.get(b, ""), e["as_of"])
            meta = (srcs.get(e["source_id"]) or {}).get("meta") or {}
            if eid in (meta.get("basis") or {}):
                basis[b] = meta["basis"][eid]
            if (meta.get("notes") or {}).get(eid):
                notes[b] = meta["notes"][eid]
        if not cells:
            continue
        mets = sorted({k for c in cells.values() for k in c})
        street, actual, surprise = {}, {}, {}
        for met in mets:
            vals = [c[met] for c in cells.values() if met in c]
            street[met] = {"median": _median(vals), "low": min(vals), "high": max(vals),
                           "n": len(vals)}
            a = acts.get((eid, met))
            if a:
                actual[met] = a["value_num"]
                is_pp = met.endswith(("_pct", "_pp")) or norm_unit(a["unit"]) in ("%", "pp")
                surprise[met] = _diff(a["value_num"], street[met]["median"], is_pp)
        bset = {v.get("basis") for v in basis.values()}
        rows.append({"id": eid, "name": n.get("name") or eid, "nse": n.get("nse"),
                     "cells": cells, "street": street, "actual": actual,
                     "surprise": surprise, "basis": basis, "notes": notes,
                     "dates": dates,
                     "mixed_basis": len(bset) > 1})
    used = {met for r in rows for met in r["street"]}
    other = "ebitda" if m == "ebit" else "ebit"
    groups = []
    for g in BOARD_GROUPS:
        ms = [x.format(m=m) for x in g["metrics"]]
        if g["id"] == "margin":             # a house that printed the OTHER margin line
            ms += [f"{other}_margin_pct", other, f"{other}_yoy_pct"]
        ms = [x for x in ms if x in used]
        if ms:
            lead = g["lead"].format(m=m)
            groups.append({"id": g["id"], "label": g["label"].format(M=mt),
                           "metrics": ms, "lead": lead if lead in ms else ms[0]})
    # anything the three P&L tabs do not hold (EBITDA/t, volume, order book)
    # gets one more tab rather than vanishing from the board
    rest = [x for x in METRIC_ORDER if x in used
            and not any(x in g["metrics"] for g in groups)]
    if rest:
        groups.append({"id": "other", "label": "Operating lines", "metrics": rest,
                       "lead": rest[0]})
    units = {}
    for e in est:
        units.setdefault(e["metric"], e["unit"])
    hs = sorted(houses.values(), key=lambda h: (-len(h["names"]), h["house"].lower()))
    for h in hs:
        h["names"] = len(h["names"])
        h["sources"] = list(h["sources"].values())
    return {"period": per, "periods": pers, "houses": hs, "rows": rows,
            "groups": groups, "margin_type": mt,
            "n_prints": sum(1 for r in rows if r["actual"]),
            "metrics": {k: {"label": METRICS.get(k, (k, None))[0],
                            "better": METRICS.get(k, (k, None))[1],
                            "unit": units.get(k)} for k in sorted(used)}}


def summary(conn: sqlite3.Connection, entity_ids: list[str],
            today: str | None = None) -> dict[str, dict]:
    """Cheap per-entity counts for the roster picker: what is stored, the
    newest period, the next calendar date. `today` exists for the selftest:
    its fixture dates a print 2026-10-20, and against the real clock the
    "next event" check began failing on 2026-10-21 — the code was right,
    the test had a calendar in it."""
    if not entity_ids:
        return {}
    q = ",".join("?" * len(entity_ids))
    out = {e: {"n_estimates": 0, "n_actuals": 0, "n_views": 0, "periods": set(),
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
    for r in conn.execute(
            f"SELECT entity_id, period FROM results_analyst_views WHERE entity_id "
            f"IN ({q}) AND withdrawn=0", entity_ids):
        o = out[r["entity_id"]]
        o["n_views"] += 1
        o["periods"].add(r["period"])
    today = today or dt.date.today().isoformat()
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
    conn.executescript(VIEWS_DDL)
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
    sm = summary(conn, ["acme", "beta"], today="2026-10-01")   # pinned: see summary()
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

    ok += _selftest_views(tmp, SRC, est, act, T)
    print(f"results_io selftest: {ok} checks passed")
    return 0


def _selftest_views(tmp, SRC, est, act, T) -> int:
    """Analyst views: acceptance beside rejection (the GLOB lesson)."""
    ok = 0
    # firm matching — the alias table and the suffix strip, both directions
    assert firm_key("Kotak Institutional Equities") == firm_key("Kotak")         == firm_key("KIE") == firm_key("kotak securities ltd") == "kotak"
    assert firm_key("JM Financial") == "jm" and firm_key("Nomura") != firm_key("Kotak")
    ok += 1
    assert period_of(dt.date(2026, 10, 6)) == "Q3FY27"         and period_of(dt.date(2026, 3, 31)) == "Q4FY26"         and period_of(dt.date(2026, 4, 1)) == "Q1FY27"
    assert season_periods(dt.date(2026, 10, 6)) == ["Q2FY27", "Q3FY27"]
    ok += 1

    conn = _fixture_db()
    load(_write(tmp, "v_seed.json", {"sources": SRC,
        "estimates": [est("Kotak", "ebitda", 118, "2026-07-07"),
                      est("Nomura", "ebitda", 120, "2026-07-06"),
                      est("Emkay", "ebitda", 130, "2026-07-08")],
        "actuals": [act("ebitda", 126)]}), conn, T)

    def sv(**k):
        base = {"entity_id": "acme", "period": "Q1FY27", "metric": "ebitda",
                "unit": "INR cr", "note": "call note"}
        return save_view(conn, {**base, **k}, T)

    # acceptance
    assert sv(firm="Kotak", analyst="Ravi", said_on="2026-06-25", value_num=105)["ok"]
    assert sv(firm="Kotak Institutional Equities", analyst="ravi",   # same person, revised
              said_on="2026-07-01", value_num=117)["ok"]
    assert sv(firm="Nomura", analyst="Lee", said_on="2026-07-02", stance="above",
              unit="")["ok"]
    assert sv(firm="UBS", analyst="Kim", said_on="2026-07-03", value_num=140)["ok"]
    assert sv(firm="UBS", analyst="Kim", said_on="2026-07-04", withdraw=True,
              note="Kim pulled the number after the channel check")["ok"]
    assert sv(firm="Emkay", analyst="Raj", said_on="2026-07-30", value_num=131)["ok"]
    assert sv(firm="Kotak", analyst="Ravi", period="2QFY27E", said_on="2026-09-01",
              value_num=150)["ok"]
    ok += 1

    ps = {x["period"]: x for x in periods_for(conn, "acme", T)}
    vb = ps["Q1FY27"]["metrics"][0]["views"]
    live = {(v["firm"], v["analyst"]): v for v in vb["live"]}
    assert set(live) == {("Kotak Institutional Equities", "ravi"), ("Nomura", "Lee"),
                         ("Emkay", "Raj")}, set(live)
    ok += 1                                    # revision replaces, withdrawal removes
    assert len(vb["history"]) == 3 and sum(h["withdrawn"] for h in vb["history"]) == 1
    ok += 1
    r = live[("Kotak Institutional Equities", "ravi")]
    # street = median(118, 120, 130) = 120; the view 117 is 2.5% below it
    assert abs(r["vs_street"]["value"] + 2.5) < 1e-9 and r["implied_stance"] == "below"
    ok += 1
    # the house PUBLISHED 118 six days later, 0.85% from what was said: followed
    pb = r["published"]
    assert pb["value"] == 118 and pb["lead_days"] == 6 and pb["followed"] is True, pb
    ok += 1
    # at the print (126): the view missed by 7.1%, the street by 4.8% -> not closer
    g = r["graded"]
    assert g["closer"] is False and abs(g["err"]["value"] - (117 - 126) / 126 * 100) < 1e-9
    assert g["stance_hit"] is False and g["actual_stance"] == "above"
    ok += 1
    lee = live[("Nomura", "Lee")]
    assert lee["graded"]["stance_hit"] is True, "stance 'above' and the print beat"
    assert lee["published"]["followed"] is False,         "Nomura then published AT the street — 'above' did not follow through"
    ok += 1
    raj = live[("Emkay", "Raj")]
    assert raj["post_print"] is True and raj["graded"] is None
    assert vb["whisper"]["n"] == 1 and vb["whisper"]["median"] == 117,         "a post-print view must not enter the whisper"
    ok += 1
    assert ps["Q2FY27"]["status"] == "analyst views only"
    ok += 1
    sc = {(x["firm"].lower()[:5], x["analyst"].lower()): x for x in analyst_scorecard(conn)}
    rv = sc[("kotak", "ravi")]
    assert rv["n_views"] == 2 and rv["n_graded"] == 1 and rv["n_closer"] == 0         and rv["n_published"] == 1 and rv["n_followed"] == 1, rv
    assert sc[("nomur", "lee")]["n_stance_hit"] == 1
    ok += 1

    # rejection — each refused, nothing written
    def refused(needle, **k):
        nonlocal ok
        n0 = conn.execute("SELECT count(*) FROM results_analyst_views").fetchone()[0]
        d = sv(**{"firm": "Kotak", "analyst": "Ravi", "said_on": "2026-07-05",
                  "value_num": 119, **k})
        assert "error" in d and needle in d["error"], (needle, d)
        assert conn.execute("SELECT count(*) FROM results_analyst_views").fetchone()[0] == n0
        ok += 1
    refused("note is empty", note=" ")
    refused("a number or a stance", value_num=None)
    refused("needs its unit", unit="")
    refused("disagrees with what is stored", unit="INR bn")
    refused("in the future", said_on="2026-09-23")
    refused("stance 'bullish'", value_num=None, stance="bullish")
    refused("adapter's name", firm="consensus_yahoo")
    refused("not in `entities`", entity_id="acmee")
    refused("to withdraw", firm="Jefferies", withdraw=True)
    refused("is not a number", value_num="41.5bn")
    # acceptance twin of the unit guard: case/spacing is not a conflict
    assert sv(firm="Jefferies", analyst="Ann", said_on="2026-07-05",
              value_num=119, unit="inr  CR")["ok"]
    ok += 1
    return ok


# ----------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--load", help="one staged JSON file")
    ap.add_argument("--load-all", action="store_true",
                    help=f"every *.json under {STAGING.relative_to(REPO)}")
    ap.add_argument("--report", metavar="ENTITY_ID")
    ap.add_argument("--add-view", metavar="JSON",
                    help="one analyst view, e.g. '{\"entity_id\":\"tata_steel\","
                         "\"period\":\"Q2FY27\",\"metric\":\"ebitda_per_t\","
                         "\"firm\":\"Kotak\",\"analyst\":\"...\",\"value_num\":"
                         "9800,\"unit\":\"INR/t India\",\"note\":\"...\"}' "
                         "(the chat path; the Results tab form is the other)")
    ap.add_argument("--scorecard", action="store_true",
                    help="every analyst's record: closer than street, stance hits, follow-through")
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
    if a.add_view:
        conn = _connect()
        d = save_view(conn, json.loads(a.add_view), via="chat")
        conn.close()
        print(json.dumps(d, indent=1))
        return 2 if d.get("error") else 0
    if a.scorecard:
        conn = _connect()
        rows = analyst_scorecard(conn)
        conn.close()
        if not rows:
            print("no analyst views stored")
        for r in rows:
            print(f"{r['firm']:<22} {r['analyst']:<16} views {r['n_views']:>3}  "
                  f"closer {r['n_closer']}/{r['n_graded']}  stance {r['n_stance_hit']}"
                  f"/{r['n_stance']}  published {r['n_published']} (followed "
                  f"{r['n_followed']})  names {', '.join(r['names'])}")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
