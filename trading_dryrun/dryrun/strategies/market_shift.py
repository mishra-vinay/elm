"""Market shift strategy (BUY side, 5-minute candles). The whole strategy is in this file: the numbers are in RULES, the logic is in MarketShiftEngine.

It does not use yesterday's High/Low. It watches how the day develops around the day's OPEN level:

  1. DAY OPEN LEVEL: the open of the first 5-minute candle of the session (the engine's `day_open`).
  2. MARKET SHIFT: a completed 5-minute candle closes ABOVE the day open level. That is the shift candle. (The first candle of the day can never be one:
     the stop search needs a candle before it.)
  3. CONFIRMATION SEQUENCE: the candles that follow, while each one is higher than the one before it (a higher high AND a higher low). There is no
     minimum and no maximum length. The first candle that is not higher ends the sequence.
  4. REFERENCE CANDLE: the last candle of the sequence (the highest one; the shift candle itself if no candle followed it).
       breakout level       = its high
       reference entry price = its close
  5. INVALIDATION: until the trade is taken, any completed candle that closes BELOW the day open level cancels the setup (a pending order too).
     A dip below the level without a candle closing below it changes nothing. The next candle that closes above the level starts a new, independent setup.
  6. BREAKOUT: price must trade above the reference candle's high (a candle that ends the sequence can already contain the breakout).
  7. ENTRY: after the breakout a BUY order waits at the reference entry price. If price comes back to it the trade is taken (at the price that comes,
     which is lower than the reference when price gaps down through it). If price reaches the target first, or the setup is invalidated, there is no trade.
  8. STOP: start at the candle before the shift candle and walk BACKWARDS while each earlier candle's low is not higher than the lowest low so far; stop at
     the first candle with a higher low. The stop is the lowest low found, also compared with the lows of the shift and sequence candles. It is a fixed price.
  9. TARGET: reference entry + (reference entry - stop), i.e. 1:1 from the reference price. When the fill is lower than the reference price the difference is
     ADDED to the target (a fill at 263.50 on a reference of 263.80 moves the target up by 0.30). The stop does not move.
 10. EXIT: stop (market), target (limit), or the runner's session square-off (MCX 23:20).

MULTIPLE SETUPS: any number a day, one position at a time. While a trade is open no new setup is started. The run's per-day trade-count limit is not
applied to this strategy (the daily loss limit and the risk gate still are).

Quantity: no rule in this strategy; the run's fixed quantity is used (a backtest shows the result per unit, so points).

Candles are built from the ticks the engine receives (5-minute buckets), so it needs 1-minute or 5-minute data to be backtested.
"""
from __future__ import annotations

from datetime import datetime

from ..strategy import Event, StrategyEngine, StrategySpec, register

RULES = {
    "candle_minutes": 5,        # the candle size the rules are read on
    "target_x_risk": 1.0,       # the target is this many times (reference entry - stop) above the reference entry price (1.0 = 1:1)
    "last_entry": None,         # a waiting order is cancelled from this time of day (None = no cut-off; the runner's session square-off still applies)
}


