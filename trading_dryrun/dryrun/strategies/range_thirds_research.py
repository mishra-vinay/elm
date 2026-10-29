"""RESEARCH VERSION of Range thirds, with every option (both rules, both entry modes, windows). Hidden from the dropdowns: the strategy you run is the simple
`range_thirds.py`. This file exists so the earlier D/6 rule, the sweep tool (`range_thirds_backtest.py`) and `scripts/verify_backtest.py` keep working.

Range thirds: a previous-day High/Low strategy built on thirds of the previous day's range (D = High - Low).

THE CURRENT RULE (`rule = "dev"`, the default; the same as tradingview/range_thirds_strategy.pine). third = D/3, dev = "the 0.8"
(0.8% of the previous High for a BUY / of the previous Low for a SELL, or 0.8 price points: `dev_unit`):
  Rule 1, BUY  (High side): entry = PDH - (third - dev) ; stop = entry - dev ; target = entry + third + (third - dev)   [= PDH + third]
  Rule 2, SELL (Low side) : entry = PDL + (third - dev) ; stop = entry + dev ; target = entry - third - (third - dev)   [= PDL - third]
All three are fixed prices computed from the previous day's High/Low (not re-based on the actual fill). The risk is exactly `dev`, the reward third + (third - dev).
A side is skipped on a day when that target distance is not positive (the previous range is tiny compared with dev).

Rules around it (all adjustable):
  * approach entry (default): price comes from the inside of the range and touches the entry price, before the level breaks.
  * `gap_filter = "on"` (approach mode): no SELL on a day that OPENS below the previous Low, no BUY on a day that opens above the previous High.
  * `skip_minutes`: no entry in the first candle of the day (default 5 minutes); an order may be armed from the end of that period on.
  * `last_entry`: no new entry from that time (default 18:00); a pending (armed) side is cancelled then. Open positions keep their stop and target
    until the runner's session square-off.
  * `trades_per_day`: total trades of both sides together (default 1); counted when the entry fills, not given back when it closes; once one side has
    traded the other side is cancelled.

THE EARLIER RULE (`rule = "classic"`, kept so past results can be reproduced and the fast simulator verified):
  trigger distance e = entry_frac * D (default D/6), stop distance s = stop_frac * D (D/3), target distance t = target_frac * D (D/3 from the fill):
    SELL: enter at L + e ; stop L + s (`stop_side = inside`, on the losing side) or L - s (`outside`, beyond the level: it lies beyond the target
    so it can never limit a loss) ; target entry - t.   BUY: the mirror image from H.

`entry_mode`: approach (above) or after_break (the level must trade first, the entry is the pullback to the trigger).

Backtests: see docs/range-thirds-study.md (the classic rule on a year of Bajaj Finance and Reliance 1-minute candles found no profitable setting).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from ..strategy import Event, StrategyEngine, StrategySpec, register

DEFAULTS = {"entry_mode": "approach", "rule": "dev", "dev_value": 0.8, "dev_unit": "pct", "third_frac": 1 / 3,
            "entry_frac": 1 / 6, "stop_frac": 1 / 3, "target_frac": 1 / 3, "stop_side": "inside",
            "gap_filter": "on", "skip_minutes": 5.0, "last_entry": "18:00", "trades_per_day": 1}
CLASSIC = {"rule": "classic", "skip_minutes": 0, "last_entry": "none", "trades_per_day": 2}      # the earlier rule with no time window (tests, sweep tool)
LAST_ENTRY_CHOICES = ["none", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00", "21:00", "22:00"]


def opts(o: dict) -> dict:
    return {**DEFAULTS, **{k: v for k, v in (o or {}).items() if v is not None}}


def validate(o: dict) -> None:
    o = opts(o)
    if o["entry_mode"] not in ("approach", "after_break"):
        raise ValueError("entry_mode must be 'approach' or 'after_break'")
    if o["rule"] not in ("dev", "classic"):
        raise ValueError("rule must be 'dev' or 'classic'")
    if o["gap_filter"] not in ("on", "off"):
        raise ValueError("gap_filter must be 'on' or 'off'")
    if o["skip_minutes"] < 0:
        raise ValueError("skip_minutes must be >= 0")
    if o["last_entry"] not in LAST_ENTRY_CHOICES:
        raise ValueError("last_entry must be one of " + ", ".join(LAST_ENTRY_CHOICES))
    if o["trades_per_day"] < 1:
        raise ValueError("trades_per_day must be >= 1")
    if o["rule"] == "dev":
        if o["dev_unit"] not in ("pct", "points"):
            raise ValueError("dev_unit must be 'pct' or 'points'")
        if o["dev_value"] <= 0 or o["third_frac"] <= 0:
            raise ValueError("dev_value and third_frac must be > 0")
        return
    if o["stop_side"] not in ("outside", "inside"):
        raise ValueError("stop_side must be 'outside' or 'inside'")
    if o["entry_frac"] < 0 or o["target_frac"] <= 0 or o["stop_frac"] <= 0:
        raise ValueError("entry_frac must be >= 0 and stop_frac and target_frac must be > 0")
    if o["stop_side"] == "inside" and o["stop_frac"] <= o["entry_frac"]:
        raise ValueError("for an inside stop, stop_frac must be larger than entry_frac (the stop has to be beyond the entry)")


def _cutoff(o: dict) -> time | None:
    return None if o["last_entry"] == "none" else time(int(o["last_entry"][:2]), int(o["last_entry"][3:]))


class RangeThirdsEngine(StrategyEngine):
    def __init__(self, high: float, low: float, params, approve=None):
        super().__init__(high, low, params, approve)
        self.o = opts(params.options)
        self.d = high - low
        self.third = self.o["third_frac"] * self.d
        self.dev_rule = self.o["rule"] == "dev"
        self.armed = {"BUY": False, "SELL": False}
        self._noted: set[str] = set()
        self.first_ts: datetime | None = None
        self._sync_flags()

    def blocked(self, side: str) -> bool:
        """Day-open filter (approach mode): the day opened beyond this side's level, so the side is off for the whole day."""
        if self.o["gap_filter"] != "on" or self.o["entry_mode"] != "approach" or self.day_open is None:
            return False
        return self.day_open > self.high if side == "BUY" else self.day_open < self.low

    # --- prices ----------------------------------------------------------------------------------
    def dev(self, side: str) -> float:
        """The '0.8' as a price distance: % of the previous High (BUY) / Low (SELL), or plain price points."""
        v = self.o["dev_value"]
        return v / 100 * (self.high if side == "BUY" else self.low) if self.o["dev_unit"] == "pct" else v

    @property
    def buy_entry_level(self) -> float:
        return self.high - (self.third - self.dev("BUY")) if self.dev_rule else self.high - self.o["entry_frac"] * self.d

    @property
    def sell_entry_level(self) -> float:
        return self.low + (self.third - self.dev("SELL")) if self.dev_rule else self.low + self.o["entry_frac"] * self.d

    @property
    def buy_stop_level(self) -> float:
        if self.dev_rule:
            return self.buy_entry_level - self.dev("BUY")
        s = self.o["stop_frac"] * self.d
        return self.high + s if self.o["stop_side"] == "outside" else self.high - s

    @property
    def sell_stop_level(self) -> float:
        if self.dev_rule:
            return self.sell_entry_level + self.dev("SELL")
        s = self.o["stop_frac"] * self.d
        return self.low - s if self.o["stop_side"] == "outside" else self.low + s

    def target_dist(self, side: str) -> float:
        return self.third + (self.third - self.dev(side))

    @property
    def buy_target_level(self) -> float:
        return self.buy_entry_level + self.target_dist("BUY")

    @property
    def sell_target_level(self) -> float:
        return self.sell_entry_level - self.target_dist("SELL")

    def side_valid(self, side: str) -> bool:
        return self.target_dist(side) > 0 if self.dev_rule else True

    # --- the entry window ------------------------------------------------------------------------
    def _start(self, ts: datetime) -> datetime:
        return datetime.combine(ts.date(), self.session_open) if self.session_open else (self.first_ts or ts)

    def entry_open(self, ts: datetime) -> bool:
        """No entry in the first `skip_minutes` of the session, none from `last_entry`, and none once `trades_per_day` trades have been entered."""
        if ts < self._start(ts) + timedelta(minutes=self.o["skip_minutes"]):
            return False
        cut = _cutoff(self.o)
        if cut is not None and ts.time() >= cut:
            return False
        return sum(self.trades_by_side.values()) < self.o["trades_per_day"]

    def _sync_flags(self) -> None:
        if self.o["entry_mode"] == "approach":
            self.flags = {"BUY armed": self.armed["BUY"], "SELL armed": self.armed["SELL"]}
        else:
            self.flags = {"High broken, waiting for pullback": self.armed["BUY"], "Low broken, waiting for pullback": self.armed["SELL"]}

    # --- ticks -----------------------------------------------------------------------------------
    def evaluate(self, ts: datetime, price: float) -> list[Event]:
        out: list[Event] = []
        if self.first_ts is None:
            self.first_ts = ts
        if self.day_open is None:
            self.day_open = price
        for side in ("BUY", "SELL"):
            if self.blocked(side) and side not in self._noted:
                self._noted.add(side)
                up = side == "BUY"
                out.append(Event("note", ts, side, price, self.high if up else self.low,
                                 f"{side} is off for today: the day opened at {self.day_open:,.2f}, {'above the previous High' if up else 'below the previous Low'} "
                                 f"{(self.high if up else self.low):,.2f}"))
            out.extend(self._side(side, ts, price))
        self._sync_flags()
        return out

    def _side(self, side: str, ts: datetime, price: float) -> list[Event]:
        up = side == "BUY"
        trig = self.buy_entry_level if up else self.sell_entry_level
        level = self.high if up else self.low
        approach = self.o["entry_mode"] == "approach"
        if not self.entry_open(ts):                       # before the first candle is over, after the cut-off, or the day's trade is used: nothing armed
            self.armed[side] = False
            return []
        if not self.armed[side]:
            if self.trades_by_side[side] >= self.p.max_trades_per_side or self.blocked(side) or not self.side_valid(side):
                return []
            if approach:
                ready = price < trig if up else price > trig                  # still on the inside, before the trigger
                note = "price is on the inside of the %s trigger %.2f" % (side, trig)
            else:
                ready = price > self.high if up else price < self.low         # the level has traded
                note = "price broke previous day %s" % ("High" if up else "Low")
            if not ready:
                return []
            self.armed[side] = True
            return [Event("armed" if approach else "break", ts, side, price, level, note)]
        hit = (price >= trig if up else price <= trig) if approach else (price <= trig if up else price >= trig)
        if not hit:
            return []
        self.armed[side] = False
        if self.dev_rule:
            stop, tgt = (self.buy_stop_level, self.buy_target_level) if up else (self.sell_stop_level, self.sell_target_level)
            exits = lambda entry: (stop, tgt)                                  # noqa: E731  fixed prices, not re-based on the fill
        else:
            tf, sign = self.o["target_frac"] * self.d, 1 if up else -1
            stop = self.buy_stop_level if up else self.sell_stop_level
            exits = lambda entry: (stop, entry + sign * tf)                    # noqa: E731
        return self.signal(side, ts, price, level, "price reached the %s trigger" % side if approach else "pullback after break", exits)

    def _manage(self, ts: datetime, price: float):
        if self.dev_rule or self.o["stop_side"] == "inside":
            return super()._manage(ts, price)
        # classic rule, stop beyond the level (SELL: below the Low, BUY: above the High), i.e. on the profit side of the entry
        pos = self.position
        buy = pos.side == "BUY"
        if pos.stop is not None and (price >= pos.stop if buy else price <= pos.stop):
            return self._exit(ts, price, "stop", market=True)
        if price >= pos.target if buy else price <= pos.target:
            return self._exit(ts, pos.target, "target", market=False)
        return None

    # --- dashboard -------------------------------------------------------------------------------
    def levels(self) -> list[dict]:
        out = [{"name": "prev High", "value": self.high, "color": "blue"}, {"name": "buy trigger", "value": self.buy_entry_level, "color": "green"},
               {"name": "sell trigger", "value": self.sell_entry_level, "color": "red"}, {"name": "prev Low", "value": self.low, "color": "orange"},
               {"name": "buy stop", "value": self.buy_stop_level, "color": "muted"}, {"name": "sell stop", "value": self.sell_stop_level, "color": "muted"}]
        if self.dev_rule:
            out += [{"name": "buy target", "value": self.buy_target_level, "color": "green"}, {"name": "sell target", "value": self.sell_target_level, "color": "red"}]
        return out

    def describe_event(self, ev: Event):
        if ev.kind == "note":
            return ev.reason
        if ev.kind == "armed":
            trig = self.buy_entry_level if ev.side == "BUY" else self.sell_entry_level
            return f"{ev.side} armed: waiting for price to {'rise' if ev.side == 'BUY' else 'fall'} to the trigger {trig:,.2f}."
        if ev.kind == "break":
            up = ev.side == "BUY"
            return (f"price broke {'above the previous High' if up else 'below the previous Low'} ({self.high if up else self.low:,.2f}); "
                    f"waiting for a pullback to {'<= ' + format(self.buy_entry_level, ',.2f') if up else '>= ' + format(self.sell_entry_level, ',.2f')}.")
        return ev.reason

    def status_text(self, price):
        off = " ".join(f"{s} is off today (the day opened at {self.day_open:,.2f}, {'above the previous High' if s == 'BUY' else 'below the previous Low'})."
                       for s in ("BUY", "SELL") if self.blocked(s))
        if self.o["entry_mode"] == "approach":
            waits = []
            if self.armed["BUY"]:
                waits.append(f"BUY when price rises to {self.buy_entry_level:,.2f} (stop {self.buy_stop_level:,.2f})")
            if self.armed["SELL"]:
                waits.append(f"SELL when price falls to {self.sell_entry_level:,.2f} (stop {self.sell_stop_level:,.2f})")
            if waits:
                main = "Waiting: " + "; ".join(waits) + "."
            elif sum(self.trades_by_side.values()) >= self.o["trades_per_day"]:
                main = "Today's trade has been entered; no further entry."
            else:
                main = "No side is armed (the first candle or the entry cut-off applies, price is already beyond both triggers, or a side is off)."
            return (main + " " + off).strip()
        notes = []
        if self.armed["BUY"]:
            notes.append(f"Price broke above the previous High; waiting for a pullback to {self.buy_entry_level:,.2f} or lower to BUY.")
        if self.armed["SELL"]:
            notes.append(f"Price broke below the previous Low; waiting for a pullback to {self.sell_entry_level:,.2f} or higher to SELL.")
        return " ".join(notes) or (f"A setup starts if price trades above {self.high:,.2f} or below {self.low:,.2f}.")


