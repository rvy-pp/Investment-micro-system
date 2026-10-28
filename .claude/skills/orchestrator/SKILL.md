---
name: orchestrator
description: "The day-end watch over every book leg and its case agent: load today's closes, re-mark every case's juice, flag own moves / juice moves / revisions / prints due, search mail daily for new house targets and send the leg's agent to re-strike its anchor, and on the rota's sector day (Mon cement, Tue IT, Wed steel, Thu metals & mining, Fri EMS) sweep that sector's mail and news, hand it to the case agents, run them, and report at market end. Use for the case-orchestrator scheduled task, or when asked to run the orchestrator, the day-end check, the sector review, or to have the agents check in."
---

# Orchestrator — the day-end watch over the case agents

PM, 2026-10-26: *"an orchestrator agent that monitors all company-agents. It
will update all prices on a daily basis, check each sector periodically for
updates (say IT on tuesday, steel on wednesday etc; look at mails, news, give
feedback at market end)."*

**You are the orchestrator, running in the main session.** A subagent cannot
start another subagent, so this is a skill the `case-orchestrator` scheduled
task runs (weekdays after the close), not one more file in `.claude/agents`.
You dispatch the case agents (`tcs-agent`, `utcem-agent`, …) with the Agent
tool. The usual split holds: `packages/book/orchestrator.py` does the
arithmetic and the flags; you do the reading.

