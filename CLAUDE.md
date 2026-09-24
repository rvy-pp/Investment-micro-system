# Investment Micro-System — read this first

Investment micro-system for a long/short book. Built from scratch starting
2026-08-15 for the PM (Rajvaibhav Yadav, PinPOINT Fund). Localhost only, no
hosting cost, no cloud.

```bash
cd C:\Users\rajvaibhav.yadav\Investment-micro-system
python packages/api/serve.py          # -> http://127.0.0.1:8770
python packages/pipeline.py           # dry run; --run to execute
python packages/score/run_scores.py   # compute + PERSIST all pillars
python packages/score/combined.py     # composite + pillar disagreement
python packages/core/db_state.py      # what is actually in the store
```

Remote: **https://github.com/rvy-pp/Investment-micro-system** (private).
Push after any spec or override change — `data/` and `snapshots/` are ignored.

**There is no `daily.py` and there must not be.** The deprecated vault system
has a `/daily` skill that ran for about a month; sharing that word would get the
two confused. The runner here is `pipeline.py`.

---

## THE OLD VAULT SYSTEM IS NOT THIS PROJECT

`OneDrive - PinPOINT\Obsidian Vault` contains a previous system — `/daily`,
knowledge cards, `Scoring/`, event-pipeline workflows. **The PM explicitly
called it trash and asked for a completely new system. Do not read it for
methodology, do not copy its scoring, do not run it.**

`Desktop\CLAUDE.md` documents that old system in detail and will auto-load if a
session starts from Desktop. **It is not relevant here.** Same for the two
memory entries about `/pm` and `Portfolio Management/`.

Three things from the vault ARE used, because they are *data*, not methodology:

| Path | Used for |
|---|---|
| `Broker Mails/*.md` | 44 dated digests — the extraction corpus |
| `Coverage/*/*/OI History.md` | futures OI, already computed daily |
| — | nothing else |

---

## The one inversion everything rests on

**The model extracts cited facts. Deterministic code scores them.**

The previous attempt had an LLM read broker prose and emit a score directly.
That produces numbers that are not reproducible, not decomposable and not
testable — which is why its output read as jargon rather than direction.

## Four pillars, deliberately not more

A ten-factor model overfits and goes rigid. The PM was explicit about this.

| | Question | Determines | State |
|---|---|---|---|
| **P1** ASP | What do they realise? | direction + size | scoring |
| **P2** Costs | What do they consume? | direction + size | scoring |
| **P3** Valuation | Is it already priced? | conviction | **scorer NOT built** |
| **P4** Guidance | Will they hit the quarter? | forward view | scoring |
| Gate | In flavour / out of flavour — can it express? | permission | schema only |

**Flows** is the fifth section, opened 2026-08-19 — investor sentiment, which
sectors are active, risk on / risk off, crowding. It is NOT a fifth pillar: it
never sets direction and never enters scoring. **F1 (the market-wide regime
read) is LIVE since 2026-09-02**: five Yahoo cross-asset series in the dedicated
`flow_series` table (never `prices`), 8 sign-pattern states + quiet + a windowed
flow-spell layer in `market_regime`, odds as empirical base rates over ten
years. **The tab leads with the ROLLING past week, updated every US session
(PM 2026-09-08: "update daily... show weekly trend but calculate past week on
a rolling basis")** — each session read as its own trailing 5-session window
on the weekly σ scale, superseding the 2026-09-03 Friday-week lead ("daily is
of no use"), which survives as the trend strip and the evidence base. The two
rulings agree: the unit is still a week, only the anchor moved off Friday.
Rolling states flip on 50% of sessions (median run 1) — the grade carries the
signal; forward evidence at this cadence samples STATE ENTRIES only (adjacent
readings share 4 of 5 sessions). India next-week evidence stays live
(`^NSEI`/`^CNXMETAL`/`^CNXIT` ride along in flow_series as evidence series,
never regime inputs). Daily still computes and persists (it feeds the spell
and the future review layer). Method frozen in `specs/flows.yaml`; evidence
via `regime.py --backtest` / `--weekly` / `--rolling-backtest`.
F2–F4 are still scoped only — read `docs/FLOWS.md` before touching
`sector_regime`.

P1+P2 is the margin bridge. `market_pct` is the field that does the work: a
captive input contributes ZERO to cost however far its market price moves.
That, not a coefficient, is why one alumina print moves NALCO up, VAML down and
Hindalco barely.

---

## Invariants — do not break these

1. **No number without a citation.** `observations.quote` and
   `guidance.quote` are NOT NULL with non-empty CHECKs.
2. **No intensity without provenance.** `economics.source_note` likewise.
3. **Silence changes nothing.** There is no decay anywhere. A quiet name shows
   rising `stale_days` and an unchanged score. Never add decay.
4. **Score the spread, do not spread the scores.** The curve is flat in the
   tails, so subtracting two scores understated a real 1.75pp gap as 0.09
   points. Pair score is `score(pct_long - pct_short)`.
5. **`base_ebitda` IS the beta.** If a modelled beta disagrees with the
   observed one, fix `base_ebitda` or accept the gap as a P3 effect. NEVER
   insert a fudge factor to make them agree.
6. **A proxy is never aliased to the thing it proxies.** SHFE zinc is
   `zinc_shfe`, never `lme_zinc`. Seaborne coal is `thermal_coal_seaborne`,
   never `thermal_coal_eauction`.
7. **Withhold rather than guess.** No score when `coverage_ok` is false; no
   broker action without a named broker; no row for a name not in F&O.

## Scoring

`score = 3.0 + 2.0 * sgn(x) * |x/k|^p / (1 + |x/k|^p)`, hill form, `p = 1.5`,
anchored so 5% of EBITDA reads 4.0 (hence `k = 0.05`). Config in
`specs/scoring.yaml`. Calibrate by moving the ANCHOR, not `k`.

P4 scores **linearly** (`1 + 4*confidence`) because a confidence is already a
bounded probability — squashing it again would distort it.

---

## The front end — built 2026-08-21, non-ferrous first

Double-click **`launch\Investment Micro-System.vbs`** (a Desktop shortcut points
at it). It refreshes the scores, starts the server on 8770 and opens the page —
about 18s cold. `launch\Stop.bat` frees the port; `launch\Update Now.bat` runs
the refresh where you can read it.

**A Claude Desktop scheduled task `daily-full-refresh` runs `/full-refresh` at
08:00 local daily (PM instruction 2026-09-11; it reversed the 2026-08-21 choice
of no scheduled task).** It is the desktop app's scheduler, not Windows Task
Scheduler — it fires only while the app is open (a missed run fires on next
launch) and needs the machine awake and logged in for Outlook. Its prompt lives
in `~/.claude/scheduled-tasks/daily-full-refresh/SKILL.md`; the skill files in
`.claude/skills/` stay the procedure. Known timing caveat: most broker mail and
the Kotak packs arrive 08:00–09:30 IST, so an 08:00 run leans on the
`outlook_pack.py --save` retry inside the skill and may brief on a thin sweep.
The launcher still refreshes on every double-click; `launch\Install Daily
Task.bat` (Windows Task Scheduler) remains written and unrun.

### Two halves of the API that must not be merged

| | reads | serves |
|---|---|---|
| `api/engine.py` | re-runs `bridge.py` in memory, **overrides applied** | Bridge, Inputs |
| `api/tape.py` | `pillar_scores` **as stored**, never recomputed | Pair |

They disagree whenever an override is active or `run_scores.py` is stale, and
that is correct rather than a bug: the pair chart must show numbers that were
actually persisted, or the chart and the backtest describe different systems.
The Inputs what-if loop needs the opposite, hence both.

### The pair chart scores the spread, and now so can P3

`specs/scoring.yaml` says the pair rule "will apply to the P3 and P4 scores too,
so do the same there." Nothing did until now. It needs each pillar's OWN k,
because each anchors on a different quantity — economics on 0.05 of EBITDA,
valuation on z = 1.0, mood on 2.0. Reusing k = 0.05 on a z-score would read a
1.4sd gap as a 28×-anchor move and pin the pair at 5.0. `tape.curves()` returns
them per pillar for exactly this reason.

Two pillars cannot be spread-scored, for different reasons the UI states
separately: **guidance** is linear (1 + 4·confidence) so the plain difference is
already exact, and **composite** stores `raw = NULL` so there is no single x to
difference at all.

**A correction to how invariant 4 is usually stated.** "The naive difference
understates" is true in the tails and false elsewhere — the relationship is not
one-directional:

| both legs at | naive | spread | |
|---|---|---|---|
| 11.0% / 9.25% | 0.100 | 0.343 | naive understates **3.4×** — the documented case |
| 5.0% / 3.25% | 0.312 | 0.343 | roughly agree |
| 3.0% / 1.25% | 0.412 | 0.343 | naive **overstates** |
| 4.65% / 4.33% | 0.053 | 0.032 | naive overstates 1.7× — hzl/vedl, today |

They agree exactly only when one leg sits at neutral. The honest statement is
the one the chart makes: **the naive difference depends on where the pair sits
on the curve; the pair score depends only on the gap.** That is the reason for
the rule, and it is stronger than "it understates". Both lines are drawn — solid
and faint-dashed — so the divergence is visible rather than asserted.

### What the chart refuses to draw through

A confirmed corporate action **breaks the price line**; each segment rebases to
its own start. Only VEDL 2026-04-30 is confirmed. The 15% jump scan finds eleven
more across the five names since 2011 and **all of them are real market moves** —
the COVID crash, the 2024 election result, the failed VEDL delisting. Those are
marked and drawn *through*. Auto-excluding them would erase exactly the relative
performance a pair chart exists to show; `CONFIRMED_ACTIONS` in `tape.py` is a
hand-verified allow-list and must stay one.

The break path **cannot fire on real data** — vedanta's scores begin 2026-05-01,
the day after the demerger — so it was verified by injecting a synthetic action
into a live window and checking the marker, the two segments and the rebase. Per
the GLOB lesson: a guard needs an acceptance test, not only a rejection test.

### Three things the UI says that the numbers cannot

- **Cross peer-group pairs are flagged.** `peer_group` is the scoring universe;
  the default pair is picked inside one group. Opening on hindalco/hindustan_zinc
  would have led with the comparison the schema exists to prevent.
- **hzl/vedanta on economics is flagged as degenerate**, per the standing finding
  that `pct_of_ebitda` divides the 63.4% stake straight back out. A flat line
  there is the arithmetic, not the absence of news.
- **The pair span shows which leg binds it.** economics advertises 1374 dates but
  a pair gets the intersection, and vedanta starts 2026-05-01 (post-demerger) and
  vaml at its 2026-06-15 listing. Without the note a five-year chart silently
  collapsing to three months reads as a broken query.

### The refresh light

`packages/refresh.py` writes `data/refresh/status.json`; the header renders it.
**A scheduled task that silently stops looks exactly like a quiet market** —
there is no decay here by design (invariant 3), so old scores and unchanged
scores are indistinguishable on the page. Staleness is counted in **trading
days**, or every Monday reads as two days old and the light gets ignored, which
is the only way an indicator can fail.

`refresh.py` is a strict subset of `pipeline.py`, and the subset is the point: it
does equity closes, corporate actions, preflight and score-and-persist, and it
**names the four things it did not do** on every run — Wind zinc, broker mail,
the metals pack and extraction, three of which no unattended process can do.

## The morning brief — the Overview's pre-market block (2026-08-30)

The Daily Overview opens with the overnight callout at the TOP (US, semis,
GIFT Nifty vs the Nifty close, Accenture/Cognizant with a reason) and closes
with broker-mail actionables at the BOTTOM, grouped by sector, every bullet
carrying its source so the mail is findable in Outlook. That ordering is the
PM's, set with the 2026-08-30 reskin (next section). Two dated files under
`data/morning/`, read by `/api/morning`, and the split is the mail-fetch/Wind
constraint again:

| file | written by | when |
|---|---|---|
| `markets_YYYY-MM-DD.json` | `adapters/morning_markets.py` — a refresh.py step | unattended, every launch |
| `brief_YYYY-MM-DD.json` | `.claude/skills/morning-brief` | agent-only — the mailbox is behind the M365 MCP; runs inside `full-refresh` as Step 1b |

**Neither writes to `prices` or anywhere in ims.db.** ACN or the SOX in
`prices` would become bridge-shockable — the same reason `estimates` exists
and cement_pack refuses its own Valuation sheet. Display only.

**GIFT Nifty is nseix.com's own site API** (`/api/market-rate?type=derivatives`,
plain urllib, no token — discovered by watching the SPA's XHR; the HTML is a
5KB shell). Rows arrive duplicated and carry two expiries: front contract =
**max volume, never first row** — on 2026-08-29 first-row was a 23-lot
back-month print against the Sep contract's 46,485. The pre-market number is
the GAP to the Nifty close (from `/api/nifty-market-rate`), and a >10%
gap fails a relative plausibility guard rather than rendering.

**The mail sweep is date-only** — `outlook_email_search` with no `query`
returns everything in the window (verified 2026-08-30), paged 25 at a time.
One agent per non-empty sector bucket summarizes actionables; quiet sectors
are LISTED, not omitted, and yesterday's files render as loud warnings via
`engine.morning()` — never as this morning's bullets. Bellwether "reasons"
need a dated on-entity source or they are `null`; the tab then falls back to
the top on-entity headline from the markets file, and an empty headline list
after the on-entity filter means NO SIGNAL, not "no move" (macro-fetch's
Yahoo-search lesson: the endpoint is entity-keyed, not a search engine).

## The 2026-08-30 reskin — the vault dashboard's face on this system's data

PM instruction, verbatim intent: *"that front-end was way better and
sublime"* — meaning the OLD vault dashboard's LOOK, not its methodology
(which stays trash per the standing ruling). So the visual language of
`Obsidian Vault/Dashboard/index.html` was copied onto app.html; the data
layer did not move an inch.

- **The palette is the vault dashboard's, verbatim, and the page is
  DARK-ONLY** — GitHub-dark (#0d1117 / #161b22 / #30363d), applied by
  re-pointing the existing tokens. The light branch and both
  `prefers-color-scheme` blocks are deleted, not hidden; panels separate by
  1px border now, not shadow. The chart series palette keeps only its
  dark-stepped column — the ORDER is still the colourblind-separation
  safety mechanism, do not reorder it.
- **Component kit** (`.pgt`, `.meta-line`, `.panel`, `.callout`, `.pill`,
  `.plist` ▸ bullets, `.dwrap/.dbar` percentile bars, `.sechead`) matches the
  vault original's class behaviour and px values where they existed there.
- **Overview order is the PM's (re-cut twice, 2026-08-31):** title +
  meta-line, overnight callout, cement-watch line, broker mail, what-moved
  LAST (read the morning's words first, then the tape that should
  corroborate them). **Tiles, the Run panel and the Stale-feeds section are
  ALL GONE — "just warn me if something isn't executed."** The tab is
  EXCEPTION-ONLY on plumbing: a healthy run renders nothing about itself;
  failed steps (by name), stale feeds (by name and age), route problems and
  a yesterday's-run/brief date all collapse into one amber ⚠ line under the
  title. The full step record still lives in data/refresh/status.json — the
  page just stops re-printing it when it is green.
- **Mail bullets are INSIGHTS, not summaries (PM, 2026-08-31):** one
  sentence, ≤25 words, max 3 per sector, source line as the pointer into
  Outlook — the morning-brief skill carries the calibration example. The
  renderer does not truncate; the discipline lives at generation time.
  **Since 2026-09-18 each bullet also carries a `detail` block** — a
  ≤60-word summary plus 3–5 number-first points — and CLICKS OPEN into a
  drawer beneath itself, the what-moved commodity rows' grammar (PM: "similar
  functionality to a click and expand for the commodities below"). Native
  `<details>`, no fetch: unlike the price chart the payload is already on the
  page. **The collapsed bullet is unchanged, and that is the load-bearing
  part** — the drawer is a second layer, never licence to write a longer
  bullet, because the collapsed page is still the 08:00 read. A bullet with
  no `detail` renders flat and WITHOUT a caret (an empty drawer reads as a
  broken page; a missing caret reads as "the bullet is the whole mail"), and
  `detail.read` prints `snippet` when the drawer was built off the ~250-char
  preview rather than the body — provenance, not decoration. The sector
  agents now read the body of every mail they bullet (≤24 reads/run).
  `engine.morning()` needed no change: it passes the brief through verbatim
  and `_mark_repeated_bullets` only touches source/received/text. Run/brief staleness rides in the meta-line;
  frontend problems are a red hint line inside the Run panel; placeholder
  callouts live in the Book tab beside the numbers they qualify. The cement
  watch keeps a callout ONLY in its `live`-with-alerts state; `calibrating`
  stays one quiet line (an absent banner reads as "no move" — the one thing
  it does not mean).
- **The Book is its own top-level tab** ("The Book", between Daily Overview
  and Flows) — same `/api/overview` book block, new address. The PM plans to
  rework it; the Overview is the morning read.
- **Positioning is the vault's viewOI, ported, then cut to ONE horizon
  (PM, 2026-09-15):** tiles, buildup pill, percentile number-over-bar, the
  z+% Mag cell, per-sector grouping, status pills (OI is T-1 by design, so
  ≤2d = Live). `/api/oi` rows carry `sector` and `name` from the specs for
  the grouping. The 15d buildup, 15d percentile and the day OI/price delta
  columns are REMOVED — "adds no value", only the 3m read stays. **The 3m
  percentile is computed by `vault_oi.py` as the raw rank of today's OI over
  the last 63 table rows, NOT copied from the vault frontmatter.** The
  vault's `percentile_3m` is expiry-cycle-normalised (rank of OI / own-cycle
  median), which put Coforge at 95th while its OI sat 8.6% below the 3m
  median with a -0.7σ z-score in the same block, and 10 of 31 names on the
  wrong side of 50. The stored columns `oi_percentile_15d`, `oi_chg_pct`,
  `price_chg_pct` still load (data, cheap); they just are not rendered.
  **Dalmia is OUT of OI tracking (PM, 2026-09-15: no longer in F&O)** —
  unmapped in `vault_oi.NAMES`, its `oi` rows deleted; scoring untouched.
- **IT is OI-ONLY (added 2026-08-31, PM instruction):** 13 names mapped in
  `vault_oi.NAMES` (12 F&O + LTTS not_in_fno), no specs, no pillars, no
  Book rows — `vault_oi.UNMODELLED` ensure-inserts their `entities` rows
  (kind company, sector 'it', NO peer_group, so invariant 7 keeps them out
  of every scoring path) purely to satisfy the oi FK and give the
  Positioning tab a label. The vault fetcher was already pulling all 13
  daily; only the load-side map was missing. `engine.oi_snapshot` resolves
  sector/name as specs -> entities table -> "other".
  **Amended 2026-09-01 — IT still scores NOTHING, but it is no longer
  OI-only.** PM: "rather than scoring, we will look at 1 year forward p/e
  ratios." IT now has its own sector tab whose content is the consensus
  forward-P/E panel (scatter vs growth + table, with a 1-yr/2-yr horizon
  toggle — the 2-yr side is Bloomberg's blended 24-month multiple,
  hand-captured from terminal screenshots via `adapters/bbg_pe2y.py`, since
  no fetchable source carries FY29), fed by the same daily
  `yahoo_estimates` capture as EMS and by closes in `prices` (13 tickers in
  `yahoo_prices.CANDIDATES`; LTIMindtree is **LTM.NS** — the company renamed
  itself "LTM Limited", which is why every old-name search returned nothing;
  PM supplied the ticker 2026-09-02, cross-checked against BSE 540005.BO).
  Still no specs, no peer_group, no pillars — `consensus_panel` falls back
  to the `entities` table for the roster, and its display sub-groups
  (`est_groups` in the SECTORS entry) are the vault coverage convention,
  not scoring universes. `packages/review/it_forward_pe.py` is the
  standalone cross-section; the panel relaxes the PEG growth floor for
  display only (`compute_row(require_growth=False)`) because TCS, Infosys
  and Wipro all grow under 5% FY27→FY28 and their multiples are still the
  point.

**Preflight rule 2 was corrected the same day, and it was a real blocker:**
the morning refresh HALTED because the four EMS names carry a `peer_group`
and deliberately zero bridge lines, and the rule demanded a `base_ebitda`
that nothing divides by. It now requires base_ebitda only where lines exist
("bridged", not "scoreable"); the lineless state stays visible as a warn.
The halt had cost the whole day's scores for every sector — a guard written
for bridged sectors firing on the first unbridged one.

## The Book tab is the PM's ACTUAL book — `daily_review`, 2026-09-05

The promised rework landed: the tab now leads with the PM's real IMS
positions, pair-wise, and the model-scores table is demoted to a "model view"
section beneath. Driven by the **`daily_review` skill** — on demand, on days
the PM wants, entirely OUTSIDE `refresh.py`/`pipeline.py`. Flow: PM uploads
the day's IMS snapshot → the skill transcribes it into
`data/book/staging/YYYY-MM-DD.json` → `packages/book/book_io.py --load`
persists and computes → `/api/book` (engine.book_view) → the tab. Then the
review conversation: thesis / sizing / add / trim per pair, verdicts recorded
in `book_pair_reviews` + `data/book/reviews/`.

**The two things the company IMS cannot show, which is why this exists:**

1. **Pairs.** The IMS lists positions singly; the PM tags each with a pair
   name ("IT 5") and a tag can carry MORE than two legs. Display is one line
   per pair — inception, days on, legs, chained P&L, day P&L, gross MV.
2. **Rollovers.** Bloomberg contract tokens (`=U6`) reset at expiry and the
   IMS P&L column resets with them. `book_io` strips the token to a stable
   root and CHAINS P&L: contract change between snapshots freezes the old
   contract's last-seen P&L into `realized`; total = frozen + live.

**The input format is settled — it is `/pm`'s** (2026-09-06, first real
snapshot loaded the same day: 04-09-2026, 32 positions, 14 pairs, all legs
mapped). The export is the tab-separated IMS paste specified in the vault's
`Portfolio Management/IMS-Spec.md` (PM-confirmed 2026-08-09): sector
aggregate rows, position rows, the N.A. bucket (closed positions' realized
P&L + currency/rollover/fixed costs — the operation's cost of carry, zero MV
by construction), a book-total row. Numeric tail after LONG/SHORT is always
`MV% βMV% DTD DTD% DTD_trading MTD YTD MTD% YTD% GMV%` (USD; % of NAV). The
Cost column is being dropped from the export; the parser takes both shapes.
**NAV is derived per snapshot** (`DTD ÷ DTD%`, median), never hardcoded —
$3,000,965 on 04-09. `book_io.py --parse` stages and loads in one step, and
its **cross-foot against the export's own total row is the transcription
guard** (MV/GMV to 1e-9, P&L to 2¢) — a dropped or mangled line refuses.

