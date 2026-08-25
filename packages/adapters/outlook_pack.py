"""L0 fetch — today's Daily Metals Pack out of Outlook, as real .xlsx bytes.

    python packages/adapters/outlook_pack.py            # probe: what would it take
    python packages/adapters/outlook_pack.py --save     # write data/staging/

WHY THIS EXISTS, AND WHY IT IS NOT THE M365 MCP. The pack was an agent-only step
for a year because the Microsoft 365 connector is agent-callable and Python is
not. That was the wrong constraint to design around. The connector is a TEXT
service: it hands back an attachment CONVERTED to text, capped at ~200k
characters, and it keeps the OLDEST rows. So a connector read of a 4,700-row
workbook returns 2010-2013 and never today, at any hour, awake or asleep, and
`startPage` is documented as applying to file:/// reads only — verified in the
tool schema, not inferred. The skill told you to read the tail of the extract
because today's prices are there; they are at the tail of the FILE, not the tail
of the extract. Those are different things and the difference is thirteen years.

Outlook itself has the bytes. Classic Outlook 16 is installed with a live MAPI
profile, so this shells to PowerShell, saves the attachment, and hands the path
to metals_pack.py's openpyxl reader — the one that was always there for a
hand-dropped file. Result on 2026-08-24: 1,934,986 bytes, 4,728 rows, span
ending the same day, ~0 tokens because no model ever sees the grid.

TWO THINGS THE OLD SKILL GOT WRONG, both of which failed SILENTLY:

  sender     it searched sumangal.nevatia@kotak.com. The pack comes from
             Samriddhi.Choudhury@kotak.com. Sumangal Nevatia is a real Kotak
             metals analyst who sends other notes, which is presumably how the
             address got in there. A search on the wrong address returns zero
             hits, and the documented response to zero hits was "say the pack
             has not arrived and stop" — so a wrong constant read as a quiet
             morning. THIS MATCHES ON NO SENDER AT ALL, by instruction.

  filename   it said the cement pack is distinguished by a stray space before
             the comma. Not stable: on 2026-08-18 BOTH files had it
             ("August 18 , 2026"); on 2026-08-24 metals had "August 24, 2026"
             and cement had "August 24 , 2026". Match the PREFIX.

EXIT CODES ARE A DISTINCTION, NOT A FORMALITY.
  0  saved, or no pack for today -> a quiet day, fallback sources supply
  1  Outlook unreachable -> a broken capability, worth a red step

A missing mail must not read as a broken fetcher, and a broken fetcher must not
read as a missing mail. That is the same silent-gap shape this repo keeps
finding, in operational form.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
STAGE = REPO / "data" / "staging"

SUBJECT_HINT = "asic materials"          # 'Basic materials - daily news and prices'
ATTACH_PREFIX = "Daily Metals Pack"      # NOT the cement pack; see docstring
LOOKBACK_DAYS = 6                        # a long weekend plus a holiday

# Index-based iteration, deliberately. Piping a Restrict() collection through
# foreach skipped today's message in testing while a folder walk found it — a
# known COM foible when the collection is sorted. $r.Item($i) is reliable.
PS = r"""
$ErrorActionPreference = 'Stop'
try   { $ol = New-Object -ComObject Outlook.Application }
catch { Write-Output 'ERR|outlook-unreachable|' + $_.Exception.Message; exit 0 }
$ns = $ol.GetNamespace('MAPI')
$since = (Get-Date).AddDays(-__DAYS__).ToString('MM/dd/yyyy 00:00')
$script:hits = @()
function Scan($folder) {
  try {
    $r = $folder.Items.Restrict("[ReceivedTime] >= '" + $since + "'")
    for ($i = 1; $i -le $r.Count; $i++) {
      try {
        $m = $r.Item($i)
        if ($m.Subject -notlike '*__SUBJ__*') { continue }
        for ($k = 1; $k -le $m.Attachments.Count; $k++) {
          $a = $m.Attachments.Item($k)
          if ($a.FileName -like '__PREFIX__*') {
            $script:hits += [pscustomobject]@{ M = $m; A = $a; T = $m.ReceivedTime }
          }
        }
      } catch {}
    }
  } catch {}
}
foreach ($store in $ns.Folders) {
  try { Scan $store.Folders.Item('Inbox') } catch {}
  try { foreach ($sub in $store.Folders) { Scan $sub } } catch {}
}
if ($script:hits.Count -eq 0) { Write-Output 'NONE|'; exit 0 }
$best = $script:hits | Sort-Object T -Descending | Select-Object -First 1
$stamp = $best.T.ToString('yyyy-MM-dd')
$dest  = Join-Path '__STAGE__' ('metals_pack_' + $stamp + '.xlsx')
if ('__SAVE__' -eq 'yes') {
  New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
  $best.A.SaveAsFile($dest)
  Write-Output ('SAVED|' + $stamp + '|' + $dest + '|' + (Get-Item $dest).Length + '|' + $best.A.FileName + '|' + $best.M.SenderEmailAddress)
} else {
  Write-Output ('FOUND|' + $stamp + '|' + $dest + '|' + [int]$best.A.Size + '|' + $best.A.FileName + '|' + $best.M.SenderEmailAddress)
}
"""


def run(save: bool) -> tuple[str, list[str]]:
    script = (PS.replace("__DAYS__", str(LOOKBACK_DAYS))
                .replace("__SUBJ__", SUBJECT_HINT)
                .replace("__PREFIX__", ATTACH_PREFIX)
                .replace("__STAGE__", str(STAGE))
                .replace("__SAVE__", "yes" if save else "no"))
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", script],
                       capture_output=True, text=True, timeout=180)
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if re.match(r"^(SAVED|FOUND|NONE|ERR)\|", line):
            parts = line.split("|")
            return parts[0], parts[1:]
    return "ERR", ["no-parseable-output", (p.stderr or p.stdout or "").strip()[:300]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true",
                    help="write the attachment to data/staging/")
    a = ap.parse_args()

    try:
        kind, rest = run(a.save)
    except subprocess.TimeoutExpired:
        print("Outlook COM timed out after 180s — treating as UNREACHABLE")
        return 1

    today = dt.date.today().isoformat()

    if kind == "ERR":
        print(f"Outlook UNREACHABLE: {' '.join(rest)}")
        print("This is a broken capability, not a quiet mail day. Price steps "
              "below will still run and lower-ranked sources will supply.")
        return 1

    if kind == "NONE":
        print(f"no Daily Metals Pack in the last {LOOKBACK_DAYS} days")
        print("Fallback sources (westmetall, Yahoo, FRED) supply what they can; "
              "anything they do not cover keeps its last stored price.")
        return 0

    stamp, dest, size, fname, sender = rest[0], rest[1], rest[2], rest[3], rest[4]
    verb = "saved" if kind == "SAVED" else "found (probe, not saved)"
    print(f"{verb}: {fname}")
    print(f"  from    {sender}")
    print(f"  dated   {stamp}" + ("" if stamp == today else "   <-- NOT TODAY"))
    print(f"  bytes   {int(size):,}")
    print(f"  path    {dest}")
    if stamp != today:
        print(f"\nNewest pack is {stamp}, not {today}. It still carries full "
              "history, so loading it tops up any day the store is missing — "
              "but it cannot contain a price for today.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
