"""Buyback event study:  python -m strategy_lab.buyback_study

Rule under test (as specified): BUY after a tender-offer buyback is announced; SELL at the record date, or earlier if the
price reaches the offer price. Also tests holding through the tender (acceptance-ratio scenarios).

Pre-registered success criteria (fixed before the first run), for the main variant 'sell at record-date close':
  S1  mean net return per trade, after costs, is > 0 with a bootstrap 95% CI that excludes 0
  S2  mean excess return over the Nifty 50 for the same window is > 0
  S3  mean return beats the placebo (random same-length windows in the SAME stocks) at p <= 0.05
  S4  S1 also holds in BOTH sub-periods (2016-2021 and 2022-2026)
Execution: entry at the OPEN of the first trading day on which the announcement is public (announced before 09:00 ->
same day, otherwise next trading day); the announcement-day reaction already in the price at that open is NOT captured.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import data
from .engine import COSTS

R = Path(__file__).parent / "results"
CACHE = Path(__file__).parent / ".cache" / "buyback"
COST_RT = COSTS.buy + COSTS.sell            # ~0.34% round trip (delivery); tender-accepted shares charged the same (conservative)
ACCEPT = (0.15, 0.30, 0.50, 1.00)           # acceptance-ratio scenarios for holding through the tender
RESIDUAL_DAYS = 20                          # unaccepted shares sold ~20 trading days after the record date (assumption)
N_PLACEBO = 300


def clean_one(df: pd.DataFrame) -> pd.DataFrame:
    """Same artefact rule as the main study: a one-day fall of >= 30% is an unadjusted corporate action."""
    df = df.copy()
    r = df.close / df.close.shift(1) - 1
    for d in df.index[(r <= -0.30).fillna(False).values]:
        f = df.close.at[d] / df.close.shift(1).at[d]
        m = df.index < d
        df.loc[m, ["open", "high", "low", "close", "raw_close"]] *= f
    return df


def load_prices(symbols):
    out = {}
    for s in symbols:
        try:
            df = data.load(s)
        except Exception:
            df = None
        if df is not None and len(df) > 300:
            out[s] = clean_one(df)
    return out


def trading_pos(index: pd.DatetimeIndex, ts: pd.Timestamp, before_open_cutoff="09:00") -> int | None:
    """Index position of the first open at which a filing stamped `ts` can be acted on."""
    day = ts.normalize()
    pos = index.searchsorted(day)                       # first trading day >= day
    if pos >= len(index):
        return None
    if index[pos] == day and ts.time() < pd.Timestamp(before_open_cutoff).time():
        return pos                                      # public before the open -> trade at that open
    return pos + 1 if index[pos] == day else pos        # else next trading day's open


def build_trades() -> pd.DataFrame:
    ev = pd.read_csv(CACHE / "buyback_events.csv", parse_dates=["rec_date", "ex_date", "first_ts", "approval_ts"])
    px = load_prices(sorted(ev.symbol.unique()))
    nifty = data.load("^NSEI")
    rows = []
    for _, e in ev.iterrows():
        d = px.get(e.symbol)
        if d is None or pd.isna(e.approval_ts):
            rows.append(dict(**e.to_dict(), status="no price data" if d is None else "no announcement date")); continue
        idx = d.index
        i0 = trading_pos(idx, e.approval_ts)
        ir = idx.searchsorted(e.rec_date)               # record-date position
        if i0 is None or ir >= len(idx) or idx[min(ir, len(idx) - 1)] != e.rec_date and ir >= len(idx):
            rows.append(dict(**e.to_dict(), status="out of range")); continue
        if i0 >= ir:
            rows.append(dict(**e.to_dict(), status="announced too close to record date")); continue
        ie = idx.searchsorted(e.ex_date) if pd.notna(e.ex_date) else ir
        i_cum = max(i0, min(ie, ir) - 1)                # last cum day (day before ex-date)
        entry = d.open.iloc[i0]
        raw_entry = d.open.iloc[i0] * d.raw_close.iloc[i0] / d.close.iloc[i0]
        pre_close_raw = d.raw_close.iloc[max(i0 - 1, 0)]
        ann_react = d.close.iloc[min(i0, len(d) - 1)] / d.close.iloc[max(i0 - 1, 0)] - 1
        # exit variants (adjusted prices -> total return)
        rec_close = d.close.iloc[ir]; cum_close = d.close.iloc[i_cum]
        ratio = d.raw_close.iloc[ir] / d.close.iloc[ir]
        # take-profit at the offer price (price series compared on the raw/traded-price scale)
        tp_hit, tp_exit = False, rec_close
        if pd.notna(e.offer_price):
            seg_hi = (d.high.iloc[i0:ir + 1] * (d.raw_close.iloc[i0:ir + 1] / d.close.iloc[i0:ir + 1])).values
            hit = np.where(seg_hi >= e.offer_price)[0]
            if len(hit):
                tp_hit = True; k = i0 + hit[0]
                tp_exit = entry * (e.offer_price / (d.open.iloc[i0] * d.raw_close.iloc[i0] / d.close.iloc[i0]))
        res_pos = min(ir + RESIDUAL_DAYS, len(d) - 1)
        res_close = d.close.iloc[res_pos]
        # market
        nf = nifty.close
        nr = nf.iloc[nf.index.searchsorted(idx[ir])] / nf.iloc[nf.index.searchsorted(idx[i0])] - 1 if idx[ir] <= nf.index[-1] else np.nan
        tr = dict(**e.to_dict(), status="ok", entry_date=idx[i0], rec_pos=ir, entry_pos=i0, hold_days=ir - i0,
                  entry_px=raw_entry, offer_premium=(e.offer_price / pre_close_raw - 1) if pd.notna(e.offer_price) else np.nan,
                  gap_to_offer_at_rec=(e.offer_price / (d.raw_close.iloc[ir]) - 1) if pd.notna(e.offer_price) else np.nan,
                  ann_day_reaction=ann_react,
                  r_rec_close=rec_close / entry - 1, r_cum_close=cum_close / entry - 1,
                  r_tp=tp_exit / entry - 1, tp_hit=tp_hit, nifty_ret=nr,
                  r_resid=res_close / entry - 1)
        # tender scenarios: accepted fraction receives offer price (converted to adjusted scale), rest sold at residual close
        if pd.notna(e.offer_price):
            offer_adj = e.offer_price / ratio
            for a in ACCEPT:
                tr[f"r_tender_{int(a * 100)}"] = (a * offer_adj + (1 - a) * res_close) / entry - 1
        rows.append(tr)
    t = pd.DataFrame(rows)
    return t, px


def stats(x: pd.Series, name: str, cost=COST_RT) -> dict:
    n = x.dropna(); net = n - cost
    rng = np.random.default_rng(3)
    bs = np.array([rng.choice(net.values, len(net)).mean() for _ in range(2000)])
    return dict(variant=name, n=len(net), mean_gross=n.mean(), mean_net=net.mean(), median_net=net.median(),
                win_rate=(net > 0).mean(), ci_lo=np.percentile(bs, 2.5), ci_hi=np.percentile(bs, 97.5),
                worst=net.min(), best=net.max(), sharpe_per_trade=net.mean() / net.std() if net.std() > 0 else np.nan)


def placebo(t: pd.DataFrame, px: dict) -> np.ndarray:
    """Mean gross return of random same-length windows in the same stocks (event windows excluded +/-60 days)."""
    rng = np.random.default_rng(5)
    ok = t[t.status == "ok"]
    sims = np.empty(N_PLACEBO)
    pools = []
    for _, e in ok.iterrows():
        d = px[e.symbol]; n = len(d)
        L = int(e.rec_pos - e.entry_pos)
        bad = np.zeros(n, bool); lo, hi = max(0, int(e.entry_pos) - 60), min(n, int(e.rec_pos) + 60); bad[lo:hi] = True
        starts = np.where(~bad[: n - L - 1])[0]
        starts = starts[(d.index[starts] >= pd.Timestamp("2016-01-01"))]
        pools.append((d.open.values, d.close.values, starts, L))
    for k in range(N_PLACEBO):
        rr = []
        for o, c, st, L in pools:
            if len(st) == 0:
                continue
            s0 = rng.choice(st); rr.append(c[s0 + L] / o[s0] - 1)
        sims[k] = np.mean(rr)
    return sims


def main():
    t0 = time.time()
    t, px = build_trades()
    ok = t[t.status == "ok"].copy()
    t.to_csv(R / "buyback_trades.csv", index=False)
    print("events:", len(t), "| usable trades:", len(ok)); print(t.status.value_counts().to_string())
    print("with parsed offer price:", ok.offer_price.notna().sum())
    ok["period"] = np.where(ok.rec_date < "2022-01-01", "2016-2021", "2022-2026")
    ok["excess"] = ok.r_rec_close - ok.nifty_ret
    rows = [stats(ok.r_rec_close, "A. sell at record-date close (main)"),
            stats(ok.r_cum_close, "B. sell at close of last cum-date (day before ex-date)"),
            stats(ok.r_tp, "C. sell at offer price if reached, else record-date close"),
            stats(ok.r_resid, f"D. hold {RESIDUAL_DAYS} trading days past record date, no tender"),
            stats(ok.excess, "A minus Nifty 50 over same window (excess)")]
    for a in ACCEPT:
        c = f"r_tender_{int(a * 100)}"
        if c in ok:
            rows.append(stats(ok[c], f"E. tender, {int(a * 100)}% accepted (rest sold {RESIDUAL_DAYS}d after record date)"))
    for per, g in ok.groupby("period"):
        rows.append(stats(g.r_rec_close, f"A in {per}"))
    for lab, g in [("premium < 10%", ok[ok.offer_premium < 0.10]), ("premium 10-25%", ok[(ok.offer_premium >= .10) & (ok.offer_premium < .25)]),
                   ("premium >= 25%", ok[ok.offer_premium >= .25])]:
        if len(g) >= 8:
            rows.append(stats(g.r_rec_close, f"A, offer {lab}"))
    res = pd.DataFrame(rows); res.to_csv(R / "buyback_results.csv", index=False)
    sims = placebo(t, px)
    pl = pd.DataFrame({"placebo_mean_gross": sims}); pl.to_csv(R / "buyback_placebo.csv", index=False)
    actual = ok.r_rec_close.mean()
    p = (1 + (sims >= actual).sum()) / (1 + len(sims))
    pd.set_option("display.width", 220); pd.set_option("display.max_colwidth", 70)
    show = res.copy()
    for c in ("mean_gross", "mean_net", "median_net", "ci_lo", "ci_hi", "worst", "best"):
        show[c] = (show[c] * 100).round(2)
    show["win_rate"] = (show.win_rate * 100).round(0); show["sharpe_per_trade"] = show.sharpe_per_trade.round(2)
    print(show.to_string(index=False))
    print(f"\nplacebo (random same-length windows, same stocks): mean {sims.mean():.2%} sd {sims.std():.2%} | actual {actual:.2%} | p={p:.3f}")
    A = res.iloc[0]
    sub = {k: res[res.variant == f"A in {k}"].iloc[0] for k in ("2016-2021", "2022-2026") if (res.variant == f"A in {k}").any()}
    s1 = (A.mean_net > 0) and (A.ci_lo > 0); s2 = res[res.variant.str.startswith("A minus")].iloc[0].mean_net + COST_RT > 0  # excess is gross of cost; net deducted below
    exc = ok.excess.mean() - COST_RT
    s3 = p <= 0.05; s4 = all((v.mean_net > 0 and v.ci_lo > 0) for v in sub.values())
    print(f"\nS1 net mean>0 & CI excludes 0 ({A.mean_net:.2%}, CI {A.ci_lo:.2%}..{A.ci_hi:.2%}): {s1}")
    print(f"S2 excess over Nifty after costs > 0 ({exc:.2%}): {exc > 0}")
    print(f"S3 beats placebo p<=0.05 (p={p:.3f}): {s3}")
    print(f"S4 positive in both sub-periods: {s4}  {[(k, round(v.mean_net * 100, 2)) for k, v in sub.items()]}")
    print(f"\nannouncement-day reaction already priced at entry open: mean {ok.ann_day_reaction.mean():.2%} median {ok.ann_day_reaction.median():.2%}")
    print(f"offer premium to pre-announcement close: mean {ok.offer_premium.mean():.1%} median {ok.offer_premium.median():.1%} | gap to offer at record date: mean {ok.gap_to_offer_at_rec.mean():.1%} | TP hit rate {ok.tp_hit.mean():.0%}")
    print(f"hold (trading days announce->record): mean {ok.hold_days.mean():.0f} median {ok.hold_days.median():.0f}")
    print(f"DONE {time.time() - t0:.0f}s")
    return ok, res, sims


if __name__ == "__main__":
    main()
