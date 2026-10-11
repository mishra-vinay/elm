"""Simulated feeds with the same interface as DhanFeed, for development, tests and demos.

`SimFeed` makes a seeded random walk around a start price per instrument. `ReplayFeed` plays back a given list
of prices as fast as possible (used in tests). Neither contacts any network.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Callable, Iterable

from .config import IST
from .dhan_feed import FeedTick


class _BaseSim:
    def __init__(self, on_tick: Callable[[FeedTick], None], on_note=None, on_status=None):
        self.on_tick, self.on_note, self.on_status = on_tick, on_note, on_status
        self.subscribed: set[tuple[str, str]] = set()
        self.start_prices: dict[tuple[str, str], float] = {}
        self.connected = False
        self.fatal = None
        self.disconnects = 0
        self.last_message_at = None
        self._task: asyncio.Task | None = None

    async def start(self):
        self.connected = True
        if self.on_status:
            self.on_status("info", "simulated feed started (no real market data)")
        self._task = asyncio.create_task(self._run(), name="sim-feed")

    async def stop(self):
        self.connected = False
        if self._task:
            self._task.cancel()

    async def subscribe(self, instruments: Iterable[tuple[str, str]]):
        self.subscribed.update(instruments)

    def set_start_price(self, key: tuple[str, str], price: float):
        self.start_prices[key] = price

    def update_token(self, token: str):  # same interface as DhanFeed
        pass

    async def reconnect(self):
        pass

    async def _run(self):
        raise NotImplementedError


class SimFeed(_BaseSim):
    def __init__(self, on_tick, on_note=None, on_status=None, ticks_per_second: float = 5.0,
                 tick_vol_pct: float = 0.03, seed: int | None = None):
        super().__init__(on_tick, on_note, on_status)
        self.tps, self.vol, self.rng = ticks_per_second, tick_vol_pct / 100, random.Random(seed)
        self._px: dict[tuple[str, str], float] = {}
        self._vol: dict[tuple[str, str], int] = {}

    async def _run(self):
        try:
            while True:
                for key in list(self.subscribed):
                    px = self._px.get(key) or self.start_prices.get(key, 100.0)
                    px *= 1 + self.rng.gauss(0, self.vol)
                    self._px[key] = px
                    self._vol[key] = self._vol.get(key, 0) + self.rng.randint(1, 50)
                    now = datetime.now(IST).replace(tzinfo=None)
                    self.last_message_at = now
                    self.on_tick(FeedTick(key[0], key[1], round(px, 2), None, self._vol[key], None, None, now))
                await asyncio.sleep(1.0 / self.tps)
        except asyncio.CancelledError:
            pass


class ReplayFeed(_BaseSim):
    """Plays `prices` for the first subscribed instrument, one tick per `step_seconds` of *simulated* time."""

    def __init__(self, on_tick, prices: list[float], start: datetime, step_seconds: float = 1.0,
                 on_note=None, on_status=None, pause: float = 0.0, on_done=None):
        super().__init__(on_tick, on_note, on_status)
        self.prices, self.start_dt, self.step, self.pause, self.on_done = prices, start, step_seconds, pause, on_done

    async def _run(self):
        from datetime import timedelta
        while not self.subscribed:
            await asyncio.sleep(0.01)
        seg, sid = sorted(self.subscribed)[0]
        for i, p in enumerate(self.prices):
            ts = self.start_dt + timedelta(seconds=i * self.step)
            self.last_message_at = ts
            self.on_tick(FeedTick(seg, sid, p, None, None, None, None, ts))
            await asyncio.sleep(self.pause)
        if self.on_done:
            self.on_done()
