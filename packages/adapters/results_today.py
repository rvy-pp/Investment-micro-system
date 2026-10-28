"""Which coverage names report today, and whether during market or after.

PM, 2026-10-28: *"Can we have something in the daily that prompts me the
during market and post market results for companies in our coverage? Just a
daily check once to update for tomorrow. It should be the first line in daily
mail report. Like: during market-a,b,c / post-market-c,d,e"*. So the Overview's
Broker mail section opens with two lines, and the morning-brief skill's chat
read opens with the same two.

THE DATE is NSE's own event calendar (`/api/event-calendar`), the board
meetings companies have intimated to the exchange. Plain urllib, no cookie
dance (the homepage cookie step 403s; the API does not — verified
2026-10-28, the same free ride flow_series gets from indicesHistory). Only
purposes naming "Financial Results" count: Avalon's 27-Oct "Fund Raising"
board meeting sits in the same calendar.

THE TIMING IS NOT PUBLISHED ANYWHERE, so it is the company's own HABIT: the
NSE dissemination time of its last four result filings ("Outcome of Board
Meeting ... financial results", `/api/corporate-announcements`). TCS filed at
15:40 / 15:52 / 15:55 / 15:50 — post-market. APL Apollo filed 12:24 / 14:41 /
13:45 — during market. A habit is a forecast, not a notice, and the page says
"usually". Hindalco went 14:40 / 17:22 / 17:06 and lands post-market on 2 of
3; a tie is printed as "timing unclear", never broken by a guess.

Only filings on a REAL SESSION count toward the habit. APL Apollo's FY26
print (02-May-2026, 15:48) was a Saturday: no market to be during or after.
A session is a date in `fo_oi` (the NSE bhavcopy, which has no phantom
holiday rows, unlike Yahoo — see basket_index) inside its stored range, and a
weekday before it. A weekday holiday before 2025-08-20 would therefore count;
stated, not modelled.

The boundary is the CLOSE, 15:30. A 15:29 filing lands into a live market;
15:30 does not. Before 09:15 is pre-market and gets its own line when it
happens. A name that has already filed today prints its time instead.

Coverage is grading's sector + NSE-symbol map, the Futures-vs-cash roster
(engine.fo_basis_view), so "coverage" means one thing across the Overview.

Display only. Writes data/morning/results_<date>.json and nothing else — no
table, no `results_calendar` row, nothing a pillar reads.

Usage:
    python packages/adapters/results_today.py             # fetch + write + print
    python packages/adapters/results_today.py --date 2026-10-29
    python packages/adapters/results_today.py --selftest
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import pathlib
import sqlite3
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO / "data" / "morning"
DB = REPO / "data" / "ims.db"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CAL_URL = "https://www.nseindia.com/api/event-calendar?index=equities&from_date={f}&to_date={t}"
ANN_URL = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
           "&symbol={s}&from_date={f}&to_date={t}")

OPEN, CLOSE = dt.time(9, 15), dt.time(15, 30)
LATE_NIGHT = dt.time(6, 0)   # before this, a filing is last night's meeting
HABIT_N = 4          # last N session prints make the habit
HISTORY_DAYS = 400   # enough for four quarterly prints and a late one
LOOKAHEAD_DAYS = 7   # the payload carries the week ahead for the hover


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as r:
        b = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            b = gzip.decompress(b)
    return json.loads(b)


def _nse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s.strip(), "%d-%b-%Y").date()


def _ddmmyyyy(d: dt.date) -> str:
    return d.strftime("%d-%m-%Y")


# ------------------------------------------------------------------ pieces
def calendar_rows(raw: list[dict], symbols: set[str]) -> list[dict]:
    """Coverage names' result board meetings, one row per (symbol, date)."""
    out, seen = [], set()
    for x in raw or []:
        sym = (x.get("symbol") or "").strip()
        if sym not in symbols or "financial result" not in (x.get("purpose") or "").lower():
            continue
        try:
            d = _nse_date(x["date"])
        except (KeyError, ValueError):
            continue
        if (sym, d) in seen:
            continue
        seen.add((sym, d))
        out.append({"symbol": sym, "date": d.isoformat(),
                    "purpose": x.get("purpose"), "desc": x.get("bm_desc")})
    return out


