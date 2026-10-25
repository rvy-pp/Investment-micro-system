"""The fact pack and the arithmetic behind one stock's case agent.

PM, 2026-10-11: "can we create agents per company that track and work on that
specific analysis only? Can we start with say, TCS?" Each of the 32 dictated
legs can get its own Claude Code agent (`.claude/agents/<token>-agent.md`,
named off the IMS ticker token — `tcs-agent`, PM 2026-10-13)
whose only job is that stock's Analysis-tab checklist and its juice. The repo's
usual split holds: THIS SCRIPT DOES THE ARITHMETIC, THE AGENT DOES THE READING.

Three things live per stock under `data/analysis/cases/<entity>/` (gitignored,
like the book):

  case.md     the agent's working memory — thesis, scenarios, watch list, the
              questions it owes the PM. Prose. The agent rewrites it each run.
  build.json  the EPS build the agent maintains: one row per quarter (actual /
              pm / draft), each with a source, plus base/bear/bull scenarios.
  brief.json  what this script last computed from the store + build.json.

What the brief computes, so no agent ever does it in its head:

  priced    price, consensus FY0/FY1 EPS, forward P/E, P/E vs the sector median,
            consensus revisions (7/30/90d) — analysis.priced_auto plus Yahoo's
            own dated anchors.
  rerate    the last ~90 days split exactly: ln(price) = ln(EPS) + ln(P/E), and
            relative P/E then vs now. Earnings-led or multiple-led, measured.
  street    each house's preview for the open quarter, and the median.
  implied   what consensus FY EPS leaves for the quarters not yet reported,
            per quarter, against the last actual quarter (the h2 step).
  juice     per scenario: EPS / consensus EPS at TODAY's P/E (or a named anchor
            P/E), on each fiscal year — the method settled 2026-10-08.

Deliberately NOT computed: a lean. The juice the Kelly edge uses is the PM's
number (standing rule: nothing the system computes is used as an edge), so the
brief shows base / bear / bull and the PM picks. Confidence is displayed, never
turned into a weight — no formula for that was agreed and inventing one would
be the statistics the PM rejected.

    python packages/book/case_brief.py tcs              # print the brief
    python packages/book/case_brief.py tcs --json       # also write brief.json
    python packages/book/case_brief.py tcs --price-entry # analysis.py --add payload
    python packages/book/case_brief.py --scaffold infosys
    python packages/book/case_brief.py --selftest

Reads the store; writes only under data/analysis/cases/ and .claude/agents/.
Nothing here reaches `prices`, `estimates`, `book_kelly_edges` or a pillar.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import re
import sqlite3
import statistics as st
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import analysis  # noqa: E402

REPO = analysis.REPO
CASES = REPO / "data" / "analysis" / "cases"
AGENTS = REPO / ".claude" / "agents"
VAULT_INSIGHTS = pathlib.Path.home() / "OneDrive - PinPOINT" / "Obsidian Vault" / "AI Insights"

QKINDS = ("actual", "pm", "draft", "street")  # draft = the agent's proposal; street = no view
# `street` (PM, 2026-10-18: "what is the point of doing Q4 with no credible
# information" -> "make Q4 street"): a quarter nobody has a view on takes what
# consensus leaves for it, carried by the view's gap on the quarters that DO
# have one. A drafted number there would be invented juice — the Q4 draft was
# ~half of TCS's base juice before this existed.
CONFIDENCE = ("low", "medium", "high")  # the PM's; displayed only, never a weight
STREET_METRICS = ("revenue", "revenue_growth_cc_pct", "ebit_margin_pct", "ebit", "pat")
STREET_METRICS_EV = ("revenue", "volume", "ebitda", "ebitda_per_t", "pat")
RERATE_DAYS = 90                         # Yahoo carries a 90-days-ago anchor in every capture


# --------------------------------------------------------------- helpers
def _close_on(conn, ent: str, day: str):
    r = conn.execute("SELECT date, close FROM prices WHERE entity_id=? AND date<=? "
                     "ORDER BY date DESC LIMIT 1", (ent, day)).fetchone()
    return (r["date"], r["close"]) if r else None


def _cons(conn, ent: str, fy: str, metric: str = "eps", as_of: str | None = None):
    q = ("SELECT as_of, value_num FROM estimates WHERE entity_id=? AND broker='consensus_yahoo' "
         "AND period=? AND metric=?")
    args = [ent, fy, metric]
    if as_of:
        q += " AND as_of<=?"
        args.append(as_of)
    r = conn.execute(q + " ORDER BY as_of DESC LIMIT 1", args).fetchone()
    # Yahoo's exact-0.0 is a placeholder, never an estimate (results_io._usable_eps).
    return (r["as_of"], r["value_num"]) if r and r["value_num"] and r["value_num"] > 0 else None


def qlabel_key(q: str) -> tuple[int, int]:
    """'Q3FY27' -> (27, 3), sortable. Refuses anything else."""
    m = re.fullmatch(r"Q([1-4])FY(\d{2})", q)
    if not m:
        raise ValueError(f"quarter label {q!r} is not QnFYyy")
    return int(m.group(2)), int(m.group(1))


def fy_of(q: str) -> str:
    return f"FY{qlabel_key(q)[0]:02d}"


def quarter_of(d: dt.date) -> str:
    """Calendar date -> Indian fiscal quarter label. Oct-2026 is Q3FY27."""
    fy = d.year + (1 if d.month >= 4 else 0)
    return f"Q{((d.month - 4) % 12) // 3 + 1}FY{fy % 100:02d}"


# --------------------------------------------------------------- the pieces
def revisions(conn, ent: str, fy: str) -> dict | None:
    now = _cons(conn, ent, fy)
    if not now:
        return None
    out = {"fy": fy, "eps": round(now[1], 2), "as_of": now[0]}
    for n in (7, 30, 90):
        a = _cons(conn, ent, fy, f"eps_{n}d_ago", now[0])
        out[f"chg_{n}d_pct"] = round((now[1] / a[1] - 1) * 100, 2) if a else None
    na = _cons(conn, ent, fy, "n_analysts", now[0])
    out["n_analysts"] = int(na[1]) if na else None
    return out


def rerate(conn, ent: str, fy: str) -> dict | None:
    """Price move over the last RERATE_DAYS split into EPS and multiple.

    Start EPS is Yahoo's own `eps_90d_ago` from the latest capture — a
    point-in-time record, not a reconstruction — so the window always ends on
    the latest capture and starts exactly 90 days earlier. The split is exact
    in logs (price = P/E x EPS); the % figures are the logs exponentiated, so
    they multiply back rather than add.
    """
    now = _cons(conn, ent, fy)
    if not now:
        return None
    end_day = now[0]
    start_day = (dt.date.fromisoformat(end_day) - dt.timedelta(days=RERATE_DAYS)).isoformat()
    then = _cons(conn, ent, fy, f"eps_{RERATE_DAYS}d_ago", end_day)
    p0, p1 = _close_on(conn, ent, start_day), _close_on(conn, ent, end_day)
    if not (then and p0 and p1):
        return None
    lp, le = math.log(p1[1] / p0[1]), math.log(now[1] / then[1])
    lm = lp - le
    rel = {}
    for tag, day, col in (("start", start_day, f"eps_{RERATE_DAYS}d_ago"), ("end", end_day, "eps")):
        mine = (p0 if tag == "start" else p1)[1] / (then if tag == "start" else now)[1]
        peer = []
        for p in analysis._peers(conn, ent):
            pc, pe = _close_on(conn, p, day), _cons(conn, p, fy, col, end_day)
            if pc and pe:
                peer.append(pc[1] / pe[1])
        med = st.median(peer) if len(peer) >= 3 else None
        rel[tag] = {"pe": round(mine, 2), "sector_median": round(med, 2) if med else None,
                    "rel_pe": round(mine / med, 3) if med else None, "n_peers": len(peer)}
    return {"fy": fy, "start": p0[0], "end": p1[0],
            "price_pct": round((math.exp(lp) - 1) * 100, 2),
            "eps_pct": round((math.exp(le) - 1) * 100, 2),
            "pe_pct": round((math.exp(lm) - 1) * 100, 2),
            "share_from_eps": round(le / lp, 2) if abs(lp) > 1e-9 else None,
            "rel": rel}


def street(conn, ent: str, period: str, metrics: tuple = STREET_METRICS) -> dict:
    """Every house's latest preview for one period, and the median per metric.
    The machine consensus is excluded here — it is shown under `priced`."""
    rows = conn.execute(
        "SELECT broker, metric, value_num, unit, as_of FROM estimates WHERE entity_id=? AND period=? "
        "AND broker NOT IN ('consensus_yahoo','bloomberg') ORDER BY as_of", (ent, period)).fetchall()
    latest: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        latest[(r["broker"], r["metric"])] = r          # ordered, last wins
    out = {}
    for m in metrics:
        vals = [(b, r["value_num"], r["unit"], r["as_of"]) for (b, mm), r in latest.items() if mm == m]
        if vals:
            out[m] = {"median": round(st.median(v[1] for v in vals), 2), "unit": vals[0][2],
                      "houses": {b: v for b, v, _, _ in vals}, "n": len(vals),
                      "latest": max(v[3] for v in vals)}
    return out


DRIVERS = ("revenue_cr", "ebit_margin_pct", "pat_to_ebit")

# EV/EBITDA cases (PM, 2026-10-25, cement: "we will use EV/EBITDA instead of
# P/E. Process is similar"). The quarter machinery is IDENTICAL — actuals,
# one viewed quarter, street quarters carried, pins, the band — only the line
# the multiple applies to changes: EBITDA (Rs cr) instead of EPS. So an EV
# build is normalised ONCE at the boundary (`internal`) into the slot the
# machinery already reads, named `eps` below for history's sake: in an EV
# build that slot holds quarterly EBITDA in Rs cr, never an EPS.
VALUATIONS = ("pe", "ev_ebitda")
EV_DRIVERS = ("volume_mt", "ebitda_per_t")      # cement's two numbers a quarter


def is_ev(b: dict | None) -> bool:
    return (b or {}).get("valuation") == "ev_ebitda"


def drivers(b: dict | None) -> tuple:
    return EV_DRIVERS if is_ev(b) else DRIVERS


def internal(b: dict) -> dict:
    """An EV build in the machinery's shape: each quarter's and pin's
    `ebitda_cr` into the earnings slot, `anchor_multiple` into the anchor slot.
    A P/E build passes through untouched (bit-identical — the selftest's P/E
    cases are the regression). A P/E build may also name its anchor
    `anchor_multiple`; `anchor_pe` stays its legacy spelling."""
    b = json.loads(json.dumps(b))
    for sc in (b.get("scenarios") or {}).values():
        if "anchor_multiple" in sc:
            sc["anchor_pe"] = sc.pop("anchor_multiple")
    if not is_ev(b):
        return b
    for row in (b.get("quarters") or {}).values():
        if "ebitda_cr" in row:
            row["eps"] = row.pop("ebitda_cr")
    for pin in (b.get("street_q") or {}).values():
        if isinstance(pin, dict) and "ebitda_cr" in pin:
            pin["eps"] = pin.pop("ebitda_cr")
    return b


def resolve_row(row: dict, shares_cr: float | None, ev: bool = False) -> dict:
    """A quarter's EPS: as stated for an actual (the reported figure wins), or
    from drivers — revenue x EBIT margin x PAT/EBIT / shares — for a pm or
    draft row that carries all three. Drivers keep a forecast decomposable:
    "Q3 at 23.9%" is an argument, "Q3 EPS 38.6" is only a number.

    EV: EBITDA Rs cr = volume (mt) x EBITDA/t (Rs) / 10 — 1e6 t x Rs / 1e7.
    Checked on UltraTech's own Q1FY27: 41.3mt x Rs1,214 = Rs5,014cr against
    the cited Rs50.2bn. A x10 error here would read as a 10x EBITDA beat."""
    row = dict(row)
    if ev:
        if row.get("kind") != "actual" and all(isinstance(row.get(k), (int, float)) for k in EV_DRIVERS):
            row["eps"] = round(row["volume_mt"] * row["ebitda_per_t"] / 10, 1)
        return row
    if row.get("kind") != "actual" and shares_cr and all(isinstance(row.get(k), (int, float)) for k in DRIVERS):
        row["eps"] = round(row["revenue_cr"] * row["ebit_margin_pct"] / 100 * row["pat_to_ebit"] / shares_cr, 2)
    return row


def resolve_quarters(b: dict, overrides: dict | None = None) -> dict:
    """Base quarters with a scenario's overrides applied — a number replaces
    the EPS, a dict is merged into the row (e.g. just a lower margin) and the
    EPS re-derived from drivers."""
    qs, dv = {}, drivers(b)
    for q, row in (b.get("quarters") or {}).items():
        row = dict(row)
        ov = (overrides or {}).get(q)
        if isinstance(ov, (int, float)):
            row = {**row, "eps": ov, **{k: None for k in dv}}
        elif isinstance(ov, dict):
            row.update(ov)
        qs[q] = resolve_row(row, b.get("shares_cr"), is_ev(b))
    return qs


def street_per_q(qs: dict, cons: dict, fy: str, sq: dict | None = None,
                 q: str | None = None) -> float | None:
    """The street's EPS for quarter `q` of `fy`. A quarter pinned in `sq`
    (build.json `street_q`, the house-model median) is its pinned number; every
    other not-yet-reported quarter takes an EVEN share of what consensus leaves
    after the actuals and the pins — the split implied_rest prints when nothing
    is pinned. None when there is no consensus for fy, or nothing is left to
    split.

    WHY THE PIN EXISTS (PM, 2026-10-21, TechM): the even split read an in-line
    Q3 as 6% SHORT of the street. Consensus FY27 76.39 leaves 20.61 a quarter
    for H2, but the houses back-load it — MS/BofA/JPM Q3 19.18-21.13, Q4
    20.85-23.11 — so the view (19.36) was being compared with an average of
    Q3 and a fatter Q4. Against the house-median Q3 (19.74) it is -1.9%.
    Same arithmetic, wrong comparator; nothing raised."""
    pins = {k: v["eps"] for k, v in (sq or {}).items() if fy_of(k) == fy}
    if q is not None and q in pins:
        return pins[q]
    acts = [r["eps"] for k, r in qs.items() if fy_of(k) == fy and r.get("kind") == "actual"]
    n = 4 - len(acts) - len(pins)
    if fy not in cons or n <= 0:
        return None
    return (cons[fy] - sum(acts) - sum(pins.values())) / n


def viewed(qs: dict) -> list[str]:
    """Forecast quarters someone holds a view on (pm or draft)."""
    return sorted((q for q, r in qs.items() if r.get("kind") in ("pm", "draft")), key=qlabel_key)


def street_ratio(qs: dict, cons: dict, sq: dict | None = None) -> float | None:
    """The view's EPS over the street's on the quarters that carry a view —
    the CARRIED convention (PM, 2026-10-18): if the view is right, the street
    re-bases the later quarters and years on it. 1.0 with no viewed quarter;
    None when a viewed quarter's year has no consensus to compare with.

    The street side is the pinned house-median quarter when build.json
    carries one in `street_q`, else consensus's per-quarter AVERAGE of what is
    left (approximate: it ignores the street's own Q3/Q4 seasonality)."""
    V = viewed(qs)
    if not V:
        return 1.0
    den = 0.0
    for q in V:
        p = street_per_q(qs, cons, fy_of(q), sq, q)
        if p is None or qs[q].get("eps") is None:
            return None
        den += p
    return sum(qs[q]["eps"] for q in V) / den if den else None


def fill_street(qs: dict, cons: dict, ratio: float, sq: dict | None = None) -> dict:
    """Give every `street` quarter its street EPS (pinned, else consensus's
    per-quarter rest after the pins) x the carry ratio. Left None (and so an
    incomplete year) where no consensus exists."""
    for q, r in qs.items():
        if r.get("kind") == "street":
            p = street_per_q(qs, cons, fy_of(q), sq, q)
            r["eps"] = round(p * ratio, 2) if p is not None else None
    return qs


def band_key(group: str | None) -> str:
    """Which `case_bands` entry a leg reads: every IT sub-group reads IT."""
    return "IT" if is_it(group) else (group or "").upper()


def case_band(group: str | None, cfg: dict | None = None) -> dict | None:
    """The sector's band from specs/book.yaml `case_bands`, or None (then the
    build keeps hand-drawn bear/bull). Returned with its key, for the label."""
    import yaml
    cfg = cfg if cfg is not None else yaml.safe_load(analysis.BOOK_YAML.read_text(encoding="utf-8"))
    band = (cfg.get("case_bands") or {}).get(band_key(group))
    return {**band, "key": band_key(group)} if band else None


def case_valuation(group: str | None, cfg: dict | None = None) -> str:
    """Which multiple a leg's case runs on: specs/book.yaml `case_valuation`
    by sector (CEMENT: ev_ebitda, PM 2026-10-25), else P/E. A sector setting,
    like the band — never chosen name by name."""
    import yaml
    cfg = cfg if cfg is not None else yaml.safe_load(analysis.BOOK_YAML.read_text(encoding="utf-8"))
    return (cfg.get("case_valuation") or {}).get(band_key(group), "pe")


def band_text(bd: dict) -> str:
    """The band's two inputs in words, for either shape (IT margin/growth,
    EV sector EBITDA-per-tonne/volume). Both print sites read it: they
    assumed IT's keys and raised KeyError on the first cement band."""
    if "per_t_inr" in bd:
        return f"EBITDA/t ±Rs{bd['per_t_inr']}, volume growth ±{bd['growth_pp']}pp"
    return f"margin ±{bd['margin_bps']}bps, growth ±{bd['growth_pp']}pp"


def band_scenarios(b: dict, band: dict) -> dict:
    """Bear and bull as the BASE moved by the sector band (PM, 2026-10-18:
    "fixed sector band, ±50bps margin and ±0.5pp growth for IT, carried").

    Every forecast quarter (pm or draft) moves its EBIT margin by margin_bps
    and its QoQ growth by growth_pp — so the k-th forecast quarter's revenue
    is the base's x (1 ± g)^k: growth is a per-quarter input and the level
    compounds. Actuals never move. Later years are carried in scenario_juice.
    Mechanical by design: there is no story here to write, and none is
    invented."""
    base = (b.get("scenarios") or {}).get("base") or {}
    qs = resolve_quarters(b, base.get("quarters"))
    fc = viewed(qs)            # street quarters follow by the carry ratio, never moved directly
    out = {}
    for name, s in (("bear", -1), ("bull", 1)):
        sg = "+" if s > 0 else "-"
        if is_ev(b):
            # EV band: EBITDA/t moves by per_t_inr, volume growth by growth_pp
            # per forecast quarter (the volume LEVEL compounds), the same
            # two-input shape as IT's margin / growth.
            g = band["growth_pp"] / 100
            ov = {q: {"volume_mt": round(qs[q]["volume_mt"] * (1 + s * g) ** k, 3),
                      "ebitda_per_t": round(qs[q]["ebitda_per_t"] + s * band["per_t_inr"], 2)}
                  for k, q in enumerate(fc, 1)}
            why = (f"{band.get('key', '')} band: every forecast quarter's EBITDA/t {sg}Rs{band['per_t_inr']} "
                   f"and volume growth {sg}{band['growth_pp']}pp (volume level compounds), carried into "
                   f"later years at the same EBITDA ratio. Mechanical, not a story.")
        else:
            m, g = band["margin_bps"] / 100, band["growth_pp"] / 100
            ov = {q: {"revenue_cr": round(qs[q]["revenue_cr"] * (1 + s * g) ** k, 1),
                      "ebit_margin_pct": round(qs[q]["ebit_margin_pct"] + s * m, 4),
                      "pat_to_ebit": qs[q]["pat_to_ebit"]}
                  for k, q in enumerate(fc, 1)}
            why = (f"{band.get('key', '')} band: every forecast quarter's EBIT margin "
                   f"{sg}{band['margin_bps']}bps and QoQ growth "
                   f"{sg}{band['growth_pp']}pp (revenue level compounds), "
                   f"carried into later years at the same EPS ratio. Mechanical, not a story.")
        out[name] = {"band": True, "quarters": ov, "why": why}
        # The band moves EPS, never the multiple: a base anchor P/E (PM,
        # 2026-10-20 — TCS on the broker-median implied multiple) is carried
        # into bear and bull. Without this the base read at the anchor while
        # bear/bull silently stayed at today's P/E — a range straddling two
        # multiples, plausible on the page and wrong by the whole anchor gap.
        if base.get("anchor_pe") is not None:
            out[name].update(anchor_pe=base["anchor_pe"], anchor_why=base.get("anchor_why"))
    return out


ANCHOR_STATS = ("mean", "median")


def implied_anchor(b: dict, cons: dict, ev: dict | None, fy: str) -> dict | None:
    """Each house's IMPLIED multiple on `fy` and their mean and median, from
    build.json `house_tps` — the PM's anchor rule made mechanical (TCS
    2026-10-20 "adopt median"; cement 2026-10-25 "get latest valuations,
    select mean").

        P/E        implied = TP / consensus EPS
        EV/EBITDA  implied = (TP x shares + net debt) / consensus EBITDA

    Implied, never the stated multiple: the houses state theirs on mixed bases
    (FY28E, Sep-28E, Dec-28E, 2YF), and only the implied one puts every TP on
    the year and the earnings line the juice is measured on. The EV form uses
    the SAME net debt as ev_juice, so the round trip is exact: the mean implied
    multiple on consensus EBITDA gives back the mean TP (tested)."""
    tps = b.get("house_tps") or []
    if fy not in cons or not tps:
        return None
    rows = []
    for t in tps:
        if ev is not None:
            m = (t["tp"] * ev["shares_cr"] + ev["net_debt_cr"]) / cons[fy]
        else:
            m = t["tp"] / cons[fy]
        rows.append({**t, "implied": round(m, 2), "_m": m})
    ms = [r.pop("_m") for r in rows]
    tp = [t["tp"] for t in tps]
    # Full precision, rounded only when printed: a 0.005x rounding is ~Rs3.7 a
    # share on UltraTech's ~Rs22,000cr EBITDA, enough to break the round trip.
    return {"fy": fy, "n": len(rows), "houses": rows,
            "mean": st.mean(ms), "median": st.median(ms),
            "tp_mean": round(st.mean(tp), 1), "tp_median": round(st.median(tp), 1)}


def apply_anchor(b: dict, anc: dict | None) -> dict:
    """A base scenario with `"anchor": "mean"` (or "median") takes that
    statistic of the implied multiples as its anchor, with an anchor_why that
    names every house, TP and date. Returns the build unchanged otherwise."""
    base = (b.get("scenarios") or {}).get("base") or {}
    stat = base.get("anchor")
    if stat not in ANCHOR_STATS or not anc:
        return b
    b = json.loads(json.dumps(b))
    names = " / ".join(f"{h['house']} {h['tp']:,.0f} ({h['date']})" for h in anc["houses"])
    b["scenarios"]["base"]["anchor_pe"] = anc[stat]
    b["scenarios"]["base"]["anchor_why"] = (
        f"{stat} of {anc['n']} houses' implied {anc['fy']} multiples = {anc[stat]:.2f}x "
        f"(mean {anc['mean']:.2f}x, median {anc['median']:.2f}x; TP mean {anc['tp_mean']:,.0f}, median "
        f"{anc['tp_median']:,.0f}): {names}. " + (base.get("anchor_note") or "")).strip()
    return b


def _validate_tps(b: dict) -> list[str]:
    probs = []
    base = (b.get("scenarios") or {}).get("base") or {}
    if "anchor" in base:
        if base["anchor"] not in ANCHOR_STATS:
            probs.append(f"base anchor {base['anchor']!r} not in {ANCHOR_STATS}")
        if not b.get("house_tps"):
            probs.append("base anchor from house TPs needs house_tps")
        if base.get("anchor_pe") is not None or base.get("anchor_multiple") is not None:
            probs.append("base carries both a computed anchor and a typed one — keep one")
    for i, t in enumerate(b.get("house_tps") or []):
        if not (isinstance(t, dict) and (t.get("house") or "").strip()):
            probs.append(f"house_tps[{i}]: needs a house"); continue
        if not (isinstance(t.get("tp"), (int, float)) and t["tp"] > 0):
            probs.append(f"house_tps {t['house']}: needs a positive tp")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t.get("date") or ""):
            probs.append(f"house_tps {t['house']}: needs an ISO date")
        if not (t.get("source") or "").strip():
            probs.append(f"house_tps {t['house']}: needs a source")
    return probs


