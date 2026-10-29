"""Silver data for the options-strategy study.

Free historical MCX silver *option* prices do not exist, so the study uses COMEX silver futures
(Yahoo SI=F) as a proxy for the MCX silver futures that the options are written on, converted to
rupees/kg with USDINR. Intraday bars: 1h (Yahoo keeps ~730 days) and 15m/5m (~60 days).
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

CACHE = Path(__file__).parent / ".cache"
URL = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0 (research script)"}
IST = "Asia/Kolkata"


def _fetch(sym: str, interval: str, rng: str, retries: int = 6) -> pd.DataFrame:
    for attempt in range(retries):
        r = requests.get(URL.format(sym=sym), params={"interval": interval, "range": rng},
                         headers=HEADERS, timeout=30)
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(IST)
        df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")}, index=idx)
        return df.dropna(subset=["open", "high", "low", "close"])
    raise RuntimeError(f"Yahoo kept returning 429 for {sym}")


def load(sym: str, interval: str, rng: str, refresh: bool = False) -> pd.DataFrame:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{sym.replace('=', '_')}_{interval}_{rng}.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST)
        return df
    df = _fetch(sym, interval, rng)
    df.to_csv(path)
    return df
