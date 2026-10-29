#!/usr/bin/env python
"""Independent verification of the backtest on real stored candles (default 1-minute).

It does NOT reuse the engine's fill code. It re-implements the Range thirds rules ("dev" rule) from the written specification with a different
mechanism, then compares trade by trade with what the Backtest page's engine produces (`dryrun.backtests.run_backtest`, the code under test):

  1. DATA      candle sanity: OHLC consistency, duplicates, timestamps outside the session, missing minutes, flat/zero-volume bars.
  2. ORACLE    each candle becomes a DENSE tick path (linear steps of 0.004% of price along open -> low -> high -> close), so a level is crossed
               by the first tick at or beyond it, with no "insert a tick at the level" logic; plain tick-by-tick rules decide arming, entry,
               stop, target, cut-off, one trade a day. Every engine trade must match: date, side, entry/exit minute, exit reason, prices within a tick.
  3. INVARIANTS per trade: entry minute inside the window, one trade a day, prices inside the candle that filled them, P&L arithmetic,
               stop = entry level -/+ dev, target = entry level +/- (D/3 + (D/3 - dev)), exit before the square-off.
  4. LEVELS    the previous-day High/Low and trigger lines the engine drew for every day against an independent aggregation.
  5. LOOK-AHEAD truncating the data at several dates must not change any earlier trade.
  6. STATS     the summary (counts, total, profit factor, t-stat, drawdown) recomputed from the trade list.
  7. PATH      how much the result depends on the unknown order of high and low inside a candle (four rules).

  python scripts/verify_backtest.py --db sqlite:///data/dryrun.db --symbol BAJFINANCE --segment NSE_EQ
  python scripts/verify_backtest.py --db sqlite:///data/dryrun.db --instrument-id 14420 --from 2026-09-14        # crude oil mini, liquid part
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select                      # noqa: E402

from dryrun.backtests import run_backtest          # noqa: E402
from dryrun.config import SESSIONS                 # noqa: E402
from dryrun.db import Candle, Instrument, init_db  # noqa: E402
from dryrun.strategy import Params                 # noqa: E402
import dryrun.strategies                          # noqa: E402,F401  (registers the installed strategies, as the app does)

# independent copy of the session facts (open, square-off): asserted equal to the config so a drift is reported
SESS = {"NSE_EQ": (time(9, 15), time(15, 15)), "MCX_COMM": (time(9, 0), time(23, 20))}
CLOSE = {"NSE_EQ": time(15, 30), "MCX_COMM": time(23, 30)}
STEP_PCT = 0.004          # dense tick step, % of price
TOL = 2e-3                # the engine's trade list rounds prices to 4 decimals
FAILS: list[str] = []


def check(ok: bool, text: str) -> bool:
    print(("  PASS  " if ok else "  FAIL  ") + text)
    if not ok:
        FAILS.append(text)
    return ok


# ------------------------------------------------------------------------------------------------ data
def load(factory, a):
    with factory() as s:
        inst = s.get(Instrument, a.instrument_id) if a.instrument_id else s.scalar(select(Instrument).where(Instrument.symbol == a.symbol, Instrument.segment == a.segment))
        if inst is None:
            raise SystemExit("instrument not found")
        q = select(Candle).where(Candle.instrument_id == inst.id, Candle.interval == a.interval, Candle.source != "sim")
        if a.date_from:
            q = q.where(Candle.ts >= datetime.fromisoformat(a.date_from))
        rows = s.scalars(q.order_by(Candle.ts)).all()
        return inst, [(c.ts, c.open, c.high, c.low, c.close, c.volume or 0.0) for c in rows]


def data_checks(rows, seg, minutes):
    print("\n1. DATA")
    open_t = SESS[seg][0]
    bad = sum(1 for _, o, h, l, c, _v in rows if h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9 or h < l or min(o, h, l, c) <= 0)
    check(bad == 0, f"OHLC consistent in all {len(rows):,} candles ({bad} violations)")
    ts = [r[0] for r in rows]
    check(len(ts) == len(set(ts)), "no duplicate timestamps")
    outside = sum(1 for t in ts if t.time() < open_t or t.time() > SESSIONS[seg][3])
    check(outside == 0, f"every candle inside the session {open_t:%H:%M}-{SESSIONS[seg][3]:%H:%M} ({outside} outside)")
    by = defaultdict(list)
    for r in rows:
        by[r[0].date()].append(r)
    missing = 0
    for d, v in by.items():
        for a, b in zip(v, v[1:]):
            gap = (b[0] - a[0]).total_seconds() / 60
            if gap > minutes:
                missing += int(gap // minutes) - 1
    flat = sum(1 for _, o, h, l, c, _v in rows if o == h == l == c)
    zero_vol = sum(1 for r in rows if not r[5])
    print(f"  info  {len(by)} sessions; median {sorted(len(v) for v in by.values())[len(by) // 2]} candles a session; {missing:,} missing candle(s) inside sessions; "
          f"{flat:,} flat candles (o=h=l=c, {100 * flat / len(rows):.1f}%); {zero_vol:,} zero-volume candles ({100 * zero_vol / len(rows):.1f}%)")
    return by


# ------------------------------------------------------------------------------------------------ the oracle
def path_points(o, h, l, c, mode):
    if mode == "engine":
        return [o, l, h, c] if c >= o else [o, h, l, c]
    if mode == "hl":
        return [o, h, l, c]
    if mode == "lh":
        return [o, l, h, c]
    return [o, h, l, c] if (h - o) < (o - l) else [o, l, h, c]             # "near": the extreme closer to the open first (TradingView's rule)


def dense(points, step):
    yield points[0]
    for a, b in zip(points, points[1:]):
        n = max(1, math.ceil(abs(b - a) / step))
        for k in range(1, n + 1):
            yield b if k == n else a + (b - a) * k / n


def keep_days(by, pct, seg, minutes):
    """Sessions with at least pct % of a FULL exchange session's candles (independent of the other days in the data)."""
    span = CLOSE[seg].hour * 60 + CLOSE[seg].minute - SESS[seg][0].hour * 60 - SESS[seg][0].minute
    expected = max(1, math.ceil(span / minutes))
    return sorted(d for d, v in by.items() if len(v) >= expected * pct / 100)