def _validate_ev(b: dict) -> list[str]:
    """What an EV/EBITDA build needs that a P/E build gets from the store.

    There is no daily consensus for these names (the Yahoo capture is EMS + IT
    only, and Yahoo carries no EBITDA anyway), so the consensus EBITDA, the
    share count and the net debt are written into build.json — each sourced,
    because each moves the juice: net debt is the wedge between an EBITDA gap
    and an equity return, and a missing one makes every case read as if the
    company were debt-free."""
    probs = []
    if not (isinstance(b.get("shares_cr"), (int, float)) and b["shares_cr"] > 0):
        probs.append("an EV build needs shares_cr (equity value / shares = price)")
    if not isinstance(b.get("net_debt_cr"), (int, float)):
        probs.append("an EV build needs net_debt_cr (negative for net cash)")
    if not (b.get("net_debt_source") or "").strip():
        probs.append("net_debt_cr needs a net_debt_source")
    cons = b.get("consensus") or {}
    if not cons:
        probs.append("an EV build needs consensus EBITDA per fiscal year (consensus: {FY: {ebitda_cr, source}})")
    for fy, c in cons.items():
        if not re.fullmatch(r"FY\d{2}", fy):
            probs.append(f"consensus {fy!r} is not FYyy")
        elif not (isinstance(c, dict) and isinstance(c.get("ebitda_cr"), (int, float)) and c["ebitda_cr"] > 0):
            probs.append(f"consensus {fy}: needs a positive ebitda_cr")
        elif not (c.get("source") or "").strip():
            probs.append(f"consensus {fy}: needs a source (which houses, which notes)")
    if b.get("headline_fy") and cons and b["headline_fy"] not in cons:
        probs.append(f"headline {b['headline_fy']} has no consensus EBITDA")
    for name, sc in (b.get("scenarios") or {}).items():
        if "anchor_pe" in sc:
            probs.append(f"scenario {name}: anchor_pe in an EV/EBITDA build — the anchor is anchor_multiple")
    for q, row in (b.get("quarters") or {}).items():
        if "eps" in row:
            probs.append(f"{q}: an EV build's quarters carry ebitda_cr, not eps")
    return probs


