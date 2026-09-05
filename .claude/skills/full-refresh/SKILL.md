---
name: full-refresh
description: One run that fetches every data source, rescores, and rebuilds every front-end input including the Overview's morning brief — MCP steps, sector agents and Python steps in a single sequence. Use for the scheduled overnight refresh, or when asked to refresh everything, update all data, update the full front end, or run the full pipeline.
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

## Step 1b — morning brief (MCP + sector agents, agent only)

Follow the **`morning-brief` skill in full** — its procedure stays the single
source of truth; this step only fixes where it sits in the sequence. It writes
`data/morning/brief_<today>.json`: the 24h all-mail sweep, one summarizer agent
per non-empty sector bucket, the AI/semis bullets and the ACN/CTSH reasons.

Run it **here, not later**, for two reasons:

- It needs the same interactively-authenticated M365 MCP as step 1, so both
  mailbox steps run while the token is known-good — a lapsed token then costs
  one contiguous block of the run, not two scattered holes.
- Step 3's LAST step is the front-end check, which reads `/api/morning`. With
  the brief written first, the check certifies the WHOLE page current —
  "update the full front end at once" is this ordering, not a separate pass.
  (The markets half is no concern either way: the skill's own step 1 fetches
  it, and refresh.py re-pulls it in step 3 — deliberately unguarded, so the
  later pull just lands a fresher GIFT print.)

**Step 1 and this step both read the mailbox and are NOT mergeable.** Step 1's
per-entity searches match the FULL BODY server-side (a mail whose subject never
names NALCO still surfaces); the sweep is date-only and keeps ~250-char
snippets. Deriving `mail_<today>.json` from the sweep would silently lose
body-only entity mentions — the watch's whole completeness claim. Two searches,
two questions, both cheap.

If the sweep fails (M365 down), continue the run: everything downstream is
independent, `/api/morning` shows the dated warning, and the Overview says "no
brief yet" rather than hiding the section.

## Step 2 — metals pack (now ordinary Python, run it every day)

```bash
python packages/adapters/outlook_pack.py --save
```

**Rewritten 2026-08-24. Everything the old version of this step said is obsolete.**
It said the MCP truncates the attachment and returns 2013 data, that the step was
expensive at ~50k tokens, and to **SKIP IT** unless the PM asked. All true of the
MCP route and none of it true any more.

The pack now comes out of **Outlook itself** — real `.xlsx` bytes, full history,
span ending the same day, ~0 tokens because no model reads the grid. It is a
plain unattended step like westmetall, and `refresh.py` runs it. See the
`metals-pack-fetch` skill for why the MCP could never do this, and for the two
wrong constants (sender, filename) that used to make a miss look like a quiet
morning.

**The pack is the PRIMARY price source** for everything it carries — it already
outranks every other source in `prices_io.PRECEDENCE` (40 against westmetall 30,
fred 10, yahoo 5; the issuer-filing source `filing` shares rank 40 and owns
disjoint ids), so nothing needed changing to make that true. All **36** columns
load. When this step was written the 27 steel-complex columns fed no spec; steel
(2026-08-25) took five of them, cement (2026-08-28) the Indonesian coal column,
and mining (2026-08-29) `lme_copper` — what remains parked is only what
`metals_pack.PARKED` lists (lead, nickel, gold, dxy, the China HRC/rebar set).

Two exit codes, and the distinction is the point:

- **no mail today** -> exit 0. A quiet day. The fallbacks below supply what they
  cover, and anything they do not **keeps its last stored price**.
- **Outlook unreachable** -> exit 1, a red step. A broken capability must not read
  as a missing mail, and a missing mail must not read as a broken fetcher.

Needs the machine **awake and logged in**. Locked is fine; asleep was already
fatal to the whole run.

## Step 2b — cement pack (MCP, agent only, and it WORKS here)

Added 2026-08-27. The same Kotak mail carries a **second** attachment,
`Daily Cement Pack, <Month> DD , YYYY.xlsx` (note the stray space before the
comma — `outlook_pack.py` records that this is not a stable discriminator; match
the `Daily Cement Pack` prefix). It holds cement's **output price**, which no
other source in this system carries.

