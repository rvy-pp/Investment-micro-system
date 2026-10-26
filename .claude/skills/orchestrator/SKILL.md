---
name: orchestrator
description: "The day-end watch over every book leg and its case agent: load today's closes, re-mark every case's juice, flag own moves / juice moves / revisions / prints due, and on the rota's sector day (Mon cement, Tue IT, Wed steel, Thu metals & mining, Fri EMS) sweep that sector's mail and news, hand it to the case agents, run them, and report at market end. Use for the case-orchestrator scheduled task, or when asked to run the orchestrator, the day-end check, the sector review, or to have the agents check in."
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

The rota and every threshold live in `specs/book.yaml orchestrator:`. Every
dictated group must sit on exactly one weekday. The script refuses to run if
one does not, because a group missing from the rota would never be reviewed.

## Every weekday

1. **Prices.** `python packages/book/orchestrator.py --prices` (~40s). If the
   day-end header then says `NO SESSION TODAY`, it is an NSE holiday or
   Yahoo has not posted the close yet. Wait 20 minutes and run it once more
   before believing either.
2. **The sweep.** `python packages/book/orchestrator.py --day`. It prints every
   leg's 1-day stock / sector / own split, each case's bear / base / bull juice
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

## On the sector day

5. **The pack.** `python packages/book/orchestrator.py --sector today`. (Run
   no sector step on a weekend.) Per leg it gives the window (since that
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
   and URL) and what it says, numbers first, in two or three lines. A quiet
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
   own rules. You write neither. **A leg with no agent gets no case work:**
   summarize its material yourself in the report. Never scaffold a new agent
   unless the PM asks for it. List the legs without one.
10. **Pairs.** `python packages/book/move_check.py <PAIR>` for every pair in the
    sector. One line each in the report: the 7-day and 30-day reading.
11. **Mark it done.** `python packages/book/orchestrator.py --mark "<groups>"`
    (the groups the pack printed). The next review's window starts here.

## Close

12. If any agent wrote an Analysis-log entry today, the vault copy owes an
    export: `python packages/web/export_static.py` (~90s).
13. **The report**, `data/orchestrator/reports/<date>.md`, in continuous prose
    (the PM's standing preference: paragraphs, never one sentence per line):
    - **Day-end:** closes to which date, the flags and what each pair-check
      read, juice and consensus moves.
    - **Sector review** (on a sector day): pair by pair, what was new for each
      leg, what each agent changed (quote its juice range), and the legs that
      have no agent.
    - **Questions for the PM:** the agents' questions, gathered and
      de-duplicated.

    Your final message is that report, shortened to what the PM must read.

## What the orchestrator never does

- Sets or proposes a lean, writes a `juice_pct`, a Kelly edge or a call.
- Re-marks a target on a price move.
- Calls a move noise.
- Writes `prices` by any route but `--prices`, or writes `estimates`, a
  pillar table, or another stock's case.
- Sweeps mail beyond the day's sector names and flagged legs. Sends, replies
  to or labels mail.
- Commits or pushes. Code changes are not its job.
