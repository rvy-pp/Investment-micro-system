# Indian Auto — Vahan Market Share & SIAM Channel Inventory

**A replication spec. Hand this file to Claude and say: "Build this, then update it every morning."**

It describes two views over India's auto registrations and dispatches, the exact public endpoints behind them, and — most importantly — the traps that produce plausible, wrong numbers without raising an error. Every trap listed here was hit once for real. None of them is hypothetical.

Reference numbers in §11 are as of **2026-09-25**, so a fresh build can check itself.

---

## 0. Instructions for the agent

1. **Build it in whatever stack you like.** Everything below works with the Python standard library (`urllib`, `sqlite3`, `json`, `re`, `openpyxl` only for one optional Excel import). No login, no API key.
2. **Never bypass a CAPTCHA or bot protection.** One Vahan page is CAPTCHA-gated (§6.2) — a human fills it. BSE's announcements API returns 403 to scripts — leave it alone. Nothing in this spec needs either.
3. **Store everything you fetch, keyed by the date you fetched it.** Two of the series here can never be backfilled (§3.4 T7, §6). A day you do not capture is gone.
4. **Treat every number as suspect until a cross-check passes.** §11 lists the checks. Run them after the first build, and keep the automated ones running daily.
5. **When a source is down, keep yesterday's value and say so.** Never write a zero because a fetch failed — a zero in a market-share denominator silently inflates every other maker.

---

## 1. What it produces

### View A — Maker share (four charts)

| chart | segment | lines | Others line? |
|---|---|---|---|
| Two-wheelers | 2W | Hero, TVS, Bajaj, Royal Enfield, Ather | yes |
| Passenger vehicles | PV | Maruti, Mahindra, Hyundai, Tata Motors PV | yes |
| M&HCV | medium + heavy CV | Tata Motors (CV), Ashok Leyland, VE Commercial | **no** (by design) |
| LCV | light CV | Mahindra, Tata Motors (CV), Force Motors | yes |

- **Lines = listed names in NSE F&O only**; everyone else sums into **Others**, computed as a residual.
- **x-axis: the last 13 months**, one point per completed month. The **current month is drawn as a dotted line** from the last completed month to the month-end forecast (§6).
- **Beside each chart, a table:** OEM · live volume (month-to-date) · live share · **YoY change in share points**.
- Legend lists each maker once. No explanatory prose on the page; caveats go in hover tooltips.

### View B — Channel inventory (two charts)

| chart | wholesale (SIAM) | retail (Vahan) |
|---|---|---|
| Two-wheelers | SIAM 2W domestic sales | Vahan `Two Wheeler` |
| Passenger vehicles | SIAM PV domestic sales | Vahan `Four Wheeler` |

- **Monthly bars of `wholesale − retail`** = units that went into (green) or out of (red) dealer stock that month.
- Beside each chart, a 12-month table: month · wholesale · retail · into stock.
- **No running cumulative** — see §8 for why it would lie.

---

## 2. Data sources at a glance

| source | what | granularity | cost | gate |
|---|---|---|---|---|
| Vahan public dashboard JSON API | retail registrations by maker × segment × state | **monthly** (current month = month-to-date) | free | none |
| Vahan report builder (§6.2) | registrations over any date range | a **date range** | free | **CAPTCHA** (human) |
| SIAM monthly press release | wholesale (dispatches) PV / 2W / 3W totals | monthly, ~15 days after month end | free | none |
| NSE `fo_mktlots.csv` | the F&O roster | daily | free | none |

**SIAM does NOT publish vehicle prices or ASP** — not free, not paid. Its only "price" product is a raw-material commodity report. If you need ASP, use company results (revenue ÷ reported wholesale volume).

---

## 3. Vahan — the retail source

### 3.1 Endpoint and parameters

```
GET https://analytics.parivahan.gov.in/analytics/publicdashboard/vahandashboard/durationWiseRegistrationTable
```

