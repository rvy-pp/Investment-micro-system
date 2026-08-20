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

## What actually caught them

Worth being honest, because it is thin and it is not the code:

- **1** — real data arriving
- **2, 3** — a human thinking "that magnitude is not plausible"
- **4** — the source document happening to contain an independent calculation
- **5** — a second data source disagreeing
- **6** — re-reading the source to check the quote contained the figure

**Nothing in the test suite caught any of them.** Five of six were caught by
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
