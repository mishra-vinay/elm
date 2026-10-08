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


if __name__ == "__main__":
    main()