# ---- registration: settings form, calculation preview, run-log description -------------------------------------------------
def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def _window_notes(o: dict) -> list[str]:
    out = []
    if o["gap_filter"] == "on" and o["entry_mode"] == "approach":
        out.append("Day-open filter: no SELL if the day opens below the previous Low, no BUY if it opens above the previous High.")
    cut = "no entry from " + o["last_entry"] + " (an armed side is cancelled then)" if o["last_entry"] != "none" else "no entry cut-off"
    out.append(f"Entry window: none in the first {o['skip_minutes']:g} minutes of the session, {cut}; {o['trades_per_day']:g} trade(s) a day in total, BUY and SELL together "
               "(counted when the entry fills). Open positions keep their stop and target until the session square-off.")
    return out


def _preview_dev(high: float, low: float, o: dict) -> dict:
    d = high - low
    third = o["third_frac"] * d
    appr = o["entry_mode"] == "approach"
    pct = o["dev_unit"] == "pct"
    dev_b = o["dev_value"] / 100 * high if pct else o["dev_value"]
    dev_s = o["dev_value"] / 100 * low if pct else o["dev_value"]
    buy_e, sell_e = high - (third - dev_b), low + (third - dev_s)
    tdist_b, tdist_s = third + (third - dev_b), third + (third - dev_s)
    valid_b, valid_s = tdist_b > 0, tdist_s > 0
    summary = (f"D = High - Low = {_fmt(high)} - {_fmt(low)} = <b>{_fmt(d)}</b> · D/3 = <b>{_fmt(third)}</b> · the {o['dev_value']:g}"
               f"{'%' if pct else ' points'} = <b>{_fmt(dev_b)}</b> (BUY, of the High) / <b>{_fmt(dev_s)}</b> (SELL, of the Low)")
    notes = []
    if not (valid_b and valid_s):
        notes.append("<span class='warnc'><b>Note:</b> the target distance D/3 + (D/3 - dev) is not positive for a side, so that side is skipped; the previous range is too small for this dev.</span>")
    elif buy_e > high or sell_e < low:
        notes.append("<span class='warnc'><b>Note:</b> D/3 is smaller than dev, so an entry sits beyond the previous High/Low (a breakout entry) and the reward is smaller than the risk.</span>")
    notes += _window_notes(o)
    notes.append(("Approach mode: the trade is taken before the level breaks, when price comes from inside the range and touches the entry price."
                  if appr else "After-break mode: the level must trade first (above the High for BUY, below the Low for SELL); the entry is the pullback to the entry price.")
                 + " All prices are fixed from the previous day's High/Low; the actual entry is the tick that reaches the price plus slippage.")

    def card(title, side):
        up = side == "BUY"
        e, dev, tdist, valid = (buy_e, dev_b, tdist_b, valid_b) if up else (sell_e, dev_s, tdist_s, valid_s)
        stop = e - dev if up else e + dev
        tgt = e + tdist if up else e - tdist
        lvl = high if up else low
        return {"title": title, "rows": [
            ["Arms when", (f"price is below {_fmt(e)} (inside the range)" if up else f"price is above {_fmt(e)} (inside the range)") if appr else
             (f"price trades above the High {_fmt(high)}" if up else f"price trades below the Low {_fmt(low)}")],
            ["Entry price", f"<b>{_fmt(e)}</b> <span class='muted'>= {'High' if up else 'Low'} {'-' if up else '+'} (D/3 - dev) = {_fmt(lvl)} {'-' if up else '+'} {_fmt(third - dev)}</span>"],
            ["Stop", f"<b>{_fmt(stop)}</b> <span class='muted'>= entry {'-' if up else '+'} {_fmt(dev)} (risk {dev / e * 100:.2f}%)</span>"],
            ["Target", (f"<b>{_fmt(tgt)}</b> <span class='muted'>= entry {'+' if up else '-'} {_fmt(tdist)} (reward {tdist / e * 100:.2f}%, {tdist / dev:.2f} : 1)</span>"
                        if valid else "<span class='warnc'>side skipped (target distance not positive)</span>")]]}

    return {"summary": summary, "notes": notes, "cards": [card("Rule 1 · BUY (High side)", "BUY"), card("Rule 2 · SELL (Low side)", "SELL")]}


