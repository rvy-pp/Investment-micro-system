"""Render the P1 pillar explainer."""
import json, math, pathlib, html

HERE = pathlib.Path(__file__).resolve().parent
D = json.load(open(HERE / "_p1.json"))
esc = lambda x: html.escape(str(x))
RS = "&#8377;"
NICE = {"lme_aluminium": "LME aluminium", "alumina_index": "Alumina FOB",
        "thermal_coal_seaborne": "Thermal coal", "cp_coke": "Pet coke",
        "lme_zinc": "LME zinc", "silver": "Silver", "usdinr": "USDINR",
        "zinc_shfe": "Zinc (SHFE proxy)"}
nm = lambda s: NICE.get(s, str(s).replace("_", " "))
CO = {"nalco": "NALCO", "hindalco": "Hindalco", "vaml": "VAML",
      "hindustan_zinc": "Hindustan Zinc", "vedanta": "Vedanta"}
co = lambda s: CO.get(s, s)


def cls(v):
    return "pos" if v > 0 else "neg" if v < 0 else "zero"


# ---------- hill curve ----------
W, H, PL, PR, PT, PB = 940, 340, 56, 20, 20, 40
C = D["curve"]
xs = [c["x"] for c in C]
X = lambda v: PL + (W - PL - PR) * (v - xs[0]) / (xs[-1] - xs[0])
Y = lambda s: PT + (H - PT - PB) * (1 - (s - 1) / 4)
path = " ".join(f"{X(c['x']):.1f},{Y(c['score']):.1f}" for c in C)
ygrid = "".join(
    f'<line class="gridline" x1="{PL}" y1="{Y(s):.1f}" x2="{W-PR}" y2="{Y(s):.1f}"/>'
    f'<text class="axis" x="{PL-8}" y="{Y(s)+3.5:.1f}" text-anchor="end">{s:.0f}</text>'
    for s in (1, 2, 3, 4, 5))
xg = ""
for v in (-0.20, -0.10, -0.05, 0, 0.05, 0.10, 0.20):
    xg += (f'<line class="gridline" x1="{X(v):.1f}" y1="{PT}" x2="{X(v):.1f}" '
           f'y2="{H-PB}" opacity=".55"/>'
           f'<text class="axis" x="{X(v):.1f}" y="{H-PB+16}" text-anchor="middle">'
           f'{v*100:+.0f}%</text>')
anchor = (f'<circle cx="{X(0.05):.1f}" cy="{Y(4.0):.1f}" r="4.5" fill="var(--accent)"/>'
          f'<line x1="{X(0.05):.1f}" y1="{Y(4.0):.1f}" x2="{X(0.05):.1f}" y2="{H-PB}" '
          f'stroke="var(--accent)" stroke-width="1" stroke-dasharray="3 3"/>'
          f'<text class="axis" x="{X(0.05)+8:.1f}" y="{Y(4.0)-8:.1f}" '
          f'fill="var(--accent)">anchor: +5% of EBITDA reads 4.0</text>')
# where each company currently sits
dots = ""
for book in D["books"].values():
    for r in book:
        if r["pct"] is None:
            continue
        x, y = X(r["pct"] / 100), Y(r["score"])
        dots += (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="var(--ink)"/>'
                 f'<title>{esc(co(r["entity"]))} {r["pct"]:+.2f}% &rarr; {r["score"]:.2f}</title>')
curve_svg = (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="The scoring curve">'
             f'{ygrid}{xg}<polyline class="curve" points="{path}"/>{anchor}{dots}</svg>')

# ---------- worked example: NALCO lines ----------
nal = [r for r in D["books"]["aluminium"] if r["entity"] == "nalco"][0]
mx = max(abs(l["impact"]) for l in nal["lines"])
legs = ""
for l in nal["lines"]:
    w = abs(l["impact"]) / mx * 100
    side = "pos" if l["impact"] > 0 else "neg"
    mp = "" if l["market_pct"] is None else f'&times; market {l["market_pct"]:.0%}'
    legs += f"""<div class="line">
      <span class="tag {'l' if l['kind']=='output' else 's'}">{esc(l['kind'])}</span>
      <span class="lname">{esc(nm(l['item']))}<em>{esc(nm(l['driver']))} {mp}</em></span>
      <span class="lbar"><i class="{side}" style="width:{w:.1f}%"></i></span>
      <span class="num mono {side}">{l['impact']:+,.0f}</span></div>"""

# ---------- shocks ----------
shock_rows = "".join(
    f'<tr><td>{esc(nm(s["driver"]))}</td><td class="mono">{s["level"]:,.1f}</td>'
    f'<td class="mono {cls(s["delta"])}">{s["delta"]:+,.1f}</td>'
    f'<td class="mono {cls(s["pct"])}">{s["pct"]:+.2f}%</td></tr>'
    for s in D["shocks"] if s["pct"] is not None)