**Invariants of the chain, each tested in `--selftest`:**

- **A snapshot is the FULL book.** Absence from a snapshot = closed. That is
  how re-entries are detected — so the skill must never load a partial
  paste (phantom exits are silent).
- **Segment splits key on STORED SNAPSHOT DATES, never calendar days.** The
  tool runs on demand; a quiet fortnight between runs must not split a
  continuously-held contract and double-count its P&L. The first draft used
  a 10-calendar-day rule and had exactly that bug.
- **The chain basis is the IMS's per-ticker YTD PNL column, never
  recomputed** — no lot sizes or multipliers exist here, per the
  silent-arithmetic rule. A YEAR BOUNDARY also splits a segment: YTD resets
  Jan 1 with no ticker change on cash lines (futures never span it).
- **`gap_risk`** flags a roll observed across a snapshot gap > 7 calendar
  days: the dying contract's final P&L may be under-captured. Flagged, not
  "corrected".
- **ASSUMED, VERIFY ON THE FIRST OBSERVED ROLL:** a fresh contract's YTD
  starts near zero (the old contract's realized P&L falls into N.A.). If the
  new ticker ever CARRIES the old P&L, the chain double-counts. Evidence so
  far: KAYNE opened in-window shows DTD == MTD == YTD exactly.
- Options (`XXXX IS MM/DD/YY C1000 Equity`) have no `=` token → each series
  its own root, matching the PM's no-delta-netting instruction in IMS-Spec.

### The money column is YTD, and it is OUR ledger — 2026-09-18

PM: *"instead of the Day $ that you directly pick from what I input, replace
it with YTD $, which you calculate daily. The input I give expires after
rolling."* So the Book tab's `day` column is GONE and `pnl_ytd` sits in its
place, computed by `book_io._ytd_leg`.

**It is seeded ONCE from the IMS YTD column and thereafter only ever advanced
by an INCREMENT.** That is the whole mechanism: a column that resets cannot
reset a number it is no longer being read into. Four increments, each chosen
against a specific failure:

| situation | increment | why that one |
|---|---|---|
| same contract | Δ of the cumulative YTD | cumulative, so it spans sessions never snapshotted |
| **roll** | the **DAY** figure | not a level, so it cannot reset — and cannot double count |
| reopen after an absence | the new ticker's YTD | its own accrual; covers more than one day |
| 1 January | **resets** | that is what YTD means |

**DO NOT "simplify" this by summing the day column.** Measured on the live
book the day-sum is **+4,124.78** against a true **+3,774.02** — a 350.75 hole
that is exactly **Monday 2026-09-14, an NSE session with no snapshot**. The
ledger absorbs such a session whole at the next paste; a day-sum loses it
permanently and silently. This is the SILENT_BUGS shape: the wrong number is
plausible and nothing raises.

**The roll bridge is the DAY figure specifically because of the standing
ASSUMED-AND-UNVERIFIED note above.** If a fresh ticker ever CARRIES the old
contract's P&L, `_chain_leg` freezes the old YTD and then adds a new YTD that
already contains it — a double count. A day figure cannot do that. The
selftest proves the divergence rather than asserting it: on a fixture where
V6 carries U6's 5,000, the chain says **10,200** and the ledger says **5,200**
and raises a flag. `pnl_total` (since inception) is left alone; the two are
IDENTICAL today (4,523.52, reconciling to the raw IMS YTD sum) and diverge
only at a roll or the year boundary.

**Flagged, never corrected.** A roll whose YTD and day figure disagree by more
than `YTD_ROLL_TOL`, or any year seam, lands in `ytd_flags` → a `ytd?` chip on
the pair and a printed line in `--report`.

**It advances only when a snapshot is loaded.** It recomputes from every
stored snapshot on each request, but `refresh.py`/`pipeline.py` never write a
`book_*` table, so on a day the PM pastes nothing the tab correctly shows the
last snapshot's YTD. Marking to market from `prices` instead was considered
and REJECTED: it needs a per-contract multiplier, which this system
deliberately does not have (see the chain-basis rule above).


**Pairs are DICTATED, not derived — `specs/book.yaml pairs` (PM, 06-09-2026,
names verbatim).** The IMS pair tags ("IT 5") are coarser clusters and stay
internal (the chain and the store still key on them); the page renders the
PM's 20 pairs under the PM's sector headings, in dictation order. A leg may
serve several pairs (TCS long backs the INFO, WPRO and HCLT shorts; DIXON
short backs three longs) — engine.book_view apportions a shared leg's
DOLLARS across its pairs by the gross of the OPPOSITE side of each pair, so
the pair dollars sum exactly to the book (verified: 749.5 vs 749.52).
Price-%s are never apportioned; the pair's % is SINCE ENTRY — mean(long
legs' price moves) − mean(short legs'), each leg anchored on **the OPEN of
its entry day** (PM rule 2026-09-07: "the purpose is to see if the pair has
worked out in thesis" — the IMS avg cost blends adds and pre-capture
history, so it answers a different question). Opens live in
`book_entry_anchors`, fetched once per streak by `book_io
--fetch-anchors` (auto-run after every load) from the same Yahoo chart
endpoint as the closes, and REFUSED unless the fetched close matches the
stored `prices` close to 0.5% — the wrong-symbol guard. Fallback when no
open could be fetched: IMS avg cost, then the entry-day close; the leg
hover names which. **The anchor date is fixed at the day entered and
survives resizes and pair-tag changes; it resets only on a direction flip
or a day out of the book** (same ruling — `book_io._entry_anchor`,
keyed on root across tags after the 09-07 IT retag silently re-anchored
MPHL and TELX to 0.0%). Every position event is logged at load time into
`book_anchor_log` (entered/reopened/flipped reset the anchor; retagged/
resized do not; closed ends a streak) — the tab and `--report` say the
day's events out loud, `--rebuild-log` replays history. A live position in
no dictated pair renders as a loud callout —
ask the PM, never guess it into a pair. Reviews key on the dictated name.

`specs/book.yaml` also holds `ticker_map` (ticker first token → entity_id,
joins each leg to its model composite — SEEDED from IMS-Spec's PM-confirmed
table, so it is sourced, not guessed; unrecognized tickers are flagged,
never guessed), `names` (print names) and `carry` (desk-stated pre-capture
P&L, displayed flagged, never mixed into the chain — inception can only be
the first STORED snapshot). Book tables are `book_*` only; nothing on this path writes to
`prices` or anything a pillar reads, and position data stays in gitignored
`data/`. The API server must be RESTARTED after engine changes (it imports
once — `/api/version` shows staleness); done for this change.

### The long/short index — rebuilt from scratch 2026-09-17, and it replaced TWO charts

PM: *"Let's rebuild the whole thing again. We get rid of both graph
methods."* Then the spec, verbatim: *"Create an equal weighted index for
longs, equal for shorts... Keep VAML out, use 2007 onwards. Once VAML starts
to exist, add VAML to the long index. Till 16th sep 2026, we keep the index
same. Post that, as and when the positions change, we change the index on a
daily basis."*

**`engine.book_ohlc` and the first `engine.book_index` ARE DELETED. Do not
reintroduce either.** The first was a portfolio-weighted back-cast of today's
roster over a chosen window — years long, survivorship-biased by
construction. The second read the roster straight off the stored snapshots, so
it was honest and **nine sessions long**, and with membership unchanged across
all eight snapshots it was a re-weighted copy of the first. Carrying both was
the problem; the reason they both existed is settled inside the replacement,
by freezing membership at a **spec date** instead of at today.

`packages/book/basket_index.py` owns the arithmetic, the guards and the
selftest. `engine.book_index(rng)` is a transport shim; `/api/book_index?range=`
serves it; one block on the Book tab renders three charts — long index, short
index, long ÷ short.

**Parameters live in `specs/book.yaml index`, not in code**, because each one
is a judgement that would otherwise have to be found again: `start:
2007-01-01`, `freeze_before: 2026-09-16`, `session_quorum: 0.5`.

**Membership has two regimes and the boundary is a CONSTANT.** On or before
the freeze, the roster is that snapshot's, held constant — the back-cast.
After it, the roster of the latest snapshot on or before the previous close,
so the index follows positions on and off. **Deriving the freeze from "the
latest snapshot" would advance it every morning and silently re-write the
whole back-cast to whatever the book looks like today**, which is exactly the
bias the split exists to bound.

**Thirteen of the thirty-two legs list after 2007**, so `n` grows from **9
longs / 10 shorts to 16 / 16** — VAML 2026-06-15, Kaynes, Syrma, KPIT, Dalmia,
Amber, Dixon, LTTS, LTIMindtree, APL Apollo, Coal India, Persistent, NMDC. The
PM's VAML rule generalises to all of them or the index cannot start in 2007 at
all. **This is NOT a constant-composition index** and the page says so: when a
leg joins, every other leg's weight falls. And a leg **never contributes a
return on its first day** — it joins at its first close and counts from the
next session, or a listing-day pop at a price nobody in the book paid enters
as performance.

**The index has ONE base (its first session, level 100); a CHART has a
window.** `window()` slices and rebases for display and never re-chains, so
two windows of the same index cannot disagree about what happened between two
dates — tested. Current levels: **long 2879.54, short 2448.88, ratio 117.59**.

| window | long | short | L/S % | pp apart |
|---|---|---|---|---|
| 1y | −0.88% | −9.55% | **+9.59%** | +8.67 |
| 5y | +94.46% | +58.60% | **+22.61%** | +35.86 |
| max (19.7y) | +2779.54% | +2348.88% | **+17.59%** | **+430.66** |

**That last row is the ratio-vs-pp distinction at the scale where it stops
being pedantry** — +17.59% against +430.66pp. The ruler makes the same point
live: a mid-window drag reads **+336.24% against +1,977.38pp**, and the two
agree *only* at the rebase date, where the level is 1.00 (verified: a drag
from the base gives +2465.48% and +2465.48pp exactly).

**Every window is CANDLES; the bar PERIOD is what changes** (PM, same day:
*"can you make candles since you have ohlc? It should be simple right?"* — it
shipped drawing a line above 260 bars, because a daily candle over 19.7 years
is 0.19px wide). Daily to 320 sessions, **weekly** to 1,600, **monthly**
beyond, which puts every window between 3.8 and 9.3px a candle:

**The windows are the PM's** (same day: *"Lets remove 10 and max, add 6 months,
3 months, 1 month"*) — **1M / 3M / 6M / 1Y / 2Y / 3Y / 5Y**, shortest first.
10Y and MAX are gone. **The INDEX still runs from 2007; only the VIEWS
changed** — `build()` chains the whole history regardless, the header keeps
printing the levels against their 2007 base, and the freeze logic is
untouched. Dropping a window is not dropping the data, and confusing the two
would move the base every time a button changed.

| window | sessions | bars | period |
|---|---|---|---|
| 1M | 21 | 21 | daily |
| 3M | 64 | 64 | daily |
| 6M | 123 | 123 | daily |
| 1Y | 246 | 246 | daily |
| 2Y | 494 | 105 | weekly |
| 3Y | 737 | 157 | weekly |
| 5Y | 1,234 | 262 | weekly |

Monthly is now unreachable from the UI but the code path stays: it is the
correct answer above 1,600 sessions and `--selftest` still covers it.

**The aggregation is EXACT, which is why it beats thinning**: open = the
period's first open, high = max, low = min, close = last close. Every session
still contributes — verified against the raw daily levels, 236 monthly bars,
**zero mismatches**, and the session counts add back to the window exactly.
Dropping every fifth bar would have been a silent lie about the path.

**The bound survives aggregation.** A daily high here is already an upper bound
on the index's true high; the max of upper bounds over a month is still an
upper bound, and likewise the min for the low. So the wick means exactly what
it meant daily — checked: the bound contains the body on 850/850 bars across
all windows and all three charts.

Two labelling traps came with it, both fixed at the site. The **rollup's open
falls back to the first bar's CLOSE, never the next bar's open** — only the
index's base bar lacks an open, and its close is the level the period started
at; falling through discarded the first session's move. The selftest caught it,
and it was the only thing that could have: on real data it shifts one monthly
candle out of 237 and the chart still looks sensible. And the **ruler's span is
BARS, not sessions** — it printed "90 sessions" on a monthly chart where 90
bars is 3,660 sessions, twenty times short. It now reads `178 months (3,660
sessions)`, summed from each bar's own count.

**There is no index table, deliberately.** The whole series recomputes from
`prices` and `book_positions` in well under a second, so persisting it would
only create a second thing that can be stale. **The daily review needs no step
for it**: a snapshot lands in `book_positions` and the index extends itself.

#### What counts as a session — measured over 4,867 dates, not assumed

Three independent filters, each reported separately on the page so one cannot
mask another:

- **QUORUM.** A date is a session only if ≥50% of the legs listed by then
  printed. 4,863 of 4,867 dates sit at 100%; the only two below 90% are
  **2010-02-06, a SATURDAY special session that just 1 of 20 listed names
  carries**, and 2026-09-16 at 75%. Nothing is near the threshold.
- **PHANTOM SESSIONS — 10 of them.** 2008-11-27, 2009-10-13, 2014-04-24,
  2014-10-15, 2025-03-18, 2026-01-15, 2026-05-01, 2026-05-28, 2026-06-26,
  2026-09-14. Yahoo answers for a day the exchange was shut with
  `open == high == low == close` and volume 0. The test is the tape's own,
  applied across members, so a single halted stock on a real session can never
  take the day out. A holiday calendar was rejected: another thing to maintain,
  wrong the first year nobody updates it. **The rows are still in `prices`** —
  this cleans only what the index reads.
- **ALL-OR-NOTHING.** A session yields a bar only if every LISTED member has a
  close at both ends; otherwise no bar and the chain runs on, so the interval
  widens but stays COMMON to every member. **It costs exactly one session in
  19.7 years** (2026-09-16). The bar that absorbs a gap is MARKED by the API
  (`skipped`) rather than inferred in the browser — a skipped session leaves no
  bar, so `span` is the previous bar's date either way and cannot reveal it.

**A correction to what this file said on 2026-09-16: that session was NOT a
fetch that failed.** Yahoo itself has no bar for those eight names on that
date — a hole in the source. Checked directly against the endpoint.