def _preview_classic(high: float, low: float, o: dict) -> dict:
    d = high - low
    e, st, tg = o["entry_frac"] * d, o["stop_frac"] * d, o["target_frac"] * d
    appr, outside = o["entry_mode"] == "approach", o["stop_side"] == "outside"
    sell_e, buy_e = low + e, high - e
    sell_s, buy_s = (low - st, high + st) if outside else (low + st, high - st)
    sell_t, buy_t = sell_e - tg, buy_e + tg
    sell_dead, buy_dead = outside and sell_s <= sell_t, outside and buy_s >= buy_t
    pc = lambda a, b: f"{abs(a / b - 1) * 100:.2f}%"      # noqa: E731

    def stop_row(s, e_, t, dead, where):
        return ["Stop", f"<b>{_fmt(s)}</b> <span class='muted'>{where}</span>" + (f" <span class='warnc'>· beyond the target {_fmt(t)}</span>" if dead else f" · risk {_fmt(abs(e_ - s))} ({pc(s, e_)})")]

    summary = (f"D = High - Low = {_fmt(high)} - {_fmt(low)} = <b>{_fmt(d)}</b> · entry {o['entry_frac']:.4f} × D = <b>{_fmt(e)}</b> · stop {o['stop_frac']:.4f} × D = <b>{_fmt(st)}</b>"
               f" · target {o['target_frac']:.4f} × D = <b>{_fmt(tg)}</b>" + ("" if outside else f" · reward : risk = <b>{tg / (st - e):.2f} : 1</b>"))
    notes = []
    if sell_dead or buy_dead:
        notes.append("<span class='warnc'><b>Note:</b> with the stop placed beyond the level, price has to pass the target before it can reach the stop, so the stop can never limit a loss. "
                     "A trade ends at its target or at the square-off (and the daily loss limit blocks new trades). Choose the stop on the losing side of the entry.</span>")
    notes += _window_notes(o)
    notes.append(("Approach mode: the trade is taken before the level breaks, when price comes from inside the range and touches the entry price."
                  if appr else "After-break mode: the level must trade first (below the Low for SELL, above the High for BUY); the entry is the pullback to the entry price.")
                 + " Actual entry = the tick that reaches the price plus slippage; the target is measured from that entry.")
    return {"summary": summary, "notes": notes, "cards": [
        {"title": "Rule 1 · SELL (Low side)", "rows": [
            ["Arms when", f"price is above {_fmt(sell_e)} (inside the range)" if appr else f"price trades below the Low {_fmt(low)}"],
            ["Entry price", f"<b>{_fmt(sell_e)}</b> <span class='muted'>= Low + {_fmt(e)}; {'price falls to it' if appr else 'price rises back to it'}</span>"],
            stop_row(sell_s, sell_e, sell_t, sell_dead, f"= Low - {_fmt(st)} (below the Low)" if outside else f"= Low + {_fmt(st)}"),
            ["Target", f"<b>{_fmt(sell_t)}</b> <span class='muted'>= entry - {_fmt(tg)}</span> · reward {_fmt(tg)} ({pc(sell_e, sell_t)})"]]},
        {"title": "Rule 2 · BUY (High side)", "rows": [
            ["Arms when", f"price is below {_fmt(buy_e)} (inside the range)" if appr else f"price trades above the High {_fmt(high)}"],
            ["Entry price", f"<b>{_fmt(buy_e)}</b> <span class='muted'>= High - {_fmt(e)}; {'price rises to it' if appr else 'price falls back to it'}</span>"],
            stop_row(buy_s, buy_e, buy_t, buy_dead, f"= High + {_fmt(st)} (above the High)" if outside else f"= High - {_fmt(st)}"),
            ["Target", f"<b>{_fmt(buy_t)}</b> <span class='muted'>= entry + {_fmt(tg)}</span> · reward {_fmt(tg)} ({pc(buy_t, buy_e)})"]]}]}