# ---------- books ----------
def book_block(title, rows, blurb):
    cards = ""
    for r in rows:
        sc = r["score"]
        pos = (sc - 1) / 4 * 100
        cards += f"""<div class="cocard">
          <div class="cohead"><strong>{esc(co(r['entity']))}</strong>
            <span class="mono score">{sc:.2f}</span></div>
          <div class="track"><i style="left:{pos:.1f}%"></i></div>
          <div class="corow"><span>&Delta;EBITDA</span>
            <span class="mono {cls(r['d_ebitda_cr'])}">{RS}{r['d_ebitda_cr']:+,.0f} cr</span></div>
          <div class="corow"><span>as % of base</span>
            <span class="mono {cls(r['pct'])}">{r['pct']:+.2f}%</span></div>
          <div class="corow"><span>base EBITDA</span>
            <span class="mono dim">{RS}{r['base_ebitda']:,.0f} cr</span></div>
          <div class="corow"><span>lines priced</span>
            <span class="mono dim">{esc(r['coverage'])}</span></div></div>"""
    return f"""<section class="sec"><div class="eyebrow">{esc(title)}</div>
      <p>{blurb}</p><div class="cogrid">{cards}</div></section>"""


# ---------- sensitivity matrix ----------
drivers = ["lme_aluminium", "alumina_index", "lme_zinc", "silver",
           "thermal_coal_seaborne", "cp_coke"]
allv = [abs(v) for row in D["sens"].values() for v in row.values() if v]
smax = max(allv) if allv else 1
head_cells = "".join(f"<th>{esc(nm(d))}</th>" for d in drivers)
srows = ""
for e, row in D["sens"].items():
    cells = ""
    for d in drivers:
        v = row.get(d, 0) or 0
        if abs(v) < 0.005:
            cells += '<td class="hm zero">&middot;</td>'
        else:
            op = min(abs(v) / smax, 1) * 0.85 + 0.1
            c = "var(--win)" if v > 0 else "var(--loss)"
            cells += (f'<td class="hm" style="background:color-mix(in srgb,{c} '
                      f'{op*100:.0f}%, transparent)">{v:+.2f}</td>')
    srows += f"<tr><td>{esc(co(e))}</td>{cells}</tr>"

# ---------- score history ----------
W2, H2, PL2 = 940, 260, 40
hist = D["hist"]
alld = sorted({d for v in hist.values() for d, _ in v})
di = {d: i for i, d in enumerate(alld)}
COLR = {"nalco": "#C2703A", "hindalco": "#3E7CA6", "vaml": "#8A6BA8",
        "hindustan_zinc": "#2F7D63", "vedanta": "#B5432A"}
X2 = lambda i: PL2 + (W2 - PL2 - 12) * i / max(len(alld) - 1, 1)
Y2 = lambda s: 16 + (H2 - 16 - 26) * (1 - (s - 1) / 4)
lines_svg = ""
for e, v in hist.items():
    if len(v) < 20:
        continue
    pts = " ".join(f"{X2(di[d]):.1f},{Y2(s):.1f}" for d, s in v if d in di)
    lines_svg += (f'<polyline points="{pts}" fill="none" stroke="{COLR.get(e,"#888")}" '
                  f'stroke-width="1.15" opacity=".9"/>')
g2 = "".join(f'<line class="gridline" x1="{PL2}" y1="{Y2(s):.1f}" x2="{W2-12}" '
             f'y2="{Y2(s):.1f}"/><text class="axis" x="{PL2-8}" y="{Y2(s)+3.5:.1f}" '
             f'text-anchor="end">{s}</text>' for s in (1, 2, 3, 4, 5))
yrl = ""
seen = set()
for d in alld:
    if d[:4] not in seen:
        seen.add(d[:4])
        yrl += (f'<text class="axis" x="{X2(di[d]):.1f}" y="{H2-8}" '
                f'text-anchor="middle">{d[:4]}</text>')
neut = f'<line x1="{PL2}" y1="{Y2(3):.1f}" x2="{W2-12}" y2="{Y2(3):.1f}" class="zero"/>'
hist_svg = (f'<svg viewBox="0 0 {W2} {H2}" role="img" aria-label="Economics score history">'
            f'{g2}{neut}{lines_svg}{yrl}</svg>')
key = " ".join(f'<span class="k"><i style="background:{COLR[e]}"></i>{esc(co(e))}</span>'
               for e in hist if len(hist[e]) >= 20)

