# strategy_lab — equity strategy research, backtesting and scanning

A small research toolkit (no UI, no notifications): download adjusted NSE daily prices, backtest long-only
equity strategies with realistic next-open execution and Indian delivery costs, validate them, and scan today's
market for stocks currently matching each strategy.

Findings and recommended process: [`docs/equity-strategy-research.md`](../docs/equity-strategy-research.md).

```
pip install -r strategy_lab/requirements.txt
python -m unittest strategy_lab.tests.test_engine -v   # alignment, costs, no-look-ahead
python -m strategy_lab.research                        # full study, writes results/*.csv (~100 s)
python -m strategy_lab.index_check                     # Sensex 1997-2026 survivorship-free check
python -m strategy_lab.plots                           # results/*.png
python -m strategy_lab.scan [--no-refresh]             # today's signals -> results/scan_<date>_*.csv
```

| File | Purpose |
|---|---|
| `universe.py` | The 102-stock research universe (survivorship-biased; see report §7) |
| `data.py` | Yahoo chart-API download, CSV cache, calendar alignment, corporate-action/bad-print cleaning |
| `indicators.py` | EMA/SMA/ATR/RSI/ADX/SuperTrend (every value uses data up to the current bar only) |
| `strategies.py` | Per-stock 0/1 position-state functions, one registry entry per variant |
| `engine.py` | Sleeve and monthly-rebalanced portfolio engines, costs, metrics, random-timing null test |
| `research.py` | The full pipeline and the pre-registered screen |
| `scan.py` | Live scanner: new entries / holds / exits on the last completed bar, plus factor rankings |
| `tests/` | Alignment, cost-arithmetic and causality tests |

**Adding a strategy:** write a function returning a 0/1 numpy array (1 = want to be long after this bar's
close; use only data up to that bar), add it to `STRATEGIES`, and run the tests — the causality test will fail
if it peeks at the future. Count it as another trial when judging results.

Conventions: signal at close *t*, trade at open *t+1*; open-to-open total-return prices; idle cash earns 6%;
costs ≈ 0.34% round trip (see `engine.Costs`). The cache (`.cache/`) is git-ignored; prices come from an
unofficial free source — replace with broker/vendor data before live use.
