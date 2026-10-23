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

| 8 | `cement_pack._stamp`, 2026-08-27 | stamped every month column at **month end**, so the pack's August column — a month-TO-DATE average, read on 27 August — was written as `2026-08-31`. Six rows dated in the **future**. `bridge.py` takes its default `as_of` from `MAX(date) FROM prices`, so one stamp moved **every pillar's as_of forward four days**, silently, six series still `coverage_ok` | `min(month_end, capture_date)`, capture date taken from the staging FILENAME so a re-load of an old capture cannot re-date it to now; a future capture date is refused | running `SELECT MAX(date)` after the load. Nothing raised — the load reported "894 rows" and the Cement tab rendered 11/11 priced. `westmetall.py` already refuses a future date; this had no equivalent until it was copied over |
| 8b | the same store clock, five call sites | with the future dates gone, `MAX(date) FROM prices` was **still** the wrong clock: cement's in-progress month is stamped at the CAPTURE date, so MAX(date) reads today while every equity close is yesterday's, and `run_scores.py` would persist a score dated a day after the prices it is made of — against 1,407 stored dates where the two are the same day | `series.latest_daily_date()`, measuring cadence rather than listing ids; wired into `run_scores`, `bridge`, `mood`, `mood_bias`, `whatif` | noticed while checking what entry 8's fix had left behind. **Removing the future date fixed the symptom and not the cause** — which is the general shape here, and the reason the register records both halves |
| 7 | `base_financials.net_debt`, steel | screener.in `Borrowings - Investments` as a net-debt proxy. screener's "Investments" is **non-current investments only** — no cash, no current investments — so it overstated net debt on **every** steel name with a cited figure, worst JSW **+65%** (89,092 vs 53,900), and **flipped the sign** on APL Apollo (+449 vs **-1,410 net cash**) | the digest-cited 1QFY27 figure, with the screener number kept beside it as the recorded disagreement | cross-checking a *derived* figure against a *cited* one. Three brokers independently quote Tata at Rs842bn, and JSW's own "net debt down to Rs462bn" cannot be reconciled with Rs99,310 cr of gross borrowings unless ~Rs45,000 cr of liquidity sits outside the subtracted line |
| 9 | `prices`, NSE holidays — found 2026-09-17, OPEN | Yahoo returns a **placeholder bar for a day the exchange was shut** and `yahoo_prices` loads it. On 2026-09-14 (an NSE holiday) 35 equities carry a row with `open == high == low == close` and `volume = 0`, and **34 of the 35 repeat the 2026-09-11 close to the paisa**. It is a guaranteed zero-return session injected into every equity series — and the five names that got NO placeholder then measure the next session over two days while the rest measure one | **not fixed at the store.** `engine.book_index` refuses such a date for its own chart (no range and no volume on every member that priced); the loader-side guard and the existing rows are the PM's call | building the live index: its all-or-nothing completeness rule started naming dates, and 09-14 named itself by being flat for everyone at once |
| 10 | `yahoo_prices.fetch_bars`, `range=max` — found 2026-09-17 | `interval=1d` is a REQUEST, not a contract. `range=max` returns `dataGranularity: 1mo` for a long history (**370 month-END bars** for TATASTEEL.NS, 1996-2026) and `1h` for a short one (VAML.NS: **481 hourly bars over 69 dates**). Nothing read `meta.dataGranularity`, so a deep backfill would have written monthly candles into `prices` as sessions, or a date carrying five bars of which the last silently wins. The closes are REAL closes — of the wrong period | assert `meta.dataGranularity == '1d'` and refuse otherwise; `rng` takes an ISO date, routed through `period1`/`period2`, which holds `1d` out to 31 years | probing the endpoint before a backfill rather than after. `max` on a 31-year symbol returning 370 rows is only suspicious if you happen to divide |

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

### Entry 9 is the first one caught by a NEW consumer rather than by a person

The other eight were found by somebody eyeballing a number, re-reading a source,
or watching two sources disagree. This one was found because a chart was written
whose arithmetic *could not proceed* without deciding what counts as a session —
and having to state the rule is what exposed the rows that break it.

