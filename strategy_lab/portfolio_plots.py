"""Charts + consolidated table for step 1 (realistic portfolio):  python -m strategy_lab.portfolio_plots"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import research
from .engine import COSTS, interval_returns, perf, rebalanced_returns
from .indicators import adx
from .plots import AQUA, BLUE, GRAY, GRID, INK, INK2, ORANGE, SURFACE, style
from .portfolio import simulate
from .portfolio_study import evaluate, panel_from
from .strategies import STRATEGIES

R = Path(__file__).parent / "results"
KS = (10, 15, 20, 30, 40, 50)


def main():
    uni, bm, cal, P, _ = research.build_world()
    S = research.states_for(P, STRATEGIES["ema_cross_20_50_adx"][1])
    start = int(np.searchsorted(cal.values, np.datetime64(research.EVAL_START)))
    oadj, ratio, sc = P["open"].values, (P["raw_close"] / P["close"]).values, panel_from(P, adx)
    ret = interval_returns(P["open"]).values
    ones = np.where(np.isnan(P["close"].values), np.nan, 1.0)
    rows, curves = [], {}
    for K in KS:
        eq, ex, npos, st, tr = simulate(oadj, ratio, S, sc, K, 1_000_000, start)
        full, ins, oos, ser = evaluate(eq, cal)
        rr = []
        for sd in range(20):
            r = pd.Series(rebalanced_returns(ret, ones, K, COSTS, index=cal, rng=np.random.default_rng(sd)), index=cal)
            p = perf(r[r.index >= research.EVAL_START].dropna()); rr.append((p["sharpe"], p["maxdd"], p["cagr"]))
        a = np.array(rr)
        rows.append(dict(K=K, trend_cagr=full["cagr"], trend_sharpe=full["sharpe"], trend_maxdd=full["maxdd"], trend_calmar=full["calmar"],
                         trend_sharpe_oos=oos["sharpe"], invested=float(ex[start:].mean()), trades=st["exits"], skipped_price=st["skipped_price"],
                         passive_cagr=a[:, 2].mean(), passive_sharpe=a[:, 0].mean(), passive_maxdd=a[:, 1].mean()))
        curves[K] = ser
    t = pd.DataFrame(rows); t.to_csv(R / "portfolio_by_slots.csv", index=False)
    d = pd.read_csv(R / "daily_returns.csv", index_col=0, parse_dates=True).fillna(0)
    bh, sleeve = d["BUY&HOLD equal-weight universe"], d["ema_cross_20_50_adx"]
    bhp, slp = perf(bh), perf(sleeve)

    # ---- chart 1: worst drawdown (depth) and Sharpe by number of slots ----------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.6), facecolor=SURFACE)
    x = np.arange(len(KS)); w = 0.36
    panels = [("trend_maxdd", "passive_maxdd", -bhp["maxdd"], -slp["maxdd"], -1, "Worst drawdown (% fall from peak)", "Smaller is safer",
               lambda v: f"{v*100:.0f}%", 1.0, 0.46),
              ("trend_sharpe", "passive_sharpe", bhp["sharpe"], slp["sharpe"], 1, "Sharpe ratio", "Higher is better",
               lambda v: f"{v:.2f}", 1.0, 1.05)]
    for ax, (tc, pc, ref_b, ref_s, sign, ttl, sub, fmt, mult, ylim) in zip(axes, panels):
        tv, pv = t[tc] * sign, t[pc] * sign
        ax.bar(x + w / 2, tv, w - 0.04, color=BLUE)
        ax.bar(x - w / 2, pv, w - 0.04, color=GRAY)
        ax.axhline(ref_b, color=INK2, lw=1.1, ls="--"); ax.axhline(ref_s, color=AQUA, lw=1.6, ls="--")
        for xi, v in zip(x + w / 2, tv): ax.text(xi, v * 0.03 + 0.002, fmt(v), ha="center", va="bottom", fontsize=8.5, color="white", fontweight="bold")
        for xi, v in zip(x - w / 2, pv): ax.text(xi, v * 0.03 + 0.002, fmt(v), ha="center", va="bottom", fontsize=8.5, color=INK, fontweight="bold")
        ax.set_xticks(x); ax.set_xticklabels([f"{k} slots" for k in KS], fontsize=9, color=INK)
        ax.set_ylim(0, ylim); style(ax, ttl, sub)
    axes[0].yaxis.set_major_formatter(lambda v, _: f"{v*100:.0f}%")
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    handles = [Patch(color=BLUE, label="EMA 20/50 + ADX portfolio, real constraints"),
               Patch(color=GRAY, label="Buy & hold of the same number of random stocks"),
               Line2D([0], [0], color=INK2, ls="--", label=f"All 102 stocks, buy & hold (drawdown {-bhp['maxdd']*100:.0f}%, Sharpe {bhp['sharpe']:.2f})"),
               Line2D([0], [0], color=AQUA, ls="--", lw=1.6, label=f"Idealised 102-stock version (drawdown {-slp['maxdd']*100:.0f}%, Sharpe {slp['sharpe']:.2f})")]
    fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False, fontsize=9, labelcolor=INK, bbox_to_anchor=(0.5, 0.045))
    fig.suptitle("Step 1: with real-world limits (₹10 lakh, whole shares, N positions), how much does the trend filter still protect?",
                 x=0.06, ha="left", fontsize=12.5, fontweight="bold", color=INK)
    fig.text(0.06, 0.012, "2010 → Oct 2026, after costs (incl. flat ₹16 DP charge per sale). Survivorship-biased universe; compare blue vs grey. "
             "Slots 30-50 are an exploratory, not pre-registered, extension.", fontsize=8, color=INK2)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.76, bottom=0.2, wspace=0.16)
    fig.savefig(R / "portfolio_by_slots.png", dpi=150, facecolor=SURFACE); plt.close(fig)

    # ---- chart 2: growth + drawdown ------------------------------------------------------------------------------
    lines = [("All 102 stocks, buy & hold", bh, GRAY, 1.5), ("Idealised: 1/102 of capital per stock", sleeve, AQUA, 1.3),
             ("Real: 15 positions", curves[15].reindex(bh.index).fillna(0), ORANGE, 1.6), ("Real: 50 positions", curves[50].reindex(bh.index).fillna(0), BLUE, 1.8)]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 8), facecolor=SURFACE, gridspec_kw=dict(height_ratios=[1.35, 1], hspace=0.3))
    for lab, r, c, lw in lines:
        r = r[r.index >= research.EVAL_START]; e = (1 + r).cumprod(); dd = e / e.cummax() - 1
        a1.plot(e.index, e, color=c, lw=lw, label=lab); a2.plot(dd.index, dd * 100, color=c, lw=lw)
        a1.annotate(f"{e.iloc[-1]:.1f}×", (e.index[-1], e.iloc[-1]), xytext=(6, 0), textcoords="offset points", fontsize=9, color=INK, va="center")
    a1.set_yscale("log"); a1.set_yticks([1, 2, 5, 10, 20]); a1.set_yticklabels(["1×", "2×", "5×", "10×", "20×"])
    style(a1, "Growth of ₹1 and drawdowns, 2010 → Oct 2026 (log scale, after costs)", "Whole-share ₹10 lakh account for the two 'Real' lines.")
    a1.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK); a1.set_xlim(a1.get_xlim()[0], a1.get_xlim()[1])
    a2.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%"); style(a2, "", ""); a2.set_title("")
    a2.text(0, 1.03, "Drawdown from previous peak", transform=a2.transAxes, color=INK2, fontsize=9)
    fig.subplots_adjust(left=0.07, right=0.93, top=0.9, bottom=0.05)
    fig.savefig(R / "portfolio_equity_drawdown.png", dpi=150, facecolor=SURFACE); plt.close(fig)
    pd.set_option("display.width", 220)
    print(t.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
