"""Charts for the study:  python -m strategy_lab.plots   (reads results/*.csv, writes results/*.png)"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

R = Path(__file__).parent / "results"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
BLUE, ORANGE, AQUA, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#8a8984"   # slots 1-3 validated all-pairs; gray = benchmark

LINES = [  # (column, label, colour, width)
    ("BUY&HOLD equal-weight universe", "Buy & hold (same stocks)", GRAY, 1.6),
    ("ema_cross_20_50_adx", "EMA 20/50 cross + ADX>20", BLUE, 1.8),
    ("xs_momentum_12-1_top20", "Momentum 12-1, top 20", ORANGE, 1.8),
    ("xs_lowvol_top20", "Low-vol, top 20", AQUA, 1.8),
]


def style(ax, title, sub):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    ax.set_title(title, loc="left", color=INK, fontsize=13, fontweight="bold", pad=24)
    ax.text(0, 1.04, sub, transform=ax.transAxes, color=INK2, fontsize=9)


def label_ends(ax, series, fmt):
    ys = sorted([(v.iloc[-1], lab, c) for v, lab, c in series])
    last_y = None
    for y, lab, c in ys:                                    # nudge apart so direct labels never collide
        yy = y if last_y is None else max(y, last_y * 1.08) if ax.get_yscale() == "log" else max(y, last_y + 0.035)
        ax.annotate(f"{lab}  {fmt(y)}", (ax.get_xlim()[1], yy), xytext=(6, 0), textcoords="offset points",
                    color=INK, fontsize=9, va="center", annotation_clip=False)
        ax.plot([], [], color=c)
        last_y = yy


def main():
    d = pd.read_csv(R / "daily_returns.csv", index_col=0, parse_dates=True).fillna(0)
    eq = (1 + d).cumprod()
    dd = eq / eq.cummax() - 1

    fig, ax = plt.subplots(figsize=(11, 5.6), facecolor=SURFACE)
    ser = []
    for col, lab, c, w in LINES:
        ax.plot(eq.index, eq[col], color=c, lw=w, label=lab)
        ser.append((eq[col], lab, c))
    ax.set_yscale("log"); ax.set_xlim(eq.index[0], eq.index[-1])
    ax.set_yticks([1, 2, 5, 10, 20, 40]); ax.set_yticklabels(["1×", "2×", "5×", "10×", "20×", "40×"])
    style(ax, "Growth of ₹1, 2010 → Oct 2026 (log scale, after costs)",
          "102 large-cap NSE stocks (survivorship-biased — compare lines to each other, not to the index). Idle cash earns 6%.")
    ax.axvline(pd.Timestamp("2020-01-01"), color=INK2, lw=0.8, ls=":")
    ax.text(pd.Timestamp("2020-02-15"), 1.15, "out-of-sample →", color=INK2, fontsize=8)
    label_ends(ax, ser, lambda y: f"{y:.1f}×")
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK)
    fig.subplots_adjust(right=0.74, left=0.07, top=0.84, bottom=0.08)
    fig.savefig(R / "equity_curves.png", dpi=150, facecolor=SURFACE); plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.6), facecolor=SURFACE)
    ser = []
    for col, lab, c, w in LINES:
        ax.plot(dd.index, dd[col] * 100, color=c, lw=w, label=lab)
        ser.append((dd[col] * 100, lab, c))
    ax.set_xlim(dd.index[0], dd.index[-1]); ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    style(ax, "Drawdown from previous peak", "How far each approach fell from its high-water mark — the trend filter's main benefit.")
    ys = sorted([(v.min(), lab) for v, lab, c in ser])
    ax.legend(loc="lower left", frameon=False, fontsize=9, labelcolor=INK, ncol=2)
    for v, lab, c in ser:
        i = v.idxmin(); ax.annotate(f"{v.min():.0f}%", (i, v.min()), xytext=(0, -11), textcoords="offset points", color=INK, fontsize=8, ha="center")
        ax.plot([i], [v.min()], "o", ms=5, color=c, mec=SURFACE, mew=1.5)
    fig.subplots_adjust(left=0.07, right=0.97, top=0.82, bottom=0.08)
    fig.savefig(R / "drawdowns.png", dpi=150, facecolor=SURFACE); plt.close(fig)

    s = pd.read_csv(R / "summary.csv", index_col=0)
    t = s[s.matched_bh_calmar.notna()].sort_values("calmar")
    fig, ax = plt.subplots(figsize=(11, 6.2), facecolor=SURFACE)
    y = np.arange(len(t)); h = 0.34
    ax.barh(y + h / 2, t.calmar, height=h - 0.04, color=BLUE, label="Strategy")
    ax.barh(y - h / 2, t.matched_bh_calmar, height=h - 0.04, color=GRAY, label="Buy & hold held for the same % of time")
    ax.set_yticks(y); ax.set_yticklabels(t.index, fontsize=9, color=INK)
    for i, (a, b) in enumerate(zip(t.calmar, t.matched_bh_calmar)):
        ax.text(a + 0.03, i + h / 2, f"{a:.2f}", va="center", fontsize=8, color=INK)
        ax.text(b + 0.03, i - h / 2, f"{b:.2f}", va="center", fontsize=8, color=INK2)
    ax.grid(axis="x", color=GRID, lw=0.8); ax.grid(axis="y", visible=False); ax.set_axisbelow(True)
    style(ax, "Return per unit of worst drawdown (Calmar), strategy vs. just holding for the same share of time",
          "Per-stock rules, 2010–2026, after costs. Right of the grey bar = the timing rule protected capital better than simply holding less.")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right", frameon=False, fontsize=9, labelcolor=INK)
    fig.subplots_adjust(left=0.2, right=0.97, top=0.84, bottom=0.07)
    fig.savefig(R / "calmar_vs_matched.png", dpi=150, facecolor=SURFACE); plt.close(fig)
    print("wrote equity_curves.png, drawdowns.png, calmar_vs_matched.png")


def strategy_card(symbol="KOTAKBANK", start="2024-07-01"):
    """One-page picture of the recommended strategy: the rule on a real stock + how it scored vs buy & hold."""
    from . import data
    from .indicators import adx, ema
    from .strategies import STRATEGIES
    df = data.load(symbol)
    df = df[["open", "high", "low", "close", "raw_close"]]
    st = pd.Series(STRATEGIES["ema_cross_20_50_adx"][1](df[["open", "high", "low", "close"]]), index=df.index)
    f20, f50, a = ema(df.close, 20), ema(df.close, 50), adx(df)
    w = df.index >= start
    d, price = df.index[w], df.raw_close[w]
    scale = (df.raw_close / df.close)[w]          # draw the EMAs on the traded-price scale
    fig = plt.figure(figsize=(12, 9), facecolor=SURFACE)
    gs = fig.add_gridspec(3, 2, height_ratios=[3.2, 1, 1.5], width_ratios=[3, 1.05], hspace=0.38, wspace=0.06)
    ax = fig.add_subplot(gs[0, 0]); ax2 = fig.add_subplot(gs[1, 0], sharex=ax); axb = fig.add_subplot(gs[2, :])
    # in-position shading
    inpos = st[w].values.astype(bool)
    ax.fill_between(d, 0, 1, where=inpos, transform=ax.get_xaxis_transform(), color=BLUE, alpha=0.10, lw=0)
    ax2.fill_between(d, 0, 1, where=inpos, transform=ax2.get_xaxis_transform(), color=BLUE, alpha=0.10, lw=0)
    ax.plot(d, price, color=INK, lw=1.2)
    ax.plot(d, (f20 * (df.raw_close / df.close))[w], color=BLUE, lw=1.6)
    ax.plot(d, (f50 * (df.raw_close / df.close))[w], color=ORANGE, lw=1.6)
    chg = st[w].diff().fillna(0)
    for dt in chg.index[chg == 1]:
        ax.plot([dt], [price[dt]], marker="^", ms=9, color=AQUA, mec=SURFACE, mew=1.5, zorder=5)
    for dt in chg.index[chg == -1]:
        ax.plot([dt], [price[dt]], marker="v", ms=9, color="#e34948", mec=SURFACE, mew=1.5, zorder=5)
    lo, hi = price.min(), price.max(); ax.set_ylim(lo * 0.94, hi * 1.06)
    ax.set_xlim(d[0], d[-1] + pd.Timedelta(days=5))
    ax.plot([], [], color=INK, lw=1.2, label=f"{symbol} price")
    ax.plot([], [], color=BLUE, lw=1.6, label="EMA 20 (fast)")
    ax.plot([], [], color=ORANGE, lw=1.6, label="EMA 50 (slow)")
    ax.plot([], [], "^", color=AQUA, ms=8, label="Buy signal (trade next open)")
    ax.plot([], [], "v", color="#e34948", ms=8, label="Exit signal (trade next open)")
    ax.fill_between([], [], color=BLUE, alpha=0.10, label="Holding the stock")
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK, ncol=2)
    ax.set_yticks(ax.get_yticks()); ax.yaxis.set_major_formatter(lambda v, _: f"₹{v:,.0f}")
    style(ax, "EMA 20/50 + ADX trend filter — how the rule trades a real stock",
          f"{symbol}, daily bars, Jul 2024 → 8 Oct 2026. Buy when EMA20 > EMA50 and ADX > 20; sell when EMA20 closes below EMA50.")
    plt.setp(ax.get_xticklabels(), visible=False)
    ax2.plot(d, a[w], color=INK2, lw=1.3)
    ax2.axhline(20, color=INK2, lw=0.9, ls="--")
    ax2.text(0.0, 1.05, "ADX measures trend strength. A new trade starts only above the dashed line (20); the exit ignores ADX.", transform=ax2.transAxes, color=INK2, fontsize=8.5)
    ax2.set_ylim(0, max(45, a[w].max() * 1.1)); ax2.set_ylabel("ADX", color=INK2, fontsize=9)
    style(ax2, "", ""); ax2.set_title("")
    ax2.set_xlim(d[0], d[-1] + pd.Timedelta(days=5))

    # rules / facts panel
    axr = fig.add_subplot(gs[0:2, 1]); axr.axis("off")
    rules = ("THE RULE (checked once a day, after the close)\n\n"
             "BUY   EMA20 > EMA50  and  ADX > 20\n"
             "SELL  EMA20 closes below EMA50\n\n"
             "Orders go in for the NEXT open.\n"
             "No price target, no intraday stops.\n\n"
             "Backtest: 102 NSE large caps,\n2010 → Oct 2026, after ~0.34%\nround-trip costs.\n\n"
             "About 1.7 trades per stock per year;\naverage trade lasts ~74 days.\n"
             "Wins 42% of trades, but the\naverage win is 4.4× the average loss.")
    axr.text(0, 1, rules, va="top", ha="left", fontsize=9.5, color=INK, linespacing=1.35)

    # scorecard: strategy vs buy & hold
    sm = pd.read_csv(R / "summary.csv", index_col=0)
    a_, b_ = sm.loc["ema_cross_20_50_adx"], sm.loc["BUY&HOLD equal-weight universe"]
    yr = pd.read_csv(R / "yearly_returns.csv", index_col=0)
    tiles = [("Worst drawdown", f"{a_.maxdd*100:.1f}%", f"{b_.maxdd*100:.1f}%", "smaller is safer"),
             ("Worst calendar year", f"{yr.loc['ema_cross_20_50_adx'].min()*100:.0f}%", f"{yr.loc['BUY&HOLD equal-weight universe'].min()*100:.0f}%", "2011 in both"),
             ("Sharpe ratio", f"{a_.sharpe:.2f}", f"{b_.sharpe:.2f}", "about the same"),
             ("Return per year", f"{a_.cagr*100:.1f}%", f"{b_.cagr*100:.1f}%", "the price of safety"),
             ("Money in stocks", f"{a_.exposure*100:.0f}%", "100%", "rest earns cash yield")]
    axb.axis("off"); axb.set_xlim(0, 5); axb.set_ylim(0, 1)
    axb.text(0, 1.02, "Scorecard — same 102 stocks, 2010 → Oct 2026, after costs", fontsize=11, fontweight="bold", color=INK, va="bottom")
    for i, (name, v1, v2, note) in enumerate(tiles):
        x = i + 0.03
        axb.add_patch(plt.Rectangle((i + 0.02, 0.02), 0.94, 0.9, fc="#f3f2ee", ec=GRID, lw=1))
        axb.text(x + 0.05, 0.80, name, fontsize=9, color=INK2, va="center")
        axb.text(x + 0.05, 0.52, v1, fontsize=20, fontweight="bold", color=BLUE, va="center")
        axb.text(x + 0.05, 0.27, f"Buy & hold: {v2}", fontsize=9, color=INK2, va="center")
        axb.text(x + 0.05, 0.10, note, fontsize=8, color=INK2, va="center", style="italic")
    fig.text(0.06, 0.012, "Survivorship-biased universe (today's large caps) — compare the two numbers in each tile, not the absolute values. "
             "Back-test only, not a forecast or advice.", fontsize=8, color=INK2)
    fig.subplots_adjust(left=0.07, right=0.97, top=0.9, bottom=0.05)
    fig.savefig(R / "strategy_ema_20_50_adx.png", dpi=150, facecolor=SURFACE); plt.close(fig)
    print("wrote strategy_ema_20_50_adx.png")


if __name__ == "__main__":
    main()
    strategy_card()