**The same hole shows up a second way on 2026-09-07, and the chart says so.**
Eight long legs (amber, dalmia, hindalco, jindal_steel, kaynes, ltts, mphasis,
syrma_sgs) carry a CLOSE for that date but no open/high/low, and the deep
backfill could not fill them because Yahoo now skips 09-07 for those names
entirely — it serves 09-04 then 09-08. The closes are genuine and distinct
(none repeats 09-04's), so **the chain uses them and the level is right**; what
cannot be drawn is the candle, because the range was never measured for half
the side. That bar renders as a labelled close TICK rather than a doji — a doji
would claim a range of zero, which is a measurement, where the truth is an
absence.

#### 2007, and why not 1996

The endpoint reaches 1996 for six of the legs, and **the deep history is
unusable**: Tata Steel −86.8% (2004-04-27) plus thirty >35% moves before it,
UltraTech −75.6% (2004-08-24, the Grasim cement demerger), Hindustan Zinc
**+5,575%** (2006-11-21), Ambuja +652% (2004-10-14), Wipro +400%
(1999-09-27), and five **identical** +949.6% jumps in Tata Steel between 1996
and 1999 — a magnitude that repeats to the decimal cannot be a market. From
2007 the only one-day move above 35% anywhere in the thirty-two is **VEDL's
real 2026-04-30 demerger**, which `core/corporate_actions` already carries;
it is dropped from that day's mean while the 2008 crash and the 2020 COVID
moves are drawn through, per tape.py's allow-list rule.

**Splits are adjusted; demergers are not.** Tata Steel's 1:10 (2022-07-28) is
smooth through the event, so the chart endpoint is split-adjusted. VEDL is
773.60 → 271.55 on **both** `close` and `adjclose` — Yahoo reports splits as
events but not demergers, so that break stays a hand-verified allow-list
problem.

#### The backfill, and the trap that would have poisoned it

`yahoo_prices.load("2007-01-01", only=<the 32 legs>)` wrote **123,768 OHLC
rows**; `prices` went 201,267 → 280,166. Every one of the 32 resolves, and
**not a single bar is missing open/high/low**.

**`range=max` SILENTLY OVERRIDES `interval=1d`, and `fetch_bars` never read
`meta.dataGranularity`.** Measured: `max` on TATASTEEL.NS returns
`dataGranularity: 1mo` — 370 **month-end** bars spanning 1996-2026 — and on
VAML.NS returns `1h`, 481 hourly bars collapsing to 69 distinct dates. Both
parse perfectly and both would have been stored as daily rows: monthly candles
written as sessions, or a date carrying five bars of which the last silently
wins. Nothing downstream could see it — the closes are real closes, of the
wrong period. That is the same shape as the `chartPreviousClose`/`range=5d`
warning already at the top of that file: **the numbers are right and the
PERIOD is wrong.**

`fetch_bars` now **verifies** the granularity and refuses anything but `1d`,
and `rng` accepts an **ISO date** that goes through `period1`/`period2` — the
only way to get daily bars past 20 years (31 years of `1d` for Tata Steel).
Range tokens hold `1d` up to and including `20y` and break at `max`. Tested
both directions: the date and `1mo`/`1y` accept, `max` is refused on both the
monthly and the hourly flavour.

## Price sources have a precedence order — read before adding a feed

Added 2026-08-21. Four adapters wrote `prices` with `INSERT OR REPLACE` and no
`source` column, so the last one to run owned every overlapping date and nothing
recorded who that was. `prices.source` now exists and every adapter writes
through **`packages/core/prices_io.py`**:

| rank | source | |
|---|---|---|
| 40 | `metals_pack` | licensed, hand-dropped, the desk's own reference |
| 40 | `cement_pack` | same broker, same mail, same provenance — cannot collide, it owns only `cement_price_*` |
| 30 | `westmetall` | real LME cash-settlement, free, day-delayed |
| 20 | `wind` | Wind terminal |
| 10 | `fred` | monthly fallback |
| 5 | `yahoo` | equities and exchange proxies |

A write is **refused** when a higher-ranked source already holds that cell, and
the refusal is reported. Equal rank overwrites, so Yahoo can still improve its
own close intraday. A legacy row with `source IS NULL` ranks 0 — anything may
replace it, which is right, because those are the rows of unknown provenance.

**`lme_aluminium` was CME, not LME, and had been for months.** `yahoo_prices.py`
loaded `ALI=F` — a documented proxy that embeds a Midwest premium — into a series
named for the LME. That is invariant 6 exactly, the rule the same file honours
for zinc (`zinc_shfe`, never `lme_zinc`). The gap is not cosmetic: **+142 USD/t,
+4.5%** against real LME cash on 2026-08-20, and across 161 overlapping dates the
store was a **bimodal mixture** — 70 dates within 30 USD/t of LME cash, 65 beyond
80 — depending on whether Yahoo or the pack wrote last. `ALI=F` is now removed
from `CANDIDATES`; do not re-add it.

**Correcting it moved the aluminium scores hard.** The 30-day shock went
−10.09% (CME) to **−15.35%** (LME cash), and economics fell hindalco 3.82→2.51,
nalco 3.97→2.65, vaml 4.03→2.23. The zinc names did not move, which is the
control that says the change is real rather than a bug.

### The correction was invisible until the endpoints were cleaned

Loading 161 rows of LME cash changed **nothing** at first, and looked like it
had worked. The bridge's window resolved to two leftover CME rows:

```
start 2026-05-23  3,720.28  source NULL   <- a SATURDAY
end   2026-08-21  3,344.75  source NULL   <- Yahoo CME, written that morning
```

**A partially-corrected series is worse than an uncorrected one, because it looks
fixed.** `packages/core/clean_lme_aluminium.py` removed 71 rows: 66 weekends (the
LME does not settle on a Saturday, and score dates DO land on them — `as_of` runs
on calendar days) and 5 orphaned CME weekdays. Only aluminium. `lme_zinc` has 66
weekend rows too and they are deliberately kept: those came from the pack, which
carries a full calendar and forward-fills its own LME cash, so its weekends are
the *same measure* as its weekdays. Aluminium's were a different one.

**Still mixed, and known:** `lme_zinc`'s window spans `metals_pack` → `westmetall`
(both LME cash, ~22 USD/t apart) and `alumina_index` spans NULL → `yahoo`. Both
are one measure, so neither is urgent; a shock whose endpoints have different
`source` values is worth checking before trusting it.

**Pre-2026 aluminium is untouched** — ~4,493 rows, still a pack/CME mixture. No
LME cash source reaches back that far, so removing them would leave a hole
rather than a correction.

## Getting LME, alumina and iron ore — search first, then a fetchable mirror

`lme.com` returns **HTTP 403** to automated fetching and its prices are licensed,
which is why `yahoo_prices.RESEARCH_SOURCED` concluded "no live free feed found".
That conclusion was reached by trying `WebFetch` on lme.com alone. **Search finds
both the price and a fetchable mirror**, and `WebFetch` itself works fine here.

| want | route | note |
|---|---|---|
| LME Al/Zn **cash** + 3-month | `westmetall.com/en/markdaten.php?action=table&field=LME_Al_cash` | dated table, ~161 rows, free, **T-1** |
| LME 3-month | Wind `AH.LME`, `ZS.LME` | agrees with westmetall's 3M to ~3 USD/t |
| SHFE aluminium / alumina | Wind `AL.SHF`, `AO.SHF` | current to **T**, CNY/t, VAT-inclusive |
| Dalian iron ore | Wind `I.DCE` | current to **T**, CNY/t |

**Cash and 3-month are different instruments and the gap is not small** —
westmetall's own zinc basis averaged **+84 USD/t** backwardation. The specs price
off cash. Wind's `AH.LME`/`ZS.LME` are the **3-month** (identified by matching
westmetall's 3M column), so they are NOT loaded. No Wind cash code resolves
(`AHC/ZSC/AHS/ZSS` all empty).

**SHFE/DCE are Chinese domestic, not seaborne.** Per invariant 6 they would be
`alumina_shfe` and `iron_ore_dce`, never `alumina_index` or `iron_ore`.

**It does NOT need an agent — corrected 2026-08-21, same day.** The line above
originally said it did. That was inferred from lme.com's 403 without testing the
mirror: **westmetall answers plain stdlib `urllib` with HTTP 200 in ~1.5s.**
`adapters/westmetall.py` fetches and parses directly and is an ordinary cron
step, now in `refresh.py`.

The distinction that was blurred: Wind and M365 are **interactively-authenticated
MCP servers** and genuinely can fail unattended — the vault's own
`daily-morning-orchestrator` carries a KNOWN CAVEAT saying exactly that about
`/mail-read`. `WebSearch`/`WebFetch` are built-in and need no login, and
westmetall needs neither. Three different things.

It also removed a real hazard: the agent version had a model transcribe 161 rows
per metal out of a rendered table. Nothing in the validator catches 3,182 read as
3,812 — in range, weekday, not future. (Cross-checked afterwards: that capture
was byte-accurate on 322/322 rows. The risk was real, the instance was clean.)

**The column guard is the load-bearing part.** Cash vs 3-month is a genuine
instrument difference — westmetall's own zinc basis averages +84 USD/t — so the
cash column is found by matching its header text AND asserting its position.
Tested against four page mutations; the header check alone caught three, and a
header/data desync silently returned the 3-month, which is why the position pin
exists. All four now refuse.

**Day-delayed, and the date must not be moved.** The newest LME row is T-1.
Dating it T would be a look-ahead bug of exactly the kind the `effective_from`
rule warns about. `westmetall.py` validates and refuses a future date, a weekend,
or a value outside 500–20,000 USD/t.

## The silent-arithmetic bug class — read `docs/SILENT_BUGS.md`

**One failure shape has produced every serious bug here.** Not a crash: a lookup
or a divisor quietly wrong, returning a PLAUSIBLE number, raising nothing,
`coverage_ok` still true. Five entries so far — the GLOB date guard that rejected
every valid date, a hardcoded FX rate 9.7% off, an unregistered unit that dropped
an FX leg (95x), an annualisation by elapsed time that read -52.7% for -5.5%, and
a real price move misclassified as a contract roll.

**Nothing in the test suite caught any of them.** Four of five were caught by a
person thinking "that magnitude is not plausible".

**THE STANDING RULE: fix it AND leave a disclaimer at the site** — a comment
naming the wrong value, the right value, and how it was caught. This class needs
it because the corrected code looks identical to the broken code: nothing about
`n_reported` reads as more correct than `n_elapsed`, so without the note the fix
is invisible and any tidy-up can revert it.

Before shipping new arithmetic, the one question that matters: **if this divisor
were wrong by 2x, would anything complain?** For entries 3 and 4 the answer was
no.

## Gotchas that already cost time

- **Port 8765 is the vault's node dashboard** (running since 13 Aug). We use
  **8770**. A bind clash does not error visibly — the other server answers and
  every request 404s as if routing were broken.
- **FRED rejects a browser User-Agent** (`ECONNRESET`); use `curl/8.0`. Yahoo
  requires the opposite. Do not unify them.
- **SQLite GLOB: `_` is a LITERAL underscore**, not a wildcard (that is LIKE).
  `GLOB '____-__-__'` rejected every valid date and passed every test, because
  the tests only checked that bad rows are REJECTED. Every guard needs an
  ACCEPTANCE test too.
- **Digest filenames are `DD-MM-YYYY.md`** — plain `sorted()` orders by day of
  month. Always sort on the parsed ISO date.
- **Yahoo's `range=max` is NOT daily, whatever `interval=1d` says.** It returns
  `dataGranularity: 1mo` for a long history (370 month-END bars for
  TATASTEEL.NS, 1996-2026) and `1h` for a short one (VAML.NS: 481 hourly bars
  over 69 dates). Both parse cleanly and would store as daily rows. Use an ISO
  date — `fetch_bars` routes it through `period1`/`period2`, which holds `1d`
  out to 31 years — and never trust the interval you asked for: `fetch_bars`
  asserts `meta.dataGranularity` now. Range tokens are daily up to `20y`.
- **Verify a Yahoo symbol by NAME, not instrumentType.** `ZN=F` is the 10-Year
  T-Note. `ZNC=F` is a dead 2019 contract returning a frozen price. `ALA=F`
  reports `ALTSYMBOL` and is perfectly live. Resolve tickers with
  `packages/adapters/yahoo_search.py` — do not guess (VAML.NS cost three wrong
  guesses).
- **VEDL's price history has an unadjusted demerger**: 773.60 → 271.55 on
  2026-04-30, −64.9%. Anything crossing that date compares two different
  companies. `check_corporate_actions.py` scans for this.
- **Windows are CALENDAR DAYS, not row counts.** The store mixes daily equities
  with monthly IMF series; N rows back is N *months* on a monthly series.
- **`.gitignore` needs `data/*` not `data/`** — git will not descend into an
  excluded directory, so a negation can never re-include the staging file.
- **Write commit messages with the Write tool to a file, then `git commit -F`.**
  PowerShell here-strings mangle quotes and `-Encoding utf8` adds a BOM.
- **`.bat` files need CRLF.** Written with LF they fail as `'M' is not
  recognized as an internal or external command` — cmd mis-tokenises `REM`.
  `.vbs` tolerates LF; batch does not. Convert before testing.
- **An unescaped `)` inside a batch `do ( ... )` block closes it early.**
  `echo Stopping (PID %%p)...` dies with `... was unexpected at this time.`
  The vault's `Stop Dashboard.bat` carries this bug. Use `^(` `^)` or no brackets.
- **git-bash's `/usr/bin/timeout` shadows Windows `timeout.exe`** when cmd
  inherits a POSIX PATH, so a `.bat` that works on a double-click throws a usage
  error when tested from bash. The launch scripts call it by absolute path.

## Where valuation inputs come from

| Input | Source | Note |
|---|---|---|
| price | Yahoo `.NS` | daily |
| shares outstanding | Wind `total_shares`, **BSE numeric codes** (`500440.BO`) | validated: computed mcap matches screener to the rupee for all five names |
| net debt | **screener.in**, Borrowings − Investments | agent-fetched, like Wind |
| EBITDA base | cited 1QFY27 prints ×4 | annualises a peak quarter, so runs high |

Wind covers Indian **prices and share counts only** — `pe_ttm`, `pb_lf`,
`mkt_cap_ard` and every balance-sheet field return empty for `.BO` tickers
while a Chinese control works normally. And Wind's `ev` field for Indian names
is **market cap with no debt** — a plausible number that is not what it claims.
Do not use it.

## Wind MCP

The Wind MCP is callable by the **agent**, not by a Python process. So fetch
and load are separate by necessity: the agent calls
`get_wind_historical_data(ZN.SHF, ...)`, writes the result to
`data/staging/`, and an adapter converts and loads it. Staging files are
version-controlled — they are the dated record of what Wind returned.

---

## Where the reasoning lives

**`git log` is the real record.** Every commit message states what was found,
what was fixed, what was deliberately NOT fixed, and why. Read it before
changing anything — many decisions look arbitrary until you see what they
prevent.

```bash
git log --format='%h %s' | head -30
git log -1 --format=%B <sha>       # full reasoning for one change
```

## Current state (2026-08-17, 29 commits)

**All four pillars score and PERSIST.** `pillar_scores` holds 1,000 rows over 40
dates (2026-06-18 .. 2026-08-14), each stamped with spec_version + code_sha.

| pillar | what | state |
|---|---|---|
| P1+P2 economics | margin bridge, EWMA half-life 10d | live |
| P3 valuation | **spot** EV/EBITDA re-marked at current prices, scored on z | live |
| P3 mood | broker actions + policy, gated by breadth | live |
| P4 guidance | commitments + evidence, linear score | live, one entity only |
| composite | 0.45 / 0.25 / 0.15 / 0.15, renormalised over what scored | live |

Latest composite: vedanta 3.90 > hzl 3.62 > nalco 2.73 > vaml 2.37 ≈ hindalco 2.37.
VAML carries the widest pillar spread (mood 3.77 vs valuation 2.03) — which is
why `combined.py` reports spread and not just the average.

**Not built:** `signals` (no directional call with a falsifier is emitted),
`outcomes` (nothing grades them), the in-flavour/out-of-flavour regime gate
(scoped as Flows — `docs/FLOWS.md`), OI as a conviction modifier, book ingestion,
cement's, mining's and EMS's P4 guidance ledgers, and IT / Autos. **Steel IS
built as of 2026-08-25**, **Cement as of 2026-08-28**, **Mining as of
2026-08-29** and **EMS as of 2026-08-30** — see the sections below.

**The gate that still stands.** The backtest exists now
(`python packages/review/backtest.py`) and has been run. It does NOT pass the
gate and does not fail it either — the sample cannot answer the question:

- composite IC vs forward 5-day RELATIVE move: **−0.17**, |t_adj| < 1
- ~7 non-overlapping windows over 5 names in one correlated complex
- leave-one-out: dropping **vedanta** flips every horizon positive
  (h=1/3/5/10 → +0.04 / +0.01 / +0.07 / +0.35); dropping any other name leaves
  it negative

So the negative headline is one name over one two-month window, not a broken
model. Read the CONCENTRATION table before the IC table — that ordering is
enforced in the output because the IC alone reads as a wholesale sign error.

The live hypothesis it produced, which is falsifiable and worth carrying:
**VEDL's valuation pillar may be scoring a holdco discount as cheapness.** It is
the highest valuation score (3.84 mean) while being a holdco whose principal
asset is a 63.4% HZL stake; `holdco_discount_pct` is specified in `zinc.yaml`
as a `market_layer` column that nothing computes. Do not "fix" this on 40 days
of data — it is a hypothesis, not a finding.

**More dates, not more pillars.** The binding constraint is sample size, which
only time supplies.

**This line used to read "the gate stays shut on extending to a second sector".
It was opened by the PM on 2026-08-25 and steel was built.** The sentence is
corrected rather than deleted, because the gate is still UNANSWERED and a reader
needs to know the extension happened without it being cleared. Steel is a
structurally independent replication rather than a bet on an unvalidated model,
and it adds seven names in a less-correlated complex — which is the only thing
that actually shortens the wait for a decidable backtest. But nothing about steel
makes the aluminium IC decidable. **Do not read any composite in this system as
validated.**

## Steel — built 2026-08-25, and what is load-bearing in it

Seven names from the vault's `Coverage/Steel` roster, four peer groups. The split
is per-cost-stack, not per-label, and it was the PM's call:

| peer group | names | economics |
|---|---|---|
| `steel_integrated` | tata_steel, jsw_steel, jindal_steel, sail | live |
| `steel_converter` | apl_apollo | live but uninformative, see below |
| `steel_stainless` | jindal_stainless | **withheld** |
| `steel_secondary` | shyam_metalics | **withheld** |

**THE STRUCTURAL CLAIM, and it is tested rather than asserted.** Iron ore
captivity sets the DISPERSION; coking coal sets the LEVEL. Nobody in India holds
captive coking coal of steel grade, so a seaborne coal move hits all four mills
and separates them only by EBITDA/t — the thin-margin name takes the bigger
percentage hit. Iron ore is the opposite. Run the three shocks in
`specs/sectors/steel.yaml` `validation.test_1_result`:

    iron ore +$20/t   jindal_steel -9.44%  jsw_steel -7.58%   <- material
                      sail         -0.77%  tata_steel -0.42%  <- immaterial
    coking coal +$20  all four material and negative, ordered by EBITDA/t alone
    HRC +Rs2,000/t    mills all positive; SAIL +0.00%, correctly — it is
                      priced off REBAR, not HRC

**THE GATE ON ALL OF IT IS `market_pct` ON THE ORE LINES, AND IT IS MOSTLY
UNSOURCED.** Those numbers produce the entire test_1 result and only ONE of four
is cited in the 48 digests — Jindal Steel's "captive iron ore targeted ~40% by
FY27-end", a forward target rather than today's share. JSW has a 2030 target.
Tata and SAIL have nothing but sector knowledge. **test_1 confirms the arithmetic,
not the inputs.** `tata 0.05` vs `jsw 0.75` IS the flagship pair; if the true gap
is narrower the pair has far less ore content than the spec claims.

**Iron ore carries a `basis_pass_through`, and it must.** Indian mills buy
domestic ore from NMDC at administered prices, not seaborne. The digests show the
two diverging inside one quarter — "costlier iron ore (NMDC hikes +18% QoQ)"
against "Iron ore 62% Fe stable ~$93/t" in the same fortnight. Applying a CFR
China delta at face value would have read ~nothing in a quarter when domestic ore
rose sharply. Currently 0.50 and provisional, like aluminium's coal 0.35.

**THE FX ASYMMETRY RUNS OPPOSITE TO ALUMINIUM.** Steel revenue is quoted in
RUPEES (`hrc_india_inr`, `rebar_india_primary_inr`) while the two biggest cost
lines are in DOLLARS, so in this bridge a weaker INR is pure cost inflation with
no offsetting revenue leg. That OVERSTATES the real exposure — Indian HRC is
priced off import parity and does lift with a weak rupee, with a lag. Do NOT
"fix" it by repointing the output at `hrc_india_usd`; that creates the opposite
error, a full instant pass-through. Handle it on the input side.

**Two things left deliberately broken, both named in the specs rather than
patched:**

- **APL Apollo nets to -0.73% on an HRC shock.** Output and input are both linked
  to `hrc_india_inr` at ~1:1, so the bridge calls it HRC-neutral when the digests
  plainly say rising coil hurt it. That is the correct output of a wrong
  structure: a converter earns the tube/coil SPREAD and no tube price series
  exists. Same gap that put Novelis on a placeholder. Its valuation and mood are
  real.
- **SAIL's whole 16.64mt sits on one rebar line**, making it the highest-beta name
  by construction (+35.6% of EBITDA on the live window). SAIL is roughly half
  flat product. Fix by SPLITTING the line at the true flat/long ratio, both legs
  summing to 16.64mt — cutting the tonnage instead breaks the volume/base_ebitda
  basis rule. The ratio is a PM number; no digest gives it.

**The denominators are more trustworthy here than in non-ferrous**, because steel
brokers quote EBITDA/t against a disclosed tonnage, so the absolute print is
independently checkable. It reconciles on all five names where both were cited
(e.g. SAIL 4.16mt x Rs9,974 = Rs4,149 cr vs cited Rs41.5bn). The computed
multiple also lands on the sell-side's own: SAIL 6.26x against Avendus quoting
"6x EV/EBITDA".

**VOLUME AND `base_ebitda` MUST STAY ON THE SAME BASIS.** Every steel volume is
the cited quarterly tonnage x4, matching its x4 denominator. SAIL is the trap:
the digests cite an "FY27 volume guide 22-22.5mt" which is 34% above its
Q1-annualised 16.64mt and is probably crude steel PRODUCTION rather than saleable
SALES. Swapping the guide into the volume field while leaving the denominator at
Q1-annualised inflates every `pct_of_ebitda` by 1.34x with nothing raising.

**net_debt does NOT use the screener convention here.** See `SILENT_BUGS.md`
entry 7 — `Borrowings - Investments` overstates it on every steel name with a
cited figure and flips the sign on APL Apollo. Steel uses digest-cited 1QFY27
figures. SAIL is the exception with no cross-check, flagged
`convention_upper_bound`; read its 6.26x as a ceiling.

**Two dead columns in the metals pack, both visible on the Steel tab.**
`scrap_turkey` last printed 2020-12-03 and `coking_coal_contract_qtr` 2022-06-27.
Neither is an adapter bug — `read()` correctly drops `#N/A` strings and
non-positive values, so the zeros that follow never loaded. They are columns the
broker stopped populating. `scrap_turkey` is why a stainless bridge is not
possible. `coking_coal_spot_aus` (col 10) is live and is the one that matters.

## Cement — LIVE 2026-08-28: four scored names, three pillars

The spec landed the day after the prices did: `specs/entities/cement.yaml` +
`specs/sectors/cement.yaml`, zero new engine code except one generic line (see
below). Four scored of nine — ultratech, ambuja, shree, dalmia are the F&O
names; jk_cement, ramco, nuvoco, star_cement, jsw_cement are `peer_group: null`
per invariant 7 (the vault's OI fetch records `status: not_in_fno` for all
five). ONE peer group where steel needed two: the four share a cost stack and
differ by REVENUE REGION, which lives on the entity output lines.

**THE STRUCTURAL CLAIM, tested not asserted (validation in the sector yaml):
regional mix sets the dispersion; fuel sets the level.** A petcoke +$20 shock
hits all four, ordered by EBITDA/t (ambuja -12.3%, shree -5.6%); an EAST-only
-Rs200/t reaches shree at 0.35 of volumes and dalmia at 0.45 while ultratech
and ambuja correctly read ZERO through all-India links. Every denominator is a
cited Q1FY27 print that reconciles to the rupee against volumes x EBITDA/t
(e.g. UltraTech 41.3mt x Rs1,214/t = Rs50.14bn vs the cited Rs50.2bn).

**The bridge grew ONE generic knob for this: outputs now honour
`basis_pass_through`** (default 1.0 — steel regression-verified bit-identical
before anything else was built). Cement's only output series is RETAIL incl.
GST while EBITDA earns NSR (~0.74x the level, empirically and by GST
arithmetic), so every cement output line carries 0.75. The Oct-2025 -9.4%
retail print is the Sep-2025 GST cut (28->18%) passing through — the mechanism
behind the move the PM confirmed as genuine.

**The fuel intensities are DERIVED but double-anchored**: ~750 kcal/kg clinker
x 0.65 clinker factor / 8,200 kcal/kg petcoke GCV = 0.060 t/t, and the check
that makes it believable is that Shree's cited "fuel cost to Rs1.95/kcal (from
Rs1.6)" reconciles exactly with Nomura's cited petcoke USD147/t (= Rs1.59 per
1,000 kcal). Per-company deviations only where cited: UltraTech's green power
47% halves its coal line; Shree's petcoke-to-domestic-coal switch (West Asia
disruption) halves its petcoke and adds a 0.50-basis domestic coal line —
TRANSIENT by management's own framing, restore when contracted petcoke resumes.

