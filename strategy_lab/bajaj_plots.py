"""Chart for the Bajaj Finance study (reads results/bajaj_*.csv)."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

R = Path(__file__).parent / "results"
INK, MUTED, GRID, SURF = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"
BLUE, ORANGE = "#2a78d6", "#eb6834"


def main():
    opp = pd.read_csv(R / "bajaj_opportunities.csv").set_index("window")
    tr = pd.read_csv(R / "bajaj_trades_primary.csv", parse_dates=["entry_time"])
    tr = tr[tr.window == "1Y"].sort_values("entry_time")
    fig, (a, b) = plt.subplots(1, 2, figsize=(13, 5.6), gridspec_kw={"width_ratios": [1, 1.15]}, facecolor=SURF)
    for ax in (a, b):
        ax.set_facecolor(SURF)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(GRID)
        ax.tick_params(colors=MUTED, labelsize=9)

    # left: opportunity funnel for 1Y and 6M, as % of sessions
    steps = [("Trading sessions", "sessions"), ("Price broke prev. high or low", "any_break"),
             ("Break + pullback (trade taken)", "sessions_with_setup"), ("Trades that hit the +2% target", "target_hits")]
    ys = list(range(len(steps)))[::-1]
    h = 0.34
    for off, per, col in ((h / 2 + 0.02, "1Y", BLUE), (-h / 2 - 0.02, "6M", ORANGE)):
        n = opp.loc[per, "sessions"]
        for y, (_, key) in zip(ys, steps):
            v = opp.loc[per, key]
            a.barh(y + off, v / n * 100, height=h, color=col)
            a.text(v / n * 100 + 1.5, y + off, f"{v:.0f}  ({v / n * 100:.0f}%)", va="center", fontsize=9, color=INK)
    a.set_yticks(ys)
    a.set_yticklabels([s for s, _ in steps], color=INK, fontsize=10)
    a.set_xlim(0, 125)
    a.set_xlabel("% of trading sessions", color=MUTED)
    a.set_xticks([0, 25, 50, 75, 100])
    a.grid(axis="x", color=GRID, lw=0.8)
    a.set_axisbelow(True)
    a.set_title("How often the setup appears", loc="left", color=INK, fontsize=12, fontweight="bold")
    a.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=BLUE), plt.Rectangle((0, 0), 1, 1, color=ORANGE)],
             labels=[f"Last 1 year ({opp.loc['1Y','sessions']} sessions)", f"Last 6 months ({opp.loc['6M','sessions']} sessions)"],
             loc="lower right", frameon=False, fontsize=9, labelcolor=INK)

    # right: cumulative modelled option P&L per lot, buys vs sells
    for side, col in (("BUY", BLUE), ("SELL", ORANGE)):
        g = tr[tr.side == side]
        cum = (g.ret_net * 200000).cumsum() / 1000
        b.step(g.entry_time, cum, where="post", color=col, lw=2)
        b.text(g.entry_time.iloc[-1], cum.iloc[-1], f"  {'Long setups' if side=='BUY' else 'Short setups'}: ₹{cum.iloc[-1]:,.0f}k",
               color=INK, fontsize=9.5, va="center")
    allc = (tr.ret_net * 200000).cumsum() / 1000
    b.step(tr.entry_time, allc, where="post", color=MUTED, lw=1.4, ls="--")
    b.text(tr.entry_time.iloc[-1], allc.iloc[-1], f"  Total: ₹{allc.iloc[-1]:,.0f}k", color=INK, fontsize=9.5, va="center", fontweight="bold")
    b.axhline(0, color=MUTED, lw=0.8)
    b.set_ylabel("Cumulative P&L, ₹ thousand, ₹2 lakh per trade", color=MUTED)
    b.grid(axis="y", color=GRID, lw=0.8)
    b.set_axisbelow(True)
    b.set_xlim(tr.entry_time.min(), tr.entry_time.max() + pd.Timedelta(days=95))
    b.set_title("Bajaj Finance shares, primary spec, last 1 year", loc="left", color=INK, fontsize=12, fontweight="bold")
    fig.text(0.01, 0.01, "BAJFINANCE.NS hourly bars (Yahoo), intraday long and short, flat by the close. Cost 0.10% round trip. "
             "Rule: p=1% pullback, +2% target, 1% stop.", fontsize=8.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(R / "bajaj_summary.png", dpi=150, facecolor=SURF)


if __name__ == "__main__":
    main()