def geometry(H, L, cfg):
    D = H - L
    third = cfg["third_frac"] * D
    dev = {"BUY": cfg["dev_value"] / 100 * H if cfg["dev_unit"] == "pct" else cfg["dev_value"], "SELL": cfg["dev_value"] / 100 * L if cfg["dev_unit"] == "pct" else cfg["dev_value"]}
    entry = {"BUY": H - (third - dev["BUY"]), "SELL": L + (third - dev["SELL"])}
    tdist = {s: third + (third - dev[s]) for s in dev}
    stop = {"BUY": entry["BUY"] - dev["BUY"], "SELL": entry["SELL"] + dev["SELL"]}
    target = {"BUY": entry["BUY"] + tdist["BUY"], "SELL": entry["SELL"] - tdist["SELL"]}
    return {"D": D, "third": third, "dev": dev, "entry": entry, "stop": stop, "target": target, "valid": {s: tdist[s] > 0 for s in tdist}}


def oracle_day(H, L, candles, seg, cfg, mode, slip, cost):
    g = geometry(H, L, cfg)
    open_t, sq_t = SESS[seg]
    day = candles[0][0].date()
    go = datetime.combine(day, open_t) + timedelta(minutes=cfg["skip_minutes"])
    cut = None if cfg["last_entry"] == "none" else time(int(cfg["last_entry"][:2]), int(cfg["last_entry"][3:]))
    day_open = candles[0][1]
    if cfg.get("gap_both"):             # the installed strategy: the day must open INSIDE yesterday's range, otherwise no trade on either side
        out_of_range = bool(cfg["gap_filter"]) and not (L <= day_open <= H)
        blocked = {"BUY": out_of_range, "SELL": out_of_range}
    else:                               # the research version: only the side the gap points to is off
        blocked = {"BUY": cfg["gap_filter"] and day_open > H, "SELL": cfg["gap_filter"] and day_open < L}
    armed = {"BUY": False, "SELL": False}
    used = {"BUY": 0, "SELL": 0}
    pos, trades, net_sum, last_px, last_ts = None, [], 0.0, None, None
    sl = slip / 100

    def close(ts, px, reason, market):
        nonlocal pos, net_sum
        side, e = pos["side"], pos["entry"]
        xp = (px - px * sl if side == "BUY" else px + px * sl) if market else px
        gross = (xp - e) if side == "BUY" else (e - xp)
        net = gross - e * cost / 100
        trades.append({"date": day, "side": side, "entry_ts": pos["ts"], "exit_ts": ts, "entry": e, "exit": xp, "reason": reason, "net_pct": net / e * 100,
                       "stop": pos["stop"], "target": pos["target"]})
        net_sum += net / e * 100
        pos = None

    for ts, o, h, l, c, _v in candles:
        if ts.time() >= sq_t:
            break
        last_ts = ts
        for p in dense(path_points(o, h, l, c, mode), max(p_step(o), 1e-9)):
            last_px = p
            if pos is not None:                                                  # manage first (stop, then target), as a live tick would
                if pos["side"] == "BUY":
                    if p <= pos["stop"]:
                        close(ts, p, "stop", True)
                    elif p >= pos["target"]:
                        close(ts, pos["target"], "target", False)
                else:
                    if p >= pos["stop"]:
                        close(ts, p, "stop", True)
                    elif p <= pos["target"]:
                        close(ts, pos["target"], "target", False)
            for side in ("BUY", "SELL"):
                window = ts >= go and (cut is None or ts.time() < cut) and len(trades) + (pos is not None) < cfg["trades_per_day"]
                if not window:
                    armed[side] = False
                    continue
                if not armed[side]:
                    if used[side] >= 1 or blocked[side] or not g["valid"][side]:
                        continue
                    if (p < g["entry"]["BUY"]) if side == "BUY" else (p > g["entry"]["SELL"]):
                        armed[side] = True
                    continue
                if (p >= g["entry"]["BUY"]) if side == "BUY" else (p <= g["entry"]["SELL"]):
                    armed[side] = False
                    if pos is not None or net_sum <= -2.0:
                        continue
                    e = p + p * sl if side == "BUY" else p - p * sl
                    pos = {"side": side, "entry": e, "ts": ts, "stop": g["stop"][side], "target": g["target"][side]}
                    used[side] += 1
    if pos is not None:                                                          # forced exit at the last price of the session (market, with slippage)
        close(last_ts, last_px, "squareoff", True)
    return trades, g


