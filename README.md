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
| 3 | Scores are relative and gated | `score_runs.dispersion_ok`; "no trade" is a first-class outcome |
| 4 | Every signal has a falsifier | `signals.falsifier` NOT NULL + non-empty CHECK |
| 5 | Everything is replayable | `score_runs.spec_version` + `code_sha`; a spec change is a re-run |

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

## Alpha vs modifier

Broker mood and OI positioning are **not** summed into the composite. Summing
them recommends the most crowded name — consensus bullishness looks like alpha
when it is in fact the reason the move already happened. They adjust conviction
and size, and can veto entry. Direction comes from fundamentals only.

## Modularity test

Adding a sector is **one YAML factor spec plus one entity list. Zero new code.**
Adding a sub-system is one package plus one tab. If a change requires touching
the scoring engine, the spec format is wrong.

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
