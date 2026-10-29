"""Market shift SELL: the mirror image of the BUY strategy. Every scenario of test_market_shift.py is reflected around 100 (price -> 200 - price, highs and lows swap),
so the day open is still 100 and every price, entry, stop and target of the BUY scenario becomes 200 - that price."""
from datetime import datetime, time, timedelta

import pytest

from dryrun.replay import candle_path, feed_path
from dryrun.strategies.market_shift_sell import RULES, MarketShiftSellEngine
from dryrun.strategy import Params, make_engine
from test_market_shift import BASE, C, notes, rules                        # noqa: F401  (rules() works on the BUY dict; SELL has its own below)
from contextlib import contextmanager

D0 = datetime(2026, 10, 13)
pytestmark = pytest.mark.usefixtures("only_shipped")


def M(c):
    """The mirror image of a candle dict."""
    return {"ts": c["ts"], "open": 200 - c["open"], "high": 200 - c["low"], "low": 200 - c["high"], "close": 200 - c["close"]}


def m(price):
    return 200 - price


@contextmanager
def srules(**r):
    old = dict(RULES)
    RULES.update(r)
    try:
        yield
    finally:
        RULES.clear()
        RULES.update(old)


def run(buy_candles, **r):
    with srules(**r):
        eng = make_engine(120.0, 90.0, Params(strategy="market_shift_sell", slippage_pct=0, cost_pct=0, daily_loss_limit_pct=None))
        eng.session_open = time(9, 0)
        evs = []
        orig = eng.on_tick
        eng.on_tick = lambda ts, p: (lambda e: (evs.extend(e), e)[1])(orig(ts, p))
        last = None
        for c in map(M, buy_candles):
            path = candle_path(c["open"], c["high"], c["low"], c["close"])
            feed_path(eng, c["ts"], path)
            last = c["ts"] + timedelta(seconds=len(path))
        eng.square_off(last, reason="squareoff")
        return eng, evs


def test_a_sell_setup_reference_breakdown_fill_at_the_reference_close_and_target():
    e, ev = run(BASE + [C(9, 35, 102.5, 103, 101.5, 102.8), C(9, 40, 103, 107, 102.9, 106.8)])
    assert isinstance(e, MarketShiftSellEngine) and len(e.closed) == 1
    t = e.closed[0]
    assert t["side"] == "SELL" and t["entry_price"] == pytest.approx(m(101.8)) and t["stop"] == pytest.approx(m(97.0)) and t["target"] == pytest.approx(m(106.6))
    assert t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(m(106.6)) and t["net_pnl"] == pytest.approx(4.8)
    text = " | ".join(notes(ev))
    assert "below the day open level" in text and "market shift" in text and "low 98.00, close 98.20" in text and "broke below 98.00" in text


def test_a_fill_above_the_reference_price_keeps_the_stop_and_subtracts_the_difference_from_the_target():
    e, _ = run(BASE + [C(9, 35, 101.0, 103, 100.9, 102.8), C(9, 40, 103, 107.6, 102.9, 107.5)])       # the mirror: the SELL gaps up to 99.0, 0.8 above the reference 98.2
    t = e.closed[0]
    assert t["entry_price"] == pytest.approx(m(101.0)) and t["stop"] == pytest.approx(m(97.0))
    assert t["target"] == pytest.approx(m(106.6 + 0.8)) and t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(m(107.4))


