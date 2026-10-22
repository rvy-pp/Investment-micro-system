"""The deal list behind an IT stock's case — what was signed, what is
running, what is late, what has rolled off — cross-checked as results near.

PM, 2026-10-13: *"for the IT agents, can we work on the deals as well? What
major deals were active, what were scheduled ... were they expected to be
completed/added or are they delayed, have the older ones gone or not."* Then,
after seeing a straight-line revenue bridge built on assumed tenures: *"just a
list is fine, can be cross checked once quarter is near."*

So this is a LIST, deliberately. The first version turned each deal into a
run-rate (TCV / tenure) and summed a quarter-by-quarter revenue bridge; most of
its inputs were assumptions (tenures, start quarters, a two-quarter BSNL
billing), and a number that precise on inputs that loose is the statistics
the PM keeps rejecting. It was removed the same day. Do not rebuild it.

The agent keeps `data/analysis/cases/<entity>/deals.json`:

  quarters  reported TCV per quarter with what it excludes, and USD revenue —
            the company's own numbers, so book-to-bill is printed beside them.
  deals     one row per named deal or acquisition: client, value as reported,
            when signed, expected start/end if a source gives them, status,
            and a dated, sourced status history.
  next_result  the expected results date. From 21 days before it, the list
            prints CROSS-CHECK DUE: every deal that is not finished needs its
            status re-confirmed against the latest call, filing or note before
            the print — that is when a stale status costs something.

Two calendar checks are always shown, because they need no assumption beyond
a date a source gave: OVERDUE (expected start passed, still not started — the
delay question) and PAST_END (stated end passed, not marked ended or renewed
— has it gone?).

    python packages/book/it_deals.py tcs
    python packages/book/it_deals.py --selftest

Reads only deals.json; writes nothing. Nothing here reaches ims.db.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
CASES = REPO / "data" / "analysis" / "cases"

STATUSES = ("announced", "scheduled", "pending_close", "ramping", "at_run_rate",
            "ramping_down", "delayed", "ended", "renewed", "cancelled")
NOT_STARTED = ("announced", "scheduled", "pending_close", "delayed")
FINISHED = ("ended", "cancelled")
KINDS = ("new", "renewal", "extension", "vendor_consolidation", "gcc", "acquisition", "other")
CROSS_CHECK_DAYS = 21


def qidx(q: str) -> int:
    m = re.fullmatch(r"Q([1-4])FY(\d{2})", q or "")
    if not m:
        raise ValueError(f"quarter label {q!r} is not QnFYyy")
    return int(m.group(2)) * 4 + int(m.group(1)) - 1


def qlab(i: int) -> str:
    return f"Q{i % 4 + 1}FY{i // 4:02d}"


def quarter_of(d: dt.date) -> str:
    fy = d.year + (1 if d.month >= 4 else 0)
    return f"Q{((d.month - 4) % 12) // 3 + 1}FY{fy % 100:02d}"


def validate(doc: dict) -> list[str]:
    probs = []
    for q, row in (doc.get("quarters") or {}).items():
        try:
            qidx(q)
        except ValueError as e:
            probs.append(str(e)); continue
        if not (row.get("source") or "").strip():
            probs.append(f"quarter {q}: needs a source")
    if doc.get("next_result"):
        try:
            dt.date.fromisoformat(doc["next_result"])
        except ValueError:
            probs.append(f"next_result {doc['next_result']!r} is not an ISO date")
    seen = set()
    for d in doc.get("deals") or []:
        did, tag = d.get("id"), f"deal {d.get('id')!r}"
        if not did or did in seen:
            probs.append(f"{tag}: id missing or duplicated"); continue
        seen.add(did)
        if not (d.get("source") or "").strip():
            probs.append(f"{tag}: needs a source")
        if d.get("status") not in STATUSES:
            probs.append(f"{tag}: status {d.get('status')!r} not in {STATUSES}")
        if d.get("kind") and d["kind"] not in KINDS:
            probs.append(f"{tag}: kind {d['kind']!r} not in {KINDS}")
        for k in ("start_q", "end_q", "revised_start_q", "booked_q"):
            if d.get(k):
                try:
                    qidx(d[k])
                except ValueError as e:
                    probs.append(f"{tag}: {e}")
        for h in d.get("history") or []:
            if h.get("status") not in STATUSES or not (h.get("source") or "").strip() or not h.get("date"):
                probs.append(f"{tag}: every history row needs date, a valid status and a source")
    return probs


def book_to_bill(doc: dict) -> list[dict]:
    out = []
    for q, r in sorted((doc.get("quarters") or {}).items(), key=lambda x: qidx(x[0])):
        tcv, rev = r.get("tcv_usd_m"), r.get("revenue_usd_m")
        out.append({"quarter": q, "tcv_usd_m": tcv, "revenue_usd_m": rev,
                    "btb": round(tcv / rev, 2) if tcv and rev else None, "tcv_note": r.get("tcv_note")})
    return out


def flags(doc: dict, today: dt.date) -> list[dict]:
    oq = qidx(quarter_of(today))
    out = []
    for d in doc.get("deals") or []:
        st, s = d.get("status"), d.get("revised_start_q") or d.get("start_q")
        if st in NOT_STARTED and s and qidx(s) < oq:
            out.append({"deal": d["id"], "flag": "OVERDUE",
                        "detail": f"expected start {s} has passed, still {st!r}"})
        if d.get("end_q") and st not in FINISHED + ("renewed",) and qidx(d["end_q"]) < oq:
            out.append({"deal": d["id"], "flag": "PAST_END",
                        "detail": f"stated end {d['end_q']} has passed, still {st!r} — gone, renewed or extended?"})
    return out


def cross_check(doc: dict, today: dt.date) -> dict | None:
    """From CROSS_CHECK_DAYS before the next result: the deals whose status
    must be re-confirmed, each with the date it was last confirmed."""
    nr = doc.get("next_result")
    if not nr:
        return None
    due = dt.date.fromisoformat(nr) - dt.timedelta(days=CROSS_CHECK_DAYS)
    if today < due:
        return {"due": due.isoformat(), "next_result": nr, "open": False, "deals": []}
    rows = [{"deal": d["id"], "client": d.get("client"), "status": d["status"],
             "last_confirmed": max((h["date"] for h in d.get("history") or []), default=None)}
            for d in doc.get("deals") or [] if d.get("status") not in FINISHED]
    return {"due": due.isoformat(), "next_result": nr, "open": True, "deals": rows}


def deal_list(doc: dict) -> list[dict]:
    return [{"id": d["id"], "client": d.get("client"), "kind": d.get("kind"),
             "value": d.get("value_reported") or (f"${d['tcv_usd_m']:,.0f}m" if d.get("tcv_usd_m") else "undisclosed"),
             "signed": d.get("booked_q"), "start": d.get("revised_start_q") or d.get("start_q"),
             "end": d.get("end_q"), "status": d["status"]}
            for d in doc.get("deals") or []]


def summary(doc: dict, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    probs = validate(doc)
    if probs:
        return {"problems": probs}
    return {"book_to_bill": book_to_bill(doc), "deals": deal_list(doc),
            "flags": flags(doc, today), "cross_check": cross_check(doc, today)}


def load(ent: str) -> dict | None:
    f = CASES / ent / "deals.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def render(s: dict) -> str:
    if s.get("problems"):
        return "DEALS   REFUSED: " + "; ".join(s["problems"])
    bt = s["book_to_bill"][-4:]
    L = ["DEALS   reported TCV: " + " · ".join(f"{r['quarter']} {r['tcv_usd_m']}" + (f" (BtB {r['btb']})" if r["btb"] else "")
                                            for r in bt) if bt else "DEALS   no reported TCV yet"]
    for d in s["deals"]:
        L.append(f"        {d['client'][:44] if d['client'] else d['id']:44s} {d['status']:13s} {d['value']:>14s}  "
                 f"signed {d['signed'] or '?'} · start {d['start'] or '?'} · end {d['end'] or '?'}")
    for f in s["flags"]:
        L.append(f"FLAG    {f['flag']:9s} {f['deal']}: {f['detail']}")
    cc = s.get("cross_check")
    if cc and cc["open"]:
        L.append(f"CHECK   CROSS-CHECK DUE before the {cc['next_result']} print — re-confirm "
                 f"{len(cc['deals'])} deals: " + ", ".join(f"{x['deal']} (last {x['last_confirmed'] or 'never'})"
                                                          for x in cc["deals"]))
    elif cc:
        L.append(f"CHECK   deal cross-check opens {cc['due']} (print {cc['next_result']})")
    else:
        L.append("CHECK   no next_result in deals.json — the cross-check cannot be scheduled")
    return "\n".join(L)


def _selftest() -> None:
    ok = 0
    assert qlab(qidx("Q3FY27")) == "Q3FY27" and quarter_of(dt.date(2026, 10, 13)) == "Q3FY27"; ok += 1
    doc = {"next_result": "2027-01-09", "quarters": {
        "Q1FY27": {"tcv_usd_m": 9500, "revenue_usd_m": 7600, "source": "s"},
        "Q2FY27": {"tcv_usd_m": 9600, "revenue_usd_m": 8000, "source": "s", "tcv_note": "ex 2 deals"}},
        "deals": [
            {"id": "live", "client": "A", "status": "ramping", "tcv_usd_m": 800, "booked_q": "Q1FY27",
             "start_q": "Q2FY27", "source": "call",
             "history": [{"date": "2026-10-08", "status": "ramping", "source": "call"}]},
            {"id": "late", "client": "B", "status": "scheduled", "start_q": "Q1FY27", "source": "press",
             "value_reported": "Rs2,903cr", "history": [{"date": "2025-05-21", "status": "scheduled", "source": "filing"}]},
            {"id": "lapsed", "client": "C", "status": "at_run_rate", "end_q": "Q4FY26", "source": "call"},
            {"id": "done", "client": "D", "status": "ended", "end_q": "Q4FY25", "source": "call"}]}
    assert validate(doc) == []; ok += 1
    for mut, why in [(lambda x: x["deals"][0].pop("source"), "needs a source"),
                     (lambda x: x["deals"][0].update(status="live"), "status"),
                     (lambda x: x["deals"].append(dict(x["deals"][0])), "duplicated"),
                     (lambda x: x.update(next_result="9 Jan"), "ISO date"),
                     (lambda x: x["deals"][1]["history"][0].pop("source"), "history row")]:
        dd = json.loads(json.dumps(doc)); mut(dd)
        assert any(why in p for p in validate(dd)), (why, validate(dd)); ok += 1
    have = {(f["deal"], f["flag"]) for f in flags(doc, dt.date(2026, 10, 13))}
    assert have == {("late", "OVERDUE"), ("lapsed", "PAST_END")}, have; ok += 1      # none on live/done
    # cross-check: closed before the window, open inside it, finished deals excluded
    assert cross_check(doc, dt.date(2026, 10, 13))["open"] is False; ok += 1
    cc = cross_check(doc, dt.date(2026, 12, 19))
    assert cc["open"] and {x["deal"] for x in cc["deals"]} == {"live", "late", "lapsed"}; ok += 1
    assert cross_check({**doc, "next_result": None}, dt.date(2026, 12, 19)) is None; ok += 1
    s = summary(doc, dt.date(2026, 10, 13))
    assert s["book_to_bill"][-1]["btb"] == 1.2 and s["deals"][1]["value"] == "Rs2,903cr" \
        and s["deals"][2]["value"] == "undisclosed"; ok += 1
    print(f"it_deals selftest: {ok} checks passed")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("entity", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    doc = load(a.entity) if a.entity else None
    if not doc:
        sys.exit(f"no deals.json under {CASES / str(a.entity)}")
    s = summary(doc)
    print(json.dumps(s, indent=2) if a.json else render(s))


if __name__ == "__main__":
    main()
