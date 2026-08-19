# What has to be watched, every day

Written 2026-08-18 while the system covers five aluminium/zinc names. It is
written for the platform it becomes, not the one it is: every check below is
sector-agnostic, and adding steel or cement should add rows to the tables, not
sections to this document.

**The organising rule: a check that can fail silently is worse than no check.**
Most of what follows exists because something already failed quietly here — an
unregistered unit that dropped an FX leg, a date guard that rejected every valid
date, a spec field nothing read. Each of those looked healthy from the outside.
So every item states what it does **when it fails**, and the answer is never
"skip and continue".

---

## Tier 0 — integrity preflight

Cheap, deterministic, runs before anything else. These catch the class of bug
that produces a plausible number rather than an error.

| Check | Why it exists | On failure |
|---|---|---|
| every `price_link` in the specs has a `units` entry | `lme_zinc` had none, so `to_inr` skipped the USD→INR leg and understated zinc **95×**, with `coverage_ok` still true | **HALT.** Nothing downstream is trustworthy |
| every scoreable entity has a `base_ebitda` | it is the denominator of every score; a missing one silently rescales a name | HALT for that entity |
| every entity with a `peer_group` has a price series | a name modelled but unpriced scores nothing and reports nothing | warn, name the entity |
| schema guards pass (`init_db.py`) | 7 rejection + 2 acceptance tests. Rejection alone is not enough — `GLOB '____-__-__'` rejected every valid date and passed every test | HALT |
| specs parse and `effective_from` ≤ today | a future-dated parameter silently applies to nothing | warn |

The first row is the one to add first. It is ten lines and would have caught a
95× error that survived a full analysis and two reports.

---

## Tier 1 — data freshness

Automated. The store must be able to say *when* each series last printed, not
just what it says.

| Feed | Source | Cadence | Stale after |
|---|---|---|---|
| LME aluminium, zinc, alumina, coal, coke, silver, FX | Daily Metals Pack (`metals_pack.py`) | daily, manual workbook drop | 3 trading days |
| equity closes | Yahoo `.NS` (`yahoo_prices.py`) | daily | 2 trading days |
| USDINR / USDCNY | metals pack + Yahoo | daily | 2 trading days |
| coal (fallback) | FRED — **needs `curl/8.0` UA, browser UA gets ECONNRESET** | monthly | 45 days |
| broker mail staging | agent + MCP → `data/staging/mail_<date>.json` | daily | same day |

**On any staleness breach: report it, and withhold the affected scores.**
Invariant 7 — withhold rather than guess. A stale price is not a flat price.

**The metals pack is a manual drop and that is a standing risk.** It is the
best data in the system and the only feed a person has to remember. If it stops
arriving, prices go stale silently unless the check above runs.

---

## Tier 2 — corporate actions

Runs daily over the full tape, not just today. Cheap, and the failure it prevents
is severe.

| Check | Why |
|---|---|
| `check_corporate_actions.py` — any daily move ≥15% | VEDL's demerger (773.60 → 271.55, −64.9%) is **unadjusted** in the tape. Any window crossing it compares two different companies |
| a NEW break appears | either a real corporate action needing a wall, or a bad print |

**Do not auto-exclude what it finds.** Four "rolls" were excluded on this basis
and one of them — silver, −26.4% on 2026-01-30 — was a real move present in both
the spot assessment and the futures. That exclusion discarded 63 date-entity
pairs of real data. A flagged break needs a human deciding *what kind* of break
it is.

---

## Tier 3 — the mail watch

`packages/adapters/mail_watch.py`. Two steps, and the split is forced: **the
Microsoft 365 MCP is agent-callable, never Python-callable** — the same
constraint that applies to the Wind MCP.

```
step A (agent)   one search per covered entity, afterDateTime=yesterday
                 strip uri/webLink   →  data/staging/mail_<date>.json
step B (python)  mail_watch.py — entity terms AND structural keywords
```

- Search terms are **derived from the specs** (name, id, `nse_symbol`), so a new
  company is watched as soon as it is modelled.
- Keywords are **keyed by sector**. `"*"` applies everywhere; add a block per
  sector. Steel and cement blocks are already stubbed.
- A record must match **both** a covered name and an event word. Either alone is
  noise — "commissioning" appears in every steel note written.
- Cost: ~8–12k tokens/day. Snippets only; bodies are read on a real candidate.

**Staging files are version-controlled deliberately.** They are the dated record
of what the mailbox returned that morning, which is what lets a catalyst be
graded later without trusting anyone's memory of what was knowable when.

**A missing staging file is reported loudly, never skipped** — a silent skip
looks exactly like a quiet day.

What it is for: **completeness and quantification, not speed.** There is no
same-day edge on a broker note. The value is the structural line buried in 200
emails, and the fact that the bridge can price it in a second.

---

## Tier 4 — compute and persist

| Step | Note |
|---|---|
| refresh feeds | FX **first** — the zinc conversion depends on it |
| episodic scan | candidate sentences only; extraction is never automated |
| run the bridge, score, persist | every row stamped with `spec_version` + `code_sha` |
| report withheld | a withheld pillar is a row saying so, never a gap |

`coverage_ok` false must produce a *recorded withholding*, not a missing row.
Otherwise a gap is ambiguous: did we not score it, or score it and get nothing?

---

## Weekly

| Item | Why |
|---|---|
| rebalance on the week's **closing** print | weekly averaging loses on every horizon — a weekly mean is centred on Wednesday and adds lag to a signal already built from a difference of levels |
| 13-week hold is the horizon | 2-week holds are below a coin flip; the edge peaks at 13w and decays past 18w |
| report 1:1 market value **beside** 2:1 | a 2:1 book is 33% net long. Across every cut tested, 2:1 lands 60–65% and 1:1 lands 52–56%. That gap is market exposure, not skill |

---

## Quarterly — the calendar, not a monitor

These arrive on known dates. They do not need watching, they need a reminder.

| Item | Lands | Changes |
|---|---|---|
| production / operational updates | ~2 weeks after quarter end, **ahead of full results** | `volume` on every output line |
| concall + results presentation | with results | realisation vs spot, hedge ratios, coal linkage mix, contract reset timing |
| spec parameter review | quarterly | anything `verify: pending` — which today is every intensity |

**The look-ahead rule governs all of these.** A dated value carries the date the
market **could have known** it, not the date it happened. Q2 production is about
July–September and knowable in October. Utkal starting in October but announced
in July dates from **July**. Get this backwards and every backtest improves
beautifully and means nothing. `economics.source_note NOT NULL` is what forces it
to be written down.

---

## Never automated, deliberately

| | Why |
|---|---|
| extraction → store | a wrong extraction enters as a fact. The scan says where to look; a person decides |
| spec parameter changes | changing `market_pct` rewrites history for that name |
| acting on a catalyst | the watch flags and quantifies. It does not size and does not trade |

---

## When a sector is added

1. `specs/entities/<sector>.yaml` — entities, lines, intensities, `market_pct`
2. `specs/sectors/<sector>.yaml` — materiality threshold, pair candidates
3. `mail_watch.KEYWORDS["<sector>"]` — the event vocabulary
4. price series for every `price_link`, **with a `units` entry** (Tier 0, row 1)
5. `base_ebitda` per entity

Steps 3–5 are where it will break, and step 4 is where it will break silently.
