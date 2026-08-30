---
name: morning-brief
description: Build the pre-market morning brief for the Daily Overview tab — run morning_markets.py, sweep the last 24h of ALL mail via the Microsoft 365 MCP, summarize actionables per coverage sector with sources, and write data/morning/brief_YYYY-MM-DD.json. Use every morning pre-market, or when asked for the morning brief / overnight summary / what came in the mail.
---

# morning-brief — the agent half of the Daily Overview's morning block

Two files feed the Overview tab's "Morning brief" section (`/api/morning`):

| file | written by | contains |
|---|---|---|
| `data/morning/markets_YYYY-MM-DD.json` | `morning_markets.py` (python) | US/semis/IT closes, GIFT Nifty + gap, entity-keyed headlines |
| `data/morning/brief_YYYY-MM-DD.json` | **this skill** | sector mail actionables, AI/semis bullets, bellwether reasons |

This skill exists because the mailbox is behind the Microsoft 365 MCP —
callable by the agent, never by a Python process (the mail-fetch / Wind
constraint). Unlike mail-fetch, this skill DOES interpret: the brief is
display prose for the PM, it feeds no score, touches no DB, and cites its
source on every bullet so the claim is checkable in Outlook.

## 1. Markets first (python, ~10s)

```bash
cd "C:\Users\rajvaibhav.yadav\Investment-micro-system"
python packages/adapters/morning_markets.py
```

If it prints ERROR lines, carry on — errors ride inside the JSON and the tab
renders them. Do not re-fetch any of its numbers by hand; the file is the
record.

## 2. Sweep ALL mail, last 24 hours

`outlook_email_search` with **no `query`** returns everything in the window —
verified 2026-08-30 (the schema's "Omit for all emails" is real):

- `afterDateTime: "24 hours ago"`, `order: "newest"`, `limit: 25`, `offset: 0`
- page via `nextOffset` until exhausted. A weekday runs 50–200 mails; if it
  pages past ~300, report it rather than silently truncating.

Strip each record to `subject, sender, receivedDateTime, summary,
hasAttachments, uri` — keep `uri` (unlike mail-fetch) because sector agents
may read full bodies. Drop obvious non-research noise (IT alerts, marketing,
calendar plumbing) but NOT conference/event mail — an agenda can be an
actionable.

## 3. Bucket by sector, then one agent per non-empty bucket

Buckets = the modelled sectors plus the desk's standing interests:

```
Non-ferrous (aluminium/zinc) · Steel · Cement · Mining · EMS
IT / AI-tech · Macro & strategy · Other coverage-adjacent
```

The first five come from `specs/sectors/*.yaml` — a newly modelled sector
gets a bucket by existing here, so check the directory rather than trusting
this list. Bucket on subject + summary in the main chat (cheap); a mail that
plausibly belongs to two buckets goes to both.

Spawn the sector agents **in parallel, one per non-empty bucket**, each with
only its bucket's records. Instructions to each agent:

- One bullet per mail that carries something actionable: a number, a rating
  or estimate change, a price/volume datapoint, guidance, an event with a
  date. Skip pleasantries; a mail with nothing actionable returns nothing.
- Bullet = one or two sentences, PM-readable, no jargon-compression.
- Every bullet carries `source` ("Broker — subject line", enough to find the
  mail in Outlook) and `received` (IST HH:MM).
- The metadata `summary` field is ~250 chars. Read the full body via
  `read_resource(uri)` ONLY when the snippet is not enough to state the
  actionable — typically the 2–5 material notes, not all of them.
- Return JSON: `{"sector": "...", "bullets": [{"text","source","received"}]}`

Sectors with no mail are NOT omitted — they go in `quiet`, because "no mail"
and "not checked" must stay distinguishable (the mail-fetch empty-array rule).

## 4. The global section

From the markets file plus the swept mail (a broker's overnight tech note
often beats a headline), write:

- `ai_semis`: 2–5 bullets on AI/semiconductor overnight — earnings, guides,
  big moves. Each bullet's `source` names where it came from (headline
  publisher or broker mail).
- `bellwethers`: for Accenture and Cognizant, `{"name", "reason"}` — one
  line on WHY it moved, only if a dated on-entity headline or broker mail
  supports it. **No supported reason -> `"reason": null`**, and the tab
  falls back to showing the top on-entity headline. Never infer a reason
  from the direction of the move.

## 5. Write and verify

Write `data/morning/brief_YYYY-MM-DD.json` (today, local):

```json
{
  "date": "YYYY-MM-DD",
  "generated_at": "ISO datetime",
  "mail": {
    "window": "24h to HH:MM IST",
    "n_scanned": 0,
    "sectors": [{"sector": "...", "bullets": [{"text","source","received"}]}],
    "quiet": ["..."]
  },
  "global": {
    "ai_semis": [{"text","source"}],
    "bellwethers": [{"name": "Accenture", "reason": null}]
  }
}
```

Then verify the API parses it — the server reads files per request, no
restart needed:

```bash
python -c "import sys; sys.path.insert(0,'packages/api'); import engine, json; d=engine.morning(); print(json.dumps(d['warnings'],indent=1)); print('brief ok' if d['brief'] else 'BRIEF MISSING')"
```

`warnings` should not mention today's brief. Finish by giving the PM the
brief inline — bulleted, same content as the file — plus anything the sweep
dropped as noise, in one line.

## What this never does

- Never writes to `data/ims.db`, `prices`, or anything a pillar reads.
- Never sends, replies to, forwards or flags mail.
- Never states a move's "reason" without a dated source naming the entity.
- Never lets a quiet Sunday render as a broken pipeline — few mails is a
  fact, say it and write the file anyway.