No cookie, no token, no CSRF. Answers plain `urllib` with a browser User-Agent in ~0.4s. **Every parameter below must be present** — an empty-string guess returns HTTP 400. The values were read off the dashboard's own network traffic; they are not guessable.

| param | value | note |
|---|---|---|
| `fromYear` / `toYear` | e.g. `2026` | **ignored** — full history comes back regardless; slice client-side |
| `stateCode` | `""` for all-India, or `MH`, `UP`… | see T2 |
| `rtoCode` | **`0`** | not `""` — empty returns 400 |
| `vehicleMakers` | exact maker string, or `""` for all | see §3.5 |
| `vehicleCategoryGroup` | e.g. `Two Wheeler` | segment filter (§3.6) |
| `vehicleSubCategories` | e.g. `HEAVY GOODS VEHICLE` | alternative segment filter (§3.6) |
| `timePeriod` | **`2`** | dashboard default |
| `fitnessCheck` | **`0`** | |
| `calendarType` | **`3`** = monthly | `1` yearly, `2` quarterly — see T3 |
| `archiveTypeAC` | **`ACTIVE_COMPLIANT`** | literal enum string |
| `archiveTypeANC` | **`ACTIVE_NON_COMPLIANT`** | literal enum string |
| `vehicleClasses`, `vehicleEmissions`, `vehicleFuels`, `evType`, `vehicleStatus`, `vehicleOwnerType`, `vehicleType`, `archiveTypePA`, `archiveTypeTA`, `archiveTypeNA` | `""` | must be present, empty |

**Response:** a JSON list of rows `{"year": 0, "yearAsString": "2026-September", "registeredVehicleCount": 56684, "monthlyCountList": null}`. With `calendarType=3` the month label is `"YYYY-MonthName"`.

**Maker-string lookup:**
```
GET https://analytics.parivahan.gov.in/analytics/publicdashboard/lazy/vehicle-makers?page=0&size=50&search=HERO
```
The parameter is **`search`**, not `searchTerm` — a wrong name is silently ignored and you get page 0 of the alphabet (`3EV INDUSTRIES…`), which looks like an answer.

### 3.2 Reference fetch (Python stdlib)

```python
import json, ssl, urllib.parse, urllib.request, time
DASH = "https://analytics.parivahan.gov.in/analytics/publicdashboard/vahandashboard"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

def vahan_monthly(maker="", state="", group=None, subcat=None):
    p = {"fromYear":"2026","toYear":"2026","stateCode":state,"rtoCode":"0",
         "vehicleClasses":"","vehicleMakers":maker,"vehicleSubCategories":subcat or "",
         "vehicleEmissions":"","vehicleFuels":"","timePeriod":"2",
         "vehicleCategoryGroup":group or "","evType":"","vehicleStatus":"",
         "vehicleOwnerType":"","fitnessCheck":"0","vehicleType":"","calendarType":"3",
         "archiveTypeAC":"ACTIVE_COMPLIANT","archiveTypeANC":"ACTIVE_NON_COMPLIANT",
         "archiveTypePA":"","archiveTypeTA":"","archiveTypeNA":""}
    url = f"{DASH}/durationWiseRegistrationTable?{urllib.parse.urlencode(p)}"
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=180, context=CTX) as r:
                rows = json.loads(r.read())
            return {x["yearAsString"]: x["registeredVehicleCount"] for x in rows if x.get("yearAsString")}
        except Exception:
            time.sleep(1 + i)
    raise RuntimeError(f"Vahan failed: maker={maker!r} group={group!r} subcat={subcat!r}")
```

### 3.3 The 36 state codes

`AN AP AR AS BR CG CH DD DL GA GJ HP HR JH JK KA KL LA LD MH ML MN MP MZ NL OR PB PY RJ SK TG TN TR UK UP WB` — all 36 report, Telangana included.

### 3.4 Vahan traps — read these before writing any code

