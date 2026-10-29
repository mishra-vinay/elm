"""Range thirds. The whole strategy is in this file: the numbers are in RULES, the logic is in RangeThirdsEngine. Nothing is configured on a page.

Yesterday's range is D = previous High - previous Low, and a "third" is D/3. The "0.8" is 0.8% of price (of the previous High for a BUY, of the previous Low for a SELL).

  BUY  (High side)   entry  = previous High - (third - 0.8)          the entry sits a little below yesterday's High
                     stop   = entry - 0.8                           exactly 0.8 below the entry
                     target = entry + third + (third - 0.8)         [= previous High + third]
  SELL (Low side)    entry  = previous Low + (third - 0.8)
                     stop   = entry + 0.8
                     target = entry - third - (third - 0.8)         [= previous Low - third]

All prices are fixed from yesterday's High and Low (they are not re-based on the actual fill). A side is skipped for the day when its target distance
(third + (third - 0.8)) is not positive, i.e. yesterday's range was tiny.

Entry ("approach"): a side is ARMED while price is on the inside of its entry price (BUY: below it, SELL: above it); when price then reaches the entry price
the trade is taken, before the level breaks.

Around it:
  * no entry in the first SKIP_MINUTES of the session (the first 5-minute candle), none from LAST_ENTRY; a side that is armed then is cancelled
  * ONE trade a day, BUY and SELL together: counted when the entry fills, not given back when the trade closes; once one side trades the other is cancelled
  * day-open filter: the day must OPEN inside yesterday's range (between the previous Low and High, both included); otherwise there is NO trade that day, on either side
  * an open trade keeps its stop and target until the session square-off (done by the runner: MCX 23:20, NSE 15:15)
  * quantity: nobody enters one. The strategy is given the most a trade may lose, MAX_LOSS_RS (Rs 10,000), and works the quantity out for every trade
    (see sizing.py): whole lots such that a stop-out, with slippage and costs, loses at most that. A day on which even one lot would lose more is skipped.

The earlier D/6 rule and the other options live in `range_thirds_research.py` (hidden), with `range_thirds_backtest.py` and `scripts/verify_backtest.py`.
Backtests: docs/range-thirds-study.md. TradingView version: tradingview/range_thirds_strategy.pine.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from types import SimpleNamespace

from ..sizing import lot_size, units_for_max_loss
from ..strategy import Event, StrategyEngine, StrategySpec, register

RULES = {
    "dev_pct": 0.8,              # the "0.8": % of the previous High (BUY) / previous Low (SELL)
    "third_frac": 1 / 3,         # the "D/3"
    "skip_minutes": 5,           # no entry in the first candle of the day (a 5-minute candle)
    "last_entry": time(18, 0),   # no new entry from this time (None = no cut-off)
    "trades_per_day": 1,         # BUY and SELL together
    "day_open_filter": True,     # no trade at all unless the day opens between the previous Low and High
    "max_loss_rs": 10000,        # the most one trade may lose at its stop, in rupees; the quantity follows from it (None = the run's fixed quantity)
}
SIDES = ("BUY", "SELL")


def geometry(high: float, low: float, r: dict) -> SimpleNamespace:
    """The prices of the rule for yesterday's High and Low (the formulas in the header, in code)."""
    third = r["third_frac"] * (high - low)
    dev = {"BUY": r["dev_pct"] / 100 * high, "SELL": r["dev_pct"] / 100 * low}            # the 0.8 as a price distance
    entry = {"BUY": high - (third - dev["BUY"]), "SELL": low + (third - dev["SELL"])}
    stop = {"BUY": entry["BUY"] - dev["BUY"], "SELL": entry["SELL"] + dev["SELL"]}
    reach = {s: third + (third - dev[s]) for s in SIDES}                                    # entry to target
    target = {"BUY": entry["BUY"] + reach["BUY"], "SELL": entry["SELL"] - reach["SELL"]}
    return SimpleNamespace(third=third, dev=dev, entry=entry, stop=stop, target=target, valid={s: reach[s] > 0 for s in SIDES})


