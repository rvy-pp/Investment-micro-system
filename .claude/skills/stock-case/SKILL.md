---
name: stock-case
description: The method every per-stock case agent follows — the Book tab's Analysis checklist (priced in, quarter build, later quarters vs street, price/bear/confidence, re-rating) and the juice (expected return) for ONE stock, kept in a case file. Use when running or acting as a <token>-agent case agent, when asked for a stock's case, juice, expected return or EPS build, or to set up a new stock's agent ("make an agent for Infosys").
---

# Stock case — one stock, its checklist, its juice

PM, 2026-10-11: *"agents per company that track and work on that specific
analysis only ... build in everything from the analysis tab requirements, work
on making cases and measuring expected returns (juice)."* Each dictated leg can
have an agent, `.claude/agents/<token>-agent.md` (the IMS ticker token,
lowercased — `tcs-agent`, `info-agent`); this file is the method they
all share. TCS was the first.

## The method — plain investing logic, the PM's (2026-10-08)

- **Today's price = forward P/E x consensus EPS.** The brief prints both years.
- **Target = today's P/E x the PM's EPS**, same year, same EPS definition,
  one-offs stripped. **Juice = PM EPS / consensus EPS − 1.** The multiple stays
  where it is unless an observable anchor is NAMED: the stock's own history
  with a reason the gap closes, a named peer's P/E, or — PM, 2026-10-20 (TCS)
  — the **median of the houses' implied target multiples** (each TP ÷ today's
  consensus EPS for the headline year; the stated multiples sit on mixed
  bases, so they are recorded, not used). Name every TP and its date in
  `anchor_why`, say the CMP the TPs were struck at, re-strike when a house
  moves, and apply the same rule to the pair partner. Never an invented
  multiple. A scenario with `anchor_pe` must carry `anchor_why`; the brief
  refuses it otherwise.
- **EPS is built quarter by quarter**: actuals for reported quarters; a view of
  the visible quarter; later quarters anchored to guidance, seasonality, known
  events and trend. Check the sum against consensus — the brief prints what
  consensus leaves per remaining quarter and the step-up that implies.
- **Re-rating = a relative-P/E shift with a nameable reason that survives the
  next print.** The brief splits the last 90 days exactly into EPS and P/E.
- **Rejected, do not reintroduce:** backtests of the method, revision betas,
  timing haircuts, statistical scaling, consensus- or momentum-derived edges.
  *"why hide behind statistics when we are talking about investing logic."*

## The five checks, and what "done" means for each

| step | done when |
|---|---|
| `priced` | automatic for IT/EMS (daily consensus); otherwise a log entry with price, consensus EPS, P/E, relative P/E — each sourced |
| `qbuild` | every reported quarter of the year has a sourced actual in `build.json` and the visible quarter has a driver-based view |
| `h2` | later quarters are built and compared with what consensus implies for them, with the reason for any gap |
| `price` | bear and bull juice on the headline year (the sector band around the base — see below) are logged as `juice_low/high` with a horizon. **`juice_pct` (the lean) is added only when the PM picks it** — then status `done` |
| `rerate` | the 90-day split is read and called: earnings-led, multiple-led with a nameable reason, or noise |

## Every run

1. Read your case file `data/analysis/cases/<entity>/case.md` — it is your
   memory. If it does not exist: `python packages/book/case_brief.py --scaffold <entity>`.
2. `python packages/book/case_brief.py <entity>` — the facts. If it prints
   `BUILD REFUSED`, fix build.json first.
3. Find what is new since the last run: a print, a preview the PM handed
   over, a concall note in the vault (`AI Insights/<TICKER>_Concall/`), a
   consensus revision, a price move. The brief's revisions and log are the
   quick check. **Do not sweep Outlook unless the PM asks** — the PM supplies
   broker documents. The morning-brief files under `data/morning/` are already
   on disk and may be read.
4. Update `build.json` (below). Actuals need a source; forecast quarters
   should carry drivers so the argument is visible. Re-run the brief.
