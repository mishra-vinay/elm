"""Previous-day High/Low for a trading date.

Order of preference:
  1. a manual entry for that date (the dashboard form);
  2. the stored daily candle of the latest earlier date;
  3. High/Low derived from stored intraday candles of the latest earlier date (regular session only);
  4. fetched now from Dhan's historical API (if credentials exist), then stored;
  5. nothing -> the caller asks the user for a manual entry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .config import session_for
from .db import Candle, DailyLevel, Instrument


@dataclass
class Levels:
    high: float
    low: float
    source: str        # manual | candles | derived | api
    from_date: date | None = None


def set_manual(factory: sessionmaker, instrument_id: int, for_date: date, high: float, low: float) -> Levels:
    if not (high > low > 0):
        raise ValueError("High must be greater than Low and both positive")
    with factory() as s:
        row = s.scalar(select(DailyLevel).where(DailyLevel.instrument_id == instrument_id, DailyLevel.for_date == for_date))
        if row is None:
            s.add(DailyLevel(instrument_id=instrument_id, for_date=for_date, high=high, low=low, source="manual"))
        else:
            row.high, row.low, row.source, row.created_at = high, low, "manual", datetime.now()
        s.commit()
    return Levels(high, low, "manual")


def from_db(factory: sessionmaker, inst: Instrument, for_date: date) -> Levels | None:
    with factory() as s:
        row = s.scalar(select(DailyLevel).where(DailyLevel.instrument_id == inst.id, DailyLevel.for_date == for_date))
        if row is not None and row.source == "manual":
            return Levels(row.high, row.low, "manual")
        day = datetime.combine(for_date, time.min)
        c = s.scalar(select(Candle).where(Candle.instrument_id == inst.id, Candle.interval == "D", Candle.ts < day)
                     .order_by(Candle.ts.desc()).limit(1))
        if c is not None:
            return Levels(c.high, c.low, "candles", c.ts.date())
        # derive from intraday candles of the latest earlier day with data
        last = s.scalar(select(func.max(Candle.ts)).where(Candle.instrument_id == inst.id, Candle.interval != "D", Candle.ts < day))
        if last is not None:
            d0 = last.date()
            open_t, _e, _sq, close_t = session_for(inst.segment)
            lo_ts, hi_ts = datetime.combine(d0, open_t), datetime.combine(d0, close_t)
            hi, lo = s.execute(select(func.max(Candle.high), func.min(Candle.low)).where(
                Candle.instrument_id == inst.id, Candle.interval != "D", Candle.ts >= lo_ts, Candle.ts < hi_ts)).one()
            if hi is not None and lo is not None and hi > lo > 0:
                return Levels(hi, lo, "derived", d0)
    return None


def resolve(factory: sessionmaker, inst: Instrument, for_date: date, rest=None) -> Levels | None:
    lv = from_db(factory, inst, for_date)
    if lv is not None:
        return lv
    if rest is not None:
        from .history import store_candles
        try:
            rows = rest.daily(inst.segment, inst.security_id, inst.instrument, for_date - timedelta(days=12), for_date)
        except Exception:      # noqa: BLE001  (the dashboard falls back to manual entry)
            return None
        store_candles(factory, inst.id, "D", rows, "api")
        return from_db(factory, inst, for_date)
    return None
