"""Silver options study: previous-day high/low break + pullback strategy.

Design is fixed in docs/silver-options-study.md BEFORE results were looked at (primary spec p=1%,
target=2%, stop=1%, intraday exit). Everything else is reported as exploratory.

Underlying: COMEX silver (Yahoo SI=F) hourly bars x USDINR, treated as a proxy of MCX silver futures
in the MCX session window 09:00-23:30 IST. Option prices are MODELLED (Black-76); real MCX option
history is not freely available. See the report for what this does and does not prove.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from . import silver_data as sd

KG_PER_OZ = 32.1507
MCX_PREMIUM = 1.20        # calibrates the proxy to the ~Rs 2.28 lakh/kg MCX quote seen on 9-Oct-2026
LOT_KG = 5                # SILVERM (Silver Mini) lot; options exist on 30 kg and 5 kg futures
STRIKE_STEP = 1000        # Rs, from 29-Jan-2026
SESSION_START, SESSION_END = "09:00", "23:30"

PERIODS = {
    "6M": pd.Timestamp("2026-04-10"),
    "1Y": pd.Timestamp("2025-10-10"),
    "2Y": pd.Timestamp("2024-10-10"),
}
END = pd.Timestamp("2026-10-10")


# ---------------------------------------------------------------- data
def load_bars(interval: str = "1h", rng: str = "2y") -> pd.DataFrame:
    """Silver bars in rupees/kg (MCX proxy), restricted to MCX trading hours on weekdays."""
    si = sd.load("SI=F", interval, rng)
    fx_iv = "1h" if interval != "1d" else "1d"
    fx = sd.load("USDINR=X", fx_iv, "2y" if fx_iv == "1h" else "5y")["close"]
    fx = fx[~fx.index.duplicated(keep="last")].sort_index()
    rate = fx.reindex(si.index.union(fx.index)).ffill().reindex(si.index)
    rate = rate.fillna(fx.iloc[0])
    out = si[["open", "high", "low", "close"]].mul(rate, axis=0) * KG_PER_OZ * MCX_PREMIUM
    out["volume"] = si["volume"]
    t = out.index
    mins = t.hour * 60 + t.minute
    in_sess = (mins >= 9 * 60) & (mins < 23 * 60 + 30) & (t.dayofweek < 5)
    out = out[in_sess].copy()
    out["session"] = out.index.normalize().tz_localize(None)
    return out


def session_table(bars: pd.DataFrame, min_bars: int = 8) -> pd.DataFrame:
    g = bars.groupby("session")
    s = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                      "close": g["close"].last(), "n": g.size()})
    s = s[s["n"] >= min_bars]
    s["prev_high"] = s["high"].shift(1)
    s["prev_low"] = s["low"].shift(1)
    lr = np.log(s["close"]).diff()
    s["rv20"] = lr.rolling(20).std().shift(1) * math.sqrt(252)   # known before the session starts
    return s


# ---------------------------------------------------------------- setups and trades
@dataclass(frozen=True)
class Spec:
    pullback: float = 0.01      # depth of the retest below the broken high (above the broken low)
    target: float = 0.02        # underlying move from entry that closes the trade
    stop: float | None = 0.01   # adverse underlying move; None = no stop (session-end exit only)
    carry: bool = False         # False = flat by session close, True = may be held to next session close
    conservative: bool = True   # same-bar ambiguity resolved against the trader
    break_buffer: float = 0.0   # break must exceed the level by this fraction
    enter_on_break: bool = False  # ablation: enter at the break instead of waiting for the pullback


def _bar_arrays(bars: pd.DataFrame):
    return (bars["open"].to_numpy(), bars["high"].to_numpy(), bars["low"].to_numpy(),
            bars["close"].to_numpy(), bars["session"].to_numpy())


def _manage(side: int, i0: int, entry: float, spec: Spec, o, h, l, c, sess, last_idx):
    """Walk bars from entry bar i0; return (exit_index, exit_price, reason)."""
    tgt = entry * (1 + side * spec.target)
    stp = None if spec.stop is None else entry * (1 - side * spec.stop)
    for k in range(i0, last_idx + 1):
        hi, lo, op = h[k], l[k], o[k]
        fav = hi if side > 0 else lo
        adv = lo if side > 0 else hi
        if k == i0:
            # the entry happened somewhere inside this bar: the favourable extreme may have come BEFORE it
            hit_stop = stp is not None and (adv <= stp if side > 0 else adv >= stp)
            hit_tgt = (fav >= tgt if side > 0 else fav <= tgt)
            if hit_stop and (spec.conservative or not hit_tgt):
                return k, stp, "stop"
            if hit_tgt and not spec.conservative:
                return k, tgt, "target"
            continue
        if stp is not None and ((op <= stp) if side > 0 else (op >= stp)):
            return k, op, "stop_gap"
        if (op >= tgt) if side > 0 else (op <= tgt):
            return k, op, "target_gap"
        hit_stop = stp is not None and (adv <= stp if side > 0 else adv >= stp)
        hit_tgt = (fav >= tgt if side > 0 else fav <= tgt)
        if hit_stop and hit_tgt:
            return (k, stp, "stop") if spec.conservative else (k, tgt, "target")
        if hit_stop:
            return k, stp, "stop"
        if hit_tgt:
            return k, tgt, "target"
    return last_idx, c[last_idx], "time"


def find_trades(bars: pd.DataFrame, sess: pd.DataFrame, spec: Spec, start: pd.Timestamp,
                end: pd.Timestamp = END) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (trades, per-session setup flags) for sessions in [start, end)."""
    o, h, l, c, sid = _bar_arrays(bars)
    idx = bars.index
    days = sess.index
    first = {}
    last = {}
    for i, d in enumerate(sid):
        first.setdefault(d, i)
        last[d] = i
    day_pos = {d: n for n, d in enumerate(days)}
    trades, flags = [], []
    for d in days:
        if d < start or d >= end or pd.isna(sess.at[d, "prev_high"]):
            continue
        H, L = sess.at[d, "prev_high"], sess.at[d, "prev_low"]
        a, z = first[d], last[d]
        fl = {"session": d, "up_break": False, "dn_break": False, "buy_setup": False, "sell_setup": False}
        for side, level in ((1, H), (-1, L)):
            brk_lvl = level * (1 + side * spec.break_buffer)
            b = None
            for i in range(a, z + 1):
                if (h[i] > brk_lvl) if side > 0 else (l[i] < brk_lvl):
                    b = i
                    break
            if b is None:
                continue
            fl["up_break" if side > 0 else "dn_break"] = True
            if spec.enter_on_break:
                e_i, e_px = b, brk_lvl
                if (o[b] > brk_lvl) if side > 0 else (o[b] < brk_lvl):
                    e_px = o[b]
            else:
                ent = level * (1 - side * spec.pullback)
                e_i = None
                for j in range(b + 1, z + 1):
                    if (l[j] <= ent) if side > 0 else (h[j] >= ent):
                        e_i = j
                        e_px = min(o[j], ent) if side > 0 else max(o[j], ent)
                        break
                if e_i is None:
                    continue
            fl["buy_setup" if side > 0 else "sell_setup"] = True
            if spec.carry and day_pos[d] + 1 < len(days) and days[day_pos[d] + 1] in last:
                end_idx = last[days[day_pos[d] + 1]]
            else:
                end_idx = z
            x_i, x_px, why = _manage(side, e_i, e_px, spec, o, h, l, c, sid, end_idx)
            trades.append({"session": d, "side": "BUY" if side > 0 else "SELL", "break_time": idx[b],
                           "entry_time": idx[e_i], "entry": e_px, "exit_time": idx[x_i], "exit": x_px,
                           "reason": why, "ret": side * (x_px / e_px - 1), "prev_level": level})
        flags.append(fl)
    return pd.DataFrame(trades), pd.DataFrame(flags)


