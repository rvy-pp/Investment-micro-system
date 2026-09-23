"""Vahan registrations off analytics.parivahan.gov.in — maker-wise, no login, no captcha.

    python packages/adapters/vahan.py --makers "TATA MOTORS"     # resolve maker strings
    python packages/adapters/vahan.py --series "BAJAJ AUTO LTD"  # all-India monthly
    python packages/adapters/vahan.py --share 2W --months 4      # a share cross-section
    python packages/adapters/vahan.py --selftest

TRANSPORT ONLY. This module fetches and reconciles; it writes nothing. No spec
decision has been made about which of these numbers reach a pillar, and nothing
here touches `prices` — a registration count is a VOLUME, and putting a volume
in prices.close is the cement_pack `Volumes`-sheet mistake (the bridge would
shock it). When it lands, it lands in its own table.

THE ROUTE, and why it is not the obvious one.
The portal has two faces. `/analytics/vahanpublicreport` is the report builder
everyone screenshots — it has fromDate/toDate, so it is the only DAILY source —
and it is gated by an image CAPTCHA plus a per-session _csrf token. We do not
touch it: solving a CAPTCHA is off the table, so daily needs either a human at
the form or the MTD-delta method described below.

The other face is the public dashboard's own JSON API, which needs NO cookie, NO
csrf, NO captcha and answers stdlib urllib in ~0.4s. That is what this uses.
Discovered by watching the dashboard's XHR in a browser pane — the same method
that found the GIFT Nifty endpoint, and necessary for the same reason: the
parameter VALUES are not guessable (rtoCode=0 not "", archiveTypeAC the literal
string ACTIVE_COMPLIANT). An empty-string sweep returns HTTP 400.

*** TRAP 1: top5Makerchart IGNORES fromYear/toYear COMPLETELY. ***
`/vahandashboard/top5Makerchart` is the endpoint whose name says "makers", and it
is the wrong one. Measured 2026-09-19: fromYear/toYear of 2024, 2025 and 2026 all
return byte-identical numbers (Hero 49,710,727) — it is an ALL-TIME cumulative
total wearing a year filter. It DOES honour vehicleCategoryGroup, so the filters
"work" and the thing looks alive. Built a market-share series on it and every day
would print the same number. This is the repo's standing shape: the numbers are
right and the PERIOD is wrong, exactly like Yahoo's range=max returning monthly
bars. Use durationWiseRegistrationTable, which honours both.

*** TRAP 2: AN EMPTY LIST IS A REFUSAL AT ALL-INDIA AND A ZERO AT STATE LEVEL. ***
Read `series()` for the second half of this — conflating the two is a bug this
module shipped with and that zeroed eight OEMs in a table that looked fine.
All-India (stateCode="") returns `[]` in 0.3s for the largest makers — HERO
MOTOCORP LTD and MARUTI SUZUKI INDIA LTD both do. It is not a timeout and not
flaky: three consecutive calls, all 0.3s, all empty. The same maker scoped to a
single state answers fine (Hero/UP 2026 = 1,035,226). So the server
short-circuits above some row count and says nothing about it.

**IT IS INTERMITTENT — measured 2026-09-23, four days after the above.** The
same two makers now ANSWER at all-India, five consecutive calls, and both
reconcile to the 36-state sum with zero difference on every year. So the
short-circuit comes and goes with something server-side we cannot see. Nothing
below changes: `all_india()` still sums the 36 states, because that is the only
path that is correct under BOTH regimes, and the guard stays because an empty
body must never become a zero. What DID change is the selftest, which used to
assert the server stays broken — see the note in `selftest()`. A caller that
treats [] as zero silently drops the two biggest two-wheeler OEMs out of a market
share denominator and every other maker's share rises to compensate — plausible,
monotonic, and wrong. `all_india()` therefore NEVER uses the all-India call; it
always sums the 36 states, and `series()` raises on an empty body.

The state-sum is not a guess — it is reconciled. TATA MOTORS PASSENGER VEHICLES
LTD answers all-India directly, and direct vs sum-of-36 agrees to ZERO on every
year 2021-2026 (420,701 / 511,557 / 491,166 / 485,164 / 430,273 / 9). 36 calls
take ~16s serially, ~3s at 8 threads.

GRANULARITY IS `calendarType`, and it bottoms out at MONTHLY.
    1 -> yearly        ("2026")
    2 -> quarterly     ("2026-Q3")
    3 -> MONTHLY       ("2026-September")
    4..8 -> silently fall back to YEARLY. There is no daily here.
That fallback is itself trap-shaped: asking for 4 hoping for "weekly" returns a
well-formed yearly series, no error.

DAILY, given all of the above. The current month's row is a running
month-to-date, so day t's registrations = MTD(t) - MTD(t-1), captured each
morning. That is forward-only — it cannot be backfilled — which is the same
bargain indiamart_cement.py took, and the same reason it must start capturing
before it is needed. Vahan also BACKDATES: a registration is filed against its
own date, so a recent MTD keeps growing for days afterwards. Any daily series
built this way is a revision series, not a print, and must be stored so a later
capture can correct an earlier day rather than append beside it.

MAKER STRINGS ARE THE ENTITY-RESOLUTION PROBLEM, not a lookup.
The API matches the maker name EXACTLY and uppercase; the chart labels are
title-cased for display, so copying a label back in returns []. Worse, one
listed company is several maker strings, and the splits are not cosmetic:
  - TATA MOTORS LTD *and* TATA MOTORS PASSENGER VEHICLES LTD (the demerged PV
    arm) — summing is mandatory or PVs vanish.
  - ROYAL-ENFIELD (UNIT OF EICHER LTD) carries Eicher's motorcycles; EICHER
    MOTORS LTD does not. EICHER TRACTORS is a different business again.
  - MAHINDRA & MAHINDRA LIMITED vs (TRACTOR) vs (SWARAJ DIVISION) vs FARM
    MACHINERY vs MAHINDRA ELECTRIC AUTOMOBILE LTD — auto/tractor/EV must be a
    deliberate spec choice, not a prefix match.
  - ASHOK LEYLAND LTD *and* ASHOK LEYLAND LTD. — same company, trailing period,
    two rows. A prefix match catches both; an exact match on one halves it.
  - HARLEY DAVIDSON (IMPORTER: HERO MOTOCORP) is NOT Hero's own volume.
No mapping is hardcoded here on purpose. It belongs in specs/ beside a PM
decision, the way book.yaml ticker_map is sourced rather than guessed.

COVERAGE: all 36 states/UTs report for 2026, Telangana included (778,118) — the
historic Vahan TG hole is closed on this portal. Checked, because a silently
absent large state is the same failure as trap 2.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://analytics.parivahan.gov.in/analytics"
DASH = BASE + "/publicdashboard/vahandashboard"
MAKER_LOOKUP = BASE + "/publicdashboard/lazy/vehicle-makers"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

# 36 states/UTs, from the report page's own <select id="stateName">.
STATES = [
    "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "GA", "GJ", "HP",
    "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP", "MZ",
    "NL", "OR", "PB", "PY", "RJ", "SK", "TG", "TN", "TR", "UK", "UP", "WB",
]

CALENDAR = {"year": "1", "quarter": "2", "month": "3"}

# The government cert chain is not reliably present on this box; the payload is
# public registration counts, so verification buys nothing here.
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


class VahanRefused(RuntimeError):
    """The server answered 200 with an empty body — see TRAP 2. Never a zero."""


def _params(maker: str, state: str, calendar: str, from_year: str, to_year: str) -> dict:
    """The dashboard's own parameter set, captured from its live XHR.

    Every value here is load-bearing. rtoCode must be "0" and NOT "": the empty
    string returns HTTP 400. archiveTypeAC/ANC carry literal enum names, not
    flags. timePeriod=2 is the dashboard's own default and means "all years
    available"; the year window is set by fromYear/toYear.
    """
    return {
        "fromYear": from_year, "toYear": to_year,
        "stateCode": state, "rtoCode": "0",
        "vehicleClasses": "", "vehicleMakers": maker,
        "vehicleSubCategories": "", "vehicleEmissions": "", "vehicleFuels": "",
        "timePeriod": "2", "vehicleCategoryGroup": "", "evType": "",
        "vehicleStatus": "", "vehicleOwnerType": "", "fitnessCheck": "0",
        "vehicleType": "", "calendarType": calendar,
        "archiveTypeAC": "ACTIVE_COMPLIANT",
        "archiveTypeANC": "ACTIVE_NON_COMPLIANT",
        "archiveTypePA": "", "archiveTypeTA": "", "archiveTypeNA": "",
    }


def _get(url: str, tries: int = 3, timeout: int = 180):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return json.loads(r.read().decode("utf8", "replace"))
        except Exception as e:                       # noqa: BLE001 - retry anything
            last = e
            time.sleep(1 + i)
    raise RuntimeError(f"GET failed after {tries}: {url}\n  {last}")


def find_makers(search: str) -> list[str]:
    """Exact maker strings matching `search`. The param is `search`, not
    `searchTerm` — a wrong name is IGNORED rather than rejected, and you get
    page 0 of the alphabet ('3EV INDUSTRIES...') which looks like a real answer.
    """
    q = urllib.parse.urlencode({"page": "0", "size": "50", "search": search})
    return _get(f"{MAKER_LOOKUP}?{q}")


def series(maker: str, state: str = "", calendar: str = "month",
           from_year: str = "2026", to_year: str = "2026") -> dict[str, int]:
    """{period_label: count} for one maker in one state.

    AN EMPTY BODY MEANS TWO DIFFERENT THINGS AND THE STATE ARG SEPARATES THEM.
    The first version of this raised on BOTH, and that was wrong in the
    direction that hurts: `all_india()` propagated the first exception, so ONE
    tiny state with no registrations killed the whole maker. Escorts Kubota,
    TAFE, Sonalika, John Deere, VECV, Force Motors, Atul Auto and Ola all
    reported 12-month volume of EXACTLY ZERO in a table that otherwise looked
    fine — a guard written for trap 2 firing on the ordinary case. Caught by
    reading the table and not believing that TAFE sells no tractors.

      stateCode="" (all-India) + empty  -> the server short-circuit, TRAP 2.
                                           A refusal. Raise.
      stateCode=XX + empty              -> that maker genuinely registered
                                           nothing in that state. A real zero.

    Verified rather than assumed: TAFE LIMITED returns empty for AN, LD and SK
    and returns real data for MH (135), UP (590) and TN (234) in the same month.
    A tractor maker with no Andaman sales is not a failed query.
    """
    cal = CALENDAR[calendar]
    q = urllib.parse.urlencode(_params(maker, state, cal, from_year, to_year))
    rows = _get(f"{DASH}/durationWiseRegistrationTable?{q}")
    if not rows:
        if not state:
            raise VahanRefused(
                f"empty body for maker={maker!r} at ALL-INDIA — a refusal, not "
                "a zero (see TRAP 2); sum the states instead")
        return {}
    out: dict[str, int] = {}
    for r in rows:
        label = r.get("yearAsString") or str(r.get("year"))
        out[label] = out.get(label, 0) + (r.get("registeredVehicleCount") or 0)
    return out


def all_india(maker: str, calendar: str = "month", from_year: str = "2026",
              to_year: str = "2026", workers: int = 8) -> dict[str, int]:
    """Sum the 36 states. NEVER calls stateCode="" — see TRAP 2.

    Reconciled against the one large maker whose all-India call does work
    (TATA MOTORS PASSENGER VEHICLES LTD): direct and state-sum agree to zero on
    every year 2021-2026.
    """
    def one(st):
        return series(maker, st, calendar, from_year, to_year)

    total: dict[str, int] = {}
    empty = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(one, STATES):
            if not part:
                empty += 1
                continue
            for k, v in part.items():
                total[k] = total.get(k, 0) + v
    # All 36 empty is not a maker with no sales anywhere — it is a maker string
    # that does not exist. The lookup is exact and case-sensitive, so a
    # title-cased chart label pasted back in lands here. Refuse it rather than
    # contributing a silent zero to somebody's market-share denominator.
    if empty == len(STATES):
        raise VahanRefused(
            f"maker={maker!r} returned empty in all {len(STATES)} states — "
            "almost certainly a wrong maker string; resolve it with find_makers()")
    return total


def _months(n: int, year: str = "2026") -> list[str]:
    names = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
    today = time.gmtime()
    return [f"{year}-{names[m]}" for m in range(today.tm_mon - 1, max(-1, today.tm_mon - 1 - n), -1)]


GROUPS = {
    # Illustrative only — NOT a spec. The real mapping is a PM decision; see the
    # maker-string note in the module docstring.
    "2W": {
        "Hero": ["HERO MOTOCORP LTD"],
        "Honda 2W": ["HONDA MOTORCYCLE AND SCOOTER INDIA (P) LTD"],
        "TVS": ["TVS MOTOR COMPANY LTD"],
        "Bajaj": ["BAJAJ AUTO LTD"],
        "Royal Enfield": ["ROYAL-ENFIELD (UNIT OF EICHER LTD)"],
        "Suzuki 2W": ["SUZUKI MOTORCYCLE INDIA PVT LTD"],
    },
    "PV": {
        "Maruti Suzuki": ["MARUTI SUZUKI INDIA LTD"],
        "Hyundai": ["HYUNDAI MOTOR INDIA LTD"],
        "Tata Motors": ["TATA MOTORS LTD", "TATA MOTORS PASSENGER VEHICLES LTD"],
        "Mahindra": ["MAHINDRA & MAHINDRA LIMITED"],
        "Toyota": ["TOYOTA KIRLOSKAR MOTOR PVT LTD"],
        "Kia": ["KIA INDIA PRIVATE LIMITED"],
    },
}


def share(group: str, n_months: int = 4) -> None:
    months = _months(n_months)
    names = GROUPS[group]
    got: dict[str, dict[str, int]] = {}
    t0 = time.time()
    for label, makers in names.items():
        agg: dict[str, int] = {}
        for mk in makers:
            for k, v in all_india(mk).items():
                agg[k] = agg.get(k, 0) + v
        got[label] = agg
    hdr = f"{'maker':16}" + "".join(f"{m.split('-')[1][:3]:>12}" for m in months)
    print(f"\n{group} registrations, Vahan all-India ({time.time()-t0:.0f}s)\n")
    print(hdr)
    print("-" * len(hdr))
    for label in names:
        print(f"{label:16}" + "".join(f"{got[label].get(m,0):>12,}" for m in months))
    tot = {m: sum(got[l].get(m, 0) for l in names) for m in months}
    print("-" * len(hdr))
    print(f"{'TOTAL (listed)':16}" + "".join(f"{tot[m]:>12,}" for m in months))
    print(f"\nshare % of the listed set — NOT of the market: these are the "
          f"{len(names)} names above, not every maker\n")
    for label in names:
        print(f"{label:16}" + "".join(
            f"{(100*got[label].get(m,0)/tot[m] if tot[m] else 0):>11.1f}%" for m in months))
    print("\nthe newest month is MONTH-TO-DATE and still being backdated into — "
          "it is not comparable to a full month.")


def selftest() -> int:
    """Acceptance AND rejection, per the GLOB lesson: a guard that only ever
    refuses is indistinguishable from one that refuses everything.
    """
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
        if not cond:
            fails.append(name)

    print("ACCEPTANCE")
    mk = find_makers("BAJAJ AUTO")
    check("maker lookup resolves", "BAJAJ AUTO LTD" in mk, str(mk[:3]))

    s = series("TATA MOTORS PASSENGER VEHICLES LTD", "DL", "year")
    check("per-state yearly returns data", len(s) > 3, f"{len(s)} periods")

    m = series("HERO MOTOCORP LTD", "UP", "month")
    check("monthly labels look monthly", any("-" in k and k.split("-")[1].isalpha() for k in m),
          str(sorted(m)[-1:]))

    print("\nRECONCILIATION (the state-sum workaround)")
    direct = series("TATA MOTORS PASSENGER VEHICLES LTD", "", "year")
    summed = all_india("TATA MOTORS PASSENGER VEHICLES LTD", "year")
    common = sorted(set(direct) & set(summed), reverse=True)[:6]
    diffs = {y: summed[y] - direct[y] for y in common}
    check("direct == sum of 36 states", all(v == 0 for v in diffs.values()), str(diffs))

    print("\nREJECTION")
    # THE GUARD IS ASSERTED; THE SERVER BUG IS ONLY OBSERVED.
    # This check originally read "Hero at all-India MUST raise", which passed on
    # 2026-09-19 (three consecutive 0.3s empty bodies) and FAILED on 2026-09-23,
    # when the endpoint began serving Hero and Maruti at all-India correctly —
    # both reconcile to the 36-state sum with zero difference. That was a test
    # asserting the SERVER STAYS BROKEN, which is not an invariant and not
    # something this repo gets to depend on.
    #
    # The short-circuit is INTERMITTENT, so neither branch can be assumed: the
    # guard stays (an empty body must never become a zero) and `all_india()`
    # keeps summing states (the only path that is correct under both regimes).
    # What is asserted below is the guard's own contract, with an input that
    # returns empty deterministically.
    try:
        series("NOT A REAL MAKER PVT LTD", "", "year")
        check("all-India empty raises rather than returning {}", False)
    except VahanRefused:
        check("all-India empty raises rather than returning {}", True)
    try:
        series("HERO MOTOCORP LTD", "", "year")
        print("  note  the all-India short-circuit is NOT firing today "
              "(Hero answers) — intermittent, so the state-sum path stays")
    except VahanRefused:
        print("  note  the all-India short-circuit IS firing today (Hero empty)")

    try:
        all_india("NOT A REAL MAKER PVT LTD", "year")
        check("bogus maker raises (empty in all 36 states)", False)
    except VahanRefused:
        check("bogus maker raises (empty in all 36 states)", True)

    # The regression for the bug this module shipped with: a state-scoped empty
    # must be a ZERO, not a refusal, or one dead UT zeroes an entire OEM.
    check("state-scoped empty is a zero, NOT a raise",
          series("TAFE LIMITED", "AN", "month") == {}, "TAFE/Andaman")
    tafe = all_india("TAFE LIMITED", "month")
    check("...and the maker still totals through it",
          tafe.get("2026-August", 0) > 1000, f"TAFE 2026-Aug={tafe.get('2026-August',0):,}")

    print("\nTRAP 1 REGRESSION (top5Makerchart ignores the year window)")
    q = lambda y: urllib.parse.urlencode(_params("", "", "1", y, y))  # noqa: E731
    a = _get(f"{DASH}/top5Makerchart?{q('2024')}")
    b = _get(f"{DASH}/top5Makerchart?{q('2026')}")
    check("top5Makerchart is STILL year-blind (do not use it)",
          a["datasets"][0]["data"] == b["datasets"][0]["data"],
          "if this FAILS the endpoint was fixed upstream — re-test before trusting it")

    print(f"\n{len(fails)} failure(s)" + (f": {fails}" if fails else ""))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--makers", metavar="SEARCH", help="resolve exact maker strings")
    ap.add_argument("--series", metavar="MAKER", help="all-India series for one maker")
    ap.add_argument("--state", default="", help="restrict --series to one state code")
    ap.add_argument("--calendar", default="month", choices=list(CALENDAR))
    ap.add_argument("--share", choices=list(GROUPS), help="a share cross-section")
    ap.add_argument("--months", type=int, default=4)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()
    if a.makers:
        for m in find_makers(a.makers):
            print(" ", m)
        return 0
    if a.series:
        d = (series(a.series, a.state, a.calendar) if a.state
             else all_india(a.series, a.calendar))
        for k in sorted(d, reverse=True)[:24]:
            print(f"  {k:20} {d[k]:>12,}")
        return 0
    if a.share:
        share(a.share, a.months)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
