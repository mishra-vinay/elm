"""Run the live strategy engine over stored historical candles (same class the dry run uses).

Each candle becomes a short price path (open -> low -> high -> close for an up bar, open -> high -> low -> close
for a down bar), which is a standard approximation: order inside a bar is unknown, so use 1-minute candles where
possible. Previous-day High/Low come from the previous session in the same data.

  python -m dryrun.replay --symbol BAJFINANCE --interval 1m --from 2026-01-01 --to 2026-10-09
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from .config import session_for
from .strategy import BreakoutPullbackEngine, Params


def candle_path(o: float, h: float, l: float, c: float) -> list[float]:
    return [o, l, h, c] if c >= o else [o, h, l, c]


def replay_day(candles: list[dict], high: float, low: float, params: Params, squareoff: time | None = None) -> dict:
    """candles: dicts with ts, open, high, low, close, ascending. Returns the engine summary plus the trade list."""
    eng = BreakoutPullbackEngine(high, low, params)
    last_ts = None
    for c in candles:
        if squareoff and c["ts"].time() >= squareoff:
            break
        for i, p in enumerate(candle_path(c["open"], c["high"], c["low"], c["close"])):
            ts = c["ts"] + timedelta(seconds=i)
            eng.on_tick(ts, p)
            last_ts = ts
    if last_ts is not None:
        eng.square_off(last_ts, reason="squareoff")
    out = eng.summary()
    out["trade_list"] = eng.closed
    return out


def replay_range(candles: list[dict], params: Params, segment: str = "NSE_EQ") -> dict:
    """Group candles by date; each day uses the previous day's High/Low. Returns totals and per-day results."""
    by_day: dict[date, list[dict]] = defaultdict(list)
    for c in sorted(candles, key=lambda x: x["ts"]):
        by_day[c["ts"].date()].append(c)
    days = sorted(by_day)
    _o, _e, squareoff, _c = session_for(segment)
    per_day, trades = {}, []
    for prev, cur in zip(days, days[1:]):
        hi, lo = max(c["high"] for c in by_day[prev]), min(c["low"] for c in by_day[prev])
        if not hi > lo:
            continue
        r = replay_day(by_day[cur], hi, lo, params, squareoff)
        per_day[cur] = r
        trades.extend(r["trade_list"])
    wins = [t for t in trades if t["net_pnl"] > 0]
    return {"sessions": len(per_day), "sessions_with_trade": sum(1 for r in per_day.values() if r["trades"]),
            "trades": len(trades), "wins": len(wins), "win_rate": len(wins) / len(trades) if trades else None,
            "net_pnl": sum(t["net_pnl"] for t in trades), "net_pct_mean": (sum(t["net_pct"] for t in trades) / len(trades)) if trades else None,
            "targets": sum(1 for t in trades if t["exit_reason"] == "target"),
            "stops": sum(1 for t in trades if t["exit_reason"] == "stop"), "per_day": per_day}


def main():     # pragma: no cover
    from .__main__ import _load_env
    from .config import Settings
    from .db import Candle, init_db
    from .instruments import search
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--pullback", type=float, default=1.0)
    ap.add_argument("--target", type=float, default=2.0)
    ap.add_argument("--stop", type=float, default=1.0)
    a = ap.parse_args()
    _e, factory = init_db(Settings().database_url)
    hit = search(factory, a.symbol, 1)
    if not hit:
        raise SystemExit("instrument not found")
    with factory() as s:
        rows = s.scalars(select(Candle).where(Candle.instrument_id == hit[0].id, Candle.interval == a.interval,
                                              Candle.ts >= datetime.fromisoformat(a.start),
                                              Candle.ts < datetime.fromisoformat(a.end) + timedelta(days=1)).order_by(Candle.ts)).all()
    candles = [{"ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in rows]
    if not candles:
        raise SystemExit("no stored candles for that range; run dryrun.history first")
    res = replay_range(candles, Params(pullback_pct=a.pullback, target_pct=a.target, stop_pct=a.stop), hit[0].segment)
    res.pop("per_day")
    print(res)


if __name__ == "__main__":
    main()
