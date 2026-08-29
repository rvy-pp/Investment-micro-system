"""Per-feed staleness. The check the refresh light was missing.

    python packages/core/freshness.py
    python packages/core/freshness.py --json

WHY THIS EXISTS. refresh.py can exit clean, run_scores.py can stamp today's
date, and the dashboard can show a green light over commodity inputs that are
five days old — because "the run succeeded" and "the inputs are current" are
different claims and only the first was ever checked. That is this repo's
signature failure shape in operational form: a plausible indicator, nothing
raised, nothing wrong on the surface. Found 2026-08-21 when the light read green
with zinc_shfe 5 trading days stale and OI 5 days stale.

TWO AGES, NOT ONE, and the distinction is load-bearing:

  row_age    trading days since the series had ANY row. Feed liveness. This is
             what gets a threshold, because a feed that stops printing is
             unambiguously broken.

  value_age  trading days since the close actually CHANGED. Information age.
             NOT thresholded, because a flat market is legitimately flat and
             invariant 3 says silence changes nothing.

They diverge hard on the assessed series. cp_coke had THREE distinct closes in
its last thirty rows and thermal_coal_seaborne six, because both are episodic
cited levels carried forward daily. Judging those on row_age alone reports them
as fresh when the last real assessment is weeks old; judging them on value_age
alone reports a quiet market as a broken feed. Report both, threshold the first,
and label the series so the reader knows which number means something.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

# Thresholds in TRADING days, from docs/DAILY_MONITORING.md "Tier 1 — data
# freshness". Trading days, not calendar: on a calendar count every Monday reads
# two days stale and the indicator gets ignored, which is the only way an
# indicator can fail. Indian market holidays are NOT modelled, so a Diwali gap
# reads one day stale — that errs toward over-reporting, which is the safe side.
#
# kind: 'feed'     a live price feed; row_age is the meaningful number
#       'assessed' episodic cited level carried forward; value_age is
#       'monthly'  a monthly series; row_age in trading days is meaningless
FEEDS = {
    "hindalco":              ("Yahoo .NS",        2, "feed"),
    "nalco":                 ("Yahoo .NS",        2, "feed"),
    "hindustan_zinc":        ("Yahoo .NS",        2, "feed"),
    "vedanta":               ("Yahoo .NS",        2, "feed"),
    "vaml":                  ("Yahoo .NS",        2, "feed"),
    "usdinr":                ("Yahoo / pack",     2, "feed"),
    "usdcny":                ("Yahoo / pack",     2, "feed"),
    # westmetall is DAY-DELAYED by design: the newest LME row is always T-1,
    # so the limit is 3 rather than 2 — a 1-day age is the healthy state here,
    # not a warning.
    "lme_aluminium":         ("westmetall LME cash", 3, "feed"),
    "alumina_index":         ("Yahoo ALA=F / pack", 3, "feed"),
    "midwest_premium":       ("Yahoo AUP=F",      3, "feed"),
    "silver":                ("Yahoo SI=F / pack", 3, "feed"),
    "lme_zinc":              ("pack / westmetall LME cash", 3, "feed"),
    "brent":                 ("Daily Metals Pack", 3, "feed"),
    # PARKED 2026-08-24. Wind ZN.SHF, USD/t ex-VAT, and NOTHING price_links it:
    # aluminium.yaml moved both zinc lines to lme_zinc once the pack supplied the
    # real benchmark ("was zinc_shfe (Chinese domestic PROXY)"). It stayed on a
    # 3-day feed limit, so every run raised a staleness gate and a STALE line for
    # a series that cannot move a score. Kept and reported — it is a real capture
    # and may be wanted again as a China-domestic signal — but not as a fault.
    # Re-promote it to "feed" the moment a spec price_links it.
    "zinc_shfe":             ("Wind ZN.SHF — parked, unlinked", 3, "parked"),
    # THRESHOLD MUST MATCH THE SOURCE YOU CAN RELY ON, not the best case.
    # Both of these were ("Daily Metals Pack", 3, "assessed") and reported STALE
    # on essentially every run, for two different reasons:
    #
    #  thermal_coal_seaborne  the pack carries it daily, but the pack is a manual
    #                         drop. The source that ALWAYS arrives is FRED
    #                         PCOALAUUSDM, which is MONTHLY — so a 3-trading-day
    #                         rule could never be satisfied and the warning
    #                         carried no information. Now judged as monthly.
    #                         When the pack IS loaded it sits far inside 45 days
    #                         anyway, so nothing is lost.
    #
    #  cp_coke                has NO automated source at all. Yahoo has no feed,
    #                         FRED retired nothing that covers it, and only the
    #                         pack supplies it. Reporting it as STALE implies a
    #                         broken feed; the truth is there is no feed. `kind`
    #                         is now "manual", which reports the age without
    #                         crying wolf — the same distinction the store makes
    #                         between a withheld score and a missing one.
    #
    # BOTH OF THESE WERE WRONG, and both were corrected 2026-08-24 when the pack
    # became a daily automated feed via Outlook rather than a manual drop.
    #
    #  cp_coke                was "NO automated source" / kind manual. There is
    #                         one now, so a stale cp_coke is once again a real
    #                         fault worth reporting. Kept as `assessed` because
    #                         it is an episodic cited level carried forward daily
    #                         — 3 distinct closes in its last 30 rows — so
    #                         value_age is the number that means something.
    #
    #  thermal_coal_seaborne  was ("FRED PCOALAUUSDM monthly / pack", 70,
    #                         "monthly"), justified in a comment as "the source
    #                         that ALWAYS arrives is FRED". FRED HAS NEVER
    #                         WRITTEN A SINGLE ROW OF IT. Verified: `fred`
    #                         appears 31 times in `prices`, all iron_ore. The
    #                         pack holds a daily row on every month-first FRED
    #                         would write, and prices_io ranks metals_pack 40
    #                         against fred 10, so every FRED coal write is
    #                         refused and always will be. The two are not even
    #                         the same benchmark — FRED is Australian thermal at
    #                         140.40 for 2026-07-01, the pack is Richards Bay at
    #                         103.00, a 36% gap under one entity_id. So this is a
    #                         pack-only DAILY series that had been given the
    #                         loosest threshold in the file (70 calendar days) on
    #                         the strength of a fallback that does not function.
    "cp_coke":               ("Daily Metals Pack",  3, "assessed"),
    "thermal_coal_seaborne": ("Daily Metals Pack — Richards Bay", 3, "assessed"),
    "iron_ore":              ("FRED PIORECRUSDM monthly", 70, "monthly"),

    # UNPARKED 2026-08-28, and the steel five are a CORRECTION, not an
    # addition: PARKED_FEEDS' own rule says "move a series OUT of here the
    # moment a spec price_links it", and steel price_linked all five on
    # 2026-08-25 while they stayed parked — so for three days a dead pack
    # could not have gated a steel score. Found while unparking cement's
    # coal line, which would have repeated the mistake.
    "hrc_india_inr":             ("Daily Metals Pack", 3, "assessed"),
    "rebar_india_primary_inr":   ("Daily Metals Pack", 3, "assessed"),
    "rebar_india_secondary_inr": ("Daily Metals Pack", 3, "assessed"),
    "iron_ore_china_cfr62":      ("Daily Metals Pack", 3, "assessed"),
    "coking_coal_spot_aus":      ("Daily Metals Pack", 3, "assessed"),
    "thermal_coal_indonesia_6322": ("Daily Metals Pack", 3, "assessed"),

    # Cement's OUTPUT price: monthly, and the pack that carries it lands ~15
    # days late (PM figure), so the threshold is a monthly cadence plus that
    # lag — not the 3-day rule of the daily pack columns. 45 calendar days:
    # a completed month lands at month end + capture, and an unchanged
    # in-progress month deliberately keeps its stored date (a restatement is
    # not a new datapoint), so the newest row can legitimately be ~40 days
    # old between flat months.
    "cement_price_india_inr":   ("Daily Cement Pack — monthly", 45, "monthly"),
    "cement_price_north_inr":   ("Daily Cement Pack — monthly", 45, "monthly"),
    "cement_price_central_inr": ("Daily Cement Pack — monthly", 45, "monthly"),
    "cement_price_east_inr":    ("Daily Cement Pack — monthly", 45, "monthly"),
    "cement_price_west_inr":    ("Daily Cement Pack — monthly", 45, "monthly"),
    "cement_price_south_inr":   ("Daily Cement Pack — monthly", 45, "monthly"),

    # --- mining, added 2026-08-29 ---
    "nmdc":              ("Yahoo .NS", 2, "feed"),
    "coal_india":        ("Yahoo .NS", 2, "feed"),
    "hindustan_copper":  ("Yahoo .NS", 2, "feed"),
    "lloyds_metals":     ("Yahoo .NS", 2, "feed"),
    # UNPARKED 2026-08-29: hindustan_copper's spec price_links it — the
    # PARKED_FEEDS rule ("move a series OUT the moment a spec links it").
    # Pack column, daily, same 3-day rule as the other pack dailies.
    "lme_copper":        ("Daily Metals Pack", 3, "assessed"),
    # CIL files production/offtake and SWMA e-auction ~the 1st of the next
    # month (mining_filings.py --fetch); month-end stamping means a healthy
    # feed's newest row is ~30-33 calendar days old just before the next
    # filing. 50 covers a late filing without crying wolf monthly.
    "coalindia_offtake_ttm_mt":      ("CIL monthly filing", 50, "monthly"),
    "coal_eauction_realisation_inr": ("CIL SWMA filing — monthly", 50, "monthly"),
    # NMDC's monthly print reaches the digests days after the filing, but the
    # loading path is the DESK LEDGER (mining_prints.json) because nmdc.co.in
    # uploads ~6 months late — so the threshold covers a digest gap plus the
    # hand-entry lag, not just the filing cadence.
    "nmdc_sales_ttm_mt":  ("NMDC filing via digest ledger — monthly", 60, "monthly"),
    # Administered prices move on NMDC's own circulars, roughly monthly in
    # 2026 but with no fixed schedule — the loader stamps CHANGE EVENTS, not
    # daily carries, so two quiet months are a legitimate state.
    "nmdc_lumps_inr":     ("NMDC circular via digest ledger", 75, "monthly"),
    "nmdc_fines_inr":     ("NMDC circular via digest ledger", 75, "monthly"),
    # No automated source CAN exist: it moves on a notified price hike, an
    # administered decision with no schedule (last visible watch item: CLSA
    # 08-07-2026). Reported, never a breach — the cp_coke "manual" logic.
    "coal_fsa_realisation_inr": ("quarterly cited derivation", 120, "manual"),
}

# CAPTURED BUT NOT MODELLED. Loaded from the pack so the history exists for the
# steel group, but price_link-ed from no spec, so nothing reads them and nothing
# can be scored wrong by them. kind 'parked' reports the age and never counts as
# a breach — 27 red lines for series no bridge consumes is exactly how a reader
# is trained to ignore this report, which is the only way it can fail.
# Move a series OUT of here the moment a spec price_links it.
PARKED_FEEDS = [
    "lme_lead", "lme_nickel", "gold", "dxy",
    "iron_ore_china_cfr62", "iron_ore_china_import62", "iron_ore_sgx_tsi62",
    "iron_ore_futures_china_cny",
    "hrc_china_export_fob", "hrc_china_domestic", "hrc_cis_fob",
    "hrc_india_usd", "hrc_uk", "hrc_germany",
    "rebar_china_cny",
    "aluminium_shfe_cny", "alumina_shfe_cny", "alumina_shfe_usd",
]
for _e in PARKED_FEEDS:
    FEEDS[_e] = ("Daily Metals Pack — parked", 3, "parked")

# THE BROKER STOPPED MAINTAINING THESE COLUMNS. Not a fetch failure and not
# something to chase: the column is still in the workbook and still empty. Their
# age is genuine history, so it is reported, but calling them STALE would be
# reporting a broker's editorial decision as a pipeline fault.
DISCONTINUED = {
    "scrap_turkey":             "pack col 8 — last print 2020-12-03",
    "coking_coal_contract_qtr": "pack col 9 — last print 2022-06-27; col 10 "
                                "(coking_coal_spot_aus) is the live one",
    "zinc_shfe_cny":            "pack col 22 — last print 2021-04-01; #N/A since",
}
for _e in DISCONTINUED:
    FEEDS[_e] = ("Daily Metals Pack — DISCONTINUED", 3, "parked")

DEFAULT = ("unmapped", 3, "feed")

# OI is not in `prices`, so it is checked separately rather than being missed.
OI_THRESHOLD = 2


def trading_days(a: dt.date, b: dt.date) -> int:
    """Weekdays strictly after `a`, up to and including `b`."""
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def check(today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    conn = sqlite3.connect(DB)
    rows, worst = [], 0

    for (eid,) in conn.execute("SELECT DISTINCT entity_id FROM prices ORDER BY entity_id"):
        feed, limit, kind = FEEDS.get(eid, DEFAULT)
        hist = conn.execute(
            "SELECT date, close FROM prices WHERE entity_id=? AND close IS NOT NULL "
            "ORDER BY date", (eid,)).fetchall()
        if not hist:
            continue
        last_d, last_c = hist[-1]
        row_age = trading_days(dt.date.fromisoformat(last_d), today)

        # Walk back to the last date the value actually moved.
        changed_on = last_d
        for d, c in reversed(hist[:-1]):
            if c != last_c:
                break
            changed_on = d
        value_age = trading_days(dt.date.fromisoformat(changed_on), today)

        # A monthly series is never judged on trading days. 70 CALENDAR days,
        # not 45: an IMF series is DATED to the first of the month it describes
        # and PUBLISHED around the middle of the following one, so the July print
        # is stamped 2026-07-01 and appears ~2026-08-15. On 2026-08-24 that is 54
        # days old and perfectly current, which a 45-day bar called STALE. The
        # limit has to cover one month of data lag plus one of publication lag,
        # or it flags a healthy feed every single month.
        if kind == "parked":
            # Age reported, never a breach. See PARKED_FEEDS / DISCONTINUED.
            cal = (today - dt.date.fromisoformat(last_d)).days
            stale = False
            why = DISCONTINUED.get(eid)
            age_txt = f"{cal}d — " + ("discontinued" if why else "not modelled")
        elif kind == "manual":
            # No automated source exists. Age is reported so it cannot be
            # forgotten, but it is never counted as a staleness breach — a feed
            # that was never wired is not a feed that broke, and conflating the
            # two trains the reader to ignore the column.
            cal = (today - dt.date.fromisoformat(last_d)).days
            stale = False
            age_txt = f"{cal}d — no auto source"
        elif kind == "monthly":
            cal = (today - dt.date.fromisoformat(last_d)).days
            stale = cal > limit
            age_txt = f"{cal} calendar days"
        else:
            stale = row_age > limit
            age_txt = f"{row_age} trading days"

        if stale:
            worst = max(worst, row_age)
        rows.append({
            "series": eid, "feed": feed, "kind": kind, "limit": limit,
            "last_date": last_d, "row_age": row_age,
            "value_changed_on": changed_on, "value_age": value_age,
            "stale": stale, "age_txt": age_txt,
        })

    # OI lives in its own table and was silently absent from every check until
    # 2026-08-21, when it turned out to be 5 trading days behind.
    oi_last = conn.execute("SELECT MAX(date) FROM oi").fetchone()[0]
    oi = None
    if oi_last:
        age = trading_days(dt.date.fromisoformat(oi_last), today)
        oi = {"series": "oi", "feed": "vault_oi.py", "kind": "feed",
              "limit": OI_THRESHOLD, "last_date": oi_last, "row_age": age,
              "stale": age > OI_THRESHOLD, "age_txt": f"{age} trading days"}
        if oi["stale"]:
            worst = max(worst, age)
    conn.close()

    stale = [r for r in rows if r["stale"]] + ([oi] if oi and oi["stale"] else [])
    return {
        "as_of": today.isoformat(),
        "series": rows,
        "oi": oi,
        "stale": stale,
        "n_stale": len(stale),
        "worst_age": worst,
        "ok": not stale,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = check()
    if a.json:
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1

    print(f"feed freshness as of {r['as_of']}\n")
    print(f"{'series':30}{'feed':36}{'last':>12}{'row':>6}{'value':>7}  note")
    print("-" * 104)
    for x in sorted(r["series"], key=lambda x: (-x["row_age"], x["series"])):
        note = ""
        if x["stale"]:
            note = f"STALE — limit {x['limit']}"
        elif x["kind"] == "parked":
            note = DISCONTINUED.get(x["series"], "captured, not modelled")
        elif x["kind"] == "assessed" and x["value_age"] > x["row_age"] + 2:
            note = f"assessed — unchanged {x['value_age']}d, carried forward"
        print(f"{x['series']:30}{x['feed']:36}{x['last_date']:>12}"
              f"{x['row_age']:>6}{x['value_age']:>7}  {note}")
    if r["oi"]:
        o = r["oi"]
        print(f"{'oi':30}{o['feed']:36}{o['last_date']:>12}{o['row_age']:>6}"
              f"{'':>7}  {'STALE — limit ' + str(o['limit']) if o['stale'] else ''}")

    print()
    if r["ok"]:
        print("all feeds within their thresholds")
    else:
        print(f"{r['n_stale']} feed(s) STALE, worst {r['worst_age']} trading days:")
        for x in r["stale"]:
            print(f"   {x['series']:30}{x['feed']:36}{x['age_txt']}")
        print("\ndocs/DAILY_MONITORING.md: report it, and withhold the affected")
        print("scores. A stale price is not a flat price.")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
