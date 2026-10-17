---
name: results-ingest
description: Stage and load sell-side result estimates, reported prints and result dates for the Results tab — from a broker preview/result note, a digest line, or a morning-brief bullet. Writes a cited JSON under data/results/staging/, loads it with packages/results/results_io.py, re-exports the vault copy. Use when asked to add/store an estimate or a result, "results preview", "what did X print vs consensus", or to log a result date.
---

# results-ingest — a broker number into the Results tab, with its citation

The Results tab (added 2026-09-22) is sector → company → quarter: every
house's estimate for a metric, the median consensus, the print, the
surprise. It stores NOTHING it cannot cite. This skill is the only way
numbers get in; it is deliberately agent-driven, like every extraction here.

Store: estimates → `estimates` (house broker rows); prints → `observations`
factor='actual'; dates → `results_calendar`. `packages/results/results_io.py`
validates and loads; `--selftest` covers what it refuses and why.

## Step 1 — find the numbers

Sources, in order of quality:

1. The broker PDF/mail itself (M365 MCP: `outlook_email_search` by broker +
   company, then read the body). A preview table gives one row per metric.
2. A digest line under `Broker Mails/DD-MM-YYYY.md` (corpus through 18-08-2026).
3. A morning-brief bullet (`data/morning/brief_YYYY-MM-DD.json`) — its
   `detail` block often carries the numbers; the collapsed bullet rarely does.

**Only numbers the source STATES.** "PAT +6% vs est" gives you the actual,
not the estimate — 349/1.06 is arithmetic the broker did not write down, so
it is not loaded. "13% ahead" likewise. If the estimate matters, find the
preview that stated it.

## Step 2 — write the staging file

`data/results/staging/YYYY-MM-DD_<slug>.json` (tracked in git — it is the
dated record). Shape, one file per note or per day:

```json
{
  "sources": [{"id": "kotak-shree-2026-08-02", "kind": "broker_note", "origin": "Kotak",
               "title": "Shree Cement 1QFY27 result", "source_date": "2026-08-02",
               "raw_path": "Broker Mails/02-08-2026.md"}],
  "estimates": [{"entity_id": "shree", "broker": "Emkay", "period": "Q1FY27",
                 "metric": "ebitda_per_t", "value_num": 1200, "unit": "INR/t",
                 "as_of": "2026-08-01", "source_id": "...", "quote": "verbatim sentence"}],
  "actuals":   [{"entity_id": "shree", "period": "Q1FY27", "metric": "ebitda_per_t",
                 "value_num": 1024, "unit": "INR/t", "as_of": "2026-08-01",
                 "source_id": "...", "quote": "verbatim sentence"}],
  "calendar":  [{"entity_id": "shree", "period": "Q2FY27", "event_date": "2026-10-20",
                 "status": "confirmed", "source_id": "...", "quote": "exchange notice text"}]
}
```

Rules the loader enforces (it refuses the WHOLE file on any one of them —
read the list it prints, fix, re-run):

- `entity_id` must exist in `entities`. Ids are the spec slugs (`tata_steel`,
  `hindustan_zinc`, `pg_electroplast`, `novelis`); `--report <id>` or the
  tab's picker shows them. Never invent one.
- `period`: `Q1FY27`/`FY27`. `1QFY27E`, `2QFY27F`, `FY27E` are accepted and
  normalised. Half-years are not a period here.
- `metric` from the vocabulary in `results_io.METRICS`: revenue, ebitda,
  ebit, pat, eps, ebitda_margin_pct, ebit_margin_pct, gross_margin_pct,
  volume, realisation, ebitda_per_t, cost_per_t, revenue_growth_cc_pct,
  order_inflow, order_book, capex, net_debt. A new metric is an edit to
  METRICS with a `better` direction, not a new string.
- **Units are canonical and must agree with the store** for that
  (entity, period, metric): INR cr for money lines (Rs41.5bn → 4150; Rs349mn
  → 34.9), INR/sh for EPS, INR/t per tonne, `t` for volumes (10.5mt →
  10,500,000), `%` for margins, USD mn / USD/t for Novelis. If the store
  holds "INR/t India" (Tata Steel standalone) a bare "INR/t" is refused —
  that is a real basis difference, state the same basis or do not load.
  The quote keeps the source's own figure, so the conversion is auditable.
- `broker` is the house ("Kotak", "Nomura", "Morgan Stanley") — never
  `consensus_yahoo`/`bloomberg`, which are adapter feeds.
- `as_of` is the note's date, never in the future. An estimate dated AFTER
  the print is kept and shown as post-print but excluded from the consensus
  the print is graded against — so date the row by when the house said it.
- `quote` verbatim, non-empty, on every row (invariant 1).

## Step 3 — load, check, export

```bash
python packages/results/results_io.py --load data/results/staging/<file>.json
python packages/results/results_io.py --report <entity_id>
python packages/web/export_static.py --quiet
```

The export is not optional: this skill writes tables the page reads outside
`refresh.py`, so it owes the vault copy a re-export (the daily_review rule).
`refresh.py` also runs `--load-all` each morning, so a staged file left
unloaded is picked up at 08:00 — but the export from that run is what makes
it visible on the phone.

## What not to do

- Do not load a sector-level number ("2QFY27F cement spread ~INR2,464/t")
  against a company. The tab is per entity.
- Do not average a broker's range into a point without saying so in the
  quote — better to load the midpoint with the range in the quote.
- Do not "fix" a unit conflict by editing the stored row; state the new one
  in the stored unit, or record why the basis genuinely differs.
