---
name: daily_review
description: Ingest the PM's daily IMS positions export (tab-separated paste), store it pair-wise with futures rollovers chained, refresh the Book tab, then run the position review — thesis intact, sizing right, add/trim/exit — recording verdicts. Use when the PM pastes/uploads an IMS snapshot, says daily review / review the book / review pairs, or asks about pair P&L since inception. Runs on demand, independent of the refresh pipeline.
---

# daily_review — the PM's book, pair-wise, then the review conversation

Two problems the company IMS cannot solve, and this skill exists for both:

1. **It lists positions singly.** The PM tags each position with a pair name
   ("IT 5") — more than two legs can share one tag (STEEL 1 runs three,
   EMS 1 four). The unit of thought is the pair; the display is ONE LINE PER
   PAIR.
2. **Futures tickers reset at every monthly roll** (`TATA=U6` → `=V6`) and
   the per-ticker YTD P&L resets with them — pair inception and
   since-inception P&L are invisible there. `packages/book/book_io.py`
   strips the contract token to a stable root and CHAINS the YTD P&L across
   rolls.

Division of labour, same as everywhere in this system: **the paste is the
input; `book_io.py` parses and computes.** Never transcribe numbers by hand
and never compute P&L from prices — the IMS's own printed figures are the
only P&L input.

```bash
cd "C:\Users\rajvaibhav.yadav\Investment-micro-system"
```

## Step 1 — save the paste, parse, load (one command)

The PM pastes the IMS export (tab-separated; the format is specified in the
vault's `Portfolio Management/IMS-Spec.md`, PM-confirmed 2026-08-09 — sector
aggregate rows, position rows, the N.A. bucket, a book-total row; the Cost
column is being dropped from the export and the parser accepts both shapes).
Save it VERBATIM — do not retype, reorder or "fix" anything:

```bash
# paste -> data/book/staging/raw_YYYY-MM-DD.tsv  (tabs intact), then:
python packages/book/book_io.py --parse "data/book/staging/raw_YYYY-MM-DD.tsv" --date YYYY-MM-DD
```

The date is the TRADING day the export describes, not the day it is pasted.
Re-load of a corrected paste for the same date: add `--replace`.

The parser refuses rather than repairs, and its **cross-foot check is the
transcription guard**: positions + N.A. must reconcile to the export's own
total row on MV%, GMV%, DTD, MTD and YTD, so a dropped or mangled line fails
loudly. NAV is derived per snapshot (`DTD PNL ÷ DTD PNL %`, median across
rows), never hardcoded. A parse refusal goes back to the paste — never edit
the loader to accept.

If a row carries a ticker not in `specs/book.yaml ticker_map`, load anyway
(mapping only affects model context) but flag it to the PM and add the
mapping once confirmed — never guess it.

## Step 2 — show the book

