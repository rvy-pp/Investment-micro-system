"""Score line against price line, per company."""
import json, math, pathlib, statistics, html

HERE = pathlib.Path(__file__).resolve().parent
L = json.load(open(HERE / "_lines.json"))
CO = {"nalco": "NALCO", "hindalco": "Hindalco", "hindustan_zinc": "Hindustan Zinc"}
FWD = {"nalco": [0.143, 0.163, 0.146, 0.121, 0.071, 0.053],
       "hindalco": [0.156, 0.146, 0.102, 0.072, 0.006, 0.001],
       "hindustan_zinc": [0.143, 0.084, 0.048, 0.022, 0.014, -0.018]}
TRL = {"nalco": [0.401, 0.279, 0.218, 0.230, 0.176],
       "hindalco": [0.500, 0.361, 0.239, 0.176, 0.126],
       "hindustan_zinc": [0.312, 0.232, 0.152, 0.115, 0.077]}
BUCK = {
    "nalco": [("below 2.75", 82, 8.20, 67), ("2.75 - 3.00", 47, 14.48, 81),
              ("3.00 - 3.25", 54, 15.08, 67), ("3.25 and up", 98, 11.36, 72)],
    "hindalco": [("below 2.75", 74, 3.97, 68), ("2.75 - 3.00", 64, 9.17, 83),
                 ("3.00 - 3.25", 55, 9.87, 76), ("3.25 and up", 88, 6.06, 68)],
    "hindustan_zinc": [("below 2.75", 13, 1.55, 46), ("2.75 - 3.00", 116, 3.88, 51),
                       ("3.00 - 3.25", 132, 7.65, 58), ("3.25 and up", 20, -1.87, 40)],
}
esc = html.escape


def chart(eid):
    sc = L[eid]["score"]
    pr = {w: v for w, v in L[eid]["price"]}
    weeks = [w for w, _ in sc if w in pr]
    s = [v for w, v in sc if w in pr]
    p = [pr[w] for w in weeks]
    W, H, PL, PR, PT, PB = 1000, 300, 46, 52, 18, 30
    X = lambda i: PL + (W - PL - PR) * i / (len(weeks) - 1)
    slo, shi = 1.0, 5.0
    YS = lambda v: PT + (H - PT - PB) * (1 - (v - slo) / (shi - slo))
    plo, phi = min(p) * 0.96, max(p) * 1.04
    YP = lambda v: PT + (H - PT - PB) * (1 - (v - plo) / (phi - plo))
    sp = " ".join(f"{X(i):.1f},{YS(v):.1f}" for i, v in enumerate(s))
    pp = " ".join(f"{X(i):.1f},{YP(v):.1f}" for i, v in enumerate(p))
    grid = "".join(
        f'<line class="gridline" x1="{PL}" y1="{YS(v):.1f}" x2="{W-PR}" y2="{YS(v):.1f}"/>'
        f'<text class="axis" x="{PL-7}" y="{YS(v)+3.5:.1f}" text-anchor="end">{v:.0f}</text>'
        for v in (1, 2, 3, 4, 5))
    neut = (f'<line x1="{PL}" y1="{YS(3):.1f}" x2="{W-PR}" y2="{YS(3):.1f}" '
            f'stroke="var(--zinc)" stroke-width="1" stroke-dasharray="4 3" opacity=".8"/>')
    pax = "".join(
        f'<text class="axis" x="{W-PR+7}" y="{YP(v)+3.5:.1f}" fill="var(--accent)">'
        f'{v:,.0f}</text>' for v in (plo + (phi - plo) * i / 3 for i in range(4)))
    yl, seen = "", set()
    for i, w in enumerate(weeks):
        if w[:4] not in seen:
            seen.add(w[:4])
            yl += (f'<text class="axis" x="{X(i):.1f}" y="{H-8}" '
                   f'text-anchor="middle">{w[:4]}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(CO[eid])} score against '
            f'price">{grid}{neut}<polyline points="{pp}" fill="none" stroke="var(--accent)" '
            f'stroke-width="1.7" opacity=".9"/><polyline points="{sp}" fill="none" '
            f'stroke="var(--ink)" stroke-width="1.25" opacity=".85"/>{pax}{yl}</svg>')


