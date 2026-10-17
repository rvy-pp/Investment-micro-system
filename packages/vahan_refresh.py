"""The afternoon Vahan run: maker share + SIAM/Vahan retail, then the vault copy.

    python packages/vahan_refresh.py              # wait for Vahan, capture, export
    python packages/vahan_refresh.py --detach     # same, spawned detached and returns
    python packages/vahan_refresh.py --no-wait    # one probe; fail now if Vahan refuses
    python packages/vahan_refresh.py --force      # even if today's already ran / is running

Scheduled task `vahan-afternoon` (Claude Desktop) fires this at 13:00 (+ jitter:
13:13) with --detach. PM instruction 2026-09-29: "Lets do vahan update in the afternoon.
Remove the process for daily-refresh. Add a different routine that runs
everyday at a different time. Make sure the front-end gets updated again in
the afternoon and no error shows up in the morning."

WHY IT LEFT THE 08:00 RUN. Four mornings running (26-29 Sep 2026) Vahan's
`durationWiseRegistrationTable` refused EVERY filtered request at ~08:10 IST —
by segment, by maker, by state, even with an empty `vehicleMakers=` key —
while the unfiltered call, the page and the maker lookup all answered, and the
public dashboard's own filtered XHR failed identically in a browser. It came
back by ~12:37 (28-Sep) and by the afternoon (27-Sep). Every number this feed
needs is filtered, so a morning run could only ever fail and turn the Overview
amber. Nothing is lost by moving: Vahan's month-to-date refreshes ONCE, in an
overnight batch (measured 2026-09-25 — identical 08:10 and afternoon reads), so
a 13:00 read is the same number the 08:00 read would have been.

THE PROBE IS THE GATE. One filtered 2W query every POLL_MIN until it answers,
for up to MAX_WAIT_H; only then the capture. Probing first rather than just
running the capture on a timer keeps a down server from producing a traceback
every 15 minutes, and the capture itself still refuses a partial state-sum.

ORDER. vahan_share reads its F&O roster from `fo_oi`, which the 08:00
refresh's F&O bhavcopy step loads — so an afternoon run is always after it,
which is what the old step comment demanded.

STATUS. Writes data/refresh/vahan_status.json — NEVER status.json, which is
the morning run's and whose steps the header light and the Overview read. The
Overview warns on this file ONLY when today's run FAILED, has been "running"
implausibly long, or has not run at all by engine.VAHAN_DUE. So a morning page
says nothing about Vahan, and an afternoon failure is still loud: a scheduled
task that silently stops must not look like a quiet market.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "refresh"
STATUS = OUT / "vahan_status.json"
LOG = OUT / "vahan_last.log"
PY = sys.executable

POLL_MIN = 15       # minutes between probes while Vahan refuses
MAX_WAIT_H = 3.0    # 13:13 start -> gives up ~16:13; engine.VAHAN_DUE is 17:00

sys.path.insert(0, str(REPO / "packages" / "adapters"))


def probe() -> str | None:
    """None if a FILTERED query answers, else the reason. Filtered on purpose:
    the unfiltered call kept working through every outage and would say "up"."""
    import vahan
    p = vahan._params("", "", vahan.CALENDAR["month"], "2026", "2026")
    p["vehicleCategoryGroup"] = "Two Wheeler"
    try:
        rows = vahan._get(f"{vahan.DASH}/durationWiseRegistrationTable?"
                          f"{urllib.parse.urlencode(p)}", tries=1, timeout=90)
    except Exception as e:                       # noqa: BLE001 - reason only
        return str(e).splitlines()[-1].strip() or type(e).__name__
    return None if rows else "empty body"


def _write(st: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    tmp.replace(STATUS)


def _already(force: bool) -> str | None:
    """Why this run should step aside, or None. A second copy (the 13:00 task
    firing while a manual run is still polling, or a missed task firing on the
    next app launch after a good run) would just repeat the probe loop and
    fight over the status file. A FAILED run today does not block a retry."""
    if force:
        return None
    try:
        st = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if st.get("day") != datetime.now().strftime("%Y-%m-%d"):
        return None
    if st.get("state") == "ok":
        return f"today's run already succeeded at {str(st.get('finished'))[11:16]}"
    if st.get("state") == "running":
        try:
            age = (datetime.now().astimezone()
                   - datetime.fromisoformat(st["started"])).total_seconds()
        except (KeyError, ValueError):
            return None
        if age < (float(st.get("max_wait_h") or 0) + 0.5) * 3600:
            return f"a run started {st['started'][11:16]} is still in progress"
    return None


def run(wait: bool) -> int:
    started = datetime.now().astimezone()
    st = {"day": started.strftime("%Y-%m-%d"),
          "started": started.isoformat(timespec="seconds"),
          "state": "running", "max_wait_h": MAX_WAIT_H if wait else 0,
          "attempts": [], "steps": []}
    _write(st)

    def say(s: str) -> None:
        print(s, flush=True)

    deadline = time.time() + (MAX_WAIT_H * 3600 if wait else 0)
    while True:
        err = probe()
        now = datetime.now().strftime("%H:%M")
        st["attempts"].append({"at": now, "ok": err is None, "error": err})
        _write(st)
        if err is None:
            say(f"{now} Vahan answers a filtered query")
            break
        say(f"{now} Vahan refuses filtered queries: {err}")
        if time.time() + POLL_MIN * 60 > deadline:
            st.update(state="failed", finished=datetime.now().astimezone()
                      .isoformat(timespec="seconds"),
                      reason=f"Vahan refused filtered queries from "
                             f"{st['attempts'][0]['at']} to {now} "
                             f"({len(st['attempts'])} probes; last: {err})")
            _write(st)
            say(st["reason"])
            return 1
        time.sleep(POLL_MIN * 60)

    ok = True
    steps = [
        ("vahan share",    ["packages/adapters/vahan_share.py", "--capture"]),
        ("SIAM wholesale", ["packages/adapters/siam_wholesale.py", "--fetch"]),
        # LAST, and after the captures: the vault copy is the offline read of
        # the Auto tab, and exporting before them re-freezes the morning's.
        # The desk page needs nothing — engine reads vahan_share live.
        ("vault copy",     ["packages/web/export_static.py", "--quiet"]),
    ]
    for label, argv in steps:
        say(f"\n$ {' '.join(argv)}")
        r = subprocess.run([PY, *argv], cwd=REPO, capture_output=True, text=True)
        out = (r.stdout or "").strip()
        if out:
            say("  " + out.replace("\n", "\n  "))
        err = None
        if r.returncode != 0:
            err = ((r.stderr or "").strip() or f"exit {r.returncode}")[-2000:]
        elif label == "SIAM wholesale" and "retail NOT refreshed" in out:
            # siam_wholesale exits 0 when the Vahan half fails (it keeps the
            # stored retail, by design) — so the exit code alone would call a
            # failed retail read "ok". Read what it said.
            err = next(ln.strip() for ln in out.splitlines()
                       if "retail NOT refreshed" in ln)[:2000]
        if err:
            ok = False
            say("  ! " + err.replace("\n", "\n  ! "))
        st["steps"].append({"step": label, "status": "fail" if err else "ok",
                            **({"error": err} if err else {})})
        _write(st)

    st.update(state="ok" if ok else "failed",
              finished=datetime.now().astimezone().isoformat(timespec="seconds"))
    if not ok:
        st["reason"] = "step(s) failed: " + ", ".join(
            s["step"] for s in st["steps"] if s["status"] == "fail")
    _write(st)
    say(f"\n{'OK' if ok else 'FAILED'} -> {STATUS.relative_to(REPO)}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--detach", action="store_true",
                    help="spawn detached (log -> data/refresh/vahan_last.log) and return")
    ap.add_argument("--no-wait", action="store_true",
                    help="probe once; fail immediately if Vahan refuses")
    ap.add_argument("--force", action="store_true",
                    help="run even if today's run succeeded or is in progress")
    a = ap.parse_args()
    why = _already(a.force)
    if why:
        print(f"skipped — {why}; --force to run anyway")
        return 0
    if a.detach:
        # The scheduled task is a short agent session; a 3h wait inside it
        # would outlive it. Same DETACHED_PROCESS + -u as refresh.py's
        # BACKGROUND steps, for the same reasons (no console to inherit; a
        # block-buffered log is empty exactly when the process dies).
        OUT.mkdir(parents=True, exist_ok=True)
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        argv = [PY, "-u", str(pathlib.Path(__file__).resolve())]
        if a.no_wait:
            argv.append("--no-wait")
        if a.force:
            argv.append("--force")
        with open(LOG, "w", encoding="utf-8") as lf:
            p = subprocess.Popen(argv, cwd=REPO, stdout=lf,
                                 stderr=subprocess.STDOUT, creationflags=flags)
        print(f"started detached (pid {p.pid}); log -> {LOG.relative_to(REPO)}, "
              f"status -> {STATUS.relative_to(REPO)}")
        return 0
    return run(wait=not a.no_wait)


if __name__ == "__main__":
    sys.exit(main())
