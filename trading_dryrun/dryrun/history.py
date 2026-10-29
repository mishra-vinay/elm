"""Historical data into the database: daily candles from inception, intraday candles as far back as requested.

Dhan allows about 5 years of intraday history and 90 days per request; `backfill` chunks and de-duplicates.
Run from the dashboard or:  python -m dryrun.history --symbol BAJFINANCE --days 1825
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from sqlalchemy.exc import IntegrityError

from .db import Candle, Instrument
from .dhan_rest import DhanError

log = logging.getLogger("dryrun.history")
INTERVAL_LABEL = {1: "1m", 5: "5m", 15: "15m", 25: "25m", 60: "60m", 0: "D"}


def store_candles(factory: sessionmaker, instrument_id: int, interval: str, rows: list[dict], source: str) -> int:
    """Insert candles. Rows that already exist are skipped, except that official API candles replace ones we built
    ourselves from live ticks (source live/sim). Returns the number inserted or replaced.

    The live collector may write the current minute's candle while a large download is being inserted. If that makes the commit fail on the unique
    (instrument, interval, time) key, the whole insert is repeated against the rows that now exist (an official candle then replaces the live one)."""
    if not rows:
        return 0
    rows = list({r["ts"]: r for r in rows}.values())       # Dhan's daily series repeats a date on contract roll-over days: keep one row per time (the later one)
    for attempt in range(4):
        try:
            with factory() as s:
                have = {c.ts: c for c in s.scalars(select(Candle).where(
                    Candle.instrument_id == instrument_id, Candle.interval == interval,
                    Candle.ts >= min(r["ts"] for r in rows), Candle.ts <= max(r["ts"] for r in rows)))}
                n = 0
                for r in rows:
                    cur = have.get(r["ts"])
                    if cur is None:
                        s.add(Candle(instrument_id=instrument_id, interval=interval, ts=r["ts"], open=r["open"], high=r["high"],
                                     low=r["low"], close=r["close"], volume=r.get("volume"), source=source))
                        n += 1
                    elif source == "api" and cur.source != "api":
                        cur.open, cur.high, cur.low, cur.close, cur.volume, cur.source = (
                            r["open"], r["high"], r["low"], r["close"], r.get("volume"), "api")
                        n += 1
                s.commit()
                return n
        except IntegrityError:
            log.warning("candle insert collided with a concurrent write (attempt %d); repeating", attempt + 1)
    raise RuntimeError("could not store candles: repeated key collisions")


def store_live_bar(s, instrument_id: int, bar, source: str) -> None:
    """Writer-thread helper: add a self-built 1-minute candle unless that minute already exists."""
    if s.scalar(select(Candle.id).where(Candle.instrument_id == instrument_id, Candle.interval == "1m", Candle.ts == bar.ts)) is None:
        s.add(Candle(instrument_id=instrument_id, interval="1m", ts=bar.ts, open=bar.open, high=bar.high, low=bar.low,
                     close=bar.close, volume=bar.volume, source=source))


def _no_data(e: Exception) -> bool:
    """Dhan answers DH-907 when a contract has no candles for the requested range (not listed or not traded yet): that is an empty result, not a failure."""
    return "DH-907" in str(e)


def _latest(factory: sessionmaker, instrument_id: int, interval: str):
    with factory() as s:
        return s.scalar(select(func.max(Candle.ts)).where(Candle.instrument_id == instrument_id, Candle.interval == interval))


def backfill(factory: sessionmaker, rest, inst: Instrument, daily_from: date | None = None,
             intraday_days: int = 365, intervals: tuple[int, ...] = (1,), progress=None, incremental: bool = True) -> dict:
    """Daily candles from `daily_from` (default 2000-01-01 = inception) and intraday candles for the last
    `intraday_days`. With `incremental` (default) only the part after what is already stored is requested
    (the last stored day is fetched again to complete it)."""
    out = {"daily": 0, "intraday": 0, "daily_from": None, "intraday_from": None}
    today = date.today()
    d_from = daily_from or date(2000, 1, 1)
    last_d = _latest(factory, inst.id, "D") if incremental else None
    if last_d is not None:
        d_from = max(d_from, last_d.date() - timedelta(days=5))
    out["daily_from"] = d_from.isoformat()
    if progress:
        progress(f"daily candles from {d_from}")
    cur = d_from
    while cur <= today:
        end = min(date(cur.year + 10, 12, 31), today + timedelta(days=1))
        try:
            rows = rest.daily(inst.segment, inst.security_id, inst.instrument, cur, end)
        except DhanError as e:
            if not _no_data(e):
                raise
            rows = []
        out["daily"] += store_candles(factory, inst.id, "D", rows, "api")
        cur = end + timedelta(days=1)
    if intraday_days > 0:
        for iv in intervals:
            start = today - timedelta(days=min(intraday_days, 1825))
            last_i = _latest(factory, inst.id, INTERVAL_LABEL[iv]) if incremental else None
            if last_i is not None:
                start = max(start, last_i.date())
            out["intraday_from"] = start.isoformat()
            if progress:
                progress(f"{iv}-minute candles from {start}")
            try:
                rows = rest.intraday(inst.segment, inst.security_id, inst.instrument, iv, start, today)
            except DhanError as e:
                if not _no_data(e):
                    raise
                rows = []
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