def p_step(price):
    return price * STEP_PCT / 100


def oracle(by, seg, cfg, mode="engine", min_bars_pct=80.0, slip=0.02, cost=0.10, minutes=1):
    days = keep_days(by, min_bars_pct, seg, minutes)
    out, levels = [], {}
    for prev, cur in zip(days, days[1:]):
        H, L = max(r[2] for r in by[prev]), min(r[3] for r in by[prev])
        if not H > L:
            continue
        t, g = oracle_day(H, L, by[cur], seg, cfg, mode, slip, cost)
        out.extend(t)
        levels[cur] = (H, L, g)
    return out, levels, days


# ------------------------------------------------------------------------------------------------ comparisons
def run_engine(factory, inst, a, options, date_to=None, strategy="range_thirds"):
    p = Params(strategy=strategy, options=options, qty=1.0, slippage_pct=0.02, cost_pct=0.10)
    return run_backtest(factory, inst.id, a.interval, p, date.fromisoformat(a.date_from) if a.date_from else None, date_to, 80.0)


def compare(eng, ora, tick_tol):
    """Engine trades vs oracle trades, matched in order. Returns (n_ok, [problems])."""
    probs = []
    e_list = [(t["date"], t["side"], t["entry_dt"][:16], t["exit_dt"][:16], t["entry"], t["exit"], t["exit_reason"], t["net_pct"], t["stop"], t["target"]) for t in eng["trade_list"]]
    o_list = [(t["date"].isoformat(), t["side"], t["entry_ts"].strftime("%Y-%m-%d %H:%M"), t["exit_ts"].strftime("%Y-%m-%d %H:%M"), t["entry"], t["exit"], t["reason"], t["net_pct"],
               t["stop"], t["target"]) for t in ora]
    if len(e_list) != len(o_list):
        probs.append(f"trade count: engine {len(e_list)} vs oracle {len(o_list)}")
    ok = 0
    for e, o in zip(e_list, o_list):
        why = []
        if e[0] != o[0] or e[1] != o[1]:
            why.append("date/side")
        if e[2] != o[2]:
            why.append(f"entry minute {e[2][11:]} vs {o[2][11:]}")
        if e[3] != o[3]:
            why.append(f"exit minute {e[3][11:]} vs {o[3][11:]}")
        if e[6] != o[6]:
            why.append(f"reason {e[6]} vs {o[6]}")
        if abs(e[4] - o[4]) > tick_tol * e[4] or abs(e[5] - o[5]) > tick_tol * e[5]:
            why.append(f"prices {e[4]:.2f}/{e[5]:.2f} vs {o[4]:.2f}/{o[5]:.2f}")
        if abs(e[8] - o[8]) > 1e-6 or abs(e[9] - o[9]) > 1e-6:
            why.append(f"stop/target {e[8]:.4f}/{e[9]:.4f} vs {o[8]:.4f}/{o[9]:.4f}")
        if why:
            probs.append(f"{e[0]} {e[1]}: " + "; ".join(why))
        else:
            ok += 1
    return ok, probs


