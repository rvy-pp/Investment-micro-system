---
name: pair-check
description: Seven-check report on WHY a book pair's legs moved — sector vs own move, consensus revisions, mail, OI positioning, read-across, plus the two blind spots done by hand (NSE block/bulk deals; conference / analyst-meet commentary). Run it, and tell the PM the result, whenever the PM asks about a dictated pair — its move, P&L, juice, sizing, add / trim / exit, or "why did X move" — and for any pair discussed in a daily review.
---

# Pair check — noise, or information we are missing?

PM, 2026-10-22: *"Add these 5 + 2 blind spot checks. If I ever [ask about] a
pair, this should be checked and told."* The question behind it: when a leg
moves against the target, is it **noise** (add toward the target),
**information we could have seen** (re-mark the target first), or **a
repricing we cannot see** (in a pair, the sector part largely cancels)?

The arithmetic is `packages/book/move_check.py` (selftest 19). It reports and
never decides. The two blind spots are not in the store and are done here, by
hand, every time.

## When

Every time the PM asks about a dictated pair (specs/book.yaml `pairs`): its
move, P&L, juice, sizing, add / trim / exit, or why a leg moved. In a daily
review, run it for every pair discussed. **Do it before answering, and put the
result in the answer**: the PM asked for the checks to be "checked and told",
not offered.

## Step 1 — the five data checks (one command)

```bash
python packages/book/move_check.py <PAIR>              # 7 and 30 calendar days
python packages/book/move_check.py <PAIR> --days 5     # the period the PM names
```

| # | check | reads | fires when |
|---|---|---|---|
| 1 | sector | `sector_rel.split` (the Book tab's definition) | own move > 1.5pp |
| 2 | consensus | Yahoo FY+1/FY+2 EPS, start vs end | either year moved >= 1% |
| 3 | mail | morning-brief bullets, staged mail, house estimates, analyst views | anything names the stock |
| 4 | positioning | `oi`: OI change vs price (long/short build-up, covering, unwinding) | OI moved >= 5% |
| 5 | read-across | ACN/CTSH (IT), the sector's top movers, peer result bullets | bellwether >= 3%, peer >= 5%, or a peer print |

Thresholds are a first cut, printed in the header. Read the numbers, not just
the arrows.

**Read the alignment line.** If it says "both legs aligned to <date>", the
pair was measured to the last close both legs AND a quorum of their sector
share. That happens when a leg has a Yahoo hole (LTIMindtree, 21-Oct-2026) or
only the pair's own bars were loaded intraday. Say the date out loud; never
present a stale window as today's.

## Step 2 — blind spot 6: block / bulk deals

Not in the store. For each leg, over the window:

- In the built-in browser, open nseindia.com → Market Data → **Bulk Deals**
  and **Block Deals**, filter the symbol (`move_check` prints it) and the date
  range. The day's files are also at `archives.nseindia.com/content/equities/
  bulk.csv` and `block.csv` (today only).
- Report each deal: date, buyer/seller, quantity, price, and quantity as a
  share of the day's volume where visible.
- If NSE refuses automated access or demands a sign-in, say **"not checked:
  <reason>"**. Never report "no deals" for a check that did not run.

## Step 3 — blind spot 7: conferences, analyst meets, management commentary

Not in the store. For each leg, over the window:

- Mail via the M365 connector (`outlook_email_search`): the company name or
  ticker with *conference, meet, NDR, fireside, takeaways, analyst day, AGM,
  investor day*. The PM asked for this check, so this search is sanctioned.
  Do not widen it into a general sweep.
- News: one `WebSearch` on the company over the window.
- Report dated items with their source. A comment that names guidance, deal
  flow or margins counts as information; a generic "positive tone" does not.

## Step 4 — tell the PM

Per leg, a seven-row table (check | finding | fired?), then the reading:

- **moved with its sector**: the pair hedge did its job; nothing to re-mark.
- **stock-specific, with information**: name the item. Say whether it changes
  the EPS view or the anchor, and so the target. That is a re-mark, not a
  price move.
- **stock-specific, positioning only**: flows. Say which (short covering, long
  unwinding).
- **stock-specific and unexplained so far**: only after all seven came back
  empty, and still **never call it noise**. Absence of evidence is not
  evidence: in the Avalon dossier 7 of 18 big moves had no reason in the
  archive. Persistence is itself a signal.

Then the consequence, under the Kelly rule agreed on 2026-10-22: size ∝
(target ÷ price − 1), floored at zero, capped per pair. **Re-mark the target
only on information, never on price.** A noise move changes the size through
the price. An information move changes the target first. Leans and edges stay
the PM's: this skill writes nothing.

## What this never does

- Writes `book_analysis`, a lean, a Kelly edge, a call, or anything in the store.
- Calls a move noise, or skips a blind spot without saying it was skipped.
- Sweeps Outlook beyond the company-specific search in Step 3.
