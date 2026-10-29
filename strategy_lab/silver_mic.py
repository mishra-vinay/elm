"""Silver Micro (1 kg) futures version of the break-and-pullback rule.

MCX lists options only on Silver (30 kg) and Silver Mini (5 kg); Silver Micro is a futures contract, so this is
the same rule traded as a 1 kg futures position. Costs: Rs 40 flat + 0.08% of notional round trip
(0.03% slippage per side, ~0.02% CTT/exchange/GST).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import silver_study as ss

OUT = Path(__file__).parent / "results"
KG = 1
FLAT, PCT = 40.0, 0.0008


def main():
    bars = ss.load_bars()
    sess = ss.session_table(bars)
    rows, trades = [], []
    for name, start, end in (("6M", ss.PERIODS["6M"], ss.END), ("1Y", ss.PERIODS["1Y"], ss.END),
                             ("prior year (Oct24-Oct25)", ss.PERIODS["2Y"], ss.PERIODS["1Y"])):
        tr, _ = ss.find_trades(bars, sess, ss.Spec(), start, end)
        n = int(((sess.index >= start) & (sess.index < end) & sess.prev_high.notna()).sum())
        sign = tr.side.map({"BUY": 1, "SELL": -1})
        tr["gross_rs"] = sign * (tr.exit - tr.entry) * KG
        tr["cost_rs"] = FLAT + PCT * tr.entry * KG
        tr["net_rs"] = tr.gross_rs - tr.cost_rs
        tr["period"] = name
        trades.append(tr)
        lo, hi = ss.boot_ci(tr.net_rs.to_numpy(), tr.session.to_numpy())
        cum = tr.net_rs.cumsum()
        rows.append({"period": name, "sessions": n, "trades": len(tr), "win": (tr.net_rs > 0).mean(),
                     "mean_net_rs": tr.net_rs.mean(), "ci_lo": lo, "ci_hi": hi, "total_net_rs": tr.net_rs.sum(),
                     "total_gross_rs": tr.gross_rs.sum(), "total_cost_rs": tr.cost_rs.sum(),
                     "max_drawdown_rs": (cum - cum.cummax()).min(), "avg_notional_rs": (tr.entry * KG).mean(),
                     "buy_total_rs": tr[tr.side == "BUY"].net_rs.sum(), "sell_total_rs": tr[tr.side == "SELL"].net_rs.sum()})
    pd.DataFrame(rows).to_csv(OUT / "silver_mic_summary.csv", index=False)
    pd.concat(trades).to_csv(OUT / "silver_mic_trades.csv", index=False)
    print(pd.DataFrame(rows).round(0).T)


if __name__ == "__main__":
    main()