# ---------------------------------------------------------------- option model
def _cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76(F, K, T, sigma, cp, r=0.065):
    T = max(T, 1e-6)
    sd_ = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd_ * sd_) / sd_
    d2 = d1 - sd_
    df = math.exp(-r * T)
    if cp == "C":
        return df * (F * _cdf(d1) - K * _cdf(d2))
    return df * (K * _cdf(-d2) - F * _cdf(-d1))


def expiry_dates(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """MCX silver option expiry ~ 3 business days before the last business day of the month
    (matches 27-Jan, 24-Feb, 27-Oct-2026; April 2026 was 24th so +-2 days error is possible)."""
    out = []
    for m in pd.period_range(start - pd.Timedelta(days=40), end + pd.Timedelta(days=70), freq="M"):
        last_bd = pd.bdate_range(m.start_time, m.end_time)[-1]
        out.append(pd.bdate_range(end=last_bd, periods=4)[0] + pd.Timedelta(hours=23, minutes=30))
    return out


@dataclass(frozen=True)
class OptCost:
    half_spread: float = 0.015   # fraction of premium paid on each side
    ctt: float = 0.001           # on the premium value of the sell leg (conservative assumption)
    flat: float = 40.0           # brokerage per round trip, Rs
    iv_mult: float = 1.0         # IV = realised 20-session vol x this
    min_dte_days: float = 7.0    # use the nearest expiry with at least this many days left


def option_pnl(trades: pd.DataFrame, sess: pd.DataFrame, cost: OptCost = OptCost()) -> pd.DataFrame:
    if trades.empty:
        return trades.assign(prem=[], opt_ret=[], opt_pnl=[])
    exps = expiry_dates(trades["entry_time"].min().tz_localize(None), trades["exit_time"].max().tz_localize(None))
    rows = []
    for _, t in trades.iterrows():
        et = t["entry_time"].tz_localize(None)
        xt = t["exit_time"].tz_localize(None)
        exp = next(e for e in exps if (e - et).days >= cost.min_dte_days)
        sigma = max(0.15, sess.at[t["session"], "rv20"] * cost.iv_mult) if not np.isnan(sess.at[t["session"], "rv20"]) else 0.4
        cp = "C" if t["side"] == "BUY" else "P"
        K = round(t["entry"] / STRIKE_STEP) * STRIKE_STEP
        T0 = (exp - et).total_seconds() / (365 * 86400)
        T1 = (exp - xt).total_seconds() / (365 * 86400)
        p0 = black76(t["entry"], K, T0, sigma, cp)
        p1 = black76(t["exit"], K, T1, sigma, cp)
        buy = p0 * (1 + cost.half_spread)
        sell = p1 * (1 - cost.half_spread)
        lot = LOT_KG
        pnl = (sell - buy) * lot - cost.flat - cost.ctt * sell * lot
        rows.append({"strike": K, "expiry": exp, "iv": sigma, "dte": T0 * 365, "prem": buy,
                     "prem_lot": buy * lot, "opt_pnl": pnl, "opt_ret": pnl / (buy * lot)})
    return pd.concat([trades.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


# ---------------------------------------------------------------- summaries
def boot_ci(x: np.ndarray, groups: np.ndarray, n: int = 4000, seed: int = 7):
    """Day-clustered bootstrap CI of the mean."""
    rng = np.random.default_rng(seed)
    ug = np.unique(groups)
    sums = {g: x[groups == g] for g in ug}
    means = np.empty(n)
    for k in range(n):
        pick = rng.choice(ug, size=len(ug), replace=True)
        arr = np.concatenate([sums[g] for g in pick])
        means[k] = arr.mean()
    return np.percentile(means, [2.5, 97.5])


def summarise(tr: pd.DataFrame, col: str = "ret") -> dict:
    if tr.empty:
        return {"n": 0}
    x = tr[col].to_numpy()
    lo, hi = boot_ci(x, tr["session"].to_numpy())
    gains, losses = x[x > 0].sum(), -x[x < 0].sum()
    return {"n": len(x), "mean": x.mean(), "median": float(np.median(x)), "win": (x > 0).mean(),
            "pf": gains / losses if losses > 0 else np.inf, "ci_lo": lo, "ci_hi": hi,
            "sum": x.sum(), "worst": x.min(), "best": x.max()}


def opportunity_stats(flags: pd.DataFrame, n_sessions: int, trades: pd.DataFrame) -> dict:
    f = flags
    any_break = (f.up_break | f.dn_break).sum()
    setups = (f.buy_setup | f.sell_setup).sum()
    return {
        "sessions": n_sessions,
        "up_break": int(f.up_break.sum()), "dn_break": int(f.dn_break.sum()), "any_break": int(any_break),
        "buy_setup": int(f.buy_setup.sum()), "sell_setup": int(f.sell_setup.sum()),
        "sessions_with_setup": int(setups), "both_sides": int((f.buy_setup & f.sell_setup).sum()),
        "trades": len(trades),
        "target_hits": int(trades["reason"].isin(["target", "target_gap"]).sum()) if len(trades) else 0,
        "stops": int(trades["reason"].isin(["stop", "stop_gap"]).sum()) if len(trades) else 0,
        "time_exits": int((trades["reason"] == "time").sum()) if len(trades) else 0,
    }
