"""A TEST-ONLY strategy (break and pullback on the previous day's High/Low, in percent) used to exercise the framework: runs, the tick
pipeline, the replay and the online backtest. It is registered by tests/conftest.py and is never installed in the application."""
from datetime import datetime

from dryrun.strategy import Event, Params, StrategyEngine, StrategySpec, register

COMMON = {"qty", "slippage_pct", "cost_pct", "allow_both_sides", "max_trades_per_side", "max_trades_per_day", "daily_loss_limit_pct"}
DEFAULTS = {"pullback_pct": 1.0, "target_pct": 2.0, "stop_pct": 1.0, "break_buffer_pct": 0.0}


def split(kw: dict) -> tuple[dict, dict]:
    common = {k: v for k, v in kw.items() if k in COMMON}
    options = {k: v for k, v in kw.items() if k not in COMMON}
    return common, options


def P(**kw) -> Params:
    """Params for the demo strategy; strategy settings (pullback_pct, target_pct, stop_pct, break_buffer_pct) are routed into options."""
    common, options = split(kw)
    return Params(strategy="demo", options={**DEFAULTS, **options}, **common)


def jp(**kw) -> dict:
    """The same as a JSON body for the API."""
    common, options = split(kw)
    return {"strategy": "demo", "options": options, **common}


def _validate(o: dict) -> None:
    o = {**DEFAULTS, **o}
    if o["pullback_pct"] < 0 or o["target_pct"] <= 0 or o["break_buffer_pct"] < 0:
        raise ValueError("pullback_pct and break_buffer_pct must be >= 0 and target_pct > 0")
    if o["stop_pct"] is not None and o["stop_pct"] <= 0:
        raise ValueError("stop_pct must be > 0 or empty")


class DemoEngine(StrategyEngine):
    def __init__(self, high, low, params, approve=None):
        super().__init__(high, low, params, approve)
        self.o = {**DEFAULTS, **params.options}
        self.armed = self.flags = {"BUY": False, "SELL": False}

    @property
    def break_up_level(self): return self.high * (1 + self.o["break_buffer_pct"] / 100)

    @property
    def break_down_level(self): return self.low * (1 - self.o["break_buffer_pct"] / 100)

    @property
    def buy_entry_level(self): return self.high * (1 - self.o["pullback_pct"] / 100)

    @property
    def sell_entry_level(self): return self.low * (1 + self.o["pullback_pct"] / 100)

    def levels(self):
        return [{"name": "prev High", "value": self.high, "color": "blue"}, {"name": "buy trigger", "value": self.buy_entry_level, "color": "green"},
                {"name": "sell trigger", "value": self.sell_entry_level, "color": "red"}, {"name": "prev Low", "value": self.low, "color": "orange"}]

    def evaluate(self, ts: datetime, price: float) -> list[Event]:
        out: list[Event] = []
        for side in ("BUY", "SELL"):
            up = side == "BUY"
            broke = price > self.break_up_level if up else price < self.break_down_level
            level = self.high if up else self.low
            if not self.armed[side]:
                if broke and self.trades_by_side[side] < self.p.max_trades_per_side:
                    self.armed[side] = True
                    out.append(Event("break", ts, side, price, level, "price broke previous day %s" % ("High" if up else "Low")))
                continue
            if not (price <= self.buy_entry_level if up else price >= self.sell_entry_level):
                continue
            self.armed[side] = False
            sign = 1 if up else -1
            tgt, stp = self.o["target_pct"], self.o["stop_pct"]
            out += self.signal(side, ts, price, level, "pullback after break",
                               lambda e, sign=sign, tgt=tgt, stp=stp: (None if stp is None else e * (1 - sign * stp / 100), e * (1 + sign * tgt / 100)))
        return out

    def describe_event(self, ev):
        if ev.kind == "break":
            up = ev.side == "BUY"
            return (f"Price {ev.price:,.2f} broke {'above the previous High' if up else 'below the previous Low'} ({self.high if up else self.low:,.2f}). "
                    f"Waiting for a pullback to {'<= ' + format(self.buy_entry_level, ',.2f') if up else '>= ' + format(self.sell_entry_level, ',.2f')}.")
        return ev.reason

    def status_text(self, price):
        notes = []
        if self.armed["BUY"]:
            notes.append(f"Broke above the previous High; waiting for a pullback to {self.buy_entry_level:,.2f} or lower to BUY.")
        if self.armed["SELL"]:
            notes.append(f"Broke below the previous Low; waiting for a pullback to {self.sell_entry_level:,.2f} or higher to SELL.")
        return " ".join(notes)


DEMO = register(StrategySpec(
    name="demo", label="Demo break and pullback (tests only)", engine=DemoEngine, validate=_validate,
    fields=[{"key": "pullback_pct", "label": "Pullback %", "type": "number", "default": 1.0, "step": 0.05},
            {"key": "target_pct", "label": "Target %", "type": "number", "default": 2.0, "step": 0.1},
            {"key": "stop_pct", "label": "Stop %", "type": "number", "default": 1.0, "step": 0.1}],
    preview=lambda h, l, o: {"summary": f"High {h} Low {l}", "cards": [{"title": "BUY", "rows": [["Entry", f"{h * (1 - (o.get('pullback_pct', 1.0)) / 100):.2f}"]]}], "notes": []},
    describe=lambda h, l, o: "Demo strategy."))
