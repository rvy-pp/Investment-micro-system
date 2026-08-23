# Replicating this system on another machine

Written 2026-08-23. Read section 1 before starting — **the repository does not
contain the data**, and three inputs are not fetchable from the public internet.
Cloning and running `pipeline.py` will not give you a working system. Knowing why
up front saves an hour.

---

## 1. What is and is not in the repo

`git clone` gives you **code, specs and staging captures**. That is all.

| in the repo | NOT in the repo (`.gitignore`) |
|---|---|
| every `.py` under `packages/` | `data/ims.db` — the entire store |
| `specs/` — entities, sectors, scoring, extracted facts | `snapshots/` |
| `data/staging/*.csv`, `*.json` — dated Wind/Westmetall captures | generated pages, `_*.json`, the audit workbook |
| `docs/`, `CLAUDE.md`, `README.md` | everything else under `data/` |

The store is rebuildable and section 4 is the order. What is **not** rebuildable
from the repo alone:

| input | why it is a problem | consequence if absent |
|---|---|---|
| **Daily Metals Pack** `.xlsx` | a broker workbook, emailed, saved by hand | no LME cash, no assessed alumina, no LME zinc, no pet coke. P1 and P3 degrade badly |
| **Obsidian vault** | a OneDrive folder on the PM's machine | no OI, no broker digests, no concall notes → mood and guidance have no input |
| **Wind MCP** | agent-callable only, licensed | SHFE zinc. A dated capture is committed under `data/staging/`, so survivable |
| **Microsoft 365 MCP** | agent-callable only | the daily mail watch. Not needed to rebuild history |

Yahoo and FRED are public and need no credentials.

---

## 2. Prerequisites

```bash
python --version     # 3.12+. STRICT tables need sqlite >= 3.37
git --version
```

There is deliberately no `requirements.txt` — the core runs on the standard
library. Two packages are needed:

```bash
pip install pyyaml openpyxl
```

`pyyaml` reads the specs, `openpyxl` reads the metals pack. `check_deps.py` also
probes fastapi/uvicorn/pydantic — **ignore those**. They are vestigial from an
earlier design and nothing imports them; the API is stdlib `http.server`.

```bash
git clone https://github.com/rvy-pp/Investment-micro-system.git
cd Investment-micro-system
```

**Keep the working copy OFF OneDrive.** SQLite writes `.db`, `.db-wal` and
`.db-shm` as one transaction; OneDrive syncs them as three independent files and a
mid-write sync corrupts the store. A closed snapshot on OneDrive is fine — the
live database is not. See the comment block in `packages/core/snapshot.py`.

---

## 3. Paths you must change

Four modules hardcode the PM's machine. Edit before running anything.

| file | what to point at |
|---|---|
| `packages/extract/candidates.py` | `<vault>/Broker Mails` |
| `packages/adapters/vault_oi.py` | `<vault>/Coverage` |
| `packages/adapters/mail_watch.py` | `<vault>/Broker Mails` (in `from_digests`) |
| `packages/core/snapshot.py` | your backup target, or just skip `--mirror` |

With no vault at all, skip these and accept that mood and guidance withhold.
**That is a correct outcome, not a broken one** — the system withholds rather
than guesses (invariant 7).

---

## 4. Rebuild order

Order matters: FX before anything USD-denominated, prices before scores,
everything before `preflight`.

### 4.1 Schema, and prove the guards work

```bash
python packages/core/init_db.py
```

