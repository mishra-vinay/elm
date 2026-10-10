"""Download NSE SME-series (SM, ST) daily data + circuit bands.   python -m strategy_lab.sme_data [start] [end]

Sources: NSE UDiFF bhavcopy (open/high/low/close/prev close/volume/value/trades/lot size) and sec_list (price band per symbol per day).
Cached per day in strategy_lab/.cache/sme/YYYYMMDD.csv
"""
import io
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

CACHE = Path(__file__).parent / ".cache" / "sme"
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"}


def session():
    s = requests.Session(); s.headers.update(H); s.get("https://www.nseindia.com/", timeout=30)
    return s


def fetch_day(s, d: pd.Timestamp):
    p = CACHE / f"{d:%Y%m%d}.csv"
    if p.exists():
        return pd.read_csv(p) if p.stat().st_size > 5 else None
    r = s.get(f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip", timeout=60)
    if r.status_code != 200:
        p.write_text(""); return None                             # holiday / no file
    z = zipfile.ZipFile(io.BytesIO(r.content)); df = pd.read_csv(z.open(z.namelist()[0]))
    df = df[df.SctySrs.isin(["SM", "ST"])][["TradDt", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric",
                                              "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "NewBrdLotQty"]].copy()
    df.columns = ["date", "symbol", "series", "open", "high", "low", "close", "prev_close", "volume", "value", "trades", "lot"]
    sl = None
    for _ in range(3):
        rr = s.get(f"https://nsearchives.nseindia.com/content/equities/sec_list_{d:%d%m%Y}.csv", timeout=60)
        if rr.status_code == 200:
            sl = pd.read_csv(io.StringIO(rr.text)); break
        time.sleep(1)
    if sl is not None:
        sl.columns = [c.strip() for c in sl.columns]
        sl = sl[sl.Series.isin(["SM", "ST"])][["Symbol", "Band"]].rename(columns={"Symbol": "symbol", "Band": "band"})
        sl["symbol"] = sl.symbol.astype(str).str.strip(); df = df.merge(sl.drop_duplicates("symbol"), on="symbol", how="left")
    else:
        df["band"] = None
    df.to_csv(p, index=False); time.sleep(0.25)
    return df


def main(start="2024-01-01", end=None):
    CACHE.mkdir(parents=True, exist_ok=True)
    end = pd.Timestamp(end) if end else pd.Timestamp.now().normalize()
    s = session(); n = 0
    for d in pd.bdate_range(start, end):
        try:
            fetch_day(s, d)
        except Exception as e:
            time.sleep(3); s = session()
            try:
                fetch_day(s, d)
            except Exception as e2:
                print("fail", d.date(), e2, flush=True)
        n += 1
        if n % 50 == 0:
            print(n, d.date(), flush=True)
    print("done", flush=True)


def load_all() -> pd.DataFrame:
    parts = [pd.read_csv(p) for p in sorted(CACHE.glob("*.csv")) if p.stat().st_size > 5]
    df = pd.concat(parts, ignore_index=True); df["date"] = pd.to_datetime(df.date)
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


if __name__ == "__main__":
    main(*(sys.argv[1:3]))