1. `outlook_email_search` for `Daily Cement Pack`, newest first.
2. `read_resource` on that message to get the attachment URI.
3. `read_resource` on the **attachment** URI.
4. Save the returned text verbatim to
   `data/staging/cement_pack_<YYYY-MM-DD>.tsv` — the date being the mail's date,
   because `cement_pack.py` reads the capture date out of the filename.

**Do NOT reach for `outlook_pack.py` here.** Step 2 exists because the connector
truncates the metals pack to the ~830 oldest rows. That does not happen to this
one: the cement pack is a **WIDE** sheet — dates run across the columns — so the
whole four-sheet workbook is 336 lines and ~70k characters, inside the ~200k cap,
current to the same morning. Verified 2026-08-27. Cost is ~18k tokens.

If it ever does outgrow the cap the symptom is a **short span**, not an error, so
read the `--probe` span rather than assuming.

`refresh.py` then loads it in Step 3 (`cement pack (staged)`), and skips
cleanly — never fails — when no capture exists.

**The pack lands ~15 days late (PM), so it confirms a move rather than carrying
it.** That is why `refresh.py` also runs `cement watch (IndiaMART)` — a scrape of
dealer asks that banners a same-day multi-region move on the Overview tab. It is
ordinary Python and needs no agent. The sweep takes ~6.5 min (32 pages at 12s
apart, around IndiaMART's 429 limit), so `refresh.py` **spawns it detached** —
the refresh, and therefore the .vbs launcher that blocks on it, is not held up;
the capture lands in the table minutes later and its log is
`data/refresh/cement_watch_last.log`. Guarded to once a day, marker written at
spawn. **It writes to `cement_watch*`, never to `prices`, and no pillar reads
it.**

## Step 2c — BBG 2-yr forward P/E (screenshots, agent only)

Added 2026-09-02, PM instruction: "keep updating the BBG 2 year numbers daily
with the full-refresh." The feed is a person with a terminal — the PM drops
Bloomberg screenshots of **PE Ratio [2 Yr Fwd]** (GF, BEst blended 24-month)
into the OneDrive root — so the capture is an agent step and everything
deterministic about it lives in `adapters/bbg_pe2y.py`.

1. Look for screenshots newer than the latest staging file:

   ```bash
   ls -t "$HOME/OneDrive - PinPOINT"/Screenshot*.png | head -5
   ls packages/../data/staging/estimates/bbg_pe2y_*.json | tail -1
   ```

2. **No new screenshot -> do nothing and move on.** This is the normal case
   and it is fine: the panel re-marks the captured multiple on each day's
   close (implied 24m EPS = capture-day close / captured P/E), and shows a ⚠
   line per name whenever the Yahoo consensus has moved ≥1% since the capture
   — the "EPS might have changed" warning. NEVER write a staging file without
   a screenshot to read; the multiple must come off a terminal image, not be
   inferred.

3. New screenshot(s): Read the image(s) and transcribe **the last dated row
   only** — the daily history in frame stays untranscribed (480 numbers of
   silent-transcription risk; the images are the record). BBG tickers map
   COFORGE/TCS/INFO/WPRO/HCLT/TECHM/LTM/PSYS/MPHL/TELX/KPITTECH/LTTS/OFSS →
   coforge/tcs/infosys/wipro/hcl_tech/tech_mahindra/ltimindtree/persistent/
   mphasis/tata_elxsi/kpit/ltts/ofss.

4. **Cross-check before staging** (the screenshot equivalent of the name
   guard): each value must sit BELOW the panel's Yahoo P/E FY28E column by a
   margin that grows with the name's growth (~2% on TCS/Wipro, ~7-8% on
   Persistent/Coforge on 2026-09-02). A value ABOVE FY28E, or off by >20%,
   is a mis-read or the wrong terminal field — stop and say so.

5. Write `data/staging/estimates/bbg_pe2y_<YYYY-MM-DD>.json` (date = the
   screenshot's last table row, format per the 2026-09-02 file: `captured`,
   `source`, `screenshots` paths, `note`, `values`). Then load — or skip,
   Step 3 loads it too:

   ```bash
   python packages/adapters/bbg_pe2y.py --load
   ```

## Step 3 — everything Python

```bash
python packages/refresh.py
```

Runs in order, and the order is load-bearing:

