---
name: concall
description: Earnings-call analyst note for one company and quarter — "/concall tcs q2fy27". Resolves the name, reads the FULL call transcript Google Finance builds from the call audio (or a PDF the PM supplies), runs the vault's earnings-transcript analysis on it, and saves the note to AI Insights/<TICKER>_Concall/, where the Results tab's Concall notes sub-tab shows it. Says "no concall found" when Google Finance does not carry that quarter. Use when asked to run /concall, analyse/read/summarise an earnings call, or write a concall note.
---

# /concall — one call, one analyst note

`/concall <company> <quarter> [quick] [redo]` — e.g. `/concall tcs q2fy27`,
`/concall tata steel Q1 FY27 quick`. A PDF path or an uploaded transcript in
place of the quarter lookup also works (Step 3b).

**Exactly two outcomes, the PM's rule (2026-10-08):** either the reply is
**"No concall found for <NAME> <Qn FYyy>"** with the one-line reason, or a full
analyst note written off the **complete** transcript. Never a note built from
Google's AI summary, news headlines or a partial read, and never a
silently-substituted quarter.

The note is the vault's `/concall` note — same method, same structure, same
folder — so next quarter grades against it and `vault_concall.py` (P4) and the
Results tab read it unchanged. This skill is the path to it from THIS project;
the vault's `.claude/commands/concall.md` still exists for vault sessions.

## Step 1 — resolve the request (script, not judgement)

```bash
python packages/company/concall_target.py <company words> <quarter>
```

Returns `entity`, `ticker` (the vault folder key), `google_symbol`,
`google_url`, `google_label` (e.g. `Fiscal Q2 2027 earnings call`),
`note_path` / `note_exists`, `prior_path` / `prior_exists`.

- `ok: false` + `ambiguous company` → ask the PM which one; do not pick.
- `ok: false` otherwise → report the error; stop.
- `resolved: as_symbol…` → not a covered name; proceed, but the company name on
  the Google page must match what the PM meant before you read a word.
