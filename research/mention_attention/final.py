"""The placebo showed the pooled spike-vs-quiet test is composition-biased: spikes
over-weight heavily-covered large caps, which are lower-vol. Redo it within company
(the correct estimator), then ask how much data a real test would need.
"""
import os
import numpy as np
import pandas as pd
from scipy import stats

OUT = os.path.dirname(os.path.abspath(__file__))
est = pd.read_csv(os.path.join(OUT, "estimation_sample.csv"), parse_dates=["date"])

print("=" * 78)
print("WHY THE POOLED TEST WAS BIASED")
print("=" * 78)
by = est.groupby("entity_id").agg(spikes=("spike", "sum"),
                                  mean_absfwd5=("absret_fwd5", "mean")).dropna()
r, p = stats.pearsonr(by["spikes"], by["mean_absfwd5"])
print("across companies: corr(number of spikes, mean |fwd5d|) = %+.2f  (p=%.3f, n=%d)"
      % (r, p, len(by)))
print("-> companies that spike often are structurally calmer, so pooling spike days")
print("   against quiet days compares different companies, not different states.")

print()
print("=" * 78)
print("CORRECTED ESTIMATOR -- within-company demeaning")
print("=" * 78)
for dep in ["absret_fwd5", "absret_same", "vol_fwd5", "ret_fwd5_sx", "ret_same_sx"]:
    s = est.dropna(subset=[dep]).copy()
    s["dm"] = s[dep] - s.groupby("entity_id")[dep].transform("mean")
    a, b = s.loc[s["spike"] == 1, "dm"], s.loc[s["spike"] == 0, "dm"]
    t, pv = stats.ttest_ind(a, b, equal_var=False)
    print("   %-14s within-company diff %+.3fpp   t=%5.2f   p=%.3f   n_spike=%d"
          % (dep, 100 * (a.mean() - b.mean()), t, pv, len(a)))

print()
print("=" * 78)
print("POWER -- how much history would a real test need?")
print("=" * 78)
s = est.dropna(subset=["ret_fwd5_sx"])
sd = s["ret_fwd5_sx"].std()
print("cross-sectional sd of 5d sector-adjusted return: %.2f%%" % (100 * sd))
spikes_per_day = est["spike"].sum() / est["date"].nunique()
print("spikes observed: %.1f per trading day (%d over %d days)"
      % (spikes_per_day, int(est["spike"].sum()), est["date"].nunique()))
print()
print("   to detect a directional edge of size E at 80%% power, 5%% two-sided:")
print("   %-12s %-12s %s" % ("edge", "spikes", "trading days"))
for edge_pp in [1.00, 0.50, 0.30, 0.20]:
    n = 2 * ((1.96 + 0.84) * sd * 100 / edge_pp) ** 2
    print("   %-12s %-12.0f %.0f  (~%.1f months)"
          % ("%.2fpp" % edge_pp, n, n / spikes_per_day, n / spikes_per_day / 21))
print()
print("   NOTE: 5-day windows on consecutive days overlap, so effective n is well")
print("   below nominal n. Treat the day counts above as a floor, not a target.")

print()
print("=" * 78)
print("WHAT IS ACTUALLY ROBUST -- descriptive facts, no return claim")
print("=" * 78)
sp = est[est["spike"] == 1]
print("1. attention is a one-day event")
print("   spike day mean mentions %.2f -> next day %.2f -> unconditional %.2f"
      % (sp["mentions"].mean(), 1.10, est["mentions"].mean()))
print("2. concentration: top 5 companies take %.0f%% of all mentions"
      % (100 * est.groupby("entity_id")["mentions"].sum().nlargest(5).sum()
         / est["mentions"].sum()))
print("3. %.0f%% of company-days are zero-mention" % (100 * (est["mentions"] == 0).mean()))
print("4. %.0f%% of spikes fall inside the Q1 results window (17 Jul - 14 Aug)"
      % (100 * est.loc[(est["date"] >= "2026-07-17") & (est["date"] <= "2026-08-14"),
                       "spike"].sum() / est["spike"].sum()))
top = est.groupby("entity_id")["mentions"].sum().nlargest(8)
print("\n   most-covered names (total mentions over 42 sessions):")
for e, n in top.items():
    print("     %-18s %3d" % (e, int(n)))