def preview(high: float, low: float, options: dict) -> dict:
    o = opts(options)
    validate(o)
    return _preview_dev(high, low, o) if o["rule"] == "dev" else _preview_classic(high, low, o)


def describe(high: float, low: float, options: dict) -> str:
    o = opts(options)
    d = high - low
    win = (f" No entry in the first {o['skip_minutes']:g} min, none from {o['last_entry']}, {o['trades_per_day']:g} trade(s) a day." if o["last_entry"] != "none"
           else f" No entry in the first {o['skip_minutes']:g} min, {o['trades_per_day']:g} trade(s) a day.")
    gap = " Day-open filter on." if o["gap_filter"] == "on" and o["entry_mode"] == "approach" else ""
    if o["rule"] == "dev":
        third = o["third_frac"] * d
        pct = o["dev_unit"] == "pct"
        db, ds = (o["dev_value"] / 100 * high, o["dev_value"] / 100 * low) if pct else (o["dev_value"], o["dev_value"])
        be, se = high - (third - db), low + (third - ds)
        return (f"Range thirds ({o['entry_mode']}): D = {d:,.2f}, D/3 = {third:,.2f}, the {o['dev_value']:g}{'%' if pct else ' pts'} = {db:,.2f}/{ds:,.2f}: "
                f"BUY entry {be:,.2f}, stop {be - db:,.2f}, target {be + third + (third - db):,.2f}; "
                f"SELL entry {se:,.2f}, stop {se + ds:,.2f}, target {se - third - (third - ds):,.2f}." + win + gap)
    return (f"Range thirds, classic rule ({o['entry_mode']}): D = {d:,.2f}; trigger {o['entry_frac']:.3f}*D = {o['entry_frac'] * d:,.2f} inside the level "
            f"(BUY at {high - o['entry_frac'] * d:,.2f}, SELL at {low + o['entry_frac'] * d:,.2f}), stop {o['stop_frac']:.3f}*D "
            f"({'SELL below the Low, BUY above the High' if o['stop_side'] == 'outside' else 'on the losing side of the entry'}), target {o['target_frac']:.3f}*D." + win + gap)


