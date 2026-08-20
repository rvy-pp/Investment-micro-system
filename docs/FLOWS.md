# Flows — who is buying, not what is worth buying

Opened 2026-08-19 at the PM's request. **This is a scoped placeholder, not a
built section.** The monitors below are named and the schema they land in mostly
exists; almost none of it computes yet. Everything marked *open* is a decision
the PM has not made, and nothing should be implemented past that line without it.

The one thing already settled: **Flows never sets direction.** It answers whether
a correct call can be expressed, and at what size. The moment a flow reading is
added to a pillar as a fifth weighted term, the system starts recommending
whatever is most crowded — the same failure L2 exists to avoid, one layer up.
Same reason `sector_regime.can_express` is a gate and not a coefficient.

---

## It is not a new pillar. It is the layer that was always specified

`specs/sectors/aluminium_primary.yaml` `layer3:` already names five inputs:

```yaml
breadth_pct:     {source: pct_of_sector_above_50dma}
rel_strength:    {source: sector_index_vs_nifty, lookback_days: 60}
turnover_pctile: {source: sector_turnover_vs_own_history, lookback_days: 252}
flow_fii:        {source: fii_sector_flow, lookback_days: 20}
dispersion:      {source: intra_sector_return_dispersion, lookback_days: 20}
```

`sector_regime` has a column for each. **The table holds 0 rows and no code
writes it** — those `source:` handles are names, not implementations. Flows is
the section that computes them, plus the market-wide risk read that has no home
in the schema at all.

So the ordering is: L1 says the economics moved, L2 says whether it is priced,
**Flows says whether anyone is there to pay for it.**

---

## The four monitors

| # | Monitor | Question | Scope | Lands in |
|---|---|---|---|---|
| **F1** | Risk on / risk off | is the market paying for risk at all today | market-wide | **no table yet** |
| **F2** | Sector activity | which sectors are being worked, which are ignored | per sector | `sector_regime` |
| **F3** | Investor sentiment | who is doing it — FII / DII / retail, cash vs derivatives | market + sector | `sector_regime.flow_fii`, else new |
| **F4** | Crowding | is this specific name already full | per entity | `oi` (loaded, unused by scoring) |

**F2 already has its states defined and they are not degrees of one another.**
`out_of_flavour` means actively sold — there is interest, it is negative, and
shorts work. `ignored` means nobody is looking, and nothing expresses in either
direction however good the economics. The spec is right about this; whatever
computes the inputs must preserve the distinction rather than collapsing it onto
a single bullish/bearish axis.

**F1 has no schema and needs a decision, not a column.** `sector_regime` is keyed
`(sector, as_of)`, so a market-wide reading has nowhere to go. Either a
`market_regime (as_of, ...)` table, or the convention `sector = '*'`. The second
is cheaper and worse — every query against the table would then have to know to
exclude a magic row. Recommend a table. *Open.*

---

## What can be computed today, and what cannot

Verified against `data/ims.db` on 2026-08-19, not assumed:

| Input | Status | Blocker |
|---|---|---|
| `dispersion` | **computable now** | none — 3,700 daily closes per name |
| `breadth_pct` | computable, **but see below** | only 5 names carry closes |
| `rel_strength` | **blocked** | no benchmark series loaded. `prices` holds 17 series, none of them an index |
| `turnover_pctile` | **blocked** | `prices.volume` is **NULL on every one of 54,951 rows** |
| `flow_fii` | **blocked** | no source, no adapter, not probed |
| F4 crowding | data present, unused | `oi` holds 90 dates × 4 names, from the vault; nothing in `score/` reads it |

Three findings behind that table, each of the "field exists, nothing fills it"
class this repo keeps getting bitten by:

- **`prices.volume` is dead.** `yahoo_prices.py:212` inserts
  `(entity_id, date, close)` only. The column has existed since the schema was
  written, the Yahoo payload carries volume, and nothing has ever put one in it.
  Any turnover or liquidity measure is a loader change first.
- **`sector_aluminium` is an orphan entity row** with zero price rows and zero
  references anywhere in the tree (`grep` across `*.py`, `*.yaml`, `*.md` returns
  nothing). Someone registered the handle for a sector index and never fed it.
  That is the hook `rel_strength` wants.