Creates `data/ims.db`, then attempts **7 illegal inserts that must all be
rejected** and **2 legal ones that must be accepted**. If any line says FAIL,
stop. A constraint that rejects *everything* passes every rejection test — which
is precisely what `GLOB '____-__-__'` did (`SILENT_BUGS.md` #1). That is why the
acceptance tests exist.

### 4.2 Prices — Yahoo (public, no key)

```bash
python packages/adapters/yahoo_prices.py --probe
python packages/adapters/yahoo_prices.py --load --range 2y
```

Probe first. Symbols are validated **by name pattern, not instrument type**,
because `ZN=F` is the 10-Year T-Note and `ZNC=F` is a dead 2019 contract returning
a frozen price. Five `NO FEED` rows are expected — those arrive as cited
observations, not feeds.

Valuation z-scores want a longer equity history:

```bash
python -c "
import sys,sqlite3; sys.path.insert(0,'packages/adapters')
import yahoo_prices as Y
c=sqlite3.connect('data/ims.db')
for e in ('hindalco','nalco','hindustan_zinc','vedanta','vaml'):
    sym,pat = Y.CANDIDATES[e][0]
    try: s = Y.fetch(sym,'15y',pat)
    except Exception as ex: print(e,'FAIL',ex); continue
    c.executemany('INSERT OR REPLACE INTO prices (entity_id,date,close) VALUES (?,?,?)',
                  [(e,d,v) for d,v in s]); print(e,len(s))
c.commit()"
```

### 4.3 Prices — the Daily Metals Pack (the important one)

```bash
python packages/adapters/metals_pack.py --file "<path>.xlsx" --probe
python packages/adapters/metals_pack.py --file "<path>.xlsx" --load
```

~39,000 rows, daily to 2010. **Load it AFTER Yahoo so it overwrites those
series**, not before. It supersedes four of them:

- `lme_zinc` — Yahoo has **no feed at all**; this was blocking the whole zinc group
- `cp_coke` — specified at 0.40 t/t and never priced, so anode cost was zero in every bridge
- `alumina_index` — Yahoo's `ALA=F` carries a −21.3% roll on 2025-02-03 that is not a price move
- `lme_aluminium` — Yahoo's `ALI=F` is CME and carries a Midwest premium basis

The sheet name has a **trailing space** (`"Daily prices "`) and the header is on
row 18. Both handled; do not "fix" them.

### 4.4 Prices — FRED and Wind

```bash
python packages/adapters/fred_prices.py --load
python packages/adapters/wind_zinc.py --load
```

**Note the `--load`.** Both default to probe-only, as does `vault_oi.py`. Running
them bare prints a report and writes nothing — which looks like success.

FRED **kills the connection on a browser User-Agent** (`ECONNRESET`) — it needs
`curl/8.0`. Yahoo wants the opposite. Do not unify them.

Wind zinc loads from the committed capture at `data/staging/zn_shf_close.csv`. The
Wind MCP is callable by the **agent**, never by a Python process — that is why
fetch and load are separate steps and why the capture is version-controlled.

### 4.5 Check the tape before scoring anything

```bash
python packages/adapters/check_corporate_actions.py
```

This one genuinely takes no flags — it only reads. Expect VEDL **−64.9% on 2026-04-30** — a real unadjusted demerger, walled off in
code — plus futures rolls on the Yahoo proxies.

**Do not auto-exclude what this finds.** Silver's −26.4% was wrongly classified as
a roll and that exclusion discarded 63 date-entity pairs of real data
(`SILENT_BUGS.md` #5). A flagged break needs a human deciding what kind it is.

### 4.6 Cited facts

```bash
python packages/extract/load_observations.py --file specs/extracted/cp_coke_prices.json
python packages/extract/load_observations.py --file specs/extracted/policy_aluminium.json
python packages/extract/load_guidance.py    --file specs/extracted/hzl_guidance.json
python packages/extract/load_concall_guidance.py --write
python packages/adapters/vault_oi.py --load               # needs the vault
python packages/extract/extract_broker_actions.py --load  # needs the vault digests
```

**Every one of these needs an explicit flag.** `load_observations` and
`load_guidance` require `--file` and print usage otherwise; `vault_oi` and
`extract_broker_actions` default to probe/dry and write nothing without `--load`.
Run bare, they print something that looks like output and change nothing — the
same shape as every entry in `SILENT_BUGS.md`. Check `db_state.py` row counts
after this step rather than trusting the console.

`load_concall_guidance.py` is hand-transcribed and self-contained, so it runs
without the vault. The last two do not.

### 4.7 Preflight — stop here if it fails

```bash
python packages/core/preflight.py
```

Must report **0 failures**. Two warnings about Novelis `can_sheet_spread` and
`al_scrap_midwest` are expected and permanent — no public feed exists for either.

The check that matters most: every `price_link` has a `units` entry. `to_inr`
converts only when the unit starts with `"USD"` and otherwise returns the delta
**unchanged**, so a missing entry silently drops the FX leg — a 95× understatement
that survived a full analysis and two written reports (`SILENT_BUGS.md` #3).

### 4.8 Score

```bash
python packages/score/run_scores.py --as-of <latest-price-date>
python packages/score/run_scores.py --backfill 60      # slow, ~10 min
python packages/score/backfill_p1.py --days 730 --write
```

`--backfill` is slow because valuation rebuilds a full spot-multiple series per
entity per date. Background it.

### 4.9 Read it

```bash
python packages/core/db_state.py
python packages/score/combined.py
python packages/score/conviction.py --compare
python packages/api/serve.py            # -> http://127.0.0.1:8770
```

**Port 8770, not 8765.** 8765 is the old vault dashboard; a bind clash does not
error visibly — the other server answers and every request 404s as if routing
were broken.

---

## 5. Verification

Row counts should match; scores drift with prices.

```
init_db      7 rejections ok, 2 acceptances ok, 21 tables, 4 views
db_state     prices ~54,000 · pillar_scores ~5,500 · guidance 26 · broker_actions 39
preflight    0 failures, 2 warnings
combined     5 entities; econ/val/mood on all, guidance on 4
```

Four spot checks that catch the classic failures:

| check | expected | catches |
|---|---|---|
| latest `lme_zinc` close | ~3,850 USD/t | metals pack not loaded |
| `whatif.py` +10% zinc on HZL | ~+9.4% of EBITDA | missing `units` entry — would read ~+0.1% |
| NALCO spot EV/EBITDA | ~7.1x against a ~5.1x mean | valuation base quarter wrong |
| HZL guidance gaps | cost +12.7%, volume −5.5%, silver −12.4% | concall actuals not loaded |

---

## 6. The daily loop

```bash
python packages/adapters/yahoo_prices.py --load --range 5d
python packages/adapters/metals_pack.py --file "<today's pack>.xlsx" --load
python packages/core/preflight.py
python packages/score/run_scores.py --as-of <today>
python packages/score/combined.py
```

Agent-driven steps, which cannot run from Python — see `docs/DAILY_MONITORING.md`:

- `mail-fetch` skill writes `data/staging/mail_<date>.json`
- `python packages/adapters/mail_watch.py` filters it for structural events
- `concall-ingest` skill, quarterly, per name

---

## 7. Read before changing anything

| file | why |
|---|---|
| `CLAUDE.md` | the invariants. Breaking one silently is this project's recurring failure |
| `docs/SILENT_BUGS.md` | six bugs that returned plausible wrong numbers. **Nothing in the test suite caught any of them** |
| `docs/DAILY_MONITORING.md` | what must be watched daily, and what happens on failure |
| `docs/FLOWS.md` | the unbuilt regime gate, scoped only |
| `git log` | the real record — every message states what was found, fixed, and deliberately not fixed |

---

## 8. Known state, so you are not surprised

- **Two combination rules print side by side and neither is canonical.** `COMP` is
  a weighted average; `SIZE` is conviction sizing. The average contradicts
  `README:91` ("L2 never sets direction"); sizing is unvalidated. Open on purpose.
- **The backtest does not pass** — ~7 independent windows over 5 correlated names.
  It is not refuted either; the sample cannot answer it.
- **P1 is a short-term description, not a forecast.** It correlates with the move
  already past 2–3× more strongly than the move ahead. Do not retune to fix that
  — the ceiling is informational, not parametric.
- **Mood still reads from the retired vault pipeline.** It works and has no live feed.
- **Roll-forward detection is built and inert** — only 5 of 39 notes state a base period.
- **No position limit exists anywhere**, so "full position" has no %NAV attached,
  and the regime gate is hardcoded to 1.
- **The zinc pair has no P1 signal by construction** — VEDL is HZL × 0.634 and
  `pct_of_ebitda` divides the scaling straight back out.
