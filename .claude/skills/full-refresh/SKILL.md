---
name: full-refresh
description: One run that fetches every data source and rescores — MCP steps and Python steps in a single sequence. Use for the scheduled overnight refresh, or when asked to refresh everything, update all data, or run the full pipeline.
---

# full-refresh — every fetch, one run

The PM's instruction: **every data fetch happens in one run, no cron.** This skill
is that run. It does the two things a Python process cannot, then hands off to
`packages/refresh.py` for everything else, so there is one sequence and one
status file rather than two half-pipelines that can disagree about what happened.

Working directory for every command:

```
C:\Users\rajvaibhav.yadav\Investment-micro-system
```

---

## Step 1 — broker mail (MCP, agent only)

Follow the `mail-fetch` skill. It writes `data/staging/mail_<today>.json`.

**Write the file even when every search is empty.** An empty array records that
the watch ran and the day was quiet; a missing file means it did not run.
`refresh.py --consume mail` distinguishes them and so must you.

## Step 2 — metals pack (MCP, agent only)

Follow the `metals-pack-fetch` skill → `data/staging/metals_pack_<today>.tsv`.

**Known limitation, do not fight it.** The MCP truncates the attachment at ~200k
characters and keeps the OLDEST rows, so a fetch returns 2010-2013 and never
today. `startPage` is ignored on mail attachments — tested, byte-identical
output. Until that changes this step is expected to contribute nothing current.

Run it anyway: it is cheap, it captures the dated record, and it will start
working if the cap ever lifts. But **do not report the pack as refreshed** —
`--probe` and check the span ends today before claiming it.

The prices the pack uniquely supplies are covered elsewhere now: LME aluminium
and zinc come from `westmetall.py` in step 3. Only `alumina_index` and `cp_coke`
have no automated source, and `freshness.py` in step 5 will say so.

## Step 3 — everything Python

```bash
python packages/refresh.py
```

Runs in order, and the order is load-bearing:

```
preflight              HALT on failure — everything downstream is untrustworthy
equity closes          Yahoo, 3mo
LME cash               westmetall — plain urllib, no agent, no auth
open interest          the vault
FRED coal
metals pack (staged)   consumes step 2, skips cleanly if absent
mail watch (staged)    consumes step 1, skips cleanly if absent
corporate actions
score + persist        HALT on failure
```

The two `(staged)` steps are why the MCP work happens first. They are no-ops
without a staging file and never fail for its absence — so this same command is
still correct if a human runs it with no agent.

## Step 4 — rebuild the page

```bash
python packages/review/build_pillars_page.py
```

## Step 5 — report, and be specific about what did NOT land

```bash
python packages/core/freshness.py
python packages/score/combined.py
```

State plainly:

- which series are stale and by how many days
- whether the metals pack contributed anything current (it probably did not)
- whether mail staging was written, and how many structural hits it produced
- the composite and SIZE for all five names
- **any step that failed, quoted, not summarised**

`data/refresh/status.json` is written by step 3 and served by the API, so a
failed run is loud on the dashboard rather than only in this transcript.

---

## The rule that matters most

**A refresh that half-worked must not read as a refresh that worked.** The
recurring failure in this project is a plausible result with a silent gap behind
it — six entries in `docs/SILENT_BUGS.md`, none caught by a test. Overnight and
unattended is exactly where that bites.

So: if a step fails, say so at the top of the report, not in a footnote. If the
pack contributed nothing, say the pack contributed nothing. Do not write
"refresh complete" over a run where preflight halted.

## What this run still does not do

- **Concall ingestion** — quarterly, `concall-ingest`, needs a human read
- **Extraction into the store** — deliberately never automated; a wrong
  extraction enters as a fact
- **`alumina_index` and `cp_coke`** — no automated source exists. They age, and
  `freshness.py` reports it

## Scheduling notes

Screen lock is fine; **sleep is not**. On AC this machine is set to never sleep
(`AC index 0x0`), so a locked overnight session survives. On battery it sleeps
after 15 minutes and the run dies with it.

The likeliest overnight failure is **M365 auth lapsing** — steps 1 and 2 need an
interactively-authenticated MCP, and CLAUDE.md notes those can be absent in
headless runs. If they fail, step 3 still refreshes prices and rescores, and
step 5 reports mail as stale. That degradation is intentional: one failure, not
a dead pipeline.
