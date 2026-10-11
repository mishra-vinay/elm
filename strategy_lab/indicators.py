"""Technical indicators (pandas/numpy). Every value at bar t uses data up to and including bar t."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).mean()


def ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def supertrend_dir(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> np.ndarray:
    """+1 while price is above the SuperTrend line (uptrend), -1 below, 0 during warm-up."""
    a = atr(df, period).values
    hl2 = ((df["high"] + df["low"]) / 2).values
    c = df["close"].values
    n = len(c)
    ub, lb = hl2 + mult * a, hl2 - mult * a
    fub, flb, trend = np.full(n, np.nan), np.full(n, np.nan), np.zeros(n, dtype=np.int8)
    start = int(np.argmax(~np.isnan(a))) if (~np.isnan(a)).any() else n
    for i in range(start, n):
        if i == start:
            fub[i], flb[i] = ub[i], lb[i]
            trend[i] = 1 if c[i] > hl2[i] else -1
            continue
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if trend[i - 1] == -1 and c[i] > fub[i]:
            trend[i] = 1
        elif trend[i - 1] == 1 and c[i] < flb[i]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]
    return trend
