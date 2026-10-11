"""Previous-day High/Low break-and-pullback engine.

Pure logic: no database, no network, no clock. Feed it (timestamp, price) ticks and it returns events.
The same class is used by the live dry-run runner and by the historical replay (dryrun.replay), so the
rule cannot drift between testing and running.

Rule
  BUY : price trades above the previous-day High (+break_buffer), later pulls back `pullback` below
        the High  -> buy signal, enter at that tick.
  SELL: price trades below the previous-day Low (-break_buffer), later pulls back `pullback` above
        the Low   -> sell signal, enter at that tick.
  Exit: +target (limit, fills at the target price), -stop (market, fills at the tick that crossed it),
        or square-off at the end of the session (market).
Validation status: the rule was backtested on silver and Bajaj Finance and showed no edge
(docs/silver-options-study.md, docs/bajajfinance-study.md). Running it here is for data collection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass
class Params:
    pullback_pct: float = 1.0          # how far back inside the broken level the entry needs (0 = retest of the level)
    target_pct: float = 2.0            # profit target from entry, %
    stop_pct: float | None = 1.0       # stop-loss from entry, %; None = no stop
    break_buffer_pct: float = 0.0      # break must exceed the level by this %
    qty: float = 1.0                   # units (shares / contract units) per trade
    slippage_pct: float = 0.02         # simulated slippage per side, % of price
    cost_pct: float = 0.10             # round-trip costs, % of entry value (brokerage, taxes, fees)
    allow_both_sides: bool = False     # False = one open position at a time per run
    max_trades_per_side: int = 1       # per day; >1 allows re-entry after a fresh break
    max_trades_per_day: int = 2        # risk limit across both sides
    daily_loss_limit_pct: float | None = 2.0   # stop opening trades when the run's net P&L (% of entry value, summed) <= -this

    def validate(self) -> None:
        if self.pullback_pct < 0 or self.target_pct <= 0 or self.break_buffer_pct < 0:
            raise ValueError("pullback_pct and break_buffer_pct must be >= 0 and target_pct > 0")
        if self.stop_pct is not None and self.stop_pct <= 0:
            raise ValueError("stop_pct must be > 0 or empty")
        if self.qty <= 0:
            raise ValueError("qty must be > 0")

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        p = cls(**known)
        p.validate()
        return p


@dataclass
class Event:
    kind: str                 # break | entry_signal | entry_skipped | entry | exit
    ts: datetime
    side: str                 # BUY | SELL
    price: float
    level: float | None = None
    reason: str | None = None
    trade: dict | None = None  # for entry/exit: the position snapshot


@dataclass
class Position:
    side: str
    entry_ts: datetime
    signal_price: float
    entry_price: float
    target: float
    stop: float | None
    qty: float


class BreakoutPullbackEngine:
    def __init__(self, high: float, low: float, params: Params, approve=None):
        """approve(side, ts, price, state) -> (bool, reason): independent risk gate called before every entry."""
        if not (high > low > 0):
            raise ValueError(f"need High > Low > 0, got High={high} Low={low}")
        params.validate()
        self.high, self.low, self.p = high, low, params
        self.approve = approve
        self.armed = {"BUY": False, "SELL": False}
        self.trades_by_side = {"BUY": 0, "SELL": 0}
        self.position: Position | None = None
        self.closed: list[dict] = []
        self.last_price: float | None = None
        self.net_pct_sum = 0.0
        self.session_over = False

    # --- levels -------------------------------------------------------------------------------
    @property
    def break_up_level(self) -> float:
        return self.high * (1 + self.p.break_buffer_pct / 100)

    @property
    def break_down_level(self) -> float:
        return self.low * (1 - self.p.break_buffer_pct / 100)

    @property
    def buy_entry_level(self) -> float:
        return self.high * (1 - self.p.pullback_pct / 100)

    @property
    def sell_entry_level(self) -> float:
        return self.low * (1 + self.p.pullback_pct / 100)

    def state(self) -> dict:
        return {"armed_buy": self.armed["BUY"], "armed_sell": self.armed["SELL"],
                "trades_today": sum(self.trades_by_side.values()), "net_pct_sum": self.net_pct_sum,
                "position": None if self.position is None else self.position.side}

    # --- ticks --------------------------------------------------------------------------------
    def on_tick(self, ts: datetime, price: float) -> list[Event]:
        if self.session_over or price <= 0:
            return []
        self.last_price = price
        events: list[Event] = []
        if self.position is not None:
            ev = self._manage(ts, price)
            if ev:
                events.append(ev)
        # break detection (arming) and entry; a single tick can arm or trigger a side, never both
        for side in ("BUY", "SELL"):
            events.extend(self._side(side, ts, price))
        return events

    def _side(self, side: str, ts: datetime, price: float) -> list[Event]:
        out: list[Event] = []
        up = side == "BUY"
        broke = price > self.break_up_level if up else price < self.break_down_level
        level = self.high if up else self.low
        if not self.armed[side]:
            if broke and self.trades_by_side[side] < self.p.max_trades_per_side:
                self.armed[side] = True
                out.append(Event("break", ts, side, price, level, "price broke previous day %s" % ("High" if up else "Low")))
            return out
        pulled_back = price <= self.buy_entry_level if up else price >= self.sell_entry_level
        if not pulled_back:
            return out
        self.armed[side] = False          # one shot per break; a re-entry needs a fresh break
        out.append(Event("entry_signal", ts, side, price, level, "pullback after break"))
        reason = self._block_reason(side, ts, price)
        if reason:
            out.append(Event("entry_skipped", ts, side, price, level, reason))
            return out
        out.append(self._enter(side, ts, price, level))
        return out

    def _block_reason(self, side: str, ts: datetime, price: float) -> str | None:
        if self.position is not None and not self.p.allow_both_sides:
            return "a position is already open"
        if self.position is not None and self.position.side == side:
            return "same-side position already open"
        if sum(self.trades_by_side.values()) >= self.p.max_trades_per_day:
            return "max trades per day reached"
        if self.p.daily_loss_limit_pct is not None and self.net_pct_sum <= -self.p.daily_loss_limit_pct:
            return "daily loss limit reached"
        if self.approve is not None:
            ok, why = self.approve(side, ts, price, self.state())
            if not ok:
                return why or "risk engine rejected"
        return None

    def _enter(self, side: str, ts: datetime, price: float, level: float) -> Event:
        slip = price * self.p.slippage_pct / 100
        entry = price + slip if side == "BUY" else price - slip
        sign = 1 if side == "BUY" else -1
        target = entry * (1 + sign * self.p.target_pct / 100)
        stop = None if self.p.stop_pct is None else entry * (1 - sign * self.p.stop_pct / 100)
        self.position = Position(side, ts, price, entry, target, stop, self.p.qty)
        self.trades_by_side[side] += 1
        return Event("entry", ts, side, entry, level, None, trade=self._snap(self.position))

    def _manage(self, ts: datetime, price: float) -> Event | None:
        pos = self.position
        assert pos is not None
        buy = pos.side == "BUY"
        if pos.stop is not None and (price <= pos.stop if buy else price >= pos.stop):
            return self._exit(ts, price, "stop", market=True)
        if price >= pos.target if buy else price <= pos.target:
            return self._exit(ts, pos.target, "target", market=False)     # limit order: fills at the target
        return None

    def _exit(self, ts: datetime, price: float, reason: str, market: bool) -> Event:
        pos = self.position
        assert pos is not None
        buy = pos.side == "BUY"
        slip = price * self.p.slippage_pct / 100 if market else 0.0
        exit_price = price - slip if buy else price + slip
        sign = 1 if buy else -1
        gross = sign * (exit_price - pos.entry_price) * pos.qty
        value = pos.entry_price * pos.qty
        cost = value * self.p.cost_pct / 100
        net = gross - cost
        rec = {**self._snap(pos), "exit_ts": ts, "exit_price": exit_price, "exit_reason": reason,
               "gross_pnl": gross, "cost": cost, "net_pnl": net, "net_pct": net / value * 100}
        self.closed.append(rec)
        self.net_pct_sum += rec["net_pct"]
        self.position = None
        return Event("exit", ts, pos.side, exit_price, None, reason, trade=rec)

    @staticmethod
    def _snap(pos: Position) -> dict:
        return {"side": pos.side, "entry_ts": pos.entry_ts, "signal_price": pos.signal_price, "entry_price": pos.entry_price,
                "target": pos.target, "stop": pos.stop, "qty": pos.qty}

    # --- end of session ------------------------------------------------------------------------
    def square_off(self, ts: datetime, price: float | None = None, reason: str = "squareoff") -> list[Event]:
        price = price if price is not None else self.last_price
        events: list[Event] = []
        if self.position is not None and price is not None:
            events.append(self._exit(ts, price, reason, market=True))
        self.session_over = True
        return events

    def summary(self) -> dict:
        t = self.closed
        wins = [x for x in t if x["net_pnl"] > 0]
        return {"trades": len(t), "wins": len(wins), "losses": len(t) - len(wins),
                "win_rate": (len(wins) / len(t)) if t else None,
                "gross_pnl": sum(x["gross_pnl"] for x in t), "costs": sum(x["cost"] for x in t),
                "net_pnl": sum(x["net_pnl"] for x in t), "net_pct_sum": self.net_pct_sum,
                "targets": sum(1 for x in t if x["exit_reason"] == "target"),
                "stops": sum(1 for x in t if x["exit_reason"] == "stop"),
                "squareoffs": sum(1 for x in t if x["exit_reason"] in ("squareoff", "manual"))}