def validate_build(b: dict, band: dict | None = None) -> list[str]:
    """Every problem with a build.json, named. An actual needs a source; a
    quarter label must parse; a scenario may override only quarters that exist.
    With a sector band, bear/bull are generated — a hand-drawn one is refused,
    and every forecast quarter must carry drivers for the band to move."""
    probs = []
    raw, b = b, internal(b)
    dv = drivers(b)
    if (raw.get("valuation") or "pe") not in VALUATIONS:
        probs.append(f"valuation {raw.get('valuation')!r} not in {VALUATIONS}")
    if is_ev(raw):
        probs += _validate_ev(raw)
    probs += _validate_tps(raw)
    if band is not None:
        bk = ("per_t_inr", "growth_pp") if is_ev(b) else ("margin_bps", "growth_pp")
        if not all(isinstance(band.get(k), (int, float)) and band[k] > 0 for k in bk):
            probs.append(f"case band {band!r}: {' and '.join(bk)} must be positive numbers")
        hand = [k for k in ("bear", "bull") if k in (b.get("scenarios") or {})]
        if hand:
            probs.append(f"scenario {', '.join(hand)}: bear/bull come from the {band.get('key')} case band — "
                         "delete the hand-drawn ones")
        if "base" not in (b.get("scenarios") or {}):
            probs.append("a banded build needs a base scenario")
        for q, row in resolve_quarters(b, ((b.get("scenarios") or {}).get("base") or {}).get("quarters")).items():
            if row.get("kind") in ("pm", "draft") and not all(isinstance(row.get(k), (int, float)) for k in dv):
                probs.append(f"{q}: the band moves drivers, so a forecast quarter needs {', '.join(dv)}")
    line = "ebitda_cr" if is_ev(b) else "eps"
    qs = resolve_quarters(b)
    for q, row in qs.items():
        try:
            qlabel_key(q)
        except ValueError as e:
            probs.append(str(e)); continue
        if row.get("kind") not in QKINDS:
            probs.append(f"{q}: kind {row.get('kind')!r} not in {QKINDS}")
        if row.get("kind") == "street":
            if any(row.get(k) is not None for k in ("eps",) + dv):
                probs.append(f"{q}: a street quarter takes consensus — it carries no {line} or drivers")
            continue
        if not isinstance(row.get("eps"), (int, float)):
            probs.append(f"{q}: {line} must be a number, got {row.get('eps')!r}")
        if row.get("kind") == "actual" and not (row.get("source") or "").strip():
            probs.append(f"{q}: an actual needs a source")
    for name, sc in (b.get("scenarios") or {}).items():
        for q in (sc.get("quarters") or {}):
            if q not in qs:
                probs.append(f"scenario {name}: overrides {q}, which is not in quarters")
        if not (sc.get("why") or "").strip():
            probs.append(f"scenario {name}: says nothing about why — every scenario needs words")
        if sc.get("anchor_pe") is not None and not (sc.get("anchor_why") or "").strip():
            probs.append(f"scenario {name}: an anchor P/E needs a named anchor (anchor_why)")
    # PM, 2026-10-17: "I want low, high, medium" — the same three words
    # book_calls.conviction takes, so a stock's confidence and a pair call's
    # conviction read on one scale. The 1-5 scale it replaces was never set.
    if b.get("confidence") is not None and b.get("confidence") not in CONFIDENCE:
        probs.append(f"confidence {b.get('confidence')!r} not in {CONFIDENCE} (or null)")
    # street_q: a sourced street EPS for one quarter (PM, 2026-10-21 — the
    # house-model median), compared with the view instead of the even split.
    # A pin is a number somebody published, so it needs a source like an actual.
    sq = b.get("street_q") or {}
    for q, pin in sq.items():
        try:
            qlabel_key(q)
        except ValueError as e:
            probs.append(f"street_q {e}"); continue
        if not isinstance(pin, dict) or not isinstance(pin.get("eps"), (int, float)) or pin["eps"] <= 0:
            probs.append(f"street_q {q}: needs a positive {line}")
            continue
        if not (pin.get("source") or "").strip():
            probs.append(f"street_q {q}: a pinned street quarter needs a source")
        if (qs.get(q) or {}).get("kind") == "actual":
            probs.append(f"street_q {q}: the quarter has reported — the actual is the number, not a street pin")
    def _parses(q):
        try:
            qlabel_key(q); return True
        except ValueError:
            return False
    for fy in {fy_of(q) for q in sq if _parses(q)}:
        n_act = sum(1 for q, r in qs.items() if fy_of(q) == fy and r.get("kind") == "actual")
        n_pin = sum(1 for q in sq if _parses(q) and fy_of(q) == fy)
        if n_act + n_pin >= 4:
            probs.append(f"street_q {fy}: pins every unreported quarter — leave one to take what consensus leaves")
    return probs


def fy_sums(quarters: dict) -> dict:
    """Sum EPS per fiscal year; a year is complete only with all four quarters."""
    by: dict[str, dict] = {}
    for q, row in quarters.items():
        if row.get("eps") is None:          # a street quarter with no consensus to take
            continue
        fy = fy_of(q)
        d = by.setdefault(fy, {"eps": 0.0, "n": 0, "kinds": {}})
        d["eps"] += row["eps"]; d["n"] += 1
        d["kinds"][row["kind"]] = d["kinds"].get(row["kind"], 0) + 1
    return {fy: {**d, "eps": round(d["eps"], 2), "complete": d["n"] == 4} for fy, d in by.items()}


def implied_rest(quarters: dict, cons_fy_eps: float, fy: str) -> dict | None:
    """What consensus FY EPS leaves for the quarters with no actual yet."""
    acts = {q: r for q, r in quarters.items() if fy_of(q) == fy and r["kind"] == "actual"}
    if not acts or len(acts) == 4:
        return None
    left = 4 - len(acts)
    rest = cons_fy_eps - sum(r["eps"] for r in acts.values())
    last_q = max(acts, key=qlabel_key)
    per_q = rest / left
    return {"fy": fy, "actual_quarters": sorted(acts, key=qlabel_key), "left": left,
            "implied_per_q": round(per_q, 2), "last_actual": last_q,
            "last_actual_eps": acts[last_q]["eps"],
            "step_vs_last_pct": round((per_q / acts[last_q]["eps"] - 1) * 100, 2)}


def ev_juice(e: float, c: float, mult_today: float, anchor: float | None, ev: dict) -> dict:
    """EV/EBITDA juice on one year: the multiple prices the ENTERPRISE, and
    net debt sits between it and the equity.

        target equity = M x EBITDA - net debt,   M = anchor or today's multiple
        juice         = target equity / today's market cap - 1

    Today's market cap is exactly mult_today x consensus - net debt, so with no
    anchor a view equal to consensus is zero juice (tested), and an EBITDA gap
    reaches the equity LEVERED by EV / market cap — a 5% EBITDA beat is ~5.4%
    of equity at UltraTech's ~0.07x net debt to market cap, and much more on a
    geared name. Reading the EBITDA gap as the return would be the P/E rule on
    the wrong line: right for a debt-free company and silently wrong elsewhere.
    Net debt is held at today's figure in every case; the method re-marks the
    multiple and the earnings line, it does not forecast the balance sheet."""
    m = anchor if anchor else mult_today
    eq = m * e - ev["net_debt_cr"]
    return {"eps": round(e, 1), "cons": round(c, 1),
            "eps_gap_pct": round((e / c - 1) * 100, 2),
            "pe_today": round(mult_today, 2), "pe_target": round(m, 2),
            "target_price": round(eq / ev["shares_cr"], 1),
            "juice_pct": round((eq / ev["mcap_cr"] - 1) * 100, 2)}


