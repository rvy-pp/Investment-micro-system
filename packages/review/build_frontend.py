"""Front-end check - prove every route the page calls answers with today's data.

    python packages/review/build_frontend.py
    python packages/review/build_frontend.py --json

THERE IS NOTHING TO "BUILD". The page is `packages/web/app.html`, served live by
`packages/api/serve.py` straight out of `data/ims.db`, so it is current the moment
refresh.py finishes and no artefact needs regenerating. What CAN break is the
data behind a route: an endpoint that 500s, an empty peer group, an OI table that
stopped being written. The page shows those as a spinner or a blank panel, which
reads as "still loading" rather than "the source is dead" - the same
plausible-looking-nothing this repo keeps finding.

So this calls the engine IN-PROCESS, exactly as the API would, and reports what
the page will actually render. In-process deliberately: it needs no listening
socket, so it runs inside an unattended refresh whether or not the server is up.

WHY build_pillars_page.py IS NOT CALLED HERE, and should not be added. It renders
`packages/review/pillars.html` from `packages/review/_pillars.json` - and NOTHING
IN THE REPO WRITES THAT FILE. It has been frozen since 2026-08-21, so the step
the full-refresh skill used to call "rebuild the page" regenerates a page from a
stale snapshot every time and cannot show today's run. Wiring it into the refresh
would make a stale page look freshly built, which is worse than leaving it out.
Either give it a data builder or retire it; do not schedule it.

Exit code is nonzero when a route would fail or a live sector renders nothing,
so a broken front end shows red in status.json rather than only as a blank tab.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "api"))

import engine            # noqa: E402
import tape as tape_mod  # noqa: E402

OUT = REPO / "data" / "refresh" / "frontend.json"


def check() -> dict:
    today = dt.date.today().isoformat()
    r: dict = {"as_of": today, "routes": [], "problems": [], "warnings": []}

    def route(name, fn):
        try:
            return fn(), None
        except Exception as exc:
            r["problems"].append(f"{name}: {type(exc).__name__}: {exc}")
            r["routes"].append({"route": name, "ok": False,
                                "detail": f"{type(exc).__name__}: {exc}"})
            return None, exc

    # /api/sectors - the top-level nav
    sectors, _ = route("/api/sectors", engine.sector_list)
    if sectors:
        live = [s["id"] for s in sectors if s["live"]]
        r["routes"].append({"route": "/api/sectors", "ok": True,
                            "detail": f"{len(sectors)} sectors, "
                                      f"{len(live)} live: {', '.join(live)}"})
        r["sectors"] = sectors

        # /api/sector?id=... - every tab must render something
        for s in sectors:
            d, _ = route(f"/api/sector?id={s['id']}",
                         lambda sid=s["id"]: engine.sector_detail(sid))
            if d is None:
                continue
            fresh = sum(1 for c in d["commodities"] if c["date"] == today)
            ok = d["n_priced"] > 0
            if not ok:
                r["problems"].append(
                    f"sector {s['id']}: NOTHING priced - the tab renders an "
                    f"empty table")
            r["routes"].append({
                "route": f"/api/sector?id={s['id']}", "ok": ok,
                "detail": f"{d['n_priced']}/{d['n_total']} priced, "
                          f"{fresh} dated today"})

        # /api/scores - the Bridge tab, one call per live peer group
        for pg in [g for s in sectors for g in s["peer_groups"]]:
            d, _ = route(f"/api/scores[{pg}]", lambda g=pg: engine.compute(g, 30))
            if d is None:
                continue
            n = len(d.get("rows") or d.get("entities") or [])
            ok = n > 0
            if not ok:
                r["problems"].append(
                    f"peer group {pg}: 0 rows - the Bridge tab renders empty")
            r["routes"].append({"route": f"/api/scores[{pg}]", "ok": ok,
                                "detail": f"{n} name(s), as_of {d.get('as_of')}"})

    # /api/oi - the Positioning tab. Its own table, and the one that went five
    # trading days stale unnoticed once already.
    oi, _ = route("/api/oi", engine.oi_snapshot)
    if oi is not None:
        dates = [x.get("date") for x in oi if x.get("date")]
        newest = max(dates) if dates else None
        age = ((dt.date.today() - dt.date.fromisoformat(newest)).days
               if newest else None)
        det = f"{len(oi)} name(s), newest {newest or 'EMPTY'}"
        if age is not None and age > 4:
            det += f" ({age}d)  <-- SOURCE MAY BE DEAD"
            r["warnings"].append(
                f"/api/oi: newest row is {newest}, {age} days old - the "
                f"Positioning tab renders, but on stale positioning. "
                f"freshness.py reports this too; it is a dead source, not a "
                f"broken route")
        elif age is not None:
            det += f" ({age}d)"
        if not oi:
            r["problems"].append("/api/oi: empty - Positioning renders nothing")
        r["routes"].append({"route": "/api/oi", "ok": bool(oi), "detail": det})

    # /api/tape - the Pair tab's charts
    tp, _ = route("/api/tape[composite]",
                  lambda: tape_mod.tape("composite", None))
    if tp is not None:
        series = tp.get("series") or {}
        # series[eid] is a dict; its "points" list is what the chart draws.
        # len(v) here counted the dict's KEYS and reported 40 for 5 names of 63
        # points each.
        pts = sum(len((v or {}).get("points") or []) for v in series.values())
        ok = len(series) > 0 and pts > 0
        if not ok:
            r["problems"].append(
                "/api/tape: no series - the pair chart and every mini chart "
                "render blank")
        r["routes"].append({"route": "/api/tape[composite]", "ok": ok,
                            "detail": f"{len(series)} name(s), {pts:,} point(s), "
                                      f"as_of_max {tp.get('as_of_max')}"})

    # /api/inputs and /api/guidance - the editable panels
    for name, fn in (("/api/inputs", engine.inputs_for_ui),
                     ("/api/guidance", engine.guidance_rows)):
        d, _ = route(name, fn)
        if d is not None:
            r["routes"].append({"route": name, "ok": True,
                                "detail": f"{len(d)} row(s)"})

    r["ok"] = not r["problems"]
    r["n_warnings"] = len(r["warnings"])
    r["n_routes"] = len(r["routes"])
    r["n_failed"] = sum(1 for x in r["routes"] if not x["ok"])
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = check()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")

    if a.json:
        print(json.dumps(r, indent=2, default=str))
        return 0 if r["ok"] else 1

    print(f"front end as of {r['as_of']}  ->  packages/web/app.html "
          f"(served live, nothing to rebuild)\n")
    # ---- IS THE RUNNING SERVER ACTUALLY SERVING THIS CODE? ----------------
    #
    # "served live, nothing to rebuild" above is true of the FILE and says nothing
    # about the PROCESS. serve.py imports engine at startup, so engine.SECTORS is
    # frozen in memory from whenever the server was launched.
    #
    # On 2026-08-26 the Steel tab still showed the "no spec" badge a full day
    # after steel went live. The server had been running since 8/25 15:17 and was
    # answering `Steel live=False, peer_groups=[]` while the code on disk said
    # otherwise. Every route answered, every check here passed, and the page was a
    # day stale — with this step's own output reassuring the reader.
    #
    # Same shape as the refresh light in CLAUDE.md: "a scheduled task that
    # silently stops looks exactly like a quiet market". A stale server looks
    # exactly like a sector with no spec.
    #
    # NOT FATAL. No server, or one on another port, is a normal state for a
    # headless run and must not fail the refresh.
    try:
        import urllib.request
        live = json.load(urllib.request.urlopen(
            "http://127.0.0.1:8770/api/nav", timeout=5))
        live_groups = {x["id"]: sorted(x.get("peer_groups") or [])
                       for x in live if x.get("kind") == "sector"}
        disk_groups = {s["id"]: sorted(s["peer_groups"]) for s in engine.SECTORS}
        drift = {k: (live_groups.get(k), v) for k, v in disk_groups.items()
                 if live_groups.get(k) != v}
        if drift:
            print("  !! THE RUNNING SERVER IS SERVING STALE CODE — restart it")
            for k, (was, now) in drift.items():
                print("       %-14s server %s  disk %s" % (k, was, now))
            print()
        else:
            print("  server on 8770 matches the code on disk")
            print()
    except Exception:
        print("  no server answering on 8770 — nothing to check against")
        print()

    print(f"{'route':32}{'':4}detail")
    print("-" * 92)
    for x in r["routes"]:
        print(f"{x['route']:32}{'ok' if x['ok'] else 'FAIL':>4}  {x['detail']}")
    print()
    if r["ok"]:
        print(f"all {r['n_routes']} routes answer; every tab has something to render")
    else:
        print(f"{len(r['problems'])} problem(s) - a tab will render BROKEN:")
        for pr in r["problems"]:
            print(f"   {pr}")
    if r["warnings"]:
        print(f"\n{len(r['warnings'])} warning(s) - renders, but the data is old:")
        for w in r["warnings"]:
            print(f"   {w}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