**What is NOT live: P4 guidance** — no cement guidance ledger exists, so it
withholds for all four, honestly. The raw material is in the digests (Ambuja
"FY27 vol +8%", UltraTech "cost +Rs130-140/t qoq peaking 2Q", Dalmia "~67mtpa
by 3QFY28"); building `specs/extracted/cement_guidance.json` is the next step,
steel's playbook. Ambuja's volume guide is the SAIL-class trap-in-waiting: it
guides +8% while running -7% on a deliberate "value over volume" share cession.

**Mood carries a known bias worth a PM decision**: the extractor classifies
"maintains SELL, FV raised (rollover)" as tp_change/+1 at half weight. Kotak
rates the ENTIRE cement pack SELL/SELL/SELL/REDUCE while rolling FVs forward,
so cement mood reads more positive than the house view it summarises
(ultratech +17/-1). Same calculus steel lives with — changing it changes
steel's stored mood too, so it is flagged rather than patched.

**Extraction traps recorded in the extractor itself**: #JKCement bullets that
are actually about JK LAKSHMI (guarded in named_in), "ambuja" resolving to
GUJARAT AMBUJA EXPORTS and "dalmia bharat" to DALMIA BHARAT SUGAR on Yahoo
(the search-first rule paid twice), and no bare #Cement tag (sector, not
entity).

## Cement prices — the feed underneath (landed 2026-08-27)

`adapters/cement_pack.py` reads the **Daily Cement Pack**, the second attachment
on the same Kotak mail that carries the metals pack. Six regional series —
North / Central / East / West / South / all-India — **monthly, 2014-04 to
current, in Rs per TONNE**, source `cement_pack`. The Cement tab is 11/11 priced:
those six plus `cp_coke`, `thermal_coal_seaborne`,
`thermal_coal_indonesia_6322`, `brent`, `usdinr`, which were already landing.

**THE OCTOBER 2025 DROP IS GENUINE. DO NOT "CORRECT" IT.** PM ruling
2026-08-28. All-India fell **−9.37%** in one month, every region −8.0% to
−11.4%, and it is by a wide margin the largest monthly move in 149 months — the
next-worst is −4.43%, and October's own seasonal mean is −0.16%. Every property
that normally marks a rebasing is present, which is exactly why this note
exists: **it was a real price collapse.** No cleaning, no exclusion window, no
allow-list entry. A P1 window spanning Sep→Oct 2025 SHOULD book it. Treat this
the way `tape.py`'s `CONFIRMED_ACTIONS` treats the eleven 15% equity jumps that
are real market moves — marked, understood, and drawn through.

**"Daily" is the MAIL, not the prices — and the distinction is the whole shape
of this feed.** Region-wise: yes, five regions plus all-India. Daily: **no.** The
2026-08-27 and 2026-08-28 captures were diffed and the price sheet is identical
to the fourth decimal — all six regions, all 149 months, the in-progress August
column included. Cement is the only sector here whose OUTPUT price is monthly
while its whole cost stack is daily, which is why the pack is worth re-reading
each morning anyway: **the cost side moves every day and the price side does
not.** Consequence for the load: an unchanged current-month value is left on its
stored date rather than re-stamped, per `series.py` rule 2 and invariant 3. The
current-month column's true update cadence is still unknown — two adjacent
captures cannot separate weekly from month-end — and the load does not need to
know, because it keys on the value rather than a predicted schedule.

**The M365 connector works for THIS pack and not for the metals pack.** Worth
stating plainly because `outlook_pack.py` exists entirely to route around that
connector, and a reader who knows why will assume the same is needed here. The
metals pack is TALL — ~4,760 dated rows — so the ~200k-character cap truncates it
to the oldest ~830 and it never contains today. The cement pack is **WIDE**:
dates run across the columns, the whole four-sheet workbook is 336 lines and
~70k characters, and the connector returns all of it, current to the morning.
One `read_resource`, ~18k tokens, no Outlook automation.

**Rs/TONNE, and the 20x is done in the adapter on purpose.** The sheet prints
Rs per 50 kg bag. `bridge.py` multiplies an ABSOLUTE delta by a tonnage, so a
Rs/bag delta against a tonnage basis understates the revenue leg by exactly 20x
with nothing raising. Rs/t also matches steel's `hrc_india_inr`. The staging
`.tsv` keeps the source's own Rs/bag numbers; divide by 20 to get back.

**Three traps in the sheet, all live, all guarded:**

- The **date convention changes mid-series** at 2019-10-01 -> 2019-11-30: the
  first 67 columns label a month by its first day, the rest by its last. Every
  column is normalised to (year, month).
- The **current month is a month-to-date average**, so stamping it at month end
  writes a FUTURE row — which moves `bridge.py`'s default `as_of` for every
  pillar, silently. This one actually happened; it is entry 8 in
  `SILENT_BUGS.md`. Stamp is `min(month_end, capture_date)`, capture date read
  from the staging filename, future dates refused.
- The **six region labels appear three times** — levels, then mom change, then
  yoy change. A naive label match loads DELTAS as levels. Guarded twice: first
  block only, plus a Rs/bag plausibility range a delta block cannot pass.

### The pack lands ~15 days late — hence the IndiaMART watch

PM figure, 2026-08-28: the pack's prints are about a **fortnight** behind. Cement
takes its hikes at the start of a month, so by the time the priced series carries
a move it is no longer tradeable. `adapters/indiamart_cement.py` exists only to
close that gap, and its scope is deliberately narrow: **a warning on the Overview
tab when several regions move together.** Nothing else.

**IT WRITES TO `cement_watch*`, NEVER TO `prices`, AND NO PILLAR READS IT.** Three
reasons; the third is the one that would actually bite. These are marketplace
ASKS, not transactions. They carry no date, so they fail the citation standard.
And `prices` is where the CLOCK comes from — a daily-printing scrape would let
`latest_daily_date()` hand an IndiaMART ask the as_of for the whole book, which
is SILENT_BUGS 8b invited back in through the front door.

**The signal is a MATCHED-PAIR median, not a median.** Listings carry stable
product ids in their URLs, so the reported move is the median % change across
only the ids present in BOTH captures. A plain median of a marketplace page moves
when the PANEL churns, which it does all day for commercial reasons that have
nothing to do with cement. `n_matched` is printed beside every figure: 0.0%
across 40 matched listings and across 4 are different statements.

**Two limits that are not fixable by better parsing.** Every rupee figure on the
page comes from the promoted "Best Sellers" rails — the visible result grid says
"Request a quote" and carries no price — so this samples PROMOTED inventory, a
selection the tool cannot see around. And there is **no date anywhere in the
HTML**, so there is no history: the series starts the day capture started
(2026-08-28, 1,032 listings across 5 regions) and can never be backfilled.

**Thresholds are UNCALIBRATED on purpose and the page says so.** With no history
the panel's own day-to-day noise floor is unknown, and a number guessed now would
either scream every morning or never fire. `--report` stays in CALIBRATING mode
for 15 captures, printing moves and refusing to alert; the Overview renders that
state as a quiet line rather than nothing, because an absent banner would read as
"no move" — the one thing it does not mean. `python packages/adapters/
indiamart_cement.py --selftest` covers eight alert scenarios including the
non-firing ones.

### The store clock changed because of this, and it is not a cement detail

**`as_of` is `series.latest_daily_date()`, never `MAX(date) FROM prices`.** PM
decision 2026-08-27: **a coarse series contributes its SHOCK but must not set the
CLOCK.** Removing cement's future dates fixed the symptom and left the cause —
even correctly stamped, the in-progress month sits on the capture date, so
`MAX(date)` runs a day ahead of every equity close and `run_scores.py` would
stamp a score a day after the prices it is built from, against 1,407 stored
dates where the two agree. Five call sites now go through the helper:
`run_scores`, `bridge`, `mood`, `mood_bias`, `whatif`. `iron_ore` (FRED monthly)
was always coarse too and had simply never been the max.

**Cadence is MEASURED, not listed** — an explicit list of monthly ids is right
today and silently wrong the first time somebody adds one without reading this,
the same maintenance failure as an unregistered unit or price source. And it is
the **median** gap over the last 8 prints, not the last gap: on 1 September the
cement series holds 2026-08-31 and 2026-09-01, one day apart, so a last-gap test
calls it daily on exactly the month seam. `python packages/core/series.py
--selftest` tests both directions — the seam and a daily series with a long
holiday break that must NOT be demoted.

**The level disagrees with Nomura's and it is NOT resolved.** Kotak reads ~353
Rs/bag all-India in Jul-Aug 2026; Nomura's channel checks in the digests read
~321-326 and say explicitly "trade prices". ~9% apart, so they are probably
trade versus a trade/non-trade blend. Deltas are what reach a score, so a
constant offset is harmless — a divergence in the CHANGES would not be. The ids
claim no basis (`cement_price_<region>_inr`), per invariant 6.

**What the spec still needs, and one thing it must decide.** Peer groups,
volumes, `base_ebitda`, intensities, `market_pct`. The decision with no default:
**which regional price each company sells into.** Cement is a regional market in
a way steel is not — a South-heavy name and a North-heavy name do not share an
output price, and the pack gives all five regions precisely so that can be a spec
choice rather than an all-India fudge. The digests support it: on the August
print East ran -2.25% m/m and North +0.36%.

**Three sheets are captured but NOT loaded**, listed so "read" is never mistaken
for "loaded": `Volumes` (DIPP all-India monthly production, '000 t, back to
2004 — a demand indicator, and putting a tonnage in `prices.close` would let the
bridge shock it), `Valuation` (Kotak's comparative table incl. **EV/ton of
capacity** and EV/EBITDA — real P3 inputs, and valuable because Wind returns
empty for every Indian fundamental field), and `Stock`. Unlike the metals pack
there is no urgency: the connector returns the full history every morning, so
"a series you are not capturing today is history you cannot recover" does not
apply here.

## Mining — LIVE 2026-08-29: three scored names, volumes as a driver

PM instruction verbatim: NMDC, Coal India, Hindustan Copper; leave Lloyds
Metals; NMDC gets the effect of volumes every month; Coal India gets volumes
plus e-auction premiums in economics; Hindustan Copper is normal. Two peer
groups — `mining_bulk` (nmdc, coal_india) and `mining_copper`
(hindustan_copper, a singleton). **Hindustan Copper is NOT in F&O and is
scored anyway — that is the PM's explicit invariant-7 exception, recorded in
`specs/sectors/mining.yaml`. Cash-only expression: long/avoid signal, never a
pair leg.** Zero new engine code, zero placeholders.

**THE STRUCTURAL CLAIM, tested not asserted: THE DRIVER SETS THE NAME.** The
inverse of steel/cement — these three sell on three unrelated mechanisms
(NMDC's own circulars, CIL's SWMA auction premium, LME copper), and the
validation runs in the sector yaml show every shock reaching exactly one
name. The interesting spread is cross-sector: an NMDC cut is NMDC-negative
and JSW/JSPL-positive on external ore.

**VOLUMES ARE A SCORED DRIVER — the first sector where they are.** Both PSU
miners file monthly. The mechanism costs no engine code: a `volume_effect`
output line links a trailing-12M volume series and carries EBITDA-per-mt in
its `volume` field, so the ordinary volume x delta arithmetic yields
EBITDA/t x d(TTM tonnes). TTM is what makes monthly prints meaningful — its
MoM delta is exactly the YoY monthly increment, so monsoon seasonality
cancels. And the TTM needs NO history chain: every filing carries this-year
AND last-year, monthly AND FYTD, so `TTM = FY_prev_total + (FYTD − FYTD_LY)`
comes out of ONE document; gaps cost a point, never the months after it.
Scale: the 60-vs-55mt NMDC FY27 guidance debate is worth ~10.7% of its
EBITDA through this line; CIL's July print alone stepped TTM offtake
751.5 → 761.5mt (+17.4% YoY month).

**E-auction premiums enter as a REALISATION SERIES, and they are material.**
`coal_eauction_realisation_inr` = notified base Rs1,614/t x (1 + the SWMA
filing's CIL-total monthly premium). The base is derived (Kotak's cited Q1
auction realisation Rs2,321/t / 1.438) and the 1.438 is CROSS-VALIDATED: the
SWMA Q1 volume-weighted premium computes to 43.8% against UBS's independently
cited "avg 44% in Q1". A 10pp premium month = ±3.4% of CIL EBITDA; the
observed 2026 range (33%..51%) spans ~6%. The FSA leg (87% of tonnes) sits on
a derived Rs1,487/t series that moves only on a notified hike — deliberately
near-static, `kind: manual` in freshness so it never false-alarms.

**THE NMDC BASIS BREAK — the catch that would have been a fake price crash.**
NMDC's circulars changed basis on 2026-01-09: through Nov-2025 the FOR prices
are INCLUSIVE of Royalty+DMF+NMET, from Jan-2026 EXCLUSIVE. Naively loading
the sequence books a ~Rs1,000/t January "cut" (5,600 → 4,600 lumps) that is
~18% basis redefinition — consistent with 15% royalty x 1.32 gross-up.
`mining_filings.py` parses the basis SENTENCE of every circular and refuses
anything not `ex_royalty`; the series therefore starts 2026-01-09 and the
eight older circulars are recorded-and-refused, not loaded. Corollary: the
royalty input lines (intensity 0.25 of the price move, both legs) are
anchored by the same wedge plus JPM's royalty/t prints.

**Data plumbing, and where the upkeep seam is.** `adapters/mining_filings.py`
(--fetch/--load/--selftest, both steps in refresh.py) parses coalindia.in's
monthly production/offtake and SWMA pages — timely, ~1st of the month — and
NMDC's CMS API (the Angular bundle's own public read key). **The NMDC website
lags ~6 months**, so NMDC's recent months and price changes are hand-entered
into `specs/extracted/mining_prints.json` from the digests, every row
source-noted; the fetch picks the site back up whenever it catches up, and
identical months overwrite at equal rank ("filing", 40, registered in
prices_io). Two CIL months (May/Jun-26) are OCR scans the parser refuses —
hand-verified into staging with digest corroboration recorded. THE MONTHLY
CHORE: when a digest carries NMDC's monthly print (~2nd-4th) or a circular,
add the row to mining_prints.json; CIL needs nothing.

**Extraction traps, both measured live in the corpus before writing the
patterns:** bare `\bHCL\b` is HCL TECHNOLOGIES all 11 times (the bare-JSW
class — Hindustan Copper matches full name and tag only), and `\bNMDC\b`
matches inside "NMDC Steel" (NSLNISP, the demerged plant — also the "NSL"
whose receivables sit on NMDC's book), guarded by a named_in() sentence
disqualifier, the JK Lakshmi mechanism. Yahoo search paid again too: a bare
"NMDC" query returns NMDC STEEL first, so the CANDIDATES pattern requires
"nmdc ltd".

**Financials are primary-sourced** (HC has ZERO broker coverage in 47
digests — 7 mentions, all tags): BSE-filed results/AR/decks, all three share
counts cross-checked to BSE's MktCapFull to the rupee, net debt on the
borrowings-minus-cash convention (all three are NET CASH; CIL by Rs38.5k cr,
the biggest in the book). Three caveats that will bite if forgotten: NMDC's
base quarter is ~10% flattered by a Rs10.9bn inventory build (Elara adj
24.7bn vs Kotak 27.4bn — the volume line watching the sales ramp is also
watching this unwind); CIL's FY27 EBITDA prints are basis-fights after an
accounting-policy change (anchor on EBITDA/t ~Rs610 and volumes); HC's
2,032 cr base annualises a 54%-OPM quarter at record LME copper, and a
fund-raising intimation (BSE 27-08-2026) may dilute the share count.

**P4 withholds for all three** (cement-consistent; weight 0.00). The next P4
step writes itself: NMDC's 60mt guide and Emkay's 815mt CIL model graded
MONTHLY against the TTM series. Mining's mood: NMDC scores (fresh mid-Aug
actions), CIL's results-week actions have decayed past the half-life, HC has
nothing to extract — all three states honest.

## EMS — LIVE 2026-08-30: the first non-commodity sector, forward P/E leads

PM instruction verbatim: "Dixon, amber, kaynes, PGEL. Find a method to score
them, they are harder to track btw ... along the lines of 1 year fwd P/E
valuations majorly and maybe some commodity prices. Self-valuation score is
fine, other economics is difficult, if you can, figure something out." One
peer group — `ems_assemblers`, all four F&O — plus syrma_sgs and avalon at
`peer_group: null` (not_in_fno), tracked not scored. Zero schema changes.

**THE METHOD: P3 forward P/E is the lead pillar; economics and guidance
withhold by construction.** These are converters (the APL Apollo finding at
sector scale) whose binding cost drivers per the digests — copper-clad
laminate, resin — have NO price series at all, so a margin bridge would run
on unsourced intensities and carry economics' 0.50 composite weight. The
composite renormalises to valuation 0.60 / mood 0.40 effective. "Maybe some
commodity prices" landed as the tab's context panel (copper, aluminium, HRC,
brent, USDINR), linked from no spec.