SPEC = register(StrategySpec(
    name="range_thirds_research", label="Range thirds, research version (all options)", engine=RangeThirdsEngine, hidden=True,
    fields=[{"key": "entry_mode", "label": "Entry mode", "type": "select", "default": "approach", "help": "Approach: trade before the level breaks, when price comes from inside the range and touches the entry price. After break: the level must break first, then the entry is the pullback.",
             "choices": [["approach", "Approach"], ["after_break", "After break"]]},
            {"key": "rule", "label": "Rule", "type": "select", "default": "dev",
             "help": "Current: entry D/3 minus the 0.8, stop 0.8, target D/3 + (D/3 - 0.8). Earlier: entry D/6, stop D/3, target D/3.",
             "choices": [["dev", "Current (the 0.8)"], ["classic", "Earlier (D/6)"]]},
            {"key": "dev_value", "label": "The 0.8 (risk, and what is taken off D/3)", "type": "number", "default": 0.8, "step": 0.1},
            {"key": "dev_unit", "label": "The 0.8 means", "type": "select", "default": "pct",
             "help": "% of price: 0.8% of the previous High (BUY) or of the previous Low (SELL). Price points: 0.8 price units.",
             "choices": [["pct", "% of price"], ["points", "Price points"]]},
            {"key": "third_frac", "label": "D fraction used as 'D/3'", "type": "number", "default": round(1 / 3, 6), "step": 0.01},
            {"key": "entry_frac", "label": "Entry distance (× D)", "type": "number", "default": round(1 / 6, 6), "step": 0.01},
            {"key": "stop_frac", "label": "Stop distance (× D)", "type": "number", "default": round(1 / 3, 6), "step": 0.01},
            {"key": "target_frac", "label": "Target distance (× D)", "type": "number", "default": round(1 / 3, 6), "step": 0.01},
            {"key": "stop_side", "label": "Stop placement", "type": "select", "default": "inside",
             "choices": [["inside", "on the losing side of the entry"], ["outside", "beyond the level (never triggers before the target)"]]},
            {"key": "gap_filter", "label": "Day-open filter", "type": "select", "default": "on", "help": "On: no SELL if the day opens below the previous Low and no BUY if it opens above the previous High (approach mode only).",
             "choices": [["on", "On"], ["off", "Off"]]},
            {"key": "skip_minutes", "label": "No entry in the first (minutes)", "help": "5 = the first 5-minute candle of the day", "type": "number", "default": 5.0, "step": 1},
            {"key": "last_entry", "label": "No new entry from", "type": "select", "default": "18:00",
             "choices": [[c, c if c == "none" else c + " IST"] for c in LAST_ENTRY_CHOICES]},
            {"key": "trades_per_day", "label": "Trades per day", "help": "BUY and SELL together", "type": "number", "default": 1, "step": 1}],
    validate=validate, preview=preview, describe=describe))
