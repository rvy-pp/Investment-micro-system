"""Start the API server if it is not answering, so the page is up after a refresh.

    python packages/api/ensure_server.py              # start it if down (or stale)
    python packages/api/ensure_server.py --check      # report only, start nothing
    python packages/api/ensure_server.py --selftest

WHY IT EXISTS (PM, 2026-10-28: the front end showed an error with nothing wrong in
the data). The server had last been restarted from inside a Claude session, after
an engine change. That made it a child of the Claude desktop app, and when the app
restarted itself at 03:25 (an update, every session file rewritten in the same
second) Windows took the server down with it. The 08:00 refresh never started a
server, so the page stayed dead until someone noticed. Nothing raised: the data
was current, the refresh light was green, and the page just could not connect.

WHY WMI AND NOT subprocess. refresh.py itself runs inside the app's process tree
when the 08:00 task fires it. A Popen child, even DETACHED_PROCESS, stays in that
tree and dies at the next app restart, which is the exact failure being fixed.
Win32_Process.Create makes the process a child of WmiPrvSE.exe, outside every
job the app owns. Verified 2026-10-28: the spawned server's parent is
WmiPrvSE.exe, and it survived its spawning shell.

A STALE server is restarted too. serve.py imports engine once, so a server that
predates an engine change serves the old code. /api/version reports that; an
08:00 restart is harmless, and serving yesterday's code all day is not.

Output goes to data/refresh/serve_last.log, overwritten per start, so the next
"why did it die" has something to read. serve.py logs no requests, so it stays
small.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
HOST, PORT = "127.0.0.1", 8770
LOG = REPO / "data" / "refresh" / "serve_last.log"
WAIT_S = 30           # cold import of engine is ~5s; the .vbs allows 25


def probe(timeout: float = 5.0) -> dict | None:
    """/api/version as a dict, or None when nothing answers on the port."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/version",
                                    timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def listener_pid() -> int | None:
    """PID holding the port, whether or not it answers HTTP."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[1] == f"{HOST}:{PORT}"
                and parts[3] == "LISTENING"):
            return int(parts[4])
    return None


def spawn() -> int:
    """Start serve.py via WMI so it lives outside the caller's process tree."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    serve = REPO / "packages" / "api" / "serve.py"
    cmd = f'cmd /c ""{sys.executable}" -u "{serve}" > "{LOG}" 2>&1"'
    ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          "-Arguments @{CommandLine=$env:IMS_CMD; CurrentDirectory=$env:IMS_CWD}; "
          "Write-Output \"$($r.ReturnValue) $($r.ProcessId)\"")
    env = {**__import__("os").environ, "IMS_CMD": cmd, "IMS_CWD": str(REPO)}
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", ps], capture_output=True, text=True, env=env)
    rc, _, pid = (out.stdout.strip() or "x").partition(" ")
    if rc != "0":
        raise RuntimeError(f"Win32_Process.Create failed: {out.stdout.strip()} "
                           f"{out.stderr.strip()}")
    return int(pid)


def stop(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    time.sleep(1)


def ensure(check_only: bool = False, *, _probe=probe, _pid=listener_pid,
           _spawn=spawn, _stop=stop, _sleep=time.sleep) -> tuple[int, str]:
    """(exit code, one line). 0 = up on current code, 1 = could not get it up."""
    v = _probe()
    if v is not None and not v.get("stale"):
        return 0, f"server up on {PORT}, current code"
    if check_only:
        return (1, f"server on {PORT} is STALE (predates "
                   f"{v['disk']['newest_file']})" if v else
                   f"server DOWN on {PORT}")
    why = "stale, restarted" if v is not None else "was down, started"
    held = _pid()
    if held is not None:
        # Either the stale server, or something holding the port without
        # answering /api/version — the vault-dashboard-on-the-wrong-port shape.
        # Only kill it if it answered as OUR server; otherwise say so and stop.
        if v is None:
            return 1, (f"port {PORT} held by PID {held}, which does not answer "
                       f"/api/version — not ours, left alone")
        _stop(held)
    _spawn()
    for _ in range(WAIT_S * 2):
        _sleep(0.5)
        nv = _probe(2.0)
        if nv is not None and not nv.get("stale"):
            return 0, f"server {why} on {PORT} (log -> {LOG.name})"
    return 1, f"server did not come up on {PORT} within {WAIT_S}s — read {LOG}"


def selftest() -> int:
    fails = []
    up = {"stale": False, "disk": {"newest_file": "x"}}
    stale = {"stale": True, "disk": {"newest_file": "packages/api/engine.py"}}
    nap = lambda s: None

    def case(name, probes, pid, check_only, want_rc, want_spawn, want_stop):
        seq = iter(probes)
        calls = {"spawn": 0, "stop": 0}
        rc, msg = ensure(check_only, _probe=lambda t=5.0: next(seq, probes[-1]),
                         _pid=lambda: pid,
                         _spawn=lambda: calls.__setitem__("spawn", calls["spawn"] + 1),
                         _stop=lambda p: calls.__setitem__("stop", calls["stop"] + 1),
                         _sleep=nap)
        ok = (rc == want_rc and calls["spawn"] == want_spawn
              and calls["stop"] == want_stop)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: rc={rc} spawn={calls['spawn']} "
              f"stop={calls['stop']}  {msg}")
        if not ok:
            fails.append(name)

    # Acceptance: a healthy server is left exactly as it is.
    case("up and current -> nothing touched", [up], 111, False, 0, 0, 0)
    # Down -> started, and success is read off the probe, not assumed.
    case("down -> started", [None, None, up], None, False, 0, 1, 0)
    # Stale -> the old process is stopped before a new one binds.
    case("stale -> restarted", [stale, up], 222, False, 0, 1, 1)
    # Rejection: a port held by something that is NOT our server is never killed.
    case("foreign listener -> left alone", [None], 333, False, 1, 0, 0)
    # Rejection: a spawn that never answers is a failure, not a quiet success.
    case("never comes up -> rc 1", [None], None, False, 1, 1, 0)
    # --check reports and starts nothing.
    case("--check on a down server", [None], None, True, 1, 0, 0)
    print(f"selftest: {6 - len(fails)}/6 passed")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rc, msg = ensure(a.check)
    print(msg)
    return rc


if __name__ == "__main__":
    sys.exit(main())
