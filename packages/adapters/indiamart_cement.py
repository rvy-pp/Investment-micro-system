"""L0 watch — IndiaMART cement bag listings, as a DAY-TO-DAY MOVE DETECTOR.

    python packages/adapters/indiamart_cement.py --probe        # fetch, parse, print, write nothing
    python packages/adapters/indiamart_cement.py --capture      # fetch and store today's panel
    python packages/adapters/indiamart_cement.py --report       # alerts from what is stored

WHAT THIS IS FOR, AND WHAT IT IS NOT FOR.

The Kotak Daily Cement Pack is the priced series and stays the reference, but the
PM's standing figure is that its prints land about **15 days late**. Cement takes
its hikes at the start of a month, so a fortnight's lag means the pack confirms a
move well after it is tradeable. This exists to notice the move earlier: a daily
scan of dealer ask-prices, per region, reported as **an alert on the dashboard**
when several regions move together.

**IT MUST NEVER REACH A SCORE.** It writes to `cement_watch*`, NOT to `prices`.
Three reasons, and the third is the one that would actually bite:

  1. These are marketplace ASKS, not transactions or an assessed index.
  2. They carry no date of their own (see below), so they fail the citation
     standard everything in `prices` is held to.
  3. `prices` is where the CLOCK comes from. This series would print every day,
     so `series.latest_daily_date()` would happily let a scraped ask set the
     as_of for every pillar in the book. That is SILENT_BUGS entry 8b again, and
     the fix for it was to stop coarse series setting the clock — not to invite
     a noisier one in.

--- the thing that makes this workable: STABLE LISTING IDS ---

Each listing carries a product id in its URL (`/proddetail/<slug>-<id>.html`).
So the signal is NOT the median of whatever happened to be on the page today —
that moves when the PANEL changes, not when PRICES change, and on a marketplace
the panel churns for commercial reasons all day long.

Instead the headline number is a **matched-pair change**: computed across only
those product ids present in BOTH today's capture and the previous one.
Composition changes cancel out of it by construction. The raw median level is
stored too, but as context, not as the trigger.

**THE STATISTIC CHANGED ON 2026-09-20 — MEAN OF LOGS, NOT MEDIAN, NOT THE PLAIN
MEAN.** The first 20 captures settled it with data; `summarize()` carries the
numbers. Short version: the median never left +0.000 in 95 readings because it
needs >50% of the panel moving the same day and the most that has ever moved at
all is 45%; the plain mean compounds to +10.7% over the same 19 sessions
against a panel whose level went ~0%, because a listing flipping 290<->430
reads +48.3% up and -32.6% back. The log mean cancels that and compounds to
+0.55%. `matched_median_pct` and `n_moved` ride alongside so the change stays
auditable.

--- what the page actually gives you, measured 2026-08-28 ---

  ~67 priced listings per page, all quoting Rs/Bag, all with a product id.
  Per-city URLs exist (`dir.indiamart.com/<city>/opc-cement.html`).
  Plain urllib, HTTP 200 in ~0.5s, no auth, no model, no API key.

**A CORRECTION WORTH CARRYING, because the first read of this page was wrong.**
The visible result grid — the ~28 cards with `dispId` attributes — shows
"Request a quote" and carries NO price. Every rupee figure on the page comes from
the promoted/"Best Sellers" rails. So this samples IndiaMART's PROMOTED
inventory, not its whole listing set, and that is a selection this cannot see
around. It does vary by city (Delhi ~365, Hyderabad ~290 on the same day), so it
is not one national rail repeated — but "the market" it measures is a curated
slice, and any read of the level has to say so.

**THERE IS NO DATE ANYWHERE IN THE HTML.** No "updated", no timestamp, nothing.
A seller's ask is whatever they last typed, which could be this morning or two
years ago. Two consequences that are not fixable by better parsing:

  - There is NO history. The series starts the day capture starts. Nothing here
    can be backfilled, ever.
  - A listing that never changes is indistinguishable from a price that has not
    moved. This is why the matched-pair change is reported with `n_matched`
    beside it: a 0.0% move across 40 matched listings and across 4 are different
    statements, and only the first is worth anything.

--- the alert threshold is NOT calibrated, deliberately ---

With no history there is no way to know the day-to-day noise floor of this
panel, and a threshold guessed now would either scream every morning or never
fire — the two ways an indicator dies. So `--report` runs in CALIBRATING mode
until `MIN_DAYS_TO_CALIBRATE` captures exist: it prints the observed moves and
refuses to raise an alert. After that it prints the observed distribution so the
threshold can be set FROM the data, the same way `scoring.yaml` says to calibrate
by moving the anchor rather than inventing a coefficient.

`ALERT_PCT` and `MIN_REGIONS` below are provisional starting points, marked as
such, and the report says which mode it is in on every run.

--- rate limits are real ---

IndiaMART returns **HTTP 429** after roughly 15 rapid requests. `SLEEP_S` spaces
them; a 429 backs off and retries. A full sweep of the city list takes a few
minutes, which is why this is its own step rather than something inline.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import math
import pathlib
import re
import sqlite3
import statistics as st
import sys
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Cities mapped onto the Kotak pack's five regions, so the watch and the priced
# series can be read side by side. The mapping is the PM's regional convention,
# not IndiaMART's — IndiaMART has no notion of "Central".
REGION_CITIES = {
    "north":   ["delhi", "jaipur", "ludhiana", "chandigarh"],
    "central": ["lucknow", "indore", "bhopal"],
    "east":    ["kolkata", "patna", "bhubaneswar"],
    "west":    ["mumbai", "pune", "ahmedabad"],
    "south":   ["hyderabad", "bengaluru", "chennai"],
}
CATEGORIES = ["opc-cement", "ppc-cement"]

SLEEP_S = 12.0            # 429 lands at roughly 15 rapid requests
PLAUSIBLE_BAG = (150.0, 800.0)

# CALIBRATED 2026-09-20, on the 19 stored day-pairs x 5 regions of the FIRST
# panel that could produce a distribution at all (the median never left zero,
# so until the log mean landed there was nothing to calibrate against).
#
#   95 regional readings: mean +0.027, sd 0.368, |move| p95 0.694, p99 0.949
#
# ALERT_PCT = 1.0 was a guess in August and the measurement HAPPENS TO SUPPORT
# IT: it sits just above the 99th percentile of this panel's own daily noise,
# and fires on 0 of 19 days at MIN_REGIONS = 2. 0.5 would fire on 16% of days
# on churn alone. Keeping the number and recording why is the honest outcome —
# the value did not need to change, the JUSTIFICATION did.
#
# STILL THIN: 19 day-pairs. Re-run the distribution at ~60 captures before
# treating this as settled, and note that no real regional hike has been
# observed yet — the Kotak pack has printed no new month since capture began,
# so the upper tail here is noise only, never a known true positive.
ALERT_PCT = 1.0           # matched-pair LOG-MEAN move, per region
MIN_MATCHED = 20          # listings present in both captures, per region
MIN_REGIONS = 2           # the PM's filter: substantial means several regions
MIN_DAYS_TO_CALIBRATE = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS cement_watch_listing (
    capture_date TEXT NOT NULL,
    product_id   TEXT NOT NULL,
    city         TEXT NOT NULL,
    region       TEXT NOT NULL,
    category     TEXT NOT NULL,
    price_bag    REAL NOT NULL,
    PRIMARY KEY (capture_date, product_id, city, category),
    CHECK (capture_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (price_bag > 0)
);
CREATE TABLE IF NOT EXISTS cement_watch (
    capture_date TEXT NOT NULL,
    region       TEXT NOT NULL,
    n_listings   INTEGER NOT NULL,
    median_bag   REAL,
    mean_bag     REAL,
    prior_date   TEXT,
    n_matched    INTEGER,
    matched_pct  REAL,
    matched_median_pct REAL,
    n_moved      INTEGER,
    PRIMARY KEY (capture_date, region),
    CHECK (capture_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
);
"""

