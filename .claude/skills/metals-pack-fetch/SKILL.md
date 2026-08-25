---
name: metals-pack-fetch
description: Pull the latest Daily Metals Pack out of Outlook as a real .xlsx and load all 36 commodity columns as the primary price source. Use for the daily price refresh, when commodity prices look stale, or when asked to update the metals pack.
---

# metals-pack-fetch — the real workbook, out of Outlook

The Daily Metals Pack is the best price data in this system and now the
**primary** source for every commodity it carries: LME cash, assessed alumina,
pet coke, coking coal, and the whole steel complex. It arrives as an email
attachment each morning, and as of **2026-08-24** nothing here is agent-only.

```bash
cd C:\Users\rajvaibhav.yadav\Investment-micro-system
python packages/adapters/outlook_pack.py --save     # find + save the .xlsx
python packages/refresh.py --consume metals         # probe-free load
```

Both run inside `python packages/refresh.py`. **You do not normally invoke this
skill at all** — it is documentation for the two steps the refresh already does.

---

## Why this stopped being an MCP step

The Microsoft 365 connector is a **text service**. It hands back an attachment
converted to text, capped at ~200k characters, keeping the **OLDEST** rows. On a
4,760-row workbook that is 2010 to about 2013, at any hour, awake or asleep. The
old version of this file told you to read the *tail* of what came back because
today's prices are there — they are at the tail of the **file**, not the tail of
the **extract**, and the difference is thirteen years. `startPage` does not
rescue it: the tool schema documents it for `file:///` reads only and says
outright it is ignored for every other URI scheme.

Outlook has the actual bytes. Classic Outlook 16 with a live MAPI profile is
installed, so `outlook_pack.py` shells to PowerShell, saves the attachment, and
hands the path to the openpyxl reader that was always in `metals_pack.py` for a
hand-dropped file. Measured 2026-08-24: **1,934,986 bytes, 4,728 rows, span
ending the same day, ~0 tokens** because no model ever sees the grid.

It needs the machine **awake and logged in** — locked is fine, asleep is not. A
scheduled run could not have fired through sleep either, so this costs nothing
that was previously working.

## Two constants the old skill got wrong, both silently

**Sender.** It searched `sumangal.nevatia@kotak.com`. The pack comes from
**`Samriddhi.Choudhury@kotak.com`**. Sumangal Nevatia is a real Kotak metals
analyst who sends other notes, which is presumably how the address got in. A
search on the wrong address returns zero hits, and the documented response to
zero hits was *"say the pack has not arrived and stop"* — so a wrong constant
read as a quiet morning. **`outlook_pack.py` matches on no sender at all.**

**Filename.** It said the cement pack is distinguished by a stray space before
the comma. Not stable — on 2026-08-18 **both** files had it (`August 18 , 2026`);
on 2026-08-24 metals had `August 24, 2026` and cement had `August 24 , 2026`.
Match the prefix `Daily Metals Pack`, which is what the code does.

## The last row is dated today and does NOT hold today's prices

Read this before quoting a number off the bottom of the file.

The mail leaves around **08:39 IST**. LME cash settles at London midday, roughly
16:30 IST. So the row dated today exists but is **unfilled**, carrying the
previous session's values until tomorrow's file backfills it. Verified against
westmetall's own cash settlements date by date: the pack's date column is the
**trade date**, and 775 of 842 historical Mondays differ from the preceding
Friday precisely because by the time you see a Monday row in a *later* pack it
has been filled in.

This needs no handling. Prices are `INSERT OR REPLACE` on `(entity_id, date)`,
so tomorrow's pack overwrites today's placeholder with the real print. What it
does mean: **never read the last row as a live quote.** `freshness.py` separates
`row_age` from `value_age` for exactly this, and a carried-forward row shows up
as value_age > row_age.

## What gets loaded

All **36** price columns, up from nine. Columns 37–43 are date labels, not
prices, and are not mapped. Nine feed the aluminium and zinc bridges; the other
27 are **parked** — captured so the history exists for the steel group, but
`price_link`-ed from no spec, so nothing reads them and nothing can be scored
wrong by them. `metals_pack.PARKED` is the list; move a series out of it the
moment a spec links it.

Capturing them now is not tidiness. The connector route can never backfill, so a
column you do not capture today is history you cannot recover.

**Four naming collisions were available here and all four are avoided**, per
invariant 6 — a proxy is never aliased to the thing it proxies:

| pack column | gets | because `…` already means |
|---|---|---|
| 21 SHFE aluminium CNY | `aluminium_shfe_cny` | `lme_aluminium` is LME cash |
| 22 SHFE zinc CNY | `zinc_shfe_cny` | `zinc_shfe` is Wind ZN.SHF, USD/t **ex-VAT** |
| 30/31 SHFE alumina | `alumina_shfe_cny/_usd` | `alumina_index` is Australia FOB assessed |
| 5/27/17/28 iron ore | four distinct ids | `iron_ore` is FRED PIORECRUSDM, **monthly** |

Columns 5 and 27 are both "China import iron ore fines 62%" and are **not the
same number** — 100.00 against 91.79 on 2026-08-24. Two ids, headers kept
verbatim in the notes so the basis stays auditable.

**Three columns the broker stopped maintaining.** Turkey scrap ends 2020-12-03,
the quarterly coking coal contract 2022-06-27, SHFE zinc CNY 2021-04-01. They
load as history and `freshness.py` marks them `DISCONTINUED` rather than STALE —
a broker's editorial decision is not a pipeline fault. Column 10
(`coking_coal_spot_aus`) is the **only live coking coal number**, and the only
coking coal source this system has at all: FRED `PCOKEUSDM` is a dead 404 and
four replacements were probed without success.

## Confirm it landed

```bash
python packages/core/freshness.py
```

Every pack series should read `2026-08-24` (or the last trading day) with row age
0. If they do not, the load did not take, and **saying nothing is the failure
mode** — the scores recompute happily on yesterday's prices and look entirely
normal.

## Full-history rebuild

This route **does** rebuild history, which the connector route never could. One
`--save` and one `--load` writes all 4,728 rows per series back to 2010. That is
why `docs/REPLICATE.md` §4.3's "a person saves the workbook" step is no longer
required.