**T1. `top5Makerchart` ignores the year filter entirely.** The endpoint whose name says "makers" returns identical numbers for 2024, 2025 and 2026 — it is an all-time cumulative total wearing a year filter. It *does* honour category filters, so it looks alive. **Never use it.** Use `durationWiseRegistrationTable`.

**T2. An empty response means two different things.**
- **All-India, unfiltered, large maker** → Vahan has an intermittent server-side short-circuit that returns `[]` in 0.3s for the biggest makers (Hero, Maruti). That is a **refusal**, not a zero. It was deterministic on 2026-09-19 and gone by 2026-09-23. Do not rely on either state.
- **With a segment or sub-category filter, or state-scoped** → an empty body is a **genuine zero** (e.g. TAFE registers nothing in Andaman; a small maker has no medium-goods registrations).
- **Rule:** always query with a segment filter. Under a filter, all-India reconciles exactly to the 36-state sum (verified on five months, zero difference) and is 36× cheaper. Treat empty-under-filter as zero **except for a segment total**, which cannot genuinely be empty — refuse that.
- A first version treated every empty as a refusal: one tiny state with no sales killed the whole maker, and **eight OEMs reported exactly zero volume** in a table that otherwise looked fine.

**T3. Monthly is the floor; `calendarType` 4–8 silently fall back to yearly.** No error, just a well-formed yearly series. There is no daily data on this API — daily lives only behind the CAPTCHA'd report builder.

**T4. Maker strings are exact, uppercase, and one company is often several strings.** Chart labels are title-cased for display — pasting one back returns nothing. See §3.5.

**T5. Month-to-date updates ONCE, overnight — not intraday.** Measured: the 2W total read 1,444,108 at 08:10 and exactly 1,444,108 hours later. The morning read is the day's read; the newest point is effectively T-1. Re-running later the same day is harmless and gains nothing.

**T6. Vahan backdates.** A registration files against its own date, so a month keeps growing for days after it ends. Measured on completed months, the effect is small (August +0.025% three weeks later; July +8 vehicles), so completed months are effectively final. Store captures, don't overwrite blindly.

**T7. Daily history cannot be backfilled.** A past date's month-to-date level was never published. If you want a daily series, capture every morning from day one.

**T8. Partial server outages happen, and they are maker-specific.** On 2026-09-25 Vahan returned HTTP 500 at *every* scope for Ola Electric, BMW, Ashok Leyland + Four Wheeler, and the entire three-wheeler category — while segment totals and most makers worked. A retry does not help; wait a day. Your tests must report these as **SKIP, not FAIL**, or a source outage looks like broken code.

### 3.5 Maker strings — exact, as Vahan stores them

| line | exact Vahan string(s) — sum when several |
|---|---|
| Hero MotoCorp | `HERO MOTOCORP LTD` |
| TVS Motor | `TVS MOTOR COMPANY LTD` |
| Bajaj Auto | `BAJAJ AUTO LTD` |
| Royal Enfield | `ROYAL-ENFIELD (UNIT OF EICHER LTD)` |
| Ather Energy | `ATHER ENERGY LTD` |
| Maruti Suzuki | `MARUTI SUZUKI INDIA LTD` |
| Mahindra (PV) | `MAHINDRA & MAHINDRA LIMITED` |
| Hyundai | `HYUNDAI MOTOR INDIA LTD` |
| Tata Motors PV | `TATA MOTORS PASSENGER VEHICLES LTD` |
| Tata Motors (CV) | `TATA MOTORS LTD` |
| Ashok Leyland | `ASHOK LEYLAND LTD` + `ASHOK LEYLAND LTD.` |
| VE Commercial | `VE COMMERCIAL VEHICLES LTD` + `VE COMMERCIAL VEHICLES LTD (VOLVO BUSES DIVISION)` |
| Mahindra (LCV) | `MAHINDRA & MAHINDRA LIMITED` + `SML MAHINDRA LTD` |
| Force Motors | `FORCE MOTORS LIMITED` |

