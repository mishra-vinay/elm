"""Scan the universe for stocks currently satisfying each validated strategy.

    python -m strategy_lab.scan              # refresh prices, then scan
    python -m strategy_lab.scan --no-refresh # use cached prices

For each (strategy, stock) the state is computed on the last COMPLETED daily bar:
  NEW ENTRY  state turned on at the last close  -> would be bought at the NEXT open
  HOLD       already on                         -> keep (already-long signal; late entries are NOT recommended)
  NEW EXIT   state turned off at the last close -> would be sold at the NEXT open
Cross-sectional books (momentum / low-vol) are listed as 'as-of' rankings; they only rebalance at month end.
Not investment advice: a signal is a rule firing, not a forecast.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import research
from .indicators import atr, ema
from .strategies import STRATEGIES

RESULTS = Path(__file__).parent / "results"
SCAN = ["ema_cross_20_50_adx", "ema_cross_10_30", "ema_cross_20_50", "donchian_100_50", "high52_trail3atr", "supertrend_10_3"]
MIN_TURNOVER_CR = 20.0      # liquidity filter: 20-day average traded value, Rs crore


def exit_ref(name: str, df: pd.DataFrame):
    if name.startswith("ema_cross"):
        slow = int(name.split("_")[3])
        return float(ema(df.close, slow).iloc[-1]), f"EMA{slow}"
    if name.startswith("donchian"):
        n_out = int(name.split("_")[2])
        return float(df.low.rolling(n_out).min().shift(1).iloc[-1]), f"{n_out}d low"
    return np.nan, ""


def main(refresh: bool = True):
    uni, bm, cal, P, _ = research.build_world(refresh=refresh)
    last = cal[-1]
    syms = list(P["close"].columns)
    rows = []
    for name in SCAN:
        fn = STRATEGIES[name][1]
        for s in syms:
            df = pd.DataFrame({k: P[k][s] for k in ("open", "high", "low", "close", "volume", "raw_close")}).dropna(subset=["close"])
            if df.index[-1] != last or len(df) < 260:
                continue                                           # stale / too short
            st = fn(df[["open", "high", "low", "close"]])
            cur, prev = int(st[-1]), int(st[-2])
            if cur == 0 and prev == 0:
                continue
            run = 0
            for v in st[::-1]:
                if v == cur:
                    run += 1
                else:
                    break
            px = float(df.raw_close.iloc[-1])
            turnover = float((df.raw_close * df.volume).tail(20).mean() / 1e7)
            ref, ref_lab = exit_ref(name, df)
            rows.append(dict(strategy=name, symbol=s,
                             status="NEW ENTRY" if (cur, prev) == (1, 0) else "HOLD" if cur == 1 else "NEW EXIT",
                             bars_in_state=run, last_close=round(px, 2),
                             atr_pct=round(float(atr(df, 14).iloc[-1] / df.close.iloc[-1] * 100), 2),
                             avg_turnover_cr=round(turnover, 1), exit_ref=round(ref * px / float(df.close.iloc[-1]), 2) if ref == ref else np.nan,
                             exit_ref_label=ref_lab, liquid=turnover >= MIN_TURNOVER_CR))
    out = pd.DataFrame(rows)
    # cross-sectional rankings as of the last close
    c = P["close"]
    mom = (c.shift(21) / c.shift(252) - 1).iloc[-1].dropna().sort_values(ascending=False)
    vol = c.pct_change(fill_method=None).rolling(252, min_periods=252).std().iloc[-1].dropna().sort_values()
    nifty = bm["NIFTY50_PRICE"]["close"]
    regime_on = bool(nifty.iloc[-1] > nifty.rolling(200).mean().iloc[-1])
    ranks = pd.DataFrame({"momentum_12_1_rank": mom.rank(ascending=False).astype(int),
                          "momentum_12_1_pct": (mom * 100).round(1),
                          "lowvol_rank": vol.rank().astype(int), "ann_vol_pct": (vol * np.sqrt(252) * 100).round(1)}).sort_values("momentum_12_1_rank")

    stamp = last.date().isoformat()
    out.to_csv(RESULTS / f"scan_{stamp}_signals.csv", index=False)
    ranks.to_csv(RESULTS / f"scan_{stamp}_rankings.csv")
    pd.set_option("display.width", 200); pd.set_option("display.max_rows", 200)
    print(f"\nScan as of last close {stamp}  (universe {len(syms)}; Nifty50 {'ABOVE' if regime_on else 'BELOW'} its 200-DMA)\n")
    if not out.empty:
        print(out.groupby(["strategy", "status"]).size().unstack(fill_value=0).to_string())
        ne = out[(out.status == "NEW ENTRY") & out.liquid]
        print(f"\nNEW ENTRIES at last close (liquid only, {len(ne)}):")
        print(ne.sort_values(["strategy", "symbol"]).to_string(index=False) if len(ne) else "  none")
        conf = out[(out.status != "NEW EXIT") & out.liquid].groupby("symbol").strategy.nunique().sort_values(ascending=False)
        print(f"\nConfluence — stocks long under the most strategies (of {len(SCAN)}):")
        print(conf.head(15).to_string())
    print("\nTop 20 by 12-1 momentum / lowest-vol 20:")
    print(ranks.head(20).to_string()); print("\nLowest-vol 20:"); print(ranks.sort_values("lowvol_rank").head(20).to_string())
    return out, ranks


if __name__ == "__main__":
    main(refresh="--no-refresh" not in sys.argv)
