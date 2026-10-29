"""Upper-circuit SME: 'buy the UC, sell next day'.     python -m strategy_lab.sme_uc_study

Event: an SME-series (SM/ST) stock closes AT its upper circuit (close == high == floor(prev_close*(1+band)/0.05)*0.05, band from NSE sec_list).
  'locked'      : open == high == low == close == UC (no trade below UC all day -> only a queue of buyers; retail cannot realistically be filled)
  'not locked'  : traded below UC during the day and still closed at UC (a fill near the close was at least possible)
Outcomes use the next trading day of the SAME stock.  Scenarios:
  A  buy at the UC close, sell next open         (UPPER BOUND - assumes you were filled at the UC price)
  B  buy at the UC close, sell next close        (UPPER BOUND)
  C  buy next open (only if not locked at UC again), sell next close   (the executable chase version)
Costs: round trip 1.0% base (STT 0.1% each side + stamp, DP, brokerage, ~0.35% slippage per side); sensitivity 0.5% / 2%.
Pre-registered check for the idea to be worth pursuing (on the NOT-locked group, which is the only one with any realistic fills):
  K1 mean net return of scenario A > 0 with a day-clustered 95% interval excluding 0 at base costs
  K2 share of events where the NEXT day closes at the lower circuit (cannot sell) <= 5%
  K3 K1 holds in both 2024-25 and 2026
"""
import numpy as np
import pandas as pd

from . import sme_data

R = sme_data.CACHE.parent.parent / "results"
TICK = 0.05
COSTS = {"0.5%": 0.005, "1.0% (base)": 0.010, "2.0%": 0.020}


def uc_price(prev, band):
    return np.floor(prev * (1 + band / 100) / TICK + 1e-9) * TICK


def lc_price(prev, band):
    return np.ceil(prev * (1 - band / 100) / TICK - 1e-9) * TICK


def build():
    df = sme_data.load_all()
    df["band_n"] = pd.to_numeric(df.band.astype(str).str.strip(), errors="coerce")
    df = df.dropna(subset=["band_n", "prev_close", "close"]); df = df[(df.band_n > 0) & (df.band_n <= 20) & (df.prev_close > 0)]
    df["uc"] = uc_price(df.prev_close, df.band_n); df["lc"] = lc_price(df.prev_close, df.band_n)
    df["is_uc"] = (df.close >= df.uc - 1e-6) & (df.high >= df.close - 1e-9) & (df.volume > 0)
    df["is_lc"] = (df.close <= df.lc + 1e-6) & (df.low <= df.close + 1e-9) & (df.volume > 0)
    df["locked"] = df.is_uc & (df.open >= df.uc - 1e-6) & (df.low >= df.uc - 1e-6)
    dates = sorted(df.date.unique()); nxt = {d: dates[i + 1] for i, d in enumerate(dates[:-1])}
    n2 = df.copy(); n2["key_date"] = n2.date.map({v: k for k, v in nxt.items()})   # previous market date for each row
    merged = df.merge(n2[["symbol", "key_date", "open", "high", "low", "close", "volume", "value", "is_uc", "is_lc", "locked", "uc", "lc"]]
                      .rename(columns=lambda c: c if c in ("symbol", "key_date") else c + "_n"),
                      left_on=["symbol", "date"], right_on=["symbol", "key_date"], how="left")
    return df, merged


def clustered_ci(x, days, seed=1):
    rng = np.random.default_rng(seed); ud = np.unique(days); g = {d: x[days == d] for d in ud}
    bs = np.array([np.mean(np.concatenate([g[d] for d in rng.choice(ud, len(ud))])) for _ in range(1500)])
    return np.percentile(bs, 2.5), np.percentile(bs, 97.5)


def stat(label, ret, days, cost):
    ok = ~np.isnan(ret); r, d = ret[ok] - cost, days[ok]
    if len(r) < 5:
        print(f"  {label:34s} n={len(r)}"); return None
    lo, hi = clustered_ci(r, d)
    print(f"  {label:34s} n={len(r):4d} win {np.mean(r > 0):4.0%} mean {r.mean():+7.2%} (CI {lo:+.2%}..{hi:+.2%}) median {np.median(r):+6.2%} worst {r.min():+.1%}")
    return dict(scenario=label, n=len(r), win=np.mean(r > 0), mean=r.mean(), ci_lo=lo, ci_hi=hi, median=np.median(r), worst=r.min())