5. Log what moved: `python packages/book/analysis.py --add '<json>'` with
   `"via": "agent:<token>-agent"`. One entry per step that changed, the note
   saying in words what the numbers are. For the price step,
   `python packages/book/case_brief.py <entity> --price-entry` prints the
   payload (range only, no lean) — pass it to `--add`.
6. Rewrite `case.md` in continuous prose (the PM's standing preference — no
   one-sentence-per-line). Append one line to its Run log.
7. If anything was logged, the vault copy owes an export:
   `python packages/web/export_static.py` (~90s). Say if you skipped it.
8. Report to the PM: what changed, base/bear/bull juice, the questions that
   stand between the range and a lean. Short.

## build.json

```json
{
  "entity": "tcs",
  "eps_basis": "consolidated, attributable to shareholders, ex-exceptional",
  "shares_cr": 361.81,
  "headline_fy": "FY28",
  "horizon": "to Q3FY27 results (mid-Jan 2027)",
  "confidence": null,
  "quarters": {
    "Q2FY27": {"kind": "actual", "eps": 38.37, "source": "..."},
    "Q3FY27": {"kind": "draft", "revenue_cr": 74200, "ebit_margin_pct": 23.9,
               "pat_to_ebit": 0.79, "why": "..."}
  },
  "scenarios": {
    "base": {"why": "...", "growth_pct": {"FY28": 5.0}}
  },
  "conflicts": [{"what": "...", "values": [...], "used": "...", "why": "..."}]
}
```

## Bear and bull are GENERATED, never written — the case band

PM, 2026-10-18, on seeing hand-drawn cases: *"how do we have random % numbers
assigned to the cases?"* and *"I will have nearly 100 companies at maturity, I
simply can not create cases for each company."* Then: *"Go with (a): fixed
sector band, ±50bps margin and ±0.5pp growth for IT, carried convention."*

- **Only the base is written.** In it, the PM's input per quarter is two
  numbers, growth and margin (or "street"). The agent does the legwork
  (guidance, same-quarter precedents, deals, peer prints) and PROPOSES them as a
  `draft` row, one line of anchors each. The PM accepts or edits it, and the row
  becomes `pm`.
- **Bear and bull are the base moved by the sector band** in
  `specs/book.yaml case_bands` (IT: ±50bps margin, ±0.5pp QoQ growth on EVERY
  forecast quarter, so the revenue level compounds). Later years are CARRIED:
  FY28 scales by the forecast quarters' EPS ratio, because after a print the
  street re-bases on the new margin level. The brief **refuses** a hand-drawn
  bear/bull in a sector that has a band.
- So the range means *how wrong can the two inputs plausibly be*, not a story.
  Do not write bear/bull narratives. Do not pick FY28 growth rates per
  scenario: that was where most of the old range came from, unanchored.
- A sector with no band yet keeps hand-drawn bear/bull until the PM sets one.
  Setting a band is the PM's call, made once per sector, never per name.
- Rule (b), each company's own last three same-quarter prints, was tried on
  TCS and NOT chosen. BSNL and a wage hike contaminate its history, so using
  it honestly needs per-name adjustments, and those are judgment again.

