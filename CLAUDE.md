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

**Flows** is the fifth section, opened 2026-08-19 and scoped only — investor
sentiment, which sectors are active, risk on / risk off, crowding. It is NOT a
fifth pillar: it computes the L3 gate's five specified-but-uncomputed inputs and
never sets direction. Read `docs/FLOWS.md` before touching `sector_regime`; most
of it is still open questions for the PM.

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

**No scheduled task is registered, by the PM's choice 2026-08-21.** The launcher
refreshes on every double-click, so the scores are current whenever the page is
open and nothing runs when it is not. `launch\Install Daily Task.bat` is written
and unrun if that changes.

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

## Price sources have a precedence order — read before adding a feed

Added 2026-08-21. Four adapters wrote `prices` with `INSERT OR REPLACE` and no
`source` column, so the last one to run owned every overlapping date and nothing
recorded who that was. `prices.source` now exists and every adapter writes
through **`packages/core/prices_io.py`**:

| rank | source | |
|---|---|---|
| 40 | `metals_pack` | licensed, hand-dropped, the desk's own reference |
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
Cement, and Mining / EMS / IT / Autos. **Steel IS built as of 2026-08-25** — see
the section below.

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
