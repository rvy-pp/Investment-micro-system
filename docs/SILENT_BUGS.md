# The silent-arithmetic bug register

**One failure shape has produced every serious bug in this project.** Not a
crash, not an exception — a lookup or a divisor that is quietly wrong, returns a
*plausible* number, raises nothing, and leaves `coverage_ok` true. The output
looks like an answer. It survives review, gets written up, and is sometimes acted
on before anyone notices.

This file exists so the pattern is recognised on sight rather than rediscovered.

## The standing rule

**When one of these is found, fix it AND leave a disclaimer at the site.** A
comment naming the wrong value, the right value, and how it was caught. Not a
changelog entry — a comment where the next person will be standing.

The reason is specific to this class: the code after the fix looks exactly like
the code before it. Nothing about `n_reported` reads as more correct than
`n_elapsed`. Without the note the fix is invisible and reversible by anyone
tidying up. Every entry below is commented in place; keep it that way.

## The register

| # | Where | Wrong | Right | How it was caught |
|---|---|---|---|---|
| 1 | `schema.sql` date CHECKs | `GLOB '____-__-__'` — in GLOB `_` is a **literal underscore**, so it rejected every valid date | `GLOB '[0-9][0-9][0-9][0-9]-...'` | the price loader failed on real data. **Every guard test passed** — they only checked that bad rows are REJECTED |
| 2 | `base_financials.usdinr` | hardcoded `87.0` against an actual `95.43` — scaled **every** USD-linked line | read the live series | a 9.7% error that never looked wrong, just understated everything |
| 3 | `units[lme_zinc]` missing | `to_inr` converts only when the unit starts with `"USD"` and otherwise returns the delta **unchanged** — dropped the FX leg, **95×** understatement | register `lme_zinc: USD/t` | a magnitude sanity check: ₹32 cr for a 10% zinc move on 820kt was not plausible. It had already survived a full HZL analysis and two written reports |
| 4 | `guidance_runrate` annualisation | divided reported quarters by **time elapsed** — `round(0.39×4)=2` halved one reported quarter, reading **−52.7%** for a **−5.5%** gap | divide by periods **reported**; use elapsed time only to weight confidence | the source concall contained a human's independent arithmetic — *"Q1 260 KT annualises ~1.04 Mt"* — to disagree with |
| 6 | NALCO Q1 actuals, 2026-08-20 | loaded metal 119,000 t and alumina 575,000 t as `factor='actual'` — **both DERIVED from run-rate statements; the transcript states neither** | remove them; those commitments withhold | re-reading the transcript to confirm the quote actually contained the number |
| 5 | `beta_stability` silver "roll" | classified a **real −26.4% move** as a contract roll and excluded a 30-day window, discarding 63 date-entity pairs | it appears in **both** the spot assessment and the futures, so it was never a roll | a second, independent price source |

| 7 | `base_financials.net_debt`, steel | screener.in `Borrowings - Investments` as a net-debt proxy. screener's "Investments" is **non-current investments only** — no cash, no current investments — so it overstated net debt on **every** steel name with a cited figure, worst JSW **+65%** (89,092 vs 53,900), and **flipped the sign** on APL Apollo (+449 vs **-1,410 net cash**) | the digest-cited 1QFY27 figure, with the screener number kept beside it as the recorded disagreement | cross-checking a *derived* figure against a *cited* one. Three brokers independently quote Tata at Rs842bn, and JSW's own "net debt down to Rs462bn" cannot be reconciled with Rs99,310 cr of gross borrowings unless ~Rs45,000 cr of liquidity sits outside the subtracted line |

### Entry 6 is a different mechanism with the same consequence, and it exposes a gap

The other five are wrong arithmetic. This one is a **fabricated fact wearing a
citation**, and it matters because it slipped past the constraint designed to stop
exactly that.

`observations.quote` is `NOT NULL` with `length(trim(quote)) > 0`. That enforces
*a quote exists* — **not that the quote contains the number in `value_num`**. So a
row can carry an honest-looking quote about run-rates and a `value_num` nobody
ever said. Invariant 1 reads "no number without a citation"; what SQL actually
enforces is "no number without *some* text".

No constraint can close this — checking that a figure appears in its own quote
would defeat any unit change ("8.80 lakh tons" vs `880000`). It is a discipline,
and the discipline is: **the number must be IN the quote, in the source's own
words, or the row does not get written.** "Best-ever Q1 hydrate production" is not
an actual. A withheld commitment is the correct output when a company does not
disclose the quarter.

