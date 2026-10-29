"""Previous-day high/low break-and-pullback on Bajaj Finance (cash market, intraday long and short).

Protocol: docs/bajajfinance-protocol.md (committed before results). Reuses the silver engine.
"""
from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from . import silver_data as sd
from . import silver_run as sr
from . import silver_study as ss

OUT = Path(__file__).parent / "results"
SYM = "BAJFINANCE.NS"
COST = 0.0010
WINDOWS = {"6M": (pd.Timestamp("2026-04-10"), ss.END), "1Y": (pd.Timestamp("2025-10-10"), ss.END),
           "prior year": (pd.Timestamp("2024-10-10"), pd.Timestamp("2025-10-10"))}


def load_bars(interval, rng):
    b = sd.load(SYM, interval, rng)[["open", "high", "low", "close", "volume"]].copy()
    t = b.index
    mins = t.hour * 60 + t.minute
    b = b[(mins >= 9 * 60 + 15) & (mins < 15 * 60 + 30) & (t.dayofweek < 5)].copy()
    b["session"] = b.index.normalize().tz_localize(None)
    return b


def n_sessions(sess, start, end):
    return int(((sess.index >= start) & (sess.index < end) & sess.prev_high.notna()).sum())


def run(bars, sess, spec, start, end, cost=COST):
    tr, fl = ss.find_trades(bars, sess, spec, start, end)
    if len(tr):
        tr["ret_net"] = tr["ret"] - cost
    return tr, fl


def main():
    bars = load_bars("1h", "2y")
    sess = ss.session_table(bars, min_bars=6)
    # Yahoo's intraday bars are not split-adjusted: 16-Jun-2025 (1:2 split + 4:1 bonus) shows a -80% gap.
    # No setup can be valid on that session, so drop it and chain returns around the jump.
    gap = sess["open"] / sess["close"].shift(1) - 1
    bad = gap.abs() > 0.30
    sess.loc[bad, ["prev_high", "prev_low"]] = np.nan
    step = (sess["close"] / sess["close"].shift(1)).where(~bad, sess["close"] / sess["open"])
    primary = ss.Spec(pullback=0.01, target=0.02, stop=0.01)
    rows, opp, trades_all = [], [], []

    for name, (a, z) in WINDOWS.items():
        tr, fl = run(bars, sess, primary, a, z)
        n = n_sessions(sess, a, z)
        o = ss.opportunity_stats(fl, n, tr)
        o["window"] = name
        opp.append(o)
        tr["window"] = name
        trades_all.append(tr)
        s = ss.summarise(tr, "ret_net")
        s.update(window=name, spec="primary")
        # position of Rs 2 lakh
        s["rs_per_2L_total"] = float((tr.ret_net * 200000).sum())
        rows.append(s)
        win_s = sess[(sess.index >= a) & (sess.index < z)]
        o["stock_return"] = float(step.loc[win_s.index].iloc[1:].prod() - 1)
        o["median_range_pct"] = float((win_s["high"] / win_s["low"] - 1).median())
    pd.DataFrame(opp).to_csv(OUT / "bajaj_opportunities.csv", index=False)
    pd.concat(trades_all).to_csv(OUT / "bajaj_trades_primary.csv", index=False)

    a, z = WINDOWS["1Y"]
    tr1 = trades_all[1]
    side = []
    for sd_, g in tr1.groupby("side"):
        s = ss.summarise(g, "ret_net")
        s.update(window="1Y", side=sd_)
        side.append(s)
    pd.DataFrame(side).to_csv(OUT / "bajaj_by_side.csv", index=False)

    # variants / bounds / cost sensitivity
    var = {"conservative(primary)": (primary, COST), "optimistic_intrabar": (ss.Spec(conservative=False), COST),
           "no_stop": (ss.Spec(stop=None), COST), "enter_on_break(no pullback)": (ss.Spec(enter_on_break=True), COST),
           "cost 0%": (primary, 0.0), "cost 0.20%": (primary, 0.002)}
    for name in ("1Y", "6M", "prior year"):
        a_, z_ = WINDOWS[name]
        for lab, (sp, c) in var.items():
            tr, _ = run(bars, sess, sp, a_, z_, c)
            s = ss.summarise(tr, "ret_net")
            s.update(window=name, spec=lab)
            rows.append(s)
    pd.DataFrame(rows).to_csv(OUT / "bajaj_summary.csv", index=False)

    # exploratory grid, volatility-scaled
    grid = []
    for p, t, st in itertools.product((0.0, 0.005, 0.01), (0.01, 0.015, 0.02, 0.03), (0.005, 0.01, None)):
        sp = ss.Spec(pullback=p, target=t, stop=st)
        for name in ("6M", "1Y", "prior year"):
            a_, z_ = WINDOWS[name]
            tr, fl = run(bars, sess, sp, a_, z_)
            s = ss.summarise(tr, "ret_net")
            grid.append({"window": name, "pullback": p, "target": t, "stop": st, "trades": s.get("n", 0),
                         "win": s.get("win"), "mean": s.get("mean"), "ci_lo": s.get("ci_lo"), "ci_hi": s.get("ci_hi"),
                         "total": s.get("sum")})
    pd.DataFrame(grid).to_csv(OUT / "bajaj_grid.csv", index=False)

    # random-timing null + top-3 removal
    sr.UND_COST = COST
    nulls, rob = [], []
    for name in ("6M", "1Y", "prior year"):
        a_, z_ = WINDOWS[name]
        tr = trades_all[list(WINDOWS).index(name)]
        # restrict null to the window by temporarily trimming the session index
        sub = sess[(sess.index >= a_) & (sess.index < z_)]
        old_end = ss.END
        ss.END = z_
        means = sr.random_null(bars, sub, primary, a_)
        ss.END = old_end
        actual = tr.ret_net.mean()
        nulls.append({"window": name, "actual_mean": actual, "null_mean": means.mean(), "null_p95": np.percentile(means, 95),
                      "p_one_sided": float((means >= actual).mean())})
        x = np.sort(tr.ret_net.to_numpy())
        rob.append({"window": name, "mean": x.mean(), "mean_minus_top3": x[:-3].mean()})
    pd.DataFrame(nulls).to_csv(OUT / "bajaj_null.csv", index=False)
    pd.DataFrame(rob).to_csv(OUT / "bajaj_robust.csv", index=False)

    # resolution check (last ~60 days)
    res = []
    b15, s15 = load_bars("15m", "60d"), None
    s15 = ss.session_table(b15, min_bars=20)
    b5 = load_bars("5m", "60d")
    s5 = ss.session_table(b5, min_bars=60)
    st = s15.index[1]
    for lab, bb, sx in (("5m", b5, s5), ("15m", b15, s15), ("1h (same days)", bars, sess)):
        for cons in (True, False):
            tr, fl = run(bb, sx, ss.Spec(conservative=cons), st, ss.END)
            n = n_sessions(sx, st, ss.END)
            o = ss.opportunity_stats(fl, n, tr)
            o.update(resolution=lab, rule="conservative" if cons else "optimistic", from_=str(st.date()),
                     mean_net=float(tr.ret_net.mean()) if len(tr) else None,
                     win=float((tr.ret_net > 0).mean()) if len(tr) else None)
            res.append(o)
    pd.DataFrame(res).to_csv(OUT / "bajaj_resolution.csv", index=False)
    print("done")


if __name__ == "__main__":
    main()
