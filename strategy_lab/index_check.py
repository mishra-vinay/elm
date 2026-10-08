"""Survivorship-free sanity check: run the same trend rules on a broad INDEX (Sensex, 1997-2026).

The stock study uses today's large caps (survivorship-biased) over 2010-2026. This check uses an index that
reconstitutes itself, over a longer span that includes 1998-2009 (dot-com bust, 2008 GFC) which the stock study
never sees. Costs are set for trading an index ETF/futures proxy (~0.10% round trip) and are an assumption.
Run:  python -m strategy_lab.index_check
"""
import numpy as np
import pandas as pd

from . import data
from .engine import RF, Costs, perf
from .strategies import STRATEGIES

COST = Costs(0.0005, 0.0005)   # assumption: ETF/futures-like, 5 bps per side incl. slippage
TREND = ["sma200_filter", "ema_cross_10_30", "ema_cross_20_50", "ema_cross_50_200", "ema_cross_20_50_adx",
         "donchian_20_10", "donchian_55_20", "donchian_100_50", "supertrend_10_3", "supertrend_20_3", "high52_trail3atr"]


def run(symbol="^BSESN"):
    df = data.load(symbol, start="1997-01-01", tag="_long")
    df = df[["open", "high", "low", "close"]].dropna()
    df = df[(df.open > 0)]
    rows, curves = [], {}
    # open-to-open interval returns; state at close t -> hold interval t+1
    ret = (df.open.shift(-1) / df.open - 1).values
    for name in TREND:
        st = STRATEGIES[name][1](df)
        H = np.concatenate([[0], st[:-1]]).astype(int)
        prev = np.concatenate([[0], H[:-1]])
        r = np.where(H == 1, ret, RF / 252) - ((H == 1) & (prev == 0)) * COST.buy - ((H == 0) & (prev == 1)) * COST.sell
        curves[name] = pd.Series(r, index=df.index).iloc[:-1]
        curves[name + "|expo"] = H.mean()
    bh = pd.Series(ret, index=df.index).iloc[:-1]
    for lo, hi, lab in [("1998-01-01", "2010-01-01", "1998-2009"), ("2010-01-01", "2027-01-01", "2010-2026"),
                        ("1998-01-01", "2027-01-01", "1998-2026")]:
        b = perf(bh[(bh.index >= lo) & (bh.index < hi)])
        rows.append(dict(period=lab, strategy="BUY&HOLD Sensex (price)", exposure=1.0, **{k: b[k] for k in ("cagr", "vol", "sharpe", "maxdd", "calmar")}))
        for name in TREND:
            x = curves[name]; x = x[(x.index >= lo) & (x.index < hi)]
            h = np.concatenate([[0], STRATEGIES[name][1](df)[:-1]])
            m = perf(x)
            rows.append(dict(period=lab, strategy=name, exposure=float(h[(df.index >= lo) & (df.index < hi)].mean()),
                             **{k: m[k] for k in ("cagr", "vol", "sharpe", "maxdd", "calmar")}))
    out = pd.DataFrame(rows)
    return out, df


if __name__ == "__main__":
    out, df = run()
    print(f"Sensex price index {df.index[0].date()} -> {df.index[-1].date()} ({len(df)} bars)")
    from pathlib import Path
    out.to_csv(Path(__file__).parent / "results" / "index_check_sensex.csv", index=False)
    pd.set_option("display.width", 200)
    for lab, g in out.groupby("period", sort=False):
        t = g.drop(columns="period").set_index("strategy").copy()
        for c in ("cagr", "vol", "maxdd", "exposure"):
            t[c] = (t[c] * 100).round(1)
        print(f"\n== {lab} ==\n", t.round(2).to_string())
