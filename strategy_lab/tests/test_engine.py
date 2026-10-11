"""Run:  python -m unittest strategy_lab.tests.test_engine -v"""
import unittest

import numpy as np
import pandas as pd

from strategy_lab import data
from strategy_lab.engine import Costs, extract_trades, holdings, interval_returns, rebalanced_returns, sleeve_returns
from strategy_lab.strategies import STRATEGIES

ZERO = Costs(0.0, 0.0)


class Alignment(unittest.TestCase):
    def setUp(self):
        # opens: day0..day7
        self.open = pd.DataFrame({"A": [100, 110, 121, 100, 90, 99, 99, 99.0]},
                                 index=pd.date_range("2020-01-01", periods=8))

    def test_signal_on_close_executes_next_open(self):
        ret = interval_returns(self.open).values
        S = np.zeros((8, 1), dtype=np.int8)
        S[2:4, 0] = 1                       # decided at close of day2 and day3 -> want long after those closes
        H = holdings(S, ret)
        # long over intervals 3 and 4: open3(100)->open4(90)->open5(99); NOT interval 2 (121->100)
        self.assertEqual(H[:, 0].tolist(), [0, 0, 0, 1, 1, 0, 0, 0])
        r = sleeve_returns(H, ret, ZERO, rf=0.0)
        self.assertAlmostEqual(r[3], 90 / 100 - 1)
        self.assertAlmostEqual(r[4], 99 / 90 - 1)
        self.assertAlmostEqual(r[2], 0.0)

    def test_trade_extraction_and_costs(self):
        ret = interval_returns(self.open).values
        S = np.zeros((8, 1), dtype=np.int8); S[2:4, 0] = 1
        H = holdings(S, ret)
        t = extract_trades(H, self.open.values, ret, ["A"], self.open.index, ZERO)
        self.assertEqual(len(t), 1)
        self.assertAlmostEqual(t.gross.iloc[0], 99 / 100 - 1)   # buy open3=100, sell open5=99
        self.assertEqual(t.bars.iloc[0], 2)
        c = Costs(0.001, 0.002)
        t2 = extract_trades(H, self.open.values, ret, ["A"], self.open.index, c)
        self.assertAlmostEqual(t2.net.iloc[0], (99 / 100) * 0.999 * 0.998 - 1)
        r = sleeve_returns(H, ret, c, rf=0.0)
        self.assertAlmostEqual(r[3], 0.9 - 1 - 0.001)             # entry cost on entry interval
        self.assertAlmostEqual(r[5], -0.002)                    # exit cost on the interval we are back in cash

    def test_rebalance_uses_prior_month_end_signal(self):
        idx = pd.bdate_range("2020-01-01", periods=70)
        rng = np.random.default_rng(0)
        ret = rng.normal(0, 0.01, (70, 3)); ret[-1] = np.nan
        score = np.tile([1.0, 2.0, 3.0], (70, 1))               # stock 2 always best
        r = rebalanced_returns(ret, score, 1, ZERO, rf=0.0, index=idx)
        # first month-end signal -> trade at next open; before that: cash
        first_exec = np.where(~np.isnan(r) & (r != 0))[0][0]
        self.assertGreater(first_exec, 15)
        self.assertAlmostEqual(r[first_exec], ret[first_exec, 2], places=9)


class RebalanceCosts(unittest.TestCase):
    def test_costs_reduce_value_permanently(self):
        idx = pd.bdate_range("2020-01-01", periods=70)
        rng = np.random.default_rng(1)
        ret = rng.normal(0.0005, 0.01, (70, 3)); ret[-1] = np.nan
        score = np.tile([1.0, 2.0, 3.0], (70, 1))
        r0 = rebalanced_returns(ret, score, 1, ZERO, rf=0.0, index=idx)
        r1 = rebalanced_returns(ret, score, 1, Costs(0.01, 0.01), rf=0.0, index=idx)
        e = np.where(r0 != 0)[0][0]
        # first rebalance buys 100% of capital at 1% cost, same stock thereafter (no more turnover)
        self.assertAlmostEqual((1 + r1[e]) / (1 + r0[e]), 0.99, places=9)
        eq0, eq1 = np.nancumprod(1 + r0), np.nancumprod(1 + r1)
        self.assertAlmostEqual(eq1[-2] / eq0[-2], 0.99, places=9)   # cost is permanent, not reversed next day


class Causality(unittest.TestCase):
    """Truncating history must never change an earlier state => no look-ahead inside any strategy."""

    def test_no_lookahead(self):
        for sym in ("RELIANCE", "HDFCBANK"):
            df = data.load(sym)[["open", "high", "low", "close"]].loc["2015":"2024"]
            for name, (_, fn) in STRATEGIES.items():
                full = fn(df)
                for cut in (700, 1100, 1700):
                    part = fn(df.iloc[:cut])
                    np.testing.assert_array_equal(full[:cut], part, err_msg=f"{name} on {sym} cut {cut}")


if __name__ == "__main__":
    unittest.main()
