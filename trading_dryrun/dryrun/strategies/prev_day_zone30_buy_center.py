"""30% opening, buy at the center. The whole strategy is in this file: the numbers are in RULES, the logic is in CenterEngine. Nothing is configured on a page.

It reads the day like `prev_day_zone30`, but it does not buy at the open: it buys at the CENTER of yesterday's range.

PDH, PDL = yesterday's High and Low, D = PDH - PDL, zone = 30% of D, centre = (PDH + PDL) / 2.

  1. The day's real OPENING price must be in the TOP zone [PDH - 0.3 D, PDH]. An open anywhere else (bottom zone, between the zones, above PDH or below PDL)
     means no trade that day. This strategy only buys.
  2. After the first candle (SKIP_MINUTES) a BUY order rests at the centre. It fills when price falls to the centre (the open was above it, so price has to come down).
     If price is already at or below the centre when the order starts, it fills at once at that price (a buy limit at a higher price fills at the market).
  3. Stop = PDL, exactly. Target = entry + TARGET_X_STOP (1.4) x the stop distance, i.e. risk 1 : reward 1.4. From the centre the stop distance is D/2, so the target is
     the centre + 0.7 D (= PDL + 1.2 D, above yesterday's High).
  4. If price never falls to the centre, or has fallen to PDL or below when the order starts, there is no trade. The resting order is cancelled from LAST_ENTRY (18:00).
     ONE trade a day, win or lose. An open trade keeps its stop and target until the runner's session square-off (MCX 23:20).

Timing: the opening price is the session's REAL open (`engine.day_open`; a run started mid-session reads it from the day's first candle). A run that only starts later than
GRACE_MINUTES after the end of the first candle does not have the resting order from the start, so it skips the day.

Quantity: nobody enters one. The strategy is given the most a trade may lose, MAX_LOSS_RS (Rs 10,000), and works the quantity out (see sizing.py):
units = floor(max loss / (stop distance + exit slippage + round-trip cost) / lot size) x lot size. If even one lot would lose more, the day is skipped.
(Entries pay the same simulated slippage as every strategy, although a real resting buy order would not.)
Backtests: docs/prev-day-zone30-study.md. (There is no TradingView version of this strategy.)
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from ..sizing import lot_size, units_for_max_loss
from ..strategy import Event, StrategyEngine, StrategySpec, register

RULES = {
    "zone_pct": 30.0,            # the day must open in the top this-many % of yesterday's range
    "skip_minutes": 5,           # no order in the first candle of the day (a 5-minute candle)
    "grace_minutes": 30,         # skip the day if the run only starts later than this after the first candle
    "last_entry": time(18, 0),   # the resting buy order is cancelled from this time (None = no cut-off)
    "target_x_stop": 1.4,        # the target is this many times the stop distance, from the entry price
    "max_loss_rs": 10000,        # the most one trade may lose at its stop, in rupees; the quantity follows from it (None = the run's fixed quantity)
}


class CenterEngine(StrategyEngine):
    def __init__(self, high: float, low: float, params, approve=None):
        super().__init__(high, low, params, approve)
        self.r = dict(RULES)                                   # read when the day starts, so a test can change RULES
        self.d = high - low
        self.upper = high - self.r["zone_pct"] / 100 * self.d  # the top zone is [upper, high]
        self.centre = (high + low) / 2
        self.decided = False
        self.planned = False                                   # the day opened in the top zone: a buy at the centre is planned
        self.done = False                                      # today's one decision has been taken (entered, or no trade)
        self.first_ts: datetime | None = None
        self.late: bool | None = None

    # --- the rules ---------------------------------------------------------------------------------
    def units(self, entry: float) -> int:
        return units_for_max_loss(self.r["max_loss_rs"], entry, self.low, self.p.slippage_pct, self.p.cost_pct, lot_size(self.symbol))

    def qty_for(self, side: str, entry: float, stop: float | None) -> float:
        return self.units(entry) if self.r["max_loss_rs"] else self.p.qty

    def _open_text(self) -> str:
        o = self.day_open
        if self.planned:
            return f"the day opened at {o:,.2f}, inside the top zone {self.upper:,.2f} to {self.high:,.2f}"
        where = ("above the previous High" if o > self.high else "below the previous Low" if o < self.low else
                 "in the bottom zone, which this strategy does not trade" if o <= self.low + self.r["zone_pct"] / 100 * self.d else "between the zones")
        return f"the day opened at {o:,.2f}, {where}"

    def evaluate(self, ts: datetime, price: float) -> list[Event]:
        out: list[Event] = []
        if self.first_ts is None:
            self.first_ts = ts
        if self.day_open is None:
            self.day_open = price
        if not self.decided:                                   # the opening price is evaluated once a day
            self.decided = True
            self.planned = self.upper <= self.day_open <= self.high
            out.append(Event("note", ts, "BUY", price, None, self._open_text() + (f": a BUY is planned at the centre {self.centre:,.2f} of yesterday's range."
                                                                              if self.planned else ": no trade today.")))
            if not self.planned:
                self.done = True
        if self.done:
            return out
        start = datetime.combine(ts.date(), self.session_open) if self.session_open else self.first_ts
        go = start + timedelta(minutes=self.r["skip_minutes"])
        if ts < go:
            return out                                         # still inside the first candle: no order yet
        if self.late is None:                                  # was this run watching from the start of the day?
            self.late = self.first_ts > go + timedelta(minutes=self.r["grace_minutes"])
            if self.late:
                self.done = True
                out.append(Event("note", ts, "BUY", price, None, f"BUY NOT taken today: too late, the first price seen was {self.first_ts:%H:%M:%S} and the order starts at {go:%H:%M}."))
                return out
        cut = self.r["last_entry"]
        if cut is not None and ts.time() >= cut:
            self.done = True
            out.append(Event("note", ts, "BUY", price, None, f"BUY NOT taken today: price never came down to the centre {self.centre:,.2f} before {cut:%H:%M}."))
            return out
        if price > self.centre:
            return out                                         # the resting buy order at the centre has not been reached yet
        est = price * (1 + self.p.slippage_pct / 100)
        why = None
        if est <= self.low:
            why = f"price {price:,.2f} is already at or below yesterday's Low {self.low:,.2f}, so there is no stop distance"
        elif self.r["max_loss_rs"] and self.units(est) <= 0:
            why = f"even one lot ({lot_size(self.symbol)} units) would lose more than Rs {self.r['max_loss_rs']:,.0f} at the stop ({est - self.low:,.2f} away)"
        self.done = True                                       # one decision: the entry below, or a skip
        if why:
            out.append(Event("note", ts, "BUY", price, None, f"BUY NOT taken today: {why}."))
            return out
        out.extend(self.signal("BUY", ts, price, self.centre, "price came down to the centre of yesterday's range",
                               lambda entry: (self.low, entry + self.r["target_x_stop"] * (entry - self.low))))
        return out

    # --- dashboard ---------------------------------------------------------------------------------
    def levels(self) -> list[dict]:
        return [{"name": "prev High", "value": self.high, "color": "blue"}, {"name": "top zone from", "value": self.upper, "color": "green"},
                {"name": "buy at centre", "value": self.centre, "color": "green"}, {"name": "prev Low (stop)", "value": self.low, "color": "orange"}]

    def describe_event(self, ev: Event):
        return ev.reason

    def status_text(self, price):
        if not self.decided:
            return "Waiting for the first price to read the day's open."
        base = self._open_text() + "."
        if self.position is not None:
            return base
        if not self.planned:
            return base + " No trade today."
        if self.done:
            return base + (" The trade for today has been taken." if self.trades_by_side["BUY"] else " Today's buy was not taken.")
        return base + f" A BUY order rests at the centre {self.centre:,.2f} (stop {self.low:,.2f}, target {self.r['target_x_stop']:g} x the stop distance above the entry)."


# ---- the calculation panel of the Dry runs page, and the run-log line -----------------------------------------------------------
def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def preview(high: float, low: float, options: dict) -> dict:
    r = RULES
    d = high - low
    upper, centre = high - r["zone_pct"] / 100 * d, (high + low) / 2
    x = r["target_x_stop"]
    risk = centre - low
    cut = r["last_entry"].strftime("%H:%M") if r["last_entry"] else "no cut-off"
    sizing = (f" Quantity is worked out so that a stop-out loses at most Rs {r['max_loss_rs']:,.0f} (stop distance + slippage + costs, whole lots); a day on which even one lot would lose more is skipped."
              if r["max_loss_rs"] else " Quantity is the run's fixed quantity.")
    return {"summary": (f"D = High - Low = {_fmt(high)} - {_fmt(low)} = <b>{_fmt(d)}</b> · top zone {_fmt(upper)} to {_fmt(high)} · centre = <b>{_fmt(centre)}</b> · "
                        f"stop {_fmt(low)} · target = {x:g} × stop distance"),
            "notes": [f"Only a day that OPENS in the top zone is traded, and only as a BUY. A buy order rests at the centre of yesterday's range from the start of the second candle; "
                      f"it is cancelled from {cut}. One trade a day, win or lose. The numbers are fixed in the strategy file." + sizing,
                      "Open in the bottom zone, between the zones, or beyond yesterday's High/Low: no trade."],
            "cards": [{"title": "BUY at the centre", "rows": [
                ["Day must open between", f"<b>{_fmt(upper)}</b> and <b>{_fmt(high)}</b>"], ["Entry", f"<b>{_fmt(centre)}</b> <span class='muted'>= (High + Low) / 2, when price comes down to it</span>"],
                ["Stop", f"<b>{_fmt(low)}</b> <span class='muted'>= previous Low ({_fmt(risk)} below the entry)</span>"],
                ["Target", f"<b>{_fmt(centre + x * risk)}</b> <span class='muted'>= entry + {x:g} × {_fmt(risk)} (reward {_fmt(x * risk)}, risk 1 : reward {x:g})</span>"]]}]}


def describe(high: float, low: float, options: dict) -> str:
    r = RULES
    d = high - low
    centre = (high + low) / 2
    cut = r["last_entry"].strftime("%H:%M") if r["last_entry"] else "never"
    return (f"30% opening, buy at the centre: BUY only if the day opens in {high - r['zone_pct'] / 100 * d:,.2f} to {high:,.2f}; buy at the centre {centre:,.2f}, stop {low:,.2f}, "
            f"target {centre + r['target_x_stop'] * (centre - low):,.2f} ({r['target_x_stop']:g} x the stop distance); order from {r['skip_minutes']} min after the open, cancelled from {cut}, "
            f"one trade a day; " + (f"quantity from a Rs {r['max_loss_rs']:,.0f} maximum loss per trade." if r["max_loss_rs"] else "fixed quantity."))


GUIDE = [
    ("Idea", ["The same reading of the day as the 30% opening zones, but BUY only, and the buy is placed at the centre of yesterday's range instead of at the open."]),
    ("Which days", ["The day must OPEN in the top 30% of yesterday's range (previous High - 0.3 D to previous High). Any other open means no trade."]),
    ("Entry", ["From the start of the second candle a BUY order rests at the centre, (High + Low) / 2. It fills when price falls to the centre, or at once if price is already at or below it.",
               "If price never reaches the centre, or has fallen to the previous Low or below when the order starts, there is no trade. The resting order is cancelled from 18:00."]),
    ("Stop and target", ["The stop is yesterday's Low, exactly.", "The target is entry + 1.4 times the stop distance (risk 1 : reward 1.4). From the centre that is the centre + 0.7 D."]),
    ("Limits", ["One trade a day, win or lose. An open trade keeps its stop and target until the session square-off (MCX 23:20)."]),
    ("Quantity", ["You give only the maximum loss per trade (Rs 10,000); the strategy works out whole lots so that a stop-out loses at most that. A day on which even one lot would lose more is skipped."]),
]

SPEC = register(StrategySpec(
    name="prev_day_zone30_buy_center", label="30% opening buy at center", engine=CenterEngine, fields=[], preview=preview, describe=describe, guide=GUIDE,
    blurb="Only a day that opens in the top 30% of yesterday's range is traded, and only as a BUY, placed at the centre of yesterday's range (the middle of the previous High and Low) "
          "when price comes down to it. The stop is the previous Low and the target is 1.4 times the stop distance above the entry (risk 1 : reward 1.4). "
          "The quantity is worked out so that a stop-out loses at most Rs 10,000. One trade a day."))
