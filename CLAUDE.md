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
`outcomes` (nothing grades them), the in-flavour/out-of-flavour regime gate,
OI as a conviction modifier, book ingestion, steel and the other sectors.

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

**More dates, not more pillars.** The gate stays shut on extending to a second
sector, and the binding constraint is now sample size, which only time supplies.

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