Things that look wrong and aren't:
- **`EICHER MOTORS LTD` registers zero vehicles.** Every Eicher truck files as VE Commercial. "VECV and Eicher" is one line. Eicher's motorcycles file as Royal Enfield.
- **`ASHOK LEYLAND LTD.`** (trailing period) is a data-entry duplicate — 36 vehicles in its whole life. Sum it anyway; harmless.
- **Tata Motors is two companies** since the 2025-10 demerger: the PV arm and the CV arm are separate strings and separate listed entities.
- **The same `MAHINDRA & MAHINDRA LIMITED` string covers SUVs *and* the Bolero pickup.** Only the segment filter separates them — query it unfiltered and you get a blend (39% goods vehicles in one state sample).
- `HARLEY DAVIDSON (IMPORTER: HERO MOTOCORP)` is **not** Hero's own volume.

### 3.6 Segments — which filter carves each one

| segment | filter | values |
|---|---|---|
| 2W | `vehicleCategoryGroup` | `Two Wheeler` |
| PV | `vehicleCategoryGroup` | `Four Wheeler` |
| M&HCV | `vehicleSubCategories` | `HEAVY GOODS VEHICLE`, `MEDIUM GOODS VEHICLE`, `HEAVY PASSENGER VEHICLE`, `MEDIUM PASSENGER VEHICLE` (sum) |
| LCV | `vehicleSubCategories` | `LIGHT GOODS VEHICLE`, `LIGHT PASSENGER VEHICLE` (sum) |

- **`Four Wheeler` is passenger.** Verified: Maruti 168,502 there in Aug-2026 against Ashok Leyland's 1. Test it as a *ratio* (a pure CV maker is <0.1% of its own CV volume), not as `== 0` — one stray vehicle breaks an equality test.
- **Split CV on sub-category, not by listing makers.** Ashok Leyland and Tata straddle both tiers (Aug-2026 M&HCV/LCV: Ashok Leyland 10,495 / 6,595; Tata 14,021 / 17,920). A maker list cannot express that.
- **`LIGHT PASSENGER VEHICLE` is mostly taxi-registered cars** (Maruti alone is ~23,600/month of it). It is in LCV because it is the only place Force Motors' Traveller van appears. The cost: Maruti is ~27% of the LCV chart, inside Others.
- **Category groups overlap ~0.6%**, so they are not a partition of the whole. Irrelevant to shares, because every share is computed inside one segment against that segment's own total.
- **Three-wheelers are excluded from both views.** See §8.

---

## 4. The F&O roster — read it, never hardcode it

```
GET https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv          # the roster (column 2 = symbol)
GET https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip   # what actually traded (stock futures: FinInstrmTp = STF)
```

A name that leaves F&O and keeps its own line **stops being in Others** — the shares still sum to 100% and nothing raises. So derive the roster from contracts that traded in the last 30 days, every run.

| symbol | line(s) |
|---|---|
| `HEROMOTOCO` `TVSMOTOR` `BAJAJ-AUTO` `ATHERENERG` | 2W |
| `EICHERMOT` | Royal Enfield (2W) and VE Commercial (M&HCV) |
| `MARUTI` `HYUNDAI` `TMPV` | PV |
| `M&M` | Mahindra (PV and LCV) |
| `ASHOKLEY` | M&HCV |
| `FORCEMOT` | LCV |
| **`TMCV`** | Tata Motors CV — **not in F&O as of 2026-09-25; drawn by explicit exemption** |

**Tata Motors CV is an exemption, on purpose.** TMCV is listed (`TMCV.NS`) but still in its post-demerger qualification period and absent from the roster. Without it, Others was 54% of M&HCV and 70% of LCV — India's largest CV maker hidden in the residual. Keep an explicit exemption list and **test that it hasn't gone stale**: the day TMCV enters F&O, the exemption is no longer needed.

**Ather (`ATHERENERG`) is in F&O** (first contract 2026-08-26).