That is worth naming as a defence, because it is repeatable and the others are
not: **a new reader of old data is a test of that data.** The phantom rows had
been sitting in `prices` doing nothing visibly wrong. Every chart drew them as a
flat candle, which looks like a quiet day; `book_ohlc` takes an intersection
across its legs, so it silently dropped the sessions where the placeholder was
missing rather than reporting them. Nothing raised, nothing looked wrong, and
nothing would have.

**Two things about it are still open and are the PM's call, not a tidy-up.**

The rows are **still in `prices`**. `book_index` cleans only what it reads, and
says so on the page. Deleting them touches every chart, every window and
potentially the dates scores are stamped on, which is a decision with a blast
radius rather than a fix.

And the loader has **no guard**. The test that works is the tape's own — no
range and no volume is not a session — applied across names, so a single halted
stock on a real session can never take the day out. It belongs in
`yahoo_prices`/`prices_io` beside the identity and stale checks that already
stop `ZN=F` being loaded as zinc. Note it is NOT a holiday calendar: a calendar
is another thing to maintain and would be wrong the first year it is not
updated, where the volume test reads the fact directly.

**A related hole that is not a placeholder at all**, found in the same pass:
2026-09-16 carries no bar for eight of the book's names. This was first written
up here as "the equity fetch dropped ten names", which was wrong — checked
against the endpoint directly on 2026-09-17, **Yahoo itself has no bar for
those names on that date**. A source hole and a holiday look identical from
inside a chart (both are "a name has no row") and they need opposite handling,
which is why the index distinguishes them and reports them separately. The
correction matters because the two imply different fixes: a failed fetch wants
a retry, and a source hole wants the session skipped.


### 11. An indicator that was structurally incapable of moving — and the "fix" that would have been worse

*Found 2026-09-20, on the PM asking "what happened to the cement price check?"*

`indiamart_cement.py` reported the **matched-pair MEDIAN** % change per region
on the Overview, live since 2026-09-11. Across 20 captures x 5 regions, all
**95 readings were exactly +0.000**. Not approximately — exactly, every one.

It was not a quiet market. **~40% of matched listings re-price every single
day** (23.5%-45.3%), and 1,074 of 1,569 listings have moved at some point, some
290 -> 430 Rs/bag. The median reads zero because it only leaves zero when MORE
THAN HALF the panel moves the same day, and the most that has ever moved at all
is 45.3%. **The statistic could not fire, at any threshold.** On the page it
read as a calm cement market — which is the failure shape exactly: plausible,
silent, `state: live`, no error anywhere, and a `--report` footer cheerfully
advising that the thresholds just needed tuning.

**The obvious repair is the one that had to be refused.** Switching to the
plain arithmetic mean produces a number that moves — and it is biased UP, hard.
A listing flipping 290 <-> 430 reads **+48.3% up and -32.6% back**, so the mean
of a round trip on an UNCHANGED price is **+7.86% per leg**. With ~40% of the
panel flipping daily and direction a coin flip (up-share 46.8-52.6% over 19
days), that accrues: **compounding the daily plain means over the 19 stored
day-pairs gives +10.72%, against a panel whose own median level went
-1.47/0/0/0/0.** It was positive on **19 days out of 19**. At `ALERT_PCT = 1.0`
it would have alerted most weeks, on nothing.

The fix is the **mean of log changes**, which cancels the flip by construction
(+39.5% / -39.5%) and compounds to **+0.55%** against a true ~0%. It has
variance — it ranges -0.40 to +0.30 and goes both ways — so unlike the median
it can actually say something. The median rides along in
`matched_median_pct`, the count that moved at all in `n_moved`, and the report
prints `moved%` beside the move, because **+0.00 across a panel where 40%
re-priced and +0.00 across a panel that did not move are different statements
and the headline number cannot tell them apart.**

**A second, smaller bug found in the same function.** The matched-pair
comparison built `{product_id: price}` over rows whose PK is
`(capture_date, product_id, city, category)`, so one id legitimately appearing
under several cities silently kept **whichever row SQLite returned last** —
727 such collisions, up to 4 copies. The same listing could therefore appear to
move between two copies of itself. `_panel()` now averages.