- **`ticker` and `google_symbol` differ on purpose** (LTIMindtree trades as
  `LTM`, folder `LTIM`; VAML's folder is `VEDANTALUM`). Open Google with
  `google_symbol`; save under `ticker`. Never derive one from the other.

**Note already on file** (`note_exists: true`) and no `redo` → say so, give the
path, and present the existing note's Bottom line. Do not write a second
reading of the same call. With `redo`, overwrite it.

## Step 2 — Google Finance: is this quarter's call there?

Use the **built-in browser** (`mcp__Claude_Browser__*`). Navigate to
`google_url`, wait ~3s, then run this in the page (`javascript_tool`):

```js
(() => {
  const t = document.querySelector('main').innerText;
  const label = (t.match(/Fiscal Q[1-4] \d{4} earnings call/) || [null])[0];
  const period = (t.match(/Fiscal period\n(Q[1-4] \d{4})/) || [,null])[1];
  const hasTx = /\nCall transcript\n/.test(t);
  const company = t.slice(0, t.indexOf('Overview')).split('\n').find(l => / Ltd| Limited|Inc\b/.test(l));
  const est = [...t.matchAll(/\n((?:Normalized EPS|Revenue)) \/ Est\. \(([A-Z]{3})\)\n([^\n]+)\n([^\n]+)\n(beat|miss)\n/g)].map(m => m.slice(1).join(' | '));
  let s = t.indexOf('\nexpand_all\n'); s = s >= 0 ? s + 12 : t.indexOf('[Operator');
  let e = t.length;
  for (const m of ['\nAt a glance\n', '\ninsights_auto\n', '\nRelated earnings\n']) { const i = t.indexOf(m, s); if (i > 0 && i < e) e = i; }
  const raw = t.slice(s, e);
  const stamp = /^(?:\d+h )?(?:\d+m)?(?: ?\d+s)?$/;
  const stamps = raw.split('\n').map(l => l.trim()).filter(l => l && stamp.test(l));
  window.__cc = raw.split('\n').filter(l => !/^(music_history|Listen from here)$/.test(l.trim()) && !(l.trim() && stamp.test(l.trim()))).join('\n').replace(/\n{3,}/g, '\n\n');
  return {company, label, period, hasTx, est, chars: window.__cc.length, chunks: Math.ceil(window.__cc.length / 20000), last_stamp: stamps[stamps.length - 1], end: window.__cc.slice(-250)};
})()
```

**Decide, in this order — each failure ends the skill with "No concall found":**

| check | fails when | reply |
|---|---|---|
| company | `company` is not the company the PM meant | "Google Finance's <SYM> page is <other company>" |
| quarter | `label` ≠ `google_label` | "Google Finance carries only the latest call — <label> — not <Qn FYyy>" |
| transcript | `hasTx` false or `chars` < 15,000 | "The <label> page has no call transcript (yet)" |

Google shows **only each company's latest call**. "Previous reports" never
loads, and older quarters' calls are not reachable. So a past quarter that is
not the latest is a "No concall found". So is a quarter whose results are not
out yet, and so is a quarter whose results are out but whose transcript has not
been posted (Google adds it some hours after the call). **Do not fall back** to
screener, BSE, news, or Google's own AI summary. Say instead, in one line, that
a transcript PDF can be supplied (Step 3b).

`google_label` uses the INDIAN fiscal year — `Fiscal Q2 2027` is the
Jul–Sep 2026 quarter, i.e. Q2 FY27 (verified on TCS 08-Oct-2026, Tata Steel
and APL Apollo 31-Jul / 03-Aug-2026). If a page ever labels a calendar-year
quarter instead, the quarter check is the guard; do not "fix" the mapping on
one page.

## Step 3a — read the WHOLE transcript

Pull it in 20,000-character chunks until done:

```js
window.__cc.slice(0, 20000)        // then 20000-40000, 40000-60000, ...
```

**Completeness check before writing anything:** the last chunk must end with the
operator's close ("…concludes this conference call… disconnect"), and
`last_stamp` should be ~45–90 minutes for a normal call. If the text stops
mid-Q&A, scroll the page to the bottom, wait, and re-run the Step 2 snippet.
If it is still short, that is "No concall found — transcript on Google Finance
is incomplete (<last_stamp>)". A note written off half a call grades
commitments it never heard.

**It is a machine transcript of the AUDIO, not the company's filed
transcript.** Known artefacts (all met on TCS Q2 FY27):
- every operator line is labelled `[Operator Instructions]`; speakers are
  attributed automatically and can be wrong on cross-talk;
- `[inaudible]` gaps, and an analyst's sentence cut off when management talks over them;
- misheard numbers are possible. **If a number matters and looks wrong, flag it
  in the note. Never correct it.** The same rule applies to a figure that
  contradicts itself inside the call: on TCS Q2 FY27, "India revenue grew 6%"
  in the prepared remarks sat against an analyst's "India revenue decline by
  about 10%". That pair goes in Red flags as unreconciled.

## Step 3b — the PM supplied a PDF / text instead

Extract it as the vault instructions say (`pdftotext -layout`, else
pdfplumber). A scanned PDF is refused, never OCR'd. Skip Step 2 entirely.

## Step 4 — write the note: the vault's method, unchanged

Read and follow, in full:

```
C:\Users\rajvaibhav.yadav\OneDrive - PinPOINT\Obsidian Vault\SKILLS\EARNINGS CALL TRANSCRIPT ANALYST.md
```

That is: load the prior note (`prior_path`, if `prior_exists`). Its **New
commitments** are what you grade in **Did they deliver?**. Anchor sentiment on
consensus BEFORE writing (Step 2.5 there), using the sector-relative
thresholds. Then write the exact structure: header → ⚡ Bottom line → 📊 Did
they deliver? → 🎯 New commitments → 🚩 Red flags → ⚠️ Top risks → 🔭 Watch
next quarter → 🗂️ Q&A appendix. With `quick`, write only Bottom line + Did they
deliver? + Watch next quarter.

Consensus: Google's `est` lines (EPS and revenue vs estimate) are a usable
anchor. Cite them as **"Google Finance consensus"** with the numbers. They do
not cover EBITDA/margin, so search for that per the vault instructions, or say
it was not found.

**One addition to the vault header, on its own line after `**Call:**`.** It
does not touch any line a parser reads:

```
**Source:** Google Finance call transcript (machine-generated from the call audio), read <DD Mon YYYY> — not the company's filed transcript
```

For a PDF, name the file instead.

`vault_concall.py` reads the **Did they deliver?** table by verdict WORD
(Delivered / Partial / Missed / Not yet due), so every verdict cell must carry
one of those words beside its emoji.

## Step 5 — save, show, propagate

- Write the note to `note_path`, creating the `<TICKER>_Concall` folder if it
  does not exist. UTF-8, Write tool.
- Present the full note in chat (the vault rule). The desk page needs no
  restart: the Results tab's **Concall notes** sub-tab reads the folder on each
  request, so the note appears there as the newest quarter.
- Re-export the vault copy in the background:
  `python packages/web/export_static.py`. It takes ~90s and is never fatal.
  This is the daily_review rule: a skill that writes something the page reads,
  outside `refresh.py`, owes the vault copy an export.
- **Nothing here writes to `ims.db`.** Turning the note's commitments into P4
  `guidance` rows is `/concall-ingest`'s job. Offer it in one line for a scored
  name; never run it unasked.
