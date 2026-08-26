"""Compute all pillars for a date and PERSIST them. The system's daily write.

Until this existed the pillars printed and vanished, so there was no score
history — nothing to backtest, nothing for the review layer to grade, and no
way to ask whether the pillars disagreed last week.

Every row is stamped with spec_version and the git sha of the code that
produced it, so a scoring change is a re-run over history rather than a silent
rewrite of what the system used to think.

WITHHELD IS RECORDED, NOT SKIPPED. A name with no share count gets a row saying
so. Otherwise a gap in the history is ambiguous — did we not score it, or did
we score it and get nothing? The review layer needs to tell those apart.

Usage:
    python packages/score/run_scores.py --as-of 2026-08-15
    python packages/score/run_scores.py --backfill 60
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import subprocess
import sys
import datetime as dt
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
DB = REPO / "data" / "ims.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bridge import (load_specs, load_scoring, load_accumulation, run_bridge,  # noqa: E402
                    shocks_from_store, _series_in_store)
from scoring import score as to_score, solve_k  # noqa: E402
import mood as mood_mod  # noqa: E402
import valuation as val_mod  # noqa: E402
import guidance_runrate as gr  # noqa: E402
import guidance_forward as gf  # noqa: E402

SPEC_VERSION = "0.5.0"
# Pillar weights for the composite. Economics leads because it is the only
# pillar measuring the thing itself; the rest qualify it.
#
# GUIDANCE IS WEIGHTED ZERO — PM decision, 2026-08-25. Its 0.15 was redistributed
# EQUALLY (+0.05 each) rather than proportionally, which is what was asked for and
# is worth stating because the two differ: proportional would have given economics
# +0.078 and mood +0.026, tilting the composite further toward economics. Equal
# redistribution lifts mood's relative share the most (0.15 -> 0.20, +33%).
#
#     before   economics 0.45  valuation 0.25  mood 0.15  guidance 0.15
#     after    economics 0.50  valuation 0.30  mood 0.20  guidance 0.00
#
# WHY, in the PM's words: "guidance is making it obscure". The pillar had just
# been rebuilt twice in one day — forward-scoring, then centred on the peer median
# — and still needed a placeholder for JSW and carried a probable basis artefact
# on SAIL. A term nobody can read is worse than a term that is absent, because it
# moves the ranking while looking like information.
#
# THE PILLAR IS STILL COMPUTED, STORED AND SHOWN. Only its composite weight is
# zero. That is deliberate: it stays visible and auditable, accumulates history
# for the day it earns weight back, and can be re-weighted with a one-line edit
# and a re-run. Deleting it would throw away the concall ledger work.
#
# TWO CONSEQUENCES, both real:
#
#  1. THE JSW GUIDANCE PLACEHOLDER IS NOW INERT. Its entire purpose was to stop
#     the composite renormalising over three pillars instead of four. At weight
#     zero, renormalisation is identical whether guidance scored or not, so the
#     grant changes nothing — JSW's composite is the same 2.60 with or without it.
#     Noted in specs/placeholders.yaml; it is a candidate for revocation rather
#     than something to leave reporting on every run as though it mattered.
#
#  2. `conviction.py` STILL USES GUIDANCE AS A MULTIPLIER, and that is NOT
#     changed here. Its sizing rule is
#         size = (economics - 3) x f(valuation) x g(guidance)
#     which is a different mechanism from a weighted average — a withheld pillar
#     returns exactly 1.0 there rather than reweighting a denominator. Removing
#     guidance from the sizing path is a separate decision and has not been taken.
#     So guidance currently has zero weight in the SCORE and non-zero influence on
#     the SIZE. That is inconsistent on its face and is flagged rather than
#     quietly harmonised.
WEIGHTS = {"economics": 0.50, "valuation": 0.30, "mood": 0.20, "guidance": 0.00}


# --------------------------------------------------------------------------
# PLACEHOLDER PILLAR SCORES. See specs/placeholders.yaml for why this exists at
# all and why it is deliberately awkward. Loaded per run so revoking one is a
# file edit plus a re-run, with nothing to unwind.
#
# A placeholder is written as a REAL score with `detail.placeholder = true` and
# the original withhold reason carried alongside it. `withheld` stays NULL,
# because the column means "why there is no score" and here there is one — the
# fact that it was manufactured belongs in the decomposition, which is what
# `detail` is for. Every consumer that distinguishes withheld from scored keys
# on `score IS NULL`, so this does not silently reclassify anything.
# --------------------------------------------------------------------------
def load_placeholders() -> dict:
    import datetime as _dt
    f = REPO / "specs" / "placeholders.yaml"
    if not f.exists():
        return {}
    try:
        import yaml
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        # A malformed placeholder file must not take the whole run down, but it
        # must not pass silently either — silence here means scores quietly stop
        # carrying a placeholder the desk believes is in force.
        print(f"  WARNING: specs/placeholders.yaml unreadable ({exc}); "
              f"no placeholders applied — affected pillars will be WITHHELD")
        return {}
    out = {}
    today = _dt.date.today()
    for pillar, ents in (doc or {}).items():
        if pillar in ("spec_version",) or not isinstance(ents, dict):
            continue
        for eid, cfg in ents.items():
            if not isinstance(cfg, dict) or cfg.get("score") is None:
                continue
            age = None
            granted = cfg.get("granted")
            if granted:
                try:
                    age = (today - _dt.date.fromisoformat(str(granted))).days
                except ValueError:
                    age = None
            lim = cfg.get("escalate_after_days")
            out[(pillar, eid)] = {
                "pillar": pillar,
                "score": float(cfg["score"]),
                # str(): PyYAML turns an unquoted 2026-08-24 into datetime.date,
                # and `detail` is persisted as JSON.

                "reason": (cfg.get("reason") or "").strip(),
                "granted": (str(granted) if granted is not None else None),
                "granted_by": cfg.get("granted_by"),
                "review_trigger": cfg.get("review_trigger"),
                "age_days": age,
                "overdue": bool(lim and age is not None and age > lim),
                "escalate_after_days": lim,
            }
    return out


def code_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                              capture_output=True, text=True).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put(conn, as_of, eid, pillar, score, raw, detail, withheld, sha):
    conn.execute(
        "INSERT OR REPLACE INTO pillar_scores (as_of,entity_id,pillar,score,raw,"
        "detail,withheld,spec_version,code_sha,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (as_of, eid, pillar, score, raw,
         json.dumps(detail) if detail else None, withheld,
         SPEC_VERSION, sha, now()))


PLACEHOLDERS = load_placeholders()
PH_APPLIED: list = []
PH_SUPERSEDED: list = []


def score_one_date(conn, as_of: str, sha: str,
                   skip_gate: bool = False) -> int:
    entities, units, fin = load_specs()
    fins = fin["companies"]
    form, k, p = load_scoring()
    acc, hl = load_accumulation()
    available = _series_in_store()
    # One definition of the base quarter, from base_financials.yaml. It used to
    # be a literal here while base_ebitda lived in the spec, so the two could
    # drift apart silently and mark one quarter's earnings against another
    # quarter's prices.
    bq = fin.get("base_quarter") or {}
    if not (bq.get("start") and bq.get("end")):
        raise SystemExit("base_financials.yaml has no base_quarter{start,end}; "
                         "P3 valuation cannot re-mark without it.")
    n = 0

    shocks, _detail, _resolved, fx = shocks_from_store(30, as_of, acc, hl)
    usdinr = fx or fin["usdinr"]

    # ---- THE STALENESS GATE ------------------------------------------------
    # docs/DAILY_MONITORING.md, Tier 1: "On any staleness breach: report it, and
    # withhold the affected scores. Invariant 7 — withhold rather than guess. A
    # stale price is not a flat price."
    #
    # That was written 2026-08-18 and never implemented. On 2026-08-21 this file
    # produced five economics scores on inputs 4 to 81 days stale, with ZERO
    # withheld rows, while freshness.py printed a warning nobody was required to
    # read. On a cron nobody reads it at all.
    #
    # Withholding is not a cost here — it is the product. A recorded withholding
    # says "we could not tell", which the review layer can grade. A score
    # computed on a dead feed says "nothing is happening", which is a claim, and
    # a false one: carry-forward means a broken source and a quiet market are
    # byte-identical in the store (cp_coke has 3 distinct closes in 30 rows).
    #
    # PER ENTITY, not global. A stale coal print must not withhold a zinc name
    # that never consumes coal, so the gate resolves each entity's own
    # price_links and only fires on the ones it actually reads.
    stale_series: dict[str, str] = {}
    if not skip_gate:
        sys.path.insert(0, str(REPO / "packages" / "core"))
        import freshness
        fr = freshness.check(dt.date.fromisoformat(as_of))
        stale_series = {x["series"]: x["age_txt"] for x in fr["stale"]
                        if x["series"] != "oi"}   # OI is not a bridge input
        if stale_series:
            print(f"staleness gate: {len(stale_series)} series over threshold — "
                  + ", ".join(f"{k} ({v})" for k, v in sorted(stale_series.items())))


    def consumed_by(ent: dict) -> set[str]:
        """price_links this entity reads, including through its reporting units."""
        out = set()
        for e in [ent] + [x for x in entities.values()
                          if x.get("parent_id") == ent["id"]]:
            for ln in (e.get("outputs") or []) + (e.get("inputs") or []):
                if ln.get("price_link"):
                    out.add(ln["price_link"])
        return out

    val_k = solve_k("hill", val_mod.Z_ANCHOR, val_mod.SCORE_ANCHOR, val_mod.P)
    mood_k = solve_k("hill", mood_mod.MOOD_ANCHOR, mood_mod.SCORE_ANCHOR, mood_mod.P)

    for ent in sorted(entities.values(), key=lambda e: e["id"]):
        pg = ent.get("peer_group")
        if not pg:
            continue
        eid = ent["id"]
        f = fins.get(eid, {})
        parts: dict[str, float] = {}

        # --- economics (P1+P2) ---
        # Gate first: a stale INPUT invalidates the bridge before coverage does.
        # coverage_ok asks "is every line priced at all", which a carried-forward
        # dead feed answers yes to.
        bad = sorted(consumed_by(ent) & set(stale_series))
        r = run_bridge(ent, shocks, units, f.get("base_ebitda", 0), usdinr,
                       available | set(shocks))
        pct = r["pct_of_ebitda"]
        if bad:
            put(conn, as_of, eid, "economics", None, pct, None,
                "stale inputs: " + "; ".join(f"{s} {stale_series[s]}" for s in bad),
                sha)
        elif pct is not None and r["coverage_ok"]:
            s = to_score(pct, k, form, p)
            parts["economics"] = s
            put(conn, as_of, eid, "economics", s, pct,
                {"d_ebitda_cr": round(r["d_ebitda_cr"], 1),
                 "priced": f"{r['n_priced']}/{r['n_total']}"}, None, sha)
        else:
            put(conn, as_of, eid, "economics", None, pct, None,
                f"coverage {r['n_priced']}/{r['n_total']}", sha)
        n += 1

        # --- valuation (P3a) ---
        ser, cut, _cp, err = val_mod.spot_multiple_series(
            conn, ent, f, units, bq["start"], bq["end"], usdinr,
            lookback_days=val_mod.spec_lookback(pg), as_of=as_of)
        # P3 re-marks EV/EBITDA at the CURRENT price, so a stale equity close
        # means it is re-marking at an old price and calling it spot.
        if eid in stale_series:
            put(conn, as_of, eid, "valuation", None, None, None,
                f"stale price: {eid} {stale_series[eid]}", sha)
        elif len(ser) >= 20:
            import statistics
            mults = [m for _, m, _ in ser]
            z = (mults[-1] - statistics.fmean(mults)) / (statistics.pstdev(mults) or 1e-9)
            s = to_score(-z, val_k, "hill", val_mod.P)
            parts["valuation"] = s
            put(conn, as_of, eid, "valuation", s, z,
                {"multiple": round(mults[-1], 2), "n": len(mults)}, None, sha)
        else:
            put(conn, as_of, eid, "valuation", None, None, None,
                err or "insufficient clean history", sha)
        n += 1

        # --- mood (P3b) ---
        c_raw, brokers, events = mood_mod.company_mood(conn, eid, as_of)
        s_raw, pol = mood_mod.policy_mood(conn, eid, ent.get("sector") or "", as_of)
        gate = mood_mod.gate(len(brokers), len(events) + len(pol))
        eff = (c_raw + s_raw) * gate
        if events or pol:
            s = to_score(eff, mood_k, "hill", mood_mod.P)
            parts["mood"] = s
            put(conn, as_of, eid, "mood", s, eff,
                {"brokers": len(brokers), "events": len(events) + len(pol),
                 "gate": round(gate, 2)}, None, sha)
        else:
            put(conn, as_of, eid, "mood", None, None, None, "no events", sha)
        n += 1

        # --- guidance (P4) ---
        # RUN-RATE ARITHMETIC, replacing the hand-weighted evidence vote that was
        # here until 2026-08-20. The old version summed guidance_evidence
        # direction x weight through a sigmoid and never read target_value at
        # all, so HZL's committed 1.1mt / 680t / $975-1,000 played no part in
        # scoring HZL's own guidance. Worse, three of its five evidence rows were
        # "guidance reiterated" — management repeating itself drifting confidence
        # UPWARD, the inverse of invariant 3. It scored HZL 3.61 while the company
        # was behind on both volume commitments.
        #
        # Now: actual vs target, cited both ends, weighted by how much of the
        # period has elapsed. guidance_evidence is no longer read here; it is for
        # facts arithmetic cannot reach (a regulatory clearance), not for numbers
        # it can.
        # TWO SCORERS, ONE PILLAR, AND THE CHOICE IS THE PERIOD'S STATE.
        #
        # guidance_runrate answers "given what has printed, are they on track in
        # the period already running" and needs a cited ACTUAL.
        # guidance_forward answers "will they hit a period that has not reported"
        # and needs NO actual — it uses the demonstrated delivery record plus the
        # observable divergence of the guided driver from the guided path.
        #
        # CLAUDE.md defines P4 as "Will they hit the quarter? — forward view", and
        # until 2026-08-25 only the run-rate scorer existed, so the pillar could
        # not score the one thing it was specified for: Tata Steel's Q2FY27
        # guidance was withheld as "no actual" when the absence of an actual is
        # exactly what makes a guide forward-looking.
        #
        # RUN-RATE WINS WHERE BOTH APPLY. A period with a reported print is better
        # graded against that print than against a proxy series, and letting the
        # forward score override it would replace a measurement with an inference.
        # Forward fills in only where run-rate has nothing to stand on.
        # ONE SCORER FOR THE WHOLE COLUMN. guidance_forward now absorbs the
        # run-rate gap as evidence rather than competing with it, so every name is
        # scored on one question ("will they hit it, versus a typical company
        # here") on one scale, with 3.00 meaning neutral for all of them.
        #
        # It used to try run-rate first and fall back to forward, which meant the
        # guidance column answered a different question per name depending on
        # whether a period had reported — Tata on peer-relative credibility,
        # SAIL on its own FY27 volume run-rate. A composite cannot blend a column
        # that means two things.
        gscore, gconf, gdetail, gwithheld = gf.score_entity(conn, eid, as_of)
        if isinstance(gdetail, dict):
            gdetail = {"basis": "forward+runrate", **gdetail}
        ph = PLACEHOLDERS.get(("guidance", eid))
        if gscore is not None:
            parts["guidance"] = gscore
            put(conn, as_of, eid, "guidance", gscore, gconf, gdetail, None, sha)
            if ph:
                # The measurement caught up. Do not quietly keep the placeholder
                # in the file: a stale grant that no longer applies is a lie the
                # next reader has to discover.
                PH_SUPERSEDED.append(eid)
        elif ph:
            parts["guidance"] = ph["score"]
            put(conn, as_of, eid, "guidance", ph["score"], None,
                {"placeholder": True, "value": ph["score"],
                 "withheld_reason": gwithheld,
                 "reason": ph["reason"], "granted": ph["granted"],
                 "granted_by": ph["granted_by"],
                 "review_trigger": ph["review_trigger"],
                 "age_days": ph["age_days"], "overdue": ph["overdue"],
                 "source": "specs/placeholders.yaml"},
                None, sha)
            PH_APPLIED.append((eid, ph))
        else:
            put(conn, as_of, eid, "guidance", None, None, None, gwithheld, sha)
        n += 1

        # --- composite: re-weighted over the pillars that ACTUALLY scored ---
        # Treating a withheld pillar as 3.0 would let missing data masquerade as
        # neutral evidence. Renormalising over what exists keeps the composite
        # honest, and `covered` records how much of the intended weight it rests on.
        if parts:
            wsum = sum(WEIGHTS[k_] for k_ in parts)
            # GUARD ADDED WITH THE ZERO WEIGHT. This division was safe while every
            # pillar carried weight: `parts` non-empty implied wsum > 0. With
            # guidance at 0.00 that stops holding — a name whose ONLY scored
            # pillar is guidance now yields wsum == 0.0 and a ZeroDivisionError.
            # Rare but reachable: a new name with a concall ledger and no prices
            # would hit it on its first run. Withhold instead, which is what
            # "nothing with weight has scored" honestly means.
            comp = (sum(parts[k_] * WEIGHTS[k_] for k_ in parts) / wsum
                    if wsum else None)
            put(conn, as_of, eid, "composite", comp, None,
                {"pillars": {k_: round(v, 2) for k_, v in parts.items()},
                 "covered": round(wsum, 2)}, None, sha)
        else:
            put(conn, as_of, eid, "composite", None, None, None,
                "no pillar scored", sha)
        n += 1

    # ---- second pass: placeholder-only entities ----------------------------
    # The loop above skips anything with no peer_group, which is correct — an
    # untradeable reporting unit is not ranked against listed names, and the
    # schema forbids giving it one (CHECK peer_group IS NULL OR is_tradeable=1).
    # But a unit CAN still carry a placeholder, and Novelis does: its economics
    # cannot be computed because al_scrap_midwest has no source anywhere, so the
    # PM set it flat at 3.0 rather than leaving it absent.
    #
    # These rows are written from placeholders ONLY. Nothing is computed here and
    # nothing may be: the moment this pass starts deriving a number it stops being
    # a recorded decision and becomes an unreviewed model.
    for ent in sorted(entities.values(), key=lambda e: e["id"]):
        eid = ent["id"]
        if ent.get("peer_group"):
            continue
        mine = {p_: ph for (p_, e_), ph in PLACEHOLDERS.items() if e_ == eid}
        if not mine:
            continue
        parts = {}
        for pillar, ph in sorted(mine.items()):
            parts[pillar] = ph["score"]
            put(conn, as_of, eid, pillar, ph["score"], None,
                {"placeholder": True, "value": ph["score"],
                 "withheld_reason": "not computed — placeholder-only entity",
                 "reason": ph["reason"], "granted": ph["granted"],
                 "granted_by": ph["granted_by"],
                 "review_trigger": ph["review_trigger"],
                 "age_days": ph["age_days"], "overdue": ph["overdue"],
                 "source": "specs/placeholders.yaml"}, None, sha)
            PH_APPLIED.append((eid, ph))
        wsum = sum(WEIGHTS[k_] for k_ in parts if k_ in WEIGHTS)
        comp = (sum(parts[k_] * WEIGHTS[k_] for k_ in parts if k_ in WEIGHTS)
                / wsum) if wsum else None
        put(conn, as_of, eid, "composite", comp, None,
            {"pillars": {k_: round(v, 2) for k_, v in parts.items()},
             "covered": round(wsum, 2), "placeholder": True,
             "reason": "every pillar is a placeholder; nothing here is measured",
             "source": "specs/placeholders.yaml"}, None, sha)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--no-placeholders", action="store_true",
                    help="ignore specs/placeholders.yaml — the affected pillars "
                         "go back to withheld, for checking what a placeholder "
                         "is actually doing to the book")
    # A BACKFILL MUST SKIP THE GATE. freshness.check() measures age against the
    # date being scored, so every historical date is "stale" by construction —
    # scoring 2021 would withhold all of it. The gate is about TODAY's feeds
    # being alive, which is only a meaningful question for a current run.
    ap.add_argument("--skip-gate", action="store_true",
                    help="score even on stale feeds (implied by --backfill)")
    a = ap.parse_args()

    # --no-placeholders wipes the loaded table rather than threading a flag
    # through score_one_date. Same effect, one place to reason about.
    global PLACEHOLDERS
    if a.no_placeholders and PLACEHOLDERS:
        print(f"  --no-placeholders: ignoring {len(PLACEHOLDERS)} placeholder(s); "
              f"those pillars will be WITHHELD")
        PLACEHOLDERS = {}

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    sha = code_sha()

    if a.backfill:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM prices WHERE entity_id='lme_aluminium' "
            "ORDER BY date DESC LIMIT ?", (a.backfill,))]
        dates = sorted(dates)
    else:
        dates = [a.as_of or conn.execute(
            "SELECT MAX(date) FROM prices").fetchone()[0]]

    total = 0
    for d in dates:
        total += score_one_date(conn, d, sha, skip_gate=a.skip_gate or bool(a.backfill))
        conn.commit()
    print(f"wrote {total} pillar_scores rows across {len(dates)} dates "
          f"({dates[0]} .. {dates[-1]}) at spec {SPEC_VERSION} / {sha}")

    # REPORTED EVERY RUN, ON PURPOSE. A placeholder that stops being mentioned
    # stops being a placeholder and becomes a number nobody questions. Deduped
    # across backfill dates so a 60-date run prints it once, not sixty times.
    if PH_APPLIED:
        seen = {}
        for eid, ph in PH_APPLIED:
            seen[(eid, ph.get("pillar"))] = ph
        print("")
        print(f"PLACEHOLDER pillar score(s) in force — specs/placeholders.yaml:")
        for (eid, pillar), ph in sorted(seen.items(),
                                        key=lambda kv: (kv[0][0], kv[0][1] or "")):
            age = "" if ph["age_days"] is None else f", {ph['age_days']}d old"
            flag = "  <-- OVERDUE, REVIEW IT" if ph["overdue"] else ""
            print(f"   {(pillar or '?'):10}{eid:16}{ph['score']:.2f}  "
                  f"granted {ph['granted']} by {ph['granted_by']}{age}{flag}")
            print(f"             ends on: {ph['review_trigger']}")
            print(f"             reason: {ph['reason'][:92]}")
        print("   These are DECISIONS, not measurements. `--no-placeholders` "
              "re-runs without them.")
    if PH_SUPERSEDED:
        print("")
        print(f"PLACEHOLDER NO LONGER NEEDED — the scorer now produces a real "
              f"number for: {', '.join(sorted(set(PH_SUPERSEDED)))}")
        print("   Remove the block from specs/placeholders.yaml; it is being "
              "ignored, and a stale grant misleads the next reader.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
