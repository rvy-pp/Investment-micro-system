"""Valuation multiple through time, decomposed."""
import json, pathlib, statistics, html

HERE = pathlib.Path(__file__).resolve().parent
D = json.load(open(HERE / "_val.json"))
BQ, B = D["base_quarter"], D["books"]
CO = {"nalco": "NALCO", "hindalco": "Hindalco", "vaml": "VAML",
      "hindustan_zinc": "Hindustan Zinc", "vedanta": "Vedanta"}
esc = html.escape


def chart(eid, W=1000, H=310, show_mean=True):
    r = B[eid]["rows"]
    m = [x["mult"] for x in r]
    PL, PR, PT, PB = 46, 16, 16, 30
    lo, hi = min(m) * 0.95, max(m) * 1.05
    X = lambda i: PL + (W - PL - PR) * i / max(len(r) - 1, 1)
    Y = lambda v: PT + (H - PT - PB) * (1 - (v - lo) / (hi - lo))
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(m))
    ticks = [lo + (hi - lo) * i / 4 for i in range(5)]
    grid = "".join(
        f'<line class="gridline" x1="{PL}" y1="{Y(t):.1f}" x2="{W-PR}" y2="{Y(t):.1f}"/>'
        f'<text class="axis" x="{PL-7}" y="{Y(t)+3.5:.1f}" text-anchor="end">'
        f'{t:.1f}x</text>' for t in ticks)
    mean = statistics.fmean(m)
    meanline = (f'<line x1="{PL}" y1="{Y(mean):.1f}" x2="{W-PR}" y2="{Y(mean):.1f}" '
                f'stroke="var(--zinc)" stroke-width="1" stroke-dasharray="5 4"/>'
                f'<text class="axis" x="{W-PR-4}" y="{Y(mean)-6:.1f}" '
                f'text-anchor="end">mean {mean:.2f}x</text>') if show_mean else ""
    yl, seen = "", set()
    for i, x in enumerate(r):
        if x["d"][:4] not in seen:
            seen.add(x["d"][:4])
            yl += (f'<text class="axis" x="{X(i):.1f}" y="{H-8}" '
                   f'text-anchor="middle">{x["d"][:4]}</text>')
    last = (f'<circle cx="{X(len(m)-1):.1f}" cy="{Y(m[-1]):.1f}" r="4" '
            f'fill="var(--accent)"/>'
            f'<text class="axis" x="{X(len(m)-1)-8:.1f}" y="{Y(m[-1])-9:.1f}" '
            f'text-anchor="end" fill="var(--accent)">{m[-1]:.2f}x now</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(CO[eid])} spot '
            f'EV/EBITDA through time">{grid}{meanline}'
            f'<polyline points="{pts}" fill="none" stroke="var(--ink)" '
            f'stroke-width="1.6"/>{last}{yl}</svg>')


def decomp(eid, W=1000, H=250):
    """EV and spot EBITDA, both indexed to 100 — which leg moved the multiple."""
    r = B[eid]["rows"]
    ev0, sp0 = r[0]["ev"], r[0]["spot"]
    ev = [x["ev"] / ev0 * 100 for x in r]
    sp = [x["spot"] / sp0 * 100 for x in r]
    PL, PR, PT, PB = 46, 16, 16, 30
    lo, hi = min(min(ev), min(sp)) * 0.95, max(max(ev), max(sp)) * 1.05
    X = lambda i: PL + (W - PL - PR) * i / max(len(r) - 1, 1)
    Y = lambda v: PT + (H - PT - PB) * (1 - (v - lo) / (hi - lo))
    grid = "".join(
        f'<line class="gridline" x1="{PL}" y1="{Y(t):.1f}" x2="{W-PR}" y2="{Y(t):.1f}"/>'
        f'<text class="axis" x="{PL-7}" y="{Y(t)+3.5:.1f}" text-anchor="end">'
        f'{t:.0f}</text>' for t in (lo + (hi - lo) * i / 4 for i in range(5)))
    pe = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(ev))
    ps = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(sp))
    base = (f'<line x1="{PL}" y1="{Y(100):.1f}" x2="{W-PR}" y2="{Y(100):.1f}" '
            f'stroke="var(--rule)" stroke-width="1"/>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="EV vs spot EBITDA, '
            f'indexed">{grid}{base}'
            f'<polyline points="{pe}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>'
            f'<polyline points="{ps}" fill="none" stroke="var(--win)" stroke-width="1.6"/>'
            f'</svg>')


n = B["nalco"]["rows"]
nm = [x["mult"] for x in n]
mean_n = statistics.fmean(nm)
others = ""
for eid in ("hindalco", "hindustan_zinc", "vedanta", "vaml"):
    r = B[eid]["rows"]
    m = [x["mult"] for x in r]
    cut = B[eid].get("cut")
    note = (f"history starts {r[0]['d']}" + (f" — cut at a "
            f"{B[eid]['cut_pct']:+.0f}% step change" if cut else ""))
    others += f"""<div class="ocard">
      <div class="ohead"><strong>{esc(CO[eid])}</strong>
        <span class="mono onow">{m[-1]:.2f}x</span></div>
      <div class="orow"><span>range</span><span class="mono">{min(m):.2f} &ndash; {max(m):.2f}x</span></div>
      <div class="orow"><span>mean</span><span class="mono">{statistics.fmean(m):.2f}x</span></div>
      <div class="orow"><span>days</span><span class="mono">{len(m)}</span></div>
      <div class="onote">{esc(note)}</div></div>"""

