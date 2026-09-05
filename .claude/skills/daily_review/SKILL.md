---
name: daily_review
description: Ingest the PM's daily IMS positions snapshot (screenshot/xlsx/paste), store it pair-wise with futures rollovers chained, refresh the Book tab, then run the position review — thesis intact, sizing right, add/trim/exit — recording verdicts. Use when the PM uploads an IMS snapshot, says daily review / review the book / review pairs, or asks about pair P&L since inception. Runs on demand, independent of the refresh pipeline.
---

# daily_review — the PM's book, pair-wise, then the review conversation

Two problems the company IMS cannot solve, and this skill exists for both:

1. **It lists positions singly.** The PM tags each position with a pair name
   ("IT 5") in a description field — more than two legs can share one tag.
   The unit of thought is the pair, so everything here displays ONE LINE PER
   PAIR.
2. **Futures tickers reset at every roll** (`TATASTEEL=U6` → `=V6`) and the
   IMS P&L column resets with them — pair inception and since-inception P&L
   are invisible there. `packages/book/book_io.py` strips the contract token
   to a stable root and CHAINS P&L across rolls.

Division of labour, same as everywhere in this system: **the agent
transcribes; `book_io.py` computes.** Never do P&L arithmetic in your head —
no lot sizes or multipliers exist here, the IMS's own printed P&L figure is
the only P&L input.

```bash
cd "C:\Users\rajvaibhav.yadav\Investment-micro-system"
```

## Step 1 — parse the snapshot into staging JSON

The PM uploads the day's IMS positions view (screenshot, xlsx, csv, or a
paste). Transcribe it into `data/book/staging/YYYY-MM-DD.json`:

```json
{
  "date": "2026-09-05",
  "source_file": "what was uploaded, for the record",
  "pnl_basis": "contract_itd",
  "positions": [
    {"pair": "IT 5", "ticker": "TCS=U6 IS Equity", "qty": 100,
     "avg_price": 4100.5, "last_price": 4188.0, "mv": 418800,
     "pnl": 8750, "side": "L"}
  ]
}
```

Rules — each one exists because its violation is silent:

- **Every row, verbatim.** A snapshot is treated as the FULL book: a leg
  absent from a snapshot is read as CLOSED (that is how re-entries are
  detected), so a partially transcribed snapshot books phantom exits. If the
  upload is cropped or ambiguous, ask — do not load half a book.
- **Ticker exactly as printed**, contract code (`=U6`) included. The code
  strips it; you do not.
- **`pair` exactly as the PM wrote it** on the IMS ("IT 5", "MET 2"…). A row
  with no tag gets NO pair — the loader files it under UNTAGGED and the tab
  flags it. Never invent or infer a tag.
- **`pnl` is the IMS's own P&L figure for that row.** Never compute it from
  prices, never "correct" it.
- **`qty` signed (long +, short −) or with an explicit `side`.** Both is
  fine; sign wins.
- A cell you cannot read → omit the field (avg/last/mv/pnl may be null) and
  note it; a TICKER or QTY you cannot read → stop and ask.
- After writing the JSON, **count**: rows in file == rows in the upload, and
  spot-check 3 rows byte-for-byte (the westmetall transcription lesson —
  nothing downstream catches 3182 read as 3812).

## Step 1b — FIRST RUN ONLY: calibration (ask, do not assume)

1. **What is the P&L column?** Show the PM one position and ask whether its
   figure is (a) since the current contract was opened — resets at roll →
   `pnl_basis: contract_itd` (default), or (b) that day's P&L only →
   `"daily"`. Set it in the staging JSON and as the default in
   `specs/book.yaml`. The chain arithmetic differs; a wrong basis is a wrong
   total that looks plausible.
2. **Fill `specs/book.yaml ticker_map`** — Bloomberg root → entity_id — for
   legs covered by this system, confirming each mapping with the PM (a
   guessed map glues the wrong model context to a real position; leave
   unknowns unmapped).
3. **Pre-history**: if a pair was running before the first capture, the PM
   can state its earned P&L → `carry:` in `specs/book.yaml`. Displayed and
   flagged as carry, never mixed into the chained figure.

## Step 2 — load (deterministic)

```bash
python packages/book/book_io.py --load "data/book/staging/YYYY-MM-DD.json"
```

Re-upload of a corrected snapshot for the same date: add `--replace`.
The loader refuses rather than repairs (future date, empty book, dup rows,
qty 0). A refusal goes back to Step 1 — never edit the loader to accept.

Verify: the printed report's position count matches the upload, and every
pair tag you saw on the sheet appears.

## Step 3 — show the book

Print the pair report inline for the PM (`--report` output is the shape):
one line per pair — legs, inception, days on, **P&L since inception (chained
across rolls)**, day P&L, flags. The Book tab (http://127.0.0.1:8770, "The
Book") now renders the same thing; note `gap` flags out loud — a roll across
a snapshot gap means the frozen figure may miss the old contract's last days.

## Step 4 — the review (the point of all of it)

For each pair the PM wants to look at (default: all, worst day-P&L first),
assemble context BEFORE opining:

- **Model**: `/api/book` joins each mapped leg's composite; pillar detail via
  `/api/overview`'s book block or `/api/tape`. Unmapped legs have no model
  view — say so, don't improvise one.
- **Positioning**: `/api/oi` for the legs' OI percentile + buildup.
- **Regime**: `/api/flows` weekly state — can the pair express right now?
- **History**: `book_io.reviews()` — what was said last time, and did it
  hold? A pair previously marked `thesis_intact: 0` or repeatedly `trim` is
  a standing candidate for the mistakes review.

Then the back-and-forth with the PM: does the thesis hold, is the sizing
optimal, add or not, exit. Your role is to confront the position with the
evidence (score moved against the leg, OI says crowded, pair bleeding since
inception despite thesis) — the CALL is the PM's.

Record every verdict reached:

```bash
python -c "import sys; sys.path.insert(0,'packages/book'); import book_io; \
book_io.add_review('IT 5','hold','one-line reason as agreed with PM',thesis_intact=True)"
```

and write the fuller discussion to `data/book/reviews/YYYY-MM-DD.md` (create
the folder if absent) — pair, evidence considered, decision, what would
change the mind. Closed pairs with a negative final P&L get a post-mortem
line there too: what broke, was it visible in the model, what to carry
forward.

## What this never does

- Never writes to `prices`, `pillar_scores`, or anything a pillar reads —
  the book tables are `book_*` only.
- Never computes P&L from prices; the IMS figure is the only P&L input.
- Never invents a pair tag, a ticker mapping, or a missing cell.
- Never loads a partial transcription of a snapshot.
- Position data stays local — `data/` is gitignored; never commit or paste
  the book into anything that leaves the machine.
