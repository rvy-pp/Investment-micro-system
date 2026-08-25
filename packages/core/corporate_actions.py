"""The ONE hand-verified list of confirmed corporate actions.

WHY THIS FILE EXISTS. Two places needed to know "is this 15% step a corporate
action or a real market move", and they answered it differently:

  api/tape.py       kept a hand-verified allow-list, CONFIRMED_ACTIONS, and drew
                    THROUGH anything not on it. Its own comment says why: "Add to
                    this list only when the action is verified against a filing.
                    A wrong entry here silently deletes a real return from the
                    chart."
  score/valuation.py truncated the history at ANY single-session move >= 15%, with
                    no allow-list at all.

So the pair chart drew through the 2024 election selloff while the valuation
pillar cut its history at it. Same data, same question, opposite answers, and only
one of them was written down as a decision.

WHAT IT COST, measured 2026-08-25 across the nine scored names. `basis` is the
days of history the z-score was actually computed over, against a spec that says
`lookback_days: 1260`:

    vedanta            84d   from 2026-04-30  -65%   <- REAL, the demerger
    nalco             556d   from 2024-06-04  -19%   <- election result
    hindustan_zinc    566d   from 2024-05-21  +26%   <- real market move
    sail              511d   from 2024-06-04  -20%   <- election result
    jindal_steel      916d   from 2022-05-23  -17%   <- steel export duty
    jsw_steel       1,448d   from 2020-03-23  -18%   <- COVID crash
    hindalco        1,585d   from 2020-04-07  +17%   <- COVID recovery
    tata_steel      1,767d   (no cut)
    apl_apollo         n/a   (own peer group)

Only the first is a corporate action. CLAUDE.md already recorded this conclusion
for the aluminium names — "all of them are real market moves — the COVID crash,
the 2024 election result, the failed VEDL delisting" — and tape.py already acted
on it. valuation.py had not, so five names' valuation z was measured against a
window that a real selloff had truncated, three of them to under two years.

THE STEEL ONES ARE IDENTIFIED, not guessed:
  2022-05-21  India imposed a 15% steel export duty. It hit every steel name;
              jindal_steel -17.4% and jindal_stainless -18.1% on 23-May.
  2024-06-04  General election result; a broad PSU selloff. sail -20.0%,
              nalco -19%.
  2020-03-23  COVID crash, and 2020-04-07 the bounce off it.

ADD TO THIS LIST ONLY WHEN THE ACTION IS VERIFIED AGAINST A FILING. A wrong entry
deletes a real return from a chart and a real observation from a valuation
history, in both cases silently. An entry that is MISSING is the safe failure: the
series is drawn through and the step shows up as a move, which a reader can see.

CANDIDATES DELIBERATELY NOT ADDED, because they are not verified:
  jindal_stainless 2015-11-19  -60.9%. Almost certainly the JSL restructuring,
      and the magnitude is right for a demerger rather than a market move — but
      it has not been checked against a filing. Anything regressing JSL across
      2015-16 is comparing two different companies; that warning lives in
      specs/sectors/steel.yaml known_discontinuities instead of here.
"""

from __future__ import annotations

# entity_id -> {date: reason}. `date` is the first session on the NEW basis.
CONFIRMED_ACTIONS: dict[str, dict[str, str]] = {
    "vedanta": {
        "2026-04-30": "1:1 demerger of four entities, record date 1 May 2026",
    },
}


def confirmed_cut(entity_id: str, rows, jump: float = 0.15):
    """Latest CONFIRMED action in `rows`, or (None, None).

    `rows` is [(date, close)] ascending. Returns (date, pct_move) for the most
    recent step that is BOTH >= `jump` and on the allow-list, so an unverified
    jump is drawn through rather than truncating the series.

    Deliberately NOT "the latest jump": that is the behaviour this module exists
    to replace. Compare `adapters/check_corporate_actions.py`, which reports
    EVERY jump — that is a detection tool whose whole point is to surface
    candidates for a human, and it prints a note saying so.
    """
    known = CONFIRMED_ACTIONS.get(entity_id) or {}
    if not known:
        return None, None
    for (d0, c0), (d1, c1) in zip(reversed(rows[:-1]), reversed(rows[1:])):
        if c0 and abs(c1 / c0 - 1) >= jump and d1 in known:
            return d1, (c1 / c0 - 1) * 100
    return None, None