class RangeThirdsEngine(StrategyEngine):
    def __init__(self, high: float, low: float, params, approve=None):
        super().__init__(high, low, params, approve)
        self.r = dict(RULES)                                   # read when the day starts, so a test can change RULES
        g = geometry(high, low, self.r)
        self.third, self.dev, self.entry, self.stop, self.target, self.valid = g.third, g.dev, g.entry, g.stop, g.target, g.valid
        self.armed = {s: False for s in SIDES}
        self.first_ts: datetime | None = None
        self._noted: set[str] = set()
        self.flags = {"BUY armed": False, "SELL armed": False}

    # --- the rules ---------------------------------------------------------------------------------
    def blocked(self, side: str) -> bool:
        """Day-open filter: the day must open inside yesterday's range (Low <= open <= High); otherwise both sides are off for the whole day."""
        if not self.r["day_open_filter"] or self.day_open is None:
            return False
        return not (self.low <= self.day_open <= self.high)

    def _outside_text(self) -> str:
        o = self.day_open
        return f"the day opened at {o:,.2f}, {'above the previous High ' + format(self.high, ',.2f') if o > self.high else 'below the previous Low ' + format(self.low, ',.2f')}, outside yesterday's range"

    def entry_open(self, ts: datetime) -> bool:
        """Inside the entry window: past the first candle, before the cut-off, and today's trade not yet taken."""
        start = datetime.combine(ts.date(), self.session_open) if self.session_open else (self.first_ts or ts)
        if ts < start + timedelta(minutes=self.r["skip_minutes"]):
            return False
        if self.r["last_entry"] is not None and ts.time() >= self.r["last_entry"]:
            return False
        return sum(self.trades_by_side.values()) < self.r["trades_per_day"]

    def units(self, side: str, entry: float) -> int:
        """Quantity from the maximum loss (0 = even one lot would lose more than that)."""
        return units_for_max_loss(self.r["max_loss_rs"], entry, self.stop[side], self.p.slippage_pct, self.p.cost_pct, lot_size(self.symbol))

    def qty_for(self, side: str, entry: float, stop: float | None) -> float:
        return self.units(side, entry) if self.r["max_loss_rs"] else self.p.qty

    def evaluate(self, ts: datetime, price: float) -> list[Event]:
        out: list[Event] = []
        if self.first_ts is None:
            self.first_ts = ts
        if self.day_open is None:
            self.day_open = price
        if self.blocked("BUY") and not self._noted:                # one note for the day: both sides are off
            self._noted.add("day")
            out.append(Event("note", ts, "BUY", price, None, f"No trade today: {self._outside_text()}"))
        for side in SIDES:
            out.extend(self._side(side, ts, price))
        self.flags = {"BUY armed": self.armed["BUY"], "SELL armed": self.armed["SELL"]}
        return out

    def _side(self, side: str, ts: datetime, price: float) -> list[Event]:
        up = side == "BUY"
        level = self.high if up else self.low
        if not self.entry_open(ts):                            # first candle, after the cut-off, or today's trade is used: nothing stays armed
            self.armed[side] = False
            return []
        if not self.armed[side]:
            if self.blocked(side) or not self.valid[side]:
                return []
            if (price < self.entry[side]) if up else (price > self.entry[side]):       # on the inside of the entry price: armed
                self.armed[side] = True
                return [Event("armed", ts, side, price, level, f"price is on the inside of the {side} entry price {self.entry[side]:,.2f}")]
            return []
        if (price >= self.entry[side]) if up else (price <= self.entry[side]):         # price reached the entry price: take the trade
            self.armed[side] = False
            if self.r["max_loss_rs"]:
                est = price * (1 + self.p.slippage_pct / 100) if up else price * (1 - self.p.slippage_pct / 100)
                if self.units(side, est) <= 0:
                    return [Event("note", ts, side, price, level, f"{side} NOT taken: even one lot ({lot_size(self.symbol)} units) would lose more than Rs {self.r['max_loss_rs']:,.0f} at the stop "
                                                                  f"({abs(est - self.stop[side]):,.2f} away)")]
            return self.signal(side, ts, price, level, f"price reached the {side} entry price",
                               lambda entry: (self.stop[side], self.target[side]))     # fixed stop and target
        return []

    # --- dashboard ---------------------------------------------------------------------------------
    def levels(self) -> list[dict]:
        return [{"name": "prev High", "value": self.high, "color": "blue"}, {"name": "buy trigger", "value": self.entry["BUY"], "color": "green"},
                {"name": "sell trigger", "value": self.entry["SELL"], "color": "red"}, {"name": "prev Low", "value": self.low, "color": "orange"},
                {"name": "buy stop", "value": self.stop["BUY"], "color": "muted"}, {"name": "sell stop", "value": self.stop["SELL"], "color": "muted"},
                {"name": "buy target", "value": self.target["BUY"], "color": "green"}, {"name": "sell target", "value": self.target["SELL"], "color": "red"}]

    def describe_event(self, ev: Event):
        if ev.kind == "armed":
            return f"{ev.side} armed: waiting for price to {'rise' if ev.side == 'BUY' else 'fall'} to {self.entry[ev.side]:,.2f}."
        return ev.reason

    def status_text(self, price):
        off = f"No trade today: {self._outside_text()}." if self.blocked("BUY") else ""
        waits = [f"{s} when price {'rises' if s == 'BUY' else 'falls'} to {self.entry[s]:,.2f} (stop {self.stop[s]:,.2f}, target {self.target[s]:,.2f})" for s in SIDES if self.armed[s]]
        if waits:
            main = "Waiting: " + "; ".join(waits) + "."
        elif sum(self.trades_by_side.values()) >= self.r["trades_per_day"]:
            main = "Today's trade has been entered; no further entry."
        else:
            main = "No side is armed (the first candle or the entry cut-off applies, price is already beyond both entry prices, or a side is off)."
        return (main + " " + off).strip()


