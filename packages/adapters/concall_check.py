"""Is the Hindalco/Novelis base quarter still the latest one? Advisory only.

    python packages/adapters/concall_check.py           # compare and report
    python packages/adapters/concall_check.py --json

THE CONTRACT THIS ENFORCES, set by the PM on 2026-08-24:

    Hindalco's and Novelis' base numbers are STATIC. They change once a quarter,
    when the company reports, and they are taken from the public release.

So nothing here writes. It fetches the investor page, works out the newest
quarter published, compares that to `base_quarter` in base_financials.yaml, and
either says "still current" or prints exactly what to change. Editing the spec
stays a human action, because CLAUDE.md's list of things that must never be
automated has extraction at the top of it: a wrong extraction enters the store as
a fact, and a base_ebitda is the beta for every signal that name produces.

WHY THIS IS NOT WebFetch. The web tools route through the auxiliary model and are
agent-only, so a Python step cannot use them and an unattended refresh could not
run this. hindalco.com and investors.novelis.com both answer plain urllib with
HTTP 200 given a browser User-Agent (probed 2026-08-24: 110 KB, 128 KB, 48 KB),
so this is an ordinary adapter with no model in the loop.

WHAT IT DOES NOT EXTRACT, deliberately. Segment EBITDA lines are uniquely
labelled ("Upstream EBITDA at Rs7,390 crore") and safe to pull. SHIPMENTS ARE
NOT: the string "Shipments Kt" appears in the Aluminium, Copper and Novelis
tables, and a naive match returns 963 — Novelis' prior-year comparative — where
the wanted number is 335. So volumes are flagged for a human read rather than
scraped into a plausible wrong figure. That asymmetry is the whole point: pull
what is unambiguous, name what is not.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SPEC = REPO / "specs" / "entities" / "base_financials.yaml"
LISTING = "https://www.hindalco.com/media/press-releases"
PAGE = "https://www.hindalco.com/media/press-releases/{slug}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Uniquely-labelled figures only. Each pattern must match ONE thing on the page.
FIELDS = {
    "consolidated_ebitda": r"Consolidated EBITDA[^.]{0,80}?([\d,]{4,})\s*crore",
    "upstream_ebitda":     r"Upstream EBITDA at [^\d]{0,12}([\d,]{3,})\s*crore",
    "downstream_ebitda":   r"Downstream EBITDA at [^\d]{0,12}([\d,]{3,})\s*crore",
    "novelis_ebitda":      r"Novelis quarterly Adjusted EBITDA[^\d]{0,20}"
                           r"([\d,]{3,})\s*crore",
}

# Which spec value each figure feeds, x4 for the annualising convention.
FEEDS = {
    "upstream_ebitda": ("hindalco", "base_ebitda"),
    "consolidated_ebitda": ("hindalco", "valuation_ebitda"),
    "novelis_ebitda": ("novelis", "base_ebitda"),
}


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def plain(html: str) -> str:
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"&#8377;|&nbsp;|&amp;|&rsquo;|&nbsp", " ", t)
    return re.sub(r"\s+", " ", t)


def qkey(slug: str) -> tuple[int, int] | None:
    """('...q1fy27' | '...q4-fy26') -> (fy, q), sortable. None if not a result page."""
    m = re.search(r"q([1-4])-?fy(\d{2})", slug)
    if not m:
        return None
    return (int(m.group(2)), int(m.group(1)))


def spec_state() -> dict:
    import yaml
    doc = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    bq = doc.get("base_quarter") or {}
    return {"label": bq.get("label"), "end": str(bq.get("end")),
            "values": {n: {k: (doc["companies"].get(n) or {}).get(k)
                           for k in ("base_ebitda", "valuation_ebitda")}
                       for n in ("hindalco", "novelis")}}


def check() -> dict:
    spec = spec_state()
    out: dict = {"spec_base_quarter": spec["label"], "spec": spec["values"]}

    try:
        slugs = sorted(set(re.findall(
            r"/media/press-releases/([a-z0-9\-]*result[a-z0-9\-]*)",
            get(LISTING))))
    except Exception as exc:
        out["error"] = f"listing unreachable: {type(exc).__name__}: {exc}"
        return out

    ranked = sorted(((qkey(s), s) for s in slugs if qkey(s)), reverse=True)
    if not ranked:
        out["error"] = f"no result pages found among {slugs}"
        return out
    out["pages_found"] = [s for _, s in ranked]
    newest_key, newest = ranked[0]
    out["latest_published"] = f"{newest_key[1]}QFY{newest_key[0]}"
    out["latest_slug"] = newest

    spec_key = qkey((spec["label"] or "").lower().replace("q", "q")) or None
    m = re.match(r"(\d)QFY(\d{2})", (spec["label"] or "").upper())
    spec_key = (int(m.group(2)), int(m.group(1))) if m else None
    out["current"] = (spec_key == newest_key)

    try:
        txt = plain(get(PAGE.format(slug=newest)))
    except Exception as exc:
        out["error"] = f"{newest} unreachable: {type(exc).__name__}: {exc}"
        return out

    got = {}
    for k, pat in FIELDS.items():
        mm = re.search(pat, txt, re.I)
        got[k] = int(mm.group(1).replace(",", "")) if mm else None
    out["published"] = got
    # x4: base_quarter annualising convention, documented in base_financials.yaml
    out["implied_annualised"] = {
        f"{ent}.{field}": (got[k] * 4 if got.get(k) else None)
        for k, (ent, field) in FEEDS.items()}
    out["needs_manual"] = ["hindalco aluminium_ingot volume (upstream shipments)",
                           "novelis flat_rolled_products volume (shipments)"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = check()
    if a.json:
        print(json.dumps(r, indent=2))
        return 0

    if r.get("error"):
        # Not a failure of the pipeline. The base numbers are static by design,
        # so an unreachable investor site changes nothing today.
        print(f"concall check: {r['error']}")
        print("  base numbers are static and unaffected; retry another day")
        return 0

    print(f"spec base_quarter   {r['spec_base_quarter']}")
    print(f"latest published    {r['latest_published']}  ({r['latest_slug']})")
    if r["current"]:
        print("\nstill current — Hindalco/Novelis base numbers need no change.")
        return 0

    print(f"\n*** A NEWER QUARTER IS PUBLISHED. The spec is one or more quarters "
          f"behind. ***\n")
    print("published on the page (Rs crore, quarterly):")
    for k, v in (r["published"] or {}).items():
        print(f"   {k:22}{'—' if v is None else format(v, ',')}")
    print("\nwould become (x4, annualised per the base_quarter convention):")
    for k, v in (r["implied_annualised"] or {}).items():
        cur = None
        ent, field = k.split(".")
        cur = (r["spec"].get(ent) or {}).get(field)
        arrow = "" if v is None or cur is None else \
            f"   (now {cur:,}{'' if v == cur else f' -> {v:,}'})"
        print(f"   {k:28}{'—' if v is None else format(v, ',')}{arrow}")
    print("\nNOT scraped, read these off the release yourself:")
    for x in r["needs_manual"]:
        print(f"   {x}")
    print("\nNOTHING WAS WRITTEN. Edit specs/entities/base_financials.yaml —")
    print("  base_quarter.label/start/end AND the base_ebitda values together,")
    print("  in one edit, with the verbatim quote in *_source. Then re-run")
    print("  packages/score/run_scores.py. Leaving it stale is SAFE; splitting")
    print("  the quarter from the values is what breaks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
