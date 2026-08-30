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
import time
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "refresh"
PY = sys.executable

# (label, argv, fatal). fatal=True means later steps are pointless if it fails —
# Tier 0 in DAILY_MONITORING.md is explicitly "HALT", not "warn and continue",
# because everything downstream of a failed integrity check is untrustworthy
# rather than merely missing.
# SQLite reports contention with two different messages depending on whether the
# whole file or a single table is held. Matching only the first would let the
# other one through as a hard failure, which is the bug this helper exists to stop
# — so both are matched, case-insensitively, and the check is deliberately narrow:
# any OTHER OperationalError is a real failure and must stay red.
_LOCK_MARKERS = ("database is locked", "database table is locked")


def _is_locked(stderr: str | None) -> bool:
    t = (stderr or "").lower()
    return any(m in t for m in _LOCK_MARKERS)


STEPS = [
    ("preflight",         ["packages/core/preflight.py"],                     True),
    ("equity closes",     ["packages/adapters/yahoo_prices.py", "--load",
                           "--range", "3mo"],                                 False),
    # The morning brief's deterministic half: US/semis/IT closes, GIFT Nifty,
    # entity-keyed headlines -> data/morning/markets_<date>.json, display-only,
    # never prices. NOT in SKIP_IF_DONE, same logic as the equity load: GIFT
    # trades ~21h, so a second double-click later in the morning pulls a
    # fresher print and re-pulling is the point. The brief_*.json half is
    # agent-only (M365) — see .claude/skills/morning-brief.
    ("morning markets",   ["packages/adapters/morning_markets.py"],           False),
    # LME cash. Promoted from the manual list 2026-08-21 once it turned out
    # westmetall answers plain urllib with HTTP 200 — the earlier
    # "needs an agent" was inferred from lme.com's 403 without testing the
    # mirror. This is an ordinary cron adapter.
    ("LME cash",          ["packages/adapters/westmetall.py", "--load"],      False),
    # THE FETCH, added 2026-08-24. `vault_oi.py` only READS OI History.md; the
    # /oi pipeline in the vault writes them, and nothing here had ever called it.
    # So this file re-read files nobody was updating and reported OI as "already
    # pulled today" while the newest row aged — 7 days by the time it was noticed.
    # One incremental run moved 31 names from 2026-08-17 to 2026-08-24. Must come
    # BEFORE the load, and shares the once-a-day guard with it.
    ("NSE OI fetch",      ["packages/adapters/nse_oi.py", "--fetch"],         False),
    # OI was missing from this list until 2026-08-21 and had gone 5 trading days
    # stale unnoticed, because nothing checked a table outside `prices`.
    # pipeline.py STEP 1 always ran it; this file simply forgot to.
    ("open interest",     ["packages/adapters/vault_oi.py", "--load"],        False),
    # Monthly series, but pulled daily because the cost is one request and the
    # failure mode of forgetting is a coal price that silently ages for weeks.
    ("FRED coal",         ["packages/adapters/fred_prices.py", "--load"],     False),
    # Mining primary-source filings: CIL production/offtake + SWMA e-auction
    # from coalindia.in (timely, ~1st of the month) and the NMDC CMS lists
    # (currently ~6 months stale — the fetch no-ops until the site catches up,
    # and NMDC's recent months arrive via specs/extracted/mining_prints.json).
    # Fetch is monthly-cadence work run daily for the same reason as FRED:
    # one page request each, and the failure mode of forgetting is a volume
    # series that silently ages. --load recomputes from staging + ledger, so
    # a hand-edit to mining_prints.json lands on the next refresh.
    ("mining filings (fetch)", ["packages/adapters/mining_filings.py", "--fetch"], False),
    ("mining filings (load)",  ["packages/adapters/mining_filings.py", "--load"],  False),
    # EMS consensus EPS (Yahoo earningsTrend, cookie+crumb, stdlib-only — no
    # login, so an ordinary unattended step). Daily because the P3 forward-P/E
    # scorer gates on capture age (30d) and, longer-term, because the
    # accumulated captures are the only route to an own-history variant —
    # historical consensus can never be backfilled, the same "a series you
    # are not capturing today is history you cannot recover" logic as the
    # pack. Fetch stages data/staging/estimates/; load is idempotent per
    # capture date.
    ("EMS consensus (fetch)", ["packages/adapters/yahoo_estimates.py", "--fetch"], False),
    ("EMS consensus (load)",  ["packages/adapters/yahoo_estimates.py", "--load"],  False),
    # THE PACK IS NO LONGER AN AGENT-ONLY STEP. Promoted 2026-08-24, same shape
    # as westmetall's promotion: the "needs an agent" was inferred from the M365
    # connector's limits and never tested against Outlook itself. Outlook has the
    # real .xlsx bytes and PowerShell can reach MAPI, so this is now an ordinary
    # unattended step that costs no tokens. Exit 1 means Outlook was unreachable
    # (a red step); exit 0 with no mail means a quiet day (fallbacks supply).
    ("metals pack (Outlook)", ["packages/adapters/outlook_pack.py", "--save"],
                                                                       False),
    # --- steps that consume what was staged, if anything was staged -----------
    # These are no-ops when no staging file exists for today, which is the
    # correct behaviour for a Python-only run: the MCP steps genuinely cannot
    # run here. They are listed so a full agent-driven run has ONE sequence
    # rather than two, per the PM's instruction that every fetch happens in one
    # run. `--if-staged` makes each a skip, never a failure, when absent.
    ("metals pack (staged)", ["packages/refresh.py", "--consume", "metals"],  False),
    # The CEMENT pack, unlike the metals pack, comes through the M365 connector
    # complete — it is a WIDE sheet, so the whole workbook fits inside the
    # connector's character cap. That makes it an agent step rather than an
    # unattended one (the connector is agent-callable only), but a cheap one:
    # one read, no Outlook automation. See adapters/cement_pack.py.
    ("cement pack (staged)", ["packages/refresh.py", "--consume", "cement"],  False),
    # THE ONLY STEP HERE THAT IS A WATCH RATHER THAN A FEED. Scrapes IndiaMART
    # dealer asks into `cement_watch`, never into `prices`, and no pillar reads
    # it. It earns its place because the Kotak pack lands ~15 days late, so a
    # multi-region move in asks is knowable before the priced series shows it.
    # SLOW BY NECESSITY (32 pages at 12s apart, ~6.5 min — IndiaMART 429s at
    # roughly 15 rapid requests), WHICH IS WHY IT RUNS DETACHED — see
    # BACKGROUND below. The .vbs launcher runs this whole file BLOCKING before
    # it opens the page, so a foreground sweep would turn an ~18s morning
    # launch into ~7 minutes of staring at nothing. It did, on 2026-08-28,
    # for one day. Never fatal: a scraped side-signal must not be able to
    # stop a scoring run.
    ("cement watch (IndiaMART)",
     ["packages/adapters/indiamart_cement.py", "--capture"],                False),
    ("mail watch (staged)",  ["packages/refresh.py", "--consume", "mail"],    False),
    # ADVISORY, NEVER WRITES. Hindalco's and Novelis' base numbers are static by
    # the PM's instruction and change once a quarter from the public release.
    # This says whether that quarter has turned. Two HTTP requests; it cannot
    # alter a score, so it is safe on every run and its only job is to stop a
    # stale base_quarter going unnoticed for three months.
    ("concall check",     ["packages/adapters/concall_check.py"],             False),
    # Also advisory. No-ops with a one-line message when data/models/ is empty,
    # so it is safe on a machine that has no model.
    ("desk model check",  ["packages/adapters/hindalco_model.py"],            False),
    ("corporate actions", ["packages/adapters/check_corporate_actions.py"],   False),
    ("score + persist",   ["packages/score/run_scores.py"],                   True),
    # LAST, and after scoring on purpose: it checks what the PAGE will render,
    # so it has to run against the scores this refresh just persisted. Nothing
    # is compiled — app.html is served live out of the DB — so this verifies
    # every route the page calls instead, in-process, no socket needed. A blank
    # tab is indistinguishable from a loading tab, which is why it is a step
    # rather than something you notice by opening the page.
    ("front end",         ["packages/review/build_frontend.py"],             False),
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
# The IndiaMART sweep is here too, and for a plainer reason than OI: it takes
# ~6 minutes of deliberate rate-limit sleeping, and the launcher refreshes on
# every double-click. Without the guard, opening the dashboard three times in a
# morning costs 18 minutes and scrapes an unchanged panel three times. Once a
# day is also the right SAMPLING rate — the signal is a day-on-day change, so a
# second capture on the same date would overwrite the first and compare today
# against today. `--force` is the escape hatch.
SKIP_IF_DONE = {"NSE OI fetch", "open interest", "cement watch (IndiaMART)"}

# Steps SPAWNED DETACHED rather than awaited. The refresh reports "background"
# and moves on; the step writes its own tables when it finishes, minutes later.
# Two consequences, both accepted and both visible rather than silent:
#   - the day-marker is written AT SPAWN, not at success — for an awaited step
#     the marker-on-success rule lets a failed pull retry on the next launch,
#     but an unawaited step has no success to observe, and spawning a second
#     6-minute sweep on every launch until one finishes is the worse failure.
#     A sweep that dies leaves no capture for today, which the Overview banner
#     shows as a stale capture date. `--force` re-spawns.
#   - this run's status.json says "background", never "ok" — the step's own
#     outcome lands in data/refresh/cement_watch_last.log and in the
#     cement_watch table itself, which /api/cement_watch reads live.
BACKGROUND = {"cement watch (IndiaMART)"}
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
    ("cement pack",        "Microsoft 365 MCP is agent-callable only — but "
                           "one connector read, not an Outlook fetch"),
    ("extraction",         "deliberately never automated — a wrong extraction "
                           "enters the store as a fact"),
]