- `kind`: `actual` (reported — needs `source`), `pm` (the PM's stated view),
  `draft` (the agent's proposal, not yet the PM's), `street` (no view — no
  eps, no drivers). **Never relabel a draft as `pm` without the PM saying so.**
- **A view only on the visible quarter; everything after it is `street`**
  (PM, 2026-10-18: *"what is the point of doing Q4 with no credible
  information"*). A street quarter takes what consensus leaves per quarter,
  CARRIED by the view's gap to the street on the viewed quarters. A later year
  with no stated growth is consensus × that same carry. So the base juice is
  the PM's visible-quarter gap and nothing invented, and a view equal to the
  street gives exactly zero (tested). When the quarter prints, the next one
  becomes visible and gets the PM's two numbers.
- **`street_q` pins the street's own number for the visible quarter** (PM,
  2026-10-21). The even split ignores the street's Q3/Q4 seasonality: TechM's
  houses put Q3 at 19.18–21.13 and Q4 at 20.85–23.11, so an in-line Q3 read 6%
  short against the 20.61 average. When house models or previews give the
  quarter, set `"street_q": {"Q3FY27": {"eps": <median>, "source": "<each
  house, its number, file/sheet>"}}`. The view is then compared with the pin,
  and the unpinned quarters split what consensus leaves after it. EPS must be on
  the build's share basis, so restate any house that uses another count. A pin
  needs a source, cannot sit on a reported quarter, and cannot pin every
  unreported quarter of the year.
- A forecast row with all three drivers gets its EPS derived; an actual keeps
  its reported EPS.
- A scenario override is a number (replaces EPS) or a dict (changes drivers).
  A later year comes from `fy_eps` or `growth_pct` on the prior year.
- `confidence` is `low`, `medium` or `high` (PM, 2026-10-17 — the same three
  words a pair call's `conviction` takes; the brief refuses anything else). It
  is the PM's and is only displayed — no formula turns it into a weight.
- **Currency: always constant currency** (PM, 2026-10-17: *"we do not take in
  rupee, it's always CC, so ignore currency changes"*). Forecast revenue is
  held at the last reported quarter's average rate and `fx_basis` in
  build.json says which. Consensus EPS is in rupees, so part of any gap to it
  is the street's FX assumption, not a view — say so, never adjust for it.
- `conflicts`: when sources disagree on a number, list every value, the one
  used and why. Never pick silently (the silent-arithmetic rule).

## EV/EBITDA sectors (cement first)

PM, 2026-10-25: *"cement sector agents ... we will use EV/EBITDA instead of
P/E. Process is similar, get latest valuations, select mean, draft
earnings."* `specs/book.yaml case_valuation` names the sectors (CEMENT today);
the brief refuses a build whose `valuation` disagrees. Everything above holds
with EBITDA in place of EPS. These are the differences:

- **The line is quarterly consolidated EBITDA, Rs cr.** A forecast quarter is
  two numbers, `volume_mt` and `ebitda_per_t`, and EBITDA = volume x EBITDA/t
  / 10. Actuals carry `ebitda_cr` with a source.
- **No feed carries these names**, so build.json carries `consensus`
  ({FY: {ebitda_cr, source}}, the houses that print an absolute figure),
  `shares_cr` and `net_debt_cr` + `net_debt_source`. Re-strike them when a
  house publishes or the company reports.
- **Juice = (M x EBITDA − net debt) / today's market cap − 1**, where M is the
  anchor (or today's EV/EBITDA). Net debt stays at today's figure. An EBITDA
  gap reaches the equity levered by EV / market cap, so it is not the return.
- **"Get latest valuations, select mean":** list every post-result house TP in
  `house_tps` (house, tp, date, stated basis, source) and set the base
  `"anchor": "mean"`. The brief computes each house's IMPLIED multiple on the
  headline year, (TP x shares + net debt) / consensus EBITDA, and their mean
  (median printed beside it), and writes `anchor_why` naming every TP. Stated
  multiples sit on mixed bases and are recorded, not used. The same `anchor`
  key works for a P/E leg (implied = TP / consensus EPS).
- **Bands:** `case_bands` for an EV sector take `per_t_inr` and `growth_pp`
  (QoQ volume, compounding). Cement has none until the PM sets one, so there
  is no bear/bull and no price entry. Show a preview if asked, and never write
  a hand-drawn case.
- `street_q` pins take `ebitda_cr`. Cement's Q4 is the seasonal peak, so an
  even split of H2 understates Q4 and overstates Q3. Pin Q3 from house numbers
  whenever any exist.
- There is no automatic rerate split (no dated consensus history). Read it
  from the houses' revisions and say what is missing.

## IT deals — a list, cross-checked as the quarter nears (IT agents only)

PM, 2026-10-13: *"What major deals were active, what were scheduled ... were
they expected to be completed/added or are they delayed, have the older ones
gone or not"* — then, on seeing a revenue bridge built from assumed tenures:
*"just a list is fine, can be cross checked once quarter is near."* So the
deals are a LIST. **Do not turn deals into run-rates, a revenue bridge or
anything summed into build.json** — that version was built and removed the
same day.

`data/analysis/cases/<entity>/deals.json`, printed by
`python packages/book/it_deals.py <entity>` (and in the brief):

```json
{
  "entity": "tcs", "next_result": "2027-01-09",
  "quarters": {"Q2FY27": {"tcv_usd_m": 9600, "tcv_note": "excludes Porsche and Best Buy",
                          "revenue_usd_m": 7642, "source": "..."}},
  "deals": [
    {"id": "skf", "client": "SKF", "kind": "new", "value_reported": "$800m",
     "booked_q": "Q1FY27", "start_q": "Q2FY27", "end_q": null, "status": "ramping",
     "source": "Q1FY27 call p3",
     "history": [{"date": "2026-10-08", "status": "ramping", "source": "Q2FY27 call"}]}
  ]
}
```

- **Value as the source states it** (`value_reported`: "€1.25bn / 5y",
  "₹2,903cr incl. tax", "undisclosed"). No conversion, no straight-lining.
- **Statuses:** `announced`, `scheduled`, `pending_close` (acquisitions),
  `ramping`, `at_run_rate`, `ramping_down`, `delayed` (with
  `revised_start_q`), `ended`, `renewed`, `cancelled`. A change is a new dated,
  sourced `history` row, never an edit — the history answers "was it delayed?".
- **Reported TCV is the company's number** with its exclusions in `tcv_note`;
  never add an excluded deal back.
- **Two calendar checks** print always: OVERDUE (expected start passed, not
  started) and PAST_END (stated end passed, not ended/renewed).
- **The cross-check:** set `next_result`. From 21 days before it the list
  prints CROSS-CHECK DUE — re-confirm every unfinished deal's status against the
  latest call, filing or a note the PM supplies, add history rows, and note
  what changed in case.md. Outside that window, update the list only when news
  arrives; do not chase it.
- In case.md the **Deals** section is the list (a table is fine) plus one or
  two sentences on what is late and what has gone.

## Horizon — the trap between juice and Kelly

The brief's juice is a re-marking at today's P/E on a fiscal year's EPS. The
Kelly edge is a **13-week** expected pair spread. They are the same number only
if the market recognises the EPS gap inside 13 weeks — usually because a print
lands inside the window. State the horizon in build.json and in every price
entry; when the PM turns juice into an edge, say whether a print falls inside
the 13 weeks. Do not scale one into the other mechanically.

**Since 2026-10-22 the lean IS the Kelly input** (PM: *"Instead of my view in
the kelly, we put the agents view from here"*). The Kelly sizing sub-tab reads
each pair's edge as long lean − short lean (`kelly.agent_edges`), read-only, and
a pair gets an edge only when both legs carry a lean. So when the PM sets a lean,
say what it does to the pair's edge, and flag a lean whose print falls OUTSIDE
the 13-week window — that is the one case where the equivalence fails.

## What a case agent never does

Sets `juice_pct` (since 2026-10-22 the pair's Kelly edge is long lean − short lean, so a lean
sizes the book — it stays the PM's number), writes `book_calls`, writes `prices`,
`estimates` or a pillar table, writes another stock's log, or presents a draft
as the PM's view. The edge is the PM's expectation; the agent's job is to make
the range honest and the questions sharp.

## Making a new agent

`python packages/book/case_brief.py --scaffold <entity>` writes
`.claude/agents/<token>-agent.md` and an empty case folder (plus `deals.json`
for an IT leg) (case files are
never overwritten; `--force` regenerates only the agent file). The entity must
be one of the 32 dictated legs in `specs/book.yaml`. A new agent file is
picked up by a new Claude Code session.
