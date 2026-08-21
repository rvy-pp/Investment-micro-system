"""The daily refresh: what a scheduled task is allowed to do unattended.

    python packages/refresh.py            # do it
    python packages/refresh.py --dry      # show the sequence, run nothing

THIS IS A STRICT SUBSET OF pipeline.py, AND THE SUBSET IS THE POINT.
docs/DAILY_MONITORING.md lists four things that must never be automated, three
of them because a machine physically cannot do them:

    Wind zinc (ZN.SHF)   the Wind MCP is agent-callable, never Python-callable
    broker mail          same constraint, Microsoft 365 MCP
    Daily Metals Pack    a workbook a person drops in by hand
    extraction -> store  deliberate: a wrong extraction enters as a FACT

So an unattended run refreshes equity closes, re-checks corporate actions, and
recomputes + persists the pillars. It does NOT pretend to have done the rest.

WHY THIS WRITES A STATUS FILE. A scheduled task that silently stops looks
exactly like a quiet market: the dashboard keeps serving yesterday's scores and
nothing anywhere says they are old. That is the silent-arithmetic failure shape
in operational form — plausible output, no error. So every run writes
data/refresh/status.json, the API serves it, and the page shows it. A refresh
that fails is LOUD on the dashboard, not just in a log nobody opens.

Exit code is nonzero if any step failed, so Task Scheduler shows a red run.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "refresh"
PY = sys.executable

# (label, argv, fatal). fatal=True means later steps are pointless if it fails —
# Tier 0 in DAILY_MONITORING.md is explicitly "HALT", not "warn and continue",
# because everything downstream of a failed integrity check is untrustworthy
# rather than merely missing.
STEPS = [
    ("preflight",         ["packages/core/preflight.py"],                     True),
    ("equity closes",     ["packages/adapters/yahoo_prices.py", "--load",
                           "--range", "3mo"],                                 False),
    ("corporate actions", ["packages/adapters/check_corporate_actions.py"],   False),
    ("score + persist",   ["packages/score/run_scores.py"],                   True),
]

# Named in the status so the dashboard can say what is NOT covered by an
# unattended run. Silence about these would imply a completeness the run does
# not have.
MANUAL = [
    ("Wind zinc (ZN.SHF)", "Wind MCP is agent-callable only"),
    ("broker mail",        "Microsoft 365 MCP is agent-callable only"),
    ("Daily Metals Pack",  "manual workbook drop"),
    ("extraction",         "deliberately never automated — a wrong extraction "
                           "enters the store as a fact"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    day = started.astimezone().strftime("%Y-%m-%d")
    log_path = OUT / f"{day}.log"
    lines, results, ok = [], [], True

    def say(s: str = "") -> None:
        print(s)
        lines.append(s)

    say(f"refresh  {started.astimezone().isoformat(timespec='seconds')}")
    say("=" * 70)

    for label, argv, fatal in STEPS:
        say(f"\n$ {' '.join(argv)}")
        if a.dry:
            say("  (dry — not executed)")
            results.append({"step": label, "status": "dry"})
            continue
        r = subprocess.run([PY, *argv], cwd=REPO, capture_output=True, text=True)
        body = (r.stdout or "").strip()
        if body:
            say("  " + body.replace("\n", "\n  "))
        if r.returncode != 0:
            ok = False
            err = (r.stderr or "").strip() or f"exit {r.returncode}"
            say("  ! " + err.replace("\n", "\n  ! "))
            results.append({"step": label, "status": "fail", "error": err[:2000]})
            if fatal:
                say(f"\nHALT — {label} is a Tier 0 gate; "
                    f"nothing downstream would be trustworthy.")
                break
        else:
            results.append({"step": label, "status": "ok"})

    finished = datetime.now(timezone.utc)
    say("\n" + "=" * 70)
    say(f"{'OK' if ok else 'FAILED'}  in "
        f"{(finished - started).total_seconds():.1f}s")
    say("\nNOT refreshed by an unattended run (see docs/DAILY_MONITORING.md):")
    for name, why in MANUAL:
        say(f"   {name:22} {why}")

    if not a.dry:
        log_path.write_text("\n".join(lines), encoding="utf-8")
        (OUT / "status.json").write_text(json.dumps({
            "day": day,
            "started": started.isoformat(timespec="seconds"),
            "finished": finished.isoformat(timespec="seconds"),
            "ok": ok,
            "steps": results,
            "manual": [{"name": n, "why": w} for n, w in MANUAL],
            "log": str(log_path.relative_to(REPO)),
        }, indent=2), encoding="utf-8")
        say(f"\nstatus -> {(OUT / 'status.json').relative_to(REPO)}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
