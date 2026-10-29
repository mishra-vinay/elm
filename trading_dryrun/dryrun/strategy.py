"""Strategy framework. No strategy ships with the app: add one by writing a `StrategyEngine` subclass and registering it (see `strategies/`).

The engine here holds everything that is the same for every strategy, so a new rule only has to say *when to enter and where its stop and
target are*: one open position at a time (optional both sides), slippage on market fills, a limit target, a market stop, round-trip costs,
the daily loss limit, the trade-count limits, the independent risk gate (`approve`), square-off and the run summary.

Pure logic: no database, no network, no clock. Feed it (timestamp, price) ticks and it returns events. The live dry-run runner, the historical
replay and the online backtest all build the engine through `make_engine`, so a rule cannot drift between testing and running.

To add a strategy:

    class MyEngine(StrategyEngine):
        def evaluate(self, ts, price):            # called on every tick after any open position has been managed
            ...                                    # return a list of Events; to open a trade call self._open(...)

    register(StrategySpec(name="my_strategy", label="My strategy", engine=MyEngine,
                          fields=[{"key": "distance_pct", "label": "Distance %", "type": "number", "default": 1.0, "step": 0.1}]))
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from typing import Callable


class NoStrategyError(ValueError):
    pass


@dataclass
class Params:
    strategy: str = ""                 # name of a registered strategy
    options: dict = field(default_factory=dict)   # that strategy's own settings (see its StrategySpec.fields)
    qty: float = 1.0                   # units (shares / contract units) per trade
    slippage_pct: float = 0.02         # simulated slippage per side, % of price
    cost_pct: float = 0.10             # round-trip costs, % of entry value (brokerage, taxes, fees)
    allow_both_sides: bool = False     # False = one open position at a time per run
    max_trades_per_side: int = 1       # per day
    max_trades_per_day: int = 2        # risk limit across both sides
    daily_loss_limit_pct: float | None = 2.0   # stop opening trades when the run's net P&L (% of entry value, summed) <= -this

    def validate(self) -> None:
        if self.qty <= 0:
            raise ValueError("qty must be > 0")
        spec = get_strategy(self.strategy)
        if spec.validate is not None:
            spec.validate(self.options)

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
    kind: str                 # entry_signal | entry_skipped | entry | exit | any short label a strategy adds (e.g. "break", "armed"; at most 8 characters)
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


class StrategyEngine:
    """Generic trade mechanics. Subclasses implement `evaluate` (and optionally `levels`, `status_text`, `describe_event`)."""

    def __init__(self, high: float, low: float, params: Params, approve=None):
        """approve(side, ts, price, state) -> (bool, reason): independent risk gate called before every entry."""
        if not (high > low > 0):
            raise ValueError(f"need High > Low > 0, got High={high} Low={low}")
        params.validate()
        self.high, self.low, self.p = high, low, params
        self.options = params.options
        self.approve = approve
        self.flags: dict[str, bool] = {}              # free-form booleans a strategy exposes to the dashboard (e.g. "BUY armed")
        self.trades_by_side = {"BUY": 0, "SELL": 0}
        self.position: Position | None = None
        self.closed: list[dict] = []
        self.last_price: float | None = None
        self.day_open: float | None = None            # the session's opening price; the runner sets it when a run starts mid-session, else the first price seen
        self.session_open: time | None = None         # the session's opening time; set by the runner and the replay, None = the first tick's time
        self.symbol: str | None = None                # the instrument's symbol; set by the runner and the replay (used for the lot size when a strategy sizes its trades)
        self.net_pct_sum = 0.0
        self.session_over = False

    # --- hooks for a strategy ------------------------------------------------------------------
    def evaluate(self, ts: datetime, price: float) -> list[Event]:
        raise NotImplementedError("a strategy must implement evaluate()")

    def levels(self) -> list[dict]:
        """Horizontal lines for the dashboard chart: [{"name", "value", "color"}], color one of blue green red orange muted."""
        return [{"name": "prev High", "value": self.high, "color": "blue"}, {"name": "prev Low", "value": self.low, "color": "orange"}]

    def status_text(self, price: float | None) -> str:
        return ""

    def describe_event(self, ev: Event) -> str | None:
        return ev.reason

    def qty_for(self, side: str, entry: float, stop: float | None) -> float:
        """Units for a trade about to be opened at `entry` with `stop`. The default is the run's fixed quantity; a strategy can size each trade (e.g. by a maximum loss)."""
        return self.p.qty

    # --- ticks ---------------------------------------------------------------------------------
    def state(self) -> dict:
        return {"flags": dict(self.flags), "trades_today": sum(self.trades_by_side.values()), "net_pct_sum": self.net_pct_sum,
                "position": None if self.position is None else self.position.side}

    def on_tick(self, ts: datetime, price: float) -> list[Event]:
        if self.session_over or price <= 0:
            return []
        self.last_price = price
        events: list[Event] = []
        if self.position is not None:
            ev = self._manage(ts, price)
            if ev:
                events.append(ev)
        events.extend(self.evaluate(ts, price))
        return events

    def block_reason(self, side: str, ts: datetime, price: float) -> str | None:
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

    def signal(self, side: str, ts: datetime, price: float, level: float | None, reason: str,
               exits: Callable[[float], tuple[float | None, float]]) -> list[Event]:
        """The usual entry sequence: an entry_signal event, then either entry_skipped (with the reason) or the entry itself.
        `exits(entry_price)` returns (stop or None, target) in absolute prices."""
        out = [Event("entry_signal", ts, side, price, level, reason)]
        blocked = self.block_reason(side, ts, price)
        if blocked:
            out.append(Event("entry_skipped", ts, side, price, level, blocked))
        else:
            out.append(self._enter(side, ts, price, level, exits))
        return out

    def _enter(self, side: str, ts: datetime, price: float, level: float | None, exits: Callable[[float], tuple[float | None, float]]) -> Event:
        slip = price * self.p.slippage_pct / 100
        entry = price + slip if side == "BUY" else price - slip
        stop, target = exits(entry)
        self.position = Position(side, ts, price, entry, target, stop, self.qty_for(side, entry, stop))
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