**What this one adds to the register:** every earlier entry is a wrong number.
This is a number that was RIGHT and could not ever be interesting — a
correctly-computed statistic with no power. The question the checklist was
missing: *can this number move at all, and what would it take?* For a median
over a panel, the answer is "more than half of it, together", and nobody had
asked.

### 12. A scaling law assumed, not measured — Kelly's 13-week variance

*Found 2026-10-06, on the PM asking whether pairs are normally distributed.*

`kelly.compute` sized every pair as if its 13-week variance were **65 × the
daily variance**, the √time law that holds only when daily moves are
independent. Same-sector spreads partly mean-revert inside a quarter: the
Lo–MacKinlay variance ratio over each pair's history since 2007 pools to
**0.646** (13-week σ at 0.80× the √65-scaled daily σ), and 15 of 16 pairs sit
below 1. Kelly divides by variance, so every size was **1.55× too small** and
every implied edge 1.55× too large. TCS_INFO at a +9% edge: 929% of NAV gross
then, 1,438% now.

Nothing about the old output looked wrong. The sizes were already far above
anything the book runs, so being 35% short changed no decision anyone could
see, and the "at today's gross" allocation, the column actually read, is
identical under a common ratio. It was found by testing the assumption
directly (variance of rolling 13-week sums against daily × 65), not through a
symptom.