---

## 5. Share computation

```
share(maker)  = maker_count / segment_total                      # segment_total fetched as its OWN query (maker = "")
Others        = segment_total − Σ named makers                   # a residual, never a separate query
```

- **Others is derived**, so lines always sum to 100% and every unlisted maker is inside it. If `Others < 0`, a maker string is being double-counted — **raise, don't clamp**; a clamped zero reads as "no unlisted makers", a claim about the market.
- **M&HCV draws no Others line**, but its shares are still computed against the *full* segment total. The three drawn lines therefore sum to ~85–89%, not 100%. Do not rebase them onto the smaller universe — that would inflate every share.
- **YoY in the side table is in share points**, not % of volume: a maker can grow volume 20% and still be losing share. Compare the current month to **the same month last year, looked up by name** (not by array index).
- The current month is month-to-date against a completed month last year — a small basis mismatch (share drifts only ±0.3pp on 2W within a month). State it in the column tooltip.

**Daily capture** (the minimum): each morning, for every segment, fetch the segment total and each mapped maker string for the last 13 months, and store `(capture_date, period, segment, label, count)`. About 18 requests, ~15 seconds with a thread pool.

---

## 6. Month-end forecast (the dotted line)

**Goal:** from the month-to-date count on day *d*, project the month's final total.

### 6.1 Work in working days, never calendar days

Registrations stop on Sundays. **The sign of the trend depends on this.** Measured on Hero, September 2026: days 20–23 ran 13,466 per calendar day against 14,188 over days 1–19 (a "slowdown"); per working day it was 17,955 against 15,857, a 13% **acceleration**. The whole apparent slowdown was one Sunday.

```
wd_fraction(d) = working_days(1..d) / working_days(whole month)        # Sundays excluded; public holidays deliberately NOT modelled
```

### 6.2 The month is back-loaded — measure how much, per maker

Registrations run heavier late in the month (dealer month-end push), mildly for 2W and more for PV. Measure it:

```
skew(maker, d) = [ partial_1_to_d / full_month ] / wd_fraction(d)       # 1.00 = no back-loading
forecast       = MTD / ( wd_fraction(d) × skew(maker, d) )
segment total  = Σ per-maker forecasts                                   # so projected shares add to 100%
```

**`partial_1_to_d` needs the CAPTCHA'd report builder** — the only place Vahan gives an arbitrary date range. `full_month` comes free from the API. **A human does this part:**

```
https://analytics.parivahan.gov.in/analytics/vahanpublicreport?lang=en
```

| field | set to |
|---|---|
| Year Type | **1 Month Flexible** (the only option that reveals From/To dates) |
| From / To | **From = the 1st**, To = the cut day (e.g. 1 Aug 2026 → 24 Aug 2026) — cumulative, not two halves |
| Y-Axis | **Maker** (set Y first — the X-Axis list is rebuilt from it) |
| X-Axis | **Vehicle Category Group** (2W/PV) — or **Sub-Category** to fit M&HCV/LCV |
| Maker filter | leave blank for all makers |
| CAPTCHA | type it, then **Apply**; export Excel |

Limits: To must be within one month of From and not past today. There is no day-by-day axis — one query per cut. The Excel's first row carries the range: `Maker and Vehicle Category Group Data for All State (01 Aug 2026 to 24 Aug 2026)`; **read the range from that title, not the filename** (downloads arrive as `…_all_records (2).xlsx`). Header is row 3; a `Total` column lets you cross-foot every row.

**Why per maker:** a segment-wide skew cancels out of a share — `(mtd/f) / (total/f) = mtd/total` — so the dotted line would be **flat by construction**, reading as "share will not move". Makers genuinely differ (§11: Tata PV 0.829 against Maruti 0.912).

**The skew is a curve, not a constant.** It relaxes toward 1.00 as the cut approaches month end (2W: 0.911 at day 15, 0.983 at day 24). Key it on the cut day; don't reuse a day-15 factor on day 24. If no harvest exists for today's cut day, **fall back to 1.00 and flag it** ("no back-loading applied") rather than interpolating across a curve with two points.