**The money column is YTD, not the day figure (PM, 2026-09-18: "the input I
give expires after rolling").** `book_io._ytd_leg` keeps a calendar-YTD
ledger seeded ONCE from the IMS YTD column and thereafter advanced only by
increments — by the cumulative YTD's delta normally, and by the DAY figure
across a roll, which cannot reset and cannot double count. Two consequences
for this step:

- **Never summarise the book by summing the day column.** On the live book
  that sum was +4,124.78 against a true +3,774.02 — a 350.75 hole that is
  exactly Monday 2026-09-14, an NSE session with no snapshot. The ledger
  absorbs such a session whole at the next paste; a day-sum loses it forever.
- **A partial paste is still fatal and now costs more.** The ledger's
  increments key on stored snapshots, so a missing leg reads as a close and
  its reappearance as a reopen. The full-book rule already in Step 1 is what
  protects the YTD, not just the pair inception.

Print the `--report` output inline for the PM: one line per pair — legs,
inception, days on, **P&L since inception (chained across rolls)**, the
**calendar-YTD ledger**, the IMS's own MTD figure, flags. The Book tab
(http://127.0.0.1:8770, "The Book") renders the same thing with per-leg
detail and each mapped leg's model composite. Say the flags out loud:

- `gap` — a roll happened across a snapshot gap, so the frozen figure may
  miss the dying contract's last days.
- `ytd?` — the YTD ledger hit a roll or a year seam it cannot verify. Print
  the flag text; it is deliberately not corrected.
- `UNTAGGED` — rows with no pair name; never guessed into a pair.
- N.A. YTD — the cost of carry of the operation (closed positions' realized
  P&L + currency/rollover/fixed costs). Real P&L, attributed to no pair.

**⚠ VERIFY ON THE FIRST OBSERVED ROLL** (standing until it happens): when a
leg's contract changes between snapshots, check the new ticker's YTD starts
near zero. The chain assumes the old contract's realized P&L falls into
N.A.; if the new ticker ever CARRIES it, the chain double-counts and the
split rule must change. Evidence so far (04-09-2026): KAYNE, opened
in-window, shows DTD == MTD == YTD to the last digit.

**Inception is the first STORED snapshot with the tag** — pairs running
before capture started show a too-recent inception. The PM can state the
pre-capture P&L and true start as `carry:` in `specs/book.yaml`; it displays
flagged, never mixed into the chained figure.

**Pairs are DICTATED, not derived.** `specs/book.yaml pairs` is the PM's own
pair list (06-09-2026), names verbatim (JSTL_TATA, COFORGE_PSYS…) grouped
under the PM's sector headings — the IMS pair tags ("IT 5") are coarser
clusters and stay internal. A leg may serve several pairs (TCS long backs
three shorts); the engine apportions a shared leg's DOLLARS across its pairs
by the gross of the opposite side it hedges, so book totals still sum —
price-%s are never apportioned. When a snapshot brings a position that is in
no dictated pair, the tab flags it: **ask the PM which pair it belongs to and
add it to the spec — never guess.**

## Step 2b — the index, and the ONE case where it needs you

The Book tab's **equal-weighted long and short indices** (`/api/book_index`,
`packages/book/basket_index.py`) recompute from `prices` and `book_positions`
on every request. **There is no index table and no refresh step: loading the
snapshot in Step 1 IS the update.** Membership is frozen at
`specs/book.yaml index.freeze_before` and follows the snapshots after it, so a
position that went on or came off today shows up in the index as soon as the
paste is loaded.

**The one thing that does need a command: a leg that is NEW to the book.** Its
history in `prices` only goes back as far as the daily 3mo window, so the index
would read it as having "listed" three months ago and the back-cast would show
it joining there — a leg with fifteen years of history appearing to start in
June. Backfill it once, deep:

```bash
python packages/adapters/yahoo_prices.py --load --only <entity_id> --range 2007-01-01
```

`--only` is not optional in practice: a deep range over the whole candidate
list would rewrite years of commodity closes as a side effect of wanting equity
bars. And **use an ISO date, never `--range max`** — Yahoo silently returns
monthly bars for a long history and hourly for a short one, and `fetch_bars`
now refuses anything whose `dataGranularity` is not `1d` rather than storing
month-end candles as sessions (docs/SILENT_BUGS.md entry 10).

Then read the index block's footer back to the PM if any of these appear — each
is a real exclusion, not a cosmetic note:

- **"In the book but priced by nothing"** — the leg maps to an entity with no
  symbol in `yahoo_prices.CANDIDATES`. This is the only failure the index
  cannot see by itself: a leg with no price is skipped as "not yet listed",
  which holds up no session and records no gap, so **it would sit outside the
  index in silence**. Add the symbol, backfill, tell the PM.
- **"Unmapped legs"** — the ticker is not in `ticker_map`. Same Step 1 rule:
  flag, never guess.
- **"Sessions skipped for a missing price"** — a listed leg had no close, so
  the bar was not drawn and the chain widened its interval. These are holes in
  Yahoo's data, not fetches that failed; nothing to fix, but say it.
- **Membership changes since the freeze** — the footer lists them. Confirm they
  match what the PM actually did; a change the PM does not recognise means the
  paste was partial, which Step 1's "never load a partial paste" rule exists to
  prevent.

**What NOT to do to it.** Do not move `freeze_before` forward to today's date
to "keep it current" — it is a constant on purpose, and advancing it re-writes
the whole back-cast to whatever the book looks like now, which is the
survivorship bias the freeze exists to bound. Do not add an index table. Do not
re-weight it by position size: the PM asked for **direction only**, so every
leg counts 1/n whatever it is worth.

## Step 2c — re-export the vault copy (ALWAYS, after a snapshot loads)

```bash
python packages/web/export_static.py
```

~1.5 min, foreground. Run it once Step 1 has loaded and Step 2b's backfill (if
any) is done.

