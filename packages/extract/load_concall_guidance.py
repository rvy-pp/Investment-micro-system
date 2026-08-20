"""Load P4 commitments and actuals from the vault's concall notes.

Hand-transcribed from `AI Insights/<TICKER>_Concall/Concall - <TICKER> - Q1 FY27.md`
per the `concall-ingest` skill. Every row carries the verbatim quote and the
CALL DATE, never the quarter end — Q1 covers Apr-Jun and is knowable in late
July, so dating it 30 June would assert three weeks of foresight.

WHAT WAS DELIBERATELY LEFT OUT, since a commitment absent from the store is
better than one that scores the wrong thing:

  - Novelis-specific targets (leverage, working capital, cost savings). Novelis
    is a reporting_unit with no peer_group, so it is never scored; loading its
    guidance under `hindalco` would mix a subsidiary's promises into the parent's
    scorecard. The concall itself says to "score the two books separately".
  - VEDL's Zinc India lines (COP $975-1,000, refined metal, silver). These ARE
    hindustan_zinc's commitments and are already loaded there. Duplicating them
    under vedanta would double-count the same promise in a holdco whose
    economics are already HZL x 0.634.
  - Anything without a number: "evaluating", "on drawing board", "similar to
    Q1". The CHECK constraint on target_type would reject them, correctly.
  - NALCO's Q1 metal and alumina PRODUCTION. The transcript states neither. It
    gives FY26 actuals (4.72 lakh t metal, 2.3 MT alumina) and FY27 targets, and
    calls Q1 hydrate output "best ever" without a number. I initially loaded
    119,000 t and 575,000 t DERIVED from those run-rates — a fabricated actual
    wearing a citation. Both removed. Those commitments now withhold, which is
    the correct output: the company did not disclose the quarter.
  - Balance-sheet ratios (net debt/EBITDA). Real commitments, but `net_debt`
    polarity in guidance_runrate refers to an absolute level, and a RATIO needs
    its own metric and denominator handling. Left for when that is built rather
    than mapped onto the wrong metric.

Usage:
    python packages/extract/load_concall_guidance.py            # dry
    python packages/extract/load_concall_guidance.py --write
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
NOTE = "AI Insights/{t}_Concall/Concall - {t} - Q1 FY27.md"

# (source_id, ticker, entity_id, call_date)
CALLS = [
    ("concall-hindalco-q1fy27", "HINDALCO", "hindalco", "2026-08-07"),
    ("concall-vedantalum-q1fy27", "VEDANTALUM", "vaml", "2026-07-30"),
    ("concall-vedl-q1fy27", "VEDL", "vedanta", "2026-07-30"),
    ("concall-nationalum-q1fy27", "NATIONALUM", "nalco", "2026-08-03"),
]

# entity, metric, period, type, value/lo/hi, unit, quote
GUIDANCE = [
    # ---- HINDALCO ----------------------------------------------------------
    ("hindalco", "volume", "FY27", "range", None, 700000.0, 800000.0, "t alumina",
     "Alumina external sales run-rate 700-800 Kt/yr"),
    ("hindalco", "ebitda_per_t", "FY27", "point", 250.0, None, None, "USD/t",
     "Downstream aluminium: ~$250/t for the remaining quarters of FY27 "
     "(long-term \"over $300/t\")"),
    # ---- VAML --------------------------------------------------------------
    ("vaml", "cost_per_t", "FY27", "range", None, 1650.0, 1700.0, "USD/t",
     "Hot-metal COP: in-band $1,650-1,700/t FY27; -$175-200/t over 3-4 quarters "
     "(~70% alumina+bauxite, ~30% coal)"),
    ("vaml", "volume", "FY27", "range", None, 2600000.0, 2700000.0, "t aluminium",
     "Aluminium production FY27 2.6-2.7 Mnt (~650+ kt/qtr)"),
    ("vaml", "alumina_volume", "FY27", "range", None, 4000000.0, 4100000.0, "t alumina",
     "Alumina production 4.0-4.1 Mnt FY27"),
    # ---- NALCO -------------------------------------------------------------
    # Note written 2026-08-20 from the Q1 FY27 transcript (no note existed), per
    # step 2 of the concall-ingest skill.
    ("nalco", "volume", "FY27", "range", None, 476000.0, 477000.0, "t aluminium",
     "Metal production FY27: 4.76-4.77 lakh t (FY26: 4.72); running 958-959 pots"),
    ("nalco", "alumina_volume", "FY27", "point", 2500000.0, None, None, "t alumina",
     "Alumina production FY27 ~25 lakh t (23 existing + 2 from 5th Stream); "
     "reaffirmed despite the 5th Stream slip"),
    ("nalco", "cost_per_t", "FY27", "point", 1600000.0, None, None, "INR/t",
     "Q4 FY26: \"Aluminium CoP to stay within Rs.1,60,000/ton despite input cost "
     "inflation (captive coal + employee savings offsetting)\". Still the standing "
     "FY27 commitment — Q1 FY27 came in at INR1,70,000/t and Q2 is guided "
     "INR1,70,000-1,72,000, and the prior commitment was never referenced."),
    ("nalco", "coal_volume", "FY27", "point", 4800000.0, None, None, "t coal",
     "Captive coal FY27: 4.8 MT; EC from MoEFCC in 2-3 months. \"4.8 million "
     "tons, we are sure\" — against Q1's 8.80 lakh t, which annualises ~3.5 MT"),

    # ---- VEDANTA (group-only lines; Zinc India belongs to hindustan_zinc) ---
    ("vedanta", "volume", "FY27", "range", None, 280000.0, 300000.0, "t MIC",
     "Zinc International: Gamsberg Ph2 starts August 2026; 450 ktpa total MIC "
     "capacity; FY27 280-300 kt (vs 185 kt FY26)"),
]

# entity, metric, period, value, unit, quote
ACTUALS = [
    ("hindalco", "volume", "Q1FY27", 138000.0, "t alumina",
     "Alumina external sales run-rate 700-800 Kt/yr -> 138 Kt in Q1 (one shipment "
     "missed); ~190 Kt guided for Q2. Verdict: Partial - annualised run-rate "
     "slipping vs the 700-800 Kt promise"),
    ("hindalco", "ebitda_per_t", "Q1FY27", 303.0, "USD/t",
     "Downstream India targeting $300/t EBITDA -> $303/t ($298 cr, +30% YoY). "
     "Verdict: Delivered - though immediately guided back to ~$250/t for the rest "
     "of FY27"),
    ("vaml", "cost_per_t", "Q1FY27", 1698.0, "USD/t",
     "Aluminium COP FY27 $1,650-1,700/t -> $1,698/t; Q2 \"marginally higher\" on "
     "monsoon shutdowns. Verdict: Delivered (in band)"),
    ("vaml", "volume", "Q1FY27", 632000.0, "t aluminium",
     "Aluminium production FY27 2.6-2.7 Mnt -> 632 kt (+5% YoY, +3% QoQ); "
     "H2-weighted plan intact. Verdict: On track (slightly below run-rate, seasonal)"),
    ("vaml", "alumina_volume", "Q1FY27", 826000.0, "t alumina",
     "Alumina production 4.0-4.1 Mnt FY27 -> 826 kt (-6% QoQ on power/red-mud/"
     "bauxite stabilisation). Verdict: On track (behind ask-rate, H2 catch-up guided)"),
    ("nalco", "cost_per_t", "Q1FY27", 1700000.0, "INR/t",
     "Last year average INR1,56,000-1,57,000; this year Q1 average around "
     "INR1,70,000. Raw material +INR15,000-16,000/t from caustic soda, CP coke "
     "and HFO."),
    ("nalco", "coal_volume", "Q1FY27", 880000.0, "t coal",
     "Q1 was 8.80 lakh tons. Initially 4, 5 days in the beginning of Q1 the "
     "production from the mines were not there; there were some technical issues."),
    ("vedanta", "volume", "Q1FY27", 45000.0, "t MIC",
     "Gamsberg Phase 1 - sustain 100% utilisation -> Ph1 +10% QoQ to 45 kt; "
     "COP -7% to $1,549/t. Verdict: Delivered"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys=ON")
    by_entity = {e: (sid, d) for sid, _t, e, d in CALLS}

    print(f"{len(CALLS)} calls, {len(GUIDANCE)} commitments, {len(ACTUALS)} actuals\n")
    for sid, t, eid, cd in CALLS:
        print(f"  {eid:16} {cd}  {NOTE.format(t=t)}")
        if a.write:
            conn.execute("INSERT OR IGNORE INTO sources (id,kind,source_date,"
                         "captured_at,raw_path) VALUES (?,'concall',?,?,?)",
                         (sid, cd, now, NOTE.format(t=t)))

    print("\n  COMMITMENTS")
    for eid, metric, period, ttype, val, lo, hi, unit, quote in GUIDANCE:
        sid, cd = by_entity[eid]
        tgt = (f"{val:g}" if ttype == "point" else f"{lo:g}-{hi:g}")
        print(f"    {eid:16} {metric:16} {period:6} {ttype:9} {tgt:>19} {unit}")
        if a.write:
            conn.execute(
                "INSERT INTO guidance (entity_id,source_id,issued_date,period,metric,"
                "target_type,target_value,target_low,target_high,unit,quote,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, sid, cd, period, metric, ttype, val, lo, hi, unit, quote, now))

    print("\n  ACTUALS")
    for eid, metric, period, val, unit, quote in ACTUALS:
        sid, cd = by_entity[eid]
        print(f"    {eid:16} {metric:16} {period:6} {val:>19,.0f} {unit}   as_of {cd}")
        if a.write:
            conn.execute(
                "INSERT INTO observations (source_id,entity_id,as_of,factor,metric,"
                "value_num,unit,period,direction,confidence,quote,extractor_version,"
                "created_at) VALUES (?,?,?,'actual',?,?,?,?,1,0.9,?,'concall-1',?)",
                (sid, eid, cd, metric, val, unit, period, quote, now))

    if a.write:
        conn.commit()
        print("\nwritten")
    else:
        print("\nDRY RUN — nothing written. Re-run with --write.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
