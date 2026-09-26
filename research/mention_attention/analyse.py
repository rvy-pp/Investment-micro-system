"""Does a change in broker-mail mention count predict return direction, or only magnitude?

Primary corpus: vault digests, 47 days 2026-06-17 -> 2026-08-18, 45 companies.
Timing: a digest dated D is written pre-open on D, so open[D] is a fair entry.
  ret_same = open[T]  -> close[T]     immediate reaction   (T = first trading day >= D)
  ret_fwd5 = close[T] -> close[T+5]   post-reaction drift  (strictly after publication)
"""
import os
import sqlite3
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

IMS = os.path.expanduser("~/Investment-micro-system")
OUT = os.path.dirname(os.path.abspath(__file__))
pd.set_option("display.width", 200)

panel = pd.read_csv(os.path.join(OUT, "mention_panel.csv"), parse_dates=["date"])

con = sqlite3.connect(os.path.join(IMS, "data", "ims.db"))
px = pd.read_sql("select entity_id, date, open, close from prices where date >= '2026-04-01'",
                 con, parse_dates=["date"])
con.close()
px = px.sort_values(["entity_id", "date"])

# ---- per-entity trading-day series with forward returns -------------------
frames = []
for ent, g in px.groupby("entity_id"):
    g = g.reset_index(drop=True)
    c, o = g["close"], g["open"]
    g["ret_same"] = c / o - 1.0
    g["ret_d1"] = c.shift(-1) / c - 1.0
    g["ret_fwd5"] = c.shift(-5) / c - 1.0
    g["ret_fwd10"] = c.shift(-10) / c - 1.0
    dly = c.pct_change()
    # realised vol over the 5 sessions AFTER T, annualised
    g["vol_fwd5"] = dly.shift(-5).rolling(5).std().shift(0) * np.sqrt(252)
    g["vol_pre10"] = dly.rolling(10).std() * np.sqrt(252)
    g["entity_id"] = ent
    frames.append(g)
px = pd.concat(frames, ignore_index=True)

# ---- map each digest date to the first trading day >= D -------------------
cal = px[["entity_id", "date"]].copy()
mention = panel[panel["corpus"] == "digest"].copy()
merged = []
for ent, g in mention.groupby("entity_id"):
    pg = px[px["entity_id"] == ent]
    if pg.empty:
        continue
    m = pd.merge_asof(g.sort_values("date"), pg.sort_values("date"),
                      on="date", direction="forward", by="entity_id",
                      tolerance=pd.Timedelta("5D"))
    merged.append(m)
df = pd.concat(merged, ignore_index=True).dropna(subset=["close"])

# ---- attention features ---------------------------------------------------
df = df.sort_values(["entity_id", "date"])
g = df.groupby("entity_id")["mentions"]
df["base"] = g.transform(lambda s: s.shift(1).rolling(10, min_periods=5).mean())
df["abn"] = df["mentions"] - df["base"]
df["sov_base"] = df.groupby("entity_id")["sov"].transform(
    lambda s: s.shift(1).rolling(10, min_periods=5).mean())
df["abn_sov"] = df["sov"] - df["sov_base"]
df["spike"] = ((df["mentions"] >= 3) & (df["mentions"] >= 2 * df["base"])).astype(float)
df["silent"] = (df["mentions"] == 0).astype(float)

# sector-demeaned returns: strips the sector-wide day, which moves mentions AND price together
for col in ["ret_same", "ret_fwd5", "ret_fwd10"]:
    df[col + "_sx"] = df[col] - df.groupby(["date", "sector"])[col].transform("mean")

df["absret_same"] = df["ret_same"].abs()
df["absret_fwd5"] = df["ret_fwd5"].abs()
df["absret_same_sx"] = df["ret_same_sx"].abs()

est = df.dropna(subset=["abn", "ret_same", "ret_fwd5"]).copy()
est["ent"] = est["entity_id"].astype("category")
est["dt"] = est["date"].astype(str).astype("category")

print("=" * 78)
print("SAMPLE")
print("=" * 78)
print("company-days with mentions + prices + baseline: %d" % len(est))
print("companies %d   trading days %d   spike days %d (%.1f%% of obs)"
      % (est["entity_id"].nunique(), est["date"].nunique(),
         int(est["spike"].sum()), 100 * est["spike"].mean()))
print("mentions: mean %.2f  sd %.2f  max %d" % (est["mentions"].mean(),
                                                est["mentions"].std(), est["mentions"].max()))

