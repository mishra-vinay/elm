import unittest

import numpy as np
import pandas as pd

from strategy_lab import silver_study as ss


def make_bars(days):
    """days: list of lists of (o,h,l,c); 14 hourly bars/day starting 09:30 IST."""
    rows, idx, sess = [], [], []
    d0 = pd.Timestamp("2026-03-02", tz="Asia/Kolkata")
    for n, day in enumerate(days):
        base = d0 + pd.Timedelta(days=n)
        for k, (o, h, l, c) in enumerate(day):
            idx.append(base + pd.Timedelta(hours=9, minutes=30 + 60 * k))
            rows.append((o, h, l, c, 1))
            sess.append(base.tz_localize(None))
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx), columns=["open", "high", "low", "close", "volume"])
    df["session"] = sess
    return df


def flat(px, n):
    return [(px, px, px, px)] * n


class TestSilver(unittest.TestCase):
    def setUp(self):
        # day0: range 95..100.  day1: breaks 100, pulls back to 99, rallies to 102.
        day0 = [(97, 100, 95, 97)] + flat(97, 13)
        day1 = [(99.5, 100.5, 99.5, 100.4), (100.4, 100.6, 99.0, 99.2), (99.2, 102.5, 99.2, 102.4)] + flat(102.4, 11)
        self.bars = make_bars([day0, day1])
        self.sess = ss.session_table(self.bars, min_bars=8)
        self.start = pd.Timestamp("2026-03-03")

    def test_long_setup_hits_target(self):
        spec = ss.Spec(pullback=0.01, target=0.02, stop=0.01)
        tr, fl = ss.find_trades(self.bars, self.sess, spec, self.start, pd.Timestamp("2026-03-04"))
        self.assertEqual(len(tr), 1)
        t = tr.iloc[0]
        self.assertEqual(t["side"], "BUY")
        self.assertAlmostEqual(t["entry"], 99.0)           # H*(1-1%) = 99.0
        self.assertEqual(t["reason"], "target")
        self.assertAlmostEqual(t["exit"], 99.0 * 1.02)

    def test_no_entry_in_break_bar(self):
        # pullback level touched inside the SAME bar as the break must not fill (order of ticks unknown)
        day1 = [(99.5, 100.5, 98.9, 100.4)] + flat(100.4, 13)
        bars = make_bars([[(97, 100, 95, 97)] + flat(97, 13), day1])
        sess = ss.session_table(bars, min_bars=8)
        tr, fl = ss.find_trades(bars, sess, ss.Spec(), pd.Timestamp("2026-03-03"), pd.Timestamp("2026-03-04"))
        self.assertTrue(tr.empty)
        self.assertTrue(bool(fl.iloc[0]["up_break"]))

    def test_stop_before_target_same_bar_conservative(self):
        day1 = [(99.5, 100.5, 99.5, 100.4), (100.4, 100.6, 99.0, 99.2), (99.2, 102.5, 97.0, 100)] + flat(100, 11)
        bars = make_bars([[(97, 100, 95, 97)] + flat(97, 13), day1])
        sess = ss.session_table(bars, min_bars=8)
        spec = ss.Spec()
        tr, _ = ss.find_trades(bars, sess, spec, pd.Timestamp("2026-03-03"), pd.Timestamp("2026-03-04"))
        self.assertEqual(tr.iloc[0]["reason"], "stop")
        tr2, _ = ss.find_trades(bars, sess, ss.Spec(conservative=False), pd.Timestamp("2026-03-03"), pd.Timestamp("2026-03-04"))
        self.assertEqual(tr2.iloc[0]["reason"], "target")

    def test_levels_use_only_previous_session(self):
        self.assertTrue(np.isnan(self.sess["prev_high"].iloc[0]))
        self.assertEqual(self.sess["prev_high"].iloc[1], 100)
        self.assertEqual(self.sess["prev_low"].iloc[1], 95)

    def test_black76_put_call_parity(self):
        F, K, T, s, r = 220000, 220000, 0.1, 0.35, 0.065
        c = ss.black76(F, K, T, s, "C", r)
        p = ss.black76(F, K, T, s, "P", r)
        self.assertAlmostEqual(c - p, np.exp(-r * T) * (F - K), places=4)


if __name__ == "__main__":
    unittest.main()
