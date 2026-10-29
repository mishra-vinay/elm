"""Download + cache daily adjusted OHLCV for NSE symbols from Yahoo Finance's public chart API.

Prices are adjusted for splits AND dividends (open/high/low scaled by adjclose/close), so
returns computed from them are total returns. Cache lives in strategy_lab/.cache (git-ignored).
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from .universe import BENCHMARKS, SYMBOLS, yahoo

CACHE = Path(__file__).parent / ".cache"
URL = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0 (research script)"}
START = "2008-01-01"  # warm-up before the 2010 evaluation start


def _fetch(sym: str, retries: int = 5, start: str = START) -> pd.DataFrame | None:
    params = {
        "period1": int(pd.Timestamp(start).timestamp()),
        "period2": int(pd.Timestamp.now().timestamp()) + 86400,
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    for attempt in range(retries):
        r = requests.get(URL.format(sym=sym), params=params, headers=HEADERS, timeout=30)
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        res = (r.json().get("chart", {}).get("result") or [None])[0]
        if not res or "timestamp" not in res:
            return None
        q = res["indicators"]["quote"][0]
        adj = res["indicators"]["adjclose"][0]["adjclose"]
        idx = (pd.to_datetime(res["timestamp"], unit="s", utc=True)
               .tz_convert("Asia/Kolkata").normalize().tz_localize(None))
        df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")}, index=idx)
        df["adjclose"] = adj
        df = df[~df.index.duplicated(keep="last")].dropna(subset=["close", "adjclose"])
        f = df["adjclose"] / df["close"]
        out = pd.DataFrame({
            "open": df["open"] * f, "high": df["high"] * f, "low": df["low"] * f,
            "close": df["adjclose"], "volume": df["volume"],
            "raw_close": df["close"],  # unadjusted-for-dividends close (for display / sizing)
        })
        return out.dropna(subset=["open", "high", "low", "close"])
    return None


def load(sym: str, refresh: bool = False, start: str = START, tag: str = "") -> pd.DataFrame | None:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{sym.replace('^', '_').replace('&', '_')}{tag}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    df = _fetch(yahoo(sym), start=start)
    if df is None or df.empty:
        return None
    df.to_csv(path)
    time.sleep(0.25)
    return df


def load_universe(refresh: bool = False, verbose: bool = True):
    """Return (dict sym->df, list of failed symbols)."""
    out, failed = {}, []
    for s in SYMBOLS:
        df = load(s, refresh)
        if df is None or len(df) < 300:
            failed.append(s)
        else:
            out[s] = df
    if verbose:
        print(f"loaded {len(out)} symbols, failed {len(failed)}: {failed}")
    return out, failed


def load_benchmarks(refresh: bool = False) -> dict[str, pd.DataFrame]:
    return {name: load(sym, refresh) for name, sym in BENCHMARKS.items()}


SPLICE_DROP = -0.30   # one-day close-to-close move at/below this = unadjusted corporate action (split/demerger)
BAD_OPEN = 0.40       # open differing from BOTH prev close and same-day close by more than this = corrupt print


def clean_panels(P: dict[str, pd.DataFrame]):
    """Remove non-economic artefacts from the aligned panels. Returns (cleaned panels, log DataFrame).

    1. Corrupt prints: an open >40% away from both the previous close and the same-day close is replaced
       by that day's close (high/low clipped to the bar). Genuine gap opens (Covid, 2008, election day) are kept.
    2. Unadjusted corporate actions: a one-day close-to-close fall of >= 30% in these large caps is
       treated as a split/demerger Yahoo failed to adjust; all earlier prices are rescaled so the
       event day's return is the (close-based) 0. Large UP moves are kept (PSU-bank recap days etc.),
       as are real crashes < 30% (Adani Feb-2023, Covid).
    """
    P = {k: v.copy() for k, v in P.items()}
    log = []
    o, h, l, c = P["open"], P["high"], P["low"], P["close"]
    bad = ((o / c - 1).abs() > BAD_OPEN) & ((o / c.shift(1) - 1).abs() > BAD_OPEN)
    for s in o.columns:
        for d in o.index[bad[s].fillna(False).values]:
            log.append((s, d.date(), "bad_open", float(o.at[d, s]), float(c.at[d, s])))
            o.at[d, s] = c.at[d, s]
            h.at[d, s] = max(c.at[d, s], o.at[d, s])
            l.at[d, s] = min(c.at[d, s], o.at[d, s])
    r = c / c.shift(1) - 1
    for s in c.columns:
        for d in r.index[(r[s] <= SPLICE_DROP).fillna(False).values]:
            prev = c[s].shift(1).at[d]
            f = c.at[d, s] / prev
            before = c.index < d
            for k in ("open", "high", "low", "close", "raw_close"):
                P[k].loc[before, s] = P[k].loc[before, s] * f
            P["volume"].loc[before, s] = P["volume"].loc[before, s] / f
            log.append((s, d.date(), "spliced_drop", float(r.at[d, s]), float(f)))
    return P, pd.DataFrame(log, columns=["symbol", "date", "action", "a", "b"])


def panels(universe: dict[str, pd.DataFrame], calendar: pd.DatetimeIndex):
    """Align all symbols to the exchange calendar -> dict of (T x N) DataFrames."""
    cols = {k: {} for k in ("open", "high", "low", "close", "volume", "raw_close")}
    for s, df in universe.items():
        d = df.reindex(calendar)
        first, last = df.index[0], df.index[-1]
        for k in cols:
            ser = d[k]
            if k != "volume":
                # fill short holes (halts) only inside the listed span; never before listing / after last bar
                ser = ser.ffill(limit=5)
            cols[k][s] = ser.where((calendar >= first) & (calendar <= last))
    return {k: pd.DataFrame(v) for k, v in cols.items()}
