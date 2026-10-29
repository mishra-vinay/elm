"""Market shift strategy (BUY, 5-minute candles): shift above the day open, climbing confirmation sequence, reference candle, breakout, a BUY waiting at the
reference close, stop from the backward walk, target 1:1 from the reference (plus a better fill), several setups a day but one position at a time."""
from contextlib import contextmanager
from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from dryrun.replay import candle_path, feed_path
from dryrun.strategies.market_shift import RULES, MarketShiftEngine
from dryrun.strategy import Params, make_engine
from test_pages_and_settings import sim_app

D0 = datetime(2026, 10, 13)
pytestmark = pytest.mark.usefixtures("only_shipped")


def P():
    return Params(strategy="market_shift", slippage_pct=0, cost_pct=0, daily_loss_limit_pct=None)


@contextmanager
def rules(**r):
    old = dict(RULES)
    RULES.update(r)
    try:
        yield
    finally:
        RULES.clear()
        RULES.update(old)


def C(h, m, o, hi, lo, c):
    return {"ts": D0.replace(hour=h, minute=m), "open": o, "high": hi, "low": lo, "close": c}


# the day open is 100. Candles 09:00 to 09:30 build the setup: stop search finds 97 (09:05 low), the shift candle is 09:15, the sequence 09:20 and 09:25 climbs,
# 09:30 is not higher (lower low), so the reference is 09:25: high 102.0, close 101.8; 09:30 broke the high (102.2).
# Stop 97, risk 4.8, target 106.6; the BUY waits at 101.8.
BASE = [C(9, 0, 100, 100.5, 98, 98.5), C(9, 5, 98.5, 99, 97, 98), C(9, 10, 98, 98.6, 97.5, 98.4), C(9, 15, 98.4, 100.8, 98.3, 100.5),
        C(9, 20, 100.5, 101.5, 100.2, 101.2), C(9, 25, 101.2, 102.0, 100.9, 101.8), C(9, 30, 101.8, 102.2, 100.0, 100.8)]


def run(candles, **r):
    with rules(**r):
        eng = make_engine(120.0, 90.0, P())
        eng.session_open = time(9, 0)
        evs = []
        orig = eng.on_tick
        eng.on_tick = lambda ts, p: (lambda e: (evs.extend(e), e)[1])(orig(ts, p))
        last = None
        for c in candles:
            path = candle_path(c["open"], c["high"], c["low"], c["close"])
            feed_path(eng, c["ts"], path)
            last = c["ts"] + timedelta(seconds=len(path))
        eng.square_off(last, reason="squareoff")
        return eng, evs


def notes(evs):
    return [e.reason for e in evs if e.reason]


def test_the_reference_candle_the_breakout_and_a_fill_at_the_reference_close_to_the_target():
    e, ev = run(BASE + [C(9, 35, 102.5, 103, 101.5, 102.8), C(9, 40, 103, 107, 102.9, 106.8)])
    assert len(e.closed) == 1
    t = e.closed[0]
    assert t["side"] == "BUY" and t["entry_price"] == pytest.approx(101.8) and t["stop"] == pytest.approx(97.0) and t["target"] == pytest.approx(106.6)
    assert t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(106.6) and t["net_pnl"] == pytest.approx(4.8)
    text = " | ".join(notes(ev))
    assert "market shift" in text and "Reference candle 09:25" in text and "high 102.00, close 101.80" in text and "broke above 102.00" in text


def test_a_fill_below_the_reference_price_keeps_the_stop_and_adds_the_difference_to_the_target():
    e, _ = run(BASE + [C(9, 35, 101.0, 103, 100.9, 102.8), C(9, 40, 103, 107.6, 102.9, 107.5)])      # the next candle opens at 101.0, 0.8 below the reference 101.8
    t = e.closed[0]
    assert t["entry_price"] == pytest.approx(101.0) and t["stop"] == pytest.approx(97.0)
    assert t["target"] == pytest.approx(106.6 + 0.8) and t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(107.4)