# ---- the calculation panel of the Dry runs page, and the run-log line -----------------------------------------------------------
def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def preview(high: float, low: float, options: dict) -> dict:
    g = geometry(high, low, RULES)

    def card(title, side):
        up = side == "BUY"
        ent, stop, tgt, dev = g.entry[side], g.stop[side], g.target[side], g.dev[side]
        rows = [["Arms when", f"price is {'below' if up else 'above'} {_fmt(ent)} (inside the range)"],
                ["Entry price", f"<b>{_fmt(ent)}</b> <span class='muted'>= {'High' if up else 'Low'} {'-' if up else '+'} (D/3 - 0.8) = {_fmt(high if up else low)} {'-' if up else '+'} {_fmt(g.third - dev)}</span>"],
                ["Stop", f"<b>{_fmt(stop)}</b> <span class='muted'>= entry {'-' if up else '+'} {_fmt(dev)} (risk {dev / ent * 100:.2f}%)</span>"]]
        if g.valid[side]:
            rows.append(["Target", f"<b>{_fmt(tgt)}</b> <span class='muted'>= entry {'+' if up else '-'} {_fmt(abs(tgt - ent))} (reward {abs(tgt - ent) / ent * 100:.2f}%, {abs(tgt - ent) / dev:.2f} : 1)</span>"])
        else:
            rows.append(["Target", "<span class='warnc'>this side is skipped: yesterday's range is too small</span>"])
        return {"title": title, "rows": rows}

    notes = []
    if g.entry["BUY"] > high or g.entry["SELL"] < low:
        notes.append("<span class='warnc'><b>Note:</b> D/3 is smaller than the 0.8, so an entry sits beyond yesterday's High/Low (a breakout entry) and the reward is smaller than the risk.</span>")
    cut = RULES["last_entry"].strftime("%H:%M") if RULES["last_entry"] else "no cut-off"
    notes.append(f"No entry in the first {RULES['skip_minutes']} minutes of the session, none from {cut}; {RULES['trades_per_day']} trade a day (BUY and SELL together); "
                 "no trade at all if the day opens outside yesterday's range (below the Low or above the High). The numbers are fixed in the strategy file.")
    if RULES["max_loss_rs"]:
        notes.append(f"Quantity is worked out for every trade so that a stop-out loses at most Rs {RULES['max_loss_rs']:,.0f} (stop distance + slippage + costs, whole lots); "
                     "a day on which even one lot would lose more is skipped.")
    return {"summary": f"D = High - Low = {_fmt(high)} - {_fmt(low)} = <b>{_fmt(high - low)}</b> · D/3 = <b>{_fmt(g.third)}</b> · the 0.8 = 0.8% = <b>{_fmt(g.dev['BUY'])}</b> (of the High) / <b>{_fmt(g.dev['SELL'])}</b> (of the Low)",
            "notes": notes, "cards": [card("BUY (High side)", "BUY"), card("SELL (Low side)", "SELL")]}