def corr_bars(eid):
    """Trailing vs forward correlation, same scale, side by side."""
    f, t = FWD[eid], TRL[eid]
    mx = 0.55
    W, H = 460, 128
    bw = 30
    out = []
    labs = ["1w", "2w", "4w", "8w", "13w"]
    for i in range(5):
        x = 44 + i * 84
        ht = t[i] / mx * 86
        out.append(f'<rect x="{x}" y="{100-ht:.1f}" width="{bw}" height="{ht:.1f}" '
                   f'fill="var(--zinc)" opacity=".75"/>')
        hf = f[i] / mx * 86
        out.append(f'<rect x="{x+bw+4}" y="{100-hf:.1f}" width="{bw}" height="{hf:.1f}" '
                   f'fill="var(--accent)"/>')
        out.append(f'<text class="axis" x="{x+bw:.0f}" y="115" text-anchor="middle">'
                   f'{labs[i]}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="trailing vs forward correlation">'
            f'<line x1="34" y1="100" x2="{W-10}" y2="100" stroke="var(--rule)"/>'
            f'{"".join(out)}</svg>')


def bucket_rows(eid):
    mx = max(abs(b[2]) for b in BUCK[eid])
    out = ""
    for lab, n, mean, win in BUCK[eid]:
        w = abs(mean) / mx * 100
        col = "win" if mean > 0 else "loss"
        out += (f'<div class="brow"><span class="blab">{esc(lab)}</span>'
                f'<span class="mono dim">n={n}</span>'
                f'<span class="bbar"><i class="{col}" style="width:{w:.0f}%"></i></span>'
                f'<span class="mono num">{mean:+.2f}%</span>'
                f'<span class="mono dim">{win}% up</span></div>')
    return out


head = open(HERE / "_lines_head.html", encoding="utf-8").read()
blocks = ""
for eid in ("nalco", "hindalco", "hindustan_zinc"):
    best = max(BUCK[eid], key=lambda b: b[2])
    blocks += f"""
<section class="sec">
  <div class="eyebrow">{esc(CO[eid])}</div>
  <h2>Score against price</h2>
  <div class="legend"><span class="lg"><i style="background:var(--ink)"></i>economics score
    (left, 1&ndash;5)</span><span class="lg"><i style="background:var(--accent)"></i>share price
    (right)</span><span class="lg dashed">3.0 &mdash; neutral</span></div>
  {chart(eid)}
  <div class="two">
    <div>
      <h3>Does the score lead or follow?</h3>
      {corr_bars(eid)}
      <div class="legend"><span class="lg"><i style="background:var(--zinc)"></i>correlation with
        the move already <strong>past</strong></span><span class="lg"><i
        style="background:var(--accent)"></i>with the move still <strong>ahead</strong></span></div>
      <p class="note">Past {TRL[eid][0]:+.2f} against future {FWD[eid][0]:+.2f} at one week.
      The score tracks what the stock has already done
      {TRL[eid][0]/max(FWD[eid][0],0.001):.1f}&times; more strongly than what it will do.</p>
    </div>
    <div>
      <h3>Forward 13-week return, by score level</h3>
      <div class="buckets">{bucket_rows(eid)}</div>
      <p class="note">Best bucket is <strong>{esc(best[0])}</strong> at {best[2]:+.2f}%, not the
      highest one. A higher score did not mean a higher return.</p>
    </div>
  </div>
</section>
<hr class="rule"/>"""

body = f"""
<div class="wrap">
<header class="sec">
  <div class="eyebrow">P1 economics score &middot; weekly &middot; 2021 &ndash; 2026 &middot; 268 weeks</div>
  <h1>The score follows the price</h1>
  <p class="lede">No pairs here &mdash; just each company&#8217;s own score against its own share
  price, and whether one predicts the other. The short answer is that the score is a mirror, not
  a forecast: it correlates two to three times more strongly with the move that has already
  happened than with the move still to come.</p>
</header>
<hr class="rule"/>
{blocks}
<section class="sec">
  <div class="eyebrow">Why</div>
  <h2>The mechanism, and what would fix it</h2>
  <p>The score is built from commodity prices &mdash; aluminium, alumina, coal, coke, silver. The
  shares are priced off the same commodities, and they react within days. So by the time a price
  move has flowed through the bridge into a score, the market has already moved on it. The score
  is measuring the same thing as the share price, slightly later.</p>
  <p>That is not a bug in the arithmetic. The bridge is correct: it says what a commodity move
  does to profit, and it does. It is a statement about <em>information</em> &mdash; a published
  commodity price is not private, so a score derived from it cannot be early.</p>
  <div class="callout">
    <p><strong>What could carry a forecast instead.</strong> Something the market has not
    already priced: physical volumes and shipments before they are reported, contracted realisations
    that differ from spot, cost positions locked in earlier than the market assumes, or the
    captive-supply share changing. All of these are in the specs as fixed parameters today.
    They are the part of the model that is genuinely private, and they are exactly the part that
    never varies.</p>
  </div>
</section>
</div>
"""
(HERE / "score_vs_price.html").write_text(head + body, encoding="utf-8")
print("written", len(head + body))
