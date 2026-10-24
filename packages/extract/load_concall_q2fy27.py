"""Load P4 commitments and actuals from the Q2 FY27 concall notes: Infosys,
Coforge, Shree Cement, JSW Steel, Jindal Steel — plus the Q1 FY27 commitments
those notes grade.

Hand-transcribed per the `concall-ingest` skill from
`AI Insights/<TICKER>_Concall/Concall - <TICKER> - Q<n> FY27.md`. Every row
carries a quote and the CALL DATE as its date, never the quarter end. Q1 rows
take quotes from the Q1 note; Q2 rows quote the transcript, as the Q2 note does.

None of the three had a guidance row before this. The Q1 commitments are loaded
here for the first time, those the Q2 call settled are loaded RESOLVED (met /
missed, resolved on the Q2 call date), and the rest stay open.

STEEL (added the same day). Coking-coal guides are QoQ CHANGES in USD/t and go in
`cost_per_t` with unit "USD/t coking coal, qoq", Tata's convention, so
guidance_forward's OBSERVABLE map reads them against coking_coal_spot_aus. JSW's
Q1 call date is not in its note ("exact date not stated"); 2026-07-19, the day it
was analysed, is the latest the call can have been, so no row claims foresight.
JSW's FY27 sales 28.6 MT comes from the Q4 FY26 call (14 May 2026): it was
never loaded and is still live. Its scope includes BMM Ispat, while the reported
consolidated sales it is graded against do not, so the run-rate reads slightly
low. Jindal's FY27 10.5-11 MT is already stored (id 35) and is not duplicated.
Existing row id 31 (jsw, FY27 cost_per_t 12-15, missed) is left alone. Its quote
fits either the Q4-call guide for Q1 or the Q1-call guide for Q2, and only the
latter is loaded here, under Q2FY27.

WHO THIS SCORES. Shree, JSW and Jindal. Infosys and Coforge are IT, which has no specs and
no peer_group, so run_scores never reaches them (CLAUDE.md: "IT still scores
NOTHING"). Their rows are a delivery record for v_guidance_track_record and the
Results tab, nothing more.

RULES APPLIED, so the next quarter's load makes the same calls:

  - A RAISED guide supersedes the lower one: Coforge's Q1 "15.5% or higher" is
    loaded `withdrawn` on the day the 16% replaced it. Leaving both open would
    run-rate the same margin twice.
  - A CUT guide does NOT supersede: Infosys' Q1 1.5-3.0% stays open beside the
    Q2 1.0-2.0%, and both grade against the FY27 print. Withdrawing the original
    would let a cut launder the first promise (the Avalon ledger rule).
  - A restatement is not a new commitment: Shree's "40-41 MT" on the Q2 call
    restates Q1's 40 MT, so only the Q1 row is loaded. Same for Infosys' OPM
    20-22%.
  - Hard commitments only. Shree's "Q2 profitability better than Q1" was in the
    Q1 note's Soft list and is not loaded, though the Q2 note grades it missed.

LEFT OUT, and why:

  - Capex (Shree's Rs1,500 cr FY27). guidance_runrate annualises only volumes;
    every other metric is AVERAGED across reported quarters. A capex actual would
    be read as a rate, so H1's Rs895 cr against Rs1,500 cr would score 40%
    "under budget". The commitment is not loaded until capex is cumulative there.
  - Coforge EBITDA 20.5-21%. The call states H1 20.5%, not Q2. A derived Q2
    would be a fabricated actual (the NALCO lesson in load_concall_guidance.py),
    and an H1 row would be averaged with Q1 as if it were a quarter.
  - Coforge FCF/PAT, Infosys freshers, dividend, tax rates, Shree UAE volume and
    premium share: no polarity, or no stated period, or not a target.
  - Revenue growth carries no polarity on purpose (see guidance_runrate.POLARITY);
    the growth commitments are stored and refused rather than mis-scored.

Usage:
    python packages/extract/load_concall_q2fy27.py            # dry
    python packages/extract/load_concall_q2fy27.py --write
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
NOTE = "AI Insights/{t}_Concall/Concall - {t} - {q}.md"

# source_id -> (ticker, quarter label, entity_id, call_date)
CALLS = {
    "concall-infy-q1fy27":     ("INFY", "Q1 FY27", "infosys", "2026-07-23"),
    "concall-infy-q2fy27":     ("INFY", "Q2 FY27", "infosys", "2026-10-23"),
    "concall-coforge-q1fy27":  ("COFORGE", "Q1 FY27", "coforge", "2026-07-28"),
    "concall-coforge-q2fy27":  ("COFORGE", "Q2 FY27", "coforge", "2026-10-23"),
    "concall-shreecem-q1fy27": ("SHREECEM", "Q1 FY27", "shree", "2026-07-31"),
    "concall-shreecem-q2fy27": ("SHREECEM", "Q2 FY27", "shree", "2026-10-23"),
    "concall-jswsteel-q4fy26":  ("JSWSTEEL", "Q4 FY26", "jsw_steel", "2026-05-14"),
    "concall-jswsteel-q1fy27":  ("JSWSTEEL", "Q1 FY27", "jsw_steel", "2026-07-19"),
    "concall-jswsteel-q2fy27":  ("JSWSTEEL", "Q2 FY27", "jsw_steel", "2026-10-23"),
    "concall-jindalstel-q1fy27": ("JINDALSTEL", "Q1 FY27", "jindal_steel", "2026-07-25"),
    "concall-jindalstel-q2fy27": ("JINDALSTEL", "Q2 FY27", "jindal_steel", "2026-10-23"),
}
Q2_CALL = "2026-10-23"

# source, metric, period, type, value, lo, hi, dir, unit, quote,
#   status, resolved_date, actual_value
GUIDANCE = [
    # ---- INFOSYS -----------------------------------------------------------
    ("concall-infy-q1fy27", "revenue_growth_cc_yoy", "FY27", "range", None, 1.5, 3.0,
     None, "% CC YoY",
     "FY27 revenue guidance 1.5%–3.0% CC YoY (revised down from 1.5%–3.5%).",
     "open", None, None),
    ("concall-infy-q1fy27", "ebit_margin_pct", "FY27", "range", None, 20.0, 22.0,
     None, "%", "FY27 OPM 20%–22% (held); Q1 21.1%.",
     "open", None, None),
    ("concall-infy-q2fy27", "revenue_growth_cc_yoy", "FY27", "range", None, 1.0, 2.0,
     None, "% CC YoY",
     "Based on our Q2 performance and revised outlook for rest of the year, our "
     "revenue growth guidance is 1%-2% year-on-year in constant currency terms.",
     "open", None, None),
    # ---- COFORGE -----------------------------------------------------------
    ("concall-coforge-q1fy27", "ebit_margin_pct", "FY27", "point", 15.5, None, None,
     None, "% consolidated",
     "FY27 guidance unchanged: consolidated EBITDA 20.5–21%; EBIT 16.5–17% "
     "standalone, \"15.5% or higher\" consolidated; FCF/PAT >100%.",
     "withdrawn", Q2_CALL, None),
    ("concall-coforge-q1fy27", "large_deals", "Q2FY27", "direction", None, None, None,
     "up", "count",
     "Q2 large-deal signings to be a record — \"very close to the full-year total "
     "from three years back.\"",
     "met", Q2_CALL, 8.0),
    ("concall-coforge-q1fy27", "exceptional_cost", "FY27", "point", 15.0, None, None,
     None, "USD mn",
     "Acquisition/integration costs within the $15M budget ($6.5M Q1 + $5.4M Q4); "
     "marginal in Q2, nil from Q3.",
     "missed", Q2_CALL, 17.0),
    ("concall-coforge-q1fy27", "ga_pct", "Q2FY27", "direction", None, None, None,
     "down", "% of revenue",
     "Encora Q1: 20.3% EBITDA / 19.1% EBIT; G&A cut 40%, combined G&A 6.6% (from "
     "Coforge 6.7% + Encora 10%), further improvement in Q2.",
     "met", Q2_CALL, 6.3),
    ("concall-coforge-q2fy27", "ebit_margin_pct", "FY27", "point", 16.0, None, None,
     None, "% consolidated",
     "We are pleased to raise our consolidated EBIT margin guidance for fiscal year "
     "2027 to 16%, from the 15.5% we had shared at the beginning of the year.",
     "open", None, None),
    ("concall-coforge-q2fy27", "ebit_margin_pct", "Q4FY27", "point", 17.0, None, None,
     None, "% consolidated",
     "We also plan to achieve a reported Q4 EBIT of 17%, thereby establishing the "
     "glide path to further and significant margin expansion in fiscal year 2028 "
     "as well.",
     "open", None, None),
    ("concall-coforge-q2fy27", "ebit_margin_pct", "FY28", "point", 17.0, None, None,
     None, "% consolidated",
     "If we exit at 17%, the least we should deliver next year should be 17%.",
     "open", None, None),
    ("concall-coforge-q2fy27", "eps", "Q4FY27", "range", None, 19.0, 20.0,
     None, "INR",
     "I think the EPS should be anywhere touching around INR 19-INR 20 in Q4.",
     "open", None, None),
    ("concall-coforge-q2fy27", "large_deals", "Q3FY27", "point", 8.0, None, None,
     None, "count",
     "In Q3, which has now started, which incidentally is the shortest quarter of "
     "the year, we hope to again sign eight, if not more, large deals.",
     "open", None, None),
    # ---- SHREE CEMENT ------------------------------------------------------
    ("concall-shreecem-q1fy27", "volume", "FY27", "point", 40_000_000.0, None, None,
     None, "t India sales",
     "FY27 volume 40 MT (India) maintained; H1 target 19.5–20 MT (10.4–10.5 done "
     "in Q1, 9–9.4 guided for Q2); July already 3.1 MT",
     "open", None, None),
    ("concall-shreecem-q1fy27", "volume", "Q2FY27", "range", None, 9_000_000.0,
     9_400_000.0, None, "t India sales",
     "FY27 volume 40 MT (India) maintained; H1 target 19.5–20 MT (10.4–10.5 done "
     "in Q1, 9–9.4 guided for Q2); July already 3.1 MT",
     "met", Q2_CALL, 9_310_000.0),
    ("concall-shreecem-q1fy27", "fuel_cost", "Q2FY27", "point", 1.95, None, None,
     None, "INR/'000 kcal",
     "Fuel cost to stay ~₹1.95/kcal (\"may move 2–3 paisa\"), i.e. has peaked — "
     "should ease as contracted petcoke arrives",
     "missed", Q2_CALL, 2.09),
    ("concall-shreecem-q1fy27", "capacity", "Q3FY27", "point", 7_000_000.0, None,
     None, None, "t UAE cement",
     "UAE capacity to 7 MT by Q3 FY27 (doubling of Ras Al Khaimah)",
     "met", Q2_CALL, 7_000_000.0),
    ("concall-shreecem-q2fy27", "fuel_cost", "Q3FY27", "range", None, 2.15, 2.20,
     None, "INR/'000 kcal",
     "For the coming quarter, we believe this will be around INR 2.15-INR 2.20.",
     "open", None, None),
    # ---- JSW STEEL ---------------------------------------------------------
    ("concall-jswsteel-q4fy26", "volume", "FY27", "point", 28_600_000.0, None, None,
     None, "t consol sales",
     "FY27 production: 29.75 MT; sales: 28.6 MT — excluding BPSL JV, including "
     "BMM Ispat; like-to-like growth of ~13% production and ~10% sales",
     "open", None, None),
    ("concall-jswsteel-q1fy27", "volume", "Q2FY27", "direction", None, None, None,
     "up", "t consol sales",
     "Q2 volumes higher QoQ — BF-3 ramp (already ~80%) + Ohio recovery",
     "met", Q2_CALL, 7_460_000.0),
    ("concall-jswsteel-q1fy27", "cost_per_t", "Q2FY27", "range", None, 12.0, 15.0,
     None, "USD/t coking coal, qoq",
     "Q2 coking coal +$12–15/t; recent coal softening reflects in Q3; iron ore "
     "relief late-Q2 and Q3",
     "met", Q2_CALL, 10.0),
    ("concall-jswsteel-q1fy27", "cost_per_t", "Q3FY27", "direction", None, None, None,
     "down", "USD/t coking coal, qoq",
     "Q2 coking coal +$12–15/t; recent coal softening reflects in Q3; iron ore "
     "relief late-Q2 and Q3",
     "open", None, None),
    ("concall-jswsteel-q2fy27", "cost_per_t", "Q3FY27", "point", 25.0, None, None,
     None, "USD/t coking coal, qoq",
     "Coking coal costs are expected to rise by about $25 in Q3, in line with the "
     "sharp increase in prices in the latter half of Q2.",
     "open", None, None),
    ("concall-jswsteel-q2fy27", "volume", "Q3FY27", "direction", None, None, None,
     "up", "t consol sales",
     "Looking ahead, we expect volumes to increase in Q3 with stronger production "
     "on the back of full production of Vijayanagar BF-3 and JVML operations.",
     "open", None, None),
    ("concall-jswsteel-q2fy27", "capacity", "Q4FY27", "point", 15_000_000.0, None,
     None, None, "t iron ore Karnataka",
     "The three new mines in Karnataka are now expected to be commissioned in Q4 "
     "of FY 2027. This will provide additional capacity of around 4 million tons, "
     "taking the overall capacity in Karnataka to about 15 million tons per annum "
     "of iron ore.",
     "open", None, None),
    # ---- JINDAL STEEL ------------------------------------------------------
    ("concall-jindalstel-q1fy27", "cost_per_t", "Q2FY27", "range", None, 12.0, 15.0,
     None, "USD/t coking coal, qoq",
     "Q2 coking coal: +$12–15/t (moderating from Q1's +$23/t).",
     # Met: the CFO says "within the guidance", the MD "$12-$14". No single
     # number was stated, so actual_value stays NULL rather than a picked point.
     "met", Q2_CALL, None),
    ("concall-jindalstel-q1fy27", "hot_metal_rate", "Q2FY27", "point", 12000.0, None,
     None, None, "t/day Angul BF2",
     "Angul BF2 ramp: ~11,000 t/day now → 12,000 t/day by Sept, 13,000 t/day "
     "(100%) by Dec 2026",
     "missed", Q2_CALL, 10000.0),
    ("concall-jindalstel-q1fy27", "hot_metal_rate", "Q3FY27", "point", 13000.0, None,
     None, None, "t/day Angul BF2",
     "Angul BF2 ramp: ~11,000 t/day now → 12,000 t/day by Sept, 13,000 t/day "
     "(100%) by Dec 2026",
     "open", None, None),
    ("concall-jindalstel-q1fy27", "capacity", "Q2FY27", "point", 18_000_000.0, None,
     None, None, "t/yr slurry pipeline",
     "Slurry pipeline: commission first half August 2026 (rain caveat: could slip "
     "to end-Aug); 18 MTPA (up to 20 at lower water); saving reflects \"from "
     "September.\"",
     # Commissioned only by the 23-Oct call, commercial flow from 24-Oct: after
     # the quarter the promise named. Nothing flowed in Q2, so no actual value.
     "missed", Q2_CALL, None),
    ("concall-jindalstel-q1fy27", "net_debt_ebitda", "Q2FY27", "point", 1.5, None,
     None, None, "x, sub-",
     "Net debt/EBITDA sub-1.5x by Q2 FY27 (reaffirmed).",
     # 1.51x is not sub-1.5x. The note grades it Partial; the ledger is binary
     # and the promise was a ceiling.
     "missed", Q2_CALL, 1.51),
    ("concall-jindalstel-q1fy27", "ebitda_per_t", "Q2FY27", "direction", None, None,
     None, "up", "INR/t",
     "Operating-leverage reversal ~Rs 2,000/t in Q2 as shutdown ends and volumes "
     "recover.",
     "missed", Q2_CALL, 10529.0),
    ("concall-jindalstel-q1fy27", "captive_ore_share", "FY27", "point", 40.0, None,
     None, None, "% backward-integrated iron ore, exit",
     "Iron-ore backward integration 28% → ~40% exit FY27.",
     "open", None, None),
    ("concall-jindalstel-q2fy27", "hot_metal_rate", "Q3FY27", "point", 12000.0, None,
     None, None, "t/day Angul BF2",
     "In November, we are aiming for 11,000 tons, and then December, 12,000 tons "
     "per day.",
     "open", None, None),
    ("concall-jindalstel-q2fy27", "cost_per_t", "Q3FY27", "range", None, 20.0, 25.0,
     None, "USD/t coking coal, qoq",
     "Coking coal, I would say $20-$25.",
     "open", None, None),
    ("concall-jindalstel-q2fy27", "capacity", "Q1FY28", "point", 2_000_000.0, None,
     None, None, "t/yr DRI2",
     "The DRI plant will be commissioned again in June 2027.",
     "open", None, None),
    ("concall-jindalstel-q2fy27", "capacity", "Q1FY28", "point", 6_000_000.0, None,
     None, None, "t/yr Pellet Plant 2",
     "Hopefully by June next year, this will be fully commissioned. So by March, we "
     "will be doing the complete installation, and maybe end of March or early "
     "April, we'll start our trials. June it should be in commercial production.",
     "open", None, None),
]

# source, metric, period, value, unit, quote
ACTUALS = [
    # ---- INFOSYS -----------------------------------------------------------
    ("concall-infy-q1fy27", "ebit_margin_pct", "Q1FY27", 21.1, "%",
     "FY27 OPM 20%–22% | Held; Q1 at 21.1%"),
    ("concall-infy-q2fy27", "ebit_margin_pct", "Q2FY27", 21.0, "%",
     "Operating margin was stable at 21%."),
    ("concall-infy-q1fy27", "revenue_growth_cc_yoy", "Q1FY27", 2.4, "% CC YoY",
     "Q1 delivering only +1% QoQ / +2.4% YoY CC"),
    ("concall-infy-q2fy27", "revenue_growth_cc_yoy", "Q2FY27", 1.5, "% CC YoY",
     "Our revenue growth for Q2 was 1.5% year-on-year and 1.2% sequentially in "
     "constant currency terms."),
    ("concall-infy-q2fy27", "revenue_growth_cc_pct", "Q2FY27", 1.2, "%",
     "Our revenue growth for Q2 was 1.5% year-on-year and 1.2% sequentially in "
     "constant currency terms."),
    # ---- COFORGE -----------------------------------------------------------
    ("concall-coforge-q1fy27", "ebit_margin_pct", "Q1FY27", 16.0, "% consolidated",
     "Q1: standalone 16.7%, consolidated 16.0% (already > FY target)"),
    ("concall-coforge-q2fy27", "ebit_margin_pct", "Q2FY27", 16.0, "% consolidated",
     "Equally importantly, we delivered a consolidated EBIT margin of 16%, above "
     "our current guidance of 15.5%, while the standalone business delivered an "
     "EBIT margin of 17%."),
    ("concall-coforge-q2fy27", "revenue_growth_cc_pct", "Q2FY27", 10.9, "%",
     "In FY 2027 Q2, we registered sequential consolidated growth of 10.9% in CC "
     "terms, 10.1% in U.S. dollar terms, and 11.6% in Indian rupee terms."),
    # ---- SHREE CEMENT ------------------------------------------------------
    ("concall-shreecem-q2fy27", "volume", "Q2FY27", 9_310_000.0, "t",
     "India sales volumes increased by approximately 18% year-on-year from 7.91 "
     "million tons-9.31 million tons."),
    ("concall-shreecem-q1fy27", "fuel_cost", "Q1FY27", 1.95, "INR/'000 kcal",
     "Q1 fuel cost / clinker factor / realisation / mix? → A: Fuel ₹1.95/kcal"),
    ("concall-shreecem-q2fy27", "fuel_cost", "Q2FY27", 2.09, "INR/'000 kcal",
     "For the current quarter, the landed cost was INR 2.09 as against INR 1.66 "
     "last year and INR 1.95 for the June quarter."),
    # Consolidated. The stored Q1 ebitda_per_t (1,024, Emkay first cut) is on
    # another basis, hence the unit; no open target reads this metric for Shree.
    ("concall-shreecem-q2fy27", "ebitda_per_t", "Q2FY27", 966.0, "INR/t consol",
     "However, consolidated EBITDA per ton declined to INR 966 from INR 1,055 a "
     "year ago, primarily due to a higher fuel cost."),
    # ---- JSW STEEL ---------------------------------------------------------
    ("concall-jswsteel-q1fy27", "volume", "Q1FY27", 6_250_000.0, "t",
     "FY27 production 29.75 MT / sales 28.6 MT | Q1: 6.59 / 6.25 MT (~22% of "
     "guide)"),
    ("concall-jswsteel-q2fy27", "volume", "Q2FY27", 7_460_000.0, "t",
     "Our consolidated sales were the highest at 7.46 million tons, growing at 13% "
     "year-on-year."),
    ("concall-jswsteel-q1fy27", "cost_per_t", "Q1FY27", 17.0, "USD/t coking coal, qoq",
     "Q1 coking coal +$12–15/t | +$17/t — \"slightly higher than our guidance\""),
    ("concall-jswsteel-q2fy27", "cost_per_t", "Q2FY27", 10.0, "USD/t coking coal, qoq",
     "Overall input costs were higher, particularly coking coal, which increased by "
     "about $10 a ton. A bit lower than our earlier guidance of $12-$15 for this "
     "quarter."),
    # ---- JINDAL STEEL ------------------------------------------------------
    # The stored Q1 actuals (digest-2026-07-25: 2.2 MT, Rs12,000/t) are a broker's
    # reading; the call's own figures are 2.23 MT and Rs11,197/t adjusted. Left
    # as they are: two different Q1 prints would be averaged as two quarters.
    ("concall-jindalstel-q2fy27", "volume", "Q2FY27", 2_380_000.0, "t",
     "This was driven by the record Q2 sales volume, as Mr. Sharma said, of 2.38 "
     "million tons and higher average selling prices"),
    ("concall-jindalstel-q2fy27", "ebitda_per_t", "Q2FY27", 10529.0, "INR/t",
     "Our adjusted EBITDA stood at INR 2,504 crore, up 34% year-on-year, while "
     "adjusted EBITDA per ton increased 5% year-over-year to INR 10,529 per ton."),
    ("concall-jindalstel-q1fy27", "cost_per_t", "Q1FY27", 23.0, "USD/t coking coal, qoq",
     "Q1 FY27 coking coal cost +$20–25/t sequentially | +$23/t (confirmed by CFO)"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys=ON")

    print(f"{len(CALLS)} calls, {len(GUIDANCE)} commitments, {len(ACTUALS)} actuals\n")
    for sid, (t, q, eid, cd) in CALLS.items():
        print(f"  {eid:10} {cd}  {NOTE.format(t=t, q=q)}")
        if a.write:
            conn.execute("INSERT OR IGNORE INTO sources (id,kind,source_date,"
                         "captured_at,raw_path) VALUES (?,'concall',?,?,?)",
                         (sid, cd, now, NOTE.format(t=t, q=q)))

    # Re-running must not duplicate: the Q1 loader has no such guard and every
    # reader averages or counts rows, so a second --write would double-weight.
    n_g = n_a = skipped = 0
    print("\n  COMMITMENTS")
    for (sid, metric, period, ttype, val, lo, hi, tdir, unit, quote,
         status, resolved, actual) in GUIDANCE:
        eid, cd = CALLS[sid][2], CALLS[sid][3]
        tgt = (f"{val:g}" if ttype == "point" else
               f"{lo:g}-{hi:g}" if ttype == "range" else tdir)
        print(f"    {eid:10} {metric:22} {period:7} {ttype:9} {tgt:>19}  "
              f"{status}{'' if actual is None else f' ({actual:g})'}")
        if not a.write:
            continue
        if conn.execute("SELECT 1 FROM guidance WHERE entity_id=? AND source_id=? "
                        "AND metric=? AND period=?",
                        (eid, sid, metric, period)).fetchone():
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO guidance (entity_id,source_id,issued_date,period,metric,"
            "target_type,target_value,target_low,target_high,target_dir,unit,quote,"
            "status,resolved_date,actual_value,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, sid, cd, period, metric, ttype, val, lo, hi, tdir, unit, quote,
             status, resolved, actual, now))
        n_g += 1

    print("\n  ACTUALS")
    for sid, metric, period, val, unit, quote in ACTUALS:
        eid, cd = CALLS[sid][2], CALLS[sid][3]
        print(f"    {eid:10} {metric:22} {period:7} {val:>15,.2f} {unit}   as_of {cd}")
        if not a.write:
            continue
        if conn.execute("SELECT 1 FROM observations WHERE entity_id=? AND source_id=? "
                        "AND factor='actual' AND metric=? AND period=?",
                        (eid, sid, metric, period)).fetchone():
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO observations (source_id,entity_id,as_of,factor,metric,"
            "value_num,unit,period,direction,confidence,quote,extractor_version,"
            "created_at) VALUES (?,?,?,'actual',?,?,?,?,1,0.9,?,'concall-1',?)",
            (sid, eid, cd, metric, val, unit, period, quote, now))
        n_a += 1

    if a.write:
        conn.commit()
        print(f"\nwritten: {n_g} commitments, {n_a} actuals, {skipped} already present")
    else:
        print("\nDRY RUN — nothing written. Re-run with --write.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
