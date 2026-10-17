---
name: company-analysis
description: Build a full backward-looking coverage dossier for one company from its earnings-call transcripts — fetch every call off screener.in (or take PDFs the PM supplies), grade management's guidance against what was delivered, test whether raw materials explain anything, and write the chapter-by-chapter narrative that explains the share price. Renders as the "Company historical analysis" sub-tab of the company's sector. Use when asked to cover / analyse / read up on a company, to explain what moved a stock, or to add a name to a sector's company analysis.
---

# company-analysis — the whole history of one name, and what moved the price

**The output is a READ, not a score.** Nothing this skill produces reaches
`pillar_scores`, and nothing it writes goes into `prices`. A transcript-derived
revenue number sitting where a bridge could shock it is the `estimates`-vs-
`prices` mistake again. The dossier is `data/companies/<slug>/dossier.json`,
served by `engine.company_view()` at `/api/company?sector=<id>`, rendered by
`companyNarrative()` + the module sections on the **Company historical
analysis** sub-tab of the company's SECTOR. There is no top-level Company tab
(PM 2026-09-16: it "sits out like a sore spot. Keep it within the sectors
only").

**The universe is the folder list.** Adding a company is a directory under
`data/companies/`, never a code change. Do not add a name anywhere else.

Worked reference: **avalon-technologies**, 14 calls, built 2026-09-14. Read its
dossier before your first run — every rule below came out of it.

---

## Step 0 — resolve the company, and write the disqualifier first

Get the NSE symbol as **screener** knows it. Verify by name, never by guess —
`yahoo_search.py` exists because VAML.NS cost three wrong guesses.

Then write the `aliases` list, and **write the disqualifier in the same
breath**. Ambiguous short names are the rule, not the exception:

- bare `Avalon` in an aviation context is the AIRCRAFT LESSOR
- bare `HCL` is HCL Technologies all 11 times in the corpus, never Hindustan Copper
- `NMDC` matches inside "NMDC Steel", a different listed company

An over-broad alias mis-files another company's news onto this page, and it is
silent. State the exclusion in `_aliases` so the next reader sees why.

## Step 1 — fetch the calls

```bash
python packages/company/fetch_transcripts.py <SYMBOL> --list            # look first
python packages/company/fetch_transcripts.py <SYMBOL> --slug <slug>     # then pull
```

Screener's Concalls block links the company's own IR PDFs; plain urllib with a
browser UA gets HTTP 200, no login. The script dedupes, derives the quarter
from the CALL MONTH (Aug→Q1, Nov→Q2, Feb→Q3, May/Jun→Q4), extracts page-marked
text to `sources/_text/` and a boilerplate-stripped copy to `sources/_clean/`,
and prints per call: pages, words, provenance, and two flags.

**Read the tail of its output before going on.** It tells you whether the
quarter sequence is CONTIGUOUS or has gaps, and whether the oldest call is the
company's first as a listed entity. *"All 14 calls that exist"* and *"a sample
of 14"* are different claims and the dossier must make the right one.

Act on the flags:
- `cover_letter` — page 1 is the filing letter to BSE/NSE. Skip it; it is not
  the call.
- `scanned` — the PDF is an image. **Refuse it.** Do not OCR and do not guess.
  Record it as unavailable and tell the PM which quarter is missing.

If the PM supplies PDFs instead, drop them in `sources/` with the same
`transcript_<QxFYxx>_<Mon><Year>.pdf` naming and run the extraction directly.

## Step 2 — read them, oldest first

**Chronological, always.** Guidance only resolves forward: a commitment made on
call N is graded by call N+1. Reading newest-first files every commitment as
`pending` and resolves none.

Read the full text, not the summary. Per call, capture:

- **headline numbers** exactly as stated, with the basis (consolidated /
  standalone) as stated
- **every forward commitment**, verbatim, filed into the LATER period it refers
  to as `pending`
- **what management would not answer** — the hedges and the declines are a
  section of their own and they are often the most informative part
- **what the analysts kept asking** — repetition across calls is the trend
  detector
- **events**: things the company chose (strategy) vs things that happened to it
  (industry). The test is *could management have decided otherwise?*

### Flag conflicts, never resolve them

Two calls will state the same figure differently. **Carry both and print the
conflict.** All three of Avalon's were real:

- FY23 revenue printed in the transcript as "INR44.7 crores" against a Q4 of
  272 — a transcription error. Reconstructed, flagged, and the dossier says to
  take it from a filed statement.
- Q1FY24 gross profit 77cr on its own call, 80.0cr when the next year restated it.
- June-24 working capital 156 days on one call, 163 on another.

None changed a view. A quietly-picked number is the silent-arithmetic shape;
the rule is in `docs/SILENT_BUGS.md` and it applies here unchanged.

## Step 3 — grade the guidance ledger

This is the product. It is the one thing a transcript archive can say that a
price series cannot.

**Grade against the ORIGINAL commitment, never against the reset.** Avalon
guided FY24 at +25-30%, cut it to 15-25%, then to "lower end with a negative
bias", then replaced it with a degrowth forecast of 8-10%, and landed at
-8.2%. That is a **miss**, and the note says so explicitly: *miss against the
original commitment; met against the guidance as reset.* A company that resets
guidance does not get to launder the first promise.

Verdicts are `beat | met | miss | pending` and nothing else — the renderer
styles on them. Nuance goes in `note`, not in the verdict. Anything partial is
a miss.

**Sort misses first.** A scoreboard that opens on the wins is a brochure.

Watch for the commitment that quietly stops being mentioned. Avalon's aerospace
wiper-blade programme was guided to volume production and then disappeared from
the script after FY25. **Absence is not disclosure** — file it as a miss with a
note, and put it on the list to ask.

## Step 4 — the price spine

```bash
python packages/company/price_story.py <entity_id> --calls <calls.json> --out <story.json>
```

If the name has no rows in `prices`, load closes first via
`packages/adapters/yahoo_prices.py` — do not fetch into a local file and
compute off that, or the story and every other surface will disagree.

It returns earnings reactions (measured **from the close before the call**,
because an after-hours result belongs to the next session), drawdown episodes,
every session that moved 8%+, candidate chapter boundaries drawn on the price,
and the commodity test.

### Two outputs you must render rather than swallow

**`unattributed`** — big sessions with no call within two days. Calls happen
four times a year; news flow, initiations and blocks do not. **List them as
unexplained.** Ten of Avalon's eighteen came back unattributed. Inventing a
reason for each would make the eight real attributions worthless.

**`market_factor: true` on a drawdown** — no call inside the fall, so nothing
the company said caused it. Avalon's DEEPEST drawdown (-39.7%, Dec-24 to
Jan-25) was the Indian small-cap correction; there was no call, no filing, no
guidance change, and the print six days after the trough was excellent. A
dossier reading only company documents would have read that trough as a signal.
**Check the market before you look inside the business.**