def scenario_juice(b: dict, cons: dict[str, float], pe_today: dict[str, float],
                   band: dict | None = None, ev: dict | None = None) -> dict:
    """Per scenario: its quarter build (base quarters with overrides), FY sums,
    and juice on each FY that consensus covers.

    juice = (scenario EPS / consensus EPS) x (anchor P/E / today's P/E) - 1.
    With no anchor the multiple holds at today's level, so juice is pure EPS
    gap — the method's default. A later FY that the quarter build does not
    complete takes `fy_eps` from the scenario if given, else growth_pct on the
    prior FY; anything else is left out rather than guessed.

    With a sector band, bear/bull are band_scenarios() of the base, and a later
    year is CARRIED: base EPS x (sum of the moved forecast quarters' EPS / sum
    of the base's). Growing FY28 off a moved FY27 instead would dilute the move
    with the two unmoved actual quarters — a 50bps margin shift would reach
    FY28 at about half strength, the "a Q3 move barely reaches FY28" reading
    the PM ruled out (2026-10-18).

    `street` quarters take consensus's per-quarter rest x the carry ratio r0
    (the base view's EPS over the street's on the viewed quarters); a base
    later year with no stated fy_eps/growth_pct is consensus x r0. A band
    scenario moves r0 by its own ratio on the viewed quarters, so the street
    quarters and later years follow the moved view by the same rule.

    EV builds (`ev` = net debt, market cap, shares): the same build on EBITDA,
    valued by ev_juice. `pe_today` then holds today's EV/EBITDA per year.
    """
    b = internal(b)
    specs = dict(b.get("scenarios") or {})
    if band is not None:
        specs = {"base": specs.get("base") or {}, **band_scenarios(b, band)}
    bq = resolve_quarters(b, (specs.get("base") or {}).get("quarters"))
    sq = b.get("street_q") or {}
    r0 = street_ratio(bq, cons, sq)
    r0 = 1.0 if r0 is None else r0
    bq = fill_street(bq, cons, r0, sq)
    V = viewed(bq)
    den = sum(bq[q]["eps"] for q in V)
    out = {}
    base_fy = None
    for name, sc in specs.items():
        if name == "base":
            qs, rel = bq, 1.0
        else:
            qs = resolve_quarters(b, sc.get("quarters"))
            rel = sum(qs[q]["eps"] for q in V) / den if (sc.get("band") and den) else 1.0
            fill_street(qs, cons, r0 * rel, sq)
        sums = fy_sums(qs)
        eps_fy = {fy: d["eps"] for fy, d in sums.items() if d["complete"]}
        for fy, v in (sc.get("fy_eps") or {}).items():
            eps_fy[fy] = v
        for fy, g in (sc.get("growth_pct") or {}).items():
            prev = f"FY{int(fy[2:]) - 1:02d}"
            if prev in eps_fy and fy not in eps_fy:
                eps_fy[fy] = round(eps_fy[prev] * (1 + g / 100), 2)
        if name == "base":
            for fy in cons:                    # carried: no stated later year -> the street, re-based
                if fy not in eps_fy:
                    eps_fy[fy] = round(cons[fy] * r0, 2)
            base_fy = dict(eps_fy)
        elif sc.get("band") and base_fy is not None:
            for fy, v in base_fy.items():
                if fy not in eps_fy:
                    eps_fy[fy] = round(v * rel, 2)
        juice = {}
        for fy, e in eps_fy.items():
            if fy in cons and fy in pe_today and ev is not None:
                juice[fy] = ev_juice(e, cons[fy], pe_today[fy], sc.get("anchor_pe"), ev)
            elif fy in cons and fy in pe_today:
                mult = (sc["anchor_pe"] / pe_today[fy]) if sc.get("anchor_pe") else 1.0
                juice[fy] = {"eps": round(e, 2), "cons": round(cons[fy], 2),
                             "eps_gap_pct": round((e / cons[fy] - 1) * 100, 2),
                             "pe_today": round(pe_today[fy], 2),
                             "pe_target": round(sc["anchor_pe"], 2) if sc.get("anchor_pe") else round(pe_today[fy], 2),
                             "juice_pct": round((e / cons[fy] * mult - 1) * 100, 2)}
        out[name] = {"why": sc.get("why"), "anchor_why": sc.get("anchor_why"), "band": bool(sc.get("band")),
                     "quarters": {q: r["eps"] for q, r in sorted(qs.items(), key=lambda x: qlabel_key(x[0]))},
                     "fy_eps": {k: round(v, 2) for k, v in eps_fy.items()}, "juice": juice}
    return out


def pair_context(conn, ent: str, base_juice: float | None) -> list[dict]:
    """Each dictated pair the stock sits in: the partner, its juice, the pair
    juice IF this stock's base were the lean, the PM's live Kelly edge."""
    v = analysis.view(conn)
    by = {s["entity"]: s for s in v["stocks"]}
    out = []
    for p in v["pairs"]:
        if ent not in p["long"] + p["short"]:
            continue
        side = "long" if ent in p["long"] else "short"
        other = p["short"] if side == "long" else p["long"]
        oj = [by.get(e, {}).get("juice") for e in other]
        oj_mean = st.mean(oj) if oj and all(x is not None for x in oj) else None
        what_if = None
        if base_juice is not None and oj_mean is not None:
            what_if = round(base_juice - oj_mean if side == "long" else oj_mean - base_juice, 2)
        # The Kelly edge IS the logged pair juice since 2026-10-22 (kelly.
        # agent_edges); book_kelly_edges is history nothing reads.
        out.append({"pair": p["name"], "side": side,
                    "partners": [by.get(e, {}).get("name", e) for e in other],
                    "partner_juice": oj_mean, "pair_juice_if_base": what_if,
                    "pair_juice_logged": p["pair_juice"]})
    return out


def concall_notes(ent: str, token: str) -> list[str]:
    """Vault concall notes on file for the stock (read-only; the Results tab's
    Concall sub-tab reads the same files)."""
    names = {token, token.upper()}
    # The vault folder is keyed on the NSE symbol (ULTRACEMCO_Concall), which
    # the IMS token (UTCEM) is not; the entity specs carry it.
    try:
        import yaml
        for f in (REPO / "specs" / "entities").glob("*.yaml"):
            for e in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("entities") or []:
                if e.get("id") == ent and e.get("nse_symbol"):
                    names.add(str(e["nse_symbol"]))
    except Exception:
        pass
    try:
        sys.path.insert(0, str(REPO / "packages" / "api"))
        import engine
        names |= {d.split("_")[0] for k, d in getattr(engine, "CONCALL_DIRS", {}).items() if k == ent}
    except Exception:
        pass
    out = []
    for n in names:
        d = VAULT_INSIGHTS / f"{n}_Concall"
        if d.is_dir():
            out += sorted(p.name for p in d.glob("Concall - *.md"))
    return sorted(set(out))


# --------------------------------------------------------------- brief
def load_build(ent: str) -> dict | None:
    f = CASES / ent / "build.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def brief(ent: str, conn=None, today: dt.date | None = None, build: dict | None = None) -> dict:
    own = conn is None
    conn = conn or analysis.connect()
    try:
        today = today or dt.date.today()
        ros = {r["entity"]: r for r in analysis.roster()}
        if ent not in ros:
            raise SystemExit(f"{ent!r} is not one of the dictated legs")
        me = ros[ent]
        fy0, fy1 = analysis.fy_label(today), analysis.fy_label(today, 1)
        priced = analysis.priced_auto(conn, ent, today)
        revs = {fy: revisions(conn, ent, fy) for fy in (fy0, fy1)}
        cons = {fy: r["eps"] for fy, r in revs.items() if r}
        px = analysis._close(conn, ent)
        pe_today = {fy: px[1] / e for fy, e in cons.items()} if px else {}
        build = build if build is not None else load_build(ent)
        valuation = case_valuation(me["group"])
        ev = None
        if valuation == "ev_ebitda":
            # No feed carries these names' consensus EBITDA, so the build does
            # (sourced); Yahoo's EPS rows, if any, are not this case's line.
            bc = (build or {}).get("consensus") or {}
            cons = {fy: c["ebitda_cr"] for fy, c in bc.items()
                    if isinstance(c, dict) and isinstance(c.get("ebitda_cr"), (int, float)) and c["ebitda_cr"] > 0}
            sh, nd = (build or {}).get("shares_cr"), (build or {}).get("net_debt_cr")
            if px and isinstance(sh, (int, float)) and sh > 0 and isinstance(nd, (int, float)):
                mcap = px[1] * sh
                ev = {"price": px[1], "price_date": px[0], "shares_cr": sh, "mcap_cr": round(mcap, 1),
                      "net_debt_cr": nd, "net_debt_source": build.get("net_debt_source"),
                      "ev_cr": round(mcap + nd, 1)}
                pe_today = {fy: (mcap + nd) / c for fy, c in cons.items()}
            else:
                pe_today = {}
        out = {"entity": ent, "name": me["name"], "token": me["token"], "side": me["side"],
               "group": me["group"], "as_of": today.isoformat(), "valuation": valuation,
               "multiple": "EV/EBITDA" if valuation == "ev_ebitda" else "P/E",
               "ev": ev, "consensus": cons, "open_quarter": quarter_of(today),
               "priced": priced, "revisions": revs,
               "pe_today": {k: round(v, 2) for k, v in pe_today.items()},
               "rerate": rerate(conn, ent, fy1),
               "street": {q: street(conn, ent, q, STREET_METRICS_EV if valuation == "ev_ebitda" else STREET_METRICS)
                          for q in sorted(
                   {r["period"] for r in conn.execute(
                       "SELECT DISTINCT period FROM estimates WHERE entity_id=? AND period LIKE 'Q_FY__' "
                       "AND broker NOT IN ('consensus_yahoo','bloomberg')", (ent,))}, key=qlabel_key)},
               "concall_notes": concall_notes(ent, me["token"]),
               "log": analysis._entries(conn, ent)}
        if build:
            band = case_band(me["group"])
            out["band"] = band
            probs = validate_build(build, band)
            if (build.get("valuation") or "pe") != valuation:
                probs.append(f"build says valuation {build.get('valuation') or 'pe'!r} but the {band_key(me['group'])} "
                             f"sector runs on {valuation!r} (specs/book.yaml case_valuation)")
            if valuation == "ev_ebitda" and ev is None and not probs:
                probs.append("no close in prices for the EV bridge")
            build = internal(build)
            anc = implied_anchor(build, cons, ev, build.get("headline_fy", fy1))
            out["anchor"] = anc
            if ((build.get("scenarios") or {}).get("base") or {}).get("anchor") and anc is None:
                probs.append("base anchor from house TPs, but no implied multiple could be computed "
                             f"(needs {build.get('headline_fy', fy1)} consensus and, for EV, the EV bridge)")
            build = apply_anchor(build, anc)
            qs = resolve_quarters(build, ((build.get("scenarios") or {}).get("base") or {}).get("quarters"))
            sq = build.get("street_q") or {}
            r0 = street_ratio(qs, cons, sq)
            probs += [f"{q}: a street quarter needs {fy_of(q)} consensus, and there is none"
                      for q, r in qs.items() if r.get("kind") == "street" and street_per_q(qs, cons, fy_of(q), sq, q) is None]
            out["build_problems"] = probs
            if not probs:
                qs = fill_street(qs, cons, 1.0 if r0 is None else r0, sq)
                out["street_q"] = sq
                out["street_ratio"] = None if r0 is None else round(r0, 4)
                out["quarters"] = qs
                out["fy_sums"] = fy_sums(qs)
                out["implied"] = {fy: implied_rest(qs, cons[fy], fy) for fy in cons}
                out["scenarios"] = scenario_juice(build, cons, pe_today, band, ev)
                out["confidence"] = build.get("confidence")
                out["horizon"] = build.get("horizon")
                out["headline_fy"] = build.get("headline_fy", fy1)
        if is_it(me["group"]):
            import it_deals
            doc = it_deals.load(ent)
            out["deals"] = it_deals.summary(doc, today) if doc else None
        base = (out.get("scenarios") or {}).get("base", {}).get("juice", {}).get(out.get("headline_fy", fy1), {})
        out["pairs"] = pair_context(conn, ent, base.get("juice_pct"))
        return out
    finally:
        if own:
            conn.close()


