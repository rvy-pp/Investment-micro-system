# Investment Micro-System

Investment micro-system for a long/short book across Steel, Cement, Aluminium,
Mining, EMS, IT and Autos (later: Power, Oil & Gas, Defence, Capital Goods).

Runs entirely on localhost. No hosting, no cloud services, no recurring cost.

---

## The one inversion this system is built on

**The model extracts facts. Deterministic code scores them.**

The previous attempt did the opposite — an LLM read broker prose and emitted a
score directly. That produces numbers which are not reproducible, not
decomposable, and not testable, which is why the output read as jargon rather
than direction.

## Five commitments, enforced in SQL rather than by convention

| # | Commitment | Where it is enforced |
|---|---|---|
| 1 | No number without a citation | `observations.quote` NOT NULL + non-empty CHECK |
| 2 | Silence changes nothing | no decay column exists; staleness is derived in `v_factor_freshness` |
| 3 | Output is gated, not always-on | `sector_regime.can_express`, `bridge_results.coverage_ok`; "no trade" and "cannot tell" are both first-class |
| 4 | Every signal has a falsifier | `signals.falsifier` NOT NULL + non-empty CHECK |
| 5 | Everything is replayable | `bridge_runs.spec_version` + `code_sha`; a spec change is a re-run |
| 6 | No intensity without provenance | `economics.source_note` NOT NULL + non-empty CHECK |

Verify all of these actually hold, rather than trusting this table:

```bash
python packages/core/init_db.py
```

It applies the schema and then attempts three illegal inserts, each of which
must be rejected.

## Layers

| Layer | Job | Who |
|---|---|---|
| **L0** `adapters/` | sources → immutable dated raw captures | code |
| **L1** `extract/` | raw → typed **cited** observations | LLM, its only contact with data |
| **L2** `core/` | SQLite store, append-only observations | code |
| **L3** `score/` | factor specs → normalised factors, composites, pairs, signals | deterministic code |
| **L4** `api/` + `web/` | stdlib `http.server` over SQLite, serving one static page | code |
| **L5** review | joins signals to outcomes, grades decisions, promotes priors | LLM, built last |

**Flows** (`docs/FLOWS.md`) is the section that computes L3 — investor
sentiment, sector activity, risk on / risk off, crowding. Scoped 2026-08-19,
largely open, nothing built.

## Two ideas the schema keeps separate

- `sector` — your coverage bucket. Organisational.
- `peer_group` — the scoring universe: names that share a factor basis and are
  therefore rankable against one another.

They genuinely differ. The Aluminium bucket holds Hindustan Zinc (an LME-zinc name), which is scored in
the `zinc` peer group instead. Post-demerger the aluminium peer group is
Hindalco, NALCO and VAML — all three directly tradeable. Scoring keys on
`peer_group`; `is_tradeable = 0` names carry observations but can never receive
a score, because a score you cannot express in the book is not an output.

## Three layers, each able to stop the process

There is **no generic factor-weight model**. Companies do not share a factor
structure; they share an arithmetic.

| | Question | Determines |
|---|---|---|
| **L1** Economics | Did the economics actually change, and by how much? | direction + size |
| **L2** Priced in | Is it already in the price? | conviction |
| **L3** Regime | Can the sector express it at all — in or out of flavour? | permission |

**L1** is arithmetic on each company's own cost stack (consumption intensity ×
the price series driving it × how much is actually bought at market) and revenue
stack (mix, volume, ASP vs benchmark), producing ₹ and basis points — not a
score. The field doing the work is `market_pct`: a captive input contributes
zero to cost however far its market price moves. That is why one alumina print
moves three aluminium names in three directions, without anyone asserting a
coefficient:

| | alumina exposure | mechanism |
|---|---|---|
| NALCO | **revenue** | smelter alumina is captive (`market_pct` 0.00) *plus* a marked-to-market surplus output line |
| VAML | **cost** | buys ~25% of its alumina (`market_pct` 0.25), no surplus line |
| Hindalco | **~neutral** | broadly self-sufficient, small surplus |

**L2** never sets direction. Summing consensus mood into a score is how a system
ends up recommending the most crowded name — the bullishness *is* the reason the
move already happened. It adjusts conviction and size, and can veto entry.

**L3** is a permission layer, not another additive term. Good news into a sector
with no investor interest does not move stocks. Gated signals are still computed
and stored with `l3_gated = 1`, so the review layer can later test whether the
gate cost money or saved it.

