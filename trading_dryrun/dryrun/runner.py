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
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from . import levels as levels_mod
from .candles import MinuteAggregator
from .config import IST, Settings, session_for
from .db import ApiLog, Candle, Instrument, RiskLog, Run, Signal, Tick, Trade, purge_old_ticks
from .dhan_feed import FeedNote, FeedTick
from .risk import RiskContext, RiskEngine
from .strategy import BreakoutPullbackEngine, Event, Params

log = logging.getLogger("dryrun.runner")
MAX_JUMP = 0.20          # a single tick moving more than 20% from the previous one is treated as bad data


class LevelsNeeded(Exception):
    """Previous-day High/Low not found anywhere: the caller must provide them manually."""


class StartError(Exception):
    pass


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


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


@dataclass
class LiveRun:
    id: int
    inst: Instrument
    params: Params
    engine: BreakoutPullbackEngine
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
        self._cur_msg: datetime | None = None
        self._prev_msg: datetime | None = None
        self._task: asyncio.Task | None = None
        self._last_purge = datetime.min
        self.notes: deque = deque(maxlen=30)

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        with self.factory() as s:       # runs that were live when the process died cannot be resumed
            s.execute(update(Run).where(Run.status.in_(("starting", "waiting", "running"))).values(
                status="interrupted", ended_at=datetime.now(), ran_ok=False,
                error="process restarted while the run was live; ticks up to that point are stored"))
            s.commit()
        self.feed = self.feed_factory(self._on_tick, self._on_note, self._on_status)
        await self.feed.start()
        self._task = asyncio.create_task(self._supervisor(), name="supervisor")

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
        with self.lock:
            st = self.states.get((ft.segment, ft.security_id))
            if st is None or not (ft.ltp and ft.ltp > 0):
                return
            ts = ft.recv_ts or self.clock()
            if st.last_price and abs(ft.ltp / st.last_price - 1) > MAX_JUMP:
                st.bad_ticks += 1
                self.writer.submit(lambda s: s.add(ApiLog(source="feed", level="warn",
                                                          message=f"bad tick dropped {st.inst.symbol}: {st.last_price} -> {ft.ltp}")))
                return
            self._prev_msg, self._cur_msg = self._cur_msg, ts
            st.last_price, st.last_tick_at = ft.ltp, ts
            st.ticks_today += 1
            st.minutes_seen.add(ts.replace(second=0, microsecond=0))
            if self.s.store_ticks:
                self._tick_buf.append(Tick(instrument_id=st.inst.id, ts=ts, ltp=ft.ltp, ltt=ft.ltt, volume=ft.volume,
                                           day_high=ft.day_high, day_low=ft.day_low))
            bar = st.agg.add(ts, ft.ltp, ft.volume)
            if bar is not None:
                self._store_bar(st.inst.id, bar)
            for run in [r for r in self.runs.values() if r.inst.id == st.inst.id and not r.closed]:
                self._run_tick(run, ts, ft.ltp)

    def _store_bar(self, instrument_id: int, bar):
        source = "sim" if self.s.feed_mode == "sim" else "live"       # simulated prices must never pass as market data
        self.writer.submit(lambda s: s.add(Candle(instrument_id=instrument_id, interval="1m", ts=bar.ts, open=bar.open,
                                                  high=bar.high, low=bar.low, close=bar.close, volume=bar.volume, source=source)))

    def _flush_ticks(self):
        if self._tick_buf:
            buf, self._tick_buf = self._tick_buf, []
            self.writer.submit(lambda s: s.add_all(buf))

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

    def _log(self, run: LiveRun, text: str, ts: datetime | None = None):
        run.log.appendleft(((ts or self.clock()).strftime("%H:%M:%S"), text))

    def _describe(self, run: LiveRun, ev: Event) -> str | None:
        e, p = run.engine, ev.price
        if ev.kind == "break":
            up = ev.side == "BUY"
            return (f"Price {p:,.2f} broke {'above the previous High' if up else 'below the previous Low'} "
                    f"({run.levels.high if up else run.levels.low:,.2f}). Waiting for a pullback to "
                    f"{'<= ' + format(e.buy_entry_level, ',.2f') if up else '>= ' + format(e.sell_entry_level, ',.2f')} to take a {ev.side}.")
        if ev.kind == "entry_signal":
            return f"Pullback reached at {p:,.2f}: {ev.side} signal."
        if ev.kind == "entry_skipped":
            return f"{ev.side} signal NOT taken: {ev.reason}."
        if ev.kind == "entry":
            t = ev.trade
            return (f"SIMULATED {ev.side} entered at {t['entry_price']:,.2f} (target {t['target']:,.2f}, "
                    f"stop {'none' if t['stop'] is None else format(t['stop'], ',.2f')}).")
        if ev.kind == "exit":
            t = ev.trade
            return f"SIMULATED exit ({ev.reason}) at {t['exit_price']:,.2f}: net Rs {t['net_pnl']:,.2f} ({t['net_pct']:+.2f}%)."
        return None

    def _handle(self, run: LiveRun, events: list[Event]):
        for ev in events:
            text = self._describe(run, ev)
            if text:
                self._log(run, text, ev.ts)
            run.events.appendleft({"ts": ev.ts.strftime("%H:%M:%S"), "kind": ev.kind, "side": ev.side,
                                   "price": round(ev.price, 4), "reason": ev.reason})
            if ev.kind == "break":
                self._signal(run, ev, "break", "info", ev.reason)
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
                        if not self.s.ignore_market_hours and (now.date() > run.trading_date or now.time() >= run.squareoff_t):
                            self.finalize(run, "finished", "squareoff")
                        elif int(now.timestamp()) % 5 == 0:
                            self._update_run(run)
                    if (now - self._last_purge) > timedelta(hours=6):
                        self._last_purge = now
                        asyncio.get_running_loop().run_in_executor(None, purge_old_ticks, self.factory, self.s.tick_retention_days)
        except asyncio.CancelledError:
            pass

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
            status=status, ended_at=datetime.now(), ticks_received=run.ticks, first_tick_at=run.first_tick,
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
    async def start_run(self, inst: Instrument, params: Params, manual_levels: tuple[float, float] | None = None) -> dict:
        now = self.clock()
        open_t, last_entry_t, squareoff_t, close_t = session_for(inst.segment)
        if not self.s.ignore_market_hours:
            if now.weekday() >= 5 and inst.segment != "MCX_COMM":
                raise StartError("Market is closed (weekend).")
            if now.time() >= squareoff_t:
                raise StartError(f"Today's session is over (square-off {squareoff_t:%H:%M}); start a run before then.")
        params.validate()
        with self.lock:
            if any(r.inst.id == inst.id for r in self.runs.values()):
                raise StartError("A run is already active for this instrument.")
        if manual_levels:
            lv = levels_mod.set_manual(self.factory, inst.id, now.date(), manual_levels[0], manual_levels[1])
        else:
            lv = await asyncio.get_running_loop().run_in_executor(
                None, levels_mod.resolve, self.factory, inst, now.date(), self.rest)
        if lv is None:
            raise LevelsNeeded("Previous day High/Low not found in the database or from Dhan; enter them manually.")
        try:
            engine_params = params
            with self.factory() as s:
                run = Run(instrument_id=inst.id, status="waiting", trading_date=now.date(), params=engine_params.as_dict(),
                          prev_high=lv.high, prev_low=lv.low, levels_source=lv.source)
                s.add(run)
                s.commit()
                run_id = run.id
        except Exception as e:      # noqa: BLE001
            raise StartError(f"could not create the run: {e}")
        holder: dict = {}

        def approve(side, ts, price, state):
            return holder["risk"].approve(side, ts, price, state)

        engine = BreakoutPullbackEngine(lv.high, lv.low, params, approve=approve)
        live = LiveRun(run_id, inst, params, engine, now.date(), lv, open_t, last_entry_t, squareoff_t,
                       started_disconnects=getattr(self.feed, "disconnects", 0) or 0)
        holder["risk"] = RiskEngine(RiskContext(
            last_tick_at=lambda: self._prev_msg, now=lambda: self._cur_msg or self.clock(), last_entry_time=last_entry_t,
            session_open=open_t, stale_seconds=self.s.stale_tick_seconds, ignore_hours=self.s.ignore_market_hours,
            log=lambda check, ok, detail: self.writer.submit(lambda s: s.add(RiskLog(run_id=run_id, check=check, approved=ok, detail=detail[:200])))))
        with self.lock:
            self.runs[run_id] = live
            key = (inst.segment, inst.security_id)
            if key not in self.states:
                self.states[key] = InstState(inst)
        self._log(live, f"Run started. Previous-day High {lv.high:,.2f}, Low {lv.low:,.2f} (source: {lv.source}). "
                        f"Pullback {params.pullback_pct}%, target {params.target_pct}%, stop "
                        f"{'none' if params.stop_pct is None else str(params.stop_pct) + '%'}.")
        await self.feed.subscribe([key])
        self._log(live, f"Subscribed to {inst.label}. Waiting for prices.")
        if hasattr(self.feed, "set_start_price"):       # simulated feed: start in the middle of yesterday's range
            self.feed.set_start_price(key, (lv.high + lv.low) / 2)
        return {"id": run_id, "levels": {"high": lv.high, "low": lv.low, "source": lv.source}}

    def stop_run(self, run_id: int) -> bool:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                return False
            self.finalize(run, "stopped", "manual")
            return True

    async def watch(self, inst: Instrument, on: bool = True):
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
        notes = []
        if e.armed["BUY"]:
            notes.append(f"Price broke above the previous High ({r.levels.high:,.2f}); waiting for a pullback to {e.buy_entry_level:,.2f} or lower to BUY (now {p:,.2f}).")
        if e.armed["SELL"]:
            notes.append(f"Price broke below the previous Low ({r.levels.low:,.2f}); waiting for a pullback to {e.sell_entry_level:,.2f} or higher to SELL (now {p:,.2f}).")
        if notes:
            return prefix + " ".join(notes)
        up_pct, dn_pct = (e.break_up_level / p - 1) * 100, (1 - e.break_down_level / p) * 100
        where = "inside" if r.levels.low <= p <= r.levels.high else "outside"
        return (f"{prefix}Price {p:,.2f} is {where} yesterday's range ({r.levels.low:,.2f} to {r.levels.high:,.2f}). "
                f"A setup starts if it trades above {e.break_up_level:,.2f} ({up_pct:+.2f}%) or below {e.break_down_level:,.2f} ({-dn_pct:+.2f}%).")

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
                    "id": r.id, "instrument": r.inst.label, "symbol": r.inst.symbol, "status": r.status,
                    "high": r.levels.high, "low": r.levels.low, "levels_source": r.levels.source,
                    "buy_trigger": e.buy_entry_level, "sell_trigger": e.sell_entry_level,
                    "break_up": e.break_up_level, "break_down": e.break_down_level,
                    "armed_buy": e.armed["BUY"], "armed_sell": e.armed["SELL"],
                    "last_price": r.last_price, "ticks": r.ticks,
                    "last_tick_age": None if r.last_tick is None else round((now - r.last_tick).total_seconds(), 1),
                    "position": None if pos is None else {"side": pos.side, "entry": pos.entry_price, "target": pos.target,
                                                          "stop": pos.stop, "unrealised_pct": unreal},
                    "summary": e.summary(), "events": list(r.events), "prices": list(r.recent)[-300:],
                    "status_text": self._status_text(r, now), "log": list(r.log)[:40],
                    "params": r.params.as_dict(), "squareoff": r.squareoff_t.strftime("%H:%M")})
            watch = [{"instrument": st.inst.label, "last_price": st.last_price, "ticks": st.ticks_today,
                      "prev_close": st.prev_close, "bad_ticks": st.bad_ticks} for st in self.states.values()
                     if st.watch]
        feed = self.feed
        return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "mode": self.s.feed_mode,
                "feed": {"connected": bool(getattr(feed, "connected", False)), "fatal": getattr(feed, "fatal", None),
                         "disconnects": getattr(feed, "disconnects", 0),
                         "subscribed": len(getattr(feed, "subscribed", [])),
                         "last_message_age": None if not getattr(feed, "last_message_at", None) else round((now - feed.last_message_at).total_seconds(), 1)},
                "writer_queue": self.writer.q.qsize(), "writer_errors": self.writer.errors,
                "runs": runs, "watch": watch, "notes": [(t.strftime("%H:%M:%S"), l, m) for t, l, m in list(self.notes)[:8]]}
