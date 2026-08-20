---
name: concall-ingest
description: Turn a company's earnings call into P4 guidance rows and cited actuals. Reuses the vault's existing concall note if one exists for the quarter; otherwise runs the earnings-transcript analysis on the PDF first, writes the note, then extracts. Use when asked to score guidance for a name, add commitments, or ingest a concall.
---

# concall-ingest — earnings call → P4 guidance and actuals

P4 is the only pillar whose input is not a price feed. It needs two things per
company per quarter, both cited:

- **commitments** — what management promised, quantified (`guidance` rows)
- **actuals** — what they delivered against those promises (`observations`,
  `factor='actual'`)

Both live in the concall. This skill gets them into the store.

## Step 1 — is there already a note?

```bash
VAULT="C:\Users\rajvaibhav.yadav\OneDrive - PinPOINT\Obsidian Vault"
ls "$VAULT\AI Insights\<TICKER>_Concall\"
```

Tickers for the covered names: `NATIONALUM`, `HINDALCO`, `VEDANTALUM` (= VAML),
`HINDZINC`, `VEDL`.

**If `Concall - <TICKER> - <Quarter> <FY>.md` exists, use it. Go to step 3.**
It is already an analyst note and re-deriving one from the transcript wastes
tokens and invites a second, differing reading of the same call.

## Step 2 — no note: analyse the PDF first

Follow the vault's own instructions rather than improvising a format — next
quarter's note grades against this one, so the structure has to match:

```
$VAULT\SKILLS\EARNINGS CALL TRANSCRIPT ANALYST.md
```

In short: extract with `pdftotext -layout` (fall back to `pdfplumber` if
garbled), auto-load the prior quarter's note to grade against, write the note in
the fixed structure, and save it to
`AI Insights\<TICKER>_Concall\Concall - <TICKER> - <Quarter> <FY>.md`.

The transcript may already be extracted — several folders hold a `.txt` beside
the `.pdf`. Check before re-extracting.

Known gap at the time of writing: `NATIONALUM` has a Q1 FY27 **transcript** and
no Q1 FY27 note. That is a step-2 case.

## Step 3 — pull the two sections that matter

**`## 🎯 New commitments → next quarter's scorecard`** → `guidance` rows.
Take **Hard** commitments only. "Soft" items ("stay net-cash", "no hedging
currently") are intentions, not targets, and a `guidance` row needs a stated
number the CHECK constraint will enforce.

| field | from |
|---|---|
| `metric` | `volume` · `silver_volume` · `cost_per_t` · `capex` · `ebitda_per_t` · `margin` |
| `target_type` | `point` / `range` / `direction` |
| `target_value` or `target_low`+`target_high` or `target_dir` | the number as stated |
| `period` | `FY27`, `Q2FY27` — **Indian FY, so FY27 is Apr 2026–Mar 2027** |
| `issued_date` | **the call date**, from the note header (`**Call:** 24 Jul 2026`) |
| `quote` | verbatim. NOT NULL with a non-empty CHECK |

**`## 📊 Did they deliver?`** → `observations` with `factor='actual'`.
Its rows are prior-guidance-vs-actual, which is exactly a scoreable actual. Set
`metric` to **match the guidance metric it answers**, or `guidance_runrate.py`
will not find it and will withhold.

`as_of` is **the call date, never the quarter end.** Q1 covers Apr–Jun and is
knowable in late July. Dating it to 30 June asserts you knew three weeks early
and quietly corrupts every backtest that uses it.

## Step 4 — check the polarity exists

`guidance_runrate.POLARITY` maps each metric to +1 (more is better) or −1 (less
is better). **A metric absent from the map is refused, not guessed** — guessing
the sign of a cost target scores a beat as a miss. If the call introduces a new
metric, add it to the map with the sign stated, in the same change.

## Step 5 — score, and reproduce the note's own arithmetic

```bash
python packages/score/guidance_runrate.py --entity <entity_id>
```

**Compare the output against what the note says**, line by line. The notes state
their own conclusions — *"Q1 260 KT annualises ~1.04 Mt"*, *"149 t vs ~175 t/qtr
needed"*, verdicts of Delivered / On track / Partial. If the computed gap
disagrees with the note's read, **one of them is wrong and it is usually the
code**: this is precisely how the annualisation bug in `docs/SILENT_BUGS.md` #4
was caught, and nothing else would have caught it.

A source that shows its working is a free test. Use it every time.

## Step 6 — resolve what has closed

If the note grades a **prior** commitment as delivered or missed, close that
`guidance` row: set `status` to `met`/`missed`, plus `resolved_date` and
`actual_value`. `v_guidance_track_record` computes each management's hit rate
from resolved rows and is currently empty because nothing has ever been closed —
that view is the intended prior for future confidence.

## What not to do

- **Do not create `guidance_evidence` rows for reiterations.** "Guidance
  maintained at 1.1mt" is management repeating itself, not information. Three of
  HZL's five original evidence rows were reiterations and they dragged confidence
  upward for no reason.
- **Do not hand-weight what arithmetic can reach.** A cost actual against a cost
  target is a computation. `guidance_evidence` is for facts the run-rate cannot
  see — a regulatory clearance, a commissioning slip with a stated reason.
- **Do not put sentiment here.** "Management sounded confident" is P3 mood, or
  nothing. The note's own `**Sentiment:**` header line is not a P4 input.