**What this one adds:** the divisor was not mis-coded. It was a modelling
assumption that the code made silently, by converting the edge to a per-
session μ. The defence is the same as for a period you did not check (#10):
when arithmetic moves a number from one horizon to another, measure the
scaling on the data rather than assume it. A related trap the fix avoids: the
per-pair ratios range from 0.41 to 1.13, but their spread equals sampling
noise exactly (0.0325 vs 0.0326), so using them would have swapped one
silent error for 16 noisy ones. They are pooled, with a measured shrink that
lets a pair keep its own ratio the day it becomes distinguishable.

### 13. A missing session dropped from one side only — the sector split

*Found 2026-10-22, by the first live run of `move_check.py`.*

`sector_rel.split` paired the stock's daily returns with the basket's day by
day and kept only days present in BOTH. LTIMindtree has no 21-Oct close (a hole
at Yahoo), so its 22-Oct return spanned two sessions, while the basket's
21-Oct return, an IT rally, was simply dropped. The stock side kept the day
and the sector side lost it. The 7-day split read **sector −0.0% / own +1.4%**
while every IT peer was up 5–9%. The Book tab's 60-day column read **own
−3.4** against a true **−6.4**. Plausible numbers, signed sensibly, nothing
raised.

A second shape sat behind it. A basket day counted whenever ANY member printed,
so with only the pair legs' intraday bars loaded, each leg's "sector" for that
day was its own pair partner: TechM's −2.1% standing in for eleven IT names.

**Fix:** both sides now run between the common dates of the stock's closes
and the basket's compounded level, so a session the stock lacks is carried by
the next interval on both sides. A basket day needs half its members
(`QUORUM`, the `basket_index` rule). Selftest gained the mirror case (the
stock lacks a day on which the basket moved 10%: basket and stock spans must
match) and a quorum acceptance/rejection pair.

**What this one adds:** it was caught by a cross-check, not a symptom. The new
report printed each peer's own move next to the "sector" figure, and the two
could not both be true. The defence is to show a derived number beside the raw
numbers it was derived from. Then a gap that is invisible inside one figure
becomes a contradiction between two.

### 14. A blank cell that stopped being a column — the CIL e-auction premium

*Found 2026-10-23, reading an uncommitted staging row.*

`parse_cil_swma` took every NUMBER on the "% increase over Notified Price" line
and chose its branch by the count: nine values meant eight subsidiaries plus the
CIL total, and eight meant the Feb-26 first-format filing with no total column,
volume-weighted. In the Sep-26 filing NEC held no auction and printed `--`. The
number regex skipped it, so eight subsidiaries plus the total read as eight
values and took the first-format branch. **The CIL TOTAL was weighted in as if
it were a subsidiary**: premium **89.9%** against the filing's **94%**, and
allocation **187.28 LT**, exactly twice the true **93.64**. Realisation went into
`prices` at **3,065 Rs/t** against **3,131**.

It reached no stored score, by luck: Coal India's economics was already
withheld because the Aug and Sep offtake filings extract too badly to parse, so
`coalindia_offtake_ttm_mt` went stale. Had the offtake feed been healthy, the
premium would have scored from 2026-10-02.

**Fix:** a `--` is a cell, and it counts as a column. The total column must
cross-foot to the sum of the subsidiaries within 0.1 LT or the filing is
refused, and an eight-column line whose last value IS the sum of the other
seven is refused too, because that is this bug's shape. Selftest covers the
Sep-26 line verbatim, a fully populated line, a total that does not cross-foot,
the hidden-total shape, and the first format.

**What this one adds:** the tell was a quantity that had no reason to be
there. Nothing reads `alloc_lt`, and it was exactly 2x the month's real figure.
A field kept only for provenance is still a check, provided someone sets it
beside the source.

## What actually caught them

Worth being honest, because it is thin and it is not the code:

- **1** — real data arriving
- **2, 3** — a human thinking "that magnitude is not plausible"
- **4** — the source document happening to contain an independent calculation
- **5** — a second data source disagreeing
- **6** — re-reading the source to check the quote contained the figure
- **7** — cross-checking a derived figure against a cited one, in a sector where the sell-side happens to quote the same quantity
- **9** — a NEW consumer of old data, forced to define what counts as a session
- **10** — probing a feed's shape BEFORE loading it, and dividing rows by years

**Nothing in the test suite caught any of them.** Six of nine were caught by
somebody eyeballing a number and finding it implausible, or re-reading a source.
That is not a process. Entries 9 and 10 are the first two that came out of
writing new code over old data and probing a feed before trusting it — the only
routes here that can be planned for rather than hoped for.

**Entry 10 is the cleanest statement of the whole class.** The numbers were
right. Every close was a real close that really printed. Only the PERIOD was
wrong, and no validator anywhere in this repo inspects a period — the identity
guard checks the instrument, the stale guard checks the last date, the frozen
guard checks distinctness. All three pass on 370 monthly bars presented as
daily. It pairs with the `range=5d`/`chartPreviousClose` note that has been at
the top of `yahoo_prices.py` since it was written: **the same file had already
been bitten once by a period it did not verify, and the lesson had not been
generalised from the close to the bar.**

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
7. **A row's existence is not evidence that the event happened.** A feed will
   answer for a day the market never traded, and the answer is flat and
   plausible. Check the tape's own marks — range and volume — not a calendar.
   From #9.
8. **Verify the PERIOD, not just the value.** A parameter that names a
   granularity is a request; the response says what you actually got, and the
   two differ silently. Divide rows by years before trusting a series. From
   #10.

## Before shipping any new arithmetic

- [ ] does a wrong lookup here return a plausible number instead of raising?
- [ ] is there a magnitude I already know, to check against?
- [ ] does the source document state its own answer, so I can reproduce it?
- [ ] is "no data" distinguishable from "zero" in the output?
- [ ] if this divisor were wrong by 2×, would anything complain?
- [ ] if a row here were **absent**, would that be distinguishable from a fetch
      that failed? (a holiday and a dropped name look identical from inside)
- [ ] does the response say what PERIOD it is, and did I check it against what
      I asked for?
- [ ] for every stored fact: is the number **in** the quote, in the source's words?
- [ ] **can this statistic move at all, and what exactly would it take?** A
      median over a panel needs >half of it moving together; if that never
      happens the indicator is dead on arrival and reads as calm. From #11.
- [ ] if I am averaging RATIOS or percentage changes, is the mean biased by
      their asymmetry? A round trip that returns to the same price must
      average to zero — check that it does. From #11.

The last one is the whole file. In #3 and #4 the answer was no.
