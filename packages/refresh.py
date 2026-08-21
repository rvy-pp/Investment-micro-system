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
    # OI was missing from this list until 2026-08-21 and had gone 5 trading days
    # stale unnoticed, because nothing checked a table outside `prices`.
    # pipeline.py STEP 1 always ran it; this file simply forgot to.
    ("open interest",     ["packages/adapters/vault_oi.py", "--load"],        False),
    ("corporate actions", ["packages/adapters/check_corporate_actions.py"],   False),
    ("score + persist",   ["packages/score/run_scores.py"],                   True),
]

# Steps that run AT MOST ONCE A DAY. The launcher refreshes on every
# double-click, so without this a busy morning re-pulls the same OI repeatedly.
#
# Only OI is guarded. The equity load deliberately is NOT: Yahoo revises through
# the session, so the same call at 15:00 returns a better close than at 08:00 and
# re-pulling is the point. run_scores.py is not guarded either — it rewrites
# today's rows in place, so re-running it against a fresher price IS the update.
#
# THE TEST IS "DID THE PULL RUN TODAY", NOT "DOES THE STORE HOLD TODAY'S DATA".
# My first version asked the second question, and it can never be true: OI comes
# from the vault's own pipeline, which publishes T-1 at best — the store held
# 2026-08-17 on 2026-08-21 — so a data-date guard skips nothing and re-pulls on
# every launch, the exact opposite of the intent. A run marker is the only thing
# that answers the question actually being asked.
#
# Trade-off accepted: if the vault publishes new OI after the day's first
# refresh, it is not picked up until tomorrow. `--force` is the escape hatch.
SKIP_IF_DONE = {"open interest"}
MARKER = OUT / "steps_done.json"


def _marker_read() -> dict:
    try:
        return json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

# Named in the status so the dashboard can say what is NOT covered by an
# unattended run. Silence about these would imply a completeness the run does
# not have.
MANUAL = [
    ("Wind zinc (ZN.SHF)", "Wind MCP is agent-callable only"),
    ("broker mail",        "Microsoft 365 MCP is agent-callable only"),
    ("Daily Metals Pack",  "manual workbook drop"),
    ("LME cash (westmetall)",
                           "needs an agent: lme.com 403s automated fetching, so the "
                           "table is captured with WebFetch into data/staging/ and "
                           "loaded by adapters/westmetall.py"),
    ("extraction",         "deliberately never automated — a wrong extraction "
                           "enters the store as a fact"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-pull even steps whose data is already there for today")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    day = started.astimezone().strftime("%Y-%m-%d")
    log_path = OUT / f"{day}.log"
    lines, results, ok = [], [], True
    marker = _marker_read()

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
        if label in SKIP_IF_DONE and not a.force and marker.get(label) == day:
            # Reported as a step with its own status, never omitted. A silently
            # absent step is indistinguishable from one that ran and found
            # nothing, which is the ambiguity run_scores.py's withheld rows
            # exist to avoid.
            say(f"  skipped — already pulled today ({day}); --force to re-pull")
            results.append({"step": label, "status": "skipped"})
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
            # Marked only on SUCCESS, so a failed pull is retried on the next
            # launch instead of being skipped for the rest of the day.
            if label in SKIP_IF_DONE:
                marker[label] = day
                MARKER.write_text(json.dumps(marker, indent=2), encoding="utf-8")

    # ---- feed freshness, AFTER the loads ---------------------------------
    # THE CHECK THIS FILE WAS MISSING. Every step above can succeed against a
    # feed that has itself stopped printing: "the run worked" and "the inputs
    # are current" are different claims and only the first was being made.
    # A stale feed does NOT fail the run — it is a data condition, not a code
    # failure — but it lands in status.json so the dashboard light goes amber
    # over a green run.
    fresh = None
    if not a.dry:
        try:
            sys.path.insert(0, str(REPO / "packages" / "core"))
            import freshness
            fresh = freshness.check()
        except Exception as exc:                      # never let it break a run
            say(f"\n! freshness check failed: {type(exc).__name__}: {exc}")

    finished = datetime.now(timezone.utc)
    say("\n" + "=" * 70)
    say(f"{'OK' if ok else 'FAILED'}  in "
        f"{(finished - started).total_seconds():.1f}s")

    if fresh:
        if fresh["ok"]:
            say("feeds: all within threshold")
        else:
            say(f"feeds: {fresh['n_stale']} STALE — scores above were computed "
                f"on these:")
            for x in fresh["stale"]:
                say(f"   {x['series']:24}{x['feed']:22}{x['age_txt']}"
                    f"   (limit {x['limit']})")
            say("   docs/DAILY_MONITORING.md: a stale price is not a flat price.")

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
            # Separate from `ok` on purpose. A run can succeed against stale
            # inputs; the dashboard must be able to tell those two apart rather
            # than collapsing them into one green light.
            "feeds_ok": (fresh["ok"] if fresh else None),
            "stale_feeds": ([{"series": x["series"], "feed": x["feed"],
                              "age": x["age_txt"], "limit": x["limit"]}
                             for x in fresh["stale"]] if fresh else []),
            "worst_feed_age": (fresh["worst_age"] if fresh else None),
            "manual": [{"name": n, "why": w} for n, w in MANUAL],
            "log": str(log_path.relative_to(REPO)),
        }, indent=2), encoding="utf-8")
        say(f"\nstatus -> {(OUT / 'status.json').relative_to(REPO)}")

    # THREE EXIT CODES, because "it broke" and "it ran on stale data" need
    # different responses and a cron can only see the number.
    #
    #   0  ran clean, feeds current
    #   1  a step FAILED — code or connectivity; someone must look
    #   2  ran fine, but a feed is over its threshold. Scores for the affected
    #      entities are WITHHELD by run_scores.py rather than computed, so the
    #      output is honest; what is missing is the input, and only a person can
    #      supply it (a metals-pack drop, a Wind pull, a westmetall capture).
    #
    # Distinct rather than collapsed into 1: a stale feed is the NORMAL state of
    # this system between manual drops, and if it shared an exit code with a real
    # failure the red would stop meaning anything within a week.
    if not ok:
        return 1
    if fresh and not fresh["ok"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