## Modularity test

Adding a sector is **one economics spec plus one layer config. Zero new code.**
Adding a sub-system is one package plus one tab. If a change requires touching
the bridge engine, the spec format is wrong.

**Tested for real on 2026-08-25, when steel was added, and it very nearly held.**
Seven companies across four peer groups needed `specs/entities/steel.yaml` and
`specs/sectors/steel.yaml` and **nothing whatsoever in `packages/score/`** — the
bridge, the scorer and the persist step all picked them up from disk, and the run
went from 26 to 61 `pillar_scores` rows the moment the two YAML files landed.

Two exceptions, both in L4 rather than in the engine, and both were hardcoded
lists rather than logic:

- `api/engine.py` `SECTORS` — the nav is a data edit by design, so this one is
  the intended cost, not a violation.
- `api/serve.py` `/api/scores` named `aluminium_primary` and `zinc` as literals.
  This is the one that mattered: adding steel to `SECTORS` made the tab appear in
  the nav and stay **absent from `/api/scores`**, so the tab would render with an
  empty Bridge view and nothing would say why. Now derived from `SECTORS`.

A third near-miss worth recording, because it was silent rather than visible:
`adapters/yahoo_prices.py` held the equity roster as a literal tuple in **two**
places, and adding a name to `CANDIDATES` without editing both inserted it into
`entities` with `kind='commodity'`. No error, a plausible row, and
`_series_in_store()` would then count an equity as a priceable input series. One
`EQUITIES` set now.

## Layout

```
packages/
  core/       schema.sql, init_db.py, store access
  adapters/   L0: broker mail, prices, NSE OI, commodities, IMS paste
  extract/    L1: extraction prompts + the validator that rejects uncited rows
  score/      L3: engine (generic) — all sector knowledge lives in specs/
  api/        L4: read-only stdlib http.server over SQLite
  web/        L4: one static page, served by api/
specs/
  sectors/    layer config, one per peer_group
  entities/   rosters with per-company cost and revenue structure
  extracted/  cited facts: guidance, policy, prices, PM overrides
data/         gitignored — ims.db + raw captures
snapshots/    gitignored — dated db backups
```

## Run it

Double-click **`launch\Investment Micro-System.vbs`**, or point a Desktop
shortcut at it. It refreshes the scores, starts the server and opens the page.

```bash
python packages/core/init_db.py       # apply schema, run the guard tests
python packages/refresh.py            # the daily refresh: feeds, gates, score, persist
python packages/pipeline.py           # dry run of the whole sequence; --run to execute
python packages/api/serve.py          # -> http://127.0.0.1:8770
```

Port 8770, not 8765 — the vault's node dashboard already owns 8765.

`refresh.py` is what an unattended run is allowed to do: equity closes,
corporate-action scan, preflight, score-and-persist. It cannot do Wind zinc,
broker mail or the metals pack — two need an agent, one needs a person — and it
prints that list every run rather than implying completeness it does not have.

## Status

**Six peer groups score on live data across two sectors**, 13 entities, as of
2026-08-25.

| sector | peer group | names |
|---|---|---|
| Non-Ferrous | `aluminium_primary` | nalco, hindalco, vaml |
| | `zinc` | hindustan_zinc, vedanta |
| Steel | `steel_integrated` | tata_steel, jsw_steel, jindal_steel, sail |
| | `steel_converter` | apl_apollo |
| | `steel_stainless` | jindal_stainless — economics **withheld** |
| | `steel_secondary` | shyam_metalics — economics **withheld** |

Aluminium is still the proving ground: alumina's sign flips across the group,
which is exactly what an absolute per-company score cannot express. **Steel is
the second instance of the same structure and the reason to believe it
generalises** — iron ore is captive for Tata and SAIL and bought by JSW and
Jindal Steel, so one ore print separates them by ~18x, while coking coal (which
nobody in India holds captive) hits all four and differentiates only by EBITDA/t.
APL Apollo inverts the *output* line, which nothing in non-ferrous does: it buys
hot-rolled coil, so rising HRC is a cost to it and revenue to every mill.

The last two steel groups score valuation and mood but emit **no economics at
all**, deliberately. Stainless needs ferrochrome and stainless scrap and the pack
carries neither; Shyam is four segments including aluminium on a sponge-iron cost
route. A bridge missing most of its cost stack while reporting `coverage_ok` is
the exact failure this project spends the most effort avoiding, so those two
withhold — and the withhold is *recorded* as a row, not left as a gap.

