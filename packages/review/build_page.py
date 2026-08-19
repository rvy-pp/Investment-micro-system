"""Render the backtest explainer page from the trade list."""
import json, math, statistics, html, pathlib

HERE = pathlib.Path(__file__).resolve().parent
D = json.load(open(HERE / "_viz.json"))
T, EQ, S = D["trades"], D["equity"], D["summary"]
ex = [t for t in T if t["month"] == "2025-10"][0]
head = open(HERE / "_page_head.html", encoding="utf-8").read()

RS = "&#8377;"
esc = lambda x: html.escape(str(x))


def sgn(v, d=2, suf="%"):
    cls = "pos" if v > 0 else "neg"
    return f'<span class="mono {cls}">{v:+.{d}f}{suf}</span>'


# ---- equity curve, log scale ----
W, H, PL, PR, PT, PB = 1000, 300, 52, 14, 14, 30
lo, hi = min(EQ), max(EQ)
lo_l, hi_l = math.log10(max(lo, 1)), math.log10(hi)
X = lambda i: PL + (W - PL - PR) * i / (len(EQ) - 1)
Y = lambda v: PT + (H - PT - PB) * (1 - (math.log10(max(v, 1)) - lo_l) / (hi_l - lo_l))
pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(EQ))
grid = "".join(
    f'<line class="gridline" x1="{PL}" y1="{Y(t):.1f}" x2="{W-PR}" y2="{Y(t):.1f}"/>'
    f'<text class="axis" x="{PL-7}" y="{Y(t)+3.5:.1f}" text-anchor="end">{t}</text>'
    for t in (100, 300, 1000, 3000) if lo <= t <= hi)
yrs = {}
for i, t in enumerate(T):
    yrs.setdefault(t["month"][:4], i)
xlab = "".join(f'<text class="axis" x="{X(i):.1f}" y="{H-8}" text-anchor="middle">{y}</text>'
               for y, i in yrs.items() if int(y) % 3 == 0)
vi = [i for i, t in enumerate(T) if "2023-08" <= t["month"] <= "2026-05"]
shade = (f'<rect x="{X(vi[0]):.1f}" y="{PT}" width="{X(vi[-1])-X(vi[0]):.1f}" '
         f'height="{H-PT-PB}" fill="var(--accent)" opacity=".07"/>') if vi else ""
vlab = (f'<text class="axis" x="{X(vi[0])+5:.1f}" y="{PT+13}" fill="var(--accent)">'
        f'vault&#8217;s test window</text>') if vi else ""
eqsvg = (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Equity curve of 164 trades, '
         f'log scale">{shade}{grid}{vlab}<polyline class="eqline" points="{pts}"/>{xlab}</svg>')

# ---- per-trade bar strip ----
W2, H2 = 1000, 150
mid = H2 / 2
mx = max(abs(t["alpha_pct"]) for t in T)
bw = (W2 - 20) / len(T)
bars = []
for i, t in enumerate(T):
    h = abs(t["alpha_pct"]) / mx * (mid - 12)
    y = mid - h if t["alpha_pct"] > 0 else mid
    col = "win" if t["alpha_pct"] > 0 else "loss"
    tip = (f'{esc(t["month"])} {esc(t["regime"])} &middot; long {esc(t["long"])} / '
           f'short {esc(t["short"])} &middot; {t["alpha_pct"]:+.2f}%')
    bars.append(f'<rect x="{10+i*bw:.2f}" y="{y:.2f}" width="{max(bw-0.7,0.7):.2f}" '
                f'height="{h:.2f}" fill="var(--{col})" opacity=".85"><title>{tip}</title></rect>')
strip = (f'<svg viewBox="0 0 {W2} {H2}" role="img" aria-label="Return of each of 164 trades">'
         f'<line class="zero" x1="10" y1="{mid}" x2="{W2-10}" y2="{mid}"/>{"".join(bars)}</svg>')

rows = "".join(
    f'<tr><td class="mono">{esc(k)}</td><td class="mono">{v["n"]}</td>'
    f'<td class="mono">{v["win"]:.0f}%</td><td>{sgn(v["avg"],1," "+RS)}</td></tr>'
    for k, v in sorted(S["regime"].items()))