class MarketShiftEngine(StrategyEngine):
    """BUY side. `MarketShiftSellEngine` (market_shift_sell.py) is the same engine with SIDE = "SELL": every comparison below is written with the sign `sg`
    (+1 BUY, -1 SELL) so the SELL side is the exact mirror image."""
    SIDE = "BUY"
    rules = RULES                                              # the module's dict; a subclass points this at its own

    def __init__(self, high: float, low: float, params, approve=None):
        super().__init__(high, low, params, approve)
        self.side = self.SIDE
        self.sg = 1 if self.SIDE == "BUY" else -1
        self.r = dict(type(self).rules)                        # read when the day starts, so a test can change the rules
        self.cs: list[dict] = []                               # completed candles of the day {k, o, h, l, c}
        self.cur: dict | None = None                           # the candle being built
        self.st = "idle"                                       # idle | seq | armed | pending | trade
        self.S: dict | None = None                             # the setup in progress (kept after it ends, for the chart)
        self.setups = 0

    # --- candles -----------------------------------------------------------------------------------
    def _bucket(self, ts: datetime) -> datetime:
        m = self.r["candle_minutes"]
        return ts.replace(minute=ts.minute // m * m, second=0, microsecond=0)

    def far(self, c: dict) -> float:
        """The candle's extreme in the direction of the trade: the high for a BUY, the low for a SELL."""
        return c["h"] if self.sg > 0 else c["l"]

    def near(self, c: dict) -> float:
        """The candle's extreme on the stop side: the low for a BUY, the high for a SELL."""
        return c["l"] if self.sg > 0 else c["h"]

    def block_reason(self, side: str, ts: datetime, price: float) -> str | None:
        """Like the base class, but without the per-day trade-count limits: this strategy is meant to take several setups a day."""
        if self.position is not None:
            return "a position is already open"
        if self.p.daily_loss_limit_pct is not None and self.net_pct_sum <= -self.p.daily_loss_limit_pct:
            return "daily loss limit reached"
        if self.approve is not None:
            ok, why = self.approve(side, ts, price, self.state())
            if not ok:
                return why or "risk engine rejected"
        return None

    def evaluate(self, ts: datetime, price: float) -> list[Event]:
        out: list[Event] = []
        if self.day_open is None:
            self.day_open = price
        b = self._bucket(ts)
        if self.cur is None:
            self.cur = {"k": b, "o": price, "h": price, "l": price, "c": price}
        elif b != self.cur["k"]:
            out.extend(self._close(self.cur, ts))
            self.cur = {"k": b, "o": price, "h": price, "l": price, "c": price}
        else:
            self.cur["h"], self.cur["l"], self.cur["c"] = max(self.cur["h"], price), min(self.cur["l"], price), price
        out.extend(self._tick(ts, price))
        return out

    # --- a candle has closed -----------------------------------------------------------------------
    def _note(self, ts: datetime, price: float, text: str, kind: str = "note", level: float | None = None) -> Event:
        return Event(kind, ts, self.side, price, level, text)

    def _close(self, c: dict, ts: datetime) -> list[Event]:
        out: list[Event] = []
        sg, buy = self.sg, self.sg > 0
        idx = len(self.cs)
        self.cs.append(c)
        L, t = self.day_open, f"{c['k']:%H:%M}"
        on, against = ("above", "below") if buy else ("below", "above")          # which side of the day open level is "our" side
        hl = "high" if buy else "low"
        if self.st in ("seq", "armed", "pending") and sg * (c["c"] - L) < 0:
            out.append(self._note(ts, c["c"], f"{t} candle closed at {c['c']:,.2f}, {against} the day open level {L:,.2f}: the setup is cancelled."))
            self.st = "idle"
            return out
        if self.st == "idle":
            if sg * (c["c"] - L) > 0 and idx >= 1:             # a market shift: needs a candle before it for the stop search
                p = idx - 1
                lo = self.near(self.cs[p])
                while p > 0 and sg * (self.near(self.cs[p - 1]) - lo) <= 0:   # walk back while the stop-side extremes keep going further (or stay level)
                    p -= 1
                    lo = self.near(self.cs[p])
                self.setups += 1
                stop = lo if sg * (lo - self.near(c)) <= 0 else self.near(c)
                self.S = {"n": self.setups, "shift": c, "seq": [c], "stop": stop}
                self.st = "seq"
                out.append(self._note(ts, c["c"], f"{t} candle closed at {c['c']:,.2f}, {on} the day open level {L:,.2f}: market shift (setup {self.setups}). "
                                                  f"The stop search found a {'low' if buy else 'high'} of {lo:,.2f}.", "shift"))
        elif self.st == "seq":
            S, prev = self.S, self.S["seq"][-1]
            if sg * (self.far(c) - self.far(prev)) > 0 and sg * (self.near(c) - self.near(prev)) > 0:     # still climbing (BUY) / falling (SELL): the sequence goes on
                S["seq"].append(c)
                if sg * (self.near(c) - S["stop"]) < 0:
                    S["stop"] = self.near(c)
            else:                                             # not further along: the sequence ended with the previous candle
                ref = S["seq"][-1]
                stop = S["stop"]
                for x in S["seq"]:
                    if sg * (self.near(x) - stop) < 0:
                        stop = self.near(x)
                risk = sg * (ref["c"] - stop)
                if risk <= 0:                                  # the reference closed at the very edge of the search: no stop distance
                    out.append(self._note(ts, c["c"], f"Setup {S['n']} dropped: the reference close {ref['c']:,.2f} is not {on} the stop {stop:,.2f}."))
                    self.st = "idle"
                    return out
                S.update(ref_far=self.far(ref), ref_close=ref["c"], ref_k=ref["k"], stop=stop, target=ref["c"] + sg * self.r["target_x_risk"] * risk)
                out.append(self._note(ts, c["c"], f"Reference candle {ref['k']:%H:%M} ({hl} {S['ref_far']:,.2f}, close {S['ref_close']:,.2f}) after {len(S['seq'])} candle(s). "
                                                  f"Stop {S['stop']:,.2f}, target {S['target']:,.2f}. Waiting for a break {'above' if buy else 'below'} {S['ref_far']:,.2f}.", "armed", S["ref_far"]))
                self.st = "armed"
                if sg * (self.far(c) - S["ref_far"]) > 0:      # the candle that ended the sequence already broke the level
                    out.extend(self._breakout(ts, c["c"]))
        return out

    def _breakout(self, ts: datetime, price: float) -> list[Event]:
        S = self.S
        self.st = "pending"
        return [self._note(ts, price, f"Price broke {'above' if self.sg > 0 else 'below'} {S['ref_far']:,.2f}: a {self.side} now waits at {S['ref_close']:,.2f} "
                                      f"(stop {S['stop']:,.2f}, target {S['target']:,.2f}).", "break", S["ref_far"])]

    # --- every tick --------------------------------------------------------------------------------
    def _tick(self, ts: datetime, price: float) -> list[Event]:
        out: list[Event] = []
        sg, S = self.sg, self.S
        if self.st == "trade":
            if self.position is None:                         # closed on this tick (stop, target or square-off)
                self.st = "idle"
            return out
        if self.st == "armed" and sg * (price - S["ref_far"]) > 0:
            return self._breakout(ts, price)
        if self.st != "pending":
            return out
        cut = self.r["last_entry"]
        if cut is not None and ts.time() >= cut:
            self.st = "idle"
            return [self._note(ts, price, f"Setup {S['n']}: the {self.side} at {S['ref_close']:,.2f} was not filled before {cut:%H:%M}: cancelled.")]
        if sg * (price - S["ref_close"]) <= 0:                # price is back at (or gapped beyond) the reference entry price
            if sg * (price - S["stop"]) <= 0:                 # at or beyond the stop already (BUY: price at or below it, SELL: at or above it)
                self.st = "idle"
                return [self._note(ts, price, f"Setup {S['n']}: price came back at {price:,.2f}, at or beyond the stop {S['stop']:,.2f}: no trade.")]
            fill = price
            out.extend(self.signal(self.side, ts, fill, S["ref_close"], f"price came back to the reference entry price {S['ref_close']:,.2f} (setup {S['n']})",
                                   lambda entry: (S["stop"], S["target"] + (S["ref_close"] - fill))))
            self.st = "trade" if self.position is not None else "idle"
        elif sg * (price - S["target"]) >= 0:
            self.st = "idle"
            out.append(self._note(ts, price, f"Setup {S['n']}: price reached the target {S['target']:,.2f} without coming back to {S['ref_close']:,.2f}: no trade."))
        return out

    # --- dashboard ---------------------------------------------------------------------------------
    def levels(self) -> list[dict]:
        lv = [{"name": "day open", "value": self.day_open, "color": "muted"}] if self.day_open else []
        S = self.S
        if S and "target" in S:
            buy = self.sg > 0
            lv += [{"name": "breakout level", "value": S["ref_far"], "color": "blue" if buy else "orange"}, {"name": "entry (ref close)", "value": S["ref_close"], "color": "green" if buy else "red"},
                   {"name": "stop", "value": S["stop"], "color": "red" if buy else "green"}, {"name": "target", "value": S["target"], "color": "green" if buy else "red"}]
        return lv

    def describe_event(self, ev: Event):
        return ev.reason

    def status_text(self, price):
        if self.day_open is None:
            return "Waiting for the first price to read the day's open."
        base = f"Day open level {self.day_open:,.2f}."
        S, buy = self.S, self.sg > 0
        if self.st == "trade":
            return base + " A trade is open; no new setup is started until it is closed."
        if self.st == "idle":
            return base + (f" Waiting for a 5-minute candle to close {'above' if buy else 'below'} it (a market shift)." if not self.setups else
                           f" {self.setups} setup(s) so far today; waiting for the next candle to close {'above' if buy else 'below'} the level.")
        if self.st == "seq":
            return base + f" Market shift at {S['shift']['k']:%H:%M}; reading the confirmation candles ({len(S['seq'])} so far)."
        if self.st == "armed":
            return base + f" Reference candle {S['ref_k']:%H:%M}: waiting for a break {'above' if buy else 'below'} {S['ref_far']:,.2f} (then a {self.side} at {S['ref_close']:,.2f}, stop {S['stop']:,.2f}, target {S['target']:,.2f})."
        return base + f" Broke {'above' if buy else 'below'} {S['ref_far']:,.2f}: a {self.side} waits at {S['ref_close']:,.2f} (stop {S['stop']:,.2f}, target {S['target']:,.2f})."


# ---- the calculation panel of the Dry runs page, and the run-log line -----------------------------------------------------------
def preview(high: float, low: float, options: dict) -> dict:
    return {"summary": "Reads the day's open level and 5-minute candles; yesterday's High and Low are not used.",
            "notes": ["Day open level = the open of the first 5-minute candle. A candle closing above it starts a setup; the highest candle of the climbing sequence that follows is the reference candle.",
                      "After a break above the reference high, a BUY waits at the reference candle's close. Stop = the lowest low found by walking back from the candle before the shift; "
                      "target = reference entry + (reference entry - stop), plus the difference if the fill is lower.",
                      "Several setups a day, one position at a time. No quantity rule: the run's fixed quantity is used."],
            "cards": []}


def describe(high: float, low: float, options: dict) -> str:
    return ("Market shift: BUY after a 5-minute candle closes above the day open level, a climbing confirmation sequence and a break above the reference candle's high; "
            "the BUY waits at the reference close; stop = lowest low of the walk-back, target 1:1 from the reference (plus any better fill); several setups a day, one position at a time.")


GUIDE = [
    ("Idea", ["BUY only, on 5-minute candles. It does not use yesterday's High and Low: it reads the day's open and how the market moves above it.",
              "Any number of setups a day, but only one position at a time."]),
    ("1. Day open level", ["The open of the first 5-minute candle of the session (09:00 on MCX). It is fixed for the day."]),
    ("2. Market shift", ["A completed 5-minute candle closes above the day open level. That is the shift candle.",
                         "The first candle of the day cannot be one (the stop search needs a candle before it). Price moving above or below the level without a candle close changes nothing."]),
    ("3. Confirmation sequence", ["The candles after the shift candle, as long as each one is higher than the one before: a higher high and a higher low.",
                                  "No minimum and no maximum length. The first candle that is not higher ends the sequence."]),
    ("4. Reference candle", ["The last candle of the sequence, the highest one (the shift candle itself if no candle followed it).",
                             "Its high is the breakout level. Its close is the reference entry price."]),
    ("5. Invalidation", ["Until the trade is taken, any completed candle that closes below the day open level cancels the setup, including a waiting order.",
                         "The next candle that closes above the level starts a new, independent setup with its own entry, stop and target."]),
    ("6. Breakout", ["Price must trade above the reference candle's high. This only arms the setup; nothing is bought yet."]),
    ("7. Entry", ["After the breakout a BUY order waits at the reference entry price. If price comes back to it, the trade is taken there.",
                  "If price gaps down through it, the trade is taken at the lower price that comes.",
                  "If price never comes back, or reaches the target first, there is no trade. Missing a trade is fine."]),
    ("8. Stop-loss", ["Start at the candle before the shift candle and walk backwards. Keep going while each earlier candle's low is not higher than the lowest so far; stop at the first candle with a higher low.",
                      "The stop is the lowest low found, also compared with the lows of the shift and sequence candles. It is a fixed price and never moves."]),
    ("9. Target", ["Reference entry + (reference entry - stop), which is 1:1 from the reference price.",
                   "If the fill is lower than the reference price, the difference is added to the target. Example: reference 263.80, stop 261.80, fill 263.50 gives target 265.80 + 0.30 = 266.10."]),
    ("10. Exit", ["At the stop (market), at the target (limit), or at the session square-off (MCX 23:20)."]),
    ("Limits", ["Several setups a day, one position at a time: no new setup starts while a trade is open.",
                "No entry cut-off time in the strategy (the runner's session limit still applies). No quantity rule: the run's fixed quantity is used, so a backtest reads in points.",
                "Backtest on 1-minute or 5-minute data."]),
    ("Example (22 Oct, NATGASMINI)", ["Day open 262.60. 12:05 closes above it (shift). 12:10, 12:15, 12:20 each climb; 12:20 is the reference (high 264.00, close 263.80).",
                                      "Price breaks above 264.00 at 13:28; the BUY fills at 263.80 at 13:38. Stop 261.80 (the 12:00 low), target 265.80. Stopped at 15:19 for -2.00 points."]),
]

SPEC = register(StrategySpec(
    name="market_shift", label="Market shift strategy", engine=MarketShiftEngine, fields=[], preview=preview, describe=describe, guide=GUIDE,
    blurb="Reads the day's open level on 5-minute candles. When a candle closes above it and the candles that follow keep climbing, the highest one becomes the reference: "
          "after price breaks above its high, a BUY waits at its close. The stop is the lowest low found walking back from before the shift, the target is 1:1 from the reference price "
          "(plus any better fill). A close below the open level cancels the setup. Several setups a day, one position at a time; the strategy has no quantity rule."))