- **Breadth over a 3-name peer group is quantised to 0 / 33 / 67 / 100.** The
  spec's `breadth_pct > 60` threshold therefore fires on exactly one name
  flipping its 50dma. Breadth is a statistic about a population, and this
  population is too small for it. Either measure breadth on the **coverage
  bucket** (the spec says `scope: aluminium`, which is the right instinct) with a
  real sector constituent list, or drop the input and say so — a threshold on a
  4-valued variable is a coin flip wearing a decimal point.

---

## Rules Flows inherits, and one it adds

The existing invariants all apply unchanged. Two bite harder here than elsewhere:

- **Silence changes nothing (invariant 3).** A flow reading is only as of the
  print that produced it. When a source is stale, `can_express` is **withheld**,
  not carried forward and not defaulted to permissive. A stale flow reading that
  silently reads "in flavour" is the exact shape of a plausible wrong number.
- **Withhold rather than guess (invariant 7).** No `sector_regime` row without
  the inputs that justify it. `note` exists to say which input was missing.

The rule Flows adds, because its data is the first in the system that is
*published on a lag*:

> **A flow value carries the date the market could have known it.** Indian FII /
> DII cash figures publish after the close; participant-wise F&O OI later still.
> A number stamped with the trading day it describes, rather than the evening it
> was released, backtests beautifully and means nothing. Same look-ahead rule as
> `economics.source_note`, and it needs the same NOT NULL treatment on whatever
> table F1 and F3 land in.

And one distinction to keep in the schema rather than in someone's head: **cash
flows and derivative positioning are different measurements.** FII cash buying
and FII index-future longs answer different questions and routinely disagree. One
column named `flow_fii` cannot hold both. *Open: whether F3 splits into cash and
F&O from the start — recommend yes, it is free now and a migration later.*

---

## Open — to be discussed

Nothing below has an answer yet. Listed so the discussion has an agenda.

1. **What is the risk-on/off read actually made of?** India VIX, advance/decline,
   INR, the gold/copper ratio, small-cap vs large-cap breadth — or one composite.
   Fewer inputs, honestly sourced, beats a rich index nobody can decompose.
2. **Does F1 gate, or only size?** A market-wide risk-off could set
   `can_express = 0` everywhere, or just cut the size multiplier. The spec's pair
   exception already argues a market-neutral pair should survive a regime a naked
   long should not.
3. **Sector universe.** `sector` is the coverage bucket and `peer_group` is the
   scoring universe — F2 measures the bucket, so it needs a constituent list per
   sector, which does not exist yet. NSE sector index membership, or hand-listed?
4. **FII / DII source.** NSE publishes daily; whether it is fetchable without a
   browser session from this machine is **unprobed**. Assume nothing until a
   probe returns a dated row. Sector-level FII flow may not be freely available
   at all, in which case `flow_fii` is a market-level input misfiled.
5. **Does F4 (OI) belong here or in L2?** OI is already loaded, and CLAUDE.md
   lists "OI as a conviction modifier" as not built. Crowding is a positioning
   fact, which is a flow; conviction sizing is L2's job. Pick one home.
6. **What falsifies a flow reading?** Every signal carries a falsifier
   (`signals.falsifier` NOT NULL). A gate should too: the review layer's whole
   purpose here is to test whether the gate cost money or saved it, and
   `l3_gated = 1` is already stored so that test is possible. Define the test
   before the gate goes live, not after.

---

## Build order, when it is agreed

Cheapest-first, and each step is independently useful:

1. `yahoo_prices.py` writes `volume` — one line, unblocks turnover, and should
   backfill.
2. A benchmark and a sector index into `prices` (feeds `sector_aluminium`), which
   unblocks `rel_strength`.
3. `packages/score/flows.py` computes what the data supports and **withholds the
   rest by name**, writing `sector_regime` rows with a `note` saying which inputs
   were absent. A partial regime row that says what it is missing is the useful
   artefact; a complete-looking one built on two of five inputs is not.
4. F1 / F3 only after the source question (4) is settled by a probe.
5. The gate stays **recorded and non-binding** until the review layer can grade
   it. `signals.l3_gated` exists for exactly this — compute the gate, store what
   it would have blocked, and let it start blocking only when there is evidence
   it should.

The system-wide gate still stands and applies here too: **more dates, not more
layers.** Flows is scoped now because the PM asked for it to be scoped; it is not
evidence that anything is ready to be extended.