wr = S["wins"] / S["n"] * 100
body = f"""
<div class="wrap">

<header class="sec">
  <div class="eyebrow">Aluminium regime model &middot; 164 trades &middot; Jul 2012 &ndash; Aug 2026</div>
  <h1>What one trade actually is</h1>
  <p class="lede">Every number I gave you before &mdash; win rate, alpha, Sharpe &mdash; comes from
  repeating one simple action 164 times. Here is that action, in rupees, before any statistics.</p>
</header>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">The action</div>
  <h2>Once a month, buy one stock and short another. Hold 90 trading days. Close.</h2>
  <p>That is the whole strategy. It is a <strong>pair</strong> &mdash; always long one name and short
  another, never just long. The two commodity prices decide which pair. Here is the trade the model
  put on in <span class="mono">October 2025</span>:</p>

  <div class="card">
    <div class="leg">
      <span class="tag r">Signal</span>
      <span>LME aluminium {sgn(ex['alu'],1)} on the month, alumina {sgn(ex['alm'],1)}
        &rarr; regime <strong class="mono">{esc(ex['regime'])}</strong></span>
      <span class="mono" style="color:var(--zinc)">rule &rarr; long {esc(ex['long'])}</span>
    </div>
    <div class="leg">
      <span class="tag l">Buy {RS}200</span>
      <span><strong>{esc(ex['long'])}</strong> at entry, sold 90 trading days later</span>
      <span class="num mono pos">{ex['lret']:+.2f}%</span>
    </div>
    <div class="leg">
      <span class="tag s">Short {RS}100</span>
      <span><strong>{esc(ex['short'])}</strong> at entry, bought back 90 trading days later</span>
      <span class="num mono pos">{ex['sret']:+.2f}%</span>
    </div>
    <div class="leg" style="border-top:1px solid var(--rule);padding-top:15px">
      <span class="tag r">Result</span>
      <span class="mono" style="font-size:13.5px;color:var(--ink-2)">
        {RS}200 &times; {ex['lret']:+.2f}% = {200*ex['lret']/100:+.1f}
        &nbsp;&minus;&nbsp; {RS}100 &times; {ex['sret']:+.2f}% = {100*ex['sret']/100:+.1f}
      </span>
      <span class="num mono pos">+{RS}{ex['pnl_rs']:.1f}</span>
    </div>
  </div>

  <div class="callout">
    <p><strong>Why short at all?</strong> Both stocks rose that month &mdash; {esc(ex['short'])} was up
    {ex['sret']:+.2f}%. If I only told you {esc(ex['long'])} gained {ex['lret']:+.2f}%, I would be
    measuring the aluminium market, not the model. The short leg cancels the sector move. What is left
    is the only thing the model claims to know: that {esc(ex['long'])} would beat {esc(ex['short'])}.</p>
    <p><strong>Why {RS}200 against {RS}100?</strong> That is the 2:1 sizing &mdash; the long leg gets
    twice the money. At 1:1 the same 164 trades return roughly half as much.</p>
  </div>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">The counting</div>
  <h2>&ldquo;Win rate&rdquo; is just how many of those 164 trades ended above zero</h2>
  <p>No weighting, no adjustment. Count the trades that made money, divide by 164.</p>
  <div class="grid4">
    <div class="stat"><span class="k">Trades</span><span class="v mono">{S['n']}</span>
      <span class="s">one per month</span></div>
    <div class="stat"><span class="k">Ended positive</span>
      <span class="v mono pos">{S['wins']}</span><span class="s">made money</span></div>
    <div class="stat"><span class="k">Ended negative</span>
      <span class="v mono neg">{S['n']-S['wins']}</span><span class="s">lost money</span></div>
    <div class="stat"><span class="k">Win rate</span><span class="v mono">{wr:.0f}%</span>
      <span class="s">{S['wins']} &divide; {S['n']}</span></div>
  </div>
  <p class="note">A coin flip is 50%. This is {wr:.0f}%. The average trade made
  <span class="mono">{RS}{S['avg']:.2f}</span> on the <span class="mono">{RS}300</span> deployed, but the
  median was <span class="mono">{RS}{S['med']:.2f}</span> &mdash; a handful of large winners carry
  that average.</p>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">Every trade</div>
  <h2>All 164, in order. Green made money, red lost it.</h2>
  {strip}
  <p class="note">Bar height is that trade&#8217;s return on {RS}300. Hover any bar for the month and the
  pair. Best was <span class="mono">{esc(S['best']['month'])}</span>, long {esc(S['best']['long'])} /
  short {esc(S['best']['short'])} at {sgn(S['best']['alpha_pct'])}. Worst was
  <span class="mono">{esc(S['worst']['month'])}</span>, long {esc(S['worst']['long'])} /
  short {esc(S['worst']['short'])} at {sgn(S['worst']['alpha_pct'])}.</p>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">The problem</div>
  <h2>The model only works inside the shaded band</h2>
  {eqsvg}
  <p class="note">{RS}100 compounded through every trade, log scale. The shaded band is the window the
  vault&#8217;s original study tested &mdash; a stretch when LME aluminium rose 74%. Outside it the line
  is close to flat.</p>
  <div class="grid2">
    <div class="stat"><span class="k">Vault&#8217;s window &middot; 2023&ndash;26</span>
      <span class="v mono">{S['vault']['win']:.0f}%</span>
      <span class="s">{S['vault']['n']} trades &middot; avg {RS}{S['vault']['avg']:.1f}
        &middot; LME +74%</span></div>
    <div class="stat"><span class="k">The 11 years before it</span>
      <span class="v mono">{S['pre']['win']:.0f}%</span>
      <span class="s">{S['pre']['n']} trades &middot; avg {RS}{S['pre']['avg']:.1f}
        &middot; LME +21%</span></div>
  </div>
  <div class="callout">
    <p>{S['pre']['win']:.0f}% is a coin flip. The original 74% came from testing inside one aluminium
    bull market. Same rules, same code, earlier decade &mdash; the edge is not there.</p>
  </div>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">By regime</div>
  <h2>Which pair the rule picks, and how each did</h2>
  <div class="scroll"><table>
    <thead><tr><th>Regime</th><th>Trades</th><th>Win rate</th><th>Avg P&amp;L</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  <p class="note">R1 both prices up &middot; R2 aluminium up, alumina down &middot; R3 aluminium down,
  alumina up &middot; R4 both down. R3 is the one the vault called a goldmine at 100% over 7 months;
  over 31 months it is {S['regime']['R3']['win']:.0f}%.</p>
  <p class="note"><strong>One caveat against my own numbers.</strong> Before VAML listed in June 2026 I
  substitute Vedanta, which was then a diversified conglomerate &mdash; a weak stand-in. That affects
  R2 and R3. The two clean pairs are R1 and R4, both NALCO against Hindalco, and they run at
  {S['regime']['R1']['win']:.0f}% and {S['regime']['R4']['win']:.0f}%.</p>
</section>

</div>
"""
out = HERE / "backtest_explained.html"
out.write_text(head + body, encoding="utf-8")
print("written", out, len(head + body), "bytes")