head = open(HERE / "_p1_head.html", encoding="utf-8").read()
body = f"""
<div class="wrap">
<header class="sec">
  <div class="eyebrow">Pillar 1 &middot; the margin bridge &middot; as at {esc(D['as_of'])}</div>
  <h1>How a metal price becomes a score</h1>
  <p class="lede">P1 is the only pillar that measures the business itself. It takes what each
  company sells and consumes, applies the last 30 days of commodity moves, and converts the
  resulting change in profit into a number between 1 and 5. No judgement anywhere in the chain.</p>
</header>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">Step 1 &middot; the inputs</div>
  <h2>What moved in the last 30 days</h2>
  <p>Every company sees the <em>same</em> commodity moves. What differs is how much of each one
  it touches.</p>
  <div class="scroll"><table class="t">
    <thead><tr><th>Driver</th><th>Level</th><th>30-day move</th><th>%</th></tr></thead>
    <tbody>{shock_rows}</tbody></table></div>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">Step 2 &middot; the bridge</div>
  <h2>Each price move is multiplied by how much of it the company actually touches</h2>
  <p>Take NALCO. It has five lines &mdash; two things it sells, three it buys. Each line is
  <strong>tonnes &times; price move &times; the share bought at market</strong>. Add them up and
  you have the change in profit.</p>
  <div class="card">{legs}
    <div class="line total"><span class="tag r">Sum</span>
      <span class="lname">Change in EBITDA<em>revenue {RS}{nal['lines'][0]['impact']+nal['lines'][1]['impact']:+,.0f} cr, costs {sum(l['impact'] for l in nal['lines'] if l['kind']=='input'):+,.0f} cr</em></span>
      <span class="lbar"></span>
      <span class="num mono {cls(nal['d_ebitda_cr'])}">{nal['d_ebitda_cr']:+,.0f}</span></div>
  </div>
  <div class="callout">
    <p><strong>The field that does all the work is <span class="mono">market_pct</span>.</strong>
    NALCO&#8217;s alumina line shows <span class="mono">{RS}0</span> impact even though alumina moved
    {[s for s in D['shocks'] if s['driver']=='alumina_index'][0]['pct']:+.2f}% &mdash; because NALCO
    makes its own alumina, so <span class="mono">market_pct = 0</span>. The same print is
    <em>revenue</em> for NALCO through its surplus line, and a <em>cost</em> for VAML, which buys it.
    One number, opposite signs. That is the entire reason these stocks can be paired.</p>
  </div>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">Step 3 &middot; the scale</div>
  <h2>Divide by the company&#8217;s own profit, then bend the line</h2>
  <p>{RS}{nal['d_ebitda_cr']:+,.0f} cr means nothing on its own. Against NALCO&#8217;s
  {RS}{nal['base_ebitda']:,.0f} cr base it is <span class="mono">{nal['pct']:+.2f}%</span> &mdash;
  and that percentage goes through this curve.</p>
  {curve_svg}
  <div class="grid2">
    <div class="callout"><p><strong>Why bent and not straight?</strong> The curve is deliberately
    flat near zero, so a 0.2% wobble in profit barely moves the score &mdash; that is daily noise,
    not news. It is steepest between 2% and 8%, where decisions actually get made, and it never
    quite reaches 1 or 5, so two large moves stay distinguishable instead of both pinning at the
    top.</p></div>
    <div class="callout"><p><strong>It is calibrated by argument, not by a constant.</strong>
    You state &ldquo;a 5% hit to profit should read 4.0&rdquo; and the curve&#8217;s width solves
    itself. Nobody tunes a coefficient. Move the anchor and every score in history recomputes
    &mdash; which is why each stored score carries the spec version that produced it.</p></div>
  </div>
</section>

<hr class="rule"/>

{book_block("The aluminium book", D["books"]["aluminium"],
  "Three names, one set of prices. NALCO sells surplus alumina, VAML buys it, Hindalco is "
  "nearly self-sufficient &mdash; so the same alumina print pushes their scores apart.")}

{book_block("The silver and zinc book", D["books"]["silver / zinc"],
  "Hindustan Zinc and Vedanta share the same two revenue lines: refined zinc and silver as a "
  "byproduct. Vedanta is a holding company whose main asset is 63.4% of HZL, so its economics "
  "are HZL&#8217;s scaled down &mdash; which is why the two scores track each other closely.")}

<section class="sec">
  <div class="eyebrow">Exposure</div>
  <h2>What each name is actually sensitive to</h2>
  <p>Change one price by 10% and hold everything else still. The number is the resulting change in
  profit, as a percentage of that company&#8217;s own EBITDA.</p>
  <div class="scroll"><table class="t hmt">
    <thead><tr><th>Company</th>{head_cells}</tr></thead><tbody>{srows}</tbody></table></div>
  <p class="note">Read across for what a company is exposed to, down for who a price separates.
  <strong>LME aluminium moves all three aluminium names by 12&ndash;18% and therefore separates
  none of them.</strong> Alumina is the opposite: +3.81 for NALCO, +0.18 for Hindalco,
  &minus;0.91 for VAML. That single column is where the aluminium pair trade comes from.</p>
  <div class="callout">
    <p><strong>The blank LME zinc column is a live gap, not a design choice.</strong> Both zinc
    names still take their zinc price from <span class="mono">zinc_shfe</span> &mdash; a Chinese
    domestic contract used as a stand-in, with 119 days of history. Real LME zinc now has 4,722
    days loaded and nothing points at it yet. Until the spec is repointed, the zinc book is being
    scored off a proxy for its single most important price.</p>
  </div>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">History</div>
  <h2>Every score since 2021</h2>
  {hist_svg}
  <div class="keys">{key}</div>
  <p class="note">3.0 is the neutral line &mdash; nothing happening. The aluminium names sit close
  together for long stretches, which is exactly the problem: when NALCO and Hindalco score within
  0.10 of each other, ranking them is a coin toss. That happens on 43% of days.</p>
</section>
</div>
"""
(HERE / "p1_explained.html").write_text(head + body, encoding="utf-8")
print("written", len(head + body))
