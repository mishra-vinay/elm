"""Independent risk gate. Every entry goes through `approve`; rejections are logged to `risk_logs`.

The strategy cannot bypass this: the runner hands `approve` to the engine and the engine calls it
before opening any (simulated) position. The same gate is meant to sit in front of real orders later.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable


@dataclass
class RiskContext:
    last_tick_at: Callable[[], datetime | None]      # when the last good tick arrived
    now: Callable[[], datetime]
    last_entry_time: time | None                     # no new entries after this time of day
    session_open: time | None
    stale_seconds: float
    ignore_hours: bool
    log: Callable[[str, bool, str], None]            # (check, approved, detail)


class RiskEngine:
    def __init__(self, ctx: RiskContext):
        self.ctx = ctx

    def approve(self, side: str, ts: datetime, price: float, state: dict):
        c = self.ctx
        # 1. market-data freshness: never act on a stale feed
        lt = c.last_tick_at()
        age = (c.now() - lt).total_seconds() if lt else None
        if age is not None and age > c.stale_seconds:
            return self._no("stale_feed", f"stale feed: last tick {age:.0f}s old (> {c.stale_seconds:.0f}s)")
        # 2. time window
        if not c.ignore_hours:
            t = ts.time()
            if c.session_open and t < c.session_open:
                return self._no("session_window", f"before session open {c.session_open:%H:%M}")
            if c.last_entry_time and t > c.last_entry_time:
                return self._no("session_window", f"after last entry time {c.last_entry_time:%H:%M}")
        # 3. sanity
        if price <= 0:
            return self._no("price_sanity", "non-positive price")
        c.log("entry_checks", True, f"{side} @ {price:.2f}")
        return True, None

    def _no(self, check: str, detail: str):
        self.ctx.log(check, False, detail)
        return False, f"risk: {detail}"
