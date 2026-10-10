"""Run the silver options study and write results to strategy_lab/results/silver_*.csv."""
from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from . import silver_study as ss

OUT = Path(__file__).parent / "results"
UND_COST = 0.001  # round-trip cost on the underlying (futures) trade


def und_net(tr: pd.DataFrame) -> pd.DataFrame:
    tr = tr.copy()
    tr["ret_net"] = tr["ret"] - UND_COST
    return tr


def run(spec, bars, sess, start, cost=ss.OptCost(), with_opt=True):
    tr, fl = ss.find_trades(bars, sess, spec, start)
    n_sess = int(((sess.index >= start) & (sess.index < ss.END) & sess.prev_high.notna()).sum())
    if tr.empty:
        return tr, fl, n_sess
    tr = und_net(tr)
    if with_opt:
        tr = ss.option_pnl(tr, sess, cost)
    return tr, fl, n_sess


def random_null(bars, sess, spec, start, n_draws=1000, seed=11):
    """Random bar, random direction, same exit rules; mean net underlying return per draw."""
    o, h, l, c, sid = ss._bar_arrays(bars)
    first, last = {}, {}
    for i, d in enumerate(sid):
        first.setdefault(d, i)
        last[d] = i
    days = [d for d in sess.index if start <= d < ss.END and d in first]
    rng = np.random.default_rng(seed)
    means = np.empty(n_draws)
    for k in range(n_draws):
        rets = []
        for d in days:
            a, z = first[d], last[d]
            if z - a < 2:
                continue
            i = int(rng.integers(a + 1, z))   # not the first bar (keeps parity with 'needs a prior break')
            side = 1 if rng.random() < 0.5 else -1
            entry = o[i]
            _, px, _ = ss._manage(side, i, entry, spec, o, h, l, c, sid, z)
            rets.append(side * (px / entry - 1) - UND_COST)
        means[k] = np.mean(rets)
    return means


