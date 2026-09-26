"""SIAM monthly WHOLESALE (dispatches to dealers) and the channel it fills.

    python packages/adapters/siam_wholesale.py --fetch        # new releases only
    python packages/adapters/siam_wholesale.py --backfill     # every release since 2024
    python packages/adapters/siam_wholesale.py --report
    python packages/adapters/siam_wholesale.py --selftest

WHAT SIAM GIVES FOR FREE IS THREE NUMBERS A MONTH, AND NOTHING ELSE. The
monthly press release carries PV, 2W and 3W domestic sales plus a production
total. No company split (paid, INR 53,350/yr), no model split (paid, 47,300),
no cars-vs-UVs, no CV at all (quarterly, paid), and **no price of any kind** —
the only "price" product SIAM sells is a raw-material commodity report. The
original plan had SIAM supplying vehicle ASP; it cannot.

WHY IT IS STILL WORTH HAVING: it is WHOLESALE, and Vahan is RETAIL. Dispatch
minus registration is what went INTO DEALER STOCK that month. The festive cycle
is plain in it — 2W +814,785 into the channel in Sep-2025 ahead of Navratri,
then -1,029,867 and -702,423 as Oct/Nov retail drew it down.

*** THREE PARSE TRAPS, ALL SILENT, ALL MET ON THE FIRST RUN. ***

1. **A QUARTER LOADED AS A MONTH.** Sep-2025 prints `84,077units` with no space.
   A regex requiring `([\\d,]+) units` skips it and matches the NEXT figure on
   the page — the July-September QUARTERLY total, 229,239. Three times too
   high, perfectly plausible, and nothing raises: the same "numbers right,
   PERIOD wrong" shape as Yahoo's range=max. Guarded twice: the monthly block
   is cut off at the quarterly `Performance: <Month> - <Month>` heading so a
   quarter's figure is never in reach, AND `_plausible()` refuses a month more
   than 2.2x the median of its neighbours.
2. **THE SOURCE HAS A TYPO.** May- and Jul-2026 read `were units 19,02,209
   units`. Tolerated rather than treated as missing.
3. **CURLY QUOTES** around `'Total PV'` defeated the ex-Tata match on some
   months. Normalised before parsing.

**AND SIAM REUSED A NUMBER.** Dec-2025's "PV without Tata Motors" is 270,704 —
identical to the unit to Dec-2024's, and it would imply Tata sold 128,512 PVs
that month, which is impossible. The ex-Tata figure is therefore NOT stored:
nothing here needs it, and a column known to contain copy-paste errors is a
column somebody will eventually divide by. Tata IS in the headline PV in every
one of the 31 months parsed, so the headline series has no basis break.

COVERAGE IS NOT IDENTICAL TO VAHAN, and the difference always leans the same
way. Vahan counts every maker; SIAM counts its members, and **Ola Electric is
not a member** (nor Kinetic Green, Okinawa, Revolt, Hero Electric). In PV,
BMW / Mercedes / JLR / Volvo are members but "data not available". So retail
carries registrations wholesale never sees, and dispatch-minus-registration
reads slightly LOW every month — Ola alone ran ~8-16k/month through 2026. That
is 1-2% of a festive swing, so the MONTHLY flow is sound; but it COMPOUNDS, so
a running cumulative "inventory level" would drift negative by ~150k a year and
read as destocking that did not happen. The page therefore shows monthly flow
and no cumulative. Correcting it needs those makers' Vahan series, and on
2026-09-25 exactly those queries (Ola, BMW) were returning HTTP 500 at every
scope — a partial server-side outage — so the correction is deferred rather
than half-applied.

3W IS OUT BY DESIGN, NOT JUST BY OUTAGE. Vahan's three-wheeler count is
dominated by e-rickshaws whose makers are not SIAM members, so retail would
exceed wholesale permanently. (It was also 500ing on every route that day.)

Stored in `siam_wholesale`, never `prices` — a unit count in prices.close is
bridge-shockable, the cement_pack `Volumes` rule.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sqlite3
import ssl
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data" / "ims.db"
BASE = "https://www.siam.in/pressrelease-details.aspx?mpgid=53&pgidtrail=50&pid={pid}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
SEGMENTS = {"PV": r"Passenger Vehicles\s*\d?\s*sales were",
            "2W": r"Two-?\s?wheeler sales were",
            "3W": r"Three-?\s?wheeler sales were"}
BACKFILL_FROM_PID = 560   # Feb-2024, the earliest release with this layout
SCAN_AHEAD = 12           # stop after this many CONSECUTIVE empty pids

# AN UNUSED pid IS NOT A 404. SIAM answers any pid — 623, 700, 9999 — with
# HTTP 200 and the site chrome and nothing else, so a "scan until 404" loop
# never terminates. Emptiness has to be read off the CONTENT.
#
# *** AND THE FIRST WAY OF READING IT SILENTLY DROPPED TWO MONTHS IN THREE. ***
# It used a fixed threshold, "under 6,000 characters is empty", calibrated
# against the shortest real release I had looked at — a convention write-up at
# 12,693. The shortest DATA release is far shorter: an ordinary monthly
# release is 4,898-5,319 characters, and only the quarter-end ones (which carry
# an extra quarterly section, ~9,585) cleared the bar. The backfill stored ten
# months — Dec, Mar, Jun, Sep — reported success, and looked like a feed that
# simply published quarterly. A threshold calibrated on the wrong sample.
#
# So nothing is guessed now: the chrome is MEASURED each run by fetching a pid
# that cannot exist, and a page is empty only if it carries essentially nothing
# beyond that chrome. Measured 2026-09-25: chrome 3,557 characters on every
# unused pid, shortest real release 4,898, a gap of 1,341.
CHROME_PROBE_PID = 999999
CHROME_MARGIN = 300       # a page this close to bare chrome has no article on it
_CHROME_LEN: int | None = None

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

DDL = """
CREATE TABLE IF NOT EXISTS siam_wholesale (
    period   TEXT NOT NULL,     -- '2026-August'
    segment  TEXT NOT NULL,     -- 'PV' | '2W' | '3W'
    units    INTEGER NOT NULL CHECK (units > 0),
    pid      INTEGER NOT NULL,  -- the press release it came from (provenance)
    fetched  TEXT NOT NULL,
    PRIMARY KEY (period, segment)
);
CREATE TABLE IF NOT EXISTS siam_seen (
    pid      INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,     -- 'monthly' | 'other' | 'empty'
    period   TEXT,
    checked  TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.executescript(DDL)
    return c


def _text(raw: str) -> str:
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    # Curly quotes defeated the ex-Tata pattern on some months; normalise
    # everything that looks like a quote before any regex sees it.
    for a, b in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"')):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t)


def _get(pid: int) -> str | None:
    req = urllib.request.Request(BASE.format(pid=pid), headers={"User-Agent": UA})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40, context=_CTX) as r:
                return r.read().decode("utf8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1 + i)
        except Exception:                      # noqa: BLE001 - retry anything else
            time.sleep(1 + i)
    return None


def _chrome_len() -> int:
    """Length of the bare site chrome, measured by asking for a pid that cannot exist."""
    global _CHROME_LEN
    if _CHROME_LEN is None:
        raw = _get(CHROME_PROBE_PID)
        _CHROME_LEN = len(_text(raw)) if raw else 3557
    return _CHROME_LEN


def _is_empty(text: str) -> bool:
    return len(text) <= _chrome_len() + CHROME_MARGIN


def _monthly_block(text: str) -> tuple[str | None, str]:
    """(period, the MONTHLY paragraph only) — or (None, '') if not a data release.

    The block stops at the quarterly/cumulative heading, `Performance:
    July - September 2025`. That cut is what keeps trap 1 out of reach: with the
    quarter's figures outside the block, a monthly regex that fails CANNOT fall
    through to a three-month number. It fails loud instead.
    """
    m = re.search(r"Monthly Performance:\s*(" + "|".join(MONTHS) + r")\s+(20\d\d)", text)
    if not m:
        return None, ""
    start = m.start()
    tail = text[m.end():]
    q = re.search(r"Performance:\s*(?:" + "|".join(MONTHS) + r")\s*[-–]\s*(?:"
                  + "|".join(MONTHS) + ")", tail)
    end = m.end() + (q.start() if q else 1500)
    return f"{m.group(2)}-{m.group(1)}", text[start:end]


def parse(text: str) -> dict:
    """{period, PV, 2W, 3W} from one release's text. Missing segments are None."""
    period, blk = _monthly_block(text)
    if not period:
        return {"period": None}
    out = {"period": period}
    for seg, pat in SEGMENTS.items():
        # `(?:units\\s*)?` tolerates the source's own typo, "were units 19,02,209
        # units"; `\\s*units` rather than `\\s+units` tolerates "84,077units".
        m = re.search(pat + r"\s*(?:units\s*)?([\d,]{4,})\s*units", blk, re.I)
        out[seg] = int(m.group(1).replace(",", "")) if m else None
    return out


def _plausible(conn: sqlite3.Connection, period: str, seg: str, units: int) -> str | None:
    """A reason to refuse, or None. Catches a QUARTER parsed as a MONTH.

    2.2x the median of up to six surrounding stored months. The genuine
    festive peaks do not reach it — Oct-2025 2W at 2.21m against neighbours
    around 1.9m is 1.16x — while trap 1's 229,239 against ~70k is 3.3x. A
    ratio, not a z-score: with six points a z-score is mostly noise.
    """
    rows = [r[0] for r in conn.execute(
        "SELECT units FROM siam_wholesale WHERE segment=? AND period!=?", (seg, period))]
    if len(rows) < 4:
        return None
    med = statistics.median(rows[-6:] if len(rows) > 6 else rows)
    if units > 2.2 * med or units < med / 2.2:
        return f"{units:,} is {units/med:.2f}x the recent median {med:,.0f} — refused"
    return None


def ingest(conn: sqlite3.Connection, pid: int, raw: str | None) -> dict:
    now = dt.datetime.now().isoformat(timespec="seconds")
    text = _text(raw) if raw is not None else ""
    if raw is None or _is_empty(text):
        # Recorded, but RE-CHECKED next run: today's empty shell is where next
        # month's release will appear. Marking it permanently seen would mean
        # the October release is never fetched.
        conn.execute("INSERT OR REPLACE INTO siam_seen VALUES (?,?,?,?)", (pid, "empty", None, now))
        return {"pid": pid, "kind": "empty"}
    p = parse(text)
    if not p["period"]:
        conn.execute("INSERT OR REPLACE INTO siam_seen VALUES (?,?,?,?)", (pid, "other", None, now))
        return {"pid": pid, "kind": "other"}
    stored, refused = [], []
    for seg in SEGMENTS:
        v = p.get(seg)
        if v is None:
            refused.append(f"{seg}: not found")
            continue
        why = _plausible(conn, p["period"], seg, v)
        if why:
            refused.append(f"{seg}: {why}")
            continue
        conn.execute("INSERT OR REPLACE INTO siam_wholesale VALUES (?,?,?,?,?)",
                     (p["period"], seg, v, pid, now))
        stored.append(seg)
    conn.execute("INSERT OR REPLACE INTO siam_seen VALUES (?,?,?,?)", (pid, "monthly", p["period"], now))
    conn.commit()
    return {"pid": pid, "kind": "monthly", "period": p["period"],
            "stored": stored, "refused": refused}


def fetch(conn: sqlite3.Connection, backfill: bool = False) -> list[dict]:
    """Scan forward from the last real release until SCAN_AHEAD empty pids in a row.

    The first draft computed the window from the START pid — `hi = lo + 12` —
    so the backfill stopped at 572 and loaded seven months of thirty-one while
    reporting success. Discovery now keys on consecutive emptiness, which is the
    only thing that actually marks the end.
    """
    real = conn.execute(
        "SELECT MAX(pid) FROM siam_seen WHERE kind IN ('monthly','other')").fetchone()[0]
    pid = BACKFILL_FROM_PID if (backfill or real is None) else real + 1
    out, empties = [], 0
    while empties < SCAN_AHEAD:
        seen = conn.execute("SELECT kind FROM siam_seen WHERE pid=?", (pid,)).fetchone()
        if not backfill and seen and seen[0] in ("monthly", "other"):
            pid += 1
            empties = 0
            continue
        r = ingest(conn, pid, _get(pid))
        if r["kind"] == "empty":
            empties += 1
        else:
            empties = 0
            if r["kind"] == "monthly":
                out.append(r)
        pid += 1
    conn.commit()
    return out


def _key(p: str) -> tuple[int, int]:
    y, m = p.split("-")
    return int(y), MONTHS.index(m)


# ---------------------------------------------------------------------------
# THE CHANNEL — wholesale (SIAM) minus retail (Vahan), per segment, per month.
#
# Monthly is the floor and it is set by the SLOWER side: Vahan retail updates
# daily, but SIAM publishes once a month, ~15 days after month end (August's
# figures on 15 September). So the newest channel reading is always last
# month, and the current month appears only once SIAM prints it — `channel()`
# joins on months where BOTH sides exist and never shows retail beside a
# wholesale that does not exist yet.
#
# RETAIL IS THE SEGMENT TOTAL, uncorrected for coverage — see the module
# docstring for why, and for the ~8-16k/month Ola Electric gap that makes every
# month read slightly LOW. Segment totals are also the queries Vahan actually
# serves reliably; on 2026-09-25 it was 500ing on specific makers (Ola, BMW)
# and on the whole 3W category, at every scope.
# ---------------------------------------------------------------------------

# SIAM segment -> Vahan vehicleCategoryGroup. Four Wheeler is PASSENGER (checked
# in vahan_share: Maruti 168,502 there against Ashok Leyland's 1).
RETAIL_GROUP = {"PV": "Four Wheeler", "2W": "Two Wheeler"}

RETAIL_DDL = """
CREATE TABLE IF NOT EXISTS vahan_retail_monthly (
    period        TEXT NOT NULL,
    segment       TEXT NOT NULL,       -- 'PV' | '2W'
    units         INTEGER NOT NULL CHECK (units >= 0),
    capture_date  TEXT NOT NULL,       -- LATEST capture wins: Vahan backdates,
    PRIMARY KEY (period, segment)      -- so a later read is the more complete one
);
"""


def capture_retail(conn: sqlite3.Connection) -> dict:
    """Vahan all-India monthly registrations per segment — full history, one call each."""
    import vahan
    conn.executescript(RETAIL_DDL)
    today = dt.date.today().isoformat()
    out = {}
    for seg, group in RETAIL_GROUP.items():
        p = vahan._params("", "", vahan.CALENDAR["month"], "2026", "2026")
        p["vehicleCategoryGroup"] = group
        rows = vahan._get(f"{vahan.DASH}/durationWiseRegistrationTable?"
                          + urllib.parse.urlencode(p))
        if not rows:
            # A SEGMENT total cannot genuinely be empty — refuse, keep yesterday.
            raise RuntimeError(f"Vahan returned no rows for {group}; stored retail kept")
        n = 0
        for r in rows:
            per = r.get("yearAsString")
            if not per or "-" not in per:
                continue
            conn.execute("INSERT OR REPLACE INTO vahan_retail_monthly VALUES (?,?,?,?)",
                         (per, seg, int(r.get("registeredVehicleCount") or 0), today))
            n += 1
        out[seg] = n
    conn.commit()
    return out


def channel(conn: sqlite3.Connection, segment: str) -> list[dict]:
    """[{period, wholesale, retail, net}] for months where BOTH sides exist.

    net > 0: dispatches exceeded registrations — dealer stock BUILT.
    net < 0: registrations ran ahead — the channel was DRAWN DOWN.
    """
    conn.executescript(RETAIL_DDL)
    rows = conn.execute(
        "SELECT w.period, w.units, r.units FROM siam_wholesale w "
        "JOIN vahan_retail_monthly r ON r.period = w.period AND r.segment = w.segment "
        "WHERE w.segment = ?", (segment,)).fetchall()
    out = [{"period": p, "wholesale": w, "retail": r, "net": w - r} for p, w, r in rows]
    return sorted(out, key=lambda x: _key(x["period"]))


def report(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT period, segment, units FROM siam_wholesale").fetchall()
    by: dict[str, dict] = {}
    for p, s, u in rows:
        by.setdefault(p, {})[s] = u
    print(f"{len(by)} months of wholesale stored")
    for seg in RETAIL_GROUP:
        ch = channel(conn, seg)
        print("")
        print(f"  {seg} channel ({len(ch)} months): wholesale - retail = into dealer stock")
        for x in ch[-6:]:
            print(f"    {x['period']:16}{x['wholesale']:>11,}{x['retail']:>11,}{x['net']:>+11,}")


def selftest() -> int:
    fails = []

    def check(n, c, d=""):
        print(f"  {'PASS' if c else 'FAIL'}  {n}{'  ' + d if d else ''}")
        if not c:
            fails.append(n)

    base = ("Monthly Performance: September 2025 Production: The total production of "
            "Passenger Vehicles 1 , Three Wheelers, Two Wheelers, and Quadricycle in "
            "September 2025 was 30,73,654 units Domestic Sales: Passenger Vehicles 2 "
            "sales were 3,72,458 units in September 2025. Three-wheeler sales were "
            "84,077units in September 2025 Two-wheeler sales were 21,60,889 units in "
            "September 2025. Performance: July - September 2025 Production: ... "
            "Three-wheeler sales were 2,29,239 units in July-September 2025")
    print("ACCEPTANCE")
    p = parse(base)
    check("period read from the heading", p["period"] == "2025-September", str(p["period"]))
    check("PV", p["PV"] == 372458, str(p["PV"]))
    check("2W", p["2W"] == 2160889, str(p["2W"]))

    print("\nTRAP 1 — a quarter must never be read as a month")
    check("'84,077units' (no space) reads the MONTHLY figure",
          p["3W"] == 84077, f"got {p['3W']:,}" if p["3W"] else "None")
    check("...and NOT the quarterly 229,239 that follows it", p["3W"] != 229239)
    cut = base.replace("84,077units", "units")      # monthly figure absent entirely
    check("with the monthly figure gone, it fails LOUD (None), not to the quarter",
          parse(cut)["3W"] is None, str(parse(cut)["3W"]))

    print("\nTRAP 2 — the source's own typo")
    t2 = base.replace("Two-wheeler sales were 21,60,889 units",
                      "Two-wheeler sales were units 19,02,209 units")
    check("'were units 19,02,209 units' is tolerated", parse(t2)["2W"] == 1902209,
          str(parse(t2)["2W"]))

    print("\nREJECTION")
    check("a non-data release parses to no period", parse("SAFE Annual Convention")["period"] is None)
    mem = sqlite3.connect(":memory:")
    mem.executescript(DDL)
    for i, v in enumerate([70000, 72000, 75000, 69000, 74000, 77000]):
        mem.execute("INSERT INTO siam_wholesale VALUES (?,?,?,?,?)",
                    (f"2026-{MONTHS[i]}", "3W", v, 1, "x"))
    check("plausibility refuses a quarter-sized month (229,239 vs ~73k)",
          _plausible(mem, "2026-July", "3W", 229239) is not None)
    check("...and ACCEPTS a real festive month (+16% on neighbours)",
          _plausible(mem, "2026-July", "3W", 85000) is None)

    print("\nEMPTY-PAGE DETECTION (the bug that dropped two months in three)")
    global _CHROME_LEN
    saved, _CHROME_LEN = _CHROME_LEN, 3557          # fixed so the test needs no network
    check("bare chrome is EMPTY", _is_empty("x" * 3557))
    # THE REGRESSION: an ordinary monthly release measured 4,898 characters.
    # The old fixed threshold (6,000) classified it empty and skipped it, so
    # the backfill kept only quarter-end months and reported success.
    check("a SHORT monthly data release (4,898) is REAL, not empty",
          not _is_empty("x" * 4898))
    check("a quarter-end release (9,585) is REAL", not _is_empty("x" * 9585))
    _CHROME_LEN = saved


    print(f"\n{len(fails)} failure(s)" + (f": {fails}" if fails else ""))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    conn = _conn()
    if a.fetch or a.backfill:
        try:
            got = capture_retail(conn)
            print(f"  retail captured: {got}")
        except Exception as e:                  # noqa: BLE001 - keep stored retail
            print(f"  retail NOT refreshed ({type(e).__name__}: {e}); stored values kept")
        for r in fetch(conn, backfill=a.backfill):
            print(f"  pid {r['pid']}: {r['period']:16} stored {r['stored']}"
                  + (f"  REFUSED {r['refused']}" if r["refused"] else ""))
        report(conn)
        return 0
    if a.report:
        report(conn)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
