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

Print the `--report` output inline for the PM: one line per pair — legs,
inception, days on, **P&L since inception (chained across rolls)**, the
IMS's own day and MTD figures, flags. The Book tab (http://127.0.0.1:8770,
"The Book") renders the same thing with per-leg detail and each mapped leg's
model composite. Say the flags out loud:

- `gap` — a roll happened across a snapshot gap, so the frozen figure may
  miss the dying contract's last days.
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
- **Regime**: `/api/flows` weekly state — can the pair express right now?
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
  the book into anything that leaves the machine.
