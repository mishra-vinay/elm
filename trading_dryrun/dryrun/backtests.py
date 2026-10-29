"""Online backtests: run the live strategy engine (the same class the dry runs use) over stored candles and summarise the result.

The Backtest page picks a stored dataset (instrument + candle interval), a date range and the strategy settings. Each candle becomes a short
price path (open -> low -> high -> close for an up bar, open -> high -> low -> close for a down bar), so intraday 1-minute data is much more
faithful than 15- or 60-minute data. Previous-day High/Low come from the previous session in the same data. Simulated (source 'sim')
candles are never used.

One backtest runs at a time in a background thread (a year of 1-minute data takes a few seconds); results are kept in memory.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .config import session_for
from .db import Candle, Instrument
from .replay import replay_range
from .strategy import Params

MAX_CANDLES = 600_000
INTRADAY = ("1m", "5m", "15m", "25m", "60m")


class BacktestError(Exception):
    pass


def _mean(x):
    return sum(x) / len(x) if x else None


def _tstat(x):
    n = len(x)
    if n < 3:
        return None
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))
    return None if sd == 0 else m / (sd / math.sqrt(n))


def summarise(trades: list[dict], sessions: dict[date, float], n_sessions: int, capital: float = 100_000.0) -> dict:
    """trades: dicts with date, side, net_pct, exit_reason (sorted by entry time) and optionally net_pnl (rupees) and exit_dt;
    sessions: net % summed per tested session; capital: the starting capital the rupee result is shown against."""
    pct = [t["net_pct"] for t in trades]
    wins = [v for v in pct if v > 0]
    losses = [v for v in pct if v <= 0]
    gp, gl = sum(wins), -sum(losses)
    eq, peak, mdd, curve = 0.0, 0.0, 0.0, []
    for t in trades:
        eq += t["net_pct"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        curve.append([t["date"], round(eq, 4)])
    rupees = {}
    if trades and all(t.get("net_pnl") is not None for t in trades):         # TradingView-style result in rupees, against the starting capital
        cum, rpeak, rdd, rdd_pct, rcurve = 0.0, 0.0, 0.0, 0.0, []
        for t in trades:
            cum += t["net_pnl"]
            rpeak = max(rpeak, cum)
            rdd = max(rdd, rpeak - cum)
            rdd_pct = max(rdd_pct, (rpeak - cum) / (capital + rpeak) * 100) if capital + rpeak > 0 else rdd_pct
            rcurve.append([t.get("exit_dt") or t["date"], round(cum, 2)])
        rupees = {"capital": capital, "net_pnl": round(cum, 2), "net_pnl_pct": round(cum / capital * 100, 2), "max_drawdown_rs": round(rdd, 2),
                  "max_drawdown_rs_pct": round(rdd_pct, 2), "equity_rs": rcurve}
    daily = list(sessions.values())
    sharpe = None
    if len(daily) > 20:
        m = sum(daily) / len(daily)
        sd = math.sqrt(sum((v - m) ** 2 for v in daily) / (len(daily) - 1))
        sharpe = round(m / sd * math.sqrt(252), 2) if sd > 0 else None

    def group(keyf):
        g: dict = defaultdict(list)
        for t in trades:
            g[keyf(t)].append(t["net_pct"])
        return [{"key": k, "trades": len(v), "win_rate": round(100 * sum(1 for x in v if x > 0) / len(v), 1),
                 "mean": round(sum(v) / len(v), 4), "total": round(sum(v), 3)} for k, v in sorted(g.items())]

    mid = sorted(sessions)[len(sessions) // 2] if sessions else None
    first = [t["net_pct"] for t in trades if mid and t["date"] < mid.isoformat()]
    second = [t["net_pct"] for t in trades if mid and t["date"] >= mid.isoformat()]
    t = _tstat(pct)
    warnings = []
    if len(pct) < 30:
        warnings.append(f"Only {len(pct)} trades: too few to say anything reliable.")
    if t is not None and abs(t) < 2:
        warnings.append("The t-statistic is below 2 in size: the average trade is not distinguishable from zero (luck).")
    if first and second and (_mean(first) > 0) != (_mean(second) > 0):
        warnings.append("The first and second half of the period disagree in sign: the result is not stable over time.")
    return {
        "sessions": n_sessions, "sessions_with_trade": len({x["date"] for x in trades}), "trades": len(pct), "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(pct), 1) if pct else None, "mean": round(_mean(pct), 4) if pct else None,
        "total": round(sum(pct), 3), "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "t_stat": round(t, 2) if t is not None else None, "avg_win": round(_mean(wins), 4) if wins else None,
        "avg_loss": round(_mean(losses), 4) if losses else None, "max_drawdown": round(mdd, 3), "sharpe_sessions": sharpe,
        "first_half_mean": round(_mean(first), 4) if first else None, "second_half_mean": round(_mean(second), 4) if second else None,
        **rupees, "split_date": mid.isoformat() if mid else None, "by_side": group(lambda x: x["side"]),
        "by_exit": group(lambda x: x["exit_reason"]), "by_month": group(lambda x: x["date"][:7]), "equity": curve, "warnings": warnings}


def run_backtest(factory: sessionmaker, instrument_id: int, interval: str, params: Params, date_from: date | None = None,
                 date_to: date | None = None, min_bars_pct: float = 80.0, capital: float = 100_000.0) -> dict:
    if interval not in INTRADAY:
        raise BacktestError("choose an intraday dataset (1m, 5m, 15m, 25m or 60m); the strategy trades inside the session")
    params.validate()
    with factory() as s:
        inst = s.get(Instrument, instrument_id)
        if inst is None:
            raise BacktestError("instrument not found")
        q = select(Candle).where(Candle.instrument_id == instrument_id, Candle.interval == interval, Candle.source != "sim")
        if date_from:
            q = q.where(Candle.ts >= datetime.combine(date_from, datetime.min.time()))
        if date_to:
            q = q.where(Candle.ts < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
        rows = s.scalars(q.order_by(Candle.ts).limit(MAX_CANDLES + 1)).all()
    if len(rows) > MAX_CANDLES:
        raise BacktestError(f"more than {MAX_CANDLES:,} candles selected; narrow the date range")
    if not rows:
        raise BacktestError("no candles in that dataset and date range")
    by: dict[date, list] = defaultdict(list)
    for c in rows:
        by[c.ts.date()].append(c)
    # a session is complete enough when it has at least min_bars_pct % of the bars a FULL exchange session has at this interval. (Not the median of
    # the selected range: that made the chosen days depend on the other days in the data, so cutting the data off earlier could change earlier trades.)
    open_t, _last, _sq, close_t = session_for(inst.segment)
    expected = max(1, math.ceil(((close_t.hour * 60 + close_t.minute) - (open_t.hour * 60 + open_t.minute)) / int(interval.rstrip("m"))))
    keep = {d for d, v in by.items() if len(v) >= expected * min_bars_pct / 100}
    dropped = sorted(d.isoformat() for d in by if d not in keep)
    candles = [{"ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close} for d in sorted(keep) for c in by[d]]
    if len(keep) < 3:
        raise BacktestError("fewer than 3 complete sessions in that range")
    t0 = time.time()
    res = replay_range(candles, params, inst.segment, inst.symbol)
    trades, sessions = [], {}
    for d, r in sorted(res["per_day"].items()):
        sessions[d] = sum(t["net_pct"] for t in r["trade_list"])
        for t in r["trade_list"]:
            trades.append({"date": d.isoformat(), "side": t["side"], "entry_ts": t["entry_ts"].strftime("%H:%M"), "exit_ts": t["exit_ts"].strftime("%H:%M"),
                           "entry_dt": t["entry_ts"].strftime("%Y-%m-%d %H:%M:%S"), "exit_dt": t["exit_ts"].strftime("%Y-%m-%d %H:%M:%S"),
                           "entry": round(t["entry_price"], 4), "exit": round(t["exit_price"], 4), "stop": t.get("stop"), "target": t.get("target"),
                           "exit_reason": t["exit_reason"], "net_pct": round(t["net_pct"], 4), "net_pnl": round(t["net_pnl"], 2), "qty": t.get("qty")})
    out = summarise(trades, sessions, res["sessions"], capital)
    out.update(instrument_id=inst.id, day_levels={d.isoformat(): r["levels"] for d, r in res["per_day"].items()},
               instrument=inst.label, symbol=inst.symbol, segment=inst.segment, interval=interval, candles=len(candles),
               from_date=min(keep).isoformat(), to_date=max(keep).isoformat(), dropped_sessions=dropped, params=params.as_dict(),
               seconds=round(time.time() - t0, 1), trade_list=trades)
    return out


def day_view(factory: sessionmaker, result: dict, day: date) -> dict:
    """One tested session for the day chart: its candles, the strategy's lines for that day and the trades taken."""
    iso = day.isoformat()
    levels = (result.get("day_levels") or {}).get(iso)
    if levels is None:
        raise BacktestError("that date was not part of this backtest (the first session only supplies the previous-day High/Low)")
    with factory() as s:
        rows = s.scalars(select(Candle).where(Candle.instrument_id == result["instrument_id"], Candle.interval == result["interval"], Candle.source != "sim",
                                              Candle.ts >= datetime.combine(day, datetime.min.time()),
                                              Candle.ts < datetime.combine(day + timedelta(days=1), datetime.min.time())).order_by(Candle.ts)).all()
    return {"date": iso, "symbol": result["symbol"], "interval": result["interval"], "levels": levels,
            "trades": [t for t in result["trade_list"] if t["date"] == iso],
            "candles": [[c.ts.strftime("%H:%M"), c.open, c.high, c.low, c.close] for c in rows]}


