"""Backtest engine.

Conventions (identical for every strategy so results are comparable):
* Daily bars. A signal is formed from data up to the CLOSE of day t and executed at the OPEN of day t+1.
* Returns are open-to-open on dividend/split-adjusted prices (total return).
* Long-only cash-equity (delivery) trading; uninvested capital earns RF (liquid-fund proxy).
* Costs are charged per side when a position is opened/closed (see COSTS).
* "Sleeve" portfolio: capital is split equally across all listed stocks; each sleeve trades its own signal
  independently, so there is no ranking/selection step that could hide look-ahead or add arbitrary choices.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

RF = 0.06  # annual cash yield on idle capital


@dataclass(frozen=True)
class Costs:
    """Per-side cost as a fraction of traded value. Delivery equity, discount broker (approximate, verify with broker):
    buy  : STT 0.10% + stamp 0.015% + exchange/SEBI ~0.003% + GST on those  ~= 0.12%
    sell : STT 0.10% + exchange/SEBI ~0.003% + DP charge (~Rs16 on a ~Rs1L position) ~= 0.12%
    plus slippage assumption (opening-auction/large-cap) 0.05% each side.
    """
    buy: float = 0.00119 + 0.0005
    sell: float = 0.00103 + 0.00016 + 0.0005

    def scaled(self, k: float) -> "Costs":
        return Costs(self.buy * k, self.sell * k)


COSTS = Costs()


# ---------------------------------------------------------------------------------------------------------------
# sleeve portfolio
# ---------------------------------------------------------------------------------------------------------------
def interval_returns(open_: pd.DataFrame) -> pd.DataFrame:
    """ret[i] = open[i+1]/open[i]-1, i.e. the return of holding from the open of day i to the open of day i+1."""
    return open_.shift(-1) / open_ - 1


def holdings(states: np.ndarray, ret: np.ndarray) -> np.ndarray:
    """H[i]=1 if we hold during interval i. State formed at close of i-1 -> executed at open of i."""
    H = np.zeros_like(states, dtype=np.int8)
    H[1:] = states[:-1]
    H[np.isnan(ret)] = 0
    return H


def sleeve_returns(H: np.ndarray, ret: np.ndarray, costs: Costs = COSTS, rf: float = RF, cols=None) -> np.ndarray:
    """Equal-weight portfolio of independent sleeves. Returns a (T,) array of daily returns (NaN where no stock listed)."""
    if cols is not None:
        H, ret = H[:, cols], ret[:, cols]
    rf_d = rf / 252
    prev = np.vstack([np.zeros((1, H.shape[1]), dtype=np.int8), H[:-1]])
    enter = (H == 1) & (prev == 0)
    leave = (H == 0) & (prev == 1)
    r = np.where(H == 1, ret, rf_d) - enter * costs.buy - leave * costs.sell
    r = np.where(np.isnan(ret), np.nan, r)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # all-NaN rows (before first listing / last bar)
        return np.nanmean(r, axis=1)


def extract_trades(H: np.ndarray, open_: np.ndarray, ret: np.ndarray, symbols, dates, costs: Costs = COSTS) -> pd.DataFrame:
    rows = []
    lr = np.log1p(np.nan_to_num(ret, nan=0.0))
    cs = np.cumsum(lr, axis=0)
    T, N = H.shape
    for j in range(N):
        h = H[:, j]
        d = np.diff(np.concatenate([[0], h, [0]]))
        for i0, i1 in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
            if i1 >= T:  # still open at the end -> excluded from closed-trade stats
                continue
            base = cs[i0 - 1, j] if i0 > 0 else 0.0
            gross = np.exp(cs[i1 - 1, j] - base) - 1
            net = (1 + gross) * (1 - costs.buy) * (1 - costs.sell) - 1
            rows.append((symbols[j], dates[i0], dates[i1], i1 - i0, gross, net))
    return pd.DataFrame(rows, columns=["symbol", "entry", "exit", "bars", "gross", "net"])


# ---------------------------------------------------------------------------------------------------------------
# cross-sectional (rank-based, monthly rebalanced) portfolios
# ---------------------------------------------------------------------------------------------------------------
def month_end_positions(index: pd.DatetimeIndex) -> np.ndarray:
    s = pd.Series(np.arange(len(index)), index=index)
    return s.groupby([index.year, index.month]).last().values


def rebalanced_returns(ret: np.ndarray, score: np.ndarray, top_n: int, costs: Costs = COSTS, rf: float = RF,
                       regime: np.ndarray | None = None, rng: np.random.Generator | None = None,
                       index: pd.DatetimeIndex | None = None, sig_pos: np.ndarray | None = None,
                       cols=None, return_weights: bool = False):
    """Equal-weight top-N by `score` (higher = better), rebalanced at the first open after each month end.

    Weights drift within the month (buy-and-hold); turnover costs are charged on the executed change versus the
    DRIFTED weights. If `rng` is given the N names are drawn at random from the eligible set (null model).
    """
    if cols is not None:
        ret, score = ret[:, cols], score[:, cols]
    T, Nn = ret.shape
    rf_d = rf / 252
    out = np.full(T, np.nan)
    w_cur = np.zeros(Nn)           # drifted weights entering the rebalance
    sig_pos = sig_pos if sig_pos is not None else month_end_positions(index)
    sig_pos = [k for k in sig_pos if k < T - 2]
    first_valid = None
    for n, k in enumerate(sig_pos):
        e = k + 1                                   # executed at the open of day e
        e_next = (sig_pos[n + 1] + 1) if n + 1 < len(sig_pos) else T - 1
        sc = score[k]
        elig = np.where(~np.isnan(sc))[0]
        if first_valid is None and len(elig) < top_n:
            continue                                  # not enough history yet -> stay in cash, no trade
        w_new = np.zeros(Nn)
        if len(elig) >= top_n and (regime is None or regime[k]):
            if rng is not None:
                pick = rng.choice(elig, size=top_n, replace=False)
            else:
                pick = elig[np.argsort(-sc[elig], kind="stable")[:top_n]]
            w_new[pick] = 1.0 / top_n                 # otherwise (risk-off regime) -> all cash
        if first_valid is None:
            first_valid = e
        buy = np.clip(w_new - w_cur, 0, None).sum()
        sell = np.clip(w_cur - w_new, 0, None).sum()
        seg = np.nan_to_num(ret[e:e_next], nan=0.0)             # intervals held under the new weights
        growth = np.cumprod(1 + seg, axis=0)                      # per-stock growth since rebalance
        cash_g = (1 + rf_d) ** np.arange(1, e_next - e + 1)
        val = growth @ w_new + (1 - w_new.sum()) * cash_g          # pre-cost value path of the month
        fac = 1 - buy * costs.buy - sell * costs.sell              # trading costs shrink the whole capital base
        val_c = val * fac
        prev_val = np.concatenate([[1.0], val_c[:-1]])
        out[e:e_next] = val_c / prev_val - 1
        w_cur = (growth[-1] * w_new) / val[-1] if len(val) else w_new
    if first_valid is not None:
        out[:first_valid] = rf_d
    return out


# ---------------------------------------------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------------------------------------------
def perf(r: pd.Series, rf: float = RF) -> dict:
    r = r.dropna()
    if len(r) < 50:
        return {k: np.nan for k in ("cagr", "vol", "sharpe", "sortino", "maxdd", "calmar", "total")}
    eq = (1 + r).cumprod()
    years = (r.index[-1] - r.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1
    vol = r.std() * np.sqrt(252)
    ex = r - rf / 252
    sharpe = ex.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan
    dn = ex[ex < 0]
    sortino = ex.mean() / np.sqrt((dn ** 2).sum() / len(ex)) * np.sqrt(252) if len(dn) else np.nan
    dd = eq / eq.cummax() - 1
    maxdd = dd.min()
    return dict(cagr=cagr, vol=vol, sharpe=sharpe, sortino=sortino, maxdd=maxdd,
                calmar=cagr / abs(maxdd) if maxdd < 0 else np.nan, total=eq.iloc[-1] - 1)


def trade_stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return dict(trades=0, win=np.nan, avg_net=np.nan, pf=np.nan, avg_win=np.nan, avg_loss=np.nan, hold=np.nan, payoff=np.nan)
    w, l = t.net[t.net > 0], t.net[t.net <= 0]
    return dict(trades=len(t), win=(t.net > 0).mean(), avg_net=t.net.mean(),
                pf=w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else np.nan,
                avg_win=w.mean() if len(w) else np.nan, avg_loss=l.mean() if len(l) else np.nan,
                payoff=(w.mean() / abs(l.mean())) if len(w) and len(l) and l.mean() != 0 else np.nan,
                hold=t.bars.mean())


def annual_returns(r: pd.Series) -> pd.Series:
    r = r.dropna()
    return (1 + r).groupby(r.index.year).prod() - 1


def sharpe_of(arr: np.ndarray, rf: float = RF) -> float:
    a = arr[~np.isnan(arr)]
    s = a.std()
    return (a.mean() - rf / 252) / s * np.sqrt(252) if s > 0 else np.nan


# ---------------------------------------------------------------------------------------------------------------
# null model: does the *timing* of the signal add anything beyond simply being invested the same fraction of time?
# ---------------------------------------------------------------------------------------------------------------
def circular_shift_null(S: np.ndarray, ret: np.ndarray, n_sims: int, seed: int = 7, costs: Costs = COSTS,
                        mask: np.ndarray | None = None) -> np.ndarray:
    """Randomly rotate each stock's state series within its listed span (keeps exposure, holding lengths and trade
    count; destroys timing). Returns the Sharpe ratio of the equal-weight sleeve portfolio for each simulation."""
    rng = np.random.default_rng(seed)
    T, N = S.shape
    valid = ~np.isnan(ret)
    spans = []
    for j in range(N):
        idx = np.where(valid[:, j])[0]
        spans.append((idx[0], idx[-1] + 1) if len(idx) else (0, 0))
    sims = np.empty(n_sims)
    for m in range(n_sims):
        S2 = S.copy()
        for j, (a, b) in enumerate(spans):
            if b - a > 2:
                S2[a:b, j] = np.roll(S[a:b, j], rng.integers(1, b - a))
        H = holdings(S2, ret)
        r = sleeve_returns(H, ret, costs)
        if mask is not None:
            r = r[mask]
        sims[m] = sharpe_of(r)
    return sims