```
preflight                HALT on failure — everything downstream is untrustworthy
equity closes            Yahoo, 3mo — includes the mining four since 2026-08-29
                         and the EMS six since 2026-08-30
morning markets          US/semis/IT closes, GIFT Nifty (nseix.com own API),
                         entity-keyed headlines -> data/morning/markets_*.json.
                         DISPLAY-ONLY, never prices. The brief_*.json half is
                         the morning-brief skill (agent — M365)
LME cash                 westmetall — plain urllib, no agent, no auth
NSE OI fetch             the /oi pipeline. WRITES the vault OI History.md files
open interest            reads them into the store. Reports NEWEST DATA AGE when
                         skipped, so a dead source is visible, not "pulled"
FRED coal                iron_ore only in practice; see the note below
flow series              Flows F1 (2026-09-02): S&P 500, SOX, IGV, gold,
                         US 10Y via Yahoo into the dedicated flow_series
                         table — never `prices`. Live partial session
                         dropped, so the newest row is the last COMPLETED
                         US close (T-1 from India, by design). Since
                         2026-09-03 also nine NSE indices (Nifty 50, Metal,
                         IT, Auto, Commodities, Infra, Energy, Realty, PSE)
                         for the weekly panel's India evidence (the
                         sector tape rendered for one day and was removed
                         at the PM's instruction 2026-09-04; the series
                         keep accruing so a restore is UI-only) — Yahoo history plus NSE's own EOD API
                         for the tail Yahoo stopped carrying (its sectoral
                         daily history froze 2026-07-17; the live quote
                         kept updating, which is why it looked alive)
market regime            classifies the FULL history into the 9 regime
                         states + flow spell (specs/flows.yaml), writes
                         market_regime. Deterministic and idempotent
mining filings (fetch)   CIL production/offtake + SWMA e-auction from
                         coalindia.in, NMDC CMS lists — plain urllib, NO agent.
                         Monthly-cadence work run daily like FRED: one page
                         request each, and forgetting silently ages a volume
                         series. NMDC's site lags ~6 months, so its recent rows
                         come from specs/extracted/mining_prints.json instead
mining filings (load)    staging + ledger -> the six mining series (source
                         'filing'). Recomputes every run, so a hand-edit to
                         mining_prints.json lands on the next refresh
EMS consensus (fetch)    Yahoo earningsTrend + trailingEps for the six EMS
                         names — crumb dance, stdlib-only, no login. Stages
                         data/staging/estimates/yahoo_estimates_<date>.json.
                         Daily on purpose: historical consensus can never be
                         backfilled, and the P3 forward-P/E scorer withholds
                         past a 30d capture age. Trailing (normal) P/E is
                         display-only on the EMS panel, never scored
EMS consensus (load)     -> the `estimates` table (NOT prices — an EPS is not
                         a price and must not become bridge-shockable).
                         Idempotent per capture date
metals pack (Outlook)    step 2, an ordinary step since 2026-08-24
metals pack (staged)     loads it. .xlsx first, .tsv only as a legacy fallback
cement pack (staged)     consumes step 2b's capture, skips cleanly if absent
cement watch (IndiaMART) spawned DETACHED (~6.5 min sweep); once a day;
                         writes cement_watch*, never prices
mail watch (staged)      consumes step 1, skips cleanly if absent
concall check            advisory, never writes
desk model check         advisory, no-ops without data/models/
corporate actions
score + persist          HALT on failure
front end                build_frontend.py — step 4, run here so the run
                         verifies what the page will render
```

This block is the ACTUAL `STEPS` list in `packages/refresh.py` — if the two
disagree, refresh.py is the truth and this file is the bug (it was, until
2026-08-30: the block above stopped at eleven steps and named neither cement
nor mining, while refresh.py had carried both for days).

**THE FALLBACK IS THE PRECEDENCE RULE, NOT A BRANCH.** Nothing checks whether the
pack arrived before deciding what else to run. Every source runs every day, and
`prices_io` refuses a write only where a **higher-ranked source already holds that
(entity_id, date)**. So on a pack day the pack wins each cell it covers; on a
no-pack day westmetall, Yahoo and FRED are simply the highest bidder and write
normally. There is no mode to get wrong.