head = open(HERE / "_val_head.html", encoding="utf-8").read()
body = f"""
<div class="wrap">
<header class="sec">
  <div class="eyebrow">P3 valuation &middot; spot EV/EBITDA &middot; base quarter
    {esc(BQ['label'])} {esc(str(BQ['start']))}&ndash;{esc(str(BQ['end']))}</div>
  <h1>NALCO has re-rated, not cheapened</h1>
  <p class="lede">The multiple is enterprise value over EBITDA re-marked to each day&#8217;s
  commodity prices &mdash; so it answers &ldquo;at today&#8217;s prices, what am I paying?&rdquo;
  rather than quoting a quarter that is already over.</p>
</header>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">NALCO</div>
  <h2>Spot EV/EBITDA, {n[0]['d']} to {n[-1]['d']}</h2>
  {chart('nalco')}
  <div class="grid4">
    <div class="stat"><span class="k">Now</span><span class="v mono">{nm[-1]:.2f}x</span>
      <span class="s">vs {mean_n:.2f}x mean</span></div>
    <div class="stat"><span class="k">Cheapest</span><span class="v mono">{min(nm):.2f}x</span>
      <span class="s">{esc([x['d'] for x in n if x['mult']==min(nm)][0])}</span></div>
    <div class="stat"><span class="k">Dearest</span><span class="v mono">{max(nm):.2f}x</span>
      <span class="s">{esc([x['d'] for x in n if x['mult']==max(nm)][0])}</span></div>
    <div class="stat"><span class="k">vs mean</span>
      <span class="v mono">{(nm[-1]/mean_n-1)*100:+.0f}%</span>
      <span class="s">dearer than usual</span></div>
  </div>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">Decomposition</div>
  <h2>The multiple rose because the price rose, not because earnings fell</h2>
  <div class="legend"><span class="lg"><i style="background:var(--accent)"></i>enterprise
    value</span><span class="lg"><i style="background:var(--win)"></i>spot EBITDA</span>
    <span class="lg">both indexed to 100 at the start</span></div>
  {decomp('nalco')}
  <div class="grid2">
    <div class="stat"><span class="k">Price</span>
      <span class="v mono">{n[0]['price']:.0f} &rarr; {n[-1]['price']:.0f}</span>
      <span class="s">{(n[-1]['price']/n[0]['price']-1)*100:+.0f}%</span></div>
    <div class="stat"><span class="k">Spot EBITDA</span>
      <span class="v mono">{n[0]['spot']:,.0f} &rarr; {n[-1]['spot']:,.0f}</span>
      <span class="s">{(n[-1]['spot']/n[0]['spot']-1)*100:+.0f}% &mdash; barely moved</span></div>
  </div>
  <div class="callout">
    <p>A multiple can rise two ways: the price goes up, or the earnings it is measured against go
    down. Here it is almost entirely the first. NALCO earns about the same at today&#8217;s
    commodity prices as it did two years ago, and costs more than twice as much to buy. At
    <strong>{nm[-1]:.2f}x against a {mean_n:.2f}x mean</strong> it is
    {(nm[-1]/mean_n-1)*100:+.0f}% dearer than its own history &mdash; which is what the P3 score
    of 1.69 is saying.</p>
  </div>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">Inputs, verified</div>
  <h2>Every NALCO input is sourced &mdash; with one stated bias</h2>
  <div class="scroll"><table class="t">
    <thead><tr><th>Input</th><th>Value</th><th>Source</th></tr></thead>
    <tbody>
      <tr><td>shares outstanding</td><td class="mono">1.837 bn</td>
        <td>Wind <span class="mono">total_shares</span> &mdash; matches the quoted 1.836bn</td></tr>
      <tr><td>net debt</td><td class="mono">&minus;473 cr</td>
        <td>screener.in &mdash; net <em>cash</em>, so EV sits just below market cap</td></tr>
      <tr><td>base EBITDA</td><td class="mono">10,840 cr</td>
        <td>1QFY27 standalone &#8377;27.1bn &times; 4 (Kotak, digest 2026-08-05)</td></tr>
      <tr><td>market cap</td><td class="mono">71,482 cr</td>
        <td>computed at &#8377;389.20</td></tr>
    </tbody></table></div>
  <div class="callout">
    <p><strong>The stated bias, and it runs the cheap way.</strong> That 1QFY27 print was
    <strong>+81% year on year</strong> &mdash; a peak-cycle quarter. Annualising it overstates a
    mid-cycle base, which understates the multiple. So NALCO&#8217;s true multiple is
    <em>higher</em> than 7.15x, and it is already dear. The bias does not rescue it.</p>
    <p>Note this is the opposite direction to Vedanta, whose pre-demerger net debt inflates EV and
    makes it read <em>dearer</em> than it is. Two names, two biases, opposite signs &mdash; which is
    why each carries its own note rather than a blanket confidence level.</p>
  </div>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">The rest of the book</div>
  <h2>Where each name sits against its own history</h2>
  <div class="ogrid">{others}</div>
  <p class="note">Histories are cut at the last step change of 15% or more &mdash; a multiple
  compared across a demerger compares two different companies. That is why Vedanta has 79 days
  and VAML 47: both are genuinely new entities, not gaps in the data.</p>
</section>
</div>
"""
(HERE / "valuation_time.html").write_text(head + body, encoding="utf-8")
print("written", len(head + body))
