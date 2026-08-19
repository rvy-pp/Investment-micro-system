---
name: mail-fetch
description: Fetch the last day of broker mail for every covered company via the Microsoft 365 MCP and write it to data/staging/ for mail_watch.py to filter. Use for the daily structural-event watch, or when asked to check mail for catalysts. Transport only — it reads, strips and writes, and interprets nothing.
---

# mail-fetch — step A of the daily mail watch

This is a **transport step**. It exists only because the Microsoft 365 MCP is
callable by the agent and never by a Python process — the same constraint that
applies to the Wind MCP. Step B (`packages/adapters/mail_watch.py`) does all the
filtering and is pure Python.

**Do not read, summarise, judge or interpret anything.** Every hit you find would
be a hit `mail_watch.py` finds a second later, deterministically and for free.
Interpreting here makes the output non-reproducible and burns tokens for nothing.

## 1. Get the search plan from the specs

```bash
cd "C:\Users\rajvaibhav.yadav\Investment-micro-system"
python packages/adapters/mail_watch.py --terms
```

It prints one line per covered entity with its search terms. **Do not hardcode
the list** — it is derived from `specs/entities/*.yaml`, so a newly modelled
company starts being watched automatically. If a name is missing from the plan,
the fix belongs in the spec, not here.

## 2. One search per entity

For each entity, call `outlook_email_search` with:

- `query` — the entity's terms joined with ` OR `, each multi-word term quoted
  (e.g. `NALCO OR NATIONALUM OR "National Aluminium Company"`)
- `afterDateTime` — `"yesterday"` for the daily run; a date for a backfill
- `limit` — `25`

Search by **name, never by event keyword**. A broad `commissioning OR ramp OR
smelter` query returned 10 results of which 2 touched covered names — the rest
were steel, textiles and healthcare — and it still reported `moreResults`.
Keyword matching is step B's job and it is free there.

If a response ends with `nextOffset`, page until exhausted or you have 50 records
for that entity. More than that in a day means the query is too loose; report it
rather than silently truncating.

## 3. Strip before writing

Keep exactly these fields per record:

```
subject, sender, receivedDateTime, summary, hasAttachments
```

**Drop `uri`, `webLink`, `internetMessageId`, `id`.** They are roughly 40% of the
payload, they are never used by step B, and leaving them in means they re-enter
context on every future read of the file.

Deduplicate on `subject` + `receivedDateTime` — the same note often matches
several entities, and step B tags entities itself.

## 4. Write the staging file

```
data/staging/mail_YYYY-MM-DD.json
```

A JSON array of the stripped records. Use today's date in the local timezone.

Write it **even when every search returns nothing** — an empty array is the
record that the watch ran and the day was quiet. A missing file means the watch
did not run, and `mail_watch.py` reports that loudly and exits non-zero. Those
two states must stay distinguishable.

These files are **version-controlled on purpose**. They are the dated record of
what the mailbox returned that morning, which is what lets a catalyst be graded
later without trusting anyone's memory of what was knowable when.

## 5. Hand off

```bash
python packages/adapters/mail_watch.py
```

Report its output as-is. If it flags a hit, the next step is a human reading it
and deciding whether a spec parameter moved — then `packages/score/whatif.py`
quantifies it. Neither is your job here.

## Cost

Five searches, metadata only, ~8-12k tokens. If a run costs materially more,
something is paging further than it should — check for a term matching too
broadly (a bare company name that is also a common word) before accepting it.

## What this never does

- Never sends, replies to, forwards or flags mail. It reads.
- Never writes to `data/ims.db`. Staging only.
- Never decides that something is a catalyst.
