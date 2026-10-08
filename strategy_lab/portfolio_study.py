"""Step 1 study: is the EMA 20/50 + ADX trend filter still worthwhile as a REAL, capital-constrained portfolio?

    python -m strategy_lab.portfolio_study

Pre-registered criteria (fixed before the first run) for the PRIMARY configuration
(K=15, rank by ADX, fresh signals only, Rs 10 lakh):
  C1 worst drawdown <= 60% of buy-and-hold's (equal-weight universe)
  C2 Sharpe >= 0.80   (buy-and-hold: 0.89; allow ~0.1 below)
  C3 ranked Sharpe > 75th percentile of random-selection Sharpes (ranking must earn its keep)
  C4 C1 and C2 also hold for K=10 and K=20, and for capital Rs 5 lakh and Rs 25 lakh
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import research
from .engine import perf
from .indicators import adx, atr
from .portfolio import PCosts, simulate
from .strategies import STRATEGIES

R = Path(__file__).parent / "results"
STRAT = "ema_cross_20_50_adx"
KS, CAPS = (10, 15, 20), (500_000, 1_000_000, 2_500_000)
N_RANDOM = 30


def panel_from(P, fn):
    cal, syms = P["close"].index, list(P["close"].columns)
    out = np.full((len(cal), len(syms)), np.nan)
    for j, s in enumerate(syms):
        df = pd.DataFrame({k: P[k][s] for k in ("open", "high", "low", "close")}).dropna()
        out[cal.get_indexer(df.index), j] = fn(df).values
    return out


def evaluate(eq: pd.Series, dates: pd.DatetimeIndex, lo=research.EVAL_START):
    r = (eq.iloc[1:].values / eq.iloc[:-1].values - 1)
    ser = pd.Series(r, index=dates[: len(r)]) if False else pd.Series(r, index=dates[eq.index[1:]])
    x = ser[ser.index >= lo]
    full = perf(x)
    ins, oos = perf(x[x.index < research.SPLIT]), perf(x[x.index >= research.SPLIT])
    return full, ins, oos, ser


def main():
    t0 = time.time()
    uni, bm, cal, P, _ = research.build_world()
    syms = list(P["close"].columns)
    S = research.states_for(P, STRATEGIES[STRAT][1])
    oadj = P["open"].values
    ratio = (P["raw_close"] / P["close"]).values
    scores = {"adx": panel_from(P, adx),
              "rel_strength_126d": (P["close"] / P["close"].shift(126) - 1).values,
              "low_atr_pct": panel_from(P, lambda d: -(atr(d, 14) / d.close))}
    start = int(np.searchsorted(cal.values, np.datetime64(research.EVAL_START)))
    base = pd.read_csv(R / "daily_returns.csv", index_col=0, parse_dates=True)
    bh = perf(base["BUY&HOLD equal-weight universe"])
    sleeve = perf(base[STRAT])
    print(f"benchmarks 2010-26: buy&hold Sharpe {bh['sharpe']:.2f} maxDD {bh['maxdd']:.1%} CAGR {bh['cagr']:.1%} | "
          f"ideal sleeve Sharpe {sleeve['sharpe']:.2f} maxDD {sleeve['maxdd']:.1%} CAGR {sleeve['cagr']:.1%}")

    rows, curves = [], {}
    for fill in (False, True):
        for K in KS:
            for cap in CAPS:
                for sname, sc in scores.items():
                    eq, ex, npos, st, tr = simulate(oadj, ratio, S, sc, K, cap, start, fill_vacancies=fill)
                    full, ins, oos, ser = evaluate(eq, cal)
                    mask = (cal >= research.EVAL_START)
                    rows.append(dict(rank_by=sname, fill_vacancies=fill, K=K, capital=cap, cagr=full["cagr"], vol=full["vol"],
                                     sharpe=full["sharpe"], maxdd=full["maxdd"], calmar=full["calmar"],
                                     sharpe_is=ins["sharpe"], sharpe_oos=oos["sharpe"], maxdd_oos=oos["maxdd"],
                                     invested=float(ex[start:].mean()), avg_positions=float(npos[start:].mean()),
                                     trades=st["exits"], signals=st["signals"], skipped_full=st["skipped_full"], skipped_price=st["skipped_price"]))
                    curves[(sname, fill, K, cap)] = ser
        print(f"  grid fill={fill} done {time.time() - t0:.0f}s")
    grid = pd.DataFrame(rows)

    # random-selection baseline (same slots, random pick among simultaneous candidates)
    rnd = []
    for fill in (False, True):
        for K in KS:
            for sd in range(N_RANDOM):
                eq, ex, npos, st, tr = simulate(oadj, ratio, S, scores["adx"], K, 1_000_000, start, fill_vacancies=fill,
                                                rng=np.random.default_rng(1000 + sd))
                full, ins, oos, _ = evaluate(eq, cal)
                rnd.append(dict(fill_vacancies=fill, K=K, seed=sd, sharpe=full["sharpe"], cagr=full["cagr"], maxdd=full["maxdd"]))
    rnd = pd.DataFrame(rnd)
    grid.to_csv(R / "portfolio_grid.csv", index=False)
    rnd.to_csv(R / "portfolio_random_baseline.csv", index=False)
    pd.DataFrame({f"{a}|fill={b}|K={c}|cap={d}": v for (a, b, c, d), v in curves.items()
                  if (a, d) == ("adx", 1_000_000)}).to_csv(R / "portfolio_daily_returns_adx_10L.csv")

    # ---- pre-registered verdict -------------------------------------------------------------------------------
    g = grid[(~grid.fill_vacancies) & (grid.rank_by == "adx")]
    prim = g[(g.K == 15) & (g.capital == 1_000_000)].iloc[0]
    c1 = prim.maxdd >= 0.6 * bh["maxdd"]                      # maxdd negative: -0.22 >= -0.219
    c2 = prim.sharpe >= 0.80
    rr = rnd[(~rnd.fill_vacancies) & (rnd.K == 15)]
    c3 = prim.sharpe > rr.sharpe.quantile(0.75)
    sub = g[((g.K.isin([10, 20])) & (g.capital == 1_000_000)) | ((g.K == 15) & (g.capital.isin([500_000, 2_500_000])))]
    c4 = bool(((sub.maxdd >= 0.6 * bh["maxdd"]) & (sub.sharpe >= 0.80)).all())
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    show = grid.copy()
    for c in ("cagr", "vol", "maxdd", "maxdd_oos", "invested"): show[c] = (show[c] * 100).round(1)
    for c in ("sharpe", "calmar", "sharpe_is", "sharpe_oos"): show[c] = show[c].round(2)
    print("\nPRIMARY (K=15, ADX rank, fresh signals, Rs10L):")
    print(prim.to_frame().T.to_string(index=False))
    print(f"\nC1 drawdown <= 60% of buy&hold ({prim.maxdd:.1%} vs limit {0.6 * bh['maxdd']:.1%}): {c1}")
    print(f"C2 Sharpe >= 0.80 ({prim.sharpe:.2f}): {c2}")
    print(f"C3 beats 75th pct of random picks ({prim.sharpe:.2f} vs p75 {rr.sharpe.quantile(0.75):.2f}, median {rr.sharpe.median():.2f}): {c3}")
    print(f"C4 robust across K and capital: {c4}")
    print("\nFULL GRID (fresh signals only):"); print(show[~show.fill_vacancies].drop(columns="fill_vacancies").to_string(index=False))
    print("\nWITH VACANCY FILLING:"); print(show[show.fill_vacancies].drop(columns="fill_vacancies").to_string(index=False))
    print("\nRANDOM-PICK BASELINE (Rs10L):"); print(rnd.groupby(["fill_vacancies", "K"])[["sharpe", "cagr", "maxdd"]].agg(["mean", "std"]).round(3).to_string())
    print(f"\nDONE {time.time() - t0:.0f}s")
    return grid, rnd, curves


if __name__ == "__main__":
    main()
