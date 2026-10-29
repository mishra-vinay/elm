"""Market shift SELL. The mirror image of `market_shift` (the BUY strategy): same engine (`MarketShiftEngine` with SIDE = "SELL"), every comparison reversed. The numbers are in RULES.

  1. DAY OPEN LEVEL: the open of the first 5-minute candle of the session.
  2. MARKET SHIFT: a completed 5-minute candle closes BELOW the day open level (the shift candle; the first candle of the day can never be one).
  3. CONFIRMATION SEQUENCE: the candles that follow, while each one is lower than the one before (a lower low AND a lower high); no minimum, no maximum length.
  4. REFERENCE CANDLE: the last candle of the sequence, the lowest one (the shift candle itself if none followed).
       breakout level        = its LOW
       reference entry price = its close
  5. INVALIDATION: until the trade is taken, a completed candle that closes ABOVE the day open level cancels the setup (a pending order too). The next close below the level starts a new, independent setup.
  6. BREAKOUT: price trades below the reference candle's low.
  7. ENTRY: after the breakout a SELL order waits at the reference entry price. When price comes back UP to it the trade is taken (at the higher price that comes if price gaps up through it).
     If price reaches the target first, or the setup is invalidated, there is no trade.
  8. STOP: start at the candle before the shift candle and walk BACKWARDS while each earlier candle's high is not lower than the highest high so far; stop at the first candle with a lower high.
     The stop is the highest high found, also compared with the highs of the shift and sequence candles. A fixed price.
  9. TARGET: reference entry - (stop - reference entry), 1:1 from the reference price. A fill HIGHER than the reference price SUBTRACTS the difference from the target (the mirror of the BUY rule). The stop does not move.
 10. EXIT: stop (market), target (limit), or the runner's session square-off (MCX 23:20).

Several setups a day, one position at a time; no quantity rule (the run's fixed quantity), no entry cut-off. See market_shift.py for the details shared by both sides.
"""
from __future__ import annotations

from ..strategy import StrategySpec, register
from .market_shift import MarketShiftEngine

RULES = {
    "candle_minutes": 5,        # the candle size the rules are read on
    "target_x_risk": 1.0,       # the target is this many times (stop - reference entry) below the reference entry price (1.0 = 1:1)
    "last_entry": None,         # a waiting order is cancelled from this time of day (None = no cut-off; the runner's session square-off still applies)
}


class MarketShiftSellEngine(MarketShiftEngine):
    SIDE = "SELL"
    rules = RULES


def preview(high: float, low: float, options: dict) -> dict:
    return {"summary": "Reads the day's open level and 5-minute candles; yesterday's High and Low are not used.",
            "notes": ["Day open level = the open of the first 5-minute candle. A candle closing below it starts a setup; the lowest candle of the falling sequence that follows is the reference candle.",
                      "After a break below the reference low, a SELL waits at the reference candle's close. Stop = the highest high found by walking back from the candle before the shift; "
                      "target = reference entry - (stop - reference entry), minus the difference if the fill is higher.",
                      "Several setups a day, one position at a time. No quantity rule: the run's fixed quantity is used."],
            "cards": []}


def describe(high: float, low: float, options: dict) -> str:
    return ("Market shift SELL: SELL after a 5-minute candle closes below the day open level, a falling confirmation sequence and a break below the reference candle's low; "
            "the SELL waits at the reference close; stop = highest high of the walk-back, target 1:1 from the reference (minus any better fill); several setups a day, one position at a time.")


GUIDE = [
    ("Idea", ["SELL only, on 5-minute candles: the exact mirror of the Market shift (BUY) strategy. It does not use yesterday's High and Low; it reads the day's open and how the market moves below it.",
              "Any number of setups a day, but only one position at a time."]),
    ("1. Day open level", ["The open of the first 5-minute candle of the session (09:00 on MCX). It is fixed for the day."]),
    ("2. Market shift", ["A completed 5-minute candle closes below the day open level. That is the shift candle.",
                         "The first candle of the day cannot be one (the stop search needs a candle before it). Price moving above or below the level without a candle close changes nothing."]),
    ("3. Confirmation sequence", ["The candles after the shift candle, as long as each one is lower than the one before: a lower low and a lower high.",
                                  "No minimum and no maximum length. The first candle that is not lower ends the sequence."]),
    ("4. Reference candle", ["The last candle of the sequence, the lowest one (the shift candle itself if no candle followed it).",
                             "Its low is the breakout level. Its close is the reference entry price."]),
    ("5. Invalidation", ["Until the trade is taken, any completed candle that closes above the day open level cancels the setup, including a waiting order.",
                         "The next candle that closes below the level starts a new, independent setup with its own entry, stop and target."]),
    ("6. Breakout", ["Price must trade below the reference candle's low. This only arms the setup; nothing is sold yet."]),
    ("7. Entry", ["After the breakout a SELL order waits at the reference entry price. If price comes back up to it, the trade is taken there.",
                  "If price gaps up through it, the trade is taken at the higher price that comes.",
                  "If price never comes back, or reaches the target first, there is no trade. Missing a trade is fine."]),
    ("8. Stop-loss", ["Start at the candle before the shift candle and walk backwards. Keep going while each earlier candle's high is not lower than the highest so far; stop at the first candle with a lower high.",
                      "The stop is the highest high found, also compared with the highs of the shift and sequence candles. It is a fixed price and never moves."]),
    ("9. Target", ["Reference entry - (stop - reference entry), which is 1:1 from the reference price.",
                   "If the fill is higher than the reference price, the difference is subtracted from the target. Example: reference 263.80, stop 265.80, fill 264.10 gives target 261.80 - 0.30 = 261.50."]),
    ("10. Exit", ["At the stop (market), at the target (limit), or at the session square-off (MCX 23:20)."]),
    ("Limits", ["Several setups a day, one position at a time: no new setup starts while a trade is open.",
                "No entry cut-off time in the strategy (the runner's session limit still applies). No quantity rule: the run's fixed quantity is used, so a backtest reads in points.",
                "Backtest on 1-minute or 5-minute data."]),
]

SPEC = register(StrategySpec(
    name="market_shift_sell", label="Market shift SELL", engine=MarketShiftSellEngine, fields=[], preview=preview, describe=describe, guide=GUIDE,
    blurb="The mirror of the Market shift strategy, on 5-minute candles. When a candle closes below the day's open level and the candles that follow keep falling, the lowest one becomes the reference: "
          "after price breaks below its low, a SELL waits at its close. The stop is the highest high found walking back from before the shift, the target is 1:1 from the reference price "
          "(minus any better fill). A close above the open level cancels the setup. Several setups a day, one position at a time; the strategy has no quantity rule."))
