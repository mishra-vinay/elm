"""Previous-day High/Low 30% opening zones. The whole strategy is in this file: the numbers are in RULES, the logic is in ZoneEngine. Nothing is configured on a page.

PDH, PDL = yesterday's High and Low, D = PDH - PDL, zone = 30% of D. The day's OPENING price chooses the side, once a day:

  opens in the top 30%     [PDH - 0.3 D, PDH]   -> BUY    stop = centre    target = entry + 1.3 x (entry - centre)
  opens in the bottom 30%  [PDL, PDL + 0.3 D]   -> SELL   stop = centre    target = entry - 1.3 x (centre - entry)
  opens anywhere else (between the zones, or a gap beyond PDH/PDL) -> no trade that day

Timing:
  * The side is decided from the session's REAL opening price (`engine.day_open`; a run started mid-session reads it from the day's first candle), never
    from a later candle.
  * "No entry in the first candle": the order cannot be at the opening price itself. The trade is a market entry at the first price on or after SKIP_MINUTES
    after the session open, i.e. at the open of the second candle (= the close of the first). A run that only starts later than GRACE_MINUTES after that
    moment skips the day instead of entering late.
  * No entry from LAST_ENTRY. ONE trade a day, win or lose. An open trade keeps its stop and target until the runner's session square-off (MCX 23:20).

Stop and target: the stop is the MIDDLE price of yesterday's High and Low, the centre = (PDH + PDL) / 2. A BUY (top zone) is entered above the centre and a SELL (bottom zone)
below it, so the centre is always on the losing side. The target is TARGET_X_STOP (1.3) times the stop distance (entry to centre), from the actual entry price (reward : risk = 1.3 : 1).
If the price has already crossed the centre when the entry moment comes, there is no stop distance and the day is skipped.

Quantity: nobody enters one. The strategy is given the most a trade may lose, MAX_LOSS_RS (Rs 10,000), and works the quantity out for every trade
(see sizing.py): units = floor(max loss / (stop distance + exit slippage + round-trip cost) / lot size) x lot size. A day on which even one lot would lose more than
that is skipped.
Backtests: docs/prev-day-zone30-study.md. TradingView version: tradingview/opening_zone30_strategy.pine.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from ..sizing import lot_size, units_for_max_loss
from ..strategy import Event, StrategyEngine, StrategySpec, register

RULES = {
    "zone_pct": 30.0,            # each zone is this % of yesterday's range
    "skip_minutes": 5,           # no entry in the first candle of the day (a 5-minute candle)
    "grace_minutes": 30,         # skip the day if the first price only arrives later than this after the entry moment
    "last_entry": time(18, 0),   # no new entry from this time (None = no cut-off)
    "target_x_stop": 1.3,        # the target is this many times the (new) stop distance, from the entry price
    "max_loss_rs": 10000,        # the most one trade may lose at its stop, in rupees; the quantity follows from it (None = the run's fixed quantity)
}


class ZoneEngine(StrategyEngine):
    def __init__(self, high: float, low: float, params, approve=None):
        super().__init__(high, low, params, approve)
        self.r = dict(RULES)                                   # read when the day starts, so a test can change RULES
        self.d = high - low
        z = self.r["zone_pct"] / 100 * self.d
        self.upper, self.lower = high - z, low + z             # BUY zone [upper, high], SELL zone [low, lower]
        self.decided = False
        self.side: str | None = None                           # BUY | SELL | None (no trade today)
        self.done = False                                      # today's one decision about entering has been taken
        self.first_ts: datetime | None = None

    # --- the rules ---------------------------------------------------------------------------------
    def zone_of(self, price: float) -> str | None:
        if self.upper <= price <= self.high:
            return "BUY"
        if self.low <= price <= self.lower:
            return "SELL"
        return None

    @property
    def centre(self) -> float:
        return (self.high + self.low) / 2

    def stop_for(self, side: str, entry: float) -> float:
        """The middle price of yesterday's High and Low (below a BUY entry, above a SELL entry)."""
        return self.centre

    def target_for(self, side: str, entry: float) -> float:
        reach = self.r["target_x_stop"] * abs(entry - self.stop_for(side, entry))
        return entry + reach if side == "BUY" else entry - reach

    def units(self, side: str, entry: float) -> int:
        """Quantity from the maximum loss (0 = even one lot would lose more than that)."""
        return units_for_max_loss(self.r["max_loss_rs"], entry, self.stop_for(side, entry), self.p.slippage_pct, self.p.cost_pct, lot_size(self.symbol))

    def qty_for(self, side: str, entry: float, stop: float | None) -> float:
        return self.units(side, entry) if self.r["max_loss_rs"] else self.p.qty

    def _open_text(self) -> str:
        o = self.day_open
        if self.side == "BUY":
            return f"the day opened at {o:,.2f}, inside the BUY zone {self.upper:,.2f} to {self.high:,.2f}"
        if self.side == "SELL":
            return f"the day opened at {o:,.2f}, inside the SELL zone {self.low:,.2f} to {self.lower:,.2f}"
        where = "above the previous High" if o > self.high else "below the previous Low" if o < self.low else "between the two zones"
        return f"the day opened at {o:,.2f}, {where}: outside both zones"

    def evaluate(self, ts: datetime, price: float) -> list[Event]:
        out: list[Event] = []
        if self.first_ts is None:
            self.first_ts = ts
        if self.day_open is None:
            self.day_open = price
        if not self.decided:                                   # the opening price is evaluated once a day
            self.decided = True
            self.side = self.zone_of(self.day_open)
            out.append(Event("note", ts, self.side or "BUY", price, None,
                             self._open_text() + (f": a {self.side} is planned for after the first {self.r['skip_minutes']} minutes." if self.side else ": no trade today.")))
            if self.side is None:
                self.done = True
        if self.done:
            return out
        start = datetime.combine(ts.date(), self.session_open) if self.session_open else self.first_ts
        go = start + timedelta(minutes=self.r["skip_minutes"])
        if ts < go:
            return out                                         # still inside the first candle: no entry
        side, cut, why = self.side, self.r["last_entry"], None
        if ts > go + timedelta(minutes=self.r["grace_minutes"]):
            why = f"too late: the entry moment was {go:%H:%M} and the first price seen was {ts:%H:%M:%S}"
        elif cut is not None and ts.time() >= cut:
            why = f"no new entries from {cut:%H:%M}"
        else:
            slip = price * self.p.slippage_pct / 100
            est = price + slip if side == "BUY" else price - slip
            if (side == "BUY" and est <= self.centre) or (side == "SELL" and est >= self.centre):
                why = f"the price {price:,.2f} has already crossed the centre {self.centre:,.2f} of yesterday's range, so there is no stop distance"
            elif self.r["max_loss_rs"] and self.units(side, est) <= 0:
                why = (f"even one lot ({lot_size(self.symbol)} units) would lose more than Rs {self.r['max_loss_rs']:,.0f} at the stop "
                       f"(stop {abs(est - self.stop_for(side, est)):,.2f} away, entry {est:,.2f})")
        self.done = True                                       # one decision: either the entry below, or a skip
        if why:
            out.append(Event("note", ts, side, price, None, f"{side} NOT taken today: {why}."))
            return out
        out.extend(self.signal(side, ts, price, self.high if side == "BUY" else self.low, f"first price {self.r['skip_minutes']} minutes after the open",
                               lambda entry: (self.stop_for(side, entry), self.target_for(side, entry))))
        return out

    # --- dashboard ---------------------------------------------------------------------------------
    def levels(self) -> list[dict]:
        return [{"name": "prev High", "value": self.high, "color": "blue"}, {"name": "BUY zone from", "value": self.upper, "color": "green"},
                {"name": "SELL zone to", "value": self.lower, "color": "red"}, {"name": "centre (stop)", "value": self.centre, "color": "muted"}, {"name": "prev Low", "value": self.low, "color": "orange"}]

    def describe_event(self, ev: Event):
        return ev.reason

    def status_text(self, price):
        if not self.decided:
            return "Waiting for the first price to read the day's open."
        base = self._open_text() + "."
        if self.position is not None:
            return base
        if self.side is None:
            return base + " No trade today."
        if self.done:
            return base + (" The trade for today has been taken." if self.trades_by_side[self.side] else " Today's entry was not taken.")
        return base + f" The {self.side} is entered at the first price {self.r['skip_minutes']} minutes after the open (stop at the centre {self.centre:,.2f} of yesterday's range; quantity from the Rs {self.r['max_loss_rs'] or 0:,.0f} maximum loss)."