**Where nothing at all covers a series, the gap stays empty.** No synthetic
carried-forward row is ever written. The last real print remains the newest row,
so it is still what every score reads — and `freshness.py` keeps reporting the
true age instead of showing row age 0 for a series that has not printed in a
week. That is invariant 3 (*a quiet name shows rising stale_days and an unchanged
score*), and it is the same reason no expired-futures proxy is substituted.

**The `/oi` skill is now part of this run, and it had to be.** `vault_oi.py` only
*reads* `Coverage/<sector>/<name>/OI History.md` — the vault's
`options-dashboard/oi_to_vault.py` writes them, and **nothing here had ever called
it**. So the refresh faithfully re-read files nobody was updating and reported OI
as "already pulled today" while the newest row aged. It reached seven days before
anyone looked.

I diagnosed that wrong the first time and the correction matters: I said the vault
pipeline had been *retired*. It had not — it had never been **called**. One
incremental run took seconds and moved all 31 F&O names from 2026-08-17 to
2026-08-24, four new trading days each. **A dead source and an uncalled source
look identical from inside the store**, which is why the fetch is now a step with
its own name instead of an assumption.

`packages/adapters/nse_oi.py --fetch` wraps it. Three things about it:

- **Incremental only.** It recovers the prior ~90 days from the existing files and
  downloads only trading days since the last stored date — typically one, a few
  seconds, and a clean no-op when current (`31 already current`). The `--full`
  90-day re-download takes **~50 minutes** and stays a weekly manual job to catch
  NSE back-revisions. This wrapper never passes `--full`.
- **OI is T-1 by design.** `futures.py` walks back from *today − 1*, and NSE does
  not publish the day's F&O bhavcopy until well after the close. On a morning run
  the newest row is **yesterday, and that is correct** — not a failed write, and
  not a reason to re-run.
- **It shares the once-a-day marker with `open interest`.** Both are in
  `SKIP_IF_DONE`. Guarding only one would let them disagree: a fetch that ran with
  a skipped load leaves new rows in the vault and nothing in the store. `--force`
  re-runs the pair.

It is the one place this repo **writes** to the vault. Deliberately narrow —
`OI History.md` is data the vault pipeline owns and the same files `vault_oi.py`
already reads. Nothing else in the vault is touched. If `oi_to_vault.py` is
missing the step exits **0**, not 1: the fetcher lives outside this repo, so its
absence is a missing capability rather than a broken refresh, and the load still
ingests whatever the files hold.

`FRED coal` is kept for `iron_ore` and is **dead weight for coal**. It has never
written a single row of `thermal_coal_seaborne`: the pack holds a daily row on
every month-first FRED would write, and pack 40 beats fred 10, so every coal
write is refused and always will be. They are not the same benchmark anyway —
FRED is Australian thermal at 140.40 for 2026-07-01, the pack is Richards Bay at
103.00, a 36% gap that had been sitting under one `entity_id`.

The `(staged)` steps are no-ops without a staging file and never fail for its
absence, so this command is still correct run by hand.

## Step 4 — the front end

```bash
python packages/review/build_frontend.py
```

**Nothing is compiled.** `packages/web/app.html` is served live by
`packages/api/serve.py` straight out of `data/ims.db`, so the page is current the
instant step 3 finishes — OI, the pair charts and the bridge all read the store
directly. Step 3 already runs this as its last step; the command is here for when
you want to read it on its own.

What it does instead is call every route the page calls, **in-process** so it
needs no listening socket, and report what each tab will actually render:

```
/api/sectors                      ok  4 sectors, 4 live: non_ferrous, steel, cement, mining
/api/sector?id=mining             ok  11/11 priced, 1 dated today
/api/scores[mining_bulk]          ok  2 name(s), as_of 2026-08-29
/api/oi                           ok  15 name(s), newest 2026-08-28 (1d)
/api/tape[composite]              ok  18 name(s), 2,463 point(s), as_of_max 2026-08-29
```

(real output, 2026-08-29 — seventeen routes in all, one line per sector and per
peer group; the OI line reads `SOURCE MAY BE DEAD` when the newest row is >4
days old, which is the state that used to be invisible)

A blank tab and a loading tab look identical, which is the whole reason this is a
step and not something you spot by opening the page. It separates two things
deliberately: a **problem** (a route errors, or a live tab would render nothing)
fails the step; a **warning** (the route answers but the data is old, e.g. OI)
does not, because `freshness.py` already reports that and failing twice daily for
one known-dead source is how a red light stops meaning anything.

