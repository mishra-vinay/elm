"""Backtest of the range-thirds strategy on stored 1-minute candles, plus a parameter sweep with a first-half / second-half split.

  python -m dryrun.strategies.range_thirds_backtest --symbol BAJFINANCE --symbol RELIANCE        # stop on the losing side, both entry modes
  python -m dryrun.strategies.range_thirds_backtest --symbol BAJFINANCE --symbol RELIANCE --grid # sweep, tuned on the first half, judged on the second
  python -m dryrun.strategies.range_thirds_backtest --symbol BAJFINANCE --verify                 # prove the fast simulator equals the live engine
(add --stop-side outside for the stop beyond the level). The online Backtest page runs the same engine for a single setting.

Each 1-minute candle becomes the same short price path the live engine's replay uses (open -> low -> high -> close for an up bar,
open -> high -> low -> close for a down bar), with a tick at every strategy line the path crosses so orders fill at their level. `sim_day` is a fast re-implementation of RangeThirdsEngine used for the sweep;
`--verify` runs both on the same data and requires identical trades, so the sweep numbers can be trusted.
"""
from __future__ import annotations

import argparse
import itertools
import math
from collections import defaultdict
from datetime import date, time

from ..replay import candle_path, replay_range
from ..strategy import Params


def load_days(factory, symbol: str, segment: str = "NSE_EQ", interval: str = "1m", skip_short: int = 300):
    """[(date, prev_high, prev_low, path_points)] for every session that has a previous session. Days with fewer than `skip_short`
    bars (muhurat sessions, data gaps) are dropped from both roles."""
    from sqlalchemy import select
    from ..db import Candle, Instrument
    with factory() as s:
        inst = s.scalar(select(Instrument).where(Instrument.symbol == symbol, Instrument.segment == segment))
        if inst is None:
            raise SystemExit(f"{symbol} ({segment}) is not in the instrument table")
        rows = s.scalars(select(Candle).where(Candle.instrument_id == inst.id, Candle.interval == interval,
                                              Candle.source == "api").order_by(Candle.ts)).all()
    by: dict[date, list] = defaultdict(list)
    for c in rows:
        by[c.ts.date()].append(c)
    days = [d for d in sorted(by) if len(by[d]) >= skip_short]
    out = []
    for prev, cur in zip(days, days[1:]):
        hi, lo = max(c.high for c in by[prev]), min(c.low for c in by[prev])
        pts: list[list[float]] = []
        for c in by[cur]:
            if c.ts.time() >= time(15, 15):
                break
            pts.append(candle_path(c.open, c.high, c.low, c.close))
        if hi > lo and pts:
            out.append((cur, hi, lo, pts))
    return out