**THE DENOMINATOR IS DATED CONSENSUS, NOT A YAML CONSTANT.**
`adapters/yahoo_estimates.py` captures Yahoo's earningsTrend daily (crumb
dance, stdlib-only, unattended-safe) into `estimates` — the table that had
the right shape and zero rows since the schema was written. NOT into
`prices`: anything there becomes bridge-shockable (`_series_in_store()`),
the reason cement_pack refuses to load its own Valuation sheet. The feed was
cross-validated against the digests before being trusted: PhillipCapital
"35x FY28E EPS of Rs16" vs Yahoo PGEL FY28 16.56; CLSA "52x FY28 PE" vs
Dixon FY28 281.6 at CMP. Both agree.

**THE SCORE: since 2026-09-05, a 50/50 BLEND of the peer PEG ratio and an
own-recent z.** raw = 0.5·ln(PEG/group median)/ln(1.5) + 0.5·z_own, in
anchor units (1.0 = the act threshold on either component), hill k = 1.0.
The original raw was the peer ratio alone, and its first week demonstrated
its blind spot: the four names sold off -2.6%..-8.8% as a block, every
common move cancelled out of ln(PEG/median), and the PM saw "4 straight
lines". z_own is each name's fwd P/E against its OWN last 60 days
(OWN_WINDOW_DAYS), sd floored at 0.5% of the window mean so a quiet window
reads "no signal" rather than z=30; a window under 15 points falls back to
the peer ratio alone, flagged in detail.raw_basis. W_REL/W_OWN in
valuation_pe.py is the PM's volatility knob.

**Where z_own's history comes from — the reason it is honest.** Long
own-history is still IMPOSSIBLE (no free source carries historical
consensus; realised-EPS reconstruction is look-ahead), but every capture
carries Yahoo's own 7/30/60/90-days-ago estimate values — point-in-time
records, dated by offsetting the capture date. Those anchors plus the daily
captures give a stepwise forward-EPS series reaching ~90 days before the
first capture; closes over it give the daily fwd P/E window. **The blend
RE-RANKED the group on day one, and that is the design speaking:** Dixon's
upgrade cycle (+10% estimates, 90d) makes it CHEAP against its own window,
lifting it from the group floor (2.29 old raw) to mid-pack (~3.5); Kaynes,
which de-rated after its cuts, took the floor. PEG = (close/FY2 EPS)/growth
per the 2026-09-03 desk convention (numerator and denominator describe the
same year). The full own-history variant at ~120 captures remains the
recorded intent in `specs/sectors/ems.yaml pillar_3.reference`.

**`pillar_3.metrics` FINALLY HAS A READER.** It was written in all five
sector specs and consulted by nothing (the `effective_from` shape).
`run_scores.py` now dispatches on it: `pe_forward_peg` routes to
`valuation_pe.scores_for_group()` (group-at-once, memoised — a relative
score does not exist name-by-name); everything else takes the EV/EBITDA
path untouched.

**Consequences to expect, stated before they are noticed:** a name's score
can move on a PEER's revision with no news of its own (peg_median and
n_group ride in detail); the strict capture<=as_of rule (westmetall T-1
logic) means the live score uses a consensus ~1 trading day behind the
close, and the first scored date was the first capture date (2026-08-29);
and PEG treats consensus growth as deliverable — Kaynes reads mid-group
partly BECAUSE its E was cut 19% in 90 days. That is why **rev_90d
(estimate-revision momentum) rides beside the score everywhere but inside
it nowhere** — Yahoo's own 90-days-ago fields make it computable from one
capture. First cross-section, 2026-08-29: amber P3 3.87 (PEG 0.66, revisions
-5%), pgel 3.40 (0.79, -19%), kaynes 2.67 (1.07, -19%), dixon 2.29 (1.22,
**+10% — the only name being upgraded**, the Vivo JV). The score and the
momentum disagree on Dixon by construction; hiding either half would make
the other read as a verdict.

**Gates (withhold rather than guess):** capture older than 30d (the feed is
daily; older means it broke), fewer than 5 analysts on FY1 (PGEL sits at 7 —
the floor is under it deliberately), FY1/blended EPS <= 0, growth < 5%
(PEG explodes as g->0), fewer than 3 computable names (a median of two is
just the other name — one gated name can therefore withhold the GROUP).

**Extraction traps, measured live before writing patterns:** bare \bDixon\b
(65), \bAmber\b (53), \bKaynes\b (32), \bSyrma\b (7) are all clean in the
corpus; "PG Electroplast" appears ZERO times — #PGEL is the entity's only
handle. The "Avalon" in aviation bullets is the AIRCRAFT LESSOR (the
18-08-2026 digest flags it itself) — named_in() disqualifier on
leaseback/lessor/Akasa/aircraft. "amber flag/light" is guarded pre-emptively
(re.I matching; zero corpus hits today). #EMS (33) is a sector tag, never an
entity.

**Two registries were already one sector stale when found** — mining had
been added to neither `mail_watch.KEYWORDS` nor `valuation_pairs.GROUPS`.
Both fixed with the EMS pass; ems is deliberately ABSENT from
valuation_pairs (it replays EV/EBITDA z-history, which EMS does not use).

## What P1 is — settled 2026-08-18

**P1 is an isolated SHORT-TERM score. It is not a forecast and must not be
presented as one.** The PM's decision after the test below. Treat it as the
standing definition, not an open question.

Tested per company against its OWN price — no pairs, no ranking — weekly over
268 observations, 2021-2026:

| | corr. with move ALREADY PAST | corr. with move STILL AHEAD |
|---|---|---|
| nalco | +0.40 | +0.14 |
| hindalco | +0.50 | +0.16 |
| hindustan_zinc | +0.39 | +0.11 |

Forward correlation decays to ~0 by 13 weeks (hindalco +0.006). Bucketing
forward 13-week returns by score level is non-monotonic: the TOP bucket is never
the best of four for any name, and for hindustan_zinc it was the worst.

**Nothing in the bridge needs fixing.** P1 is built from PUBLISHED commodity
prices; the shares are priced off the same prices and react within days. A score
derived from public data can be faithful but never early. **Do not try to lift
P1's predictive power by retuning the curve, the EWMA, the anchor or the
weights** — the ceiling is informational, not parametric.

Two fixes that made the score MORE CORRECT without making it more predictive
(which is itself the evidence): repricing zinc off LME, and pricing cp_coke at
all. HZL's score dispersion doubled, sd 0.293 -> 0.588, and its lead/lag ratio
did not move.

### Where a forecast could come from instead

The part of the model that is genuinely private is the part that never varies:
tonnage volumes, contracted realisations against spot, when cost positions were
locked, captive-supply share shifting. All four sit in the specs as fixed
`verify: pending` constants.

They are one change, not four: **turn a static parameter into a dated, sourced
series and have the bridge read it as-of.** `effective_from` is written in every
spec and read by NO scoring code today.

**The data does not arrive as a feed.** Every automated source here is a price
feed; these four come from disclosures — quarterly production filings, concalls,
capex announcements — at quarterly cadence, event-driven. That is the extraction
layer doing the job it was designed for, not a new adapter.

**They belong in the `economics` table, not in YAML.** It already has
`effective_from`, `source_note NOT NULL`, and a CHECK rejecting an intensity with
no provenance. `db_state.py` labelled it "superseded" — that was wrong and is now
corrected; the label held only for STATIC intensities. YAML keeps the structure
that never changes (which lines exist, what each is priced off); the DB holds
what the value was, when, and who said so.

**The rule that decides whether any of it is honest: a dated value carries the
date the market COULD HAVE KNOWN it, not the date it physically happened.** Q2
production is about July-September and knowable in October. Utkal starting in
October but announced in July dates from July. Get this backwards and the
backtest improves beautifully and means nothing. `source_note NOT NULL` is what
forces it to be written down.

### Testing decisions, so they are not relitigated

- **Weekly beats monthly, and take the week's CLOSING print, not its average.**
  Averaging lost on every row (13w hold, 1:1 MV: 53% / +0.86% vs 55% / +1.29%) —
  a weekly mean is centred on Wednesday, adding lag to a signal already built
  from a difference of levels.
- **13 weeks is the horizon.** 2-week holds are below a coin flip (48% at 1:1);
  the edge peaks at 13w and decays past 18w.
- **Always report 1:1 market value beside 2:1.** A 2:1 book is 33% NET LONG. In
  a rising tape it flatters everything: across every cut, 2:1 lands 60-65% and
  1:1 lands 52-56%. The gap is market exposure, not skill.
- **Only 2021+ is testable.** Every spec is `effective_from: 2026-04-01`.
  Hindalco's Mahan/Aditya smelters ramped 2013-16 and tripled its Indian
  capacity; nalco-on-hindalco beta ran 0.28 (2015) to 1.14 (2026), r2 0.10 to
  0.60. Earlier tests measure a structure that did not exist.
- **The vault's four-regime model does not survive.** 74% came from testing
  inside one aluminium bull market; over 170 months it is 52% at 2:1, ~50% at
  1:1. Ported as `packages/review/regime_pairs.py`.
- **The zinc pair has no P1 signal by construction.** hindustan_zinc and vedanta
  score within 0.001 of each other (sd 0.041) — same two revenue lines, VEDL's
  scaled by the 63.4% stake, which `pct_of_ebitda` divides straight back out.
  What separates them is holdco discount, i.e. P3. Do not rank them on
  economics.

**Numbers still provisional** (`verify:` in the specs): all intensities, NALCO's
alumina surplus tonnage, VAML's alumina `market_pct`, the coal
`basis_pass_through` of 0.35, Hindalco's `base_ebitda` (the only unsourced
denominator), VAML's derived share count, and VEDL's **pre-demerger** net debt —
which makes VEDL read dearer than it is, so its cheapness survives its own bias.

## The Company tab — transcript-only coverage, added 2026-09-14

PM instruction: *"a 'company' tab in the frontend, an option to select
companies, put avalon in it... The end goal is to have a past coverage and
analysis of the stocks in such a manner that I can read and get done with the
company in depth."* First name: **Avalon Technologies (AVALON)**.

**It scores NOTHING and must not.** Avalon already sits in `specs/entities/
ems.yaml` with `peer_group: null` (not in F&O, invariant 7). This tab is a
READ surface: `data/companies/<slug>/dossier.json` → `engine.company_view()` →
`/api/company` → `#s-company`. No adapter writes to it, no pillar reads it,
nothing here reaches `pillar_scores`. Putting a transcript-derived revenue
number anywhere a bridge could shock it is the `estimates`-vs-`prices` lesson
again.

**The universe is the folder list**, deliberately — `company_list()` globs
`data/companies/*/dossier.json`. Adding a company is a directory, never a code
change, and a sector shows the sub-tab only when it has a dossier. The
one hardcoded company list in this system is the thing that would rot first.

**Since 2026-09-16 it is NOT a top-level tab.** PM: *"The company analysis
sits out like a sore spot. Keep it within the sectors only, add a tab within
each sector that says company historical analysis."* So each sector's
sub-view bar carries a **Company historical analysis** tab, shown only where
that sector has at least one dossier, and the picker offers that sector's
names alone. Every dossier carries a top-level **`sector`** key (an
`engine.SECTORS` id) — the skill writes it; `_company_sector()` falls back to
the entity spec matching `tickers.nse`, then the `entities` table, and an
unresolved dossier is reported as `company_unplaced` on the nav and on every
Company sub-tab rather than dropped. Coforge needed the explicit key: IT has
no specs and the `entities` table holds no NSE symbols. `/api/company` takes
`sector=`; a slug outside that sector falls back to the sector's first name.
A non-scoring sector (IT) used to hide the sub-view bar entirely; with a
dossier it now shows the bar with the three scoring tabs hidden and its panel
reachable as Prices & Watch. Three dossiers today: Tata Steel → Steel, Avalon
→ EMS, Coforge → IT.

### What 14 transcripts actually produce

Q4FY23 (29-May-2023, the inaugural call) through Q1FY27 — **14 consecutive
quarters, no gaps, and that is the entire history**: Avalon listed in April
2023, so 14 is not a sample, it is the population. Downloaded from
screener.in's concall list, which links the company's own IR PDFs; plain
`urllib` with a browser UA, no login, ~122k words, all digital (no OCR).

Ten of fifteen module keys populate from transcripts alone. **Five cannot and
are rendered as named holes with the document that closes each** —
shareholding (needs the pattern filing), market share (needs peer revenues),
ratios and valuation (need a screener export), stock (needs prices). An
absent module reading as "nothing happened" is the one thing it must not do.

### The guidance ledger is the product, and it leads the page

20 commitments harvested and graded: **7 miss, 6 beat, 1 met, 6 pending.**
Sorted misses-first, because a scoreboard that opens on the wins is a
brochure.

**A revised commitment is graded against the ORIGINAL.** FY24 was guided at
+25-30% revenue and 12-13% margins, cut to 15-25% one quarter later, then to
"lower end with a negative bias", then replaced with a **degrowth** forecast of
8-10%. It landed at **-8.2%**. That is a MISS, and the note says so explicitly:
*miss against the original commitment; met against the guidance as reset.* A
company that resets guidance does not get to launder the first promise.

The mirror image is FY26: guided 18-20%, **raised four times in four
consecutive quarters** (23-25 → 28-30 → ~40) and still beaten at +46%. Same
management, same disclosure habit, opposite sign. Both rows are on the page
and neither is smoothed.

### The finding that no single call contains

**US manufacturing share went 28% → 11% → 28%.** Management drove it down
deliberately (stated target 85:15 India:US, ~55-60% of US customers approved
for transfer to India), reached 11% by Q2FY25 — and then it climbed back to
28% by Q1FY27 as new US wins landed. **At Q1FY27 the target was quietly
restated as "naturally settle at around 20%".** The reversal is never named as
a change of plan on any call; it is only visible as a series. That is exactly
what a 14-quarter dossier is for, and it renders as an amber callout above the
table rather than as a row you have to notice.

### Flags are rendered, never resolved

Three unreconciled conflicts are carried on the page rather than silently
picked, per the silent-arithmetic rule:

- **FY23 revenue is PRINTED in the transcript as "INR44.7 crores"** against a
  Q4 of 272 and an FY23 gross margin of 338 — impossible. Reconstructed as
  ~944.7cr and flagged; module 9 must take it from a filed statement. A second
  instance on the same call prints FY25 capex guidance as "FY'20".
- **Q1FY24 gross profit is 77cr on its own call and 80.0cr when the Q1FY25
  call restates it** (32.8% vs 34.0%).
- **June-24 net working capital is 156 days on the Q1FY25 call and 163 days
  when Q1FY26 restates it.**

None is large enough to change a view. All three are the shape that is.

### Plumbing

`/data/companies/...` is a read-only passthrough in `serve.py` so a citation
links AT the filed PDF and its long-form note; it is confined to
`data/companies/` **after** normalisation, and `.md` is served as `text/plain`
so a note opens in the tab instead of landing in Downloads. Refusal and
acceptance are both tested (200 on the pdf and the note, 404 on a traversal
and on a missing file) — per the GLOB lesson, a guard needs both.

Company data lives under gitignored `data/`, like the book. The 14 PDFs are
7.2MB and are not in git; the dossier is regenerable from them.

**`engine.REPO`, not `ROOT`** — there is no `ROOT` in engine.py, and the first
draft of this used it. Caught immediately because the module would not import;
noted because the two names are interchangeable everywhere else in the repo.

### The narrative layer — added same day, and it changed what the tab is for

PM, on seeing the module dossier: *"I wanted some sort of a backward looking
company analysis over the history of the company. Any relevant news and
earnings or raw material change can look to somehow explain the share price
movement."* So the **price became the spine** and the modules were demoted to
reference beneath it. `narrative` in the dossier; `companyNarrative()` renders
first.

Avalon listed 2023-04-18 at INR398 and closed INR2,264.50 on 2026-09-11 —
**+468.9% over 846 sessions**, high INR2,373.30 on 2026-08-31. The history is
cut into **eight chapters bounded by turning points in the price**, never by
the calendar, each carrying what happened, what the market did, and an
explicit **attribution grade**.

**THE FINDING THAT ORGANISES EVERYTHING: the stock tracks the change in
expectation, not the level of delivery.**

| | revenue growth | share return |
|---|---|---|
| FY24 | **−8.2%** | **+24.1%** |
| FY25 | +26.6% | +49.6% |
| FY26 | **+46.0%** | **+20.5%** |
| FY27 YTD | one quarter | **+141.7%** |

The business shrank and the stock rose a quarter; three years later the best
operating year in its history produced the worst full-year return of the
three. Three of fourteen earnings days moved it ±20% — a guidance CUT
(−20.3%, the worst on record), a delivered inflection (+34.5%, the best) and a
beaten year (+20.5%) — **and none of the three was about the quarter being
reported.**

**RAW MATERIALS WERE TESTED AND THEY EXPLAIN NOTHING.** 21-day return
correlations: copper +0.00, HRC −0.06, aluminium −0.11, brent −0.16, USDINR
−0.26, cp_coke −0.26. The regime test settles it — FY24 inputs flat-to-down
with revenue −8.2%, FY26 inputs up 27-64% with revenue +46%, gross margin
36.3% → 34.3% across the whole span. Avalon is a converter with pass-through
pricing, demonstrated under the hardest test available: through the tariff
period it paid **over 50 distinct tariff rates and recovered more than 99%**
from customers. The two mild negatives are risk-off proxies for Indian small
caps, not cost channels. **This is why the EMS spec was right to withhold
economics for these names** — a margin bridge here would have carried a 0.50
composite weight on a linkage that measures zero.

**Two honesty rules are rendered on the page, and both cost something:**

- **Seven of eighteen 8%+ sessions have NO explanation in this archive** and
  are listed as unattributed. Calls happen four times a year; news flow,
  initiations and blocks do not. Inventing a reason for those seven would
  make the eleven real attributions worthless.
- **The deepest drawdown in the history carried ZERO company information.**
  −39.7% peak-to-trough between 2024-12-27 and 2025-01-28, inside the
  January-2025 Indian small-cap correction, with no call, no filing and no
  guidance change — and the Q3FY25 print six days after the trough was
  excellent. A dossier that reads only company documents would have read that
  trough as a signal. It is labelled `attribution: none — market factor`.

**The most instructive single event is Q2FY26 (2025-11-06): guidance was
RAISED to 28-30% and the stock fell 9.6% on the day, −22.1% over twenty
sessions.** It had run 34% into the print, and the reported margin was
optically compressed ~110bps because the tariff pass-through grossed up both
revenue and cost — a mechanism stated plainly on the call and invisible in the
headline. A raise already in the price is not a catalyst, and a margin you have
not read the accounting for is not a margin.

### `/company-analysis` — the skill, added 2026-09-14

PM: *"can we create a skill that creates a company analysis like this? Takes
in earnings calls or searches for them on screener, bifurcates everything, does
the analysis, creates price explanation."* So Avalon's run was generalised into
`.claude/skills/company-analysis/` plus two scripts, keeping the repo's split —
**the scripts do the arithmetic, the agent does the reading.**

| | |
|---|---|
| `packages/company/fetch_transcripts.py` | screener Concalls block → deduped, dated PDFs → page-marked text + a boilerplate-stripped copy |
| `packages/company/price_story.py` | reactions, drawdowns, 8%+ sessions, chapter candidates, and the commodity test |

Both carry `--selftest` with acceptance AND rejection cases per the GLOB
lesson. `price_story --selftest` caught a bug on its first run — and it was the
TEST that was wrong: the synthetic fixture started its recovery the session
after the crash, so `d1` correctly read −24.5% rather than −25.0%. The fixture
was fixed and a comment left at the site saying so, because the next reader
will otherwise "fix" the code.

**Four traps are encoded in the fetcher**, all met on the first real run:
screener rows duplicate (dedupe on URL, not date); the quarter is NOT in the
link and is derived from the call month (Aug→Q1 … May/Jun→Q4, fiscal year
rolling with it); some transcripts are broker-hosted (MOFSL, JM) — same call,
different provenance, recorded; and page 1 is often the covering letter to the
exchanges, flagged and skipped. A scanned PDF is REFUSED, never OCR'd.