### 6.3 Exclude festive months — judged across the whole month

The festive season keys on the **lunar calendar**, not the calendar month. Sep-2025's day-24 ratios were 0.722 (2W), 0.659 (PV), 0.873 (CV) — far outside the normal 0.85–1.01 — because Navratri began 22 Sep 2025 (October 2025 2W then printed +140.7% m/m). A forecast built on that shape said September 2026 would be **+75% YoY** against a market running +20–30%.

- **Exclude a month if any segment's ratio falls outside 0.80–1.15**, and exclude it from **every** segment together. A per-segment test kept Sep-2025 for CV (0.873 is inside the band) and silently blended a festive month into CV's factor.
- **Do not use a year-on-year anchor** (`last_year_total × MTD/last_year_MTD`). It cancels shape only when the shape repeats, and it doesn't when the festival moves month.
- **October will not look like last October** if the festival has shifted to November. A naive YoY will show a collapse that is only the calendar.

---

## 7. SIAM — the wholesale source

### 7.1 What it gives

```
GET https://www.siam.in/pressrelease-details.aspx?mpgid=53&pgidtrail=50&pid={pid}
```

The free monthly release is **three numbers**: PV, 2W and 3W domestic sales (plus a production total), published ~15 days after month end. Monthly releases run from **pid ≈ 560 (Feb 2024)** upward; Aug-2026 is pid 620. Company-wise (₹53,350/yr) and model-wise (₹47,300/yr) data are paid, and CV is quarterly and paid.

### 7.2 Discovery

**An unused pid returns HTTP 200, never 404** — SIAM serves the bare site chrome for pid 623, 700 or 9999. "Scan until 404" never ends.

- Detect empty pages by **measuring the chrome each run** (fetch pid 999999; it was 3,557 characters of text) and treating any page within ~300 characters of it as empty.
- **Do not use a fixed length threshold.** A threshold of 6,000 characters, calibrated on a long convention write-up, silently classified every ordinary monthly release (4,898–5,319 chars) as empty. Only quarter-end releases (~9,585 chars) passed; the backfill stored ten months and reported success.
- Scan forward from the last real pid and stop after **12 consecutive empty pids**. Re-check empty pids on later runs — next month's release appears there.

### 7.3 Parser

```python
import re, html
MONTHS = ["January","February","March","April","May","June","July","August",
          "September","October","November","December"]

def to_text(raw):
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    for a, b in (("‘","'"),("’","'"),("“",'"'),("”",'"')):
        t = t.replace(a, b)                                  # curly quotes break regexes
    return re.sub(r"\s+", " ", t)

def parse_siam(text):
    m = re.search(r"Monthly Performance:\s*(" + "|".join(MONTHS) + r")\s+(20\d\d)", text)
    if not m: return None                                     # not a data release
    tail = text[m.end():]
    q = re.search(r"Performance:\s*(?:" + "|".join(MONTHS) + r")\s*[-–]\s*(?:" + "|".join(MONTHS) + ")", tail)
    blk = text[m.start(): m.end() + (q.start() if q else 1500)]   # MONTHLY block only — cut before the quarter
    out = {"period": f"{m.group(2)}-{m.group(1)}"}
    for seg, pat in {"PV": r"Passenger Vehicles\s*\d?\s*sales were",
                     "2W": r"Two-?\s?wheeler sales were",
                     "3W": r"Three-?\s?wheeler sales were"}.items():
        g = re.search(pat + r"\s*(?:units\s*)?([\d,]{4,})\s*units", blk, re.I)
        out[seg] = int(g.group(1).replace(",", "")) if g else None
    return out
```

