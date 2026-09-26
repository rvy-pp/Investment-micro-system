"""Build a per-company mention panel from the two corpora that exist on disk.

Corpus A: vault Broker Mails/*.md       2026-06-17 -> 2026-08-18, hashtag-tagged
Corpus B: IMS data/staging/mail_*.json  2026-08-24 -> 2026-09-19, explicit hits+zeros

The two are NOT spliced on raw counts (different measurement units: A counts
tagged paragraphs in a curated digest, B counts matched emails). Share of voice
is scale-free and is the only metric compared across them.
"""
import json, glob, os, re, csv
from collections import defaultdict

VAULT = os.path.expanduser("~/OneDrive - PinPOINT/Obsidian Vault")
IMS = os.path.expanduser("~/Investment-micro-system")
OUT = os.path.dirname(os.path.abspath(__file__))

TAG2ENT = {
    "tatasteel": "tata_steel", "jswsteel": "jsw_steel", "jindalsteel": "jindal_steel",
    "jspl": "jindal_steel", "sail": "sail", "aplapollo": "apl_apollo",
    "jindalstainless": "jindal_stainless", "shyammetalics": "shyam_metalics",
    "lloydsmetals": "lloyds_metals", "nmdc": "nmdc", "coalindia": "coal_india",
    "hindustancopper": "hindustan_copper", "hindalco": "hindalco", "nalco": "nalco",
    "vedanta": "vedanta", "vedantaltd": "vedanta", "vaml": "vaml",
    "vedantaaluminium": "vaml", "hz": "hindustan_zinc", "hindustanzinc": "hindustan_zinc",
    "ultratech": "ultratech", "ambuja": "ambuja", "dalmia": "dalmia", "shree": "shree",
    "shreecement": "shree", "jkcement": "jk_cement", "ramco": "ramco",
    "ramcocement": "ramco", "nuvoco": "nuvoco", "starcement": "star_cement",
    "jswcement": "jsw_cement", "acc": "acc", "dixon": "dixon", "amber": "amber",
    "kaynes": "kaynes", "syrmasgs": "syrma_sgs", "avalon": "avalon",
    "pgel": "pg_electroplast", "tcs": "tcs", "infosys": "infosys", "hcltech": "hcl_tech",
    "wipro": "wipro", "techm": "tech_mahindra", "techmahindra": "tech_mahindra",
    "ltm": "ltimindtree", "ltimindtree": "ltimindtree", "ltts": "ltts",
    "coforge": "coforge", "persistent": "persistent", "mphasis": "mphasis",
    "kpit": "kpit", "tataelxsi": "tata_elxsi", "ofss": "ofss",
}
SECTOR_TAGS = {"steel", "mining", "aluminium", "cement", "ems", "it", "other"}

ENT_SECTOR = {
    "tata_steel": "steel", "jsw_steel": "steel", "jindal_steel": "steel", "sail": "steel",
    "apl_apollo": "steel", "jindal_stainless": "steel", "shyam_metalics": "steel",
    "lloyds_metals": "mining", "nmdc": "mining", "coal_india": "mining",
    "hindustan_copper": "mining", "hindalco": "aluminium", "nalco": "aluminium",
    "vedanta": "aluminium", "vaml": "aluminium", "hindustan_zinc": "aluminium",
    "ultratech": "cement", "ambuja": "cement", "dalmia": "cement", "shree": "cement",
    "jk_cement": "cement", "ramco": "cement", "nuvoco": "cement", "star_cement": "cement",
    "jsw_cement": "cement", "acc": "cement", "dixon": "ems", "amber": "ems",
    "kaynes": "ems", "syrma_sgs": "ems", "avalon": "ems", "pg_electroplast": "ems",
    "tcs": "it", "infosys": "it", "hcl_tech": "it", "wipro": "it",
    "tech_mahindra": "it", "ltimindtree": "it", "ltts": "it", "coforge": "it",
    "persistent": "it", "mphasis": "it", "kpit": "it", "tata_elxsi": "it", "ofss": "it",
}

DATEFILE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})\.md$")
TAGRE = re.compile(r"#([A-Za-z][A-Za-z0-9&]*)")

# ---------- corpus A: vault digests ----------
dayA, unmapped = {}, defaultdict(int)
for p in sorted(glob.glob(os.path.join(VAULT, "Broker Mails", "*.md"))):
    m = DATEFILE.match(os.path.basename(p))
    if not m:
        continue
    d, mo, y = m.groups()
    txt = open(p, encoding="utf-8", errors="replace").read()
    counts = defaultdict(int)
    for tag in TAGRE.findall(txt):
        t = tag.lower()
        if t in SECTOR_TAGS:
            continue
        ent = TAG2ENT.get(t)
        if ent:
            counts[ent] += 1
        else:
            unmapped[tag] += 1
    dayA["%s-%s-%s" % (y, mo, d)] = dict(counts)

