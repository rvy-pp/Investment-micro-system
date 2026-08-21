"""All four pillars: current scores, history, and the composite backtest."""
import json, math, pathlib, statistics, html

HERE = pathlib.Path(__file__).resolve().parent
D = json.load(open(HERE / "_pillars.json"))
SNAP, HIST, BT, PX, LATEST = D["snap"], D["hist"], D["bt"], D["px"], D["latest"]
CO = {"nalco": "NALCO", "hindalco": "Hindalco", "vaml": "VAML",
      "hindustan_zinc": "Hindustan Zinc", "vedanta": "Vedanta"}
PIL = [("economics", "Economics"), ("valuation", "Valuation"),
       ("mood", "Mood"), ("guidance", "Guidance")]
COLR = {"economics": "#C2703A", "valuation": "#3E7CA6", "mood": "#8A6BA8",
        "guidance": "#2F7D63", "composite": "#191C20"}
esc = html.escape
ORDER = sorted(SNAP, key=lambda e: -(SNAP[e]["composite"] or 0))


def band(v):
    """Position on the 1-5 scale as a percentage."""
    return (v - 1) / 4 * 100


def scorecard():
    out = ""
    for e in ORDER:
        s = SNAP[e]
        cells = ""
        for k, lab in PIL:
            v = s[k]
            if v is None:
                cells += (f'<div class="pcell"><span class="pl">{esc(lab)}</span>'
                          f'<span class="pv dim">—</span>'
                          f'<span class="pnote">withheld</span></div>')
            else:
                tone = "hi" if v >= 3.4 else "lo" if v <= 2.6 else "mid"
                cells += (f'<div class="pcell"><span class="pl">{esc(lab)}</span>'
                          f'<span class="pv {tone}">{v:.2f}</span>'
                          f'<span class="ptrack"><i style="left:{band(v):.1f}%"></i></span></div>')
        got = [s[k] for k, _ in PIL if s[k] is not None]
        spread = max(got) - min(got)
        c = s["composite"]
        out += f"""<div class="card">
          <div class="chead"><strong>{esc(CO[e])}</strong>
            <span class="mono comp">{c:.2f}</span></div>
          <div class="ctrack"><i style="left:{band(c):.1f}%"></i></div>
          <div class="pgrid">{cells}</div>
          <div class="cfoot">spread <strong class="mono">{spread:.2f}</strong>
            &middot; {len(got)} of 4 pillars scored</div></div>"""
    return out


def hist_chart(pillar, W=1000, H=210):
    series = HIST.get(pillar, {})
    alld = sorted({d for v in series.values() for d, _ in v})
    if not alld:
        return ""
    idx = {d: i for i, d in enumerate(alld)}
    PL, PR, PT, PB = 40, 84, 14, 26
    X = lambda i: PL + (W - PL - PR) * i / max(len(alld) - 1, 1)
    Y = lambda v: PT + (H - PT - PB) * (1 - (v - 1) / 4)
    grid = "".join(
        f'<line class="gridline" x1="{PL}" y1="{Y(v):.1f}" x2="{W-PR}" y2="{Y(v):.1f}"/>'
        f'<text class="axis" x="{PL-6}" y="{Y(v)+3.5:.1f}" text-anchor="end">{v}</text>'
        for v in (1, 2, 3, 4, 5))
    neut = (f'<line x1="{PL}" y1="{Y(3):.1f}" x2="{W-PR}" y2="{Y(3):.1f}" '
            f'stroke="var(--zinc)" stroke-width="1" stroke-dasharray="4 3"/>')
    lines, labels = "", ""
    NC = {"nalco": "#C2703A", "hindalco": "#3E7CA6", "vaml": "#8A6BA8",
          "hindustan_zinc": "#2F7D63", "vedanta": "#B5432A"}
    for e, v in series.items():
        pts = " ".join(f"{X(idx[d]):.1f},{Y(s):.1f}" for d, s in v if d in idx)
        if not pts:
            continue
        lines += (f'<polyline points="{pts}" fill="none" stroke="{NC.get(e,"#888")}" '
                  f'stroke-width="1.5" opacity=".92"/>')
        ld, ls = v[-1]
        labels += (f'<text class="axis" x="{X(idx[ld])+6:.1f}" y="{Y(ls)+3.5:.1f}" '
                   f'fill="{NC.get(e,"#888")}">{esc(CO.get(e,e))}</text>')
    xl, seen = "", set()
    for d in alld:
        if d[:7] not in seen:
            seen.add(d[:7])
            xl += (f'<text class="axis" x="{X(idx[d]):.1f}" y="{H-8}" '
                   f'text-anchor="middle">{d[:7]}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(pillar)} '
            f'through time">{grid}{neut}{lines}{labels}{xl}</svg>')


def bt_strip(h, W=1000, H=150):
    rows = BT[str(h)]["rows"]
    mid, mx = H / 2, max(abs(r["ls"]) for r in rows)
    bw = (W - 20) / len(rows)
    bars = ""
    for i, r in enumerate(rows):
        ht = abs(r["ls"]) / mx * (mid - 14)
        y = mid - ht if r["ls"] > 0 else mid
        col = "win" if r["ls"] > 0 else "loss"
        bars += (f'<rect x="{10+i*bw:.2f}" y="{y:.2f}" width="{max(bw-1,1):.2f}" '
                 f'height="{ht:.2f}" fill="var(--{col})" opacity=".85">'
                 f'<title>{esc(r["d"])} long {esc(CO[r["long"]])} / short '
                 f'{esc(CO[r["short"]])} = {r["ls"]:+.2f}%</title></rect>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="each trade">'
            f'<line x1="10" y1="{mid}" x2="{W-10}" y2="{mid}" class="zero"/>{bars}</svg>')


