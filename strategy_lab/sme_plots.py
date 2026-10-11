"""Chart for the SME upper-circuit study:  python -m strategy_lab.sme_plots"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .plots import AQUA, BLUE, GRAY, GRID, INK, INK2, ORANGE, SURFACE, style

R = Path(__file__).parent / "results"
RED = "#e34948"


def main():
    t = pd.read_csv(R / "sme_touch_uc.csv", parse_dates=["date"]); cost = 0.010
    closed = t[t.closed_uc].ret_uc_price.dropna() - cost; off = t[~t.closed_uc].ret_uc_price.dropna() - cost
    allt = t.ret_uc_price.dropna() - cost
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12.5, 5.6), facecolor=SURFACE, gridspec_kw=dict(width_ratios=[1, 1.5], wspace=0.22))
    # panel 1: what happens to stocks that touch the upper circuit
    n = len(t); c = t.closed_uc.mean()
    a1.bar([0], [c * 100], color=AQUA, width=0.6); a1.bar([0], [(1 - c) * 100], bottom=[c * 100], color=RED, width=0.6)
    a1.text(0, c * 50, f"{c:.0%}\nclosed at the\nupper circuit", ha="center", va="center", color="white", fontsize=10.5, fontweight="bold")
    a1.text(0, c * 100 + (1 - c) * 50, f"{1-c:.0%}\ncame off it", ha="center", va="center", color="white", fontsize=10.5, fontweight="bold")
    a1.set_xlim(-0.6, 0.6); a1.set_xticks([]); a1.set_ylim(0, 100); a1.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    style(a1, "Stocks that touch the upper circuit", f"{n:,} SME stock-days, 2024 → Oct 2026")
    # panel 2: average net return, next-open exit
    items = [("Closed at UC\n(hindsight,\nnot tradable)", closed.mean(), closed, AQUA),
             ("Came off UC,\nclosed lower", off.mean(), off, RED),
             ("All that touched\nUC (what you\ncould buy)", allt.mean(), allt, BLUE)]
    for f in (0.5, 0.25):
        w = f * len(closed); m = (closed.mean() * w + off.mean() * len(off)) / (w + len(off))
        items.append((f"Only {f:.0%} of the\ngood orders\nget filled", m, None, ORANGE))
    for i, (lab, mean, s, col) in enumerate(items):
        a2.bar(i, mean * 100, color=col, width=0.62)
        a2.text(i, mean * 100 + (0.25 if mean >= 0 else -0.25), f"{mean*100:+.2f}%", ha="center", va="bottom" if mean >= 0 else "top", fontsize=10, fontweight="bold", color=INK)
        if s is not None:
            a2.text(i, -5.7, f"win {np.mean(s>0):.0%}\n5th pct {s.quantile(.05)*100:+.0f}%", ha="center", va="top", fontsize=8, color=INK2)
    a2.axhline(0, color=INK2, lw=1); a2.set_xticks(range(len(items))); a2.set_xticklabels([i[0] for i in items], fontsize=8, color=INK)
    a2.tick_params(axis="x", pad=40); a2.set_ylim(-5.2, 3.2); a2.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    style(a2, "Buy at UC price, sell next open: average net return",
          "After 1.0% round-trip costs")
    fig.text(0.05, 0.035, "You cannot know at 2 pm whether a stock will close at the circuit. Orders at the circuit join a queue of buyers; fills are not guaranteed.", fontsize=8, color=INK2)
    fig.text(0.05, 0.012, "Exit: next open (or the open after, if the stock hit the lower circuit). Back-test only, not advice.", fontsize=8, color=INK2)
    fig.subplots_adjust(left=0.06, right=0.98, top=0.82, bottom=0.27)
    fig.savefig(R / "sme_uc_summary.png", dpi=150, facecolor=SURFACE); plt.close(fig); print("ok")


if __name__ == "__main__":
    main()
