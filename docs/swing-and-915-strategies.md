# Swing trading and the "9:15 high-volume" idea

*Run on 9 Oct 2026. Code: [`volume_swing.py`](../strategy_lab/volume_swing.py), [`orb_intraday.py`](../strategy_lab/orb_intraday.py). Not investment advice.*

## 1. Short answers

* **Swing trading:** yes — it is what most of the earlier research covers ([main report](equity-strategy-research.md)). The best-supported swing strategy remains the **EMA 20/50 + ADX trend filter**, with the realistic-portfolio caveats in [step 1](equity-step1-realistic-portfolio.md). Adding a **volume filter to a breakout does not help** (§2).
* **9:15 high-volume strategy:** **it cannot be tested properly with the free data I can reach**, so I don't have a verified result for it. A price-only version of the 9:15 breakout loses money after costs on the 48 sessions available (§3). Testing the real idea needs broker or vendor intraday history that includes opening-bar volume.

## 2. Swing: does a volume filter improve a breakout?

Same engine, costs, in-sample/out-of-sample split and screen as the main study, paired with the plain Donchian rules already tested (2010 → Oct 2026, 102 large caps). These are trials 20 and 21; the main study tested 19.

| Strategy | CAGR | Sharpe | Worst drawdown | % invested | Win rate | Avg net trade | Random-timing p | Sharpe at 2× costs |
|---|---|---|---|---|---|---|---|---|
| `vol_breakout_20_10` | 9.4% | 0.63 | -5.5% | 29% | 42% | 2.21% | 0.93 | 0.48 |
| `vol_breakout_55_20` | 10.6% | 0.77 | -6.2% | 31% | 45% | 5.29% | 0.78 | 0.70 |

Reference: buy-and-hold of the same stocks 21.4% / Sharpe 0.89 / worst drawdown -36.5%.

| Pair | Sharpe with vs without the volume filter | Random-timing p (with vs without) |
|---|---|---|
| 20-day breakout, 10-day exit | 0.63 vs 0.64 | 0.93 vs 0.95 |
| 55-day breakout, 20-day exit | 0.77 vs 0.71 | 0.78 vs 0.93 |

The volume filter trims exposure (about 30% invested instead of 41–44%) and shaves the drawdown, but **risk-adjusted performance is the same within noise and both versions fail the random-timing test**: their results are explained by being invested part of the time in a rising market, not by the breakout signal. Volume confirmation is not a reason to prefer these over the trend filter.

## 3. The 9:15 idea

### Why the volume part cannot be tested here
Yahoo Finance's 5-minute and 1-hour data give the **09:15 bar a volume of zero on 77–87% of days** (RELIANCE, HDFCBANK, INFY, KOTAKBANK checked), and summed intraday volume is only 93–97% of the true daily volume — the opening-auction volume is missing. Free 5-minute history is also limited to the last ~60 sessions. Any "9:15 high volume" result from this data would be fiction, so I did not produce one.

### What I could test: the price-only mechanics
Rules fixed before running: the 09:15 candle sets the range; long if it is green, short if red; enter on the first 5-minute close beyond the candle's high/low (by 11:00), filling at the next bar's open; stop at the other end of the candle; target 1.5× risk; exit by 15:15; 0.20% round-trip costs. "High activity" is the opening candle's range ≥ 1.5× its own 20-session average — a stand-in for volume, not the same thing.

| Group | Trades | Win rate | Gross per trade | Net per trade (after 0.20% costs) | 95% interval (by day) | Avg R |
|---|---|---|---|---|---|---|
| ALL days | 2415 | 41% | 0.05% | -0.15% | -0.23% to -0.07% | +0.04 |
| HIGH activity (range >= 1.5x avg) | 295 | 46% | 0.18% | -0.02% | -0.25% to 0.21% | +0.10 |
| LOW activity (range < 1.0x avg) | 1494 | 40% | 0.02% | -0.18% | -0.22% to -0.12% | +0.02 |
| HIGH activity, longs only | 121 | 42% | 0.06% | -0.14% | -0.45% to 0.20% | +0.01 |
| HIGH activity, shorts only | 174 | 49% | 0.26% | 0.06% | -0.27% to 0.41% | +0.16 |
| VERY HIGH activity (>= 3x) | 18 | 44% | 0.35% | 0.15% | -0.55% to 1.01% | +0.12 |

Window: 31 Jul → 8 Oct 2026, 48 sessions, 102 stocks, a period when the Nifty 50 fell about 8.8%.

* **Without a filter the breakout loses money**: about +0.05% per trade before costs, -0.16% after them (interval -0.23% to -0.07%).
* **The activity filter improves things but does not make it profitable**: 295 trades, about -0.02% net per trade, and the interval (-0.25% to +0.21%) covers zero.
* **Shorts beat longs, but this was a falling market** and the samples are small (174 and 121 trades).
* **Caveats:** one regime, 48 market days (trades on the same day are correlated, so I bootstrapped by day), no volume data, 5-minute fills assumed at the next bar's open, and a flat 0.20% cost.

## 4. What this means

1. **Costs decide intraday breakouts.** Gross edge per trade (+0.05% to +0.18%) is smaller than a realistic 0.20% round-trip cost. A real volume signal would have to add well over 0.15% per trade to change that.
2. **Don't automate a 9:15 strategy yet.** First get proper data: broker historical intraday APIs (for example Dhan's Data API or Zerodha's historical API) with volume on the opening bar, or a licensed vendor. Then test 3–5 years across regimes with the same discipline: pre-registered rules, a low-activity placebo, cost stress, and a day-clustered interval.
3. **Swing remains the better-supported path** for automation: fewer trades, daily data, no latency requirement, and costs matter far less.

## 5. Reproduce
```
python -m strategy_lab.volume_swing      # swing volume-breakout test (~2 min)
python -m strategy_lab.orb_intraday      # 9:15 price-only test (downloads 5-minute data once)
```
Results: `strategy_lab/results/volume_swing_results.csv`, `orb_results.csv`, `orb_trades.csv`.