def test_the_stop_is_a_fixed_price_and_a_stop_out_is_a_loss():
    e, _ = run(BASE + [C(9, 35, 102.5, 103, 101.5, 102.8), C(9, 40, 102.8, 103, 96.5, 97.0)])
    t = e.closed[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(97.0) and t["net_pnl"] == pytest.approx(-4.8)


def test_no_trade_when_price_reaches_the_target_before_coming_back():
    e, ev = run(BASE + [C(9, 35, 102.5, 107, 102.4, 106.9)])
    assert e.closed == [] and e.position is None
    assert any("reached the target 106.60 without coming back to 101.80" in n for n in notes(ev))


def test_no_trade_when_price_never_comes_back_to_the_reference_price():
    e, _ = run(BASE + [C(9, 35, 102.5, 103.5, 102.2, 103.4), C(9, 40, 103.4, 104, 102.9, 103.8)])
    assert e.closed == [] and e.position is None


def test_a_candle_closing_below_the_day_open_cancels_the_setup_and_a_later_break_is_ignored():
    candles = BASE[:6] + [C(9, 30, 101.8, 101.95, 100.0, 100.8),         # the reference 09:25 is not broken (high 101.95 < 102.0)
                          C(9, 35, 100.8, 100.9, 99.0, 99.5),            # closes below the day open level 100: the setup is cancelled
                          C(9, 40, 99.5, 103, 99.4, 102.9),              # breaks 102.0 and closes above the level: that is a new shift candle, not a trade
                          C(9, 45, 102.9, 103.0, 102.8, 102.9)]          # (a candle is only complete when the next one starts)
    e, ev = run(candles)
    assert e.closed == [] and e.position is None
    assert any("below the day open level 100.00: the setup is cancelled" in n for n in notes(ev))
    assert e.st == "seq" and e.S["n"] == 2                                 # the 09:40 close started a fresh, independent setup


def test_a_dip_below_the_level_without_a_close_below_it_does_not_cancel():
    candles = BASE[:6] + [C(9, 30, 101.8, 101.95, 99.5, 100.8),           # the low goes below 100 but the candle closes at 100.8
                          C(9, 35, 100.8, 103, 100.6, 102.5)]            # breaks 102.0 ...
    candles += [C(9, 40, 102.5, 102.6, 101.5, 101.7)]                    # ... then back to 101.8 (the 09:40 candle trades down through it)
    e, _ = run(candles)
    assert e.position is None and len(e.closed) == 1 and e.closed[0]["entry_price"] == pytest.approx(101.8) and e.closed[0]["exit_reason"] == "squareoff"


def test_no_confirmation_candle_the_shift_candle_is_the_reference():
    candles = BASE[:4] + [C(9, 20, 100.5, 100.7, 100.1, 100.3),           # not higher than the shift candle: the shift candle (09:15) is the reference
                          C(9, 25, 100.3, 101.5, 100.2, 101.2),          # breaks its high 100.8
                          C(9, 30, 101.0, 101.1, 100.4, 100.6),          # comes back to its close 100.5
                          C(9, 35, 100.6, 104.2, 100.5, 104.1)]
    e, _ = run(candles)
    t = e.closed[0]
    assert t["entry_price"] == pytest.approx(100.5) and t["stop"] == pytest.approx(97.0) and t["target"] == pytest.approx(104.0) and t["exit_reason"] == "target"


def test_the_stop_search_walks_back_while_the_lows_keep_falling_and_stops_at_a_higher_low():
    def day(first_low):
        return [C(9, 0, 100, 100.5, first_low, 99.5), C(9, 5, 99.5, 99.9, 98.0, 99.2), C(9, 10, 99.2, 99.4, 98.5, 99.0), C(9, 15, 99.0, 100.8, 98.9, 100.5),
                C(9, 20, 100.5, 100.7, 100.1, 100.3)]                          # the last candle is not higher: the sequence ends and the stop is fixed
    e, _ = run(day(99.0))                       # from 09:10 (98.5) back: 09:05 low 98.0 is lower, continue; 09:00 low 99.0 is higher: stop. The stop is 98.0
    assert e.S["stop"] == pytest.approx(98.0)
    e2, _ = run(day(95.0))                      # 09:00 low 95.0 is lower still: the walk reaches the start of the day. The stop is 95.0
    assert e2.S["stop"] == pytest.approx(95.0)


def test_several_setups_in_one_day_one_position_at_a_time():
    candles = BASE + [C(9, 35, 102.5, 103, 101.5, 102.8),                 # setup 1 fills at 101.8
                      C(9, 40, 102.8, 103, 96.5, 97.0),                   # stopped out at 97.0 (-4.8)
                      C(9, 45, 97.0, 97.5, 96.8, 97.2),
                      C(9, 50, 97.2, 101.0, 97.1, 100.9),                 # setup 2: a new shift candle (the close is above 100)
                      C(9, 55, 100.9, 101.6, 100.6, 101.4),               # climbs
                      C(10, 0, 101.2, 101.5, 100.4, 100.9),               # not higher: the reference is 09:55 (high 101.6, close 101.4)
                      C(10, 5, 101.3, 102.0, 101.2, 101.9),               # breaks 101.6
                      C(10, 10, 101.9, 102.0, 101.0, 101.1),              # back to 101.4: the second BUY
                      C(10, 15, 101.1, 107.0, 101.0, 106.9)]              # target 106.3
    e, ev = run(candles)
    assert [(round(t["entry_price"], 2), t["exit_reason"]) for t in e.closed] == [(101.8, "stop"), (101.4, "target")]
    t2 = e.closed[1]
    assert t2["stop"] == pytest.approx(96.5) and t2["target"] == pytest.approx(101.4 + (101.4 - 96.5)) and t2["net_pnl"] == pytest.approx(4.9)
    assert sum(1 for n in notes(ev) if "market shift" in n) == 2           # the candles while the first trade was open did not start a setup


def test_no_new_setup_while_a_trade_is_open():
    candles = BASE + [C(9, 35, 102.5, 103, 101.5, 102.8),                 # fills 101.8; the position stays open
                      C(9, 40, 102.8, 103.5, 102.6, 103.3), C(9, 45, 103.3, 104.5, 103.0, 104.2), C(9, 50, 104.2, 105.0, 103.9, 104.8)]
    e, ev = run(candles)
    assert sum(1 for n in notes(ev) if "market shift" in n) == 1 and len(e.closed) == 1 and e.closed[0]["exit_reason"] == "squareoff"


def test_the_per_day_trade_limit_does_not_apply():
    p = Params(strategy="market_shift", slippage_pct=0, cost_pct=0, max_trades_per_day=1)
    eng = make_engine(120.0, 90.0, p)
    assert isinstance(eng, MarketShiftEngine)
    eng.trades_by_side["BUY"] = 5
    assert eng.block_reason("BUY", D0, 100.0) is None
    eng.net_pct_sum = -3.0                                                  # the run's daily loss limit (2%) still applies
    assert eng.block_reason("BUY", D0, 100.0) == "daily loss limit reached"


def test_the_cut_off_setting_cancels_a_waiting_order():
    e, ev = run(BASE + [C(9, 35, 102.5, 103.5, 102.2, 103.4), C(9, 40, 103.4, 103.5, 103.0, 103.2)], last_entry=time(9, 36))
    assert e.closed == [] and e.st == "idle"
    e2, ev2 = run(BASE + [C(9, 35, 102.5, 103.5, 102.2, 103.4), C(9, 40, 103.4, 103.5, 100.5, 101.0)], last_entry=time(9, 38))
    assert e2.closed == [] and any("not filled before 09:38" in n for n in notes(ev2))


def test_levels_for_the_chart_and_the_status_text():
    e, _ = run(BASE + [C(9, 35, 102.5, 102.6, 102.2, 102.4)])             # the 09:35 candle starts: the 09:30 candle is complete and the BUY waits
    names = [x["name"] for x in e.levels()]
    assert names == ["day open", "breakout level", "entry (ref close)", "stop", "target"]
    assert [x["value"] for x in e.levels()] == [100.0, 102.0, 101.8, 97.0, 106.6]
    assert "BUY waits at 101.80" in e.status_text(101.0)


def test_listed_with_a_step_by_step_guide_and_the_strategies_page(tmp_path):
    with TestClient(sim_app(tmp_path, [1.0])) as c:
        data = c.get("/api/strategies").json()["strategies"]
        ms = next(x for x in data if x["name"] == "market_shift")
        assert ms["label"] == "Market shift strategy" and ms["fields"] == [] and ms["guide"] and ms["guide"][0]["title"] == "Idea"
        assert all(x["guide"] for x in data)                                                  # every listed strategy is described on the Strategies page
        page = c.get("/strategies")
        assert page.status_code == 200 and "Strategies" in page.text
        assert any(x["name"] == "market_shift" for x in c.get("/api/strategies").json()["strategies"])