**SIAM parse traps:**
- **A quarter read as a month.** Sep-2025 prints `84,077units` (no space). A regex requiring a space skipped it and took the *next* figure on the page — the July–September **quarterly** total, 229,239, three times too high and plausible. Hence the monthly block is cut at the quarterly heading, and a month over **2.2× the median of its neighbours** is refused.
- **The source's own typo:** `were units 19,02,209 units` (May and Jul 2026). Tolerated by the `(?:units\s*)?`.
- **SIAM reused a number:** Dec-2025's "PV without Tata Motors" is identical to Dec-2024's and implies Tata sold 128,512 PVs — impossible. **Don't store the ex-Tata figure.** Tata *is* included in the headline PV in every month, so the headline series has no basis break.

---

## 8. Channel inventory

```
net(month, segment) = SIAM_wholesale − Vahan_retail        # > 0: stock built; < 0: stock drawn down
```

Join only on months where **both** sides exist; the current month appears once SIAM prints it.

- **Monthly is the floor**, set by SIAM. Retail is daily; wholesale is monthly and late.
- **PV and 2W only.** SIAM publishes no monthly CV. **3W is excluded by design:** Vahan's three-wheeler count is dominated by e-rickshaws whose makers are not SIAM members, so retail would exceed wholesale every month.
- **No running cumulative.** Vahan counts makers SIAM does not: **Ola Electric is not a SIAM member** (nor Kinetic Green, Okinawa, Revolt, Hero Electric), and BMW, Mercedes, JLR and Volvo are members whose data is "not available". Every month reads slightly **low** — Ola alone ~8–16k/month. That's 1–2% of a festive swing, so one bar is sound; summed over 30 months it drifts ~150k a year into destocking that never happened. To add a cumulative, first subtract those makers from Vahan retail.
- The festive cycle is the thing to watch: in Sep-2025 dealers loaded +814,785 two-wheelers ahead of Navratri, then retail drew down −1,029,867 (Oct) and −702,423 (Nov).
- Registration lags dispatch, and December buyers register in January for the model-year benefit, so read multi-month patterns rather than single-month noise.

---

## 9. Daily update procedure