> **`build_pillars_page.py` was the old step 4 and is deliberately NOT run.**
> It renders `packages/review/pillars.html` from `packages/review/_pillars.json`,
> and **nothing in the repo writes that JSON** — it has been frozen since
> 2026-08-21. So the step called "rebuild the page" was regenerating a page from
> a stale snapshot on every run and could never show that day's numbers. Keeping
> it scheduled would make a stale page look freshly built. Give it a data builder
> or retire it; do not put it back in the sequence.

### The tabs

Top-level navigation is by **sector**, driven by `engine.SECTORS` — adding one is
a Python data edit and needs no front-end change:

| tab | state |
|---|---|
| **Daily Overview** | the landing tab, reskinned 2026-08-30 in the vault dashboard's visual language. Overnight callout, broker mail, what-moved — and EXCEPTION-ONLY on plumbing since 2026-08-31: no tiles, no Run panel, no stale-feeds section; failed steps / stale feeds / route problems collapse into one amber ⚠ line, and a green run shows nothing about itself |
| **The Book** | split out of the Overview 2026-08-30. The persisted book with SIZE and verdicts; placeholder callouts sit here beside the numbers they qualify. PM plans a rework |
| **Flows** | scoped, not built. 1 of 5 L3 inputs ready |
| **Non-Ferrous** | live. Holds the Pair / Bridge / Positioning views |
| **Steel** | LIVE 2026-08-25 — 5 scored names in two groups (integrated + apl_apollo on the HRC-patra spread) |
| **Cement** | LIVE 2026-08-28 — 4 scored F&O names, one peer group, regional output links. Holds Pair / Bridge / Positioning plus a **Prices & Watch** sub-view (regional chart + IndiaMART asks). P4 withheld pending a guidance ledger |
| **Mining** | LIVE 2026-08-29 — nmdc + coal_india (`mining_bulk`, the F&O pair) and hindustan_copper (`mining_copper`, NOT in F&O, scored on explicit PM decision, cash-only). Volumes are a scored driver (monthly TTM lines); chart is NMDC's own circular sequence. P4 withheld, same as cement |
| **EMS** | LIVE 2026-08-30 — dixon, amber, kaynes, pg_electroplast (`ems_assemblers`, all F&O; syrma_sgs and avalon tracked unscored). The first non-commodity sector: P3 is FORWARD P/E vs growth — since 2026-09-05 a 50/50 blend of the peer PEG ratio and each name's fwd P/E vs its own last 60 days (the peer ratio alone drew flat lines through a sector-wide de-rating) — mood beside it, economics deliberately unbridged, P4 withheld. Its **Prices & Watch** sub-view leads with the consensus panel (fwd P/E, PEG, revision momentum) over the copper chart and cost-complex table |

**Daily Overview** is assembled from what the run *wrote* — `status.json`,
`frontend.json`, `freshness.check()`, `pillar_scores`, the `oi` table — and never
recomputed. A recomputed overview would drift from the persisted tape the moment
an override was active, and then the first screen of the app would be the one
telling a different story. It carries a step-chip row, the stale-feed list, the
book with SIZE, today's moves on **modelled inputs only** (the 27 parked steel
series drive nothing, so they are not there), and futures OI. If `status.json` is
from a previous day it says so at the top rather than showing a green tile over
yesterday's run.

**Flows** probes `data/ims.db` on every request instead of restating
`docs/FLOWS.md`, whose blocker table was hand-verified once on 2026-08-19. A
hard-coded table would go stale silently the first time a blocker cleared — the
failure mode the doc itself warns about. Current read: `dispersion` ready,
`breadth_pct` thin (5 names, so a percentage moves in 20pp steps), and
`rel_strength` / `turnover_pctile` / `flow_fii` blocked — no benchmark index,
`prices.volume` NULL on all 155,401 rows, and no FII source. `sector_regime` holds
0 rows. F4 crowding has its data (96 dates x 4 names) and nothing in `score/`
reads it. **F1 is LIVE since 2026-09-02** — `market_regime` carries ~2,470
classified US sessions (9 states + the flow-spell layer) and the Flows tab leads
with the current read and its next-session odds; the readiness tables above
describe F2-F4 only.