def sim_day(high, low, paths, ef, sf, tf, mode, slip=0.02, cost=0.10, max_day=2, loss_lim=2.0, stop_side="inside", gap_filter=True) -> list[float]:
    """Net % of every trade of one session; `paths` is one open/low/high/close path per candle. Same rules as RangeThirdsEngine through replay's
    `feed_path`: one position at a time, one trade per side, `max_day` trades, stop checked before target, market stop (slippage) and limit target
    (none), forced exit at the last point, and a tick at exactly every level a leg of the path crosses (so orders fill at their level)."""
    d = high - low
    outside = stop_side == "outside"
    trig_b, trig_s, sl = high - ef * d, low + ef * d, slip / 100
    stop_b, stop_s = (high + sf * d, low - sf * d) if outside else (high - sf * d, low + sf * d)
    base = [high, low, trig_b, trig_s, stop_b, stop_s]
    armed = {1: False, -1: False}
    n = {1: 0, -1: 0}
    pos = None
    out: list[float] = []
    # day-open filter (approach mode): the day opened beyond this side's level, so that side is off for the day
    opened = paths[0][0]
    off = {1: gap_filter and mode == "approach" and opened > high, -1: gap_filter and mode == "approach" and opened < low}

    def tick(px):
        nonlocal pos
        if pos is not None:
            side, e, stop, tgt = pos
            if side > 0:
                if (px >= stop) if outside else (px <= stop):
                    out.append(((px - px * sl - e) - e * cost / 100) / e * 100); pos = None
                elif px >= tgt:
                    out.append(((tgt - e) - e * cost / 100) / e * 100); pos = None
            else:
                if (px <= stop) if outside else (px >= stop):
                    out.append(((e - (px + px * sl)) - e * cost / 100) / e * 100); pos = None
                elif px <= tgt:
                    out.append(((e - tgt) - e * cost / 100) / e * 100); pos = None
        for side in (1, -1):
            trig = trig_b if side > 0 else trig_s
            if not armed[side]:
                if off[side]:
                    continue
                if mode == "approach":
                    ready = px < trig if side > 0 else px > trig
                else:
                    ready = px > high if side > 0 else px < low
                if ready and n[side] < 1:
                    armed[side] = True
                continue
            hit = (px >= trig if side > 0 else px <= trig) if mode == "approach" else (px <= trig if side > 0 else px >= trig)
            if not hit:
                continue
            armed[side] = False
            if pos is not None or n[1] + n[-1] >= max_day or sum(out) <= -loss_lim:
                continue
            e = px + px * sl if side > 0 else px - px * sl
            pos = (1, e, stop_b, e + tf * d) if side > 0 else (-1, e, stop_s, e - tf * d)
            n[side] += 1

    for path in paths:
        last = None
        for p in path:
            if last is not None:
                while True:
                    marks = base + ([pos[2], pos[3]] if pos is not None else [])
                    cands = [v for v in marks if last < v < p or p < v < last]
                    if not cands:
                        break
                    last = min(cands, key=lambda v: abs(v - last))
                    tick(last)
            tick(p)
            last = p
    if pos is not None:
        side, e, _, _ = pos
        px = paths[-1][-1]
        xp = px - px * sl if side > 0 else px + px * sl
        out.append((side * (xp - e) - e * cost / 100) / e * 100)
    return out


def run(days, ef, sf, tf, mode, **kw) -> list[tuple[date, float]]:
    return [(d, r) for d, hi, lo, pts in days for r in sim_day(hi, lo, pts, ef, sf, tf, mode, **kw)]


def stats(x) -> dict:
    x = [float(v) for v in x]
    n = len(x)
    if n < 5:
        return {"n": n, "mean": float("nan"), "t": float("nan"), "wr": float("nan"), "pf": float("nan"), "tot": sum(x)}
    mean = sum(x) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in x) / (n - 1))
    pos, neg = sum(v for v in x if v > 0), -sum(v for v in x if v < 0)
    return {"n": n, "mean": mean, "t": mean / (sd / math.sqrt(n)) if sd > 0 else float("nan"),
            "wr": 100 * sum(1 for v in x if v > 0) / n, "pf": pos / neg if neg > 0 else float("inf"), "tot": sum(x)}


