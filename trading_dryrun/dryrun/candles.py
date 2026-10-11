"""Aggregate live ticks into 1-minute candles (keyed by local receive time)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None      # volume traded in the minute, when the feed reports cumulative day volume
    ticks: int = 1


class MinuteAggregator:
    def __init__(self):
        self.bar: Bar | None = None
        self._cum_at_bar_start: int | None = None
        self._last_cum: int | None = None
        self.day_high: float | None = None
        self.day_low: float | None = None

    def add(self, ts: datetime, price: float, cum_volume: int | None = None) -> Bar | None:
        """Add a tick; returns the completed previous bar when a new minute starts."""
        self.day_high = price if self.day_high is None else max(self.day_high, price)
        self.day_low = price if self.day_low is None else min(self.day_low, price)
        minute = ts.replace(second=0, microsecond=0)
        done = None
        if self.bar is not None and minute != self.bar.ts:
            done = self._finish()
        if self.bar is None:
            self.bar = Bar(minute, price, price, price, price, None)
            self._cum_at_bar_start = self._last_cum if self._last_cum is not None else cum_volume
        else:
            b = self.bar
            b.high, b.low, b.close, b.ticks = max(b.high, price), min(b.low, price), price, b.ticks + 1
        if cum_volume is not None:
            self._last_cum = cum_volume
        return done

    def _finish(self) -> Bar | None:
        b = self.bar
        if b is None:
            return None
        if self._last_cum is not None and self._cum_at_bar_start is not None:
            b.volume = max(0, self._last_cum - self._cum_at_bar_start)
        self.bar = None
        return b

    def flush(self) -> Bar | None:
        return self._finish()
