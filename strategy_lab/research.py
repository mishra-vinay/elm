"""Run the full strategy study:  python -m strategy_lab.research

Pipeline: load -> clean -> build states for every strategy -> sleeve/rebalanced portfolios -> metrics on
full / in-sample / out-of-sample -> random-timing null test -> cost sensitivity -> universe-half robustness
-> pre-registered screen. Writes CSVs (and charts) to strategy_lab/results/.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import data
from .engine import (COSTS, RF, annual_returns, circular_shift_null, extract_trades, holdings, interval_returns, perf,
                     rebalanced_returns, sharpe_of, sleeve_returns, trade_stats)
from .strategies import STRATEGIES

RESULTS = Path(__file__).parent / "results"
EVAL_START = pd.Timestamp("2010-01-01")   # earlier data only warms indicators up
SPLIT = pd.Timestamp("2020-01-01")        # in-sample 2010-2019 | out-of-sample 2020-Oct 2026
N_SIMS = 200

# ---- PRE-REGISTERED SCREEN (fixed before looking at results) -------------------------------------------------
#  1. Sharpe > 0 in BOTH the in-sample and out-of-sample periods
#  2. random-timing null p-value <= 0.10 on the full period  (timing must beat "same exposure, random timing")
#  3. Sharpe at 2x costs >= 70% of Sharpe at 1x costs
#  4. Sharpe > 0 in BOTH random halves of the universe
# Survivors are ranked by out-of-sample Sharpe (tie-break: smaller max drawdown).
# --------------------------------------------------------------------------------------------------------------


def build_world(refresh=False):
    uni, failed = data.load_universe(refresh=refresh, verbose=False)
    bm = data.load_benchmarks(refresh=refresh)
    cal = bm["NIFTY50_PRICE"].index
    P, log = data.clean_panels(data.panels(uni, cal))
    return uni, bm, cal, P, log


def states_for(P, fn) -> np.ndarray:
    cal, syms = P["close"].index, list(P["close"].columns)
    S = np.zeros((len(cal), len(syms)), dtype=np.int8)
    for j, s in enumerate(syms):
        df = pd.DataFrame({k: P[k][s] for k in ("open", "high", "low", "close")}).dropna()
        st = fn(df)
        S[cal.get_indexer(df.index), j] = st
    return S


def slice_perf(r: pd.Series, lo=None, hi=None):
    x = r
    if lo is not None:
        x = x[x.index >= lo]
    if hi is not None:
        x = x[x.index < hi]
    return perf(x)


def main(refresh=False, n_sims=N_SIMS):
    t0 = time.time()
    RESULTS.mkdir(exist_ok=True)
    uni, bm, cal, P, clean_log = build_world(refresh)
    syms = list(P["close"].columns)
    close = P["close"].values
    ret = interval_returns(P["open"]).values
    open_np = P["open"].values
    dates = cal
    ev = np.asarray(dates >= EVAL_START)
    is_mask, oos_mask = ev & np.asarray(dates < SPLIT), np.asarray(dates >= SPLIT)
    N = len(syms)
    halves = [np.arange(0, N, 2), np.arange(1, N, 2)]
    print(f"universe {N} stocks | calendar {dates[0].date()} -> {dates[-1].date()} | eval from {EVAL_START.date()} "
          f"(IS<{SPLIT.date()}<=OOS) | costs buy {COSTS.buy:.4%} sell {COSTS.sell:.4%} | rf {RF:.0%}")
    clean_log.to_csv(RESULTS / "data_cleaning_log.csv", index=False)

    series: dict[str, pd.Series] = {}      # full-length daily returns per strategy
    meta: dict[str, dict] = {}             # family, kind, extras
    rows, trade_rows, nulls = [], {}, {}

    def S_(r):  # to Series
        return pd.Series(r, index=dates)

    # ---- benchmarks -----------------------------------------------------------------------------------------
    S_bh = np.zeros_like(close, dtype=np.int8); S_bh[~np.isnan(close)] = 1
    H_bh = holdings(S_bh, ret)
    series["BUY&HOLD equal-weight universe"] = S_(sleeve_returns(H_bh, ret))
    meta["BUY&HOLD equal-weight universe"] = dict(family="benchmark", kind="bench", exposure=1.0, trades=np.nan)
    nf = bm["NIFTY50_PRICE"]["close"]  # price index (excl. ~1.2-1.5%/yr dividends); ETF data on Yahoo is corrupted
    nb_ret = nf.pct_change().shift(-1).reindex(dates)           # next-day return aligned to interval start
    series["BUY&HOLD Nifty 50 price index"] = nb_ret
    meta["BUY&HOLD Nifty 50 price index"] = dict(family="benchmark", kind="bench", exposure=1.0, trades=np.nan)

    # ---- per-stock signal strategies (sleeve portfolio) --------------------------------------------------------
    states = {}
    for name, (family, fn) in STRATEGIES.items():
        S = states_for(P, fn); states[name] = S
        H = holdings(S, ret)
        r = sleeve_returns(H, ret)
        series[name] = S_(r)
        tr = extract_trades(H, open_np, ret, syms, dates)
        trade_rows[name] = tr
        valid = ~np.isnan(ret)
        meta[name] = dict(family=family, kind="sleeve", exposure=float((H[ev] * valid[ev]).sum() / valid[ev].sum()), H=H, S=S)
        print(f"  built {name:24s} {time.time() - t0:6.1f}s")

    # ---- cross-sectional momentum / low-vol portfolios (monthly, top-N) ----------------------------------------
    c = P["close"]
    mom12 = (c.shift(21) / c.shift(252) - 1).values
    mom6 = (c.shift(21) / c.shift(126) - 1).values
    lowvol = (-c.pct_change(fill_method=None).rolling(252, min_periods=252).std()).values
    nifty = bm["NIFTY50_PRICE"]["close"].reindex(dates)
    regime = (nifty > nifty.rolling(200, min_periods=200).mean()).values
    XS = {
        "xs_momentum_12-1_top10": ("Cross-sectional momentum", mom12, 10, None),
        "xs_momentum_12-1_top20": ("Cross-sectional momentum", mom12, 20, None),
        "xs_momentum_6-1_top10": ("Cross-sectional momentum", mom6, 10, None),
        "xs_momentum_12-1_top10_niftyRegime": ("Cross-sectional momentum", mom12, 10, regime),
        "xs_lowvol_top20": ("Low volatility", lowvol, 20, None),
    }
    for name, (family, score, topn, reg) in XS.items():
        r = rebalanced_returns(ret, score, topn, index=dates, regime=reg)
        series[name] = S_(r)
        meta[name] = dict(family=family, kind="xs", score=score, topn=topn, regime=reg, exposure=np.nan)
        print(f"  built {name:24s} {time.time() - t0:6.1f}s")

    # ---- metrics ---------------------------------------------------------------------------------------------
    for name, r in series.items():
        full, ins, oos = slice_perf(r, EVAL_START), slice_perf(r, EVAL_START, SPLIT), slice_perf(r, SPLIT)
        row = dict(strategy=name, family=meta[name]["family"],
                   cagr=full["cagr"], vol=full["vol"], sharpe=full["sharpe"], sortino=full["sortino"],
                   maxdd=full["maxdd"], calmar=full["calmar"],
                   sharpe_is=ins["sharpe"], cagr_is=ins["cagr"], maxdd_is=ins["maxdd"],
                   sharpe_oos=oos["sharpe"], cagr_oos=oos["cagr"], maxdd_oos=oos["maxdd"], calmar_oos=oos["calmar"])
        if meta[name]["kind"] == "sleeve":
            H = meta[name]["H"]
            ts = trade_stats(trade_rows[name][trade_rows[name].entry >= EVAL_START])
            row.update(exposure=meta[name]["exposure"], **ts)
            row["trades_per_stock_yr"] = ts["trades"] / N / ((dates[-1] - EVAL_START).days / 365.25)
        rows.append(row)
    summ = pd.DataFrame(rows).set_index("strategy")
    # exposure-matched passive benchmark: hold the universe e% of the time (constant mix), rest in cash
    bh_r = series["BUY&HOLD equal-weight universe"].loc[EVAL_START:].dropna()
    for nm in summ.index:
        e = summ.at[nm, "exposure"] if "exposure" in summ.columns else np.nan
        if pd.notna(e):
            m = perf(e * bh_r + (1 - e) * RF / 252)
            summ.at[nm, "matched_bh_cagr"], summ.at[nm, "matched_bh_maxdd"], summ.at[nm, "matched_bh_calmar"] = m["cagr"], m["maxdd"], m["calmar"]

    # ---- null test, cost sensitivity, universe halves ----------------------------------------------------------
    rng_master = np.random.default_rng(11)
    extra = {}
    for name in series:
        k = meta[name]["kind"]
        if k == "bench":
            extra[name] = dict(p_null=np.nan)
            continue
        base_sharpe = sharpe_of(series[name].values[ev])
        if k == "sleeve":
            S, H = meta[name]["S"], meta[name]["H"]
            sims = circular_shift_null(S, ret, n_sims, mask=ev)
            sens = {m: sharpe_of(sleeve_returns(H, ret, COSTS.scaled(m))[ev]) for m in (0, 1, 2, 3)}
            cagr_sens = {m: perf(S_(sleeve_returns(H, ret, COSTS.scaled(m))).loc[EVAL_START:])["cagr"] for m in (0, 2, 3)}
            half = [sharpe_of(sleeve_returns(H, ret, cols=h)[ev]) for h in halves]
        else:
            m_ = meta[name]
            # Null for rank-based books = random N names from the same eligible set each month. Random picking churns
            # ~90%/month vs ~30% for momentum, so compare GROSS (cost-free) Sharpe to isolate selection skill;
            # costs are assessed separately via the cost-sensitivity columns.
            zero = COSTS.scaled(0)
            sims = np.empty(n_sims)
            for i in range(n_sims):
                rr = rebalanced_returns(ret, m_["score"], m_["topn"], zero, index=dates, regime=m_["regime"],
                                        rng=np.random.default_rng(rng_master.integers(1 << 31)))
                sims[i] = sharpe_of(rr[ev])
            sens = {m: sharpe_of(rebalanced_returns(ret, m_["score"], m_["topn"], COSTS.scaled(m), index=dates,
                                                   regime=m_["regime"])[ev]) for m in (0, 1, 2, 3)}
            base_sharpe = sens[0]
            cagr_sens = {m: perf(S_(rebalanced_returns(ret, m_["score"], m_["topn"], COSTS.scaled(m), index=dates,
                                                      regime=m_["regime"])).loc[EVAL_START:])["cagr"] for m in (0, 2, 3)}
            half = [sharpe_of(rebalanced_returns(ret, m_["score"], m_["topn"], index=dates, regime=m_["regime"],
                                                 cols=h)[ev]) for h in halves]
        nulls[name] = sims
        extra[name] = dict(p_null=(1 + (sims >= base_sharpe).sum()) / (1 + len(sims)),
                           null_median=np.median(sims), null_p95=np.percentile(sims, 95),
                           sharpe_cost0=sens[0], sharpe_cost1=sens[1], sharpe_cost2=sens[2], sharpe_cost3=sens[3],
                           cagr_cost0=cagr_sens[0], cagr_cost2=cagr_sens[2], cagr_cost3=cagr_sens[3],
                           sharpe_half_a=half[0], sharpe_half_b=half[1])
        print(f"  tested {name:24s} p={extra[name]['p_null']:.3f}  {time.time() - t0:6.1f}s")
    summ = summ.join(pd.DataFrame(extra).T)

    # ---- pre-registered screen ---------------------------------------------------------------------------------
    s = summ.copy()
    bench = s.family == "benchmark"
    s["c1_is_oos_pos"] = (s.sharpe_is > 0) & (s.sharpe_oos > 0)
    s["c2_beats_random_timing"] = s.p_null <= 0.10
    s["c3_cost_robust"] = s.sharpe_cost2 >= 0.7 * s.sharpe_cost1
    s["c4_both_halves_pos"] = (s.sharpe_half_a > 0) & (s.sharpe_half_b > 0)
    s["passes_screen"] = s[["c1_is_oos_pos", "c2_beats_random_timing", "c3_cost_robust", "c4_both_halves_pos"]].all(axis=1) & ~bench
    bh = s.loc["BUY&HOLD equal-weight universe"]
    s["oos_sharpe_vs_bh"] = s.sharpe_oos - bh.sharpe_oos
    s["oos_calmar_vs_bh"] = s.calmar_oos - bh.calmar_oos
    summ = s

    # ---- family-level walk-forward style selection: pick on IS, judge on OOS ------------------------------------
    sel = []
    for fam, g in summ[~summ.family.isin(["benchmark"])].groupby("family"):
        best = g.sharpe_is.idxmax()
        sel.append(dict(family=fam, variants=len(g), chosen_on_IS=best, sharpe_is=g.loc[best, "sharpe_is"],
                        sharpe_oos=g.loc[best, "sharpe_oos"], cagr_oos=g.loc[best, "cagr_oos"],
                        maxdd_oos=g.loc[best, "maxdd_oos"],
                        oos_vs_family_median=g.loc[best, "sharpe_oos"] - g.sharpe_oos.median()))
    sel = pd.DataFrame(sel).set_index("family")

    yr = pd.DataFrame({k: annual_returns(v.loc[EVAL_START:]) for k, v in series.items()}).T
    yr.columns = [int(c) for c in yr.columns]

    # ---- save ---------------------------------------------------------------------------------------------------
    summ.drop(columns=[c for c in summ.columns if c in ("kind",)], errors="ignore").to_csv(RESULTS / "summary.csv")
    yr.to_csv(RESULTS / "yearly_returns.csv")
    sel.to_csv(RESULTS / "family_selection_is_to_oos.csv")
    pd.DataFrame({k: v.loc[EVAL_START:] for k, v in series.items()}).to_csv(RESULTS / "daily_returns.csv")
    pd.DataFrame(nulls).to_csv(RESULTS / "null_sharpes.csv", index=False)
    allt = pd.concat([t.assign(strategy=k) for k, t in trade_rows.items()])
    allt[allt.entry >= EVAL_START].to_csv(RESULTS / "trades.csv", index=False)
    print(f"\nDONE in {time.time() - t0:.0f}s -> {RESULTS}")
    return summ, yr, sel, series, nulls


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv)