def result_filings(raw: list[dict]) -> list[dt.datetime]:
    """Dissemination times of RESULT filings, earliest per calendar day,
    newest first. The outcome notice is the one carrying the results; a
    later same-day "Financial Result Updates" (press release, investor deck)
    must not move the day's time later.

    Syrma never writes "financial results" in the outcome text, only "under
    Regulation 30 and 33" — Reg 33 IS the results regulation. Matching the
    words alone left it "timing unknown" with four prints on file (caught
    2026-10-28 on the first live run)."""
    best: dict[dt.date, dt.datetime] = {}
    for a in raw or []:
        desc = (a.get("desc") or "").lower()
        text = (a.get("attchmntText") or "").lower()
        if not ("outcome of board meeting" in desc
                and ("financial result" in text or "regulation 30 and 33" in text
                     or "regulations 30 and 33" in text or "regulation 33" in text)):
            continue
        try:
            t = dt.datetime.strptime(a.get("an_dt") or a.get("sort_date"), "%d-%b-%Y %H:%M:%S")
        except (TypeError, ValueError):
            try:
                t = dt.datetime.strptime(a.get("sort_date"), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
        d = meeting_day(t)
        if d not in best or t < best[d]:
            best[d] = t
    return sorted(best.values(), reverse=True)


def meeting_day(t: dt.datetime) -> dt.date:
    """The day the board met. A filing before LATE_NIGHT is the previous
    evening's meeting running past midnight: Amber filed 00:40 Fri 14-Aug,
    02:26 Sat 16-May and 00:22 Tue 10-Feb, Mphasis 00:01 Fri 31-Oct. Read on
    the stamp's own date, Amber came out "pre-market" 2 of 3 — the opposite
    of what happened (nobody saw those numbers before the previous close)."""
    return t.date() - dt.timedelta(days=1) if t.time() < LATE_NIGHT else t.date()


def classify(t: dt.datetime, is_session) -> str | None:
    """'pre' / 'during' / 'post', or None when there was no session that day.
    After midnight counts as post-market of the meeting day (meeting_day)."""
    if not is_session(meeting_day(t)):
        return None
    if t.time() < LATE_NIGHT:
        return "post"
    if t.time() < OPEN:
        return "pre"
    return "during" if t.time() < CLOSE else "post"


def habit(filings: list[dt.datetime], is_session, n: int = HABIT_N) -> dict:
    """The label from the last n SESSION prints: a strict majority, else
    'unclear'. No session prints at all is 'unknown' — never a default."""
    hist, counts = [], {}
    for t in filings:
        c = classify(t, is_session)
        if c is None:
            continue
        hist.append({"date": meeting_day(t).isoformat(), "time": t.strftime("%H:%M"), "when": c})
        counts[c] = counts.get(c, 0) + 1
        if len(hist) == n:
            break
    if not hist:
        return {"when": "unknown", "history": [], "counts": {}}
    top = max(counts, key=counts.get)
    when = top if counts[top] * 2 > len(hist) else "unclear"
    return {"when": when, "history": hist, "counts": counts}


def session_test(conn: sqlite3.Connection | None):
    """date -> bool. fo_oi dates inside its range; a weekday outside it."""
    days, lo, hi = set(), None, None
    if conn is not None:
        try:
            days = {r[0] for r in conn.execute("SELECT DISTINCT date FROM fo_oi")}
            if days:
                lo, hi = min(days), max(days)
        except sqlite3.Error:
            days = set()

    def is_session(d: dt.date) -> bool:
        iso = d.isoformat()
        if lo and lo <= iso <= hi:
            return iso in days
        return d.weekday() < 5
    return is_session


def roster(conn: sqlite3.Connection) -> list[dict]:
    """Coverage = grading's sector + NSE-symbol map, the Futures-vs-cash list."""
    sys.path.insert(0, str(REPO / "packages" / "book"))
    sys.path.insert(0, str(REPO / "packages" / "api"))
    import grading
    cfg = grading.load_config(conn)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM entities")}
    try:
        import engine
        ents, _u, _f = engine.load_specs()
        names.update({k: v.get("name") for k, v in ents.items() if v.get("name")})
    except Exception:
        pass
    return [{"id": eid, "symbol": sym, "sector": cfg["sector_of"].get(eid, "other"),
             "name": names.get(eid) or eid} for eid, sym in cfg["symbol_of"].items()]


def build(day: dt.date, ros: list[dict], cal_raw: list[dict], ann_of, is_session) -> dict:
    """The payload. `ann_of(symbol) -> raw announcements` is injected so the
    selftest runs offline."""
    by_sym = {r["symbol"]: r for r in ros}
    rows = calendar_rows(cal_raw, set(by_sym))
    today = [r for r in rows if r["date"] == day.isoformat()]
    out_rows, errors = [], []
    for r in today:
        meta = by_sym[r["symbol"]]
        try:
            fil = result_filings(ann_of(r["symbol"]))
        except Exception as e:  # noqa: BLE001 — one name's fetch must not lose the rest
            errors.append(f"{r['symbol']}: announcements fetch failed ({e})")
            fil = []
        filed = [t for t in fil if meeting_day(t) == day]
        h = habit([t for t in fil if meeting_day(t) < day], is_session)
        out_rows.append({**r, "entity": meta["id"], "name": meta["name"], "sector": meta["sector"],
                         "when": h["when"], "history": h["history"], "counts": h["counts"],
                         "filed_at": filed[-1].strftime("%H:%M") if filed else None})
    out_rows.sort(key=lambda x: x["name"])
    groups: dict[str, list[str]] = {}
    for x in out_rows:
        groups.setdefault(x["when"], []).append(x["name"])
    ahead = [{"date": r["date"], "name": by_sym[r["symbol"]]["name"], "symbol": r["symbol"]}
             for r in rows if day.isoformat() < r["date"]]
    ahead.sort(key=lambda x: (x["date"], x["name"]))
    return {"date": day.isoformat(), "session": bool(is_session(day)) if day.weekday() < 5 else False,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": "NSE event calendar (date) + NSE result-filing times, last "
                      f"{HABIT_N} session prints (timing)",
            "n_coverage": len(ros), "groups": groups, "rows": out_rows,
            "ahead": ahead, "errors": errors}


def run(day: dt.date) -> dict:
    conn = sqlite3.connect(DB)
    try:
        ros = roster(conn)
        is_session = session_test(conn)
    finally:
        conn.close()
    errors = []
    try:
        cal = _get(CAL_URL.format(f=_ddmmyyyy(day), t=_ddmmyyyy(day + dt.timedelta(days=LOOKAHEAD_DAYS))))
    except Exception as e:  # noqa: BLE001
        cal, errors = [], [f"NSE event calendar fetch failed ({e})"]
    frm = _ddmmyyyy(day - dt.timedelta(days=HISTORY_DAYS))
    doc = build(day, ros, cal,
                lambda s: _get(ANN_URL.format(s=s, f=frm, t=_ddmmyyyy(day))), is_session)
    doc["errors"] = errors + doc["errors"]
    return doc


def line(doc: dict) -> str:
    g = doc.get("groups") or {}
    if doc.get("errors") and not doc.get("rows"):
        return "Results today: NOT CHECKED — " + "; ".join(doc["errors"])
    if not doc.get("rows"):
        return "Results today: none in coverage"
    lab = [("pre", "Pre-market"), ("during", "During market"), ("post", "Post-market"),
           ("unclear", "Timing unclear"), ("unknown", "Timing unknown")]
    return "\n".join(f"{t}: {', '.join(g[k])}" for k, t in lab if g.get(k))


# ---------------------------------------------------------------- selftest
def selftest() -> int:
    fails = 0

    def ok(cond, msg):
        nonlocal fails
        print(("  ok   " if cond else "  FAIL ") + msg)
        fails += 0 if cond else 1

    wk = lambda d: d.weekday() < 5  # noqa: E731
    T = lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M")  # noqa: E731
    # the close boundary, both sides
    ok(classify(T("2026-10-08 15:29"), wk) == "during", "15:29 on a session is during market")
    ok(classify(T("2026-10-08 15:30"), wk) == "post", "15:30 is post-market")
    ok(classify(T("2026-10-08 09:14"), wk) == "pre", "09:14 is pre-market")
    ok(classify(T("2026-08-14 00:40"), wk) == "post", "00:40 is the previous evening's post-market")
    ok(classify(T("2026-08-17 00:40"), wk) is None, "00:40 Monday is a Sunday meeting: no session")
    ok(result_filings([{"desc": "Outcome of Board Meeting", "attchmntText": "financial results",
                        "an_dt": "14-Aug-2026 00:40:00"}])[0] == T("2026-08-14 00:40")
       and meeting_day(T("2026-08-14 00:40")) == dt.date(2026, 8, 13), "after midnight dates to the meeting")
    ok(classify(T("2026-10-08 09:15"), wk) == "during", "09:15 is during market")
    ok(classify(T("2026-05-02 15:48"), wk) is None, "a Saturday filing has no session")
    hol = lambda d: d != dt.date(2026, 10, 2) and d.weekday() < 5  # noqa: E731
    ok(classify(T("2026-10-02 12:00"), hol) is None, "a weekday holiday has no session")

    # the habit: majority, tie, skip non-sessions, unknown
    h = habit([T("2026-10-08 15:40"), T("2026-07-09 15:52"), T("2026-04-09 15:55"),
               T("2026-01-12 15:50"), T("2025-10-09 12:00")], wk)
    ok(h["when"] == "post" and len(h["history"]) == 4, "TCS-like: four post prints, fifth ignored")
    h = habit([T("2026-10-26 12:24"), T("2026-08-01 14:41"), T("2026-05-02 15:48"),
               T("2026-01-22 13:45")], wk)
    # The fixture was wrong on its first run and the code was right: it
    # expected 3, forgetting that 01-Aug-2026 is ALSO a Saturday. APL Apollo
    # really did file two of its last four prints on a weekend.
    ok(h["when"] == "during" and len(h["history"]) == 2, "APL-like: both Saturday prints skipped, during")
    h = habit([T("2026-08-07 14:40"), T("2026-05-22 17:22"), T("2026-02-12 17:06")], wk)
    ok(h["when"] == "post" and h["counts"] == {"during": 1, "post": 2}, "Hindalco-like: 2 of 3 is post")
    h = habit([T("2026-08-07 14:40"), T("2026-05-22 17:22")], wk)
    ok(h["when"] == "unclear", "1-1 tie is unclear, never guessed")
    ok(habit([], wk)["when"] == "unknown", "no history is unknown")

    # filings: only outcome-with-results, earliest per day
    ann = [
        {"desc": "Outcome of Board Meeting", "attchmntText": "X has submitted the financial results",
         "an_dt": "08-Oct-2026 15:40:12"},
        {"desc": "Outcome of Board Meeting", "attchmntText": "X has submitted the financial results",
         "an_dt": "08-Oct-2026 18:02:00"},
        {"desc": "Outcome of Board Meeting", "attchmntText": "approved fund raising",
         "an_dt": "15-Sep-2026 11:00:00"},
        {"desc": "Board Meeting Intimation", "attchmntText": "to consider financial results",
         "an_dt": "20-Sep-2026 10:00:00"},
        {"desc": "Outcome of Board Meeting", "attchmntText": "Outcome of Board Meeting held on "
         "July 29, 2026, under Regulation 30 and 33 read with Part A", "an_dt": "29-Jul-2026 15:29:00"},
    ]
    f = result_filings(ann)
    ok(len(f) == 2 and f[0] == dt.datetime(2026, 10, 8, 15, 40, 12),
       "earliest same-day result filing kept; fund raising and intimation dropped")
    ok(f[1] == dt.datetime(2026, 7, 29, 15, 29), "a Reg 33 outcome with no 'financial results' counts (Syrma)")

    # calendar: coverage and purpose filters, dedupe
    cal = [{"symbol": "TCS", "purpose": "Financial Results", "date": "29-Oct-2026"},
           {"symbol": "TCS", "purpose": "Financial Results/Dividend", "date": "29-Oct-2026"},
           {"symbol": "AVALON", "purpose": "Fund Raising", "date": "29-Oct-2026"},
           {"symbol": "ZZZ", "purpose": "Financial Results", "date": "29-Oct-2026"},
           {"symbol": "INFY", "purpose": "Financial Results", "date": "30-Oct-2026"}]
    rows = calendar_rows(cal, {"TCS", "AVALON", "INFY"})
    ok([(r["symbol"], r["date"]) for r in rows] == [("TCS", "2026-10-29"), ("INFY", "2026-10-30")],
       "results purpose only, coverage only, one row per name-day")

    # build end to end, offline
    ros = [{"id": "tcs", "symbol": "TCS", "sector": "it", "name": "TCS"},
           {"id": "infy", "symbol": "INFY", "sector": "it", "name": "Infosys"},
           {"id": "avalon", "symbol": "AVALON", "sector": "ems", "name": "Avalon"}]
    hist = {"TCS": [{"desc": "Outcome of Board Meeting", "attchmntText": "financial results",
                     "an_dt": d} for d in ("08-Oct-2026 15:40:12", "09-Jul-2026 15:52:21",
                                           "09-Apr-2026 15:55:34")],
            "INFY": [{"desc": "Outcome of Board Meeting", "attchmntText": "financial results",
                      "an_dt": "29-Oct-2026 13:05:00"}] +
                    [{"desc": "Outcome of Board Meeting", "attchmntText": "financial results",
                      "an_dt": d} for d in ("17-Jul-2026 15:45:00", "16-Apr-2026 15:50:00")]}
    cal2 = cal + [{"symbol": "INFY", "purpose": "Financial Results", "date": "29-Oct-2026"}]
    doc = build(dt.date(2026, 10, 29), ros, cal2, lambda s: hist.get(s, []), wk)
    ok(doc["groups"] == {"post": ["Infosys", "TCS"]}, "both habitually post-market")
    infy = [r for r in doc["rows"] if r["symbol"] == "INFY"][0]
    ok(infy["filed_at"] == "13:05" and len(infy["history"]) == 2,
       "today's own filing is shown, never counted toward the habit")
    ok([a["symbol"] for a in doc["ahead"]] == ["INFY"], "the week ahead rides along")
    ok(line(doc) == "Post-market: Infosys, TCS", "the line reads as the PM asked")

    def boom(_s):
        raise OSError("403")
    doc = build(dt.date(2026, 10, 29), ros, cal2, boom, wk)
    ok(len(doc["errors"]) == 2 and doc["groups"].get("unknown") == ["Infosys", "TCS"],
       "a failed history fetch reads unknown and is named, not dropped")
    ok(line({"rows": [], "groups": {}, "errors": ["calendar 403"]}).startswith("Results today: NOT CHECKED"),
       "a failed calendar is NOT CHECKED, never 'none in coverage'")
    ok(line({"rows": [], "groups": {}, "errors": []}) == "Results today: none in coverage",
       "an empty day says none")

    print(f"\n{'PASS' if not fails else 'FAIL'} — {fails} failure(s)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="ISO date (default today)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    day = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    doc = run(day)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / f"results_{day.isoformat()}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(p)
    print(line(doc))
    for r in doc["rows"]:
        hs = " ".join(f"{h['date'][5:]} {h['time']}" for h in r["history"])
        print(f"  {r['symbol']:<12} {r['when']:<8} {('filed ' + r['filed_at']) if r['filed_at'] else ''} [{hs}]")
    for e in doc["errors"]:
        print("ERROR", e)
    print(f"wrote {p.relative_to(REPO)}")
    return 1 if doc["errors"] and not doc["rows"] else 0


if __name__ == "__main__":
    sys.exit(main())
