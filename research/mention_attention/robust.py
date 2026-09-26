"""Robustness on the one result that survived: |fwd 5d| is LOWER after an attention spike.

Three ways it could be an artifact:
  (a) earnings season -- spikes cluster at results, and post-results vol always falls
  (b) a few names or a few days doing all the work
  (c) chance, given ~20 tests were run
"""
import os
import numpy as np
import pandas as pd
from scipy import stats

OUT = os.path.dirname(os.path.abspath(__file__))
est = pd.read_csv(os.path.join(OUT, "estimation_sample.csv"), parse_dates=["date"])
rng = np.random.default_rng(0)


def gap(s, dep="absret_fwd5"):
    s = s.dropna(subset=[dep])
    a, b = s.loc[s["spike"] == 1, dep], s.loc[s["spike"] == 0, dep]
    if len(a) < 5:
        return None
    t, p = stats.ttest_ind(a, b, equal_var=False)
    return 100 * a.mean(), 100 * b.mean(), 100 * (a.mean() - b.mean()), t, p, len(a)


print("=" * 78)
print("BASELINE")
print("=" * 78)
r = gap(est)
print("|fwd5|  spike %.2f%%  quiet %.2f%%  diff %+.2fpp  t=%.2f  p=%.4f  n_spike=%d" % r)

print()
print("=" * 78)
print("(a) EARNINGS SEASON SPLIT")
print("=" * 78)
# Q1 FY27 results for this coverage ran roughly 2026-07-17 .. 2026-08-14
ern = (est["date"] >= "2026-07-17") & (est["date"] <= "2026-08-14")
for label, sub in [("results window ", est[ern]), ("outside results", est[~ern])]:
    r = gap(sub)
    if r:
        print("%s  spike %.2f%%  quiet %.2f%%  diff %+.2fpp  t=%.2f  p=%.4f  n_spike=%d"
              % ((label,) + r))
    else:
        print("%s  too few spikes (n=%d)" % (label, int(sub["spike"].sum())))
print("   spikes inside results window: %d of %d (%.0f%%)"
      % (int(est.loc[ern, "spike"].sum()), int(est["spike"].sum()),
         100 * est.loc[ern, "spike"].sum() / est["spike"].sum()))

print()
print("=" * 78)
print("(b) INFLUENCE -- drop one company at a time, and winsorise")
print("=" * 78)
diffs = []
for ent in est["entity_id"].unique():
    r = gap(est[est["entity_id"] != ent])
    if r:
        diffs.append((ent, r[2], r[4]))
diffs.sort(key=lambda x: x[1])
print("   most favourable drop : %-18s diff %+.2fpp p=%.3f" % diffs[0])
print("   least favourable drop: %-18s diff %+.2fpp p=%.3f" % diffs[-1])
print("   sign flips on any single-company drop: %s"
      % ("YES" if diffs[-1][1] > 0 else "no"))
print("   still p<0.05 after every single-company drop: %s"
      % ("yes" if max(d[2] for d in diffs) < 0.05 else "NO (max p=%.3f)" % max(d[2] for d in diffs)))

w = est.copy()
lo, hi = w["absret_fwd5"].quantile([0.01, 0.99])
w["absret_fwd5"] = w["absret_fwd5"].clip(lo, hi)
r = gap(w)
print("   winsorised 1/99      : diff %+.2fpp  t=%.2f  p=%.4f" % (r[2], r[3], r[4]))

# drop the single biggest-|move| day overall
big = est["absret_fwd5"].idxmax()
r = gap(est.drop(index=big))
print("   drop largest |fwd5|  : diff %+.2fpp  t=%.2f  p=%.4f" % (r[2], r[3], r[4]))

print()
print("=" * 78)
print("(c) PLACEBO -- reassign the spike label at random within each company")
print("=" * 78)
obs = gap(est)[2]
sims = []
for _ in range(2000):
    p = est.copy()
    p["spike"] = p.groupby("entity_id")["spike"].transform(
        lambda s: rng.permutation(s.values))
    r = gap(p)
    if r:
        sims.append(r[2])
sims = np.array(sims)
print("   observed diff        : %+.2fpp" % obs)
print("   placebo mean         : %+.2fpp   sd %.2fpp" % (sims.mean(), sims.std()))
print("   placebo p (2-sided)  : %.4f" % np.mean(np.abs(sims) >= abs(obs)))

print()
print("=" * 78)
print("(d) SPIKE THRESHOLD SENSITIVITY")
print("=" * 78)
for lo_n, mult in [(2, 1.5), (2, 2.0), (3, 2.0), (3, 3.0), (4, 2.0)]:
    s = est.copy()
    s["spike"] = ((s["mentions"] >= lo_n) & (s["mentions"] >= mult * s["base"])).astype(float)
    r = gap(s)
    if r:
        print("   mentions>=%d and >=%.1fx base : diff %+.2fpp  t=%.2f  p=%.4f  n=%d"
              % (lo_n, mult, r[2], r[3], r[4], r[5]))

print()
print("=" * 78)
print("(e) SAME TEST ON DIRECTION, for completeness")
print("=" * 78)
for dep in ["ret_same_sx", "ret_fwd5_sx", "ret_fwd10_sx"]:
    r = gap(est, dep)
    if r:
        print("   %-14s spike %+.3f%%  quiet %+.3f%%  diff %+.3fpp  p=%.3f"
              % (dep, r[0], r[1], r[2], r[4]))