def verify(factory, symbol: str, segment: str = "NSE_EQ") -> bool:
    """Run the live engine (through replay_range) and the fast simulator on the same candles; every trade must match."""
    from sqlalchemy import select
    from ..db import Candle, Instrument
    days = load_days(factory, symbol, segment)
    with factory() as s:
        inst = s.scalar(select(Instrument).where(Instrument.symbol == symbol, Instrument.segment == segment))
        rows = s.scalars(select(Candle).where(Candle.instrument_id == inst.id, Candle.interval == "1m", Candle.source == "api").order_by(Candle.ts)).all()
    by: dict[date, int] = defaultdict(int)
    for c in rows:
        by[c.ts.date()] += 1
    ok_days = {d for d, n in by.items() if n >= 300}
    candles = [{"ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in rows if c.ts.date() in ok_days]
    good = True
    from .range_thirds_research import CLASSIC
    for ef, sf, tf, mode, side in [(1 / 6, 1 / 3, 1 / 3, "approach", "outside"), (1 / 6, 1 / 3, 1 / 3, "after_break", "outside"),
                                   (1 / 6, 1 / 3, 1 / 3, "approach", "inside"), (1 / 6, 1 / 3, 1 / 3, "after_break", "inside"),
                                   (0.25, 0.5, 0.5, "approach", "inside"), (0.0, 0.25, 1 / 6, "after_break", "inside"), (0.0, 0.1, 0.5, "approach", "outside")]:
        P = Params(strategy="range_thirds_research", options={**CLASSIC, "entry_frac": ef, "stop_frac": sf, "target_frac": tf, "entry_mode": mode, "stop_side": side})
        eng = [t["net_pct"] for r in replay_range(candles, P, segment)["per_day"].values() for t in r["trade_list"]]
        fast = [r for _, r in run(days, ef, sf, tf, mode, stop_side=side)]
        same = len(eng) == len(fast) and all(abs(a - b) < 1e-9 for a, b in zip(eng, fast))
        good &= same
        print(f"  {mode:11s} {side:7s} stop entry {ef:.3f} stop {sf:.3f} target {tf:.3f}: engine {len(eng)} trades, fast {len(fast)} trades, identical: {same}")
    return good


def main():     # pragma: no cover  (needs a populated database)
    from ..__main__ import _load_env
    from ..config import Settings
    from ..db import init_db
    _load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", action="append", required=True)
    ap.add_argument("--segment", default="NSE_EQ")
    ap.add_argument("--split", default=None, help="first-half / second-half split date (default: the middle session)")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--stop-side", default="inside", choices=["outside", "inside"], help="inside = stop on the losing side of the entry (default); outside = BUY stop above the High, SELL stop below the Low")
    ap.add_argument("--no-gap-filter", action="store_true", help="trade even when the day opens beyond the level (approach mode)")
    ap.add_argument("--cost", type=float, default=0.10)
    ap.add_argument("--slippage", type=float, default=0.02)
    a = ap.parse_args()
    _e, factory = init_db(Settings().database_url)
    if a.verify:
        raise SystemExit(0 if all(verify(factory, s, a.segment) for s in a.symbol) else 1)
    data = {s: load_days(factory, s, a.segment) for s in a.symbol}
    first = sorted(d for v in data.values() for d, *_ in v)
    split = date.fromisoformat(a.split) if a.split else first[len(first) // 2]
    kw = {"cost": a.cost, "slip": a.slippage, "stop_side": a.stop_side, "gap_filter": not a.no_gap_filter}
    print(f"sessions: {({s: len(v) for s, v in data.items()})}; first half before {split}; cost {a.cost}% + slippage {a.slippage}%/side")
    if not a.grid:
        for mode in ("approach", "after_break"):
            for s, v in data.items():
                r = run(v, 1 / 6, 1 / 3, 1 / 3, mode, **kw)
                st, i, o = stats(x for _, x in r), stats(x for d, x in r if d < split), stats(x for d, x in r if d >= split)
                print(f"{mode:11s} {s:12s} trades {st['n']:3d} win {st['wr']:.0f}% mean {st['mean']:+.3f}%/trade t={st['t']:.2f} PF {st['pf']:.2f} "
                      f"total {st['tot']:+.1f}% | first half {i['mean']:+.3f} | second half {o['mean']:+.3f}")
        return
    rows = []
    for mode, ef, sf, tf in itertools.product(("approach", "after_break"), (0, 1 / 12, 1 / 6, 1 / 4, 1 / 3), (1 / 4, 1 / 3, 1 / 2, 2 / 3), (1 / 6, 1 / 3, 1 / 2, 1)):
        if sf <= ef:
            continue
        allr = [x for v in data.values() for x in run(v, ef, sf, tf, mode, **kw)]
        i, o, al = stats(r for d, r in allr if d < split), stats(r for d, r in allr if d >= split), stats(r for _, r in allr)
        rows.append((mode, ef, sf, tf, i, o, al))
    for mode in ("approach", "after_break"):
        M = [r for r in rows if r[0] == mode]
        print(f"\n{mode}: {len(M)} configs, positive over the full period: {sum(1 for r in M if r[6]['mean'] > 0)}, with t>2: {sum(1 for r in M if r[6]['t'] > 2)}")
        print("  top 5 by first-half mean (>=60 trades) -> second half:")
        for r in sorted((r for r in M if r[4]["n"] >= 60), key=lambda r: -r[4]["mean"])[:5]:
            print(f"   entry {r[1]:.3f} stop {r[2]:.3f} target {r[3]:.3f}: first half {r[4]['mean']:+.3f}% (n={r[4]['n']}) -> second half {r[5]['mean']:+.3f}% (t={r[5]['t']:.1f}, n={r[5]['n']})")


if __name__ == "__main__":
    main()