def main():
    df, m = build()
    print(f"SME-series rows {len(df):,} | stocks {df.symbol.nunique()} | {df.date.min().date()} -> {df.date.max().date()} | trading days {df.date.nunique()}")
    ev = m[m.is_uc & m.date.notna()].copy()
    ev["gapA"] = ev.open_n / ev.close - 1; ev["c2c"] = ev.close_n / ev.close - 1
    ev["o2c"] = ev.close_n / ev.open_n - 1
    ev["next_lc"] = ev.is_lc_n.fillna(False).astype(bool); ev["next_uc"] = ev.is_uc_n.fillna(False).astype(bool)
    ev["next_locked_uc_open"] = ev.open_n >= ev.uc_n - 1e-6
    ev["lot_value"] = ev.lot * ev.close
    ev["period"] = np.where(ev.date < "2026-01-01", "2024-25", "2026")
    has_next = ev.open_n.notna()
    print(f"\nUC-close events: {len(ev)} ({has_next.sum()} with a next trading day) | {ev.symbol.nunique()} stocks | locked all day: {ev.locked.mean():.0%} | on trade-for-trade (ST): {(ev.series=='ST').mean():.0%}")
    print("bands:", ev.band_n.value_counts().to_dict())
    print(f"UC-day traded value (Rs lakh): median {ev.value.median()/1e5:.1f}, 25th {ev.value.quantile(.25)/1e5:.1f}, 75th {ev.value.quantile(.75)/1e5:.1f} | minimum lot value (Rs lakh): median {ev.lot_value.median()/1e5:.1f}")
    print(f"UC-day trades: median {ev.trades.median():.0f} | share of events with <= 5 trades: {(ev.trades<=5).mean():.0%}")
    ev = ev[has_next].copy(); rows = []
    for grp, g in [("ALL UC events", ev), ("NOT locked (traded below UC)", ev[~ev.locked]), ("LOCKED all day", ev[ev.locked])]:
        print(f"\n=== {grp}: {len(g)} events ===")
        print(f"  next day: opens at UC again {g.next_locked_uc_open.mean():.0%} | closes at UC again {g.next_uc.mean():.0%} | closes at LOWER circuit (cannot sell) {g.next_lc.mean():.1%} | gap up {np.mean(g.gapA>0):.0%} gap down {np.mean(g.gapA<0):.0%}")
        for cn, c in COSTS.items():
            print(f" costs {cn}:")
            for lab, col in [("A buy UC close -> sell next open", "gapA"), ("B buy UC close -> sell next close", "c2c")]:
                r = stat(lab, g[col].values, g.date.values, c)
                if r: rows.append(dict(group=grp, costs=cn, **r))
            gc = g[~g.next_locked_uc_open]
            r = stat("C buy next open -> sell next close", gc.o2c.values, gc.date.values, c)
            if r: rows.append(dict(group=grp, costs=cn, **r))
    pd.DataFrame(rows).to_csv(R / "sme_uc_results.csv", index=False)
    ev.to_csv(R / "sme_uc_events.csv", index=False)
    # unconditional baseline: next-open / next-close vs close for ALL SME stock-days
    mm = m[m.open_n.notna()]
    print(f"\nBASELINE all SME stock-days ({len(mm):,}): mean next-open gap {np.mean(mm.open_n/mm.close-1):+.3%}, mean close-to-close {np.mean(mm.close_n/mm.close-1):+.3%}")
    # pre-registered checks on NOT-locked group
    g = ev[~ev.locked]; cost = COSTS["1.0% (base)"]
    a = (g.gapA - cost).values; lo, hi = clustered_ci(a, g.date.values)
    k1 = lo > 0; k2 = g.next_lc.mean() <= 0.05
    k3 = all((lambda x: (lambda c: c[0] > 0)(clustered_ci(x, gg.date.values)))((gg.gapA - cost).values) for _, gg in g.groupby("period") if len(gg) >= 10)
    print(f"\nK1 not-locked, scenario A mean net > 0 with interval excluding 0: mean {a.mean():+.2%} CI {lo:+.2%}..{hi:+.2%} -> {k1}")
    print(f"K2 next-day lower-circuit share <= 5%: {g.next_lc.mean():.1%} -> {k2}")
    print(f"K3 holds in both periods: {k3}")
    # streaks & repeat offenders
    print("\nrepeat offenders: top 10 stocks account for", f"{ev.symbol.value_counts().head(10).sum()/len(ev):.0%} of UC events;",
          f"stocks with >=3 UC events: {(ev.symbol.value_counts()>=3).sum()} of {ev.symbol.nunique()}")
    return ev


if __name__ == "__main__":
    main()