# Tables a SKIP_IF_DONE step feeds, so a skipped step can still report whether
# the source is alive. Keyed by the step label.
_STEP_TABLE = {
    "NSE OI fetch":  ("oi", "date"),
    "cement watch (IndiaMART)": ("cement_watch", "capture_date"),
    "open interest": ("oi", "date"),
}


def _data_age(label: str) -> str | None:
    """How old is the newest row the step is responsible for."""
    spec = _STEP_TABLE.get(label)
    if not spec:
        return None
    table, col = spec
    try:
        import sqlite3
        conn = sqlite3.connect(REPO / "data" / "ims.db")
        last = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]
        conn.close()
        if not last:
            return "table EMPTY"
        import datetime as _dt
        days = (_dt.date.today() - _dt.date.fromisoformat(last)).days
        flag = "  <-- SOURCE MAY BE DEAD" if days > 4 else ""
        return f"{last} ({days}d){flag}"
    except Exception as exc:
        return f"unknown ({type(exc).__name__})"

def consume(what: str) -> int:
    """Load a staging file the agent wrote earlier in the same run.

    SKIP IS NOT FAILURE. A Python-only invocation has no MCP and therefore no
    staging file, and that must exit 0 — otherwise every unattended run reports
    a red step for something it was never able to do. A run that DID stage a
    file and then fails to load it is a real error and exits nonzero.
    """
    import datetime as _dt
    today = _dt.date.today().isoformat()
    stage = REPO / "data" / "staging"
    if what == "metals":
        # .xlsx FIRST, and it is not a cosmetic preference. The .tsv is the M365
        # connector's text conversion, truncated to the ~830 OLDEST rows, so it
        # can only ever carry 2010-2013. The .xlsx is the real workbook and
        # carries today. Before 2026-08-24 this looked only for the .tsv, so
        # dropping a genuine workbook into staging was invisible to the run.
        f = None
        for ext in (".xlsx", ".tsv"):
            cand = stage / f"metals_pack_{today}{ext}"
            if cand.exists():
                f = cand
                break
        # No pack dated today: fall back to the newest staged pack inside a week.
        # It cannot contain today's price, but it carries FULL history, so it
        # still tops up every day the store is behind — which on 2026-08-24 was
        # four trading days. Loud about which file it used; a silent substitution
        # is how a stale number gets read as a fresh one.
        stale_note = ""
        if f is None:
            import datetime as _d
            cands = sorted(stage.glob("metals_pack_*.xlsx")) +                     sorted(stage.glob("metals_pack_*.tsv"))
            recent = []
            for c in cands:
                try:
                    d = _d.date.fromisoformat(c.stem.replace("metals_pack_", ""))
                except ValueError:
                    continue
                if 0 < (_d.date.today() - d).days <= 7:
                    recent.append((d, c))
            if recent:
                d, f = max(recent)
                stale_note = (f"no pack dated {today}; using the {d.isoformat()} "
                              f"pack for its history — it holds NO price for today")
        if f is None:
            print(f"no metals staging for {today} and none within 7 days — "
                  f"skipped. Lower-ranked sources supply what they cover; "
                  f"anything they do not keeps its last stored price.")
            return 0
        if stale_note:
            print(stale_note)
        r = subprocess.run([PY, "packages/adapters/metals_pack.py",
                            "--file", str(f), "--load"], cwd=REPO,
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        return r.returncode
    if what == "cement":
        # THE OPPOSITE PREFERENCE TO metals, and deliberately so. There the .tsv
        # is the connector's truncation and the .xlsx is the real thing; here the
        # cement pack is WIDE (336 lines, ~70k chars) so the connector returns
        # the whole workbook and the .tsv is the normal capture. Both are read
        # by cement_pack.py, so the order only decides which is preferred when
        # somebody has also dropped a workbook by hand — in which case that hand
        # -dropped file should win, hence .xlsx first here too.
        f = None
        for ext in (".xlsx", ".tsv"):
            cand = stage / f"cement_pack_{today}{ext}"
            if cand.exists():
                f = cand
                break
        stale_note = ""
        if f is None:
            import datetime as _d
            cands = (sorted(stage.glob("cement_pack_*.xlsx"))
                     + sorted(stage.glob("cement_pack_*.tsv")))
            recent = []
            for c in cands:
                try:
                    d = _d.date.fromisoformat(c.stem.replace("cement_pack_", ""))
                except ValueError:
                    continue
                if 0 < (_d.date.today() - d).days <= 7:
                    recent.append((d, c))
            if recent:
                d, f = max(recent)
                # Safe to fall back BECAUSE the adapter dates the in-progress
                # month from the FILENAME, not from today. An older capture
                # therefore re-states what that morning knew rather than
                # re-dating a stale month-to-date average to now.
                stale_note = (f"no cement pack dated {today}; using the "
                              f"{d.isoformat()} capture — its month-to-date "
                              f"column stays stamped {d.isoformat()}")
        if f is None:
            print(f"no cement staging for {today} and none within 7 days — "
                  f"skipped. Cement prices keep their last stored month.")
            return 0
        if stale_note:
            print(stale_note)
        r = subprocess.run([PY, "packages/adapters/cement_pack.py",
                            "--file", str(f), "--load"], cwd=REPO,
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        return r.returncode
    if what == "mail":
        f = stage / f"mail_{today}.json"
        if not f.exists():
            print(f"no mail staging for {today} — skipped (agent step did not run)")
            return 0
        r = subprocess.run([PY, "packages/adapters/mail_watch.py"], cwd=REPO,
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        return r.returncode
    print(f"unknown --consume target {what!r}")
    return 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--consume", choices=["metals", "cement", "mail"],
                    help="load a staging file the agent wrote (internal)")
    ap.add_argument("--force", action="store_true",
                    help="re-pull even steps whose data is already there for today")
    a = ap.parse_args()
    if a.consume:
        return consume(a.consume)

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
            # REPORT THE DATA AGE, NOT THE PULL AGE. The marker records that a
            # pull HAPPENED today; it says nothing about whether the pull
            # brought anything new. On 2026-08-24 this printed "already pulled
            # today" while the newest OI row was 5 trading days old, because the
            # vault stopped being updated when that pipeline was retired. A
            # dead source and a healthy one looked identical.
            age = _data_age(label)
            extra = "" if age is None else f" — newest data {age}"
            say(f"  skipped — already pulled today ({day}){extra}; --force to re-pull")
            results.append({"step": label, "status": "skipped",
                            "data_age": age})
            continue
        if label in BACKGROUND:
            # DETACHED_PROCESS: the child must outlive this refresh AND the
            # cmd.exe the .vbs launcher wraps it in. Stdout goes to a log file
            # because a detached process has no console to inherit — writing to
            # a dead handle would kill the sweep with a cryptic OSError.
            logf = OUT / "cement_watch_last.log"
            OUT.mkdir(parents=True, exist_ok=True)
            with open(logf, "w", encoding="utf-8") as lf:
                flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                # -u: unbuffered. Piped-to-file stdout is BLOCK-buffered, so
                # without it a sweep that dies mid-run leaves a 0-byte log —
                # verified 2026-08-28: 38s into a live sweep the log held
                # nothing. The log exists precisely for the dying case.
                subprocess.Popen([PY, "-u", *argv], cwd=REPO, stdout=lf,
                                 stderr=subprocess.STDOUT, creationflags=flags)
            say(f"  started in background (~6.5 min); output -> {logf.name}")
            results.append({"step": label, "status": "background"})
            if label in SKIP_IF_DONE:
                marker[label] = day        # at spawn — see BACKGROUND above
                MARKER.write_text(json.dumps(marker, indent=2), encoding="utf-8")
            continue
        r = subprocess.run([PY, *argv], cwd=REPO, capture_output=True, text=True)
        body = (r.stdout or "").strip()
        if body:
            say("  " + body.replace("\n", "\n  "))

        # A LOCKED DATABASE IS A SCHEDULING CONDITION, NOT A CODE FAILURE, and
        # conflating the two turned the whole run red for a reason that had
        # nothing wrong with it.
        #
        # 2026-08-26: the launcher fired a refresh while a `run_scores --backfill
        # 148` was still writing, and `equity closes` died on
        # "sqlite3.OperationalError: database is locked". The run went FAILED and
        # the dashboard light went red on a store that was completely healthy. The
        # store is WAL with a 5,000 ms busy_timeout — WAL lets readers coexist
        # with ONE writer, but not two writers, and 5s is nothing against a
        # twenty-minute backfill.
        #
        # THE POINT OF THAT LIGHT IS THAT IT IS TRUSTED. A red light that means
        # "two things ran at once" trains you to ignore a red light that means
        # something is broken, which is the same reasoning this file already
        # applies to a stale feed: "a data condition, not a code failure".
        #
        # RETRY ONCE FIRST. Ordinary contention clears in seconds and a retry is
        # cheaper and more honest than a skip. Only a persistent lock is treated
        # as contention.
        if r.returncode != 0 and _is_locked(r.stderr):
            say("  db locked — another writer is active; retrying once in 8s")
            time.sleep(8)
            r = subprocess.run([PY, *argv], cwd=REPO,
                               capture_output=True, text=True)
            body = (r.stdout or "").strip()
            if body:
                say("  " + body.replace("\n", "\n  "))

        if r.returncode != 0 and _is_locked(r.stderr):
            # SKIPPED, not failed. The page renders anything that is neither `ok`
            # nor `skipped` as red, so the status string is what keeps the light
            # honest; `detail` is what the chip shows on hover.
            #
            # NOT marked in the done-marker, because that is only written on
            # SUCCESS — so this step is retried on the next launch rather than
            # being skipped for the rest of the day.
            say("  ! still locked — SKIPPED, not failed. Nothing is broken; this "
                "step did not run and will retry next launch.")
            results.append({
                "step": label, "status": "skipped", "reason": "db_locked",
                "detail": "skipped: database locked by a concurrent writer "
                          "(e.g. a backfill). Not a failure; retries next launch.",
                "error": (r.stderr or "").strip()[:2000]})
            if fatal:
                # A FATAL step that never ran is still fatal. If score+persist
                # could not write, everything downstream is stale and calling the
                # run OK would be exactly the silent failure this file exists to
                # prevent. Halt — but say it is contention, not breakage.
                ok = False
                say(f"\nHALT — {label} is a Tier 0 gate and it did not run "
                    f"(database contended, not broken). Re-run once the other "
                    f"writer finishes.")
                break
            # Non-fatal: the run stays green. That is only safe because the
            # freshness check below independently verifies the DATA is current —
            # so a skipped load whose feed has genuinely gone stale still turns
            # the light amber. The two checks answer different questions.
            continue

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