**The fetcher reports whether the quarter sequence is contiguous**, because
*"all 14 calls that exist"* and *"a sample of 14"* are different claims and the
dossier has to make the right one.

**`price_story.commodity_test` exists to say NO.** It ran zero on every input
for Avalon and prints `no usable linkage — do NOT build a cost overlay` below
|0.35|. Its docstring carries the reason the threshold is not a significance
test: a converter with pass-through pricing reads ~0 *because the economics
work*, so the regime check (did margin survive a period when inputs moved
hard?) has to be run by hand before concluding either way.

**Two script outputs the skill forbids swallowing.** `unattributed` — sessions
that moved 8%+ with no call within two days; ten of Avalon's eighteen, and the
skill says to render them as unexplained because inventing a reason for each
would devalue the eight that are real. And `market_factor` on a drawdown — no
call inside the fall, so nothing the company said caused it; the flag fires
automatically on Avalon's deepest drawdown, which was the January-2025
small-cap correction.

Verified end-to-end: `fetch_transcripts.py AVALON --list` returns the same 14
calls with correct quarter labels, and `price_story.py avalon --calls …`
reproduces the published reactions, the three drawdown episodes and the
commodity verdict exactly.

## Auto — a READ tab, added 2026-09-19 with Ather Energy

PM instruction: cover "Aether (the ev company)" and "create an auto tab also".
The company is **Ather Energy (ATHERENERG, NSE; 544397, BSE)** — "Aether" is
not an EV maker, and the spelling was corrected before anything was fetched.

**The tab scores NOTHING and must not start to.** `auto` is in
`engine.SECTORS` with `peer_groups: []` — the same shape IT had on
2026-08-31, and honest for the same reason: no spec has been written, no cost
stack tested, no peer group defined. `ather` reaches `entities` only through
`yahoo_prices.EQUITIES`, which inserts with peer_group NULL, so invariant 7
keeps it out of every pillar, every pair and every backtest. `run_scores.py`
was re-run after the change and still writes 106 rows across the same five
sectors. **Do not give `auto` a peer group to make the tab look finished.**

**The chart is BRENT, and that is a deliberate refusal.** For an ICE maker
crude is a cost; for an electric two-wheeler maker it sits on the REVENUE
side, and it is the driver management names first — the Q1FY27 call lists
petrol *availability* fears and rising pump prices among four structural
tailwinds. Charting a cost series there would assert the cost story the
evidence below refuses. The caption carries the caveat: brent is +102% over
Ather's listed life against the stock's +442%, but the 21-day correlation is
**−0.149**, the generic risk-off sign. Level evidence, no short-horizon
evidence, and the page says both.

`ATHERENERG-BL.NS` is recorded in `yahoo_prices.REJECTED`: Yahoo returns it
beside the ordinary line under the same `longName`, so the name pattern cannot
separate them and the symbol is pinned instead. It is a thin parallel
(block/trade-for-trade) book, not the tape.

### Ather's dossier — six calls, and that is the whole population

Listed 06-May-2025, so Q4FY25 was its first call as a listed company. Six
transcripts, contiguous, none scanned, ~53,400 words. **Five and a bit
quarters is a record, not a track record**, and the coverage note says so
before the ledger does.

**THE CENTRAL FINDING: one line explains the whole chart, and it is the
EBITDA margin.** Listed at −23%, first positive EBITDA quarter five prints
later, +442.5% on the share price. The ladder is almost monotone against the
close before each call — and it has exactly one exception, which is also the
only drawdown in the history:

| | EBITDA margin | cum. share return at the print |
|---|---|---|
| Q4FY25 | −23.0% | −0.7% |
| Q1FY26 | −16.0% | +14.9% |
| Q2FY26 | −10.0% | +107.0% |
| Q3FY26 | −3.0% | **+105.5%** ← the exception |
| Q4FY26 | −2.0% | +209.2% |
| Q1FY27 | **+0.8%** | +316.9% |

**The stock paid for a SIGN, not a SIZE.** +INR9cr of EBITDA on roughly
INR1,300cr of quarterly revenue produced the largest single-day reaction in
the history (+15.1%), in the worst commodity quarter on record. The best
operating year the company ever had produced +0.3% over twenty sessions,
because management spent that call warning about commodities instead.
**Every one of the six reactions is positive on all three horizons** — there
is no negative earnings reaction anywhere in this company's listed life,
which is the clearest single sign of how short the sample is.

### The commodity test returns zero, and it means the OPPOSITE of Avalon's zero

This is the finding worth carrying to the next name. Avalon's inputs were
**tested and measured zero**, because a pass-through converter has no cost
channel. **Ather's binding input is simply ABSENT from the store.**

In Q1FY27 management booked a **5.6pp hit to gross margin** from commodity
inflation — the largest input shock in its listed life — and in that exact
quarter every tradeable series this system carries **FELL**: aluminium
−12.8%, nickel −4.9%, Indian HRC −2.2%, brent −43.7%, lead −1.9%, USDINR
flat, only copper up (+8.9%). **A cost overlay on those series would have
read the worst commodity quarter on record as a TAILWIND.** The inputs that
actually moved are lithium ($8/kg → $24/kg), the NMC cathode complex, cells
(+30-50%) and DRAM — none has a series here and none is backfillable from a
free source. Management's own number is a company commodity index **+46% over
five quarters**.

**So a null correlation has two opposite readings and they must be told
apart:** either the business has no cost channel, or the channel is real and
the series does not exist. `price_story.commodity_test` cannot distinguish
them — only the regime check by hand can, which is exactly what its docstring
warns.

### The drawdown was NOT the market, and that had to be checked

Ather's deepest drawdown (−21.5%, 2025-10-21 → 2026-01-29) landed while the
**Nifty fell 1.7% and Nifty Auto 2.5%** — so it was entirely the stock's own
de-rating. That is the mirror image of Avalon's deepest drawdown, which was
the January-2025 small-cap correction. `price_story` already flagged
`market_factor: false`; the benchmark check confirmed it rather than assuming
it. **The transferable thing is the test, not the answer.** The benchmarks
live in `flow_series` (`nifty`, `nifty_auto`), not `prices`, so
`price_story --benchmark` cannot reach them — it was computed directly.

**Two of the five `unattributed` 8%+ sessions ARE explained by this archive**,
because the script's rule only knows about earnings calls: 01-Sep-2025 (+8.1%)
is the first session after Ather Community Day 2025 (Stack 7 + EL unveil, a
Saturday), though Nifty Auto rose 2.80% that day so the attribution is
partial; and 28-Aug-2026 (+8.1%) is the Friday before Community Day 2026,
where EL launched, with Nifty Auto at −0.11%. Three remain genuinely
unexplained — 09-Sep-2025, 16-Oct-2025 and 29-Jun-2026, the last being +8.5%
on a day Nifty Auto fell 2.08%.

### The ledger — 22 commitments, 2 miss / 3 beat / 8 met / 9 pending

Two rows carry more than their verdict:

- **The qualifier moved before the date did.** "The full 42,000 should be
  operationalized before end of this FY **for sure**" (Q4FY26) became "could
  spill over into the first few months of FY2028" **one call later**. The
  plant is tracking; the certainty went first.
- **The motorcycle horizon has receded faster than time has passed.** Fifteen
  months took Zenith from "we have begun platform level work" to "more than a
  year away for sure. Probably two years plus" and "Ather may not be the
  pioneer this time." That is disclosure rather than the silence Avalon's
  aerospace programme went quiet in, but it is the same shape. Every EL date,
  from the same management in the same archive, was met.

**The one hard number the company has ever given was 700 stores, and it landed
on it** — then it declined to repeat the exercise for FY27 and, in Q1FY27,
gave no store count at all for the first time since listing. A company that
refuses guidance tells you something when it stops refusing.

### Flags rendered, never resolved

- **Q4FY26 volume is 83,000 on p3 and 81,000 on p5 of the same call.** 83,000
  reconciles with the separately-stated +23% QoQ on Q3's 67,800; 81,000 does
  not.
- **The Q4FY26 price hike is "INR3,000" on the Q3FY26 call and "roughly about
  INR1,000 to INR1,500" on the Q4FY26 call.** Roughly 2x apart, never
  reconciled.
- **FY26's EBITDA margin is DERIVED, never printed** (~−7%, from two
  independent routes), and the four stated quarterly wholesale figures sum to
  ~262,900 against the ~257,300 that "+66%" on FY25's 155,000 implies — ~2%,
  all of it verbal rounding. Both must come off a filed statement.
- **Market shares were restated upward between calls** (Q1FY25 7.4%→7.6%,
  Q4FY25 13.3%→13.6%). Vahan back-dates registrations, which would explain it;
  no call ever says so.

**F&O membership is NOT established by this archive** and is listed as an open
hole. Ather has no `oi` row and is in no spec, so nothing here says whether it
can be a pair leg. Check the NSE list before treating it as one.
**CLOSED 2026-09-23: ATHERENERG IS in F&O**, first contract 2026-08-26,
confirmed from `fo_oi` — the loaded NSE bhavcopy, not the transcripts. The
sentence above stands as written because the point it makes is still right:
the ARCHIVE could not answer it, and the answer came from somewhere else. It
still has no `oi` row and no spec, so invariant 7 keeps it out of scoring
regardless; what changed is that it is now eligible to be a pair leg.

**The transcript long-form notes were NOT written** — the Document Archive
shows `no note yet` against all six PDFs, which is the honest state. The
dossier does not depend on them.

**`fetch_transcripts.py` reported `cover_letter: false` on all six and page 1
of every one IS the covering letter to NSE/BSE.** Harmless here (the reader
skips it) but the flag is not firing on this issuer's filing format, and a
future run on a scanned or differently-laid-out filing should not be trusted
to catch it either.

### Eicher Motors — the Auto tab's second dossier, 2026-09-20

PM: *"now do for eicher motors"*. `EICHERMOT` / BSE 505200, added to
`yahoo_prices.CANDIDATES` and loaded from 2007-01-01 (4,867 rows). **The 2020
1:10 split is smooth through the event** — the >15% jump scan finds eight
moves, all 2007-2010, all GFC-era market moves, so no allow-list entry is
needed. Scores nothing; `peer_group` NULL; `run_scores.py` still writes 106
rows.

**SEVENTEEN CALLS IS A SAMPLE, NOT THE POPULATION — the opposite of Ather and
Avalon, and it changes the tooling.** Screener's concall block starts at
Q1FY23 (Aug-2022) against nearly twenty years of loaded price. Run unbounded,
`price_story` reports **45 unexplained 8%+ sessions of which 44 predate the
first transcript** — which does not make the dossier honest, it makes the
unexplained list meaningless. Hence `--since`.

#### THE CENTRAL FINDING: most of it is the sector, and the alpha is one tax change

| chapter | Eicher | Nifty Auto | vs sector | sessions |
|---|---|---|---|---|
| 1 Aug–Oct 2022 | +24.7% | +3.3% | **+21.4pp** | 60 |
| 2 Oct 2022–Mar 2023 | −25.9% | −10.9% | −15.0pp | 103 |
| 3 Mar–Dec 2023 | +46.6% | +48.4% | −1.9pp | 167 |
| 4 Dec 2023–Jul 2025 | +28.8% | +34.0% | −5.1pp | 407 |
| 5 **Jul 2025–Feb 2026** | **+52.1%** | +21.1% | **+31.0pp** | 146 |
| 6 Feb–Sep 2026 | −8.2% | −5.5% | −2.8pp | 141 |

Over the window Eicher did **+143.4% against Nifty Auto's +109.4%** — ~34pp of
outperformance. **Chapters 3 and 4 are 574 of the 1,025 sessions (56%) and
Eicher LOST to its sector in both.** Every point of the relative gain and more
came in chapter 5, on the **22-Sep-2025 GST reform** that cut sub-350cc to 18%
— and roughly nine in ten Royal Enfields are 350cc. Q2FY26 revenue +45%,
Bullet +70%, Hunter +41%, enquiry-to-booking 20-21% → 29-30%.

**The uncomfortable read: the market paid Eicher almost nothing for four years
of strategy and everything for the exogenous rate cut that made the strategy
pay.** The refreshed Classic/Bullet/Hunter that caught the cut existed because
of the FY25 spending the market had already marked down — the Feb-2025 print
(record volumes, margin 24.2% vs 26.1%) is the **worst reaction in the
archive at −7.4%**.

**Nifty Auto lives in `flow_series`, not `prices`, so `price_story
--benchmark` cannot reach it.** Computed directly. Worth fixing if a third
sector-heavy name lands.

#### Two bugs found and fixed, both SILENT_BUGS shape

**1. `fetch_transcripts` silently de-spaced three filings.** PyMuPDF's default
`get_text()` reads inter-word spacing from the PDF's own encoding. Eicher's
Q1/Q2/Q3 FY25 (Google-Docs exports) wrap every word in U+202D…U+202C with **no
space between runs**, so the extractor returned
`Ourtotalsalesstoodatabout2,27,736motorcyclesinthisquarteras`. Nothing raised:
the file existed, `scanned` was correctly False, **every number survived
intact**, and the only symptom was a word count **55% low** (4,353 against a
real ~9,700) — which reads as "that was a short call", not "the extractor is
broken". `_page_text()` now falls back to `get_text("words")`, which derives
boundaries from GLYPH POSITIONS, **only where the default is materially worse**
(words-mode reorders multi-column pages, so it must not become the default),
and reports `respaced:Np` per file. The fourteen healthy filings came back
byte-identical in word count.

**2. `price_story` anchored a non-trading call date BACKWARD.** Eicher's Q4FY24
was **Saturday 11-May-2024**. The walk-back draft anchored on Thursday's close
and reported the reaction as **+2.0% — every point of which happened on the
Friday, BEFORE the call.** Monday, the first session that could have reacted,
was −0.01%. In range, correctly signed for a good quarter, entirely
pre-announcement noise. Now resolves forward, so base = Friday's close, same
treatment a Thursday-evening result already gets. Fixed in **both** sites —
`reactions()` and `benchmark_test()` — because the plain reaction and its
sector/idio split must describe the same window.

Both carry acceptance AND rejection tests. **The respacing fixture's rejection
case is the load-bearing one**: firing on a healthy page would corrupt fourteen
files to repair three. And the Saturday fixture was wrong on its first run and
the CODE was right — it put the reaction on the Tuesday and asserted d1 == 0,
forgetting d1 spans the resolved call day plus one. Comment left at the site,
per the drawdown-fixture precedent.

#### The commodity test returns zero for a THIRD different reason

Three companies, three near-identical correlation tables, three different
mechanisms — which is why the regime check is not optional:

| | what zero meant |
|---|---|
| Avalon | inputs tested, **measured zero** — pass-through converter, no cost channel |
| Ather | the binding input (lithium, NMC, DRAM) **has no series in the store** |
| **Eicher** | the inputs **are** in the store, the cost line **is** large — and management re-prices it quarterly |

Steel, aluminium and copper are genuinely Eicher's stack and all three are in
`prices`. Over the window HRC +21.1%, aluminium +36.8%, copper +48.6% while
the EBITDA margin went 23.4% → 24.0%. **Q1FY27 is the proof**: a 4-4.5% input
shock — the largest in the archive — moved the margin 10bps, because a 1.75%
price rise and 0.4% of value engineering absorbed it. Royal Enfield took **no
price increase at all across FY25** and then four tranches in fifteen months.
A cost line re-priced every quarter cannot show up at 21 days. **Track the
basis-point bridge management gives; do not rebuild it from LME.**

#### The ledger — 19 commitments, 4 miss / 3 beat / 6 met / 6 pending

- **International was the miss that matters.** *"it will mimic what we saw in
  India a few years back"* (Q1FY23). Four years later: 100k → 120,634 units,
  FY24 **went backwards** to 77,209, and midsize share outside India is still
  the same 8-9% band quoted in 2022. The CKD network built underneath it is
  real; the growth curve did not travel.
- **The concession that never had to be made.** Siddhartha Lal wrote midsize
  share down to *"80%-85%"* in Aug-2023 against Harley, Triumph, Bajaj and
  Hero entering. Exit share Q3FY26: **88.9%**. It dipped to 84% for exactly
  one quarter, on the GST slab change, not on competition.
- **The replacement cycle is the receding-horizon row.** *"it will kick in"*
  (Q3FY24) → *"we are waiting for that moment"* (Q3FY25) → *"it is yet to kick
  in"* (Q4FY26), while the installed base went 6m → 8m+. Q1FY27 finally
  quantified it: **RE-to-RE upgraders are 5-6% of buyers**; ~70% are conquest
  from other brands. Thirteen quarters, horizon unmoved. Ask whether it is
  late or wrong.
- **VECV is the row nobody asks about and it compounded hardest** — EBITDA
  margin 7.5% → 9.5% across four years, two with a flat-to-down industry, and
  past 100,000 units in FY26. It is **equity-accounted**, so it reaches EML's
  P&L only as ₹135-183cr a quarter of share-of-profit and never as revenue.
  Structure decides attention; attention is not value.

**The capacity date will not sit still.** ₹958cr Cheyyar brownfield to 2m
units has been stated as FY27-28, then "beginning of fiscal 29" (accepted
without correction in the same call), then Q2 FY28, then FY27-28 again — three
different answers in three consecutive calls. Plus ₹1,225cr greenfield at Tada
(261 acres) for 2.45m by FY29-30, and a **50-50 vehicle-financing JV with
Volvo** (₹750cr) — the first non-vehicle business in the archive.

#### Flags rendered, never resolved

- **Q4FY24 EBITDA is CORRECTED, not as stated.** The call says *"₹829 crores,
  up 21%"* — which is DOWN on Q4FY23's ₹934cr, contradicts the 26.5% margin
  stated in the same breath, and breaks the stated FY24 total. The three
  earlier quarters sum to ₹3,198cr of ₹4,327cr, leaving **₹1,129cr = 26.5% of
  ₹4,256cr exactly**. A transcription error — and identifiable as one *only*
  because **every one of the four complete years in this archive cross-foots
  to the rupee**, which neither previous company's did.
- **Stark Future is €50m on the Q3FY23 call and €15m on the Q4FY23 call.** A
  3.3x discrepancy, never reconciled on any later call.
- **VECV restates its own comparatives on almost every call** — Q1FY23 EBITDA
  ₹207cr → ₹218cr, Q2FY25 7.1% → 7.3%, Q3FY25 ₹509cr/8.8% → ₹517cr/9.2%. Each
  small, the pattern systematic, no call explains it.
- **Four different April-2026 growth rates in one call** (51%, 57%, 31%, 37%)
  with no basis stated for any of them.
- **The midsize share column changes basis mid-archive** — ">125cc" through
  FY24, "midsize 250-750cc" from FY25 — which is why it jumps ~30% → ~88%. The
  two are not comparable and the dossier says so on the table.
- **VECV Q3FY24 parts sales printed as "₹5,060 crores"**, which would exceed
  that quarter's entire VECV revenue. Reads as ₹506cr on the trend.

**One session in 1,025 moved 8%+** (2025-01-02, +8.7%) — and Nifty Auto rose
5.18% that day, so most of it is the sector. A large cap grinds: worst
earnings reaction −7.4%, best +8.0%, both inside the threshold. Compare Avalon
(18 in 846) and Ather (8 in 347).

**The transcript long-form notes were NOT written** — all seventeen show `no
note yet`. `EICHERMOT-` has no BL-series twin to reject, unlike ATHERENERG.

### Vahan maker share — the Auto tab's chart, 2026-09-23

PM: *"in the auto tab, I need a graph for market share change on a daily
basis. Different for PV, 2 wheelers and CV. Only include fno names in the graph
lines, keep rest as others."* Three stacked charts on Auto's Prices & Watch
panel, `/api/auto_share` -> `engine.auto_share` (transport shim) ->
`packages/adapters/vahan_share.py`, which owns the fetch, the roster, the
arithmetic and the selftest. Scores NOTHING; `vahan_share` is a VOLUME table
and nothing on this path reaches `prices` or `pillar_scores`.