def invariants(eng, by, cfg, seg):
    open_t, sq_t = SESS[seg]
    sl = 0.02 / 100
    seen, bad = defaultdict(int), []
    cut = None if cfg["last_entry"] == "none" else time(int(cfg["last_entry"][:2]), int(cfg["last_entry"][3:]))
    optimistic = 0
    for t in eng["trade_list"]:
        d = date.fromisoformat(t["date"])
        seen[d] += 1
        ent, ex = datetime.fromisoformat(t["entry_dt"]), datetime.fromisoformat(t["exit_dt"])
        if ent < datetime.combine(d, open_t) + timedelta(minutes=cfg["skip_minutes"]):
            bad.append(f"{d} entry {ent:%H:%M} inside the first {cfg['skip_minutes']:g} minutes")
        if cut and ent.time() >= cut:
            bad.append(f"{d} entry {ent:%H:%M} at/after the cut-off")
        if ex.time() >= sq_t or ex.date() != d:
            bad.append(f"{d} exit {ex} at/after the square-off or overnight")
        c_in = next((r for r in by[d] if r[0] == ent.replace(second=0)), None)
        c_out = next((r for r in by[d] if r[0] == ex.replace(second=0)), None)
        if c_in is None or c_out is None:
            bad.append(f"{d} no candle at the entry/exit minute")
            continue
        buy = t["side"] == "BUY"
        lo_in, hi_in = (c_in[3] * (1 + sl), c_in[2] * (1 + sl)) if buy else (c_in[3] * (1 - sl), c_in[2] * (1 - sl))
        if not lo_in - TOL <= t["entry"] <= hi_in + TOL:
            bad.append(f"{d} entry {t['entry']:.2f} outside its candle [{lo_in:.2f}, {hi_in:.2f}]")
        if t["exit_reason"] == "target":
            if not c_out[3] - TOL <= t["exit"] <= c_out[2] + TOL:
                optimistic += 1                                           # a gap beyond the target: the limit order is assumed filled AT the target
        elif t["exit_reason"] == "stop":
            lo, hi = (c_out[3] * (1 - sl), c_out[2] * (1 - sl)) if buy else (c_out[3] * (1 + sl), c_out[2] * (1 + sl))
            if not lo - TOL <= t["exit"] <= hi + TOL:
                bad.append(f"{d} stop exit {t['exit']:.2f} outside its candle")
        sign = 1 if buy else -1
        net = (sign * (t["exit"] - t["entry"]) - t["entry"] * 0.10 / 100) / t["entry"] * 100
        if abs(net - t["net_pct"]) > 5e-4:
            bad.append(f"{d} net % {t['net_pct']:.4f} vs recomputed {net:.4f}")
    multi = [d for d, n in seen.items() if n > cfg["trades_per_day"]]
    if multi:
        bad.append(f"{len(multi)} day(s) with more than {cfg['trades_per_day']} trade(s)")
    return bad, optimistic