def describe(high: float, low: float, options: dict) -> str:
    g = geometry(high, low, RULES)
    cut = RULES["last_entry"].strftime("%H:%M") if RULES["last_entry"] else "never"
    return (f"Range thirds: D = {high - low:,.2f}, D/3 = {g.third:,.2f}, the 0.8 = {g.dev['BUY']:,.2f}/{g.dev['SELL']:,.2f}: "
            f"BUY entry {g.entry['BUY']:,.2f}, stop {g.stop['BUY']:,.2f}, target {g.target['BUY']:,.2f}; SELL entry {g.entry['SELL']:,.2f}, stop {g.stop['SELL']:,.2f}, "
            f"target {g.target['SELL']:,.2f}. No entry in the first {RULES['skip_minutes']} min, none from {cut}, {RULES['trades_per_day']} trade a day, "
            f"day-open filter {'on' if RULES['day_open_filter'] else 'off'}; "
            + (f"quantity from a Rs {RULES['max_loss_rs']:,.0f} maximum loss per trade." if RULES["max_loss_rs"] else "fixed quantity."))


GUIDE = [
    ("Idea", ["Buys when price rises to just under yesterday's High and sells when it falls to just above yesterday's Low. D = yesterday's High - Low, a third = D / 3, and 0.8 means 0.8% of price."]),
    ("Prices", ["BUY entry = previous High - (third - 0.8); stop = entry - 0.8; target = entry + third + (third - 0.8) (= previous High + third).",
                "SELL entry = previous Low + (third - 0.8); stop = entry + 0.8; target = entry - third - (third - 0.8) (= previous Low - third).",
                "All prices are fixed from yesterday's High and Low; they are not re-based on the actual fill."]),
    ("Entry", ["A side is armed while price is on the inside of its entry price (BUY below it, SELL above it); when price reaches the entry price the trade is taken."]),
    ("Day-open filter", ["The day must open between yesterday's Low and High. Otherwise there is no trade that day, on either side."]),
    ("Limits", ["No entry in the first 5 minutes of the session and none from 18:00. One trade a day, BUY and SELL together.", "An open trade keeps its stop and target until the session square-off (MCX 23:20, NSE 15:15)."]),
    ("Quantity", ["You give only the maximum loss per trade (Rs 10,000); the strategy works out whole lots so that a stop-out, with slippage and costs, loses at most that. A day on which even one lot would lose more is skipped."]),
]

SPEC = register(StrategySpec(
    name="range_thirds", label="Range thirds", engine=RangeThirdsEngine, fields=[], preview=preview, describe=describe, guide=GUIDE,
    blurb="Buys when price rises to just under yesterday's High and sells when it falls to just above yesterday's Low (the entry is D/3 minus 0.8% in from the level, where D is yesterday's range). "
          "The stop is 0.8% from the entry and the target is about D/3 beyond the level. One trade a day; no entry in the first 5 minutes or from 18:00; "
          "no trade at all on a day that opens outside yesterday's range (below the Low or above the High). The quantity is worked out so that a stop-out loses at most Rs 10,000."))
