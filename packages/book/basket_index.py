"""The Book tab's equal-weighted long and short indices.

PM instruction 2026-09-17, verbatim: *"Create an equal weighted index for
longs, equal for shorts... Keep VAML out, use 2007 onwards. Once VAML starts
to exist, add VAML to the long index. Till 16th sep 2026, we keep the index
same. Post that, as and when the positions change, we change the index on a
daily basis."*

IT REPLACED TWO CHARTS AND NEITHER SHOULD COME BACK. The first was a
portfolio-weighted back-cast of today's roster over a chosen window
(`engine.book_ohlc`); the second read the roster out of each stored snapshot
and so could not start before 2026-09-04 (`engine.book_index`, the first
version). The PM's verdict on having both: *"Let's rebuild the whole thing
again. We get rid of both graph methods."* This is the single method.

WHAT IT IS
----------
Two indices, one a side, each an equal-weighted (1/n) basket of the book's
legs, rebalanced every session, chained from 2007-01-01 with the level at the
first session set to 100. Position SIZE never enters it — the PM asked for
direction only, so an 11.5% position and a 1.8% position count the same.

MEMBERSHIP HAS TWO REGIMES, and the boundary is a spec constant
(`specs/book.yaml index.freeze_before`, 2026-09-16):

  ON OR BEFORE THE FREEZE   the roster of the freeze date's snapshot, held
                            constant. This is the back-cast, and it is
                            survivorship-biased by construction: today's legs
                            run backwards through years in which the book did
                            not hold them. Say so wherever it is rendered.

  AFTER THE FREEZE          the roster of the latest snapshot on or before the
                            PREVIOUS close, so the index follows the book as
                            positions go on and come off.

THE FREEZE DATE MUST STAY A CONSTANT. Deriving it from "the latest snapshot"
would advance it every morning and quietly re-write the whole back-cast to
whatever the book looks like today, which is precisely the bias this split
exists to bound.

A LEG JOINS ON ITS OWN LISTING DATE, not just VAML. THIRTEEN of the thirty-two
list after 2007 — VAML 2026-06-15, Kaynes 2022-11-22, Syrma 2022-08-26, KPIT
2019-04-22, Dalmia 2018-12-21, Amber 2018-01-30, Dixon 2017-09-18, LTTS
2016-09-23, LTIMindtree 2016-07-21, APL Apollo 2011-12-14, Coal India
2010-11-04, Persistent 2010-04-06, NMDC 2008-03-03 — so `n` grows from 9 longs
/ 10 shorts in 2007 to 16 / 16 today. (The count is derived on every render
from `joins`, never written down in the UI: this docstring said "twelve" for
an hour because VAML was being counted as the exception rather than as one of
the thirteen.) The
consequence is real and is reported in `composition`: when a leg joins,
everyone else's weight falls, so this is not a constant-composition index and
must not be read as one.

AND A LEG NEVER CONTRIBUTES A RETURN ON ITS FIRST DAY. It joins at its first
close and counts from the next session. Otherwise a listing-day pop — VAML
listed at a price nobody in the book paid — enters the index as performance.
Same rule as the roster's: what was knowable at the previous close.

WHAT COUNTS AS A SESSION, measured rather than assumed over 4,867 dates
-----------------------------------------------------------------------
QUORUM. A date is a session only if at least `session_quorum` of the legs
listed by then actually printed. 4,863 of 4,867 dates are at 100%; the only
two below 90% are 2010-02-06, a SATURDAY special session that just 1 of 20
listed names carries, and 2026-09-16 at 75%. Nothing sits near the threshold.

PHANTOM SESSIONS. Ten dates carry a bar for a day the exchange was shut:
Yahoo answers with `open == high == low == close` and volume 0. Left in, each
is a guaranteed zero-return session, and the names that did NOT get a
placeholder would then measure the next session over two days while the rest
measured one — the silent-arithmetic shape (docs/SILENT_BUGS.md entry 9). The
test is the tape's own, applied across members, so a single halted stock on a
real session can never take the day out. A holiday calendar was rejected: it
is another thing to maintain and is wrong the first year nobody updates it.

ALL-OR-NOTHING. A session yields a bar only if every LISTED member has a close
on both ends of the interval; otherwise no bar is drawn and the chain runs on
to the next session where they all price, so the interval widens but stays
COMMON to every member. It costs exactly one session in 19.7 years
(2026-09-16, where Yahoo has no bar for eight names — a hole in the source,
not a fetch that failed) and every skip is named. Averaging over whoever
happened to print would move the index on a data gap rather than on a price.

CONFIRMED CORPORATE ACTIONS are dropped from the day's mean — only VEDL's
2026-04-30 demerger qualifies in this window. The allow-list is hand-verified
(`core/corporate_actions`), so an UNVERIFIED jump is still a real market move
and is carried through, which is the rule tape.py set and the reason the
2008 crash and the 2020 COVID moves stay in.

WHY THERE IS NO `book_index` TABLE. The whole series recomputes from `prices`
and `book_positions` in well under a second, so persisting it would only
create a second thing that can be stale. The daily review needs no step for
it: a new snapshot lands in `book_positions` and the index extends itself.

    python packages/book/basket_index.py              # summary
    python packages/book/basket_index.py --selftest
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "core"))

DB = REPO / "data" / "ims.db"
SPEC = REPO / "specs" / "book.yaml"

# Display windows, in calendar days back from the last session. Calendar, not
# row counts: "windows are CALENDAR DAYS, not row counts" is a standing gotcha
# here, and a monthly series N rows back is N months back.
# PM, 2026-09-17: "Lets remove 10 and max, add 6 months, 3 months, 1 month."
# Shortest first, which is the order the buttons render in.
#
# THE INDEX STILL RUNS FROM 2007 — only the VIEWS changed. `build()` chains the
# whole history regardless, the header keeps printing the index LEVELS against
# their 2007 base, and the freeze logic is unaffected. Dropping a window is not
# dropping the data, and the two must not be confused: shortening the chain
# would move the base every time a button changed.
RANGES = {"1mo": 30, "3mo": 92, "6mo": 183, "1y": 365, "2y": 730,
          "3y": 1095, "5y": 1826}

# EVERY WINDOW IS CANDLES; the BAR PERIOD is what changes (PM, 2026-09-17:
# "can you make candles since you have ohlc?"). Long windows used to drop
# open/high/low and draw a line, because a DAILY candle over 19 years is a
# fifth of a pixel wide. The fix is not a line and it is not thinning the
# series — it is the bar period every charting package uses at this scale.
# Measured against the 934px plot area:
#
#     window   daily bars   px/slot        aggregated        px/slot
#     1y              246      3.80        daily                3.80
#     2y              501      1.86        100 weekly           9.34
#     3y              737      1.27        147 weekly           6.35
#     5y            1,234      0.76        247 weekly           3.78
#     10y           2,475      0.38        118 monthly          7.92
#     max           4,855      0.19        231 monthly          4.04
#
# So every window lands between 3.8 and 9.3 px a candle instead of between
# 0.19 and 3.8.
#
# AGGREGATION IS EXACT AND LOSES NOTHING, which is why this beats thinning:
# open = the period's FIRST open, high = MAX high, low = MIN low, close = the
# period's LAST close. Every session in the window still contributes. Dropping
# every fifth bar would have been a silent lie about the path.
#
# THE BOUND SURVIVES IT. A daily high here is already an upper bound on the
# index's true high (the legs do not print their highs simultaneously); the
# max of upper bounds over a week is still an upper bound on the week's high,
# and likewise the min for the low. Aggregating neither tightens nor loosens
# the claim — so the wick means exactly what it meant daily.
DAILY_MAX_BARS = 320        # above this, aggregate to weeks
WEEKLY_MAX_BARS = 1600      # above this, aggregate to months

BASE_LEVEL = 100.0


def _spec() -> dict:
    import yaml
    cfg = yaml.safe_load(SPEC.read_text(encoding="utf-8")) or {}
    ix = cfg.get("index") or {}
    return {"start": ix.get("start", "2007-01-01"),
            "freeze": ix.get("freeze_before", "2026-09-16"),
            "quorum": float(ix.get("session_quorum", 0.5)),
            "tmap": cfg.get("ticker_map") or {},
            "names": cfg.get("names") or {}}


def _read(conn, spec) -> tuple:
    """Rosters per snapshot, bars, volumes — everything the chain needs."""
    tmap, names = spec["tmap"], spec["names"]
    snaps = [r[0] for r in conn.execute(
        "SELECT snap_date FROM book_snapshots ORDER BY snap_date")]

    roster, unmapped = {s: {"L": {}, "S": {}} for s in snaps}, []
    for r in conn.execute("SELECT DISTINCT snap_date, root, side "
                          "FROM book_positions ORDER BY snap_date, root"):
        tok = (r["root"].split() or [""])[0]
        eid = tmap.get(tok) or tmap.get(r["root"])
        if not eid:
            unmapped.append(tok)
            continue
        roster.setdefault(r["snap_date"], {"L": {}, "S": {}})[
            r["side"]][eid] = names.get(tok, tok)

    ents = sorted({e for s in roster.values() for sd in s.values() for e in sd})
    bars, vols = {}, {}
    for eid in ents:
        bars[eid], vols[eid] = {}, {}
        for r in conn.execute(
                "SELECT date, open, high, low, close, volume FROM prices "
                "WHERE entity_id=? AND date>=? AND close IS NOT NULL "
                "ORDER BY date", (eid, spec["start"])):
            bars[eid][r["date"]] = (r["open"], r["high"], r["low"], r["close"])
            vols[eid][r["date"]] = r["volume"]
    return snaps, roster, bars, vols, sorted(set(unmapped))


def sessions(bars, vols, first, quorum) -> tuple[list, list, list]:
    """Split every dated row into real sessions, phantoms and non-quorum dates.

    Both filters are measured, not listed — see the module docstring. They are
    independent tests and the order between them does not change the outcome,
    so each is reported on its own rather than one masking the other.
    """
    alld = sorted({d for b in bars.values() for d in b})
    phantom, thin, live = [], [], []
    for d in alld:
        on = [e for e in bars if d in bars[e]]
        listed = [e for e in bars if first.get(e) and first[e] <= d]
        # QUORUM: was the market open for this book at all?
        if listed and len(on) / len(listed) < quorum:
            thin.append({"date": d, "printed": len(on), "listed": len(listed)})
            continue

        def _flat(e):
            o, h, lo, c = bars[e][d]
            v = vols[e].get(d)
            return (None not in (o, h, lo) and o == h == lo == c
                    and v is not None and v <= 0)

        # PHANTOM: no range and no volume on every member that priced.
        if len(on) >= 2 and all(_flat(e) for e in on):
            phantom.append({"date": d, "printed": len(on)})
            continue
        live.append(d)
    return live, phantom, thin


def chain(side, grid, roster_at, bars, first, actions, skipped, dropped):
    """Equal-weighted, daily-rebalanced index for one side. Level starts 100.

    Returns absolute LEVELS, not percentages. The percent a chart shows
    depends on the window it is rebased to, and mixing the two is how an axis
    gap gets printed as a return.
    """
    lvl, out, base, pending = BASE_LEVEL, [], grid[0], []
    # The base bar's `n` is the LISTED count, not the roster count. It read 16
    # on 2007-01-02 when nine of the sixteen longs had not floated yet — the
    # one number on the whole chart that would have overstated the breadth of
    # the index at its own start, and it feeds `composition`, which exists
    # precisely to make the breadth visible.
    out.append({"d": base, "o": None, "h": None, "l": None, "c": lvl,
                "n": sum(1 for e in roster_at(base, side)
                         if first.get(e) and first[e] <= base),
                "span": base})
    for day in grid[1:]:
        mem = roster_at(base, side)
        # NOT-YET-LISTED IS NOT A GAP. A leg with no price because it had not
        # floated yet is legitimately absent and must not hold the session
        # hostage; a leg that HAS listed and is missing is a data gap and must.
        listed = {e: n for e, n in mem.items()
                  if first.get(e) and first[e] <= base}
        missing = sorted(n for e, n in listed.items()
                         if base not in bars[e] or day not in bars[e])
        if missing:
            skipped.append({"date": day, "side": side, "missing": missing,
                            "of": len(listed)})
            # Remember it for the NEXT bar. A skipped session leaves no bar,
            # so `span` alone cannot reveal it: span is the previous BAR's
            # date on every ordinary bar too, which made a browser-side
            # comparison print "spans from" on all 4,855 of them and then, once
            # corrected, on none. The bar that absorbs the gap has to be
            # told, here, where the gap is actually known.
            pending.append(day)
            continue                     # chain on; do NOT advance the base
        use = []
        for e in sorted(listed):
            if day in (actions.get(e) or {}):
                dropped.append({"date": day, "side": side, "name": listed[e]})
                continue
            use.append(e)
        if not use:
            base = day
            continue
        w = 1.0 / len(use)               # EQUAL WEIGHT, 1/n, every session
        # A bar is candled only if EVERY member has a full bar. Substituting a
        # leg's close for its own open/high/low would flatten the index's wick
        # toward that leg while still drawing it at full ink.
        full = all(None not in bars[e][day][:3] for e in use)
        o = h = lo = c = 0.0
        for e in use:
            po, ph, pl, pc = bars[e][day]
            b0 = bars[e][base][3]
            c += w * (pc / b0)
            if full:
                o += w * (po / b0)
                h += w * (ph / b0)
                lo += w * (pl / b0)
        nl = lvl * c
        out.append({"d": day, "span": base, "n": len(use),
                    "o": (lvl * o if full else None),
                    "h": (lvl * h if full else None),
                    "l": (lvl * lo if full else None), "c": nl,
                    **({"skipped": list(pending)} if pending else {})})
        lvl, base, pending = nl, day, []
    return out


def build(conn: sqlite3.Connection | None = None, rng: str = "5y") -> dict:
    """The whole payload: both indices, the ratio, and every exclusion named."""
    if rng not in RANGES:
        return {"error": f"unknown range {rng!r}; known: "
                         + ", ".join(RANGES)}
    from corporate_actions import CONFIRMED_ACTIONS

    own = conn is None
    if own:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
    spec = _spec()
    snaps, roster, bars, vols, unmapped = _read(conn, spec)
    if own:
        conn.close()
    if not snaps:
        return {"error": "no IMS snapshot loaded — run the daily_review skill"}

    freeze = spec["freeze"]
    at_freeze = [s for s in snaps if s <= freeze]
    if not at_freeze:
        return {"error": f"no snapshot on or before the freeze date {freeze}"}
    frozen = roster[at_freeze[-1]]

    def roster_at(day, side):
        """The book as it was KNOWN TO BE HELD at the close of `day`."""
        if day <= freeze:
            return frozen[side]
        prior = [s for s in snaps if s <= day]
        return roster[prior[-1]][side] if prior else frozen[side]

    first = {e: min(bars[e]) for e in bars if bars[e]}
    # A LEG IN THE ROSTER WITH NO PRICE AT ALL IS A SILENT EXCLUSION, and
    # it is the one failure this design cannot notice by itself: `listed`
    # skips it, so no session is held up and no skip is recorded — the leg
    # simply never appears in the index. It happens when a ticker maps to
    # an entity that has no symbol in yahoo_prices.CANDIDATES. Reported so
    # a newly entered position cannot quietly sit outside the index.
    no_history = sorted({n for side in ("L", "S")
                         for e, n in roster_at(snaps[-1], side).items()
                         if e not in first})
    live, phantom, thin = sessions(bars, vols, first, spec["quorum"])
    if len(live) < 2:
        return {"error": "not enough sessions since " + spec["start"],
                "phantom_sessions": phantom, "thin_sessions": thin}

    skipped, dropped = [], []
    L = chain("L", live, roster_at, bars, first, CONFIRMED_ACTIONS,
              skipped, dropped)
    S = chain("S", live, roster_at, bars, first, CONFIRMED_ACTIONS,
              skipped, dropped)

    # ---- the ratio. Both levels are strictly positive (every factor in the
    # product is a price relative and a price cannot go below zero), so this
    # never divides by zero. It is a RATIO and therefore a true percent; the
    # pp difference of the two indices is a different quantity and is labelled
    # as such wherever it appears.
    smap = {b["d"]: b for b in S}
    ratio = []
    for b in L:
        s = smap.get(b["d"])
        if not s or s["c"] <= 0:
            continue
        r = {"d": b["d"], "c": b["c"] / s["c"] * BASE_LEVEL,
             "o": None, "h": None, "l": None, "span": b["span"]}
        if None not in (b["o"], b["h"], b["l"], s["o"], s["h"], s["l"]) \
                and min(s["o"], s["h"], s["l"]) > 0:
            r["o"] = b["o"] / s["o"] * BASE_LEVEL
            # the CROSS pairing: the ratio is highest with the longs at their
            # high AND the shorts at their low, so this is a bound built from
            # two quantities that are already bounds.
            r["h"] = b["h"] / s["l"] * BASE_LEVEL
            r["l"] = b["l"] / s["h"] * BASE_LEVEL
        ratio.append(r)

    # ---- composition: when each leg joined, and n over time. This is what
    # stops the growing membership being invisible.
    joins = []
    for side in ("L", "S"):
        for e, n in frozen[side].items():
            if e in first:
                joins.append({"date": first[e], "name": n, "side": side})
    joins.sort(key=lambda j: (j["date"], j["name"]))
    comp, prev = [], None
    for b in L:
        pair = (b["n"], smap.get(b["d"], {}).get("n"))
        if pair != prev:
            comp.append({"date": b["d"], "long": pair[0], "short": pair[1]})
            prev = pair

    # membership changes after the freeze — the live half of the series
    changes = []
    post = [s for s in snaps if s > freeze]
    for i, s in enumerate(post):
        pr = roster[post[i - 1]] if i else frozen
        for side in ("L", "S"):
            for e in sorted(set(roster[s][side]) - set(pr[side])):
                changes.append({"date": s, "side": side, "event": "in",
                                "name": roster[s][side][e]})
            for e in sorted(set(pr[side]) - set(roster[s][side])):
                changes.append({"date": s, "side": side, "event": "out",
                                "name": pr[side][e]})

    return {"long": L, "short": S, "ratio": ratio,
            "as_of": live[-1], "start": live[0], "n_sessions": len(live),
            "freeze": freeze, "snapshot": snaps[-1], "snapshots": len(snaps),
            "joins": joins, "composition": comp, "changes": changes,
            "phantom_sessions": phantom, "thin_sessions": thin,
            "skipped_sessions": skipped, "actions_dropped": dropped,
            "unmapped": unmapped, "no_history": no_history,
            "base_level": BASE_LEVEL}


def aggregate(bars: list, period: str) -> list:
    """Roll daily level bars up to weekly or monthly ones. Exact, not sampled.

    open  = the period's FIRST open      high = MAX high
    close = the period's LAST close      low  = MIN low

    Every session contributes, so nothing is thinned away — and the period's
    high is still the bound the daily highs were (max of upper bounds is an
    upper bound), so the wick keeps exactly the meaning it had.

    THE BAR IS DATED BY ITS LAST SESSION, not its first. A monthly candle
    labelled 2020-03-31 closed on 2020-03-31; labelling it 2020-03-01 would
    date a close to before most of the moves inside it. `from` carries the
    period's first session so the tooltip can state the span.

    A bar whose `o`/`h`/`l` are None contributes only its close — that is the
    index's own first bar, which is the base level and has no move in it. Its
    close IS its open there, so the period opens at the right level rather
    than at the second session's open.
    """
    if period == "daily" or not bars:
        return bars

    def key(d):
        y, m, dd = (int(x) for x in d.split("-"))
        if period == "monthly":
            return (y, m)
        return dt.date(y, m, dd).isocalendar()[:2]      # ISO year, ISO week

    out, cur, ck = [], [], None
    for b in bars:
        k = key(b["d"])
        if ck is not None and k != ck:
            out.append(_roll(cur))
            cur = []
        cur.append(b)
        ck = k
    if cur:
        out.append(_roll(cur))
    return out


def _roll(group: list) -> dict:
    first, last = group[0], group[-1]
    highs = [b["h"] for b in group if b.get("h") is not None]
    lows = [b["l"] for b in group if b.get("l") is not None]
    skipped = [d for b in group for d in (b.get("skipped") or [])]
    row = {"d": last["d"], "from": first["d"], "sessions": len(group),
           # THE OPEN IS THE FIRST BAR'S, and when that bar has none it is the
           # first bar's CLOSE — never the next bar's open. Only the index's
           # own base bar lacks an open, and its close IS the level the period
           # started at; falling through to the second session's open instead
           # discards the first session's move entirely. Caught by the
           # selftest, which is the only place it could have been: on the real
           # data it shifts one monthly candle out of 237 and the chart still
           # looks perfectly sensible.
           "o": (first["o"] if first.get("o") is not None else first["c"]),
           "h": (max(highs) if highs else None),
           "l": (min(lows) if lows else None),
           "c": last["c"],
           "n": last.get("n"), "span": last.get("span")}
    if skipped:
        row["skipped"] = skipped
    return row


def window(payload: dict, rng: str) -> dict:
    """Slice the index to a display window and rebase it to the window's start.

    THE INDEX HAS ONE BASE (its first session, level 100); a CHART has a
    window. Rebasing here rather than in the chain keeps those separate: the
    level is a fact about the series, the percentage is a fact about the view.
    Two windows of the same index must never disagree about what happened.
    """
    if payload.get("error"):
        return payload
    if rng not in RANGES:
        # a clean refusal, not a KeyError: window() is called directly by the
        # selftest and by anything that already holds a payload, so it cannot
        # rely on build() having screened the range first.
        return {"error": f"unknown range {rng!r}; known: " + ", ".join(RANGES)}
    days = RANGES[rng]
    cut = None
    if days is not None:
        cut = (dt.date.fromisoformat(payload["as_of"])
               - dt.timedelta(days=days)).isoformat()

    out = dict(payload)
    out["range"] = rng
    out["ranges"] = list(RANGES)

    # the period is chosen ONCE, off the long index, so all three charts share
    # a bar period and an x axis. Choosing per series would let the ratio be
    # weekly while the indices were monthly, and the three would stop lining up.
    probe = payload["long"]
    if cut:
        probe = [b for b in probe if b["d"] >= cut]
    period = ("daily" if len(probe) <= DAILY_MAX_BARS else
              "weekly" if len(probe) <= WEEKLY_MAX_BARS else "monthly")
    out["period"] = period
    out["sessions_per_bar"] = 1 if period == "daily" else None

    n = None
    for key in ("long", "short", "ratio"):
        bars = payload[key]
        if cut:
            head = [b for b in bars if b["d"] < cut]
            bars = [b for b in bars if b["d"] >= cut]
            # BASE = the last close BEFORE the window, so the first bar of the
            # chart is a real session's move and not a pin at zero.
            base = head[-1]["c"] if head else (bars[0]["c"] if bars else None)
        else:
            base = bars[0]["c"] if bars else None
        n = len(bars) if n is None else n
        # AGGREGATE ON LEVELS, THEN REBASE — not the other way round. Rebasing
        # first and aggregating percentages would work here only because the
        # base is a constant divisor, and would quietly stop working the moment
        # anything else changed. Levels are the fact; the percentage is the view.
        grouped = aggregate(bars, period)
        pct = []
        for b in grouped:
            row = {"d": b["d"], "n": b.get("n"), "span": b.get("span"),
                   "c": round((b["c"] / base - 1) * 100, 4),
                   "sessions": b.get("sessions"), "from": b.get("from")}
            if b.get("skipped"):
                row["skipped"] = b["skipped"]
            for f in ("o", "h", "l"):
                row[f] = (round((b[f] / base - 1) * 100, 4)
                          if b.get(f) is not None else None)
            pct.append(row)
        out[key] = {"bars": pct, "candles": True, "period": period,
                    "last": (pct[-1]["c"] if pct else None),
                    "base_date": (head[-1]["d"] if cut and head
                                  else (bars[0]["d"] if bars else None)),
                    "level_last": (round(bars[-1]["c"], 4) if bars else None),
                    "n": (bars[-1].get("n") if bars else None)}
    out["spread_last"] = (None if out["long"]["last"] is None
                          or out["short"]["last"] is None
                          else round(out["long"]["last"]
                                     - out["short"]["last"], 4))
    out["window_sessions"] = n
    out["window_bars"] = len(out["long"]["bars"])
    return out


# ---------------------------------------------------------------------------
# selftest — every guard in BOTH directions
# ---------------------------------------------------------------------------
# A guard with only a rejection test passes every rejection test, which is how
# the GLOB date CHECK rejected every valid date and shipped
# (docs/SILENT_BUGS.md entry 1). So each case below has a twin that must not
# fire.

def _tape(series, vol=1000.0):
    """{eid: {date: close}} -> (bars, vols). A close of None means NO ROW."""
    bars, vols = {}, {}
    for eid, row in series.items():
        bars[eid], vols[eid] = {}, {}
        for d, c in row.items():
            if c is None:
                continue
            bars[eid][d] = (c, c, c, c)
            vols[eid][d] = vol
    return bars, vols


def _selftest() -> None:
    D = ["2007-01-01", "2007-01-02", "2007-01-03", "2007-01-04"]
    d0, d1, d2, d3 = D

    def run(series, frozen, snaps=None, freeze=d3, actions=None, vols=None,
            quorum=0.5):
        bars, v = _tape(series)
        if vols:
            for e, row in vols.items():
                v[e].update(row)
        first = {e: min(bars[e]) for e in bars if bars[e]}
        live, ph, thin = sessions(bars, v, first, quorum)
        sk, dr = [], []
        roster_at = lambda day, side: frozen[side]          # noqa: E731
        out = {s: chain(s, live, roster_at, bars, first, actions or {}, sk, dr)
               for s in ("L", "S")}
        return out, live, ph, thin, sk, dr

    def one(names):
        return {"L": {n: n for n in names}, "S": {}}

    # -- 1. EQUAL WEIGHTING, hand-computed, level starts at 100 -------------
    # A 100->120->120, B 100->80->100, rebalanced daily:
    #   d1  mean(1.20, 0.80) = 1.000 -> 100.0
    #   d2  mean(1.00, 1.25) = 1.125 -> 112.5
    S = {"A": {d0: 100, d1: 120, d2: 120},
         "B": {d0: 100, d1: 80,  d2: 100}}
    out, live, *_ = run(S, one("AB"))
    assert [round(b["c"], 4) for b in out["L"]] == [100.0, 100.0, 112.5], \
        out["L"]
    assert out["L"][0]["o"] is None                     # the base bar

    # -- 2. ALL-OR-NOTHING, and it must CHANGE the answer -------------------
    # B unpriced on d1: d1 is skipped and d2 chains from d0 —
    #   mean(1.20, 1.00) = 1.100 -> 110.0, NOT the 112.5 above. Averaging over
    #   whoever printed would give 112.5 here too and the gap would not show.
    S2 = {"A": {d0: 100, d1: 120, d2: 120},
          "B": {d0: 100, d1: None, d2: 100}}
    out, live, ph, thin, sk, dr = run(S2, one("AB"))
    assert [round(b["c"], 4) for b in out["L"]] == [100.0, 110.0], out["L"]
    assert out["L"][-1]["span"] == d0
    assert [(x["date"], x["missing"]) for x in sk] == [(d1, ["B"])], sk
    # THE BAR THAT ABSORBS THE GAP MUST SAY SO. A skipped session leaves no
    # bar, so nothing downstream can infer it from `span` — the marker is the
    # only record a reader ever sees.
    assert out["L"][-1]["skipped"] == [d1], out["L"][-1]
    out, *_r = run(S, one("AB"))                        # acceptance twin
    assert _r[3] == [] and len(out["L"]) == 3
    assert all("skipped" not in b for b in out["L"]), out["L"]

    # -- 3. A LEG JOINS AT ITS LISTING AND CONTRIBUTES FROM THE NEXT DAY ----
    # B lists on d1 at 100 and doubles on d2. Its LISTING-DAY presence must
    # not create a return (d1 is A alone), and d2 must include it.
    S3 = {"A": {d0: 100, d1: 110, d2: 110, d3: 110},
          "B": {d1: 100, d2: 200, d3: 200}}
    out, live, ph, thin, sk, dr = run(S3, one("AB"))
    b = {x["d"]: x for x in out["L"]}
    assert b[d1]["n"] == 1 and round(b[d1]["c"], 4) == 110.0, b[d1]
    #   d2: A 1.0, B 2.0 -> mean 1.5 -> 110 * 1.5 = 165
    assert b[d2]["n"] == 2 and round(b[d2]["c"], 4) == 165.0, b[d2]
    # REJECTION TWIN, and this is the one that matters: B has no price on d0
    # because it had NOT LISTED, which must not be read as a data gap. If
    # not-yet-listed and missing were conflated, d1 would be skipped and the
    # index would start three sessions late for every one of the twelve legs
    # that listed after 2007.
    assert sk == [], sk
    assert [x["d"] for x in out["L"]] == [d0, d1, d2, d3], out["L"]

    # -- 4. PHANTOM SESSIONS -----------------------------------------------
    S4 = {"A": {d0: 100, d1: 100, d2: 110},
          "B": {d0: 100, d1: 100, d2: 90}}
    out, live, ph, thin, sk, dr = run(
        S4, one("AB"), vols={"A": {d1: 0.0}, "B": {d1: 0.0}})
    assert [x["date"] for x in ph] == [d1], ph
    assert [x["d"] for x in out["L"]] == [d0, d2]
    # REJECTION (a): flat for everyone but the tape TRADED — a real dull day
    out, live, ph, *_ = run(S4, one("AB"))
    assert ph == [] and [x["d"] for x in out["L"]] == [d0, d1, d2]
    # REJECTION (b): ONE name halted on a day the other traded
    S5 = {"A": {d0: 100, d1: 105, d2: 110},
          "B": {d0: 100, d1: 100, d2: 90}}
    out, live, ph, *_ = run(S5, one("AB"), vols={"B": {d1: 0.0}})
    assert ph == []

    # -- 5. THE QUORUM ------------------------------------------------------
    # d1 carries 1 of 3 listed names — the 2010-02-06 Saturday shape.
    S6 = {"A": {d0: 100, d1: 105, d2: 110},
          "B": {d0: 100, d1: None, d2: 90},
          "C": {d0: 100, d1: None, d2: 95}}
    out, live, ph, thin, sk, dr = run(S6, one("ABC"))
    assert [x["date"] for x in thin] == [d1], thin
    assert d1 not in [x["d"] for x in out["L"]]
    # and because d1 is not a session at all, it is NOT reported as a skip
    assert sk == [], sk
    # REJECTION twin: 2 of 3 printing clears a 0.5 quorum, so d1 IS a session
    # — and is then skipped by all-or-nothing, which is a different statement
    S7 = {"A": {d0: 100, d1: 105, d2: 110},
          "B": {d0: 100, d1: 102, d2: 90},
          "C": {d0: 100, d1: None, d2: 95}}
    out, live, ph, thin, sk, dr = run(S7, one("ABC"))
    assert thin == [] and [x["date"] for x in sk] == [d1], (thin, sk)

    # -- 6. AN INDEX LEVEL CANNOT GO NEGATIVE ------------------------------
    S8 = {"A": {d0: 100, d1: 50, d2: 25, d3: 0.25}}
    out, *_ = run(S8, one("A"))
    lv = [round(b["c"], 4) for b in out["L"]]
    assert lv == [100.0, 50.0, 25.0, 0.25], lv
    assert all(x > 0 for x in lv)

    # -- 7. A CONFIRMED ACTION IS NOT A RETURN; AN UNVERIFIED JUMP IS ------
    S9 = {"A": {d0: 100, d1: 100, d2: 110},
          "B": {d0: 100, d1: 100, d2: 35}}
    out, live, ph, thin, sk, dr = run(S9, one("AB"), actions={"B": {d2: "x"}})
    b = {x["d"]: x for x in out["L"]}
    assert b[d2]["n"] == 1 and round(b[d2]["c"], 4) == 110.0, b[d2]
    assert dr and dr[0]["name"] == "B"
    out, live, ph, thin, sk, dr = run(S9, one("AB"))     # rejection twin
    b = {x["d"]: x for x in out["L"]}
    assert b[d2]["n"] == 2 and b[d2]["c"] < 80, b[d2]

    # -- 8. WINDOWING REBASES; IT NEVER RE-CHAINS --------------------------
    # The same index read over two windows must agree about what happened
    # between two dates, even though the windows rebase on different bars.
    # Levels 100 -> 105 -> 110 -> 115 -> 121 -> 127 -> 133.1 over three months.
    W = ["2026-01-05", "2026-01-19", "2026-02-02", "2026-02-16",
         "2026-03-02", "2026-03-16", "2026-03-30"]
    LV = [100, 105, 110, 115, 121, 127, 133.1]
    SW = {"A": dict(zip(W, LV))}
    outw, livew, *_ = run(SW, one("A"))
    pay = {"long": outw["L"], "short": outw["L"], "ratio": outw["L"],
           "as_of": livew[-1], "start": livew[0], "n_sessions": len(livew)}
    wide = window(dict(pay), "1y")     # covers everything, base = the first bar
    near = window(dict(pay), "1mo")    # cuts, base = the close BEFORE it
    assert wide["long"]["base_date"] == W[0], wide["long"]["base_date"]
    assert near["long"]["base_date"] == W[3], near["long"]["base_date"]
    assert len(wide["long"]["bars"]) == 7 and len(near["long"]["bars"]) == 3,         (len(wide["long"]["bars"]), len(near["long"]["bars"]))
    # THE LEVELS ARE EXACT AND THE DISPLAYED PERCENTAGES ARE NOT, and this
    # test has to respect the difference or it measures the wrong thing. The
    # first draft compared moves reconstructed from the plotted `c` values and
    # failed at 4.80315 vs 4.803106 — which is not a disagreement about the
    # index, it is what dividing two numbers already rounded to 4dp for display
    # does. The same caution the chain takes with its `raw` levels.
    def move(w, a, b):
        bs = {x["d"]: x["c"] for x in w["long"]["bars"]}
        return ((1 + bs[b] / 100) / (1 + bs[a] / 100) - 1) * 100
    for a, b in ((W[5], W[6]), (W[4], W[6]), (W[4], W[5])):
        d = abs(move(wide, a, b) - move(near, a, b))
        assert d < 1e-3, (a, b, d)          # display rounding only, ~1e-5
    # exact, off the LEVELS the window never touched
    lv = {x["d"]: x["c"] for x in pay["long"]}
    for a, b in ((W[5], W[6]), (W[4], W[6])):
        assert abs((lv[b] / lv[a] - 1) * 100
                   - move(wide, a, b)) < 1e-3, (a, b)
    assert wide["long"]["level_last"] == near["long"]["level_last"] == 133.1

    # the PM's range set, in order, shortest first — 10y and max are GONE
    assert list(RANGES) == ["1mo", "3mo", "6mo", "1y", "2y", "3y", "5y"],         list(RANGES)
    assert window(dict(pay), "10y").get("error"), "10y must no longer resolve"

    # -- 9. AGGREGATION IS EXACT, AND IT IS NOT SAMPLING -------------------
    # PM, 2026-09-17: "can you make candles since you have ohlc?" — a daily
    # candle over 19 years is a fifth of a pixel, so long windows roll up.
    # The rollup must lose nothing: open = FIRST open, high = MAX, low = MIN,
    # close = LAST close, and every session accounted for.
    week = [{"d": "2026-01-05", "o": 10, "h": 12, "l": 9,  "c": 11, "n": 3,
             "span": "2026-01-02"},                       # Mon
            {"d": "2026-01-06", "o": 11, "h": 18, "l": 10, "c": 17, "n": 3,
             "span": "2026-01-05"},                       # Tue — the high
            {"d": "2026-01-07", "o": 17, "h": 17, "l": 4,  "c": 6,  "n": 4,
             "span": "2026-01-06", "skipped": ["2026-01-06x"]},   # the low
            {"d": "2026-01-12", "o": 6,  "h": 8,  "l": 5,  "c": 7,  "n": 4,
             "span": "2026-01-07"}]                       # the NEXT Mon
    wk = aggregate(week, "weekly")
    assert len(wk) == 2, wk                     # two ISO weeks, not one
    a, b = wk
    assert (a["o"], a["h"], a["l"], a["c"]) == (10, 18, 4, 6), a
    assert a["d"] == "2026-01-07" and a["from"] == "2026-01-05", a
    assert a["sessions"] == 3 and a["n"] == 4   # legs are the CLOSE's, not the open's
    assert a["skipped"] == ["2026-01-06x"]      # skips survive the rollup
    assert (b["o"], b["c"], b["sessions"]) == (6, 7, 1), b
    # NOTHING IS DROPPED: the sessions must add back up.
    assert sum(x["sessions"] for x in wk) == len(week)
    # monthly rolls the same four into ONE bar, open 10 close 7
    mo = aggregate(week, "monthly")
    assert len(mo) == 1 and (mo[0]["o"], mo[0]["h"], mo[0]["l"], mo[0]["c"])         == (10, 18, 4, 7), mo
    # THE BOUND SURVIVES: the period high is >= both open and close, so an
    # aggregated candle can never be drawn inverted.
    for x in wk + mo:
        assert x["h"] >= max(x["o"], x["c"]) and x["l"] <= min(x["o"], x["c"])
    # REJECTION TWIN: 'daily' is a pass-through, byte for byte. If it rolled
    # anything up, a 1y window would silently stop being daily.
    assert aggregate(week, "daily") is week
    # the base bar has no open; its CLOSE is the level the period opened at,
    # so the period must not open at the second bar's open (11 here).
    base_first = aggregate([{"d": "2026-01-05", "o": None, "h": None,
                             "l": None, "c": 100}] + week[1:2], "weekly")
    assert base_first[0]["o"] == 100, base_first

    # -- 10. LONG/SHORT IS A RATIO, NOT THE PP GAP -------------------------
    SB = {"A": {d0: 100, d1: 140}, "B": {d0: 100, d1: 150}}
    bars, v = _tape(SB)
    first = {e: min(bars[e]) for e in bars}
    live, *_ = sessions(bars, v, first, 0.5)
    fr = {"L": {"A": "A"}, "S": {"B": "B"}}
    sk, dr = [], []
    Ls = chain("L", live, lambda d, s: fr[s], bars, first, {}, sk, dr)
    Ss = chain("S", live, lambda d, s: fr[s], bars, first, {}, sk, dr)
    assert round(Ls[-1]["c"], 4) == 140.0 and round(Ss[-1]["c"], 4) == 150.0
    ratio = Ls[-1]["c"] / Ss[-1]["c"] * BASE_LEVEL      # 93.33
    assert abs(ratio - 93.3333) < 1e-3, ratio
    pp = (140 - 100) - (150 - 100)                      # -10 pp
    assert abs((ratio - 100) - pp) > 3                  # they are not the same

    print("basket_index selftest OK — equal weighting at 1/n, all-or-nothing "
          "sessions (accept+reject), listing-day join with no entry-day "
          "return, phantom sessions (accept + flat-but-traded + one halted "
          "name), session quorum (accept+reject), level positivity, confirmed "
          "actions (accept+reject), window rebasing without re-chaining, "
          "exact weekly/monthly aggregation (accept + daily pass-through + "
          "base-bar open), window rebasing across two different bases, the "
          "PM's range set, ratio vs pp gap")


def _summary() -> None:
    p = build()
    if p.get("error"):
        print("ERROR:", p["error"])
        return
    print(f"sessions      : {p['n_sessions']}  {p['start']} -> {p['as_of']}")
    print(f"freeze        : {p['freeze']}  (snapshots: {p['snapshots']}, "
          f"latest {p['snapshot']})")
    print(f"long level    : {p['long'][-1]['c']:.2f}   "
          f"({p['long'][-1]['n']} legs today)")
    print(f"short level   : {p['short'][-1]['c']:.2f}   "
          f"({p['short'][-1]['n']} legs today)")
    print(f"ratio level   : {p['ratio'][-1]['c']:.2f}  (100 = parity at "
          f"{p['start']})")
    print(f"phantom       : {len(p['phantom_sessions'])} "
          f"{[x['date'] for x in p['phantom_sessions']]}")
    print(f"thin (quorum) : {len(p['thin_sessions'])} "
          f"{[x['date'] for x in p['thin_sessions']]}")
    print(f"skipped       : {len(p['skipped_sessions'])} "
          f"{[(x['date'], x['side']) for x in p['skipped_sessions']]}")
    print(f"actions       : {len(p['actions_dropped'])} "
          f"{[(x['date'], x['name']) for x in p['actions_dropped']]}")
    print(f"post-freeze   : {len(p['changes'])} membership change(s)")
    print(f"unmapped      : {p['unmapped'] or 'none'}")
    print("composition (n changes):")
    for c in p["composition"]:
        print(f"   {c['date']}  long {c['long']:2d}  short {c['short']}")
    for r in ("1mo", "6mo", "1y", "5y"):
        w = window(p, r)
        print(f"{r:>4s}: {w['window_bars']:4d} {w['period']:7s} bars over "
              f"{w['window_sessions']:5d} sessions  "
              f"long {w['long']['last']:+8.2f}%  short {w['short']['last']:+8.2f}%"
              f"  L/S {w['ratio']['last']:+8.2f}%  "
              f"({w['spread_last']:+.2f} pp apart)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        _summary()
