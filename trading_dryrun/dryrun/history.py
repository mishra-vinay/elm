"""Historical data into the database: daily candles from inception, intraday candles as far back as requested.

Dhan allows about 5 years of intraday history and 90 days per request; `backfill` chunks and de-duplicates.
Run from the dashboard or:  python -m dryrun.history --symbol BAJFINANCE --days 1825
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .db import Candle, Instrument

log = logging.getLogger("dryrun.history")
INTERVAL_LABEL = {1: "1m", 5: "5m", 15: "15m", 25: "25m", 60: "60m", 0: "D"}


def store_candles(factory: sessionmaker, instrument_id: int, interval: str, rows: list[dict], source: str) -> int:
    """Insert candles, ignoring ones that already exist. Returns the number inserted."""
    if not rows:
        return 0
    with factory() as s:
        have = set(s.scalars(select(Candle.ts).where(Candle.instrument_id == instrument_id, Candle.interval == interval,
                                                      Candle.ts >= min(r["ts"] for r in rows),
                                                      Candle.ts <= max(r["ts"] for r in rows))))
        new = [Candle(instrument_id=instrument_id, interval=interval, ts=r["ts"], open=r["open"], high=r["high"],
                      low=r["low"], close=r["close"], volume=r.get("volume"), source=source)
               for r in rows if r["ts"] not in have]
        s.add_all(new)
        s.commit()
        return len(new)


def backfill(factory: sessionmaker, rest, inst: Instrument, daily_from: date | None = None,
             intraday_days: int = 365, intervals: tuple[int, ...] = (1,), progress=None) -> dict:
    """Daily from `daily_from` (default 2000-01-01, i.e. inception) and intraday for the last `intraday_days`."""
    out = {"daily": 0, "intraday": 0}
    today = date.today()
    d_from = daily_from or date(2000, 1, 1)
    if progress:
        progress(f"daily candles from {d_from}")
    # daily: one request per ~20 years is allowed by the docs; chunk by 10 years to be safe
    cur = d_from
    while cur <= today:
        end = min(date(cur.year + 10, 12, 31), today + timedelta(days=1))
        rows = rest.daily(inst.segment, inst.security_id, inst.instrument, cur, end)
        out["daily"] += store_candles(factory, inst.id, "D", rows, "api")
        cur = end + timedelta(days=1)
    if intraday_days > 0:
        start = today - timedelta(days=min(intraday_days, 1825))
        for iv in intervals:
            if progress:
                progress(f"{iv}-minute candles from {start}")
            rows = rest.intraday(inst.segment, inst.security_id, inst.instrument, iv, start, today)
            out["intraday"] += store_candles(factory, inst.id, INTERVAL_LABEL[iv], rows, "api")
    return out


def main():     # pragma: no cover  (needs live credentials)
    from .__main__ import _load_env
    from .config import Settings
    from .db import init_db
    from .dhan_rest import DhanRest
    from .instruments import search

    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="name, symbol, security id, or SEGMENT:ID")
    ap.add_argument("--days", type=int, default=365, help="intraday days (max about 1825)")
    ap.add_argument("--interval", type=int, default=1, choices=[1, 5, 15, 25, 60])
    a = ap.parse_args()
    st = Settings()
    _e, factory = init_db(st.database_url)
    hit = search(factory, a.symbol, 1)
    if not hit:
        raise SystemExit("instrument not found; load the instrument list first (python -m dryrun.instruments)")
    rest = DhanRest(st.dhan_client_id, st.dhan_access_token)
    print(backfill(factory, rest, hit[0], intraday_days=a.days, intervals=(a.interval,), progress=print))


if __name__ == "__main__":
    main()