**Only stocks with a case agent are watched** (PM, 2026-10-26: *"do not
create anything for stocks with no agents build yet"*). A leg with no
`.claude/agents/<token>-agent.md` gets no sweep, no flag, no material file
and no summary. The script filters on the agent file, so a leg joins the
watch the day its agent is created. Never scaffold one unless the PM asks.

The rota and every threshold live in `specs/book.yaml orchestrator:`. Every
dictated group must sit on exactly one weekday. The script refuses to run if
one does not, because a group missing from the rota would never be reviewed.

## Every weekday

**Never on a weekend** (PM, 2026-10-27: *"do not run it on weekends, at least
the prices part"*). The task is Mon–Fri, but a missed Friday run fires on the
next app launch, which can be a Saturday. `--prices` and `--day` refuse a
Saturday or Sunday and write nothing. In that case the whole run is that one
line: no mail, no watches, no targets, no agents, no report. Do not pass
`--force` unless the PM asks.

1. **Prices.** `python packages/book/orchestrator.py --prices` (~40s). If the
   day-end header then says `NO SESSION TODAY`, it is an NSE holiday or
   Yahoo has not posted the close yet. Wait 20 minutes and run it once more
   before believing either.
2. **The sweep.** `python packages/book/orchestrator.py --day`. It prints every
   watched leg's 1-day stock / sector / own split, each case's bear / base / bull juice
   on its headline year at today's price, the PM's lean where one is set, and
   the FLAGS. It writes `data/orchestrator/day_<date>.json`, which is what
   tomorrow's juice and revision flags compare against.
3. **Own-move flags → pair-check, never a re-mark.** For each pair a flag
   names, run `python packages/book/move_check.py <PAIR>` and read the result
   per the pair-check skill. Off a sector day, do blind spot 7 (one
   company-specific mail search plus one news search on the moving leg).
   Report blind spot 6 (NSE block/bulk deals) as "not checked: day-end
   sweep" unless the move is the largest in the book, when you do it.
   - *stock-specific, with information*: that leg's agent runs today, whatever
     the rota. Write the item to its material file (below) and dispatch it.
     **Re-mark the target only on information, never on price.**
   - Any other reading goes in the report as a reading. **Never call a move
     noise.**
4. **Other flags.** `prints …` means a result is near. Make sure the leg's
   agent ran its pre-print checks: CROSS-CHECK DUE for IT deals, and a
   `street_q` pin. If it has not, name that in the report. When a covered
   name printed since the last day-end, its agent runs today whatever the
   rota, with the concall note if the vault has one. `BUILD REFUSED`: name
   it; the leg's agent fixes it on its next run. `base juice … ->` and
   `consensus … %`: report them. A juice move from price alone, against an
   anchored target, is arithmetic, not news. Say which kind it was.

4b. **Watches** (the `WATCH` blocks at the end of `--day`; thresholds in
   `specs/book.yaml orchestrator.watches`). A watch is a named risk to one
   pair, run every weekday until its `until` date. The first one is
   UTCEM_SRCM (PM, 2026-10-27: *"It loses if the market decides Shree deserves
   UltraTech's multiple, or if petcoke normalises quickly. Watch for both
   through Q3: the November dealer price checks, Shree's trade share, and
   petcoke. On a daily basis."*). The script computes three things:
   - **the multiple gap:** the short's FY28 EV/EBITDA over the long's, at today's closes;
   - **petcoke:** the level and its 4-week change;
   - **the Kotak pack's monthly price:** weighted to each leg's regions, against each build's price path. It prints the months still awaited, with the build's Rs/bag.

   You do the rest:
   - **Mail, daily, narrow.** Run `outlook_email_search` with each `mail_terms`
     entry as `query`, `afterDateTime` = the previous day-end's date. The PM
     asked for this search, so it is sanctioned, but only these terms.
     Read the body of any dealer or channel check that gives numbers (cap: 6
     bodies). Note the month, the region, the Rs/bag change, trade vs
     non-trade, and the house. Set each against the build path the WATCH
     block prints, for example *"Nuvama: November North flat, East −Rs5 → Shree mix ≈
     Rs369 vs build Rs371.9"*. Report any Shree trade-share number with its
     source. Only the Q3 call gives the real figure.
   - **An ALERT** (gap at or past par, petcoke at or below the alert level):
     run `move_check` on the pair and lead the report with it. A WARN or a
     NEW pack month gets one sentence each.
   - **A quiet day is stated:** *"watch quiet: gap −10.9%, petcoke $189,
     no new pack month, N mails searched, no dealer checks"*.
   - **Never re-mark a build from a watch.** A dealer check that undercuts a
     build's path is material for that leg's agent on its next run (write it to
     the material file if the agent runs today). The PM decides what changes.
   - **An expired watch** gets one line asking the PM to extend or drop it.

4c. **Targets, daily** (PM, 2026-10-27: *"Add a daily narrow mail search for
   rating/TP changes on every watched leg to the orchestrator; a new TP goes
   to that leg's material file and its agent re-strikes the anchor."*). Every
   case anchors on the houses' targets, so one house re-striking moves the base
   juice. The `TARGETS` block of `--day` gives, per leg, the search terms, the
   window (`since` = the previous day-end) and the targets the anchor already
   **holds**. A leg marked `sector day` is skipped here, because step 6's sweep
   covers it, but its TP mails are judged the same way.
   - **Mail, narrow.** For each leg, run `outlook_email_search` with each
     search term as `query`, `afterDateTime` = `since`, and page it. Keep a hit
     only if its subject or preview carries a rating or target word: TP, PT,
     FV, PO, target, price objective, fair value, upgrade, downgrade, initiate,
     rating, or a rating word (BUY / ADD / HOLD / REDUCE / SELL / OW / UW /
     Neutral). Read the body of each one you keep (cap: 3 per leg, 15 a day,
     and say so if the cap binds). Check it is the right company (the traps in
     step 6), and run a word search, never this sweep, for anything else.
   - **New or not.** A target is NEW when the `held` list lacks that house at
     that price. A sales note re-quoting a held target is not new, and neither is
     a pre-result target (the anchors are post-result). The usual case is a house
     whose TP changed, or a house not held at all. Note the house, the rating
     (and its change), the new TP, the prior TP, the note's date, the basis and
     the quote.
   - **A new target → the material file → the agent, today, off-rota.** Write
     it to `data/orchestrator/material/<date>/<entity>.md` (append if the file
     exists) and dispatch that leg's agent:
     > Target re-strike, orchestrator, <date>. New house target(s) since
     > <since> are in `<material file>`. Read them as material the PM handed
     > you; do not sweep Outlook yourself. Add each to your target table and
     > re-strike the anchor per the stock-case skill's "Re-striking" rule,
     > re-run the brief, and log the new range. Never touch the lean. End with:
     > the old and new anchor, bear / base / bull juice before and after, and
     > where the PM's lean now sits against the new range.
     Several legs: dispatch them together. A rating change with an unchanged
     TP is reported, never dispatched.
   - **The next day-end flags it.** `--day` compares each leg's held targets
     with the previous day-end and prints `targets RE-STRUCK: House a -> b;
     anchor x -> y`, so a re-strike is on the record even if today's report is
     never read.
   - **A quiet day is stated in one clause:** *"targets: N mails across 8 legs,
     no new TP"*.
   - **The PM's lean never moves here.** If a re-strike puts the lean outside
     the new bear–bull range, say so, with both numbers, in the report's
     questions. Only the PM re-sets a lean.

## On the sector day

5. **The pack.** `python packages/book/orchestrator.py --sector today`. (Run
   no sector step on a weekend.) **If it says no leg has a case agent, the
   sector day ends there: skip steps 6–11 and do not `--mark`.**
   Then refresh the σ ranges' multiple history for the day's legs:
   `python packages/book/multiple_sigma.py --fetch <entity> ...` (screener
   throttles after ~7 requests; the script retries). A history older than 35
   days withholds that leg's bear/bull, and a weekly sector day keeps every
   leg inside that. Per watched
   leg it gives the window (since that
   group's last review, or 7 days), the move, the consensus change, stored
   mail/brief mentions, the agent if one exists, and the search terms. It
   writes `data/orchestrator/sector_<date>_<slug>.json`.
6. **Mail.** For each leg, `outlook_email_search` with each search term as
   `query`, `afterDateTime` = the window start, newest first, paged via
   `nextOffset`. The PM asked for this sweep, so it is sanctioned. Keep it to
   the sector's names; it is read-only (no labels, no replies). Read the body
   (`read_resource(uri)`) of every mail that carries a number, a rating or TP
   change, guidance, a channel check or management commentary. Cap it at 8
   bodies per leg and 30 per sector, and say so if the cap bound.
   **Check every hit is the right company.** Known traps (CLAUDE.md):
   - "Ambuja" can be GUJARAT AMBUJA EXPORTS, and "Dalmia" can be DALMIA BHARAT
     SUGAR.
   - Bare "HCL" is HCL Tech, never Hindustan Copper.
   - "NMDC Steel" is not NMDC.
   - "LTM TCV" / "LTM revenue" is last-twelve-months, not LTIMindtree.
   - "#JKCement" bullets can be about JK Lakshmi.
7. **News.** One news search per leg over the window (the `search_news` tool if
   it is connected, else `WebSearch`). Keep dated, on-entity items only.
8. **Material files.** Write `data/orchestrator/material/<date>/<entity>.md`
   per leg. Each item gets its date, its source (sender and subject, or outlet
   and URL) and what it says, numbers first, in two or three lines. Judge any
   house target in the sweep against the `TARGETS` held list exactly as in
   step 4c. A new one goes in under a heading **New targets: re-strike the
   anchor**, so the agent re-strikes on this run. A quiet
   leg's file says *"nothing new since <window start>: N mails searched, M
   news results, none on-entity"*. That is a stated quiet, not an omission.
9. **Dispatch the agents**, all of the sector's at once (one message, several
   Agent calls, `subagent_type` = the agent name). The prompt:
   > Sector-day run, orchestrator, <date>. New material since <window start>
   > is in `<material file>`. Read it as material the PM handed you; do not
   > sweep Outlook yourself. Do your normal run per the stock-case skill.
   > If nothing in it moves your build, say so and change nothing. End with
   > your usual short report: what changed, bear / base / bull juice, and the
   > questions for the PM.

   Agents write their own case files and Analysis-log entries under their
   own rules. You write neither.
10. **Pairs.** `python packages/book/move_check.py <PAIR>` for every pair the
    pack lists (a pair with at least one watched leg). One line each in the report: the 7-day and 30-day reading.
11. **Mark it done.** `python packages/book/orchestrator.py --mark "<groups>"`
    (the groups the pack printed). The next review's window starts here.

## Close

12. If any agent wrote an Analysis-log entry today, the vault copy owes an
    export: `python packages/web/export_static.py` (~90s).
13. **The report**, `data/orchestrator/reports/<date>.md`, in continuous prose
    (the PM's standing preference: paragraphs, never one sentence per line):
    - **Day-end:** closes to which date, the flags and what each pair-check
      read, juice and consensus moves.
    - **Targets:** every new house target found today (house, old → new TP,
      rating), what each re-strike did to the anchor and the bear / base / bull
      range, and where the PM's lean sits against it; or the one-clause quiet.
    - **Watches:** each watch's numbers and flags, the dealer checks found in
      mail set against the build paths, or the stated quiet. An ALERT leads the
      whole report.
    - **Sector review** (on a sector day): pair by pair, what was new for each
      leg, what each agent changed (quote its juice range). The pack's count
      of unwatched legs goes in as one clause, never a list or a summary.
    - **Questions for the PM:** the agents' questions, gathered and
      de-duplicated.

    Your final message is that report, shortened to what the PM must read.

## What the orchestrator never does

- Sets or proposes a lean, writes a `juice_pct`, a Kelly edge or a call.
- Re-marks a target on a price move.
- Calls a move noise.
- Writes `prices` by any route but `--prices`, or writes `estimates`, a
  pillar table, or another stock's case.
- Sweeps mail beyond the day's sector names, the flagged legs, the
  watches' `mail_terms` and the daily target search (step 4c). Sends, replies to or labels mail.
- Commits or pushes. Code changes are not its job.