A sector with no `peer_groups` is not an empty tab. It lists the commodity inputs
already arriving for it, dated and sourced, plus the three steps needed to make it
live — so the tab shows what a build would have to work with rather than a
placeholder. Its change column measures against the last close that actually
**differed**, not the previous row, because the pack pre-creates the current day
and carries the prior session forward until the next file backfills it.

## Step 5 — report, and be specific about what did NOT land

```bash
python packages/core/freshness.py
python packages/score/combined.py
```

`data/refresh/frontend.json` holds step 4's per-route result if you need to say
which tab is affected rather than only which series is stale.

State plainly:

- which series are stale and by how many days
- whether the metals pack arrived, and its date — if the newest pack is not
  today's, `--consume metals` says so and loads it for history only
- whether mail staging was written, and how many structural hits it produced
- whether the morning brief was written: mails scanned, which sectors had
  bullets, which were quiet — and if step 1b failed, that the Overview is
  showing "no brief yet" for today
- the composite and SIZE across the scored names (17 across seven peer
  groups as of 2026-08-29)
- around the 1st-4th of a month: whether the mining filings fetch found the
  new CIL month, and whether NMDC's monthly print has appeared in a digest —
  if it has, its row belongs in `specs/extracted/mining_prints.json` (the one
  monthly HAND step mining has; see below)
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
- **NMDC's recent months** — the fetch step checks nmdc.co.in every run, but
  the site uploads its own filings ~6 months late, so NMDC's monthly
  production/sales print and any price-circular change reach the store
  through `specs/extracted/mining_prints.json`, hand-entered from the digest
  that carried them (usually the 2nd-4th of the month). Deliberately not
  automated for the same reason as extraction: the entry is a cited number
  with a basis to check (EX-ROYALTY levels only — Morgan Stanley quotes the
  same circulars including taxes, and loading that basis is the fake-crash
  bug `mining_filings.py`'s header documents). Coal India needs nothing;
  its site is timely and the fetch parses it unattended
- **Nothing is unsourced any more.** The old text here named `alumina_index` and
  `cp_coke` as having no automated source. Both were wrong: `alumina_index` had
  Yahoo `ALA=F` all along, and `cp_coke` now has the pack, so its
  `kind="manual"` exemption in `freshness.py` has been removed and a stale
  `cp_coke` is a real fault again. The one genuinely unsourced series was
  **`brent`**, which nothing outside the pack has ever supplied and which this
  list never mentioned. It is on the pack now too
- **Coking coal is sourced, from one column only.** `PCOKEUSDM` is a dead 404 and
  four replacements were probed without success, so the pack is the only route —
  but it is **column 10** (`coking_coal_spot_aus`), not "columns 9 and 10".
  Column 9 is the quarterly contract and the broker stopped publishing it in June
  2022. It loads as history and reports as `DISCONTINUED`, not STALE

## Reading the freshness report

Three states, and they mean different things:

| shown as | means |
|---|---|
| `STALE — limit N` | a feed that should have delivered and did not. **Act on it** |
| `Nd — no auto source` | never had a feed. Age is FYI, not a fault |
| `skipped … SOURCE MAY BE DEAD` | the pull ran but brought nothing new for >4 days |

The third is the one that used to be invisible. On 2026-08-24 the OI step read
"already pulled today" while its newest row was 7 days old, because the vault
stopped being updated when that pipeline was retired.

## Scheduling notes

Screen lock is fine; **sleep is not**. On AC this machine is set to never sleep
(`AC index 0x0`), so a locked overnight session survives. On battery it sleeps
after 15 minutes and the run dies with it.

The likeliest overnight failure is **M365 auth lapsing**, and it now costs less
than it used to: only **steps 1 and 1b** need the interactively-authenticated MCP
(which is why they sit together). Step 2 moved to Outlook COM precisely because
that dependency was fragile, so a lapsed token loses the mail digest and the
brief's mail bullets and no longer touches prices. Step 3 still refreshes
everything and rescores — including the brief's MARKETS half, which is plain
Python — and step 5 reports mail as stale. One failure, not a dead pipeline.

The new failure mode to know is **Outlook COM**: it needs a logged-in desktop
session, so it works locked and idle but not asleep and not as a service.
`outlook_pack.py` exits 1 in that case so the step shows red, rather than printing
the quiet-day message and letting prices silently age.