def fmt(d):
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def main():
    OUT.mkdir(exist_ok=True)
    bars = ss.load_bars("1h", "2y")
    sess = ss.session_table(bars)
    primary = ss.Spec()
    rows, opp_rows, all_trades = [], [], []

    # ---- primary spec in each period
    for name, start in ss.PERIODS.items():
        tr, fl, n_sess = run(primary, bars, sess, start)
        o = ss.opportunity_stats(fl, n_sess, tr)
        o["period"] = name
        opp_rows.append(o)
        tr["period"] = name
        all_trades.append(tr)
        for col, lab in (("ret_net", "underlying_net"), ("opt_ret", "option_return_on_premium"), ("opt_pnl", "option_pnl_rs_per_lot")):
            s = ss.summarise(tr, col)
            s.update(period=name, metric=lab, spec="primary")
            rows.append(s)
    pd.DataFrame(opp_rows).to_csv(OUT / "silver_opportunities.csv", index=False)
    pd.concat(all_trades).to_csv(OUT / "silver_trades_primary.csv", index=False)

    # ---- by side and by exit reason (1Y)
    tr1 = all_trades[1]
    side_rows = []
    for side, g in tr1.groupby("side"):
        for col in ("ret_net", "opt_ret"):
            s = ss.summarise(g, col)
            s.update(period="1Y", side=side, metric=col)
            side_rows.append(s)
    pd.DataFrame(side_rows).to_csv(OUT / "silver_by_side.csv", index=False)
    tr1.groupby("reason").agg(n=("ret_net", "size"), und=("ret_net", "mean"), opt=("opt_ret", "mean")).to_csv(OUT / "silver_by_reason.csv")

    # ---- bounds, sensitivities (1Y and 6M)
    for name in ("1Y", "6M"):
        start = ss.PERIODS[name]
        variants = {
            "conservative(primary)": (primary, ss.OptCost()),
            "optimistic_intrabar": (ss.Spec(conservative=False), ss.OptCost()),
            "carry_overnight": (ss.Spec(carry=True), ss.OptCost()),
            "no_stop": (ss.Spec(stop=None), ss.OptCost()),
            "enter_on_break(no pullback)": (ss.Spec(enter_on_break=True), ss.OptCost()),
            "break_buffer_0.3%": (ss.Spec(break_buffer=0.003), ss.OptCost()),
            "zero_spread_zero_cost": (primary, ss.OptCost(half_spread=0.0, ctt=0.0, flat=0.0)),
            "spread_3%/side": (primary, ss.OptCost(half_spread=0.03)),
            "IV x0.8": (primary, ss.OptCost(iv_mult=0.8)),
            "IV x1.25": (primary, ss.OptCost(iv_mult=1.25)),
        }
        for lab, (sp, co) in variants.items():
            tr, fl, n = run(sp, bars, sess, start, co)
            for col, m in (("ret_net", "underlying_net"), ("opt_ret", "option_return_on_premium")):
                s = ss.summarise(tr, col)
                s.update(period=name, metric=m, spec=lab, sessions=n)
                rows.append(s)

    # ---- exploratory grid
    grid_rows = []
    for p, t, s_ in itertools.product((0.0, 0.01, 0.02), (0.02, 0.03), (0.01, 0.015, None)):
        sp = ss.Spec(pullback=p, target=t, stop=s_)
        for name in ("6M", "1Y"):
            tr, fl, n = run(sp, bars, sess, ss.PERIODS[name])
            su = ss.summarise(tr, "ret_net")
            so = ss.summarise(tr, "opt_ret")
            grid_rows.append({"period": name, "pullback": p, "target": t, "stop": s_, "sessions": n,
                              "trades": su.get("n", 0), "setup_sessions": int((fl.buy_setup | fl.sell_setup).sum()),
                              "und_mean": su.get("mean"), "und_win": su.get("win"), "und_ci_lo": su.get("ci_lo"),
                              "opt_mean": so.get("mean"), "opt_win": so.get("win"), "opt_ci_lo": so.get("ci_lo"),
                              "opt_pnl_sum_rs": float(tr["opt_pnl"].sum()) if len(tr) else 0.0})
    pd.DataFrame(grid_rows).to_csv(OUT / "silver_grid.csv", index=False)
    pd.DataFrame(rows).to_csv(OUT / "silver_summary.csv", index=False)

    # ---- random timing null, 1Y and 6M
    null_rows = []
    for name in ("6M", "1Y"):
        start = ss.PERIODS[name]
        tr = all_trades[0 if name == "6M" else 1]
        means = random_null(bars, sess, primary, start)
        actual = tr["ret_net"].mean()
        null_rows.append({"period": name, "actual_mean_net": actual, "null_mean": means.mean(),
                          "null_p95": np.percentile(means, 95), "p_one_sided": float((means >= actual).mean())})
    pd.DataFrame(null_rows).to_csv(OUT / "silver_null.csv", index=False)

    # ---- top-3 removed (1Y, 6M)
    rob = []
    for name, tr in zip(("6M", "1Y"), (all_trades[0], all_trades[1])):
        for col in ("ret_net", "opt_ret"):
            x = np.sort(tr[col].to_numpy())
            rob.append({"period": name, "metric": col, "mean": x.mean(), "mean_minus_top3": x[:-3].mean()})
    pd.DataFrame(rob).to_csv(OUT / "silver_robust.csv", index=False)

    # ---- 15-minute resolution check (last ~60 days only)
    b15 = ss.load_bars("15m", "60d")
    s15 = ss.session_table(b15, min_bars=40)
    s15["prev_high"], s15["prev_low"] = s15["high"].shift(1), s15["low"].shift(1)
    start15 = s15.index[1]
    res15 = []
    for lab, bb, ss_ in (("15m", b15, s15), ("1h(same days)", bars, sess)):
        tr, fl, n = run(primary, bb, ss_, start15)
        o = ss.opportunity_stats(fl, n, tr)
        su = ss.summarise(tr, "ret_net") if len(tr) else {}
        o.update(label=lab, und_mean=su.get("mean"), und_win=su.get("win"), from_=str(start15.date()))
        res15.append(o)
    pd.DataFrame(res15).to_csv(OUT / "silver_resolution_check.csv", index=False)

    # ---- session volatility context
    ctx = []
    for name, start in ss.PERIODS.items():
        s = sess[(sess.index >= start) & (sess.index < ss.END)]
        rng_pct = (s["high"] / s["low"] - 1)
        ctx.append({"period": name, "sessions": len(s), "median_range_pct": rng_pct.median(), "mean_range_pct": rng_pct.mean(),
                    "p90_range_pct": rng_pct.quantile(0.9), "avg_rv20": s["rv20"].mean(),
                    "ret_pct": s["close"].iloc[-1] / s["open"].iloc[0] - 1})
    pd.DataFrame(ctx).to_csv(OUT / "silver_context.csv", index=False)
    print("done")


if __name__ == "__main__":
    main()
