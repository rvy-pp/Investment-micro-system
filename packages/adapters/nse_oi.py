"""L0 fetch - NSE futures OI, by running the vault's own /oi pipeline.

    python packages/adapters/nse_oi.py            # probe: is it reachable, how stale
    python packages/adapters/nse_oi.py --fetch    # run the incremental fetch

WHY THIS WRAPPER EXISTS. `vault_oi.py` READS `Coverage/<sector>/<name>/
OI History.md`; it does not fetch anything. Something has to write those files,
and that something is `options-dashboard/oi_to_vault.py` - the `/oi` skill in the
deprecated vault system. Until now nothing in this repo ran it, so `refresh.py`
faithfully re-read files nobody was updating and reported OI as "already pulled
today" while its newest row aged.

I HAD THIS WRONG AND THE CORRECTION MATTERS. On 2026-08-24 I described OI as
stale because "the vault pipeline that fed it was retired". It was not retired.
It was never called. One incremental run took a few seconds and moved every one
of 31 F&O names from 2026-08-17 to 2026-08-24 - four new trading days each. A
dead source and an uncalled source look identical from inside the store, which
is exactly why this is now a step with its own name rather than an assumption.

TWO PROPERTIES THAT MAKE IT SAFE TO RUN UNATTENDED, both from the /oi command:

  incremental by default   It reads the existing OI History.md files to recover
                           the prior ~90 days and downloads only trading days
                           since the last stored date - typically one, ~4s. The
                           `--full` 90-day re-download takes ~50 MINUTES and is
                           a weekly manual job to catch NSE back-revisions.
                           This wrapper never passes --full.

  T-1 BY DESIGN            futures.py walks from today-1 backwards, and NSE does
                           not publish the day's F&O bhavcopy until well after
                           the close. So on a morning run the newest row is
                           YESTERDAY and that is correct, not a failed write.
                           Do not re-run to "pick up today".

It writes into the vault, which is otherwise read-only from this repo. That is
deliberate and narrow: `OI History.md` is data the vault pipeline owns, the same
files vault_oi.py already reads, and nothing else in the vault is touched.

Needs `jugaad_data` and `pandas` - present in the default interpreter here. It
runs from the VAULT ROOT because oi_to_vault.py resolves `../options-dashboard/`
relative to its own caller.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
VAULT = pathlib.Path(
    r"C:\Users\rajvaibhav.yadav\OneDrive - PinPOINT\Obsidian Vault")
SCRIPT = pathlib.Path(
    r"C:\Users\rajvaibhav.yadav\OneDrive - PinPOINT\options-dashboard\oi_to_vault.py")
DB = REPO / "data" / "ims.db"

# ~4s when there is nothing to fetch, minutes when it is backfilling several
# days across 31 symbols. Generous, but finite: a hung NSE request must not
# stall an unattended refresh forever.
TIMEOUT = 900


def store_age() -> tuple[str | None, int | None]:
    import sqlite3
    try:
        conn = sqlite3.connect(DB)
        last = conn.execute("SELECT MAX(date) FROM oi").fetchone()[0]
        conn.close()
    except Exception:
        return None, None
    if not last:
        return None, None
    return last, (dt.date.today() - dt.date.fromisoformat(last)).days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true",
                    help="run the incremental NSE fetch (writes OI History.md)")
    a = ap.parse_args()

    last, age = store_age()
    print(f"store: newest OI row {last or 'EMPTY'}"
          + (f" ({age}d)" if age is not None else ""))

    if not SCRIPT.exists():
        # NOT a failure. The fetcher lives outside this repo, so its absence is a
        # missing capability on this machine, not a broken refresh - and
        # vault_oi.py can still load whatever the files already hold.
        print(f"fetcher NOT FOUND at {SCRIPT}")
        print("skipped — vault_oi.py will load whatever OI History.md already "
              "holds, and freshness.py reports the age")
        return 0

    if not a.fetch:
        print(f"fetcher present: {SCRIPT}")
        print("probe only — pass --fetch to run the incremental NSE download")
        return 0

    print(f"running incremental fetch from {VAULT} …")
    try:
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(VAULT),
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"NSE fetch timed out after {TIMEOUT}s — leaving the existing "
              f"OI History.md files untouched")
        return 1

    tail = [x for x in (r.stdout or "").splitlines() if x.strip()][-3:]
    for line in tail:
        print("  " + line.strip())
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        print(f"fetcher exited {r.returncode}")
        for line in err[-4:]:
            print("  " + line)
        # Missing deps are the likeliest cause and are worth naming, because the
        # fetcher needs the options-dashboard environment, not necessarily this one.
        if any("ModuleNotFoundError" in x for x in err):
            print("  -> needs jugaad_data + pandas; run it under the "
                  "options-dashboard environment")
        return 1
    print("fetch ok — vault_oi.py --load is the next step and reads these files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
