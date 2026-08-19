"""Does the P1 economics score work at MONTHLY frequency, on the pack's data?

Three things changed since P1 last tested flat at every horizon, and each could
matter on its own:

  cp_coke was UNPRICED    specified at 0.40 t/t in aluminium.yaml and absent from
                          the store, so anode cost contributed exactly ZERO to
                          every bridge. It is ~the same order as coal.
  alumina was a FUTURE    ALA=F, carrying a -21.3% front-month roll that is not a
                          price. Now an assessed Australia FOB series.
  lme was a PROXY         ALI=F (CME) carries a Midwest premium basis and read
                          3,355 against the digest's LME 3,310 the same day. Now
                          LME cash.

And the test itself changes: monthly ranking held 90 trading days, which is the
frequency and horizon the regime model works at. The earlier P1 test was daily
scores at h<=20, which could not have seen a slow signal.

Compared against the regime model on the SAME months and the same sizing, so the
question "is the score better than the four-regime rule" has a like-for-like
answer rather than two tables at different frequencies.

Usage:
    python packages/review/p1_monthly.py
"""

from __future__ import annotations

import pathlib
import sqlite3
import statistics
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from regime_pairs import ROTATION  # noqa: E402

TRIO = ["nalco", "hindalco", "vaml"]
# Structural comparability. Hindalco's Mahan/Aditya smelters ramped 2013-16 and
# tripled its Indian aluminium capacity; before that it was mostly copper and
# Novelis and did not track NALCO (r2 0.10-0.20 vs 0.45-0.66 today). The specs
# are all `effective_from: 2026-04-01`, so testing them earlier tests a structure
# that did not exist. 2021 is where beta and r2 settle.
COMPARABLE_FROM = "2021-01"
HOLD = 90


def load(conn):
    px = {}
    for e, d, c in conn.execute("SELECT entity_id,date,close FROM prices "
                                "WHERE close IS NOT NULL"):
        px.setdefault(e, {})[d] = c
    scores = {}
    for as_of, eid, s in conn.execute(
            "SELECT as_of,entity_id,score FROM pillar_scores WHERE pillar='economics' "
            "AND score IS NOT NULL ORDER BY as_of"):
        scores.setdefault(as_of[:7], {})[eid] = s      # last of month wins
    return px, scores


def me(px, e):
    m = {}
    for d in sorted(px.get(e, {})):
        m[d[:7]] = px[e][d]
    return m


def regime_of(px):
    alu, alm = me(px, "lme_aluminium"), me(px, "alumina_index")
    ms = sorted(set(alu) & set(alm))
    out = {}
    for p, q in zip(ms, ms[1:]):
        da, dl = alu[q] / alu[p] - 1, alm[q] / alm[p] - 1
        out[q] = ("R1" if (da > 0 and dl > 0) else "R2" if (da > 0 and dl <= 0)
                  else "R3" if (da <= 0 and dl > 0) else "R4")
    return out


def leg(px, eid, entry, h=HOLD):
    real = "vedanta" if (eid == "vaml" and entry < "2026-06-15") else eid
    days = sorted(px.get(real, {}))
    nxt = [d for d in days if d >= entry]
    if not nxt:
        return None
    i = days.index(nxt[0])
    if i + h >= len(days):
        return None
    a, b = px[real][days[i]], px[real][days[i + h]]
    if real == "vedanta" and days[i] < "2026-04-30" <= days[i + h]:
        return None
    return b / a - 1 if a else None


def stat(pl):
    if len(pl) < 5:
        return None
    sd = statistics.stdev(pl)
    return dict(n=len(pl), win=sum(1 for x in pl if x > 0) / len(pl) * 100,
                avg=statistics.fmean(pl), med=statistics.median(pl),
                sharpe=statistics.fmean(pl) / sd if sd else 0.0)


def main() -> int:
    conn = sqlite3.connect(DB)
    px, sc = load(conn)
    conn.close()
    regs = regime_of(px)

    months = sorted(m for m in sc if m in regs and
                    len([e for e in sc[m] if e in TRIO]) >= 2)
    comp = [m for m in months if m >= COMPARABLE_FROM]
    print(f"P1 economics, month-end, hold {HOLD} trading days")
    print(f"  scored months {len(months)}  {months[0]} .. {months[-1]}")
    print(f"  structurally comparable ({COMPARABLE_FROM}+): {len(comp)}\n")

    def run(ms, pick, wl, ws):
        pl = []
        for m in ms:
            got = pick(m)
            if not got:
                continue
            lng, srt = got
            rl, rs = leg(px, lng, f"{m}-01"), leg(px, srt, f"{m}-01")
            if rl is None or rs is None:
                continue
            pl.append((wl * rl - ws * rs) / (wl + ws) * 100)
        return pl

    def by_score(m):
        have = {e: v for e, v in sc[m].items() if e in TRIO}
        if len(have) < 2:
            return None
        lng = max(have, key=lambda e: have[e])
        srt = min(have, key=lambda e: have[e])
        return None if lng == srt else (lng, srt)

    def by_regime(m):
        return ROTATION[regs[m]]

    for label, ms in (("ALL MONTHS", months), (f"COMPARABLE {COMPARABLE_FROM}+", comp)):
        print(f"{label}")
        print(f"  {'model':22}{'sizing':>8}{'n':>5}{'win%':>7}{'avg%':>8}"
              f"{'med%':>8}{'sharpe':>8}")
        for mname, pick in (("P1 score rank", by_score), ("4-regime rule", by_regime)):
            for sz, (wl, ws) in (("2:1", (2, 1)), ("1:1 MV", (1, 1))):
                s = stat(run(ms, pick, wl, ws))
                if s:
                    print(f"  {mname:22}{sz:>8}{s['n']:>5}{s['win']:>7.0f}"
                          f"{s['avg']:>+8.2f}{s['med']:>+8.2f}{s['sharpe']:>8.3f}")
        print()

    # How often does the score pick the same long leg as the regime rule?
    ag = [(m, by_score(m), ROTATION[regs[m]]) for m in comp]
    ag = [(m, a, b) for m, a, b in ag if a]
    same = sum(1 for _, a, b in ag if a[0] == b[0])
    print(f"score's long leg == regime's long leg on {same}/{len(ag)} comparable months "
          f"({same/len(ag)*100:.0f}%)")
    pick_count: dict[str, int] = {}
    for _, a, _ in ag:
        pick_count[a[0]] = pick_count.get(a[0], 0) + 1
    print("score picks as LONG:", dict(sorted(pick_count.items(), key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