def bt_table():
    out = ""
    for h in (5, 10, 20):
        b = BT.get(str(h))
        if not b:
            continue
        tone = "pos" if b["avg"] > 0 else "neg"
        mtone = "pos" if b["med"] > 0 else "neg"
        out += (f'<tr><td class="mono">{h}d</td><td class="mono">{b["n"]}</td>'
                f'<td class="mono">{b["win"]:.0f}%</td>'
                f'<td class="mono {tone}">{b["avg"]:+.2f}%</td>'
                f'<td class="mono {mtone}">{b["med"]:+.2f}%</td>'
                f'<td class="mono dim">{b["n"]//h}</td></tr>')
    return out


picked = {}
for r in BT["5"]["rows"]:
    k = (r["long"], r["short"])
    picked[k] = picked.get(k, 0) + 1
mix = " &middot; ".join(f"long {esc(CO[a])} / short {esc(CO[b])} &times;{n}"
                       for (a, b), n in sorted(picked.items(), key=lambda x: -x[1]))

head = open(HERE / "_pillars_head.html", encoding="utf-8").read()
charts = "".join(
    f'<section class="sec"><div class="eyebrow">{esc(lab)}</div>'
    f'<h3>{esc(lab)} through time</h3>{hist_chart(k)}</section>'
    for k, lab in PIL)

body = f"""
<div class="wrap">
<header class="sec">
  <div class="eyebrow">Four pillars &middot; 5 names &middot; as at {esc(LATEST)}</div>
  <h1>The whole scorecard</h1>
  <p class="lede">Every pillar scores 1&ndash;5 where <strong>3 means nothing is
  happening</strong>. A withheld pillar is never filled in with 3 &mdash; the composite
  renormalises over whatever scored, so missing data cannot pose as neutral evidence.</p>
</header>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">Current</div>
  <h2>Ranked by composite</h2>
  <div class="cards">{scorecard()}</div>
  <p class="note"><strong>Spread is the column to read.</strong> Hindalco at 0.10 has
  all four pillars within a tenth of neutral &mdash; a genuinely uninformative name, and
  the number says so rather than averaging to a confident-looking 2.99. Vedanta at 1.47
  is the opposite: valuation 4.47 against mood 3.00, on three pillars not four.</p>
</section>

<hr class="rule"/>

{charts}

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">Backtest</div>
  <h2>Long the top composite, short the bottom</h2>
  <p>Each date: rank the five names by composite, buy the best, short the worst, hold
  <em>h</em> trading days, and measure the return of one against the other after
  stripping the sector move.</p>
  <div class="scroll"><table class="t">
    <thead><tr><th>hold</th><th>trades</th><th>win rate</th><th>avg</th>
      <th>median</th><th>independent</th></tr></thead>
    <tbody>{bt_table()}</tbody></table></div>
  <h3>Every trade at a 5-day hold</h3>
  {bt_strip(5)}
  <p class="note">Green made money, red lost it. Hover for the pair and the date.</p>
  <div class="callout">
    <p><strong>This does not work, and the honest reading is that it cannot yet be
    judged.</strong> At a 5-day hold the composite ranking wins 38% of the time and loses
    1.52% per trade. At 20 days it wins 59% and roughly breaks even. The signs disagree
    across horizons, which is what noise looks like.</p>
    <p>The sample is the reason. <strong>37 trades at a 5-day hold is about 7 independent
    observations</strong>, over five names that move together in one sector, across two
    months. And the pair the ranking picks barely changes: {mix}. It is close to one
    repeated trade, not 37 tests.</p>
  </div>
</section>

<hr class="rule"/>

<section class="sec">
  <div class="eyebrow">What each pillar is, and is not</div>
  <h2>Read this before trusting any number above</h2>
  <div class="grid2">
    <div class="callout"><p><strong>Economics</strong> &mdash; the margin bridge:
    commodity moves &times; tonnage &times; the share bought at market, as a percentage
    of EBITDA. Tested and understood: it correlates with the move <em>already past</em>
    two to three times more strongly than the move ahead. A faithful short-term
    description, <strong>not a forecast</strong>. Do not retune it to fix that; the
    ceiling is informational.</p></div>
    <div class="callout"><p><strong>Valuation</strong> &mdash; spot EV/EBITDA, with EBITDA
    re-marked to today's prices, scored against the name's own history. Both known biases
    run in opposite directions: NALCO's peak-quarter base makes it read too cheap,
    Vedanta's pre-demerger net debt makes it read too dear.</p></div>
    <div class="callout"><p><strong>Mood</strong> &mdash; now reads what brokers
    <em>do</em>, not what they say: changes in implied upside rather than rating labels,
    reiterations that changed nothing dropped, and each broker's rating weighted by how
    much they actually vary it. Still fed by the retired vault pipeline, and
    roll-forward detection is built but inert (5 of 39 notes state a base year).</p></div>
    <div class="callout"><p><strong>Guidance</strong> &mdash; actual against target from
    the concalls, weighted by how much of the year has elapsed. It independently
    reproduced three human verdicts on Hindustan Zinc. Vedanta is withheld because its
    target depends on a capacity ramp that has not started, and annualising a
    pre-ramp quarter would report a large false miss.</p></div>
  </div>
</section>
</div>
"""
(HERE / "pillars.html").write_text(head + body, encoding="utf-8")
print("written", len(head + body))
