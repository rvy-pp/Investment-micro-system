"""Back up the store, and export the parts that CANNOT be regenerated into git.

MOST OF THE DATABASE IS REPRODUCIBLE, which bounds the loss if the disk dies:

  prices          re-fetchable (Yahoo, FRED) + data/staging/ for the Wind series
  oi              re-readable from the vault's OI History files
  broker_actions  re-extractable from the digests
  observations    specs/extracted/*.json are in git
  guidance        same
  pillar_scores   recomputable — run_scores.py --backfill

ONE THING IS NOT: `overrides`. Those are the PM's own corrections, typed into
the Inputs tab and existing nowhere else. Losing them loses judgement, not
data — so they are exported to a git-tracked JSON on every snapshot. That is
the difference between a backup and a versioned record: the DB copy is local
insurance, the overrides export is history.

Usage:
    python packages/core/snapshot.py            # snapshot + export
    python packages/core/snapshot.py --keep 30  # prune older snapshots
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
from datetime import date

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
SNAP = REPO / "snapshots"
EXPORT = REPO / "specs" / "extracted" / "overrides.json"

# Off-machine copy. A SNAPSHOT is safe on OneDrive because it is closed,
# complete and write-once — nothing holds it open.
#
# THE LIVE REPO AND LIVE DATABASE ARE NOT, and must stay off OneDrive:
#   .git      writes many small objects plus index.lock on every operation;
#             sync grabbing one mid-write gives lock contention and corrupt
#             packfiles
#   ims.db    WAL mode keeps ims.db, -wal and -shm mutually consistent.
#             OneDrive syncs them as three independent files on its own
#             schedule, so a restore can yield a database that OPENS and is
#             silently wrong — the worst failure mode available.
# Evidence the sync layer rewrites aggressively: 31 vault files shared one
# mtime to the second, having not been individually edited.
#
# OUTSIDE the vault deliberately: the vault is Obsidian-indexed and the
# dashboard's serve.js recursively watches its directories, rebuilding on any
# filesystem event. A daily binary drop there would trigger spurious rebuilds.
# "OneDrive - PinPOINT" is the firm's actual OneDrive folder name and must stay
# as-is; only the project-owned subfolder was renamed.
MIRROR = pathlib.Path(r"C:\Users\rajvaibhav.yadav\OneDrive - PinPOINT"
                      r"\Investment-micro-system-backups")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=30)
    ap.add_argument("--mirror", action="store_true",
                    help="also copy the snapshot to OneDrive (safe: closed file)")
    a = ap.parse_args()

    if not DB.exists():
        print(f"no database at {DB}", file=sys.stderr)
        return 1
    SNAP.mkdir(exist_ok=True)

    # sqlite's own backup API, not a file copy — a copy taken mid-write can
    # capture a torn WAL and restore to a corrupt database.
    stamp = date.today().isoformat()
    dest = SNAP / f"ims-{stamp}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()

    rows = [dict(zip([c[0] for c in src.execute(
        "SELECT * FROM overrides LIMIT 0").description], r))
        for r in src.execute("SELECT * FROM overrides ORDER BY id")]
    src.close()

    EXPORT.write_text(json.dumps(
        {"_note": "PM overrides — the one part of the store that cannot be "
                  "regenerated. Exported on every snapshot so judgement is "
                  "versioned, not just backed up.",
         "exported": stamp, "overrides": rows}, indent=2), encoding="utf-8")

    snaps = sorted(SNAP.glob("ims-*.db"))
    pruned = 0
    for old in snaps[:-a.keep] if len(snaps) > a.keep else []:
        old.unlink()
        pruned += 1

    print(f"snapshot  {dest.name}  {dest.stat().st_size/1e6:.2f} MB")
    print(f"exported  {len(rows)} override(s) -> {EXPORT.relative_to(REPO)} (git-tracked)")
    print(f"retained  {min(len(snaps), a.keep)} snapshot(s)"
          + (f", pruned {pruned}" if pruned else ""))

    if a.mirror:
        MIRROR.mkdir(parents=True, exist_ok=True)
        target = MIRROR / dest.name
        shutil.copy2(dest, target)
        # keep the mirror shallow — OneDrive charges for every version it keeps
        for old in sorted(MIRROR.glob("ims-*.db"))[:-7]:
            old.unlink()
        print(f"mirrored  {target}")
        print("          OneDrive adds cloud redundancy and ~30d version history")

    print("\nsnapshots/ is gitignored — local insurance against a bad write.")
    if not a.mirror:
        print("Nothing is off this machine. --mirror copies to OneDrive; code and")
        print("authored data still want a git remote, which is not configured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