def test_the_stop_is_a_fixed_price_and_a_stop_out_is_a_loss():
    e, _ = run(BASE + [C(9, 35, 102.5, 103, 101.5, 102.8), C(9, 40, 102.8, 103, 96.5, 97.0)])
    t = e.closed[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(m(97.0)) and t["net_pnl"] == pytest.approx(-4.8)


def test_no_trade_when_price_reaches_the_target_before_coming_back():
    e, ev = run(BASE + [C(9, 35, 102.5, 107, 102.4, 106.9)])
    assert e.closed == [] and e.position is None
    assert any("reached the target 93.40 without coming back to 98.20" in n for n in notes(ev))


def test_a_candle_closing_above_the_day_open_cancels_the_setup():
    candles = BASE[:6] + [C(9, 30, 101.8, 101.95, 100.0, 100.8), C(9, 35, 100.8, 100.9, 99.0, 99.5), C(9, 40, 99.5, 103, 99.4, 102.9), C(9, 45, 102.9, 103.0, 102.8, 102.9)]
    e, ev = run(candles)
    assert e.closed == [] and e.position is None
    assert any("above the day open level 100.00: the setup is cancelled" in n for n in notes(ev))
    assert e.st == "seq" and e.S["n"] == 2


def test_no_confirmation_candle_the_shift_candle_is_the_reference():
    candles = BASE[:4] + [C(9, 20, 100.5, 100.7, 100.1, 100.3), C(9, 25, 100.3, 101.5, 100.2, 101.2), C(9, 30, 101.0, 101.1, 100.4, 100.6), C(9, 35, 100.6, 104.2, 100.5, 104.1)]
    e, _ = run(candles)
    t = e.closed[0]
    assert t["entry_price"] == pytest.approx(m(100.5)) and t["stop"] == pytest.approx(m(97.0)) and t["target"] == pytest.approx(m(104.0)) and t["exit_reason"] == "target"


def test_the_stop_search_walks_back_while_the_highs_keep_rising_and_stops_at_a_lower_high():
    def day(first_low):
        return [C(9, 0, 100, 100.5, first_low, 99.5), C(9, 5, 99.5, 99.9, 98.0, 99.2), C(9, 10, 99.2, 99.4, 98.5, 99.0), C(9, 15, 99.0, 100.8, 98.9, 100.5),
                C(9, 20, 100.5, 100.7, 100.1, 100.3)]
    e, _ = run(day(99.0))
    assert e.S["stop"] == pytest.approx(m(98.0))
    e2, _ = run(day(95.0))
    assert e2.S["stop"] == pytest.approx(m(95.0))


def test_several_setups_in_one_day_one_position_at_a_time():
    candles = BASE + [C(9, 35, 102.5, 103, 101.5, 102.8), C(9, 40, 102.8, 103, 96.5, 97.0), C(9, 45, 97.0, 97.5, 96.8, 97.2), C(9, 50, 97.2, 101.0, 97.1, 100.9),
                      C(9, 55, 100.9, 101.6, 100.6, 101.4), C(10, 0, 101.2, 101.5, 100.4, 100.9), C(10, 5, 101.3, 102.0, 101.2, 101.9),
                      C(10, 10, 101.9, 102.0, 101.0, 101.1), C(10, 15, 101.1, 107.0, 101.0, 106.9)]
    e, ev = run(candles)
    assert [(round(t["entry_price"], 2), t["exit_reason"]) for t in e.closed] == [(m(101.8), "stop"), (m(101.4), "target")]
    t2 = e.closed[1]
    assert t2["stop"] == pytest.approx(m(96.5)) and t2["net_pnl"] == pytest.approx(4.9)
    assert sum(1 for n in notes(ev) if "market shift" in n) == 2


def test_levels_status_and_listing():
    e, _ = run(BASE + [C(9, 35, 102.5, 102.6, 102.2, 102.4)])
    assert [x["name"] for x in e.levels()] == ["day open", "breakout level", "entry (ref close)", "stop", "target"]
    assert [round(x["value"], 2) for x in e.levels()] == [100.0, m(102.0), m(101.8), m(97.0), m(106.6)]
    assert "SELL waits at 98.20" in e.status_text(99.0)
    from dryrun.strategy import STRATEGIES
    spec = STRATEGIES["market_shift_sell"]
    assert spec.label == "Market shift SELL" and spec.guide and spec.fields == [] and not spec.hidden