def price_entry(b: dict) -> dict:
    """The analysis.py --add payload for the `price` step, built from the
    scenarios: bear and bull become juice_low / juice_high. NO juice_pct — the
    lean is the PM's, added by hand when they pick one."""
    fy = b.get("headline_fy")
    sc = b.get("scenarios") or {}
    j = {k: sc[k]["juice"][fy]["juice_pct"] for k in ("bear", "base", "bull")
         if k in sc and fy in sc[k]["juice"]}
    if "bear" not in j or "bull" not in j:
        raise SystemExit("price entry needs bear and bull scenarios on the headline FY")
    bd = b.get("band")
    how = (f" Range = base moved by the {bd['key']} band ({band_text(bd)} per forecast quarter, carried)."
           if bd else "")
    # The multiple is named from the scenario itself: this line said "at today's
    # P/E" unconditionally, which was false the first time an anchor was set
    # (TCS, 2026-10-20 — juice at 14.54x printed as "today's P/E", 15.27x).
    jb = (sc.get("base") or {}).get("juice", {}).get(fy) or {}
    mu = b.get("multiple") or "P/E"
    at = (f"the anchor {mu} {jb['pe_target']}x (today {jb['pe_today']}x)"
          if jb and jb.get("pe_target") != jb.get("pe_today") else f"today's {mu}")
    note = (f"{fy} juice at {at}: bear {j['bear']:+.1f}%, base {j.get('base', float('nan')):+.1f}%, "
            f"bull {j['bull']:+.1f}% (horizon: {b.get('horizon') or 'unstated'}).{how} No lean picked — PM to choose.")
    return {"entity": b["entity"], "step": "price", "status": "partial", "via": f"agent:{agent_name(b['token'])}",
            "note": note, "data": {"juice_low": j["bear"], "juice_high": j["bull"],
                                   "horizon": b.get("horizon")}}


def render(b: dict) -> str:
    L = [f"{b['name']} ({b['entity']}) — {b['side']} leg, {b['group']} · as of {b['as_of']} "
         f"· open quarter {b['open_quarter']}"]
    ev = b.get("ev")
    line = "EBITDA" if ev is not None or b.get("valuation") == "ev_ebitda" else "EPS"
    if ev:
        L.append(f"PRICED  close {ev['price']} ({ev['price_date']}) x {ev['shares_cr']}cr shares = mcap "
                 f"Rs{ev['mcap_cr']:,.0f}cr + net debt Rs{ev['net_debt_cr']:,.0f}cr = EV Rs{ev['ev_cr']:,.0f}cr")
        for fy, m in (b.get("pe_today") or {}).items():
            c = (b.get("consensus") or {}).get(fy)
            L.append(f"        {fy} consensus EBITDA Rs{c:,.1f}cr → EV/EBITDA {m}x")
    elif b.get("valuation") == "ev_ebitda":
        L.append("PRICED  no EV bridge yet — build.json needs shares_cr, net_debt_cr and consensus")
    p = b.get("priced")
    if p and not ev:
        L.append(f"PRICED  close {p['price']} ({p['price_date']}) · {p['fy1']} EPS {p['eps_fy1']} → "
                 f"P/E {p['pe_fy1']}x · sector median {p['sector_pe_median']}x · rel {p['rel_pe']} "
                 f"(n={p['n_peers']}) · {p['fy0']}→{p['fy1']} EPS growth "
                 f"{(p['eps_growth'] or 0) * 100:+.1f}%")
    for fy, r in (b.get("revisions") or {}).items():
        if r:
            L.append(f"        {fy} consensus {r['eps']} (n={r['n_analysts']}) · revisions "
                     f"7d {r['chg_7d_pct']:+}% · 30d {r['chg_30d_pct']:+}% · 90d {r['chg_90d_pct']:+}%")
    r = b.get("rerate")
    if r:
        s, e = r["rel"]["start"], r["rel"]["end"]
        L.append(f"RERATE  {r['start']}→{r['end']}: price {r['price_pct']:+}% = EPS {r['eps_pct']:+}% x "
                 f"P/E {r['pe_pct']:+}% · P/E {s['pe']}→{e['pe']}x · rel P/E {s['rel_pe']}→{e['rel_pe']}")
    for q, sm in (b.get("street") or {}).items():
        bits = [f"{m} {d['median']}{'' if d['unit'] in ('INR cr',) else d['unit']} (n={d['n']})"
                for m, d in sm.items()]
        L.append(f"STREET  {q}: " + " · ".join(bits))
    if b.get("build_problems"):
        L.append("BUILD   REFUSED: " + "; ".join(b["build_problems"]))
    for fy, d in (b.get("fy_sums") or {}).items():
        L.append(f"BUILD   {fy}: {line} {d['eps']} from {d['n']}/4 quarters {d['kinds']}")
    for q, pin in sorted((b.get("street_q") or {}).items(), key=lambda x: qlabel_key(x[0])):
        L.append(f"STREET  {q}: pinned at {pin['eps']} ({pin.get('source', '')[:90]}) — the view is "
                 f"compared with this, the rest of the year splits what consensus leaves")
    for q, r in sorted((b.get("quarters") or {}).items(), key=lambda x: qlabel_key(x[0])):
        if r.get("kind") == "street":
            L.append(f"STREET  {q}: no view — takes what consensus leaves per quarter x the view's carry "
                     f"{b.get('street_ratio')} = {line} {r['eps']}")
    for fy, im in (b.get("implied") or {}).items():
        if im:
            L.append(f"IMPLIED {fy} consensus leaves {im['implied_per_q']}/qtr for {im['left']} quarters "
                     f"= {im['step_vs_last_pct']:+}% on {im['last_actual']} ({im['last_actual_eps']})")
    for name, sc in (b.get("scenarios") or {}).items():
        mu = b.get("multiple") or "P/E"
        js = " · ".join(f"{fy} {line} {j['eps']} vs {j['cons']} → juice {j['juice_pct']:+}%"
                        + (f" (TP {j['target_price']:,.0f})" if "target_price" in j else "")
                        + (f" ({mu} {j['pe_today']}→{j['pe_target']})" if j['pe_target'] != j['pe_today'] else "")
                        for fy, j in sc["juice"].items())
        L.append(f"SCEN    {name:5s} {js}")
    an = b.get("anchor")
    if an:
        mu = b.get("multiple") or "P/E"
        L.append(f"ANCHOR  {an['n']} houses' implied {an['fy']} {mu}: mean {an['mean']:.2f}x · median {an['median']:.2f}x "
                 f"(TP mean {an['tp_mean']:,.0f} · median {an['tp_median']:,.0f})")
        L += [f"        {h['house']:14s} TP {h['tp']:>9,.0f} ({h['date']}) → {h['implied']}x"
              + (f"   stated: {h['basis']}" if h.get("basis") else "")
              for h in sorted(an["houses"], key=lambda h: h["implied"])]
    if b.get("scenarios") and b.get("band"):
        bd = b["band"]
        L.append(f"        bear/bull = base moved by the {bd['key']} band: {band_text(bd)} "
                 f"per forecast quarter, carried into later years")
    if b.get("scenarios"):
        L.append(f"        confidence {b.get('confidence')} · horizon {b.get('horizon')} · "
                 f"headline {b.get('headline_fy')} · lean: the PM's to pick")
    for pc in b.get("pairs") or []:
        ke = pc["pair_juice_logged"]
        L.append(f"PAIR    {pc['pair']} ({pc['side']}) vs {', '.join(pc['partners'])}: partner juice "
                 f"{pc['partner_juice']} · pair juice if base {pc['pair_juice_if_base']} · Kelly edge "
                 f"(both leans) {str(ke) + '%' if ke is not None else 'none — a leg has no lean'}")
    if "deals" in b:
        if b["deals"] is None:
            L.append("DEALS   no deals.json yet — an IT case needs one (--scaffold creates it)")
        else:
            import it_deals
            L.append(it_deals.render(b["deals"]))
    L.append(f"NOTES   concall notes on file: {', '.join(b['concall_notes']) or 'none'}")
    L.append(f"LOG     {len(b['log'])} entries" + "".join(
        f"\n        {e['as_of']} {e['step']:6s} [{e['status']}] {e['note'][:110]}" for e in b["log"][-8:]))
    return "\n".join(L)


# --------------------------------------------------------------- scaffold
def agent_name(token: str) -> str:
    """The IMS ticker token, lowercased, + "-agent": tcs-agent, info-agent.
    PM, 2026-10-13: first "name it as TCS_agent", then "use tcs-agent" — the
    lowercase-hyphen form is also the one Claude Code agent names expect."""
    return f"{token.lower()}-agent"


def is_it(group: str | None) -> bool:
    return (group or "").upper().startswith("IT ")

AGENT_TEMPLATE = """---
name: {agent}
description: {name}'s case agent — works ONLY on {name} ({token}) for the Book tab's Analysis sub-tab - what's priced in, the quarter-by-quarter {earn} build, later quarters vs the street, base/bear/bull juice, the re-rating check{deals_desc} - and keeps {name}'s case file current. Use when the PM asks about {name}'s case, juice, expected return, {earn} build, or "run the {name} agent". Never sets a Kelly edge or a lean.
---

You are the case agent for **{name}** ({token}, entity `{entity}`), the
{side_word} leg in {pairs} ({group}). You work on this one stock and nothing
else. If asked about another name, say which agent owns it and stop.

**First, every run, read in this order:**

1. `.claude/skills/stock-case/SKILL.md` — the method and the rules. Follow it.
2. `data/analysis/cases/{entity}/case.md` — your own memory: thesis, scenarios,
   open questions, what you said last time. Pick up from it.
3. `python packages/book/case_brief.py {entity}` — today's facts from the
   store and the arithmetic on your build. Never do {earn} or {mult} arithmetic in
   your head; edit `build.json` and re-run the brief.

**What you may write:** `data/analysis/cases/{entity}/case.md` and
`build.json`, and Analysis-log entries for **{entity} only** via
`python packages/book/analysis.py --add '<json>'` with
`"via": "agent:{agent}"`.{it_block}{ev_block}

**What you never write:** a `juice_pct` lean (the PM's number — and since
2026-10-22 the Kelly edge IS long lean − short lean, so a lean sizes the book),
a call (`book_calls`), anything in `prices`,
`estimates` or a pillar table, or another stock's log.

**Sources:** the store, the vault's concall notes for {name}, broker notes the
PM hands you, public results filings. Do not sweep Outlook unless the PM asks.
When sources disagree on a number, record both and the one you used, never
pick silently.

End every run with a short report to the PM in continuous prose: what changed
since your last run, the base/bear/bull juice, and the questions you need the
PM to answer before a lean can be set.
"""

