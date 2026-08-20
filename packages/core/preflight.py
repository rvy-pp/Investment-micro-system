"""Integrity checks that run BEFORE anything computes.

Every check here exists because something already failed silently in this repo.
A bug that raises is cheap; a bug that returns a plausible number survives a full
analysis and two written reports, which is exactly what the unit check below
failed to prevent.

Usage:
    python packages/core/preflight.py
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "packages" / "score"))


def main() -> int:
    from bridge import load_specs
    ents, units, fin = load_specs()
    conn = sqlite3.connect(REPO / "data" / "ims.db")
    priced = {r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM prices")}
    fails, warns = [], []

    # 1. EVERY price_link MUST have a units entry.
    #
    # `to_inr` converts only when the unit string starts with "USD" and otherwise
    # returns the delta UNCHANGED. So a link with no units entry silently loses
    # the FX leg. lme_zinc had none: a +10% zinc move read Rs32 cr instead of
    # Rs3,014 cr — a 95x understatement, no error, no warning, coverage_ok true.
    # It survived a full HZL analysis and two reports before a sanity check on
    # the magnitude caught it.
    for eid, e in sorted(ents.items()):
        for kind in ("outputs", "inputs"):
            for ln in (e.get(kind) or []):
                link = ln.get("price_link")
                if not link:
                    continue
                u = units.get(link)
                if u is None:
                    fails.append(f"{eid}.{ln.get('item')}: price_link '{link}' has "
                                 f"NO units entry — to_inr will skip the FX leg")
                elif not u.strip():
                    fails.append(f"{eid}.{ln.get('item')}: price_link '{link}' has "
                                 f"an EMPTY unit — same failure as no entry")
                if link not in priced:
                    warns.append(f"{eid}.{ln.get('item')}: '{link}' has no price "
                                 f"series in the store — the line cannot be scored")

    # 2. A scoreable entity needs a denominator. base_ebitda IS the beta.
    for eid, e in sorted(ents.items()):
        if not e.get("peer_group"):
            continue
        if not fin["companies"].get(eid, {}).get("base_ebitda"):
            fails.append(f"{eid}: scoreable but has no base_ebitda — every score "
                         f"for this name would be silently rescaled")
        if eid not in priced:
            warns.append(f"{eid}: scoreable but has no price series")

    # 3. The base quarter must exist and be coherent. base_ebitda is THIS
    # quarter's print and P3 re-marks from THIS quarter's average prices; if the
    # block is missing or reversed, valuation marks earnings against the wrong
    # window and says nothing about it.
    import datetime as _dt
    bq = fin.get("base_quarter") or {}
    if not (bq.get("start") and bq.get("end")):
        fails.append("base_financials.yaml has no base_quarter{start,end} — "
                     "P3 valuation cannot re-mark")
    else:
        try:
            s_, e_ = (_dt.date.fromisoformat(str(bq["start"])),
                      _dt.date.fromisoformat(str(bq["end"])))
            if e_ <= s_:
                fails.append(f"base_quarter end {e_} is not after start {s_}")
            elif e_ > _dt.date.today():
                warns.append(f"base_quarter ends {e_}, in the future — the "
                             f"re-mark reference has not finished yet")
            elif (_dt.date.today() - e_).days > 200:
                warns.append(f"base_quarter ended {e_}, {(_dt.date.today()-e_).days}d "
                             f"ago — safe but the re-mark window is long; consider "
                             f"rolling base_ebitda AND base_quarter together")
        except ValueError:
            fails.append(f"base_quarter dates do not parse: {bq}")

    conn.close()
    print(f"preflight — {len(ents)} entities, {len(units)} priced units\n")
    for f in fails:
        print(f"  FAIL  {f}")
    for w in warns:
        print(f"  warn  {w}")
    if not fails and not warns:
        print("  all checks pass")
    print(f"\n{len(fails)} failure(s), {len(warns)} warning(s)")
    if fails:
        print("\nHALT: a failure here means downstream numbers are plausible and "
              "wrong.\nDo not run the bridge until they are fixed.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