**Why it is a step here and not left to tomorrow's refresh.** The offline copy
in OneDrive — `Obsidian Vault\Investment Micro-System\Investment
Micro-System.html` — is a FROZEN snapshot of every API payload, rebuilt by
`refresh.py` as its last step. But **this skill runs outside `refresh.py` on
purpose**, and it is the only thing that writes `book_*`. So a review that
loads a new paste at 16:00 changes the Book tab on the desk and leaves the
vault copy showing the PREVIOUS snapshot's positions, pairs, YTD ledger and
long/short index until the next morning's 08:00 run.

**That is the one staleness the snapshot bar cannot catch.** The bar counts
weekdays since the export, so a copy exported this morning reads "today" and
green — correctly, for everything that came out of `refresh.py`, and wrongly
for the book, which moved after it. Nothing on the page can tell the two
apart. Re-exporting is the whole fix and it costs 90 seconds.

Same reasoning applies to `book_io.add_review(...)` in Step 3 if the verdicts
are to show in the vault copy: **export after the review, not before it.** One
run at the end covers both.

Tell the PM it re-exported, with the route count and the timestamp the command
prints. If OneDrive is signed out the command fails and says so — report it and
move on, never fatal.

## Step 3 — the review (the point of all of it)

For each pair the PM wants (default: all, worst since-start % first),
assemble the evidence BEFORE opining. Reviews are keyed by the DICTATED pair
name (`book_io.add_review('JINDA_SAIL', ...)`).

- **Model**: `/api/book` carries each mapped leg's composite; pillar detail
  via `/api/overview`'s book block or `/api/tape`. IT legs have no score by
  the PM's ruling (forward P/E panel instead — the IT tab); say so rather
  than improvising one.

  **Read scores as CHANGES, never as levels (PM ruling, 2026-09-07).**
  Absolute composites carry no verdict on a position: some names score
  structurally low or high forever (SAIL's one-rebar-line beta, APL
  Apollo's converter bridge, VEDL's holdco arithmetic), and the PM may
  deliberately hold the opposite view of a level. Never present "model
  prefers the short leg" as evidence against a pair from levels alone.
  What carries information is a leg's score moving against its OWN recent
  history — pull the composite series from `/api/tape` (persisted scores,
  never recomputed) and quote the delta since the last review or over
  ~5-10 trading days, as a % change of the score where the base makes
  that meaningful. A widening or narrowing of the PAIR's score spread is
  the pair-level read; a level ranking is not.
- **Positioning**: `/api/oi` for OI percentile + buildup per leg.
- **Regime**: `/api/flows` — `w1.lead` is the rolling past-week state,
  updated every US session (the Friday weekly layer is the trend behind
  it) — can the pair express right now?
- **History**: `book_io.reviews()` — what was said last time, did it hold?
  A pair marked `thesis_intact: 0` or repeatedly `trim` is a standing
  candidate for the mistakes review, as is any closed pair with a negative
  final P&L.

Then the back-and-forth: does the thesis hold, is the sizing optimal, add or
not, exit. Confront the position with the evidence (score against the leg,
OI crowded, pair bleeding since inception) — the CALL is the PM's.

Record every verdict reached:

```bash
python -c "import sys; sys.path.insert(0,'packages/book'); import book_io; \
book_io.add_review('IT 5','hold','one-line reason as agreed with PM',thesis_intact=True)"
```

and write the fuller discussion to `data/book/reviews/YYYY-MM-DD.md` — pair,
evidence considered, decision, what would change the mind. Closed pairs with
a negative final P&L get a post-mortem line: what broke, was it visible in
the model, what to carry forward.

## What this never does

- Never writes to `prices`, `pillar_scores`, or anything a pillar reads —
  the book tables are `book_*` only.
- Never computes P&L from prices; never "corrects" an IMS figure.
- Never invents a pair tag, a ticker mapping, or a missing cell.
- Never loads a partial paste — a snapshot is the FULL book (absence from
  one is read as a position CLOSED; that is how re-entries are detected).
- Position data stays local — `data/` is gitignored; never commit or paste
  the book into anything that leaves the machine. **ONE deliberate exception,
  PM-sanctioned 2026-09-20:** the vault copy (Step 2c) bakes the Book tab into
  the HTML file in the PM's own PinPOINT OneDrive, so he can read the book off
  this machine. That is the firm's storage, not a third party, and the git
  remote is still untouched. `export_static.py --no-book` excludes it if the
  PM ever wants it out — the tab then says so rather than rendering empty.