IT_BLOCK = """

**IT names carry a deal LIST** — `data/analysis/cases/{entity}/deals.json`,
printed by `python packages/book/it_deals.py {entity}` (the brief prints it
too): reported TCV per quarter, and each named deal with its value as
reported, when signed, status and a dated, sourced history. A list only — no
run-rates, no revenue bridge (PM, 2026-10-13). When the brief prints
CROSS-CHECK DUE (21 days before `next_result`), re-confirm every unfinished
deal's status before the print. The stock-case skill's "IT deals" section is
the method."""

EV_BLOCK = """

**{group} runs on EV/EBITDA, not P/E** (PM, 2026-10-25). The build is
quarterly consolidated EBITDA (Rs cr); a forecast quarter is two numbers,
volume (mt) and EBITDA/t (Rs), and the brief derives the EBITDA. Consensus
EBITDA per year, the share count and net debt are written into `build.json`
(no feed carries them), each with a source; re-strike them when a house
publishes or the company reports. The anchor multiple is the MEAN of the
houses' implied EV/EBITDA on the headline year (PM: "get latest valuations,
select mean"): list every post-result TP in `house_tps` and set the base
`"anchor": "mean"`. The brief computes each (TP x shares + net debt) /
consensus EBITDA and the mean, and names every TP in `anchor_why`. Never type
a multiple yourself. The stock-case skill's "EV/EBITDA sectors" section is the
method."""

CASE_TEMPLATE = """# {name} — case file

*Maintained by the `{agent}` agent. Rewritten each run; the Analysis
log in ims.db is the dated record, this file is the current thinking.*

**Leg:** {side_word} in {pairs} ({group}) · **Last run:** never

## Thesis
_Not yet written._

## What's priced in
_Run the brief._

## {earn} build
_See build.json._

## Scenarios and juice
_None yet._

## Re-rating read
_Not yet checked._

## Watch list and falsifiers
_None yet._

## Questions for the PM
_None yet._

## Run log
"""

BUILD_TEMPLATE = {"entity": None, "eps_basis": None, "shares_cr": None, "headline_fy": None,
                  "horizon": None, "confidence": None, "quarters": {}, "scenarios": {},
                  "conflicts": []}


def scaffold(ent: str, force: bool = False, agents_dir: pathlib.Path = AGENTS,
             cases_dir: pathlib.Path = CASES, today: dt.date | None = None) -> list[str]:
    """Create one stock's agent file and empty case folder. The case files are
    NEVER overwritten (they are the agent's memory); the agent file only with
    force, since its text is generated and carries nothing hand-written."""
    ros = {r["entity"]: r for r in analysis.roster()}
    if ent not in ros:
        raise SystemExit(f"{ent!r} is not one of the dictated legs")
    r = ros[ent]
    it = is_it(r["group"])
    evs = case_valuation(r["group"]) == "ev_ebitda"
    fill = {"earn": "EBITDA" if evs else "EPS", "mult": "EV/EBITDA" if evs else "P/E",
            "ev_block": EV_BLOCK.format(group=band_key(r["group"])) if evs else "","entity": ent, "name": r["name"], "token": r["token"], "group": r["group"],
            "side_word": r["side"], "pairs": ", ".join(r["pairs"]), "agent": agent_name(r["token"]),
            "it_block": IT_BLOCK.format(entity=ent, name=r["name"]) if it else "",
            "deals_desc": (" - and the deal list: reported TCV, named deals, what is delayed and "
                           "what has rolled off, cross-checked before each result") if it else ""}
    wrote = []
    agents_dir.mkdir(parents=True, exist_ok=True)
    af = agents_dir / f"{fill['agent']}.md"
    if force or not af.exists():
        af.write_text(AGENT_TEMPLATE.format(**fill), encoding="utf-8"); wrote.append(str(af))
    d = cases_dir / ent
    d.mkdir(parents=True, exist_ok=True)
    if not (d / "case.md").exists():
        (d / "case.md").write_text(CASE_TEMPLATE.format(**fill), encoding="utf-8"); wrote.append(str(d / "case.md"))
    if not (d / "build.json").exists():
        today = today or dt.date.today()
        b = dict(BUILD_TEMPLATE, entity=ent, headline_fy=analysis.fy_label(today, 1))
        if evs:
            b = {**b, "valuation": "ev_ebitda", "eps_basis": None,
                 "ebitda_basis": "consolidated, reported, ex one-offs", "net_debt_cr": None,
                 "net_debt_source": None, "consensus": {}}
            b.pop("eps_basis")
        (d / "build.json").write_text(json.dumps(b, indent=2), encoding="utf-8"); wrote.append(str(d / "build.json"))
    if it and not (d / "deals.json").exists():
        (d / "deals.json").write_text(json.dumps({"entity": ent, "currency": "USD m", "quarters": {},
                                                  "deals": []}, indent=2), encoding="utf-8")
        wrote.append(str(d / "deals.json"))
    return wrote


