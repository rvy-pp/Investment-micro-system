# PinPOINT IMS

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
| **L4** `api/` + `web/` | FastAPI over SQLite, serving a Vite-built SPA | code |
| **L5** review | joins signals to outcomes, grades decisions, promotes priors | LLM, built last |

## Two ideas the schema keeps separate

- `sector` — your coverage bucket. Organisational.
- `peer_group` — the scoring universe: names that share a factor basis and are
  therefore rankable against one another.

They genuinely differ. The Aluminium bucket holds Hindustan Zinc (an LME-zinc
name) and Vedanta Aluminium (an unlisted division with no ticker). Only
Hindalco, NALCO and Vedanta Ltd are mutually rankable. Scoring keys on
`peer_group`; `is_tradeable = 0` names carry observations but can never receive
a score, because a score you cannot express in the book is not an output.

## Three layers, each able to stop the process

There is **no generic factor-weight model**. Companies do not share a factor
structure; they share an arithmetic.

| | Question | Determines |
|---|---|---|
| **L1** Economics | Did the economics actually change, and by how much? | direction + size |
| **L2** Priced in | Is it already in the price? | conviction |
| **L3** Regime | Can the sector express it at all — tezi or mandi? | permission |

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
| VAML | **cost** | buys ~half its alumina (`market_pct` 0.50), no surplus line |
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

## Layout

```
packages/
  core/       schema.sql, init_db.py, store access
  adapters/   L0: broker mail, prices, NSE OI, commodities, IMS paste
  extract/    L1: extraction prompts + the validator that rejects uncited rows
  score/      L3: engine (generic) — all sector knowledge lives in specs/
  api/        L4: read-only FastAPI over SQLite
  web/        L4: Vite SPA, built to static, served by api/
specs/
  sectors/    factor specs, one per peer_group
  entities/   rosters with per-entity signed factor exposures
data/         gitignored — ims.db + raw captures
snapshots/    gitignored — dated db backups
```

## Status

Aluminium is the proving ground: 3 tradeable names, the cleanest external
driver, and a factor whose sign flips across the group (alumina is revenue for
NALCO, cost for Vedanta), which is precisely what an absolute per-company score
cannot express.

**The front-end is deliberately last.** The gate is in
`specs/sectors/aluminium_primary.yaml` under `validation`: the ranking must
predict realised relative moves on a 30-day backfill, with high-conviction
calls beating low-conviction ones. A dashboard over an unvalidated scoring
engine is how the last attempt produced output nobody could trade.