_PID = re.compile(r"proddetail/[a-z0-9\-]*?(\d{10,})\.html")
_PRICE = re.compile(r"₹\s?([\d,]+(?:\.\d+)?)\s*/\s*([A-Za-z]+)")


def fetch(url: str, tries: int = 4) -> str:
    for k in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and k < tries - 1:
                time.sleep(20 * (k + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def parse(body: str) -> dict[str, float]:
    """{product_id: price_per_bag} for one page.

    A price is attached to the NEAREST PRECEDING product id within 1500 chars.
    Not a card-boundary parse, and that is on purpose: the rendered grid and the
    promoted rails use different markup, and a structural parser tuned to one of
    them silently returns a short list when the other changes. Proximity holds
    across both. The 1500 is wide enough for the longest observed card and
    narrow enough that a price cannot reach back past the card before it.

    First id wins per product, so a listing repeated across rails counts once.
    """
    ids = [(m.start(), m.group(1)) for m in _PID.finditer(body)]
    out: dict[str, float] = {}
    for m in _PRICE.finditer(body):
        val, unit = float(m.group(1).replace(",", "")), m.group(2)
        if unit.lower() != "bag":
            continue
        if not PLAUSIBLE_BAG[0] <= val <= PLAUSIBLE_BAG[1]:
            continue
        near = [(m.start() - p, i) for p, i in ids if 0 < m.start() - p < 1500]
        if not near:
            continue
        pid = min(near)[1]
        out.setdefault(pid, val)
    return out


def sweep(verbose: bool = True) -> list[tuple]:
    """Every city x category. Returns (product_id, city, region, category, price)."""
    rows, failures = [], []
    jobs = [(r, c, cat) for r, cs in REGION_CITIES.items()
            for c in cs for cat in CATEGORIES]
    for n, (region, city, cat) in enumerate(jobs, 1):
        url = f"https://dir.indiamart.com/{city}/{cat}.html"
        try:
            got = parse(fetch(url))
            rows += [(pid, city, region, cat, v) for pid, v in got.items()]
            if verbose:
                med = st.median(list(got.values())) if got else float("nan")
                print(f"  [{n:>2}/{len(jobs)}] {region:<8}{city:<12}{cat:<12}"
                      f"n={len(got):<4}median={med:.0f}")
        except Exception as e:
            failures.append((city, cat, type(e).__name__))
            if verbose:
                print(f"  [{n:>2}/{len(jobs)}] {region:<8}{city:<12}{cat:<12}"
                      f"FAIL {type(e).__name__}")
        if n < len(jobs):
            time.sleep(SLEEP_S)
    if failures and verbose:
        # A PARTIAL SWEEP IS NOT A QUIET MARKET. Named explicitly, because a
        # region that silently dropped out looks exactly like a region that did
        # not move, and this whole tool is a move detector.
        print(f"\n  {len(failures)} page(s) failed: "
              + ", ".join(f"{c}/{k}" for c, k, _ in failures))
    return rows


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    # This process now runs DETACHED and lands its write ~6.5 min after the
    # refresh that spawned it — squarely inside the window where run_scores or
    # a backfill may hold the store. Same contention shape refresh.py already
    # handles for its own steps; 5s matches the store's busy_timeout norm.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
    # so a column added to SCHEMA never reaches a live store. Added with the
    # log-mean change (2026-09-20); without this the first --capture after a
    # pull fails on an unknown column rather than migrating.
    have = {r[1] for r in conn.execute("PRAGMA table_info(cement_watch)")}
    for col, typ in (("matched_median_pct", "REAL"), ("n_moved", "INTEGER"),
                     ("mean_bag", "REAL")):
        if col not in have:
            conn.execute(f"ALTER TABLE cement_watch ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def store(conn: sqlite3.Connection, rows: list[tuple], day: str) -> dict:
    conn.executemany(
        "INSERT OR REPLACE INTO cement_watch_listing "
        "(capture_date, product_id, city, region, category, price_bag) "
        "VALUES (?,?,?,?,?,?)",
        [(day, pid, city, region, cat, px) for pid, city, region, cat, px in rows])

    return summarize(conn, day)


def _panel(conn: sqlite3.Connection, day: str, region: str) -> dict[str, float]:
    """{product_id: price} for one region on one day.

    AVERAGED over duplicates, and that is a fix rather than a nicety. The
    listings PK is (capture_date, product_id, city, category), so ONE product
    id legitimately appears several times in a region — 727 such collisions in
    the first 20 captures, up to 4 copies. The previous dict comprehension over
    the raw rows kept whichever SQLite returned LAST, so the matched-pair
    comparison silently picked an arbitrary one of them on both sides and could
    report a move between two copies of the same listing.
    """
    return {r[0]: r[1] for r in conn.execute(
        "SELECT product_id, AVG(price_bag) FROM cement_watch_listing "
        "WHERE capture_date=? AND region=? GROUP BY product_id", (day, region))}


def summarize(conn: sqlite3.Connection, day: str) -> dict:
    """Per-region day-on-day move for one capture, from the listings table.

    THE HEADLINE `matched_pct` IS THE MEAN OF LOG CHANGES, NOT THE MEDIAN AND
    NOT THE PLAIN MEAN. Changed 2026-09-20 on the PM's question "can we do a
    mean instead of median?", and both halves of that are measured rather than
    argued:

    THE MEDIAN COULD NOT MOVE. It is the median of the per-listing changes, so
    it only leaves zero when MORE THAN HALF the panel moves the same day. The
    most that has ever moved at all is 45.3%. Across 20 captures x 5 regions,
    all 95 readings were EXACTLY +0.000 — a statistic incapable of signalling,
    which reads on the page as a calm market.

    THE PLAIN MEAN IS BIASED UP, AND BADLY. A listing flipping 290 <-> 430
    reads +48.3% up and -32.6% back, so the arithmetic mean of a round trip on
    an UNCHANGED price is +7.86% per leg. ~40% of this panel re-prices daily,
    direction a coin flip (up-share 46.8-52.6% over 19 days), so that bias
    accrues: compounding the daily plain means over the 19 stored day-pairs
    gives +10.72% while the panel's own median level went -1.47/0/0/0/0. It was
    positive on 19 days out of 19. That is the SILENT_BUGS shape exactly —
    +0.5%/day is a completely plausible tightening cement market, nothing
    raises, and at ALERT_PCT = 1.0 it would have fired most weeks on nothing.

    THE LOG MEAN IS UNBIASED UNDER THE SAME FLIP (+39.5% / -39.5% cancel) and
    it reconciles: the same compounding gives +0.55% against a true ~0%. Unlike
    the median it has variance — it ranges -0.40 to +0.30 and goes both ways —
    so it is a statistic that CAN say something. It is stored in percent, and
    for the small moves that matter it reads the same as a percentage change.

    The median rides along in `matched_median_pct` and the count of listings
    that moved at all in `n_moved`: keeping them is what makes the change
    auditable rather than a number that quietly became a different number.
    """
    prior = conn.execute(
        "SELECT MAX(capture_date) FROM cement_watch_listing WHERE capture_date < ?",
        (day,)).fetchone()[0]

    summary = []
    for region in REGION_CITIES:
        today = _panel(conn, day, region)
        if not today:
            continue
        n_matched = matched_pct = matched_median = n_moved = None
        if prior:
            before = _panel(conn, prior, region)
            both = [k for k in today if k in before and before[k] > 0]
            n_matched = len(both)
            if both:
                rel = [(today[k] / before[k] - 1) * 100 for k in both]
                lg = [math.log(today[k] / before[k]) * 100 for k in both]
                matched_pct = st.mean(lg)          # see the docstring
                matched_median = st.median(rel)
                n_moved = sum(1 for x in rel if abs(x) > 1e-9)
        # THE LEVEL IS CHARTED AS THE MEAN, the median kept beside it.
        # PM, 2026-09-20: "why can't you just put the average of all bags in a
        # region". Measured over the 20 captures, the mean is both more
        # informative AND smoother: south's median is pinned at exactly 300 on
        # every single day while its mean moves 308.1-312.8, and north's median
        # jumps to 350 where its mean stays inside 336.0-345.5. A median over a
        # panel carrying only ~100 distinct price points lands on a round
        # number and sits there — the same coarseness that pinned the matched
        # -pair median at zero, showing up a second way.
        levels = list(today.values())
        conn.execute(
            "INSERT OR REPLACE INTO cement_watch (capture_date, region, "
            "n_listings, median_bag, mean_bag, prior_date, n_matched, "
            "matched_pct, matched_median_pct, n_moved) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (day, region, len(today), st.median(levels), st.mean(levels),
             prior, n_matched, matched_pct, matched_median, n_moved))
        summary.append((region, len(today), st.median(list(today.values())),
                        n_matched, matched_pct))
    conn.commit()
    return {"prior": prior, "summary": summary}


def rebuild(conn: sqlite3.Connection) -> int:
    """Recompute every stored capture's summary from cement_watch_listing.

    The listings table holds every row ever scraped, so a change to the
    statistic backfills the whole history instead of starting a new series
    beside the old one — which would leave two bases in one column.
    """
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT capture_date FROM cement_watch_listing ORDER BY 1")]
    for d in days:
        summarize(conn, d)
    return len(days)


def report(conn: sqlite3.Connection, day: str | None = None) -> dict:
    """The dashboard payload: per-region move, and whether it is alertable yet."""
    day = day or conn.execute(
        "SELECT MAX(capture_date) FROM cement_watch").fetchone()[0]
    if not day:
        return {"state": "no_data", "regions": [], "alerts": [],
                "note": "no IndiaMART capture yet — run --capture"}
    n_days = conn.execute(
        "SELECT COUNT(DISTINCT capture_date) FROM cement_watch").fetchone()[0]
    rows = [dict(zip(("region", "n_listings", "median_bag", "prior_date",
                      "n_matched", "matched_pct", "matched_median_pct",
                      "n_moved"), r))
            for r in conn.execute(
                "SELECT region, n_listings, median_bag, prior_date, n_matched, "
                "matched_pct, matched_median_pct, n_moved FROM cement_watch "
                "WHERE capture_date=? ORDER BY region", (day,))]

    calibrating = n_days < MIN_DAYS_TO_CALIBRATE
    movers = [r for r in rows
              if r["matched_pct"] is not None
              and (r["n_matched"] or 0) >= MIN_MATCHED
              and abs(r["matched_pct"]) >= ALERT_PCT]
    # BOTH DIRECTIONS ARE COUNTED, then the larger side wins.
    #
    # My first version anchored on `movers[0]` — the first region alphabetically
    # that cleared the threshold — and kept only the movers agreeing with it.
    # That silently misses the real case: `central +2, east -2, south -2,
    # west -2` anchors on central, finds one region agreeing, and reports
    # nothing, while three regions dropped together. Alphabetical order is not
    # a fact about the market. Caught by a scenario test, not by reading it.
    up = [m for m in movers if m["matched_pct"] > 0]
    down = [m for m in movers if m["matched_pct"] < 0]
    same_way = up if len(up) >= len(down) else down

    alerts = []
    if not calibrating and len(same_way) >= MIN_REGIONS:
        direction = "RISE" if same_way[0]["matched_pct"] > 0 else "DROP"
        alerts.append({
            "level": "alert", "direction": direction,
            "regions": [m["region"] for m in same_way],
            "text": (f"Cement bag prices {direction} across "
                     f"{len(same_way)} regions "
                     f"({', '.join(m['region'] for m in same_way)}) — median "
                     f"matched-listing log-mean move "
                     f"{st.median([m['matched_pct'] for m in same_way]):+.1f}% "
                     f"day-on-day on IndiaMART asks. The Kotak pack will not "
                     f"show this for ~15 days.")})
    return {"state": "calibrating" if calibrating else "live",
            "day": day, "n_days": n_days,
            "days_to_calibrate": max(0, MIN_DAYS_TO_CALIBRATE - n_days),
            "regions": rows, "alerts": alerts,
            "thresholds": {"pct": ALERT_PCT, "min_matched": MIN_MATCHED,
                           "min_regions": MIN_REGIONS},
            "note": ("PROVISIONAL thresholds, not calibrated — needs "
                     f"{MIN_DAYS_TO_CALIBRATE} captures to measure this panel's "
                     "own day-to-day noise. Alerts are suppressed until then."
                     if calibrating else
                     "ALERT_PCT is calibrated (2026-09-20) at ~p99 of this "
                     "panel's own daily noise over 19 day-pairs; no real "
                     "regional hike has been observed yet, so the tail is "
                     "noise only. Re-measure at ~60 captures.")}


def _print_report(rep: dict) -> None:
    print(f"\nIndiaMART cement watch — {rep.get('day') or 'no captures'}  "
          f"[{rep['state'].upper()}]")
    if rep["state"] == "no_data":
        print("  " + rep["note"])
        return
    print(f"  captures stored: {rep['n_days']}"
          + (f" (alerts suppressed for {rep['days_to_calibrate']} more)"
             if rep["state"] == "calibrating" else ""))
    print(f"\n  {'region':<9}{'n':>5}{'median':>9}{'matched':>9}"
          f"{'logmean%':>10}{'med %':>8}{'moved%':>8}  vs")
    for r in rep["regions"]:
        mp = "—" if r["matched_pct"] is None else f"{r['matched_pct']:+.2f}"
        nm = "—" if r["n_matched"] is None else str(r["n_matched"])
        md = ("—" if r.get("matched_median_pct") is None
              else f"{r['matched_median_pct']:+.2f}")
        # moved% is printed beside the move for the same reason n_matched is:
        # +0.00 across a panel where 40% re-priced and +0.00 across one that
        # did not move at all are completely different statements, and the
        # headline number alone cannot tell them apart.
        mv = ("—" if not r.get("n_moved") or not r["n_matched"]
              else f"{r['n_moved'] / r['n_matched'] * 100:.0f}%")
        print(f"  {r['region']:<9}{r['n_listings']:>5}{r['median_bag']:>9.0f}"
              f"{nm:>9}{mp:>10}{md:>8}{mv:>8}"
              f"  {r['prior_date'] or 'first capture'}")
    for a in rep["alerts"]:
        print(f"\n  ** {a['text']}")
    print(f"\n  {rep['note']}")


def _selftest() -> int:
    """python packages/adapters/indiamart_cement.py --selftest

    The ALERT logic, on synthetic panels, in memory — never the real store.
    Eight scenarios, and the ones that must NOT fire matter as much as the one
    that must: an alert that cries wolf on a single region, or on two regions
    moving opposite ways, gets ignored within a week and then the real one is
    ignored too.

    Scenario F is here because it was a live bug. The first version anchored on
    `movers[0]` — the first region ALPHABETICALLY over the threshold — and kept
    only movers agreeing with it, so `central +2, east -2, south -2, west -2`
    reported nothing while three regions dropped together. Reading the code did
    not catch it; running this did.
    """
    def panel(conn, day, shift):
        rows = []
        for reg, cities in REGION_CITIES.items():
            for c in cities:
                for i in range(40):
                    pid = f"{reg}{c}{i:03d}0000000"
                    rows.append((pid, c, reg, "opc-cement",
                                 (300 + hash(pid) % 60) * (1 + shift.get(reg, 0) / 100)))
        store(conn, rows, day)

    cases = [
        ("quiet day",                       {},                                              20, 0),
        ("one region -2.5%",                {"south": -2.5},                                 20, 0),
        ("three regions down",              {"south": -2.5, "west": -2.4, "east": -3.1},     20, 1),
        ("three down, still calibrating",   {"south": -2.5, "west": -2.4, "east": -3.1},      5, 0),
        ("two regions opposite ways",       {"south": -2.5, "west": 2.4},                    20, 0),
        ("minority mover sorts first (F)",  {"central": 2.0, "east": -2.0,
                                             "south": -2.0, "west": -2.0},                   20, 1),
        ("two regions up together",         {"north": 1.6, "central": 1.9},                  20, 1),
        ("moves below the floor",           {"south": -0.4, "west": -0.6, "east": -0.3},     20, 0),
    ]
    bad = 0
    for name, shift, days, want in cases:
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        panel(conn, "2026-08-27", {})
        panel(conn, "2026-08-28", shift)
        for k in range(days):
            conn.execute("INSERT OR IGNORE INTO cement_watch "
                         "(capture_date, region, n_listings) VALUES (?,'north',1)",
                         (f"2026-07-{k + 1:02d}",))
        conn.commit()
        got = len(report(conn, "2026-08-28")["alerts"])
        conn.close()
        ok = got == want
        bad += not ok
        print(f"  {name:<34} alerts={got} expected={want}  "
              f"{'ok' if ok else '*** FAIL ***'}")
    bad += _selftest_stat()
    print("selftest: " + ("PASS" if not bad else f"{bad} FAILURE(S)"))
    return 0 if not bad else 1


def _selftest_stat() -> int:
    """The STATISTIC, against the two failures that produced it (2026-09-20).

    Both cases are load-bearing and neither is hypothetical — each reproduces,
    on four listings, something measured on the live 20-capture panel.
    """
    bad = 0

    def check(name, got, want, tol=1e-6):
        nonlocal bad
        ok = abs(got - want) <= tol
        print(f"  {name:<42s} {got:+8.3f} want {want:+8.3f}  "
              + ("ok" if ok else "FAIL"))
        if not ok:
            bad += 1

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    def put(day, rows):
        conn.executemany(
            "INSERT OR REPLACE INTO cement_watch_listing (capture_date, "
            "product_id, city, region, category, price_bag) VALUES (?,?,?,?,?,?)",
            [(day, pid, city, "north", cat, px) for pid, city, cat, px in rows])

    # REJECTION CASE, and the reason the plain mean was refused. Two listings
    # flip 290->430 and two flip 430->290: the panel is unchanged in aggregate
    # and every price has a partner moving the other way. The plain mean reads
    # +7.86% — it would clear ALERT_PCT eight times over on nothing. The log
    # mean must read 0.
    put("2026-01-01", [("1", "delhi", "opc-cement", 290.0),
                       ("2", "delhi", "opc-cement", 290.0),
                       ("3", "delhi", "opc-cement", 430.0),
                       ("4", "delhi", "opc-cement", 430.0)])
    put("2026-01-02", [("1", "delhi", "opc-cement", 430.0),
                       ("2", "delhi", "opc-cement", 430.0),
                       ("3", "delhi", "opc-cement", 290.0),
                       ("4", "delhi", "opc-cement", 290.0)])
    summarize(conn, "2026-01-02")
    r = conn.execute("SELECT matched_pct, matched_median_pct, n_moved, n_matched "
                     "FROM cement_watch WHERE capture_date='2026-01-02'").fetchone()
    check("symmetric flip: log mean is zero", r[0], 0.0)
    # 48.2759 up, -32.5581 back -> +7.8589 per leg on an unchanged price. The
    # constant was written as 7.8604 first and the ARITHMETIC was right, not
    # the code; pinned to 4dp now so a later edit cannot drift it unnoticed.
    check("symmetric flip: plain mean WOULD have been",
          st.mean([(430/290-1)*100, (430/290-1)*100,
                   (290/430-1)*100, (290/430-1)*100]), 7.8589, 1e-3)
    check("symmetric flip: moved count", float(r[2]), 4.0)

    # ACCEPTANCE CASE — a real, uniform 5% rise must come through as ~5%, or
    # the statistic is only good at saying nothing. log(1.05) = 4.879%, and
    # that gap to 5.00 is why the column is labelled logmean% on the page.
    put("2026-01-03", [("1", "delhi", "opc-cement", 451.5),
                       ("2", "delhi", "opc-cement", 451.5),
                       ("3", "delhi", "opc-cement", 304.5),
                       ("4", "delhi", "opc-cement", 304.5)])
    summarize(conn, "2026-01-03")
    r = conn.execute("SELECT matched_pct FROM cement_watch "
                     "WHERE capture_date='2026-01-03'").fetchone()
    check("uniform +5% comes through", r[0], math.log(1.05) * 100, 1e-3)

    # THE DUPLICATE COLLAPSE. One product id under two cities, 300 and 400.
    # The old dict comprehension kept whichever row came last, so the same
    # listing could appear to move 300->400 between two captures in which
    # nothing changed. _panel averages, so this reads flat.
    conn.execute("DELETE FROM cement_watch_listing")
    conn.execute("DELETE FROM cement_watch")
    put("2026-02-01", [("9", "delhi", "opc-cement", 300.0),
                       ("9", "jaipur", "opc-cement", 400.0)])
    put("2026-02-02", [("9", "jaipur", "opc-cement", 400.0),
                       ("9", "delhi", "opc-cement", 300.0)])
    summarize(conn, "2026-02-02")
    r = conn.execute("SELECT matched_pct, n_matched FROM cement_watch "
                     "WHERE capture_date='2026-02-02'").fetchone()
    check("duplicate ids average, not last-wins", r[0], 0.0)
    check("duplicate ids count once", float(r[1]), 1.0)
    conn.close()
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="fetch and print, write nothing")
    ap.add_argument("--capture", action="store_true", help="fetch and store")
    ap.add_argument("--report", action="store_true",
                    help="alerts from what is already stored, no fetching")
    ap.add_argument("--selftest", action="store_true",
                    help="alert logic on synthetic panels, in memory")
    ap.add_argument("--rebuild", action="store_true",
                    help="recompute every stored capture's summary from the "
                         "listings table (after a change to the statistic)")
    a = ap.parse_args()

    if a.selftest:
        return _selftest()

    if a.rebuild:
        conn = connect()
        n = rebuild(conn)
        print(f"recomputed {n} captures from cement_watch_listing")
        _print_report(report(conn))
        conn.close()
        return 0

    if a.report and not (a.probe or a.capture):
        conn = connect()
        _print_report(report(conn))
        conn.close()
        return 0

    if not (a.probe or a.capture):
        print("give --probe, --capture or --report")
        return 2

    day = dt.date.today().isoformat()
    print(f"IndiaMART sweep {day} — {sum(len(c) for c in REGION_CITIES.values())}"
          f" cities x {len(CATEGORIES)} categories, {SLEEP_S:.0f}s apart\n")
    rows = sweep()
    print(f"\n  {len(rows):,} priced listings, "
          f"{len({r[0] for r in rows}):,} distinct product ids")
    if not rows:
        print("  nothing parsed — refusing to write an empty capture")
        return 1
    if a.probe:
        for region in REGION_CITIES:
            v = [r[4] for r in rows if r[2] == region]
            if v:
                print(f"  {region:<9}n={len(v):<5}median={st.median(v):.0f}")
        print("\nprobe only — pass --capture to store")
        return 0

    conn = connect()
    store(conn, rows, day)
    _print_report(report(conn, day))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
