"""Swing: does a VOLUME filter improve a breakout?   python -m strategy_lab.volume_swing

Paired with the earlier Donchian tests (same channel lengths, no volume filter):
  vol_breakout_20_10 : close > prior 20-day high AND volume >= 2x prior 20-day average; exit on close < prior 10-day low
  vol_breakout_55_20 : close > prior 55-day high AND volume >= 2x prior 20-day average; exit on close < prior 20-day low
These are trials #20 and #21 (the main study tested 19). Same engine, costs, split and screen as research.py.
"""
import numpy as np
import pandas as pd

from . import research
from .engine import COSTS, annual_returns, circular_shift_null, extract_trades, holdings, interval_returns, perf, sharpe_of, sleeve_returns, trade_stats
from .strategies import _b, _machine


def vol_breakout(n_in, n_out, vmult=2.0):
    def f(df):
        brk = df.close > df.high.rolling(n_in, min_periods=n_in).max().shift(1)
        vol = df.volume >= vmult * df.volume.rolling(20, min_periods=20).mean().shift(1)
        stop = df.close < df.low.rolling(n_out, min_periods=n_out).min().shift(1)
        return _machine(_b(brk & vol), _b(stop))
    return f


def states(P, fn):
    cal, syms = P["close"].index, list(P["close"].columns)
    S = np.zeros((len(cal), len(syms)), dtype=np.int8)
    for j, s in enumerate(syms):
        df = pd.DataFrame({k: P[k][s] for k in ("open", "high", "low", "close", "volume")}).dropna(subset=["open", "high", "low", "close"])
        df["volume"] = df.volume.fillna(0)
        S[cal.get_indexer(df.index), j] = fn(df)
    return S


def main():
    uni, bm, cal, P, _ = research.build_world()
    ret = interval_returns(P["open"]).values; open_np = P["open"].values; syms = list(P["close"].columns)
    ev = np.asarray(cal >= research.EVAL_START); N = len(syms)
    halves = [np.arange(0, N, 2), np.arange(1, N, 2)]
    prior = pd.read_csv(research.RESULTS / "summary.csv", index_col=0)
    rows = []
    for name, (nin, nout) in {"vol_breakout_20_10": (20, 10), "vol_breakout_55_20": (55, 20)}.items():
        S = states(P, vol_breakout(nin, nout)); H = holdings(S, ret)
        r = pd.Series(sleeve_returns(H, ret), index=cal)
        full, ins, oos = [research.slice_perf(r, *a) for a in ((research.EVAL_START,), (research.EVAL_START, research.SPLIT), (research.SPLIT,))]
        tr = extract_trades(H, open_np, ret, syms, cal); ts = trade_stats(tr[tr.entry >= research.EVAL_START])
        sims = circular_shift_null(S, ret, 200, mask=ev); p = (1 + (sims >= sharpe_of(r.values[ev])).sum()) / (1 + len(sims))
        s1, s2 = (sharpe_of(sleeve_returns(H, ret, COSTS.scaled(m))[ev]) for m in (1, 2))
        hv = [sharpe_of(sleeve_returns(H, ret, cols=h)[ev]) for h in halves]
        expo = float((H[ev] * ~np.isnan(ret[ev])).sum() / (~np.isnan(ret[ev])).sum())
        rows.append(dict(strategy=name, cagr=full["cagr"], vol=full["vol"], sharpe=full["sharpe"], maxdd=full["maxdd"], calmar=full["calmar"],
                         exposure=expo, sharpe_is=ins["sharpe"], sharpe_oos=oos["sharpe"], p_null=p, sharpe_2x=s2, sharpe_1x=s1,
                         half_a=hv[0], half_b=hv[1], trades=ts["trades"], win=ts["win"], payoff=ts["payoff"], avg_net=ts["avg_net"], hold=ts["hold"],
                         screen=bool(ins["sharpe"] > 0 and oos["sharpe"] > 0 and p <= 0.10 and s2 >= 0.7 * s1 and min(hv) > 0)))
    out = pd.DataFrame(rows).set_index("strategy"); out.to_csv(research.RESULTS / "volume_swing_results.csv")
    pairs = {"vol_breakout_20_10": "donchian_20_10", "vol_breakout_55_20": "donchian_55_20"}
    pd.set_option("display.width", 220)
    for k, d in pairs.items():
        a, b = out.loc[k], prior.loc[d]
        print(f"\n{k}  vs  {d} (same channel, NO volume filter)")
        print(f"  Sharpe {a.sharpe:.2f} vs {b.sharpe:.2f} | CAGR {a.cagr:.1%} vs {b.cagr:.1%} | maxDD {a.maxdd:.1%} vs {b.maxdd:.1%} | invested {a.exposure:.0%} vs {b.exposure:.0%}")
        print(f"  OOS Sharpe {a.sharpe_oos:.2f} vs {b.sharpe_oos:.2f} | null p {a.p_null:.3f} vs {b.p_null:.3f} | trades {int(a.trades)} vs {int(b.trades)} | win {a.win:.0%} vs {b.win:.0%} | avg net trade {a.avg_net:.2%} vs {b.avg_net:.2%}")
        print(f"  Sharpe 2x costs {a.sharpe_2x:.2f} vs {b.sharpe_cost2:.2f} | halves {a.half_a:.2f}/{a.half_b:.2f} | passes pre-registered screen: {a.screen}")
    bh = prior.loc["BUY&HOLD equal-weight universe"]
    print(f"\nreference buy&hold: Sharpe {bh.sharpe:.2f} CAGR {bh.cagr:.1%} maxDD {bh.maxdd:.1%}")
    return out


if __name__ == "__main__":
    main()