# ---- the calculation panel of the Dry runs page, and the run-log line -----------------------------------------------------------
def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def preview(high: float, low: float, options: dict) -> dict:
    r = RULES
    d = high - low
    z = r["zone_pct"] / 100 * d
    upper, lower = high - z, low + z
    x = r["target_x_stop"]
    centre = (high + low) / 2
    mid_b, mid_s = (upper + high) / 2, (low + lower) / 2
    risk_b, risk_s = mid_b - centre, centre - mid_s                 # stop distance of an entry in the middle of each zone
    cut = r["last_entry"].strftime("%H:%M") if r["last_entry"] else "no cut-off"
    sizing = (f"Quantity is worked out for every trade so that a stop-out loses at most Rs {r['max_loss_rs']:,.0f} (stop distance + slippage + costs, whole lots); "
              "a day on which even one lot would lose more is skipped." if r["max_loss_rs"] else "Quantity is the run's fixed quantity.")
    notes = ["The side is chosen once a day from the session's real opening price. An open outside both zones, or a gap beyond the previous High/Low, means no trade that day.",
             f"Entry is a market order at the first price {r['skip_minutes']} minutes after the open (the open of the second candle), not at the opening price itself. "
             f"One trade a day, win or lose; no entry from {cut}. The numbers are fixed in the strategy file.",
             f"The stop is the middle price of yesterday's High and Low ({_fmt(centre)}); the target is {x:g} times the stop distance (entry to the centre), from the actual entry price. " + sizing]
    return {"summary": (f"D = High - Low = {_fmt(high)} - {_fmt(low)} = <b>{_fmt(d)}</b> · zones are {r['zone_pct']:g}% of D = <b>{_fmt(z)}</b> · "
                        f"BUY zone {_fmt(upper)} to {_fmt(high)} · SELL zone {_fmt(low)} to {_fmt(lower)} · stop = centre {_fmt(centre)} · target = {x:g} × stop distance"),
            "notes": notes, "cards": [
                {"title": "BUY (open in the upper zone)", "rows": [
                    ["Opens between", f"<b>{_fmt(upper)}</b> and <b>{_fmt(high)}</b>"], ["Stop", f"<b>{_fmt(centre)}</b> <span class='muted'>= middle of the previous High and Low</span>"],
                    ["Target", f"<b>entry + {x:g} × stop distance</b>"], ["Example (open in the middle)", f"entry ~{_fmt(mid_b)}: stop {_fmt(mid_b - risk_b)}, target {_fmt(mid_b + x * risk_b)} (risk {_fmt(risk_b)}, reward {_fmt(x * risk_b)})"]]},
                {"title": "SELL (open in the lower zone)", "rows": [
                    ["Opens between", f"<b>{_fmt(low)}</b> and <b>{_fmt(lower)}</b>"], ["Stop", f"<b>{_fmt(centre)}</b> <span class='muted'>= middle of the previous High and Low</span>"],
                    ["Target", f"<b>entry - {x:g} × stop distance</b>"], ["Example (open in the middle)", f"entry ~{_fmt(mid_s)}: stop {_fmt(mid_s + risk_s)}, target {_fmt(mid_s - x * risk_s)} (risk {_fmt(risk_s)}, reward {_fmt(x * risk_s)})"]]}]}


