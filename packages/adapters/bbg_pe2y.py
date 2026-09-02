"""Bloomberg 2-yr-forward P/E, hand-captured from terminal screenshots.

The IT tab's scatter toggles between the 1-yr forward multiple (computed
daily from the Yahoo consensus) and this: Bloomberg's PE Ratio [2 Yr Fwd] —
the BEst blended 24-month forward P/E, which embeds FY29 consensus that no
fetchable source carries (Yahoo's earningsTrend stops at +1y; stockanalysis
403s every UA; the digests' FY29 mentions are capex roadmaps, not EPS).

SO THIS FEED IS A PERSON WITH A TERMINAL, and the load path is built around
that: values arrive in a dated staging JSON transcribed from screenshots
(the screenshots' paths are recorded in the file), each load is idempotent
per capture date, and the as_of comes from the STAGING FILENAME, never the
load date. Only the latest terminal row is transcribed — the 07/03-09/02
daily history visible in the screenshots is deliberately left untranscribed
(~480 numbers of silent-transcription risk, the westmetall lesson; the
images remain the recoverable record).

Rows land as broker='bloomberg', period='BF24M', metric='pe_fwd_2y'. A
MULTIPLE, not an EPS — nothing scores it, nothing blends it; the panel
displays it beside the Yahoo-computed columns with its capture date, because
a screenshot goes stale silently while the Yahoo feed refreshes itself.

Usage:
    python packages/adapters/bbg_pe2y.py --load        # latest staging file
    python packages/adapters/bbg_pe2y.py --load --file data/staging/estimates/bbg_pe2y_2026-09-02.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
STAGING = REPO / "data" / "staging" / "estimates"


def latest_staging() -> pathlib.Path | None:
    files = sorted(STAGING.glob("bbg_pe2y_????-??-??.json"))
    return files[-1] if files else None


def load(path: pathlib.Path | None = None) -> int:
    path = path or latest_staging()
    if path is None:
        print("nothing staged — write data/staging/estimates/bbg_pe2y_YYYY-MM-DD.json first")
        return 0
    doc = json.loads(path.read_text(encoding="utf-8"))
    m = re.search(r"(\d{4}-\d{2}-\d{2})\.json$", path.name)
    if not m:
        raise ValueError(f"staging filename carries no date: {path.name}")
    as_of = m.group(1)
    if as_of > dt.date.today().isoformat():
        raise ValueError(f"refusing future capture date {as_of}")

    values = doc.get("values") or {}
    if not values:
        raise ValueError(f"{path.name} has no values block")
    for eid, v in values.items():
        if not (isinstance(v, (int, float)) and 1.0 < v < 200.0):
            raise ValueError(f"{eid}: implausible 2-yr fwd P/E {v!r}")

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    sid = f"bbg-pe2y-{as_of}"
    conn.execute(
        "INSERT OR IGNORE INTO sources (id,kind,origin,title,source_date,"
        "captured_at,raw_path) VALUES (?,?,?,?,?,?,?)",
        (sid, "consensus", "bloomberg",
         "Bloomberg PE Ratio [2 Yr Fwd] — terminal screenshot capture",
         as_of, now, str(path.relative_to(REPO))))
    conn.execute("DELETE FROM estimates WHERE source_id=?", (sid,))

    quote = (f"Bloomberg PE Ratio [2 Yr Fwd] (BEst blended 24m), terminal "
             f"screenshot {as_of} — see {path.name} for the image paths")
    n = 0
    for eid, v in sorted(values.items()):
        conn.execute(
            "INSERT INTO estimates (source_id,entity_id,broker,as_of,period,"
            "metric,value_num,unit,quote,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, eid, "bloomberg", as_of, "BF24M", "pe_fwd_2y",
             float(v), "x", quote, now))
        n += 1
    conn.commit()
    conn.close()
    print(f"loaded {n} bbg 2-yr fwd P/E rows from {path.name} as source {sid}")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--file", type=pathlib.Path, default=None)
    a = ap.parse_args()
    if a.load:
        sys.exit(0 if load(a.file) else 1)
    print(__doc__)