# ------------------------------------------------------------------------------------------------
# registry
@dataclass
class StrategySpec:
    name: str                                   # stored in Params.strategy and in every run's record
    label: str                                  # shown in the dropdown
    engine: type                                # a StrategyEngine subclass
    fields: list[dict] = field(default_factory=list)   # settings form: {"key","label","type": number|select,"default","step","choices": [[value,label]],"help"}   (most strategies need no form: their rules and numbers are written in the strategy file)
    validate: Callable[[dict], None] | None = None     # raise ValueError for bad options
    preview: Callable[[float, float, dict], dict | None] | None = None   # (high, low, options) -> {"summary": str, "cards": [{"title","rows":[[k,v]]}], "notes": [str]}
    describe: Callable[[float, float, dict], str] | None = None          # one-line description for the run log
    blurb: str = ""                                                      # one plain-English paragraph shown under the strategy dropdown
    hidden: bool = False                                                 # research strategies: usable by name (tests, tools) but not listed in the dropdowns
    guide: list = field(default_factory=list)                            # the Strategies page: [(heading, [paragraph, ...]), ...] describing the rules step by step

    def public(self) -> dict:
        return {"name": self.name, "label": self.label, "blurb": self.blurb, "fields": self.fields, "has_preview": self.preview is not None,
                "guide": [{"title": t, "items": list(items)} for t, items in self.guide]}


STRATEGIES: dict[str, StrategySpec] = {}


def register(spec: StrategySpec) -> StrategySpec:
    STRATEGIES[spec.name] = spec
    return spec


def unregister(name: str) -> None:
    STRATEGIES.pop(name, None)


def get_strategy(name: str) -> StrategySpec:
    if not STRATEGIES:
        raise NoStrategyError("No strategy is installed yet. Add one in dryrun/strategies/ (see the README).")
    spec = STRATEGIES.get(name)
    if spec is None:
        raise NoStrategyError(f"unknown strategy '{name}'; installed: {', '.join(sorted(STRATEGIES))}")
    return spec


def make_engine(high: float, low: float, params: Params, approve=None) -> StrategyEngine:
    """The engine for params.strategy (shared by the live runner, the replay and the backtest)."""
    return get_strategy(params.strategy).engine(high, low, params, approve=approve)