class BacktestJobs:
    """One backtest at a time; the last few results are kept in memory."""

    def __init__(self, factory: sessionmaker, keep: int = 12):
        self.factory, self.keep = factory, keep
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def running(self) -> bool:
        return any(j["status"] == "running" for j in self.jobs.values())

    def start(self, instrument_id: int, interval: str, params: Params, date_from, date_to, min_bars_pct: float, title: str, capital: float = 100_000.0) -> str:
        with self._lock:
            if self.running():
                raise BacktestError("another backtest is still running; wait for it to finish")
            jid = uuid.uuid4().hex[:10]
            job = {"id": jid, "status": "running", "title": title, "started": time.time(), "result": None, "error": None}
            self.jobs[jid] = job
            for old in sorted(self.jobs, key=lambda k: self.jobs[k]["started"])[:-self.keep]:
                self.jobs.pop(old, None)

        def work():
            try:
                job["result"] = run_backtest(self.factory, instrument_id, interval, params, date_from, date_to, min_bars_pct, capital)
                job["status"] = "done"
            except BacktestError as e:
                job.update(status="error", error=str(e))
            except Exception as e:      # noqa: BLE001
                job.update(status="error", error=f"backtest failed: {str(e)[:200]}")
            job["elapsed"] = round(time.time() - job["started"], 1)

        threading.Thread(target=work, name="backtest", daemon=True).start()
        return jid

    def get(self, jid: str) -> dict | None:
        j = self.jobs.get(jid)
        if j is None:
            return None
        out = {k: v for k, v in j.items() if k != "result"}
        out["elapsed"] = j.get("elapsed", round(time.time() - j["started"], 1))
        out["result"] = j["result"]
        return out
