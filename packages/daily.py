"""The daily run: refresh feeds, look for new cited values, then score.

    python packages/daily.py                 # dry — reports what it would do
    python packages/daily.py --run

ORDER MATTERS. Feeds first so FX is current before the zinc conversion; then
the episodic scan, so a value cited in today's digest is in the store before
the bridge resolves anything.

WHAT "A NEW ENTRY UPDATES THE SCORE" ACTUALLY MEANS, because the behaviour has
one consequence worth understanding:

  Between citations the last level is CARRIED FORWARD, so the line contributes
  nothing and the score does not drift. Correct — nobody observed a move.

  When a new value lands after a gap, the bridge compares it to the level at
  the START OF THE WINDOW, not to yesterday. So a 30-day window picks up the
  full move regardless of when it was reported.

  But the move ARRIVES on the day research reports it. If petcoke drifted for
  three weeks and a broker writes it up today, the score moves today. That is
  information arriving late, not a move happening today — and on an episodic
  series there is no way to know otherwise. It is why these series carry a
  `cited` marker and their age on every line.

STEP 3 IS NOT AUTOMATED, deliberately. Scanning finds CANDIDATE sentences;
turning one into a stored number requires reading it, and a wrong extraction
enters the store as a fact. The scan tells you where to look.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable

# Series with no feed. Scanned every day for a new cited level.
EPISODIC = ["cp_coke", "al_scrap_midwest", "can_sheet_spread",
            "thermal_coal_eauction"]


def run(cmd: list[str], dry: bool) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd[1:])}")
    if dry:
        print("  (dry run — not executed)")
        return
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    out = (r.stdout or "").strip()
    if out:
        print("  " + out.replace("\n", "\n  "))
    if r.returncode != 0 and (r.stderr or "").strip():
        print("  ! " + r.stderr.strip().replace("\n", "\n  ! "))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--window", type=int, default=30)
    a = ap.parse_args()
    dry = not a.run

    print("=" * 70)
    print("STEP 1  refresh continuous feeds")
    print("=" * 70)
    run([PY, "packages/adapters/yahoo_prices.py", "--load", "--range", "3mo"], dry)
    run([PY, "packages/adapters/wind_zinc.py", "--load"], dry)
    run([PY, "packages/adapters/vault_oi.py", "--load"], dry)
    print("\n  NOTE: wind_zinc.py reads data/staging/zn_shf_close.csv. Refreshing")
    print("  that file needs an agent to call the Wind MCP — a script cannot.")

    print("\n" + "=" * 70)
    print("STEP 2  data-quality gates")
    print("=" * 70)
    run([PY, "packages/adapters/check_corporate_actions.py"], dry)

    print("\n" + "=" * 70)
    print("STEP 3  scan for new cited values in episodic series")
    print("=" * 70)
    for s in EPISODIC:
        run([PY, "packages/extract/candidates.py", "--series", s], dry)
    print("\n  Any NEW dated level found here becomes an observation via")
    print("  load_observations.py, and the next bridge run picks it up through")
    print("  carry-forward. Extraction stays a read-and-decide step: a wrong")
    print("  one enters the store as a fact.")

    print("\n" + "=" * 70)
    print("STEP 4  score")
    print("=" * 70)
    # Refresh auto-extracted broker actions BEFORE scoring, or today's mood
    # would be computed from yesterday's broker posture.
    run([PY, "packages/extract/extract_broker_actions.py", "--load"], dry)
    # The write. Everything above this line is input; this is what persists.
    run([PY, "packages/score/run_scores.py"], dry)
    run([PY, "packages/score/combined.py"], dry)

    if dry:
        print("\n" + "-" * 70)
        print("dry run. re-run with --run to execute.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
