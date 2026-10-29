"""Per-stock, long-only daily strategies.

Each strategy maps an OHLC DataFrame to a 0/1 *state* array: state[t] = 1 means "we want to be long after the
close of day t". It may only use data up to and including bar t. The engine executes the change at the NEXT
day's open. (Cross-sectional momentum / low-vol portfolio strategies live in engine.py.)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import adx, atr, ema, rsi, sma, supertrend_dir


def _machine(entry: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    """Enter when `entry`, leave when `exit_`; stay otherwise."""
    out = np.zeros(len(entry), dtype=np.int8)
    pos = 0
    for t in range(len(entry)):
        if pos == 0 and entry[t]:
            pos = 1
        elif pos == 1 and exit_[t]:
            pos = 0
        out[t] = pos
    return out


def _b(x: pd.Series) -> np.ndarray:
    return x.fillna(False).values.astype(bool)


# --- trend following -----------------------------------------------------------------------------------------
def ema_cross(fast: int, slow: int):
    def f(df):
        return (_b(ema(df.close, fast) > ema(df.close, slow))).astype(np.int8)
    return f


def ema_cross_adx(fast: int, slow: int, adx_min: float = 20):
    def f(df):
        up = ema(df.close, fast) > ema(df.close, slow)
        return _machine(_b(up & (adx(df) > adx_min)), _b(~up))
    return f


def sma_filter(n: int):
    def f(df):
        return _b(df.close > sma(df.close, n)).astype(np.int8)
    return f


def donchian(n_in: int, n_out: int):
    def f(df):
        brk = df.close > df.high.rolling(n_in, min_periods=n_in).max().shift(1)
        stop = df.close < df.low.rolling(n_out, min_periods=n_out).min().shift(1)
        return _machine(_b(brk), _b(stop))
    return f


def supertrend(period: int, mult: float):
    def f(df):
        return (supertrend_dir(df, period, mult) == 1).astype(np.int8)
    return f


def high52_trail(atr_mult: float = 3.0, lookback: int = 252):
    """Buy a new 252-day closing high (in a >200d uptrend); exit on a close below a ratcheting ATR trailing stop."""
    def f(df):
        c = df.close.values
        hh = _b(df.close >= df.close.rolling(lookback, min_periods=lookback).max())
        up = _b(df.close > sma(df.close, 200))
        a = atr(df, 14).values
        out = np.zeros(len(c), dtype=np.int8)
        pos, trail = 0, 0.0
        for t in range(len(c)):
            if np.isnan(a[t]):
                continue
            if pos == 1:
                if c[t] < trail:
                    pos = 0
                else:
                    trail = max(trail, c[t] - atr_mult * a[t])
            elif hh[t] and up[t]:
                pos, trail = 1, c[t] - atr_mult * a[t]
            out[t] = pos
        return out
    return f


# --- mean reversion ------------------------------------------------------------------------------------------
def rsi2_meanrev(entry_rsi: float = 10, exit_ma: int = 5, max_hold: int = 10):
    """Connors-style: buy deep 2-day RSI oversold inside a 200d uptrend, exit on a close above the 5d SMA (or time)."""
    def f(df):
        r2 = rsi(df.close, 2).values
        up = _b(df.close > sma(df.close, 200))
        back = _b(df.close > sma(df.close, exit_ma))
        out = np.zeros(len(r2), dtype=np.int8)
        pos, held = 0, 0
        for t in range(len(r2)):
            if pos == 1:
                held += 1
                if back[t] or held >= max_hold:
                    pos = 0
            elif up[t] and r2[t] < entry_rsi:
                pos, held = 1, 0
            out[t] = pos
        return out
    return f


def rsi14_pullback(entry: float = 35, exit_: float = 55):
    def f(df):
        r = rsi(df.close, 14)
        up = df.close > sma(df.close, 200)
        return _machine(_b(up & (r < entry)), _b(r > exit_))
    return f


# name -> (family, factory)
STRATEGIES = {
    "sma200_filter":        ("trend filter", sma_filter(200)),
    "ema_cross_10_30":      ("EMA crossover", ema_cross(10, 30)),
    "ema_cross_20_50":      ("EMA crossover", ema_cross(20, 50)),
    "ema_cross_50_200":     ("EMA crossover", ema_cross(50, 200)),
    "ema_cross_20_50_adx":  ("EMA crossover", ema_cross_adx(20, 50, 20)),
    "donchian_20_10":       ("Donchian breakout", donchian(20, 10)),
    "donchian_55_20":       ("Donchian breakout", donchian(55, 20)),
    "donchian_100_50":      ("Donchian breakout", donchian(100, 50)),
    "supertrend_10_3":      ("SuperTrend", supertrend(10, 3.0)),
    "supertrend_20_3":      ("SuperTrend", supertrend(20, 3.0)),
    "high52_trail3atr":     ("52-wk high breakout", high52_trail(3.0)),
    "rsi2_oversold_10":     ("RSI(2) mean reversion", rsi2_meanrev(10)),
    "rsi2_oversold_5":      ("RSI(2) mean reversion", rsi2_meanrev(5)),
    "rsi14_pullback_35_55": ("RSI(14) pullback", rsi14_pullback(35, 55)),
}