The tell to look for: a quote that explains *how* the number was arrived at
rather than *stating* it. Entry 6's quotes read "taken as one quarter of the
run-rate management states it is operating at" — which is reasoning, and
reasoning in a `quote` field means the number is derived.

### Entry 7 is the first one where measuring the impact CHANGED THE CONCLUSION

Recorded because the mistake is instructive and it was mine, in the first draft of
the note now sitting in `base_financials.yaml`. That draft said the uneven bias
"corrupts the cross-sectional ranking that P3 exists to produce". It was written
from the rupee table, which is genuinely alarming — a 65% error and a sign flip.

Then it was measured, and it is not true. **Market cap dominates EV for all seven
names, so a 65% error in net debt is 11.6% in the multiple:**

| name | EV/EBITDA cited | convention | delta |
|---|---|---|---|
| tata_steel | 8.47x | 8.50x | +0.3% |
| jindal_steel | 12.51x | 12.84x | +2.7% |
| apl_apollo | 35.27x | 36.40x | +3.2% |
| jindal_stainless | 11.42x | 11.98x | +4.9% |
| jsw_steel | 9.84x | 10.98x | **+11.6%** |

On the prices of the day it was found, the convention changes **no name's rank**
in the group — JSW sits between shyam and jindal_stainless either way. So the real
finding is narrower and more useful than the draft: **JSW is the one material
distortion, and APL Apollo's is wrong in KIND rather than in size** (a net-cash
company recorded as levered). Both worth fixing; neither is a ranking disaster.

**The lesson generalises past this entry.** An input error and its output error
are different magnitudes, and for a bug class defined by *plausible* numbers the
temptation runs the other way once you find one — to state the scariest true
number rather than the relevant one. A ratio whose denominator is dominated by a
correct term absorbs a lot of error in the other term. Size the consequence in the
units the decision is actually made in, and do it before writing the note, not
after.

**What is NOT settled, and is deliberately left open.** The five non-ferrous names
still use the convention. Hindalco's figure was cross-checked to within 2% of a
cited Novelis number and HZL/NALCO are net cash, so the failure may be specific to
levered names holding large cash balances — but nobody has checked
hindalco/vaml/vedanta against a cited level. That check is worth running on its
own; it must not ride along inside an unrelated commit.

## What actually caught them

Worth being honest, because it is thin and it is not the code:

- **1** — real data arriving
- **2, 3** — a human thinking "that magnitude is not plausible"
- **4** — the source document happening to contain an independent calculation
- **5** — a second data source disagreeing
- **6** — re-reading the source to check the quote contained the figure
- **7** — cross-checking a derived figure against a cited one, in a sector where the sell-side happens to quote the same quantity

**Nothing in the test suite caught any of them.** Six of seven were caught by
somebody eyeballing a number and finding it implausible, or re-reading a source.
That is not a process.

## The defences that generalise

Each of these came out of an entry above and would have caught it:

1. **Every guard needs an ACCEPTANCE test, not just a rejection test.** A
   constraint that rejects everything passes every rejection test. From #1.
2. **A lookup miss must fail loud, never fall through to a default.** `to_inr`
   silently skipping conversion on an unknown unit is #3. `packages/core/preflight.py`
   now checks every `price_link` has a unit and HALTS.
3. **Sanity-check the magnitude against something you already know.** "A 10% move
   in the main revenue line should be worth roughly X% of EBITDA." Both #2 and #3
   were caught this way and nothing else would have.
4. **Prefer a source that carries its own arithmetic.** The concall note stating
   its own annualisation is what caught #4. When ingesting a document that shows
   its working, reproduce the working and compare — that is a free test.
5. **Two sources beat one.** #5 was only decidable because two independent price
   series disagreed about whether a move was real.
6. **Distinguish "no data" from "zero".** A withheld score must be a recorded
   withholding, never a gap or a neutral 3.0.

## Before shipping any new arithmetic

- [ ] does a wrong lookup here return a plausible number instead of raising?
- [ ] is there a magnitude I already know, to check against?
- [ ] does the source document state its own answer, so I can reproduce it?
- [ ] is "no data" distinguishable from "zero" in the output?
- [ ] if this divisor were wrong by 2×, would anything complain?
- [ ] for every stored fact: is the number **in** the quote, in the source's words?

The last one is the whole file. In #3 and #4 the answer was no.