**DAILY DOES NOT EXIST ON THE OPEN API, AND THAT IS THE SHAPE OF THE WHOLE
FEED.** Vahan's daily granularity lives only behind
`/analytics/vahanpublicreport`, which is CAPTCHA-gated. The public dashboard
API bottoms out at MONTHLY (`calendarType=3`); **4..8 silently fall back to
YEARLY**, and `transactionLineChart`, `vahanYearWiseRegistrationComparisonChart`
and `vehicleRegistration` all return twelve monthly values per year. All
probed. So "daily" here is the **month-to-date level re-read every morning**:
day t = MTD(t) - MTD(t-1). Two consequences, both printed on the page:

- **FORWARD-ONLY.** First capture **2026-09-23**. A past date's MTD level was
  never published, so there is no daily history and none can be manufactured —
  the IndiaMART bargain again. The MONTHLY series is long (13 months stored,
  the source reaches the 1990s) and is captured alongside, which is the only
  reason the chart was not empty on the day it was asked for. The toggle
  defaults to Monthly and the Daily view REFUSES to draw one point — a single
  dot renders as a flat line, which reads as "share did not move", the one
  thing one capture cannot tell you.
- **IT IS A REVISION SERIES, NOT A PRINT.** Vahan BACKDATES: a registration
  files against its own date, so a month's MTD keeps growing for days. Ather's
  own dossier records the symptom from the other side — market shares restated
  upward between calls (Q1FY25 7.4%->7.6%) with no call explaining it. So a
  naive MTD(t)-MTD(t-1) can go NEGATIVE when a backdated batch lands in the
  prior month. **The MTD SHARE is what is stored and led with**; it revises
  gently and converges on the month's final answer.

**THE F&O ROSTER IS READ FROM WHAT ACTUALLY TRADED, NEVER HARDCODED.**
`_fno_live()` reads distinct symbols from `fo_oi` within 30 days. A hardcoded
list rots INVISIBLY: a name that leaves F&O keeps drawing its own line and
stops being in Others, the shares still sum to 100%, and nothing raises. Two
findings fell straight out of it, both in the selftest so a change upstream
fails loudly:

- **ATHERENERG IS in F&O** (first contract 2026-08-26) — closing the open hole
  in Ather's dossier, from the bhavcopy rather than the transcripts.
- **TATA MOTORS' CV ENTITY IS NOT.** `TATAMOTORS` last traded **2025-10-23**
  and `TMPV` first traded **2025-10-24** — the demerger, visible as a clean
  handover in `fo_oi`. Only the PV arm carries futures. **So on the CV chart
  India's LARGEST CV maker sits inside Others**, which is the PM's rule applied
  faithfully and NOT an omission. Others is consequently the biggest line there
  (44.8%), and the per-segment note says why, under the chart it applies to —
  a CV chart whose residual leads reads as a bug otherwise.

**CV WAS SPLIT INTO M&HCV AND LCV ON 2026-09-24** (PM: *"Mahindra is in LCV,
only Ashok, TMCV, VECV and eicher are MHCV. Forcemotor is also different"*),
and **the split is `vehicleSubCategories`, not a list of makers.** Assigning
tiers by hand would have put Ashok Leyland's 6,595 Dost units into M&HCV and
Tata's whole book into one tier; the tape already carries HEAVY/MEDIUM/LIGHT,
so makers fall where they belong instead of where a spec asserts. Confirmed
before building — Aug-26 MHCV/LCV: Mahindra 746/23,887, Force 171/3,019, Ashok
Leyland 10,495/6,595, Tata 14,021/17,920, VECV 5,948/1,409. Ashok Leyland and
Tata STRADDLE both tiers, which a maker list could not have expressed.

  - **`EICHER MOTORS LTD` REGISTERS ZERO VEHICLES.** Every Eicher truck files
    as `VE COMMERCIAL VEHICLES LTD`, so "VECV and Eicher" is ONE line. Drawing
    both would put a permanent flat zero beside a real series, which reads as a
    collapsed business rather than a naming fact. Three MHCV lines, not four.
  - **`LIGHT PASSENGER VEHICLE` IS TAXI-REGISTERED CARS**, not LCV product —
    Maruti alone is 23,592 of Aug-26's 43,182. LCV includes it on the PM's
    choice because it is the only place Force Motors' Traveller appears (95
    goods against 2,924 passenger); the cost is Maruti at ~27% of the LCV
    chart, inside Others. The tooltip says so.
  - **M&HCV DRAWS NO OTHERS LINE** (PM). Its three lines are ~89% and
    deliberately do NOT sum to 100 — suppressing the residual must not rebase
    the rest onto a smaller universe, which would inflate every share.
  - **TMCV IS IN `FNO_EXEMPT`.** Listed (TMCV.NS, "Tata Motors Limited") but
    absent from the live NSE roster — a demerged entity still in its
    qualification period. The roster guard correctly dropped it on the first
    run, leaving Others at 54% of M&HCV and 70% of LCV, the exact complaint
    that started the rework. Drawn on instruction, the Hindustan Copper
    precedent: cash-only, never a pair leg. `--selftest` checks the exemption
    has not ROTTED — the day TMCV enters F&O it stops being an exception.
  - The CV tiers have **no harvested month shape**: the exports are a
    maker x category-GROUP pivot and cannot carve sub-categories. `load_shape`
    SKIPS them and reports it; their skew is basis `none`, so the forecast is
    plain working-day extrapolation and the tooltip says so. One export with
    X-Axis = **Sub-Category** would fit them.

**THE OTHER SEGMENTS ARE `vehicleCategoryGroup`, AND THE BUCKETS WERE CHECKED
RATHER THAN ASSUMED:** `2W = Two Wheeler`, `PV = Four Wheeler`.
Four Wheeler is PASSENGER — Maruti 168,502 there in Aug-26 against Ashok
Leyland's **1** (0.0059% of its own 16,861 CV registrations), while the CV
entity Tata Motors Ltd sits at 36 in Four Wheeler against 3,547+618 in
Goods+Bus on the MH cross-section. The selftest asserts that as a RATIO, not a
zero: an `== 0` failed on that single stray vehicle, and "no CV maker ever
appears in Four Wheeler" is both untrue and not the claim worth testing.
The eleven groups OVERLAP ~0.6% so they are not a partition — which does not
touch a share, because every share is computed INSIDE one segment against that
segment's own separately-fetched total. Tractors and 3W are deliberately out;
the PM named three segments.

**THE EMPTY-BODY RULE, THIRD AND FINAL FORM.** TRAP 2 is a ROW-COUNT
short-circuit, and every call here carries a filter that keeps the result set
under it. So **under a filter an empty body means NO REGISTRATIONS**, not a
refusal. Treating it as one broke the CV split on its first run:
`ASHOK LEYLAND LTD.` — the trailing-period duplicate holding 36 vehicles in its
whole life — has no MEDIUM GOODS VEHICLE row and the entire capture aborted.
Narrow the query enough and every maker eventually has an honest zero. `strict`
is reserved for the SEGMENT TOTAL, where an empty body cannot be real.

**ALL-INDIA IS SAFE ONLY WITH A SEGMENT FILTER — and that is measured.**
Unfiltered, `stateCode=""` short-circuits to an empty body for the largest
makers (see `vahan.py` TRAP 2) and needs a 36-state sum. **That short-circuit
is INTERMITTENT** — deterministic on 2026-09-19, gone by 2026-09-23 — which is
exactly why the state-sum stays: it is the only path correct under both
regimes, and a test asserting the server stays broken was removed the day it
failed. WITH a segment filter
the result set is small enough that it does not fire, and it is **36x cheaper**
— the whole capture is ~18 requests, ~15s. Hero/2W and ALL/Four-Wheeler both
reconcile to the state-sum with **zero difference** across five months, and
`--selftest` re-runs that reconciliation every time, because the day the
server's threshold moves is the day this silently starts under-counting.

**Others is DERIVED as `total - sum(named)`**, never a separate query, so the
lines cannot fail to sum to 100% and every unlisted maker is inside it. A
NEGATIVE Others raises rather than clamping: it would mean a maker string is
double-counted, and a silently clamped 0 reads as "no unlisted makers", a claim
about the market rather than a bug report. Tested both ways.

**Wired into `refresh.py` AFTER the F&O bhavcopy step** — the roster is read
from `fo_oi`, so running first would classify against yesterday's F&O universe,
and silently on exactly the day a name enters or leaves. **Deliberately NOT in
`SKIP_IF_DONE`**, same reasoning as the equity load: the capture is an MTD
LEVEL keyed on capture_date, so a 15:00 re-run overwrites the 08:00 row with a
fuller count of the same day — re-running is an improvement, not a duplicate.
`export_static.py` enumerates the route and its parity selftest knows it (20
checks pass), or the vault copy would render the panel empty.

**First cross-section, 2026-09-23 MTD** — 2W total 1,303,493: Others 38.4%,
Hero 24.8%, TVS 20.6%, Bajaj 9.1%, Royal Enfield 5.5%, Ather 1.6%. PV 284,978:
Maruti 38.8%, Others 27.3%, Hyundai 12.5%, Mahindra 12.1%, TMPV 9.3%. CV
68,705: Others 44.8%, Mahindra 26.6%, Ashok Leyland 18.2%, VECV 8.3%, Force
2.1%.

**Registrations are RETAIL, not dispatches** — companies report wholesales to
dealers and the two differ by channel inventory. That divergence is the reason
to track Vahan at all, and it is also why these numbers will never tie to a
reported volume. The page says so.

**Known and NOT fixed:** the Mahindra PV line is `MAHINDRA & MAHINDRA LIMITED`
filtered to Four Wheeler, which is clean — but the same maker string
unfiltered mixes SUVs with the Bolero pickup (39% of its MH Aug-26
registrations are Goods Vehicle). The segment filter does the splitting here,
so the charts are right; anyone querying that maker string WITHOUT a segment
filter gets a blend. `vahan.py`'s illustrative `GROUPS` map is exactly that
trap and is labelled "NOT a spec".

#### The month-end forecast — on the tab since 2026-09-24

PM: *"Given the sales from the start of the month, can we extrapolate it from
the 1st till whatever is today and extend that to month end? ... I just know
sales increase in the second half of the month usually."* Both halves true;
neither true the way the naive method assumes.
`forecast = MTD / (working-day fraction x segment skew)`, a panel above the
share charts, printing BOTH factors rather than only the answer.

