"""Capital-constrained portfolio simulator (the realistic version of the per-stock 'sleeve' study).

Account of `capital` rupees, at most K positions, whole shares only, each new position sized at (equity / K) at entry.
A signal formed at the close of day t-1 is executed at the open of day t (same convention as engine.py).
When more stocks signal than there are free slots, candidates are ranked by `score` (or randomly for the null).
Returns are total-return (adjusted prices); share counts use the ACTUAL traded price (adjusted open x raw/adj ratio).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .engine import RF


@dataclass(frozen=True)
class PCosts:
    """Delivery equity: percentage costs per side + a flat DP charge per scrip sold (small positions feel it)."""
    buy_pct: float = 0.00119 + 0.0005          # STT 0.10% + stamp 0.015% + exchange/GST ~0.004% + slippage 0.05%
    sell_pct: float = 0.00103 + 0.0005         # STT 0.10% + exchange/GST ~0.003% + slippage 0.05%
    dp_fixed: float = 15.93                    # DP charge incl. GST per scrip sold (varies by broker; ~Rs13.5 + GST)

    def scaled(self, k: float) -> "PCosts":
        return PCosts(self.buy_pct * k, self.sell_pct * k, self.dp_fixed * k)


def simulate(oadj: np.ndarray, ratio: np.ndarray, S: np.ndarray, score: np.ndarray, K: int, capital: float,
             start_idx: int, costs: PCosts = PCosts(), rf: float = RF, fill_vacancies: bool = False,
             rng: np.random.Generator | None = None):
    T, N = oadj.shape
    rf_d = rf / 252
    cash = float(capital)
    held = np.zeros(N, bool)
    units = np.zeros(N)                        # value of holding j at open i = units[j] * oadj[i, j]
    last_oa = np.where(np.isnan(oadj[start_idx]), 0.0, oadj[start_idx])
    eq = np.full(T, np.nan); expo = np.full(T, np.nan); npos = np.full(T, np.nan)
    stats = dict(entries=0, exits=0, skipped_full=0, skipped_price=0, signals=0)
    trade_pnl = []                             # (symbol index, entry value, exit proceeds)
    entry_val = np.zeros(N); entry_i = np.zeros(N, int)
    trades = []
    for i in range(start_idx, T):
        oa = np.where(np.isnan(oadj[i]), last_oa, oadj[i]); last_oa = oa
        vals = units * oa
        equity = cash + vals.sum()
        eq[i], expo[i], npos[i] = equity, vals.sum() / equity, held.sum()
        if i == T - 1:
            break
        prev = S[i - 1] == 1
        # exits: signal went off at the previous close
        ex = held & ~prev
        for j in np.where(ex)[0]:
            proceeds = vals[j] * (1 - costs.sell_pct) - costs.dp_fixed
            cash += proceeds
            trades.append((j, entry_i[j], i, entry_val[j], proceeds))
            held[j] = False; units[j] = 0.0; stats["exits"] += 1
        # entries
        new = prev & (S[i - 2] == 0) if not fill_vacancies else prev
        cand = np.where(new & ~held & ~np.isnan(oadj[i]))[0]
        stats["signals"] += int(((prev & (S[i - 2] == 0)) & ~held).sum())
        slots = K - int(held.sum())
        if len(cand):
            if slots <= 0:
                stats["skipped_full"] += len(cand)
            else:
                if rng is not None:
                    order = cand[rng.permutation(len(cand))]
                else:
                    sc = score[i - 1, cand]
                    order = cand[np.argsort(-np.nan_to_num(sc, nan=-np.inf), kind="stable")]
                taken, skipped = order[:slots], order[slots:]
                stats["skipped_full"] += len(skipped) if not fill_vacancies else 0
                alloc_target = equity / K
                for j in taken:
                    price = oadj[i, j] * ratio[i, j]
                    alloc = min(alloc_target, cash / (1 + costs.buy_pct))
                    n = int(alloc // price)
                    if n < 1:
                        stats["skipped_price"] += 1
                        continue
                    value = n * price
                    cash -= value * (1 + costs.buy_pct)
                    units[j] = value / oadj[i, j]
                    held[j] = True; entry_val[j] = value; entry_i[j] = i; stats["entries"] += 1
        cash *= (1 + rf_d)                     # idle cash earns the liquid-fund yield over the interval
    return pd.Series(eq), pd.Series(expo), pd.Series(npos), stats, trades
