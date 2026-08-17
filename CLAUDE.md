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

**The gate that still stands:** 40 days of stored scores now exist, so the
backtest is finally *possible* — does the composite predict realised relative
moves, and do high-spread names behave differently? Nothing should be extended
to a second sector before that is answered.

**Numbers still provisional** (`verify:` in the specs): all intensities, NALCO's
alumina surplus tonnage, VAML's alumina `market_pct`, the coal
`basis_pass_through` of 0.35, Hindalco's `base_ebitda` (the only unsourced
denominator), VAML's derived share count, and VEDL's **pre-demerger** net debt —
which makes VEDL read dearer than it is, so its cheapness survives its own bias.