# ---------- corpus B: IMS staging ----------
# Three formats across the 16 days:
#   Aug24-Sep15  bare list of mails, NO entity_id  -> attribute by term match
#   Sep16-Sep18  list of mails WITH entity_id
#   Sep19        dict with hits[] + no_hit_entities[]
# Term list mirrors mail_watch.py --terms (derived from specs/entities/*.yaml).
ENT_TERMS = {
    "amber": ["amber enterprises", "amber"], "ambuja": ["ambujacem", "ambuja"],
    "apl_apollo": ["apl apollo", "aplapollo"], "coal_india": ["coalindia", "coal india"],
    "dalmia": ["dalbharat", "dalmia"], "dixon": ["dixon"],
    "hindalco": ["hindalco", "novelis"], "hindustan_copper": ["hindcopper", "hindustan copper"],
    "hindustan_zinc": ["hindzinc", "hindustan zinc"],
    "jindal_steel": ["jindalstel", "jindal steel", "jspl"],
    "jsw_steel": ["jsw steel", "jswsteel"], "kaynes": ["kaynes"],
    "nalco": ["nalco", "nationalum", "national aluminium"], "nmdc": ["nmdc"],
    "pg_electroplast": ["pg electroplast", "pgel"],
    "sail": ["sail", "steel authority"], "shree": ["shreecem", "shree cement"],
    "tata_steel": ["tatasteel", "tata steel"],
    "ultratech": ["ultracemco", "ultratech"],
    "vaml": ["vaml", "vedanta alum"], "vedanta": ["vedl", "vedanta"],
}
BUILTIN_UNIVERSE = sorted(ENT_TERMS)


def attribute(mail):
    """entity_ids named by a mail, from its own field or by term match."""
    if mail.get("entity_id"):
        return [mail["entity_id"]]
    blob = ((mail.get("subject") or "") + " " + (mail.get("summary") or "")).lower()
    hits = []
    for ent, terms in ENT_TERMS.items():
        if any(t in blob for t in terms):
            hits.append(ent)
    # 'vedanta' term also fires on VAML headlines; keep the more specific one only
    if "vaml" in hits and "vedanta" in hits:
        hits.remove("vedanta")
    return hits


dayB = {}
for p in sorted(glob.glob(os.path.join(IMS, "data", "staging", "mail_*.json"))):
    d = json.load(open(p, encoding="utf-8"))
    mails = d.get("hits", []) if isinstance(d, dict) else d
    counts = defaultdict(int)
    for e in (d.get("no_hit_entities", []) if isinstance(d, dict) else []):
        counts.setdefault(e, 0)
    for e in BUILTIN_UNIVERSE:
        counts.setdefault(e, 0)
    for mail in mails:
        for ent in attribute(mail):
            counts[ent] += 1
    dayB[os.path.basename(p)[5:15]] = dict(counts)

# corpus A records only tags that appeared; add explicit zeros over its universe
universeA = sorted({e for c in dayA.values() for e in c})
for counts in dayA.values():
    for e in universeA:
        counts.setdefault(e, 0)


def to_long(day, corpus):
    out = []
    for date, counts in sorted(day.items()):
        tot = sum(counts.values())
        for ent, n in counts.items():
            out.append(dict(date=date, entity_id=ent, sector=ENT_SECTOR.get(ent, "?"),
                            mentions=n, day_total=tot, corpus=corpus))
    return out


longA, longB = to_long(dayA, "digest"), to_long(dayB, "staging")
panel = longA + longB

sect_tot = defaultdict(int)
for r in panel:
    sect_tot[(r["corpus"], r["date"], r["sector"])] += r["mentions"]
for r in panel:
    r["sov"] = r["mentions"] / r["day_total"] if r["day_total"] else 0.0
    st = sect_tot[(r["corpus"], r["date"], r["sector"])]
    r["sov_sector"] = r["mentions"] / st if st else 0.0

with open(os.path.join(OUT, "mention_panel.csv"), "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["corpus", "date", "entity_id", "sector",
                                      "mentions", "day_total", "sov", "sov_sector"])
    w.writeheader()
    w.writerows(panel)

print("corpus A (vault digests): %d days  %s -> %s   entities=%d"
      % (len(dayA), min(dayA), max(dayA), len(universeA)))
print("corpus B (IMS staging)  : %d days  %s -> %s   entities=%d"
      % (len(dayB), min(dayB), max(dayB), len({e for c in dayB.values() for e in c})))
print("panel rows: %d  -> mention_panel.csv" % len(panel))

tot = defaultdict(int)
for r in longA:
    tot[r["entity_id"]] += r["mentions"]
print("\ncorpus A mentions per company (top 20 of %d):" % len(tot))
for e, n in sorted(tot.items(), key=lambda x: -x[1])[:20]:
    print("  %-18s %4d" % (e, n))

print("\nmentions/day distribution, corpus A:")
vals = sorted(r["mentions"] for r in longA)
n = len(vals)
print("  zeros %.0f%%   median %d   p90 %d   p99 %d   max %d"
      % (100.0 * sum(1 for v in vals if v == 0) / n, vals[n // 2],
         vals[int(n * 0.9)], vals[int(n * 0.99)], vals[-1]))

print("\nunmapped tags (sanity):", dict(unmapped) if unmapped else "(none)")