Run every morning (after ~08:00 IST; Vahan's overnight update is in by then):

1. **Refresh the F&O roster** from `fo_mktlots.csv` / the latest bhavcopy.
2. **Vahan share capture** — for each segment (§3.6), fetch the segment total plus every mapped maker string (§3.5), last 13 months, all-India with the segment filter. Store with today's `capture_date`. If a maker 500s (T8), keep its last stored value and flag it; if a **segment total** fails, refuse the whole segment for today.
3. **Vahan retail totals** for the channel view — `Two Wheeler` and `Four Wheeler`, full history, one call each. Latest capture wins per month.
4. **SIAM** — scan from the last real pid for a new release (§7.2); parse, plausibility-check, store.
5. **Recompute and render:** shares, forecast (§6), channel (§8).
6. **Run the automated checks** (§11) and report SKIP separately from FAIL.

When a new month-shape harvest exists (a human exported it from the report builder, §6.2), load it and refit the skews.

---

## 10. Suggested storage

```sql
CREATE TABLE vahan_share (            -- one row per capture x month x segment x line
  capture_date TEXT, period TEXT, segment TEXT, label TEXT,   -- label '__TOTAL__' = segment total
  registrations INTEGER NOT NULL CHECK (registrations >= 0),
  PRIMARY KEY (capture_date, period, segment, label));

CREATE TABLE vahan_month_shape (      -- report-builder harvests, for the skew
  period TEXT, cut_day INTEGER, segment TEXT, label TEXT, partial INTEGER, source TEXT NOT NULL,
  PRIMARY KEY (period, cut_day, segment, label));

CREATE TABLE siam_wholesale (
  period TEXT, segment TEXT, units INTEGER CHECK (units > 0), pid INTEGER, fetched TEXT,
  PRIMARY KEY (period, segment));

CREATE TABLE vahan_retail_monthly (   -- latest capture wins
  period TEXT, segment TEXT, units INTEGER, capture_date TEXT,
  PRIMARY KEY (period, segment));
```

Keep a `source`/provenance column on anything a human harvested. Never put these counts in a price table.

---

## 11. Verification — your build should reproduce these

### Automated checks (run daily)

| check | pass condition |
|---|---|
| filtered all-India == 36-state sum | Hero / `Two Wheeler` for two months: **difference exactly 0** |
| Four Wheeler is passenger | Maruti > 100k, and Ashok Leyland's `Four Wheeler` < 0.1% of its own CV |
| shares sum to 100 | per segment and capture (except M&HCV drawn lines, which sum to ~85–89%) |
| Others ≥ 0 | raise otherwise |
| F&O exemptions still needed | every exempt symbol is still absent from the roster |
| forecast shares sum to 100 | per segment |
| SIAM month plausibility | no month > 2.2× its neighbours' median |
| SIAM empty-page detector | a 4,898-character page is classified **real** |
| trap T1 still present | `top5Makerchart` returns identical numbers for 2024 and 2026 — if not, it was fixed upstream; re-test before using it |

### Reference values (as of 2026-09-25)

**Aug-2026 completed-month shares:**

| segment | total | shares |
|---|--:|---|
| 2W | 1,725,580 | Hero 24.10% · TVS 20.62% · Bajaj 9.16% · RE 5.60% · Ather 1.68% · Others 38.84% |
| PV | 413,389 | Maruti 40.78% · Mahindra 11.13% · Hyundai 11.61% · Tata PV 10.68% · Others 25.80% |
| M&HCV | 35,859 | Tata 39.10% · Ashok Leyland 29.27% · VECV 16.80% · (Others 14.83%, not drawn) |
| LCV | 100,979 | Mahindra 24.30% · Tata 17.75% · Force 2.99% · Others 54.96% |

**Per-maker skew at day 24** (fitted on Aug-2026; Sep-2025 excluded as festive):

| 2W | skew | PV | skew |
|---|--:|---|--:|
| Hero | 0.9752 | Maruti | 0.9121 |
| TVS | 0.9955 | Mahindra | 0.9314 |
| Bajaj | 0.9756 | Hyundai | 0.9159 |
| Royal Enfield | 0.9969 | Tata Motors PV | 0.8293 |
| Ather | 0.9849 | Others | 0.9335 |
| Others | 0.9802 | | |

M&HCV and LCV have no harvest yet (skew 1.00, flagged) — export once with X-Axis = **Sub-Category** to fit them.

**September 2026 forecast** (MTD to day 24, working-day fraction 21/26 = 0.8077): 2W ≈ 1,731,000 (+28.6% YoY) · PV ≈ 412,000 (+27.9%) · both inside the July/August YoY band (+20–30%), which is the plausibility check.

**Channel inventory:**

| | Aug-2026 wholesale | Aug-2026 retail | into stock | Sep-2025 into stock |
|---|--:|--:|--:|--:|
| 2W | 2,034,698 | 1,725,580 | +309,118 | +814,785 |
| PV | 439,309 | 413,389 | +25,920 | +50,053 |

---

## 12. Known limitations and open items

- **No daily history before your first capture** (T7). The monthly series is long; the daily one starts the day you start.
- **The forecast skew rests on one normal month per segment** (Aug-2026 at day 24). More harvests — especially at the cut days you forecast from — sharpen it.
- **M&HCV and LCV forecasts have no skew yet** (plain working-day extrapolation, flagged).
- **Channel inventory is uncorrected for coverage** (§8). Adding Ola Electric and the luxury PV makers to the Vahan side would allow a cumulative.
- **Company-wise wholesale is not free.** SIAM sells it; BSE's filings API blocks scripts; brokers' monthly auto notes carry growth rates in the body and units behind a PDF link.
- **Retail ≠ revenue.** Company revenue is booked on wholesale dispatches, so a revenue estimate should multiply **wholesale** volume by ASP, not Vahan retail.
