"""RunManager: owns the market-data feed, the per-instrument state and every live dry-run.

Flow for each tick:  feed -> bad-tick filter -> tick buffer + 1-minute candle -> each run's strategy engine
(inside the session window) -> signals/trades queued to the DB writer thread.
Nothing here can place an order.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import queue
import time as _time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable

from sqlalchemy import insert, select, update
from sqlalchemy.orm import sessionmaker

from . import levels as levels_mod
from .candles import MinuteAggregator
from .config import Settings, session_for
from .db import ApiLog, Candle, Instrument, RiskLog, Run, Signal, Tick, Trade, Watch, get_setting, now_ist, purge_old_ticks
from .dhan_feed import FeedNote, FeedTick
from .history import backfill, store_live_bar
from .risk import RiskContext, RiskEngine
from .strategy import Event, Params, StrategyEngine, get_strategy, make_engine

log = logging.getLogger("dryrun.runner")
MAX_ACTIVE_RUNS = 20
MAX_JUMP = 0.20          # a tick more than 20% away from the last accepted price is held back until the NEXT distinct tick
                         # confirms it (a real gap or split-day move) or contradicts it (a bad print, which is then dropped)
EPOCH = datetime(1970, 1, 1)


class LevelsNeeded(Exception):
    """Previous-day High/Low not found anywhere: the caller must provide them manually."""


class StartError(Exception):
    pass


class Writer:
    """Single background thread that performs all database writes, so the feed callback never blocks on I/O."""

    def __init__(self, factory: sessionmaker):
        self.factory = factory
        self.q: queue.Queue = queue.Queue()
        self.t = threading.Thread(target=self._loop, name="db-writer", daemon=True)
        self.errors = 0
        self.t.start()

    def submit(self, fn: Callable) -> None:
        self.q.put(fn)

    def _loop(self):
        while True:
            fn = self.q.get()
            if fn is None:
                self.q.task_done()
                return
            try:
                with self.factory() as s:
                    fn(s)
                    s.commit()
            except Exception:      # noqa: BLE001
                self.errors += 1
                log.exception("db write failed")
            finally:
                self.q.task_done()

    def flush(self):
        self.q.join()

    def stop(self):
        self.q.put(None)
        self.t.join(timeout=10)


@dataclass
class InstState:
    inst: Instrument
    agg: MinuteAggregator = field(default_factory=MinuteAggregator)
    last_price: float | None = None
    last_tick_at: datetime | None = None
    prev_close: float | None = None
    ticks_today: int = 0
    minutes_seen: set = field(default_factory=set)
    bad_ticks: int = 0
    watch: bool = False                    # collect-only subscription
    # --- feed quality, per instrument (shown on the Ticks page) ---
    day: date | None = None                # trading day of the data held here; everything resets when it changes
    last_ltt: int | None = None
    last_vol: int | None = None
    last_key: tuple | None = None          # (ltp, ltt, volume) of the last accepted packet
    pending: tuple | None = None           # (FeedTick, ts): a >20% jump waiting for confirmation
    prev_msg_at: datetime | None = None    # arrival of the packet BEFORE the current one (any packet, even a skipped duplicate)
    last_msg_at: datetime | None = None    # arrival of the latest packet: this is "is the feed alive for this instrument"
    packets: int = 0
    accepted: int = 0
    dup_skipped: int = 0
    old_skipped: int = 0
    jump_confirmed: int = 0
    day_high: float | None = None
    day_low: float | None = None
    volume: int | None = None
    rate: deque = field(default_factory=lambda: deque(maxlen=4000))       # epoch seconds of every packet (for packets/s)
    lat: deque = field(default_factory=lambda: deque(maxlen=200))         # arrival minus exchange last-trade time, seconds
    proc: deque = field(default_factory=lambda: deque(maxlen=2000))       # microseconds spent in our code per packet
    proc_max_us: float = 0.0
    tail: deque = field(default_factory=lambda: deque(maxlen=25))         # newest accepted ticks, for the Ticks page


@dataclass
class LiveRun:
    id: int
    inst: Instrument
    params: Params
    engine: StrategyEngine
    trading_date: date
    levels: levels_mod.Levels
    open_t: time
    last_entry_t: time
    squareoff_t: time
    status: str = "waiting"
    ticks: int = 0
    first_tick: datetime | None = None
    last_tick: datetime | None = None
    last_price: float | None = None
    started_disconnects: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=900))
    events: deque = field(default_factory=lambda: deque(maxlen=40))
    pending_signal: dict | None = None
    error: str | None = None
    closed: bool = False
    label: str = ""
    log: deque = field(default_factory=lambda: deque(maxlen=200))          # human timeline: (time, text)


class RunManager:
    def __init__(self, settings: Settings, factory: sessionmaker, feed_factory: Callable, rest=None,
                 clock: Callable[[], datetime] = now_ist):
        self.s, self.factory, self.rest, self.clock = settings, factory, rest, clock
        self.feed_factory = feed_factory
        self.feed = None
        self.writer = Writer(factory)
        self.states: dict[tuple[str, str], InstState] = {}
        self.runs: dict[int, LiveRun] = {}
        self.lock = threading.RLock()
        self._tick_buf: list[Tick] = []
        self._task: asyncio.Task | None = None
        self._last_purge = datetime.min
        self._last_update: date | None = None
        self.notes: deque = deque(maxlen=30)

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        self._close_orphans()
        self.feed = self.feed_factory(self._on_tick, self._on_note, self._on_status)
        await self.feed.start()
        with self.factory() as s:                                   # live collection survives restarts
            for w, inst in s.execute(select(Watch, Instrument).join(Instrument, Instrument.id == Watch.instrument_id)
                                     .where(Watch.collect_live.is_(True))):
                key = (inst.segment, inst.security_id)
                self.states[key] = InstState(inst, watch=True)
                await self.feed.subscribe([key])
        self._task = asyncio.create_task(self._supervisor(), name="supervisor")

    def _close_orphans(self):
        """Runs that were live when the process died cannot be resumed. Any simulated position they still had open is closed
        at the run's last stored price (reason 'interrupted'), so no trade stays open forever and the P&L is not lost."""
        with self.factory() as s:
            live = s.scalars(select(Run).where(Run.status.in_(("starting", "waiting", "running")))).all()
            for run in live:
                prm = run.params or {}
                closed = []
                for t in s.scalars(select(Trade).where(Trade.run_id == run.id, Trade.exit_ts.is_(None))):
                    px = run.last_price
                    t.exit_ts, t.exit_reason = now_ist(), "interrupted"
                    if px:
                        buy = t.side == "BUY"
                        slip = px * float(prm.get("slippage_pct", 0.0)) / 100
                        t.exit_price = px - slip if buy else px + slip
                        t.gross_pnl = (1 if buy else -1) * (t.exit_price - t.entry_price) * t.qty
                        t.cost = t.entry_price * t.qty * float(prm.get("cost_pct", 0.0)) / 100
                        t.net_pnl = t.gross_pnl - t.cost
                        t.net_pct = t.net_pnl / (t.entry_price * t.qty) * 100
                    closed.append(t)
                note = "process restarted while the run was live; ticks up to that point are stored"
                if closed:
                    note += (f"; {len(closed)} open simulated trade(s) closed at the last stored price {run.last_price}" if run.last_price
                             else f"; {len(closed)} open simulated trade(s) flagged 'interrupted' (no price stored, P&L unknown)")
                    s.add(ApiLog(source="run", level="warn", message=f"[run #{run.id}] {note}"[:500]))
                run.status, run.ended_at, run.ran_ok, run.error = "interrupted", now_ist(), False, note
                run.summary = {**(run.summary or {}), "interrupted": True, "open_trades_closed": len(closed)}
            s.commit()

    async def stop(self):
        for rid in list(self.runs):
            self.finalize(self.runs[rid], "stopped", "manual")
        self._flush_ticks()
        if self._task:
            self._task.cancel()
        if self.feed:
            await self.feed.stop()
        self.writer.flush()
        self.writer.stop()

    # ------------------------------------------------------------------ feed callbacks
    def _on_status(self, level: str, msg: str):
        self.notes.appendleft((self.clock(), level, msg))
        self.writer.submit(lambda s: s.add(ApiLog(source="feed", level=level, message=msg[:500])))

    def _on_note(self, n: FeedNote):
        st = self.states.get((n.segment, n.security_id))
        if st is not None and n.kind == "prev_close":
            st.prev_close = n.value

    def _on_tick(self, ft: FeedTick):
        t0 = _time.perf_counter()
        with self.lock:
            st = self.states.get((ft.segment, ft.security_id))
            if st is None or not (ft.ltp and ft.ltp > 0):
                return
            try:
                self._process_packet(st, ft)
            finally:
                us = (_time.perf_counter() - t0) * 1e6
                st.proc.append(us)
                st.proc_max_us = max(st.proc_max_us, us)

    def _process_packet(self, st: InstState, ft: FeedTick):
        ts = ft.recv_ts or self.clock()
        # liveness is per instrument and counts EVERY packet (even a duplicate we then skip): it is what the stale-feed check uses
        st.prev_msg_at, st.last_msg_at = st.last_msg_at, ts
        st.packets += 1
        st.rate.append(_time.time())
        if st.day != ts.date():                       # new trading day: nothing from yesterday may judge today's ticks
            st.day, st.last_price, st.last_ltt, st.last_vol, st.last_key, st.pending = ts.date(), None, None, None, None, None
            st.ticks_today, st.minutes_seen = 0, set()
        if ft.ltt:
            lat = (ts - (EPOCH + timedelta(seconds=ft.ltt))).total_seconds()       # ltt is Unix seconds read as IST wall clock
            if -5 < lat < 3600:
                st.lat.append(lat)
        key = (ft.ltp, ft.ltt, ft.volume)
        if ft.ltt is not None:
            # a packet older than one already processed (the feed can deliver them out of order) would move the price backwards
            if st.last_ltt is not None and (ft.ltt < st.last_ltt or (
                    ft.ltt == st.last_ltt and ft.volume is not None and st.last_vol is not None and ft.volume < st.last_vol)):
                st.old_skipped += 1
                return
            if key == st.last_key or (st.pending is not None and key == self._key(st.pending[0])):
                st.dup_skipped += 1              # the quote feed repeats identical snapshots on every depth change
                return
        if st.pending is not None:
            pft, pts = st.pending
            st.pending = None
            if abs(ft.ltp / pft.ltp - 1) <= MAX_JUMP:          # the next distinct tick agrees: the big move was real
                st.jump_confirmed += 1
                self.activity("feed", "warn", f"large move confirmed {st.inst.symbol}: {st.last_price} -> {pft.ltp}")
                self._accept(st, pft, pts)
            else:                                              # the next tick is back near the old price: the jump was a bad print
                st.bad_ticks += 1
                self.writer.submit(lambda s, a=st.last_price, b=pft.ltp, sym=st.inst.symbol: s.add(
                    ApiLog(source="feed", level="warn", message=f"bad tick dropped {sym}: {a} -> {b}")))
        if st.last_price and abs(ft.ltp / st.last_price - 1) > MAX_JUMP:
            st.pending = (ft, ts)                 # hold it; the next distinct tick decides
            return
        self._accept(st, ft, ts)

    @staticmethod
    def _key(ft: FeedTick) -> tuple:
        return (ft.ltp, ft.ltt, ft.volume)

    def _accept(self, st: InstState, ft: FeedTick, ts: datetime):
        st.last_price, st.last_tick_at = ft.ltp, ts
        st.last_key = self._key(ft)
        if ft.ltt is not None:
            st.last_ltt = ft.ltt
        if ft.volume is not None:
            st.last_vol = ft.volume
            st.volume = ft.volume
        st.day_high = ft.day_high or st.day_high
        st.day_low = ft.day_low or st.day_low
        st.accepted += 1
        st.ticks_today += 1
        st.tail.appendleft((ts.strftime("%H:%M:%S"), ft.ltp, ft.volume))
        if st.ticks_today == 1:
            self.activity("feed", "info", f"First price for {st.inst.symbol}: {ft.ltp:,.2f}")
        st.minutes_seen.add(ts.replace(second=0, microsecond=0))
        if self.s.store_ticks:
            self._tick_buf.append({"instrument_id": st.inst.id, "ts": ts, "ltp": ft.ltp, "ltt": ft.ltt, "volume": ft.volume,
                                   "day_high": ft.day_high, "day_low": ft.day_low})
        bar = st.agg.add(ts, ft.ltp, ft.volume)
        if bar is not None:
            self._store_bar(st.inst.id, bar)
        for run in [r for r in self.runs.values() if r.inst.id == st.inst.id and not r.closed]:
            self._run_tick(run, ts, ft.ltp)

    def _store_bar(self, instrument_id: int, bar):
        source = "sim" if self.s.feed_mode == "sim" else "live"       # simulated prices must never pass as market data
        self.writer.submit(lambda s: store_live_bar(s, instrument_id, bar, source))

    def _flush_ticks(self):
        if self._tick_buf:
            buf, self._tick_buf = self._tick_buf, []
            self.writer.submit(lambda s: s.execute(insert(Tick), buf))           # one executemany, several times faster than ORM objects

    # ------------------------------------------------------------------ per-run processing
    def _run_tick(self, run: LiveRun, ts: datetime, price: float):
        run.ticks += 1
        run.first_tick = run.first_tick or ts
        run.last_tick, run.last_price = ts, price
        run.recent.append((ts.strftime("%H:%M:%S"), price))
        in_window = self.s.ignore_market_hours or (run.open_t <= ts.time() < run.squareoff_t and ts.date() == run.trading_date)
        if not in_window:
            return
        if run.status == "waiting":
            run.status = "running"
            self._log(run, f"First price received ({price:,.2f}). Strategy is now watching the market.", ts)
            self._update_run(run)
        self._handle(run, run.engine.on_tick(ts, price))

    def activity(self, source: str, level: str, message: str) -> None:
        """One place for everything worth showing in the dashboard's activity log (persisted, newest first)."""
        self.writer.submit(lambda s: s.add(ApiLog(source=source, level=level, message=message[:500])))

    def _log(self, run: LiveRun, text: str, ts: datetime | None = None):
        run.log.appendleft(((ts or self.clock()).strftime("%H:%M:%S"), text))
        level = "warn" if "NOT taken" in text else "info"
        self.activity("run", level, f"[{run.inst.symbol} #{run.id}{(' ' + run.label) if run.label else ''}] {text}")

    def _describe(self, run: LiveRun, ev: Event) -> str | None:
        if ev.kind == "note":                              # a plain sentence from the strategy
            return run.engine.describe_event(ev)
        if ev.kind == "entry_skipped":
            return f"{ev.side} signal NOT taken: {ev.reason}."
        if ev.kind == "entry":
            t = ev.trade
            return (f"SIMULATED {ev.side} entered at {t['entry_price']:,.2f} (target {t['target']:,.2f}, "
                    f"stop {'none' if t['stop'] is None else format(t['stop'], ',.2f')}).")
        if ev.kind == "exit":
            t = ev.trade
            return f"SIMULATED exit ({ev.reason}) at {t['exit_price']:,.2f}: net Rs {t['net_pnl']:,.2f} ({t['net_pct']:+.2f}%)."
        text = run.engine.describe_event(ev)              # the strategy's own wording for its signals and notes
        if ev.kind == "entry_signal":
            return f"{ev.side} signal at {ev.price:,.2f}" + (f": {text}" if text else ".")
        return f"{ev.side} {ev.kind} at {ev.price:,.2f}" + (f": {text}" if text else ".")

    def _handle(self, run: LiveRun, events: list[Event]):
        for ev in events:
            text = self._describe(run, ev)
            if text:
                self._log(run, text, ev.ts)
            run.events.appendleft({"ts": ev.ts.strftime("%H:%M:%S"), "kind": ev.kind, "side": ev.side,
                                   "price": round(ev.price, 4), "reason": ev.reason})
            if ev.kind not in ("entry_signal", "entry_skipped", "entry", "exit"):
                self._signal(run, ev, ev.kind[:8], "info", ev.reason)          # a note from the strategy (e.g. a break), stored as an info signal
            elif ev.kind == "entry_signal":
                run.pending_signal = {"ev": ev}
            elif ev.kind == "entry_skipped":
                self._signal(run, run.pending_signal["ev"] if run.pending_signal else ev, "entry", "skipped", ev.reason)
                run.pending_signal = None
            elif ev.kind == "entry":
                sig = run.pending_signal["ev"] if run.pending_signal else ev
                self._signal(run, sig, "entry", "taken", None)
                run.pending_signal = None
                t = ev.trade
                self.writer.submit(lambda s, t=t: s.add(Trade(run_id=run.id, side=t["side"], qty=t["qty"],
                                                              signal_price=t["signal_price"], entry_ts=t["entry_ts"],
                                                              entry_price=t["entry_price"], target=t["target"], stop=t["stop"])))
            elif ev.kind == "exit":
                t = ev.trade
                self.writer.submit(lambda s, t=t: self._close_trade_row(s, run.id, t))

    @staticmethod
    def _close_trade_row(s, run_id: int, t: dict):
        row = s.scalar(select(Trade).where(Trade.run_id == run_id, Trade.exit_ts.is_(None)).order_by(Trade.id.desc()).limit(1))
        if row is not None:
            row.exit_ts, row.exit_price, row.exit_reason = t["exit_ts"], t["exit_price"], t["exit_reason"]
            row.gross_pnl, row.cost, row.net_pnl, row.net_pct = t["gross_pnl"], t["cost"], t["net_pnl"], t["net_pct"]

    def _signal(self, run: LiveRun, ev: Event, kind: str, action: str, reason: str | None):
        self.writer.submit(lambda s: s.add(Signal(run_id=run.id, ts=ev.ts, kind=kind, side=ev.side, price=ev.price,
                                                  level=ev.level or 0.0, action=action, reason=reason)))

    def _update_run(self, run: LiveRun):
        vals = dict(status=run.status, ticks_received=run.ticks, first_tick_at=run.first_tick, last_tick_at=run.last_tick,
                    last_price=run.last_price, error=run.error)
        self.writer.submit(lambda s: s.execute(update(Run).where(Run.id == run.id).values(**vals)))

    # ------------------------------------------------------------------ supervisor
    async def _supervisor(self):
        try:
            while True:
                await asyncio.sleep(1.0)
                with self.lock:
                    self._flush_ticks()
                    now = self.clock()
                    for run in list(self.runs.values()):
                        if run.closed:
                            continue
                        if not self.s.ignore_market_hours and (now.date() > run.trading_date or (now.date() == run.trading_date and now.time() >= run.squareoff_t)):
                            self.finalize(run, "finished", "squareoff")
                        elif int(now.timestamp()) % 5 == 0:
                            self._update_run(run)
                    if (self.rest is not None and now.weekday() < 5 and now.time() >= time(16, 5)
                            and self._last_update != now.date()):
                        self._last_update = now.date()
                        asyncio.get_running_loop().run_in_executor(None, self.update_history_now)
                    if (now - self._last_purge) > timedelta(hours=6):
                        self._last_purge = now
                        asyncio.get_running_loop().run_in_executor(None, purge_old_ticks, self.factory, self.retention_days())
        except asyncio.CancelledError:
            pass

    def retention_days(self) -> int:
        """Days of raw ticks to keep: the value saved on the Settings page, else TICK_RETENTION_DAYS from .env."""
        try:
            v = get_setting(self.factory, "tick_retention_days")
            return max(1, int(v)) if v is not None else self.s.tick_retention_days
        except Exception:      # noqa: BLE001
            return self.s.tick_retention_days

    def finalize(self, run: LiveRun, status: str, reason: str):
        if run.closed:
            return
        run.closed = True
        self._log(run, f"Run {status} ({reason}). Saving results.")
        self._flush_ticks()
        now = self.clock()
        self._handle(run, run.engine.square_off(now, run.last_price, reason))
        st = self.states.get((run.inst.segment, run.inst.security_id))
        if st is not None:
            bar = st.agg.flush()
            if bar is not None:
                self._store_bar(run.inst.id, bar)
        summ = run.engine.summary()
        elapsed_min = max(1, int(((run.last_tick or now) - (run.first_tick or now)).total_seconds() // 60) + 1)
        seen = len({t for t in (st.minutes_seen if st else set()) if run.first_tick and t >= run.first_tick.replace(second=0, microsecond=0)})
        disconnects = max(0, (getattr(self.feed, "disconnects", 0) or 0) - run.started_disconnects)
        summ.update(ticks=run.ticks, minutes_with_data=seen, minutes_elapsed=elapsed_min,
                    data_coverage=round(min(1.0, seen / elapsed_min), 3), feed_disconnects=disconnects,
                    levels={"high": run.levels.high, "low": run.levels.low, "source": run.levels.source},
                    status_reason=reason)
        ran_ok = run.ticks > 0 and run.error is None
        note = None if ran_ok else (run.error or "no ticks were received during the run")
        run.status = status
        self.writer.submit(lambda s: s.execute(update(Run).where(Run.id == run.id).values(
            status=status, ended_at=now_ist(), ticks_received=run.ticks, first_tick_at=run.first_tick,
            last_tick_at=run.last_tick, last_price=run.last_price, feed_disconnects=disconnects, ran_ok=ran_ok,
            summary=summ, error=note)))
        self._release(run)

    def _release(self, run: LiveRun):
        self.runs.pop(run.id, None)
        key = (run.inst.segment, run.inst.security_id)
        if not any((r.inst.segment, r.inst.security_id) == key for r in self.runs.values()):
            st = self.states.get(key)
            if st is not None and not st.watch:
                self.states.pop(key, None)       # keep collecting only if on the watch list

    # ------------------------------------------------------------------ API used by the dashboard
    async def start_run(self, inst: Instrument, params: Params, manual_levels: tuple[float, float] | None = None,
                        label: str | None = None) -> dict:
        now = self.clock()
        open_t, last_entry_t, squareoff_t, close_t = session_for(inst.segment)
        trading_date = self.next_session_date(now, inst.segment)
        params.validate()
        with self.lock:
            if len(self.runs) >= MAX_ACTIVE_RUNS:
                raise StartError(f"At most {MAX_ACTIVE_RUNS} runs can be active at once; stop one first.")
        if manual_levels:
            lv = levels_mod.set_manual(self.factory, inst.id, trading_date, manual_levels[0], manual_levels[1])
        else:
            lv = await asyncio.get_running_loop().run_in_executor(
                None, levels_mod.resolve, self.factory, inst, trading_date, self.rest)
        if lv is None:
            raise LevelsNeeded("Previous day High/Low not found in the database or from Dhan; enter them manually.")
        try:
            engine_params = params
            with self.factory() as s:
                run = Run(instrument_id=inst.id, status="waiting", trading_date=trading_date,
                          params={**engine_params.as_dict(), **({"label": label} if label else {})},
                          prev_high=lv.high, prev_low=lv.low, levels_source=lv.source)
                s.add(run)
                s.commit()
                run_id = run.id
        except Exception as e:      # noqa: BLE001
            raise StartError(f"could not create the run: {e}")
        holder: dict = {}

        def approve(side, ts, price, state):
            return holder["risk"].approve(side, ts, price, state)

        engine = make_engine(lv.high, lv.low, params, approve=approve)
        engine.session_open = open_t
        engine.symbol = inst.symbol
        mid_session = trading_date == now.date() and now >= datetime.combine(trading_date, open_t) + timedelta(minutes=1)
        day_open_note = None
        if mid_session:             # started after the open: the first price seen is not the day's open, so look the opening price up
            op, src = await asyncio.get_running_loop().run_in_executor(None, self._day_open, inst, trading_date, open_t)
            if op:
                engine.day_open = op
                day_open_note = f"Day open {op:,.2f} (from the {src})."
            else:
                day_open_note = "The day's opening price could not be found, so the first price seen is used as the day open (any day-open filter may be wrong for this run)."
        live = LiveRun(run_id, inst, params, engine, trading_date, lv, open_t, last_entry_t, squareoff_t, label=label or "",
                       started_disconnects=getattr(self.feed, "disconnects", 0) or 0)
        ikey = (inst.segment, inst.security_id)

        def _st():
            return self.states.get(ikey)

        holder["risk"] = RiskEngine(RiskContext(
            # this instrument's previous packet vs this packet: a silent instrument is stale even while others keep ticking
            last_tick_at=lambda: getattr(_st(), "prev_msg_at", None), now=lambda: getattr(_st(), "last_msg_at", None) or self.clock(),
            last_entry_time=last_entry_t,
            session_open=open_t, stale_seconds=self.s.stale_tick_seconds, ignore_hours=self.s.ignore_market_hours,
            log=lambda check, ok, detail: self._risk_log(run_id, inst.symbol, check, ok, detail)))
        with self.lock:
            self.runs[run_id] = live
            key = (inst.segment, inst.security_id)
            if key not in self.states:
                self.states[key] = InstState(inst)
        if trading_date > now.date():
            self._log(live, f"The market is closed now. This run is scheduled for {trading_date:%A %d %b %Y} and starts watching at {open_t:%H:%M} IST. Keep the app running until then.")
        spec = get_strategy(params.strategy)
        desc = spec.describe(lv.high, lv.low, params.options) if spec.describe else f"Strategy: {spec.label}."
        self._log(live, f"Run started. Previous-day High {lv.high:,.2f}, Low {lv.low:,.2f} (source: {lv.source}{', day ' + str(lv.from_date) if lv.from_date else ''}). " + desc)
        if day_open_note:
            self._log(live, day_open_note)
        await self.feed.subscribe([key])
        self._log(live, f"Subscribed to {inst.label}. Waiting for prices.")
        if hasattr(self.feed, "set_start_price"):       # simulated feed: start in the middle of yesterday's range
            self.feed.set_start_price(key, (lv.high + lv.low) / 2)
        return {"id": run_id, "levels": {"high": lv.high, "low": lv.low, "source": lv.source},
                "trading_date": trading_date.isoformat(), "scheduled": trading_date > now.date()}

    def _day_open(self, inst: Instrument, trading_date: date, open_t: time) -> tuple[float | None, str | None]:
        """The session's opening price: the first stored 1m/5m candle of the day, else Dhan's 1-minute candles for today. A candle that starts
        more than 10 minutes after the open is not the opening candle (collection started late), so it is not used."""
        lo = datetime.combine(trading_date, open_t)
        with self.factory() as s:
            for iv in ("1m", "5m"):
                row = s.execute(select(Candle.ts, Candle.open).where(
                    Candle.instrument_id == inst.id, Candle.interval == iv, Candle.source != "sim", Candle.ts >= lo,
                    Candle.ts < lo + timedelta(days=1)).order_by(Candle.ts).limit(1)).first()
                if row and row.ts - lo <= timedelta(minutes=10):
                    return float(row.open), f"stored {iv} candle of {row.ts:%H:%M}"
        if self.rest is not None:
            try:
                rows = [r for r in self.rest.intraday(inst.segment, inst.security_id, inst.instrument, 1, trading_date, trading_date) if r["ts"] >= lo]
                if rows and rows[0]["ts"] - lo <= timedelta(minutes=10):
                    return float(rows[0]["open"]), f"Dhan 1-minute candle of {rows[0]['ts']:%H:%M}"
            except Exception as e:      # noqa: BLE001
                log.warning("day open lookup failed for %s: %s", inst.symbol, e)
        return None, None

    def _risk_log(self, run_id: int, symbol: str, check: str, ok: bool, detail: str):
        self.writer.submit(lambda s: s.add(RiskLog(run_id=run_id, check=check, approved=ok, detail=detail[:200])))
        if not ok:
            self.activity("risk", "warn", f"[{symbol}] entry rejected by risk check '{check}': {detail}")

    def next_session_date(self, now: datetime, segment: str) -> date:
        """The trading day a run started now belongs to: today while the session is open or still to come,
        otherwise the next weekday (exchange holidays are not known to the app)."""
        if self.s.ignore_market_hours:
            return now.date()
        sq = session_for(segment)[2]
        d = now.date()
        if d.weekday() >= 5 or now.time() >= sq:
            d += timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
        return d

    def stop_all(self) -> int:
        with self.lock:
            ids = list(self.runs)
        return sum(1 for rid in ids if self.stop_run(rid))

    def stop_run(self, run_id: int) -> bool:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                return False
            self.finalize(run, "stopped", "manual")
            return True

    def set_watch_flags(self, inst_id: int, collect_live: bool | None = None, daily_update: bool | None = None):
        def op(s):
            w = s.scalar(select(Watch).where(Watch.instrument_id == inst_id))
            if w is None:
                w = Watch(instrument_id=inst_id, collect_live=False, daily_update=False)
                s.add(w)
            if collect_live is not None:
                w.collect_live = collect_live
            if daily_update is not None:
                w.daily_update = daily_update
        self.writer.submit(op)

    def update_history_now(self) -> dict:
        """Incrementally bring stored history up to date for every instrument flagged for daily updates."""
        if self.rest is None:
            return {"updated": 0, "error": "no Dhan credentials"}
        done, failed = [], []
        with self.factory() as s:
            targets = [(inst, w) for w, inst in s.execute(select(Watch, Instrument).join(
                Instrument, Instrument.id == Watch.instrument_id).where(Watch.daily_update.is_(True)))]
        for inst, _w in targets:
            try:
                with self.factory() as s:
                    stored = s.scalars(select(Candle.interval).where(Candle.instrument_id == inst.id).distinct()).all()
                ivs = tuple(sorted({int(i[:-1]) for i in stored if i.endswith("m") and i[:-1].isdigit()})) or (1,)
                res = backfill(self.factory, self.rest, inst, intraday_days=10 if stored else 0, intervals=ivs)
                done.append(f"{inst.symbol}: +{res['daily']} daily, +{res['intraday']} intraday")
            except Exception as e:      # noqa: BLE001
                failed.append(f"{inst.symbol}: {str(e)[:120]}")
        msg = "after-close update: " + ("; ".join(done) or "nothing to update") + (" | FAILED " + "; ".join(failed) if failed else "")
        self._on_status("warn" if failed else "info", msg)
        return {"updated": len(done), "failed": failed, "detail": done}

    async def watch(self, inst: Instrument, on: bool = True):
        self.set_watch_flags(inst.id, collect_live=on)
        key = (inst.segment, inst.security_id)
        with self.lock:
            st = self.states.get(key)
            if on:
                if st is None:
                    st = self.states[key] = InstState(inst)
                st.watch = True
            elif st is not None:
                st.watch = False
                if not any((r.inst.segment, r.inst.security_id) == key for r in self.runs.values()):
                    self.states.pop(key, None)
        if on:
            await self.feed.subscribe([key])

    def _status_text(self, r: LiveRun, now: datetime) -> str:
        e, p = r.engine, r.last_price
        feed_down = not getattr(self.feed, "connected", False)
        prefix = "Feed disconnected, reconnecting. " if feed_down else ""
        hours = not self.s.ignore_market_hours
        if hours and now.date() < r.trading_date:
            return (f"{prefix}Scheduled for {r.trading_date:%A %d %b}: waiting for the market to open at {r.open_t:%H:%M} IST. "
                    f"Levels are loaded: High {r.levels.high:,.2f}, Low {r.levels.low:,.2f}.")
        if r.ticks == 0:
            if hours and now.time() < r.open_t:
                return f"{prefix}Waiting for the market to open at {r.open_t:%H:%M} IST (it is {now:%H:%M}). Levels are loaded: High {r.levels.high:,.2f}, Low {r.levels.low:,.2f}."
            return (f"{prefix}Subscribed, but no price has arrived yet. If the market is open, check the feed badge above "
                    "and that this instrument trades today.")
        if e.position is not None:
            pos = e.position
            sign = 1 if pos.side == "BUY" else -1
            u = sign * (p - pos.entry_price) / pos.entry_price * 100 if p else 0
            return (f"{prefix}SIMULATED {pos.side} open since {pos.entry_ts:%H:%M:%S} at {pos.entry_price:,.2f}. "
                    f"Now {p:,.2f} ({u:+.2f}%). Exits at target {pos.target:,.2f}"
                    f"{'' if pos.stop is None else f' or stop {pos.stop:,.2f}'}, or square-off at {r.squareoff_t:%H:%M}.")
        if hours and now.time() >= r.last_entry_t:
            return f"{prefix}No new entries after {r.last_entry_t:%H:%M}. Square-off at {r.squareoff_t:%H:%M}."
        done = sum(e.trades_by_side.values())
        if done >= r.params.max_trades_per_day:
            return f"{prefix}Max trades per day reached ({done}). No new entries; waiting for square-off."
        if r.params.daily_loss_limit_pct is not None and e.net_pct_sum <= -r.params.daily_loss_limit_pct:
            return f"{prefix}Daily loss limit reached. No new entries."
        own = e.status_text(p)                              # what the strategy is waiting for
        where = "inside" if r.levels.low <= p <= r.levels.high else "outside"
        return f"{prefix}Price {p:,.2f} is {where} yesterday's range ({r.levels.low:,.2f} to {r.levels.high:,.2f}). " + (own or "Watching the market.")

    def ticks_snapshot(self) -> dict:
        """Everything the Ticks page shows: feed health plus, per instrument, rates, quality counters, latency and our own processing time."""
        now, nowt = self.clock(), _time.time()
        feed = self.feed
        stale = self.s.stale_tick_seconds
        rows = []
        with self.lock:
            for key, st in self.states.items():
                age = None if st.last_msg_at is None else max(0.0, (now - st.last_msg_at).total_seconds())
                seg = st.inst.segment
                o, _e, _sq, c = session_for(seg)
                open_now = self.s.ignore_market_hours or (o <= now.time() < c and now.weekday() < 5)
                if st.packets == 0:
                    status = "waiting" if open_now else "market closed"
                elif not open_now and (age is None or age > stale):
                    status = "market closed"
                elif age is not None and age <= stale:
                    status = "receiving"
                else:
                    status = "silent"
                rate = [t for t in st.rate if nowt - t <= 60]
                r10 = sum(1 for t in rate if nowt - t <= 10)
                persec = [0] * 60
                for t in rate:
                    persec[59 - min(59, int(nowt - t))] += 1
                lat = sorted(st.lat)
                proc = sorted(st.proc)
                pq = lambda q: round(proc[min(len(proc) - 1, int(q * len(proc)))], 1) if proc else None      # noqa: E731
                rows.append({
                    "key": f"{key[0]}:{key[1]}", "instrument": st.inst.label, "symbol": st.inst.symbol, "segment": seg,
                    "kind": "dry run" if any((r.inst.segment, r.inst.security_id) == key for r in self.runs.values())
                    else ("recording" if st.watch else "—"),
                    "runs": [r.id for r in self.runs.values() if (r.inst.segment, r.inst.security_id) == key],
                    "status": status, "last_price": st.last_price, "day_high": st.day_high, "day_low": st.day_low, "volume": st.volume,
                    "age": None if age is None else round(age, 1), "rate10": round(r10 / 10, 1), "rate60": round(len(rate) / 60, 1),
                    "persec": persec, "packets": st.packets, "accepted": st.accepted, "dup": st.dup_skipped, "old": st.old_skipped,
                    "bad": st.bad_ticks, "jump_confirmed": st.jump_confirmed, "held": st.pending is not None,
                    "lat_min": round(lat[0], 2) if lat else None, "lat_med": round(lat[len(lat) // 2], 2) if lat else None,
                    "proc_avg_us": round(sum(proc) / len(proc), 1) if proc else None, "proc_p99_us": pq(0.99), "proc_max_us": round(st.proc_max_us, 1),
                    "tail": [{"t": t, "ltp": p, "vol": v} for t, p, v in list(st.tail)[:12]]})
            known = set(self.states)
        extra = [f"{a}:{b}" for a, b in sorted(getattr(feed, "subscribed", set())) if (a, b) not in known]
        return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "mode": self.s.feed_mode, "stale_seconds": stale, "instruments": rows,
                "subscribed_idle": extra,
                "feed": {"connected": bool(getattr(feed, "connected", False)), "fatal": getattr(feed, "fatal", None),
                         "disconnects": getattr(feed, "disconnects", 0), "subscribed": len(getattr(feed, "subscribed", [])),
                         "packet": getattr(feed, "packet", self.s.feed_packet), "ping_interval": getattr(feed, "ping_interval", None),
                         "ping_timeout": getattr(feed, "ping_timeout", None), "rtt_ms": getattr(feed, "rtt_ms", None),
                         "last_message_age": None if not getattr(feed, "last_message_at", None) else round((now - feed.last_message_at).total_seconds(), 1)},
                "writer_queue": self.writer.q.qsize(), "writer_errors": self.writer.errors, "store_ticks": self.s.store_ticks,
                "retention_days": self.retention_days(),
                "rest": {"configured": self.rest is not None, "last_latency_ms": None if self.rest is None else (round(self.rest.last_latency_ms) if self.rest.last_latency_ms else None)},
                "notes": [(t.strftime("%H:%M:%S"), l, m) for t, l, m in list(self.notes)[:12]]}

    def status_snapshot(self) -> dict:
        """Cheap version of snapshot() for the header badges that every page polls."""
        feed, now = self.feed, self.clock()
        with self.lock:
            n_runs = len(self.runs)
        return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "mode": self.s.feed_mode, "writer_errors": self.writer.errors, "active_runs": n_runs,
                "feed": {"connected": bool(getattr(feed, "connected", False)), "fatal": getattr(feed, "fatal", None),
                         "disconnects": getattr(feed, "disconnects", 0), "subscribed": len(getattr(feed, "subscribed", [])),
                         "last_message_age": None if not getattr(feed, "last_message_at", None) else round((now - feed.last_message_at).total_seconds(), 1)}}

    def snapshot(self) -> dict:
        now = self.clock()
        with self.lock:
            runs = []
            for r in self.runs.values():
                e = r.engine
                pos = e.position
                unreal = None
                if pos is not None and r.last_price:
                    sign = 1 if pos.side == "BUY" else -1
                    unreal = sign * (r.last_price - pos.entry_price) / pos.entry_price * 100
                runs.append({
                    "id": r.id, "label": r.label, "instrument": r.inst.label, "symbol": r.inst.symbol, "status": r.status,
                    "high": r.levels.high, "low": r.levels.low, "levels_source": r.levels.source + (f" {r.levels.from_date}" if r.levels.from_date else ""),
                    "strategy": r.params.strategy, "levels": e.levels(), "flags": dict(e.flags),
                    "last_price": r.last_price, "ticks": r.ticks,
                    "last_tick_age": None if r.last_tick is None else round((now - r.last_tick).total_seconds(), 1),
                    "position": None if pos is None else {"side": pos.side, "entry": pos.entry_price, "target": pos.target,
                                                          "stop": pos.stop, "unrealised_pct": unreal},
                    "summary": e.summary(), "events": list(r.events), "prices": list(r.recent)[-300:],
                    "status_text": self._status_text(r, now), "log": list(r.log)[:40], "trading_date": r.trading_date.isoformat(),
                    "params": r.params.as_dict(), "squareoff": r.squareoff_t.strftime("%H:%M")})
            watch = [{"instrument": st.inst.label, "last_price": st.last_price, "ticks": st.ticks_today,
                      "prev_close": st.prev_close, "bad_ticks": st.bad_ticks, "duplicates_skipped": st.dup_skipped} for st in self.states.values()
                     if st.watch]
        feed = self.feed
        return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "mode": self.s.feed_mode,
                "feed": {"connected": bool(getattr(feed, "connected", False)), "fatal": getattr(feed, "fatal", None),
                         "disconnects": getattr(feed, "disconnects", 0),
                         "subscribed": len(getattr(feed, "subscribed", [])),
                         "last_message_age": None if not getattr(feed, "last_message_at", None) else round((now - feed.last_message_at).total_seconds(), 1)},
                "writer_queue": self.writer.q.qsize(), "writer_errors": self.writer.errors,
                "runs": runs, "watch": watch, "notes": [(t.strftime("%H:%M:%S"), l, m) for t, l, m in list(self.notes)[:8]]}