print()
print("=" * 78)
print("1. EVENT STUDY  -- attention spike vs the rest (same companies)")
print("=" * 78)
rows = []
for dep, label in [("ret_same", "same-day open->close"),
                   ("ret_same_sx", "same-day, sector-demeaned"),
                   ("ret_fwd5", "fwd 5d close->close"),
                   ("ret_fwd5_sx", "fwd 5d, sector-demeaned"),
                   ("absret_same", "|same-day|"),
                   ("absret_fwd5", "|fwd 5d|"),
                   ("vol_fwd5", "realised vol, next 5d")]:
    s = est.dropna(subset=[dep])
    a, b = s.loc[s["spike"] == 1, dep], s.loc[s["spike"] == 0, dep]
    if len(a) < 5:
        continue
    from scipy import stats
    t, p = stats.ttest_ind(a, b, equal_var=False)
    rows.append(dict(metric=label, spike=100 * a.mean(), quiet=100 * b.mean(),
                     diff_pp=100 * (a.mean() - b.mean()), t=t, p=p, n_spike=len(a)))
ev = pd.DataFrame(rows)
print(ev.to_string(index=False, float_format=lambda x: "%8.3f" % x))

print()
print("=" * 78)
print("2. PANEL REGRESSION  -- company + date fixed effects, SE clustered by date")
print("=" * 78)
res_rows = []
for dep in ["ret_same", "ret_fwd5", "ret_fwd10", "absret_same", "absret_fwd5", "vol_fwd5"]:
    s = est.dropna(subset=[dep, "abn"])
    if len(s) < 50:
        continue
    m = smf.ols(dep + " ~ abn + C(ent) + C(dt)", data=s).fit(
        cov_type="cluster", cov_kwds={"groups": s["date"]})
    res_rows.append(dict(dep=dep, beta_per_mention_pp=100 * m.params["abn"],
                         t=m.tvalues["abn"], p=m.pvalues["abn"],
                         r2_within=m.rsquared, n=int(m.nobs)))
print(pd.DataFrame(res_rows).to_string(index=False, float_format=lambda x: "%9.4f" % x))

print()
print("   same, using share-of-voice instead of raw count:")
res_rows = []
for dep in ["ret_same", "ret_fwd5", "absret_same", "vol_fwd5"]:
    s = est.dropna(subset=[dep, "abn_sov"])
    m = smf.ols(dep + " ~ abn_sov + C(ent) + C(dt)", data=s).fit(
        cov_type="cluster", cov_kwds={"groups": s["date"]})
    res_rows.append(dict(dep=dep, beta_per_1pct_sov_pp=100 * m.params["abn_sov"] / 100,
                         t=m.tvalues["abn_sov"], p=m.pvalues["abn_sov"], n=int(m.nobs)))
print(pd.DataFrame(res_rows).to_string(index=False, float_format=lambda x: "%9.4f" % x))

print()
print("=" * 78)
print("3. DIRECTION TEST  -- is the sign of the move predictable at all?")
print("=" * 78)
s = est.dropna(subset=["ret_fwd5_sx"])
sp = s[s["spike"] == 1]
up = (sp["ret_fwd5_sx"] > 0).mean()
from scipy import stats
bt = stats.binomtest(int((sp["ret_fwd5_sx"] > 0).sum()), len(sp), 0.5)
print("after an attention spike, fwd-5d sector-adj return is positive %.1f%% of the time"
      % (100 * up))
print("   n = %d   binomial p vs 50%% = %.3f" % (len(sp), bt.pvalue))
print("   (a directionless attention signal predicts exactly 50%%)")

print()
print("=" * 78)
print("4. ATTENTION PERSISTENCE  -- how long does a spike stay elevated?")
print("=" * 78)
sp_idx = est.index[est["spike"] == 1]
prof = []
for k in range(0, 6):
    vals = []
    for i in sp_idx:
        ent = est.at[i, "entity_id"]
        d = est.at[i, "date"]
        fut = est[(est["entity_id"] == ent) & (est["date"] > d)].head(k + 1)
        if len(fut) == k + 1:
            vals.append(fut.iloc[k]["mentions"] if k > 0 else est.at[i, "mentions"])
    if vals:
        prof.append((k, np.mean(vals), len(vals)))
print("   day   mean mentions   n")
for k, mu, n in prof:
    print("   T+%-3d  %8.2f      %d" % (k, mu, n))
base = est["mentions"].mean()
print("   unconditional mean = %.2f" % base)

est.to_csv(os.path.join(OUT, "estimation_sample.csv"), index=False)
print("\nwrote estimation_sample.csv (%d rows)" % len(est))
