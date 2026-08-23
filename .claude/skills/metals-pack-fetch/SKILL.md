---
name: metals-pack-fetch
description: Pull today's Daily Metals Pack out of the Kotak basic-materials email and load its prices. Replaces the manual save-the-attachment step. Use for the daily price refresh, when commodity prices look stale, or when asked to update the metals pack.
---

# metals-pack-fetch — the pack, without saving an attachment

The Daily Metals Pack is the best price data in this system — LME cash, assessed
alumina, LME zinc and pet coke, none of which Yahoo supplies correctly — and it
arrives as an email attachment. It used to require a human saving a file. It does
not any more.

This is **step A** of a two-step split, forced by the same constraint as Wind and
mail-fetch: **the Microsoft 365 MCP is callable by the agent, never by a Python
process.**

## 1. Find today's mail

```
outlook_email_search
  sender:        sumangal.nevatia@kotak.com
  query:         Basic materials
  afterDateTime: yesterday
  limit:         5
```

Subject is **"Basic materials - daily news and prices"**, sent daily around
**03:30 UTC** (09:00 IST). `hasAttachments` must be true.

Weekly sector wraps from the same sender also match `sender` alone — that is why
`query: Basic materials` is there. If the only hit is a "Weekly wrap", today's
pack has not arrived; say so and stop rather than loading a stale file.

## 2. Read the message to get the attachment URI

```
read_resource  uri: mail:///messages/<messageId>
```

The response carries an `attachments` array with **two** xlsx files:

```
Daily Metals Pack, <Month DD, YYYY>.xlsx     ~1.9 MB   <- this one
Daily Cement Pack, <Month DD , YYYY>.xlsx    ~0.6 MB   <- ignore until cement is modelled
```

Match on the **name**, not on position or size. Note the cement file has a stray
space before the comma in its name; do not let a loose pattern catch it.

## 3. Read the attachment

```
read_resource  uri: <the metals pack attachment uri>
```

It comes back as **tab-separated text, not bytes** — the MCP converts it. The
first line is `=== Sheet: Daily prices ===`, then the header, then the grid.

**Expect a truncation notice.** The read caps at ~200k characters, roughly 830 of
the pack's ~4,760 rows, and it keeps the **oldest** rows. When the result is
saved to a file, read the **tail** of that file — that is where today's prices
are. Do not re-read the resource hoping for more.

## 4. Write it to staging

```
data/staging/metals_pack_YYYY-MM-DD.tsv
```

Version-controlled on purpose, like the Wind capture: it is the dated record of
what the broker actually sent that morning, which is what lets a price be audited
later without trusting anyone's memory.

## 5. Load

```bash
cd C:\Users\rajvaibhav.yadav\Investment-micro-system
python packages/adapters/metals_pack.py --file data/staging/metals_pack_<date>.tsv --probe
python packages/adapters/metals_pack.py --file data/staging/metals_pack_<date>.tsv --load
```

`--probe` first, every time. It prints rows and span per series. **Check the span
ends today** — if it ends three years ago you have the truncated head and not the
tail, which is the one way this can silently load nothing useful.

`cp_coke` showing `0 rows` on a truncated read is normal: that series only starts
in 2018.

Prices are `INSERT OR REPLACE` on `(entity_id, date)`, so re-loading the same day
is harmless and a partial file tops up rather than overwriting history.

## 6. Confirm it landed

```bash
python packages/core/freshness.py            # or:
python -c "import sqlite3;c=sqlite3.connect('data/ims.db');print(c.execute(
  \"select entity_id,max(date) from prices where entity_id in
   ('lme_aluminium','lme_zinc','alumina_index') group by entity_id\").fetchall())"
```

All three should read today (or the last trading day). If they do not, the load
did not take and **saying nothing is the failure mode** — the scores will
recompute happily on yesterday's prices and look entirely normal.

## Cost

One search plus two reads. The attachment read is the expensive part at roughly
50k tokens. Do not read both attachments; the cement pack doubles the cost and
nothing consumes it yet.

## Full-history rebuild

**This skill cannot do it.** Truncation keeps the oldest ~830 rows, so no single
extraction covers 2010-2026. For a rebuild you need the real `.xlsx` saved to
disk and `--file <path>.xlsx`, which uses the openpyxl reader instead. See
`docs/REPLICATE.md` §4.3.