**CALENDAR DAYS FLIP THE SIGN.** Hero, Sep-2026: days 20-23 ran
13,466/CALENDAR day against 14,188 over days 1-19 — a slowdown. Per WORKING
day the same window is **17,955 against 15,857, a 13% ACCELERATION**. The whole
apparent slowdown was one Sunday. Public holidays are deliberately NOT
modelled (basket_index's holiday-calendar ruling); whatever they do is absorbed
into the measured skew.

**THE MONTH IS BACK-LOADED, BY SEGMENT, AND MILDLY** — harvested from the
CAPTCHA-gated report builder, four exports now VERSIONED in
`data/staging/vahan/` because they are the only record of numbers no automated
step can re-fetch. `observed / working-day`, 1.00 = no skew:

| cut | 2W | PV | CV |
|---|---|---|---|
| Jul-26 d15 | 0.995 | 0.927 | 1.006 |
| Aug-26 d15 | 0.911 | 0.846 | 0.940 |
| Aug-26 d24 | **0.983** | **0.912** | **0.983** |

**PV is the most back-loaded in EVERY month** — month-end dealer push shows up
in cars far more than two-wheelers — so the skew is per segment, never one
market number. And **it is a CURVE**: it relaxes toward 1.0 as the cut nears
month end, because the late surge has by then been counted. `cut_day` is a KEY
in `vahan_month_shape`, never averaged over; a cut with no harvested month
returns skew 1.0 with `basis: none` and the panel labels that row amber, since
it is plain working-day extrapolation with no back-loading at all.

**THE FESTIVE MONTHS BROKE THE METHOD RECOMMENDED FIRST.** Sep-2025 at d24 is
0.722/0.659/0.873, far outside the normal band; Navratri began 22 Sep 2025 and
October-2025 2W then printed **+140.7% m/m**. Year-on-year anchoring cancels
intra-month shape ONLY WHEN THE SHAPE REPEATS, and the festive effect keys on
the LUNAR calendar — so it forecast Sep-2026 at +75%/+77%/+41% against a market
running +20-30% in Jul/Aug. Not used. The working-day method lands +28.6% /
+27.9% / +25.4%, inside the band, which is the plausibility check the other
failed.

**FESTIVITY IS A PROPERTY OF THE MONTH, NOT OF ONE SEGMENT.** The first
`skew()` tested each segment alone and KEPT Sep-2025 for CV (0.873, inside the
band) while dropping it for 2W and PV — so CV's factor blended a festive month
with a normal one, **0.928 instead of 0.983**, and nothing about it looked
wrong. Commercial buyers genuinely care less about an auspicious date than
retail ones, which is why the per-segment test passed and exactly why it must
not be the test. A period is now judged across ALL segments and excluded from
every one together; the panel names the excluded month ONCE with each
segment's own ratio, because that spread is the evidence.

**`_full_month()` reads `vahan_share` before the network.** Fitting over HTTP
cost 20-30 round trips per page load; the daily capture already stores every
segment's monthly TOTAL. It REFUSES the current month — its stored total is a
month-to-DATE, and a partial over a partial gives a fraction near 1.0, a
"no skew" reading that is pure arithmetic on the one month that matters.

**EACH CHART HAS A LIVE TABLE BESIDE IT** (PM, 2026-09-24, with a mock):
OEM · LIVE VOL (month) · LIVE SHARE · **YOY (PP)**. The chart gives up its
right-hand 40% for it. **YoY is in SHARE POINTS, not % of volume** — that is
the whole point of the column, because a maker can grow volume 20% and still be
losing its market. Green is share GAINED; a move under 0.005pp gets NO arrow,
since a green triangle on a flat share is a claim the number does not make.

The comparator is the prior-year same month, looked up **BY NAME** in
`monthly.periods` and not by index — the 13-entry window means `periods[0]` is
September-to-September today, and an index would silently compare the wrong two
months the day `MONTHS_KEPT` changes. **A basis mismatch is accepted and
stated**: this month is month-to-DATE against a completed month last year.
Share barely drifts inside a month (±0.3pp on 2W, worst case +0.93pp on Tata
PV), but on a column denominated in tenths of a point that is not nothing, so
the header carries the note. Comparing against last year's same-DAY MTD is only
possible for the two segments whose shape was harvested, and a column meaning
different things on different charts is worse than one meaning a single
slightly imperfect thing everywhere.

**THE FIRST DRAW MEASURES ZERO AND IS RECONCILED ONCE.** `#autoshare-box`
reported `clientWidth` 0 while its children had real rects — the whole document
did, `window.innerWidth` included — so `W` fell back to 892 and rendered a
535px chart beside an 826px table on a 1377px panel. Same hidden-section
problem `loadPair()` already solves. `drawAutoShare` re-measures after paint and
redraws ONCE, guarded by a `_retry` flag so a still-wrong measurement cannot
loop. The chart pane is also PINNED to `W`: `flex:0 0 auto` sized it to its
widest child, which is the LEGEND and not the plot, pushing the table past the
wrap point and under the graph.

**IT RENDERS AS A DOTTED CONTINUATION OF EACH SHARE LINE, NOT A TABLE** (PM,
2026-09-24: *"just add a dotted line to show forecast, no need for a whole
table"*) — solid through the last completed month (or last capture), dotted
across the interval that has not happened yet. On the monthly view the dash
REPLACES the current month's point, which was a partial month standing beside
full ones.

**THE SKEW HAD TO BECOME PER MAKER FOR THAT LINE TO SAY ANYTHING.** A
segment-wide factor CANCELS out of a share — (mtd/f)/(total/f) = mtd/total — so
the projected share would have equalled today's exactly and the dotted line
would have been flat BY CONSTRUCTION, reading as "share will not move" rather
than "this chart cannot tell you". The makers genuinely differ: at Aug-26 d24,
PV runs **Tata Motors PV 0.830 against Maruti 0.912 and Others 0.934**, and
that spread IS the forecast — Tata's share drifts **+0.93pp** into month end,
Mahindra CV **+0.74pp**, while 2W barely moves (±0.27pp), which is itself the
finding that 2W share is stable late in the month. `vahan_month_shape` is
therefore keyed on `label` too, Others is the residual on both sides, and the
segment forecast is the SUM OF THE PARTS so the projected shares add to 100%.
A maker with no fit of its own falls back to the SEGMENT skew, never to 1.0 —
which would assert that one maker alone is not back-loaded while every peer is.

`--forecast` and `--load-shape GLOB` on the CLI; 15 selftest checks, acceptance
beside rejection (a range not starting on the 1st is refused, a cross-foot
failure refuses the file, the skew must relax toward 1.0 at a later cut,
per-maker skews must DIFFER, and forecast shares must sum to 100).
**KNOWN: n=1 at d24** — one normal month per segment. The daily captures
supply the curve empirically from October without another CAPTCHA.

## The vault copy — one file in OneDrive, rewritten every refresh (2026-09-20)

PM: *"can we create a copy of the front-end in the vault that can be accessed
by me elsewhere? It should get updated with each full-refresh?"* The vault is
in OneDrive, so a file written there syncs to the phone and to any other
signed-in machine. That is the entire delivery mechanism — no server, no port,
no hosting cost, the same constraint everything else here is built under.

`packages/web/export_static.py` writes **ONE self-contained HTML file** to
`OneDrive - PinPOINT\Obsidian Vault\Investment Micro-System\`, beside a README
that tells the PM how to read it. **NOT into `Dashboard/`** — that is the old
vault system's node dashboard on 8765, which the standing ruling says is not
this project.

**It is a photograph of the app, not a second app.** `app.html` is copied
through byte-identical with ONE `<script>` inserted before `</head>`; ~920
`/api/*` payloads are computed once by importing `engine` directly and baked
into that script, and a shim in front of `window.fetch` answers from the map.
The day this exporter starts editing app.html is the day the vault copy and the
desk copy can disagree about what the system says, and a second front end is
exactly what this project does not want.

**It imports `engine`; it does not call the server.** refresh.py runs
unattended and serve.py may not be up — driving the export over HTTP would make
the vault copy silently skip on any morning the port was busy.

### Three things cannot survive the freeze, and each is loud

- **A missed route** lands in `window.__IMS_MISSES` and turns the snapshot bar
  amber with the route named. **This is not theoretical.** The first build
  trimmed `/api/tape?pillar=…` with no `groups`, on the reasoning that app.html
  hides the Pair tab when `live:false` (IT, Auto) so that request is
  unreachable. A click-through of the nav missed it **three times**: `pair` is
  SECTOR_SCOPED, so switching to IT or Auto re-runs `loadPair()` for the new
  sector even though the section is not the visible one. It costs **3.7MB of
  the 11MB** and it is baked, because the cheap alternatives are worse — an
  `{error:…}` stub makes `getTape()` throw into a visible error banner on a tab
  where nothing is wrong, an empty payload draws a blank chart (the silent
  hole), and leaving it to miss puts a permanent amber warning in the bar.
- **Staleness.** The in-page refresh light reads the FROZEN `status.json`, so
  it stays green forever once the file stops being rewritten — a copy that
  stopped updating would look exactly like a quiet market, which is the failure
  `refresh.py`'s light exists to prevent. The snapshot bar therefore counts
  **weekdays in the viewer's own browser** against the export stamp, the only
  clock in the file that still moves, and goes amber at 2. Weekdays rather than
  true trading days because a holiday calendar is another thing to maintain and
  wrong the first year nobody updates it; the bar says "weekdays" so the
  approximation is stated rather than implied.
- **Filed PDFs** (~20MB, not in git) stay on the desk machine. The citation
  links remain — they are provenance — and a click says where the document is.

### Wiring and the two rules that fell out of it

**A step in `refresh.py`, LAST, after `front end`** — `build_frontend.py`
verifies every route renders, and exporting a route it just failed on would put
the failure in the vault where nothing checks it. **BACKGROUND**, for the same
reason as the IndiaMART sweep: ~920 payloads take ~80s and the `.vbs` launcher
runs refresh BLOCKING before opening the page, so a foreground export turns an
~18s morning launch into ~100s. Never fatal — OneDrive being signed out is a
laptop condition, not a code failure.

**Deliberately NOT in `SKIP_IF_DONE`:** a second double-click at 15:00 should
re-export against the fresher closes, exactly as the equity load re-pulls them.
That permits two exports running at once, which is why the write is **atomic**
(temp file beside the target, `os.replace`) — OneDrive watches that folder and
must never sync a half-written 11MB page that opens blank on the phone. The
background log name is now derived from the step label, so
`cement_watch_last.log` keeps its name and the new one is
`vault_copy_last.log`.

**The Book is included and `--no-book` excludes it.** The vault is the PM's own
OneDrive inside PinPOINT, not a third party; "position data stays in gitignored
`data/`" is a rule about the git remote, and this exporter writes nothing into
the repo. Excluded, the Book tab says so rather than rendering empty.

**`--selftest` carries route parity with `serve.py`.** A route app.html fetches
that the exporter does not enumerate FAILS the test, rather than becoming a
blank tab in the vault. It also checks the key normaliser both ways (it exists
twice — Python and JS — and a divergence misses every parameterised route while
the plain ones keep working, which reads as "some tabs broke") and the
`</script>` break-out, with an acceptance case beside the rejection case per the
GLOB lesson. **One fixture was wrong on its first run and the code was right**
— it expected `?pillar=…&groups=…` in insertion order, forgetting that the
whole point is to SORT and `groups` < `pillar`. Comment left at the site, per
the drawdown- and Saturday-fixture precedents.

Verified by opening the exported file and driving it: every sector × every
visible sub-view, all four `#win` options, all five pillar pills, all seven
index ranges (3 charts each), all 30 OI rows expanded, all 23 what-moved rows
expanded, all 801 `oi_movers` date×window combinations, every company picker in
four sectors, and the `/data/` link interception. **Zero misses.**

### It updates daily, and there was exactly one hole

The chain: the Claude Desktop scheduled task `daily-full-refresh` fires
`/full-refresh` at 08:00 → its Step 3 is `python packages/refresh.py` → whose
last step is `vault copy`. The launcher re-runs the same file on every
double-click, so a second export lands whenever the PM opens the dashboard.
Nothing extra to schedule.

**The hole was `daily_review`, and it is the one staleness the snapshot bar
cannot catch.** That skill runs **outside** `refresh.py` on purpose and is the
only thing that writes `book_*` — so a review loading a paste at 16:00 changed
the Book tab on the desk and left the vault copy showing the PREVIOUS
snapshot's positions, pairs, YTD ledger and long/short index until 08:00 the
next day. The bar would still read "today · green", **correctly** for
everything that came out of the morning refresh and **wrongly** for the book,
and nothing on the page can tell the two apart — a green light over a stale
half is worse than an amber one over the whole.

So `daily_review` grew **Step 2c: re-export, foreground, after the paste loads
and after the review verdicts are recorded** (one run at the end covers both —
`add_review` writes `book_pair_reviews`, which the tab renders). ~90 seconds.
The same rule generalises: **any skill that writes a table the page reads and
does not run inside `refresh.py` owes the vault copy an export.** Today
`daily_review` is the only one.

Its "position data stays local" bullet is amended rather than deleted — the
vault copy is a deliberate, PM-sanctioned exception to it, and a rule with a
silent exception is how the rule gets ignored.

**A second hole, same shape, in the 08:00 run itself.** `refresh.py --consume
metals` returns BEFORE the STEPS list, so it does not re-run `vault copy` — and
the Kotak pack lands ~08:50-09:00 IST against an 08:00 start, so on a normal
morning the export is built off PRE-PACK scores and the late-pack path then
rescores behind it. Both the `full-refresh` skill and the scheduled task's own
prompt now say to run `export_static.py` after `--consume metals` +
`run_scores.py`, and say why. **Nothing else in the 08:00 sequence needs it**:
mail, brief, cement pack and BBG all finish before `refresh.py`, so its own
step already catches them. Rescoring without re-exporting is how the offline
copy and the desk quietly stop agreeing.

### The IndiaMART watch: the median could not move, and the plain mean lies (2026-09-20)

PM: *"what happened to the cement price check from daily mart? Any updates?"*
then *"can we do a mean instead of median?"* Both answered with the panel's own
20 captures; full write-up is `docs/SILENT_BUGS.md` **entry 11**.

**It ran perfectly and signalled nothing.** 20 captures (2026-08-28 ..
2026-09-20, ~1,100 listings/day; only 09-02 and 09-03 missing, days no refresh
ran at all). Across 5 regions x 19 day-pairs, **all 95 matched-pair medians
were exactly +0.000** — while ~40% of matched listings re-priced every day. A
median of per-listing changes only leaves zero when MORE THAN HALF the panel
moves the same day; the most that has ever moved at all is 45.3%. **The
indicator was structurally incapable of firing and read on the page as a calm
market.**

**The plain mean was refused, with numbers.** A listing flipping 290 <-> 430
reads +48.3% up and -32.6% back, so the arithmetic mean of a round trip on an
UNCHANGED price is **+7.86% per leg**. Compounding the daily plain means over
the 19 day-pairs gives **+10.72%** against a panel whose own median level went
**-1.47 / 0 / 0 / 0 / 0**, and it was positive on **19 days of 19**. At
`ALERT_PCT = 1.0` it would have alerted most weeks on churn.

**`matched_pct` is now the MEAN OF LOG CHANGES.** It cancels the flip by
construction and compounds to +0.55% against a true ~0%, and unlike the median
it has variance (-0.40 .. +0.30, both directions). `matched_median_pct` and
`n_moved` are stored beside it and `--report` prints `moved%`, because **+0.00
across a panel where 40% re-priced and +0.00 across one that did not move are
different statements and the headline cannot tell them apart.**
`--rebuild` recomputed all 20 captures from `cement_watch_listing` — the
listings table holds every row ever scraped, so a change of statistic
backfills rather than starting a second basis inside one column. The selftest
gained four cases: the symmetric flip (log mean 0 where the plain mean is
+7.859), a real uniform +5% coming through as log(1.05), and the duplicate fix
below — acceptance beside rejection, per the GLOB lesson.

**A second bug in the same function.** The matched-pair join built
`{product_id: price}` over rows keyed `(capture_date, product_id, city,
category)`, so an id under several cities silently kept **whichever row SQLite
returned last** — 727 collisions, up to 4 copies, and the same listing could
appear to move between two copies of itself. `_panel()` averages now.

**`ALERT_PCT = 1.0` IS NOW CALIBRATED, and the August guess survived.** There
was nothing to calibrate against while the median sat at zero. On the log-mean
basis: 95 readings, mean +0.027, sd 0.368, |move| p95 0.694, **p99 0.949** — so
1.0 sits just above the panel's own 99th percentile and fires on **0 of 19
days** at `MIN_REGIONS = 2` (0.5 would fire on 16%). The value did not change;
the justification did. **Still thin at 19 day-pairs — re-measure at ~60.**

### The ask chart is on the Cement tab, and the line is a MEAN

PM, same day: *"put the first graph below the score graph in cement tab, above
the stock price"*, then *"why can't you just put the average of all bags in a
region"*. Both taken; the log-mean panel is NOT displayed anywhere — it stays
the alert statistic and nothing else.

**The chart line is the MEAN level, and the median was wrong for it too.** Over
the 20 captures the mean is both more informative and smoother: **south's
median is pinned at exactly 300 on every single day** while its mean moves
308.1-312.8, and **north's median jumps to 350** where its mean stays inside
336.0-345.5. A panel carrying ~100 distinct price points puts a median on a
round number and holds it there — the same coarseness that pinned the
matched-pair median at zero, showing up a second way. Stored as `mean_bag`.

It is the **per-listing** mean (`_panel()`, so a listing syndicated across four
cities counts once), which lands within ~1 Rs of the raw all-rows mean on every
region and is the sounder of the two.

Plumbing: `engine.cement_watch()` gained a `series` block and the chart rides on
**that existing route rather than a new one** — the Overview already fetches it,
and a new route would have to be added to `export_static.py`'s enumeration or
the vault copy would render the panel empty. `drawCementAsks()` reuses
`lineChart()`, so the three charts share one visual grammar, and it renders on
**cement only** — an empty box on the other four sectors would read as a broken
query.

**The x-axis says its own span, in the axis label.** The ask chart is
sandwiched between two charts running the selected span (often months) while it
holds 20 days, and three stacked charts read as one shared time axis unless
told otherwise. The axes genuinely cannot be made to match — capture began
2026-08-28 and nothing before it exists — so the label carries
`own span 28 Aug-20 Sep, shorter than the charts above and below` rather than
leaving it to the note underneath.

**AND IT HAS NEVER HAD THE EVENT IT EXISTS FOR.** The whole point is to lead
the Kotak pack by ~15 days. **The pack has printed no new month since capture
began**: the Monthly Prices sheet in the 2026-09-18 staging file ends at
2026-08-31 (Excel serial 46265 — checked directly), so 20 days into September
there is no September column at all. The loader is right; Kotak has not
published. So the upper tail measured above is **noise only, never a known
true positive**, and nothing yet says whether this panel leads, lags or is
unrelated. Keep sweeping; judge it on the first real hike.

## The Results tab — sell-side estimates vs the print, added 2026-09-22

PM: *"add a Results tab in the end, it will be used for forecasting, storing
result estimates from sell-side analysts... sectors and then within that,
companies. Within each company, a view of results and estimates for quarter."*
Last top-level tab. Sector chips → company picker → one panel per period,
newest first: every house's estimate for a metric, the consensus, the print,
the surprise, a beat/miss verdict. `/api/results?sector=&entity=` →
`engine.results_view` (transport shim) → `packages/results/results_io.py`,
which owns the arithmetic, the loader and the selftest (basket_index's split).

**THERE IS NO NEW NUMBER TABLE, DELIBERATELY.** Estimates go in `estimates` —
whose schema comment has said "feeds the Projections tab" since day one; this
is that tab — and prints go in `observations` with `factor='actual'`, the
convention concall-ingest and `steel_actuals.json` already use. So the Q1FY27
steel and aluminium prints P4 grades against showed up on the tab before a
single row was loaded for it. Both tables carry a NOT NULL quote, so invariant
1 holds on both sides of every row. The one new table is `results_calendar`
(WHEN a print lands) because neither existing table has a place for a date
that is not a fact about the business. The consensus panel and valuation_pe
filter on `broker IN ('consensus_yahoo','bloomberg')`; those two names are
REFUSED as brokers here, so the machine feed and the house rows can never
blur. Nothing on this path reaches `prices` or `pillar_scores`.

**The roster is a UNION, resolved through the peer group first.** Specs (a
scored name's `peer_group` names its tab — aluminium.yaml says `sector:
aluminium` for six names whose tab is `non_ferrous`; a reporting unit such as
Novelis takes its parent's), then the `entities` table (IT lives only there),
then the dossiers joined on NSE symbol via `yahoo_prices.CANDIDATES` (Ather
and Eicher have no spec and a NULL sector in the table). 47 names, none
unplaced; an unplaceable one would list under `other`, never vanish. Every
sector chip renders even at 0/N, and the picker offers every covered name
with "nothing stored" against the empty ones — the picker is the coverage
list, not a list of what happens to be loaded.

**Three guards in the loader, each with acceptance AND rejection cases in
`--selftest` (27 checks):**

- **All-or-nothing per file.** One bad row refuses the whole file with every
  problem listed. A half-loaded file is the partially-corrected shape from
  the aluminium clean-up: it looks fixed and is not.
- **Units must agree within (entity, period, metric)** — across the file and
  against the store. Kotak writes "Rs41.5bn", Emkay "Rs4,150 cr"; a median
  of the two is 2,095.75 of nothing, plausible, and nothing downstream would
  complain (the SILENT_BUGS shape). Money lines are INR cr, volumes `t`,
  per-tonne INR/t, margins `%`, Novelis USD mn — and `tata_steel`'s EBITDA/t
  is `INR/t India` because the stored print is standalone; a bare `INR/t`
  against it is refused, correctly, since consolidated and standalone are
  different measures. The quote keeps the source's own figure, so every
  conversion is auditable.
- **An estimate dated after the print is not a forecast.** Kept and shown as
  `post-print`, excluded from the consensus the print is graded against.
  Including it is the `effective_from` look-ahead: the surprise shrinks
  toward zero because the "estimate" already knew the answer.

Consensus is the **median of each broker's LATEST pre-print estimate** (a
revision replaces, never averages with, the house's earlier number). Beat/miss
follows `METRICS[m].better` — a lower cost/t beats; capex carries no verdict.
"In line" is within 1% (0.2pp on margins), a named constant printed in the
meta-line. Several distinct prints for one metric are FLAGGED with all values
listed, latest shown, none corrected. `1QFY27E`/`2QFY27F`/`FY27E` normalise to
`Q1FY27`/`FY27`; FY sorts after its own Q4.

**Only numbers the source STATES are loaded.** "PAT +6% vs est" gives the
actual, not the estimate: 349/1.06 is arithmetic the broker did not write
down. The seed file (`data/results/staging/2026-09-22_q1fy27_digest_seed.json`,
tracked) carries 9 estimates and 14 prints lifted verbatim from the digests,
which is why the first cross-section reads Shree EBITDA/t 1,024 vs Emkay 1,200
(−14.7%, miss), Dalmia 664 vs KIE 701 (−5.3%, miss), Novelis $516mn vs Kotak
$508mn (+1.6%, beat). The digests end 18-08-2026; **Q2FY27 previews in early
October are the first quarter this tab forecasts LIVE.**

**Feeding it is agent-only, like every extraction here — `/results-ingest`.**
Stage a cited JSON, `results_io.py --load`, then `export_static.py` (the
daily_review rule: a skill that writes a table the page reads outside
`refresh.py` owes the vault copy an export). `refresh.py` runs `--load-all`
each morning so a staged-but-unloaded file lands at 08:00. The exporter
enumerates every sector × roster name (~60 payloads) and its parity selftest
knows the route; `build_frontend.py` checks it. The API server was restarted
for the engine change.

**Known, not fixed:** `observations` holds the steel/aluminium Q1FY27 actuals
three times over (steel_actuals.json was loaded before load_observations grew
its identity check). `periods_for` dedupes on (source, period, metric, quote)
at read time; the duplicate rows are still in the table.

### The end labels came off every line chart — 2026-09-23

PM: *"in scoring tabs for all sectors, I see you write the company name at the
end of the graphs and draw a small line to show which line graph is which
company. Remove that, it makes the visualization confusing. I can just hover
and see."*

**What was there:** each line's last value carried its name in the right
margin, de-collided down a 13px ladder with a leader line back to the true
value. It was built because five names inside a 1-5 band overlap routinely —
and the cure was worse than the disease. On a tight band EVERY label is
displaced, so the chart ended in a column of names joined by diagonal leaders
to dots they do not sit beside, and the eye has to trace each one.

**Identity was already answered twice over**, which is what made the third
answer the confusing one: the **legend** above each chart pairs every name with
its colour, and the **hover readout** lists every series at the crosshair date
with its key, value and label (verified live: the score chart returns
`2.17 hindalco / 3.29 hindustan_zinc / …`, the price chart the same names in
percent). Neither was touched.

**The end DOT stays.** It marks where a line stops — a fact about the data
rather than a label, and it cannot be mis-attributed.

**`MR` went 116 → 16 and the plot reclaimed ~100px**, about 12% more width on a
929px chart, which is most of what made a flat 1-5 band hard to read.

**One thing had to move with it, and it is the kind that goes unnoticed:** the
x-axis ticks were all `text-anchor="middle"`, safe only while MR held a 116px
gutter. At MR 16 a centred `23 Sep` on the newest tick overhangs the svg and
clips to `23 Se` — and a truncated axis label just reads as a shorter month,
so nothing would raise. Edge ticks now anchor `end`/`start`; checked
positionally rather than by eye (rightmost text ends at 914 of 929, zero
elements overflowing).

Applies to all four `lineChart()` consumers — score, price, the cement ask
panel and the sector price chart — because it is one function. Verified across
non_ferrous / steel / cement / mining / ems: **0 `.vend` elements, dots
preserved, legends intact.** The dead `.vend` CSS rule is deleted too, not left
behind.

### The economics chart plots ₹/tonne, not the score — 2026-09-23

PM, after asking why SAIL's economics line looked like everyone else's despite
carrying the group's highest coking-coal salience: *"keep the scores as is, but
in the economics graph, give raw ebitda/t. Scores make no sense to me."*

**The score is untouched.** It is still computed, still persisted, still what
the table, the composite and the backtest read. Only the Pair tab's top chart
changed, and only on the economics pillar.

**WHY THE SCORE WAS THE WRONG PICTURE HERE, measured before changing anything.**
The hill curve anchors 5% of EBITDA at 4.0, and on a strong tape the bridged
names sit far past it — the four steel mills ran 1.6x to 5.0x the anchor, so a
**3.09x spread in the underlying (8.17% -> 25.21% of EBITDA) compressed into
0.485 of a 4.00-point scale.** Worse, it **inverted the ranking**: over the
past month SAIL had the largest raw improvement (+26.45pp against Tata's
+10.46pp) and the *smallest* score gain (+2.058 vs Jindal's +2.694), because it
hit the ceiling first, and the cross-sectional spread NARROWED 0.920 -> 0.485
while the raw numbers were fanning out. The counterfactual is the cleanest
statement of it: **deleting the coking coal line from SAIL's bridge entirely
moves its score by 0.028 points** while costing it 3.70% of EBITDA. Not a bug —
invariant 4's flat tail doing exactly what it is specified to do — but it makes
the chart say the opposite of the data in precisely the weeks that matter.

₹/t has no ceiling and separates them: **SAIL +2,515/t against the pack's
+1,394 to +1,486.**

**Plumbing.** `bridge.py` already computed `d_ebitda_per_t` (with `_primary()`
handling SAIL's co-product basis — 16.64mt, not one 8.32mt leg). `run_scores`
now PERSISTS it; `tape.py` exposes `ebitda_per_t` per point and **derives every
point from `d_ebitda_cr` and today's spec tonnage rather than mixing the newly
stored value with derived history** — 174 of 175 stored dates predate the
persist, and two denominators inside one line is the numbers-right-basis-wrong
shape this repo keeps meeting. Stored and derived agree to <0.01% (the gap is
`d_ebitda_cr` being rounded to 1dp before storage). A WITHHELD point stays a
hole, exactly as on the score chart — Coal India's line correctly stops where
its TTM offtake went stale.

**Two things that had to move with it, neither obvious:**

- **Divergence markers are still computed on the SCORE.** `DIV_S = 0.25` is on
  the 1-5 scale; run against ₹/t every session clears a threshold meant to be
  rare and the chart buries itself in triangles. The marker is re-anchored to
  the plotted series' value so it still sits on the visible line.
- **The axis fits the data on ₹/t and stays FIXED at 1-5 for a score.** A score
  chart that rescaled to its own range would make a quiet week look violent.
  Zero is always on the ₹/t axis because the sign is the whole point.

**AND ONE REAL BUG, caught by cross-checking rather than by looking.** The first
version chose per-tonne mode PER ENTITY, falling back to the score when a name
had no ₹/t. `apl_apollo` has **0 of 174** per-tonne points — its economics is a
**spread z-score** (`detail.basis: spread_z`, the HRC-tube spread), with no
tonnage anywhere — so it fell through and drew its 1-5 score on the rupee axis,
hovering as **"+2/t"**. A score rendered as a rupee figure, plausible and
wrong, on a chart nobody would re-check. The mode is now decided ONCE per
chart, and a name with no ₹/t is dropped and NAMED underneath rather than
silently converted.

Verified across every sector x every pillar: the four bridged sectors switch to
₹/t, EMS (which withholds economics) falls back cleanly to the score, and
valuation / mood / guidance / composite are untouched everywhere.