**All four pillars score and persist.** `pillar_scores` holds 5,618 rows across
1,377 dates. Composite as of 2026-08-25: vedanta 3.85 > hindustan_zinc 3.77 >
sail 3.19 > vaml 2.79 > tata_steel 2.68 > nalco 2.62 > apl_apollo 2.58 >
jindal_steel 2.57 > jindal_stainless 2.56 > hindalco 2.55 > jsw_steel 2.43 >
shyam_metalics 2.16.

**Guidance is withheld on all seven steel names** — nothing loaded yet. Steel
looks like the best P4 candidate in the book, better than HZL: the guidance is
numeric, dated, per-tonne and resolves every quarter, and the digests already
carry both the commitment and the outcome.

VAML carries the widest pillar spread — mood 3.77 against valuation 2.03 — and
lands on the *same* composite as Hindalco, whose pillars broadly agree. That is
why `combined.py` reports spread and not just the average: an average alone
makes those two look identical.

**The front end covers non-ferrous as of 2026-08-21.** The Pair tab reads the
persisted tape: pick a long and a short, and the pair score is charted against
the pair's 1:1 relative price, with every name's score and price below it. The
pair score is the **spread run through that pillar's own curve** — extended to
valuation and mood, which `scoring.yaml` always asked for and nothing did. The
Bridge, Inputs, Positioning and Guidance tabs are unchanged and still recompute
live.

**Not built:** `signals` (no directional call with a falsifier is emitted),
`outcomes` (nothing grades them), the in-flavour/out-of-flavour regime gate
(scoped as Flows — see `docs/FLOWS.md`), OI as a conviction modifier, book
ingestion, cement's P4 guidance ledger, and Mining, EMS, IT and Autos.
**Cement went LIVE 2026-08-28** — four scored F&O names (ultratech, ambuja,
shree, dalmia) on economics + valuation + mood, regional output prices from
the Daily Cement Pack, an IndiaMART day-to-day watch, and P4 withheld until a
guidance ledger exists.

**The gate still standing:** the backtest has now been RUN
(`packages/review/backtest.py`) and the honest answer is *not yet decidable*.
Composite IC over the 40 stored dates is −0.17 at a 5-day horizon, but with
roughly 7 non-overlapping windows over 5 correlated names nothing approaches
significance, and leave-one-out shows the entire negative sign is one name:
drop Vedanta and every horizon turns positive. So the gate is not passed and not
failed — the sample cannot answer it. It needs more dates, not more pillars.
`specs/sectors/aluminium_primary.yaml` under `validation` sets the bar: the
ranking must predict realised relative moves, with high-conviction calls beating
low-conviction ones. A dashboard over an unvalidated scoring engine is how the
last attempt produced output nobody could trade.

**That gate was not cleared before steel was added, and the extension was made
anyway — the PM's call, 2026-08-25.** The caution above is kept rather than
softened, because it is still the honest statement of what is and is not known.
Two things make the decision defensible, and one does not:

- Steel does not *depend* on the aluminium result. It is a second, structurally
  independent instance of the same arithmetic, and if the approach works it
  should reproduce there — so it is closer to a replication than to a bet on an
  unvalidated model.
- The binding constraint on the original gate is **sample size, which only time
  supplies.** Steel adds seven names in a second, less-correlated complex, which
  is the one thing that actually shortens the wait for an answerable backtest.
- Against that: **nothing about steel makes the aluminium IC decidable**, and
  there are now two sectors' worth of scores that no backtest has graded. Do not
  read a steel composite as validated. It is not.

**Steel's own gate is narrower and sharper**, and it is a data gap rather than a
statistical one. `market_pct` on the iron ore lines produces the entire result,
and only **one of four** captive shares is cited anywhere in the 48 digests —
Jindal Steel's "captive iron ore targeted ~40% by FY27-end", which is a forward
target rather than today's share. JSW has a 2030 target. Tata and SAIL have
nothing but sector knowledge. So the structural tests in
`specs/sectors/steel.yaml` confirm the **arithmetic** works, not that the
**inputs** are right. Confirm all four against annual reports before sizing
anything off that group.

## Working on this

Read **`CLAUDE.md`** first, then `git log`. Commit messages are written as a
findings record: what was found, what was fixed, what was deliberately left
alone and why. Several design choices look arbitrary until you see the bug they
prevent.