# --------------------------------------------------------------- selftest
def _selftest() -> None:
    import tempfile, os, shutil
    ok = 0
    # quarter labels, both sides of April, and a refusal
    assert quarter_of(dt.date(2026, 10, 11)) == "Q3FY27" and quarter_of(dt.date(2026, 4, 1)) == "Q1FY27"
    assert quarter_of(dt.date(2027, 3, 31)) == "Q4FY27"; ok += 1
    try:
        qlabel_key("2QFY27"); raise AssertionError("accepted 2QFY27")
    except ValueError:
        ok += 1

    build = {"entity": "x", "headline_fy": "FY28", "quarters": {
        "Q1FY27": {"eps": 38.0, "kind": "actual", "source": "filing"},
        "Q2FY27": {"eps": 38.4, "kind": "actual", "source": "filing"},
        "Q3FY27": {"eps": 39.0, "kind": "draft"}, "Q4FY27": {"eps": 39.6, "kind": "draft"}},
        "scenarios": {"base": {"why": "trend", "growth_pct": {"FY28": 5.0}},
                      "bear": {"why": "margin slips", "quarters": {"Q3FY27": 37.0, "Q4FY27": 37.0},
                               "growth_pct": {"FY28": 2.0}},
                      "bull": {"why": "re-rates", "anchor_pe": 15.0, "anchor_why": "5y median",
                               "growth_pct": {"FY28": 5.0}}}}
    assert validate_build(build) == []; ok += 1
    for lvl in ("low", "medium", "high"):                                   # acceptance, per the GLOB lesson
        assert validate_build({**build, "confidence": lvl}) == [], lvl
    ok += 1
    # rejections, each naming its reason
    for mut, why in [(lambda b: b["quarters"]["Q1FY27"].pop("source"), "needs a source"),
                     (lambda b: b["quarters"]["Q3FY27"].update(kind="guess"), "kind"),
                     (lambda b: b["scenarios"]["bear"]["quarters"].update(Q1FY28=1.0), "not in quarters"),
                     (lambda b: b["scenarios"]["bull"].pop("anchor_why"), "named anchor"),
                     (lambda b: b["scenarios"]["base"].update(why=" "), "needs words"),
                     (lambda b: b.update(confidence=4), "confidence")]:
        bb = json.loads(json.dumps(build)); mut(bb)
        probs = validate_build(bb)
        assert any(why in p for p in probs), (why, probs); ok += 1

    # FY sums and the implied rest of year
    s = fy_sums(build["quarters"])
    assert s["FY27"]["eps"] == 155.0 and s["FY27"]["complete"]; ok += 1
    im = implied_rest(build["quarters"], 154.0, "FY27")
    assert im["left"] == 2 and im["implied_per_q"] == 38.8 and im["step_vs_last_pct"] == 1.04; ok += 1
    assert implied_rest(build["quarters"], 154.0, "FY28") is None; ok += 1   # no actuals in FY28

    # juice: EPS gap at today's P/E; an anchor scales by anchor/today
    cons, pe = {"FY27": 154.0, "FY28": 161.0}, {"FY27": 14.0, "FY28": 13.4}
    sj = scenario_juice(build, cons, pe)
    b28 = sj["base"]["juice"]["FY28"]
    assert sj["base"]["fy_eps"]["FY28"] == 162.75 and b28["juice_pct"] == round((162.75 / 161 - 1) * 100, 2); ok += 1
    assert sj["bear"]["fy_eps"]["FY27"] == 150.4; ok += 1                   # overrides applied
    bull = sj["bull"]["juice"]["FY28"]
    assert bull["juice_pct"] == round((162.75 / 161 * 15 / 13.4 - 1) * 100, 2); ok += 1

    # drivers: EPS derived for a forecast row, never for an actual; a dict
    # override changes one driver and the EPS follows
    bd = {"shares_cr": 100.0, "quarters": {
        "Q1FY27": {"eps": 30.0, "kind": "actual", "source": "f", "revenue_cr": 1.0, "ebit_margin_pct": 1, "pat_to_ebit": 1},
        "Q2FY27": {"kind": "draft", "revenue_cr": 10000, "ebit_margin_pct": 25.0, "pat_to_ebit": 0.8}},
        "scenarios": {"bear": {"why": "m", "quarters": {"Q2FY27": {"ebit_margin_pct": 20.0}}}}}
    rq = resolve_quarters(bd)
    assert rq["Q1FY27"]["eps"] == 30.0 and rq["Q2FY27"]["eps"] == 20.0; ok += 1
    assert resolve_quarters(bd, bd["scenarios"]["bear"]["quarters"])["Q2FY27"]["eps"] == 16.0; ok += 1
    assert validate_build({**bd, "quarters": {"Q2FY27": {"kind": "draft", "revenue_cr": 1}}}) != []; ok += 1

    # case band (PM, 2026-10-18): bear/bull generated from the base. Acceptance
    # first — the arithmetic — then the refusals, per the GLOB lesson.
    cfg = {"case_bands": {"IT": {"margin_bps": 50, "growth_pp": 0.5}}}
    band = case_band("IT LARGECAP", cfg)
    assert band["key"] == "IT" and case_band("IT MIDCAP", cfg)["key"] == "IT" and case_band("STEEL", cfg) is None; ok += 1
    bb = {"entity": "x", "shares_cr": 100.0, "headline_fy": "FY28", "quarters": {
        "Q1FY27": {"eps": 30.0, "kind": "actual", "source": "f"},
        "Q2FY27": {"eps": 30.0, "kind": "actual", "source": "f"},
        "Q3FY27": {"kind": "pm", "revenue_cr": 10000, "ebit_margin_pct": 25.0, "pat_to_ebit": 0.8},
        "Q4FY27": {"kind": "draft", "revenue_cr": 10100, "ebit_margin_pct": 25.0, "pat_to_ebit": 0.8}},
        "scenarios": {"base": {"why": "pm", "growth_pct": {"FY28": 10.0}}}}
    assert validate_build(bb, band) == []; ok += 1
    sj = scenario_juice(bb, {"FY27": 100.0, "FY28": 110.0}, {"FY27": 15.0, "FY28": 14.0}, band)
    bq, lq, hq = (sj[k]["quarters"] for k in ("base", "bear", "bull"))
    assert lq["Q1FY27"] == bq["Q1FY27"] == hq["Q1FY27"] == 30.0; ok += 1          # actuals never move
    # Q3: one quarter of -0.5pp growth and -50bps; Q4: the level compounds twice
    assert lq["Q3FY27"] == round(9950 * 0.245 * 0.8 / 100, 2); ok += 1
    assert lq["Q4FY27"] == round(round(10100 * 0.995 ** 2, 1) * 0.245 * 0.8 / 100, 2); ok += 1
    # carried: FY28 moves by the forecast quarters' EPS ratio, NOT by growing a
    # FY27 that is half unmoved actuals
    ratio = (lq["Q3FY27"] + lq["Q4FY27"]) / (bq["Q3FY27"] + bq["Q4FY27"])
    assert sj["bear"]["fy_eps"]["FY28"] == round(sj["base"]["fy_eps"]["FY28"] * ratio, 2); ok += 1
    diluted = round(sj["bear"]["fy_eps"]["FY27"] * 1.10, 2)
    assert sj["bear"]["fy_eps"]["FY28"] < diluted; ok += 1                        # the carried move is the bigger one
    j = {k: sj[k]["juice"]["FY28"]["juice_pct"] for k in sj}
    assert j["bear"] < j["base"] < j["bull"] and sj["bear"]["band"] and not sj["base"]["band"]; ok += 1
    # a base anchor reaches bear and bull: same EPS as unanchored, every juice
    # scaled by anchor/today. Acceptance beside the no-anchor case above.
    ba = json.loads(json.dumps(bb)); ba["scenarios"]["base"].update(anchor_pe=12.6, anchor_why="broker median")
    assert validate_build(ba, band) == []; ok += 1
    sa = scenario_juice(ba, {"FY27": 100.0, "FY28": 110.0}, {"FY27": 15.0, "FY28": 14.0}, band)
    for k in ("base", "bear", "bull"):
        e = sj[k]["fy_eps"]["FY28"]
        assert sa[k]["fy_eps"]["FY28"] == e and sa[k]["anchor_why"] == "broker median", k
        assert sa[k]["juice"]["FY28"]["juice_pct"] == round((e / 110.0 * 12.6 / 14.0 - 1) * 100, 2), k
    ok += 1
    pe_b = {"entity": "x", "token": "X", "headline_fy": "FY28", "horizon": "h",
            "scenarios": {k: {"juice": {"FY28": sa[k]["juice"]["FY28"]}} for k in sa}}
    assert "anchor P/E 12.6x (today 14.0x)" in price_entry(pe_b)["note"]          # anchored: names the anchor
    pe_b["scenarios"] = {k: {"juice": {"FY28": sj[k]["juice"]["FY28"]}} for k in sj}
    assert "at today's P/E" in price_entry(pe_b)["note"]; ok += 1                  # unanchored: unchanged
    ba["scenarios"]["base"].pop("anchor_why")                                    # rejection: unnamed anchor
    assert any("named anchor" in p for p in validate_build(ba, band)); ok += 1
    for mut, why in [(lambda b: b["scenarios"].update(bull={"why": "story", "growth_pct": {"FY28": 9}}), "case band"),
                     (lambda b: b["quarters"]["Q4FY27"].pop("pat_to_ebit"), "needs revenue_cr"),
                     (lambda b: b["scenarios"].pop("base"), "needs a base")]:
        bx = json.loads(json.dumps(bb)); mut(bx)
        assert any(why in p for p in validate_build(bx, band)), (why, validate_build(bx, band)); ok += 1
    assert any("positive" in p for p in validate_build(bb, {"key": "IT", "margin_bps": 0, "growth_pp": 0.5})); ok += 1
    assert validate_build(build) == []; ok += 1                                  # no band: hand bear/bull still fine

    # street quarters (PM, 2026-10-18: "make Q4 street"). Consensus leaves
    # (99.6 - 60) / 2 = 19.8 a quarter; the PM's Q3 is 20.0, so the carry is
    # 20/19.8 and the street Q4 and FY28 re-base on it.
    bs = json.loads(json.dumps(bb))
    bs["quarters"]["Q4FY27"] = {"kind": "street"}
    bs["scenarios"]["base"] = {"why": "pm Q3, street after"}
    cs = {"FY27": 99.6, "FY28": 110.0}
    assert validate_build(bs, band) == []; ok += 1
    sj = scenario_juice(bs, cs, {"FY27": 15.0, "FY28": 14.0}, band)
    r0 = 20.0 / 19.8
    assert sj["base"]["quarters"]["Q4FY27"] == round(19.8 * r0, 2) == 20.0; ok += 1
    assert sj["base"]["fy_eps"]["FY28"] == round(110.0 * r0, 2); ok += 1          # carried, no growth pick
    rel = sj["bear"]["quarters"]["Q3FY27"] / 20.0
    assert sj["bear"]["quarters"]["Q4FY27"] == round(19.8 * r0 * rel, 2); ok += 1  # street follows the moved view
    assert sj["bear"]["fy_eps"]["FY28"] == round(sj["base"]["fy_eps"]["FY28"] * rel, 2); ok += 1
    # a view equal to the street gives zero juice everywhere — acceptance of the identity
    eq = json.loads(json.dumps(bs)); eq["quarters"]["Q3FY27"] = {"kind": "pm", "revenue_cr": 9900,
                                                                 "ebit_margin_pct": 25.0, "pat_to_ebit": 0.8}
    z = scenario_juice(eq, cs, {"FY27": 15.0, "FY28": 14.0})["base"]["juice"]
    assert z["FY27"]["juice_pct"] == 0.0 and z["FY28"]["juice_pct"] == 0.0; ok += 1

    # street_q (PM, 2026-10-21): a back-loaded street. The houses put Q3 at
    # 19.0 and leave 99.6 - 60 - 19.0 = 20.6 for Q4; the even split said 19.8
    # for both. The view (20.0) is compared with 19.0, Q4 takes 20.6 x carry.
    bp = json.loads(json.dumps(bs))
    bp["street_q"] = {"Q3FY27": {"eps": 19.0, "source": "median of three house models"}}
    assert validate_build(bp, band) == []; ok += 1
    sp = scenario_juice(bp, cs, {"FY27": 15.0, "FY28": 14.0}, band)
    rp = 20.0 / 19.0
    assert sp["base"]["quarters"]["Q4FY27"] == round(20.6 * rp, 2); ok += 1     # the rest after the pin, carried
    assert sp["base"]["fy_eps"]["FY28"] == round(110.0 * rp, 2); ok += 1
    relp = sp["bear"]["quarters"]["Q3FY27"] / 20.0
    assert sp["bear"]["quarters"]["Q4FY27"] == round(20.6 * rp * relp, 2); ok += 1  # band still follows the view
    # acceptance of the point of it: a view EQUAL to the pinned house Q3 is
    # zero juice, where the even split would have read it as -4% (19.0 / 19.8)
    eqp = json.loads(json.dumps(bp)); eqp["quarters"]["Q3FY27"] = {"kind": "pm", "revenue_cr": 9500,
                                                                   "ebit_margin_pct": 25.0, "pat_to_ebit": 0.8}
    zp = scenario_juice(eqp, cs, {"FY27": 15.0, "FY28": 14.0})["base"]["juice"]
    assert zp["FY27"]["juice_pct"] == 0.0 and zp["FY28"]["juice_pct"] == 0.0; ok += 1
    eqp.pop("street_q")
    assert scenario_juice(eqp, cs, {"FY27": 15.0, "FY28": 14.0})["base"]["juice"]["FY28"]["juice_pct"]         == round((19.0 / 19.8 - 1) * 100, 2); ok += 1                               # what the even split said
    # rejections, each naming its reason
    for mut, why in [(lambda b: b["street_q"]["Q3FY27"].pop("source"), "needs a source"),
                     (lambda b: b["street_q"].update(Q1FY27={"eps": 30.0, "source": "s"}), "has reported"),
                     (lambda b: b["street_q"].update(Q4FY27={"eps": 20.0, "source": "s"}), "pins every unreported"),
                     (lambda b: b["street_q"]["Q3FY27"].update(eps=0), "positive eps"),
                     (lambda b: b["street_q"].update(Q3FY2={"eps": 1.0, "source": "s"}), "street_q")]:
        bx = json.loads(json.dumps(bp)); mut(bx)
        assert any(why in p for p in validate_build(bx, band)), (why, validate_build(bx, band)); ok += 1
    # refusals: a street quarter carrying its own number, and the band still
    # demands drivers on a viewed quarter
    for mut, why in [(lambda b: b["quarters"]["Q4FY27"].update(eps=21.0), "takes consensus"),
                     (lambda b: b["quarters"]["Q3FY27"].pop("revenue_cr"), "needs revenue_cr")]:
        bx = json.loads(json.dumps(bs)); mut(bx)
        assert any(why in p for p in validate_build(bx, band)), why; ok += 1

    # EV/EBITDA (PM, 2026-10-25, cement). Acceptance first — the arithmetic —
    # then the refusals.
    assert resolve_row({"kind": "draft", "volume_mt": 41.3, "ebitda_per_t": 1214}, None, ev=True)["eps"] == 5013.8; ok += 1
    assert "eps" not in resolve_row({"kind": "actual", "volume_mt": 41.3, "ebitda_per_t": 1214}, None, ev=True); ok += 1
    evx = {"shares_cr": 30.0, "mcap_cr": 300000.0, "net_debt_cr": 20000.0}       # price 10,000
    c = 20000.0; m0 = (300000 + 20000) / c                                         # EV/EBITDA 16.0x
    z = ev_juice(c, c, m0, None, evx)
    assert z["juice_pct"] == 0.0 and z["target_price"] == 10000.0; ok += 1        # view = street: zero, at today's price
    up = ev_juice(c * 1.05, c, m0, None, evx)
    assert up["juice_pct"] == round(5 * 320000 / 300000, 2) == 5.33; ok += 1      # levered by EV / mcap, NOT the EBITDA gap
    # the anchor round trip: the implied multiple of a TP, applied to consensus,
    # gives back exactly that TP — so a mean of implied multiples targets the mean TP
    tp = 12000.0; mi = (tp * 30 + 20000) / c
    assert ev_juice(c, c, m0, mi, evx)["target_price"] == tp; ok += 1
    cfgv = {"case_valuation": {"CEMENT": "ev_ebitda"},
            "case_bands": {"CEMENT": {"per_t_inr": 50, "growth_pp": 1.0}}}
    assert case_valuation("CEMENT", cfgv) == "ev_ebitda" and case_valuation("IT LARGECAP", cfgv) == "pe"; ok += 1
    be = {"entity": "x", "valuation": "ev_ebitda", "shares_cr": 30.0, "net_debt_cr": 20000.0,
          "net_debt_source": "Sep-26 balance sheet", "headline_fy": "FY28",
          "consensus": {"FY27": {"ebitda_cr": 17000.0, "source": "mean of 3 houses"},
                        "FY28": {"ebitda_cr": 20000.0, "source": "mean of 3 houses"}},
          "quarters": {"Q1FY27": {"kind": "actual", "ebitda_cr": 5000.0, "source": "filing"},
                       "Q2FY27": {"kind": "actual", "ebitda_cr": 3700.0, "source": "filing"},
                       "Q3FY27": {"kind": "draft", "volume_mt": 40.0, "ebitda_per_t": 1050.0},
                       "Q4FY27": {"kind": "street"}},
          "scenarios": {"base": {"why": "Q3 drafted, Q4 street"}}}
    assert validate_build(be) == []; ok += 1
    bandc = case_band("CEMENT", cfgv)
    assert validate_build(be, bandc) == []; ok += 1
    sev = scenario_juice(be, {"FY27": 17000.0, "FY28": 20000.0}, {"FY27": 320000 / 17000, "FY28": m0}, bandc, evx)
    # street leaves (17000-8700)/2 = 4150 a quarter; the Q3 view is 40 x 1050 / 10 = 4200
    r0 = 4200 / 4150
    assert sev["base"]["quarters"]["Q4FY27"] == round(4150 * r0, 1); ok += 1
    assert sev["base"]["fy_eps"]["FY28"] == round(20000 * r0, 2); ok += 1          # carried, no growth picked
    jb28 = sev["base"]["juice"]["FY28"]
    assert jb28["juice_pct"] == round((m0 * 20000 * r0 - 20000) / 300000 * 100 - 100, 2); ok += 1
    lq = sev["bear"]["quarters"]["Q3FY27"]
    assert lq == round(40 * 0.99 * 1000 / 10, 1); ok += 1                         # -1pp volume, -Rs50/t
    jv = {k: sev[k]["juice"]["FY28"]["juice_pct"] for k in ("bear", "base", "bull")}
    assert jv["bear"] < jv["base"] < jv["bull"]; ok += 1
    eqv = json.loads(json.dumps(be)); eqv["quarters"]["Q3FY27"].update(volume_mt=41.5, ebitda_per_t=1000.0)
    ze = scenario_juice(eqv, {"FY27": 17000.0, "FY28": 20000.0}, {"FY27": 320000 / 17000, "FY28": m0}, None, evx)
    assert ze["base"]["juice"]["FY28"]["juice_pct"] == 0.0; ok += 1               # view = street (4150): zero
    pinv = json.loads(json.dumps(be)); pinv["street_q"] = {"Q3FY27": {"ebitda_cr": 4000.0, "source": "house median"}}
    assert validate_build(pinv) == []; ok += 1
    sp = scenario_juice(pinv, {"FY27": 17000.0, "FY28": 20000.0}, {"FY27": 320000 / 17000, "FY28": m0}, None, evx)
    assert sp["base"]["quarters"]["Q4FY27"] == round((17000 - 8700 - 4000) * 4200 / 4000, 1); ok += 1
    anc = json.loads(json.dumps(be)); anc["scenarios"]["base"].update(anchor_multiple=mi, anchor_why="mean of TPs")
    assert validate_build(anc) == []; ok += 1
    sa = scenario_juice(anc, {"FY27": 17000.0, "FY28": 20000.0}, {"FY27": 320000 / 17000, "FY28": m0}, bandc, evx)
    assert sa["bull"]["juice"]["FY28"]["pe_target"] == round(mi, 2); ok += 1      # the band carries the anchor
    pe_ev = {"entity": "x", "token": "X", "headline_fy": "FY28", "horizon": "h", "multiple": "EV/EBITDA",
             "scenarios": {k: {"juice": {"FY28": sa[k]["juice"]["FY28"]}} for k in sa}}
    assert "anchor EV/EBITDA" in price_entry(pe_ev)["note"]; ok += 1
    pe_ev["band"] = bandc                                                         # an EV band prints in its own words
    assert "EBITDA/t ±Rs50, volume growth ±1.0pp" in price_entry(pe_ev)["note"]; ok += 1
    assert band_text({"margin_bps": 50, "growth_pp": 0.5}) == "margin ±50bps, growth ±0.5pp"; ok += 1
    for mut, why in [(lambda b: b.pop("net_debt_source"), "net_debt_source"),
                     (lambda b: b.pop("net_debt_cr"), "net_debt_cr"),
                     (lambda b: b["consensus"]["FY28"].pop("source"), "needs a source"),
                     (lambda b: b["consensus"].pop("FY28"), "headline FY28 has no consensus"),
                     (lambda b: b["scenarios"]["base"].update(anchor_pe=15.0, anchor_why="x"), "anchor_multiple"),
                     (lambda b: b["quarters"]["Q1FY27"].update(eps=5000.0), "carry ebitda_cr, not eps"),
                     (lambda b: b["scenarios"]["base"].update(anchor_multiple=15.0), "named anchor"),
                     (lambda b: b.update(valuation="ev_sales"), "valuation"),
                     (lambda b: b["quarters"]["Q3FY27"].pop("ebitda_per_t"), "ebitda_cr must be a number")]:
        bx = json.loads(json.dumps(be)); mut(bx)
        assert any(why in p for p in validate_build(bx)), (why, validate_build(bx)); ok += 1
    bx = json.loads(json.dumps(be)); bx["quarters"]["Q3FY27"] = {"kind": "draft", "ebitda_cr": 4200.0}
    assert any("volume_mt" in p for p in validate_build(bx, bandc)); ok += 1        # the band needs drivers
    assert any("per_t_inr" in p for p in validate_build(be, {"key": "CEMENT", "margin_bps": 50, "growth_pp": 1})); ok += 1

    # house-TP anchor: implied multiples, mean/median, and the exact round trip
    tps = [{"house": h, "tp": t, "date": "2026-10-20", "source": "note"} for h, t in
           (("A", 9000.0), ("B", 12000.0), ("C", 15000.0), ("D", 13000.0))]
    ta = json.loads(json.dumps(be)); ta["house_tps"] = tps; ta["scenarios"]["base"]["anchor"] = "mean"
    assert validate_build(ta) == []; ok += 1
    an = implied_anchor(ta, {"FY28": 20000.0}, evx, "FY28")
    assert an["houses"][0]["implied"] == round((9000 * 30 + 20000) / 20000, 2) and an["tp_mean"] == 12250.0; ok += 1
    assert abs(an["median"] - st.median([h["implied"] for h in an["houses"]])) < 0.01; ok += 1
    tb = apply_anchor(internal(ta), an)
    assert tb["scenarios"]["base"]["anchor_pe"] == an["mean"] and "C 15,000 (2026-10-20)" in tb["scenarios"]["base"]["anchor_why"]; ok += 1
    eqa = json.loads(json.dumps(tb)); eqa["quarters"]["Q3FY27"].update(volume_mt=41.5, ebitda_per_t=1000.0)
    tj = scenario_juice(eqa, {"FY27": 17000.0, "FY28": 20000.0}, {"FY27": 320000 / 17000, "FY28": m0}, None, evx)
    assert abs(tj["base"]["juice"]["FY28"]["target_price"] - 12250.0) < 1.0; ok += 1   # view = street -> the mean TP
    pa = implied_anchor({"house_tps": tps}, {"FY28": 100.0}, None, "FY28")            # P/E form: TP / EPS
    assert pa["houses"][1]["implied"] == 120.0; ok += 1
    for mut, why in [(lambda b: b["scenarios"]["base"].update(anchor="mode"), "not in"),
                     (lambda b: b.pop("house_tps"), "needs house_tps"),
                     (lambda b: b["house_tps"][0].pop("date"), "ISO date"),
                     (lambda b: b["house_tps"][1].pop("source"), "needs a source"),
                     (lambda b: b["scenarios"]["base"].update(anchor_multiple=15.0, anchor_why="x"), "keep one")]:
        bx = json.loads(json.dumps(ta)); mut(bx)
        assert any(why in p for p in validate_build(bx)), (why, validate_build(bx)); ok += 1

    # price entry carries the range, never a lean
    pe_doc = price_entry({"entity": "x", "token": "X", "headline_fy": "FY28", "horizon": "13w", "scenarios": sj})
    assert "juice_pct" not in pe_doc["data"] and pe_doc["data"]["juice_low"] == sj["bear"]["juice"]["FY28"]["juice_pct"]; ok += 1

    # rerate split is exact in logs, on a fixture store
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
    try:
        conn = analysis.connect(tmp.name)
        conn.executescript("""
          CREATE TABLE entities (id TEXT PRIMARY KEY, sector TEXT);
          CREATE TABLE prices (entity_id TEXT, date TEXT, close REAL);
          CREATE TABLE estimates (entity_id TEXT, broker TEXT, period TEXT, metric TEXT,
                                  as_of TEXT, value_num REAL);""")
        for i, e in enumerate(["tcs", "infosys", "wipro", "hcl_tech"]):
            conn.execute("INSERT INTO entities VALUES (?, 'it')", (e,))
            conn.execute("INSERT INTO prices VALUES (?, '2026-07-13', ?)", (e, 100.0 + i))
            conn.execute("INSERT INTO prices VALUES (?, '2026-10-09', ?)", (e, 110.0 + i))
            conn.execute("INSERT INTO estimates VALUES (?, 'consensus_yahoo', 'FY28', 'eps', '2026-10-11', ?)", (e, 10.5))
            conn.execute("INSERT INTO estimates VALUES (?, 'consensus_yahoo', 'FY28', 'eps_90d_ago', '2026-10-11', ?)", (e, 10.0))
        conn.commit()
        r = rerate(conn, "tcs", "FY28")
        assert r["start"] == "2026-07-13" and r["price_pct"] == 10.0 and r["eps_pct"] == 5.0
        assert abs((1 + r["eps_pct"] / 100) * (1 + r["pe_pct"] / 100) - 1.10) < 1e-3; ok += 1
        assert r["rel"]["start"]["rel_pe"] is not None and r["rel"]["end"]["n_peers"] == 4; ok += 1
        assert rerate(conn, "nobody", "FY28") is None; ok += 1
        conn.close()
    finally:
        os.unlink(tmp.name)

    # scaffold: creates, then never overwrites the case files
    d = pathlib.Path(tempfile.mkdtemp())
    try:
        w = scaffold("tcs", agents_dir=d / "agents", cases_dir=d / "cases", today=dt.date(2026, 10, 11))
        assert len(w) == 4 and "name: tcs-agent" in (d / "agents" / "tcs-agent.md").read_text(encoding="utf-8")
        assert (d / "cases" / "tcs" / "deals.json").exists(); ok += 1   # IT leg gets a deal ledger
        (d / "cases" / "tcs" / "case.md").write_text("MY MEMORY", encoding="utf-8")
        scaffold("tcs", force=True, agents_dir=d / "agents", cases_dir=d / "cases")
        assert (d / "cases" / "tcs" / "case.md").read_text(encoding="utf-8") == "MY MEMORY"; ok += 1
        live = AGENTS / "tcs-agent.md"                     # the EV parametrisation left P/E text alone
        if live.exists():
            assert (d / "agents" / "tcs-agent.md").read_text(encoding="utf-8") == live.read_text(encoding="utf-8"); ok += 1
        scaffold("ultratech", agents_dir=d / "agents", cases_dir=d / "cases", today=dt.date(2026, 10, 25))
        ub = json.loads((d / "cases" / "ultratech" / "build.json").read_text(encoding="utf-8"))
        ua = (d / "agents" / "utcem-agent.md").read_text(encoding="utf-8")
        assert ub["valuation"] == "ev_ebitda" and "consensus" in ub and "eps_basis" not in ub
        assert "EV/EBITDA" in ua and "EPS" not in ua and not (d / "cases" / "ultratech" / "deals.json").exists(); ok += 1
        try:
            scaffold("not_a_leg", agents_dir=d / "agents", cases_dir=d / "cases"); raise AssertionError
        except SystemExit:
            ok += 1
    finally:
        shutil.rmtree(d)
    print(f"case_brief selftest: {ok} checks passed")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("entity", nargs="?")
    ap.add_argument("--json", action="store_true", help="also write cases/<entity>/brief.json")
    ap.add_argument("--price-entry", action="store_true", help="print the analysis.py --add payload")
    ap.add_argument("--scaffold", metavar="ENTITY", help="create the agent file and case folder")
    ap.add_argument("--force", action="store_true", help="with --scaffold: regenerate the agent file")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    if a.scaffold:
        for w in scaffold(a.scaffold, force=a.force):
            print("wrote", w)
        return
    if not a.entity:
        ap.error("entity required")
    b = brief(a.entity)
    if a.price_entry:
        print(json.dumps(price_entry(b)))
        return
    print(render(b))
    if a.json:
        f = CASES / a.entity / "brief.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(b, indent=2, default=str), encoding="utf-8")
        print("wrote", f)


if __name__ == "__main__":
    main()