def stats_check(eng):
    tl = eng["trade_list"]
    pct = [t["net_pct"] for t in tl]
    n = len(pct)
    ok = []
    if n:
        wins = [v for v in pct if v > 0]
        gp, gl = sum(wins), -sum(v for v in pct if v <= 0)
        mean = sum(pct) / n
        sd = math.sqrt(sum((v - mean) ** 2 for v in pct) / (n - 1)) if n > 2 else 0
        eq = peak = dd = 0.0
        for v in pct:
            eq += v
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        cum = 0.0
        rdd = rpeak = 0.0
        for t in tl:
            cum += t["net_pnl"]
            rpeak = max(rpeak, cum)
            rdd = max(rdd, rpeak - cum)
        ok = [("trades", n == eng["trades"]), ("wins", len(wins) == eng["wins"]), ("total %", abs(sum(pct) - eng["total"]) < 0.01),
              ("mean %", abs(mean - eng["mean"]) < 5e-4), ("profit factor", gl == 0 or abs(gp / gl - eng["profit_factor"]) < 0.01),
              ("t-stat", sd == 0 or abs(mean / (sd / math.sqrt(n)) - eng["t_stat"]) < 0.01), ("max drawdown %", abs(dd - eng["max_drawdown"]) < 5e-3),
              ("net P&L Rs", abs(cum - eng["net_pnl"]) < 0.05), ("max drawdown Rs", abs(rdd - eng["max_drawdown_rs"]) < 0.05)]
    return [k for k, v in ok if not v]


