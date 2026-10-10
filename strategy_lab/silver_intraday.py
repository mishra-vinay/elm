"""Same rule on 5-minute and 15-minute bars (Yahoo keeps ~60 days), against hourly bars on the same days."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import silver_study as ss

OUT = Path(__file__).parent / "results"
FLAT, PCT = 40.0, 0.0008   # Silver Micro (1 kg) futures costs, as in silver_mic.py


def prep(interval, rng, min_bars):
    b = ss.load_bars(interval, rng)
    return b, ss.session_table(b, min_bars=min_bars)


def main():
    hourly, hs = prep("1h", "2y", 8)
    sets = {"5m": prep("5m", "60d", 120), "15m": prep("15m", "60d", 40), "1h (same days)": (hourly, hs)}
    start = sets["15m"][1].index[1]
    print("window from", start.date(), "to", ss.END.date())
    rows = []
    for lab, (bars, sess) in sets.items():
        n = int(((sess.index >= start) & sess.prev_high.notna()).sum())
        for p in (0.0, 0.01, 0.02):
            for t in (0.02, 0.03):
                for cons in (True, False):
                    sp = ss.Spec(pullback=p, target=t, stop=0.01, conservative=cons)
                    tr, fl = ss.find_trades(bars, sess, sp, start)
                    r = {"resolution": lab, "pullback": p, "target": t, "rule": "conservative" if cons else "optimistic",
                         "sessions": n, "trades": len(tr), "setup_sessions": int((fl.buy_setup | fl.sell_setup).sum())}
                    if len(tr):
                        sign = tr.side.map({"BUY": 1, "SELL": -1})
                        net = sign * (tr.exit - tr.entry) - FLAT - PCT * tr.entry     # Rs per 1 kg lot
                        tr["ret_net"] = tr.ret - 0.001
                        o = ss.option_pnl(tr, hs)                                      # IV from the hourly 20-session vol
                        r.update(win=float((net > 0).mean()), und_net_pct=float(tr.ret_net.mean()),
                                 mic_net_rs_total=float(net.sum()), mic_net_rs_per_trade=float(net.mean()),
                                 target_hits=int(tr.reason.isin(["target", "target_gap"]).sum()),
                                 stops=int(tr.reason.isin(["stop", "stop_gap"]).sum()),
                                 opt_ret_mean=float(o.opt_ret.mean()), opt_pnl_total_rs=float(o.opt_pnl.sum()))
                    rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "silver_intraday_resolution.csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(df[(df.pullback == 0.01) & (df.target == 0.02)].round(4).T)
    print(df[df.rule == "conservative"][["resolution", "pullback", "target", "trades", "win", "und_net_pct", "mic_net_rs_total", "opt_ret_mean"]].round(4).to_string())


if __name__ == "__main__":
    main()