## Step 5 — test the raw materials. Do not assume them.

The script prints a verdict. Avalon's was **no usable linkage** — copper
+0.000, HRC -0.060, aluminium -0.113, brent -0.160, USDINR -0.261, coke -0.264
on 21-day returns.

Then run the regime check by hand, because correlation alone is not the
argument: Avalon's inputs were flat-to-down in FY24 while revenue fell 8.2%,
and up 27-64% in FY26 while revenue rose 46%, with gross margin moving 36.3% →
34.3% across the whole span. **The signs are wrong for a cost story.**

The mechanism, when you find it, is usually pass-through. Avalon proved it
under the hardest available test: through the tariff period it paid over 50
distinct tariff rates and recovered more than **99%** from customers.

**A weak negative on USDINR or an energy series is a risk-off proxy for Indian
small caps, not a cost channel.** Do not report it as one.

Saying "no linkage" out loud is a result worth as much as finding one — it is
exactly why the EMS spec withholds economics for these names rather than
carrying a 0.50 composite weight on a measured zero.

## Step 6 — write the narrative

Cut the history into chapters **bounded by turning points in the price, never
by the calendar**. A chapter starting on 1 April describes the fiscal year, not
the stock. The script proposes candidates; you name them.

Each chapter carries: what happened, what the market did, an **attribution
grade** (`high` / `partial` / `none — market factor`), and the lesson.

Lead the dossier with the one finding that organises everything. For Avalon it
was that the stock tracks **the change in expectation, not the level of
delivery** — the business shrank 8.2% in FY24 and the stock rose 24.1%; the
best operating year in its history (+46%) produced the worst annual return of
the three (+20.5%).

Then write `standing_lessons` — what this name teaches that transfers to the
next one. That is the part that compounds across companies.

## Step 7 — name the holes, then render

Five modules cannot come from a transcript and must be listed with the document
that closes each: **shareholding** (pattern filing), **market share** (peer
revenues per period), **ratios** and **valuation** (screener export),
**stock/abnormality** (prices). Render them as named holes. An absent module
reading as "nothing happened" is the one thing it must not do.

Write `data/companies/<slug>/dossier.json`. **It MUST carry a top-level
`sector` key** whose value is an id from `engine.SECTORS` (`non_ferrous`,
`steel`, `cement`, `mining`, `ems`, `it`) — that key decides which sector tab
the dossier renders under. Without it the engine falls back to the entity spec
whose `nse_symbol` matches `tickers.nse`, then to the `entities` table; an
unresolved dossier renders under NO tab and is only reported as `unplaced` on
the nav and on every Company sub-tab. Coforge needed the explicit key (IT has
no specs and the entities table carries no symbols). Then:

```bash
curl -s "http://127.0.0.1:8770/api/company?sector=<sector>&slug=<slug>" | head -c 300
curl -s "http://127.0.0.1:8770/api/nav" | grep -o '"company_unplaced"' || echo placed
```

**Restart the server if you changed `engine.py` or `serve.py`** — it imports
once. No restart is needed for a new dossier; the folder is read per request.

Check the tab renders and the console is clean.

## Step 8 — the long-form notes (optional, and say if you skipped it)

Every transcript should get a restructured note under `sources/summaries/` —
1,800-3,500 words, reorganised by theme, every number page-cited, Q&A grouped
by topic, plus a section on what management would not answer and an exhaustive
numbers index. The Document Archive shows `no note yet` against any that lack
one, which is the honest state rather than a hidden gap.

The dossier does not depend on them. **Report how many you wrote.**

---

## What this skill will not do

- **Score anything.** If the PM wants the name scored it needs a spec, a peer
  group and F&O membership per invariant 7 — a different job.
- **Write a number with no source.** Missing is `"status": "unavailable"` with
  a reason.
- **Explain a move it cannot explain.** The unattributed list is a feature.
- **Read the old vault for methodology.** Standing ruling, unchanged.

## Cost

One call is a full read plus a note. Avalon's 14 ran ~122,000 words. Budget the
reading, not the writing — the structured dossier is cheap once the calls are
read, and the notes are the expensive half.