# ------------------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--symbol")
    ap.add_argument("--segment", default="NSE_EQ")
    ap.add_argument("--instrument-id", type=int)
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--quick", action="store_true", help="skip the path-order sensitivity runs")
    a = ap.parse_args()
    if not (a.symbol or a.instrument_id):
        ap.error("--symbol or --instrument-id")
    _e, factory = init_db(a.db)
    inst, rows = load(factory, a)
    seg = inst.segment
    minutes = int(a.interval.rstrip("m"))
    print(f"VERIFYING {inst.label} {a.interval}: {len(rows):,} candles {rows[0][0]:%Y-%m-%d} to {rows[-1][0]:%Y-%m-%d}")
    check(SESS[seg][0] == SESSIONS[seg][0] and SESS[seg][1] == SESSIONS[seg][2], f"session facts in this script match dryrun/config.py (open {SESSIONS[seg][0]:%H:%M}, square-off {SESSIONS[seg][2]:%H:%M})")
    by = data_checks(rows, seg, minutes)
    base = {"dev_value": 0.8, "dev_unit": "pct", "third_frac": 1 / 3, "gap_filter": True, "skip_minutes": 5, "last_entry": "18:00", "trades_per_day": 1}
    R = "range_thirds_research"             # the hidden all-options version: the variants need settings, the simple strategy has none
    installed = {**base, "gap_both": True}
    variants = [("range_thirds as installed (the 0.8 rule, one trade a day, day must open inside yesterday's range)", installed, {}, "range_thirds"),
                ("research version, default settings", base, {}, R),
                ("research version, day-open filter off", {**base, "gap_filter": False}, {"gap_filter": "off"}, R),
                ("research version, two trades a day", {**base, "trades_per_day": 2}, {"trades_per_day": 2}, R),
                ("research version, 0.5% instead of 0.8%", {**base, "dev_value": 0.5}, {"dev_value": 0.5}, R)]
    first_eng = None
    for label, cfg, opt, strat in variants:
        print(f"\n2-4/6. ORACLE vs ENGINE, {label}")
        eng = run_engine(factory, inst, a, opt, strategy=strat)
        ora, levels, days = oracle(by, seg, cfg, minutes=minutes)
        first_eng = first_eng or (eng, ora, levels, cfg)
        ok, probs = compare(eng, ora, 3 * STEP_PCT / 100 + 1e-9)
        check(not probs, f"{ok} of {len(eng['trade_list'])} engine trades match the independent simulator ({len(ora)} oracle trades)" + ("" if not probs else f"; first problems: {probs[:4]}"))
        bad, optimistic = invariants(eng, by, cfg, seg)
        check(not bad, f"per-trade invariants hold (window, one a day, candle ranges, P&L arithmetic, square-off){'' if not bad else ': ' + str(bad[:3])}")
        print(f"  info  {optimistic} target exit(s) were gap fills: the target price lies outside that candle's range, so a limit order is assumed filled AT the target (optimistic)")
        miss = stats_check(eng)
        check(not miss, f"summary statistics recomputed from the trade list agree{'' if not miss else ': ' + str(miss)}")
    eng, ora, levels, cfg = first_eng
    print("\n4. LEVELS (previous-day High/Low and entry/stop/target lines per day)")
    dl = eng["day_levels"]
    mism = 0
    for d, (H, L, g) in levels.items():
        got = {x["name"]: x["value"] for x in dl.get(d.isoformat(), [])}
        want = {"prev High": H, "prev Low": L, "buy trigger": g["entry"]["BUY"], "sell trigger": g["entry"]["SELL"], "buy stop": g["stop"]["BUY"], "sell stop": g["stop"]["SELL"],
                "buy target": g["target"]["BUY"], "sell target": g["target"]["SELL"]}
        if not got or any(abs(got.get(k, 1e18) - v) > 1e-6 for k, v in want.items()):
            mism += 1
    check(mism == 0 and len(dl) == len(levels), f"all {len(levels)} days: engine levels equal the independent calculation ({mism} differ)")
    print("\n5. LOOK-AHEAD (trades before a cut date must not change when the data after it is removed)")
    days = sorted(levels)
    full = {(t["date"], t["side"], t["entry_dt"], t["exit_dt"], round(t["entry"], 6), round(t["exit"], 6)) for t in eng["trade_list"]}
    for frac in (0.3, 0.55, 0.8):
        cut_day = days[int(len(days) * frac)]
        part = run_engine(factory, inst, a, {}, date_to=cut_day)
        pk = {(t["date"], t["side"], t["entry_dt"], t["exit_dt"], round(t["entry"], 6), round(t["exit"], 6)) for t in part["trade_list"]}
        fk = {k for k in full if k[0] <= cut_day.isoformat()}
        check(pk == fk, f"data cut at {cut_day}: {len(pk)} trades, identical to the same days of the full run ({len(fk)})")
    if not a.quick:
        print("\n7. PATH-ORDER SENSITIVITY (the order of high and low inside a candle is unknown; the independent simulator with four rules)")
        res = {}
        for mode in ("engine", "hl", "lh", "near"):
            ora_m, _lv, _d = oracle(by, seg, installed, mode=mode, minutes=minutes)
            pct = [t["net_pct"] for t in ora_m]
            res[mode] = (len(pct), sum(pct) / len(pct) if pct else float("nan"), sum(pct))
            print(f"  info  {mode:6s} trades {len(pct):4d}  mean {res[mode][1]:+.4f}% per trade  total {res[mode][2]:+.2f}%")
        spread = max(v[1] for v in res.values()) - min(v[1] for v in res.values())
        print(f"  info  spread of the mean per trade across the four rules: {spread:.4f} percentage points")
    print("\nRESULT: " + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED"))
    for f in FAILS:
        print("  - " + f[:300])
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
