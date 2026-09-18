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

**Called by `full-refresh` as its Step 1b** (right after mail-fetch, so both
mailbox steps share one known-good M365 token, and before refresh.py so its
closing front-end check certifies the brief too). This file stays the single
source of truth for the procedure — runs standalone exactly the same way.

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

- **PM ruling 2026-08-31: insights, not summaries.** One sentence per
  bullet, ~25 words or fewer, leading with the thing that matters — the
  number, the call change, the dated event. No methodology, no second
  clause explaining the first, no "the broker notes that". The PM opens the
  mail when detail is wanted; the source line is the pointer.
- **At most 3 bullets per sector.** More material mails than that: keep the
  three most tradeable and end the last bullet's source with
  `(+N more in Outlook)`. Dropping is the feature — an eight-bullet sector
  is a digest, not a brief.
- Only genuinely actionable content earns a bullet: a number, a rating or
  estimate change, a price/volume datapoint, guidance, an event with a
  date. A mail with nothing actionable returns nothing.
- Every bullet carries `source` ("Broker — subject line", enough to find the
  mail in Outlook) and `received` (IST HH:MM).
- The metadata `summary` field is ~250 chars — enough to CHOOSE a mail, not
  enough to expand one. So read the full body via `read_resource(uri)` for
  **every mail you bullet**, and for nothing else. That is at most 3 per
  sector, so at most ~24 reads across the whole brief. This REVERSES the
  older "read only the 2–5 notes the snippet cannot cover" rule, which was
  correct while the bullet was all there was; §3b is why it changed.
- Return JSON:
  `{"sector": "...", "bullets": [{"text","source","received","detail"}]}`

Bullet calibration, from the 2026-08-30 run:

- ✗ *"Nvidia's 2QFY27 beat (US$96.2bn revenue, +106% YoY vs US$91bn guided;
  FY28 growth guided ~70%) read as a volume opportunity for Indian SIs:
  CLSA ranks Infosys strongest on Nvidia-ecosystem capability, then TechM
  and TCS, with Persistent the only advanced tech partner in coverage.
  Cautious near term on AI deflation; AI net positive for SIs
  medium-to-long term."* — that is the mail, re-typed.
- ✓ *"CLSA ranks Infosys > TechM > TCS on Nvidia-ecosystem capability after
  the 2QFY27 beat; cautious near term on AI deflation."*

Sectors with no mail are NOT omitted — they go in `quiet`, because "no mail"
and "not checked" must stay distinguishable (the mail-fetch empty-array rule).

**Repeats are handled server-side — do not dedup against yesterday's brief
yourself.** The 24h window overlaps day to day, so a mail bulleted yesterday
is a legitimate candidate again today; bullet it normally if it is still the
sector's most tradeable content. `engine._mark_repeated_bullets` (PM,
2026-09-08) marks any bullet whose MAIL already appeared in an earlier brief
with `seen_on`, and the tab greys it with a "↺ seen" tag — marked, never
dropped. Prefer a fresh mail over a repeat when both compete for the third
slot, but never spend agent time diffing old brief files.

## 3b. The expansion — `detail` on every bullet

PM, 2026-09-18: *"similar functionality to a click and expand for the
commodities below. Just a brief summary of emails with quick read on important
points."* So each bullet carries a `detail` block and the Overview renders the
bullet as a click-to-expand row — the same grammar the what-moved commodity
rows already use for their price charts.

**THE BULLET ITSELF DOES NOT CHANGE, and that is the load-bearing part of this
step.** The expansion is a SECOND layer, never permission to relax the
2026-08-31 ruling: one sentence, ≤25 words, at most three per sector, exactly
as written above. The collapsed page is still what the PM reads at 08:00, so a
brief whose bullets grew because a drawer existed has destroyed the thing the
drawer was added to serve.

```json
"detail": {
  "summary": "2-3 sentences: what the note argues, on what basis.",
  "points": ["<=12-word fragments, 3-5 of them, numbers first"],
  "read": "body"
}
```

- **`summary`** — 2–3 sentences, ≤60 words: the note's ARGUMENT, not its
  table of contents. What it claims, what it rests on, and where it disagrees
  with the desk or with its own last note. Never restate the bullet in longer
  words — the bullet sits directly above it on the page, so a paraphrase
  costs a click and returns nothing.
- **`points`** — 3–5 quick-read fragments, **numbers first**: the new target
  and the old one, the estimate change and its size, the dated event, the
  volume or price print. Fragments, not sentences; no connectives. This is
  the block the PM's eye lands on. Three real numbers beat five padded lines.
- **`read`** — `"body"` when `read_resource(uri)` returned the mail,
  `"snippet"` when it did not (fetch refused, or the content is only an
  attachment). **This is not cosmetic.** A detail built from 250 characters
  of preview is a different claim from one built off the note, and the tab
  prints which it is. Never write `"body"` for a body you did not read.
- **Nothing in `detail` may state a number the mail does not.** If the body
  never carries the old target, the point gives the new one alone. The
  citation standard does not loosen because the text is one click down.

**Omit `detail` entirely rather than padding one.** A bullet without it
renders flat and unclickable — no caret — and that reads as "nothing more
here", which is the honest signal when a mail's whole content is its one
actionable line. An empty drawer reads as a broken page instead.

**Scope: sector mail bullets only.** `ai_semis` stays one line per bullet —
its sources are mostly headlines with no body to fetch, so a `detail` there
would be reconstruction rather than summary.

## 4. The global section

From the markets file plus the swept mail (a broker's overnight tech note
often beats a headline), write:

- `ai_semis`: 2–3 bullets on AI/semiconductor overnight — earnings, guides,
  big moves. Same one-sentence discipline as the sector bullets. Each
  bullet's `source` names where it came from (headline publisher or broker
  mail).
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
    "sectors": [{"sector": "...", "bullets": [
      {"text": "one sentence, <=25 words",
       "source": "Broker - subject line",
       "received": "HH:MM",
       "detail": {"summary": "2-3 sentences", "points": ["..."],
                  "read": "body|snippet"}}]}],
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