def describe(high: float, low: float, options: dict) -> str:
    r = RULES
    z = r["zone_pct"] / 100 * (high - low)
    cut = r["last_entry"].strftime("%H:%M") if r["last_entry"] else "never"
    return (f"Previous-day {r['zone_pct']:g}% opening zones: BUY if the day opens in {high - z:,.2f} to {high:,.2f}, SELL if it opens in {low:,.2f} to {low + z:,.2f}; "
            f"stop at the centre {(high + low) / 2:,.2f} of yesterday's range, target {r['target_x_stop']:g} x the stop distance; "
            f"market entry {r['skip_minutes']} min after the open, one trade a day, no entry from {cut}; "
            + (f"quantity from a Rs {r['max_loss_rs']:,.0f} maximum loss per trade." if r["max_loss_rs"] else "fixed quantity."))


GUIDE = [
    ("Idea", ["The day's opening price chooses the side. D = yesterday's High - Low; each zone is 30% of D."]),
    ("Side", ["Opens in the top 30% (previous High - 0.3 D to previous High): BUY.", "Opens in the bottom 30% (previous Low to previous Low + 0.3 D): SELL.",
              "Opens anywhere else (between the zones, or beyond yesterday's High or Low): no trade that day."]),
    ("Entry", ["A market order at the first price 5 minutes after the open (the open of the second candle), not at the opening price itself.", "A run that starts more than 30 minutes after that moment skips the day. No entry from 18:00."]),
    ("Stop and target", ["The stop is the middle price of yesterday's High and Low, (High + Low) / 2: below a BUY, above a SELL.", "The target is 1.3 times the distance from the entry to the stop, on the winning side.",
                         "If price has already crossed the middle price when the entry moment comes, the day is skipped."]),
    ("Limits", ["One trade a day, win or lose. An open trade keeps its stop and target until the session square-off (MCX 23:20)."]),
    ("Quantity", ["You give only the maximum loss per trade (Rs 10,000); the strategy works out whole lots so that a stop-out loses at most that. A day on which even one lot would lose more is skipped."]),
]

SPEC = register(StrategySpec(
    name="prev_day_zone30", label="Previous-day 30% opening zones", engine=ZoneEngine, fields=[], preview=preview, describe=describe, guide=GUIDE,
    blurb="The day's opening price decides the side: an open in the top 30% of yesterday's range is a BUY, an open in the bottom 30% is a SELL, anything else is no trade. "
          "The trade is entered at the start of the second candle (not at the open itself). The stop is the middle price of yesterday's High and Low and the target is 1.3 times "
          "that stop distance. The quantity is worked out so that a stop-out loses at most Rs 10,000. One trade a day."))
