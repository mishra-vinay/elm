"""Charts for the buyback study:  python -m strategy_lab.buyback_plots"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import buyback_study as bs
from . import data
from .plots import AQUA, BLUE, GRAY, GRID, INK, INK2, ORANGE, SURFACE, style

R = Path(__file__).parent / "results"
RED = "#e34948"


def ci(x, seed=1):
    rng = np.random.default_rng(seed)
    b = np.array([rng.choice(x, len(x)).mean() for _ in range(3000)])
    return np.percentile(b, 2.5), np.percentile(b, 97.5)


def main():
    ok = pd.read_csv(R / "buyback_trades_ok.csv", parse_dates=["rec_date", "entry_date", "approval_ts", "ex_date"])
    px = bs.load_prices(sorted(ok.symbol.unique())); nifty = data.load("^NSEI").close.pct_change()
    cost = bs.COST_RT

    # ---- chart 1: event time around the ex-date (day 0), average excess return vs Nifty -----------------------------
    K = list(range(-25, 6)); rows = []
    for r in ok.itertuples():
        d = px[r.symbol]; e = int(r.ex_pos) if r.ex_pos < len(d) else None
        if e is None:
            continue
        s = d.close.pct_change()
        for k in K:
            i = e + k
            if 1 <= i < len(d):
                dt = d.index[i]
                if dt in nifty.index:
                    rows.append((k, s.iloc[i] - nifty.loc[dt]))
    ev = pd.DataFrame(rows, columns=["k", "x"]).groupby("k").x.agg(["mean", "median", "count"])
    cum = ev["mean"].cumsum()
    fig, ax = plt.subplots(figsize=(11, 5.4), facecolor=SURFACE)
    ax.bar(ev.index, ev["mean"] * 100, color=[RED if v < 0 else BLUE for v in ev["mean"]], width=0.7, alpha=0.9)
    ax.plot([], [], "s", color=BLUE, label="Average daily return vs Nifty 50 (stock minus index)")
    ax2 = ax.twinx()
    ax2.plot(ev.index, cum * 100, color=INK, lw=1.8, label="Cumulative from day -25 (right axis)")
    ax2.set_ylabel("")
    for a in (ax, ax2):
        a.spines["top"].set_visible(False); a.tick_params(colors=INK2, labelsize=9, length=0)
    ax.axvline(0, color=INK2, lw=1, ls="--"); ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID); ax.set_axisbelow(True)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.1f}%"); ax2.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.set_xlabel("Trading days relative to the ex-date (day 0; since T+1 settlement this is also the record date)", color=INK2, fontsize=9)
    d0 = ev.loc[0, "mean"] * 100
    ax.annotate(f"Ex-date: stocks fall {d0:.1f}% vs the index\non average — new buyers can no longer tender", (0, d0), xytext=(-17.5, d0 + 0.2),
                fontsize=9, color=INK, arrowprops=dict(arrowstyle="-", color=INK2))
    style(ax, "Buyback stocks drift up into the last cum-date, then drop on the ex-date",
          f"{len(ok)} NSE tender-offer buybacks, 2016 → 2026. Selling at the close before day 0 avoids the drop.")
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False, fontsize=9, labelcolor=INK)
    fig.subplots_adjust(left=0.07, right=0.93, top=0.84, bottom=0.12)
    fig.savefig(R / "buyback_event_time.png", dpi=150, facecolor=SURFACE); plt.close(fig)

    # ---- chart 2: mean net return per trade with 95% CI -------------------------------------------------------------
    sub = ok[ok.offer_price.notna()]
    items = [("Sell at record-date\nclose (as first\nspecified)", ok.r_rec_close - cost, GRAY),
             ("Sell at close\nbefore ex-date\n(last tender day)", ok.r_cum_close - cost, BLUE),
             ("…same, liquid\n(≥ ₹5 cr/day,\nn=%d)" % (ok.turnover_cr >= 5).sum(), (ok.r_cum_close - cost)[ok.turnover_cr >= 5], BLUE),
             ("…same, very liquid\n(≥ ₹20 cr/day,\nn=%d)" % (ok.turnover_cr >= 20).sum(), (ok.r_cum_close - cost)[ok.turnover_cr >= 20], BLUE),
             ("…same, illiquid\n(< ₹2 cr/day,\nn=%d)" % (ok.turnover_cr < 2).sum(), (ok.r_cum_close - cost)[ok.turnover_cr < 2], "#8aa8d6"),
             ("Hold through\ntender, 15%\naccepted", sub.r_tender_15 - cost, ORANGE), ("Hold through\ntender, 30%\naccepted", sub.r_tender_30 - cost, ORANGE)]
    fig, ax = plt.subplots(figsize=(12, 5.8), facecolor=SURFACE)
    for i, (lab, x, c) in enumerate(items):
        x = x.dropna().values; m = x.mean(); lo, hi = ci(x)
        ax.bar(i, m * 100, color=c, width=0.62)
        ax.plot([i, i], [lo * 100, hi * 100], color=INK, lw=1.6); ax.plot([i - .08, i + .08], [lo * 100] * 2, color=INK, lw=1.6); ax.plot([i - .08, i + .08], [hi * 100] * 2, color=INK, lw=1.6)
        ax.text(i, hi * 100 + 0.8, f"{m*100:+.1f}%", ha="center", fontsize=9.5, fontweight="bold", color=INK)
        ax.text(i, -3.4, f"median {np.median(x)*100:+.1f}%\nwin {(x>0).mean()*100:.0f}%", ha="center", va="top", fontsize=8, color=INK2)
    ax.axhline(0, color=INK2, lw=1)
    ax.set_xticks(range(len(items))); ax.set_xticklabels([i[0] for i in items], fontsize=8.5, color=INK)
    ax.tick_params(axis="x", pad=36)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%"); ax.set_ylim(-4.5, 19)
    style(ax, "Average net return per buyback trade, with 95% confidence interval (after costs)",
          "Buy at the first open after the announcement. Orange bars use the 95 events with a parsed offer price; acceptance ratios are scenarios, not data.")
    ax.grid(axis="y", color=GRID)
    fig.text(0.07, 0.004, "Bars show the mean (dominated by a minority of big winners); the median is far smaller. Pre-tax. Intervals treat events as independent. Not advice.", fontsize=8, color=INK2)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.84, bottom=0.24)
    fig.savefig(R / "buyback_variants.png", dpi=150, facecolor=SURFACE); plt.close(fig)
    ev.to_csv(R / "buyback_event_time.csv"); print("ok")


if __name__ == "__main__":
    main()
