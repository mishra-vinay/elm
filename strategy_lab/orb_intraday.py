"""9:15 opening-candle breakout, price-only test on Yahoo 5-minute data (last ~60 sessions only).   python -m strategy_lab.orb_intraday

LIMITATION: Yahoo's 09:15 bar has volume 0 on ~80% of days and intraday volume sums to only ~93-97% of the daily total, so a
"high volume at 9:15" filter CANNOT be tested here. 'Activity' is proxied by the opening candle's range vs its own 20-session average.

Rules (fixed before running):
  opening candle = 09:15 bar; activity = (high-low)/open divided by the mean of that ratio over the prior 20 sessions (>=10 needed)
  direction: green opening candle -> long, red -> short (cash MIS; intraday shorting allowed)
  entry: first 5-min bar (09:20-11:00) whose CLOSE is beyond the opening candle's high (long) / low (short); fill at the NEXT bar's open
  stop: opposite end of the opening candle (skip if that risk < 0.25% of price); target: 1.5 x risk; otherwise exit at the 15:15 bar's close
  same-bar stop+target ambiguity -> assume the stop hit first.  Cost: 0.20% round trip (brokerage, STT 0.025%, exchange, GST, slippage).
Comparison: high-activity days (ratio >= 1.5) vs low-activity days (< 1.0) vs all days.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import data
from .universe import SYMBOLS, yahoo

CACHE = Path(__file__).parent / ".cache" / "intraday"
R = Path(__file__).parent / "results"
COST = 0.0020


def load5(sym):
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{sym.replace('&', '_')}_5m.csv"
    if p.exists():
        return pd.read_csv(p, index_col=0, parse_dates=True)
    r = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo(sym)}", params={"range": "60d", "interval": "5m"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
    if r.status_code != 200:
        return None
    j = r.json()["chart"]["result"][0]; q = j["indicators"]["quote"][0]
    idx = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None)
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")}, index=idx).dropna(subset=["open", "close"])
    df.to_csv(p)
    import time; time.sleep(0.3)
    return df


def trades_for(sym, df):
    out = []
    days = sorted(set(df.index.normalize()))
    ratios = {}
    for d in days:
        b = df[df.index.normalize() == d]
        if len(b) < 60 or b.index[0].strftime("%H:%M") != "09:15" or b.index[-1].strftime("%H:%M") < "15:15":
            continue                                             # incomplete session (Yahoo drops some 5-min bars)
        o = b.iloc[0]; ratios[d] = (o.high - o.low) / o.open
    keys = sorted(ratios)
    for i, d in enumerate(keys):
        hist = [ratios[k] for k in keys[max(0, i - 20):i]]
        if len(hist) < 10:
            continue
        act = ratios[d] / np.mean(hist)
        b = df[df.index.normalize() == d]; o = b.iloc[0]
        if o.close == o.open:
            continue
        side = 1 if o.close > o.open else -1
        lvl = o.high if side == 1 else o.low
        stop = o.low if side == 1 else o.high
        bars = b.iloc[1:]
        entry_i = None
        for k in range(len(bars) - 1):
            t = bars.index[k].strftime("%H:%M")
            if t > "11:00":
                break
            if (side == 1 and bars.close.iloc[k] > lvl) or (side == -1 and bars.close.iloc[k] < lvl):
                entry_i = k + 1; break
        if entry_i is None:
            continue
        entry = bars.open.iloc[entry_i]
        risk = (entry - stop) * side
        if risk <= 0 or risk / entry < 0.0025:
            continue
        target = entry + side * 1.5 * risk
        exit_px, why = None, "time"
        for k in range(entry_i, len(bars)):
            hi, lo = bars.high.iloc[k], bars.low.iloc[k]
            hit_stop = (lo <= stop) if side == 1 else (hi >= stop)
            hit_tgt = (hi >= target) if side == 1 else (lo <= target)
            if hit_stop:
                exit_px, why = stop, "stop"; break
            if hit_tgt:
                exit_px, why = target, "target"; break
            if bars.index[k].strftime("%H:%M") >= "15:15":
                exit_px = bars.close.iloc[k]; break
        if exit_px is None:
            exit_px = bars.close.iloc[-1]
        gross = side * (exit_px - entry) / entry
        out.append(dict(symbol=sym, day=d, side=side, activity=act, risk_pct=risk / entry, gross=gross, net=gross - COST,
                        r_mult=side * (exit_px - entry) / risk, why=why))
    return out


def summarize(g, label):
    if len(g) == 0:
        print(f"{label}: no trades"); return None
    rng = np.random.default_rng(2); days = g.day.unique()
    bs = []
    for _ in range(2000):
        pick = rng.choice(days, len(days)); bs.append(np.mean(np.concatenate([g[g.day == d].net.values for d in pick])))
    wins = g.net[g.net > 0].sum(); losses = -g.net[g.net <= 0].sum()
    row = dict(group=label, trades=len(g), days=len(days), win=(g.net > 0).mean(), mean_net=g.net.mean(), median_net=g.net.median(),
               ci_lo=np.percentile(bs, 2.5), ci_hi=np.percentile(bs, 97.5), avg_R=g.r_mult.mean(), pf=wins / losses if losses > 0 else np.nan,
               gross=g.gross.mean(), stop_rate=(g.why == "stop").mean(), target_rate=(g.why == "target").mean())
    print(f"{label:34s} n={row['trades']:5d} win {row['win']:.0%}  mean net {row['mean_net']:+.3%} (CI {row['ci_lo']:+.3%}..{row['ci_hi']:+.3%})  "
          f"gross {row['gross']:+.3%}  avgR {row['avg_R']:+.2f}  PF {row['pf']:.2f}  stop {row['stop_rate']:.0%} target {row['target_rate']:.0%}")
    return row


def main():
    allt = []
    for s in SYMBOLS:
        df = load5(s)
        if df is not None and len(df) > 1000:
            allt += trades_for(s, df)
    t = pd.DataFrame(allt); t.to_csv(R / "orb_trades.csv", index=False)
    print(f"{t.symbol.nunique()} stocks, {t.day.nunique()} sessions ({t.day.min().date()} -> {t.day.max().date()}), {len(t)} trades\n")
    rows = [summarize(t, "ALL days"), summarize(t[t.activity >= 1.5], "HIGH activity (range >= 1.5x avg)"),
            summarize(t[t.activity < 1.0], "LOW activity (range < 1.0x avg)"),
            summarize(t[(t.activity >= 1.5) & (t.side == 1)], "HIGH activity, longs only"),
            summarize(t[(t.activity >= 1.5) & (t.side == -1)], "HIGH activity, shorts only"),
            summarize(t[t.activity >= 3.0], "VERY HIGH activity (>= 3x)")]
    pd.DataFrame([r for r in rows if r]).to_csv(R / "orb_results.csv", index=False)
    print("\nMarket context: Nifty 50 over the window:", end=" ")
    n = data.load("^NSEI").close; w = n[(n.index >= t.day.min()) & (n.index <= t.day.max())]; print(f"{w.iloc[-1] / w.iloc[0] - 1:+.1%}")
    return t


if __name__ == "__main__":
    main()
